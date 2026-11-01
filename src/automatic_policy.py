"""Automatic Mode policy (default since 2026-10-16 4:01 PM PT; owner authorization via voice call).

Supersedes the 2026-10-14 plan (manual-only for the alpha owner, opt-in Automatic for marketplace) at the owner's
direction. ToS risk: the owner has already decided to accept the risk of assisted native removals (see
docs/product-architecture.md); this module only decides WHO may be removed, it performs no X action.

Rules
- Card answer = authorization: when the user submits a candidate card, selected accounts that are
  ELIGIBLE are authorized for removal against the frozen snapshot (no separate Start Task step).
- Eligible = classification Likely bot (BOT-LIKE, incl. BOT-LIKE + INACTIVE) with >= 2 independent
  signals. Never unverified-alone; never inactive-only.
- Selected 'Needs your look' / inactive-only -> 'Selected, awaiting confirmation' (explicit yes in chat).
- Protections run before classification AND again at execution time: accounts the user follows, verified,
  Always Keep, and per-user protected handles (from that user's own config: protected_handles) are never removed.
- Execution: X native 'Remove this follower', one at a time, ~10 s apart, recheck after each; stop and
  report on any X warning or rate limit. Removal sessions never block (Graveyard returns are blocked only via graveyard_returns.py, with authorization).
- Recording: Actions rows + column A Terminate + history.jsonl entry; GRAVEYARD only after a
  verified removal.
- [superseded 2026-10-16 ~4:05 PM PT] Unselected accounts get a follow-up card: Keep / Protect / Always Keep.

Amendment 2026-10-16 ~4:05 PM PT (owner):
- 'Select all' + done on a card (Automatic Mode) = every account on that card is authorized for
  termination immediately, INCLUDING Needs-your-look and inactive ones (explicit owner yes; overrides the
  confirmation step). Protections still apply before and at execution.
- Individually tapped accounts follow the policy above (CURRENT INTERPRETATION, pending owner review).
- No per-card follow-up. Unselected accounts across ALL cards are collected and shown after the last
  card on ONE final review card (see final_review_message / parse_final_review / final_review_mapping).

Amendment 2026-10-16 ~4:06 PM PT (owner; resolves the 'pending owner review' item):
- Automatic Mode: EVERY account selected on a card (individual tap or Select all) and submitted with done is
  authorized for immediate removal, incl. Needs-your-look and inactive-only. No confirmation prompt.
  The >=2-signal eligibility rule no longer gates selected accounts. Protections unchanged and rechecked
  at execution; X warnings / rate limits pause execution. Manual mode unchanged (awaits confirmation).
- Per-user isolation: mode, policy, whitelist and decisions are keyed by user_key.
- Mode can be switched back to Manual anytime; the switch is logged and affects only future cards.
"""
from __future__ import annotations
import datetime as dt, json, re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
AUTOMATIC, MANUAL = "automatic", "manual"
DEFAULT_MODE = AUTOMATIC
EXECUTION = {"method": "X native 'Remove this follower'", "one_at_a_time": True, "spacing_seconds": 10,
             "recheck_after_each": True, "stop_on": ["x_warning", "rate_limit", "x_error"], "on_stop": "pause and report", "block": False}
FOLLOW_UP_OPTIONS = ("Keep", "Protect", "Always Keep")  # superseded 2026-10-16 ~4:05 PM PT
FINAL_REVIEW_OPTIONS = ("Add all to Always Keep", "Mark all as not a bot (Keep)", "Leave all as is")
INDIVIDUAL_PICK_POLICY_STATUS = "resolved by owner 2026-10-16 ~4:06 PM PT: every selected account is authorized"
# Per-user protected handles are NOT hard-coded (isolation fix 2026-10-16). They come only from the
# user's own config (`protected_handles`), see protected_handles_for().

# Signals that are context/evidence notes, not independent bot evidence.
_NON_EVIDENCE_PREFIXES = ("unverified", "you follow", "account based in", "activity stats are sample",
                          "cleared stage 1", "30-day activity: not available", "verified")
_INACTIVITY_WORDS = ("inactive", "no posts in last", "dormant", "zero posts")


def independent_bot_signals(signals: list[str]) -> list[str]:
    """Bot-evidence signals only (excludes context notes and inactivity-only signals)."""
    out = []
    for s in signals or []:
        t = s.lower().strip()
        if t.startswith(_NON_EVIDENCE_PREFIXES) or any(w in t for w in _INACTIVITY_WORDS):
            continue
        out.append(s)
    return out


def _norm(h: str | None) -> str:
    return (h or "").lstrip("@").strip().lower()


@dataclass
class Account:
    username: str
    label: str  # BOT-LIKE / BOT-LIKE + INACTIVE / INACTIVE / REVIEW / KEEP / PROTECTED...
    signals: list[str] = field(default_factory=list)
    verified_type: str | None = "none"
    i_follow: bool | None = False
    always_keep: bool = False


def protected_handles_for(user_key: str, root=None) -> set[str]:
    """Normalized protected handles from THIS user's own config only (empty if the user has none)."""
    try:
        from storage import load_user_config_dict
    except ImportError:  # pragma: no cover
        from .storage import load_user_config_dict
    raw = load_user_config_dict(root, user_key=user_key)
    return {_norm(h) for h in (raw.get("protected_handles") or [])}


def _verified_reason(a):
    try:
        from .protections import verified_reason
    except ImportError:  # pragma: no cover
        from protections import verified_reason
    return verified_reason(a)


def protected_reason(a: Account, user_key: str, always_keep_handles=(), protected_handles=None) -> str | None:
    """protected_handles=None -> loaded from this user's own config."""
    if a.i_follow:
        return "you follow this account"
    if (a.verified_type or "none") not in ("none", "") or _verified_reason(a):
        return _verified_reason(a) or f"verified ({a.verified_type})"
    if a.always_keep or _norm(a.username) in {_norm(h) for h in always_keep_handles}:
        return "Always Keep"
    ph = protected_handles_for(user_key) if protected_handles is None else {_norm(h) for h in protected_handles}
    if _norm(a.username) in ph:
        return "owner-protected account"
    return None


def decide(a: Account, selected: bool, mode: str, user_key: str, always_keep_handles=(),
           via_select_all: bool = False, protected_handles=None) -> dict:
    """Outcome for one account on a submitted card."""
    pr = protected_reason(a, user_key, always_keep_handles, protected_handles)
    if pr:
        return {"action": "PROTECTED_SKIP", "status": f"Protected ({pr}); never removed", "authorized": False}
    if not selected:
        return {"action": "DEFER_FINAL_REVIEW", "status": "Not selected; held for the final review card",
                "authorized": False, "final_review_options": list(FINAL_REVIEW_OPTIONS)}
    if mode == AUTOMATIC:
        return {"action": "AUTO_REMOVE", "status": "Authorized by card answer (selected + done); queued for native removal",
                "authorized": True, "basis": "select_all_owner_yes" if via_select_all else "selected_owner_yes",
                "policy_status": INDIVIDUAL_PICK_POLICY_STATUS, "evidence": independent_bot_signals(a.signals)}
    return {"action": "AWAIT_CONFIRMATION", "status": "Selected, awaiting confirmation", "authorized": False,
            "reason": "Manual mode"}


def recheck_at_execution(a: Account, user_key: str, always_keep_handles=(), protected_handles=None) -> bool:
    """Second protection pass right before removal (fresh data). True = still OK to remove."""
    return protected_reason(a, user_key, always_keep_handles, protected_handles) is None


class ModeStore:
    """Per-user mode with an append-only switch log (data/users/{user_key}/mode.json)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _p(self, user_key: str) -> Path:
        return self.root / user_key / "mode.json"

    def get(self, user_key: str) -> str:
        p = self._p(user_key)
        return json.load(open(p)).get("mode", DEFAULT_MODE) if p.exists() else DEFAULT_MODE

    def switch(self, user_key: str, mode: str, by: str, now: dt.datetime | None = None) -> dict:
        if mode not in (AUTOMATIC, MANUAL):
            raise ValueError(mode)
        p = self._p(user_key); p.parent.mkdir(parents=True, exist_ok=True)
        d = json.load(open(p)) if p.exists() else {"mode": DEFAULT_MODE, "log": []}
        ts = (now or dt.datetime.now(PT)).strftime("%Y-%m-%d %H:%M PT")
        d["log"].append({"at": ts, "from": d["mode"], "to": mode, "by": by, "applies_to": "future cards only"})
        d["mode"] = mode
        with open(p, "w") as f:
            json.dump(d, f, indent=1)
        import os
        os.chmod(p, 0o600); os.chmod(p.parent, 0o755)  # mode.json is main data
        return d


def mode_for_card(card_issued_mode: str) -> str:
    """A card is judged under the mode in force when it was issued (switches affect only future cards)."""
    return card_issued_mode


# ---- Final review card for unselected accounts (2026-10-16 ~4:05 PM PT) ----
FINAL_REVIEW_MAP = {
    "ALWAYS_KEEP": {"label": "Always Keep", "sheet": "Keepers", "add_to_always_keep_list": True, "column_a": "Keep"},
    "NOT_A_BOT": {"label": "Not a bot (Keep)", "sheet": "Keepers", "add_to_always_keep_list": False, "column_a": "Keep"},
    "LEAVE": {"label": "Leave as is", "sheet": None, "add_to_always_keep_list": False, "column_a": None,
              "note": "logged; re-evaluated next scan"},
}


def collect_unselected(cards: list[dict]) -> list[dict]:
    """cards: [{"accounts": [acct dicts with chat_no], "selected": [chat_no,...]}]. Order kept, deduped."""
    out, seen = [], set()
    for c in cards:
        sel = set(c.get("selected", []))
        for a in c["accounts"]:
            if a["chat_no"] not in sel and a["chat_no"] not in seen:
                seen.add(a["chat_no"]); out.append(a)
    return out


def final_review_message(unselected: list[dict]) -> str:
    lines = [f"I noticed you didn't select these {len(unselected)} accounts. What do you want me to do with them?"]
    for a in unselected:
        reason = a.get("reason") or a.get("label") or "Not available"
        lines.append(f"{a['chat_no']}. @{a['handle']} (https://x.com/{a['handle']}): {reason}")
    return "\n".join(lines)


def _nums_in(t: str) -> list[int]:
    out = []
    for m in re.finditer(r"(\d+)(?:\s*(?:-|to)\s*(\d+))?", t):
        lo = int(m.group(1)); hi = int(m.group(2) or lo)
        out += list(range(lo, hi + 1))
    return out


def _kind(t: str) -> str | None:
    t = t.lower()
    if "always" in t or "protect" in t:
        return "ALWAYS_KEEP"
    if "not a bot" in t or "not bot" in t or re.search(r"\bkeep\b", t):
        return "NOT_A_BOT"
    if "leave" in t or "as is" in t or "skip" in t or "nothing" in t:
        return "LEAVE"
    return None


def parse_final_review(raw: str, nums: list[int]) -> dict[int, str]:
    """Map every unselected chat number to ALWAYS_KEEP / NOT_A_BOT / LEAVE.

    Accepts the three buttons or typed per-account instructions, e.g. 'Always Keep 11, 13; rest leave'.
    Unmentioned numbers default to LEAVE (never to a removal).
    """
    t = (raw or "").strip()
    if t in FINAL_REVIEW_OPTIONS:
        k = {"Add all to Always Keep": "ALWAYS_KEEP", "Mark all as not a bot (Keep)": "NOT_A_BOT",
             "Leave all as is": "LEAVE"}[t]
        return {n: k for n in nums}
    res, rest = {}, None
    for part in re.split(r"[;\n]|\.\s", t):
        k = _kind(part)
        if not k:
            continue
        if re.search(r"\b(rest|others?|everything else|remaining|all)\b", part.lower()) and not _nums_in(part):
            rest = k; continue
        for n in _nums_in(part):
            if n in nums:
                res[n] = k
    for n in nums:
        res.setdefault(n, rest or "LEAVE")
    return res


def final_review_mapping(decisions: dict[int, str]) -> dict[int, dict]:
    return {n: dict(FINAL_REVIEW_MAP[k], kind=k) for n, k in decisions.items()}
