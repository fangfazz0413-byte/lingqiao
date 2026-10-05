"""灵桥在 Windows 上的安装、补装依赖和重启（macOS 上对应 install.sh、repair-runtime.sh 和 会话桥.app）。

  install.bat                                    安装：建灵桥自己的运行环境、装依赖，在桌面和开始菜单放「灵桥」快捷方式
  python app\\windows_setup.py --repair           只补装依赖（检查更新时依赖清单变了会用到）
  python app\\windows_setup.py --restart-after N  等进程 N（旧的灵桥）退出后再打开灵桥（更新后重启用）

可以重复运行；不会动你的会话数据。
"""
import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / '.bridge' / 'runtime'
RUNTIME_PYTHON = RUNTIME / 'Scripts' / 'python.exe'
RUNTIME_PYTHONW = RUNTIME / 'Scripts' / 'pythonw.exe'
REQUIREMENTS = REPO / 'app' / 'requirements-windows.txt'
SERVER = REPO / 'app' / 'server.py'
ICON = REPO / 'app' / 'lingqiao.ico'
NAME = '灵桥'
MIN_PYTHON = (3, 13)          # 3.13 起 Windows 上才有 os.fchmod
WEBVIEW2 = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
WEBVIEW2_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/'
DETACHED = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)


def runtime_ok():
    if not RUNTIME_PYTHON.is_file():
        return False
    check = f'import ssl, sys; assert sys.version_info >= {MIN_PYTHON!r}'
    return subprocess.run([str(RUNTIME_PYTHON), '-c', check], capture_output=True).returncode == 0


def install_requirements(python):
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(REQUIREMENTS)], check=True)
    subprocess.run([str(python), '-c', 'import webview'], check=True)


def webview2_installed():
    """灵桥的窗口用 Edge WebView2 显示。Windows 11 自带，Windows 10 一般随 Edge 装好了。"""
    import winreg
    keys = [(winreg.HKEY_LOCAL_MACHINE, rf'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2}'),
            (winreg.HKEY_LOCAL_MACHINE, rf'SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2}'),
            (winreg.HKEY_CURRENT_USER, rf'Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2}')]
    for root, key in keys:
        try:
            with winreg.OpenKey(root, key) as handle:
                version = winreg.QueryValueEx(handle, 'pv')[0]
        except OSError:
            continue
        if version and version != '0.0.0.0':
            return True
    return False


class GUID(ctypes.Structure):
    _fields_ = [('raw', ctypes.c_ubyte * 16)]

    @classmethod
    def parse(cls, text):
        return cls((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le))


CLSID_SHELL_LINK = '00021401-0000-0000-C000-000000000046'
IID_SHELL_LINK_W = '000214F9-0000-0000-C000-000000000046'
IID_PERSIST_FILE = '0000010B-0000-0000-C000-000000000046'
FOLDERID_DESKTOP = 'B4BFCC3A-DB2C-424C-B029-7FE99A87C641'
FOLDERID_PROGRAMS = 'A77F5D77-2E2B-44C3-A6A2-ABA601054A51'   # 开始菜单「程序」


def _com(obj, index, restype, *argtypes):
    """取 COM 对象虚表里第 index 个方法。"""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def known_folder(folder_id):
    """桌面、开始菜单的真实位置（可能被 OneDrive 挪走了）。"""
    guid, path = GUID.parse(folder_id), ctypes.c_wchar_p()
    ctypes.OleDLL('shell32').SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path))
    try:
        return Path(path.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def create_shortcut(link, target, arguments, workdir, icon, description):
    """用 IShellLinkW 建快捷方式：名字、路径里有中文也行（WScript.Shell 按系统代码页转，英文系统上中文会变成 ?）。"""
    from ctypes import wintypes
    ole32 = ctypes.OleDLL('ole32')
    ole32.CoInitialize(None)
    clsid, iid_link, iid_persist = GUID.parse(CLSID_SHELL_LINK), GUID.parse(IID_SHELL_LINK_W), GUID.parse(IID_PERSIST_FILE)
    shell_link, persist = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid_link), ctypes.byref(shell_link))  # 进程内
        text = lambda index: _com(shell_link, index, ctypes.HRESULT, wintypes.LPCWSTR)  # noqa: E731
        text(20)(shell_link, str(target))          # SetPath
        text(11)(shell_link, arguments)            # SetArguments
        text(9)(shell_link, str(workdir))          # SetWorkingDirectory
        text(7)(shell_link, description)           # SetDescription
        _com(shell_link, 17, ctypes.HRESULT, wintypes.LPCWSTR, ctypes.c_int)(shell_link, str(icon), 0)       # SetIconLocation
        _com(shell_link, 0, ctypes.HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(         # QueryInterface
            shell_link, ctypes.byref(iid_persist), ctypes.byref(persist))
        _com(persist, 6, ctypes.HRESULT, wintypes.LPCWSTR, wintypes.BOOL)(persist, str(link), True)          # IPersistFile::Save
    finally:
        for obj in (persist, shell_link):
            if obj.value:
                _com(obj, 2, ctypes.c_ulong)(obj)  # Release
        ole32.CoUninitialize()


def make_shortcuts():
    """灵桥文件夹、桌面、开始菜单各放一个「灵桥」。返回建好的位置。"""
    folders = [REPO, known_folder(FOLDERID_DESKTOP), known_folder(FOLDERID_PROGRAMS)]
    made = []
    for folder in folders:
        link = folder / f'{NAME}.lnk'
        create_shortcut(link, RUNTIME_PYTHONW, f'-X utf8 "{SERVER}"', REPO, ICON, '灵桥 · AI 会话工作台')
        made.append(link)
    return made


def launch():
    subprocess.Popen([str(RUNTIME_PYTHONW), '-X', 'utf8', str(SERVER)], cwd=str(REPO), close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=DETACHED)


def wait_for_exit(pid):
    """Windows 不能用 kill -0 探测（os.kill 会直接结束进程），要拿句柄等它退出。"""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE；打不开说明已经退出了
    if handle:
        try:
            kernel32.WaitForSingleObject(handle, 120 * 1000)
        finally:
            kernel32.CloseHandle(handle)


def install():
    print('== 安装灵桥 ==')
    print(f'用这个 Python：{sys.executable}')
    if not runtime_ok():
        print(f'正在创建灵桥独立运行环境：{RUNTIME}')
        RUNTIME.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, '-m', 'venv', str(RUNTIME)], check=True)
    print('正在安装依赖（要联网，第一次可能要几分钟）…')
    install_requirements(RUNTIME_PYTHON)
    try:
        make_shortcuts()
        print('已在桌面、开始菜单和灵桥文件夹里放好「灵桥」快捷方式。')
    except OSError as exc:
        print(f'快捷方式没建成（{exc}），不影响使用：可以直接运行  "{RUNTIME_PYTHONW}" -X utf8 "{SERVER}"')
    if not webview2_installed():
        print(f'\n提示：没找到 Microsoft Edge WebView2，灵桥的窗口要用它。到这里下载「常青版引导程序」装上：{WEBVIEW2_URL}')
    print('\n装好了。双击桌面上的「灵桥」打开。')
    print('以后更新：灵桥侧栏底部点「检查更新」；或者在这个文件夹里运行 git pull，再重新打开灵桥。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='灵桥 Windows 安装 / 补装依赖 / 重启')
    parser.add_argument('--repair', action='store_true', help='只补装依赖')
    parser.add_argument('--restart-after', type=int, metavar='PID', help='等这个进程退出后打开灵桥')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        print('这个脚本只给 Windows 用；macOS 上运行 bash install.sh。')
        return 1
    if args.restart_after:
        wait_for_exit(args.restart_after)
        if args.repair:   # 更新时依赖清单变了：旧灵桥退出、文件不再占用，这时补装
            try:
                install_requirements(RUNTIME_PYTHON)
            except (OSError, subprocess.CalledProcessError):
                pass      # 补装没成功也照样打开；打不开时再运行一次 install.bat
        launch()
        return 0
    if sys.version_info < MIN_PYTHON:
        print(f'灵桥在 Windows 上需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更新的版本，现在是 {sys.version.split()[0]}。\n'
              '到 https://www.python.org/downloads/windows/ 下载安装（勾选「Add python.exe to PATH」），再运行一次 install.bat。')
        return 1
    try:
        if args.repair:
            install_requirements(RUNTIME_PYTHON if RUNTIME_PYTHON.is_file() else sys.executable)
            print('灵桥依赖已恢复。现在可以重新打开灵桥。')
        else:
            install()
    except subprocess.CalledProcessError as exc:
        print(f'\n没装成：{" ".join(map(str, exc.cmd[:4]))}… 出错（返回 {exc.returncode}）。看上面的输出找原因，处理后再运行一次。')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
