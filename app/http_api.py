"""Authenticated loopback HTTP API for the desktop bridge."""
import hmac
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

def make_handler(env):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def _send(self,code,body,ctype='application/json; charset=utf-8'):
            raw=body.encode() if isinstance(body,str) else body
            self.send_response(code); self.send_header('Content-Type',ctype)
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store'); self.send_header('Referrer-Policy','no-referrer'); self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            self.end_headers(); self.wfile.write(raw)
        def _json(self,data,code=200): self._send(code,json.dumps(data,ensure_ascii=False))
        def _check(self,allow_query=False):
            host=self.headers.get('Host','')
            if host not in {f'127.0.0.1:{env.PORT}',f'localhost:{env.PORT}'}:
                raise PermissionError('Host 校验失败')
            origin=self.headers.get('Origin')
            if origin and origin not in {f'http://127.0.0.1:{env.PORT}',f'http://localhost:{env.PORT}'}:
                raise PermissionError('来源校验失败')
            if self.headers.get('Sec-Fetch-Site') in ('cross-site',):
                raise PermissionError('拒绝跨站请求')
            token=self.headers.get('X-Bridge-Token','')
            if allow_query:
                token=token or parse_qs(urlparse(self.path).query).get('token',[''])[0]
            if not hmac.compare_digest(token,env.API_TOKEN):
                raise PermissionError('请通过会话桥 App 打开此页面。')
        def _error(self,error):
            confirm=getattr(error,'confirm',None)
            if isinstance(error,PermissionError): code=403
            elif confirm: code=409
            elif isinstance(error,(ValueError,FileNotFoundError)): code=400
            else: code=500
            env.log('request-failed path='+urlparse(self.path).path+' type='+type(error).__name__)
            body={'error':str(error)}
            if confirm: body.update(confirm=confirm,detail=getattr(error,'detail',None))
            self._json(body,code)
        def do_GET(self):
            u=urlparse(self.path); q=parse_qs(u.query)
            try:
                if u.path.startswith('/assets/icons/'):
                    # 工具图标：从本机装好的 App 里提取（不随代码分发），没有就给字母徽标；不含账号和会话内容。
                    import re
                    name=u.path.removeprefix('/assets/icons/')
                    if not re.fullmatch(r'(claude|codex|zcode|workbuddy|kimi|openai|codex-light|glm|minimax|gemini|cc-switch)\.png', name):
                        raise ValueError('不存在的图标')
                    if self.headers.get('Host','') not in {f'127.0.0.1:{env.PORT}', f'localhost:{env.PORT}'}:
                        raise PermissionError('Host 校验失败')
                    path=env.APP_DIR/'assets/icons'/name
                    if path.is_file(): self._send(200,path.read_bytes(),'image/png');return
                    self._send(200,env.brand_icons.badge_svg(name.removesuffix('.png')),'image/svg+xml; charset=utf-8');return
                if u.path=='/api/health':
                    self._json({'product':'session-bridge','version':env.VERSION,'instance':env.INSTANCE_ID}); return
                self._check(allow_query=u.path in ('/','/index.html','/mtoken-panel','/mtoken-panel.html'))
                if u.path in ('/','/index.html','/mtoken-panel','/mtoken-panel.html'):
                    name='mtoken-panel.html' if u.path.startswith('/mtoken-panel') else 'index.html'
                    html=(env.APP_DIR/name).read_text()
                    boot='<script>window.BRIDGE_TOKEN='+json.dumps(env.API_TOKEN)+';const bridgeFetch=window.fetch.bind(window);window.fetch=(input,options={})=>{const url=new URL(typeof input==="string"?input:input.url,location.href);const h=new Headers(options.headers||{});if(url.origin===location.origin&&url.pathname.startsWith("/api/"))h.set("X-Bridge-Token",window.BRIDGE_TOKEN);return bridgeFetch(input,{...options,headers:h});};history.replaceState(null,"",location.pathname);</script>'
                    self._send(200,html.replace('<head>','<head>'+boot,1),'text/html; charset=utf-8')
                elif u.path=='/api/sessions':
                    sessions=env.collect_sessions(q.get('refresh',['0'])[0]=='1')
                    days=int(q.get('days',['0'])[0]); minimum=float(q.get('min_mb',['0'])[0]); tool=q.get('tool',['all'])[0]; kw=q.get('q',[''])[0].strip().lower()
                    if days<0 or minimum<0: raise ValueError('筛选值必须非负')
                    now=env.time.time()
                    out=[s for s in sessions if (tool=='all' or s['tool']==tool) and (not days or s['mtime']>=now-days*86400) and s['size']>=minimum*1048576 and (not kw or kw in (s['title']+s['dir']).lower())]
                    counts={t:sum(s['tool']==t for s in sessions) for t in env.TOOLS}; counts['all']=len(sessions)
                    self._json({'sessions':out,'all_counts':counts,'capabilities':env.capabilities(),'scan_status':env.scan_status()})
                elif u.path=='/api/cleanup/plan': self._json(env.bulk_cleanup.get_plan(env,q.get('plan_id',[''])[0]))
                elif u.path=='/api/cleanup/subagent-auto': self._json(env.bulk_cleanup.auto_settings(env))
                elif u.path=='/api/session':
                    self._json(env.session_reader.cached_detail(env,q.get('tool',[''])[0],q.get('src',[''])[0],int(q.get('offset',['0'])[0]),int(q.get('limit',['100'])[0])))
                elif u.path=='/api/usage': self._json(env.usage_backend.read_snapshot(env.MT_CACHE))
                elif u.path=='/api/keystatus': self._json(env.usage_backend.get_provider_status())
                elif u.path=='/api/usage/providers': self._json({'providers':env.usage_backend.get_provider_status()})
                elif u.path=='/api/trash': self._json({'items':env.bridge_ops.list_trash(env)})
                elif u.path in ('/static/accounts.js','/static/accounts.css','/static/transfer.js','/static/transfer.css','/static/update.js','/static/update.css'):
                    # 双账号、导出导入、检查更新的前端单独成文件，和接口一样要 token 头，不收 URL 里的 token。
                    name=u.path.removeprefix('/static/')
                    self._send(200,(env.APP_DIR/name).read_bytes(),'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/css; charset=utf-8')
                elif u.path=='/api/plugins': self._json({'plugins':env.plugins.public()})
                elif u.path.startswith('/plugins/'):
                    # 插件页面的脚本和样式：只给 plugin.json 里登记过的文件，同样要 token 头。
                    parts=u.path.split('/')
                    try:
                        if len(parts)!=4: raise FileNotFoundError('不存在的文件')
                        data,ctype=env.plugins.asset(parts[2],parts[3])
                    except (FileNotFoundError,ValueError): self._json({'error':'不存在的文件'},404); return
                    self._send(200,data,ctype)
                elif u.path.startswith('/api/accounts/'): self._json(env.accounts.handle_get(u.path,q))
                elif u.path.startswith('/api/transfer/'): self._json(env.transfer.handle_get(u.path,q))
                elif u.path.startswith('/api/update/'): self._json(env.updates.handle_get(u.path,q))
                else:
                    routed=env.plugins.dispatch_get(u.path,q)
                    if routed is None: self._json({'error':'不存在的接口'},404)
                    elif routed[0]=='file': self._send(200,routed[1],routed[2])
                    else: self._json(routed[1])
            except Exception as e: self._error(e)
        def do_POST(self):
            u=urlparse(self.path)
            try:
                self._check()
                self.connection.settimeout(15)
                if self.headers.get('Transfer-Encoding'): raise ValueError('不支持分块请求体')
                lengths=self.headers.get_all('Content-Length',[])
                if len(lengths)!=1: raise ValueError('请求长度无效')
                n=int(lengths[0])
                # 导出可以一次选几百条会话，这一个接口放宽到 1 MB。
                if n<0 or n>(1048576 if u.path=='/api/transfer/export' else 65536): raise ValueError('请求体超出限制')
                if 'application/json' not in self.headers.get('Content-Type',''): raise ValueError('必须使用 JSON')
                raw=self.rfile.read(n)
                if len(raw)!=n: raise ValueError('请求体未完整传输')
                body=json.loads(raw or b'{}')
                if not isinstance(body,dict): raise ValueError('请求体必须为对象')
                if u.path=='/api/sync-one': result=env.sync_one(body.get('tool',''),body.get('src',''),body.get('target',''))
                elif u.path=='/api/cleanup/preview': result=env.bulk_cleanup.preview(env,body.get('days'),body.get('tool','all'),target=body.get('target','main'))
                elif u.path=='/api/cleanup/subagent-auto': result=env.bulk_cleanup.set_auto(env,body.get('enabled'),body.get('days'))
                elif u.path=='/api/cleanup/commit': result=env.bulk_cleanup.commit(env,body.get('plan_id'),body.get('confirm'),body.get('selected_ids'))
                elif u.path=='/api/delete': result=env.delete_session(body.get('tool',''),body.get('src',''))
                elif u.path=='/api/restore': result=env.bridge_ops.restore_session(env,body.get('trash_id','')); env.invalidate()
                elif u.path=='/api/usage/refresh': result=env.usage_backend.refresh_snapshot(env.MT_CACHE,force=body.get('force',True))
                elif u.path=='/api/keys': result=env.usage_backend.set_provider_keys(body)
                elif u.path=='/api/usage/keys': result=env.usage_backend.set_provider_keys(body.get('keys',{}))
                elif u.path=='/api/window/open': env.show_main(); result={'ok':True}
                elif u.path=='/api/shutdown': env.request_shutdown(); result={'ok':True}
                elif u.path.startswith('/api/accounts/'): result=env.accounts.handle_post(u.path,body)
                elif u.path.startswith('/api/transfer/'): result=env.transfer.handle_post(u.path,body)
                elif u.path.startswith('/api/update/'): result=env.updates.handle_post(u.path,body)
                else:
                    routed=env.plugins.dispatch_post(u.path,body)
                    if routed is None: self._json({'error':'不存在的接口'},404); return
                    result=routed[1]
                self._json(result)
            except Exception as e: self._error(e)
    return Handler
