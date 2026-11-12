# Onboarding and monthly check-in: engagement preference (v3.5.18)

Getting-started step (right after the interest topic cards and before the brain.db save and GitHub push):

**Engagement card (yes/no, single choice, nothing pre-selected):**
"Should I suggest accounts that mostly reply to or quote big accounts with short, popular takes, but post very little of their own?"
- Yes, include them → `preferences.set_allow_reply_heavy(user_key, True)`
- No, score them down (default) → `preferences.set_allow_reply_heavy(user_key, False)`

If the owner skips the card, leave the setting unset. Unset reads as OFF (`DEFAULTS["allow_reply_heavy"] = False`).
Save it with the interests (brain.db `fact`, subject `owner_preferences`, predicate `allow_reply_heavy`, value
`yes`/`no`), then push the memory backup to the owner's GitHub repo as usual.

Daily review: pass `preferences.get_allow_reply_heavy(user_key)` to `verified_deep_dive.deep_dive(...,
allow_reply_heavy=...)` and to `recheck_queue.process_due(...)`.

Monthly settings check-in: the settings summary also covers this preference. Pass
`allow_reply_heavy=preferences.get_allow_reply_heavy(user_key)` to `build_monthly_settings_card.build()`. That adds the
line "Reply-heavy accounts ...: INCLUDED / scored down (default)" under 6-7. If the owner taps it, show the same yes/no
card again, save the answer and push the backup.

The setting only controls the reply-heavy low-originality penalty (`DEEP_DIVE_CONFIG["reply_heavy"]`). No other
scoring changes.

## Optional situational card: possible bought or bot accounts (`show_bought_risk`)

Optional; skip it in onboarding unless the owner asks about it. Stored the same way as `allow_reply_heavy` (brain.db fact, subject `owner_preferences`).

- Question: "Show very new accounts that already have thousands of followers and a polished bio, flagged as 'Possible bought or bot account', so you can decide?"
- Choices: "Yes, show them with the flag" / "No, hide them (default)"
- Default **OFF**: flagged accounts are left off the daily card. They are never auto-skipped and nothing is recorded as a Skip.
- ON: they appear on the card with "⚠ Possible bought or bot account: joined N days ago but has M followers and a polished bio (signals)".
- Rule (`src/bought_risk.py` BOUGHT_RISK_CONFIG): account age < `max_account_age_days` (90) AND followers >= `min_followers` (2000) AND a polished bio. A polished bio has at least `bio_min_chars` (60) characters and at least `bio_min_signals` (2) of these: separators, emoji, link/@handle, title words, or multi-sentence.

## Languages you read (`read_languages`) and the browser translate step

Default `["en"]`. If most (60%+) of a candidate's posts are in another language, the daily review's box browser (signed in to X) opens up to 3 of their recent non-pinned posts. It clicks X's own **Translate post** and copies only the translated text. It never likes, replies, reposts, follows or DMs, and it stops on any X warning or login wall. The exact steps are in `translation.BROWSER_TRANSLATE_STEPS`. The card shows "Translated by X from <Language>: <text>". If nothing was captured, it shows "Translation not available for this account today."

## Daily review time (`daily_review_time`, v3.5.26)
Ask: "What time should I bring you new verified followers each day?" Default is **18:00 (6 PM) in your timezone**.
Save it with `preferences.set_daily_review_time(owner, "HH:MM")`. Then create or update the single daily routine as
described in `docs/getting_started_daily_review.md`.
