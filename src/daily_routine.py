"""Daily scan routine (beta feedback 2026-10-20). Time picked by each owner at setup (config daily_scan.time,
HH:MM in the owner's time zone). Each run: read the Book, scan, then show TWO cards: likely bots (groups of 5)
and a few inactive mutuals. Nothing is removed without the owner's marks. Optional reminder via the owner's
front-door bot (a reminder message is an outgoing message: only if the owner turned it on at setup).

Not the same thing: the optional "Terminate reminder" (src/terminate_reminder.py), the bot's own daily "Hey, want to
terminate some bots?" card in chat. Its Yes button runs scan_steps() below."""
from __future__ import annotations

import re

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validate_time(t: str) -> str:
    t = (t or "").strip()
    if not TIME_RE.match(t):
        raise ValueError("daily scan time must be HH:MM (24-hour), e.g. 09:00")
    return t


def scan_steps(cfg: dict) -> list[str]:
    """The steps of one daily scan. The terminate reminder's "Yes" button runs these same steps now."""
    return (["license: python3 -m src.license_check require scan (mandatory; if it exits non-zero, stop and "
             "show the owner the text it prints, keeping the [Subscribe](...) link clickable; do not scan, "
             "suggest, follow, or change X; do not delete records or the X login). Before a removal, list "
             "leave, or block in this same run: python3 -m src.license_check require write",
             "preflight: recheck Google Sheets + Google Drive, read the Follower Finder Book",
             "scan new followers (signed-in browser: Profile > Followers)",
             "card 1: likely bots (build_candidate_cards.render_cards)",
             "card 2: inactive mutuals (inactive_mutuals.build_card)"]
            + (["lists: scope card first (lists_scan.scope_card): 'Only lists from people I don't follow back' "
                "or 'Show all lists'; that run sticks to the choice",
                "lists: rescan lists you're on (signed-in browser: Profile > Lists > Lists you're on; "
                "lists_scan.build_snapshot), report them",
                "lists: detect soft-blocked owners who added you again (list_runner.detect_returns)",
                "cards: negative lists, 4 per card + Remove / Keep buttons (list_cards.build_cards); then the "
                "final skipped-lists card",
                "on each 'Remove me ...' tap: leave the chosen lists NOW, one at a time (list_flow, "
                "docs/list_runbook.md); write each list to List Graveyard the moment its task finishes",
                "card: repeat offenders (list_cards.build_repeat_offender_card)"]
               if (cfg.get("lists") or {}).get("enabled") else []))


def routine_spec(cfg: dict) -> dict:
    scan = cfg.get("daily_scan") or {}
    t = validate_time(scan.get("time", ""))
    hh, mm = t.split(":")
    tr = cfg.get("terminate_reminder") or {}
    return {"name": "Follower Finder daily scan", "cron": f"{int(mm)} {int(hh)} * * *",
            "time_zone": cfg.get("time_zone") or "America/Los_Angeles",
            "steps": scan_steps(cfg),
            "reminder_via_front_door": bool(scan.get("reminder_via_front_door")),
            # the bot's own daily chat nudge is a separate routine (terminate_reminder.routine_spec); nothing here sends it
            "terminate_reminder": ({"enabled": True, "time": tr.get("time"), "routine": "Follower Finder terminate reminder"}
                                   if tr.get("enabled") else {"enabled": False}),
            "removes_without_marks": False}
