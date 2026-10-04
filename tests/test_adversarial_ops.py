"""Failure fixtures use only TemporaryDirectory databases and transcripts."""
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import test_ops as fixtures
from test_ops import db, dbconn, ops, bridge_state


class AdversarialOperations(unittest.TestCase):
    setUp = fixtures.Operations.setUp
    tearDown = fixtures.Operations.tearDown
    source = fixtures.Operations.source

    def wb_source(self):
        src = self.source('workbuddy')
        sid = Path(src).stem
        with dbconn(self.env.WB_DB) as c:
            c.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                      (sid, '/fixture', 'account', 'title', 'completed', 1, 1, 1, None, 'local', 'default'))
        return sid, src

    def zsource(self, sid='sess_source1111'):
        with dbconn(self.env.Z_DB) as c:
            c.execute('INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (sid, 'proj', 'slug', '/fixture', '/fixture', 'title', '1', '{}', 1, 1, 'interactive', 'first_input', 'trace'))
        return sid

    def test_set_null_workflow_keeps_workflow_and_descendants(self):
        sid = self.zsource()
        child_sid = self.zsource('sess_child1111')
        db(self.env.Z_DB, '''CREATE TABLE workflow_run(id TEXT PRIMARY KEY,
            parent_session_id TEXT REFERENCES session(id) ON DELETE SET NULL,status TEXT);
            CREATE TABLE workflow_activity(id TEXT PRIMARY KEY,
            run_id TEXT REFERENCES workflow_run(id) ON DELETE CASCADE,payload BLOB);
            CREATE TABLE session_task_link(id TEXT PRIMARY KEY,
            parent_session_id TEXT REFERENCES session(id) ON DELETE SET NULL,
            child_session_id TEXT REFERENCES session(id) ON DELETE CASCADE);''')
        with dbconn(self.env.Z_DB) as c:
            c.execute('INSERT INTO workflow_run VALUES (?,?,?)', ('run', sid, 'completed'))
            c.execute('INSERT INTO workflow_activity VALUES (?,?,?)', ('activity', 'run', b'workflow logs'))
            c.execute('INSERT INTO session_task_link VALUES (?,?,?)', ('link', sid, child_sid))
        result = ops.delete_session(self.env, 'zcode', 'zcode:' + sid)
        with dbconn(self.env.Z_DB) as c:
            self.assertEqual(c.execute('SELECT * FROM workflow_run').fetchall(), [('run', None, 'completed')])
            self.assertEqual(c.execute('SELECT * FROM workflow_activity').fetchall(), [('activity', 'run', b'workflow logs')])
            self.assertEqual(c.execute('SELECT * FROM session_task_link').fetchall(), [('link', None, child_sid)])
            self.assertEqual(c.execute('SELECT id FROM session').fetchall(), [(child_sid,)])
        ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.Z_DB) as c:
            self.assertEqual(c.execute('SELECT parent_session_id FROM workflow_run').fetchone()[0], sid)
            self.assertEqual(c.execute('SELECT parent_session_id FROM session_task_link').fetchone()[0], sid)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_delete_and_detach_triggers_cannot_mutate_unsnapshotted_records(self):
        sid = self.zsource()
        db(self.env.Z_DB, '''CREATE TABLE workflow_run(id TEXT PRIMARY KEY,
            parent_session_id TEXT REFERENCES session(id) ON DELETE SET NULL,status TEXT);
            CREATE TABLE global_state(id TEXT PRIMARY KEY,value TEXT);
            INSERT INTO global_state VALUES ('global','preserve');
            CREATE TRIGGER destructive_delete AFTER DELETE ON session
            BEGIN DELETE FROM global_state; END;
            CREATE TRIGGER destructive_detach AFTER UPDATE ON workflow_run
            BEGIN UPDATE global_state SET value='outside the session'; END;''')
        with dbconn(self.env.Z_DB) as c:
            c.execute('INSERT INTO workflow_run VALUES (?,?,?)', ('run', sid, 'completed'))
        result = ops.delete_session(self.env, 'zcode', 'zcode:' + sid)
        with dbconn(self.env.Z_DB) as c:
            self.assertEqual(c.execute('SELECT * FROM global_state').fetchall(), [('global', 'preserve')])
            self.assertIsNone(c.execute('SELECT parent_session_id FROM workflow_run').fetchone()[0])
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('destructive_delete','destructive_detach')").fetchone()[0], 2)
        ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.Z_DB) as c:
            self.assertEqual(c.execute('SELECT * FROM global_state').fetchall(), [('global', 'preserve')])
            self.assertEqual(c.execute('SELECT parent_session_id FROM workflow_run').fetchone()[0], sid)

    def test_real_codex_cleanup_trigger_with_fk_and_zero_recency_restores_exactly(self):
        fixture_root = Path(__file__).parent / 'fixtures'
        # Recreate the production schemas only in this test's private paths.
        self.env.CX_STATE.unlink()
        self.env.CX_SQLITE.unlink()
        db(self.env.CX_STATE, (fixture_root / 'codex-state.sql').read_text())
        db(self.env.CX_SQLITE, (fixture_root / 'codex-history.sql').read_text())
        src = self.source('codex')
        sid = json.loads(Path(src).read_text().splitlines()[0])['payload']['id']
        with dbconn(self.env.CX_STATE) as c:
            c.execute('INSERT INTO threads(id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode) VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (sid, src, 1, 2, 'vscode', 'openai', '/fixture', 'fixture', '{}', 'untrusted'))
            c.execute('UPDATE threads SET created_at_ms=NULL,updated_at_ms=NULL,recency_at=0,recency_at_ms=0 WHERE id=?', (sid,))
            c.execute('INSERT INTO thread_dynamic_tools(thread_id,position,name,description,input_schema) VALUES (?,?,?,?,?)', (sid, 0, 'tool', 'description', '{}'))
            state_before = c.execute('SELECT * FROM threads WHERE id=?', (sid,)).fetchone()
            state_triggers = c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
        with dbconn(self.env.CX_SQLITE) as c:
            c.execute('INSERT INTO thread_history_projection_state VALUES (?,?,?)', (sid, 50, 5))
            cols = {r[1] for r in c.execute('PRAGMA table_info(thread_realtime_items)')}
            values = {'thread_id': sid, 'item_id': 'realtime', 'rollout_ordinal': 1, 'created_at_ms': 1,
                      'item_json': '{}', 'item_type': 'fixture', 'type': 'fixture', 'body': 'retained'}
            values = {key: value for key, value in values.items() if key in cols}
            c.execute('INSERT INTO thread_realtime_items(' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', list(values.values()))
            history_before = c.execute('SELECT * FROM thread_realtime_items').fetchall()
        result = ops.delete_session(self.env, 'codex', src)
        with dbconn(self.env.CX_SQLITE) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM thread_realtime_items').fetchone()[0], 0)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name='thread_realtime_items_projection_cleanup'").fetchone()[0], 1)
        ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.CX_STATE) as c:
            self.assertEqual(c.execute('SELECT * FROM threads WHERE id=?', (sid,)).fetchone(), state_before)
            self.assertEqual(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall(), state_triggers)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
        with dbconn(self.env.CX_SQLITE) as c:
            self.assertEqual(c.execute('SELECT * FROM thread_realtime_items').fetchall(), history_before)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_detached_workflow_modified_after_delete_is_not_overwritten(self):
        sid = self.zsource()
        db(self.env.Z_DB, '''CREATE TABLE workflow_run(id TEXT PRIMARY KEY,
            parent_session_id TEXT REFERENCES session(id) ON DELETE SET NULL,status TEXT);''')
        with dbconn(self.env.Z_DB) as c:
            c.execute('INSERT INTO workflow_run VALUES (?,?,?)', ('run', sid, 'completed'))
        result = ops.delete_session(self.env, 'zcode', 'zcode:' + sid)
        with dbconn(self.env.Z_DB) as c:
            c.execute("UPDATE workflow_run SET status='new native state'")
        with self.assertRaisesRegex(ValueError, '解除关联'):
            ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.Z_DB) as c:
            self.assertEqual(c.execute('SELECT * FROM workflow_run').fetchone(), ('run', None, 'new native state'))
            self.assertEqual(c.execute('SELECT count(*) FROM session').fetchone()[0], 0)

    def test_no_pk_identical_blob_rows_keep_rowids_and_multiplicity(self):
        sid, src = self.wb_source()
        db(self.env.WB_DB, 'CREATE TABLE duplicate_log(session_id TEXT, payload BLOB);')
        with dbconn(self.env.WB_DB) as c:
            c.executemany('INSERT INTO duplicate_log(rowid,session_id,payload) VALUES (?,?,?)',
                          [(3, sid, b'\x00same'), (7, sid, b'\x00same')])
        result = ops.delete_session(self.env, 'workbuddy', src)
        ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.WB_DB) as c:
            self.assertEqual(c.execute('SELECT rowid,session_id,payload FROM duplicate_log ORDER BY rowid').fetchall(),
                             [(3, sid, b'\x00same'), (7, sid, b'\x00same')])

    def test_restore_null_sequence_avoids_trigger_rewrite(self):
        sid = self.zsource()
        with dbconn(self.env.Z_DB) as c:
            c.execute('INSERT INTO message VALUES (?,?,?,?,?,?)', ('msg', sid, 1, 1, '{}', 0))
            c.execute('UPDATE message SET sequence=NULL')
        result = ops.delete_session(self.env, 'zcode', 'zcode:' + sid)
        ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.Z_DB) as c:
            self.assertIsNone(c.execute('SELECT sequence FROM message').fetchone()[0])
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 2)
            c.execute('INSERT INTO message VALUES (?,?,?,?,?,?)', ('new', sid, 1, 1, '{}', None))
            self.assertIsNotNone(c.execute("SELECT sequence FROM message WHERE id='new'").fetchone()[0])

    def test_restore_does_not_create_new_trigger_derived_rows(self):
        sid, src = self.wb_source()
        db(self.env.WB_DB, '''CREATE TRIGGER producer_insert AFTER INSERT ON sessions
            BEGIN INSERT INTO history VALUES ('auto-'||NEW.id,NEW.id,'derived'); END;''')
        result = ops.delete_session(self.env, 'workbuddy', src)
        ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.WB_DB) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM history').fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name='producer_insert'").fetchone()[0], 1)

    def test_changed_trigger_blocks_restore_without_mutation(self):
        sid, src = self.wb_source()
        result = ops.delete_session(self.env, 'workbuddy', src)
        db(self.env.WB_DB, '''CREATE TRIGGER new_native_trigger AFTER INSERT ON sessions
            BEGIN INSERT INTO history VALUES ('auto-'||NEW.id,NEW.id,'derived'); END;''')
        with self.assertRaisesRegex(ValueError, '触发器'):
            ops.restore_session(self.env, result['trash_id'])
        self.assertFalse(Path(src).exists())
        with dbconn(self.env.WB_DB) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM sessions WHERE id=?', (sid,)).fetchone()[0], 0)

    def test_composite_fk_does_not_capture_other_tenant(self):
        sid, src = self.wb_source()
        db(self.env.WB_DB, '''CREATE TABLE assets(session_id TEXT,asset_id TEXT,tenant TEXT,
            PRIMARY KEY(asset_id,tenant));
            CREATE TABLE assets_child(id TEXT PRIMARY KEY,asset_id TEXT,tenant TEXT,
            FOREIGN KEY(asset_id,tenant) REFERENCES assets(asset_id,tenant) ON DELETE CASCADE);''')
        with dbconn(self.env.WB_DB) as c:
            c.executemany('INSERT INTO assets VALUES (?,?,?)', [(sid, 'same', 'A'), ('seed', 'same', 'B')])
            c.executemany('INSERT INTO assets_child VALUES (?,?,?)', [('mine', 'same', 'A'), ('other', 'same', 'B')])
        result = ops.delete_session(self.env, 'workbuddy', src)
        with dbconn(self.env.WB_DB) as c:
            self.assertEqual(c.execute('SELECT id FROM assets_child').fetchall(), [('other',)])
        ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.WB_DB) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM assets_child').fetchone()[0], 2)

    def test_fk_order_ignores_table_creation_order(self):
        sid, src = self.wb_source()
        db(self.env.WB_DB, '''CREATE TABLE early_child(id TEXT PRIMARY KEY,session_id TEXT,
            parent_id TEXT REFERENCES late_parent(id) ON DELETE RESTRICT);
            CREATE TABLE late_parent(id TEXT PRIMARY KEY,
            session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE);''')
        with dbconn(self.env.WB_DB) as c:
            c.execute('INSERT INTO late_parent VALUES (?,?)', ('parent', sid))
            c.execute('INSERT INTO early_child VALUES (?,?,?)', ('child', sid, 'parent'))
        result = ops.delete_session(self.env, 'workbuddy', src)
        ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.WB_DB) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM early_child').fetchone()[0], 1)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_unowned_restrict_child_refuses_to_expand_delete_scope(self):
        sid, src = self.wb_source()
        db(self.env.WB_DB, 'CREATE TABLE unowned(id TEXT PRIMARY KEY,parent_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT);')
        with dbconn(self.env.WB_DB) as c:
            c.execute('INSERT INTO unowned VALUES (?,?)', ('keep', sid))
        with self.assertRaisesRegex(ValueError, '未归属'):
            ops.delete_session(self.env, 'workbuddy', src)
        self.assertTrue(Path(src).exists())
        with dbconn(self.env.WB_DB) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM unowned').fetchone()[0], 1)

    def test_codex_spawn_edges_removed_and_restored(self):
        src = self.source('codex')
        sid = json.loads(Path(src).read_text().splitlines()[0])['payload']['id']
        db(self.env.CX_STATE, 'CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT PRIMARY KEY,status TEXT);')
        with dbconn(self.env.CX_STATE) as c:
            c.executemany('INSERT INTO thread_spawn_edges VALUES (?,?,?)', [(sid, 'child', 'complete'), ('parent', sid, 'complete'), ('other', 'other-child', 'complete')])
        result = ops.delete_session(self.env, 'codex', src)
        with dbconn(self.env.CX_STATE) as c:
            self.assertEqual(c.execute('SELECT child_thread_id FROM thread_spawn_edges').fetchall(), [('other-child',)])
        ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.CX_STATE) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM thread_spawn_edges').fetchone()[0], 3)

    def test_restore_second_database_failure_rolls_back_only_first(self):
        src = self.source('codex')
        sid = json.loads(Path(src).read_text().splitlines()[0])['payload']['id']
        with dbconn(self.env.CX_SQLITE) as c:
            c.execute('INSERT INTO thread_items VALUES (?,?,?)', (sid, 'msg', 'retained'))
        result = ops.delete_session(self.env, 'codex', src)
        restore = ops._restore_rows
        def fixture_failure(snapshot, **kw):
            if snapshot['path'] == str(self.env.CX_SQLITE.resolve()):
                raise RuntimeError('second database failure')
            return restore(snapshot, **kw)
        with patch.object(ops, '_restore_rows', side_effect=fixture_failure):
            with self.assertRaisesRegex(RuntimeError, 'second database'):
                ops.restore_session(self.env, result['trash_id'])
        journal = json.loads((Path(result['trash']) / 'journal.json').read_text())
        self.assertEqual(journal['status'], 'deleted')
        self.assertNotIn('recovery_error', journal)
        self.assertFalse(Path(src).exists())
        ops.restore_session(self.env, result['trash_id'])
        with dbconn(self.env.CX_SQLITE) as c:
            self.assertEqual(c.execute('SELECT body FROM thread_items WHERE thread_id=?', (sid,)).fetchone()[0], 'retained')

    def test_native_append_between_check_and_remove_preserves_original_bytes(self):
        sid, src = self.wb_source()
        old = Path(src).read_bytes()
        added = b'{"type":"message","role":"user","content":"native update"}\n'
        def writer(stage):
            if stage == 'delete_file_checked':
                with Path(src).open('ab') as stream:
                    stream.write(added)
        self.env.ops_failpoint = writer
        with self.assertRaisesRegex(ValueError, '删除期间变化'):
            ops.delete_session(self.env, 'workbuddy', src)
        self.assertEqual(Path(src).read_bytes(), old + added)
        journal = json.loads(next((self.env.BRIDGE / 'operations').glob('*/journal.json')).read_text())
        self.assertEqual(journal['status'], 'compensated')

    def test_restore_atomic_file_publish_does_not_replace_recreated_file(self):
        _, src = self.wb_source()
        result = ops.delete_session(self.env, 'workbuddy', src)
        real_write = ops._write_private
        def racer(path, data, **kwargs):
            if Path(path).resolve() == Path(src).resolve() and kwargs.get('no_replace'):
                Path(src).write_bytes(b'new native transcript')
            return real_write(path, data, **kwargs)
        with patch.object(ops, '_write_private', side_effect=racer):
            with self.assertRaises(FileExistsError):
                ops.restore_session(self.env, result['trash_id'])
        self.assertEqual(Path(src).read_bytes(), b'new native transcript')

    def test_crash_compensation_prechecks_corrupt_backup_before_database_writes(self):
        sid, src = self.wb_source()
        result = ops.delete_session(self.env, 'workbuddy', src)
        opdir = Path(result['trash'])
        journal = json.loads((opdir / 'journal.json').read_text())
        ops._save(opdir, journal, 'databases_removed')
        (opdir / journal['files'][0]['backup']).write_bytes(b'corrupted backup')
        attention = ops.recover_pending_operations(self.env)
        self.assertEqual(len(attention), 1)
        with dbconn(self.env.WB_DB) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM sessions WHERE id=?', (sid,)).fetchone()[0], 0)
        self.assertFalse(Path(src).exists())

    def test_serialized_crash_snapshots_compensate_fk_records(self):
        sid, src = self.wb_source()
        with dbconn(self.env.WB_DB) as c:
            c.execute('INSERT INTO history VALUES (?,?,?)', ('h', sid, 'retained'))
        result = ops.delete_session(self.env, 'workbuddy', src)
        opdir = Path(result['trash'])
        journal = json.loads((opdir / 'journal.json').read_text())
        ops._save(opdir, journal, 'databases_removed')
        self.assertEqual(ops.recover_pending_operations(self.env), [])
        self.assertEqual(json.loads((opdir / 'journal.json').read_text())['status'], 'compensated')
        self.assertTrue(Path(src).exists())
        with dbconn(self.env.WB_DB) as c:
            self.assertEqual(c.execute('SELECT body FROM history WHERE session_id=?', (sid,)).fetchone()[0], 'retained')


if __name__ == '__main__':
    unittest.main()
