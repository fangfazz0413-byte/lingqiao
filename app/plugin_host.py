"""本机插件：plugins/<文件夹>/plugin.json + 一个 Python 后端模块 + 可选的页面脚本和样式。

插件和灵桥跑在同一个进程里，权限和灵桥一样，只装自己信得过的。某个插件装坏了（清单不对、
导入出错、创建失败），只记下原因、在侧栏显示成灰色，灵桥其他功能照常。

plugin.json：
{
  "id": "demo",                        小写字母开头，字母、数字、"-"，2–32 位；也是侧栏分组名和接口前缀 /api/<id>/
  "name": "示例插件",
  "version": "1.0.0",
  "min_core": "3.5.0",                 可选：需要的最低灵桥版本
  "backend": "plugin.py",              插件文件夹里的模块文件，要有 create(env, folder)
  "assets": ["demo.js", "demo.css"],   页面要用的文件（只能是 .js / .css，放在插件文件夹里）
  "frontend": {"global": "DemoPlugin", "script": "demo.js", "style": "demo.css"},
  "nav": {"section": "插件", "label": "示例", "symbol": "◇", "title": "鼠标停留时的说明"}
}

create() 返回的对象：
  handle_get(path, query)  -> dict，或 ('json', dict, None) / ('file', bytes, content_type)
  handle_post(path, body)  -> dict
  status()      可选，{"available": bool, "reason": str}，不可用时侧栏变灰
  busy_reason() 可选，返回非空字符串表示正在忙（检查更新时不打断）
  shutdown()    可选，灵桥退出时调用
抛出的异常带 confirm 属性时，接口回 409，页面可以请用户再确认一次。
"""
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading

ID_RE = re.compile(r'[a-z][a-z0-9-]{1,31}\Z')
MODULE_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,40}\.py\Z')
ASSET_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.(js|css)\Z')
GLOBAL_RE = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]{0,40}\Z')
VERSION_RE = re.compile(r'\d{1,4}(\.\d{1,4}){0,3}\Z')
# 灵桥自己的侧栏分组和接口名，插件不能占用。
RESERVED = frozenset({
    'all', 'claude', 'codex', 'zcode', 'workbuddy', 'usage', 'trash', 'accounts', 'transfer', 'cleanup',
    'sessions', 'session', 'keystatus', 'keys', 'health', 'window', 'shutdown', 'sync-one', 'delete',
    'restore', 'plugins', 'static', 'assets', 'update', 'index'})
MAX_MANIFEST = 64 * 1024
MAX_ASSETS = 10
TYPES = {'.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8'}


def _same_folder(file, folder):
    if not file:
        return False
    try:
        return Path(file).resolve().parent == Path(folder).resolve()
    except (OSError, RuntimeError):
        return False


def version_tuple(text):
    return tuple(int(part) for part in str(text).split('.'))


def _text(value, limit, field, required=False):
    if value is None and not required:
        return ''
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(ch) < 32 for ch in value):
        raise ValueError(f'plugin.json 的 {field} 不对（要 1–{limit} 个字，不能有控制字符）')
    return value.strip()


class Plugin:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.id = ''
        self.name = folder.name if isinstance(folder, Path) else str(folder)
        self.version = ''
        self.assets = ()
        self.frontend = {}
        self.nav = {}
        self.backend = None
        self.error = ''

    def public(self):
        data = {'id': self.id, 'name': self.name, 'version': self.version, 'nav': dict(self.nav),
                'frontend': dict(self.frontend), 'error': self.error, 'status': {'available': not self.error, 'reason': self.error}}
        if self.backend is not None and not self.error and callable(getattr(self.backend, 'status', None)):
            try:
                status = self.backend.status()
                if isinstance(status, dict) and isinstance(status.get('available'), bool):
                    data['status'] = {'available': status['available'], 'reason': str(status.get('reason') or '')[:200]}
            except Exception as exc:  # noqa: BLE001  状态查不出来也不影响列表
                data['status'] = {'available': False, 'reason': '状态查不出来（' + type(exc).__name__ + '）'}
        return data


class PluginHost:
    def __init__(self, env, root=None):
        self.env = env
        self.root = Path(root) if root is not None else Path(env.REPO) / 'plugins'
        self.plugins = {}
        self.failed = []
        self.loaded = False
        self.lock = threading.RLock()

    # ------------------------------------------------------------ 发现和加载
    def folders(self):
        if not self.root.is_dir():
            return []
        return sorted(p for p in self.root.iterdir()
                      if p.is_dir() and not p.name.startswith(('.', '_')) and (p / 'plugin.json').is_file())

    def load(self):
        with self.lock:
            if self.loaded:
                return self.public()
            self.loaded = True
            for folder in self.folders():
                plugin = Plugin(folder)
                try:
                    self._load_one(plugin)
                    self.plugins[plugin.id] = plugin
                    self.env.log('plugin-loaded id=' + plugin.id)
                except Exception as exc:  # noqa: BLE001  坏插件只记原因，不拖垮灵桥
                    plugin.error = (str(exc) or type(exc).__name__)[:200]
                    plugin.backend = None
                    self.failed.append(plugin)
                    self.env.log('plugin-failed folder=' + folder.name + ' type=' + type(exc).__name__)
            return self.public()

    def _manifest(self, plugin):
        path = plugin.folder / 'plugin.json'
        raw = path.read_bytes()
        if len(raw) > MAX_MANIFEST:
            raise ValueError('plugin.json 太大')
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError('plugin.json 不是合法的 JSON') from None
        if not isinstance(data, dict):
            raise ValueError('plugin.json 要是一个对象')
        return data

    def _load_one(self, plugin):
        data = self._manifest(plugin)
        pid = data.get('id')
        if not isinstance(pid, str) or not ID_RE.match(pid):
            raise ValueError('plugin.json 的 id 不对（小写字母开头，字母、数字、"-"，2–32 位）')
        plugin.id = pid
        plugin.name = _text(data.get('name'), 40, 'name', required=True)
        if pid in RESERVED:
            raise ValueError(f'id「{pid}」是灵桥自己用的名字，换一个')
        if pid in self.plugins:
            raise ValueError(f'id「{pid}」和已装的插件重复')
        version = data.get('version', '')
        if version and (not isinstance(version, str) or not VERSION_RE.match(version)):
            raise ValueError('plugin.json 的 version 不对')
        plugin.version = version
        need = data.get('min_core')
        if need is not None:
            if not isinstance(need, str) or not VERSION_RE.match(need):
                raise ValueError('plugin.json 的 min_core 不对')
            if version_tuple(need) > version_tuple(self.env.VERSION):
                raise ValueError(f'要灵桥 {need} 或更新的版本（现在是 {self.env.VERSION}）')
        assets = data.get('assets', [])
        if not isinstance(assets, list) or len(assets) > MAX_ASSETS or not all(isinstance(a, str) and ASSET_RE.match(a) for a in assets):
            raise ValueError('plugin.json 的 assets 不对（只能是插件文件夹里的 .js / .css 文件名）')
        for name in assets:
            self._asset_path(plugin.folder, name)
        plugin.assets = tuple(dict.fromkeys(assets))
        frontend = data.get('frontend') or {}
        if not isinstance(frontend, dict):
            raise ValueError('plugin.json 的 frontend 不对')
        plugin.frontend = {}
        if frontend:
            name = frontend.get('global')
            if not isinstance(name, str) or not GLOBAL_RE.match(name):
                raise ValueError('plugin.json 的 frontend.global 不对')
            plugin.frontend['global'] = name
            for key in ('script', 'style'):
                value = frontend.get(key)
                if value is None:
                    continue
                if value not in plugin.assets or not value.endswith('.js' if key == 'script' else '.css'):
                    raise ValueError(f'plugin.json 的 frontend.{key} 要是 assets 里的文件')
                plugin.frontend[key] = value
        nav = data.get('nav') or {}
        if not isinstance(nav, dict):
            raise ValueError('plugin.json 的 nav 不对')
        plugin.nav = {key: _text(nav.get(key), limit, 'nav.' + key)
                      for key, limit in (('section', 20), ('label', 30), ('symbol', 4), ('title', 120)) if nav.get(key) is not None}
        backend = data.get('backend')
        if not isinstance(backend, str) or not MODULE_RE.match(backend):
            raise ValueError('plugin.json 的 backend 要是插件文件夹里的 .py 文件名')
        entry = plugin.folder / backend
        if entry.is_symlink() or not entry.is_file():
            raise ValueError(f'找不到 {backend}')
        self._check_names(plugin.folder, skip=entry.name)
        folder = str(plugin.folder)
        if folder not in sys.path:
            sys.path.append(folder)   # 放在最后：插件的模块挡不住标准库和灵桥自己的模块
        module_name = 'lingqiao_plugin_' + pid.replace('-', '_')
        spec = importlib.util.spec_from_file_location(module_name, entry)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        create = getattr(module, 'create', None)
        if not callable(create):
            raise ValueError(f'{backend} 里没有 create(env, folder)')
        instance = create(self.env, plugin.folder)
        for method in ('handle_get', 'handle_post'):
            if not callable(getattr(instance, method, None)):
                raise ValueError(f'插件对象缺少 {method}')
        plugin.backend = instance

    @staticmethod
    def _check_names(folder, skip=''):
        """插件文件夹里的模块名不能和已有模块重名，不然 import 会拿错文件。

        入口文件按路径加载、用 lingqiao_plugin_<id> 这个名字，不参与检查（几个插件都叫 plugin.py 也没事）。
        """
        for path in sorted(folder.glob('*.py')):
            if path.name == skip:
                continue
            stem = path.stem
            if stem in sys.modules:
                if _same_folder(getattr(sys.modules[stem], '__file__', None), folder):
                    continue
                raise ValueError(f'{path.name} 和灵桥或 Python 自带的模块重名，改个名字')
            try:
                spec = importlib.util.find_spec(stem)
            except (ImportError, ValueError):
                spec = None
            if spec is not None and not _same_folder(spec.origin, folder):
                raise ValueError(f'{path.name} 和灵桥或 Python 自带的模块重名，改个名字')

    @staticmethod
    def _asset_path(folder, name):
        if not ASSET_RE.match(name):
            raise ValueError('不存在的文件')
        path = folder / name
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError(f'找不到 {name}') from None
        if real.parent != folder.resolve() or not real.is_file():
            raise ValueError(f'{name} 要是插件文件夹里的普通文件')
        return real

    # ------------------------------------------------------------ 给接口用
    def public(self):
        with self.lock:
            items = [p.public() for p in self.plugins.values()]
            items += [p.public() for p in self.failed if p.id and ID_RE.match(p.id) and p.id not in self.plugins and p.id not in RESERVED]
            return items

    def _owner(self, path):
        parts = path.split('/')
        if len(parts) < 4 or parts[0] != '' or parts[1] != 'api':
            return None
        plugin = self.plugins.get(parts[2])
        return plugin if plugin is not None and plugin.backend is not None else None

    def dispatch_get(self, path, query):
        plugin = self._owner(path)
        if plugin is None:
            return None
        result = plugin.backend.handle_get(path, query)
        if isinstance(result, tuple) and len(result) == 3 and result[0] in ('json', 'file'):
            kind, payload, ctype = result
            if kind == 'file' and not isinstance(payload, (bytes, bytearray)):
                raise TypeError('插件返回的文件内容不是 bytes')
            return kind, payload, ctype
        return 'json', result, None

    def dispatch_post(self, path, body):
        plugin = self._owner(path)
        if plugin is None:
            return None
        return ('json', plugin.backend.handle_post(path, body), None)

    def asset(self, plugin_id, name):
        plugin = self.plugins.get(plugin_id)
        if plugin is None or name not in plugin.assets:
            raise FileNotFoundError('不存在的文件')
        path = self._asset_path(plugin.folder, name)
        return path.read_bytes(), TYPES[path.suffix]

    def busy_reasons(self):
        reasons = []
        for plugin in list(self.plugins.values()):
            check = getattr(plugin.backend, 'busy_reason', None)
            if callable(check):
                try:
                    reason = check()
                except Exception as exc:  # noqa: BLE001
                    reason = '状态查不出来（' + type(exc).__name__ + '）'
                if reason:
                    reasons.append(plugin.name + '：' + str(reason)[:200])
        return reasons

    def shutdown(self):
        for plugin in list(self.plugins.values()):
            stop = getattr(plugin.backend, 'shutdown', None)
            if callable(stop):
                try:
                    stop()
                except Exception as exc:  # noqa: BLE001
                    self.env.log('plugin-shutdown-failed id=' + plugin.id + ' type=' + type(exc).__name__)
