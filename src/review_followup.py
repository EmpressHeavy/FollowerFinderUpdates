"""Accounts in review: one follow-up flow for everything left unmarked on any card.

Sources (both, merged, numbered continuously):
  - normal removal candidate cards: chat numbers in CHAT_SELECTION "held_for_final_review";
  - Graveyard returns cards: graveyard_return_cards.kept_in_review().
Every such account has status IN_REVIEW. It is never moved to Always Keep / Keepers / Not a bot automatically.

Design (6-option widget limit; same standard as the other cards):
  1. Terminate card per chunk of up to 5: "N. @handle — remove" or "N. @handle — block (Graveyard return)" plus
     "Mark all for termination" (danger), multi-select, no free text. Marked normal candidates get a removal
     authorization (REVIEW_FOLLOWUP entry, read by removal_runner); marked returnees get a BLOCK authorization
     (graveyard_returns.record_block_authorization). Protections, pacing, cap and stop-on-warning still apply at
     execution in those runners.
  2. Keep card for the accounts NOT terminated on that chunk: one option per account (= keep as "Not a bot",
     moved to Keepers) plus "Add the ones I keep to Always Keep". Unmarked stay IN_REVIEW for a later card.
This replaces using the old 3-button final review card (Always Keep / Not a bot / Leave as is) for these
accounts; that card's decisions (CHAT_FINAL_REVIEW) are still honored when computing what is in review.
In-review rows go to the In review section of the sheet / reports/in_review_<snapshot>.csv with status
"In review" and Source CANDIDATE or GRAVEYARD_RETURN (never Keepers / Always Keep).
History is append-only (hash chained). Numbering persists in snapshots/<id>/review_followup_numbering.json.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

try:
    from . import storage as st
    from . import removal_runner as rr
    from . import graveyard_returns as gr
    from . import graveyard_return_cards as grc
    from . import review_sheet_writer as rsw
except ImportError:  # pragma: no cover
    import storage as st
    import removal_runner as rr
    import graveyard_returns as gr
    import graveyard_return_cards as grc
    import review_sheet_writer as rsw

CARD_SIZE = 5
MAX_OPTIONS = 6
IN_REVIEW = "IN_REVIEW"
MARK_ALL = "Mark all for termination"
ALWAYS_KEEP_TOGGLE = "Add the ones I keep to Always Keep"
CANDIDATE, RETURNEE = "CANDIDATE", "GRAVEYARD_RETURN"


def _key(a):
    return rr.st_key(a)


def collect_in_review(user_key: str, snapshot_id: str, root=None) -> list[dict]:
    hist = rr._history(user_key, root)
    sd = st.snapshot_dir(snapshot_id, root, user_key=user_key)
    num = st.load_json(sd / "chat_numbering.json", {}) or {}
    by_no = {a["chat_no"]: a for a in num.get("accounts", [])}
    items, seen = [], set()
    for e in hist:
        if e.get("status") == "CHAT_SELECTION" and e.get("snapshot_id") == snapshot_id:
            for n in e.get("held_for_final_review") or []:
                a = by_no.get(n)
                if a and _key(a) not in seen:
                    seen.add(_key(a))
                    items.append({"type": CANDIDATE, "action": "remove", "handle": a["handle"],
                                  "x_user_id": a.get("user_id") if str(a.get("user_id")).isdigit() else None,
                                  "label": a.get("label"), "ref": f"card {e.get('card_no')} #{n}", "chat_no": n})
    for r in grc.kept_in_review(user_key, snapshot_id, root):
        if _key(r) not in seen:
            seen.add(_key(r))
            items.append({"type": RETURNEE, "action": "block", "handle": r["handle"], "x_user_id": r.get("x_user_id"),
                          "label": "Graveyard return", "ref": f"returns R{r['n']}"})
    # resolved = authorized to act, acted on, or decided keep (here or on the old final review card)
    resolved = set()
    for e in hist:
        s = e.get("status")
        if e.get("snapshot_id") not in (snapshot_id, None):
            continue
        if s in ("REMOVED", "BLOCKED"):
            resolved.add(e.get("account_key"))
        elif s == "REVIEW_FOLLOWUP" or s == "BLOCK_AUTHORIZATION" or (
                s == "CHAT_SELECTION" and str(e.get("authorization", "")).startswith(rr.AUTHORIZED_PREFIXES)):
            resolved |= {_key(a) for a in e.get("selected") or []}
        elif s == "REVIEW_DECISION":
            resolved |= {k for k, v in (e.get("decisions") or {}).items() if v in ("NOT_A_BOT", "ALWAYS_KEEP")}
        elif s == "CHAT_FINAL_REVIEW":
            for n, k in (e.get("decisions") or {}).items():
                if k in ("NOT_A_BOT", "ALWAYS_KEEP") and int(n) in by_no:
                    resolved.add(_key(by_no[int(n)]))
    return [dict(i, status=IN_REVIEW) for i in items if _key(i) not in resolved]


def _opt(i):
    try:
        from . import card_text as ct
    except ImportError:  # pragma: no cover
        import card_text as ct
    tail = "block (Graveyard return)" if i["type"] == RETURNEE else "remove"
    return {"label": ct.option_label(i["n"], i, tail), "value": str(i["n"])}


def build_review_cards(items: list[dict]) -> dict:
    items = [dict(i, n=k + 1) for k, i in enumerate(items)]
    chunks = [items[k:k + CARD_SIZE] for k in range(0, len(items), CARD_SIZE)]
    cards = []
    for c, ch in enumerate(chunks, 1):
        opts = [_opt(i) for i in ch] + [{"label": MARK_ALL, "value": "mark_all", "style": "danger"}]
        assert len(opts) <= MAX_OPTIONS
        cards.append({"card": c, "of": len(chunks), "kind": "terminate", "multiSelect": True, "allowCustom": False,
                      "prompt": (f"Accounts in review ({c} of {len(chunks)}): terminate which? Followers are removed; "
                                 "Graveyard returns are blocked. Unmarked accounts will be kept in review."),
                      "options": opts})
    return {"items": items, "cards": cards, "numbering": {str(k + 1): [i["n"] for i in ch] for k, ch in enumerate(chunks)}}


def keep_card(items_left: list[dict], card_no: int) -> dict | None:
    if not items_left:
        return None
    opts = [{"label": f"{i['n']}. @{i['handle']}", "value": str(i["n"])} for i in items_left[:CARD_SIZE]]
    opts.append({"label": ALWAYS_KEEP_TOGGLE, "value": "always_keep"})
    return {"card": card_no, "kind": "keep", "multiSelect": True, "allowCustom": False,
            "prompt": "Keep which of these? Kept accounts are marked Not a bot (moved to Keepers). "
                      "Unmarked accounts will be kept in review.", "options": opts}


def _np(user_key, snapshot_id, root):
    return st.snapshot_dir(snapshot_id, root, user_key=user_key) / "review_followup_numbering.json"


def build_for_snapshot(user_key: str, snapshot_id: str, root=None) -> dict:
    flow = build_review_cards(collect_in_review(user_key, snapshot_id, root))
    st.save_json(_np(user_key, snapshot_id, root), {"snapshot_id": snapshot_id, "items": flow["items"],
                                                     "cards": flow["numbering"], "answers": {}})
    st.apply_user_permissions(user_key, root)
    return flow


def _load(user_key, snapshot_id, root, card_no):
    num = st.load_json(_np(user_key, snapshot_id, root), None)
    if not num:
        raise ValueError("no review follow-up numbering for this snapshot; build the cards first")
    nums = num["cards"].get(str(card_no))
    if not nums:
        raise ValueError(f"review card {card_no} does not exist")
    return num, nums, {i["n"]: i for i in num["items"]}


def record_terminate(user_key: str, snapshot_id: str, card_no: int, selected, *, root=None) -> dict:
    num, nums, by_n = _load(user_key, snapshot_id, root, card_no)
    picked = grc.parse_answer(selected, nums) if not any(str(x) in (MARK_ALL, "mark_all") for x in (selected or [])) \
        else list(nums)
    cand = [by_n[n] for n in picked if by_n[n]["type"] == CANDIDATE]
    rets = [by_n[n] for n in picked if by_n[n]["type"] == RETURNEE]
    cfg = st.load_user_config_dict(root, user_key=user_key)
    auto = cfg.get("operating_mode", "automatic") == "automatic"
    if cand:
        rr._append(user_key, root, {"status": "REVIEW_FOLLOWUP", "at": dt.datetime.now().astimezone().isoformat(),
                                    "snapshot_id": snapshot_id, "card": card_no,
                                    "authorization": ("SELECTED_OWNER_YES (accounts-in-review card)" if auto
                                                      else "MANUAL (awaiting explicit confirmation)"),
                                    "selected": [{"handle": "@" + i["handle"], "x_user_id": i["x_user_id"]} for i in cand],
                                    "action": "NONE (removal runs via removal_runner)"})
    if rets:
        gr.record_block_authorization(user_key, snapshot_id, rets, root=root,
                                      raw_answer=f"accounts-in-review card {card_no}")
    left = [by_n[n] for n in nums if n not in picked]
    num["answers"].setdefault(str(card_no), {})["terminate"] = picked
    st.save_json(_np(user_key, snapshot_id, root), num)
    st.apply_user_permissions(user_key, root)
    return {"removal_authorized": [i["n"] for i in cand], "block_authorized": [i["n"] for i in rets],
            "keep_card": keep_card(left, card_no), "still_in_review": [i["n"] for i in left]}


def record_keep(user_key: str, snapshot_id: str, card_no: int, selected, *, root=None) -> dict:
    num, nums, by_n = _load(user_key, snapshot_id, root, card_no)
    done = set(num["answers"].get(str(card_no), {}).get("terminate", []))
    sel = [str(x) for x in (selected or [])]
    ak = any(s in (ALWAYS_KEEP_TOGGLE, "always_keep") for s in sel)
    kept = [n for n in grc.parse_answer([s for s in sel if s not in (ALWAYS_KEEP_TOGGLE, "always_keep")], nums)
            if n not in done]
    kind = "ALWAYS_KEEP" if ak else "NOT_A_BOT"
    decisions = {_key(by_n[n]): kind for n in kept}
    sheet_ops = [{"tool": "keepers_add", "sheet": "Keepers", "handle": "@" + by_n[n]["handle"],
                  "x_user_id": by_n[n]["x_user_id"], "reason": ("Always Keep" if ak else "Not a bot (Keep)")
                  + " (owner, accounts-in-review card)"} for n in kept]
    if kept:
        rr._append(user_key, root, {"status": "REVIEW_DECISION", "at": dt.datetime.now().astimezone().isoformat(),
                                    "snapshot_id": snapshot_id, "card": card_no, "decisions": decisions,
                                    "handles": {_key(by_n[n]): by_n[n]["handle"] for n in kept}, "action": "NONE"})
    if ak and kept:
        rcs = rr._rcs(); old = rcs.ROOT
        rcs.ROOT = Path(root) if root is not None else st.PROJECT_ROOT
        try:
            rcs.save_always_keep_additions([{"handle": by_n[n]["handle"], "x_user_id": by_n[n]["x_user_id"]}
                                            for n in kept], user_key=user_key, note="accounts-in-review card")
        finally:
            rcs.ROOT = old
    still = [n for n in nums if n not in done and n not in kept]
    num["answers"].setdefault(str(card_no), {})["keep"] = {"kept": kept, "kind": kind}
    st.save_json(_np(user_key, snapshot_id, root), num)
    st.apply_user_permissions(user_key, root)
    return {"kept": kept, "kind": kind, "still_in_review": still, "sheet_ops": sheet_ops}


def in_review_rows(items: list[dict], snapshot_id: str, review_date: str | None = None) -> list[list]:
    """In review rows (rsw.REVIEW_QUEUE_HEADER order) with Status 'In review'."""
    d = review_date or dt.date.today().isoformat()
    rows = []
    for i in items:
        note = ("Graveyard return; if terminated the action is BLOCK" if i["type"] == RETURNEE
                else "Left unmarked on a card; if terminated the action is REMOVE")
        # Leading apostrophe = keep as text under USER_ENTERED (same as build_rows): long X IDs would otherwise
        # become rounded numbers and the date a date serial. Found in the real-sheet run 2026-10-16.
        xid = i.get("x_user_id")
        rows.append(["", i.get("ref"), "", ("'" + str(xid)) if xid else "Not available", "@" + i["handle"],
                     i.get("label") or "", "Unmarked on card", "", "Y", snapshot_id, "In review", "", "", "'" + d, "", "",
                     i["type"], note])
    return rows


def _row_key(r):
    """account + snapshot (+ In review status) so repeated writes don't add the same in-review row twice."""
    r = list(r) + [""] * 18
    xid = str(r[3]).lstrip("'")
    acct = xid if xid.isdigit() else str(r[4]).lstrip("@").lower()
    return (acct, r[9], r[10])


def write_in_review(user_key: str, snapshot_id: str, root=None, *, spreadsheet_id: str | None = None,
                    first_row: int | None = None, service=None, existing=None, cfg: dict | None = None,
                    connector_available: bool = True, env=None) -> dict:
    """CSV always (reports/in_review_<snapshot>.csv, a local file). Plus the sheet's In review when
    spreadsheet_id is given, through the backend picked by sheet_connector.select_backend (sheets.backend):
    - "connector" (default): NO Google call here. Returns out["sheet"] = {"backend": "connector", ...}:
      without `existing` it holds "read" (the read_range step to run first); with `existing` (the read result,
      any format rows_from_read_result accepts) it holds "plan" (the single append_rows call, or None when
      every row is already there).
    - "google_api" (explicit fallback): appends via review_sheet_writer.write_rows_to_sheet as before.
    - "csv": no sheet write; out["sheet"]["message"] says why (never a silent skip).
    - connector chosen but not connected: raises preflight.PreflightError (no fallback inside Grok Bot).
    All paths APPEND only; an account already In review for this snapshot is not added again (_row_key).
    first_row only places the header/rows when the tab is empty. cfg defaults to this user's own config."""
    try:
        from . import sheet_connector as sc
    except ImportError:  # pragma: no cover
        import sheet_connector as sc
    items = collect_in_review(user_key, snapshot_id, root)
    rows = in_review_rows(items, snapshot_id)
    p = rsw.write_rows_csv(rows, st.reports_path(root, user_key=user_key) / f"in_review_{snapshot_id}.csv",
                           dedupe_key=_row_key)
    st.apply_user_permissions(user_key, root)
    out = {"csv": str(p), "rows": len(rows)}
    if not spreadsheet_id:
        return out
    if cfg is None:
        cfg = st.load_user_config_dict(root, user_key=user_key)
    sel = sc.select_backend(cfg, connector_available=connector_available, env=env)
    b = sel["backend"]
    if b == "refused":  # Grok Bot: not connected, or a non-connector backend asked for: refuse, don't fall back
        try:
            from . import preflight
        except ImportError:  # pragma: no cover
            import preflight
        raise preflight.PreflightError(f"{sel['message']} (The local report {p} was still saved.)")
    if b == "connector":
        sheet = {"backend": "connector", "message": sel["message"]}
        if existing is None:
            sheet["read"] = sc.read_step(spreadsheet_id, rsw.REVIEW_QUEUE_TAB)
        else:
            ex = existing if isinstance(existing, list) else sc.rows_from_read_result(existing)
            sheet["plan"] = sc.plan_append(rows, spreadsheet_id=spreadsheet_id, existing=ex,
                                           tab=rsw.REVIEW_QUEUE_TAB, first_row=first_row or 4, dedupe_key=_row_key)
        out["sheet"] = sheet
    elif b == "google_api":
        res = rsw.write_rows_to_sheet(rows, spreadsheet_id=spreadsheet_id, first_row=first_row or 4,
                                      tab=rsw.REVIEW_QUEUE_TAB, service=service, user_key=user_key, root=root,
                                      dedupe_key=_row_key)
        out["sheet"] = res if not sel["message"] else {"backend": "google_api", "message": sel["message"], "result": res}
    else:
        out["sheet"] = {"backend": "csv", "message": sel["message"] or f"sheets.backend is csv; rows are in {p}.",
                        "csv": str(p)}
    return out
