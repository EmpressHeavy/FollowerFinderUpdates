"""X "About this account" page: parser, mismatch check and paced Stage 2 read plan (v3.5.8).

How the page is reached (box-browser scan path): open the profile, click the "Joined <Month Year>" text, and the
About page opens (also at https://x.com/<handle>/about). The bot passes the page's visible text, or its
accessibility-tree text, to `parse_about_text`. Fields:

    date_joined, account_based_in, verified, id_verified, username_changes, connected_via

Any field the page does not show is "Not available". Nothing is ever guessed or inferred (no nationality from
names, photos or language). Location alone is never a judgment; see classify.stage2_classify for how the fields
are weighed. No X calls are made by this module.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from .read_pacing import PaceConfig, PaceEvent, PaceMachine, PaceState
except ImportError:  # pragma: no cover
    from read_pacing import PaceConfig, PaceEvent, PaceMachine, PaceState

NA = "Not available"
ABOUT_FIELDS = ("date_joined", "account_based_in", "verified", "id_verified", "username_changes", "connected_via")

# Label variants as X shows them (lower-case, no trailing colon).
_LABELS: dict[str, tuple[str, ...]] = {
    "date_joined": ("date joined", "joined"),
    "account_based_in": ("account based in", "based in"),
    "id_verified": ("id verified", "identity verified", "id verification"),
    "verified": ("verified",),
    "username_changes": ("username changes", "username change", "changed username"),
    "connected_via": ("connected via",),
}
# Longest first so "id verified" wins over "verified", "account based in" over "based in".
_LABEL_ORDER = sorted(((lab, f) for f, labs in _LABELS.items() for lab in labs), key=lambda x: -len(x[0]))
_NOISE = {"about this account", "back", "close", "learn more", ""}

_A11Y_PREFIX = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\[\d+\]\s*)?(?:(?:statictext|text|heading|generic|link|button|img|paragraph|listitem|"
    r"labeltext|cell|group)\b[: ]*)?",
    re.I,
)


def _clean_line(line: str) -> str:
    s = _A11Y_PREFIX.sub("", str(line or ""))
    s = re.sub(r"\[(?:level|ref|cursor)[^\]]*\]", "", s, flags=re.I)
    s = s.strip().strip('"').strip("'").strip()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" :")


def _match_label(line: str) -> tuple[str | None, str]:
    """Return (field, rest-of-line) when the line starts with a known label."""
    low = line.lower()
    for lab, f in _LABEL_ORDER:
        if low == lab:
            return f, ""
        if low.startswith(lab) and len(low) > len(lab) and low[len(lab)] in " :\t,":
            if f == "verified" and low.startswith("verified since"):
                return f, line  # "Verified since March 2023" is a value line, keep it whole
            return f, line[len(lab):].strip(" :\t,")
    return None, ""


def _yes_no(value: str) -> str:
    v = value.strip().lower()
    if not v:
        return NA
    if v.startswith(("not ", "no", "unverified", "isn't", "is not")) or v in {"false", "none"}:
        return "No"
    if v.startswith(("yes", "verified", "since", "true", "id verified", "identity verified")) or re.search(r"\b(19|20)\d\d\b", v):
        return "Yes"
    return NA


def _count(value: str) -> int | str:
    v = value.strip().lower()
    if not v:
        return NA
    if v.startswith(("no ", "none", "never")) or v == "no":
        return 0
    m = re.search(r"\d[\d,]*", v)
    return int(m.group(0).replace(",", "")) if m else NA


def _blank() -> dict[str, Any]:
    return {f: NA for f in ABOUT_FIELDS}


def parse_about_text(text: str | Iterable[str] | None) -> dict[str, Any]:
    """Parse the About page's visible or accessibility-tree text. Unknown/missing fields stay "Not available"."""
    out = _blank()
    if not text:
        out["about_read"] = False
        return out
    raw_lines = text.splitlines() if isinstance(text, str) else [str(x) for x in text]
    lines = [c for c in (_clean_line(l) for l in raw_lines) if c.lower() not in _NOISE]
    i = 0
    found = False
    while i < len(lines):
        f, rest = _match_label(lines[i])
        if f is None:
            i += 1
            continue
        value = rest
        if not value and i + 1 < len(lines) and (_match_label(lines[i + 1])[0] is None
                                              or lines[i + 1].lower().startswith("verified since")):
            value = lines[i + 1]
            i += 1
        i += 1
        if out[f] != NA:
            continue
        if f in ("verified", "id_verified"):
            # "Verified" as a bare label with no value line means nothing is shown for it.
            out[f] = _yes_no(value) if value else NA
            if out[f] == "Yes" and value and value.lower() not in ("yes", "verified"):
                out[f + "_detail"] = value
        elif f == "username_changes":
            out[f] = _count(value)
        elif value:
            out[f] = value
        found = found or out[f] != NA
    out["about_read"] = found
    return out


# ---------------------------------------------------------------- mismatch
_STORE_SUFFIX = re.compile(
    r"\s*(?:-\s*)?(?:apple\s+)?(?:app\s*store|google\s*play(?:\s*store)?|play\s*store|android\s*app|ios\s*app)\s*$",
    re.I,
)
_NOT_A_COUNTRY = {"", "web", "android", "ios", "iphone", "ipad", "x", "twitter", "unknown"}
_REGIONS = {"europe", "asia", "africa", "south asia", "east asia", "southeast asia", "middle east", "north america",
            "south america", "latin america", "oceania", "caribbean", "central america", "west africa",
            "east africa", "eastern europe", "western europe"}
_ALIASES = {"us": "united states", "usa": "united states", "u.s.": "united states", "u.s.a.": "united states",
            "united states of america": "united states", "america": "united states", "uk": "united kingdom",
            "great britain": "united kingdom", "britain": "united kingdom", "england": "united kingdom",
            "uae": "united arab emirates", "the netherlands": "netherlands", "holland": "netherlands",
            "south korea": "korea", "republic of korea": "korea", "russian federation": "russia"}


def norm_country(v: Any) -> str | None:
    s = re.sub(r"\s+", " ", str(v or "")).strip().strip(".").lower()
    if s.startswith("the "):
        s = s[4:]
    if not s or s == NA.lower():
        return None
    return _ALIASES.get(s, s)


def connected_via_country(connected_via: Any) -> str | None:
    """'Bangladesh App Store' -> 'Bangladesh'. Web/Android/etc. carry no country -> None."""
    s = str(connected_via or "").strip()
    if not s or s == NA:
        return None
    base = _STORE_SUFFIX.sub("", s).strip()
    if base == s and not re.search(r"store|play", s, re.I):
        return None  # e.g. "Web": no store, no country
    return base if base.lower() not in _NOT_A_COUNTRY else None


def location_mismatch(about: dict[str, Any] | None) -> dict[str, Any] | None:
    """Both a country-level 'Account based in' and a store country are shown and they differ.
    Regions ("Europe", "South Asia") never count. Returns None when there is nothing to compare."""
    if not about:
        return None
    based = norm_country(about.get("account_based_in"))
    store = connected_via_country(about.get("connected_via"))
    nstore = norm_country(store)
    if not based or not nstore or based in _REGIONS or nstore in _REGIONS:
        return None
    if based == nstore or based in nstore or nstore in based:
        return None
    return {"based_in": about.get("account_based_in"), "connected_via": about.get("connected_via"),
            "store_country": store}


def about_from_record(record: dict[str, Any] | None) -> dict[str, Any]:
    """Old snapshots have no About fields: they all come back "Not available" (about_read False).
    Accepts a nested `about` dict, raw `about_text`, or flat keys (`about_<field>` or the field name)."""
    rec = record or {}
    nested = rec.get("about")
    if isinstance(nested, str):
        return parse_about_text(nested)
    if rec.get("about_text"):
        return parse_about_text(rec.get("about_text"))
    src = nested if isinstance(nested, dict) else {}
    out = _blank()
    for f in ABOUT_FIELDS:
        v = src.get(f)
        if v in (None, ""):
            v = rec.get(f"about_{f}")
        if v in (None, "") and f == "account_based_in":
            v = rec.get("account_based_in")
        if v in (None, "") and f == "connected_via":
            v = rec.get("connected_via")
        if v in (None, ""):
            continue
        if f in ("verified", "id_verified"):
            v = v if isinstance(v, str) and v in ("Yes", "No", NA) else ("Yes" if v is True else "No" if v is False else _yes_no(str(v)))
        elif f == "username_changes":
            v = v if isinstance(v, int) and not isinstance(v, bool) else _count(str(v))
        out[f] = v
    read = src.get("about_read", rec.get("about_read"))
    out["about_read"] = bool(read) if read is not None else any(out[f] != NA for f in ABOUT_FIELDS)
    return out


def about_display(about: dict[str, Any] | None) -> list[str]:
    """Short 'label: value' parts for card text; only fields that are available."""
    a = about or {}
    parts = []
    if a.get("date_joined") not in (None, "", NA):
        parts.append(f"joined {a['date_joined']}")
    if a.get("account_based_in") not in (None, "", NA):
        parts.append(f"based in {a['account_based_in']}")
    if a.get("connected_via") not in (None, "", NA):
        parts.append(f"connected via {a['connected_via']}")
    if a.get("verified") == "Yes":
        parts.append("verified")
    if a.get("id_verified") == "Yes":
        parts.append("ID verified")
    uc = a.get("username_changes")
    if isinstance(uc, int) and not isinstance(uc, bool):
        parts.append(f"{uc} username change{'s' if uc != 1 else ''}")
    return parts


# ---------------------------------------------------------------- Stage 2 read plan + pacing
def about_read_queue(items: Iterable[tuple[str, dict[str, Any]]]) -> list[str]:
    """Handles whose About page should be read: ONLY accounts that survived Stage 1 (escalated to Stage 2),
    never protected / kept ones, never one already read."""
    out = []
    for handle, s1 in items:
        s1 = s1 or {}
        if not s1.get("escalate"):
            continue
        if str(s1.get("label") or "").upper().startswith("PROTECTED") or s1.get("about_read"):
            continue
        h = str(handle or "").strip().lstrip("@")
        if h and h not in out:
            out.append(h)
    return out


def about_url(handle: str) -> str:
    return f"https://x.com/{str(handle).strip().lstrip('@')}/about"


@dataclass
class AboutReadPacer:
    """Pacing for About-page reads (each one is a page view on X). Defaults stay well under X's limits:
    about 8-12 s between reads, a 90 s pause after every 20, a 3 minute cooldown after a 429/warning, and a stop
    for the session after 2 consecutive 429s / challenges (resume later from `done`)."""
    min_interval_s: float = 8.0
    jitter_s: float = 4.0
    burst_size: int = 20
    burst_pause_s: float = 90.0
    cooldown_429_s: float = 180.0
    max_per_session: int = 150
    machine: PaceMachine = field(default_factory=lambda: PaceMachine(PaceConfig(
        initial_batch_size=20, min_batch_size=5, max_batch_size=20, cooldown_seconds=180.0,
        hard_stop_after_failures=2)))
    reads: int = 0
    done: list[str] = field(default_factory=list)
    _rng: random.Random = field(default_factory=lambda: random.Random(0), repr=False)

    def can_continue(self) -> bool:
        return self.machine.can_continue() and self.reads < self.max_per_session

    def wait_before_next(self) -> float:
        """Seconds to wait before the next About read (the caller sleeps; nothing sleeps here)."""
        if not self.can_continue():
            return 0.0
        if self.machine.state == PaceState.COOLDOWN:
            return self.cooldown_429_s
        if self.reads == 0:
            return 0.0
        if self.reads % self.burst_size == 0:
            return self.burst_pause_s
        return self.min_interval_s + self._rng.uniform(0, self.jitter_s)

    def record(self, handle: str, event: PaceEvent | str = PaceEvent.SUCCESS) -> dict[str, Any]:
        ev = PaceEvent(event) if not isinstance(event, PaceEvent) else event
        st = self.machine.note_event(ev, key=handle)
        if ev == PaceEvent.SUCCESS:
            self.reads += 1
            if handle not in self.done:
                self.done.append(handle)
        return {**st, "reads": self.reads, "stop": not self.can_continue()}

    def plan(self, handles: Iterable[str]) -> list[str]:
        return [h for h in handles if h not in self.done]
