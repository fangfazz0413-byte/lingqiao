"""各系统上放应用数据的位置，以及「在文件夹里显示」。

macOS 是 ~/Library/Application Support，Windows 是 %APPDATA%（~/AppData/Roaming）。
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


def claude_meta_root(home):
    """Claude 桌面版侧栏条目：…/Claude/claude-code-sessions/<账号>/<组织>/local_*.json

    第三方壳（汉化版等）的数据目录可能叫 Claude-3p / local.claude.desktop.zh.cn 之类，
    目录名以 Claude 开头的都扫一遍；官方的 Claude 优先，找不到任何已存在的就返回官方路径。
    """
    support = app_support(home)
    official = support / 'Claude' / 'claude-code-sessions'
    if official.is_dir():
        return official
    if support.is_dir():
        for candidate in sorted(support.glob('Claude*/claude-code-sessions')):
            if candidate.is_dir():
                return candidate
    return official


def reveal(path):
    """在访达 / 资源管理器里打开所在文件夹，并选中这个文件。"""
    if WINDOWS:
        subprocess.run(['explorer.exe', '/select,', str(path)], check=False, timeout=10)
    else:
        subprocess.run(['/usr/bin/open', '-R', str(path)], check=False, timeout=10)
