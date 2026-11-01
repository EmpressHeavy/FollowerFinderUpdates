"""Blocked-by filter for the daily verified review (v3.5.25).

Detection, checked honestly against the stack on 2026-10-28:
- The X API has no blocked-by signal. user.fields=connection_status is real (returned live: e.g. ["followed_by"]), but
  X documents only follow_request_received, follow_request_sent, blocking, followed_by, following and muting. "blocking"
  means the OWNER blocks them, not that they block the owner. No blocked_by field exists, so none is read here.
- The signed-in box browser is the only reliable signal. On https://x.com/<handle> X shows "You're blocked" /
  "@<handle> blocked you". The steps are in BROWSER_BLOCK_CHECK_STEPS.

Cache: the blocked_by table in terminator.db is append-only (triggers refuse UPDATE/DELETE). The latest row per
account wins. Accounts are rechecked in the browser when there's no row or the latest is older than BLOCK_RECHECK_DAYS.

Effect: silent, with no notice or card text. A blocked account is dropped before any profiling, deep dive, scoring or
interaction counting. It gets no card and no recheck queue entry, and follow_executor refuses it even if it was picked.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

try:
    from . import records as rec
except ImportError:  # top-level import
    import records as rec

BLOCK_CONFIG = {"BLOCK_RECHECK_DAYS": 30}
BLOCK_NOTICES = ("You're blocked", "blocked you")
BROWSER_BLOCK_CHECK_STEPS = (
    "Browser step: blocked-by check (box browser, signed in to X; read only).\n"
    "For each id from blocked_by.needs_check(), before any profiling or deep dive:\n"
    "1. Open https://x.com/<handle>.\n"
    "2. Read the profile notice only. If it says \"You're blocked\" or \"@<handle> blocked you\", report blocked=True. "
    "Otherwise report blocked=False.\n"
    "3. Call blocked_by.record(owner, user_id, handle, blocked, source='browser').\n"
    "Never click anything (no Follow, Block or Report). STOP on any X warning, captcha or login wall; unchecked "
    "accounts stay unchecked and are not treated as blocked. Tell the owner nothing."
)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now(now):
    return now or dt.datetime.now(dt.timezone.utc)


def page_says_blocked(page_text: str) -> bool:
    return any(n.lower() in (page_text or "").lower() for n in BLOCK_NOTICES)


def record(owner: str, user_id, handle: str | None, blocked: bool, *, source: str = "browser", now=None,
           root=None) -> None:
    con = rec.connect(owner, root)
    try:
        con.execute("INSERT INTO blocked_by(owner_key,account_key,handle,detected_at,blocked,source) VALUES(?,?,?,?,?,?)",
                    (owner, str(user_id), handle, _iso(_now(now)), 1 if blocked else 0, source))
        con.commit()
    finally:
        con.close()


def _latest(owner, user_id, root):
    con = rec.connect(owner, root)
    try:
        return con.execute("SELECT blocked, detected_at FROM blocked_by WHERE owner_key=? AND account_key=? "
                           "ORDER BY detected_at DESC LIMIT 1", (owner, str(user_id))).fetchone()
    finally:
        con.close()


def is_blocked_by(owner: str, user_id, *, root=None) -> bool:
    r = _latest(owner, user_id, root)
    return bool(r and r[0])


def needs_check(owner: str, users: Iterable[dict], *, now=None, root=None, cfg=None) -> list[dict]:
    c = {**BLOCK_CONFIG, **(cfg or {})}
    cutoff = _iso(_now(now) - dt.timedelta(days=c["BLOCK_RECHECK_DAYS"]))
    return [u for u in users if not (r := _latest(owner, u.get("id") or u.get("user_id"), root)) or r[1] < cutoff]


def filter_candidates(owner: str, users: Iterable[dict], *, root=None) -> list[dict]:
    """Silently drops accounts with a cached 'blocked you'. Call before profiling or deep dive."""
    return [u for u in users if not is_blocked_by(owner, u.get("id") or u.get("user_id"), root=root)]


def follow_guard(owner: str, root=None):
    """skip= callback for follow_executor.run: refuses a blocked account even if the owner picked it."""
    return lambda p: "blocked_by" if is_blocked_by(owner, p["user_id"], root=root) else None
