"""Per-user suggestion history (v3.5.36).

Every account this owner is shown is appended to
data/users/<user_key>/suggestions/history.jsonl. The file is append-only and hash-chained, the same way
follow_back.jsonl is. Nothing is rewritten, and one owner's file is never read for another owner.

States (separate):
  SUGGESTED        shown on a card
  CHECKED          researched in a pass but not offered (so a later run does not fetch it again yet)
  FOLLOWED         a follow was confirmed (button verified). This is not a rejection.
  DECLINED         the owner explicitly chose skip / declined. This is a rejection: do not suggest again.
  NOT_PICKED       shown, and the owner left it unticked. Not a rejection.
  NO_FOLLOW_BACK   followed, and they did not follow back. Not a rejection.

SUGGESTED, CHECKED, NOT_PICKED and NO_FOLLOW_BACK block another suggestion only until COOLDOWN_DAYS has passed.
After that the account can be researched and offered again. DECLINED stays blocked. FOLLOWED stays blocked until
a later NO_FOLLOW_BACK, which starts the cooldown instead of a permanent rejection.

Missing facts are never invented. This module only stores what the caller actually did.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Iterable

try:
    from . import storage as st
except ImportError:  # pragma: no cover
    import storage as st

COOLDOWN_DAYS = 30
DECLINE_ALL = "__decline_all__"
SKIP_ALL = "Skip all"
ALREADY_SUGGESTED_NOTE = (
    "No new suggestions this round. Accounts already suggested stay off the list during the cooldown. "
    "Not picking one, or them not following back, is not a rejection."
)
_EVENTS = ("SUGGESTED", "CHECKED", "FOLLOWED", "DECLINED", "NOT_PICKED", "NO_FOLLOW_BACK", "RESEARCH_PASS")


def _dir(user_key: str, root=None) -> Path:
    d = st.user_data_root(user_key, root) / "suggestions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def history_path(user_key: str, root=None) -> Path:
    return _dir(user_key, root) / "history.jsonl"


def _stamp(now) -> str:
    if now is None:
        now = dt.datetime.now().astimezone()
    elif not isinstance(now, dt.datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone().isoformat(timespec="seconds")


def _parse(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def _prev_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            last = line
    return hashlib.sha256(last.encode()).hexdigest() if last else None


def append_event(user_key: str, event: dict, root=None, *, now=None) -> dict:
    """Append one line. Never rewrites earlier lines."""
    name = event.get("event")
    if name not in _EVENTS:
        raise ValueError(f"unknown suggestion event {name!r}")
    path = history_path(user_key, root)
    row = {"at": _stamp(now), **event, "prev_entry_sha256": _prev_hash(path)}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    st.apply_user_permissions(user_key, root)
    return row


def read_events(user_key: str, root=None) -> list[dict]:
    path = history_path(user_key, root)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def is_decline_answer(raw) -> bool:
    """True when this card answer is an explicit skip / decline, not an empty tick list."""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        return False
    return any(str(v) in (DECLINE_ALL, SKIP_ALL) for v in raw)


def is_rejection(status: str | None) -> bool:
    """Only an explicit decline is a rejection. Shown-but-not-picked and no-follow-back are not."""
    return status == "declined"


def _blank() -> dict:
    return {"declined": False, "followed_at": None, "no_follow_back_at": None, "suggested_at": None,
            "not_picked_at": None, "checked_at": None, "handle": ""}


def _fold(events: Iterable[dict]) -> dict[str, dict]:
    by: dict[str, dict] = {}
    for e in events:
        uid = str(e.get("user_id") or "")
        if not uid:
            continue
        stt = by.setdefault(uid, _blank())
        if e.get("handle"):
            stt["handle"] = e["handle"]
        at = _parse(e.get("at"))
        ev = e.get("event")
        if ev == "DECLINED":
            stt["declined"] = True
        elif ev == "FOLLOWED":
            stt["followed_at"] = at
        elif ev == "NO_FOLLOW_BACK":
            stt["no_follow_back_at"] = at
        elif ev == "NOT_PICKED":
            stt["not_picked_at"] = at
        elif ev == "SUGGESTED":
            stt["suggested_at"] = at
        elif ev == "CHECKED":
            stt["checked_at"] = at
    return by


def _within(anchor: dt.datetime | None, now: dt.datetime) -> bool:
    if anchor is None:
        return False
    return now < anchor + dt.timedelta(days=COOLDOWN_DAYS)


def _as_utc(now) -> dt.datetime:
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def status_label(stt: dict) -> str:
    if stt.get("declined"):
        return "declined"
    fol, nfb, sug = stt.get("followed_at"), stt.get("no_follow_back_at"), stt.get("suggested_at")
    if sug and (fol is None or sug >= fol) and (nfb is None or sug >= nfb) and not stt.get("declined"):
        if stt.get("not_picked_at") and stt["not_picked_at"] >= sug:
            return "not_picked"
        return "suggested"
    if fol and (nfb is None or nfb < fol):
        return "followed"
    if nfb and (fol is None or nfb >= fol):
        return "no_follow_back"
    if stt.get("not_picked_at"):
        return "not_picked"
    if sug:
        return "suggested"
    if stt.get("checked_at"):
        return "checked"
    return "none"


def is_blocked(stt: dict, now=None) -> bool:
    now = _as_utc(now)
    if stt.get("declined"):
        return True
    fol, nfb = stt.get("followed_at"), stt.get("no_follow_back_at")
    sug, picked = stt.get("suggested_at"), stt.get("not_picked_at")
    if sug and (fol is None or sug >= fol) and (nfb is None or sug >= nfb):
        anchor = max(t for t in (sug, picked) if t)
        return _within(anchor, now)
    if fol and (nfb is None or nfb < fol):
        return True
    if nfb and (fol is None or nfb >= fol):
        return _within(nfb, now)
    anchors = [t for t in (picked, sug, stt.get("checked_at")) if t]
    return bool(anchors) and _within(max(anchors), now)


def account_status(user_key: str, user_id: str, *, now=None, root=None) -> dict:
    stt = _fold(read_events(user_key, root)).get(str(user_id), _blank())
    label = status_label(stt)
    return {"user_id": str(user_id), "status": label, "rejection": is_rejection(label),
            "blocked": is_blocked(stt, now), "handle": stt.get("handle") or ""}


def blocked_ids(user_key: str, *, now=None, root=None) -> set[str]:
    """Ids that must not be suggested or fetched again right now. Empty when this owner has no log.
    Includes accounts only checked (not offered) so a repeat run does not redo that research."""
    now = _as_utc(now)
    return {uid for uid, stt in _fold(read_events(user_key, root)).items() if is_blocked(stt, now)}


def active_suggestion_ids(user_key: str, *, now=None, root=None) -> set[str]:
    """Blocked because they were suggested, followed, declined, or are waiting out a not-picked / no-follow-back
    cooldown. Checked-only ids are not included: those were never offered."""
    now = _as_utc(now)
    out = set()
    for uid, stt in _fold(read_events(user_key, root)).items():
        if status_label(stt) == "checked":
            continue
        if is_blocked(stt, now):
            out.add(uid)
    return out


def _account_event(user_key: str, event: str, user_id, handle: str, *, source: str, run_id: str | None,
                   root=None, now=None) -> dict:
    uid = str(user_id or "")
    if not uid:
        raise ValueError("user_id is required")
    return append_event(user_key, {"event": event, "user_id": uid, "handle": handle or "",
                                   "source": source, "run_id": run_id}, root, now=now)


def record_suggested(user_key: str, accounts: Iterable[dict], *, source: str, run_id: str | None,
                     root=None, now=None) -> int:
    """Append SUGGESTED for each account actually offered. Returns how many lines were added."""
    n = 0
    for a in accounts or []:
        uid = str(a.get("user_id") or "")
        if not uid:
            continue
        _account_event(user_key, "SUGGESTED", uid, str(a.get("handle") or ""), source=source,
                       run_id=run_id, root=root, now=now)
        n += 1
    return n


def record_checked(user_key: str, accounts: Iterable[dict], *, source: str, run_id: str | None,
                   root=None, now=None) -> int:
    """Researched but not offered. Blocks a repeat fetch until the cooldown, and is not a rejection."""
    n = 0
    for a in accounts or []:
        uid = str(a.get("user_id") or "")
        if not uid:
            continue
        _account_event(user_key, "CHECKED", uid, str(a.get("handle") or ""), source=source,
                       run_id=run_id, root=root, now=now)
        n += 1
    return n


def record_pass(user_key: str, *, pass_id: str, source: str, qualified_ids: Iterable[str], checked: int,
                root=None, now=None) -> dict:
    """One line listing every qualified id this research pass returned. Append-only."""
    ids = [str(i) for i in qualified_ids if str(i)]
    return append_event(user_key, {"event": "RESEARCH_PASS", "pass_id": pass_id, "source": source,
                                   "qualified_ids": ids, "checked": int(checked)}, root, now=now)


def record_followed(user_key: str, user_id, handle: str, *, run_id: str | None = None,
                    source: str = "follow", root=None, now=None) -> dict:
    return _account_event(user_key, "FOLLOWED", user_id, handle, source=source, run_id=run_id, root=root, now=now)


def record_no_follow_back(user_key: str, user_id, handle: str, *, run_id: str | None = None,
                          source: str = "followback_window", root=None, now=None) -> dict:
    return _account_event(user_key, "NO_FOLLOW_BACK", user_id, handle, source=source, run_id=run_id,
                          root=root, now=now)


def record_card_result(user_key: str, run_id: str, card_ids: Iterable[str], selected_ids: Iterable[str],
                       handles: dict | None, *, declined: bool, source: str, root=None, now=None) -> int:
    """Record the owner's answer for one card. Selected ids wait for a confirmed FOLLOWED line.
    Unticked ids are NOT_PICKED. An explicit decline records DECLINED for every id on the card.
    Neither NOT_PICKED nor a later no-follow-back is a rejection."""
    selected = {str(i) for i in selected_ids or []}
    handles = handles or {}
    n = 0
    for raw in card_ids or []:
        uid = str(raw)
        if not uid:
            continue
        handle = str(handles.get(uid) or handles.get(raw) or "")
        if declined:
            event = "DECLINED"
        elif uid not in selected:
            event = "NOT_PICKED"
        else:
            continue
        _account_event(user_key, event, uid, handle, source=source, run_id=run_id, root=root, now=now)
        n += 1
    return n
