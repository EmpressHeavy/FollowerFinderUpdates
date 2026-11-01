"""Silent drop for drive-by follows (v3.5.19).

A verified account that follows the owner and unfollows within DRIVE_BY_CONFIG["DRIVE_BY_WINDOW_HOURS"] (24) is
quietly left out of the daily verified follower card: no flag, no notification, no card text. It is also never put on
(and is silently resolved in) the monthly recheck queue.

Data: the follower list the review already reads each run (get_users_me_followers). observe() records first_seen /
last_seen per follower in the owner's terminator.db (table verified_follower_seen). A follower first seen less than 24 h
ago that is missing from the current list is marked drive_by.

Choice (documented): only that drive-by OCCURRENCE is dropped. If the account follows again later it starts a new
occurrence (first_seen reset, status present) and is reviewed normally like any new follower; a second quick
unfollow is again a silent drop. Detection depends on snapshot frequency: a follow + unfollow entirely between two
snapshots is never seen at all (and so never shown), which is the same outcome.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

try:
    from . import records as rec
except ImportError:  # top-level import
    import records as rec

DRIVE_BY_CONFIG = {"DRIVE_BY_WINDOW_HOURS": 24}


def _utc(now):
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def observe(user_key: str, followers: Iterable[dict], *, now=None, root=None, cfg=None) -> set[str]:
    """Record the current follower snapshot. Returns ids newly marked drive_by in this run."""
    c = {**DRIVE_BY_CONFIG, **(cfg or {})}
    t = _utc(now); ts = _iso(t)
    cur = {str(u["id"]): u for u in followers}
    new_drive_by: set[str] = set()
    con = rec.connect(user_key, root)
    try:
        rows = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT user_id, first_seen, status FROM verified_follower_seen WHERE owner_key=?", (user_key,))}
        for uid, u in cur.items():
            h = u.get("username")
            if uid not in rows:
                con.execute("INSERT INTO verified_follower_seen(owner_key,user_id,handle,first_seen,last_seen) VALUES(?,?,?,?,?)",
                            (user_key, uid, h, ts, ts))
            elif rows[uid][1] != "present":   # re-follow: a new occurrence
                con.execute("UPDATE verified_follower_seen SET status='present', first_seen=?, last_seen=?, handle=?, "
                            "drive_by_at=NULL WHERE owner_key=? AND user_id=?", (ts, ts, h, user_key, uid))
            else:
                con.execute("UPDATE verified_follower_seen SET last_seen=?, handle=? WHERE owner_key=? AND user_id=?",
                            (ts, h, user_key, uid))
        window = dt.timedelta(hours=c["DRIVE_BY_WINDOW_HOURS"])
        for uid, (first, status) in rows.items():
            if uid in cur or status != "present":
                continue
            if t - _parse(first) < window:
                con.execute("UPDATE verified_follower_seen SET status='drive_by', drive_by_at=? WHERE owner_key=? AND user_id=?",
                            (ts, user_key, uid))
                new_drive_by.add(uid)
            else:
                con.execute("UPDATE verified_follower_seen SET status='gone' WHERE owner_key=? AND user_id=?", (user_key, uid))
        con.commit()
    finally:
        con.close()
    return new_drive_by


def drive_by_ids(user_key: str, *, root=None) -> set[str]:
    con = rec.connect(user_key, root)
    try:
        return {r[0] for r in con.execute(
            "SELECT user_id FROM verified_follower_seen WHERE owner_key=? AND status='drive_by'", (user_key,))}
    finally:
        con.close()


def is_drive_by(user_key: str, user_id, *, root=None) -> bool:
    return user_id is not None and str(user_id) in drive_by_ids(user_key, root=root)


def exclude(user_key: str, items: list, *, root=None, key=lambda x: x.get("user_id") or x.get("id")) -> list:
    """Silently drop drive-by accounts from candidates/profiles (no text, no flag)."""
    bad = drive_by_ids(user_key, root=root)
    return [x for x in items if str(key(x)) not in bad]
