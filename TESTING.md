# Testing

## Subscription check against the live server

Follower Finder uses the shared license server. `config/license.json` points at the live server:

`https://empress-license.empressheavy.workers.dev`

The product code this bot sends is `ff`. A subscription for Follower Finder (`ff`) or for both bots (`both`) turns Follower Finder on. Bot Terminator alone (`bt`) does not. The server decides that. This repo does not keep a second copy of the server.

Nothing about an X account is sent. The check sends the install id and `ff` only.

Follower Finder is $4.49/month. Bot Terminator and Follower Finder together are $8.99/month. Both are listed at https://empressheavy.com/subscribe/.

1. From this repo (or from `~/follower_finder/app` on an install):

   ```sh
   python3 -m src.license_check register
   python3 -m src.license_check require scan
   ```

   The first command prints an install id. The second should refuse, say the subscription isn't active, and print a clickable `[Subscribe](https://empressheavy.com/subscribe/?product=ff&install_id=...)` link (product and install ID already filled in), the install ID, and the `/pay` fallback `https://empress-license.empressheavy.workers.dev/pay?install_id=...&product=ff`. If the install id is not known yet, the Subscribe link is https://empressheavy.com/subscribe/?product=ff with no `/pay` fallback. Your local records and X login stay as they are.

2. Open that link and pay with a real card. This is the live server, so the charge is real. Stripe's test card `4242 4242 4242 4242` is only for a Stripe test-mode server, not this one.

3. Run the check again:

   ```sh
   python3 -m src.license_check require scan
   python3 -m src.license_check require recommend
   python3 -m src.license_check require follow
   python3 -m src.license_check require write
   ```

   Each one should print `subscription active`.

4. Offline grace: after one active answer, if the server cannot be reached, scans, suggestions, follows, and other X changes still run for 72 hours, then they stop. An answer that says the subscription is not active stops immediately. Records and the X login are not deleted.

Unit tests do not call Stripe or the license server:

```sh
python3 -m pytest -q tests
python3 -m unittest discover -s tests -t .
```
