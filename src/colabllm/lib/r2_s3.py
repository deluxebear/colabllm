"""Pure-stdlib Cloudflare R2 S3 SigV4 client, cost-aware ledger, and safe Drive path mapper."""
from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import random
import re
import ssl
import threading
import time
from typing import Any, Callable, Sequence
import urllib.parse
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

FREE_CLASS_A_MONTHLY = 1_000_000
FREE_CLASS_B_MONTHLY = 10_000_000
DEFAULT_SAFE_FREE_CLASS_A = 950_000
DEFAULT_SAFE_FREE_CLASS_B = 9_500_000

PRICE_CLASS_A_PER_MILLION = 4.50
PRICE_IA_CLASS_A_PER_MILLION = 9.00
PRICE_CLASS_B_PER_MILLION = 0.36
PRICE_IA_CLASS_B_PER_MILLION = 0.90
PRICE_IA_RETRIEVAL_PER_GB = 0.01  # decimal 10^9 bytes per R2 pricing

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_XML_ILLEGAL_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_CF_CACHE_HIT_STATUSES = {"HIT", "REVALIDATED", "UPDATING", "STALE"}


class R2Error(RuntimeError):
    """Base error for Cloudflare R2 S3 operations."""


class R2HttpError(R2Error):
    """HTTP error returned by R2 / S3 or CDN endpoint."""

    def __init__(self, status: int, message: str, *, code: str = "", body: str = "") -> None:
        super().__init__(f"HTTP {status}" + (f" ({code})" if code else "") + f": {message}")
        self.status = status
        self.code = code
        self.body = body


class R2IntegrityError(R2Error):
    """Downloaded object failed size or MD5 ETag integrity verification."""


class R2TruncatedReadError(R2IntegrityError):
    """Downloaded object stream ended early before expected Size."""


def s3_quote(value: str, safe: str = "-_.~") -> str:
    """Percent-encode a string strictly according to AWS SigV4 RFC 3986 rules."""
    return urllib.parse.quote(value, safe=safe, encoding="utf-8", errors="strict")


def s3_quote_path(key: str) -> str:
    """Percent-encode an S3 object key while preserving '/' hierarchy delimiters."""
    return urllib.parse.quote(key, safe="/-_.~", encoding="utf-8", errors="strict")


def mask_secret(value: str | None) -> str:
    """Return a non-sensitive masked representation of an access key or secret."""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def current_utc_month() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def parse_public_urls(items: Sequence[str] | dict[str, str] | None) -> dict[str, str]:
    """Validate and normalize per-bucket CDN public URL mappings."""
    mapping: dict[str, str] = {}
    if not items:
        return mapping
    pairs: list[tuple[str, str]] = []
    if isinstance(items, dict):
        pairs = [(str(k), str(v)) for k, v in items.items()]
    else:
        for entry in items:
            raw = str(entry or "").strip()
            if not raw:
                continue
            if "=" not in raw:
                raise ValueError(f"--public-url 需要 BUCKET=URL 格式，收到: {raw}")
            b, u = raw.split("=", 1)
            pairs.append((b, u))

    for bucket, url in pairs:
        b = bucket.strip()
        u = url.strip().rstrip("/")
        if not b or "/" in b:
            raise ValueError(f"无效的 bucket 名称: {bucket!r}")
        parsed = urllib.parse.urlsplit(u)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            raise ValueError(f"CDN public-url 必须是无内嵌凭据的 HTTP(S) URL: {u!r}")
        if parsed.scheme == "http" and host not in _LOOPBACK_HOSTS:
            raise ValueError(f"非本地 CDN public-url 必须使用 https://: {u!r}")
        mapping[b] = u
    return mapping


@dataclasses.dataclass
class R2Credentials:
    access_key_id: str
    secret_access_key: str
    account_id: str = ""
    endpoint_url: str = ""
    jurisdiction: str = ""  # "", "eu", "fedramp"
    region: str = "auto"

    def resolve_endpoint_url(self) -> str:
        raw = (self.endpoint_url or "").strip()
        if raw:
            parsed = urllib.parse.urlsplit(raw)
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
                raise ValueError(f"无效的 R2 endpoint URL: {raw!r}")
            if parsed.query or parsed.fragment:
                raise ValueError("R2 endpoint URL 不应包含 query 或 fragment")
            if parsed.scheme == "http" and host not in _LOOPBACK_HOSTS:
                raise ValueError("非本地 R2 endpoint 必须使用 https://")
            return f"{parsed.scheme.lower()}://{parsed.netloc}{parsed.path.rstrip('/')}"

        acct = (self.account_id or "").strip()
        if not _ACCOUNT_ID_RE.fullmatch(acct):
            raise ValueError("请提供有效的 R2 account_id 或 --endpoint-url")
        jur = (self.jurisdiction or "").strip().lower()
        if jur not in ("", "eu", "fedramp"):
            raise ValueError(f"不支持的 R2 jurisdiction: {jur!r} (可选: eu, fedramp)")
        jur_part = f"{jur}." if jur else ""
        return f"https://{acct}.{jur_part}r2.cloudflarestorage.com"

    def validate(self) -> None:
        if not (self.access_key_id or "").strip():
            raise ValueError("缺少 R2 access_key_id")
        if not (self.secret_access_key or "").strip():
            raise ValueError("缺少 R2 secret_access_key")
        self.resolve_endpoint_url()

    def scope_key(self) -> str:
        return self.resolve_endpoint_url()

    def to_dict(self) -> dict[str, str]:
        return {
            "account_id": (self.account_id or "").strip(),
            "endpoint_url": (self.endpoint_url or "").strip(),
            "jurisdiction": (self.jurisdiction or "").strip().lower(),
            "access_key_id": (self.access_key_id or "").strip(),
            "secret_access_key": (self.secret_access_key or "").strip(),
            "region": (self.region or "auto").strip() or "auto",
        }

    def redacted_summary(self, *, source: str = "configured") -> dict[str, Any]:
        endpoint = ""
        try:
            endpoint = self.resolve_endpoint_url()
        except ValueError:
            endpoint = (self.endpoint_url or "").strip()
        return {
            "configured": bool((self.access_key_id or "").strip() and (self.secret_access_key or "").strip()),
            "source": source,
            "account_id": (self.account_id or "").strip(),
            "endpoint_url": endpoint,
            "jurisdiction": (self.jurisdiction or "").strip().lower(),
            "access_key_id_masked": mask_secret(self.access_key_id),
            "region": (self.region or "auto").strip() or "auto",
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "R2Credentials":
        return cls(
            access_key_id=str(data.get("access_key_id") or "").strip(),
            secret_access_key=str(data.get("secret_access_key") or "").strip(),
            account_id=str(data.get("account_id") or "").strip(),
            endpoint_url=str(data.get("endpoint_url") or "").strip(),
            jurisdiction=str(data.get("jurisdiction") or "").strip().lower(),
            region=str(data.get("region") or "auto").strip() or "auto",
        )


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_s3_request(
    creds: R2Credentials,
    method: str,
    host_header: str,
    canonical_uri: str,
    query_params: Sequence[tuple[str, str]] = (),
    headers: dict[str, str] | None = None,
    payload: bytes = b"",
    *,
    now: dt.datetime | None = None,
) -> tuple[str, dict[str, str]]:
    """Sign an HTTP request for S3 / Cloudflare R2 with AWS Signature Version 4.

    Returns `(request_target, signed_headers_dict)`.
    """
    stamp = now or dt.datetime.now(dt.timezone.utc)
    amz_date = stamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = stamp.strftime("%Y%m%d")
    region = (creds.region or "auto").strip() or "auto"
    service = "s3"

    payload_hash = hashlib.sha256(payload).hexdigest()
    hdrs: dict[str, str] = {}
    for k, v in (headers or {}).items():
        hdrs[k.lower().strip()] = " ".join(str(v).strip().split())
    hdrs["host"] = host_header
    hdrs["x-amz-date"] = amz_date
    hdrs["x-amz-content-sha256"] = payload_hash

    encoded_params = sorted((s3_quote(k), s3_quote(v)) for k, v in query_params)
    canonical_query = "&".join(f"{ek}={ev}" for ek, ev in encoded_params)

    signed_names = sorted(hdrs.keys())
    canonical_headers = "".join(f"{name}:{hdrs[name]}\n" for name in signed_names)
    signed_headers = ";".join(signed_names)

    canon_uri = canonical_uri if canonical_uri.startswith("/") else "/" + canonical_uri
    canonical_request = "\n".join(
        (
            method.upper(),
            canon_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        )
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )
    k_date = _hmac_sha256(("AWS4" + creds.secret_access_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    out_headers = dict(hdrs)
    out_headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={creds.access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    request_target = canon_uri + (f"?{canonical_query}" if canonical_query else "")
    return request_target, out_headers


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _xml_text(elem: ET.Element, child_name: str, default: str = "") -> str:
    for child in elem:
        if _strip_ns(child.tag) == child_name:
            return child.text or default
    return default


def _parse_s3_error_xml(raw: bytes) -> tuple[str, str]:
    if not raw:
        return "", ""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return "", raw.decode("utf-8", errors="replace")[:300]
    if _strip_ns(root.tag) != "Error":
        return "", raw.decode("utf-8", errors="replace")[:300]
    return _xml_text(root, "Code"), _xml_text(root, "Message")


@dataclasses.dataclass(frozen=True)
class ObjectEntry:
    key: str
    size: int
    etag: str
    storage_class: str = "STANDARD"
    last_modified: str = ""


@dataclasses.dataclass(frozen=True)
class ListPage:
    objects: list[ObjectEntry]
    is_truncated: bool
    next_continuation_token: str = ""
    last_key: str = ""


@dataclasses.dataclass(frozen=True)
class DownloadResult:
    bytes_written: int
    md5_hex: str
    via_cdn_cache: bool


@dataclasses.dataclass(frozen=True)
class DeleteBatchResult:
    deleted: list[str]
    errors: dict[str, str]


class R2UsageLedger:
    """Thread-safe UTC-monthly R2 call-count and estimated USD cost tracker."""

    def __init__(self, data_dir: Path, *, flush_interval: float = 30.0) -> None:
        self.data_dir = Path(data_dir)
        self.flush_interval = max(0.2, float(flush_interval))
        self._lock = threading.Lock()
        self._month = current_utc_month()
        self._dirty = False
        self._last_flush = time.monotonic()
        self._counters = self._empty_counters()
        self._load_month_locked(self._month)

    @staticmethod
    def _empty_counters() -> dict[str, int]:
        return {
            "class_a_ops": 0,
            "ia_class_a_ops": 0,
            "class_b_ops": 0,
            "ia_class_b_ops": 0,
            "ia_retrieval_bytes": 0,
            "free_delete_ops": 0,
            "deleted_objects": 0,
            "cdn_hit_ops": 0,
            "skipped_existing_ops": 0,
            "zero_byte_ops": 0,
            "bytes_transferred": 0,
        }

    def _path_for_month(self, month: str) -> Path:
        return self.data_dir / f"r2-usage-{month}.json"

    def _load_month_locked(self, month: str) -> None:
        self._month = month
        self._counters = self._empty_counters()
        path = self._path_for_month(month)
        if not path.is_file():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for key in self._counters:
            try:
                self._counters[key] = max(0, int(loaded.get(key, 0)))
            except (TypeError, ValueError):
                pass

    def _roll_month_if_needed_locked(self) -> None:
        now_month = current_utc_month()
        if now_month != self._month:
            if self._dirty:
                self._write_locked()
            self._load_month_locked(now_month)

    def _write_locked(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = self._snapshot_locked()
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        target = self._path_for_month(self._month)
        if target.is_file():
            with target.open("r+b") as fh:
                fh.seek(0)
                fh.write(raw)
                fh.truncate()
                fh.flush()
        else:
            tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                tmp.write_bytes(raw)
                os.replace(tmp, target)
            except Exception:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
                raise
        self._dirty = False
        self._last_flush = time.monotonic()

    def _maybe_flush_locked(self) -> None:
        if self._dirty and (time.monotonic() - self._last_flush) >= self.flush_interval:
            try:
                self._write_locked()
            except OSError:
                self._dirty = True
                self._last_flush = time.monotonic()

    def _snapshot_locked(self) -> dict[str, Any]:
        c = dict(self._counters)
        paid_a = max(0, c["class_a_ops"] - FREE_CLASS_A_MONTHLY)
        paid_b = max(0, c["class_b_ops"] - FREE_CLASS_B_MONTHLY)
        cost = (
            (paid_a / 1_000_000.0) * PRICE_CLASS_A_PER_MILLION
            + (c["ia_class_a_ops"] / 1_000_000.0) * PRICE_IA_CLASS_A_PER_MILLION
            + (paid_b / 1_000_000.0) * PRICE_CLASS_B_PER_MILLION
            + (c["ia_class_b_ops"] / 1_000_000.0) * PRICE_IA_CLASS_B_PER_MILLION
            + (c["ia_retrieval_bytes"] / 1_000_000_000.0) * PRICE_IA_RETRIEVAL_PER_GB
        )
        return {
            "month": self._month,
            **c,
            "free_class_a_limit": FREE_CLASS_A_MONTHLY,
            "free_class_b_limit": FREE_CLASS_B_MONTHLY,
            "free_class_a_remaining": max(0, FREE_CLASS_A_MONTHLY - c["class_a_ops"]),
            "free_class_b_remaining": max(0, FREE_CLASS_B_MONTHLY - c["class_b_ops"]),
            "paid_class_a_ops": paid_a,
            "paid_class_b_ops": paid_b,
            "estimated_cost_usd": round(cost, 6),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._roll_month_if_needed_locked()
            return self._snapshot_locked()

    def flush(self, *, ignore_errors: bool = False) -> dict[str, Any]:
        with self._lock:
            self._roll_month_if_needed_locked()
            try:
                self._write_locked()
            except OSError:
                if not ignore_errors:
                    raise
                self._dirty = True
                self._last_flush = time.monotonic()
            return self._snapshot_locked()

    def can_spend_class_a(self, cost_mode: str, max_free_class_a: int = DEFAULT_SAFE_FREE_CLASS_A) -> tuple[bool, bool]:
        """Return `(allowed, uses_paid_tier)` for a Class A operation (ListBuckets / ListObjectsV2)."""
        with self._lock:
            self._roll_month_if_needed_locked()
            cap = min(FREE_CLASS_A_MONTHLY, max(0, int(max_free_class_a)))
            cur = self._counters["class_a_ops"]
            uses_paid = cur >= FREE_CLASS_A_MONTHLY
            if cost_mode == "free-only":
                return (cur < cap, cur >= cap)
            return (True, uses_paid)

    def can_spend_class_b(
        self,
        storage_class: str,
        cost_mode: str,
        max_free_class_b: int = DEFAULT_SAFE_FREE_CLASS_B,
    ) -> tuple[bool, bool]:
        """Return `(allowed, uses_paid_tier)` for a Class B GetObject operation."""
        with self._lock:
            self._roll_month_if_needed_locked()
            sc = (storage_class or "STANDARD").upper()
            if sc == "STANDARD_IA":
                # STANDARD_IA Class B + retrieval GB have no free tier on R2.
                return (cost_mode != "free-only", True)
            cap = min(FREE_CLASS_B_MONTHLY, max(0, int(max_free_class_b)))
            cur = self._counters["class_b_ops"]
            uses_paid = cur >= FREE_CLASS_B_MONTHLY
            if cost_mode == "free-only":
                return (cur < cap, cur >= cap)
            return (True, uses_paid)

    def record_class_a(self, count: int = 1) -> None:
        with self._lock:
            self._roll_month_if_needed_locked()
            self._counters["class_a_ops"] += max(0, int(count))
            self._dirty = True
            self._maybe_flush_locked()

    def record_class_b(self, *, storage_class: str = "STANDARD", count: int = 1) -> None:
        with self._lock:
            self._roll_month_if_needed_locked()
            n = max(0, int(count))
            if (storage_class or "STANDARD").upper() == "STANDARD_IA":
                self._counters["ia_class_b_ops"] += n
            else:
                self._counters["class_b_ops"] += n
            self._dirty = True
            self._maybe_flush_locked()

    def record_transfer(self, bytes_count: int, *, storage_class: str = "STANDARD", counted_class_b: bool = False) -> None:
        with self._lock:
            self._roll_month_if_needed_locked()
            b = max(0, int(bytes_count))
            self._counters["bytes_transferred"] += b
            if counted_class_b and (storage_class or "STANDARD").upper() == "STANDARD_IA":
                self._counters["ia_retrieval_bytes"] += b
            self._dirty = True
            self._maybe_flush_locked()

    def record_free(
        self,
        *,
        delete_ops: int = 0,
        deleted_objects: int = 0,
        cdn_hits: int = 0,
        skipped_existing: int = 0,
        zero_byte: int = 0,
        bytes_transferred: int = 0,
    ) -> None:
        with self._lock:
            self._roll_month_if_needed_locked()
            self._counters["free_delete_ops"] += max(0, int(delete_ops))
            self._counters["deleted_objects"] += max(0, int(deleted_objects))
            self._counters["cdn_hit_ops"] += max(0, int(cdn_hits))
            self._counters["skipped_existing_ops"] += max(0, int(skipped_existing))
            self._counters["zero_byte_ops"] += max(0, int(zero_byte))
            self._counters["bytes_transferred"] += max(0, int(bytes_transferred))
            self._dirty = True
            self._maybe_flush_locked()

    def calibrate(self, *, class_a_ops: int | None = None, class_b_ops: int | None = None, reset: bool = False) -> dict[str, Any]:
        with self._lock:
            self._roll_month_if_needed_locked()
            if reset:
                self._counters = self._empty_counters()
            if class_a_ops is not None:
                self._counters["class_a_ops"] = max(0, int(class_a_ops))
            if class_b_ops is not None:
                self._counters["class_b_ops"] = max(0, int(class_b_ops))
            self._write_locked()
            return self._snapshot_locked()


class R2Client:
    """Thread-safe, connection-pooled pure-stdlib Cloudflare R2 S3 client."""

    def __init__(
        self,
        creds: R2Credentials,
        *,
        ledger: R2UsageLedger | None = None,
        timeout: float = 60.0,
        max_retries: int = 5,
    ) -> None:
        self.creds = creds
        self.ledger = ledger
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        endpoint = creds.resolve_endpoint_url()
        parsed = urllib.parse.urlsplit(endpoint)
        self.scheme = parsed.scheme.lower()
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if self.scheme == "https" else 80)
        self.netloc = parsed.netloc
        self.base_path = parsed.path.rstrip("/")
        self._ssl_ctx = ssl.create_default_context()
        self._local = threading.local()
        self._pool_lock = threading.Lock()
        self._all_conns: dict[int, dict[tuple[str, str, int], http.client.HTTPConnection]] = {}
        self._bucket_delete_mode: dict[str, tuple[str, int]] = {}

    def _thread_pool(self) -> dict[tuple[str, str, int], http.client.HTTPConnection]:
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = {}
            self._local.pool = pool
            with self._pool_lock:
                self._all_conns[threading.get_ident()] = pool
        return pool

    def _get_conn(
        self,
        scheme: str,
        host: str,
        port: int,
        *,
        timeout: float | None = None,
    ) -> http.client.HTTPConnection:
        pool = self._thread_pool()
        key = (scheme, host, port)
        eff_timeout = self.timeout if timeout is None else float(timeout)
        conn = pool.get(key)
        if conn is None:
            if scheme == "https":
                conn = http.client.HTTPSConnection(host, port, timeout=eff_timeout, context=self._ssl_ctx)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=eff_timeout)
            pool[key] = conn
        else:
            conn.timeout = eff_timeout
            if conn.sock is not None:
                with contextlib.suppress(OSError):
                    conn.sock.settimeout(eff_timeout)
        return conn

    def _drop_conn(self, scheme: str, host: str, port: int) -> None:
        pool = self._thread_pool()
        conn = pool.pop((scheme, host, port), None)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    def close(self) -> None:
        with self._pool_lock:
            pools = list(self._all_conns.values())
            self._all_conns.clear()
        for pool in pools:
            for conn in pool.values():
                with contextlib.suppress(Exception):
                    conn.close()
            pool.clear()

    def _retry_sleep(self, attempt: int, retry_after: str | None = None) -> None:
        delay: float | None = None
        if retry_after:
            try:
                delay = max(0.05, min(30.0, float(retry_after.strip())))
            except ValueError:
                delay = None
        if delay is None:
            delay = min(30.0, 0.25 * (2 ** attempt)) + random.uniform(0.0, 0.15)
        time.sleep(delay)

    def _signed_request(
        self,
        method: str,
        rel_path: str,
        *,
        query_params: Sequence[tuple[str, str]] = (),
        headers: dict[str, str] | None = None,
        payload: bytes = b"",
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> tuple[int, http.client.HTTPMessage, bytes]:
        canon_uri = self.base_path + (rel_path if rel_path.startswith("/") else "/" + rel_path)
        retries = self.max_retries if max_retries is None else max(1, int(max_retries))
        last_err: Exception | None = None
        for attempt in range(retries):
            req_target, signed_hdrs = sign_s3_request(
                self.creds,
                method,
                self.netloc,
                canon_uri,
                query_params=query_params,
                headers=headers,
                payload=payload,
            )
            try:
                conn = self._get_conn(self.scheme, self.host, self.port, timeout=timeout)
                conn.request(method, req_target, body=payload if payload else None, headers=signed_hdrs)
                resp = conn.getresponse()
                body = resp.read()
                if resp.status in _RETRY_STATUSES and attempt + 1 < retries:
                    ra = resp.getheader("Retry-After")
                    self._drop_conn(self.scheme, self.host, self.port)
                    self._retry_sleep(attempt, ra)
                    continue
                if resp.status >= 400:
                    code, msg = _parse_s3_error_xml(body)
                    raise R2HttpError(
                        resp.status,
                        msg or resp.reason or "S3 request failed",
                        code=code,
                        body=body.decode("utf-8", errors="replace")[:500],
                    )
                return resp.status, resp.headers, body
            except R2HttpError:
                raise
            except (http.client.HTTPException, OSError, TimeoutError, ssl.SSLError) as err:
                last_err = err
                self._drop_conn(self.scheme, self.host, self.port)
                if attempt + 1 >= retries:
                    raise R2Error(f"R2 S3 request failed after {retries} attempts: {err}") from err
                self._retry_sleep(attempt)
        raise R2Error(f"R2 S3 request failed: {last_err}")

    def list_buckets(self) -> list[dict[str, str]]:
        """List all buckets in the R2 account (1 Class A op)."""
        if self.ledger is not None:
            self.ledger.record_class_a(1)
        _, _, body = self._signed_request("GET", "/")
        root = ET.fromstring(body)
        buckets: list[dict[str, str]] = []
        for elem in root.iter():
            if _strip_ns(elem.tag) == "Bucket":
                name = _xml_text(elem, "Name")
                if name:
                    buckets.append(
                        {
                            "name": name,
                            "creation_date": _xml_text(elem, "CreationDate"),
                        }
                    )
        return buckets

    def list_objects_v2(
        self,
        bucket: str,
        *,
        prefix: str = "",
        start_after: str = "",
        continuation_token: str = "",
        max_keys: int = 1000,
    ) -> ListPage:
        """Fetch a single flat ListObjectsV2 page (1 Class A op per up to 1,000 objects)."""
        if self.ledger is not None:
            self.ledger.record_class_a(1)
        params: list[tuple[str, str]] = [
            ("encoding-type", "url"),
            ("list-type", "2"),
            ("max-keys", str(max(1, min(1000, int(max_keys))))),
        ]
        if prefix:
            params.append(("prefix", prefix))
        if continuation_token:
            params.append(("continuation-token", continuation_token))
        elif start_after:
            params.append(("start-after", start_after))

        _, _, body = self._signed_request("GET", f"/{s3_quote(bucket)}", query_params=params)
        root = ET.fromstring(body)
        enc_type = _xml_text(root, "EncodingType").lower()
        decode = urllib.parse.unquote_plus if enc_type == "url" else (lambda s: s)
        is_truncated = _xml_text(root, "IsTruncated").lower() == "true"
        next_token = _xml_text(root, "NextContinuationToken")

        entries: list[ObjectEntry] = []
        for child in root:
            if _strip_ns(child.tag) != "Contents":
                continue
            raw_key = _xml_text(child, "Key")
            key = decode(raw_key)
            size = int(_xml_text(child, "Size", "0") or "0")
            etag = _xml_text(child, "ETag").strip('"').strip()
            sc = (_xml_text(child, "StorageClass", "STANDARD") or "STANDARD").upper()
            lm = _xml_text(child, "LastModified")
            entries.append(
                ObjectEntry(key=key, size=size, etag=etag, storage_class=sc, last_modified=lm)
            )
        last_key = entries[-1].key if entries else start_after
        return ListPage(
            objects=entries,
            is_truncated=is_truncated,
            next_continuation_token=next_token,
            last_key=last_key,
        )

    def _stream_to_temp(
        self,
        conn: http.client.HTTPConnection,
        method_url: str,
        headers: dict[str, str],
        dest_temp: Path,
        *,
        expected_size: int | None,
        expected_etag: str,
        chunk_size: int,
    ) -> tuple[int, str, http.client.HTTPMessage]:
        conn.request("GET", method_url, headers=headers)
        resp = conn.getresponse()
        if resp.status in _RETRY_STATUSES:
            ra = resp.getheader("Retry-After")
            resp.read()
            raise _RetryableHttp(resp.status, ra)
        if resp.status != 200:
            body = resp.read(65536)
            code, msg = _parse_s3_error_xml(body)
            raise R2HttpError(
                resp.status,
                msg or resp.reason or "GET failed",
                code=code,
                body=body[:2048].decode("utf-8", errors="replace"),
            )
        md5 = hashlib.md5()
        total = 0
        with dest_temp.open("wb") as out:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                out.write(chunk)
                md5.update(chunk)
                total += len(chunk)
        md5_hex = md5.hexdigest()
        if expected_size is not None and total < expected_size:
            raise R2TruncatedReadError(f"Size mismatch: expected {expected_size}, got {total}")
        if expected_size is not None and total != expected_size:
            raise R2IntegrityError(f"Size mismatch: expected {expected_size}, got {total}")
        etag = (expected_etag or "").strip('"').strip()
        if etag and _HEX32_RE.fullmatch(etag) and md5_hex.lower() != etag.lower():
            raise R2IntegrityError(f"MD5 ETag mismatch: expected {etag.lower()}, got {md5_hex}")
        return total, md5_hex, resp.headers

    def _try_download_via_cdn(
        self,
        public_base_url: str,
        key: str,
        dest_temp: Path,
        *,
        expected_size: int | None,
        expected_etag: str,
        storage_class: str,
        chunk_size: int,
    ) -> DownloadResult | None:
        base = public_base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(base)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        req_path = parsed.path.rstrip("/") + "/" + s3_quote_path(key).lstrip("/")
        for attempt in range(2):
            try:
                conn = self._get_conn(scheme, host, port)
                total, md5_hex, resp_headers = self._stream_to_temp(
                    conn,
                    req_path,
                    {"Host": parsed.netloc, "Accept-Encoding": "identity"},
                    dest_temp,
                    expected_size=expected_size,
                    expected_etag=expected_etag,
                    chunk_size=chunk_size,
                )
                cf_cache = (resp_headers.get("CF-Cache-Status") or "").strip().upper()
                via_hit = cf_cache in _CF_CACHE_HIT_STATUSES
                if self.ledger is not None:
                    if via_hit:
                        self.ledger.record_free(cdn_hits=1, bytes_transferred=total)
                    else:
                        self.ledger.record_class_b(storage_class=storage_class, count=1)
                        self.ledger.record_transfer(total, storage_class=storage_class, counted_class_b=True)
                return DownloadResult(bytes_written=total, md5_hex=md5_hex, via_cdn_cache=via_hit)
            except _RetryableHttp:
                self._drop_conn(scheme, host, port)
                if attempt == 0:
                    time.sleep(0.15)
                    continue
                return None
            except (R2HttpError, R2IntegrityError, http.client.HTTPException, OSError, TimeoutError, ssl.SSLError):
                self._drop_conn(scheme, host, port)
                with contextlib.suppress(OSError):
                    dest_temp.unlink(missing_ok=True)
                return None
        return None

    def download_object_to_file(
        self,
        bucket: str,
        key: str,
        dest_temp: Path,
        *,
        expected_size: int | None = None,
        expected_etag: str = "",
        storage_class: str = "STANDARD",
        public_base_url: str | None = None,
        chunk_size: int = 256 * 1024,
    ) -> DownloadResult:
        """Download an object cleanly with CDN cache-first optimization + SigV4 fallback and MD5 verification."""
        if public_base_url:
            cdn_res = self._try_download_via_cdn(
                public_base_url,
                key,
                dest_temp,
                expected_size=expected_size,
                expected_etag=expected_etag,
                storage_class=storage_class,
                chunk_size=chunk_size,
            )
            if cdn_res is not None:
                return cdn_res

        rel_path = f"/{s3_quote(bucket)}/{s3_quote_path(key)}"
        canon_uri = self.base_path + rel_path
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            if self.ledger is not None:
                self.ledger.record_class_b(storage_class=storage_class, count=1)
            req_target, signed_hdrs = sign_s3_request(
                self.creds,
                "GET",
                self.netloc,
                canon_uri,
            )
            try:
                conn = self._get_conn(self.scheme, self.host, self.port)
                total, md5_hex, _ = self._stream_to_temp(
                    conn,
                    req_target,
                    signed_hdrs,
                    dest_temp,
                    expected_size=expected_size,
                    expected_etag=expected_etag,
                    chunk_size=chunk_size,
                )
                if self.ledger is not None:
                    self.ledger.record_transfer(total, storage_class=storage_class, counted_class_b=True)
                return DownloadResult(bytes_written=total, md5_hex=md5_hex, via_cdn_cache=False)
            except _RetryableHttp as retry_exc:
                self._drop_conn(self.scheme, self.host, self.port)
                last_err = R2HttpError(retry_exc.status, "Retryable upstream status")
                if attempt + 1 >= self.max_retries:
                    raise last_err from None
                self._retry_sleep(attempt, retry_exc.retry_after)
            except R2TruncatedReadError as err:
                last_err = err
                self._drop_conn(self.scheme, self.host, self.port)
                with contextlib.suppress(OSError):
                    dest_temp.unlink(missing_ok=True)
                if attempt + 1 >= self.max_retries:
                    raise
                self._retry_sleep(attempt)
            except R2IntegrityError:
                self._drop_conn(self.scheme, self.host, self.port)
                with contextlib.suppress(OSError):
                    dest_temp.unlink(missing_ok=True)
                raise
            except R2HttpError:
                self._drop_conn(self.scheme, self.host, self.port)
                with contextlib.suppress(OSError):
                    dest_temp.unlink(missing_ok=True)
                raise
            except (http.client.HTTPException, OSError, TimeoutError, ssl.SSLError) as err:
                last_err = err
                self._drop_conn(self.scheme, self.host, self.port)
                with contextlib.suppress(OSError):
                    dest_temp.unlink(missing_ok=True)
                if attempt + 1 >= self.max_retries:
                    raise R2Error(f"Download failed for {bucket}/{key}: {err}") from err
                self._retry_sleep(attempt)
        raise R2Error(f"Download failed for {bucket}/{key}: {last_err}")

    def delete_object(
        self,
        bucket: str,
        key: str,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        """Delete a single object (Free $0.00 operation on R2)."""
        if self.ledger is not None:
            self.ledger.record_free(delete_ops=1)
        rel_path = f"/{s3_quote(bucket)}/{s3_quote_path(key)}"
        try:
            self._signed_request("DELETE", rel_path, timeout=timeout, max_retries=max_retries)
        except R2HttpError as err:
            if err.status == 404 or err.code == "NoSuchKey":
                pass
            else:
                raise
        if self.ledger is not None:
            self.ledger.record_free(deleted_objects=1)

    def _delete_xml_batch(
        self,
        bucket: str,
        batch: Sequence[str],
        *,
        quiet: bool = True,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Execute one Free $0.00 S3 DeleteObjects XML POST request."""
        if not batch:
            return [], {}
        quiet_tag = "true" if quiet else "false"
        body_parts = [f"<Delete><Quiet>{quiet_tag}</Quiet>"]
        for k in batch:
            body_parts.append(f"<Object><Key>{xml_escape(k)}</Key></Object>")
        body_parts.append("</Delete>")
        xml_bytes = "".join(body_parts).encode("utf-8")
        md5_b64 = base64.b64encode(hashlib.md5(xml_bytes).digest()).decode("ascii")
        if self.ledger is not None:
            self.ledger.record_free(delete_ops=1)
        _, _, resp_body = self._signed_request(
            "POST",
            f"/{s3_quote(bucket)}",
            query_params=[("delete", "")],
            headers={
                "content-md5": md5_b64,
                "content-type": "application/xml",
            },
            payload=xml_bytes,
            timeout=timeout,
            max_retries=max_retries,
        )
        batch_deleted: list[str] = []
        batch_errors: dict[str, str] = {}
        try:
            root = ET.fromstring(resp_body)
            for child in root:
                name = _strip_ns(child.tag)
                if name == "Deleted":
                    k = _xml_text(child, "Key")
                    if k:
                        batch_deleted.append(k)
                elif name == "Error":
                    k = _xml_text(child, "Key")
                    code = _xml_text(child, "Code")
                    msg = _xml_text(child, "Message")
                    if k:
                        batch_errors[k] = f"{code}: {msg}".strip(": ") or "Delete error"
        except ET.ParseError as parse_err:
            return [], {k: f"Invalid DeleteObjects response XML: {parse_err}" for k in batch}

        if quiet and not batch_deleted:
            batch_deleted = [k for k in batch if k not in batch_errors]
        else:
            accounted = set(batch_deleted) | set(batch_errors.keys())
            if not batch_deleted and not batch_errors:
                batch_deleted = list(batch)
            elif len(accounted) < len(batch):
                for k in batch:
                    if k not in accounted:
                        batch_errors[k] = "Missing from DeleteObjects result"
        if self.ledger is not None and batch_deleted:
            self.ledger.record_free(deleted_objects=len(batch_deleted))
        return batch_deleted, batch_errors

    def delete_objects(
        self,
        bucket: str,
        keys: Sequence[str],
        *,
        batch_size: int = 250,
        quiet: bool = True,
        concurrency: int = 1,
        log: Callable[[str], None] | None = None,
    ) -> DeleteBatchResult:
        """Delete keys via Free $0.00 R2 DeleteObjects with adaptive fallback to smaller batches / single DELETE."""
        deleted: list[str] = []
        errors: dict[str, str] = {}
        xml_safe: list[str] = []
        for k in keys:
            if _XML_ILLEGAL_CTRL_RE.search(k):
                try:
                    self.delete_object(bucket, k, timeout=min(self.timeout, 25.0), max_retries=2)
                    deleted.append(k)
                except Exception as err:
                    errors[k] = str(err)
            else:
                xml_safe.append(k)

        if not xml_safe:
            return DeleteBatchResult(deleted=deleted, errors=errors)

        req_batch = max(1, min(1000, int(batch_size)))
        conc = max(1, int(concurrency))

        def _is_non_retryable_http(err: Exception) -> bool:
            return isinstance(err, R2HttpError) and err.status < 500 and err.status not in (408, 429)

        def _delete_via_single(sub_keys: Sequence[str]) -> tuple[list[str], dict[str, str]]:
            if not sub_keys:
                return [], {}
            probe_timeout = min(self.timeout, 20.0)
            self.delete_object(bucket, sub_keys[0], timeout=probe_timeout, max_retries=2)
            ok_keys: list[str] = [sub_keys[0]]
            err_map: dict[str, str] = {}
            if len(sub_keys) == 1:
                return ok_keys, err_map

            rest = sub_keys[1:]
            max_w = min(len(rest), max(8, conc * 2))
            last_r2_err: R2Error | None = None

            def _del_one(one_key: str) -> tuple[str, bool, str, R2Error | None]:
                try:
                    self.delete_object(bucket, one_key, timeout=probe_timeout, max_retries=2)
                    return one_key, True, "", None
                except R2HttpError as http_err:
                    if _is_non_retryable_http(http_err):
                        return one_key, False, str(http_err), None
                    return one_key, False, str(http_err), http_err
                except R2Error as r2_err:
                    return one_key, False, str(r2_err), r2_err

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as pool:
                for one_key, ok, msg, r2_err in pool.map(_del_one, rest):
                    if ok:
                        ok_keys.append(one_key)
                    else:
                        err_map[one_key] = msg
                        if r2_err is not None:
                            last_r2_err = r2_err
            if len(ok_keys) == 1 and len(err_map) == len(rest) and last_r2_err is not None:
                raise last_r2_err
            return ok_keys, err_map

        def _process_chunk(chunk: list[str]) -> tuple[list[str], dict[str, str]]:
            if not chunk:
                return [], {}
            with self._pool_lock:
                cur_mode = self._bucket_delete_mode.get(bucket, ("batch", req_batch))[0]
            if cur_mode == "single":
                return _delete_via_single(chunk)

            try:
                res_ok = self._delete_xml_batch(
                    bucket,
                    chunk,
                    quiet=quiet,
                    timeout=min(self.timeout, 35.0),
                    max_retries=2,
                )
                with self._pool_lock:
                    if bucket not in self._bucket_delete_mode:
                        self._bucket_delete_mode[bucket] = ("batch", req_batch)
                return res_ok
            except R2Error as err:
                if _is_non_retryable_http(err):
                    raise
                d_list: list[str] = []
                e_dict: dict[str, str] = {}
                remaining_keys: Sequence[str] = chunk
                if len(chunk) > 15:
                    with self._pool_lock:
                        self._bucket_delete_mode[bucket] = ("batch", 15)
                    if log is not None:
                        log(f"R2 DeleteObjects on {bucket} timed out at batch={len(chunk)}; adapting to 15-key batches ({err})")
                    fallback_from: int | None = None
                    for idx in range(0, len(chunk), 15):
                        sub = chunk[idx : idx + 15]
                        try:
                            sd, se = self._delete_xml_batch(
                                bucket,
                                sub,
                                quiet=quiet,
                                timeout=min(self.timeout, 25.0),
                                max_retries=2,
                            )
                            d_list.extend(sd)
                            e_dict.update(se)
                        except R2Error as err2:
                            if _is_non_retryable_http(err2):
                                raise
                            fallback_from = idx
                            break
                    if fallback_from is None:
                        return d_list, e_dict
                    remaining_keys = chunk[fallback_from:]

                if log is not None:
                    log(f"R2 POST ?delete on {bucket} failed at <=15 keys; probing single DELETE fallback")
                d_rest, e_rest = _delete_via_single(remaining_keys)
                with self._pool_lock:
                    self._bucket_delete_mode[bucket] = ("single", 1)
                return d_list + d_rest, {**e_dict, **e_rest}

        with self._pool_lock:
            calibrated = bucket in self._bucket_delete_mode
            mode, eff_batch = self._bucket_delete_mode.get(bucket, ("batch", req_batch))

        if mode == "single":
            d0, e0 = _delete_via_single(xml_safe)
            deleted.extend(d0)
            errors.update(e0)
            return DeleteBatchResult(deleted=deleted, errors=errors)

        eff_batch = max(1, min(1000, int(eff_batch)))
        if not calibrated:
            first_chunk = xml_safe[:eff_batch]
            d_first, e_first = _process_chunk(first_chunk)
            deleted.extend(d_first)
            errors.update(e_first)

            rest_keys = xml_safe[len(first_chunk) :]
            if not rest_keys:
                return DeleteBatchResult(deleted=deleted, errors=errors)

            with self._pool_lock:
                mode, new_batch = self._bucket_delete_mode.get(bucket, ("batch", req_batch))
            if mode == "single":
                d_rest, e_rest = _delete_via_single(rest_keys)
                deleted.extend(d_rest)
                errors.update(e_rest)
                return DeleteBatchResult(deleted=deleted, errors=errors)
            eff_batch = max(1, min(1000, int(new_batch)))
        else:
            rest_keys = xml_safe

        rem_chunks = [rest_keys[i : i + eff_batch] for i in range(0, len(rest_keys), eff_batch)]
        if conc <= 1 or len(rem_chunks) <= 1:
            for ch in rem_chunks:
                dc, ec = _process_chunk(ch)
                deleted.extend(dc)
                errors.update(ec)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(conc, len(rem_chunks))) as pool:
                for dc, ec in pool.map(_process_chunk, rem_chunks):
                    deleted.extend(dc)
                    errors.update(ec)

        return DeleteBatchResult(deleted=deleted, errors=errors)


class _RetryableHttp(Exception):
    def __init__(self, status: int, retry_after: str | None = None) -> None:
        self.status = status
        self.retry_after = retry_after


# --------------------------------------------------------------------------- safe Drive path mapping


def sanitize_segment(segment: str) -> str:
    """Convert an arbitrary S3 key path component into a safe, contained POSIX segment."""
    clean = segment.replace("\x00", "%00")
    if clean == "":
        return "__r2_empty__"
    if clean == ".":
        return "__r2_dot__"
    if clean == "..":
        return "__r2_dotdot__"
    return clean


def resolve_drive_output_root(
    drive_mount: Path,
    output_dir: str | None,
    *,
    default_subdir: str = "r2-backup",
    fallback_root: Path | None = None,
) -> Path:
    """Resolve and strictly contain an output directory inside MyDrive (or local fallback)."""
    mount = drive_mount if drive_mount.is_dir() else (fallback_root or drive_mount)
    mount_resolved = mount.resolve()
    raw = (output_dir or "").strip()
    if raw.startswith("drive://"):
        raw = raw[len("drive://") :]
    elif raw.startswith("/content/drive/MyDrive/"):
        raw = raw[len("/content/drive/MyDrive/") :]
    elif raw == "/content/drive/MyDrive":
        raw = ""
    elif raw.startswith("MyDrive/"):
        raw = raw[len("MyDrive/") :]
    elif raw == "MyDrive":
        raw = ""
    raw = raw.lstrip("/")
    if not raw:
        raw = default_subdir
    if "\x00" in raw:
        raise ValueError("输出目录路径包含非法空字节")
    candidate = (mount_resolved / raw).resolve()
    if not candidate.is_relative_to(mount_resolved):
        raise ValueError(f"输出目录超出允许根目录 {mount_resolved}: {output_dir!r}")
    return candidate


def map_object_to_drive_relpath(
    bucket: str,
    key: str,
    *,
    size: int,
    include_bucket: bool = True,
) -> tuple[str, bool]:
    """Map `(bucket, key, size)` to `(relative_posix_path, is_dir_marker)` safely."""
    safe_bucket = sanitize_segment(bucket.strip("/"))
    if key == "":
        parts = ["__r2_empty__"]
        is_dir = False
    elif key.endswith("/"):
        trimmed = key[:-1]
        raw_parts = trimmed.split("/") if trimmed else ["__r2_empty__"]
        parts = [sanitize_segment(p) for p in raw_parts]
        if int(size) == 0:
            is_dir = True
        else:
            parts.append("__r2_slash_object__")
            is_dir = False
    else:
        parts = [sanitize_segment(p) for p in key.split("/")]
        is_dir = False

    if include_bucket:
        parts = [safe_bucket, *parts]
    return "/".join(parts), is_dir
