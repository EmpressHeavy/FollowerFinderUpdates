"""Review cards the owner can pick from (v3.5.35).

Verified follower review and interest discovery both use this. A card lists only accounts
rated Follow back or Maybe, at most 5, with nothing pre-ticked. Follow all selects only
those accounts. Skip is not an option: callers still review and remember Skip accounts
so they are not offered again, and they do not put them on a card or in a user-facing list.
When every reviewed account is Skip, the note is NO_SUITABLE_NOTE and there are no cards.
"""
from __future__ import annotations

try:
    from . import verified_followers as vf
except ImportError:  # pragma: no cover
    import verified_followers as vf

FOLLOW_ALL = "__follow_all__"
FOLLOW_ALL_LABEL = "Follow all on this card"
RECHECKED_LABEL = "Rechecked"
OFFERED_RECS = ("Follow back", "Maybe")
NO_SUITABLE_NOTE = "No suitable candidates this round."
_MISSING = object()


def is_offered(rec) -> bool:
    """True only for ratings a review card may show."""
    return rec in OFFERED_RECS


def is_selectable(rec, *, hard_skip: bool = False) -> bool:
    """Follow / Follow all may use this id. Explicit Skip and hard skips never qualify.
    A missing rating (None or "") is a legacy pending row from before ratings were stored:
    it was on the card, so the owner's tick still counts. Any other label does not."""
    if hard_skip or rec == "Skip":
        return False
    return rec in OFFERED_RECS or rec in (None, "")


def selectable_ids(card_ids, rec_by_id, *, hard_skip_ids=()) -> list[str]:
    """Card order, only ids we are allowed to follow. An id with no stored rating is omitted
    here; resolve_picks raises if the owner ticks that id (it is not a known Skip)."""
    hard = {str(i) for i in (hard_skip_ids or ())}
    out = []
    for i in (str(x) for x in card_ids):
        if i not in rec_by_id:
            continue
        if is_selectable(rec_by_id.get(i), hard_skip=i in hard):
            out.append(i)
    return out


def _as_list(raw, card_no) -> list:
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise ValueError(f"picks for card {card_no} must be a list")
    return list(raw)


def resolve_picks(card_no, card_ids, raw, rec_by_id, *, hard_skip_ids=(),
                  follow_all: str = FOLLOW_ALL) -> list[str]:
    """Owner's answer for one pending card -> user ids to follow, in card order.

    Follow all -> every selectable id on the card (legacy Skip rows on that card are left out).
    An id that is not on the card raises ValueError. A known Skip or hard skip that is still
    on a legacy card is dropped, not followed. An id on the card with no stored rating raises
    if it was ticked (Follow all simply leaves it out). Nothing is ticked by default: empty
    raw follows nobody. This does not follow anyone.
    """
    ids = [str(i) for i in card_ids]
    idset = set(ids)
    chosen = [str(v) for v in _as_list(raw, card_no)]
    unknown = [v for v in chosen if v != follow_all and v not in idset]
    if unknown:
        raise ValueError(f"picked ids not on card {card_no}: {unknown}")
    hard = {str(i) for i in (hard_skip_ids or ())}
    eligible = selectable_ids(ids, rec_by_id, hard_skip_ids=hard)
    if follow_all in chosen:
        return eligible
    eligible_set = set(eligible)
    bad = []
    for v in chosen:
        if v in eligible_set:
            continue
        if v in hard or (v in rec_by_id and rec_by_id.get(v) == "Skip"):
            continue
        bad.append(v)
    if bad:
        raise ValueError(f"picked ids are not eligible on card {card_no}: {bad}")
    want = set(chosen)
    return [i for i in eligible if i in want]


def check_card_keys(cards: list[dict], picks: dict) -> None:
    """Reject a card number that is not in this pending review. Omitted cards mean no ticks."""
    known = set()
    for c in cards:
        known.add(c["card_no"])
        known.add(str(c["card_no"]))
    for key in picks or {}:
        if key not in known:
            raise ValueError(f"no such card {key}")


def build_review_cards(profiles: list[dict], run_id: str, *,
                       title_prefix: str = "New verified followers") -> list[dict]:
    """Multi-select cards of at most 5. Only Follow back and Maybe are included.
    Nothing is pre-ticked. The first option is Follow all, and it names only this card's accounts.
    Title counts are the offered accounts, not the ones left off for Skip."""
    offered = [p for p in profiles if is_offered(p.get("rec"))]
    cards = vf.build_cards(offered, run_id, title_prefix=title_prefix)
    by_id = {str(p["user_id"]): p for p in offered}
    for c in cards:
        for o in c["options"]:
            p = by_id[str(o["value"])]
            o["checked"] = False
            o["rec"] = p.get("rec")
            if p.get("rechecked"):
                o["label"] = f"{RECHECKED_LABEL} · " + o["label"]
            o["detail"] = (f"{p.get('rec')}: {p.get('reason')} | {p.get('account_health')} | "
                           f"{p.get('interest')} | {p.get('recency')} | " + o["detail"])
            if not is_offered(o["rec"]):
                raise RuntimeError("review card included an account that is not Follow back or Maybe")
        n = len(c["options"])
        if n > vf.CARD_SIZE:
            raise RuntimeError(f"review card has {n} accounts; max is {vf.CARD_SIZE}")
        c["options"].insert(0, {"value": FOLLOW_ALL, "checked": False, "label": FOLLOW_ALL_LABEL,
                                "detail": f"Follows all {n} accounts on this card, one at a time"})
        c["account_ids"] = [o["value"] for o in c["options"] if o["value"] != FOLLOW_ALL]
        for o in c["options"]:
            if o.get("checked"):
                raise RuntimeError("review card option was pre-ticked")
    return cards


def assemble_review(profiles: list[dict], run_id: str, *,
                    title_prefix: str = "New verified followers") -> dict:
    """Cards plus the user-facing note. `skipped_count` is a number, not a list of accounts.
    When `profiles` is non-empty and none are offered, `note` is NO_SUITABLE_NOTE and `cards` is []."""
    cards = build_review_cards(profiles, run_id, title_prefix=title_prefix)
    offered = sum(len(c["account_ids"]) for c in cards)
    note = NO_SUITABLE_NOTE if profiles and offered == 0 else None
    return {"cards": cards, "note": note, "offered": offered,
            "skipped_count": len(profiles) - offered}
