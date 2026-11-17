"""Stateless shared runtime primitives across Colab and Kaggle providers (stdlib only)."""
from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Mapping

from cli_support import CliError, UsageError, command_timeout, diagnostic, safe_urlopen
from paths import private_dir, user_home, write_private

INSTANCE_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SECRET_ENV_KEYS = frozenset({
    "CT_TUNNEL_TOKEN",
    "CT_FRP_TOKEN",
    "CT_FRP_CA_CERT",
    "CT_AUTH_PASS",
    "CT_API_TOKEN",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
})

_SECRET_KV_RE = re.compile(
    r"\b("
    + "|".join(sorted(SECRET_ENV_KEYS))
    + r')\s*=\s*(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s]+)'
)
_SECRET_JSON_RE = re.compile(
    r'(["\'](?:' + "|".join(sorted(SECRET_ENV_KEYS)) + r')["\']\s*:\s*)(["\'])(?:\\.|[^\\])*?\2'
)
_BEARER_BASIC_HDR_RE = re.compile(r"(Authorization\s*:\s*(?:Bearer|Basic)\s+)[^\s,;\"']+", re.IGNORECASE)
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_HF_TOKEN_RE = re.compile(r"\bhf_[A-Za-z0-9]{8,}\b")
_KGAT_TOKEN_RE = re.compile(r"\bKGAT_[A-Za-z0-9_-]{6,}\b")
_KGRT_TOKEN_RE = re.compile(r"\bKGRT_[A-Za-z0-9._-]{6,}\b")
_KAGGLE_PROXY_SEG_RE = re.compile(r"/k/(\d+)/(?!\[REDACTED\])([^/?#\s\"']+)")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"


@dataclass(frozen=True)
class ProviderNamespace:
    provider: str                     # "colab" | "kaggle"
    state_subdir: str                 # "services" | "kaggle/services"
    tunnel_prefix: str                # "colab-" | "kaggle-"
    dns_owner_prefix: str             # "colabsvc" | "kagglesvc"
    frp_owner_prefix: str             # "colabsvc-frp" | "kagglesvc-frp"
    service_prefix: str               # "" | "kaggle-"
    hostname_prefix: str              # "" | "kaggle-"
    reserved_ports: frozenset[int]    # frozenset({8080, 8012}) | frozenset({8888})


COLAB_NAMESPACE = ProviderNamespace(
    provider="colab",
    state_subdir="services",
    tunnel_prefix="colab-",
    dns_owner_prefix="colabsvc",
    frp_owner_prefix="colabsvc-frp",
    service_prefix="",
    hostname_prefix="",
    reserved_ports=frozenset({8080, 8012}),
)

KAGGLE_NAMESPACE = ProviderNamespace(
    provider="kaggle",
    state_subdir="kaggle/services",
    tunnel_prefix="kaggle-",
    dns_owner_prefix="kagglesvc",
    frp_owner_prefix="kagglesvc-frp",
    service_prefix="kaggle-",
    hostname_prefix="kaggle-",
    reserved_ports=frozenset({8888}),
)


def resolve_state_root(state_root: Path | None = None) -> Path:
    return Path(state_root) if state_root is not None else user_home()


class LockedState:
    """Flock-synchronized atomic 0600 JSON state under `<state_root>/<namespace.state_subdir>/<session>`."""

    def __init__(
        self,
        session_name: str,
        namespace: ProviderNamespace = COLAB_NAMESPACE,
        *,
        state_root: Path | None = None,
    ) -> None:
        self.namespace = namespace
        self.root = resolve_state_root(state_root)
        self.dir = self.root / namespace.state_subdir / session_name
        self.path = self.dir / "state.json"

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            if not isinstance(data, dict):
                raise ValueError("expected an object")
            return data
        except (ValueError, UnicodeError) as error:
            raise RuntimeError(f"状态文件损坏：{self.path}；请修复该 JSON 文件或从备份恢复") from error

    @contextlib.contextmanager
    def lock(self, name: str = "state", blocking: bool = True):
        # Ensure root, intermediate dirs (e.g. `kaggle/` and `kaggle/services/`), and instance dir are 0700.
        private_dir(self.root)
        rel_parts = self.dir.relative_to(self.root).parts
        cur = self.root
        for part in rel_parts:
            cur = cur / part
            private_dir(cur)
        with (self.dir / f".{name}.lock").open("a", encoding="utf-8") as lockfile:
            os.chmod(lockfile.name, 0o600)
            try:
                fcntl.flock(lockfile, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise RuntimeError(f"{self.dir.name} 正在执行其他生命周期操作，请稍后重试") from None
            try:
                yield
            finally:
                fcntl.flock(lockfile, fcntl.LOCK_UN)

    def update(self, change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        with self.lock():
            st = change(self.load())
            fd, filename = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.dir)
            temporary = Path(filename)
            try:
                os.chmod(temporary, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(st, output, indent=2, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return st

    def save(self, **kw: Any) -> dict[str, Any]:
        return self.update(lambda st: {**st, **kw})

    @property
    def log_path(self) -> Path:
        return self.dir / "run.log"


def nonnegative_int(value: str | int) -> int:
    if isinstance(value, bool):
        raise ValueError("整数格式无效")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("整数格式无效") from None
    if parsed < 0:
        raise ValueError("数值不能小于 0")
    return parsed


def validate_instance(value: str) -> str:
    if not isinstance(value, str) or not INSTANCE_ID_RE.fullmatch(value):
        raise UsageError("实例 ID 须为 1–64 位字母、数字、下划线或连字符，且不能以连字符开头")
    return value


def extract_instance_arg(
    argv: list[str] | None = None,
    *,
    validator: Callable[[str], str] = validate_instance,
) -> tuple[str | None, list[str]]:
    """Extract `-i <id>` / `--instance <id>` from anywhere before `--`."""
    args = list(sys.argv[1:] if argv is None else argv)
    inst = os.environ.get("CT_INSTANCE") or None
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            cleaned.extend(args[i:])
            break
        if a in ("-i", "--instance"):
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                raise UsageError(f"{a} 需要有效的实例 ID")
            inst = validator(args[i + 1])
            i += 2
        elif a.startswith("--instance="):
            inst = validator(a.split("=", 1)[1])
            i += 1
        elif a.startswith("-i") and not a.startswith("--") and len(a) > 2:
            inst = validator(a[2:])
            i += 1
        else:
            cleaned.append(a)
            i += 1
    return validator(inst) if inst is not None else None, cleaned


def apply_instance(
    svc: Any,
    inst: str | None,
    *,
    validator: Callable[[str], str] = validate_instance,
) -> Any:
    """Apply an instance suffix (`-i 2` -> `<base>-2`, domain `<host>-2.<zone>`) to `svc` in place."""
    if inst is not None:
        validator(inst)
    base = svc.base_name or svc.name
    svc.base_name = base
    base_host = svc.base_default_hostname
    if base_host is None:
        base_host = svc.default_hostname
        svc.base_default_hostname = base_host
    svc.instance = inst
    if not inst:
        svc.name = base
        svc.default_hostname = base_host
        return svc
    suffix = f"-{inst}"
    svc.name = base if base.endswith(suffix) else f"{base}{suffix}"
    if base_host:
        if "." in base_host:
            sub, rest = base_host.split(".", 1)
            svc.default_hostname = f"{sub}{suffix}.{rest}" if not sub.endswith(suffix) else base_host
        else:
            svc.default_hostname = f"{base_host}{suffix}" if not base_host.endswith(suffix) else base_host
    return svc


def validate_domain(value: str) -> str:
    """Accept DNS hostnames, never URLs, wildcard patterns, ports or IPs."""
    try:
        host = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, AttributeError):
        raise ValueError("域名格式无效，请填写 example.com，不包含协议、端口或路径") from None
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
    ):
        raise ValueError("域名格式无效，请填写 example.com，不包含协议、端口或路径")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("请填写 DNS 域名，不能使用 IP 地址")


def load_tunnel_config(*, state_root: Path | None = None) -> dict[str, Any]:
    path = resolve_state_root(state_root) / "tunnel-config.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise RuntimeError(f"配置文件损坏：{path}；请修复该 JSON 文件或从备份恢复") from error
    if not isinstance(data, dict):
        raise ValueError("tunnel-config.json 必须是 JSON 对象")
    if data.get("base_domain"):
        data["base_domain"] = validate_domain(data["base_domain"])
    if data.get("frp") is not None:
        from frp_backend import validate_config

        data["frp"] = validate_config(data["frp"])
    return data


def save_tunnel_config(data: dict[str, Any], *, state_root: Path | None = None) -> None:
    path = resolve_state_root(state_root) / "tunnel-config.json"
    if data:
        write_private(path, data)
    else:
        path.unlink(missing_ok=True)


def tunnel_transport(args: Any, *, state_root: Path | None = None) -> str:
    explicit = getattr(args, "transport", None)
    if getattr(args, "quick_tunnel", False):
        if explicit == "frp":
            raise ValueError("--quick-tunnel cannot be used with --transport frp")
        return "cloudflare"
    if explicit:
        return explicit
    return "frp" if load_tunnel_config(state_root=state_root).get("frp", {}).get("enabled") else "cloudflare"


def tunnel_hostname(
    svc: Any,
    args: Any,
    *,
    namespace: ProviderNamespace = COLAB_NAMESPACE,
    state_root: Path | None = None,
) -> str | None:
    if getattr(args, "quick_tunnel", False):
        return None
    domain = getattr(args, "domain", None)
    if domain and domain not in (True, "@default"):
        return validate_domain(domain)
    base = (
        getattr(args, "base_domain", None)
        or os.environ.get("COLABSVC_BASE_DOMAIN")
        or load_tunnel_config(state_root=state_root).get("base_domain")
    )
    if not base:
        return None
    base = validate_domain(base)
    alias = svc.default_hostname or svc.name
    if "." in alias:
        alias = alias.split(".", 1)[0]
    if namespace.hostname_prefix and not alias.startswith(namespace.hostname_prefix):
        alias = f"{namespace.hostname_prefix}{alias}"
    return validate_domain(f"{alias}.{base}")


def cmd_tunnel_config(args: Any, *, state_root: Path | None = None) -> None:
    path = resolve_state_root(state_root) / "tunnel-config.json"
    saved = load_tunnel_config(state_root=state_root)
    if args.clear:
        if saved.get("frp", {}).get("enabled"):
            raise ValueError("FRP requires a base domain; disable FRP before clearing it")
        saved.pop("base_domain", None)
        save_tunnel_config(saved, state_root=state_root)
    elif args.base_domain is not None:
        saved["base_domain"] = validate_domain(args.base_domain)
        save_tunnel_config(saved, state_root=state_root)
    saved = load_tunnel_config(state_root=state_root)
    print(
        json.dumps(
            {
                "base_domain": saved.get("base_domain"),
                "transport": "frp" if saved.get("frp", {}).get("enabled") else "cloudflare",
                "frp": saved.get("frp"),
                "environment_override": os.environ.get("COLABSVC_BASE_DOMAIN") or None,
                "config_file": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cf_json(*args: str, run_fn: Callable[..., Any] | None = None) -> Any:
    if run_fn is not None:
        res = run_fn(["cf", *args], capture=True)
    else:
        res = subprocess.run(
            ["cf", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=command_timeout(),
        )
        if res.returncode != 0:
            err_tail = (res.stderr or res.stdout or "").strip()[-420:]
            raise CliError(f"cf {' '.join(args[:3])} 失败 ({res.returncode}): {err_tail}", res.returncode or 1)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        raise CliError(f"unexpected output from cf {' '.join(args[:3])}: {(res.stdout or '')[:300]}") from None


def find_zone(hostname: str, *, cf_json_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    caller = cf_json_fn or cf_json
    zones = caller("zones", "list")
    match = [z for z in zones if hostname == z["name"] or hostname.endswith("." + z["name"])]
    if not match:
        raise CliError(f"no Cloudflare zone in this account matches {hostname}")
    return max(match, key=lambda z: len(z["name"]))


def ensure_named_tunnel(
    svc: Any,
    hostname: str,
    *,
    namespace: ProviderNamespace = COLAB_NAMESPACE,
    state_root: Path | None = None,
    cf_json_fn: Callable[..., Any] | None = None,
    find_zone_fn: Callable[[str], dict[str, Any]] | None = None,
    run_fn: Callable[..., Any] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Idempotently create `<tunnel_prefix><svc>`, point `<hostname>` at it, return id + token."""
    caller = cf_json_fn or cf_json
    zone_finder = find_zone_fn or (lambda h: find_zone(h, cf_json_fn=caller))
    logger = log_fn or (lambda msg: diagnostic(f"[{namespace.dns_owner_prefix}] {msg}", "36"))
    runner = run_fn or (
        lambda cmd, **kw: subprocess.run(cmd, text=True, timeout=command_timeout(), **kw)
    )

    name = f"{namespace.tunnel_prefix}{svc.name}"
    found = [t for t in caller("tunnels", "list", "--name", name, "--is-deleted", "false") if t["name"] == name]
    if found:
        tunnel = found[0]
        logger(f"cf: reusing tunnel {name} ({tunnel['id']})")
    else:
        tunnel = caller("tunnels", "create", "--name", name, "--config-source", "cloudflare")
        logger(f"cf: created tunnel {name} ({tunnel['id']})")
    tid = tunnel["id"]

    ingress = [
        {"hostname": hostname, "service": f"http://localhost:{svc.tunnel_port}"},
        {"service": "http_status:404"},
    ]
    caller("tunnels", "config", "update", tid, "--body", json.dumps({"config": {"ingress": ingress}}))
    logger(f"cf: ingress {hostname} -> http://localhost:{svc.tunnel_port}")

    zone = zone_finder(hostname)
    target = f"{tid}.cfargotunnel.com"
    owner_tag = f"{namespace.dns_owner_prefix} {svc.name}"
    body = json.dumps({"type": "CNAME", "name": hostname, "content": target, "proxied": True, "comment": owner_tag})
    records = caller("dns", "records", "list", "-z", zone["id"], "--name", hostname)
    if len(records) > 1:
        raise ValueError(f"DNS conflict for {hostname}: multiple existing records")

    state_obj = LockedState(svc.name, namespace, state_root=state_root)
    saved_state = state_obj.load()
    saved_frp_owner = (saved_state.get("frp_route") or {}).get("dns_owner")

    if not records:
        caller("dns", "records", "create", "-z", zone["id"], "--body", body)
        logger(f"cf: DNS CNAME {hostname} -> {target} (created)")
    elif records[0].get("type") == "CNAME" and records[0].get("content") == target:
        existing_comment = records[0].get("comment") or ""
        if existing_comment and existing_comment != owner_tag and existing_comment != saved_frp_owner:
            raise CliError(f"{hostname} CNAME 归属注释冲突 ({existing_comment!r})；拒绝覆盖", 1)
        logger(f"cf: DNS CNAME {hostname} already points at the tunnel")
    elif records[0].get("comment") == owner_tag or (
        saved_frp_owner
        and records[0].get("comment") == saved_frp_owner
        and str(records[0].get("comment", "")).startswith(f"{namespace.frp_owner_prefix} ")
    ):
        caller("dns", "records", "edit", records[0]["id"], "-z", zone["id"], "--body", body)
        logger(f"cf: DNS CNAME {hostname} -> {target} (updated from {records[0].get('content')})")
    else:
        raise CliError(f"{hostname} already has a {records[0].get('type')} record; refusing to overwrite it", 1)

    prev_cf = saved_state.get("cloudflare_route") or {}
    old_host, old_zone = prev_cf.get("hostname"), prev_cf.get("zone_id")
    if old_host and old_zone and old_host != hostname:
        for old_rec in caller("dns", "records", "list", "-z", old_zone, "--name", old_host):
            if (
                old_rec.get("type") == "CNAME"
                and old_rec.get("content") == target
                and old_rec.get("comment") == owner_tag
            ):
                runner(["cf", "dns", "records", "delete", old_rec["id"], "-z", old_zone, "--force"], capture_output=True)
                logger(f"cf: removed previous DNS CNAME {old_host}")

    token = caller("tunnels", "token", "get", tid)
    return {"tunnel_id": tid, "tunnel_name": name, "zone_id": zone["id"], "hostname": hostname, "token": token}


def delete_named_tunnel(
    st: dict[str, Any],
    *,
    namespace: ProviderNamespace = COLAB_NAMESPACE,
    svc_name: str | None = None,
    cf_json_fn: Callable[..., Any] | None = None,
    run_fn: Callable[..., Any] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    caller = cf_json_fn or cf_json
    logger = log_fn or (lambda msg: diagnostic(f"[{namespace.dns_owner_prefix}] {msg}", "36"))
    runner = run_fn or (
        lambda cmd, **kw: subprocess.run(cmd, text=True, timeout=command_timeout(), check=kw.pop("check", True), **kw)
    )
    host, tid = st.get("hostname"), st.get("tunnel_id")
    expected_comment = f"{namespace.dns_owner_prefix} {svc_name}" if svc_name else None
    if host and tid and st.get("zone_id"):
        for r in caller("dns", "records", "list", "-z", st["zone_id"], "--name", host):
            if r.get("type") == "CNAME" and r.get("content", "").startswith(tid):
                comment = r.get("comment") or ""
                if expected_comment is not None:
                    if comment and comment != expected_comment:
                        continue
                elif comment and not comment.startswith(f"{namespace.dns_owner_prefix} "):
                    continue
                if run_fn is not None:
                    run_fn(["cf", "dns", "records", "delete", r["id"], "-z", st["zone_id"], "--force"], capture=True)
                else:
                    runner(
                        ["cf", "dns", "records", "delete", r["id"], "-z", st["zone_id"], "--force"],
                        capture_output=True,
                    )
                logger(f"cf: deleted DNS record {host}")
    if tid:
        if run_fn is not None:
            run_fn(["cf", "tunnels", "connections", "cleanup", tid, "--force"], capture=True, check=False)
            run_fn(["cf", "tunnels", "delete", tid, "--force"], capture=True)
        else:
            runner(["cf", "tunnels", "connections", "cleanup", tid, "--force"], capture_output=True, check=False)
            runner(["cf", "tunnels", "delete", tid, "--force"], capture_output=True)
        logger(f"cf: deleted tunnel {st.get('tunnel_name')}")


def probe(
    url: str,
    auth: tuple[str, str] | None = None,
    bearer: str | None = None,
    *,
    path: str = "",
    timeout: float = 15.0,
) -> str:
    """HTTP probe returning status code string (e.g. '200', '401') or 'error (<ExcName>)'."""
    target = url.strip()
    if path:
        target = target.rstrip("/") + "/" + path.lstrip("/")
    req = urllib.request.Request(target, headers={"User-Agent": _DEFAULT_UA})
    if auth and (auth[0] or auth[1]):
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    elif bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    try:
        with safe_urlopen(req, timeout=timeout) as r:
            return str(r.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception as e:
        return f"error ({type(e).__name__})"


class PrivateRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler ensuring active log and rolled backups stay mode 0600."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)

    def doRollover(self) -> None:
        super().doRollover()
        with contextlib.suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        for idx in range(1, self.backupCount + 1):
            rolled = f"{self.baseFilename}.{idx}"
            if os.path.exists(rolled):
                with contextlib.suppress(OSError):
                    os.chmod(rolled, 0o600)


def create_private_rotating_logger(
    log_path: Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
    logger_name: str,
) -> tuple[logging.Logger, PrivateRotatingFileHandler]:
    private_dir(log_path.parent)
    handler = PrivateRotatingFileHandler(
        str(log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for old in list(logger.handlers):
        logger.removeHandler(old)
        with contextlib.suppress(Exception):
            old.close()
    logger.addHandler(handler)
    return logger, handler


def _escape_env_value(value: Any) -> str:
    raw = "" if value is None else str(value)
    escaped = (
        raw.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def format_single_line_env_file(env_map: Mapping[str, Any]) -> str:
    """Format KEY=\"ESCAPED\" lines symmetrically with kaggle-ext session_ws._unquote_env_value."""
    lines: list[str] = []
    for key, val in env_map.items():
        if not isinstance(key, str) or not ENV_KEY_RE.fullmatch(key):
            raise ValueError(f"无效的环境变量名: {key!r}")
        lines.append(f"{key}={_escape_env_value(val)}")
    return ("\n".join(lines) + "\n") if lines else ""


def write_private_env_file(path: Path, env_map: Mapping[str, Any]) -> None:
    content = format_single_line_env_file(env_map)
    private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".env-", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def redact_runtime_secrets(text: str, known_secrets: Iterable[str] = ()) -> str:
    """Redact whitelisted secret keys, known plaintext secrets, and token prefixes without hiding CT_* markers."""
    if not text:
        return text
    out = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    out = _SECRET_JSON_RE.sub(lambda m: f'{m.group(1)}{m.group(2)}[REDACTED]{m.group(2)}', out)
    out = _BEARER_BASIC_HDR_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", out)
    out = _SK_TOKEN_RE.sub("[REDACTED]", out)
    out = _HF_TOKEN_RE.sub("[REDACTED]", out)
    out = _KGAT_TOKEN_RE.sub("[REDACTED]", out)
    out = _KGRT_TOKEN_RE.sub("[REDACTED]", out)
    out = _KAGGLE_PROXY_SEG_RE.sub(r"/k/\1/[REDACTED]", out)
    literals = sorted(
        {str(s) for s in known_secrets if isinstance(s, str) and len(s) >= 4},
        key=len,
        reverse=True,
    )
    for literal in literals:
        if literal in out:
            out = out.replace(literal, "[REDACTED]")
    return out


class LineBufferedSecretRedactor:
    """Line-buffered chunk redactor with 64 KiB overflow cap."""

    def __init__(self, known_secrets: Iterable[str] = (), *, max_buffer: int = 65536) -> None:
        self.known_secrets = tuple(known_secrets)
        self.max_buffer = max(1024, int(max_buffer))
        self._buf = ""

    def feed(self, text: str, *, final: bool = False) -> str:
        if text:
            self._buf += _ANSI_RE.sub("", text)
        out_parts: list[str] = []
        while "\n" in self._buf:
            line, rest = self._buf.split("\n", 1)
            self._buf = rest
            out_parts.append(redact_runtime_secrets(line, self.known_secrets) + "\n")
        if len(self._buf) > self.max_buffer:
            out_parts.append(redact_runtime_secrets(self._buf, self.known_secrets))
            self._buf = ""
        if final and self._buf:
            out_parts.append(redact_runtime_secrets(self._buf, self.known_secrets))
            self._buf = ""
        return "".join(out_parts)
