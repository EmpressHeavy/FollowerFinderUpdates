"""Preflight: inside Grok Bot a run starts only after BOTH Google connections were re-verified THIS session.

Owner decisions (2026-10-16): Google Sheets and Google Drive are mandatory inside Grok Bot (8:13/8:14 PM PT);
they are re-checked at the start of EACH session and any failed check stops runs until a fresh success (8:17 PM PT);
the Grok Bot template is locked to runtime "grokbot" and can't be switched to the standalone path (8:17 PM PT).

Runtime
- "grokbot" (default config; also assumed when runtime is missing = fail closed). sheets.backend must be
  "connector"; google_api/csv are refused, not honored.
- Standalone (outside Grok Bot) ONLY when BOTH: env FOLLOWER_FINDER_STANDALONE=1 AND the config says
  runtime "standalone" (a separate standalone config, not the template). Then the connectors aren't required.
- set_runtime / set_sheets_backend refuse to move a grokbot config off grokbot/connector. The bot must never do it
  on a user's request either.

Session recheck (user's own config.json, "connections"):
  {"session": {"id", "started_at"},
   "google_sheets": {"confirmed_at", "session_id"}, "google_drive": {"confirmed_at", "session_id"},
   "failure": {"connection", "error", "at", "session_id"}   # present only after a failed check}
- start_session(): at the start of every session; clears old confirmations and failures.
- record_confirmation(conn): only after a cheap connector call worked (Drive search_files; Sheets read_range on
  the review sheet); clears a failure recorded for that connection.
- record_failure(conn, error): when a connector call errors mid-session; runs stop until a fresh success.
- check()/require(): both confirmations must belong to the current session, the session must be younger than
  SESSION_MAX_HOURS, and no failure may be outstanding. Otherwise PreflightError (fail closed).
Nothing here calls Google or X.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from pathlib import Path
from typing import Any

try:
    from . import storage as st
except ImportError:  # pragma: no cover
    import storage as st

CONNECTIONS = {"google_sheets": "Google Sheets", "google_drive": "Google Drive"}
GROKBOT = "grokbot"
STANDALONE = "standalone"
STANDALONE_ENV = "FOLLOWER_FINDER_STANDALONE"
SESSION_MAX_HOURS = 12
BOOK_READ_MAX_MINUTES = 60  # the Follower Finder Book is read before every scan or removal


class PreflightError(RuntimeError):
    """A run (or config change) was refused by the Grok Bot lockdown."""


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _parse(ts):
    try:
        t = dt.datetime.fromisoformat(str(ts))
        return t if t.tzinfo else t.astimezone()
    except (TypeError, ValueError):
        return None


# ---------- runtime lock ----------
def is_standalone(cfg: dict | None, env=None) -> bool:
    env = os.environ if env is None else env
    return (cfg or {}).get("runtime") == STANDALONE and env.get(STANDALONE_ENV) == "1"


def inside_grok_bot(cfg: dict | None, env=None) -> bool:
    return not is_standalone(cfg, env)


def backend_problem(cfg: dict | None, env=None) -> str | None:
    """Inside Grok Bot only the connector backend is allowed."""
    if not inside_grok_bot(cfg, env):
        return None
    want = ((cfg or {}).get("sheets") or {}).get("backend", "connector")
    if want != "connector":
        return (f"sheets.backend {want!r} isn't allowed in Grok Bot. Follower Finder in Grok Bot always writes "
                "through your Google Sheets connector; the manual key and CSV paths are only for the standalone package.")
    return None


def _cfg_path(user_key, root) -> Path:
    return st.user_data_root(user_key, root) / "config.json"


def _load(user_key, root) -> dict:
    p = _cfg_path(user_key, root)
    return st.load_user_config_dict(root, user_key=user_key) if p.is_file() else {"user_key": user_key}


def _save(user_key, root, cfg) -> Path:
    p = _cfg_path(user_key, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    st.save_json(p, cfg)
    st.apply_user_permissions(user_key, root)
    return p


def set_runtime(user_key: str, runtime: str, root=None, env=None) -> Path:
    """Config helper. Refuses to move a Grok Bot config off grokbot (no skipping the connector checks)."""
    cfg = _load(user_key, root)
    if inside_grok_bot(cfg, env) and runtime != GROKBOT:
        raise PreflightError("Can't change runtime: Follower Finder in Grok Bot stays in Grok Bot mode, and its "
                             "Google Sheets / Google Drive checks can't be turned off. The standalone path is only "
                             "for running the package outside Grok Bot with its own standalone config.")
    cfg["runtime"] = runtime
    return _save(user_key, root, cfg)


def set_sheets_backend(user_key: str, backend: str, root=None, env=None) -> Path:
    cfg = _load(user_key, root)
    trial = dict(cfg, sheets=dict(cfg.get("sheets") or {}, backend=backend))
    prob = backend_problem(trial, env)
    if prob:
        raise PreflightError(prob)
    return _save(user_key, root, trial)


# ---------- session recheck ----------
def start_session(user_key: str, root=None, *, session_id: str | None = None, when: str | None = None) -> str:
    """Start a new session: old confirmations and failures no longer count; re-run both connector checks."""
    cfg = _load(user_key, root)
    sid = session_id or uuid.uuid4().hex[:16]
    cfg["connections"] = {"session": {"id": sid, "started_at": when or _now().isoformat()}}
    _save(user_key, root, cfg)
    return sid


def _session(cfg) -> dict:
    return ((cfg or {}).get("connections") or {}).get("session") or {}


def record_confirmation(user_key: str, connection: str, root=None, *, when: str | None = None) -> Path:
    """Call ONLY after a successful cheap connector call for `connection` in the current session."""
    if connection not in CONNECTIONS:
        raise ValueError(f"unknown connection {connection!r}")
    cfg = _load(user_key, root)
    sess = _session(cfg)
    if not sess.get("id"):
        raise PreflightError("No session started: call start_session() first, then re-run the connector checks.")
    conns = cfg["connections"]
    conns[connection] = {"confirmed_at": when or _now().isoformat(), "session_id": sess["id"]}
    if (conns.get("failure") or {}).get("connection") == connection:
        conns.pop("failure")
    return _save(user_key, root, cfg)


def record_book_read(user_key: str, root=None, *, document_id: str | None = None, when: str | None = None) -> Path:
    """Call ONLY right after reading the owner's "Follower Finder Book" Google Doc (rules) with the Docs connector."""
    cfg = _load(user_key, root)
    sess = _session(cfg)
    if not sess.get("id"):
        raise PreflightError("No session started: call start_session() first, then re-run the connector checks.")
    cfg["connections"]["book_read"] = {"read_at": when or _now().isoformat(), "session_id": sess["id"],
                                       "document_id": document_id or ((cfg.get("book") or {}).get("document_id"))}
    return _save(user_key, root, cfg)


def record_failure(user_key: str, connection: str, error: str = "", root=None) -> Path:
    """A connector call failed: runs stop until a fresh successful check of that connection."""
    if connection not in CONNECTIONS:
        raise ValueError(f"unknown connection {connection!r}")
    cfg = _load(user_key, root)
    conns = cfg.setdefault("connections", {})
    conns.pop(connection, None)
    conns["failure"] = {"connection": connection, "error": str(error)[:300], "at": _now().isoformat(),
                        "session_id": _session(cfg).get("id")}
    return _save(user_key, root, cfg)


def check(user_key: str, root=None, cfg: dict | None = None, *, env=None, now: dt.datetime | None = None) -> dict[str, Any]:
    if cfg is None:
        cfg = st.load_user_config_dict(root, user_key=user_key)
    if not inside_grok_bot(cfg, env):
        return {"ok": True, "inside_grok_bot": False, "missing": [], "message": None}
    prob = backend_problem(cfg, env)
    if prob:
        return {"ok": False, "inside_grok_bot": True, "missing": [], "message": prob}
    if not needs_google(cfg):
        # v3.5.0: records live in this owner's own terminator.db; Google Sheets/Drive are only required when the
        # owner chose the legacy Google Sheets backup. The Book read applies only if this owner has a Book doc.
        return _book_gate(cfg, now or _now())
    conns = (cfg or {}).get("connections") or {}
    sess = _session(cfg)
    now = now or _now()
    started = _parse(sess.get("started_at"))
    if not sess.get("id") or started is None:
        return {"ok": False, "inside_grok_bot": True, "missing": list(CONNECTIONS),
                "message": ("Can't start: Google Sheets and Google Drive haven't been checked this session. "
                            "I need to recheck both connections in your Grok Bot first.")}
    if now - started > dt.timedelta(hours=SESSION_MAX_HOURS):
        return {"ok": False, "inside_grok_bot": True, "missing": list(CONNECTIONS), "stale": True,
                "message": (f"Can't start: the Google Sheets and Google Drive checks are from an old session "
                            f"(over {SESSION_MAX_HOURS} hours ago). I need to recheck both connections; reconnect "
                            "them in your Grok Bot if the check fails.")}
    fail = conns.get("failure")
    if fail:
        name = CONNECTIONS.get(fail.get("connection"), fail.get("connection"))
        return {"ok": False, "inside_grok_bot": True, "missing": [fail.get("connection")], "failed": True,
                "message": (f"Stopped: the {name} connection failed a check ({fail.get('error') or 'error'}). "
                            f"Reconnect {name} in your Grok Bot; runs stay stopped until a fresh check succeeds.")}
    miss = [k for k in CONNECTIONS if (conns.get(k) or {}).get("session_id") != sess["id"]
            or _parse((conns.get(k) or {}).get("confirmed_at")) is None]
    if miss:
        names = " and ".join(CONNECTIONS[m] for m in miss)
        return {"ok": False, "inside_grok_bot": True, "missing": miss,
                "message": (f"Can't start: {names} {'is' if len(miss) == 1 else 'are'} not confirmed for this "
                            f"session. Follower Finder needs both Google Sheets and Google Drive connected in your "
                            f"Grok Bot. Connect {names} in your Grok Bot, then I'll recheck and you can start.")}
    book = conns.get("book_read") or {}
    read_at = _parse(book.get("read_at"))
    if book.get("session_id") != sess["id"] or read_at is None or \
            now - read_at > dt.timedelta(minutes=BOOK_READ_MAX_MINUTES):
        return {"ok": False, "inside_grok_bot": True, "missing": ["follower_finder_book"],
                "message": ("Can't start yet: I read your Follower Finder Book (your rules doc) before every scan "
                            "or removal. I'll read it now, then start.")}
    return {"ok": True, "inside_grok_bot": True, "missing": [], "message": None}


def needs_google(cfg: dict | None) -> bool:
    """Legacy configs (no records section) keep the old Google rule; otherwise only existing owners still on Sheets.
    v3.5.1: new owners can't pick Sheets, so Google is never required for them."""
    if "records" not in (cfg or {}):
        return True
    return (((cfg or {}).get("records") or {}).get("backup") or {}).get("method") == "sheets"


def _book_gate(cfg: dict, now: dt.datetime) -> dict[str, Any]:
    if not ((cfg or {}).get("book") or {}).get("document_id"):
        return {"ok": True, "inside_grok_bot": True, "missing": [], "message": None, "google_required": False}
    conns = (cfg or {}).get("connections") or {}
    sess = _session(cfg)
    book = conns.get("book_read") or {}
    read_at = _parse(book.get("read_at"))
    if not sess.get("id") or book.get("session_id") != sess["id"] or read_at is None or \
            now - read_at > dt.timedelta(minutes=BOOK_READ_MAX_MINUTES):
        return {"ok": False, "inside_grok_bot": True, "missing": ["follower_finder_book"], "google_required": False,
                "message": ("Can't start yet: I read your Follower Finder Book (your rules doc) before every scan "
                            "or removal. I'll read it now, then start.")}
    return {"ok": True, "inside_grok_bot": True, "missing": [], "message": None, "google_required": False}


def require(user_key: str, root=None, cfg: dict | None = None, *, env=None) -> None:
    res = check(user_key, root, cfg, env=env)
    if not res["ok"]:
        raise PreflightError(res["message"])
