"""Removal runner: enforces the removal rules; the actual click is a pluggable adapter.

By design the owner's own signed-in browser (driven by the agent) performs X's native "Remove this follower".
This module never talks to X itself. It:
  1. only acts on a RECORDED, AUTHORIZED selection (record_chat_selection history entries for the snapshot);
  2. refuses a snapshot older than max_snapshot_age_days (default 7);
  3. rechecks protections immediately before each removal, fail-safe (protections.execution_protection_reason):
     followed, verified, Always Keep, protected_handles; ID first, handle fallback; missing data = protected;
  0. one run at a time per user (run.lock, shared with the block runner). A lock left by an interrupted run means
     that run MAY have finished some removals on X: reconcile_interrupted_run() checks each account on X first;
  3b. the recheck also covers recent interactions with the owner (adapter searches X both ways), mutuals (you follow
     them), verified, Always Keep and Keepers (Not a bot). Missing interaction data = protected;
  4. caps removals at daily_removal_cap per user per calendar day in the user's time zone (asked at setup; suggested
     start 10; default 10 if never set; never > 100),
     counting earlier removals AND Graveyard-return blocks today from history (one shared cap);
  5. paces removals with a FIXED interval (PACING_SECONDS, no jitter);
  6. stops on the first X warning or rate limit, records it and refuses further removals and blocks that day;
  7. removes only: this path calls lookup() and remove() and nothing else (no block, unfollow, mute, DM or post).
     Graveyard returns are excluded here; they are blocked, with the owner's authorization, only via
     graveyard_returns.run_blocks (which in turn never calls remove());
  6b. confirms each removal on X (lookup() again: the account no longer follows the owner) BEFORE logging it as
     REMOVED; an unconfirmed removal is logged REMOVAL_NOT_CONFIRMED and the run stops;
  8. appends every result to the user's hash-chained history.jsonl and a graveyard record, and prints a summary.
There is no practice mode: a run against a real adapter is a real run.

Adapter interface (implemented by the agent's browser bridge):
    lookup(account: dict) -> dict   # fresh {user_id, handle, i_follow, verified_type, follows_me,
                                    #        recent_interaction}; use None for unknown (unknown = protected)
    remove(account: dict) -> RemovalResult(status in {"ok","warning","rate_limited","error"}, message)
    block(account: dict)  -> RemovalResult   # used ONLY by graveyard_returns.run_blocks for authorized returnees

CLI:
    python -m src.removal_runner --user <key> --snapshot <snapshot_id> --adapter package.module:AdapterClass
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

try:
    from . import storage as st
    from . import preflight
    from .protections import execution_protection_reason
except ImportError:  # pragma: no cover
    import storage as st
    import preflight
    from protections import execution_protection_reason

PACING_SECONDS = 10.0          # fixed; no random jitter or human-mimicking timing
MAX_DAILY_CAP = 100            # hard ceiling; config may only lower it
DEFAULT_DAILY_CAP = 10         # 2026-10-20: the cap is asked at setup (user picks, max 100); suggest starting low
SUGGESTED_START_CAP = 10
LOCK_NAME = "run.lock"
DEFAULT_MAX_SNAPSHOT_AGE_DAYS = 7
OK, WARNING, RATE_LIMITED, ERROR = "ok", "warning", "rate_limited", "error"
STOP_STATUSES = ("REMOVAL_STOPPED", "BLOCK_STOPPED", "LIST_STOPPED")  # a locking stop of any kind pauses ALL for the day
# Shared daily cap. A list soft block (block + unblock on X) carries cap_weight 2; everything else counts 1.
COUNTED_ACTIONS = ("REMOVED", "BLOCKED", "LIST_SOFT_BLOCKED", "LIST_OWNER_BLOCKED", "LIST_UNBLOCK_FAILED",
                   "LIST_SOFTBLOCK_NOT_CONFIRMED")
AUTHORIZED_PREFIXES = ("SELECT_ALL_OWNER_YES", "SELECTED_OWNER_YES", "MANUAL_CONFIRMED_OWNER_YES",
                       "MUTUAL_REMOVE_OWNER_YES")
SELECTION_STATUSES = ("CHAT_SELECTION", "MANUAL_CONFIRMATION", "REVIEW_FOLLOWUP", "INACTIVE_MUTUAL_SELECTION")


@dataclass
class RemovalResult:
    status: str                 # ok | warning | rate_limited | error
    message: str = ""


class RemovalAdapter(Protocol):
    def lookup(self, account: dict) -> dict: ...
    def remove(self, account: dict) -> RemovalResult: ...


class RefusedError(RuntimeError):
    """The runner refused to start (no authorized selection, stale snapshot, day locked, cap reached...)."""


def _rcs():
    try:
        from . import record_chat_selection as rcs
    except ImportError:  # pragma: no cover
        import record_chat_selection as rcs
    return rcs


def _history(user_key: str, root) -> list[dict]:
    p = st.history_path(root, user_key=user_key)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _tz(cfg: dict) -> dt.tzinfo:
    name = cfg.get("time_zone")
    if name:
        return ZoneInfo(name)
    return dt.datetime.now().astimezone().tzinfo  # machine's local zone


def daily_cap(cfg: dict) -> int:
    try:
        v = int(cfg.get("daily_removal_cap", DEFAULT_DAILY_CAP))
    except (TypeError, ValueError):
        v = DEFAULT_DAILY_CAP
    return max(0, min(v, MAX_DAILY_CAP))


def _local_day(ts: str, tz) -> str | None:
    try:
        return dt.datetime.fromisoformat(ts).astimezone(tz).date().isoformat()
    except (TypeError, ValueError):
        return None


def removals_today(hist: list[dict], today: str, tz) -> int:
    return sum(int(e.get("cap_weight", 1)) for e in hist
               if e.get("status") in COUNTED_ACTIONS and _local_day(e.get("at", ""), tz) == today)


def day_locked(hist: list[dict], today: str, tz) -> dict | None:
    for e in reversed(hist):
        if e.get("status") in STOP_STATUSES and e.get("locks_day") and _local_day(e.get("at", ""), tz) == today:
            return e
    return None


def authorized_accounts(hist: list[dict], snapshot_id: str) -> list[dict]:
    """Selected accounts from recorded, authorized chat selections for this snapshot (dedup by ID/handle).

    Selections recorded before the latest warning/rate-limit stop are void: after a stop, removals resume
    (on a later day) only from a NEW authorized selection."""
    out, seen = [], set()
    start = 0
    for i, e in enumerate(hist):
        if e.get("status") in STOP_STATUSES and e.get("locks_day"):
            start = i + 1
    for e in hist[start:]:
        if e.get("status") not in SELECTION_STATUSES or e.get("snapshot_id") != snapshot_id:
            continue
        if not str(e.get("authorization") or "").startswith(AUTHORIZED_PREFIXES):
            continue
        for a in e.get("selected") or []:
            key = st_key(a)
            if key and key not in seen:
                seen.add(key)
                out.append(dict(a, authorized_by=e.get("entry_id"),
                                inactive_mutual_ok=e.get("status") == "INACTIVE_MUTUAL_SELECTION"))
    return out


def st_key(a: dict) -> str:
    uid = str(a.get("x_user_id") or a.get("user_id") or "").lstrip("'")
    return uid if uid.isdigit() else str(a.get("handle") or "").lstrip("@").lower()


def keepers_from_history(hist: list[dict]) -> list[dict]:
    """Keepers (Not a bot / Always Keep decisions) recorded on review and final-review cards."""
    out = []
    for e in hist:
        if e.get("status") == "REVIEW_DECISION":
            for k, kind in (e.get("decisions") or {}).items():
                if kind in ("NOT_A_BOT", "ALWAYS_KEEP"):
                    h = (e.get("handles") or {}).get(k)
                    out.append({"x_user_id": k if str(k).isdigit() else None, "handle": h or (None if str(k).isdigit() else k)})
        elif e.get("status") == "CHAT_FINAL_REVIEW":
            out.extend(e.get("keepers") or [])
    return out


class run_lock:
    """One removal/block run at a time per user. The lock file stays behind if the process dies mid-run."""

    def __init__(self, user_key: str, root, kind: str, snapshot_id: str):
        self.p = st.user_data_root(user_key, root) / LOCK_NAME
        self.info = {"kind": kind, "snapshot_id": snapshot_id, "started_at": dt.datetime.now().astimezone().isoformat()}

    def __enter__(self):
        import os
        self.p.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            other = st.load_json(self.p, {}) or {}
            raise RefusedError(
                f"another {other.get('kind', 'removal')} run is in progress or was interrupted (started "
                f"{other.get('started_at', 'unknown')}). An interrupted run may have finished some actions on X: "
                "check each account on X with reconcile_interrupted_run() before re-running or reporting.")
        with os.fdopen(fd, "w") as f:
            json.dump(self.info, f)
        return self

    def __exit__(self, *exc):
        try:
            self.p.unlink()
        except FileNotFoundError:
            pass
        return False


def lock_info(user_key: str, root=None) -> dict | None:
    p = st.user_data_root(user_key, root) / LOCK_NAME
    return (st.load_json(p, {}) or {"kind": "unknown"}) if p.exists() else None


def _already_removed(hist: list[dict]) -> set[str]:
    return {e.get("account_key") for e in hist if e.get("status") == "REMOVED"}


def _snapshot_age_ok(user_key: str, snapshot_id: str, root, now: dt.datetime, max_days: float) -> None:
    meta = st.load_json(st.snapshot_dir(snapshot_id, root, user_key=user_key) / "meta.json", None)
    if not meta:
        raise RefusedError(f"snapshot {snapshot_id} not found for this user")
    ts = meta.get("scan_completed") or meta.get("created_at")
    try:
        made = dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        raise RefusedError("snapshot has no valid creation time; refusing")
    if made.tzinfo is None:
        made = made.astimezone()
    if now - made > dt.timedelta(days=max_days):
        raise RefusedError(f"snapshot {snapshot_id} is older than {max_days:g} days; run a new scan")


def _append(user_key: str, root, entry: dict) -> None:
    rcs = _rcs()
    old = rcs.ROOT
    rcs.ROOT = Path(root) if root is not None else st.PROJECT_ROOT
    try:
        prev, last_n = rcs._prev_hash(rcs.history_path(user_key))
        entry = {"entry_id": f"TH-{last_n + 1:04d}", "prev_entry_sha256": prev, "user_key": user_key, **entry}
        rcs.append_history(entry, user_key=user_key)
    finally:
        rcs.ROOT = old


def _graveyard(user_key: str, root, rec: dict) -> None:
    p = st.user_data_root(user_key, root) / "graveyard.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    st._mirror(lambda r: r.record_graveyard_event, user_key, rec, root=root)  # v3.5.0 write-through


def run_removals(user_key: str, snapshot_id: str, adapter: RemovalAdapter, *, root=None,
                 now_fn: Callable[[], dt.datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
                 start_followers: int | None = None, out=sys.stdout) -> dict[str, Any]:
    st.validate_user_key(user_key)
    with run_lock(user_key, root, "removal", snapshot_id):
        return _run_removals(user_key, snapshot_id, adapter, root=root, now_fn=now_fn, sleep=sleep,
                             start_followers=start_followers, out=out)


def _run_removals(user_key: str, snapshot_id: str, adapter: RemovalAdapter, *, root=None,
                  now_fn: Callable[[], dt.datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
                  start_followers: int | None = None, out=sys.stdout) -> dict[str, Any]:
    cfg = st.load_user_config_dict(root, user_key=user_key)
    preflight.require(user_key, root, cfg)  # inside Grok Bot: Google Sheets + Drive must be confirmed
    tz = _tz(cfg)
    now_fn = now_fn or (lambda: dt.datetime.now(tz))
    now = now_fn().astimezone(tz)
    today = now.date().isoformat()
    hist = _history(user_key, root)

    _snapshot_age_ok(user_key, snapshot_id, root, now,
                     float(cfg.get("max_snapshot_age_days", DEFAULT_MAX_SNAPSHOT_AGE_DAYS)))
    lock = day_locked(hist, today, tz)
    if lock:
        raise RefusedError(f"removals are paused for today after an X {lock.get('reason')}; "
                           "resume tomorrow with a new authorized run")
    accounts = authorized_accounts(hist, snapshot_id)
    if not accounts:
        raise RefusedError("no recorded, authorized selection for this snapshot; nothing to remove")
    done = _already_removed(hist)
    accounts = [a for a in accounts if st_key(a) not in done]
    # Graveyard returns are never removed: they go to the separate, authorized block flow (graveyard_returns).
    returnees = {st_key(r) for r in (st.load_json(st.snapshot_dir(snapshot_id, root, user_key=user_key)
                                                  / "graveyard_returns.json", []) or [])}
    accounts = [a for a in accounts if st_key(a) not in returnees]
    cap = daily_cap(cfg)
    used = removals_today(hist, today, tz)
    if used >= cap:
        raise RefusedError(f"daily cap reached ({used}/{cap} removals today)")

    always_keep = st.load_always_keep(root, user_key=user_key)
    keepers = keepers_from_history(hist)
    protected_handles = cfg.get("protected_handles") or []
    removed, skipped, errors = [], [], []
    stopped = None
    first = True
    for a in accounts:
        if used >= cap:
            stopped = {"reason": "daily cap", "message": f"{cap} removals today"}
            break
        fresh = adapter.lookup(dict(a)) or {}
        why = execution_protection_reason(fresh, always_keep=always_keep, protected_handles=protected_handles,
                                          recorded=a, keepers=keepers, require_interaction=True,
                                          waive_follow=bool(a.get("inactive_mutual_ok")))
        key = st_key(a)
        if not why and fresh.get("follows_me") is False:
            _append(user_key, root, {"status": "REMOVAL_SKIPPED_NOT_FOLLOWING", "at": now_fn().isoformat(),
                                     "snapshot_id": snapshot_id, "account_key": key, "handle": a.get("handle"),
                                     "reason": "no longer follows you (checked on X)", "action": "NONE"})
            skipped.append({"handle": a.get("handle"), "reason": "no longer follows you"})
            continue
        if why:
            skipped.append({"handle": a.get("handle"), "reason": why})
            _append(user_key, root, {"status": "REMOVAL_SKIPPED_PROTECTED", "at": now_fn().isoformat(),
                                     "snapshot_id": snapshot_id, "account_key": key, "handle": a.get("handle"),
                                     "reason": why, "action": "NONE"})
            continue
        if not first:
            sleep(PACING_SECONDS)
        first = False
        res = adapter.remove(dict(a))
        at = now_fn().isoformat()
        status = getattr(res, "status", ERROR)
        msg = getattr(res, "message", "")
        if status == OK:
            # Confirm on X before logging: the account must no longer follow the owner.
            check = adapter.lookup(dict(a)) or {}
            if check.get("follows_me") is not False:
                stopped = {"reason": "not confirmed",
                           "message": "X still shows this account following you" if check.get("follows_me")
                           else "could not confirm on X that the account no longer follows you"}
                _append(user_key, root, {"status": "REMOVAL_NOT_CONFIRMED", "at": at, "snapshot_id": snapshot_id,
                                         "account_key": key, "handle": a.get("handle"), "reason": stopped["message"],
                                         "locks_day": False, "action": "NONE (stopped; check on X)"})
                errors.append({"handle": a.get("handle"), "message": stopped["message"]})
                break
            used += 1
            removed.append(a.get("handle"))
            _append(user_key, root, {"status": "REMOVED", "at": at, "snapshot_id": snapshot_id, "account_key": key,
                                     "handle": a.get("handle"), "x_user_id": a.get("x_user_id") or a.get("user_id"),
                                     "authorized_by": a.get("authorized_by"), "confirmed_on_x": True,
                                     "action": "X native 'Remove this follower' (via adapter)", "message": msg})
            _graveyard(user_key, root, {"event": "REMOVED", "at": at, "handle": a.get("handle"),
                                        "x_user_id": a.get("x_user_id") or a.get("user_id"),
                                        "snapshot_id": snapshot_id, "method": "remove follower"})
            continue
        locks = status in (WARNING, RATE_LIMITED)
        stopped = {"reason": "warning" if status == WARNING else "rate limit" if locks else "error", "message": msg}
        _append(user_key, root, {"status": "REMOVAL_STOPPED", "at": at, "snapshot_id": snapshot_id,
                                 "account_key": key, "handle": a.get("handle"), "reason": stopped["reason"],
                                 "message": msg, "locks_day": locks, "action": "NONE (stopped)"})
        if not locks:
            errors.append({"handle": a.get("handle"), "message": msg})
        break

    st.apply_user_permissions(user_key, root)
    summary = {"user_key": user_key, "snapshot_id": snapshot_id, "start_followers": start_followers,
               "removed": len(removed), "removed_handles": removed, "skipped_protected": len(skipped),
               "skipped": skipped, "removals_today": used, "daily_cap": cap,
               "stopped_reason": stopped["reason"] if stopped else None,
               "stopped_message": stopped["message"] if stopped else None}
    lines = [f"Start follower count: {start_followers if start_followers is not None else 'Not available'}",
             f"Removed: {len(removed)}", f"Skipped (protected): {len(skipped)}",
             f"Removals today: {used}/{cap}",
             f"Stopped: {stopped['reason'] + (' - ' + stopped['message'] if stopped['message'] else '') if stopped else 'no'}"]
    if stopped and stopped["reason"] in ("warning", "rate limit"):
        lines.append("No more removals today. Resume tomorrow with a new authorized run.")
    print("\n".join(lines), file=out)
    return summary


def reconcile_interrupted_run(user_key: str, snapshot_id: str, adapter: RemovalAdapter, *, root=None,
                              now_fn: Callable[[], dt.datetime] | None = None, out=sys.stdout) -> dict[str, Any]:
    """After an interrupted run: check each authorized, not-yet-logged account ON X (lookup -> follows_me) and log
    the ones already gone as REMOVED (confirmed_on_x, counted toward today's cap). Clears the run lock only when
    every account's state is known. Never removes anything itself."""
    st.validate_user_key(user_key)
    hist = _history(user_key, root)
    now_fn = now_fn or (lambda: dt.datetime.now().astimezone())
    done = _already_removed(hist)
    gone, still, unknown = [], [], []
    for a in authorized_accounts(hist, snapshot_id):
        key = st_key(a)
        if key in done:
            continue
        fm = (adapter.lookup(dict(a)) or {}).get("follows_me")
        if fm is False:
            at = now_fn().isoformat()
            _append(user_key, root, {"status": "REMOVED", "at": at, "snapshot_id": snapshot_id, "account_key": key,
                                     "handle": a.get("handle"), "x_user_id": a.get("x_user_id") or a.get("user_id"),
                                     "authorized_by": a.get("authorized_by"), "confirmed_on_x": True,
                                     "action": "found removed on X after an interrupted run (reconciled)"})
            _graveyard(user_key, root, {"event": "REMOVED", "at": at, "handle": a.get("handle"),
                                        "x_user_id": a.get("x_user_id") or a.get("user_id"),
                                        "snapshot_id": snapshot_id, "method": "remove follower (reconciled)"})
            gone.append(a.get("handle"))
        elif fm is True:
            still.append(a.get("handle"))
        else:
            unknown.append(a.get("handle"))
    p = st.user_data_root(user_key, root) / LOCK_NAME
    cleared = False
    if not unknown and p.exists():
        p.unlink(); cleared = True
    st.apply_user_permissions(user_key, root)
    print(f"Reconciled: {len(gone)} already removed on X, {len(still)} still following, {len(unknown)} unknown; "
          f"lock {'cleared' if cleared else 'kept (check the unknown ones on X)' if unknown else 'not present'}", file=out)
    return {"already_removed": gone, "still_following": still, "unknown": unknown, "lock_cleared": cleared}


def record_manual_confirmation(user_key: str, snapshot_id: str, accounts: list[dict], *, root=None,
                               confirmed_text: str = "") -> None:
    """Manual mode: record the owner's explicit yes for specific selected accounts (makes them authorized)."""
    _append(user_key, root, {"status": "MANUAL_CONFIRMATION", "at": dt.datetime.now().astimezone().isoformat(),
                             "snapshot_id": snapshot_id, "authorization": "MANUAL_CONFIRMED_OWNER_YES",
                             "selected": [{"handle": a.get("handle"), "x_user_id": a.get("x_user_id") or a.get("user_id")}
                                          for a in accounts], "raw_answer": confirmed_text, "action": "NONE"})


def _load_adapter(spec: str):
    mod, _, cls = spec.partition(":")
    if not mod or not cls:
        raise ValueError("--adapter must look like package.module:ClassName")
    return getattr(importlib.import_module(mod), cls)()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Run authorized follower removals through an adapter (no practice mode)")
    p.add_argument("--user", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--adapter", required=True, help="package.module:ClassName implementing lookup()/remove()")
    p.add_argument("--start-followers", type=int)
    p.add_argument("--root", type=Path, default=st.PROJECT_ROOT)
    a = p.parse_args(argv)
    try:
        adapter = _load_adapter(a.adapter)
        s = run_removals(a.user, a.snapshot, adapter, root=a.root, start_followers=a.start_followers)
    except (RefusedError, ValueError, ImportError, AttributeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 1 if s["stopped_reason"] in ("warning", "rate limit", "error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
