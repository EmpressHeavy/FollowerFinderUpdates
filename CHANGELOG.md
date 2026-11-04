# Changelog

## v3.5.32
- Follow-backs: a fixed contract with the browser helper. One account at a time, it clicks only "Follow", reports back in a set format, and the run stops at any sign-in page or X warning. A follow only counts once X shows it in your following list (`follow_executor.FollowSession`).
- No daily removal limit by default. You still pick every removal, cards show at most 5, and runs stop at any X warning or when you say stop. You can set your own limit at setup, from the monthly check-in, or anytime (`preferences.set_limits`).
- Inactive mutuals: no fixed 3 per day anymore. They come in cards of up to 5, and you can set a limit if you want one.
- Language detection works without extra packages (built-in detector; `langdetect` is used only if it's installed).
- New interest discovery (`src/interest_discovery.py`): learns your topics from your posts and bio, lets you confirm them on a card, searches X for people posting about them, and filters out low-quality and likely bought or bot accounts.
- Setup steps are now code (`src/setup_flow.py`): the update check runs right after the welcome card, the routines are built from code (review schedule, monthly check-in on the 28th at 10 AM, weekly update check Sunday 10 AM), typed schedules are repeated back to you to confirm before they're saved, plus backup restore and update messages.
- Removed the old unused `recommend()` scorer.

## v3.5.31
- New setup card "How often would you like me to run Follower Finder?": Daily, Weekdays, 3 times a week, Twice a week, Once a week, or type your own schedule. Change it anytime by asking or from the monthly check-in.
- Less-than-daily reviews cover every new follower since the last run.
- No cap on how many accounts get reviewed per run; it keeps going in cards of 5 until done, an X warning or rate limit, or you say stop.

## v3.5.30
- Self-updater (`src/updater.py`): checks for a newer build right after the welcome card and weekly, applies it safely with rollback.

## v3.5.29.1
- Daily review and unfollows use the browser plan; clearer sign-in status for follows and unfollows.
