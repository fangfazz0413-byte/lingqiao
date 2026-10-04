"""DOM-free regressions for the bridge pages; requests use synthetic fixtures only."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

# A deliberately small DOM stub. This verifies logic and contracts, not visual layout.
HARNESS = r'''
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const html = fs.readFileSync(PAGE, 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
const elements = new Map(), calls = [], timers = [];
function element() {
  const children = [], selectors = new Map(); let text = '', markup = null;
  return {
    children, dataset: {}, style: {}, disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    set textContent(value) { text = String(value ?? ''); markup = null; }, get textContent() { return text; },
    set innerHTML(value) { markup = value; children.length = 0; },
    get innerHTML() { return markup ?? text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
    querySelector(selector) { if (!selectors.has(selector)) selectors.set(selector, element()); return selectors.get(selector); },
    querySelectorAll() { return []; }, addEventListener() {}, appendChild(child) { children.push(child); }, setAttribute() {}
  };
}
function lookup(selector) { if (!elements.has(selector)) elements.set(selector, element()); return elements.get(selector); }
const document = {
  querySelector: lookup, getElementById: id => lookup('#' + id), createElement: element,
  querySelectorAll: selector => selector === '#list .row' ? lookup('#list').children.filter(row => row.dataset.key) : [],
  addEventListener() {}, hasFocus: () => true, hidden: false, body: element()
};
let responder = async () => ({ sessions: [], all_counts: {}, capabilities: {}, scan_status: { scanning: false, generation: 1 } });
const context = vm.createContext({
  document, window: { BRIDGE_TOKEN: 'fixture-token', addEventListener() {} },
  Headers, AbortController, URLSearchParams, Date, Map, Set, console, assert,
  setInterval() {}, clearTimeout() {},
  setTimeout(callback, ms) { const timer = { callback, ms }; timers.push(timer); if (ms === 500) queueMicrotask(callback); return timer; },
  fetch: async (url, options) => { calls.push({ url, options }); const data = await responder(url, options); return { ok: true, status: 200, json: async () => data }; },
  confirm: () => false
});
(async () => {
  vm.runInContext(script, context);
  await new Promise(setImmediate);
  await vm.runInContext(TEST, context);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''


@unittest.skipUnless(NODE, "Node.js is required for front-end regression tests")
class FrontendTests(unittest.TestCase):
    def run_js(self, code, page="index.html", setup=""):
        path = ROOT / "app" / page
        runner = "const PAGE = " + json.dumps(str(path)) + "; const TEST = " + json.dumps(code) + ";\n" + HARNESS
        if setup:
            runner = runner.replace("await vm.runInContext(TEST, context);", setup + "\n  await vm.runInContext(TEST, context);")
        result = subprocess.run([NODE, "-"], input=runner, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_javascript_syntax(self):
        for page in ["index.html", "mtoken-panel.html"]:
            source = (ROOT / "app" / page).read_text()
            script = source.split("<script>", 1)[1].split("</script>", 1)[0]
            result = subprocess.run([NODE, "--check", "-"], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_month_dates_defaults_cross_month_and_model_case(self):
        self.run_js(r'''
          assert.equal(completeMonthDays('2026-10').length, 31);
          assert.equal(completeMonthDays('2024-02').length, 29);
          assert.equal(completeMonthDays('2026-02').length, 28);
          assert.equal(completeMonthDays('2026-13').length, 0);
          const local = {month:'2026-10', today:{day:'2026-10-02'},
            days:['2026-09-28','2026-09-29','2026-09-30','2026-10-01','2026-10-02'],
            daily:[[1],[10],[0],[2],[3]],
            months:['2026-10','2026-09'], sources:[], heat:[],
            day_models:{'2026-10-02':[
              {m:'Model-A',s:'source',t:3,c:1,u:1,o:1,k:1},
              {m:'model-a',s:'source',t:2,c:1,u:1,o:1,k:0} ]}, hours:{}, stat_today:{tokens:3}, stat_7d:{} };
          assert.equal(defaultDayInMonth(local, '2026-09'), '2026-09-29');
          mtState.data = normalizeUsage({at:1,local,quota:[]}); mtState.month = '2026-10';
          assert.equal((renderDaily().match(/class="bar/g) || []).length, 31);
          mtPickDay('2026-09-29'); assert.equal(mtState.month, '2026-09');
          assert.equal(mtState.day, '2026-09-29');
          assert.equal(modelsOn(['2026-10-02']).length, 2);
          mtPickMonth('2026-10'); assert.equal(mtState.day, '2026-10-02');
        ''')

    def test_capabilities_summary_snapshot_and_shared_curve_scale(self):
        self.run_js(r'''
          assert.equal(capabilityFor('claude','workbuddy',{claude:{workbuddy:{supported:true}}}).supported,true);
          assert.equal(capabilityFor('claude','workbuddy',{}).supported,false);
          state.data=[]; state.counts={all:10,claude:1,codex:2,zcode:3,workbuddy:4}; updateSummary();
          assert.match($('#k-total-sub').textContent, /WorkBuddy 4/);
          assert.notEqual(sourceTimestampText(1), sourceTimestampText(999999));
          const html = polyPts([0,2], 100,100,'red',false,10);
          assert.match(html, /100\.0,78\.6/);
          assert.throws(() => normalizeUsage({local:{}}), /快照格式/);
        ''')

    def test_api_tokens_on_get_post_and_real_refresh(self):
        self.run_js(r'''
          (async () => {
            await apiJson('/api/usage');
            await apiJson('/api/usage/refresh', {method:'POST',body:{}});
          })()
        ''', setup=r'''
          responder = async () => ({ok:true});
          context.assertTokens = () => { for (const call of calls) assert.equal(call.options.headers.get('X-Bridge-Token'),'fixture-token'); };
        ''')
        # Inspect request recording from the host, including the automatic bootstrap GET.
        self.run_js("(async () => { await apiJson('/api/trash'); await apiJson('/api/restore',{method:'POST',body:{trash_id:'fixture'}}); assertTokens(); })()",
                    setup="context.assertTokens = () => { for(const call of calls) assert.equal(call.options.headers.get('X-Bridge-Token'),'fixture-token'); }; responder = async () => ({ok:true});")

    def test_loading_detail_then_two_pages(self):
        self.run_js(r'''
          (async () => {
            const session={tool:'claude',src:'/fixture/session.jsonl'};
            await fetchDetail(session,0);
            const entry=detailEntry(session);
            assert.equal(entry.turns.length,100); assert.equal(entry.nextOffset,100); assert.equal(entry.total,101);
            await fetchDetail(session,100);
            assert.equal(entry.turns.length,101); assert.equal(entry.nextOffset,null); assert.equal(entry.busy,false);
          })()
        ''', setup=r'''
          let request=0;
          responder = async (url) => {
            if (!url.startsWith('/api/session?')) return {};
            if (++request === 1) return {loading:true,turns:[]};
            const offset=Number(new URL(url,'http://localhost').searchParams.get('offset'));
            return {loading:false,total:101,next_offset:offset===0?100:null,
              turns:Array.from({length:offset===0?100:1},(_,i)=>({role:i%2?'assistant':'user',text:'fixture '+(offset+i)}))};
          };
        ''')

    def test_failed_detail_is_retryable(self):
        self.run_js(r'''
          (async () => {
            const session={tool:'codex',src:'/fixture/rollout.jsonl'};
            await fetchDetail(session,0); const entry=detailEntry(session);
            assert.equal(entry.busy,false); assert.match(entry.error,/fixture error/); assert.equal(entry.nextOffset,0);
            await fetchDetail(session,0); assert.equal(entry.error,''); assert.equal(entry.loaded,true);
          })()
        ''', setup="let attempt=0; responder=async () => ++attempt===1 ? {error:'fixture error'} : {turns:[{role:'user',text:'ok'}],total:1,next_offset:null};")

    def test_stale_list_request_cannot_replace_new_filter(self):
        self.run_js(r'''
          (async () => {
            state.q='old'; const first=loadList(); state.q='new'; const second=loadList();
            release(1,{sessions:[],all_counts:{all:20},capabilities:{},scan_status:{scanning:false,generation:2}});
            await second;
            release(0,{sessions:[],all_counts:{all:10},capabilities:{},scan_status:{scanning:false,generation:1}});
            await first; assert.equal(state.counts.all,20); assert.equal(state.scanStatus.generation,2);
          })()
        ''', setup="const pending=[]; responder=() => new Promise(resolve => pending.push(resolve)); context.release=(index,data)=>pending[index](data);")

    def test_panel_quota_error_and_timestamp_without_mtime(self):
        self.run_js(r'''
          const data={at:1,_mtime:999999,local:{month_tokens:50,stat_today:{tokens:5,calls:2},avg7_curve:[0,2],today_curve:[0,10]},quota:[
            {name:'x',label:'<unsafe>',configured:false,windows:[]},
            {name:'y',label:'error',configured:true,error:'failed',windows:[]},
            {name:'z',label:'quota',configured:true,windows:[{pct:80}]}]};
          renderPanel(data);
          assert.match($('quota-panel').innerHTML,/&lt;unsafe&gt;/);
          assert.match($('quota-panel').innerHTML,/查询失败/);
          assert.match($('quota-panel').innerHTML,/未配置/);
          assert.equal($('ring-pct').textContent,'20%');
          assert.match($('foot').textContent,/1970/);
          assert.equal(quotaHeadline({windows:[{unlimited:true,pct:90},{pct:10}]}).pct,10);
        ''', page="mtoken-panel.html")


if __name__ == "__main__":
    unittest.main()
