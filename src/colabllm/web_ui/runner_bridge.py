"""Subprocess bridge and streaming task manager for ColabLLM Web UI."""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from cli_support import CliError, UsageError
from colab_accounts import validate_account
from colabsvc import validate_instance
from processes import process_identity, stop_owned_process
from .state_store import strip_ansi

PKG_ROOT = Path(__file__).resolve().parents[1]
_MAX_TASK_OUTPUT_BYTES = 512 * 1024
_MAX_RETAINED_TASKS = 80


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

    def _pump_output(self, task: ManagedTask, proc: subprocess.Popen[bytes]) -> None:
        try:
            if proc.stdout is not None:
                while True:
                    chunk = proc.stdout.read1(8192) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                    if not chunk:
                        break
                    text = strip_ansi(chunk.decode("utf-8", errors="replace"))
                    if not text:
                        continue
                    with task.cond:
                        task.chunks.append(text)
                        task.total_bytes += len(text.encode("utf-8", errors="replace"))
                        while task.total_bytes > _MAX_TASK_OUTPUT_BYTES and len(task.chunks) > 1:
                            dropped = task.chunks.pop(0)
                            task.total_bytes -= len(dropped.encode("utf-8", errors="replace"))
                            task.truncated = True
                        task.cond.notify_all()
                    self._notify_change()
        finally:
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
    output = strip_ansi(((res.stdout or "") + "\n" + (res.stderr or "")).strip())
    if res.returncode != 0:
        raise CliError(output or f"dry-run 执行失败 ({res.returncode})", res.returncode)
    return {"ok": True, "output": output}


def run_probe(
    script_path: str | Path,
    *,
    instance_arg: str = "",
    account: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    cmd = [sys.executable, str(script_path)]
    inst = (instance_arg or "").strip()
    if inst and inst != "default":
        cmd.extend(["-i", validate_instance(inst)])
    if account and str(account).strip():
        cmd.extend(["--account", validate_account(str(account).strip())])
    cmd.extend(["status", "--json"])
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
        raise CliError("健康探针超时", 124) from None

    stdout_clean = strip_ansi((res.stdout or "").strip())
    stderr_clean = strip_ansi((res.stderr or "").strip())
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
    raise CliError(stderr_clean or stdout_clean or f"状态探测失败 ({res.returncode})", res.returncode or 1)


def run_doctor(
    *,
    online: bool = False,
    account: str | None = None,
    drive_account: str | None = None,
    transport: str | None = None,
    no_drive: bool = False,
    timeout: float = 10.0,
) -> dict[str, Any]:
    cmd = [sys.executable, str(PKG_ROOT / "utility.py"), "doctor", "--json", "--timeout", f"{float(timeout):g}"]
    if online:
        cmd.append("--online")
    if no_drive:
        cmd.append("--no-drive")
    if account and str(account).strip():
        cmd.extend(["-a", validate_account(str(account).strip())])
    if drive_account and str(drive_account).strip():
        cmd.extend(["--drive-account", validate_account(str(drive_account).strip())])
    if transport:
        if transport not in ("quick", "cloudflare", "frp"):
            raise UsageError(f"不支持的传输检查类型: {transport}")
        cmd.extend(["--transport", transport])

    proc_timeout = max(15.0, float(timeout) * (6 if online else 3))
    try:
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=proc_timeout,
            env=_subprocess_env(),
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
