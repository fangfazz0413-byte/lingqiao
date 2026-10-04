"""工具图标：从这台 Mac 上装好的官方 App 里提取，不随代码分发。

图标是各家的商标和作品，所以仓库里不放这些图片。第一次启动灵桥（或运行 install.sh）时，用 macOS
自带的 sips 把已装 App 的图标转成 64×64 的 PNG，放在 app/assets/icons/（不进 git）。没装的工具，
页面上显示一个字母徽标。

单独运行：python3 app/brand_icons.py   —— 补齐缺的图标，打印结果。
"""
import html
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

# names：在 /Applications、~/Applications 里按这些名字找；bundle：找不到时用 Spotlight 按 bundle id 找；
# resource：App 包里 Resources 下的图标文件，不写就用 Info.plist 的 CFBundleIconFile。
APPS = {
    'claude': {'names': ('Claude.app',), 'bundle': 'com.anthropic.claudefordesktop', 'resource': 'electron.icns'},
    'codex': {'names': ('Codex.app', 'ChatGPT.app'), 'bundle': 'com.openai.codex', 'resource': 'icon-codex-dark-color.png'},
    'codex-light': {'names': ('Codex.app', 'ChatGPT.app'), 'bundle': 'com.openai.codex', 'resource': 'icon-codex-light.png'},
    'openai': {'names': ('ChatGPT.app',), 'bundle': 'com.openai.chat', 'resource': 'icon-chatgpt.png'},
    'zcode': {'names': ('ZCode.app',), 'bundle': 'dev.zcode.app'},
    'workbuddy': {'names': ('WorkBuddy.app',), 'bundle': 'com.tencent.workbuddy.mac'},
    'kimi': {'names': ('Kimi.app',)},
    'cc-switch': {'names': ('CC Switch.app',)},
}
# 字母徽标：没有对应 App（GLM、MiniMax、Gemini 只有网站）或还没提取出来时用。
BADGES = {
    'claude': ('C', '#c4704f'), 'codex': ('Cx', '#2f6f5e'), 'codex-light': ('Cx', '#2f6f5e'), 'openai': ('AI', '#2f6f5e'),
    'zcode': ('Z', '#3d5a99'), 'workbuddy': ('W', '#2a7fb8'), 'kimi': ('K', '#3b3b46'), 'glm': ('G', '#4a5bd4'),
    'minimax': ('M', '#c2415a'), 'gemini': ('G', '#4b7be5'), 'cc-switch': ('CC', '#6a5acd'),
}
SIZE = 64


def badge_svg(name):
    label, color = BADGES.get(name, ((name[:1] or '?').upper(), '#7b8494'))
    size = 26 if len(label) == 1 else 21
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
            f'<rect width="64" height="64" rx="14" fill="{color}"/>'
            f'<text x="32" y="{32 + size * 0.36:.0f}" font-family="-apple-system,Helvetica,Arial,sans-serif" '
            f'font-size="{size}" font-weight="700" text-anchor="middle" fill="#ffffff">{html.escape(label)}</text></svg>')


def _app_dirs(home):
    return (Path('/Applications'), Path(home) / 'Applications')


def _spotlight(bundle):
    try:
        out = subprocess.run(['/usr/bin/mdfind', f'kMDItemCFBundleIdentifier == "{bundle}"'], capture_output=True,
                             text=True, timeout=5, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [Path(line) for line in out.splitlines() if line.endswith('.app')]


def find_source(name, home=None, spotlight=True):
    spec = APPS.get(name)
    if not spec:
        return None
    apps = [d / n for d in _app_dirs(home or Path.home()) for n in spec['names']]
    if spotlight and spec.get('bundle'):
        apps += _spotlight(spec['bundle'])
    for app in apps:
        resources = app / 'Contents' / 'Resources'
        if not resources.is_dir():
            continue
        candidates = []
        if spec.get('resource'):
            candidates.append(resources / spec['resource'])
        else:
            try:
                with open(app / 'Contents' / 'Info.plist', 'rb') as stream:
                    icon = plistlib.load(stream).get('CFBundleIconFile') or ''
            except (OSError, ValueError, plistlib.InvalidFileException):
                icon = ''
            if icon:
                candidates.append(resources / (icon if Path(icon).suffix else icon + '.icns'))
        for path in candidates:
            if path.is_file():
                return path
    return None


def render(source, target):
    """sips 转成 64×64 PNG，先写临时文件再换上，不留半截文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.icon-', suffix='.png', dir=target.parent)
    os.close(fd)
    try:
        result = subprocess.run(['/usr/bin/sips', '-s', 'format', 'png', '-Z', str(SIZE), str(source), '--out', tmp],
                                capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        if result.returncode != 0 or os.path.getsize(tmp) == 0:
            raise OSError('sips 转换失败')
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ensure(folder, home=None, spotlight=True):
    """补齐缺的图标，已有的不动。返回 {名字: 'ok' | 'exists' | 'not-installed' | 'failed'}。"""
    folder = Path(folder)
    result = {}
    for name in APPS:
        target = folder / f'{name}.png'
        if target.is_file():
            result[name] = 'exists'
            continue
        source = find_source(name, home, spotlight)
        if source is None:
            result[name] = 'not-installed'
            continue
        try:
            render(source, target)
            result[name] = 'ok'
        except (OSError, subprocess.TimeoutExpired):
            result[name] = 'failed'
    return result


def ensure_quietly(folder, log=None):
    try:
        result = ensure(folder)
        if log:
            made = sum(v == 'ok' for v in result.values())
            if made:
                log('brand-icons rendered=' + str(made))
    except Exception as exc:  # noqa: BLE001  图标只是装饰，出错不影响启动
        if log:
            log('brand-icons-failed type=' + type(exc).__name__)


if __name__ == '__main__':
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / 'assets' / 'icons'
    labels = {'ok': '已提取', 'exists': '已有', 'not-installed': '没装（显示字母徽标）', 'failed': '提取失败（显示字母徽标）'}
    for key, value in ensure(target).items():
        print(f'  {key}: {labels[value]}')
