"""Small JSON storage helpers for the offline follower-cleaner pipeline."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _root(root: str | Path | None = None) -> Path:
    return Path(root) if root is not None else PROJECT_ROOT


# Per-user isolation (2026-10-16 ~5:50 PM PT): every user's private data lives under
# data/users/{user_key}/ (same pattern as default_user_config's data_root_relative).
# There is NO global fallback: a missing/invalid user_key raises.
_USER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def validate_user_key(user_key: Any) -> str:
    if not isinstance(user_key, str) or not _USER_KEY_RE.match(user_key) or ".." in user_key:
        raise ValueError(f"a valid per-user user_key is required (got {user_key!r}); no global fallback")
    return user_key


def user_data_relative(user_key: str) -> str:
    return f"data/users/{validate_user_key(user_key)}"


def user_data_root(user_key: str, root: str | Path | None = None) -> Path:
    """data/users/{user_key}/ under the project root. Raises without a valid user_key."""
    return _root(root) / user_data_relative(user_key)


def _atomic_json_write(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
        os.chmod(path, mode_for(path))
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


# File permissions (owner decision 2026-10-16 6:52 PM PT), always set explicitly, never left to umask:
# - every folder under data/users/<key>/                          0755 (others may list/read, not change)
# - journal.md, reports/* (csv)                                     0644 (readable reports)
# - all other files = main data (config, always_keep, backups, checkpoints, last_scan_report, scan_raw,
#   snapshots' candidates/meta/selections/review_index/metrics/chat_numbering, history.jsonl, mode.json,
#   removal state)                                                 0600 (owner only)
DIR_MODE = 0o755
DATA_FILE_MODE = 0o600
READABLE_FILE_MODE = 0o644


def mode_for(path: str | Path) -> int:
    p = Path(path)
    if p.name == "journal.md" or p.parent.name == "reports":
        return READABLE_FILE_MODE
    return DATA_FILE_MODE


def apply_user_permissions(user_key: str, root: str | Path | None = None) -> Path:
    """Set the documented modes on this user's whole tree (folders 0755, journal/reports 0644, data 0600)."""
    base = user_data_root(user_key, root)
    if not base.exists():
        return base
    os.chmod(base, DIR_MODE)
    for d, dirs, files in os.walk(base):
        for x in dirs:
            os.chmod(os.path.join(d, x), DIR_MODE)
        for x in files:
            fp = os.path.join(d, x)
            if not os.path.islink(fp):
                os.chmod(fp, mode_for(fp))
    return base


def load_json(path: str | Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: str | Path, value: Any) -> Path:
    return _atomic_json_write(Path(path), value)


def always_keep_path(root: str | Path | None = None, *, user_key: str) -> Path:
    return user_data_root(user_key, root) / "always_keep.json"


def load_always_keep(root: str | Path | None = None, *, user_key: str) -> list[Any]:
    """Return the configured whitelist entries; an absent/empty file means []."""
    value = load_json(always_keep_path(root, user_key=user_key), {})
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        accounts = value.get("accounts", [])
        return accounts if isinstance(accounts, list) else []
    raise ValueError("always_keep.json must contain an array or an object with accounts[]")


def save_always_keep(entries: list[Any], root: str | Path | None = None, *, user_key: str) -> Path:
    p = save_json(always_keep_path(root, user_key=user_key), {
        "version": 1,
        "updated_at": None,
        "accounts": entries,
    })
    _mirror(lambda r: r.sync_allowlist, user_key, entries, root=root)  # v3.5.0 records allowlist (append-only)
    return p


def checkpoints_path(root: str | Path | None = None, *, user_key: str) -> Path:
    return user_data_root(user_key, root) / "checkpoints.json"


def load_checkpoints(root: str | Path | None = None, *, user_key: str) -> dict[str, Any]:
    value = load_json(checkpoints_path(root, user_key=user_key), {})
    return value if isinstance(value, dict) else {}


def save_checkpoints(checkpoints: dict[str, Any], root: str | Path | None = None, *, user_key: str) -> Path:
    return save_json(checkpoints_path(root, user_key=user_key), checkpoints)


def snapshots_path(root: str | Path | None = None, *, user_key: str) -> Path:
    return user_data_root(user_key, root) / "snapshots"


def snapshot_dir(snapshot_id: str, root: str | Path | None = None, *, user_key: str) -> Path:
    return snapshots_path(root, user_key=user_key) / snapshot_id


def save_snapshot(
    snapshot_id: str,
    candidates: list[dict[str, Any]],
    meta: dict[str, Any],
    selections: dict[str, Any],
    root: str | Path | None = None,
    *,
    user_key: str,
) -> Path:
    directory = snapshot_dir(snapshot_id, root, user_key=user_key)
    directory.mkdir(parents=True, exist_ok=True)
    save_json(directory / "candidates.json", candidates)
    save_json(directory / "meta.json", meta)
    save_json(directory / "selections.json", selections)
    _mirror(lambda r: r.record_scan, user_key, snapshot_id, len(candidates or []),
            followers=[c for c in (candidates or []) if isinstance(c, dict)], root=root)
    return directory


def load_snapshot(snapshot_id: str, root: str | Path | None = None, *, user_key: str) -> dict[str, Any]:
    directory = snapshot_dir(snapshot_id, root, user_key=user_key)
    return {
        "candidates": load_json(directory / "candidates.json", []),
        "meta": load_json(directory / "meta.json", {}),
        "selections": load_json(directory / "selections.json", {}),
    }


def history_path(root: str | Path | None = None, *, user_key: str) -> Path:
    """Per-user hash-chained audit log: data/users/{user_key}/history/history.jsonl."""
    return user_data_root(user_key, root) / "history" / "history.jsonl"


def reports_path(root: str | Path | None = None, *, user_key: str) -> Path:
    return user_data_root(user_key, root) / "reports"


def journal_path(root: str | Path | None = None, *, user_key: str) -> Path:
    return user_data_root(user_key, root) / "journal.md"


def always_keep_backups_path(root: str | Path | None = None, *, user_key: str) -> Path:
    return user_data_root(user_key, root) / "backups" / "always_keep"


def user_config_path(root: str | Path | None = None, *, user_key: str) -> Path | None:
    """This user's own config: data/users/{k}/config.json, else the keyed dev fixture
    config/dev_user_{k}.json. Never the shared defaults and never another user's file."""
    base = _root(root)
    for p in (user_data_root(user_key, root) / "config.json", base / "config" / f"dev_user_{validate_user_key(user_key)}.json"):
        if p.is_file():
            return p
    return None


def load_user_config_dict(root: str | Path | None = None, *, user_key: str) -> dict[str, Any]:
    """Raw per-user config dict ({} when the user has none). Rejects a file whose user_key differs."""
    p = user_config_path(root, user_key=user_key)
    if p is None:
        return {}
    raw = load_json(p, {}) or {}
    if raw.get("user_key") not in (None, user_key):
        raise ValueError(f"{p} belongs to user_key {raw.get('user_key')!r}, not {user_key!r}")
    return raw


def append_journal(text: str, root: str | Path | None = None, *, user_key: str) -> Path:
    """Append one line to this user's own journal (data/users/{k}/journal.md); never a shared journal."""
    p = journal_path(root, user_key=user_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")
    apply_user_permissions(user_key, root)
    return p


def _records():
    try:
        from . import records
    except ImportError:  # pragma: no cover
        import records
    return records


def _mirror(fn, *a, **k):
    """Write-through to terminator.db (v3.5.0). Never breaks the caller; a failure is saved for the bot to report."""
    try:
        return fn(_records())(*a, **k)
    except Exception as exc:  # pragma: no cover - defensive
        try:
            r = _records(); uk = a[0]
            st_ = r.pending(uk, k.get("root")); st_["needs_attention"] = {"error": f"records write failed: {exc}",
                "tell_owner": "I couldn't save one action to your records file; I'll retry on the next action."}
            r._save_state(uk, k.get("root"), st_)
        except Exception:
            pass
        return None
