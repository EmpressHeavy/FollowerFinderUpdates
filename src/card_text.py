"""Shared card text (beta feedback 2026-10-20).

Every suggestion shows the account's post, follower and following counts.
v3.5.7 round-based review (owner default): flagged accounts go in rounds of 5, BOT-LIKE first, then REVIEW. Each
round is (a) a chat message listing its accounts (number, clickable markdown x.com link, label, stats, join date,
reason) and (b) a multi-select card "Round N of M: which should I terminate?" with the accounts plus
"Mark all K for termination" (danger). Selected accounts are terminated right after the pick; unselected stay in
review. Picks come only from the card (no typed-number replies, no all-at-once list).
"""
from __future__ import annotations

from typing import Any

CARD_SIZE = 5
REVIEW_NOTE = "Unmarked accounts will be kept in review."
MARK_ALL_TERMINATION = "Mark all for termination"
NA = "Not available"


def _n(v: Any) -> str:
    if v is None or v == "" or v == NA:
        return "?"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "?"


def counts(acct: dict) -> dict[str, Any]:
    g = acct.get
    return {"posts": g("post_count", g("posts", g("statuses_count"))),
            "followers": g("followers", g("followers_count")),
            "following": g("following", g("following_count"))}


def counts_text(acct: dict) -> str:
    c = counts(acct)
    if all(v in (None, "", NA) for v in c.values()):
        return "posts/followers/following: Not available"
    return f"{_n(c['posts'])} posts · {_n(c['followers'])} followers · {_n(c['following'])} following"


_ABOUT_KEYS = ("date_joined", "account_based_in", "verified", "id_verified", "username_changes", "connected_via")


def about_of(acct: dict) -> dict:
    """v3.5.8 About-this-account fields from a card record (nested `about` dict or flat `about_<field>` keys)."""
    nested = acct.get("about") if isinstance(acct.get("about"), dict) else {}
    return {k: nested.get(k, acct.get(f"about_{k}", NA)) for k in _ABOUT_KEYS}


def about_text(acct: dict) -> str:
    """' · based in X · connected via Y · ID verified · 3 username changes', only fields that are available."""
    a = about_of(acct)
    parts = []
    if a["account_based_in"] not in (None, "", NA):
        parts.append(f"based in {a['account_based_in']}")
    if a["connected_via"] not in (None, "", NA):
        parts.append(f"connected via {a['connected_via']}")
    if a["verified"] == "Yes":
        parts.append("verified")
    if a["id_verified"] == "Yes":
        parts.append("ID verified")
    uc = a["username_changes"]
    if isinstance(uc, int) and not isinstance(uc, bool):
        parts.append(f"{uc} username change{'s' if uc != 1 else ''}")
    return "".join(f" · {p}" for p in parts)


def stats_text(acct: dict) -> str:
    """Stats line: counts plus the About fields that are available."""
    return counts_text(acct) + about_text(acct)


def handle(acct: dict) -> str:
    return str(acct.get("handle") or acct.get("username") or "").strip().lstrip("@")


def link(acct: dict) -> str:
    return f"https://x.com/{handle(acct)}"


def flags_text(acct: dict) -> str:
    out = []
    if acct.get("impersonation_flag") or any("impersonation" in str(s).lower() for s in acct.get("signals") or ()):
        out.append("possible impersonation")
    return (" ⚠ " + ", ".join(out)) if out else ""


def option_label(n: int, acct: dict, tail: str = "") -> str:
    lab = f"{n}. @{handle(acct)} — {counts_text(acct)}{flags_text(acct)}"
    return lab + (f" — {tail}" if tail else "")


def details_text(acct: dict) -> str:
    """v3.5.5: account age, last post and the spam/impersonation signals, shown in the links message
    ('Not available' when the scan didn't get it)."""
    g = acct.get
    joined = str(g("created_at") or g("account_created") or "")[:10] or NA
    last = str(g("last_post_at") or g("most_recent_post_at") or "")[:16].replace("T", " ") or NA
    sig = [str(s) for s in (g("signals") or ()) if s]
    out = f"joined {joined} · last post {last}"
    return out + (f" · signals: {'; '.join(sig[:4])}" if sig else "")


def links_message(title: str, numbered: list[tuple[int, dict]], reason_key: str = "label_short") -> str:
    lines = [title]
    for n, a in numbered:
        why = a.get(reason_key) or a.get("label") or ""
        line = f"{n}. @{handle(a)} — {link(a)} — {stats_text(a)}{flags_text(a)}" + (f" — {why}" if why else "")
        if any(a.get(k) for k in ("created_at", "account_created", "last_post_at", "most_recent_post_at", "signals")):
            line += f"\n   {details_text(a)}"
        lines.append(line)
    return "\n".join(lines)


LABEL_TEXT = {"bot": "likely bot", "review": "worth a closer look"}
_BOILER = ("unverified", "you follow", "account based in", "30-day activity", "last post date", "x account country")


def kind(acct: dict) -> str:
    lab = str(acct.get("label") or "").upper()
    return "bot" if lab.startswith(("BOT-LIKE", "LIKELY BOT", "INACTIVE")) else "review"


def round_reason(acct: dict) -> str:
    r = acct.get("reason")
    if r and r != NA:
        return str(r)
    sig = [str(s) for s in (acct.get("signals") or ()) if s and not str(s).lower().startswith(_BOILER)]
    return "; ".join(sig[:3]) if sig else NA


def round_line(n: int, acct: dict) -> str:
    h = handle(acct)
    about_joined = about_of(acct)["date_joined"]
    joined = (about_joined if about_joined not in (None, "", NA) else
              str(acct.get("joined") or acct.get("created_at") or acct.get("account_created") or "")[:10] or NA)
    return (f"{n}. [@{h}]({link(acct)}) — {LABEL_TEXT[kind(acct)]} — {stats_text(acct)} · joined {joined}"
            f"{flags_text(acct)} — {round_reason(acct)}")


def round_title(r: int, m: int) -> str:
    return f"Round {r} of {m}: which should I terminate?"


def round_message(r: int, m: int, numbered: list[tuple[int, dict]]) -> str:
    return "\n".join([f"**Round {r} of {m}** — flagged accounts:"] + [round_line(n, a) for n, a in numbered])


def mark_all_label(k: int) -> str:
    return f"Mark all {k} for termination"


def prompt(text: str) -> str:
    """Card prompt that always ends with the review note."""
    t = text.strip()
    return t if t.endswith(REVIEW_NOTE) else f"{t} {REVIEW_NOTE}"
