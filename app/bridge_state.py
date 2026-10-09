"""Shared private state for the App and command line bridge.

Use one operation.lock per bridge repository for mutations. Lab databases have
independent state directories and cannot modify the real database's ledger.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_registry_guard = threading.Lock()
_locks = {}


def lock_fd(fd, blocking=True):
    """Exclusive lock on an open file; False when non-blocking and someone else holds it.

    macOS uses flock.  Windows has no flock, so lock the first byte with
    msvcrt (allowed past EOF) and poll, because LK_LOCK gives up after 10 s.
    """
    if os.name != "nt":
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        return True
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if not blocking:
                return False
            time.sleep(0.05)


def unlock_fd(fd):
    if os.name != "nt":
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class _LockState:
    def __init__(self):
        self.lock = threading.RLock()
        self.depth = 0
        self.fd = None


@contextmanager
def file_lock(path):
    """Thread and process lock, reentrant for the same resolved file path."""
    path = Path(path).expanduser().resolve()
    with _registry_guard:
        state = _locks.setdefault(str(path), _LockState())
    with state.lock:
        if state.depth == 0:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                lock_fd(fd)
            except BaseException:
                os.close(fd)
                raise
            state.fd = fd
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0:
                unlock_fd(state.fd)
                os.close(state.fd)
                state.fd = None


operation_lock = file_lock


def load_json(path, default=None):
    """Only a missing file is empty; corrupted bookkeeping must stop writes."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"Invalid bridge state: {path.name}") from exc


def atomic_json(path, obj):
    """Write private JSON, fsync and atomically replace without shared temp names."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        replace(temporary, path)
        os.chmod(path, 0o600)
        if os.name != "nt":  # Windows 打不开文件夹做 fsync；NTFS 的改名本身有日志
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def replace(source, target):
    """os.replace; on Windows retry briefly while another reader still has the target open."""
    for attempt in range(20 if os.name == "nt" else 1):
        try:
            return os.replace(source, target)
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.05)


def database_identity(db_path):
    path = Path(db_path).expanduser().resolve(strict=True)
    stat = path.stat()
    identity = {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino}
    # key 只用路径和 inode：device 编号 macOS 每次开机（或插拔外接盘）都可能重新分配，同一个文件也会变。
    identity["key"] = hashlib.sha256(json.dumps({"path": identity["path"], "inode": identity["inode"]},
                                                sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return identity


def same_database(stored, current):
    """记下的身份和现在的是不是同一个数据库文件：路径和 inode 都一样就是。

    不比 device：2026-10 实测，Mac 重启后 ZCode 数据库的 device 从 16777230 变成 16777232，
    路径和 inode 都没变，按 device 比就把同一个文件当成"换了数据库"，同步和回收站恢复都被拒。
    文件真被换掉（删了重建、从备份拷回来）时 inode 会变，照样拒绝。
    """
    return (isinstance(stored, dict) and isinstance(current, dict)
            and stored.get("path") == current.get("path")
            and stored.get("inode") is not None and stored.get("inode") == current.get("inode"))


def zcode_state_paths(db_path, real_db, bridge_dir):
    identity = database_identity(db_path)
    real_path = Path(real_db).expanduser().resolve()
    base = Path(bridge_dir)
    if Path(identity["path"]) != real_path:
        base = base / "labs" / identity["key"]
    return {"ledger": base / "ledger.json",
            "manifest": base / "zcode-injected-manifest.json",
            "provenance": base / "provenance.json",
            "lock": base / "operation.lock"}


def provenance_records(path):
    raw = load_json(path, {"version": 1, "targets": {}})
    if not isinstance(raw, dict) or not isinstance(raw.get("targets"), dict):
        raise ValueError("Invalid provenance state")
    return raw["targets"]


def record_provenance(path, target_tool, target, source_tool, source, *,
                      database=None, status="active", ledger_key=None):
    path = Path(path)
    with operation_lock(path.parent / "operation.lock"):
        targets = provenance_records(path)
        old = targets.get(str(target), {})
        targets[str(target)] = {
            **old, "target_tool": target_tool, "target": str(target),
            "source_tool": source_tool, "source": str(source),
            "status": status, "database": database, "ledger_key": ledger_key,
            "created_at": old.get("created_at", time.time()),
            "updated_at": time.time()}
        atomic_json(path, {"version": 1, "targets": targets})


def tombstone_provenance(path, target, *, status="source_deleted"):
    path = Path(path)
    with operation_lock(path.parent / "operation.lock"):
        targets = provenance_records(path)
        if str(target) not in targets:
            raise ValueError("Cannot tombstone an unknown target")
        targets[str(target)] = {**targets[str(target)], "status": status,
                                "updated_at": time.time()}
        atomic_json(path, {"version": 1, "targets": targets})


def load_zcode_manifest(path, db_path):
    identity = database_identity(db_path)
    raw = load_json(path, None)
    if raw is None:
        return {"version": 2, "database": identity, "sessions": []}
    if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), list):
        raise ValueError("Invalid ZCode manifest")
    stored = raw.get("database")
    if stored != identity and same_database(stored, identity):
        # 同一个文件，只是开机后 device 编号变了：换成现在的身份，下次写清单时一起存下。
        return {**raw, "database": identity}
    if stored != identity:
        # Never infer that an old 'real' manifest belongs to an arbitrary --db.
        if not raw["sessions"] and stored is None:
            return {"version": 2, "database": identity, "sessions": []}
        raise ValueError("ZCode manifest database identity mismatch; restore or migrate it explicitly")
    return raw


def record_zcode_injection(paths, db_path, key, sid, tool, source, title, mtime,
                           *, status="committed"):
    """Shared manifest/provenance format. Caller owns operation.lock and SQL tx."""
    with operation_lock(paths["lock"]):
        ledger = load_json(paths["ledger"], {})
        if not isinstance(ledger, dict):
            raise ValueError("Invalid ledger")
        manifest = load_zcode_manifest(paths["manifest"], db_path)
        entry = {"id": sid, "ledger_key": key, "title": title, "tool": tool,
                 "source": source, "time": int(mtime * 1000), "status": status}
        manifest["sessions"] = [e for e in manifest["sessions"]
                                 if e.get("id") != sid] + [entry]
        atomic_json(paths["manifest"], manifest)
        record_provenance(paths["provenance"], "zcode", sid, tool, source,
                          database=manifest["database"], status=status,
                          ledger_key=key)
        if status == "committed":
            ledger[key] = sid
            atomic_json(paths["ledger"], ledger)

