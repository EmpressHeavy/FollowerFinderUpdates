"""Run the offline, read-only follower classification pipeline."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # Works as a module and as `python src/run_classify_pipeline.py`.
    from .build_snapshot import build_snapshot, candidate_from_raw
    from .classify import classify
    from .classify_batch import classify_in_chunks, load_inactivity_config
    from .merge_enrichment import merge_enrichment
    from .metrics import compute_metrics
    from .storage import load_always_keep, save_json, snapshots_path, user_data_root, apply_user_permissions
    from .checkpoints import checkpoint_classify_freeze
    from .product_config import PHASE2_EXECUTION_ENABLED, AUTOMATIC_MODE_ENABLED
    from . import preflight
except ImportError:  # pragma: no cover
    from build_snapshot import build_snapshot, candidate_from_raw
    from classify import classify
    from classify_batch import classify_in_chunks, load_inactivity_config
    from merge_enrichment import merge_enrichment
    from metrics import compute_metrics
    from storage import load_always_keep, save_json, snapshots_path, user_data_root, apply_user_permissions
    from checkpoints import checkpoint_classify_freeze
    from product_config import PHASE2_EXECUTION_ENABLED, AUTOMATIC_MODE_ENABLED
    import preflight


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_raw_arrays(raw_dir: Path) -> list[dict[str, Any]]:
    """Load follower batch JSON arrays from scan_raw (skip progress / non-arrays)."""
    records: list[dict[str, Any]] = []
    paths = sorted(raw_dir.glob("batch_*.json"))
    if not paths:
        # Fallback: any top-level JSON array except known non-batch names.
        skip = {"progress.json", "checkpoints.json"}
        paths = [
            path for path in sorted(raw_dir.glob("*.json"))
            if path.name not in skip
        ]
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, list):
            # progress.json and similar metadata — ignore rather than abort.
            continue
        for index, record in enumerate(value):
            if not isinstance(record, dict):
                raise ValueError(f"{path}[{index}] must be a JSON object")
            records.append(record)
    return records


class EmptyScanError(RuntimeError):
    """Raised when scan_raw holds zero follower records; no snapshot is written."""


def run_pipeline(
    root: str | Path | None = None,
    inactivity_threshold_days: int = 90,
    *,
    user_key: str,
    account_handle: str | None = None,
    operating_mode: str = "review",
    write_checkpoint: bool = True,
    chunk_size: int | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Offline classify only. Phase 2 / Automatic Mode remain disabled by product flags."""
    assert not PHASE2_EXECUTION_ENABLED, "Phase 2 must stay disabled"
    # Automatic Mode may be enabled (2026-10-16); this pipeline is classification-only and never executes.
    root = Path(root) if root is not None else PROJECT_ROOT
    # Per-user isolation (2026-10-16): all scan inputs/outputs live under data/users/{user_key}/; no global fallback.
    user_root = user_data_root(user_key, root)
    preflight.require(user_key, root)  # inside Grok Bot: Google Sheets + Drive must be confirmed before a run
    scan_started_wall = datetime.now().astimezone().isoformat()
    started = time.perf_counter()
    raw_dir = user_root / "scan_raw"
    enrichment_dir = raw_dir / "enrichment"
    raw_followers = _read_raw_arrays(raw_dir)
    # Optional enrichment (counts/age/activity) keyed by username under scan_raw/enrichment/.
    raw_followers = merge_enrichment(raw_followers, enrichment_dir=enrichment_dir)
    if not raw_followers:
        raise EmptyScanError(
            f"No follower records found in {raw_dir} (expected batch_*.json arrays). "
            "Nothing was saved. Bring followers in first (the agent's browser, or "
            "`python -m src.import_x_archive --user <key> <archive.zip>`)."
        )
    snapshots_path(root, user_key=user_key).mkdir(parents=True, exist_ok=True)
    (user_root / "tasks").mkdir(parents=True, exist_ok=True)
    has_enrichment = enrichment_dir.is_dir() and any(enrichment_dir.glob("*.json"))
    keep_entries = load_always_keep(root, user_key=user_key)

    # Chunked, checkpointed classification: every record is classified (no sampling cap);
    # a record that cannot be classified is labelled NOT EVALUATED, never KEEP.
    inactivity_cfg = load_inactivity_config(user_root)
    batch = classify_in_chunks(
        raw_followers,
        keep_entries,
        inactivity_days_threshold=inactivity_threshold_days,
        inactivity_cfg=inactivity_cfg,
        chunk_size=chunk_size or int(inactivity_cfg.get("classify_chunk_size", 1000)),
        progress_path=user_root / "tasks" / "classify_progress.json",
        resume=resume,
    )
    classifications: list[tuple[Any, dict[str, Any]]] = batch["classifications"]

    scan_seconds = time.perf_counter() - started
    mode = "LIST_ONLY_PARTIAL" if not has_enrichment else "READ_ONLY_LIVE_TEST"
    note = (
        "LIST_ONLY_PARTIAL pending enrichment — list scrape only; Stage-1 counts/age "
        "mostly absent so most non-protected/non-verified accounts escalate to Stage 2 "
        "as REVIEW. Not final live results."
        if not has_enrichment
        else "Offline read-only live-test classification snapshot; no removals are enabled."
    )
    scan_completed_wall = datetime.now().astimezone().isoformat()
    snapshot_dir = build_snapshot(
        raw_followers,
        classifications,
        root=root,
        user_key=user_key,
        scan_seconds=scan_seconds,
        inactivity_threshold_days=inactivity_threshold_days,
        always_keep=keep_entries,
        note=note,
        mode=mode,
        operating_mode=operating_mode,
        account_handle=account_handle,
        scan_started=scan_started_wall,
        scan_completed=scan_completed_wall,
    )
    snapshot_id = snapshot_dir.name
    if write_checkpoint:
        checkpoint_classify_freeze(snapshot_id=snapshot_id, root=root, user_key=user_key)
    metrics = compute_metrics(classifications, scan_seconds=scan_seconds)
    try:  # v3.5.4: per-scan counts go into terminator.db next to the scan row (append-only)
        try:
            from . import records as _records
        except ImportError:  # pragma: no cover
            import records as _records
        _records.record_scan_metrics(user_key, snapshot_id, metrics, root=root)
    except Exception as exc:  # records must never block a read-only scan; surfaced in the report
        metrics = {**metrics, "records_metrics_error": type(exc).__name__}
    # Enrichment flag is report-only convenience on meta.
    try:
        meta_path = snapshot_dir / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["enrichment_merged"] = has_enrichment
        save_json(meta_path, meta)
    except Exception:
        pass
    report = {
        "snapshot_id": snapshot_id,
        "mode": mode,
        "removal_enabled": False,
        "created_at": datetime.now().astimezone().isoformat(),
        "scan_started": scan_started_wall,
        "scan_completed": scan_completed_wall,
        "user_key": user_key,
        "account": account_handle,
        "operating_mode": operating_mode,
        "phase2_execution_enabled": PHASE2_EXECUTION_ENABLED,
        "automatic_mode_enabled": AUTOMATIC_MODE_ENABLED,
        "raw_input_dir": str(raw_dir),
        "raw_files": [str(path) for path in sorted(raw_dir.glob("batch_*.json"))],
        "enrichment_dir": str(enrichment_dir),
        "enrichment_merged": has_enrichment,
        "snapshot_dir": str(snapshot_dir),
        "note": note,
        "metrics": metrics,
        "classification_coverage": {
            "total": batch["total"], "classified": batch["classified"],
            "not_evaluated": batch["not_evaluated"], "chunks": batch["chunks"],
            "resumed_chunks": batch["resumed_chunks"],
        },
    }
    report_path = user_root / "last_scan_report.json"
    save_json(report_path, report)
    # The snapshot meta already contains metrics; this separate file is convenient for scripts.
    save_json(snapshot_dir / "metrics.json", metrics)
    # Flag Graveyard returns (previously removed accounts that follow again); they leave the removal flow.
    try:
        from .graveyard_returns import detect_returns
    except ImportError:  # pragma: no cover
        from graveyard_returns import detect_returns
    returns = detect_returns(user_key, snapshot_id, root)
    apply_user_permissions(user_key, root)
    return {"snapshot_dir": snapshot_dir, "report_path": report_path, "metrics": metrics, "graveyard_returns": returns}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify offline follower arrays without removals")
    parser.add_argument("--user", required=True, help="per-user key (data/users/{user}/); required, no global fallback")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="project root")
    parser.add_argument("--inactivity-days", type=int, default=90)
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(args.root, args.inactivity_days, user_key=args.user)
    except EmptyScanError as exc:
        print(f"WARNING: {exc}", file=sys.stderr)
        return 2
    print(f"snapshot: {result['snapshot_dir']}")
    print(f"report: {result['report_path']}")
    print(json.dumps(result["metrics"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
