"""Verified Follower Review: deep profile dive (v3.5.16).

For each candidate verified follower, read their posts AND replies as far back as the X API allows, capped at
DEEP_DIVE_CONFIG["window_days"] (90), then score Follow back / Maybe / Skip from documented weights.

Reads (read-only, built-in X tools; see the x-api-mcp-guide skill):
  1. get_users_posts(id, max_results=25, start_time=now-90d, pagination_token=...)  -> timeline incl. replies/reposts
  2. search_posts_all(query="from:<handle>", start_time=now-90d, max_results=25, next_token)  -> archive search,
     may reach further back than the timeline. Results from both are merged and de-duplicated by post id.
  Every call is counted against a per-candidate budget (max pages per source + max calls total) and the global
  pacing of 30 calls/min, 1000/day is left to the caller (read_pacing). Nothing here posts, follows or DMs.

Coverage: we record the oldest post date actually reached, the number of items read, and WHY reading stopped
(window reached / API had no more pages / budget ran out / account newer than the window). If fewer than 90 days
are covered the output says so ("covered 34 days / 120 posts") and the activity/content weights are scaled by a
confidence factor. Missing data is never invented: no posts read -> "Not available" and at most Maybe.

Pinned post: the user's pinned_tweet_id (user.fields=pinned_tweet_id) is dropped from every activity, recency and
content calculation, because an old pinned post can make an inactive account look active.

Recency: last_post_at = created_at of the newest NON-pinned post/reply/repost/quote in the window. Scored by the
thresholds in DEEP_DIVE_CONFIG["recency"]; beyond the "negative" band (or nothing found in the window) Follow back
is impossible: the recommendation is capped at Maybe (or Skip) no matter how good the content is.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from typing import Any, Callable

try:
    from . import verified_followers as vf
    from . import classify as cl
except ImportError:  # top-level import
    import verified_followers as vf
    import classify as cl

NA = "Not available"

# ---------------------------------------------------------------------------------------------------------------
# ONE config dict: limits, thresholds and every scoring weight. Points are added up; see score_candidate().
# ---------------------------------------------------------------------------------------------------------------
DEEP_DIVE_CONFIG: dict[str, Any] = {
    "window_days": 90,                 # never read further back than this
    "page_size": 25,                   # X tools return max 25 results per call
    "max_pages_timeline": 8,           # get_users_posts pages per candidate (<= 200 items)
    "max_pages_search": 8,             # search_posts_all pages per candidate
    "max_calls_per_candidate": 16,     # hard budget across both sources
    "rate_limit_per_min": 30,          # documented X limits (pacing is enforced by the caller)
    "rate_limit_per_day": 1000,
    "min_posts_for_full_confidence": 150,  # a sample this big is enough even if it spans < 90 days
    "recency": {                       # days since last NON-pinned post -> points; beyond 'negative_max' = no Follow back
        "strong_max": 7,   "strong_pts": 4.0,     # <= 7 days
        "mild_max": 30,    "mild_pts": 1.5,       # 8-30 days
        "negative_max": 90, "negative_pts": -3.0,  # 31-90 days
        "stale_pts": -6.0,                         # > 90 days or nothing in window -> cap at Maybe/Skip
    },
    "repost": {
        "high_ratio": 0.8,  "high_pts": -5.0,     # > 80% reposts: strong negative
        "mid_ratio": 0.5,   "mid_pts": -2.0,      # > 50% reposts
        # repost-only (zero originals/replies/quotes) -> Skip unless substantive interaction with the owner
    },
    "weights": {                       # per-signal points (capped where noted)
        "grok_bot_building": 2.0,      # per item, cap 'signal_cap'
        "grok_imagine": 1.5,           # per item, cap 'signal_cap'
        "substantive_reply": 0.5,      # per substantive reply/quote, cap 'signal_cap'
        "back_and_forth": 1.0,         # >= 3 replies in the same conversation with real content (once per convo, cap)
        "signal_cap": 4.0,             # max points from any single positive content signal
        "owner_interaction": 3.0,      # any reply/mention/quote/repost of the owner in the window
        "owner_substantive": 2.0,      # extra if at least one owner interaction is substantive
        "low_effort_ratio_pts": -3.0,  # times share of own items that are GM/GN, emoji-only, bait, crypto, dupes
        "crypto_shill": -1.0,          # per item, cap -4
        "duplicates": -1.0,            # per duplicated text beyond the first, cap -3
        "bot_number_signs": -1.0,      # per Follower Finder person_check bot-number sign
        "bio_hint": -0.5,              # Follower Finder handle/bio screen hint (weak)
    },
    # v3.5.18 targeted low-originality penalty, gated by the owner's allow_reply_heavy preference (default OFF).
    # Pattern: almost no originals; nearly everything is a reply/quote to a BIG account (the referenced post has
    # >= big_ref_likes likes, a proxy because user.fields of the referenced author aren't fetched), the takes are
    # short (<= short_max_chars after mentions/links), and they collect many likes (median >= min_median_likes).
    # OFF -> add 'pts'. ON -> no penalty. Nothing else in scoring depends on this block.
    "reply_heavy": {
        "min_items": 10,               # need at least this many own (non-repost) items to judge
        "max_original_share": 0.1,     # originals / own items
        "min_reply_quote_share": 0.8,  # replies + quotes / own items
        "big_ref_likes": 1000,         # referenced post likes that count as a 'big account'
        "min_big_share": 0.5,          # share of replies/quotes aimed at big accounts
        "short_max_chars": 100,        # a 'short take'
        "min_short_share": 0.6,        # share of replies/quotes that are short
        "min_median_likes": 20,        # median likes on those replies/quotes
        "pts": -3.0,                   # penalty when the owner has allow_reply_heavy OFF
    },
    "hard_skip": ["impersonation"],    # Follower Finder impersonation signal -> Skip regardless of score
    "thresholds": {"follow_back": 6.0, "maybe": 1.0},  # score >= follow_back -> Follow back; >= maybe -> Maybe
}

GROK_BOT_RE = re.compile(r"(grok\s*bot|\bbots?\b|\bagents?\b|agentic|workflows?|automation|mcp\b|"
                         r"built (a|my|an)|i built|shipped|claude code|cursor)", re.I)
IMAGINE_RE = re.compile(r"(grok\s*imagine|#grokimagine|made (this|it) with (grok|imagine)|imagine\s*(video|image))", re.I)
EMOJI_ONLY_RE = re.compile(r"^[\W_\s]*$")


# ------------------------------------------------------------------ reading -------------------------------------
def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


POST_FIELDS = "created_at,public_metrics,author_id,lang,conversation_id,referenced_tweets,in_reply_to_user_id,entities,attachments"
EXPANSIONS = "author_id,referenced_tweets.id,referenced_tweets.id.author_id"


def fetch_activity(user: dict, call_tool: Callable[[str, dict], dict], *, now: dt.datetime | None = None,
                   cfg: dict | None = None) -> dict:
    """Page both sources up to the window or the budget. call_tool(name, args) -> raw tool JSON.
    Returns {"posts": [...deduped...], "includes_tweets": {...}, "coverage": {...}}."""
    cfg = cfg or DEEP_DIVE_CONFIG
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=cfg["window_days"])
    posts: dict[str, dict] = {}
    inc: dict[str, dict] = {}
    calls, stops, errors = 0, {}, []

    def run(source: str, tool: str, base: dict, token_arg: str, max_pages: int):
        nonlocal calls
        token, pages = None, 0
        while True:
            if pages >= max_pages:
                stops[source] = "page budget reached"; return
            if calls >= cfg["max_calls_per_candidate"]:
                stops[source] = "call budget reached"; return
            args = dict(base, **({token_arg: token} if token else {}))
            try:
                r = call_tool(tool, args) or {}
            except Exception as e:  # noqa: BLE001 - surfaced in coverage, never guessed around
                errors.append(f"{tool}: {e}"); stops[source] = "API error"; return
            calls += 1; pages += 1
            for t in (r.get("includes") or {}).get("tweets", []) or []:
                inc[str(t["id"])] = t
            for p in r.get("data") or []:
                c = _parse(p.get("created_at"))
                if c and c >= start:
                    posts.setdefault(str(p["id"]), p)
            token = (r.get("meta") or {}).get("next_token")
            oldest_page = min((_parse(p.get("created_at")) for p in r.get("data") or [] if p.get("created_at")),
                              default=None)
            if not token:
                stops[source] = "API returned no more pages"; return
            if oldest_page and oldest_page < start:
                stops[source] = "90-day window reached"; return

    run("timeline", "get_users_posts",
        {"id": str(user["id"]), "max_results": cfg["page_size"], "start_time": _iso(start),
         "post.fields": POST_FIELDS, "expansions": EXPANSIONS}, "pagination_token", cfg["max_pages_timeline"])
    run("search", "search_posts_all",
        {"query": f"from:{user['username']}", "start_time": _iso(start), "max_results": cfg["page_size"],
         "post.fields": POST_FIELDS, "expansions": EXPANSIONS}, "next_token", cfg["max_pages_search"])
    return {"posts": list(posts.values()), "includes_tweets": inc,
            "fetch": {"calls": calls, "stops": stops, "errors": errors}}


# ------------------------------------------------------------------ classification ------------------------------
def item_kind(p: dict) -> str:
    """original | reply | repost | quote"""
    types = {r.get("type") for r in p.get("referenced_tweets") or []}
    if "retweeted" in types or (p.get("text") or "").startswith("RT @"):
        return "repost"
    if "quoted" in types:
        return "quote"
    if "replied_to" in types or p.get("in_reply_to_user_id"):
        return "reply"
    return "original"


def _norm_text(t: str) -> str:
    t = vf.LINK_RE.sub("", vf._strip_mentions(t or "")).lower()
    return " ".join(re.findall(r"\w+", t))


def content_signals(p: dict) -> dict:
    t = p.get("text") or ""
    bare = vf.LINK_RE.sub("", vf._strip_mentions(t)).strip()
    q = vf.classify_text(t)
    has_media = bool((p.get("attachments") or {}).get("media_keys"))
    return {
        "grok_bot": bool(GROK_BOT_RE.search(t)),
        "imagine": bool(IMAGINE_RE.search(t)) or (has_media and bool(re.search(r"\bimagine\b", t, re.I))),
        "emoji_only": bool(EMOJI_ONLY_RE.match(bare)),
        "quality": q,  # high | low_gm | low_farm | low_crypto | low_effort
        "low": q != "high" or bool(EMOJI_ONLY_RE.match(bare)),
    }


# ------------------------------------------------------------------ analysis ------------------------------------
def analyze(user: dict, fetched: dict, owner_id: str, *, now: dt.datetime | None = None,
            cfg: dict | None = None) -> dict:
    cfg = cfg or DEEP_DIVE_CONFIG
    now = now or dt.datetime.now(dt.timezone.utc)
    W = cfg["window_days"]
    pinned = str(user.get("pinned_tweet_id") or "")
    inc = fetched.get("includes_tweets") or {}
    items = [p for p in fetched.get("posts") or [] if str(p.get("id")) != pinned]
    items.sort(key=lambda p: p.get("created_at") or "", reverse=True)

    kinds = Counter()
    sig = Counter()
    owner_items, convo = [], Counter()
    seen_txt = Counter()
    for p in items:
        k = item_kind(p); p["_kind"] = k; kinds[k] += 1
        if k == "repost":
            ref = next((r for r in p.get("referenced_tweets") or [] if r.get("type") == "retweeted"), None)
            if ref and str((inc.get(str(ref.get("id"))) or {}).get("author_id")) == str(owner_id):
                owner_items.append({"kind": "repost", "substantive": False, "at": p.get("created_at")})
            continue
        s = content_signals(p); p["_sig"] = s
        nt = _norm_text(p.get("text"))
        if nt:
            seen_txt[nt] += 1
        sig["grok_bot"] += s["grok_bot"]; sig["imagine"] += s["imagine"]
        sig["low"] += s["low"]; sig["crypto"] += s["quality"] == "low_crypto"
        if k in ("reply", "quote") and not s["low"]:
            sig["substantive"] += 1
            convo[p.get("conversation_id")] += 1
        to_owner = str(p.get("in_reply_to_user_id")) == str(owner_id) or any(
            str((inc.get(str(r.get("id"))) or {}).get("author_id")) == str(owner_id)
            for r in p.get("referenced_tweets") or []) or any(
            str(m.get("id")) == str(owner_id) for m in ((p.get("entities") or {}).get("mentions") or []))
        if to_owner:
            owner_items.append({"kind": k, "substantive": not s["low"], "at": p.get("created_at"),
                                "text": vf._clip(p.get("text"), 120)})
    rh = reply_heavy_pattern(items, inc, cfg)
    dups = sum(c - 1 for c in seen_txt.values() if c > 1)
    sig["back_and_forth"] = sum(1 for c in convo.values() if c >= 3)

    # coverage
    oldest = _parse(items[-1]["created_at"]) if items else None
    created = _parse(user.get("created_at"))
    covered_days = min(W, (now - oldest).days) if oldest else 0
    stops = (fetched.get("fetch") or {}).get("stops", {})
    reasons = sorted(set(stops.values()))
    account_newer = bool(created and (now - created).days < W)
    if account_newer:
        reasons.append(f"account is {(now - created).days} days old")
    # complete = every source stopped because it hit the window or ran out of data (not budget/error)
    complete = bool(stops) and all(v in ("90-day window reached", "API returned no more pages") for v in stops.values())
    age = (now - created).days if created else W
    eff = max(1, min(W, age))                       # the most days that could exist for this account
    days_ratio = 1.0 if complete else min(1.0, covered_days / eff)
    posts_ratio = min(1.0, len(items) / cfg["min_posts_for_full_confidence"])
    # enough days OR a big enough sample; a short window with few posts gets low confidence
    conf = round(max(days_ratio, posts_ratio), 2) if items else 0.0
    last = _parse(items[0]["created_at"]) if items else None
    return {
        "user_id": str(user["id"]), "handle": "@" + user["username"], "pinned_excluded": pinned or None,
        "n_items": len(items), "kinds": dict(kinds),
        "repost_ratio": round(kinds["repost"] / len(items), 2) if items else None,
        "signals": {**dict(sig), "duplicates": dups},
        "owner_interactions": owner_items,
        "reply_heavy": rh,
        "last_post_at": items[0]["created_at"] if items else None,
        "days_since_last_post": (now - last).days if last else None,
        "coverage": {"oldest_reached": items[-1]["created_at"][:10] if items else None,
                     "days_covered": covered_days, "posts_read": len(items), "full": complete and not account_newer, "complete": complete,
                     "stop_reasons": reasons, "confidence": conf,
                     "calls": (fetched.get("fetch") or {}).get("calls"),
                     "errors": (fetched.get("fetch") or {}).get("errors", [])},
    }


def _median(xs: list) -> float:
    xs = sorted(xs)
    n = len(xs)
    return 0.0 if not n else float(xs[n // 2]) if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def reply_heavy_pattern(items: list[dict], inc: dict, cfg: dict | None = None) -> dict:
    """Detect 'almost no originals, constant short replies/quotes to big accounts that get many likes'."""
    c = (cfg or DEEP_DIVE_CONFIG).get("reply_heavy") or DEEP_DIVE_CONFIG["reply_heavy"]
    own = [p for p in items if item_kind(p) != "repost"]
    rq = [p for p in own if item_kind(p) in ("reply", "quote")]
    out = {"match": False, "own": len(own), "replies_quotes": len(rq)}
    if len(own) < c["min_items"] or not rq:
        return out
    orig = len(own) - len(rq)

    def ref_likes(p):
        for r in p.get("referenced_tweets") or []:
            if r.get("type") in ("replied_to", "quoted"):
                t = inc.get(str(r.get("id"))) or {}
                return (t.get("public_metrics") or {}).get("like_count")
        return None

    big = sum(1 for p in rq if (ref_likes(p) or 0) >= c["big_ref_likes"])
    short = sum(1 for p in rq if len(vf.LINK_RE.sub("", vf._strip_mentions(p.get("text") or "")).strip()) <= c["short_max_chars"])
    med = _median([(p.get("public_metrics") or {}).get("like_count") or 0 for p in rq])
    out.update({"original_share": round(orig / len(own), 2), "big_share": round(big / len(rq), 2),
                "short_share": round(short / len(rq), 2), "median_likes": med})
    out["match"] = (orig / len(own) <= c["max_original_share"] and len(rq) / len(own) >= c["min_reply_quote_share"]
                    and big / len(rq) >= c["min_big_share"] and short / len(rq) >= c["min_short_share"]
                    and med >= c["min_median_likes"])
    return out


def coverage_line(a: dict) -> str:
    c = a["coverage"]
    if not c["posts_read"]:
        return f"no posts found in last {DEEP_DIVE_CONFIG['window_days']} days" + (
            f" ({'; '.join(c['stop_reasons'])})" if c["stop_reasons"] else "")
    s = f"covered {c['days_covered']} days / {c['posts_read']} posts"
    if c.get("complete") and not c["full"]:
        s += f" (all available: {'; '.join(r for r in c['stop_reasons'] if r.startswith('account'))})"
    elif not c["full"]:
        s += f" (partial: {'; '.join(c['stop_reasons']) or 'limited data'}; confidence {c['confidence']})"
    return s


def recency_line(a: dict) -> str:
    if not a["last_post_at"]:
        return f"Last post: none in window (pinned excluded)" if a["pinned_excluded"] else "Last post: none in window"
    return f"Last post: {a['last_post_at'][:10]} ({a['days_since_last_post']} days ago)"


# ------------------------------------------------------------------ scoring -------------------------------------
def _bt_detectors(user: dict, friends: Any = None) -> dict:
    """Reuse Follower Finder detectors from src/classify.py."""
    pm = user.get("public_metrics") or {}
    c = cl.Candidate(user_id=str(user["id"]), username=user.get("username", ""), name=user.get("name") or "",
                     verified=True, bio=user.get("description"), followers_count=pm.get("followers_count"),
                     following_count=pm.get("following_count"), post_count=pm.get("tweet_count"))
    return {"impersonation": cl._impersonation_signals(c.name, c.username, c.bio, friends, c.user_id),
            "bio": cl.handle_bio_screen(c), "person": cl.person_check(c)}


def score_candidate(user: dict, a: dict, *, friends: Any = None, cfg: dict | None = None,
                    allow_reply_heavy: bool = False) -> dict:
    cfg = cfg or DEEP_DIVE_CONFIG
    W, R, RP = cfg["weights"], cfg["recency"], cfg["repost"]
    T = cfg["thresholds"]
    f: list[tuple[str, float]] = []   # (label, points)
    det = _bt_detectors(user, friends)
    out = {"handle": a["handle"], "coverage": coverage_line(a), "recency": recency_line(a)}

    if det["impersonation"] and "impersonation" in cfg["hard_skip"]:
        return {**out, "rec": "Skip", "score": None, "reason": det["impersonation"][0], "factors": [],
                "hard_skip": True}
    if not a["n_items"]:
        f.append((f"no posts in last {cfg['window_days']} days", R["stale_pts"]))
        rec = "Skip" if a["coverage"]["stop_reasons"] and "API error" not in a["coverage"]["stop_reasons"] else "Maybe"
        return {**out, "rec": rec, "score": R["stale_pts"], "factors": f,
                "reason": f"No activity found in {cfg['window_days']} days ({coverage_line(a)})"}

    conf = a["coverage"]["confidence"]
    d = a["days_since_last_post"]
    if d <= R["strong_max"]:
        f.append((f"active {d}d ago", R["strong_pts"]))
    elif d <= R["mild_max"]:
        f.append((f"last post {d}d ago", R["mild_pts"]))
    elif d <= R["negative_max"]:
        f.append((f"quiet {d}d", R["negative_pts"]))
    else:
        f.append((f"inactive {d}d", R["stale_pts"]))
    stale = d > R["negative_max"]

    s, n = a["signals"], a["n_items"]
    cap = W["signal_cap"]
    pos = [("builds Grok bots/agents", min(cap, s.get("grok_bot", 0) * W["grok_bot_building"])),
           ("Grok Imagine work", min(cap, s.get("imagine", 0) * W["grok_imagine"])),
           ("real conversations", min(cap, s.get("substantive", 0) * W["substantive_reply"]
                                     + s.get("back_and_forth", 0) * W["back_and_forth"]))]
    for lbl, p in pos:
        if p:
            f.append((lbl, round(p * conf, 2)))   # content scaled by coverage confidence
    oi = a["owner_interactions"]
    owner_sub = any(x["substantive"] for x in oi)
    if oi:
        f.append(("interacted with you" + (" substantively" if owner_sub else ""),
                  W["owner_interaction"] + (W["owner_substantive"] if owner_sub else 0)))
    rr = a["repost_ratio"] or 0
    own = n - a["kinds"].get("repost", 0)
    if rr > RP["high_ratio"]:
        f.append((f"{int(rr*100)}% reposts", RP["high_pts"]))
    elif rr > RP["mid_ratio"]:
        f.append((f"{int(rr*100)}% reposts", RP["mid_pts"]))
    if own:
        low_share = s.get("low", 0) / own
        if low_share:
            f.append((f"{int(low_share*100)}% low-effort posts", round(low_share * W["low_effort_ratio_pts"], 2)))
    if s.get("crypto"):
        f.append(("crypto shilling", max(-4.0, s["crypto"] * W["crypto_shill"])))
    if s.get("duplicates"):
        f.append(("duplicate posts", max(-3.0, s["duplicates"] * W["duplicates"])))
    if (a.get("reply_heavy") or {}).get("match") and not allow_reply_heavy:
        f.append(("low originality: short replies/quotes to big accounts", (cfg.get("reply_heavy") or DEEP_DIVE_CONFIG["reply_heavy"])["pts"]))
    for sign in det["person"]["bot_number_signs"]:
        f.append((sign.lower(), W["bot_number_signs"]))
    if det["bio"]["bio_words"]:
        f.append(("bio: " + ", ".join(det["bio"]["bio_words"]), W["bio_hint"]))

    score = round(sum(p for _, p in f), 2)
    rec = "Follow back" if score >= T["follow_back"] else "Maybe" if score >= T["maybe"] else "Skip"
    capped = None
    if own == 0 and not owner_sub:
        rec, capped = "Skip", "repost-only, no originals"
    if stale and rec == "Follow back":
        rec, capped = "Maybe", f"inactive {d}d: Follow back not possible"
    if conf < 0.5 and rec == "Follow back":
        rec, capped = "Maybe", "limited data"
    top = sorted(f, key=lambda x: abs(x[1]), reverse=True)[:3]
    reason = "; ".join(l for l, _ in top)
    if capped:
        reason = f"{capped}; {reason}"
    if not a["coverage"]["full"]:
        reason += f" [{coverage_line(a)}]"
    return {**out, "rec": rec, "score": score, "factors": f, "reason": reason, "hard_skip": False}


def deep_dive(user: dict, call_tool: Callable[[str, dict], dict], owner_id: str, *, friends: Any = None,
              now: dt.datetime | None = None, cfg: dict | None = None, allow_reply_heavy: bool = False) -> dict:
    """allow_reply_heavy: the owner's engagement preference (preferences.get_allow_reply_heavy), default OFF."""
    fetched = fetch_activity(user, call_tool, now=now, cfg=cfg)
    a = analyze(user, fetched, owner_id, now=now, cfg=cfg)
    # posts ride along so later checks (conduct.py account health) read the same evidence without new calls
    return {"analysis": a, "posts": fetched.get("posts") or [],
            **score_candidate(user, a, friends=friends, cfg=cfg, allow_reply_heavy=allow_reply_heavy)}


def card_line(r: dict) -> str:
    return f"{r['handle']}: {r['rec']} — {r['reason']} | {r['recency']} | {r['coverage']}"
