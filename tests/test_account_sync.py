"""Claude 双账号会话补齐（灵桥 3.3 追加）：扫描、预览、同步、备份、撤销、HTTP 守卫。

全部用临时目录里造的假账号目录和假聊天记录；不读真实的 ~/Library/Application Support/Claude，
不碰真实会话数据，也不读任何登录凭据。
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app')); sys.path.insert(0, str(ROOT / '.bridge'))
import account_sync as asy  # noqa: E402

A = 'aaaaaaaa-0000-4000-8000-00000000000a'
B = 'bbbbbbbb-0000-4000-8000-00000000000b'
C = 'cccccccc-0000-4000-8000-00000000000c'
D = 'dddddddd-0000-4000-8000-00000000000d'
ORG_A = '11111111-0000-4000-8000-000000000001'
ORG_B = '22222222-0000-4000-8000-000000000002'
ORG_B_EMPTY = '33333333-0000-4000-8000-000000000003'
ORG_C = '44444444-0000-4000-8000-000000000004'
ORG_D = '55555555-0000-4000-8000-000000000005'
MAIN = '/Applications/Claude.app/Contents/MacOS/Claude'
HELPER = '/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/Contents/MacOS/Claude Helper (Renderer)'
HARMLESS = [(11, '/Applications/Claude.app/Contents/Helpers/chrome-native-host'), (12, '/usr/local/bin/CheckClaude'),
            (13, '/Users/x/Library/Application Support/Claude/claude-code/2.1/claude.app/Contents/MacOS/claude')]

_AUDIT = {'on': False, 'paths': []}


def _audit(event, args):
    if _AUDIT['on'] and event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):
        _AUDIT['paths'].append(os.fsdecode(args[0]))


sys.addaudithook(_audit)


def uid(n):
    return f'{n:08x}-1111-4111-8111-{n:012x}'


def tree_digest(folder):
    out = {}
    for path in sorted(Path(folder).rglob('*')):
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(folder))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class Sandbox:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.claude = self.base / 'Library' / 'Application Support' / 'Claude'
        self.meta = self.claude / 'claude-code-sessions'
        self.projects = self.base / '.claude' / 'projects' / '-Volumes-WorkDisk'
        self.bridge = self.base / 'repo' / '.bridge'
        self.ssd_backups = self.base / 'Backup' / '灵桥备份'
        for folder in (self.meta, self.projects, self.bridge, self.ssd_backups):
            folder.mkdir(parents=True)
        # 不该被读的东西：Claude 的配置、Cookies、Local Storage（里面放假的“凭据”做哨兵）
        (self.claude / 'config.json').write_text('{"oauth:tokenCache": "SENTINEL-NOT-A-REAL-TOKEN"}')
        (self.claude / 'Cookies').write_text('SENTINEL')
        (self.claude / 'Local Storage').mkdir(); (self.claude / 'Local Storage' / 'leveldb.log').write_text('SENTINEL')
        self.config = self.bridge / 'config.json'
        self.write_config()
        self.logs = []
        self.processes = list(HARMLESS)
        self.env = SimpleNamespace(CC_META_ROOT=self.meta, CC_ROOT=self.base / '.claude' / 'projects', BRIDGE=self.bridge,
                                   CONFIG=self.config, HOME=self.base, log=self.logs.append, invalidate=mock.Mock())
        self.opened = []
        self.sync = asy.AccountSync(self.env, process_lister=lambda: list(self.processes), opener=self.opened.append)
        self.now = time.time()
        self.build()

    def write_config(self, **overrides):
        section = {'labels': {A: '个人账号（测试）', B: '团队账号'}, 'backup_dir': str(self.ssd_backups)}
        section.update(overrides)
        self.config.write_text(json.dumps({'sync_window_days': 3, 'claude_accounts': section}, ensure_ascii=False), encoding='utf-8')

    def session(self, account, org, n, cid=None, title='', hours_ago=1, transcript=True, **extra):
        folder = self.meta / account / org
        folder.mkdir(parents=True, exist_ok=True)
        cid = cid if cid is not None else uid(1000 + n)
        last = int((self.now - hours_ago * 3600) * 1000)
        data = {'sessionId': f'local_{uid(n)}', 'cliSessionId': cid, 'cwd': '/Volumes/WorkDisk', 'originCwd': '/Volumes/WorkDisk',
                'createdAt': last - 60000, 'lastActivityAt': last, 'lastFocusedAt': last, 'model': 'claude-opus-5-5', 'isArchived': False,
                'permissionMode': 'default', 'remoteMcpServersConfig': [], 'title': title, 'bridgeSessionIds': ['bridge-old-account'],
                'remoteControlAutoEligible': True, 'spawnSeed': {'x': 1}}
        if cid == '':
            data.pop('cliSessionId')
        data.update(extra)
        path = folder / f'local_{uid(n)}.json'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        path.chmod(0o600)
        os.utime(path, (self.now - hours_ago * 3600, self.now - hours_ago * 3600))
        if transcript and cid:
            (self.projects / f'{cid}.jsonl').write_text('{"type":"user","message":"聊天内容不该被读"}\n')
        return path

    def build(self):
        s = self.session
        self.s1 = s(A, ORG_A, 1, title='新会话：只在 A')
        s(A, ORG_A, 2, title='标题在 A 更新过', hours_ago=1)
        self.b2 = s(B, ORG_B, 2, cid=uid(1002), title='旧标题', hours_ago=5, transcript=False)
        s(A, ORG_A, 3, title='两边一样'); s(B, ORG_B, 3, cid=uid(1003), title='两边一样', transcript=False)
        s(A, ORG_A, 4, title='A 这边旧', hours_ago=6); self.b4 = s(B, ORG_B, 4, cid=uid(1004), title='B 这边新', hours_ago=2, transcript=False)
        s(A, ORG_A, 5, title='聊天记录不在本机', transcript=False)
        s(A, ORG_A, 6, title='B 删过的会话')
        (self.meta / B / ORG_B / f'deleted_{uid(6)}').write_text('1759500000000')
        s(A, ORG_A, 7, cid='', title='没有会话 ID')
        s(A, ORG_A, 8, title='三天前的会话', hours_ago=72)
        s(A, ORG_A, 9, title='撞名的会话')
        s(B, ORG_B, 9, cid=uid(9999), title='B 里同名文件但是别的会话', transcript=False)
        (self.meta / A / ORG_A / f'local_{uid(10)}.json').write_text('{坏的 json')
        (self.meta / A / ORG_A / f'deleted_{uid(11)}').write_text('1759500000000')
        (self.meta / A / ORG_A / 'scheduled-tasks.json').write_text('[]')
        (self.meta / A / ORG_A / 'backlog').mkdir()
        self.b20 = s(B, ORG_B, 20, title='新会话：只在 B', hours_ago=2)
        (self.meta / B / ORG_B_EMPTY).mkdir(parents=True); (self.meta / B / ORG_B_EMPTY / 'scheduled-tasks.json').write_text('[]')
        s(C, ORG_C, 30, title='八月的旧会话', hours_ago=24 * 50, transcript=False)
        (self.meta / D / ORG_D).mkdir(parents=True)
        (self.meta / 'not-an-account.json').write_text('{}')

    def close(self):
        self.tmp.cleanup()


class AccountCase(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.sync = self.box.sync

    def tearDown(self):
        self.box.close()

    def preview(self, **kw):
        body = {'source': A, 'target': B, 'window_hours': 24}
        body.update(kw)
        return self.sync.preview(body)


class StatusAndPreviewTests(AccountCase):
    def test_status_lists_accounts_and_no_titles(self):
        status = self.sync.handle_get('/api/accounts/status', {})
        self.assertTrue(status['available']); self.assertFalse(status['claude']['running'])
        names = [a['name'] for a in status['accounts']]
        self.assertEqual(names[:2], ['个人账号（测试）', '团队账号'])
        self.assertIn('账号 cccccccc', names); self.assertIn('账号 dddddddd', names)
        by_id = {a['id']: a for a in status['accounts']}
        self.assertEqual(by_id[B]['target_org'], ORG_B)  # 空的组织目录不当目标
        self.assertEqual(by_id[B]['tombstones'], 1); self.assertFalse(by_id[B]['ambiguous'])
        self.assertIsNone(by_id[D]['target_org'])
        self.assertEqual(by_id[A]['sessions'], 9)  # 坏的 json 不算会话
        self.assertTrue(by_id[A]['recent'])
        self.assertEqual(status['backup']['dir'], str(self.box.ssd_backups)); self.assertFalse(status['backup']['fallback'])
        text = json.dumps(status, ensure_ascii=False)
        for title in ('新会话：只在 A', '旧标题', '八月的旧会话'):
            self.assertNotIn(title, text)

    def test_preview_sorts_every_session_into_one_bucket(self):
        before = tree_digest(self.box.meta)
        self.box.processes.append((99, MAIN))  # 桌面版开着也能预览
        result = self.preview()
        self.assertEqual(tree_digest(self.box.meta), before)
        plan = result['plans'][0]
        self.assertEqual((plan['source_label'], plan['target_label'], plan['target_org']), ('个人账号（测试）', '团队账号', ORG_B))
        self.assertEqual(plan['counts'], {'copy': 1, 'update': 1, 'already': 2, 'out_of_window': 1, 'unreadable': 1, 'no_transcript': 1,
                                          'deleted': 1, 'no_id': 1, 'duplicate': 0, 'conflict': 1, 'changed': 0, 'error': 0})
        self.assertEqual([c['title'] for c in plan['copy']], ['新会话：只在 A'])
        self.assertEqual(plan['update'][0]['title_old'], '旧标题'); self.assertEqual(plan['update'][0]['title_new'], '标题在 A 更新过')
        self.assertEqual({s['title']: s['reason_text'] for s in plan['skipped']},
                         {'聊天记录不在本机': '聊天记录不在本机', 'B 删过的会话': '目标账号删过', '没有会话 ID': '没有会话 ID', '撞名的会话': '目标里有同名文件'})
        self.assertEqual(sorted(a['title'] for a in plan['already']), ['A 这边旧', '两边一样'])
        self.assertNotIn('A 这边旧', [u['title_new'] for u in plan['update']])  # B 那边标题更新，不改回旧的
        self.assertTrue(result['claude']['running']); self.assertFalse(result['nothing'])
        unlimited = self.preview(window_hours=0)['plans'][0]['counts']
        self.assertEqual((unlimited['copy'], unlimited['out_of_window']), (2, 0))

    def test_both_directions_and_target_choice(self):
        plans = self.preview(both=True)['plans']
        self.assertEqual([(p['source'], p['target']) for p in plans], [(A, B), (B, A)])
        self.assertEqual([c['title'] for c in plans[1]['copy']], ['新会话：只在 B'])
        with self.assertRaisesRegex(ValueError, '还没有会话'):
            self.preview(target=D)
        with self.assertRaisesRegex(ValueError, '还没有会话'):
            self.preview(orgs={B: ORG_B_EMPTY})
        self.assertEqual(self.preview(orgs={B: ORG_B})['plans'][0]['target_org'], ORG_B)

    def test_bad_parameters(self):
        for body, message in [({'source': 'x', 'target': B}, '来源账号'), ({'source': A, 'target': A}, '同一个账号'),
                              ({'source': A, 'target': B, 'window_hours': 7}, '时间范围'), ({'source': A, 'target': B, 'window_hours': '24'}, '时间范围'),
                              ({'source': A, 'target': B, 'window_hours': True}, '时间范围'), ({'source': A, 'target': B, 'orgs': {B: '../x'}}, '组织目录'),
                              ({'source': A, 'target': 'eeeeeeee-0000-4000-8000-00000000000e'}, '找不到')]:
            with self.subTest(body=body):
                with self.assertRaisesRegex(ValueError, message):
                    self.sync.preview(body)

    def test_missing_folder_turns_page_off(self):
        self.box.env.CC_META_ROOT = self.box.base / '没有这个目录'
        status = self.sync.status()
        self.assertFalse(status['available']); self.assertIn('没找到', status['reason'])
        with self.assertRaisesRegex(ValueError, '没找到'):
            self.preview()


class SyncTests(AccountCase):
    def run_sync(self, **kw):
        body = {'source': A, 'target': B, 'window_hours': 24, 'confirm': True}
        body.update(kw)
        return self.sync.handle_post('/api/accounts/sync', body)

    def test_refused_while_desktop_is_open_or_unknown(self):
        before = tree_digest(self.box.meta)
        for processes, message in [([(99, MAIN)], '还开着'), ([(98, HELPER)], '还开着')]:
            self.box.processes = list(HARMLESS) + processes
            with self.subTest(processes=processes):
                with self.assertRaisesRegex(ValueError, '⌘Q'):
                    self.run_sync()
        self.sync.process_lister = mock.Mock(side_effect=OSError('ps 坏了'))
        with self.assertRaisesRegex(ValueError, '没法确认'):
            self.run_sync()
        self.assertEqual(tree_digest(self.box.meta), before)
        self.assertEqual(list(self.box.ssd_backups.iterdir()), [])
        with self.assertRaisesRegex(ValueError, '先预览'):
            self.sync.sync({'source': A, 'target': B})

    def test_sync_backs_up_then_copies_and_refreshes_titles(self):
        before = tree_digest(self.box.meta)
        transcripts = tree_digest(self.box.projects)
        _AUDIT['paths'].clear(); _AUDIT['on'] = True
        try:
            result = self.run_sync()
        finally:
            _AUDIT['on'] = False
        run = result['run']
        self.assertEqual(run['totals'], {'copied': 1, 'updated': 1, 'failed': 0})
        copied = self.box.meta / B / ORG_B / self.box.s1.name
        self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
        original, copy = json.loads(self.box.s1.read_text()), json.loads(copied.read_text())
        self.assertEqual(copy['bridgeSessionIds'], []); self.assertIs(copy['remoteControlAutoEligible'], False)
        for key in ('sessionId', 'cliSessionId', 'title', 'cwd', 'model', 'spawnSeed', 'lastActivityAt'):
            self.assertEqual(copy[key], original[key])
        self.assertEqual(json.loads(self.box.b2.read_text())['title'], '标题在 A 更新过')
        self.assertEqual(self.box.b2.stat().st_mode & 0o777, 0o600)
        after = tree_digest(self.box.meta)
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        self.assertEqual(changed, {f'{B}/{ORG_B}/{self.box.s1.name}', f'{B}/{ORG_B}/{self.box.b2.name}'})
        self.assertEqual(json.loads(self.box.s1.read_text()), original)  # 来源不动
        self.assertEqual(tree_digest(self.box.projects), transcripts)  # 聊天记录不动
        self.assertFalse(list((self.box.meta / B / ORG_B).glob('.lingqiao-*')))
        # 备份：同步前的整份目录，逐个文件一致；旁边放一份同步记录
        folder = Path(run['backup']['path'])
        self.assertEqual(folder.parent, self.box.ssd_backups); self.assertRegex(folder.name, r'^Claude会话迁移备份_\d{8}_\d{6}$')
        self.assertEqual(tree_digest(folder / 'claude-code-sessions'), before)
        self.assertEqual(run['backup']['files'], len(before)); self.assertTrue(run['backup']['verified'])
        self.assertEqual(json.loads((folder / '灵桥同步记录.json').read_text())['id'], run['id'])
        record = self.box.bridge / 'account-sync' / 'runs' / f"{run['id']}.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.box.bridge / 'account-sync').stat().st_mode & 0o777, 0o700)
        self.assertTrue(any(line.startswith('account-sync run=') for line in self.box.logs))
        self.assertFalse(any('只在 A' in line for line in self.box.logs))  # 日志不写标题
        self.box.env.invalidate.assert_called()
        # 不读 Claude 的配置/Cookies/Local Storage，也不读聊天记录内容
        opened = _AUDIT['paths']
        self.assertTrue(opened)
        self.assertFalse([p for p in opened if p.startswith(str(self.box.claude)) and not p.startswith(str(self.box.meta))])
        self.assertFalse([p for p in opened if p.endswith('.jsonl')])
        # 再跑一遍：什么都不用做，也不再备份
        again = self.run_sync()
        self.assertTrue(again['nothing']); self.assertEqual(len(list(self.box.ssd_backups.iterdir())), 1)
        self.assertEqual(self.sync.status()['runs'][0]['id'], run['id'])

    def test_both_directions_then_idempotent(self):
        result = self.run_sync(both=True)
        # A→B：复制 1、刷新 1；B→A：复制 1，并把 A 那边较旧的标题刷新成 B 的新标题
        self.assertEqual(result['run']['totals'], {'copied': 2, 'updated': 2, 'failed': 0})
        self.assertEqual(json.loads((self.box.meta / A / ORG_A / f'local_{uid(4)}.json').read_text())['title'], 'B 这边新')
        self.assertTrue((self.box.meta / A / ORG_A / self.box.b20.name).is_file())
        self.assertTrue((self.box.meta / B / ORG_B / self.box.s1.name).is_file())
        self.assertEqual(result['run']['direction'], '个人账号（测试） → 团队账号 + 团队账号 → 个人账号（测试）')
        self.assertTrue(self.run_sync(both=True)['nothing'])
        self.assertTrue(self.run_sync(source=B, target=A, window_hours=0)['nothing'])

    def test_falls_back_to_local_backup_without_creating_ssd_paths(self):
        missing = self.box.base / '没接的硬盘' / '灵桥备份'
        self.box.write_config(backup_dir=str(missing))
        run = self.run_sync()['run']
        self.assertTrue(run['backup']['fallback'])
        self.assertTrue(Path(run['backup']['path']).is_relative_to(self.box.bridge / 'account-sync' / 'backups'))
        self.assertFalse(missing.exists()); self.assertFalse(missing.parent.exists())

    def test_without_backup_dir_backs_up_inside_lingqiao(self):
        # 公开版默认不指定备份目录：直接放灵桥本地，这不算"退而求其次"。
        self.box.write_config(backup_dir='')
        self.assertEqual(self.sync.backup_public()['configured'], '')
        run = self.run_sync()['run']
        self.assertFalse(run['backup']['fallback'])
        self.assertTrue(Path(run['backup']['path']).is_relative_to(self.box.bridge / 'account-sync' / 'backups'))

    def test_bad_backup_stops_before_writing(self):
        before = tree_digest(self.box.meta)
        with mock.patch.object(asy.AccountSync, '_verify_copy', side_effect=ValueError('备份核对没过（x），这次没有同步')):
            with self.assertRaisesRegex(ValueError, '备份核对没过'):
                self.run_sync()
        self.assertEqual(tree_digest(self.box.meta), before)

    def test_desktop_opened_during_backup_stops_before_writing(self):
        before = tree_digest(self.box.meta)
        answers = iter([list(HARMLESS), list(HARMLESS) + [(99, MAIN)]])
        self.sync.process_lister = lambda: next(answers)
        with self.assertRaisesRegex(ValueError, '还开着'):
            self.run_sync()
        self.assertEqual(tree_digest(self.box.meta), before)
        self.assertEqual(len(list(self.box.ssd_backups.iterdir())), 1)  # 备份留着

    def test_existing_file_is_never_overwritten(self):
        plan = self.sync.plans({'source': A, 'target': B, 'window_hours': 24})[0]
        squatter = self.box.meta / B / ORG_B / self.box.s1.name
        squatter.write_text('{"cliSessionId": "别的"}')  # 规划之后、写之前冒出来的同名文件
        result = self.sync._apply(plan)
        self.assertEqual(result['copied'], []); self.assertEqual(result['failed'][0]['reason'], 'conflict')
        self.assertEqual(squatter.read_text(), '{"cliSessionId": "别的"}')


class UndoTests(AccountCase):
    def test_undo_moves_untouched_copies_and_restores_titles(self):
        run = self.sync.sync({'source': A, 'target': B, 'window_hours': 0, 'confirm': True})['run']
        self.assertEqual(run['totals']['copied'], 2)
        copies = [self.box.meta / B / ORG_B / item['file'] for item in run['results'][0]['copied']]
        touched = copies[1]
        data = json.loads(touched.read_text()); data['lastFocusedAt'] += 1000; touched.write_text(json.dumps(data))  # 桌面版打开过这条
        self.box.processes.append((99, MAIN))
        with self.assertRaisesRegex(ValueError, '还开着'):
            self.sync.undo(run['id'])
        self.box.processes = list(HARMLESS)
        result = self.sync.handle_post('/api/accounts/undo', {'run_id': run['id']})['run']['undone']
        self.assertEqual([m['file'] for m in result['moved']], [copies[0].name])
        self.assertEqual([k['file'] for k in result['kept']], [touched.name]); self.assertIn('动过', result['kept'][0]['reason'])
        self.assertEqual([r['title'] for r in result['restored']], ['标题在 A 更新过'])
        self.assertFalse(copies[0].exists()); self.assertTrue(touched.exists())
        held = Path(result['holding']) / B / ORG_B / copies[0].name
        self.assertTrue(held.is_file())  # 挪走，不删除
        self.assertEqual(json.loads(self.box.b2.read_text())['title'], '旧标题')
        self.assertTrue(Path(result['backup']['path']).is_dir())
        with self.assertRaisesRegex(ValueError, '撤销过了'):
            self.sync.undo(run['id'])
        for bad in ('../x', '', None, '20261003-120000-zzzz'):
            with self.assertRaisesRegex(ValueError, '编号不对'):
                self.sync.undo(bad)
        with self.assertRaisesRegex(ValueError, '找不到'):
            self.sync.undo('20261003-120000-abcd')

    def test_runs_are_listed_newest_first_and_open_claude(self):
        self.sync.sync({'source': A, 'target': B, 'window_hours': 24, 'confirm': True})
        runs = self.sync.handle_get('/api/accounts/runs', {})['runs']
        self.assertEqual(len(runs), 1); self.assertNotIn('results', runs[0])
        detail = self.sync.handle_get('/api/accounts/run', {'id': [runs[0]['id']]})['run']
        self.assertEqual(detail['results'][0]['copied'][0]['title'], '新会话：只在 A')
        self.assertEqual(self.sync.handle_post('/api/accounts/open-claude', {}), {'ok': True})
        self.assertEqual(self.box.opened, [['/usr/bin/open', '-b', 'com.anthropic.claudefordesktop']])
        self.assertEqual(self.sync.handle_get('/api/accounts/claude', {})['running'], False)
        with self.assertRaises(FileNotFoundError):
            self.sync.handle_get('/api/accounts/nope', {})


class AccountHttpTests(unittest.TestCase):
    def setUp(self):
        import server as s
        self.s = s
        self.box = Sandbox()
        self.patches = [mock.patch.object(s, 'accounts', self.box.sync), mock.patch.object(s, 'log', lambda message: None)]
        for p in self.patches:
            p.start()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), s.http_api.make_handler(s))
        self.port_patch = mock.patch.object(s, 'PORT', self.httpd.server_port); self.port_patch.start()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        self.port_patch.stop()
        for p in reversed(self.patches):
            p.stop()
        self.box.close()

    def request(self, path, headers=None, method='GET', body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=10)
        connection.request(method, quote(path, safe='/?=&:%'), json.dumps(body).encode() if body is not None else None, headers or {})
        response = connection.getresponse(); raw = response.read(); connection.close()
        return response.status, raw

    def test_routes_check_token_host_and_origin(self):
        auth = {'X-Bridge-Token': self.s.API_TOKEN}
        for path in ['/api/accounts/status', '/api/accounts/claude', '/api/accounts/runs', '/static/accounts.js', '/static/accounts.css']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 403)
                self.assertEqual(self.request(path + '?token=' + self.s.API_TOKEN)[0], 403)
                self.assertEqual(self.request(path, {**auth, 'Host': 'evil.invalid'})[0], 403)
                self.assertEqual(self.request(path, {**auth, 'Origin': 'https://evil.invalid'})[0], 403)
                self.assertEqual(self.request(path, auth)[0], 200)
        json_headers = {'Content-Type': 'application/json'}
        before = tree_digest(self.box.meta)
        for path, body in [('/api/accounts/preview', {'source': A, 'target': B}), ('/api/accounts/sync', {'source': A, 'target': B, 'confirm': True}),
                           ('/api/accounts/undo', {'run_id': '20261003-120000-abcd'}), ('/api/accounts/open-claude', {})]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, json_headers, 'POST', body)[0], 403)
                self.assertEqual(self.request(path, {**auth, **json_headers, 'Origin': 'http://evil.invalid'}, 'POST', body)[0], 403)
                self.assertEqual(self.request(path, {**auth, 'Content-Type': 'text/plain'}, 'POST', body)[0], 400)
        self.assertEqual(tree_digest(self.box.meta), before); self.assertEqual(self.box.opened, [])
        status, raw = self.request('/api/accounts/preview', {**auth, **json_headers}, 'POST', {'source': A, 'target': B, 'window_hours': 24})
        self.assertEqual(status, 200); self.assertEqual(json.loads(raw)['plans'][0]['counts']['copy'], 1)
        self.box.processes.append((99, MAIN))
        status, raw = self.request('/api/accounts/sync', {**auth, **json_headers}, 'POST', {'source': A, 'target': B, 'window_hours': 24, 'confirm': True})
        self.assertEqual(status, 400); self.assertIn('⌘Q', json.loads(raw)['error'])
        status, raw = self.request('/static/accounts.js', auth)
        self.assertEqual(raw, (ROOT / 'app' / 'accounts.js').read_bytes())


if __name__ == '__main__':
    unittest.main()
