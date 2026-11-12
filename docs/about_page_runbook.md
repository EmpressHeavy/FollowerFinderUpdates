# Reading "About this account" (box-browser scan path, v3.5.8)

Used only in the Stage 2 deep check, and only for followers that survived Stage 1 (`about_account.about_read_queue`).
Read-only: never click Follow, Block, Report or anything else on these pages.

1. Open the follower's profile in the signed-in box browser (`https://x.com/<handle>`).
2. Click the "Joined <Month Year>" text under the bio. The "About this account" page opens.
   (Direct address: `https://x.com/<handle>/about` = `about_account.about_url(handle)`.)
3. Wait for the page to finish loading. Copy its visible text, or its accessibility-tree text, as is.
4. Call `about_account.parse_about_text(text)`. It returns `date_joined`, `account_based_in`, `verified`,
   `id_verified`, `username_changes` and `connected_via` (for example "United States App Store").
   Anything the page does not show stays "Not available". Never fill a field in from the name, photo, bio or language.
5. Save the result on the follower's record as `about` (or the page text as `about_text`) in `scan_raw/` or an
   enrichment file, then `build_snapshot` as usual.
6. Pace the reads with `about_account.AboutReadPacer`: wait `wait_before_next()` seconds before each page
   (about 8-12 s, a longer pause every 20 reads), call `record(handle, event)` after each one. On a 429,
   "Something went wrong", a blank page or a challenge, record that event; after a cooldown try once more, and after
   2 in a row stop the About reads for this session (`stop` is true) and resume later (finished handles are in `done`).

How the fields count (classify.stage2_classify):
- "Account based in" differs from the "Connected via" store country: a moderate signal (possible VPN or bot). Never proof on its own.
- 3 or more username changes: a signal (6 or more counts more).
- Verified (any kind, e.g. "Verified since September 2026" or "Verified, since ..."): the account is verified and is excluded before classification (v3.5.13 hard pre-filter). It is never scored, never REVIEW, never on a card.
- ID verified: a strong keep signal. The account is never labelled bot-like.
- Location alone never counts and is never a nationality judgment. It only matters as part of a mismatch, or
  alongside other bot signals.
