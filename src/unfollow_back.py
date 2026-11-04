"""Unfollow-back tracker (v3.5.28).

1. Tracking. Every account the owner follows through Follower Finder (follow_executor.run, any source) is recorded in
   tracked_follow in the owner's brain.db (data/users/<user_key>/memory/brain.db; per owner, nothing shared). The first
   time, seed_from_mutuals() also adds verified followers the owner already follows back (they follow each other),
   from the same get_users_me_followers / get_users_me_following data the daily review reads. Seeded rows have no
   follow date (X gives none), so followed_at stays NULL and shows as "Not available"; nothing is invented.
2. Monthly recheck (monthly check-in on the 28th, run_monthly(); or recheck() directly). Does each tracked account still
   follow the owner? Source: the user-X plugin's get_users_me_followers, paged to the END (no cap). The plugin has no
   per-account relationship / "follows you" tool (checked v3.5.28: only get_users_me_followers / _following and list
   tools), so the complete follower list is the check. get_users_me_following tells whether the owner still follows them
   (if not, the row is closed as 'owner_unfollowed' and is not offered). If the follower list can't be read completely,
   the browser runs BROWSER_FOLLOWS_YOU_CHECK_STEPS (read-only "Follows you" badge). Anything still unknown is
   "Not available" and is never treated as an unfollow.
3. Cards. report_card() lists who stopped following, each with https://x.com/<handle>. choice_cards(): multi-select, at
   most 5 accounts per card, nothing pre-ticked, first option 'Keep following all', then 'Unfollow @handle'.
4. execute_unfollows() unfollows ONLY ticked accounts (picks must come from record_picks), one at a time at a natural human pace (fx.PACING: about 20-45 s apart, randomized, ~2-4 min break every 5)
   (follow_executor pacing), stops on any X warning, logs every attempt append-only (unfollow_back_log). Each confirmed
   unfollow is flagged permanently in flagged_account.
6. 30-day follow-back window. A follow of an account that does NOT already follow the owner (e.g. interest discovery;
   record_follow(..., follows_user=False)) is tracked with status 'awaiting_followback' and its followed_at. The daily
   review calls followback_window(): accounts followed 30+ days ago that still don't follow back get WINDOW_MESSAGE
   (owner's wording, verbatim) and window_cards() (max 4 accounts per card, nothing pre-ticked, profile links,
   'Unfollow all' first, then 'Keep all', then 'Unfollow @handle'). record_window_picks() marks kept ones 'kept' (never
   asked again); execute_unfollows(..., flag=False) unfollows picks with the same pacing/stop/logging and does NOT flag
   them (the flag is only for accounts that followed the owner and then stopped). Ones that followed back join the
   monthly unfollow-back check (status 'tracking').
5. Re-follow gate. If a flagged account shows up as a daily-review candidate, its card option carries FLAG_MARK and
   REFOLLOW_WARNING, and finish_review follows it only after a 'Yes' on refollow_gate_card(). The flag changes nothing
   else (no score change); it is only a warning gate.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import time
from typing import Callable

try:
    from . import follow_executor as fx, memory as mem
except ImportError:  # pragma: no cover
    import follow_executor as fx, memory as mem  # type: ignore

NA = "Not available"
CARD_SIZE = 5
MONTHLY_DAY = 28
KEEP_ALL = "__keep_following_all__"
KEEP_ALL_LABEL = "Keep following all"
FLAG_MARK = "\u26a0\ufe0f"
REFOLLOW_WARNING = ("This account has followed you and unfollowed you before, are you sure you want to follow this "
                    "account back?")
YES, NO = "Yes", "No"
FOLLOWS_YOU_TOOL = "get_users_me_followers"
FOLLOWING_TOOL = "get_users_me_following"
STOP_REASONS = fx.STOP_REASONS
AWAITING = "awaiting_followback"
WINDOW_DAYS = 30
WINDOW_CARD_SIZE = 4
UNFOLLOW_ALL = "__unfollow_all__"
KEEP_ALL_WINDOW = "__keep_all__"
WINDOW_MESSAGE = ("hey, these accounts you have followed, but they havn't followed you back in 30 days, do you want to "
                  "unfollow or keep following them?")  # owner's wording, verbatim
KEPT_WARNING = "Heads up: I won't remind you about these accounts again, even if they never follow you back."

BROWSER_FOLLOWS_YOU_CHECK_STEPS = (
    "Browser step: does this account still follow the owner? (box browser, signed in to X; read only). Only used when "
    "the X plugin's follower list could not be read completely.\n"
    "For each account:\n"
    "1. Open https://x.com/<handle>.\n"
    "2. Read the profile only. If it shows the 'Follows you' badge, report follows_you=True; if the profile loads "
    "normally without it, report follows_you=False. If the profile doesn't load (suspended, deactivated, error), report None.\n"
    "Never click anything (no Follow, Unfollow, Block or Report). STOP on any X warning, captcha or login wall; "
    "unchecked accounts are reported as None (Not available), never guessed."
)
BROWSER_UNFOLLOW_STEPS = (
    "Browser step: unfollow ONLY the accounts the owner ticked (monthly unfollow-back card or 30-day window card), in "
    "the bot's own browser on x.com, signed in as the owner (x_web_session preflight first). Strictly one at a time, "
    "in order; never in parallel. Use each account's own task (BROWSER_UNFOLLOW_ONE_STEPS): open the three-dot 'More' "
    "menu; click 'Unfollow @handle' if it is there, otherwise Escape and click the main 'Following' button; then "
    "promptly confirm 'Unfollow', reload and check the main "
    "button reads 'Follow'. Never click Block, Mute, Report or any other menu item. STOP the whole run on any X "
    "warning, rate-limit notice, challenge/captcha or login wall. Between accounts wait the gap unfollow_back gives "
    "you (natural human pace: about 20-45 s randomized, 2-5 s pause after the profile loads, ~2-4 min break after "
    "every 5); " + fx.WAIT_TEXT + ". Never block, mute, like, reply, repost or DM. One summary at the end."
)
BROWSER_UNFOLLOW_ONE_STEPS = (
    "Browser step (computerUse subagent, the bot's own browser on x.com). Unfollow exactly one account: @{handle}.\n"
    "1. Open {url}. If X shows a sign-in page or you are not signed in, do nothing else and report signed_in=false "
    "(never ask for a password in chat).\n"
    "2. Confirm the profile's @handle is exactly @{handle} (case-insensitive). If not, report profile_matches=false and "
    "stop. Then wait a random 2-5 s (one short wait), like a person reading the profile.\n"
    "3. Open the three-dot 'More' menu in the upper right of the profile header (the button with aria-label 'More', "
    "next to the main Follow/Following button). If that dropdown has 'Unfollow @{handle}', click it (path_used=menu) "
    "and go to step 5. Never click Block, Mute, Report or any other menu item.\n"
    "4. If the menu has no 'Unfollow @{handle}' item (the desktop menu usually doesn't), press Escape to close it, then "
    "read the main profile button. If it reads 'Follow', you are not following them: click nothing, report "
    "not_following=true and stop. If it reads 'Following', click it (path_used=main_button).\n"
    "5. A confirm dialog appears: act on it promptly (it can vanish) and click its 'Unfollow' button, never anything "
    "else. If the dialog vanished before you could click it, set dialog_vanished=true, reload the profile and read "
    "the main button: 'Follow' means the unfollow went through (verified_unfollowed=true, go to step 7). 'Following' "
    "means retry steps 3-5 ONCE (menu or main button, then confirm), set retries=1, then reload and check again in "
    "step 6. If it still reads 'Following' after that one retry, report ok=false, needs_owner=true and stop; never a "
    "second retry. Report retries (0 or 1) either way.\n"
    "6. Reload the profile page and read the main profile button. It must read 'Follow'. Report button_after, and "
    "verified_unfollowed=true only if it reads 'Follow'.\n"
    "7. If X shows any warning, rate-limit notice, challenge/captcha, 'Something went wrong' dialog or login wall, "
    "report stop=<reason> right away.\n"
    "Waits: " + fx.WAIT_TEXT + ".\n"
    "Never click anything else: no DM, no post, no like, no reply, no repost, no block, no mute, no report. Don't message anyone.\n"
    "Report JSON: {{signed_in, profile_matches, menu_opened, unfollow_item_found, path_used, clicked, confirmed, dialog_vanished, retries, "
    "not_following, button_after, needs_owner, ok, verified_unfollowed, stop, error}}."
)
UNFOLLOW_REPORT_KEYS = ("signed_in", "profile_matches", "menu_opened", "unfollow_item_found", "path_used", "clicked", "confirmed",
                        "dialog_vanished", "retries", "not_following", "button_after", "needs_owner", "ok", "verified_unfollowed", "stop", "error")
NEVER_CLICK_MENU = ("Block", "Mute", "Report", "About this account", "Add/remove from Lists", "View Lists",
                    "Copy link to profile")


def unfollow_task(handle: str) -> str:
    h = _h(handle)
    return BROWSER_UNFOLLOW_ONE_STEPS.format(handle=h, url=profile_link(h))


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _log(c, event: str, *, run_id=None, user_id=None, handle=None, detail=None) -> None:
    c.execute("INSERT INTO unfollow_back_log(ts,event,run_id,user_id,handle,detail) VALUES(?,?,?,?,?,?)",
              (_now(), event, run_id, None if user_id is None else str(user_id), handle,
               None if detail is None else json.dumps(detail, ensure_ascii=False, sort_keys=True)))


def _h(handle: str) -> str:
    return (handle or "").lstrip("@")


def profile_link(handle: str) -> str:
    return f"https://x.com/{_h(handle)}"


# ---------- 1. tracking ----------
def record_follow(user_key: str, user_id, handle: str, *, source: str, followed_at: str | None = None,
                  run_id: str | None = None, follows_user: bool | None = None, root=None) -> None:
    """Called for every confirmed follow made through Follower Finder. Re-following re-opens tracking.
    follows_user=False (account didn't follow the owner when followed) -> status 'awaiting_followback' (30-day window)."""
    uid, h = str(user_id), _h(handle)
    mem.check_no_secret(uid, h, source)
    at = followed_at or _now()
    stat = AWAITING if follows_user is False else "tracking"
    with mem._Tx(user_key, root) as c:
        c.execute("INSERT INTO tracked_follow(user_id,handle,followed_at,source,still_follows_user,status) "
                  "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET handle=excluded.handle, "
                  "followed_at=excluded.followed_at, source=excluded.source, status=excluded.status, "
                  "still_follows_user=excluded.still_follows_user",
                  (uid, h, at, source, None if follows_user is None else int(follows_user), stat))
        _log(c, "TRACKED_FOLLOW", run_id=run_id, user_id=uid, handle=h,
             detail={"source": source, "followed_at": at, "status": stat})


def seed_from_mutuals(user_key: str, followers: list[dict], following_ids, *, verified_only: bool = True,
                      root=None) -> list[str]:
    """Seed with verified followers the owner already follows back. followed_at unknown -> NULL (Not available)."""
    try:
        from . import verified_followers as vf
    except ImportError:  # pragma: no cover
        import verified_followers as vf  # type: ignore
    fids = {str(i) for i in following_ids}
    added = []
    with mem._Tx(user_key, root) as c:
        have = {r[0] for r in c.execute("SELECT user_id FROM tracked_follow")}
        for u in followers:
            uid = str(u.get("id"))
            if uid in have or uid not in fids or (verified_only and not vf.is_verified(u)) or not u.get("username"):
                continue
            c.execute("INSERT INTO tracked_follow(user_id,handle,followed_at,source,still_follows_user,status) "
                      "VALUES(?,?,NULL,'seed_mutual',1,'tracking')", (uid, _h(u["username"])))
            _log(c, "TRACKED_SEED", user_id=uid, handle=_h(u["username"]), detail={"source": "seed_mutual"})
            have.add(uid); added.append(uid)
    return added


def tracked(user_key: str, root=None, *, status: str | None = None) -> list[dict]:
    with mem._Tx(user_key, root) as c:
        cur = c.execute("SELECT * FROM tracked_follow" + (" WHERE status=?" if status else "") + " ORDER BY rowid",
                        (status,) if status else ())
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur]


# ---------- 2. monthly recheck ----------
def _page_all(x, tool: str) -> tuple[set[str] | None, str | None]:
    """All ids from a paged X tool, or (None, why) when the list could not be read to the end."""
    ids, token = set(), None
    for _ in range(100000):
        try:
            r = x.call(tool, {"max_results": 1000, **({"pagination_token": token} if token else {})}) or {}
        except Exception as e:  # noqa: BLE001
            return None, f"{tool} failed: {type(e).__name__}"
        if r.get("errors") and not r.get("data"):
            return None, f"{tool} returned errors"
        ids |= {str(u["id"]) for u in r.get("data") or []}
        token = (r.get("meta") or {}).get("next_token")
        if not token:
            return ids, None
    return None, f"{tool} did not finish paging"


def recheck(user_key: str, x, browser=None, *, run_id: str | None = None, root=None) -> dict:
    """Re-check every tracked account. browser (optional) has follows_you_check(users, steps) -> {user_id: bool|None}.
    Returns {'run_id','unfollowed':[rows],'still_following':[...],'not_available':[...],'owner_unfollowed':[...],'source'}."""
    run_id = run_id or dt.datetime.now(dt.timezone.utc).strftime("ub%Y%m%dT%H%M%SZ")
    rows = tracked(user_key, root, status="tracking")
    out = {"run_id": run_id, "unfollowed": [], "still_following": [], "not_available": [], "owner_unfollowed": [],
           "source": FOLLOWS_YOU_TOOL}
    if not rows:
        return out
    followers, why = _page_all(x, FOLLOWS_YOU_TOOL)
    following, _ = _page_all(x, FOLLOWING_TOOL)
    status: dict[str, bool | None] = {}
    if followers is not None:
        status = {r["user_id"]: r["user_id"] in followers for r in rows}
    else:
        out["source"] = "browser" if browser is not None else NA
        got = {}
        if browser is not None:
            got = browser.follows_you_check([{"user_id": r["user_id"], "handle": r["handle"]} for r in rows],
                                            BROWSER_FOLLOWS_YOU_CHECK_STEPS) or {}
        status = {r["user_id"]: got.get(r["user_id"]) for r in rows}
    now = _now()
    with mem._Tx(user_key, root) as c:
        for r in rows:
            uid = r["user_id"]
            if following is not None and uid not in following:
                c.execute("UPDATE tracked_follow SET status='owner_unfollowed', last_checked=? WHERE user_id=?", (now, uid))
                _log(c, "RECHECK", run_id=run_id, user_id=uid, handle=r["handle"], detail={"result": "owner_unfollowed"})
                out["owner_unfollowed"].append(r)
                continue
            s = status.get(uid)
            if s is None:
                c.execute("UPDATE tracked_follow SET last_checked=? WHERE user_id=?", (now, uid))
                _log(c, "RECHECK", run_id=run_id, user_id=uid, handle=r["handle"],
                     detail={"result": NA, "why": why or "browser could not read"})
                out["not_available"].append(r)
                continue
            new_status = "tracking" if s else "unfollowed_user"
            c.execute("UPDATE tracked_follow SET last_checked=?, still_follows_user=?, status=? WHERE user_id=?",
                      (now, 1 if s else 0, new_status, uid))
            _log(c, "RECHECK", run_id=run_id, user_id=uid, handle=r["handle"],
                 detail={"result": "still_follows" if s else "unfollowed_user", "source": out["source"]})
            (out["still_following"] if s else out["unfollowed"]).append({**r, "unfollowed_user_at": now})
    return out


def run_monthly(user_key: str, x, browser=None, *, now: dt.datetime | None = None, force: bool = False,
                root=None) -> dict | None:
    """Monthly check-in hook: runs on the 28th (or when forced). Returns recheck() + cards, or None on other days."""
    now = now or dt.datetime.now()
    if not force and now.day != MONTHLY_DAY:
        return None
    res = recheck(user_key, x, browser, root=root)
    res["report"] = report_card(res)
    res["cards"] = choice_cards(res["unfollowed"], res["run_id"])
    return res


# ---------- 3. cards ----------
def report_card(res: dict) -> dict:
    gone = res.get("unfollowed") or []
    lines = [f"@{_h(r['handle'])}: {profile_link(r['handle'])}" for r in gone]
    if gone:
        title = f"{len(gone)} account{'s' if len(gone) != 1 else ''} you follow stopped following you"
    else:
        title = "Everyone you follow through Follower Finder still follows you"
    na = res.get("not_available") or []
    if na:
        lines.append(f"Not available for {len(na)} account(s) this month (couldn't read follow status).")
    return {"title": title, "lines": lines, "accounts": [{"user_id": r["user_id"], "handle": _h(r["handle"]),
                                                          "link": profile_link(r["handle"])} for r in gone]}


def choice_cards(gone: list[dict], run_id: str) -> list[dict]:
    """Multi-select, <= 5 accounts per card, nothing pre-ticked, 'Keep following all' first."""
    cards = []
    for n, i in enumerate(range(0, len(gone), CARD_SIZE), 1):
        chunk = gone[i:i + CARD_SIZE]
        opts = [{"value": KEEP_ALL, "label": KEEP_ALL_LABEL, "checked": False,
                 "detail": "Unfollow nobody on this card"}]
        opts += [{"value": str(r["user_id"]), "label": f"Unfollow @{_h(r['handle'])}", "checked": False,
                  "detail": profile_link(r["handle"])} for r in chunk]
        cards.append({"run_id": run_id, "card_no": n, "multi_select": True,
                      "question": "Do you want to keep following these accounts? Tick the ones to unfollow.",
                      "options": opts, "account_ids": [str(r["user_id"]) for r in chunk]})
    return cards


def record_picks(user_key: str, run_id: str, card: dict, picked: list[str], root=None) -> list[str]:
    """Owner's ticks for one card. 'Keep following all' (alone or with others) means unfollow nobody on that card."""
    picked = [str(p) for p in picked]
    bad = [p for p in picked if p != KEEP_ALL and p not in card["account_ids"]]
    if bad:
        raise ValueError(f"picked ids not on card {card['card_no']}: {bad}")
    ids = [] if KEEP_ALL in picked else [p for p in card["account_ids"] if p in picked]
    with mem._Tx(user_key, root) as c:
        _log(c, "UNFOLLOW_SELECTION", run_id=run_id, detail={"card_no": card["card_no"], "picked": ids,
             "keep_all": KEEP_ALL in picked, "authorization": "OWNER_CHECKBOX"})
    return ids


def _picked(c, run_id: str) -> set[str]:
    out = set()
    for (d,) in c.execute("SELECT detail FROM unfollow_back_log WHERE event='UNFOLLOW_SELECTION' AND run_id=?", (run_id,)):
        out |= set(json.loads(d)["picked"])
    return out


# ---------- 4. unfollow + flag ----------
def execute_unfollows(user_key: str, run_id: str, picks: list[dict], browser_unfollow: Callable[[str], dict], *,
                      sleep: Callable[[float], None] = time.sleep, rand=random.uniform, flag: bool = True,
                      root=None, preflight: Callable[[dict], dict] | None = None, owner_handle: str | None = None,
                      vault_saved: bool | None = None) -> dict:
    """v3.5.33: preflight(task) runs x_web_session.preflight_task read-only first; unless the bot's x.com session is
    signed in as the owner, nothing is unfollowed (status needs_x_signin / wrong_account + 'action').
    picks: [{'user_id','handle'}]; each must be in an UNFOLLOW_SELECTION for run_id (else PermissionError, nothing done).
    browser_unfollow(handle) -> {'ok', 'verified_unfollowed', 'stop', 'error'}."""
    with mem._Tx(user_key, root) as c:
        allowed = _picked(c, run_id)
        rows = {r[0]: r for r in c.execute("SELECT user_id,handle,followed_at,last_checked FROM tracked_follow")}
    bad = [p["handle"] for p in picks if str(p["user_id"]) not in allowed]
    if bad:
        raise PermissionError(f"not ticked by the owner in {run_id}: {bad}")
    plan = build_unfollow_plan(picks)
    xs = fx.xws
    cfg = fx.owner_config(user_key, root)
    owner = owner_handle or cfg.get("owner_x_handle") or cfg.get("x_handle")
    vs = bool(cfg.get("x_login_in_vault")) if vault_saved is None else vault_saved
    if not xs.norm_handle(owner):
        d = {"action": xs.SHOW_SIGNIN_CARD, "status": xs.NEEDS_X_SIGNIN, "text": "Nothing was unfollowed: owner X handle unknown."}
        rep = {"signed_in_as": None}
    else:
        rep = xs.parse_report(preflight(xs.preflight_task(owner)) if preflight else {"signed_in": None}, owner)
        d = xs.decide(rep, owner, vault_saved=vs)
    if d["action"] != xs.PROCEED:
        stop = {"reason": xs.WRONG_ACCOUNT if d["status"] == xs.WRONG_ACCOUNT else fx.NEEDS_SIGNIN,
                "action": d["action"], "signed_in_as": rep["signed_in_as"], "at_handle": None,
                "not_attempted": [_h(x["handle"]) for x in picks]}
        with mem._Tx(user_key, root) as c:
            _log(c, "UNFOLLOW_STOPPED", run_id=run_id, detail=stop)
        return {"status": stop["reason"], "unfollowed": [], "failed": [], "stopped": stop, "plan": plan, "gaps": [],
                "text": d["text"]}
    done, failed, gaps, stop, skipped, needs_owner = [], [], [], None, [], []
    for i, p in enumerate(picks):
        if i:
            g = fx.pace_gap(i, rand); gaps.append(g); fx.sleep_chunked(g, sleep)
        r = browser_unfollow(_h(p["handle"])) or {}
        if r.get("signed_in") is False or r.get("stop") == "login_wall":
            stop = {"reason": fx.NEEDS_SIGNIN, "at_handle": _h(p["handle"]),
                    "not_attempted": [_h(x["handle"]) for x in picks[i:]]}
            with mem._Tx(user_key, root) as c:
                _log(c, "UNFOLLOW_STOPPED", run_id=run_id, detail=stop)
            break
        if r.get("not_following") or r.get("profile_matches") is False:
            why = "not_following" if r.get("not_following") else "profile_mismatch"
            with mem._Tx(user_key, root) as c:
                _log(c, "UNFOLLOW_SKIPPED", run_id=run_id, user_id=str(p["user_id"]), handle=_h(p["handle"]),
                     detail={"reason": why, "method": "browser_more_menu"})
            skipped.append({"handle": _h(p["handle"]), "reason": why})
            if r.get("stop"):
                stop = {"reason": r["stop"], "at_handle": _h(p["handle"]),
                        "not_attempted": [_h(x["handle"]) for x in picks[i + 1:]]}
                break
            continue
        after = r.get("button_after")
        ok = bool(r.get("ok")) and bool(r.get("verified_unfollowed")) and (after is None or str(after).strip().lower() == "follow")
        uid, now = str(p["user_id"]), _now()
        with mem._Tx(user_key, root) as c:
            _log(c, "UNFOLLOWED" if ok else "UNFOLLOW_FAILED", run_id=run_id, user_id=uid, handle=_h(p["handle"]),
                 detail={"error": r.get("stop") or r.get("error"), "method": "browser_more_menu",
                         "path_used": r.get("path_used"), "dialog_vanished": bool(r.get("dialog_vanished")),
                         "retries": min(int(r.get("retries") or 0), 1), "needs_owner": (not ok) and bool(r.get("needs_owner"))})
            if ok and not flag:  # 30-day window: unfollowed, never flagged
                c.execute("UPDATE tracked_follow SET status='owner_unfollowed_no_followback' WHERE user_id=?", (uid,))
            elif ok:
                t = rows.get(uid)
                c.execute("UPDATE tracked_follow SET status='owner_unfollowed_flagged' WHERE user_id=?", (uid,))
                c.execute("INSERT OR IGNORE INTO flagged_account(user_id,handle,followed_at,unfollowed_user_at,"
                          "user_unfollowed_at) VALUES(?,?,?,?,?)",
                          (uid, _h(p["handle"]), t[2] if t else None, t[3] if t else None, now))
                _log(c, "FLAGGED", run_id=run_id, user_id=uid, handle=_h(p["handle"]))
            if r.get("stop"):
                stop = {"reason": r["stop"], "at_handle": _h(p["handle"]),
                        "not_attempted": [_h(x["handle"]) for x in picks[i + 1:]]}
                _log(c, "UNFOLLOW_STOPPED", run_id=run_id, detail=stop)
        (done if ok else failed).append(_h(p["handle"]))
        if not ok and r.get("needs_owner"):
            needs_owner.append(_h(p["handle"]))
        if stop:
            break
    text = f"Unfollowed {len(done)}."
    if stop and stop["reason"] == fx.NEEDS_SIGNIN:
        text += f" Stopped: {fx.NEEDS_SIGNIN_TEXT}."
    elif stop:
        text += f" Stopped: X showed a {stop['reason'].replace('_', ' ')} at @{stop['at_handle']}."
    if failed and not stop:
        text += f" {len(failed)} didn't confirm."
    if needs_owner:
        text += " Still following after one retry, please check: " + ", ".join(f"@{h}" for h in needs_owner) + "."
    if skipped:
        text += " Skipped: " + ", ".join(f"@{k['handle']} ({k['reason'].replace('_', ' ')})" for k in skipped) + "."
    status = fx.NEEDS_SIGNIN if stop and stop["reason"] == fx.NEEDS_SIGNIN else ("stopped" if stop else "done")
    return {"status": status, "plan": plan, "unfollowed": done, "failed": failed, "skipped": skipped, "stopped": stop,
            "needs_owner": needs_owner, "gaps": gaps, "text": text}


def build_unfollow_plan(picks: list[dict]) -> list[dict]:
    """One computerUse browser task per ticked account, in order, at fx.PACING."""
    return [{"user_id": str(p["user_id"]), "handle": _h(p["handle"]), "url": profile_link(p["handle"]),
             "order": i + 1, "gap_before_s": 0 if i == 0 else list(fx.PACING["batch_break_s"] if i % fx.PACING["batch_every"] == 0
                                                                  else fx.PACING["gap_s"]),
             "wait_chunks_max_s": fx.MAX_WAIT_CHUNK_S, "steps": unfollow_task(p["handle"]),
             "report_keys": list(UNFOLLOW_REPORT_KEYS)} for i, p in enumerate(picks)]


# ---------- 5. re-follow warning gate ----------
def flagged(user_key: str, root=None) -> dict[str, dict]:
    with mem._Tx(user_key, root) as c:
        cur = c.execute("SELECT * FROM flagged_account")
        cols = [d[0] for d in cur.description]
        return {r[0]: dict(zip(cols, r)) for r in cur}


def is_flagged(user_key: str, user_id, root=None) -> bool:
    return str(user_id) in flagged(user_key, root)


def mark_card_options(cards: list[dict], flags: dict) -> list[dict]:
    """Adds FLAG_MARK + REFOLLOW_WARNING to flagged accounts on daily-review cards. Nothing else changes."""
    for c in cards:
        for o in c["options"]:
            if str(o.get("value")) in flags:
                o["flagged"] = True
                o["label"] = f"{FLAG_MARK} " + o["label"]
                o["warning"] = REFOLLOW_WARNING
    return cards


def refollow_gate_card(user_id, handle: str) -> dict:
    return {"user_id": str(user_id), "handle": _h(handle), "link": profile_link(handle),
            "title": f"{FLAG_MARK} @{_h(handle)}", "text": REFOLLOW_WARNING, "multi_select": False,
            "options": [{"value": YES, "label": YES, "checked": False}, {"value": NO, "label": NO, "checked": False}]}


def gate_skip(user_key: str, confirmations: dict | None, root=None) -> Callable[[dict], str | None]:
    """skip() for follow_executor: a flagged pick is followed only when confirmations[user_id] == 'Yes'."""
    flags = flagged(user_key, root)
    conf = {str(k): v for k, v in (confirmations or {}).items()}

    def skip(p: dict) -> str | None:
        if str(p["user_id"]) in flags and conf.get(str(p["user_id"])) != YES:
            return "flagged_refollow_not_confirmed"
        return None
    return skip


# ---------- 6. 30-day follow-back window (daily) ----------
def _parse(ts: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d if d.tzinfo else d.astimezone()


def followback_window(user_key: str, x, *, now: dt.datetime | None = None, run_id: str | None = None,
                      root=None) -> dict:
    """Daily. Accounts followed WINDOW_DAYS+ ago (status awaiting_followback) that still don't follow back.
    Reads the full follower list (get_users_me_followers) only when something is due; if it can't be read completely,
    nothing is asked today (status 'Not available'), never guessed."""
    now = now or dt.datetime.now(dt.timezone.utc)
    run_id = run_id or now.strftime("fb%Y%m%dT%H%M%SZ")
    due = [r for r in tracked(user_key, root, status=AWAITING)
           if r["followed_at"] and now - _parse(r["followed_at"]) >= dt.timedelta(days=WINDOW_DAYS)]
    out = {"run_id": run_id, "due": [], "followed_back": [], "status": "ok", "message": None, "cards": []}
    if not due:
        return out
    followers, why = _page_all(x, FOLLOWS_YOU_TOOL)
    if followers is None:
        out["status"] = NA
        return out
    ts = _now()
    with mem._Tx(user_key, root) as c:
        for r in due:
            back = r["user_id"] in followers
            c.execute("UPDATE tracked_follow SET last_checked=?, still_follows_user=?, status=? WHERE user_id=?",
                      (ts, int(back), "tracking" if back else AWAITING, r["user_id"]))
            _log(c, "FOLLOWBACK_WINDOW", run_id=run_id, user_id=r["user_id"], handle=r["handle"],
                 detail={"result": "followed_back" if back else "no_followback_30d"})
            (out["followed_back"] if back else out["due"]).append(r)
    if out["due"]:
        out["message"] = WINDOW_MESSAGE
        out["cards"] = window_cards(out["due"], run_id)
    return out


def window_cards(due: list[dict], run_id: str) -> list[dict]:
    """<= 4 accounts per card, nothing pre-ticked: 'Unfollow all', 'Keep all', then 'Unfollow @handle' (with link)."""
    cards = []
    for n, i in enumerate(range(0, len(due), WINDOW_CARD_SIZE), 1):
        chunk = due[i:i + WINDOW_CARD_SIZE]
        opts = [{"value": UNFOLLOW_ALL, "label": "Unfollow all", "checked": False,
                 "detail": f"Unfollows all {len(chunk)} accounts on this card, one at a time"},
                {"value": KEEP_ALL_WINDOW, "label": "Keep all", "checked": False,
                 "detail": "Keep following everyone on this card; you won't be asked about them again"}]
        opts += [{"value": str(r["user_id"]), "label": f"Unfollow @{_h(r['handle'])}", "checked": False,
                  "detail": profile_link(r["handle"])} for r in chunk]
        cards.append({"run_id": run_id, "card_no": n, "multi_select": True, "kind": "followback_window",
                      "options": opts, "account_ids": [str(r["user_id"]) for r in chunk],
                      "links": [profile_link(r["handle"]) for r in chunk]})
    return cards


def kept_confirmation(card: dict, kept_ids: list[str]) -> dict | None:
    """Shown when the owner keeps any non-follow-back accounts: lists them with links + KEPT_WARNING."""
    if not kept_ids:
        return None
    by = {o["value"]: o for o in card["options"]}
    accts = [{"user_id": i, "handle": by[i]["label"].replace("Unfollow @", ""), "link": by[i]["detail"]} for i in kept_ids]
    return {"title": "Keeping these accounts", "lines": [f"@{a['handle']}: {a['link']}" for a in accts],
            "text": KEPT_WARNING, "accounts": accts}


def record_window_picks(user_key: str, run_id: str, card: dict, picked: list[str], root=None,
                        *, with_confirmation: bool = False):
    """Returns ids to unfollow. Unticked accounts (or all, on 'Keep all') are marked 'kept' and never asked again.
    'Keep all' wins if ticked together with anything else (never unfollow on an ambiguous pick).
    with_confirmation=True returns (ids, kept_confirmation card or None) so the chat can show KEPT_WARNING."""
    picked = [str(p) for p in picked]
    bad = [p for p in picked if p not in (UNFOLLOW_ALL, KEEP_ALL_WINDOW) and p not in card["account_ids"]]
    if bad:
        raise ValueError(f"picked ids not on card {card['card_no']}: {bad}")
    if KEEP_ALL_WINDOW in picked:
        ids = []
    elif UNFOLLOW_ALL in picked:
        ids = list(card["account_ids"])
    else:
        ids = [i for i in card["account_ids"] if i in picked]
    kept = [i for i in card["account_ids"] if i not in ids]
    with mem._Tx(user_key, root) as c:
        _log(c, "UNFOLLOW_SELECTION", run_id=run_id, detail={"card_no": card["card_no"], "picked": ids, "kept": kept,
             "kind": "followback_window", "authorization": "OWNER_CHECKBOX"})
        for i in kept:
            c.execute("UPDATE tracked_follow SET status='kept' WHERE user_id=?", (i,))
            _log(c, "KEPT", run_id=run_id, user_id=i, detail={"warning_shown": KEPT_WARNING})
    return (ids, kept_confirmation(card, kept)) if with_confirmation else ids
