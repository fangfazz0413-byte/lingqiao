"""Previewed, expiring, recoverable idle cleanup; never derives deletion from cache alone."""
import hashlib
import hmac
import json
import math
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

TTL = 300
MAX_ITEMS = 1000
TARGETS = ('main', 'subagent')      # 清理对象：主对话（闲置会话）｜子代理对话（主对话保留）
AUTO_FILE = 'subagent-cleanup.json'  # 定时清理子代理对话的开关和上次结果（默认关）
AUTO_INTERVAL = 20 * 3600            # 打开后大约每天跑一次
TERMINAL = {'completed','failed','expired'}
_jguard=threading.RLock()
_threads={}


def _root(env):
    root=Path(env.BRIDGE)/'cleanup-plans';root.mkdir(parents=True,exist_ok=True,mode=0o700)
    return root


def _path(env,plan_id):
    if not isinstance(plan_id,str) or not re.fullmatch(r'[0-9a-f]{32}',plan_id):
        raise ValueError('清理计划编号无效')
    return _root(env)/(plan_id+'.json')


def _save(env,p):env.bridge_state.atomic_json(_path(env,p['plan_id']),p)


def _stamp(value):
    if value is None or value=='':return None
    if isinstance(value,bool):raise ValueError('活动时间无效')
    if isinstance(value,str):
        try:number=float(value)
        except ValueError:
            try:number=datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
            except ValueError:raise ValueError('活动时间无效')
    else:number=float(value)
    if number>1e12:number/=1000
    if not math.isfinite(number) or number<=0:raise ValueError('活动时间缺失或无效')
    if number>time.time()+300:raise ValueError('活动时间在未来，无法安全判断闲置')
    return number


def _max(times,value):
    val=_stamp(value)
    if val is not None:times.append(val)


def _file_state(env,tool,src,times):
    p=env.session_reader.validate(env,tool,src)
    if not isinstance(p,Path):return None,False,None
    st=p.stat();_max(times,st.st_mtime)
    if st.st_size>2*1024**3:raise ValueError('会话过大，需在原工具核对')
    digest=hashlib.sha256();last_role=None;last_tool=False;event_running=None;rootid=None
    with p.open('rb') as stream:
        for raw in stream:
            digest.update(raw)
            if not raw.strip():continue
            try:r=json.loads(raw)
            except (ValueError,UnicodeError):raise ValueError('会话记录尚未写完或损坏')
            payload=r.get('payload') or {};message=r.get('message') or {}
            for obj in (r,payload,message):
                _max(times,obj.get('timestamp'))
            if tool=='codex' and r.get('type')=='session_meta':
                rootid=payload.get('id') or p.stem[-36:]
            if tool=='claude':
                rootid=r.get('sessionId') or rootid or p.stem
                if r.get('isSidechain'):continue
                role=message.get('role')
                if r.get('type') in ('user','assistant') and role in ('user','assistant'):
                    last_role=role
                    last_tool=message.get('stop_reason')=='tool_use'
            elif tool=='codex':
                if r.get('type')=='event_msg':
                    if payload.get('type')=='task_started':event_running=True
                    elif payload.get('type') in ('task_complete','task_completed','turn_aborted','task_failed'):event_running=False
                if r.get('type')=='response_item' and payload.get('type')=='message' and payload.get('role') in ('user','assistant'):
                    last_role=payload['role']
            else:
                if r.get('type')=='message' and r.get('role') in ('user','assistant'):last_role=r['role']
    after=p.stat()
    if (st.st_ino,st.st_size,st.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
        raise ValueError('读取时会话变化，正在使用')
    running=(last_role=='user' or last_tool or event_running is True)
    fingerprint={'path':str(p),'inode':st.st_ino,'size':st.st_size,'mtime_ns':st.st_mtime_ns,'sha256':digest.hexdigest()}
    return fingerprint,running,rootid or p.stem


def observe(env,row,now=None,*,subagent=False):
    """Fresh file/native activity+running check. Missing or unrecognized state fails closed.
    subagent=True：只核验子代理对话（ZCode 子会话、Codex 子线程、Claude Code 子代理记录），主对话一律不收。"""
    now=time.time() if now is None else now
    if row.get('archived'):raise ValueError('归档会话仅浏览，不参与批量清理')
    if subagent:
        if row.get('kind')!='subagent':raise ValueError('不是子代理对话，这里不清')
    elif row.get('kind')=='subagent':raise ValueError('子代理线程请用「清理对象：子代理对话」清理')
    if row.get('mirror'):raise ValueError('同步副本需单独管理')
    if row.get('missing_file'):raise ValueError('缺少本地会话文件')
    if row.get('related_files'):raise ValueError('同一会话分散在多个文件，需在原工具核对')
    tool,src=row['tool'],row['src'];times=[];native={};running=False
    if tool!='zcode':
        file,running,sid=_file_state(env,tool,src,times)
    else:file=None;sid=env.session_reader.validate(env,tool,src)
    if tool=='claude' and subagent:
        # 子代理记录（<主对话>/subagents/*.jsonl）没有桌面元数据，活动时间和运行状态只看文件本身
        if '/subagents/' not in str(row['src']).replace('\\','/'):raise ValueError('不是 Claude Code 子代理记录')
        native={'subagent_file':True}
    elif tool=='claude':
        count=0
        for meta in Path(env.CC_META_ROOT).rglob('local_*.json'):
            data=env.bridge_state.load_json(meta,{})
            if data.get('cliSessionId')!=sid:continue
            count+=1
            if data.get('isArchived'):raise ValueError('归档会话不参与批量清理')
            for name in ('lastActivityAt','lastFocusedAt'):_max(times,data.get(name))
            if data.get('isRunning') or data.get('isInProgress'):running=True
        if count==0:raise ValueError('缺少 Claude 桌面状态，无法核实会话是否闲置')
        native={'metadata_count':count}
    elif tool=='codex':
        with env.session_reader.connect(env.CX_STATE) as c:
            c.row_factory=__import__('sqlite3').Row
            record=c.execute('SELECT * FROM threads WHERE id=?',(sid,)).fetchone()
            if not record:raise ValueError('缺少官方列表记录，无法核实活动状态')
            data=dict(record)
            if data.get('archived'):raise ValueError('归档会话不参与批量清理')
            for name in ('updated_at_ms','recency_at_ms','updated_at','recency_at'):
                if data.get(name):_max(times,data[name])
            native['thread']=data
        with env.session_reader.connect(env.CX_SQLITE) as c:
            turns=c.execute('SELECT status,started_at,completed_at FROM thread_turns WHERE thread_id=?',(sid,)).fetchall()
            if any(str(x[0]).lower() not in ('completed','failed','interrupted','cancelled','canceled') for x in turns):running=True
            for _,started,completed in turns:
                if started:_max(times,started)
                if completed:_max(times,completed)
            native['turns']=turns
    elif tool=='workbuddy':
        with env.session_reader.connect(env.WB_DB) as c:
            c.row_factory=__import__('sqlite3').Row
            record=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
            if not record:raise ValueError('缺少官方会话记录')
            data=dict(record)
            if data.get('deleted_at'):raise ValueError('会话已删除')
            # An automation definition owns these conversations; manual bulk cleanup cannot delete it.
            if data.get('is_background_automation') or data.get('is_playground'):raise ValueError('自动化或试验会话需在原工具管理')
            status=str(data.get('status','')).lower()
            if status not in ('completed','failed','interrupted','cancelled','canceled','idle'):running=True
            for name in ('last_activity_at','updated_at'):_max(times,data.get(name))
            native=data
    elif tool=='zcode':
        with env.session_reader.connect(env.Z_DB) as c:
            c.row_factory=__import__('sqlite3').Row
            record=c.execute('SELECT * FROM session WHERE id=?',(sid,)).fetchone()
            if not record:raise ValueError('会话已不存在')
            data=dict(record)
            if data.get('time_archived'):raise ValueError('归档或子会话不参与批量清理')
            if subagent and not data.get('parent_id'):raise ValueError('不是子会话，这里不清')
            if not subagent and data.get('parent_id'):raise ValueError('归档或子会话不参与批量清理（子会话请用「清理对象：子代理对话」）')
            if data.get('time_compacting'):running=True
            _max(times,data.get('time_updated'));native['session']=data
            last=c.execute('SELECT data,time_updated FROM message WHERE session_id=? ORDER BY sequence DESC LIMIT 1',(sid,)).fetchone()
            if not last:raise ValueError('没有消息可核对，不批量清理')
            message=json.loads(last[0]);_max(times,last[1]);native['last_message']=message
            if message.get('role')!='assistant' or not (message.get('time') or {}).get('completed'):running=True
            parts=c.execute('SELECT count(*),sum(length(data)),max(time_updated) FROM part WHERE session_id=?',(sid,)).fetchone()
            if parts[2]:_max(times,parts[2])
            native['parts']=list(parts)
            tables={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table,col,active in [('model_usage','session_id',('running',)),('turn_usage','session_id',('running',)),('workflow_run','parent_session_id',('pending','running','paused')),('session_target','session_id',('active','paused','budget_limited'))]:
                if table not in tables:continue
                records=c.execute('SELECT status FROM '+table+' WHERE '+col+'=?',(sid,)).fetchall()
                native[table]=[x[0] for x in records]
                if any(str(x[0]).lower() in active for x in records):running=True
            for raw, in c.execute("SELECT data FROM session_entry WHERE session_id=? AND type='runtime/execution_state'",(sid,)):
                state=json.loads(raw)
                if str(state.get('status','')).lower() in ('running','working','inprogress','busy') or state.get('isRunning'):running=True
    else:raise ValueError('未知工具')
    if running:raise ValueError('正在运行、等待回复或状态未完成')
    if not times:raise ValueError('缺少可信活动时间')
    last_active=max(times)
    payload={'file':file,'native':native,'last_active_at':last_active}
    fingerprint=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
    return {'last_active_at':last_active,'fingerprint':fingerprint}


def _proof(env,p):
    body={key:p[key] for key in ('plan_id','days','tool','created_at','expires_at','items')}
    body['target']=p.get('target','main')
    return hmac.new(env.API_TOKEN.encode(),json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode(),hashlib.sha256).hexdigest()


def public(p):
    data={k:v for k,v in p.items() if k!='proof'}
    data['items']=[{k:v for k,v in x.items() if k not in ('fingerprint','row')} for x in p.get('items',[])]
    return data


def _selected_items(p, selected_ids):
    """Validate an explicit user selection against the frozen preview rows."""
    if not isinstance(selected_ids, list) or not selected_ids:
        raise ValueError('请选择至少一个会话后再确认清理')
    if any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value) for value in selected_ids):
        raise ValueError('所选会话编号无效')
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError('所选会话重复，请重新选择')
    by_id={item.get('item_id'):item for item in p.get('items',[])}
    if any(value not in by_id for value in selected_ids):
        raise ValueError('所选会话不属于当前预览清单，请重新预览')
    return [by_id[value] for value in selected_ids]


def _zcode_subagent_rows(env):
    path=Path(env.Z_DB)
    if not path.exists():return []
    out=[]
    with env.session_reader.connect(path) as c:
        c.row_factory=__import__('sqlite3').Row
        if 'parent_id' not in {r[1] for r in c.execute('PRAGMA table_info(session)')}:return []
        rows=c.execute('SELECT s.id,s.parent_id,s.title,s.directory,s.time_updated,s.time_archived,coalesce(p.n,0) n,ps.id parent_found,ps.title parent_title '
                       'FROM session s LEFT JOIN (SELECT session_id,sum(length(data)) n FROM part GROUP BY session_id) p ON p.session_id=s.id '
                       'LEFT JOIN session ps ON ps.id=s.parent_id WHERE s.parent_id IS NOT NULL')
        for r in rows:
            out.append({'tool':'zcode','src':'zcode:'+r['id'],'dir':r['directory'] or '(未知)','title':env.clean_title(r['title']) or r['id'][:12],
                        'size':r['n'],'mtime':(r['time_updated'] or 0)/1000,'archived':bool(r['time_archived']),'kind':'subagent',
                        'parent_title':(env.clean_title(r['parent_title'] or '') or '（无标题）') if r['parent_found'] else '（主对话已不在）'})
    return out


def _claude_subagent_rows(env,main_rows):
    root=Path(env.CC_ROOT);out=[]
    if not root.exists():return out
    titles={r.get('session_id'):r.get('title') for r in main_rows if r.get('tool')=='claude'}
    for path in root.glob('*/*/subagents/*.jsonl'):
        try:
            path=env.session_reader.contained(path,root);st=path.stat()
            title,cwd,sid,kind=env.session_reader.file_head(env,path,'claude')
        except (OSError,ValueError):continue
        if kind!='subagent':continue          # 不是 sidechain 记录就不当子代理
        parent=path.parent.parent.name
        out.append({'tool':'claude','src':str(path),'dir':cwd or '(未知)','title':env.clean_title(title) or path.stem[:12],'size':st.st_size,
                    'mtime':st.st_mtime,'session_id':sid,'kind':'subagent','archived':False,
                    'parent_title':titles.get(parent) or titles.get(sid) or '（主对话已不在）'})
    return out


def subagent_rows(env,tool='all'):
    """子代理对话候选：ZCode 的子会话（parent_id 不为空）、Codex 的子代理线程、Claude Code 的 subagents/*.jsonl。"""
    rows=env.rescan_sessions();out=[]
    if tool in ('all','zcode'):out+=_zcode_subagent_rows(env)
    if tool in ('all','codex'):out+=[{**r,'parent_title':r.get('parent_title','')} for r in rows if r.get('tool')=='codex' and r.get('kind')=='subagent']
    if tool in ('all','claude'):out+=_claude_subagent_rows(env,rows)
    return out


def get_plan(env,plan_id):
    p=env.bridge_state.load_json(_path(env,plan_id),None)
    if not p:raise ValueError('清理计划不存在')
    if p['status']=='ready' and time.time()>=p['expires_at']:
        p['status']='expired';_save(env,p)
    return public(p)


def _build(env,p):
    try:
        subagent=p.get('target')=='subagent'
        rows=subagent_rows(env,p['tool']) if subagent else env.rescan_sessions()
        for row in rows:
            if p['tool']!='all' and row['tool']!=p['tool']:continue
            # The cache is merely a prefilter; every candidate is checked against source data.
            if row.get('mtime',0)>p['created_at']-p['days']*86400:continue
            item={'tool':row['tool'],'src':row['src'],'title':row.get('title',''),'size':row.get('size',0)}
            if subagent:item['parent_title']=row.get('parent_title','')
            try:
                state=observe(env,row,subagent=subagent)
                if state['last_active_at']>p['created_at']-p['days']*86400:
                    p['skipped'].append({**item,'reason':'官方活动时间比文件缓存更新'});continue
                p['items'].append({**item,**state,'item_id':uuid.uuid4().hex,'row':row})
            except (ValueError,OSError,KeyError,TypeError) as e:
                p['skipped'].append({**item,'reason':str(e)})
        if len(p['items'])>MAX_ITEMS:raise ValueError('候选超过1000条，请选择单个工具分批清理')
        p['count']=len(p['items']);p['total_size']=sum(max(0,x['size']) for x in p['items'])
        p['selected_ids']=[];p['selected_count']=0;p['selected_size']=0
        p['expires_at']=time.time()+TTL;p['proof']=_proof(env,p);p['status']='ready'
    except Exception as e:p['status']='failed';p['error']='预览失败：'+str(e)
    with env.bridge_state.operation_lock(env.BRIDGE/'operation.lock'):_save(env,p)


def preview(env,days,tool='all',*,background=True,target='main'):
    if type(days) is not int or days not in (15,30):raise ValueError('只支持15天或30天闲置阈值')
    if not isinstance(tool,str) or tool not in ('all',*env.TOOLS):raise ValueError('清理范围无效')
    if target not in TARGETS:raise ValueError('清理对象只能是主对话或子代理对话')
    if getattr(env,'_attention',[]):raise ValueError('有中断操作待复核，暂不允许新清理')
    now=time.time();p={'plan_id':uuid.uuid4().hex,'days':days,'tool':tool,'target':target,'created_at':now,'expires_at':now+TTL,
        'status':'preparing','count':0,'total_size':0,'selected_ids':[],'selected_count':0,'selected_size':0,
        'items':[],'skipped':[],'errors':[],'deleted':[],'processed':0}
    with env.bridge_state.operation_lock(env.BRIDGE/'operation.lock'):
        # Old previews contain titles and paths; retain only recent operational evidence.
        for old in _root(env).glob('*.json'):
            if time.time()-old.stat().st_mtime>7*86400:old.unlink()
        _save(env,p)
    if background:
        threading.Thread(target=_build,args=(env,p),daemon=True).start()
    else:_build(env,p)
    return public(p)


def _execute(env,plan_id):
    with env.bridge_state.operation_lock(env.BRIDGE/'operation.lock'):
        p=env.bridge_state.load_json(_path(env,plan_id),{})
        selected={value for value in p.get('selected_ids',[])}
        for item in p['items']:
            if item.get('item_id') not in selected: continue
            try:
                if getattr(env,'_attention',[]):raise RuntimeError('出现待复核数据操作，已停止清理')
                subagent=p.get('target')=='subagent'
                fresh=observe(env,item['row'],subagent=subagent)
                if fresh['fingerprint']!=item['fingerprint']:raise ValueError('预览后会话发生变化，已跳过')
                if fresh['last_active_at']>p['created_at']-p['days']*86400:raise ValueError('会话最近被使用，已跳过')
                if shutil.disk_usage(env.REPO).free<max(item['size']*8,64*1024**2):raise RuntimeError('备份空间不足，已停止清理')
                def guard():
                    checked=observe(env,item['row'],subagent=subagent)
                    if checked['fingerprint']!=item['fingerprint']:
                        raise ValueError('备份过程中会话发生变化，已跳过')
                result=env.bridge_ops.delete_session(env,item['tool'],item['src'],pre_delete_check=guard)
                p['deleted'].append({'tool':item['tool'],'src':item['src'],'title':item['title'],'trash_id':result['trash_id']})
            except (ValueError,FileNotFoundError) as e:p['skipped'].append({'tool':item['tool'],'src':item['src'],'title':item['title'],'reason':str(e)})
            except Exception as e:
                p['errors'].append({'tool':item['tool'],'title':item['title'],'reason':str(e)})
                p['status']='failed';p['processed']+=1;_save(env,p)
                attention=env.bridge_ops.recover_pending_operations(env)
                if attention and hasattr(env,'_attention'):env._attention=attention
                break
            p['processed']+=1;_save(env,p)
        if p['status']=='running':p['status']='completed'
        p['completed_at']=time.time();_save(env,p)
    env.invalidate()
    env.log('cleanup-finished plan='+plan_id+' target='+p.get('target','main')+' deleted='+str(len(p['deleted']))+' skipped='+str(len(p['skipped']))+' errors='+str(len(p['errors'])))


def commit(env,plan_id,confirm,selected_ids=None,*,background=True):
    if confirm is not True:raise ValueError('必须确认预览清单后才能移入回收站')
    with env.bridge_state.operation_lock(env.BRIDGE/'operation.lock'):
        p=env.bridge_state.load_json(_path(env,plan_id),None)
        if not p:raise ValueError('计划不存在')
        if p['status'] in ('running','completed','failed'):return public(p)
        if p['status']!='ready':raise ValueError('请先完成预览')
        if time.time()>=p['expires_at']:
            p['status']='expired';_save(env,p);raise ValueError('预览已过期，请重新预览')
        if not hmac.compare_digest(p.get('proof',''),_proof(env,p)):raise ValueError('计划已变化或软件已重启，请重新预览')
        selected=_selected_items(p,selected_ids)
        p['selected_ids']=[item['item_id'] for item in selected]
        p['selected_count']=len(selected)
        p['selected_size']=sum(max(0,item.get('size',0)) for item in selected)
        p['processed']=0;p['deleted']=[];p['errors']=[]
        if not p['items']:raise ValueError('没有符合条件的会话')
        p['status']='running';p['started_at']=time.time();_save(env,p)
    if background:threading.Thread(target=_execute,args=(env,plan_id),daemon=True).start()
    else:_execute(env,plan_id)
    return get_plan(env,plan_id)


def recover_plans(env):
    # Never resume a deletion without the human reviewing it again after a restart.
    for path in _root(env).glob('*.json'):
        p=env.bridge_state.load_json(path,{})
        if p.get('status') in ('running','preparing'):
            p['status']='failed';p['error']='软件重启，剩余清理已停止。已删除会话仍在回收站。';_save(env,p)


def _auto_path(env):return Path(env.BRIDGE)/AUTO_FILE


def auto_settings(env):
    data=env.bridge_state.load_json(_auto_path(env),{}) or {}
    days=data.get('days') if data.get('days') in (15,30) else 30
    return {'enabled':data.get('enabled') is True,'days':days,'last_run_at':data.get('last_run_at'),
            'last_result':data.get('last_result'),'interval_hours':AUTO_INTERVAL//3600}


def set_auto(env,enabled,days):
    if not isinstance(enabled,bool):raise ValueError('开关只能是开或关')
    if type(days) is not int or days not in (15,30):raise ValueError('只支持15天或30天闲置阈值')
    with env.bridge_state.operation_lock(env.BRIDGE/'operation.lock'):
        data=env.bridge_state.load_json(_auto_path(env),{}) or {}
        data.update(enabled=enabled,days=days,changed_at=time.time())
        env.bridge_state.atomic_json(_auto_path(env),data)
    env.log('subagent-auto enabled='+str(enabled).lower()+' days='+str(days))
    return auto_settings(env)


def auto_tick(env,now=None):
    """开关打开、距上次超过 AUTO_INTERVAL 才跑。跑法和手动完全一样：预览 → 逐条核验 → 备份 → 移入回收站（可恢复）。"""
    settings=auto_settings(env);now=time.time() if now is None else now
    if not settings['enabled']:return None
    if settings['last_run_at'] and now-settings['last_run_at']<AUTO_INTERVAL:return None
    if getattr(env,'_attention',[]):return None
    try:
        p=preview(env,settings['days'],'all',background=False,target='subagent')
        if p['status']!='ready':result={'status':'failed','error':p.get('error') or '预览失败','deleted':0,'skipped':0,'errors':0}
        elif not p['items']:result={'status':'completed','deleted':0,'skipped':len(p['skipped']),'errors':0,'plan_id':p['plan_id']}
        else:
            done=commit(env,p['plan_id'],True,[item['item_id'] for item in p['items']],background=False)
            result={'status':done['status'],'deleted':len(done.get('deleted',[])),'skipped':len(done.get('skipped',[])),
                    'errors':len(done.get('errors',[])),'plan_id':p['plan_id']}
    except Exception as exc:
        result={'status':'failed','error':type(exc).__name__+': '+str(exc)[:200],'deleted':0,'skipped':0,'errors':1}
    with env.bridge_state.operation_lock(env.BRIDGE/'operation.lock'):
        data=env.bridge_state.load_json(_auto_path(env),{}) or {}
        data.update(last_run_at=now,last_result={**result,'days':settings['days'],'at':now})
        env.bridge_state.atomic_json(_auto_path(env),data)
    env.log('subagent-auto-run status='+result['status']+' deleted='+str(result['deleted'])+' skipped='+str(result['skipped']))
    return result
