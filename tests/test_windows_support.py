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
        self.assertEqual(platform_paths.claude_meta_root(home, windows=False), home / 'Library/Application Support/Claude/claude-code-sessions')
        self.assertEqual(platform_paths.claude_meta_root(home, windows=True), home / 'AppData/Roaming/Claude/claude-code-sessions')

    def test_claude_meta_root_finds_msix_install_on_windows(self):
        # Windows 上桌面版是 MSIX：%APPDATA% 被重定向到 %LOCALAPPDATA%\Packages\Claude_…\LocalCache\Roaming
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            msix = home / 'AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude'
            (home / 'AppData/Roaming/Claude').mkdir(parents=True)              # 只有空壳，没有侧栏条目
            (msix.parent.parent.parent).mkdir(parents=True)                  # 装了 MSIX 版，还没开过 Claude Code
            self.assertEqual(platform_paths.claude_meta_root(home, windows=True), msix / 'claude-code-sessions')
            (msix / 'claude-code-sessions/acct/org').mkdir(parents=True)
            self.assertEqual(platform_paths.claude_meta_root(home, windows=True), msix / 'claude-code-sessions')
        with tempfile.TemporaryDirectory() as folder:                          # 老的安装方式：数据就在 %APPDATA%
            home = Path(folder)
            (home / 'AppData/Roaming/Claude/claude-code-sessions').mkdir(parents=True)
            self.assertEqual(platform_paths.claude_meta_root(home, windows=True), home / 'AppData/Roaming/Claude/claude-code-sessions')

    def test_desktop_detection_covers_both_platforms(self):
        def blocking(path):
            rows = account_sync.AccountSync(None, process_lister=lambda: [(1, path)]).claude_status()
            return rows['running']
        self.assertTrue(blocking('/Applications/Claude.app/Contents/MacOS/Claude'))
        self.assertTrue(blocking(r'C:\Users\x\AppData\Local\AnthropicClaude\app-1.2.3\claude.exe'))
        self.assertFalse(blocking(r'C:\Users\x\.local\bin\claude.exe'))
        self.assertFalse(blocking(r'C:\Users\x\AppData\Roaming\Claude\claude-code\2.1\claude.exe'))
        self.assertFalse(blocking('/Users/x/Library/Application Support/Claude/claude-code/2.1/claude.app/Contents/MacOS/claude'))


class ServerPortTests(unittest.TestCase):
    def test_second_server_cannot_take_the_same_port(self):
        import server
        from http.server import BaseHTTPRequestHandler
        first = server.BridgeHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        try:
            with self.assertRaises(OSError):
                server.BridgeHTTPServer(('127.0.0.1', first.server_address[1]), BaseHTTPRequestHandler).server_close()
        finally:
            first.server_close()


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

    def test_shortcut_keeps_chinese_name_and_paths(self):
        import ctypes
        from ctypes import wintypes
        import windows_setup as ws
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / '灵桥 测试'
            base.mkdir()
            target = base / '程序.exe'
            target.write_bytes(b'')
            link = base / '灵桥.lnk'
            arguments = '-X utf8 "C:\\灵桥\\app\\server.py"'
            ws.create_shortcut(link, target, arguments, base, ws.ICON, '灵桥 · 测试')
            self.assertTrue(link.is_file())
            ole32 = ctypes.OleDLL('ole32')
            ole32.CoInitialize(None)
            shell_link, persist = ctypes.c_void_p(), ctypes.c_void_p()
            clsid, iid_link, iid_persist = (ws.GUID.parse(g) for g in (ws.CLSID_SHELL_LINK, ws.IID_SHELL_LINK_W, ws.IID_PERSIST_FILE))
            try:
                ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid_link), ctypes.byref(shell_link))
                ws._com(shell_link, 0, ctypes.HRESULT, ctypes.POINTER(ws.GUID), ctypes.POINTER(ctypes.c_void_p))(
                    shell_link, ctypes.byref(iid_persist), ctypes.byref(persist))
                ws._com(persist, 5, ctypes.HRESULT, wintypes.LPCWSTR, wintypes.DWORD)(persist, str(link), 0)       # Load
                buffer = ctypes.create_unicode_buffer(1024)
                ws._com(shell_link, 3, ctypes.HRESULT, wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)(
                    shell_link, buffer, 1024, None, 0)                                                            # GetPath
                self.assertEqual(Path(buffer.value).resolve(), target.resolve())
                ws._com(shell_link, 10, ctypes.HRESULT, wintypes.LPWSTR, ctypes.c_int)(shell_link, buffer, 1024)  # GetArguments
                self.assertEqual(buffer.value, arguments)
            finally:
                for obj in (persist, shell_link):
                    if obj.value:
                        ws._com(obj, 2, ctypes.c_ulong)(obj)
                ole32.CoUninitialize()

    def test_updater_finds_git(self):
        import updater
        from types import SimpleNamespace
        git = updater.Updater(SimpleNamespace()).git_path()
        self.assertTrue(git and os.path.isfile(git), git)


if __name__ == '__main__':
    unittest.main()
