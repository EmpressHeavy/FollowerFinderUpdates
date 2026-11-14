"""End-to-end daily verified follower review runner (v3.5.26; v3.5.28 unfollow-back tracker hooks).

The runner has two phases, because the owner's picks arrive later in chat:

  run_daily_review(owner_key, x, browser, prefs=None, now=None, calls_remaining=None) -> {"run_id", "cards", ...}
  finish_review(owner_key, run_id, picks, browser, ...) -> one follow summary

Everything is per owner. Every read and write passes owner_key (and root) to the owner-scoped files: terminator.db,
brain.db and data/users/<owner>/verified_review/. Nothing reads another owner's data.

Order in run_daily_review (each filter runs BEFORE any profiling, deep dive or scoring):
 1. get_users_me followers (newest first, paged) + following. Keep verified followers, drop mutuals, drop already seen.
 2. drive_by.observe() + exclude(): a follow then unfollow within 24 h is silently dropped.
 3. blocked_by: the browser runs BROWSER_BLOCK_CHECK_STEPS for needs_check(). filter_candidates() drops blocked accounts.
 4. recheck_queue.process_due(calls_remaining=...). Reactivated accounts join the card labelled "Rechecked".
 5. Deep dive per candidate (90-day window, pinned post excluded, repost penalties) and score_candidate with the
    allow_reply_heavy pref (default off). Scoring values are unchanged.
 6. bought_risk.apply with show_bought_risk. OFF (default) hides flagged accounts; ON shows them with the flag and
    reason. Never an auto-skip.
 7. translation_requests for Follow back / Maybe only -> the browser runs BROWSER_TRANSLATE_STEPS ->
    apply_browser_translations. Skip-rated accounts are not sent to translation and not shown.
 8. Cards: multi-select, at most 5 Follow back or Maybe accounts per card, nothing pre-ticked. Skip is left off
    the card (still marked seen, so it is not reviewed again as new, and it is not listed for the owner). If every
    reviewed account is Skip, cards is empty and note is review_cards.NO_SUITABLE_NOTE. A "Follow all on this card"
    option comes first and selects only that card's accounts. Each offered account shows its rating + one-line
    reason, the interest tie-in and the interaction summary. Missing data reads "Not available" / "None found",
    never invented.
finish_review:
 9. Validate picks against the pending card (unknown ids and unknown card numbers raise; Follow all and ticks
    expand only to displayed Follow back / Maybe, so a legacy pending card that still lists a Skip does not follow
    it). Then record_selection, then recheck_queue.record_card_outcome (unpicked inactive accounts are queued), then
    follow_executor.run on ONLY the picked accounts, with blocked_by.follow_guard (natural human pace (about 20-45s apart, randomized, with a short break every 5), stop on warning,
    append-only log). Returns ONE summary.
v3.5.28 (unfollow_back): each run seeds tracked_follow from verified mutuals; flagged accounts (owner unfollowed them
    after they unfollowed the owner) get unfollow_back.FLAG_MARK + REFOLLOW_WARNING on their card option, and
    followback_window() runs each day (result in out['followback_window']), and
    finish_review returns Yes/No gate cards (needs_refollow_confirmation) instead of following when a flagged account is
    picked without an answer. Only 'Yes' follows it. Every confirmed follow is recorded in tracked_follow.

Adapters (the live routine supplies real ones):
  XAdapter.call(tool, args) -> raw X tool JSON. In the routine this is the user-X MCP: get_users_me,
    get_users_me_followers, get_users_me_following, get_users_posts, search_posts_all, get_users_by_usernames.
  BrowserAdapter.block_check / translate / follow. In the routine each method is one computerUse browser run in the
    box browser signed in to the owner's X, given the matching *_STEPS constant verbatim.

CLI:  python -m src.daily_review --owner <key> [--dry-run]
A dry run shows the plan and the owner's prefs and calls nothing. A live run needs the adapters, so it can only run
inside the bot's routine (see docs/getting_started_daily_review.md).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import uuid
from typing import Any, Protocol

try:
    from . import (blocked_by as bb, bought_risk as br, conduct as cd, drive_by as db, follow_executor as fx, preferences as pf,
                   recheck_queue as rq, review_cards as rc, suggestion_history as sh, translation as tl, unfollow_back as ub,
                   verified_deep_dive as dd, verified_followers as vf)
except ImportError:  # pragma: no cover
    import blocked_by as bb, bought_risk as br, conduct as cd, drive_by as db, follow_executor as fx, preferences as pf  # type: ignore
    import recheck_queue as rq, review_cards as rc, suggestion_history as sh, translation as tl, unfollow_back as ub, verified_deep_dive as dd, verified_followers as vf  # type: ignore

FOLLOW_ALL = rc.FOLLOW_ALL
FOLLOW_ALL_LABEL = rc.FOLLOW_ALL_LABEL
RECHECKED_LABEL = rc.RECHECKED_LABEL
NO_SUITABLE_NOTE = rc.NO_SUITABLE_NOTE
NONE_FOUND = "None found"


def _with_tip(result: dict, user_key: str, root, now, kind: str) -> dict:
    """Count a finished review. Local bookkeeping only; a failure leaves the review result as it is."""
    try:
        from . import tip_prompt
    except ImportError:  # pragma: no cover
        import tip_prompt
    return tip_prompt.attach(result, user_key, root=root, now=now, kind=kind)


FOLLOWER_PAGE_CAP = None  # v3.5.31: no cap; page until X runs out (rate limits pause+resume in the adapter)
USER_FIELDS = "created_at,description,public_metrics,verified,verified_type,pinned_tweet_id,connection_status"
INTEREST_FACTORS = ("builds Grok bots/agents", "Grok Imagine work", "real conversations")


class XAdapter(Protocol):
    def call(self, tool: str, args: dict) -> dict: ...


class BrowserAdapter(Protocol):
    def block_check(self, users: list[dict], steps: str) -> dict:
        """-> {user_id: True | False | None}. None = not checked (stopped); never treated as blocked."""
    # optional: def mute_check(self, users, steps) -> {user_id: True | False | None}; only for accounts X returned
    # without connection_status (conduct.BROWSER_MUTE_CHECK_STEPS). None = not read; never guessed.
    def translate(self, requests: list[dict], steps: str) -> dict:
        """-> {user_id: [translated texts copied from X's Translate post]}. Missing = nothing captured."""
    def follow(self, handle: str, steps: str) -> dict:
        """ONE account via the computerUse subagent; steps = follow_executor.BROWSER_FOLLOW_ONE_STEPS for it.
        -> {'signed_in','profile_matches','button_before','clicked','button_after','stop','error'}."""


def load_prefs(owner_key: str, root=None) -> dict:
    return {"allow_reply_heavy": pf.get_allow_reply_heavy(owner_key, root=root),
            "show_bought_risk": pf.get_show_bought_risk(owner_key, root=root),
            "read_languages": pf.get_read_languages(owner_key, root=root),
            "daily_review_time": pf.get_daily_review_time(owner_key, root=root)}


def _page(x: XAdapter, tool: str, base: dict, cap: int | None = FOLLOWER_PAGE_CAP, stop_when=None) -> list[dict]:
    out, token = [], None
    while cap is None or len(out) < cap:
        r = x.call(tool, dict(base, **({"pagination_token": token} if token else {}))) or {}
        data = r.get("data") or []
        out += data
        token = (r.get("meta") or {}).get("next_token")
        if not token or not data or (stop_when and stop_when(data)):
            break
    return out if cap is None else out[:cap]


def _interest_line(score: dict) -> str:
    hits = [lbl for lbl, pts in score.get("factors") or [] if lbl in INTEREST_FACTORS and pts > 0]
    return "Interest tie-in: " + (", ".join(hits) if hits else NONE_FOUND)


def _slim(r: dict) -> dict:
    a = r.get("analysis") or {}
    return {"rec": r.get("rec"), "hard_skip": r.get("hard_skip", False),
            "analysis": {"n_items": a.get("n_items"), "pinned_excluded": a.get("pinned_excluded"),
                         "days_since_last_post": a.get("days_since_last_post"),
                         "coverage": {"stop_reasons": (a.get("coverage") or {}).get("stop_reasons") or []}}}


def _profile(user: dict, owner_id: str, score: dict, posts: list[dict] | None, *, rechecked=False,
             includes: dict | None = None) -> dict:
    # includes (referenced tweets) let interaction_summary see reposts/quotes/replies OF the owner; posts double as
    # search hits (only those replying to / mentioning the owner) so direct replies to the owner count too (same facts the deep dive scored on)
    payload = {"data": posts, "includes": {"tweets": list((includes or {}).values())}} if posts is not None else None
    hits = None if posts is None else [q for q in posts if str(q.get("in_reply_to_user_id")) == str(owner_id) or any(
        str(m.get("id")) == str(owner_id) for m in ((q.get("entities") or {}).get("mentions") or []))]
    p = vf.build_profile(user, owner_id, payload, hits)
    if p["interaction"]["status"] == "none found":
        p["interaction"]["note"] = NONE_FOUND
    p["rec"], p["reason"] = score.get("rec") or "Maybe", score.get("reason") or vf.NA
    p["interest"] = _interest_line(score)
    p["recency"] = score.get("recency") or vf.NA
    p["rechecked"] = rechecked
    p["posts"] = posts or []
    p["pinned_tweet_id"] = user.get("pinned_tweet_id")
    a = score.get("conduct")
    p["account_health"] = cd.card_line(a) if a else "Account health: " + cd.NA
    return p


def build_review_cards(profiles: list[dict], run_id: str, *, title_prefix: str = "New verified followers") -> list[dict]:
    """Multi-select, <= 5 Follow back / Maybe accounts per card, nothing pre-ticked, Follow all first.
    Skip-rated profiles are omitted. Counts and Follow all use only the accounts that remain."""
    return rc.build_review_cards(profiles, run_id, title_prefix=title_prefix)


def _state_path(owner_key, run_id, root):
    return vf._dir(owner_key, root) / f"pending_{run_id}.json"


def run_daily_review(owner_key: str, x: XAdapter, browser: BrowserAdapter, prefs: dict | None = None,
                     now: dt.datetime | None = None, calls_remaining: int | None = None, *, root=None,
                     first_run_show: int = 0, dd_cfg: dict | None = None,
                     conduct_cfg: dict | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    prefs = {**load_prefs(owner_key, root), **(prefs or {})}
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    used = 0
    budget = lambda: None if calls_remaining is None else calls_remaining - used  # noqa: E731

    # 1. followers + following
    me = (x.call("get_users_me", {}) or {}).get("data") or {}
    owner_id = str(me.get("id"))
    seen = vf.load_seen(owner_key, root)
    followers = _page(x, "get_users_me_followers", {"max_results": 1000, "user.fields": USER_FIELDS},
                      stop_when=lambda d: all(str(u["id"]) in seen["ids"] for u in d))
    following = _page(x, "get_users_me_following", {"max_results": 1000}, cap=10 ** 6)
    following_ids = {str(u["id"]) for u in following}
    verified = [u for u in followers if vf.is_verified(u)]
    ub.seed_from_mutuals(owner_key, verified, following_ids, root=root)  # v3.5.28; idempotent, real data only
    # 2. drive-by snapshot, then silent drop
    db.observe(owner_key, verified, now=now, root=root)
    if not seen.get("baselined_at") and not seen["ids"] and not first_run_show:
        vf.mark_seen(owner_key, verified, root, baseline=True)
        vf.log_run(owner_key, {"run_id": run_id, "baseline": len(verified)}, root)
        return _with_tip({"run_id": run_id, "cards": [], "baseline": True,
                          "summary": "First run: baseline saved, nothing to review."},
                         owner_key, root, now, "review")
    new = vf.select_new(verified, following_ids, seen)
    if first_run_show and not seen["ids"]:
        new = new[:first_run_show]
    new = db.exclude(owner_key, new, root=root)
    # 3. blocked-by (browser), filter BEFORE any profiling
    need = bb.needs_check(owner_key, new, now=now, root=root)
    if need:
        res = browser.block_check(need, bb.BROWSER_BLOCK_CHECK_STEPS) or {}
        for u in need:
            v = res.get(str(u["id"]))
            if v is not None:
                bb.record(owner_key, u["id"], u.get("username"), bool(v), now=now, root=root)
    new = bb.filter_candidates(owner_key, new, root=root)
    # 3b. muted? X returns connection_status ("muting") on the follower object: no extra call. Only when that field
    # is missing do we ask the browser (conduct.BROWSER_MUTE_CHECK_STEPS); unknown stays unknown (nothing shown).
    mutes = {}
    unknown = [u for u in new if cd.is_muted(u) is None]
    if unknown and hasattr(browser, "mute_check"):
        res = browser.mute_check(unknown, cd.BROWSER_MUTE_CHECK_STEPS) or {}
        mutes = {str(k): v for k, v in res.items() if v is not None}
    # 4. recheck queue
    rechecked = rq.process_due(owner_key, x.call, owner_id, now=now, root=root, dd_cfg=dd_cfg, calls_remaining=budget(),
                               following_ids=following_ids, allow_reply_heavy=bool(prefs["allow_reply_heavy"]))
    used += rechecked.get("calls_used") or 0
    # 5. deep dive + score
    pairs, shown = [], []
    cfg = dd_cfg or dd.DEEP_DIVE_CONFIG
    deferred = []
    for u in new:
        b = budget()
        if b is not None and b < cfg["max_calls_per_candidate"]:
            deferred.append(u); continue
        fetched = dd.fetch_activity(u, x.call, now=now, cfg=cfg)
        used += (fetched.get("fetch") or {}).get("calls") or 0
        a = dd.analyze(u, fetched, owner_id, now=now, cfg=cfg)
        s = {"analysis": a, **dd.score_candidate(u, a, cfg=cfg, allow_reply_heavy=bool(prefs["allow_reply_heavy"]))}
        posts = sorted(fetched.get("posts") or [], key=lambda p: p.get("created_at") or "", reverse=True)
        cd.apply(s, cd.assess(u, posts, cfg=conduct_cfg, muted=mutes.get(str(u["id"]))), cfg=cfg)
        pairs.append((u, _profile(u, owner_id, s, posts, includes=fetched.get("includes_tweets"))))
        shown.append((u, s))
    for r in rechecked.get("surface") or []:
        u = r["user"]
        if bb.is_blocked_by(owner_key, u.get("id"), root=root) or str(u.get("id")) in following_ids:
            continue
        cd.apply(r, cd.assess(u, r.get("posts"), cfg=conduct_cfg, muted=mutes.get(str(u.get("id")))), cfg=cfg)
        pairs.append((u, _profile(u, owner_id, r, None, rechecked=True)))
        shown.append((u, r))
    # 6. bought-risk (hide or flag; never a skip)
    kept = br.apply(owner_key, pairs, show=bool(prefs["show_bought_risk"]), now=now)
    kept_ids = {p["user_id"] for p in kept}
    shown = [(u, s) for u, s in shown if str(u["id"]) in kept_ids]
    # 7. translation via the browser, only for accounts a card will show (X's own Translate post)
    offered_profiles = [p for p in kept if rc.is_offered(p.get("rec"))]
    reqs = tl.translation_requests(offered_profiles, {"read_languages": prefs["read_languages"]})
    if reqs:
        captured = browser.translate(reqs, tl.BROWSER_TRANSLATE_STEPS) or {}
        by_id = {p["user_id"]: p for p in kept}
        for q in reqs:
            tl.apply_browser_translations(by_id[str(q["user_id"])], captured.get(str(q["user_id"])), lang=q["lang"])
    # 8. cards. Skip stays in `shown` (marked seen, internal recheck) and off the card and the return value.
    window = ub.followback_window(owner_key, x, now=now, root=root)  # v3.5.28 30-day follow-back window
    assembled = rc.assemble_review(kept, run_id)
    cards = ub.mark_card_options(assembled["cards"], ub.flagged(owner_key, root))
    sh.record_suggested(owner_key, offered_profiles, source="verified_review", run_id=run_id, root=root, now=now)
    sh.record_pass(owner_key, pass_id=run_id, source="verified_review",
                   qualified_ids=[p["user_id"] for p in offered_profiles], checked=len(shown), root=root, now=now)
    vf.mark_seen(owner_key, [u for u, _ in shown], root)  # includes Skip, so select_new will not return them
    state = {"run_id": run_id, "owner_key": owner_key, "cards": [{"card_no": c["card_no"], "ids": c["account_ids"]} for c in cards],
             "shown": [{"user": u, "r": _slim(s), "handle": "@" + u["username"]} for u, s in shown]}
    vf.st.save_json(_state_path(owner_key, run_id, root), state)
    vf.log_run(owner_key, {"run_id": run_id, "new": len(new), "shown": len(kept), "offered": assembled["offered"],
                           "skipped_count": assembled["skipped_count"], "rechecked": len(rechecked.get("surface") or []),
                           "deferred": len(deferred), "calls_used": used}, root)
    return _with_tip({"run_id": run_id, "cards": cards, "note": assembled["note"], "offered": assembled["offered"],
                      "skipped_count": assembled["skipped_count"], "calls_used": used,
                      "deferred": [u["id"] for u in deferred], "translation_requests": reqs,
                      "recheck": {k: rechecked.get(k) for k in ("still_inactive", "removed", "deferred")},
                      "followback_window": window},
                     owner_key, root, now, "review")


def finish_review(owner_key: str, run_id: str, picks: dict, browser: BrowserAdapter, *, root=None, now=None,
                  sleep=None, rand=None, refollow_confirmations: dict | None = None, x=None) -> dict:
    """picks: {card_no: [user_id, ... or FOLLOW_ALL]}. Follows ONLY the picked accounts, then returns one summary.
    Picks are checked against this owner's pending card before anything is logged or followed. Follow all and ticked
    ids expand only to Follow back / Maybe (a legacy card that still lists a Skip does not follow it). An id or card
    number that is not in the pending review raises ValueError and follows nobody. Empty ticks follow nobody.
    refollow_confirmations: {user_id: 'Yes'|'No'} answers to unfollow_back.refollow_gate_card for flagged picks. If a
    flagged account is picked without an answer, nothing is recorded or followed; returns
    {'needs_refollow_confirmation': [gate cards]} so the owner can answer, then call again with the answers."""
    state = vf.st.load_json(_state_path(owner_key, run_id, root), None)
    if not state or state.get("owner_key") != owner_key:
        raise ValueError(f"no pending review {run_id} for this owner")
    picks = picks or {}
    shown = {str(s["user"]["id"]): s for s in state["shown"]}
    profs = {i: {"handle": s["handle"]} for i, s in shown.items()}
    rec_by_id = {i: (s.get("r") or {}).get("rec") for i, s in shown.items()}
    hard_ids = {i for i, s in shown.items() if (s.get("r") or {}).get("hard_skip")}
    rc.check_card_keys(state["cards"], picks)
    submit_all = not picks
    resolved = []
    for c in state["cards"]:
        key_present = submit_all or c["card_no"] in picks or str(c["card_no"]) in picks
        raw = [] if submit_all else picks.get(c["card_no"], picks.get(str(c["card_no"]), []))
        if key_present and sh.is_decline_answer(raw):
            resolved.append((c, [], True, True))
        else:
            resolved.append((c, rc.resolve_picks(c["card_no"], c["ids"], raw, rec_by_id, hard_skip_ids=hard_ids),
                             False, key_present))
    flags = ub.flagged(owner_key, root)
    conf = {str(k): v for k, v in (refollow_confirmations or {}).items()}
    pending = []
    for _c, ids, _declined, _answered in resolved:
        pending += [ub.refollow_gate_card(i, shown[i]["handle"]) for i in ids
                    if i in flags and conf.get(i) not in (ub.YES, ub.NO)]
    if pending:
        return {"needs_refollow_confirmation": pending, "followed": [], "text": ub.REFOLLOW_WARNING}
    picked: list[str] = []
    handles = {i: s["handle"] for i, s in shown.items()}
    for c, ids, declined, answered in resolved:
        offered_ids = rc.selectable_ids(c["ids"], rec_by_id, hard_skip_ids=hard_ids)
        vf.record_selection(owner_key, run_id, c["card_no"], offered_ids, ids, profs, root=root)
        if answered:
            sh.record_card_result(owner_key, run_id, c["ids"], ids, handles, declined=declined,
                                  source="verified_review", root=root, now=now)
        picked += ids
    rq.record_card_outcome(owner_key, [(s["user"], s["r"]) for s in state["shown"]], picked, now=now, root=root)
    plan = [{"user_id": i, "handle": shown[i]["handle"], "follows_user": True} for i in picked]
    kw = {k: v for k, v in (("sleep", sleep), ("rand", rand)) if v is not None}
    # v3.5.29.1: the X plugin has no follow tool -> the browser plan (follow_executor.run_browser). browser.follow gets
    # ONE plan item's handle + steps for the bot's computerUse subagent; x re-reads get_users_me_following to verify.
    summary = fx.run_browser(owner_key, run_id, plan, lambda item: browser.follow(item["handle"], item["steps"]), x=x,
                             confirmations=conf,
                             skip=_both(bb.follow_guard(owner_key, root), ub.gate_skip(owner_key, conf, root)),
                             root=root, preflight=getattr(browser, "preflight", None), **kw)
    for uid in summary["followed_ids"]:
        rq.remove(owner_key, uid, shown[uid]["handle"], why="followed", root=root)
    return summary


def _both(a, b):
    return lambda p: a(p) or b(p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.daily_review")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    prefs = load_prefs(a.owner)
    plan = {"owner": a.owner, "prefs": prefs, "steps": vf.daily_steps({"verified_review": {"time": prefs["daily_review_time"]}})}
    if a.dry_run:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    print("A live run needs the X and browser adapters, so it can only run inside the bot's daily routine "
          "(docs/getting_started_daily_review.md). Use --dry-run here.", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
