"""v3.5.41: optional tip counter and cadence.

First prompt after 10 completed uses, then at most once every 7 days.
Maybe later keeps that cadence. Remind me monthly, or dismissing the prompt,
switches it to at most once every 30 days. No forever off. Never during first-run setup.
The prompt does not block a scan.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from src import daily_routine
from src import memory as mem
from src import tip_prompt as tip
from src import updater
from src import verified_followers as vf

ROOT = Path(__file__).resolve().parents[1]
USER = "tip-owner"
T0 = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
PROMPT = "Wow you seem to be using Follower Finder a lot lately! Would you mind sending my creator a tip? 🪙"


def _at(days: float) -> datetime:
    return T0 + timedelta(days=days)


class TipCadence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _use(self, n=1, *, day=0, during_setup=False, run_id=None, user=USER):
        last = None
        for i in range(n):
            token = run_id if run_id is not None else f"d{day}-u{i}"
            last = tip.record_use(user, root=self.root, now=_at(day), during_setup=during_setup,
                                  run_id=token, kind="scan")
        return last

    def test_quiet_until_the_tenth_use_then_the_exact_prompt(self):
        early = self._use(9)
        self.assertFalse(early["show"])
        self.assertEqual(early["reason"], "not_yet")
        self.assertEqual(early["uses"], 9)
        self.assertEqual(early["interval_days"], 7)
        self.assertFalse(early["blocks"])
        shown = self._use(1, run_id="tenth")
        self.assertTrue(shown["show"])
        self.assertEqual(shown["reason"], "due")
        self.assertEqual(shown["uses"], 10)
        self.assertEqual(shown["prompt"], PROMPT)
        self.assertEqual(shown["prompt"], tip.PROMPT)
        self.assertEqual([item["url"] for item in shown["links"]],
                         [tip.STRIPE_URL, tip.CASH_APP_URL, tip.VENMO_URL, tip.X_MONEY_URL])
        self.assertEqual(shown["links"][3]["handle"], "@HeavyMetalShip")
        labels = [opt["label"] for opt in shown["card"]["options"]]
        self.assertEqual(labels, ["Maybe later", "Remind me monthly"])
        self.assertNotIn("Don't ask again", labels)
        self.assertIn(tip.STRIPE_URL, shown["links_markdown"])
        self.assertIn("https://cash.app/$EmpressHeavy", shown["links_markdown"])
        self.assertFalse(shown["card"]["multiSelect"])
        self.assertFalse(shown["blocks"])

    def test_weekly_cadence_and_maybe_later_keeps_it(self):
        self._use(9)
        shown = self._use(1, run_id="first-prompt")
        self.assertTrue(shown["show"])
        answered = tip.answer(USER, "Maybe later", root=self.root, now=T0)
        self.assertEqual(answered["answer"], "later")
        self.assertEqual(answered["interval_days"], 7)
        self.assertFalse(answered["show"])
        again = self._use(1, day=6, run_id="day6")
        self.assertFalse(again["show"])
        self.assertEqual(again["reason"], "waiting")
        self.assertEqual(again["interval_days"], 7)
        nxt = self._use(1, day=7, run_id="day7")
        self.assertTrue(nxt["show"])
        self.assertEqual(nxt["interval_days"], 7)

    def test_remind_me_monthly_switches_to_thirty_days(self):
        self._use(10)
        out = tip.answer(USER, "Remind me monthly", root=self.root, now=T0)
        self.assertEqual(out["answer"], "monthly")
        self.assertEqual(out["interval_days"], 30)
        self.assertFalse(self._use(1, day=7, run_id="week")["show"])
        self.assertFalse(self._use(1, day=29, run_id="day29")["show"])
        again = self._use(1, day=30, run_id="day30")
        self.assertTrue(again["show"])
        self.assertEqual(again["interval_days"], 30)
        kept = tip.answer(USER, "Maybe later", root=self.root, now=_at(30))
        self.assertEqual(kept["interval_days"], 30)
        self.assertFalse(self._use(1, day=37, run_id="still-monthly")["show"])
        self.assertTrue(self._use(1, day=60, run_id="month-again")["show"])

    def test_dismiss_and_dont_ask_again_are_monthly_not_forever(self):
        self._use(10)
        dismissed = tip.answer(USER, "Don't ask again", root=self.root, now=T0)
        self.assertEqual(dismissed["answer"], "dismiss")
        self.assertEqual(dismissed["interval_days"], 30)
        state = tip.load_state(USER, self.root)
        self.assertNotIn("opted_out", state)
        self.assertFalse(self._use(3, day=14, run_id="soon")["show"])
        again = self._use(1, day=30, run_id="still-asks")
        self.assertTrue(again["show"])
        self.assertEqual(again["prompt"], PROMPT)
        closed = tip.answer(USER, "closed", root=self.root, now=_at(30))
        self.assertEqual(closed["answer"], "dismiss")
        self.assertEqual(closed["interval_days"], 30)

    def test_ignoring_an_open_prompt_counts_as_a_dismiss(self):
        self._use(10)
        self.assertTrue(tip.load_state(USER, self.root)["prompt_open"])
        nxt = self._use(1, day=1, run_id="moved-on")
        self.assertFalse(nxt["show"])
        state = tip.load_state(USER, self.root)
        self.assertEqual(state["interval_days"], 30)
        self.assertEqual(state["last_answer"], "dismiss")
        self.assertFalse(state["prompt_open"])
        self.assertFalse(self._use(1, day=29, run_id="day29")["show"])
        self.assertTrue(self._use(1, day=30, run_id="day30")["show"])

    def test_same_run_does_not_count_twice_or_change_cadence(self):
        self._use(9)
        first = self._use(1, run_id="same")
        self.assertTrue(first["show"])
        second = self._use(1, run_id="same")
        self.assertTrue(second["show"])
        self.assertEqual(second["uses"], 10)
        state = tip.load_state(USER, self.root)
        self.assertEqual(state["uses"], 10)
        self.assertEqual(state["interval_days"], 7)
        self.assertTrue(state["prompt_open"])
        tip.answer(USER, "Maybe later", root=self.root, now=T0)
        quiet = self._use(1, run_id="same")
        self.assertFalse(quiet["show"])
        self.assertEqual(quiet["reason"], "replay")
        self.assertEqual(tip.load_state(USER, self.root)["uses"], 10)
        self.assertEqual(tip.load_state(USER, self.root)["interval_days"], 7)

    def test_setup_does_not_count_or_show(self):
        tip.begin_setup(USER, root=self.root)
        quiet = self._use(12)
        self.assertFalse(quiet["show"])
        self.assertEqual(quiet["reason"], "setup")
        self.assertEqual(quiet["uses"], 0)
        forced = tip.record_use(USER, root=self.root, now=T0, during_setup=True, run_id="forced")
        self.assertEqual(forced["reason"], "setup")
        self.assertEqual(tip.load_state(USER, self.root)["uses"], 0)
        tip.finish_setup(USER, root=self.root)
        self.assertEqual(self._use(9)["uses"], 9)
        shown = self._use(1, run_id="after-setup")
        self.assertTrue(shown["show"])
        self.assertEqual(shown["uses"], 10)

    def test_corrupt_file_does_not_block_the_next_use(self):
        path = tip.state_path(USER, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        out = tip.record_use(USER, root=self.root, now=T0, run_id="recovered")
        self.assertFalse(out["show"])
        self.assertEqual(out["uses"], 1)
        self.assertFalse(out["blocks"])
        self.assertEqual(tip.note_completed("Bad Key", root=self.root)["reason"], "error")
        saved = tip.state_path(USER, self.root)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    def test_unknown_interval_cannot_silence_the_prompt(self):
        tip.record_use(USER, root=self.root, now=T0, run_id="seed")
        path = tip.state_path(USER, self.root)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["uses"] = 40
        raw["interval_days"] = 99999
        raw["opted_out"] = True
        raw["last_prompt_at"] = None
        path.write_text(json.dumps(raw), encoding="utf-8")
        out = tip.record_use(USER, root=self.root, now=_at(1), run_id="unblocked")
        self.assertTrue(out["show"])
        self.assertEqual(out["interval_days"], 7)
        self.assertNotIn("opted_out", tip.load_state(USER, self.root))


class TipFilesAndSkill(unittest.TestCase):
    def test_qr_png_is_the_stripe_link(self):
        path = tip.qr_path()
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), "tip QR must be a real PNG in the repo")
        width, height = struct.unpack(">II", raw[16:24])
        self.assertGreaterEqual(width, 100)
        self.assertGreaterEqual(height, 100)
        self.assertEqual(tip.presentation()["qr_url"], tip.STRIPE_URL)
        if shutil.which("zbarimg"):
            decoded = subprocess.check_output(["zbarimg", "--raw", str(path)], text=True, stderr=subprocess.DEVNULL)
            self.assertEqual(decoded.strip(), tip.STRIPE_URL)

    def test_skill_version_and_latest_only_updater(self):
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "v3.5.41")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## v3.5.41", changelog)
        self.assertIn(PROMPT, changelog)
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("free for everyone", skill)
        self.assertIn(PROMPT, skill)
        self.assertIn("Remind me monthly", skill)
        self.assertIn("Maybe later", skill)
        self.assertIn(tip.STRIPE_URL, skill)
        self.assertIn("https://cash.app/$EmpressHeavy", skill)
        self.assertIn("https://venmo.com/u/EmpressHeavy", skill)
        self.assertIn("https://x.com/HeavyMetalShip", skill)
        self.assertIn("no forever off", skill.lower())
        self.assertIn("latest VERSION on main", skill)
        self.assertNotIn("[Subscribe]", skill)
        self.assertNotIn("$4.49", skill)
        self.assertIn("assets", updater.APP_ITEMS)
        self.assertTrue(updater.VERSION_URL.endswith("/main/VERSION"))
        self.assertIn("refs/heads/main", updater.TARBALL_URL)
        self.assertNotIn("--version", updater.main.__doc__ or "")
        steps = daily_routine.scan_steps({}) + vf.daily_steps({})
        self.assertTrue(any("tip_prompt" in step for step in steps))
        self.assertFalse(any("license" in step or "Subscribe" in step for step in steps))
        self.assertFalse(any("Don't ask again" in step for step in steps))

    def test_update_copies_the_prebuilt_qr(self):
        import io
        import tarfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = tmp.name
        os.makedirs(os.path.join(home, "app"))
        with open(os.path.join(home, "app", "VERSION"), "w", encoding="utf-8") as handle:
            handle.write("v3.5.40\n")
        with open(os.path.join(home, "app", "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("old")
        png = (ROOT / "assets" / "tip-qr-stripe.png").read_bytes()
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w:gz") as archive:
            files = {"VERSION": b"v3.5.41\n", "SKILL.md": b"new skill", "assets/tip-qr-stripe.png": png}
            for name, data in files.items():
                info = tarfile.TarInfo("FollowerFinderUpdates-main/" + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        payload = blob.getvalue()

        def fetch(url, timeout=30):
            return b"v3.5.41\n" if url == updater.VERSION_URL else payload

        result = updater.apply(home, fetch)
        self.assertEqual(result["status"], "updated")
        copied = Path(home) / "app" / "assets" / "tip-qr-stripe.png"
        self.assertEqual(copied.read_bytes(), png)

    def test_cli_never_fails_a_caller_when_the_counter_cannot_count(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        buf = StringIO()
        with redirect_stdout(buf):
            code = tip.main(["record", "--user", "../nope", "--root", tmp.name, "--now", T0.isoformat()])
        self.assertEqual(code, 0)
        body = json.loads(buf.getvalue())
        self.assertFalse(body["show"])
        self.assertFalse(body["blocks"])
        self.assertEqual(body["reason"], "error")

    def test_cli_records_a_use(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        buf = StringIO()
        with redirect_stdout(buf):
            code = tip.main(["record", "--user", USER, "--root", tmp.name, "--run-id", "cli-1",
                             "--now", T0.isoformat()])
        self.assertEqual(code, 0)
        body = json.loads(buf.getvalue())
        self.assertEqual(body["uses"], 1)
        self.assertFalse(body["show"])
        self.assertEqual(oct(os.stat(tip.state_path(USER, tmp.name)).st_mode & 0o777), oct(0o600))


class TipHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_finished_review_counts_and_setup_does_not(self):
        from src import daily_review as dr
        mem.init_db(USER, self.root)
        tip.begin_setup(USER, root=self.root)

        class X:
            def call(self, tool, args):
                if tool == "get_users_me":
                    return {"data": {"id": "owner-1", "username": "owneracct"}}
                if tool == "get_users_me_followers":
                    return {"data": [{"id": "v1", "username": "verifiedone", "verified": True}], "meta": {}}
                return {"data": [], "meta": {}}

        class Browser:
            def block_check(self, users, steps):
                return {}

            def translate(self, requests, steps):
                return {}

        quiet = dr.run_daily_review(USER, X(), Browser(), now=T0, root=self.root,
                                    prefs={"allow_reply_heavy": False, "show_bought_risk": False,
                                           "read_languages": ["en"], "daily_review_time": "18:00"})
        self.assertTrue(quiet.get("baseline"))
        self.assertFalse(quiet["tip"]["show"])
        self.assertEqual(quiet["tip"]["reason"], "setup")
        self.assertEqual(quiet["tip"]["uses"], 0)
        self.assertFalse(quiet["tip"]["blocks"])
        tip.finish_setup(USER, root=self.root)
        counted = tip.record_use(USER, root=self.root, now=T0, run_id="after", kind="review")
        self.assertEqual(counted["uses"], 1)
        self.assertFalse(counted["blocks"])

    def test_a_discovery_counts_and_a_missing_size_does_not(self):
        from src import interest_discovery as disc
        missed = disc.run_size_discovery(["gardening"], owner_followers=None, search=lambda q: {},
                                         fetch_posts=lambda i: {}, user_key=USER, root=self.root, now=T0)
        self.assertNotIn("tip", missed)
        self.assertFalse(tip.state_path(USER, self.root).exists())
        found = disc.run_discovery(["gardening"], search=lambda q: {}, fetch_posts=lambda i: {},
                                   user_key=USER, root=self.root, now=T0)
        self.assertEqual(found["tip"]["uses"], 1)
        self.assertFalse(found["tip"]["show"])
        self.assertFalse(found["tip"]["blocks"])

    def test_attach_keeps_the_result_when_counting_fails(self):
        out = tip.attach({"run_id": "x", "cards": []}, "Bad Key", root=self.root, now=T0)
        self.assertEqual(out["cards"], [])
        self.assertFalse(out["tip"]["show"])
        self.assertEqual(out["tip"]["reason"], "error")


if __name__ == "__main__":
    unittest.main()
