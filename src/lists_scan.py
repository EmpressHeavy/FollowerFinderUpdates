"""Lists scan (v3). Every X list the owner is a member of, saved as a frozen per-user snapshot.

Two browser steps on the owner's signed-in X website (the bot's own browser):
1. Memberships page: https://x.com/<handle>/lists/memberships, scrolled to the end. It shows list names and
   usually the owner, but no descriptions or list URLs, and some rows show "N followers including @someone"
   instead of the owner. Raw rows -> data/users/<user_key>/lists_raw/*.json.
2. Deep inspection of EVERY list (inspection_plan): open each list page, even plain names, to read the list
   URL/ID, name, description, owner display name + @handle, member/follower counts and, if useful, a sample of
   members. Opening the page is how a missing owner gets filled. Results -> lists_inspect/*.json.
The X connector's list-memberships read may supplement both. Missing fields stay "Not available".

Tone (calibrated, 2026-10-22): NEGATIVE only for really negative content (harassment, hate, spam/bot rings,
scams, blocklists, mocking/targeting). Playful or ambiguous names are OK. Unclear -> REVIEW (watched), never
flagged by name alone. Lists whose owner is a mutual are never auto-flagged: they're marked for watch.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any

try:
    from . import storage as st
except ImportError:  # pragma: no cover
    import storage as st

NA = "Not available"
NEGATIVE, REVIEW, OK = "NEGATIVE", "REVIEW", "OK"
NEUTRAL = OK  # backward-compatible name
# Really negative content (phrases, not playful single words).
STRONG = ("harass", "harassment", "harassers", "hate", "haters", "hate list", "spam", "spammers", "spam bots",
          "spambots", "bot ring", "bot farm", "bot network", "scam", "scams", "scammer", "scammers", "blocklist",
          "block list", "to block", "block these", "mass block", "mass report", "report these", "report them",
          "dogpile", "targets", "to target", "targeting", "mock", "mocking", "idiots", "morons", "losers",
          "clowns", "lunatics", "grifters", "nazis", "bigots", "doxx", "stalk", "trolls to", "enemies")
# Could go either way: REVIEW (watched), never NEGATIVE on their own.
WEAK = ("bot", "bots", "fake", "fakes", "troll", "trolls", "sus", "suspicious", "drama", "beef", "exposed",
        "callout", "receipts", "trash", "garbage", "cringe", "creep", "creeps", "liars", "frauds", "shills")
# Context that turns a match into REVIEW (e.g. "anti-spam", "bot builders").
SOFTEN = ("favorite", "favourite", "best", "love", "great", "helpful", "builders", "developers", "devs",
          "research", "against", "fighting", "anti-spam", "anti spam", "anti-scam", "protect", "safety",
          "awareness", "report scams to", "fans", "funny", "lol", "joke")


def _has(text: str, words) -> list[str]:
    return [w for w in words if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text)]


def mutual(rec: dict) -> Any:
    """Whether the user and the owner follow each other: True / False / NA."""
    a, b = rec.get("owner_i_follow"), rec.get("owner_follows_me")
    if a is True and b is True:
        return True
    if a is False or b is False:
        return False
    return NA


PRODUCT_NAMES = ("grok bots", "grok bot", "grokbots", "grokbot")   # product names, not a bot signal


def classify_list(rec: dict) -> dict:
    text = " ".join(str(rec.get(k) or "") for k in ("name", "description") if rec.get(k) != NA).lower()
    for pn in PRODUCT_NAMES:
        text = text.replace(pn, " ")
    strong, weak, soft = _has(text, STRONG), _has(text, WEAK), _has(text, SOFTEN)
    if strong and not soft:
        tone, why = NEGATIVE, "negative content: " + ", ".join(strong)
    elif strong or weak:
        tone = REVIEW
        why = "unclear: " + ", ".join(strong + weak) + (f" (with {', '.join(soft)})" if soft else "")
    else:
        tone, why = OK, "nothing negative found"
    watch = tone == REVIEW
    if mutual(rec) is True:
        watch = True
        if tone == NEGATIVE:
            tone, why = REVIEW, why + "; owner is a mutual, so watched, not flagged"
    return {"tone": tone, "tone_reason": why, "watch": watch, "flagged": tone == NEGATIVE}


def _v(rec, *keys):
    for k in keys:
        v = rec.get(k)
        if v not in (None, "", NA):
            return v
    return NA


_URL_ID = re.compile(r"/i/lists/(\d+)")


def normalize(rec: dict) -> dict:
    url = _v(rec, "url", "list_url")
    lid = str(_v(rec, "list_id", "id"))
    if lid == NA and url != NA and _URL_ID.search(str(url)):
        lid = _URL_ID.search(str(url)).group(1)
    if url == NA and lid != NA:
        url = f"https://x.com/i/lists/{lid}"
    oh = _v(rec, "owner_handle", "owner_username")
    out = {"list_id": lid, "url": url, "name": _v(rec, "name"), "description": _v(rec, "description"),
           "owner_name": _v(rec, "owner_name", "owner_display_name"),
           "owner_handle": "@" + str(oh).lstrip("@") if oh != NA else NA,
           "owner_id": str(_v(rec, "owner_id", "owner_user_id")),
           "member_count": _v(rec, "member_count", "members"),
           "follower_count": _v(rec, "follower_count", "subscriber_count", "followers"),
           "followers_including": _v(rec, "followers_including"),   # memberships-row text when owner missing
           "sample_members": rec.get("sample_members") or [],
           "owner_i_follow": rec.get("owner_i_follow"), "owner_follows_me": rec.get("owner_follows_me"),
           "row_index": rec.get("row_index"), "deep_inspected": bool(rec.get("deep_inspected")),
           "source": rec.get("source") or "browser"}
    out["owner_url"] = f"https://x.com/{out['owner_handle'].lstrip('@')}" if out["owner_handle"] != NA else NA
    out["mutual"] = mutual(out)
    out.update(classify_list(out))
    return out


def _key(r: dict) -> str:
    if r.get("list_id") not in (None, NA):
        return r["list_id"]
    if r.get("row_index") is not None:
        return f"row{r['row_index']}"
    return f"{r.get('owner_handle')}/{r.get('name')}".lower()


def inspection_plan(rows: list[dict], user_key: str | None = None, root=None) -> list[dict]:
    """EVERY list gets opened (not only flagged or nameless ones), except Protected Lists (owner you follow),
    which are not re-inspected; only the follow check runs for them (recheck_protected)."""
    prot = load_protected(user_key, root) if user_key else {}
    plan = []
    for i, r in enumerate(rows):
        n = normalize(dict(r, row_index=r.get("row_index", i)))
        if _key(n) in prot:
            continue
        plan.append({"row_index": n["row_index"], "name": n["name"],
                     "open": n["url"] if n["url"] != NA else f"click row {n['row_index'] + 1} on the memberships page",
                     "read": ["list URL", "name", "description", "owner display name + @handle",
                              "member and follower counts", "sample of members (optional)",
                              "whether you follow the owner and the owner follows you"],
                     "owner_missing": n["owner_handle"] == NA})
    return plan


def _merge(a: dict, b: dict) -> dict:
    """Fill gaps; a deep-inspected value wins over a memberships-row value."""
    out = dict(a)
    for f, v in b.items():
        if v in (None, NA, "", []):
            continue
        if out.get(f) in (None, NA, "", []) or b.get("deep_inspected"):
            out[f] = v
    out["deep_inspected"] = bool(a.get("deep_inspected") or b.get("deep_inspected"))
    return out


def _read_dir(d):
    out = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        data = json.loads(p.read_text(encoding="utf-8"))
        out += data.get("records", data) if isinstance(data, dict) else data
    return out


SCOPE_NOT_FOLLOWED = "not_followed_back"
SCOPE_ALL = "all"
SCOPE_LABELS = {SCOPE_NOT_FOLLOWED: "Only lists from people I don't follow back", SCOPE_ALL: "Show all lists"}


def scope_card() -> dict:
    """Pre-run card, shown before each lists-checking run. The choice is frozen into that run's snapshot."""
    return {"multiSelect": False, "allowCustom": False, "prompt": "Which lists should this check cover?",
            "options": [{"label": v, "value": k} for k, v in SCOPE_LABELS.items()]}


def parse_scope(answer) -> str:
    a = str(answer or "").strip()
    for k, v in SCOPE_LABELS.items():
        if a in (k, v):
            return k
    raise ValueError("pick one of the two scope options before the lists check runs")


def _protected_path(user_key, root):
    return st.user_data_root(user_key, root) / "lists_protected.json"


def load_protected(user_key: str, root=None) -> dict:
    return st.load_json(_protected_path(user_key, root), {}) or {}


def recheck_protected(user_key: str, follows: dict, root=None, when: str | None = None) -> list[dict]:
    """The only ongoing check for Protected Lists: does the user still follow the owner?
    follows: {"@owner": True/False/None}. False -> leaves Protected Lists (logged with date), back to review."""
    try:
        from . import removal_runner as rr
    except ImportError:  # pragma: no cover
        import removal_runner as rr
    prot = load_protected(user_key, root)
    when = when or dt.datetime.now().astimezone().isoformat()
    released = []
    for k, r in list(prot.items()):
        if follows.get(r["owner_handle"]) is False:
            prot.pop(k)
            e = {"status": "PROTECTED_LIST_RELEASED", "at": when, "list_key": k, "name": r["name"],
                 "url": r["url"], "owner_handle": r["owner_handle"], "protected_since": r["since"],
                 "reason": "you no longer follow the owner; back to normal review", "action": "NONE"}
            rr._append(user_key, root, e)
            released.append(e)
        elif follows.get(r["owner_handle"]) is True:
            r["last_checked"] = when
    st.save_json(_protected_path(user_key, root), prot)
    return released


def _kept_path(user_key, root):
    return st.user_data_root(user_key, root) / "lists_kept.json"


def load_kept(user_key: str, root=None) -> dict:
    return st.load_json(_kept_path(user_key, root), {}) or {}


def keep_list(user_key: str, snapshot_id: str, item: dict, root=None) -> None:
    """Kept Lists: not reviewed again on future runs unless something material changes (owner or name)."""
    k = _key(item)
    kept = load_kept(user_key, root)
    kept[k] = {"name": item["name"], "url": item["url"], "owner_handle": item["owner_handle"],
               "kept_at": dt.datetime.now().astimezone().isoformat(), "snapshot_id": snapshot_id}
    st.save_json(_kept_path(user_key, root), kept)


def build_snapshot(user_key: str, root=None, *, records: list[dict] | None = None,
                   inspections: list[dict] | None = None, when: str | None = None,
                   scope: str = SCOPE_ALL) -> dict:
    """Memberships rows + deep inspections (matched by row_index, list ID or URL) -> frozen snapshot."""
    base = st.user_data_root(user_key, root)
    rows = list(records) if records is not None else _read_dir(base / "lists_raw")
    insp = list(inspections) if inspections is not None else _read_dir(base / "lists_inspect")
    merged: dict[str, dict] = {}
    for i, rec in enumerate(rows):
        rec = dict(rec, row_index=rec.get("row_index", i))
        k = _key(normalize(rec))
        merged[k] = _merge(merged[k], rec) if k in merged else dict(rec)
    by_row = {m.get("row_index"): k for k, m in merged.items()}
    for ins in insp:
        ins = dict(ins, deep_inspected=True)
        n = normalize(ins)
        k = by_row.get(ins.get("row_index")) if ins.get("row_index") is not None else None
        k = k or next((kk for kk, m in merged.items() if normalize(m)["list_id"] == n["list_id"] != NA), None)
        k = k or _key(n)
        merged[k] = _merge(merged.get(k) or {}, ins)
    if scope not in SCOPE_LABELS:
        raise ValueError("unknown lists scope")
    prot = load_protected(user_key, root)
    when = when or dt.datetime.now().astimezone().isoformat()
    lists = [normalize(m) for m in merged.values()]
    kept, changed = load_kept(user_key, root), []
    for r in lists:
        k = _key(r)
        if r["owner_i_follow"] is True and k not in prot:
            prot[k] = {"name": r["name"], "url": r["url"], "owner_handle": r["owner_handle"], "since": when,
                       "last_checked": when}
        r["protected"] = k in prot
        kp = kept.get(k)
        if kp and (kp["owner_handle"] != r["owner_handle"] or kp["name"] != r["name"]):
            changed.append({"key": k, "was": {"name": kp["name"], "owner_handle": kp["owner_handle"]},
                            "now": {"name": r["name"], "owner_handle": r["owner_handle"]}})
            kept.pop(k)
            kp = None
        r["kept"] = bool(kp)
        r["in_scope"] = scope == SCOPE_ALL or not r["protected"]
    st.save_json(_protected_path(user_key, root), prot)
    st.save_json(_kept_path(user_key, root), kept)
    if changed:
        try:
            from . import removal_runner as rr
        except ImportError:  # pragma: no cover
            import removal_runner as rr
        for c in changed:
            rr._append(user_key, root, dict(c, status="KEPT_LIST_RECHECK", at=when,
                                            reason="owner or name changed; back to review", action="NONE"))
    lists.sort(key=lambda r: ({NEGATIVE: 0, REVIEW: 1}.get(r["tone"], 2), str(r["name"]).lower()))
    sid = "L" + re.sub(r"[^0-9]", "", when)[:14]
    d = base / "lists" / sid
    d.mkdir(parents=True, exist_ok=True)
    body = {"snapshot_id": sid, "scanned_at": when, "scope": scope, "scope_label": SCOPE_LABELS[scope],
            "lists": lists}
    st.save_json(d / "lists.json", body)
    st.save_json(d / "frozen.json", {"sha256": snapshot_hash(body), "frozen_at": when})
    st.apply_user_permissions(user_key, root)
    return {"snapshot_id": sid, "scope": scope, "lists": lists, "kept_rechecks": changed, "rechecks": update_watch(user_key, sid, lists, root)}


def snapshot_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def load(user_key: str, snapshot_id: str, root=None) -> dict:
    return st.load_json(st.user_data_root(user_key, root) / "lists" / snapshot_id / "lists.json", {}) or {}


def is_frozen_intact(user_key: str, snapshot_id: str, root=None) -> bool:
    fz = st.load_json(st.user_data_root(user_key, root) / "lists" / snapshot_id / "frozen.json", {}) or {}
    body = load(user_key, snapshot_id, root)
    return bool(body) and fz.get("sha256") == snapshot_hash(body)


def update_watch(user_key: str, snapshot_id: str, lists: list[dict], root=None) -> list[dict]:
    """Keep the watch state across scans: mutual-owner and ambiguous lists are tracked and rechecked later.
    Returns rechecks: previously watched lists seen again (with their current tone) or gone."""
    p = st.user_data_root(user_key, root) / "lists_watch.json"
    watch = st.load_json(p, {}) or {}
    now = {_key(r): r for r in lists}
    rechecks = []
    for k, w in list(watch.items()):
        if k in now:
            r = now[k]
            rechecks.append({"key": k, "name": r["name"], "since": w["since"], "was": w["tone"], "now": r["tone"],
                             "still_on": True})
            w.update(last_seen=snapshot_id, tone=r["tone"])
        else:
            rechecks.append({"key": k, "name": w["name"], "since": w["since"], "was": w["tone"], "now": NA,
                             "still_on": False})
            watch.pop(k)
    for k, r in now.items():
        if r["watch"] and k not in watch:
            watch[k] = {"name": r["name"], "since": snapshot_id, "last_seen": snapshot_id, "tone": r["tone"],
                        "why": "owner is a mutual" if r["mutual"] is True else r["tone_reason"]}
    st.save_json(p, watch)
    return rechecks


def report(lists: list[dict]) -> str:
    """Full list of lists: negative ones separately, then needs-a-look / watched, then the rest."""
    groups = [(NEGATIVE, "Negative lists"), (REVIEW, "Needs your look / watched"), (OK, "Other lists")]
    lines = [f"You're on {len(lists)} list{'s' if len(lists) != 1 else ''}."]
    for tone, title in groups:
        g = [r for r in lists if r["tone"] == tone]
        if not g:
            continue
        lines.append(f"\n{title} ({len(g)}):")
        for r in g:
            lines.append(f"- {r['name']} — {r['url']} — owner {r['owner_handle']} — {r['member_count']} members"
                         + (" — watch" if r["watch"] else "")
                         + (f" — {r['tone_reason']}" if tone != OK else ""))
    return "\n".join(lines)
