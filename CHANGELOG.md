# Changelog

## v3.5.33
- Unfollows try the three-dot 'More' menu first. When it has no Unfollow item (the usual case on desktop), I close it and use the main 'Following' button, then confirm 'Unfollow' right away, and if that dialog disappears I reload, check, and retry once before asking you, and each one is checked by reloading the profile. If I'm not following an account, I skip it, and I never touch Block, Mute or Report. Every wait is now a few short waits, so the browser can keep up.
- Follows and unfollows are clicked in my own browser on x.com, so setup now checks that browser is signed in to YOUR X account (right after the X connector check). If it isn't, a sign-in card lets you sign in on my desktop or through a secure login form. I never ask for your password in chat.
- Recommended: save your X login to your vault so I can sign back in on my own if the session drops. A 2FA code or captcha still needs you.
- Before every follow or unfollow batch I confirm the signed-in @handle is yours. If a different account is signed in, or none is, I stop before clicking anything.

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
