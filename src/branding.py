"""Product branding — rename here / in config only.

Never hard-code display names into classifier, storage, snapshot, execution,
or other core logic. Working product name may change again.
"""
from __future__ import annotations

# User-facing product / report / interface display name (current).
PRODUCT_DISPLAY_NAME = "Follower Finder"

# Alias kept for compatibility with older docs/config keys.
PRODUCT_NAME = PRODUCT_DISPLAY_NAME

# Current agent/bot display name — same as product unless config overrides.
BOT_DISPLAY_NAME = PRODUCT_DISPLAY_NAME

# Short internal package / path slug (filesystem-safe; do not rename existing folders).
PRODUCT_SLUG = "follower-finder"


def report_title() -> str:
    return f"{PRODUCT_DISPLAY_NAME} Review Report"


def agent_intro_line() -> str:
    return PRODUCT_DISPLAY_NAME
