"""Interest discovery (v3.5.32): find people worth following who match what the owner posts about.

Pure functions, no network. The bot passes in data it already read with the X tools (the owner's recent posts and
bio, X search results) or injects fetchers for tests. Nothing here follows anyone: candidates go on normal cards and
the owner picks; picked accounts then run through follow_executor.FollowSession like any follow.

    interests = learn_interests(owner_posts, owner_bio)            # [{term, weight, source}], strongest first
    card      = interests_card(interests)                           # confirm / edit topics (preference card)
    saved     = apply_interest_answer(interests, picked, typed)     # owner's answer -> final topic list
    queries   = build_queries(saved)                                # X recent-search queries (no retweets, lang)
    ranked    = rank_candidates(users, posts_by_id, saved, ...)     # existing quality + bot checks applied
    run_discovery(saved, search=..., fetch_posts=..., ...)          # same, with injectable fetchers

Ranking reuses the existing checks: verified_followers.content_value / classify_text (post quality, crypto and
engagement-farm detection) and bought_risk.assess (bought/bot risk). Low-quality or flagged accounts are dropped,
never shown; accounts the owner already follows, already blocked, or is protected from are excluded by the caller's
exclude set.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Callable, Iterable

try:
    from . import verified_followers as vf
    from . import bought_risk as br
except ImportError:  # pragma: no cover
    import verified_followers as vf
    import bought_risk as br

STOPWORDS = set("""a an and are as at be been but by can could did do does doing for from had has have having he her
here hers him his how i if in into is it its just like me more most my no not now of off on once only or other our out
over own same she should so some such than that the their them then there these they this those through to too under
until up very was we were what when where which while who whom why will with would you your yours yourself today
tomorrow yesterday really still also got get gets going gonna one two new time day week year thing things lot lots
people make made want need know think see look good great best love im ive dont cant thats its youre wanna amp via rt
""".split())
WORD_RE = re.compile(r"[#]?[A-Za-z][A-Za-z0-9_+\-]{2,}")
MAX_TERMS = 8
CARD_MAX = 6              # cards never show more than 6 options; the typed field covers the rest
MAX_QUERY_LEN = 480        # X recent search allows 512 chars; keep headroom
BIO_WEIGHT, HASHTAG_WEIGHT = 2.0, 1.5


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


def rank_candidates(users: list[dict], posts_by_id: dict[str, dict], topics: list[str], *,
                    exclude_ids: Iterable[str] = (), owner_id: str | None = None, limit: int = 5, now=None) -> list[dict]:
    """Drop excluded/owner/duplicates and anything failing the checks; best first; at most `limit` (one card)."""
    ex = {str(i) for i in exclude_ids or []} | ({str(owner_id)} if owner_id else set())
    seen, out = set(), []
    for u in users or []:
        uid = str(u.get("id") or u.get("user_id") or "")
        if not uid or uid in ex or uid in seen:
            continue
        seen.add(uid)
        s = score_candidate(u, posts_by_id.get(uid), topics, now=now)
        if s["keep"]:
            out.append({"user_id": uid, "handle": u.get("username") or u.get("handle"), "score": s["score"],
                        "why": s["reasons"][0], "matched": s["matched"], "source": "interest_discovery"})
    out.sort(key=lambda c: (-c["score"], c["handle"] or ""))
    return out[:limit]


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
                  max_profiles: int = 25, limit: int = 5, now=None) -> dict:
    """search(query) -> X search payload; fetch_posts(user_id) -> that user's recent posts payload.
    Reads only; at most max_profiles post fetches. Returns {queries, candidates, checked}."""
    queries = build_queries(topics, lang=lang)
    ex = {str(i) for i in exclude_ids or []} | ({str(owner_id)} if owner_id else set())
    users, seen = [], set()
    for q in queries:
        for u in authors_from_search(search(q)):
            if str(u.get("id")) not in seen and str(u.get("id")) not in ex:
                seen.add(str(u.get("id"))); users.append(u)
    users = users[:max_profiles]
    posts = {str(u["id"]): fetch_posts(str(u["id"])) for u in users}
    return {"queries": queries, "checked": len(users),
            "candidates": rank_candidates(users, posts, topics, exclude_ids=ex, limit=limit, now=now)}
