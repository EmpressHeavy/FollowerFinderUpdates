"""Create a new owner's config: data/users/<user_key>/config.json from config/default_user_config.json.

    python -m src.new_user --user <user_key> --handle @your_handle --daily-cap 10 --scan-time 09:00 --lists no   # optional; default no
        [--reminder] [--protect @someone ...] [--manual]
        [--terminate-reminder yes --terminate-time 18:00]   # optional daily chat nudge; default no

Asked at setup: an optional daily removal limit (default no limit; or 1-100, e.g. 10) and the daily scan time (HH:MM, your time
zone), whether to check the lists you're on (default: no). Setup then has the bot create the "Follower Finder Book"
rules doc and the log sheet (Actions, Keepers, In review, List Graveyard, Kept Lists, Protected Lists; the three list
tabs are created either way so turning list checks on later needs no sheet change) with the connector plans returned
by setup_plans().

Also asked: the optional "Terminate reminder" (default: no): once a day, at the time you pick, the bot asks "Hey, want to
terminate some bots?" in chat with Yes / Remind me later / Not today (src/terminate_reminder.py). It is separate from the
front-door reminder.

v3.3.0: every owner gets their own EMPTY private memory (data/users/<user_key>/memory/brain.db, src/memory.py).
Also asked: back it up to a PRIVATE GitHub repo you own? (default: no; uses your own gh login, never a saved token).

Sets user_key, owner_x_handle (required), x_handle and data_root_relative=data/users/<user_key> for you.
Refuses to overwrite an existing config. Modes are set explicitly: folder 0755, config.json 0600.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from . import daily_routine, memory, records, rules_book, sheet_layout, terminate_reminder
    from .storage import PROJECT_ROOT, user_data_root, user_data_relative, validate_user_key, apply_user_permissions
except ImportError:  # pragma: no cover
    import daily_routine, memory, records, rules_book, sheet_layout, terminate_reminder
    from storage import PROJECT_ROOT, user_data_root, user_data_relative, validate_user_key, apply_user_permissions


MAX_DAILY_CAP = 100
SUGGESTED_DAILY_CAP = 10
SETUP_QUESTIONS = [
    f"Want a daily limit on removals? (default: no limit; you still pick every removal, cards of up to 5, and I stop "
    f"at any X warning. Or type a number 1-{MAX_DAILY_CAP}, e.g. {SUGGESTED_DAILY_CAP})",
    "What time should I run the daily scan? (e.g. 09:00, your time zone)",
    "Want a reminder through your front-door bot when the daily cards are ready? (yes/no; this is a ping through a "
    "different bot, separate from the chat nudge asked below)",
    "Want me to also check the X lists you've been added to and flag negative ones? (yes/no; default: no, you can turn it on any time)",
    terminate_reminder.SETUP_QUESTION,
    terminate_reminder.SETUP_TIME_QUESTION,
    memory.SETUP_QUESTION,
    memory.SETUP_REPO_QUESTION,
]


NO_LIMIT_WORDS = ("no limit", "none", "unlimited", "no cap", "uncapped", "off")


def validate_cap(cap) -> int | None:
    """v3.5.32: None or 'no limit' = uncapped (default). Otherwise a whole number 1..MAX_DAILY_CAP."""
    if cap is None or (isinstance(cap, str) and cap.strip().lower() in NO_LIMIT_WORDS):
        return None
    try:
        n = int(cap)
    except (TypeError, ValueError):
        raise ValueError(f"daily cap must be a whole number from 1 to {MAX_DAILY_CAP}, or 'no limit'") from None
    if isinstance(cap, bool) or not 1 <= n <= MAX_DAILY_CAP:
        raise ValueError(f"daily cap must be from 1 to {MAX_DAILY_CAP} (or 'no limit')")
    return n


def create_user(user_key: str, handle: str, *, daily_cap=None, scan_time: str, lists_enabled: bool = False, reminder: bool = False,
                terminate_reminder_on: bool = False, terminate_reminder_time: str | None = None,
                memory_backup: bool = False, memory_backup_repo: str | None = None,
                root: str | Path | None = None, protected_handles=(), manual: bool = False,
                time_zone: str | None = None, records_backup: str = "pending",
                records_repo: str | None = None) -> Path:
    validate_user_key(user_key)
    daily_cap = validate_cap(daily_cap)
    scan_time = daily_routine.validate_time(scan_time)
    if not handle or not handle.lstrip("@"):
        raise ValueError("owner X handle is required")
    handle = "@" + handle.lstrip("@")
    base = Path(root) if root is not None else PROJECT_ROOT
    cfg = json.loads((base / "config" / "default_user_config.json").read_text(encoding="utf-8"))
    d = user_data_root(user_key, root)
    p = d / "config.json"
    if p.exists():
        raise FileExistsError(f"{p} already exists; not overwriting")
    cfg.update(user_key=user_key, x_handle=handle, owner_x_handle=handle,
               data_root_relative=user_data_relative(user_key),
               protected_handles=[h.lstrip("@") for h in protected_handles], hide_verified_forced=False,
               daily_removal_cap=daily_cap,
               daily_scan={"time": scan_time, "reminder_via_front_door": bool(reminder)},
               lists={"enabled": bool(lists_enabled)},
               terminate_reminder=terminate_reminder.initial_section(terminate_reminder_on, terminate_reminder_time, time_zone),
               memory=memory.initial_section(memory_backup, memory_backup_repo))
    cfg["records"] = records.set_backup(cfg, records_backup, repo=records_repo)["records"]  # v3.5.0
    if time_zone:
        cfg["time_zone"] = time_zone
    if manual:
        cfg.update(operating_mode="review", automatic_opt_in=False, first_run_direction="REVIEW_FIRST")
    (d / "scan_raw").mkdir(parents=True, exist_ok=True)  # where follower batches go
    p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    memory.init_db(user_key, root)  # this owner's own EMPTY brain.db (v3.3.0)
    records.init_db(user_key, root)  # this owner's own records file terminator.db (v3.5.0)
    apply_user_permissions(user_key, root)  # folder 0755, config.json 0600
    return p


def setup_plans(cfg: dict, root=None) -> dict:
    """Connector steps the bot runs right after create_user: the Book doc, the log sheet, the daily routine."""
    return {"book": rules_book.create_plan(cfg, root), "sheet": sheet_layout.setup_plan(
        (cfg.get("sheets") or {}).get("title") or sheet_layout.TITLE), "routine": daily_routine.routine_spec(cfg),
            "terminate_reminder": terminate_reminder.routine_spec(cfg),  # None unless the owner turned it on
            "memory": {"db": str(memory.db_path(cfg["user_key"], root)), "session_start": "mem boot",
                       "backup": memory.backup_plan(cfg["user_key"], cfg, root)},
            "records": {"db": str(records.db_path(cfg["user_key"], root)), "status": records.status_line(cfg),
                        "sheet_needed": records.section(cfg)["backup"]["method"] == "sheets"}}  # backup steps empty unless turned on


def main(argv=None) -> int:
    a = argparse.ArgumentParser(description="Create a new Follower Finder owner config")
    a.add_argument("--user", required=True); a.add_argument("--handle", required=True)
    a.add_argument("--protect", action="append", default=[]); a.add_argument("--manual", action="store_true")
    a.add_argument("--daily-cap", default=None, help="optional daily removal limit 1-100, or 'no limit' (default)")
    a.add_argument("--scan-time", required=True, help="HH:MM daily scan time"); a.add_argument("--reminder", action="store_true")
    a.add_argument("--terminate-reminder", choices=("yes", "no"), default="no",
                   help="the bot's own daily chat nudge 'Hey, want to terminate some bots?' (default: no; needs --terminate-time)")
    a.add_argument("--terminate-time", help="HH:MM daily time for the terminate reminder")
    a.add_argument("--memory-backup", choices=("yes", "no"), default="no", help="back up your memory to a PRIVATE GitHub repo you own (default: no)")
    a.add_argument("--memory-repo", help="owner/name of that private repo")
    a.add_argument("--lists", choices=("yes", "no"), default="no", help="check the X lists you're on (default: no)")
    a.add_argument("--root", type=Path, default=PROJECT_ROOT)
    x = a.parse_args(argv)
    try:
        print(create_user(x.user, x.handle, daily_cap=x.daily_cap, scan_time=x.scan_time,
                          reminder=x.reminder, lists_enabled=x.lists == "yes",
                          terminate_reminder_on=x.terminate_reminder == "yes", terminate_reminder_time=x.terminate_time,
                          memory_backup=x.memory_backup == "yes", memory_backup_repo=x.memory_repo, root=x.root, protected_handles=x.protect, manual=x.manual))
    except (ValueError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
