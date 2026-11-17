"""HTTP/1.1 data-plane server, staged timeout forwarding, symmetric model rewrite, and 0600 JSONL audit logging."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import http.server
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import ssl
import sys
import threading
import time
from typing import Any, Literal
import urllib.parse

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from http_client import BROWSER_UA  # noqa: E402
from paths import private_dir, user_home  # noqa: E402

from .auth import (  # noqa: E402
    GatewayError,
    TokenStore,
    inspect_and_prepare_request,
    parse_request_payload,
    validate_inbound_access,
)
from .catalog import (  # noqa: E402
    RuntimeGatewaySpec,
    all_root_post_routes,
    get_runtime_spec,
)
from .routing import (  # noqa: E402
    PREFIXED_ROUTE_RE,
    GatewayRouter,
    ModelTarget,
    ReplicaRuntime,
    build_upstream_connection,
    compute_upstream_auth_header,
    extract_affinity_key,
    INSTANCE_ID_RE,
)

_MAX_LOG_BYTES = 10 * 1024 * 1024
_LOG_BACKUPS = 3


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    allow_remote: bool = False
    allow_hosts: tuple[str, ...] = ()
    connect_timeout: float = 8.0
    prefill_timeout: float = 1800.0
    stream_idle_timeout: float = 120.0
    slot_wait_timeout: float = 5.0
    affinity_grace_timeout: float = 1.0
    max_concurrent: int = 32
    max_body_bytes: int = 96 * 1024 * 1024
    max_text_field_bytes: int = 1024 * 1024
    cooldown_seconds: float = 15.0
    probe_timeout: float = 3.0
    probe_ttl_seconds: float = 30.0
    max_affinity_entries: int = 1024
    state_root: Path | None = None
    services_dir: Path | None = None


@dataclass
class RequestContext:
    request_id: str
    client_ip: str
    token_id: str | None = None
    token_name: str | None = None
    method: str = ""
    raw_path: str = ""
    normalized_v1_path: str = ""
    pinned_runtime: str | None = None
    pinned_instance: str | None = None
    upstream_model: str | None = None
    requested_model: str | None = None
    parsed_body: Any | None = None
    is_multipart: bool = False
    raw_body: bytes = b""
    stream: bool = False
    content_type: str | None = None
    api_family: Literal["openai", "anthropic"] = "openai"


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler whose log and backup files are opened with 0600 permissions."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)


def _create_audit_logger(log_path: Path) -> tuple[logging.Logger, RotatingFileHandler]:
    private_dir(log_path.parent)
    handler = _PrivateRotatingFileHandler(
        str(log_path),
        maxBytes=_MAX_LOG_BYTES,
        backupCount=_LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(f"colabllm.gateway.{secrets.token_hex(6)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger, handler


def _is_anthropic_path(path: str) -> Literal["openai", "anthropic"]:
    if path == "/v1/messages" or path.startswith("/v1/messages/"):
        return "anthropic"
    return "openai"


def _rewrite_model_in_json_obj(obj: Any, requested_model: str) -> tuple[Any, bool]:
    """Symmetrically rewrite existing 'model' keys at root, response.model, or message.model."""
    if not isinstance(obj, dict):
        return obj, False
    changed = False
    if "model" in obj and isinstance(obj["model"], str):
        obj["model"] = requested_model
        changed = True
    resp = obj.get("response")
    if isinstance(resp, dict) and "model" in resp and isinstance(resp["model"], str):
        resp["model"] = requested_model
        changed = True
    msg = obj.get("message")
    if isinstance(msg, dict) and "model" in msg and isinstance(msg["model"], str):
        msg["model"] = requested_model
        changed = True
    return obj, changed


def _rewrite_sse_line(line_bytes: bytes, requested_model: str | None) -> bytes:
    """Symmetrically rewrite model fields inside SSE 'data: {json}' lines when requested_model is set."""
    if not requested_model:
        return line_bytes
    stripped = line_bytes.rstrip(b"\r\n")
    eol = line_bytes[len(stripped):]
    if not stripped.startswith(b"data:"):
        return line_bytes
    after = stripped[5:]
    leading_space = b" " if after.startswith(b" ") else b""
    payload_bytes = after[len(leading_space):]
    if payload_bytes == b"[DONE]" or not payload_bytes.startswith(b"{"):
        return line_bytes
    try:
        parsed = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return line_bytes
    updated, changed = _rewrite_model_in_json_obj(parsed, requested_model)
    if not changed:
        return line_bytes
    re_encoded = json.dumps(updated, ensure_ascii=False).encode("utf-8")
    return b"data:" + leading_space + re_encoded + eol


def _set_response_timeout(
    resp: http.client.HTTPResponse | None,
    conn: http.client.HTTPConnection | None,
    timeout: float,
) -> None:
    """Apply socket timeout to an HTTPResponse whose owning socket may have detached from HTTPConnection."""
    sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    if sock is None and conn is not None:
        sock = conn.sock
    if sock is not None:
        sock.settimeout(timeout)


def _is_pre_response_retryable_exception(err: BaseException) -> bool:
    return isinstance(
        err,
        (
            OSError,
            TimeoutError,
            http.client.HTTPException,
            ssl.SSLError,
        ),
    )


def _read_fresh_replica_auth(rep: ReplicaRuntime) -> tuple[str | None, str | None, dict[str, Any]]:
    try:
        loaded = json.loads(rep.state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return rep.auth_header, rep.url, rep.env
    if not isinstance(loaded, dict):
        return rep.auth_header, rep.url, rep.env
    env = loaded.get("env") if isinstance(loaded.get("env"), dict) else rep.env
    url = str(loaded.get("url") or rep.url).strip()
    return compute_upstream_auth_header(loaded), url, env


class GatewayRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP/1.1 handler for colabllm gateway."""

    protocol_version = "HTTP/1.1"
    server: "GatewayHTTPServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def handle_expect_100(self) -> bool:
        # Defer 100-continue until after Host, Sec-Fetch-Site, Token, Route, and Content-Length checks pass.
        return True

    def _send_json_response(
        self,
        status: int,
        payload: Any,
        *,
        request_id: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Request-Id", request_id)
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()
        self.close_connection = True

    def _send_gateway_error(self, err: GatewayError, *, request_id: str, api_family: str) -> None:
        self._send_json_response(
            err.status,
            err.to_payload(api_family),
            request_id=request_id,
            extra_headers=err.headers,
        )

    def _read_bounded_body(self) -> bytes:
        has_cl = "Content-Length" in self.headers
        has_te = "Transfer-Encoding" in self.headers
        if has_cl and has_te:
            raise GatewayError(400, "拒绝同时包含 Content-Length 与 Transfer-Encoding 的请求")

        max_bytes = self.server.config.max_body_bytes
        if has_te:
            te_val = (self.headers.get("Transfer-Encoding") or "").strip().lower()
            if "chunked" not in te_val:
                raise GatewayError(400, f"不支持的 Transfer-Encoding: {te_val!r}")
            expect_val = (self.headers.get("Expect") or "").strip().lower()
            if expect_val == "100-continue":
                self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                self.wfile.flush()
            return self._read_chunked_body(max_bytes)

        raw_cl = (self.headers.get("Content-Length") or "0").strip()
        try:
            content_length = int(raw_cl)
        except ValueError:
            raise GatewayError(400, f"无效的 Content-Length: {raw_cl!r}") from None
        if content_length < 0:
            raise GatewayError(400, f"Content-Length 不能为负数: {content_length}")
        if content_length > max_bytes:
            raise GatewayError(
                413,
                f"请求体大小 ({content_length} 字节) 超过上限 {max_bytes} 字节",
                oai_code="payload_too_large",
            )
        expect_val = (self.headers.get("Expect") or "").strip().lower()
        if expect_val == "100-continue":
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            self.wfile.flush()
        if content_length == 0:
            return b""
        remaining = content_length
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                raise GatewayError(400, "请求体读取提前结束（截断的连接）")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_chunked_body(self, max_bytes: int) -> bytes:
        total = 0
        out: list[bytes] = []
        while True:
            line = self.rfile.readline(4096)
            if not line or not line.endswith(b"\n"):
                raise GatewayError(400, "chunked 请求块长度行格式无效")
            size_hex = line.split(b";", 1)[0].strip()
            try:
                chunk_len = int(size_hex, 16)
            except ValueError:
                raise GatewayError(400, f"无效的 chunk 长度: {size_hex!r}") from None
            if chunk_len < 0:
                raise GatewayError(400, "chunk 长度不能为负")
            if chunk_len == 0:
                while True:
                    trailer = self.rfile.readline(4096)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += chunk_len
            if total > max_bytes:
                raise GatewayError(
                    413,
                    f"chunked 请求总大小超过上限 {max_bytes} 字节",
                    oai_code="payload_too_large",
                )
            data = self.rfile.read(chunk_len)
            if len(data) != chunk_len:
                raise GatewayError(400, "chunked 请求块数据提前截断")
            crlf = self.rfile.read(2)
            if crlf != b"\r\n":
                raise GatewayError(400, "chunked 请求块缺少结尾 CRLF")
            out.append(data)
        return b"".join(out)

    def _validate_framing_before_read(self) -> None:
        has_cl = "Content-Length" in self.headers
        has_te = "Transfer-Encoding" in self.headers
        if has_cl and has_te:
            raise GatewayError(400, "拒绝同时包含 Content-Length 与 Transfer-Encoding 的请求")
        if has_cl:
            raw_cl = (self.headers.get("Content-Length") or "").strip()
            try:
                cl = int(raw_cl)
            except ValueError:
                raise GatewayError(400, f"无效的 Content-Length: {raw_cl!r}") from None
            if cl < 0:
                raise GatewayError(400, f"Content-Length 不能为负数: {cl}")
            if cl > self.server.config.max_body_bytes:
                raise GatewayError(
                    413,
                    f"请求体大小 ({cl} 字节) 超过上限 {self.server.config.max_body_bytes} 字节",
                    oai_code="payload_too_large",
                )

    def _handle_all(self, method: str) -> None:
        self.close_connection = True
        raw_req_id = (self.headers.get("X-Request-Id") or "").strip()
        request_id = raw_req_id[:64] if raw_req_id else f"req_{secrets.token_hex(8)}"
        parsed_url = urllib.parse.urlsplit(self.path)
        clean_path = parsed_url.path or "/"
        client_ip = str(self.client_address[0]) if self.client_address else ""

        ctx = RequestContext(
            request_id=request_id,
            client_ip=client_ip,
            method=method,
            raw_path=self.path,
            normalized_v1_path=clean_path,
            api_family=_is_anthropic_path(clean_path),
        )
        start_ts = time.monotonic()
        status_out = 500
        ttfb_ms: float | None = None
        err_class: str | None = None

        try:
            # Step 1: Framing / dual length headers before reading
            self._validate_framing_before_read()

            # Step 2 & 3: Host, Sec-Fetch-Site, /healthz, status_token, client token
            auth_out = validate_inbound_access(
                method=method,
                path=clean_path,
                client_ip=client_ip,
                headers=self.headers,
                bound_host=self.server.config.host,
                allow_remote=self.server.config.allow_remote,
                allow_hosts=self.server.config.allow_hosts,
                status_token=self.server.status_token,
                token_store=self.server.token_store,
            )
            ctx.token_id = auth_out.token_id
            ctx.token_name = auth_out.token_name

            # Fast-path status/health metadata
            if method == "GET" and clean_path == "/healthz":
                status_out = 200
                self._send_json_response(200, {"status": "ok"}, request_id=request_id)
                return

            if method == "GET" and clean_path == "/gateway/status":
                snap = self.server.router.status_snapshot()
                status_out = 200
                self._send_json_response(200, snap, request_id=request_id)
                return

            # Step 4: Control/mutation route blocks and fast-path classification
            if clean_path == "/api/ct" or clean_path.startswith("/api/ct/"):
                raise GatewayError(
                    403,
                    "网关禁止访问内部控制路由 /api/ct/*",
                    oai_type="permission_error",
                    oai_code="policy_blocked",
                    anth_type="permission_error",
                )

            pref_match = PREFIXED_ROUTE_RE.match(clean_path)
            if pref_match:
                svc_part, subpath = pref_match.group(1), (pref_match.group(2) or "/")
                if subpath == "/api" or subpath.startswith("/api/"):
                    raise GatewayError(
                        403,
                        "网关禁止访问运行时内部管理路由 /r/<runtime>/api/*",
                        oai_type="permission_error",
                        oai_code="policy_blocked",
                        anth_type="permission_error",
                    )
                if method in ("POST", "DELETE") and (
                    subpath == "/v1/audio/voices" or subpath.startswith("/v1/audio/voices/")
                ):
                    raise GatewayError(
                        403,
                        "网关禁止创建或删除持久化音色",
                        oai_type="permission_error",
                        oai_code="policy_blocked",
                    )
                if method == "GET" and subpath == "/v1/audio/voices":
                    if ":" in svc_part:
                        svc, _, inst_raw = svc_part.partition(":")
                        if not inst_raw or (inst_raw != "default" and not INSTANCE_ID_RE.fullmatch(inst_raw)):
                            raise GatewayError(400, f"无效的实例前缀参数：{svc_part!r}")
                        pinned_inst: str | None = inst_raw
                    else:
                        svc = svc_part
                        pinned_inst = None
                    spec = get_runtime_spec(svc)
                    if spec is None or not spec.gateway_enabled:
                        raise GatewayError(
                            501,
                            f"运行时 {svc!r} 未注册或未开启网关暴露",
                            oai_code="service_unregistered",
                        )
                    if ("GET", "/v1/audio/voices") not in spec.prefixed_only_routes:
                        raise GatewayError(
                            403,
                            f"运行时 {svc!r} 未开放音色查询前缀路由",
                            oai_type="permission_error",
                            oai_code="route_forbidden",
                        )
                    ctx.pinned_runtime = svc
                    ctx.pinned_instance = pinned_inst
                    ctx.normalized_v1_path = "/v1/audio/voices"
                    rep = self.server.router.resolve_prefixed_voice_replica(svc, pinned_inst)
                    status_out, ttfb_ms, err_class = self._forward_simple_get(
                        ctx=ctx,
                        rep=rep,
                        upstream_path="/v1/audio/voices",
                    )
                    return
                raise GatewayError(
                    404,
                    f"网关不支持路径 {clean_path!r}（仅支持 /r/<tts>/v1/audio/voices 只读音色查询）",
                    oai_type="not_found_error",
                    oai_code="not_found",
                )

            if method in ("POST", "DELETE") and (
                clean_path == "/v1/audio/voices" or clean_path.startswith("/v1/audio/voices/")
            ):
                raise GatewayError(
                    403,
                    "网关禁止创建或删除持久化音色",
                    oai_type="permission_error",
                    oai_code="policy_blocked",
                )

            if method == "GET" and clean_path == "/v1/audio/voices":
                raise GatewayError(
                    404,
                    "请通过 /r/<runtime>[:<inst>]/v1/audio/voices 查询对应运行时的音色列表",
                    oai_type="not_found_error",
                    oai_code="not_found",
                )

            if method == "GET" and clean_path == "/v1/models":
                models_data = self.server.router.list_aggregated_models()
                status_out = 200
                self._send_json_response(
                    200,
                    {"object": "list", "data": models_data},
                    request_id=request_id,
                )
                return

            if method == "GET" and clean_path.startswith("/v1/models/"):
                raw_id = clean_path[len("/v1/models/"):]
                if not raw_id or "/" in raw_id:
                    raise GatewayError(404, f"未知的模型路径 {clean_path!r}", oai_type="not_found_error", oai_code="not_found")
                card = self.server.router.lookup_model_card(raw_id)
                status_out = 200
                self._send_json_response(200, card, request_id=request_id)
                return

            # Validate POST target path against union of gateway-enabled routes before sending 100 Continue / reading body.
            exact_posts, prefix_posts = all_root_post_routes()
            is_valid_post = method == "POST" and (
                clean_path in exact_posts or any(clean_path.startswith(pref) for pref in prefix_posts)
            )
            if not is_valid_post:
                raise GatewayError(
                    404,
                    f"网关不支持请求 {method} {clean_path}",
                    oai_type="not_found_error",
                    oai_code="not_found",
                    anth_type="not_found_error",
                )

            # Check sensitive Drive headers before reading body as well (prevents 100 Continue on blocked header)
            if self.headers.get("X-Save-Drive") or self.headers.get("X-Drive-Only"):
                raise GatewayError(
                    403,
                    "网关禁止携带 X-Save-Drive 或 X-Drive-Only 请求头",
                    oai_type="permission_error",
                    oai_code="drive_header_blocked",
                    anth_type="permission_error",
                    error_class="drive_header_blocked",
                )

            # Step 5: Bounded request read & parse
            raw_body = self._read_bounded_body()
            ctx.raw_body = raw_body
            prepared = parse_request_payload(
                method=method,
                headers=self.headers,
                raw_body=raw_body,
                max_text_field_bytes=self.server.config.max_text_field_bytes,
            )
            ctx.is_multipart = prepared.is_multipart
            ctx.parsed_body = prepared.parsed_body
            ctx.requested_model = prepared.requested_model
            ctx.stream = bool(isinstance(prepared.parsed_body, dict) and prepared.parsed_body.get("stream") is True)

            # Step 6: Resolve target model & runtime
            if not prepared.requested_model or "@" not in prepared.requested_model or prepared.requested_model.rpartition("@")[2].lower().startswith("sha256:"):
                hint_ids = [item["id"] for item in self.server.router.list_aggregated_models()]
            else:
                hint_ids = None
            target = self.server.router.parse_model_target(prepared.requested_model, candidates_hint=hint_ids)
            ctx.pinned_runtime = target.runtime
            ctx.pinned_instance = target.pinned_instance
            ctx.upstream_model = target.upstream_model

            spec = get_runtime_spec(target.runtime)
            assert spec is not None

            # Step 7: Policy guard, parameter rewrite, candidate discovery, concurrency & failover forwarding
            forward_bytes, forward_ctype = inspect_and_prepare_request(
                method=method,
                normalized_v1_path=clean_path,
                headers=self.headers,
                payload=prepared,
                spec=spec,
                upstream_model=target.upstream_model,
            )

            candidates = self.server.router.resolve_candidate_pool(
                target=target,
                method=method,
                normalized_v1_path=clean_path,
                parsed_body=prepared.parsed_body,
            )

            affinity_key = extract_affinity_key(clean_path, prepared.parsed_body)
            status_out, ttfb_ms, err_class = self._execute_with_failover(
                ctx=ctx,
                target=target,
                candidates=candidates,
                affinity_key=affinity_key,
                forward_bytes=forward_bytes,
                forward_ctype=forward_ctype,
                upstream_path=clean_path,
                start_ts=start_ts,
            )
        except GatewayError as err:
            status_out = err.status
            err_class = err.error_class
            self._send_gateway_error(err, request_id=request_id, api_family=ctx.api_family)
        except Exception as err:
            status_out = 500
            err_class = type(err).__name__
            self._send_gateway_error(
                GatewayError(
                    500,
                    f"网关内部处理异常: {err}",
                    oai_type="server_error",
                    oai_code="internal_error",
                    anth_type="api_error",
                ),
                request_id=request_id,
                api_family=ctx.api_family,
            )
        finally:
            dur_ms = round((time.monotonic() - start_ts) * 1000.0, 2)
            self.server.write_audit_log(
                {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "request_id": ctx.request_id,
                    "client_address": ctx.client_ip,
                    "method": ctx.method,
                    "path": clean_path,
                    "token_id": ctx.token_id,
                    "token_name": ctx.token_name,
                    "runtime": ctx.pinned_runtime,
                    "instance": ctx.pinned_instance,
                    "requested_model": ctx.requested_model,
                    "upstream_model": ctx.upstream_model,
                    "status": status_out,
                    "duration_ms": dur_ms,
                    "ttfb_ms": ttfb_ms,
                    "error_class": err_class,
                }
            )

    def do_GET(self) -> None:
        self._handle_all("GET")

    def do_POST(self) -> None:
        self._handle_all("POST")

    def do_DELETE(self) -> None:
        self._handle_all("DELETE")

    def do_PUT(self) -> None:
        self._handle_all("PUT")

    def do_PATCH(self) -> None:
        self._handle_all("PATCH")

    def do_HEAD(self) -> None:
        self._handle_all("HEAD")

    def _forward_simple_get(
        self,
        *,
        ctx: RequestContext,
        rep: ReplicaRuntime,
        upstream_path: str,
    ) -> tuple[int, float | None, str | None]:
        """Forward read-only GET requests (e.g. prefixed voices lookup) with 401 refresh support."""
        cfg = self.server.config
        auth_header = rep.auth_header
        for attempt in range(2):
            conn: http.client.HTTPConnection | None = None
            resp: http.client.HTTPResponse | None = None
            phase = "connect"
            try:
                conn, base_path = build_upstream_connection(rep.url, timeout=cfg.connect_timeout)
                req_target = f"{base_path}{upstream_path}" if base_path else upstream_path
                headers = {
                    "User-Agent": BROWSER_UA,
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "X-Request-Id": ctx.request_id,
                }
                if auth_header:
                    headers["Authorization"] = auth_header
                t0 = time.monotonic()
                conn.connect()
                if conn.sock is not None:
                    conn.sock.settimeout(max(cfg.connect_timeout, 30.0))
                conn.request("GET", req_target, headers=headers)
                phase = "prefill"
                if conn.sock is not None:
                    conn.sock.settimeout(cfg.prefill_timeout)
                resp = conn.getresponse()
                ttfb = round((time.monotonic() - t0) * 1000.0, 2)
                if resp.status == 401 and attempt == 0:
                    resp.read()
                    fresh_auth, fresh_url, _ = _read_fresh_replica_auth(rep)
                    if fresh_auth != auth_header:
                        auth_header = fresh_auth
                        rep.auth_header = fresh_auth
                        if fresh_url:
                            rep.url = fresh_url
                        continue
                if 300 <= resp.status < 400:
                    resp.read()
                    raise GatewayError(
                        502,
                        "上游返回意外的重定向响应",
                        oai_type="api_error",
                        oai_code="upstream_redirect",
                        anth_type="api_error",
                    )
                if resp.status == 524:
                    resp.read()
                    raise GatewayError(
                        504,
                        "Cloudflare Quick Tunnel 等待上游响应超时 (524)",
                        oai_type="timeout_error",
                        oai_code="cf_edge_524",
                        anth_type="api_error",
                        error_class="cf_edge_524",
                    )
                _set_response_timeout(resp, conn, cfg.stream_idle_timeout)
                body = resp.read()
                ctype = resp.getheader("Content-Type") or "application/json; charset=utf-8"
                self.send_response(resp.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("X-Request-Id", ctx.request_id)
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                ctx.pinned_instance = rep.instance
                return resp.status, ttfb, None if resp.status < 400 else f"upstream_{resp.status}"
            except GatewayError:
                raise
            except TimeoutError as err:
                if phase == "connect":
                    raise GatewayError(
                        502,
                        f"连接上游 {rep.key} 超时: {err}",
                        oai_type="api_error",
                        oai_code="upstream_error",
                        anth_type="api_error",
                    ) from err
                raise GatewayError(
                    504,
                    f"上游响应超时: {err}",
                    oai_type="timeout_error",
                    oai_code="prefill_timeout",
                    anth_type="api_error",
                    error_class="prefill_timeout",
                ) from err
            except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as err:
                raise GatewayError(
                    502,
                    f"连接上游 {rep.key} 失败: {err}",
                    oai_type="api_error",
                    oai_code="upstream_error",
                    anth_type="api_error",
                ) from err
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        raise GatewayError(502, "上游 401 重试耗尽", oai_type="api_error", oai_code="upstream_error")

    def _execute_with_failover(
        self,
        *,
        ctx: RequestContext,
        target: ModelTarget,
        candidates: list[ReplicaRuntime],
        affinity_key: str,
        forward_bytes: bytes,
        forward_ctype: str | None,
        upstream_path: str,
        start_ts: float,
    ) -> tuple[int, float | None, str | None]:
        """Acquire global + replica slots, handle 401 credential diff retry, pre-response failover, and streaming."""
        cfg = self.server.config
        self.server.router.acquire_global_slot()
        try:
            tried_keys: set[str] = set()
            wait_deadline = time.monotonic() + cfg.slot_wait_timeout
            last_err: GatewayError | None = None

            while len(tried_keys) < len(candidates):
                try:
                    rep, wait_deadline = self.server.router.acquire_replica_slot(
                        candidates=candidates,
                        runtime=target.runtime,
                        upstream_model=target.upstream_model,
                        affinity_key=affinity_key,
                        exclude_keys=tried_keys,
                        wait_deadline=wait_deadline,
                    )
                except GatewayError as err:
                    if last_err is not None and err.status in (502, 503):
                        raise last_err from None
                    raise

                tried_keys.add(rep.key)
                ctx.pinned_instance = rep.instance
                try:
                    result = self._attempt_single_replica(
                        ctx=ctx,
                        target=target,
                        rep=rep,
                        affinity_key=affinity_key,
                        forward_bytes=forward_bytes,
                        forward_ctype=forward_ctype,
                        upstream_path=upstream_path,
                        start_ts=start_ts,
                    )
                    return result
                except _RetryableReplicaFailure as retry_exc:
                    self.server.router.mark_cooldown(rep.key)
                    last_err = retry_exc.gateway_error
                    if target.pinned_instance is not None:
                        raise last_err from None
                    continue
                finally:
                    self.server.router.release_replica_slot(rep.key)

            assert last_err is not None
            raise last_err
        finally:
            self.server.router.release_global_slot()

    def _attempt_single_replica(
        self,
        *,
        ctx: RequestContext,
        target: ModelTarget,
        rep: ReplicaRuntime,
        affinity_key: str,
        forward_bytes: bytes,
        forward_ctype: str | None,
        upstream_path: str,
        start_ts: float,
    ) -> tuple[int, float | None, str | None]:
        cfg = self.server.config
        auth_header = rep.auth_header
        rep_url = rep.url

        for auth_attempt in range(2):
            conn: http.client.HTTPConnection | None = None
            resp: http.client.HTTPResponse | None = None
            headers_sent_to_client = False
            phase = "connect"
            try:
                conn, base_path = build_upstream_connection(rep_url, timeout=cfg.connect_timeout)
                req_target = f"{base_path}{upstream_path}" if base_path else upstream_path
                out_headers = {
                    "User-Agent": BROWSER_UA,
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "X-Request-Id": ctx.request_id,
                    "Content-Length": str(len(forward_bytes)),
                }
                if forward_ctype:
                    out_headers["Content-Type"] = forward_ctype
                if auth_header:
                    out_headers["Authorization"] = auth_header
                for passthrough_name in ("Accept",):
                    v = self.headers.get(passthrough_name)
                    if v:
                        out_headers[passthrough_name] = v

                # Stage 1: Connect using connect_timeout, then transmit bounded body
                conn.connect()
                if conn.sock is not None:
                    conn.sock.settimeout(max(cfg.connect_timeout, 30.0))
                conn.request(ctx.method, req_target, body=forward_bytes, headers=out_headers)

                # Stage 2: Prefill wait before response headers
                phase = "prefill"
                if conn.sock is not None:
                    conn.sock.settimeout(cfg.prefill_timeout)
                resp = conn.getresponse()
                ttfb_ms = round((time.monotonic() - start_ts) * 1000.0, 2)

                # Check 401 credential rotation once before switching/failing
                if resp.status == 401 and auth_attempt == 0:
                    err_body = resp.read()
                    fresh_auth, fresh_url, _ = _read_fresh_replica_auth(rep)
                    if fresh_auth != auth_header:
                        auth_header = fresh_auth
                        rep.auth_header = fresh_auth
                        if fresh_url:
                            rep_url = fresh_url
                            rep.url = fresh_url
                        continue
                    # Credential unchanged -> forward upstream 401 faithfully (no failover!)
                    return self._relay_non_streaming_response(
                        resp=resp,
                        pre_read_body=err_body,
                        ctx=ctx,
                        target=target,
                        rep=rep,
                        affinity_key=affinity_key,
                        ttfb_ms=ttfb_ms,
                    )

                if 300 <= resp.status < 400:
                    resp.read()
                    raise GatewayError(
                        502,
                        "上游返回意外的重定向响应，已截断以保护凭据",
                        oai_type="api_error",
                        oai_code="upstream_redirect",
                        anth_type="api_error",
                        error_class="upstream_redirect",
                    )

                if resp.status == 524:
                    resp.read()
                    raise GatewayError(
                        504,
                        "Cloudflare Quick Tunnel 等待首包响应超过约 100s (HTTP 524)；建议开启流式输出 stream: true 或改用 colabllm <svc> up --transport frp",
                        oai_type="timeout_error",
                        oai_code="cf_edge_524",
                        anth_type="api_error",
                        error_class="cf_edge_524",
                    )

                if resp.status in (502, 503, 530):
                    resp.read()
                    raise _RetryableReplicaFailure(
                        GatewayError(
                            502,
                            f"上游副本 {rep.key} 返回可重试状态 HTTP {resp.status}",
                            oai_type="api_error",
                            oai_code=f"upstream_{resp.status}",
                            anth_type="api_error",
                            error_class=f"upstream_{resp.status}",
                        )
                    )

                resp_ctype = (resp.getheader("Content-Type") or "").strip().lower()
                is_sse = resp_ctype.startswith("text/event-stream")
                if is_sse:
                    # Stage 3: Switch response socket timeout to stream_idle_timeout after headers arrive
                    phase = "stream"
                    _set_response_timeout(resp, conn, cfg.stream_idle_timeout)
                    self.send_response(resp.status)
                    self.send_header("Content-Type", resp.getheader("Content-Type") or "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.send_header("X-Request-Id", ctx.request_id)
                    self.end_headers()
                    headers_sent_to_client = True

                    stream_err: str | None = None
                    try:
                        while True:
                            line = resp.fp.readline()
                            if not line:
                                break
                            rewritten = _rewrite_sse_line(line, target.requested_model)
                            self.wfile.write(rewritten)
                            if line in (b"\n", b"\r\n"):
                                self.wfile.flush()
                        self.wfile.flush()
                    except TimeoutError:
                        stream_err = "stream_idle_timeout"
                        self._emit_sse_abort_event(
                            code="stream_idle_timeout",
                            message="上游流式输出空闲超时，连接已终止",
                            api_family=ctx.api_family,
                        )
                    except (BrokenPipeError, ConnectionResetError):
                        stream_err = "client_disconnected"
                    except (OSError, http.client.HTTPException, ssl.SSLError):
                        stream_err = "stream_aborted"
                        self._emit_sse_abort_event(
                            code="stream_aborted",
                            message="上游流式连接异常中断",
                            api_family=ctx.api_family,
                        )

                    if resp.status < 300 and stream_err is None:
                        self.server.router.touch_probe_ttl(rep)
                        self.server.router.record_affinity_success(
                            target.runtime, target.upstream_model, affinity_key, rep.key
                        )
                    return resp.status, ttfb_ms, stream_err

                # Non-streaming response (JSON / image / binary audio)
                phase = "body"
                _set_response_timeout(resp, conn, cfg.stream_idle_timeout)
                raw_payload = resp.read()
                return self._relay_non_streaming_response(
                    resp=resp,
                    pre_read_body=raw_payload,
                    ctx=ctx,
                    target=target,
                    rep=rep,
                    affinity_key=affinity_key,
                    ttfb_ms=ttfb_ms,
                )

            except (_RetryableReplicaFailure, GatewayError):
                raise
            except TimeoutError as err:
                if headers_sent_to_client:
                    return 200, None, "stream_idle_timeout"
                if phase == "connect":
                    raise _RetryableReplicaFailure(
                        GatewayError(
                            502,
                            f"连接上游 {rep.key} 超时: {err}",
                            oai_type="api_error",
                            oai_code="upstream_error",
                            anth_type="api_error",
                            error_class="connect_timeout",
                        )
                    ) from err
                raise GatewayError(
                    504,
                    "上游长上下文推理等待首包超时 (prefill_timeout)；建议开启 stream: true 或检查副本日志",
                    oai_type="timeout_error",
                    oai_code="prefill_timeout",
                    anth_type="api_error",
                    error_class="prefill_timeout",
                ) from err
            except (OSError, http.client.HTTPException, ssl.SSLError) as err:
                if headers_sent_to_client:
                    return 200, None, "stream_aborted"
                raise _RetryableReplicaFailure(
                    GatewayError(
                        502,
                        f"连接上游 {rep.key} 失败: {err}",
                        oai_type="api_error",
                        oai_code="upstream_error",
                        anth_type="api_error",
                        error_class="connect_error",
                    )
                ) from err
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        raise _RetryableReplicaFailure(
            GatewayError(502, f"上游 {rep.key} 认证刷新重试失败", oai_type="api_error", oai_code="upstream_error")
        )

    def _emit_sse_abort_event(self, *, code: str, message: str, api_family: str) -> None:
        try:
            err = GatewayError(504, message, oai_type="timeout_error", oai_code=code, anth_type="api_error")
            payload = json.dumps(err.to_payload(api_family), ensure_ascii=False).encode("utf-8")
            if api_family == "anthropic":
                self.wfile.write(b"event: error\ndata: " + payload + b"\n\n")
            else:
                self.wfile.write(b"data: " + payload + b"\n\ndata: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass

    def _relay_non_streaming_response(
        self,
        *,
        resp: http.client.HTTPResponse,
        pre_read_body: bytes,
        ctx: RequestContext,
        target: ModelTarget,
        rep: ReplicaRuntime,
        affinity_key: str,
        ttfb_ms: float,
    ) -> tuple[int, float, str | None]:
        raw_ctype = resp.getheader("Content-Type") or "application/octet-stream"
        ctype_low = raw_ctype.split(";", 1)[0].strip().lower()
        body_to_send = pre_read_body

        # Symmetric model rewrite ONLY for JSON responses that already include a 'model' key.
        if ctype_low == "application/json" and target.requested_model:
            try:
                parsed = json.loads(pre_read_body.decode("utf-8"))
                updated, changed = _rewrite_model_in_json_obj(parsed, target.requested_model)
                if changed:
                    body_to_send = json.dumps(updated, ensure_ascii=False).encode("utf-8")
            except (UnicodeDecodeError, ValueError):
                pass

        self.send_response(resp.status)
        self.send_header("Content-Type", raw_ctype)
        self.send_header("Content-Length", str(len(body_to_send)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Request-Id", ctx.request_id)
        for hdr_name in ("Content-Disposition",):
            hdr_val = resp.getheader(hdr_name)
            if hdr_val:
                self.send_header(hdr_name, hdr_val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body_to_send)
            self.wfile.flush()

        if 200 <= resp.status < 300:
            self.server.router.touch_probe_ttl(rep)
            self.server.router.record_affinity_success(
                target.runtime, target.upstream_model, affinity_key, rep.key
            )
            return resp.status, ttfb_ms, None
        return resp.status, ttfb_ms, f"upstream_{resp.status}"


class _RetryableReplicaFailure(Exception):
    def __init__(self, gateway_error: GatewayError) -> None:
        super().__init__(gateway_error.message)
        self.gateway_error = gateway_error


class GatewayHTTPServer(http.server.ThreadingHTTPServer):
    """Multi-threaded HTTP/1.1 gateway server with private JSONL audit logging."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        config: GatewayConfig | None = None,
        status_token: str | None = None,
    ) -> None:
        self.config = config or GatewayConfig(host=server_address[0], port=server_address[1])
        self.state_root = Path(self.config.state_root) if self.config.state_root is not None else user_home()
        self.gateway_dir = self.state_root / "gateway"
        self.token_store = TokenStore(self.gateway_dir)
        self.status_token = status_token or secrets.token_urlsafe(24)
        self.router = GatewayRouter(
            state_root=self.state_root,
            services_dir=self.config.services_dir,
            max_concurrent=self.config.max_concurrent,
            slot_wait_timeout=self.config.slot_wait_timeout,
            affinity_grace_timeout=self.config.affinity_grace_timeout,
            cooldown_seconds=self.config.cooldown_seconds,
            probe_timeout=self.config.probe_timeout,
            probe_ttl_seconds=self.config.probe_ttl_seconds,
            max_affinity_entries=self.config.max_affinity_entries,
        )
        self._audit_lock = threading.Lock()
        self._audit_logger, self._audit_handler = _create_audit_logger(self.gateway_dir / "gateway.log")
        bare_host = str(server_address[0]).strip().strip("[]")
        try:
            if ipaddress.ip_address(bare_host).version == 6:
                self.address_family = socket.AF_INET6
        except ValueError:
            pass
        super().__init__(server_address, GatewayRequestHandler)
        # Keep config.host/port aligned with actual bound address (supports port=0 ephemeral tests).
        self.config.host = str(self.server_address[0])
        self.config.port = int(self.server_address[1])

    def write_audit_log(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._audit_lock:
            self._audit_logger.info(line)
            self._audit_handler.flush()

    def server_close(self) -> None:
        try:
            with self._audit_lock:
                self._audit_handler.close()
                self._audit_logger.removeHandler(self._audit_handler)
        finally:
            super().server_close()
