"""Inactive mutuals (beta feedback 2026-10-20).

A mutual = a follower the owner follows back. Mutuals are protected and are NEVER removed automatically or put
on a removal card. Mutuals that look inactive (dated evidence only) are shown a few per day (default 3) on a
SEPARATE card. Only an account the owner explicitly marks there is authorized for "Remove this follower" (it does
not unfollow them; owner-requested unfollows are out of scope). Every other protection still applies at removal
time (verified, Always Keep, Keepers, recent interactions; confirmed on X afterwards).
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

try:
    from . import storage as st
    from . import removal_runner as rr
    from . import card_text as ct
except ImportError:  # pragma: no cover
    import storage as st
    import removal_runner as rr
    import card_text as ct

DEFAULT_PER_DAY = 3
AUTH = "MUTUAL_REMOVE_OWNER_YES"


def _shown_path(user_key, root):
    return st.user_data_root(user_key, root) / "inactive_mutuals_shown.json"


def build_card(user_key: str, snapshot_id: str, root=None, *, today: str | None = None) -> dict:
    """Pick up to inactive_mutuals_per_day not-yet-shown inactive mutuals; returns {links_message, card_spec,
    accounts} or {"accounts": []}. Never shown twice; never more than the daily number per day."""
    cfg = st.load_user_config_dict(root, user_key=user_key)
    per_day = max(0, min(int(cfg.get("inactive_mutuals_per_day", DEFAULT_PER_DAY)), ct.CARD_SIZE))
    today = today or dt.datetime.now(rr._tz(cfg)).date().isoformat()
    shown = st.load_json(_shown_path(user_key, root), {}) or {}
    left = per_day - sum(1 for v in shown.values() if v == today)
    rows = json.loads((st.snapshot_dir(snapshot_id, root, user_key=user_key) / "candidates.json").read_text())
    keep = st.load_always_keep(root, user_key=user_key)
    keep_keys = {rr.st_key(k) if isinstance(k, dict) else str(k).lstrip("@").lower() for k in keep}
    keep_keys |= {rr.st_key(k) for k in rr.keepers_from_history(rr._history(user_key, root))}
    pick = []
    for r in rows:
        a = {"handle": r.get("handle") or r.get("username"), "x_user_id": r.get("user_id"), **r}
        key = rr.st_key(a)
        if left <= 0:
            break
        if not r.get("inactive_mutual") or key in shown or key in keep_keys or r.get("verified"):
            continue
        pick.append(a); left -= 1
    if not pick:
        return {"accounts": []}
    for a in pick:
        shown[rr.st_key(a)] = today
    st.save_json(_shown_path(user_key, root), shown)
    nums = [(i + 1, a) for i, a in enumerate(pick)]
    tail = lambda a: f"inactive {a.get('mutual_days_inactive', '?')} days, you follow them"  # noqa: E731
    opts = [{"label": ct.option_label(n, a, tail(a)), "value": str(n)} for n, a in nums]
    opts.append({"label": ct.MARK_ALL_TERMINATION, "value": "mark_all", "style": "danger"})
    numbering = {"snapshot_id": snapshot_id, "date": today,
                 "accounts": [{"n": n, "handle": ct.handle(a), "x_user_id": a.get("x_user_id")} for n, a in nums]}
    st.save_json(st.snapshot_dir(snapshot_id, root, user_key=user_key) / "inactive_mutuals_numbering.json", numbering)
    st.apply_user_permissions(user_key, root)
    return {"accounts": numbering["accounts"],
            "links_message": ct.links_message("Inactive mutuals (you follow them back; never removed automatically):",
                                              nums, reason_key="none"),
            "card_spec": {"multiSelect": True, "allowCustom": False, "options": opts,
                          "prompt": ct.prompt("Remove any of these inactive mutuals as followers? This does not "
                                              "unfollow them.")}}


def record_answer(user_key: str, snapshot_id: str, selected, *, root=None, raw_answer: str = "") -> dict:
    """Owner's explicit marks on the inactive-mutuals card -> INACTIVE_MUTUAL_SELECTION (authorized for removal
    with the 'you follow them' protection waived for these accounts only). Unmarked stay kept."""
    num = st.load_json(st.snapshot_dir(snapshot_id, root, user_key=user_key) / "inactive_mutuals_numbering.json", None)
    if not num:
        raise ValueError("no inactive-mutuals card for this snapshot")
    by_n = {a["n"]: a for a in num["accounts"]}
    sel = [str(x) for x in (selected or [])]
    nums = list(by_n) if any(s in ("mark_all", ct.MARK_ALL_TERMINATION) for s in sel) else \
        [int(s.split(".")[0]) for s in sel if s.split(".")[0].strip().isdigit() and int(s.split(".")[0]) in by_n]
    chosen = [{"handle": by_n[n]["handle"], "x_user_id": by_n[n]["x_user_id"]} for n in nums]
    if chosen:
        rr._append(user_key, root, {"status": "INACTIVE_MUTUAL_SELECTION",
                                    "at": dt.datetime.now().astimezone().isoformat(), "snapshot_id": snapshot_id,
                                    "authorization": AUTH, "selected": chosen, "raw_answer": raw_answer,
                                    "action": "NONE (authorization only)"})
    return {"authorized": [c["handle"] for c in chosen],
            "kept": [a["handle"] for n, a in by_n.items() if n not in nums]}
