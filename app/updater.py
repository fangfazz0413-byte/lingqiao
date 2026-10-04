"""检查更新：灵桥和 plugins/ 里的插件，只要是从 GitHub 克隆下来的文件夹，就能在灵桥里一键拉新版本。

宁可不更新，也不弄乱：
- 只做"快进"：这台电脑没改过程序文件、也没有还没上传的提交，才会更新；两边都改过就停下来，写明原因。
- 更新前确认灵桥没在做别的事（同步、删除、清理、导出导入、插件任务），更新期间占着操作锁。
- 步骤：记下当前版本 → 快进到 GitHub 上的版本 → 依赖清单变了就补装 → 跑一遍测试 → 通过就提示重启；
  没通过就退回原来的版本（依赖也按原来的清单装回去）。
- 会话、配置、回收站这些数据不在 git 里，更新不碰。
- 自动检查默认每天一次，只看不改；.bridge/config.json 里写 "update": {"auto_check": false} 可以关掉。
"""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time

import bridge_state

GIT_CANDIDATES = ('/opt/homebrew/bin/git', '/usr/local/bin/git')
CHECK_EVERY = 24 * 3600
FIRST_CHECK_DELAY = 90
FETCH_TIMEOUT = 90
MERGE_TIMEOUT = 120
TEST_TIMEOUT = 20 * 60
REPAIR_TIMEOUT = 20 * 60
RUNS_KEPT = 30
TAIL_LINES = 40
CREDENTIALS = re.compile(r'(\w+://)[^/@\s]+@')
VERSION_LINE = re.compile(r"^VERSION\s*=\s*'([0-9][0-9.]*)'", re.M)
REQUIREMENTS = 'app/requirements-desktop.txt'
EXECUTABLES = ('会话桥.app/Contents/MacOS/会话桥', 'app/repair-runtime.sh', 'install.sh')


class UpdateError(ValueError):
    pass


def clean(text, limit=300):
    """git 的报错里可能带着远端地址；地址里要是嵌了账号口令，去掉再显示。"""
    return CREDENTIALS.sub(r'\1', str(text or '')).strip()[:limit]


class Updater:
    def __init__(self, env):
        self.env = env
        self.lock = threading.RLock()
        self.job = None
        self.last = None
        self.git = None
        self.runner = subprocess.run            # 测试里可以换掉
        self.python = sys.executable

    # ------------------------------------------------------------ 基础
    def state_dir(self):
        folder = Path(self.env.BRIDGE) / 'update'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder

    def config(self):
        try:
            data = bridge_state.load_json(Path(self.env.CONFIG), {}) or {}
        except ValueError:
            data = {}
        section = data.get('update') if isinstance(data, dict) else None
        section = section if isinstance(section, dict) else {}
        return {'auto_check': section.get('auto_check', True) is not False}

    def git_path(self):
        if self.git:
            return self.git
        for candidate in GIT_CANDIDATES:
            if os.access(candidate, os.X_OK):
                self.git = candidate
                return candidate
        # /usr/bin/git 只是个壳：没装 Xcode 命令行工具时一调用就弹安装窗口，所以先确认装了再用。
        try:
            ready = subprocess.run(['/usr/bin/xcode-select', '-p'], capture_output=True, timeout=10,
                                   stdin=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ready = False
        if ready and os.access('/usr/bin/git', os.X_OK):
            self.git = '/usr/bin/git'
        return self.git

    def run_git(self, repo, *args, timeout=30, check=True):
        git = self.git_path()
        if not git:
            raise UpdateError('这台 Mac 没装 git（装好 Xcode 命令行工具或 Homebrew 的 git 再试）')
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', LC_ALL='C',
                   GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=15')
        try:
            # core.fileMode=false：只是可执行权限不一样（比如经网页上传过）不算本机改过文件
            result = subprocess.run([git, '-c', 'core.fileMode=false', '-C', str(repo), *args], capture_output=True, text=True, timeout=timeout,
                                    env=env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise UpdateError('git ' + args[0] + ' 超时') from None
        if check and result.returncode != 0:
            lines = (result.stderr or result.stdout or '').strip().splitlines()
            raise UpdateError(clean(lines[-1] if lines else 'git ' + args[0] + ' 失败'))
        return result.stdout.strip() if result.returncode == 0 else ''

    # ------------------------------------------------------------ 哪些文件夹能更新
    def repos(self):
        items = [{'key': 'core', 'name': '灵桥', 'kind': 'core', 'path': Path(self.env.REPO)}]
        host = getattr(self.env, 'plugins', None)
        seen = set()
        for plugin in list(getattr(host, 'plugins', {}).values()) + list(getattr(host, 'failed', [])):
            key = 'plugin:' + (plugin.id or plugin.folder.name)
            if key in seen:
                continue
            seen.add(key)
            items.append({'key': key, 'name': plugin.name or plugin.folder.name, 'kind': 'plugin', 'path': Path(plugin.folder)})
        return items

    def local_version(self, repo):
        if repo['kind'] == 'core':
            return str(self.env.VERSION)
        try:
            return str(json.loads((repo['path'] / 'plugin.json').read_text(encoding='utf-8')).get('version') or '')
        except (OSError, ValueError, AttributeError):
            return ''

    def remote_version(self, repo):
        if repo['kind'] == 'core':
            match = VERSION_LINE.search(self.run_git(repo['path'], 'show', '@{u}:app/server.py', check=False))
            return match.group(1) if match else ''
        try:
            return str(json.loads(self.run_git(repo['path'], 'show', '@{u}:plugin.json', check=False) or '{}').get('version') or '')
        except (ValueError, AttributeError):
            return ''

    def inspect(self, repo, fetch=False):
        path = repo['path']
        info = {'key': repo['key'], 'name': repo['name'], 'kind': repo['kind'], 'git': False, 'reason': '',
                'version': self.local_version(repo), 'behind': 0, 'ahead': 0, 'dirty': [], 'dirty_count': 0, 'changes': []}
        if not (path / '.git').exists():
            info['reason'] = '这个文件夹不是从 GitHub 克隆的，没法自动更新'
            return info
        try:
            top = self.run_git(path, 'rev-parse', '--show-toplevel')
            if Path(top).resolve() != path.resolve():
                info['reason'] = '这个文件夹在别的 git 仓库里面，没法单独更新'
                return info
            info['git'] = True
            upstream = self.run_git(path, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}', check=False)
            if not upstream:
                info['reason'] = '没有对应的 GitHub 分支（克隆时的设置不全）'
                return info
            info['upstream'] = upstream
            if fetch:
                self.run_git(path, 'fetch', '--quiet', '--prune', upstream.split('/', 1)[0], timeout=FETCH_TIMEOUT)
            info['head'] = self.run_git(path, 'rev-parse', 'HEAD')
            info['remote_head'] = self.run_git(path, 'rev-parse', '@{u}')
            ahead, behind = self.run_git(path, 'rev-list', '--left-right', '--count', 'HEAD...@{u}').split()
            info['ahead'], info['behind'] = int(ahead), int(behind)
            dirty = [line[3:] for line in self.run_git(path, 'status', '--porcelain', '--untracked-files=no').splitlines() if line.strip()]
            info['dirty'], info['dirty_count'] = dirty[:20], len(dirty)
            info['remote_version'] = self.remote_version(repo)
            if info['behind']:
                log = self.run_git(path, 'log', '--format=%s', '-n', '20', 'HEAD..@{u}')
                info['changes'] = [clean(line, 120) for line in log.splitlines() if line.strip()]
            info['reason'] = self.block_reason(info)
        except UpdateError as exc:
            info['reason'] = str(exc)
        return info

    @staticmethod
    def block_reason(info):
        if not info['behind']:
            return ''
        if info['dirty_count']:
            return f'这台电脑上改过 {info["dirty_count"]} 个程序文件，自动更新会把它们冲掉；先交给 AI 处理（提交或撤回）再更新'
        if info['ahead']:
            return f'这台电脑上有 {info["ahead"]} 个还没上传的改动，两边都改过，需要先合并'
        return ''

    @staticmethod
    def updatable(info):
        return info.get('git') and info.get('behind', 0) > 0 and not info.get('reason')

    # ------------------------------------------------------------ 查看 / 检查
    def status(self):
        git = bool(self.git_path())
        repos = [self.inspect(repo) for repo in self.repos()]
        saved = bridge_state.load_json(self.state_dir() / 'state.json', {}) or {}
        with self.lock:
            job = self.public_job(self.job)
        return {'git': git, 'repos': repos, 'checked_at': saved.get('checked_at'), 'auto_check': self.config()['auto_check'],
                'available': any(self.updatable(r) for r in repos), 'job': job}

    def check(self):
        repos = [self.inspect(repo, fetch=True) for repo in self.repos()]
        now = time.time()
        bridge_state.atomic_json(self.state_dir() / 'state.json', {'checked_at': now})
        with self.lock:
            self.last = {'checked_at': now, 'repos': repos}
            job = self.public_job(self.job)
        return {'git': bool(self.git_path()), 'repos': repos, 'checked_at': now, 'auto_check': self.config()['auto_check'],
                'available': any(self.updatable(r) for r in repos), 'job': job}

    def auto_check(self, stop_event):
        """后台：启动一会儿后看一次，之后每小时醒来看看是不是隔了一天。只下载版本信息，不改文件。"""
        if stop_event.wait(FIRST_CHECK_DELAY):
            return
        while not stop_event.is_set():
            try:
                if self.config()['auto_check'] and self.git_path() and any((r['path'] / '.git').exists() for r in self.repos()):
                    saved = bridge_state.load_json(self.state_dir() / 'state.json', {}) or {}
                    if time.time() - float(saved.get('checked_at') or 0) >= CHECK_EVERY:
                        result = self.check()
                        self.env.log('update-check available=' + str(int(result['available'])))
            except Exception as exc:  # noqa: BLE001  自动检查失败下次再说
                self.env.log('update-check-failed type=' + type(exc).__name__)
            stop_event.wait(3600)

    # ------------------------------------------------------------ 更新
    def busy_reasons(self):
        reasons = []
        job = getattr(getattr(self.env, 'transfer', None), '_job', None)
        if isinstance(job, dict) and job.get('status') == 'running':
            reasons.append('会话导出 / 导入正在进行')
        plans = Path(self.env.BRIDGE) / 'cleanup-plans'
        for path in sorted(plans.glob('*.json')) if plans.is_dir() else ():
            try:
                if (bridge_state.load_json(path, {}) or {}).get('status') == 'running':
                    reasons.append('清理正在进行')
                    break
            except ValueError:
                continue
        host = getattr(self.env, 'plugins', None)
        if host is not None:
            reasons += host.busy_reasons()
        return reasons

    def _take_operation_lock(self):
        path = Path(self.env.BRIDGE) / 'operation.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise UpdateError('灵桥正在做别的操作（同步、删除、清理或导入），等它做完再更新') from None
        return fd

    def apply(self, body):
        if body.get('confirm') is not True:
            raise UpdateError('更新要先确认')
        expect = body.get('expect')
        if not isinstance(expect, dict) or not expect or not all(isinstance(k, str) and isinstance(v, str) for k, v in expect.items()):
            raise UpdateError('先检查更新，再点更新')
        with self.lock:
            if self.job is not None and self.job['status'] == 'running':
                raise UpdateError('已经在更新了')
            busy = self.busy_reasons()
            if busy:
                raise UpdateError('现在不能更新：' + '；'.join(busy))
            targets = []
            for repo in self.repos():
                if repo['key'] not in expect:
                    continue
                info = self.inspect(repo)
                if not self.updatable(info):
                    raise UpdateError(f'「{repo["name"]}」现在不能更新：' + (info['reason'] or '已经是最新的'))
                if info['remote_head'] != expect[repo['key']]:
                    raise UpdateError(f'「{repo["name"]}」在 GitHub 上又有了新改动，重新检查一下再更新')
                targets.append((repo, info))
            if not targets:
                raise UpdateError('没有要更新的')
            fd = self._take_operation_lock()
            job = {'id': time.strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(3), 'status': 'running', 'step': '准备更新…',
                   'started': time.time(), 'finished': None, 'error': '', 'tail': [], 'restart': False, 'rolled_back': None,
                   'repos': [{'key': r['key'], 'name': r['name'], 'from': i['version'], 'to': i.get('remote_version', ''),
                              'head': i['head'], 'target': i['remote_head']} for r, i in targets]}
            self.job = job
        threading.Thread(target=self._run, args=(job, targets, fd), daemon=True).start()
        return {'job': self.public_job(job)}

    @staticmethod
    def _fix_modes(folder):
        """启动器和脚本要能执行：仓库里要是没记可执行权限，更新后补上。"""
        for rel in EXECUTABLES:
            path = Path(folder) / rel
            if path.is_file() and not path.is_symlink():
                path.chmod(0o755)

    def _changed(self, repo, before, name):
        return bool(self.run_git(repo['path'], 'diff', '--name-only', before, 'HEAD', '--', name))

    def _repair(self):
        script = Path(self.env.REPO) / 'app' / 'repair-runtime.sh'
        result = self.runner(['/bin/bash', str(script)], capture_output=True, text=True, timeout=REPAIR_TIMEOUT,
                             stdin=subprocess.DEVNULL, cwd=str(self.env.REPO))
        if result.returncode != 0:
            lines = (result.stderr or result.stdout or '').strip().splitlines()
            raise UpdateError('补装依赖没成功：' + clean(lines[-1] if lines else '没有输出'))

    def _tests(self, repos):
        """跑测试：灵桥自己的一套，加上每个带 tests/ 的插件的一套。全过才算更新成功。"""
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP')}
        env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8', LINGQIAO_CORE=str(self.env.REPO))
        suites = [Path(self.env.REPO)] + [r['path'] for r in repos if r['kind'] == 'plugin' and (r['path'] / 'tests').is_dir()]
        for folder in suites:
            try:
                result = self.runner([self.python, '-B', '-m', 'unittest', 'discover', '-s', 'tests'], capture_output=True, text=True,
                                     timeout=TEST_TIMEOUT, cwd=str(folder), env=env, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return False, ['测试超时：' + folder.name]
            lines = [clean(line, 200) for line in (result.stderr + '\n' + result.stdout).strip().splitlines()]
            if result.returncode != 0:
                return False, lines[-TAIL_LINES:]
        return True, []

    def _run(self, job, targets, fd):
        updated = []
        try:
            for repo, info in targets:
                job['step'] = f'正在下载「{repo["name"]}」的新版本…'
                self.run_git(repo['path'], 'merge', '--ff-only', '--quiet', info['remote_head'], timeout=MERGE_TIMEOUT)
                updated.append((repo, info['head']))
                if repo['kind'] == 'core':
                    self._fix_modes(repo['path'])
            if any(repo['kind'] == 'core' and self._changed(repo, before, REQUIREMENTS) for repo, before in updated):
                job['step'] = '依赖清单变了，正在补装（要联网，可能要几分钟）…'
                job['repaired'] = True
                self._repair()
            job['step'] = '正在跑测试，确认新版本在这台电脑上没问题…'
            ok, tail = self._tests(self.repos())
            if not ok:
                job['tail'] = tail
                raise UpdateError('新版本的测试没通过，已退回原来的版本')
            job.update(status='done', step='更新好了，重启灵桥后生效', restart=True, finished=time.time())
        except Exception as exc:  # noqa: BLE001  任何一步出错都退回
            job['step'] = '没更新成功，正在退回原来的版本…'
            problems = self._rollback(updated, job)
            job.update(status='failed', finished=time.time(), rolled_back=not problems,
                       error=(str(exc) if isinstance(exc, UpdateError) else '更新出错（' + type(exc).__name__ + '）') +
                       ('' if not problems else '；退回时出了问题：' + '；'.join(problems)))
            self.env.log('update-failed type=' + type(exc).__name__)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            self._save_run(job)

    def _rollback(self, updated, job):
        problems = []
        for repo, before in reversed(updated):
            try:
                self.run_git(repo['path'], 'reset', '--keep', '--quiet', before, timeout=MERGE_TIMEOUT)
            except UpdateError as exc:
                problems.append(f'「{repo["name"]}」：{exc}')
        if job.get('repaired') and not problems:
            try:
                self._repair()
            except (UpdateError, OSError, subprocess.TimeoutExpired) as exc:
                problems.append('依赖装回原来的版本没成功：' + clean(exc))
        return problems

    def _save_run(self, job):
        try:
            folder = self.state_dir() / 'runs'
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            bridge_state.atomic_json(folder / (job['id'] + '.json'), self.public_job(job))
            for old in sorted(folder.glob('*.json'))[:-RUNS_KEPT]:
                old.unlink()
        except (OSError, ValueError) as exc:
            self.env.log('update-record-failed type=' + type(exc).__name__)

    def public_job(self, job):
        if job is None:
            return None
        return {k: job.get(k) for k in ('id', 'status', 'step', 'started', 'finished', 'error', 'tail', 'restart', 'rolled_back', 'repos')}

    def restart(self, body):
        if body.get('confirm') is not True:
            raise UpdateError('重启要先确认')
        with self.lock:
            if self.job is None or self.job.get('status') != 'done' or not self.job.get('restart'):
                raise UpdateError('没有等着重启的更新')
        app = Path(self.env.REPO) / '会话桥.app'
        # 等这个进程退出后再打开灵桥：新开的那个才会用上新代码。
        script = 'while kill -0 "$1" 2>/dev/null; do sleep 0.3; done; sleep 0.5; exec /usr/bin/open "$2"'
        subprocess.Popen(['/bin/sh', '-c', script, 'lingqiao-restart', str(os.getpid()), str(app)], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        threading.Timer(0.5, self.env.request_shutdown).start()
        return {'ok': True}

    # ------------------------------------------------------------ 接口
    def handle_get(self, path, query):
        if path == '/api/update/status':
            return self.status()
        if path == '/api/update/job':
            with self.lock:
                return {'job': self.public_job(self.job)}
        raise FileNotFoundError('不存在的接口')

    def handle_post(self, path, body):
        if path == '/api/update/check':
            return self.check()
        if path == '/api/update/apply':
            return self.apply(body)
        if path == '/api/update/restart':
            return self.restart(body)
        raise FileNotFoundError('不存在的接口')
