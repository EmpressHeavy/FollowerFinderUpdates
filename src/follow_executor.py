"""Paced follow-backs from a daily verified card, including 'Follow all' (v3.5.24).

Runs the owner's picks strictly one at a time, never batched or in parallel, in the signed-in box browser using X's
normal Follow button flow: open the profile, click Follow, check the button now reads Following. No unusual automation,
no API hacks, no hidden endpoints.

Pacing (v3.5.29, one place: PACING below, used by every web follow AND unfollow): natural human pace. A jittered
random gap of about 20-45 s between accounts (never a constant interval), a short random 2-5 s pause after the profile
loads before clicking, and a longer ~2-4 min break after every 5 actions. Still one at a time; it is never sped up.

Any X warning, rate-limit notice, challenge/captcha or login wall stops the run at once. Every attempt is appended to the
existing hash-chained follow_back.jsonl through verified_followers.record_follow, along with a FOLLOW_STOPPED entry that
lists what was not attempted. Rows are never updated or deleted. v3.5.28: each confirmed follow is also recorded in brain.db
tracked_follow (unfollow_back.record_follow) for the monthly unfollow-back check. Nothing is sent to the owner per follow: run() returns
one completion summary.

v3.5.29: the user-X plugin has no follow tool, so follows really go through the bot's own browser on x.com via a
computerUse subagent. run_browser() drives that path: build_follow_plan() gives per-handle steps, the flagged re-follow
Yes/No gate and blocked-by guard run first, a 'Following'/'Pending' button is never clicked, any X warning stops the
whole batch, a signed-out browser returns NEEDS_SIGNIN (the bot hands the desktop to the user via request_box_help;
passwords are never asked for in chat), and every follow is verified afterwards against get_users_me_following (or
the button reading 'Following'). Statuses: followed / pending / failed / not_available. Only verified follows count
and only those are added to tracked_follow.
"""
from __future__ import annotations

import random
import time
from typing import Callable

try:
    from . import verified_followers as vf
except ImportError:  # pragma: no cover
    import verified_followers as vf

# v3.5.29: the ONE place web follow/unfollow pacing is configured.
PACING = {
    "gap_s": (20.0, 45.0),          # between accounts, uniform random (jittered)
    "pre_click_s": (2.0, 5.0),      # after the profile loads, before clicking
    "batch_every": 5,               # after every N actions...
    "batch_break_s": (120.0, 240.0),  # ...take a longer ~2-4 min break instead of the normal gap
}
PACE_TEXT = "natural human pace (about 20-45s apart, randomized, with a short break every 5)"
FOLLOW_GAP_MIN, FOLLOW_GAP_MAX = PACING["gap_s"]
FOLLOW_PACING = {"FOLLOW_GAP_MIN": FOLLOW_GAP_MIN, "FOLLOW_GAP_MAX": FOLLOW_GAP_MAX, **PACING}

BROWSER_FOLLOW_STEPS = (
    "Browser step: follow back the owner's picks (box browser, signed in to X).\n"
    "Do the accounts strictly one at a time, in order. Never open several tabs or follow in parallel.\n"
    "For each account:\n"
    "1. Open https://x.com/<handle>.\n"
    "2. Click the normal 'Follow' button on the profile. Use no scripts, keyboard macros, API calls or hidden endpoints.\n"
    "3. Check that the button now reads 'Following'. Report ok / not ok.\n"
    "4. If X shows any warning, rate-limit notice, challenge/captcha or login wall, report it and STOP the whole run.\n"
    "5. Wait the gap follow_executor gives you (natural human pace: about 20-45 s randomized, a 2-5 s pause after the "
    "profile loads before clicking, and a ~2-4 min break after every 5) before the next account.\n"
    "Never like, reply, repost, DM or change anything else. Don't message the owner per follow; there's one summary at the end."
)
STOP_REASONS = ("warning", "rate_limit", "challenge", "login_wall")


def gap(rand: Callable[[float, float], float] = random.uniform, lo: float = FOLLOW_GAP_MIN,
        hi: float = FOLLOW_GAP_MAX) -> float:
    return rand(lo, hi)


def pace_gap(done: int, rand: Callable[[float, float], float] = random.uniform) -> float:
    """Wait before the next account after `done` actions: a ~2-4 min break after every batch_every, else 20-45 s."""
    if done and done % PACING["batch_every"] == 0:
        return rand(*PACING["batch_break_s"])
    return rand(*PACING["gap_s"])


def pre_click_pause(rand: Callable[[float, float], float] = random.uniform) -> float:
    return rand(*PACING["pre_click_s"])


def estimate_seconds(n: int) -> float:
    gaps = max(0, n - 1)
    breaks = gaps // PACING["batch_every"]
    return ((gaps - breaks) * sum(PACING["gap_s"]) / 2 + breaks * sum(PACING["batch_break_s"]) / 2
            + n * sum(PACING["pre_click_s"]) / 2)


def run(user_key: str, run_id: str, picks: list[dict], browser_follow: Callable[[str], dict], *,
        sleep: Callable[[float], None] = time.sleep, rand: Callable[[float, float], float] = random.uniform,
        skip: Callable[[dict], str | None] | None = None, root=None, track_source: str = "follow_back") -> dict:
    """picks: [{'user_id','handle'}] in card order (all of them for 'Follow all'). browser_follow(handle) -> {'ok': bool,
    'verified_following': bool, 'stop': None|one of STOP_REASONS, 'error': str|None}. skip(pick) -> reason or None,
    for guard filters. When skip isn't given, blocked_by.follow_guard is used, so blocked accounts are always refused. Returns one summary; nothing is sent per follow."""
    if skip is None:
        try:
            from . import blocked_by as bb
        except ImportError:  # pragma: no cover
            import blocked_by as bb
        guard = bb.follow_guard(user_key, root)
        try:
            from . import unfollow_back as _ub
        except ImportError:  # pragma: no cover
            import unfollow_back as _ub
        gate = _ub.gate_skip(user_key, None, root)  # v3.5.28: flagged accounts need a Yes (finish_review passes it)
        skip = lambda p: guard(p) or gate(p)  # noqa: E731
    followed, failed, skipped, gaps = [], [], [], []
    stop = None
    attempted = 0
    for i, p in enumerate(picks):
        why = skip(p) if skip else None
        if why:
            skipped.append({**p, "reason": why})
            vf.append_log(user_key, {"event": "FOLLOW_SKIPPED", "run_id": run_id, "user_id": p["user_id"],
                                     "handle": p["handle"], "reason": why}, root)
            continue
        if attempted:
            g = pace_gap(attempted, rand)
            gaps.append(g)
            sleep(g)
        attempted += 1
        r = browser_follow(p["handle"]) or {}
        ok = bool(r.get("ok")) and bool(r.get("verified_following"))
        vf.record_follow(user_key, run_id, p["user_id"], p["handle"], ok,
                         verified_following=bool(r.get("verified_following")),
                         error=r.get("stop") or r.get("error"), root=root)
        (followed if ok else failed).append(p)
        if ok:  # v3.5.28: every confirmed follow is tracked for the monthly unfollow-back check
            try:
                from . import unfollow_back as ub
            except ImportError:  # pragma: no cover
                import unfollow_back as ub
            ub.record_follow(user_key, p["user_id"], p["handle"], source=p.get("source", track_source), run_id=run_id,
                             follows_user=p.get("follows_user"), root=root)
        if r.get("stop"):
            stop = {"reason": r["stop"], "at_handle": p["handle"], "not_attempted": picks[i + 1:]}
            vf.append_log(user_key, {"event": "FOLLOW_STOPPED", "run_id": run_id, "reason": r["stop"],
                                     "at_handle": p["handle"],
                                     "not_attempted": [{"user_id": x["user_id"], "handle": x["handle"]}
                                                       for x in picks[i + 1:]]}, root)
            break
    return summary(followed, failed, skipped, stop, gaps)


def summary(followed, failed, skipped, stop, gaps) -> dict:
    not_done = len(failed) + len(skipped) + (len(stop["not_attempted"]) if stop else 0)
    reasons = []
    if stop:
        reasons.append(f"stopped after X showed a {stop['reason'].replace('_', ' ')} at {stop['at_handle']}")
    if failed and not (stop and len(failed) == 1 and failed[0]["handle"] == stop["at_handle"]):
        reasons.append(f"{len(failed)} didn't confirm as Following")
    if skipped:
        reasons.append(f"{len(skipped)} skipped")
    text = f"Follow-backs done: followed {len(followed)}."
    if not_done:
        text += f" Not followed {not_done} ({'; '.join(reasons) or 'failed'})."
    return {"followed": [p["handle"] for p in followed], "failed": [p["handle"] for p in failed],
            "skipped": skipped, "stopped": stop, "gaps": gaps, "text": text}


# ---------- v3.5.29: browser follow path (the X plugin has no follow tool) ----------
FOLLOWING_TOOL = "get_users_me_following"
NEEDS_SIGNIN = "needs_x_signin"
NEEDS_SIGNIN_TEXT = "needs X sign-in in the bot's browser (one time)"
FOLLOWED, PENDING, FAILED, NOT_AVAILABLE = "followed", "pending", "failed", "not_available"
STOP_REASONS_V2 = STOP_REASONS + ("something_went_wrong",)

BROWSER_FOLLOW_ONE_STEPS = (
    "Browser step (computerUse subagent, the bot's own browser on x.com). Follow exactly one account: @{handle}.\n"
    "1. Open {url}. If X shows a sign-in page or you are not signed in, do nothing else and report signed_in=false.\n"
    "2. Confirm the profile's @handle is exactly @{handle} (case-insensitive). If not, report profile_matches=false and stop.\n"
    "   Then wait the plan's pre_click_pause_s (a random 2-5 s, like a person reading the profile).\n"
    "3. Read the main profile button. If it says 'Following' or 'Pending', do NOT click it; report button_before.\n"
    "4. Only if it says 'Follow', click that one button once. Use no scripts, macros, API calls or hidden endpoints.\n"
    "5. Read the button again and report button_after ('Following', 'Pending' or what it shows).\n"
    "6. If X shows any warning, rate-limit notice, challenge/captcha, 'Something went wrong' dialog or login wall, "
    "report stop=<reason> right away.\n"
    "Never click anything else: no DM, no post, no like, no reply, no repost, no block, no mute. Don't message anyone.\n"
    "Report JSON: {{signed_in, profile_matches, button_before, clicked, button_after, stop, error}}."
)


def build_follow_plan(picks: list[dict], rand: Callable[[float, float], float] = random.uniform) -> list[dict]:
    """One browser task per picked handle, in card order, strictly one at a time at a natural human pace."""
    plan = []
    for i, p in enumerate(picks):
        h = (p.get("handle") or "").lstrip("@")
        url = f"https://x.com/{h}"
        plan.append({"user_id": str(p["user_id"]), "handle": h, "url": url, "order": i + 1,
                     "gap_before_s": 0 if i == 0 else list(PACING["batch_break_s"] if i % PACING["batch_every"] == 0
                                                           else PACING["gap_s"]),
                     "pre_click_pause_s": round(pre_click_pause(rand), 1),
                     "steps": BROWSER_FOLLOW_ONE_STEPS.format(handle=h, url=url)})
    return plan


def _following_ids(x):
    if x is None:
        return None
    try:
        from . import unfollow_back as ub
    except ImportError:  # pragma: no cover
        import unfollow_back as ub
    ids, _why = ub._page_all(x, FOLLOWING_TOOL)
    return ids


def _btn(v) -> str:
    return (v or "").strip().lower()


def verify_status(r: dict, user_id: str, following: set | None) -> str:
    """Never claim a follow unless verified by get_users_me_following or the button reading 'Following'."""
    if following is not None and str(user_id) in following:
        return FOLLOWED
    after = _btn(r.get("button_after"))
    if following is not None:  # the list was read: it wins over the button
        return PENDING if after == "pending" else FAILED
    if after == "following":
        return FOLLOWED
    if after == "pending":
        return PENDING
    if not r.get("clicked"):
        return FAILED
    if following is None and not after:
        return NOT_AVAILABLE
    return FAILED


def run_browser(user_key: str, run_id: str, picks: list[dict], browser_follow: Callable[[dict], dict], *, x=None,
                confirmations: dict | None = None, sleep: Callable[[float], None] = time.sleep,
                rand: Callable[[float, float], float] = random.uniform, skip: Callable[[dict], str | None] | None = None,
                root=None, track_source: str = "follow_back") -> dict:
    """browser_follow(plan_item) -> the subagent's JSON report (see BROWSER_FOLLOW_ONE_STEPS). x: X plugin client used
    to re-read get_users_me_following after the batch. confirmations: {user_id: 'Yes'|'No'} from refollow_gate_card."""
    try:
        from . import unfollow_back as ub, blocked_by as bb
    except ImportError:  # pragma: no cover
        import unfollow_back as ub
        import blocked_by as bb
    if skip is None:
        guard, gate = bb.follow_guard(user_key, root), ub.gate_skip(user_key, confirmations, root)
        skip = lambda p: guard(p) or gate(p)  # noqa: E731
    plan = build_follow_plan(picks, rand)
    results, skipped, gaps = [], [], []
    stop = None
    attempted = 0

    def log(ev: dict):
        vf.append_log(user_key, {"run_id": run_id, **ev}, root)

    for i, item in enumerate(plan):
        why = skip(picks[i])
        if why:
            skipped.append({**item, "reason": why})
            log({"event": "FOLLOW_SKIPPED", "user_id": item["user_id"], "handle": item["handle"], "reason": why})
            continue
        if attempted:
            g = pace_gap(attempted, rand)
            gaps.append(g)
            sleep(g)
        attempted += 1
        r = dict(browser_follow(item) or {})
        if "button_after" not in r and "verified_following" in r:  # legacy adapter report {'ok','verified_following'}
            r.setdefault("clicked", bool(r.get("ok") or r.get("verified_following")))
            r["button_after"] = "Following" if r.get("verified_following") else ("Follow" if r.get("clicked") else None)
        if r.get("signed_in") is False or r.get("stop") == "login_wall":
            stop = {"reason": NEEDS_SIGNIN, "at_handle": item["handle"],
                    "not_attempted": [{"user_id": q["user_id"], "handle": q["handle"]} for q in plan[i:]]}
            log({"event": "FOLLOW_STOPPED", **stop})
            break
        if r.get("profile_matches") is False:
            skipped.append({**item, "reason": "profile_mismatch"})
            log({"event": "FOLLOW_SKIPPED", "user_id": item["user_id"], "handle": item["handle"],
                 "reason": "profile_mismatch"})
        elif _btn(r.get("button_before")) in ("following", "pending") and not r.get("clicked"):
            reason = "already_" + _btn(r.get("button_before"))
            skipped.append({**item, "reason": reason})
            log({"event": "FOLLOW_SKIPPED", "user_id": item["user_id"], "handle": item["handle"], "reason": reason})
        else:
            results.append({**item, "report": r, "pick": picks[i]})
            log({"event": "FOLLOW_ATTEMPT", "user_id": item["user_id"], "handle": item["handle"],
                 "clicked": bool(r.get("clicked")), "button_after": r.get("button_after"),
                 "stop": r.get("stop"), "error": r.get("error")})
        if r.get("stop"):
            stop = {"reason": r["stop"], "at_handle": item["handle"],
                    "not_attempted": [{"user_id": q["user_id"], "handle": q["handle"]} for q in plan[i + 1:]]}
            log({"event": "FOLLOW_STOPPED", **stop})
            break
    following = _following_ids(x) if results else None
    out = {FOLLOWED: [], PENDING: [], FAILED: [], NOT_AVAILABLE: []}
    for it in results:
        st = verify_status(it["report"], it["user_id"], following)
        out[st].append(it["handle"])
        vf.record_follow(user_key, run_id, it["user_id"], it["handle"], st == FOLLOWED,
                         verified_following=st == FOLLOWED, error=None if st == FOLLOWED else st, root=root)
        log({"event": "FOLLOW_VERIFIED", "user_id": it["user_id"], "handle": it["handle"], "status": st,
             "verified_by": FOLLOWING_TOOL if following is not None and it["user_id"] in following else
             ("button" if st in (FOLLOWED, PENDING) else None)})
        if st == FOLLOWED:
            p = it["pick"]
            ub.record_follow(user_key, it["user_id"], it["handle"], source=p.get("source", track_source),
                             run_id=run_id, follows_user=p.get("follows_user"), root=root)
    out["followed_ids"] = [it["user_id"] for it in results if it["handle"] in out[FOLLOWED]]
    status = NEEDS_SIGNIN if stop and stop["reason"] == NEEDS_SIGNIN else ("stopped" if stop else "done")
    parts = [f"Verified follows: {len(out[FOLLOWED])}" + (f" ({', '.join('@' + h for h in out[FOLLOWED])})"
                                                          if out[FOLLOWED] else "") + "."]
    for k, lab in ((PENDING, "Pending (protected account)"), (FAILED, "Failed"), (NOT_AVAILABLE, "Not available")):
        if out[k]:
            parts.append(f"{lab}: {', '.join('@' + h for h in out[k])}.")
    if skipped:
        parts.append(f"Skipped: {', '.join('@' + s['handle'] + ' (' + s['reason'] + ')' for s in skipped)}.")
    if status == NEEDS_SIGNIN:
        parts.append(f"Stopped: {NEEDS_SIGNIN_TEXT}.")
    elif stop:
        parts.append(f"Stopped after X showed {stop['reason'].replace('_', ' ')} at @{stop['at_handle']}; "
                     f"{len(stop['not_attempted'])} not attempted.")
    return {"status": status, "plan": plan, **out, "skipped": skipped, "stopped": stop, "gaps": gaps,
            "verified_with": FOLLOWING_TOOL if following is not None else "button_state", "text": " ".join(parts)}
