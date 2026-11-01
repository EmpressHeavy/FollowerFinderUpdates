"""Send feedback to the creator (v3.5.0). Separate from, and independent of, the Hive Mind.

When an owner makes a feature request or complaint (for example they decline Google Drive and want a different
storage), the bot offers to pass it on. The request is ALWAYS recorded in the owner's own private memory first
(memory.log kind 'feedback'). v3.5.14: only on a clear yes does the bot send it through the Hive Mind: show the one-shot
`hive feedback --bot ff --category C "text" --dry-run` payload, and send it (same command without --dry-run) only on a
second yes. No email draft. No handles,
emails or account details are ever sent.
"""
from __future__ import annotations

import re

try:
    from . import memory
except ImportError:  # pragma: no cover
    import memory

TEMPLATE_VERSION = "v3.5.15"
SUBJECT = "Follower Finder feedback"
OFFER = ("Want me to send this to Follower Finder's creator as anonymous feedback? I'll show you exactly what will "
         "be sent first. (yes / no thanks)")


def intake_url() -> str:
    """The accepted Hive intake URL, read from the shipped client (hive/hive.py `URL`), not duplicated here."""
    import re as _re
    from pathlib import Path as _P
    m = _re.search(r"^URL = '([^']+)'", (_P(__file__).resolve().parents[1] / "hive" / "hive.py").read_text(), _re.M)
    return m.group(1) if m else ""

HIVE_KINDS = {"correction", "request", "declined", "broken", "workaround"}
YES = {"yes", "y", "yes please", "sure", "ok", "okay", "send it", "yep", "yeah"}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_HANDLE = re.compile(r"(?<![\w@])@\w{1,30}")
_URL = re.compile(r"https?://\S+")
_LONGNUM = re.compile(r"\b\d{6,}\b")


def scrub(text: str) -> str:
    """Strip emails, @handles, links and long numbers (account IDs) from the owner's request."""
    t = _EMAIL.sub("[email removed]", text or "")
    t = _URL.sub("[link removed]", t)
    t = _HANDLE.sub("[handle removed]", t)
    t = _LONGNUM.sub("[number removed]", t)
    return re.sub(r"\s+", " ", t).strip()


def record(user_key: str, request: str, *, sent: bool | None = None, root=None) -> int:
    """Always called: the request goes into the owner's own memory, whether or not it is sent."""
    state = "offered" if sent is None else ("owner said yes; hive dry run shown, sent only on a second yes" if sent
                                            else "owner said no; not sent")
    return memory.log(user_key, "feedback", f"{scrub(request)} ({state})", root=root)


def is_clear_yes(answer: str) -> bool:
    return (answer or "").strip().lower().rstrip(".!") in YES


CATEGORIES = ("bug", "request", "praise", "other")


def hive_category(category: str) -> str:
    """Owner category for the one-shot command (bug|request|praise|other); unknown -> other."""
    c = (category or "").strip().lower()
    return "bug" if c in {"bug", "broken", "error"} else (c if c in CATEGORIES else "other")


def hive_kind(category: str) -> str:
    """Kind for the always-on private record (`hive feedback add`): bug -> broken; unknown -> request."""
    c = (category or "").strip().lower()
    return "broken" if c in {"bug", "broken", "error"} else (c if c in HIVE_KINDS else "request")


def hive_steps(topic: str, did: str, wanted: str, category: str = "request") -> dict:
    """v3.5.14: the feedback step goes through the Hive Mind one-shot command (no email draft). Only that one item
    is sent, with its own daily allowance. Dry run shown verbatim, send only after a second clear yes."""
    q = lambda x: '"' + scrub(x).replace('"', "'") + '"'
    one = f"hive feedback --bot ff --category {hive_category(category)} {q(wanted)}"
    return {"record": f"hive feedback add {hive_kind(category)} {q(topic)} {q(did)} {q(wanted)}",
            "dry_run": one + " --dry-run", "send": one, "intake": intake_url(),
            "on_422": "reword without private details, dry run again, show it, then send"}


def handle(user_key: str, request: str, answer: str, *, topic: str = "feedback", did: str = "",
           category: str = "request", root=None) -> dict:
    """Record locally either way; return Hive steps only on a clear yes. Nothing is sent by this code."""
    yes = is_clear_yes(answer)
    rid = record(user_key, request, sent=True if yes else False, root=root)
    return {"memory_id": rid, "hive": hive_steps(topic, did or "built-in behavior", request, category) if yes else None}
