"""v3.5.40: Follower Finder is free. No gate module, config, or check step remains."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FreeForEveryone(unittest.TestCase):
    def test_gate_files_are_gone(self):
        self.assertFalse((ROOT / "src" / "license_check.py").exists())
        self.assertFalse((ROOT / "config" / "license.json").exists())

    def test_no_code_calls_a_gate(self):
        for path in (ROOT / "src").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("license_check", text, path.name)

    def test_routines_have_no_check_step(self):
        from src import daily_routine, setup_flow, verified_followers
        steps = daily_routine.scan_steps({}) + verified_followers.daily_steps({})
        self.assertFalse(any("license" in s or "Subscribe" in s for s in steps))
        for spec in setup_flow.routine_specs(None, "America/Los_Angeles"):
            self.assertNotIn("before", spec)
            self.assertFalse(any("license" in d for d in spec.get("does", [])))

    def test_skill_never_asks_to_pay(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("free for everyone", skill)
        self.assertNotIn("[Subscribe]", skill)
        self.assertNotIn("$4.49", skill)


if __name__ == "__main__":
    unittest.main()
