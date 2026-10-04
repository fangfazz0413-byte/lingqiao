"""插件位：plugins/<文件夹>/plugin.json 的加载、接口转发、页面文件和出错隔离。

全用临时文件夹里的假插件，不碰真实会话和真实插件。
"""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app')); sys.path.insert(0, str(ROOT / '.bridge'))
import server as s  # noqa: E402
import plugin_host  # noqa: E402

DEMO = '''
class Desk:
    def __init__(self, env, folder):
        self.env, self.folder, self.stopped, self.busy = env, folder, False, ''
    def handle_get(self, path, query):
        if path == '/api/demo-x/hello':
            return {'hello': (query.get('name') or [''])[0]}
        if path == '/api/demo-x/file':
            return ('file', b'PNG-demo', 'image/png')
        raise FileNotFoundError('不存在的接口')
    def handle_post(self, path, body):
        if path == '/api/demo-x/echo':
            return {'echo': body}
        if path == '/api/demo-x/ask':
            error = ValueError('要再确认一次'); error.confirm = 'again'; error.detail = {'n': 1}
            raise error
        raise FileNotFoundError('不存在的接口')
    def status(self):
        return {'available': False, 'reason': '硬盘没接'}
    def busy_reason(self):
        return self.busy
    def shutdown(self):
        self.stopped = True

def create(env, folder):
    return Desk(env, folder)
'''

GOOD = {'id': 'demo-x', 'name': '示例插件', 'version': '1.2.0', 'min_core': '3.5.0', 'backend': 'plugin.py',
        'assets': ['demo.js', 'demo.css'], 'frontend': {'global': 'DemoPlugin', 'script': 'demo.js', 'style': 'demo.css'},
        'nav': {'section': '示例区', 'label': '<b>示例</b>', 'symbol': '◇', 'title': '示例插件的说明'}}


def make_plugin(root, folder, manifest, backend=DEMO, files=None):
    path = Path(root) / folder
    path.mkdir(parents=True)
    raw = manifest if isinstance(manifest, str) else json.dumps(manifest, ensure_ascii=False)
    (path / 'plugin.json').write_text(raw, encoding='utf-8')
    if backend is not None:
        (path / 'plugin.py').write_text(backend, encoding='utf-8')
    for name, text in (files if files is not None else {'demo.js': 'window.DemoPlugin = {};', 'demo.css': '.demo{}'}).items():
        (path / name).write_text(text, encoding='utf-8')
    return path


class PluginCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'plugins'
        self.root.mkdir()
        self.logs = []
        self.env = SimpleNamespace(REPO=self.root.parent, VERSION='3.5.0', log=self.logs.append)
        self.path_before = list(sys.path)

    def tearDown(self):
        sys.path[:] = self.path_before
        self.tmp.cleanup()

    def host(self):
        host = plugin_host.PluginHost(self.env, root=self.root)
        host.load()
        return host


class LoadingTests(PluginCase):
    def test_valid_plugin_is_listed_with_its_status(self):
        make_plugin(self.root, 'demo', GOOD)
        host = self.host()
        self.assertEqual(list(host.plugins), ['demo-x']); self.assertEqual(host.failed, [])
        [item] = host.public()
        self.assertEqual((item['id'], item['name'], item['version'], item['error']), ('demo-x', '示例插件', '1.2.0', ''))
        self.assertEqual(item['nav'], GOOD['nav']); self.assertEqual(item['frontend'], GOOD['frontend'])
        self.assertEqual(item['status'], {'available': False, 'reason': '硬盘没接'})
        self.assertIn('plugin-loaded id=demo-x', self.logs)
        self.assertIn(str(self.root / 'demo'), sys.path); self.assertNotEqual(sys.path[0], str(self.root / 'demo'))  # 放在最后

    def test_no_plugins_folder_means_no_plugins(self):
        self.root.rmdir()
        self.assertEqual(self.host().public(), [])

    def test_broken_plugins_are_reported_and_the_rest_still_load(self):
        make_plugin(self.root, 'a-json', '{不是 json')
        make_plugin(self.root, 'b-reserved', {**GOOD, 'id': 'usage'})
        make_plugin(self.root, 'c-dup', GOOD)
        make_plugin(self.root, 'c-good', GOOD)          # 文件夹按名字排序：c-dup 先加载，c-good 撞名
        make_plugin(self.root, 'd-too-new', {**GOOD, 'id': 'too-new', 'min_core': '9.0.0'})
        make_plugin(self.root, 'e-no-create', {**GOOD, 'id': 'no-create'}, backend='x = 1\n')
        make_plugin(self.root, 'f-shadow', {**GOOD, 'id': 'shadow'}, files={'demo.js': '', 'demo.css': '', 'json.py': ''})
        make_plugin(self.root, 'g-crash', {**GOOD, 'id': 'crash-x'}, backend='def create(env, folder):\n    raise RuntimeError("创建时出错")\n')
        make_plugin(self.root, 'h-escape', {**GOOD, 'id': 'escape', 'assets': ['../x.js'], 'frontend': {}})
        make_plugin(self.root, 'i-missing', {**GOOD, 'id': 'missing', 'assets': ['nothere.js'], 'frontend': {}})
        outside = Path(self.tmp.name) / 'outside.js'; outside.write_text('secret')
        link = make_plugin(self.root, 'j-link', {**GOOD, 'id': 'link-x', 'assets': ['demo.js'], 'frontend': {}}, files={})
        os.symlink(outside, link / 'demo.js')
        make_plugin(self.root, 'k-bad-id', {**GOOD, 'id': 'Bad_ID'})
        make_plugin(self.root, '.hidden', {**GOOD, 'id': 'hidden-x'})
        host = self.host()
        self.assertEqual(list(host.plugins), ['demo-x'])
        self.assertEqual(host.plugins['demo-x'].folder.name, 'c-dup')
        errors = {p.folder.name: p.error for p in host.failed}
        self.assertIn('不是合法的 JSON', errors['a-json'])
        self.assertIn('灵桥自己用的名字', errors['b-reserved'])
        self.assertIn('重复', errors['c-good'])
        self.assertIn('要灵桥 9.0.0', errors['d-too-new'])
        self.assertIn('没有 create', errors['e-no-create'])
        self.assertIn('重名', errors['f-shadow'])
        self.assertIn('创建时出错', errors['g-crash'])
        self.assertIn('assets 不对', errors['h-escape'])
        self.assertIn('找不到 nothere.js', errors['i-missing'])
        self.assertIn('普通文件', errors['j-link'])
        self.assertIn('id 不对', errors['k-bad-id'])
        self.assertNotIn('.hidden', errors)
        listed = {p['id']: p for p in host.public()}
        # 装坏的插件也在列表里（侧栏显示成灰色、写明原因），但占用灵桥名字的不列
        self.assertEqual(listed['crash-x']['status'], {'available': False, 'reason': '创建时出错'})
        self.assertNotIn('usage', listed)
        self.assertTrue(any(line.startswith('plugin-failed folder=g-crash type=RuntimeError') for line in self.logs))

    def test_manifest_text_fields_are_checked(self):
        make_plugin(self.root, 'a', {**GOOD, 'id': 'long-label', 'nav': {'label': '长' * 31}})
        make_plugin(self.root, 'b', {**GOOD, 'id': 'ctrl-label', 'nav': {'label': '换\n行'}})
        make_plugin(self.root, 'c', {**GOOD, 'id': 'bad-global', 'frontend': {'global': 'window.x'}})
        make_plugin(self.root, 'd', {**GOOD, 'id': 'bad-script', 'frontend': {'global': 'X', 'script': 'other.js'}})
        make_plugin(self.root, 'e', {**GOOD, 'id': 'bad-backend', 'backend': '../plugin.py'})
        host = self.host()
        self.assertEqual(host.plugins, {})
        errors = {p.id: p.error for p in host.failed}
        self.assertIn('nav.label', errors['long-label']); self.assertIn('nav.label', errors['ctrl-label'])
        self.assertIn('frontend.global', errors['bad-global']); self.assertIn('frontend.script', errors['bad-script'])
        self.assertIn('backend', errors['bad-backend'])

    def test_busy_and_shutdown(self):
        make_plugin(self.root, 'demo', GOOD)
        host = self.host()
        backend = host.plugins['demo-x'].backend
        self.assertEqual(host.busy_reasons(), [])
        backend.busy = '还有任务在跑'
        self.assertEqual(host.busy_reasons(), ['示例插件：还有任务在跑'])
        host.shutdown()
        self.assertTrue(backend.stopped)
        backend.shutdown = lambda: (_ for _ in ()).throw(RuntimeError('x'))
        host.shutdown()                                 # 插件退出出错不影响灵桥退出
        self.assertIn('plugin-shutdown-failed id=demo-x type=RuntimeError', self.logs)

    def test_two_plugins_can_both_use_plugin_py(self):
        make_plugin(self.root, 'one', {**GOOD, 'id': 'one-x'})
        make_plugin(self.root, 'two', {**GOOD, 'id': 'two-x'})
        host = self.host()
        self.assertEqual(sorted(host.plugins), ['one-x', 'two-x'])


class PluginHttpTests(PluginCase):
    def setUp(self):
        super().setUp()
        make_plugin(self.root, 'demo', GOOD)
        self.plugins = self.host()
        self.patches = [patch.object(s, 'plugins', self.plugins), patch.object(s, 'log', lambda message: None)]
        for p in self.patches:
            p.start()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), s.http_api.make_handler(s))
        self.port_patch = patch.object(s, 'PORT', self.httpd.server_port); self.port_patch.start()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        self.port_patch.stop()
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()

    def request(self, path, headers=None, method='GET', body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=10)
        payload = json.dumps(body).encode() if body is not None else None
        connection.request(method, quote(path, safe='/?=&:%'), payload, headers or {})
        response = connection.getresponse(); raw = response.read()
        result = (response.status, raw, response.getheader('Content-Type'))
        connection.close()
        return result

    def auth(self, extra=None):
        return {'X-Bridge-Token': s.API_TOKEN, **(extra or {})}

    def test_routes_reach_the_plugin(self):
        status, raw, _ = self.request('/api/plugins', self.auth())
        self.assertEqual(status, 200); self.assertEqual([p['id'] for p in json.loads(raw)['plugins']], ['demo-x'])
        status, raw, _ = self.request('/api/demo-x/hello?name=灵桥', self.auth())
        self.assertEqual((status, json.loads(raw)), (200, {'hello': '灵桥'}))
        self.assertEqual(self.request('/api/demo-x/file', self.auth())[:3], (200, b'PNG-demo', 'image/png'))
        json_headers = self.auth({'Content-Type': 'application/json'})
        status, raw, _ = self.request('/api/demo-x/echo', json_headers, 'POST', {'a': 1})
        self.assertEqual((status, json.loads(raw)), (200, {'echo': {'a': 1}}))
        status, raw, _ = self.request('/api/demo-x/ask', json_headers, 'POST', {})
        self.assertEqual(status, 409); self.assertEqual(json.loads(raw), {'error': '要再确认一次', 'confirm': 'again', 'detail': {'n': 1}})
        self.assertEqual(self.request('/api/demo-x/nothing', self.auth())[0], 400)
        self.assertEqual(self.request('/api/nobody/x', self.auth())[0], 404)
        self.assertEqual(self.request('/api/nobody/x', json_headers, 'POST', {})[0], 404)
        self.assertEqual(json.loads(self.request('/api/health')[1])['version'], s.VERSION)

    def test_every_plugin_route_needs_the_token_header(self):
        for path in ['/api/plugins', '/api/demo-x/hello', '/plugins/demo-x/demo.js']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 403)
                self.assertEqual(self.request(path + '?token=' + s.API_TOKEN)[0], 403)
                self.assertEqual(self.request(path, self.auth({'Host': 'evil.invalid'}))[0], 403)
                self.assertEqual(self.request(path, self.auth({'Origin': 'https://evil.invalid'}))[0], 403)
        self.assertEqual(self.request('/api/demo-x/echo', {'Content-Type': 'application/json'}, 'POST', {})[0], 403)

    def test_only_declared_page_files_are_served(self):
        status, raw, ctype = self.request('/plugins/demo-x/demo.js', self.auth())
        self.assertEqual((status, raw, ctype), (200, b'window.DemoPlugin = {};', 'text/javascript; charset=utf-8'))
        self.assertEqual(self.request('/plugins/demo-x/demo.css', self.auth())[2], 'text/css; charset=utf-8')
        for path in ['/plugins/demo-x/plugin.py', '/plugins/demo-x/plugin.json', '/plugins/demo-x/../demo/plugin.py',
                     '/plugins/demo-x/sub/demo.js', '/plugins/nobody/demo.js', '/plugins/demo-x/']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, self.auth())[0], 404)


if __name__ == '__main__':
    unittest.main()
