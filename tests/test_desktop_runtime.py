"""Launcher and dependency recovery tests that never open a native window."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


STAGE = Path(__file__).resolve().parents[1]


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / 'isolated 灵桥'
        self.launcher = self.repo / '会话桥.app/Contents/MacOS/会话桥'
        self.launcher.parent.mkdir(parents=True)
        shutil.copy2(STAGE / '会话桥.app/Contents/MacOS/会话桥', self.launcher)
        (self.repo / 'app').mkdir()
        (self.repo / 'app/server.py').write_text('# never executed by real Python\n')
        self.marker = self.repo / 'selected.json'
        self.environment = dict(os.environ, BRIDGE_PYTHON='')

    def tearDown(self):
        self.temp.cleanup()

    def fake_python(self, path, label, valid=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        # A Python import probe is emulated by a shell wrapper; a launch writes
        # a selection marker instead of executing the real application's code.
        payload = json.dumps({'selected': label})
        path.write_text(
            '#!/bin/bash\n'
            'if [ "$1" = "-c" ]; then\n'
            '  [ "$2" = "import webview, AppKit, Quartz, Security, WebKit" ] || exit 9\n'
            f'  exit {0 if valid else 1}\n'
            'fi\n'
            f'printf "%s" {shlex.quote(payload)} > {shlex.quote(str(self.marker))}\n'
        )
        path.chmod(0o755)
        return path

    def run_launcher(self):
        result = subprocess.run(['/bin/bash', str(self.launcher)], env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(self.marker.read_text())['selected']

    def test_launcher_uses_owned_runtime_without_workbuddy(self):
        self.fake_python(self.repo / '.bridge/runtime/bin/python3', 'owned-runtime')
        self.assertEqual(self.run_launcher(), 'owned-runtime')
        self.assertNotIn('.workbuddy', self.launcher.read_text())

    def test_explicit_override_has_priority(self):
        self.fake_python(self.repo / '.bridge/runtime/bin/python3', 'owned-runtime')
        override = self.fake_python(self.repo / 'override/bin/python3', 'override')
        self.environment['BRIDGE_PYTHON'] = str(override)
        self.assertEqual(self.run_launcher(), 'override')

    def test_incomplete_override_falls_back_to_owned_runtime(self):
        self.fake_python(self.repo / '.bridge/runtime/bin/python3', 'owned-runtime')
        override = self.fake_python(self.repo / 'override/bin/python3', 'incomplete', valid=False)
        self.environment['BRIDGE_PYTHON'] = str(override)
        self.assertEqual(self.run_launcher(), 'owned-runtime')

    def test_repair_reuses_owned_environment_and_installs_exact_manifest(self):
        repair = self.repo / 'app/repair-runtime.sh'
        shutil.copy2(STAGE / 'app/repair-runtime.sh', repair)
        requirements = self.repo / 'app/requirements-desktop.txt'
        shutil.copy2(STAGE / 'app/requirements-desktop.txt', requirements)
        owned = self.repo / '.bridge/runtime/bin/python3'
        owned.parent.mkdir(parents=True)
        calls = self.repo / 'calls.txt'
        owned.write_text(
            '#!/bin/bash\n'
            f'printf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n'
            'exit 0\n'
        )
        owned.chmod(0o755)
        result = subprocess.run(['/bin/bash', str(repair)], env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        recorded = calls.read_text().splitlines()
        self.assertIn('-m ensurepip --upgrade', recorded)
        self.assertIn('-m pip install --disable-pip-version-check -r ' + str(requirements), recorded)
        self.assertIn('-c import webview, AppKit, Quartz, Security, WebKit', recorded)
        self.assertFalse(any('-m venv' in line for line in recorded))

    def test_scripts_have_valid_bash_syntax(self):
        for rel in ('会话桥.app/Contents/MacOS/会话桥', 'app/repair-runtime.sh', 'install.sh'):
            result = subprocess.run(['/bin/bash', '-n', str(STAGE / rel)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
