"""v3.5.37: an update must not touch user data, and X ids are not card numbers.

No live X calls. The follow batch is driven through FollowSession with an injected browser report.
"""
import hashlib
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from src import follow_executor as fx
from src import memory as mem
from src import updater as u
from src import verified_followers as vf

SNOW = "2115856401167736833"  # 19-digit X id that passes Luhn (@grokbotspot)
CARD = "4111111111111111"
OWNER = "owner-a"


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _events(root, user=OWNER):
    path = vf._dir(user, root) / "follow_back.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _fetcher(ver, files):
    def _tar(filemap):
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            for n, d in filemap.items():
                data = d.encode()
                ti = tarfile.TarInfo("FollowerFinderUpdates-main/" + n)
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
        return b.getvalue()

    blob = _tar(files)

    def f(url, timeout=30):
        return (ver + "\n").encode() if url == u.VERSION_URL else blob
    return f


def _run_batch(root, picks, run_id):
    ids = [str(p["user_id"]) for p in picks]
    profs = {i: {"handle": "@" + p["handle"].lstrip("@")} for i, p in zip(ids, picks)}
    vf.record_selection(OWNER, run_id, 1, ids, ids, profs, root=root)
    report = {"signed_in": True, "profile_matches": True, "clicked": True,
              "button_before": "Follow", "button_after": "Following", "stop": None, "error": None}
    return fx.run_browser(
        OWNER, run_id, picks, lambda _item: report, x=None, sleep=lambda _s: None, rand=lambda _a, _b: 0,
        skip=lambda _p: None, root=root, owner_handle="owner", vault_saved=False,
        preflight=lambda _task: {"signed_in": True, "signed_in_as": "owner"})


class UpdaterAndSecretsV3537(unittest.TestCase):
    def test_id_fields_are_not_cards_but_free_text_cards_are(self):
        mem.check_no_secret({"user_id": SNOW, "tweet_id": SNOW, "ids": [SNOW, 42], "id": int(SNOW)})
        mem.check_no_secret('{"user_id": "%s", "note": "followed back"}' % SNOW)
        mem.check_no_secret({"author_id": SNOW, "handle": "grokbotspot"})
        with self.assertRaises(mem.SecretRejected):
            mem.check_no_secret("please charge %s today" % CARD)
        with self.assertRaises(mem.SecretRejected):
            mem.check_no_secret("4111 1111 1111 1111")
        with self.assertRaises(mem.SecretRejected):
            mem.check_no_secret('{"user_id": "%s", "note": "card %s"}' % (SNOW, CARD))
        with self.assertRaises(mem.SecretRejected):
            mem.check_no_secret(CARD)

    def test_finish_writes_snowflake_ids_and_does_not_half_write(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        picks = [
            {"user_id": SNOW, "handle": "grokbotspot", "follows_user": True},
            {"user_id": "42", "handle": "otheracct", "follows_user": True},
        ]
        out = _run_batch(root, picks, "run")
        self.assertEqual(out["followed_ids"], [SNOW, "42"])
        logged = [e for e in _events(root) if e["event"] in ("FOLLOWED", "FOLLOW_VERIFIED")]
        self.assertEqual([e["user_id"] for e in logged if e["event"] == "FOLLOWED"], [SNOW, "42"])
        self.assertEqual([e["user_id"] for e in logged if e["event"] == "FOLLOW_VERIFIED"], [SNOW, "42"])
        db = mem.db_path(OWNER, root)
        rows = sqlite3.connect(db).execute("SELECT user_id FROM tracked_follow ORDER BY user_id").fetchall()
        self.assertIn(SNOW, {r[0] for r in rows})

        bad = [
            {"user_id": "7", "handle": "firstacct", "follows_user": True},
            {"user_id": "8", "handle": CARD, "follows_user": True},
        ]
        before = _events(root)
        with self.assertRaises(mem.SecretRejected):
            _run_batch(root, bad, "r2")
        after = _events(root)
        added = after[len(before):]
        self.assertEqual([e["event"] for e in added].count("FOLLOWED"), 0)
        self.assertEqual([e["event"] for e in added].count("FOLLOW_VERIFIED"), 0)
        ids = {r[0] for r in sqlite3.connect(db).execute("SELECT user_id FROM tracked_follow")}
        self.assertNotIn("7", ids)
        self.assertNotIn("8", ids)

    def test_apply_keeps_user_data_byte_for_byte_and_does_not_clobber_seeds(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = tmp.name
        os.makedirs(os.path.join(h, "app"))
        with open(os.path.join(h, "app", "VERSION"), "w", encoding="utf-8") as f:
            f.write("v3.5.36")
        with open(os.path.join(h, "app", "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("old")
        with open(os.path.join(h, "brain.db"), "wb") as f:
            f.write(b"home-brain")

        log = os.path.join(h, "app", "data", "users", "alice", "verified_review", "follow_back.jsonl")
        os.makedirs(os.path.dirname(log))
        payload = b'{"event":"FOLLOWED","user_id":"%s"}\xff\xfe' % SNOW.encode()
        with open(log, "wb") as f:
            f.write(payload)
        brain = os.path.join(h, "app", "data", "users", "alice", "memory", "brain.db")
        os.makedirs(os.path.dirname(brain))
        with open(brain, "wb") as f:
            f.write(b"SQLite format 3\x00user-bytes-not-text")
        with open(os.path.join(h, "app", "brain.db"), "wb") as f:
            f.write(b"app-root-brain")
        os.makedirs(os.path.join(h, "app", "memory"))
        note = os.path.join(h, "app", "memory", "note.txt")
        with open(note, "wb") as f:
            f.write(b"keep-me")
        before = {p: _sha(p) for p in (log, brain, os.path.join(h, "app", "brain.db"), note)}

        files = {
            "VERSION": "v3.5.37\n",
            "SKILL.md": "skill v3.5.37",
            "src/x.py": "X=2",
            "data/users/alice/verified_review/follow_back.jsonl": "SEED-CLOBBER",
            "data/users/alice/new_seed.txt": "seed-ok",
            "data/defaults/readme.txt": "seed-readme",
        }
        result = u.apply(h, _fetcher("v3.5.37", files))
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["to"], "v3.5.37")
        with open(os.path.join(h, "app", "SKILL.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "skill v3.5.37")
        with open(log, "rb") as f:
            self.assertEqual(f.read(), payload)
        with open(brain, "rb") as f:
            self.assertEqual(f.read(), b"SQLite format 3\x00user-bytes-not-text")
        with open(os.path.join(h, "app", "brain.db"), "rb") as f:
            self.assertEqual(f.read(), b"app-root-brain")
        with open(note, "rb") as f:
            self.assertEqual(f.read(), b"keep-me")
        self.assertEqual({p: _sha(p) for p in before}, before)
        with open(os.path.join(h, "app", "data", "users", "alice", "new_seed.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "seed-ok")
        with open(os.path.join(h, "app", "data", "defaults", "readme.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "seed-readme")
        with open(os.path.join(h, "brain.db"), "rb") as f:
            self.assertEqual(f.read(), b"home-brain")
        backed_log = os.path.join(result["user_data_backup"], "data", "users", "alice",
                                  "verified_review", "follow_back.jsonl")
        with open(backed_log, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_failed_update_restores_user_data_bytes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = tmp.name
        os.makedirs(os.path.join(h, "app"))
        with open(os.path.join(h, "app", "VERSION"), "w", encoding="utf-8") as f:
            f.write("v3.5.36")
        with open(os.path.join(h, "app", "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("old")
        log = os.path.join(h, "app", "data", "users", "alice", "verified_review", "follow_back.jsonl")
        os.makedirs(os.path.dirname(log))
        payload = b"row-one\nrow-two\n" + SNOW.encode()
        with open(log, "wb") as f:
            f.write(payload)
        result = u.apply(h, _fetcher("v3.5.37", {"VERSION": "v9.9.9", "SKILL.md": "evil"}))
        self.assertEqual(result["status"], "rolled_back")
        with open(log, "rb") as f:
            self.assertEqual(f.read(), payload)
        with open(os.path.join(h, "app", "SKILL.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "old")

    def test_merge_seeds_does_not_open_existing_files_for_write(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        src = root / "release" / "data"
        dst = root / "app" / "data"
        (src / "users" / "alice").mkdir(parents=True)
        (dst / "users" / "alice").mkdir(parents=True)
        (src / "users" / "alice" / "follow_back.jsonl").write_text("SEED", encoding="utf-8")
        (dst / "users" / "alice" / "follow_back.jsonl").write_bytes(b"USER\xff")
        (src / "fresh.txt").write_text("new", encoding="utf-8")
        self.assertEqual(u.merge_seeds(str(src), str(dst)), 1)
        self.assertEqual((dst / "users" / "alice" / "follow_back.jsonl").read_bytes(), b"USER\xff")
        self.assertEqual((dst / "fresh.txt").read_text(encoding="utf-8"), "new")


if __name__ == "__main__":
    unittest.main()
