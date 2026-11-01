"""Record a chat selection-card answer (Follower Finder chat-first review).

Chat is the primary interface: candidates are shown in cards of up to 5 accounts plus "Select all",
one card pending at a time. This module turns one card answer into:
  1. rows for the append-only Actions tab (one per account shown on the card),
  2. In review column A writes for queue accounts (Terminate for selected; Keep only when a Keep
     follow-up decision is given; unselected otherwise left blank) plus a short note in column S,
  3. a new hash-chained TH entry appended to history.jsonl (status CHAT_SELECTION, no X action).

It never touches X and never removes anything. Selecting is NOT confirmation; removal still needs the
owner's explicit confirmation per the selected mode. Sheet writes are emitted as a JSON plan
(`sheet_ops`) which the agent applies with the Google Sheets connector (write_range parseInput=true),
then records in the snapshot's sheet_apply_record.json.

Amendment 2026-10-16 ~4:05 PM PT (owner): in Automatic Mode, 'Select all' + done authorizes EVERY account on
the card for termination immediately (incl. Needs-your-look / inactive); protections are still rechecked at
execution. Individual picks keep status 'Selected, awaiting confirmation' here and are resolved by
automatic_policy.decide (current interpretation, pending owner review). There is no per-card follow-up:
unselected accounts are collected for one final review card after the last card (build_final_review).
Amendment 2026-10-16 ~4:06 PM PT (owner): in Automatic Mode EVERY selected account (individual tap or Select
all) is authorized for immediate removal, incl. Needs-your-look / inactive-only; no confirmation prompt.
Manual mode unchanged.

Usage:
  python src/record_chat_selection.py --user <user_key> <snapshot_id> <card_no> <nums e.g. 1,2,3,4,5> "<raw answer>"
         [--keep 2,4] [--protect 3] [--always-keep 5] [--dry-run]
Prints JSON {"selected": [...], "chat_rows": [...], "sheet_ops": [...], "history_entry": {...}}.
"""
from __future__ import annotations
import datetime as dt, hashlib, json, re, sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
# Per-user isolation (2026-10-16): history and snapshots live under data/users/{user_key}/; there is
# no shared HISTORY path and no global fallback. owner_x_handle comes from the user's own config.


def _storage():
    try:
        import storage as st
    except ImportError:  # pragma: no cover
        from . import storage as st
    return st


def history_path(user_key: str) -> Path:
    return _storage().history_path(ROOT, user_key=user_key)


def _snap_dir(user_key: str, snapshot_id: str) -> Path:
    return _storage().snapshot_dir(snapshot_id, ROOT, user_key=user_key)


def owner_handle_for(user_key: str) -> str:
    raw = _storage().load_user_config_dict(ROOT, user_key=user_key)
    h = raw.get("owner_x_handle") or raw.get("x_handle")
    if not h:
        raise ValueError(f"user {user_key!r} has no owner_x_handle in their own config")
    return h if h.startswith("@") else "@" + h
PT = ZoneInfo("America/Los_Angeles")
CHAT_TAB, QUEUE_TAB = "Actions", "In review"
QUEUE_NOTE_COL = "S"  # 'Chat selection note' column added 2026-10-16

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15}


def _nums(text: str) -> list[int]:
    """All numbers in text, expanding ranges like 1-3 / 1 to 3 and number words."""
    t = text.lower()
    for w, n in _WORDS.items():
        t = re.sub(rf"\b{w}\b", str(n), t)
    out: list[int] = []
    for a, b in re.findall(r"(\d+)\s*(?:-|–|to|through)\s*(\d+)", t):
        out += list(range(int(a), int(b) + 1))
    t = re.sub(r"(\d+)\s*(?:-|–|to|through)\s*(\d+)", " ", t)
    out += [int(x) for x in re.findall(r"\d+", t)]
    return out


def parse_answer(raw, card_nums: list[int]) -> tuple[list[int], str]:
    """Return (selected chat numbers restricted to this round's card, via).

    v3.5.7: the answer is the round card's selection (option values/labels, e.g. ["1", "3"] or "1. @a — …, 3. @b — …",
    or "mark_all" / "Mark all K for termination"). Numbers outside this round are ignored: there is no
    'type the extra numbers' path and no all-at-once list.
    """
    if isinstance(raw, (list, tuple)):
        vals = [str(v).strip() for v in raw]
        if any(v.lower() == "mark_all" or v.lower().startswith("mark all") for v in vals):
            return list(card_nums), "Select all"
        raw = ", ".join(v.split(".")[0] for v in vals)
    elif isinstance(raw, str) and re.search(r"\d+\.\s*@", raw):  # option labels ("N. @handle — counts …")
        if re.search(r"\bmark all\b", raw.lower()):
            return list(card_nums), "Select all"
        raw = ", ".join(re.findall(r"(\d+)\.\s*@", raw))
    t = (raw or "").strip().lower()
    card = list(card_nums)
    m = re.search(r"\b(?:everyone|everybody|all|select all|mark all)\b[^0-9]*\b(?:except|but|apart from|other than|minus)\b(.*)", t)
    if m:
        ex = set(_nums(m.group(1)))
        return [n for n in card if n not in ex], "typed 'everyone except'"
    # 'Mark all for termination' is the select-all button label since 2026-10-16 4:52 PM PT
    # (older labels 'Select all' / 'Select all — mark for termination' still accepted).
    if re.search(r"\bselect all\b", t) or re.search(r"\bmark all\b", t):
        return card, "Select all"
    if re.fullmatch(r"(all|everyone|everybody|all of them|all \d+)\.?", t):
        return card, "card"
    if re.fullmatch(r"(none|nothing|no|keep all|skip)\.?", t):
        return [], "card"
    picked = [n for n in dict.fromkeys(_nums(t)) if n in card]
    return picked, "card"


def _prev_hash(path: Path) -> tuple[str | None, int]:
    if not path.exists():
        return None, 0
    lines = [l for l in path.read_bytes().splitlines() if l.strip()]
    if not lines:
        return None, 0
    last = lines[-1]
    n = int(json.loads(last)["entry_id"].split("-")[1])
    return hashlib.sha256(last).hexdigest(), n


def build(snapshot_id: str, card_no: int, card_nums: list[int], raw: str, keep=(), protect=(), always_keep=(),
          now: dt.datetime | None = None, via_override: str | None = None, mode: str = "automatic", *,
          user_key: str):
    snap = _snap_dir(user_key, snapshot_id)
    m = json.load(open(snap / "chat_numbering.json"))
    by_no = {a["chat_no"]: a for a in m["accounts"]}
    missing = [n for n in card_nums if n not in by_no]
    if missing:
        raise ValueError(f"chat numbers not in {snapshot_id} chat_numbering.json: {missing}")
    sel, via = parse_answer(raw, card_nums)
    via = via_override or via
    now = now or dt.datetime.now(PT)
    ts = now.strftime("%Y-%m-%d %H:%M PT")
    follow = {**{n: "Keep" for n in keep}, **{n: "Protect" for n in protect}, **{n: "Always Keep" for n in always_keep}}
    auto = mode == "automatic"
    select_all_auth = auto  # name kept; since 4:06 PM PT any selection in Automatic is authorized
    sel_status = ((f"Authorized: {'Select all' if via == 'Select all' else 'selected'} + done (recheck protections at execution)")
                  if auto else "Selected, awaiting confirmation")
    chat_rows, ops = [], []
    for n in card_nums:
        a = by_no[n]; s = n in sel; fu = follow.get(n, "")
        chat_rows.append([ts, snapshot_id, m["batch_id"], card_no, n, "@" + a["handle"], "Y",
                          "Terminate" if s else "Not selected", via if s or via != "Select all" else via, raw, fu,
                          sel_status if s else ("Held for final review card" if not fu else ""), f"{a['review_id']} ({a['tab']} row {a['sheet_row']})"])
        if a["tab"] == QUEUE_TAB:
            val = "Terminate" if s else ("Keep" if fu in ("Keep", "Protect", "Always Keep") else None)
            if val:
                ops.append({"tool": "write_range", "sheet": QUEUE_TAB, "cells": f"A{a['sheet_row']}", "values": [[val]],
                            "expect": {"B": a["review_id"]}})
            ops.append({"tool": "write_range", "sheet": QUEUE_TAB, "cells": f"{QUEUE_NOTE_COL}{a['sheet_row']}",
                        "values": [[f"{ts}: chat card {card_no} #{n} -> {('Terminate (authorized)' if select_all_auth else 'Terminate (awaiting confirmation)') if s else (fu or 'not selected')}"]],
                        "expect": {"B": a["review_id"]}})
    ops.insert(0, {"tool": "append_rows", "sheet": CHAT_TAB, "values": chat_rows})
    prev, last_n = _prev_hash(history_path(user_key))
    entry = {"entry_id": f"TH-{last_n + 1:04d}", "prev_entry_sha256": prev, "status": "CHAT_SELECTION",
             "user_key": user_key, "owner_x_handle": owner_handle_for(user_key), "snapshot_id": snapshot_id, "batch_id": m["batch_id"],
             "card_no": card_no, "card_chat_numbers": card_nums, "raw_answer": raw, "via": via,
             "selected": [{"chat_no": n, "handle": "@" + by_no[n]["handle"], "x_user_id": by_no[n]["user_id"],
                           "review_id": by_no[n]["review_id"]} for n in sel],
             "not_selected": [{"chat_no": n, "handle": "@" + by_no[n]["handle"]} for n in card_nums if n not in sel],
             "follow_up_decisions": {str(k): v for k, v in follow.items()},
             "authorization": (("SELECT_ALL_OWNER_YES" if via == "Select all" else "SELECTED_OWNER_YES")
                               + " (selected accounts authorized; protections rechecked at execution)" if auto
                               else "MANUAL (awaiting explicit confirmation)"),
             "held_for_final_review": [n for n in card_nums if n not in sel and n not in follow],
             "action": "NONE (recording only; removal is performed by the agent after execution-time protection recheck)", "bot_terminator_executed": False,
             "recorded_by": "src/record_chat_selection.py", "recorded_at_pt": ts}
    return {"selected": sel, "via": via, "chat_rows": chat_rows, "sheet_ops": ops, "history_entry": entry}


def build_final_review(snapshot_id: str, nums: list[int], raw: str, now: dt.datetime | None = None, *,
                       user_key: str):
    """Final review card for unselected accounts across all cards (2026-10-16 ~4:05 PM PT).

    Always Keep -> KEEPERS + always_keep list; Not a bot -> KEEPERS; Leave as is -> unchanged (logged,
    re-evaluated next scan). Everything is logged to Actions and history.jsonl.
    """
    import automatic_policy as ap
    snap = _snap_dir(user_key, snapshot_id)
    m = json.load(open(snap / "chat_numbering.json"))
    by_no = {a["chat_no"]: a for a in m["accounts"]}
    dec = ap.final_review_mapping(ap.parse_final_review(raw, nums))
    now = now or dt.datetime.now(PT); ts = now.strftime("%Y-%m-%d %H:%M PT")
    rows, ops, always_keep_add = [], [], []
    for n in nums:
        a = by_no[n]; d = dec[n]
        rows.append([ts, snapshot_id, m["batch_id"], "FINAL", n, "@" + a["handle"], "Y", "Not selected",
                     "final review card", raw, d["label"], d.get("note", f"-> {d['sheet']}") if d["sheet"] or d.get("note") else "",
                     f"{a['review_id']} ({a['tab']} row {a['sheet_row']})"])
        if d["add_to_always_keep_list"]:
            always_keep_add.append({"handle": a["handle"], "x_user_id": a["user_id"]})
        if d["sheet"] == "Keepers":
            ops.append({"tool": "keepers_add", "sheet": "Keepers", "handle": "@" + a["handle"], "x_user_id": a["user_id"],
                        "reason": d["label"] + " (owner, final review card)"})
            if a["tab"] == QUEUE_TAB:
                ops.append({"tool": "write_range", "sheet": QUEUE_TAB, "cells": f"A{a['sheet_row']}", "values": [["Keep"]],
                            "expect": {"B": a["review_id"]}})
        if a["tab"] == QUEUE_TAB:
            ops.append({"tool": "write_range", "sheet": QUEUE_TAB, "cells": f"{QUEUE_NOTE_COL}{a['sheet_row']}",
                        "values": [[f"{ts}: final review #{n} -> {d['label']}"]], "expect": {"B": a["review_id"]}})
    ops.insert(0, {"tool": "append_rows", "sheet": CHAT_TAB, "values": rows})
    prev, last_n = _prev_hash(history_path(user_key))
    entry = {"entry_id": f"TH-{last_n + 1:04d}", "prev_entry_sha256": prev, "status": "CHAT_FINAL_REVIEW",
             "user_key": user_key, "owner_x_handle": owner_handle_for(user_key), "snapshot_id": snapshot_id, "batch_id": m["batch_id"],
             "raw_answer": raw, "decisions": {str(n): dec[n]["kind"] for n in nums},
             "always_keep_add": always_keep_add,
             "keepers": [{"handle": by_no[n]["handle"], "x_user_id": by_no[n]["user_id"]} for n in nums
                         if dec[n]["sheet"] == "Keepers"],
             "action": "NONE (no X action)", "bot_terminator_executed": False,
             "recorded_by": "src/record_chat_selection.py build_final_review", "recorded_at_pt": ts}
    return {"decisions": {n: dec[n]["kind"] for n in nums}, "chat_rows": rows, "sheet_ops": ops,
            "always_keep_add": always_keep_add, "history_entry": entry}


def append_history(entry: dict, *, user_key: str) -> str:
    """Append to THIS user's history only; refuses an entry recorded for a different user."""
    if entry.get("user_key") not in (None, user_key):
        raise ValueError(f"history entry is for {entry.get('user_key')!r}, not {user_key!r}")
    path = history_path(user_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev, _ = _prev_hash(path)
    if entry["prev_entry_sha256"] != prev:
        raise RuntimeError("history.jsonl changed since build(); rebuild the entry")
    line = json.dumps(entry, ensure_ascii=False, separators=(", ", ": "))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    _storage().apply_user_permissions(user_key, ROOT)  # history.jsonl 0600, folders 0755
    return hashlib.sha256(line.encode()).hexdigest()


def save_always_keep_additions(additions: list[dict], *, user_key: str, note: str = "final review card") -> dict:
    """Persist final-review 'Always Keep' choices to THIS user's always_keep.json (gap fix 2026-10-16).

    Keyed by X user ID when known, handle as fallback; duplicates are skipped. A timestamped backup of the
    current list is written first through always_keep_backup.local_backup_copy."""
    st = _storage()
    try:
        import always_keep_backup as akb
    except ImportError:  # pragma: no cover
        from . import always_keep_backup as akb
    current = list(st.load_always_keep(ROOT, user_key=user_key))
    ids = {str(e.get("user_id")) for e in current if isinstance(e, dict) and e.get("user_id")}
    hs = {str(e.get("username") or e.get("handle") or "").lstrip("@").lower() for e in current if isinstance(e, dict)}
    hs |= {str(e).lstrip("@").lower() for e in current if isinstance(e, str)}
    added = []
    now = dt.datetime.now(PT).isoformat()
    for a in additions or []:
        uid = str(a.get("x_user_id") or a.get("user_id") or "").lstrip("'")
        uid = uid if uid.isdigit() else ""
        h = str(a.get("handle") or a.get("username") or "").lstrip("@")
        if (uid and uid in ids) or (not uid and h.lower() in hs) or (not uid and not h):
            continue
        entry = {"user_id": uid or None, "username": h or None, "key": "user_id" if uid else "handle",
                 "added_at": now, "source": note}
        current.append(entry); added.append(entry)
        if uid: ids.add(uid)
        if h: hs.add(h.lower())
    backup = None
    if added:
        backup = str(akb.local_backup_copy(ROOT, user_key=user_key))
        st.save_always_keep(current, ROOT, user_key=user_key)
    st.apply_user_permissions(user_key, ROOT)
    return {"added": added, "total": len(current), "backup": backup}


def commit_final_review(res: dict, *, user_key: str) -> dict:
    """Append the final-review history entry and save its Always Keep choices for this user."""
    tip = append_history(res["history_entry"], user_key=user_key)
    ak = save_always_keep_additions(res.get("always_keep_add") or [], user_key=user_key)
    return {"history_tip": tip, "always_keep": ak}


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--user" not in a:
        sys.exit("--user <user_key> is required (per-user history; no global fallback)")
    uk = a[a.index("--user") + 1]; del a[a.index("--user"):a.index("--user") + 2]
    opt = lambda k: [int(x) for x in a[a.index(k) + 1].split(",")] if k in a else []
    res = build(a[0], int(a[1]), [int(x) for x in a[2].split(",")], a[3], opt("--keep"), opt("--protect"), opt("--always-keep"), user_key=uk)
    if "--dry-run" not in a:
        res["history_tip"] = append_history(res["history_entry"], user_key=uk)
    print(json.dumps(res, ensure_ascii=False, indent=1))
