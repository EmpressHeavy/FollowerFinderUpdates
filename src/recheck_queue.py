"""Verified Follower Review: monthly recheck queue (v3.5.17).

When the 90-day deep dive (verified_deep_dive.deep_dive) finds a verified follower INACTIVE -- no post in the window /
last non-pinned post more than DEEP_DIVE_CONFIG["recency"]["negative_max"] (90) days ago, or the only activity was an
old pinned post (dropped via pinned_tweet_id) -- and the owner does not pick it on the card, the account is not
discarded: it goes into the per-owner `recheck_queue` table in terminator.db with recheck_due = now +
RECHECK_INTERVAL_DAYS. Each daily review run calls process_due() first: due rows get the SAME deep dive again,
scored by the SAME scorer (no thresholds change here). Active now -> returned for the daily card (marked
rechecked=True) and resolved. Still inactive -> recheck_due pushed out another interval, recheck_count += 1.

Removed (status='resolved') instead of rechecked: followed back, owner unfollowed/blocked us (on_unfollow), or a hard
Skip (impersonation/scam). Read-only on X: nothing here posts, follows or DMs.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable, Iterable

try:
    from . import records as rec
    from . import verified_deep_dive as dd
except ImportError:  # top-level import
    import records as rec
    import verified_deep_dive as dd
try:
    from . import drive_by as db
    from . import blocked_by as bb
except ImportError:  # top-level import
    import drive_by as db
    import blocked_by as bb

RECHECK_CONFIG: dict[str, Any] = {
    "RECHECK_INTERVAL_DAYS": 30,     # skipped_at/last check -> next recheck_due
    "RECHECK_MAX_PER_RUN": None,     # v3.5.31: no per-run cap (None = all due rows)
    "RECHECK_MAX_COUNT": None,       # optional: resolve after this many still-inactive rechecks (None = keep forever)
}


def _utc(now: dt.datetime | None) -> dt.datetime:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(user_id: Any, handle: str | None) -> str:
    return str(user_id) if user_id not in (None, "") else "@" + (handle or "").lstrip("@").lower()


def _cfg(cfg: dict | None) -> dict:
    return {**RECHECK_CONFIG, **(cfg or {})}


def inactive_reason(result: dict, cfg: dict | None = None) -> str | None:
    """deep_dive() result -> 'inactive_90d' | 'pinned_only' | 'no_activity' | None (active). Hard skips -> None."""
    if result.get("hard_skip"):
        return None
    a = result.get("analysis") or {}
    c = cfg or dd.DEEP_DIVE_CONFIG
    if not a.get("n_items"):
        if "API error" in ((a.get("coverage") or {}).get("stop_reasons") or []):
            return None  # unknown, not inactive: never guessed
        return "pinned_only" if a.get("pinned_excluded") else "no_activity"
    d = a.get("days_since_last_post")
    if d is not None and d > c["recency"]["negative_max"]:
        return "inactive_90d"
    return None


def enqueue(user_key: str, user: dict, reason: str, *, now=None, root=None, cfg=None) -> dict:
    """Upsert (pending). Re-enqueueing a resolved row reopens it; recheck_count is kept.
    Drive-by follows (followed + unfollowed within 24 h, drive_by.py) are never queued: returns None."""
    if db.is_drive_by(user_key, user.get("id") or user.get("user_id"), root=root) or \
            bb.is_blocked_by(user_key, user.get("id") or user.get("user_id"), root=root):
        return None
    c, t = _cfg(cfg), _utc(now)
    due = _iso(t + dt.timedelta(days=c["RECHECK_INTERVAL_DAYS"]))
    handle = "@" + (user.get("username") or user.get("handle") or "").lstrip("@")
    uid = user.get("id") or user.get("user_id")
    row = (user_key, _key(uid, handle), None if uid is None else str(uid), handle, _iso(t), due, reason,
           json.dumps({k: user[k] for k in user if not k.startswith("_")}, ensure_ascii=False, default=str))
    con = rec.connect(user_key, root)
    try:
        con.execute("""INSERT INTO recheck_queue(owner_key,account_key,user_id,handle,skipped_at,recheck_due,reason,user_json)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(owner_key,account_key) DO UPDATE SET handle=excluded.handle,
                         user_id=coalesce(excluded.user_id,user_id), skipped_at=excluded.skipped_at,
                         recheck_due=excluded.recheck_due, reason=excluded.reason, user_json=excluded.user_json,
                         status='pending', resolved_reason=NULL""", row)
        con.commit()
    finally:
        con.close()
    return get(user_key, uid, handle, root=root)


def get(user_key: str, user_id=None, handle=None, *, root=None) -> dict | None:
    con = rec.connect(user_key, root)
    try:
        con.row_factory = __import__("sqlite3").Row
        r = con.execute("SELECT * FROM recheck_queue WHERE owner_key=? AND account_key=?",
                        (user_key, _key(user_id, handle))).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def remove(user_key: str, user_id=None, handle=None, *, why: str, root=None) -> bool:
    """Resolve (kept for history, never rechecked again unless re-enqueued)."""
    con = rec.connect(user_key, root)
    try:
        n = con.execute("UPDATE recheck_queue SET status='resolved', resolved_reason=? "
                        "WHERE owner_key=? AND account_key=? AND status='pending'",
                        (why, user_key, _key(user_id, handle))).rowcount
        con.commit()
        return bool(n)
    finally:
        con.close()


def on_unfollow(user_key: str, user_id=None, handle=None, *, root=None) -> bool:
    return remove(user_key, user_id, handle, why="unfollowed_owner", root=root)


def due(user_key: str, *, now=None, limit: int | None = None, root=None) -> list[dict]:
    con = rec.connect(user_key, root)
    try:
        con.row_factory = __import__("sqlite3").Row
        sql = ("SELECT * FROM recheck_queue WHERE owner_key=? AND status='pending' AND recheck_due<=? "
               "ORDER BY recheck_due, account_key")
        args: list = [user_key, _iso(_utc(now))]
        if limit is not None:
            sql += " LIMIT ?"; args.append(int(limit))
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def record_card_outcome(user_key: str, results: Iterable[tuple[dict, dict]], picked_ids: Iterable[str],
                        followed_ids: Iterable[str] = (), *, now=None, root=None, cfg=None) -> dict:
    """After a card submit. results: (user, deep_dive_result) pairs shown on the card.
    Followed -> resolved. Hard Skip -> resolved. Inactive and not followed -> enqueued (a pick that was not
    followed yet is left alone until record_follow; an unfollowed pick is still enqueued)."""
    picked, followed = set(map(str, picked_ids)), set(map(str, followed_ids))
    out = {"enqueued": [], "removed": []}
    for user, r in results:
        uid, h = str(user["id"]), user.get("username")
        if uid in followed:
            remove(user_key, uid, h, why="followed", root=root) and out["removed"].append(uid); continue
        if r.get("hard_skip"):
            remove(user_key, uid, h, why="hard_skip", root=root) and out["removed"].append(uid); continue
        reason = inactive_reason(r)
        if reason and uid not in picked and not db.is_drive_by(user_key, uid, root=root) \
                and not bb.is_blocked_by(user_key, uid, root=root):
            enqueue(user_key, user, reason, now=now, root=root, cfg=cfg); out["enqueued"].append(uid)
    return out


def process_due(user_key: str, call_tool: Callable[[str, dict], dict], owner_id: str, *, now=None, root=None,
                cfg=None, dd_cfg: dict | None = None, refresh_user: Callable[[dict], dict | None] | None = None,
                calls_remaining: int | None = None, friends: Any = None, following_ids: Iterable[str] = (),
                allow_reply_heavy: bool = False) -> dict:
    """Recheck due rows with the unchanged deep dive + scorer. Returns
    {"surface": [deep_dive results with rechecked=True], "still_inactive": [...], "removed": [...], "deferred": [...]}.
    refresh_user(row_user) -> fresh user dict (e.g. get_users_by_usernames with pinned_tweet_id), None = gone.
    calls_remaining: X call budget left today; a recheck needs max_calls_per_candidate, else it waits (deferred)."""
    c, t = _cfg(cfg), _utc(now)
    dcfg = dd_cfg or dd.DEEP_DIVE_CONFIG
    per = dcfg["max_calls_per_candidate"]
    fol = set(map(str, following_ids))
    out = {"surface": [], "still_inactive": [], "removed": [], "deferred": [], "calls_used": 0}
    rows = due(user_key, now=t, root=root)
    for i, row in enumerate(rows):
        if (c["RECHECK_MAX_PER_RUN"] is not None and i >= c["RECHECK_MAX_PER_RUN"]) or (calls_remaining is not None and calls_remaining - out["calls_used"] < per):
            out["deferred"].append(row["account_key"]); continue
        if db.is_drive_by(user_key, row["user_id"], root=root):
            remove(user_key, row["user_id"], row["handle"], why="drive_by", root=root)
            out["removed"].append(row["account_key"]); continue
        if bb.is_blocked_by(user_key, row["user_id"], root=root):
            remove(user_key, row["user_id"], row["handle"], why="blocked_by", root=root)
            out["removed"].append(row["account_key"]); continue
        if row["user_id"] and row["user_id"] in fol:
            remove(user_key, row["user_id"], row["handle"], why="followed", root=root)
            out["removed"].append(row["account_key"]); continue
        user = json.loads(row["user_json"] or "{}") or {"id": row["user_id"], "username": row["handle"].lstrip("@")}
        if refresh_user:
            user = refresh_user(user)
            if not user:
                remove(user_key, row["user_id"], row["handle"], why="account_unavailable", root=root)
                out["removed"].append(row["account_key"]); continue
        r = dd.deep_dive(user, call_tool, owner_id, friends=friends, now=t, cfg=dcfg, allow_reply_heavy=allow_reply_heavy)
        out["calls_used"] += (r["analysis"]["coverage"].get("calls") or 0)
        if r.get("hard_skip"):
            remove(user_key, row["user_id"], row["handle"], why="hard_skip", root=root)
            out["removed"].append(row["account_key"]); continue
        reason = inactive_reason(r, dcfg)
        api_error = not r["analysis"]["n_items"] and "API error" in r["analysis"]["coverage"]["stop_reasons"]
        con = rec.connect(user_key, root)
        try:
            if reason or api_error:  # still inactive (or unknown): push out, never surface a guess
                n = row["recheck_count"] + 1
                cap = c["RECHECK_MAX_COUNT"]
                if cap is not None and n >= cap:
                    con.execute("UPDATE recheck_queue SET recheck_count=?, last_checked_at=?, status='resolved', "
                                "resolved_reason='max_rechecks' WHERE owner_key=? AND account_key=?",
                                (n, _iso(t), user_key, row["account_key"]))
                else:
                    con.execute("UPDATE recheck_queue SET recheck_count=?, last_checked_at=?, recheck_due=?, "
                                "reason=coalesce(?,reason) WHERE owner_key=? AND account_key=?",
                                (n, _iso(t), _iso(t + dt.timedelta(days=c["RECHECK_INTERVAL_DAYS"])), reason,
                                 user_key, row["account_key"]))
                out["still_inactive"].append(row["account_key"])
            else:
                con.execute("UPDATE recheck_queue SET recheck_count=recheck_count+1, last_checked_at=?, "
                            "status='resolved', resolved_reason='reactivated' WHERE owner_key=? AND account_key=?",
                            (_iso(t), user_key, row["account_key"]))
                out["surface"].append({**r, "user": user, "rechecked": True})
            con.commit()
        finally:
            con.close()
    return out
