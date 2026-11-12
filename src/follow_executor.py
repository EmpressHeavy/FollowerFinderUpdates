"""Paced follow-backs from a daily verified card, including 'Follow all' (v3.5.24; browser contract v3.5.32).

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
    from . import x_web_session as xws
except ImportError:  # pragma: no cover
    import verified_followers as vf
    import x_web_session as xws

# v3.5.29: the ONE place web follow/unfollow pacing is configured.
PACING = {
    "gap_s": (20.0, 45.0),          # between accounts, uniform random (jittered)
    "pre_click_s": (2.0, 5.0),      # after the profile loads, before clicking
    "batch_every": 5,               # after every N actions...
    "batch_break_s": (120.0, 240.0),  # ...take a longer ~2-4 min break instead of the normal gap
}
PACE_TEXT = "natural human pace (about 20-45s apart, randomized, with a short break every 5)"
# v3.5.33: browser agents can't hold one long wait, so every wait is split into short waits, each under 30 s.
MAX_WAIT_CHUNK_S = 25.0
WAIT_TEXT = ("do every wait as several short waits of at most 25 s each (never one wait of 30 s or more); "
             "e.g. a 40 s gap = 20 s + 20 s, a 3 min break = 8 waits of 22.5 s")


def wait_chunks(seconds: float, max_chunk: float = MAX_WAIT_CHUNK_S) -> list[float]:
    """Split a wait into equal short waits, each <= max_chunk (< 30 s). 0 -> []."""
    import math
    seconds = max(0.0, float(seconds or 0))
    if seconds <= 0:
        return []
    n = max(1, math.ceil(seconds / max_chunk))
    return [round(seconds / n, 3)] * n


def sleep_chunked(seconds: float, sleep: Callable[[float], None] = time.sleep) -> list[float]:
    ch = wait_chunks(seconds)
    for c in ch:
        sleep(c)
    return ch
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
    "profile loads before clicking, and a ~2-4 min break after every 5) before the next account; " + WAIT_TEXT + ".\n"
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


# ---------- v3.5.32: browser-step contract (what the computerUse subagent gets and must report) ----------
# The X plugin cannot follow, so the real click happens in a browser subagent on x.com. Everything else (plan, pacing,
# guards, the append-only log, parsing what the subagent reports, the stop rules and the final verification) lives
# here and is tested. The only manual / out-of-repo part is the subagent itself clicking the one 'Follow' button.
OUT_FOLLOWED, OUT_ALREADY, OUT_NEEDS_SIGN_IN, OUT_WARNING = "followed", "already", "needs_sign_in", "warning"
OUT_FAILED, OUT_MISMATCH = "failed", "profile_mismatch"
OUTCOMES = (OUT_FOLLOWED, OUT_ALREADY, OUT_NEEDS_SIGN_IN, OUT_WARNING, OUT_FAILED, OUT_MISMATCH)
REPORT_KEYS = ("signed_in", "profile_matches", "button_before", "clicked", "button_after", "stop", "error")
NEVER_CLICK = ("Following", "Pending", "Message", "Post", "Like", "Reply", "Repost", "Block", "Mute", "Report")
_WARN_WORDS = (("rate limit", "rate_limit"), ("rate-limit", "rate_limit"), ("captcha", "challenge"),
               ("challenge", "challenge"), ("something went wrong", "something_went_wrong"), ("warning", "warning"),
               ("unusual activity", "warning"), ("suspended", "warning"), ("locked", "warning"))


def browser_task(item: dict) -> dict:
    """Structured per-account instruction for ONE browser subagent run (one account, one click at most)."""
    return {"task": "x_follow_one", "handle": item["handle"], "url": item["url"], "order": item.get("order"),
            "expect_handle": "@" + item["handle"].lower(), "pre_click_pause_s": item.get("pre_click_pause_s"),
            "click_only": "Follow", "never_click": list(NEVER_CLICK),
            "stop_on": ["sign-in page", "any X warning", "rate limit", "captcha/challenge", "Something went wrong"],
            "report_keys": list(REPORT_KEYS), "instructions": item.get("steps") or
            BROWSER_FOLLOW_ONE_STEPS.format(handle=item["handle"], url=item["url"])}


def _boolish(v):
    if isinstance(v, bool) or v is None:
        return v
    t = str(v).strip().lower()
    return True if t in ("true", "yes", "1", "y") else False if t in ("false", "no", "0", "n") else None


def parse_browser_result(raw) -> dict:
    """Normalize what the subagent reported (a dict, a JSON string, or JSON inside prose) into the REPORT_KEYS dict
    plus 'outcome' (one of OUTCOMES). Free text without JSON is read conservatively: it can stop a run or mark
    already/needs-sign-in, but it never counts as a follow (a follow needs button_after == 'Following')."""
    import json as _json
    import re as _re
    r: dict = {}
    if isinstance(raw, dict):
        r = dict(raw)
    elif isinstance(raw, str):
        m = _re.search(r"\{.*\}", raw, _re.S)
        if m:
            try:
                r = _json.loads(m.group(0))
            except ValueError:
                r = {}
        if not r:
            low = raw.lower()
            if "sign in" in low or "log in" in low or "signed out" in low or "not signed in" in low:
                r = {"signed_in": False}
            else:
                for w, code in _WARN_WORDS:
                    if w in low:
                        r = {"stop": code}
                        break
            if not r and ("already following" in low or "already pending" in low):
                r = {"button_before": "Following" if "following" in low else "Pending", "clicked": False}
            r.setdefault("error", raw.strip()[:200] or "empty report")
    for k in ("signed_in", "profile_matches", "clicked"):
        if k in r:
            r[k] = _boolish(r[k])
    if r.get("stop") is not None:
        s = str(r["stop"]).strip().lower().replace(" ", "_").replace("-", "_")
        r["stop"] = s if s in STOP_REASONS_V2 else ("warning" if s not in ("", "none", "null", "false") else None)
    r["outcome"] = outcome(r)
    return r


def outcome(r: dict) -> str:
    if r.get("signed_in") is False or r.get("stop") == "login_wall":
        return OUT_NEEDS_SIGN_IN
    if r.get("stop"):
        return OUT_WARNING
    if r.get("profile_matches") is False:
        return OUT_MISMATCH
    if _btn(r.get("button_before")) in ("following", "pending") and not r.get("clicked"):
        return OUT_ALREADY
    if r.get("clicked") and _btn(r.get("button_after")) in ("following", "pending"):
        return OUT_FOLLOWED
    return OUT_FAILED


def owner_config(user_key, root=None) -> dict:
    try:
        try:
            from . import storage
        except ImportError:  # pragma: no cover
            import storage
        return storage.load_user_config_dict(root, user_key=user_key)
    except Exception:
        return {}


class FollowSession:
    """Step-by-step driver for the parent bot: one browser subagent per account, strictly one at a time.

        s = FollowSession(user_key, run_id, picks)
        while (t := s.next_task()):          # t = {'wait_s', 'task' (browser_task), 'item'}
            wait t['wait_s'];  report = <run one browser subagent with t['task']>
            s.submit(report)                 # any X warning / sign-in wall ends the session
        result = s.finish(x)                 # re-reads get_users_me_following; only verified follows count
    v3.5.33: the FIRST task (and the first after any mid-batch needs_x_signin) is {'kind': 'x_preflight'}: run it
    read-only and submit() its report. No follow task is emitted until the signed-in @handle equals the owner's
    saved handle; a mismatch / sign-out stops the batch with nothing clicked (stop['action'] says what to do).
    Every step is appended to follow_back.jsonl (never rewritten). Report 'done' to the owner only from finish()."""

    def __init__(self, user_key: str, run_id: str, picks: list[dict], *, confirmations: dict | None = None,
                 rand: Callable[[float, float], float] = random.uniform, skip: Callable[[dict], str | None] | None = None,
                 root=None, track_source: str = "follow_back", owner_handle: str | None = None,
                 vault_saved: bool | None = None, relogin_attempts: int = 0):
        try:
            from . import unfollow_back as ub, blocked_by as bb
        except ImportError:  # pragma: no cover
            import unfollow_back as ub
            import blocked_by as bb
        self._ub = ub
        if skip is None:
            guard, gate = bb.follow_guard(user_key, root), ub.gate_skip(user_key, confirmations, root)
            skip = lambda p: guard(p) or gate(p)  # noqa: E731
        self.user_key, self.run_id, self.picks, self.root = user_key, run_id, picks, root
        self.rand, self.skip, self.track_source = rand, skip, track_source
        self.plan = build_follow_plan(picks, rand)
        self.results, self.skipped, self.gaps = [], [], []
        self.stop = None
        self.attempted = 0
        self._i = 0
        self._pending = None
        self.finished = None
        cfg = owner_config(user_key, root)
        self.owner_handle = owner_handle or cfg.get("owner_x_handle") or cfg.get("x_handle")
        self.vault_saved = bool(cfg.get("x_login_in_vault")) if vault_saved is None else bool(vault_saved)
        self.relogin_attempts, self.preflight_ok, self.reauths = relogin_attempts, False, 0
        self.preflight = None

    def _remaining(self, i):
        return [{"user_id": q["user_id"], "handle": q["handle"]} for q in self.plan[i:]]

    def _log(self, ev: dict):
        vf.append_log(self.user_key, {"run_id": self.run_id, **ev}, self.root)

    def next_task(self) -> dict | None:
        if self._pending is not None:
            raise RuntimeError("submit() the current account's report before asking for the next one")
        if self.stop is not None or self.finished is not None:
            return None
        if not self.preflight_ok:
            if not xws.norm_handle(self.owner_handle):
                self.stop = {"reason": xws.NEEDS_X_SIGNIN, "action": xws.SHOW_SIGNIN_CARD, "at_handle": None,
                             "detail": "owner X handle unknown", "not_attempted": self._remaining(self._i)}
                self._log({"event": "FOLLOW_STOPPED", **self.stop})
                return None
            self._pending = "preflight"
            return {"kind": "x_preflight", "wait_s": 0.0, "item": None, "task": xws.preflight_task(self.owner_handle)}
        while self._i < len(self.plan):
            item, pick = self.plan[self._i], self.picks[self._i]
            why = self.skip(pick)
            if why:
                self.skipped.append({**item, "reason": why})
                self._log({"event": "FOLLOW_SKIPPED", "user_id": item["user_id"], "handle": item["handle"],
                           "reason": why})
                self._i += 1
                continue
            wait = 0.0
            if self.attempted:
                wait = pace_gap(self.attempted, self.rand)
                self.gaps.append(wait)
            self._pending = self._i
            return {"kind": "follow", "wait_s": wait, "wait_chunks": wait_chunks(wait), "item": item,
                    "task": browser_task(item)}
        return None

    def submit(self, raw) -> dict:
        if self._pending is None:
            raise RuntimeError("no account is waiting for a report")
        if self._pending == "preflight":
            return self._submit_preflight(raw)
        i = self._pending
        self._pending, self._i = None, i + 1
        self.attempted += 1
        item, pick = self.plan[i], self.picks[i]
        r = parse_browser_result(raw if raw is not None else "")
        if "button_after" not in r and "verified_following" in r:  # legacy adapter report {'ok','verified_following'}
            r.setdefault("clicked", bool(r.get("ok") or r.get("verified_following")))
            r["button_after"] = "Following" if r.get("verified_following") else ("Follow" if r.get("clicked") else None)
        out = outcome(r)
        if out == OUT_NEEDS_SIGN_IN and self.reauths < 1:  # re-check the session, then retry this account once
            self.reauths += 1
            self.attempted -= 1
            self._i, self.preflight_ok = i, False
            self._log({"event": "FOLLOW_RECHECK_SESSION", "at_handle": item["handle"]})
            return {"outcome": out, "stop": None, "recheck": True}
        if out == OUT_NEEDS_SIGN_IN:
            self.stop = {"reason": NEEDS_SIGNIN, "at_handle": item["handle"],
                         "not_attempted": [{"user_id": q["user_id"], "handle": q["handle"]} for q in self.plan[i:]]}
            self._log({"event": "FOLLOW_STOPPED", **self.stop})
            return {"outcome": out, "stop": self.stop}
        if out == OUT_MISMATCH:
            self.skipped.append({**item, "reason": "profile_mismatch"})
            self._log({"event": "FOLLOW_SKIPPED", "user_id": item["user_id"], "handle": item["handle"],
                       "reason": "profile_mismatch"})
        elif out == OUT_ALREADY:
            reason = "already_" + _btn(r.get("button_before"))
            self.skipped.append({**item, "reason": reason})
            self._log({"event": "FOLLOW_SKIPPED", "user_id": item["user_id"], "handle": item["handle"], "reason": reason})
        else:
            self.results.append({**item, "report": r, "pick": pick})
            self._log({"event": "FOLLOW_ATTEMPT", "user_id": item["user_id"], "handle": item["handle"],
                       "clicked": bool(r.get("clicked")), "button_after": r.get("button_after"),
                       "stop": r.get("stop"), "error": r.get("error")})
        if r.get("stop"):
            self.stop = {"reason": r["stop"], "at_handle": item["handle"],
                         "not_attempted": [{"user_id": q["user_id"], "handle": q["handle"]} for q in self.plan[i + 1:]]}
            self._log({"event": "FOLLOW_STOPPED", **self.stop})
        return {"outcome": out, "stop": self.stop}

    def _submit_preflight(self, raw) -> dict:
        self._pending = None
        rep = xws.parse_report(raw if raw is not None else "", self.owner_handle)
        d = xws.decide(rep, self.owner_handle, vault_saved=self.vault_saved, relogin_attempts=self.relogin_attempts)
        self.preflight = {**rep, **d}
        self._log({"event": "X_SESSION_CHECK", "status": d["status"], "signed_in_as": rep["signed_in_as"],
                   "expected": "@" + xws.norm_handle(self.owner_handle), "action": d["action"]})
        if d["action"] == xws.PROCEED:
            self.preflight_ok = True
            return {"outcome": "x_preflight", "action": d["action"], "stop": None}
        self.stop = {"reason": d["status"] if d["status"] in (xws.WRONG_ACCOUNT,) else xws.NEEDS_X_SIGNIN,
                     "action": d["action"], "at_handle": None, "signed_in_as": rep["signed_in_as"],
                     "detail": d["text"], "not_attempted": self._remaining(self._i)}
        self._log({"event": "FOLLOW_STOPPED", **self.stop})
        return {"outcome": "x_preflight", "action": d["action"], "stop": self.stop}

    def finish(self, x=None) -> dict:
        """Confirm before reporting done: verify every attempted follow, write FOLLOW_VERIFIED rows, build the one
        summary. Until this returns, nothing may be reported to the owner as followed."""
        if self.finished is not None:
            return self.finished
        if self._pending is not None:
            raise RuntimeError("an account is still waiting for its report")
        ub, user_key, run_id, root = self._ub, self.user_key, self.run_id, self.root
        try:
            from . import memory as mem
        except ImportError:  # pragma: no cover
            import memory as mem
        following = _following_ids(x) if self.results else None
        # Validate the whole batch before any FOLLOWED / FOLLOW_VERIFIED line is appended.
        # A secret-filter rejection must not leave the earlier accounts written and the rest missing.
        planned = []
        for it in self.results:
            st = verify_status(it["report"], it["user_id"], following)
            p = it["pick"]
            mem.check_no_secret({"user_id": it["user_id"], "handle": it["handle"],
                                 "source": p.get("source", self.track_source), "status": st,
                                 "tweet_id": it.get("tweet_id"), "ids": it.get("ids")})
            planned.append((it, st))
        out = {FOLLOWED: [], PENDING: [], FAILED: [], NOT_AVAILABLE: []}
        for it, st in planned:
            out[st].append(it["handle"])
            vf.record_follow(user_key, run_id, it["user_id"], it["handle"], st == FOLLOWED,
                             verified_following=st == FOLLOWED, error=None if st == FOLLOWED else st, root=root)
            self._log({"event": "FOLLOW_VERIFIED", "user_id": it["user_id"], "handle": it["handle"], "status": st,
                       "verified_by": FOLLOWING_TOOL if following is not None and it["user_id"] in following else
                       ("button" if st in (FOLLOWED, PENDING) else None)})
            if st == FOLLOWED:
                p = it["pick"]
                ub.record_follow(user_key, it["user_id"], it["handle"], source=p.get("source", self.track_source),
                                 run_id=run_id, follows_user=p.get("follows_user"), root=root)
        out["followed_ids"] = [it["user_id"] for it in self.results if it["handle"] in out[FOLLOWED]]
        stop, skipped = self.stop, self.skipped
        status = stop["reason"] if stop and stop["reason"] in (NEEDS_SIGNIN, xws.WRONG_ACCOUNT) else (
            "stopped" if stop else "done")
        parts = [f"Verified follows: {len(out[FOLLOWED])}" + (f" ({', '.join('@' + h for h in out[FOLLOWED])})"
                                                              if out[FOLLOWED] else "") + "."]
        for k, lab in ((PENDING, "Pending (protected account)"), (FAILED, "Failed"), (NOT_AVAILABLE, "Not available")):
            if out[k]:
                parts.append(f"{lab}: {', '.join('@' + h for h in out[k])}.")
        if skipped:
            parts.append(f"Skipped: {', '.join('@' + s['handle'] + ' (' + s['reason'] + ')' for s in skipped)}.")
        if stop and stop.get("action") and stop.get("detail"):
            parts.append(stop["detail"])
        elif status == NEEDS_SIGNIN:
            parts.append(f"Stopped: {NEEDS_SIGNIN_TEXT}.")
        elif stop:
            parts.append(f"Stopped after X showed {stop['reason'].replace('_', ' ')} at @{stop['at_handle']}; "
                         f"{len(stop['not_attempted'])} not attempted.")
        self.finished = {"status": status, "plan": self.plan, **out, "skipped": skipped, "stopped": stop,
                         "gaps": self.gaps, "verified_with": FOLLOWING_TOOL if following is not None else "button_state",
                         "confirmed": True, "text": " ".join(parts)}
        return self.finished


def run_browser(user_key: str, run_id: str, picks: list[dict], browser_follow: Callable[[dict], dict], *, x=None,
                confirmations: dict | None = None, sleep: Callable[[float], None] = time.sleep,
                rand: Callable[[float, float], float] = random.uniform, skip: Callable[[dict], str | None] | None = None,
                root=None, track_source: str = "follow_back", preflight: Callable[[dict], dict] | None = None,
                owner_handle: str | None = None, vault_saved: bool | None = None) -> dict:
    """browser_follow(plan_item) -> the subagent's report (dict, or text parsed by parse_browser_result). x: X plugin
    client used to re-read get_users_me_following after the batch. confirmations: {user_id: 'Yes'|'No'} from
    refollow_gate_card. Thin loop over FollowSession."""
    s = FollowSession(user_key, run_id, picks, confirmations=confirmations, rand=rand, skip=skip, root=root,
                      track_source=track_source, owner_handle=owner_handle, vault_saved=vault_saved)
    while (t := s.next_task()) is not None:
        if t["kind"] == "x_preflight":
            s.submit(preflight(t["task"]) if preflight else {"signed_in": None, "error": "no preflight runner"})
            continue
        if t["wait_s"]:
            sleep_chunked(t["wait_s"], sleep)
        s.submit(browser_follow(t["item"]))
    return s.finish(x)
