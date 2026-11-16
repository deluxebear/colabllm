"""Standard-library HTTP/SSE server and security guards for ColabLLM Web UI."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from cli_support import CliError, UsageError, origin as parse_origin, safe_urlopen
from . import introspect, runner_bridge, state_store

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES: dict[str, str] = {
    "styles.css": "text/css; charset=utf-8",
    "copilot.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "copilot.js": "application/javascript; charset=utf-8",
    "copilot_studios.js": "application/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}
_PROXY_SAFE_TYPES = (
    "application/json",
    "text/event-stream",
    "application/x-ndjson",
    "text/plain",
    "application/octet-stream",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "audio/",
    "video/",
)
_PROXY_PASSTHROUGH_HEADERS = (
    "X-Audio-Sample-Rate",
    "X-Saved-Drive-Path",
    "X-Audio-Duration",
    "Content-Disposition",
    "ETag",
    "X-Request-Id",
    "X-Queue-Position",
    "X-Job-Id",
)
_MAX_BODY_BYTES = 16 * 1024 * 1024
_MAX_PROXY_BODY_BYTES = 128 * 1024 * 1024


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("colabllm")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
        if pyproject.is_file():
            import tomllib

            return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
        return "0.0.0"


def _extract_host_only(host_header: str) -> str:
    raw = (host_header or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return ""
        return raw[: end + 1]
    if raw.count(":") == 1:
        return raw.split(":", 1)[0]
    return raw


def _make_nonce() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


class WebConsoleServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    EVENTS_SNAPSHOT_TTL = 0.75
    MAX_EVENTS_SSE_STREAMS = 3
    MAX_AUX_SSE_STREAMS = 2
    MAX_TOTAL_SSE_STREAMS = 4

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        read_only: bool = False,
        default_account: str | None = None,
        allow_remote: bool = False,
    ) -> None:
        self.stop_event = threading.Event()
        self.read_only = bool(read_only)
        self.default_account = default_account
        self.allow_remote = bool(allow_remote)
        self.bootstrap_token = secrets.token_urlsafe(24)
        self.hmac_key = os.urandom(32)
        self.version = _package_version()
        self._events_lock = threading.Lock()
        self._events_compute_lock = threading.Lock()
        self._events_gen = 0
        self._events_cache: tuple[float, str, str] | None = None
        self._sse_lock = threading.Lock()
        self._events_sse: list[threading.Event] = []
        self._aux_sse: list[threading.Event] = []
        self.task_manager = runner_bridge.TaskManager(on_change=self.invalidate_events_snapshot)

        bound_host = server_address[0].strip().lower()
        self.wildcard_remote = bool(allow_remote and bound_host in ("0.0.0.0", "::"))
        self.allowed_hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
        if bound_host and not self.wildcard_remote:
            bare = bound_host.strip("[]")
            self.allowed_hosts.add(bare)
            if ":" in bare:
                self.allowed_hosts.add(f"[{bare}]")

        super().__init__(server_address, WebRequestHandler)

    def mint_session_cookie(self) -> tuple[str, str]:
        nonce = secrets.token_hex(16)
        sig = hmac.new(self.hmac_key, nonce.encode("ascii"), "sha256").hexdigest()
        return f"{nonce}.{sig}", self.derive_csrf(nonce)

    def derive_csrf(self, session_nonce: str) -> str:
        return hmac.new(self.hmac_key, f"csrf:{session_nonce}".encode("ascii"), "sha256").hexdigest()

    def verify_session_cookie(self, value: str | None) -> str | None:
        if not value or "." not in value:
            return None
        nonce, sig = value.split(".", 1)
        if not nonce or not sig:
            return None
        expected = hmac.new(self.hmac_key, nonce.encode("ascii"), "sha256").hexdigest()
        return nonce if hmac.compare_digest(sig, expected) else None

    def invalidate_events_snapshot(self) -> None:
        with self._events_lock:
            self._events_gen += 1
            self._events_cache = None

    def get_events_snapshot(self) -> tuple[str, str]:
        with self._events_compute_lock:
            while True:
                with self._events_lock:
                    now = time.monotonic()
                    if self._events_cache is not None:
                        cached_ts, encoded, digest = self._events_cache
                        if now - cached_ts < self.EVENTS_SNAPSHOT_TTL:
                            return encoded, digest
                    start_gen = self._events_gen

                catalog = introspect.get_service_catalog()
                payload = {
                    "instances": state_store.list_all_instances(catalog),
                    "tasks": self.task_manager.list_tasks(),
                }
                encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

                with self._events_lock:
                    if self._events_gen != start_gen:
                        continue
                    self._events_cache = (time.monotonic(), encoded, digest)
                    return encoded, digest

    def register_sse_stream(self, kind: str) -> threading.Event:
        """Register an active SSE stream and evict oldest surplus streams so HTTP/1.1 sockets stay free."""
        ev = threading.Event()
        to_close: list[threading.Event] = []
        with self._sse_lock:
            if kind == "events":
                self._events_sse.append(ev)
                while len(self._events_sse) > self.MAX_EVENTS_SSE_STREAMS:
                    to_close.append(self._events_sse.pop(0))
            else:
                self._aux_sse.append(ev)
                while len(self._aux_sse) > self.MAX_AUX_SSE_STREAMS:
                    to_close.append(self._aux_sse.pop(0))
            while (len(self._events_sse) + len(self._aux_sse)) > self.MAX_TOTAL_SSE_STREAMS:
                if self._events_sse and (kind != "events" or len(self._events_sse) > 1):
                    to_close.append(self._events_sse.pop(0))
                elif self._aux_sse:
                    to_close.append(self._aux_sse.pop(0))
                else:
                    break
        for old in to_close:
            old.set()
        return ev

    def unregister_sse_stream(self, kind: str, ev: threading.Event) -> None:
        with self._sse_lock:
            bucket = self._events_sse if kind == "events" else self._aux_sse
            if ev in bucket:
                bucket.remove(ev)

    def shutdown(self) -> None:
        self.stop_event.set()
        with self._sse_lock:
            active_streams = [*self._events_sse, *self._aux_sse]
            self._events_sse.clear()
            self._aux_sse.clear()
        for ev in active_streams:
            ev.set()
        super().shutdown()
        self.task_manager.shutdown_all()


class WebRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: WebConsoleServer

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default HTTP access log noise on stdout/stderr.
        return

    def finish(self) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            super().finish()

    def _add_common_headers(self, *, csp_nonce: str | None = None) -> None:
        self.send_header("Vary", "Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
        self.send_header(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=(), focus-without-user-activation=()",
        )
        if csp_nonce is not None:
            csp = "; ".join([
                "default-src 'self'",
                f"script-src 'nonce-{csp_nonce}' 'strict-dynamic' 'report-sample'",
                "style-src 'self'",
                "img-src 'self' data: blob:",
                "media-src 'self' data: blob:",
                "connect-src 'self'",
                "font-src 'self'",
                "object-src 'none'",
                "base-uri 'none'",
                "frame-ancestors 'none'",
                "form-action 'self'",
                "require-trusted-types-for 'script'",
            ])
            self.send_header("Content-Security-Policy", csp)

    def _send_json(self, status: int, payload: Any, *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if status >= 400:
            self.close_connection = True
        self.send_response(status)
        self._add_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if status >= 400:
            self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(
        self,
        status: int,
        html_text: str,
        *,
        csp_nonce: str,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        body = html_text.encode("utf-8")
        if status >= 400:
            self.close_connection = True
        self.send_response(status)
        self._add_common_headers(csp_nonce=csp_nonce)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if status >= 400:
            self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra_headers or ():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}
        try:
            length = int(raw_len)
        except ValueError:
            raise UsageError("无效的 Content-Length") from None
        if length < 0 or length > _MAX_BODY_BYTES:
            raise UsageError("请求体大小超出限制")
        if length == 0:
            return {}
        data = self.rfile.read(length)
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise UsageError("请求体必须是合法 JSON") from None
        if not isinstance(parsed, dict):
            raise UsageError("JSON 请求体必须是对象")
        return parsed

    def _read_raw_body(self, max_bytes: int = _MAX_BODY_BYTES) -> bytes:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return b""
        try:
            length = int(raw_len)
        except ValueError:
            raise UsageError("无效的 Content-Length") from None
        if length < 0 or length > max_bytes:
            raise UsageError("请求体大小超出限制")
        return self.rfile.read(length) if length else b""

    def _session_nonce(self) -> str | None:
        raw_cookie = self.headers.get("Cookie")
        if not raw_cookie:
            return None
        jar = http.cookies.SimpleCookie()
        with contextlib.suppress(http.cookies.CookieError):
            jar.load(raw_cookie)
        morsel = jar.get("colabllm_sid")
        return self.server.verify_session_cookie(morsel.value if morsel else None)

    def _check_origin_and_fetch_metadata(self, path: str) -> bool:
        host_only = _extract_host_only(self.headers.get("Host", ""))
        if not host_only:
            self._send_json(403, {"error": "Missing Host header"})
            return False
        if not self.server.wildcard_remote and host_only not in self.server.allowed_hosts:
            self._send_json(403, {"error": "Forbidden host"})
            return False

        # Reject disallowed cross-site requests BEFORE authentication checks to prevent timing leaks.
        sec_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        sec_mode = (self.headers.get("Sec-Fetch-Mode") or "").strip().lower()
        sec_dest = (self.headers.get("Sec-Fetch-Dest") or "").strip().lower()
        if sec_site and sec_site not in ("same-origin", "none"):
            is_top_nav = (
                self.command in ("GET", "HEAD")
                and path in ("/", "/auth")
                and sec_mode == "navigate"
                and sec_dest not in ("object", "embed", "iframe")
            )
            if not is_top_nav:
                self._send_json(403, {"error": "Cross-site request blocked"})
                return False
        return True

    def _require_api_auth(self, *, require_csrf: bool = False, write_action: bool = False) -> str | None:
        nonce = self._session_nonce()
        if nonce is None:
            self._send_json(401, {"error": "未授权：请使用终端打印的控制台链接打开页面"})
            return None
        if require_csrf:
            sent_csrf = (self.headers.get("X-ColabLLM-CSRF") or "").strip()
            expected_csrf = self.server.derive_csrf(nonce)
            if not sent_csrf or not hmac.compare_digest(sent_csrf, expected_csrf):
                self._send_json(403, {"error": "CSRF 校验失败，请刷新页面后重试"})
                return None
        if write_action and self.server.read_only:
            self._send_json(403, {"error": "当前处于只读模式 (--read-only)，禁止执行变更或读取密钥操作"})
            return None
        return nonce

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path or "/"
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if not self._check_origin_and_fetch_metadata(path):
            return

        try:
            if path == "/auth" and method == "GET":
                self._handle_auth(query)
                return

            if path.startswith("/static/") and method in ("GET", "HEAD"):
                self._handle_static(path[len("/static/") :])
                return

            if path == "/" and method in ("GET", "HEAD"):
                self._handle_index()
                return

            if path == "/api/bootstrap" and method == "GET":
                if self._require_api_auth() is None:
                    return
                self._send_json(200, self._build_bootstrap())
                return

            if path == "/api/events" and method == "GET":
                if self._require_api_auth() is None:
                    return
                self._handle_events_sse()
                return

            if path.startswith("/api/services/") and path.endswith("/schema") and method == "GET":
                if self._require_api_auth() is None:
                    return
                svc_name = urllib.parse.unquote(path[len("/api/services/") : -len("/schema")].strip("/"))
                self._send_json(200, introspect.get_service_schema(svc_name))
                return

            if path.startswith("/api/instances/"):
                self._handle_instances_route(method, path[len("/api/instances/") :], query)
                return

            if path == "/api/tasks/preview" and method == "POST":
                if self._require_api_auth(require_csrf=True, write_action=False) is None:
                    return
                body = self._read_json_body()
                svc_name = str(body.get("service") or "").strip()
                subcommand = str(body.get("subcommand") or "").strip()
                _, preview = introspect.build_cli_argv(
                    svc_name,
                    subcommand,
                    instance=body.get("instance"),
                    account=body.get("account"),
                    values=body.get("values") or {},
                )
                self._send_json(200, {"ok": True, "command": preview})
                return

            if path == "/api/tasks/run" and method == "POST":
                if self._require_api_auth(require_csrf=True, write_action=True) is None:
                    return
                body = self._read_json_body()
                svc_name = str(body.get("service") or "").strip()
                subcommand = str(body.get("subcommand") or "").strip()
                argv, preview = introspect.build_cli_argv(
                    svc_name,
                    subcommand,
                    instance=body.get("instance"),
                    account=body.get("account") or self.server.default_account,
                    values=body.get("values") or {},
                )
                catalog = introspect.get_service_catalog()
                task = self.server.task_manager.start_task(
                    service_name=svc_name,
                    script_path=catalog[svc_name]["script_path"],
                    subcommand=subcommand,
                    argv=argv,
                    command_preview=preview,
                )
                self.server.invalidate_events_snapshot()
                self._send_json(202, task)
                return

            if path.startswith("/api/tasks/"):
                self._handle_tasks_route(method, path[len("/api/tasks/") :])
                return

            if path == "/api/doctor" and method == "GET":
                if self._require_api_auth() is None:
                    return
                online = _truthy(query.get("online", ["0"])[0])
                no_drive = _truthy(query.get("no_drive", ["0"])[0])
                acct = query.get("account", [""])[0].strip() or self.server.default_account
                drv_acct = query.get("drive_account", [""])[0].strip() or None
                transport = query.get("transport", [""])[0].strip() or None
                timeout_raw = query.get("timeout", ["10"])[0].strip() or "10"
                try:
                    timeout_val = float(timeout_raw)
                except ValueError:
                    raise UsageError("timeout 必须是数字") from None
                report = runner_bridge.run_doctor(
                    online=online,
                    account=acct,
                    drive_account=drv_acct,
                    transport=transport,
                    no_drive=no_drive,
                    timeout=timeout_val,
                )
                self._send_json(200, report)
                return

            if path == "/api/accounts/usage" and method == "GET":
                if self._require_api_auth() is None:
                    return
                refresh = _truthy(query.get("refresh", ["0"])[0])
                snap = runner_bridge.fetch_accounts_usage(refresh=refresh)
                overview = state_store.accounts_overview(
                    default_account=self.server.default_account,
                    usage_snapshot=snap,
                )
                self._send_json(200, overview)
                return

            if path == "/api/tunnel":
                if method == "GET":
                    if self._require_api_auth() is None:
                        return
                    self._send_json(200, state_store.tunnel_overview())
                    return
                if method == "POST":
                    if self._require_api_auth(require_csrf=True, write_action=True) is None:
                        return
                    body = self._read_json_body()
                    res = state_store.update_tunnel_config(
                        base_domain=body.get("base_domain"),
                        clear=bool(body.get("clear")),
                    )
                    self._send_json(200, res)
                    return

            if path == "/api/frp-status" and method == "GET":
                if self._require_api_auth() is None:
                    return
                self._send_json(200, runner_bridge.run_frp_status())
                return

            if path.startswith("/api/proxy/") and method in ("GET", "HEAD", "POST", "PUT", "DELETE"):
                require_csrf = method in ("POST", "PUT", "DELETE")
                if self._require_api_auth(require_csrf=require_csrf, write_action=True) is None:
                    return
                self._handle_proxy(method, path[len("/api/proxy/") :], parsed.query)
                return

            self._send_json(404, {"error": f"未知接口路径: {path}"})
        except UsageError as err:
            self._send_json(400, {"error": str(err)})
        except CliError as err:
            self._send_json(502 if err.code not in (2,) else 400, {"error": str(err), "code": err.code})
        except (ValueError, RuntimeError) as err:
            self._send_json(500, {"error": str(err)})
        except BrokenPipeError:
            pass

    def _handle_auth(self, query: dict[str, list[str]]) -> None:
        token = (query.get("token") or [""])[0].strip()
        if not token or not hmac.compare_digest(token, self.server.bootstrap_token):
            nonce = _make_nonce()
            self._send_html(
                403,
                _render_auth_gate_html("访问令牌无效或缺失，请重新复制终端输出的完整控制台地址。"),
                csp_nonce=nonce,
            )
            return
        sid, _ = self.server.mint_session_cookie()
        self.send_response(303)
        self._add_common_headers()
        self.send_header("Set-Cookie", f"colabllm_sid={sid}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_static(self, rel_name: str) -> None:
        if rel_name not in STATIC_FILES:
            self._send_json(404, {"error": "Static asset not found"})
            return
        asset_path = (STATIC_DIR / rel_name).resolve()
        if not asset_path.is_file() or not asset_path.is_relative_to(STATIC_DIR.resolve()):
            self._send_json(404, {"error": "Static asset not found"})
            return
        data = asset_path.read_bytes()
        self.send_response(200)
        self._add_common_headers()
        self.send_header("Content-Type", STATIC_FILES[rel_name])
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _handle_index(self) -> None:
        nonce = _make_nonce()
        session_nonce = self._session_nonce()
        if session_nonce is None:
            self._send_html(
                200,
                _render_auth_gate_html(""),
                csp_nonce=nonce,
            )
            return
        csrf = self.server.derive_csrf(session_nonce)
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("__CSP_NONCE__", nonce).replace("__CSRF_TOKEN__", csrf)
        html = html.replace("__READ_ONLY__", "true" if self.server.read_only else "false")
        self._send_html(200, html, csp_nonce=nonce)

    def _build_bootstrap(self) -> dict[str, Any]:
        catalog = introspect.get_service_catalog()
        instances = state_store.list_all_instances(catalog)
        accounts = state_store.accounts_overview(
            default_account=self.server.default_account,
            usage_snapshot=runner_bridge.get_cached_accounts_usage(),
        )
        tunnel = state_store.tunnel_overview()
        tasks = self.server.task_manager.list_tasks()
        services_list = sorted(catalog.values(), key=lambda item: item["name"])
        return {
            "version": self.server.version,
            "read_only": self.server.read_only,
            "default_account": self.server.default_account,
            "capability_surfaces": list(introspect.CAPABILITY_SURFACES),
            "services": services_list,
            "instances": instances,
            "accounts": accounts,
            "tunnel": tunnel,
            "tasks": tasks,
        }

    def _handle_events_sse(self) -> None:
        stream_stop = self.server.register_sse_stream("events")
        self.send_response(200)
        self._add_common_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        last_digest = ""
        ticks = 0
        try:
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while not self.server.stop_event.is_set() and not stream_stop.is_set():
                encoded, digest = self.server.get_events_snapshot()
                if digest != last_digest:
                    last_digest = digest
                    self.wfile.write(f"event: state\ndata: {encoded}\n\n".encode("utf-8"))
                    self.wfile.flush()
                elif ticks % 15 == 0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                ticks += 1
                if stream_stop.wait(1.0) or self.server.stop_event.is_set():
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.unregister_sse_stream("events", stream_stop)

    def _handle_instances_route(self, method: str, tail: str, query: dict[str, list[str]]) -> None:
        parts = [urllib.parse.unquote(p) for p in tail.strip("/").split("/") if p]
        if parts == ["prune"] and method == "POST":
            if self._require_api_auth(require_csrf=True, write_action=True) is None:
                return
            body = self._read_json_body()
            catalog = introspect.get_service_catalog()
            raw_replicas_only = body.get("replicas_only")
            replicas_only = True if raw_replicas_only is None else bool(raw_replicas_only)
            raw_svc = body.get("service")
            svc_filter = (str(raw_svc).strip() or None) if raw_svc is not None else None
            res = state_store.prune_stopped_instances(
                catalog,
                replicas_only=replicas_only,
                service_name=svc_filter,
            )
            self.server.invalidate_events_snapshot()
            self._send_json(200, res)
            return

        if len(parts) != 2:
            self._send_json(404, {"error": "无效的实例操作路径"})
            return
        svc_name, action = parts
        catalog = introspect.get_service_catalog()
        if svc_name not in catalog:
            raise UsageError(f"未知运行时服务: {svc_name}")
        script_path = catalog[svc_name]["script_path"]

        if action == "up" and method == "POST":
            if self._require_api_auth(require_csrf=True, write_action=True) is None:
                return
            body = self._read_json_body()
            if not body.get("account") and self.server.default_account:
                body["account"] = self.server.default_account
            argv, preview = introspect.build_up_argv(svc_name, body)
            if body.get("dry_run"):
                res = runner_bridge.run_dry_run(script_path, argv)
                self._send_json(200, {"dry_run": True, "command": preview, "output": res["output"]})
            else:
                task = self.server.task_manager.start_task(
                    service_name=svc_name,
                    script_path=script_path,
                    subcommand="up",
                    argv=argv,
                    command_preview=preview,
                )
                self.server.invalidate_events_snapshot()
                self._send_json(202, task)
            return

        if action == "down" and method == "POST":
            if self._require_api_auth(require_csrf=True, write_action=True) is None:
                return
            body = self._read_json_body()
            inst = str(body.get("instance") or "").strip()
            if inst == "default":
                inst = ""
            account = str(body.get("account") or "").strip()
            argv: list[str] = []
            if inst:
                from colabsvc import validate_instance

                argv.extend(["-i", validate_instance(inst)])
            if account:
                from colab_accounts import validate_account

                argv.extend(["--account", validate_account(account)])
            argv.append("down")
            if body.get("all"):
                argv.append("--all")
            if body.get("delete_tunnel"):
                argv.append("--delete-tunnel")
            preview = introspect.format_command_preview(svc_name, argv)
            task = self.server.task_manager.start_task(
                service_name=svc_name,
                script_path=script_path,
                subcommand="down",
                argv=argv,
                command_preview=preview,
            )
            self.server.invalidate_events_snapshot()
            self._send_json(202, task)
            return

        if action == "remove" and method == "POST":
            if self._require_api_auth(require_csrf=True, write_action=True) is None:
                return
            body = self._read_json_body()
            inst = str(body.get("instance") or "").strip()
            res = state_store.remove_stopped_instance(svc_name, inst, catalog)
            self.server.invalidate_events_snapshot()
            self._send_json(200, res)
            return

        if action == "probe" and method == "GET":
            if self._require_api_auth() is None:
                return
            inst = query.get("instance", [""])[0].strip()
            account = query.get("account", [""])[0].strip() or None
            report = runner_bridge.run_probe(script_path, instance_arg=inst, account=account)
            self._send_json(200, report)
            return

        if action == "secret" and method == "POST":
            if self._require_api_auth(require_csrf=True, write_action=True) is None:
                return
            body = self._read_json_body()
            inst = str(body.get("instance") or "").strip()
            self._send_json(200, state_store.reveal_secrets(svc_name, inst, catalog))
            return

        if action == "logs" and method == "GET":
            if self._require_api_auth() is None:
                return
            inst = query.get("instance", [""])[0].strip()
            lines_raw = query.get("lines", ["200"])[0].strip() or "200"
            try:
                lines = int(lines_raw)
            except ValueError:
                raise UsageError("lines 必须是整数") from None
            if _truthy(query.get("stream", ["0"])[0]):
                self._handle_logs_sse(svc_name, inst, lines, set(catalog.keys()))
            else:
                self._send_json(200, state_store.read_log_tail(svc_name, inst, lines, set(catalog.keys())))
            return

        self._send_json(404, {"error": f"不支持的实例接口: {action}"})

    def _handle_logs_sse(self, svc_name: str, inst: str, lines: int, known: set[str]) -> None:
        tail = state_store.read_log_tail(svc_name, inst, lines, known)
        log_path = Path(tail["log_path"])
        stream_stop = self.server.register_sse_stream("aux")

        self.send_response(200)
        self._add_common_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            self.wfile.write(b"retry: 4000\n\n")
            self.wfile.write(f"event: init\ndata: {json.dumps(tail, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

            offset = int(tail["size"])
            ticks = 0
            while not self.server.stop_event.is_set() and not stream_stop.is_set():
                if log_path.is_file():
                    size = log_path.stat().st_size
                    if size < offset:
                        offset = 0
                    if size > offset:
                        with log_path.open("rb") as handle:
                            handle.seek(offset)
                            raw = handle.read(min(size - offset, 65536))
                        offset += len(raw)
                        chunk_text = state_store.strip_ansi(raw.decode("utf-8", errors="replace"))
                        if chunk_text:
                            evt = json.dumps({"text": chunk_text, "offset": offset}, ensure_ascii=False)
                            self.wfile.write(f"event: chunk\ndata: {evt}\n\n".encode("utf-8"))
                            self.wfile.flush()
                if ticks % 30 == 0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                ticks += 1
                if stream_stop.wait(0.4) or self.server.stop_event.is_set():
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.unregister_sse_stream("aux", stream_stop)

    def _handle_tasks_route(self, method: str, tail: str) -> None:
        parts = [urllib.parse.unquote(p) for p in tail.strip("/").split("/") if p]
        if len(parts) == 2 and parts[1] == "cancel" and method == "POST":
            if self._require_api_auth(require_csrf=True, write_action=True) is None:
                return
            cancelled = self.server.task_manager.cancel(parts[0])
            self.server.invalidate_events_snapshot()
            self._send_json(200, cancelled)
            return

        if len(parts) == 2 and parts[1] == "stream" and method == "GET":
            if self._require_api_auth() is None:
                return
            task = self.server.task_manager.get_task(parts[0])
            if task is None:
                self._send_json(404, {"error": f"未找到任务: {parts[0]}"})
                return
            self._handle_task_sse(task)
            return

        if len(parts) == 1 and method == "GET":
            if self._require_api_auth() is None:
                return
            task = self.server.task_manager.get_task(parts[0])
            if task is None:
                self._send_json(404, {"error": f"未找到任务: {parts[0]}"})
                return
            with task.cond:
                self._send_json(200, task.summary())
            return

        self._send_json(404, {"error": "无效的任务接口"})

    def _handle_task_sse(self, task: runner_bridge.ManagedTask) -> None:
        stream_stop = self.server.register_sse_stream("aux")
        self.send_response(200)
        self._add_common_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        sent_chunks = 0
        try:
            self.wfile.write(b"retry: 4000\n\n")
            self.wfile.flush()
            while not self.server.stop_event.is_set() and not stream_stop.is_set():
                with task.cond:
                    if sent_chunks < len(task.chunks):
                        new_text = "".join(task.chunks[sent_chunks:])
                        sent_chunks = len(task.chunks)
                    else:
                        new_text = ""
                    status = task.status
                    summary = task.summary() if status != "running" else None
                    if not new_text and status == "running":
                        task.cond.wait(timeout=0.4)
                        continue
                if new_text:
                    evt = json.dumps({"text": new_text}, ensure_ascii=False)
                    self.wfile.write(f"event: chunk\ndata: {evt}\n\n".encode("utf-8"))
                    self.wfile.flush()
                if summary is not None:
                    self.wfile.write(f"event: done\ndata: {json.dumps(summary, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.unregister_sse_stream("aux", stream_stop)

    def _handle_proxy(self, method: str, tail: str, raw_query: str) -> None:
        parts = tail.lstrip("/").split("/", 2)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise UsageError("代理路径格式应为 /api/proxy/<service>/<instance>/<path>")
        svc_name = urllib.parse.unquote(parts[0])
        inst_raw = urllib.parse.unquote(parts[1])
        subpath = parts[2].lstrip("/") if len(parts) > 2 else ""

        catalog = introspect.get_service_catalog()
        _, session_name, _, st = state_store.load_instance_state(svc_name, inst_raw, set(catalog))
        base_url = (st.get("url") or "").strip().rstrip("/")
        if not base_url:
            raise UsageError(f"实例 {session_name} 尚未记录公网地址 (url)")
        parse_origin(base_url)

        target_url = f"{base_url}/{subpath}" if subpath else f"{base_url}/"
        if raw_query:
            target_url = f"{target_url}?{raw_query}"

        req_headers: dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        }
        for hdr in ("Content-Type", "Accept", "Range", "If-None-Match"):
            val = self.headers.get(hdr)
            if val:
                req_headers[hdr] = val

        custom_auth = (self.headers.get("X-ColabLLM-Proxy-Authorization") or "").strip()
        auth_mode = catalog[svc_name]["auth"]
        if custom_auth:
            req_headers["Authorization"] = custom_auth
        elif st.get("api_token") and (
            auth_mode == "builtin" or subpath.startswith(("v1/", "api/")) or not st.get("auth_pass")
        ):
            req_headers["Authorization"] = f"Bearer {st['api_token']}"
        elif st.get("auth_user") and st.get("auth_pass"):
            creds = base64.b64encode(f"{st['auth_user']}:{st['auth_pass']}".encode("utf-8")).decode("ascii")
            req_headers["Authorization"] = f"Basic {creds}"
        elif st.get("api_token"):
            req_headers["Authorization"] = f"Bearer {st['api_token']}"

        body: bytes | None = None
        if method in ("POST", "PUT"):
            body = self._read_raw_body(max_bytes=_MAX_PROXY_BODY_BYTES)
            if (
                svc_name == "unsloth-studio"
                and subpath == "api/auth/login"
                and method == "POST"
                and st.get("login_user")
                and st.get("login_pass")
            ):
                should_inject = not body.strip()
                if not should_inject:
                    with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                        parsed_body = json.loads(body.decode("utf-8"))
                        should_inject = isinstance(parsed_body, dict) and not parsed_body
                if should_inject:
                    body = json.dumps(
                        {"username": st["login_user"], "password": st["login_pass"]},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(target_url, data=body, headers=req_headers, method=method)
        head_only = method == "HEAD"

        try:
            with safe_urlopen(req, timeout=180) as resp:
                self._stream_proxy_response(
                    resp.status,
                    resp.headers.get("Content-Type", ""),
                    resp,
                    head_only=head_only,
                )
        except urllib.error.HTTPError as err:
            raw_err = b"" if head_only else err.read()
            ct = _sanitize_proxy_content_type(err.headers.get("Content-Type", "text/plain; charset=utf-8"))
            self.send_response(err.code)
            self._add_common_headers()
            self.send_header("Content-Type", ct)
            self.send_header("Content-Security-Policy", "sandbox")
            for hdr in _PROXY_PASSTHROUGH_HEADERS:
                val = err.headers.get(hdr)
                if val:
                    self.send_header(hdr, val)
            if not head_only:
                self.send_header("Content-Length", str(len(raw_err)))
            elif err.headers.get("Content-Length"):
                self.send_header("Content-Length", err.headers.get("Content-Length"))
            self.end_headers()
            if not head_only:
                self.wfile.write(raw_err)
        except urllib.error.URLError as err:
            raise CliError(f"远端代理请求失败: {err.reason}") from None

    def _stream_proxy_response(
        self,
        status: int,
        upstream_ct: str,
        resp: Any,
        *,
        head_only: bool = False,
    ) -> None:
        safe_ct = _sanitize_proxy_content_type(upstream_ct)
        base_ct = safe_ct.split(";", 1)[0].strip().lower()
        is_stream = base_ct in ("text/event-stream", "application/x-ndjson")
        self.send_response(status)
        self._add_common_headers()
        self.send_header("Content-Type", safe_ct)
        self.send_header("Content-Security-Policy", "sandbox")
        self.send_header("Cache-Control", "no-store")
        for hdr in _PROXY_PASSTHROUGH_HEADERS:
            val = resp.headers.get(hdr)
            if val:
                self.send_header(hdr, val)

        raw_cl = (resp.headers.get("Content-Length") or "").strip()
        if head_only:
            if raw_cl.isdigit():
                self.send_header("Content-Length", raw_cl)
            self.end_headers()
            return

        if is_stream:
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while not self.server.stop_event.is_set():
                line = resp.readline()
                if not line:
                    break
                self.wfile.write(line)
                self.wfile.flush()
        elif raw_cl.isdigit() and int(raw_cl) > 8 * 1024 * 1024:
            self.send_header("Content-Length", raw_cl)
            self.end_headers()
            while not self.server.stop_event.is_set():
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        else:
            data = resp.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


def _sanitize_proxy_content_type(content_type: str) -> str:
    lower = (content_type or "").split(";", 1)[0].strip().lower()
    for allowed in _PROXY_SAFE_TYPES:
        if lower == allowed or (allowed.endswith("/") and lower.startswith(allowed)):
            return content_type
    return "text/plain; charset=utf-8"


def _truthy(val: str) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _render_auth_gate_html(message: str) -> str:
    msg_html = f'<p class="auth-error">{_escape_html(message)}</p>' if message else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>ColabLLM Web 控制台 · 访问验证</title>
  <link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body class="auth-gate-body">
  <main class="auth-gate-card">
    <h1>ColabLLM Web 控制台</h1>
    <p>请输入终端启动时输出的一次性访问令牌（Token）以建立受保护的本地会话。</p>
    {msg_html}
    <form method="get" action="/auth" class="auth-gate-form">
      <label for="gate-token">启动令牌</label>
      <input id="gate-token" name="token" type="password" required autocomplete="off" autofocus placeholder="粘贴 token 参数值">
      <button type="submit" class="btn primary">进入控制台</button>
    </form>
  </main>
</body>
</html>
"""
