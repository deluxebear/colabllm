"""Single-process asyncio multiplexer daemon hosting native Colab WS keep-alive runners."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import fcntl
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from cli_support import CliError, redirect_diagnostics
from paths import private_dir, user_home
from processes import owned_process, process_identity, runner_alive

_RPC_TIMEOUT = 10.0
_DAEMON_BOOT_TIMEOUT = 8.0
_IDLE_GRACE_SECONDS = 0.25


def mux_paths(state_root: Path | None = None) -> tuple[Path, Path, Path, Path, Path]:
    root = Path(state_root) if state_root is not None else user_home()
    mux_dir = root / "mux"
    return (
        mux_dir,
        mux_dir / "state.json",
        mux_dir / "mux.log",
        mux_dir / "mux.lock",
        mux_dir / "spawn.lock",
    )


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".mux-", suffix=".json", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _load_live_mux_state(state_root: Path) -> dict[str, Any] | None:
    _, state_path, _, _, _ = mux_paths(state_root)
    if not state_path.is_file():
        return None
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    if not owned_process(loaded.get("pid"), loaded.get("identity")):
        return None
    port = loaded.get("port")
    token = loaded.get("token")
    if not isinstance(port, int) or port <= 0 or not isinstance(token, str) or not token:
        return None
    return loaded


def _rpc_sync(
    port: int,
    token: str,
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = _RPC_TIMEOUT,
) -> dict[str, Any]:
    req = {"token": token, "action": action, **(payload or {})}
    raw_req = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(raw_req)
        chunks: list[bytes] = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
    raw_resp = b"".join(chunks).split(b"\n", 1)[0].strip()
    if not raw_resp:
        raise EOFError("mux 守护进程提前关闭连接")
    resp = json.loads(raw_resp.decode("utf-8"))
    if not isinstance(resp, dict):
        raise ValueError("mux 响应格式无效")
    return resp


@dataclasses.dataclass
class ActiveMuxSession:
    session_name: str
    lock_fd: int
    log_file: BinaryIO
    task: asyncio.Task[Any] | None = None


class MuxDaemon:
    """Single-process asyncio supervisor multiplexing Colab WS sessions."""

    def __init__(self, state_root: Path) -> None:
        import colabsvc

        self.state_root = state_root.resolve()
        colabsvc.STATE_ROOT = self.state_root
        self.mux_dir, self.state_path, _, self.lock_path, _ = mux_paths(self.state_root)
        self.pid = os.getpid()
        self.identity = process_identity(self.pid) or ""
        self.token = secrets.token_hex(32)
        self.sessions: dict[str, ActiveMuxSession] = {}
        self._inflight_rpcs = 0
        self._daemon_lock_fd: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._stop_event: asyncio.Event | None = None
        self._idle_task: asyncio.Task[Any] | None = None

    def acquire_daemon_lock(self) -> None:
        private_dir(self.state_root)
        private_dir(self.mux_dir)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._daemon_lock_fd = fd

    def release_daemon_lock(self) -> None:
        self._cleanup_state_file()
        fd = self._daemon_lock_fd
        self._daemon_lock_fd = None
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)

    def _cleanup_state_file(self) -> None:
        if not self.state_path.is_file():
            return
        with contextlib.suppress(OSError, ValueError, UnicodeError):
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("pid") == self.pid and loaded.get("identity") == self.identity:
                self.state_path.unlink(missing_ok=True)

    def _schedule_idle_check(self) -> None:
        if self._stop_event is None or self._stop_event.is_set():
            return
        if self.sessions or self._inflight_rpcs > 0:
            return
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._idle_task = asyncio.create_task(self._maybe_idle_exit())

    async def _maybe_idle_exit(self) -> None:
        await asyncio.sleep(_IDLE_GRACE_SECONDS)
        if self.sessions or self._inflight_rpcs > 0:
            return
        self._cleanup_state_file()
        if self._stop_event is not None:
            self._stop_event.set()

    async def _run_session_task(
        self,
        entry: ActiveMuxSession,
        svc: Any,
        nb_path: Path,
        env_overrides: dict[str, str],
    ) -> None:
        import colabsvc

        state = colabsvc.State(svc)
        exit_code = 1
        try:
            def sink(data: bytes) -> None:
                if not data or entry.log_file.closed:
                    return
                entry.log_file.write(data)
                entry.log_file.flush()

            with redirect_diagnostics(entry.log_file):
                exit_code = await colabsvc.supervise_instance_async(
                    svc,
                    nb_path,
                    owner_pid=self.pid,
                    owner_identity=self.identity,
                    output_sink=sink,
                    env_overrides=env_overrides,
                )
        except asyncio.CancelledError:
            exit_code = 0
            raise
        except BaseException as err:
            with contextlib.suppress(OSError, ValueError):
                entry.log_file.write(f"[colabsvc] {err}\n".encode("utf-8", errors="replace"))
                entry.log_file.flush()
            code = getattr(err, "code", None)
            exit_code = code if isinstance(code, int) and code > 0 else 1
        finally:
            with contextlib.suppress(Exception):
                colabsvc.finish_runner(state, self.pid, self.identity, exit_code)
            with contextlib.suppress(OSError):
                fcntl.flock(entry.lock_fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(entry.lock_fd)
            with contextlib.suppress(OSError):
                entry.log_file.close()
            if self.sessions.get(svc.name) is entry:
                self.sessions.pop(svc.name, None)
            self._schedule_idle_check()

    async def _handle_start(self, req: dict[str, Any]) -> dict[str, Any]:
        import colabsvc

        svc_data = req.get("service")
        nb_path_raw = req.get("nb_path")
        exec_backend = str(req.get("exec_backend") or "native")
        env_overrides = req.get("env_overrides") or {}
        if not isinstance(svc_data, dict) or not isinstance(nb_path_raw, str):
            return {"ok": False, "error": "无效的 start 请求"}
        svc = colabsvc.Service(**svc_data)
        existing = self.sessions.get(svc.name)
        if existing is not None and existing.task is not None and not existing.task.done():
            return {"ok": False, "error": f"'{svc.name}' is already running (mux pid {self.pid})"}

        state = colabsvc.State(svc)
        private_dir(self.state_root)
        private_dir(state.dir.parent)
        private_dir(state.dir)
        lock_path = state.dir / ".runner.lock"
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(lock_fd)
            return {"ok": False, "error": f"'{svc.name}' 锁被占用，无法重复启动"}

        try:
            log_file = state.log_path.open("ab")
            with contextlib.suppress(OSError):
                os.chmod(state.log_path, 0o600)
        except BaseException:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            raise

        state.save(
            runner_pid=self.pid,
            runner_identity=self.identity,
            runner_mode="mux",
            runner_lock=str(lock_path),
            exec_backend=exec_backend,
        )
        entry = ActiveMuxSession(session_name=svc.name, lock_fd=lock_fd, log_file=log_file)
        self.sessions[svc.name] = entry
        entry.task = asyncio.create_task(
            self._run_session_task(
                entry,
                svc,
                Path(nb_path_raw),
                {str(k): str(v) for k, v in env_overrides.items() if isinstance(env_overrides, dict)},
            )
        )
        return {"ok": True, "pid": self.pid, "identity": self.identity}

    async def _handle_stop(self, req: dict[str, Any]) -> dict[str, Any]:
        session_name = str(req.get("session") or "").strip()
        entry = self.sessions.get(session_name)
        if entry is None or entry.task is None or entry.task.done():
            return {"ok": True, "stopped": False}
        entry.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(entry.task), timeout=4.5)
        return {"ok": True, "stopped": True}

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._inflight_rpcs += 1
        try:
            raw_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw_line:
                return
            try:
                req = json.loads(raw_line.decode("utf-8"))
            except (UnicodeError, ValueError):
                resp: dict[str, Any] = {"ok": False, "error": "invalid json"}
            else:
                sent_tok = str(req.get("token") or "") if isinstance(req, dict) else ""
                if not isinstance(req, dict) or not hmac.compare_digest(sent_tok, self.token):
                    resp = {"ok": False, "error": "unauthorized"}
                else:
                    action = req.get("action")
                    if action == "ping":
                        resp = {"ok": True, "pid": self.pid, "sessions": sorted(self.sessions)}
                    elif action == "start":
                        resp = await self._handle_start(req)
                    elif action == "stop":
                        resp = await self._handle_stop(req)
                    elif action == "shutdown":
                        for entry in list(self.sessions.values()):
                            if entry.task and not entry.task.done():
                                entry.task.cancel()
                        if self._stop_event is not None:
                            self._stop_event.set()
                        resp = {"ok": True}
                    else:
                        resp = {"ok": False, "error": f"unknown action: {action!r}"}
            writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as err:
            with contextlib.suppress(Exception):
                writer.write((json.dumps({"ok": False, "error": str(err)}) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            self._inflight_rpcs = max(0, self._inflight_rpcs - 1)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self._schedule_idle_check()

    async def run(self) -> int:
        self.acquire_daemon_lock()
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self._stop_event.set)
        try:
            self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
            sock = self._server.sockets[0]
            port = int(sock.getsockname()[1])
            _write_private_json(
                self.state_path,
                {
                    "pid": self.pid,
                    "identity": self.identity,
                    "port": port,
                    "token": self.token,
                    "state_root": str(self.state_root),
                },
            )
            await self._stop_event.wait()
            for entry in list(self.sessions.values()):
                if entry.task and not entry.task.done():
                    entry.task.cancel()
            tasks = [e.task for e in self.sessions.values() if e.task is not None]
            if tasks:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=4.0)
            if self._server is not None:
                self._server.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            return 0
        finally:
            self.release_daemon_lock()


class MuxRunnerHandle:
    """Popen-compatible lightweight handle for `_wait_ready` polling."""

    def __init__(self, pid: int, state: Any) -> None:
        self.pid = pid
        self._state = state
        self.returncode: int | None = None

    def poll(self) -> int | None:
        st = self._state.load()
        if runner_alive(st):
            return None
        exit_code = st.get("runner_exit")
        self.returncode = int(exit_code) if exit_code is not None else 1
        return self.returncode


def _ensure_mux_daemon(state_root: Path) -> dict[str, Any]:
    mux_dir, state_path, log_path, _, spawn_lock_path = mux_paths(state_root)
    private_dir(state_root)
    private_dir(mux_dir)
    spawn_fd = os.open(str(spawn_lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(spawn_lock_path, 0o600)
        fcntl.flock(spawn_fd, fcntl.LOCK_EX)
        st = _load_live_mux_state(state_root)
        if st is not None:
            try:
                pong = _rpc_sync(st["port"], st["token"], "ping", timeout=2.0)
                if pong.get("ok"):
                    return st
            except (OSError, EOFError, ValueError):
                pass

        with log_path.open("ab") as logf:
            os.chmod(log_path, 0o600)
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--state-root", str(state_root)],
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + _DAEMON_BOOT_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            st = _load_live_mux_state(state_root)
            if st is not None and st.get("pid") == proc.pid:
                with contextlib.suppress(OSError, EOFError, ValueError):
                    pong = _rpc_sync(st["port"], st["token"], "ping", timeout=1.5)
                    if pong.get("ok"):
                        return st
            time.sleep(0.05)
        raise CliError(f"无法启动 mux 共享保活守护进程；请查看 {log_path}")
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(spawn_fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(spawn_fd)


def start_in_mux(
    svc: Any,
    nb_path: Path,
    *,
    state_root: Path,
    exec_backend: str,
    env_overrides: dict[str, str],
) -> MuxRunnerHandle:
    import colabsvc

    payload = {
        "service": dataclasses.asdict(svc),
        "nb_path": str(nb_path),
        "exec_backend": exec_backend,
        "env_overrides": env_overrides,
    }
    last_err: Exception | None = None
    for _ in range(2):
        st = _ensure_mux_daemon(state_root)
        try:
            resp = _rpc_sync(st["port"], st["token"], "start", payload, timeout=_RPC_TIMEOUT)
        except (OSError, EOFError, ValueError) as err:
            last_err = err
            continue
        if not resp.get("ok"):
            raise CliError(str(resp.get("error") or "mux 启动实例失败"))
        return MuxRunnerHandle(int(resp["pid"]), colabsvc.State(svc))
    raise CliError(f"连接 mux 守护进程失败: {last_err}")


def stop_in_mux(
    session_name: str,
    state: Any,
    *,
    state_root: Path,
    timeout: float = 5.0,
) -> None:
    st = _load_live_mux_state(state_root)
    if st is not None:
        with contextlib.suppress(OSError, EOFError, ValueError):
            _rpc_sync(st["port"], st["token"], "stop", {"session": session_name}, timeout=timeout)
    deadline = time.monotonic() + timeout
    while runner_alive(state.load()) and time.monotonic() < deadline:
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ColabLLM shared asyncio multiplexer daemon")
    parser.add_argument("--state-root", required=True)
    args = parser.parse_args(argv)
    daemon = MuxDaemon(Path(args.state_root))
    return asyncio.run(daemon.run())


if __name__ == "__main__":
    sys.exit(main())
