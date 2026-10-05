"""会话导出 / 导入（灵桥 3.4）：两台“电脑”都是临时目录里的四工具真实表结构，不碰真实会话。"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_server as fixture  # noqa: E402
import server as s  # noqa: E402
import account_sync  # noqa: E402
import bridge_ops  # noqa: E402
import session_transfer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ACCT, ORG = str(uuid.uuid4()), str(uuid.uuid4())


class Crash(BaseException):
    """模拟灵桥在导入中途被强行关掉（不走 except Exception 的当场撤回）。"""


def machine_paths(r):
    return {'HOME': r, 'REPO': r / 'repo', 'BRIDGE': r / 'repo/.bridge', 'CC_ROOT': r / '.claude/projects',
            'CC_META_ROOT': r / 'Library/Application Support/Claude/claude-code-sessions', 'CX_ROOT': r / '.codex/sessions',
            'CX_STATE': r / 'state.sqlite', 'CX_SQLITE': r / 'history.sqlite', 'CX_INDEX': r / 'index.jsonl', 'Z_DB': r / 'zcode.sqlite',
            'WB_ROOT': r / '.workbuddy/projects', 'WB_DB': r / 'wb.sqlite', 'MT_CACHE': r / 'cache.json',
            'CONFIG': r / 'repo/.bridge/config.json', 'DISK_CACHE': r / 'repo/.bridge/cache.json',
            'LEDGER': r / 'repo/.bridge/ledger.json', 'PROVENANCE': r / 'repo/.bridge/provenance.json'}


def build_machine(r, *, user='user-b'):
    paths = machine_paths(r)
    for name in ('CC_ROOT', 'CX_ROOT', 'WB_ROOT', 'BRIDGE'):
        paths[name].mkdir(parents=True, exist_ok=True)
    for attr, name in [('CX_STATE', 'codex-state'), ('CX_SQLITE', 'codex-history'), ('Z_DB', 'zcode'), ('WB_DB', 'workbuddy')]:
        c = sqlite3.connect(paths[attr]); c.executescript((ROOT / 'tests/fixtures' / f'{name}.sql').read_text()); c.close()
    if user:
        c = sqlite3.connect(paths['WB_DB'])
        c.execute('INSERT INTO sessions (id,cwd,user_id,title,status,created_at,updated_at,last_activity_at,transport) VALUES (?,?,?,?,?,?,?,?,?)',
                  ('seed-b', '/seed', user, 'seed', 'completed', 1, 1, 1, 'local'))
        c.commit(); c.close()
    org = paths['CC_META_ROOT'] / ACCT / ORG
    org.mkdir(parents=True)
    (org / f'local_{uuid.uuid4()}.json').write_text(json.dumps({'cliSessionId': 'seed-b', 'title': 'seed', 'lastActivityAt': int(time.time() * 1000)}))
    return paths


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ServerIntegration('test_real_schemas_all_twelve_roundtrip_and_restore')
        self.f.setUp()
        self.extra = [patch.object(s, 'invalidate', lambda: None)]
        for p in self.extra:
            p.start()
        self.accounts_a = account_sync.AccountSync(s, process_lister=lambda: [])
        self.pa = patch.object(s, 'accounts', self.accounts_a); self.pa.start()
        self.A = {k: getattr(s, k) for k in machine_paths(Path('/')).keys()}
        # A 上再造一条结构完整的 Codex 会话（rollout 放在 年/月/日 下，库里有线程和历史记录、索引行）
        src = self.f.sources['claude']
        made = s.bridge_ops.sync_session(s, 'claude', src, 'codex', s.find_meta('claude', src), s.session_detail('claude', src))
        self.assertFalse(made['already'])
        self.codex_src = s.load_ledger()['to-codex:claude:' + src]
        s.rescan_sessions()
        self.tmp_b = tempfile.TemporaryDirectory()
        self.rb = Path(self.tmp_b.name).resolve()
        self.B = build_machine(self.rb)
        self.on_b = []
        self.ta = session_transfer.SessionTransfer(s)

    def tearDown(self):
        self.leave_b()
        self.pa.stop()
        for p in reversed(self.extra):
            p.stop()
        self.f.tearDown()
        self.tmp_b.cleanup()

    # ---------------------------------------------------------------- helpers
    def enter_b(self, running=False):
        self.on_b = [patch.object(s, k, v) for k, v in self.B.items()]
        self.on_b += [patch.object(s.sync, 'HOME', self.rb),
                      patch.object(s, 'accounts', account_sync.AccountSync(s, process_lister=lambda: [(1, '/Applications/Claude.app/Contents/MacOS/Claude')] if running else []))]
        for p in self.on_b:
            p.start()
        s._cache.update(sessions=[], ts=time.time())
        return session_transfer.SessionTransfer(s)

    def leave_b(self):
        for p in reversed(self.on_b):
            p.stop()
        self.on_b = []

    def export(self, tools=('claude', 'codex', 'zcode', 'workbuddy'), extra=()):
        items = [{'tool': t, 'src': self.codex_src if t == 'codex' else self.f.sources[t]} for t in tools] + list(extra)
        result = self.ta.start_export({'items': items}, background=False)
        job = self.ta.job(result['job_id'])
        self.assertEqual(job['status'], 'done', job.get('error'))
        return Path(job['result']['path']), job['result']

    def plan(self, tb, zip_path):
        return tb.inspect({'handle': tb.register('import', zip_path)['handle']})

    def run_import(self, tb, plan, choices=None):
        if choices is None:
            choices = [{'id': i['id'], 'mode': i['mode'], 'target': i['target']} for i in plan['items'] if i['mode'] != 'skip']
        result = tb.start_import({'plan_id': plan['plan_id'], 'confirm': True, 'items': choices}, background=False)
        job = tb.job(result['job_id'])
        self.assertEqual(job['status'], 'done', job.get('error'))
        return job['result']

    def items_by_tool(self, plan):
        return {i['tool']: i for i in plan['items']}

    def fk_clean(self):
        for db in (s.CX_STATE, s.CX_SQLITE, s.Z_DB, s.WB_DB):
            c = sqlite3.connect(db); self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), []); c.close()

    # ---------------------------------------------------------------- tests
    def test_claude_project_dirname_matches_claude_code(self):
        # 期望值是 Claude Code 2.1.286 对同样路径实际算出来的目录名：每个不是字母数字的 UTF-16 单元换成 "-"
        # （表情符号占两个），超过 200 个字符就截到 200 个再加 "-哈希"。哈希后缀是实测记下的。
        def dashes(path):
            return ''.join(ch if ch.isascii() and ch.isalnum() else '-' * (2 if ord(ch) > 0xFFFF else 1) for ch in path)
        long_cn = '/Users/someone/' + '很长的目录名' * 30 + '/😀/project'
        long_ascii = '/Users/x/' + 'abc/' * 60
        cases = [('/Volumes/WorkDisk/工作室/02_规则与工具', '-Volumes-WorkDisk-----02------'),
                 ('/Users/a b/Library/Application Support/x', '-Users-a-b-Library-Application-Support-x'),
                 ('/tmp/😀/x', '-tmp----x'),
                 (long_cn, dashes(long_cn)[:200] + '-2xw605'),
                 (long_ascii, dashes(long_ascii)[:200] + '-j3lw8p')]
        for path, expected in cases:
            self.assertEqual(bridge_ops.claude_project_dirname(path), expected, path)
        self.assertEqual(bridge_ops.claude_project_dirname('/Volumes/WorkDisk'), '-Volumes-WorkDisk')

    def test_export_zip_has_manifest_raw_material_and_turns(self):
        zip_path, result = self.export()
        self.assertEqual(result['sessions'], 4)
        if os.name != 'nt':  # Windows 没有这种权限位，靠用户目录的访问控制
            self.assertEqual(oct(zip_path.stat().st_mode & 0o777), '0o600')
        with zipfile.ZipFile(zip_path) as zf:
            manifest = json.loads(zf.read('manifest.json'))
            self.assertEqual(manifest['format'], session_transfer.FORMAT)
            self.assertIn('README.txt', zf.namelist())
            by_tool = {x['tool']: x for x in manifest['sessions']}
            for entry in manifest['sessions']:
                for rec in entry['raw']['files'] + entry['raw']['databases'] + [entry['turns']]:
                    self.assertEqual(hashlib.sha256(zf.read(rec['path'])).hexdigest(), rec['sha256'])
                self.assertEqual(entry['turns']['count'], 3)
            claude = by_tool['claude']
            self.assertEqual({f['role'] for f in claude['raw']['files']}, {'transcript', 'sidebar'})
            self.assertEqual(zf.read(claude['raw']['files'][0]['path']), Path(self.f.sources['claude']).read_bytes())
            codex = by_tool['codex']
            self.assertEqual({d['db'] for d in codex['raw']['databases']}, {'codex_state', 'codex_history'})
            self.assertEqual(len(codex['raw']['index']), 1)
            self.assertRegex(codex['raw']['files'][0]['rel'], r'^\d{4}/\d{2}/\d{2}/rollout-')
            zrows = by_tool['zcode']['raw']['databases'][0]['rows']
            self.assertEqual(zrows['session'], 1)
            self.assertGreater(zrows['part'], 0)
            self.assertEqual(by_tool['workbuddy']['raw']['databases'][0]['rows'], {'sessions': 1})
        runs = self.ta.runs()
        self.assertEqual(runs[0]['kind'], 'export')
        self.assertTrue(runs[0]['exists'])

    def test_import_raw_roundtrip_all_four_tools(self):
        zip_path, _ = self.export()
        a_turns = {t: s.session_detail(t, self.codex_src if t == 'codex' else self.f.sources[t]) for t in ('claude', 'codex', 'zcode', 'workbuddy')}
        a_claude = Path(self.f.sources['claude']).read_bytes()
        a_rollout = Path(self.codex_src).read_bytes()
        tb = self.enter_b()
        plan = self.plan(tb, zip_path)
        self.assertFalse(plan['same_machine'])
        self.assertEqual({i['tool']: i['mode'] for i in plan['items']}, dict.fromkeys(a_turns, 'raw'))
        result = self.run_import(tb, plan)
        self.assertEqual(result['counts']['imported'], 4, result['items'])
        self.assertEqual(result['counts']['raw'], 4)
        local = {i['tool']: i for i in tb.load_run(result['id'])['items']}
        # Claude：聊天记录按 Claude Code 自己的规则放到 /fixture 对应的项目目录，字节不变；侧栏条目进 B 的账号目录
        sid = Path(self.f.sources['claude']).stem
        dst = s.CC_ROOT / bridge_ops.claude_project_dirname('/fixture') / (sid + '.jsonl')
        self.assertEqual(dst.read_bytes(), a_claude)
        sidebars = [json.loads(p.read_text()) for p in (s.CC_META_ROOT / ACCT / ORG).glob('local_*.json')]
        mine = [d for d in sidebars if d.get('cliSessionId') == sid]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]['bridgeSessionIds'], [])
        self.assertFalse(mine[0]['remoteControlAutoEligible'])
        # Codex：rollout 原样，库里路径换成 B 的
        rollout = Path(local['codex']['local_src'])
        self.assertTrue(rollout.resolve().is_relative_to(Path(s.CX_ROOT).resolve()))
        self.assertEqual(rollout.read_bytes(), a_rollout)
        c = sqlite3.connect(s.CX_STATE)
        self.assertEqual(c.execute('SELECT rollout_path FROM threads WHERE id=?', (local['codex']['local_sid'],)).fetchone()[0], str(rollout))
        c.close()
        self.assertEqual(len(bridge_ops._index_rows(s, local['codex']['local_sid'])), 1)
        # WorkBuddy：账号换成 B 的
        c = sqlite3.connect(s.WB_DB)
        self.assertEqual(c.execute('SELECT user_id FROM sessions WHERE id=?', (local['workbuddy']['local_sid'],)).fetchone()[0], 'user-b')
        c.close()
        for tool, item in local.items():
            self.assertEqual(s.session_detail(tool, item['local_src']), a_turns[tool], tool)
        self.fk_clean()
        # 再导一次：全都跳过，不覆盖
        again = self.plan(tb, zip_path)
        self.assertEqual({i['mode'] for i in again['items']}, {'skip'})
        self.assertTrue(all('已经有' in i['skip_reason'] for i in again['items']))

    def test_schema_difference_and_missing_tool_fall_back_to_text(self):
        zip_path, _ = self.export(tools=('zcode', 'workbuddy', 'claude'))
        tb = self.enter_b()
        c = sqlite3.connect(s.Z_DB); c.execute('ALTER TABLE session ADD COLUMN made_up_by_newer_version TEXT'); c.commit(); c.close()
        os.unlink(s.WB_DB)
        plan = self.plan(tb, zip_path)
        items = self.items_by_tool(plan)
        self.assertEqual(items['zcode']['mode'], 'text')
        self.assertIn('表结构', items['zcode']['raw_reason'])
        self.assertEqual(items['zcode']['target'], 'zcode')
        self.assertEqual(items['workbuddy']['mode'], 'text')
        self.assertNotIn('workbuddy', items['workbuddy']['text_targets'])
        self.assertEqual(items['claude']['mode'], 'raw')
        result = self.run_import(tb, plan)
        self.assertEqual(result['counts'], {'imported': 3, 'skipped': 0, 'failed': 0, 'raw': 1, 'text': 2})
        for item in tb.load_run(result['id'])['items']:
            if item['mode'] == 'text':
                self.assertEqual(s.session_detail(item['local_tool'], item['local_src']), self.f.turns)
        zitem = [i for i in tb.load_run(result['id'])['items'] if i['tool'] == 'zcode'][0]
        self.assertNotEqual(zitem['local_sid'], self.f.sources['zcode'].removeprefix('zcode:'))
        # 转文字的再导一次：记得导过，跳过
        again = self.items_by_tool(self.plan(tb, zip_path))
        self.assertEqual(again['zcode']['mode'], 'skip')
        self.assertIn('之前已经导入过', again['zcode']['skip_reason'])

    def test_unrealistic_codex_rollout_location_goes_text(self):
        zip_path, _ = self.export(tools=(), extra=[{'tool': 'codex', 'src': self.f.sources['codex']}])
        tb = self.enter_b()
        item = self.plan(tb, zip_path)['items'][0]
        self.assertEqual(item['mode'], 'text')
        self.assertIn('位置不认识', item['raw_reason'])

    def test_choosing_text_mode_for_another_tool(self):
        zip_path, _ = self.export(tools=('claude',))
        tb = self.enter_b()
        plan = self.plan(tb, zip_path)
        item = plan['items'][0]
        self.assertIn('zcode', item['text_targets'])
        result = self.run_import(tb, plan, [{'id': item['id'], 'mode': 'text', 'target': 'zcode'}])
        imported = tb.load_run(result['id'])['items'][0]
        self.assertEqual(imported['local_tool'], 'zcode')
        self.assertEqual(s.session_detail('zcode', imported['local_src']), self.f.turns)
        with self.assertRaisesRegex(ValueError, '不能原样'):
            fresh = self.plan(tb, zip_path)
            bad = dict(fresh['items'][0]); bad['raw_ok'] = False
            tb._plans[fresh['plan_id']]['items'][0]['raw_ok'] = False
            tb.start_import({'plan_id': fresh['plan_id'], 'confirm': True, 'items': [{'id': bad['id'], 'mode': 'raw'}]}, background=False)

    def test_claude_must_quit_before_sidebar_writes(self):
        zip_path, _ = self.export(tools=('claude', 'zcode'))
        tb = self.enter_b(running=True)
        plan = self.plan(tb, zip_path)
        self.assertTrue(plan['claude']['running'])
        claude = self.items_by_tool(plan)['claude']
        with self.assertRaisesRegex(ValueError, '⌘Q|托盘'):
            self.run_import(tb, plan, [{'id': claude['id'], 'mode': 'raw', 'target': 'claude'}])
        zitem = self.items_by_tool(plan)['zcode']
        result = self.run_import(tb, plan, [{'id': zitem['id'], 'mode': 'raw', 'target': 'zcode'}])
        self.assertEqual(result['counts']['imported'], 1)

    def test_tampered_or_hostile_zip_is_rejected(self):
        zip_path, _ = self.export(tools=('claude', 'zcode'))
        with zipfile.ZipFile(zip_path) as zf:
            members = {n: zf.read(n) for n in zf.namelist()}
        manifest = json.loads(members['manifest.json'])
        tb = self.enter_b()

        def write(name, mutate_manifest=None, mutate=None):
            data = dict(members)
            m = json.loads(json.dumps(manifest))
            if mutate_manifest:
                mutate_manifest(m)
            if mutate:
                mutate(data, m)
            data['manifest.json'] = json.dumps(m).encode()
            out = self.rb / name
            with zipfile.ZipFile(out, 'w') as zf:
                for n, b in data.items():
                    zf.writestr(n, b)
            return out

        traversal = write('t1.zip', lambda m: m['sessions'][0]['raw']['files'][0].__setitem__('path', 'sessions/0001/../../evil'))
        with self.assertRaisesRegex(ValueError, '不认识的条目'):
            self.plan(tb, traversal)
        bad_id = write('t2.zip', lambda m: m['sessions'][0].__setitem__('session_id', '../../etc'))
        with self.assertRaisesRegex(ValueError, '会话 ID'):
            self.plan(tb, bad_id)
        not_ours = self.rb / 't3.zip'
        with zipfile.ZipFile(not_ours, 'w') as zf:
            zf.writestr('hello.txt', 'x')
        with self.assertRaisesRegex(ValueError, '不是灵桥'):
            self.plan(tb, not_ours)

        claude = [x for x in manifest['sessions'] if x['tool'] == 'claude'][0]
        tpath = claude['raw']['files'][0]['path']

        def swap_transcript(data, m):
            changed = data[tpath].replace('第一条AI'.encode(), '被人改过'.encode())
            data[tpath] = changed
            for x in m['sessions']:
                for f in x['raw']['files']:
                    if f['path'] == tpath:
                        f['size'] = len(changed)
        tampered = write('t4.zip', mutate=swap_transcript)
        plan = self.plan(tb, tampered)
        claude_item = self.items_by_tool(plan)['claude']
        result = self.run_import(tb, plan, [{'id': claude_item['id'], 'mode': 'raw', 'target': 'claude'}])
        bad = [i for i in result['items'] if i['tool'] == 'claude'][0]
        self.assertEqual(bad['status'], 'failed')
        self.assertIn('对不上', bad['reason'])
        self.assertEqual(list(s.CC_ROOT.rglob(Path(self.f.sources['claude']).stem + '.jsonl')), [])

        zentry = [x for x in manifest['sessions'] if x['tool'] == 'zcode'][0]
        dpath = zentry['raw']['databases'][0]['path']

        def foreign_row(data, m):
            lines = data[dpath].decode().splitlines()
            out = []
            for line in lines:
                rec = json.loads(line)
                if rec.get('t') == 'message':
                    spec = json.loads(lines[0])['tables']['message']
                    rec['r'][spec['columns'].index('session_id')] = 'sess_someone-else-1234'
                out.append(json.dumps(rec, ensure_ascii=False))
            blob = ('\n'.join(out) + '\n').encode()
            data[dpath] = blob
            for x in m['sessions']:
                for d in x['raw']['databases']:
                    if d['path'] == dpath:
                        d['size'] = len(blob); d['sha256'] = hashlib.sha256(blob).hexdigest()
        hostile = write('t5.zip', mutate=foreign_row)
        result = self.run_import(tb, self.plan(tb, hostile))
        bad = [i for i in result['items'] if i['tool'] == 'zcode'][0]
        self.assertEqual(bad['status'], 'failed')
        self.assertIn('别的会话', bad['reason'])
        c = sqlite3.connect(s.Z_DB)
        self.assertEqual(c.execute("SELECT count(*) FROM message WHERE session_id='sess_someone-else-1234'").fetchone()[0], 0)
        c.close()

    def test_undo_moves_imports_to_trash_and_they_restore(self):
        zip_path, _ = self.export()
        tb = self.enter_b()
        result = self.run_import(tb, self.plan(tb, zip_path))
        run = tb.load_run(result['id'])
        undone = tb.undo({'run_id': result['id'], 'confirm': True})['run']['undone']
        self.assertEqual(len(undone['moved']), 4, undone['kept'])
        for item in run['items']:
            if item['local_tool'] == 'zcode':
                c = sqlite3.connect(s.Z_DB)
                self.assertIsNone(c.execute('SELECT 1 FROM session WHERE id=?', (item['local_sid'],)).fetchone())
                c.close()
            else:
                self.assertFalse(Path(item['local_src']).exists())
        with self.assertRaisesRegex(ValueError, '撤销过'):
            tb.undo({'run_id': result['id'], 'confirm': True})
        # 撤销后能从回收站恢复
        trash = {e['trash_id'] for e in undone['moved']}
        for trash_id in trash:
            bridge_ops.restore_session(s, trash_id)
        for item in run['items']:
            self.assertEqual(s.session_detail(item['local_tool'], item['local_src']), self.f.turns, item['tool'])
        self.fk_clean()
        # 撤销后同一个包可以再导（原样的已经恢复了，所以跳过）
        self.assertEqual({i['mode'] for i in self.plan(tb, zip_path)['items']}, {'skip'})

    def test_failure_midway_is_rolled_back_and_crash_is_recovered(self):
        zip_path, _ = self.export(tools=('codex',))
        tb = self.enter_b()
        plan = self.plan(tb, zip_path)

        def boom(stage):
            if stage == 'import_database':
                raise RuntimeError('磁盘出错（模拟）')
        with patch.object(s, 'ops_failpoint', boom, create=True):
            result = self.run_import(tb, plan)
        self.assertEqual(result['items'][0]['status'], 'failed')
        self.assertEqual(list(Path(s.CX_ROOT).rglob('rollout-*.jsonl')), [])
        c = sqlite3.connect(s.CX_STATE); self.assertEqual(c.execute('SELECT count(*) FROM threads').fetchone()[0], 0); c.close()
        c = sqlite3.connect(s.CX_SQLITE); self.assertEqual(c.execute('SELECT count(*) FROM thread_items').fetchone()[0], 0); c.close()

        plan = self.plan(tb, zip_path)

        def crash(stage):
            if stage == 'import_database':
                raise Crash()
        with patch.object(s, 'ops_failpoint', crash, create=True):
            with self.assertRaises(Crash):
                tb._run_import(session_transfer.Job(progress={}), tb._plans[plan['plan_id']],
                               [(plan_item, 'raw', 'codex') for plan_item in tb._plans[plan['plan_id']]['items']])
        self.assertTrue(list(Path(s.CX_ROOT).rglob('rollout-*.jsonl')))
        attention = bridge_ops.recover_pending_operations(s)
        self.assertEqual(attention, [])
        self.assertEqual(list(Path(s.CX_ROOT).rglob('rollout-*.jsonl')), [])
        for db, table in ((s.CX_STATE, 'threads'), (s.CX_SQLITE, 'thread_items'), (s.CX_SQLITE, 'thread_turns')):
            c = sqlite3.connect(db); self.assertEqual(c.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0); c.close()
        self.assertEqual(Path(s.CX_INDEX).read_text() if Path(s.CX_INDEX).exists() else '', '')

    def test_home_paths_are_mapped_to_this_machine(self):
        cwd_a = str(Path(s.HOME) / 'work/项目')
        Path(cwd_a).mkdir(parents=True)
        sid = str(uuid.uuid4())
        p = s.CC_ROOT / bridge_ops.claude_project_dirname(cwd_a) / (sid + '.jsonl')
        p.parent.mkdir(parents=True)
        p.write_text('\n'.join(json.dumps({'type': t['role'], 'sessionId': sid, 'cwd': cwd_a, 'message': {'role': t['role'], 'content': [{'type': 'text', 'text': t['text']}]}}, ensure_ascii=False) for t in self.f.turns) + '\n')
        (p.parent / sid / 'tool-results').mkdir(parents=True)
        (p.parent / sid / 'tool-results' / 'toolu_1.txt').write_text('工具输出')
        (p.parent / sid / 'custom-title.json').write_text('{"title":"自定义"}')
        s.rescan_sessions()
        zip_path, _ = self.export(tools=(), extra=[{'tool': 'claude', 'src': str(p)}])
        tb = self.enter_b()
        (self.rb / 'work/项目').mkdir(parents=True)
        item = self.plan(tb, zip_path)['items'][0]
        self.assertEqual(item['local_cwd'], str(self.rb / 'work/项目'))
        self.assertTrue(any('换成这台电脑上的' in n for n in item['notes']))
        result = self.run_import(tb, self.plan(tb, zip_path))
        self.assertEqual(result['counts']['raw'], 1)
        project = s.CC_ROOT / bridge_ops.claude_project_dirname(str(self.rb / 'work/项目'))
        self.assertEqual((project / sid / 'tool-results' / 'toolu_1.txt').read_text(), '工具输出')
        self.assertEqual((project / sid / 'custom-title.json').read_text(), '{"title":"自定义"}')
        sidebar = [json.loads(x.read_text()) for x in (s.CC_META_ROOT / ACCT / ORG).glob('local_*.json')]
        self.assertEqual([d['cwd'] for d in sidebar if d.get('cliSessionId') == sid], [str(self.rb / 'work/项目')])
        # 撤销：聊天记录进回收站，会话文件夹里的文件挪到撤销区
        undone = tb.undo({'run_id': result['id'], 'confirm': True})['run']['undone']
        self.assertEqual(len(undone['moved']), 1)
        self.assertFalse((project / sid / 'tool-results' / 'toolu_1.txt').exists())
        self.assertTrue(list(Path(undone['holding']).glob('*toolu_1.txt')))

    def test_partial_last_line_of_active_session_is_left_out_and_secrets_only_counted(self):
        fake_key = 'sk-ant-' + 'A1b2C3d4' * 5
        p = Path(self.f.sources['claude'])
        with p.open('a') as stream:
            stream.write(json.dumps({'type': 'user', 'sessionId': p.stem, 'message': {'role': 'user', 'content': '我的钥匙 ' + fake_key}}, ensure_ascii=False) + '\n')
        s.rescan_sessions()
        with p.open('a') as stream:
            stream.write('{"type":"assistant","message":{"role":"assist')   # 还在写的半行
        zip_path, result = self.export(tools=('claude',))
        self.assertGreaterEqual(result['secrets'], 1)
        self.assertNotIn(fake_key, json.dumps(result, ensure_ascii=False))
        self.assertNotIn(fake_key, json.dumps(self.ta.runs(), ensure_ascii=False))
        with zipfile.ZipFile(zip_path) as zf:
            manifest = json.loads(zf.read('manifest.json'))
            raw = zf.read(manifest['sessions'][0]['raw']['files'][0]['path'])
        self.assertTrue(raw.endswith(b'\n'))
        self.assertNotIn(b'{"type":"assistant","message":{"role":"assist', raw)
        self.assertEqual(len(raw.splitlines()), 4)
        self.assertNotIn(fake_key, json.dumps(manifest, ensure_ascii=False))

    def test_handles_and_paths_are_validated(self):
        with self.assertRaisesRegex(ValueError, '过期'):
            self.ta.inspect({'handle': 'nope'})
        with self.assertRaisesRegex(ValueError, '数据目录'):
            self.ta.register('export', str(s.CC_ROOT / 'x.zip'))
        target = Path(s.HOME) / 'out.zip'
        target.write_text('old')
        with self.assertRaisesRegex(ValueError, '不覆盖'):
            self.ta.register('export', str(target))
        handle = self.ta.register('export', str(Path(s.HOME) / 'fresh'))
        self.assertEqual(handle['name'], 'fresh.zip')
        with self.assertRaisesRegex(ValueError, '过期'):
            self.ta.inspect({'handle': handle['handle']})  # 导出用的编号不能拿来导入
        with self.assertRaisesRegex(ValueError, '先选'):
            self.ta.start_export({'items': []}, background=False)

    def test_http_routes_need_token(self):
        import http.client, threading
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), s.http_api.make_handler(s)); port = httpd.server_port
        with patch.object(s, 'PORT', port), patch.object(s, 'transfer', self.ta):
            threading.Thread(target=httpd.serve_forever, daemon=True).start()

            def request(path, headers=None, method='GET', body=None):
                c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                c.request(method, path, json.dumps(body) if body is not None else None, headers or {})
                r = c.getresponse(); raw = r.read(); c.close()
                return r.status, (json.loads(raw) if raw else None)
            try:
                auth = {'X-Bridge-Token': s.API_TOKEN}; js = {**auth, 'Content-Type': 'application/json'}
                self.assertEqual(request('/api/transfer/status')[0], 403)
                self.assertEqual(request('/static/transfer.js')[0], 403)
                code, body = request('/api/transfer/status', auth)
                self.assertEqual(code, 200); self.assertIn('machine', body); self.assertIn('tools', body)
                self.assertEqual(request('/api/transfer/inspect', js, 'POST', {'handle': 'x'})[0], 400)
                self.assertEqual(request('/api/transfer/export', {**js, 'Origin': 'https://evil.invalid'}, 'POST', {'items': []})[0], 403)
                big = {'items': [{'tool': 'claude', 'src': 'x' * 900}] * 1000}
                self.assertEqual(request('/api/transfer/export', js, 'POST', big)[0], 400)   # 1 MB 以内收下，再按条数拒绝
                code, body = request('/api/transfer/export', js, 'POST', {'items': [{'tool': 'claude', 'src': self.f.sources['claude']}]})
                self.assertEqual(code, 200, body)
                for _ in range(200):
                    status, job = request('/api/transfer/job?id=' + body['job_id'], auth)
                    if job['status'] != 'running':
                        break
                    time.sleep(0.05)
                self.assertEqual(job['status'], 'done', job)
                self.assertEqual(request('/api/transfer/runs', auth)[1]['runs'][0]['kind'], 'export')
            finally:
                httpd.shutdown(); httpd.server_close()


if __name__ == '__main__':
    unittest.main()
