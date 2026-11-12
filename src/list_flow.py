"""Lists: from the card tap to execution to the log rows (v3.1.0).

"Remove me from these lists" / "Remove me from selected lists" = authorization to act NOW (Automatic Mode, the default;
in Manual Mode the bot asks for a yes first). "Keep all ..." writes the lists to Kept Lists. Each executed (or failed,
unverified, skipped, stopped) list is written to the List Graveyard tab THE MOMENT ITS TASK FINISHES, right after the
verified result, before the next list starts; never in a later batch step. A Book change-log line follows the same way.

The package cannot call connectors itself, so every sheet / Book write is a connector PLAN (the exact append_rows /
insert_text call). Two ways to run, same rules (list_runner.run_list_removals does the checks either way):

1. Step mode (what the bot does in Grok Bot): the bot drives the signed-in browser itself, one list at a time
   (docs/list_runbook.md), then calls leave_list(...) with what it saw. The result carries sheet_plan / book_plan for
   THAT list; the bot runs them at once and calls mark_written(...). Next list only after that.
2. Adapter mode (standalone / tests): run_now(..., adapter, write_sheet=, write_book=) runs every authorized list; the
   writer callables are called inside the run right after each list's result is saved. If a write fails the run stops.

catch_up() lists anything saved but not yet written (e.g. the connector was down); it is a safety net, not the normal path.
No X action is ever performed by this module.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from typing import Any, Callable

try:
    from . import storage as st
    from . import removal_runner as rr
    from . import list_cards as lc
    from . import list_runner as lr
    from . import lists_scan as ls
    from . import rules_book
    from . import sheet_layout as sl
except ImportError:  # pragma: no cover
    import storage as st
    import removal_runner as rr
    import list_cards as lc
    import list_runner as lr
    import lists_scan as ls
    import rules_book
    import sheet_layout as sl

SYNC_FILE = "lists_sheet_synced.json"
RUNBOOK = "docs/list_runbook.md"
BOOK_LINE_STATUSES = ("LIST_SOFT_BLOCKED", "LIST_SOFTBLOCK_NOT_CONFIRMED", "LIST_UNBLOCK_FAILED")
OBSERVED_KEYS = {"on_list_before", "owner_blocked_before", "block", "unblock", "on_list_after", "owner_blocked_after"}


# ---------- ids from the user's own config ----------
def sheet_id(cfg: dict) -> str | None:
    return (cfg.get("sheets") or {}).get("spreadsheet_id")


def book_id(cfg: dict) -> str | None:
    return (cfg.get("book") or {}).get("document_id")


# ---------- what has already been written ----------
def _sync_path(user_key, root):
    return st.user_data_root(user_key, root) / SYNC_FILE


def _sync(user_key, root) -> dict:
    d = st.load_json(_sync_path(user_key, root), {}) or {}
    d.setdefault("graveyard", {}); d.setdefault("kept", {})
    return d


def mark_written(user_key: str, entry_id: str, root=None, *, sheet: bool = False, book: bool = False) -> dict:
    """Call right after the connector write succeeded (step mode). Idempotent."""
    d = _sync(user_key, root)
    rec = d["graveyard"].setdefault(entry_id, {})
    now = dt.datetime.now().astimezone().isoformat()
    if sheet:
        rec["sheet"] = rec.get("sheet") or now
    if book:
        rec["book"] = rec.get("book") or now
    st.save_json(_sync_path(user_key, root), d)
    return rec


def book_line(entry: dict) -> str | None:
    n, o = entry.get("list_name", "a list"), entry.get("owner_handle", "its owner")
    s = entry.get("status")
    if s == "LIST_SOFT_BLOCKED":
        return f'Left the list "{n}" (owner {o}): soft block, verified off the list. Snapshot {entry.get("snapshot_id")}.'
    if s == "LIST_SOFTBLOCK_NOT_CONFIRMED":
        return f'Soft block of {o} for the list "{n}" NOT verified ({entry.get("result")}). Snapshot {entry.get("snapshot_id")}.'
    if s == "LIST_UNBLOCK_FAILED":
        return f'Blocked {o} for the list "{n}" but the unblock FAILED; {o} is still blocked until you say so. Snapshot {entry.get("snapshot_id")}.'
    return None


def plans_for(user_key: str, entry: dict, root=None, *, spreadsheet_id: str | None = None,
              book_document_id: str | None = None) -> dict:
    """The connector writes still owed for ONE saved entry: {"sheet": append_rows plan | None, "book": steps | None}."""
    cfg = st.load_user_config_dict(root, user_key=user_key)
    spreadsheet_id = spreadsheet_id or sheet_id(cfg)
    book_document_id = book_document_id or book_id(cfg)
    rec = _sync(user_key, root)["graveyard"].get(entry["entry_id"], {})
    sheet = None
    if not rec.get("sheet") and entry.get("status") in sl.LIST_STATUSES:
        sheet = sl.append_plan(spreadsheet_id or "<spreadsheet id in config sheets.spreadsheet_id>", sl.LIST_GRAVEYARD,
                               sl.list_rows([entry]))
    line, book = book_line(entry), None
    if line and not rec.get("book"):
        book = rules_book.action_log_plan(book_document_id or "<document id in config book.document_id>", line,
                                          today=str(entry.get("at", ""))[:10] or None)
    return {"entry_id": entry["entry_id"], "sheet": sheet, "book": book}


def graveyard_hook(user_key: str, root=None, *, write_sheet: Callable[[dict], Any],
                   write_book: Callable[[list], Any] | None = None, spreadsheet_id: str | None = None,
                   book_document_id: str | None = None):
    """on_logged hook for list_runner.run_list_removals: writes this list's Graveyard row (and Book line) at once."""
    def hook(entry: dict) -> None:
        p = plans_for(user_key, entry, root, spreadsheet_id=spreadsheet_id, book_document_id=book_document_id)
        if p["sheet"]:
            write_sheet(p["sheet"])
            mark_written(user_key, entry["entry_id"], root, sheet=True)
        if p["book"] and write_book:
            write_book(p["book"])
            mark_written(user_key, entry["entry_id"], root, book=True)
    return hook


def unsynced(user_key: str, root=None) -> list[dict]:
    """Saved list entries whose Graveyard row (or Book line) was never confirmed written."""
    d = _sync(user_key, root)["graveyard"]
    out = []
    for e in rr._history(user_key, root):
        if e.get("status") not in sl.LIST_STATUSES or e.get("status") == "LIST_OWNER_RETURN_DETECTED":
            continue
        rec = d.get(e.get("entry_id"), {})
        if not rec.get("sheet") or (book_line(e) and not rec.get("book")):
            out.append(e)
    return out


def catch_up(user_key: str, root=None, *, write_sheet, write_book=None, spreadsheet_id=None, book_document_id=None) -> int:
    hook = graveyard_hook(user_key, root, write_sheet=write_sheet, write_book=write_book,
                          spreadsheet_id=spreadsheet_id, book_document_id=book_document_id)
    todo = unsynced(user_key, root)
    for e in todo:
        hook(e)
    return len(todo)


# ---------- card taps ----------
def kept_plan(user_key: str, root=None, *, spreadsheet_id: str | None = None) -> dict | None:
    """Kept Lists rows not yet written (call right after a Keep answer; marks nothing, call kept_written after)."""
    cfg = st.load_user_config_dict(root, user_key=user_key)
    done = _sync(user_key, root)["kept"]
    todo = {k: v for k, v in ls.load_kept(user_key, root).items() if k not in done}
    return {"keys": list(todo), "sheet": sl.append_plan(spreadsheet_id or sheet_id(cfg) or "<config sheets.spreadsheet_id>",
                                                        sl.KEPT_LISTS, sl.kept_rows(todo))} if todo else None


def kept_written(user_key: str, keys: list[str], root=None) -> None:
    d = _sync(user_key, root)
    now = dt.datetime.now().astimezone().isoformat()
    for k in keys:
        d["kept"][k] = now
    st.save_json(_sync_path(user_key, root), d)


def _automatic(cfg: dict) -> bool:
    return str(cfg.get("operating_mode", "automatic")).lower() == "automatic"


def handle_answer(user_key: str, snapshot_id: str, selected, *, card: int | None = None, final: bool = False,
                  root=None, raw_answer: str = "") -> dict:
    """Record a batch card (card=N) or the final skipped card (final=True) tap. Returns the record result plus:
    execute_now (True when the tap authorizes immediate execution: 'Remove me' with lists chosen, Automatic Mode),
    awaiting_confirmation (Manual Mode), tasks (the lists to leave, in order), kept_plan (Kept Lists rows to write now)."""
    cfg = st.load_user_config_dict(root, user_key=user_key)
    res = (lc.record_final_answer(user_key, snapshot_id, selected, root=root, raw_answer=raw_answer) if final
           else lc.record_answer(user_key, snapshot_id, card, selected, root=root, raw_answer=raw_answer))
    out = dict(res, execute_now=False, awaiting_confirmation=False, tasks=[], kept_plan=None)
    if not res.get("recorded"):
        return out
    if res.get("kept"):
        out["kept_plan"] = kept_plan(user_key, root)
    if res.get("authorized"):
        if _automatic(cfg):
            out["execute_now"] = True
            out["tasks"] = pending(user_key, snapshot_id, root)
        else:
            out["awaiting_confirmation"] = True
    return out


def pending(user_key: str, snapshot_id: str, root=None) -> list[dict]:
    """Authorized lists on this frozen snapshot that have no final outcome yet, in order."""
    hist = rr._history(user_key, root)
    final = {e.get("list_id") for e in hist if e.get("status") in lr.FINAL and e.get("snapshot_id") == snapshot_id}
    return [r for r in lr._selected(hist, snapshot_id, "LIST_SELECTION", lc.AUTH) if r["list_id"] not in final]


def next_task(user_key: str, snapshot_id: str, root=None, *, now: dt.datetime | None = None) -> dict | None:
    """Step mode: the next list to leave, or None. Includes the protected-owner flag and how long to wait first."""
    todo = pending(user_key, snapshot_id, root)
    if not todo:
        return None
    r = todo[0]
    cfg = st.load_user_config_dict(root, user_key=user_key)
    keep = st.load_always_keep(root, user_key=user_key)
    hist = rr._history(user_key, root)
    last = next((e for e in reversed(hist) if str(e.get("status", "")).startswith("LIST_")
                 and e.get("status") not in ("LIST_SELECTION", "LIST_KEPT") and e.get("at")), None)
    wait = 0.0
    if last:
        try:
            age = ((now or dt.datetime.now().astimezone()) - dt.datetime.fromisoformat(last["at"])).total_seconds()
            wait = max(0.0, rr.PACING_SECONDS - age)
        except ValueError:
            pass
    return {"list": r, "protected_owner": lr._protected_owner(r, cfg, keep), "wait_seconds": round(wait, 1),
            "remaining": len(todo), "runbook": RUNBOOK}


class ObservedAdapter:
    """Adapter that replays what the bot SAW in its own browser for one list (step mode). It performs no X action; the
    runner applies the exact same checks it applies to a live adapter. A missing observation counts as 'unknown'."""

    def __init__(self, observed: dict):
        bad = set(observed) - OBSERVED_KEYS
        if bad:
            raise ValueError(f"unknown observation keys: {sorted(bad)}")
        self.o = observed
        self._on = [observed.get("on_list_before"), observed.get("on_list_after")]
        self._bl = [observed.get("owner_blocked_before"), observed.get("owner_blocked_after")]

    def list_state(self, r):
        return {"on_list": self._on.pop(0) if self._on else None}

    def owner_state(self, r):
        return {"blocked": self._bl.pop(0) if self._bl else None}

    def _res(self, key):
        v = self.o.get(key)
        if not v:
            return rr.RemovalResult(rr.ERROR, f"no {key} observation")
        return rr.RemovalResult(v.get("status", rr.ERROR), v.get("message", ""))

    def block(self, r):
        return self._res("block")

    def unblock(self, r):
        return self._res("unblock")


def leave_list(user_key: str, snapshot_id: str, list_id: str, observed: dict, *, root=None, now_fn=None,
               out=None, spreadsheet_id: str | None = None, book_document_id: str | None = None) -> dict:
    """Step mode, ONE list. observed = what the bot saw/did in its browser for this list (see docs/list_runbook.md):
      on_list_before, owner_blocked_before   (memberships page / owner profile, before touching anything)
      block, unblock                         ({"status": "ok"|"warning"|"rate_limited"|"error", "message": ""})
      on_list_after, owner_blocked_after     (memberships page again / owner profile)
    The same runner checks apply (frozen snapshot, one-run lock, cap, day lock, protected, already blocked, verify).
    Returns {"entry": the saved Graveyard-bound entry, "sheet_plan", "book_plan", "stopped", ...}: run the plans NOW,
    then mark_written(...). Do not start the next list before that."""
    import io
    saved = {}
    res = lr.run_list_removals(user_key, snapshot_id, ObservedAdapter(observed or {}), root=root, now_fn=now_fn,
                               sleep=lambda s: None, out=out or io.StringIO(), only_list_id=str(list_id),
                               on_logged=lambda e: saved.update(entry=e))
    entry = saved.get("entry")
    if entry:
        st._mirror(lambda r: r.record_list_left, user_key, entry, root=root)  # v3.5.0 write-through
    plans = plans_for(user_key, entry, root, spreadsheet_id=spreadsheet_id, book_document_id=book_document_id) if entry else \
        {"sheet": None, "book": None}
    return {"entry": entry, "sheet_plan": plans["sheet"], "book_plan": plans["book"], "stopped": res["stopped"],
            "left": res["left"], "skipped": res["skipped"], "actions_today": res["actions_today"],
            "daily_cap": res["daily_cap"]}


def run_now(user_key: str, snapshot_id: str, adapter, *, write_sheet, write_book=None, root=None, now_fn=None,
            sleep=time.sleep, out=sys.stdout, spreadsheet_id=None, book_document_id=None) -> dict:
    """Adapter mode: leave every authorized list; each list's Graveyard row (and Book line) is written inside the run,
    right after that list's result is saved."""
    hook = graveyard_hook(user_key, root, write_sheet=write_sheet, write_book=write_book,
                          spreadsheet_id=spreadsheet_id, book_document_id=book_document_id)
    return lr.run_list_removals(user_key, snapshot_id, adapter, root=root, now_fn=now_fn, sleep=sleep, out=out,
                                on_logged=hook)
