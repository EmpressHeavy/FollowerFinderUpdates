"""Always Keep backup/restore helpers.

Local JSON (data/users/{user_key}/always_keep.json) remains the authoritative
working store. Google Drive is an optional mirror via user-Google-drive MCP.

Backup files include timestamp + version. Never overwrite a newer valid list
with an older backup on restore.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    from .storage import load_always_keep, save_always_keep, load_json, save_json, always_keep_path, always_keep_backups_path, validate_user_key, apply_user_permissions
except ImportError:  # pragma: no cover
    from storage import load_always_keep, save_always_keep, load_json, save_json, always_keep_path, always_keep_backups_path, validate_user_key, apply_user_permissions


DRIVE_FOLDER_NAME = "Follower Finder Always Keep Backups"


def _now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def build_backup_payload(root: str | Path | None = None, *, user_key: str) -> dict[str, Any]:
    path = always_keep_path(root, user_key=user_key)
    raw = load_json(path, {"version": 1, "updated_at": None, "accounts": []})
    if isinstance(raw, list):
        raw = {"version": 1, "updated_at": None, "accounts": raw}
    accounts = raw.get("accounts") if isinstance(raw, dict) else []
    updated_at = raw.get("updated_at") if isinstance(raw, dict) else None
    version = int(raw.get("version") or 1) if isinstance(raw, dict) else 1
    return {
        "backup_format_version": 1,
        "source": "always_keep_local_authoritative",
        "user_key": user_key,
        "backed_up_at": datetime.now().astimezone().isoformat(),
        "local_updated_at": updated_at,
        "list_version": version,
        "account_count": len(accounts or []),
        "accounts": accounts or [],
    }


def local_backup_copy(root: str | Path | None = None, *, user_key: str) -> Path:
    """Write a timestamped local backup under data/users/{user_key}/backups/always_keep/."""
    out_dir = always_keep_backups_path(root, user_key=user_key)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = build_backup_payload(root, user_key=user_key)
    out = out_dir / f"always_keep_backup_{_now_stamp()}.json"
    save_json(out, payload)
    apply_user_permissions(user_key, root)
    return out


def is_newer_or_equal(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """True if candidate should NOT be overwritten by an older backup.

    Prefer local_updated_at / backed_up_at ISO timestamps; fall back to list_version.
    """
    def stamp(d: dict[str, Any]) -> str:
        return str(d.get("local_updated_at") or d.get("backed_up_at") or d.get("updated_at") or "")

    cs, cur = stamp(candidate), stamp(current)
    if cs and cur and cs < cur:
        return False  # candidate older
    if cs and cur and cs > cur:
        return True
    # equal stamps or missing — compare version
    try:
        return int(candidate.get("list_version") or candidate.get("version") or 0) >= int(
            current.get("list_version") or current.get("version") or 0
        )
    except (TypeError, ValueError):
        return True


def restore_from_payload(
    payload: dict[str, Any],
    root: str | Path | None = None,
    *,
    user_key: str,
    force: bool = False,
) -> dict[str, Any]:
    """Restore Always Keep from a backup payload without clobbering a newer local list."""
    if payload.get("user_key") not in (None, user_key):
        return {"restored": False, "reason": f"backup belongs to user_key {payload.get('user_key')!r}, not {user_key!r}"}
    current_path = always_keep_path(root, user_key=user_key)
    current_raw = load_json(current_path, {"version": 1, "updated_at": None, "accounts": []})
    if isinstance(current_raw, list):
        current_raw = {"version": 1, "updated_at": None, "accounts": current_raw}
    current_wrap = {
        "local_updated_at": current_raw.get("updated_at"),
        "list_version": current_raw.get("version") or 1,
        "accounts": current_raw.get("accounts") or [],
    }
    if not force and current_wrap["accounts"] and not is_newer_or_equal(payload, current_wrap):
        return {
            "restored": False,
            "reason": "local Always Keep is newer than backup; refusing to overwrite",
            "local_updated_at": current_wrap["local_updated_at"],
            "backup_backed_up_at": payload.get("backed_up_at"),
        }
    accounts = payload.get("accounts") or []
    save_always_keep(list(accounts), root=root, user_key=user_key)
    # stamp updated_at on file
    stamped = load_json(current_path, {})
    if isinstance(stamped, dict):
        stamped["updated_at"] = datetime.now().astimezone().isoformat()
        stamped["version"] = int(payload.get("list_version") or stamped.get("version") or 1)
        stamped["accounts"] = accounts
        save_json(current_path, stamped)
    return {"restored": True, "account_count": len(accounts)}


def drive_backup_filename(user_key: str) -> str:
    key = validate_user_key(user_key)
    return f"always_keep_{key}_{_now_stamp()}.json"


def backup_to_drive_via_mcp(
    *,
    create_file: Callable[..., Any],
    search_files: Callable[..., Any] | None = None,
    folder_name: str = DRIVE_FOLDER_NAME,
    root: str | Path | None = None,
    user_key: str,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """Upload a timestamped Always Keep backup using injected Drive MCP callables.

    Does not overwrite existing Drive files (unique timestamped title).
    If folder resolution fails, uploads to provided parent_id or Drive root.
    """
    payload = build_backup_payload(root, user_key=user_key)
    # Also keep a local copy for audit
    local = local_backup_copy(root, user_key=user_key)
    title = drive_backup_filename(user_key)
    resolved_parent = parent_id
    folder_note = None
    if search_files is not None and parent_id is None:
        try:
            # Best-effort: find existing folder by title
            found = search_files(query=f"title = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder'")
            folder_note = "search attempted"
            # MCP return shapes vary; leave parent unset if ambiguous
            _ = found
        except Exception as exc:  # noqa: BLE001
            folder_note = f"folder search failed: {exc}"
    try:
        kwargs: dict[str, Any] = {
            "title": title,
            "textContent": json.dumps(payload, indent=2, ensure_ascii=False),
            "contentMimeType": "application/json",
            "disableConversionToGoogleType": True,
        }
        if resolved_parent:
            kwargs["parentId"] = resolved_parent
        result = create_file(**kwargs)
        return {
            "ok": True,
            "local_backup": str(local),
            "drive_title": title,
            "drive_folder": folder_name,
            "folder_note": folder_note,
            "drive_result": result,
            "account_count": payload["account_count"],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "local_backup": str(local),
            "error": str(exc),
            "note": "Local backup written; Drive upload failed — verification gap.",
        }
