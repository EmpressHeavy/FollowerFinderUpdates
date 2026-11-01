# Routine: Follower Finder weekly update check (always on)
- Name: Follower Finder weekly update check. Weekly, Sunday 10:00 AM owner time zone (cron `0 10 * * 0`). May be moved, not turned off; the monthly check re-creates it if missing.
1. Run `python3 <HOME>/app/src/updater.py apply --home <HOME>` (HOME = ~/follower_finder, or the test install's own folder).
2. "updated": replace the getting-started skill with <HOME>/app/SKILL.md, save `mem` fact `ff installed_version`, send one note "Follower Finder updated to <version>. What's new: <2-4 bullets from CHANGELOG.md>".
3. "current": silent. "rolled_back": one line saying it tried, rolled back, will retry next week. Fetch error: silent; mention only after 3 failed weeks in a row.
Updates never turn on follows, unfollows, messages or other optional settings; new options are offered off by default. Source: github.com/EmpressHeavy/FollowerFinderUpdates only.
