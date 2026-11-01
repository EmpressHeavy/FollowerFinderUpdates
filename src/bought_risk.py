"""Bought-account risk flag for the daily verified review (v3.5.20).

Flags a verified follower whose account is very new but already has thousands of followers and a polished bio, a
common sign of a bought or farmed account. Never an auto-Skip and never a score change. With the owner's
situational pref show_bought_risk OFF (default) flagged accounts are left off the card. ON: they're shown with a
visible "Possible bought or bot account" flag and a one-line reason, and the owner decides.

Polished bio heuristic (all configurable): non-empty, at least bio_min_chars characters, and at least
bio_min_signals of: a separator (| • · / ▪ ✦ ➡), an emoji, a link or @handle, a title word (founder, ceo,
entrepreneur, investor, coach, creator, ...), two or more sentences or lines.
"""
from __future__ import annotations

import datetime as dt
import re

BOUGHT_RISK_CONFIG = {
    "max_account_age_days": 90,
    "min_followers": 2000,
    "bio_min_chars": 60,
    "bio_min_signals": 2,
}
FLAG = "Possible bought or bot account"
SEP_RE = re.compile(r"[|•·/▪✦➡]")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
LINK_RE = re.compile(r"(https?://\S+|@\w{2,})")
TITLE_RE = re.compile(r"\b(founder|co-?founder|ceo|cto|entrepreneur|investor|coach|creator|mentor|author|speaker|"
                      r"trader|strategist|consultant|expert|visionary|building)\b", re.I)


def bio_signals(bio: str | None) -> list[str]:
    b = (bio or "").strip()
    out = []
    if SEP_RE.search(b): out.append("separators")
    if EMOJI_RE.search(b): out.append("emoji")
    if LINK_RE.search(b): out.append("link/handle")
    if TITLE_RE.search(b): out.append("title words")
    if len([s for s in re.split(r"[.!?\n]+", b) if s.strip()]) >= 2: out.append("multi-sentence")
    return out


def assess(user: dict, *, now: dt.datetime | None = None, cfg: dict | None = None) -> dict:
    c = {**BOUGHT_RISK_CONFIG, **(cfg or {})}
    now = now or dt.datetime.now(dt.timezone.utc)
    created = user.get("created_at")
    fol = (user.get("public_metrics") or {}).get("followers_count")
    bio = (user.get("description") or "").strip()
    if not created or fol is None or not bio:
        return {"flag": False}
    age = (now - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).days
    sig = bio_signals(bio)
    if age < c["max_account_age_days"] and fol >= c["min_followers"] and len(bio) >= c["bio_min_chars"] \
            and len(sig) >= c["bio_min_signals"]:
        return {"flag": True, "reason": f"joined {age} days ago but has {fol:,} followers and a polished bio ({', '.join(sig)})"}
    return {"flag": False}


def card_text(a: dict) -> str:
    return f"⚠ {FLAG}: {a['reason']}"


def apply(user_key_unused, pairs: list[tuple[dict, dict]], *, show: bool, now=None, cfg=None) -> list[dict]:
    """pairs: (user, profile). OFF -> flagged profiles dropped. ON -> kept with a card flag line. Returns profiles."""
    out = []
    for user, prof in pairs:
        a = assess(user, now=now, cfg=cfg)
        if a["flag"]:
            if not show:
                continue
            prof["bought_risk"] = a
            prof.setdefault("card_extras", []).append(card_text(a))
        out.append(prof)
    return out
