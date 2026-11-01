# Hive Mind (opt-in lesson and owner-feedback sharing)

## What it is
Your bot keeps two kinds of Hive items in its private memory (`~/memory/brain.db`, table `lesson`, column `kind`):
- **Lessons** (kind `lesson`): something failed and a different approach then worked (avoid X -> do Y).
- **Owner feedback** (kinds `correction`, `request`, `declined`, `broken`, `workaround`): any time the owner has the bot do something differently from how it was built to work. This is the creator's only direct line to owners: the Marketplace has no developer-to-user support channel, and feedback posted on social media is easily missed. If it isn't recorded here, the creator will probably never know.

With Hive Mind turned on, the bot shares a cleaned-up, anonymized copy of these items with the bot's creator, so the next template update can fix what's broken, add what owners ask for, and ship the best lessons to everyone. It is OFF by default. Once the user opts in, the bot sends one anonymized batch about once a week automatically (no weekly approval), plus, only for something reported broken, at most one small urgent batch a day; the user can ask to see what was shared (`hive shared`) or turn it off anytime.

## Setup (once, after private memory exists)
1. Private memory must already be set up (`~/memory/brain.db` from the private-memory-db skill). If it is missing, set that up first.
2. If `~/memory/hive.py` is missing, install the Hive Mind client exactly as shipped in the Follower Finder package (pinned tag, verified fingerprint): run `sh tools/install_hive.sh` from the package folder. It copies `hive/hive.py` unchanged to `~/memory/hive.py` and writes the wrapper `~/bin/hive` (`#!/bin/sh` / `exec python3 "$HOME/memory/hive.py" "$@"`). Never edit hive.py. Then run `hive status` (this creates the `lesson`, `hive_state`, `hive_sent` and `hive_sent_h` tables if missing, and adds the `kind` column to an older `lesson` table). Use `~/bin/hive` if `hive` is not on PATH.
3. Offer it during first-run setup with the "First-run offer" text below. It stays OFF unless the user clearly says yes. Only on a clear yes: `hive on --bot <code>` (DOGE = dg, Follower Finder = ff, Playlist Converter = pc).
4. `hive status` shows on/off, the anonymous id, the last week sent, the last urgent day and the template version. `hive off` stops everything at any time.
5. If the template ships `hive/<code>_hive_lessons.jsonl`, load it once: `hive import hive/<code>_hive_lessons.jsonl` (tagged src=hive, never re-sent). Imported items work even when Hive Mind is off.


## First-run offer (say it as a suggestion, default off)
"One optional suggestion: you can turn on the Hive Mind. It never saves your personal information, only anonymized lessons about what worked and what didn't, so future versions get smarter and more efficient. Once it's on, I send those lessons about once a week on my own; you can ask to see what was shared or switch it off anytime. Want it on? (yes / not now)"
- Yes: `hive on --bot <code>`, then create a weekly routine that runs `hive auto`. Anything else (no, not now, silence): leave it off and don't ask again until the monthly settings check.

## Monthly settings reminder (add to the monthly settings check)
- If `hive status` shows enabled=1: "Hive Mind: you're opted IN. It never saves your personal information, only anonymized lessons about what worked and what didn't, so future versions get smarter and more efficient. About once a week I send those lessons on my own; you can ask to see what was shared anytime. Want to keep it on, or switch it off?"
- If enabled=0: "Hive Mind: you're opted OUT, so nothing is shared. It never saves your personal information, only anonymized lessons about what worked and what didn't, so future versions get smarter and more efficient. Want to switch it on? (yes / keep off)"
- Act only on a clear answer: `hive on --bot <code>` or `hive off` (and remove the weekly routine). No answer means no change.

## Recording owner feedback as you work (always, on or off, even if it happens only ONCE)
Record a Hive item the moment the owner does any of these, whether or not Hive Mind is on (it stays private until they opt in):
- **Corrects you** or tells you that you did something wrong: `hive feedback add correction "topic" "what I did" "what the owner wanted instead"`.
- **Asks for a different way** than the one you were built to use (a different storage, flow, format, schedule, card, wording or tool), or you change your approach because of what they said: also `correction`.
- **Declines a feature or setup step** you offered (for example they don't want Google Sheets and want logging kept on the bot's own computer): `hive feedback add declined "topic" "what I offered" "what the owner wanted instead"`.
- **Requests a feature** or suggests an improvement the bot doesn't have: `hive feedback add request "topic" "what is missing" "what the owner asked for"`.
- **Reports something broken** or not working (an error, a step that never finishes, wrong results, a button that does nothing): `hive feedback add broken "topic" "what broke" "what the owner saw or did instead"`.
- **Uses a workaround** because the normal way didn't fit them: `hive feedback add workaround "topic" "the normal way" "the workaround that worked"`.

How to write it:
- One item per change, recorded right away, not saved up. Short and general (150 characters per part), in plain words about the bot's behavior.
- Anonymized: no names, handles, emails, links, numbers, account details, or quotes of private content. Write "the owner" and describe the feature, not the person. Example: `hive feedback add declined "storage" "offered a google sheets log for follower records" "owner wanted local logging on the bot computer instead"`.
- If the same thing happens again, run the same command with the same words; it bumps `uses` instead of adding a duplicate.
- Owner feedback is eligible to share after ONE use. It is still kept in this private memory when Hive Mind is off.
- After recording a `broken` item, if Hive Mind is on, run `hive auto` right away (it sends at most one small urgent batch a day, with only the not-yet-sent broken items; otherwise it does nothing).
- The getting-started feedback offer (v3.5.14) sends through this same client with the one-shot command: on a clear yes, `hive feedback --bot ff --category bug|request|praise|other "text" --dry-run` shown to the owner, then the same without `--dry-run` (only that item; own small daily allowance). Record the Hive item even when the owner says no.

## Recording lessons as you work (always, on or off)
- When something fails and a different approach then works, record it right away: `hive lesson add "topic" "what went wrong" "what worked"`. Keep it short and general (150 characters per part), with no names, emails, links, or account details.
- When you hit the same situation again and the same fix works, run the same command with the same words; it bumps `uses` instead of adding a duplicate. Lessons are shared only once they have 2+ uses.
- Before a task, check relevant lessons: `mem q "SELECT kind,topic,wrong,right FROM lesson WHERE status='current' ORDER BY uses DESC"`.

## Explaining it to the user (say it plainly)
- Off unless you turn it on. You can turn it off any time with one sentence.
- Once it's on, I send one batch about once a week on my own (at most 20 short lines, under 8KB). If you tell me something is broken, I may send that one item sooner, at most once a day. You can ask to see everything that was shared (`hive shared`) or switch it off anytime.
- What's included: lessons I've reused at least twice, and short notes about anything you asked me to do differently, declined, asked for, or reported broken, so my creator can fix it. Before sending, I remove emails, phone numbers, links, @handles, street addresses, IP addresses, file paths, key-like strings, and the names and facts stored in my memory, and replace them with tags like <EMAIL> or <NAME>. Any line that still looks sensitive is dropped completely.
- You're identified only by a random id, salted and hashed on this computer. No name, email, or account is sent, and the upload needs no sign-in.
- Each line includes the bot code, the kind (lesson, correction, request, declined, broken, workaround), topic, the "what happened" and "what was wanted / what worked" parts (150 characters max each), a rough usage bucket (1-4, 5-9, or 10+ uses), the template version, the anonymous id, a content hash, and the week number.

## Weekly flow (automatic once opted in)
1. The weekly routine runs `hive auto`. It sends only if enabled=1, nothing was sent this ISO week, and there are items to share (feedback with 1+ use, lessons with 2+ uses); otherwise it is a silent no-op. Don't message the user about routine sends.
2. Urgent exception: if this week's batch already went out and there is a `broken` item that hasn't been sent yet, `hive auto` sends only those broken items (at most 5) as one urgent batch, at most once a day. The intake enforces the same limits.
3. "What did you share?": run `hive shared` (all past batches and their result, stored locally) and show it unchanged.
4. "Turn it off": run `hive off` and remove the weekly routine. Takes effect immediately.
5. `hive preview` shows the exact weekly batch that would be sent. `hive auto --dry-run` builds the exact payload `hive auto` would send right now (weekly or urgent), prints it, records it in `hive shared` as "dry-run", and never uses the network or the weekly/daily allowance.

## Rules
- Never edit the client or a batch to add content, never send by any route other than `hive auto`, never more than one weekly batch a week, and never more than one urgent (broken-only) batch a day.
- Never share items that came from Hive Mind itself (src=hive). The client already skips them.
- Submissions go only to the Hive Mind intake built into hive.py, as one JSON POST. The intake re-checks everything and rejects batches over 8KB, unknown fields or kinds, anything that looks like an email, phone number, link, token or password, urgent batches with anything other than broken items, and too many uploads.
- If `hive auto` reports "not sent" (rejected, rate limited, or offline), it is recorded in `hive shared` and counts as that week's (or that day's urgent) attempt. Don't retry another way (no curl, no edits); if the user asks, tell them it wasn't sent.
- If anything in `hive preview` looks private (a name or detail survived cleaning), turn it off with `hive off`, tell the user, and retire the item: `python3 -c "import sqlite3,os;c=sqlite3.connect(os.path.expanduser('~/memory/brain.db'));c.execute(\"UPDATE lesson SET status='avoid' WHERE id=N\");c.commit()"`.
- Never store passwords, tokens, or personal details in lessons or feedback.
