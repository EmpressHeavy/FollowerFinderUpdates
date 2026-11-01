"""Protection gates applied BEFORE bot-risk classification (Review Mode defaults).

Order (defaults):
1. Accounts the user follows → PROTECTED
2. Always Keep whitelist → PROTECTED
3. Verified → HARD PRE-FILTER (v3.5.13): ANY verified account (blue, gold, gray/government, paid or not,
   or an About page that shows a Verified status such as "Verified since September 2026") is excluded
   BEFORE classification. It is never scored, never REVIEW, never a suspect, in scans or initial setup.
   This gate is not switchable: only unverified followers are ever evaluated.

Policy is configurable per user via ProtectionPolicy; defaults match development Review Mode.
Never invent fields. Missing relationship/verified flags are treated as False (not protected
by that gate) — callers must populate them from list/API when available.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

try:
    from .product_config import ProtectionPolicy
except ImportError:  # pragma: no cover
    from product_config import ProtectionPolicy


@runtime_checkable
class Protectable(Protocol):
    followed_by_me: bool
    always_keep: bool
    verified: bool


@dataclass(frozen=True)
class ProtectionDecision:
    protected: bool
    stage: str  # "protected" | "pass"
    label: str | None
    signals: list[str]
    gate: str | None  # followed | always_keep | verified | None

    def as_stage1_result(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "label": self.label,
            "escalate": False,
            "signals": list(self.signals),
            "protection_gate": self.gate,
            "bot_like": False,
            "inactive": False,
            "candidate": False,
        }


_VERIFIED_FLAG_KEYS = ("verified", "is_verified", "is_blue_verified", "blue_verified", "legacy_verified",
                       "verified_badge", "has_checkmark")
_VERIFIED_TYPE_KEYS = ("verified_type", "verification_type", "badge_type")


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    t = str(v).strip().lower()
    return t not in ("", "0", "false", "no", "none", "null", "not available", "n/a", "unverified")


def _about_shows_verified(about: Any) -> bool:
    if not about:
        return False
    try:
        try:
            from .about_account import about_from_record
        except ImportError:  # pragma: no cover
            from about_account import about_from_record
        a = about_from_record({"about": about})
    except Exception:  # pragma: no cover - defensive
        return False
    return a.get("verified") == "Yes"


def verified_reason(account: Any) -> str | None:
    """Return why an account counts as verified, or None. Any source wins: list checkmark, API flag,
    verified_type (blue/business/government, paid or not), or a Verified status on the About page."""
    get = (lambda k: account.get(k)) if isinstance(account, dict) else (lambda k: getattr(account, k, None))
    raw = get("raw") if isinstance(get("raw"), dict) else {}
    for src in (get, raw.get):
        for k in _VERIFIED_TYPE_KEYS:
            if _truthy(src(k)):
                return f"verified ({str(src(k)).strip().lower()})"
        for k in _VERIFIED_FLAG_KEYS:
            if _truthy(src(k)):
                return "verified"
    for about in (get("about"), get("about_text"), raw.get("about"), raw.get("about_text")):
        if _about_shows_verified(about):
            return "verified (About page shows Verified)"
    if isinstance(account, dict) and _truthy(account.get("about_verified")):
        return "verified (About page shows Verified)"
    if isinstance(account, dict) and account.get("about_verified") == "Yes":
        return "verified (About page shows Verified)"
    return None


def is_verified(account: Any) -> bool:
    return verified_reason(account) is not None


def apply_protections(
    account: Protectable,
    policy: ProtectionPolicy | None = None,
) -> ProtectionDecision | None:
    """Return a ProtectionDecision if a gate fires; else None (continue to Stage 1 scoring)."""
    policy = policy or ProtectionPolicy()

    if policy.protect_accounts_user_follows and bool(getattr(account, "followed_by_me", False)):
        return ProtectionDecision(
            protected=True,
            stage="protected",
            label="PROTECTED / DO NOT REMOVE",
            signals=["You follow this account"],
            gate="followed",
        )
    if policy.protect_always_keep and bool(getattr(account, "always_keep", False)):
        return ProtectionDecision(
            protected=True,
            stage="protected",
            label="ALWAYS KEEP / DO NOT REMOVE",
            signals=["On Always Keep whitelist"],
            gate="always_keep",
        )
    # Hard pre-filter: ignores policy.exclude_verified_from_removal (owner rule, v3.5.13).
    vr = verified_reason(account)
    if vr:
        return ProtectionDecision(
            protected=True,
            stage="protected",
            label="KEEP",
            signals=[f"Verified — excluded before classification ({vr})"],
            gate="verified",
        )
    return None


# --- Execution-time protection recheck (shared; used by removal_runner) — added 2026-10-16 ---
def _norm_handle(h: Any) -> str:
    return str(h or "").strip().lstrip("@").lower()


def _norm_id(x: Any) -> str:
    t = str(x or "").strip().lstrip("'")
    return t if t.isdigit() else ""


def execution_protection_reason(
    fresh: dict[str, Any],
    *,
    always_keep: Any = (),
    protected_handles: Any = (),
    recorded: dict[str, Any] | None = None,
    keepers: Any = (),
    require_interaction: bool = False,
    waive_follow: bool = False,
) -> str | None:
    """Fail-safe recheck right before a removal. Returns a reason (protected -> do NOT remove) or None.

    fresh: just-looked-up data {user_id, handle, i_follow, verified_type|verified}. Unlike the
    pre-classification gates above, MISSING follow or verified data counts as PROTECTED.
    Always Keep / protected handles match by X user ID first, handle as fallback; both the fresh and the
    recorded handle/ID are checked so a renamed account is still matched by ID.
    """
    recorded = recorded or {}
    follow = fresh.get("i_follow", fresh.get("followed_by_me"))
    if follow is None:
        return "follow status unknown (treated as protected)"
    if follow and not waive_follow:
        # Mutuals (you follow them back) are protected. waive_follow is set ONLY for an account the owner
        # explicitly marked on the separate inactive-mutuals card (never automatic).
        return "you follow this account"
    if require_interaction:
        # Recent interactions with the owner, X search both ways (from:owner to:them, from:them to:owner),
        # done by the adapter right before each removal (beta feedback 2026-10-20). Missing = protected.
        ri = fresh.get("recent_interaction")
        if ri is None:
            return "recent-interaction check missing (treated as protected)"
        if ri:
            return "recent interaction with you (X search both ways)"
    if "verified_type" in fresh and fresh.get("verified_type") is not None:
        vt = str(fresh.get("verified_type") or "none").lower()
        if vt not in ("none", "", "false"):
            return f"verified ({vt})"
    elif "verified" in fresh and fresh.get("verified") is not None:
        if fresh.get("verified"):
            return "verified"
    else:
        return "verified status unknown (treated as protected)"
    ids = {i for i in (_norm_id(fresh.get("user_id")), _norm_id(recorded.get("user_id") or recorded.get("x_user_id"))) if i}
    handles = {h for h in (_norm_handle(fresh.get("handle") or fresh.get("username")),
                           _norm_handle(recorded.get("handle") or recorded.get("username"))) if h}
    for e in always_keep or ():
        if isinstance(e, dict):
            eid = _norm_id(e.get("user_id") or e.get("x_user_id") or e.get("id"))
            if eid and eid in ids:
                return "Always Keep"
            if eid and ids:
                continue  # both IDs known and different: match by ID only (handles can be reused)
            if _norm_handle(e.get("username") or e.get("handle")) in handles:
                return "Always Keep"
        elif _norm_id(e) and _norm_id(e) in ids:
            return "Always Keep"
        elif _norm_handle(e) in handles:
            return "Always Keep"
    for e in keepers or ():
        eid = _norm_id(e.get("x_user_id") or e.get("user_id")) if isinstance(e, dict) else _norm_id(e)
        eh = _norm_handle(e.get("handle") or e.get("username")) if isinstance(e, dict) else _norm_handle(e)
        if (eid and eid in ids) or (not (eid and ids) and eh and eh in handles):
            return "in Keepers (Not a bot)"
    for h in protected_handles or ():
        if isinstance(h, dict):
            hid = _norm_id(h.get("user_id"))
            if (hid and hid in ids) or (_norm_handle(h.get("handle") or h.get("username")) in handles):
                return "owner-protected account"
        elif _norm_id(h) and _norm_id(h) in ids:
            return "owner-protected account"
        elif _norm_handle(h) in handles:
            return "owner-protected account"
    return None
