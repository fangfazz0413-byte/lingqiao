"""工具图标：从本机已装的 App 提取（用临时文件夹里的假 App），没有就给字母徽标。"""
import http.client
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import server as s  # noqa: E402
import brand_icons  # noqa: E402


def solid_png(path, size=128, rgb=(90, 120, 200)):
    """一张纯色 PNG，当假 App 的"官方图标"。"""
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    rows = b''.join(b'\x00' + bytes(rgb) * size for _ in range(size))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)) +
                     chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def fake_app(apps, name, icon_name='icon.png'):
    resources = apps / name / 'Contents' / 'Resources'
    resources.mkdir(parents=True)
    with open(apps / name / 'Contents' / 'Info.plist', 'wb') as stream:
        plistlib.dump({'CFBundleIconFile': icon_name}, stream)
    solid_png(resources / icon_name)
    return resources / icon_name


class BadgeTests(unittest.TestCase):
    def test_badges_are_svg_and_escaped(self):
        svg = brand_icons.badge_svg('glm')
        self.assertTrue(svg.startswith('<svg ')); self.assertIn('>G</text>', svg)
        self.assertIn('>&lt;</text>', brand_icons.badge_svg('<x'))


@unittest.skipUnless(Path('/usr/bin/sips').exists(), '需要 macOS 的 sips')
class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.apps = self.base / 'Applications'
        self.icons = self.base / 'icons'
        # 只在临时的"应用程序"文件夹里找，不读这台电脑上真装的 App
        self.patch = patch.object(brand_icons, '_app_dirs', lambda home: (self.apps,)); self.patch.start()

    def tearDown(self):
        self.patch.stop(); self.tmp.cleanup()

    def test_installed_apps_become_64px_png_and_missing_ones_are_reported(self):
        fake_app(self.apps, 'ZCode.app')
        fake_app(self.apps, 'Claude.app', icon_name='ignored.png')
        claude = self.apps / 'Claude.app' / 'Contents' / 'Resources'
        shutil.copy(claude / 'ignored.png', claude / 'electron.icns')     # Claude 用固定的资源名
        result = brand_icons.ensure(self.icons, home=self.base, spotlight=False)
        self.assertEqual(result['zcode'], 'ok'); self.assertEqual(result['claude'], 'ok')
        self.assertEqual(result['workbuddy'], 'not-installed')
        out = subprocess.run(['/usr/bin/sips', '-g', 'pixelWidth', '-g', 'pixelHeight', str(self.icons / 'zcode.png')],
                             capture_output=True, text=True, check=True).stdout
        self.assertIn('pixelWidth: 64', out); self.assertIn('pixelHeight: 64', out)
        if os.name != 'nt':  # Windows 没有这种权限位，靠用户目录的访问控制
            self.assertEqual(oct(os.stat(self.icons / 'zcode.png').st_mode & 0o777), '0o644')
        self.assertEqual([p.name for p in self.icons.iterdir() if p.name.startswith('.')], [])   # 不留临时文件
        again = brand_icons.ensure(self.icons, home=self.base, spotlight=False)
        self.assertEqual(again['zcode'], 'exists')


class IconRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app_dir = Path(self.tmp.name) / 'app'
        (app_dir / 'assets' / 'icons').mkdir(parents=True)
        (app_dir / 'assets' / 'icons' / 'claude.png').write_bytes(b'\x89PNG-claude')
        self.patches = [patch.object(s, 'APP_DIR', app_dir), patch.object(s, 'log', lambda message: None)]
        for p in self.patches:
            p.start()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), s.http_api.make_handler(s))
        self.port = patch.object(s, 'PORT', self.httpd.server_port); self.port.start()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.port.stop()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def get(self, path, host=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=10)
        connection.request('GET', path, headers={'Host': host} if host else {})
        response = connection.getresponse(); raw = response.read(); connection.close()
        return response.status, raw, response.getheader('Content-Type')

    def test_extracted_icon_or_letter_badge(self):
        self.assertEqual(self.get('/assets/icons/claude.png'), (200, b'\x89PNG-claude', 'image/png'))
        status, raw, ctype = self.get('/assets/icons/glm.png')
        self.assertEqual((status, ctype), (200, 'image/svg+xml; charset=utf-8')); self.assertIn(b'<svg', raw)
        self.assertEqual(self.get('/assets/icons/evil.png')[0], 400)
        self.assertEqual(self.get('/assets/icons/../server.py')[0], 400)
        self.assertEqual(self.get('/assets/icons/claude.png', host='evil.invalid')[0], 403)


if __name__ == '__main__':
    unittest.main()
