"""灵桥 3.4 · 会话导出 / 导入：在两台电脑的灵桥之间搬会话。

导出：选好会话，打成一个 .zip。每条会话里放两样东西：
  - 原始材料：Claude Code 的聊天记录文件、会话文件夹（工具输出、子代理记录、自定义标题）和桌面版侧栏条目；
    Codex 的 rollout 文件、索引行和两个库里属于这条会话的记录；ZCode、WorkBuddy 库里属于这条会话的记录；
  - 整理好的文字对话（只有人和 AI 的正文），原样放不回去时用它新建。
导入：先只读预览，逐条看能不能原样放回去：
  - 能原样就原样：ID 不变，文件按这台电脑的规则放好，库记录原样插进去（表结构必须和导出那台一致）；
  - 不行就转文字：工具版本不同、关联的主记录这台没有、这台没装那个工具……就用文字对话新建一条；
  - 这台已经有同一条会话的跳过，不覆盖。
每条导入都是一次可恢复操作（.bridge/operations），出错当场撤回；整次导入可以撤销（导入的会话进回收站）。
往 Claude 桌面版侧栏写条目前，桌面版必须完全退出（不然它会把改动覆盖回去）。

接口不收文件路径：压缩包的位置只来自本机的打开/保存对话框，或灵桥自己列出的候选文件，在服务端换成一次性的编号。
压缩包是另一台电脑来的数据：清单逐项校验，只读清单上列出的条目，大小和 SHA-256 都要对上，写入位置一律在这台电脑上重新算。
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
import zipfile

import bridge_ops
import bridge_state
import platform_paths

FORMAT = 'lingqiao-session-transfer'
FORMAT_VERSION = 1
TOOLS = ('claude', 'codex', 'zcode', 'workbuddy')
TOOL_NAMES = {'claude': 'Claude Code', 'codex': 'Codex', 'zcode': 'ZCode', 'workbuddy': 'WorkBuddy'}
DB_FILES = {'codex': ('codex_state', 'codex_history'), 'zcode': ('zcode',), 'workbuddy': ('workbuddy',), 'claude': ()}
MAX_SESSIONS = 1000
MAX_TOTAL = 16 << 30         # 一个压缩包解开后最多 16 GB
MAX_ENTRY = 4 << 30          # 单个文件最多 4 GB
MAX_ZIP = 16 << 30
MAX_SESSION_FILES = 20000
MAX_MANIFEST = 32 << 20
MAX_SMALL = 4 << 20          # 侧栏条目、库的表头
MAX_RATIO = 1000             # 压缩比上限，防压缩炸弹
MAX_INDEX_LINE = 64 << 10
PLAN_TTL = 2 * 3600
HANDLE_TTL = 2 * 3600
RUNS_KEPT = 100
CHUNK = 1 << 20
ID_RE = {'claude': re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,99}\Z'), 'codex': re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,99}\Z'),
         'workbuddy': re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,99}\Z'), 'zcode': re.compile(r'sess_[A-Za-z0-9_-]{8,100}\Z')}
ARC_RE = re.compile(r'sessions/\d{4}/(?:turns\.jsonl|raw/(?:transcript\.jsonl|rollout\.jsonl|sidebar\.json'
                    r'|db-(?:codex_state|codex_history|zcode|workbuddy)\.jsonl|files/\d{5}))\Z')
CODEX_REL_RE = re.compile(r'\d{4}/\d{2}/\d{2}/rollout-[A-Za-z0-9T:._-]{1,200}\.jsonl\Z')
RUN_RE = re.compile(r'\d{8}-\d{6}-[0-9a-f]{6}\Z')
# 每张表里哪一列说明“这行属于这条会话”；没列在这里的表不带。
OWNER = {'zcode': {'session': 'id', 'message': 'session_id', 'part': 'session_id', 'session_entry': 'session_id',
                   'model_usage': 'session_id', 'session_input': 'session_id', 'session_target': 'session_id',
                   'todo': 'session_id', 'tool_usage': 'session_id', 'turn_usage': 'session_id',
                   'input_history': 'session_id', 'session_task_link': 'child_session_id', 'dwf_actor': 'session_id'},
         'codex_state': {'threads': 'id', 'thread_dynamic_tools': 'thread_id', 'thread_attachments': 'thread_id',
                         'thread_spawn_edges': 'child_thread_id'},
         'codex_history': {'thread_items': 'thread_id', 'thread_turns': 'thread_id',
                           'thread_history_projection_state': 'thread_id', 'thread_realtime_items': 'thread_id'},
         'workbuddy': {'sessions': 'id', 'session_usage': 'session_id', 'automation_runs': 'thread_id'}}
MAIN_TABLE = {'zcode': 'session', 'codex_state': 'threads', 'workbuddy': 'sessions'}
# 只数“像密钥”的片段，不记录内容。
SECRET_RE = re.compile(r'sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}'
                       r'|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abposr]-[A-Za-z0-9-]{10,}'
                       r'|-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')
README = """这是灵桥（AI 会话工作台）导出的会话压缩包。

来源电脑：{machine}
导出时间：{at}
会话：{count} 条（{tools}）

怎么用：在另一台电脑的灵桥里，左侧「数据 → 导出 / 导入」→「导入」→ 选这个文件。
能原样放回去的原样放回去；对方电脑上工具版本不同、放不回去的，转成文字对话导入。

注意：里面是完整的聊天记录，可能有代码、文件内容，甚至密钥。只在自己的电脑之间传，别发给别人。
"""


class Job(dict):
    pass


def now_text(ts=None):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts or time.time()))


def _zinfo(name, mtime=None):
    stamp = time.localtime(max(mtime or time.time(), 315532800))[:6]
    info = zipfile.ZipInfo(name, date_time=stamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


class Secrets:
    """数像密钥的片段（按行），只留个数。"""

    def __init__(self):
        self.count = 0
        self._tail = ''

    def feed(self, text):
        data = self._tail + text
        cut = data.rfind('\n')
        if cut < 0:
            self._tail = data[-200000:]
            return
        self.count += len(SECRET_RE.findall(data[:cut]))
        self._tail = data[cut + 1:][-200000:]

    def feed_value(self, value):
        if isinstance(value, str) and len(value) > 20:
            self.count += len(SECRET_RE.findall(value))

    def close(self):
        if self._tail:
            self.count += len(SECRET_RE.findall(self._tail))
            self._tail = ''
        return self.count


class HashWriter:
    def __init__(self, stream):
        self.stream, self.hash, self.size = stream, hashlib.sha256(), 0

    def write(self, data):
        self.hash.update(data)
        self.size += len(data)
        self.stream.write(data)


def _safe_rel(rel, depth=8):
    if not isinstance(rel, str) or not rel or len(rel.encode()) > 1024 or rel.startswith('/'):
        return None
    parts = rel.split('/')
    if len(parts) > depth:
        return None
    for part in parts:
        if not part or part in ('.', '..') or len(part.encode()) > 255 or '\\' in part or any(ord(ch) < 32 for ch in part):
            return None
    return rel


def _plain_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    return isinstance(value, dict) and set(value) == {'__blob__'} and isinstance(value['__blob__'], str)


class SessionTransfer:
    def __init__(self, env):
        self.env = env
        self._lock = threading.RLock()
        self._handles = {}
        self._plans = {}
        self._jobs = {}
        self._job = None
        self._machine = None

    # ------------------------------------------------------------ 基础
    def state_dir(self, *parts):
        path = Path(self.env.BRIDGE) / 'transfer'
        for part in parts:
            path = path / part
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def config(self):
        data = bridge_state.load_json(Path(self.env.CONFIG), {}) or {}
        section = data.get('transfer') if isinstance(data.get('transfer'), dict) else {}
        return {'export_dir': section.get('export_dir') if isinstance(section.get('export_dir'), str) else '',
                'extra_dirs': [d for d in section.get('import_dirs', []) if isinstance(d, str)][:5] if isinstance(section.get('import_dirs'), list) else []}

    def machine(self):
        if self._machine:
            return self._machine
        path = self.state_dir() / 'machine.json'
        data = bridge_state.load_json(path, None)
        if not isinstance(data, dict) or not isinstance(data.get('id'), str) or not re.fullmatch(r'[0-9a-f-]{36}', data['id']):
            data = {'id': str(uuid.uuid4()), 'created': time.time()}
            bridge_state.atomic_json(path, data)
        name = ''
        try:
            name = subprocess.run(['/usr/sbin/scutil', '--get', 'ComputerName'], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        self._machine = {'id': data['id'], 'name': (name or socket.gethostname() or '这台电脑')[:80]}
        return self._machine

    def default_export_dir(self):
        configured = self.config()['export_dir']
        for candidate in ([Path(configured)] if configured else []) + [Path(self.env.HOME) / 'Downloads', Path(self.env.HOME)]:
            if candidate.is_dir():
                return candidate
        return Path(self.env.HOME)

    def default_name(self, count):
        machine = re.sub(r'[^\w-]+', '-', self.machine()['name']).strip('-')[:30] or 'mac'
        return f'灵桥会话导出_{machine}_{time.strftime("%Y%m%d-%H%M")}_{int(count)}条.zip'

    def tool_status(self):
        out = {}
        for tool in TOOLS:
            try:
                reason = self.env.target_reason(tool)
            except Exception as exc:  # noqa: BLE001  某个工具的库读不了，不影响别的
                reason = '读不了（' + type(exc).__name__ + '）'
            out[tool] = {'name': TOOL_NAMES[tool], 'available': not reason, 'reason': reason}
        return out

    def claude_status(self):
        accounts = getattr(self.env, 'accounts', None)
        if accounts is None:
            return {'running': None, 'blocking': [], 'error': '没有进程检查'}
        return accounts.claude_status()

    def _require_claude_quit(self):
        status = self.claude_status()
        if status.get('running') is None:
            raise ValueError('没法确认 Claude 桌面版有没有退出（' + str(status.get('error') or '') + '），这次先不写 Claude 的侧栏条目')
        if status['running']:
            raise ValueError(f'要往 Claude 桌面版侧栏里加条目：先{platform_paths.QUIT_CLAUDE}，再回来导入')

    def _attention(self):
        if getattr(self.env, '_attention', None):
            raise ValueError('有没处理完的数据操作（回收站里能看到原因），先停止新写入')

    # ------------------------------------------------------------ 文件编号（接口不收路径）
    def _forbidden(self, path):
        home = Path(self.env.HOME)
        roots = [home / '.claude', home / '.codex', home / '.zcode', home / '.workbuddy', Path(self.env.BRIDGE),
                 home / 'Library', Path(self.env.CC_ROOT), Path(self.env.CX_ROOT), Path(self.env.WB_ROOT)]
        p = path.resolve()
        return any(p == r.resolve() or p.is_relative_to(r.resolve()) for r in roots if r)

    def register(self, kind, path):
        if kind not in ('export', 'import'):
            raise ValueError('未知用途')
        p = Path(str(path)).expanduser()
        if not p.is_absolute():
            raise ValueError('要用完整路径')
        if kind == 'export':
            if p.suffix.lower() != '.zip':
                p = p.with_name(p.name + '.zip')
            if not p.parent.is_dir():
                raise ValueError('保存的文件夹不存在')
            if p.exists() or p.is_symlink():
                raise ValueError('这个文件名已经有了：换个名字，灵桥不覆盖已有文件')
            if self._forbidden(p.parent):
                raise ValueError('别把压缩包存在工具自己的数据目录里，换个地方（比如“下载”）')
            info = {'name': p.name, 'dir': str(p.parent)}
        else:
            if p.is_symlink() or not p.is_file():
                raise ValueError('找不到这个文件')
            st = p.stat()
            if st.st_size > MAX_ZIP:
                raise ValueError('压缩包太大（超过 16 GB）')
            if not zipfile.is_zipfile(p):
                raise ValueError('这不是 zip 压缩包')
            info = {'name': p.name, 'dir': str(p.parent), 'size': st.st_size, 'mtime': st.st_mtime}
        handle = secrets.token_urlsafe(12)
        with self._lock:
            now = time.time()
            for key in [k for k, v in self._handles.items() if now - v['at'] > HANDLE_TTL]:
                self._handles.pop(key, None)
            self._handles[handle] = {'kind': kind, 'path': str(p), 'at': now}
        return {'handle': handle, **info}

    def _take(self, handle, kind):
        with self._lock:
            item = self._handles.get(handle) if isinstance(handle, str) else None
            if not item or item['kind'] != kind or time.time() - item['at'] > HANDLE_TTL:
                raise ValueError('文件选择过期了，请重新选一次')
            return Path(item['path'])

    def choose_export(self, window, count=0):
        import webview
        result = window.create_file_dialog(webview.FileDialog.SAVE, directory=str(self.default_export_dir()),
                                           save_filename=self.default_name(count))
        if not result:
            return {'cancelled': True}
        return self.register('export', result if isinstance(result, str) else result[0])

    def choose_import(self, window):
        import webview
        result = window.create_file_dialog(webview.FileDialog.OPEN, directory=str(self.default_export_dir()),
                                           allow_multiple=False, file_types=('灵桥会话压缩包 (*.zip)',))
        if not result:
            return {'cancelled': True}
        return self.register('import', result if isinstance(result, str) else result[0])

    def candidates(self):
        """下载、桌面（和配置里的文件夹）里最近的灵桥会话压缩包。"""
        home = Path(self.env.HOME)
        dirs = [home / 'Downloads', home / 'Desktop'] + [Path(d) for d in self.config()['extra_dirs']]
        found = []
        for d in dirs:
            try:
                entries = sorted(d.glob('*.zip'), key=lambda p: -p.stat().st_mtime)[:60] if d.is_dir() else []
            except OSError:
                continue
            for p in entries:
                try:
                    if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_ZIP:
                        continue
                    with zipfile.ZipFile(p) as zf:
                        if 'manifest.json' not in zf.namelist():
                            continue
                        info = zf.getinfo('manifest.json')
                        if info.file_size > MAX_MANIFEST:
                            continue
                        head = json.loads(zf.read('manifest.json'))
                    if not isinstance(head, dict) or head.get('format') != FORMAT:
                        continue
                    source = head.get('source') if isinstance(head.get('source'), dict) else {}
                    found.append((p.stat().st_mtime, p, str(source.get('machine') or '')[:80], len(head.get('sessions') or [])))
                except (OSError, ValueError, zipfile.BadZipFile, KeyError):
                    continue
        found.sort(key=lambda x: -x[0])
        out = []
        for mtime, p, machine, count in found[:10]:
            item = self.register('import', p)
            out.append({**item, 'machine': machine, 'sessions': count, 'at': now_text(mtime)})
        return out

    # ------------------------------------------------------------ 后台任务
    def _start(self, kind, fn, args, background=True):
        with self._lock:
            if self._job is not None and self._job['status'] == 'running':
                raise ValueError('上一个导出或导入还没做完，等它结束再来')
            job = Job(id=time.strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(3), kind=kind, status='running',
                      started=time.time(), progress={'done': 0, 'total': 0, 'title': '', 'bytes': 0}, result=None, error='')
            self._job = job
            self._jobs[job['id']] = job
            for key in sorted(self._jobs)[:-20]:
                self._jobs.pop(key, None)
        if background:
            threading.Thread(target=self._main, args=(job, fn, args), daemon=True).start()
        else:
            self._main(job, fn, args)
        return job

    def _main(self, job, fn, args):
        try:
            job['result'] = fn(job, *args)
            job['status'] = 'done'
        except Exception as exc:  # noqa: BLE001  原因交给页面显示
            job['status'] = 'failed'
            job['error'] = str(exc) or type(exc).__name__
            self.env.log('transfer-' + job['kind'] + '-failed type=' + type(exc).__name__)
        finally:
            job['finished'] = time.time()

    def job(self, job_id):
        job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if not job:
            raise ValueError('找不到这个任务（灵桥重启过？）')
        return dict(job)

    # ------------------------------------------------------------ 导出
    def start_export(self, body, background=True):
        items = body.get('items')
        if not isinstance(items, list) or not items:
            raise ValueError('先选要导出的会话')
        if len(items) > MAX_SESSIONS:
            raise ValueError(f'一次最多导出 {MAX_SESSIONS} 条')
        metas, seen = [], set()
        for item in items:
            if not isinstance(item, dict) or item.get('tool') not in TOOLS or not isinstance(item.get('src'), str):
                raise ValueError('会话参数不对')
            key = (item['tool'], item['src'])
            if key in seen:
                continue
            seen.add(key)
            metas.append(self.env.find_meta(item['tool'], item['src']))
        handle = body.get('handle')
        if handle:
            dest = self._take(handle, 'export')
            with self._lock:
                self._handles.pop(handle, None)
        else:
            dest = self.default_export_dir() / self.default_name(len(metas))
        if dest.exists() or dest.is_symlink():
            raise ValueError('这个文件名已经有了：换个名字，灵桥不覆盖已有文件')
        if not dest.parent.is_dir():
            raise ValueError('保存的文件夹不存在')
        need = sum(int(m.get('size') or 0) for m in metas)
        free = shutil.disk_usage(dest.parent).free
        if free < need // 2 + (128 << 20):
            raise ValueError('保存位置的空间可能不够（这些会话一共约 ' + str(round(need / 1048576)) + ' MB）')
        job = self._start('export', self._run_export, (metas, dest), background)
        return {'job_id': job['id'], 'job': self.public_job(job)}

    def _run_export(self, job, metas, dest):
        machine = self.machine()
        tmp = dest.with_name('.' + dest.name + '.' + secrets.token_hex(4) + '.part')
        sessions, skipped, secret_total = [], [], 0
        job['progress'].update(total=len(metas))
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6) as zf:
                for i, meta in enumerate(metas):
                    job['progress'].update(done=i, title=meta.get('title', ''))
                    try:
                        entry = self._export_session(zf, len(sessions) + 1, meta, job)
                    except Exception as exc:  # noqa: BLE001  一条出问题不影响别的
                        skipped.append({'tool': meta['tool'], 'title': meta.get('title', ''), 'reason': str(exc) or type(exc).__name__})
                        continue
                    sessions.append(entry)
                    secret_total += entry['secrets']
                job['progress'].update(done=len(metas), title='')
                if not sessions:
                    raise ValueError('一条都没导出来：' + (skipped[0]['reason'] if skipped else '没有会话'))
                by_tool = {t: sum(1 for s in sessions if s['tool'] == t) for t in TOOLS}
                manifest = {'format': FORMAT, 'version': FORMAT_VERSION, 'export_id': str(uuid.uuid4()),
                            'created_at': now_text(), 'created_ts': time.time(),
                            'source': {'machine': machine['name'], 'machine_id': machine['id'], 'home': str(self.env.HOME),
                                       'lingqiao': self.env.VERSION},
                            'sessions': sessions, 'skipped': skipped,
                            'totals': {'sessions': len(sessions), 'by_tool': by_tool, 'secrets': secret_total}}
                zf.writestr(_zinfo('manifest.json'), json.dumps(manifest, ensure_ascii=False, indent=1))
                tools = '、'.join(f'{TOOL_NAMES[t]} {n}' for t, n in by_tool.items() if n)
                zf.writestr(_zinfo('README.txt'), README.format(machine=machine['name'], at=manifest['created_at'], count=len(sessions), tools=tools))
            os.chmod(tmp, 0o600)
            with open(tmp, 'rb') as stream:
                os.fsync(stream.fileno())
            os.link(tmp, dest)
        except FileExistsError as exc:
            raise ValueError('这个文件名已经有了：换个名字，灵桥不覆盖已有文件') from exc
        finally:
            if tmp.exists():
                tmp.unlink()
        size = dest.stat().st_size
        run = {'id': time.strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(3), 'kind': 'export', 'at': now_text(), 'ts': time.time(),
               'path': str(dest), 'name': dest.name, 'size': size, 'machine': machine['name'],
               'sessions': len(sessions), 'by_tool': by_tool, 'secrets': secret_total,
               'secret_sessions': sum(1 for s in sessions if s['secrets']), 'skipped': skipped,
               'items': [{'tool': s['tool'], 'title': s['title'], 'size': s['size'], 'turns': s['turns']['count'],
                          'secrets': s['secrets']} for s in sessions]}
        self._save_run(run)
        self.env.log('transfer-export sessions=' + str(len(sessions)) + ' skipped=' + str(len(skipped)) + ' bytes=' + str(size))
        return self.public_run(run)

    def _export_session(self, zf, n, meta, job):
        env, tool, src = self.env, meta['tool'], meta['src']
        if meta.get('missing_file'):
            raise ValueError('缺少本地文字记录文件')
        base = f'sessions/{n:04d}/'
        found = Secrets()
        entry = {'n': n, 'tool': tool, 'title': str(meta.get('title') or '')[:500], 'cwd': str(meta.get('dir') or ''),
                 'mtime': float(meta.get('mtime') or 0), 'size': int(meta.get('size') or 0), 'kind': meta.get('kind') or 'user',
                 'archived': bool(meta.get('archived')), 'mirror': bool(meta.get('mirror')),
                 'mirror_from': meta.get('mirror_from') or '', 'raw': {'files': [], 'databases': [], 'index': []}, 'notes': []}
        if tool == 'claude':
            path = env.session_reader.validate(env, tool, src)
            sid = path.stem
            entry['raw']['project_dir'] = path.parent.name
            entry['raw']['files'].append(self._add_file(zf, base + 'raw/transcript.jsonl', path, 'transcript', found, job, jsonl=True))
            folder = path.parent / sid
            if folder.is_dir() and not folder.is_symlink():
                files = []
                for current, dirs, names in os.walk(folder, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(current, d)))
                    files.extend(Path(current) / n for n in sorted(names) if os.path.isfile(os.path.join(current, n))
                                 and not os.path.islink(os.path.join(current, n)))
                    if len(files) > MAX_SESSION_FILES:
                        break
                if len(files) > MAX_SESSION_FILES:
                    entry['notes'].append(f'会话文件夹里文件太多（{len(files)} 个），没带')
                else:
                    for k, p in enumerate(files):
                        rel = _safe_rel(p.relative_to(folder).as_posix())
                        if rel is None:
                            entry['notes'].append('有个文件名不安全，没带：' + p.name[:40])
                            continue
                        entry['raw']['files'].append(self._add_file(zf, base + f'raw/files/{k:05d}', p, 'session_file', found, job, rel=rel))
            sidebar = self._sidebar_for(sid)
            if sidebar is not None:
                zf.writestr(_zinfo(base + 'raw/sidebar.json'), sidebar)
                entry['raw']['files'].append({'role': 'sidebar', 'path': base + 'raw/sidebar.json', 'size': len(sidebar),
                                              'sha256': hashlib.sha256(sidebar).hexdigest()})
            else:
                entry['notes'].append('没找到 Claude 桌面版侧栏条目（可能只在命令行里用过）')
        elif tool == 'codex':
            path = env.session_reader.validate(env, tool, src)
            with path.open(encoding='utf-8') as stream:
                first = json.loads(stream.readline() or '{}')
            sid = (first.get('payload') or {}).get('id') if isinstance(first, dict) else None
            sid = sid if isinstance(sid, str) and sid else path.stem[-36:]
            archived_root = Path(env.HOME) / '.codex/archived_sessions'
            area = 'archived' if path.resolve().is_relative_to(archived_root.resolve()) else 'sessions'
            root = archived_root if area == 'archived' else Path(env.CX_ROOT)
            rel = path.resolve().relative_to(root.resolve()).as_posix()
            record = self._add_file(zf, base + 'raw/rollout.jsonl', path, 'rollout', found, job, jsonl=True)
            record.update(rel=rel, area=area)
            entry['raw']['files'].append(record)
            index = []
            for line in bridge_ops._index_rows(env, sid):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get('id') == sid and len(line) <= MAX_INDEX_LINE:
                    index.append(obj)
            entry['raw']['index'] = index
            for name, db in (('codex_state', env.CX_STATE), ('codex_history', env.CX_SQLITE)):
                if Path(db).is_file():
                    entry['raw']['databases'].append(self._add_db(zf, base + f'raw/db-{name}.jsonl', name, db, sid, 'codex', found))
        elif tool == 'zcode':
            sid = env.session_reader.validate(env, tool, src)
            entry['raw']['databases'].append(self._add_db(zf, base + 'raw/db-zcode.jsonl', 'zcode', env.Z_DB, sid, 'zcode', found))
        else:
            sid, path = bridge_ops._source(env, 'workbuddy', src)
            if path is not None:
                record = self._add_file(zf, base + 'raw/transcript.jsonl', path, 'transcript', found, job, jsonl=True)
                record['rel'] = path.resolve().relative_to(Path(env.WB_ROOT).resolve()).as_posix()
                entry['raw']['files'].append(record)
            if Path(env.WB_DB).is_file():
                entry['raw']['databases'].append(self._add_db(zf, base + 'raw/db-workbuddy.jsonl', 'workbuddy', env.WB_DB, sid, 'workbuddy', found))
        if not ID_RE[tool].match(sid):
            raise ValueError('会话 ID 格式不认识，没导出')
        entry['session_id'] = sid
        entry['turns'] = self._add_turns(zf, base + 'turns.jsonl', tool, src, found, entry)
        entry['secrets'] = found.close()
        return entry

    def _sidebar_for(self, sid):
        root = Path(getattr(self.env, 'CC_META_ROOT', platform_paths.claude_meta_root(self.env.HOME)))
        best, best_ts = None, -1
        if not root.is_dir():
            return None
        for p in root.glob('*/*/local_*.json'):
            try:
                if p.is_symlink() or p.stat().st_size > MAX_SMALL:
                    continue
                raw = p.read_bytes()
                data = json.loads(raw)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get('cliSessionId') == sid:
                ts = data.get('lastActivityAt') if isinstance(data.get('lastActivityAt'), (int, float)) else p.stat().st_mtime * 1000
                if ts > best_ts:
                    best, best_ts = raw, ts
        return best

    def _add_file(self, zf, arc, path, role, found, job, *, rel=None, jsonl=False):
        st = path.stat()
        limit = st.st_size
        if limit > MAX_ENTRY:
            raise ValueError('有个文件太大（超过 4 GB）：' + path.name)
        with path.open('rb') as src, zf.open(_zinfo(arc, st.st_mtime), 'w', force_zip64=limit > (1 << 30)) as raw_out:
            out = HashWriter(raw_out)
            remaining, pending = limit, b''
            while remaining > 0:
                chunk = src.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                if jsonl:
                    data = pending + chunk
                    cut = data.rfind(b'\n')
                    if cut >= 0:
                        out.write(data[:cut + 1])
                        found.feed(data[:cut + 1].decode('utf-8', 'ignore'))
                        pending = data[cut + 1:]
                    else:
                        pending = data
                else:
                    out.write(chunk)
                    found.feed(chunk.decode('utf-8', 'ignore'))
                job['progress']['bytes'] += len(chunk)
            if pending:
                # 正在写的会话：最后一行没写完就不带；写完了只是没换行的，照带。
                try:
                    json.loads(pending)
                    out.write(pending)
                    found.feed(pending.decode('utf-8', 'ignore') + '\n')
                except ValueError:
                    pass
        record = {'role': role, 'path': arc, 'size': out.size, 'sha256': out.hash.hexdigest(), 'mtime': st.st_mtime}
        if rel is not None:
            record['rel'] = rel
        return record

    def _add_db(self, zf, arc, name, db, sid, kind, found):
        snap = bridge_ops._db_snapshot(db, sid, kind, strict=False)
        tables = {t: spec for t, spec in snap['tables'].items() if spec['rows'] and t in OWNER[name]}
        header = {'db': name, 'sid': sid, 'kind': kind,
                  'tables': {t: {'schema': spec['schema'], 'columns': spec['columns'], 'pk': spec['pk'], 'fks': spec['fks'],
                                 'count': len(spec['rows'])} for t, spec in tables.items()}}
        with zf.open(_zinfo(arc), 'w', force_zip64=True) as raw_out:
            out = HashWriter(raw_out)
            out.write((json.dumps(header, ensure_ascii=False) + '\n').encode())
            for t, spec in tables.items():
                for row in spec['rows']:
                    for value in row:
                        found.feed_value(value)
                    out.write((json.dumps({'t': t, 'r': row}, ensure_ascii=False) + '\n').encode())
        return {'db': name, 'path': arc, 'size': out.size, 'sha256': out.hash.hexdigest(),
                'rows': {t: len(spec['rows']) for t, spec in tables.items()}}

    def _add_turns(self, zf, arc, tool, src, found, entry):
        count = chars = 0
        with zf.open(_zinfo(arc), 'w', force_zip64=True) as raw_out:
            out = HashWriter(raw_out)
            try:
                for turn in self.env.session_reader.iter_turns(self.env, tool, src):
                    out.write((json.dumps({'role': turn['role'], 'text': turn['text']}, ensure_ascii=False) + '\n').encode())
                    count += 1
                    chars += len(turn['text'])
            except (OSError, ValueError) as exc:
                entry['notes'].append('文字对话没读全（' + str(exc)[:80] + '），转文字导入可能不完整')
        return {'path': arc, 'count': count, 'chars': chars, 'size': out.size, 'sha256': out.hash.hexdigest()}

    # ------------------------------------------------------------ 导入：读清单（只读）
    def _member(self, zf, record, kinds=None):
        """清单里的一项：名字合规、在压缩包里、大小和压缩比都正常。"""
        if not isinstance(record, dict):
            raise ValueError('清单格式不对')
        arc = record.get('path')
        if not isinstance(arc, str) or not ARC_RE.match(arc):
            raise ValueError('清单里有不认识的条目')
        size, sha = record.get('size'), record.get('sha256')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_ENTRY:
            raise ValueError('清单里的大小不对')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError('清单里的校验值不对')
        try:
            info = zf.getinfo(arc)
        except KeyError as exc:
            raise ValueError('压缩包里少了文件（' + arc + '），可能没传完整') from exc
        if info.file_size != size:
            raise ValueError('压缩包里的文件大小和清单对不上（' + arc + '）')
        if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
            raise ValueError('压缩包里有异常的文件（压缩比太大）')
        return arc, size, sha

    def _read_manifest(self, zf):
        try:
            info = zf.getinfo('manifest.json')
        except KeyError as exc:
            raise ValueError('这不是灵桥导出的会话压缩包（没有 manifest.json）') from exc
        if info.file_size > MAX_MANIFEST:
            raise ValueError('清单太大')
        try:
            manifest = json.loads(zf.read('manifest.json'))
        except ValueError as exc:
            raise ValueError('清单读不了，压缩包可能坏了') from exc
        if not isinstance(manifest, dict) or manifest.get('format') != FORMAT:
            raise ValueError('这不是灵桥导出的会话压缩包')
        if manifest.get('version') != FORMAT_VERSION:
            raise ValueError('压缩包是新版本灵桥导出的（格式 ' + str(manifest.get('version')) + '），先把这台的灵桥升级')
        source = manifest.get('source')
        sessions = manifest.get('sessions')
        if not isinstance(source, dict) or not isinstance(sessions, list) or not sessions or len(sessions) > MAX_SESSIONS:
            raise ValueError('清单格式不对')
        total, seen_n, seen_id = 0, set(), set()
        for s in sessions:
            if not isinstance(s, dict) or s.get('tool') not in TOOLS:
                raise ValueError('清单里有不认识的工具')
            sid = s.get('session_id')
            if not isinstance(sid, str) or not ID_RE[s['tool']].match(sid):
                raise ValueError('清单里有不认识的会话 ID')
            n = s.get('n')
            if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > 9999 or n in seen_n or (s['tool'], sid) in seen_id:
                raise ValueError('清单里的编号不对')
            seen_n.add(n)
            seen_id.add((s['tool'], sid))
            for key in ('title', 'cwd'):
                if not isinstance(s.get(key), str) or len(s[key]) > 4096:
                    raise ValueError('清单里的标题或目录不对')
            if not isinstance(s.get('mtime'), (int, float)) or isinstance(s.get('mtime'), bool):
                raise ValueError('清单里的时间不对')
            raw = s.get('raw')
            if not isinstance(raw, dict) or not isinstance(raw.get('files'), list) or not isinstance(raw.get('databases'), list):
                raise ValueError('清单里的原始材料不对')
            if len(raw['files']) > MAX_SESSION_FILES + 4:
                raise ValueError('清单里文件太多')
            prefix = f'sessions/{n:04d}/'
            for record in raw['files'] + raw['databases'] + [s.get('turns')]:
                arc, size, _ = self._member(zf, record)
                if not arc.startswith(prefix):
                    raise ValueError('清单里的条目编号对不上')
                total += size
            if total > MAX_TOTAL:
                raise ValueError('压缩包解开后太大（超过 16 GB）')
        return manifest

    def _db_header(self, zf, record):
        arc, size, _ = self._member(zf, record)
        with zf.open(arc) as stream:
            line = stream.readline(MAX_SMALL + 1)
        if len(line) > MAX_SMALL or not line.endswith(b'\n'):
            raise ValueError('库记录的表头不对')
        header = json.loads(line)
        if not isinstance(header, dict) or header.get('db') != record.get('db') or not isinstance(header.get('tables'), dict):
            raise ValueError('库记录的表头不对')
        for name, spec in header['tables'].items():
            if not isinstance(spec, dict) or not isinstance(spec.get('schema'), (str, type(None))) or not isinstance(spec.get('columns'), list) \
                    or not all(isinstance(c, str) for c in spec['columns']) or not isinstance(spec.get('pk'), list) or not isinstance(spec.get('fks'), list):
                raise ValueError('库记录的表头不对')
        return header

    def _local_db(self, name):
        return {'codex_state': self.env.CX_STATE, 'codex_history': self.env.CX_SQLITE, 'zcode': self.env.Z_DB, 'workbuddy': self.env.WB_DB}[name]

    def _schema_problem(self, name, header):
        path = Path(self._local_db(name))
        if not path.is_file():
            return '这台电脑上没有' + {'codex_state': ' Codex', 'codex_history': ' Codex', 'zcode': ' ZCode', 'workbuddy': ' WorkBuddy'}[name] + ' 的库'
        with self.env.session_reader.connect(path) as c:
            current = bridge_ops._schema(c)
        for table, spec in header['tables'].items():
            if table not in OWNER[name]:
                continue
            here = current.get(table)
            if here is None:
                return '这台电脑的库里没有表 ' + table + '，多半是工具版本不同'
            if here['columns'] != spec['columns'] or (here['schema'] or '').strip() != (spec['schema'] or '').strip():
                return '表结构和导出那台电脑不一样（' + table + '），多半是工具版本不同'
        return ''

    def _local_user(self):
        path = Path(self.env.WB_DB)
        if not path.is_file():
            return None
        with self.env.session_reader.connect(path) as c:
            row = c.execute('SELECT user_id FROM sessions WHERE user_id IS NOT NULL ORDER BY last_activity_at DESC LIMIT 1').fetchone()
        return row[0] if row and row[0] else None

    def _codex_account(self):
        """这台 Codex 最近的会话都属于同一个账号时，返回它；导入的线程换成这个账号，不然列表里可能看不到。"""
        path = Path(self.env.CX_STATE)
        if not path.is_file():
            return None
        with self.env.session_reader.connect(path) as c:
            cols = {r[1] for r in c.execute('PRAGMA table_info(threads)')}
            if not {'creator_user_id', 'creator_account_id'} <= cols:
                return None
            rows = c.execute('SELECT DISTINCT creator_user_id, creator_account_id FROM threads WHERE creator_user_id IS NOT NULL '
                             'ORDER BY updated_at DESC LIMIT 50').fetchall()
        distinct = {tuple(r) for r in rows}
        return distinct.pop() if len(distinct) == 1 else None

    def _map_cwd(self, cwd, source_home):
        home = str(Path(self.env.HOME))
        if not cwd or cwd == '(未知)':
            return home, '原来的工作目录不知道，放在用户目录下'
        # 在导出那台的用户目录下：换成这台的用户目录（两台用户名不同的时候）。
        src_home = (source_home or '').rstrip('/')
        if src_home and src_home != home and (cwd == src_home or cwd.startswith(src_home + '/')):
            mapped = home + cwd[len(src_home):]
            if Path(mapped).is_dir():
                return mapped, '工作目录换成这台电脑上的 ' + mapped
            return mapped, '工作目录换成 ' + mapped + '（这台电脑上还没有这个文件夹）'
        if Path(cwd).is_dir():
            return cwd, ''
        return cwd, '这台电脑上没有这个文件夹，打开会话前先建好（或接上外接硬盘）'

    def _claude_target(self):
        """侧栏条目放哪个账号/组织目录：最近有会话活动的账号里，最近用的那个组织（同双账号功能）。"""
        accounts = getattr(self.env, 'accounts', None)
        if accounts is None:
            return None, '没有 Claude 账号信息'
        scan = accounts.scan()
        if not scan.get('available'):
            return None, '这台电脑没装 Claude 桌面版：只放聊天记录，侧栏条目不建'
        usable = [a for a in scan['accounts'] if any(o['sessions'] for o in a['orgs'])]
        if not usable:
            return None, 'Claude 桌面版里还没有会话：先在桌面版里新建一条，再导入才会出现在侧栏'
        account = max(usable, key=lambda a: a['newest'])
        org = accounts.target_org(account)
        return Path(accounts.meta_root()) / account['id'] / org['id'], ''

    def _registry_path(self):
        return self.state_dir() / 'imported.json'

    def _registry(self):
        data = bridge_state.load_json(self._registry_path(), {'version': 1, 'items': {}})
        if not isinstance(data, dict) or not isinstance(data.get('items'), dict):
            raise ValueError('导入记录损坏，先停止导入')
        return data

    def _still_there(self, record):
        tool, src = record.get('local_tool'), record.get('local_src')
        try:
            if tool == 'zcode':
                with self.env.session_reader.connect(self.env.Z_DB) as c:
                    return c.execute('SELECT 1 FROM session WHERE id=?', (str(src).removeprefix('zcode:'),)).fetchone() is not None
            return isinstance(src, str) and Path(src).is_file()
        except Exception:  # noqa: BLE001
            return False

    def inspect(self, body):
        path = self._take(body.get('handle'), 'import')
        st = path.stat()
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise ValueError('压缩包坏了，读不了') from exc
        with zf:
            manifest = self._read_manifest(zf)
            source = manifest['source']
            machine = self.machine()
            tools = self.tool_status()
            registry = self._registry()['items']
            claude_dir, claude_note = self._claude_target()
            items = []
            for s in sorted(manifest['sessions'], key=lambda x: x['n']):
                items.append(self._plan_item(zf, s, source, tools, registry, claude_dir, claude_note))
        plan_id = secrets.token_urlsafe(12)
        plan = {'id': plan_id, 'path': str(path), 'identity': [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns], 'at': time.time(),
                'manifest': manifest, 'items': items, 'claude_dir': str(claude_dir) if claude_dir else ''}
        with self._lock:
            now = time.time()
            for key in [k for k, v in self._plans.items() if now - v['at'] > PLAN_TTL]:
                self._plans.pop(key, None)
            self._plans[plan_id] = plan
        same = source.get('machine_id') == machine['id']
        return {'plan_id': plan_id, 'zip': path.name, 'size': st.st_size, 'source': {k: source.get(k) for k in ('machine', 'lingqiao')},
                'created_at': manifest.get('created_at'), 'same_machine': same, 'this_machine': machine['name'],
                'items': [self.public_item(i) for i in items], 'claude': self.claude_status(),
                'counts': {m: sum(1 for i in items if i['mode'] == m) for m in ('raw', 'text', 'skip')},
                'skipped_at_export': manifest.get('skipped') or []}

    def _plan_item(self, zf, s, source, tools, registry, claude_dir, claude_note):
        tool, sid = s['tool'], s['session_id']
        local_cwd, cwd_note = self._map_cwd(s['cwd'], source.get('home'))
        turns = s.get('turns') if isinstance(s.get('turns'), dict) else {}
        count = turns.get('count') if isinstance(turns.get('count'), int) else 0
        item = {'id': s['n'], 'tool': tool, 'session_id': sid, 'title': s['title'][:300], 'cwd': s['cwd'], 'local_cwd': local_cwd,
                'cwd_note': cwd_note, 'mtime': float(s['mtime']), 'size': sum(int(f.get('size') or 0) for f in s['raw']['files'] + s['raw']['databases']),
                'turns': count, 'secrets': s.get('secrets') if isinstance(s.get('secrets'), int) else 0,
                'kind': s.get('kind') if s.get('kind') in ('user', 'subagent') else 'user', 'archived': bool(s.get('archived')),
                'mirror': bool(s.get('mirror')), 'notes': [str(n)[:200] for n in (s.get('notes') or [])][:10],
                'raw_ok': False, 'raw_reason': '', 'sidebar': False, 'mode': 'skip', 'skip_reason': '', 'target': '',
                'text_targets': [t for t in TOOLS if tools[t]['available']] if count else []}
        if cwd_note:
            item['notes'].append(cwd_note)
        key = source.get('machine_id', '') + '|' + tool + '|' + sid
        previous = registry.get(key)
        if bridge_ops.import_exists(self.env, tool, sid):
            item['skip_reason'] = '这台电脑上已经有这条会话了（不覆盖）'
            return item
        if previous and self._still_there(previous):
            item['skip_reason'] = '之前已经导入过（在 ' + TOOL_NAMES.get(previous.get('local_tool'), '?') + ' 里）'
            return item
        item['raw_ok'], item['raw_reason'] = self._raw_check(zf, s, claude_dir, claude_note, item)
        if item['raw_ok']:
            item['mode'], item['target'] = 'raw', tool
        elif item['text_targets']:
            item['mode'] = 'text'
            item['target'] = tool if tool in item['text_targets'] else item['text_targets'][0]
        else:
            item['skip_reason'] = (item['raw_reason'] + '；' if item['raw_reason'] else '') + ('没有文字对话可转' if not count else '这台电脑上没有能接收的工具')
        return item

    def _raw_check(self, zf, s, claude_dir, claude_note, item):
        tool, raw = s['tool'], s['raw']
        roles = {f.get('role') for f in raw['files']}
        try:
            if tool == 'claude':
                if 'transcript' not in roles:
                    return False, '压缩包里没有原始聊天记录'
                if not Path(self.env.CC_ROOT).is_dir():
                    return False, '这台电脑上没用过 Claude Code'
                if claude_dir:
                    item['sidebar'] = True
                elif claude_note:
                    item['notes'].append(claude_note)
                for f in raw['files']:
                    if f.get('role') == 'session_file' and _safe_rel(f.get('rel')) is None:
                        return False, '会话文件夹里有不安全的文件名'
                return True, ''
            if tool == 'codex':
                rollout = [f for f in raw['files'] if f.get('role') == 'rollout']
                if not rollout:
                    return False, '压缩包里没有原始 rollout 文件'
                if rollout[0].get('area') != 'sessions':
                    return False, 'Codex 归档会话先转文字导入'
                if not isinstance(rollout[0].get('rel'), str) or not CODEX_REL_RE.match(rollout[0]['rel']):
                    return False, 'rollout 文件位置不认识'
                if not Path(self.env.CX_ROOT).is_dir():
                    return False, '这台电脑上没用过 Codex'
                names = {d.get('db') for d in raw['databases']}
                if names != {'codex_state', 'codex_history'}:
                    return False, '压缩包里没有 Codex 的库记录'
            elif tool == 'workbuddy':
                files = [f for f in raw['files'] if f.get('role') == 'transcript']
                if not files or _safe_rel(files[0].get('rel'), depth=3) is None or not files[0]['rel'].endswith('.jsonl'):
                    return False, '压缩包里没有原始聊天记录'
                if not Path(self.env.WB_ROOT).is_dir():
                    return False, '这台电脑上没用过 WorkBuddy'
                if not self._local_user():
                    return False, '这台的 WorkBuddy 还没有登录账号'
            if tool in ('codex', 'zcode', 'workbuddy'):
                dbs = {d.get('db'): d for d in raw['databases']}
                if set(dbs) != set(DB_FILES[tool]):
                    return False, '压缩包里没有' + TOOL_NAMES[tool] + '的库记录'
                for name in DB_FILES[tool]:
                    header = self._db_header(zf, dbs[name])
                    if header.get('sid') != s['session_id']:
                        return False, '库记录和会话对不上'
                    main = MAIN_TABLE.get(name)
                    if main and main not in header['tables']:
                        return False, '库记录里没有会话本身'
                    problem = self._schema_problem(name, header)
                    if problem:
                        return False, problem
            return True, ''
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            return False, str(exc) or type(exc).__name__

    @staticmethod
    def public_item(item):
        return {k: item[k] for k in ('id', 'tool', 'title', 'cwd', 'local_cwd', 'mtime', 'size', 'turns', 'secrets', 'kind', 'archived',
                                     'mirror', 'notes', 'raw_ok', 'raw_reason', 'sidebar', 'mode', 'skip_reason', 'target', 'text_targets')}

    # ------------------------------------------------------------ 导入：写
    def start_import(self, body, background=True):
        if body.get('confirm') is not True:
            raise ValueError('导入前要先看预览，再确认')
        self._attention()
        with self._lock:
            plan = self._plans.get(body.get('plan_id')) if isinstance(body.get('plan_id'), str) else None
        if not plan or time.time() - plan['at'] > PLAN_TTL:
            raise ValueError('导入预览过期了，请重新选一次压缩包')
        choices = body.get('items')
        if not isinstance(choices, list) or not choices:
            raise ValueError('先勾选要导入的会话')
        by_id = {i['id']: i for i in plan['items']}
        picked, seen = [], set()
        for choice in choices:
            if not isinstance(choice, dict) or choice.get('id') not in by_id or choice['id'] in seen:
                raise ValueError('选择的会话不对，请重新预览')
            seen.add(choice['id'])
            item = by_id[choice['id']]
            mode, target = choice.get('mode'), choice.get('target') or ''
            if mode == 'raw':
                if not item['raw_ok']:
                    raise ValueError('“' + item['title'][:30] + '”不能原样导入：' + item['raw_reason'])
                target = item['tool']
            elif mode == 'text':
                if target not in item['text_targets']:
                    raise ValueError('“' + item['title'][:30] + '”转文字只能导入到这台装了的工具里')
            else:
                raise ValueError('导入方式不对')
            picked.append((item, mode, target))
        st = Path(plan['path']).stat()
        if [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns] != plan['identity']:
            raise ValueError('压缩包在预览之后变了，请重新选一次')
        if any(self._writes_sidebar(plan, item, mode, target) for item, mode, target in picked):
            self._require_claude_quit()
        need = sum(item['size'] for item, _, _ in picked)
        if shutil.disk_usage(Path(self.env.HOME)).free < need + (512 << 20):
            raise ValueError('这台电脑的空间不够（这些会话解开后约 ' + str(round(need / 1048576)) + ' MB）')
        job = self._start('import', self._run_import, (plan, picked), background)
        return {'job_id': job['id'], 'job': self.public_job(job)}

    @staticmethod
    def _writes_sidebar(plan, item, mode, target):
        return (mode == 'raw' and item['tool'] == 'claude' and bool(plan['claude_dir'])) or (mode == 'text' and target == 'claude')

    def _run_import(self, job, plan, picked):
        manifest = plan['manifest']
        source = manifest['source']
        sessions = {s['n']: s for s in manifest['sessions']}
        run = {'id': time.strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(3), 'kind': 'import', 'at': now_text(), 'ts': time.time(),
               'zip': Path(plan['path']).name, 'source': {'machine': source.get('machine'), 'machine_id': source.get('machine_id')},
               'items': [], 'undone': None}
        job['progress'].update(total=len(picked))
        with zipfile.ZipFile(plan['path']) as zf:
            for i, (item, mode, target) in enumerate(picked):
                job['progress'].update(done=i, title=item['title'])
                base = {'id': item['id'], 'tool': item['tool'], 'session_id': item['session_id'], 'title': item['title'],
                        'mode': mode, 'target': target}
                try:
                    self._attention()
                    if self._writes_sidebar(plan, item, mode, target):
                        self._require_claude_quit()
                    s = sessions[item['id']]
                    if mode == 'raw':
                        try:
                            result = self._import_raw(zf, plan, s, item)
                        except bridge_ops.ImportIncompatible as exc:
                            fallback = item['tool'] if item['tool'] in item['text_targets'] else (item['text_targets'][0] if item['text_targets'] else '')
                            if not fallback or (fallback == 'claude' and self.claude_status().get('running') is not False):
                                raise
                            result = self._import_text(zf, s, item, fallback)
                            result['notes'].insert(0, '原样写不进去（' + str(exc) + '），已转成文字导入')
                    else:
                        result = self._import_text(zf, s, item, target)
                    run['items'].append({**base, 'status': 'imported', **result})
                    self._remember(source, item, result, run['id'])
                except bridge_ops.ImportConflict as exc:
                    run['items'].append({**base, 'status': 'skipped', 'reason': str(exc)})
                except Exception as exc:  # noqa: BLE001  一条失败不影响别的，原因写进结果
                    run['items'].append({**base, 'status': 'failed', 'reason': str(exc) or type(exc).__name__})
                    self.env.log('transfer-import-item-failed type=' + type(exc).__name__)
        job['progress'].update(done=len(picked), title='')
        self._save_run(run)
        try:
            self.env.invalidate()
        except Exception as exc:  # noqa: BLE001
            self.env.log('transfer-invalidate-failed type=' + type(exc).__name__)
        counts = self.run_counts(run)
        self.env.log('transfer-import imported=' + str(counts['imported']) + ' skipped=' + str(counts['skipped']) + ' failed=' + str(counts['failed']))
        return self.public_run(run)

    def _remember(self, source, item, result, run_id):
        path = self._registry_path()
        with bridge_state.file_lock(self.state_dir() / 'registry.lock'):
            data = self._registry()
            data['items'][source.get('machine_id', '') + '|' + item['tool'] + '|' + item['session_id']] = {
                'local_tool': result['local_tool'], 'local_src': result['local_src'], 'mode': result['mode'], 'run_id': run_id, 'at': time.time()}
            bridge_state.atomic_json(path, data)

    def _forget(self, keys):
        path = self._registry_path()
        with bridge_state.file_lock(self.state_dir() / 'registry.lock'):
            data = self._registry()
            for key in keys:
                data['items'].pop(key, None)
            bridge_state.atomic_json(path, data)

    @staticmethod
    def _writer(zf, arc, sha, size):
        def write(stream):
            h, n = hashlib.sha256(), 0
            with zf.open(arc) as src:
                for chunk in iter(lambda: src.read(CHUNK), b''):
                    n += len(chunk)
                    if n > size:
                        raise ValueError('压缩包里的文件比清单上大，可能被改过')
                    h.update(chunk)
                    stream.write(chunk)
            if n != size or h.hexdigest() != sha:
                raise ValueError('压缩包里的文件和清单对不上（可能没传完整或被改过）')
            return h.hexdigest()
        return write

    def _load_db(self, zf, record, name, sid):
        arc, size, sha = self._member(zf, record)
        h, n, header, tables = hashlib.sha256(), 0, None, {}
        with zf.open(arc) as stream:
            for line in stream:
                n += len(line)
                h.update(line)
                if header is None:
                    header = json.loads(line)
                    if not isinstance(header, dict) or header.get('db') != name or header.get('sid') != sid or not isinstance(header.get('tables'), dict):
                        raise ValueError('库记录的表头不对')
                    for table, spec in header['tables'].items():
                        tables[table] = {'schema': spec['schema'], 'columns': spec['columns'], 'pk': spec['pk'], 'fks': spec['fks'],
                                         'rows': [], 'count': spec.get('count')}
                    continue
                rec = json.loads(line)
                if not isinstance(rec, dict) or rec.get('t') not in tables or not isinstance(rec.get('r'), list):
                    raise ValueError('库记录格式不对')
                spec = tables[rec['t']]
                if len(rec['r']) != len(spec['columns']) or not all(_plain_value(v) for v in rec['r']):
                    raise ValueError('库记录格式不对')
                spec['rows'].append(rec['r'])
        if n != size or h.hexdigest() != sha:
            raise ValueError('压缩包里的库记录和清单对不上（可能没传完整或被改过）')
        out = {}
        for table, spec in tables.items():
            if spec.pop('count') != len(spec['rows']):
                raise ValueError('库记录条数对不上')
            owner = OWNER[name].get(table)
            if owner is None:
                continue
            if owner not in spec['columns']:
                raise ValueError('库记录格式不对')
            index = spec['columns'].index(owner)
            if any(row[index] != sid for row in spec['rows']):
                raise ValueError('库记录里混进了别的会话，没导入')
            out[table] = spec
        main = MAIN_TABLE.get(name)
        if main and len(out.get(main, {}).get('rows', [])) != 1:
            raise ValueError('库记录里没有会话本身')
        return out

    @staticmethod
    def _set(spec, row, column, value):
        if column in spec['columns']:
            row[spec['columns'].index(column)] = value

    @staticmethod
    def _get(spec, row, column):
        return row[spec['columns'].index(column)] if column in spec['columns'] else None

    def _import_raw(self, zf, plan, s, item):
        env, tool, sid = self.env, s['tool'], s['session_id']
        raw = s['raw']
        label = 'import:' + str(plan['manifest']['source'].get('machine_id', ''))[:36] + ':' + tool + ':' + sid
        files, databases, index_lines, extra, notes = [], [], [], [], []
        mapped = item['local_cwd'] != s['cwd']
        if tool == 'claude':
            project = Path(env.CC_ROOT) / bridge_ops.claude_project_dirname(item['local_cwd'])
            for f in raw['files']:
                if f.get('role') == 'transcript':
                    arc, size, sha = self._member(zf, f)
                    files.insert(0, {'dst': project / (sid + '.jsonl'), 'write': self._writer(zf, arc, sha, size), 'mtime': f.get('mtime')})
                elif f.get('role') == 'session_file':
                    rel = _safe_rel(f.get('rel'))
                    if rel is None:
                        raise bridge_ops.ImportIncompatible('会话文件夹里有不安全的文件名')
                    arc, size, sha = self._member(zf, f)
                    dst = project / sid / rel
                    if not dst.resolve().is_relative_to((project / sid).resolve()):
                        raise bridge_ops.ImportIncompatible('会话文件夹里有不安全的文件名')
                    files.append({'dst': dst, 'write': self._writer(zf, arc, sha, size), 'mtime': f.get('mtime')})
                    extra.append(str(dst))
            sidebar = [f for f in raw['files'] if f.get('role') == 'sidebar']
            if plan['claude_dir']:
                data = {}
                if sidebar:
                    arc, size, sha = self._member(zf, sidebar[0])
                    if size > MAX_SMALL:
                        raise ValueError('侧栏条目太大')
                    blob = zf.read(arc)
                    if hashlib.sha256(blob).hexdigest() != sha:
                        raise ValueError('压缩包里的侧栏条目和清单对不上')
                    loaded = json.loads(blob)
                    data = loaded if isinstance(loaded, dict) else {}
                ms = int(s['mtime'] * 1000)
                local = 'local_' + str(uuid.uuid4())
                data.update({'sessionId': local, 'cliSessionId': sid, 'cwd': item['local_cwd'], 'originCwd': item['local_cwd'],
                             'bridgeSessionIds': [], 'remoteControlAutoEligible': False})
                data.setdefault('title', item['title'])
                data.setdefault('createdAt', ms)
                data.setdefault('lastActivityAt', ms)
                data.setdefault('isArchived', False)
                data.setdefault('permissionMode', 'default')
                blob = json.dumps(data, ensure_ascii=False).encode('utf-8')
                files.append({'dst': Path(plan['claude_dir']) / (local + '.json'), 'write': self._bytes_writer(blob), 'mtime': None})
            src = str(project / (sid + '.jsonl'))
        elif tool == 'codex':
            rollout = [f for f in raw['files'] if f.get('role') == 'rollout'][0]
            arc, size, sha = self._member(zf, rollout)
            dst = Path(env.CX_ROOT) / rollout['rel']
            files.append({'dst': dst, 'write': self._writer(zf, arc, sha, size), 'mtime': rollout.get('mtime')})
            dbs = {d['db']: d for d in raw['databases']}
            state = self._load_db(zf, dbs['codex_state'], 'codex_state', sid)
            history = self._load_db(zf, dbs['codex_history'], 'codex_history', sid)
            threads = state['threads']
            row = threads['rows'][0]
            self._set(threads, row, 'rollout_path', str(dst))
            if mapped:
                self._set(threads, row, 'cwd', item['local_cwd'])
            account = self._codex_account()
            if account and (self._get(threads, row, 'creator_user_id'), self._get(threads, row, 'creator_account_id')) != account:
                self._set(threads, row, 'creator_user_id', account[0])
                self._set(threads, row, 'creator_account_id', account[1])
                notes.append('换成了这台 Codex 登录的账号')
            databases = [(env.CX_STATE, state), (env.CX_SQLITE, history)]
            for obj in raw.get('index') or []:
                if isinstance(obj, dict) and obj.get('id') == sid:
                    line = json.dumps(obj, ensure_ascii=False)
                    if len(line) <= MAX_INDEX_LINE:
                        index_lines.append(line)
            src = str(dst)
        elif tool == 'zcode':
            tables = self._load_db(zf, raw['databases'][0], 'zcode', sid)
            session = tables['session']
            row = session['rows'][0]
            if mapped:
                self._set(session, row, 'directory', item['local_cwd'])
                self._set(session, row, 'path', item['local_cwd'])
                self._set(session, row, 'project_id', self._zcode_project(item['local_cwd']))
            if self._get(session, row, 'parent_id'):
                notes.append('这是子会话：它的主会话没在这台电脑上的话，ZCode 里可能看不到')
            databases = [(env.Z_DB, tables)]
            src = 'zcode:' + sid
        else:
            transcript = [f for f in raw['files'] if f.get('role') == 'transcript'][0]
            arc, size, sha = self._member(zf, transcript)
            rel = _safe_rel(transcript['rel'], depth=3)
            dst = Path(env.WB_ROOT) / rel
            if not dst.resolve().is_relative_to(Path(env.WB_ROOT).resolve()):
                raise bridge_ops.ImportIncompatible('聊天记录位置不安全')
            files.append({'dst': dst, 'write': self._writer(zf, arc, sha, size), 'mtime': transcript.get('mtime')})
            tables = self._load_db(zf, [d for d in raw['databases'] if d.get('db') == 'workbuddy'][0], 'workbuddy', sid)
            sessions = tables['sessions']
            row = sessions['rows'][0]
            user = self._local_user()
            if not user:
                raise bridge_ops.ImportIncompatible('这台的 WorkBuddy 还没有登录账号')
            if self._get(sessions, row, 'user_id') != user:
                self._set(sessions, row, 'user_id', user)
                notes.append('换成了这台 WorkBuddy 登录的账号')
            if mapped:
                self._set(sessions, row, 'cwd', item['local_cwd'])
            databases = [(env.WB_DB, tables)]
            src = str(dst)
        result = bridge_ops.import_raw_session(env, tool, sid, label=label, files=files, databases=databases, index_lines=index_lines)
        notes.extend(result['notes'])
        if tool == 'claude' and not plan['claude_dir']:
            notes.append('Claude 桌面版侧栏条目没建，可以在命令行里用 claude --resume ' + sid + ' 打开')
        extra_hashes = {}
        for f in extra:
            if Path(f).is_file():
                extra_hashes[f] = bridge_ops._file_sha256(f)
        return {'mode': 'raw', 'local_tool': tool, 'local_src': src, 'local_sid': sid, 'operation_id': result['operation_id'],
                'extra_files': extra_hashes, 'notes': notes, 'turns': item['turns']}

    @staticmethod
    def _bytes_writer(blob):
        def write(stream):
            stream.write(blob)
            return hashlib.sha256(blob).hexdigest()
        return write

    def _zcode_project(self, directory):
        with self.env.session_reader.connect(self.env.Z_DB) as c:
            row = c.execute('SELECT project_id FROM session WHERE directory=? AND project_id IS NOT NULL LIMIT 1', (directory,)).fetchone()
        return row[0] if row else self.env.zi.make_project_id(directory)

    def _load_turns(self, zf, s):
        record = s.get('turns')
        arc, size, sha = self._member(zf, record)
        h, n, turns, total = hashlib.sha256(), 0, [], 0
        limit_turn = getattr(self.env, 'MAX_TURN_BYTES', 4 << 20)
        limit_total = getattr(self.env, 'MAX_SYNC_BYTES', 512 << 20)
        with zf.open(arc) as stream:
            for line in stream:
                n += len(line)
                h.update(line)
                rec = json.loads(line)
                if not isinstance(rec, dict) or rec.get('role') not in ('user', 'assistant') or not isinstance(rec.get('text'), str):
                    raise ValueError('文字对话格式不对')
                size_turn = len(rec['text'].encode())
                total += size_turn
                if size_turn > limit_turn or total > limit_total:
                    raise ValueError('文字对话太长，超过转文字导入的上限')
                turns.append({'role': rec['role'], 'text': rec['text']})
        if n != size or h.hexdigest() != sha:
            raise ValueError('压缩包里的文字对话和清单对不上')
        if not turns:
            raise ValueError('没有文字对话可转')
        return turns

    def _import_text(self, zf, s, item, target):
        reason = self.env.target_reason(target)
        if reason:
            raise ValueError(TOOL_NAMES[target] + '现在接收不了：' + reason)
        turns = self._load_turns(zf, s)
        meta = {'dir': item['local_cwd'], 'title': item['title'] or turns[0]['text'][:60], 'mtime': item['mtime']}
        label = 'import-text:' + item['tool'] + ':' + item['session_id']
        result = bridge_ops.import_text_session(self.env, target, meta, turns, label=label, source_tool=item['tool'])
        return {'mode': 'text', 'local_tool': target, 'local_src': result['src'], 'local_sid': result['sid'],
                'operation_id': result['operation_id'], 'extra_files': {}, 'notes': [], 'turns': len(turns)}

    def discard(self, body):
        with self._lock:
            self._plans.pop(body.get('plan_id'), None)
        return {'ok': True}

    # ------------------------------------------------------------ 记录与撤销
    def _run_path(self, run_id):
        if not isinstance(run_id, str) or not RUN_RE.match(run_id):
            raise ValueError('记录编号不对')
        return self.state_dir('runs') / (run_id + '.json')

    def _save_run(self, run):
        bridge_state.atomic_json(self._run_path(run['id']), run)
        runs = sorted(self.state_dir('runs').glob('*.json'))
        for old in runs[:-RUNS_KEPT]:
            old.unlink(missing_ok=True)

    def load_run(self, run_id):
        run = bridge_state.load_json(self._run_path(run_id), None)
        if not isinstance(run, dict):
            raise ValueError('找不到这次记录')
        return run

    @staticmethod
    def run_counts(run):
        items = run.get('items') or []
        return {k: sum(1 for i in items if i.get('status') == k) for k in ('imported', 'skipped', 'failed')} | \
               {'raw': sum(1 for i in items if i.get('status') == 'imported' and i.get('mode') == 'raw'),
                'text': sum(1 for i in items if i.get('status') == 'imported' and i.get('mode') == 'text')}

    def public_run(self, run):
        out = {k: run.get(k) for k in ('id', 'kind', 'at', 'name', 'size', 'machine', 'sessions', 'by_tool', 'secrets',
                                       'secret_sessions', 'skipped', 'zip', 'source', 'undone')}
        out['path'] = run.get('path', '')
        if run.get('kind') == 'export':
            out['items'] = run.get('items') or []
            out['exists'] = bool(run.get('path')) and Path(run['path']).is_file()
        else:
            out['items'] = [{k: i.get(k) for k in ('id', 'tool', 'title', 'mode', 'target', 'status', 'reason', 'notes', 'local_tool', 'turns')}
                            for i in run.get('items') or []]
            out['counts'] = self.run_counts(run)
        return out

    def runs(self, limit=30):
        out = []
        for p in sorted(self.state_dir('runs').glob('*.json'), reverse=True)[:limit]:
            try:
                out.append(self.public_run(self.load_run(p.stem)))
            except ValueError:
                continue
        return out

    def undo(self, body):
        if body.get('confirm') is not True:
            raise ValueError('撤销前要确认')
        self._attention()
        run = self.load_run(body.get('run_id'))
        if run.get('kind') != 'import':
            raise ValueError('只有导入能撤销')
        if run.get('undone'):
            raise ValueError('这次导入已经撤销过了')
        items = [i for i in run.get('items') or [] if i.get('status') == 'imported']
        if not items:
            raise ValueError('这次导入没有进来的会话，不用撤销')
        if any(i.get('local_tool') == 'claude' for i in items):
            self._require_claude_quit()
        with self._lock:
            if self._job is not None and self._job['status'] == 'running':
                raise ValueError('有导出或导入正在进行，等它结束再撤销')
            holding = self.state_dir('undo', run['id'])
            moved, kept, keys = [], [], []
            for item in items:
                entry = {'id': item['id'], 'title': item['title'], 'tool': item['local_tool']}
                try:
                    result = bridge_ops.delete_session(self.env, item['local_tool'], item['local_src'])
                    entry['trash_id'] = result['trash_id']
                    for k, (f, digest) in enumerate(sorted((item.get('extra_files') or {}).items())):
                        p = Path(f)
                        if p.is_file() and not p.is_symlink() and bridge_ops._file_sha256(p) == digest:
                            target = holding / f'{item["id"]:04d}-{k:05d}-{p.name[:80]}'
                            os.replace(p, target)
                    moved.append(entry)
                    keys.append(str(run['source'].get('machine_id', '')) + '|' + item['tool'] + '|' + str(item.get('session_id') or ''))
                except (OSError, ValueError) as exc:
                    kept.append({**entry, 'reason': str(exc) or type(exc).__name__})
            self._forget([k for k in keys if not k.endswith('|')])
            run['undone'] = {'at': now_text(), 'moved': moved, 'kept': kept, 'holding': str(holding)}
            self._save_run(run)
        try:
            self.env.invalidate()
        except Exception as exc:  # noqa: BLE001
            self.env.log('transfer-invalidate-failed type=' + type(exc).__name__)
        return {'run': self.public_run(run)}

    def reveal(self, body):
        run = self.load_run(body.get('run_id'))
        if run.get('kind') != 'export' or not Path(run.get('path', '')).is_file():
            raise ValueError('找不到这个压缩包了（可能被挪走了）')
        platform_paths.reveal(run['path'])
        return {'ok': True}

    # ------------------------------------------------------------ 接口
    def public_job(self, job):
        if job is None:
            return None
        return {k: job.get(k) for k in ('id', 'kind', 'status', 'progress', 'result', 'error', 'started', 'finished')}

    def status(self):
        machine = self.machine()
        return {'machine': machine, 'tools': self.tool_status(), 'claude': self.claude_status(),
                'job': self.public_job(self._job), 'export_dir': str(self.default_export_dir()), 'format': FORMAT_VERSION}

    def handle_get(self, path, query):
        if path == '/api/transfer/status':
            return self.status()
        if path == '/api/transfer/candidates':
            return {'items': self.candidates()}
        if path == '/api/transfer/job':
            return self.public_job(self.job(query.get('id', [''])[0]))
        if path == '/api/transfer/runs':
            return {'runs': self.runs()}
        if path == '/api/transfer/claude':
            return self.claude_status()
        raise ValueError('不存在的接口')

    def handle_post(self, path, body):
        if path == '/api/transfer/export':
            return self.start_export(body)
        if path == '/api/transfer/inspect':
            return self.inspect(body)
        if path == '/api/transfer/import':
            return self.start_import(body)
        if path == '/api/transfer/discard':
            return self.discard(body)
        if path == '/api/transfer/undo':
            return self.undo(body)
        if path == '/api/transfer/reveal':
            return self.reveal(body)
        raise ValueError('不存在的接口')
