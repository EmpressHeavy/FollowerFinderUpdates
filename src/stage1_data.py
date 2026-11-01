"""Stage-1 cheap-field requirements and ingest scaffolding (no live X calls).

Corrected architecture:
  LIST → CHEAP STAGE-1 DATA → STAGE 1 → ONLY ESCALATIONS → EXPENSIVE STAGE 2

When required cheap fields are missing, Stage 1 must skip/defer with a clear
status — never invent counts/age, and never treat missing as "normal KEEP".
"""
from __future__ import annotations

from typing import Any, Mapping

# Fields the current browser follower-list cards typically yield in this project.
LIST_CARD_FIELDS = (
    "username",
    "name",
    "bio",
    "verified",
    "you_follow_them",
    "follow_button_visible",
    "follows_you",
    "protected",
)

# Cheap Stage-1 fields needed for a decisive KEEP vs escalate decision.
REQUIRED_CHEAP_FIELDS = (
    "followers_count",
    "following_count",
)

# At least one of these age/volume signals should be present for a KEEP clear.
OPTIONAL_BUT_USEFUL_CHEAP_FIELDS = (
    "account_age_days",
    "created_at",
    "post_count",
    "has_default_avatar",
    "days_inactive_cheap",
)


def missing_cheap_fields(record: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in REQUIRED_CHEAP_FIELDS:
        if record.get(key) is None:
            missing.append(key)
    # age/volume: need at least one
    if all(record.get(k) is None for k in ("account_age_days", "created_at", "post_count")):
        missing.append("account_age_or_post_count")
    return missing


def stage1_field_status(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return clear status for Stage-1 readiness (no invention)."""
    missing = missing_cheap_fields(record)
    if missing:
        return {
            "stage1_ready": False,
            "status": "STAGE1_DEFER_MISSING_FIELDS",
            "missing_fields": missing,
            "message": (
                "Required cheap Stage-1 fields missing — defer Stage-1 KEEP decision; "
                "escalate/queue for cheap enrichment or richer list/API ingest. "
                "Do not invent values."
            ),
        }
    return {
        "stage1_ready": True,
        "status": "STAGE1_READY",
        "missing_fields": [],
        "message": "Cheap Stage-1 fields present.",
    }


def normalize_list_or_api_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Scaffolding: accept richer list-card / API-shaped records into a common dict.

    Supports:
    - Current list scrape keys
    - X API v2 user objects with public_metrics / created_at / verified
    Does not call the network.
    """
    out = dict(record)
    # API-shaped nesting
    metrics = record.get("public_metrics") if isinstance(record.get("public_metrics"), dict) else {}
    if out.get("followers_count") is None and "followers_count" in metrics:
        out["followers_count"] = metrics.get("followers_count")
    if out.get("following_count") is None and "following_count" in metrics:
        out["following_count"] = metrics.get("following_count")
    if out.get("post_count") is None and metrics.get("tweet_count") is not None:
        out["post_count"] = metrics.get("tweet_count")
    # id / rest_id → user_id (numeric string only when clearly numeric)
    raw_id = out.get("user_id") or out.get("id") or out.get("rest_id")
    if raw_id is not None:
        text = str(raw_id).strip()
        if text.isdigit():
            out["user_id"] = text
            out["user_id_available"] = True
        else:
            # Never put username in user_id
            out["user_id"] = "Not available"
            out["user_id_available"] = False
            if "username" not in out and not text.isdigit():
                # if someone passed handle as id, leave username path alone
                pass
    else:
        out.setdefault("user_id", "Not available")
        out["user_id_available"] = False
    # screen_name alias
    if out.get("username") is None and out.get("screen_name"):
        out["username"] = out["screen_name"]
    status = stage1_field_status(out)
    out["_stage1_status"] = status
    return out
