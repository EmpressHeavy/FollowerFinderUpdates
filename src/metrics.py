"""Metrics for an offline classification scan."""
from __future__ import annotations

from typing import Any, Iterable


LABEL_TO_METRIC = {
    "KEEP": "KEEP",
    "REVIEW": "REVIEW",
    "BOT-LIKE / REMOVE CANDIDATE": "BOT-LIKE",
    "INACTIVE / REMOVE CANDIDATE": "INACTIVE",
    "BOT-LIKE + INACTIVE / REMOVE CANDIDATE": "BOT-LIKE + INACTIVE",
}


def _candidate_and_result(item: Any) -> tuple[Any, dict[str, Any]]:
    if isinstance(item, tuple) and len(item) == 2:
        return item[0], item[1]
    if isinstance(item, dict) and isinstance(item.get("result"), dict):
        return item.get("candidate"), item["result"]
    return None, item if isinstance(item, dict) else {}


def compute_metrics(classifications: Iterable[Any], scan_seconds: float = 0.0) -> dict[str, Any]:
    """Compute the stable report fields from (Candidate, classify-result) pairs."""
    entries = list(classifications)
    results = [_candidate_and_result(entry) for entry in entries]
    out: dict[str, Any] = {
        "total_examined": len(results),
        "protected_followed": 0,
        "verified_excluded": 0,
        "always_keep_excluded": 0,
        "stage1_cleared": 0,
        "stage2_sent": 0,
        "KEEP": 0,
        "REVIEW": 0,
        "BOT-LIKE": 0,
        "INACTIVE": 0,
        "BOT-LIKE + INACTIVE": 0,
        "country_unavailable": 0,
        "activity_insufficient": 0,
        "scan_seconds": round(float(scan_seconds), 3),
    }

    for candidate, result in results:
        stage = result.get("stage")
        label = result.get("label")
        if getattr(candidate, "followed_by_me", False) or result.get("label") == "PROTECTED / DO NOT REMOVE":
            out["protected_followed"] += 1
        if getattr(candidate, "verified", False) or result.get("signals") == ["Verified — excluded from removal pool"]:
            out["verified_excluded"] += 1
        if getattr(candidate, "always_keep", False) or result.get("label") == "ALWAYS KEEP / DO NOT REMOVE":
            out["always_keep_excluded"] += 1
        if stage == "stage1_keep":
            out["stage1_cleared"] += 1
        if stage == "stage2":
            out["stage2_sent"] += 1
            if result.get("account_country_unavailable"):
                out["country_unavailable"] += 1
            if result.get("activity_insufficient"):
                out["activity_insufficient"] += 1
        metric_label = LABEL_TO_METRIC.get(label)
        if metric_label:
            out[metric_label] += 1
    return out
