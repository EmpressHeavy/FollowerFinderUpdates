"""Per-user memory (v3.3.0; habits + per-push privacy check v3.4.0): a small private SQLite "brain" for each owner, plus a `mem`-style CLI.

Every owner gets their OWN, initially EMPTY database at data/users/<user_key>/memory/brain.db on their own bot's
computer (created at setup from templates/memory_schema.sql). Nothing is shared between owners and nothing ships
pre-filled. The bot writes to it as things happen (scan summaries, rule/setting changes, removals, list actions,
project status), each write in its own short transaction (never a once-a-day batch), and runs `mem boot` at the start
of every session.

    python -m src.memory --user <user_key> boot "<bot name>"
    python -m src.memory --user <user_key> add TABLE col=value [col=value ...]
    python -m src.memory --user <user_key> fact SUBJECT PREDICATE VALUE      # closes the current row, adds the new one
    python -m src.memory --user <user_key> log KIND SUMMARY...
    python -m src.memory --user <user_key> q "SELECT ..."                    # read-only
    python -m src.memory --user <user_key> find KEYWORDS                     # full-text search ([OLD] = outdated, don't follow)
    python -m src.memory --user <user_key> lessons [TOPIC]                   # check before a task
    python -m src.memory --user <user_key> lesson TOPIC "what went wrong" "what worked"   # after a mistake
    python -m src.memory --user <user_key> supersede rule|capability ID "new way" --why REASON   # never a duplicate
    python -m src.memory --user <user_key> history rule|capability ID         # old methods, oldest to newest
    python -m src.memory --user <user_key> backup                            # optional; private repo re-checked before every push
    (tools/mem is a shortcut: MEM_USER=<user_key> tools/mem boot)

NEVER store secrets: passwords, tokens, API keys, cookies, card numbers, recovery codes. Writes that look like one are
refused (SecretRejected).

Habits (v3.4.0): start every session with `boot "<bot name>"`; check `lessons` before a task; when know-how changes use
`supersede` instead of adding a duplicate; record each mistake with `lesson`; see old methods with `history`; never follow a
row marked [OLD]; nothing is ever deleted (old rows are marked superseded / avoid and kept).

Optional backup (OFF by default; offered at setup and in the monthly check-in): an export (schema + JSONL per table)
pushed to a PRIVATE GitHub repo the owner owns, using the owner's own `gh` login. No token is ever written to a file,
the config, this template or a remote URL; a repo that isn't private is refused.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

try:
    from .storage import PROJECT_ROOT, user_data_root, validate_user_key, DATA_FILE_MODE, DIR_MODE
except ImportError:  # pragma: no cover
    from storage import PROJECT_ROOT, user_data_root, validate_user_key, DATA_FILE_MODE, DIR_MODE

TABLES = ("entity", "person", "bot", "account_platform", "quest", "project", "rule", "capability", "routine",
          "fact", "event_log", "lesson")
UNFOLLOW_BACK_TABLES = ("tracked_follow", "unfollow_back_log", "flagged_account")  # v3.5.28, exported too
STATUS_TABLES = {"rule": "text", "capability": "description"}
_MIGRATIONS = {  # columns added after v3.3.0; added in place to existing databases, nothing dropped
    "rule": [("status", "TEXT DEFAULT 'current'"), ("reason", "TEXT"), ("changed_on", "TEXT")],
    "capability": [("superseded_by", "INTEGER"), ("status", "TEXT DEFAULT 'current'"), ("reason", "TEXT"), ("changed_on", "TEXT")],
    "fact": [("subject", "TEXT")],
}
BOT_NAME = "Follower Finder"
EVENT_KINDS = ("scan", "rule_change", "setting_change", "removal", "list_action", "project_status", "note")
SAFETY_RULES = (
    "Never store secrets (passwords, tokens, API keys, cookies, card numbers, recovery codes) in memory.",
    "Never take an X action, message or post unless the owner asked for it or their chosen mode allows it.",
    "Read the Follower Finder Book and run preflight before any scan or removal.",
    "Before saying something can't be done, search for a connector (SearchPlugins) and try the signed-in browser.",
    "Never follow a row marked [OLD]; nothing in memory is deleted.",
)
SETUP_QUESTION = ("Want me to back up your private Follower Finder memory to a GitHub repo you own? (yes/no; default: "
                  "no. It must be a private repo, and I use your own GitHub login; no tokens are saved anywhere)")
SETUP_REPO_QUESTION = "Which private repo should I use? (owner/name, e.g. yourname/follower-finder-memory; only if you said yes)"
DEFAULTS = {"enabled": True, "backup": {"enabled": False, "repo": None, "last_backup_at": None}}


class SecretRejected(ValueError):
    """A write that looks like it contains a secret."""


_SECRET_PATTERNS = [re.compile(p, re.I) for p in (
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_[p]at_[A-Za-z0-9_]{20,}", r"\bsk-[A-Za-z0-9_-]{16,}", r"xox[abprs]-[A-Za-z0-9-]{10,}",
    r"AKIA[0-9A-Z]{16}", r"AIza[0-9A-Za-z_-]{30,}", r"ya29\.[0-9A-Za-z_-]{20,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", r"\bbearer\s+[A-Za-z0-9._~+/-]{16,}",
    r"\b(pass(word|wd|code)?|pwd|secret|api[_ -]?key|access[_ -]?token|auth[_ -]?token|refresh[_ -]?token|session[_ -]?id|cookie|otp|2fa code|recovery code)\b\s*[:=]\s*\S+",
    r"(auth_token|ct0)=[A-Za-z0-9]{16,}", r"https?://[^/\s:@]+:[^/\s@]+@",
)]
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits: str) -> bool:
    s, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        s += d; alt = not alt
    return s % 10 == 0


def check_no_secret(*values) -> None:
    """Raise SecretRejected if any value looks like a password, token, key or card number."""
    for v in values:
        if v is None:
            continue
        t = str(v)
        if any(p.search(t) for p in _SECRET_PATTERNS):
            raise SecretRejected("refused: that looks like a secret (password/token/key). Secrets are never stored in memory.")
        for m in _CARD_RE.finditer(t):
            d = re.sub(r"\D", "", m.group())
            if 13 <= len(d) <= 19 and _luhn(d) and len(set(d)) > 1:
                raise SecretRejected("refused: that looks like a card number. Secrets are never stored in memory.")


# ---------- paths and setup ----------
def db_path(user_key: str, root=None) -> Path:
    return user_data_root(user_key, root) / "memory" / "brain.db"


def schema_sql(root=None) -> str:
    base = Path(root) if root is not None else PROJECT_ROOT
    p = base / "templates" / "memory_schema.sql"
    if not p.exists():
        p = PROJECT_ROOT / "templates" / "memory_schema.sql"
    return p.read_text(encoding="utf-8")


def init_db(user_key: str, root=None) -> Path:
    """Create this owner's EMPTY brain.db (idempotent; never copies anyone's rows). Folder 0755, db 0600."""
    validate_user_key(user_key)
    p = db_path(user_key, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, DIR_MODE)
    c = sqlite3.connect(p)
    try:
        c.executescript(schema_sql(root))
        _migrate(c)
        c.commit()
    finally:
        c.close()
    _fix_modes(p)
    return p


def _migrate(c) -> None:
    for table, cols in _MIGRATIONS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    c.execute("UPDATE fact SET subject=(SELECT name FROM entity WHERE id=fact.subject_id) WHERE subject IS NULL")


def _fix_modes(p: Path) -> None:
    for f in (p, Path(str(p) + "-wal"), Path(str(p) + "-shm")):
        if f.exists():
            os.chmod(f, DATA_FILE_MODE)


def connect(user_key: str, root=None, *, readonly: bool = False) -> sqlite3.Connection:
    p = db_path(user_key, root)
    if not p.exists() and readonly:
        raise FileNotFoundError(f"no memory yet for {user_key}; run setup (memory.init_db)")
    if not readonly:
        init_db(user_key, root)  # creates or upgrades in place (idempotent)
    if readonly:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        c.execute("PRAGMA query_only=ON")
    else:
        c = sqlite3.connect(p, isolation_level=None, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


class _Tx:
    """One short write transaction (BEGIN IMMEDIATE ... COMMIT); rolled back on error."""
    def __init__(self, user_key, root=None):
        self.user_key, self.root = user_key, root

    def __enter__(self):
        self.c = connect(self.user_key, self.root)
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, et, ev, tb):
        try:
            self.c.execute("ROLLBACK" if et else "COMMIT")
        finally:
            self.c.close()
            _fix_modes(db_path(self.user_key, self.root))
        return False


def _eid(c, name: str, typ: str = "thing", create: bool = True):
    r = c.execute("SELECT id FROM entity WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if r:
        return r[0]
    if not create:
        return None
    return c.execute("INSERT INTO entity(type,name) VALUES(?,?)", (typ, name)).lastrowid


def _idx(c, src, ref, body):
    c.execute("INSERT INTO notes_fts(src,ref,body) VALUES(?,?,?)", (src, str(ref), body))


_IDENT = re.compile(r"^[a-z_]+$")


# ---------- commands ----------
def add(user_key: str, table: str, values: dict, root=None) -> int:
    if table not in TABLES or table in ("fact", "event_log", "lesson"):
        raise ValueError(f"add works on: {', '.join(t for t in TABLES if t not in ('fact', 'event_log', 'lesson'))}")
    if not values or not all(_IDENT.match(k) for k in values):
        raise ValueError("give column=value pairs")
    check_no_secret(*values.values())
    with _Tx(user_key, root) as c:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        bad = set(values) - cols
        if bad:
            raise ValueError(f"unknown column(s) for {table}: {', '.join(sorted(bad))}")
        rid = c.execute(f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' * len(values))})",
                        tuple(values.values())).lastrowid
        _idx(c, table, rid, " ".join(str(v) for v in values.values()))
    return rid


def fact(user_key: str, subject: str, predicate: str, value: str, *, source: str | None = None, root=None) -> dict:
    """Close the subject's current row for this predicate (valid_to=today) and insert the new value."""
    check_no_secret(subject, predicate, value, source)
    today = dt.date.today().isoformat()
    with _Tx(user_key, root) as c:
        sid = _eid(c, subject)
        cur = c.execute("SELECT id,value FROM fact WHERE subject_id=? AND predicate=? AND valid_to IS NULL",
                        (sid, predicate)).fetchall()
        if any(v == value for _, v in cur):
            return {"id": None, "closed": 0, "unchanged": True}
        c.execute("UPDATE fact SET valid_to=? WHERE subject_id=? AND predicate=? AND valid_to IS NULL", (today, sid, predicate))
        rid = c.execute("INSERT INTO fact(subject_id,subject,predicate,value,valid_from,source,recorded_by_bot) VALUES(?,?,?,?,?,?,?)",
                        (sid, subject, predicate, value, today, source, BOT_NAME)).lastrowid
        _idx(c, "fact", rid, f"{subject} {predicate} {value}")
    return {"id": rid, "closed": len(cur), "unchanged": False}


def log(user_key: str, kind: str, summary: str, *, entity: str | None = None, root=None) -> int:
    """Append one event (its own short transaction)."""
    if not str(summary).strip():
        raise ValueError("log needs a summary")
    check_no_secret(kind, summary, entity)
    with _Tx(user_key, root) as c:
        bid = _eid(c, BOT_NAME, "bot")
        en = _eid(c, entity) if entity else None
        rid = c.execute("INSERT INTO event_log(bot_id,entity_id,kind,summary) VALUES(?,?,?,?)", (bid, en, kind, summary)).lastrowid
        _idx(c, "event_log", rid, f"{kind} {summary}")
    return rid


def q(user_key: str, sql: str, params=(), root=None) -> tuple[list[str], list[tuple]]:
    """Read-only SELECT (query_only connection)."""
    if not re.match(r"^\s*(select|with)\b", sql, re.I):
        raise ValueError("q is read-only: SELECT/WITH only")
    c = connect(user_key, root, readonly=True)
    try:
        cur = c.execute(sql, params)
        return [d[0] for d in cur.description or []], cur.fetchall()
    finally:
        c.close()


def find(user_key: str, keywords: str, limit: int = 20, root=None) -> list[tuple]:
    terms = [t for t in re.findall(r"[\w@.-]+", keywords) if t]
    if not terms:
        return []
    match = " ".join('"' + t.replace('"', '') + '"' for t in terms)
    c = connect(user_key, root, readonly=True)
    try:
        hits = c.execute("SELECT src,ref,snippet(notes_fts,2,'[',']','...',12) FROM notes_fts WHERE notes_fts MATCH ? "
                         "ORDER BY rank LIMIT ?", (match, limit)).fetchall()
        out = []
        for src, ref, snip in hits:
            old = False
            if src in STATUS_TABLES or src == "lesson":
                r = c.execute(f"SELECT status FROM {src} WHERE id=?", (ref,)).fetchone()
                old = bool(r and r[0] in ("superseded", "avoid"))
            out.append((src, ref, ("[OLD] " if old else "") + snip))
        return out
    finally:
        c.close()


def boot(user_key: str, root=None, bot: str | None = None) -> str:
    """Session-start summary (`mem boot "<bot name>"`): safety rules, current rules, top lessons, quests, projects,
    routines, current settings, recent events. Superseded rows are left out."""
    init_db(user_key, root)
    bot = bot or BOT_NAME
    c = connect(user_key, root, readonly=True)
    j = lambda xs: "; ".join(xs) or "(none)"
    try:
        safety = list(SAFETY_RULES) + [t for (t,) in c.execute(
            "SELECT text FROM rule WHERE coalesce(status,'current')='current' AND superseded_by IS NULL AND category='safety' ORDER BY id")]
        rules = [t for (t,) in c.execute("SELECT text FROM rule WHERE coalesce(status,'current')='current' AND superseded_by IS NULL AND (category IS NULL OR category<>'safety') ORDER BY id DESC LIMIT 10")]
        lessons_ = [f"#{i} avoid {w} -> {r}" for i, w, r in c.execute(
            "SELECT id,wrong,right FROM lesson WHERE status='current' ORDER BY uses DESC, id DESC LIMIT 5")]
        quests = [f"{t}{'@' + d if d else ''}" for t, d in c.execute(
            "SELECT title,due FROM quest WHERE status='open' ORDER BY due IS NULL,due,id")]
        projects = [f"{t}: {s or '?'}" + (f" (waiting on {w})" if w else "") for t, s, w in c.execute(
            "SELECT title,status,waiting_on FROM project WHERE coalesce(status,'') NOT IN ('done','closed') ORDER BY id")]
        routines = [f"{s or ''} {d}".strip() for s, d in c.execute("SELECT schedule,description FROM routine WHERE active=1 ORDER BY id")]
        facts = [f"{n}.{p}={v}" for n, p, v in c.execute(
            "SELECT e.name,f.predicate,f.value FROM fact f JOIN entity e ON e.id=f.subject_id WHERE f.valid_to IS NULL ORDER BY f.id DESC LIMIT 15")]
        recent = [f"{ts} {k}: {s}" for ts, k, s in c.execute("SELECT ts,kind,summary FROM event_log ORDER BY id DESC LIMIT 8")]
    finally:
        c.close()
    return "\n".join([f"MEMORY ({user_key})", f"BOT: {bot}", "SAFETY: " + j(safety), "RULES: " + j(rules),
                      "LESSONS: " + j(lessons_), "QUESTS: " + j(quests),
                      "PROJECTS: " + j(projects), "ROUTINES: " + j(routines), "CURRENT: " + j(facts),
                      "RECENT: " + j(recent),
                      "more: mem lessons TOPIC | mem find KW | mem history rule ID | mem q \"SELECT ...\" ([OLD] = don't follow)"])


# ---------- lessons, supersede, history (nothing is ever deleted) ----------
def lesson(user_key: str, topic: str, wrong: str, right: str, *, bot: str | None = None, root=None) -> dict:
    """Record a mistake and what worked. The same words again bump `uses` instead of adding a duplicate."""
    if not all(str(x or "").strip() for x in (topic, wrong, right)):
        raise ValueError('lesson TOPIC "what went wrong" "what worked"')
    check_no_secret(topic, wrong, right)
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _Tx(user_key, root) as c:
        r = c.execute("SELECT id FROM lesson WHERE topic=? AND wrong=? AND right=?", (topic, wrong, right)).fetchone()
        if r:
            c.execute("UPDATE lesson SET uses=uses+1,last=? WHERE id=?", (now, r[0]))
            return {"id": r[0], "new": False}
        lid = c.execute("INSERT INTO lesson(bot,topic,wrong,right,added,last) VALUES(?,?,?,?,?,?)",
                        (bot or BOT_NAME, topic, wrong, right, now, now)).lastrowid
        _idx(c, "lesson", lid, f"{topic} avoid {wrong} -> {right}")
    return {"id": lid, "new": True}


def lessons(user_key: str, topic: str | None = None, limit: int = 10, root=None) -> list[tuple]:
    """Current lessons (most used first), optionally for one topic. Check these before a task."""
    sql = "SELECT id,topic,wrong,right,uses FROM lesson WHERE status='current'"
    params: list = []
    if topic:
        sql += " AND (topic LIKE ? OR wrong LIKE ? OR right LIKE ?)"
        params += [f"%{topic}%"] * 3
    return q(user_key, sql + " ORDER BY uses DESC, id DESC LIMIT ?", tuple(params + [limit]), root=root)[1]


def supersede(user_key: str, table: str, old_id: int, new_text: str, *, why: str, root=None) -> dict:
    """Replace outdated know-how: the old rule/capability is kept but marked superseded (pointing at the new one), a new
    current row is added and a lesson records old -> new. Use this instead of adding a duplicate."""
    if table not in STATUS_TABLES:
        raise ValueError("supersede works on: rule, capability")
    if not str(new_text).strip() or not str(why or "").strip():
        raise ValueError('supersede TABLE ID "new way" --why REASON')
    check_no_secret(new_text, why)
    col, d = STATUS_TABLES[table], dt.date.today().isoformat()
    with _Tx(user_key, root) as c:
        if table == "rule":
            o = c.execute("SELECT scope,category,text,coalesce(status,'current') FROM rule WHERE id=?", (old_id,)).fetchone()
        else:
            o = c.execute("SELECT bot_id,source,description,coalesce(status,'current') FROM capability WHERE id=?", (old_id,)).fetchone()
        if not o:
            raise ValueError(f"{table}#{old_id} not found")
        if o[3] != "current":
            raise ValueError(f"{table}#{old_id} is {o[3]}; supersede its current successor instead (see history)")
        if table == "rule":
            nid = c.execute("INSERT INTO rule(scope,category,text,set_by,set_on,status,reason,changed_on) VALUES(?,?,?,?,?,'current',?,?)",
                            (o[0], o[1], new_text, "owner", d, why, d)).lastrowid
        else:
            nid = c.execute("INSERT INTO capability(bot_id,description,learned_on,source,status,reason,changed_on) VALUES(?,?,?,?,'current',?,?)",
                            (o[0], new_text, d, "supersede", why, d)).lastrowid
        c.execute(f"UPDATE {table} SET status='superseded',superseded_by=?,reason=?,changed_on=? WHERE id=?", (nid, why, d, old_id))
        now = dt.datetime.now().isoformat(timespec="seconds")
        lid = c.execute("INSERT INTO lesson(bot,topic,wrong,right,added,last,ref_table,ref_id) VALUES(?,?,?,?,?,?,?,?)",
                        (BOT_NAME, table, f"{o[2]} ({why})", new_text, now, now, table, nid)).lastrowid
        _idx(c, table, nid, new_text)
        _idx(c, "lesson", lid, f"{table} avoid {o[2]} -> {new_text}")
        bid = _eid(c, BOT_NAME, "bot")
        c.execute("INSERT INTO event_log(bot_id,kind,summary) VALUES(?,?,?)", (bid, "supersede", f"{table}#{old_id} -> #{nid}: {why}"))
    return {"new_id": nid, "old_id": old_id, "lesson_id": lid}


def history(user_key: str, table: str, row_id: int, root=None) -> list[dict]:
    """The whole chain for a rule/capability, oldest first (status, date, reason, text)."""
    if table not in STATUS_TABLES:
        raise ValueError("history works on: rule, capability")
    col = STATUS_TABLES[table]
    c = connect(user_key, root, readonly=True)
    try:
        rid, seen = row_id, set()
        while True:  # walk back to the first version
            r = c.execute(f"SELECT id FROM {table} WHERE superseded_by=?", (rid,)).fetchone()
            if not r or r[0] in seen:
                break
            seen.add(r[0]); rid = r[0]
        out, seen = [], set()
        while rid and rid not in seen:
            seen.add(rid)
            r = c.execute(f"SELECT id,coalesce(status,'current'),changed_on,reason,{col},superseded_by FROM {table} WHERE id=?", (rid,)).fetchone()
            if not r:
                break
            out.append({"id": r[0], "status": r[1], "changed_on": r[2], "reason": r[3], "text": r[4],
                        "old": r[1] != "current"})
            rid = r[5]
        return out
    finally:
        c.close()


# ---------- write-as-it-happens helpers (each is one short transaction) ----------
def record_scan(user_key, *, scanned: int, bots: int, review: int, safe: int, follower_count=None, root=None) -> int:
    s = f"scanned {scanned}: {bots} likely bots, {review} need a look, {safe} safe" + (f"; followers {follower_count}" if follower_count is not None else "")
    return log(user_key, "scan", s, root=root)


def record_rule_change(user_key, change: str, *, setting: str | None = None, value: str | None = None, root=None) -> int:
    rid = log(user_key, "setting_change" if setting else "rule_change", change, root=root)
    if setting:
        fact(user_key, "settings", setting, str(value), source="owner", root=root)
    else:
        add(user_key, "rule", {"scope": "follower-finder", "category": "owner", "text": change, "set_by": "owner",
                               "set_on": dt.date.today().isoformat()}, root=root)
    return rid


def record_removal(user_key, handle: str, *, result: str = "removed", reason: str | None = None, root=None) -> int:
    h = "@" + str(handle).lstrip("@")
    return log(user_key, "removal", f"{h} {result}" + (f" ({reason})" if reason else ""), entity=h, root=root)


def record_list_action(user_key, list_name: str, action: str, *, result: str | None = None, root=None) -> int:
    return log(user_key, "list_action", f"{action}: {list_name}" + (f" -> {result}" if result else ""), root=root)


def record_project_status(user_key, title: str, status: str, *, waiting_on: str | None = None, root=None) -> int:
    check_no_secret(title, status, waiting_on)
    with _Tx(user_key, root) as c:
        c.execute("INSERT INTO project(title,status,waiting_on) VALUES(?,?,?) ON CONFLICT(title) DO UPDATE SET "
                  "status=excluded.status, waiting_on=excluded.waiting_on, updated_at=datetime('now','localtime')",
                  (title, status, waiting_on))
    return log(user_key, "project_status", f"{title}: {status}", root=root)


# ---------- config section ----------
def section(cfg: dict | None) -> dict:
    s = json.loads(json.dumps(DEFAULTS))
    m = (cfg or {}).get("memory") or {}
    s["enabled"] = bool(m.get("enabled", True))
    s["backup"].update({k: v for k, v in (m.get("backup") or {}).items() if k in s["backup"]})
    s["backup"]["enabled"] = bool(s["backup"]["enabled"])
    return s


_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


def validate_repo(repo: str) -> str:
    r = (repo or "").strip()
    r = re.sub(r"^(https?://)?github\.com/", "", r).removesuffix(".git")
    if "@" in r or ":" in r or not _REPO_RE.match(r):
        raise ValueError("give the repo as owner/name (no URL with a login or token in it)")
    check_no_secret(r)
    return r


def initial_section(backup_on: bool = False, repo: str | None = None) -> dict:
    s = json.loads(json.dumps(DEFAULTS))
    if backup_on:
        if not repo:
            raise ValueError("memory backup needs a private repo you own (owner/name)")
        s["backup"].update(enabled=True, repo=validate_repo(repo))
    return s


def set_backup(cfg: dict, enabled: bool, repo: str | None = None) -> dict:
    """Monthly check-in change: returns the new memory section plus the Book change line."""
    s = section(cfg)
    if enabled:
        s["backup"].update(enabled=True, repo=validate_repo(repo or s["backup"].get("repo") or ""))
        line = f"Memory backup turned ON (private repo {s['backup']['repo']})."
    else:
        s["backup"]["enabled"] = False
        line = "Memory backup turned OFF."
    return {"memory": s, "book_change": line}


def status_line(cfg: dict | None) -> str:
    b = section(cfg)["backup"]
    if b["enabled"]:
        return (f"Memory backup: ON to your private repo {b['repo']}" + (f" (last {b['last_backup_at']})" if b.get("last_backup_at") else "")
                + ". Tap 6\u20137 to turn it off or change the repo.")
    return "Memory backup: OFF (default). Tap 6\u20137 to back up your private memory to a private GitHub repo you own."


# ---------- optional backup ----------
def export(user_key: str, out_dir, root=None) -> list[Path]:
    """Write schema.sql + one JSONL per table (like a git-friendly dump). Refuses if a row looks like a secret."""
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    c = connect(user_key, root, readonly=True)
    files = []
    try:
        (out / "schema.sql").write_text("\n".join(r[0] + ";" for r in c.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'notes_fts_%' ORDER BY name")) + "\n", encoding="utf-8")
        files.append(out / "schema.sql")
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in TABLES + tuple(x for x in UNFOLLOW_BACK_TABLES if x in have):
            cur = c.execute(f"SELECT * FROM {t} ORDER BY rowid")
            cols = [d[0] for d in cur.description]
            lines = []
            for row in cur:
                check_no_secret(*row)
                lines.append(json.dumps(dict(zip(cols, row)), ensure_ascii=False, sort_keys=True))
            (out / f"{t}.jsonl").write_text("".join(x + "\n" for x in lines), encoding="utf-8")
            files.append(out / f"{t}.jsonl")
    finally:
        c.close()
    return files


def backup_plan(user_key: str, cfg: dict, root=None) -> dict:
    """Shell steps the bot runs (on a yes only) with the owner's OWN `gh` login. No tokens anywhere: the remote is a
    plain https://github.com/<owner>/<name>.git URL and auth comes from `gh auth setup-git`. The visibility check must
    print PRIVATE or the push is skipped."""
    b = section(cfg)["backup"]
    if not b["enabled"]:
        return {"enabled": False, "steps": [], "note": "Memory backup is off (default)."}
    repo = validate_repo(b["repo"])
    work = user_data_root(user_key, root) / "memory" / "backup_repo"
    url = f"https://github.com/{repo}.git"
    return {"enabled": True, "repo": repo, "work_dir": str(work), "steps": [
        {"why": "owner's own GitHub login must be active", "cmd": ["gh", "auth", "status"]},
        {"why": "create the repo PRIVATE if it doesn't exist yet (ask first)", "cmd": ["gh", "repo", "create", repo, "--private"], "only_if_missing": True},
        {"why": "refuse unless private", "cmd": ["gh", "repo", "view", repo, "--json", "visibility", "-q", ".visibility"], "must_equal": "PRIVATE"},
        {"why": "git uses gh's credential helper (no token in URL or files)", "cmd": ["gh", "auth", "setup-git"]},
        {"why": "export, commit, re-check the repo is PRIVATE, then push (memory.push_backup refuses the push otherwise)",
         "cmd": ["python3", "-m", "src.memory", "--user", user_key, "backup"]},
    ], "remote": url, "note": "Every push goes through memory.push_backup, which checks visibility right before that push."}


def _run(cmd, runner, cwd=None):
    r = runner(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def push_backup(user_key: str, cfg: dict, root=None, *, runner=None) -> dict:
    """Export, commit and push the backup. Before EVERY push the repo's visibility is read with the owner's own `gh`
    login and the push is refused unless it is PRIVATE and the remote URL carries no login or token."""
    import subprocess
    runner = runner or subprocess.run
    b = section(cfg)["backup"]
    if not b["enabled"]:
        return {"pushed": False, "reason": "memory backup is off (default)"}
    repo = validate_repo(b["repo"])
    url = f"https://github.com/{repo}.git"
    work = user_data_root(user_key, root) / "memory" / "backup_repo"
    work.mkdir(parents=True, exist_ok=True)
    export(user_key, work, root)
    for cmd in (["git", "init", "-q", "-b", "main"] if not (work / ".git").exists() else None,
                ["git", "remote", "add", "origin", url] if _run(["git", "remote", "get-url", "origin"], runner, work)[0] else None,
                ["git", "add", "-A"],
                ["git", "-c", "user.name=Follower Finder memory", "-c", "user.email=memory-backup@localhost", "commit", "-qm", "memory backup", "--allow-empty"]):
        if cmd:
            _run(cmd, runner, work)
    code, vis, err = _run(["gh", "repo", "view", repo, "--json", "visibility", "-q", ".visibility"], runner, work)
    _, remote, _ = _run(["git", "remote", "get-url", "origin"], runner, work)
    try:
        if code != 0:
            raise ValueError(f"memory backup refused: couldn't confirm the repo is private ({err or 'gh error'})")
        check_backup_target(vis, remote or url)
    except ValueError as exc:
        return {"pushed": False, "reason": str(exc)}
    code, _, err = _run(["git", "push", "-q", "origin", "main"], runner, work)
    if code != 0:
        return {"pushed": False, "reason": f"push failed: {err}"}
    return {"pushed": True, "repo": repo, "at": dt.datetime.now().isoformat(timespec="seconds")}


def check_backup_target(visibility: str, remote_url: str) -> None:
    """Guard used before pushing: private only, and no credentials in the remote URL."""
    if (visibility or "").strip().upper() != "PRIVATE":
        raise ValueError("memory backup refused: the repo is not private")
    if "@" in remote_url.split("github.com")[0] or re.search(r"https?://[^/]*:[^/]*@", remote_url) or any(p.search(remote_url) for p in _SECRET_PATTERNS):
        raise ValueError("memory backup refused: the remote URL contains a login or token")


# ---------- CLI ----------
def main(argv=None) -> int:
    a = argparse.ArgumentParser(description="Follower Finder per-user memory")
    a.add_argument("--user", default=os.environ.get("MEM_USER"))
    a.add_argument("--root", type=Path, default=None)
    a.add_argument("cmd", choices=("init", "boot", "add", "fact", "log", "q", "find", "lesson", "lessons", "supersede",
                                   "history", "backup"))
    a.add_argument("args", nargs="*")
    a.add_argument("--why")
    x = a.parse_args(argv)
    try:
        uk = validate_user_key(x.user)
        r, args = x.root, x.args
        if x.cmd == "init":
            print(init_db(uk, r))
        elif x.cmd == "boot":
            print(boot(uk, r, bot=" ".join(args) or None))
        elif x.cmd == "lesson":
            res = lesson(uk, args[0], args[1], args[2], root=r)
            print(f"lesson#{res['id']}" + ("" if res["new"] else " used again"))
        elif x.cmd == "lessons":
            for row in lessons(uk, " ".join(args) or None, root=r):
                print("#%s [%s] avoid %s -> %s (uses %s)" % row)
        elif x.cmd == "supersede":
            res = supersede(uk, args[0], int(args[1]), " ".join(args[2:]), why=x.why, root=r)
            print(f"{args[0]}#{res['new_id']} current; #{res['old_id']} superseded; lesson#{res['lesson_id']}")
        elif x.cmd == "history":
            for h in history(uk, args[0], int(args[1]), root=r):
                print(("[OLD] " if h["old"] else "") + f"#{h['id']} {h['status']} {h['changed_on'] or ''} {h['reason'] or ''}: {h['text']}")
        elif x.cmd == "backup":
            cfgp = user_data_root(uk, r) / "config.json"
            res = push_backup(uk, json.loads(cfgp.read_text(encoding="utf-8")), r)
            print(json.dumps(res))
            if not res["pushed"]:
                return 3
        elif x.cmd == "add":
            kv = dict(s.split("=", 1) for s in args[1:] if "=" in s)
            print(f"{args[0]}#{add(uk, args[0], kv, r)}")
        elif x.cmd == "fact":
            res = fact(uk, args[0], args[1], " ".join(args[2:]), root=r)
            print("unchanged" if res["unchanged"] else f"fact#{res['id']} (closed {res['closed']})")
        elif x.cmd == "log":
            print(f"log#{log(uk, args[0], ' '.join(args[1:]), root=r)}")
        elif x.cmd == "q":
            cols, rows = q(uk, " ".join(args), root=r)
            print("|".join(cols))
            for row in rows:
                print("|".join("" if v is None else str(v) for v in row))
        elif x.cmd == "find":
            for row in find(uk, " ".join(args), root=r):
                print("|".join(str(v) for v in row))
    except (ValueError, IndexError, FileNotFoundError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
