"""Google Sheets via the bot's own Sheets CONNECTOR: the DEFAULT sheet-write path (owner rule 2026-10-16 8:03 PM PT).

Package code can't call the connector itself, so this module produces a deterministic CONNECTOR WRITE PLAN that the
agent executes with the Google Sheets connector installed in that user's own Grok Bot (writes land in that user's
own Drive). Steps:
  1. read_step(spreadsheet_id, tab)          -> the connector read_range call to make
  2. rows_from_read_result(<read result>)    -> existing rows (trailing empty rows trimmed)
  3. plan_append(rows, tab=..., spreadsheet_id=..., existing=..., dedupe_key=...) -> {"tool": "append_rows",
     "arguments": {...}} : ONE atomic append (parseInput=true == USER_ENTERED, insertRows=true == INSERT_ROWS,
     cells "A3:R"), or None when there is nothing new to write
  4. start_row_from_response(<append result>, plan) -> first DATA row (from updates.updatedRange)
The plan is produced by running review_sheet_writer.write_rows_to_sheet (the same code as the google_api fallback)
against a recording service, so empty-tab layout (header row 3, data from row 4), dedupe, text markers for
IDs/dates and position-independent formulas are identical on every backend.

Backends (config "sheets": {"backend": ...}): "connector" (default) | "google_api" (fallback for running the
package outside Grok Bot; manual OAuth client / service-account key) | "csv". select_backend() never silently
skips. With "connector" (inside Grok Bot) and no connection it REFUSES (backend "refused"); it never falls back
to CSV or google_api there (owner 2026-10-16: Sheets + Drive are required inside Grok Bot). google_api/csv are
only for running outside Grok Bot, when chosen explicitly.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from typing import Any, Callable, Sequence

try:
    from . import review_sheet_writer as rsw
except ImportError:  # pragma: no cover
    import review_sheet_writer as rsw

BACKENDS = ("connector", "google_api", "csv")
DEFAULT_BACKEND = "connector"
READ_TOOL, APPEND_TOOL = "read_range", "append_rows"


# ---------- backend selection ----------
def _fallback_env(env) -> bool:
    return bool(env.get(rsw.OAUTH_CLIENT_ENV) or env.get(rsw.CREDENTIALS_ENV))


def select_backend(cfg: dict | None = None, *, connector_available: bool = True, env=None) -> dict:
    """-> {"backend": connector|google_api|csv, "message": str|None}. Rules:
    - default (no config) is the connector;
    - inside Grok Bot (runtime grokbot, the default) any backend but connector -> "refused";
    - google_api only when explicitly chosen in a STANDALONE config (and its env vars are set; outside Grok Bot);
    - connector chosen but unavailable -> "refused" (no CSV/google_api fallback inside Grok Bot);
    - otherwise CSV with a message saying why (never a silent skip)."""
    env = os.environ if env is None else env
    want = ((cfg or {}).get("sheets") or {}).get("backend", DEFAULT_BACKEND)
    try:
        from . import preflight
    except ImportError:  # pragma: no cover
        import preflight
    prob = preflight.backend_problem(cfg, env)  # Grok Bot runtime: only the connector is allowed
    if prob:
        return {"backend": "refused", "message": prob}
    if want not in BACKENDS:
        return {"backend": "csv", "message": f"Unknown sheets.backend {want!r}; writing a CSV report instead."}
    if want == "csv":
        return {"backend": "csv", "message": None}
    if want == "google_api":
        if _fallback_env(env):
            return {"backend": "google_api", "message": None}
        return {"backend": "csv", "message": (f"sheets.backend is google_api but neither {rsw.OAUTH_CLIENT_ENV} nor "
                                              f"{rsw.CREDENTIALS_ENV} is set; wrote a CSV report instead.")}
    if connector_available:
        return {"backend": "connector", "message": None}
    return {"backend": "refused", "message": ("Google Sheets isn't connected. Follower Finder requires Google Sheets "
                                              "(and Google Drive) connected in your Grok Bot; connect it and try again. "
                                              "Nothing was written to a sheet.")}


# ---------- connector plan ----------
def read_step(spreadsheet_id: str, tab: str = rsw.REVIEW_QUEUE_TAB) -> dict:
    return {"tool": READ_TOOL, "arguments": {"spreadsheetId": spreadsheet_id,
                                             "ranges": [{"sheet": tab, "cells": "A1:R"}], "maxChars": 250000}}


def rows_from_read_result(result) -> list[list[str]]:
    """Accepts the connector's read_range result (dict or JSON text) or the CSV content string."""
    content = result
    if isinstance(result, (bytes, bytearray)):
        result = result.decode("utf-8")
    if isinstance(result, str) and '"ranges"' in result and "{" in result:  # JSON result, possibly wrapped in text
        result = json.loads(result[result.index("{"):result.rindex("}") + 1])
    if isinstance(result, dict):
        content = (result.get("ranges") or [{}])[0].get("content", "")
    rows = [list(r) for r in csv.reader(io.StringIO(content or ""))]
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    return rows


class _Recorder:
    """Stands in for the Sheets API so write_rows_to_sheet's real logic produces the append payload."""
    def __init__(self, existing):
        self.existing = [list(r) for r in existing]; self.calls = []; self._op = None
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, spreadsheetId, range): self._op = "get"; return self
    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        self._op = "app"; self.calls.append((range, valueInputOption, insertDataOption, body["values"])); return self
    def execute(self):
        return {"values": self.existing} if self._op == "get" and self.existing else {}


def plan_append(rows, *, spreadsheet_id: str, existing: Sequence[Sequence[Any]], tab: str = rsw.REVIEW_QUEUE_TAB,
                first_row: int = 4, write_header: bool = True,
                dedupe_key: Callable[[Sequence[Any]], Any] | None = None) -> dict | None:
    rec = _Recorder(existing)
    res = rsw.write_rows_to_sheet(rows, spreadsheet_id=spreadsheet_id, first_row=first_row, tab=tab,
                                  write_header=write_header, service=rec, dedupe_key=dedupe_key)
    if not rec.calls:
        return None
    rng, vio, ido, values = rec.calls[0]
    assert vio == "USER_ENTERED" and ido == "INSERT_ROWS"
    cells = rng.split("!", 1)[1]
    return {"tool": APPEND_TOOL,
            "arguments": {"spreadsheetId": spreadsheet_id, "sheet": tab, "cells": cells, "values": values,
                          "parseInput": True, "insertRows": True},
            "rows_written": res["rows_written"], "header_written": res["header_written"]}


def start_row_from_response(response, plan: dict) -> int | None:
    if isinstance(response, str):
        response = json.loads(response[response.index("{"):response.rindex("}") + 1])
    rng = ((response or {}).get("updates") or {}).get("updatedRange", "")
    start = rsw._start_row(rng)
    if start is not None and plan.get("header_written"):
        start += 1
    return start


def in_review_plan(user_key: str, snapshot_id: str, *, spreadsheet_id: str, existing, root=None) -> dict | None:
    """Connector plan for the accounts-in-review rows (dedupe per account + snapshot), In review tab."""
    try:
        from . import review_followup as rf
    except ImportError:  # pragma: no cover
        import review_followup as rf
    rows = rf.in_review_rows(rf.collect_in_review(user_key, snapshot_id, root), snapshot_id)
    return plan_append(rows, spreadsheet_id=spreadsheet_id, existing=existing, dedupe_key=rf._row_key)
