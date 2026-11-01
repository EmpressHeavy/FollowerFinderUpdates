"""Start and end follower count for every termination session (added 2026-10-16 ~4:26 PM PT).

All users, per-user isolated (keyed by user_key). Counts come from a read-only
x get_users_by_username(user.fields=public_metrics) -> data.public_metrics.followers_count.
Start count is shown in the first chat message; end shows 'Removed this session: N'
(verified removals only) and a fresh final count, with a lag caveat. Both go to BATCH LOG
and history.jsonl.
"""
from __future__ import annotations
import datetime as dt
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
LAG_NOTE = ("X follower counts can lag, and new follows or unfollows during the session also change them, "
            "so the difference may not equal the number removed exactly.")


def parse_followers(resp: dict) -> int | None:
    """followers_count from an x get_users_by_username response, or None if not available."""
    try:
        v = resp["data"]["public_metrics"]["followers_count"]
        return int(v)
    except (KeyError, TypeError, ValueError):
        return None


def fmt_time(t: dt.datetime) -> str:
    t = t.astimezone(PT)
    return f"{t:%b} {t.day}, {t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'} PT"


def fmt_count(n: int | None) -> str:
    return "Not available" if n is None else f"{n:,}"


def start_record(user_key: str, handle: str, followers: int | None, at: dt.datetime) -> dict:
    return {"user_key": user_key, "handle": "@" + handle.lstrip("@"), "start_followers": followers,
            "start_read_at_pt": at.astimezone(PT).strftime("%Y-%m-%d %H:%M:%S PT"),
            "source": "x get_users_by_username public_metrics.followers_count (read-only)"}


def start_message(rec: dict, at: dt.datetime) -> str:
    return f"Your follower count at the start of this session: {fmt_count(rec['start_followers'])} (as of {fmt_time(at)})."


def end_summary(start: dict, verified_removed: int, end_followers: int | None, at: dt.datetime) -> dict:
    s = start.get("start_followers")
    diff = None if s is None or end_followers is None else end_followers - s
    msg = (f"Removed this session: {verified_removed}\n"
           f"Follower count now: {fmt_count(end_followers)} (as of {fmt_time(at)}; started at {fmt_count(s)})\n"
           f"Note: {LAG_NOTE}")
    return {**start, "verified_removed": verified_removed, "end_followers": end_followers,
            "end_read_at_pt": at.astimezone(PT).strftime("%Y-%m-%d %H:%M:%S PT"), "net_change": diff,
            "matches_removed": (diff == -verified_removed) if diff is not None else None, "message": msg}


def batch_log_note(summary: dict) -> str:
    return (f"Followers start {fmt_count(summary['start_followers'])} ({summary['start_read_at_pt']}) -> end "
            f"{fmt_count(summary['end_followers'])} ({summary['end_read_at_pt']}); removed this session (verified): "
            f"{summary['verified_removed']}")
