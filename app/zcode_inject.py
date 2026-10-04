#!/usr/bin/env python3
"""会话桥：把 CC/Codex/WorkBuddy 近 3 天会话注入 ZCode db.sqlite

用法：
  python3 zcode_inject.py --dry-run            # 只列出将要注入的会话
  python3 zcode_inject.py --db /tmp/zcode-lab.sqlite   # 练习库
  python3 zcode_inject.py                      # 真库注入（默认 ~/.zcode/cli/db/db.sqlite）
  python3 zcode_inject.py --rollback           # 按清单回滚（删掉注入的会话）

设计要点：
  - 内容来源：复用 sync.collect()（含 3 天窗口、系统块过滤、codeg 跳过）
  - 镜像去重：三期A CC↔Codex 互注产生的副本（路径在 ledger.values()）不重复注入
  - 真库和 --db 练习库按数据库身份隔离账本、manifest 和 provenance。
  - 回滚只删 manifest 内、在指定数据库成功匹配的 ID，保留其他账本。
  - SQL 全部静态字符串 + ? 参数（Mimosa 钩子要求），python sqlite3 直连
  - 每会话一个事务、提交前验证；App/CLI 共用操作锁和私有原子状态文件。
  - 模仿真实行结构：session + session_entry(runtime/model_selection,
    runtime/execution_state) + message(semantics.uiVisibility=visible) +
    part(text/step-start/step-finish)，sequence 交给库内触发器自动填充
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
import bridge_state as bs

HOME = Path(os.environ.get("BRIDGE_HOME", str(Path.home())))
REPO = Path(os.environ.get("BRIDGE_REPO", str(Path(__file__).resolve().parent.parent)))
BRIDGE = REPO / ".bridge"
LEDGER = BRIDGE / "ledger.json"
MANIFEST = BRIDGE / "zcode-injected-manifest.json"
APP_STATE = REPO / "app" / "state.json"  # App 自动同步开关
REAL_DB = str(HOME / ".zcode" / "cli" / "db" / "db.sqlite")
BATCH_SLEEP = 0.3          # 每个会话写入后的间隔，给 App 留并发余量
ZCODE_VERSION = "0.16.9"   # 与库内最新会话一致

# ---------- 静态 SQL（全部 ? 参数化） ----------

SQL_INS_SESSION = (
    "INSERT INTO session (id, project_id, slug, directory, path, title,"
    " version, permission, time_created, time_updated, task_type,"
    " title_source, trace_id)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
SQL_INS_ENTRY = (
    "INSERT INTO session_entry (id, session_id, type, time_created,"
    " time_updated, data) VALUES (?,?,?,?,?,?)"
)
SQL_INS_MESSAGE = (
    "INSERT INTO message (id, session_id, time_created, time_updated, data)"
    " VALUES (?,?,?,?,?)"
)
SQL_INS_PART = (
    "INSERT INTO part (id, message_id, session_id, time_created,"
    " time_updated, data) VALUES (?,?,?,?,?,?)"
)
SQL_FIND_PROJECT = (
    "SELECT project_id FROM session WHERE directory = ?"
    " AND project_id IS NOT NULL LIMIT 1"
)
SQL_HAS_SESSION = "SELECT 1 FROM session WHERE id = ?"
SQL_COUNT_SESSION = "SELECT count(*) FROM session"
SQL_DEL_PART = "DELETE FROM part WHERE session_id = ?"
SQL_DEL_MESSAGE = "DELETE FROM message WHERE session_id = ?"
SQL_DEL_ENTRY = "DELETE FROM session_entry WHERE session_id = ?"
SQL_DEL_SESSION = "DELETE FROM session WHERE id = ?"
SQL_VERIFY_MSGS = (
    "SELECT count(*), coalesce(max(sequence), -1) FROM message"
    " WHERE session_id = ?"
)
SQL_VERIFY_PARTS = "SELECT count(*) FROM part WHERE session_id = ?"


def oid(prefix: str) -> str:
    """模仿真实 id 格式：msg_xxxxxxxx_uuid / part_xxxxxxxx_uuid。"""
    return prefix + "_" + uuid.uuid4().hex[:8] + "_" + str(uuid.uuid4())


def clean_title(text: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return (text[:limit] + "…") if len(text) > limit else text


def make_project_id(directory: str) -> str:
    """按 ZCode 规律从 directory 推 project_id：
    每段小写、非 [a-z0-9._-] 字符归并为 '-'、压缩连续 '-'、去尾 '-'、空段跳过。"""
    segs = []
    for raw in directory.split("/"):
        s = re.sub(r"[^a-z0-9._-]+", "-", raw.lower())
        s = re.sub(r"-{2,}", "-", s).rstrip("-")
        if s:
            segs.append(s)
    return "proj_" + "-".join(segs)


def load_json(path: Path, default):
    return bs.load_json(path, default)


def dump_json(path: Path, obj) -> None:
    bs.atomic_json(path, obj)


def build_session_rows(s, tool, project_id):
    """把一个 collect() 会话转成 ZCode 各表的行。返回 dict。"""
    turns = s["turns"]
    t_updated = int(s["mtime"] * 1000)
    span = max(900000, len(turns) * 3000)
    t_created = t_updated - span
    sid = "sess_" + str(uuid.uuid4())
    # "zcode"：另一台电脑导出的 ZCode 会话转文字导入（灵桥 3.4）。
    if tool not in ("claude", "codex", "workbuddy", "zcode"):
        raise ValueError("Unsupported source tool")
    if any(role not in ("user", "assistant") or not isinstance(text, str)
           for role, text in turns):
        raise ValueError("Only human/assistant text turns may be imported")
    title = clean_title(s["title"]) or "(无标题)"

    entries = [
        (sid + ":runtime-execution-state", sid, "runtime/execution_state",
         t_created, t_created,
         json.dumps({"mode": "build", "planEnabled": False},
                    ensure_ascii=False)),
    ]
    # Unknown historical models must not become a fabricated current model.
    selection = s.get("model_selection")
    if isinstance(selection, dict) and selection.get("modelId") and selection.get("providerId"):
        entries.insert(0, (sid + ":runtime-model-selection", sid, "runtime/model_selection",
                           t_created, t_created,
                           json.dumps({"modelSelection": selection}, ensure_ascii=False)))

    messages, parts = [], []
    prev_msg = None
    n = max(len(turns) - 1, 1)
    for i, (role, text) in enumerate(turns):
        mid = oid("msg")
        t = t_created + (t_updated - t_created) * i // n
        if role == "user":
            mdata = {"role": "user", "time": {"created": t},
                     "agent": "zcode-agent",
                     "semantics": {"origin": "real_user",
                                   "kind": "user_prompt",
                                   "uiVisibility": "visible",
                                   "providerVisibility": "visible",
                                   "transcriptVisibility": "visible"}}
            messages.append((mid, sid, t, t,
                             json.dumps(mdata, ensure_ascii=False)))
            parts.append((oid("part"), mid, sid, t, t,
                          json.dumps({"type": "text", "text": text,
                                      "time": {"start": t, "end": t}},
                                     ensure_ascii=False)))
        else:
            mdata = {"role": "assistant",
                     "time": {"created": t, "completed": t + 900},
                     "parentID": prev_msg, "mode": "build",
                     "planEnabled": False, "agent": "zcode-agent",
                     "semantics": {"origin": "agent_runtime",
                                   "kind": "assistant_response",
                                   "uiVisibility": "visible",
                                   "providerVisibility": "visible",
                                   "transcriptVisibility": "visible"}}
            messages.append((mid, sid, t, t + 900,
                             json.dumps(mdata, ensure_ascii=False)))
            parts.append((oid("part"), mid, sid, t, t,
                          json.dumps({"type": "step-start"},
                                     ensure_ascii=False)))
            parts.append((oid("part"), mid, sid, t, t + 900,
                          json.dumps({"type": "text", "text": text,
                                      "time": {"start": t,
                                               "end": t + 900}},
                                     ensure_ascii=False)))
            parts.append((oid("part"), mid, sid, t + 900, t + 900,
                          json.dumps({"type": "step-finish", "reason": "stop",
                                      "cost": 0,
                                      "tokens": {"total": 0, "input": 0,
                                                 "output": 0, "reasoning": 0,
                                                 "cache": {"read": 0,
                                                           "write": 0}}},
                                     ensure_ascii=False)))
        prev_msg = mid

    session_row = (sid, project_id, sid, s["dir"], s["dir"], title,
                   ZCODE_VERSION,
                   json.dumps({"mode": "build"}, ensure_ascii=False),
                   t_created, t_updated, "interactive", "first_input",
                   str(uuid.uuid4()))
    return {"sid": sid, "session": session_row, "entries": entries,
            "messages": messages, "parts": parts, "title": title}


def write_one(cur, rows):
    cur.execute(SQL_INS_SESSION, rows["session"])
    for e in rows["entries"]:
        cur.execute(SQL_INS_ENTRY, e)
    for m in rows["messages"]:
        cur.execute(SQL_INS_MESSAGE, m)
    for p in rows["parts"]:
        cur.execute(SQL_INS_PART, p)


def verify_one(cur, rows):
    cur.execute(SQL_VERIFY_MSGS, (rows["sid"],))
    nmsg, maxseq = cur.fetchone()
    cur.execute(SQL_VERIFY_PARTS, (rows["sid"],))
    npart = cur.fetchone()[0]
    expect_msg = len(rows["messages"])
    if nmsg != expect_msg or maxseq != expect_msg - 1:
        raise RuntimeError(
            f"verify fail {rows['sid']}: msg {nmsg}/{expect_msg} seq {maxseq}")
    if npart != len(rows["parts"]):
        raise RuntimeError(
            f"verify fail {rows['sid']}: part {npart}/{len(rows['parts'])}")
    # Check before commit, when failures can still roll back this session.
    violations = cur.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("ZCode foreign key verification failed")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()

def connect_db(path, readonly=False):
    path = str(Path(path).expanduser().resolve(strict=True))
    if readonly:
        conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True,
                               timeout=60, factory=ClosingConnection)
    else:
        conn = sqlite3.connect(path, timeout=60, factory=ClosingConnection)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def pick_targets(result, ledger, provenance=None):
    """选出要注入的原生会话，排除镜像、codeg 残留、已入账、App 里关闭的。"""
    mirror_files = set(ledger.values()) | set(provenance or {})
    app_state = load_json(APP_STATE, {})
    autosync_map = app_state.get("autosync", {})
    autosync_all = app_state.get("autosync_all", False)  # 默认全关，逐个勾选
    targets = []
    cutoff = time.time() - 3 * 86400
    for tool in ("claude", "codex", "workbuddy"):
        for s in result.get(tool, []):
            if s["mtime"] < cutoff:
                continue
            if "app.codeg" in s["dir"]:
                continue
            if s["src"] in mirror_files:
                continue          # 三期A 互注产生的镜像，原件会单独注入
            key = "zcode:" + tool + ":" + s["src"]
            if key in ledger:
                continue          # 已注入过
            if not s["turns"]:
                continue
            # App 里的自动同步开关（key 无 zcode: 前缀）；未设置时跟随总开关
            if not autosync_map.get(tool + ":" + s["src"], autosync_all):
                continue
            targets.append((tool, key, s))
    return targets


def resolve_project_ids(conn, targets):
    """directory → project_id：先复用库内已有的，再生成并缓存。"""
    cache = {}
    for _, _, s in targets:
        d = s["dir"]
        if d in cache:
            continue
        row = conn.execute(SQL_FIND_PROJECT, (d,)).fetchone()
        if row:
            cache[d] = row[0]
        else:
            cache[d] = make_project_id(d)
    return cache


def state_paths(db_path):
    return bs.zcode_state_paths(db_path, REAL_DB, BRIDGE)


def _state_snapshots(paths):
    return {name: bs.load_json(paths[name], None)
            for name in ("ledger", "manifest", "provenance")}


def _restore_snapshots(paths, snapshots):
    for name, value in snapshots.items():
        if value is None:
            paths[name].unlink(missing_ok=True)
        else:
            bs.atomic_json(paths[name], value)


def do_inject(db_path, dry_run):
    import sync
    paths = state_paths(db_path)
    with bs.operation_lock(paths["lock"]):
        ledger = load_json(paths["ledger"], {})
        if not isinstance(ledger, dict):
            raise ValueError("Invalid ledger")
        bs.load_zcode_manifest(paths["manifest"], db_path)
        provenance = bs.provenance_records(paths["provenance"])
        result = sync.collect()
        targets = pick_targets(result, ledger, provenance)
        print(f"待注入：{len(targets)} 个会话（数据库 {bs.database_identity(db_path)['key']}）")
        for tool, key, session in targets:
            print(f"  [{tool}] turns={len(session['turns']):3d} {clean_title(session['title'])[:36]!r}")
        if dry_run or not targets:
            return 0

        conn = connect_db(db_path)
        cur = conn.cursor()
        before = cur.execute(SQL_COUNT_SESSION).fetchone()[0]
        projects = resolve_project_ids(conn, targets)
        count = 0
        try:
            for tool, key, session in targets:
                rows = build_session_rows(session, tool, projects[session["dir"]])
                snapshots = _state_snapshots(paths)
                committed = False
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    write_one(cur, rows)
                    verify_one(cur, rows)
                    # Prepared provenance survives a crash between SQL and JSON commits.
                    bs.record_zcode_injection(
                        paths, db_path, key, rows["sid"], tool, session["src"],
                        rows["title"], session["mtime"], status="prepared")
                    conn.commit()
                    committed = True
                    bs.record_zcode_injection(
                        paths, db_path, key, rows["sid"], tool, session["src"],
                        rows["title"], session["mtime"])
                except BaseException:
                    conn.rollback()
                    if committed:
                        # Compensate only the freshly generated session ID.
                        cur.execute("BEGIN IMMEDIATE")
                        for sql in (SQL_DEL_PART, SQL_DEL_MESSAGE, SQL_DEL_ENTRY, SQL_DEL_SESSION):
                            cur.execute(sql, (rows["sid"],))
                        conn.commit()
                    _restore_snapshots(paths, snapshots)
                    raise
                count += 1
                print(f"  ✓ [{tool}] {rows['title'][:30]} msgs={len(rows['messages'])}")
                time.sleep(BATCH_SLEEP)
            after = cur.execute(SQL_COUNT_SESSION).fetchone()[0]
        finally:
            conn.close()
        print(f"完成：注入 {count}/{len(targets)}，会话总数 {before} → {after}")
        if Path(db_path).resolve() == Path(REAL_DB).resolve():
            print("请重启 ZCode 桌面版验证会话列表。")
        return 0


def do_rollback(db_path):
    paths = state_paths(db_path)
    with bs.operation_lock(paths["lock"]):
        ledger = load_json(paths["ledger"], {})
        if not isinstance(ledger, dict):
            raise ValueError("Invalid ledger")
        manifest = bs.load_zcode_manifest(paths["manifest"], db_path)
        entries = [entry for entry in manifest["sessions"]
                   if entry.get("status") in ("committed", "prepared")]
        for entry in entries:
            if not re.fullmatch(r"sess_[0-9a-zA-Z_-]{8,80}", entry.get("id", "")):
                raise ValueError("Invalid rollback session ID")
            if not entry.get("ledger_key", "").startswith("zcode:"):
                raise ValueError("Rollback entry has no scoped ledger key")
        if not entries:
            print("清单为空，无回滚对象。")
            return 0
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
        import bridge_ops
        from types import SimpleNamespace
        base=paths["ledger"].parent
        env=SimpleNamespace(HOME=HOME,BRIDGE=base,LEDGER=paths["ledger"],PROVENANCE=paths["provenance"],Z_DB=db_path,
                            CX_STATE=base/"unused-state",CX_SQLITE=base/"unused-history",WB_DB=base/"unused-wb",
                            CC_ROOT=base/"unused-cc",CX_ROOT=base/"unused-cx",WB_ROOT=base/"unused-wb-root")
        removed = []
        for entry in entries:
            with connect_db(db_path,readonly=True) as conn:
                present=conn.execute(SQL_HAS_SESSION,(entry["id"],)).fetchone()
            if not present: continue
            bridge_ops.delete_session(env,"zcode","zcode:"+entry["id"])
            removed.append(entry)
        ledger=load_json(paths["ledger"],{})
        manifest=bs.load_zcode_manifest(paths["manifest"],db_path)
        removed_ids = {entry["id"] for entry in removed}
        for entry in removed:
            key, sid = entry["ledger_key"], entry["id"]
            if ledger.get(key) == sid:
                ledger.pop(key)
            records = bs.provenance_records(paths["provenance"])
            if sid in records:
                bs.tombstone_provenance(paths["provenance"], sid, status="rolled_back")
        for entry in manifest["sessions"]:
            if entry.get("id") in removed_ids:
                entry["status"] = "rolled_back"
        manifest.setdefault("rolled_back", []).extend(sorted(removed_ids))
        dump_json(paths["ledger"], ledger)
        dump_json(paths["manifest"], manifest)
        print(f"回滚完成：删除 {len(removed_ids)} 个匹配会话，其他账本保留。")
        return 0


def main():
    ap = argparse.ArgumentParser(description="ZCode 会话注入（三期B）")
    ap.add_argument("--db", default=REAL_DB, help="目标库路径")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不写入")
    ap.add_argument("--rollback", action="store_true", help="按清单回滚")
    args = ap.parse_args()
    if args.rollback:
        sys.exit(do_rollback(args.db))
    sys.exit(do_inject(args.db, args.dry_run))


if __name__ == "__main__":
    main()
