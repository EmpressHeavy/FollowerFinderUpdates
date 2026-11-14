# Changelog

## v3.5.41
- Optional tip. After 10 completed scans or sessions, and never during first-run setup, Follower Finder may ask: "Wow you seem to be using Follower Finder a lot lately! Would you mind sending my creator a tip? 🪙" It shows a QR code that is already in the app (assets/tip-qr-stripe.png, the Stripe link) plus clickable links for Stripe, Cash App, Venmo, and X Money (@HeavyMetalShip). Buttons: Maybe later, and Remind me monthly. Maybe later keeps the current cadence (at most once every 7 days until it changes). Remind me monthly, or dismissing the prompt, switches that to at most once every 30 days. There is no forever off. The prompt never blocks or delays a scan, suggestion, follow, or anything else. Follower Finder stays free.
- The QR ships in assets/, and updates copy assets/ with the app, so the bot does not download or generate it.
- Installs and updates still always take the latest VERSION on main, with no older-version menu.

## v3.5.40
- Follower Finder is now free for everyone. There is nothing to pay, and no install code or lock card anymore: scans, suggestions, follows, and other X actions no longer wait on a check.
- Removed `src/license_check.py`, `config/license.json`, and their tests. `~/follower_finder/data/install_license.json` from v3.5.38 or v3.5.39 is no longer used, and any routine step that ran the old check is dropped. Records, settings, and the X login are kept.
- Installs and updates still always take the latest VERSION on main, with no older-version menu.

## v3.5.39
- First install and updates from this repo always take the latest VERSION on main. The bot must not offer older versions, tags, or commits, or treat commit messages as a version menu.

## v3.5.38
- Paid-plan release, withdrawn in v3.5.40 (Follower Finder is free).

## v3.5.37
- Updates no longer delete per-user state. `updater.py apply` copies `app/data/`, `app/memory/` and any `app/*.db` aside (also under `user-data-backups/`) before swapping code, puts those files back byte-for-byte, and restores the previous app snapshot if the swap fails. A release that ships files under `data/` only adds paths that are not already there.
- X user ids and tweet ids in id fields (`user_id`, `id`, `ids`, `*_id`) are not treated as card numbers. A real card number in other text is still refused. A follow batch is checked before any FOLLOWED or FOLLOW_VERIFIED row is written, so a filter rejection cannot leave a half-written log.

## v3.5.36
- Suggestion history (`src/suggestion_history.py`): every account offered to you is appended to your own `suggestions/history.jsonl` (append-only, hash-chained, one owner per file). A confirmed follow and an explicit decline (Skip all) are separate states. Leaving an account unticked, or that account not following you back, is not a rejection: it can be suggested again after 30 days. Until then the same account is not suggested again and is not fetched again.
- Small accounts (1,000 followers or fewer) are judged by what they post. A low follower count is not a penalty. Spam and engagement bait are still skipped. A genuine question is not treated as bait. One research pass returns every Follow back and Maybe account, in the usual cards of at most 5, so a later run does not redo that research.
- New discovery pass (`interest_discovery.run_size_discovery`): people who match your topics and are a similar size to you, with a healthy follower/following ratio and genuine replies (substantive, not GM/GN, farming or shilling). Missing counts or replies stay "Not available" or "None found". Nothing is followed unless you pick it.

## v3.5.35
- Verified follower review and interest discovery cards only list accounts rated Follow back or Maybe. Skip accounts are still checked and remembered, so they don't keep coming back, and they are not shown or listed. If everyone is a Skip, I just say: No suitable candidates this round.
- Each card holds at most 5 of those accounts, the count is only the accounts on the cards, nothing starts ticked, and Follow all follows only the accounts on that card. A pick has to be one of those accounts. Nothing is followed unless you pick it. Interest discovery offers every Follow back and Maybe account from the profiles it already checks (up to 25), in those cards of 5.

## v3.5.34
- New installs: I set my own Follower Finder profile picture right after the update check. If it doesn't work, I'll say so and keep going; ask me anytime to set or reset it.

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
