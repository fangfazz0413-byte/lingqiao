"""Lingqiao usage collector.

Coding-plan quota for GLM, Kimi Coding and MiniMax (keys live in the macOS
Keychain), plus local token usage from ZCode, CC Switch, WorkBuddy and Claude
Code history.  Runtime state is stored under the private LingqiaoUsage
directory.  This data layer started as the author's earlier "Mtoken" menu-bar
tool and now lives inside Lingqiao; no Mtoken executable or app is needed.
"""

import collections
import concurrent.futures
import datetime
import glob
import json
import os
from pathlib import Path
import sqlite3
import getpass
import tempfile
import time
import urllib.error
import urllib.request

import platform_paths

KEYCHAIN_PREFIX = "aiquota-"
HOME = os.path.expanduser("~")
TIMEOUT = 12
QUOTA_TTL = 5 * 60            # 额度缓存5分钟，及时展示窗口消耗
LOCAL_TTL = 30 * 60           # 本机统计缓存30分钟；跨日直接刷新

# ================================================================= 额度：供应商
PROVIDERS = {
    "glm": {
        "label": "智谱 GLM",
        "hint": "z.ai 或 bigmodel.cn 控制台 → API Keys",
        "urls": {"global": "https://api.z.ai/api/monitor/usage/quota/limit",
                 "cn": "https://open.bigmodel.cn/api/monitor/usage/quota/limit"},
        "default_region": "global",
        # 智谱这个接口不带 Bearer 前缀
        "auth": lambda k: {"Authorization": k, "Accept-Language": "en-US,en"},
    },
    "kimi": {
        "label": "Kimi Coding",
        "hint": "kimi.com/code/console → API Key",
        "urls": {"global": "https://api.kimi.com/coding/v1/usages"},
        "default_region": "global",
        "auth": lambda k: {"Authorization": f"Bearer {k}"},
    },
    "minimax": {
        "label": "MiniMax",
        "hint": "MiniMax 开放平台 → 接口密钥",
        "urls": {"cn": "https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains",
                 "global": "https://www.minimax.io/v1/api/openplatform/coding_plan/remains"},
        "default_region": "cn",
        "auth": lambda k: {"Authorization": f"Bearer {k}"},
    },
}


# ================================================================= 钥匙串
def _security():
    """Load macOS Security only when needed; no credentials enter subprocesses."""
    try:
        import Security
    except ImportError as exc:
        raise RuntimeError("原生钥匙串组件不可用") from exc
    return Security


def _target(name):
    if name not in PROVIDERS:
        raise ValueError("未知额度来源")
    return KEYCHAIN_PREFIX + name


def _key_query(api, name):
    return {api.kSecClass: api.kSecClassGenericPassword,
            api.kSecAttrService: _target(name)}


# Windows 上同样的三件事交给凭据管理器（wincred.py），名字沿用 aiquota-<来源>。
def _wincred_read(name):
    import wincred
    try:
        return wincred.read(_target(name))
    except OSError as exc:
        raise RuntimeError("凭据管理器读取失败") from exc


def kc_get(name):
    if platform_paths.WINDOWS:
        raw = _wincred_read(name)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8").strip() or None
        except UnicodeError as exc:
            raise RuntimeError("凭据管理器内容格式无效") from exc
    api = _security()
    query = {**_key_query(api, name), api.kSecReturnData: True,
             api.kSecMatchLimit: api.kSecMatchLimitOne,
             api.kSecUseAuthenticationUI: api.kSecUseAuthenticationUIFail}
    status, value = api.SecItemCopyMatching(query, None)
    if status == api.errSecItemNotFound:
        return None
    if status != api.errSecSuccess:
        raise RuntimeError("钥匙串读取失败（状态 %s）" % status)
    try:
        return bytes(value).decode("utf-8").strip() or None
    except (UnicodeError, TypeError, ValueError) as exc:
        raise RuntimeError("钥匙串内容格式无效") from exc


def kc_configured(name):
    """Check item attributes without retrieving key bytes."""
    if platform_paths.WINDOWS:
        return _wincred_read(name) is not None
    api = _security()
    status, unused = api.SecItemCopyMatching(
        {**_key_query(api, name), api.kSecReturnAttributes: True,
         api.kSecMatchLimit: api.kSecMatchLimitOne,
         api.kSecUseAuthenticationUI: api.kSecUseAuthenticationUIFail}, None)
    if status == api.errSecItemNotFound:
        return False
    if status != api.errSecSuccess:
        raise RuntimeError("钥匙串状态读取失败（状态 %s）" % status)
    return True


def kc_set(name, value):
    if platform_paths.WINDOWS:
        import wincred
        try:
            wincred.write(_target(name), getpass.getuser(), value.encode("utf-8"))
        except OSError:
            return False, "凭据管理器保存失败"
        return True, ""
    api = _security()
    query = _key_query(api, name)
    raw = value.encode("utf-8")
    status = api.SecItemUpdate(query, {api.kSecValueData: raw})
    if status == api.errSecItemNotFound:
        status, unused = api.SecItemAdd({**query, api.kSecAttrAccount: getpass.getuser(),
                                        api.kSecValueData: raw}, None)
    return status == api.errSecSuccess, ("" if status == api.errSecSuccess else "钥匙串保存失败")


# ================================================================= 各家专用解析
def ts_fmt(ms):
    if not ms:
        return None
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return None


def iso_fmt(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(
            str(s).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(s)


def iso_epoch(s):
    """ISO8601 → epoch 秒，给前端算「时间进度」用。"""
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(
            str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def parse_glm(d):
    """data.limits[]：percentage 就是已用百分比，unit/number 决定窗口。"""
    data = d.get("data") or {}
    out = []
    for l in data.get("limits", []):
        unit, num = l.get("unit"), l.get("number")
        if unit == 3:
            win = "5h" if num == 5 else f"{num} 小时"
        elif unit == 6:
            win = "7d" if num == 1 else f"{num} 周"
        elif unit == 7:
            win = "month"
        else:
            win = f"窗口 {num}/{unit}"
        if l.get("percentage") is None:
            continue
        out.append({"window": win, "pct": float(l["percentage"]),
                    "used": l.get("currentValue"), "total": l.get("usage"),
                    "unit_name": "credits", "reset": ts_fmt(l.get("nextResetTime")),
                    "reset_ts": (l.get("nextResetTime") or 0) / 1000 or None})
    return out, {"plan": data.get("level")}


def parse_kimi(d):
    """usages.limit_5h / limit_7d 里的 used_ratio 是 0~1。"""
    out = []
    usages = d.get("usages") or {}
    top = d.get("usage") or {}
    for k, win in (("limit_5h", "5h"), ("limit_7d", "7d")):
        v = usages.get(k)
        if not isinstance(v, dict) or v.get("used_ratio") is None:
            continue
        item = {"window": win, "pct": float(v["used_ratio"]) * 100,
                "reset": iso_fmt(v.get("reset_time")),
                "reset_ts": iso_epoch(v.get("reset_time"))}
        if win == "7d":
            try:
                item.update(used=float(top["used"]), total=float(top["limit"]), unit_name="次")
            except (TypeError, ValueError, KeyError):
                pass
        out.append(item)
    return out, {}


def parse_minimax(d):
    """model_remains[] 按模型分组；字段是「剩余」百分比，status==3 表示该窗口不限额。"""
    models = d.get("model_remains") or []
    out = []
    UNLIMITED = 3
    for win, field, endk, statusk in (
            ("5h", "current_interval_remaining_percent", "end_time", "current_interval_status"),
            ("7d", "current_weekly_remaining_percent", "weekly_end_time", "current_weekly_status")):
        vals = [m for m in models if isinstance(m.get(field), (int, float))]
        if not vals:
            continue
        limited = [m for m in vals if m.get(statusk) != UNLIMITED]
        if not limited:
            out.append({"window": win, "unlimited": True,
                        "detail": "、".join(str(m.get("model_name")) for m in vals) + " 均不限额"})
            continue
        worst = min(limited, key=lambda m: m[field])
        out.append({"window": win, "pct": 100.0 - float(worst[field]),
                    "reset": ts_fmt(worst.get(endk)),
                    "reset_ts": (worst.get(endk) or 0) / 1000 or None,
                    "detail": "、".join(f"{m.get('model_name')} 剩 {m[field]}%" for m in limited)})
    return out, {}


PARSERS = {"glm": parse_glm, "kimi": parse_kimi, "minimax": parse_minimax}


def fetch_quota(name, spec):
    key = kc_get(name)
    if not key:
        return {"name": name, "label": spec["label"], "configured": False}
    region = os.environ.get(f"AIQUOTA_{name.upper()}_REGION", spec["default_region"])
    url = spec["urls"].get(region) or spec["urls"][spec["default_region"]]
    req = urllib.request.Request(url, headers={"Accept": "application/json", **spec["auth"](key)})

    data, last = None, None
    for attempt in range(2):                      # TLS 短暂抖动重试一次，总等待有界
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:       # 4xx/5xx 是确定性错误，不重试
            # Do not echo provider response bodies: gateways occasionally include
            # account identifiers or diagnostic material in an error payload.
            try: e.read()
            except OSError: pass
            return {"name": name, "label": spec["label"], "configured": True,
                    "error": f"HTTP {e.code}"}
        except Exception as e:
            last = f"网络请求失败（{type(e).__name__}）"
            if attempt < 1:
                time.sleep(0.6)
    if data is None:
        return {"name": name, "label": spec["label"], "configured": True, "error": last}

    if isinstance(data, dict):                    # 有些家 HTTP 200，错误码塞在 body 里
        code = data.get("code")
        if data.get("success") is False or (isinstance(code, int) and code not in (0, 200)):
            return {"name": name, "label": spec["label"], "configured": True,
                    "error": f"额度接口返回错误（代码 {code}）"}

    try:
        windows, meta = PARSERS[name](data)
    except Exception as e:
        return {"name": name, "label": spec["label"], "configured": True,
                "error": f"额度解析失败（{type(e).__name__}）"}
    return {"name": name, "label": spec["label"], "configured": True,
            "windows": windows, "plan": meta.get("plan"),
            **({"error": "额度接口未返回可识别窗口"} if not windows else {})}


# ================================================================= 本地用量
# 每个采集器统一吐 records：{month, model, tokens, calls, cost}，再由上层聚合。

def _ro(path):
    """Read-only SQLite access, never ignoring an existing uncheckpointed WAL."""
    uri = Path(path).expanduser().resolve().as_uri()
    connection = None
    try:
        connection = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=5)
        connection.execute("select 1 from sqlite_master limit 1")
        return connection
    except sqlite3.OperationalError:
        if connection is not None:
            connection.close()
        wal = Path(str(path) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise sqlite3.OperationalError("用量数据库 WAL 尚未合并，已停止忽略增量数据")
        return sqlite3.connect(uri + "?immutable=1", uri=True, timeout=5)


HEAT_DAYS = 365       # 保留一年，前端按月切片
CURVE_DAYS = 8        # 今日曲线 + 近 7 日均值所需的天数
LOCAL_VERSION = 5     # 修正近90天桶；旧缓存首次检查时自动重新采集


def day_list(n):
    today = datetime.date.today()
    return [(today - datetime.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


# daily 的每个桶是 [总量, 调用数, 未缓存输入, 输出, 缓存]
DAILY_SLOTS = 5


class Series:
    """一个来源的时间序列：按天、按天×模型、按天×小时。只保留近一年。"""

    def __init__(self):
        self.floor = day_list(HEAT_DAYS)[0]
        self.daily = collections.defaultdict(lambda: [0] * DAILY_SLOTS)
        self.hourly = collections.defaultdict(lambda: [0.0] * 24)
        self.dm = collections.defaultdict(
            lambda: collections.defaultdict(lambda: [0] * DAILY_SLOTS))

    def add_day(self, day, model, tokens, calls, unc, out, cache):
        """已按天汇总好的行（没有时刻，不进小时桶）。"""
        if not day or day < self.floor:
            return
        vals = (tokens or 0, calls or 0, unc or 0, out or 0, cache or 0)
        for b in (self.daily[day], self.dm[day][model or "未知"]):
            for i, v in enumerate(vals):
                b[i] += v

    def add_hour(self, day, hour, tokens):
        if day and day >= self.floor and 0 <= hour < 24:
            self.hourly[day][hour] += tokens or 0

    def add_ts(self, ts_ms, model, parts, calls):
        """单次调用：按本地时区落进日桶、模型桶和小时桶。parts = (未缓存, 输出, 缓存)。"""
        dt = datetime.datetime.fromtimestamp(ts_ms / 1000)
        day = dt.date().isoformat()
        self.add_day(day, model, sum(parts), calls, *parts)
        self.add_hour(day, dt.hour, sum(parts))

    def export(self):
        return {"daily": self.daily, "hourly": self.hourly, "day_models": self.dm}


def src_zcode():
    p = f"{HOME}/.zcode/cli/db/db.sqlite"
    if not os.path.exists(p):
        return None
    # ZCode 的 input_tokens 已经包含缓存命中部分（全库 0 行 cache_read>input），
    # 再加 cache_read 会把同一批 token 算两遍。总量口径与 ZCode 界面一致。
    TOK = "input_tokens+output_tokens+reasoning_tokens"
    ser = Series()
    c = _ro(p)
    rows = c.execute(f"""
        select strftime('%Y-%m', started_at/1000, 'unixepoch', 'localtime') as m,
               model_id, sum({TOK}), count(*)
        from model_usage group by m, model_id
    """).fetchall()
    drows = c.execute(f"""
        select date(started_at/1000, 'unixepoch', 'localtime') as d, model_id, sum({TOK}), count(*),
               sum(input_tokens-cache_read_input_tokens), sum(output_tokens+reasoning_tokens),
               sum(cache_read_input_tokens)
        from model_usage
        where date(started_at/1000, 'unixepoch', 'localtime') >= ?
        group by d, model_id
    """, (ser.floor,)).fetchall()
    hrows = c.execute(f"""
        select date(started_at/1000, 'unixepoch', 'localtime') as d,
               cast(strftime('%H', started_at/1000, 'unixepoch', 'localtime') as integer) as h,
               sum({TOK})
        from model_usage
        where date(started_at/1000, 'unixepoch', 'localtime') >= ?
        group by d, h
    """, (ser.floor,)).fetchall()
    c.close()

    for d, mo, t, ca, i, o, ch in drows:
        ser.add_day(d, mo, t, ca, i, o, ch)
    for d, h, t in hrows:
        ser.add_hour(d, h, t)
    return {"label": "ZCode", "note": "数据来源：ZCode 本地数据库 · 含 GLM / Kimi / MiniMax / Grok",
            "records": [{"month": m, "model": mo, "tokens": t or 0, "calls": ca, "cost": 0}
                        for m, mo, t, ca in rows],
            **ser.export()}


def _cc_separate_apps(c):
    """判断每个 app_type 的 input 是否已含缓存。

    Anthropic 口径（claude）里 input 与 cache_read 是分开的，相加才是总量；
    OpenAI 口径（codex）的 input 已经包含缓存命中，再加会重复计算。
    经验判断：出现 cache_read > input 的行，说明两者独立。
    """
    sep = set()
    for app, n, gt in c.execute("""
        select app_type, count(*),
               sum(case when cache_read_tokens > input_tokens then 1 else 0 end)
        from proxy_request_logs group by app_type
    """):
        if n and gt / n > 0.05:
            sep.add(app)
    return sep


def _cc_tok_expr(sep):
    """按 app_type 生成总量表达式。"""
    if not sep:
        return "(input_tokens+output_tokens)"
    lst = ",".join("'%s'" % a.replace("'", "''") for a in sorted(sep))
    return ("(case when app_type in (%s) "
            "then input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens "
            "else input_tokens+output_tokens end)" % lst)


def _cc_parts_expr(sep):
    """返回 (未缓存, 输出, 缓存) 三个表达式。"""
    if not sep:
        unc = "(input_tokens-cache_read_tokens)"
        cache = "(cache_read_tokens)"
    else:
        lst = ",".join("'%s'" % a.replace("'", "''") for a in sorted(sep))
        unc = ("(case when app_type in (%s) then input_tokens "
               "else input_tokens-cache_read_tokens end)" % lst)
        cache = ("(case when app_type in (%s) then cache_read_tokens+cache_creation_tokens "
                 "else cache_read_tokens end)" % lst)
    return unc, "output_tokens", cache


CC_DB = f"{HOME}/.cc-switch/cc-switch.db"


def src_ccswitch():
    if not os.path.exists(CC_DB):
        return None
    c = _ro(CC_DB)
    SEP = _cc_separate_apps(c)
    RAW_TOK = _cc_tok_expr(SEP)
    U_EXPR, O_EXPR, C_EXPR = _cc_parts_expr(SEP)
    ser = Series()
    rows = c.execute("""
        select substr(date,1,7) as m, model, sum(%s),
               sum(request_count), sum(cast(total_cost_usd as real))
        from usage_daily_rollups group by m, model
    """ % RAW_TOK).fetchall()
    # CC Switch 把老日志汇总进 usage_daily_rollups 后就删掉原始行，
    # 近期的还留在 proxy_request_logs 里。两张表零重叠，必须合起来读，
    # 只读汇总表会丢掉最近一个月。
    raw_rows = c.execute(f"""
        select strftime('%Y-%m', created_at, 'unixepoch', 'localtime') as m, model,
               sum({RAW_TOK}), count(*), sum(cast(total_cost_usd as real))
        from proxy_request_logs group by m, model
    """).fetchall()
    drows = c.execute("""
        select date, model, sum(%s), sum(request_count), sum(%s), sum(%s), sum(%s)
        from usage_daily_rollups where date >= ? group by date, model
    """ % (RAW_TOK, U_EXPR, O_EXPR, C_EXPR), (ser.floor,)).fetchall()
    raw_drows = c.execute(f"""
        select date(created_at, 'unixepoch', 'localtime') as d, model, sum({RAW_TOK}), count(*),
               sum({U_EXPR}), sum({O_EXPR}), sum({C_EXPR})
        from proxy_request_logs
        where date(created_at, 'unixepoch', 'localtime') >= ? group by d, model
    """, (ser.floor,)).fetchall()
    # 原始日志带时刻，能进小时分布；汇总表只有日期
    raw_hrows = c.execute(f"""
        select date(created_at, 'unixepoch', 'localtime') as d,
               cast(strftime('%H', created_at, 'unixepoch', 'localtime') as integer) as h,
               sum({RAW_TOK})
        from proxy_request_logs
        where date(created_at, 'unixepoch', 'localtime') >= ? group by d, h
    """, (ser.floor,)).fetchall()
    last = c.execute("""
        select max(d) from (select max(date) d from usage_daily_rollups
                            union all
                            select date(max(created_at),'unixepoch','localtime') from proxy_request_logs)
    """).fetchone()[0]
    c.close()

    for src in (drows, raw_drows):
        for d, mo, t, ca, i, o, ch in src:
            ser.add_day(d, mo, t, ca, i, o, ch)
    for d, h, t in raw_hrows:
        ser.add_hour(d, h, t)

    recs = [{"month": m, "model": mo or "?", "tokens": t or 0,
             "calls": ca or 0, "cost": co or 0}
            for m, mo, t, ca, co in list(rows) + list(raw_rows)]
    return {"label": "CC Switch",
            "note": f"数据来源：CC Switch 日汇总与请求日志 · 截至 {last}",
            "records": recs, **ser.export()}


WB_DIR = f"{HOME}/.workbuddy"


def _wb_raw_entries():
    """WorkBuddy 自己的会话日志（含 subagents/ 子任务目录），随用随写，永远是最新的。

    一次请求会拆成 reasoning / function_call / message 好几行，usage 完全相同；
    分叉出来的会话文件还会把整段历史复制一遍。都按 messageId 只算一次。
    返回 [(毫秒时间戳, 模型, (未缓存, 输出, 缓存), 调用数)]。
    """
    seen = {}
    for fp in glob.glob(f"{WB_DIR}/projects/**/*.jsonl", recursive=True):
        try:
            with open(fp, errors="ignore") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    pd = d.get("providerData") or {}
                    u, mid, ts = pd.get("usage"), pd.get("messageId"), d.get("timestamp")
                    if not isinstance(u, dict) or not mid or mid in seen \
                            or not isinstance(ts, (int, float)):
                        continue
                    inp = u.get("inputTokens", 0) or 0          # 已含缓存命中
                    cache = min(inp, sum((x or {}).get("cached_tokens", 0) or 0
                                         for x in u.get("inputTokensDetails") or []))
                    parts = (inp - cache, u.get("outputTokens", 0) or 0, cache)
                    model = pd.get("model") or pd.get("requestModelId") or "未知"
                    seen[mid] = (ts, model, parts, u.get("requests", 1) or 1)
        except OSError:
            continue
    return list(seen.values())


def _wb_cache_entries():
    """WorkDaddy 算好的统计缓存。它只在 WorkDaddy 重算时才更新、也不含子任务，只拿来补原始日志已被清掉的日子。"""
    try:
        with open(f"{WB_DIR}/.workdaddy-token-stats-cache.json") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    seen = {}
    for bucket in list(d.get("historicalBuckets") or []) + list((d.get("todayFiles") or {}).values()):
        for e in (bucket or {}).get("entries", []):
            seen.setdefault(e.get("key"), e)      # 同一次调用会在多个 jsonl 里重复出现
    out = []
    for e in seen.values():
        if not isinstance(e.get("timestamp"), (int, float)):
            continue
        # WorkDaddy 的 input 同样已含缓存命中（0 行 cacheRead>input）
        cache = (e.get("cacheRead", 0) or 0) + (e.get("cacheWrite", 0) or 0)
        parts = (max(0, (e.get("input", 0) or 0) - cache), e.get("output", 0) or 0, cache)
        out.append((e["timestamp"], e.get("model") or "未知", parts, e.get("calls", 0) or 0))
    return out


def src_workbuddy():
    def day(ts):
        return datetime.datetime.fromtimestamp(ts / 1000).date().isoformat()

    raw = _wb_raw_entries()
    have = {day(e[0]) for e in raw}
    extra = [e for e in _wb_cache_entries() if day(e[0]) not in have]
    if not raw and not extra:
        return None
    agg = collections.defaultdict(lambda: [0, 0])
    ser = Series()
    for ts, model, parts, calls in raw + extra:
        k = (datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m"), model)
        agg[k][0] += sum(parts)
        agg[k][1] += calls
        ser.add_ts(ts, model, parts, calls)
    note = "数据来源：WorkBuddy 会话日志（含子任务）"
    if extra:
        note += f" · {len({day(e[0]) for e in extra})} 天由 WorkDaddy 缓存补全"
    return {"label": "WorkBuddy", "note": note,
            "records": [{"month": m, "model": mo, "tokens": v[0], "calls": v[1], "cost": 0}
                        for (m, mo), v in agg.items()],
            **ser.export()}


def _cc_imported_claude():
    """CC Switch 会把 Claude Code 的会话日志导入进来（request_id = session:<消息id>）。

    返回 (已导入的消息 id, 只剩汇总行的日期)。两者覆盖到的调用不再从 Claude 日志重复计入。
    """
    if not os.path.exists(CC_DB):
        return set(), set()
    try:
        c = _ro(CC_DB)
        ids = {r[0] for r in c.execute(
            "select substr(request_id, 9) from proxy_request_logs "
            "where app_type='claude' and request_id like 'session:%'")}
        days = {r[0] for r in c.execute(
            "select distinct date from usage_daily_rollups where app_type='claude'")}
        c.close()
        return ids, days
    except sqlite3.Error:
        return set(), set()


def src_claude():
    files = glob.glob(f"{HOME}/.claude/projects/**/*.jsonl", recursive=True)
    if not files:
        return None
    imported, rolled_days = _cc_imported_claude()
    agg = collections.defaultdict(lambda: [0, 0])
    ser = Series()
    seen = set()
    for fp in files:
        try:
            with open(fp, errors="ignore") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage") or d.get("usage")
                    if not isinstance(u, dict):
                        continue
                    # 一条回复的每个内容块各写一行、usage 相同，按消息 id 只算一次
                    mid = msg.get("id") or d.get("requestId") or d.get("uuid")
                    if mid in seen or mid in imported:
                        continue
                    seen.add(mid)
                    parts = (u.get("input_tokens", 0) or 0,
                             u.get("output_tokens", 0) or 0,
                             (u.get("cache_creation_input_tokens", 0) or 0)
                             + (u.get("cache_read_input_tokens", 0) or 0))
                    ts = d.get("timestamp") or ""
                    if not sum(parts) or len(ts) < 10:
                        continue
                    try:                       # 日志里是 UTC 的 ISO 串，转成本地时间再分桶
                        dt = datetime.datetime.fromisoformat(
                            ts.replace("Z", "+00:00")).astimezone()
                    except (TypeError, ValueError):
                        continue
                    if dt.date().isoformat() in rolled_days:
                        continue
                    model = msg.get("model") or "claude"
                    k = (dt.strftime("%Y-%m"), model)
                    agg[k][0] += sum(parts)
                    agg[k][1] += 1
                    ser.add_ts(dt.timestamp() * 1000, model, parts, 1)
        except OSError:
            continue
    if not agg:
        return None
    return {"label": "Claude Code", "note": "数据来源：Claude Code 会话日志 · 已排除 CC Switch 导入部分",
            "records": [{"month": m, "model": mo, "tokens": v[0], "calls": v[1], "cost": 0}
                        for (m, mo), v in agg.items()],
            **ser.export()}


SOURCES = [src_zcode, src_ccswitch, src_workbuddy, src_claude]


def _canon_names(sources):
    """同一个模型在不同软件里大小写不同（GLM-5.3-Flash / glm-5.3-flash），合成一个名字。

    取用量最大的那种写法作为显示名。
    """
    votes = collections.defaultdict(collections.Counter)
    for s in sources:
        for r in s["records"]:
            votes[r["model"].lower()][r["model"]] += r["tokens"] or 0
    return {v: cnt.most_common(1)[0][0] for low, cnt in votes.items() for v in cnt}


def collect_local():
    """跑完所有采集器，聚合成前端直接能用的几个视图。"""
    cur = datetime.datetime.now().strftime("%Y-%m")
    sources, errors = [], []
    for fn in SOURCES:
        try:
            r = fn()
            if r:
                sources.append(r)
        except Exception as e:
            errors.append({"label": fn.__name__.replace("src_", ""),
                           "error": f"{type(e).__name__}: {e}"})

    canon = _canon_names(sources)
    name = lambda m: canon.get(m, m)
    days = day_list(HEAT_DAYS)
    today = days[-1]

    month_models = collections.defaultdict(lambda: {"tokens": 0, "calls": 0})
    monthly = collections.defaultdict(lambda: {"tokens": 0, "calls": 0, "cost": 0})
    all_daily = collections.defaultdict(lambda: [0] * DAILY_SLOTS)
    all_hourly = collections.defaultdict(lambda: [0.0] * 24)
    # 某一天用了哪些模型：{day: [{m 模型, s 来源, t 总量, c 调用, u 未缓存, o 输出, k 缓存}]}
    day_models = collections.defaultdict(dict)
    for s in sources:
        for d, v in (s.get("daily") or {}).items():
            for i in range(min(DAILY_SLOTS, len(v))):
                all_daily[d][i] += v[i]
        for d, hrs in (s.get("hourly") or {}).items():
            for h in range(24):
                all_hourly[d][h] += hrs[h]
        for d, models in (s.get("day_models") or {}).items():
            for mo, v in models.items():
                key = (name(mo), s["label"])
                e = day_models[d].setdefault(key, [0] * DAILY_SLOTS)
                for i in range(DAILY_SLOTS):
                    e[i] += v[i]

    src_view = []
    for s in sources:
        t = c = co = mt = mc = 0
        by_model_month = collections.defaultdict(lambda: {"tokens": 0, "calls": 0})
        for r in s["records"]:
            t += r["tokens"]; c += r["calls"]; co += r["cost"]
            monthly[r["month"]]["tokens"] += r["tokens"]
            monthly[r["month"]]["calls"] += r["calls"]
            monthly[r["month"]]["cost"] += r["cost"]
            if r["month"] == cur:
                mt += r["tokens"]; mc += r["calls"]
                month_models[name(r["model"])]["tokens"] += r["tokens"]
                month_models[name(r["model"])]["calls"] += r["calls"]
                by_model_month[name(r["model"])]["tokens"] += r["tokens"]
                by_model_month[name(r["model"])]["calls"] += r["calls"]
        sd = s.get("daily") or {}
        src_view.append({
            "label": s["label"], "note": s["note"],
            "total": t, "calls": c, "cost": co,
            "month_tokens": mt, "month_calls": mc,
            # 与顶层 days 对齐的日序列；前端按月切片后画各自的热力图
            "daily": [list(sd.get(d, [0] * DAILY_SLOTS)) for d in days],
            "models": sorted([{"name": k, **v} for k, v in by_model_month.items()],
                             key=lambda m: -m["tokens"])[:10],
        })

    heat = [{"day": d, "tokens": all_daily[d][0], "calls": all_daily[d][1]} for d in days]

    def bucket_sum(day_keys):
        """把若干天合并成 {tokens, calls, uncached, output, cache}。"""
        acc = [0] * DAILY_SLOTS
        for d in day_keys:
            for i in range(DAILY_SLOTS):
                acc[i] += all_daily[d][i]
        return {"tokens": acc[0], "calls": acc[1],
                "uncached": acc[2], "output": acc[3], "cache": acc[4]}

    def cumulative(hrs):
        out, run = [], 0.0
        for h in range(24):
            run += hrs[h]
            out.append(run)
        return out

    prev7 = day_list(CURVE_DAYS)[:-1]                 # 今天之前的 7 天
    avg7 = [0.0] * 24
    for d in prev7:
        cu = cumulative(all_hourly[d])
        for h in range(24):
            avg7[h] += cu[h] / len(prev7)

    return {
        "version": LOCAL_VERSION,
        "month": cur,
        "days": days,
        "daily": [list(all_daily[d]) for d in days],
        "months": sorted({d[:7] for d in days}, reverse=True),
        "heat": heat,
        "heat_days": HEAT_DAYS,
        "today": {"day": today, "tokens": all_daily[today][0], "calls": all_daily[today][1]},
        "stat_today": bucket_sum([today]),
        "stat_7d": bucket_sum(days[-7:]),
        "stat_90d": bucket_sum(days[-90:]),
        "today_curve": cumulative(all_hourly[today]),
        "avg7_curve": avg7,
        "day_models": {
            d: sorted([{"m": m, "s": src, "t": v[0], "c": v[1], "u": v[2], "o": v[3], "k": v[4]}
                       for (m, src), v in entries.items() if v[0] > 0],
                      key=lambda e: -e["t"])
            for d, entries in day_models.items() if d >= days[0]
        },
        "hours": {d: [round(x) for x in hrs] for d, hrs in all_hourly.items()
                  if d >= days[0] and any(hrs)},
        "month_tokens": sum(s["month_tokens"] for s in src_view),
        "month_calls": sum(s["month_calls"] for s in src_view),
        "total_tokens": sum(s["total"] for s in src_view),
        "total_cost": sum(s["cost"] for s in src_view),
        "month_models": sorted([{"name": k, **v} for k, v in month_models.items()],
                               key=lambda m: -m["tokens"]),
        "sources": sorted(src_view, key=lambda s: -s["month_tokens"]),
        "monthly": [{"month": k, **v} for k, v in sorted(monthly.items())],
        "errors": errors,
    }


def all_quota():
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        return list(ex.map(lambda kv: fetch_quota(*kv), PROVIDERS.items()))


# 私有磁盘缓存：采集结果在重启后继续可用，额度与本机用量独立计时。
USAGE_DIR = os.path.abspath(os.environ.get(
    "LINGQIAO_USAGE_DIR", str(platform_paths.app_support(Path.home()) / "LingqiaoUsage")))
CACHE_DIR = USAGE_DIR
CACHE_FILE = os.path.join(CACHE_DIR, "cache.json")


def provider_status():
    """Return provider labels and configured state without exposing credentials."""
    return [{"name": name, "label": spec["label"], "hint": spec["hint"],
             "configured": kc_configured(name)}
            for name, spec in PROVIDERS.items()]


def save_provider_keys(values):
    """Validate all submitted keys, then store through the native Keychain API.

    Only known providers and nonblank UTF-8 strings are accepted. Values never
    enter responses, caches, logs, or command-line arguments.
    """
    if not isinstance(values, dict):
        raise ValueError("密钥配置必须是对象")
    if not values or any(name not in PROVIDERS for name in values):
        raise ValueError("请选择有效的额度来源")
    prepared = {}
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 8192 or any(c in value for c in ("\x00", "\r", "\n")):
            raise ValueError("API Key 格式无效")
        prepared[name] = value.strip()
    ok, failed = [], []
    for name, value in prepared.items():
        try:
            good, unused = kc_set(name, value)
        except Exception:
            good = False
        if good:
            ok.append(PROVIDERS[name]["label"])
        else:
            failed.append({"label": PROVIDERS[name]["label"], "error": "保存失败"})
    return {"ok": ok, "fail": failed}


def atomic_private_json(path, payload):
    """Persist a snapshot with a unique private temp file; preserve old on failure."""
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".usage-", suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fresh(at, now, ttl):
    return isinstance(at, (int, float)) and not isinstance(at, bool) and 0 <= now - at < ttl


def snapshot(force=False, cache_file=None):
    """Refresh quotas every five minutes and local usage every thirty minutes.

    A day/month boundary forces local collection immediately, even with a fresh
    timestamp.  A failed write raises and leaves the previous cache in place.
    """
    if not isinstance(force, bool):
        raise ValueError("刷新标记必须为布尔值")
    cache_file = os.path.abspath(cache_file or CACHE_FILE)
    try:
        with open(cache_file, encoding="utf-8") as stream:
            c = json.load(stream)
    except (OSError, ValueError):
        c = {}
    if not isinstance(c, dict):
        c = {}

    now = time.time()
    quota, qat = c.get("quota"), c.get("quota_at", 0)
    local, lat = c.get("local"), c.get("local_at", 0)
    today = datetime.date.today().isoformat()
    local_valid = (isinstance(local, dict) and local.get("version") == LOCAL_VERSION
                   and isinstance(local.get("today"), dict)
                   and local["today"].get("day") == today
                   and local.get("month") == today[:7])
    updated = False
    if force or not isinstance(quota, list) or not _fresh(qat, now, QUOTA_TTL):
        quota, qat = all_quota(), now
        updated = True
    if force or not local_valid or not _fresh(lat, now, LOCAL_TTL):
        local, lat = collect_local(), now
        if local.get("errors"):
            raise RuntimeError("本地用量来源采集不完整，已保留上次快照")
        updated = True
    payload = {"at": min(qat, lat), "quota_at": qat, "local_at": lat,
               "quota": quota, "local": local}
    if updated:
        atomic_private_json(cache_file, payload)
    return payload
