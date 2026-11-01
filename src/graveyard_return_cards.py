"""Graveyard returns chat flow (all users): notice, disclaimer, links list, block cards, answer recording.

Built from the approved mockup (2026-10-16, Option A chunking: up to 5 returnees + "Mark all for blocking" per
card, max 6 options). Input is graveyard_returns.json from graveyard_returns.detect_returns. Output messages, in
order: notice -> disclaimer -> links list -> card 1..k. Card answers become per-returnee BLOCK_AUTHORIZATION
entries (graveyard_returns.record_block_authorization), which graveyard_returns.run_blocks consumes. Unmarked
returnees are logged as kept in review (GRAVEYARD_RETURN_KEPT_IN_REVIEW) and listed for the final review card.
Dates come only from recorded data; anything missing is shown as "Not available".
With graveyard_return_action = "auto_block" the notice and list are still shown, but no cards are built: the
owner's standing setting already authorizes blocks (existing run_blocks semantics).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

try:
    from . import storage as st
    from . import graveyard_returns as gr
    from . import removal_runner as rr
except ImportError:  # pragma: no cover
    import storage as st
    import graveyard_returns as gr
    import removal_runner as rr

CARD_SIZE = 5
MAX_OPTIONS = 6
MARK_ALL_LABEL = "Mark all for blocking"
REVIEW_NOTE = "Unmarked accounts will be kept in review."
NA = "Not available"
DISCLAIMER = ("Heads up: these accounts are repeat offenders. I removed them before and they followed you again. "
              "This round won't just remove them as followers. They'll be blocked so they can't come back and reattach.")


def notice_text(n: int) -> str | None:
    if n <= 0:
        return None
    if n == 1:
        return "1 bot has reattached to your account, and I have it recorded in the Graveyard."
    return f"{n} bots have reattached to your account, and I have them recorded in the Graveyard."


def _date(ts, tz) -> str:
    if not ts:
        return NA
    try:
        d = dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return NA
    if d.tzinfo is not None and tz is not None:
        d = d.astimezone(tz)
    return f"{d.strftime('%b')} {d.day}"


def list_line(r: dict, tz=None) -> str:
    h = r["handle"]
    line = (f"{r['n']}. [@{h}](https://x.com/{h}) — removed {_date(r.get('removed_at'), tz)}, "
            f"came back {_date(r.get('returned_at'), tz)}")
    if r.get("previous_handle"):
        line += f" (new handle, was @{r['previous_handle']})"
    return line


def option_label(r: dict, tz=None) -> str:
    try:
        from . import card_text as ct
    except ImportError:  # pragma: no cover
        import card_text as ct
    lab = f"{r['n']}. @{r['handle']} — {ct.counts_text(r)} — removed {_date(r.get('removed_at'), tz)}, followed again"
    return lab + (" (new handle)" if r.get("previous_handle") else "")


def build_flow(returns: list[dict], *, tz=None, auto_block: bool = False) -> dict:
    """Return {"messages": [notice, disclaimer, list], "cards": [...], "numbering": {...}} (empty if no returnees)."""
    rets = [dict(r, n=i + 1) for i, r in enumerate(returns)]  # continuous numbering, synced list <-> cards
    n = len(rets)
    if n == 0:
        return {"messages": [], "cards": [], "numbering": {"accounts": [], "cards": {}}}
    msgs = [notice_text(n), DISCLAIMER, "\n".join(list_line(r, tz) for r in rets)]
    cards, card_map = [], {}
    if not auto_block:
        chunks = [rets[i:i + CARD_SIZE] for i in range(0, n, CARD_SIZE)]
        k = len(chunks)
        for i, ch in enumerate(chunks, 1):
            opts = [{"label": option_label(r, tz), "value": str(r["n"])} for r in ch]
            opts.append({"label": MARK_ALL_LABEL, "value": "mark_all", "style": "danger"})
            assert len(opts) <= MAX_OPTIONS
            cards.append({"card": i, "of": k, "multiSelect": True, "allowCustom": False,
                          "prompt": f"Graveyard returns ({i} of {k}): block these repeat followers? {REVIEW_NOTE}",
                          "options": opts})
            card_map[str(i)] = [r["n"] for r in ch]
    else:
        msgs.append("Your auto-block setting is on, so these will be blocked under that setting "
                    "(protections are still rechecked right before each block).")
    numbering = {"cards": card_map, "accounts": [{"n": r["n"], "handle": r["handle"], "x_user_id": r.get("x_user_id"),
                                                   "previous_handle": r.get("previous_handle")} for r in rets]}
    return {"messages": msgs, "cards": cards, "numbering": numbering}


def numbering_path(user_key: str, snapshot_id: str, root=None):
    return st.snapshot_dir(snapshot_id, root, user_key=user_key) / "graveyard_return_numbering.json"


def build_flow_for_snapshot(user_key: str, snapshot_id: str, root=None) -> dict:
    """Load this user's detected returns, build the flow and persist the numbering (0600) like removal cards."""
    cfg = st.load_user_config_dict(root, user_key=user_key)
    flow = build_flow(gr.load_returns(user_key, snapshot_id, root), tz=rr._tz(cfg),
                      auto_block=cfg.get("graveyard_return_action") == "auto_block")
    if flow["numbering"]["accounts"]:
        st.save_json(numbering_path(user_key, snapshot_id, root), dict(flow["numbering"], snapshot_id=snapshot_id))
        st.apply_user_permissions(user_key, root)
    return flow


def parse_answer(selected, card_nums: list[int]) -> list[int]:
    """selected: list of option labels/values (or a raw string). 'Mark all for blocking' -> whole card.
    Numbers outside this card are ignored. Empty -> []."""
    items = [selected] if isinstance(selected, str) else list(selected or [])
    out = []
    for it in items:
        t = str(it).strip()
        if not t:
            continue
        if t == "mark_all" or MARK_ALL_LABEL.lower() in t.lower():
            return list(card_nums)
        m = re.match(r"^(\d+)", t)
        if m and int(m.group(1)) in card_nums and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return out


def record_card_answer(user_key: str, snapshot_id: str, card_no: int, selected, *, root=None) -> dict:
    """Selected -> per-returnee block authorization; unmarked -> logged as kept in review."""
    num = st.load_json(numbering_path(user_key, snapshot_id, root), None)
    if not num:
        raise ValueError("no Graveyard returns card numbering for this snapshot; build the cards first")
    card_nums = num["cards"].get(str(card_no))
    if not card_nums:
        raise ValueError(f"card {card_no} does not exist")
    picked = parse_answer(selected, card_nums)
    by_n = {a["n"]: a for a in num["accounts"]}
    raw = selected if isinstance(selected, str) else json.dumps(list(selected or []), ensure_ascii=False)
    auth = None
    if picked:
        auth = gr.record_block_authorization(user_key, snapshot_id, [by_n[n] for n in picked], root=root,
                                             raw_answer=f"card {card_no}: {raw}")
    kept = [n for n in card_nums if n not in picked]
    if kept:
        rr._append(user_key, root, {"status": "GRAVEYARD_RETURN_KEPT_IN_REVIEW",
                                    "at": dt.datetime.now().astimezone().isoformat(), "snapshot_id": snapshot_id,
                                    "card": card_no, "kept": [{"n": n, "handle": by_n[n]["handle"],
                                                               "x_user_id": by_n[n].get("x_user_id")} for n in kept],
                                    "next": "final review card (Always Keep / Not a bot / Leave as is)",
                                    "action": "NONE"})
    num.setdefault("answers", {})[str(card_no)] = {"authorized": picked, "kept_in_review": kept}
    st.save_json(numbering_path(user_key, snapshot_id, root), num)
    st.apply_user_permissions(user_key, root)
    return {"authorized": picked, "kept_in_review": kept, "authorization_entry": auth}


def kept_in_review(user_key: str, snapshot_id: str, root=None) -> list[dict]:
    """Returnees left unmarked on answered cards: input for the final review card."""
    num = st.load_json(numbering_path(user_key, snapshot_id, root), {}) or {}
    by_n = {a["n"]: a for a in num.get("accounts", [])}
    return [by_n[n] for a in (num.get("answers") or {}).values() for n in a["kept_in_review"]]
