"""Operational checkpoint writing for list/enrichment/classify resume.

A successful checkpoint means: a durable, atomic write of progress after a
completed unit of work (list batch, enrichment batch, or classify freeze),
including enough keys to resume without reprocessing duplicates.

When written:
- after each successful list batch write
- after each successful enrichment batch write
- after classify freeze (snapshot created)

How resume works:
- load checkpoints.json
- skip keys already in seen_usernames / done_usernames / last_batch_index
- continue from cursor; never storm-retry hard-stopped targets

Duplicate/skip prevention: callers pass keys into mark_batch_done / merge seen sets.
Safe no-op helpers exist when no live run is active.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    from .storage import load_checkpoints, save_checkpoints, PROJECT_ROOT
except ImportError:  # pragma: no cover
    from storage import load_checkpoints, save_checkpoints, PROJECT_ROOT


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def empty_checkpoints() -> dict[str, Any]:
    return {
        "version": 2,
        "last_successful_scan_at": None,
        "last_snapshot_id": None,
        "list_scan": {
            "status": "idle",
            "last_batch_index": None,
            "seen_usernames": [],
            "unique_count": 0,
            "updated_at": None,
        },
        "enrichment": {
            "status": "idle",
            "done_usernames": [],
            "last_username": None,
            "updated_at": None,
        },
        "classify_freeze": {
            "status": "idle",
            "last_snapshot_id": None,
            "updated_at": None,
        },
        "notes": (
            "Successful checkpoint = atomic write after completed list/enrichment/classify unit. "
            "Resume skips seen keys. Hard-stop pacing is recorded under pacing when present."
        ),
    }


def ensure_checkpoints(root: str | Path | None = None, *, user_key: str) -> dict[str, Any]:
    """Load or initialize checkpoints (additive merge onto v1 files)."""
    existing = load_checkpoints(root, user_key=user_key)
    base = empty_checkpoints()
    if not existing:
        save_checkpoints(base, root=root, user_key=user_key)
        return base
    # Additive: preserve unknown keys; fill missing sections.
    out = dict(base)
    out.update({k: v for k, v in existing.items() if k not in ("list_scan", "enrichment", "classify_freeze")})
    for section in ("list_scan", "enrichment", "classify_freeze"):
        merged = dict(base[section])
        if isinstance(existing.get(section), dict):
            merged.update(existing[section])
        out[section] = merged
    # Preserve legacy top-level fields
    if existing.get("last_successful_scan_at") is not None:
        out["last_successful_scan_at"] = existing["last_successful_scan_at"]
    if existing.get("last_snapshot_id") is not None:
        out["last_snapshot_id"] = existing["last_snapshot_id"]
    return out


def checkpoint_list_batch(
    *,
    batch_index: int,
    usernames: Iterable[str],
    unique_count: int | None = None,
    root: str | Path | None = None,
    user_key: str,
    pacing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a successful list batch (atomic)."""
    cp = ensure_checkpoints(root, user_key=user_key)
    seen = set(cp["list_scan"].get("seen_usernames") or [])
    for u in usernames:
        if u:
            seen.add(str(u).lstrip("@").lower())
    cp["list_scan"].update({
        "status": "running",
        "last_batch_index": batch_index,
        "seen_usernames": sorted(seen),
        "unique_count": unique_count if unique_count is not None else len(seen),
        "updated_at": _now_iso(),
    })
    if pacing:
        cp["pacing"] = pacing
    save_checkpoints(cp, root=root, user_key=user_key)
    return cp


def checkpoint_enrichment_batch(
    *,
    done_usernames: Iterable[str],
    last_username: str | None = None,
    root: str | Path | None = None,
    user_key: str,
    pacing: dict[str, Any] | None = None,
    status: str = "running",
) -> dict[str, Any]:
    cp = ensure_checkpoints(root, user_key=user_key)
    done = set(cp["enrichment"].get("done_usernames") or [])
    for u in done_usernames:
        if u:
            done.add(str(u).lstrip("@").lower())
    cp["enrichment"].update({
        "status": status,
        "done_usernames": sorted(done),
        "last_username": last_username,
        "updated_at": _now_iso(),
    })
    if pacing:
        cp["pacing"] = pacing
    save_checkpoints(cp, root=root, user_key=user_key)
    return cp


def checkpoint_classify_freeze(
    *,
    snapshot_id: str,
    root: str | Path | None = None,
    user_key: str,
) -> dict[str, Any]:
    cp = ensure_checkpoints(root, user_key=user_key)
    cp["classify_freeze"].update({
        "status": "frozen",
        "last_snapshot_id": snapshot_id,
        "updated_at": _now_iso(),
    })
    cp["last_snapshot_id"] = snapshot_id
    cp["last_successful_scan_at"] = _now_iso()
    save_checkpoints(cp, root=root, user_key=user_key)
    return cp


def usernames_to_skip(root: str | Path | None = None, *, user_key: str, phase: str = "list") -> set[str]:
    cp = ensure_checkpoints(root, user_key=user_key)
    if phase == "enrichment":
        return set(cp.get("enrichment", {}).get("done_usernames") or [])
    return set(cp.get("list_scan", {}).get("seen_usernames") or [])
