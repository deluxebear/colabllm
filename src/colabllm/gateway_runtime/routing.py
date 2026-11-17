"""Service discovery filtering, model probing cache, LRU KV affinity, slot queueing, and pre-response failover."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import http.client
import json
from pathlib import Path
import re
import ssl
import sys
import threading
import time
from typing import Any, Callable
import urllib.parse

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from cli_support import shared_ssl_context  # noqa: E402
from http_client import BROWSER_UA  # noqa: E402
from instance_discovery import INSTANCE_ID_RE, discover_service_pairs, list_raw_instances  # noqa: E402
from processes import runner_alive  # noqa: E402

from .auth import GatewayError  # noqa: E402
from .catalog import (  # noqa: E402
    GATEWAY_RUNTIME_SPECS,
    RuntimeGatewaySpec,
    get_runtime_spec,
    ollama_is_gateway_compatible,
    parse_slot_capacity,
    runtime_allows_v1_route,
)

PREFIXED_ROUTE_RE = re.compile(r"^/r/([^/]+)(/.*)?$")


def compute_upstream_auth_header(state: dict[str, Any]) -> str | None:
    """Derive upstream Authorization header value from raw instance state dict."""
    import base64

    api_token = state.get("api_token")
    if isinstance(api_token, str) and api_token.strip():
        return f"Bearer {api_token.strip()}"
    user = state.get("auth_user")
    password = state.get("auth_pass")
    if isinstance(user, str) and user and isinstance(password, str) and password:
        raw = f"{user}:{password}".encode("utf-8")
        return f"Basic {base64.b64encode(raw).decode('ascii')}"
    return None


def redact_upstream_url(raw_url: str | None) -> str | None:
    """Strip query parameters and fragments from upstream URLs before status exposure."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    parts = urllib.parse.urlsplit(raw_url.strip())
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def build_upstream_connection(raw_url: str, *, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    """Create a one-shot HTTP(S)Connection and base path prefix for an upstream tunnel URL."""
    parts = urllib.parse.urlsplit(raw_url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"无效的上游 URL: {raw_url!r}")
    base_path = parts.path.rstrip("/")
    host = parts.hostname or ""
    if scheme == "https":
        port = parts.port or 443
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host,
            port,
            timeout=timeout,
            context=shared_ssl_context(),
        )
    else:
        port = parts.port or 80
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    return conn, base_path


@dataclass(frozen=True)
class ModelTarget:
    upstream_model: str
    runtime: str
    pinned_instance: str | None
    requested_model: str


@dataclass
class ReplicaRuntime:
    service: str
    instance: str
    instance_arg: str
    session_name: str
    state_path: Path
    state_mtime_ns: int
    state_size: int
    url: str
    auth_header: str | None
    transport: str
    env: dict[str, Any]
    slot_capacity: int
    alive: bool
    ready: bool

    @property
    def key(self) -> str:
        return f"{self.service}:{self.instance}"

    @property
    def fingerprint(self) -> tuple[int, int, str]:
        return (self.state_mtime_ns, self.state_size, self.url)


@dataclass
class _ProbeEntry:
    fingerprint: tuple[int, int, str]
    expires_at: float
    models: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class _ReplicaRuntimeState:
    inflight: int = 0
    cooldown_until: float = 0.0


def extract_affinity_key(normalized_v1_path: str, parsed_body: Any) -> str:
    """Compute stable protocol-aware root prefix hash for KV-cache affinity."""
    if not isinstance(parsed_body, dict):
        return ""
    payload: Any = ""
    if normalized_v1_path == "/v1/chat/completions":
        msgs = parsed_body.get("messages")
        sys_msgs = []
        first_user = None
        if isinstance(msgs, list):
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role") or "").lower()
                if role in ("system", "developer"):
                    sys_msgs.append(msg)
                elif role == "user" and first_user is None:
                    first_user = msg
        payload = {"s": sys_msgs, "u": first_user, "t": parsed_body.get("tools")}
    elif normalized_v1_path == "/v1/messages" or normalized_v1_path.startswith("/v1/messages/"):
        msgs = parsed_body.get("messages")
        first_msg = msgs[0] if isinstance(msgs, list) and msgs else None
        payload = {"s": parsed_body.get("system"), "m0": first_msg, "t": parsed_body.get("tools")}
    elif normalized_v1_path == "/v1/responses":
        inp = parsed_body.get("input")
        first_inp = inp[0] if isinstance(inp, list) and inp else (inp[:512] if isinstance(inp, str) else inp)
        payload = {"i": parsed_body.get("instructions"), "in0": first_inp, "t": parsed_body.get("tools")}
    elif normalized_v1_path == "/v1/completions":
        prompt = parsed_body.get("prompt")
        payload = prompt[:512] if isinstance(prompt, str) else (prompt[0][:512] if isinstance(prompt, list) and prompt and isinstance(prompt[0], str) else str(prompt)[:512])
    else:
        return ""

    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


class GatewayRouter:
    """Stateless discovery + concurrency control, TTL probing, LRU stickiness, and failover."""

    def __init__(
        self,
        *,
        state_root: Path,
        services_dir: Path | None,
        max_concurrent: int = 32,
        slot_wait_timeout: float = 5.0,
        affinity_grace_timeout: float = 1.0,
        cooldown_seconds: float = 15.0,
        probe_timeout: float = 3.0,
        probe_ttl_seconds: float = 30.0,
        max_affinity_entries: int = 1024,
        specs: dict[str, RuntimeGatewaySpec] | None = None,
    ) -> None:
        self.state_root = Path(state_root)
        self.services_dir = Path(services_dir) if services_dir is not None else self.state_root / "services"
        self.max_concurrent = max(1, int(max_concurrent))
        self.slot_wait_timeout = max(0.0, float(slot_wait_timeout))
        self.affinity_grace_timeout = max(0.0, float(affinity_grace_timeout))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.probe_timeout = max(0.2, float(probe_timeout))
        self.probe_ttl_seconds = max(1.0, float(probe_ttl_seconds))
        self.max_affinity_entries = max(16, int(max_affinity_entries))
        self.specs = GATEWAY_RUNTIME_SPECS if specs is None else specs
        self.global_semaphore = threading.BoundedSemaphore(self.max_concurrent)
        self._inflight_global = 0

        self._lock = threading.RLock()
        self._slot_cond = threading.Condition(self._lock)
        self._replica_states: dict[str, _ReplicaRuntimeState] = {}
        self._probe_cache: dict[str, _ProbeEntry] = {}
        self._inflight_probes: dict[str, threading.Event] = {}
        self._affinity_lru: OrderedDict[tuple[str, str, str], str] = OrderedDict()

    def _service_names(self) -> tuple[str, ...]:
        pairs = set(discover_service_pairs())
        pairs.update(self.specs.keys())
        return tuple(sorted(pairs))

    def scan_replicas(self, target_service: str | None = None) -> list[ReplicaRuntime]:
        """Perform lockless read-only discovery of local instance state files."""
        raw_items = list_raw_instances(
            self.services_dir,
            self._service_names(),
            target_service=target_service,
            alive_fn=runner_alive,
            on_corrupt="skip",
        )
        replicas: list[ReplicaRuntime] = []
        for item in raw_items:
            svc = item["service"]
            spec = get_runtime_spec(svc, self.specs)
            if spec is None or not spec.gateway_enabled:
                continue
            st = item["state"]
            env = st.get("env") if isinstance(st.get("env"), dict) else {}
            url = str(st.get("url") or "").strip()
            replicas.append(
                ReplicaRuntime(
                    service=svc,
                    instance=item["instance"],
                    instance_arg=item["instance_arg"],
                    session_name=item["session_name"],
                    state_path=item["state_path"],
                    state_mtime_ns=item["state_mtime_ns"],
                    state_size=item["state_size"],
                    url=url,
                    auth_header=compute_upstream_auth_header(st),
                    transport=str(st.get("transport") or "cloudflare"),
                    env=dict(env),
                    slot_capacity=parse_slot_capacity(spec, env),
                    alive=bool(item["alive"]),
                    ready=bool(item["ready"]),
                )
            )
        return replicas

    def _probe_single_upstream(self, rep: ReplicaRuntime) -> _ProbeEntry:
        if not rep.url:
            return _ProbeEntry(rep.fingerprint, time.monotonic() + self.probe_ttl_seconds, error="missing url")
        conn: http.client.HTTPConnection | None = None
        resp: http.client.HTTPResponse | None = None
        try:
            conn, base_path = build_upstream_connection(rep.url, timeout=self.probe_timeout)
            headers = {
                "User-Agent": BROWSER_UA,
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
            if rep.auth_header:
                headers["Authorization"] = rep.auth_header
            target = f"{base_path}/v1/models" if base_path else "/v1/models"
            conn.request("GET", target, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(2 * 1024 * 1024)
            if resp.status != 200:
                return _ProbeEntry(
                    rep.fingerprint,
                    time.monotonic() + min(5.0, self.probe_ttl_seconds),
                    error=f"HTTP {resp.status}",
                )
            data = json.loads(raw.decode("utf-8"))
            items = data.get("data") if isinstance(data, dict) else None
            models: list[str] = []
            if isinstance(items, list):
                for entry in items:
                    if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
                        models.append(entry["id"])
            elif isinstance(data, dict) and isinstance(data.get("models"), list):
                for entry in data["models"]:
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]:
                        models.append(entry["name"])
            if not models:
                return _ProbeEntry(
                    rep.fingerprint,
                    time.monotonic() + min(5.0, self.probe_ttl_seconds),
                    error="empty models list",
                )
            return _ProbeEntry(
                rep.fingerprint,
                time.monotonic() + self.probe_ttl_seconds,
                models=tuple(dict.fromkeys(models)),
            )
        except Exception as err:
            return _ProbeEntry(
                rep.fingerprint,
                time.monotonic() + min(5.0, self.probe_ttl_seconds),
                error=str(err),
            )
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

    def ensure_probed(self, rep: ReplicaRuntime, *, force: bool = False) -> _ProbeEntry:
        """Return cached or singleflight-probed model list for a READY replica."""
        now = time.monotonic()
        leader = False
        waiter: threading.Event | None = None
        with self._lock:
            cached = self._probe_cache.get(rep.key)
            if (
                not force
                and cached is not None
                and cached.fingerprint == rep.fingerprint
                and cached.expires_at > now
            ):
                return cached
            inflight = self._inflight_probes.get(rep.key)
            if inflight is None:
                inflight = threading.Event()
                self._inflight_probes[rep.key] = inflight
                leader = True
            else:
                waiter = inflight

        if not leader and waiter is not None:
            waiter.wait(timeout=self.probe_timeout + 0.5)
            with self._lock:
                cached = self._probe_cache.get(rep.key)
                if cached is not None and cached.fingerprint == rep.fingerprint:
                    return cached
                return _ProbeEntry(rep.fingerprint, time.monotonic() + 2.0, error="probe timeout")

        try:
            result = self._probe_single_upstream(rep)
            with self._lock:
                self._probe_cache[rep.key] = result
            return result
        finally:
            with self._lock:
                ev = self._inflight_probes.pop(rep.key, None)
                if ev is not None:
                    ev.set()

    def ensure_probed_many(self, replicas: list[ReplicaRuntime]) -> dict[str, _ProbeEntry]:
        """Probe multiple replicas concurrently, bounded by per-replica probe_timeout."""
        results: dict[str, _ProbeEntry] = {}
        if not replicas:
            return results
        if len(replicas) == 1:
            results[replicas[0].key] = self.ensure_probed(replicas[0])
            return results
        res_lock = threading.Lock()
        threads: list[threading.Thread] = []

        def _worker(r: ReplicaRuntime) -> None:
            entry = self.ensure_probed(r)
            with res_lock:
                results[r.key] = entry

        for rep in replicas:
            t = threading.Thread(target=_worker, args=(rep,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=self.probe_timeout + 1.0)
        return results

    def touch_probe_ttl(self, rep: ReplicaRuntime) -> None:
        with self._lock:
            cached = self._probe_cache.get(rep.key)
            if cached is not None and cached.fingerprint == rep.fingerprint and not cached.error:
                cached.expires_at = time.monotonic() + self.probe_ttl_seconds

    def invalidate_probe(self, rep_key: str) -> None:
        with self._lock:
            self._probe_cache.pop(rep_key, None)

    def mark_cooldown(self, rep_key: str) -> None:
        with self._lock:
            st = self._replica_states.setdefault(rep_key, _ReplicaRuntimeState())
            st.cooldown_until = time.monotonic() + self.cooldown_seconds
            self._probe_cache.pop(rep_key, None)

    def list_aggregated_models(self) -> list[dict[str, Any]]:
        """Build deduplicated standard OpenAI model list from READY compatible replicas."""
        all_reps = self.scan_replicas()
        eligible: list[ReplicaRuntime] = []
        now = time.monotonic()
        for rep in all_reps:
            if not rep.ready or not rep.url:
                continue
            if rep.service == "ollama" and not ollama_is_gateway_compatible(rep.env):
                continue
            with self._lock:
                st = self._replica_states.get(rep.key)
                if st is not None and st.cooldown_until > now:
                    continue
            eligible.append(rep)

        probed = self.ensure_probed_many(eligible)
        seen: set[tuple[str, str]] = set()
        models_out: list[dict[str, Any]] = []
        created_ts = int(time.time())
        for rep in eligible:
            p = probed.get(rep.key)
            if p is None or p.error:
                continue
            for up_model in p.models:
                pair = (rep.service, up_model)
                if pair in seen:
                    continue
                seen.add(pair)
                models_out.append({
                    "id": f"{up_model}@{rep.service}",
                    "object": "model",
                    "created": created_ts,
                    "owned_by": rep.service,
                })
        return models_out

    def parse_model_target(self, raw_model: str | None, *, candidates_hint: list[str] | None = None) -> ModelTarget:
        """Parse '<upstream_model>@<runtime>[:<instance_id|default>]' deterministically."""
        cleaned = (raw_model or "").strip() if isinstance(raw_model, str) else ""
        if not cleaned:
            raise GatewayError(400, "请求缺少有效的 model 字段")

        if "@" not in cleaned:
            hint = f"（可用示例：{', '.join(candidates_hint[:5])}）" if candidates_hint else ""
            raise GatewayError(400, f"模型名称 {cleaned!r} 缺少 @<runtime> 运行时后缀{hint}")

        base, _, rhs = cleaned.rpartition("@")
        if not base or not rhs:
            raise GatewayError(400, f"模型名称格式无效：{cleaned!r}")

        if rhs.lower().startswith("sha256:"):
            hint = f"（可用示例：{', '.join(candidates_hint[:5])}）" if candidates_hint else ""
            raise GatewayError(400, f"模型名称 {cleaned!r} 包含摘要但缺少 @<runtime> 运行时后缀{hint}")

        if ":" in rhs:
            runtime_part, _, inst_part = rhs.partition(":")
            if not inst_part:
                raise GatewayError(400, f"实例后缀不能为空：{cleaned!r}")
            if inst_part != "default" and not INSTANCE_ID_RE.fullmatch(inst_part):
                raise GatewayError(400, f"非法的实例标识：{inst_part!r}")
            pinned_instance: str | None = inst_part
        else:
            runtime_part = rhs
            pinned_instance = None

        spec = get_runtime_spec(runtime_part, self.specs)
        if spec is None or not spec.gateway_enabled:
            raise GatewayError(
                501,
                f"运行时 {runtime_part!r} 未在网关目录注册或未开放透传",
                oai_code="service_unregistered",
            )

        return ModelTarget(
            upstream_model=base,
            runtime=runtime_part,
            pinned_instance=pinned_instance,
            requested_model=cleaned,
        )

    def lookup_model_card(self, encoded_model_id: str) -> dict[str, Any]:
        """Resolve GET /v1/models/{id} strictly from gateway local probe cache."""
        decoded = urllib.parse.unquote(encoded_model_id)
        target = self.parse_model_target(decoded)
        reps = [
            r for r in self.scan_replicas(target.runtime)
            if r.ready and r.url and (r.service != "ollama" or ollama_is_gateway_compatible(r.env))
        ]
        if target.pinned_instance is not None:
            reps = [r for r in reps if r.instance == target.pinned_instance]
        probed = self.ensure_probed_many(reps)
        for rep in reps:
            p = probed.get(rep.key)
            if p is not None and not p.error and target.upstream_model in p.models:
                return {
                    "id": decoded,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": target.runtime,
                }
        raise GatewayError(
            404,
            f"未找到可用模型：{decoded!r}",
            oai_type="not_found_error",
            oai_code="model_not_found",
            anth_type="not_found_error",
        )

    def resolve_prefixed_voice_replica(self, service: str, pinned_instance: str | None) -> ReplicaRuntime:
        """Select READY TTS replica for prefix-only GET /r/<runtime>[:<inst>]/v1/audio/voices."""
        all_reps = self.scan_replicas(service)
        if pinned_instance is not None:
            matched = [r for r in all_reps if r.instance == pinned_instance]
            if not matched:
                raise GatewayError(
                    404,
                    f"未找到实例 {service}:{pinned_instance}",
                    oai_type="not_found_error",
                    oai_code="instance_not_found",
                )
            rep = matched[0]
            if not rep.alive:
                raise GatewayError(
                    503,
                    f"实例 {service}:{pinned_instance} 当前未运行（请先执行 colabllm {service} status）",
                    oai_type="service_unavailable_error",
                    oai_code="runtime_unavailable",
                )
            if not rep.ready:
                raise GatewayError(
                    503,
                    f"实例 {service}:{pinned_instance} 正在启动中（STARTING），请稍后重试或执行 colabllm {service} status",
                    oai_type="service_unavailable_error",
                    oai_code="runtime_starting",
                )
            return rep

        ready_reps = [r for r in all_reps if r.ready and r.url]
        starting_reps = [r for r in all_reps if r.alive and not r.ready]
        if not ready_reps:
            if starting_reps:
                raise GatewayError(
                    503,
                    f"运行时 {service} 正在启动中（STARTING），请执行 colabllm {service} status 排查",
                    oai_type="service_unavailable_error",
                    oai_code="runtime_starting",
                )
            raise GatewayError(
                503,
                f"运行时 {service} 当前无就绪实例",
                oai_type="service_unavailable_error",
                oai_code="runtime_unavailable",
            )
        if len(ready_reps) > 1:
            choices = ", ".join(f"{service}:{r.instance}" for r in ready_reps)
            raise GatewayError(
                400,
                f"运行时 {service} 存在多个活跃副本，请在路径指定实例 /r/<runtime>:<inst>/v1/audio/voices（候选：{choices}）",
            )
        return ready_reps[0]

    def resolve_candidate_pool(
        self,
        *,
        target: ModelTarget,
        method: str,
        normalized_v1_path: str,
        parsed_body: Any,
    ) -> list[ReplicaRuntime]:
        """Resolve filtered READY replicas supporting route + target upstream_model."""
        spec = get_runtime_spec(target.runtime, self.specs)
        assert spec is not None

        all_reps = self.scan_replicas(target.runtime)

        if target.pinned_instance is not None:
            matched = [r for r in all_reps if r.instance == target.pinned_instance]
            if not matched:
                raise GatewayError(
                    404,
                    f"未找到副本 {target.runtime}:{target.pinned_instance}",
                    oai_type="not_found_error",
                    oai_code="instance_not_found",
                )
            rep = matched[0]
            if not rep.alive:
                raise GatewayError(
                    503,
                    f"副本 {target.runtime}:{target.pinned_instance} 已停止（可检查 colabllm {target.runtime} status）",
                    oai_type="service_unavailable_error",
                    oai_code="runtime_unavailable",
                )
            if not rep.ready or not rep.url:
                raise GatewayError(
                    503,
                    f"副本 {target.runtime}:{target.pinned_instance} 正在启动中（STARTING），请执行 colabllm {target.runtime} status 查看进度",
                    oai_type="service_unavailable_error",
                    oai_code="runtime_starting",
                )
            if target.runtime == "ollama" and not ollama_is_gateway_compatible(rep.env):
                raise GatewayError(
                    503,
                    f"Ollama 副本 {rep.instance} 的 CT_OLLAMA_COMPAT 仅启用原生 ollama 模式，未开放 /v1/* 兼容接口",
                    oai_type="service_unavailable_error",
                    oai_code="runtime_compat_disabled",
                )
            if not runtime_allows_v1_route(target.runtime, spec, method, normalized_v1_path, env=rep.env):
                raise GatewayError(
                    404,
                    f"副本 {rep.key} 不支持或未启用路由 {method} {normalized_v1_path}",
                    oai_type="not_found_error",
                    oai_code="route_not_found",
                    anth_type="not_found_error",
                )
            probe = self.ensure_probed(rep)
            if probe.error:
                raise GatewayError(
                    502,
                    f"探测副本 {rep.key} 模型列表失败: {probe.error}",
                    oai_type="api_error",
                    oai_code="upstream_error",
                    anth_type="api_error",
                )
            if target.upstream_model not in probe.models:
                raise GatewayError(
                    404,
                    f"副本 {rep.key} 未加载模型 {target.upstream_model!r}",
                    oai_type="not_found_error",
                    oai_code="model_not_found",
                    anth_type="not_found_error",
                )
            return [rep]

        # Unpinned multi-replica discovery
        if not runtime_allows_v1_route(target.runtime, spec, method, normalized_v1_path, env=None):
            raise GatewayError(
                404,
                f"运行时 {target.runtime} 不支持路由 {method} {normalized_v1_path}",
                oai_type="not_found_error",
                oai_code="route_not_found",
                anth_type="not_found_error",
            )

        starting_reps = [r for r in all_reps if r.alive and not r.ready]
        raw_ready_reps = [r for r in all_reps if r.ready and r.url]
        if target.runtime == "ollama":
            compat_ready = [r for r in raw_ready_reps if ollama_is_gateway_compatible(r.env)]
            if raw_ready_reps and not compat_ready:
                raise GatewayError(
                    503,
                    "当前在线 Ollama 副本的 CT_OLLAMA_COMPAT 仅开启原生协议（未启用 openai/anthropic），已从 /v1/* 排除",
                    oai_type="service_unavailable_error",
                    oai_code="runtime_compat_disabled",
                )
            ready_reps = [
                r for r in compat_ready
                if runtime_allows_v1_route(target.runtime, spec, method, normalized_v1_path, env=r.env)
            ]
            if compat_ready and not ready_reps:
                raise GatewayError(
                    404,
                    f"当前在线 Ollama 副本均未开启对应兼容协议簇 ({normalized_v1_path})",
                    oai_type="not_found_error",
                    oai_code="route_not_found",
                    anth_type="not_found_error",
                )
        else:
            ready_reps = raw_ready_reps

        if not ready_reps:
            if starting_reps:
                raise GatewayError(
                    503,
                    f"运行时 {target.runtime} 实例正在启动中（STARTING），请执行 colabllm {target.runtime} status 查看进度",
                    oai_type="service_unavailable_error",
                    oai_code="runtime_starting",
                )
            raise GatewayError(
                503,
                f"运行时 {target.runtime} 当前没有可用的就绪实例",
                oai_type="service_unavailable_error",
                oai_code="runtime_unavailable",
            )

        if (
            target.runtime == "ollama"
            and normalized_v1_path == "/v1/responses"
            and isinstance(parsed_body, dict)
            and parsed_body.get("previous_response_id") is not None
            and len(ready_reps) > 1
        ):
            choices = ", ".join(f"{target.upstream_model}@ollama:{r.instance}" for r in ready_reps)
            raise GatewayError(
                400,
                f"ollama 存在多个兼容副本，携带 previous_response_id 时必须显式指定 :instance_id（如 {choices}）",
            )

        probed = self.ensure_probed_many(ready_reps)
        any_probe_ok = False
        matching: list[ReplicaRuntime] = []
        for rep in ready_reps:
            p = probed.get(rep.key)
            if p is not None and not p.error:
                any_probe_ok = True
                if target.upstream_model in p.models:
                    matching.append(rep)

        if not matching:
            if any_probe_ok:
                raise GatewayError(
                    404,
                    f"未在任何就绪的 {target.runtime} 副本上发现模型 {target.upstream_model!r}",
                    oai_type="not_found_error",
                    oai_code="model_not_found",
                    anth_type="not_found_error",
                )
            raise GatewayError(
                502,
                f"无法探测到 {target.runtime} 的模型清单（所有就绪副本均探测失败）",
                oai_type="api_error",
                oai_code="upstream_error",
                anth_type="api_error",
            )
        return matching

    def acquire_global_slot(self) -> None:
        if not self.global_semaphore.acquire(blocking=False):
            raise GatewayError(
                503,
                "网关当前总并发达到上限，请稍后重试",
                oai_type="service_unavailable_error",
                oai_code="server_overloaded",
                anth_type="overloaded_error",
            )
        with self._lock:
            self._inflight_global += 1

    def release_global_slot(self) -> None:
        with self._lock:
            self._inflight_global = max(0, self._inflight_global - 1)
        self.global_semaphore.release()

    def _sticky_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for inst_key in self._affinity_lru.values():
            counts[inst_key] = counts.get(inst_key, 0) + 1
        return counts

    def _select_free_overflow(self, reps: list[ReplicaRuntime]) -> ReplicaRuntime | None:
        sticky_counts = self._sticky_counts()
        candidates: list[tuple[float, int, ReplicaRuntime]] = []
        for r in reps:
            st = self._replica_states.setdefault(r.key, _ReplicaRuntimeState())
            if st.inflight < r.slot_capacity:
                load_ratio = st.inflight / max(1, r.slot_capacity)
                candidates.append((load_ratio, sticky_counts.get(r.key, 0), r))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1], item[2].instance))
        return candidates[0][2]

    def acquire_replica_slot(
        self,
        *,
        candidates: list[ReplicaRuntime],
        runtime: str,
        upstream_model: str,
        affinity_key: str,
        exclude_keys: set[str] | None = None,
        wait_deadline: float | None = None,
    ) -> tuple[ReplicaRuntime, float]:
        """Acquire a concurrency slot respecting cooldowns, LRU stickiness, grace wait, and queue bounds."""
        now = time.monotonic()
        total_deadline = wait_deadline if wait_deadline is not None else (now + self.slot_wait_timeout)
        excluded = exclude_keys or set()
        lru_lookup_key = (runtime, upstream_model, affinity_key) if affinity_key else None

        with self._slot_cond:
            eligible = []
            for r in candidates:
                if r.key in excluded:
                    continue
                st = self._replica_states.setdefault(r.key, _ReplicaRuntimeState())
                if st.cooldown_until <= now:
                    eligible.append(r)

            if not eligible:
                raise GatewayError(
                    502,
                    "所有含目标模型的健康副本均处于故障冷却期或已失败",
                    oai_type="api_error",
                    oai_code="upstream_error",
                    anth_type="api_error",
                )

            sticky_rep: ReplicaRuntime | None = None
            if lru_lookup_key is not None:
                sticky_key = self._affinity_lru.get(lru_lookup_key)
                if sticky_key is not None:
                    for r in eligible:
                        if r.key == sticky_key:
                            sticky_rep = r
                            break

            # Phase A: Try sticky replica immediately or within bounded affinity grace timeout.
            if sticky_rep is not None:
                st_sticky = self._replica_states.setdefault(sticky_rep.key, _ReplicaRuntimeState())
                grace_end = min(total_deadline, time.monotonic() + self.affinity_grace_timeout)
                while st_sticky.inflight >= sticky_rep.slot_capacity:
                    rem = grace_end - time.monotonic()
                    if rem <= 0:
                        break
                    self._slot_cond.wait(timeout=rem)
                if st_sticky.inflight < sticky_rep.slot_capacity:
                    st_sticky.inflight += 1
                    self._affinity_lru.move_to_end(lru_lookup_key)
                    return sticky_rep, total_deadline

            # Phase B: Pick any free eligible replica, or wait up to total_deadline across all eligible replicas.
            while True:
                chosen = self._select_free_overflow(eligible)
                if chosen is not None:
                    self._replica_states[chosen.key].inflight += 1
                    return chosen, total_deadline
                rem = total_deadline - time.monotonic()
                if rem <= 0:
                    raise GatewayError(
                        429,
                        "所有匹配副本的推理槽位已满，排队超时，请稍后重试",
                        oai_type="rate_limit_error",
                        oai_code="runtime_busy",
                        anth_type="rate_limit_error",
                        headers={"Retry-After": "5"},
                    )
                self._slot_cond.wait(timeout=rem)

    def release_replica_slot(self, rep_key: str) -> None:
        with self._slot_cond:
            st = self._replica_states.get(rep_key)
            if st is not None:
                st.inflight = max(0, st.inflight - 1)
            self._slot_cond.notify_all()

    def record_affinity_success(self, runtime: str, upstream_model: str, affinity_key: str, rep_key: str) -> None:
        if not affinity_key:
            return
        lru_key = (runtime, upstream_model, affinity_key)
        with self._lock:
            self._affinity_lru[lru_key] = rep_key
            self._affinity_lru.move_to_end(lru_key)
            while len(self._affinity_lru) > self.max_affinity_entries:
                self._affinity_lru.popitem(last=False)

    def status_snapshot(self) -> dict[str, Any]:
        """Build redacted real-time gateway status snapshot (stripping query credentials from URLs)."""
        reps = self.scan_replicas()
        now = time.monotonic()
        with self._lock:
            inflight_global = self._inflight_global
            state_copy = {
                k: (v.inflight, max(0.0, round(v.cooldown_until - now, 2)))
                for k, v in self._replica_states.items()
            }
            probes = {
                k: list(v.models) if not v.error else []
                for k, v in self._probe_cache.items()
            }
            lru_size = len(self._affinity_lru)

        items = []
        for rep in reps:
            inflight, cooldown_rem = state_copy.get(rep.key, (0, 0.0))
            compat_ok = True if rep.service != "ollama" else ollama_is_gateway_compatible(rep.env)
            items.append({
                "service": rep.service,
                "instance": rep.instance,
                "state": "READY" if rep.ready else ("STARTING" if rep.alive else "STOPPED"),
                "url": redact_upstream_url(rep.url),
                "transport": rep.transport,
                "compat_enabled": compat_ok,
                "slot_capacity": rep.slot_capacity,
                "inflight": inflight,
                "cooldown_remaining_s": cooldown_rem,
                "models": probes.get(rep.key, []),
            })
        return {
            "max_concurrent": self.max_concurrent,
            "inflight_global": inflight_global,
            "affinity_entries": lru_size,
            "instances": items,
        }
