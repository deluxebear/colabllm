"""Shared local HTTP daemon host checks, locking, socket binding, and lifecycle helpers."""
from __future__ import annotations

import contextlib
import errno
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import socket
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


def is_wildcard_host(host: str) -> bool:
    """Return True if host is an all-interfaces wildcard address (0.0.0.0 or ::)."""
    return normalize_host(host) in ("0.0.0.0", "::")


def format_host_port(host: str, port: int) -> str:
    """Format host:port with brackets around IPv6 addresses."""
    normalized = normalize_host(host)
    if ":" in normalized:
        return f"[{normalized}]:{port}"
    return f"{normalized}:{port}"


def resolve_bind_host(host: str | None, allow_remote: bool) -> str:
    """Resolve effective bind address (defaulting --allow-remote without --host to 0.0.0.0)."""
    if host is None:
        return "0.0.0.0" if allow_remote else "127.0.0.1"
    return normalize_host(host)


def _probe_route_addresses(family: int, targets: tuple[tuple[str, int], ...]) -> list[str]:
    found: list[str] = []
    for target in targets:
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM)
        except OSError:
            continue
        try:
            sock.connect(target)
            raw = sock.getsockname()[0].split("%", 1)[0]
            addr = ipaddress.ip_address(raw)
        except (OSError, ValueError):
            continue
        finally:
            sock.close()
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if addr.is_loopback or addr.is_unspecified or addr.is_link_local or addr.is_multicast:
            continue
        text = str(addr)
        if text not in found:
            found.append(text)
    return found


def detect_remote_hosts(bind_host: str) -> list[str]:
    """Best-effort detection of routable local IP(s) when bound to a wildcard address."""
    normalized = normalize_host(bind_host)
    if normalized not in ("0.0.0.0", "::"):
        return []
    v4_targets = (("10.255.255.255", 1), ("192.0.2.1", 80), ("100.64.0.1", 1))
    if normalized == "0.0.0.0":
        return _probe_route_addresses(socket.AF_INET, v4_targets)
    v6_targets = (("2001:db8::1", 80), ("2606:4700:4700::1111", 80))
    hosts = _probe_route_addresses(socket.AF_INET6, v6_targets)
    for v4 in _probe_route_addresses(socket.AF_INET, v4_targets):
        if v4 not in hosts:
            hosts.append(v4)
    return hosts


def describe_daemon_endpoints(bind_host: str, port: int, *, suffix: str = "") -> dict[str, Any]:
    """Compute listen address, primary URL, loopback URL, and detected remote URLs."""
    normalized = normalize_host(bind_host)
    listen = format_host_port(normalized, port)
    if is_wildcard_host(normalized):
        local_host = "127.0.0.1" if normalized == "0.0.0.0" else "::1"
        local_base = f"http://{format_host_port(local_host, port)}"
        remote_bases = [f"http://{format_host_port(h, port)}" for h in detect_remote_hosts(normalized)]
        primary_base = remote_bases[0] if remote_bases else local_base
        return {
            "host": normalized,
            "port": port,
            "listen": listen,
            "wildcard": True,
            "base_url": primary_base,
            "url": f"{primary_base}{suffix}",
            "local_base_url": local_base,
            "local_url": f"{local_base}{suffix}",
            "remote_base_urls": remote_bases,
            "remote_urls": [f"{b}{suffix}" for b in remote_bases],
        }
    base = f"http://{listen}"
    remote = not is_loopback_host(normalized)
    return {
        "host": normalized,
        "port": port,
        "listen": listen,
        "wildcard": False,
        "base_url": base,
        "url": f"{base}{suffix}",
        "local_base_url": None if remote else base,
        "local_url": None if remote else f"{base}{suffix}",
        "remote_base_urls": [base] if remote else [],
        "remote_urls": [f"{base}{suffix}"] if remote else [],
    }


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
        raise UsageError("默认仅允许绑定本机回环地址；若确需监听所有网卡可直接指定 --allow-remote，或配合 --host <IP> --allow-remote 绑定指定地址")
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
