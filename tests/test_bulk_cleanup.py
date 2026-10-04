import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import test_server as fixture
import bulk_cleanup as bulk
import server as s

class BulkCleanupTests(unittest.TestCase):
 def setUp(self):
  self.f=fixture.ServerIntegration('test_real_schemas_all_twelve_roundtrip_and_restore');self.f.setUp()
  self.p=patch.object(s,'invalidate',lambda:None);self.p.start()
  self.old=time.time()-35*86400
  for tool,src in self.f.sources.items():self.make_old(tool,src,self.old)
  self.metadata=next(s.CC_META_ROOT.rglob('local_*.json'));self.scope='all'
  s.rescan_sessions()
 def tearDown(self):self.p.stop();self.f.tearDown()
 def make_old(self,tool,src,stamp):
  if tool=='zcode':
   sid=src.removeprefix('zcode:');c=sqlite3.connect(s.Z_DB)
   for table in ('session','message','part'):c.execute('UPDATE '+table+' SET time_updated=?',(int(stamp*1000),))
   c.commit();c.close();return
  path=Path(src);records=[json.loads(x) for x in path.read_text().splitlines()]
  for r in records:r['timestamp']=stamp
  path.write_text('\n'.join(json.dumps(r) for r in records)+'\n');os.utime(path,(stamp,stamp))
  sid=path.stem[-36:] if tool=='codex' else path.stem
  if tool=='claude':
   for p in s.CC_META_ROOT.rglob('local_*.json'):
    d=json.loads(p.read_text());d['lastActivityAt']=int(stamp*1000);p.write_text(json.dumps(d))
  if tool=='workbuddy':
   c=sqlite3.connect(s.WB_DB);c.execute('UPDATE sessions SET last_activity_at=?,updated_at=? WHERE id=?',(int(stamp*1000),int(stamp*1000),sid));c.commit();c.close()
  if tool=='codex':
   c=sqlite3.connect(s.CX_STATE);c.execute('INSERT OR REPLACE INTO threads (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode) VALUES (?,?,?,?,?,?,?,?,?,?)',(sid,str(path),int(stamp),int(stamp),'vscode','fixture','/fixture','fixture','{}','untrusted'));c.commit();c.close()
 def plan(self,tool='all',days=30):return bulk.preview(s,days,tool,background=False)
 def commit(self,p,selected_ids=None):
  ids=selected_ids or [item['item_id'] for item in p.get('items',[])]
  return bulk.commit(s,p['plan_id'],True,selected_ids=ids,background=False)
 def test_preview_is_readonly_and_requires_fifteen_or_thirty(self):
  before={t:Path(src).read_bytes() for t,src in self.f.sources.items() if t!='zcode'}
  p=self.plan();self.assertEqual(p['status'],'ready');self.assertEqual(p['count'],4)
  for t,raw in before.items():self.assertEqual(Path(self.f.sources[t]).read_bytes(),raw)
  self.assertEqual(list((s.BRIDGE/'operations').glob('*')),[])
  for val in [True,None,'15',0,14,31,-30]:
   with self.assertRaises(ValueError):bulk.preview(s,val)
 def test_only_selected_subset_deleted_and_unselected_unchanged(self):
  p=self.plan();selected=next(item for item in p['items'] if item['tool']=='workbuddy')
  before={tool:Path(src).read_bytes() for tool,src in self.f.sources.items() if tool in ('claude','codex')}
  z_before=s.session_detail('zcode',self.f.sources['zcode'])
  result=self.commit(p,[selected['item_id']])
  self.assertEqual(result['selected_count'],1);self.assertEqual(result['count'],4)
  self.assertEqual(result['processed'],1);self.assertEqual(len(result['deleted']),1)
  self.assertEqual(result['deleted'][0]['tool'],'workbuddy')
  for tool,raw in before.items():self.assertEqual(Path(self.f.sources[tool]).read_bytes(),raw)
  self.assertEqual(s.session_detail('zcode',self.f.sources['zcode']),z_before)
  self.assertFalse(Path(self.f.sources['workbuddy']).exists())
  s.bridge_ops.restore_session(s,result['deleted'][0]['trash_id'])
  self.assertEqual(s.session_detail('workbuddy',self.f.sources['workbuddy']),self.f.turns)
 def test_missing_empty_duplicate_foreign_selection_is_rejected(self):
  p=self.plan();item_id=p['items'][0]['item_id']
  for ids in [None,[],item_id,[item_id,item_id],['a'*32],['bogus'],[True],{},[1]]:
   with self.subTest(ids=ids),self.assertRaises(ValueError):
    bulk.commit(s,p['plan_id'],True,selected_ids=ids,background=False)
  self.assertEqual(bulk.get_plan(s,p['plan_id'])['status'],'ready')
  self.assertEqual(list((s.BRIDGE/'operations').glob('*')),[])
 def test_retry_cannot_expand_original_selection(self):
  p=self.plan();chosen=p['items'][0]['item_id']
  first=self.commit(p,[chosen]);second=self.commit(p,[x['item_id'] for x in p['items']])
  self.assertEqual(second['selected_ids'],[chosen]);self.assertEqual(second['deleted'],first['deleted'])
  self.assertEqual(second['selected_count'],1)
 def test_native_activity_overrides_old_file_mtime(self):
  d=json.loads(self.metadata.read_text());d['lastActivityAt']=int(time.time()*1000);self.metadata.write_text(json.dumps(d))
  p=self.plan('claude');self.assertEqual(p['count'],0)
 def test_running_workbuddy_excluded_even_when_old(self):
  c=sqlite3.connect(s.WB_DB);c.execute("UPDATE sessions SET status='working'");c.commit();c.close()
  p=self.plan('workbuddy');self.assertEqual(p['count'],0);self.assertTrue(any('运行' in x['reason'] for x in p['skipped']))
 def test_fifteen_days_differs_from_thirty(self):
  self.make_old('claude',self.f.sources['claude'],time.time()-20*86400);s.rescan_sessions()
  self.assertEqual(self.plan('claude',15)['count'],1);self.assertEqual(self.plan('claude',30)['count'],0)
 def test_expired_tampered_and_unconfirmed_plans_cannot_delete(self):
  p=self.plan('claude')
  with self.assertRaises(ValueError):bulk.commit(s,p['plan_id'],False,selected_ids=[p['items'][0]['item_id']],background=False)
  path=bulk._path(s,p['plan_id']);raw=s.bridge_state.load_json(path);raw['items'][0]['src']='tampered';s.bridge_state.atomic_json(path,raw)
  with self.assertRaises(ValueError):self.commit(p)
  p=self.plan('claude');raw=s.bridge_state.load_json(bulk._path(s,p['plan_id']));raw['expires_at']=time.time()-1;s.bridge_state.atomic_json(bulk._path(s,p['plan_id']),raw)
  with self.assertRaises(ValueError):self.commit(p)
  self.assertTrue(Path(self.f.sources['claude']).exists())
 def test_changed_since_preview_is_skipped(self):
  p=self.plan('claude');src=Path(self.f.sources['claude']);src.write_text(src.read_text()+json.dumps({'type':'user','timestamp':time.time(),'message':{'role':'user','content':'new'}})+'\n')
  result=self.commit(p);self.assertEqual(result['deleted'],[]);self.assertTrue(src.exists());self.assertEqual(result['status'],'completed')
 def test_duplicate_commit_idempotent_and_restorable(self):
  p=self.plan('workbuddy');result=self.commit(p);self.assertEqual(len(result['deleted']),1)
  again=self.commit(p);self.assertEqual(again['deleted'],result['deleted'])
  s.bridge_ops.restore_session(s,result['deleted'][0]['trash_id']);self.assertEqual(s.session_detail('workbuddy',self.f.sources['workbuddy']),self.f.turns)
 def test_concurrent_commits_delete_only_once(self):
  p=self.plan('claude');results=[]
  def run():results.append(self.commit(p))
  threads=[threading.Thread(target=run) for _ in range(2)]
  for t in threads:t.start()
  for t in threads:t.join()
  done=bulk.get_plan(s,p['plan_id']);self.assertEqual(len(done['deleted']),1);self.assertEqual(len(list((s.BRIDGE/'operations').glob('*/journal.json'))),1)
 def test_software_restart_stops_remaining_work(self):
  p=self.plan('claude');raw=s.bridge_state.load_json(bulk._path(s,p['plan_id']));raw['status']='running';s.bridge_state.atomic_json(bulk._path(s,p['plan_id']),raw)
  bulk.recover_plans(s);self.assertEqual(bulk.get_plan(s,p['plan_id'])['status'],'failed');self.assertTrue(Path(self.f.sources['claude']).exists())
 def test_low_space_stops_before_deletion(self):
  p=self.plan('claude')
  with patch.object(bulk.shutil,'disk_usage',return_value=SimpleNamespace(free=1)):
   result=self.commit(p)
  self.assertEqual(result['status'],'failed');self.assertEqual(result['deleted'],[]);self.assertTrue(Path(self.f.sources['claude']).exists())
 def test_unknown_timestamp_and_archive_fail_closed(self):
  for value in [float('nan'),float('inf'),-1,True,'notdate',time.time()+3600]:
   with self.assertRaises(ValueError):bulk._stamp(value)
  row=s.find_meta('claude',self.f.sources['claude']);row={**row,'archived':True}
  with self.assertRaises(ValueError):bulk.observe(s,row)
 def test_zcode_active_goal_protected(self):
  sid=self.f.sources['zcode'].removeprefix('zcode:');c=sqlite3.connect(s.Z_DB);c.execute("INSERT INTO session_target (session_id,target_id,objective,status,time_created,time_updated) VALUES (?,?,?,'active',?,?)",(sid,'target','fixture',int(self.old*1000),int(self.old*1000)));c.commit();c.close()
  p=self.plan('zcode');self.assertEqual(p['count'],0)
 def test_running_after_backup_does_not_restore_stale_rows(self):
  p=self.plan('workbuddy');sid=Path(self.f.sources['workbuddy']).stem
  def failpoint(stage):
   if stage=='delete_prepared':
    c=sqlite3.connect(s.WB_DB);c.execute("UPDATE sessions SET status='working' WHERE id=?",(sid,));c.commit();c.close()
  with patch.object(s,'ops_failpoint',failpoint,create=True):result=self.commit(p)
  self.assertEqual(result['deleted'],[]);self.assertEqual(result['status'],'completed')
  c=sqlite3.connect(s.WB_DB);self.assertEqual(c.execute('SELECT status FROM sessions WHERE id=?',(sid,)).fetchone()[0],'working');c.close()
  journal=json.loads(next((s.BRIDGE/'operations').glob('*/journal.json')).read_text());self.assertEqual(journal['status'],'compensated')
  self.assertTrue(Path(self.f.sources['workbuddy']).exists())
if __name__=='__main__':unittest.main()
