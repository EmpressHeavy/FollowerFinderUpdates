"""Chunked, checkpointed classification for large follower lists (offline; no network).

- Every input record is classified; there is no sampling cap.
- Work is done in chunks (default 1000). After each chunk, compact results are written to a
  progress file so an interrupted run resumes from the last finished chunk.
- The progress file is tied to a fingerprint of the input (user_ids/usernames + settings); a different
  input or settings starts fresh rather than reusing stale results.
- A record that raises during classification is labelled NOT EVALUATED (never KEEP) with the reason.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

try:
    from .build_snapshot import candidate_from_raw
    from .classify import classify, DEFAULT_INACTIVITY_CONFIG, Candidate
except ImportError:  # pragma: no cover
    from build_snapshot import candidate_from_raw
    from classify import classify, DEFAULT_INACTIVITY_CONFIG, Candidate

NOT_EVALUATED = "NOT EVALUATED"
PROGRESS_VERSION = 2  # 2026-10-20: person check (b)/(c) + impersonation flag


def load_inactivity_config(user_root: str | Path | None) -> dict[str, Any]:
    """Defaults merged with the optional "inactivity" block of data/users/{key}/config.json."""
    cfg = dict(DEFAULT_INACTIVITY_CONFIG)
    cfg.setdefault("classify_chunk_size", 1000)
    if user_root is not None:
        p = Path(user_root) / "config.json"
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                block = raw.get("inactivity") if isinstance(raw, dict) else None
                if isinstance(block, dict):
                    cfg.update({k: v for k, v in block.items() if not str(k).startswith("_")})
            except (OSError, ValueError):
                pass
    return cfg


def _fingerprint(records: list[dict[str, Any]], threshold: int, cfg: dict[str, Any], as_of: Any) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([threshold, cfg, str(as_of)], sort_keys=True, default=str).encode())
    for r in records:
        h.update(json.dumps(r, sort_keys=True, default=str).encode())
    return h.hexdigest()


def _not_evaluated(exc: BaseException) -> dict[str, Any]:
    return {
        "stage": "not_evaluated", "label": NOT_EVALUATED, "bot_like": False, "inactive": False,
        "candidate": False, "signals": [f"Not evaluated: {type(exc).__name__}"],
        "days_inactive": "Not available", "last_activity": "Not available",
    }


def _save_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
    os.replace(tmp, path)


def classify_in_chunks(
    records: list[dict[str, Any]],
    always_keep: Iterable[Any] = (),
    *,
    inactivity_days_threshold: int = 90,
    inactivity_cfg: dict[str, Any] | None = None,
    chunk_size: int = 1000,
    progress_path: str | Path | None = None,
    resume: bool = True,
    as_of: Any = None,
    stop_after_chunks: int | None = None,  # test hook: simulate an interruption
    friends: Iterable[Any] | None = None,
) -> dict[str, Any]:
    keep = list(always_keep)
    if friends is None:  # impersonation targets: followers the owner follows back (mutuals) in this scan
        friends = []
        for rec in records:
            try:
                cf = candidate_from_raw(rec, keep)
            except Exception:  # noqa: BLE001
                continue
            if cf.followed_by_me:
                friends.append({"handle": cf.username, "name": cf.name, "user_id": cf.user_id})
    try:
        from .classify import FriendIndex
    except ImportError:  # pragma: no cover
        from classify import FriendIndex
    friends = FriendIndex(list(friends))
    chunk_size = max(1, int(chunk_size))
    fp = _fingerprint(records, inactivity_days_threshold, inactivity_cfg or {}, as_of)
    done: dict[int, dict[str, Any]] = {}
    progress_path = Path(progress_path) if progress_path else None
    if progress_path and resume and progress_path.is_file():
        try:
            prog = json.loads(progress_path.read_text(encoding="utf-8"))
            if prog.get("fingerprint") == fp and prog.get("version") == PROGRESS_VERSION:
                done = {int(k): v for k, v in prog.get("results", {}).items()}
        except (OSError, ValueError):
            done = {}
    n_chunks = (len(records) + chunk_size - 1) // chunk_size
    resumed = len({i // chunk_size for i in done})
    ran = 0
    interrupted = False
    for ci in range(n_chunks):
        lo, hi = ci * chunk_size, min(len(records), (ci + 1) * chunk_size)
        if all(i in done for i in range(lo, hi)):
            continue
        if stop_after_chunks is not None and ran >= stop_after_chunks:
            interrupted = True
            break
        for i in range(lo, hi):
            try:
                cand = candidate_from_raw(records[i], keep)
                done[i] = classify(cand, inactivity_days_threshold=inactivity_days_threshold,
                                   inactivity_cfg=inactivity_cfg, as_of=as_of, friends=friends)
            except Exception as exc:  # noqa: BLE001 - recorded, never silently dropped
                done[i] = _not_evaluated(exc)
        ran += 1
        if progress_path:
            _save_atomic(progress_path, {"version": PROGRESS_VERSION, "fingerprint": fp, "total": len(records),
                                         "chunk_size": chunk_size, "results": {str(k): v for k, v in done.items()}})
    classifications = []
    for i, rec in enumerate(records):
        res = done.get(i) or {**_not_evaluated(RuntimeError("interrupted")), "signals": ["Not evaluated: run interrupted"]}
        try:
            cand = candidate_from_raw(rec, keep)
        except Exception:  # noqa: BLE001
            cand = Candidate(user_id=str(rec.get("user_id") or "Not available"),
                             username=str(rec.get("username") or "Not available"), name=str(rec.get("name") or ""))
        classifications.append((cand, res))
    not_eval = sum(1 for _, r in classifications if r.get("label") == NOT_EVALUATED)
    if progress_path and not interrupted and progress_path.exists():
        progress_path.unlink()  # complete: nothing left to resume
    return {"classifications": classifications, "total": len(records),
            "classified": len(records) - not_eval, "not_evaluated": not_eval,
            "chunks": n_chunks, "resumed_chunks": resumed, "interrupted": interrupted}
