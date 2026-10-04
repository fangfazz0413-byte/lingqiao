import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import bridge_state
import bridge_ops as ops
import zcode_inject as zi


@contextmanager
def dbconn(path):
    c=sqlite3.connect(path)
    try:
        with c:
            yield c
    finally:
        c.close()

def db(path, ddl):
    with dbconn(path) as c:
        c.executescript(ddl)


class Operations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = SimpleNamespace(HOME=self.root, BRIDGE=self.root/'bridge',
            LEDGER=self.root/'bridge/ledger.json', PROVENANCE=self.root/'bridge/provenance.json',
            CC_ROOT=self.root/'claude', CX_ROOT=self.root/'codex', WB_ROOT=self.root/'wb',
            CX_STATE=self.root/'state.sqlite', CX_SQLITE=self.root/'history.sqlite',
            Z_DB=self.root/'zcode.sqlite', WB_DB=self.root/'wb.sqlite',
            CX_INDEX=self.root/'index.jsonl', CC_META_ROOT=self.root/'meta', zi=zi)
        for d in (self.env.BRIDGE,self.env.CC_ROOT,self.env.CX_ROOT,self.env.WB_ROOT,self.env.CC_META_ROOT): d.mkdir()
        (self.env.CC_META_ROOT/'account'/'project').mkdir(parents=True)
        self.env.sync = SimpleNamespace(cc_desktop_project_dir=lambda: self.env.CC_META_ROOT/'account'/'project')
        bridge_state.atomic_json(self.env.LEDGER,{})
        db(self.env.CX_STATE, '''CREATE TABLE threads (id TEXT PRIMARY KEY,rollout_path TEXT NOT NULL,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,source TEXT NOT NULL,
            model_provider TEXT NOT NULL,cwd TEXT NOT NULL,title TEXT NOT NULL,sandbox_policy TEXT NOT NULL,
            approval_mode TEXT NOT NULL,preview TEXT NOT NULL,history_mode TEXT NOT NULL);
            CREATE TABLE assets(id TEXT PRIMARY KEY,thread_id TEXT REFERENCES threads(id) ON DELETE CASCADE,body BLOB);
            CREATE TABLE asset_tags(id TEXT PRIMARY KEY,asset_id TEXT REFERENCES assets(id) ON DELETE CASCADE,value TEXT);''')
        db(self.env.CX_SQLITE, '''CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,PRIMARY KEY(thread_id,turn_id));
            CREATE TABLE thread_items(thread_id TEXT,item_id TEXT,body TEXT,PRIMARY KEY(thread_id,item_id));
            CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER);
            CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,body TEXT,PRIMARY KEY(thread_id,item_id));''')
        db(self.env.WB_DB, '''CREATE TABLE sessions(id TEXT PRIMARY KEY,cwd TEXT NOT NULL,user_id TEXT NOT NULL,
            title TEXT,status TEXT,created_at INTEGER,updated_at INTEGER,last_activity_at INTEGER,
            deleted_at INTEGER,transport TEXT,permission_mode TEXT);
            CREATE TABLE history(id TEXT PRIMARY KEY,session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,body TEXT);''')
        with dbconn(self.env.WB_DB) as c:
            c.execute("INSERT INTO sessions VALUES ('seed','/fixture','account','seed','completed',1,1,1,NULL,'local','default')")
        db(self.env.Z_DB, '''CREATE TABLE session(id TEXT PRIMARY KEY,project_id TEXT,slug TEXT,directory TEXT,path TEXT,title TEXT,
            version TEXT,permission TEXT,time_created INTEGER,time_updated INTEGER,task_type TEXT,title_source TEXT,trace_id TEXT);
            CREATE TABLE session_entry(id TEXT PRIMARY KEY,session_id TEXT REFERENCES session(id) ON DELETE CASCADE,type TEXT,time_created INTEGER,time_updated INTEGER,data TEXT);
            CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT REFERENCES session(id) ON DELETE CASCADE,time_created INTEGER,time_updated INTEGER,data TEXT,sequence INTEGER);
            CREATE TABLE part(id TEXT PRIMARY KEY,message_id TEXT REFERENCES message(id) ON DELETE CASCADE,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT,sequence INTEGER);
            CREATE TABLE custom_child(id TEXT PRIMARY KEY,part_id TEXT REFERENCES part(id) ON DELETE CASCADE,secret BLOB);
            CREATE TRIGGER msgseq AFTER INSERT ON message WHEN new.sequence IS NULL BEGIN UPDATE message SET sequence=(SELECT COUNT(*)-1 FROM message WHERE session_id=new.session_id) WHERE id=new.id; END;
            CREATE TRIGGER partseq AFTER INSERT ON part WHEN new.sequence IS NULL BEGIN UPDATE part SET sequence=(SELECT COUNT(*)-1 FROM part WHERE message_id=new.message_id) WHERE id=new.id; END;''')
        self.env.steal_codex_meta_fields = lambda: {'model_provider':'fixture','base_instructions':{'text':'fixture'},'context_window':1000}
        def patch(path):
            sid=json.loads(Path(path).read_text().splitlines()[0])['payload']['id']
            with dbconn(self.env.CX_SQLITE) as c:
                c.execute('INSERT INTO thread_turns VALUES (?,?)',(sid,'turn'))
                c.execute('INSERT INTO thread_items VALUES (?,?,?)',(sid,'msg','fixture'))
                c.execute('INSERT INTO thread_history_projection_state VALUES (?,?,?)',(sid,50,5))
        self.env.patch_codex_sqlite=patch

    def tearDown(self):
        self.tmp.cleanup()

    def source(self,tool,sid='11111111-1111-1111-1111-111111111111'):
        root=getattr(self.env,{'claude':'CC_ROOT','codex':'CX_ROOT','workbuddy':'WB_ROOT'}[tool])
        p=root/('rollout-2026-10-02T01-00-00-'+sid+'.jsonl' if tool=='codex' else sid+'.jsonl')
        p.write_text(json.dumps({'type':'session_meta','payload':{'id':sid,'session_id':'parent-not-child'}})+'\n')
        return str(p)

    def test_all_twelve_sync_directions_are_idempotent(self):
        for source_tool in ops.TOOLS:
            if source_tool=='zcode':
                source='zcode:sess_source1111'
            else: source=self.source(source_tool)
            for target in ops.TOOLS-{source_tool}:
                with self.subTest(source=source_tool,target=target):
                    r=ops.sync_session(self.env,source_tool,source,target,{'dir':'/fixture','title':'Title','mtime':1234567890},
                                       [{'role':'user','text':'hello'},{'role':'assistant','text':'world'}])
                    self.assertFalse(r['already'])
                    again=ops.sync_session(self.env,source_tool,source,target,{'dir':'/fixture','title':'Title','mtime':1234567890},
                                          [{'role':'user','text':'hello'}])
                    self.assertTrue(again['already'])
        self.assertEqual(len(bridge_state.load_json(self.env.LEDGER,{})),12)
        for dbpath in (self.env.CX_STATE,self.env.CX_SQLITE,self.env.WB_DB,self.env.Z_DB):
            with dbconn(dbpath) as c:self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_codex_delete_restores_all_related_tables_and_merges_index(self):
        sid='11111111-1111-1111-1111-111111111111'; source=self.source('codex',sid)
        with dbconn(self.env.CX_STATE) as c:
            c.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(sid,source,1,1,'fixture','fixture','/fixture','Title','{}','untrusted','hello','paginated'))
            c.execute('INSERT INTO assets VALUES (?,?,?)',('asset',sid,b'private\x00'))
            c.execute('INSERT INTO asset_tags VALUES (?,?,?)',('tag','asset','tag'))
        with dbconn(self.env.CX_SQLITE) as c:
            c.execute('INSERT INTO thread_realtime_items VALUES (?,?,?)',(sid,'real','retained'))
        self.env.CX_INDEX.write_text(json.dumps({'id':sid,'thread_name':'old'})+'\n')
        bridge_state.atomic_json(self.env.LEDGER,{'to-codex:claude:source':source})
        bridge_state.record_provenance(self.env.PROVENANCE,'codex',source,'claude','source',ledger_key='to-codex:claude:source')
        result=ops.delete_session(self.env,'codex',source)
        self.assertFalse(Path(source).exists())
        self.assertEqual(bridge_state.provenance_records(self.env.PROVENANCE)[source]['status'],'deleted')
        self.env.CX_INDEX.write_text(json.dumps({'id':'new','thread_name':'new since delete'})+'\n')
        ops.restore_session(self.env,result['trash_id'])
        self.assertTrue(Path(source).exists())
        self.assertEqual({json.loads(x)['id'] for x in self.env.CX_INDEX.read_text().splitlines()}, {'new',sid})
        with dbconn(self.env.CX_STATE) as c:
            self.assertEqual(c.execute('SELECT body FROM assets').fetchone()[0], b'private\x00')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM asset_tags').fetchone()[0],1)
        with dbconn(self.env.CX_SQLITE) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM thread_realtime_items').fetchone()[0],1)
        self.assertEqual(Path(result['trash']).joinpath('journal.json').stat().st_mode&0o777,0o600)

    def test_delete_failure_restores_files_and_rows(self):
        source=self.source('workbuddy'); sid=Path(source).stem
        with dbconn(self.env.WB_DB) as c:
            c.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)',(sid,'/fixture','account','title','completed',1,1,1,123,'local','default'))
            c.execute('INSERT INTO history VALUES (?,?,?)',('h',sid,'preserve'))
        self.env.ops_failpoint=lambda stage: (_ for _ in ()).throw(RuntimeError('fixture failure')) if stage=='delete_state' else None
        with self.assertRaises(RuntimeError):ops.delete_session(self.env,'workbuddy',source)
        self.assertTrue(Path(source).exists())
        with dbconn(self.env.WB_DB) as c:
            self.assertEqual(c.execute('SELECT deleted_at FROM sessions WHERE id=?',(sid,)).fetchone()[0],123)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM history').fetchone()[0],1)
        j=json.loads(next((self.env.BRIDGE/'operations').glob('*/journal.json')).read_text())
        self.assertEqual(j['status'],'compensated')

    def test_restore_conflict_refuses_without_mutation(self):
        source=self.source('claude'); r=ops.delete_session(self.env,'claude',source)
        Path(source).write_text('new material')
        with self.assertRaises(ValueError):ops.restore_session(self.env,r['trash_id'])
        self.assertEqual(Path(source).read_text(),'new material')
        self.assertEqual(ops.list_trash(self.env)[0]['status'],'deleted')

    def test_sync_failure_removes_only_this_operation(self):
        source=self.source('claude')
        self.env.ops_failpoint=lambda stage: (_ for _ in ()).throw(RuntimeError('fixture failure')) if stage=='sync_state' else None
        with self.assertRaises(RuntimeError):ops.sync_session(self.env,'claude',source,'codex',{'dir':'/fixture','title':'t','mtime':12345},[{'role':'user','text':'hello'}])
        self.assertEqual(list(self.env.CX_ROOT.rglob('*.jsonl')),[])
        self.assertEqual(bridge_state.load_json(self.env.LEDGER,{}),{})
        with dbconn(self.env.CX_STATE) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM threads').fetchone()[0],0)
        with dbconn(self.env.CX_SQLITE) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM thread_items').fetchone()[0],0)

    def test_symlink_escape_rejected(self):
        outside=self.root/'secret.jsonl'; outside.write_text('secret')
        escaped=self.env.CC_ROOT/'link.jsonl'; escaped.symlink_to(outside)
        with self.assertRaises(ValueError):ops.delete_session(self.env,'claude',str(escaped))
        self.assertTrue(outside.exists())

    def test_zcode_delete_dynamic_fk_and_source_provenance(self):
        sid='sess_source1111'
        with dbconn(self.env.Z_DB) as c:
            c.execute('INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',(sid,'proj','slug','/fixture','/fixture','title','1','{}',1,1,'interactive','first_input','trace'))
            c.execute('INSERT INTO message VALUES (?,?,?,?,?,?)',('msg',sid,1,1,'{}',0))
            c.execute('INSERT INTO part VALUES (?,?,?,?,?,?,?)',('part','msg',sid,1,1,'{}',0))
            c.execute('INSERT INTO custom_child VALUES (?,?,?)',('child','part',b'secret'))
        bridge_state.record_provenance(self.env.PROVENANCE,'codex','target','zcode','zcode:'+sid)
        r=ops.delete_session(self.env,'zcode','zcode:'+sid)
        self.assertEqual(bridge_state.provenance_records(self.env.PROVENANCE)['target']['status'],'source_deleted')
        ops.restore_session(self.env,r['trash_id'])
        with dbconn(self.env.Z_DB) as c:self.assertEqual(c.execute('SELECT secret FROM custom_child').fetchone()[0],b'secret')
        self.assertEqual(bridge_state.provenance_records(self.env.PROVENANCE)['target']['status'],'active')


if __name__=='__main__':unittest.main()
