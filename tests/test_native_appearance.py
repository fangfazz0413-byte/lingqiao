import sys
import threading
import types
import unittest
from unittest.mock import patch


sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'app'))
import native_appearance


class NativeAppearanceTests(unittest.TestCase):
    def test_only_frontend_themes_are_accepted(self):
        self.assertEqual(set(native_appearance.THEMES), {'rose', 'ocean', 'mint', 'lavender', 'cream'})
        for name in native_appearance.THEMES:
            self.assertEqual(native_appearance.normalize_theme(name), name)
        for value in ('', 'dark', None, 1, [], {'theme': 'rose'}):
            with self.assertRaises(ValueError):
                native_appearance.normalize_theme(value)

    def test_rgb_conversion_is_strict_and_srgb(self):
        self.assertEqual(native_appearance._rgb('#0d0f13'), (13 / 255, 15 / 255, 19 / 255))
        for value in ('#fff', '#0000000', '#gg0000', '000000'):
            with self.assertRaises(ValueError):
                native_appearance._rgb(value)

    def test_theme_updates_native_titlebar_and_light_appearance(self):
        calls = []

        class Color:
            @staticmethod
            def colorWithSRGBRed_green_blue_alpha_(r, g, b, a):
                return ('color', r, g, b, a)

        class Appearance:
            @staticmethod
            def appearanceNamed_(name):
                return ('appearance', name)

        fake_appkit = types.SimpleNamespace(
            NSColor=Color,
            NSAppearance=Appearance,
            NSAppearanceNameAqua='Aqua',
            NSAppearanceNameDarkAqua='DarkAqua',
            NSWindowTitleVisible=0,
        )

        class Native:
            def setTitlebarAppearsTransparent_(self, value): calls.append(('transparent', value))
            def setTitleVisibility_(self, value): calls.append(('title-visibility', value))
            def setBackgroundColor_(self, value): calls.append(('background', value))
            def setAppearance_(self, value): calls.append(('appearance', value))

            class Content:
                def superview(self):
                    return self

                class Subs:
                    @staticmethod
                    def lastObject(): return None

                def subviews(self): return self.Subs()

            def contentView(self): return self.Content()

        window = types.SimpleNamespace(native=Native())
        with patch.object(native_appearance.sys, 'platform', 'darwin'), patch.dict(sys.modules, {'AppKit': fake_appkit}):
            result = native_appearance.apply_theme(window, 'cream')
        self.assertEqual(result, 'cream')
        self.assertIn(('transparent', True), calls)
        self.assertIn(('title-visibility', 0), calls)
        self.assertIn(('appearance', ('appearance', 'Aqua')), calls)
        self.assertFalse(any(call[1] == ('appearance', ('appearance', 'DarkAqua')) for call in calls if call[0] == 'appearance'))

    def test_worker_thread_dispatches_cocoa_mutation_to_appkit(self):
        callbacks = []
        fake_helper = types.ModuleType('PyObjCTools.AppHelper')
        fake_helper.callAfter = callbacks.append
        fake_pkg = types.ModuleType('PyObjCTools')
        fake_pkg.AppHelper = fake_helper
        with patch.object(native_appearance.sys, 'platform', 'darwin'), patch.dict(
            sys.modules,
            {'PyObjCTools': fake_pkg, 'PyObjCTools.AppHelper': fake_helper},
        ):
            result = []
            worker = threading.Thread(target=lambda: result.append(native_appearance.apply_theme(object(), 'mint')))
            worker.start(); worker.join()
        self.assertEqual(result, ['mint'])
        self.assertEqual(len(callbacks), 1)


if __name__ == '__main__':
    unittest.main()
