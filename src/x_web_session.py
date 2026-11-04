"""v3.5.33: the bot's own browser session on x.com (web, NOT the X connector).

Follow / unfollow clicks happen in the bot's browser on x.com, so that browser must be signed in to the OWNER'S
X account. Pure logic only (no browser, no secrets):

    task   = preflight_task(owner_handle)              # one computerUse subagent reads the signed-in @handle
    report = parse_report(<subagent reply>)            # {'status': signed_in_as|needs_x_signin|wrong_account|...}
    act    = decide(report, owner_handle, vault_saved=..., relogin_attempts=...)
    act['action'] in PROCEED / AUTO_RELOGIN / SHOW_SIGNIN_CARD / STOP_WRONG_ACCOUNT

Rules: no click before a matching preflight; a mismatch or sign-out stops the batch with nothing clicked. Passwords
are never asked for in chat and never printed: sign-in is the host's desktop hand-off (request_box_help) or a
RequestUserForm login form, and a vault-saved x.com login is filled with FillCredential. 2FA codes and captchas
always need the owner."""
from __future__ import annotations

import json
import re

X_HOME = "https://x.com/home"
X_LOGIN = "https://x.com/i/flow/login"
VAULT_SITE = "x.com"

# report statuses
SIGNED_IN_AS = "signed_in_as"
NEEDS_X_SIGNIN = "needs_x_signin"
WRONG_ACCOUNT = "wrong_account"
CHALLENGE = "challenge"      # 2FA code / captcha / "confirm it's you" -> owner must act
UNREADABLE = "unreadable"    # signed in but the @handle could not be read
STATUSES = (SIGNED_IN_AS, NEEDS_X_SIGNIN, WRONG_ACCOUNT, CHALLENGE, UNREADABLE)

# next actions for the parent bot
PROCEED = "proceed"
AUTO_RELOGIN = "auto_relogin_with_vault"
SHOW_SIGNIN_CARD = "show_signin_card"
STOP_WRONG_ACCOUNT = "stop_wrong_account"
ACTIONS = (PROCEED, AUTO_RELOGIN, SHOW_SIGNIN_CARD, STOP_WRONG_ACCOUNT)
MAX_AUTO_RELOGIN = 1

_HANDLE = re.compile(r"@?([A-Za-z0-9_]{1,15})")


def norm_handle(h) -> str | None:
    if not h:
        return None
    m = _HANDLE.fullmatch(str(h).strip())
    return m.group(1).lower() if m else None


def same_account(a, b) -> bool:
    na, nb = norm_handle(a), norm_handle(b)
    return bool(na) and na == nb


def preflight_task(owner_handle: str) -> dict:
    """Browser-subagent task text: read-only, no clicks on any account."""
    h = norm_handle(owner_handle)
    if not h:
        raise ValueError("owner X handle is required for the x.com session check")
    steps = (
        f"In the bot's own browser open {X_HOME}. Do not click Follow, Unfollow, or any button on any account.",
        "If you see a login page, 'Sign in' / 'Log in' buttons, or are redirected to /i/flow/login, report signed_in=false.",
        "If X asks for a verification code, captcha, or 'confirm it's you', report challenge=true (do not try to solve it).",
        "Otherwise read the signed-in @handle: the left-nav Profile link (href https://x.com/<handle>) or the "
        "account switcher at the bottom-left. Report it exactly.",
        'Reply with JSON only: {"signed_in": true|false, "signed_in_as": "@handle" or null, "challenge": true|false, '
        '"error": null or short text}. Never type, read out, or report any password or code.',
    )
    return {"kind": "x_preflight", "url": X_HOME, "expect_handle": "@" + h, "steps": "\n".join(steps),
            "read_only": True}


def _boolish(v):
    if isinstance(v, bool) or v is None:
        return v
    s = str(v).strip().lower()
    return True if s in ("true", "yes", "1", "y") else False if s in ("false", "no", "0", "n") else None


def parse_report(raw, owner_handle: str | None = None) -> dict:
    """Subagent reply (dict, JSON text, or loose text) -> {'status','signed_in_as','signed_in','challenge','error'}."""
    d: dict = {}
    if isinstance(raw, dict):
        d = dict(raw)
    elif raw:
        txt = str(raw)
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
            except ValueError:
                d = {}
        if not d:
            low = txt.lower()
            if re.search(r"captcha|verification code|confirm it'?s you|2fa", low):
                d["challenge"] = True
            if re.search(r"signed out|not signed in|log ?in page|sign in to x|login wall", low):
                d["signed_in"] = False
            hm = re.search(r"signed in as\s+@?([A-Za-z0-9_]{1,15})", txt, re.I)
            if hm:
                d["signed_in_as"] = hm.group(1)
                d.setdefault("signed_in", True)
    signed_in, challenge = _boolish(d.get("signed_in")), bool(_boolish(d.get("challenge")))
    if str(d.get("status", "")).lower() == NEEDS_X_SIGNIN or d.get("stop") == "login_wall":
        signed_in = False
    who = norm_handle(d.get("signed_in_as") or d.get("handle"))
    if challenge:
        status = CHALLENGE
    elif signed_in is False:
        status, who = NEEDS_X_SIGNIN, None
    elif not who:
        status = UNREADABLE
    elif owner_handle is not None and not same_account(who, owner_handle):
        status = WRONG_ACCOUNT
    else:
        status = SIGNED_IN_AS
    return {"status": status, "signed_in_as": "@" + who if who else None, "signed_in": status not in (NEEDS_X_SIGNIN,),
            "challenge": challenge, "error": d.get("error")}


def decide(report, owner_handle: str, *, vault_saved: bool = False, relogin_attempts: int = 0) -> dict:
    """-> {'action', 'status', 'reason', 'text'}. Only PROCEED allows clicks."""
    r = report if isinstance(report, dict) and report.get("status") in STATUSES else parse_report(report, owner_handle)
    if r["status"] == SIGNED_IN_AS and not same_account(r["signed_in_as"], owner_handle):
        r = {**r, "status": WRONG_ACCOUNT}
    st, owner = r["status"], "@" + (norm_handle(owner_handle) or "?")
    if st == SIGNED_IN_AS:
        return {"action": PROCEED, "status": st, "reason": None, "text": f"Signed in on x.com as {r['signed_in_as']}."}
    if st == WRONG_ACCOUNT:
        return {"action": STOP_WRONG_ACCOUNT, "status": st, "reason": "wrong_account",
                "text": f"The bot's browser is signed in to x.com as {r['signed_in_as']}, not {owner}. Nothing was "
                        f"clicked. Sign out there and sign in as {owner} (sign-in card)."}
    if st == NEEDS_X_SIGNIN and vault_saved and relogin_attempts < MAX_AUTO_RELOGIN:
        return {"action": AUTO_RELOGIN, "status": st, "reason": "signed_out",
                "text": f"x.com session dropped; signing back in as {owner} with the saved login, then re-checking."}
    why = {CHALLENGE: "X is asking for a code or captcha, which needs you",
           UNREADABLE: "couldn't confirm which account is signed in",
           NEEDS_X_SIGNIN: "the bot's browser isn't signed in to x.com"}[st]
    return {"action": SHOW_SIGNIN_CARD, "status": st, "reason": st,
            "text": f"Nothing was clicked: {why}. Please sign in as {owner} using the sign-in card."}


SIGNIN_CARD = {
    "id": "x_web_signin",
    "title": "Sign in to X in my browser",
    "body": ("I follow and unfollow on x.com in my own browser (not the X connector), so it needs to be signed in to "
             "YOUR X account. Pick how you'd like to sign in. I never ask for your password in chat."),
    "options": [
        {"id": "desktop", "label": "Sign in myself on the bot's desktop",
         "how": "request_box_help hand-off at https://x.com/i/flow/login"},
        {"id": "form", "label": "Use a secure login form",
         "how": "RequestUserForm: username + password, then a 2FA code field only if X asks"},
        {"id": "later", "label": "Later (no follows/unfollows until signed in)"},
    ],
}

VAULT_OFFER_CARD = {
    "id": "x_vault_offer",
    "title": "Save your X login to your vault? (recommended)",
    "body": ("If my x.com session drops, I can sign back in on my own with FillCredential instead of asking you "
             "each time. The password stays in your vault; I never see or print it. 2FA codes and captchas still "
             "need you."),
    "options": [{"id": "save", "label": "Yes, save my x.com login"},
                {"id": "no", "label": "No, show me the sign-in card when needed"}],
}


def setup_plan(report, owner_handle: str, *, vault_saved: bool = False) -> dict:
    """Setup step x_web_session: what to show next. Signed in as owner -> offer vault (if not saved) -> done."""
    d = decide(report, owner_handle, vault_saved=vault_saved)
    cards = []
    if d["action"] in (SHOW_SIGNIN_CARD, STOP_WRONG_ACCOUNT, AUTO_RELOGIN):
        cards.append(SIGNIN_CARD if d["action"] != AUTO_RELOGIN else None)
    if d["action"] == PROCEED and not vault_saved:
        cards.append(VAULT_OFFER_CARD)
    return {**d, "cards": [c for c in cards if c], "done": d["action"] == PROCEED}
