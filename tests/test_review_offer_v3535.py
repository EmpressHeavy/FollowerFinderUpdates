"""v3.5.35: review cards offer Follow back and Maybe only.

Skip is reviewed and remembered, not shown and not listed. Covers mixed batches, all-skip notes,
card counts, max 5, Follow all, no default ticks, pending validation, and legacy pending reviews.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src import daily_review as dr
from src import follow_executor as fx
from src import interest_discovery as disc
from src import memory as mem
from src import review_cards as rc
from src import verified_followers as vf

NOW = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)
NOTE = "No suitable candidates this round."
KEEPER_TEXT = "Today I planted basil in the gardening bed and watered the soil carefully before sunset"
CRYPTO_TEXT = "airdrop presale memecoin pump 100x join my telegram whitelist now"


def _profile(uid, handle, rec, *, rechecked=False):
    return {
        "user_id": str(uid), "handle": "@" + handle, "display_name": handle,
        "url": f"https://x.com/{handle}", "about": "about", "posts_about": "posts",
        "interaction": {"status": "none found", "items": []},
        "followers": 10, "following": 4, "post_count": 3,
        "rec": rec, "reason": "because " + rec, "account_health": "Account health: Not available",
        "interest": "Interest tie-in: None found", "recency": "Last post: Not available",
        "rechecked": rechecked,
    }


def _analysis():
    return {"n_items": 4, "pinned_excluded": False, "days_since_last_post": 2,
            "coverage": {"stop_reasons": []}}


def _shown(uid, username, rec, *, hard=False):
    return {"user": {"id": str(uid), "username": username}, "handle": "@" + username,
            "r": {"rec": rec, "hard_skip": hard, "analysis": _analysis()}}


class CardRules(unittest.TestCase):
    def test_mixed_batch_counts_follow_all_and_no_ticks(self):
        profiles = [_profile(i, f"keeper{i}", "Follow back" if i % 2 == 0 else "Maybe") for i in range(1, 7)]
        profiles.append(_profile("s1", "zzskipone", "Skip"))
        profiles.append(_profile("s2", "zzskiptwo", "Skip", rechecked=True))
        profiles[0]["rechecked"] = True
        out = rc.assemble_review(profiles, "run-1")
        self.assertIsNone(out["note"])
        self.assertEqual(out["offered"], 6)
        self.assertEqual(out["skipped_count"], 2)
        self.assertEqual(len(out["cards"]), 2)
        self.assertEqual(out["cards"][0]["title"], "New verified followers (1-5 of 6)")
        self.assertEqual(out["cards"][1]["title"], "New verified followers (6-6 of 6)")
        blob = json.dumps(out["cards"])
        self.assertNotIn("zzskipone", blob)
        self.assertNotIn("zzskiptwo", blob)
        self.assertNotIn('"rec": "Skip"', blob)
        shown = []
        for card in out["cards"]:
            self.assertLessEqual(len(card["account_ids"]), 5)
            self.assertEqual(card["options"][0]["value"], rc.FOLLOW_ALL)
            self.assertEqual(card["options"][0]["checked"], False)
            self.assertIn(str(len(card["account_ids"])), card["options"][0]["detail"])
            self.assertNotIn(rc.FOLLOW_ALL, card["account_ids"])
            for opt in card["options"]:
                self.assertFalse(opt["checked"])
                if opt["value"] != rc.FOLLOW_ALL:
                    self.assertIn(opt["rec"], ("Follow back", "Maybe"))
                    shown.append(opt["value"])
        self.assertEqual(shown, [str(i) for i in range(1, 7)])
        self.assertTrue(out["cards"][0]["options"][1]["label"].startswith("Rechecked"))
        ids = rc.resolve_picks(1, out["cards"][0]["account_ids"], [rc.FOLLOW_ALL],
                               {str(i): profiles[i - 1]["rec"] for i in range(1, 7)})
        self.assertEqual(ids, [str(i) for i in range(1, 6)])
        self.assertEqual(rc.resolve_picks(1, out["cards"][0]["account_ids"], [], 
                                          {str(i): "Maybe" for i in range(1, 6)}), [])

    def test_all_skip_note_lists_nobody(self):
        profiles = [_profile("s1", "zzskipone", "Skip"), _profile("s2", "zzskiptwo", "Skip")]
        out = rc.assemble_review(profiles, "run-skip")
        self.assertEqual(out["cards"], [])
        self.assertEqual(out["note"], NOTE)
        self.assertEqual(out["offered"], 0)
        self.assertEqual(out["skipped_count"], 2)
        self.assertNotIn("zzskip", out["note"])
        self.assertNotIn("zzskip", json.dumps(out["cards"]))

    def test_empty_review_is_not_the_all_skip_note(self):
        out = rc.assemble_review([], "run-empty")
        self.assertEqual(out["cards"], [])
        self.assertIsNone(out["note"])

    def test_pending_validation_and_legacy_rows(self):
        card = ["fb", "maybe", "skip", "old", "hard"]
        recs = {"fb": "Follow back", "maybe": "Maybe", "skip": "Skip", "old": None, "hard": "Maybe"}
        hard = {"hard"}
        self.assertEqual(rc.resolve_picks(1, card, [rc.FOLLOW_ALL], recs, hard_skip_ids=hard),
                         ["fb", "maybe", "old"])
        self.assertEqual(rc.resolve_picks(1, card, ["fb", "skip", "hard"], recs, hard_skip_ids=hard), ["fb"])
        self.assertEqual(rc.resolve_picks(1, card, [], recs), [])
        with self.assertRaises(ValueError):
            rc.resolve_picks(1, card, ["not-on-card"], recs)
        with self.assertRaises(ValueError):
            rc.resolve_picks(1, card, "fb", recs)
        with self.assertRaises(ValueError):
            rc.resolve_picks(1, card, ["ghost"], recs)  # on neither the card nor the rating map
        # on the card, but no stored rating: not a known Skip, so an explicit tick is rejected
        with self.assertRaises(ValueError):
            rc.resolve_picks(1, ["fb", "lost"], ["lost"], {"fb": "Follow back"})
        self.assertEqual(rc.resolve_picks(1, ["fb", "lost"], [rc.FOLLOW_ALL], {"fb": "Maybe"}), ["fb"])
        with self.assertRaises(ValueError):
            rc.check_card_keys([{"card_no": 1}], {2: []})

    def test_pacing_unchanged(self):
        self.assertEqual(fx.PACING["gap_s"], (20.0, 45.0))
        self.assertEqual(fx.PACING["pre_click_s"], (2.0, 5.0))
        self.assertEqual(fx.PACING["batch_every"], 5)
        self.assertEqual(fx.PACING["batch_break_s"], (120.0, 240.0))


class LegacyPendingReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.owner = "legacy-owner"
        self.run_id = "legacy-run"
        self.plans = []
        state = {
            "run_id": self.run_id, "owner_key": self.owner,
            "cards": [{"card_no": 1, "ids": ["fb", "maybe", "skip", "old", "hard"]}],
            "shown": [
                _shown("fb", "followacct", "Follow back"),
                _shown("maybe", "maybeacct", "Maybe"),
                _shown("skip", "zzskipacct", "Skip"),
                _shown("old", "oldacct", None),
                _shown("hard", "hardacct", "Maybe", hard=True),
            ],
        }
        vf.st.save_json(vf._dir(self.owner, self.root) / f"pending_{self.run_id}.json", state)

    def tearDown(self):
        self.tmp.cleanup()

    def _finish(self, picks, *, owner=None):
        def fake(user_key, run_id, plan, *args, **kwargs):
            self.plans.append(plan)
            return {"followed_ids": [], "text": "ok", "followed": []}

        with patch("src.daily_review.fx.run_browser", fake):
            return dr.finish_review(owner or self.owner, self.run_id, picks, browser=object(), root=self.root,
                                    sleep=lambda s: None, rand=lambda a, b: 0)

    def test_follow_all_skips_legacy_skip_and_hard_skip(self):
        self._finish({"1": [dr.FOLLOW_ALL]})
        ids = [p["user_id"] for p in self.plans[0]]
        self.assertEqual(ids, ["fb", "maybe", "old"])
        log = (vf._dir(self.owner, self.root) / "follow_back.jsonl").read_text()
        self.assertNotIn("zzskipacct", log)
        self.assertNotIn("hardacct", log)
        self.assertNotIn('"event": "FOLLOWED"', log)
        first = log.splitlines()[0]
        vf.append_log(self.owner, {"event": "NOTE", "run_id": self.run_id}, root=self.root)
        lines = (vf._dir(self.owner, self.root) / "follow_back.jsonl").read_text().splitlines()
        self.assertEqual(lines[0], first)
        self.assertEqual(len(lines), 2)

    def test_explicit_skip_tick_is_not_followed(self):
        self._finish({1: ["fb", "skip", "hard"]})
        self.assertEqual([p["user_id"] for p in self.plans[0]], ["fb"])

    def test_unknown_id_and_card_follow_nobody(self):
        with self.assertRaises(ValueError):
            self._finish({1: ["not-on-card"]})
        with self.assertRaises(ValueError):
            self._finish({9: [dr.FOLLOW_ALL]})
        with self.assertRaises(ValueError):
            self._finish({1: ["fb"]}, owner="other-owner")
        self.assertEqual(self.plans, [])
        self.assertFalse((vf._dir(self.owner, self.root) / "follow_back.jsonl").exists())

    def test_empty_ticks_follow_nobody(self):
        self._finish({})
        self.assertEqual(self.plans, [[]])


def _person(uid, username, *, created="2020-04-01T00:00:00.000Z", followers=120, bio=None):
    return {
        "id": uid, "username": username, "name": username, "verified": True,
        "description": bio or "I grow herbs at home and post what I learn.",
        "created_at": created,
        "public_metrics": {"followers_count": followers, "following_count": 80, "tweet_count": 40},
    }


def _post(uid, text):
    return {"id": "p-" + uid, "text": text, "author_id": uid, "created_at": "2026-11-07T15:00:00.000Z"}


class _X:
    def __init__(self, followers, posts):
        self.followers, self.posts = followers, posts

    def call(self, tool, args):
        if tool == "get_users_me":
            return {"data": {"id": "owner-1", "username": "owneracct"}}
        if tool == "get_users_me_followers":
            return {"data": list(self.followers), "meta": {}}
        if tool == "get_users_me_following":
            return {"data": [], "meta": {}}
        if tool == "get_users_posts":
            return {"data": self.posts.get(str(args.get("id")), []), "meta": {}}
        if tool == "search_posts_all":
            return {"data": [], "meta": {}}
        return {"data": [], "meta": {}}


class _Browser:
    def __init__(self):
        self.translated = []

    def block_check(self, users, steps):
        return {str(u["id"]): False for u in users}

    def translate(self, requests, steps):
        self.translated = [r.get("handle") for r in requests]
        return {}

    def follow(self, *args, **kwargs):
        raise AssertionError("review must not follow")


class VerifiedReviewRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, owner, followers, posts):
        mem.init_db(owner, self.root)
        vf.mark_seen(owner, [{"id": "seed", "username": "seeduser"}], root=self.root, baseline=True)
        return dr.run_daily_review(owner, _X(followers, posts), _Browser(), now=NOW, root=self.root,
                                   prefs={"allow_reply_heavy": False, "show_bought_risk": False,
                                          "read_languages": ["en"], "daily_review_time": "18:00"})

    def test_mixed_cards_hide_skip_and_remember_it(self):
        good = [_person(f"g{i}", f"keeper{i}") for i in range(1, 7)]
        skip = _person("sk", "zzskipacct")
        posts = {u["id"]: [_post(u["id"], KEEPER_TEXT)] for u in good}
        out = self._run("rev-owner", good + [skip], posts)
        blob = json.dumps(out)
        self.assertNotIn("zzskipacct", blob)
        self.assertIsNone(out["note"])
        self.assertEqual(out["offered"], 6)
        self.assertEqual(out["skipped_count"], 1)
        self.assertEqual(len(out["cards"]), 2)
        self.assertEqual(out["cards"][0]["title"], "New verified followers (1-5 of 6)")
        self.assertEqual(out["cards"][1]["title"], "New verified followers (6-6 of 6)")
        ids = [i for c in out["cards"] for i in c["account_ids"]]
        self.assertEqual(len(ids), 6)
        self.assertNotIn("sk", ids)
        for card in out["cards"]:
            self.assertLessEqual(len(card["account_ids"]), vf.CARD_SIZE)
            self.assertFalse(any(o.get("checked") for o in card["options"]))
            self.assertEqual(card["options"][0]["value"], dr.FOLLOW_ALL)
        seen = vf.load_seen("rev-owner", self.root)
        self.assertIn("sk", seen["ids"])
        for u in good:
            self.assertIn(u["id"], seen["ids"])
        self.assertNotIn("zzskipacct", self.browser_unused(out))
        again = self._run("rev-owner", good + [skip], posts)
        self.assertEqual(again["cards"], [])
        self.assertIsNone(again["note"])
        self.assertNotIn("zzskipacct", json.dumps(again))

    def browser_unused(self, out):
        return json.dumps(out.get("translation_requests"))

    def test_all_skip_note_and_no_follow(self):
        skip = _person("sk", "zzskipacct")
        browser = _Browser()
        mem.init_db("skip-owner", self.root)
        vf.mark_seen("skip-owner", [{"id": "seed", "username": "seeduser"}], root=self.root, baseline=True)
        out = dr.run_daily_review("skip-owner", _X([skip], {}), browser, now=NOW, root=self.root,
                                  prefs={"allow_reply_heavy": False, "show_bought_risk": False,
                                         "read_languages": ["en"], "daily_review_time": "18:00"})
        self.assertEqual(out["cards"], [])
        self.assertEqual(out["note"], NOTE)
        self.assertNotIn("zzskipacct", json.dumps(out))
        self.assertEqual(browser.translated, [])
        self.assertIn("sk", vf.load_seen("skip-owner", self.root)["ids"])
        plans = []

        def fake(*args, **kwargs):
            plans.append(args[2] if len(args) > 2 else kwargs.get("picks"))
            return {"followed_ids": [], "text": "ok", "followed": []}

        with patch("src.daily_review.fx.run_browser", fake):
            with self.assertRaises(ValueError):
                dr.finish_review("skip-owner", out["run_id"], {1: ["sk"]}, browser, root=self.root)
            summary = dr.finish_review("skip-owner", out["run_id"], {}, browser, root=self.root, sleep=lambda s: None)
        self.assertEqual(plans, [[]])
        self.assertEqual(summary["followed_ids"], [])

    def test_follow_all_is_only_the_displayed_accounts(self):
        people = [_person(f"g{i}", f"keeper{i}") for i in range(1, 4)]
        skip = _person("sk", "zzskipacct")
        posts = {u["id"]: [_post(u["id"], KEEPER_TEXT)] for u in people}
        out = self._run("pick-owner", people + [skip], posts)
        plans = []

        def fake(*args, **kwargs):
            plans.append(args[2])
            return {"followed_ids": [], "text": "ok", "followed": []}

        with patch("src.daily_review.fx.run_browser", fake):
            with self.assertRaises(ValueError):
                dr.finish_review("pick-owner", out["run_id"], {1: [dr.FOLLOW_ALL, "sk"]}, object(), root=self.root)
            dr.finish_review("pick-owner", out["run_id"], {1: [dr.FOLLOW_ALL]}, object(), root=self.root,
                             sleep=lambda s: None, rand=lambda a, b: 0)
        self.assertEqual(len(plans), 1)
        self.assertEqual([p["user_id"] for p in plans[0]], out["cards"][0]["account_ids"])
        self.assertNotIn("sk", [p["user_id"] for p in plans[0]])
        log = (vf._dir("pick-owner", self.root) / "follow_back.jsonl").read_text()
        self.assertNotIn("zzskipacct", log)


def _xuser(uid, username, *, bio, created="2018-01-01T00:00:00.000Z", followers=100):
    return {"id": uid, "username": username, "name": username, "description": bio, "created_at": created,
            "public_metrics": {"followers_count": followers, "following_count": 20, "tweet_count": 30}}


class InterestDiscoveryCards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.topics = ["gardening", "basil"]

    def tearDown(self):
        self.tmp.cleanup()

    def _users(self):
        keepers = [_xuser(f"k{i}", f"keeper{i}", bio="notes from the garden") for i in range(1, 7)]
        crypto = _xuser("c1", "zzskipcrypto", bio="signals")
        bought = _xuser(
            "b1", "zzskipbought",
            bio="Founder | creator building gardening tools for everyone who loves basil outdoors every single day",
            created="2026-10-01T00:00:00.000Z", followers=8000)
        return keepers, crypto, bought

    def _search(self, users):
        def search(_query):
            return {"includes": {"users": users}}
        return search

    def _fetch(self, fetched):
        def fetch(uid):
            fetched.append(str(uid))
            text = CRYPTO_TEXT if str(uid) == "c1" else KEEPER_TEXT
            return {"data": [{"text": text, "created_at": "2026-11-01T00:00:00.000Z"}]}
        return fetch

    def test_mixed_batch_hides_skips_and_remembers_them_per_owner(self):
        keepers, crypto, bought = self._users()
        users = keepers + [crypto, bought]
        fetched = []
        out = disc.run_discovery(self.topics, search=self._search(users), fetch_posts=self._fetch(fetched),
                                 user_key="interest-a", root=self.root, now=NOW)
        blob = json.dumps(out)
        self.assertNotIn("zzskipcrypto", blob)
        self.assertNotIn("zzskipbought", blob)
        self.assertIsNone(out["note"])
        self.assertEqual(out["offered"], 6)
        self.assertEqual(out["skipped_count"], 2)
        self.assertEqual(len(out["cards"]), 2)
        self.assertEqual(out["cards"][0]["title"], "People who match your interests (1-5 of 6)")
        self.assertEqual(out["cards"][1]["title"], "People who match your interests (6-6 of 6)")
        ids = [i for c in out["cards"] for i in c["account_ids"]]
        self.assertEqual(ids, [u["id"] for u in keepers])
        for card in out["cards"]:
            self.assertLessEqual(len(card["account_ids"]), 5)
            self.assertFalse(any(o.get("checked") for o in card["options"]))
        pending = json.loads((self.root / "data/users/interest-a/interest_discovery" /
                              f"pending_{out['run_id']}.json").read_text())
        self.assertNotIn("zzskip", json.dumps(pending))
        remembered = json.loads((self.root / "data/users/interest-a/interest_discovery/skipped.json").read_text())
        self.assertEqual(set(remembered["ids"]), {"c1", "b1"})
        self.assertNotIn("zzskip", json.dumps(remembered))
        self.assertFalse((vf._dir("interest-a", self.root) / "follow_back.jsonl").exists())
        # next search does not fetch the remembered skips; another owner still can
        fetched2 = []
        again = disc.run_discovery(self.topics, search=self._search(users), fetch_posts=self._fetch(fetched2),
                                   user_key="interest-a", root=self.root, now=NOW)
        self.assertEqual(fetched2, [])
        self.assertEqual(again["offered"], 0)
        self.assertEqual(again["qualified_ids"], [])
        self.assertEqual(again["note"], disc.ALREADY_SUGGESTED_NOTE)
        self.assertNotIn("keeper1", json.dumps(again["cards"]))
        fetched_b = []
        other = disc.run_discovery(self.topics, search=self._search(users), fetch_posts=self._fetch(fetched_b),
                                   user_key="interest-b", root=self.root, now=NOW)
        self.assertIn("c1", fetched_b)
        self.assertEqual(other["skipped_count"], 2)
        self.assertTrue((self.root / "data/users/interest-b/interest_discovery/skipped.json").exists())
        self.assertNotEqual(
            (self.root / "data/users/interest-a/interest_discovery/skipped.json").read_text(),
            "")

    def test_all_skip_note_and_selection_does_not_follow(self):
        crypto = _xuser("c1", "zzskipcrypto", bio="signals")
        other = _xuser("c2", "zzskipother", bio="more signals")
        fetched = []

        def fetch(uid):
            fetched.append(str(uid))
            return {"data": [{"text": CRYPTO_TEXT, "created_at": "2026-11-01T00:00:00.000Z"}]}

        calls = []

        def boom(*args, **kwargs):
            calls.append("follow")
            raise AssertionError("followed")

        with patch("src.follow_executor.run_browser", boom), patch("src.follow_executor.run", boom):
            out = disc.run_discovery(self.topics, search=self._search([crypto, other]), fetch_posts=fetch,
                                     user_key="skip-owner", root=self.root, now=NOW)
            self.assertEqual(out["cards"], [])
            self.assertEqual(out["note"], NOTE)
            self.assertEqual(out["candidates"], [])
            self.assertNotIn("zzskip", json.dumps(out))
            with self.assertRaises(ValueError):
                disc.validate_pending_selection("skip-owner", out["run_id"], {1: ["c1"]}, root=self.root)
            picked = disc.validate_pending_selection("skip-owner", out["run_id"], {}, root=self.root)
        self.assertEqual(picked, {"picked": []})
        self.assertEqual(calls, [])
        self.assertFalse((vf._dir("skip-owner", self.root) / "follow_back.jsonl").exists())
        fetched2 = []
        again = disc.run_discovery(self.topics, search=self._search([crypto, other]),
                                   fetch_posts=lambda uid: fetched2.append(str(uid)) or {"data": []},
                                   user_key="skip-owner", root=self.root, now=NOW)
        self.assertEqual(fetched2, [])
        self.assertEqual(again["note"], NOTE)
        self.assertEqual(again["cards"], [])

    def test_validate_follow_all_and_legacy_pending(self):
        keepers, crypto, _bought = self._users()
        out = disc.run_discovery(self.topics, search=self._search(keepers[:6] + [crypto]),
                                 fetch_posts=self._fetch([]), user_key="pick-owner", root=self.root, now=NOW)
        calls = []
        with patch("src.follow_executor.run_browser", lambda *a, **k: calls.append("x")):
            with self.assertRaises(ValueError):
                disc.validate_pending_selection("pick-owner", out["run_id"], {1: ["c1"]}, root=self.root)
            self.assertFalse((vf._dir("pick-owner", self.root) / "follow_back.jsonl").exists())
            first = disc.validate_pending_selection("pick-owner", out["run_id"], {1: [rc.FOLLOW_ALL]}, root=self.root)
            second = disc.validate_pending_selection("pick-owner", out["run_id"], {2: [rc.FOLLOW_ALL]}, root=self.root)
        self.assertEqual(calls, [])
        self.assertEqual([p["user_id"] for p in first["picked"]], out["cards"][0]["account_ids"])
        self.assertEqual([p["user_id"] for p in second["picked"]], out["cards"][1]["account_ids"])
        log = (vf._dir("pick-owner", self.root) / "follow_back.jsonl").read_text()
        self.assertNotIn("zzskipcrypto", log)
        self.assertNotIn("FOLLOWED", log)
        first_line = log.splitlines()[0]
        vf.append_log("pick-owner", {"event": "NOTE", "run_id": out["run_id"]}, root=self.root)
        lines = (vf._dir("pick-owner", self.root) / "follow_back.jsonl").read_text().splitlines()
        self.assertEqual(lines[0], first_line)
        # legacy pending card still lists a Skip; Follow all must not select it
        legacy = {
            "run_id": "old-interest", "owner_key": "pick-owner",
            "cards": [{"card_no": 1, "ids": ["k1", "c1"]}],
            "accounts": {"k1": {"handle": "@keeper1", "rec": "Follow back"},
                         "c1": {"handle": "@zzskipcrypto", "rec": "Skip"}},
        }
        vf.st.save_json(self.root / "data/users/pick-owner/interest_discovery/pending_old-interest.json", legacy)
        got = disc.validate_pending_selection("pick-owner", "old-interest", {1: [rc.FOLLOW_ALL, "c1"]}, root=self.root)
        self.assertEqual(got["picked"], [{"user_id": "k1", "handle": "@keeper1"}])
        log = (vf._dir("pick-owner", self.root) / "follow_back.jsonl").read_text()
        self.assertNotIn("zzskipcrypto", log)

    def test_empty_search_has_no_note(self):
        out = disc.run_discovery(self.topics, search=lambda q: {}, fetch_posts=lambda i: {},
                                 user_key="empty-owner", root=self.root, now=NOW)
        self.assertEqual(out["cards"], [])
        self.assertIsNone(out["note"])
        self.assertEqual(out["skipped_count"], 0)


class ReleaseMarkers(unittest.TestCase):
    def test_version_and_changelog(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual((root / "VERSION").read_text().strip(), "v3.5.38")
        changelog = (root / "CHANGELOG.md").read_text()
        self.assertIn("## v3.5.38", changelog)
        self.assertIn("## v3.5.37", changelog)
        self.assertIn("## v3.5.36", changelog)
        self.assertIn("## v3.5.35", changelog)
        self.assertLess(changelog.index("## v3.5.38"), changelog.index("## v3.5.37"))
        self.assertLess(changelog.index("## v3.5.37"), changelog.index("## v3.5.36"))
        self.assertIn("no longer delete per-user state", changelog)
        self.assertIn("$4.49/month", changelog)
        self.assertIn("https://empressheavy.com/subscribe/", changelog)
        self.assertIn(NOTE, changelog)
        self.assertIn("v3.5.38", (root / "src" / "updater.py").read_text().splitlines()[0])


if __name__ == "__main__":
    unittest.main()
