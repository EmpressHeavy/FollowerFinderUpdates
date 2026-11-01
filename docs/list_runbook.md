# Lists runbook: leaving a list (browser steps for the bot)

For the bot's browser step. The package never drives X; it decides, checks and logs. You drive the user's signed-in
browser, one list at a time, exactly as below, and report what you saw to `list_flow.leave_list(...)`.

## When you may run this

- The user tapped **Remove me from these lists** (batch card) or **Remove me from selected lists** (final skipped card).
  In Automatic Mode (the default) that tap IS the authorization: start right away. In Manual Mode ask for a yes first.
- `list_flow.handle_answer(...)` records the tap and returns `execute_now` / `awaiting_confirmation` and the `tasks`.
- One run at a time (the run lock, `run.lock`). Frozen snapshot only: the list data must be unchanged since review.
- Never act without a recorded authorization for that exact list on that frozen snapshot.

## Per list (repeat; the loop is `list_flow.next_task` -> browser steps -> `list_flow.leave_list` -> write the rows)

0. `next_task(...)` gives the list, `protected_owner` and `wait_seconds`. Wait that long (X actions are at least 10 s apart).
   If `protected_owner` is set (owner is Always Keep / protected): do NOT open X. Call `leave_list(..., {})`; it logs the
   skip. Then write the row (step 6) and go on.
1. **Check the list.** Open `https://x.com/<user>/lists/memberships` (the user's own handle; the lists they are on).
   Is the list there? Record `on_list_before` (true/false; null if the page could not be read). If it is not there: stop
   working on this list, `leave_list` logs "already off".
2. **Check the owner.** Open the owner's profile (`https://x.com/<owner_handle>`). Is the owner already blocked by the user
   (the profile shows a "Blocked" button)? Record `owner_blocked_before` (true/false/null).
   - **Owner already blocked => stop for this list.** Do NOT unblock, do not block again. The runner logs
     `LIST_SKIPPED_OWNER_ALREADY_BLOCKED`. (Unblocking would undo something the user chose.)
3. **Block.** On the owner's profile: three-dots menu > **Block** > confirm **Block** in the dialog. Record
   `block` = `{"status": "ok"}`. If X shows any warning, rate-limit or error message instead, record
   `{"status": "warning"|"rate_limited"|"error", "message": "<what X said>"}` and do nothing more (see "Stop rules").
4. **Wait about 10 seconds.** (The pause between Block and Unblock.)
5. **Unblock.** Same profile: click the **Blocked** button (or three-dots > **Unblock**) > confirm **Unblock**. Record
   `unblock` = `{"status": "ok"}` (or the failure as above). If the unblock fails, the owner stays blocked: tell the user.
6. **Verify.** Reload `https://x.com/<user>/lists/memberships`: the list must be gone (`on_list_after: false`), and the
   owner's profile must NOT show Blocked (`owner_blocked_after: false`). The list leaving the memberships page is the
   proof; do not rely on the block/unblock clicks alone.
7. **Report and log, immediately.** Call
   `list_flow.leave_list(user_key, snapshot_id, list_id, observed)` with
   `{"on_list_before", "owner_blocked_before", "block", "unblock", "on_list_after", "owner_blocked_after"}`.
   It runs the same checks as a live run and saves the outcome. It returns `sheet_plan` (one `append_rows` for the
   **List Graveyard** tab) and `book_plan` (a Change log line). **Run both with the connectors right now**, then call
   `list_flow.mark_written(user_key, entry_id, sheet=True, book=True)`. Do not open the next list until the row is written.

## Log every outcome, not just successes

The List Graveyard row is written the moment each list's task finishes, right after the verified result, never in a
later batch step. Columns: when (with time zone), action, list name, list ID, owner handle, owner ID, verified result,
snapshot ID, entry ID. The result text is what really happened:

| Outcome | Status | Verified result column |
|---|---|---|
| Off the list, owner not blocked | `LIST_SOFT_BLOCKED` | verified: no longer on the list; owner not blocked |
| Both clicks done, list still there | `LIST_SOFTBLOCK_NOT_CONFIRMED` | NOT verified: still on list=...; owner blocked=... |
| Unblock failed | `LIST_UNBLOCK_FAILED` | NOT verified: owner left BLOCKED ... |
| Already off the list | `LIST_SKIPPED_NOT_ON_LIST` | already off this list |
| Owner already blocked | `LIST_SKIPPED_OWNER_ALREADY_BLOCKED` | skipped: owner already blocked ... |
| Owner Always Keep / protected | `LIST_SKIPPED_PROTECTED_OWNER` | skipped: owner is Always Keep / protected |
| X warning / rate limit / error | `LIST_STOPPED` | NOT done: stopped on X ... |

If a write to the sheet fails, stop and tell the user; the entry is saved locally and `list_flow.catch_up(...)` writes it
later. That is only a safety net.

## Stop rules

- Any X warning, rate-limit message or unexpected error: stop the run. A warning or rate limit locks everything for the
  day (`locks_day`). Tell the user. Do not retry, do not try another list.
- Daily cap: each list exit counts **2** (block + unblock). The runner refuses a list that would pass the cap.
- A list or owner you cannot read (page did not load): record `null`; the runner stops rather than guessing.
- Never block or unblock anyone outside this runbook.

## Keep

**Keep all these lists** (or unselected lists on the final card answered "Keep all"): nothing happens on X.
`handle_answer(...)` returns `kept_plan`; run it (it appends the lists to the **Kept Lists** tab), then
`list_flow.kept_written(user_key, keys)`.

## Warning before the tap

If the user already follows a list's owner, the card message says so ("You follow this owner: leaving this list will
also unfollow them"). That is the only gate; it is a warning, not a block. Always Keep / protected owners are skipped.

## Observed facts (from the user's live test)

- `https://x.com/<user>/lists/memberships` works and shows the lists the user is on; use it for before and after checks.
- Soft block = Block, wait about 10 s, Unblock; afterwards the list is gone from that page and the owner is not blocked.
- If the owner is already blocked: stop, do not unblock.
- The block/unblock dialogs need a confirm click each.
