"""Owner preferences stored next to the interest picks (v3.5.18).

Interests from the getting-started topic cards live as facts in the owner's own memory DB
(data/users/<user_key>/memory/brain.db, table `fact`), which memory.export() writes to fact.jsonl and
memory.push_backup() pushes to the owner's private GitHub repo. Preferences use the same table:
  subject = "owner_preferences", predicate = "allow_reply_heavy", value = "yes" | "no" (bitemporal; a change
  closes the old row). Missing -> the default.

allow_reply_heavy (default OFF): when OFF the deep dive scores down accounts that post almost nothing original and
constantly reply to / quote big accounts with short takes that collect many likes (DEEP_DIVE_CONFIG["reply_heavy"]).
When ON that pattern is not penalised. It changes nothing else.
"""
from __future__ import annotations

try:
    from . import memory as mem
except ImportError:  # top-level import
    import memory as mem

SUBJECT = "owner_preferences"
ALLOW_REPLY_HEAVY = "allow_reply_heavy"
READ_LANGUAGES = "read_languages"
SHOW_BOUGHT_RISK = "show_bought_risk"
DEFAULTS = {ALLOW_REPLY_HEAVY: False, READ_LANGUAGES: ["en"], SHOW_BOUGHT_RISK: False}
CARD_STEP = {
    "pref": ALLOW_REPLY_HEAVY,
    "question": ("Should I suggest accounts that mostly reply to or quote big accounts with short, popular takes, "
                 "but post very little of their own?"),
    "choices": ["Yes, include them", "No, score them down (default)"],
    "multi_select": False,
}


def get_allow_reply_heavy(user_key: str, root=None) -> bool:
    _, rows = mem.q(user_key, "SELECT value FROM fact WHERE subject=? AND predicate=? AND valid_to IS NULL "
                              "ORDER BY id DESC LIMIT 1", (SUBJECT, ALLOW_REPLY_HEAVY), root=root)
    return rows[0][0] == "yes" if rows else DEFAULTS[ALLOW_REPLY_HEAVY]


def set_allow_reply_heavy(user_key: str, on: bool, *, source: str = "owner card", root=None) -> dict:
    return mem.fact(user_key, SUBJECT, ALLOW_REPLY_HEAVY, "yes" if on else "no", source=source, root=root)


def from_card_answer(answer: str) -> bool:
    return answer.strip().lower().startswith("yes")


def status_line(on: bool | None) -> str | None:
    if on is None:
        return None
    return ("Reply-heavy accounts (short popular replies/quotes to big accounts, few originals): "
            + ("INCLUDED" if on else "scored down (default)") + ". Tap 6-7 to change.")


def get_read_languages(user_key: str, root=None) -> list[str]:
    """Languages the owner reads (ISO 639-1, e.g. ['en','es']); stored as a JSON list. Default ['en']."""
    import json
    _, rows = mem.q(user_key, "SELECT value FROM fact WHERE subject=? AND predicate=? AND valid_to IS NULL "
                              "ORDER BY id DESC LIMIT 1", (SUBJECT, READ_LANGUAGES), root=root)
    try:
        v = json.loads(rows[0][0]) if rows else None
    except ValueError:
        v = None
    return v if isinstance(v, list) and v else list(DEFAULTS[READ_LANGUAGES])


def set_read_languages(user_key: str, langs, *, source: str = "owner card", root=None) -> dict:
    import json
    clean = sorted({str(x).strip().lower().split("-")[0] for x in langs if str(x).strip()}) or ["en"]
    return mem.fact(user_key, SUBJECT, READ_LANGUAGES, json.dumps(clean), source=source, root=root)


def _get_bool(user_key, key, root=None) -> bool:
    _, rows = mem.q(user_key, "SELECT value FROM fact WHERE subject=? AND predicate=? AND valid_to IS NULL "
                              "ORDER BY id DESC LIMIT 1", (SUBJECT, key), root=root)
    return rows[0][0] == "yes" if rows else DEFAULTS[key]


def get_show_bought_risk(user_key: str, root=None) -> bool:
    """Situational: OFF (default) hides possible bought/bot accounts from the card; ON shows them with a flag."""
    return _get_bool(user_key, SHOW_BOUGHT_RISK, root)


def set_show_bought_risk(user_key: str, on: bool, *, source: str = "owner card", root=None) -> dict:
    return mem.fact(user_key, SUBJECT, SHOW_BOUGHT_RISK, "yes" if on else "no", source=source, root=root)


BOUGHT_RISK_CARD_STEP = {
    "pref": SHOW_BOUGHT_RISK,
    "question": ("Show very new accounts that already have thousands of followers and a polished bio, flagged as "
                 "'Possible bought or bot account', so you can decide?"),
    "choices": ["Yes, show them with the flag", "No, hide them (default)"],
    "multi_select": False,
}

READ_LANGUAGES_CARD_STEP = {
    "pref": READ_LANGUAGES,
    "question": ("Which languages do you read? Candidates who mostly post in other languages get a short "
                 "summary translated by X's own Translate post."),
    "choices": ["English only (default)", "Add languages (reply with them, e.g. en, es, ja)"],
    "multi_select": False,
}

# Situational toggle registry: add an entry here and it shows up in the monthly 'Situational settings' section.
SITUATIONAL = [
    {"key": ALLOW_REPLY_HEAVY, "label": "Reply-heavy accounts", "get": lambda uk, root=None: get_allow_reply_heavy(uk, root),
     "fmt": lambda v: "included" if v else "scored down (default)", "card": CARD_STEP},
    {"key": SHOW_BOUGHT_RISK, "label": "Possible bought or bot accounts", "get": lambda uk, root=None: get_show_bought_risk(uk, root),
     "fmt": lambda v: "shown with a flag" if v else "hidden (default)", "card": BOUGHT_RISK_CARD_STEP},
    {"key": READ_LANGUAGES, "label": "Languages you read", "get": lambda uk, root=None: get_read_languages(uk, root),
     "fmt": lambda v: ", ".join(v) + (" (default)" if v == DEFAULTS[READ_LANGUAGES] else ""), "card": READ_LANGUAGES_CARD_STEP},
]


def situational_values(user_key: str, root=None) -> dict:
    return {e["key"]: e["get"](user_key, root=root) for e in SITUATIONAL}


def situational_card(key: str) -> dict:
    """The card to re-ask when the owner taps a situational toggle in the monthly review."""
    return next(e["card"] for e in SITUATIONAL if e["key"] == key)


DAILY_REVIEW_TIME = "daily_review_time"
DEFAULTS[DAILY_REVIEW_TIME] = "18:00"


def get_daily_review_time(user_key: str, root=None) -> str:
    """HH:MM in the OWNER's own timezone (default 18:00 = 6 PM). Used by the daily routine and the onboarding docs."""
    _, rows = mem.q(user_key, "SELECT value FROM fact WHERE subject=? AND predicate=? AND valid_to IS NULL "
                              "ORDER BY id DESC LIMIT 1", (SUBJECT, DAILY_REVIEW_TIME), root=root)
    return rows[0][0] if rows else DEFAULTS[DAILY_REVIEW_TIME]


def set_daily_review_time(user_key: str, hhmm: str, *, source: str = "owner card", root=None) -> dict:
    import re
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hhmm or ""):
        raise ValueError("daily_review_time must be HH:MM (24h, owner's timezone)")
    return mem.fact(user_key, SUBJECT, DAILY_REVIEW_TIME, hhmm, source=source, root=root)


REVIEW_SCHEDULE = "review_schedule"


def get_review_schedule(user_key: str, root=None) -> dict:
    """Saved review schedule {preset, cron, time, description}; falls back to daily at daily_review_time."""
    import json
    try:
        from . import review_schedule as rs
    except ImportError:
        import review_schedule as rs
    _, rows = mem.q(user_key, "SELECT value FROM fact WHERE subject=? AND predicate=? AND valid_to IS NULL "
                              "ORDER BY id DESC LIMIT 1", (SUBJECT, REVIEW_SCHEDULE), root=root)
    if rows:
        return json.loads(rows[0][0])
    return rs.build("daily", get_daily_review_time(user_key, root=root))


def set_review_schedule(user_key: str, sched: dict, *, source: str = "owner card", root=None) -> dict:
    import json
    try:
        from . import review_schedule as rs
    except ImportError:
        import review_schedule as rs
    rs.validate_cron(sched.get("cron", ""))
    return mem.fact(user_key, SUBJECT, REVIEW_SCHEDULE, json.dumps(sched, sort_keys=True), source=source, root=root)


# v3.5.32: optional daily limits. Default = no limit (None). Removals still need explicit picks, run in cards of up
# to 5 and stop on any X warning or when the owner says stop. A limit the owner sets is written to their config.json
# (where the runners read it) and remembered as a fact.
LIMITS = "daily_limits"
LIMIT_KEYS = ("daily_removal_cap", "inactive_mutuals_per_day")
LIMITS_CARD = {
    "question": ("Want a daily limit? Default is no limit: you still pick every removal, I show at most 5 per card "
                 "and stop at the first X warning. Type a number (1-100) for removals per day, or 'no limit'."),
    "choices": ["No limit (default)", "Set a removals-per-day limit", "Set an inactive-mutuals-per-day limit"],
    "multi_select": False,
}


def get_limits(cfg: dict) -> dict:
    try:
        from . import removal_runner as rr
    except ImportError:
        import removal_runner as rr
    out = {}
    for k in LIMIT_KEYS:
        try:
            out[k] = rr.parse_limit(cfg.get(k))
        except ValueError:
            out[k] = None
    return out


def limits_line(cfg: dict | None) -> str:
    lim = get_limits(cfg or {})
    r, m = lim["daily_removal_cap"], lim["inactive_mutuals_per_day"]
    return ("Daily limits: removals " + ("no limit" if r is None else f"{r}/day") +
            ", inactive mutuals " + ("no limit (cards of 5)" if m is None else f"{m}/day") +
            ". Tap to set or clear a limit.")


def set_limits(user_key: str, *, root=None, source: str = "owner card", **limits) -> dict:
    """set_limits(uk, daily_removal_cap=20) / (inactive_mutuals_per_day='no limit'). Validates, writes config.json."""
    import json
    try:
        from . import removal_runner as rr
        from . import storage as st
    except ImportError:
        import removal_runner as rr
        import storage as st
    bad = set(limits) - set(LIMIT_KEYS)
    if bad:
        raise ValueError(f"unknown limit(s): {sorted(bad)}")
    clean = {k: rr.parse_limit(v) for k, v in limits.items()}
    p = st.user_config_path(root, user_key=user_key)
    if p is None:
        raise ValueError("no config for this user")
    cfg = st.load_user_config_dict(root, user_key=user_key)
    cfg.update(clean)
    st.save_json(p, cfg)
    try:
        st.apply_user_permissions(user_key, root)
    except Exception:
        pass
    try:
        mem.fact(user_key, SUBJECT, LIMITS, json.dumps(get_limits(cfg), sort_keys=True), source=source, root=root)
    except Exception:  # memory is best-effort; config.json is the source of truth
        pass
    return get_limits(cfg)
