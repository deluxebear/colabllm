"""ASGI wrapper for the SoMark OXR parse API on Colab (embedded by templates/build_notebooks.py).

Caddy authenticates every public request (Basic Auth / session cookie / ``Authorization: Bearer``)
and adds ``X-CT-Proxy-Auth: <secret>`` on those authenticated branches. SoMark's official SDK/CLI
cannot send headers; it only puts ``api_key`` into the form body of ``POST /v1/parse/*``. Caddy
forwards exactly those three POST routes without the trusted header, and this wrapper then requires
the form field ``api_key`` to equal the runtime API token (constant time, fail-closed).

The wrapper also adds ``GET /health`` (aggregated D-FINE + vLLM + API readiness) and redirects
``GET /`` to the bundled ``/demo`` UI. It imports only the standard library at module import so it
can be unit-tested locally; the upstream OXR app is imported lazily on the Colab VM.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

FORM_AUTH_PATHS = frozenset({"/v1/parse/sync", "/v1/parse/async", "/v1/parse/async_check"})
PROXY_HEADER = b"x-ct-proxy-auth"
MAX_FORM_AUTH_BODY = int(os.environ.get("CT_OXR_MAX_BODY_BYTES", str(512 * 1024 * 1024)))


def _header(scope, name: bytes) -> bytes:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value
    return b""


def _equal(given, expected: str) -> bool:
    if not expected or not isinstance(given, (str, bytes)):
        return False
    if isinstance(given, str):
        given = given.encode("utf-8", errors="replace")
    return secrets.compare_digest(given, expected.encode("utf-8"))


def _multipart_boundary(content_type: str) -> bytes:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "boundary":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            return value.encode("latin-1", errors="replace")
    return b""


def _part_field_name(headers: bytes):
    """Return (name, has_filename) from one multipart part header block."""
    for line in headers.split(b"\r\n"):
        key, _, value = line.partition(b":")
        if key.strip().lower() != b"content-disposition":
            continue
        name, has_filename = None, False
        for item in value.split(b";")[1:]:
            k, _, v = item.strip().partition(b"=")
            k = k.strip().lower()
            v = v.strip()
            if len(v) >= 2 and v[:1] == v[-1:] == b'"':
                v = v[1:-1]
            if k == b"name":
                name = v.decode("utf-8", errors="replace")
            elif k in (b"filename", b"filename*"):
                has_filename = True
        return name, has_filename
    return None, False


def extract_form_field(content_type: str, body: bytes, field: str = "api_key"):
    """Return the first non-file form value named ``field`` or ``None``.

    Supports ``multipart/form-data`` (SoMark parse uploads) and
    ``application/x-www-form-urlencoded`` (SoMark ``async_check``). Large file parts are scanned with
    ``bytes.find`` and never copied.
    """
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/x-www-form-urlencoded":
        try:
            values = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=1000)
        except (UnicodeDecodeError, ValueError):
            return None
        found = values.get(field)
        return found[0] if found else None
    if media_type != "multipart/form-data":
        return None
    boundary = _multipart_boundary(content_type)
    if not boundary or len(boundary) > 200:
        return None
    delimiter = b"--" + boundary
    position = body.find(delimiter)
    while position != -1:
        start = position + len(delimiter)
        if body[start:start + 2] == b"--":
            return None
        if body[start:start + 2] == b"\r\n":
            start += 2
        header_end = body.find(b"\r\n\r\n", start)
        if header_end == -1 or header_end - start > 16384:
            return None
        name, has_filename = _part_field_name(body[start:header_end])
        content_start = header_end + 4
        next_position = body.find(b"\r\n" + delimiter, content_start)
        if next_position == -1:
            return None
        if name == field and not has_filename:
            return body[content_start:next_position].decode("utf-8", errors="replace")
        position = next_position + 2
    return None


async def _send_json(send, status: int, payload: dict, extra_headers=()) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [(b"content-type", b"application/json; charset=utf-8"),
               (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store"), *extra_headers]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _probe(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read(65536)
            return response.status, raw
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (OSError, ValueError):
        return 0, b""


def upstream_health(dfine_url: str, vllm_url: str, model_name: str = "oxr") -> dict:
    """Return readiness of the D-FINE layout service and the vLLM OXR model service."""
    status = {"dfine": False, "vllm": False}
    code, raw = _probe(dfine_url.rstrip("/") + "/health")
    if code == 200:
        try:
            status["dfine"] = json.loads(raw).get("status") == "healthy"
        except (ValueError, AttributeError):
            status["dfine"] = False
    code, raw = _probe(vllm_url.rstrip("/") + "/models")
    if code == 200:
        try:
            status["vllm"] = any(item.get("id") == model_name for item in json.loads(raw).get("data") or ())
        except (ValueError, AttributeError, TypeError):
            status["vllm"] = False
    return status


class OxrAuthMiddleware:
    """Fail-closed SoMark ``api_key`` form authentication + /health + / -> /demo redirect."""

    def __init__(self, app, api_token: str, proxy_secret: str, *, dfine_url: str = "", vllm_url: str = "",
                 model_name: str = "oxr", max_body: int = MAX_FORM_AUTH_BODY, health=None):
        self.app = app
        self.api_token = api_token or ""
        self.proxy_secret = proxy_secret or ""
        self.dfine_url = dfine_url
        self.vllm_url = vllm_url
        self.model_name = model_name
        self.max_body = max_body
        self.health = health or (lambda: upstream_health(self.dfine_url, self.vllm_url, self.model_name))

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path, method = scope.get("path", ""), scope.get("method", "GET").upper()
        if path == "/health" and method in ("GET", "HEAD"):
            status = await asyncio.to_thread(self.health)
            ok = all(status.values())
            return await _send_json(send, 200 if ok else 503, {"ok": ok, "api": True, **status})
        if path == "/" and method in ("GET", "HEAD"):
            await send({"type": "http.response.start", "status": 302,
                        "headers": [(b"location", b"/demo"), (b"content-length", b"0")]})
            return await send({"type": "http.response.body", "body": b""})
        if path not in FORM_AUTH_PATHS or method != "POST":
            return await self.app(scope, receive, send)
        if self.proxy_secret and _equal(_header(scope, PROXY_HEADER), self.proxy_secret):
            return await self.app(scope, receive, send)
        if not self.api_token or not self.proxy_secret:
            return await _send_json(send, 401, {"code": 1107, "message": "API key authentication is not configured"})
        chunks, total = [], 0
        declared = _header(scope, b"content-length")
        if declared.isdigit() and int(declared) > self.max_body:
            return await _send_json(send, 413, {"code": 1133, "message": "Request body too large"})
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_body:
                return await _send_json(send, 413, {"code": 1133, "message": "Request body too large"})
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        content_type = _header(scope, b"content-type").decode("latin-1", errors="replace")
        if not _equal(extract_form_field(content_type, body), self.api_token):
            return await _send_json(send, 401, {"code": 1107, "message": "Invalid api_key"},
                                    extra_headers=[(b"www-authenticate", b'Bearer realm="oxr"')])
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


def create_app():
    from oxr.server.app import app as oxr_app  # imported on the Colab VM only

    return OxrAuthMiddleware(
        oxr_app,
        os.environ.get("CT_API_TOKEN", ""),
        os.environ.get("CT_OXR_PROXY_SECRET", ""),
        dfine_url=os.environ.get("OXR__LAYOUT_ANALYZE_MODEL__URL", "http://127.0.0.1:8091"),
        vllm_url=os.environ.get("OXR__OXR_MODEL__URL", "http://127.0.0.1:8090/v1"),
        model_name=os.environ.get("OXR__OXR_MODEL__MODEL_NAME", "oxr"),
    )


def __getattr__(name):
    if name == "app":
        value = create_app()
        globals()["app"] = value
        return value
    raise AttributeError(name)
