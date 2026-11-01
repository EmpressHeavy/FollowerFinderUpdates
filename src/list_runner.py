"""List runs (v3). Soft block = Block then Unblock the list's owner on X, which takes the owner off the list.
Permanent block = repeat offenders only (owners soft-blocked before who put the owner on a list again).

Only after the owner's selection ("Remove me" on a list card: LIST_SELECTION; Done on the repeat-offender card: LIST_OWNER_BLOCK_SELECTION) or, for repeat offenders, the owner's standing
graveyard_return_action "auto_block". Re-validated on X right before each action. One at a time, 10 s apart,
shared daily cap (a soft block counts 2: block + unblock), stop for the day at any X warning or rate limit,
one run at a time (shared run.lock with removals), reconcile after an interrupted run.

Adapter (the browser bridge on the owner's signed-in X website; none ships for real use):
  list_state(list) -> {"on_list": bool|None}       (the owner's X account is still a member?)
  owner_state(owner) -> {"user_id", "handle", "blocked": bool|None}
  block(owner) / unblock(owner) -> RemovalResult   (owner profile > three dots > Block/Unblock > confirm)
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from typing import Any, Callable

try:
    from . import storage as st
    from . import removal_runner as rr
    from . import preflight
    from . import list_cards as lc
    from . import lists_scan as ls
    from . import sheet_layout
except ImportError:  # pragma: no cover
    import storage as st
    import removal_runner as rr
    import preflight
    import list_cards as lc
    import lists_scan as ls
    import sheet_layout

SOFT_WEIGHT = 2
SOFT_BLOCK_WAIT_SECONDS = 10.0   # between Block and Unblock
FINAL = ("LIST_SOFT_BLOCKED", "LIST_SKIPPED_NOT_ON_LIST", "LIST_SKIPPED_OWNER_ALREADY_BLOCKED",
         "LIST_SKIPPED_PROTECTED_OWNER")


def _okey(r: dict) -> str:
    oid = str(r.get("owner_id") or "")
    return oid if oid.isdigit() else str(r.get("owner_handle") or "").lstrip("@").lower()


def _selected(hist, snapshot_id, status, auth):
    out, seen, start = [], set(), 0
    for i, e in enumerate(hist):
        if e.get("status") in rr.STOP_STATUSES and e.get("locks_day"):
            start = i + 1
    for e in hist[start:]:
        if e.get("status") == status and e.get("snapshot_id") == snapshot_id and e.get("done") is True \
                and e.get("authorization") == auth:
            for a in e.get("selected") or []:
                k = a.get("list_id") if status == "LIST_SELECTION" else _okey(a)
                if k not in seen:
                    seen.add(k); out.append(dict(a, authorized_by=e.get("entry_id")))
    return out


def _protected_owner(r, cfg, keep) -> str | None:
    h = str(r.get("owner_handle") or "").lstrip("@").lower()
    ids = {str(k.get("x_user_id") or "") for k in keep if isinstance(k, dict)}
    hs = {str(k.get("handle") if isinstance(k, dict) else k).lstrip("@").lower() for k in keep}
    hs |= {p.lstrip("@").lower() for p in cfg.get("protected_handles") or []}
    if h in hs or str(r.get("owner_id")) in ids:
        return "Always Keep / protected"
    return None


def _start(user_key, root, now_fn):
    cfg = st.load_user_config_dict(root, user_key=user_key)
    preflight.require(user_key, root, cfg)
    tz = rr._tz(cfg)
    now_fn = now_fn or (lambda: dt.datetime.now(tz))
    now = now_fn().astimezone(tz)
    hist = rr._history(user_key, root)
    lock = rr.day_locked(hist, now.date().isoformat(), tz)
    if lock:
        raise rr.RefusedError(f"actions are paused for today after an X {lock.get('reason')}; resume tomorrow")
    return cfg, tz, now_fn, now, hist


def _snapshot_ok(user_key, sid, root, now, cfg):
    snap = ls.load(user_key, sid, root)
    if not snap:
        raise rr.RefusedError("unknown lists snapshot")
    age = now - dt.datetime.fromisoformat(snap["scanned_at"]).astimezone(now.tzinfo)
    if age > dt.timedelta(days=float(cfg.get("max_snapshot_age_days", rr.DEFAULT_MAX_SNAPSHOT_AGE_DAYS))):
        raise rr.RefusedError("lists snapshot is too old; rescan lists first")
    return snap


def _stop(user_key, root, at, sid, r, status, msg, out_list, hook=None):
    locks = status in (rr.WARNING, rr.RATE_LIMITED)
    reason = "warning" if status == rr.WARNING else "rate limit" if locks else "error"
    _emit(user_key, root, {"status": "LIST_STOPPED", "at": at, "snapshot_id": sid, "list_id": r.get("list_id"),
                           "list_name": r.get("name"), "owner_handle": r.get("owner_handle"),
                           "owner_id": r.get("owner_id"), "reason": reason, "message": msg,
                           "result": f"NOT done: stopped on X {reason}" + (f" ({msg})" if msg else "")
                           + ("; no more actions today" if locks else ""),
                           "locks_day": locks, "action": "NONE (stopped)"}, hook)
    return {"reason": reason, "message": msg}


class _HookFailed(Exception):
    """The per-list log hook (Google Sheet / Book write) failed AFTER the entry was saved locally."""


def _emit(user_key, root, entry: dict, hook) -> None:
    """Save one history entry, then (for a List Graveyard status) call the hook RIGHT AWAY with the saved entry
    (it has its entry_id). The hook writes the Graveyard row (and Book line) before the run moves on."""
    rr._append(user_key, root, entry)
    if hook is None or entry.get("status") not in sheet_layout.LIST_STATUSES:
        return
    logged = rr._history(user_key, root)[-1]
    try:
        hook(dict(logged))
    except Exception as exc:  # noqa: BLE001 - any writer failure stops the run before the next list
        raise _HookFailed(f"{type(exc).__name__}: {exc}") from exc


def run_list_removals(user_key: str, snapshot_id: str, adapter, *, root=None, now_fn=None,
                      sleep: Callable[[float], None] = time.sleep, out=sys.stdout, on_logged=None,
                      only_list_id: str | None = None) -> dict[str, Any]:
    """Leave the authorized lists (soft block of each owner), one at a time.

    on_logged(entry): called immediately after each list's outcome is saved (verified, skipped, unverified, failed or
    stopped), BEFORE the next list starts. list_flow.graveyard_hook() uses it to write that list's row to the List
    Graveyard tab and a line to the Book change log. If it raises, the run stops (the entry stays saved locally and
    list_flow.catch_up() writes whatever is missing). only_list_id: act on just that one authorized list."""
    st.validate_user_key(user_key)
    with rr.run_lock(user_key, root, "list", snapshot_id):
        cfg, tz, now_fn, now, hist = _start(user_key, root, now_fn)
        _snapshot_ok(user_key, snapshot_id, root, now, cfg)
        if not ls.is_frozen_intact(user_key, snapshot_id, root):
            raise rr.RefusedError("lists snapshot is missing or changed since review; rescan lists first")
        sha = (st.load_json(st.user_data_root(user_key, root) / "lists" / snapshot_id / "frozen.json", {}) or {})["sha256"]
        chosen = [r for r in _selected(hist, snapshot_id, "LIST_SELECTION", lc.AUTH)]
        hist_sha = {e.get("entry_id"): e.get("snapshot_sha256") for e in hist if e.get("status") == "LIST_SELECTION"}
        chosen = [r for r in chosen if hist_sha.get(r.get("authorized_by")) == sha]
        snap = ls.load(user_key, snapshot_id, root)
        if snap.get("scope") == ls.SCOPE_NOT_FOLLOWED:
            prot = {r["list_id"] for r in snap.get("lists", []) if r.get("protected")}
            if any(r.get("list_id") in prot for r in chosen):
                raise rr.RefusedError("a selected list is a Protected List, outside this run's scope; nothing done")
        if not chosen:
            raise rr.RefusedError("no lists marked for removal (Remove me) on this frozen snapshot; nothing to do")
        done = {e.get("list_id") for e in hist if e.get("status") in FINAL and e.get("snapshot_id") == snapshot_id}
        chosen = [r for r in chosen if r["list_id"] not in done]
        if only_list_id is not None:
            chosen = [r for r in chosen if r["list_id"] == str(only_list_id)]
        keep = st.load_always_keep(root, user_key=user_key)
        cap, today = rr.daily_cap(cfg), now.date().isoformat()
        used = rr.removals_today(hist, today, tz)
        left, skipped, stopped, first = [], [], None, True
        emit = lambda e: _emit(user_key, root, e, on_logged)  # noqa: E731
        try:
            for r in chosen:
                if used + SOFT_WEIGHT > cap:
                    stopped = {"reason": "daily cap", "message": f"{used}/{cap} actions today"}; break
                at = now_fn().isoformat()
                why = _protected_owner(r, cfg, keep)
                if why:
                    emit({"status": "LIST_SKIPPED_PROTECTED_OWNER", "at": at, "snapshot_id": snapshot_id,
                          "list_id": r["list_id"], "list_name": r["name"], "owner_handle": r["owner_handle"],
                          "owner_id": r.get("owner_id"), "reason": "owner is Always Keep / protected; nothing done",
                          "result": "skipped: owner is Always Keep / protected", "action": "NONE"})
                    skipped.append({"list": r["name"], "reason": "owner is Always Keep / protected"}); continue
                on = (adapter.list_state(dict(r)) or {}).get("on_list")
                own = adapter.owner_state(dict(r)) or {}
                if on is not True:
                    reason = "already off this list (checked on X)" if on is False else "could not check the list on X"
                    emit({"status": "LIST_SKIPPED_NOT_ON_LIST" if on is False else "LIST_STOPPED",
                          "at": at, "snapshot_id": snapshot_id, "list_id": r["list_id"], "list_name": r["name"],
                          "owner_handle": r["owner_handle"], "owner_id": r.get("owner_id"), "reason": reason,
                          "result": reason, "locks_day": False, "action": "NONE"})
                    if on is None:
                        stopped = {"reason": "error", "message": "could not check the list on X"}; break
                    skipped.append({"list": r["name"], "reason": "already off this list"}); continue
                if own.get("blocked") is not False:
                    emit({"status": "LIST_SKIPPED_OWNER_ALREADY_BLOCKED", "at": at, "snapshot_id": snapshot_id,
                          "list_id": r["list_id"], "list_name": r["name"], "owner_handle": r["owner_handle"],
                          "owner_id": r.get("owner_id"),
                          "reason": "owner already blocked or block state unknown; not unblocking",
                          "result": "skipped: owner already blocked (or state unknown); not unblocking", "action": "NONE"})
                    skipped.append({"list": r["name"], "reason": "owner block state not clear"}); continue
                if not first:
                    sleep(rr.PACING_SECONDS)
                first = False
                res = adapter.block(dict(r))
                if getattr(res, "status", rr.ERROR) != rr.OK:
                    stopped = _stop(user_key, root, now_fn().isoformat(), snapshot_id, r, res.status, res.message, left, on_logged)
                    break
                sleep(SOFT_BLOCK_WAIT_SECONDS)   # ~10 s between Block and Unblock
                res2 = adapter.unblock(dict(r))
                if getattr(res2, "status", rr.ERROR) != rr.OK:
                    emit({"status": "LIST_UNBLOCK_FAILED", "at": now_fn().isoformat(), "snapshot_id": snapshot_id,
                          "list_id": r["list_id"], "list_name": r["name"], "owner_handle": r["owner_handle"],
                          "owner_id": r.get("owner_id"), "cap_weight": 1, "message": res2.message,
                          "result": f"NOT verified: owner left BLOCKED, unblock failed ({res2.message or res2.status})",
                          "action": "Blocked; unblock FAILED"})
                    stopped = {"reason": "unblock failed",
                               "message": f"{r['owner_handle']} is still blocked; unblock needs your OK"}
                    if res2.status in (rr.WARNING, rr.RATE_LIMITED):
                        _stop(user_key, root, now_fn().isoformat(), snapshot_id, r, res2.status, res2.message, left, on_logged)
                    break
                on2 = (adapter.list_state(dict(r)) or {}).get("on_list")
                b2 = (adapter.owner_state(dict(r)) or {}).get("blocked")
                if on2 is not False or b2 is not False:
                    emit({"status": "LIST_SOFTBLOCK_NOT_CONFIRMED", "at": now_fn().isoformat(),
                          "snapshot_id": snapshot_id, "list_id": r["list_id"], "list_name": r["name"],
                          "owner_handle": r["owner_handle"], "owner_id": r.get("owner_id"),
                          "on_list": on2, "owner_blocked": b2, "cap_weight": SOFT_WEIGHT,
                          "result": f"NOT verified: still on list={on2}; owner blocked={b2} (check on X)",
                          "action": "Block + Unblock done; result NOT confirmed (check on X)"})
                    used += SOFT_WEIGHT
                    stopped = {"reason": "not confirmed", "message": "could not confirm you're off the list"}; break
                used += SOFT_WEIGHT
                left.append(r["name"])
                emit({"status": "LIST_SOFT_BLOCKED", "at": now_fn().isoformat(),
                      "snapshot_id": snapshot_id, "list_id": r["list_id"], "list_name": r["name"],
                      "owner_handle": r["owner_handle"], "owner_id": r.get("owner_id"),
                      "cap_weight": SOFT_WEIGHT, "verified": True, "authorized_by": r.get("authorized_by"),
                      "action": "Soft block (Block, then Unblock)",
                      "result": "verified: no longer on the list; owner not blocked"})
        except _HookFailed as exc:
            stopped = {"reason": "log write failed",
                       "message": f"the List Graveyard / Book write failed ({exc}); saved locally, run stopped; "
                                  "list_flow.catch_up() writes what is missing"}
        st.apply_user_permissions(user_key, root)
        print(f"Lists left: {len(left)}\nSkipped: {len(skipped)}\nActions today: {used}/{cap}\n"
              f"Stopped: {(stopped['reason'] + ' - ' + stopped['message']) if stopped else 'no'}", file=out)
        return {"left": left, "skipped": skipped, "actions_today": used, "daily_cap": cap, "stopped": stopped}


def detect_returns(user_key: str, list_snapshot_id: str, root=None) -> list[dict]:
    """Owners soft-blocked before who own a list the user is on again ('returned from the graveyard')."""
    hist = rr._history(user_key, root)
    soft = {_okey(e) for e in hist if e.get("status") == "LIST_SOFT_BLOCKED"}
    blocked = {_okey(e) for e in hist if e.get("status") == "LIST_OWNER_BLOCKED"}
    logged = {(e.get("owner_key"), e.get("snapshot_id")) for e in hist if e.get("status") == "LIST_OWNER_RETURN_DETECTED"}
    out = []
    for r in ls.load(user_key, list_snapshot_id, root).get("lists", []):
        k = _okey(r)
        if k in soft and k not in blocked and k not in {_okey(x) for x in out}:
            out.append(r)
            if (k, list_snapshot_id) not in logged:
                rr._append(user_key, root, {"status": "LIST_OWNER_RETURN_DETECTED",
                                            "at": dt.datetime.now().astimezone().isoformat(),
                                            "snapshot_id": list_snapshot_id, "owner_key": k,
                                            "owner_handle": r["owner_handle"], "list_id": r["list_id"],
                                            "list_name": r["name"], "action": "NONE (detected)"})
    st.save_json(st.user_data_root(user_key, root) / "lists" / list_snapshot_id / "list_returns.json", out)
    return out


def run_owner_blocks(user_key: str, list_snapshot_id: str, adapter, *, root=None, now_fn=None,
                     sleep: Callable[[float], None] = time.sleep, out=sys.stdout) -> dict[str, Any]:
    """Permanent block of repeat offenders: Done on the keycard, or the standing 'auto_block' setting."""
    st.validate_user_key(user_key)
    with rr.run_lock(user_key, root, "list owner block", list_snapshot_id):
        cfg, tz, now_fn, now, hist = _start(user_key, root, now_fn)
        _snapshot_ok(user_key, list_snapshot_id, root, now, cfg)
        chosen = _selected(hist, list_snapshot_id, "LIST_OWNER_BLOCK_SELECTION", lc.OWNER_BLOCK_AUTH)
        auth = "card (Done)"
        if not chosen and cfg.get("graveyard_return_action") == "auto_block":
            chosen = st.load_json(st.user_data_root(user_key, root) / "lists" / list_snapshot_id
                                  / "list_returns.json", []) or []
            auth = "standing setting: auto_block"
        if not chosen:
            raise rr.RefusedError("no repeat offenders authorized (Done on the card, or auto-block setting)")
        done = {_okey(e) for e in hist if e.get("status") == "LIST_OWNER_BLOCKED"}
        keep = st.load_always_keep(root, user_key=user_key)
        cap, today = rr.daily_cap(cfg), now.date().isoformat()
        used = rr.removals_today(hist, today, tz)
        blocked, skipped, stopped, first = [], [], None, True
        for r in [c for c in chosen if _okey(c) not in done]:
            if used + 1 > cap:
                stopped = {"reason": "daily cap", "message": f"{used}/{cap} actions today"}; break
            why = _protected_owner(r, cfg, keep)
            own = adapter.owner_state(dict(r)) or {}
            if why or own.get("blocked") is not False:
                skipped.append({"owner": r["owner_handle"], "reason": why or "already blocked / unknown"})
                rr._append(user_key, root, {"status": "LIST_OWNER_BLOCK_SKIPPED", "at": now_fn().isoformat(),
                                            "snapshot_id": list_snapshot_id, "owner_handle": r["owner_handle"],
                                            "reason": why or "already blocked or state unknown", "action": "NONE"})
                continue
            if not first:
                sleep(rr.PACING_SECONDS)
            first = False
            res = adapter.block(dict(r))
            if getattr(res, "status", rr.ERROR) != rr.OK:
                stopped = _stop(user_key, root, now_fn().isoformat(), list_snapshot_id, r, res.status, res.message, [])
                break
            b2 = (adapter.owner_state(dict(r)) or {}).get("blocked")
            on2 = (adapter.list_state(dict(r)) or {}).get("on_list")
            used += 1
            rr._append(user_key, root, {"status": "LIST_OWNER_BLOCKED" if b2 is True else "LIST_OWNER_BLOCK_NOT_CONFIRMED",
                                        "at": now_fn().isoformat(), "snapshot_id": list_snapshot_id,
                                        "list_id": r.get("list_id"), "list_name": r.get("name"),
                                        "owner_handle": r["owner_handle"], "owner_id": r.get("owner_id"),
                                        "authorization": auth, "verified": b2 is True and on2 is False,
                                        "action": "Permanent block (repeat offender)",
                                        "result": f"blocked={b2}; still on list={on2}"})
            if b2 is not True:
                stopped = {"reason": "not confirmed", "message": "could not confirm the block on X"}; break
            blocked.append(r["owner_handle"])
        print(f"Blocked: {len(blocked)}\nSkipped: {len(skipped)}\nActions today: {used}/{cap}", file=out)
        return {"blocked": blocked, "skipped": skipped, "stopped": stopped, "actions_today": used}


def reconcile_interrupted_list_run(user_key: str, snapshot_id: str, adapter, *, root=None, out=sys.stdout) -> dict:
    """Check each selected, not-yet-final list on X. Off the list + owner not blocked -> log LIST_SOFT_BLOCKED
    (reconciled). Owner left blocked mid-soft-block -> reported (unblock needs the owner's OK). Clears the lock
    only when every list's state is known and none is left half-done. Never acts on X itself."""
    hist = rr._history(user_key, root)
    final = {e.get("list_id") for e in hist if e.get("status") in FINAL}
    fixed, dangling, unknown = [], [], []
    for r in _selected(hist, snapshot_id, "LIST_SELECTION", lc.AUTH):
        if r["list_id"] in final:
            continue
        on = (adapter.list_state(dict(r)) or {}).get("on_list")
        b = (adapter.owner_state(dict(r)) or {}).get("blocked")
        if on is False and b is False:
            rr._append(user_key, root, {"status": "LIST_SOFT_BLOCKED", "at": dt.datetime.now().astimezone().isoformat(),
                                        "snapshot_id": snapshot_id, "list_id": r["list_id"], "list_name": r["name"],
                                        "owner_handle": r["owner_handle"], "owner_id": r.get("owner_id"),
                                        "cap_weight": SOFT_WEIGHT, "verified": True, "reconciled": True,
                                        "action": "Soft block (found done after an interrupted run)",
                                        "result": "verified: no longer on the list; owner not blocked"})
            fixed.append(r["name"])
        elif b is True:
            dangling.append(r["owner_handle"])
        elif on is None or b is None:
            unknown.append(r["name"])
    lock = st.user_data_root(user_key, root) / rr.LOCK_NAME
    if not unknown and not dangling and lock.exists():
        lock.unlink()
    print(f"Reconciled: {len(fixed)}; still blocked (needs your OK to unblock): {dangling or 'none'}; "
          f"unknown: {unknown or 'none'}", file=out)
    return {"reconciled": fixed, "owner_still_blocked": dangling, "unknown": unknown,
            "lock_cleared": not lock.exists()}
