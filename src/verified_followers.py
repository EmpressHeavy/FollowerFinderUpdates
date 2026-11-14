"""Verified Follower Review (v3.5.15). Daily, around the owner's chosen time (default 18:00 local), list VERIFIED
followers (any checkmark: blue, business/gold, government/gray) who followed since the last check, profile each one,
and show in-chat checkbox cards (multi-select, at most 5 per card). Only the accounts the owner ticks are followed
back. Product-wide: every path is per user under data/users/<user_key>/; nothing is shared between users.

Read path (X plugin, read-only):
  - followers:  get_users_me_followers (user.fields=verified,verified_type,description,public_metrics,created_at)
                X returns newest followers first; there is NO follow date, so "new since last check" means
                "not in verified_seen.json". The first run seeds the seen state (baseline) and shows nothing unless
                the owner asks for a first card from the newest N.
  - following:  get_users_me_following (to skip accounts she already follows)
  - posts:      get_users_posts(id, max_results=10, post.fields=created_at,referenced_tweets, expansions=referenced_tweets.id.author_id)
  - interaction: search_posts_all "from:<h1> OR from:<h2> ... (to:<me> OR @<me>)" (batched, max 25 results/call)
                 plus referenced_tweets in the recent posts whose author is the owner (catches reposts/quotes that
                 search misses). Search coverage is not proof of absence, so a miss is reported as
                 "None found in search + last 10 posts", never "never interacted".
Write path: follow-back. The X plugin has NO follow-user tool (checked v3.5.15: only follow_list). So a follow-back is
done in the signed-in box browser (profile > Follow), one at a time at a natural human pace (about 20-45s apart, randomized, with a short break every 5), stop on any warning, verified by the
Following button, then logged here. Missing data is always "Not available"; nothing is invented.

Files (per user, append-only where noted):
  data/users/<user_key>/verified_review/seen.json          ids already shown or baselined (+ first_seen_at)
  data/users/<user_key>/verified_review/runs.jsonl         one line per daily run (append-only)
  data/users/<user_key>/verified_review/follow_back.jsonl  every pick and every follow result (append-only, hash-chained)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

try:
    from . import storage as st
except ImportError:  # loaded as a top-level module
    import storage as st

CARD_SIZE = 5
DEFAULT_TIME = "18:00"
NA = "Not available"
VERIFIED_TYPES = {"blue", "business", "government"}
FOLLOW_METHOD = "browser_follow"  # no X plugin follow-user tool as of v3.5.15


def _dir(user_key: str, root=None) -> Path:
    d = st.user_data_root(user_key, root) / "verified_review"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def is_verified(u: dict) -> bool:
    vt = (u.get("verified_type") or "").lower()
    return bool(u.get("verified")) or vt in VERIFIED_TYPES


def load_seen(user_key: str, root=None) -> dict:
    return st.load_json(_dir(user_key, root) / "seen.json", {"ids": {}, "baselined_at": None}) or {"ids": {}, "baselined_at": None}


def save_seen(user_key: str, seen: dict, root=None) -> None:
    st.save_json(_dir(user_key, root) / "seen.json", seen)


def select_new(followers: list[dict], following_ids: Iterable[str], seen: dict, *,
               protected_ids: Iterable[str] = ()) -> list[dict]:
    """Newest-first followers -> verified, not already followed, not seen. Keeps X's order (newest first)."""
    fol, prot, ids = set(map(str, following_ids)), set(map(str, protected_ids)), seen.get("ids", {})
    return [u for u in followers if is_verified(u) and str(u["id"]) not in fol
            and str(u["id"]) not in ids and str(u["id"]) not in prot]


def mark_seen(user_key: str, users: list[dict], root=None, *, baseline: bool = False) -> dict:
    seen = load_seen(user_key, root)
    t = _now()
    for u in users:
        seen["ids"].setdefault(str(u["id"]), {"handle": u.get("username"), "first_seen_at": t,
                                              "baseline": baseline})
    if baseline and not seen.get("baselined_at"):
        seen["baselined_at"] = t
    save_seen(user_key, seen, root)
    return seen


def _clip(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def interaction_summary(user: dict, owner_id: str, posts_payload: dict | None, search_hits: list[dict] | None) -> dict:
    """Facts only. posts_payload = raw get_users_posts response; search_hits = posts from search authored by user."""
    uid = str(user["id"])
    found: list[dict] = []
    for p in (search_hits or []):
        if str(p.get("author_id")) != uid:
            continue
        kind = "reply" if str(p.get("in_reply_to_user_id")) == str(owner_id) else "mention"
        found.append({"kind": kind, "at": p.get("created_at"), "url": p.get("url")})
    if posts_payload:
        authors = {str(t["id"]): str(t.get("author_id")) for t in (posts_payload.get("includes") or {}).get("tweets", [])}
        for p in posts_payload.get("data") or []:
            for r in p.get("referenced_tweets") or []:
                if authors.get(str(r.get("id"))) == str(owner_id):
                    kind = {"retweeted": "repost", "quoted": "quote", "replied_to": "reply"}.get(r.get("type"), r.get("type"))
                    found.append({"kind": kind, "at": p.get("created_at"), "url": p.get("url")})
    seen, uniq = set(), []
    for f in found:
        k = (f["kind"], f["url"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    if uniq:
        return {"status": "yes", "items": uniq}
    if posts_payload is None and search_hits is None:
        return {"status": NA, "items": []}
    return {"status": "none found", "items": [], "note": "None found in search + last 10 posts (not proof of none)"}


def build_profile(user: dict, owner_id: str, posts_payload: dict | None = None, search_hits: list[dict] | None = None,
                  topics: str | None = None) -> dict:
    """topics: a short, factual summary of recent posts (written by the agent from posts_payload); NA if no posts."""
    pm = user.get("public_metrics") or {}
    posts = (posts_payload or {}).get("data") or []
    return {
        "user_id": str(user["id"]), "handle": "@" + user["username"], "display_name": user.get("name") or NA,
        "url": f"https://x.com/{user['username']}", "verified_type": user.get("verified_type") or NA,
        "about": _clip(user.get("description"), 160) or NA,
        "posts_about": topics or (NA if not posts else _clip(" | ".join(p.get("text", "") for p in posts[:3]), 200)),
        "last_post_date": (posts[0].get("created_at") or NA)[:10] if posts else NA,
        "followers": pm.get("followers_count", NA), "following": pm.get("following_count", NA),
        "post_count": pm.get("tweet_count", pm.get("post_count", NA)),
        "account_created": (user.get("created_at") or NA)[:10],
        "interaction": interaction_summary(user, owner_id, posts_payload, search_hits),
    }


def build_cards(profiles: list[dict], run_id: str, *, title_prefix: str = "New verified followers") -> list[dict]:
    """Checkbox cards, max CARD_SIZE each. Nothing is pre-ticked. Buttons: Follow back selected / Skip all.

    `profiles` is exactly who appears, and the title count is len(profiles). Review cards pass only
    Follow back and Maybe through review_cards.build_review_cards; this function does not add accounts.
    """
    cards = []
    for i in range(0, len(profiles), CARD_SIZE):
        chunk = profiles[i:i + CARD_SIZE]
        cards.append({
            "run_id": run_id, "card_no": i // CARD_SIZE + 1, "type": "multi_select",
            "title": f"{title_prefix} ({i + 1}-{i + len(chunk)} of {len(profiles)})",
            "options": [{"value": p["user_id"], "checked": False,
                         "label": f"{p['handle']} · {p['display_name']}",
                         "detail": (f"About: {p['about']} | Posts about: {p['posts_about']} | "
                                    f"Interacted with you: {_interaction_line(p['interaction'])} | "
                                    f"{p['followers']} followers · {p['following']} following · {p['post_count']} posts"
                                    + "".join(f" | {x}" for x in p.get("card_extras") or [])),
                         "url": p["url"]} for p in chunk],
            "buttons": ["Follow back selected", "Skip all"],
        })
    return cards


def _interaction_line(i: dict) -> str:
    if i["status"] == "yes":
        return ", ".join(sorted({x["kind"] for x in i["items"]}))
    return "None found" if i["status"] == "none found" else NA


def _prev_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    last = None
    for line in path.read_text().splitlines():
        if line.strip():
            last = line
    return hashlib.sha256(last.encode()).hexdigest() if last else None


def append_log(user_key: str, entry: dict, root=None) -> dict:
    """Append-only, hash-chained follow-back log."""
    p = _dir(user_key, root) / "follow_back.jsonl"
    e = {"at": _now(), **entry, "prev_entry_sha256": _prev_hash(p)}
    with open(p, "a") as f:
        f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
    st.apply_user_permissions(user_key, root)
    return e


def record_selection(user_key: str, run_id: str, card_no: int, card_ids: list[str], picked_ids: list[str],
                     profiles_by_id: dict, root=None) -> dict:
    picked = [i for i in picked_ids if i in card_ids]
    bad = [i for i in picked_ids if i not in card_ids]
    if bad:
        raise ValueError(f"picked ids not on card {card_no}: {bad}")
    return append_log(user_key, {"event": "SELECTION", "run_id": run_id, "card_no": card_no,
                                 "picked": [{"user_id": i, "handle": profiles_by_id[i]["handle"]} for i in picked],
                                 "not_picked": [{"user_id": i, "handle": profiles_by_id[i]["handle"]}
                                                for i in card_ids if i not in picked],
                                 "authorization": "OWNER_CHECKBOX"}, root)


def record_follow(user_key: str, run_id: str, user_id: str, handle: str, ok: bool, *, verified_following: bool,
                  method: str = FOLLOW_METHOD, error: str | None = None, root=None) -> dict:
    """Only for ids in a prior SELECTION by the owner (enforced)."""
    if user_id not in picked_ids(user_key, run_id, root):
        raise PermissionError(f"{handle} was not picked by the owner in run {run_id}")
    entry = append_log(user_key, {"event": "FOLLOWED" if ok and verified_following else "FOLLOW_FAILED",
                                  "run_id": run_id, "user_id": user_id, "handle": handle, "method": method,
                                  "verified_following": verified_following, "error": error}, root)
    if ok and verified_following:
        try:
            from . import suggestion_history as sh
        except ImportError:  # pragma: no cover
            import suggestion_history as sh
        sh.record_followed(user_key, user_id, handle, run_id=run_id, source="follow", root=root)
    return entry


def picked_ids(user_key: str, run_id: str, root=None) -> set[str]:
    p = _dir(user_key, root) / "follow_back.jsonl"
    out: set[str] = set()
    if p.exists():
        for line in p.read_text().splitlines():
            e = json.loads(line)
            if e.get("event") == "SELECTION" and e.get("run_id") == run_id:
                out |= {x["user_id"] for x in e["picked"]}
    return out


def log_run(user_key: str, run: dict, root=None) -> None:
    with open(_dir(user_key, root) / "runs.jsonl", "a") as f:
        f.write(json.dumps({"at": _now(), **run}, ensure_ascii=False) + "\n")


def daily_steps(cfg: dict) -> list[str]:
    t = ((cfg.get("verified_review") or {}).get("time")) or DEFAULT_TIME
    return [f"at {t} owner time (DAILY_REVIEW_TIME pref, default 18:00): daily_review.run_daily_review(owner_key, x, browser, "
            "prefs, now, calls_remaining) runs every step below in order and returns the cards; after the owner's picks, "
            "daily_review.finish_review(owner_key, run_id, picks, browser) records them and follows ONLY the picks",
            f"recheck_queue.process_due() first (due inactive accounts get the same 90-day deep dive;  (due inactive accounts get the same 90-day deep dive; "
            "reactivated ones join today's card marked 'rechecked', still-inactive ones wait another 30 days)",
            f"get_users_me_followers newest-first, page until a page is entirely 'seen' (cap 1000)",
            "drive_by.observe(followers) then drive_by.exclude(): a verified follow undone within 24 h is silently "
            "left off the card and out of the recheck queue (no flag, no notice)",
            "blocked_by: run BROWSER_BLOCK_CHECK_STEPS for blocked_by.needs_check(new) then "
            "blocked_by.filter_candidates() BEFORE any profiling/deep dive/scoring (silent; X API has no blocked-by field)",
            "get_users_me_following -> skip accounts already followed; skip Always Keep / Graveyard",
            "select_new(); first ever run: mark_seen(baseline=True) and show nothing (or newest N on request)",
            "per new account: get_users_posts (10) + one batched search_posts_all for interactions",
            "build_profile() facts only; 'Not available' when unknown",
            "v3.5.16 deep dive: get_users_by_usernames(user.fields=...,pinned_tweet_id) then "
            "verified_deep_dive.deep_dive() per candidate (timeline + search_posts_all from:<h>, 90 days, call budget); "
            "card shows rec + one-line reason + 'Last post: date (N days ago)' + coverage line",
            "bought_risk.apply(show=preferences.get_show_bought_risk()): new account + thousands of followers + "
            "polished bio -> hidden (default) or shown with a 'Possible bought or bot account' flag; never auto-skipped",
            "translation.translation_requests(candidates, prefs) -> for mostly-foreign-language accounts run "
            "translation.BROWSER_TRANSLATE_STEPS in the box browser (click X's own 'Translate post', copy the text, "
            "read only) -> translation.apply_browser_translations(candidate, captured)",
            "review_cards.build_review_cards(): checkbox cards, max 5, Follow back and Maybe only "
            "(Skip is marked seen and left off the card; if everyone is Skip the note says there is no "
            "suitable candidate); record_selection() on submit; mark_seen() everyone reviewed, including Skip",
            "follow back ONLY picked ids with follow_executor.run_browser() + its per-account browser plan (computerUse subagent): one at a time, "
            "normal Follow button, natural human pace (about 20-45s apart, randomized, with a short break every 5), stop on any warning/rate limit/challenge/login wall, "
            "each attempt via record_follow (append-only), ONE summary at the end; then recheck_queue.record_card_outcome() "
            "(inactive + not picked -> queued for a monthly recheck; followed / hard Skip -> removed)",
            "suggestion_history: every offered account is logged for this owner only (append-only). A confirmed follow "
            "and an explicit decline (Skip all) are separate states. Shown but not picked, or no follow-back, is not "
            "a rejection and can be suggested again after the cooldown",
            "tip: run_daily_review already counts this review (result['tip']). Show it only when tip.show is true, "
            "after the cards, never during first-run setup. Maybe later keeps the cadence. Remind me monthly, or "
            "dismissing the prompt, switches to at most once every 30 days. There is no forever off. "
            "A tip failure never stops the review"]


# ---- v3.5.15 follow-back scoring (owner beta spec, 2026-10-28). Product-wide: owner_handle is each user's own handle.
import re as _re

GM_RE = _re.compile(r"^\W*(gm|gn|good\s*(morning|night|evening)|gmgm|wagmi|lfg|follow\s*back|f4f|🔥+|💯+|❤+)\b", _re.I)
# Hard bait is the whole post's point (follow-for-follow, "say hello", timed prompts). A longer genuine
# question is not bait just because it contains a soft prompt such as "yes or no?" or "comment below".
HARD_FARM_RE = _re.compile(r"(follow\s*(me|back)|f4f|drop your|who('s| is) (still )?here|answer in \d+ seconds|"
                           r"retweet if|like if|say hello)", _re.I)
FARM_RE = _re.compile(r"(follow\s*(me|back)|f4f|drop your|who('s| is) (still )?here|answer in \d+ seconds|"
                      r"retweet if|like if|say hello|comment .* below|yes or no\?)", _re.I)
GENUINE_QUESTION_WORDS = 8
CRYPTO_RE = _re.compile(r"(\$[A-Z]{2,6}\b(?<!\$TSLA)|airdrop|presale|memecoin|pump|100x|dm me for|whitelist|"
                        r"t\.me/|join my telegram|signals)", _re.I)
VALUE_RE = _re.compile(r"(grok\s*imagine|grok\s*bot|built|building|shipped|prompt|agent|tutorial|thread|how to|"
                       r"i made|i built|code|open source|release)", _re.I)
LINK_RE = _re.compile(r"https?://\S+")


def interaction_query(candidate_handle: str, owner_handle: str) -> str:
    """Profile-search equivalent: from:<candidate> to:<owner> OR @<owner>."""
    c, o = candidate_handle.lstrip("@"), owner_handle.lstrip("@")
    return f"from:{c} (to:{o} OR @{o})"


def _strip_mentions(t: str) -> str:
    return _re.sub(r"^(@\w+\s*)+", "", t or "").strip()


def is_genuine_question(text: str) -> bool:
    """A real question with enough words to be about something. Soft bait inside it ("yes or no?",
    "comment below") does not make it engagement bait. Hard bait, crypto and shilling still do.
    A short "yes or no?" on its own is not genuine."""
    t = _strip_mentions(text or "")
    bare = LINK_RE.sub("", t).strip()
    if "?" not in bare:
        return False
    if HARD_FARM_RE.search(bare) or CRYPTO_RE.search(bare):
        return False
    return len(_re.findall(r"[A-Za-z]{2,}", bare)) >= GENUINE_QUESTION_WORDS


def classify_text(text: str) -> str:
    """high | low_gm | low_farm | low_crypto | low_effort. Rule-based, explainable.
    Genuine questions are high, not engagement bait."""
    t = _strip_mentions(text)
    bare = LINK_RE.sub("", t).strip()
    if CRYPTO_RE.search(t):
        return "low_crypto"
    if is_genuine_question(text):
        return "high"
    if FARM_RE.search(t):
        return "low_farm"
    if GM_RE.search(bare) and len(bare) < 40:
        return "low_gm"
    words = len(_re.findall(r"[A-Za-z]{2,}", bare))
    if words < 6:
        return "low_effort"
    return "high"


def reply_quality(replies: list[dict]) -> dict:
    labels = [classify_text(r.get("text", "")) for r in replies]
    hi = labels.count("high")
    return {"count": len(replies), "high": hi, "low": len(labels) - hi, "labels": labels,
            "examples": [{"text": _clip(_strip_mentions(r.get("text")), 140), "url": r.get("url"),
                          "at": (r.get("created_at") or "")[:10], "quality": l}
                         for r, l in list(zip(replies, labels))[:2]]}


def content_value(posts_payload: dict | None) -> dict:
    posts = (posts_payload or {}).get("data") or []
    if not posts:
        return {"status": NA}
    rt = sum(1 for p in posts if any(r.get("type") == "retweeted" for r in p.get("referenced_tweets") or []))
    own = [p for p in posts if not any(r.get("type") == "retweeted" for r in p.get("referenced_tweets") or [])]
    labels = [classify_text(p.get("text", "")) for p in own]
    val = sum(1 for p in own if VALUE_RE.search(p.get("text", "")))
    return {"status": "ok", "sample": len(posts), "repost_ratio": round(rt / len(posts), 2),
            "own_high": labels.count("high"), "own_low": len(labels) - labels.count("high"),
            "value_signals": val, "low_kinds": sorted({l for l in labels if l != "high"})}


def days_since(iso: str | None, now: dt.datetime | None = None) -> int | None:
    if not iso:
        return None
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return ((now or dt.datetime.now(dt.timezone.utc)) - t).days


# v3.5.32: the old recommend() scorer was removed. Follow back / Maybe / Skip comes only from the classify pipeline
# (run_classify_pipeline / classify / verified_deep_dive); content_value() and reply_quality() stay as its signals.
