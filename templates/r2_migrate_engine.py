"""Resumable SQLite WAL migration engine for Cloudflare R2 -> Google Drive."""
from __future__ import annotations

from collections import OrderedDict, deque
import concurrent.futures
import contextlib
import dataclasses
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tarfile
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

from r2_s3 import (
    DEFAULT_SAFE_FREE_CLASS_A,
    DEFAULT_SAFE_FREE_CLASS_B,
    DeleteBatchResult,
    R2Client,
    R2Credentials,
    R2Error,
    R2HttpError,
    R2UsageLedger,
    map_object_to_drive_relpath,
    parse_public_urls,
    resolve_drive_output_root,
    sanitize_segment,
)

COST_MODES = ("free-first", "free-only", "unlimited")
STORAGE_MODES = ("files", "tar")
TERMINAL_JOB_STATUSES = {
    "completed",
    "cancelled",
    "failed",
    "paused",
    "paused_budget",
    "paused_quota",
    "paused_credentials",
    "interrupted",
}
RESUMABLE_JOB_STATUSES = {
    "queued",
    "running",
    "paused",
    "paused_budget",
    "paused_quota",
    "paused_credentials",
    "interrupted",
    "failed",
}
DEFAULT_WORKERS = 48
DEFAULT_WRITER_WORKERS = 16
DEFAULT_TAR_SHARD_MB = 512
DEFAULT_SPOOL_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_CHECKPOINT_INTERVAL = 300.0
TAR_DEFAULT_CHECKPOINT_INTERVAL = 900.0
_MIN_DRIVEFS_WORK_FREE_BYTES = 6 * 1024 * 1024 * 1024
DEFAULT_PAGE_SIZE = 1000
MAX_LISTED_BUFFER = 250_000
_LARGE_FILE_FSYNC_BYTES = 16 * 1024 * 1024
_PREFETCH_MEM_MAX_FILE = 8 * 1024 * 1024
_PREFETCH_BUDGET_BYTES = 256 * 1024 * 1024
_DIR_ENTRY_CAP = 250_000


def _atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    if mode is None and path.is_file():
        with path.open("r+b") as fh:
            fh.seek(0)
            fh.write(raw)
            fh.truncate()
            fh.flush()
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(raw)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(path, mode)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


class ByteBudgetSemaphore:
    """Bounded byte-budget allocator for NVMe spool staging that never deadlocks on oversized files."""

    def __init__(self, limit_bytes: int) -> None:
        self.limit = max(64 * 1024 * 1024, int(limit_bytes))
        self._used = 0
        self._cond = threading.Condition()

    def acquire(self, size_bytes: int, stop_event: threading.Event | None = None) -> int:
        need = min(self.limit, max(1, int(size_bytes)))
        with self._cond:
            while self._used + need > self.limit:
                if stop_event is not None and stop_event.is_set():
                    raise _JobStopped("stopped")
                self._cond.wait(timeout=0.5)
            self._used += need
            return need

    def release(self, amount: int) -> None:
        with self._cond:
            self._used = max(0, self._used - max(0, int(amount)))
            self._cond.notify_all()


class _JobStopped(Exception):
    pass


class _BudgetPaused(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _QuotaPaused(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _detect_fuse_drive_mydrive(path: Path) -> Path | None:
    """Return resolved `<mount>/MyDrive` if `path` resides on a `fuse.drive` mount, else None."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    try:
        mounts_text = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt_point = parts[1].replace("\\040", " ")
        fstype = parts[2]
        if fstype != "fuse.drive":
            continue
        for candidate_name in ("MyDrive", "My Drive"):
            mydrive = Path(mnt_point) / candidate_name
            try:
                mydrive_res = mydrive.resolve()
            except OSError:
                mydrive_res = mydrive
            if resolved == mydrive_res or resolved.is_relative_to(mydrive_res):
                return mydrive_res
    return None


def _gdrive_escape_q(val: str) -> str:
    return val.replace("\\", "\\\\").replace("'", "\\'")


class _DriveCloudSyncVerifier:
    """Verifies whether files on a `fuse.drive` mount have completed upload to Google Drive Cloud."""

    _TOKEN_TTL_SKEW = 60.0
    _LIST_CACHE_TTL = 30.0
    _FILE_CACHE_TTL = 30.0

    def __init__(self, dest_root: Path, log: Callable[[str], None] | None = None) -> None:
        self.dest_root = Path(dest_root)
        self._log = log or (lambda _msg: None)
        self._mydrive_root = _detect_fuse_drive_mydrive(self.dest_root)
        self._lock = threading.Lock()
        self._access_token: str = ""
        self._token_expires_mono: float = 0.0
        self._folder_ids: dict[str, str] = {"": "root"}
        self._dir_cache: dict[str, tuple[float, dict[str, list[tuple[int, bool]]]]] = {}
        self._file_cache: dict[tuple[Path, int], tuple[float, str]] = {}

    @property
    def is_fuse_drive(self) -> bool:
        return self._mydrive_root is not None

    def invalidate_cache(self) -> None:
        with self._lock:
            self._dir_cache.clear()
            self._file_cache.clear()

    def _get_access_token_locked(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._token_expires_mono:
            return self._access_token
        candidates = ["127.0.0.1:8012"]
        for env_key in ("TBE_EPHEM_CREDS_ADDR", "TBE_CREDS_ADDR"):
            addr = (os.environ.get(env_key) or "").strip()
            if addr and addr not in candidates:
                candidates.append(addr)
        last_err: Exception | None = None
        for host_port in candidates:
            url = f"http://{host_port}/computeMetadata/v1/instance/service-accounts/default/token"
            req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
            try:
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                tok = str(data.get("access_token") or "")
                if not tok:
                    raise RuntimeError("empty access_token")
                expires_in = max(120, int(data.get("expires_in") or 3599))
                self._access_token = tok
                self._token_expires_mono = time.monotonic() + max(30.0, expires_in - self._TOKEN_TTL_SKEW)
                return tok
            except Exception as err:
                last_err = err
        raise RuntimeError(f"Drive token shim unreachable: {last_err}")

    def _gdrive_list(self, query: str, fields: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            with self._lock:
                tok = self._get_access_token_locked()
            params = {
                "q": query,
                "fields": f"nextPageToken,{fields}",
                "pageSize": "1000",
            }
            if page_token:
                params["pageToken"] = page_token
            url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
            try:
                with urllib.request.urlopen(req, timeout=20.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                if err.code == 401:
                    with self._lock:
                        self._access_token = ""
                        self._token_expires_mono = 0.0
                        tok = self._get_access_token_locked()
                    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
                    with urllib.request.urlopen(req, timeout=20.0) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                else:
                    raise
            items.extend(data.get("files") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items

    def _resolve_folder_id(self, parent_parts: tuple[str, ...]) -> str | None:
        cur_rel = ""
        cur_id = "root"
        for seg in parent_parts:
            nxt_rel = f"{cur_rel}/{seg}" if cur_rel else seg
            with self._lock:
                cached = self._folder_ids.get(nxt_rel)
            if cached is not None:
                cur_id = cached
                cur_rel = nxt_rel
                continue
            q = (
                f"'{_gdrive_escape_q(cur_id)}' in parents and trashed = false "
                "and mimeType = 'application/vnd.google-apps.folder' "
                f"and name = '{_gdrive_escape_q(seg)}'"
            )
            matches = self._gdrive_list(q, "files(id,name)")
            if not matches:
                return None
            fid = str(matches[0]["id"])
            with self._lock:
                self._folder_ids[nxt_rel] = fid
            cur_id = fid
            cur_rel = nxt_rel
        return cur_id

    def _cloud_dir_listing(self, parent_parts: tuple[str, ...]) -> dict[str, list[tuple[int, bool]]] | None:
        rel_dir = "/".join(parent_parts)
        now = time.monotonic()
        with self._lock:
            cached = self._dir_cache.get(rel_dir)
            if cached is not None and (now - cached[0]) < self._LIST_CACHE_TTL:
                return cached[1]
        folder_id = self._resolve_folder_id(parent_parts)
        if folder_id is None:
            return None
        q = f"'{_gdrive_escape_q(folder_id)}' in parents and trashed = false"
        files = self._gdrive_list(q, "files(name,size,md5Checksum)")
        listing: dict[str, list[tuple[int, bool]]] = {}
        for item in files:
            fname = str(item.get("name") or "")
            if not fname:
                continue
            try:
                sz = int(item.get("size") or 0)
            except (TypeError, ValueError):
                sz = 0
            has_md5 = bool(item.get("md5Checksum"))
            listing.setdefault(fname, []).append((sz, has_md5))
        with self._lock:
            self._dir_cache[rel_dir] = (time.monotonic(), listing)
        return listing

    def _fallback_check(self, full_path: Path, expected_size: int) -> str:
        try:
            raw = os.getxattr(full_path, "user.drive.md5")  # type: ignore[attr-defined]
            val = raw.decode("utf-8", errors="ignore").strip()
            if val or expected_size == 0:
                return "synced"
            return "pending_sync"
        except OSError as err:
            if err.errno == getattr(errno, "ENODATA", 61):
                return "pending_sync"
            if err.errno not in (errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP), errno.ENOSYS):
                return "pending_sync"
        except AttributeError:
            pass
        with contextlib.suppress(OSError):
            for log_p in Path("/tmp").glob("drive*.ERROR*"):
                try:
                    sz = log_p.stat().st_size
                    with log_p.open("rb") as lf:
                        if sz > 512 * 1024:
                            lf.seek(sz - 512 * 1024)
                        tail = lf.read().decode("utf-8", errors="replace")
                    if "userRateLimitExceeded" in tail:
                        return "pending_sync"
                except OSError:
                    continue
        return "synced"

    def verify_file(self, full_path: Path, expected_size: int) -> str:
        """Return `'synced'`, `'pending_sync'`, or `'missing'`."""
        expected = int(expected_size)
        try:
            if not full_path.is_file() or full_path.stat().st_size != expected:
                return "missing"
        except OSError:
            return "missing"

        if self._mydrive_root is None:
            return "synced"

        now = time.monotonic()
        key = (full_path, expected)
        with self._lock:
            cached = self._file_cache.get(key)
            if cached is not None and (now - cached[0]) < self._FILE_CACHE_TTL:
                return cached[1]

        try:
            rel = full_path.resolve().relative_to(self._mydrive_root)
            parent_parts = rel.parts[:-1]
            filename = rel.name
            listing = self._cloud_dir_listing(parent_parts)
            if listing is None:
                res = "pending_sync"
            else:
                entries = listing.get(filename) or []
                if any(sz == expected and (expected == 0 or has_md5) for sz, has_md5 in entries):
                    res = "synced"
                else:
                    res = "pending_sync"
        except Exception as err:
            self._log(f"Drive Cloud verify fallback for {full_path.name}: {err}")
            res = self._fallback_check(full_path, expected)

        with self._lock:
            self._file_cache[key] = (time.monotonic(), res)
        return res


class DriveDirCache:
    """Thread-safe bounded directory & readdir cache with S3 file-vs-directory POSIX collision repair."""

    def __init__(
        self,
        dest_root: Path,
        *,
        max_entries: int = 50_000,
        max_dir_entries: int = 8_000,
    ) -> None:
        self.dest_root = Path(dest_root)
        self.max_entries = max(1000, int(max_entries))
        self.max_dir_entries = max(256, int(max_dir_entries))
        self.lock = threading.Lock()
        self._write_cond = threading.Condition(self.lock)
        self._active_writes: dict[str, int] = {}
        self._known: OrderedDict[str, None] = OrderedDict()
        # rel_dir -> {entry_name: -1 (dir) | None (file, size unknown) | int >= 0 (file size)}
        self._dir_entries: OrderedDict[str, dict[str, int | None]] = OrderedDict()
        self._dir_total_items = 0
        self._scanning: set[str] = set()

    def _remember_locked(self, rel_dir: str) -> None:
        if rel_dir in self._known:
            self._known.move_to_end(rel_dir)
            return
        self._known[rel_dir] = None
        if len(self._known) > self.max_entries:
            self._known.popitem(last=False)

    def _invalidate_locked(self, rel_dir: str) -> None:
        self._known.pop(rel_dir, None)
        old = self._dir_entries.pop(rel_dir, None)
        if old is not None:
            self._dir_total_items = max(0, self._dir_total_items - len(old))

    def _set_dir_entries_locked(self, rel_dir: str, mapping: dict[str, int | None]) -> None:
        prev = self._dir_entries.pop(rel_dir, None)
        if prev is not None:
            self._dir_total_items = max(0, self._dir_total_items - len(prev))
        self._dir_entries[rel_dir] = mapping
        self._dir_total_items += len(mapping)
        while (len(self._dir_entries) > self.max_dir_entries or self._dir_total_items > _DIR_ENTRY_CAP) and len(self._dir_entries) > 1:
            _, evicted = self._dir_entries.popitem(last=False)
            self._dir_total_items = max(0, self._dir_total_items - len(evicted))

    def _record_entry_locked(self, parent_rel: str, name: str, val: int | None) -> None:
        entries = self._dir_entries.get(parent_rel)
        if entries is not None:
            if name not in entries:
                self._dir_total_items += 1
            entries[name] = val
            self._dir_entries.move_to_end(parent_rel)

    def ensure_dir_locked(
        self,
        rel_dir: str,
        on_relocate: Callable[[str, str], None] | None = None,
    ) -> Path:
        """Create directory `dest_root / rel_dir`, relocating any colliding file into `dir/__r2_object__`."""
        clean = rel_dir.strip("/")
        if "" not in self._known:
            self.dest_root.mkdir(parents=True, exist_ok=True)
            self._remember_locked("")
        if not clean:
            return self.dest_root
        if clean in self._known:
            self._known.move_to_end(clean)
            return self.dest_root / clean

        cur = self.dest_root
        parts = [p for p in clean.split("/") if p]
        built: list[str] = []
        parent_rel = ""
        for seg in parts:
            built.append(seg)
            cur_rel = "/".join(built)
            cur = cur / seg
            while self._active_writes.get(cur_rel, 0) > 0:
                self._write_cond.wait()
            if cur_rel in self._known:
                parent_rel = cur_rel
                continue
            try:
                os.mkdir(cur)
                # Newly created folder did not exist on disk -> known empty (0 scandir/stat calls later).
                self._set_dir_entries_locked(cur_rel, {})
                self._record_entry_locked(parent_rel, seg, -1)
            except FileExistsError:
                if not cur.is_dir():
                    # Regular file already exists where a directory is needed (S3 key `a/b` + `a/b/c`).
                    tmp_reloc = cur.with_name(f".{cur.name}.ctreloc.{uuid.uuid4().hex[:8]}")
                    os.replace(cur, tmp_reloc)
                    cur.mkdir(parents=True, exist_ok=True)
                    target_inside = cur / "__r2_object__"
                    os.replace(tmp_reloc, target_inside)
                    self._invalidate_locked(parent_rel)
                    reloc_sz: int | None = None
                    with contextlib.suppress(OSError):
                        reloc_sz = target_inside.stat().st_size
                    self._set_dir_entries_locked(cur_rel, {"__r2_object__": reloc_sz})
                    if on_relocate is not None:
                        on_relocate(cur_rel, f"{cur_rel}/__r2_object__")
                else:
                    self._record_entry_locked(parent_rel, seg, -1)
            self._remember_locked(cur_rel)
            parent_rel = cur_rel
        return cur

    def ensure_parent_dir(
        self,
        rel_path: str,
        on_relocate: Callable[[str, str], None] | None = None,
    ) -> str:
        """Ensure parent directory exists under short `self.lock`; returns stripped `rel_path`."""
        actual_rel = rel_path.strip("/")
        parent_rel, _, _ = actual_rel.rpartition("/")
        with self.lock:
            self.ensure_dir_locked(parent_rel, on_relocate=on_relocate)
        return actual_rel

    def _scan_dir_if_needed(self, parent_rel: str) -> dict[str, int | None] | None:
        with self.lock:
            entries = self._dir_entries.get(parent_rel)
            if entries is not None:
                self._dir_entries.move_to_end(parent_rel)
                return entries
        target_dir = (self.dest_root / parent_rel) if parent_rel else self.dest_root
        scanned: dict[str, int | None] = {}
        try:
            with os.scandir(target_dir) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith(".") and (".ctpart." in name or ".ctreloc." in name):
                        continue
                    try:
                        is_d = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_d = False
                    scanned[name] = -1 if is_d else None
        except OSError:
            return None
        with self.lock:
            existing = self._dir_entries.get(parent_rel)
            if existing is not None:
                self._dir_entries.move_to_end(parent_rel)
                return existing
            self._remember_locked(parent_rel)
            self._set_dir_entries_locked(parent_rel, scanned)
            return scanned

    def preexisting_file_size(self, rel_path: str) -> tuple[str, int | None]:
        """Fast Drive existence check via bounded readdir cache; handles file-vs-dir collisions."""
        clean = rel_path.strip("/")
        for _ in range(2):
            parent_rel, _, name = clean.rpartition("/")
            with self.lock:
                if clean in self._known:
                    clean = f"{clean}/__r2_object__"
                    continue
                cached = self._dir_entries.get(parent_rel)
                if cached is not None:
                    self._dir_entries.move_to_end(parent_rel)
                    val = cached.get(name, -2)
                    if val == -2:
                        return clean, None
                    if val == -1:
                        clean = f"{clean}/__r2_object__"
                        continue
                    if isinstance(val, int) and val >= 0:
                        return clean, val

            scanned = self._scan_dir_if_needed(parent_rel)
            if scanned is None:
                return clean, None
            val = scanned.get(name, -2)
            if val == -2:
                return clean, None
            if val == -1:
                clean = f"{clean}/__r2_object__"
                continue
            if isinstance(val, int) and val >= 0:
                return clean, val
            # File exists in directory listing with unqueried size -> single stat() outside lock.
            try:
                sz = (self.dest_root / clean).stat().st_size
            except OSError:
                return clean, None
            with self.lock:
                self._record_entry_locked(parent_rel, name, int(sz))
            return clean, int(sz)
        return clean, None

    def write_file_direct(
        self,
        rel_path: str,
        src_path: Path | None,
        expected_size: int,
        on_relocate: Callable[[str, str], None] | None = None,
    ) -> str:
        """Write file directly to DriveFS without holding `self.lock` across FUSE I/O."""
        actual_rel = rel_path.strip("/")
        for attempt in range(4):
            with self.lock:
                while True:
                    parent_rel, _, name = actual_rel.rpartition("/")
                    self.ensure_dir_locked(parent_rel, on_relocate=on_relocate)
                    cached_parent = self._dir_entries.get(parent_rel)
                    known_dir = actual_rel in self._known or (
                        cached_parent is not None and cached_parent.get(name) == -1
                    )
                    if known_dir and not actual_rel.endswith("/__r2_object__"):
                        actual_rel = f"{actual_rel}/__r2_object__"
                        continue
                    break
                tracked_rel = actual_rel
                self._active_writes[tracked_rel] = self._active_writes.get(tracked_rel, 0) + 1

            dest = self.dest_root / tracked_rel
            try:
                with dest.open("wb") as dst_f:
                    if expected_size > 0 and src_path is not None:
                        with src_path.open("rb") as src_f:
                            shutil.copyfileobj(src_f, dst_f, length=1024 * 1024)
                    if expected_size >= _LARGE_FILE_FSYNC_BYTES:
                        dst_f.flush()
                        with contextlib.suppress(OSError):
                            os.fsync(dst_f.fileno())
                st_res = dest.stat()
                if stat.S_ISDIR(st_res.st_mode):
                    raise IsADirectoryError(errno.EISDIR, "Directory created concurrently", str(dest))
                actual_size = st_res.st_size
                if actual_size != int(expected_size):
                    with contextlib.suppress(OSError):
                        dest.unlink(missing_ok=True)
                    raise OSError(f"Drive verify failed for {dest}: expected {expected_size}B, got {actual_size}B")
                with self.lock:
                    self._record_entry_locked(parent_rel, name, int(expected_size))
                return tracked_rel
            except (IsADirectoryError, FileExistsError):
                if not tracked_rel.endswith("/__r2_object__") and dest.is_dir():
                    actual_rel = f"{tracked_rel}/__r2_object__"
                    with self.lock:
                        self._remember_locked(tracked_rel)
                        self._record_entry_locked(parent_rel, name, -1)
                    continue
                raise
            except OSError as exc:
                if not tracked_rel.endswith("/__r2_object__") and dest.is_dir():
                    actual_rel = f"{tracked_rel}/__r2_object__"
                    with self.lock:
                        self._remember_locked(tracked_rel)
                        self._record_entry_locked(parent_rel, name, -1)
                    continue
                if attempt < 3 and exc.errno in (errno.ENOENT, errno.ENOTDIR):
                    with self.lock:
                        self._invalidate_locked(parent_rel)
                    continue
                with contextlib.suppress(OSError):
                    if dest.is_file():
                        dest.unlink(missing_ok=True)
                raise
            finally:
                with self.lock:
                    rem = self._active_writes.get(tracked_rel, 1) - 1
                    if rem <= 0:
                        self._active_writes.pop(tracked_rel, None)
                    else:
                        self._active_writes[tracked_rel] = rem
                    self._write_cond.notify_all()
        raise OSError(f"Drive write failed for {rel_path}")


def _open_state_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS buckets (
            bucket TEXT PRIMARY KEY,
            last_key TEXT NOT NULL DEFAULT '',
            listing_done INTEGER NOT NULL DEFAULT 0,
            objects_listed INTEGER NOT NULL DEFAULT 0,
            bytes_listed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS objects (
            bucket TEXT NOT NULL,
            key TEXT NOT NULL,
            size INTEGER NOT NULL,
            etag TEXT NOT NULL,
            storage_class TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            is_dir INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            PRIMARY KEY (bucket, key)
        );
        CREATE INDEX IF NOT EXISTS idx_objects_status ON objects(status, bucket);
        CREATE INDEX IF NOT EXISTS idx_objects_status_bk ON objects(status, bucket, key);
        CREATE INDEX IF NOT EXISTS idx_objects_rel ON objects(rel_path);
        CREATE TABLE IF NOT EXISTS tar_shards (
            shard_id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket TEXT NOT NULL,
            shard_name TEXT NOT NULL UNIQUE,
            rel_tar_path TEXT NOT NULL,
            file_count INTEGER NOT NULL,
            payload_bytes INTEGER NOT NULL,
            tar_bytes INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archived_files (
            rel_path TEXT PRIMARY KEY,
            bucket TEXT NOT NULL DEFAULT '',
            key TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL,
            shard_name TEXT NOT NULL,
            source TEXT NOT NULL,
            loose_cleaned INTEGER NOT NULL DEFAULT 0,
            archived_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_archived_shard ON archived_files(shard_name);
        CREATE INDEX IF NOT EXISTS idx_archived_uncleaned ON archived_files(loose_cleaned) WHERE loose_cleaned = 0;
        """
    )
    conn.commit()
    return conn


def _backup_sqlite_to_drive(
    src_conn: sqlite3.Connection,
    local_backup_tmp: Path,
    drive_checkpoint: Path,
    *,
    db_path: Path | None = None,
) -> None:
    """Snapshot local NVMe SQLite WAL DB -> local file (or direct WAL checkpoint) -> in-place/atomic sync on Drive."""
    drive_checkpoint.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        for stale in drive_checkpoint.parent.iterdir():
            if stale.is_file() and stale.name.startswith(f".{drive_checkpoint.name}.") and stale.name.endswith(".tmp"):
                with contextlib.suppress(OSError):
                    stale.unlink(missing_ok=True)

    local_backup_tmp.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        local_backup_tmp.unlink(missing_ok=True)

    use_direct_db = False
    if db_path is not None and db_path.is_file():
        with contextlib.suppress(OSError):
            db_sz = db_path.stat().st_size
            free_sz = shutil.disk_usage(local_backup_tmp.parent).free
            if free_sz < db_sz + 256 * 1024 * 1024:
                use_direct_db = True

    src_file: Path
    if use_direct_db and db_path is not None:
        src_conn.commit()
        src_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        src_file = db_path
    else:
        dst = sqlite3.connect(str(local_backup_tmp))
        try:
            src_conn.backup(dst)
            dst.commit()
        finally:
            dst.close()
        src_file = local_backup_tmp

    drive_tmp: Path | None = None
    try:
        if drive_checkpoint.is_file():
            with src_file.open("rb") as src_f, drive_checkpoint.open("r+b") as dst_f:
                dst_f.seek(0)
                shutil.copyfileobj(src_f, dst_f, length=4 * 1024 * 1024)
                dst_f.truncate()
                dst_f.flush()
                with contextlib.suppress(OSError):
                    os.fsync(dst_f.fileno())
        else:
            drive_tmp = drive_checkpoint.with_name(f".{drive_checkpoint.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            with src_file.open("rb") as src_f, drive_tmp.open("wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f, length=4 * 1024 * 1024)
                dst_f.flush()
                with contextlib.suppress(OSError):
                    os.fsync(dst_f.fileno())
            os.replace(drive_tmp, drive_checkpoint)
            drive_tmp = None
    finally:
        if drive_tmp is not None:
            with contextlib.suppress(OSError):
                drive_tmp.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            local_backup_tmp.unlink(missing_ok=True)


def _restore_sqlite_from_drive(drive_checkpoint: Path, local_db: Path) -> bool:
    if not drive_checkpoint.is_file():
        return False
    local_db.parent.mkdir(parents=True, exist_ok=True)
    tmp = local_db.with_name(f".{local_db.name}.restore.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with drive_checkpoint.open("rb") as src_f, tmp.open("wb") as dst_f:
            shutil.copyfileobj(src_f, dst_f, length=1024 * 1024)
            dst_f.flush()
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(str(local_db) + suffix).unlink(missing_ok=True)
        os.replace(tmp, local_db)
        return True
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


class CredentialStore:
    """Handles R2 credential resolution across request overrides, env vars, and 0600 Drive storage."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.drive_creds_path = self.data_dir / "r2-credentials.json"
        self.legacy_shared_path = Path("/content/drive/MyDrive/colab-llm/r2_credentials.json")
        self._lock = threading.Lock()
        self._memory: R2Credentials | None = None

    def set_credentials(
        self,
        creds: R2Credentials,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        creds.validate()
        with self._lock:
            self._memory = creds
            if persist:
                _atomic_write_json(self.drive_creds_path, creds.to_dict(), mode=0o600)
                return creds.redacted_summary(source="drive")
            return creds.redacted_summary(source="memory")

    def clear(self) -> None:
        with self._lock:
            self._memory = None
            with contextlib.suppress(OSError):
                self.drive_creds_path.unlink(missing_ok=True)

    def resolve(self, override: dict[str, Any] | R2Credentials | None = None) -> tuple[R2Credentials | None, str]:
        if isinstance(override, R2Credentials):
            override.validate()
            return override, "request"
        if isinstance(override, dict) and any(
            str(override.get(k) or "").strip()
            for k in ("access_key_id", "secret_access_key", "account_id", "endpoint_url")
        ):
            creds = R2Credentials.from_dict(override)
            # Fill partial override from saved/env defaults if user supplied only secret or endpoint.
            fallback, _ = self.resolve(None)
            if fallback is not None:
                if not creds.account_id and not creds.endpoint_url:
                    creds.account_id = fallback.account_id
                    creds.endpoint_url = fallback.endpoint_url
                    creds.jurisdiction = creds.jurisdiction or fallback.jurisdiction
                if not creds.access_key_id:
                    creds.access_key_id = fallback.access_key_id
                if not creds.secret_access_key:
                    creds.secret_access_key = fallback.secret_access_key
            creds.validate()
            return creds, "request"

        with self._lock:
            if self._memory is not None:
                return self._memory, "memory"

        env_ak = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
        env_sk = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
        env_ep = os.environ.get("R2_ENDPOINT_URL", "").strip()
        env_acct = os.environ.get("R2_ACCOUNT_ID", "").strip()
        env_jur = os.environ.get("R2_JURISDICTION", "").strip()
        if env_ak and env_sk and (env_ep or env_acct):
            creds = R2Credentials(
                access_key_id=env_ak,
                secret_access_key=env_sk,
                account_id=env_acct,
                endpoint_url=env_ep,
                jurisdiction=env_jur,
            )
            creds.validate()
            return creds, "env"

        for src_name, path in (("drive", self.drive_creds_path), ("shared_drive", self.legacy_shared_path)):
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    creds = R2Credentials.from_dict(loaded)
                    creds.validate()
                    return creds, src_name
                except (OSError, ValueError):
                    continue
        return None, "none"

    def summary(self) -> dict[str, Any]:
        creds, source = self.resolve(None)
        if creds is None:
            return {"configured": False, "source": "none"}
        return creds.redacted_summary(source=source)


@dataclasses.dataclass
class _RuntimeJob:
    job_id: str
    config: dict[str, Any]
    db_path: Path
    drive_job_dir: Path
    spool_dir: Path
    dest_root: Path
    status: str = "queued"
    message: str = ""
    paid_fallback_used: bool = False
    created_at: float = dataclasses.field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    session_started_at: float | None = None
    session_finished_at: float | None = None
    updated_at: float = dataclasses.field(default_factory=time.time)
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    stop_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    pause_requested: bool = False
    cancel_requested: bool = False
    worker_thread: threading.Thread | None = None
    db_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    conn: sqlite3.Connection | None = None
    recent_errors: deque[dict[str, str]] = dataclasses.field(default_factory=lambda: deque(maxlen=50))
    status_counts: dict[str, dict[str, int]] = dataclasses.field(default_factory=dict)
    bytes_done_session: int = 0
    files_done_session: int = 0
    tar_shards_count: int = 0
    tar_bytes_total: int = 0
    consolidated_files: int = 0
    consolidated_bytes: int = 0
    last_db_refresh_mono: float = 0.0
    buckets_snapshot: list[dict[str, Any]] = dataclasses.field(default_factory=list)


_SHARD_SEQ_RE = re.compile(r"-part-(\d+)\.tar$")


def _is_temp_artifact_name(name: str) -> bool:
    """Return True only for r2-migrate internal staging/temp files, never user dotfiles."""
    return name.startswith(".") and (".ctpart." in name or ".ctreloc." in name)


@dataclasses.dataclass
class _ArchiveTask:
    bucket: str
    key: str
    rel_path: str
    size: int
    is_dir: bool
    source: str  # "r2_download" | "drive_existing"
    shard_bucket: str = ""
    payload_bytes: bytes | None = None
    source_path: Path | None = None
    unlink_source_after_pack: bool = False
    held_budget: int = 0
    release_fn: Callable[[int], None] | None = None
    loose_path: Path | None = None


@dataclasses.dataclass
class _ArchivedEntry:
    bucket: str
    key: str
    orig_rel_path: str
    archive_path: str
    size: int
    is_dir: bool
    source: str
    loose_path: Path | None = None


@dataclasses.dataclass
class _StagingShard:
    bucket: str
    safe_bucket: str
    shard_name: str
    rel_tar_path: str
    stage_tar_path: Path
    raw_fh: io.BufferedWriter
    tar: tarfile.TarFile
    entries: list[_ArchivedEntry] = dataclasses.field(default_factory=list)
    seen_paths: set[str] = dataclasses.field(default_factory=set)
    payload_bytes: int = 0
    file_count: int = 0
    created_mono: float = dataclasses.field(default_factory=time.monotonic)


@dataclasses.dataclass
class _FlushOpenMarker:
    wait_event: threading.Event


@dataclasses.dataclass
class _StopPackerMarker:
    pass


class _TarArchivePipeline:
    """Packs R2 downloads & existing Drive files into uncompressed PAX tar shards on NVMe,
    atomically flushes them to Drive with manifests, commits SQLite, then cleans loose files."""

    def __init__(
        self,
        *,
        job: _RuntimeJob,
        conn: sqlite3.Connection,
        tar_shard_mb: float,
        on_committed: Callable[
            [list[_ArchivedEntry], int, dict[str, tuple[int, int]], int, int, int, int], None
        ],
        on_failed: Callable[[list[_ArchivedEntry], str], None],
        on_quota_pause: Callable[[str], None] | None = None,
        log: Callable[[str], None],
    ) -> None:
        self.job = job
        self.conn = conn
        self.archives_dir = job.dest_root / "archives"
        self.tar_shard_bytes = max(1024 * 1024, int(float(tar_shard_mb) * 1024 * 1024))
        self.max_members_per_shard = 50_000
        self._on_committed = on_committed
        self._on_failed = on_failed
        self._on_quota_pause = on_quota_pause
        self._log = log

        self.archives_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_restore_readme()
        self._loose_unlink_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=24, thread_name_prefix=f"r2-unlink-{job.job_id}"
        )
        self.reconcile_drive_manifests()
        self.reconcile_missing_drive_shards()
        self.heal_uncleaned_loose_files()

        self._seq_lock = threading.Lock()
        self._next_seq = self._init_next_seq()
        self._open_shards: dict[str, _StagingShard] = {}
        self._known_dirs: set[str] = set()
        self._archived_paths_mem: set[str] = set()

        self._inflight_lock = threading.Lock()
        self._inflight_tasks = 0
        self._closed = False

        self._pack_q: queue.Queue[Any] = queue.Queue(maxsize=256)
        self._drive_flush_q: queue.Queue[tuple[_StagingShard, int] | None] = queue.Queue(maxsize=2)
        self._flush_threads = [
            threading.Thread(
                target=self._flush_worker_loop,
                name=f"r2-tar-flush-{job.job_id}-{i}",
                daemon=True,
            )
            for i in range(2)
        ]
        for th in self._flush_threads:
            th.start()
        self._packer_thread = threading.Thread(
            target=self._packer_loop,
            name=f"r2-tar-pack-{job.job_id}",
            daemon=True,
        )
        self._packer_thread.start()

    def _ensure_restore_readme(self) -> None:
        readme = self.archives_dir / "README-RESTORE.txt"
        if readme.is_file():
            return
        content = (
            "Cloudflare R2 -> Google Drive TAR Shards Archive\n"
            "===============================================\n\n"
            "Format: Uncompressed POSIX PAX (.tar) shards + JSONL manifests (*.tar.manifest.jsonl).\n\n"
            "Restore all shards under this directory preserving relative bucket/key paths:\n"
            "  mkdir -p ./restored\n"
            "  find . -name '*.tar' -print0 | xargs -0 -n1 -I{} tar -xf '{}' -C ./restored\n\n"
            "Notes:\n"
            "- If an S3 key collided with a directory of the same path (e.g. 'a' and 'a/b'),\n"
            "  the file object is stored as 'a/__r2_object__'.\n"
            "- Each '*.tar.manifest.jsonl' file lists every archived object with its bucket,\n"
            "  key, archive_path, size, source, and shard byte size for integrity audits.\n"
        )
        with contextlib.suppress(OSError):
            tmp = readme.with_name(f".{readme.name}.ctpart.{os.getpid()}.{uuid.uuid4().hex[:6]}")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, readme)

    def _init_next_seq(self) -> dict[str, int]:
        seqs: dict[str, int] = {}
        with self.job.db_lock:
            rows = self.conn.execute("SELECT bucket, shard_name FROM tar_shards").fetchall()
        for bucket, shard_name in rows:
            m = _SHARD_SEQ_RE.search(str(shard_name))
            if m:
                n = int(m.group(1))
                seqs[str(bucket)] = max(seqs.get(str(bucket), 1), n + 1)
        if self.archives_dir.is_dir():
            for bucket_dir in self.archives_dir.iterdir():
                if not bucket_dir.is_dir():
                    continue
                for p in bucket_dir.iterdir():
                    m = _SHARD_SEQ_RE.search(p.name)
                    if m:
                        n = int(m.group(1))
                        # Prefix before -part- is safe_bucket; preserve highest seq across matching dirs too.
                        b_guess = bucket_dir.name
                        seqs[b_guess] = max(seqs.get(b_guess, 1), n + 1)
        return seqs

    def _alloc_seq(self, bucket: str, safe_bucket: str) -> int:
        with self._seq_lock:
            cur = max(self._next_seq.get(bucket, 1), self._next_seq.get(safe_bucket, 1))
            self._next_seq[bucket] = cur + 1
            self._next_seq[safe_bucket] = cur + 1
            return cur

    def pending_count(self) -> int:
        with self._inflight_lock:
            return self._inflight_tasks

    def has_open_shards(self) -> bool:
        with self._inflight_lock:
            return self._inflight_tasks > 0 or bool(self._open_shards)

    def find_archived(self, rel_path: str, expected_size: int | None = None) -> tuple[str, int, str, str] | None:
        clean = rel_path.strip("/") or "__r2_root__"
        cands = (clean, f"{clean}/__r2_object__")
        with self.job.db_lock:
            rows = self.conn.execute(
                "SELECT rel_path, size, bucket, key FROM archived_files WHERE rel_path IN (?, ?)",
                cands,
            ).fetchall()
        for rpath, sz, b, k in rows:
            if expected_size is None or int(sz) == int(expected_size):
                return str(rpath), int(sz), str(b), str(k)
        return None

    def backfill_archived_bk(self, archived_rel_path: str, bucket: str, key: str) -> None:
        with self.job.db_lock:
            self.conn.execute(
                "UPDATE archived_files SET bucket=?, key=? WHERE rel_path=? AND bucket=''",
                (bucket, key, archived_rel_path),
            )
            self.conn.commit()

    def submit(self, task: _ArchiveTask) -> None:
        with self._inflight_lock:
            self._inflight_tasks += 1
        self._pack_q.put(task)

    def flush_open_shards(self, *, wait: bool = True) -> None:
        ev = threading.Event()
        self._pack_q.put(_FlushOpenMarker(ev))
        if wait:
            ev.wait()

    def close(self, *, wait: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        self._pack_q.put(_StopPackerMarker())
        if wait:
            self._packer_thread.join()
            for _ in self._flush_threads:
                self._drive_flush_q.put(None)
            for th in self._flush_threads:
                th.join()
            self._loose_unlink_pool.shutdown(wait=True)

    def _dec_inflight(self, count: int) -> None:
        if count <= 0:
            return
        with self._inflight_lock:
            self._inflight_tasks = max(0, self._inflight_tasks - count)

    def _open_new_shard(self, bucket: str) -> _StagingShard:
        safe_bucket = sanitize_segment(bucket)
        while True:
            seq = self._alloc_seq(bucket, safe_bucket)
            shard_name = f"{safe_bucket}-part-{seq:05d}.tar"
            rel_tar_path = f"archives/{safe_bucket}/{shard_name}"
            dest_tar_path = self.job.dest_root / rel_tar_path
            if not dest_tar_path.exists():
                break
        stage_tar_path = self.job.spool_dir / f".stage.{shard_name}.{uuid.uuid4().hex[:8]}.part"
        stage_tar_path.parent.mkdir(parents=True, exist_ok=True)
        raw_fh = open(stage_tar_path, "wb", buffering=4 * 1024 * 1024)
        tar = tarfile.open(fileobj=raw_fh, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8")
        shard = _StagingShard(
            bucket=bucket,
            safe_bucket=safe_bucket,
            shard_name=shard_name,
            rel_tar_path=rel_tar_path,
            stage_tar_path=stage_tar_path,
            raw_fh=raw_fh,
            tar=tar,
        )
        self._open_shards[bucket] = shard
        return shard

    def _seal_and_enqueue(self, bucket: str) -> None:
        shard = self._open_shards.pop(bucket, None)
        if shard is None:
            return
        try:
            shard.tar.close()
            shard.raw_fh.flush()
            shard.raw_fh.close()
            if not shard.entries:
                with contextlib.suppress(OSError):
                    shard.stage_tar_path.unlink(missing_ok=True)
                return
            tar_bytes = shard.stage_tar_path.stat().st_size
            self._drive_flush_q.put((shard, int(tar_bytes)))
        except Exception as exc:
            with contextlib.suppress(Exception):
                shard.raw_fh.close()
            with contextlib.suppress(OSError):
                shard.stage_tar_path.unlink(missing_ok=True)
            self._dec_inflight(len(shard.entries))
            if (isinstance(exc, OSError) and exc.errno == errno.ENOSPC) or "No space left on device" in str(exc):
                if self._on_quota_pause is not None:
                    self._on_quota_pause(
                        "Google Drive 挂载或本地缓存空间不足（可能已触发单账号 750 GB / 24h 上传限流 userRateLimitExceeded）；"
                        "任务已安全暂停，待云端后台上传释放缓存后执行 resume 继续"
                    )
                    self._on_failed(shard.entries, "")
                    return
            self._on_failed(shard.entries, f"TAR shard seal failed: {exc}")

    def _resolve_batch_paths(self, tasks: list[_ArchiveTask]) -> list[str]:
        """Resolve S3 file-vs-directory collisions in batch with one SQLite lock acquisition."""
        cleaned = [t.rel_path.strip("/") or "__r2_root__" for t in tasks]
        for c, t in zip(cleaned, tasks):
            if "/" in c:
                cur = c.rsplit("/", 1)[0]
                while cur:
                    if cur in self._known_dirs:
                        break
                    self._known_dirs.add(cur)
                    if "/" not in cur:
                        break
                    cur = cur.rsplit("/", 1)[0]
            if t.is_dir:
                self._known_dirs.add(c)

        db_check_idx = [i for i, (c, t) in enumerate(zip(cleaned, tasks)) if not t.is_dir and c not in self._known_dirs]
        db_has_children: set[str] = set()
        if db_check_idx:
            with self.job.db_lock:
                for i in db_check_idx:
                    c = cleaned[i]
                    if self.conn.execute(
                        "SELECT 1 FROM objects WHERE rel_path >= ? AND rel_path < ? LIMIT 1",
                        (f"{c}/", f"{c}0"),
                    ).fetchone() is not None:
                        db_has_children.add(c)

        resolved: list[str] = []
        for c, t in zip(cleaned, tasks):
            if not t.is_dir and (c in self._known_dirs or c in db_has_children):
                self._known_dirs.add(c)
                resolved.append(f"{c}/__r2_object__")
            else:
                resolved.append(c)
        return resolved

    def _packer_loop(self) -> None:
        while True:
            item = self._pack_q.get()
            if isinstance(item, _StopPackerMarker):
                for b in list(self._open_shards.keys()):
                    self._seal_and_enqueue(b)
                return
            if isinstance(item, _FlushOpenMarker):
                for b in list(self._open_shards.keys()):
                    self._seal_and_enqueue(b)
                # Wait until queued shards are flushed to Drive & committed.
                self._drive_flush_q.join()
                item.wait_event.set()
                continue

            batch: list[_ArchiveTask] = [item]
            deferred_ctrl: Any = None
            while len(batch) < 64:
                try:
                    nxt = self._pack_q.get_nowait()
                except queue.Empty:
                    break
                if isinstance(nxt, (_StopPackerMarker, _FlushOpenMarker)):
                    deferred_ctrl = nxt
                    break
                batch.append(nxt)

            resolved_paths = self._resolve_batch_paths(batch)
            for task, arch_path in zip(batch, resolved_paths):
                self._pack_single_task(task, arch_path)

            if isinstance(deferred_ctrl, _FlushOpenMarker):
                for b in list(self._open_shards.keys()):
                    self._seal_and_enqueue(b)
                self._drive_flush_q.join()
                deferred_ctrl.wait_event.set()
            elif isinstance(deferred_ctrl, _StopPackerMarker):
                for b in list(self._open_shards.keys()):
                    self._seal_and_enqueue(b)
                return

    def _pack_single_task(self, task: _ArchiveTask, archive_path: str) -> None:
        shard_bucket = task.shard_bucket or task.bucket or "drive-existing"
        shard: _StagingShard | None = None
        try:
            shard = self._open_shards.get(shard_bucket)
            if shard is None:
                # Cap total open shards across different buckets to keep NVMe staging bounded.
                if len(self._open_shards) >= 2:
                    oldest_bucket = min(self._open_shards, key=lambda bk: self._open_shards[bk].created_mono)
                    self._seal_and_enqueue(oldest_bucket)
                shard = self._open_new_shard(shard_bucket)

            member_name = archive_path
            if task.is_dir and (member_name in shard.seen_paths or member_name in self._archived_paths_mem):
                # Regular file already took this exact member name in this shard; skip conflicting dir entry.
                self._dec_inflight(1)
                self._on_committed(
                    [
                        _ArchivedEntry(
                            bucket=task.bucket,
                            key=task.key,
                            orig_rel_path=task.rel_path,
                            archive_path=archive_path,
                            size=0,
                            is_dir=True,
                            source=task.source,
                            loose_path=task.loose_path,
                        )
                    ],
                    0,
                    {},
                    0,
                    0,
                    0,
                    0,
                )
                return

            now_mtime = int(time.time())
            ti = tarfile.TarInfo(name=member_name)
            ti.mtime = now_mtime
            if task.is_dir:
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                ti.size = 0
                shard.tar.addfile(ti)
            else:
                ti.type = tarfile.REGTYPE
                ti.mode = 0o644
                ti.size = int(task.size)
                if task.payload_bytes is not None:
                    with io.BytesIO(task.payload_bytes) as bio:
                        shard.tar.addfile(ti, fileobj=bio)
                elif task.source_path is not None:
                    with task.source_path.open("rb") as src_f:
                        shard.tar.addfile(ti, fileobj=src_f)
                else:
                    with io.BytesIO(b"") as bio:
                        shard.tar.addfile(ti, fileobj=bio)

            shard.seen_paths.add(member_name)
            if not task.is_dir:
                self._archived_paths_mem.add(member_name)
            shard.payload_bytes += int(task.size)
            shard.file_count += 1
            shard.entries.append(
                _ArchivedEntry(
                    bucket=task.bucket,
                    key=task.key,
                    orig_rel_path=task.rel_path,
                    archive_path=archive_path,
                    size=int(task.size),
                    is_dir=bool(task.is_dir),
                    source=task.source,
                    loose_path=task.loose_path,
                )
            )
            if shard.payload_bytes >= self.tar_shard_bytes or shard.file_count >= self.max_members_per_shard:
                self._seal_and_enqueue(shard_bucket)
        except Exception as exc:
            err_entry = _ArchivedEntry(
                bucket=task.bucket,
                key=task.key,
                orig_rel_path=task.rel_path,
                archive_path=archive_path,
                size=int(task.size),
                is_dir=bool(task.is_dir),
                source=task.source,
                loose_path=task.loose_path,
            )
            self._dec_inflight(1)
            self._on_failed([err_entry], f"TAR pack failed: {exc}")
        finally:
            if task.unlink_source_after_pack and task.source_path is not None:
                with contextlib.suppress(OSError):
                    task.source_path.unlink(missing_ok=True)
            if task.held_budget and task.release_fn is not None:
                task.release_fn(task.held_budget)

    def _flush_worker_loop(self) -> None:
        while True:
            item = self._drive_flush_q.get()
            if item is None:
                self._drive_flush_q.task_done()
                return
            shard, expected_tar_bytes = item
            try:
                self._flush_shard_to_drive(shard, expected_tar_bytes)
            finally:
                self._drive_flush_q.task_done()

    def _flush_shard_to_drive(self, shard: _StagingShard, expected_tar_bytes: int) -> None:
        dest_tar_path = self.job.dest_root / shard.rel_tar_path
        manifest_path = dest_tar_path.with_name(f"{dest_tar_path.name}.manifest.jsonl")
        entries = shard.entries
        last_err: Exception | None = None
        for attempt in range(3):
            tar_stage: Path | None = None
            man_stage: Path | None = None
            try:
                dest_tar_path.parent.mkdir(parents=True, exist_ok=True)
                tar_stage = dest_tar_path.with_name(f".{dest_tar_path.name}.ctpart.{os.getpid()}.{uuid.uuid4().hex[:6]}")
                man_stage = manifest_path.with_name(f".{manifest_path.name}.ctpart.{os.getpid()}.{uuid.uuid4().hex[:6]}")
                with shard.stage_tar_path.open("rb") as src_f, tar_stage.open("wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f, length=8 * 1024 * 1024)
                    dst_f.flush()
                    with contextlib.suppress(OSError):
                        os.fsync(dst_f.fileno())
                if tar_stage.stat().st_size != expected_tar_bytes:
                    raise OSError(
                        f"Drive TAR stage size mismatch: expected {expected_tar_bytes}B, got {tar_stage.stat().st_size}B"
                    )

                with man_stage.open("w", encoding="utf-8") as mf:
                    for e in entries:
                        mf.write(
                            json.dumps(
                                {
                                    "bucket": e.bucket,
                                    "key": e.key,
                                    "rel_path": e.orig_rel_path,
                                    "archive_path": e.archive_path,
                                    "size": e.size,
                                    "is_dir": e.is_dir,
                                    "source": e.source,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    mf.write(
                        json.dumps(
                            {
                                "__shard_meta__": True,
                                "bucket": shard.bucket,
                                "shard_name": shard.shard_name,
                                "rel_tar_path": shard.rel_tar_path,
                                "file_count": len(entries),
                                "payload_bytes": shard.payload_bytes,
                                "tar_bytes": expected_tar_bytes,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    mf.flush()
                    with contextlib.suppress(OSError):
                        os.fsync(mf.fileno())

                os.replace(man_stage, manifest_path)
                man_stage = None
                os.replace(tar_stage, dest_tar_path)
                tar_stage = None
                actual_sz = dest_tar_path.stat().st_size
                if actual_sz != expected_tar_bytes:
                    raise OSError(f"Drive TAR verify failed: expected {expected_tar_bytes}B, got {actual_sz}B")
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                for tmp in (tar_stage, man_stage):
                    if tmp is not None:
                        with contextlib.suppress(OSError):
                            tmp.unlink(missing_ok=True)
                time.sleep(1.0 * (attempt + 1))

        with contextlib.suppress(OSError):
            shard.stage_tar_path.unlink(missing_ok=True)

        if last_err is not None:
            self._dec_inflight(len(entries))
            if (isinstance(last_err, OSError) and last_err.errno == errno.ENOSPC) or "No space left on device" in str(last_err):
                if self._on_quota_pause is not None:
                    self._on_quota_pause(
                        "Google Drive 挂载或本地缓存空间不足（可能已触发单账号 750 GB / 24h 上传限流 userRateLimitExceeded）；"
                        "任务已安全暂停，待云端后台上传释放缓存后执行 resume 继续"
                    )
                    self._on_failed(entries, "")
                    return
            self._on_failed(entries, f"Drive TAR flush failed: {last_err}")
            return

        # Commit shard + archived_files + objects in a single SQLite transaction BEFORE unlinking loose files.
        deltas, cons_cnt, cons_bytes, sess_cnt, sess_bytes = self._commit_shard_to_db(
            shard, expected_tar_bytes
        )
        self._dec_inflight(len(entries))
        self._on_committed(
            entries,
            expected_tar_bytes,
            deltas,
            cons_cnt,
            cons_bytes,
            sess_cnt,
            sess_bytes,
        )

        # Unlink consolidated loose source files ONLY after Drive flush & SQLite commit succeeded!
        loose_files = [e.loose_path for e in entries if e.source == "drive_existing" and not e.is_dir and e.loose_path is not None]
        if loose_files:
            def _unlink_one(p: Path) -> None:
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)

            list(self._loose_unlink_pool.map(_unlink_one, loose_files))
        if any(e.source == "drive_existing" for e in entries):
            with self.job.db_lock:
                self.conn.execute("UPDATE archived_files SET loose_cleaned = 1 WHERE shard_name = ?", (shard.shard_name,))
                self.conn.commit()

    def _commit_shard_to_db(
        self, shard: _StagingShard, expected_tar_bytes: int
    ) -> tuple[dict[str, tuple[int, int]], int, int, int, int]:
        entries = shard.entries
        now_ts = time.time()
        deltas: dict[str, tuple[int, int]] = {}
        cons_cnt = 0
        cons_bytes = 0
        sess_cnt = 0
        sess_bytes = 0

        by_bucket: dict[str, list[_ArchivedEntry]] = {}
        for e in entries:
            if e.bucket and e.key:
                by_bucket.setdefault(e.bucket, []).append(e)
            if e.source == "drive_existing":
                cons_cnt += 1
                cons_bytes += int(e.size)
            sess_cnt += 1
            sess_bytes += int(e.size)

        with self.job.db_lock:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO tar_shards
                (bucket, shard_name, rel_tar_path, file_count, payload_bytes, tar_bytes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shard.bucket,
                    shard.shard_name,
                    shard.rel_tar_path,
                    len(entries),
                    shard.payload_bytes,
                    expected_tar_bytes,
                    now_ts,
                ),
            )
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO archived_files
                (rel_path, bucket, key, size, shard_name, source, loose_cleaned, archived_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        e.archive_path,
                        e.bucket,
                        e.key,
                        int(e.size),
                        shard.shard_name,
                        e.source,
                        0 if e.source == "drive_existing" else 1,
                        now_ts,
                    )
                    for e in entries
                ],
            )
            obj_updates: list[tuple[str, str, float, str, str]] = []
            rel_only_updates: list[tuple[str, float, str, str]] = []
            for b, b_entries in by_bucket.items():
                existing_state: dict[str, tuple[str, int, str]] = {}
                for idx in range(0, len(b_entries), 400):
                    chunk_keys = [x.key for x in b_entries[idx : idx + 400]]
                    q_marks = ",".join("?" for _ in chunk_keys)
                    rows = self.conn.execute(
                        f"SELECT key, status, size, rel_path FROM objects WHERE bucket = ? AND key IN ({q_marks})",
                        [b, *chunk_keys],
                    ).fetchall()
                    for r_key, r_st, r_sz, r_rel in rows:
                        existing_state[str(r_key)] = (str(r_st), int(r_sz), str(r_rel))

                for e in b_entries:
                    cur = existing_state.get(e.key)
                    if cur is None:
                        continue
                    cur_st, cur_sz, cur_rel = cur
                    if cur_st in ("pending", "ia_deferred", "failed"):
                        obj_updates.append((e.archive_path, "copied", now_ts, b, e.key))
                        dc, db = deltas.get(cur_st, (0, 0))
                        deltas[cur_st] = (dc - 1, db - cur_sz)
                        cc, cb = deltas.get("copied", (0, 0))
                        deltas["copied"] = (cc + 1, cb + cur_sz)
                    elif e.archive_path != cur_rel:
                        # Preserve copied/deleted status, update rel_path if relocated.
                        rel_only_updates.append((e.archive_path, now_ts, b, e.key))
            if obj_updates:
                self.conn.executemany(
                    "UPDATE objects SET rel_path=?, status=?, error='', updated_at=? WHERE bucket=? AND key=?",
                    obj_updates,
                )
            if rel_only_updates:
                self.conn.executemany(
                    "UPDATE objects SET rel_path=?, updated_at=? WHERE bucket=? AND key=?",
                    rel_only_updates,
                )
            self.conn.commit()
        return deltas, cons_cnt, cons_bytes, sess_cnt, sess_bytes

    def reconcile_drive_manifests(self) -> int:
        """Recover shards written to Drive before SQLite checkpoint was persisted."""
        if not self.archives_dir.is_dir():
            return 0
        for p in list(self.archives_dir.rglob("*")):
            if p.is_file() and _is_temp_artifact_name(p.name):
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)
        with self.job.db_lock:
            known_shards = {
                str(r[0]) for r in self.conn.execute("SELECT shard_name FROM tar_shards").fetchall()
            }
        replayed = 0
        for man_path in sorted(self.archives_dir.rglob("*.tar.manifest.jsonl")):
            if _is_temp_artifact_name(man_path.name):
                continue
            try:
                lines = [ln for ln in man_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if not lines:
                    continue
                meta = json.loads(lines[-1])
                if not isinstance(meta, dict) or not meta.get("__shard_meta__"):
                    continue
                shard_name = str(meta.get("shard_name") or "")
                if not shard_name or shard_name in known_shards:
                    continue
                rel_tar_path = str(meta["rel_tar_path"])
                tar_path = self.job.dest_root / rel_tar_path
                expected_tar_bytes = int(meta["tar_bytes"])
                if not tar_path.is_file() or tar_path.stat().st_size != expected_tar_bytes:
                    continue
                items = [json.loads(ln) for ln in lines[:-1]]
                shard_bucket = str(meta.get("bucket") or "drive-existing")
                entries = [
                    _ArchivedEntry(
                        bucket=str(it.get("bucket") or ""),
                        key=str(it.get("key") or ""),
                        orig_rel_path=str(it.get("rel_path") or it.get("archive_path") or ""),
                        archive_path=str(it.get("archive_path") or ""),
                        size=int(it.get("size") or 0),
                        is_dir=bool(it.get("is_dir", False)),
                        source=str(it.get("source") or "r2_download"),
                    )
                    for it in items
                    if isinstance(it, dict)
                ]
                dummy = _StagingShard(
                    bucket=shard_bucket,
                    safe_bucket=sanitize_segment(shard_bucket),
                    shard_name=shard_name,
                    rel_tar_path=rel_tar_path,
                    stage_tar_path=tar_path,
                    raw_fh=None,  # type: ignore[arg-type]
                    tar=None,  # type: ignore[arg-type]
                    entries=entries,
                    payload_bytes=int(meta.get("payload_bytes") or 0),
                    file_count=len(entries),
                )
                self._commit_shard_to_db(dummy, expected_tar_bytes)
                known_shards.add(shard_name)
                replayed += 1
            except Exception as exc:
                self._log(f"job {self.job.job_id}: manifest reconcile skipped {man_path.name}: {exc}")
        if replayed:
            self._log(f"job {self.job.job_id}: reconciled {replayed} TAR shards from Drive manifests")
        return replayed

    def reconcile_missing_drive_shards(self) -> int:
        """Reset objects from shards recorded in SQLite whose Drive .tar file is missing or size-mismatched."""
        with self.job.db_lock:
            rows = self.conn.execute("SELECT shard_name, rel_tar_path, tar_bytes FROM tar_shards").fetchall()
        missing: list[tuple[str, Path]] = []
        for sname, rel_tar, expected_sz in rows:
            tar_path = self.job.dest_root / str(rel_tar)
            exists_ok = False
            with contextlib.suppress(OSError):
                exists_ok = tar_path.is_file() and tar_path.stat().st_size == int(expected_sz)
            if not exists_ok:
                missing.append((str(sname), tar_path))
        if not missing:
            return 0
        reset_objs = 0
        for sname, tar_path in missing:
            with contextlib.suppress(OSError):
                tar_path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                tar_path.with_name(f"{tar_path.name}.manifest.jsonl").unlink(missing_ok=True)
            with self.job.db_lock:
                cur = self.conn.execute(
                    """
                    UPDATE objects SET status='pending', error=''
                    WHERE status='copied'
                      AND (bucket, key) IN (
                          SELECT bucket, key FROM archived_files
                          WHERE shard_name=? AND bucket != '' AND key != ''
                      )
                    """,
                    (sname,),
                )
                reset_objs += max(0, int(cur.rowcount or 0))
                self.conn.execute("DELETE FROM archived_files WHERE shard_name=?", (sname,))
                self.conn.execute("DELETE FROM tar_shards WHERE shard_name=?", (sname,))
                self.conn.commit()
        self._log(
            f"job {self.job.job_id}: reset {len(missing)} missing/incomplete TAR shards ({reset_objs} objects back to pending)"
        )
        return len(missing)

    def heal_uncleaned_loose_files(self) -> None:
        """Finish or roll back shards whose loose Drive source cleanup was interrupted by crash."""
        with self.job.db_lock:
            rows = self.conn.execute(
                """
                SELECT a.rel_path, a.is_dir, a.shard_name, t.rel_tar_path, t.tar_bytes
                FROM (
                    SELECT af.rel_path, COALESCE(o.is_dir, 0) AS is_dir, af.shard_name
                    FROM archived_files af
                    LEFT JOIN objects o ON af.bucket = o.bucket AND af.key = o.key
                    WHERE af.loose_cleaned = 0
                ) a
                JOIN tar_shards t ON a.shard_name = t.shard_name
                """
            ).fetchall()
        if not rows:
            with self.job.db_lock:
                orphan_rows = self.conn.execute(
                    "SELECT COUNT(*) FROM archived_files WHERE loose_cleaned = 0"
                ).fetchone()[0]
                if orphan_rows:
                    self.conn.execute(
                        "DELETE FROM archived_files WHERE loose_cleaned = 0 AND shard_name NOT IN (SELECT shard_name FROM tar_shards)"
                    )
                    self.conn.commit()
            return

        by_shard: dict[str, tuple[str, int, list[tuple[str, bool]]]] = {}
        for rpath, is_dir_val, sname, rel_tar, tbytes in rows:
            entry = by_shard.setdefault(str(sname), (str(rel_tar), int(tbytes), []))
            entry[2].append((str(rpath), bool(is_dir_val)))

        for sname, (rel_tar, expected_sz, items) in by_shard.items():
            tar_path = self.job.dest_root / rel_tar
            verified = False
            with contextlib.suppress(OSError):
                verified = tar_path.is_file() and tar_path.stat().st_size == expected_sz
            if verified:
                files_to_unlink: list[Path] = []
                for rp, is_d in items:
                    if is_d:
                        continue
                    files_to_unlink.append(self.job.dest_root / rp)
                    if rp.endswith("/__r2_object__"):
                        files_to_unlink.append(self.job.dest_root / rp[: -len("/__r2_object__")])
                if files_to_unlink:
                    def _rm(p: Path) -> None:
                        with contextlib.suppress(OSError):
                            if p.is_file():
                                p.unlink(missing_ok=True)

                    list(self._loose_unlink_pool.map(_rm, files_to_unlink))
                with self.job.db_lock:
                    self.conn.execute("UPDATE archived_files SET loose_cleaned = 1 WHERE shard_name = ?", (sname,))
                    self.conn.commit()
            else:
                with self.job.db_lock:
                    self.conn.execute("DELETE FROM archived_files WHERE shard_name = ?", (sname,))
                    self.conn.execute("DELETE FROM tar_shards WHERE shard_name = ?", (sname,))
                    self.conn.commit()
                self._log(f"job {self.job.job_id}: dropped unverified shard {sname}; loose files preserved")


class MigrationManager:
    """Coordinates resumable multi-bucket R2 -> Google Drive migration jobs."""

    def __init__(
        self,
        *,
        data_dir: Path,
        work_dir: Path,
        drive_mount: Path = Path("/content/drive/MyDrive"),
        default_workers: int = DEFAULT_WORKERS,
        default_writer_workers: int = DEFAULT_WRITER_WORKERS,
        spool_bytes_cap: int = DEFAULT_SPOOL_BYTES,
        checkpoint_interval: float = DEFAULT_CHECKPOINT_INTERVAL,
        page_size: int = DEFAULT_PAGE_SIZE,
        client_timeout: float = 60.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.work_dir = Path(work_dir)
        self.drive_mount = Path(drive_mount)
        self.jobs_drive_dir = self.data_dir / "jobs"
        self.state_dir = self.work_dir / "state"
        self.spool_root = self.work_dir / "spool"
        self.jobs_drive_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.spool_root.mkdir(parents=True, exist_ok=True)

        self.default_workers = max(1, int(default_workers))
        self.default_writer_workers = max(1, int(default_writer_workers))
        self.spool_bytes_cap = max(64 * 1024 * 1024, int(spool_bytes_cap))
        self.checkpoint_interval = max(0.5, float(checkpoint_interval))
        self.page_size = max(1, min(1000, int(page_size)))
        self.client_timeout = float(client_timeout)
        self._log = log or (lambda msg: print(f"[r2-migrate] {msg}", file=sys.stderr, flush=True))

        self.ledger = R2UsageLedger(self.data_dir)
        self.cred_store = CredentialStore(self.data_dir)
        self._mgr_lock = threading.Lock()
        self._jobs: dict[str, _RuntimeJob] = {}
        self._shutdown = False
        self._load_existing_jobs()

    def _compute_scope_hash(self, config: dict[str, Any]) -> str:
        canonical = json.dumps(
            {
                "endpoint_scope": config["endpoint_scope"],
                "buckets": sorted(config.get("buckets") or []),
                "exclude_buckets": sorted(config.get("exclude_buckets") or []),
                "prefix": config.get("prefix") or "",
                "dest_root": config["dest_root"],
                "flatten_single_bucket": bool(config.get("flatten_single_bucket", False)),
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    def _job_json_path(self, job: _RuntimeJob) -> Path:
        return job.drive_job_dir / "job.json"

    def _job_checkpoint_path(self, job: _RuntimeJob) -> Path:
        return job.drive_job_dir / "checkpoint.sqlite3"

    def _ensure_job_db_open(self, job: _RuntimeJob) -> sqlite3.Connection:
        with job.db_lock:
            if job.conn is not None:
                return job.conn
            if not job.db_path.is_file():
                _restore_sqlite_from_drive(self._job_checkpoint_path(job), job.db_path)
            conn = _open_state_db(job.db_path)
            job.conn = conn
            self._refresh_counts_locked(job, conn)
            self._refresh_buckets_locked(job, conn)
            return conn

    def _adjust_status_counts(self, job: _RuntimeJob, deltas: dict[str, tuple[int, int]]) -> None:
        if not deltas:
            return
        with job.lock:
            for st_key, (dc, db) in deltas.items():
                if dc == 0 and db == 0:
                    continue
                cur = job.status_counts.setdefault(st_key, {"count": 0, "bytes": 0})
                cur["count"] = max(0, int(cur["count"]) + int(dc))
                cur["bytes"] = max(0, int(cur["bytes"]) + int(db))

    def _refresh_counts_locked(self, job: _RuntimeJob, conn: sqlite3.Connection) -> None:
        counts: dict[str, dict[str, int]] = {}
        for status, cnt, total_sz in conn.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(size), 0) FROM objects GROUP BY status"
        ).fetchall():
            counts[str(status)] = {"count": int(cnt), "bytes": int(total_sz)}
        shard_row = conn.execute("SELECT COUNT(*), COALESCE(SUM(tar_bytes), 0) FROM tar_shards").fetchone()
        cons_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM archived_files WHERE source = 'drive_existing'"
        ).fetchone()
        now_m = time.monotonic()
        with job.lock:
            job.status_counts = counts
            job.tar_shards_count = int(shard_row[0]) if shard_row else 0
            job.tar_bytes_total = int(shard_row[1]) if shard_row else 0
            job.consolidated_files = int(cons_row[0]) if cons_row else 0
            job.consolidated_bytes = int(cons_row[1]) if cons_row else 0
            job.last_db_refresh_mono = now_m

    def _refresh_buckets_locked(self, job: _RuntimeJob, conn: sqlite3.Connection) -> None:
        b_rows = conn.execute(
            "SELECT bucket, last_key, listing_done, objects_listed, bytes_listed FROM buckets ORDER BY bucket"
        ).fetchall()
        by_bucket: dict[str, dict[str, int]] = {}
        for status_val, b_name, cnt in conn.execute(
            "SELECT status, bucket, COUNT(*) FROM objects GROUP BY status, bucket"
        ).fetchall():
            by_bucket.setdefault(str(b_name), {})[str(status_val)] = int(cnt)
        snap = []
        for r in b_rows:
            b_name = str(r[0])
            st_map = by_bucket.get(b_name, {})
            copied_c = st_map.get("copied", 0)
            deleted_c = st_map.get("deleted", 0)
            snap.append(
                {
                    "bucket": b_name,
                    "last_key": r[1],
                    "listing_done": bool(r[2]),
                    "objects_listed": int(r[3]),
                    "bytes_listed": int(r[4]),
                    "objects_migrated": copied_c + deleted_c,
                    "objects_deleted": deleted_c,
                    "objects_failed": st_map.get("failed", 0),
                    "objects_ia_deferred": st_map.get("ia_deferred", 0),
                }
            )
        with job.lock:
            job.buckets_snapshot = snap

    def _snapshot_job(self, job: _RuntimeJob) -> dict[str, Any]:
        with job.lock:
            counts = {
                k: dict(v)
                for k, v in job.status_counts.items()
            }
            pending = counts.get("pending", {}).get("count", 0)
            ia_deferred = counts.get("ia_deferred", {}).get("count", 0)
            copied = counts.get("copied", {}).get("count", 0)
            deleted = counts.get("deleted", {}).get("count", 0)
            failed = counts.get("failed", {}).get("count", 0)
            listed = pending + ia_deferred + copied + deleted + failed
            migrated = copied + deleted
            bytes_listed = sum(v.get("bytes", 0) for v in counts.values())
            bytes_migrated = counts.get("copied", {}).get("bytes", 0) + counts.get("deleted", {}).get("bytes", 0)

            now = time.time()
            elapsed = max(0.001, (job.finished_at or now) - (job.started_at or job.created_at))
            if job.session_started_at is not None:
                active_elapsed = max(0.001, (job.session_finished_at or now) - job.session_started_at)
            elif job.started_at:
                active_elapsed = max(0.001, (job.finished_at or now) - job.started_at)
            else:
                active_elapsed = elapsed
            files_per_sec = round(job.files_done_session / active_elapsed, 2) if job.files_done_session else 0.0
            mb_per_sec = round((job.bytes_done_session / (1024 * 1024)) / active_elapsed, 2) if job.bytes_done_session else 0.0

            cfg_safe = {k: v for k, v in job.config.items() if "secret" not in k.lower()}
            return {
                "id": job.job_id,
                "status": job.status,
                "message": job.message,
                "paid_fallback_used": job.paid_fallback_used,
                "resumed_from_checkpoint": bool(job.config.get("resumed_from_checkpoint", False)),
                "config": cfg_safe,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "updated_at": job.updated_at,
                "progress": {
                    "objects_listed": listed,
                    "objects_migrated": migrated,
                    "objects_copied_pending_delete": copied,
                    "objects_deleted": deleted,
                    "objects_failed": failed,
                    "objects_pending": pending,
                    "objects_ia_deferred": ia_deferred,
                    "bytes_listed": bytes_listed,
                    "bytes_migrated": bytes_migrated,
                    "files_per_sec": files_per_sec,
                    "mb_per_sec": mb_per_sec,
                    "storage_mode": str(job.config.get("storage_mode") or "files"),
                    "tar_shards": job.tar_shards_count,
                    "tar_bytes": job.tar_bytes_total,
                    "consolidated_existing_files": job.consolidated_files,
                    "consolidated_existing_bytes": job.consolidated_bytes,
                },
                "buckets": list(job.buckets_snapshot),
                "recent_errors": list(job.recent_errors),
                "checkpoint_path": str(self._job_checkpoint_path(job)),
                "usage": self.ledger.snapshot(),
            }

    def _persist_job_summary(self, job: _RuntimeJob) -> None:
        snap = self._snapshot_job(job)
        # Keep stored job.json concise without volatile full usage duplicate if desired; including usage is nice for inspection.
        _atomic_write_json(self._job_json_path(job), snap)

    def _flush_checkpoint(self, job: _RuntimeJob, conn: sqlite3.Connection) -> None:
        with job.db_lock:
            self._refresh_counts_locked(job, conn)
            self._refresh_buckets_locked(job, conn)
            local_tmp = self.state_dir / f".{job.job_id}.backup.tmp"
            try:
                _backup_sqlite_to_drive(conn, local_tmp, self._job_checkpoint_path(job), db_path=job.db_path)
            except OSError as exc:
                self._log(f"job {job.job_id}: checkpoint backup to Drive deferred: {exc}")
        with job.lock:
            job.updated_at = time.time()
        try:
            self._persist_job_summary(job)
        except OSError as exc:
            self._log(f"job {job.job_id}: job.json summary persist deferred: {exc}")

    def _load_existing_jobs(self) -> None:
        if not self.jobs_drive_dir.is_dir():
            return
        for job_dir in sorted(self.jobs_drive_dir.iterdir()):
            if not job_dir.is_dir():
                continue
            jpath = job_dir / "job.json"
            if not jpath.is_file():
                continue
            try:
                data = json.loads(jpath.read_text(encoding="utf-8"))
                cfg = dict(data.get("config") or {})
                job_id = str(data.get("id") or job_dir.name)
                dest_root = Path(str(cfg.get("dest_root") or (self.drive_mount / "r2-backup")))
                status = str(data.get("status") or "interrupted")
                if status in ("queued", "running"):
                    status = "interrupted"
                prog = data.get("progress") or {}
                job = _RuntimeJob(
                    job_id=job_id,
                    config=cfg,
                    db_path=self.state_dir / f"{job_id}.sqlite3",
                    drive_job_dir=job_dir,
                    spool_dir=self.spool_root / job_id,
                    dest_root=dest_root,
                    status=status,
                    message=str(data.get("message") or ""),
                    paid_fallback_used=bool(data.get("paid_fallback_used", False)),
                    created_at=float(data.get("created_at") or time.time()),
                    started_at=data.get("started_at"),
                    finished_at=data.get("finished_at"),
                    updated_at=float(data.get("updated_at") or time.time()),
                    tar_shards_count=int(prog.get("tar_shards", 0) or 0),
                    tar_bytes_total=int(prog.get("tar_bytes", 0) or 0),
                    consolidated_files=int(prog.get("consolidated_existing_files", 0) or 0),
                    consolidated_bytes=int(prog.get("consolidated_existing_bytes", 0) or 0),
                    buckets_snapshot=list(data.get("buckets") or []),
                )
                job.status_counts = {
                    "pending": {"count": int(prog.get("objects_pending", 0)), "bytes": 0},
                    "ia_deferred": {"count": int(prog.get("objects_ia_deferred", 0)), "bytes": 0},
                    "copied": {"count": int(prog.get("objects_copied_pending_delete", 0)), "bytes": 0},
                    "deleted": {"count": int(prog.get("objects_deleted", 0)), "bytes": 0},
                    "failed": {"count": int(prog.get("objects_failed", 0)), "bytes": 0},
                }
                for err_item in data.get("recent_errors") or []:
                    job.recent_errors.append(dict(err_item))
                self._jobs[job_id] = job
                if job.db_path.is_file():
                    with contextlib.suppress(sqlite3.Error, OSError):
                        self._ensure_job_db_open(job)
            except (OSError, ValueError, TypeError):
                continue

    def recover_on_startup(self) -> list[str]:
        """Auto-resume jobs that were running when a previous Colab VM died if credentials exist."""
        resumed: list[str] = []
        creds, _ = self.cred_store.resolve(None)
        with self._mgr_lock:
            candidates = [j for j in self._jobs.values() if j.status == "interrupted"]
        for job in candidates:
            if creds is not None and not self._shutdown:
                self._log(f"Restoring interrupted migration job {job.job_id} from Drive checkpoint")
                self.resume_job(job.job_id, credentials=creds)
                resumed.append(job.job_id)
            else:
                with job.lock:
                    job.status = "paused_credentials"
                    job.message = f"Colab VM restarted; supply R2 credentials and call resume {job.job_id} to continue from checkpoint"
                    job.updated_at = time.time()
                self._persist_job_summary(job)
        return resumed

    def active_jobs_count(self) -> int:
        with self._mgr_lock:
            return sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._mgr_lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return [self._snapshot_job(j) for j in jobs]

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._require_job(job_id)
        if (
            job.conn is not None
            and job.status not in ("queued", "running")
            and (time.monotonic() - job.last_db_refresh_mono) > 5.0
        ):
            with job.db_lock:
                self._refresh_counts_locked(job, job.conn)
                self._refresh_buckets_locked(job, job.conn)
        return self._snapshot_job(job)

    def _require_job(self, job_id: str) -> _RuntimeJob:
        with self._mgr_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"未找到迁移任务: {job_id}")
        return job

    def list_buckets(self, credentials: dict[str, Any] | R2Credentials | None = None) -> list[dict[str, str]]:
        creds, _ = self.cred_store.resolve(credentials)
        if creds is None:
            raise ValueError("缺少 R2 凭据；请先通过 auth 保存或传入 credentials")
        client = R2Client(creds, ledger=self.ledger, timeout=self.client_timeout)
        try:
            return client.list_buckets()
        finally:
            client.close()
            self.ledger.flush(ignore_errors=True)

    def create_or_resume_job(
        self,
        *,
        buckets: list[str] | None = None,
        exclude_buckets: list[str] | None = None,
        prefix: str = "",
        output_dir: str | None = None,
        flatten_single_bucket: bool = False,
        delete_source: bool = False,
        cost_mode: str = "free-first",
        public_urls: dict[str, str] | list[str] | None = None,
        max_free_class_a: int = DEFAULT_SAFE_FREE_CLASS_A,
        max_free_class_b: int = DEFAULT_SAFE_FREE_CLASS_B,
        workers: int | None = None,
        writer_workers: int | None = None,
        overwrite: bool = False,
        storage_mode: str | None = None,
        tar_shard_mb: int | float | None = None,
        consolidate_existing: bool | None = None,
        new_job: bool = False,
        credentials: dict[str, Any] | R2Credentials | None = None,
    ) -> dict[str, Any]:
        if cost_mode not in COST_MODES:
            raise ValueError(f"无效 cost_mode: {cost_mode!r} (可选: {', '.join(COST_MODES)})")
        if storage_mode is not None and storage_mode not in STORAGE_MODES:
            raise ValueError(f"无效 storage_mode: {storage_mode!r} (可选: {', '.join(STORAGE_MODES)})")
        if tar_shard_mb is not None and float(tar_shard_mb) <= 0:
            raise ValueError(f"tar_shard_mb 必须大于 0: {tar_shard_mb!r}")
        creds, _ = self.cred_store.resolve(credentials)
        if creds is None:
            raise ValueError("缺少 R2 凭据；请先运行 `colabllm r2-migrate auth` 或在请求中提供凭据")
        public_map = parse_public_urls(public_urls)
        fallback_outputs = self.data_dir / "outputs"
        dest_root = resolve_drive_output_root(
            self.drive_mount,
            output_dir,
            default_subdir="r2-backup",
            fallback_root=fallback_outputs,
        )
        cfg = {
            "endpoint_scope": creds.scope_key(),
            "buckets": [b.strip() for b in (buckets or []) if str(b).strip()],
            "exclude_buckets": [b.strip() for b in (exclude_buckets or []) if str(b).strip()],
            "prefix": str(prefix or ""),
            "output_dir": output_dir or "r2-backup",
            "dest_root": str(dest_root),
            "flatten_single_bucket": bool(flatten_single_bucket),
            "delete_source": bool(delete_source),
            "cost_mode": cost_mode,
            "public_urls": public_map,
            "max_free_class_a": max(0, int(max_free_class_a)),
            "max_free_class_b": max(0, int(max_free_class_b)),
            "workers": max(1, int(workers or self.default_workers)),
            "writer_workers": max(1, int(writer_workers or self.default_writer_workers)),
            "overwrite": bool(overwrite),
            "storage_mode": storage_mode or "files",
            "tar_shard_mb": max(1.0, float(tar_shard_mb if tar_shard_mb is not None else DEFAULT_TAR_SHARD_MB)),
            "consolidate_existing": True if consolidate_existing is None else bool(consolidate_existing),
            "resumed_from_checkpoint": False,
        }
        scope_hash = self._compute_scope_hash(cfg)
        cfg["scope_hash"] = scope_hash

        if not new_job:
            with self._mgr_lock:
                existing = [
                    j
                    for j in self._jobs.values()
                    if j.config.get("scope_hash") == scope_hash and j.status in RESUMABLE_JOB_STATUSES
                ]
            if existing:
                existing.sort(key=lambda j: j.created_at, reverse=True)
                target = existing[0]
                if target.status in ("queued", "running") and target.worker_thread and target.worker_thread.is_alive():
                    return self._snapshot_job(target)
                return self.resume_job(
                    target.job_id,
                    cost_mode=cost_mode,
                    delete_source=delete_source,
                    workers=workers,
                    writer_workers=writer_workers,
                    max_free_class_a=max_free_class_a,
                    max_free_class_b=max_free_class_b,
                    overwrite=overwrite,
                    public_urls=public_map if public_urls is not None else None,
                    storage_mode=storage_mode,
                    tar_shard_mb=tar_shard_mb,
                    consolidate_existing=consolidate_existing,
                    credentials=creds,
                )

        job_id = "r2m_" + secrets.token_hex(6)
        drive_job_dir = self.jobs_drive_dir / job_id
        drive_job_dir.mkdir(parents=True, exist_ok=True)
        job = _RuntimeJob(
            job_id=job_id,
            config=cfg,
            db_path=self.state_dir / f"{job_id}.sqlite3",
            drive_job_dir=drive_job_dir,
            spool_dir=self.spool_root / job_id,
            dest_root=dest_root,
            status="queued",
            message="已创建任务，准备同步",
        )
        with self._mgr_lock:
            self._jobs[job_id] = job
        self._persist_job_summary(job)
        self._launch_job_thread(job, creds)
        return self._snapshot_job(job)

    def resume_job(
        self,
        job_id: str,
        *,
        cost_mode: str | None = None,
        delete_source: bool | None = None,
        workers: int | None = None,
        writer_workers: int | None = None,
        max_free_class_a: int | None = None,
        max_free_class_b: int | None = None,
        overwrite: bool | None = None,
        public_urls: dict[str, str] | list[str] | None = None,
        storage_mode: str | None = None,
        tar_shard_mb: int | float | None = None,
        consolidate_existing: bool | None = None,
        credentials: dict[str, Any] | R2Credentials | None = None,
    ) -> dict[str, Any]:
        job = self._require_job(job_id)
        if cost_mode is not None and cost_mode not in COST_MODES:
            raise ValueError(f"无效 cost_mode: {cost_mode!r}")
        if storage_mode is not None and storage_mode not in STORAGE_MODES:
            raise ValueError(f"无效 storage_mode: {storage_mode!r}")
        if tar_shard_mb is not None and float(tar_shard_mb) <= 0:
            raise ValueError(f"tar_shard_mb 必须大于 0: {tar_shard_mb!r}")
        creds, _ = self.cred_store.resolve(credentials)
        if creds is None:
            raise ValueError("缺少 R2 凭据；请先保存或传入凭据后再恢复任务")
        public_map = parse_public_urls(public_urls) if public_urls is not None else None

        with job.lock:
            prev_th = job.worker_thread
            prev_running = job.status in ("queued", "running")
            if prev_th is not None and prev_th.is_alive() and prev_running:
                return self._snapshot_job(job)
        if prev_th is not None and prev_th.is_alive() and prev_th is not threading.current_thread():
            prev_th.join(timeout=10.0)
        with job.lock:
            if job.worker_thread is not None and job.worker_thread.is_alive() and job.status in ("queued", "running"):
                return self._snapshot_job(job)
            job.stop_event.clear()
            job.pause_requested = False
            job.cancel_requested = False
            if cost_mode is not None:
                job.config["cost_mode"] = cost_mode
            if delete_source is not None:
                job.config["delete_source"] = bool(delete_source)
            if workers is not None:
                job.config["workers"] = max(1, int(workers))
            if writer_workers is not None:
                job.config["writer_workers"] = max(1, int(writer_workers))
            if max_free_class_a is not None:
                job.config["max_free_class_a"] = max(0, int(max_free_class_a))
            if max_free_class_b is not None:
                job.config["max_free_class_b"] = max(0, int(max_free_class_b))
            if overwrite is not None:
                job.config["overwrite"] = bool(overwrite)
            if public_map is not None:
                job.config["public_urls"] = public_map
            if storage_mode is not None:
                job.config["storage_mode"] = storage_mode
            if tar_shard_mb is not None:
                job.config["tar_shard_mb"] = max(1.0, float(tar_shard_mb))
            if consolidate_existing is not None:
                job.config["consolidate_existing"] = bool(consolidate_existing)
            job.config["resumed_from_checkpoint"] = True
            job.status = "queued"
            job.finished_at = None
            job.session_finished_at = None
            job.message = "从检查点恢复任务"
            job.updated_at = time.time()

        conn = self._ensure_job_db_open(job)
        with job.db_lock:
            # Retry previously failed objects on explicit resume.
            conn.execute("UPDATE objects SET status='pending', error='' WHERE status='failed'")
            if job.config.get("cost_mode") == "unlimited":
                conn.execute("UPDATE objects SET status='pending' WHERE status='ia_deferred'")
            conn.commit()
            self._refresh_counts_locked(job, conn)
        self._persist_job_summary(job)
        self._launch_job_thread(job, creds)
        return self._snapshot_job(job)

    def pause_job(self, job_id: str, *, wait: bool = True) -> dict[str, Any]:
        job = self._require_job(job_id)
        with job.lock:
            thread = job.worker_thread
            if job.status in ("queued", "running"):
                job.pause_requested = True
                job.stop_event.set()
                job.message = "正在安全暂停并保存检查点..."
        if wait and thread is not None and thread.is_alive():
            thread.join(timeout=60.0)
        return self.get_job(job_id)

    def cancel_job(self, job_id: str, *, wait: bool = True) -> dict[str, Any]:
        job = self._require_job(job_id)
        with job.lock:
            thread = job.worker_thread
            if job.status in ("queued", "running"):
                job.cancel_requested = True
                job.stop_event.set()
                job.message = "正在取消任务并保存检查点..."
            elif job.status not in ("completed",):
                job.status = "cancelled"
                job.finished_at = time.time()
                job.session_finished_at = job.finished_at
                job.message = "任务已取消"
        if wait and thread is not None and thread.is_alive():
            thread.join(timeout=60.0)
        if job.status != "running":
            self._persist_job_summary(job)
        return self.get_job(job_id)

    def _reconcile_missing_tar_shards(self, job: _RuntimeJob, conn: sqlite3.Connection) -> int:
        """Reset objects belonging to tar_shards missing/incomplete on Drive back to pending before cleanup."""
        with job.db_lock:
            rows = conn.execute("SELECT shard_name, rel_tar_path, tar_bytes FROM tar_shards").fetchall()
        if not rows:
            return 0
        missing: list[tuple[str, Path]] = []
        for sname, rel_tar, expected_sz in rows:
            tar_path = job.dest_root / str(rel_tar)
            exists_ok = False
            with contextlib.suppress(OSError):
                exists_ok = tar_path.is_file() and tar_path.stat().st_size == int(expected_sz)
            if not exists_ok:
                missing.append((str(sname), tar_path))
        if not missing:
            return 0

        reset_objs = 0
        for sname, tar_path in missing:
            with contextlib.suppress(OSError):
                tar_path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                tar_path.with_name(f"{tar_path.name}.manifest.jsonl").unlink(missing_ok=True)
            with job.db_lock:
                cur = conn.execute(
                    """
                    UPDATE objects SET status='pending', error=''
                    WHERE status IN ('copied', 'failed')
                      AND (bucket, key) IN (
                          SELECT bucket, key FROM archived_files
                          WHERE shard_name=? AND bucket != '' AND key != ''
                      )
                    """,
                    (sname,),
                )
                reset_objs += max(0, int(cur.rowcount or 0))
                conn.execute("DELETE FROM archived_files WHERE shard_name=?", (sname,))
                conn.execute("DELETE FROM tar_shards WHERE shard_name=?", (sname,))
                conn.commit()
        with job.db_lock:
            shard_row = conn.execute("SELECT COUNT(*), COALESCE(SUM(tar_bytes), 0) FROM tar_shards").fetchone()
            self._refresh_counts_locked(job, conn)
            self._refresh_buckets_locked(job, conn)
        with job.lock:
            job.tar_shards_count = int(shard_row[0] or 0) if shard_row else 0
            job.tar_bytes_total = int(shard_row[1] or 0) if shard_row else 0
        self._log(
            f"job {job.job_id}: reconciled {len(missing)} missing/incomplete TAR shards ({reset_objs} objects back to pending)"
        )
        return len(missing)

    def cleanup_job(
        self,
        job_id: str,
        *,
        credentials: dict[str, Any] | R2Credentials | None = None,
        background: bool = False,
    ) -> dict[str, Any]:
        """Verify Drive copies (including Cloud sync on fuse.drive) and batch-delete R2 originals (Free DeleteObjects)."""
        job = self._require_job(job_id)
        with job.lock:
            prev_th = job.worker_thread
            prev_running = job.status in ("queued", "running")
        if prev_th is not None and prev_th.is_alive():
            if prev_running:
                raise ValueError("任务仍在运行，请先暂停或等待完成后再执行 cleanup")
            if prev_th is not threading.current_thread():
                prev_th.join(timeout=10.0)
        with job.lock:
            if (job.worker_thread is not None and job.worker_thread.is_alive()) or job.status in ("queued", "running"):
                raise ValueError("任务仍在运行，请先暂停或等待完成后再执行 cleanup")
            prev_status = job.status
            job.stop_event.clear()
            job.pause_requested = False
            job.cancel_requested = False
            job.status = "running"
            job.finished_at = None
            job.files_done_session = 0
            job.bytes_done_session = 0
            job.session_started_at = time.time()
            job.session_finished_at = None
            job.message = "正在核验 Drive（含云端同步状态）并批量清理 R2 原文件..."
            job.updated_at = time.time()
        creds, _ = self.cred_store.resolve(credentials)
        if creds is None:
            with job.lock:
                job.status = prev_status
            raise ValueError("缺少 R2 凭据；请提供或先保存凭据")

        conn = self._ensure_job_db_open(job)
        with contextlib.suppress(OSError):
            self._persist_job_summary(job)

        if background:
            th = threading.Thread(
                target=self._run_cleanup_wrapper,
                args=(job, conn, creds, prev_status),
                name=f"r2-cleanup-{job.job_id}",
                daemon=True,
            )
            with job.lock:
                job.worker_thread = th
            th.start()
            return self._snapshot_job(job)

        return self._run_cleanup_wrapper(job, conn, creds, prev_status)

    def _run_cleanup_wrapper(
        self,
        job: _RuntimeJob,
        conn: sqlite3.Connection,
        creds: R2Credentials,
        prev_status: str,
    ) -> dict[str, Any]:
        client = R2Client(creds, ledger=self.ledger, timeout=self.client_timeout)
        deleted_total = 0
        failed_total = 0
        deferred_total = 0
        verifier = _DriveCloudSyncVerifier(job.dest_root, log=self._log)
        after_keys: dict[str, str] = {}
        skip_buckets: set[str] = set()
        transient_bucket_errors: dict[str, str] = {}
        last_log_mono = time.monotonic()
        try:
            if job.config.get("storage_mode") == "tar":
                self._reconcile_missing_tar_shards(job, conn)
            while not job.stop_event.is_set() and not self._shutdown:
                d_cnt, f_cnt, s_cnt = self._delete_copied_chunk(
                    job,
                    conn,
                    client,
                    batch_limit=10000,
                    after_keys=after_keys,
                    single_pass=True,
                    cloud_verifier=verifier,
                    delete_concurrency=32,
                    skip_buckets=skip_buckets,
                    transient_bucket_errors=transient_bucket_errors,
                )
                deleted_total += d_cnt
                failed_total += f_cnt
                deferred_total += s_cnt
                now_m = time.monotonic()
                if (now_m - last_log_mono) >= 30.0:
                    with job.db_lock:
                        self._refresh_buckets_locked(job, conn)
                    with job.lock:
                        job.message = (
                            f"正在核验 Drive 并清理 R2 原文件：已删 {deleted_total} 个"
                            + (f"，等待同步 {deferred_total} 个" if deferred_total else "")
                            + (f"，失败 {failed_total} 个" if failed_total else "")
                        )
                        job.updated_at = time.time()
                    self._log(
                        f"job {job.job_id} cleanup progress: deleted={deleted_total} deferred_sync={deferred_total} failed={failed_total}"
                    )
                    last_log_mono = now_m
                if d_cnt == 0 and f_cnt == 0 and s_cnt == 0:
                    break

            with job.db_lock:
                self._refresh_counts_locked(job, conn)
                self._refresh_buckets_locked(job, conn)
            counts = job.status_counts
            rem_pending = counts.get("pending", {}).get("count", 0)
            rem_ia = counts.get("ia_deferred", {}).get("count", 0)
            rem_copied = counts.get("copied", {}).get("count", 0)
            rem_failed = counts.get("failed", {}).get("count", 0)

            now_done = time.time()
            with job.lock:
                if job.cancel_requested:
                    job.status = "cancelled"
                    job.message = f"已取消清理任务（本次已清理 {deleted_total} 个 R2 原文件）"
                elif job.pause_requested or self._shutdown:
                    job.status = "paused_quota" if prev_status == "paused_quota" else "paused"
                    job.message = f"已暂停清理任务（本次已清理 {deleted_total} 个 R2 原文件）"
                elif failed_total > 0 or (transient_bucket_errors and deleted_total == 0):
                    job.status = "failed"
                    err_note = ""
                    if transient_bucket_errors:
                        err_note = "；R2 暂态超时桶: " + ", ".join(
                            f"{b} ({e})" for b, e in sorted(transient_bucket_errors.items())
                        )
                    job.message = (
                        f"已核验并清理 {deleted_total} 个 R2 原文件；"
                        f"{failed_total or rem_failed} 个核验或删除失败"
                        + (f"，{deferred_total} 个正等待 Drive 云端同步" if deferred_total else "")
                        + (f"，另有 {rem_pending} 个待迁移" if rem_pending else "")
                        + err_note
                    )
                elif deferred_total > 0 or rem_copied > 0:
                    job.status = "paused_quota"
                    extra_parts: list[str] = []
                    if rem_pending or rem_ia:
                        extra_parts.append(f"另有 {rem_pending + rem_ia} 个待迁移")
                    if rem_failed:
                        extra_parts.append(f"{rem_failed} 个失败待重试")
                    if transient_bucket_errors:
                        extra_parts.append(
                            "部分桶 R2 删源暂时超时待重试: " + ", ".join(sorted(transient_bucket_errors.keys()))
                        )
                    extra_suffix = ("（" + "；".join(extra_parts) + "）") if extra_parts else ""
                    job.message = (
                        f"已核验并清理 {deleted_total} 个 R2 原文件；"
                        f"剩余 {rem_copied} 个已拷贝对象正等待 Google Drive 云端同步 (750 GB/24h 限流)"
                        f"{extra_suffix}，同步完成后请再次执行 cleanup / resume {job.job_id}"
                    )
                elif rem_pending > 0 or rem_ia > 0:
                    job.status = (
                        prev_status
                        if prev_status in ("paused", "paused_budget", "paused_quota", "interrupted")
                        else "paused"
                    )
                    job.message = (
                        f"已核验并清理 {deleted_total} 个 R2 原文件；"
                        f"仍有 {rem_pending + rem_ia} 个对象待迁移"
                        + (f"（另有 {rem_failed} 个失败待重试）" if rem_failed else "")
                    )
                elif rem_failed > 0:
                    job.status = "failed"
                    job.message = f"已核验并清理 {deleted_total} 个 R2 原文件；仍有 {rem_failed} 个失败对象待重试"
                else:
                    job.status = "completed"
                    job.message = f"已核验 Drive 并清理 {deleted_total} 个 R2 原文件"
                job.finished_at = now_done
                job.session_finished_at = now_done
            self._flush_checkpoint(job, conn)
            return self.get_job(job.job_id)
        except Exception as err:
            self._log(f"job {job.job_id} cleanup error: {err}")
            now_done = time.time()
            with job.lock:
                job.recent_errors.append({"key": "", "error": str(err)})
                job.status = "failed"
                job.message = f"R2 清理失败（已删 {deleted_total} 个）: {err}"
                job.finished_at = now_done
                job.session_finished_at = now_done
            with contextlib.suppress(Exception):
                self._flush_checkpoint(job, conn)
            return self.get_job(job.job_id)
        finally:
            client.close()
            self.ledger.flush(ignore_errors=True)

    def shutdown(self) -> None:
        self._shutdown = True
        with self._mgr_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            with job.lock:
                if job.status in ("queued", "running"):
                    job.pause_requested = True
                    job.stop_event.set()
        for job in jobs:
            th = job.worker_thread
            if th is not None and th.is_alive():
                th.join(timeout=60.0)
            with job.db_lock:
                if job.conn is not None:
                    with contextlib.suppress(Exception):
                        job.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    with contextlib.suppress(Exception):
                        job.conn.close()
                    job.conn = None
        self.ledger.flush(ignore_errors=True)

    def _launch_job_thread(self, job: _RuntimeJob, creds: R2Credentials) -> None:
        th = threading.Thread(
            target=self._run_job_wrapper,
            args=(job, creds),
            name=f"r2-job-{job.job_id}",
            daemon=True,
        )
        with job.lock:
            job.worker_thread = th
        th.start()

    def _run_job_wrapper(self, job: _RuntimeJob, creds: R2Credentials) -> None:
        client = R2Client(creds, ledger=self.ledger, timeout=self.client_timeout)
        conn: sqlite3.Connection | None = None
        try:
            conn = self._ensure_job_db_open(job)
            self._run_job(job, conn, client)
        except _BudgetPaused as pause_err:
            self._log(f"job {job.job_id} paused on free-tier cap: {pause_err.message}")
            now_wall = time.time()
            with job.lock:
                job.status = "paused_budget"
                job.message = pause_err.message
                job.finished_at = now_wall
                job.session_finished_at = now_wall
            if conn is not None:
                with contextlib.suppress(Exception):
                    self._flush_checkpoint(job, conn)
            else:
                with contextlib.suppress(OSError):
                    self._persist_job_summary(job)
        except _QuotaPaused as quota_err:
            self._log(f"job {job.job_id} paused on Drive quota/cache guard: {quota_err.message}")
            now_wall = time.time()
            with job.lock:
                job.status = "paused_quota"
                job.message = quota_err.message
                job.finished_at = now_wall
                job.session_finished_at = now_wall
            if conn is not None:
                with contextlib.suppress(Exception):
                    self._flush_checkpoint(job, conn)
            else:
                with contextlib.suppress(OSError):
                    self._persist_job_summary(job)
        except Exception as err:
            self._log(f"job {job.job_id} error: {err}")
            now_wall = time.time()
            with job.lock:
                job.recent_errors.append({"key": "", "error": str(err)})
                job.status = "failed"
                job.message = f"任务失败: {err}"
                job.finished_at = now_wall
                job.session_finished_at = now_wall
            if conn is not None:
                with contextlib.suppress(Exception):
                    self._flush_checkpoint(job, conn)
            else:
                with contextlib.suppress(OSError):
                    self._persist_job_summary(job)
        finally:
            client.close()
            self.ledger.flush(ignore_errors=True)
            with contextlib.suppress(OSError):
                if job.spool_dir.is_dir() and not any(job.spool_dir.iterdir()):
                    job.spool_dir.rmdir()

    def _consolidate_existing_drive_files(
        self,
        job: _RuntimeJob,
        conn: sqlite3.Connection,
        tar_pipeline: _TarArchivePipeline,
        *,
        include_bucket: bool,
        spool_budget: ByteBudgetSemaphore,
        is_stopping: Callable[[], bool],
        on_archived_transition: Callable[[dict[str, tuple[int, int]], int, int], None],
    ) -> None:
        """Scan loose files already stored under `dest_root`, pack verified files into TAR shards,
        unlink corrupt partial downloads, and prune empty directories only after Drive flush + DB commit."""
        dest_root = job.dest_root
        if not dest_root.is_dir():
            return
        archives_name = "archives"
        skip_roots: list[Path] = []
        for extra in (self.data_dir, self.work_dir):
            with contextlib.suppress(OSError):
                skip_roots.append(extra.resolve())

        def _is_skipped_dir(p: Path) -> bool:
            if p.parent == dest_root and p.name == archives_name:
                return True
            with contextlib.suppress(OSError):
                rp = p.resolve()
                for sr in skip_roots:
                    if rp == sr or sr in rp.parents:
                        return True
                    # Do not recurse into data_dir/work_dir if placed inside dest_root.
                    if rp == sr or rp.is_relative_to(sr):
                        return True
            return False

        # Quick check: skip thread pools completely when no loose items exist outside archives/.
        has_non_archive = False
        try:
            with os.scandir(dest_root) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    p = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if not _is_skipped_dir(p):
                            has_non_archive = True
                            break
                    elif entry.is_file(follow_symlinks=False):
                        if _is_temp_artifact_name(entry.name):
                            with contextlib.suppress(OSError):
                                p.unlink(missing_ok=True)
                        else:
                            has_non_archive = True
                            break
        except OSError:
            return
        if not has_non_archive:
            return

        with job.lock:
            job.message = "正在整理 Drive 已有散列文件并打包为 TAR 分卷..."
            job.updated_at = time.time()

        with job.db_lock:
            bucket_names = [str(r[0]) for r in conn.execute("SELECT bucket FROM buckets ORDER BY bucket").fetchall()]
        single_bucket = bucket_names[0] if len(bucket_names) == 1 else ""

        prefetch_budget = ByteBudgetSemaphore(_PREFETCH_BUDGET_BYTES)
        shard_verify_cache: dict[str, bool] = {}
        shard_verify_lock = threading.Lock()
        last_ckpt_m = time.monotonic()

        def _verify_archived_shard(shard_name: str, rel_tar_path: str, tar_bytes: int) -> bool:
            with shard_verify_lock:
                cached = shard_verify_cache.get(shard_name)
            if cached is not None:
                return cached
            ok = False
            with contextlib.suppress(OSError):
                tp = dest_root / rel_tar_path
                ok = tp.is_file() and tp.stat().st_size == int(tar_bytes)
            with shard_verify_lock:
                shard_verify_cache[shard_name] = ok
            return ok

        def _derive_shard_bucket(rel_path: str, db_bucket: str) -> str:
            if db_bucket:
                return db_bucket
            if not include_bucket and single_bucket:
                return single_bucket
            if "/" in rel_path:
                first = rel_path.split("/", 1)[0]
                if first and first != "__r2_root__":
                    return first
            return single_bucket or "drive-existing"

        def _prefetch_one(
            rel_path: str,
            full_path: Path,
            db_row: tuple[str, str, int, int, str] | None,
            arch_row: tuple[int, str, str, int] | None,
        ) -> None:
            if is_stopping():
                return
            try:
                st_res = full_path.stat()
                actual_size = int(st_res.st_size)
            except OSError as exc:
                with job.lock:
                    job.recent_errors.append({"key": rel_path, "error": f"Drive 整理 stat 失败: {exc}"})
                return

            # Case 1: Already stored in an existing verified .tar shard on Drive.
            if arch_row is not None:
                arch_size, shard_name, rel_tar_path, tar_bytes = arch_row
                if actual_size == int(arch_size) and _verify_archived_shard(shard_name, rel_tar_path, tar_bytes):
                    if db_row is not None:
                        db_b, db_k, db_sz, _, db_st = db_row
                        if db_st in ("pending", "ia_deferred", "failed"):
                            now_ts = time.time()
                            with job.db_lock:
                                conn.execute(
                                    "UPDATE objects SET status='copied', error='', updated_at=? WHERE bucket=? AND key=? AND status IN ('pending','ia_deferred','failed')",
                                    (now_ts, db_b, db_k),
                                )
                                conn.commit()
                            deltas = {
                                db_st: (-1, -int(db_sz)),
                                "copied": (1, int(db_sz)),
                            }
                            on_archived_transition(deltas, 1, actual_size)
                            self.ledger.record_free(skipped_existing=1, bytes_transferred=actual_size)
                    with contextlib.suppress(OSError):
                        full_path.unlink(missing_ok=True)
                    return

            # Case 2: Incomplete / corrupted partial loose file waiting for retry/pending download.
            if db_row is not None:
                db_b, db_k, db_sz, db_is_dir, db_st = db_row
                if not db_is_dir and db_st in ("pending", "ia_deferred", "failed") and actual_size != int(db_sz):
                    with contextlib.suppress(OSError):
                        full_path.unlink(missing_ok=True)
                    return
                if db_st in ("pending", "ia_deferred", "failed"):
                    if actual_size == 0:
                        self.ledger.record_free(zero_byte=1)
                    else:
                        self.ledger.record_free(skipped_existing=1, bytes_transferred=actual_size)
            else:
                db_b, db_k = "", ""

            shard_bucket = _derive_shard_bucket(rel_path, db_b)
            held = 0
            spool_tmp: Path | None = None
            submitted = False
            try:
                if actual_size == 0:
                    tar_pipeline.submit(
                        _ArchiveTask(
                            bucket=db_b,
                            key=db_k,
                            shard_bucket=shard_bucket,
                            rel_path=rel_path,
                            size=0,
                            is_dir=False,
                            source="drive_existing",
                            payload_bytes=b"",
                            loose_path=full_path,
                        )
                    )
                    submitted = True
                elif actual_size <= _PREFETCH_MEM_MAX_FILE:
                    held = prefetch_budget.acquire(actual_size, stop_event=job.stop_event)
                    data = full_path.read_bytes()
                    if len(data) != actual_size:
                        raise OSError(
                            f"Drive pre-pack read mismatch for {full_path}: expected {actual_size}B, read {len(data)}B"
                        )
                    tar_pipeline.submit(
                        _ArchiveTask(
                            bucket=db_b,
                            key=db_k,
                            shard_bucket=shard_bucket,
                            rel_path=rel_path,
                            size=actual_size,
                            is_dir=False,
                            source="drive_existing",
                            payload_bytes=data,
                            held_budget=held,
                            release_fn=prefetch_budget.release,
                            loose_path=full_path,
                        )
                    )
                    submitted = True
                else:
                    held = spool_budget.acquire(actual_size, stop_event=job.stop_event)
                    spool_tmp = job.spool_dir / f"cons.{uuid.uuid4().hex}.spool"
                    copied = 0
                    with full_path.open("rb") as src_f, spool_tmp.open("wb") as dst_f:
                        while True:
                            buf = src_f.read(4 * 1024 * 1024)
                            if not buf:
                                break
                            dst_f.write(buf)
                            copied += len(buf)
                    if copied != actual_size:
                        raise OSError(
                            f"Drive stage copy mismatch for {full_path}: expected {actual_size}B, copied {copied}B"
                        )
                    tar_pipeline.submit(
                        _ArchiveTask(
                            bucket=db_b,
                            key=db_k,
                            shard_bucket=shard_bucket,
                            rel_path=rel_path,
                            size=actual_size,
                            is_dir=False,
                            source="drive_existing",
                            source_path=spool_tmp,
                            unlink_source_after_pack=True,
                            held_budget=held,
                            release_fn=spool_budget.release,
                            loose_path=full_path,
                        )
                    )
                    submitted = True
            except _JobStopped:
                pass
            except Exception as exc:
                with job.lock:
                    job.recent_errors.append({"key": rel_path, "error": f"Drive 整理读取失败: {exc}"})
            finally:
                if not submitted:
                    if spool_tmp is not None:
                        with contextlib.suppress(OSError):
                            spool_tmp.unlink(missing_ok=True)
                    if held:
                        if spool_tmp is not None:
                            spool_budget.release(held)
                        else:
                            prefetch_budget.release(held)

        def _flush_batch(
            file_batch: list[tuple[str, Path]],
            dir_batch: list[tuple[str, Path]],
            prefetch_pool: concurrent.futures.ThreadPoolExecutor,
            futs: list[concurrent.futures.Future[None]],
        ) -> None:
            nonlocal last_ckpt_m
            if dir_batch:
                dir_rels = [r for r, _ in dir_batch]
                with job.db_lock:
                    q_marks = ",".join("?" for _ in dir_rels)
                    dir_rows = conn.execute(
                        f"SELECT rel_path, bucket, key FROM objects WHERE is_dir = 1 AND rel_path IN ({q_marks})",
                        dir_rels,
                    ).fetchall()
                    arch_dirs = {
                        str(r[0])
                        for r in conn.execute(
                            f"SELECT rel_path FROM archived_files WHERE rel_path IN ({q_marks})",
                            dir_rels,
                        ).fetchall()
                    }
                dir_db_map = {str(r[0]): (str(r[1]), str(r[2])) for r in dir_rows}
                dir_path_map = dict(dir_batch)
                for r_dir, (db_b, db_k) in dir_db_map.items():
                    if r_dir in arch_dirs:
                        continue
                    tar_pipeline.submit(
                        _ArchiveTask(
                            bucket=db_b,
                            key=db_k,
                            shard_bucket=_derive_shard_bucket(r_dir, db_b),
                            rel_path=r_dir,
                            size=0,
                            is_dir=True,
                            source="drive_existing",
                            loose_path=dir_path_map[r_dir],
                        )
                    )
                dir_batch.clear()

            if file_batch:
                rels = [r for r, _ in file_batch]
                obj_map: dict[str, tuple[str, str, int, int, str]] = {}
                arch_map: dict[str, tuple[int, str, str, int]] = {}
                with job.db_lock:
                    q_marks = ",".join("?" for _ in rels)
                    for rp, b, k, sz, is_d, st in conn.execute(
                        f"SELECT rel_path, bucket, key, size, is_dir, status FROM objects WHERE rel_path IN ({q_marks})",
                        rels,
                    ).fetchall():
                        obj_map[str(rp)] = (str(b), str(k), int(sz), int(is_d), str(st))
                    for rp, asz, sname, rtar, tbytes in conn.execute(
                        f"""
                        SELECT a.rel_path, a.size, a.shard_name, t.rel_tar_path, t.tar_bytes
                        FROM archived_files a
                        JOIN tar_shards t ON a.shard_name = t.shard_name
                        WHERE a.rel_path IN ({q_marks})
                        """,
                        rels,
                    ).fetchall():
                        arch_map[str(rp)] = (int(asz), str(sname), str(rtar), int(tbytes))

                for rpath, fpath in file_batch:
                    if is_stopping():
                        break
                    futs.append(
                        prefetch_pool.submit(
                            _prefetch_one,
                            rpath,
                            fpath,
                            obj_map.get(rpath),
                            arch_map.get(rpath),
                        )
                    )
                    if len(futs) >= 256:
                        done, not_done = concurrent.futures.wait(
                            futs, return_when=concurrent.futures.FIRST_COMPLETED
                        )
                        for df in done:
                            with contextlib.suppress(Exception):
                                df.result()
                        futs[:] = list(not_done)
                file_batch.clear()

            now_m = time.monotonic()
            if (now_m - last_ckpt_m) >= self.checkpoint_interval:
                self._flush_checkpoint(job, conn)
                last_ckpt_m = time.monotonic()
                with job.lock:
                    job.message = (
                        f"正在打包 Drive 已有文件: {job.consolidated_files} 个 ({job.tar_shards_count} 个 TAR 分卷)"
                    )

        visited_dirs: list[Path] = []
        file_batch: list[tuple[str, Path]] = []
        dir_batch: list[tuple[str, Path]] = []
        pending_futs: list[concurrent.futures.Future[None]] = []

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=24, thread_name_prefix=f"r2-cons-{job.job_id}"
        ) as prefetch_pool:
            dir_stack: list[Path] = [dest_root]
            while dir_stack and not is_stopping():
                cur_dir = dir_stack.pop()
                try:
                    with os.scandir(cur_dir) as it:
                        entries = list(it)
                except OSError:
                    continue
                for entry in entries:
                    if is_stopping():
                        break
                    if entry.is_symlink():
                        continue
                    ep = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if _is_skipped_dir(ep):
                            continue
                        visited_dirs.append(ep)
                        rel_d = str(ep.relative_to(dest_root)).replace("\\", "/")
                        dir_batch.append((rel_d, ep))
                        dir_stack.append(ep)
                    elif entry.is_file(follow_symlinks=False):
                        if _is_temp_artifact_name(entry.name):
                            with contextlib.suppress(OSError):
                                ep.unlink(missing_ok=True)
                            continue
                        rel_f = str(ep.relative_to(dest_root)).replace("\\", "/")
                        file_batch.append((rel_f, ep))
                    if len(file_batch) >= 400 or len(dir_batch) >= 400:
                        _flush_batch(file_batch, dir_batch, prefetch_pool, pending_futs)
            _flush_batch(file_batch, dir_batch, prefetch_pool, pending_futs)
            for df in pending_futs:
                with contextlib.suppress(Exception):
                    df.result()

        # Seal & flush consolidation shards to Drive and finish loose-file unlinks before pruning empty dirs.
        tar_pipeline.flush_open_shards(wait=True)
        for d in sorted(visited_dirs, key=lambda p: len(p.parts), reverse=True):
            with contextlib.suppress(OSError):
                d.rmdir()

    def _run_job(self, job: _RuntimeJob, conn: sqlite3.Connection, client: R2Client) -> None:
        cfg = job.config
        cost_mode = str(cfg["cost_mode"])
        max_free_a = int(cfg["max_free_class_a"])
        max_free_b = int(cfg["max_free_class_b"])
        workers = int(cfg["workers"])
        writer_workers = int(cfg["writer_workers"])
        overwrite = bool(cfg["overwrite"])
        delete_source = bool(cfg["delete_source"])
        storage_mode = str(cfg.get("storage_mode") or "files")
        tar_shard_mb = float(cfg.get("tar_shard_mb") or DEFAULT_TAR_SHARD_MB)
        consolidate_existing = bool(cfg.get("consolidate_existing", True))
        prefix = str(cfg.get("prefix") or "")
        public_urls: dict[str, str] = dict(cfg.get("public_urls") or {})

        now_wall = time.time()
        with job.lock:
            job.status = "running"
            job.started_at = job.started_at or now_wall
            job.finished_at = None
            job.session_started_at = now_wall
            job.session_finished_at = None
            job.files_done_session = 0
            job.bytes_done_session = 0
            job.message = "正在运行"
            job.updated_at = now_wall
        job.spool_dir.mkdir(parents=True, exist_ok=True)
        # Clear any orphan NVMe temp files left by a killed worker before restarting.
        for stale in list(job.spool_dir.iterdir()):
            if stale.is_file():
                with contextlib.suppress(OSError):
                    stale.unlink(missing_ok=True)
        job.dest_root.mkdir(parents=True, exist_ok=True)

        # Step A: discover buckets if the buckets table is still empty.
        with job.db_lock:
            existing_buckets = [r[0] for r in conn.execute("SELECT bucket FROM buckets ORDER BY bucket").fetchall()]
            flatten_meta = conn.execute("SELECT v FROM job_meta WHERE k='include_bucket'").fetchone()

        if not existing_buckets:
            requested = list(cfg.get("buckets") or [])
            excluded = set(cfg.get("exclude_buckets") or [])
            if requested:
                selected = [b for b in requested if b not in excluded]
            else:
                allowed, paid_a = self.ledger.can_spend_class_a(cost_mode, max_free_a)
                if not allowed:
                    raise _BudgetPaused(f"免费 Class A 配额上限已达 ({max_free_a})，ListBuckets 已暂停")
                if paid_a:
                    with job.lock:
                        job.paid_fallback_used = True
                discovered = [b["name"] for b in client.list_buckets()]
                selected = [b for b in discovered if b not in excluded]
            if not selected:
                raise ValueError("未匹配到任何可迁移的 R2 Bucket")
            if cfg.get("flatten_single_bucket") and len(selected) != 1:
                raise ValueError(f"--flatten-single-bucket 仅支持单个桶，当前匹配 {len(selected)} 个: {selected}")
            include_bucket = not bool(cfg.get("flatten_single_bucket") and len(selected) == 1)
            with job.db_lock:
                conn.executemany("INSERT OR IGNORE INTO buckets(bucket) VALUES (?)", [(b,) for b in selected])
                conn.execute(
                    "INSERT OR REPLACE INTO job_meta(k, v) VALUES ('include_bucket', ?)",
                    ("1" if include_bucket else "0",),
                )
                conn.commit()
                self._refresh_counts_locked(job, conn)
                self._refresh_buckets_locked(job, conn)
        else:
            include_bucket = (flatten_meta[0] != "0") if flatten_meta else not bool(
                cfg.get("flatten_single_bucket") and len(existing_buckets) == 1
            )
            with job.db_lock:
                self._refresh_counts_locked(job, conn)
                self._refresh_buckets_locked(job, conn)

        self._persist_job_summary(job)

        dir_cache = DriveDirCache(job.dest_root)
        spool_budget = ByteBudgetSemaphore(self.spool_bytes_cap)
        is_fuse_drive = _detect_fuse_drive_mydrive(job.dest_root) is not None
        work_cond = threading.Condition()
        budget_pause_msg: list[str] = []
        quota_pause_msg: list[str] = []
        lister_error: list[str] = []
        internal_stop = threading.Event()
        listing_finished = threading.Event()
        delete_wake = threading.Event()
        stop_deleter = threading.Event()

        dispatched_keys: set[tuple[str, str]] = set()
        active_dl: set[tuple[str, str]] = set()
        in_flight_lock = threading.Lock()
        in_flight_writers = [0]
        outcomes_q: queue.Queue[tuple[str, str, str, str, str, int, str]] = queue.Queue()
        relocate_q: queue.Queue[tuple[str, str]] = queue.Queue()
        relocated_paths: dict[str, str] = {}
        write_q: queue.Queue[tuple[str, str, str, int, Path, int] | None] = queue.Queue(maxsize=max(16, workers * 2))

        def trigger_budget_pause(msg: str) -> None:
            if not budget_pause_msg:
                budget_pause_msg.append(msg)
            internal_stop.set()
            job.stop_event.set()
            with work_cond:
                work_cond.notify_all()

        def trigger_quota_pause(msg: str) -> None:
            if not quota_pause_msg:
                quota_pause_msg.append(msg)
            internal_stop.set()
            job.stop_event.set()
            with work_cond:
                work_cond.notify_all()

        def is_stopping() -> bool:
            return job.stop_event.is_set() or internal_stop.is_set()

        def record_relocate(old_rel: str, new_rel: str) -> None:
            relocate_q.put((old_rel, new_rel))

        # --- Lister thread ---
        def lister_loop() -> None:
            try:
                while not is_stopping():
                    with job.db_lock:
                        row = conn.execute(
                            "SELECT bucket, last_key FROM buckets WHERE listing_done = 0 ORDER BY bucket LIMIT 1"
                        ).fetchone()
                    if row is None:
                        return
                    bucket, cursor_key = str(row[0]), str(row[1] or "")
                    while not is_stopping():
                        buffered = (
                            job.status_counts.get("pending", {}).get("count", 0)
                            + job.status_counts.get("ia_deferred", {}).get("count", 0)
                        )
                        if buffered >= MAX_LISTED_BUFFER:
                            time.sleep(0.5)
                            continue

                        allowed, paid_a = self.ledger.can_spend_class_a(cost_mode, max_free_a)
                        if not allowed:
                            trigger_budget_pause(
                                f"已达到 free-only Class A 上限 ({max_free_a:,})，列表在 {bucket}/{cursor_key} 处安全暂停"
                            )
                            return
                        if paid_a:
                            with job.lock:
                                if not job.paid_fallback_used:
                                    job.paid_fallback_used = True
                                    self._log(f"job {job.job_id}: Class A 已超出免费额度，自动切换付费模式继续列举")

                        page = client.list_objects_v2(
                            bucket,
                            prefix=prefix,
                            start_after=cursor_key,
                            max_keys=self.page_size,
                        )
                        now_ts = time.time()
                        rows_to_insert: list[tuple[Any, ...]] = []
                        for entry in page.objects:
                            rel_path, is_dir = map_object_to_drive_relpath(
                                bucket, entry.key, size=entry.size, include_bucket=include_bucket
                            )
                            if (
                                not is_dir
                                and entry.size > 0
                                and entry.storage_class == "STANDARD_IA"
                                and cost_mode in ("free-first", "free-only")
                            ):
                                st_val = "ia_deferred"
                            else:
                                st_val = "pending"
                            rows_to_insert.append(
                                (
                                    bucket,
                                    entry.key,
                                    entry.size,
                                    entry.etag,
                                    entry.storage_class,
                                    rel_path,
                                    1 if is_dir else 0,
                                    st_val,
                                    "",
                                    now_ts,
                                )
                            )
                        new_cursor = page.last_key or cursor_key
                        done_flag = 1 if (not page.is_truncated or not page.objects) else 0
                        list_deltas: dict[str, tuple[int, int]] = {}
                        with job.db_lock:
                            if rows_to_insert:
                                existing_keys: set[str] = set()
                                keys_page = [r[1] for r in rows_to_insert]
                                for k_idx in range(0, len(keys_page), 500):
                                    k_chunk = keys_page[k_idx : k_idx + 500]
                                    q_marks = ",".join("?" for _ in k_chunk)
                                    existing_keys.update(
                                        str(r[0])
                                        for r in conn.execute(
                                            f"SELECT key FROM objects WHERE bucket = ? AND key IN ({q_marks})",
                                            [bucket, *k_chunk],
                                        ).fetchall()
                                    )
                                new_rows = [r for r in rows_to_insert if r[1] not in existing_keys]
                                conn.executemany(
                                    """
                                    INSERT OR IGNORE INTO objects
                                    (bucket, key, size, etag, storage_class, rel_path, is_dir, status, error, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    rows_to_insert,
                                )
                                added_cnt = len(new_rows)
                                added_bytes = sum(int(r[2]) for r in new_rows)
                                for nr in new_rows:
                                    st_k = str(nr[7])
                                    dc, db = list_deltas.get(st_k, (0, 0))
                                    list_deltas[st_k] = (dc + 1, db + int(nr[2]))
                            else:
                                added_cnt = 0
                                added_bytes = 0
                            conn.execute(
                                """
                                UPDATE buckets
                                SET last_key = ?, listing_done = ?,
                                    objects_listed = objects_listed + ?,
                                    bytes_listed = bytes_listed + ?
                                WHERE bucket = ?
                                """,
                                (new_cursor, done_flag, added_cnt, added_bytes, bucket),
                            )
                            conn.commit()
                        if list_deltas:
                            self._adjust_status_counts(job, list_deltas)
                        cursor_key = new_cursor
                        with work_cond:
                            work_cond.notify_all()
                        if done_flag:
                            break
            except Exception as exc:
                self._log(f"job {job.job_id} lister error: {exc}")
                lister_error.append(str(exc))
                with job.lock:
                    job.recent_errors.append({"key": "", "error": f"列举失败: {exc}"})
                internal_stop.set()
                job.stop_event.set()
            finally:
                listing_finished.set()
                with work_cond:
                    work_cond.notify_all()

        # --- Background R2 DeleteObjects thread ---
        def deleter_loop() -> None:
            del_after_keys: dict[str, str] = {}
            del_verifier = _DriveCloudSyncVerifier(job.dest_root, log=self._log)
            while not stop_deleter.is_set() and not is_stopping():
                deferred_cycle = 0
                deleted_cycle = 0
                try:
                    with job.lock:
                        copied_total = job.status_counts.get("copied", {}).get("count", 0)
                    if copied_total >= 1000:
                        with job.db_lock:
                            b_names = [
                                str(r[0])
                                for r in conn.execute("SELECT bucket FROM buckets ORDER BY bucket").fetchall()
                            ]
                            ready_buckets: list[str] = []
                            for b_name in b_names:
                                ak = del_after_keys.get(b_name, "")
                                if (
                                    conn.execute(
                                        "SELECT 1 FROM objects WHERE bucket=? AND status='copied' AND key > ? "
                                        "ORDER BY key LIMIT 1 OFFSET 999",
                                        (b_name, ak),
                                    ).fetchone()
                                    is not None
                                ):
                                    ready_buckets.append(b_name)
                                elif ak:
                                    del_after_keys[b_name] = ""
                        for r_b in ready_buckets:
                            if stop_deleter.is_set() or is_stopping():
                                break
                            d_cnt, _, s_cnt = self._delete_copied_chunk(
                                job,
                                conn,
                                client,
                                bucket_filter=r_b,
                                batch_limit=1000,
                                after_keys=del_after_keys,
                                single_pass=False,
                                cloud_verifier=del_verifier,
                            )
                            deleted_cycle += d_cnt
                            deferred_cycle += s_cnt
                except Exception as exc:
                    self._log(f"job {job.job_id}: background deleter transient error (retrying): {exc}")
                    delete_wake.wait(timeout=3.0)
                    delete_wake.clear()
                    continue
                wait_sec = 5.0 if (deferred_cycle > 0 and deleted_cycle == 0) else 1.5
                delete_wake.wait(timeout=wait_sec)
                delete_wake.clear()

        # --- DriveFS direct writer workers (files mode) ---
        def writer_loop() -> None:
            while True:
                item = write_q.get()
                if item is None:
                    write_q.task_done()
                    return
                bucket, key, rel_path, size, spool_tmp, budget_held = item
                try:
                    actual_rel = dir_cache.write_file_direct(
                        rel_path, spool_tmp, size, on_relocate=record_relocate
                    )
                    outcomes_q.put((bucket, key, actual_rel, "copied", "", size, "pending"))
                except OSError as exc:
                    if exc.errno == errno.ENOSPC or "No space left on device" in str(exc):
                        trigger_quota_pause(
                            "Google Drive 挂载或本地缓存空间不足（可能已触发单账号 750 GB / 24h 上传限流 userRateLimitExceeded）；"
                            "任务已安全暂停，待云端后台上传释放缓存后执行 resume 继续"
                        )
                        outcomes_q.put((bucket, key, rel_path, "pending", "", size, "pending"))
                    else:
                        outcomes_q.put((bucket, key, rel_path, "failed", f"Drive write failed: {exc}", size, "pending"))
                except Exception as exc:
                    outcomes_q.put((bucket, key, rel_path, "failed", f"Drive write failed: {exc}", size, "pending"))
                finally:
                    with contextlib.suppress(OSError):
                        spool_tmp.unlink(missing_ok=True)
                    spool_budget.release(budget_held)
                    with in_flight_lock:
                        in_flight_writers[0] -= 1
                    write_q.task_done()
                    with work_cond:
                        work_cond.notify_all()

        # --- TAR pipeline callbacks (tar mode) ---
        def on_tar_committed(
            entries: list[_ArchivedEntry],
            tar_bytes: int,
            deltas: dict[str, tuple[int, int]],
            cons_cnt: int,
            cons_bytes: int,
            sess_cnt: int,
            sess_bytes: int,
        ) -> None:
            self._adjust_status_counts(job, deltas)
            if tar_bytes > 0 or cons_cnt or sess_cnt:
                with job.lock:
                    if tar_bytes > 0:
                        job.tar_shards_count += 1
                        job.tar_bytes_total += int(tar_bytes)
                    job.consolidated_files += cons_cnt
                    job.consolidated_bytes += cons_bytes
                    job.files_done_session += sess_cnt
                    job.bytes_done_session += sess_bytes
            with in_flight_lock:
                for e in entries:
                    if e.bucket and e.key:
                        dispatched_keys.discard((e.bucket, e.key))
            if deltas.get("copied", (0, 0))[0] > 0:
                delete_wake.set()
            with work_cond:
                work_cond.notify_all()

        def on_tar_failed(entries: list[_ArchivedEntry], err_msg: str) -> None:
            if not err_msg:
                with in_flight_lock:
                    for e in entries:
                        if e.bucket and e.key:
                            dispatched_keys.discard((e.bucket, e.key))
                with work_cond:
                    work_cond.notify_all()
                return
            now_ts = time.time()
            upd: list[tuple[str, float, str, str]] = []
            deltas: dict[str, tuple[int, int]] = {}
            for e in entries:
                if e.bucket and e.key:
                    upd.append((err_msg, now_ts, e.bucket, e.key))
                    if e.source == "r2_download":
                        dc, db = deltas.get("pending", (0, 0))
                        deltas["pending"] = (dc - 1, db - int(e.size))
                        fc, fb = deltas.get("failed", (0, 0))
                        deltas["failed"] = (fc + 1, fb + int(e.size))
            if upd:
                with job.db_lock:
                    conn.executemany(
                        "UPDATE objects SET status='failed', error=?, updated_at=? WHERE bucket=? AND key=?",
                        upd,
                    )
                    conn.commit()
                self._adjust_status_counts(job, deltas)
            with job.lock:
                job.recent_errors.append({"key": entries[0].orig_rel_path if entries else "", "error": err_msg})
            with in_flight_lock:
                for e in entries:
                    if e.bucket and e.key:
                        dispatched_keys.discard((e.bucket, e.key))
            with work_cond:
                work_cond.notify_all()

        def on_cons_archived_transition(
            deltas: dict[str, tuple[int, int]], sess_cnt: int, sess_bytes: int
        ) -> None:
            self._adjust_status_counts(job, deltas)
            with job.lock:
                job.files_done_session += sess_cnt
                job.bytes_done_session += sess_bytes
            if deltas.get("copied", (0, 0))[0] > 0:
                delete_wake.set()

        tar_pipeline: _TarArchivePipeline | None = None
        writer_threads: list[threading.Thread] = []
        if storage_mode == "tar":
            tar_pipeline = _TarArchivePipeline(
                job=job,
                conn=conn,
                tar_shard_mb=tar_shard_mb,
                on_committed=on_tar_committed,
                on_failed=on_tar_failed,
                on_quota_pause=trigger_quota_pause,
                log=self._log,
            )
            with job.db_lock:
                self._refresh_counts_locked(job, conn)
        else:
            writer_threads = [
                threading.Thread(target=writer_loop, name=f"r2-writer-{job.job_id}-{i}", daemon=True)
                for i in range(writer_workers)
            ]
            for wt in writer_threads:
                wt.start()

        lister_thread = threading.Thread(target=lister_loop, name=f"r2-lister-{job.job_id}", daemon=True)
        lister_thread.start()

        deleter_thread: threading.Thread | None = None
        if delete_source:
            deleter_thread = threading.Thread(target=deleter_loop, name=f"r2-del-{job.job_id}", daemon=True)
            deleter_thread.start()

        # --- Downloader worker function ---
        def process_object(row: tuple[str, str, int, str, str, str, int]) -> None:
            bucket, key, size, etag, sc, rel_path, is_dir = row
            handed_off = False
            spool_tmp: Path | None = None
            held = 0
            try:
                if is_stopping():
                    return

                if storage_mode == "tar":
                    assert tar_pipeline is not None
                    # 1. Pre-check if consolidated / previously archived into a TAR shard (0 Class B ops!)
                    if not overwrite:
                        found = tar_pipeline.find_archived(rel_path, None if is_dir else size)
                        if found is not None:
                            arch_rel, arch_sz, f_b, _ = found
                            if not f_b:
                                tar_pipeline.backfill_archived_bk(arch_rel, bucket, key)
                            if is_dir or size == 0:
                                self.ledger.record_free(zero_byte=1)
                            else:
                                self.ledger.record_free(skipped_existing=1, bytes_transferred=size)
                            outcomes_q.put((bucket, key, arch_rel, "copied", "", size, "pending"))
                            handed_off = True
                            return
                        if not consolidate_existing and not is_dir:
                            actual_rel, existing_sz = dir_cache.preexisting_file_size(rel_path)
                            if existing_sz is not None and existing_sz == size:
                                if size == 0:
                                    self.ledger.record_free(zero_byte=1)
                                else:
                                    self.ledger.record_free(skipped_existing=1, bytes_transferred=size)
                                outcomes_q.put((bucket, key, actual_rel, "copied", "", size, "pending"))
                                handed_off = True
                                return

                    # 2. Directory markers & 0-byte files pack straight into TAR (0 Class B ops, 0 FUSE ops!)
                    if is_dir:
                        self.ledger.record_free(zero_byte=1)
                        tar_pipeline.submit(
                            _ArchiveTask(
                                bucket=bucket,
                                key=key,
                                rel_path=rel_path,
                                size=0,
                                is_dir=True,
                                source="r2_download",
                            )
                        )
                        handed_off = True
                        return
                    if size == 0:
                        self.ledger.record_free(zero_byte=1)
                        tar_pipeline.submit(
                            _ArchiveTask(
                                bucket=bucket,
                                key=key,
                                rel_path=rel_path,
                                size=0,
                                is_dir=False,
                                source="r2_download",
                                payload_bytes=b"",
                            )
                        )
                        handed_off = True
                        return

                    # 3. Budget check & R2 download -> NVMe TAR packer
                    pub_url = public_urls.get(bucket)
                    allowed_b, paid_b = self.ledger.can_spend_class_b(sc, cost_mode, max_free_b)
                    if not allowed_b:
                        trigger_budget_pause(
                            f"已达到 free-only Class B 上限 ({max_free_b:,}) 或遇到低频存储 {sc}，任务已安全暂停"
                        )
                        return
                    if paid_b:
                        with job.lock:
                            if not job.paid_fallback_used:
                                job.paid_fallback_used = True
                                self._log(f"job {job.job_id}: 已切换为付费 Class B/低频检索模式继续下载 ({sc})")

                    held = spool_budget.acquire(size, stop_event=job.stop_event)
                    spool_tmp = job.spool_dir / f"{uuid.uuid4().hex}.spool"
                    client.download_object_to_file(
                        bucket,
                        key,
                        spool_tmp,
                        expected_size=size,
                        expected_etag=etag,
                        storage_class=sc,
                        public_base_url=pub_url,
                    )
                    tar_pipeline.submit(
                        _ArchiveTask(
                            bucket=bucket,
                            key=key,
                            rel_path=rel_path,
                            size=size,
                            is_dir=False,
                            source="r2_download",
                            source_path=spool_tmp,
                            unlink_source_after_pack=True,
                            held_budget=held,
                            release_fn=spool_budget.release,
                        )
                    )
                    handed_off = True
                    return

                # --- files mode ---
                if is_dir:
                    with dir_cache.lock:
                        dir_cache.ensure_dir_locked(rel_path, on_relocate=record_relocate)
                    self.ledger.record_free(zero_byte=1)
                    outcomes_q.put((bucket, key, rel_path, "copied", "", 0, "pending"))
                    handed_off = True
                    return

                if not overwrite:
                    actual_rel, existing_sz = dir_cache.preexisting_file_size(rel_path)
                    if existing_sz is not None and existing_sz == size:
                        self.ledger.record_free(skipped_existing=1, bytes_transferred=size)
                        outcomes_q.put((bucket, key, actual_rel, "copied", "", size, "pending"))
                        handed_off = True
                        return

                if size == 0:
                    try:
                        actual_rel = dir_cache.write_file_direct(rel_path, None, 0, on_relocate=record_relocate)
                        self.ledger.record_free(zero_byte=1)
                        outcomes_q.put((bucket, key, actual_rel, "copied", "", 0, "pending"))
                    except Exception as exc:
                        outcomes_q.put((bucket, key, rel_path, "failed", str(exc), 0, "pending"))
                    handed_off = True
                    return

                pub_url = public_urls.get(bucket)
                allowed_b, paid_b = self.ledger.can_spend_class_b(sc, cost_mode, max_free_b)
                if not allowed_b:
                    trigger_budget_pause(
                        f"已达到 free-only Class A/B 上限 ({max_free_b:,}) 或遇到低频存储 {sc}，任务已安全暂停"
                    )
                    return
                if paid_b:
                    with job.lock:
                        if not job.paid_fallback_used:
                            job.paid_fallback_used = True
                            self._log(f"job {job.job_id}: 已切换为付费 Class B/低频检索模式继续下载 ({sc})")

                held = spool_budget.acquire(size, stop_event=job.stop_event)
                spool_tmp = job.spool_dir / f"{uuid.uuid4().hex}.spool"
                client.download_object_to_file(
                    bucket,
                    key,
                    spool_tmp,
                    expected_size=size,
                    expected_etag=etag,
                    storage_class=sc,
                    public_base_url=pub_url,
                )
                with in_flight_lock:
                    in_flight_writers[0] += 1
                write_q.put((bucket, key, rel_path, size, spool_tmp, held))
                handed_off = True
            except _JobStopped:
                pass
            except Exception as exc:
                if (isinstance(exc, OSError) and exc.errno == errno.ENOSPC) or "No space left on device" in str(exc):
                    trigger_quota_pause(
                        "Google Drive 挂载或本地缓存空间不足（可能已触发单账号 750 GB / 24h 上传限流 userRateLimitExceeded）；"
                        "任务已安全暂停，待云端后台上传释放缓存后执行 resume 继续"
                    )
                else:
                    outcomes_q.put((bucket, key, rel_path, "failed", str(exc), size, "pending"))
                    handed_off = True
            finally:
                if not handed_off:
                    if spool_tmp is not None:
                        with contextlib.suppress(OSError):
                            spool_tmp.unlink(missing_ok=True)
                    if held:
                        spool_budget.release(held)
                    with in_flight_lock:
                        dispatched_keys.discard((bucket, key))
                with in_flight_lock:
                    active_dl.discard((bucket, key))
                with work_cond:
                    work_cond.notify_all()

        def _resolve_relpath(rel_p: str) -> str:
            seen: set[str] = set()
            cur = rel_p
            while cur in relocated_paths and cur not in seen:
                seen.add(cur)
                cur = relocated_paths[cur]
            return cur

        def drain_outcomes() -> int:
            raw_updates: list[tuple[str, str, str, str, str, int, str]] = []
            relocates: list[tuple[str, str]] = []
            while True:
                try:
                    raw_updates.append(outcomes_q.get_nowait())
                    outcomes_q.task_done()
                except queue.Empty:
                    break
            while True:
                try:
                    old_r, new_r = relocate_q.get_nowait()
                    relocates.append((old_r, new_r))
                    relocated_paths[old_r] = new_r
                    relocate_q.task_done()
                except queue.Empty:
                    break

            updates: list[tuple[str, str, str, float, str, str]] = []
            deltas: dict[str, tuple[int, int]] = {}
            session_files = 0
            session_bytes = 0
            err_items: list[dict[str, str]] = []
            now_ts = time.time()
            done_keys: list[tuple[str, str]] = []
            for b, k, rpath, st_val, err_msg, sz, prev_st in raw_updates:
                final_rel = _resolve_relpath(rpath)
                updates.append((final_rel, st_val, err_msg, now_ts, b, k))
                done_keys.append((b, k))
                dc, db = deltas.get(prev_st, (0, 0))
                deltas[prev_st] = (dc - 1, db - int(sz))
                nc, nb = deltas.get(st_val, (0, 0))
                deltas[st_val] = (nc + 1, nb + int(sz))
                if st_val == "copied":
                    session_files += 1
                    session_bytes += int(sz)
                elif st_val == "failed":
                    err_items.append({"key": f"{b}/{k}", "error": err_msg})

            if updates or relocates:
                with job.db_lock:
                    if updates:
                        conn.executemany(
                            "UPDATE objects SET rel_path=?, status=?, error=?, updated_at=? WHERE bucket=? AND key=?",
                            updates,
                        )
                    for old_r, new_r in relocates:
                        conn.execute(
                            "UPDATE objects SET rel_path = ? WHERE rel_path = ?",
                            (_resolve_relpath(new_r), old_r),
                        )
                    conn.commit()
                if deltas:
                    self._adjust_status_counts(job, deltas)
            if done_keys:
                with in_flight_lock:
                    for bk in done_keys:
                        dispatched_keys.discard(bk)
            if session_files or err_items:
                with job.lock:
                    job.files_done_session += session_files
                    job.bytes_done_session += session_bytes
                    for item in err_items:
                        job.recent_errors.append(item)
                if deltas.get("copied", (0, 0))[0] > 0:
                    delete_wake.set()
            return len(updates)

        try:
            # Phase 1 (optional in tar mode): consolidate existing loose Drive files into TAR shards first.
            if storage_mode == "tar" and tar_pipeline is not None and consolidate_existing and not is_stopping():
                self._consolidate_existing_drive_files(
                    job,
                    conn,
                    tar_pipeline,
                    include_bucket=include_bucket,
                    spool_budget=spool_budget,
                    is_stopping=is_stopping,
                    on_archived_transition=on_cons_archived_transition,
                )
                with job.db_lock:
                    self._refresh_counts_locked(job, conn)
                    self._refresh_buckets_locked(job, conn)
                if not is_stopping():
                    with job.lock:
                        job.message = "正在高速迁移 R2 对象为 TAR 分卷"

            # Phase 2: concurrent R2 downloads -> DriveFS / TAR pipeline
            ckpt_interval = (
                TAR_DEFAULT_CHECKPOINT_INTERVAL
                if (storage_mode == "tar" and self.checkpoint_interval == DEFAULT_CHECKPOINT_INTERVAL)
                else self.checkpoint_interval
            )
            last_ckpt_time = time.monotonic()
            last_ckpt_changes = conn.total_changes
            last_ui_time = time.monotonic()
            last_disk_check = 0.0
            pending_futs: set[concurrent.futures.Future[None]] = set()
            dispatch_cursor: tuple[str, str] = ("", "")

            if not is_stopping():
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as dl_pool:
                    while True:
                        drain_outcomes()

                        now_m = time.monotonic()
                        if (now_m - last_ckpt_time) >= ckpt_interval:
                            if conn.total_changes != last_ckpt_changes:
                                self._flush_checkpoint(job, conn)
                                last_ckpt_changes = conn.total_changes
                                last_ui_time = time.monotonic()
                            last_ckpt_time = time.monotonic()
                        elif (now_m - last_ui_time) >= 30.0:
                            with job.db_lock:
                                self._refresh_counts_locked(job, conn)
                                self._refresh_buckets_locked(job, conn)
                            last_ui_time = now_m

                        pending_futs = {f for f in pending_futs if not f.done()}

                        if is_fuse_drive and (now_m - last_disk_check) >= 5.0 and not is_stopping():
                            last_disk_check = now_m
                            with contextlib.suppress(OSError):
                                min_free = max(_MIN_DRIVEFS_WORK_FREE_BYTES, int(self.spool_bytes_cap) + 2 * 1024 * 1024 * 1024)
                                if shutil.disk_usage(self.work_dir).free < min_free:
                                    trigger_quota_pause(
                                        "Google Drive 本地上传缓存空间不足（可能已触发单账号 750 GB / 24h 上传限流 userRateLimitExceeded）；"
                                        "任务已安全暂停，待云端后台上传释放缓存后执行 resume 继续"
                                    )
                                    break

                        if is_stopping():
                            break

                        max_queue = workers * 4
                        with in_flight_lock:
                            slots = max(0, max_queue - len(active_dl))
                        if slots > 0:
                            fetch_lim = max(slots * 2, 200)
                            with job.db_lock:
                                candidates = conn.execute(
                                    "SELECT bucket, key, size, etag, storage_class, rel_path, is_dir "
                                    "FROM objects WHERE status='pending' AND (bucket, key) > (?, ?) "
                                    "ORDER BY bucket, key LIMIT ?",
                                    (dispatch_cursor[0], dispatch_cursor[1], fetch_lim),
                                ).fetchall()
                            dispatched = 0
                            for cand in candidates:
                                bk = (str(cand[0]), str(cand[1]))
                                queue_full = False
                                with in_flight_lock:
                                    if bk in dispatched_keys or bk in active_dl:
                                        dispatch_cursor = bk
                                        continue
                                    if len(active_dl) >= max_queue:
                                        queue_full = True
                                    else:
                                        dispatch_cursor = bk
                                        dispatched_keys.add(bk)
                                        active_dl.add(bk)
                                if queue_full:
                                    break
                                row_tuple = (
                                    str(cand[0]),
                                    str(cand[1]),
                                    int(cand[2]),
                                    str(cand[3]),
                                    str(cand[4]),
                                    str(cand[5]),
                                    int(cand[6]),
                                )
                                pending_futs.add(dl_pool.submit(process_object, row_tuple))
                                dispatched += 1
                            if dispatched > 0:
                                continue

                        # Nothing newly dispatched from cursor right now; inspect active pipeline state.
                        with in_flight_lock:
                            busy_dl = len(active_dl)
                            busy_wr = in_flight_writers[0]
                            busy_disp = len(dispatched_keys)
                        tar_pending = tar_pipeline.pending_count() if tar_pipeline is not None else 0

                        if busy_dl == 0 and busy_wr == 0 and outcomes_q.empty():
                            drain_outcomes()
                            # Rewind cursor once when no active downloader exists, to confirm zero stragglers behind cursor.
                            if dispatch_cursor != ("", "") and busy_disp == 0:
                                dispatch_cursor = ("", "")
                                continue

                            # In tar mode, seal & flush open <shard_mb shards once all undispatched pending items are consumed.
                            if (
                                tar_pipeline is not None
                                and tar_pipeline.has_open_shards()
                                and listing_finished.is_set()
                            ):
                                with job.db_lock:
                                    probe = conn.execute(
                                        "SELECT bucket, key FROM objects WHERE status='pending' ORDER BY bucket, key LIMIT 512"
                                    ).fetchall()
                                with in_flight_lock:
                                    undispatched_left = any((str(r[0]), str(r[1])) not in dispatched_keys for r in probe)
                                if not undispatched_left:
                                    tar_pipeline.flush_open_shards(wait=True)
                                    dispatch_cursor = ("", "")
                                    continue

                            if tar_pending == 0 and busy_disp == 0:
                                with job.db_lock:
                                    rem_pending = conn.execute(
                                        "SELECT COUNT(*) FROM objects WHERE status='pending'"
                                    ).fetchone()[0]
                                    rem_ia = conn.execute(
                                        "SELECT COUNT(*) FROM objects WHERE status='ia_deferred'"
                                    ).fetchone()[0]
                                if rem_pending > 0:
                                    dispatch_cursor = ("", "")
                                    continue
                                if listing_finished.is_set():
                                    if rem_ia > 0 and cost_mode == "free-first" and not is_stopping():
                                        self._log(
                                            f"job {job.job_id}: Pass 1 免费层级对象完成；切换付费 STANDARD_IA 检索 ({rem_ia} 个对象)"
                                        )
                                        with job.lock:
                                            job.paid_fallback_used = True
                                            job.message = (
                                                f"免费 STANDARD 对象已完成，正在付费迁移 {rem_ia} 个 STANDARD_IA 对象"
                                            )
                                        with job.db_lock:
                                            conn.execute(
                                                "UPDATE objects SET status='pending' WHERE status='ia_deferred'"
                                            )
                                            conn.commit()
                                            self._refresh_counts_locked(job, conn)
                                        dispatch_cursor = ("", "")
                                        continue
                                    break

                        with work_cond:
                            work_cond.wait(timeout=0.4)

                    for fut in list(pending_futs):
                        fut.result()

            # Shut down writers / TAR pipeline & lister cleanly before final R2 deletion drain.
            if writer_threads:
                for _ in writer_threads:
                    write_q.put(None)
                for wt in writer_threads:
                    wt.join()
            if tar_pipeline is not None:
                tar_pipeline.close(wait=True)
                tar_pipeline = None
            drain_outcomes()
            lister_thread.join(timeout=15.0)
            drain_outcomes()

            stop_deleter.set()
            delete_wake.set()
            if deleter_thread is not None:
                deleter_thread.join(timeout=60.0)

            if (
                delete_source
                and not job.cancel_requested
                and not job.pause_requested
                and not self._shutdown
            ):
                end_after_keys: dict[str, str] = {}
                end_verifier = _DriveCloudSyncVerifier(job.dest_root, log=self._log)
                while not self._shutdown:
                    d_cnt, f_cnt, s_cnt = self._delete_copied_chunk(
                        job,
                        conn,
                        client,
                        batch_limit=1000,
                        after_keys=end_after_keys,
                        single_pass=True,
                        cloud_verifier=end_verifier,
                    )
                    if d_cnt == 0 and f_cnt == 0 and s_cnt == 0:
                        break

            with job.db_lock:
                self._refresh_counts_locked(job, conn)
                self._refresh_buckets_locked(job, conn)
            counts = job.status_counts
            pending_cnt = counts.get("pending", {}).get("count", 0)
            failed_cnt = counts.get("failed", {}).get("count", 0)
            ia_cnt = counts.get("ia_deferred", {}).get("count", 0)
            copied_cnt = counts.get("copied", {}).get("count", 0)
            deleted_cnt = counts.get("deleted", {}).get("count", 0)

            now_done = time.time()
            with job.lock:
                if job.cancel_requested:
                    job.status = "cancelled"
                    job.message = "任务已取消并保存断点检查点"
                elif budget_pause_msg:
                    job.status = "paused_budget"
                    job.message = budget_pause_msg[0]
                elif quota_pause_msg:
                    job.status = "paused_quota"
                    job.message = quota_pause_msg[0]
                elif job.pause_requested or self._shutdown:
                    job.status = "paused"
                    job.message = "任务已暂停并保存断点检查点，可通过 resume 继续"
                elif lister_error:
                    job.status = "failed"
                    job.message = f"列举对象失败: {lister_error[0]}"
                elif ia_cnt > 0 and cost_mode == "free-only":
                    job.status = "paused_budget"
                    job.message = (
                        f"已严格保持免费额度：已完成常规对象；剩余 {ia_cnt} 个 STANDARD_IA 低频对象未读取。"
                        f"可执行 `resume {job.job_id} --cost-mode free-first` 继续。"
                    )
                elif failed_cnt > 0:
                    job.status = "failed"
                    job.message = f"部分对象失败 ({failed_cnt} 个)，可执行 `resume {job.job_id}` 重试失败项"
                elif delete_source and copied_cnt > 0 and pending_cnt == 0 and ia_cnt == 0:
                    job.status = "paused_quota"
                    job.message = (
                        f"全部对象已打包写入本地 Drive 挂载，并已清理云端核验通过的 {deleted_cnt} 个 R2 原文件；"
                        f"剩余 {copied_cnt} 个对象正等待 Google Drive 云端同步 (750 GB/24h 限流)，"
                        f"同步完成后执行 cleanup {job.job_id} 即可删空"
                    )
                else:
                    job.status = "completed"
                    if delete_source:
                        job.message = f"迁移完成：已迁移并清理 R2 原文件 {deleted_cnt} 个"
                    else:
                        job.message = f"迁移完成：已写入 Drive {copied_cnt + deleted_cnt} 个对象（R2 原文件已保留）"
                job.finished_at = now_done
                job.session_finished_at = now_done
            self._flush_checkpoint(job, conn)
        finally:
            stop_deleter.set()
            delete_wake.set()
            for _ in range(len(writer_threads)):
                with contextlib.suppress(queue.Full):
                    write_q.put_nowait(None)
            if tar_pipeline is not None:
                with contextlib.suppress(Exception):
                    tar_pipeline.close(wait=True)

    def _delete_copied_chunk(
        self,
        job: _RuntimeJob,
        conn: sqlite3.Connection,
        client: R2Client,
        *,
        bucket_filter: str | None = None,
        batch_limit: int = 1000,
        after_keys: dict[str, str] | None = None,
        single_pass: bool = False,
        cloud_verifier: _DriveCloudSyncVerifier | None = None,
        delete_concurrency: int = 4,
        skip_buckets: set[str] | None = None,
        transient_bucket_errors: dict[str, str] | None = None,
    ) -> tuple[int, int, int]:
        """Re-verify Drive copies (TAR shard or loose file, plus Cloud sync on fuse.drive) and batch-delete up to `batch_limit` keys via Free DeleteObjects.

        Returns `(deleted_total, failed_total, deferred_sync_total)`.
        """
        with job.db_lock:
            if bucket_filter:
                buckets = [bucket_filter]
            else:
                buckets = [
                    str(r[0])
                    for r in conn.execute("SELECT bucket FROM buckets ORDER BY bucket").fetchall()
                ]
                if not buckets:
                    buckets = [
                        str(r[0])
                        for r in conn.execute("SELECT DISTINCT bucket FROM objects WHERE status='copied' ORDER BY bucket").fetchall()
                    ]
        if not buckets:
            return 0, 0, 0

        verifier = cloud_verifier or _DriveCloudSyncVerifier(job.dest_root, log=self._log)
        deleted_total = 0
        failed_total = 0
        deferred_sync_total = 0
        shard_state_cache: dict[str, str] = {}
        now_ts = time.time()
        for bucket in buckets:
            if job.stop_event.is_set() or self._shutdown:
                break
            if skip_buckets is not None and bucket in skip_buckets:
                continue
            after_key = (after_keys or {}).get(bucket, "")
            with job.db_lock:
                rows = conn.execute(
                    "SELECT key, size, rel_path, is_dir FROM objects WHERE bucket=? AND status='copied' AND key > ? ORDER BY key LIMIT ?",
                    (bucket, after_key, batch_limit),
                ).fetchall()
                if not rows:
                    if after_keys is not None and not single_pass:
                        after_keys[bucket] = ""
                    continue
                arch_map: dict[str, tuple[int, str, str, int]] = {}
                rel_cands: list[str] = []
                for _, _, rp, _ in rows:
                    s_rp = str(rp)
                    rel_cands.append(s_rp)
                    if not s_rp.endswith("/__r2_object__"):
                        rel_cands.append(f"{s_rp}/__r2_object__")
                for idx in range(0, len(rel_cands), 400):
                    chunk_rp = rel_cands[idx : idx + 400]
                    q_marks = ",".join("?" for _ in chunk_rp)
                    for arp, asz, sname, rtar, tbytes in conn.execute(
                        f"""
                        SELECT a.rel_path, a.size, a.shard_name, t.rel_tar_path, t.tar_bytes
                        FROM archived_files a
                        JOIN tar_shards t ON a.shard_name = t.shard_name
                        WHERE a.rel_path IN ({q_marks})
                        """,
                        chunk_rp,
                    ).fetchall():
                        arch_map[str(arp)] = (int(asz), str(sname), str(rtar), int(tbytes))

            key_sizes: dict[str, int] = {str(k): int(sz) for k, sz, _, _ in rows}
            verified_keys: list[str] = []
            verify_failed: list[tuple[str, str]] = []
            deferred_here = 0
            for key, size, rel_path, is_dir in rows:
                s_rel = str(rel_path)
                arch_entry = arch_map.get(s_rel) or arch_map.get(f"{s_rel}/__r2_object__")
                state = "missing"
                if arch_entry is not None:
                    asz, sname, rel_tar_path, tar_bytes = arch_entry
                    if is_dir or int(asz) == int(size):
                        s_state = shard_state_cache.get(sname)
                        if s_state is None:
                            tar_p = job.dest_root / rel_tar_path
                            s_state = verifier.verify_file(tar_p, int(tar_bytes))
                            shard_state_cache[sname] = s_state
                        state = s_state
                if state == "missing":
                    target = job.dest_root / s_rel
                    if is_dir:
                        if target.is_dir():
                            state = "synced"
                    else:
                        st1 = verifier.verify_file(target, int(size))
                        if st1 != "missing":
                            state = st1
                        elif target.is_dir():
                            state = verifier.verify_file(target / "__r2_object__", int(size))
                if state == "synced":
                    verified_keys.append(str(key))
                elif state == "pending_sync":
                    deferred_sync_total += 1
                    deferred_here += 1
                else:
                    verify_failed.append((str(key), f"Drive pre-delete verification failed: {job.dest_root / s_rel}"))

            if after_keys is not None:
                if single_pass or deferred_here > 0:
                    after_keys[bucket] = str(rows[-1][0])
                elif len(rows) < batch_limit:
                    after_keys[bucket] = ""

            deltas: dict[str, tuple[int, int]] = {}
            if verify_failed:
                with job.db_lock:
                    conn.executemany(
                        "UPDATE objects SET status='failed', error=?, updated_at=? WHERE bucket=? AND key=?",
                        [(err, now_ts, bucket, k) for k, err in verify_failed],
                    )
                    conn.commit()
                for k, _ in verify_failed:
                    sz = key_sizes.get(k, 0)
                    cc, cb = deltas.get("copied", (0, 0))
                    deltas["copied"] = (cc - 1, cb - sz)
                    fc, fb = deltas.get("failed", (0, 0))
                    deltas["failed"] = (fc + 1, fb + sz)
                failed_total += len(verify_failed)
                with job.lock:
                    for k, err in verify_failed[:10]:
                        job.recent_errors.append({"key": f"{bucket}/{k}", "error": err})

            if verified_keys:
                try:
                    res: DeleteBatchResult = client.delete_objects(
                        bucket,
                        verified_keys,
                        batch_size=250,
                        quiet=True,
                        concurrency=delete_concurrency,
                        log=self._log,
                    )
                except R2Error as err:
                    if isinstance(err, R2HttpError) and err.status in (401, 403):
                        raise
                    self._log(
                        f"job {job.job_id}: transient R2 delete failure on bucket {bucket} (deferring bucket): {err}"
                    )
                    with job.lock:
                        job.recent_errors.append({"key": f"{bucket}/*", "error": str(err)})
                    if skip_buckets is not None:
                        skip_buckets.add(bucket)
                    if transient_bucket_errors is not None:
                        transient_bucket_errors[bucket] = str(err)
                    if deltas:
                        self._adjust_status_counts(job, deltas)
                    continue
                with job.db_lock:
                    if res.deleted:
                        conn.executemany(
                            "UPDATE objects SET status='deleted', error='', updated_at=? WHERE bucket=? AND key=?",
                            [(now_ts, bucket, k) for k in res.deleted],
                        )
                    if res.errors:
                        conn.executemany(
                            "UPDATE objects SET status='failed', error=?, updated_at=? WHERE bucket=? AND key=?",
                            [(err, now_ts, bucket, k) for k, err in res.errors.items()],
                        )
                    conn.commit()
                del_bytes_chunk = 0
                for k in res.deleted:
                    sz = key_sizes.get(k, 0)
                    del_bytes_chunk += sz
                    cc, cb = deltas.get("copied", (0, 0))
                    deltas["copied"] = (cc - 1, cb - sz)
                    dc, db = deltas.get("deleted", (0, 0))
                    deltas["deleted"] = (dc + 1, db + sz)
                if single_pass and res.deleted:
                    with job.lock:
                        job.files_done_session += len(res.deleted)
                        job.bytes_done_session += del_bytes_chunk
                        job.updated_at = time.time()
                for k in res.errors:
                    sz = key_sizes.get(k, 0)
                    cc, cb = deltas.get("copied", (0, 0))
                    deltas["copied"] = (cc - 1, cb - sz)
                    fc, fb = deltas.get("failed", (0, 0))
                    deltas["failed"] = (fc + 1, fb + sz)
                deleted_total += len(res.deleted)
                failed_total += len(res.errors)
                if res.errors:
                    with job.lock:
                        for k, err in list(res.errors.items())[:10]:
                            job.recent_errors.append({"key": f"{bucket}/{k}", "error": err})
            if deltas:
                self._adjust_status_counts(job, deltas)
        return deleted_total, failed_total, deferred_sync_total

    def list_drive(self, inputs: list[str] | None = None, *, recursive: bool = True, limit: int = 500) -> dict[str, Any]:
        """List migrated folders/files safely within Drive mount (or local fallback)."""
        mount = self.drive_mount if self.drive_mount.is_dir() else (self.data_dir / "outputs")
        mount.mkdir(parents=True, exist_ok=True)
        mount_res = mount.resolve()
        specs = inputs or ["r2-backup"]
        files: list[dict[str, Any]] = []
        dirs: list[dict[str, Any]] = []
        truncated = False
        for spec in specs:
            target = resolve_drive_output_root(mount_res, spec, default_subdir="r2-backup", fallback_root=self.data_dir / "outputs")
            if not target.exists():
                continue
            if target.is_file():
                rel = str(target.relative_to(mount_res)).replace("\\", "/")
                files.append({"path": rel, "size": target.stat().st_size})
                continue
            iterator = target.rglob("*") if recursive else target.iterdir()
            for item in sorted(iterator):
                if len(files) + len(dirs) >= limit:
                    truncated = True
                    break
                if item.name.startswith(".") and ".ctpart." in item.name:
                    continue
                rel = str(item.relative_to(mount_res)).replace("\\", "/")
                if item.is_dir():
                    dirs.append({"path": rel + "/"})
                elif item.is_file():
                    with contextlib.suppress(OSError):
                        files.append({"path": rel, "size": item.stat().st_size})
            if truncated:
                break
        return {
            "drive_root": str(mount_res),
            "directories": dirs,
            "files": files,
            "truncated": truncated,
        }
