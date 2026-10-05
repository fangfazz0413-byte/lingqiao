"""Lingqiao usage API: validated private snapshots and native collection.

Earlier versions launched the separate ``mtoken`` CLI.  This module now calls
the built-in collector directly, so nothing outside Lingqiao is required; an
old Mtoken cache, if present, is migrated once on startup.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any
import tempfile
import math

import platform_paths
from usage_collector import provider_status, save_provider_keys, snapshot

_lock = threading.Lock()
_REQUIRED_LOCAL_LISTS = ("days", "daily", "months", "sources")


def _default_cache() -> Path:
    return Path(os.environ.get(
        "LINGQIAO_USAGE_CACHE",
        platform_paths.app_support(Path.home()) / "LingqiaoUsage/cache.json",
    )).expanduser().resolve()


def _validate(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("用量快照必须是对象")
    local = data.get("local")
    if not isinstance(local, dict) or not isinstance(data.get("quota"), list):
        raise ValueError("用量快照结构不兼容：缺少 local/quota")
    for field in _REQUIRED_LOCAL_LISTS:
        if not isinstance(local.get(field), list):
            raise ValueError("用量快照结构不兼容：local." + field)
    stamp = data.get("at")
    if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
            or not math.isfinite(stamp) or stamp <= 0
            or not local.get("month") or not isinstance(local.get("today"), dict)):
        raise ValueError("用量快照缺少有效时间、月份或今日数据")
    return data


def read_snapshot(cache: str | os.PathLike[str] | None = None) -> dict:
    """Read and validate the private Lingqiao cache without exposing secrets."""
    path = Path(cache).expanduser() if cache is not None else _default_cache()
    if not path.is_file():
        raise FileNotFoundError("未找到灵桥用量缓存；请点击刷新完成首次采集。")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("用量缓存不是有效 JSON") from exc
    checked = _validate(data)
    # mtime is metadata only; UI uses the collector's recorded at timestamp.
    checked["_mtime"] = path.stat().st_mtime
    checked["_refresh_dependency"] = "Lingqiao 内置采集器"
    return checked


def migrate_legacy_cache(legacy: str | os.PathLike[str], target: str | os.PathLike[str] | None = None) -> dict:
    """Copy one validated legacy snapshot into LingqiaoUsage without deleting it.

    This is an explicit one-time migration hook. It copies bytes only after
    schema validation, writes with private permissions and atomic replacement,
    and returns metadata rather than cache contents.
    """
    source = Path(legacy).expanduser().resolve()
    destination = Path(target).expanduser() if target is not None else _default_cache()
    if not source.is_file():
        return {"migrated": False, "reason": "legacy_missing", "target": str(destination)}
    # Read once so validation and the bytes copied cannot diverge in a race.
    raw = source.read_bytes()
    _validate(json.loads(raw))
    if destination.is_file():
        try:
            _validate(json.loads(destination.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise ValueError("新用量缓存无效，已保留现有文件，迁移未覆盖") from exc
        destination.chmod(0o600)
        return {"migrated": False, "reason": "target_exists", "target": str(destination)}
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".usage-migration-", suffix=".tmp", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if fd is not None:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)
    return {"migrated": True, "target": str(destination), "bytes": len(raw)}


def refresh_snapshot(cache: str | os.PathLike[str] | None = None, *, force: bool = True) -> dict:
    """Call the embedded collector directly; return a validated snapshot.

    Failure messages contain only error classes, so third-party diagnostic
    material or mock credentials cannot be returned by the HTTP layer.
    """
    if not isinstance(force, bool):
        raise ValueError("刷新标记必须为布尔值")
    path = Path(cache).expanduser() if cache is not None else _default_cache()
    if not _lock.acquire(blocking=False):
        raise RuntimeError("用量正在刷新，请稍后重试。")
    try:
        try:
            return _validate(snapshot(force=force, cache_file=str(path)))
        except Exception as exc:
            raise RuntimeError("用量采集或缓存保存失败（%s），已保留上次快照。" % type(exc).__name__) from exc
    finally:
        _lock.release()


def get_provider_status() -> list[dict]:
    """List provider configuration state without returning key material."""
    return provider_status()


def set_provider_keys(values: dict) -> dict:
    """Save quota-provider keys through the macOS keychain adapter."""
    with _lock:
        result = save_provider_keys(values)
    # The UI immediately forces collection after a successful key update.
    return result
