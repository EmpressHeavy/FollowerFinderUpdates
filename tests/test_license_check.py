"""Subscription gate. Synthetic install ids only. No X profiles or Stripe network calls."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src import daily_review as dr
from src import follow_executor as fx
from src import interest_discovery as disc
from src import license_check as lc
from src import removal_runner as rr

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
PRICE = {"label": "Follower Finder", "amount_cents": 449, "currency": "usd", "interval": "month",
         "display": "4.49 USD/month"}


def write_cfg(root: Path, **over):
    cfg = {"product": "ff", "base_url": "https://license.example", "grace_hours": 72, "timeout_seconds": 5}
    cfg.update(over)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "license.json").write_text(json.dumps(cfg), encoding="utf-8")


class LicenseCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_and_check_send_an_explicit_user_agent(self):
        """urllib's default User-Agent is blocked by Cloudflare error 1010 on the license Worker."""
        write_cfg(self.root)
        seen = []

        class _Body:
            def __init__(self, status, raw):
                self.status = status
                self._raw = raw

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=None):
            seen.append(req.get_header("User-agent"))
            if req.full_url.endswith("/register"):
                return _Body(201, json.dumps({"install_id": "e" * 32}).encode())
            return _Body(200, json.dumps({
                "active": True, "product": "ff", "expires": None,
                "renew_url": "https://license.example/pay", "price": PRICE,
            }).encode())

        with mock.patch.object(lc.urllib.request, "urlopen", fake_urlopen):
            self.assertTrue(lc.require("scan", root=self.root, now=NOW)["allow"])
        expected = f"FollowerFinder/{(ROOT / 'VERSION').read_text(encoding='utf-8').strip()}"
        self.assertEqual(seen, [expected, expected])
        self.assertFalse(any(str(ua).startswith("Python-urllib") for ua in seen))

    def test_source_does_not_hardcode_prices(self):
        text = (ROOT / "src" / "license_check.py").read_text(encoding="utf-8")
        for needle in ("4.49", "8.99", "449", "899"):
            self.assertNotIn(needle, text)

    def test_shipped_config_points_at_the_live_server(self):
        cfg = json.loads((ROOT / "config" / "license.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["product"], "ff")
        self.assertEqual(cfg["base_url"], "https://empress-license.empressheavy.workers.dev")
        self.assertEqual(cfg["grace_hours"], 72)

    def test_inactive_refuses_and_keeps_the_renew_link(self):
        write_cfg(self.root)
        seen = {}

        def transport(url, payload):
            seen["url"] = url
            seen["payload"] = payload
            if url.endswith("/register"):
                return 201, json.dumps({"install_id": "a" * 32}).encode()
            return 200, json.dumps({
                "active": False, "product": "ff", "expires": None,
                "renew_url": "https://license.example/pay?install_id=" + "a" * 32,
                "price": PRICE,
            }).encode()

        with self.assertRaises(lc.LicenseError) as caught:
            lc.require("scan", root=self.root, now=NOW, transport=transport)
        message = str(caught.exception)
        self.assertIn("isn't active", message)
        self.assertIn("records are still here", message)
        self.assertIn("X login", message)
        self.assertIn("4.49 USD/month", message)
        pay = "https://license.example/pay?install_id=" + ("a" * 32) + "&product=ff"
        self.assertIn(pay, message)
        self.assertEqual(seen["payload"], {"install_id": "a" * 32, "product": "ff"})
        self.assertNotIn("both", seen["payload"].values())
        state = json.loads((self.root / "data" / "install_license.json").read_text(encoding="utf-8"))
        self.assertEqual(set(state), {"install_id", "last_active_at", "last_renew_url", "last_price"})
        self.assertEqual(state["last_renew_url"], pay)
        self.assertIsNone(state["last_active_at"])
        self.assertNotIn("handle", json.dumps(state))

    def test_active_ff_answer_allows_the_action(self):
        """A both subscription is the server's job: it answers active=true for product ff. The client trusts that."""
        write_cfg(self.root)

        def transport(url, payload):
            if url.endswith("/register"):
                self.assertEqual(payload, {})
                return 201, json.dumps({"install_id": "f" * 32}).encode()
            self.assertEqual(payload, {"install_id": "f" * 32, "product": "ff"})
            return 200, json.dumps({"active": True, "product": "ff", "expires": None,
                                    "renew_url": "https://license.example/pay", "price": PRICE}).encode()

        decision = lc.require("follow", root=self.root, now=NOW, transport=transport)
        self.assertTrue(decision["allow"])
        self.assertEqual(decision["reason"], "active")

    def test_active_check_then_offline_grace_then_lapse(self):
        write_cfg(self.root, grace_hours=72)

        def ok(url, payload):
            if url.endswith("/register"):
                return 201, b'{"install_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
            return 200, json.dumps({"active": True, "product": "ff", "expires": None,
                                    "renew_url": "https://license.example/pay?install_id=x", "price": PRICE}).encode()

        self.assertTrue(lc.require("recommend", root=self.root, now=NOW, transport=ok)["allow"])

        def down(url, payload):
            raise OSError("offline")

        self.assertEqual(lc.require("follow", root=self.root, now=NOW + timedelta(hours=71), transport=down)["reason"],
                         "grace")
        with self.assertRaises(lc.LicenseError) as caught:
            lc.require("write", root=self.root, now=NOW + timedelta(hours=73), transport=down)
        self.assertIn("more than 72 hours", str(caught.exception))
        self.assertIn("records are still here", str(caught.exception))

    def test_inactive_is_not_kept_alive_by_an_older_grace_window(self):
        write_cfg(self.root)
        calls = {"n": 0}

        def transport(url, payload):
            calls["n"] += 1
            if url.endswith("/register"):
                return 201, b'{"install_id":"cccccccccccccccccccccccccccccccc"}'
            if calls["n"] == 2:
                return 200, json.dumps({"active": True, "renew_url": "https://license.example/pay", "price": PRICE}).encode()
            return 200, json.dumps({"active": False, "renew_url": "https://license.example/pay", "price": PRICE}).encode()

        lc.require("scan", root=self.root, now=NOW, transport=transport)
        with self.assertRaises(lc.LicenseError) as caught:
            lc.require("scan", root=self.root, now=NOW + timedelta(minutes=5), transport=transport)
        self.assertIn("isn't active", str(caught.exception))
        state = lc.load_state(self.root)
        self.assertIsNone(state["last_active_at"])

    def test_install_id_lives_outside_the_replaced_app_directory(self):
        app = self.root / "app"
        write_cfg(app)

        def transport(url, payload):
            if url.endswith("/register"):
                return 201, json.dumps({"install_id": "d" * 32}).encode()
            return 200, json.dumps({"active": True, "product": "ff", "renew_url": "https://license.example/pay",
                                    "price": PRICE}).encode()

        lc.require("scan", root=app, now=NOW, transport=transport)
        self.assertTrue((self.root / "data" / "install_license.json").is_file())
        self.assertFalse((app / "data" / "install_license.json").exists())

    def test_refusal_does_not_delete_local_records(self):
        write_cfg(self.root, base_url="")
        kept = self.root / "data" / "users" / "demo_user" / "follow_back.jsonl"
        kept.parent.mkdir(parents=True)
        kept.write_text('{"event":"keep"}\n', encoding="utf-8")
        with self.assertRaises(lc.LicenseError) as caught:
            lc.require("follow", root=self.root, now=NOW)
        self.assertIn("records are still here", str(caught.exception))
        self.assertIn("https://empressheavy.com/subscribe/", str(caught.exception))
        self.assertEqual(kept.read_text(encoding="utf-8"), '{"event":"keep"}\n')

    def test_removal_runner_refuses_before_any_x_work(self):
        write_cfg(self.root, base_url="")

        class Adapter:
            def lookup(self, account):
                raise AssertionError("lookup")

            def remove(self, account):
                raise AssertionError("remove")

        with self.assertRaises(rr.RefusedError) as caught:
            rr.run_removals("demo_user", "snap", Adapter(), root=self.root)
        self.assertIn("records are still here", str(caught.exception))

    def test_scan_recommend_and_follow_refuse_before_any_x_call(self):
        write_cfg(self.root, base_url="")

        class X:
            def call(self, *args, **kwargs):
                raise AssertionError("x called")

        with self.assertRaises(lc.LicenseError) as caught:
            dr.run_daily_review("demo_user", X(), object(), root=self.root)
        self.assertIn("records are still here", str(caught.exception))

        def boom(*args, **kwargs):
            raise AssertionError("called")

        with self.assertRaises(lc.LicenseError):
            disc.run_discovery(["rust"], search=boom, fetch_posts=boom, root=self.root)
        with self.assertRaises(lc.LicenseError):
            disc.run_size_discovery(["rust"], owner_followers=200, search=boom, fetch_posts=boom, root=self.root)
        with self.assertRaises(lc.LicenseError):
            fx.run("demo_user", "r1", [{"user_id": "1", "handle": "@a"}], boom, root=self.root)
        with self.assertRaises(lc.LicenseError):
            fx.FollowSession("demo_user", "r1", [{"user_id": "1", "handle": "@a"}], root=self.root)

    def test_instructions_make_the_check_mandatory(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        steps = (ROOT / "src" / "daily_routine.py").read_text(encoding="utf-8")
        review = (ROOT / "src" / "verified_followers.py").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "getting_started_daily_review.md").read_text(encoding="utf-8")
        lists = (ROOT / "docs" / "list_runbook.md").read_text(encoding="utf-8")
        for name in ("scan", "recommend", "follow", "write"):
            self.assertIn(f"python3 -m src.license_check require {name}", skill)
        self.assertIn("do not delete", skill.lower())
        self.assertIn("license_check require scan", steps)
        self.assertIn("license_check require scan", review)
        self.assertIn("license_check require follow", review)
        self.assertIn("license_check require scan", guide)
        self.assertIn("license_check require write", lists)

    def test_usage(self):
        self.assertEqual(lc.main(["nope"]), 2)


if __name__ == "__main__":
    unittest.main()
