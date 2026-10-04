"""侧栏插件条目、「检查更新」按钮和更新弹窗（app/update.js）的页面逻辑。请求全是假数据。"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from test_frontend import HARNESS

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

PLUGINS = [
    {"id": "demo-x", "name": "示例插件", "version": "1.0.0", "error": "",
     "nav": {"section": "示例区", "label": "<b>示例</b>", "symbol": "◇", "title": "示例插件的说明"},
     "frontend": {"global": "DemoPlugin", "script": "demo.js", "style": "demo.css"},
     "status": {"available": False, "reason": "硬盘没接"}},
    {"id": "broken-x", "name": "坏插件", "version": "", "error": "创建时出错", "nav": {"label": "坏的"}, "frontend": {},
     "status": {"available": False, "reason": "创建时出错"}},
    {"id": "Bad Id", "name": "名字不对", "nav": {}, "frontend": {}},
]

UPDATE_HARNESS = r'''
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(PAGE, 'utf8');
const calls = [], timers = [];
function element(tag) {
  let html = '';
  return { tag, dataset: {}, children: [], classList: { values: new Set(), add(v) { this.values.add(v); }, remove(v) { this.values.delete(v); }, contains(v) { return this.values.has(v); } },
    set innerHTML(v) { html = String(v); }, get innerHTML() { return html; }, querySelectorAll() { return []; }, querySelector() { return null; },
    addEventListener() {}, appendChild(c) { this.children.push(c); } };
}
const body = element('body');
const document = { createElement: element, body };
let responder = async () => ({});
const window = { BRIDGE_TOKEN: 'fixture-token' };
const context = vm.createContext({ window, document, Headers, AbortController, console, assert, body, calls, timers, setImmediate,
  setTimeout(fn, ms) { const t = { fn, ms }; timers.push(t); return t; }, clearTimeout() {},
  fetch: async (url, options = {}) => { calls.push({ url, options }); const r = await responder(url, options);
    return { ok: !(r && r.__status >= 400), status: r?.__status || 200, json: async () => (r && r.__status ? r.body : r) }; } });
context.setResponder = fn => { responder = fn; };
(async () => {
  vm.runInContext(source, context);
  await vm.runInContext(TEST, context);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''


@unittest.skipUnless(NODE, "Node.js is required for front-end regression tests")
class PluginNavTests(unittest.TestCase):
    def run_index(self, code):
        code = "(async () => {" + code + "})()"
        setup = ("context.PLUGINS_FIXTURE = " + json.dumps(PLUGINS, ensure_ascii=False) + ";\n")
        runner = "const PAGE = " + json.dumps(str(ROOT / "app/index.html")) + "; const TEST = " + json.dumps(code) + ";\n" + HARNESS
        runner = runner.replace("let responder = async () => ({ sessions: [], all_counts: {}, capabilities: {}, scan_status: { scanning: false, generation: 1 } });",
                                "let responder = async url => url === '/api/plugins' ? { plugins: context.PLUGINS_FIXTURE } : url === '/api/update/status' ? { available: true, repos: [] } : { sessions: [], all_counts: {}, capabilities: {}, scan_status: { scanning: false, generation: 1 } };")
        runner = runner.replace("(async () => {\n  vm.runInContext(script, context);", "(async () => {\n  " + setup + "  vm.runInContext(script, context);")
        runner = runner.replace("confirm: () => false", "confirm: () => false, setImmediate, calls")
        runner = runner.replace("json: async () => data };", "json: async () => data, text: async () => '' };")
        runner = runner.replace("hidden: false, body: element()", "hidden: false, body: element(), head: element()")
        result = subprocess.run([NODE, "-"], input=runner, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_plugins_become_sidebar_items_and_open_their_page(self):
        self.run_index(r'''
          const nav = document.querySelector('#plugin-nav').children, pages = document.querySelector('#plugin-pages').children;
          assert.deepEqual(Array.from(nav, n => n.className), ['nav-sec', 'nav-item', 'nav-sec', 'nav-item']);   // 名字不对的那个不出现
          assert.equal(nav[0].textContent, '示例区'); assert.equal(nav[2].textContent, '插件');
          const item = nav[1];
          assert.equal(item.dataset.g, 'demo-x');
          assert.match(item.innerHTML, /&lt;b&gt;示例&lt;\/b&gt;/); assert.doesNotMatch(item.innerHTML, /<b>/);
          assert.equal(item.title, '硬盘没接：<b>示例</b>先关着，其他功能照常');
          assert.equal(nav[3].title, '坏的没有装好：创建时出错');
          assert.deepEqual(Array.from(pages, p => p.dataset.plugin), ['demo-x', 'broken-x']);
          assert.equal(PLUGINS.size, 2);
          const shown = [];
          window.DemoPlugin = { show(host) { shown.push(host); }, hide() {}, refresh() {} };
          selectNav(item);
          await new Promise(setImmediate);
          assert.equal(state.group, 'demo-x');
          assert.match(document.querySelector('#crumb').innerHTML, /示例区/); assert.match(document.querySelector('#crumb').innerHTML, /&lt;b&gt;示例/);
          assert.equal(shown.length, 1);
          selectNav(nav[3]);
          await new Promise(setImmediate);
          assert.match(document.querySelector('.plugin-page[data-plugin="broken-x"]').innerHTML, /没有装好：创建时出错/);
          assert.equal(document.querySelector('#update-button').textContent, '有新版本');
        ''')

    def test_plugin_files_are_read_with_the_token_header(self):
        self.run_index(r'''
          const p = PLUGINS.get('demo-x');
          await assert.rejects(loadPluginAssets(p), /脚本没有正确加载/);     // 假环境里脚本不会执行
          const asked = Array.from(calls.filter(c => c.url.startsWith('/plugins/')), c => c.url);
          assert.deepEqual(asked, ['/plugins/demo-x/demo.css', '/plugins/demo-x/demo.js']);
          for (const c of calls.filter(c => c.url.startsWith('/plugins/'))) assert.equal(new Headers(c.options.headers).get('X-Bridge-Token'), 'fixture-token');
        ''')


@unittest.skipUnless(NODE, "Node.js is required for front-end regression tests")
class UpdateDialogTests(unittest.TestCase):
    def run_page(self, code):
        code = "(async () => {" + code + "})()"
        runner = "const PAGE = " + json.dumps(str(ROOT / "app/update.js")) + "; const TEST = " + json.dumps(code) + ";\n" + UPDATE_HARNESS
        result = subprocess.run([NODE, "-"], input=runner, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_syntax(self):
        result = subprocess.run([NODE, "--check", str(ROOT / "app/update.js")], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_helpers(self):
        self.run_page(r'''
          const T = window.LingqiaoUpdate._t;
          const ok = { key: 'core', name: '灵桥', git: true, behind: 2, reason: '', remote_head: 'abc', version: '3.5.0', remote_version: '3.6.0', changes: ['<i>新功能</i>'] };
          const blocked = { key: 'plugin:x', name: '插件', git: true, behind: 1, reason: '这台电脑上改过 1 个程序文件', remote_head: 'def' };
          const notGit = { key: 'plugin:y', name: '<b>Y</b>', git: false, reason: '这个文件夹不是从 GitHub 克隆的，没法自动更新' };
          assert.deepEqual(JSON.parse(JSON.stringify(T.expectOf([ok, blocked, notGit]))), { core: 'abc' });
          assert.equal(T.repoState(ok).text, '有新版本 3.6.0（2 处改动）');
          assert.equal(T.repoState(blocked).cls, 'warn'); assert.equal(T.repoState({ git: true, behind: 0 }).text, '已经是最新的');
          const html = T.repoHtml(ok) + T.repoHtml(notGit);
          assert.match(html, /&lt;i&gt;新功能/); assert.match(html, /&lt;b&gt;Y/); assert.doesNotMatch(html, /<i>|<b>Y/);
          assert.equal(T.canApply({ busy: false, job: null, status: { repos: [ok] } }), true);
          assert.equal(T.canApply({ busy: false, job: { status: 'running' }, status: { repos: [ok] } }), false);
          assert.equal(T.canApply({ busy: false, job: null, status: { repos: [blocked] } }), false);
          assert.match(T.jobHtml({ status: 'failed', error: '新版本的测试没通过', rolled_back: true, tail: ['<x>'] }), /已经退回原来的版本[\s\S]*&lt;x&gt;/);
        ''')

    def test_check_update_poll_and_restart(self):
        self.run_page(r'''
          const repo = { key: 'core', name: '灵桥', git: true, behind: 1, reason: '', remote_head: 'abc', version: '3.5.0', remote_version: '3.5.1', changes: ['修好一个问题'] };
          let job = { id: 'j1', status: 'running', step: '正在跑测试…' };
          setResponder(async (url, options) => {
            if (url === '/api/update/check') return { git: true, repos: [repo], available: true, auto_check: true, checked_at: 1, job: null };
            if (url === '/api/update/apply') return { job };
            if (url === '/api/update/job') return { job };
            if (url === '/api/update/restart') return { ok: true };
            return { __status: 404, body: { error: '不存在' } };
          });
          const seen = [];
          window.LingqiaoUpdate.open({ onStatus: s => seen.push(s.available) });
          await new Promise(setImmediate);
          assert.equal(calls[0].url, '/api/update/check'); assert.equal(calls[0].options.method, 'POST');
          assert.equal(calls[0].options.headers.get('X-Bridge-Token'), 'fixture-token');
          assert.deepEqual(seen, [true]);
          const mask = body.children[0];
          assert.match(mask.innerHTML, /有新版本 3\.5\.1/); assert.match(mask.innerHTML, /修好一个问题/);
          const T = window.LingqiaoUpdate._t;
          T.act('apply');
          assert.match(mask.innerHTML, /确定现在更新/);
          T.act('confirm');
          await new Promise(setImmediate);
          const apply = calls.find(c => c.url === '/api/update/apply');
          assert.deepEqual(JSON.parse(apply.options.body), { confirm: true, expect: { core: 'abc' } });
          assert.match(mask.innerHTML, /正在跑测试/); assert.match(mask.innerHTML, /data-act="close" disabled/);
          job = { id: 'j1', status: 'done', step: '更新好了，重启灵桥后生效', restart: true };
          await timers.at(-1).fn();
          assert.match(mask.innerHTML, /现在重启/);
          T.act('restart');
          await new Promise(setImmediate);
          assert.deepEqual(JSON.parse(calls.at(-1).options.body), { confirm: true });
          assert.equal(calls.at(-1).url, '/api/update/restart');
        ''')

    def test_failed_check_is_shown(self):
        self.run_page(r'''
          setResponder(async () => ({ __status: 400, body: { error: '连不上 GitHub' } }));
          window.LingqiaoUpdate.open();
          await new Promise(setImmediate);
          assert.match(body.children[0].innerHTML, /检查失败：连不上 GitHub/);
        ''')


if __name__ == "__main__":
    unittest.main()
