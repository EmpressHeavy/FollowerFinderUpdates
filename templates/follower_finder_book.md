# Follower Finder Book

Your rules for Follower Finder. I read this before every scan and every removal, and I update it (with a Change log line) whenever you change a rule.

## Job
Find bot and spam followers on {handle} and remove them as followers, only after you mark them on a card. Removing a follower does not block or unfollow anyone.

## Always kept
- Accounts you follow (mutuals). Inactive mutuals are only shown on their own card ({mutuals_per_day}), never removed automatically.
- Verified accounts.
- Anyone who has interacted with you recently (I search X both ways right before each removal).
- Your Keepers ("Not a bot" / "Always Keep") and protected accounts: {protected}

## How bots are judged
1. Handle and bio are a first-pass screen only. A number-style handle is a hint, never proof.
2. Real replies to other people: a real person, kept.
3. The numbers: no or almost no posts, very few followers and high following are bot signs; real or verified followers mean a real person.
4. Only accounts that fail both 2 and 3 go on a removal card. Possible impersonators (of your friends, or celebrity "manager" style accounts) are flagged on the card.
Every card shows posts, followers and following.

## Lists
List checks: {lists} (off by default; you choose at setup and can change it in the monthly check-in). When on, before each check you choose "Only lists from people I don't follow back" or "Show all lists", and that run sticks to it. I open every list page to read its link, description, owner and members. Only really negative lists (harassment, hate, spam or bot rings, scams, blocklists, mocking or targeting) are flagged; playful or plain names (and "Grok Bot", a product) are fine, unclear ones are watched, and lists from people you follow each other with are watched, never flagged. You review every list 4 at a time: select the ones to leave and tap "Remove me from these lists", or tap "Keep all these lists". Lists you didn't select are skipped for now and come back on one last card (4 at a time if there are many): keep them all, or select ones and tap "Remove me from selected lists"; any you leave unselected stay in review for next time. Tapping a Remove button is your OK to act right away (in Manual Mode I ask for a yes first). Each chosen list is left one at a time, about 10 seconds apart: I open the owner's profile, Block, confirm, wait about 10 seconds, Unblock, confirm (a soft block: you're off the list and the owner is not left blocked), then check your lists-you're-on page (x.com/<you>/lists/memberships) to confirm the list is gone. Each list counts 2 toward your daily cap. I never touch owners you already blocked, owners on Always Keep or Protected, or lists you already left. If you already follow a list's owner, the card warns you (leaving also unfollows them). I stop for the day at any X warning or rate limit. Every list is written to the List Graveyard tab the moment its removal finishes, with the verified result; a failed or unverified one is written with what actually happened. A line goes into the Change log below too. Kept lists go on the Kept Lists tab and aren't shown again unless the owner or name changes. Lists whose owner you follow go on the Protected Lists tab and aren't re-checked, unless you unfollow the owner. Owners who add you again show up on a repeat-offender card (or are blocked automatically if you chose auto-block for returnees). The List Graveyard tab is also where returning owners are checked.

## Approval and pace
- Cards in groups of 5. Mark the ones to remove, or "Mark all for termination". Unmarked accounts will be kept in review.
- Daily removal limit: {daily_cap} (optional; max 100 when set).
- One removal run at a time. Each removal is confirmed on X before it's logged.

## Where things live
- This Book (Google Doc).
- Log sheet "{sheet_title}" with tabs Actions, Keepers, In review, List Graveyard, Kept Lists and Protected Lists.
- Private memory: data/users/<your key>/memory/brain.db on your own bot's computer (yours alone; never holds passwords, tokens or keys). Backup to a private GitHub repo you own: {memory_backup}

## Routines
- Daily scan at {scan_time} ({time_zone}): one card of likely bots, one card of a few inactive mutuals, and (if list checks are on) a rescan of the lists you're on.
- Reminder through your front-door bot: {reminder}
- Terminate reminder (separate from the front-door reminder; the bot's own daily question in chat, "Hey, want to terminate some bots?" with Yes / Remind me later / Not today): {terminate_reminder}
- Memory: the bot runs `mem boot "Follower Finder"` at the start of every session, checks `mem lessons` before a task, uses `mem supersede` instead of adding duplicates, records mistakes with `mem lesson`, never follows [OLD] rows (nothing is deleted; `mem history` shows old ways), and writes scans, rule and setting changes, removals, list actions and project status to your private memory as they happen.

## Lessons learned
- Before saying something can't be done, search for a connector (SearchPlugins) and try the signed-in browser.

## Change log
- {date}: Book created at setup.
