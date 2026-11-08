"""Interest discovery (v3.5.32; review cards v3.5.35): find people worth following who match what the owner posts about.

No network of its own. The bot passes in data it already read with the X tools (the owner's recent posts and
bio, X search results) or injects fetchers for tests. Nothing here follows anyone: offered accounts go on the same
review cards as verified followers and the owner picks; picked accounts then run through follow_executor like any follow.

    interests = learn_interests(owner_posts, owner_bio)            # [{term, weight, source}], strongest first
    card      = interests_card(interests)                           # confirm / edit topics (preference card)
    saved     = apply_interest_answer(interests, picked, typed)     # owner's answer -> final topic list
    queries   = build_queries(saved)                                # X recent-search queries (no retweets, lang)
    ranked    = rank_candidates(users, posts_by_id, saved, ...)     # existing quality + bot checks applied
    found     = run_discovery(saved, search=..., fetch_posts=..., user_key=..., ...)
    picks     = validate_pending_selection(user_key, found["run_id"], picks)  # does not follow

Ranking reuses the existing checks: verified_followers.content_value / classify_text (post quality, crypto and
engagement-farm detection) and bought_risk.assess (bought/bot risk). Those failures are rated Skip: reviewed and,
when user_key is set, remembered for that owner so the next search does not fetch them again. They are not on the
card and not in the returned candidates. Follow back / Maybe are the only accounts offered, in cards of at most 5.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import Counter
from typing import Callable, Iterable

try:
    from . import verified_followers as vf
    from . import bought_risk as br
    from . import review_cards as rc
    from . import storage as st
except ImportError:  # pragma: no cover
    import verified_followers as vf
    import bought_risk as br
    import review_cards as rc
    import storage as st

STOPWORDS = set("""a an and are as at be been but by can could did do does doing for from had has have having he her
here hers him his how i if in into is it its just like me more most my no not now of off on once only or other our out
over own same she should so some such than that the their them then there these they this those through to too under
until up very was we were what when where which while who whom why will with would you your yours yourself today
tomorrow yesterday really still also got get gets going gonna one two new time day week year thing things lot lots
people make made want need know think see look good great best love im ive dont cant thats its youre wanna amp via rt
""".split())
WORD_RE = re.compile(r"[#]?[A-Za-z][A-Za-z0-9_+\-]{2,}")
MAX_TERMS = 8
CARD_MAX = 6              # topic preference card: never more than 6 options; the typed field covers the rest
MAX_QUERY_LEN = 480        # X recent search allows 512 chars; keep headroom
BIO_WEIGHT, HASHTAG_WEIGHT = 2.0, 1.5
# Keepers only. One topic match scores 3; two matches, or a match plus original high-value posts, reaches Follow back.
# Below that a keeper is Maybe. Failed quality / bought-bot checks stay Skip and are not shown. Nothing here follows.
FOLLOW_BACK_SCORE = 6
CARD_TITLE = "People who match your interests"


def _clean_post(text: str) -> str:
    t = re.sub(r"https?://\S+", " ", text or "")
    t = re.sub(r"@\w+", " ", t)
    return t


def learn_interests(posts: Iterable[dict | str], bio: str | None = None, *, max_terms: int = MAX_TERMS,
                    min_count: int = 2) -> list[dict]:
    """Topic terms from the owner's own posts (retweets ignored) and bio. Hashtags and bio words weigh more.
    Single words and frequent two-word phrases; a term must appear min_count times in posts (bio terms always count)."""
    words: Counter = Counter()
    sources: dict[str, set] = {}
    for p in posts or []:
        if isinstance(p, dict):
            if any(r.get("type") == "retweeted" for r in p.get("referenced_tweets") or []):
                continue
            text = p.get("text", "")
        else:
            text = str(p)
        if vf.classify_text(text) in ("low_crypto", "low_farm"):
            continue
        toks = [w.lower() for w in WORD_RE.findall(_clean_post(text))]
        toks = [w for w in toks if w.lstrip("#") not in STOPWORDS]
        for w in set(toks):
            words[w] += HASHTAG_WEIGHT if w.startswith("#") else 1.0
            sources.setdefault(w, set()).add("posts")
        plain = [w.lstrip("#") for w in toks]
        for a, b in set(zip(plain, plain[1:])):
            if a != b:
                words[f"{a} {b}"] += 1.0
                sources.setdefault(f"{a} {b}", set()).add("posts")
    for w in {w.lower() for w in WORD_RE.findall(_clean_post(bio or ""))}:
        if w.lstrip("#") not in STOPWORDS:
            words[w] += BIO_WEIGHT
            sources.setdefault(w, set()).add("bio")
    out = []
    for term, wt in words.most_common():
        if wt < min_count and "bio" not in sources[term]:
            continue
        base = term.lstrip("#")
        # fold "#ai" into "ai" and single words into a stronger phrase containing them
        if any(o["term"] == base or base in o["term"].split() for o in out):
            continue
        out.append({"term": base, "weight": round(float(wt), 2), "source": sorted(sources[term])})
        if len(out) >= max_terms:
            break
    return out


def interests_card(interests: list[dict]) -> dict:
    """Preference card: confirm or edit topics. Multi-select; typed answer adds topics ('add: rust, jazz')."""
    opts = [{"label": i["term"], "value": i["term"]} for i in interests[:CARD_MAX]]
    return {"prompt": ("These look like the topics you post about. Tap the ones I should use to find people worth "
                       "following, or type your own (comma separated)."),
            "multiSelect": True, "allowCustom": True, "options": opts}


def apply_interest_answer(interests: list[dict], picked: Iterable[str] = (), typed: str = "") -> list[str]:
    known = {i["term"] for i in interests}
    out = [p.strip().lower() for p in picked or [] if p and p.strip().lower() in known]
    t = re.sub(r"^\s*(add|also)\s*:\s*", "", typed or "", flags=re.I)
    for extra in re.split(r"[,;\n]+", t):
        e = re.sub(r"[^\w\s#+\-]", "", extra).strip().lower().lstrip("#")
        if e and e not in out:
            out.append(e)
    return out[:MAX_TERMS]


def _q(term: str) -> str:
    return f'"{term}"' if " " in term else term


def build_queries(topics: Iterable[str], *, lang: str | None = "en", per_query: int = 3) -> list[str]:
    """X recent-search queries: topics OR-ed in small groups, original posts only (no retweets, no replies),
    optional lang filter. Each query fits MAX_QUERY_LEN."""
    topics = [t for t in (x.strip() for x in topics or []) if t]
    suffix = " -is:retweet -is:reply" + (f" lang:{lang}" if lang else "")
    out = []
    for i in range(0, len(topics), max(1, per_query)):
        grp = topics[i:i + per_query]
        q = "(" + " OR ".join(_q(t) for t in grp) + ")" + suffix
        while len(q) > MAX_QUERY_LEN and len(grp) > 1:
            grp = grp[:-1]
            q = "(" + " OR ".join(_q(t) for t in grp) + ")" + suffix
        out.append(q)
    return out


def topic_match(text: str, topics: Iterable[str]) -> list[str]:
    low = (text or "").lower()
    return [t for t in topics if re.search(r"(?<!\w)" + re.escape(t.lower()) + r"(?!\w)", low)]


def score_candidate(user: dict, posts_payload: dict | None, topics: list[str], *, now=None) -> dict:
    """Uses the existing checks. Returns {keep, score, reasons, matched, quality, bought_risk}."""
    reasons, posts = [], (posts_payload or {}).get("data") or []
    cv = vf.content_value(posts_payload)
    risk = br.assess(user, now=now)
    bio = user.get("description") or ""
    matched = sorted(set(topic_match(bio, topics)) | {t for p in posts for t in topic_match(p.get("text", ""), topics)})
    if risk.get("flag"):
        return {"keep": False, "score": 0, "reasons": ["bought/bot risk: " + risk["reason"]], "matched": matched,
                "quality": cv, "bought_risk": risk}
    if cv.get("status") != "ok":
        return {"keep": False, "score": 0, "reasons": ["no recent posts to judge"], "matched": matched,
                "quality": cv, "bought_risk": risk}
    if "low_crypto" in cv["low_kinds"] or "low_farm" in cv["low_kinds"]:
        reasons.append("crypto/engagement-farm posts")
    if cv["repost_ratio"] > 0.7:
        reasons.append("mostly reposts")
    own = cv["own_high"] + cv["own_low"]
    if own and cv["own_high"] / own < 0.4:
        reasons.append("mostly low-effort posts")
    if not matched:
        reasons.append("no posts on your topics")
    keep = not reasons
    m = user.get("public_metrics") or {}
    fol, fing = m.get("followers_count") or 0, m.get("following_count") or 0
    score = (len(matched) * 3 + cv["own_high"] + cv["value_signals"] * 2
             - (2 if fing > 5 * max(fol, 1) and fing > 1000 else 0))
    return {"keep": keep, "score": score if keep else 0, "reasons": reasons or [f"posts about {', '.join(matched)}"],
            "matched": matched, "quality": cv, "bought_risk": risk}


def rate_candidate(scored: dict) -> str:
    """Follow back / Maybe / Skip from the existing keep flag and score. Skip is not offered."""
    if not scored.get("keep"):
        return "Skip"
    return "Follow back" if (scored.get("score") or 0) >= FOLLOW_BACK_SCORE else "Maybe"


def _row(user: dict, scored: dict) -> dict:
    handle = user.get("username") or user.get("handle") or ""
    return {"user_id": str(user.get("id") or user.get("user_id") or ""),
            "handle": str(handle).lstrip("@"), "score": scored.get("score") or 0,
            "why": (scored.get("reasons") or [vf.NA])[0], "reasons": list(scored.get("reasons") or []),
            "matched": list(scored.get("matched") or []), "source": "interest_discovery",
            "rec": rate_candidate(scored), "user": user}


def review_users(users: list[dict], posts_by_id: dict[str, dict], topics: list[str], *,
                 exclude_ids: Iterable[str] = (), owner_id: str | None = None, now=None) -> list[dict]:
    """Every checked account, best first, including Skip. Caller decides what is shown."""
    ex = {str(i) for i in exclude_ids or []} | ({str(owner_id)} if owner_id else set())
    seen, out = set(), []
    for u in users or []:
        uid = str(u.get("id") or u.get("user_id") or "")
        if not uid or uid in ex or uid in seen:
            continue
        seen.add(uid)
        out.append(_row(u, score_candidate(u, posts_by_id.get(uid), topics, now=now)))
    out.sort(key=lambda c: (-c["score"], c["handle"] or ""))
    return out


def _public(row: dict) -> dict:
    return {k: row[k] for k in ("user_id", "handle", "score", "why", "matched", "source", "rec")}


def rank_candidates(users: list[dict], posts_by_id: dict[str, dict], topics: list[str], *,
                    exclude_ids: Iterable[str] = (), owner_id: str | None = None, limit: int | None = 5,
                    now=None) -> list[dict]:
    """Offered accounts only (Follow back / Maybe), best first. `limit` None keeps all of them.
    Skip is not included. Default limit stays 5 for callers that want a single batch."""
    offered = [r for r in review_users(users, posts_by_id, topics, exclude_ids=exclude_ids,
                                      owner_id=owner_id, now=now) if rc.is_offered(r["rec"])]
    if limit is not None:
        offered = offered[:limit]
    return [_public(r) for r in offered]


def _card_profile(row: dict) -> dict:
    user, handle = row["user"], row["handle"]
    pm = user.get("public_metrics") or {}
    matched = row.get("matched") or []
    return {
        "user_id": row["user_id"],
        "handle": "@" + handle if handle else "@unknown",
        "display_name": user.get("name") or vf.NA,
        "url": f"https://x.com/{handle}" if handle else vf.NA,
        "about": vf._clip(user.get("description"), 160) or vf.NA,
        "posts_about": row.get("why") or vf.NA,
        "interaction": {"status": "none found", "items": []},
        "followers": pm.get("followers_count", vf.NA),
        "following": pm.get("following_count", vf.NA),
        "post_count": pm.get("tweet_count", pm.get("post_count", vf.NA)),
        "rec": row["rec"], "reason": row.get("why") or vf.NA,
        "account_health": "Account health: " + vf.NA,
        "interest": "Interest tie-in: " + (", ".join(matched) if matched else "None found"),
        "recency": vf.NA, "rechecked": False,
    }


def _disc_dir(user_key: str, root=None):
    d = st.user_data_root(user_key, root) / "interest_discovery"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _skipped_path(user_key: str, root=None):
    return _disc_dir(user_key, root) / "skipped.json"


def _pending_path(user_key: str, run_id: str, root=None):
    return _disc_dir(user_key, root) / f"pending_{run_id}.json"


def skipped_ids(user_key: str, root=None) -> set[str]:
    """Ids this owner already reviewed as Skip. Empty when there is no file. No other owner's file is read."""
    data = st.load_json(_skipped_path(user_key, root), {"ids": {}}) or {"ids": {}}
    return {str(i) for i in (data.get("ids") or {})}


def remember_skips(user_key: str, user_ids: Iterable[str], *, root=None) -> int:
    """Record Skip ids for this owner only (no handles). Existing ids keep their first_seen_at.
    Does not write the follow log and does not follow anyone. Returns how many ids were new."""
    ids = [str(i) for i in user_ids if str(i)]
    if not ids:
        return 0
    path = _skipped_path(user_key, root)
    data = st.load_json(path, {"ids": {}}) or {"ids": {}}
    stored = data.setdefault("ids", {})
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    added = 0
    for i in ids:
        if i not in stored:
            stored[i] = {"first_seen_at": now}
            added += 1
    st.save_json(path, data)
    st.apply_user_permissions(user_key, root)
    return added


def _new_run_id(now=None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _save_pending(user_key: str, run_id: str, cards: list[dict], profiles: list[dict], root=None) -> None:
    accounts = {}
    for p in profiles:
        if rc.is_offered(p.get("rec")):
            accounts[str(p["user_id"])] = {"handle": p["handle"], "rec": p["rec"]}
    state = {"run_id": run_id, "owner_key": user_key,
             "cards": [{"card_no": c["card_no"], "ids": [str(i) for i in c["account_ids"]]} for c in cards],
             "accounts": accounts}
    st.save_json(_pending_path(user_key, run_id, root), state)
    st.apply_user_permissions(user_key, root)


def authors_from_search(payload: dict | None) -> list[dict]:
    """Users from an X search_posts payload (includes.users), in first-seen order."""
    users = ((payload or {}).get("includes") or {}).get("users") or []
    seen, out = set(), []
    for u in users:
        if u.get("id") not in seen:
            seen.add(u.get("id")); out.append(u)
    return out


def run_discovery(topics: list[str], *, search: Callable[[str], dict], fetch_posts: Callable[[str], dict],
                  exclude_ids: Iterable[str] = (), owner_id: str | None = None, lang: str | None = "en",
                  max_profiles: int = 25, limit: int | None = None, now=None,
                  user_key: str | None = None, root=None, run_id: str | None = None) -> dict:
    """search(query) -> X search payload; fetch_posts(user_id) -> that user's recent posts payload.
    Reads only; at most max_profiles post fetches. Does not follow anyone.

    Cards list every Follow back and Maybe account (pass limit to cap how many are offered), in batches of 5,
    nothing pre-ticked. Skip is not in `candidates` or `cards`. With user_key, those Skip ids are remembered
    for that owner and skipped on the next run before any post fetch. `note` is set only when this round
    reviewed someone (or only found accounts already remembered as Skip) and nobody is offered.
    `skipped_count` is a number. Returns {run_id, queries, candidates, checked, cards, note, offered, skipped_count}.
    """
    queries = build_queries(topics, lang=lang)
    remembered = skipped_ids(user_key, root) if user_key else set()
    ex = {str(i) for i in exclude_ids or []} | ({str(owner_id)} if owner_id else set()) | remembered
    found, seen_ids = [], set()
    for q in queries:
        for u in authors_from_search(search(q)):
            uid = str(u.get("id") or "")
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                found.append(u)
    fresh = [u for u in found if str(u.get("id")) not in ex][:max_profiles]
    posts = {str(u["id"]): fetch_posts(str(u["id"])) for u in fresh}
    reviewed = review_users(fresh, posts, topics, now=now)
    skips = [r for r in reviewed if not rc.is_offered(r["rec"])]
    offered_rows = [r for r in reviewed if rc.is_offered(r["rec"])]
    if limit is not None:
        offered_rows = offered_rows[:limit]
    if user_key:
        remember_skips(user_key, [r["user_id"] for r in skips], root=root)
    profiles = [_card_profile(r) for r in offered_rows + skips]
    run_id = run_id or _new_run_id(now)
    assembled = rc.assemble_review(profiles, run_id, title_prefix=CARD_TITLE)
    note = assembled["note"]
    if note is None and not assembled["offered"] and found and not fresh:
        note = rc.NO_SUITABLE_NOTE
    if user_key:
        _save_pending(user_key, run_id, assembled["cards"], profiles, root)
    return {"run_id": run_id, "queries": queries, "checked": len(fresh),
            "candidates": [_public(r) for r in offered_rows], "cards": assembled["cards"], "note": note,
            "offered": assembled["offered"], "skipped_count": len(skips)}


def validate_pending_selection(user_key: str, run_id: str, picks: dict, *, root=None) -> dict:
    """Check picks against this owner's pending interest-discovery cards. Does not follow.

    Same rules as the verified review: Follow all and ticks are only the Follow back / Maybe accounts on that
    card; a Skip still stored on a legacy pending card is not selected; an id or card number that is not pending
    raises ValueError and writes nothing. Empty ticks select nobody. Appends one SELECTION per card to the
    owner's follow log (append-only) so a later follow of those ids is the same authorized path as a verified card.
    Returns {picked: [{user_id, handle}, ...]} for the accounts the owner actually selected.
    """
    state = st.load_json(_pending_path(user_key, run_id, root), None)
    if not state or state.get("owner_key") != user_key:
        raise ValueError(f"no pending interest review {run_id} for this owner")
    picks = picks or {}
    accounts = state.get("accounts") or {}
    rec_by_id = {str(i): a.get("rec") for i, a in accounts.items()}
    rc.check_card_keys(state.get("cards") or [], picks)
    resolved = []
    for c in state.get("cards") or []:
        raw = picks.get(c["card_no"], picks.get(str(c["card_no"]), []))
        resolved.append((c, rc.resolve_picks(c["card_no"], c["ids"], raw, rec_by_id)))
    profs = {str(i): {"handle": a.get("handle")} for i, a in accounts.items()}
    picked = []
    for c, ids in resolved:
        offered_ids = rc.selectable_ids(c["ids"], rec_by_id)
        vf.record_selection(user_key, run_id, c["card_no"], offered_ids, ids, profs, root=root)
        for i in ids:
            picked.append({"user_id": i, "handle": accounts[i]["handle"]})
    return {"picked": picked}
