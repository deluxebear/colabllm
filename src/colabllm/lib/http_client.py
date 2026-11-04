"""Shared HTTP, multipart, and audio encoding helpers for Colab service CLIs."""
from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cli_support import CliError, http_timeout, safe_urlopen

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"


def sanitize_header_param(value: str) -> str:
    """Sanitize a multipart Content-Disposition parameter value against header/CRLF injection."""
    cleaned = str(value).replace("\x00", "").replace("\r", "").replace("\n", "")
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def build_multipart_form(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, str, bytes]] | None = None,
) -> tuple[bytes, str]:
    """Encode multipart/form-data safely and return ``(body_bytes, content_type_header)``."""
    boundary = f"----colabllmform{int(time.time() * 1000)}"
    parts: list[bytes] = []
    for key, val in fields:
        safe_key = sanitize_header_param(key)
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{safe_key}"\r\n\r\n{val}\r\n'.encode("utf-8")
        )
    for field_name, filename, content_type, content_bytes in files or ():
        safe_field = sanitize_header_param(field_name)
        safe_file = sanitize_header_param(filename)
        safe_ct = str(content_type).replace("\r", "").replace("\n", "")
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{safe_field}"; filename="{safe_file}"\r\n'
                f"Content-Type: {safe_ct}\r\n\r\n"
            ).encode("utf-8")
            + content_bytes
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def http_request(
    url: str,
    *,
    token: str | None = None,
    basic_auth: tuple[str, str] | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | int | None = 120,
) -> tuple[int, dict[str, str], bytes]:
    req_headers: dict[str, str] = {"User-Agent": BROWSER_UA}
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    elif basic_auth:
        creds = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")).decode("ascii")
        req_headers["Authorization"] = f"Basic {creds}"
    data = raw_body
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    effective_timeout = http_timeout(timeout)
    try:
        with safe_urlopen(req, timeout=effective_timeout) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp_headers, resp.read()
    except urllib.error.HTTPError as err:
        raw_err = err.read().decode("utf-8", errors="replace")
        raise CliError(f"HTTP {err.code} calling {url}: {raw_err}") from None


def http_json(
    url: str,
    *,
    token: str | None = None,
    basic_auth: tuple[str, str] | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | int | None = 120,
) -> dict[str, Any]:
    _, _, raw_bytes = http_request(
        url,
        token=token,
        basic_auth=basic_auth,
        method=method,
        payload=payload,
        headers=headers,
        timeout=timeout,
    )
    raw = raw_bytes.decode("utf-8", errors="replace").strip()
    return json.loads(raw) if raw else {}


def encode_local_audio_to_data_url(
    audio_path: Path,
    *,
    sample_rate: int = 24000,
    max_duration_sec: float = 24.0,
) -> str:
    p = audio_path.expanduser().resolve()
    if not p.is_file():
        raise CliError(f"Local audio file not found: {p}")
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-t", f"{max_duration_sec:.1f}",
        "-i", str(p), "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    if res.returncode == 0 and res.stdout:
        wav_bytes = res.stdout
    elif p.suffix.lower() == ".wav":
        wav_bytes = p.read_bytes()
    else:
        err_msg = res.stderr.decode("utf-8", errors="replace").strip()
        raise CliError(f"Failed to convert reference audio {p} via ffmpeg: {err_msg}")
    b64 = base64.b64encode(wav_bytes).decode("ascii")
    return f"data:audio/wav;base64,{b64}"


def resolve_audio_spec(
    spec: str,
    *,
    sample_rate: int = 24000,
    max_duration_sec: float = 24.0,
) -> str:
    s = (spec or "").strip()
    if not s:
        return ""
    if s.startswith(("drive://", "gdrive://", "http://", "https://", "data:")):
        return s
    if s.startswith("/content/drive/"):
        return "drive://" + s.removeprefix("/content/drive/MyDrive/").removeprefix("/content/drive/")
    local = Path(s).expanduser()
    if local.is_file():
        return encode_local_audio_to_data_url(local, sample_rate=sample_rate, max_duration_sec=max_duration_sec)
    return f"drive://{s.lstrip('/')}"
