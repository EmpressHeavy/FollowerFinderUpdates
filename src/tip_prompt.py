"""Optional tip prompt (v3.5.41). Follower Finder stays free.

A small per-user counter lives in the existing user store:
data/users/<user_key>/tip_prompt.json (owner-only file, same folder as the rest of
that user's data, kept across updates). One completed scan or session counts as one use.

Show the prompt only after 10 uses, and never during first-run setup. The first
follow-up is at most once every 7 days. "Maybe later" leaves that cadence alone.
"Remind me monthly", or dismissing the prompt, switches it to at most once every
30 days. There is no forever off.

Nothing here blocks or delays a scan, a suggestion, a follow, or any other feature.
Recording is a local file write after the work is done. A bad file or a bad call
returns show=false instead of raising, when it goes through note_completed or the CLI.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from . import storage as st
except ImportError:  # pragma: no cover
    import storage as st

PACKAGE = Path(__file__).resolve().parents[1]
STATE_NAME = "tip_prompt.json"
QR_NAME = "tip-qr-stripe.png"
FIRST_AFTER = 10
WEEK_DAYS = 7
MONTH_DAYS = 30
PROMPT = "Wow you seem to be using Follower Finder a lot lately! Would you mind sending my creator a tip? 🪙"
LATER = "Maybe later"
MONTHLY = "Remind me monthly"
BUTTONS = (LATER, MONTHLY)
CARD_PROMPT = "It's completely optional, and Follower Finder stays free. Ask again later, or about once a month?"
RECENT_LIMIT = 40

STRIPE_URL = "https://buy.stripe.com/aFacN40Mw0Arcz02VubMQ00"
CASH_APP_URL = "https://cash.app/$EmpressHeavy"
VENMO_URL = "https://venmo.com/u/EmpressHeavy"
X_MONEY_URL = "https://x.com/HeavyMetalShip"
X_MONEY_HANDLE = "@HeavyMetalShip"

LINKS = (
    {"label": "Stripe (card)", "url": STRIPE_URL},
    {"label": "Cash App", "url": CASH_APP_URL},
    {"label": "Venmo", "url": VENMO_URL},
    {"label": "X Money (@HeavyMetalShip)", "url": X_MONEY_URL, "handle": X_MONEY_HANDLE},
)

_FIELDS = ("version", "uses", "interval_days", "last_prompt_at", "prompt_open", "last_answer",
           "answered_at", "setup_in_progress", "recent_ids", "results", "last_kind")


def qr_path() -> Path:
    """Pre-generated Stripe QR. The bot shows this file; it does not draw or download one."""
    return PACKAGE / "assets" / QR_NAME


def state_path(user_key: str, root=None) -> Path:
    return st.user_data_root(user_key, root) / STATE_NAME


def _now(now) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _blank() -> dict:
    return {
        "version": 1,
        "uses": 0,
        "interval_days": WEEK_DAYS,
        "last_prompt_at": None,
        "prompt_open": False,
        "last_answer": None,
        "answered_at": None,
        "setup_in_progress": False,
        "recent_ids": [],
        "results": {},
        "last_kind": None,
    }


def _normalize(raw) -> dict:
    """Known fields only. An unknown interval cannot become a forever silence: only 7 or 30 are kept."""
    state = _blank()
    if not isinstance(raw, dict):
        return state
    try:
        uses = int(raw.get("uses") or 0)
    except (TypeError, ValueError):
        uses = 0
    state["uses"] = uses if uses > 0 else 0
    try:
        interval = int(raw.get("interval_days") or WEEK_DAYS)
    except (TypeError, ValueError):
        interval = WEEK_DAYS
    state["interval_days"] = interval if interval in (WEEK_DAYS, MONTH_DAYS) else WEEK_DAYS
    state["last_prompt_at"] = raw.get("last_prompt_at") if _parse(raw.get("last_prompt_at")) else None
    state["answered_at"] = raw.get("answered_at") if _parse(raw.get("answered_at")) else None
    state["prompt_open"] = bool(raw.get("prompt_open"))
    answer = raw.get("last_answer")
    state["last_answer"] = answer if answer in ("later", "monthly", "dismiss") else None
    state["setup_in_progress"] = bool(raw.get("setup_in_progress"))
    ids = [str(i) for i in (raw.get("recent_ids") or []) if i][-RECENT_LIMIT:]
    state["recent_ids"] = ids
    results = raw.get("results") if isinstance(raw.get("results"), dict) else {}
    state["results"] = {k: results[k] for k in ids if k in results and isinstance(results[k], dict)}
    kind = raw.get("last_kind")
    state["last_kind"] = str(kind) if kind else None
    return state


def load_state(user_key: str, root=None) -> dict:
    path = state_path(user_key, root)
    try:
        raw = st.load_json(path, {})
    except (OSError, json.JSONDecodeError, ValueError):
        raw = {}
    return _normalize(raw)


def save_state(state: dict, user_key: str, root=None) -> Path:
    normal = _normalize(state)
    return st.save_json(state_path(user_key, root), {k: normal[k] for k in _FIELDS})


def card() -> dict:
    """Two buttons. No forever-off button. Links stay off this card; a question card cannot open a website."""
    options = [{"label": label, "value": label, "checked": False} for label in BUTTONS]
    assert len(options) <= 6
    return {"multiSelect": False, "allowCustom": False, "prompt": CARD_PROMPT, "options": options}


def links_markdown() -> str:
    return "\n".join(f"- [{item['label']}]({item['url']})" for item in LINKS)


def presentation() -> dict:
    image = qr_path()
    return {
        "prompt": PROMPT,
        "image": str(image) if image.is_file() else None,
        "image_rel": f"assets/{QR_NAME}",
        "qr_url": STRIPE_URL,
        "links": [dict(item) for item in LINKS],
        "links_markdown": links_markdown(),
        "card": card(),
        "blocks": False,
    }


def _public(state: dict, *, show: bool, reason: str, counted: bool) -> dict:
    out = {
        "show": bool(show),
        "reason": reason,
        "uses": state["uses"],
        "interval_days": state["interval_days"],
        "counted": bool(counted),
        "prompt_open": bool(state["prompt_open"]),
        "blocks": False,
    }
    if show:
        out.update(presentation())
    return out


def _waiting(state: dict, now: datetime) -> bool:
    last = _parse(state.get("last_prompt_at"))
    if last is None:
        return False
    return now - last < timedelta(days=state["interval_days"])


def _in_setup(state: dict, during_setup: bool) -> bool:
    return bool(during_setup or state.get("setup_in_progress"))


def normalize_choice(choice) -> str:
    """Map a button or a dismiss onto later / monthly / dismiss. There is no opt-out value."""
    text = " ".join(str(choice or "").strip().lower().replace("'", "").replace("’", "").split())
    if text in ("later", "maybe later", "not now", "not right now", "ask later"):
        return "later"
    if text in ("monthly", "remind me monthly", "remind monthly", "once a month", "monthly please"):
        return "monthly"
    if text in ("dismiss", "dismissed", "closed", "close", "ignored", "ignore", "no thanks",
                "no thank you", "dont ask again", "dont ask", "stop asking", "never", "go away"):
        return "dismiss"
    raise ValueError("pick Maybe later or Remind me monthly")


def record_use(user_key: str, *, root=None, now=None, during_setup: bool = False,
               run_id: str | None = None, kind: str = "scan") -> dict:
    """Count one completed scan or session and say whether to show the tip.

    Same run_id twice does not count twice and does not change the cadence.
    During first-run setup the counter does not move and the prompt is not shown.
    An open prompt with no answer is a dismiss: the cadence becomes 30 days.
    """
    moment = _now(now)
    state = load_state(user_key, root)
    if _in_setup(state, during_setup):
        return _public(state, show=False, reason="setup", counted=False)
    token = str(run_id) if run_id else ""
    if token and token in state["recent_ids"]:
        cached = state["results"].get(token)
        if isinstance(cached, dict) and "show" in cached:
            # The first call can still say show, so a retry can display it. Once they answer,
            # the same run must not ask again.
            if cached.get("show") and not state["prompt_open"]:
                return _public(state, show=False, reason="replay", counted=False)
            return cached
        return _public(state, show=False, reason="replay", counted=False)
    if state["prompt_open"]:
        state["interval_days"] = MONTH_DAYS
        state["prompt_open"] = False
        state["last_answer"] = "dismiss"
        state["answered_at"] = _iso(moment)
    state["uses"] += 1
    state["last_kind"] = str(kind or "scan")
    if token:
        state["recent_ids"] = (state["recent_ids"] + [token])[-RECENT_LIMIT:]
    if state["uses"] < FIRST_AFTER:
        show, reason = False, "not_yet"
    elif _waiting(state, moment):
        show, reason = False, "waiting"
    else:
        show, reason = True, "due"
        state["last_prompt_at"] = _iso(moment)
        state["prompt_open"] = True
        state["last_answer"] = None
        state["answered_at"] = None
    public = _public(state, show=show, reason=reason, counted=True)
    if token:
        state["results"][token] = public
        state["results"] = {k: state["results"][k] for k in state["recent_ids"] if k in state["results"]}
    save_state(state, user_key, root)
    return public


def answer(user_key: str, choice, *, root=None, now=None) -> dict:
    """Record a button or a dismiss. Maybe later keeps the current cadence. Monthly or dismiss sets 30 days."""
    kind = normalize_choice(choice)
    moment = _now(now)
    state = load_state(user_key, root)
    if kind in ("monthly", "dismiss"):
        state["interval_days"] = MONTH_DAYS
    state["prompt_open"] = False
    state["last_answer"] = kind
    state["answered_at"] = _iso(moment)
    save_state(state, user_key, root)
    out = _public(state, show=False, reason="answered", counted=False)
    out["answer"] = kind
    return out


def begin_setup(user_key: str, *, root=None) -> dict:
    """Mark first-run setup in progress. Does not reset the counter and does not show a prompt."""
    state = load_state(user_key, root)
    state["setup_in_progress"] = True
    save_state(state, user_key, root)
    return _public(state, show=False, reason="setup", counted=False)


def finish_setup(user_key: str, *, root=None) -> dict:
    """First-run setup is over. Later completed scans may count. Still no prompt from this call."""
    state = load_state(user_key, root)
    state["setup_in_progress"] = False
    save_state(state, user_key, root)
    return _public(state, show=False, reason="ready", counted=False)


def note_completed(user_key: str, *, root=None, now=None, run_id: str | None = None, kind: str = "scan") -> dict:
    """record_use that cannot raise. A failure leaves the feature's result alone."""
    try:
        return record_use(user_key, root=root, now=now, run_id=run_id, kind=kind)
    except Exception:
        return {"show": False, "reason": "error", "counted": False, "blocks": False}


def attach(result: dict, user_key: str, *, root=None, now=None, kind: str = "scan") -> dict:
    """Add result['tip'] after a completed scan or session. Never raises and never removes other keys."""
    if isinstance(result, dict) and user_key:
        result["tip"] = note_completed(user_key, root=root, now=now, run_id=result.get("run_id"), kind=kind)
    return result


def _flag(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    i = args.index(name)
    if i + 1 >= len(args):
        raise SystemExit(2)
    value = args[i + 1]
    del args[i:i + 2]
    return value


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print("usage: tip_prompt.py record|answer|begin-setup|finish-setup "
              "--user KEY [--root DIR] [--run-id ID] [--kind scan] [--choice later|monthly|dismiss] "
              "[--during-setup] [--now ISO]")
        return 0 if args else 2
    cmd = args.pop(0)
    try:
        user = _flag(args, "--user")
        root = _flag(args, "--root")
        run_id = _flag(args, "--run-id")
        kind = _flag(args, "--kind") or "scan"
        choice = _flag(args, "--choice")
        now_s = _flag(args, "--now")
        during = "--during-setup" in args
        if not user:
            print(json.dumps({"show": False, "reason": "error", "blocks": False, "error": "user key required"}))
            return 0
        moment = _parse(now_s) if now_s else None
        if cmd == "record":
            out = record_use(user, root=root, now=moment, during_setup=during, run_id=run_id, kind=kind)
        elif cmd == "answer":
            out = answer(user, choice, root=root, now=moment)
        elif cmd == "begin-setup":
            out = begin_setup(user, root=root)
        elif cmd == "finish-setup":
            out = finish_setup(user, root=root)
        else:
            print(json.dumps({"show": False, "reason": "error", "blocks": False, "error": "unknown command"}))
            return 2
    except Exception as exc:
        print(json.dumps({"show": False, "reason": "error", "blocks": False, "error": str(exc)[:200]}))
        return 0
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
