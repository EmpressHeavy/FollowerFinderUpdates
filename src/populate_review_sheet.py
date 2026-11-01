"""Build In review rows (columns A:R) for ALL classified accounts in a snapshot.

Standard (2026-10-15): every scan populates the review sheet with every classified
account -- BOT-LIKE, INACTIVE, REVIEW, PROTECTED (verified) and KEEP -- not only
pre-selected ones. Select (A) and Is Bot (M) are left BLANK for new rows; the owner
decides. Decision values are plain labels (dropdown): "Terminate" (was TRUE) / "Keep" (was FALSE);
B1 SELECT ALL ELIGIBLE uses the same dropdown. Blank = not reviewed. Protected/KEEP rows are locked out of Effective selection by the column L
formula (Classification must be REVIEW/BOT-LIKE and Protection must be 'none').

This module only builds values; it never writes to X and never executes removals.
Writing is done by the agent via the Google Sheets connector (write_range with
parseInput=true at the first empty row after a duplicate check on Review ID and
X user ID), then recorded in the snapshot's sheet_apply_record.json.

Tabs (standard for all users, 2026-10-16): PROTECTED = protected accounts only (verified, followed by
the owner, Always Keep, owner's own accounts); KEEPERS = keepers only (classifier 'Kept, looks real' +
owner Keep decisions); no overlap, protected wins. Removed accounts or ones that lose protection are pruned
from PROTECTED/KEEPERS (rows_to_prune) after any missing data is copied to GRAVEYARD Notes.

Lifecycle: In review holds only pending accounts. Accounts already in GRAVEYARD, PROTECTED or
KEEPERS (X user ID, then handle) are excluded via --exclude-ids/--exclude-handles; GRAVEYARD
re-follows go to Re-follow Watch (refollow_from_graveyard), never back into the queue.

Usage: python src/populate_review_sheet.py <snapshot_dir> <batch_id> <review_date> --first-row N [--start-n N]
       [--skip-ids id1,id2] [--exclude-ids id1,id2] [--exclude-handles h1,h2]
Prints JSON {"rows": [...], "first_row_hint": ...}.
"""
import json, sys
from pathlib import Path

NA = "Not available"
TERMINATE, KEEP_LABEL = "Terminate", "Keep"  # sheet decision dropdown values (A, B1, M)
STATUS = {  # internal class -> plain-language Decision status shown in the sheet
    "BOT-LIKE": "Ready to remove, waiting for your OK",
    "INACTIVE": "Waiting for your decision",
    "REVIEW": "Waiting for your decision",
    "PROTECTED (verified)": "Protected, won't be removed",
    "PROTECTED": "Protected, won't be removed",
    "KEEP": "Keeping, nothing to do",
}
# Internal classes stay in code/JSON; the sheet shows these display labels.
CLASS_DISPLAY = {"BOT-LIKE": "Likely bot", "REVIEW": "Needs your look"}
ELIGIBLE_DISPLAY = ("Needs your look", "Likely bot")  # what column L treats as removable


def protection_display(reason):
    if not reason or reason == "none":
        return "none"  # column L tests H="none"; keep this literal
    if reason.startswith("verified_type="):
        return "Protected, has a blue checkmark" if reason.endswith("blue") else f"Protected, verified ({reason.split('=',1)[1]})"
    return reason


def _short(r):
    """label_short as written by the scan; derived from the full label for older snapshots (gap fix 2026-10-16)."""
    if r.get("label_short"):
        return r["label_short"]
    lab = (r.get("label") or "").upper().strip()
    if not lab:
        return ""
    if lab.startswith("PROTECTED"):
        return "PROTECTED/KEEP"
    if lab.startswith("BOT-LIKE + INACTIVE"):
        return "BOT-LIKE + INACTIVE"
    return lab.split(" / ")[0].strip()


def classification(r):
    s = _short(r)
    if s == "PROTECTED/KEEP" or r.get("protection_gate"):
        return "PROTECTED (verified)" if r.get("protection_gate") == "verified" else "PROTECTED"
    if s.startswith("BOT-LIKE"):
        return "BOT-LIKE"
    return s or NA


def _v(x):
    return NA if x is None or x == "" else x


def _num(x):
    return NA if x is None else f"{x:,}" if isinstance(x, int) else str(x)


def reason(r, snapshot_id):
    parts = [f"snapshot={snapshot_id}", f"review#{r.get('review_number', NA)}"]
    parts.append(f"PROFILE: {_num(r.get('followers'))} followers / {_num(r.get('following'))} following "
                 f"(ratio {_v(r.get('ratio'))}); {_num(r.get('post_count'))} posts; joined {_v(r.get('joined'))}; "
                 f"last activity {_v(r.get('last_activity'))}")
    a = r.get("activity_counts")
    parts.append("ACTIVITY sample: " + (f"{a['original']} original / {a['replies']} replies / {a['quotes']} quotes / "
                 f"{a['reposts']} reposts (n={a['n']})" if a else NA))
    parts.append("Signals: " + ("; ".join(r.get("signals") or []) or NA))
    parts.append(f"evidence: <user data root>/snapshots/{snapshot_id}/candidates.json")
    return " | ".join(parts)


def effective_formula(row=None):
    """Eligible formula, POSITION-INDEPENDENT since 2026-10-16 7:57 PM PT: the sheet writer uses Google's atomic
    values.append, which decides the row, so this row's cells are addressed with INDIRECT("X"&ROW()) instead of
    a fixed row number. Evaluates the same as the old fixed form. `row` is ignored (kept for compatibility)."""
    c = lambda col: f'INDIRECT("{col}"&ROW())'
    return (f'=AND(NOT($D$1=TRUE),OR({c("A")}="Terminate",$B$1="Terminate"),OR({c("F")}="Needs your look",{c("F")}="Likely bot"),'
            f'{c("H")}="none",{c("I")}=TRUE,'
            f'NOT(REGEXMATCH(UPPER(TRIM({c("K")})),"^(TERMINATED|REMOVED|SOFT_BLOCKED|BLOCKED|GRAVEYARD)")))')


def _norm_handle(h):
    return (h or "").strip().lstrip("@").lower()


def is_resolved(r, exclude_user_ids=(), exclude_handles=()):
    """True if the account already lives in GRAVEYARD, PROTECTED or KEEPERS (X user ID first, then handle)."""
    uid = str(r.get("user_id") or "")
    if uid and uid in {str(x).lstrip("'") for x in exclude_user_ids}:
        return True
    return _norm_handle(r.get("username")) in {_norm_handle(h) for h in exclude_handles if h}


def refollow_from_graveyard(snapshot_dir, graveyard_user_ids=(), graveyard_handles=()):
    """Accounts in this snapshot that are already in GRAVEYARD: they go to Re-follow Watch, not In review."""
    cands = json.load(open(Path(snapshot_dir) / "candidates.json"))
    return [r for r in cands if is_resolved(r, graveyard_user_ids, graveyard_handles)]


QUEUE_CLASSES = ("BOT-LIKE", "INACTIVE", "REVIEW")  # only these go to In review


def is_protected(r, verified_now=None):
    """Protected = blue/verified checkmark, an account the owner follows, Always Keep, or a protection gate
    (e.g. the owner's own accounts listed in their config protected_handles). Standard for all users (2026-10-16)."""
    vt = (verified_now or {}).get(str(r.get("user_id")), r.get("verified_type") or ("verified" if r.get("verified") else None))
    return bool(classification(r).startswith("PROTECTED") or r.get("protection_gate") or r.get("i_follow") or r.get("followed_by_me")
                or r.get("always_keep") or (vt or "none") not in ("none", ""))


def route(r, hide_verified=True):
    """Which tab an account belongs in: 'PROTECTED', 'Keepers' or 'In review'.

    Standard (product owner, 2026-10-16): PROTECTED holds protected accounts only; KEEPERS holds keepers
    only (classifier 'Kept, looks real' and the owner's own Keep decisions). No overlap: an account that is
    both protected and a keeper goes to PROTECTED only. Verified accounts never enter the queue.
    """
    cls = classification(r)
    if is_protected(r):
        return "PROTECTED"
    return "In review" if _short(r).split(" ")[0] in QUEUE_CLASSES or cls in ("BOT-LIKE", "REVIEW") else "Keepers"


def keepers_source(r):
    if classification(r).startswith("PROTECTED") or (r.get("verified_type") or "none") != "none":
        return "Protected, has a blue checkmark" if r.get("verified_type") == "blue" else "Protected"
    return "Kept, looks real"


def build_keeper_rows(snapshot_dir, batch_id, kept_date, verified_now=None, exclude_user_ids=(), exclude_handles=(),
                      hide_verified=True, tab="Keepers"):
    """Rows (A:R) for tab 'Keepers' (keepers only) or 'PROTECTED' (protected only); same column layout incl. Verified?.
    verified_now: {user_id: verified_type} from a live check."""
    snap = Path(snapshot_dir); sid = snap.name; out = []
    for r in sorted(json.load(open(snap / "candidates.json")), key=lambda r: r["review_number"]):
        if route(r, hide_verified) != tab or is_resolved(r, exclude_user_ids, exclude_handles):
            continue
        cls = classification(r); h = r["username"]
        vt = (verified_now or {}).get(str(r["user_id"]), r.get("verified_type"))
        ver = NA if vt is None else ("Yes, blue checkmark" if vt == "blue" else ("NOT VERIFIED" if vt == "none" else f"Yes ({vt})"))
        prot = classification(r).startswith("PROTECTED")
        out.append([keepers_source(r), f"BT-{batch_id}-{r['review_number']:03d}", f'=HYPERLINK("https://x.com/{h}","@{h}")',
                    "'" + str(r["user_id"]), _v(r.get("name")),
                    "PROTECTION KEEP (verified)" if prot else "CLASSIFIER KEEP (looks real)",
                    protection_display(r.get("protection_reason")), cls, reason(r, sid),
                    r.get("follows_me") if isinstance(r.get("follows_me"), bool) else NA, batch_id,
                    STATUS.get(cls, "Keeping, nothing to do"), "", "", "", "'" + kept_date, "", ver])
    return out


def build_protected_rows(snapshot_dir, batch_id, kept_date, **kw):
    """PROTECTED tab rows: verified, followed-by-owner, Always Keep, protection-gate accounts."""
    return build_keeper_rows(snapshot_dir, batch_id, kept_date, tab="PROTECTED", **kw)


def rows_to_prune(rows, graveyard_user_ids=(), graveyard_handles=(), protected_now=None, tab="Keepers"):
    """Standard for all users: which existing KEEPERS / PROTECTED rows must be removed.

    rows: list of sheet rows (A:R, header excluded) as read from the tab; returns [(index, reason)].
    - Any account already in GRAVEYARD (removed/terminated) leaves KEEPERS and PROTECTED; its history
      stays in GRAVEYARD / history.jsonl. Copy any row data GRAVEYARD lacks into its Notes first.
    - PROTECTED: an account that lost protection (protected_now[user_id] is False) leaves PROTECTED
      (re-route it: classifier keep -> KEEPERS, otherwise back to the review flow).
    - KEEPERS: an account that is now protected (protected_now[user_id] is True) moves to PROTECTED (no overlap);
      a row whose Reason/Rule says UNPROTECTED is also pruned.
    Matching is by X user ID first, then handle (case-insensitive, no '@').
    """
    import re as _re
    gids = {str(x) for x in graveyard_user_ids}; ghs = {_norm_handle(h) for h in graveyard_handles}
    out = []
    for i, row in enumerate(rows):
        row = list(row) + [""] * 18
        uid = str(row[3]).lstrip("'").strip()
        m = _re.search(r'"@?([^"]+)"\)\s*$', str(row[2])); h = _norm_handle(m.group(1) if m else row[2])
        if (uid and uid in gids) or (h and h in ghs):
            out.append((i, "in GRAVEYARD (removed)")); continue
        p = (protected_now or {}).get(uid)
        if tab == "PROTECTED" and p is False:
            out.append((i, "lost protection")); continue
        if tab == "Keepers" and (p is True):
            out.append((i, "protected -> belongs in PROTECTED")); continue
        if "UNPROTECTED" in (str(row[8]) + str(row[12])).upper():
            out.append((i, "marked UNPROTECTED")); continue
    return out


def build_rows(snapshot_dir, batch_id, review_date, first_sheet_row, start_n=1, skip_user_ids=(),
               exclude_user_ids=(), exclude_handles=(), hide_verified=True):
    """exclude_*: X user IDs / handles already in GRAVEYARD or KEEPERS; those accounts are never re-queued."""
    snap = Path(snapshot_dir)
    sid = snap.name
    cands = sorted(json.load(open(snap / "candidates.json")), key=lambda r: r["review_number"])
    out, row = [], first_sheet_row
    for r in cands:
        if r["review_number"] < start_n or str(r["user_id"]) in set(map(str, skip_user_ids)):
            continue
        if is_resolved(r, exclude_user_ids, exclude_handles):
            continue
        if route(r, hide_verified) != "In review":
            continue  # protected -> PROTECTED, classifier KEEP -> KEEPERS (build_keeper_rows / build_protected_rows)
        cls = classification(r)
        prot = protection_display(r.get("protection_reason"))
        h = r["username"]
        fol = r.get("follows_me")
        out.append([
            "",  # A Select: blank, owner decides
            f"BT-{batch_id}-{r['review_number']:03d}",
            _v(r.get("name")),
            "'" + str(r["user_id"]),
            f'=HYPERLINK("https://x.com/{h}","@{h}")',
            CLASS_DISPLAY.get(cls, cls),
            reason(r, sid),
            prot,
            fol if isinstance(fol, bool) else NA,
            batch_id,
            STATUS.get(cls, "Needs owner review (not selected; not executed)"),
            effective_formula(row),
            "",  # M Is Bot: blank
            "'" + review_date,
            "", "",
            f"candidates.json ({cls}); no X action",
            "",
        ])
        row += 1
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    start = int(a[a.index("--start-n") + 1]) if "--start-n" in a else 1
    first = int(a[a.index("--first-row") + 1]) if "--first-row" in a else 4
    skip = a[a.index("--skip-ids") + 1].split(",") if "--skip-ids" in a else []
    exu = a[a.index("--exclude-ids") + 1].split(",") if "--exclude-ids" in a else []
    exh = a[a.index("--exclude-handles") + 1].split(",") if "--exclude-handles" in a else []
    print(json.dumps({"rows": build_rows(a[0], a[1], a[2], first, start, skip, exu, exh)}, ensure_ascii=False))
