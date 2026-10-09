"""DOM-free regressions for the Claude 双账号会话 page (app/accounts.js) and its index.html hook.

All responses are synthetic; nothing reads the real Claude folders.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

HARNESS = r'''
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(PAGE, 'utf8');
const calls = [], toasts = [];
function element(tag) {
  const selectors = new Map(); let html = '';
  return { tag, dataset: {}, style: {}, disabled: false, value: '', checked: false,
    classList: { values: new Set(), add(v) { this.values.add(v); }, remove(v) { this.values.delete(v); }, contains(v) { return this.values.has(v); } },
    set innerHTML(v) { html = String(v); selectors.clear(); }, get innerHTML() { return html; },
    querySelector(s) { if (!selectors.has(s)) selectors.set(s, element(s)); return selectors.get(s); },
    querySelectorAll() { return []; }, addEventListener() {}, removeEventListener() {} };
}
const document = { querySelector: element, createElement: element, addEventListener() {}, removeEventListener() {} };
let responder = async () => ({});
const window = { BRIDGE_TOKEN: 'fixture-token', toast: (m, ok) => toasts.push([m, ok]) };
function respond(status, data) { return { ok: status < 400, status, json: async () => data }; }
const context = vm.createContext({ window, document, Headers, AbortController, Map, Set, Promise, console, assert, calls, toasts,
  setTimeout() { return 0; }, clearTimeout() {},
  fetch: async (url, options = {}) => { calls.push({ url, options });
    const r = await responder(url, options); return r && r.__status ? respond(r.__status, r.body) : respond(200, r); } });
context.setResponder = fn => { responder = fn; };
(async () => {
  vm.runInContext(source, context);
  context.T = window.LingqiaoAccounts._t;
  await vm.runInContext(TEST, context);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''

ACCOUNTS = r'''
const A='aaaaaaaa-0000-4000-8000-00000000000a', B='bbbbbbbb-0000-4000-8000-00000000000b', C='cccccccc-0000-4000-8000-00000000000c', D='dddddddd-0000-4000-8000-00000000000d';
const status = { available: true, claude: { running: false, blocking: [], count: 0, error: null }, backup: { dir: '/Volumes/Backup/灵桥备份', fallback: false }, runs: [],
  accounts: [ { id: B, short: 'bbbbbbbb', label: '团队账号', name: '团队账号', sessions: 63, tombstones: 21, newest: 1759480000000, recent: false, target_org: '2', ambiguous: false, orgs: [] },
              { id: A, short: 'aaaaaaaa', label: '个人账号', name: '个人账号', sessions: 27, tombstones: 6, newest: 1759490000000, recent: true, target_org: '1', ambiguous: false, orgs: [] },
              { id: C, short: 'cccccccc', label: '', name: '账号 cccccccc', sessions: 63, tombstones: 14, newest: 1, recent: false, target_org: '3', ambiguous: false, orgs: [] },
              { id: D, short: 'dddddddd', label: '', name: '账号 dddddddd', sessions: 0, tombstones: 0, newest: null, recent: false, target_org: null, ambiguous: false, orgs: [] } ] };
const sent = i => JSON.parse(calls[i].options.body);  // 在 vm 自己的 realm 里解析，deepEqual 才比得上
const plan = { source: A, source_label: '个人账号', target: B, target_label: '团队账号', target_org: '22222222-x', window_hours: 24,
  counts: { copy: 1, update: 1, already: 2, out_of_window: 3, unreadable: 0, no_transcript: 1, deleted: 1, no_id: 0, duplicate: 0, conflict: 0, changed: 0, error: 0 },
  copy: [{ file: 'local_1.json', title: '<img src=x onerror=alert(1)>', last: 1759490000000 }],
  update: [{ file: 'local_2.json', title_old: '旧标题', title_new: '新<b>标题</b>' }], skipped: [{ title: '不在本机', reason_text: '聊天记录不在本机' }], already: [] };
'''


@unittest.skipUnless(NODE, "Node.js is required for front-end regression tests")
class AccountsFrontendTests(unittest.TestCase):
    def run_js(self, code):
        runner = "const PAGE = " + json.dumps(str(ROOT / "app/accounts.js")) + "; const TEST = " + json.dumps(ACCOUNTS + code) + ";\n" + HARNESS
        result = subprocess.run([NODE, "-"], input=runner, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_syntax(self):
        result = subprocess.run([NODE, "--check", str(ROOT / "app/accounts.js")], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_form_and_sync_gate(self):
        self.run_js(r'''
          const form = T.defaultForm(status);
          assert.deepEqual(form, { source: A, target: B, window: 24, both: true });  // 最近活跃 → 另一个有名字的账号，默认两边补齐
          assert.deepEqual(T.defaultForm({ accounts: [status.accounts[2], status.accounts[3]] }), { source: C, target: '', window: 24, both: true });
          const preview = { plans: [plan], nothing: false };
          const state = { busy: false, status, form, preview, previewKey: T.planKey(form) };
          assert.equal(T.canSync(state), true); assert.equal(T.syncBlockReason(state), '');
          assert.equal(T.canSync({ ...state, previewKey: T.planKey({ ...form, window: 3 }) }), false);   // 改了参数要重新预览
          assert.match(T.syncBlockReason({ ...state, preview: null }), /预览/);
          assert.equal(T.canSync({ ...state, preview: { plans: [], nothing: true } }), false);
          const running = { ...state, status: { ...status, claude: { running: true, blocking: [{ pid: 1, name: 'Claude' }], count: 13 } } };
          assert.equal(T.canSync(running), false); assert.match(T.syncBlockReason(running), /⌘Q/);
          const unknown = { ...state, status: { ...status, claude: { running: null, error: '查不了进程' } } };
          assert.equal(T.canSync(unknown), false); assert.match(T.syncBlockReason(unknown), /没法确认/);
          assert.equal(T.canSync({ ...state, busy: true }), false);
          assert.equal(T.claudeText({ running: false }).cls, 'ok'); assert.equal(T.claudeText({ running: true, count: 13 }).cls, 'bad');
          assert.match(T.claudeText({ running: true, count: 13 }).text, /13 个进程/); assert.equal(T.claudeText(null).cls, 'warn');
          assert.deepEqual(T.totals([plan, plan]), { copy: 2, update: 2 }); assert.equal(T.skippedCount(plan.counts), 2);
        ''')

    def test_titles_are_escaped(self):
        self.run_js(r'''
          const html = T.planCard(plan);
          assert.doesNotMatch(html, /<img/); assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
          assert.match(html, /新&lt;b&gt;标题&lt;\/b&gt;/); assert.match(html, /聊天记录不在本机/);
          const card = T.accountCard({ ...status.accounts[0], name: '<script>x</script>' });
          assert.doesNotMatch(card, /<script>/);
          const run = { id: '20261003-170000-abcd', at: '2026-10-03 17:00:00', direction: 'a → <i>b</i>', totals: { copied: 1, updated: 0, failed: 0 },
            backup: { path: '/Volumes/Backup/灵桥备份/Claude会话迁移备份_20261003_170000', files: 200, bytes: 2048, fallback: false }, warnings: [], undone: null, results: [{ target_label: '<u>团队</u>' }] };
          const done = T.runCard(run);
          assert.doesNotMatch(done, /<i>|<u>/); assert.match(done, /打开 Claude 桌面版/); assert.match(done, /data-run="20261003-170000-abcd"/);
          assert.match(T.runsTable([{ ...run, undone: { at: 'x' } }]), /已撤销/);
        ''')

    def test_sync_asks_first_and_sends_confirm(self):
        self.run_js(r'''
          (async () => {
            const S = T.state; S.status = status; S.form = T.defaultForm(status);
            S.preview = { plans: [plan], nothing: false, backup: status.backup }; S.previewKey = T.planKey(S.form);
            const asked = [];
            let result = await T.requestSync({ confirm: async options => { asked.push(options); return false; } });
            assert.equal(result, null); assert.equal(calls.length, 0);
            assert.match(asked[0].check, /⌘Q/); assert.match(asked[0].body, /灵桥备份/); assert.match(asked[0].body, /复制 <b>1<\/b> 条/);
            setResponder(async url => url === '/api/accounts/sync'
              ? { ok: true, nothing: false, run: { id: 'r', totals: { copied: 1, updated: 1, failed: 0 }, results: [], backup: {} } } : status);
            result = await T.requestSync({ confirm: async () => true });
            assert.equal(calls[0].url, '/api/accounts/sync'); assert.equal(calls[0].options.method, 'POST');
            assert.deepEqual(sent(0), { source: A, target: B, window_hours: 24, both: true, confirm: true, pick: { [A + '>' + B]: ['local_1.json', 'local_2.json'] } });
            assert.equal(calls[0].options.headers.get('X-Bridge-Token'), 'fixture-token'); assert.doesNotMatch(calls[0].url, /token/);
            assert.equal(calls[1].url, '/api/accounts/status');
            assert.match(toasts.at(-1)[0], /同步好了：复制 1 条、刷新标题 1 个/);
            assert.equal(S.preview, null);  // 同步后要重新预览
          })()
        ''')

    def test_sync_refused_without_quitting_and_undo_flow(self):
        self.run_js(r'''
          (async () => {
            const S = T.state; S.status = { ...status, claude: { running: true, count: 3 } }; S.form = T.defaultForm(status);
            S.preview = { plans: [plan], nothing: false }; S.previewKey = T.planKey(S.form);
            let asked = 0;
            assert.equal(await T.requestSync({ confirm: async () => { asked++; return true; } }), null);
            assert.equal(asked, 0); assert.equal(calls.length, 0); assert.match(toasts.at(-1)[0], /⌘Q/);
            setResponder(async url => url === '/api/accounts/undo'
              ? { ok: true, run: { undone: { moved: [{}], restored: [], kept: [{ file: 'x', reason: '动过' }] } } } : status);
            const result = await T.requestUndo('20261003-170000-abcd', { confirm: async options => { assert.match(options.check, /⌘Q/); return true; } });
            assert.ok(result); assert.deepEqual(sent(0), { run_id: '20261003-170000-abcd' });
            assert.match(toasts.at(-1)[0], /挪走 1 条、标题改回 0 个、1 条没动/);
            setResponder(async () => ({ __status: 400, body: { error: 'Claude 桌面版还开着：先在桌面版里按 ⌘Q 完全退出' } }));
            await T.requestUndo('20261003-170000-abcd', { confirm: async () => true });
            assert.match(toasts.at(-1)[0], /撤销没做：Claude 桌面版还开着/);
          })()
        ''')

    def test_pick_sessions_before_sync(self):
        self.run_js(r"""
          (async () => {
            const S = T.state; S.status = status; S.form = T.defaultForm(status);
            S.preview = { plans: [plan], nothing: false, backup: status.backup }; S.previewKey = T.planKey(S.form); S.unpicked = {};
            const key = A + '>' + B;
            // 默认全勾：列表里每条都有勾选框
            let html = T.planCard(plan);
            assert.match(html, /已勾 2 \/ 2/); assert.equal((html.match(/class="as-pick-box"/g) || []).length, 2);
            assert.equal((html.match(/ checked>/g) || []).length, 2); assert.match(html, /data-act="pick-none"/);
            assert.deepEqual(T.pickFor([plan], S.unpicked), { [key]: ['local_1.json', 'local_2.json'] });
            // 去掉一条：只发勾上的；确认框里写明没勾的不动
            T.onPick({ dataset: { pick: key, file: 'local_2.json' }, checked: false });
            assert.deepEqual(T.pickedCounts(plan, S.unpicked), { copy: 1, update: 0 });
            assert.equal((T.planCard(plan).match(/ checked>/g) || []).length, 1);
            const asked = [];
            setResponder(async url => url === '/api/accounts/sync'
              ? { ok: true, nothing: false, run: { id: 'r', totals: { copied: 1, updated: 0, failed: 0 }, results: [], backup: {} } } : status);
            await T.requestSync({ confirm: async options => { asked.push(options); return true; } });
            assert.match(asked[0].body, /复制 <b>1<\/b> 条，刷新标题 <b>0<\/b> 个（没勾的 1 条不动）/);
            assert.deepEqual(sent(0).pick, { [key]: ['local_1.json'] });
            assert.deepEqual(S.unpicked, {});  // 同步后重新预览，勾选也重新开始
            // 全不选：不能同步，提示去勾
            S.preview = { plans: [plan], nothing: false }; S.previewKey = T.planKey(S.form);
            T.pickAll(key, false);
            assert.equal(T.canSync(S), false); assert.match(T.syncBlockReason(S), /一条都没勾/);
            const before = calls.length;
            assert.equal(await T.requestSync({ confirm: async () => true }), null); assert.equal(calls.length, before);
            T.pickAll(key, true); assert.equal(T.canSync(S), true);
            // 文件名进属性也要转义
            assert.doesNotMatch(T.planCard({ ...plan, copy: [{ file: '"><img src=x>', title: 't', last: 1 }] }), /<img/);
          })()
        """)

    def test_hidden_accounts_are_left_out(self):
        self.run_js(r"""
          (async () => {
            const hiddenStatus = { ...status, accounts: status.accounts.map(a => a.id === A ? { ...a, hidden: true, recent: false } : a.id === B ? { ...a, recent: true } : a) };
            // 隐藏的不进下拉框、不当默认来源
            assert.doesNotMatch(T.options(hiddenStatus.accounts, '', false), new RegExp(A));
            assert.match(T.options(hiddenStatus.accounts, '', false), new RegExp(C));
            assert.notEqual(T.defaultForm(hiddenStatus).source, A); assert.notEqual(T.defaultForm(hiddenStatus).target, A);
            assert.deepEqual(T.visible(hiddenStatus.accounts).map(a => a.id), [B, C, D]);
            // 卡片上的按钮：没隐藏的是「隐藏」，隐藏的是「取消隐藏」，放在「已隐藏的账号」里
            assert.match(T.accountCard(status.accounts[2]), /data-act="hide" data-account="cccccccc/);
            const section = T.hiddenSection(hiddenStatus.accounts);
            assert.match(section, /已隐藏的账号 1 个/); assert.match(section, /data-act="unhide" data-account="aaaaaaaa/);
            assert.equal(T.hiddenSection(status.accounts), '');
            // 隐藏正在用的来源账号：表单换成别的账号，预览作废
            const S = T.state; S.status = status; S.form = { source: A, target: B, window: 24, both: true };
            S.preview = { plans: [plan], nothing: false }; S.previewKey = T.planKey(S.form);
            setResponder(async () => hiddenStatus);
            await T.requestHide(A, true);
            assert.equal(calls[0].url, '/api/accounts/hide'); assert.deepEqual(sent(0), { account: A, hidden: true });
            assert.notEqual(S.form.source, A); assert.equal(S.preview, null);
            assert.match(toasts.at(-1)[0], /已隐藏「个人账号」：会话文件都没动/);
            setResponder(async () => ({ __status: 400, body: { error: '隐藏参数不对' } }));
            assert.equal(await T.requestHide(C, true), null); assert.match(toasts.at(-1)[0], /隐藏没做：隐藏参数不对/);
          })()
        """)

    def test_index_hooks(self):
        html = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn('data-g="accounts"', html)
        self.assertIn('<div id="page-accounts"></div>', html)
        self.assertIn('"/static/accounts.css", "/static/accounts.js"', html)
        self.assertIn('state.group === "accounts"', html)
        loader = html[html.index("function loadAccountsAssets"):html.index("async function openAccounts")]
        self.assertIn('"X-Bridge-Token": window.BRIDGE_TOKEN', loader)
        self.assertNotIn("token=", loader)
        script = re.search(r"<script>([\s\S]*)</script>\s*</body>", html).group(1)
        result = subprocess.run([NODE, "--check", "-"], input=script, text=True, capture_output=True) if NODE else None
        if result is not None:
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
