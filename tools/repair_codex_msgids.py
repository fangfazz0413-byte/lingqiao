#!/usr/bin/env python3
"""修复早期灵桥导入的 Codex 会话：去掉 response_item message 里的 msg_ id。

背景：ChatGPT 后端（rustponsesapi）把请求 input 里带 id 的条目当成要查服务器
持久化条目的引用，本地随机造的 id 服务器没存过，续聊报
"Supplied input item IDs require persisted-item lookup that is not supported by rustponsesapi"。
bridge_ops.codex_rollout_records 已改为不写 id；这个脚本处理修复之前已经导入的会话文件。

rollout 行变短后，thread_history_1.sqlite 里 thread_turns 的
rollout_byte_offset / rollout_end_byte_offset 和
thread_history_projection_state 的 next_rollout_byte_offset 会失准，
按新文件重算后原地 UPDATE（thread_id + rollout_ordinal 定位，不动 turn_id）。

只处理第一行 originator == "Session Bridge" 的 rollout。原文件备份到
.bridge/repair-msgids-<时间>/。用法：python tools/repair_codex_msgids.py [--dry]
"""
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))
import bridge_ops  # noqa: E402

HOME = Path.home()
SESSIONS = HOME / ".codex" / "sessions"
HISTORY_DB = HOME / ".codex" / "thread_history_1.sqlite"
BACKUP = APP.parent / ".bridge" / ("repair-msgids-" + time.strftime("%Y%m%d-%H%M%S"))


def bridge_rollouts():
    for path in sorted(SESSIONS.rglob("rollout-*.jsonl")):
        try:
            first = json.loads(path.open(encoding="utf-8", errors="ignore").readline())
        except (OSError, ValueError):
            continue
        payload = first.get("payload") or {}
        if isinstance(payload, dict) and payload.get("originator") == "Session Bridge":
            yield path, payload.get("id")


def strip_msg_ids(path: Path):
    """去掉 message 里的 msg_ id；返回 (改了几行, 新行字节列表)。"""
    raw_lines = path.read_bytes().splitlines(keepends=True)
    out, changed = [], 0
    for raw in raw_lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            out.append(raw)
            continue
        payload = rec.get("payload") or {}
        if (rec.get("type") == "response_item" and payload.get("type") == "message"
                and isinstance(payload.get("id"), str) and payload["id"].startswith("msg_")):
            payload.pop("id")
            out.append((json.dumps(rec, ensure_ascii=False) + "\n").encode())
            changed += 1
        else:
            out.append(raw)
    return changed, out


def new_offsets(lines):
    """每一行 ordinal 的字节区间 + 文件总长。

    不按 message 分组猜轮次边界：thread_turns 行自带 rollout_ordinal /
    rollout_end_ordinal，按每行自身的 ordinal 查区间即可，对轮首不是
    message（reasoning、turn_context 等）的轮次同样正确。
    """
    off, omap, total = 0, {}, 0
    for raw in lines:
        rec = json.loads(raw)
        omap[rec.get("ordinal")] = (off, off + len(raw))
        off += len(raw)
    return omap, off


def main():
    dry = "--dry" in sys.argv
    targets = list(bridge_rollouts())
    if not targets:
        print("没有找到灵桥写的 Codex 会话，无需修复")
        return
    if not dry:
        BACKUP.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(HISTORY_DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    for path, sid in targets:
        changed, lines = strip_msg_ids(path)
        if not changed:
            print(f"[跳过] {path.name}：没有 msg_ id")
            continue
        omap, total = new_offsets(lines)
        turns = conn.execute(
            "SELECT turn_id, rollout_ordinal, rollout_end_ordinal, rollout_byte_offset,"
            " rollout_end_byte_offset FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal",
            (sid,)).fetchall()
        mode = "dry " if dry else "修复"
        print(f"[{mode}] {path.name}：去掉 {changed} 个 id，{len(turns)} 轮偏移按新文件重算，总长 {total}")
        if dry:
            continue
        shutil.copy2(path, BACKUP / path.name)
        path.write_bytes(b"".join(lines))
        with conn:
            for turn_id, ordinal, end_ordinal, old_start, old_end in turns:
                start = omap.get(ordinal)
                end = omap.get(end_ordinal)
                if start is None or end is None:
                    raise SystemExit(f"ordinal 失配：{path.name} turn={turn_id[:8]} {ordinal}..{end_ordinal}")
                conn.execute(
                    "UPDATE thread_turns SET rollout_byte_offset=?, rollout_end_byte_offset=?"
                    " WHERE thread_id=? AND turn_id=?",
                    (start[0], end[1], sid, turn_id))
            conn.execute(
                "UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?",
                (total, sid))
        print(f"       备份：{BACKUP / path.name}")
    conn.close()
    if dry:
        print("（dry 模式：没有写任何文件）")


if __name__ == "__main__":
    main()
