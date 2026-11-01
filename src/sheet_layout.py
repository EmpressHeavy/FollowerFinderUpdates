"""Log sheet created at setup: tabs Actions, Keepers, In review (+ List Graveyard, Kept Lists, Protected Lists) (beta feedback 2026-10-20).
Plans are run by the bot with the Google Sheets connector (writes via sheet_connector: read, then append)."""
from __future__ import annotations

ACTIONS, KEEPERS, IN_REVIEW, LISTS = "Actions", "Keepers", "In review", "List Graveyard"
LIST_GRAVEYARD = LISTS
LEGACY_LIST_TAB = "List Removals"   # v3.0.0-v3.0.1 name; rename that tab to List Graveyard (same columns)
KEPT_LISTS = "Kept Lists"
PROTECTED_LISTS = "Protected Lists"
HEADERS = {
    ACTIONS: ["When", "Action", "Handle", "X user ID", "Posts", "Followers", "Following", "Why", "Confirmed on X",
              "Entry ID"],
    KEEPERS: ["Keep", "Handle", "X user ID", "Kind", "When", "Note"],
    IN_REVIEW: ["Status", "Handle", "X user ID", "Posts", "Followers", "Following", "Why", "Flags", "Link", "When"],
    LISTS: ["When (with time zone)", "Action", "List name", "List ID", "Owner handle", "Owner ID", "Verified result",
            "Snapshot ID", "Entry ID"],
    KEPT_LISTS: ["List name", "List link", "Owner handle", "Date kept", "Snapshot ID"],
    PROTECTED_LISTS: ["List name", "List link", "Owner handle", "Status", "Protected since", "Last follow check",
                      "Left on"],
}
TITLE = "Follower Finder Log"


def setup_plan(title: str = TITLE) -> list[dict]:
    steps = [{"tool": "create_spreadsheet", "args": {"title": title, "sheet_titles": list(HEADERS)}}]
    for tab, hdr in HEADERS.items():
        steps.append({"tool": "write_range", "args": {"spreadsheet_id": "<from create_spreadsheet>",
                                                      "range": f"'{tab}'!A1", "values": [hdr]}})
    return steps


def action_rows(history: list[dict], already_logged=()) -> list[list]:
    """Rows for the Actions tab from history (removals confirmed on X, blocks, skips); dedup by entry ID."""
    done = set(already_logged)
    rows = []
    for e in history:
        if e.get("status") not in ("REMOVED", "BLOCKED", "REMOVAL_NOT_CONFIRMED", "REMOVAL_SKIPPED_NOT_FOLLOWING"):
            continue
        if e.get("entry_id") in done:
            continue
        rows.append([e.get("at", ""), e.get("status"), e.get("handle", ""), "'" + str(e.get("x_user_id") or ""),
                     e.get("post_count", ""), e.get("followers", ""), e.get("following", ""), e.get("reason", ""),
                     "yes" if e.get("confirmed_on_x") else "no", e.get("entry_id", "")])
    return rows


LIST_STATUSES = ("LIST_SOFT_BLOCKED", "LIST_UNBLOCK_FAILED", "LIST_SOFTBLOCK_NOT_CONFIRMED", "LIST_SKIPPED_NOT_ON_LIST",
                 "LIST_SKIPPED_OWNER_ALREADY_BLOCKED", "LIST_SKIPPED_PROTECTED_OWNER", "LIST_OWNER_RETURN_DETECTED", "LIST_OWNER_BLOCKED",
                 "LIST_OWNER_BLOCK_NOT_CONFIRMED", "LIST_OWNER_BLOCK_SKIPPED", "LIST_STOPPED")


def list_rows(history: list[dict], already_logged=()) -> list[list]:
    """Rows for the 'List Graveyard' tab (formerly 'List Removals') (timestamps keep their time-zone offset); dedup by entry ID."""
    done = set(already_logged)
    rows = []
    for e in history:
        if e.get("status") not in LIST_STATUSES or e.get("entry_id") in done:
            continue
        rows.append([e.get("at", ""), f"{e.get('status')}: {e.get('action', '')}", e.get("list_name", "Not available"),
                     "'" + str(e.get("list_id") or ""), e.get("owner_handle", ""), "'" + str(e.get("owner_id") or ""),
                     e.get("result") or e.get("reason") or e.get("message") or "", e.get("snapshot_id", ""),
                     e.get("entry_id", "")])
    return rows


def protected_rows(protected: dict, history: list[dict] = ()) -> list[list]:
    """Rows for 'Protected Lists': lists whose owner you follow (not re-inspected; only the follow check runs),
    plus lists that left because you unfollowed the owner (with the date)."""
    rows = [[r["name"], r["url"], r["owner_handle"], "Protected", r["since"], r.get("last_checked", ""), ""]
            for r in protected.values()]
    for e in history:
        if e.get("status") == "PROTECTED_LIST_RELEASED":
            rows.append([e["name"], e["url"], e["owner_handle"], "Left: you unfollowed the owner",
                         e.get("protected_since", ""), e["at"], e["at"]])
    return rows


def kept_rows(kept: dict) -> list[list]:
    """Rows for 'Kept Lists' (not reviewed again unless the owner or name changes)."""
    return [[r["name"], r["url"], r["owner_handle"], r["kept_at"], r["snapshot_id"]] for r in kept.values()]


def _col(n: int) -> str:
    return chr(ord("A") + n - 1)


def append_plan(spreadsheet_id: str, tab: str, rows: list[list]) -> dict | None:
    """ONE connector append_rows call for rows of a log tab (header in row 1, so data goes under A2). None if empty.
    Cells starting with ' are text markers (IDs); parseInput keeps them as text. Dedupe by Entry ID is done by the
    caller (list_flow keeps a synced-entry file), never by rewriting existing rows."""
    if not rows:
        return None
    width = len(HEADERS[tab])
    assert all(len(r) == width for r in rows), f"{tab} rows must have {width} columns"
    return {"tool": "append_rows",
            "arguments": {"spreadsheetId": spreadsheet_id, "sheet": tab, "cells": f"A2:{_col(width)}",
                          "values": rows, "parseInput": True, "insertRows": True}}
