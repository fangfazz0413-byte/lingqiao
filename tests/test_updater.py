"""检查更新：用临时文件夹里的 git 仓库模拟"GitHub 上的正本 + 这台电脑上的克隆"。

不联网、不碰真实的灵桥文件夹；每个仓库都是临时建的小仓库，里面只有一个假的 server.py 和一条测试。
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app')); sys.path.insert(0, str(ROOT / '.bridge'))
import bridge_state  # noqa: E402
import updater  # noqa: E402

PASSING = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
FAILING = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.fail('新版本坏了')\n"


def git(cwd, *args):
    result = subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', '-c', 'init.defaultBranch=main',
                             '-c', 'commit.gpgsign=false', *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError('git ' + ' '.join(args) + ': ' + result.stderr)
    return result.stdout.strip()


@unittest.skipUnless(updater.Updater(SimpleNamespace()).git_path(), '这台电脑没有 git')
class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.origin = base / 'origin.git'
        git(base, 'init', '--bare', '-b', 'main', str(self.origin))
        self.dev = base / 'dev'
        git(base, 'clone', '--quiet', str(self.origin), str(self.dev))
        self.write(self.dev, {'app/server.py': "VERSION = '1.0.0'\n", 'app/requirements-desktop.txt': 'pywebview==1.0\n',
                              'tests/__init__.py': '', 'tests/test_ok.py': PASSING, '.gitignore': '/.bridge/\n'})
        self.commit(self.dev, '第一版')
        git(self.dev, 'push', '--quiet', '-u', 'origin', 'main')
        self.install = base / 'install'
        git(base, 'clone', '--quiet', str(self.origin), str(self.install))
        self.logs = []
        self.env = SimpleNamespace(REPO=self.install, BRIDGE=self.install / '.bridge', CONFIG=self.install / '.bridge' / 'config.json',
                                   VERSION='1.0.0', log=self.logs.append, plugins=None, transfer=None, request_shutdown=lambda: None)
        self.u = updater.Updater(self.env)
        self.u.python = sys.executable

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, repo, files):
        for name, text in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')

    def commit(self, repo, message):
        git(repo, 'add', '-A'); git(repo, 'commit', '--quiet', '-m', message)

    def publish(self, files, message, repo=None):
        repo = repo or self.dev
        self.write(repo, files); self.commit(repo, message); git(repo, 'push', '--quiet')

    def head(self, repo=None):
        return git(repo or self.install, 'rev-parse', 'HEAD')

    def wait(self):
        for _ in range(600):
            with self.u.lock:
                job = self.u.public_job(self.u.job)
            if job['status'] != 'running':
                return job
            time.sleep(0.05)
        self.fail('更新一直没结束')

    def expect(self, status):
        return {r['key']: r['remote_head'] for r in status['repos'] if self.u.updatable(r)}

    # ------------------------------------------------------------
    def test_folder_that_is_not_a_clone_cannot_update(self):
        self.env.REPO = Path(self.tmp.name) / 'plain'
        self.env.REPO.mkdir()
        status = self.u.status()
        self.assertFalse(status['available'])
        self.assertIn('不是从 GitHub 克隆的', status['repos'][0]['reason'])

    def test_check_shows_new_version_and_update_fast_forwards(self):
        before = self.head()
        self.assertFalse(self.u.check()['available'])               # 刚克隆下来，已经是最新的
        self.publish({'app/server.py': "VERSION = '1.1.0'\n"}, '加了新功能')
        status = self.u.check()
        [core] = status['repos']
        self.assertTrue(status['available'])
        self.assertEqual((core['behind'], core['ahead'], core['version'], core['remote_version']), (1, 0, '1.0.0', '1.1.0'))
        self.assertEqual(core['changes'], ['加了新功能'])
        self.assertTrue((self.env.BRIDGE / 'update' / 'state.json').is_file())
        self.assertEqual(self.head(), before)                        # 检查只下载版本信息，不改文件
        started = self.u.apply({'confirm': True, 'expect': self.expect(status)})
        self.assertEqual(started['job']['status'], 'running')
        job = self.wait()
        self.assertEqual(job['status'], 'done', job); self.assertTrue(job['restart'])
        self.assertEqual(self.head(), core['remote_head'])
        self.assertIn("1.1.0", (self.install / 'app/server.py').read_text())
        self.assertEqual(job['repos'][0]['from'], '1.0.0'); self.assertEqual(job['repos'][0]['to'], '1.1.0')
        self.assertEqual(len(list((self.env.BRIDGE / 'update' / 'runs').glob('*.json'))), 1)

    def test_failing_tests_roll_back(self):
        before = self.head()
        self.publish({'app/server.py': "VERSION = '1.2.0'\n", 'tests/test_ok.py': FAILING}, '有问题的版本')
        status = self.u.check()
        self.u.apply({'confirm': True, 'expect': self.expect(status)})
        job = self.wait()
        self.assertEqual(job['status'], 'failed'); self.assertTrue(job['rolled_back'])
        self.assertIn('测试没通过', job['error'])
        self.assertTrue(any('新版本坏了' in line for line in job['tail']))
        self.assertEqual(self.head(), before)
        self.assertIn("1.0.0", (self.install / 'app/server.py').read_text())
        self.assertEqual(git(self.install, 'status', '--porcelain'), '')
        with self.assertRaisesRegex(updater.UpdateError, '没有等着重启'):
            self.u.restart({'confirm': True})

    def test_local_changes_and_unpushed_commits_block_updates(self):
        self.publish({'app/server.py': "VERSION = '1.1.0'\n"}, '新版本')
        self.write(self.install, {'tests/test_ok.py': PASSING + '# 本机改的\n'})
        status = self.u.check()
        self.assertFalse(status['available'])
        self.assertIn('改过 1 个程序文件', status['repos'][0]['reason'])
        remote = status['repos'][0]['remote_head']
        with self.assertRaisesRegex(updater.UpdateError, '改过 1 个程序文件'):
            self.u.apply({'confirm': True, 'expect': {'core': remote}})
        self.commit(self.install, '本机的改动')                     # 改动提交了但没上传：两边都改过
        status = self.u.check()
        self.assertIn('1 个还没上传的改动', status['repos'][0]['reason'])
        with self.assertRaisesRegex(updater.UpdateError, '还没上传'):
            self.u.apply({'confirm': True, 'expect': {'core': remote}})

    def test_apply_needs_confirmation_and_the_checked_version(self):
        self.publish({'app/server.py': "VERSION = '1.1.0'\n"}, '新版本')
        status = self.u.check()
        with self.assertRaisesRegex(updater.UpdateError, '先确认'):
            self.u.apply({'expect': self.expect(status)})
        with self.assertRaisesRegex(updater.UpdateError, '先检查更新'):
            self.u.apply({'confirm': True})
        self.publish({'app/server.py': "VERSION = '1.1.1'\n"}, '又改了一次')
        self.u.check()
        with self.assertRaisesRegex(updater.UpdateError, '又有了新改动'):
            self.u.apply({'confirm': True, 'expect': self.expect(status)})

    def test_busy_app_is_not_interrupted(self):
        self.publish({'app/server.py': "VERSION = '1.1.0'\n"}, '新版本')
        status = self.u.check()
        self.env.transfer = SimpleNamespace(_job={'status': 'running'})
        with self.assertRaisesRegex(updater.UpdateError, '导出 / 导入正在进行'):
            self.u.apply({'confirm': True, 'expect': self.expect(status)})
        self.env.transfer = None
        plans = self.env.BRIDGE / 'cleanup-plans'; plans.mkdir(parents=True)
        bridge_state.atomic_json(plans / 'p1.json', {'status': 'running'})
        with self.assertRaisesRegex(updater.UpdateError, '清理正在进行'):
            self.u.apply({'confirm': True, 'expect': self.expect(status)})
        (plans / 'p1.json').unlink()
        self.env.plugins = SimpleNamespace(plugins={}, failed=[], busy_reasons=lambda: ['示例插件：还有任务'])
        with self.assertRaisesRegex(updater.UpdateError, '示例插件：还有任务'):
            self.u.apply({'confirm': True, 'expect': self.expect(status)})
        self.env.plugins = None
        held, release = threading.Event(), threading.Event()

        def hold():
            with bridge_state.operation_lock(self.env.BRIDGE / 'operation.lock'):
                held.set(); release.wait(10)
        thread = threading.Thread(target=hold); thread.start(); held.wait(5)
        try:
            with self.assertRaisesRegex(updater.UpdateError, '正在做别的操作'):
                self.u.apply({'confirm': True, 'expect': self.expect(status)})
        finally:
            release.set(); thread.join()
        self.u.apply({'confirm': True, 'expect': self.expect(status)})
        self.assertEqual(self.wait()['status'], 'done')

    def test_dependency_change_reinstalls_and_rollback_reinstalls_again(self):
        calls = []
        real_run = subprocess.run

        def runner(argv, **kw):
            if argv[0] == '/bin/bash':
                calls.append(Path(argv[1]).name)
                return subprocess.CompletedProcess(argv, 0, '', '')
            return real_run(argv, **kw)
        self.u.runner = runner
        self.publish({'app/requirements-desktop.txt': 'pywebview==2.0\n'}, '升级依赖')
        self.u.apply({'confirm': True, 'expect': self.expect(self.u.check())})
        self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual(calls, ['repair-runtime.sh'])
        self.publish({'app/requirements-desktop.txt': 'pywebview==3.0\n', 'tests/test_ok.py': FAILING}, '依赖和坏测试')
        self.u.apply({'confirm': True, 'expect': self.expect(self.u.check())})
        job = self.wait()
        self.assertEqual(job['status'], 'failed'); self.assertTrue(job['rolled_back'])
        self.assertEqual(calls, ['repair-runtime.sh'] * 3)           # 更新时装一次，退回后按原来的清单再装一次
        self.assertEqual((self.install / 'app/requirements-desktop.txt').read_text(), 'pywebview==2.0\n')

    def test_plugin_folders_update_too(self):
        base = Path(self.tmp.name)
        p_origin, p_dev = base / 'plugin.git', base / 'plugin-dev'
        git(base, 'init', '--bare', '-b', 'main', str(p_origin))
        git(base, 'clone', '--quiet', str(p_origin), str(p_dev))
        self.write(p_dev, {'plugin.json': json.dumps({'id': 'demo-x', 'name': '示例', 'version': '1.0.0'}),
                           'tests/__init__.py': '', 'tests/test_ok.py': PASSING})
        self.commit(p_dev, '插件第一版'); git(p_dev, 'push', '--quiet', '-u', 'origin', 'main')
        p_install = self.install / 'plugins' / 'demo'
        git(base, 'clone', '--quiet', str(p_origin), str(p_install))
        self.env.plugins = SimpleNamespace(plugins={'demo-x': SimpleNamespace(id='demo-x', name='示例', folder=p_install)},
                                           failed=[], busy_reasons=lambda: [])
        self.publish({'plugin.json': json.dumps({'id': 'demo-x', 'name': '示例', 'version': '1.1.0'})}, '插件新版本', repo=p_dev)
        status = self.u.check()
        by_key = {r['key']: r for r in status['repos']}
        self.assertEqual(sorted(by_key), ['core', 'plugin:demo-x'])
        self.assertEqual((by_key['plugin:demo-x']['version'], by_key['plugin:demo-x']['remote_version']), ('1.0.0', '1.1.0'))
        self.assertEqual(by_key['core']['behind'], 0)
        self.u.apply({'confirm': True, 'expect': self.expect(status)})
        self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual(json.loads((p_install / 'plugin.json').read_text())['version'], '1.1.0')

    def test_web_uploaded_repo_without_exec_bits_still_updates(self):
        # 经 GitHub 网页上传的仓库不记可执行权限：本机 chmod 过的启动器不算"改过"，更新后启动器仍可执行。
        launcher = '会话桥.app/Contents/MacOS/会话桥'
        self.publish({launcher: '#!/bin/bash\necho v1\n'}, '加启动器')
        self.u.apply({'confirm': True, 'expect': self.expect(self.u.check())})
        self.assertEqual(self.wait()['status'], 'done')
        path = self.install / launcher
        self.assertEqual(git(self.install, 'ls-files', '-s', launcher).split()[0], '100644')
        path.chmod(0o755)                                          # install.sh 补上的
        self.publish({launcher: '#!/bin/bash\necho v2\n', 'app/server.py': "VERSION = '1.1.0'\n"}, '启动器也改了')
        status = self.u.check()
        self.assertTrue(status['available'], status['repos'][0]['reason'])
        self.u.apply({'confirm': True, 'expect': self.expect(status)})
        self.assertEqual(self.wait()['status'], 'done')
        self.assertIn('v2', path.read_text())
        self.assertTrue(os.access(path, os.X_OK))

    def test_auto_check_respects_the_switch(self):
        bridge_state.atomic_json(self.env.CONFIG, {'update': {'auto_check': False}})
        self.assertFalse(self.u.status()['auto_check'])
        bridge_state.atomic_json(self.env.CONFIG, {})
        self.assertTrue(self.u.status()['auto_check'])

    def test_error_text_never_shows_credentials(self):
        self.assertEqual(updater.clean('fatal: unable to access https://someone:sekret@github.com/x/y.git/'),
                         'fatal: unable to access https://github.com/x/y.git/')


if __name__ == '__main__':
    unittest.main()
