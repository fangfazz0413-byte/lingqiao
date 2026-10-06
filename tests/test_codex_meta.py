"""Codex 会话第一行（session_meta）的格式：照着本机 Codex 最近写的会话来写格式字段，不抄别的会话的内容。

装了 Codex 的电脑上，还会把灵桥写出的会话文件交给 Codex 自带的检查工具（只读，在临时文件夹里）看它认不认。
"""
import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
if (ROOT / ".bridge" / "bridge_state.py").is_file():
    sys.path.insert(0, str(ROOT / ".bridge"))
import bridge_ops as ops  # noqa: E402

CODEX = next((p for p in ops.CODEX_CLI_CANDIDATES if os.access(p, os.X_OK)), None)
GENUINE = {"id": "g-1", "originator": "Codex Desktop", "cli_version": "9.9.9", "model_provider": "provider-x", "source": "vscode",
           "creator_account_id": "acct-1", "creator_user_id": "user-1", "context_window": {"window_id": "w-1"},
           "base_instructions": {"text": "别的会话的指令，不能抄", "provenance": {"type": "model", "model": "m"}}, "cwd": "/secret/project"}


def codex_app_server(codex_home, requests):
    """在临时 CODEX_HOME 里开 Codex 的 app-server，像 Codex 窗口一样发请求；返回每个请求的回复。"""
    import queue
    import threading
    proc = subprocess.Popen([CODEX, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={**os.environ, "CODEX_HOME": str(codex_home)}, text=True, bufsize=1)
    answers, lines = {}, queue.Queue()
    # 单独一个线程逐行读回复（不能用 select 配合带缓冲的读法，几行一起到时会漏掉）
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True).start()

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def wait(request_id, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                return message
        return {"error": {"message": "Codex app-server 没有回复"}}
    try:
        send({"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "lingqiao-test", "version": "1"}}})
        answers["initialize"] = wait(0)
        send({"method": "initialized"})
        for number, (method, params) in enumerate(requests, 1):
            send({"id": number, "method": method, "params": params})
            answers[method] = wait(number)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return answers


def write_rollout(root, day, sid, payload):
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{day.replace('/', '-')}T10-00-00-{sid}.jsonl"
    path.write_text(json.dumps({"timestamp": "2026-10-06T02:00:00.000Z", "ordinal": 0, "type": "session_meta", "payload": payload}) + "\n")
    return path


class CodexMetaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = SimpleNamespace(CX_ROOT=Path(self.tmp.name) / "sessions")
        self.env.steal_codex_meta_fields = lambda: ops.codex_meta_fields(self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def test_copies_format_fields_from_newest_codex_session_but_not_content(self):
        write_rollout(self.env.CX_ROOT, "2026/10/05", "old", {**GENUINE, "cli_version": "1.0.0"})
        write_rollout(self.env.CX_ROOT, "2026/10/06", "new", GENUINE)
        write_rollout(self.env.CX_ROOT, "2026/10/07", "ours", {**GENUINE, "originator": "Session Bridge", "cli_version": "x"})
        broken = self.env.CX_ROOT / "2026/10/08"
        broken.mkdir(parents=True)
        (broken / "rollout-2026-10-08T00-00-00-broken.jsonl").write_text("[1, 2]\n")   # 坏文件、灵桥自己写的都不当模板
        fields = ops.codex_meta_fields(self.env)
        self.assertEqual((fields["cli_version"], fields["model_provider"], fields["creator_account_id"], fields["creator_user_id"]),
                         ("9.9.9", "provider-x", "acct-1", "user-1"))
        self.assertIsInstance(fields["context_window"], dict)
        self.assertNotEqual(fields["context_window"]["window_id"], "w-1")
        lines = ops.codex_rollout_records(self.env, "sid-1", {"dir": "/work", "mtime": 1759716000}, [{"role": "user", "text": "hi"}])
        meta = lines[0]["payload"]
        self.assertEqual((lines[0]["type"], meta["id"], meta["cwd"], meta["originator"], meta["cli_version"]),
                         ("session_meta", "sid-1", "/work", "Session Bridge", "9.9.9"))
        self.assertEqual(meta["base_instructions"], {"text": ops.CODEX_IMPORT_NOTE})
        text = json.dumps(lines, ensure_ascii=False)
        self.assertNotIn("别的会话的指令", text)
        self.assertNotIn("/secret/project", text)

    def test_older_codex_with_integer_context_window_is_followed(self):
        write_rollout(self.env.CX_ROOT, "2026/09/01", "old", {**GENUINE, "context_window": 128000})
        self.assertEqual(ops.codex_meta_fields(self.env)["context_window"], 128000)

    def test_without_codex_sessions_the_installed_codex_version_is_used(self):
        with mock.patch.object(ops, "_codex_cli_version", return_value="1.2.3"):
            fields = ops.codex_meta_fields(self.env)
        self.assertEqual(fields["cli_version"], "1.2.3")
        self.assertIsInstance(fields["context_window"], dict)
        with mock.patch.object(ops, "_codex_cli_version", return_value=""):
            self.assertNotIn("cli_version", ops.codex_meta_fields(self.env))

    @unittest.skipUnless(CODEX, "这台电脑没装 Codex")
    def test_installed_codex_accepts_what_lingqiao_writes(self):
        sid = str(uuid.uuid4())
        lines = ops.codex_rollout_records(self.env, sid, {"dir": "/tmp", "mtime": 1759716000},
                                          [{"role": "user", "text": "你好"}, {"role": "assistant", "text": "你好！"}])
        home = Path(self.tmp.name) / "codex-home"
        folder = home / "sessions/2026/10/06"
        folder.mkdir(parents=True)
        (folder / f"rollout-2026-10-06T10-00-00-{sid}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n")
        result = subprocess.run([CODEX, "migrate-rollouts", "--thread", sid, "--verbose"], env={**os.environ, "CODEX_HOME": str(home)},
                                capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        output = result.stdout + result.stderr
        self.assertIn("0 failed", output, output)
        self.assertNotIn("does not start with session metadata", output)

    @unittest.skipUnless(CODEX, "这台电脑没装 Codex")
    def test_codex_app_server_reads_a_session_synced_by_lingqiao(self):
        import server as s
        home = Path(self.tmp.name) / "codex-home"
        sessions = home / "sessions/2026/10/06"
        sessions.mkdir(parents=True)
        # 1. 让 Codex 自己建好数据库：放一条最简单的旧格式会话，跑它自带的迁移工具
        probe = str(uuid.uuid4())
        meta = {"id": probe, "session_id": probe, "timestamp": "2026-10-06T01:00:00.000Z", "cwd": "/tmp", "originator": "probe",
                "cli_version": ops._codex_cli_version(), "source": "vscode", "model_provider": "openai",
                "context_window": {"window_id": str(uuid.uuid4())}, "base_instructions": {"text": "probe"}}
        lines = [{"timestamp": "2026-10-06T01:00:00.000Z", "ordinal": 0, "type": "session_meta", "payload": meta},
                 {"timestamp": "2026-10-06T01:00:01.000Z", "ordinal": 1, "type": "response_item",
                  "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}}]
        (sessions / f"rollout-2026-10-06T09-00-00-{probe}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        subprocess.run([CODEX, "migrate-rollouts", "--apply"], env={**os.environ, "CODEX_HOME": str(home)}, capture_output=True,
                       timeout=120, stdin=subprocess.DEVNULL, check=True)
        self.assertTrue((home / "thread_history_1.sqlite").is_file())
        # 2. 灵桥按"同步到 Codex"的流程写一条：两轮，第一轮 AI 连着回了两次
        sid = str(uuid.uuid4())
        turns = [{"role": "user", "text": "第一问"}, {"role": "assistant", "text": "先看一下"}, {"role": "assistant", "text": "答案"},
                 {"role": "user", "text": "第二问"}, {"role": "assistant", "text": "好的"}]
        with mock.patch.multiple(s, CX_ROOT=home / "sessions", CX_STATE=home / "state_5.sqlite", CX_SQLITE=home / "thread_history_1.sqlite",
                                 CX_INDEX=home / "session_index.jsonl", log=lambda message: None):
            ops._create_rollout(s, sid, sessions / f"rollout-2026-10-06T10-00-00-{sid}.jsonl",
                                {"dir": "/tmp", "title": "灵桥测试会话", "mtime": 1759716000}, turns)
        with closing(sqlite3.connect(home / "thread_history_1.sqlite")) as c:
            phases = [json.loads(x)["phase"] for (x,) in c.execute(
                "SELECT item_json FROM thread_items WHERE thread_id=? AND item_type='agentMessage' ORDER BY rollout_ordinal", (sid,))]
        self.assertEqual(phases, ["commentary", "final_answer", "final_answer"])
        # 3. 让 Codex 像窗口那样读：会话信息、整条历史、分页列表、侧栏列表都要读得出
        answers = codex_app_server(home, [("thread/read", {"threadId": sid, "includeTurns": True}),
                                          ("thread/items/list", {"threadId": sid, "limit": 100}),
                                          ("thread/list", {"modelProviders": [], "useStateDbOnly": True})])
        for method, answer in answers.items():
            self.assertIn("result", answer, f"{method}: {answer.get('error')}")
        read_turns = answers["thread/read"]["result"]["thread"]["turns"]
        self.assertEqual(len(read_turns), 2)
        self.assertEqual(sum(len(t.get("items") or []) for t in read_turns), 5)
        listed = answers["thread/list"]["result"].get("data") or []
        self.assertIn(sid, [t.get("id") for t in listed])


if __name__ == "__main__":
    unittest.main()
