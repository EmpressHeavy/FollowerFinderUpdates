"""v3.5.36 suggestion history, small-account judging, and interest+size discovery.

No live X calls. Fetchers are injected. Nothing in these tests follows an account.
"""
import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src import classify as cl
from src import interest_discovery as disc
from src import suggestion_history as sh
from src import verified_deep_dive as dd
from src import verified_followers as vf

NOW = dt.datetime(2026, 11, 8, 18, tzinfo=dt.timezone.utc)
LATER = NOW + dt.timedelta(days=sh.COOLDOWN_DAYS)
TOPICS = ["rust"]
GOOD = "A long thread about rust lifetimes and borrow checking in real codebases today"
QUESTION = "Does the rust borrow checker accept this lifetime when the caller owns the buffer, yes or no?"
REPLY = "I tried that rust pattern on a real service and the shorter lifetime was the one that held up"
SPAM = "airdrop presale memecoin pump 100x join my telegram whitelist now"


def _user(i, *, fol=150, fing=100, bio="rust notes", metrics=True):
    u = {"id": str(i), "username": f"u{i}", "name": f"U{i}", "description": bio,
         "created_at": "2019-01-01T00:00:00Z"}
    if metrics:
        u["public_metrics"] = {"followers_count": fol, "following_count": fing, "tweet_count": 40}
    return u


def _posts(*texts):
    return {"data": [{"text": t, "created_at": "2026-11-01T00:00:00Z"} for t in texts]}


class SuggestionHistoryV3536(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_genuine_question_is_not_bait_and_hard_bait_still_is(self):
        self.assertTrue(vf.is_genuine_question(QUESTION))
        self.assertEqual(vf.classify_text(QUESTION), "high")
        self.assertEqual(vf.classify_text("yes or no?"), "low_farm")
        self.assertEqual(vf.classify_text("say hello if you are here"), "low_farm")
        self.assertEqual(vf.classify_text("@a Elon asks: say hello to my son"), "low_farm")
        self.assertEqual(vf.classify_text("gm"), "low_gm")
        self.assertIn("low_farm", vf.content_value(_posts(SPAM, "yes or no?"))["low_kinds"])
        self.assertNotIn("low_farm", vf.content_value(_posts(QUESTION))["low_kinds"])

    def test_small_account_judged_by_posts_not_size(self):
        small = _user(1, fol=12, fing=4000)
        scored = disc.score_candidate(small, _posts(GOOD, QUESTION), TOPICS, now=NOW)
        self.assertTrue(scored["keep"])
        self.assertEqual(scored["judged_by"], "posts")
        self.assertEqual(scored["size_penalty"], 0)
        self.assertNotIn("follower", " ".join(scored["reasons"]).lower())
        spam = disc.score_candidate(small, _posts(SPAM), TOPICS, now=NOW)
        self.assertFalse(spam["keep"])
        self.assertIn("crypto/engagement-farm posts", spam["reasons"])
        large = _user(2, fol=5000, fing=30000)
        large_score = disc.score_candidate(large, _posts(GOOD, GOOD), TOPICS, now=NOW)
        self.assertEqual(large_score["judged_by"], "size_and_posts")
        self.assertEqual(large_score["size_penalty"], 2)
        missing = disc.account_size({"id": "3", "username": "m"})
        self.assertEqual(missing["followers"], vf.NA)
        self.assertEqual(missing["judged_by"], vf.NA)
        self.assertEqual(missing["size_penalty"], 0)

    def test_deep_dive_does_not_penalize_low_followers_removal_check_still_flags_them(self):
        user = {"id": "9", "username": "small", "name": "Small", "verified": True,
                "created_at": "2020-01-01T00:00:00Z", "description": "builder",
                "public_metrics": {"followers_count": 12, "following_count": 20, "tweet_count": 80}}

        def ago(d):
            return (NOW - dt.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")

        text = "Shipped a rust agent workflow that triages inbox mail, here is how I built the queue step by step"
        posts = [{"id": str(i), "created_at": ago(i % 6), "text": f"{text} #{i}", "conversation_id": str(i)}
                 for i in range(1, 30)]

        def call(_tool, _args):
            return {"data": posts, "meta": {}}

        result = dd.deep_dive(user, call, "owner", now=NOW)
        self.assertTrue(all("very low followers" not in label.lower() for label, _pts in result["factors"]))
        person = cl.person_check(cl.Candidate(user_id="9", username="small", name="Small", followers_count=12,
                                              following_count=20, post_count=80))
        self.assertIn("Very low followers", person["bot_number_signs"])

    def test_history_is_append_only_isolated_and_not_a_rejection_for_silence(self):
        root = self.root
        sh.record_suggested("owner-a", [{"user_id": "1", "handle": "one"}], source="interest_discovery",
                            run_id="r1", root=root, now=NOW)
        first = sh.history_path("owner-a", root).read_text(encoding="utf-8").splitlines()[0]
        sh.record_card_result("owner-a", "r1", ["1"], [], {"1": "one"}, declined=False,
                              source="interest_discovery", root=root, now=NOW)
        lines = sh.history_path("owner-a", root).read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], first)
        second = json.loads(lines[1])
        self.assertEqual(second["event"], "NOT_PICKED")
        self.assertEqual(second["prev_entry_sha256"], hashlib.sha256(lines[0].encode()).hexdigest())
        quiet = sh.account_status("owner-a", "1", now=NOW, root=root)
        self.assertEqual(quiet["status"], "not_picked")
        self.assertIs(quiet["rejection"], False)
        self.assertIs(quiet["blocked"], True)
        self.assertIs(sh.account_status("owner-a", "1", now=LATER, root=root)["blocked"], False)
        self.assertEqual(sh.blocked_ids("owner-b", now=NOW, root=root), set())
        self.assertFalse(sh.history_path("owner-b", root).exists())

        sh.record_suggested("owner-a", [{"user_id": "2", "handle": "two"}], source="interest_discovery",
                            run_id="r2", root=root, now=NOW)
        sh.record_card_result("owner-a", "r2", ["2"], [], {"2": "two"}, declined=True,
                              source="interest_discovery", root=root, now=NOW)
        declined = sh.account_status("owner-a", "2", now=LATER, root=root)
        self.assertEqual(declined["status"], "declined")
        self.assertIs(declined["rejection"], True)
        self.assertIs(declined["blocked"], True)

        sh.record_followed("owner-a", "3", "three", run_id="r3", root=root, now=NOW)
        followed = sh.account_status("owner-a", "3", now=LATER, root=root)
        self.assertEqual(followed["status"], "followed")
        self.assertIs(followed["rejection"], False)
        self.assertIs(followed["blocked"], True)
        sh.record_no_follow_back("owner-a", "3", "three", run_id="r4", root=root, now=NOW)
        waiting = sh.account_status("owner-a", "3", now=NOW, root=root)
        self.assertEqual(waiting["status"], "no_follow_back")
        self.assertIs(waiting["rejection"], False)
        self.assertIs(waiting["blocked"], True)
        self.assertIs(sh.account_status("owner-a", "3", now=LATER, root=root)["blocked"], False)

    def test_confirmed_follow_is_logged_failed_follow_is_not(self):
        profs = {"2": {"handle": "@two"}, "1": {"handle": "@one"}}
        vf.record_selection("owner-a", "run", 1, ["1", "2"], ["2"], profs, root=self.root)
        vf.record_follow("owner-a", "run", "2", "@two", True, verified_following=True, root=self.root)
        vf.record_follow("owner-a", "run", "2", "@two", False, verified_following=False, root=self.root)
        events = [e["event"] for e in sh.read_events("owner-a", self.root) if e.get("user_id") == "2"]
        self.assertEqual(events, ["FOLLOWED"])
        self.assertEqual(sh.account_status("owner-a", "2", now=NOW, root=self.root)["status"], "followed")

    def test_discovery_returns_every_qualified_pick_and_does_not_research_again(self):
        users = [_user(i, bio="rust dev") for i in range(1, 8)]
        users.append(_user("spam", bio="signals"))
        fetched = []

        def search(_q):
            return {"includes": {"users": users}}

        def fetch(uid):
            fetched.append(str(uid))
            text = SPAM if str(uid) == "spam" else GOOD
            return _posts(text, QUESTION if str(uid) != "spam" else text)

        out = disc.run_discovery(TOPICS, search=search, fetch_posts=fetch, user_key="owner-a", root=self.root, now=NOW)
        self.assertEqual(out["qualified_ids"], [str(i) for i in range(1, 8)])
        self.assertEqual(out["offered"], 7)
        self.assertEqual(len(out["cards"]), 2)
        self.assertEqual(len(out["cards"][0]["account_ids"]), 5)
        self.assertEqual(len(out["cards"][1]["account_ids"]), 2)
        self.assertNotIn("spam", out["qualified_ids"])
        self.assertNotIn("uspam", json.dumps(out["cards"]))
        passes = [e for e in sh.read_events("owner-a", self.root) if e["event"] == "RESEARCH_PASS"]
        self.assertEqual(passes[0]["qualified_ids"], out["qualified_ids"])
        self.assertEqual(passes[0]["checked"], 8)
        fetched2 = []
        again = disc.run_discovery(TOPICS, search=search, fetch_posts=lambda uid: fetched2.append(uid) or _posts(GOOD),
                                   user_key="owner-a", root=self.root, now=NOW)
        self.assertEqual(fetched2, [])
        self.assertEqual(again["offered"], 0)
        self.assertEqual(again["note"], sh.ALREADY_SUGGESTED_NOTE)
        # not a rejection: after the cooldown the same accounts can be researched again
        fetched3 = []
        back = disc.run_discovery(TOPICS, search=search,
                                  fetch_posts=lambda uid: fetched3.append(str(uid)) or _posts(GOOD, QUESTION),
                                  user_key="owner-a", root=self.root, now=LATER)
        self.assertEqual(set(fetched3), {str(i) for i in range(1, 8)})
        self.assertNotIn("spam", fetched3)
        self.assertEqual(back["offered"], 7)

    def test_explicit_decline_does_not_come_back(self):
        user = _user(1, bio="rust dev")

        def search(_q):
            return {"includes": {"users": [user]}}

        out = disc.run_discovery(TOPICS, search=search, fetch_posts=lambda _i: _posts(GOOD, QUESTION),
                                 user_key="owner-a", root=self.root, now=NOW)
        declined = disc.validate_pending_selection("owner-a", out["run_id"], {1: [sh.DECLINE_ALL]}, root=self.root)
        self.assertEqual(declined, {"picked": []})
        status = sh.account_status("owner-a", "1", now=LATER, root=self.root)
        self.assertIs(status["rejection"], True)
        self.assertIs(status["blocked"], True)
        fetched = []
        again = disc.run_discovery(TOPICS, search=search, fetch_posts=lambda i: fetched.append(i) or _posts(GOOD),
                                   user_key="owner-a", root=self.root, now=LATER)
        self.assertEqual(fetched, [])
        self.assertEqual(again["offered"], 0)
        # another owner is not affected
        other = disc.run_discovery(TOPICS, search=search, fetch_posts=lambda _i: _posts(GOOD, QUESTION),
                                   user_key="owner-b", root=self.root, now=NOW)
        self.assertEqual(other["qualified_ids"], ["1"])

    def test_size_discovery_ratio_replies_and_one_pass(self):
        good = [_user(i, fol=150, fing=100, bio="rust dev") for i in range(1, 8)]
        huge = _user("huge", fol=20000, fing=100, bio="rust dev")
        ratio = _user("ratio", fol=200, fing=5000, bio="rust dev")
        gm = _user("gm", fol=180, fing=90, bio="rust dev")
        missing = _user("miss", metrics=False, bio="rust dev")
        bait = _user("bait", fol=160, fing=80, bio="rust dev")
        users = good + [huge, ratio, gm, missing, bait]

        def search(_q):
            return {"includes": {"users": users}}

        def fetch(uid):
            return _posts(SPAM if uid == "bait" else GOOD, QUESTION if uid != "bait" else SPAM)

        def replies(uid):
            if uid == "gm":
                return {"data": [{"text": "gm"}, {"text": "gn fam"}]}
            if uid == "miss":
                return None
            return {"data": [{"text": REPLY}]}

        out = disc.run_size_discovery(TOPICS, owner_followers=200, search=search, fetch_posts=fetch,
                                      fetch_replies=replies, user_key="owner-a", root=self.root, now=NOW)
        self.assertEqual(out["method"], "interest_and_size")
        self.assertEqual(out["qualified_ids"], [str(i) for i in range(1, 8)])
        self.assertEqual(out["offered"], 7)
        self.assertEqual(len(out["cards"]), 2)
        self.assertTrue(out["cards"][0]["title"].startswith(disc.SIZE_CARD_TITLE))
        self.assertTrue(all(len(c["account_ids"]) <= 5 for c in out["cards"]))
        blob = json.dumps(out["cards"])
        for name in ("uhuge", "uratio", "ugm", "umiss", "ubait"):
            self.assertNotIn(name, blob)
        skipped = json.loads((self.root / "data/users/owner-a/interest_discovery/skipped.json").read_text())
        self.assertEqual(set(skipped["ids"]), {"bait"})
        held = {e["user_id"] for e in sh.read_events("owner-a", self.root) if e["event"] == "CHECKED"}
        self.assertTrue({"huge", "ratio", "gm", "miss"} <= held)
        self.assertIs(sh.account_status("owner-a", "huge", now=NOW, root=self.root)["rejection"], False)
        none = disc.run_size_discovery(TOPICS, owner_followers=None, search=search, fetch_posts=fetch,
                                       fetch_replies=replies, user_key="owner-a", root=self.root, now=NOW)
        self.assertEqual(none["note"], disc.OWNER_SIZE_NOTE)
        self.assertEqual(none["checked"], 0)
        self.assertEqual(none["cards"], [])
        posts2, replies2 = [], []
        again = disc.run_size_discovery(
            TOPICS, owner_followers=200, search=search,
            fetch_posts=lambda uid: posts2.append(uid) or _posts(GOOD),
            fetch_replies=lambda uid: replies2.append(uid) or {"data": [{"text": REPLY}]},
            user_key="owner-a", root=self.root, now=NOW)
        self.assertEqual(posts2, [])
        self.assertEqual(replies2, [])
        self.assertEqual(again["offered"], 0)
        # missing reply read is Not available, not a guess that they are genuine
        judged = disc.reply_judgment([], available=False)
        self.assertEqual(judged["status"], vf.NA)
        self.assertIsNone(judged["genuine"])
        self.assertEqual(disc.reply_judgment([], available=True)["status"], "None found")


if __name__ == "__main__":
    unittest.main()
