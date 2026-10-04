"""DOM-free regressions for the 会话导出 / 导入 page (app/transfer.js) and its index.html hook.

All responses are synthetic; nothing reads real sessions or files.
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
const masks = new Map();
const document = { querySelector: element, createElement: element, getElementById: id => { if (!masks.has(id)) masks.set(id, element(id)); return masks.get(id); }, addEventListener() {}, removeEventListener() {}, hidden: false };
let responder = async () => ({});
const window = { BRIDGE_TOKEN: 'fixture-token', toast: (m, ok) => toasts.push([m, ok]) };
function respond(status, data) { return { ok: status < 400, status, json: async () => data }; }
const context = vm.createContext({ window, document, Headers, AbortController, Map, Set, Promise, console, assert, calls, toasts, Date, encodeURIComponent,
  setTimeout() { return 0; }, clearTimeout() {},
  fetch: async (url, options = {}) => { calls.push({ url, options });
    const r = await responder(url, options); return r && r.__status ? respond(r.__status, r.body) : respond(200, r); } });
context.setResponder = fn => { responder = fn; };
(async () => {
  vm.runInContext(source, context);
  context.T = window.LingqiaoTransfer._t;
  await vm.runInContext(TEST, context);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''

DATA = r'''
const now = Date.now();
const sent = i => JSON.parse(calls[i].options.body);
const sessions = [
  { tool: 'claude', src: '/h/.claude/projects/p/a.jsonl', title: '灵桥 3.3 <img src=x onerror=alert(1)>', dir: '/Volumes/WorkDisk', mtime: now / 1000 - 600, size: 3 << 20, kind: 'user' },
  { tool: 'codex', src: '/h/.codex/sessions/2026/10/03/rollout-x.jsonl', title: '续写数学', dir: '/Users/x/Documents', mtime: now / 1000 - 86400 * 3, size: 500 << 20, kind: 'user', mirror: true },
  { tool: 'zcode', src: 'zcode:sess_aaaaaaaaaaaa', title: '小学数学', dir: '/Users/x', mtime: now / 1000 - 86400 * 40, size: 1 << 20, kind: 'user' },
  { tool: 'workbuddy', src: 'wb:abcdefgh', title: '没有文件', dir: '/x', mtime: now / 1000, size: 0, kind: 'user', missing_file: true },
  { tool: 'codex', src: '/h/.codex/sessions/2026/10/03/rollout-y.jsonl', title: '子代理', dir: '/Users/x', mtime: now / 1000 - 100, size: 1024, kind: 'subagent' } ];
const plan = { plan_id: 'p1', zip: '灵桥会话导出_<b>mini</b>.zip', size: 1234, source: { machine: 'Mac <i>mini</i>', lingqiao: '3.4.0' }, created_at: '2026-10-03 22:00:00',
  same_machine: false, this_machine: 'Air', claude: { running: false }, counts: { raw: 2, text: 1, skip: 1 },
  items: [
    { id: 1, tool: 'claude', title: '<script>x</script>', cwd: '/Users/a/p', local_cwd: '/Users/b/p', mtime: 1, size: 10, turns: 3, secrets: 0, kind: 'user', archived: false, mirror: false,
      notes: ['工作目录换成这台电脑上的 /Users/b/p'], raw_ok: true, raw_reason: '', sidebar: true, mode: 'raw', skip_reason: '', target: 'claude', text_targets: ['claude', 'codex', 'zcode'] },
    { id: 2, tool: 'zcode', title: 'z', cwd: '/x', local_cwd: '/x', mtime: 1, size: 10, turns: 2, secrets: 0, kind: 'user', archived: false, mirror: false,
      notes: [], raw_ok: false, raw_reason: '表结构和导出那台电脑不一样（session）', sidebar: false, mode: 'text', skip_reason: '', target: 'zcode', text_targets: ['claude', 'codex', 'zcode'] },
    { id: 3, tool: 'codex', title: 'c', cwd: '/x', local_cwd: '/x', mtime: 1, size: 10, turns: 2, secrets: 0, kind: 'user', archived: false, mirror: false,
      notes: [], raw_ok: true, raw_reason: '', sidebar: false, mode: 'raw', skip_reason: '', target: 'codex', text_targets: ['claude', 'codex', 'zcode'] },
    { id: 4, tool: 'workbuddy', title: 'w', cwd: '/x', local_cwd: '/x', mtime: 1, size: 10, turns: 2, secrets: 0, kind: 'user', archived: false, mirror: false,
      notes: [], raw_ok: false, raw_reason: '', sidebar: false, mode: 'skip', skip_reason: '这台电脑上已经有这条会话了（不覆盖）', target: '', text_targets: [] } ] };
'''


@unittest.skipUnless(NODE, "Node.js is required for front-end regression tests")
class TransferFrontendTests(unittest.TestCase):
    def run_js(self, code):
        runner = "const PAGE = " + json.dumps(str(ROOT / "app/transfer.js")) + "; const TEST = " + json.dumps(DATA + code) + ";\n" + HARNESS
        result = subprocess.run([NODE, "-"], input=runner, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_syntax(self):
        result = subprocess.run([NODE, "--check", str(ROOT / "app/transfer.js")], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_selection_and_choices(self):
        self.run_js(r"""
          const picked = T.pickAll(sessions, new Set());
          assert.equal(picked.size, 3);                              // 全选不含同步副本、缺文件的
          assert.ok(!picked.has(T.keyOf(sessions[1]))); assert.ok(!picked.has(T.keyOf(sessions[3])));
          assert.doesNotMatch(T.keyOf(sessions[0]), /[\u0000-\u001f]/);   // 进 HTML 属性的标识不能有控制字符
          const sum = T.selectionSummary(sessions, picked);
          assert.equal(sum.count, 3); assert.equal(sum.bytes, (3 << 20) + (1 << 20) + 1024);
          assert.deepEqual(JSON.parse(JSON.stringify(sum.items)), [{ tool: 'claude', src: sessions[0].src }, { tool: 'zcode', src: sessions[2].src }, { tool: 'codex', src: sessions[4].src }]);
          const choices = T.initialChoices(plan);
          assert.equal(choices.get(4).on, false); assert.equal(choices.get(1).mode, 'raw'); assert.equal(choices.get(2).mode, 'text');
          assert.deepEqual(T.modeOptions(plan.items[0]).map(o => o.value), ['raw:claude', 'text:claude', 'text:codex', 'text:zcode']);
          assert.deepEqual(T.modeOptions(plan.items[1]).map(o => o.value), ['text:claude', 'text:codex', 'text:zcode']);
          const body = T.importBody(plan, choices);
          assert.deepEqual(JSON.parse(JSON.stringify(body)), { plan_id: 'p1', confirm: true, items: [
            { id: 1, mode: 'raw', target: 'claude' }, { id: 2, mode: 'text', target: 'zcode' }, { id: 3, mode: 'raw', target: 'codex' } ] });
          assert.equal(T.needsClaudeQuit(plan, choices), true);
          choices.get(1).on = false;
          assert.equal(T.needsClaudeQuit(plan, choices), false);
          choices.get(2).target = 'claude';
          assert.equal(T.needsClaudeQuit(plan, choices), true);       // 转文字进 Claude 也要退出桌面版
          assert.match(T.importBlockReason({ plan: null }), /先选/);
          assert.match(T.importBlockReason({ plan, choices: new Map([[1, { on: false }]]) }), /勾选/);
          assert.match(T.importBlockReason({ plan, choices, claude: { running: true } }), /⌘Q/);
          assert.match(T.importBlockReason({ plan, choices, claude: { running: null } }), /没法确认/);
          assert.equal(T.importBlockReason({ plan, choices, claude: { running: false } }), '');
          assert.match(T.progressText({ kind: 'export', progress: { done: 1, total: 4, title: '续写数学', bytes: 2 << 20 } }), /第 2 \/ 4 条 · 续写数学 · 已读 2\.0 MB/);
          assert.equal(T.percent({ progress: { done: 1, total: 4 } }), 25);
        """)

    def test_everything_shown_is_escaped(self):
        self.run_js(r"""
          const S = T.state; S.rows = sessions; S.status = { machine: { name: 'mini' }, tools: {} }; S.picked = new Set([T.keyOf(sessions[0])]);
          const html = T.batchView();
          assert.doesNotMatch(html, /<img src=x/); assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
          assert.match(html, /同步副本/); assert.match(html, /缺文字记录/); assert.match(html, /已选 <b>1<\/b> 条/);
          S.plan = plan; S.choices = T.initialChoices(plan);
          const imp = T.importView();
          assert.doesNotMatch(imp, /<script>x|<b>mini|<i>mini/); assert.match(imp, /&lt;script&gt;x&lt;\/script&gt;/);
          assert.match(imp, /不能原样：表结构/); assert.match(imp, /跳过/); assert.match(imp, /工作目录换成这台电脑上的/);
          assert.match(imp, /Claude 桌面版已经退出/);
          const run = { id: '20261003-220000-abcdef', kind: 'import', at: 'x', zip: '<u>z</u>.zip', source: { machine: '<i>m</i>' }, undone: null,
            counts: { imported: 1, skipped: 1, failed: 1, raw: 1, text: 0 },
            items: [{ id: 1, tool: 'claude', title: '<b>t</b>', mode: 'raw', status: 'imported', notes: ['<s>n</s>'], local_tool: 'claude' },
                    { id: 2, tool: 'zcode', title: 'z', mode: 'raw', status: 'skipped', reason: '已经有了' },
                    { id: 3, tool: 'codex', title: 'c', mode: 'raw', status: 'failed', reason: '<img>坏了' }] };
          const card = T.importResultCard(run);
          assert.doesNotMatch(card, /<u>|<i>m|<b>t|<s>n|<img>/); assert.match(card, /撤销这次导入/); assert.match(card, /data-run="20261003-220000-abcdef"/);
          const exp = T.exportResultCard({ id: 'e1', at: 'x', machine: '<b>m</b>', path: '/Users/x/Downloads/<i>a</i>.zip', size: 2048, sessions: 2,
            by_tool: { claude: 1, codex: 1 }, secrets: 3, secret_sessions: 1, skipped: [{ tool: 'zcode', title: '<p>', reason: '<q>' }] });
          assert.doesNotMatch(exp, /<b>m|<i>a|<p>|<q>/); assert.match(exp, /像密钥的内容/); assert.match(exp, /在访达中显示/); assert.match(exp, /导入会话/);
          S.runs = [{ ...run, undone: { at: 'y', moved: [], kept: [] } }, { id: 'e1', kind: 'export', at: 'x', name: '<b>a</b>.zip', sessions: 1, size: 1, exists: true }];
          const runs = T.runsList();
          assert.doesNotMatch(runs, /<b>a/); assert.match(runs, /已撤销/); assert.match(runs, /在访达中显示/);
          S.exportItems = [sessions[0]]; S.exportResult = null;
          assert.match(T.exportView(), /导出 1 条会话/); assert.doesNotMatch(T.exportView(), /<img src=x/);
        """)

    def test_export_uses_native_dialog_handle_and_never_sends_paths(self):
        self.run_js(r"""
          (async () => {
            const S = T.state; S.status = { export_dir: '/Users/x/Downloads', machine: { name: 'mini' } };
            const rows = [sessions[0], sessions[2], sessions[3]];          // 第三条缺文件，不导
            setResponder(async url => url === '/api/transfer/export' ? { job_id: 'j1', job: { id: 'j1', kind: 'export', status: 'running', progress: {} } } : {});
            let result = await T.requestExport(rows, { native: { transfer_choose_export: async n => { assert.equal(n, 2); return { cancelled: true }; } } });
            assert.equal(result, null); assert.equal(calls.length, 0);
            result = await T.requestExport(rows, { native: { transfer_choose_export: async () => ({ error: '对话框打不开' }) } });
            assert.equal(result, null); assert.match(toasts.at(-1)[0], /对话框打不开/);
            result = await T.requestExport(rows, { native: { transfer_choose_export: async () => ({ handle: 'h-1', name: 'a.zip', dir: '/Users/x/Downloads' }) } });
            assert.equal(calls[0].url, '/api/transfer/export');
            assert.deepEqual(sent(0), { items: [{ tool: 'claude', src: sessions[0].src }, { tool: 'zcode', src: sessions[2].src }], handle: 'h-1' });
            assert.equal(calls[0].options.headers.get('X-Bridge-Token'), 'fixture-token');
            assert.doesNotMatch(calls[0].options.body, /Downloads/);   // 保存位置在服务端，不从页面传
            assert.equal(S.view, 'export'); assert.equal(S.exportItems.length, 2);
            S.job = null;
            const asked = [];
            await T.requestExport([sessions[0]], { native: null, confirm: async o => { asked.push(o); return true; } });
            assert.match(asked[0].body, /Downloads/); assert.deepEqual(Object.keys(sent(1)), ['items']);
            S.job = { status: 'running' };
            assert.equal(await T.requestExport([sessions[0]], { native: null }), null); assert.match(toasts.at(-1)[0], /还没做完/);
            S.job = null;
            assert.equal(await T.requestExport([sessions[3]], { native: null }), null); assert.match(toasts.at(-1)[0], /先选/);
          })()
        """)

    def test_import_flow_confirms_and_respects_claude(self):
        self.run_js(r'''
          (async () => {
            const S = T.state;
            setResponder(async url => url === '/api/transfer/inspect' ? plan : url === '/api/transfer/import' ? { job_id: 'j2', job: { id: 'j2', kind: 'import', status: 'running', progress: {} } } : {});
            await T.chooseImport({ native: { transfer_choose_import: async () => ({ handle: 'h-2', name: 'x.zip' }) } });
            assert.equal(calls[0].url, '/api/transfer/inspect'); assert.deepEqual(sent(0), { handle: 'h-2' });
            assert.equal(S.plan.plan_id, 'p1'); assert.equal(S.choices.size, 4);
            S.claude = { running: true };
            let asked = 0;
            assert.equal(await T.requestImport({ confirm: async () => { asked++; return true; } }), null);
            assert.equal(asked, 0); assert.match(toasts.at(-1)[0], /⌘Q/);
            S.claude = { running: false };
            const seen = [];
            await T.requestImport({ confirm: async o => { seen.push(o); return false; } });
            assert.match(seen[0].check, /⌘Q/); assert.match(seen[0].body, /原样导入 <b>2<\/b> 条，转成文字导入 <b>1<\/b> 条/);
            assert.equal(calls.length, 1);
            await T.requestImport({ confirm: async () => true });
            assert.equal(calls[1].url, '/api/transfer/import');
            assert.deepEqual(sent(1), { plan_id: 'p1', confirm: true, items: [{ id: 1, mode: 'raw', target: 'claude' }, { id: 2, mode: 'text', target: 'zcode' }, { id: 3, mode: 'raw', target: 'codex' }] });
            setResponder(async url => url === '/api/transfer/undo' ? { run: { id: 'r', undone: { moved: [{}, {}], kept: [{ reason: '改过' }] } } } : {});
            S.runs = [{ id: '20261003-220000-abcdef', kind: 'import', items: [{ status: 'imported', local_tool: 'zcode' }] }];
            const undo = await T.requestUndo('20261003-220000-abcdef', { confirm: async o => { assert.equal(o.check, ''); return true; } });
            assert.ok(undo); assert.deepEqual(sent(2), { run_id: '20261003-220000-abcdef', confirm: true });
            assert.match(toasts.at(-1)[0], /2 条进了回收站，1 条没动/);
            const none = await T.chooseImport({ native: null });
            assert.equal(none, null); assert.match(toasts.at(-1)[0], /最近的导出包/);
          })()
        ''')

    def test_index_hooks(self):
        html = (ROOT / "app/index.html").read_text(encoding="utf-8")
        # 入口在会话列表里：每条会话旁边的「导出」，工具栏的「批量导出」「导入会话」；不单独占侧栏分组
        self.assertNotIn('data-g="transfer"', html)
        self.assertNotIn('id="page-transfer"', html)
        self.assertIn('class="exp-btn"', html)
        self.assertIn('page.exportSessions([s])', html)
        self.assertIn('id="transfer-batch"', html)
        self.assertIn('id="transfer-import"', html)
        self.assertIn('page.openBatch(state.data)', html)
        self.assertIn('<div class="modal-mask tf-mask" id="transfer-mask"></div>', html)
        self.assertIn('"/static/transfer.css", "/static/transfer.js"', html)
        loader = html[html.index("function loadTransferAssets"):html.index("async function withTransfer")]
        self.assertIn('"X-Bridge-Token": window.BRIDGE_TOKEN', loader)
        self.assertNotIn("token=", loader)
        script = re.search(r"<script>([\s\S]*)</script>\s*</body>", html).group(1)
        result = subprocess.run([NODE, "--check", "-"], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        js = (ROOT / "app/transfer.js").read_text(encoding="utf-8")
        self.assertNotIn("?token=", js)


if __name__ == "__main__":
    unittest.main()
