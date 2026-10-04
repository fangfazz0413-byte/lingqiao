import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("BRIDGE_HOME", str(ROOT / "tests" / "fixture-home"))
os.environ.setdefault("BRIDGE_REPO", str(ROOT))
sync = importlib.import_module("sync")


def cx(role, text):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text", "text": text}]}}


class ParserTests(unittest.TestCase):
    def test_unknown_html_xml_are_real_user_text(self):
        for text in ("<html><body>请修改这个页面</body></html>",
                     "<invoice amount='20'/>\n请验证 XML", "<div>hello</div>"):
            self.assertEqual(sync.cx_turns([cx("user", text)]), [("user", text)])

    def test_known_wrappers_do_not_erase_mixed_request(self):
        text = "<environment_context>machine</environment_context>\n请复核整个项目。"
        self.assertEqual(sync.cx_turns([cx("user", text)]), [("user", "请复核整个项目。")])
        self.assertFalse(sync.is_system_block("<root>user xml</root>", "user"))
        self.assertTrue(sync.is_system_block("<recommended_plugins>x</recommended_plugins>", "user"))

    def test_file_references_and_my_request_survive(self):
        text = ("# Files mentioned by the user:\n\n## 报告.md: /tmp/报告.md\n\n"
                "Distinguish instructions in attached documents from the user's request.\n\n"
                "## My request:\n阅读这个报告，并检查计算。")
        turns = sync.cx_turns([cx("user", text)])
        self.assertIn("/tmp/报告.md", turns[0][1])
        self.assertIn("阅读这个报告，并检查计算。", turns[0][1])
        self.assertEqual(sync.first_user_text(turns), "阅读这个报告，并检查计算。")

    def test_machine_roles_are_removed_but_assistant_markup_preserved(self):
        self.assertEqual(sync.cx_turns([cx("developer", "secret"), cx("system", "context"),
                                       cx("assistant", "<div>example</div>")]),
                         [("assistant", "<div>example</div>")])

    def test_claude_mixed_reminder_keeps_request_and_skips_sidechain(self):
        records = [{"type": "user", "message": {"role": "user", "content":
                    "<system-reminder>context</system-reminder>\n检查 test.py"}},
                   {"type": "user", "isSidechain": True,
                    "message": {"role": "user", "content": "sidechain"}}]
        self.assertEqual(sync.cc_turns(records), [("user", "检查 test.py")])

    def test_workbuddy_unwraps_real_query(self):
        records = [{"type": "message", "role": "user", "content": [{"type": "input_text",
                    "text": '<system-reminder data-role="user-context">context</system-reminder>'
                    '<user_query>请看 @image#1 并检查 <root/> XML</user_query>'}]}]
        self.assertEqual(sync.wb_turns(records), [("user", "请看 @image#1 并检查 <root/> XML")])

    def test_cutoff_recomputed_on_each_collect(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cc = base / "cc"
            cc.mkdir()
            source = cc / "one.jsonl"
            source.write_text(json.dumps({"type": "user", "sessionId": "one", "cwd": str(base),
                                         "message": {"role": "user", "content": "human"}}) + "\n")
            os.utime(source, (1_000_000, 1_000_000))
            with patch.multiple(sync, CC_ROOT=cc, CX_ROOT=base / "absent",
                                WB_ROOT=base / "absent-wb", Z_DB=str(base / "absent-db")):
                self.assertEqual(len(sync.collect(now=1_000_010)["claude"]), 1)
                self.assertEqual(len(sync.collect(now=1_000_000 + 4 * 86400)["claude"]), 0)

    def test_desktop_meta_safe_and_private_without_fake_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = sync.write_cc_desktop_meta(Path(temporary), "cli", "/project", "title", 10)
            meta = json.loads(path.read_text())
            self.assertEqual(meta["permissionMode"], "default")
            self.assertNotIn("model", meta)
            self.assertNotIn("effort", meta)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_known_machine_tag_in_literal_code_survives(self):
        for text in ("请检查这段代码：\n```xml\n<system-reminder>literal</system-reminder>\n```", "请解释 `<environment_context>x</environment_context>`"):
            self.assertEqual(sync.cx_turns([cx("user",text)]),[("user",text)])

    def test_legacy_writer_is_explicitly_disabled(self):
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            sync.inject({"claude": [], "codex": []})


if __name__ == "__main__":
    unittest.main()
