"""Pure-stdlib asyncio RFC 6455 WebSocket & Jupyter Kernel client for Colab runtimes."""
from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile
import time
from typing import Any, Awaitable, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cli_support import safe_urlopen, shared_ssl_context
from colab_accounts import account_sessions_path
from protocol import NotebookProtocol

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_WS_MESSAGE_BYTES = 64 * 1024 * 1024
_DEFAULT_PING_INTERVAL = 30.0
_DEFAULT_PONG_TIMEOUT = 95.0


class ColabWsError(RuntimeError):
    """Base error raised by the native Colab WebSocket runner."""


class ColabAuthExpiredError(ColabWsError):
    """Colab runtime proxy token was rejected with HTTP 401/403."""


class ColabSessionLostError(ColabWsError):
    """Colab runtime or kernel returned HTTP 404 / no longer exists."""


class _ReadyDeadlineExceeded(TimeoutError):
    """Raised only when the notebook fails to emit CT_READY before `deadline`."""


def _colab_config_dir(env: dict[str, str] | None = None) -> Path:
    src = env if env is not None else os.environ
    raw = (src.get("COLAB_CLI_CONFIG_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    home = (src.get("HOME") or "").strip()
    if home:
        return Path(home).expanduser() / ".config" / "colab-cli"
    return Path.home() / ".config" / "colab-cli"


def _sessions_file_for(account: str | None, env: dict[str, str] | None = None) -> Path:
    if env is None:
        return account_sessions_path(account)
    cfg = _colab_config_dir(env)
    acct = (account or "").strip() if isinstance(account, str) else ""
    if acct and ((cfg / "accounts.json").is_file() or (cfg / "accounts" / acct).is_dir()):
        return cfg / "accounts" / acct / "sessions.json"
    return cfg / "sessions.json"


def load_colab_session_record(
    session_name: str,
    account: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return the recorded Colab session entry if it has valid `url` and `token` fields."""
    path = _sessions_file_for(account, env=env)
    if not path.is_file():
        return None
    with contextlib.suppress(OSError, UnicodeError, ValueError):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        rec = data.get(session_name)
        if not isinstance(rec, dict):
            return None
        url = rec.get("url")
        token = rec.get("token")
        if isinstance(url, str) and url.strip() and isinstance(token, str) and token.strip():
            return rec
    return None


def has_native_colab_session(
    session_name: str,
    account: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """True when `sessions.json` contains a real runtime proxy URL and token for `session_name`."""
    return load_colab_session_record(session_name, account=account, env=env) is not None


def is_token_near_expiry(record: dict[str, Any], *, margin_seconds: float = 300.0) -> bool:
    raw = record.get("token_expires_at")
    if not isinstance(raw, str) or not raw.strip():
        return True
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).total_seconds() <= margin_seconds


def update_session_kernel_ids(
    session_name: str,
    account: str | None,
    kernel_id: str,
    session_id: str,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Persist discovered kernel_id/session_id back to `sessions.json` safely."""
    path = _sessions_file_for(account, env=env)
    if not path.is_file():
        return
    lock_path = Path(f"{path}.lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with contextlib.suppress(OSError):
            os.chmod(lock_path, 0o600)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        rec = data.get(session_name)
        if not isinstance(rec, dict):
            return
        if rec.get("kernel_id") == kernel_id and rec.get("session_id") == session_id:
            return
        rec["kernel_id"] = kernel_id
        rec["session_id"] = session_id
        fd, tmp_name = tempfile.mkstemp(prefix=".sessions-", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(data, out, indent=2)
                out.flush()
                os.fsync(out.fileno())
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(lock_fd)


def _colab_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Jupyter Kernel Client",
        "Authorization": f"Bearer {token}",
        "X-Colab-Client-Agent": "colab-cli",
        "X-Colab-Runtime-Proxy-Token": token,
    }


def _http_json_once(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_colab_headers(token), method=method)
    try:
        with safe_urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise ColabAuthExpiredError(f"Colab 代理令牌失效 (HTTP {err.code})") from err
        if err.code == 404:
            raise ColabSessionLostError("Colab 运行时或 Kernel 不存在 (HTTP 404)") from err
        raise ColabWsError(f"Colab HTTP 请求失败 ({method} {url}): HTTP {err.code}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise ColabWsError(f"Colab HTTP 请求失败 ({method} {url}): {err}") from err
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as err:
        raise ColabWsError(f"Colab HTTP 返回了无效 JSON: {err}") from err


def ensure_kernel_sync(
    session_name: str,
    account: str | None = None,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[str, str, str, str]:
    """Verify or create a remote Jupyter kernel, returning `(url, token, kernel_id, session_id)`."""
    rec = load_colab_session_record(session_name, account=account, env=env)
    if rec is None:
        raise ColabSessionLostError(f"[colab] Session '{session_name}' not found.")

    base_url = str(rec["url"]).strip().rstrip("/")
    token = str(rec["token"]).strip()
    saved_kernel = rec.get("kernel_id")
    kernel_id = str(saved_kernel).strip() if isinstance(saved_kernel, str) and saved_kernel.strip() else None
    saved_sid = rec.get("session_id")
    session_id = (
        str(saved_sid).strip()
        if isinstance(saved_sid, str) and saved_sid.strip()
        else f"{uuid.uuid4().hex[:8]}-{uuid.uuid4().hex[:24]}"
    )

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            if kernel_id:
                try:
                    info = _http_json_once(
                        "GET",
                        f"{base_url}/api/kernels/{urllib.parse.quote(kernel_id, safe='')}",
                        token=token,
                        timeout=timeout,
                    )
                    if not isinstance(info, dict) or not info.get("id"):
                        kernel_id = None
                except ColabSessionLostError:
                    kernel_id = None

            if not kernel_id:
                listed = _http_json_once("GET", f"{base_url}/api/kernels", token=token, timeout=timeout)
                if isinstance(listed, list):
                    for item in listed:
                        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                            kernel_id = item["id"].strip()
                            break
                if not kernel_id:
                    created = _http_json_once(
                        "POST",
                        f"{base_url}/api/kernels",
                        token=token,
                        payload={"name": "python3", "path": None},
                        timeout=timeout,
                    )
                    if isinstance(created, dict) and isinstance(created.get("id"), str) and created["id"].strip():
                        kernel_id = created["id"].strip()
            break
        except (ColabAuthExpiredError, ColabSessionLostError):
            raise
        except ColabWsError as err:
            last_err = err
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    else:
        if last_err is not None:
            raise last_err

    if not kernel_id:
        raise ColabWsError("无法从 Colab 运行时获取或创建 Kernel ID")

    update_session_kernel_ids(session_name, account, kernel_id, session_id, env=env)
    return base_url, token, kernel_id, session_id


def _mask_bytes(data: bytes, mask: bytes) -> bytes:
    if not data:
        return b""
    n = len(data)
    repeated = (mask * ((n // 4) + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(n, "big")


class AsyncWebSocket:
    """Minimal pure-stdlib RFC 6455 asyncio WebSocket client."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._write_lock = asyncio.Lock()
        self.last_rx_mono = time.monotonic()
        self.closed = False

    @classmethod
    async def connect(
        cls,
        ws_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> "AsyncWebSocket":
        parsed = urllib.parse.urlsplit(ws_url)
        scheme = parsed.scheme.lower()
        if scheme not in ("ws", "wss") or not parsed.hostname:
            raise ColabWsError(f"无效的 WebSocket 地址: {ws_url}")
        host = parsed.hostname
        port = parsed.port or (443 if scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        is_tls = scheme == "wss"
        default_port = 443 if is_tls else 80
        host_header = f"[{host}]" if ":" in host else host
        if port != default_port:
            host_header = f"{host_header}:{port}"

        ssl_ctx = shared_ssl_context() if is_tls else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=ssl_ctx,
                server_hostname=host if is_tls else None,
                limit=131072,
            ),
            timeout=timeout,
        )
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

            ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
            req_headers = {
                "Host": host_header,
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": ws_key,
                "Sec-WebSocket-Version": "13",
                **(headers or {}),
            }
            lines = [f"GET {path} HTTP/1.1"]
            lines.extend(f"{k}: {v}" for k, v in req_headers.items())
            lines.append("")
            lines.append("")
            writer.write("\r\n".join(lines).encode("ascii"))
            await asyncio.wait_for(writer.drain(), timeout=timeout)

            raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
            head_text = raw_head.decode("latin-1")
            head_lines = head_text.split("\r\n")
            if not head_lines:
                raise ColabWsError("WebSocket 握手响应为空")
            status_parts = head_lines[0].split(None, 2)
            if len(status_parts) < 2 or not status_parts[1].isdigit():
                raise ColabWsError(f"无效的 WebSocket 握手状态行: {head_lines[0]!r}")
            status_code = int(status_parts[1])
            if status_code in (401, 403):
                raise ColabAuthExpiredError(f"WebSocket 握手鉴权失败 (HTTP {status_code})")
            if status_code == 404:
                raise ColabSessionLostError("WebSocket Kernel 不存在 (HTTP 404)")
            if status_code != 101:
                raise ColabWsError(f"WebSocket 握手失败: {head_lines[0]}")

            resp_headers: dict[str, str] = {}
            for line in head_lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    resp_headers[k.strip().lower()] = v.strip()
            expected_accept = base64.b64encode(
                hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            if resp_headers.get("sec-websocket-accept") != expected_accept:
                raise ColabWsError("WebSocket 握手 Sec-WebSocket-Accept 校验失败")
            return cls(reader, writer)
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise ColabWsError("Connection was lost.")
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        header.extend(mask)
        frame = bytes(header) + _mask_bytes(payload, mask)
        async with self._write_lock:
            self.writer.write(frame)
            await self.writer.drain()

    async def send_text(self, text: str) -> None:
        await self._send_frame(0x1, text.encode("utf-8"))

    async def send_ping(self, data: bytes = b"") -> None:
        await self._send_frame(0x9, data[:125])

    async def send_pong(self, data: bytes = b"") -> None:
        await self._send_frame(0xA, data[:125])

    async def _read_single_frame(self) -> tuple[bool, int, bytes]:
        head = await self.reader.readexactly(2)
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = int.from_bytes(await self.reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await self.reader.readexactly(8), "big")
        if length > _MAX_WS_MESSAGE_BYTES:
            raise ColabWsError(f"WebSocket 帧过大 ({length} bytes)")
        mask_key = await self.reader.readexactly(4) if masked else b""
        payload = await self.reader.readexactly(length) if length else b""
        if masked and payload:
            payload = _mask_bytes(payload, mask_key)
        self.last_rx_mono = time.monotonic()
        return fin, opcode, payload

    async def recv_message(self) -> tuple[int, bytes]:
        frag_opcode: int | None = None
        chunks: list[bytes] = []
        total_len = 0
        while True:
            fin, opcode, payload = await self._read_single_frame()
            if opcode == 0x8:
                self.closed = True
                raise EOFError("WebSocket closed by server")
            if opcode == 0x9:
                with contextlib.suppress(Exception):
                    await self.send_pong(payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x0:
                if frag_opcode is None:
                    raise ColabWsError("收到意外的 WebSocket continuation 帧")
                total_len += len(payload)
                if total_len > _MAX_WS_MESSAGE_BYTES:
                    raise ColabWsError("WebSocket 分片消息超出大小限制")
                chunks.append(payload)
                if fin:
                    return frag_opcode, b"".join(chunks)
                continue
            if not fin:
                frag_opcode = opcode
                total_len = len(payload)
                chunks = [payload]
                continue
            return opcode, payload

    async def close(self) -> None:
        if self.closed:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._send_frame(0x8, (1000).to_bytes(2, "big")), timeout=1.0)
        self.closed = True
        self.writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer.wait_closed(), timeout=1.0)


def encode_jupyter_msg(channel: str, msg_type: str, content: dict[str, Any], session_id: str) -> tuple[str, str]:
    msg_id = str(uuid.uuid4())
    msg = {
        "header": {
            "msg_id": msg_id,
            "username": "username",
            "session": session_id,
            "date": datetime.now(timezone.utc).isoformat(),
            "msg_type": msg_type,
            "version": "5.3",
        },
        "msg_id": msg_id,
        "msg_type": msg_type,
        "parent_header": {},
        "metadata": {},
        "content": content,
        "buffers": [],
        "channel": channel,
    }
    return msg_id, json.dumps(msg, ensure_ascii=False)


def decode_jupyter_ws_frame(opcode: int, payload: bytes) -> dict[str, Any]:
    if opcode == 0x1:
        return json.loads(payload.decode("utf-8"))
    if opcode == 0x2:
        if len(payload) < 8:
            raise ValueError("Invalid binary Jupyter WS frame")
        nbufs = int.from_bytes(payload[:4], "big")
        if nbufs < 1 or len(payload) < 4 * (nbufs + 1):
            raise ValueError("Invalid binary Jupyter WS buffer count")
        json_start = int.from_bytes(payload[4:8], "big")
        json_stop = int.from_bytes(payload[8:12], "big") if nbufs >= 2 else len(payload)
        if not (0 <= json_start <= json_stop <= len(payload)):
            raise ValueError("Invalid binary Jupyter WS offsets")
        return json.loads(payload[json_start:json_stop].decode("utf-8"))
    raise ValueError(f"Unsupported WebSocket opcode: {opcode}")


def _extract_notebook_cells(nb_path: Path, env_vars: dict[str, str]) -> list[tuple[str, str]]:
    raw = json.loads(nb_path.read_text(encoding="utf-8"))
    title_re = re.compile(r"^\s*#\s*@title\s+(.*)", re.MULTILINE)
    prelude = ""
    if env_vars:
        lines = ["import os"]
        lines.extend(f"os.environ[{k!r}] = {v!r}" for k, v in env_vars.items())
        prelude = "\n".join(lines) + "\n"
    cells: list[tuple[str, str]] = []
    for cell in raw.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        if not source.strip():
            continue
        code = prelude + source
        title_m = title_re.search(code)
        ident = title_m.group(1).strip() if title_m else str(cell.get("id") or "")
        cells.append((ident, code))
    del raw
    return cells


def _format_iopub_output(msg: dict[str, Any]) -> bytes:
    header = msg.get("header") or {}
    msg_type = msg.get("msg_type") or header.get("msg_type") or ""
    content = msg.get("content") or {}
    if msg_type == "stream":
        text = content.get("text") or ""
        return text.encode("utf-8", errors="replace") if text else b""
    if msg_type in ("display_data", "execute_result"):
        data = content.get("data") or {}
        plain = data.get("text/plain")
        if isinstance(plain, str) and plain:
            return (plain + "\n").encode("utf-8", errors="replace")
        return b""
    if msg_type == "error":
        tb = content.get("traceback") or []
        if tb:
            return ("".join(str(line) for line in tb) + "\n").encode("utf-8", errors="replace")
        ename = content.get("ename", "Error")
        evalue = content.get("evalue", "")
        return f"{ename}: {evalue}\n".encode("utf-8", errors="replace")
    return b""


async def _run_execute_cell(
    ws: AsyncWebSocket,
    *,
    code: str,
    session_id: str,
    svc: Any,
    state: Any,
    st: dict[str, Any],
    protocol: NotebookProtocol,
    output_sink: Callable[[bytes], None],
    deadline: float | None,
    auto_restart: bool,
    idle_check_seconds: float,
    fast_probe_seconds: float,
    probe_fn: Callable[[Any, dict[str, Any], float], Awaitable[str]],
    check_remote_fn: Callable[[], Awaitable[str]],
    initially_ready: bool,
    ping_interval: float,
    pong_timeout: float,
    state_box: dict[str, Any],
) -> tuple[bool, bool, str | None]:
    """Execute one cell over WS while multiplexing ping/health/deadline timers."""
    msg_id, frame_json = encode_jupyter_msg(
        "shell",
        "execute_request",
        {
            "code": code,
            "silent": False,
            "store_history": True,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": True,
        },
        session_id,
    )
    await ws.send_text(frame_json)

    iopub_idle = False
    shell_reply: dict[str, Any] | None = None
    recv_task: asyncio.Task[tuple[int, bytes]] | None = None
    next_ping_at = time.monotonic() + ping_interval
    try:
        while True:
            now = time.monotonic()
            if deadline is not None and not st.get("ready"):
                if now >= deadline:
                    raise _ReadyDeadlineExceeded("等待 CT_READY 超时，前台连接已停止；可用 status 检查远端会话")

            if (now - ws.last_rx_mono) > pong_timeout:
                raise ColabWsError("Connection was lost.")

            if now >= next_ping_at:
                await ws.send_ping()
                next_ping_at = now + ping_interval

            if auto_restart and st.get("ready"):
                need_remote_check = False
                if idle_check_seconds > 0 and now >= state_box["next_idle_probe_at"]:
                    need_remote_check = True
                    state_box["next_idle_probe_at"] = now + min(idle_check_seconds, 60.0)
                if fast_probe_seconds > 0 and st.get("url") and now >= state_box["next_fast_probe_at"]:
                    probe_to = min(5.0, max(0.5, fast_probe_seconds))
                    code_str = await probe_fn(svc, st, probe_to)
                    now_after = time.monotonic()
                    state_box["next_fast_probe_at"] = now_after + fast_probe_seconds
                    if code_str.isdigit() and 200 <= int(code_str) < 500:
                        state_box["next_status_allowed_at"] = 0.0
                    elif now_after >= state_box["next_status_allowed_at"]:
                        need_remote_check = True
                if need_remote_check:
                    if await check_remote_fn() == "missing":
                        msg = (
                            f"[colabsvc] [auto-restart] 远端会话 '{svc.name}' 已不存在"
                            "（连接半开或边缘探针检出停机），正在立即断开并恢复\n"
                        ).encode("utf-8")
                        output_sink(msg)
                        return False, True, None
                    cooldown = min(idle_check_seconds, 60.0) if idle_check_seconds > 0 else 60.0
                    state_box["next_status_allowed_at"] = time.monotonic() + cooldown

            wakeups = [max(0.05, next_ping_at - time.monotonic()), max(0.05, (ws.last_rx_mono + pong_timeout) - time.monotonic())]
            if deadline is not None and not st.get("ready"):
                wakeups.append(max(0.0, deadline - time.monotonic()))
            if auto_restart and st.get("ready"):
                if idle_check_seconds > 0:
                    wakeups.append(max(0.0, state_box["next_idle_probe_at"] - time.monotonic()))
                if fast_probe_seconds > 0 and st.get("url"):
                    wakeups.append(max(0.0, state_box["next_fast_probe_at"] - time.monotonic()))
            wait_sec = max(0.05, min(wakeups))

            if recv_task is None:
                recv_task = asyncio.create_task(ws.recv_message())
            done, _ = await asyncio.wait({recv_task}, timeout=wait_sec)
            if not done:
                continue

            opcode, payload = recv_task.result()
            recv_task = None
            msg = decode_jupyter_ws_frame(opcode, payload)
            now_rx = time.monotonic()
            state_box["next_idle_probe_at"] = now_rx + idle_check_seconds
            state_box["next_fast_probe_at"] = now_rx + fast_probe_seconds

            channel = msg.get("channel") or "iopub"
            header = msg.get("header") or {}
            parent = msg.get("parent_header") or {}
            msg_type = msg.get("msg_type") or header.get("msg_type") or ""
            content = msg.get("content") or {}

            if channel == "iopub" and msg_type == "status":
                exec_state = content.get("execution_state")
                if exec_state in ("dead", "restarting"):
                    err_line = f"RuntimeError: Kernel {exec_state} before the code finished.\n".encode("utf-8")
                    output_sink(err_line)
                    protocol.feed(err_line)
                    raise ColabWsError(f"Kernel {exec_state} before the code finished.")

            parent_id = parent.get("msg_id")
            if channel == "shell" and parent_id == msg_id:
                shell_reply = msg
                if iopub_idle:
                    reply_status = (shell_reply.get("content") or {}).get("status")
                    return reply_status not in ("error", "abort"), False, reply_status
            elif channel == "iopub" and (parent_id == msg_id or (initially_ready and msg_type == "stream")):
                chunk = _format_iopub_output(msg)
                if chunk:
                    output_sink(chunk)
                    updates = {k: v for k, v in protocol.feed(chunk).items() if st.get(k) != v}
                    if updates:
                        st = await asyncio.to_thread(state.save, **updates)
                        state_box["st"] = st
                        if st.get("ready"):
                            state_box["reached_ready"] = True
                if parent_id == msg_id and msg_type == "status" and content.get("execution_state") == "idle":
                    iopub_idle = True
                    if shell_reply is not None:
                        reply_status = (shell_reply.get("content") or {}).get("status")
                        return reply_status not in ("error", "abort"), False, reply_status
    finally:
        if recv_task is not None and not recv_task.done():
            recv_task.cancel()
            with contextlib.suppress(BaseException):
                await recv_task


async def run_single_ws_session(
    svc: Any,
    state: Any,
    nb_path: Path,
    output_sink: Callable[[bytes], None],
    *,
    env: dict[str, str] | None = None,
    deadline: float | None = None,
    auto_restart: bool = False,
    idle_check_seconds: float = 300.0,
    fast_probe_seconds: float = 10.0,
    initially_ready: bool = False,
    probe_fn: Callable[[Any, dict[str, Any], float], Awaitable[str]],
    check_remote_fn: Callable[[], Awaitable[str]],
    refresh_token_fn: Callable[[], Awaitable[None]],
    conn_seq: int = 1,
    ping_interval: float = _DEFAULT_PING_INTERVAL,
    pong_timeout: float = _DEFAULT_PONG_TIMEOUT,
) -> tuple[int, bool, bool, str | None]:
    """Execute notebook over native WS and keep session alive, matching `_run_single_exec` contract."""
    reached_ready = bool(initially_ready)
    half_open_reclaimed = False
    protocol = NotebookProtocol()
    st = await asyncio.to_thread(state.save, ready=bool(initially_ready), exec_pid=None, exec_identity=None)
    state_box: dict[str, Any] = {
        "st": st,
        "reached_ready": reached_ready,
        "next_idle_probe_at": 0.0,
        "next_fast_probe_at": 0.0,
        "next_status_allowed_at": 0.0,
    }

    ws: AsyncWebSocket | None = None
    try:
        rec = load_colab_session_record(svc.name, account=svc.account, env=env)
        if rec is None:
            line = f"[colab] Session '{svc.name}' not found.\n".encode("utf-8")
            output_sink(line)
            protocol.feed(line)
            return 1, reached_ready, False, protocol.preflight_error or "Session not found."

        if is_token_near_expiry(rec):
            await refresh_token_fn()

        try:
            req_timeout = float((env or os.environ).get("REQUEST_TIMEOUT") or 60)
        except ValueError:
            req_timeout = 60.0

        for auth_attempt in range(2):
            try:
                base_url, token, kernel_id, session_id = await asyncio.to_thread(
                    ensure_kernel_sync,
                    svc.name,
                    svc.account,
                    env=env,
                    timeout=req_timeout,
                )
                ws_base = re.sub(r"^http", "ws", base_url.rstrip("/"), count=1)
                qs = urllib.parse.urlencode(
                    {
                        "session_id": session_id,
                        "token": token,
                        "colab-runtime-proxy-token": token,
                    }
                )
                ws_url = f"{ws_base}/api/kernels/{urllib.parse.quote(kernel_id, safe='')}/channels?{qs}"
                ws = await AsyncWebSocket.connect(
                    ws_url,
                    headers={
                        "X-Colab-Client-Agent": "colab-cli",
                        "X-Colab-Runtime-Proxy-Token": token,
                    },
                    timeout=min(30.0, req_timeout),
                )
                break
            except ColabAuthExpiredError:
                if auth_attempt == 0:
                    await refresh_token_fn()
                    continue
                raise
            except ColabSessionLostError as err:
                line = f"[colab] Session '{svc.name}' appears to be lost (404/401).\n".encode("utf-8")
                output_sink(line)
                protocol.feed(line)
                return 1, reached_ready, False, str(err)

        assert ws is not None
        st = await asyncio.to_thread(state.save, exec_pid=None, exec_identity=f"native-ws:{conn_seq}")
        state_box["st"] = st

        cells = _extract_notebook_cells(nb_path, {"CT_SERVICE": svc.name})
        if not cells:
            return 0, reached_ready, False, None

        now0 = time.monotonic()
        state_box.update(
            {
                "st": st,
                "reached_ready": reached_ready,
                "next_idle_probe_at": now0 + idle_check_seconds,
                "next_fast_probe_at": now0 + fast_probe_seconds,
                "next_status_allowed_at": 0.0,
            }
        )

        await _run_execute_cell(
            ws,
            code="import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')",
            session_id=session_id,
            svc=svc,
            state=state,
            st=state_box["st"],
            protocol=protocol,
            output_sink=output_sink,
            deadline=deadline,
            auto_restart=auto_restart,
            idle_check_seconds=idle_check_seconds,
            fast_probe_seconds=fast_probe_seconds,
            probe_fn=probe_fn,
            check_remote_fn=check_remote_fn,
            initially_ready=initially_ready,
            ping_interval=ping_interval,
            pong_timeout=pong_timeout,
            state_box=state_box,
        )
        st = state_box["st"]
        reached_ready = reached_ready or bool(state_box["reached_ready"]) or bool(st.get("ready"))

        cell_failed = False
        total = len(cells)
        for idx in range(total):
            ident, code = cells[idx]
            cells[idx] = ("", "")  # release cell code string immediately after reading
            header = f"[colab] Executing cell {idx + 1}/{total}{f' - {ident}' if ident else ''}...\n".encode("utf-8")
            output_sink(header)
            updates = {k: v for k, v in protocol.feed(header).items() if st.get(k) != v}
            if updates:
                st = await asyncio.to_thread(state.save, **updates)
                state_box["st"] = st
            ok_cell, reclaimed, _ = await _run_execute_cell(
                ws,
                code=code,
                session_id=session_id,
                svc=svc,
                state=state,
                st=state_box["st"],
                protocol=protocol,
                output_sink=output_sink,
                deadline=deadline,
                auto_restart=auto_restart,
                idle_check_seconds=idle_check_seconds,
                fast_probe_seconds=fast_probe_seconds,
                probe_fn=probe_fn,
                check_remote_fn=check_remote_fn,
                initially_ready=initially_ready,
                ping_interval=ping_interval,
                pong_timeout=pong_timeout,
                state_box=state_box,
            )
            st = state_box["st"]
            reached_ready = reached_ready or bool(state_box["reached_ready"]) or bool(st.get("ready"))
            if reclaimed:
                half_open_reclaimed = True
                break
            if not ok_cell:
                cell_failed = True
                break

        final_updates = protocol.feed(b"", final=True)
        if final_updates:
            st = await asyncio.to_thread(state.save, **final_updates)
            state_box["st"] = st
            if st.get("ready"):
                reached_ready = True
        preflight_error = (
            protocol.preflight_error
            if (protocol.preflight_error and not protocol.cell_started and not reached_ready)
            else None
        )
        if half_open_reclaimed:
            return 1, reached_ready, True, preflight_error
        if cell_failed or not st.get("ready"):
            return 1, reached_ready, False, preflight_error
        return 0, reached_ready, False, preflight_error
    except _ReadyDeadlineExceeded:
        raise
    except (EOFError, ColabWsError, OSError, ValueError) as err:
        st = state_box["st"]
        reached_ready = reached_ready or bool(state_box["reached_ready"]) or bool(st.get("ready"))
        err_line = f"{err}\n".encode("utf-8", errors="replace")
        output_sink(err_line)
        protocol.feed(err_line)
        final_updates = protocol.feed(b"", final=True)
        if final_updates:
            st = await asyncio.to_thread(state.save, **final_updates)
            state_box["st"] = st
            if st.get("ready"):
                reached_ready = True
        preflight_error = (
            protocol.preflight_error
            if (protocol.preflight_error and not protocol.cell_started and not reached_ready)
            else (None if (protocol.cell_started or reached_ready) else str(err))
        )
        return 1, reached_ready, half_open_reclaimed, preflight_error
    finally:
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
