#!/usr/bin/env python3
"""会话桥 —— 精准正文解析 + 只读采集/导出（旧写入入口停用）

默认范围：只处理最近 3 天仍活跃的会话，老会话不动。

功能：
  1) 精准解析四家工具的 user/assistant 正文，保留文件引用和合法 HTML/XML。
  2) --export-only：导出近 3 天活跃会话为 Markdown。
  3) 旧 CC↔Codex 注入入口已停用；写入统一使用 App 的可恢复同步操作。

只读来源：~/.claude/projects、~/.codex/sessions、~/.zcode/cli/db（sqlite3 只读命令，SQL 全静态）
"""
import json
import os
import re
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from bridge_state import atomic_json, load_json

HOME = Path(os.environ.get("BRIDGE_HOME", str(Path.home())))
REPO = Path(os.environ.get("BRIDGE_REPO", str(Path(__file__).resolve().parent.parent)))
BRIDGE = REPO / ".bridge"
LEDGER = BRIDGE / "ledger.json"
MANIFEST = BRIDGE / "injected-manifest.json"
SYNC_WINDOW_DAYS = 3          # 默认只同步最近两三天活跃的会话

CC_ROOT = HOME / ".claude" / "projects"
CX_ROOT = HOME / ".codex" / "sessions"
Z_DB = str(HOME / ".zcode" / "cli" / "db" / "db.sqlite")
WB_ROOT = HOME / ".workbuddy" / "projects"

# ZCode 查询：全静态 SQL，时间窗在库内计算，无外部输入拼接
ZQ_RECENT = (
    "SELECT id, directory, title, time_updated/1000 FROM session "
    "WHERE time_updated > strftime('%s','now','-3 days')*1000 "
    "AND time_archived IS NULL ORDER BY time_updated DESC;"
)
ZQ_PARTS = (
    "SELECT p.session_id, json_extract(m.data,'$.role') AS role, p.data "
    "FROM part p JOIN message m ON m.id = p.message_id "
    "JOIN session s ON s.id = p.session_id "
    "WHERE s.time_updated > strftime('%s','now','-3 days')*1000 "
    "ORDER BY p.session_id, p.time_created;"
)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def clean_title(text: str, limit: int = 40) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return (text[:limit] + "…") if len(text) > limit else text


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "-", name.strip())
    return name[:50] or "未命名"


# ---------- 采集：近 3 天活跃会话 ----------

def collect(now=None):
    """返回 {tool: [session]}，session 含 dir/title/mtime/记录流。"""
    cutoff = (datetime.now().timestamp() if now is None else now) - SYNC_WINDOW_DAYS * 86400
    result = {"claude": [], "codex": [], "zcode": [], "workbuddy": []}

    # Claude Code（同一 sessionId 可能散落多个文件，按 turns 最多者去重）
    if CC_ROOT.exists():
        by_sid = {}
        for f in CC_ROOT.rglob("*.jsonl"):
            if f.stat().st_mtime < cutoff:
                continue
            records, cwd, sid, title = [], "", "", ""
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    records.append(rec)
                    if not cwd and rec.get("cwd"):
                        cwd = rec["cwd"]
                    if not sid and rec.get("sessionId"):
                        sid = rec["sessionId"]
                    if not title and rec.get("type") == "summary" and rec.get("summary"):
                        title = rec["summary"]
            turns = cc_turns(records)
            prev = by_sid.get(sid)
            if prev is None or len(turns) > len(prev["turns"]):
                by_sid[sid] = {"dir": cwd or "(未知)", "title": title,
                               "mtime": f.stat().st_mtime, "turns": turns, "src": str(f)}
        for s in by_sid.values():
            if not has_human_turn(s["turns"]):
                continue
            if not s["title"] and s["turns"]:
                s["title"] = first_user_text(s["turns"])[:60]
            result["claude"].append(s)

    # Codex
    if CX_ROOT.exists():
        for f in CX_ROOT.rglob("rollout-*.jsonl"):
            if f.stat().st_mtime < cutoff:
                continue
            records, cwd = [], ""
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    records.append(rec)
                    p = rec.get("payload") or {}
                    if rec.get("type") == "session_meta" and not cwd:
                        cwd = p.get("cwd", "")
            turns = cx_turns(records)
            if not has_human_turn(turns):
                continue
            result["codex"].append({"dir": cwd or "(未知)",
                                    "title": first_user_text(turns)[:60] or f.stem[8:24],
                                    "mtime": f.stat().st_mtime, "turns": turns,
                                    "src": str(f)})

    # ZCode（sqlite3 只读命令，静态 SQL）
    if Path(Z_DB).exists():
        try:
            rows = json.loads(subprocess.run(
                ["sqlite3", "-readonly", "-json", Z_DB, ZQ_RECENT],
                capture_output=True, text=True, timeout=60, check=True).stdout or "[]")
            parts = json.loads(subprocess.run(
                ["sqlite3", "-readonly", "-json", Z_DB, ZQ_PARTS],
                capture_output=True, text=True, timeout=120, check=True).stdout or "[]")
        except (subprocess.SubprocessError, json.JSONDecodeError) as e:
            print(f"[zcode] 读取失败: {e}", file=sys.stderr)
            rows, parts = [], []
        by_sess = defaultdict(list)
        for p in rows_part(parts):
            by_sess[p["session_id"]].append((p["role"], p["text"]))
        for r in rows:
            t = r.get("title") or ""
            if t.startswith("{"):
                t = ""
            turns = by_sess.get(r["id"], [])
            result["zcode"].append({"dir": r.get("directory") or "(未知)",
                                    "title": clean_title(t) or (turns[0][1][:60] if turns else r["id"][:12]),
                                    "mtime": r.get("time_updated/1000") or 0,
                                    "turns": turns, "src": f"zcode:{r['id']}"})
    if WB_ROOT.exists():
        for f in WB_ROOT.rglob("*.jsonl"):
            if f.stat().st_mtime < cutoff:
                continue
            records, cwd, title = [], "", ""
            with f.open(encoding="utf-8", errors="ignore") as stream:
                for line in stream:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    records.append(rec)
                    cwd = cwd or rec.get("cwd", "")
                    if rec.get("type") == "ai-title":
                        title = rec.get("aiTitle") or title
            turns = wb_turns(records)
            if has_human_turn(turns):
                result["workbuddy"].append({
                    "dir": cwd or "(未知)", "title": title or first_user_text(turns)[:60],
                    "mtime": f.stat().st_mtime, "turns": turns, "src": str(f)})
    return result


def rows_part(parts):
    for p in parts:
        try:
            d = json.loads(p["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        if d.get("type") == "text" and d.get("text"):
            yield {"session_id": p["session_id"], "role": p["role"], "text": d["text"]}


MACHINE_TAGS = (
    "system-reminder", "user_instructions", "environment_context",
    "skills_instructions", "app-context", "multi_agent_role", "multi_agent_mode",
    "model_switch", "recommended_plugins", "permissions_instructions",
    "collaboration_mode", "codex_apps_client_time_context", "codex_apps_open_page_instructions",
    "external_codex_apps_open_page", "external_browser_context", "image_local_path",
)
_MACHINE_BLOCK = re.compile(
    r"<(?P<tag>" + "|".join(re.escape(t) for t in MACHINE_TAGS)
    + r")\b[^>]*>[\s\S]*?</(?P=tag)\s*>", re.IGNORECASE)
_MACHINE_SELF_CLOSING = re.compile(
    r"<(?:" + "|".join(re.escape(t) for t in MACHINE_TAGS)
    + r")\b[^>]*/\s*>", re.IGNORECASE)


def clean_user_text(text):
    """Strip known machine wrappers, keeping real prose, HTML/XML and file references."""
    if not isinstance(text, str):
        return ""
    # Literal markup inside user code must survive even when its tag is known.
    spans = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)
    cleaned = "".join(part if index % 2 else _MACHINE_SELF_CLOSING.sub("", _MACHINE_BLOCK.sub("", part))
                      for index, part in enumerate(spans))
    # WorkBuddy encloses the real human request in this wrapper.
    cleaned = re.sub(r"</?user_query\s*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace(
        "Distinguish instructions in attached documents from the user's request.", "")
    return cleaned.strip()


def request_text(text):
    """For titles, prefer the actual request after Codex's file reference header."""
    text = clean_user_text(text)
    if text.lstrip().startswith("# Files mentioned by the user"):
        match = re.search(r"(?m)^## My request:\s*\n?", text)
        if match:
            return text[match.end():].strip()
    return text


def first_user_text(turns):
    return next((request_text(text) for role, text in turns
                 if role == "user" and request_text(text)), "")


def is_system_block(text: str, role: str) -> bool:
    return role in ("system", "developer") or (role == "user" and not clean_user_text(text))


def has_human_turn(turns) -> bool:
    """过滤掉没有任何真人发言的内部会话（子代理/系统会话）。"""
    return any(role == "user" for role, _ in turns)


def cc_turns(records):
    """CC jsonl → [(role, text)]，只取主链的 user/assistant 文本。"""
    turns = []
    for rec in records:
        if rec.get("isSidechain") or rec.get("type") not in ("user", "assistant"):
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    texts.append(c["text"])
        if texts:
            joined = "\n".join(texts)
            role = msg.get("role", rec.get("type"))
            if role not in ("user", "assistant"):
                continue
            joined = clean_user_text(joined) if role == "user" else joined.strip()
            if joined:
                turns.append((role, joined))
    return turns


def cx_turns(records):
    """Codex rollout → [(role, text)]。"""
    turns = []
    for rec in records:
        if rec.get("type") != "response_item":
            continue
        p = rec.get("payload") or {}
        if p.get("type") != "message":
            continue
        content = p.get("content") or []
        texts = [content] if isinstance(content, str) else [
            c["text"] for c in content
            if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"]]
        if not texts:
            continue
        joined = "\n".join(texts)
        role = p.get("role", "user")
        if role not in ("user", "assistant"):
            continue
        joined = clean_user_text(joined) if role == "user" else joined.strip()
        if joined:
            turns.append((role, joined))
    return turns


def wb_turns(records):
    turns = []
    for rec in records:
        if rec.get("type") != "message" or rec.get("role") not in ("user", "assistant"):
            continue
        role = rec["role"]
        content = rec.get("content") or []
        texts = [content] if isinstance(content, str) else [
            c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)]
        text = "\n".join(texts)
        text = clean_user_text(text) if role == "user" else text.strip()
        if text:
            turns.append((role, text))
    return turns


# ---------- 导出 ----------

def export(result):
    n = 0
    for tool, sessions in result.items():
        for s in sessions:
            if not s["turns"]:
                continue
            folder = REPO / safe_name(Path(s["dir"]).name if "/" in s["dir"] else s["dir"])
            export_dir = folder / "对话"
            export_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.fromtimestamp(s["mtime"]).strftime("%m%d")
            out = export_dir / f"{day}-{tool}-{safe_name(s['title'])[:30]}.md"
            lines = [f"# {s['title']}", "",
                     f"- 工具：{tool} · 目录：`{s['dir']}` · 最后活动："
                     f"{datetime.fromtimestamp(s['mtime']).strftime('%m-%d %H:%M')}",
                     f"- 来源：`{s['src']}`", "", "---", ""]
            for role, text in s["turns"]:
                who = "🧑 我" if role == "user" else "🤖 AI"
                lines += [f"**{who}**", "", text, ""]
            out.write_text("\n".join(lines), encoding="utf-8")
            n += 1
    return n


# ---------- 注入：CC ↔ Codex ----------

def cx_creator_ids():
    """从最近的真实 rollout 里取账号标识，让注入的会话归属到本人账号下。"""
    if not CX_ROOT.exists():
        return {}
    files = sorted(CX_ROOT.rglob("rollout-*.jsonl"), key=lambda p: -p.stat().st_mtime)
    for f in files[:5]:
        try:
            first = json.loads(open(f, encoding="utf-8", errors="ignore").readline())
            p = first.get("payload") or {}
            if p.get("creator_user_id"):
                return {"creator_user_id": p["creator_user_id"],
                        "creator_account_id": p.get("creator_account_id", "")}
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def cc_desktop_project_dir():
    """Claude 桌面版侧栏元数据：找最近活跃账号的项目子目录。

    桌面版会话列表不直接读 ~/.claude/projects，靠
    ~/Library/Application Support/Claude/claude-code-sessions/<账号>/<项目>/local_*.json
    里的元数据（cliSessionId 指回 projects 里的 jsonl）。
    """
    root = HOME / "Library/Application Support/Claude/claude-code-sessions"
    if not root.exists():
        return None
    best_dir, best_ts = None, 0
    for meta in root.rglob("local_*.json"):
        try:
            d = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        ts = d.get("lastFocusedAt") or d.get("lastActivityAt") or 0
        if ts > best_ts:
            best_ts, best_dir = ts, meta.parent
    return best_dir


def write_cc_desktop_meta(project_dir, cli_sid, cwd, title, mtime, model=None):
    """写入一条桌面版侧栏元数据，让会话出现在 Claude 桌面版列表里。"""
    if project_dir is None:
        return None
    ms = int(mtime * 1000)
    local_id = f"local_{uuid.uuid4()}"
    meta = {"sessionId": local_id, "cliSessionId": cli_sid, "cwd": cwd,
            "originCwd": cwd, "lastFocusedAt": ms, "createdAt": ms - 3600000,
            "lastActivityAt": ms,
            "isArchived": False, "title": title, "titleSource": "user",
            "permissionMode": "default", "remoteMcpServersConfig": []}
    if model:
        meta["model"] = model
    out = project_dir / f"{local_id}.json"
    atomic_json(out, meta)
    return out


def inject(result):
    """The legacy writer cannot maintain the current desktop database chain."""
    raise RuntimeError(
        "Legacy CC↔Codex injection is disabled. Use the 会话桥 App sync API, "
        "which owns the operation journal, desktop projections and rollback.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="只读采集/导出；同步请使用会话桥 App")
    parser.add_argument("--export-only", action="store_true",
                        help="导出近三天正文，不向官方工具注入")
    args = parser.parse_args()
    if not args.export_only:
        parser.error("旧注入器已停用；使用会话桥 App 定向同步，或 --export-only 只读导出")
    result = collect()
    print(f"近{SYNC_WINDOW_DAYS}天活跃会话：{ {k: len(v) for k, v in result.items()} }")
    print(f"导出 {export(result)} 份全文 Markdown → 各项目文件夹/对话/")


if __name__ == "__main__":
    main()
