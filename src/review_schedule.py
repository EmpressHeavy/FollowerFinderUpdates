"""Review schedule (v3.5.31): how often the Follower Finder review routine runs.

Presets (setup card "How often would you like me to run Follower Finder?") or a custom 5-field cron the bot built
from the owner's typed answer. Time is HH:MM in the owner's time zone (default 18:00). Runs must be at least
5 minutes apart. When the schedule is less often than daily, each run covers all new followers since the last run.
"""
from __future__ import annotations

import re

PRESETS = {
    "daily": {"label": "Daily (recommended)", "dow": "*", "sentence": "every day"},
    "weekdays": {"label": "Weekdays (Mon-Fri)", "dow": "1-5", "sentence": "every weekday (Mon-Fri)"},
    "3x_week": {"label": "3 times a week (Mon/Wed/Fri)", "dow": "1,3,5", "sentence": "Mon, Wed and Fri"},
    "2x_week": {"label": "Twice a week (Tue/Fri)", "dow": "2,5", "sentence": "Tue and Fri"},
    "weekly": {"label": "Once a week (Sunday)", "dow": "0", "sentence": "every Sunday"},
}
DEFAULT = {"preset": "daily", "time": "18:00", "cron": "0 18 * * *"}
MIN_GAP_MIN = 5
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]

CARD = {"question": "How often would you like me to run Follower Finder?", "type": "single_select",
        "allowCustom": True, "options": [p["label"] for p in PRESETS.values()]}


def preset_cron(preset: str, time: str = "18:00") -> str:
    if preset not in PRESETS:
        raise ValueError("unknown preset: %r" % preset)
    m = _TIME.match((time or "").strip())
    if not m:
        raise ValueError("time must be HH:MM (24h, owner's time zone)")
    return f"{int(m.group(2))} {int(m.group(1))} * * {PRESETS[preset]['dow']}"


def _field(s: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in s.split(","):
        step = 1
        if "/" in part:
            part, st = part.split("/", 1)
            if not st.isdigit() or int(st) < 1:
                raise ValueError("bad step")
            step = int(st)
        if part == "*":
            a, b = lo, hi
        elif re.fullmatch(r"\d+-\d+", part):
            a, b = map(int, part.split("-"))
        elif part.isdigit():
            a = b = int(part)
            if step > 1:
                b = hi
        else:
            raise ValueError("bad cron field: %r" % part)
        if a < lo or b > hi or a > b:
            raise ValueError("cron value out of range")
        out.update(range(a, b + 1, step))
    return out


def parse_cron(cron: str) -> list[set[int]]:
    parts = (cron or "").split()
    if len(parts) != 5:
        raise ValueError("cron must have 5 fields: min hour day month weekday")
    f = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES)]
    if 7 in f[4]:
        f[4] = (f[4] - {7}) | {0}
    return f


def min_gap_minutes(cron: str) -> int:
    """Smallest gap between two runs within a day (cross-day gaps are always >= 1h for distinct days/hours)."""
    mins, hours = sorted(parse_cron(cron)[0]), sorted(parse_cron(cron)[1])
    times = sorted(h * 60 + m for h in hours for m in mins)
    if len(times) < 2:
        return 24 * 60
    gaps = [b - a for a, b in zip(times, times[1:])] + [times[0] + 1440 - times[-1]]
    return min(gaps)


def validate_cron(cron: str) -> str:
    cron = " ".join((cron or "").split())
    parse_cron(cron)
    if min_gap_minutes(cron) < MIN_GAP_MIN:
        raise ValueError(f"runs must be at least {MIN_GAP_MIN} minutes apart")
    return cron


def build(choice: str, time: str = "18:00", custom_cron: str | None = None, description: str | None = None) -> dict:
    """choice = preset key or 'custom' (then custom_cron is required). Returns the dict saved as review_schedule."""
    if choice == "custom":
        cron = validate_cron(custom_cron or "")
        return {"preset": "custom", "cron": cron, "time": None, "description": description or cron}
    cron = preset_cron(choice, time)
    return {"preset": choice, "cron": cron, "time": time, "description": f"{PRESETS[choice]['sentence']} at {time}"}


def is_daily_or_more(sched: dict) -> bool:
    return parse_cron(sched["cron"])[4] == set(range(0, 7))


def routine_spec(sched: dict, time_zone: str) -> dict:
    sched = sched or DEFAULT
    cron = validate_cron(sched["cron"])
    return {"name": "Follower Finder review", "cron": cron, "time_zone": time_zone or "America/Los_Angeles",
            "covers": "new followers since the last run" if not is_daily_or_more(sched) else "new followers since yesterday's run",
            "since_last_run": True}
