"""Follower Finder records (v3.5.0): one owner's scans, removals, graveyard and allowlist in a local SQLite file,
data/users/<user_key>/terminator.db (the MAIN store; separate from the bot's memory brain.db and journal).

Write-through: every action is written here immediately (record_* helpers, also called by the removal, list and
graveyard code and by storage.save_always_keep), then pushed straight away to the owner's chosen backup
(after_write). Never a nightly batch. A failed push is retried once and saved as `needs_attention` so the bot tells
the owner (pending()).

Backup methods (owner's choice at setup, changeable in the monthly check-in):
  github     private repo the owner owns (recommended); sheet-mirror JSON (records/, v3.5.5) + dump.sql; visibility re-checked before EVERY push
  cloud_csv  CSVs in ONE dedicated folder of the owner's own cloud drive through a connector (SearchPlugins first)
  download   the bot sends a CSV zip; weekly reminder to keep it somewhere safe
  sheets     legacy Google Sheets log: existing owners only (v3.5.1: never offered at new setup; see migration_card)
  pending    v3.5.2: no choice yet. NOT an end state (a backup can't be skipped). Until the owner picks one of the
             three above, records stay saved locally and the bot keeps gently reminding them (backup_reminder);
             v3.5.7: no file is sent unless they pick the download or ask for their history
Restore: restore_from_dir / restore_from_file / restore_from_github rebuild the db after a wipe (idempotent).
Upgrade: upgrade_card + handle_upgrade_answer + import_legacy bring records over from an older version.
History tables are append-only (triggers refuse UPDATE/DELETE). No tokens are ever stored.

    python -m src.records --user K summary|graveyard|counts|export DIR|restore PATH|backup|pending
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

try:
    from .storage import PROJECT_ROOT, user_data_root, validate_user_key, DATA_FILE_MODE, DIR_MODE, load_json
except ImportError:  # pragma: no cover
    from storage import PROJECT_ROOT, user_data_root, validate_user_key, DATA_FILE_MODE, DIR_MODE, load_json

TABLES = {
    "followers": ["user_id", "handle", "first_seen", "score", "reasons", "last_seen"],
    "removals": ["user_id", "handle", "action", "at", "batch", "result"],
    "graveyard": ["user_id", "handle", "added_at", "reason", "event"],
    "allowlist": ["user_id", "handle", "added_at", "op"],
    "scans": ["id", "at", "count"],
    "candidates": ["scan_id", "user_id", "handle", "label", "reasons", "score"],
    "scan_metrics": ["scan_id", "metric", "value"],
    "audit_log": ["at", "event", "detail"],
    "sheet_tabs": ["tab", "position", "header_row", "headers", "preamble", "row_count", "imported_at"],
    "sheet_rows": ["tab", "row_num", "cells", "formulas", "imported_at"],
}
METHODS = ("github", "cloud_csv", "download", "sheets", "pending")
CHOOSABLE = ("github", "cloud_csv", "download")  # v3.5.2: the only methods an owner can pick (no skip)
DEFAULT_REPO_NAME = "my-follower-finder-backup"
DEFAULT_FOLDER = "Follower Finder records"

SETUP_MESSAGE = """Keeping your records safe 🛡️
I keep a record of every bot I remove for you: who got unfollowed, who's in the graveyard, and who you told me to leave alone. That record is how I keep bots from sneaking back onto your account.
Right now it lives on my computer, which runs on SpaceXAI's servers. That's fast and private, and it doesn't touch your Google Drive or any of your files. But if something ever happened to my computer, your records could be lost. So I'd like to keep a backup copy too:
1. GitHub backup (recommended). GitHub is a free online storage service. I'd save your records to a private folder (a "repository") that only you can see. It's free, uses no space on your device, and if I ever get reset I can restore everything in seconds. You'd sign in to GitHub (or make a free account) and give me access to that one private folder.
2. Your own cloud storage or device. I can save your records as a spreadsheet file (CSV) wherever you like: your cloud storage (Google Drive, OneDrive, Dropbox, or another; I'll show a quick sign-in card and keep one folder updated), or your device (I'll send you the file to keep anywhere, including iCloud).
Hand it back to me anytime and I'll restore everything."""

SETUP_OPTIONS = [("GitHub backup (recommended)", "github"), ("My cloud storage (CSV)", "cloud_csv"),
                 ("Send me the file (CSV)", "download")]
CONNECTOR_NOTE = "I can connect directly to most cloud drives through this app's connectors, so tell me which one you use."

UPGRADE_QUESTION = "Have you used Follower Finder before?"
UPGRADE_OPTIONS = ["Yes", "No", "Not sure"]
EXPORT_REQUEST = ("Follower Finder records upgrade: please send a full export of this owner's Follower Finder records "
                  "from your copy (log sheet tabs Actions, Keepers, List Graveyard; local files graveyard.jsonl, "
                  "always_keep.json, history/history.jsonl and snapshots/; any terminator.db or CSV export). "
                  "Records only: no tokens, passwords or keys.")


# ---------- db ----------
def db_path(user_key: str, root=None) -> Path:
    return user_data_root(user_key, root) / "terminator.db"


def init_db(user_key: str, root=None) -> Path:
    validate_user_key(user_key)
    p = db_path(user_key, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    base = Path(root) if root is not None else PROJECT_ROOT
    sp = base / "templates" / "records_schema.sql"
    if not sp.exists():
        sp = PROJECT_ROOT / "templates" / "records_schema.sql"
    c = sqlite3.connect(p)
    try:
        c.executescript(sp.read_text(encoding="utf-8"))
        c.commit()
    finally:
        c.close()
    os.chmod(p, DATA_FILE_MODE)
    return p


def connect(user_key: str, root=None) -> sqlite3.Connection:
    init_db(user_key, root)
    c = sqlite3.connect(db_path(user_key, root), timeout=10)
    c.execute("PRAGMA busy_timeout=10000")
    return c


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _s(v):
    return None if v is None else str(v)


def _k(v):
    """Key columns are never NULL ('' instead) so the UNIQUE constraints make imports/restores idempotent."""
    return "" if v is None else str(v)


def _write(user_key, root, sql, params) -> int:
    """One short transaction per action (committed before anything else happens)."""
    c = connect(user_key, root)
    try:
        cur = c.execute(sql, params)
        c.commit()
        return cur.rowcount
    finally:
        c.close()


# ---------- write-through helpers ----------
def record_scan(user_key, scan_id, count, *, at=None, followers=(), root=None, sync=True) -> dict:
    at = at or _now()
    c = connect(user_key, root)
    try:
        c.execute("INSERT OR IGNORE INTO scans(id,at,count) VALUES(?,?,?)", (str(scan_id), at, int(count)))
        n_lab = 0
        for f in followers or ():
            uid = _s(f.get("x_user_id") or f.get("user_id") or f.get("id"))
            if not uid:
                continue
            handle = f.get("handle") or f.get("username")  # snapshot candidates carry "username"
            reasons = f.get("reasons") if f.get("reasons") is not None else f.get("signals")
            reasons = "; ".join(reasons) if isinstance(reasons, (list, tuple)) else reasons
            n_lab += c.execute("INSERT OR IGNORE INTO candidates(scan_id,user_id,handle,label,reasons,score) VALUES(?,?,?,?,?,?)",
                               (str(scan_id), uid, _k(handle), f.get("label"), reasons, f.get("score"))).rowcount
            c.execute("INSERT INTO followers(user_id,handle,first_seen,score,reasons,last_seen) VALUES(?,?,?,?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET handle=excluded.handle, score=excluded.score, "
                      "reasons=excluded.reasons, last_seen=excluded.last_seen",
                      (uid, handle, at, f.get("score"), reasons, at))
        _audit(c, "scan_recorded", f"scan={scan_id} count={int(count)} candidates={n_lab}", at)
        c.commit()
    finally:
        c.close()
    return after_write(user_key, root) if sync else {}


def _audit(c, event, detail, at=None):
    c.execute("INSERT OR IGNORE INTO audit_log(at,event,detail) VALUES(?,?,?)", (at or _now(), event, detail))


def audit(user_key, event, detail="", root=None) -> None:
    """Append-only audit line (no handles/tokens beyond what the records already hold)."""
    c = connect(user_key, root)
    try:
        _audit(c, event, detail); c.commit()
    finally:
        c.close()


def record_scan_metrics(user_key, scan_id, metrics: dict, root=None, sync=True) -> dict:
    """Per-scan counts (protected, verified, KEEP/REVIEW/BOT-LIKE/INACTIVE...) next to the scan row."""
    c = connect(user_key, root)
    try:
        n = 0
        for k, v in (metrics or {}).items():
            if isinstance(v, (int, float, str, bool)) or v is None:
                n += c.execute("INSERT OR IGNORE INTO scan_metrics(scan_id,metric,value) VALUES(?,?,?)",
                               (str(scan_id), str(k), None if v is None else str(v))).rowcount
        _audit(c, "scan_metrics_recorded", f"scan={scan_id} metrics={n}")
        c.commit()
    finally:
        c.close()
    return after_write(user_key, root) if sync else {}


def record_removal(user_key, handle, user_id=None, *, action="removed", at=None, batch=None, result=None,
                   reason=None, root=None, sync=True) -> dict:
    if action not in ("removed", "blocked", "listed", "restored"):
        raise ValueError("action must be removed, blocked, listed or restored")
    at = at or _now()
    if _write(user_key, root, "INSERT OR IGNORE INTO removals(user_id,handle,action,at,batch,result) VALUES(?,?,?,?,?,?)",
              (_k(user_id), _k(handle), action, at, _s(batch), result)):
        audit(user_key, f"{action}_recorded", f"user_id={_k(user_id)} batch={_s(batch) or ''}", root)
    if action in ("removed", "blocked"):
        _write(user_key, root, "INSERT OR IGNORE INTO graveyard(user_id,handle,added_at,reason,event) VALUES(?,?,?,?,?)",
               (_k(user_id), _k(handle), at, reason, action.upper()))
    return after_write(user_key, root) if sync else {}


def record_graveyard_event(user_key, rec: dict, root=None, sync=True) -> dict:
    """Mirror of one graveyard.jsonl line (REMOVED / BLOCKED / RETURN_DETECTED)."""
    ev = (rec.get("event") or "REMOVED").upper()
    uid, h, at = _s(rec.get("x_user_id") or rec.get("user_id")), rec.get("handle"), rec.get("at") or _now()
    if ev in ("REMOVED", "BLOCKED"):
        return record_removal(user_key, h, uid, action=ev.lower(), at=at, batch=rec.get("snapshot_id"),
                              result="confirmed on X" if rec.get("confirmed_on_x", True) else None,
                              reason=rec.get("reason"), root=root, sync=sync)
    _write(user_key, root, "INSERT OR IGNORE INTO graveyard(user_id,handle,added_at,reason,event) VALUES(?,?,?,?,?)",
           (_k(uid), _k(h), at, rec.get("reason"), ev))
    return after_write(user_key, root) if sync else {}


def record_list_left(user_key, entry: dict, root=None, sync=True) -> dict:
    e = entry or {}
    return record_removal(user_key, e.get("owner_handle") or e.get("list_name"), e.get("owner_id"), action="listed",
                          at=e.get("at") or e.get("when") or _now(),
                          batch=e.get("list_id") or e.get("list_name"), result=_s(e.get("result")), root=root, sync=sync)


def _acct(a):
    if isinstance(a, str):
        return None, a.lstrip("@")
    uid = _clean_uid(a.get("x_user_id") or a.get("user_id") or a.get("id"))
    h = (a.get("handle") or a.get("username") or a.get("screen_name") or "").strip().lstrip("@") or None
    return uid, h


def _clean_uid(v):
    """Placeholders such as '(look up before use)' or 'Not available' are not IDs."""
    s = _s(v)
    s = s.strip() if isinstance(s, str) else s
    return None if not s or s.startswith("(") or s.lower() in ("not available", "n/a", "none", "unknown") else s


def _same(a, b) -> bool:
    """Same account: equal user IDs, or (when either side has no ID) equal handles."""
    (ua, ha), (ub, hb) = a, b
    if ua and ub:
        return ua == ub
    return bool(ha and hb and ha.lower() == hb.lower())


def current_allowlist(user_key, root=None) -> list[tuple]:
    c = connect(user_key, root)
    try:
        return c.execute("SELECT user_id,handle,added_at FROM current_allowlist ORDER BY added_at").fetchall()
    finally:
        c.close()


def sync_allowlist(user_key, entries, root=None, sync=True) -> dict:
    """Bring the allowlist in line with the Always Keep list: new rows for adds and removes (never edits)."""
    want = {}
    for a in entries or ():
        uid, h = _acct(a)
        if uid or h:
            want[(uid or "", (h or "").lower())] = (uid, h)
    have = {((u or ""), (h or "").lower()): (u, h) for u, h, _ in current_allowlist(user_key, root)}
    at, changed = _now(), 0
    c = connect(user_key, root)
    try:  # match by identity (ID, else handle) so the same account with more/less detail never churns remove+add
        for k, (u, h) in want.items():
            if not any(_same((u or None, h), (hu or None, hh)) for hu, hh in have.values()):
                changed += c.execute("INSERT OR IGNORE INTO allowlist(user_id,handle,added_at,op) VALUES(?,?,?,'add')", (_k(u), _k(h), at)).rowcount
        for k, (u, h) in have.items():
            if not any(_same((u or None, h), (wu or None, wh)) for wu, wh in want.values()):
                changed += c.execute("INSERT OR IGNORE INTO allowlist(user_id,handle,added_at,op) VALUES(?,?,?,'remove')", (_k(u), _k(h), at)).rowcount
        c.commit()
    finally:
        c.close()
    return (after_write(user_key, root) if sync and changed else {}) | {"changed": changed}


# ---------- answers via queries (no full-sheet reads) ----------
def counts(user_key, root=None) -> dict:
    c = connect(user_key, root)
    try:
        r = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
        r["removed"] = c.execute("SELECT count(*) FROM removals WHERE action='removed'").fetchone()[0]
        r["blocked"] = c.execute("SELECT count(*) FROM removals WHERE action='blocked'").fetchone()[0]
        r["lists_left"] = c.execute("SELECT count(*) FROM removals WHERE action='listed'").fetchone()[0]
        r["allowlist_now"] = c.execute("SELECT count(*) FROM current_allowlist").fetchone()[0]
        return r
    finally:
        c.close()


def weekly_summary(user_key, root=None, days=7) -> str:
    since = (dt.datetime.now().astimezone() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    c = connect(user_key, root)
    try:
        acts = dict(c.execute("SELECT action,count(*) FROM removals WHERE at>=? GROUP BY action", (since,)).fetchall())
        sc = c.execute("SELECT count(*),coalesce(sum(count),0) FROM scans WHERE at>=?", (since,)).fetchone()
    finally:
        c.close()
    return (f"Last {days} days: {sc[0]} scans ({sc[1]} followers checked), {acts.get('removed', 0)} removed, "
            f"{acts.get('blocked', 0)} blocked, {acts.get('listed', 0)} lists left, {acts.get('restored', 0)} restored.")


def graveyard(user_key, root=None, limit=50) -> list[tuple]:
    c = connect(user_key, root)
    try:
        return c.execute("SELECT handle,user_id,added_at,event,reason FROM graveyard ORDER BY added_at DESC LIMIT ?", (limit,)).fetchall()
    finally:
        c.close()


# ---------- export / restore ----------
def export_csv(user_key, out_dir, root=None) -> list[Path]:
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    c = connect(user_key, root)
    files = []
    try:
        for t, cols in TABLES.items():
            p = out / f"{t}.csv"
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f); w.writerow(cols)
                w.writerows(c.execute(f"SELECT {','.join(cols)} FROM {t} ORDER BY rowid"))
            files.append(p)
        (out / "dump.sql").write_text("\n".join(c.iterdump()) + "\n", encoding="utf-8")
        files.append(out / "dump.sql")
    finally:
        c.close()
    return files


def export_zip(user_key, zip_path, root=None) -> Path:
    import tempfile
    zp = Path(zip_path); zp.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        files = export_csv(user_key, td, root)
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                if f.suffix == ".csv":
                    z.write(f, f.name)
    return zp


def _import_rows(c, table, rows) -> int:
    cols = TABLES[table]
    n = 0
    for r in rows:
        vals = [_k(r.get(k)) if k in ("user_id", "handle") and table != "followers"
                else (r.get(k) if r.get(k) != "" else None) for k in cols]
        if table == "followers":
            n += c.execute(f"INSERT OR IGNORE INTO followers({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals).rowcount
        else:
            n += c.execute(f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals).rowcount
    return n


def restore_from_dir(user_key, src_dir, root=None) -> dict:
    """Rebuild/merge from CSV files (<table>.csv). Idempotent: existing rows are kept, duplicates skipped."""
    src = Path(src_dir)
    added = {}
    c = connect(user_key, root)
    try:
        for t in TABLES:
            p = src / f"{t}.csv"
            if p.exists():
                with open(p, newline="", encoding="utf-8") as f:
                    added[t] = _import_rows(c, t, csv.DictReader(f))
        c.commit()
    finally:
        c.close()
    return {"added": added, "counts": counts(user_key, root)}


def restore_from_file(user_key, path, root=None) -> dict:
    """A zip of CSVs or one <table>.csv the owner hands back."""
    import tempfile
    p = Path(path)
    with tempfile.TemporaryDirectory() as td:
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    if n.endswith(".csv") and Path(n).stem in TABLES:
                        (Path(td) / Path(n).name).write_bytes(z.read(n))
        elif p.suffix == ".csv" and p.stem in TABLES:
            (Path(td) / p.name).write_bytes(p.read_bytes())
        else:
            raise ValueError("send a .zip export or a <table>.csv file (" + ", ".join(TABLES) + ")")
        return restore_from_dir(user_key, td, root)


# ---------- config / backup choice ----------
LEGACY_SHEET_TITLE = "Bot Terminator Review - {handle}"  # the alpha-era log sheet title (find it with a Drive search)


def legacy_sheet_id(cfg) -> str | None:
    """A Sheet in use, from any place older configs kept it (sheets.spreadsheet_id, review_sheet_id, legacy_sheet_id)."""
    c = cfg or {}
    return ((c.get("sheets") or {}).get("spreadsheet_id") or c.get("review_sheet_id")
            or ((c.get("records") or {}).get("legacy_sheet_id")) or None)


def legacy_sheet_search(cfg) -> dict | None:
    """v3.5.4 fix: configs with no records section and no saved sheet ID may still have a Sheet in use. The bot runs this
    read-only Drive search at session start; pass a hit to needs_migration(cfg, found_sheet_id=...)."""
    if "records" in (cfg or {}) or legacy_sheet_id(cfg):
        return None
    h = (cfg or {}).get("owner_x_handle") or (cfg or {}).get("x_handle") or ""
    h = h if h.startswith("@") or not h else "@" + h
    return {"tool": "Google Drive search (read only)", "mimeType": "application/vnd.google-apps.spreadsheet",
            "name": LEGACY_SHEET_TITLE.format(handle=h), "also_try": "name contains 'Bot Terminator'"}



def section(cfg: dict | None) -> dict:
    r = (cfg or {}).get("records") or {}
    b = dict(r.get("backup") or {})
    if not b.get("method"):  # legacy owners who already have a log sheet keep Sheets until they choose
        b["method"] = "sheets" if legacy_sheet_id(cfg) else "pending"
    b.setdefault("repo", None); b.setdefault("service", None); b.setdefault("plugin_id", None)
    b.setdefault("folder", DEFAULT_FOLDER)
    return {"backup": b}


def validate_repo(repo: str) -> str:
    try:
        from .memory import validate_repo as v
    except ImportError:  # pragma: no cover
        from memory import validate_repo as v
    return v(repo)


def set_backup(cfg: dict, method: str, *, repo=None, service=None, plugin_id=None, folder=None) -> dict:
    if method not in METHODS:
        raise ValueError(f"backup method must be one of {CHOOSABLE} (a backup can't be skipped)")
    b = section(cfg)["backup"] | {"method": method}
    if method == "github":
        b["repo"] = validate_repo(repo or b.get("repo") or "")
    if method == "cloud_csv":
        if not service:
            raise ValueError("which cloud service? (Google Drive, OneDrive, Dropbox, ...)")
        b.update(service=str(service).strip(), plugin_id=_s(plugin_id), folder=folder or b.get("folder") or DEFAULT_FOLDER)
    return {"records": {"backup": b}, "book_change": f"Records backup: {status_line({'records': {'backup': b}})}"}


def status_line(cfg) -> str:
    b = section(cfg)["backup"]
    m = b["method"]
    return {"github": f"Records backup: GitHub, private repo {b.get('repo')} (re-checked private before every push)",
            "cloud_csv": f"Records backup: CSV in your {b.get('service')}, folder \"{b.get('folder')}\"",
            "download": "Records backup: CSV file sent to you (weekly reminder)",
            "sheets": "Records backup: Google Sheets log (legacy)",
            "pending": ("Records backup: not chosen yet. Your records are saved here; "
                        "pick GitHub, your cloud storage, or the file to finish")}[m]


REMINDER_MESSAGE = ("Quick reminder 🛡️ your Follower Finder records still need a backup home. Pick one and I'll handle "
                    "the rest: GitHub (recommended), your own cloud storage, or I send you the file. Until then your "
                    "records are saved safely here.")
REMINDER_ROUTINE = {"name": "Follower Finder backup reminder", "schedule": "every 3 days at the owner's scan time",
                    "content": "records.backup_reminder(user_key): if it returns a message, send it with its card "
                               "(no file); if it returns None, the backup is set: delete this routine."}


def needs_backup_choice(cfg) -> bool:
    return section(cfg)["backup"]["method"] == "pending"


def backup_reminder(user_key, root=None, cfg=None) -> dict | None:
    """Session start + the light reminder routine: while no backup is chosen, re-ask gently (v3.5.7: no file is
    sent; a file goes out only when the owner picked the CSV/download backup or asks for their history)."""
    cfg = cfg if cfg is not None else _cfg(user_key, root)
    if not needs_backup_choice(cfg):
        return None
    st_ = pending(user_key, root); st_["last_reminder"] = _now(); _save_state(user_key, root, st_)
    return {"message": REMINDER_MESSAGE, "card": setup_card(), "routine": REMINDER_ROUTINE}


def handle_setup_answer(user_key, text: str, root=None, cfg=None) -> dict:
    """Map a setup-card reply. A skip / not now reply never ends setup: re-ask gently (no file)."""
    m = parse_setup_answer(text)
    if m in CHOOSABLE:
        return {"action": "set", "method": m}
    r = backup_reminder(user_key, root, cfg if cfg is not None else {"records": {"backup": {"method": "pending"}}})
    return {"action": "remind", **r}


def setup_card() -> dict:
    return {"message": SETUP_MESSAGE, "note": CONNECTOR_NOTE, "options": [o for o, _ in SETUP_OPTIONS],
            "values": dict(SETUP_OPTIONS)}


def parse_setup_answer(text: str) -> str | None:
    t = (text or "").strip().lower()
    for label, m in SETUP_OPTIONS:
        if t == label.lower():
            return m
    if any(w in t for w in ("skip", "not now", "later", "no backup", "none")):
        return "remind"  # v3.5.2: not an option; gentle re-ask (backup_reminder), no file
    if "github" in t or t in ("1", "recommended"):
        return "github"
    if "sheet" in t:  # v3.5.1: Google Sheets is not offered to new owners; a spreadsheet file = the CSV download
        return "download"
    if any(w in t for w in ("device", "download", "send me", "file", "icloud")):
        return "download"
    if any(w in t for w in ("drive", "onedrive", "dropbox", "box", "cloud")) or t == "2":
        return "cloud_csv"
    return None


def cloud_plan(service: str) -> dict:
    """What the bot does when the owner names a cloud service (no code can call connectors directly)."""
    s = (service or "").strip()
    if s.lower() in ("icloud", "icloud drive"):
        return {"service": s, "steps": [{"why": "no connector for iCloud", "fallback": "download"}],
                "message": "iCloud doesn't have a connector I can use, so I'll send you the file to keep in iCloud yourself."}
    return {"service": s, "steps": [
        {"why": "look for a connector for their service", "tool": "SearchPlugins", "query": s},
        {"why": "if found and not connected: offer to add it and show the connect card; continue only after they connect"},
        {"why": f"create ONE dedicated folder \"{DEFAULT_FOLDER}\" and save its id in records.backup.folder_id; "
                "write only inside it; overwrite the CSVs on every change"},
        {"why": "if no connector exists", "fallback": "download"}]}


# ---------- after every write: push to the chosen backup ----------
def _state_path(user_key, root) -> Path:
    return user_data_root(user_key, root) / "records" / "backup_state.json"


def _save_state(user_key, root, st_: dict) -> None:
    p = _state_path(user_key, root); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st_, indent=2) + "\n", encoding="utf-8"); os.chmod(p, DATA_FILE_MODE)


def pending(user_key, root=None) -> dict:
    """What the bot must still do / tell the owner: uploads to run, failures, a download reminder."""
    return load_json(_state_path(user_key, root), {}) or {}


def _cfg(user_key, root):
    p = user_data_root(user_key, root) / "config.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _run(cmd, runner, cwd=None):
    r = runner(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def push_github(user_key, repo, root=None, *, runner=None, attempts=2) -> dict:
    """Export the standard records/ mirror (v3.5.5: pretty JSON per Sheet tab and per table, index.json, README.md,
    cross references; plus records/dump.sql), commit, then for EACH push attempt re-check the repo is PRIVATE first."""
    import subprocess
    try:
        from .memory import check_backup_target
    except ImportError:  # pragma: no cover
        from memory import check_backup_target
    runner = runner or subprocess.run
    repo = validate_repo(repo)
    url = f"https://github.com/{repo}.git"
    work = user_data_root(user_key, root) / "records" / "backup_repo"
    work.mkdir(parents=True, exist_ok=True)
    try:
        from . import sheet_mirror as sm
    except ImportError:  # pragma: no cover
        import sheet_mirror as sm
    for old in list(work.glob("*.csv")) + [work / "dump.sql"]:  # pre-v3.5.5 layout -> records/
        if old.exists():
            old.unlink()
    sm.export_mirror(user_key, work, root)
    c = connect(user_key, root)
    try:
        (work / sm.MIRROR_DIR / "dump.sql").write_text("\n".join(c.iterdump()) + "\n", encoding="utf-8")
    finally:
        c.close()
    if not (work / ".git").exists():
        _run(["git", "init", "-q", "-b", "main"], runner, work)
    if _run(["git", "remote", "get-url", "origin"], runner, work)[0]:
        _run(["git", "remote", "add", "origin", url], runner, work)
    _run(["git", "add", "-A"], runner, work)
    _run(["git", "-c", "user.name=Follower Finder records", "-c", "user.email=records-backup@localhost",
          "commit", "-qm", "records update", "--allow-empty"], runner, work)
    err = ""
    for _ in range(max(1, attempts)):
        code, vis, e = _run(["gh", "repo", "view", repo, "--json", "visibility", "-q", ".visibility"], runner, work)
        _, remote, _ = _run(["git", "remote", "get-url", "origin"], runner, work)
        try:
            if code != 0:
                raise ValueError(f"records backup refused: couldn't confirm the repo is private ({e or 'gh error'})")
            check_backup_target(vis, remote or url)
        except ValueError as exc:
            return {"ok": False, "refused": True, "error": str(exc).replace("memory backup", "records backup")}
        code, _, err = _run(["git", "push", "-q", "origin", "main"], runner, work)
        if code == 0:
            return {"ok": True}
    return {"ok": False, "error": f"push failed after {attempts} tries: {err}"}


def after_write(user_key, root=None, cfg=None, *, runner=None) -> dict:
    cfg = cfg if cfg is not None else _cfg(user_key, root)
    b = section(cfg)["backup"]
    st_ = pending(user_key, root)
    m, now = b["method"], _now()
    res: dict = {"method": m}
    if m == "github" and b.get("repo"):
        r = push_github(user_key, b["repo"], root, runner=runner)
        res |= r
        if r["ok"]:
            st_.pop("needs_attention", None); st_["last_ok"] = now
        else:
            st_["needs_attention"] = {"at": now, "error": r["error"],
                                      "tell_owner": f"Your records are saved here, but the GitHub backup didn't go through: {r['error']}"}
    elif m == "cloud_csv":
        out = user_data_root(user_key, root) / "records" / "outbox"
        files = export_csv(user_key, out, root)
        st_["upload"] = {"at": now, "service": b.get("service"), "plugin_id": b.get("plugin_id"),
                         "folder": b.get("folder"), "folder_id": b.get("folder_id"),
                         "files": [str(f) for f in files if f.suffix == ".csv"],
                         "do": "upload/overwrite these CSVs in that one folder now with the owner's connector, then call records.mark_uploaded"}
        res["upload"] = st_["upload"]
    elif m == "download":
        st_["download_due"] = True; st_["changed_at"] = now
    elif m == "pending":  # v3.5.7: no unsolicited file; records stay local and the reminder re-asks
        st_["changed_at"] = now
    _save_state(user_key, root, st_)
    return res


def mark_uploaded(user_key, root=None, *, ok=True, error=None) -> dict:
    st_ = pending(user_key, root)
    if ok:
        st_.pop("upload", None); st_.pop("needs_attention", None); st_["last_ok"] = _now()
    else:
        st_["needs_attention"] = {"at": _now(), "error": error or "upload failed",
                                  "tell_owner": f"Your records are saved here, but the cloud upload didn't go through ({error}). I'll retry."}
    _save_state(user_key, root, st_)
    return st_


def download_reminder(user_key, root=None) -> dict | None:
    """Weekly routine for the 'download' method: send a fresh zip when anything changed since the last one."""
    st_ = pending(user_key, root)
    if not st_.get("download_due"):
        return None
    zp = export_zip(user_key, user_data_root(user_key, root) / "records" / "outbox" / "follower_finder_records.zip", root)
    st_["download_due"] = False; st_["last_ok"] = _now(); _save_state(user_key, root, st_)
    return {"file": str(zp), "message": "Here's the latest copy of your Follower Finder records. Keep it anywhere safe "
                                        "(your device, iCloud or any cloud). Send it back anytime and I'll restore everything."}


# ---------- restore plans ----------
def restore_from_github(user_key, repo, root=None, *, runner=None) -> dict:
    import subprocess, tempfile
    runner = runner or subprocess.run
    repo = validate_repo(repo)
    with tempfile.TemporaryDirectory() as td:
        code, _, err = _run(["gh", "repo", "clone", repo, str(Path(td) / "r"), "--", "-q", "--depth", "1"], runner)
        if code != 0:
            return {"ok": False, "error": err or "clone failed"}
        r = Path(td) / "r"
        if (r / "records" / "index.json").exists():  # v3.5.5 standard mirror
            try:
                from . import sheet_mirror as sm
            except ImportError:  # pragma: no cover
                import sheet_mirror as sm
            return {"ok": True} | sm.restore_from_mirror(user_key, r, root)
        return {"ok": True} | restore_from_dir(user_key, r, root)


def restore_plan(cfg) -> dict:
    b = section(cfg)["backup"]
    m = b["method"]
    if m == "github":
        return {"method": m, "call": f"records.restore_from_github(user_key, {b.get('repo')!r})"}
    if m == "cloud_csv":
        return {"method": m, "steps": [f"with the {b.get('service')} connector, download the CSVs from \"{b.get('folder')}\" into "
                                       "data/users/<user_key>/records/incoming/", "records.restore_from_dir(user_key, that folder)"]}
    if m == "sheets":
        return {"method": m, "steps": ["read_range Actions, Keepers and List Graveyard (A1:Z)", "records.import_legacy(user_key, sheet_values={...})"]}
    return {"method": m, "steps": ["ask the owner for their last export file (zip or CSV)", "records.restore_from_file(user_key, path)"]}


# ---------- v3.5.1: move existing Sheets owners to terminator.db ----------
MIGRATION_QUESTION = ("Heads up: Follower Finder is retiring Google Sheets for records. Sheets get bulky and slow as "
                      "they grow and cost more tokens to read and write. I'm copying everything from your Sheet into "
                      "your own records file here now (read only, no duplicates). Then pick where I back it up: "
                      "GitHub (recommended), your cloud storage, or a file I send you. Your Sheet stays exactly where "
                      "it is as your old copy; I never delete, clear or change it.")
MIGRATION_OPTIONS = ["Got it, move my records"]  # v3.5.2: migration is not optional (no "Keep Sheets for now")


def migration_card() -> dict:
    return {"question": MIGRATION_QUESTION, "options": list(MIGRATION_OPTIONS)}


def needs_migration(cfg, found_sheet_id: str | None = None) -> bool:
    """A Sheet is still in use as the records backup: migrate (v3.5.2: no choice to keep it). found_sheet_id: a Sheet
    found by legacy_sheet_search for a config with no records section (it counts as Sheets in use)."""
    if found_sheet_id and "records" not in (cfg or {}):
        return True
    return section(cfg)["backup"]["method"] == "sheets"


needs_migration_offer = needs_migration  # pre-v3.5.2 name


def _migration_log(user_key, root, event: dict) -> None:
    """Append-only log of migration notices and moves (records/migration_log.jsonl)."""
    p = user_data_root(user_key, root) / "records" / "migration_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event | {"at": _now()}, ensure_ascii=False) + "\n")
    os.chmod(p, DATA_FILE_MODE)


MIGRATION_STEPS = [
    "send records.migration_card() (the explanation)",
    "read_range Actions, Keepers and List Graveyard (A1:Z) on their sheet (read only; never delete, clear or overwrite it)",
    "records.migrate_from_sheets(user_key, cfg, sheet_values): copies into terminator.db, stops Sheet writes and Google "
    "checks, sends records.setup_card() (no file until they pick the download or ask)",
    "when they pick: records.set_backup(cfg, method); stalls get records.backup_reminder"]


def handle_migration_answer(user_key, answer: str, root=None) -> dict:
    """Any reply (including "keep my Sheet") continues the migration; keeping Sheets is not an option."""
    a = (answer or "").strip().lower()
    _migration_log(user_key, root, {"event": "notified", "reply": a[:60]})
    out = {"action": "migrate", "steps": list(MIGRATION_STEPS)}
    if "keep" in a or "sheet" in a or "no" == a:
        out["message"] = ("I hear you. Sheets is being retired for everyone, but your Sheet stays right where it is as "
                          "your old copy. I'm only copying from it.")
    return out


def migrate_from_sheets(user_key, cfg: dict, sheet_values: dict, method: str | None = None, *, root=None, repo=None, service=None,
                        plugin_id=None, folder=None, found_sheet_id: str | None = None, sheet_formulas=None,
                        tab_order=None) -> dict:
    """Import the sheet tabs (idempotent), switch the backup method, back up once, log it. The Sheet is never touched.

    v3.5.5: sheet_values should be the read_range output of EVERY tab (displayed values) and sheet_formulas the same
    read with valueRenderOption=FORMULA; every tab is also copied in full (sheet_mirror.import_sheet)."""
    if method is not None and method not in CHOOSABLE:
        raise ValueError("pick the new backup: github, cloud_csv or download (a backup can't be skipped)")
    res = import_legacy(user_key, root, sheet_values=sheet_values) if sheet_values else {"after": counts(user_key, root)}
    tabs = None
    if sheet_values:
        try:
            from . import sheet_mirror as sm
        except ImportError:  # pragma: no cover
            import sheet_mirror as sm
        tabs = sm.import_sheet(user_key, sheet_values, sheet_formulas, tab_order=tab_order, root=root)
        res["after"] = counts(user_key, root)
    newcfg = dict(cfg); sh = dict(newcfg.get("sheets") or {})
    if sh.get("spreadsheet_id"):
        sh["legacy_kept"] = True  # kept for reference; never deleted
    newcfg["sheets"] = sh
    if method is None:  # copied; no backup picked yet -> pending (Sheet writes + Google checks stop now)
        b = section(newcfg)["backup"] | {"method": "pending"}
        newcfg["records"] = {"backup": b}
    else:
        newcfg["records"] = set_backup(newcfg, method, repo=repo, service=service, plugin_id=plugin_id, folder=folder)["records"]
    if found_sheet_id and not sh.get("spreadsheet_id"):  # legacy config: the Sheet was found by a Drive search
        newcfg["records"]["legacy_sheet_id"] = found_sheet_id; newcfg["records"]["legacy_kept"] = True
    audit(user_key, "migrated_from_sheets", f"method={method or 'pending'} counts={res['after']}", root)
    backup = after_write(user_key, root, newcfg)
    _migration_log(user_key, root, {"event": "migrated", "method": method or "pending", "counts": res["after"]})
    out = {"config": newcfg, "counts": res["after"], "backup": backup, "tabs": tabs,
           "message": (res.get("message") or "Your records are moved.") + " Your Google Sheet is still there, untouched."}
    if method is None:
        out["next"] = backup_reminder(user_key, root, newcfg)  # setup card (reminder) until they pick; no file
    return out


# ---------- upgrade from an older version ----------
def upgrade_card() -> dict:
    return {"question": UPGRADE_QUESTION, "options": list(UPGRADE_OPTIONS)}


def handle_upgrade_answer(answer: str) -> dict:
    a = (answer or "").strip().lower()
    if a == "no":
        return {"action": "fresh_start", "message": "Great, starting fresh."}
    if a not in ("yes", "not sure"):
        return {"action": "ask_again", "card": upgrade_card()}
    return {"action": "find_records", "steps": [
        {"step": 1, "why": "look for a backup: GitHub (gh repo view <their login>/" + DEFAULT_REPO_NAME + " or the repo in "
                           "their config) or a cloud folder named \"" + DEFAULT_FOLDER + "\"; if found -> restore_from_github / restore_from_dir"},
        {"step": 2, "why": "otherwise find their older Follower Finder among their bots (teammates) and ask it with SendToAgent",
         "message": EXPORT_REQUEST},
        {"step": 3, "why": "import what comes back: records.import_legacy(user_key, old_root=<their files>, export_dir=<CSVs>, sheet_values=<read_range values>)"},
        {"step": 4, "why": "back up immediately: records.after_write(user_key)"},
        {"step": 5, "why": "confirm counts to the owner with records.counts(user_key); the owner does nothing else"}]}


def _rows(values):
    """read_range values (header row first) -> list of dicts."""
    vals = values
    if isinstance(vals, dict):  # Sheets connector read_range: {"values": [[...]]} or {"content": "<csv>"}
        vals = vals.get("values") if vals.get("values") is not None else vals.get("content")
    if isinstance(vals, str):
        vals = list(csv.reader(io.StringIO(vals)))
    if not vals:
        return []
    hdr = [str(h).strip().lower() for h in vals[0]]
    out = []
    for r in vals[1:]:
        if not any(str(x).strip() for x in r):
            continue
        d = {}
        for h, x in zip(hdr, r):
            if h and h not in d:  # repeated header names (e.g. a live-mirror block to the right): first one wins
                d[h] = str(x)
        out.append(d)
    return out


def _pick(r, *names):
    for n in names:
        for k, v in r.items():
            if k == n and v:
                return v
    return None


def _already_removed(user_key, root, uid) -> bool:
    c = connect(user_key, root)
    try:
        return c.execute("SELECT 1 FROM removals WHERE user_id=? AND action IN ('removed','blocked') LIMIT 1",
                         (str(uid),)).fetchone() is not None
    finally:
        c.close()


def import_legacy(user_key, root=None, *, old_root=None, export_dir=None, sheet_values=None) -> dict:
    """Import records from an older version: its per-user folder (graveyard.jsonl, always_keep.json,
    history/history.jsonl, snapshots/), a CSV export folder, and/or log-sheet tab values. Idempotent; then one backup."""
    before = counts(user_key, root)
    if export_dir:
        restore_from_dir(user_key, export_dir, root)
    if old_root:
        o = Path(old_root)
        gy = o / "graveyard.jsonl"
        if gy.exists():
            for line in gy.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record_graveyard_event(user_key, json.loads(line), root, sync=False)
        hist = o / "history" / "history.jsonl"
        if hist.exists():
            for line in hist.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                if e.get("status") in ("REMOVED", "TERMINATED"):  # TERMINATED: alpha-era history entries
                    at = e.get("at") or e.get("terminated_at_iso") or e.get("recorded_at_iso") or "Not available"
                    ok = e.get("confirmed_on_x") or "SUCCESS" in str(e.get("removal_status") or "").upper()
                    record_removal(user_key, (e.get("handle") or "").lstrip("@") or None, e.get("x_user_id"), at=at,
                                   batch=e.get("snapshot_id") or e.get("batch_id"),
                                   result="confirmed on X" if ok else None, root=root, sync=False)
        ak = o / "always_keep.json"
        if ak.exists():
            raw = json.loads(ak.read_text(encoding="utf-8"))
            sync_allowlist(user_key, raw.get("accounts") if isinstance(raw, dict) else raw, root, sync=False)
        snaps = o / "snapshots"
        if snaps.is_dir():
            for d in sorted(x for x in snaps.iterdir() if x.is_dir()):
                n = 0
                cand = d / "candidates.json"
                if cand.exists():
                    try:
                        data = json.loads(cand.read_text(encoding="utf-8"))
                        n = len(data if isinstance(data, list) else data.get("candidates") or data.get("accounts") or [])
                    except ValueError:
                        n = 0
                at = dt.datetime.fromtimestamp(d.stat().st_mtime).astimezone().isoformat(timespec="seconds")
                _write(user_key, root, "INSERT OR IGNORE INTO scans(id,at,count) VALUES(?,?,?)", (d.name, at, n))
    sv = sheet_values or {}
    if isinstance(sv, dict) and isinstance(sv.get("ranges"), list):  # v3.5.6: one read_range call covering many tabs
        sv = {r["sheet"]: r for r in sv["ranges"]}
    for tab, values in sv.items():
        t = tab.strip().lower()
        for r in _rows(values):
            h = (_pick(r, "handle", "owner handle", "x profile / handle") or "").strip().lstrip("@") or None
            uid = _clean_uid(_pick(r, "x user id", "owner id"))
            # stable fallback (never "now") so a re-import can't duplicate rows
            when = _pick(r, "when", "when (with time zone)", "date kept", "kept date (pt)",
                         "removal / action time (pt)") or "Not available"
            act = (_pick(r, "action", "removal method / action") or "").lower()
            if t == "graveyard" and any(w in act for w in ("remov", "block")):  # alpha-era GRAVEYARD tab
                if uid and _already_removed(user_key, root, uid):  # same removal already came from history/graveyard.jsonl
                    continue
                head = act.split("(")[0]  # the method token; the note in brackets can mention blocking
                record_removal(user_key, h, uid, action="blocked" if "block" in head and "remov" not in head
                               else "removed", at=when, batch=_pick(r, "batch id"), result=_pick(r, "result"),
                               reason=_pick(r, "classification reasons / signals"), root=root, sync=False)
            elif t == "protected" and (h or uid):  # alpha-era PROTECTED tab: kept as keeps (never removed)
                _write(user_key, root, "INSERT OR IGNORE INTO allowlist(user_id,handle,added_at,op) VALUES(?,?,?,'add')", (_k(uid), _k(h), when))
            elif t == "actions" and any(w in act for w in ("remov", "block")):
                record_removal(user_key, h, uid, action="blocked" if "block" in act else "removed", at=when,
                               result=_pick(r, "confirmed on x"), reason=_pick(r, "why"), root=root, sync=False)
            elif t == "keepers" and (h or uid):
                _write(user_key, root, "INSERT OR IGNORE INTO allowlist(user_id,handle,added_at,op) VALUES(?,?,?,'add')", (_k(uid), _k(h), when))
            elif t == "list graveyard":
                record_removal(user_key, h, uid, action="listed", at=when, batch=_pick(r, "list id", "list name"),
                               result=_pick(r, "verified result"), root=root, sync=False)
    after = counts(user_key, root)
    backup = after_write(user_key, root)
    return {"before": before, "after": after, "backup": backup,
            "message": (f"Your records are here: {after['removed']} removed, {after['blocked']} blocked, "
                        f"{after['graveyard']} graveyard entries, {after['allowlist_now']} on your keep list, "
                        f"{after['lists_left']} lists left, {after['scans']} scans.")}


# ---------- CLI ----------
def main(argv=None) -> int:
    a = argparse.ArgumentParser(description="Follower Finder records (terminator.db)")
    a.add_argument("--user", default=os.environ.get("MEM_USER"))
    a.add_argument("--root", type=Path, default=None)
    a.add_argument("cmd", choices=("summary", "graveyard", "counts", "export", "restore", "backup", "pending"))
    a.add_argument("args", nargs="*")
    x = a.parse_args(argv)
    uk, r = x.user, x.root
    if not uk:
        print("--user (or MEM_USER) is required", file=sys.stderr); return 2
    if x.cmd == "summary":
        print(weekly_summary(uk, r))
    elif x.cmd == "graveyard":
        for row in graveyard(uk, r):
            print("@%s (%s) %s %s %s" % row)
    elif x.cmd == "counts":
        print(json.dumps(counts(uk, r)))
    elif x.cmd == "export":
        print(export_zip(uk, Path(x.args[0]) / "follower_finder_records.zip", r) if x.args else "export DIR")
    elif x.cmd == "restore":
        print(json.dumps(restore_from_file(uk, x.args[0], r)))
    elif x.cmd == "backup":
        res = after_write(uk, r); print(json.dumps(res))
        return 0 if res.get("ok", True) else 3
    elif x.cmd == "pending":
        print(json.dumps(pending(uk, r)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
