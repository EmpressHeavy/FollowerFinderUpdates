"""Optional offline path: import followers from the owner's own X account archive ZIP into scan_raw.

    python -m src.import_x_archive --user <user_key> --handle @your_handle path/to/archive.zip

Writes data/users/<user_key>/scan_raw/batch_NNN.json (next free number) in the scan_raw format the scan
reads (see README). By design the package never fetches followers from X itself: normally the agent's
browser brings followers in; this importer is the offline alternative. No X action is taken.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from .x_archive_follower_adapter import load_x_archive_followers
    from .storage import user_data_root, save_json, load_user_config_dict, PROJECT_ROOT, apply_user_permissions
except ImportError:  # pragma: no cover
    from x_archive_follower_adapter import load_x_archive_followers
    from storage import user_data_root, save_json, load_user_config_dict, PROJECT_ROOT, apply_user_permissions


def to_scan_raw(rec: dict[str, Any]) -> dict[str, Any]:
    """Map one normalized archive record to the scan_raw record format."""
    pm = rec.get("public_metrics") or {}
    out = {
        "username": rec.get("handle"),
        "name": rec.get("display_name") or rec.get("handle"),
        "user_id": rec.get("x_user_id"),
        "verified": rec.get("verified"),
        "protected": rec.get("protected"),
        "you_follow_them": rec.get("you_follow_them"),
        "follows_you": rec.get("follows_you"),
        "created_at": rec.get("created_at"),
        "followers_count": pm.get("followers_count"),
        "following_count": pm.get("following_count"),
        "post_count": pm.get("tweet_count") if pm.get("tweet_count") is not None else pm.get("post_count"),
        "source": "X_ACCOUNT_ARCHIVE",
        "source_timestamp": rec.get("source_timestamp"),
    }
    return {k: v for k, v in out.items() if v is not None}


def import_archive(zip_path: str | Path, *, user_key: str, account_handle: str | None = None,
                   root: str | Path | None = None) -> dict[str, Any]:
    if not account_handle:
        cfg = load_user_config_dict(root, user_key=user_key)
        account_handle = cfg.get("owner_x_handle") or cfg.get("x_handle")
    if not account_handle:
        raise ValueError("account handle required (--handle, or owner_x_handle in your config)")
    loaded = load_x_archive_followers(zip_path, account_handle)
    if loaded.get("status") != "OK":
        return {"ok": False, "status": loaded.get("status"), "reason": loaded.get("block_reason") or loaded.get("message"),
                "written": None, "record_count": 0}
    records = [to_scan_raw(r) for r in loaded.get("records") or []]
    records = [r for r in records if r.get("username")]
    if not records:
        return {"ok": False, "status": "EMPTY", "reason": "archive contained no follower records", "written": None,
                "record_count": 0}
    raw_dir = user_data_root(user_key, root) / "scan_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    while (raw_dir / f"batch_{n:03d}.json").exists():
        n += 1
    out = raw_dir / f"batch_{n:03d}.json"
    save_json(out, records)
    apply_user_permissions(user_key, root)
    return {"ok": True, "status": "OK", "written": str(out), "record_count": len(records),
            "schema_note": (loaded.get("provenance") or {}).get("schema_note")}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Import followers from your own X archive ZIP into scan_raw (offline)")
    p.add_argument("--user", required=True, help="your user_key (data/users/<user_key>/)")
    p.add_argument("--handle", help="your X handle (default: owner_x_handle from your config)")
    p.add_argument("--root", type=Path, default=PROJECT_ROOT)
    p.add_argument("archive", type=Path)
    a = p.parse_args(argv)
    try:
        res = import_archive(a.archive, user_key=a.user, account_handle=a.handle, root=a.root)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
