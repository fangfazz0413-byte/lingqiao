"""Claude 桌面版双账号会话补齐。

桌面版侧栏只显示“当前登录账号”目录里的会话条目：
    ~/Library/Application Support/Claude/claude-code-sessions/<账号>/<组织>/local_*.json
聊天记录本体在 ~/.claude/projects/<目录>/<cliSessionId>.jsonl，不分账号。
所以切账号后“会话不见了”，把另一个账号的 local_*.json 复制过来就回来了。

做法：按 cliSessionId 去重、聊天记录不在本机的跳过、bridgeSessionIds 置空、
remoteControlAutoEligible 置 false、原子写入 0600。另加几道保险：
- Claude 桌面版主进程还开着就不写（只读预览随时能看）；备份后、写完后各再查一次；
- 每次写之前把整个 claude-code-sessions 备份一份，逐个文件核对 SHA。备份放 .bridge/config.json 里
  claude_accounts.backup_dir 指定的目录（比如外接硬盘）；没指定，或者指定的目录不在，就放灵桥本地；
- 目标账号里删过的会话（deleted_<id> 墓碑）不复活；
- 标题只在来源那边更新过时才刷新，不会把新标题改回旧的；
- 新文件用 link 落地，不覆盖任何已有文件；
- 撤销 = 把这次复制进去、之后没被桌面版动过的文件挪到灵桥撤销区（不删除），标题改回原样。
不读 Claude 的 config.json / Cookies / Local Storage，不碰登录凭据，不改聊天记录。
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

import bridge_state

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
UUID_RE = re.compile(UUID + r'\Z')
LOCAL_RE = re.compile(r'local_(' + UUID + r')\.json\Z')
TOMB_RE = re.compile(r'deleted_(' + UUID + r')\Z')
RUN_RE = re.compile(r'\d{8}-\d{6}-[0-9a-f]{4}\Z')
WINDOWS = (3, 5, 24, 48, 168, 0)  # 小时；0 = 不限
DEFAULT_BACKUP_DIR = ''   # 不指定：备份放灵桥本地 .bridge/account-sync/backups
BACKUP_PREFIX = 'Claude会话迁移备份_'
CLAUDE_BUNDLE = 'com.anthropic.claudefordesktop'
MAX_META = 4 * 1024 * 1024
RUNS_KEPT = 60
LIST_LIMIT = 400
# 桌面版主进程和它的 Helper 在跑就不写；chrome-native-host、CheckClaude、crashpad 这些辅助进程无妨。
BLOCKING = (re.compile(r'(^|/)Claude\.app/Contents/MacOS/Claude\Z'),
            re.compile(r'(^|/)Claude\.app/Contents/Frameworks/Claude Helper[^/]*\.app/'))
SKIP_REASONS = {'no_transcript': '聊天记录不在本机', 'deleted': '目标账号删过', 'no_id': '没有会话 ID',
                'duplicate': '来源里重复', 'conflict': '目标里有同名文件', 'changed': '同步时已经变了', 'error': '写入出错'}


def now_text(ts=None):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts if ts is not None else time.time()))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def list_processes():
    out = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, timeout=10, check=True).stdout
    rows = []
    for line in out.splitlines():
        match = re.match(r'\s*(\d+)\s+(.+)\Z', line)
        if match:
            rows.append((int(match.group(1)), match.group(2).strip()))
    return rows


def _fsync_dir(folder):
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_private(path, data, mode=0o600):
    """原子写入：同目录临时文件 → fsync → chmod → rename。"""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.lingqiao-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    _fsync_dir(path.parent)


def create_exclusive(path, data, mode=0o600):
    """新建文件：先写临时文件，再 link 到目标名；目标已存在就失败，绝不覆盖。"""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.lingqiao-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    _fsync_dir(path.parent)


class AccountSync:
    def __init__(self, env, process_lister=None, opener=None):
        self.env = env
        self.process_lister = process_lister or list_processes
        self.opener = opener or (lambda argv: subprocess.run(argv, check=False, timeout=15, stdin=subprocess.DEVNULL,
                                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.lock = threading.Lock()

    # ------------------------------------------------------------ 路径与配置
    def meta_root(self):
        return Path(getattr(self.env, 'CC_META_ROOT', Path(self.env.HOME) / 'Library/Application Support/Claude/claude-code-sessions'))

    def projects_root(self):
        return Path(getattr(self.env, 'CC_ROOT', Path(self.env.HOME) / '.claude/projects'))

    def state_dir(self, *parts):
        base = Path(self.env.BRIDGE) / 'account-sync'
        folder = base.joinpath(*parts)
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(len(parts) + 1):
            try:
                base.joinpath(*parts[:index]).chmod(0o700)
            except OSError:
                pass
        return folder

    def config(self):
        try:
            data = json.loads(Path(self.env.CONFIG).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        section = data.get('claude_accounts') if isinstance(data, dict) else None
        section = section if isinstance(section, dict) else {}
        raw = section.get('labels') if isinstance(section.get('labels'), dict) else {}
        labels = {k: v.strip()[:40] for k, v in raw.items() if isinstance(k, str) and UUID_RE.match(k) and isinstance(v, str) and v.strip()}
        backup = section.get('backup_dir') if isinstance(section.get('backup_dir'), str) and section.get('backup_dir').strip() else DEFAULT_BACKUP_DIR
        return {'labels': labels, 'backup_dir': Path(os.path.expanduser(backup)) if backup else None}

    def backup_target(self):
        """备份放哪：指定的目录在就放那里；没指定放灵桥本地；指定了但不在（比如外接硬盘没接）也放灵桥本地，
        这时 fallback=True，页面会说明。不在 /Volumes 下乱建目录。"""
        configured = self.config()['backup_dir']
        if configured is not None and configured.is_dir() and not configured.is_symlink():
            return configured, False
        return self.state_dir('backups'), configured is not None

    def backup_public(self):
        configured = self.config()['backup_dir']
        folder, fallback = self.backup_target()
        return {'dir': str(folder), 'fallback': fallback, 'configured': str(configured) if configured is not None else ''}

    # ------------------------------------------------------------ Claude 桌面版是否在运行
    def claude_status(self):
        try:
            rows = self.process_lister()
        except Exception as exc:  # noqa: BLE001  ps 不可用时一律当“没法确认”，不写
            return {'running': None, 'blocking': [], 'error': '查不了进程（' + type(exc).__name__ + '）'}
        blocking = [{'pid': pid, 'name': os.path.basename(comm)} for pid, comm in rows if any(p.search(comm) for p in BLOCKING)]
        return {'running': bool(blocking), 'blocking': blocking[:20], 'count': len(blocking), 'error': None}

    def _require_quit(self):
        status = self.claude_status()
        if status['running'] is None:
            raise ValueError('没法确认 Claude 桌面版有没有退出（' + status['error'] + '），这次先不写')
        if status['running']:
            raise ValueError('Claude 桌面版还开着：先在桌面版里按 ⌘Q 完全退出，再回来同步（预览不受影响）')
        return status

    # ------------------------------------------------------------ 扫描（只读）
    @staticmethod
    def _read_meta(path):
        try:
            st = os.stat(path, follow_symlinks=False)
            if st.st_size > MAX_META:
                return None, st
            with open(path, 'rb') as stream:
                data = json.loads(stream.read())
        except (OSError, ValueError):
            return None, None
        return (data if isinstance(data, dict) else None), st

    def scan(self):
        root = self.meta_root()
        if root.is_symlink() or not root.is_dir():
            return {'available': False, 'root': root, 'accounts': []}
        labels = self.config()['labels']
        accounts = []
        for account in sorted(os.scandir(root), key=lambda e: e.name):
            if not account.is_dir(follow_symlinks=False) or not UUID_RE.match(account.name):
                continue
            orgs = []
            for org in sorted(os.scandir(account.path), key=lambda e: e.name):
                if not org.is_dir(follow_symlinks=False) or not UUID_RE.match(org.name):
                    continue
                sessions, tombs, unreadable = [], set(), []
                for entry in os.scandir(org.path):
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    match = LOCAL_RE.match(entry.name)
                    if match:
                        data, st = self._read_meta(entry.path)
                        if data is None:
                            unreadable.append(entry.name)
                            continue
                        last = data.get('lastActivityAt')
                        last = int(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else int(st.st_mtime * 1000)
                        focus = data.get('lastFocusedAt')
                        focus = int(focus) if isinstance(focus, (int, float)) and not isinstance(focus, bool) else 0
                        cid = data.get('cliSessionId')
                        title = data.get('title')
                        sessions.append({'file': entry.name, 'uuid': match.group(1), 'cid': cid if isinstance(cid, str) and cid else None,
                                         'title': title if isinstance(title, str) else '', 'last': last, 'recent': max(last, focus),
                                         'mtime': st.st_mtime})
                        continue
                    tomb = TOMB_RE.match(entry.name)
                    if tomb:
                        tombs.add(tomb.group(1))
                orgs.append({'id': org.name, 'sessions': sessions, 'tombs': tombs, 'unreadable': unreadable,
                             'newest': max((s['recent'] for s in sessions), default=0)})
            accounts.append({'id': account.name, 'label': labels.get(account.name, ''), 'orgs': orgs,
                             'newest': max((o['newest'] for o in orgs), default=0)})
        return {'available': True, 'root': root, 'accounts': accounts}

    def transcripts(self):
        """~/.claude/projects/*/*.jsonl 的会话 ID（只看文件名，不读聊天内容）。"""
        found = set()
        try:
            projects = list(os.scandir(self.projects_root()))
        except OSError:
            return found
        for project in projects:
            try:
                if not project.is_dir():
                    continue
                for entry in os.scandir(project.path):
                    if entry.name.endswith('.jsonl') and entry.is_file():
                        found.add(entry.name[:-6])
            except OSError:
                continue
        return found

    # ------------------------------------------------------------ 规划
    @staticmethod
    def _account(scan, account_id):
        for account in scan['accounts']:
            if account['id'] == account_id:
                return account
        raise ValueError('找不到这个账号目录，请刷新')

    @staticmethod
    def name_of(account):
        return account['label'] or ('账号 ' + account['id'][:8])

    @staticmethod
    def target_org(account, chosen=None):
        """会话补到哪个组织目录：默认是这个账号里最近有会话活动的那个；空目录不当目标（09-23 方案的教训）。"""
        candidates = [o for o in account['orgs'] if o['sessions']]
        if chosen:
            for org in candidates:
                if org['id'] == chosen:
                    return org
            raise ValueError('选的组织目录不在这个账号下，或者里面还没有会话')
        if not candidates:
            raise ValueError(f'“{AccountSync.name_of(account)}”下还没有会话：先用这个账号在桌面版里新建一条会话，再来同步')
        return max(candidates, key=lambda o: o['newest'])

    def _plan(self, scan, transcripts, source_id, target_id, window, orgs, now):
        source, target = self._account(scan, source_id), self._account(scan, target_id)
        if source['id'] == target['id']:
            raise ValueError('来源和目标不能是同一个账号')
        dst = self.target_org(target, orgs.get(target['id']))
        existing, tombs = {}, set()
        for org in target['orgs']:
            tombs |= org['tombs']
            for session in org['sessions']:
                if session['cid'] and (session['cid'] not in existing or session['last'] > existing[session['cid']]['last']):
                    existing[session['cid']] = {**session, 'org': org['id']}
        taken = {s['file'] for s in dst['sessions']} | set(dst['unreadable'])
        plan = {'source': source['id'], 'source_label': self.name_of(source), 'target': target['id'], 'target_label': self.name_of(target),
                'target_org': dst['id'], 'window_hours': window, 'copy': [], 'update': [], 'already': [], 'skipped': [],
                'out_of_window': 0, 'unreadable': sum(len(o['unreadable']) for o in source['orgs'])}
        seen = set()
        pairs = sorted(((org['id'], s) for org in source['orgs'] for s in org['sessions']), key=lambda pair: (-pair[1]['last'], pair[1]['file']))
        for org_id, session in pairs:
            if window and now - session['mtime'] >= window * 3600:
                plan['out_of_window'] += 1
                continue
            item = {'file': session['file'], 'org': org_id, 'title': session['title'], 'cid': session['cid'], 'last': session['last']}
            if not session['cid']:
                plan['skipped'].append({**item, 'reason': 'no_id'}); continue
            if session['cid'] in seen:
                plan['skipped'].append({**item, 'reason': 'duplicate'}); continue
            seen.add(session['cid'])
            if session['cid'] not in transcripts:  # 聊天记录不在本机，搬过去也打不开
                plan['skipped'].append({**item, 'reason': 'no_transcript'}); continue
            if session['uuid'] in tombs or session['cid'] in tombs:  # 目标账号里删过，不复活
                plan['skipped'].append({**item, 'reason': 'deleted'}); continue
            have = existing.get(session['cid'])
            if have:
                if session['title'] and have['title'] != session['title'] and session['last'] > have['last']:
                    plan['update'].append({**item, 'target_file': have['file'], 'target_org': have['org'],
                                           'title_old': have['title'], 'title_new': session['title']})
                else:
                    plan['already'].append(item)
                continue
            if session['file'] in taken:
                plan['skipped'].append({**item, 'reason': 'conflict'}); continue
            plan['copy'].append(item)
        return plan

    @staticmethod
    def _params(body):
        source, target = body.get('source'), body.get('target')
        if not (isinstance(source, str) and UUID_RE.match(source) and isinstance(target, str) and UUID_RE.match(target)):
            raise ValueError('请选好来源账号和目标账号')
        window = body.get('window_hours', 24)
        if isinstance(window, bool) or window not in WINDOWS:
            raise ValueError('时间范围只能选 3、5、24、48、168 小时或不限')
        orgs = body.get('orgs') or {}
        if not isinstance(orgs, dict) or any(not (isinstance(k, str) and UUID_RE.match(k) and isinstance(v, str) and UUID_RE.match(v))
                                             for k, v in orgs.items()):
            raise ValueError('组织目录参数不对')
        return source, target, int(window), body.get('both') is True, orgs

    def plans(self, body, scan=None):
        source, target, window, both, orgs = self._params(body)
        scan = scan or self.scan()
        if not scan['available']:
            raise ValueError('没找到 Claude 桌面版的会话目录')
        transcripts, now = self.transcripts(), time.time()
        result = [self._plan(scan, transcripts, source, target, window, orgs, now)]
        if both:
            result.append(self._plan(scan, transcripts, target, source, window, orgs, now))
        return result

    @staticmethod
    def counts(plan):
        out = {'copy': len(plan['copy']), 'update': len(plan['update']), 'already': len(plan['already']),
               'out_of_window': plan['out_of_window'], 'unreadable': plan['unreadable']}
        for reason in SKIP_REASONS:
            out[reason] = sum(1 for s in plan['skipped'] if s['reason'] == reason)
        return out

    def public_plan(self, plan):
        def trim(items):
            return [{k: v for k, v in item.items() if k != 'cid'} for item in items[:LIST_LIMIT]]
        return {'source': plan['source'], 'source_label': plan['source_label'], 'target': plan['target'],
                'target_label': plan['target_label'], 'target_org': plan['target_org'], 'window_hours': plan['window_hours'],
                'counts': self.counts(plan), 'copy': trim(plan['copy']), 'update': trim(plan['update']),
                'skipped': [{**s, 'reason_text': SKIP_REASONS[s['reason']]} for s in trim(plan['skipped'])],
                'already': trim(plan['already'])}

    # ------------------------------------------------------------ 接口：状态、预览
    def status(self):
        scan, claude = self.scan(), self.claude_status()
        base = {'claude': claude, 'windows': list(WINDOWS), 'backup': self.backup_public(), 'root': str(scan['root'])}
        if not scan['available']:
            return {**base, 'available': False, 'reason': '没找到 Claude 桌面版的会话目录', 'accounts': [], 'runs': self.runs(10)}
        recent = max(scan['accounts'], key=lambda a: a['newest'], default=None)
        accounts = []
        for account in scan['accounts']:
            try:
                auto = self.target_org(account)['id']
            except ValueError:
                auto = None
            accounts.append({'id': account['id'], 'short': account['id'][:8], 'label': account['label'], 'name': self.name_of(account),
                             'sessions': sum(len(o['sessions']) for o in account['orgs']),
                             'tombstones': sum(len(o['tombs']) for o in account['orgs']), 'newest': account['newest'] or None,
                             'recent': bool(recent and recent['newest'] and recent['id'] == account['id']), 'target_org': auto,
                             'ambiguous': sum(1 for o in account['orgs'] if o['sessions']) > 1,
                             'orgs': [{'id': o['id'], 'short': o['id'][:8], 'sessions': len(o['sessions']), 'tombstones': len(o['tombs']),
                                       'unreadable': len(o['unreadable']), 'newest': o['newest'] or None} for o in account['orgs']]})
        accounts.sort(key=lambda a: (not a['label'], -(a['newest'] or 0)))
        return {**base, 'available': True, 'reason': '', 'accounts': accounts, 'runs': self.runs(10)}

    def preview(self, body):
        plans = self.plans(body)
        return {'plans': [self.public_plan(p) for p in plans], 'claude': self.claude_status(), 'backup': self.backup_public(),
                'nothing': all(not p['copy'] and not p['update'] for p in plans)}

    # ------------------------------------------------------------ 备份
    def _backup(self):
        base, fallback = self.backup_target()
        stamp = time.strftime('%Y%m%d_%H%M%S')
        for n in range(1, 100):
            folder = base / (BACKUP_PREFIX + stamp + ('' if n == 1 else f'_{n}'))
            try:
                folder.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise OSError('备份目录名用完了')
        source, copy = self.meta_root(), folder / 'claude-code-sessions'
        shutil.copytree(source, copy, symlinks=True)
        files, size = self._verify_copy(source, copy)
        return {'path': str(folder), 'files': files, 'bytes': size, 'fallback': fallback, 'verified': True}

    @staticmethod
    def _verify_copy(source, copy):
        files = size = 0
        for folder, dirnames, filenames in os.walk(source):
            rel = os.path.relpath(folder, source)
            for name in dirnames + filenames:
                a, b = Path(folder) / name, Path(copy) / rel / name
                if a.is_symlink():
                    if not b.is_symlink() or os.readlink(a) != os.readlink(b):
                        raise ValueError(f'备份核对没过（{os.path.join(rel, name)}），这次没有同步')
                    continue
                if name in filenames:
                    if not b.is_file() or b.is_symlink() or sha256_file(a) != sha256_file(b):
                        raise ValueError(f'备份核对没过（{os.path.join(rel, name)}），这次没有同步')
                    files += 1
                    size += a.stat().st_size
        return files, size

    # ------------------------------------------------------------ 同步（写）
    def _apply(self, plan):
        root = self.meta_root()
        dst_dir = root / plan['target'] / plan['target_org']
        result = {'source': plan['source'], 'source_label': plan['source_label'], 'target': plan['target'],
                  'target_label': plan['target_label'], 'target_org': plan['target_org'], 'copied': [], 'updated': [], 'failed': []}
        for item in plan['copy']:
            base = {k: item[k] for k in ('file', 'org', 'title', 'cid')}
            path = root / plan['source'] / item['org'] / item['file']
            data, _ = self._read_meta(path)
            if data is None or path.is_symlink() or data.get('cliSessionId') != item['cid']:
                result['failed'].append({**base, 'reason': 'changed'}); continue
            data['bridgeSessionIds'] = []  # 旧账号的远程桥接 ID，留着会重连报错
            data['remoteControlAutoEligible'] = False
            raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
            try:
                create_exclusive(dst_dir / item['file'], raw)
            except FileExistsError:
                result['failed'].append({**base, 'reason': 'conflict'}); continue
            except OSError as exc:
                result['failed'].append({**base, 'reason': 'error', 'detail': type(exc).__name__}); continue
            result['copied'].append({**base, 'sha256': sha256_bytes(raw)})
        for item in plan['update']:
            base = {'file': item['target_file'], 'org': item['target_org'], 'cid': item['cid'],
                    'title_old': item['title_old'], 'title_new': item['title_new']}
            path = root / plan['target'] / item['target_org'] / item['target_file']
            data, st = self._read_meta(path)
            if data is None or path.is_symlink() or data.get('cliSessionId') != item['cid'] or data.get('title') != item['title_old']:
                result['failed'].append({**base, 'reason': 'changed'}); continue
            data['title'] = item['title_new']
            raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
            try:
                before = path.read_bytes()
                write_private(path, raw, st.st_mode & 0o777 or 0o600)
            except OSError as exc:
                result['failed'].append({**base, 'reason': 'error', 'detail': type(exc).__name__}); continue
            result['updated'].append({**base, 'sha256_before': sha256_bytes(before), 'sha256': sha256_bytes(raw)})
        return result

    def sync(self, body):
        if body.get('confirm') is not True:
            raise ValueError('同步前要先预览，再确认')
        if not self.lock.acquire(blocking=False):
            raise ValueError('上一次同步还没做完')
        try:
            self._require_quit()
            with bridge_state.operation_lock(Path(self.env.BRIDGE) / 'operation.lock'):
                plans = self.plans(body, self.scan())
                params = {k: body.get(k) for k in ('source', 'target', 'window_hours', 'both', 'orgs')}
                if all(not p['copy'] and not p['update'] for p in plans):
                    return {'ok': True, 'nothing': True, 'plans': [self.public_plan(p) for p in plans]}
                run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2)
                backup = self._backup()
                self._require_quit()  # 备份的这几秒里桌面版被打开了，就不写
                record = {'id': run_id, 'at': now_text(), 'params': params, 'backup': backup,
                          'plans': [self.public_plan(p) for p in plans], 'results': [self._apply(p) for p in plans],
                          'warnings': [], 'undone': None}
                after = self.claude_status()
                if after['running']:
                    record['warnings'].append('同步刚写完时 Claude 桌面版已经开着了：建议退出后重新预览核对一遍')
                self._save_run(record)
        finally:
            self.lock.release()
        totals = self._totals(record)
        self.env.log(f"account-sync run={record['id']} copied={totals['copied']} updated={totals['updated']} "
                     f"failed={totals['failed']} backup_files={backup['files']}")
        self._invalidate()
        return {'ok': True, 'nothing': False, 'run': self.public_run(record)}

    @staticmethod
    def _totals(record):
        return {key: sum(len(r[key]) for r in record['results']) for key in ('copied', 'updated', 'failed')}

    def _invalidate(self):
        invalidate = getattr(self.env, 'invalidate', None)
        if callable(invalidate):
            try:
                invalidate()
            except Exception:  # noqa: BLE001  刷新会话列表缓存失败不影响同步结果
                pass

    # ------------------------------------------------------------ 记录与撤销
    def _run_path(self, run_id):
        if not isinstance(run_id, str) or not RUN_RE.match(run_id):
            raise ValueError('同步记录编号不对')
        return self.state_dir('runs') / f'{run_id}.json'

    def _save_run(self, record):
        data = json.dumps(record, ensure_ascii=False, indent=1).encode('utf-8')
        write_private(self._run_path(record['id']), data)
        if record.get('backup', {}).get('path'):
            try:
                write_private(Path(record['backup']['path']) / '灵桥同步记录.json', data)
            except OSError:
                pass
        runs = sorted(p for p in self.state_dir('runs').glob('*.json') if RUN_RE.match(p.stem))
        for old in runs[:-RUNS_KEPT]:
            try:
                old.unlink()
            except OSError:
                pass

    def load_run(self, run_id):
        path = self._run_path(run_id)
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise ValueError('找不到这次同步的记录') from None

    def public_run(self, record):
        return {'id': record['id'], 'at': record['at'], 'backup': record['backup'], 'results': record['results'],
                'totals': self._totals(record), 'warnings': record.get('warnings', []), 'undone': record.get('undone'),
                'direction': ' + '.join(f"{r['source_label']} → {r['target_label']}" for r in record['results']),
                'window_hours': (record.get('params') or {}).get('window_hours')}

    def runs(self, limit=20):
        folder = Path(self.env.BRIDGE) / 'account-sync' / 'runs'
        if not folder.is_dir():
            return []
        out = []
        for path in sorted((p for p in folder.glob('*.json') if RUN_RE.match(p.stem)), reverse=True)[:limit]:
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            run = self.public_run(record)
            run.pop('results')
            out.append(run)
        return out

    def undo(self, run_id):
        record = self.load_run(run_id)
        if record.get('undone'):
            raise ValueError('这次同步已经撤销过了')
        if not self.lock.acquire(blocking=False):
            raise ValueError('有同步正在做，等它做完')
        try:
            self._require_quit()
            with bridge_state.operation_lock(Path(self.env.BRIDGE) / 'operation.lock'):
                backup = self._backup()  # 撤销也是写，先备份
                self._require_quit()
                root, holding = self.meta_root(), self.state_dir('undo', run_id)
                moved, restored, kept = [], [], []
                for result in record['results']:
                    for item in result['copied']:
                        path = root / result['target'] / result['target_org'] / item['file']
                        label = {'file': item['file'], 'title': item.get('title', '')}
                        if path.is_symlink() or not path.is_file():
                            kept.append({**label, 'reason': '已经不在了'}); continue
                        if sha256_file(path) != item['sha256']:
                            kept.append({**label, 'reason': '同步之后桌面版动过这条（比如打开过），没挪'}); continue
                        target = holding / result['target'] / result['target_org'] / item['file']
                        try:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(path), str(target))  # 挪到灵桥撤销区，不删除
                        except OSError as exc:
                            kept.append({**label, 'reason': '挪的时候出错（' + type(exc).__name__ + '）'}); continue
                        moved.append(label)
                    for item in result['updated']:
                        path = root / result['target'] / result['target_org'] / item['file']
                        label = {'file': item['file'], 'title': item['title_new']}
                        data, st = self._read_meta(path)
                        if data is None or path.is_symlink() or sha256_file(path) != item['sha256'] or data.get('title') != item['title_new']:
                            kept.append({**label, 'reason': '同步之后桌面版动过这条，标题没改回'}); continue
                        data['title'] = item['title_old']
                        try:
                            write_private(path, json.dumps(data, ensure_ascii=False).encode('utf-8'), st.st_mode & 0o777 or 0o600)
                        except OSError as exc:
                            kept.append({**label, 'reason': '改回标题时出错（' + type(exc).__name__ + '）'}); continue
                        restored.append(label)
                record['undone'] = {'at': now_text(), 'moved': moved, 'restored': restored, 'kept': kept,
                                    'holding': str(holding), 'backup': backup}
                self._save_run(record)
        finally:
            self.lock.release()
        self.env.log(f'account-sync undo run={run_id} moved={len(moved)} restored={len(restored)} kept={len(kept)}')
        self._invalidate()
        return {'ok': True, 'run': self.public_run(record)}

    def open_claude(self):
        self.opener(['/usr/bin/open', '-b', CLAUDE_BUNDLE])
        return {'ok': True}

    # ------------------------------------------------------------ HTTP
    def handle_get(self, path, query):
        if path == '/api/accounts/status':
            return self.status()
        if path == '/api/accounts/claude':
            return self.claude_status()
        if path == '/api/accounts/runs':
            return {'runs': self.runs(30)}
        if path == '/api/accounts/run':
            return {'run': self.public_run(self.load_run((query.get('id') or [''])[0]))}
        raise FileNotFoundError('不存在的接口')

    def handle_post(self, path, body):
        if path == '/api/accounts/preview':
            return self.preview(body)
        if path == '/api/accounts/sync':
            return self.sync(body)
        if path == '/api/accounts/undo':
            return self.undo(body.get('run_id'))
        if path == '/api/accounts/open-claude':
            return self.open_claude()
        raise FileNotFoundError('不存在的接口')
