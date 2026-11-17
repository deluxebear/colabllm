"""Subprocess bridge and streaming task manager for ColabLLM Web UI."""
from __future__ import annotations

import contextlib
import dataclasses
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from cli_support import CliError, UsageError, validate_account_syntax
from colab_accounts import list_account_names, validate_account
from colabsvc import validate_instance
from kaggle_accounts import kaggle_subprocess_env, validate_kaggle_account
from kagglesvc import validate_kaggle_instance, validate_kaggle_session_name
from processes import process_identity, stop_owned_process
from runtime_core import LineBufferedSecretRedactor, redact_runtime_secrets
from .state_store import strip_ansi

PKG_ROOT = Path(__file__).resolve().parents[1]
_MAX_TASK_OUTPUT_BYTES = 512 * 1024
_MAX_RETAINED_TASKS = 80
_USAGE_CACHE_TTL = 45.0
_USAGE_LOCK = threading.Lock()
_USAGE_COND = threading.Condition(_USAGE_LOCK)
_USAGE_INFLIGHT = False
_USAGE_CACHE: dict[str, Any] | None = None

_KAGGLE_USAGE_CACHE_TTL = 60.0
_KAGGLE_USAGE_LOCK = threading.Lock()
_KAGGLE_USAGE_COND = threading.Condition(_KAGGLE_USAGE_LOCK)
_KAGGLE_USAGE_INFLIGHT = False
_KAGGLE_USAGE_CACHE: dict[str, Any] | None = None
_KAGGLE_USAGE_FETCHED_AT = 0.0


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["BROWSER"] = "true"
    env["PYTHONUNBUFFERED"] = "1"
    return env


@dataclasses.dataclass
class ManagedTask:
    task_id: str
    service: str
    subcommand: str
    command_preview: str
    created_at: float
    status: str = "running"  # running | succeeded | failed | cancelled
    returncode: int | None = None
    pid: int | None = None
    identity: str | None = None
    finished_at: float | None = None
    chunks: list[str] = dataclasses.field(default_factory=list)
    total_bytes: int = 0
    truncated: bool = False
    cond: threading.Condition = dataclasses.field(default_factory=threading.Condition)

    def summary(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "service": self.service,
            "subcommand": self.subcommand,
            "command_preview": self.command_preview,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "returncode": self.returncode,
            "pid": self.pid,
            "truncated": self.truncated,
            "output": "".join(self.chunks),
        }


class TaskManager:
    def __init__(self, *, on_change: Callable[[], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, ManagedTask] = {}
        self._order: list[str] = []
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._on_change = on_change
        self._last_notify_mono = 0.0

    def _notify_change(self, *, force: bool = False) -> None:
        cb = self._on_change
        if cb is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_notify_mono) < 0.2:
            return
        self._last_notify_mono = now
        with contextlib.suppress(Exception):
            cb()

    def start_task(
        self,
        *,
        service_name: str,
        script_path: str | Path,
        subcommand: str,
        argv: list[str],
        command_preview: str,
    ) -> dict[str, Any]:
        task_id = secrets.token_hex(8)
        cmd = [sys.executable, str(script_path), *argv]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=_subprocess_env(),
            )
        except OSError as err:
            raise CliError(f"无法启动任务进程: {err}") from None

        identity = process_identity(proc.pid)
        task = ManagedTask(
            task_id=task_id,
            service=service_name,
            subcommand=subcommand,
            command_preview=command_preview,
            created_at=time.time(),
            pid=proc.pid,
            identity=identity,
        )
        with self._lock:
            self._tasks[task_id] = task
            self._order.append(task_id)
            self._procs[task_id] = proc
            self._prune_locked()

        reader = threading.Thread(
            target=self._pump_output,
            args=(task, proc),
            daemon=True,
        )
        reader.start()
        return task.summary()

    def _prune_locked(self) -> None:
        if len(self._order) <= _MAX_RETAINED_TASKS:
            return
        for tid in list(self._order):
            if len(self._order) <= _MAX_RETAINED_TASKS:
                break
            t = self._tasks.get(tid)
            if t and t.status != "running":
                self._order.remove(tid)
                self._tasks.pop(tid, None)
                self._procs.pop(tid, None)

    def _append_task_text(self, task: ManagedTask, text: str) -> None:
        if not text:
            return
        with task.cond:
            task.chunks.append(text)
            task.total_bytes += len(text.encode("utf-8", errors="replace"))
            while task.total_bytes > _MAX_TASK_OUTPUT_BYTES and len(task.chunks) > 1:
                dropped = task.chunks.pop(0)
                task.total_bytes -= len(dropped.encode("utf-8", errors="replace"))
                task.truncated = True
            task.cond.notify_all()
        self._notify_change()

    def _pump_output(self, task: ManagedTask, proc: subprocess.Popen[bytes]) -> None:
        redactor = LineBufferedSecretRedactor(max_buffer=65536)
        try:
            if proc.stdout is not None:
                while True:
                    chunk = proc.stdout.read1(8192) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                    if not chunk:
                        break
                    text = strip_ansi(chunk.decode("utf-8", errors="replace"))
                    if not text:
                        continue
                    safe_text = redactor.feed(text, final=False)
                    self._append_task_text(task, safe_text)
        finally:
            rem = redactor.feed("", final=True)
            if rem:
                self._append_task_text(task, rem)
            if proc.stdout is not None:
                with contextlib.suppress(OSError):
                    proc.stdout.close()
            rc = proc.wait()
            with task.cond:
                task.returncode = rc
                task.finished_at = time.time()
                if task.status == "running":
                    task.status = "succeeded" if rc == 0 else "failed"
                task.cond.notify_all()
            with self._lock:
                self._procs.pop(task.task_id, None)
            self._notify_change(force=True)

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order[-30:]))
            tasks = [self._tasks[tid] for tid in ids if tid in self._tasks]
        result = []
        for task in tasks:
            with task.cond:
                result.append({
                    "task_id": task.task_id,
                    "service": task.service,
                    "subcommand": task.subcommand,
                    "command_preview": task.command_preview,
                    "created_at": task.created_at,
                    "finished_at": task.finished_at,
                    "status": task.status,
                    "returncode": task.returncode,
                    "chunk_count": len(task.chunks),
                    "truncated": task.truncated,
                    "output_tail": "".join(task.chunks)[-4096:],
                })
        return result

    def get_task(self, task_id: str) -> ManagedTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            raise UsageError(f"未找到任务: {task_id}")
        with task.cond:
            if task.status != "running":
                return task.summary()
            task.status = "cancelled"
            pid, identity = task.pid, task.identity
        stop_owned_process(pid, identity, group=True, timeout=3)
        with task.cond:
            task.cond.notify_all()
            return task.summary()

    def shutdown_all(self) -> None:
        with self._lock:
            running = [
                (t.pid, t.identity)
                for t in self._tasks.values()
                if t.status == "running" and t.pid and t.identity
            ]
        for pid, identity in running:
            try:
                stop_owned_process(pid, identity, group=True, timeout=2)
            except Exception:
                pass


def run_dry_run(script_path: str | Path, argv: list[str], *, timeout: float = 20.0) -> dict[str, Any]:
    try:
        res = subprocess.run(
            [sys.executable, str(script_path), *argv],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        raise CliError("dry-run 预览超时", 124) from None
    raw_output = strip_ansi(((res.stdout or "") + "\n" + (res.stderr or "")).strip())
    output = redact_runtime_secrets(raw_output)
    if res.returncode != 0:
        raise CliError(output or f"dry-run 执行失败 ({res.returncode})", res.returncode)
    return {"ok": True, "output": output}


def run_probe(
    script_path: str | Path,
    *,
    instance_arg: str = "",
    account: str | None = None,
    provider: str = "colab",
    short_name: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    cmd = [sys.executable, str(script_path)]
    inst = (instance_arg or "").strip()
    if provider == "kaggle":
        if inst and inst != "default":
            validate_kaggle_instance(inst)
            if short_name:
                validate_kaggle_session_name(f"{short_name}-{inst}")
            cmd.extend(["-i", inst])
        if account and str(account).strip():
            cmd.extend(["--account", validate_kaggle_account(str(account).strip())])
        env = kaggle_subprocess_env(None, base_env=_subprocess_env())
    else:
        if inst and inst != "default":
            cmd.extend(["-i", validate_instance(inst)])
        if account and str(account).strip():
            cmd.extend(["--account", validate_account(str(account).strip())])
        env = _subprocess_env()
    cmd.extend(["status", "--json"])
    try:
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise CliError("健康探针超时", 124) from None

    stdout_clean = strip_ansi((res.stdout or "").strip())
    stderr_clean = redact_runtime_secrets(strip_ansi((res.stderr or "").strip()))
    if stdout_clean:
        try:
            data = json.loads(stdout_clean)
            if isinstance(data, dict):
                data["healthy"] = res.returncode == 0
                if stderr_clean:
                    data["diagnostics"] = stderr_clean
                return data
        except ValueError:
            pass
    raise CliError(
        redact_runtime_secrets(stderr_clean or stdout_clean) or f"状态探测失败 ({res.returncode})",
        res.returncode or 1,
    )


def run_doctor(
    *,
    online: bool = False,
    account: str | None = None,
    drive_account: str | None = None,
    transport: str | None = None,
    no_drive: bool = False,
    provider: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    if provider is not None and provider not in ("colab", "kaggle", "all"):
        raise UsageError(f"不支持的检查平台: {provider}")
    cmd = [sys.executable, str(PKG_ROOT / "utility.py"), "doctor", "--json", "--timeout", f"{float(timeout):g}"]
    if provider:
        cmd.extend(["--provider", provider])
    if online:
        cmd.append("--online")
    if no_drive and provider != "kaggle":
        cmd.append("--no-drive")
    if account and str(account).strip():
        acct_str = str(account).strip()
        if provider == "kaggle":
            validated_acct = validate_kaggle_account(acct_str)
        elif provider == "all":
            validated_acct = validate_account_syntax(acct_str)
        else:
            validated_acct = validate_account(acct_str)
        cmd.extend(["-a", validated_acct])
    if drive_account and str(drive_account).strip() and provider != "kaggle":
        cmd.extend(["--drive-account", validate_account(str(drive_account).strip())])
    if transport:
        if transport not in ("quick", "cloudflare", "frp"):
            raise UsageError(f"不支持的传输检查类型: {transport}")
        cmd.extend(["--transport", transport])

    proc_timeout = max(15.0, float(timeout) * (6 if online else 3))
    env = (
        kaggle_subprocess_env(None, base_env=_subprocess_env())
        if provider in ("kaggle", "all")
        else _subprocess_env()
    )
    try:
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=proc_timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise CliError("doctor 检查超时", 124) from None

    stdout_clean = strip_ansi((res.stdout or "").strip())
    stderr_clean = strip_ansi((res.stderr or "").strip())
    if stdout_clean:
        try:
            data = json.loads(stdout_clean)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    raise CliError(stderr_clean or stdout_clean or f"doctor 检查失败 ({res.returncode})", res.returncode or 1)


def run_frp_status(*, timeout: float = 20.0) -> dict[str, Any]:
    cmd = [sys.executable, str(PKG_ROOT / "control.py"), "frp-status"]
    try:
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        raise CliError("frp-status 查询超时", 124) from None

    stdout_clean = strip_ansi((res.stdout or "").strip())
    stderr_clean = strip_ansi((res.stderr or "").strip())
    if res.returncode != 0:
        raise CliError(stderr_clean or stdout_clean or f"frp-status 查询失败 ({res.returncode})", res.returncode)
    try:
        return json.loads(stdout_clean) if stdout_clean else {}
    except ValueError:
        return {"raw": stdout_clean}


_PLAIN_BALANCE_RE = re.compile(r"Current balance:\s*([0-9]+(?:\.[0-9]+)?)\s*compute units", re.IGNORECASE)
_PLAIN_RATE_RE = re.compile(r"Usage rate:\s*([0-9]+(?:\.[0-9]+)?)/hr", re.IGNORECASE)
_PLAIN_ACTIVE_RE = re.compile(r"Active assignments:\s*([0-9]+)", re.IGNORECASE)


def get_cached_accounts_usage(*, max_age: float | None = None) -> dict[str, Any] | None:
    """Return cached Colab compute-unit usage snapshot without spawning subprocesses."""
    ttl = _USAGE_CACHE_TTL if max_age is None else float(max_age)
    with _USAGE_COND:
        if _USAGE_CACHE is None:
            return None
        if ttl >= 0 and (time.time() - float(_USAGE_CACHE.get("fetched_at") or 0.0)) > ttl:
            return None
        return dict(_USAGE_CACHE)


def clear_accounts_usage_cache() -> None:
    """Reset the in-memory Colab and Kaggle quota caches (mainly for tests)."""
    global _USAGE_CACHE, _KAGGLE_USAGE_CACHE, _KAGGLE_USAGE_FETCHED_AT
    with _USAGE_COND:
        _USAGE_CACHE = None
    with _KAGGLE_USAGE_COND:
        _KAGGLE_USAGE_CACHE = None
        _KAGGLE_USAGE_FETCHED_AT = 0.0


def get_cached_kaggle_usage(*, max_age: float | None = None) -> dict[str, Any] | None:
    """Return cached Kaggle quota/session snapshot without spawning subprocesses."""
    ttl = _KAGGLE_USAGE_CACHE_TTL if max_age is None else float(max_age)
    with _KAGGLE_USAGE_COND:
        if _KAGGLE_USAGE_CACHE is None:
            return None
        if ttl >= 0 and (time.time() - _KAGGLE_USAGE_FETCHED_AT) > ttl:
            return None
        return dict(_KAGGLE_USAGE_CACHE)


def _normalize_kaggle_quota(raw: Any) -> dict[str, float | None] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, float | None] = {}
    for key in ("used_hours", "remaining_hours", "total_hours"):
        val = raw.get(key)
        if val is None:
            out[key] = None
        else:
            try:
                out[key] = round(float(val), 2)
            except (TypeError, ValueError):
                out[key] = None
    if all(v is None for v in out.values()):
        return None
    return out


def fetch_kaggle_accounts_usage(*, refresh: bool = False, timeout: float = 20.0) -> dict[str, Any]:
    """Query Kaggle weekly quota & session counts via kaggle CLI with 60s single-flight caching."""
    global _KAGGLE_USAGE_INFLIGHT, _KAGGLE_USAGE_CACHE, _KAGGLE_USAGE_FETCHED_AT

    with _KAGGLE_USAGE_COND:
        if (
            not refresh
            and _KAGGLE_USAGE_CACHE is not None
            and (time.time() - _KAGGLE_USAGE_FETCHED_AT) < _KAGGLE_USAGE_CACHE_TTL
        ):
            return dict(_KAGGLE_USAGE_CACHE)
        if _KAGGLE_USAGE_INFLIGHT:
            _KAGGLE_USAGE_COND.wait(timeout=timeout + 5.0)
            if not refresh and _KAGGLE_USAGE_CACHE is not None:
                return dict(_KAGGLE_USAGE_CACHE)
        _KAGGLE_USAGE_INFLIGHT = True

    try:
        env = kaggle_subprocess_env(None, base_env=_subprocess_env())
        kaggle_bin = shutil.which("kaggle", path=env.get("PATH"))
        fetched_at_ts = time.time()
        fetched_at = datetime.now(timezone.utc).isoformat()
        error_msg: str | None = None
        by_account: dict[str, dict[str, Any]] = {}
        available = False

        if not kaggle_bin:
            error_msg = "未找到 kaggle CLI"
        else:
            status_records: list[Any] | None = None
            try:
                res = subprocess.run(
                    [kaggle_bin, "account", "status", "--json"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                )
                stdout_clean = strip_ansi((res.stdout or "").strip())
                stderr_clean = redact_runtime_secrets(strip_ansi((res.stderr or "").strip()))
                if res.returncode != 0:
                    error_msg = stderr_clean or stdout_clean or f"kaggle account status 退出码 {res.returncode}"
                elif not stdout_clean:
                    error_msg = "kaggle account status 返回空输出"
                else:
                    parsed = json.loads(stdout_clean)
                    if isinstance(parsed, list):
                        status_records = parsed
                    else:
                        error_msg = "kaggle account status --json 返回格式无效"
            except subprocess.TimeoutExpired:
                error_msg = "查询 kaggle account status 超时"
            except OSError as err:
                error_msg = str(err)
            except ValueError:
                error_msg = "无法解析 kaggle account status --json 输出"

            if status_records is not None:
                available = True
                session_counts: dict[str, int] | None = None
                try:
                    sres = subprocess.run(
                        [kaggle_bin, "session", "list", "--all-accounts", "--json"],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=min(10.0, timeout),
                        env=env,
                    )
                    if sres.returncode == 0:
                        s_out = strip_ansi((sres.stdout or "").strip())
                        s_parsed = json.loads(s_out) if s_out else []
                        if isinstance(s_parsed, list):
                            session_counts = {}
                            inactive_states = {"stopped", "cancelled", "dead", "reclaimed", "complete", "error"}
                            for sess in s_parsed:
                                if not isinstance(sess, dict):
                                    continue
                                acct_key = str(sess.get("account") or "").strip()
                                if not acct_key:
                                    continue
                                state_val = str(sess.get("state") or "active").strip().lower()
                                if state_val not in inactive_states:
                                    session_counts[acct_key] = session_counts.get(acct_key, 0) + 1
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    session_counts = None

                for item in status_records:
                    if not isinstance(item, dict) or not item.get("account"):
                        continue
                    acct_name = str(item["account"]).strip()
                    by_account[acct_name] = {
                        "account": acct_name,
                        "active": bool(item.get("active")),
                        "username": item.get("username"),
                        "auth_method": item.get("auth_method"),
                        "authenticated": bool(item.get("authenticated")),
                        "gpu_quota": _normalize_kaggle_quota(item.get("gpu_quota")),
                        "tpu_quota": _normalize_kaggle_quota(item.get("tpu_quota")),
                        "active_sessions_count": None if session_counts is None else session_counts.get(acct_name, 0),
                        "refresh_at": item.get("refresh_at"),
                        "error": str(item["error"]) if item.get("error") else None,
                    }

        snapshot: dict[str, Any] = {
            "available": available,
            "error": error_msg,
            "fetched_at": fetched_at,
            "accounts": by_account,
        }
        with _KAGGLE_USAGE_COND:
            _KAGGLE_USAGE_CACHE = snapshot
            _KAGGLE_USAGE_FETCHED_AT = fetched_at_ts
            return dict(snapshot)
    finally:
        with _KAGGLE_USAGE_COND:
            _KAGGLE_USAGE_INFLIGHT = False
            _KAGGLE_USAGE_COND.notify_all()


def _normalize_account_quota(
    raw_usage: dict[str, Any] | None,
    raw_assignments: list[Any] | None,
    error: str | None,
) -> dict[str, Any]:
    usage_obj: dict[str, Any] | None = None
    if isinstance(raw_usage, dict):
        balance = round(float(raw_usage.get("balance", 0.0) or 0.0), 2)
        rate_hourly = round(float(raw_usage.get("rate_hourly", 0.0) or 0.0), 2)
        assignments_count = int(raw_usage.get("assignments_count", 0) or 0)
        estimated_hours = round(balance / rate_hourly, 1) if (balance > 0 and rate_hourly > 0) else None
        usage_obj = {
            "balance": balance,
            "rate_hourly": rate_hourly,
            "assignments_count": assignments_count,
            "estimated_hours": estimated_hours,
            "tier": "paid" if balance > 0 else "free",
        }

    assignments: list[dict[str, str]] = []
    if isinstance(raw_assignments, list):
        for a in raw_assignments:
            if not isinstance(a, dict):
                continue
            assignments.append({
                "name": str(a.get("name") or "?"),
                "endpoint": str(a.get("endpoint") or ""),
                "hardware": str(a.get("hardware") or a.get("accelerator") or "CPU"),
                "shape": str(a.get("shape") or "Standard"),
            })

    return {
        "usage": usage_obj,
        "assignments": assignments,
        "error": str(error) if error else None,
    }


def _parse_plain_colab_usage(text: str) -> dict[str, Any] | None:
    cleaned = strip_ansi(text or "")
    bal_m = _PLAIN_BALANCE_RE.search(cleaned)
    rate_m = _PLAIN_RATE_RE.search(cleaned)
    act_m = _PLAIN_ACTIVE_RE.search(cleaned)
    if not bal_m:
        return None
    return {
        "balance": float(bal_m.group(1)),
        "rate_hourly": float(rate_m.group(1)) if rate_m else 0.0,
        "assignments_count": int(act_m.group(1)) if act_m else 0,
    }


def _query_single_plain_usage(colab_bin: str, account: str | None, *, timeout: float) -> dict[str, Any]:
    cmd = [colab_bin]
    if account:
        cmd.extend(["-a", account])
    cmd.append("usage")
    try:
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return _normalize_account_quota(None, [], "查询 colab usage 超时")
    except OSError as err:
        return _normalize_account_quota(None, [], str(err))

    stdout_clean = strip_ansi((res.stdout or "").strip())
    stderr_clean = strip_ansi((res.stderr or "").strip())
    if res.returncode != 0:
        err_msg = stderr_clean or stdout_clean or f"colab usage 退出码 {res.returncode}"
        return _normalize_account_quota(None, [], err_msg)

    parsed: dict[str, Any] | None = None
    if stdout_clean.startswith("{"):
        with contextlib.suppress(ValueError):
            obj = json.loads(stdout_clean)
            if isinstance(obj, dict):
                raw_u = obj.get("usage") if isinstance(obj.get("usage"), dict) else obj
                if "balance" in raw_u:
                    parsed = raw_u
    if parsed is None:
        parsed = _parse_plain_colab_usage(stdout_clean)
    if parsed is None:
        return _normalize_account_quota(None, [], stderr_clean or stdout_clean or "无法解析 colab usage 输出")
    return _normalize_account_quota(parsed, [], None)


def fetch_accounts_usage(*, refresh: bool = False, timeout: float = 20.0) -> dict[str, Any]:
    """Query Colab compute-unit balance and active VMs with single-flight caching."""
    global _USAGE_INFLIGHT, _USAGE_CACHE

    with _USAGE_COND:
        if (
            not refresh
            and _USAGE_CACHE is not None
            and (time.time() - float(_USAGE_CACHE.get("fetched_at") or 0.0)) < _USAGE_CACHE_TTL
        ):
            return dict(_USAGE_CACHE)
        if _USAGE_INFLIGHT:
            _USAGE_COND.wait(timeout=timeout + 5.0)
            if _USAGE_CACHE is not None:
                return dict(_USAGE_CACHE)
        _USAGE_INFLIGHT = True

    try:
        env = _subprocess_env()
        colab_bin = shutil.which("colab", path=env.get("PATH"))
        now = time.time()
        fetched_time = time.strftime("%H:%M:%S", time.localtime(now))
        by_account: dict[str, dict[str, Any]] = {}
        legacy: dict[str, Any] | None = None
        source = "missing"
        supported = bool(colab_bin)
        top_error: str | None = None

        if not colab_bin:
            top_error = "未找到 colab CLI"
        else:
            ext_items: list[Any] | None = None
            try:
                res = subprocess.run(
                    [colab_bin, "account", "status", "--json"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                )
                if res.returncode == 0:
                    stdout_clean = strip_ansi((res.stdout or "").strip())
                    if stdout_clean:
                        with contextlib.suppress(ValueError):
                            parsed = json.loads(stdout_clean)
                            if isinstance(parsed, list):
                                ext_items = parsed
            except (subprocess.TimeoutExpired, OSError):
                ext_items = None

            if ext_items is not None:
                source = "colab-ext"
                for item in ext_items:
                    if not isinstance(item, dict) or not item.get("account"):
                        continue
                    acct_name = str(item["account"])
                    by_account[acct_name] = _normalize_account_quota(
                        item.get("usage"),
                        item.get("assignments"),
                        item.get("error"),
                    )
            else:
                source = "colab-usage"
                names = list_account_names()
                if names:
                    per_timeout = min(10.0, timeout)
                    for name in names:
                        by_account[name] = _query_single_plain_usage(colab_bin, name, timeout=per_timeout)
                else:
                    legacy = _query_single_plain_usage(colab_bin, None, timeout=min(10.0, timeout))
                    if legacy.get("error") and legacy.get("usage") is None:
                        top_error = legacy["error"]

        entries = list(by_account.values()) if by_account else ([legacy] if legacy else [])
        queried_accounts = sum(1 for item in entries if isinstance(item, dict) and item.get("usage") is not None)
        total_balance = round(
            sum(float((item.get("usage") or {}).get("balance") or 0.0) for item in entries if isinstance(item, dict)),
            2,
        )
        total_rate = round(
            sum(
                float((item.get("usage") or {}).get("rate_hourly") or 0.0)
                for item in entries
                if isinstance(item, dict)
            ),
            2,
        )
        total_assignments = sum(
            int((item.get("usage") or {}).get("assignments_count") or 0)
            for item in entries
            if isinstance(item, dict)
        )

        snapshot: dict[str, Any] = {
            "supported": supported,
            "source": source,
            "fetched_at": now,
            "fetched_time": fetched_time,
            "accounts": by_account,
            "legacy": legacy,
            "totals": {
                "total_balance": total_balance,
                "total_rate_hourly": total_rate,
                "total_assignments": total_assignments,
                "queried_accounts": queried_accounts,
            },
            "error": top_error,
        }
        with _USAGE_COND:
            _USAGE_CACHE = snapshot
            return dict(snapshot)
    finally:
        with _USAGE_COND:
            _USAGE_INFLIGHT = False
            _USAGE_COND.notify_all()
