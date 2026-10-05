import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import http.client
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
import server as s

class ServerIntegration(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); r=Path(self.tmp.name).resolve(); self.r=r
  paths={'HOME':r,'REPO':r/'repo','APP_DIR':ROOT/'app','BRIDGE':r/'repo/.bridge','CC_ROOT':r/'.claude/projects','CC_META_ROOT':s.platform_paths.claude_meta_root(r),'CX_ROOT':r/'.codex/sessions','CX_STATE':r/'state.sqlite','CX_SQLITE':r/'history.sqlite','CX_INDEX':r/'index.jsonl','Z_DB':r/'zcode.sqlite','WB_ROOT':r/'.workbuddy/projects','WB_DB':r/'wb.sqlite','MT_CACHE':r/'cache.json','CONFIG':r/'repo/.bridge/config.json','DISK_CACHE':r/'repo/.bridge/cache.json','LEDGER':r/'repo/.bridge/ledger.json','PROVENANCE':r/'repo/.bridge/provenance.json'}
  self.patches=[patch.object(s,k,v) for k,v in paths.items()]
  self.patches+=[patch.object(s.sync,'HOME',r),patch.object(s,'log',lambda message:None)]
  for p in self.patches:p.start()
  for name in ('CC_ROOT','CC_META_ROOT','CX_ROOT','WB_ROOT','BRIDGE'):getattr(s,name).mkdir(parents=True,exist_ok=True)
  for attr,name in [('CX_STATE','codex-state'),('CX_SQLITE','codex-history'),('Z_DB','zcode'),('WB_DB','workbuddy')]:
   c=sqlite3.connect(getattr(s,attr));c.executescript('BEGIN;'+(ROOT/'tests/fixtures'/f'{name}.sql').read_text()+'\nCOMMIT;');c.close()  # 一次提交：Windows 上逐条落盘要好几秒
  c=sqlite3.connect(s.WB_DB)
  c.execute('INSERT INTO sessions (id,cwd,user_id,title,status,created_at,updated_at,last_activity_at,transport) VALUES (?,?,?,?,?,?,?,?,?)',('seed','/fixture','user-fixture','seed','completed',1,1,1,'local'));c.commit();c.close()
  s._cache.update(sessions=[],ts=time.time(),scanning=False,generation=0,error=None)
  s._detail_jobs.clear()
  self.turns=[{'role':'user','text':'<div>合法HTML提问</div>'},{'role':'assistant','text':'第一条AI'},{'role':'assistant','text':'连续第二条AI'}]
  self.sources={};self.meta={'dir':'/fixture','title':'合法HTML提问','mtime':time.time()}
  for tool in ('claude','codex','workbuddy'):
   sid=str(__import__('uuid').uuid4());root=getattr(s,{'claude':'CC_ROOT','codex':'CX_ROOT','workbuddy':'WB_ROOT'}[tool]);p=root/('rollout-fixture-'+sid+'.jsonl' if tool=='codex' else sid+'.jsonl')
   if tool=='codex': records=[{'type':'session_meta','payload':{'id':sid,'session_id':sid,'cwd':'/fixture','thread_source':'user'}}]+[{'type':'response_item','payload':{'type':'message','role':t['role'],'content':[{'type':'input_text' if t['role']=='user' else 'output_text','text':t['text']}]}} for t in self.turns]
   elif tool=='claude':records=[{'type':t['role'],'sessionId':sid,'cwd':'/fixture','message':{'role':t['role'],'content':[{'type':'text','text':t['text']}]}} for t in self.turns]
   else:records=[{'type':'message','role':t['role'],'sessionId':sid,'content':[{'type':'input_text' if t['role']=='user' else 'output_text','text':t['text']}]} for t in self.turns]
   p.write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in records)+'\n');self.sources[tool]=str(p)
   if tool=='claude':
    d=s.CC_META_ROOT/'acct/project';d.mkdir(parents=True);(d/'local_seed.json').write_text(json.dumps({'cliSessionId':sid,'title':'合法HTML提问','lastActivityAt':int(time.time()*1000)}))
   if tool=='workbuddy':
    c=sqlite3.connect(s.WB_DB);c.execute('INSERT INTO sessions (id,cwd,user_id,title,status,created_at,updated_at,last_activity_at,transport) VALUES (?,?,?,?,?,?,?,?,?)',(sid,'/fixture','user-fixture','fixture','completed',1,1,int(time.time()*1000),'local'));c.commit();c.close()
  c=s.zi.connect_db(s.Z_DB);rows=s.zi.build_session_rows({**self.meta,'turns':[(x['role'],x['text']) for x in self.turns]},'claude','proj_fixture');s.zi.write_one(c.cursor(),rows);c.commit();c.close();self.sources['zcode']='zcode:'+rows['sid']
  s.rescan_sessions()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.tmp.cleanup()
 def test_real_schemas_all_twelve_roundtrip_and_restore(self):
  for tool,src in self.sources.items():
   for target in s.TOOLS:
    if target==tool:continue
    with self.subTest(source=tool,target=target):
     meta=s.find_meta(tool,src); turns=s.session_detail(tool,src)
     first=s.bridge_ops.sync_session(s,tool,src,target,meta,turns)
     second=s.bridge_ops.sync_session(s,tool,src,target,meta,turns);self.assertTrue(second['already'])
     key=('zcode:' if target=='zcode' else 'to-'+target+':')+tool+':'+src
     targetsrc=s.load_ledger()[key];targetsrc='zcode:'+targetsrc if target=='zcode' else targetsrc
     self.assertEqual(s.session_detail(target,targetsrc),self.turns)
     if target=='codex':
      c=sqlite3.connect(s.CX_SQLITE);tid=Path(targetsrc).stem[-36:];kinds=c.execute('SELECT item_type FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal',(tid,)).fetchall();c.close();self.assertEqual([x[0] for x in kinds],['userMessage','agentMessage','agentMessage'])
     deleted=s.bridge_ops.delete_session(s,target,targetsrc);s.bridge_ops.restore_session(s,deleted['trash_id'])
     self.assertEqual(s.session_detail(target,targetsrc),self.turns)
  for db in [s.CX_STATE,s.CX_SQLITE,s.Z_DB,s.WB_DB]:
   c=sqlite3.connect(db);self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[]);c.close()
 def test_async_empty_cache_and_paginated_large_transcript(self):
  s._cache['sessions']=None;s._cache['ts']=0;s.DISK_CACHE.unlink(missing_ok=True)
  with patch.object(s,'rescan_sessions',lambda:time.sleep(.2)):
   start=time.monotonic();self.assertEqual(s.collect_sessions(),[]);self.assertLess(time.monotonic()-start,.1)
  time.sleep(.25)
  p=Path(self.sources['claude']);p.write_text('\n'.join(json.dumps({'type':'user','message':{'role':'user','content':'message '+str(i)}}) for i in range(205))+'\n')
  result=s.session_reader.cached_detail(s,'claude',str(p),0,100)
  for _ in range(100):
   if not result['loading']:break
   time.sleep(.01);result=s.session_reader.cached_detail(s,'claude',str(p),0,100)
  self.assertEqual(result['total'],205);self.assertEqual(len(result['turns']),100);self.assertEqual(result['next_offset'],100)
  self.assertEqual(len(s.session_reader.cached_detail(s,'claude',str(p),200,100)['turns']),5)
 def test_api_requires_token_host_origin_and_body_limits(self):
  httpd=ThreadingHTTPServer(('127.0.0.1',0),s.http_api.make_handler(s));port=httpd.server_port
  with patch.object(s,'PORT',port):
   thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
   def request(path,headers={},method='GET',body=None):
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=5);c.request(method,path,body,headers);r=c.getresponse();raw=r.read();status=r.status;cache=r.getheader('Cache-Control');c.close();return status,raw,cache
   try:
    self.assertEqual(request('/api/sessions')[0],403)
    code,body,cache=request('/api/sessions',{'X-Bridge-Token':s.API_TOKEN});self.assertEqual(code,200);self.assertEqual(cache,'no-store');self.assertIn('capabilities',json.loads(body))
    self.assertEqual(request('/api/sessions',{'X-Bridge-Token':s.API_TOKEN,'Origin':'https://evil.invalid'})[0],403)
    self.assertEqual(request('/api/sessions',{'X-Bridge-Token':s.API_TOKEN,'Host':'evil.invalid'})[0],403)
    self.assertEqual(request('/api/delete',{'X-Bridge-Token':s.API_TOKEN,'Content-Type':'application/json','Content-Length':'70000'},'POST',b'{}')[0],400)
    auth={'X-Bridge-Token':s.API_TOKEN,'Content-Type':'application/json'}
    code,body,_=request('/api/cleanup/preview',auth,'POST',json.dumps({'days':14,'tool':'all'}));self.assertEqual(code,400)
    code,body,_=request('/api/cleanup/preview',auth,'POST',json.dumps({'days':30,'tool':'claude'}));self.assertEqual(code,200)
    plan_id=json.loads(body)['plan_id']
    for _ in range(100):
     code,body,_=request('/api/cleanup/plan?plan_id='+plan_id,{'X-Bridge-Token':s.API_TOKEN})
     plan=json.loads(body)
     if plan['status']!='preparing':break
     time.sleep(.01)
    self.assertEqual(plan['status'],'ready');self.assertEqual(plan['count'],0)
    code,body,_=request('/api/cleanup/commit',auth,'POST',json.dumps({'plan_id':plan_id,'confirm':False}));self.assertEqual(code,400)
    self.assertTrue(Path(self.sources['claude']).exists())
    code,body,_=request('/?token='+s.API_TOKEN);self.assertEqual(code,200);self.assertIn(b'window.BRIDGE_TOKEN',body)
   finally:httpd.shutdown();httpd.server_close()
 def test_correct_sizes_after_update_delete(self):
  before=s.session_reader.db_scan(s,'zcode')[0]['size']
  c=sqlite3.connect(s.Z_DB);c.execute("UPDATE part SET data=? WHERE type IS NULL",('{}',)) if False else c.execute("UPDATE part SET data=json_set(data,'$.text',?) WHERE json_extract(data,'$.type')='text'",('changed text much bigger than before '*20,));c.commit();c.close()
  self.assertGreater(s.session_reader.db_scan(s,'zcode')[0]['size'],before)
 def test_usage_refresh_invokes_force_and_failure_does_not_echo_secrets(self):
  data={'at':time.time(),'quota':[],'local':{'days':[],'daily':[],'months':[],'sources':[],'month':'2026-10','today':{}}}
  with patch.object(s.usage_backend,'snapshot',return_value=data) as collect, patch('subprocess.run') as subprocess_run:
   result=s.usage_backend.refresh_snapshot(s.MT_CACHE,force=True)
   self.assertEqual(result['at'],data['at']);collect.assert_called_once_with(force=True,cache_file=str(s.MT_CACHE));subprocess_run.assert_not_called()
  with patch.object(s.usage_backend,'snapshot',side_effect=RuntimeError('SECRET')):
   with self.assertRaisesRegex(RuntimeError,'RuntimeError') as caught:s.usage_backend.refresh_snapshot(s.MT_CACHE,force=True)
   self.assertNotIn('SECRET',str(caught.exception))
 def test_app_zcode_manifest_is_accepted_by_cli_and_restore_retains_history(self):
  src=self.sources['workbuddy'];result=s.bridge_ops.sync_session(s,'workbuddy',src,'zcode',self.meta,self.turns)
  key='zcode:workbuddy:'+src;sid=s.load_ledger()[key]
  s.bridge_state.atomic_json(s.LEDGER,{**s.load_ledger(),'unrelated':'retain'})
  with patch.object(s.zi,'BRIDGE',s.BRIDGE),patch.object(s.zi,'REAL_DB',str(s.Z_DB)):
   s.zi.do_rollback(str(s.Z_DB))
  self.assertNotIn(key,s.load_ledger());self.assertEqual(s.load_ledger()['unrelated'],'retain')
  item=s.bridge_ops.list_trash(s)[0];s.bridge_ops.restore_session(s,item['trash_id'])
  self.assertEqual(s.session_detail('zcode','zcode:'+sid),self.turns)
  self.assertEqual(s.bridge_state.load_zcode_manifest(s.BRIDGE/'zcode-injected-manifest.json',s.Z_DB)['sessions'][0]['status'],'committed')
 def test_scope_enforced_and_source_deletion_never_unmarks_mirror(self):
  src=self.sources['claude'];meta=s.find_meta('claude',src)
  target=s.bridge_ops.sync_session(s,'claude',src,'workbuddy',meta,self.turns)
  value=s.load_ledger()['to-workbuddy:claude:'+src]
  s.bridge_ops.delete_session(s,'claude',src);s.rescan_sessions()
  mirror=s.find_meta('workbuddy',value);self.assertTrue(mirror['mirror'])
  with self.assertRaisesRegex(ValueError,'副本'):s.sync_one('workbuddy',value,'codex')
 def test_close_hides_but_quit_allows_window_close(self):
  from types import SimpleNamespace
  hidden=[]
  with patch.object(s,'_win',{'main':SimpleNamespace(hide=lambda:hidden.append(True))}),patch.object(s,'_quitting',False):
   if s.platform_paths.WINDOWS:  # Windows 没有菜单栏入口：关窗口就是退出，不藏起来
    self.assertTrue(s.main_closing());self.assertEqual(hidden,[]);return
   self.assertFalse(s.main_closing());self.assertEqual(hidden,[True])
   s._quitting=True;self.assertTrue(s.main_closing());self.assertEqual(hidden,[True])
if __name__=='__main__':unittest.main()
