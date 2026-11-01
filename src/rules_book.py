"""The per-user "Follower Finder Book": a Google Doc with the owner's rules (beta feedback 2026-10-20).

Created at setup from templates/follower_finder_book.md (generic; filled from the owner's config only). Read
before every scan or removal (preflight.record_book_read, required by preflight.check). Updated (append-only)
whenever the owner changes a rule. Plans are executed by the bot with the Google Docs connector.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

try:
    from .storage import PROJECT_ROOT
except ImportError:  # pragma: no cover
    from storage import PROJECT_ROOT

TITLE = "Follower Finder Book"
SECTIONS = ["Job", "Always kept", "How bots are judged", "Lists", "Approval and pace", "Where things live", "Routines",
            "Lessons learned", "Change log"]


def render(cfg: dict, root=None, *, today: str | None = None) -> str:
    base = Path(root) if root is not None else PROJECT_ROOT
    tpl = (base / "templates" / "follower_finder_book.md").read_text(encoding="utf-8")
    scan = cfg.get("daily_scan") or {}
    tr = cfg.get("terminate_reminder") or {}
    prot = ", ".join("@" + h.lstrip("@") for h in cfg.get("protected_handles") or []) or "none yet"
    return tpl.format(handle=cfg.get("owner_x_handle") or "your account", protected=prot,
                      mutuals_per_day=cfg.get("inactive_mutuals_per_day", 3),
                      daily_cap=cfg.get("daily_removal_cap", 10),
                      sheet_title=(cfg.get("sheets") or {}).get("title") or "Follower Finder Log",
                      scan_time=scan.get("time") or "not set", time_zone=cfg.get("time_zone") or "local time",
                      lists="on" if (cfg.get("lists") or {}).get("enabled") else "off",
                      reminder="on" if scan.get("reminder_via_front_door") else "off",
                      terminate_reminder=(f"on, daily at {tr['time']} ({tr.get('tz') or cfg.get('time_zone') or 'America/Los_Angeles'})"
                                          if tr.get("enabled") else "off"),
                      memory_backup=(f"on, private repo {mb['repo']}" if (mb := ((cfg.get('memory') or {}).get('backup') or {})).get('enabled') else "off"),
                      date=today or dt.date.today().isoformat())


def create_plan(cfg: dict, root=None) -> list[dict]:
    """Docs connector steps: create the doc, then append the rendered text (no index)."""
    return [{"tool": "create_document", "args": {"title": TITLE}},
            {"tool": "insert_text", "args": {"document_id": "<from create_document>", "text": render(cfg, root)}}]


def rule_change_plan(document_id: str, change: str, *, section_text: str | None = None,
                     today: str | None = None) -> list[dict]:
    """When the owner changes a rule: append the new rule text (if any) and a Change log line. Append-only."""
    if not change.strip():
        raise ValueError("describe the rule change")
    day = today or dt.date.today().isoformat()
    text = (f"\n{section_text.strip()}\n" if section_text else "") + f"\n- {day}: {change.strip()}\n"
    return [{"tool": "read_document", "args": {"document_id": document_id}},
            {"tool": "insert_text", "args": {"document_id": document_id, "text": text}}]


def action_log_plan(document_id: str, line: str, *, today: str | None = None) -> list[dict]:
    """After an executed list removal: one append-only Change log line in the Book (the Change log is the last section,
    so inserting at the end lands there). Same Docs connector steps as rule_change_plan, without a rule text."""
    return rule_change_plan(document_id, line, today=today)
