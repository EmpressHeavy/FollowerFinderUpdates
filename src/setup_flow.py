"""Setup flow helpers (v3.5.32): the parts of the getting-started skill that used to be prose only.

Pure functions; the bot does the connector / routine / shell calls the specs describe.
- step order: welcome card first, update check RIGHT after it (whatever the answer), then restore or new setup
- routine specs: review routine from the saved schedule, monthly check-in (28th 10:00), weekly update check (Sun 10:00)
- typed schedule: parse the owner's own words into a cron and echo a one-sentence confirmation before saving
- backup restore: pick the owner's private backup repo and plan the restore steps
- update notifications: the one-line messages for updated / current / rolled_back / fetch errors
"""
from __future__ import annotations

import re

try:
    from . import review_schedule as rs
except ImportError:  # pragma: no cover
    import review_schedule as rs

# ---------------------------------------------------------------- step order
WELCOME_QUESTION = "Have you used Follower Finder before?"
WELCOME_OPTIONS = ["Yes", "No", "Not sure"]
STEPS = ["welcome_card", "update_check", "restore_or_new", "memory", "github_backup", "feedback", "hive_mind",
         "setup_questions", "x_connector_check", "x_web_session", "review_schedule", "routines"]


def next_step(done: str, *, welcome_answer: str | None = None, update_status: str | None = None) -> str:
    """The update check always comes right after the welcome card, whatever the answer. After the update check:
    'updated' -> reload the new skill and continue at Step 2 keeping the welcome answer; returning owners
    (Yes / Not sure) go to restore, new owners go straight to new setup."""
    if done == "welcome_card":
        return "update_check"
    if done == "update_check":
        if update_status == "updated":
            return "reload_skill_then_restore_or_new"
        return "restore_or_new"
    i = STEPS.index(done)
    return STEPS[i + 1] if i + 1 < len(STEPS) else "done"


def is_returning(welcome_answer: str | None) -> bool:
    return (welcome_answer or "").strip().lower() in ("yes", "not sure", "y", "maybe")


# ---------------------------------------------------------------- routines
REVIEW_ROUTINE = "Follower Finder review"
MONTHLY_ROUTINE = "Follower Finder monthly check-in"
UPDATE_ROUTINE = "Follower Finder weekly update check"
MONTHLY_CRON = "0 10 28 * *"
UPDATE_CRON = "0 10 * * 0"


def update_command(home: str = "~/follower_finder") -> str:
    return f"python3 {home}/app/src/updater.py apply --home {home}"


def routine_specs(sched: dict | None, time_zone: str | None, *, home: str = "~/follower_finder") -> list[dict]:
    """All always-on routines. Review: from the saved schedule (not always daily). Monthly: 28th 10 AM.
    Update check: Sunday 10 AM; may be moved, never turned off; the monthly check re-creates it if missing."""
    tz = time_zone or "America/Los_Angeles"
    rev = rs.routine_spec(sched or rs.DEFAULT, tz)
    return [
        {**rev, "name": REVIEW_ROUTINE, "can_turn_off": True,
         "before": "python3 -m src.license_check require scan"},
        {"name": MONTHLY_ROUTINE, "cron": MONTHLY_CRON, "time_zone": tz, "can_turn_off": True,
         "before": "python3 -m src.license_check require scan",
         "does": ["python3 -m src.license_check require scan, then unfollow_back.run_monthly",
                  "python3 -m src.license_check require write before any unfollow",
                  "build_monthly_settings_card.build (settings, review schedule, "
                  "daily limits, backup status, Hive Mind status)", "re-create the weekly update check if missing"]},
        {"name": UPDATE_ROUTINE, "cron": UPDATE_CRON, "time_zone": tz, "can_turn_off": False,
         "command": update_command(home), "on_result": "setup_flow.update_message"},
    ]


def missing_routines(existing_names, specs: list[dict]) -> list[dict]:
    have = {n.strip().lower() for n in existing_names or []}
    return [s for s in specs if s["name"].lower() not in have]


# ---------------------------------------------------------------- typed schedule
DAYS = {"sun": 0, "sunday": 0, "mon": 1, "monday": 1, "tue": 2, "tues": 2, "tuesday": 2, "wed": 3, "wednesday": 3,
        "thu": 4, "thur": 4, "thurs": 4, "thursday": 4, "fri": 5, "friday": 5, "sat": 6, "saturday": 6}
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_TIME_RE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?=\s|$|,|\.)", re.I)


def _parse_time(text: str):
    t = text.lower()
    if "noon" in t:
        return 12, 0
    if "midnight" in t:
        return 0, 0
    for m in _TIME_RE.finditer(t):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
        if not ap and not m.group(2) and not re.search(r"\bat\s+" + m.group(1) + r"\b", t):
            continue  # a bare number like 'every 2 days' is not a time
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
    if "morning" in t:
        return 9, 0
    if "evening" in t or "night" in t:
        return 18, 0
    if "afternoon" in t:
        return 14, 0
    return None


def _fmt_time(h: int, m: int) -> str:
    ap = "AM" if h < 12 else "PM"
    hh = h % 12 or 12
    return f"{hh}:{m:02d} {ap}" if m else f"{hh} {ap}"


def _join(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def parse_typed_schedule(text: str, default_time: str = "18:00") -> dict:
    """Owner's own words -> {sched (review_schedule dict, preset 'custom'), confirm (sentence to echo back),
    time_given}. Raises ValueError when it can't tell; then ask them to rephrase or pick a preset."""
    t = " " + (text or "").lower().strip() + " "
    if not t.strip():
        raise ValueError("empty schedule")
    tm = _parse_time(t)
    time_given = tm is not None
    h, mi = tm if tm else map(int, default_time.split(":"))
    if re.search(r"every other day|every 2 days|every two days|alternate days", t):
        dom, dow, what = "*/2", "*", "every other day"
    elif re.search(r"weekdays?\b|mon(day)?\s*(-|to|through)\s*fri(day)?|work ?days", t):
        dom, dow, what = "*", "1-5", "every weekday (Mon-Fri)"
    elif re.search(r"weekends?\b", t):
        dom, dow, what = "*", "0,6", "every Saturday and Sunday"
    elif re.search(r"\b(every ?day|daily|each day|every night|every morning|every evening)\b", t):
        dom, dow, what = "*", "*", "every day"
    else:
        found = sorted({DAYS[w if w in DAYS else w[:-1]] for w in re.findall(r"[a-z]+", t) if w in DAYS or (w.endswith("s") and len(w) > 5 and w[:-1] in DAYS)})
        if not found:
            raise ValueError("couldn't find days in that schedule")
        dom, dow = "*", ",".join(str(d) for d in found)
        what = ("every " + _join([DAY_NAMES[d] for d in found])) if len(found) < 7 else "every day"
        if len(found) == 7:
            dow = "*"
    cron = f"{mi} {h} {dom} * {dow}"
    desc = f"{what} at {h:02d}:{mi:02d}"
    sched = rs.build("custom", custom_cron=cron, description=desc)
    sched["time"] = f"{h:02d}:{mi:02d}"
    confirm = f"Got it: {what} at {_fmt_time(h, mi)} your time. Sound right?"
    return {"sched": sched, "confirm": confirm, "time_given": time_given}


CONFIRM_YES = ("yes", "y", "yep", "yeah", "sure", "correct", "right", "sounds right", "ok", "okay", "perfect", "👍")


def confirmed(answer: str) -> bool:
    """Save the typed schedule only after the owner confirms the echoed sentence."""
    a = (answer or "").strip().lower().rstrip("!. ")
    return a in CONFIRM_YES or a.startswith(("yes", "yep", "yeah", "sounds good", "that's right", "correct"))


# ---------------------------------------------------------------- backup restore
BACKUP_NAMES = ("my-follower-finder-backup", "follower-finder-memory", "follower-finder-backup")


def find_backup_repo(repos: list[dict], preferred: str = BACKUP_NAMES[0]) -> dict | None:
    """repos = `gh repo list --json name,visibility` output. Only PRIVATE repos; preferred name first."""
    priv = [r for r in repos or [] if str(r.get("visibility", "")).upper() == "PRIVATE"]
    order = (preferred,) + tuple(n for n in BACKUP_NAMES if n != preferred)
    for n in order:
        for r in priv:
            if r.get("name", "").lower() == n:
                return r
    for r in priv:
        if "follower-finder" in r.get("name", "").lower() and "backup" in r.get("name", "").lower():
            return r
    return None


def restore_plan(repo_name: str, home: str = "~/follower_finder") -> list[str]:
    """Shell steps the bot runs to restore. Keeps the current brain.db as brain.db.pre-restore first."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?", repo_name or ""):
        raise ValueError("bad repo name")
    return [f"gh repo view {repo_name} --json visibility  # must be PRIVATE, else stop",
            f"rm -rf {home}/backup && gh repo clone {repo_name} {home}/backup",
            f"test -f {home}/brain.db && cp {home}/brain.db {home}/brain.db.pre-restore || true",
            f"python3 -c \"import sqlite3,pathlib; d=sqlite3.connect('{home}/brain.db'.replace('~', str(pathlib.Path.home())));"
            f" d.executescript(open('{home}/backup/dump.sql'.replace('~', str(pathlib.Path.home()))).read()); d.commit()\""]


def restore_summary(counts: dict) -> str:
    """counts: {interests, situational, languages, review_schedule (description), follows}."""
    parts = []
    if counts.get("interests"):
        parts.append(f"{counts['interests']} interests")
    if counts.get("situational"):
        parts.append(f"{counts['situational']} situational settings")
    if counts.get("languages"):
        parts.append("languages: " + ", ".join(counts["languages"]))
    if counts.get("review_schedule"):
        parts.append("review schedule: " + counts["review_schedule"])
    if counts.get("follows") is not None:
        parts.append(f"{counts['follows']} follow log entries")
    if not parts:
        return "I found your backup but it was empty, so we'll set things up fresh."
    return "Welcome back! I restored " + "; ".join(parts) + ". Has anything changed?"


# ---------------------------------------------------------------- update notifications
def changelog_bullets(changelog: str, version: str, n: int = 4) -> list[str]:
    v = version.lstrip("v")
    m = re.search(r"^##\s*v?" + re.escape(v) + r"\s*$(.*?)(?=^##\s|\Z)", changelog or "", re.M | re.S)
    if not m:
        return []
    return [re.sub(r"^\s*[-*]\s*", "", ln).strip() for ln in m.group(1).splitlines() if ln.strip().startswith(("-", "*"))][:n]


def update_message(result: dict, changelog: str = "", *, failed_weeks: int = 0, during_setup: bool = False) -> str | None:
    """None = stay silent. result = updater.py apply JSON ({status, version})."""
    st, ver = (result or {}).get("status"), (result or {}).get("version") or "?"
    if st == "updated":
        if during_setup:
            return f"I installed the latest version ({ver})."
        bl = changelog_bullets(changelog, ver)
        return f"Follower Finder updated to {ver}." + (" What's new: " + "; ".join(bl) if bl else "")
    if st == "rolled_back":
        return None if during_setup else "I tried to update Follower Finder, but it didn't go cleanly, so I rolled back. I'll retry next week."
    if st in ("error", None) and failed_weeks >= 3 and not during_setup:
        return f"Heads up: I haven't been able to check for Follower Finder updates for {failed_weeks} weeks. I'll keep trying."
    return None


# ---------------------------------------------------------------- x.com web session (v3.5.33)
def x_web_session_step(report, owner_handle: str, *, vault_saved: bool = False) -> dict:
    """Setup step after the X connector check: the bot's own browser must be signed in to the owner's x.com account
    (follow/unfollow clicks happen there, not through the connector). report = the read-only preflight subagent's
    reply to x_web_session.preflight_task(owner_handle). Returns the decision plus the cards to show next
    (sign-in card when signed out / wrong account / code needed; vault offer once signed in) and 'done'."""
    try:
        from . import x_web_session as xws
    except ImportError:  # pragma: no cover
        import x_web_session as xws
    return xws.setup_plan(report, owner_handle, vault_saved=vault_saved)
