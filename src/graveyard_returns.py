"""Graveyard returns: detect previously removed accounts that follow again, and BLOCK them only when authorized.

Normal termination sessions (removal_runner.run_removals) only ever REMOVE followers. A returnee is different:
it was already removed once (it is in the user's graveyard.jsonl), so the action is a BLOCK, which stops it from
re-attaching. Rules enforced here:
  - Detection: a follower in a new scan whose X user ID is in graveyard.jsonl (handle fallback only when an ID is
    missing on either side) is flagged GRAVEYARD_RETURN in candidates.json, listed in graveyard_returns.json and
    reports/graveyard_returns_<snapshot>.csv, and kept OUT of normal removal cards and removal runs.
  - Authorization: a block runs only for a returnee named in a recorded BLOCK_AUTHORIZATION history entry for that
    snapshot (record_block_authorization). Per-user config "graveyard_return_action": "auto_block" lets the owner's
    standing setting authorize detected returnees; the default for new users is "review" (user-authorized).
  - Same safeguards as removals: fresh protection recheck right before each block (followed, verified, Always Keep,
    protected IDs/handles; ID first; missing data = protected), fixed 10 s pacing, SHARED daily cap (removals +
    blocks, max 100, user's time zone), stop and lock the day on the first X warning or rate limit, stale-snapshot
    refusal, everything appended to the hash-chained history.jsonl.
  - The block path calls only adapter.lookup() and adapter.block(); never remove(), unfollow, mute, DM or post.
  - graveyard.jsonl is append-only: RETURN_DETECTED and BLOCKED events are new lines; nothing is rewritten.
The real X "Block" click is done by the agent's browser adapter, outside this package (same as removal).

CLI:
    python -m src.graveyard_returns detect --user K --snapshot S
    python -m src.graveyard_returns block  --user K --snapshot S --adapter package.module:Class
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
from typing import Any, Callable

try:
    from . import storage as st
    from . import preflight
    from . import removal_runner as rr
    from .protections import execution_protection_reason
except ImportError:  # pragma: no cover
    import storage as st
    import preflight
    import removal_runner as rr
    from protections import execution_protection_reason

FLAG = "GRAVEYARD_RETURN"
AUTHORIZATION = "RETURNEE_BLOCK_OWNER_YES"
AUTO_AUTHORIZATION = "RETURNEE_AUTO_BLOCK_OWNER_SETTING"
DEFAULT_ACTION = "review"           # user-authorized block
ACTIONS = ("review", "auto_block")
CSV_HEADER = ["n", "handle", "x_user_id", "name", "removed_at", "removed_snapshot", "match", "action"]


def _id(x) -> str:
    s = str(x or "").strip().lstrip("'")
    return s if s.isdigit() else ""


def _h(x) -> str:
    return str(x or "").strip().lstrip("@").lower()


def graveyard_path(user_key: str, root=None):
    return st.user_data_root(user_key, root) / "graveyard.jsonl"


def load_graveyard(user_key: str, root=None) -> list[dict]:
    p = graveyard_path(user_key, root)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def append_graveyard(user_key: str, root, rec: dict) -> None:
    """Append-only: never rewrites earlier lines."""
    with open(graveyard_path(user_key, root), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    st._mirror(lambda r: r.record_graveyard_event, user_key, rec, root=root)  # v3.5.0 write-through


def removed_entries(user_key: str, root=None) -> list[dict]:
    """Graveyard records of actual removals (event REMOVED, or legacy lines without an event)."""
    return [r for r in load_graveyard(user_key, root) if r.get("event", "REMOVED") == "REMOVED"]


def match_graveyard(acct: dict, graveyard: list[dict]) -> tuple[dict, str] | None:
    """ID first; handle fallback only when the follower or the graveyard record has no ID."""
    uid, h = _id(acct.get("user_id") or acct.get("x_user_id")), _h(acct.get("handle") or acct.get("username"))
    for g in graveyard:
        gid = _id(g.get("x_user_id") or g.get("user_id"))
        if uid and gid:
            if uid == gid:
                return g, "user_id"
            continue
        if h and h == _h(g.get("handle")):
            return g, "handle (ID missing)"
    return None


def returns_path(user_key: str, snapshot_id: str, root=None):
    return st.snapshot_dir(snapshot_id, root, user_key=user_key) / "graveyard_returns.json"


def load_returns(user_key: str, snapshot_id: str, root=None) -> list[dict]:
    return st.load_json(returns_path(user_key, snapshot_id, root), []) or []


def detect_returns(user_key: str, snapshot_id: str, root=None) -> list[dict]:
    """Flag returnees in candidates.json, write graveyard_returns.json + a CSV report, log RETURN_DETECTED once."""
    st.validate_user_key(user_key)
    d = st.snapshot_dir(snapshot_id, root, user_key=user_key)
    cpath = d / "candidates.json"
    cands = json.loads(cpath.read_text(encoding="utf-8"))
    gy = removed_entries(user_key, root)
    seen = {(r.get("snapshot_id"), r.get("account_key")) for r in load_graveyard(user_key, root)
            if r.get("event") == "RETURN_DETECTED"}
    cfg = st.load_user_config_dict(root, user_key=user_key)
    action = cfg.get("graveyard_return_action", DEFAULT_ACTION)
    returns, now = [], dt.datetime.now().astimezone().isoformat()
    meta = st.load_json(d / "meta.json", {}) or {}
    returned_at = meta.get("scan_completed") or meta.get("created_at")  # when this scan saw them follow again
    for c in cands:
        m = match_graveyard(c, gy)
        if not m:
            continue
        g, how = m
        c["flag"] = FLAG
        c["graveyard_return"] = True
        r = {"n": len(returns) + 1, "handle": _h(c.get("handle") or c.get("username")),
             "x_user_id": _id(c.get("user_id")) or None, "name": c.get("name"),
             "post_count": c.get("post_count"), "followers": c.get("followers"), "following": c.get("following"),
             "removed_at": g.get("at"), "removed_snapshot": g.get("snapshot_id"), "match": how,
             "action": "BLOCK (needs your OK)" if action != "auto_block" else "BLOCK (auto-block setting)",
             "flag": FLAG, "returned_at": returned_at,
             "previous_handle": (_h(g.get("handle")) if _h(g.get("handle")) and
                                 _h(g.get("handle")) != _h(c.get("handle") or c.get("username")) else None)}
        returns.append(r)
        key = rr.st_key(r)
        if (snapshot_id, key) not in seen:
            append_graveyard(user_key, root, {"event": "RETURN_DETECTED", "at": now, "snapshot_id": snapshot_id,
                                              "account_key": key, "handle": r["handle"], "x_user_id": r["x_user_id"],
                                              "match": how})
    st.save_json(cpath, cands)
    st.save_json(returns_path(user_key, snapshot_id, root), returns)
    rep = st.reports_path(root, user_key=user_key)
    rep.mkdir(parents=True, exist_ok=True)
    # Append-only (2026-10-16 7:50 PM PT): never rewrite existing rows; skip returnees already listed.
    try:
        from .review_sheet_writer import write_rows_csv
    except ImportError:  # pragma: no cover
        from review_sheet_writer import write_rows_csv
    write_rows_csv([[r.get(k) if r.get(k) is not None else "Not available" for k in CSV_HEADER] for r in returns],
                   rep / f"graveyard_returns_{snapshot_id}.csv", header=CSV_HEADER,
                   dedupe_key=lambda row: (row[2] if str(row[2]).isdigit() else str(row[1]).lower()))
    st.apply_user_permissions(user_key, root)
    return returns


RETURNS_CARD_SIZE = 5   # chat card widgets allow at most 6 options: 5 returnees + the select-all option


def returns_card(returns: list[dict], page: int = 1) -> dict:
    """Separate card for returnees (never mixed into removal cards). Answer = which ones to BLOCK.
    Paged, RETURNS_CARD_SIZE per card, so a card never exceeds 6 options (select-all counts as one); pass page=2, ...
    (result has page / of). Option labels keep each returnee's own R-number across pages."""
    pages = max(1, -(-len(returns) // RETURNS_CARD_SIZE))
    page = min(max(int(page), 1), pages)
    shown = returns[(page - 1) * RETURNS_CARD_SIZE:page * RETURNS_CARD_SIZE]
    return {"title": "Graveyard returns: these were removed before and followed you again",
            "action": "BLOCK", "page": page, "of": pages, "options": [f"R{r['n']}. @{r['handle']}" for r in shown],
            "select_all_label": "Block all returnees on this card",
            "review_note": "Unmarked returnees are left alone for now.", "allow_free_text": False}


def record_block_authorization(user_key: str, snapshot_id: str, accounts: list[dict], *, root=None,
                               raw_answer: str = "") -> dict:
    """Record the owner's explicit yes to BLOCK specific returnees. Non-returnees are refused."""
    rets = {rr.st_key(r): r for r in load_returns(user_key, snapshot_id, root)}
    chosen = []
    for a in accounts:
        k = rr.st_key(a)
        if k not in rets:
            raise ValueError(f"@{_h(a.get('handle'))} is not a Graveyard return in snapshot {snapshot_id}")
        chosen.append({"handle": rets[k]["handle"], "x_user_id": rets[k]["x_user_id"]})
    entry = {"status": "BLOCK_AUTHORIZATION", "at": dt.datetime.now().astimezone().isoformat(),
             "snapshot_id": snapshot_id, "authorization": AUTHORIZATION, "selected": chosen,
             "raw_answer": raw_answer, "action": "NONE"}
    rr._append(user_key, root, entry)
    return entry


def authorized_returnees(hist: list[dict], snapshot_id: str, returns: list[dict], cfg: dict) -> list[dict]:
    start = 0
    for i, e in enumerate(hist):
        if e.get("status") in rr.STOP_STATUSES and e.get("locks_day"):
            start = i + 1
    ret = {rr.st_key(r): r for r in returns}
    out, seen = [], set()
    if cfg.get("graveyard_return_action") == "auto_block":
        for k, r in ret.items():
            seen.add(k); out.append(dict(r, authorized_by=AUTO_AUTHORIZATION))
    for e in hist[start:]:
        if e.get("status") != "BLOCK_AUTHORIZATION" or e.get("snapshot_id") != snapshot_id \
                or e.get("authorization") != AUTHORIZATION:
            continue
        for a in e.get("selected") or []:
            k = rr.st_key(a)
            if k in ret and k not in seen:   # only real returnees, never anything else
                seen.add(k); out.append(dict(ret[k], authorized_by=e.get("entry_id")))
    return out


def run_blocks(user_key: str, snapshot_id: str, adapter, *, root=None,
               now_fn: Callable[[], dt.datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
               out=sys.stdout) -> dict[str, Any]:
    st.validate_user_key(user_key)
    with rr.run_lock(user_key, root, "block", snapshot_id):  # one run at a time (shared with removals)
        return _run_blocks(user_key, snapshot_id, adapter, root=root, now_fn=now_fn, sleep=sleep, out=out)


def _run_blocks(user_key: str, snapshot_id: str, adapter, *, root=None,
                now_fn: Callable[[], dt.datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
                out=sys.stdout) -> dict[str, Any]:
    cfg = st.load_user_config_dict(root, user_key=user_key)
    preflight.require(user_key, root, cfg)  # inside Grok Bot: Google Sheets + Drive must be confirmed
    tz = rr._tz(cfg)
    now_fn = now_fn or (lambda: dt.datetime.now(tz))
    now = now_fn().astimezone(tz)
    today = now.date().isoformat()
    hist = rr._history(user_key, root)
    rr._snapshot_age_ok(user_key, snapshot_id, root, now,
                        float(cfg.get("max_snapshot_age_days", rr.DEFAULT_MAX_SNAPSHOT_AGE_DAYS)))
    lock = rr.day_locked(hist, today, tz)
    if lock:
        raise rr.RefusedError(f"actions are paused for today after an X {lock.get('reason')}; "
                              "resume tomorrow with a new authorized run")
    returns = load_returns(user_key, snapshot_id, root)
    if not returns:
        raise rr.RefusedError("no Graveyard returns detected for this snapshot; nothing to block")
    accounts = authorized_returnees(hist, snapshot_id, returns, cfg)
    if not accounts:
        raise rr.RefusedError("no recorded authorization to block these Graveyard returns; nothing to block")
    done = {e.get("account_key") for e in hist if e.get("status") == "BLOCKED"}
    accounts = [a for a in accounts if rr.st_key(a) not in done]
    cap = rr.daily_cap(cfg)
    used = rr.removals_today(hist, today, tz)
    if used >= cap:
        raise rr.RefusedError(f"daily cap reached ({used}/{cap} actions today)")
    always_keep = st.load_always_keep(root, user_key=user_key)
    protected = cfg.get("protected_handles") or []
    blocked, skipped, stopped, first = [], [], None, True
    for a in accounts:
        if used >= cap:
            stopped = {"reason": "daily cap", "message": f"{cap} actions today"}
            break
        key = rr.st_key(a)
        fresh = adapter.lookup(dict(a)) or {}
        why = execution_protection_reason(fresh, always_keep=always_keep, protected_handles=protected, recorded=a,
                                          keepers=rr.keepers_from_history(hist), require_interaction=True)
        if why:
            skipped.append({"handle": a.get("handle"), "reason": why})
            rr._append(user_key, root, {"status": "BLOCK_SKIPPED_PROTECTED", "at": now_fn().isoformat(),
                                        "snapshot_id": snapshot_id, "account_key": key, "handle": a.get("handle"),
                                        "reason": why, "action": "NONE"})
            continue
        if not first:
            sleep(rr.PACING_SECONDS)
        first = False
        res = adapter.block(dict(a))
        at = now_fn().isoformat()
        status, msg = getattr(res, "status", rr.ERROR), getattr(res, "message", "")
        if status == rr.OK:
            used += 1
            blocked.append(a.get("handle"))
            rr._append(user_key, root, {"status": "BLOCKED", "at": at, "snapshot_id": snapshot_id, "account_key": key,
                                        "handle": a.get("handle"), "x_user_id": a.get("x_user_id"),
                                        "authorized_by": a.get("authorized_by"), "reason": "Graveyard return",
                                        "action": "X native 'Block' (via adapter)", "message": msg})
            append_graveyard(user_key, root, {"event": "BLOCKED", "at": at, "snapshot_id": snapshot_id,
                                              "account_key": key, "handle": a.get("handle"),
                                              "x_user_id": a.get("x_user_id"), "method": "block (Graveyard return)",
                                              "authorized_by": a.get("authorized_by")})
            continue
        locks = status in (rr.WARNING, rr.RATE_LIMITED)
        stopped = {"reason": "warning" if status == rr.WARNING else "rate limit" if locks else "error", "message": msg}
        rr._append(user_key, root, {"status": "BLOCK_STOPPED", "at": at, "snapshot_id": snapshot_id,
                                    "account_key": key, "handle": a.get("handle"), "reason": stopped["reason"],
                                    "message": msg, "locks_day": locks, "action": "NONE (stopped)"})
        break
    st.apply_user_permissions(user_key, root)
    lines = [f"Graveyard returns blocked: {len(blocked)}", f"Skipped (protected): {len(skipped)}",
             f"Actions today (removals + blocks): {used}/{cap}",
             f"Stopped: {stopped['reason'] + (' - ' + stopped['message'] if stopped['message'] else '') if stopped else 'no'}"]
    if stopped and stopped["reason"] in ("warning", "rate limit"):
        lines.append("No more removals or blocks today. Resume tomorrow with a new authorized run.")
    print("\n".join(lines), file=out)
    return {"blocked": len(blocked), "blocked_handles": blocked, "skipped_protected": len(skipped),
            "skipped": skipped, "actions_today": used, "daily_cap": cap,
            "stopped_reason": stopped["reason"] if stopped else None}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Detect Graveyard returns and block them only when authorized")
    p.add_argument("command", choices=["detect", "block"])
    p.add_argument("--user", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--adapter", help="package.module:ClassName implementing lookup()/block() (block only)")
    p.add_argument("--root", default=str(st.PROJECT_ROOT))
    a = p.parse_args(argv)
    try:
        if a.command == "detect":
            rets = detect_returns(a.user, a.snapshot, a.root)
            print(f"Graveyard returns: {len(rets)}")
            for r in rets:
                print(f"  R{r['n']}. @{r['handle']} (matched by {r['match']})")
            return 0
        if not a.adapter:
            raise ValueError("--adapter is required for block")
        s = run_blocks(a.user, a.snapshot, rr._load_adapter(a.adapter), root=a.root)
    except (rr.RefusedError, ValueError, ImportError, AttributeError, FileNotFoundError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 1 if s["stopped_reason"] in ("warning", "rate limit", "error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
