"""Windows 专用部分：凭据管理器、进程列表、锁、重启等待。不在 Windows 上的跳过，跨平台的部分两边都跑。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import account_sync  # noqa: E402
import bridge_state  # noqa: E402
import platform_paths  # noqa: E402

LOCK_PROBE = '''
import os, sys
sys.path.insert(0, sys.argv[1])
import bridge_state
fd = os.open(sys.argv[2], os.O_CREAT | os.O_RDWR)
sys.exit(0 if bridge_state.lock_fd(fd, blocking=False) else 3)
'''


class LockTests(unittest.TestCase):
    def test_lock_excludes_other_processes_until_released(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'operation.lock'
            probe = [sys.executable, '-c', LOCK_PROBE, str(ROOT / 'app'), str(path)]
            with bridge_state.file_lock(path):
                self.assertEqual(subprocess.run(probe).returncode, 3)
                with bridge_state.file_lock(path):        # 同一线程可重入
                    pass
                self.assertEqual(subprocess.run(probe).returncode, 3)
            self.assertEqual(subprocess.run(probe).returncode, 0)

    def test_atomic_json_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'a' / 'state.json'
            bridge_state.atomic_json(path, {'中文': 1})
            bridge_state.atomic_json(path, {'中文': 2})
            self.assertEqual(bridge_state.load_json(path), {'中文': 2})
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ['state.json'])


class PathTests(unittest.TestCase):
    def test_claude_meta_root_follows_platform(self):
        home = Path('/h')
        expected = ('AppData/Roaming' if os.name == 'nt' else 'Library/Application Support') + '/Claude/claude-code-sessions'
        self.assertEqual(platform_paths.claude_meta_root(home), home / expected)

    def test_desktop_detection_covers_both_platforms(self):
        def blocking(path):
            rows = account_sync.AccountSync(None, process_lister=lambda: [(1, path)]).claude_status()
            return rows['running']
        self.assertTrue(blocking('/Applications/Claude.app/Contents/MacOS/Claude'))
        self.assertTrue(blocking(r'C:\Users\x\AppData\Local\AnthropicClaude\app-1.2.3\claude.exe'))
        self.assertFalse(blocking(r'C:\Users\x\.local\bin\claude.exe'))
        self.assertFalse(blocking(r'C:\Users\x\AppData\Roaming\Claude\claude-code\2.1\claude.exe'))
        self.assertFalse(blocking('/Users/x/Library/Application Support/Claude/claude-code/2.1/claude.app/Contents/MacOS/claude'))


@unittest.skipUnless(os.name == 'nt', '只在 Windows 上跑')
class WindowsOnlyTests(unittest.TestCase):
    def test_credential_manager_round_trip(self):
        import wincred
        target = 'lingqiao-test-' + uuid.uuid4().hex
        try:
            self.assertIsNone(wincred.read(target))
            wincred.write(target, 'tester', '密钥-1'.encode('utf-8'))
            self.assertEqual(wincred.read(target).decode('utf-8'), '密钥-1')
            wincred.write(target, 'tester', b'key-2')
            self.assertEqual(wincred.read(target), b'key-2')
        finally:
            wincred.delete(target)
        self.assertIsNone(wincred.read(target))
        wincred.delete(target)                             # 没有也不报错

    def test_process_list_has_full_paths(self):
        rows = dict(account_sync.list_processes())
        self.assertEqual(Path(rows[os.getpid()]).resolve(), Path(sys.executable).resolve())

    def test_wait_for_exit_returns_after_process_ends(self):
        import windows_setup
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1)'])
        started = time.monotonic()
        windows_setup.wait_for_exit(child.pid)
        self.assertGreater(time.monotonic() - started, 0.5)
        self.assertIsNotNone(child.wait(5))
        started = time.monotonic()
        windows_setup.wait_for_exit(child.pid)             # 已经退出：立刻返回
        self.assertLess(time.monotonic() - started, 2)

    def test_updater_finds_git(self):
        import updater
        from types import SimpleNamespace
        git = updater.Updater(SimpleNamespace()).git_path()
        self.assertTrue(git and os.path.isfile(git), git)


if __name__ == '__main__':
    unittest.main()
