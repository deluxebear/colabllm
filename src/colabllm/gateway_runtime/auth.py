"""Gateway authentication, hashed token storage, bounded multipart handling, and fail-closed security guards."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import email.parser
import email.policy
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from typing import Any
from urllib.parse import unquote

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from cli_support import UsageError  # noqa: E402
from nemotron_audio import YTDLP_DOMAIN_MARKERS  # noqa: E402
from paths import private_dir, write_private  # noqa: E402

from .catalog import RuntimeGatewaySpec  # noqa: E402

TOKEN_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
KEY_ID_RE = re.compile(r"^[0-9a-f]{12}$")
HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DATA_MEDIA_RE = re.compile(
    r"^data:(?:image|audio)/[A-Za-z0-9.+_-]+(?:;[A-Za-z0-9.=_-]+)*;base64,[A-Za-z0-9+/=\s]+$",
    re.IGNORECASE,
)

BASELINE_DRIVE_FORBIDDEN_FIELDS = frozenset({
    "save_to_drive",
    "save_drive_dir",
    "save_drive_path",
    "drive_only",
    "drive_image",
    "drive_images",
    "lora",
    "lora_path",
})
BASELINE_MEDIA_FIELDS = frozenset({
    "image_url",
    "audio_url",
    "ref_audio",
    "emo_audio",
    "audio_path",
})
MULTIPART_JSON_FIELDS = frozenset({
    "extra_body",
    "extra_params",
    "items",
    "references",
    "lora",
    "lora_path",
    "messages",
    "tools",
    "response_format",
})
FORBIDDEN_DRIVE_SCHEME_PREFIXES = (
    "drive://",
    "gdrive://",
    "file://",
    "upload://",
    "ytdl://",
    "yt-dlp://",
    "http://",
    "https://",
    "/content/",
    "/kaggle/",
    "mydrive/",
    "./",
    "../",
    "~/",
    "/",
    "\\",
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class GatewayError(Exception):
    """Structured HTTP error formatted dynamically as OpenAI or Anthropic JSON."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        oai_type: str = "invalid_request_error",
        oai_code: str = "bad_request",
        anth_type: str = "invalid_request_error",
        error_class: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)
        self.oai_type = oai_type
        self.oai_code = oai_code
        self.anth_type = anth_type
        self.error_class = error_class or oai_code
        self.headers = dict(headers or {})

    def to_payload(self, api_family: str = "openai") -> dict[str, Any]:
        if api_family == "anthropic":
            return {
                "type": "error",
                "error": {
                    "type": self.anth_type,
                    "message": self.message,
                },
            }
        return {
            "error": {
                "message": self.message,
                "type": self.oai_type,
                "param": None,
                "code": self.oai_code,
            }
        }


class TokenStoreFormatError(RuntimeError):
    """Raised when tokens.json has an unsupported version or corrupt schema."""


class TokenStore:
    """Thread-safe, flock-synchronized hashed client token store (<COLABLLM_HOME>/gateway/tokens.json)."""

    VERSION = 1

    def __init__(self, gateway_dir: Path) -> None:
        self.gateway_dir = Path(gateway_dir)
        self.tokens_path = self.gateway_dir / "tokens.json"
        self.lock_path = self.gateway_dir / "tokens.lock"
        self._cache_lock = threading.Lock()
        self._cached_fp: tuple[int, int] | None = None
        self._cached_tokens: dict[str, dict[str, Any]] = {}

    @staticmethod
    def validate_token_name(name: str) -> str:
        cleaned = (name or "").strip()
        if not TOKEN_NAME_RE.fullmatch(cleaned):
            raise UsageError("Token 名称必须为 1–64 位字母、数字、'.'、'_' 或 '-'")
        return cleaned

    @staticmethod
    def _hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_store_dict(raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, dict) or raw.get("version") != TokenStore.VERSION or not isinstance(raw.get("tokens"), dict):
            raise TokenStoreFormatError("tokens.json 版本不受支持或结构损坏（仅支持 version == 1）")
        out: dict[str, dict[str, Any]] = {}
        for kid, entry in raw["tokens"].items():
            if isinstance(kid, str) and isinstance(entry, dict):
                out[kid] = dict(entry)
        return out

    def _read_unlocked(self) -> dict[str, dict[str, Any]]:
        if not self.tokens_path.is_file():
            return {}
        try:
            loaded = json.loads(self.tokens_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as err:
            raise TokenStoreFormatError(f"无法读取 tokens.json: {err}") from err
        return self._parse_store_dict(loaded)

    def _save_unlocked(self, tokens: dict[str, dict[str, Any]]) -> None:
        payload = {"version": self.VERSION, "tokens": tokens}
        write_private(self.tokens_path, payload)

    def _locked_update(self):
        class _LockCtx:
            def __init__(ctx_self) -> None:
                ctx_self.handle = None

            def __enter__(ctx_self):
                private_dir(self.gateway_dir)
                ctx_self.handle = self.lock_path.open("a+", encoding="utf-8")
                os.chmod(self.lock_path, 0o600)
                fcntl.flock(ctx_self.handle.fileno(), fcntl.LOCK_EX)
                return ctx_self

            def __exit__(ctx_self, exc_type, exc, tb) -> None:
                if ctx_self.handle is not None:
                    try:
                        fcntl.flock(ctx_self.handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        ctx_self.handle.close()

        return _LockCtx()

    def ensure_initial_default_token(self) -> dict[str, Any] | None:
        """Create an initial 'default' token only if tokens.json does not yet exist."""
        with self._locked_update():
            if self.tokens_path.is_file():
                # Validate readable/version == 1 even when already initialized.
                self._read_unlocked()
                return None
            key_id = secrets.token_hex(6)
            secret = secrets.token_urlsafe(24)
            full_token = f"clgw_{key_id}_{secret}"
            now = _utc_now_iso()
            entry = {
                "key_id": key_id,
                "name": "default",
                "sha256": self._hash_token(full_token),
                "created_at": now,
                "revoked_at": None,
            }
            self._save_unlocked({key_id: entry})
            self._invalidate_cache()
            return {"key_id": key_id, "name": "default", "token": full_token, "created_at": now}

    def create_token(self, name: str) -> dict[str, Any]:
        valid_name = self.validate_token_name(name)
        with self._locked_update():
            tokens = self._read_unlocked()
            for entry in tokens.values():
                if entry.get("name") == valid_name and entry.get("revoked_at") is None:
                    raise UsageError(f"活跃 Token 名称已存在：{valid_name!r}（如需更新请使用 token rotate）")
            key_id = secrets.token_hex(6)
            while key_id in tokens:
                key_id = secrets.token_hex(6)
            secret = secrets.token_urlsafe(24)
            full_token = f"clgw_{key_id}_{secret}"
            now = _utc_now_iso()
            tokens[key_id] = {
                "key_id": key_id,
                "name": valid_name,
                "sha256": self._hash_token(full_token),
                "created_at": now,
                "revoked_at": None,
            }
            self._save_unlocked(tokens)
            self._invalidate_cache()
            return {"key_id": key_id, "name": valid_name, "token": full_token, "created_at": now}

    def rotate_token(self, name: str) -> dict[str, Any]:
        valid_name = self.validate_token_name(name)
        with self._locked_update():
            tokens = self._read_unlocked()
            active_id: str | None = None
            for kid, entry in tokens.items():
                if entry.get("name") == valid_name and entry.get("revoked_at") is None:
                    active_id = kid
                    break
            if active_id is None:
                raise UsageError(f"未找到可轮换的活跃 Token：{valid_name!r}")
            now = _utc_now_iso()
            tokens[active_id]["revoked_at"] = now
            new_id = secrets.token_hex(6)
            while new_id in tokens:
                new_id = secrets.token_hex(6)
            secret = secrets.token_urlsafe(24)
            full_token = f"clgw_{new_id}_{secret}"
            tokens[new_id] = {
                "key_id": new_id,
                "name": valid_name,
                "sha256": self._hash_token(full_token),
                "created_at": now,
                "revoked_at": None,
            }
            self._save_unlocked(tokens)
            self._invalidate_cache()
            return {
                "key_id": new_id,
                "previous_key_id": active_id,
                "name": valid_name,
                "token": full_token,
                "created_at": now,
            }

    def revoke_token(self, name: str) -> dict[str, Any]:
        valid_name = self.validate_token_name(name)
        with self._locked_update():
            tokens = self._read_unlocked()
            active_id: str | None = None
            for kid, entry in tokens.items():
                if entry.get("name") == valid_name and entry.get("revoked_at") is None:
                    active_id = kid
                    break
            if active_id is None:
                raise UsageError(f"未找到可吊销的活跃 Token：{valid_name!r}")
            now = _utc_now_iso()
            tokens[active_id]["revoked_at"] = now
            self._save_unlocked(tokens)
            self._invalidate_cache()
            return {"key_id": active_id, "name": valid_name, "revoked_at": now}

    def list_tokens(self) -> list[dict[str, Any]]:
        """Read-only listing returning redacted metadata; never creates directories or files."""
        tokens = self._read_unlocked()
        rows = [
            {
                "key_id": entry.get("key_id", kid),
                "name": entry.get("name", ""),
                "created_at": entry.get("created_at"),
                "revoked_at": entry.get("revoked_at"),
            }
            for kid, entry in tokens.items()
        ]
        rows.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("key_id") or "")))
        return rows

    def _invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cached_fp = None

    def _load_cached(self) -> dict[str, dict[str, Any]]:
        if not self.tokens_path.is_file():
            with self._cache_lock:
                self._cached_fp = None
                self._cached_tokens = {}
            return {}
        try:
            st = self.tokens_path.stat()
        except OSError as err:
            raise TokenStoreFormatError(f"无法检查 tokens.json 状态: {err}") from err
        fp = (st.st_mtime_ns, st.st_size)
        with self._cache_lock:
            if self._cached_fp == fp:
                return self._cached_tokens
        loaded = self._read_unlocked()
        with self._cache_lock:
            self._cached_fp = fp
            self._cached_tokens = loaded
        return loaded

    def verify_token(self, raw_token: str) -> dict[str, str] | None:
        """Verify token string in constant time against hot-reloaded SHA-256 digests."""
        if not raw_token or not raw_token.startswith("clgw_"):
            return None
        parts = raw_token.split("_", 2)
        if len(parts) != 3:
            return None
        _, key_id, secret = parts
        if not KEY_ID_RE.fullmatch(key_id) or not secret:
            return None
        tokens = self._load_cached()
        entry = tokens.get(key_id)
        if not isinstance(entry, dict) or entry.get("revoked_at") is not None:
            return None
        expected_hash = str(entry.get("sha256") or "")
        if not expected_hash:
            return None
        sent_hash = self._hash_token(raw_token)
        if not hmac.compare_digest(sent_hash, expected_hash):
            return None
        return {"key_id": key_id, "name": str(entry.get("name") or "")}


def extract_host_only(host_header: str) -> str:
    """Extract normalized lowercase host without port (retaining brackets for IPv6)."""
    raw = (host_header or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return ""
        rest = raw[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return ""
        return raw[: end + 1]
    if raw.count(":") == 1:
        host_part, port_part = raw.split(":", 1)
        if not port_part.isdigit():
            return ""
        return host_part
    if ":" in raw:
        return ""
    return raw


def validate_allow_host_pattern(pattern: str) -> str:
    """Validate and normalize --allow-host exact hostname/IP or safe multi-label '*.domain.tld' pattern."""
    raw = (pattern or "").strip().lower()
    if not raw or "://" in raw or "/" in raw or any(ch.isspace() for ch in raw):
        raise UsageError(f"无效的 --allow-host 模式：{pattern!r}")
    if raw.startswith("*"):
        if not raw.startswith("*.") or raw.count("*") > 1:
            raise UsageError(f"无效的通配 Host 模式：{pattern!r}（仅支持 '*.domain.tld' 形式）")
        base = raw[2:]
        labels = base.split(".")
        if len(labels) < 2 or not all(HOST_LABEL_RE.fullmatch(lbl) for lbl in labels):
            raise UsageError(f"拒绝不安全的单标签或非法通配域名：{pattern!r}（需至少包含两级域名，如 '*.ts.net'）")
        return raw
    bare = raw[1:-1] if (raw.startswith("[") and raw.endswith("]")) else raw
    try:
        ip = ipaddress.ip_address(bare)
        return f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    except ValueError:
        pass
    if ":" in raw:
        raise UsageError(f"--allow-host 不应包含端口或无效冒号：{pattern!r}")
    labels = raw.split(".")
    if not all(HOST_LABEL_RE.fullmatch(lbl) for lbl in labels):
        raise UsageError(f"无效的 Host 域名：{pattern!r}")
    return raw


def _host_matches_pattern(host_only: str, pattern: str) -> bool:
    pattern = pattern.strip().lower()
    if pattern.startswith("*."):
        suffix = pattern[1:]  # e.g. ".ts.net"
        if not host_only.endswith(suffix) or len(host_only) <= len(suffix):
            return False
        sub = host_only[: -len(suffix)]
        sub_labels = sub.split(".")
        return bool(sub_labels) and all(HOST_LABEL_RE.fullmatch(lbl) for lbl in sub_labels)
    if host_only == pattern:
        return True
    bare_host = host_only[1:-1] if (host_only.startswith("[") and host_only.endswith("]")) else host_only
    bare_pat = pattern[1:-1] if (pattern.startswith("[") and pattern.endswith("]")) else pattern
    return bare_host == bare_pat


def is_loopback_client_ip(client_ip: str) -> bool:
    raw = (client_ip or "").strip()
    if not raw:
        return False
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    raw = raw.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return addr.is_loopback


def _local_probe_hosts_for_bind(bound_host: str) -> set[str]:
    allowed = set(_LOOPBACK_HOSTS)
    norm = (bound_host or "").strip().lower().strip("[]")
    if norm and norm not in ("0.0.0.0", "::"):
        allowed.add(norm)
        if ":" in norm:
            allowed.add(f"[{norm}]")
    return allowed


@dataclass(frozen=True)
class AuthOutcome:
    token_id: str | None = None
    token_name: str | None = None
    is_status_token: bool = False


def validate_inbound_access(
    *,
    method: str,
    path: str,
    client_ip: str,
    headers: Any,
    bound_host: str,
    allow_remote: bool,
    allow_hosts: tuple[str, ...],
    status_token: str,
    token_store: TokenStore,
) -> AuthOutcome:
    """Execute deterministic Steps 2 (Host & Sec-Fetch-Site) and 3 (/healthz, status_token, client Token)."""
    sec_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sec_site and sec_site not in ("same-origin", "none"):
        raise GatewayError(
            403,
            "拒绝跨站浏览器请求 (Sec-Fetch-Site)",
            oai_type="permission_error",
            oai_code="policy_blocked",
            anth_type="permission_error",
        )

    host_only = extract_host_only(headers.get("Host", ""))
    if not host_only:
        raise GatewayError(
            403,
            "缺少有效的 Host 请求头",
            oai_type="permission_error",
            oai_code="policy_blocked",
            anth_type="permission_error",
        )

    sent_status_token = (headers.get("X-ColabLLM-Status-Token") or "").strip()
    local_probe_hosts = _local_probe_hosts_for_bind(bound_host)
    norm_bound = (bound_host or "").strip().lower().strip("[]")
    client_is_local_or_bound = is_loopback_client_ip(client_ip) or (
        norm_bound not in ("", "0.0.0.0", "::") and client_ip.strip().lower().strip("[]") == norm_bound
    )

    if sent_status_token:
        # Local status token is strictly bound to GET /gateway/status from local/bound interface.
        if method != "GET" or path != "/gateway/status" or not client_is_local_or_bound:
            raise GatewayError(
                401,
                "status_token 仅限本机访问 GET /gateway/status",
                oai_type="authentication_error",
                oai_code="invalid_api_key",
                anth_type="authentication_error",
            )
        if not hmac.compare_digest(sent_status_token, status_token):
            raise GatewayError(
                401,
                "无效的状态查询令牌",
                oai_type="authentication_error",
                oai_code="invalid_api_key",
                anth_type="authentication_error",
            )
        if not _host_matches_any(host_only, local_probe_hosts):
            raise GatewayError(
                403,
                f"禁止的本地状态探测 Host: {host_only}",
                oai_type="permission_error",
                oai_code="policy_blocked",
                anth_type="permission_error",
            )
        return AuthOutcome(token_id="status_token", token_name="local-status", is_status_token=True)

    # Normal Host check for all non-status_token requests.
    wildcard_remote = bool(allow_remote and norm_bound in ("0.0.0.0", "::"))
    if allow_hosts:
        if not _host_matches_any(host_only, allow_hosts):
            raise GatewayError(
                403,
                f"Host {host_only!r} 未在 --allow-host 白名单中",
                oai_type="permission_error",
                oai_code="policy_blocked",
                anth_type="permission_error",
            )
    elif not wildcard_remote:
        if not _host_matches_any(host_only, local_probe_hosts):
            raise GatewayError(
                403,
                f"禁止访问非允许 Host: {host_only!r}",
                oai_type="permission_error",
                oai_code="policy_blocked",
                anth_type="permission_error",
            )

    if method == "GET" and path == "/healthz":
        return AuthOutcome()

    auth_header = (headers.get("Authorization") or "").strip()
    x_api_key = (headers.get("x-api-key") or "").strip()
    bearer_token = ""
    if auth_header:
        scheme, sep, rest = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not sep or not rest.strip():
            raise GatewayError(
                401,
                "Authorization 请求头必须使用 Bearer <token>",
                oai_type="authentication_error",
                oai_code="invalid_api_key",
                anth_type="authentication_error",
            )
        bearer_token = rest.strip()

    if bearer_token and x_api_key and bearer_token != x_api_key:
        raise GatewayError(
            401,
            "Authorization 与 x-api-key 请求头携带了不一致的凭据",
            oai_type="authentication_error",
            oai_code="invalid_api_key",
            anth_type="authentication_error",
        )

    credential = bearer_token or x_api_key
    if not credential:
        raise GatewayError(
            401,
            "缺少网关访问凭据（请通过 Authorization: Bearer 或 x-api-key 传入 Token）",
            oai_type="authentication_error",
            oai_code="invalid_api_key",
            anth_type="authentication_error",
        )

    try:
        verified = token_store.verify_token(credential)
    except TokenStoreFormatError as err:
        raise GatewayError(
            500,
            str(err),
            oai_type="server_error",
            oai_code="token_store_error",
            anth_type="api_error",
        ) from err

    if verified is None:
        raise GatewayError(
            401,
            "无效或已吊销的网关 Token",
            oai_type="authentication_error",
            oai_code="invalid_api_key",
            anth_type="authentication_error",
        )

    return AuthOutcome(token_id=verified["key_id"], token_name=verified["name"])


def _host_matches_any(host_only: str, patterns: Any) -> bool:
    return any(_host_matches_pattern(host_only, pat) for pat in patterns)


@dataclass(frozen=True)
class MultipartFilePart:
    field_name: str
    filename: str | None
    content_type: str | None
    payload: bytes


@dataclass
class PreparedPayload:
    is_multipart: bool
    parsed_body: Any | None
    multipart_fields: list[tuple[str, str]]
    multipart_files: list[MultipartFilePart]
    requested_model: str | None


def parse_request_payload(
    *,
    method: str,
    headers: Any,
    raw_body: bytes,
    max_text_field_bytes: int,
) -> PreparedPayload:
    """Parse JSON or bounded multipart/form-data payload without altering binary file bytes."""
    if method != "POST":
        return PreparedPayload(False, None, [], [], None)

    ctype = (headers.get("Content-Type") or "").strip()
    media_type = ctype.split(";", 1)[0].strip().lower()

    if media_type == "multipart/form-data" or media_type.startswith("multipart/"):
        boundary_param = ""
        for seg in ctype.split(";")[1:]:
            k, _, v = seg.strip().partition("=")
            if k.lower() == "boundary" and v:
                boundary_param = v.strip().strip('"')
                break
        if not boundary_param or not raw_body:
            raise GatewayError(400, "multipart/form-data 请求缺少有效的 boundary 参数")

        synthetic_msg = (
            f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1") + raw_body
        )
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(synthetic_msg)
        if not msg.is_multipart():
            raise GatewayError(400, "multipart/form-data 请求体结构无效")
        subparts = msg.get_payload()
        if not isinstance(subparts, list) or not subparts:
            raise GatewayError(400, "multipart/form-data 请求未包含任何分段")

        fields: list[tuple[str, str]] = []
        files: list[MultipartFilePart] = []
        model_values: list[str] = []

        for part in subparts:
            disp_name = part.get_param("name", header="content-disposition")
            if not isinstance(disp_name, str) or not disp_name:
                raise GatewayError(400, "multipart 分段缺少 name 属性")
            filename = part.get_filename()
            part_ctype = part.get_content_type() if part.get("Content-Type") else None
            payload_bytes = part.get_payload(decode=True)
            if payload_bytes is None:
                raw_sub = part.get_payload()
                payload_bytes = raw_sub.encode("latin-1") if isinstance(raw_sub, str) else b""

            is_binary_part = (filename is not None) or (
                isinstance(part_ctype, str)
                and part_ctype.lower().startswith(("image/", "audio/", "video/", "application/octet-stream"))
            )
            if is_binary_part:
                files.append(
                    MultipartFilePart(
                        field_name=disp_name,
                        filename=filename,
                        content_type=part.get("Content-Type"),
                        payload=payload_bytes,
                    )
                )
            else:
                if len(payload_bytes) > max_text_field_bytes:
                    raise GatewayError(
                        413,
                        f"multipart 文本字段 {disp_name!r} 超过 {max_text_field_bytes} 字节上限",
                        oai_code="payload_too_large",
                    )
                try:
                    text_val = payload_bytes.decode("utf-8")
                except UnicodeDecodeError as err:
                    raise GatewayError(400, f"multipart 文本字段 {disp_name!r} 非有效 UTF-8: {err}") from err
                fields.append((disp_name, text_val))
                if disp_name == "model":
                    model_values.append(text_val)

        if len(model_values) > 1:
            raise GatewayError(400, "multipart 表单不允许重复提交多个 model 字段")
        req_model = model_values[0] if model_values else None
        return PreparedPayload(True, None, fields, files, req_model)

    if not raw_body:
        return PreparedPayload(False, {}, [], [], None)

    try:
        decoded = raw_body.decode("utf-8")
        obj = json.loads(decoded)
    except (UnicodeDecodeError, ValueError) as err:
        raise GatewayError(400, f"请求体 JSON 无效: {err}") from err
    if not isinstance(obj, dict):
        raise GatewayError(400, "JSON 请求体根元素必须为对象")
    raw_model = obj.get("model")
    req_model = raw_model if isinstance(raw_model, str) else None
    return PreparedPayload(False, obj, [], [], req_model)


def rebuild_multipart_body(
    fields: list[tuple[str, str]],
    file_parts: list[MultipartFilePart],
    *,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """Rebuild multipart/form-data preserving multi-value fields, filename-less binary parts, and raw bytes."""
    bnd = boundary or f"clgw-{secrets.token_hex(16)}"
    chunks: list[bytes] = []

    def _escape_disp(val: str) -> str:
        return str(val).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")

    for name, value in fields:
        chunks.append(
            f'--{bnd}\r\nContent-Disposition: form-data; name="{_escape_disp(name)}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")

    for fp in file_parts:
        disp = f'Content-Disposition: form-data; name="{_escape_disp(fp.field_name)}"'
        if fp.filename is not None:
            disp += f'; filename="{_escape_disp(fp.filename)}"'
        ctype_line = f"\r\nContent-Type: {fp.content_type.strip()}" if fp.content_type else "\r\nContent-Type: application/octet-stream"
        chunks.append(f"--{bnd}\r\n{disp}{ctype_line}\r\n\r\n".encode("utf-8"))
        chunks.append(fp.payload)
        chunks.append(b"\r\n")

    chunks.append(f"--{bnd}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={bnd}"


def _canonical_key(key: str) -> str:
    k = key.strip().lower()
    while k.endswith("[]"):
        k = k[:-2]
    return k


def _validate_media_string(value: str, *, field_path: str) -> None:
    val = value.strip()
    if not val:
        raise GatewayError(
            403,
            f"媒体字段 {field_path} 不允许为空",
            oai_type="permission_error",
            oai_code="policy_blocked",
            anth_type="permission_error",
        )
    if DATA_MEDIA_RE.fullmatch(val):
        return

    lower = val.lower()
    unquoted = unquote(lower)
    for marker in YTDLP_DOMAIN_MARKERS:
        m_low = marker.lower()
        if m_low in lower or m_low in unquoted:
            raise GatewayError(
                403,
                f"网关禁止在 {field_path} 中引用受限制的视频托管或 ytdl URL",
                oai_type="permission_error",
                oai_code="ytdlp_url_blocked",
                anth_type="permission_error",
                error_class="ytdlp_url_blocked",
            )

    if lower.startswith(("http://", "https://")):
        raise GatewayError(
            403,
            f"网关当前禁止外部 URL 媒体引用 ({field_path})；请改用 data: 内联 URI 或二进制 multipart 上传",
            oai_type="permission_error",
            oai_code="external_media_blocked",
            anth_type="permission_error",
            error_class="external_media_blocked",
        )

    if lower.startswith(FORBIDDEN_DRIVE_SCHEME_PREFIXES) or ":/" in lower or "\\" in val:
        raise GatewayError(
            403,
            f"网关禁止在 {field_path} 中引用 Drive 或本地路径",
            oai_type="permission_error",
            oai_code="drive_path_blocked",
            anth_type="permission_error",
            error_class="drive_path_blocked",
        )

    # Reject bare filenames (e.g. "secret.png") or unknown tokens on media fields fail-closed.
    raise GatewayError(
        403,
        f"网关仅接受 data:(image|audio)/...;base64,... 内联媒体 ({field_path})",
        oai_type="permission_error",
        oai_code="policy_blocked",
        anth_type="permission_error",
        error_class="media_scheme_blocked",
    )


def _check_media_value(val: Any, *, field_path: str, is_source_dict: bool = False) -> None:
    if isinstance(val, str):
        _validate_media_string(val, field_path=field_path)
    elif isinstance(val, list):
        for idx, item in enumerate(val):
            _check_media_value(item, field_path=f"{field_path}[{idx}]", is_source_dict=is_source_dict)
    elif isinstance(val, dict):
        if is_source_dict:
            src_type = str(val.get("type") or "").strip().lower()
            if src_type == "url" or "url" in val:
                raw_url = val.get("url")
                if not isinstance(raw_url, str):
                    raise GatewayError(
                        403,
                        f"{field_path}.url 必须为安全的 data: 内联媒体字符串",
                        oai_type="permission_error",
                        oai_code="policy_blocked",
                        anth_type="permission_error",
                    )
                _validate_media_string(raw_url, field_path=f"{field_path}.url")
            elif src_type not in ("", "base64") and src_type != "text":
                raise GatewayError(
                    403,
                    f"不允许的 Anthropic source.type: {src_type!r}",
                    oai_type="permission_error",
                    oai_code="policy_blocked",
                    anth_type="permission_error",
                )
            return
        if "url" in val:
            _check_media_value(val["url"], field_path=f"{field_path}.url")
        if "image_url" in val:
            _check_media_value(val["image_url"], field_path=f"{field_path}.image_url")
        if "audio_url" in val:
            _check_media_value(val["audio_url"], field_path=f"{field_path}.audio_url")
        if "source" in val:
            _check_media_value(val["source"], field_path=f"{field_path}.source", is_source_dict=True)
    elif val is not None:
        raise GatewayError(
            403,
            f"媒体字段 {field_path} 类型受限",
            oai_type="permission_error",
            oai_code="policy_blocked",
            anth_type="permission_error",
        )


def _inspect_multimodal_tool_block(block: Any, *, field_path: str, forbidden: frozenset[str], media: frozenset[str]) -> None:
    """Inspect only explicit multimodal blocks inside tool_result / tool outputs."""
    if not isinstance(block, dict):
        return
    btype = str(block.get("type") or "").strip().lower()
    if btype in ("image_url", "input_image", "input_audio", "audio_url", "image", "audio"):
        _inspect_json_node(
            block,
            path_label=field_path,
            forbidden=forbidden,
            media=media,
            normalized_v1_path="/v1/chat/completions",
            in_source=False,
        )


def _inspect_json_node(
    node: Any,
    *,
    path_label: str,
    forbidden: frozenset[str],
    media: frozenset[str],
    normalized_v1_path: str,
    in_source: bool = False,
) -> None:
    if isinstance(node, list):
        for idx, item in enumerate(node):
            _inspect_json_node(
                item,
                path_label=f"{path_label}[{idx}]",
                forbidden=forbidden,
                media=media,
                normalized_v1_path=normalized_v1_path,
                in_source=in_source,
            )
        return
    if not isinstance(node, dict):
        return

    btype = str(node.get("type") or "").strip().lower()
    role = str(node.get("role") or "").strip().lower()

    # Exempt Responses function_call / function_call_output and Anthropic tool_use / tool_result payloads.
    if btype in ("function_call", "tool_use"):
        return
    if btype in ("tool_result", "function_call_output"):
        content = node.get("content") if btype == "tool_result" else node.get("output")
        if isinstance(content, list):
            for idx, item in enumerate(content):
                _inspect_multimodal_tool_block(
                    item,
                    field_path=f"{path_label}[{idx}]",
                    forbidden=forbidden,
                    media=media,
                )
        return

    for raw_k, val in node.items():
        if not isinstance(raw_k, str):
            continue
        ckey = _canonical_key(raw_k)
        next_label = f"{path_label}.{raw_k}"

        # Exempt Chat/Responses/Anthropic tool declaration schemas & Chat tool_calls/role=tool payloads.
        if ckey == "tools" and isinstance(val, list):
            continue
        if ckey in ("tool_calls", "function_call") and role in ("", "assistant"):
            continue
        if role == "tool" and ckey == "content":
            if isinstance(val, list):
                for idx, item in enumerate(val):
                    _inspect_multimodal_tool_block(
                        item,
                        field_path=f"{next_label}[{idx}]",
                        forbidden=forbidden,
                        media=media,
                    )
            continue

        if ckey in forbidden:
            raise GatewayError(
                403,
                f"网关禁止在请求中使用 Google Drive / 本地权重字段 {raw_k!r} ({next_label})",
                oai_type="permission_error",
                oai_code="drive_field_blocked",
                anth_type="permission_error",
                error_class="drive_field_blocked",
            )

        if in_source and ckey == "url":
            _check_media_value(val, field_path=next_label)
            continue

        if ckey == "source" and isinstance(val, dict):
            _check_media_value(val, field_path=next_label, is_source_dict=True)
            continue

        if ckey in media:
            _check_media_value(val, field_path=next_label)
            continue

        if ckey in ("image_url", "audio_url") and val is not None:
            _check_media_value(val, field_path=next_label)
            continue

        # Recurse into containers (`extra_body`, `extra_params`, `items`, `references`, messages, etc.).
        if isinstance(val, (dict, list)):
            _inspect_json_node(
                val,
                path_label=next_label,
                forbidden=forbidden,
                media=media,
                normalized_v1_path=normalized_v1_path,
                in_source=(ckey == "source"),
            )


def _check_response_format_url(value: Any) -> None:
    if isinstance(value, str) and value.strip().lower() == "url":
        raise GatewayError(
            400,
            "生图接口在网关模式下不支持 response_format='url'，请改用 response_format='b64_json'",
            oai_type="invalid_request_error",
            oai_code="bad_request",
            anth_type="invalid_request_error",
        )
    if isinstance(value, dict) and str(value.get("type") or "").strip().lower() == "url":
        raise GatewayError(
            400,
            "生图接口在网关模式下不支持 response_format='url'，请改用 response_format='b64_json'",
            oai_type="invalid_request_error",
            oai_code="bad_request",
            anth_type="invalid_request_error",
        )


def _inspect_response_format_in_containers(obj: dict[str, Any]) -> None:
    if "response_format" in obj:
        _check_response_format_url(obj.get("response_format"))
    for cname in ("extra_body", "extra_params"):
        sub = obj.get(cname)
        if isinstance(sub, dict) and "response_format" in sub:
            _check_response_format_url(sub.get("response_format"))


def inspect_and_prepare_request(
    *,
    method: str,
    normalized_v1_path: str,
    headers: Any,
    payload: PreparedPayload,
    spec: RuntimeGatewaySpec,
    upstream_model: str | None,
) -> tuple[bytes, str | None]:
    """Enforce header/body security guards, validate parameters, and return forwarded bytes & content-type."""
    if headers.get("X-Save-Drive") or headers.get("X-Drive-Only"):
        raise GatewayError(
            403,
            "网关禁止携带 X-Save-Drive 或 X-Drive-Only 请求头",
            oai_type="permission_error",
            oai_code="drive_header_blocked",
            anth_type="permission_error",
            error_class="drive_header_blocked",
        )

    if method != "POST":
        return b"", None

    forbidden = frozenset(BASELINE_DRIVE_FORBIDDEN_FIELDS | {k.lower() for k in spec.drive_forbidden_fields})
    media = set(BASELINE_MEDIA_FIELDS)
    media.update(k.lower() for k in spec.media_fields if "." not in k)
    if spec.check_bare_url_on_images_api and normalized_v1_path.startswith("/v1/images/"):
        media.add("url")
    if spec.check_file_on_asr_api and normalized_v1_path in ("/v1/audio/transcriptions", "/v1/audio/translations"):
        media.add("file")
    media_fs = frozenset(media)

    if payload.is_multipart:
        for k, v in payload.multipart_fields:
            ckey = _canonical_key(k)
            if ckey in forbidden:
                raise GatewayError(
                    403,
                    f"网关禁止在 multipart 表单中使用敏感字段 {k!r}",
                    oai_type="permission_error",
                    oai_code="drive_field_blocked",
                    anth_type="permission_error",
                    error_class="drive_field_blocked",
                )
            if ckey in media_fs:
                _validate_media_string(v, field_path=f"multipart.{k}")
            if ckey in MULTIPART_JSON_FIELDS and v.lstrip().startswith(("{", "[")):
                try:
                    decoded = json.loads(v)
                except ValueError as err:
                    raise GatewayError(400, f"multipart 字段 {k!r} JSON 格式无效: {err}") from err
                _inspect_json_node(
                    decoded,
                    path_label=f"multipart.{k}",
                    forbidden=forbidden,
                    media=media_fs,
                    normalized_v1_path=normalized_v1_path,
                )
                if spec.reject_url_response_format and normalized_v1_path.startswith("/v1/images/"):
                    if ckey == "response_format":
                        _check_response_format_url(decoded)
                    elif isinstance(decoded, dict):
                        _inspect_response_format_in_containers(decoded)
            elif spec.reject_url_response_format and normalized_v1_path.startswith("/v1/images/") and ckey == "response_format":
                _check_response_format_url(v)

        # Rewrite 'model' field in ordered multipart field list while keeping all other fields & binary files identical.
        out_fields = [
            (k, upstream_model if (k == "model" and upstream_model is not None) else v)
            for k, v in payload.multipart_fields
        ]
        rebuilt_bytes, new_ctype = rebuild_multipart_body(out_fields, payload.multipart_files)
        return rebuilt_bytes, new_ctype

    # JSON request
    obj = payload.parsed_body
    if not isinstance(obj, dict):
        raise GatewayError(400, "JSON 请求体格式无效")

    _inspect_json_node(
        obj,
        path_label="body",
        forbidden=forbidden,
        media=media_fs,
        normalized_v1_path=normalized_v1_path,
    )

    if spec.reject_url_response_format and normalized_v1_path.startswith("/v1/images/"):
        _inspect_response_format_in_containers(obj)

    if "previous_response_id" in obj and obj.get("previous_response_id") is not None:
        if spec.stateless_responses_only and normalized_v1_path.startswith("/v1/responses"):
            raise GatewayError(
                400,
                "当前无状态推理引擎不支持 previous_response_id，请传入完整 input 对话历史",
                oai_type="invalid_request_error",
                oai_code="bad_request",
            )

    forwarded = dict(obj)
    if upstream_model is not None and "model" in forwarded:
        forwarded["model"] = upstream_model

    if spec.rewrite_batch_items_model and normalized_v1_path == "/v1/audio/speech/batch":
        items = forwarded.get("items")
        if isinstance(items, list):
            new_items = []
            for idx, item in enumerate(items):
                if isinstance(item, dict) and isinstance(item.get("model"), str):
                    item_copy = dict(item)
                    imodel = item_copy["model"].strip()
                    if "@" in imodel:
                        base_m, _, rhs = imodel.rpartition("@")
                        isvc = rhs.split(":", 1)[0]
                        if isvc != "drama3":
                            raise GatewayError(400, f"批量任务 items[{idx}].model 指定了不匹配的运行时 {isvc!r}")
                        if upstream_model and base_m != upstream_model:
                            raise GatewayError(400, f"批量任务 items[{idx}].model 与请求根 model 不一致")
                        item_copy["model"] = base_m
                    elif upstream_model and imodel != upstream_model:
                        raise GatewayError(400, f"批量任务 items[{idx}].model 与请求根 model 不一致")
                    new_items.append(item_copy)
                else:
                    new_items.append(item)
            forwarded["items"] = new_items

    return json.dumps(forwarded, ensure_ascii=False).encode("utf-8"), "application/json"
