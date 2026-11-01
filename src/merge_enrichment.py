"""Merge optional Stage-1/2 enrichment records into list-only follower JSON."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ENRICHMENT_KEYS = (
    "followers_count",
    "following_count",
    "post_count",
    "account_age_days",
    "created_at",
    "has_default_avatar",
    "profile_complete",
    "days_inactive_cheap",
    "last_activity_at",
    "account_country",
    "account_country_unavailable",
    "days_inactive",
    "activity",
    "activity_fingerprint",
    "activity_30d",
    "deep_inspected",
    "bio",
    "protected",
    "profile_image_url",
    "bio_location",
    # v3.5.8 About this account
    "about",
    "about_text",
    "about_read",
    "about_date_joined",
    "about_account_based_in",
    "about_verified",
    "about_id_verified",
    "about_username_changes",
    "about_connected_via",
)


def _username_key(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lstrip("@")
    return text.lower() if text else None


def _age_from_created_at(created_at: Any, *, now: datetime | None = None) -> int | None:
    if not isinstance(created_at, str) or not created_at.strip():
        return None
    text = created_at.strip()
    parsed: datetime | None = None
    try:
        if text.endswith("Z"):
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - parsed.astimezone(timezone.utc)).total_seconds() // 86400))


def _load_enrichment_file(path: Path) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    out: dict[str, dict[str, Any]] = {}

    def add(username: Any, payload: dict[str, Any]) -> None:
        key = _username_key(username)
        if not key or not isinstance(payload, dict):
            return
        merged = dict(out.get(key) or {})
        merged.update(payload)
        if "username" not in merged and username is not None:
            merged["username"] = str(username).lstrip("@")
        out[key] = merged

    if isinstance(value, dict):
        # Single-user object: {"username": "...", "followers_count": ...}
        if _username_key(value.get("username") or value.get("screen_name") or value.get("handle")) and any(
            k in value for k in ENRICHMENT_KEYS
        ):
            add(value.get("username") or value.get("screen_name") or value.get("handle"), value)
            return out
        # Mapping keyed by username
        for username, payload in value.items():
            if isinstance(payload, dict):
                add(payload.get("username") or username, payload)
        return out

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                add(item.get("username") or item.get("screen_name") or item.get("handle"), item)
    return out


def load_enrichment_index(enrichment_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Load all enrichment JSON files under enrichment_dir, keyed by lowercase username."""
    directory = Path(enrichment_dir)
    if not directory.is_dir():
        return {}
    index: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        for key, payload in _load_enrichment_file(path).items():
            merged = dict(index.get(key) or {})
            merged.update(payload)
            index[key] = merged
    return index


def apply_enrichment_to_record(record: dict[str, Any], enrichment: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow-copied record with enrichment fields filled in."""
    out = dict(record)
    for key in ENRICHMENT_KEYS:
        if key not in enrichment:
            continue
        value = enrichment[key]
        if value is None and key in out:
            continue
        out[key] = value
    # Prefer explicit account_age_days; else derive from created_at when still unknown.
    if out.get("account_age_days") is None and out.get("created_at") is not None:
        age = _age_from_created_at(out.get("created_at"))
        if age is not None:
            out["account_age_days"] = age
    if "activity_fingerprint" in enrichment and "activity" not in enrichment:
        out["activity"] = enrichment["activity_fingerprint"]
    return out


def merge_enrichment(
    records: list[dict[str, Any]],
    enrichment_dir: str | Path | None = None,
    enrichment_index: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Merge enrichment into each raw follower record (by username)."""
    index = enrichment_index if enrichment_index is not None else {}
    if enrichment_dir is not None:
        loaded = load_enrichment_index(enrichment_dir)
        index = {**loaded, **index}
    if not index:
        return [dict(record) for record in records]
    merged: list[dict[str, Any]] = []
    for record in records:
        key = _username_key(record.get("username") or record.get("screen_name") or record.get("handle"))
        if key and key in index:
            merged.append(apply_enrichment_to_record(record, index[key]))
        else:
            merged.append(dict(record))
    return merged
