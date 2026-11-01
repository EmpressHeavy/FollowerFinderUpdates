"""Multi-user product configuration scaffolding.

CORE PRODUCT BEHAVIOR lives in code (protections order, pipeline stages, auth rules).
PER-USER CONFIG is loaded from JSON under config/ or data/users/{user_key}/config.json.
PER-USER PRIVATE DATA stays under data/users/{user_key}/ only (no shared/global fallback).

Automatic Mode is ENABLED and is the DEFAULT operating mode (owner authorization, voice call
2026-10-16 4:01 PM PT). Users can switch to Manual (review) anytime; see src/automatic_policy.py.
This supersedes the 2026-10-14 plan (manual-only for the alpha owner, opt-in automatic for marketplace).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any

try:
    from .branding import (
        BOT_DISPLAY_NAME,
        PRODUCT_DISPLAY_NAME,
        PRODUCT_NAME,
        PRODUCT_SLUG,
    )
    from .storage import PROJECT_ROOT, load_json, save_json, validate_user_key as _validate_user_key
except ImportError:  # pragma: no cover
    from branding import (
        BOT_DISPLAY_NAME,
        PRODUCT_DISPLAY_NAME,
        PRODUCT_NAME,
        PRODUCT_SLUG,
    )
    from storage import PROJECT_ROOT, load_json, save_json, validate_user_key as _validate_user_key


class OperatingMode(str, Enum):
    REVIEW = "review"
    AUTOMATIC = "automatic"


# Hard product flags — not per-user toggles that silently enable destruction.
PHASE2_EXECUTION_ENABLED = False
AUTOMATIC_MODE_ENABLED = True  # 2026-10-16 4:01 PM PT owner authorization; default mode (see automatic_policy.py)
DEFAULT_OPERATING_MODE = "automatic"
# Candidate card UI: product-wide default for ALL users (owner, 2026-10-16 4:49 PM PT)
CARD_SELECT_ALL_LABEL = "Mark all for termination"  # 2026-10-16 4:52 PM PT (was "Select all — mark for termination")
APPROVAL_CHECK_NOTE = "Heads up: this is just a Grok Bot action check before I run a step. It has nothing to do with X, and it's not a flag on your account."  # posted alongside any Grok Bot approval/safety-check card (2026-10-16 4:55 PM PT, all users); card behavior unchanged
CARD_REVIEW_NOTE = "Unmarked accounts will be kept in review."  # shown in each card prompt above the options
CARD_ALLOW_FREE_TEXT = False  # v3.5.7: picks come only from the round card (no typed-number replies)

# Review sheet routing (2026-10-15). Verified/protected accounts go to KEEPERS, never In review.
# Optional setup toggle, default hide. A user's own config may force it on (hide_verified_forced: true).
HIDE_VERIFIED_FROM_REVIEW_QUEUE_DEFAULT = True


@dataclass
class ProtectionPolicy:
    """Configurable protection gates (Review Mode defaults match development policy)."""

    protect_accounts_user_follows: bool = True
    exclude_verified_from_removal: bool = True  # v3.5.13: ignored; verified is always a hard pre-filter
    protect_always_keep: bool = True
    # Future public users may tune watched countries; alone never = bot.
    watched_countries: list[str] = field(
        default_factory=lambda: ["nigeria", "india", "bangladesh", "china"]
    )
    inactivity_days_threshold: int = 90
    # Country is a configurable signal weight, never decisive alone.
    country_signal_weight: int = 1


@dataclass
class UserConfig:
    """Per-user settings. Never copy one user's private data into product rules."""

    user_key: str
    x_handle: str | None = None
    x_user_id: str | None = None  # numeric string when known; else None
    display_name: str | None = None
    operating_mode: OperatingMode = OperatingMode.AUTOMATIC  # default since 2026-10-16
    first_run_direction: str = "AUTOMATIC_CLEANUP"  # or REVIEW_FIRST if the user switches to Manual
    automatic_opt_in: bool = True  # default on; user can switch to Manual anytime
    protection: ProtectionPolicy = field(default_factory=ProtectionPolicy)
    data_root_relative: str | None = None  # e.g. data/users/{user_key}
    notes: str = ""
    is_test_fixture: bool = False
    # Setup toggle: keep verified (blue/business/government) accounts out of In review.
    HIDE_VERIFIED_FROM_REVIEW_QUEUE: bool = HIDE_VERIFIED_FROM_REVIEW_QUEUE_DEFAULT
    # Per-user values (2026-10-16 isolation fix); live only in that user's own config.
    owner_x_handle: str | None = None  # handle recorded in this user's history entries
    protected_handles: list[str] = field(default_factory=list)  # private never-remove handles
    hide_verified_forced: bool = False  # hard rule: verified never in In review for this user

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["operating_mode"] = self.operating_mode.value
        d["product_display_name"] = PRODUCT_DISPLAY_NAME
        d["product_name"] = PRODUCT_NAME  # alias
        d["bot_display_name"] = BOT_DISPLAY_NAME
        d["product_slug"] = PRODUCT_SLUG
        d["phase2_execution_enabled"] = PHASE2_EXECUTION_ENABLED
        d["automatic_mode_enabled_globally"] = AUTOMATIC_MODE_ENABLED
        return d


def default_user_config(user_key: str = "default") -> UserConfig:
    return UserConfig(
        user_key=user_key,
        operating_mode=OperatingMode.AUTOMATIC,
        first_run_direction="AUTOMATIC_CLEANUP",
        automatic_opt_in=True,
        protection=ProtectionPolicy(),
        data_root_relative=f"data/users/{user_key}",
        notes="Default Automatic Mode config (since 2026-10-16). Card answer = authorization for eligible Likely bot accounts only; switch to Manual anytime.",
    )


def load_user_config(path: str | Path) -> UserConfig:
    raw = load_json(path, {})
    if not isinstance(raw, dict):
        raise ValueError(f"config must be a JSON object: {path}")
    prot_raw = raw.get("protection") or {}
    protection = ProtectionPolicy(
        protect_accounts_user_follows=bool(prot_raw.get("protect_accounts_user_follows", True)),
        exclude_verified_from_removal=bool(prot_raw.get("exclude_verified_from_removal", True)),
        protect_always_keep=bool(prot_raw.get("protect_always_keep", True)),
        watched_countries=list(prot_raw.get("watched_countries") or ProtectionPolicy().watched_countries),
        inactivity_days_threshold=int(prot_raw.get("inactivity_days_threshold") or 90),
        country_signal_weight=int(prot_raw.get("country_signal_weight") or 1),
    )
    mode_raw = str(raw.get("operating_mode") or DEFAULT_OPERATING_MODE).lower()
    mode = OperatingMode.AUTOMATIC if mode_raw == "automatic" else OperatingMode.REVIEW
    # Global kill-switch: never honor automatic even if config asks.
    if mode == OperatingMode.AUTOMATIC and not AUTOMATIC_MODE_ENABLED:
        mode = OperatingMode.REVIEW
    return UserConfig(
        user_key=str(raw.get("user_key") or Path(path).stem),
        x_handle=raw.get("x_handle"),
        x_user_id=raw.get("x_user_id"),
        display_name=raw.get("display_name"),
        operating_mode=mode,
        first_run_direction=str(raw.get("first_run_direction") or ("AUTOMATIC_CLEANUP" if mode == OperatingMode.AUTOMATIC else "REVIEW_FIRST")),
        automatic_opt_in=bool(raw.get("automatic_opt_in", mode == OperatingMode.AUTOMATIC)),
        protection=protection,
        data_root_relative=raw.get("data_root_relative"),
        notes=str(raw.get("notes") or ""),
        is_test_fixture=bool(raw.get("is_test_fixture", False)),
        HIDE_VERIFIED_FROM_REVIEW_QUEUE=bool(
            raw.get("HIDE_VERIFIED_FROM_REVIEW_QUEUE", HIDE_VERIFIED_FROM_REVIEW_QUEUE_DEFAULT)
        ),
        owner_x_handle=raw.get("owner_x_handle") or raw.get("x_handle"),
        protected_handles=[str(h) for h in (raw.get("protected_handles") or [])],
        hide_verified_forced=bool(raw.get("hide_verified_forced", False)),
    )


def save_user_config(cfg: UserConfig, path: str | Path) -> Path:
    return save_json(path, cfg.to_dict())


def resolve_user_data_root(cfg: UserConfig, root: str | Path | None = None) -> Path:
    """Return the per-user data directory: always data/users/{user_key}/.

    Isolation fix 2026-10-16: the legacy shared ``data/`` fallback for test fixtures is removed.
    A config whose data_root_relative points anywhere else is rejected (no global fallback).
    """
    base = Path(root) if root is not None else PROJECT_ROOT
    expected = f"data/users/{_validate_user_key(cfg.user_key)}"
    if cfg.data_root_relative and cfg.data_root_relative.rstrip("/") != expected:
        raise ValueError(f"data_root_relative must be {expected!r} (got {cfg.data_root_relative!r})")
    return base / expected


def assert_safe_operating_mode(cfg: UserConfig) -> OperatingMode:
    """Universal rule helper: destructive paths stay off unless flags + auth allow."""
    if cfg.operating_mode == OperatingMode.AUTOMATIC:
        if not AUTOMATIC_MODE_ENABLED or not cfg.automatic_opt_in:
            return OperatingMode.REVIEW
    return cfg.operating_mode


def example_configs_dir(root: str | Path | None = None) -> Path:
    return (Path(root) if root is not None else PROJECT_ROOT) / "config"


def hide_verified_from_review_queue(cfg: UserConfig) -> bool:
    """Effective setting. Forced True when this user's own config sets hide_verified_forced."""
    if cfg.hide_verified_forced:
        return True
    return bool(cfg.HIDE_VERIFIED_FROM_REVIEW_QUEUE)


# --- Prior-cleanup setup step + first-scan window (product-wide, all users; added 2026-10-16 ~5:32 PM PT) ---
PRIOR_CLEANUP_QUESTION = ("Have you ever removed bots from your account before? If so, roughly when was your last bot "
                          "cleanup (and with what tool)? Do you remember about how many followers you had then?")
PRIOR_CLEANUP_SETUP_FIELDS = ("last_purge_date", "last_purge_tool", "last_purge_follower_count",
                              "first_scan_window_estimate", "scan_mode")
SCAN_MODES = ("backlog", "maintenance")
# Optional and skippable (owner 2026-10-16 ~5:32 PM PT follow-up)
PRIOR_CLEANUP_OPTIONAL = True
PRIOR_CLEANUP_SKIP_ANSWER = "Skip / never done one"
LARGE_ACCOUNT_FOLLOWERS = 100_000
DEFAULT_SCAN_BATCH = 80


def first_scan_window(current_followers, last_purge_follower_count=None, last_purge_date=None,
                      growth_per_day=None, today=None, ever_cleaned=None, skipped=False):
    """Estimate the first-scan window. X has no follow dates, but the followers list is newest first.

    Returns {"method", "estimate" (int or None), "batch", "scan_mode": "backlog", "stop_rule"}.
    """
    import datetime as _dt
    stop = "user says stop, or we reach accounts from the earlier cleanup; then switch to maintenance"
    out = {"batch": DEFAULT_SCAN_BATCH, "scan_mode": "backlog", "stop_rule": stop}
    if skipped or ever_cleaned is False or (current_followers or 0) >= LARGE_ACCOUNT_FOLLOWERS:
        return {**out, "method": "recent batches, working backward with pacing", "estimate": None}
    if current_followers is not None and last_purge_follower_count is not None:
        return {**out, "method": "count difference", "estimate": max(0, current_followers - last_purge_follower_count)}
    if last_purge_date and growth_per_day:
        d = _dt.date.fromisoformat(str(last_purge_date)[:10])
        days = ((today or _dt.date.today()) - d).days
        return {**out, "method": "measured follower growth x days since cleanup", "estimate": max(0, round(days * growth_per_day))}
    return {**out, "method": "newest-first batches until stop or earlier-cleanup accounts", "estimate": None}


def next_scan_mode(backlog_caught_up: bool) -> str:
    """After the backlog is caught up, scan only new arrivals since the last session."""
    return "maintenance" if backlog_caught_up else "backlog"
