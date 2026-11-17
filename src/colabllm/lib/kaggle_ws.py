"""Pure-stdlib asyncio RFC 6455 WebSocket & Jupyter Kernel client for Kaggle sessions."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cli_support import safe_urlopen
from colab_ws import (
    AsyncWebSocket,
    ColabAuthExpiredError,
    ColabSessionLostError,
    ColabWsError,
    _format_iopub_output,
    decode_jupyter_ws_frame,
    encode_jupyter_msg,
)
from protocol import NotebookProtocol
from runtime_core import redact_runtime_secrets

_DEFAULT_PING_INTERVAL = 25.0
_DEFAULT_PONG_TIMEOUT = 90.0
_DEFAULT_PROBE_INTERVAL = 15.0

_RE_KGAT = re.compile(r"\bKGAT_[0-9a-fA-F]+\b")
_RE_KGRT = re.compile(r"\bKGRT_[A-Za-z0-9._-]+\b")
_RE_PROXY_SEG = re.compile(r"/k/(\d+)/(?!\[REDACTED\])([^/?#\s\"']+)")
_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_RE_QS_SECRET = re.compile(r"(?i)\b(token|jwt|key|secret|auth|sig|signature)=([^&#\s\"']+)")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")


class KaggleWsError(RuntimeError):
    """Base error raised by the native Kaggle Jupyter proxy HTTP/WebSocket client."""


class KaggleAuthError(KaggleWsError):
    """Kaggle Jupyter proxy rejected credentials with HTTP 401/403."""


class KaggleSessionLostError(KaggleWsError):
    """Kaggle Jupyter proxy or kernel returned HTTP 404/502/503/504 or was reclaimed."""


def redact_kaggle_proxy_secrets(text: Any) -> str:
    """Mask Kaggle tokens, OAuth tokens, JWTs, query secrets, and `/k/<id>/<seg>` paths."""
    if text is None:
        return ""
    out = str(text)
    out = _RE_KGAT.sub("KGAT_[REDACTED]", out)
    out = _RE_KGRT.sub("KGRT_[REDACTED]", out)
    out = _RE_PROXY_SEG.sub(r"/k/\1/[REDACTED]", out)
    out = _RE_JWT.sub("[REDACTED_JWT]", out)
    out = _RE_QS_SECRET.sub(r"\1=[REDACTED]", out)
    return redact_runtime_secrets(out)


def _kaggle_ext_config_dir(env: Mapping[str, str] | None = None) -> Path:
    src = env if env is not None else os.environ
    explicit = (src.get("KAGGLE_EXT_CONFIG_DIR") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = (src.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "kaggle-ext"
    home = (src.get("HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base / ".config" / "kaggle-ext"


def _read_sessions_map(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    nested = raw.get("sessions")
    source = nested if isinstance(nested, dict) else raw
    return {
        str(k): dict(v)
        for k, v in source.items()
        if isinstance(k, str) and k != "version" and isinstance(v, dict)
    }


def _resolve_account_label(
    account: str | None,
    cfg_dir: Path,
    env: Mapping[str, str] | None,
) -> tuple[str, bool]:
    src = env if env is not None else os.environ
    reg: dict[str, Any] = {}
    reg_path = cfg_dir / "accounts.json"
    if reg_path.is_file():
        with contextlib.suppress(OSError, UnicodeError, ValueError):
            loaded = json.loads(reg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                reg = loaded
    accounts_map = reg.get("accounts") if isinstance(reg.get("accounts"), dict) else {}

    def _lookup(raw_id: str) -> str | None:
        ident = raw_id.strip()
        if not ident:
            return None
        if ident in accounts_map:
            return ident
        needle = ident.lstrip("@").lower()
        matches = [
            str(k)
            for k, meta in accounts_map.items()
            if isinstance(meta, dict)
            and str(meta.get("username") or "").strip().lstrip("@").lower() == needle
        ]
        if len(matches) == 1:
            return matches[0]
        cleaned = ident.lstrip("@")
        if _SAFE_SEGMENT_RE.fullmatch(cleaned) and ".." not in cleaned:
            return cleaned
        return None

    if account and isinstance(account, str) and account.strip():
        resolved = _lookup(account)
        return (resolved or "default"), True

    for env_key in ("COLABLLM_KAGGLE_ACCOUNT", "KAGGLE_ACCOUNT"):
        env_val = (src.get(env_key) or "").strip()
        if env_val:
            resolved = _lookup(env_val)
            if resolved:
                return resolved, True

    active = str(reg.get("active") or "").strip()
    if active and ".." not in active and "/" not in active and "\\" not in active:
        return active, False
    return "default", False


def load_kaggle_session_record(
    session_name: str,
    account: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Read-only lookup of a `kaggle-ext` session record from `sessions.json`."""
    if not session_name or ".." in session_name or "/" in session_name or "\\" in session_name:
        return None
    cfg_dir = _kaggle_ext_config_dir(env)
    acct_label, pinned = _resolve_account_label(account, cfg_dir, env)

    candidates: list[Path] = []
    if acct_label == "external":
        ext_root = cfg_dir / "external"
        if ext_root.is_dir():
            candidates.extend(sorted(ext_root.glob("*/sessions.json")))
    else:
        candidates.append(cfg_dir / "accounts" / acct_label / "sessions.json")
        candidates.append(cfg_dir / "sessions.json")

    for path in candidates:
        rec = _read_sessions_map(path).get(session_name)
        if rec is not None:
            return rec

    if pinned:
        return None

    matches: list[dict[str, Any]] = []
    scan_paths: list[Path] = []
    acct_root = cfg_dir / "accounts"
    if acct_root.is_dir():
        scan_paths.extend(sorted(acct_root.glob("*/sessions.json")))
    ext_root = cfg_dir / "external"
    if ext_root.is_dir():
        scan_paths.extend(sorted(ext_root.glob("*/sessions.json")))

    seen: set[Path] = set()
    for path in scan_paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        rec = _read_sessions_map(path).get(session_name)
        if rec is not None:
            matches.append(rec)
    if len(matches) == 1:
        return matches[0]
    return None


def has_native_kaggle_session(
    session_name: str,
    account: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return True when `kaggle-ext` has live Jupyter proxy credentials for `session_name`."""
    rec = load_kaggle_session_record(session_name, account=account, env=env)
    if rec is None:
        return False
    state_val = str(rec.get("state") or "").strip().lower()
    if state_val in ("reclaimed", "logged_out", "stopped"):
        return False
    jurl = rec.get("jupyter_url")
    return isinstance(jurl, str) and jurl.strip().startswith(("http://", "https://"))


def _join_proxy_url(base_url: str, sub_path: str) -> str:
    parsed = urllib.parse.urlsplit(base_url.strip())
    base_path = parsed.path.rstrip("/")
    rel = sub_path.lstrip("/")
    joined_path = f"{base_path}/{rel}" if rel else (base_path or "/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, joined_path, parsed.query, ""))


def _ws_channels_url(base_url: str, kernel_id: str, client_session_id: str) -> str:
    parsed = urllib.parse.urlsplit(base_url.strip())
    ws_scheme = "wss" if parsed.scheme.lower() == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    chan_path = f"{base_path}/api/kernels/{urllib.parse.quote(kernel_id, safe='')}/channels"
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in params if k != "session_id"]
    filtered.append(("session_id", client_session_id))
    return urllib.parse.urlunsplit(
        (ws_scheme, parsed.netloc, chan_path, urllib.parse.urlencode(filtered), "")
    )


def _proxy_headers(rec: Mapping[str, Any]) -> dict[str, str]:
    tok = rec.get("proxy_token")
    if isinstance(tok, str) and tok.strip():
        return {"Authorization": f"Bearer {tok.strip()}"}
    return {}


def proxy_http_json(
    method: str,
    base_url: str,
    sub_path: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> Any:
    """Perform an HTTP JSON call against Kaggle's Jupyter proxy using shared stdlib TLS."""
    url = _join_proxy_url(base_url, sub_path)
    safe_url = redact_kaggle_proxy_secrets(url)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": "colabllm-kaggle-ws",
    }
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with safe_urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        detail = ""
        with contextlib.suppress(Exception):
            detail = redact_kaggle_proxy_secrets(err.read().decode("utf-8", errors="replace")[:300])
        if err.code in (401, 403):
            raise KaggleAuthError(f"Jupyter proxy auth failed (HTTP {err.code}) at {safe_url}") from err
        if err.code in (404, 502, 503, 504):
            raise KaggleSessionLostError(
                f"Jupyter proxy or kernel unavailable (HTTP {err.code}) at {safe_url}"
            ) from err
        raise KaggleWsError(f"Jupyter proxy HTTP {err.code} on {method} {safe_url}: {detail}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise KaggleWsError(
            f"Jupyter proxy request failed ({method} {safe_url}): {redact_kaggle_proxy_secrets(err)}"
        ) from err

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as err:
        raise KaggleWsError(f"Jupyter proxy returned invalid JSON from {safe_url}: {err}") from err


def ensure_kaggle_kernel_sync(
    session_name: str,
    account: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[str, dict[str, str], str, str]:
    """Resolve `(base_url, headers, kernel_id, client_session_id)` for a live Kaggle session."""
    rec = load_kaggle_session_record(session_name, account=account, env=env)
    if rec is None:
        raise KaggleSessionLostError(f"Kaggle session '{session_name}' not found in kaggle-ext sessions.json.")
    state_val = str(rec.get("state") or "").strip().lower()
    if state_val in ("reclaimed", "logged_out", "stopped"):
        raise KaggleSessionLostError(f"Kaggle session '{session_name}' state is {state_val!r}.")
    raw_url = rec.get("jupyter_url")
    if not isinstance(raw_url, str) or not raw_url.strip().startswith(("http://", "https://")):
        raise KaggleSessionLostError(f"Kaggle session '{session_name}' has no valid Jupyter proxy URL.")

    base_url = raw_url.strip().rstrip("/")
    headers = _proxy_headers(rec)
    saved_kernel = rec.get("kernel_id")
    saved_kid = str(saved_kernel).strip() if isinstance(saved_kernel, str) and saved_kernel.strip() else None
    saved_sid = rec.get("client_session_id")
    client_session_id = (
        str(saved_sid).strip()
        if isinstance(saved_sid, str) and saved_sid.strip()
        else str(uuid.uuid4())
    )

    kernel_id: str | None = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            if saved_kid:
                try:
                    info = proxy_http_json(
                        "GET",
                        base_url,
                        f"api/kernels/{urllib.parse.quote(saved_kid, safe='')}",
                        headers=headers,
                        timeout=timeout,
                    )
                    if isinstance(info, dict) and isinstance(info.get("id"), str) and info["id"].strip():
                        kernel_id = info["id"].strip()
                        break
                except KaggleSessionLostError:
                    saved_kid = None

            listed = proxy_http_json("GET", base_url, "api/kernels", headers=headers, timeout=timeout)
            if isinstance(listed, list):
                for item in listed:
                    if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                        kernel_id = item["id"].strip()
                        break
            if kernel_id:
                break

            created = proxy_http_json(
                "POST",
                base_url,
                "api/kernels",
                headers=headers,
                payload={"name": "python3"},
                timeout=timeout,
            )
            if isinstance(created, dict) and isinstance(created.get("id"), str) and created["id"].strip():
                kernel_id = created["id"].strip()
                break
            raise KaggleWsError("Kaggle Jupyter proxy did not return a kernel id")
        except (KaggleAuthError, KaggleSessionLostError):
            raise
        except KaggleWsError as err:
            last_err = err
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
    else:
        if last_err is not None:
            raise last_err

    if not kernel_id:
        raise KaggleWsError("Kaggle Jupyter proxy returned no kernel id")
    return base_url, headers, kernel_id, client_session_id


async def connect_kaggle_ws(
    ws_url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> AsyncWebSocket:
    """Connect pure-stdlib RFC 6455 WebSocket with `Origin: https://www.kaggle.com` retry on 403."""
    base_headers = dict(headers) if headers else {}

    async def _dial(hdrs: dict[str, str]) -> AsyncWebSocket:
        try:
            return await AsyncWebSocket.connect(ws_url, headers=hdrs, timeout=timeout)
        except ColabAuthExpiredError as err:
            raise KaggleAuthError(redact_kaggle_proxy_secrets(err)) from err
        except ColabSessionLostError as err:
            raise KaggleSessionLostError(redact_kaggle_proxy_secrets(err)) from err
        except ColabWsError as err:
            safe_msg = redact_kaggle_proxy_secrets(err)
            if re.search(r"\b(?:404|502|503|504)\b", safe_msg):
                raise KaggleSessionLostError(safe_msg) from err
            raise KaggleWsError(safe_msg) from err
        except (OSError, TimeoutError) as err:
            raise KaggleWsError(redact_kaggle_proxy_secrets(err)) from err

    try:
        return await _dial(base_headers)
    except KaggleAuthError as err:
        if "403" in str(err) and "Origin" not in base_headers:
            with_origin = {**base_headers, "Origin": "https://www.kaggle.com"}
            return await _dial(with_origin)
        raise


def _extract_kaggle_notebook_cells(nb_path: Path) -> list[tuple[str, str]]:
    raw = json.loads(nb_path.read_text(encoding="utf-8"))
    title_re = re.compile(r"^\s*#\s*@title\s+(.*)", re.MULTILINE)
    cells: list[tuple[str, str]] = []
    for idx, cell in enumerate(raw.get("cells", []), start=1):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(str(part) for part in source)
        elif not isinstance(source, str):
            source = str(source)
        if not source.strip():
            continue
        title_m = title_re.search(source)
        ident = title_m.group(1).strip() if title_m else str(cell.get("id") or f"cell-{idx}")
        cells.append((ident, source))
    return cells


async def _apply_protocol_chunk(
    state: Any,
    state_box: dict[str, Any],
    protocol: NotebookProtocol,
    chunk: bytes,
    *,
    final: bool = False,
) -> None:
    updates = protocol.feed(chunk, final=final)
    if not updates:
        return
    st = state_box["st"]
    save_kw: dict[str, Any] = {}
    if "url" in updates and updates["url"] != st.get("url"):
        save_kw["url"] = updates["url"]
    if "login_user" in updates and (
        updates["login_user"] != st.get("login_user") or updates["login_pass"] != st.get("login_pass")
    ):
        save_kw["login_user"] = updates["login_user"]
        save_kw["login_pass"] = updates["login_pass"]
    if "stage" in updates and updates["stage"] != st.get("last_stage"):
        save_kw["last_stage"] = updates["stage"]
    if "cell" in updates and updates["cell"] != st.get("last_cell"):
        save_kw["last_cell"] = updates["cell"]
    if updates.get("ready") and (not st.get("ready") or not st.get("ever_ready")):
        save_kw["ready"] = True
        save_kw["ever_ready"] = True
    if save_kw:
        st = await asyncio.to_thread(state.save, **save_kw)
        state_box["st"] = st
        if st.get("ready"):
            state_box["reached_ready"] = True
            if save_kw.get("ready") and state_box.get("probe_interval", 0.0) > 0:
                state_box["next_probe_at"] = time.monotonic() + float(state_box["probe_interval"])


async def _execute_cell_over_ws(
    ws: AsyncWebSocket,
    *,
    code: str,
    client_session_id: str,
    svc: Any,
    state: Any,
    state_box: dict[str, Any],
    protocol: NotebookProtocol,
    output_sink: Callable[[str], None],
    deadline: float | None,
    auto_restart: bool,
    idle_check_seconds: float,
    probe_interval: float,
    probe_fn: Callable[[Any, dict[str, Any], float], Awaitable[str]],
    uptime_exceeded_fn: Callable[[], bool],
    ping_interval: float,
    pong_timeout: float,
    silent: bool = False,
    store_history: bool = True,
) -> tuple[bool, str | None]:
    """Execute one Jupyter request over WS while multiplexing ping/uptime/HTTP-probe timers."""
    msg_id, frame_json = encode_jupyter_msg(
        "shell",
        "execute_request",
        {
            "code": code,
            "silent": silent,
            "store_history": store_history,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": True,
        },
        client_session_id,
    )
    await ws.send_text(frame_json)

    iopub_idle = False
    shell_reply: dict[str, Any] | None = None
    recv_task: asyncio.Task[tuple[int, bytes]] | None = None
    next_ping_at = time.monotonic() + ping_interval
    try:
        while True:
            now = time.monotonic()
            if uptime_exceeded_fn():
                return True, "max_uptime"
            st = state_box["st"]
            if deadline is not None and not st.get("ready") and now >= deadline:
                return False, "ready_timeout"
            if (now - ws.last_rx_mono) > pong_timeout:
                raise KaggleWsError("WebSocket connection lost (pong timeout).")
            if now >= next_ping_at:
                await ws.send_ping()
                next_ping_at = now + ping_interval

            if auto_restart and st.get("ready"):
                if probe_interval > 0 and st.get("url") and now >= state_box["next_probe_at"]:
                    probe_to = min(8.0, max(0.5, probe_interval))
                    code_str = await probe_fn(svc, st, probe_to)
                    state_box["next_probe_at"] = time.monotonic() + probe_interval
                    if not (code_str.isdigit() and 200 <= int(code_str) < 400):
                        return False, f"probe_failed:{code_str}"
                if idle_check_seconds > 0 and (time.monotonic() - state_box["last_output_mono"]) >= idle_check_seconds:
                    return True, "idle_timeout"

            wakeups = [
                max(0.05, next_ping_at - time.monotonic()),
                max(0.05, (ws.last_rx_mono + pong_timeout) - time.monotonic()),
            ]
            if deadline is not None and not st.get("ready"):
                wakeups.append(max(0.0, deadline - time.monotonic()))
            if auto_restart and st.get("ready"):
                if probe_interval > 0 and st.get("url"):
                    wakeups.append(max(0.0, state_box["next_probe_at"] - time.monotonic()))
                if idle_check_seconds > 0:
                    wakeups.append(max(0.0, (state_box["last_output_mono"] + idle_check_seconds) - time.monotonic()))
            if uptime_check := 1.0:
                wakeups.append(uptime_check)
            wait_sec = max(0.05, min(wakeups))

            if recv_task is None:
                recv_task = asyncio.create_task(ws.recv_message())
            done, _ = await asyncio.wait({recv_task}, timeout=wait_sec)
            if not done:
                continue

            opcode, payload = recv_task.result()
            recv_task = None
            msg = decode_jupyter_ws_frame(opcode, payload)
            channel = msg.get("channel") or "iopub"
            header = msg.get("header") or {}
            parent = msg.get("parent_header") or {}
            msg_type = msg.get("msg_type") or header.get("msg_type") or ""
            content = msg.get("content") or {}

            if channel == "iopub" and msg_type == "status":
                exec_state = content.get("execution_state")
                if exec_state in ("dead", "restarting"):
                    err_line = f"RuntimeError: Kernel {exec_state} before the code finished.\n"
                    if not silent:
                        output_sink(err_line)
                        await _apply_protocol_chunk(state, state_box, protocol, err_line.encode("utf-8"))
                    raise KaggleSessionLostError(err_line.strip())

            parent_id = parent.get("msg_id")
            if channel == "shell" and parent_id == msg_id:
                shell_reply = msg
                if iopub_idle:
                    reply_status = str((shell_reply.get("content") or {}).get("status") or "ok")
                    if reply_status in ("error", "abort"):
                        return False, "cell_error"
                    return True, None
            elif channel == "iopub" and (
                parent_id == msg_id or (not silent and not parent_id and msg_type == "stream")
            ):
                if not silent:
                    chunk_bytes = _format_iopub_output(msg)
                    if chunk_bytes:
                        state_box["last_output_mono"] = time.monotonic()
                        text = chunk_bytes.decode("utf-8", errors="replace")
                        output_sink(text)
                        await _apply_protocol_chunk(state, state_box, protocol, chunk_bytes)
                        if not protocol.cell_started and protocol.preflight_error:
                            return False, "preflight_error"
                if parent_id == msg_id and msg_type == "status" and content.get("execution_state") == "idle":
                    iopub_idle = True
                    if shell_reply is not None:
                        reply_status = str((shell_reply.get("content") or {}).get("status") or "ok")
                        if reply_status in ("error", "abort"):
                            return False, "cell_error"
                        return True, None
    finally:
        if recv_task is not None and not recv_task.done():
            recv_task.cancel()
            with contextlib.suppress(BaseException):
                await recv_task


async def run_single_kaggle_ws_session(
    svc: Any,
    state: Any,
    nb_path: Path,
    env_vars: Mapping[str, str],
    output_sink: Callable[[str], None],
    *,
    env: Mapping[str, str] | None = None,
    deadline: float | None = None,
    auto_restart: bool = False,
    idle_check_seconds: float = 300.0,
    probe_interval: float = _DEFAULT_PROBE_INTERVAL,
    probe_fn: Callable[[Any, dict[str, Any], float], Awaitable[str]],
    uptime_exceeded_fn: Callable[[], bool],
    refresh_status_fn: Callable[[], Awaitable[None]],
    conn_seq: int = 1,
    ping_interval: float = _DEFAULT_PING_INTERVAL,
    pong_timeout: float = _DEFAULT_PONG_TIMEOUT,
    initially_ready: bool = False,
) -> tuple[int, str]:
    """Inject env silently and run notebook cells over pure-stdlib Kaggle Jupyter WebSocket."""
    protocol = NotebookProtocol(provider="kaggle")
    st = await asyncio.to_thread(
        state.save,
        ready=bool(initially_ready),
        exec_pid=None,
        exec_identity=None,
    )
    now0 = time.monotonic()
    state_box: dict[str, Any] = {
        "st": st,
        "reached_ready": bool(initially_ready),
        "last_output_mono": now0,
        "probe_interval": float(probe_interval),
        "next_probe_at": now0 + probe_interval if probe_interval > 0 else now0,
    }
    acct = getattr(svc, "account", None) or st.get("account")
    req_src = env if env is not None else os.environ
    try:
        req_timeout = float(req_src.get("REQUEST_TIMEOUT") or 20.0)
    except ValueError:
        req_timeout = 20.0

    ws: AsyncWebSocket | None = None
    try:
        for auth_attempt in range(2):
            try:
                base_url, headers, kernel_id, client_session_id = await asyncio.to_thread(
                    ensure_kaggle_kernel_sync,
                    svc.name,
                    acct,
                    env=env,
                    timeout=req_timeout,
                )
                ws_url = _ws_channels_url(base_url, kernel_id, client_session_id)
                ws = await connect_kaggle_ws(ws_url, headers=headers, timeout=min(30.0, req_timeout))
                break
            except KaggleAuthError as err:
                if auth_attempt == 0:
                    output_sink("[kagglesvc] Jupyter proxy token rejected; refreshing via kaggle session status\n")
                    await refresh_status_fn()
                    continue
                output_sink(f"[kagglesvc] {redact_kaggle_proxy_secrets(err)}\n")
                return 1, "auth_error"
            except KaggleSessionLostError as err:
                msg = f"[kagglesvc] {redact_kaggle_proxy_secrets(err)}\n"
                output_sink(msg)
                await _apply_protocol_chunk(state, state_box, protocol, msg.encode("utf-8"))
                return 1, "session_lost"
            except KaggleWsError as err:
                msg = f"[kagglesvc] {redact_kaggle_proxy_secrets(err)}\n"
                output_sink(msg)
                await _apply_protocol_chunk(state, state_box, protocol, msg.encode("utf-8"))
                return 1, "ws_error"

        assert ws is not None
        st = await asyncio.to_thread(state.save, exec_pid=None, exec_identity=f"kaggle-ws:{conn_seq}")
        state_box["st"] = st

        if env_vars:
            prelude = (
                "import os as _ct_ke_os\n"
                f"_ct_ke_os.environ.update({dict(env_vars)!r})\n"
                "del _ct_ke_os"
            )
            ok_env, reason_env = await _execute_cell_over_ws(
                ws,
                code=prelude,
                client_session_id=client_session_id,
                svc=svc,
                state=state,
                state_box=state_box,
                protocol=protocol,
                output_sink=output_sink,
                deadline=deadline,
                auto_restart=auto_restart,
                idle_check_seconds=idle_check_seconds,
                probe_interval=probe_interval,
                probe_fn=probe_fn,
                uptime_exceeded_fn=uptime_exceeded_fn,
                ping_interval=ping_interval,
                pong_timeout=pong_timeout,
                silent=True,
                store_history=False,
            )
            if reason_env == "max_uptime":
                return 0, "max_uptime"
            if reason_env == "ready_timeout":
                return 124, "ready_timeout"
            if not ok_env:
                output_sink(f"[kagglesvc] environment prelude injection failed ({reason_env or 'error'})\n")
                return 1, "env_prelude_failed"

        cells = _extract_kaggle_notebook_cells(nb_path)
        if not cells:
            return 0, "eof"

        for ident, code in cells:
            ok_cell, reason = await _execute_cell_over_ws(
                ws,
                code=code,
                client_session_id=client_session_id,
                svc=svc,
                state=state,
                state_box=state_box,
                protocol=protocol,
                output_sink=output_sink,
                deadline=deadline,
                auto_restart=auto_restart,
                idle_check_seconds=idle_check_seconds,
                probe_interval=probe_interval,
                probe_fn=probe_fn,
                uptime_exceeded_fn=uptime_exceeded_fn,
                ping_interval=ping_interval,
                pong_timeout=pong_timeout,
                silent=False,
                store_history=True,
            )
            if reason in ("max_uptime", "idle_timeout"):
                await _apply_protocol_chunk(state, state_box, protocol, b"", final=True)
                return 0, reason
            if reason == "ready_timeout":
                await _apply_protocol_chunk(state, state_box, protocol, b"", final=True)
                return 124, "ready_timeout"
            if reason and reason.startswith("probe_failed:"):
                await _apply_protocol_chunk(state, state_box, protocol, b"", final=True)
                return 1, reason
            if reason in ("preflight_error", "cell_error"):
                await _apply_protocol_chunk(state, state_box, protocol, b"", final=True)
                return 1, reason
            if not ok_cell:
                await _apply_protocol_chunk(state, state_box, protocol, b"", final=True)
                return 1, reason or f"cell_failed:{ident}"

        await _apply_protocol_chunk(state, state_box, protocol, b"", final=True)
        return 0, "eof"
    except asyncio.CancelledError:
        raise
    except (EOFError, OSError, KaggleWsError, ColabWsError, ValueError) as err:
        err_line = f"[kagglesvc] {redact_kaggle_proxy_secrets(err)}\n"
        output_sink(err_line)
        await _apply_protocol_chunk(state, state_box, protocol, err_line.encode("utf-8"), final=True)
        if isinstance(err, KaggleSessionLostError):
            return 1, "session_lost"
        return 1, "ws_eof" if isinstance(err, EOFError) else "ws_error"
    finally:
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()


async def watch_kaggle_ws_iopub(
    svc: Any,
    state: Any,
    output_sink: Callable[[str], None],
    *,
    env: Mapping[str, str] | None = None,
    idle_check_seconds: float = 300.0,
    probe_interval: float = _DEFAULT_PROBE_INTERVAL,
    probe_fn: Callable[[Any, dict[str, Any], float], Awaitable[str]],
    uptime_exceeded_fn: Callable[[], bool],
    refresh_status_fn: Callable[[], Awaitable[None]],
    conn_seq: int = 1,
    ping_interval: float = _DEFAULT_PING_INTERVAL,
    pong_timeout: float = _DEFAULT_PONG_TIMEOUT,
) -> tuple[int, str]:
    """Attach a read-only monitor WebSocket with a fresh `session_id` to stream iopub output."""
    protocol = NotebookProtocol(provider="kaggle")
    st = state.load()
    now0 = time.monotonic()
    state_box: dict[str, Any] = {
        "st": st,
        "reached_ready": bool(st.get("ready")),
        "last_output_mono": now0,
        "next_probe_at": now0 + probe_interval if probe_interval > 0 else now0,
    }
    acct = getattr(svc, "account", None) or st.get("account")
    req_src = env if env is not None else os.environ
    try:
        req_timeout = float(req_src.get("REQUEST_TIMEOUT") or 20.0)
    except ValueError:
        req_timeout = 20.0

    ws: AsyncWebSocket | None = None
    recv_task: asyncio.Task[tuple[int, bytes]] | None = None
    try:
        for auth_attempt in range(2):
            try:
                base_url, headers, kernel_id, _ = await asyncio.to_thread(
                    ensure_kaggle_kernel_sync,
                    svc.name,
                    acct,
                    env=env,
                    timeout=req_timeout,
                )
                monitor_sid = str(uuid.uuid4())
                ws_url = _ws_channels_url(base_url, kernel_id, monitor_sid)
                ws = await connect_kaggle_ws(ws_url, headers=headers, timeout=min(30.0, req_timeout))
                break
            except KaggleAuthError as err:
                if auth_attempt == 0:
                    output_sink("[kagglesvc] monitor WS token rejected; refreshing via kaggle session status\n")
                    await refresh_status_fn()
                    continue
                output_sink(f"[kagglesvc] {redact_kaggle_proxy_secrets(err)}\n")
                return 1, "auth_error"
            except KaggleSessionLostError as err:
                output_sink(f"[kagglesvc] {redact_kaggle_proxy_secrets(err)}\n")
                return 1, "session_lost"
            except KaggleWsError as err:
                output_sink(f"[kagglesvc] {redact_kaggle_proxy_secrets(err)}\n")
                return 1, "ws_error"

        assert ws is not None
        st = await asyncio.to_thread(state.save, exec_pid=None, exec_identity=f"kaggle-ws-watch:{conn_seq}")
        state_box["st"] = st
        next_ping_at = time.monotonic() + ping_interval

        while True:
            now = time.monotonic()
            if uptime_exceeded_fn():
                return 0, "max_uptime"
            if (now - ws.last_rx_mono) > pong_timeout:
                raise KaggleWsError("Watch WebSocket timed out (pong timeout).")
            if now >= next_ping_at:
                await ws.send_ping()
                next_ping_at = now + ping_interval

            curr = state_box["st"]
            if curr.get("ready") and probe_interval > 0 and curr.get("url") and now >= state_box["next_probe_at"]:
                probe_to = min(8.0, max(0.5, probe_interval))
                code_str = await probe_fn(svc, curr, probe_to)
                state_box["next_probe_at"] = time.monotonic() + probe_interval
                if not (code_str.isdigit() and 200 <= int(code_str) < 400):
                    return 1, f"probe_failed:{code_str}"
            if idle_check_seconds > 0 and (time.monotonic() - state_box["last_output_mono"]) >= idle_check_seconds:
                return 0, "idle_timeout"

            wakeups = [
                max(0.05, next_ping_at - time.monotonic()),
                max(0.05, (ws.last_rx_mono + pong_timeout) - time.monotonic()),
                1.0,
            ]
            if curr.get("ready") and probe_interval > 0 and curr.get("url"):
                wakeups.append(max(0.0, state_box["next_probe_at"] - time.monotonic()))
            if idle_check_seconds > 0:
                wakeups.append(max(0.0, (state_box["last_output_mono"] + idle_check_seconds) - time.monotonic()))
            wait_sec = max(0.05, min(wakeups))

            if recv_task is None:
                recv_task = asyncio.create_task(ws.recv_message())
            done, _ = await asyncio.wait({recv_task}, timeout=wait_sec)
            if not done:
                continue

            opcode, payload = recv_task.result()
            recv_task = None
            msg = decode_jupyter_ws_frame(opcode, payload)
            channel = msg.get("channel") or "iopub"
            if channel != "iopub":
                continue
            header = msg.get("header") or {}
            msg_type = str(msg.get("msg_type") or header.get("msg_type") or "")
            content = msg.get("content") or {}

            if msg_type == "status":
                exec_state = content.get("execution_state")
                if exec_state in ("dead", "restarting"):
                    return 1, "kernel_dead"
                continue

            chunk_bytes = _format_iopub_output(msg)
            if chunk_bytes:
                state_box["last_output_mono"] = time.monotonic()
                text = chunk_bytes.decode("utf-8", errors="replace")
                output_sink(text)
                await _apply_protocol_chunk(state, state_box, protocol, chunk_bytes)
    except asyncio.CancelledError:
        raise
    except (EOFError, OSError, KaggleWsError, ColabWsError, ValueError) as err:
        err_line = f"[kagglesvc] {redact_kaggle_proxy_secrets(err)}\n"
        output_sink(err_line)
        return 1, "ws_eof" if isinstance(err, EOFError) else "ws_error"
    finally:
        if recv_task is not None and not recv_task.done():
            recv_task.cancel()
            with contextlib.suppress(BaseException):
                await recv_task
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
