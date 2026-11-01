"""List cards (v3). Every list, reviewed in batches of up to 4 (chat card widgets allow at most 6 options:
4 lists + the two action buttons). The message shown just above the card (links message) is the ONLY place the
soft-block side-effect warning and per-owner mutual/protected notes appear. Nothing is authorized until the
owner taps "Remove me from these lists" (batch cards) or "Remove me from selected lists" (final skipped card,
paged 4 at a time). The repeat-offender card is the only one that keeps Done."""
from __future__ import annotations

import datetime as dt
from typing import Any

try:
    from . import storage as st
    from . import removal_runner as rr
    from . import lists_scan as ls
except ImportError:  # pragma: no cover
    import storage as st
    import removal_runner as rr
    import lists_scan as ls

MAX_OPTIONS = 6   # chat card widgets allow at most 6 options
CARD_SIZE = 4     # list batches and final skipped-card pages: 4 lists + 2 action buttons = 6
OFFENDER_PAGE_SIZE = 5   # repeat-offender card: 5 owners + Done = 6
DONE = "Done"   # repeat-offender card only
REMOVE_BTN = "Remove me from these lists"
KEEP_BTN = "Keep all these lists"
FINAL_PROMPT = "Some lists were skipped during review. Keep these, or select ones to remove?"
FINAL_KEEP_BTN = "Keep all skipped lists"
FINAL_REMOVE_BTN = "Remove me from selected lists"
BATCH_PROMPT = ("Select the lists you want to leave and tap \"Remove me from these lists\" (unselected ones are "
                "skipped for now), or tap \"Keep all these lists\".")
AUTH = "LIST_SOFTBLOCK_OWNER_YES"
OWNER_BLOCK_AUTH = "LIST_OWNER_BLOCK_OWNER_YES"
SOFT_BLOCK_WARNING = ("Heads up: leaving a list means a soft block of its owner (block, then unblock right "
                      "away). That also removes the owner as your follower and ends any follow you have on them.")


def _protected(r: dict, cfg: dict, keep_keys: set) -> bool:
    h = str(r.get("owner_handle", "")).lstrip("@").lower()
    return h in {p.lstrip("@").lower() for p in cfg.get("protected_handles") or []} or h in keep_keys \
        or str(r.get("owner_id")) in keep_keys


def _yn(v) -> str:
    return "yes" if v is True else "no" if v is False else ls.NA


def _paths(user_key, sid, root):
    d = st.user_data_root(user_key, root) / "lists" / sid
    return d / "list_card_numbering.json", d / "list_review_state.json"


def list_block(n: int, r: dict, note: str = "") -> str:
    """One list in the links message: link, owner @handle underneath (x.com link), members, mutual."""
    owner = r["owner_handle"] + (f" ({r['owner_name']})" if r.get("owner_name") not in (None, ls.NA) else "")
    flag = ("⚠ " + r["tone_reason"]) if r["tone"] == ls.NEGATIVE else \
        ("watch: " + ("owner is a mutual" if r.get("mutual") is True else r["tone_reason"])) if r.get("watch") else ""
    lines = [f"{n}. {r['name']} — {r['url']}",
             f"   Owner: {owner} — {r.get('owner_url', ls.NA)}",
             f"   Members: {r['member_count']} · You follow each other (mutual): {_yn(r.get('mutual'))}"]
    if flag:
        lines.append("   " + flag)
    if r.get("owner_i_follow") is True:   # warn here only; nothing else gates it (unless the owner is Always Keep)
        lines.append("   You follow this owner: leaving this list will also unfollow them.")
    if note:
        lines.append("   Owner note: " + note)
    return "\n".join(lines)


def build_cards(user_key: str, snapshot_id: str, root=None) -> list[dict]:
    """EVERY list is reviewed, in batches of up to CARD_SIZE=4 (negative first, then needs-a-look/watched, then
    the rest). Each batch: links message (warning first), then a card with one button per list plus the two action
    buttons "Remove me from these lists" and "Keep all these lists" (<= 6 options; no Done)."""
    snap = ls.load(user_key, snapshot_id, root)
    if not ls.is_frozen_intact(user_key, snapshot_id, root):
        raise ValueError("lists snapshot is missing or changed; rescan lists")
    cfg = st.load_user_config_dict(root, user_key=user_key)
    keep = st.load_always_keep(root, user_key=user_key)
    keep_keys = {str(k.get("x_user_id") or k.get("handle") or "").lstrip("@").lower() if isinstance(k, dict)
                 else str(k).lstrip("@").lower() for k in keep}
    scope = snap.get("scope", ls.SCOPE_ALL)
    lists = [r for r in snap.get("lists", []) if (scope == ls.SCOPE_ALL or not r.get("protected"))
             and not r.get("kept")]
    cards, numbering = [], []
    for c0 in range(0, len(lists), CARD_SIZE):
        group = lists[c0:c0 + CARD_SIZE]
        card_no = len(cards) + 1
        lines = [SOFT_BLOCK_WARNING, "", f"Scope: {ls.SCOPE_LABELS[scope]}", f"Lists {c0 + 1}-{c0 + len(group)} of {len(lists)}:"]
        opts = []
        for i, r in enumerate(group, 1):
            lines.append(list_block(i, r, "protected / Always Keep" if _protected(r, cfg, keep_keys) else ""))
            opts.append({"label": f"{i}. {r['name']}", "value": str(i)})
            numbering.append({"card": card_no, "n": i, "list_id": r["list_id"], "url": r["url"], "name": r["name"],
                              "owner_handle": r["owner_handle"], "owner_id": r["owner_id"],
                              "row_index": r.get("row_index")})
        opts += [{"label": REMOVE_BTN, "value": "remove", "action": True},
                 {"label": KEEP_BTN, "value": "keep_all", "action": True}]
        assert len(opts) <= MAX_OPTIONS
        cards.append({"card": card_no, "of": 0, "links_message": "\n".join(lines),
                      "card_spec": {"multiSelect": True, "allowCustom": False, "options": opts,
                                    "prompt": BATCH_PROMPT}})
    for c in cards:
        c["of"] = len(cards)
    num_p, state_p = _paths(user_key, snapshot_id, root)
    st.save_json(num_p, numbering)
    if not st.load_json(state_p, None):
        st.save_json(state_p, {"next": 1, "total": len(cards), "answered": [], "skipped": [], "final_done": False, "final_page": 0})
    return cards


def next_batch(user_key: str, snapshot_id: str, root=None) -> dict:
    """The next batch to show, or {"done": True} once every batch has been answered."""
    cards = build_cards(user_key, snapshot_id, root)
    state = st.load_json(_paths(user_key, snapshot_id, root)[1], {}) or {}
    n = state.get("next", 1)
    if n > len(cards):
        return {"done": True, "total": len(cards)}
    return dict(cards[n - 1], done=False)


def _now():
    return dt.datetime.now().astimezone().isoformat()


def _has(sel, label, value):
    return any(x.lower() in (label.lower(), value) for x in sel)


def _authorize(user_key, snapshot_id, card, chosen, root, raw_answer, page=None):
    sha = (st.load_json(st.user_data_root(user_key, root) / "lists" / snapshot_id / "frozen.json", {}) or {})["sha256"]
    rr._append(user_key, root, {"status": "LIST_SELECTION", "at": _now(), "snapshot_id": snapshot_id,
                                "snapshot_sha256": sha, "card": card, "authorization": AUTH, "done": True,
                                "selected": chosen, "raw_answer": raw_answer, "action": "NONE (authorization only)",
                                **({"final_page": page} if page is not None else {})})


def _keep(user_key, snapshot_id, items, root, card):
    for a in items:
        ls.keep_list(user_key, snapshot_id, a, root)
        rr._append(user_key, root, {"status": "LIST_KEPT", "at": _now(), "snapshot_id": snapshot_id, "card": card,
                                    "list_id": a["list_id"], "list_name": a["name"], "url": a["url"],
                                    "owner_handle": a["owner_handle"], "action": "NONE (kept)"})


def record_answer(user_key: str, snapshot_id: str, card: int, selected, *, root=None, raw_answer: str = "") -> dict:
    """Batch card answer. "Remove me from these lists": the selected lists are marked for removal (authorization
    only; the runner acts later), unselected ones in the batch are skipped (not kept, not removed).
    "Keep all these lists": every list in the batch goes to Kept Lists. Either advances to the next batch.
    Neither button -> nothing recorded."""
    sel = [str(x).strip() for x in (selected or [])]
    remove, keep_all = _has(sel, REMOVE_BTN, "remove"), _has(sel, KEEP_BTN, "keep_all")
    if remove == keep_all:
        return {"authorized": [], "kept": [], "skipped": [], "recorded": False,
                "reason": "tap exactly one of the two buttons"}
    if not ls.is_frozen_intact(user_key, snapshot_id, root):
        raise ValueError("lists snapshot is missing or changed; nothing recorded; rescan lists")
    num_p, state_p = _paths(user_key, snapshot_id, root)
    batch = [a for a in (st.load_json(num_p, []) or []) if a["card"] == int(card)]
    by_n = {a["n"]: a for a in batch}
    picked = {int(x.split(".")[0]) for x in sel if x.split(".")[0].isdigit()} & set(by_n)
    state = st.load_json(state_p, {}) or {"next": 1, "answered": [], "skipped": []}
    chosen, kept, skipped = [], [], []
    if keep_all:
        kept = batch
        _keep(user_key, snapshot_id, kept, root, int(card))
    else:
        chosen = [by_n[n] for n in sorted(picked)]
        skipped = [a for a in batch if a["n"] not in picked]
        if chosen:
            _authorize(user_key, snapshot_id, int(card), chosen, root, raw_answer)
        state["skipped"] = state.get("skipped", []) + skipped
    state["answered"] = sorted(set(state.get("answered", [])) | {int(card)})
    state["next"] = max(state.get("next", 1), int(card) + 1)
    st.save_json(state_p, state)
    return {"authorized": [c["name"] for c in chosen], "kept": [c["name"] for c in kept],
            "skipped": [c["name"] for c in skipped], "recorded": True,
            "next_card": state["next"] if state["next"] <= state.get("total", state["next"]) else None}


def _final_pages(skipped: list) -> int:
    return max(1, -(-len(skipped) // CARD_SIZE))


def final_skipped_card(user_key: str, snapshot_id: str, root=None) -> dict | None:
    """After the last batch: the skipped lists, paged CARD_SIZE (4) per card. Returns the card for the current page
    (state["final_page"], 0-based), or None if there's nothing skipped or it's all answered. Numbering is continuous
    across pages (page 2 starts at 5). Buttons: "Remove me from selected lists" then "Keep all skipped lists"."""
    state = st.load_json(_paths(user_key, snapshot_id, root)[1], {}) or {}
    if state.get("next", 1) <= state.get("total", 0) or state.get("final_done") or not state.get("skipped"):
        return None
    sk = state["skipped"]
    pages, page = _final_pages(sk), int(state.get("final_page", 0))
    if page >= pages:
        return None
    first = page * CARD_SIZE
    snap = {r["list_id"]: r for r in ls.load(user_key, snapshot_id, root).get("lists", [])}
    lines = [SOFT_BLOCK_WARNING, "", f"Skipped lists ({page + 1} of {pages}):"]
    opts = []
    for i, a in enumerate(sk[first:first + CARD_SIZE], first + 1):
        lines.append(list_block(i, snap.get(a["list_id"], a)) if a["list_id"] in snap else f"{i}. {a['name']} — {a['url']}")
        opts.append({"label": f"{i}. {a['name']}", "value": str(i)})
    opts += [{"label": FINAL_REMOVE_BTN, "value": "remove", "action": True},
             {"label": FINAL_KEEP_BTN, "value": "keep_all", "action": True}]
    assert len(opts) <= MAX_OPTIONS
    return {"card": "final", "page": page + 1, "of": pages, "links_message": "\n".join(lines),
            "card_spec": {"multiSelect": True, "allowCustom": False, "options": opts, "prompt": FINAL_PROMPT}}


def record_final_answer(user_key: str, snapshot_id: str, selected, *, root=None, raw_answer: str = "") -> dict:
    """Answer for the CURRENT page of the final skipped card (one record per page). "Keep all skipped lists" -> every
    list on this page goes to Kept Lists. "Remove me from selected lists" -> the selected ones on this page are marked
    for removal; unselected ones STAY IN REVIEW (not kept, not removed; shown again next run). Advances the page
    index in state; the last page sets final_done."""
    sel = [str(x).strip() for x in (selected or [])]
    remove, keep_all = _has(sel, FINAL_REMOVE_BTN, "remove"), _has(sel, FINAL_KEEP_BTN, "keep_all")
    if remove == keep_all:
        return {"recorded": False, "reason": "tap exactly one of the two buttons"}
    if not ls.is_frozen_intact(user_key, snapshot_id, root):
        raise ValueError("lists snapshot is missing or changed; nothing recorded; rescan lists")
    state_p = _paths(user_key, snapshot_id, root)[1]
    state = st.load_json(state_p, {}) or {}
    sk = state.get("skipped", [])
    pages, page = _final_pages(sk), int(state.get("final_page", 0))
    if state.get("final_done") or page >= pages:
        return {"recorded": False, "reason": "no skipped-lists page is waiting for an answer"}
    first = page * CARD_SIZE
    on_page = list(enumerate(sk[first:first + CARD_SIZE], first + 1))
    picked = {int(x.split(".")[0]) for x in sel if x.split(".")[0].isdigit()}
    chosen, kept, stay = [], [], []
    if keep_all:
        kept = [a for _, a in on_page]
        _keep(user_key, snapshot_id, kept, root, 0)
    else:
        chosen = [a for i, a in on_page if i in picked]
        stay = [a for i, a in on_page if i not in picked]
        if chosen:
            _authorize(user_key, snapshot_id, 0, chosen, root, raw_answer, page=page + 1)
    state["in_review"] = state.get("in_review", []) + stay
    state["final_page"] = page + 1
    state["final_done"] = state["final_page"] >= pages
    st.save_json(state_p, state)
    return {"recorded": True, "authorized": [a["name"] for a in chosen], "kept": [a["name"] for a in kept],
            "in_review": [a["name"] for a in stay], "page": page + 1, "of": pages,
            "next_page": None if state["final_done"] else state["final_page"] + 1}


def build_repeat_offender_card(user_key: str, returns: list[dict], page: int = 1) -> dict:
    """Keycard: owners soft-blocked before who put the owner on a list again. Buttons + Done. Paged so a card never
    has more than MAX_OPTIONS=6 options: OFFENDER_PAGE_SIZE (5) owners + Done. Numbering is continuous across pages;
    the result carries page / of (call again with page=2, ... until page == of)."""
    if not returns:
        return {"accounts": []}
    pages = max(1, -(-len(returns) // OFFENDER_PAGE_SIZE))
    page = min(max(int(page), 1), pages)
    first = (page - 1) * OFFENDER_PAGE_SIZE
    shown = returns[first:first + OFFENDER_PAGE_SIZE]
    lines = ["These list owners added you to a list again after you left one of theirs (returned from the "
             "graveyard). Tapping them and Done blocks them permanently:" + (f" (page {page} of {pages})" if pages > 1 else "")]
    opts = []
    for i, r in enumerate(shown, first + 1):
        lines.append(f"{i}. {r['owner_handle']} — https://x.com/{str(r['owner_handle']).lstrip('@')} — "
                     f"list: {r['name']} — {r['url']}")
        opts.append({"label": f"{i}. {r['owner_handle']}", "value": str(i)})
    opts.append({"label": DONE, "value": "done"})
    assert len(opts) <= MAX_OPTIONS
    return {"accounts": shown, "page": page, "of": pages, "links_message": "\n".join(lines),
            "card_spec": {"multiSelect": True, "allowCustom": False, "options": opts,
                          "prompt": "Pick the owners to block for good, then tap Done."}}


def record_repeat_offender_answer(user_key: str, list_snapshot_id: str, returns: list[dict], selected, *,
                                  root=None, raw_answer: str = "", page: int | None = None) -> dict:
    """Answer for one repeat-offender card page (pass page= to accept only that page's numbers; None = any)."""
    sel = [str(x).strip() for x in (selected or [])]
    if not any(s.lower() == "done" for s in sel):
        return {"authorized": [], "recorded": False, "reason": "Done was not tapped"}
    by_n = {i: r for i, r in enumerate(returns, 1)}
    if page is not None:
        lo = (int(page) - 1) * OFFENDER_PAGE_SIZE
        by_n = {i: r for i, r in by_n.items() if lo < i <= lo + OFFENDER_PAGE_SIZE}
    chosen = [by_n[int(s)] for s in sel if s.isdigit() and int(s) in by_n]
    if chosen:
        rr._append(user_key, root, {"status": "LIST_OWNER_BLOCK_SELECTION",
                                    "at": dt.datetime.now().astimezone().isoformat(),
                                    "snapshot_id": list_snapshot_id, "authorization": OWNER_BLOCK_AUTH,
                                    "done": True, "selected": chosen, "raw_answer": raw_answer,
                                    "action": "NONE (authorization only)"})
    return {"authorized": [c["owner_handle"] for c in chosen], "recorded": bool(chosen)}
