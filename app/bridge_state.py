"""Shared private state for the App and command line bridge.

Use one operation.lock per bridge repository for mutations. Lab databases have
independent state directories and cannot modify the real database's ledger.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

_registry_guard = threading.Lock()
_locks = {}


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
                fcntl.flock(fd, fcntl.LOCK_EX)
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
                fcntl.flock(state.fd, fcntl.LOCK_UN)
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
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def database_identity(db_path):
    path = Path(db_path).expanduser().resolve(strict=True)
    stat = path.stat()
    identity = {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino}
    identity["key"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return identity


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

