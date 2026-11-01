"""v3.5.7 round-based review (owner default termination UX).

Flagged accounts go in rounds of 5, BOT-LIKE first, then REVIEW (build_candidate_cards.render_cards).
Each round:
  (a) send round["round_message"] in chat: number, clickable markdown x.com/@handle link, label (likely bot /
      worth a closer look), posts, followers, following, join date and reason;
  (b) send round["card_spec"]: multi-select "Round N of M: which should I terminate?" with the round's accounts plus
      "Mark all K for termination" (danger; K = the round's real count).
After the pick, run_round() records it, terminates the selected accounts right away (about 10 s apart, each verified
on X and written to the Graveyard immediately), pushes the backup (sheet-mirror format to the owner's GitHub records
repo), and returns a checklist to report. Unselected accounts stay in review. Any X warning or rate limit stops
removals for the day. Then send the next round until all rounds are done. There is no all-at-once list and no
typed-number path: picks come only from the round card.
"""
from __future__ import annotations

import io
import time
from pathlib import Path

try:
    import record_chat_selection as rcs, removal_runner as rr, records, storage as st
except ImportError:  # package import
    from . import record_chat_selection as rcs, removal_runner as rr, records, storage as st


def _h(x) -> str:
    return str(x or "").strip().lstrip("@").lower()


def rounds(cards_res: dict) -> list[dict]:
    return list(cards_res["render"])


def run_round(user_key: str, snapshot_id: str, round_no: int, answer, adapter, *, root=None,
              sleep=time.sleep, now_fn=None, mode: str = "automatic", backup=True) -> dict:
    old = rcs.ROOT
    rcs.ROOT = Path(root) if root is not None else st.PROJECT_ROOT
    try:
        m = st.load_json(st.snapshot_dir(snapshot_id, root, user_key=user_key) / "chat_numbering.json", {})
        nums = [int(n) for n in m["cards"][str(round_no)]]
        total = len(m["cards"])
        b = rcs.build(snapshot_id, round_no, nums, answer, user_key=user_key, mode=mode)
        rcs.append_history(b["history_entry"], user_key=user_key)
    finally:
        rcs.ROOT = old
    by_no = {a["chat_no"]: a for a in m["accounts"]}
    sel = set(b["selected"])
    summary = None
    if sel:
        try:
            summary = rr.run_removals(user_key, snapshot_id, adapter, root=root, sleep=sleep, now_fn=now_fn,
                                      out=io.StringIO())
        except rr.RefusedError as exc:
            summary = {"removed_handles": [], "skipped": [], "stopped_reason": "refused", "stopped_message": str(exc)}
    done = {_h(h) for h in (summary or {}).get("removed_handles", [])}
    skipped = {_h(s["handle"]): s["reason"] for s in (summary or {}).get("skipped", [])}
    stop = (summary or {}).get("stopped_reason")
    lines = []
    for n in nums:
        h = by_no[n]["handle"]
        if n not in sel:
            lines.append(f"• {n}. @{h} — kept in review")
        elif _h(h) in done:
            lines.append(f"✅ {n}. @{h} — terminated (verified on X, logged to the Graveyard)")
        elif _h(h) in skipped:
            lines.append(f"⏭ {n}. @{h} — not removed: {skipped[_h(h)]}")
        else:
            lines.append(f"⏸ {n}. @{h} — not done" + (f": stopped ({stop})" if stop else ""))
    day_stopped = stop in ("warning", "rate limit")
    if day_stopped:
        lines.append("X showed a warning, so I stopped for today. The rest stay selected for tomorrow's run.")
    bk = records.after_write(user_key, root) if backup else {}
    nxt = round_no + 1 if round_no < total and not day_stopped else None
    return {"round": round_no, "of": total, "selected": sorted(sel), "summary": summary, "backup": bk,
            "checklist": lines, "report": f"Round {round_no} of {total} done:\n" + "\n".join(lines),
            "day_stopped": day_stopped, "next_round": nxt}
