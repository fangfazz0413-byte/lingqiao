"""Read-only scans and paged, streaming transcript extraction."""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

def records(path):
    """Never hide corrupt records as an apparently complete conversation."""
    with Path(path).open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError('第 %d 行尚未写完或 JSON 损坏，请稍后重试。' % number) from error

def contained(path, root):
    p, r = Path(path).resolve(), Path(root).resolve()
    if p == r or not p.is_relative_to(r):
        raise ValueError('路径不在允许目录中')
    return p

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()


def connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10, factory=ClosingConnection)

def title_maps(env):
    cc, cx = {}, {}
    root = env.HOME / 'Library/Application Support/Claude/claude-code-sessions'
    for p in root.rglob('local_*.json') if root.exists() else []:
        try:
            d = json.loads(p.read_text()); cc[d.get('cliSessionId')] = d.get('title') or ''
        except (OSError, ValueError):
            continue
    if Path(env.CX_STATE).exists():
        with connect(env.CX_STATE) as c:
            cols = {r[1] for r in c.execute('PRAGMA table_info(threads)')}
            if {'id', 'title'}.issubset(cols):
                name = 'name' if 'name' in cols else 'title'
                for sid, title in c.execute('SELECT id, coalesce(nullif('+name+",''),title) FROM threads"):
                    cx[sid] = title or ''
    return cc, cx

def file_head(env, path, tool):
    title, cwd, sid, kind = '', '', path.stem, 'user'
    # Bound header scanning; remaining content is streamed only when opened.
    for i, rec in enumerate(records(path)):
        if i >= 2048:
            break
        if tool == 'claude':
            cwd = cwd or rec.get('cwd', '')
            sid = rec.get('sessionId') or sid
            if rec.get('isSidechain'):
                kind = 'subagent'
            if rec.get('type') == 'summary' and rec.get('summary'):
                title = rec['summary']; break
            turns = env.sync.cc_turns([rec])
        else:
            p = rec.get('payload') or {}
            if rec.get('type') == 'session_meta':
                cwd = p.get('cwd') or cwd
                sid = p.get('id') or path.stem[-36:]
                kind = 'subagent' if isinstance(p.get('source'),dict) and 'subagent' in p['source'] else (p.get('thread_source') or 'user')
            turns = env.sync.cx_turns([rec])
        if not title:
            title = env.sync.first_user_text(turns)
        if title and cwd and i >= 30:
            break
    return title, cwd, sid, kind

def file_scan(env, tool, maps):
    roots = [env.CC_ROOT] if tool == 'claude' else [env.CX_ROOT, env.HOME / '.codex/archived_sessions']
    out = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob('*.jsonl' if tool == 'claude' else 'rollout-*.jsonl'):
            try:
                path=contained(path, root)
                st = path.stat(); title,cwd,sid,kind = file_head(env,path,tool)
                out.append({'tool':tool, 'src':str(path), 'dir':cwd or '(未知)',
                            'title':env.clean_title(maps.get(sid) or title) or sid[:12],
                            'mtime':st.st_mtime, 'size':st.st_size, 'session_id':sid,
                            'kind':kind, 'archived':root != roots[0]})
            except (OSError, ValueError) as e:
                env.log('scan-file-failed tool='+tool+' error='+type(e).__name__)
    # Canonical id is payload.id for Codex; never merge subagents into the parent.
    grouped = {}
    for row in out:
        old = grouped.get(row['session_id'])
        if old is None:
            row['related_files'] = []; grouped[row['session_id']] = row
        elif row['size'] > old['size']:
            row['related_files'] = old.get('related_files', []) + [old['src']]
            row['mtime'] = max(row['mtime'], old['mtime'])
            grouped[row['session_id']] = row
        else:
            old['related_files'].append(row['src'])
            old['mtime'] = max(old['mtime'], row['mtime'])
    return list(grouped.values())

def db_scan(env, tool):
    path = env.Z_DB if tool == 'zcode' else env.WB_DB
    if not Path(path).exists():
        return []
    out = []
    with connect(path) as c:
        c.row_factory = sqlite3.Row
        if tool == 'zcode':
            # Correct after updates/deletes/database replacement. No rowid accumulator.
            rows = c.execute('SELECT s.id,s.directory,s.title,s.time_updated,s.time_archived,coalesce(p.n,0) n FROM session s LEFT JOIN (SELECT session_id,sum(length(data)) n FROM part GROUP BY session_id) p ON p.session_id=s.id')
            for row in rows:
                d = dict(row); sid=d['id']
                out.append({'tool':tool,'src':'zcode:'+sid,'dir':d['directory'] or '(未知)',
                            'title':env.clean_title(d['title']) or sid[:12], 'size':d['n'],
                            'mtime':(d['time_updated'] or 0)/1000,'archived':bool(d['time_archived']), 'kind':'user'})
        else:
            paths = {p.stem:p.resolve() for p in env.WB_ROOT.rglob('*.jsonl') if p.resolve().is_relative_to(env.WB_ROOT.resolve())}
            for row in c.execute('SELECT * FROM sessions WHERE deleted_at IS NULL'):
                d=dict(row); p=paths.get(d['id'])
                out.append({'tool':tool,'src':str(p) if p else 'wb:'+d['id'], 'dir':d.get('cwd') or '(未知)',
                            'title':env.clean_title(d.get('custom_title') or d.get('title')) or d['id'][:12],
                            'size':p.stat().st_size if p else 0,
                            'mtime':(d.get('last_activity_at') or d.get('updated_at') or 0)/1000,
                            'missing_file':p is None,'archived':False,'kind':'user'})
    return out

def validate(env, tool, src):
    if tool == 'zcode':
        import re
        sid = src.removeprefix('zcode:')
        if not re.fullmatch(r'sess_[A-Za-z0-9_-]{8,100}', sid):
            raise ValueError('非法 ZCode ID')
        return sid
    roots = {'claude':[env.CC_ROOT], 'codex':[env.CX_ROOT, env.HOME/'.codex/archived_sessions'], 'workbuddy':[env.WB_ROOT]}
    if tool not in roots or src.startswith('wb:'):
        raise ValueError('该会话没有可读取的文本文件')
    for root in roots[tool]:
        try:
            p=contained(src,root)
            if p.suffix != '.jsonl': raise ValueError('非法扩展名')
            return p
        except ValueError:
            continue
    raise ValueError('非法会话路径')

def iter_turns(env, tool, src):
    p=validate(env,tool,src)
    if tool == 'zcode':
        with connect(env.Z_DB) as c:
            sql="SELECT json_extract(m.data,'$.role'),p.data FROM part p JOIN message m ON m.id=p.message_id WHERE p.session_id=? AND coalesce(json_extract(m.data,'$.semantics.uiVisibility'),'visible')='visible' ORDER BY m.sequence,p.sequence"
            for role,data in c.execute(sql,(p,)):
                d=json.loads(data)
                if role in ('user','assistant') and d.get('type')=='text' and d.get('text'):
                    yield {'role':role,'text':d['text']}
        return
    reader={'claude':env.sync.cc_turns,'codex':env.sync.cx_turns,'workbuddy':env.wb_turns}[tool]
    for rec in records(p):
        for role,text in reader([rec]):
            if role in ('user','assistant'):
                yield {'role':role,'text':text}

def signature(env,tool,src):
    p=validate(env,tool,src)
    if tool=='zcode':
        with connect(env.Z_DB) as c:
            row=c.execute('SELECT time_updated FROM session WHERE id=?',(p,)).fetchone()
            if not row: raise ValueError('会话已不存在')
            # Snapshot signature includes content lengths/update time and database inode.
            size=c.execute('SELECT count(*),sum(length(data)),max(time_updated) FROM part WHERE session_id=?',(p,)).fetchone()
        return str((Path(env.Z_DB).stat().st_ino,row,size))
    st=p.stat(); return str((st.st_ino,st.st_size,st.st_mtime_ns))

def cached_detail(env,tool,src,offset=0,limit=100):
    import threading
    if offset<0 or limit<1 or limit>100: raise ValueError('分页参数超出范围')
    sig=signature(env,tool,src)
    key=hashlib.sha256((tool+'|'+src+'|'+sig).encode()).hexdigest()
    root=env.BRIDGE/'details'; root.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=root/(key+'.jsonl'); idx=root/(key+'.index.json')
    with env._detail_lock:
        if not idx.exists() and key not in env._detail_jobs:
            env._detail_jobs[key]={'loading':True}
            def work():
                temp=path.with_suffix('.tmp'); offsets=[]
                try:
                    with temp.open('wb') as f:
                        temp.chmod(0o600)
                        for turn in iter_turns(env,tool,src):
                            offsets.append(f.tell()); f.write((json.dumps(turn,ensure_ascii=False)+'\n').encode())
                    if sig!=signature(env,tool,src): raise ValueError('会话在读取期间变化，请重试。')
                    temp.replace(path); env.bridge_state.atomic_json(idx,{'offsets':offsets})
                    env._detail_jobs[key]={'loading':False}
                except Exception as e:
                    temp.unlink(missing_ok=True); env._detail_jobs[key]={'loading':False,'error':str(e)}
            threading.Thread(target=work,daemon=True).start()
        job=env._detail_jobs.get(key,{})
    if job.get('error'):
        with env._detail_lock: env._detail_jobs.pop(key,None)
        raise ValueError(job['error'])
    if not idx.exists(): return {'turns':[],'loading':True,'next_offset':offset,'total':None}
    offsets=json.loads(idx.read_text())['offsets']; turns=[]
    if offset<len(offsets):
        with path.open('rb') as f:
            f.seek(offsets[offset])
            for _ in range(min(limit,len(offsets)-offset)):
                turns.append(json.loads(f.readline()))
    nxt=offset+len(turns)
    return {'turns':turns,'total':len(offsets),'next_offset':nxt if nxt<len(offsets) else None,'loading':False}
