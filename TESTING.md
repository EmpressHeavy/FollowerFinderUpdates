# Testing

## Subscription check against the test server

Follower Finder uses the same license server as Bot Terminator. The test server is in Stripe test mode:

`https://bt-license-test.empressheavy.workers.dev`

`config/license.json` already points there. The product code this bot sends is `ff`. A subscription for Follower Finder (`ff`) or for both bots (`both`) turns Follower Finder on. Bot Terminator alone (`bt`) does not. The server decides that. This repo does not keep a second copy of the server.

Nothing about an X account is sent. The check sends the install id and `ff` only.

1. From this repo (or from `~/follower_finder/app` on an install):

   ```sh
   python3 -m src.license_check register
   python3 -m src.license_check require scan
   ```

   The first command prints an install id. The second should refuse, say the subscription isn't active, and print a renew link. Your local records and X login stay as they are.

2. Open that renew link. Choose **Follower Finder**, or **Bot Terminator and Follower Finder** if you want both. Pay with Stripe's test card `4242 4242 4242 4242`, any future expiry, any CVC, and any ZIP. That card does not charge a real card.

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
