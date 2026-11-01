"""Build a read-only review snapshot from raw records and classifications."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # Works both as `python -m src...` and direct script execution.
    from .classify import ActivityFingerprint, Candidate
    from .metrics import compute_metrics
    from .storage import save_snapshot, save_json
    from .stage1_data import normalize_list_or_api_record
    from .about_account import about_from_record
except ImportError:  # pragma: no cover
    from classify import ActivityFingerprint, Candidate
    from metrics import compute_metrics
    from storage import save_snapshot, save_json
    from stage1_data import normalize_list_or_api_record
    from about_account import about_from_record


def _now() -> datetime:
    return datetime.now().astimezone()


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _age_days(record: dict[str, Any]) -> int | None:
    value = _first(record, "account_age_days", "age_days")
    parsed = _int(value)
    if parsed is not None:
        return parsed
    joined = _first(record, "joined")
    if isinstance(joined, str) and joined.startswith("age_days="):
        return _int(joined.split("=", 1)[1])
    return None


REL_SRC_API = "api_connection_status"
REL_SRC_FOLLOW_BACK = "browser_follow_back_button"
REL_SRC_FOLLOWING = "browser_following_button"
REL_SRC_NONE = "not_available"


def relationship_evidence(record: dict[str, Any]) -> tuple[bool | None, str]:
    """Return (you_follow_them, source). Precedence: API connection_status >
    explicit followed_by_me field > browser button ('Following' / 'Follow back') > not_available.
    None means unknown; callers keep treating unknown as not-protected (unchanged)."""
    cs = record.get("connection_status")
    if isinstance(cs, (list, tuple)):
        return ("following" in [str(x).lower() for x in cs]), REL_SRC_API
    value = _first(record, "followed_by_me", "you_follow_them")
    if isinstance(value, bool):
        return value, "field_followed_by_me"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"following", "yes", "true", "1"}:
            return True, "field_followed_by_me"
        if normalized in {"follow", "follow back", "no", "false", "0", "unfollow"}:
            return False, "field_followed_by_me"
    button = _first(record, "button", "follow_button_visible", "follow_button", "following_state")
    if isinstance(button, str):
        normalized = button.strip().lower()
        if normalized == "following":
            return True, REL_SRC_FOLLOWING
        if normalized == "follow back":
            return False, REL_SRC_FOLLOW_BACK
        if normalized == "follow":
            return False, "browser_follow_button"
    return None, REL_SRC_NONE


def relationship_display(followed: bool | None, source: str) -> str:
    labels = {
        REL_SRC_API: "API connection_status",
        REL_SRC_FOLLOW_BACK: "browser: Follow back button",
        REL_SRC_FOLLOWING: "browser: Following button",
        "browser_follow_button": "browser: Follow button",
        "field_followed_by_me": "followed_by_me field",
    }
    if followed is None or source == REL_SRC_NONE:
        return "Not available"
    return f"{'Yes' if followed else 'No'} ({labels.get(source, source)})"


def _relationship_bool(record: dict[str, Any]) -> bool:
    """Interpret 'you follow them' (not 'they follow you'). Unknown -> False (unchanged)."""
    return bool(relationship_evidence(record)[0])


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def activity_from_raw(record: dict[str, Any]) -> ActivityFingerprint:
    value = _first(record, "activity", "activity_30d", "last_30_days", "activity_fingerprint")
    if not isinstance(value, dict):
        value = record
    return ActivityFingerprint(
        original=_int(_first(value, "original", "original_posts")),
        replies=_int(_first(value, "replies", "reply")),
        quotes=_int(_first(value, "quotes", "quote_posts")),
        reposts=_int(_first(value, "reposts", "retweets")),
        sample_size=_int(_first(value, "sample_size", "sampled_count", "n")),
        window_days=_int(_first(value, "window_days")) or 30,
        insufficient=bool(_bool(_first(value, "insufficient"))),
    )


def _stable_user_id(record: dict[str, Any]) -> str:
    """Numeric X user_id only; never put username in user_id. Else 'Not available'."""
    raw = _first(record, "user_id", "id", "rest_id")
    if raw is None:
        return "Not available"
    text = str(raw).strip()
    if text.isdigit():
        return text
    # Non-numeric (e.g. handle mistakenly stored as id) → Not available
    return "Not available"


def candidate_from_raw(record: dict[str, Any], always_keep: Iterable[Any] = ()) -> Candidate:
    """Map common exported/X list fields into the classifier's Candidate."""
    record = normalize_list_or_api_record(record)
    username = _first(record, "username", "screen_name", "handle")
    if username is None and _stable_user_id(record) == "Not available":
        raise ValueError("raw follower record needs username/screen_name/handle or numeric user_id")
    user_id = _stable_user_id(record)
    username = str(username if username is not None else "unknown")
    name = str(_first(record, "name", "display_name") or username)

    keep_entries = list(always_keep)
    identities = {user_id, username, username.lstrip("@")}
    keep = False
    for entry in keep_entries:
        if isinstance(entry, str):
            values = {entry, entry.lstrip("@")}
        elif isinstance(entry, dict):
            values = {
                str(entry.get(k)).lstrip("@")
                for k in ("user_id", "id", "username", "screen_name", "handle")
                if entry.get(k) is not None
            }
        else:
            values = set()
        if values & identities:
            keep = True
            break

    account_country = _first(record, "account_country", "country", "account_based_in")
    country_unavailable = bool(_bool(_first(record, "account_country_unavailable", "country_unavailable")))
    deep_inspected = bool(_bool(_first(record, "deep_inspected", "deep_inspection_complete")))
    # Count/age fields stay None until enrichment supplies them (list-only scrapes omit them).
    return Candidate(
        user_id=user_id,
        username=username.lstrip("@"),
        name=name,
        verified=bool(_bool(_first(record, "verified", "is_verified"))),
        followed_by_me=_relationship_bool(record),
        follow_relationship_source=relationship_evidence(record)[1],
        always_keep=keep or bool(_bool(_first(record, "always_keep"))),
        protected=bool(_bool(_first(record, "protected", "is_protected"))),
        created_at=_first(record, "created_at", "created"),
        account_age_days=_age_days(record),
        followers_count=_int(_first(record, "followers_count", "followers")),
        following_count=_int(_first(record, "following_count", "following")),
        post_count=_int(_first(record, "post_count", "posts", "statuses_count")),
        bio=_first(record, "bio", "description"),
        profile_image_url=_first(record, "profile_image_url", "profile_image", "avatar_url", "image_url"),
        has_default_avatar=_bool(_first(record, "has_default_avatar", "default_avatar")),
        profile_complete=_bool(_first(record, "profile_complete")),
        bio_location=_first(record, "bio_location", "location"),
        days_inactive_cheap=_int(_first(record, "days_inactive_cheap", "cheap_days_inactive")),
        last_activity_at=_first(record, "last_activity_at", "last_activity"),
        account_country=str(account_country) if account_country is not None else None,
        account_country_unavailable=country_unavailable,
        about=about_from_record(record),
        days_inactive=_int(_first(record, "days_inactive")),
        activity=activity_from_raw(record),
        deep_inspected=deep_inspected,
        raw=dict(record),
    )


def _candidate_result(item: Any, raw: dict[str, Any] | None, always_keep: Iterable[Any]) -> tuple[Candidate, dict[str, Any]]:
    if isinstance(item, tuple) and len(item) == 2:
        candidate, result = item
        if isinstance(candidate, Candidate) and isinstance(result, dict):
            return candidate, result
    if isinstance(item, dict):
        candidate = item.get("candidate") or item.get("_candidate")
        result = item.get("result") or item.get("classification")
        if isinstance(candidate, Candidate) and isinstance(result, dict):
            return candidate, result
    if raw is None:
        raise ValueError("each classification needs a Candidate or matching raw record")
    return candidate_from_raw(raw, always_keep), item if isinstance(item, dict) else {}


def short_label(label: str | None) -> str:
    """'BOT-LIKE / REMOVE CANDIDATE' -> 'BOT-LIKE', 'PROTECTED / DO NOT REMOVE' -> 'PROTECTED/KEEP', etc."""
    lab = (label or "").upper().strip()
    if lab.startswith("PROTECTED"):
        return "PROTECTED/KEEP"
    if lab.startswith("BOT-LIKE + INACTIVE"):
        return "BOT-LIKE + INACTIVE"
    return lab.split(" / ")[0].strip() or "REVIEW"


def _ui_candidate(candidate: Candidate, result: dict[str, Any], inactivity_threshold_days: int = 90) -> dict[str, Any]:
    label = result.get("label") or "REVIEW"
    if label == "BOT-LIKE / REMOVE CANDIDATE":
        flag = "bot"
    elif label == "INACTIVE / REMOVE CANDIDATE":
        flag = "inactive"
    elif label == "BOT-LIKE + INACTIVE / REMOVE CANDIDATE":
        flag = "both"
    else:
        flag = "review"
    activity = result.get("activity_percents") or candidate.activity.as_percents()
    joined = candidate.created_at or (
        f"age_days={candidate.account_age_days}" if candidate.account_age_days is not None else "Not available"
    )
    days = result.get("days_inactive", "Not available")
    if not isinstance(days, (int, float)):
        days = None
    following = candidate.following_count
    followers = candidate.followers_count
    ratio = (followers / following) if followers is not None and following else None
    return {
        "user_id": candidate.user_id if str(candidate.user_id).isdigit() else "Not available",
        "username": candidate.username,
        "handle_at_scan": candidate.username,
        "display_name_at_scan": candidate.name,
        "name": candidate.name,
        "profile_image_url": candidate.profile_image_url,
        "account_country": result.get("account_country_display") or candidate.account_country or "Not available",
        "joined": joined,
        # v3.5.8 About this account (every value "Not available" unless the About page was read)
        **{(k if k.startswith("about_") else f"about_{k}"): v for k, v in (result.get("about") or about_from_record({"about": candidate.about})).items()},
        "followers": followers,
        "following": following,
        "ratio": round(ratio, 6) if ratio is not None else None,
        "post_count": candidate.post_count,
        "last_activity": result.get("last_activity") or candidate.last_activity_at or "Not available",
        "days_inactive": days,
        "activity_30d": activity,
        "flag": flag,
        "label": label,
        # Fields the card builder / review sheet route on (fresh-install gap fix 2026-10-16).
        "label_short": short_label(label),
        "verified_type": (candidate.raw.get("verified_type") or ("verified" if candidate.verified else "none")),
        "i_follow": bool(candidate.followed_by_me),
        "signals": result.get("signals", []),
        "selected": False,
        "verified": candidate.verified,
        "followed_by_me": candidate.followed_by_me,
        "follow_relationship_source": candidate.follow_relationship_source,
        "you_follow": relationship_display(
            candidate.followed_by_me if candidate.follow_relationship_source != REL_SRC_NONE else None,
            candidate.follow_relationship_source,
        ),
        "always_keep": candidate.always_keep,
        "stage": result.get("stage"),
        "deep_inspected": candidate.deep_inspected,
        # Beta-feedback fields (2026-10-20): card eligibility after the person check (b)/(c), impersonation flag,
        # and inactive-mutual routing (accounts you follow are never auto-removed; they get their own small card).
        "removal_card_eligible": bool(result.get("removal_card_eligible", result.get("candidate", False))),
        "impersonation_flag": bool(result.get("impersonation_flag")),
        "person_check": result.get("person_check"),
        **_mutual_inactivity(candidate, inactivity_threshold_days),
    }


def _mutual_inactivity(candidate: Candidate, threshold: int) -> dict[str, Any]:
    if not candidate.followed_by_me:
        return {"inactive_mutual": False}
    try:
        try:
            from .classify import inactivity_evidence
        except ImportError:  # pragma: no cover
            from classify import inactivity_evidence
        ev = inactivity_evidence(candidate, threshold)
    except Exception:  # noqa: BLE001 - never invent; unknown = not flagged
        return {"inactive_mutual": False}
    return {"inactive_mutual": bool(ev["strong"]), "mutual_days_inactive": ev["days"]}


def _snapshot_id() -> str:
    return "snap_readonly_" + _now().strftime("%Y%m%d_%H%M%S")


def build_snapshot(
    raw_followers: Sequence[dict[str, Any]],
    classifications: Sequence[Any],
    *,
    user_key: str,
    root: str | Path | None = None,
    snapshot_id: str | None = None,
    scan_seconds: float = 0.0,
    inactivity_threshold_days: int = 90,
    always_keep: Iterable[Any] = (),
    note: str = "Offline read-only live-test classification snapshot; no removals are enabled.",
    mode: str = "READ_ONLY_LIVE_TEST",
    operating_mode: str = "review",
    account_handle: str | None = None,
    scan_started: str | None = None,
    scan_completed: str | None = None,
    write_review_index: bool = True,
) -> Path:
    """Write candidates.json, meta.json, selections.json, optional review_index.json (v3.5.5: no HTML/Word files;
    review happens only in chat cards).

    New snapshots get hardened schema fields. Do NOT renumber or rewrite classifications
    on existing interim snaps — callers must pass a new snapshot_id for new freezes.
    """
    snapshot_id = snapshot_id or _snapshot_id()
    completed = scan_completed or _now().isoformat()
    started = scan_started or completed
    entries: list[tuple[Candidate, dict[str, Any]]] = []
    keep_entries = list(always_keep)
    for index, item in enumerate(classifications):
        raw = raw_followers[index] if index < len(raw_followers) else None
        entries.append(_candidate_result(item, raw, keep_entries))
    candidates = [_ui_candidate(candidate, result, inactivity_threshold_days) for candidate, result in entries]
    # Permanent review numbers (1-based) assigned at freeze time; immutable thereafter.
    for i, row in enumerate(candidates, start=1):
        row["review_number"] = i
        row.setdefault("permanent_review_number", i)
    metrics = compute_metrics(entries, scan_seconds=scan_seconds)
    meta = {
        "snapshot_id": snapshot_id,
        "mode": mode,
        "operating_mode": operating_mode,
        "removal_enabled": False,
        "created_at": _now().isoformat(),
        "scan_started": started,
        "scan_completed": completed,
        "account": account_handle,  # per-user; None until configured — never invent
        "inactivity_threshold_days": inactivity_threshold_days,
        "raw_record_count": len(raw_followers),
        "candidate_count": len(candidates),
        "note": note,
        "metrics": metrics,
        "schema_version": 2,
    }
    selections = {
        "snapshot_id": snapshot_id,
        "selected": [],
        "always_keep_marked": [],
        "removal_enabled": False,
    }
    directory = save_snapshot(snapshot_id, candidates, meta, selections, root=root, user_key=user_key)
    if write_review_index:
        review_index = {
            "snapshot_id": snapshot_id,
            "immutable": True,
            "scan_started": started,
            "scan_completed": completed,
            "operating_mode": operating_mode,
            "accounts": [
                {
                    "review_number": row["review_number"],
                    "permanent_review_number": row["permanent_review_number"],
                    "user_id": row.get("user_id") or "Not available",
                    "username": row.get("username"),
                    "display_name_at_scan": row.get("display_name_at_scan") or row.get("name"),
                    "handle_at_scan": row.get("handle_at_scan") or row.get("username"),
                    "label": row.get("label"),
                    "profile_url": f"https://x.com/{row.get('username')}" if row.get("username") else None,
                }
                for row in candidates
            ],
        }
        save_json(directory / "review_index.json", review_index)
    return directory


if __name__ == "__main__":
    print("This module provides build_snapshot(); use run_classify_pipeline.py to run a scan.")
