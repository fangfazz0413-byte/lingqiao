import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("BRIDGE_HOME", str(ROOT / "tests" / "fixture-home"))
os.environ.setdefault("BRIDGE_REPO", str(ROOT))
zi = importlib.import_module("zcode_inject")
bs = importlib.import_module("bridge_state")
sync = importlib.import_module("sync")


def fixture_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT, slug TEXT,
            directory TEXT, path TEXT, title TEXT, version TEXT, permission TEXT,
            time_created INTEGER, time_updated INTEGER, task_type TEXT, title_source TEXT, trace_id TEXT);
        CREATE TABLE session_entry(id TEXT PRIMARY KEY, session_id TEXT REFERENCES session(id),
            type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT REFERENCES session(id),
            time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER);
        CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT REFERENCES message(id),
            session_id TEXT REFERENCES session(id), time_created INTEGER, time_updated INTEGER,
            data TEXT, sequence INTEGER);
        CREATE TRIGGER message_sequence_autofill AFTER INSERT ON message
        BEGIN UPDATE message SET sequence=(SELECT COUNT(*)-1 FROM message WHERE session_id=NEW.session_id)
              WHERE id=NEW.id; END;
        CREATE TRIGGER part_sequence_autofill AFTER INSERT ON part
        BEGIN UPDATE part SET sequence=(SELECT COUNT(*)-1 FROM part WHERE message_id=NEW.message_id)
              WHERE id=NEW.id; END;
    """)
    conn.commit()
    conn.close()


class ZCodeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.real_db = self.base / "real.sqlite"
        self.lab_db = self.base / "lab.sqlite"
        fixture_db(self.real_db)
        fixture_db(self.lab_db)
        self.bridge = self.base / ".bridge"
        self.bridge.mkdir()
        self.state = self.base / "state.json"
        bs.atomic_json(self.state, {"autosync_all": True})
        self.settings = patch.multiple(zi, BRIDGE=self.bridge, REAL_DB=str(self.real_db),
                                       APP_STATE=self.state, BATCH_SLEEP=0)
        self.settings.start()
        self.session = {"dir": "/fixture/project", "title": "fixture title", "mtime": time.time(),
                        "src": str(self.base / "source.jsonl"),
                        "turns": [("user", "question"), ("assistant", "answer")]}
        self.result = {"claude": [], "codex": [], "workbuddy": [self.session]}

    def tearDown(self):
        self.settings.stop()
        self.temporary.cleanup()

    def count(self, database):
        with sqlite3.connect(database) as conn:
            return conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]

    def test_workbuddy_rows_use_safe_mode_and_no_fabricated_model(self):
        rows = zi.build_session_rows(self.session, "workbuddy", "project")
        self.assertEqual(json.loads(rows["session"][7])["mode"], "build")
        for record in rows["messages"]:
            self.assertNotIn("modelId", json.loads(record[4]))
        self.assertEqual(len(rows["entries"]), 1)

    def test_verify_failure_rolls_back_before_commit_and_has_no_ledger(self):
        with patch.object(sync, "collect", return_value=self.result), \
             patch.object(zi, "verify_one", side_effect=RuntimeError("fixture verify failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture verify failure"):
                zi.do_inject(str(self.real_db), False)
        self.assertEqual(self.count(self.real_db), 0)
        self.assertFalse(zi.state_paths(self.real_db)["ledger"].exists())

    def test_lab_isolation_and_duplicate_run(self):
        real_paths = zi.state_paths(self.real_db)
        bs.atomic_json(real_paths["ledger"], {"unrelated": "real-state"})
        original = real_paths["ledger"].read_bytes()
        with patch.object(sync, "collect", return_value=self.result):
            zi.do_inject(str(self.lab_db), False)
            zi.do_inject(str(self.lab_db), False)
        self.assertEqual(self.count(self.lab_db), 1)
        self.assertEqual(self.count(self.real_db), 0)
        self.assertEqual(real_paths["ledger"].read_bytes(), original)
        lab_paths = zi.state_paths(self.lab_db)
        self.assertNotEqual(lab_paths["ledger"], real_paths["ledger"])
        manifest = bs.load_zcode_manifest(lab_paths["manifest"], self.lab_db)
        self.assertEqual(manifest["sessions"][0]["tool"], "workbuddy")
        self.assertEqual(manifest["sessions"][0]["status"], "committed")

    def test_rollback_matches_only_committed_ids_and_preserves_unrelated_ledger(self):
        with patch.object(sync, "collect", return_value=self.result):
            zi.do_inject(str(self.lab_db), False)
        paths = zi.state_paths(self.lab_db)
        ledger = bs.load_json(paths["ledger"], {})
        sid = next(iter(ledger.values()))
        ledger["to-codex:unrelated"] = "/fixture/other.jsonl"
        ledger["zcode:claude:other"] = "sess_other_12345678"
        bs.atomic_json(paths["ledger"], ledger)
        zi.do_rollback(str(self.lab_db))
        self.assertEqual(self.count(self.lab_db), 0)
        self.assertEqual(bs.load_json(paths["ledger"], {}), {
            "to-codex:unrelated": "/fixture/other.jsonl", "zcode:claude:other": "sess_other_12345678"})
        self.assertEqual(bs.provenance_records(paths["provenance"])[sid]["status"], "rolled_back")
        self.assertTrue(all(e["status"] == "rolled_back" for e in bs.load_zcode_manifest(paths["manifest"], self.lab_db)["sessions"]))

    def test_wrong_database_manifest_refuses_rollback(self):
        paths = zi.state_paths(self.lab_db)
        bs.atomic_json(paths["manifest"], {"version": 2, "database": bs.database_identity(self.real_db),
                                          "sessions": []})
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            zi.do_rollback(str(self.lab_db))

    def test_state_failure_compensates_new_sql_rows(self):
        real = bs.record_zcode_injection
        def fail_committed(*args, **kwargs):
            if kwargs.get("status") == "prepared":
                return real(*args, **kwargs)
            raise OSError("fixture state failure")
        with patch.object(sync, "collect", return_value=self.result), \
             patch.object(bs, "record_zcode_injection", side_effect=fail_committed):
            with self.assertRaisesRegex(OSError, "fixture state failure"):
                zi.do_inject(str(self.lab_db), False)
        self.assertEqual(self.count(self.lab_db), 0)
        self.assertFalse(zi.state_paths(self.lab_db)["manifest"].exists())

    def test_old_sources_are_not_selected(self):
        self.session["mtime"] = time.time() - 4 * 86400
        self.assertEqual(zi.pick_targets(self.result, {}), [])

    def test_private_atomic_state_and_reentrant_lock(self):
        path = self.bridge / "count.json"
        lock = self.bridge / "operation.lock"
        bs.atomic_json(path, {"count": 0})
        def increment():
            for _ in range(10):
                with bs.operation_lock(lock):
                    with bs.file_lock(lock):
                        state = bs.load_json(path, {})
                        state["count"] += 1
                        bs.atomic_json(path, state)
        threads = [threading.Thread(target=increment) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(bs.load_json(path, {})["count"], 30)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_text("{bad")
        with self.assertRaisesRegex(ValueError, "Invalid bridge state"):
            bs.load_json(path, {})

    def test_provenance_tombstone_keeps_origin(self):
        path = self.bridge / "provenance.json"
        bs.record_provenance(path, "codex", "/fixture/mirror", "claude", "/fixture/source")
        bs.tombstone_provenance(path, "/fixture/mirror")
        record = bs.provenance_records(path)["/fixture/mirror"]
        self.assertEqual(record["source"], "/fixture/source")
        self.assertEqual(record["status"], "source_deleted")

    def test_operation_lock_excludes_other_processes(self):
        path = self.bridge / "process-count.json"
        lock = self.bridge / "operation.lock"
        bs.atomic_json(path, {"count": 0})
        code = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import bridge_state as bs
path, lock = Path(sys.argv[2]), Path(sys.argv[3])
for _ in range(10):
    with bs.operation_lock(lock):
        data = bs.load_json(path, {})
        time.sleep(0.002)
        data['count'] += 1
        bs.atomic_json(path, data)
"""
        command = [sys.executable, "-c", code, str(ROOT / "app"), str(path), str(lock)]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for _ in range(2)]
        for process in processes:
            out, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual(bs.load_json(path, {})["count"], 20)


if __name__ == "__main__":
    unittest.main()
