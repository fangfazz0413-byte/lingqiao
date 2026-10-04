"""Adversarial tests for cleanup review, explicit commit, and appearance inputs.

All HTTP responses are fixtures; these tests do not touch any real conversation.
"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from test_frontend import HARNESS

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
CLEANUP_HARNESS = HARNESS.replace(
    "classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },",
    "classList: { values:new Set(), add(v) {this.values.add(v);}, remove(v) {this.values.delete(v);}, toggle(v,on) {if(on){this.add(v);}else{this.remove(v);}}, contains(v) { return this.values.has(v); } },"
).replace(
    "addEventListener() {}, hasFocus: () => true, hidden: false, body: element()",
    "addEventListener() {}, hasFocus: () => true, hidden: false, body: element(), documentElement:element()"
).replace(
    "setInterval() {}, clearTimeout() {},",
    "setInterval() { return {}; }, clearInterval() {}, clearTimeout() {},"
).replace(
    "vm.runInContext(script, context);",
    "lookup('#cleanup-days').value='30'; lookup('#cleanup-scope').value='all'; vm.runInContext(script, context);"
)


@unittest.skipUnless(NODE, "Node.js is required for cleanup UI regressions")
class CleanupFrontendTests(unittest.TestCase):
    def run_js(self, code, setup=""):
        runner = "const PAGE = " + json.dumps(str(ROOT / "app/index.html")) + "; const TEST = " + json.dumps(code) + ";\n" + CLEANUP_HARNESS
        if setup:
            runner = runner.replace("await vm.runInContext(TEST, context);", setup + "\n  await vm.runInContext(TEST, context);")
        result = subprocess.run([NODE, "-"], input=runner, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cleanup_controls_present_and_backend_wording(self):
        page = (ROOT / "app/index.html").read_text()
        for key in ["cleanup-button", "cleanup-days", "cleanup-scope", "cleanup-preview", "cleanup-confirm"]:
            self.assertIn('id="' + key + '"', page)
        self.assertIn('value="15"', page)
        self.assertIn('value="30"', page)
        self.assertNotIn("数据库会话记录标记为软删除", page)
        self.assertNotIn("SFNSRounded", page)
        self.assertIn('font-family:"Yuanti SC"', page)
        self.assertIn('font-family:"Kaiti SC"', page)

    def test_brand_match_and_unknown_fallback(self):
        self.run_js(r'''
          assert.equal(brandId('CC Switch Claude Code'),'cc-switch');
          assert.equal(brandId('cc-switch'),'cc-switch');
          assert.match(brandIcon('CC Switch'), /\/assets\/icons\/cc-switch\.png/);
          assert.match(brandIcon('Unknown Provider'), /brand-placeholder/);
          assert.doesNotMatch(brandIcon('Unknown Provider'), /<img/);
          assert.equal(brandIcon('all'),'');
        ''')

    def test_appearance_invalid_storage_and_root_classes(self):
        self.run_js(r'''
          for(const value of [null, 2, 'mint', ['ocean'], {theme:'<script>',font:'bogus'}]) {
            applyAppearance(value); assert.equal(document.documentElement.dataset.theme,'');
            assert.equal(document.documentElement.dataset.font,'round');
          }
          applyAppearance({theme:'cream',font:'hand'});
          assert.equal(document.documentElement.dataset.theme,'cream');
          assert.equal(document.documentElement.dataset.font,'hand');
          assert.equal(document.documentElement.classList.contains('selected'),false);
        ''')
        self.assertIn('querySelectorAll("#theme-options [data-theme]")', (ROOT / "app/index.html").read_text())

    def test_preview_never_commits_and_empty_plan_disabled(self):
        self.run_js(r'''
          (async () => {
            await previewCleanup();
            assert.equal(cleanupReady(),false); assert.equal($('#cleanup-confirm').disabled,true);
            await commitCleanup();
            assertCleanupMethods(['/api/cleanup/preview']);
            assert.match($('#cleanup-status').textContent,/无需清理/);
          })()
        ''', setup=r'''
          const cleanupCalls=[];
          responder=async (url,opts)=>{if(url.startsWith('/api/cleanup/')) cleanupCalls.push(url); return {plan_id:'empty',days:30,tool:'all',status:'ready',count:0,total_size:0,items:[],skipped:[],expires_at:Date.now()/1000+300};};
          context.assertCleanupMethods=expected=>assert.equal(JSON.stringify(cleanupCalls),JSON.stringify(expected));
        ''')

    def test_preview_ready_escape_titles_and_scope(self):
        self.run_js(r'''
          (async () => {
            state.group='codex'; openCleanup(); assert.equal($('#cleanup-scope').value,'codex');
            $('#cleanup-days').value='15'; await previewCleanup(); setCleanupItem('fixture-1', true);
            assert.equal(cleanupReady(),true); assert.equal($('#cleanup-confirm').disabled,false);
            assert.match($('#cleanup-result').innerHTML,/&lt;img src=x onerror=alert\(1\)&gt;/);
            assert.match($('#cleanup-result').innerHTML,/最后活动/);
            assert.match($('#cleanup-result').innerHTML,/正在运行/);
            assert.match($('#cleanup-result').innerHTML,/5 分钟/);
            checkPreviewSelection();
          })()
        ''', setup=r'''
          let body;
          responder=async(url,opts)=>{body=JSON.parse(opts.body);return {plan_id:'ready',days:15,tool:'codex',status:'ready',count:1,total_size:1024,expires_at:Date.now()/1000+300,items:[{tool:'codex',src:'fixture',item_id:'fixture-1',title:'<img src=x onerror=alert(1)>',last_active_at:1000,size:1024}],skipped:[{tool:'codex',title:'active fixture',reason:'running'}]};};
          context.checkPreviewSelection=()=>assert.deepEqual(body,{days:15,tool:'codex'});
        ''')

    def test_subagent_target_body_parent_titles_and_auto_switch(self):
        self.run_js(r'''
          (async () => {
            openCleanup(); await flush();
            assert.equal($('#cleanup-auto').hidden,true);
            $('#cleanup-target').value='subagent'; renderCleanupAuto();
            assert.equal($('#cleanup-auto').hidden,false);
            assert.match($('#cleanup-auto').innerHTML,/默认关着/); assert.match($('#cleanup-auto').innerHTML,/子代理对话 = ZCode 的子会话/);
            assert.doesNotMatch($('#cleanup-auto').innerHTML,/id="cleanup-auto-toggle" checked/);
            await previewCleanup();
            assert.match($('#cleanup-result').innerHTML,/属于：主对话&lt;b&gt;/);
            assert.match($('#cleanup-result').innerHTML,/子代理对话（主对话保留）/);
            checkBody();
            await toggleCleanupAuto(true);
            checkAuto(); assert.match($('#cleanup-auto').innerHTML,/已打开/); assert.match($('#cleanup-auto').innerHTML,/id="cleanup-auto-toggle" checked/);
            $('#cleanup-target').value='main'; renderCleanupAuto(); assert.equal($('#cleanup-auto').hidden,true);
          })()
        ''', setup=r'''
          let previewBody, autoBody, auto={enabled:false,days:30,interval_hours:20,last_run_at:null,last_result:null};
          responder=async(url,opts)=>{
            if(url==='/api/cleanup/subagent-auto'){ if(opts&&opts.method==='POST'){autoBody=JSON.parse(opts.body); auto={...auto,enabled:autoBody.enabled,days:autoBody.days};} return auto; }
            if(url==='/api/cleanup/preview'){ previewBody=JSON.parse(opts.body); return {plan_id:'sub',days:30,tool:'all',target:'subagent',status:'ready',count:1,total_size:10,expires_at:Date.now()/1000+300,
              items:[{tool:'zcode',src:'zcode:x',item_id:'s-1',title:'子任务',parent_title:'主对话<b>',last_active_at:1000,size:10}],skipped:[]}; }
            return {sessions:[],all_counts:{},capabilities:{},scan_status:{scanning:false,generation:1}};
          };
          context.flush=()=>new Promise(setImmediate);
          context.checkBody=()=>assert.deepEqual(previewBody,{days:30,tool:'all',target:'subagent'});
          context.checkAuto=()=>assert.deepEqual(autoBody,{enabled:true,days:30});
        ''')

    def test_plan_for_other_target_is_rejected(self):
        self.run_js(r'''
          (async () => {
            $('#cleanup-target').value='subagent'; await previewCleanup();
            assert.equal(cleanupReady(),false); assert.match(cleanupState.error,/不一致/);
          })()
        ''', setup=r'''
          responder=async()=>({plan_id:'p',days:30,tool:'all',status:'ready',count:1,total_size:1,expires_at:Date.now()/1000+300,items:[{tool:'zcode',item_id:'x-1',title:'x',size:1,last_active_at:1}],skipped:[]});
        ''')

    def test_expired_and_incomplete_plans_cannot_commit(self):
        self.run_js(r'''
          (async () => {
            await previewCleanup(); assert.equal(cleanupReady(),false); await commitCleanup();
            assert.match($('#cleanup-status').textContent,/过期/);
            await previewCleanup(); assert.equal(cleanupReady(),false); await commitCleanup();
            assert.match($('#cleanup-status').textContent,/不完整/);
            assertNoCommit();
          })()
        ''', setup=r'''
          let attempt=0;
          responder=async(url)=>{assert.notEqual(url,'/api/cleanup/commit');return {plan_id:'p'+(++attempt),status:'ready',days:30,tool:'all',count:1,items:attempt===1?[{item_id:'expired-1',title:'expired',size:1,last_active_at:1}]:[],total_size:1,expires_at:attempt===1?1:Date.now()/1000+300};};
          context.assertNoCommit=()=>assert.equal(calls.filter(c=>c.url==='/api/cleanup/commit').length,0);
        ''')

    def test_stale_cancelled_preview_cannot_enable_confirmation(self):
        self.run_js(r'''
          (async () => {
            openCleanup(); const old=previewCleanup(); closeCleanup();
            assert.equal(cleanupState.previewing,false); assert.equal(cleanupState.plan,null);
            releasePreview(); await old;
            assert.equal(cleanupState.plan,null); assert.equal(cleanupReady(),false);
          })()
        ''', setup=r'''
          let release;
          responder=()=>new Promise(resolve=>release=resolve);
          context.releasePreview=()=>release({plan_id:'late',status:'ready',days:30,tool:'all',count:1,total_size:1,expires_at:Date.now()/1000+300,items:[{item_id:'late-1',title:'late',last_active_at:1,size:1}]});
        ''')

    def test_double_commit_and_hide_while_running(self):
        self.run_js(r'''
          (async () => {
            openCleanup(); await previewCleanup(); setCleanupItem('fixture-1', true); const first=commitCleanup(); const second=commitCleanup();
            assert.equal(cleanupState.running,true); assert.equal($('#cleanup-days').disabled,true);
            closeCleanup(); assert.equal(cleanupState.visible,false); assert.equal(cleanupState.running,true);
            releaseCommit(); await first; await second; assertSingleCommit();
            assert.equal(cleanupState.plan.status,'running');
            await pollCleanup(); assert.equal(cleanupState.plan.status,'done'); assert.equal(cleanupState.running,false);
            openCleanup(); assert.match($('#cleanup-result').innerHTML,/清理完成/);
            assert.match($('#cleanup-result').innerHTML,/已移入回收站 1 条/);
          })()
        ''', setup=r'''
          let release;
          const ready={plan_id:'commit',status:'ready',days:30,tool:'all',count:1,total_size:1,expires_at:Date.now()/1000+300,items:[{tool:'claude',item_id:'fixture-1',title:'fixture',last_active_at:1,size:1}],skipped:[]};
          responder=async(url,opts)=>{
            if(url==='/api/cleanup/preview') return ready;
            if(url==='/api/cleanup/commit') {assert.deepEqual(JSON.parse(opts.body),{plan_id:'commit',confirm:true,selected_ids:['fixture-1']});return await new Promise(resolve=>release=resolve);}
            if(url.startsWith('/api/cleanup/plan?')) return {...ready,status:'done',selected_ids:['fixture-1'],selected_count:1,processed:1,deleted:1,errors:[]};
            return {sessions:[],all_counts:{},scan_status:{scanning:false},capabilities:{}};
          };
          context.releaseCommit=()=>release({plan_id:'commit',status:'running',count:1,selected_ids:['fixture-1'],selected_count:1,processed:0,deleted:0,skipped:[],errors:[]});
          context.assertSingleCommit=()=>assert.equal(calls.filter(c=>c.url==='/api/cleanup/commit').length,1);
        ''')

    def test_preparing_poll_and_error_is_retryable(self):
        self.run_js(r'''
          (async () => {
            await previewCleanup(); assert.equal(cleanupState.previewing,true); assert.equal(cleanupReady(),false);
            await pollCleanup(); setCleanupItem('fixture-1', true); assert.equal(cleanupState.previewing,false); assert.equal(cleanupReady(),true);
            invalidateCleanup(); assert.equal(cleanupReady(),false);
            await previewCleanup(); assert.match(cleanupState.error,/fixture failure/); assert.equal(cleanupState.previewing,false);
            await previewCleanup(); setCleanupItem('fixture-1', true); assert.equal(cleanupState.error,''); assert.equal(cleanupReady(),true);
          })()
        ''', setup=r'''
          let attempt=0;
          const ready={plan_id:'prepare',status:'ready',days:30,tool:'all',count:1,total_size:1,expires_at:Date.now()/1000+300,items:[{item_id:'fixture-1',title:'fixture',last_active_at:1,size:1}],skipped:[]};
          responder=async(url)=>url.startsWith('/api/cleanup/plan?')?ready:++attempt===1?{plan_id:'prepare',status:'preparing'}:attempt===2?{error:'fixture failure'}:ready;
        ''')

    def test_selective_subset_payload_totals_and_plan_reopen(self):
        self.run_js(r"""
          (async () => {
            await previewCleanup();
            assert.equal(cleanupState.selectedIds.size,0);
            assert.equal(cleanupReady(),false);
            setCleanupItem('a', true); setCleanupItem('c', true);
            assert.deepEqual(cleanupSelectedList(),['a','c']);
            assert.deepEqual(cleanupTotals(),{count:2,size:2048});
            closeCleanup(); openCleanup();
            assert.deepEqual(cleanupSelectedList(),['a','c']);
            selectCleanupNone(); assert.equal(cleanupState.selectedIds.size,0);
            selectCleanupAll(); assert.deepEqual(cleanupSelectedList(),['a','b','c']);
            invalidateCleanup(); assert.equal(cleanupState.selectedIds.size,0);
            await previewCleanup(); assert.equal(cleanupState.selectedIds.size,0);
          })()
        """, setup=r"""
          let n=0;
          responder=async(url)=>{
            if(url==='/api/cleanup/preview') { const plan='p'+(++n); return {plan_id:plan,status:'ready',days:30,tool:'all',count:3,total_size:4096,expires_at:Date.now()/1000+300,items:[{item_id:'a',title:'A',size:1024,last_active_at:1},{item_id:'b',title:'B',size:2048,last_active_at:1},{item_id:'c',title:'C',size:1024,last_active_at:1}],skipped:[]}; }
            return {sessions:[],all_counts:{},scan_status:{scanning:false},capabilities:{}};
          };
        """)

    def test_selective_commit_sends_unique_selected_ids_and_rejects_mismatch(self):
        self.run_js(r"""
          (async () => {
            await previewCleanup(); setCleanupItem('b',true); setCleanupItem('a',true);
            const request = cleanupState.request; cleanupState.committingIds = new Set(['a','b']);
            assert.throws(() => acceptCleanupPlan({plan_id:'select',status:'running',selected_ids:['c']},request), /选择项/);
            await commitCleanup();
            assert.equal(JSON.stringify(sentIds()),JSON.stringify(['b','a']));
          })()
        """, setup=r"""
          const ready={plan_id:'select',status:'ready',days:30,tool:'all',count:2,total_size:3,expires_at:Date.now()/1000+300,items:[{item_id:'a',title:'A',size:1,last_active_at:1},{item_id:'b',title:'B',size:2,last_active_at:1}],skipped:[]};
          let sent;
          responder=async(url,opts)=>{ if(url==='/api/cleanup/preview') return ready; if(url==='/api/cleanup/commit'){sent=JSON.parse(opts.body); return {plan_id:'select',status:'done',selected_ids:['b','a'],selected_count:2,deleted:2,processed:2,skipped:[],errors:[]};} return ready; }; context.sentIds=()=>sent.selected_ids;
        """)

    def test_countdown_and_selection_preserve_candidate_nodes(self):
        self.run_js(r"""
          (async () => {
            openCleanup(); await previewCleanup();
            const before = $('#cleanup-result').innerHTML;
            const focused = {kind:'checkbox'}; document.activeElement=focused;
            const list = $('#cleanup-result').querySelector('.cleanup-items'); list.scrollTop = 140;
            setCleanupItem('fixture-1',true); updateCleanupExpiry();
            assert.equal($('#cleanup-result').innerHTML,before);
            assert.equal(document.activeElement,focused); assert.equal(list.scrollTop,140);
            assert.equal($('#cleanup-selected-total').textContent,'已选 1 条 · 1 B');
            assert.equal($('#cleanup-select-all').checked,true);
            assert.equal($('#cleanup-confirm').disabled,false);
          })()
        """, setup=r"""
          responder=async()=>({plan_id:'stable',status:'ready',days:30,tool:'all',count:1,total_size:1,selected_ids:[],expires_at:Date.now()/1000+300,items:[{item_id:'fixture-1',tool:'codex',title:'Fixture',size:1,last_active_at:1}],skipped:[]});
        """)

    def test_labels_escape_attributes_and_keep_brand_icons(self):
        self.run_js(r"""
          const html=cleanupItems([{item_id:'a" autofocus onfocus=alert(1)',tool:'codex',title:'" onfocus=alert(2) <img src=x>',size:1,last_active_at:1}]);
          assert.match(html,/a&quot; autofocus onfocus=alert\(1\)/);
          assert.match(html,/\/assets\/icons\/codex\.png/);
          assert.doesNotMatch(html,/aria-label="选择/);
          assert.doesNotMatch(html,/<img src=x>/);
        """)

    def test_unaccepted_commit_releases_selection_after_ready_plan(self):
        self.run_js(r"""
          (async () => {
            await previewCleanup(); setCleanupItem('fixture-1',true); await commitCleanup();
            assert.equal(cleanupState.running,false); assert.equal(cleanupState.committingIds,null);
            assert.equal(cleanupState.plan.status,'ready'); assert.equal(cleanupReady(),true);
            assert.equal($('#cleanup-days').disabled,false);
            assert.equal(JSON.stringify(cleanupSelectedList()),JSON.stringify(['fixture-1']));
            selectCleanupNone(); assert.equal(cleanupReady(),false);
          })()
        """, setup=r"""
          const ready={plan_id:'retry',status:'ready',days:30,tool:'all',count:1,total_size:1,selected_ids:[],selected_count:0,expires_at:Date.now()/1000+300,items:[{item_id:'fixture-1',title:'Fixture',size:1,last_active_at:1}],skipped:[]};
          responder=async(url)=>url==='/api/cleanup/commit'?{error:'confirmation not received'}:ready;
        """)

    def test_uncertain_commit_stays_frozen_if_plan_query_fails(self):
        self.run_js(r"""
          (async () => {
            await previewCleanup(); setCleanupItem('fixture-1',true); await commitCleanup();
            assert.equal(cleanupState.running,true); assert.notEqual(cleanupState.committingIds,null);
            assert.equal(setCleanupItem('fixture-1',false),false); assert.equal(selectCleanupNone(),false);
            assert.equal(invalidateCleanup(),false); assert.equal($('#cleanup-days').disabled,true);
            assert.equal(cleanupReady(),false);
          })()
        """, setup=r"""
          const ready={plan_id:'uncertain',status:'ready',days:30,tool:'all',count:1,total_size:1,selected_ids:[],expires_at:Date.now()/1000+300,items:[{item_id:'fixture-1',title:'Fixture',size:1,last_active_at:1}],skipped:[]};
          responder=async(url)=>url==='/api/cleanup/preview'?ready:{error:'fixture status unavailable'};
        """)

    def test_commit_timeout_queries_same_plan(self):
        self.run_js(r'''
          (async () => {
            await previewCleanup(); setCleanupItem('fixture-1', true); await commitCleanup();
            assert.equal(cleanupState.plan.status,'running'); assert.equal(cleanupState.running,true);
            assertSamePlan();
          })()
        ''', setup=r'''
          const ready={plan_id:'timeout',status:'ready',days:30,tool:'all',count:1,total_size:1,expires_at:Date.now()/1000+300,items:[{item_id:'fixture-1',title:'fixture',last_active_at:1,size:1}],skipped:[]};
          responder=async(url)=>url==='/api/cleanup/preview'?ready:url==='/api/cleanup/commit'?{error:'network timeout'}:{plan_id:'timeout',status:'running',count:1,selected_ids:['fixture-1'],selected_count:1,processed:0,deleted:0,skipped:[],errors:[]};
          context.assertSamePlan=()=>{assert.equal(calls.filter(c=>c.url==='/api/cleanup/commit').length,1); assert.equal(calls.filter(c=>c.url==='/api/cleanup/plan?plan_id=timeout').length,1);};
        ''')


if __name__ == "__main__":
    unittest.main()
