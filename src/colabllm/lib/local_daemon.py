"""Shared local HTTP daemon host checks, locking, socket binding, and lifecycle helpers."""
from __future__ import annotations

import contextlib
import errno
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import time
from typing import Any, Callable

from cli_support import CliError, UsageError
from paths import private_dir
from processes import owned_process, stop_owned_process


def normalize_host(raw_host: str) -> str:
    """Normalize a bind host string (stripping optional IPv6 brackets)."""
    host = (raw_host or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise UsageError("监听地址不能为空")
    return host


_normalize_host = normalize_host


def is_loopback_host(host: str) -> bool:
    """Return True if host resolves syntactically to localhost or a loopback IP."""
    normalized = normalize_host(host)
    if normalized.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def display_host(host: str) -> str:
    """Return a URL-safe host string (mapping wildcard addresses to loopback)."""
    normalized = normalize_host(host)
    if normalized in ("0.0.0.0", "::"):
        return "127.0.0.1" if normalized == "0.0.0.0" else "[::1]"
    if ":" in normalized:
        return f"[{normalized}]"
    return normalized


_display_host = display_host


def validate_bind_args(host: str, port: int, allow_remote: bool, *, min_port: int = 0) -> str:
    """Validate bind host/port range and enforce explicit --allow-remote for non-loopback binds."""
    if not (min_port <= port <= 65535):
        raise UsageError(f"端口号必须在 {min_port}–65535 之间")
    normalized_host = normalize_host(host)
    if not is_loopback_host(normalized_host) and not allow_remote:
        raise UsageError("默认仅允许绑定本机回环地址；若确需监听其他地址，请显式指定 --allow-remote")
    return normalized_host


_validate_bind_args = validate_bind_args


def load_live_daemon_state(state_path: Path, *, unlink_stale: bool = True) -> dict | None:
    """Read daemon state.json only if it exists and belongs to a live owned process."""
    if not state_path.is_file():
        return None
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        if unlink_stale:
            state_path.unlink(missing_ok=True)
        return None
    if not isinstance(loaded, dict) or not owned_process(loaded.get("pid"), loaded.get("identity")):
        if unlink_stale:
            state_path.unlink(missing_ok=True)
        return None
    return loaded


def cleanup_own_daemon_state(state_path: Path, pid: int, identity: str) -> None:
    """Remove state.json only if it still describes this process instance."""
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return
    if isinstance(loaded, dict) and loaded.get("pid") == pid and loaded.get("identity") == identity:
        state_path.unlink(missing_ok=True)


def acquire_instance_lock(
    daemon_dir: Path,
    lock_path: Path,
    state_path: Path,
    *,
    already_running_error: Callable[[dict | None], Exception],
    probe_only: bool = False,
):
    """Acquire non-blocking exclusive flock on lock_path; raise already_running_error if held."""
    private_dir(daemon_dir)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        st = None
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            st = load_live_daemon_state(state_path, unlink_stale=False)
            if st is not None:
                break
            time.sleep(0.03)
        raise already_running_error(st) from None
    except BaseException:
        lock_file.close()
        raise
    if probe_only:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
        return None
    return lock_file


def bind_http_server(
    host: str,
    port: int,
    *,
    server_factory: Callable[[tuple[str, int]], Any],
    allow_remote: bool = False,
    strict_port: bool = False,
    port_span: int = 25,
    min_port: int = 0,
):
    """Bind an HTTP server via server_factory, with optional sequential port fallback."""
    normalized_host = validate_bind_args(host, port, allow_remote, min_port=min_port)
    attempts = (
        [port]
        if (strict_port or port == 0)
        else [port + offset for offset in range(port_span) if port + offset <= 65535]
    )
    last_error: OSError | None = None
    for candidate_port in attempts:
        try:
            return server_factory((normalized_host, candidate_port))
        except OSError as err:
            last_error = err
            if err.errno != errno.EADDRINUSE or strict_port or port == 0:
                raise CliError(f"无法监听 {normalized_host}:{candidate_port}: {err.strerror or err}") from None
    raise CliError(f"从 {port} 起连续端口均已被占用：{last_error}")


def daemon_status(state_path: Path) -> dict | None:
    """Return live daemon state dict if running, or None without creating directories."""
    return load_live_daemon_state(state_path, unlink_stale=True)


def stop_daemon(state_path: Path, *, timeout: float = 5.0) -> int | None:
    """Stop a live daemon process verified by identity and remove state_path."""
    st = load_live_daemon_state(state_path, unlink_stale=True)
    if st is None:
        return None
    pid = int(st["pid"])
    stop_owned_process(pid, st["identity"], group=(st.get("mode") != "foreground"), timeout=timeout)
    state_path.unlink(missing_ok=True)
    return pid
