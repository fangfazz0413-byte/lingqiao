"""子代理对话清理（灵桥 3.3 追加）：只清 ZCode 子会话、Codex 子代理线程、Claude Code 子代理记录，主对话不动；
定时清理默认关。全部在临时目录里的四工具真实表结构上跑，不碰真实会话。"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_server as fixture  # noqa: E402
import bulk_cleanup as bulk  # noqa: E402
import server as s  # noqa: E402


class SubagentCleanupTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ServerIntegration('test_real_schemas_all_twelve_roundtrip_and_restore'); self.f.setUp()
        self.p = patch.object(s, 'invalidate', lambda: None); self.p.start()
        self.old = time.time() - 35 * 86400
        self.parent_z = self.f.sources['zcode'].removeprefix('zcode:')
        self.parent_claude = Path(self.f.sources['claude'])
        self.parent_codex = Path(self.f.sources['codex'])
        # ZCode：再写一条会话，把它挂到主对话下面当子会话
        c = s.zi.connect_db(s.Z_DB)
        rows = s.zi.build_session_rows({**self.f.meta, 'title': '子任务：查资料', 'turns': [(x['role'], x['text']) for x in self.f.turns]}, 'claude', 'proj_fixture')
        s.zi.write_one(c.cursor(), rows); c.commit(); c.close()
        self.child_z = rows['sid']
        c = sqlite3.connect(s.Z_DB)
        c.execute('UPDATE session SET parent_id=? WHERE id=?', (self.parent_z, self.child_z))
        for table in ('session', 'message', 'part'):
            c.execute('UPDATE ' + table + ' SET time_updated=?', (int(self.old * 1000),))
        c.commit(); c.close()
        # Codex：子代理线程（session_meta.source 里有 subagent）
        cid = str(uuid.uuid4())
        self.child_codex = s.CX_ROOT / ('rollout-sub-' + cid + '.jsonl')
        records = [{'type': 'session_meta', 'timestamp': self.old, 'payload': {'id': cid, 'cwd': '/fixture', 'source': {'subagent': {'parent_thread_id': 'x'}}}}]
        records += [{'type': 'response_item', 'timestamp': self.old, 'payload': {'type': 'message', 'role': t['role'], 'content': [{'type': 'input_text' if t['role'] == 'user' else 'output_text', 'text': t['text']}]}} for t in self.f.turns]
        self.child_codex.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in records) + '\n')
        os.utime(self.child_codex, (self.old, self.old))
        c = sqlite3.connect(s.CX_STATE)
        c.execute('INSERT INTO threads (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (cid, str(self.child_codex), int(self.old), int(self.old), 'subagent', 'fixture', '/fixture', 'sub', '{}', 'untrusted'))
        c.commit(); c.close()
        # Claude Code：<项目>/<主对话>/subagents/agent-*.jsonl，记录是 sidechain
        sid = self.parent_claude.stem
        folder = s.CC_ROOT / '-fixture' / sid / 'subagents'; folder.mkdir(parents=True)
        self.child_claude = folder / 'agent-a1b2c3.jsonl'
        self.child_claude.write_text('\n'.join(json.dumps({'type': t['role'], 'isSidechain': True, 'sessionId': sid, 'agentId': 'a1b2c3', 'timestamp': self.old,
                                                           'message': {'role': t['role'], 'content': [{'type': 'text', 'text': t['text']}]}}, ensure_ascii=False)
                                               for t in self.f.turns) + '\n')
        os.utime(self.child_claude, (self.old, self.old))
        self.before = {'claude': self.parent_claude.read_bytes(), 'codex': self.parent_codex.read_bytes(),
                       'zcode': s.session_detail('zcode', self.f.sources['zcode'])}
        s.rescan_sessions()

    def tearDown(self):
        self.p.stop(); self.f.tearDown()

    def plan(self, target='subagent', tool='all', days=30):
        return bulk.preview(s, days, tool, background=False, target=target)

    def zcount(self, sid):
        c = sqlite3.connect(s.Z_DB)
        try:
            return {t: c.execute('SELECT count(*) FROM ' + t + ' WHERE ' + ('id' if t == 'session' else 'session_id') + '=?', (sid,)).fetchone()[0]
                    for t in ('session', 'message', 'part')}
        finally:
            c.close()

    def assert_parents_untouched(self):
        self.assertEqual(self.parent_claude.read_bytes(), self.before['claude'])
        self.assertEqual(self.parent_codex.read_bytes(), self.before['codex'])
        self.assertEqual(s.session_detail('zcode', self.f.sources['zcode']), self.before['zcode'])

    def test_preview_lists_only_subagents_and_is_readonly(self):
        p = self.plan()
        self.assertEqual(p['status'], 'ready'); self.assertEqual(p['target'], 'subagent')
        srcs = {item['src'] for item in p['items']}
        self.assertEqual(srcs, {'zcode:' + self.child_z, str(self.child_codex), str(self.child_claude)})
        parents = {item['tool']: item['parent_title'] for item in p['items']}
        self.assertEqual(parents['claude'], '合法HTML提问'); self.assertTrue(parents['zcode'])
        self.assertNotIn('zcode:' + self.parent_z, srcs); self.assertNotIn(str(self.parent_claude), srcs)
        self.assertEqual(list((s.BRIDGE / 'operations').glob('*')), [])
        self.assertTrue(self.child_claude.exists()); self.assertEqual(self.zcount(self.child_z)['session'], 1)
        main = self.plan('main')
        self.assertNotIn('zcode:' + self.child_z, {item['src'] for item in main['items']})
        reasons = {x['src']: x['reason'] for x in main['skipped']}
        self.assertIn('子代理对话', reasons['zcode:' + self.child_z])
        with self.assertRaises(ValueError):
            self.plan('everything')

    def test_commit_moves_subagents_to_trash_keeps_parents_and_restores(self):
        p = self.plan()
        result = bulk.commit(s, p['plan_id'], True, [item['item_id'] for item in p['items']], background=False)
        self.assertEqual(result['status'], 'completed'); self.assertEqual(len(result['deleted']), 3, result['skipped'])
        self.assertFalse(self.child_claude.exists()); self.assertFalse(self.child_codex.exists())
        self.assertEqual(self.zcount(self.child_z), {'session': 0, 'message': 0, 'part': 0})
        self.assert_parents_untouched()
        c = sqlite3.connect(s.Z_DB); self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), []); c.close()
        for entry in result['deleted']:
            s.bridge_ops.restore_session(s, entry['trash_id'])
        self.assertTrue(self.child_claude.exists()); self.assertTrue(self.child_codex.exists())
        self.assertEqual(self.zcount(self.child_z)['session'], 1)
        self.assertEqual(s.session_detail('zcode', 'zcode:' + self.child_z), self.f.turns)
        self.assert_parents_untouched()

    def test_threshold_and_mode_checks(self):
        c = sqlite3.connect(s.Z_DB); c.execute('UPDATE session SET time_updated=? WHERE id=?', (int((time.time() - 20 * 86400) * 1000), self.child_z)); c.commit(); c.close()
        s.rescan_sessions()
        self.assertIn('zcode:' + self.child_z, {i['src'] for i in self.plan(tool='zcode', days=15)['items']})
        self.assertNotIn('zcode:' + self.child_z, {i['src'] for i in self.plan(tool='zcode', days=30)['items']})
        parent_row = s.find_meta('zcode', self.f.sources['zcode'])
        with self.assertRaisesRegex(ValueError, '不是'):
            bulk.observe(s, {**parent_row, 'kind': 'subagent'}, subagent=True)
        with self.assertRaisesRegex(ValueError, '不是子代理对话'):
            bulk.observe(s, parent_row, subagent=True)

    def test_preview_after_change_is_skipped(self):
        p = self.plan(tool='claude')
        with open(self.child_claude, 'a') as f:
            f.write(json.dumps({'type': 'user', 'isSidechain': True, 'sessionId': self.parent_claude.stem, 'timestamp': time.time(),
                                'message': {'role': 'user', 'content': '又说了一句'}}) + '\n')
        result = bulk.commit(s, p['plan_id'], True, [i['item_id'] for i in p['items']], background=False)
        self.assertEqual(result['deleted'], []); self.assertTrue(self.child_claude.exists())

    def test_auto_cleanup_is_off_until_turned_on_then_runs_daily(self):
        self.assertEqual(bulk.auto_settings(s)['enabled'], False)
        self.assertIsNone(bulk.auto_tick(s))
        self.assertTrue(self.child_claude.exists())
        for enabled, days in [('yes', 30), (True, 14), (None, 30), (True, '30')]:
            with self.assertRaises(ValueError):
                bulk.set_auto(s, enabled, days)
        settings = bulk.set_auto(s, True, 30)
        self.assertEqual((settings['enabled'], settings['days']), (True, 30))
        self.assertEqual((s.BRIDGE / bulk.AUTO_FILE).stat().st_mode & 0o777, 0o600)
        now = time.time()
        result = bulk.auto_tick(s, now)
        self.assertEqual(result['deleted'], 3); self.assertFalse(self.child_claude.exists())
        self.assert_parents_untouched()
        self.assertIsNone(bulk.auto_tick(s, now + 3600))               # 一天之内不重复跑
        again = bulk.auto_tick(s, now + bulk.AUTO_INTERVAL + 1)
        self.assertEqual(again['deleted'], 0)
        last = bulk.auto_settings(s)['last_result']
        self.assertEqual((last['deleted'], last['days']), (0, 30))
        bulk.set_auto(s, False, 30); self.assertIsNone(bulk.auto_tick(s, now + 10 * bulk.AUTO_INTERVAL))


    def test_http_routes_need_token_and_validate(self):
        import http.client, threading
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), s.http_api.make_handler(s)); port = httpd.server_port
        with patch.object(s, 'PORT', port):
            threading.Thread(target=httpd.serve_forever, daemon=True).start()

            def request(path, headers=None, method='GET', body=None):
                c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                c.request(method, path, json.dumps(body) if body is not None else None, headers or {})
                r = c.getresponse(); raw = r.read(); c.close()
                return r.status, (json.loads(raw) if raw else None)
            try:
                auth = {'X-Bridge-Token': s.API_TOKEN}; js = {**auth, 'Content-Type': 'application/json'}
                self.assertEqual(request('/api/cleanup/subagent-auto')[0], 403)
                self.assertEqual(request('/api/cleanup/subagent-auto', {'Content-Type': 'application/json'}, 'POST', {'enabled': True, 'days': 30})[0], 403)
                self.assertEqual(request('/api/cleanup/subagent-auto', {**js, 'Origin': 'https://evil.invalid'}, 'POST', {'enabled': True, 'days': 30})[0], 403)
                code, body = request('/api/cleanup/subagent-auto', auth)
                self.assertEqual((code, body['enabled']), (200, False))
                self.assertEqual(request('/api/cleanup/subagent-auto', js, 'POST', {'enabled': 'yes', 'days': 30})[0], 400)
                code, body = request('/api/cleanup/subagent-auto', js, 'POST', {'enabled': True, 'days': 15})
                self.assertEqual((code, body['enabled'], body['days']), (200, True, 15))
                self.assertEqual(request('/api/cleanup/preview', js, 'POST', {'days': 30, 'tool': 'all', 'target': 'x'})[0], 400)
                code, body = request('/api/cleanup/preview', js, 'POST', {'days': 30, 'tool': 'all', 'target': 'subagent'})
                self.assertEqual((code, body['target']), (200, 'subagent'))
            finally:
                httpd.shutdown(); httpd.server_close()


if __name__ == '__main__':
    unittest.main()
