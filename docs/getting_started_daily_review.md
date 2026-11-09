# Getting started: the daily verified follower review (v3.5.36)

## During onboarding
1. Ask the owner what time they want the review. Default **6 PM (`18:00`) in the owner's own timezone**. Save it with
   `preferences.set_daily_review_time(owner_key, "HH:MM")`. `get_daily_review_time()` returns `"18:00"` when the owner
   hasn't set one.
2. Create **one daily routine** at that time in the owner's timezone (default: every day at 6 PM local), named
   "Daily verified follower review". If the owner changes the time later, update that routine. Don't make a second one.
3. Ask the situational prefs as usual (see `docs/onboarding_preferences.md`): `allow_reply_heavy` (off),
   `show_bought_risk` (off), `read_languages` (`["en"]`).

## What the routine does each day
```python
from src import daily_review as dr
out = dr.run_daily_review(owner_key, x, browser, prefs=None, now=None, calls_remaining=<X budget left today>)
# show out["cards"] in chat: multi-select, nothing pre-ticked, "Follow all on this card" first
# cards hold at most 5 accounts rated Follow back or Maybe. Skip is not on the card.
# if out["note"] is set, say that sentence and do not list accounts
# ...owner picks...
summary = dr.finish_review(owner_key, out["run_id"], {card_no: [user_id | dr.FOLLOW_ALL, ...]}, browser)
# post ONE message to the owner: summary["text"]
```
`run_daily_review` runs these steps in order:
1. followers and following
2. drive-by drop
3. blocked-by browser check and filter
4. recheck queue
5. 90-day deep dive and score, then the account health / conduct check (`src/conduct.py`), for new and rechecked accounts
6. bought-risk
7. X Translate in the browser, for Follow back and Maybe accounts
8. cards of at most 5, Follow back and Maybe only. Skip is marked seen (so it is not offered again as a new follower) and is not shown. If every reviewed account is Skip, `note` is "No suitable candidates this round." and `cards` is empty. Counts and Follow all use only the accounts on the card. Nothing is pre-ticked. `finish_review` rejects a pick that is not on that pending card; a legacy pending card that still lists a Skip does not follow that Skip.

`finish_review` then records the picks, queues inactive accounts that weren't picked for a recheck, and follows **only**
the picked accounts, one at a time at a natural human pace (about 20-45s apart, randomized, with a short break every 5). It stops on any warning, and every follow is logged append-only.

## Account health (conduct check)
Following an account links your account to how it behaves. `conduct.py` reads the posts **and** replies the deep dive
already fetched (reposts are skipped), plus the bio and `connection_status`. It makes no extra X calls and treats every
account the same way, whatever its gender and whoever it targets. Every candidate's card shows one line:
`Account health: <flags>` or `Account health: No issues found · tone positive|neutral|negative`. With no posts it says
`Account health: Not available`. When a flag is found, the one-line reason starts with `Account health: <top flag>`.

| Flag | Detects | Points (CONDUCT_CONFIG) |
|---|---|---|
| You muted this account | `connection_status` contains `muting` (or the browser mute check). If unknown, nothing is shown | -8, never Follow back |
| catfishing signals | romance-bait bio, "DM me" / WhatsApp / Telegram lures, sugar or romance-scam wording | -3 each (cap -8), 2+ signals means Skip |
| sexualized pursuit | replies chasing men or women ("hey beautiful/handsome", "send pics", "dm me babe") | -3 each (cap -8), any one means Skip |
| trolling/harassment | insults and attacks ("you're an idiot", "shut up", "kys") | -2 each (cap -6), 3+ means Skip |
| platform-rule risks | adult links (OnlyFans, NSFW, 18+), threats, follow-for-follow | -1.5 each (cap -5), 3+ means Skip |
| profanity | swear words in their own posts or replies | -0.5 each (cap -3), -3 when 30% or more of items |
| negative tone | 40% or more of items (at least 3) are hostile or negative, and they outnumber the positive ones | -2 |

The points are added to the deep-dive score, and the recommendation can only go down. Impersonation is still a hard
Skip upstream. The bio and handle hint words are reused from the Follower Finder `classify.handle_bio_screen`.

## Browser follows and pacing (v3.5.29)

The user-X plugin has no follow or unfollow tool (it only reads `get_users_me_followers` / `get_users_me_following`),
so follows really go through the bot's own browser on x.com via a computerUse subagent:
`follow_executor.build_follow_plan(picks)` gives one task per handle and `follow_executor.run_browser(...)` runs them.
Each task opens `https://x.com/<handle>`, confirms the profile matches the handle, waits a random 2-5 s, clicks
**Follow** only if the button says Follow (Following/Pending are skipped, never clicked), and never clicks anything else
(no DM, post or block). Any X warning, rate limit, challenge or "Something went wrong" dialog stops the whole batch.
Flagged accounts still need the Yes/No re-follow gate. If X isn't signed in in the bot's browser, the run returns
`needs_x_signin` ("needs X sign-in in the bot's browser (one time)"); the bot hands the desktop to the owner
(`request_box_help`) to sign in once and never asks for a password in chat.

Afterwards each follow is verified against `get_users_me_following` (or the button reading Following) and recorded as
`followed`, `pending` (protected account), `failed` or `not_available` in the append-only follow_back.jsonl. Only
verified follows are added to `tracked_follow` (30-day window and monthly unfollow-back check) and reported as done.

Pacing for every web follow and unfollow is a natural human pace (about 20-45s apart, randomized, with a short break
every 5): `follow_executor.PACING` is the one place to change it (20-45 s jittered gap, 2-5 s pre-click pause,
~2-4 min break after every 5 actions). Tests: `tests/test_browser_follow_v3529.py`.

## Adapters the routine must supply
- **X adapter**: an object with `call(tool, args) -> dict` that calls the owner's **user-X MCP** tools:
  `get_users_me`, `get_users_me_followers`, `get_users_me_following`, `get_users_posts` and `search_posts_all`.
  These are read-only.
- **Browser adapter**: each method is one **computerUse** run in the box browser signed in to the owner's X. Give it
  the steps constant verbatim:
  - `block_check(users, blocked_by.BROWSER_BLOCK_CHECK_STEPS)` returns `{user_id: True | False | None}`.
  - optional `mute_check(users, conduct.BROWSER_MUTE_CHECK_STEPS)` returns `{user_id: True | False | None}`. Only
    called for accounts X returned without `connection_status`; normally X's `connection_status: ["muting"]` answers it
    with no extra call.
  - `translate(requests, translation.BROWSER_TRANSLATE_STEPS)` returns `{user_id: [copied translated texts]}`.
  - `follow(handle, follow_executor.BROWSER_FOLLOW_STEPS)` returns `{ok, verified_following, stop, error}`.

  This step is read-only except `follow`, which is called only from `finish_review` and only for picked accounts.

Everything is owner-scoped: that owner's own `terminator.db`, `brain.db` and `verified_review/` folder.
`python -m src.daily_review --owner <key> --dry-run` prints the plan and prefs. A live run has to go through the
routine, because only the routine has the adapters.

## Unfollow-back tracker (v3.5.28, `src/unfollow_back.py`)

**What is tracked.** Every account you follow through Follower Finder (follow-backs from the daily cards, or any other
Follower Finder follow) is saved in your own `brain.db` table `tracked_follow` (handle, user id, `followed_at`, source,
`last_checked`, `still_follows_user`, status). Each daily run also seeds it with verified followers you already follow
back (you follow each other). X gives no follow date for those, so their `followed_at` stays empty ("Not available").
Every event goes to the append-only `unfollow_back_log` table. Data is per owner (test bots use their own
`~/follower_finder_test*/` data roots).

**Monthly check (28th, with the monthly check-in; `unfollow_back.run_monthly`, or `recheck()` any time).** The bot reads
your full follower list with the X plugin's `get_users_me_followers` (paged to the end). The plugin has no per-account
"follows you" or relationship tool, so the full list is what it checks against. `get_users_me_following` shows whether
you still follow them. If the list can't be read completely, the browser checks each profile for the "Follows you"
badge (`BROWSER_FOLLOWS_YOU_CHECK_STEPS`, read only). Anything still unknown is "Not available" and is never counted as
an unfollow.

**Cards.** First a report card lists who stopped following you, each with `https://x.com/<handle>`. Then come
multi-select cards with at most 5 accounts each and nothing pre-ticked. The first option is **Keep following all**,
then **Unfollow @handle** for each account. Only the ticked accounts are unfollowed, one at a time at a natural human pace (about 20-45s apart, randomized, with a short break every 5) in
the browser (`BROWSER_UNFOLLOW_STEPS`). The run stops on any X warning, and every attempt is logged. Nothing is
unfollowed without your picks.

**Flag + re-follow gate.** Every account you unfollow this way is flagged permanently in `flagged_account` (handle, user
id, `followed_at`, `unfollowed_user_at`, `user_unfollowed_at`). If it follows you again and shows up on a daily card, it
gets a ⚠️ next to its name and this warning: "This account has followed you and unfollowed you before, are you sure you
want to follow this account back?" A **Yes / No** card follows (`finish_review` returns `needs_refollow_confirmation`).
It's followed only on Yes. The flag changes nothing else and lowers no score.

## Interest discovery cards (v3.5.35)

`interest_discovery.run_discovery(..., user_key=owner_key)` uses the same card rules: at most 5 accounts, only Follow back and Maybe, nothing pre-ticked, Follow all selects only that card. One pass returns every Follow back and Maybe account from the profiles it checks, split across those cards. Skip is stored for that owner and left out of `candidates` and `cards`. If `note` is set, say it and don't list anyone. `validate_pending_selection` checks the owner's picks and does not follow. Follow those ids only through the normal follow path, at the same human pace.

## Suggestion history (v3.5.36)

Offered accounts are appended to that owner's `data/users/<user_key>/suggestions/history.jsonl`. Do not suggest the same account again while it is in that log as suggested, followed, or declined. Pass `user_key` into discovery and the daily review already logs its own cards.

- Confirmed follow: recorded when the follow is verified. Not a rejection, and not offered again while that follow stands.
- Explicit decline: the owner answered **Skip all** (or `suggestion_history.DECLINE_ALL`) on that card. That is a rejection. Do not offer the account again.
- Left unticked: `NOT_PICKED`. Not a rejection. It can come back after 30 days.
- Did not follow back: the 30-day window records `NO_FOLLOW_BACK`. Not a rejection. It can come back after 30 days. Still pass accounts you follow now in `exclude_ids`.

An empty tick list is not a decline. Say `note` when it is set, including when everyone from the last pass is still in the cooldown.

## Small accounts and size discovery (v3.5.36)

Accounts with 1,000 followers or fewer are scored from their posts. Do not skip or down-rank them for being small. Spam, crypto shilling and engagement bait still fail. A real question (enough words, not a follow-for-follow or "say hello" prompt) is not bait. If a follower count was not read, say "Not available". Do not treat it as zero.

`interest_discovery.run_size_discovery(topics, owner_followers=<count>, search=..., fetch_posts=..., fetch_replies=..., user_key=owner_key)` is the extra pass for follow-backs and friendships. It keeps accounts that match the owner's topics, are a similar size (about a quarter to four times the owner's followers), have a follower/following ratio of at least 0.2, and whose replies are substantive. GM/GN, farming and shilling replies do not qualify. `fetch_replies` returns `{"data": [...]}`. `None` from that read is "Not available". An empty list is "None found". If you do not know the owner's follower count, pass `owner_followers=None` and say the note; do not invent a number. Cards are the same: every qualified account, at most 5 per card, nothing pre-ticked, and nothing is followed unless the owner picks it. A repeat call does not fetch accounts already suggested or already checked until the cooldown ends.

## 30-day follow-back window (v3.5.28)

When Follower Finder follows an account that does **not** already follow you (for example from interest discovery,
`record_follow(..., follows_user=False)`), it's tracked with its `followed_at` as `awaiting_followback`. Each daily
review runs `followback_window()`. If any account was followed 30+ days ago and still doesn't follow you back, the bot
sends exactly:

> hey, these accounts you have followed, but they havn't followed you back in 30 days, do you want to unfollow or keep following them?

Then come multi-select cards with at most 4 accounts each, nothing pre-ticked, each with its `https://x.com/<handle>`
link. The options are **Unfollow all** first, then **Keep all**, then **Unfollow @handle**. The picks are unfollowed the
same way (one at a time at a natural human pace (about 20-45s apart, randomized, with a short break every 5), stop on any X warning, logged append-only) and are **not** flagged. The flag
is only for accounts that followed you and then stopped. Accounts you keep (Keep all or not ticked) get a confirmation
that lists them, with: "Heads up: I won't remind you about these accounts again, even if they never follow you back."
They're then marked `kept` and never asked about again. Accounts that followed you back move into the monthly
unfollow-back check. If the follower list can't be read, nothing is asked that day ("Not available").
