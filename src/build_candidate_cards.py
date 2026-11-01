"""Build chat candidate cards ONLY from In review candidates (added 2026-10-16 ~4:13 PM PT).

Root cause it fixes: on 2026-10-16 the card numbering for an early test snapshot was built by hand
and numbered all 15 "non-protected" accounts, including 5 Keep-classified accounts already in KEEPERS
(cards 3 #11-15). Card 3 was voided; nothing was removed.

Rules (hard exclusions run BEFORE numbering; an excluded account can never get a chat number):
- Allowed classifications only: Likely bot (BOT-LIKE, incl. + INACTIVE), Inactive, Needs your look (REVIEW).
- Must currently be a In review row.
- Excluded by handle OR X user id: KEEPERS, PROTECTED, GRAVEYARD, always_keep list.
- Excluded: accounts the user follows, verified accounts, Keep / Protected classifications.
Unverified status alone never makes an account a candidate.
"""
from __future__ import annotations

ALLOWED_PREFIXES = ("BOT-LIKE", "INACTIVE", "REVIEW", "LIKELY BOT", "NEEDS YOUR LOOK")  # e.g. 'BOT-LIKE / REMOVE CANDIDATE'
CARD_SIZE = 5
MAX_OPTIONS = 6   # chat card widgets allow at most 6 options (5 accounts + select-all)
# Card UI settings (owner feedback 2026-10-16 4:48 PM PT)
# Product-wide defaults live in product_config (all users); re-exported here.
try:
    from product_config import CARD_SELECT_ALL_LABEL as SELECT_ALL_LABEL, CARD_ALLOW_FREE_TEXT as ALLOW_FREE_TEXT, CARD_REVIEW_NOTE as REVIEW_NOTE
except ImportError:  # package import
    from .product_config import CARD_SELECT_ALL_LABEL as SELECT_ALL_LABEL, CARD_ALLOW_FREE_TEXT as ALLOW_FREE_TEXT, CARD_REVIEW_NOTE as REVIEW_NOTE


try:
    import card_text as ct
except ImportError:  # package import
    from . import card_text as ct


def _h(x) -> str:
    return (x or "").strip().lstrip("@").lower()


def _id(x) -> str:
    return str(x or "").strip().lstrip("'")


def _ids_handles(rows):
    hs, ids = set(), set()
    for r in rows or []:
        if isinstance(r, dict):
            hs.add(_h(r.get("handle") or r.get("username"))); ids.add(_id(r.get("user_id") or r.get("x_user_id")))
        else:
            hs.add(_h(r))
    hs.discard(""); ids.discard("")
    return hs, ids


try:
    from .protections import verified_reason
except ImportError:  # pragma: no cover
    from protections import verified_reason


def exclusion_reason(acct: dict, *, keepers=(), protected=(), graveyard=(), always_keep=(), queue_ids=None):
    h, uid = _h(acct.get("handle") or acct.get("username")), _id(acct.get("user_id"))
    for name, src in (("Keepers", keepers), ("PROTECTED", protected), ("GRAVEYARD", graveyard), ("always_keep", always_keep)):
        hs, ids = _ids_handles(src)
        if h in hs or (uid and uid in ids):
            return f"in {name}"
    if acct.get("i_follow"):
        return "you follow this account"
    if verified_reason(acct):
        return "verified"
    if acct.get("always_keep"):
        return "Always Keep flag"
    if acct.get("removal_card_eligible") is False:
        return "kept by the person check (real replies, real/verified followers, or no bot signs in the numbers)"
    if acct.get("inactive_mutual"):
        return "inactive mutual (goes on the separate inactive-mutuals card, never a removal card)"
    lab = (acct.get("label") or "").upper().strip()
    if not lab.startswith(ALLOWED_PREFIXES):
        return f"classification {acct.get('label')!r} is not a candidate"
    if queue_ids is not None and uid not in {_id(x) for x in queue_ids} and acct.get("review_id") not in queue_ids:
        return "not a In review row"
    return None


def build_cards(accounts: list[dict], *, keepers=(), protected=(), graveyard=(), always_keep=(), queue_ids=None,
                card_size: int = CARD_SIZE, start_no: int = 1, snapshot_dir=None, batch_id: str | None = None,
                first_sheet_row: int = 4):
    """Return {"cards": {card: [chat_no...]}, "accounts": [...numbered...], "excluded": [{handle, reason}]}.

    When snapshot_dir is given, the card answer file chat_numbering.json (the format record_chat_selection
    reads) is written there automatically for every card built (gap fix 2026-10-16)."""
    kept, excluded = [], []
    for a in accounts:
        why = exclusion_reason(a, keepers=keepers, protected=protected, graveyard=graveyard,
                               always_keep=always_keep, queue_ids=queue_ids)
        (excluded.append({"handle": a.get("handle") or a.get("username"), "user_id": a.get("user_id"), "reason": why})
         if why else kept.append(a))
    # v3.5.7: BOT-LIKE (incl. INACTIVE) first, then REVIEW; stable within each group
    kept.sort(key=lambda a: 0 if ct.kind(a) == "bot" else 1)
    cards, out = {}, []
    for i, a in enumerate(kept):
        n = start_no + i; c = i // card_size + 1
        out.append(dict(a, chat_no=n, card=c)); cards.setdefault(str(c), []).append(n)
    res = {"cards": cards, "accounts": out, "excluded": excluded,
            "card_ui": {"select_all_label": SELECT_ALL_LABEL, "allow_free_text": ALLOW_FREE_TEXT,
                        "review_note": REVIEW_NOTE, "review_note_position": "prompt, above the options",
                        "select_all_value": "mark_all"}}
    res["render"] = render_cards(res)
    if snapshot_dir is not None:
        res["chat_numbering_path"] = str(write_chat_numbering(res, snapshot_dir, batch_id=batch_id,
                                                              first_sheet_row=first_sheet_row))
    return res


def render_cards(res: dict) -> list[dict]:
    """v3.5.7 rounds: per round, the round message (chat, sent first) then the card titled
    'Round N of M: which should I terminate?' (multi-select, no free text, accounts + 'Mark all K for termination')."""
    by_card: dict[str, list[dict]] = {}
    for a in res["accounts"]:
        by_card.setdefault(str(a["card"]), []).append(a)
    k = len(by_card)
    out = []
    for c, accts in by_card.items():
        nums = [(a["chat_no"], a) for a in accts]
        opts = [{"label": ct.option_label(n, a, a.get("label_short") or ""), "value": str(n)} for n, a in nums]
        opts.append({"label": ct.mark_all_label(len(nums)), "value": "mark_all", "style": "danger"})
        assert len(opts) <= MAX_OPTIONS  # chat card widgets allow at most 6 options
        msg = ct.round_message(int(c), k, nums)
        out.append({"card": int(c), "round": int(c), "of": k, "round_message": msg, "links_message": msg,
                    "card_spec": {"title": ct.round_title(int(c), k), "multiSelect": True, "allowCustom": False,
                                  "options": opts, "prompt": ct.prompt(ct.round_title(int(c), k))}})
    return out


def chat_numbering(res: dict, *, snapshot_id: str, batch_id: str | None = None, first_sheet_row: int = 4) -> dict:
    """Card answer map: {snapshot_id, batch_id, accounts:[{chat_no, card, handle, user_id, name, review_id, label,
    tab, sheet_row}]}. review_id matches populate_review_sheet.build_rows (BT-{batch}-{review_number:03d})."""
    batch = batch_id or snapshot_id
    accts = []
    for i, a in enumerate(res["accounts"]):
        rn = a.get("review_number") or a["chat_no"]
        accts.append({"chat_no": a["chat_no"], "card": a["card"], "handle": _h(a.get("handle") or a.get("username")),
                      "user_id": _id(a.get("user_id")) or "Not available", "name": a.get("name"),
                      "review_id": a.get("review_id") or f"BT-{batch}-{int(rn):03d}", "label": a.get("label"),
                      "tab": "In review", "sheet_row": first_sheet_row + i})
    return {"snapshot_id": snapshot_id, "batch_id": batch, "cards": res["cards"], "accounts": accts}


def write_chat_numbering(res: dict, snapshot_dir, *, batch_id: str | None = None, first_sheet_row: int = 4):
    import json, os
    from pathlib import Path
    d = Path(snapshot_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "chat_numbering.json"
    p.write_text(json.dumps(chat_numbering(res, snapshot_id=d.name, batch_id=batch_id, first_sheet_row=first_sheet_row),
                            ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(d, 0o755); os.chmod(p, 0o600)  # card answer file is main data
    return p


def build_cards_for_snapshot(user_key: str, snapshot_id: str, root=None, *, batch_id: str | None = None,
                             first_sheet_row: int = 4, **kw) -> dict:
    """Load this user's snapshot candidates (review-number order), build cards and write chat_numbering.json."""
    import json
    try:
        from storage import snapshot_dir as _sd, load_always_keep
    except ImportError:  # pragma: no cover
        from .storage import snapshot_dir as _sd, load_always_keep
    d = _sd(snapshot_id, root, user_key=user_key)
    cands = sorted(json.loads((d / "candidates.json").read_text(encoding="utf-8")),
                   key=lambda r: r.get("review_number") or 0)
    kw.setdefault("always_keep", load_always_keep(root, user_key=user_key))
    # Graveyard returns never get a removal card (they get their own block card via graveyard_returns).
    try:
        from graveyard_returns import removed_entries
    except ImportError:  # pragma: no cover
        from .graveyard_returns import removed_entries
    gy = [{"handle": g.get("handle"), "user_id": g.get("x_user_id")} for g in removed_entries(user_key, root)]
    kw["graveyard"] = list(kw.get("graveyard") or []) + gy
    cands = [c for c in cands if not c.get("graveyard_return")]
    return build_cards([dict(c, handle=c.get("handle") or c.get("username")) for c in cands], snapshot_dir=d,
                       batch_id=batch_id, first_sheet_row=first_sheet_row, **kw)
