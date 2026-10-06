"""各系统上放应用数据的位置，以及「在文件夹里显示」。

macOS 是 ~/Library/Application Support，Windows 是 %APPDATA%（~/AppData/Roaming）。
Claude 桌面版在 Windows 上是 MSIX 安装包，数据实际在 %LOCALAPPDATA%\\Packages 下面，见 claude_data_dir。
四个工具自己的会话（~/.claude、~/.codex、~/.zcode、~/.workbuddy）两边位置一样，不在这里。
"""
import os
from pathlib import Path
import subprocess

WINDOWS = os.name == 'nt'
# 怎么把 Claude 桌面版完全退出：Windows 上关窗口只是缩到托盘
QUIT_CLAUDE = '在右下角托盘里右键 Claude 图标、选「退出」' if WINDOWS else '在桌面版里按 ⌘Q 完全退出'


def app_support(home):
    """应用数据目录；跟着传进来的 home 走，测试里的假 home 也一样适用。"""
    return Path(home) / ('AppData/Roaming' if WINDOWS else 'Library/Application Support')


def claude_data_dir(home, windows=WINDOWS):
    """Claude 桌面版的数据目录。

    Windows 上桌面版是 MSIX 安装包：它写 %APPDATA%\\Claude 时，系统会把文件重定向到
    %LOCALAPPDATA%\\Packages\\Claude_<发布者编号>\\LocalCache\\Roaming\\Claude，真正的数据在那里。
    两处都看：哪处有侧栏条目用哪处，都没有时优先用安装包的位置（装了桌面版就一定有这个文件夹）。
    """
    home = Path(home)
    if not windows:
        return home / 'Library/Application Support/Claude'
    roaming = home / 'AppData/Roaming/Claude'
    packages = home / 'AppData/Local/Packages'
    packaged = [packages / 'Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude']
    packaged += [p for p in sorted(packages.glob('Claude_*/LocalCache/Roaming/Claude')) if p not in packaged]
    for d in packaged + [roaming]:
        if (d / 'claude-code-sessions').is_dir():
            return d
    for d in packaged:
        if d.parent.parent.parent.is_dir():      # Packages\Claude_xxx 在，说明装的是 MSIX 版
            return d
    return roaming


def claude_meta_root(home, windows=WINDOWS):
    """Claude 桌面版侧栏条目：<数据目录>/claude-code-sessions/<账号>/<组织>/local_*.json"""
    return claude_data_dir(home, windows) / 'claude-code-sessions'


def reveal(path):
    """在访达 / 资源管理器里打开所在文件夹，并选中这个文件。"""
    if WINDOWS:
        subprocess.run(['explorer.exe', '/select,', str(path)], check=False, timeout=10)
    else:
        subprocess.run(['/usr/bin/open', '-R', str(path)], check=False, timeout=10)
