"""kagglesvc -- Kaggle runtime runner for `colabllm kaggle <short_name> <subcmd>`."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import ipaddress
import json
import logging
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from cli_support import (
    CliError,
    UsageError,
    command_timeout,
    diagnostic,
    duration_seconds,
    nonnegative_seconds,
    positive_seconds,
)
from frp_backend import (
    ensure_route as frp_ensure_route,
    remove_route as frp_remove_route,
    validate_config as frp_validate_config,
)
from instance_discovery import KAGGLE_INSTANCE_ID_RE, KAGGLE_SESSION_RE, list_raw_instances
from kaggle_accounts import (
    kaggle_subprocess_env,
    load_kaggle_accounts_registry,
    resolve_effective_kaggle_account,
    validate_kaggle_account,
)
import kaggle_ws
import mux
from paths import private_dir, user_home
from processes import owned_process, process_identity, runner_alive, stop_owned_process
from protocol import NotebookProtocol
import runtime_core as _core
from runtime_core import (
    KAGGLE_NAMESPACE,
    LineBufferedSecretRedactor,
    LockedState,
    SECRET_ENV_KEYS,
    create_private_rotating_logger,
    delete_named_tunnel,
    ensure_named_tunnel,
    nonnegative_int,
    probe,
    tunnel_hostname,
    tunnel_transport,
    validate_domain,
    write_private_env_file,
)

_INITIAL_STATE_ROOT = user_home()
STATE_ROOT = _INITIAL_STATE_ROOT

DEFAULT_READY_TIMEOUT = 600
DEFAULT_STARTUP_TIMEOUT = 600.0
DEFAULT_RESTART_DELAY = 5.0
MAX_RESTART_BACKOFF = 300.0
DEFAULT_IDLE_CHECK_SECONDS = 300.0
DEFAULT_FAST_PROBE_SECONDS = 15.0
DEFAULT_MAX_RESTARTS = 3
DEFAULT_MAX_UPTIME_SECONDS = 21600  # 6h
QUOTA_MIN_REMAINING_HOURS = 0.15

_ALL_SECRET_KEYS = ("auth_user", "auth_pass", "api_token", "login_user", "login_pass")
_EPHEMERAL_SESSION_KEYS = (
    "kernel_session_id",
    "ready",
    "ever_ready",
    "runner_pid",
    "runner_identity",
    "runner_mode",
    "runner_lock",
    "exec_backend",
    "exec_pid",
    "exec_identity",
    "restarts",
    "restart_count",
    "session_recreate_count",
    "started",
    "started_mono",
    "last_live_state",
    "last_stage",
    "last_cell",
    "runner_exit",
)

_ACCELERATOR_ALIASES = {
    "": "cpu",
    "cpu": "cpu",
    "none": "cpu",
    "nvidiateslat4": "NvidiaTeslaT4",
    "gpu": "NvidiaTeslaT4",
    "t4": "NvidiaTeslaT4",
    "t4x2": "NvidiaTeslaT4",
    "nvidiateslat4highmem": "NvidiaTeslaT4Highmem",
    "t4-highmem": "NvidiaTeslaT4Highmem",
    "t4highmem": "NvidiaTeslaT4Highmem",
    "tpuv5e8": "TpuV5E8",
    "tpu": "TpuV5E8",
    "tpu-v5e-8": "TpuV5E8",
}


def _state_root() -> Path:
    return user_home() if STATE_ROOT == _INITIAL_STATE_ROOT else Path(STATE_ROOT)


@dataclasses.dataclass
class KaggleService:
    name: str
    notebook: str
    service_port: int
    auth: str = "caddy"
    public_port: int = 18080
    accelerator: str = "cpu"
    enable_internet: bool = True
    default_hostname: str | None = None
    ready_timeout: int = DEFAULT_READY_TIMEOUT
    max_restarts: int = DEFAULT_MAX_RESTARTS
    max_uptime: int = DEFAULT_MAX_UPTIME_SECONDS
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    has_web_ui: bool = True
    base_name: str = ""
    base_default_hostname: str | None = None
    instance: str | None = None
    zone: str | None = None
    account: str | None = None
    auth_user: str = "colabllm"

    def __post_init__(self) -> None:
        if not self.base_name:
            self.base_name = self.name
        if self.base_default_hostname is None:
            self.base_default_hostname = self.default_hostname

    @property
    def tunnel_port(self) -> int:
        return self.public_port if self.auth == "caddy" else self.service_port


def log(msg: str) -> None:
    diagnostic(f"[kagglesvc] {msg}", "36")


def warn(msg: str) -> None:
    diagnostic(f"[kagglesvc] {msg}", "33")


def die(msg: str, code: int = 1) -> None:
    raise CliError(msg, code)


def validate_kaggle_instance(value: str) -> str:
    if not isinstance(value, str) or not KAGGLE_INSTANCE_ID_RE.fullmatch(value):
        raise UsageError("Kaggle 实例 ID 须仅包含字母、数字和连字符 '-'，且不能以 '-' 开头或结尾")
    return value


def validate_kaggle_session_name(name: str) -> str:
    if not isinstance(name, str) or not KAGGLE_SESSION_RE.fullmatch(name):
        raise UsageError(
            f"Kaggle 会话名称 {name!r} 无效或超过 48 个字符限制（仅支持字母、数字与 '-'，最多 48 位）"
        )
    return name


def normalize_kaggle_accelerator(raw: str | None) -> str:
    key = (raw or "").strip().lower()
    if key in _ACCELERATOR_ALIASES:
        return _ACCELERATOR_ALIASES[key]
    raise UsageError(
        f"不支持的 Kaggle 加速器 {raw!r}；可选值: cpu, NvidiaTeslaT4 (t4/gpu), NvidiaTeslaT4Highmem (t4-highmem), TpuV5E8 (tpu)"
    )


def extract_account_arg(argv: list[str] | None = None) -> tuple[str | None, list[str]]:
    args = list(sys.argv[1:] if argv is None else argv)
    acct: str | None = None
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            cleaned.extend(args[i:])
            break
        if a == "--account":
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                raise UsageError("--account 需要有效的 Kaggle 账户标识")
            acct = validate_kaggle_account(args[i + 1])
            i += 2
        elif a.startswith("--account="):
            acct = validate_kaggle_account(a.split("=", 1)[1])
            i += 1
        else:
            cleaned.append(a)
            i += 1
    return acct, cleaned


def extract_instance_arg(argv: list[str] | None = None) -> tuple[str | None, list[str]]:
    return _core.extract_instance_arg(argv, validator=validate_kaggle_instance)


def apply_instance(svc: KaggleService, inst: str | None) -> KaggleService:
    _core.apply_instance(svc, inst, validator=validate_kaggle_instance)
    validate_kaggle_session_name(svc.name)
    return svc


def _state_for(svc: KaggleService) -> LockedState:
    return LockedState(svc.name, KAGGLE_NAMESPACE, state_root=_state_root())


def _runtime_env_path(state: LockedState) -> Path:
    return state.dir / "runtime.env"


def _clean_notebook_path(state: LockedState, src_name: str) -> Path:
    return state.dir / f"clean-{src_name}"


def _kaggle_cmd(args: list[str], *, account: str | None = None) -> list[str]:
    if account:
        return ["kaggle", "-a", account, *args]
    return ["kaggle", *args]


def _run_kaggle(
    args: list[str],
    *,
    account: str | None = None,
    timeout: float | None = None,
    check: bool = True,
    base_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = _kaggle_cmd(args, account=account)
    env = kaggle_subprocess_env(account, base_env=base_env)
    try:
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=command_timeout() if timeout is None else timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as err:
        raise CliError(f"kaggle 命令超时: {' '.join(cmd[:4])}", 124) from err
    except OSError as err:
        raise CliError(f"无法执行 kaggle CLI: {err}", 1) from err
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        hint = detail[-1][:300] if detail else f"exit={res.returncode}"
        raise CliError(f"kaggle 命令失败 ({res.returncode}): {' '.join(cmd[:4])} — {hint}", res.returncode or 1)
    return res


def _run_kaggle_json(
    args: list[str],
    *,
    account: str | None = None,
    timeout: float | None = None,
    check: bool = True,
    base_env: Mapping[str, str] | None = None,
) -> Any:
    res = _run_kaggle(args, account=account, timeout=timeout, check=check, base_env=base_env)
    raw = (res.stdout or "").strip()
    if not raw:
        raise CliError(f"kaggle {' '.join(args[:3])} 未返回 JSON 输出", res.returncode or 1)
    try:
        return json.loads(raw)
    except ValueError as err:
        raise CliError(f"kaggle {' '.join(args[:3])} 返回了无效 JSON: {raw[:200]}") from err


def _check_reserved_ports(svc: KaggleService) -> None:
    reserved = KAGGLE_NAMESPACE.reserved_ports
    if svc.service_port in reserved:
        raise UsageError(f"Kaggle 保留端口冲突: service_port={svc.service_port} 在 {sorted(reserved)} 中")
    if svc.auth == "caddy":
        if svc.public_port in reserved:
            raise UsageError(f"Kaggle 保留端口冲突: public_port={svc.public_port} 在 {sorted(reserved)} 中")
        if svc.service_port == svc.public_port:
            raise UsageError(f"service_port ({svc.service_port}) 与 public_port ({svc.public_port}) 不能相同")


def _select_account_quota_record(records: list[dict[str, Any]], account: str | None) -> dict[str, Any] | None:
    if account:
        for rec in records:
            if isinstance(rec, dict) and (
                rec.get("account") == account
                or str(rec.get("username") or "").lstrip("@").lower() == account.lstrip("@").lower()
            ):
                return rec
    for rec in records:
        if isinstance(rec, dict) and rec.get("active") is True:
            return rec
    return records[0] if (len(records) == 1 and isinstance(records[0], dict)) else None


def _check_non_cpu_quota(
    accelerator: str,
    account: str | None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fail-closed quota gate before allocating tunnels, DNS, or Kaggle sessions."""
    if accelerator == "cpu":
        return {}
    records = _run_kaggle_json(
        ["account", "status", "--json"],
        account=None,
        timeout=command_timeout(60),
        base_env=base_env,
    )
    if not isinstance(records, list) or not records:
        raise CliError("未检测到已配置的 Kaggle 账户状态；请先执行 kaggle account login 或运行 colabllm doctor --provider kaggle")
    rec = _select_account_quota_record(records, account)
    if rec is None:
        raise CliError(f"在 kaggle account status 中未找到目标账户 {account!r}")
    if rec.get("authenticated") is not True:
        err_msg = rec.get("error") or "未完成认证"
        raise CliError(f"Kaggle 账户 {rec.get('account') or account!r} 未通过认证 ({err_msg})；拒绝启动 {accelerator} 会话")
    quota_key = "tpu_quota" if accelerator == "TpuV5E8" else "gpu_quota"
    quota = rec.get(quota_key)
    if not isinstance(quota, dict) or quota.get("remaining_hours") is None:
        raise CliError(
            f"无法获取账户 {rec.get('account') or account!r} 的 {quota_key} 剩余配额（fail-closed）；拒绝创建 {accelerator} 会话"
        )
    try:
        rem = float(quota["remaining_hours"])
    except (TypeError, ValueError):
        raise CliError(f"账户 {rec.get('account') or account!r} 的 {quota_key} 配额字段无效") from None
    if rem <= QUOTA_MIN_REMAINING_HOURS:
        raise CliError(
            f"账户 {rec.get('account') or account!r} 的 {quota_key} 剩余额度仅 {rem:.2f}h "
            f"(<= {QUOTA_MIN_REMAINING_HOURS:.2f}h 安全余量)，已拦截 {accelerator} 启动以防中途被强停"
        )
    return rec


def _query_session_status(
    session_name: str,
    account: str | None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run `kaggle session status <name> --json`; map parse/unknown errors to 'unreachable'."""
    try:
        res = _run_kaggle(
            ["session", "status", session_name, "--json"],
            account=account,
            timeout=30.0,
            check=False,
            base_env=base_env,
        )
    except CliError as err:
        return "unreachable", {"probe_error": str(err)}
    raw = (res.stdout or "").strip()
    if not raw:
        return "unreachable", {"probe_error": (res.stderr or f"exit={res.returncode}").strip()}
    try:
        data = json.loads(raw)
    except ValueError:
        return "unreachable", {"probe_error": raw[:200]}
    if not isinstance(data, dict):
        return "unreachable", {"probe_error": "invalid status payload"}
    live = str(data.get("live_state") or "").strip().lower()
    if live in ("running", "reclaimed", "unreachable", "missing_credentials"):
        return live, data
    if data.get("alive") is True:
        return "running", data
    return "unreachable", data


def _stop_remote_session(
    session_name: str,
    account: str | None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    try:
        res = _run_kaggle(
            ["session", "stop", session_name, "--json"],
            account=account,
            timeout=60.0,
            check=False,
            base_env=base_env,
        )
    except CliError as err:
        return False, str(err)
    if res.returncode == 0:
        return True, ""
    detail = (res.stderr or res.stdout or "").strip() or f"exit={res.returncode}"
    return False, detail


def _clear_ephemeral_session_state(state: LockedState, *, clear_quick_url: bool = True) -> dict[str, Any]:
    def _mutate(st: dict[str, Any]) -> dict[str, Any]:
        cleaned = {k: v for k, v in st.items() if k not in _EPHEMERAL_SESSION_KEYS}
        if clear_quick_url and not (st.get("cloudflare_route") or st.get("frp_route")):
            cleaned.pop("url", None)
        return cleaned

    return state.update(_mutate)


def _strip_secret_env(env_map: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env_map.items() if k not in SECRET_ENV_KEYS}


def _strip_nb_for_exec(src_nb: Path, dst_nb: Path) -> None:
    nb = json.loads(src_nb.read_text(encoding="utf-8"))
    cells = []
    for idx, c in enumerate(nb.get("cells", [])):
        if c.get("cell_type") != "code":
            cells.append(c)
            continue
        cells.append(dict(c, id=c.get("id") or f"kcell-{idx}", outputs=[], execution_count=None))
    clean_nb = dict(nb, cells=cells, nbformat=4, nbformat_minor=5)
    private_dir(dst_nb.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".clean-nb-", suffix=".tmp", dir=dst_nb.parent)
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(clean_nb, handle, indent=1, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(dst_nb)
    finally:
        tmp_path.unlink(missing_ok=True)


def _service_secret_keys(svc: KaggleService) -> tuple[str, ...]:
    if svc.auth == "caddy":
        return _ALL_SECRET_KEYS
    return ("api_token", "login_user", "login_pass")


def _probe_service_endpoint(
    svc: KaggleService, st: dict[str, Any], url_override: str | None = None, *, timeout: float = 10.0
) -> str:
    url = (url_override or st.get("url") or "").strip()
    if not url:
        return "missing-url"
    if svc.auth == "caddy":
        return probe(url, (st.get("auth_user", ""), st.get("auth_pass", "")), path="/health", timeout=timeout)
    if svc.auth == "service" and st.get("api_token"):
        return probe(url, bearer=st["api_token"], path="/health", timeout=timeout)
    return probe(url, path="/health", timeout=timeout)


def _known_kaggle_short_names() -> set[str]:
    from instance_discovery import discover_kaggle_service_pairs

    return set(discover_kaggle_service_pairs(Path(__file__).resolve().parents[1] / "kaggle_services"))


def list_service_instances(base_name: str) -> list[dict[str, Any]]:
    raw_items = list_raw_instances(
        _state_root() / KAGGLE_NAMESPACE.state_subdir,
        _known_kaggle_short_names(),
        target_service=base_name,
        alive_fn=runner_alive,
        on_corrupt="skip",
    )
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        st = raw["state"]
        alive = raw["alive"]
        items.append({
            "provider": "kaggle",
            "instance": raw["instance"],
            "instance_arg": raw["instance_arg"],
            "name": raw["name"],
            "session_name": f"kaggle-{raw['name']}",
            "kaggle_session": st.get("kaggle_session") or raw["name"],
            "kernel_session_id": st.get("kernel_session_id"),
            "accelerator": st.get("accelerator"),
            "alive": alive,
            "running": alive,
            "ready": raw["ready"],
            "ever_ready": bool(st.get("ever_ready")),
            "auto_restart": bool(st.get("auto_restart")),
            "restarts": int(st.get("restarts") or st.get("restart_count") or 0),
            "runner_pid": st.get("runner_pid"),
            "runner_mode": st.get("runner_mode"),
            "exec_backend": st.get("exec_backend"),
            "url": st.get("url"),
            "account": st.get("account"),
            "api_token": st.get("api_token"),
            "auth_user": st.get("auth_user"),
            "auth_pass": st.get("auth_pass"),
            "login_user": st.get("login_user"),
            "login_pass": st.get("login_pass"),
            "started": st.get("started"),
            "last_live_state": st.get("last_live_state"),
        })
    return items


def show_info(
    svc: KaggleService,
    *,
    do_probe: bool = False,
    reveal: bool = False,
    output_json: bool = False,
    live_state: str | None = None,
) -> bool:
    st = _state_for(svc).load()
    alive = runner_alive(st)
    ready = bool(alive and st.get("ready") and live_state != "reclaimed")
    acct = svc.account or st.get("account")
    data: dict[str, Any] = {
        "provider": "kaggle",
        "service": svc.name,
        "catalog_service": f"kaggle-{svc.base_name}",
        "kaggle_session": st.get("kaggle_session") or svc.name,
        "kernel_session_id": st.get("kernel_session_id"),
        "accelerator": st.get("accelerator") or svc.accelerator,
        "running": alive,
        "ready": ready,
        "url": st.get("url"),
    }
    if st.get("runner_mode"):
        data["runner_mode"] = st["runner_mode"]
    if st.get("exec_backend"):
        data["exec_backend"] = st["exec_backend"]
    if acct:
        data["account"] = acct
    if live_state:
        data["live_state"] = live_state
    if st.get("auto_restart"):
        data["auto_restart"] = True
        data["restarts"] = int(st.get("restarts") or 0)
        data["max_restarts"] = st.get("max_restarts")
    if reveal:
        for key in _service_secret_keys(svc):
            if st.get(key):
                data[key] = st[key]
    healthy = ready
    if do_probe and st.get("url"):
        result = _probe_service_endpoint(svc, st)
        data["http_status"] = result
        healthy = bool(ready and result.isdigit() and 200 <= int(result) < 400)
    elif do_probe:
        healthy = False
    if output_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"service : kaggle/{svc.name} ({'alive' if alive else 'stopped'})")
        if acct:
            print(f"account : {acct}")
        print(f"accel   : {data['accelerator']}")
        if data.get("kernel_session_id"):
            print(f"kernel  : {data['kernel_session_id']}")
        if live_state:
            print(f"live    : {live_state}")
        print(f"ready   : {ready}")
        if st.get("url"):
            print(f"URL     : {st['url']}")
        if do_probe and "http_status" in data:
            print(f"probe   : {data['http_status']}")
        if reveal:
            for key in _service_secret_keys(svc):
                if st.get(key):
                    print(f"{key}: {st[key]}")
    return healthy


def show_instances(svc: KaggleService, *, reveal: bool = False, output_json: bool = False) -> None:
    items = list_service_instances(svc.base_name or svc.name)
    hidden = set(_ALL_SECRET_KEYS) if not reveal else (set(_ALL_SECRET_KEYS) - set(_service_secret_keys(svc)))
    if hidden:
        items = [{k: v for k, v in it.items() if k not in hidden} for it in items]
    if output_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        log(f"no Kaggle instances found for '{svc.base_name or svc.name}'")
        return
    print(f"{'INSTANCE':<12} {'ACCOUNT':<16} {'SESSION':<28} {'ACCEL':<14} {'STATE':<10} URL")
    for it in items:
        label = "READY" if it["ready"] else ("STARTING" if it["alive"] else "STOPPED")
        print(
            f"{it['instance']:<12} {str(it.get('account') or '-'):<16} "
            f"{it['kaggle_session']:<28} {str(it.get('accelerator') or '-'):<14} "
            f"{label:<10} {it.get('url') or '-'}"
        )


def _known_secrets_from_state(st: dict[str, Any], runtime_env_path: Path) -> tuple[str, ...]:
    secrets_list: list[str] = []
    for k in ("auth_pass", "api_token", "login_pass"):
        v = st.get(k)
        if isinstance(v, str) and len(v) >= 4:
            secrets_list.append(v)
    if runtime_env_path.is_file():
        with contextlib.suppress(OSError, UnicodeError):
            for line in runtime_env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line:
                    k, _, val = line.partition("=")
                    if k.strip() in SECRET_ENV_KEYS:
                        unq = val.strip().strip('"')
                        if len(unq) >= 4:
                            secrets_list.append(unq)
    return tuple(secrets_list)


def _terminate_child(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(OSError):
                proc.send_signal(sig)
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


def _finalize_unready_stop(
    svc: KaggleService,
    state: LockedState,
    acct: str | None,
    *,
    reason: str,
    emit: Callable[[str], None] | None = None,
    base_env: Mapping[str, str] | None = None,
) -> None:
    """Stop remote Kaggle session when aborting before first CT_READY (all launch modes)."""
    st = state.load()
    if st.get("ever_ready"):
        return
    session_name = st.get("kaggle_session") or svc.name
    ksid = st.get("kernel_session_id")
    if ksid is not None or state.path.is_file():
        stopped, err = _stop_remote_session(session_name, acct, base_env=base_env)
        if stopped:
            _clear_ephemeral_session_state(state)
            _runtime_env_path(state).unlink(missing_ok=True)
            _clean_notebook_path(state, Path(svc.notebook).name).unlink(missing_ok=True)
            if emit:
                emit(f"[kagglesvc] {reason}; stopped unready session {session_name} and cleaned runtime.env\n")
        else:
            msg = (
                f"[kagglesvc] ORPHAN_SUSPECTED kernel_session_id={ksid} session={session_name} "
                f"account={acct or '-'}: failed to stop unready session ({err}); state.json preserved\n"
            )
            if emit:
                emit(msg)
            warn(msg.strip())


def finish_runner(
    state: LockedState,
    pid: int,
    identity: str | None,
    exit_code: int | None = None,
) -> None:
    """Clear runner ownership fields atomically if `(pid, identity)` still owns the state."""

    def _finish(st: dict[str, Any]) -> dict[str, Any]:
        if st.get("runner_pid") == pid and st.get("runner_identity") == identity:
            st.update(
                runner_pid=None,
                runner_identity=None,
                runner_mode=None,
                runner_lock=None,
                exec_pid=None,
                exec_identity=None,
                ready=False,
                runner_exit=exit_code,
            )
        return st

    state.update(_finish)


def _runner_still_owned(
    state: LockedState, pid: int, identity: str | None
) -> tuple[bool, dict[str, Any]]:
    st = state.load()
    return bool(identity and st.get("runner_pid") == pid and st.get("runner_identity") == identity), st


async def supervise_instance_async(
    svc: KaggleService,
    nb_path: Path,
    *,
    owner_pid: int | None = None,
    owner_identity: str | None = None,
    output_sink: Callable[[bytes], None] | None = None,
    env_overrides: dict[str, str] | None = None,
) -> int:
    """Asyncio native WS supervisor for Kaggle with 4-branch auto-restart and --max-uptime."""
    state = _state_for(svc)
    st = state.load()
    pid = owner_pid if owner_pid is not None else os.getpid()
    identity = owner_identity if owner_identity is not None else process_identity(pid)
    acct = svc.account or st.get("account")
    merged_env = {**os.environ, **(env_overrides or {})}

    accelerator = st.get("accelerator") or svc.accelerator
    enable_internet = bool(st.get("enable_internet", True))
    auto_restart = bool(st.get("auto_restart", False))
    max_restarts = int(st.get("max_restarts") if st.get("max_restarts") is not None else svc.max_restarts)
    max_uptime = st.get("max_uptime")
    if max_uptime is None and accelerator != "cpu":
        max_uptime = DEFAULT_MAX_UPTIME_SECONDS
    restart_delay = float(st.get("restart_delay") or DEFAULT_RESTART_DELAY)

    saved_idle = float(st.get("idle_check_seconds") or DEFAULT_IDLE_CHECK_SECONDS)
    try:
        idle_check = float(merged_env.get("COLABLLM_IDLE_CHECK_SECONDS") or saved_idle)
    except ValueError:
        idle_check = saved_idle
    try:
        probe_interval = float(merged_env.get("COLABLLM_FAST_PROBE_SECONDS") or DEFAULT_FAST_PROBE_SECONDS)
    except ValueError:
        probe_interval = DEFAULT_FAST_PROBE_SECONDS

    runtime_env = _runtime_env_path(state)
    is_foreground = st.get("runner_mode") == "foreground"
    logger = None
    log_handler = None
    if output_sink is None:
        logger, log_handler = create_private_rotating_logger(
            state.log_path,
            max_bytes=10 * 1024 * 1024,
            backup_count=3,
            logger_name=f"colabllm.kagglesvc.{svc.name}.{pid}",
        )

    redactor = LineBufferedSecretRedactor(
        known_secrets=_known_secrets_from_state(st, runtime_env),
        max_buffer=65536,
    )

    def _emit_safe(safe_text: str) -> None:
        if not safe_text:
            return
        if output_sink is not None:
            output_sink(safe_text.encode("utf-8", errors="replace"))
        else:
            assert logger is not None and log_handler is not None
            for line in safe_text.splitlines():
                logger.info(line)
            log_handler.flush()
            if is_foreground:
                sys.stdout.write(safe_text)
                sys.stdout.flush()

    def _emit(text: str) -> None:
        if text:
            _emit_safe(redactor.feed(text))

    start_mono = float(st.get("started_mono") or time.monotonic())
    restarts = int(st.get("restarts") or 0)
    recreate_count = int(st.get("session_recreate_count") or 0)
    unreachable_streak = 0

    def _uptime_exceeded() -> bool:
        return bool(accelerator != "cpu" and max_uptime and (time.monotonic() - start_mono) >= float(max_uptime))

    async def _probe_async(target_svc: KaggleService, curr_st: dict[str, Any], timeout: float) -> str:
        return await asyncio.to_thread(_probe_service_endpoint, target_svc, curr_st, timeout=timeout)

    async def _refresh_status_async() -> None:
        await asyncio.to_thread(_query_session_status, svc.name, acct, base_env=merged_env)

    async def _handle_max_uptime() -> int:
        _emit(f"[kagglesvc] reached host --max-uptime ({max_uptime}s); stopping session {svc.name}\n")
        await asyncio.to_thread(_stop_remote_session, svc.name, acct, base_env=merged_env)
        await asyncio.to_thread(_clear_ephemeral_session_state, state)
        runtime_env.unlink(missing_ok=True)
        nb_path.unlink(missing_ok=True)
        return 0

    try:
        next_mode = "exec"
        conn_seq = 0
        attempt = 0

        if not auto_restart:
            if _uptime_exceeded():
                return await _handle_max_uptime()
            if not runtime_env.is_file():
                _emit(f"[kagglesvc] missing runtime.env at {runtime_env}\n")
                await asyncio.to_thread(
                    _finalize_unready_stop,
                    svc,
                    state,
                    acct,
                    reason="missing runtime.env before CT_READY",
                    emit=_emit,
                    base_env=merged_env,
                )
                return 1
            env_vars = await asyncio.to_thread(_read_single_line_env_file, runtime_env)
            redactor.known_secrets = _known_secrets_from_state(state.load(), runtime_env)
            ready_deadline = (
                time.monotonic() + float(st.get("ready_timeout") or svc.ready_timeout)
                if (is_foreground and not st.get("ready"))
                else None
            )
            conn_seq += 1
            rc, reason = await kaggle_ws.run_single_kaggle_ws_session(
                svc,
                state,
                nb_path,
                env_vars,
                _emit,
                env=merged_env,
                deadline=ready_deadline,
                auto_restart=False,
                idle_check_seconds=0.0,
                probe_interval=0.0,
                probe_fn=_probe_async,
                uptime_exceeded_fn=_uptime_exceeded,
                refresh_status_fn=_refresh_status_async,
                conn_seq=conn_seq,
            )
            if reason == "max_uptime" or _uptime_exceeded():
                return await _handle_max_uptime()
            owned, st_now = await asyncio.to_thread(_runner_still_owned, state, pid, identity)
            if not owned:
                return 0
            if (rc or 0) != 0 or not st_now.get("ever_ready"):
                exit_code = rc if (rc and rc > 0) else 1
                if not st_now.get("ever_ready"):
                    await asyncio.to_thread(
                        _finalize_unready_stop,
                        svc,
                        state,
                        acct,
                        reason=f"ws exited ({reason}, code={exit_code}) before CT_READY",
                        emit=_emit,
                        base_env=merged_env,
                    )
                return exit_code
            return 0

        while True:
            owned, st_loop = await asyncio.to_thread(_runner_still_owned, state, pid, identity)
            if not owned:
                return 0
            if _uptime_exceeded():
                return await _handle_max_uptime()

            conn_seq += 1
            if next_mode == "watch":
                rc, reason = await kaggle_ws.watch_kaggle_ws_iopub(
                    svc,
                    state,
                    _emit,
                    env=merged_env,
                    idle_check_seconds=idle_check,
                    probe_interval=probe_interval,
                    probe_fn=_probe_async,
                    uptime_exceeded_fn=_uptime_exceeded,
                    refresh_status_fn=_refresh_status_async,
                    conn_seq=conn_seq,
                )
            else:
                if not runtime_env.is_file():
                    _emit(f"[kagglesvc] missing runtime.env at {runtime_env}\n")
                    if not st_loop.get("ever_ready"):
                        await asyncio.to_thread(
                            _finalize_unready_stop,
                            svc,
                            state,
                            acct,
                            reason="missing runtime.env before CT_READY",
                            emit=_emit,
                            base_env=merged_env,
                        )
                    return 1
                env_vars = await asyncio.to_thread(_read_single_line_env_file, runtime_env)
                redactor.known_secrets = _known_secrets_from_state(st_loop, runtime_env)
                ready_deadline = (
                    time.monotonic() + float(st_loop.get("ready_timeout") or svc.ready_timeout)
                    if (is_foreground or attempt > 0)
                    else None
                )
                rc, reason = await kaggle_ws.run_single_kaggle_ws_session(
                    svc,
                    state,
                    nb_path,
                    env_vars,
                    _emit,
                    env=merged_env,
                    deadline=ready_deadline,
                    auto_restart=True,
                    idle_check_seconds=idle_check,
                    probe_interval=probe_interval,
                    probe_fn=_probe_async,
                    uptime_exceeded_fn=_uptime_exceeded,
                    refresh_status_fn=_refresh_status_async,
                    conn_seq=conn_seq,
                )

            owned, _ = await asyncio.to_thread(_runner_still_owned, state, pid, identity)
            if not owned:
                return 0
            if reason == "max_uptime" or _uptime_exceeded():
                return await _handle_max_uptime()

            live_state, _ = await asyncio.to_thread(_query_session_status, svc.name, acct, base_env=merged_env)
            await asyncio.to_thread(state.save, last_live_state=live_state)
            st_curr = await asyncio.to_thread(state.load)
            _emit(f"[auto-restart] stream ended ({reason}, rc={rc}); remote live_state={live_state}\n")

            if live_state == "running":
                unreachable_streak = 0
                code = await _probe_async(svc, st_curr, 10.0)
                if code.isdigit() and 200 <= int(code) < 400 and st_curr.get("ready"):
                    _emit(
                        f"[auto-restart] container running and HTTP {code} healthy; "
                        "reattaching read-only WS iopub without incrementing restarts\n"
                    )
                    if reason != "idle_timeout":
                        await asyncio.sleep(min(1.0, restart_delay))
                    next_mode = "watch"
                    continue

                if restarts >= max_restarts:
                    _emit(
                        f"[auto-restart] service unhealthy (HTTP {code}) and max_restarts={max_restarts} exhausted\n"
                    )
                    await asyncio.to_thread(_stop_remote_session, svc.name, acct, base_env=merged_env)
                    if not st_curr.get("ever_ready"):
                        await asyncio.to_thread(_clear_ephemeral_session_state, state)
                        runtime_env.unlink(missing_ok=True)
                    return 1

                restarts += 1
                await asyncio.to_thread(state.save, restarts=restarts, restart_count=restarts, ready=False)
                _emit(f"[auto-restart] container running but probe={code}; restarting kernel ({restarts}/{max_restarts})\n")
                res_rst = await asyncio.to_thread(
                    _run_kaggle,
                    ["session", "restart", svc.name, "--json"],
                    account=acct,
                    timeout=120.0,
                    check=False,
                    base_env=merged_env,
                )
                if res_rst.returncode != 0:
                    _emit(f"[auto-restart] kaggle session restart failed ({res_rst.returncode})\n")
                    await asyncio.sleep(restart_delay)
                    continue
                next_mode = "exec"
                attempt += 1
                continue

            if live_state == "unreachable":
                delay = min(MAX_RESTART_BACKOFF, restart_delay * (2 ** unreachable_streak))
                unreachable_streak += 1
                _emit(
                    f"[auto-restart] session unreachable (network/proxy blip); sleeping {delay:.1f}s without stopping VM\n"
                )
                await asyncio.sleep(delay)
                continue

            # Branch 4: reclaimed | missing_credentials
            unreachable_streak = 0
            if restarts >= max_restarts:
                _emit(f"[auto-restart] remote session {live_state} and max_restarts={max_restarts} exhausted\n")
                if not st_curr.get("ever_ready"):
                    await asyncio.to_thread(_clear_ephemeral_session_state, state)
                runtime_env.unlink(missing_ok=True)
                return 1

            if accelerator != "cpu":
                try:
                    await asyncio.to_thread(_check_non_cpu_quota, accelerator, acct, base_env=merged_env)
                except CliError as err:
                    _emit(f"[auto-restart] quota gate blocked recreate: {err}\n")
                    return 1

            restarts += 1
            recreate_count += 1
            await asyncio.to_thread(
                state.save,
                ready=False,
                restarts=restarts,
                restart_count=restarts,
                session_recreate_count=recreate_count,
            )
            _emit(f"[auto-restart] recreating reclaimed session {svc.name} ({restarts}/{max_restarts})\n")
            try:
                created = await asyncio.to_thread(
                    _run_kaggle_json,
                    [
                        "session",
                        "create",
                        svc.name,
                        "--accelerator",
                        accelerator,
                        "--internet" if enable_internet else "--no-internet",
                        "--startup-timeout",
                        "600",
                        "--json",
                    ],
                    account=acct,
                    timeout=command_timeout(660),
                    base_env=merged_env,
                )
            except CliError as err:
                _emit(f"[auto-restart] session recreate failed: {err}\n")
                await asyncio.sleep(restart_delay)
                continue

            start_mono = time.monotonic()
            await asyncio.to_thread(
                state.save,
                kernel_session_id=created.get("kernel_session_id"),
                last_live_state="running",
                started_mono=start_mono,
            )
            next_mode = "exec"
            attempt += 1
    finally:
        _emit_safe(redactor.feed("", final=True))
        if log_handler is not None and logger is not None:
            with contextlib.suppress(Exception):
                log_handler.close()
                logger.removeHandler(log_handler)


def _exec_runner_native(svc: KaggleService, nb_path: Path, state: LockedState, pid: int, identity: str | None) -> int:
    acct = svc.account or state.load().get("account")

    async def _runner() -> int:
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(
            supervise_instance_async(svc, nb_path, owner_pid=pid, owner_identity=identity)
        )
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, task.cancel)
        try:
            return await task
        except asyncio.CancelledError:
            st_now = state.load()
            if not st_now.get("ever_ready"):
                await asyncio.to_thread(
                    _finalize_unready_stop, svc, state, acct, reason="interrupted before CT_READY"
                )
            return 130

    exit_code = 1
    try:
        exit_code = asyncio.run(_runner())
        return exit_code
    finally:
        finish_runner(state, pid, identity, exit_code)


def exec_runner(svc: KaggleService, nb_path: Path) -> int:
    """Run Kaggle Jupyter session execution, redacting run.log, enforcing --max-uptime and --auto-restart."""
    state = _state_for(svc)
    pid = os.getpid()
    identity = process_identity(pid)
    st = state.load()
    if st.get("exec_backend") == "native":
        state.save(runner_pid=pid, runner_identity=identity)
        return _exec_runner_native(svc, nb_path, state, pid, identity)
    acct = svc.account or st.get("account")
    accelerator = st.get("accelerator") or svc.accelerator
    enable_internet = bool(st.get("enable_internet", True))
    auto_restart = bool(st.get("auto_restart", False))
    max_restarts = int(st.get("max_restarts") if st.get("max_restarts") is not None else svc.max_restarts)
    max_uptime = st.get("max_uptime")
    if max_uptime is None and accelerator != "cpu":
        max_uptime = DEFAULT_MAX_UPTIME_SECONDS
    restart_delay = float(st.get("restart_delay") or DEFAULT_RESTART_DELAY)
    idle_check = float(st.get("idle_check_seconds") or DEFAULT_IDLE_CHECK_SECONDS)
    runtime_env = _runtime_env_path(state)
    is_foreground = st.get("runner_mode") == "foreground"

    state.save(runner_pid=pid, runner_identity=identity)
    logger, log_handler = create_private_rotating_logger(
        state.log_path,
        max_bytes=10 * 1024 * 1024,
        backup_count=3,
        logger_name=f"colabllm.kagglesvc.{svc.name}.{pid}",
    )

    known_secrets = _known_secrets_from_state(st, runtime_env)
    redactor = LineBufferedSecretRedactor(known_secrets=known_secrets, max_buffer=65536)

    def _emit_text(text: str) -> None:
        if not text:
            return
        safe = redactor.feed(text)
        if not safe:
            return
        for line in safe.splitlines():
            logger.info(line)
        log_handler.flush()
        if is_foreground:
            sys.stdout.write(safe)
            sys.stdout.flush()

    stop_event = threading.Event()
    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)

    def _handle_stop(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    start_mono = float(st.get("started_mono") or time.monotonic())
    active_child: subprocess.Popen[bytes] | None = None
    restarts = int(st.get("restarts") or 0)
    recreate_count = int(st.get("session_recreate_count") or 0)
    unreachable_streak = 0
    exit_code = 0

    def _uptime_exceeded() -> bool:
        return bool(accelerator != "cpu" and max_uptime and (time.monotonic() - start_mono) >= float(max_uptime))

    def _run_stream(args: list[str], *, mode: str) -> tuple[int | None, str]:
        """Run streaming `kaggle session exec` or `kaggle session logs -f`, monitoring protocol/uptime/health."""
        nonlocal active_child
        cmd = _kaggle_cmd(args, account=acct)
        env = kaggle_subprocess_env(acct)
        proto = NotebookProtocol(provider="kaggle")
        q: queue.Queue[bytes | None] = queue.Queue()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        active_child = proc
        state.save(exec_pid=proc.pid, exec_identity=process_identity(proc.pid), runner_mode=mode)

        def _reader() -> None:
            try:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read1(8192) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                    if not chunk:
                        break
                    q.put(chunk)
            except Exception:
                pass
            finally:
                q.put(None)

        threading.Thread(target=_reader, daemon=True).start()
        last_output = time.monotonic()
        last_probe = time.monotonic()
        stream_end = False

        try:
            while not stop_event.is_set():
                if _uptime_exceeded():
                    return None, "max_uptime"
                try:
                    item = q.get(timeout=1.0)
                except queue.Empty:
                    item = b""
                    if proc.poll() is not None and stream_end:
                        return proc.returncode, "eof"
                    now = time.monotonic()
                    if mode == "exec" and not proto.cell_started and proto.preflight_error:
                        return proc.returncode or 1, "preflight_error"
                    curr = state.load()
                    if curr.get("ready") and curr.get("url") and (now - last_probe) >= 15.0:
                        last_probe = now
                        code = _probe_service_endpoint(svc, curr, timeout=8.0)
                        if not (code.isdigit() and 200 <= int(code) < 400):
                            return proc.returncode or 1, f"probe_failed:{code}"
                    if (now - last_output) >= idle_check:
                        if auto_restart:
                            return proc.returncode, "idle_timeout"
                    continue

                if item is None:
                    stream_end = True
                    break

                last_output = time.monotonic()
                text = item.decode("utf-8", errors="replace")
                _emit_text(text)
                updates = proto.feed(item)
                if updates:
                    save_kw: dict[str, Any] = {}
                    if "url" in updates:
                        save_kw["url"] = updates["url"]
                    if "login_user" in updates:
                        save_kw["login_user"] = updates["login_user"]
                        save_kw["login_pass"] = updates["login_pass"]
                    if "stage" in updates:
                        save_kw["last_stage"] = updates["stage"]
                    if "cell" in updates:
                        save_kw["last_cell"] = updates["cell"]
                    if updates.get("ready"):
                        save_kw["ready"] = True
                        save_kw["ever_ready"] = True
                    if save_kw:
                        state.save(**save_kw)
                if not proto.cell_started and proto.preflight_error:
                    _emit_text(f"\n[kagglesvc] preflight error detected: {proto.preflight_error}\n")
                    return proc.returncode or 1, "preflight_error"

            if stop_event.is_set():
                return None, "signal"
            tail_updates = proto.feed(b"", final=True)
            if tail_updates:
                save_kw = {}
                if "url" in tail_updates:
                    save_kw["url"] = tail_updates["url"]
                if "login_user" in tail_updates:
                    save_kw["login_user"] = tail_updates["login_user"]
                    save_kw["login_pass"] = tail_updates["login_pass"]
                if "stage" in tail_updates:
                    save_kw["last_stage"] = tail_updates["stage"]
                if "cell" in tail_updates:
                    save_kw["last_cell"] = tail_updates["cell"]
                if tail_updates.get("ready"):
                    save_kw["ready"] = True
                    save_kw["ever_ready"] = True
                if save_kw:
                    state.save(**save_kw)
            return proc.wait(), "eof"
        finally:
            _terminate_child(proc)
            if proc.stdout is not None:
                with contextlib.suppress(OSError):
                    proc.stdout.close()

    try:
        next_mode = "exec"
        while not stop_event.is_set():
            if _uptime_exceeded():
                _emit_text(f"[kagglesvc] reached host --max-uptime ({max_uptime}s); stopping session {svc.name}\n")
                _stop_remote_session(svc.name, acct)
                _clear_ephemeral_session_state(state)
                runtime_env.unlink(missing_ok=True)
                nb_path.unlink(missing_ok=True)
                exit_code = 0
                break

            if next_mode == "logs-follow":
                rc, reason = _run_stream(["session", "logs", "-f", svc.name], mode="logs-follow")
            else:
                if not runtime_env.is_file():
                    _emit_text(f"[kagglesvc] missing runtime.env at {runtime_env}\n")
                    exit_code = 1
                    break
                rc, reason = _run_stream(
                    ["session", "exec", svc.name, "-f", str(nb_path), "--env-file", str(runtime_env)],
                    mode="exec",
                )

            if reason == "max_uptime":
                _emit_text(f"[kagglesvc] reached host --max-uptime ({max_uptime}s); stopping session {svc.name}\n")
                _stop_remote_session(svc.name, acct)
                _clear_ephemeral_session_state(state)
                runtime_env.unlink(missing_ok=True)
                nb_path.unlink(missing_ok=True)
                exit_code = 0
                break

            if reason == "signal" or stop_event.is_set():
                st_now = state.load()
                if not st_now.get("ever_ready"):
                    _finalize_unready_stop(svc, state, acct, reason="interrupted before CT_READY", emit=_emit_text)
                exit_code = 130
                break

            if not auto_restart:
                st_now = state.load()
                if (rc or 0) != 0 or not st_now.get("ever_ready"):
                    exit_code = rc if (rc and rc > 0) else 1
                    if not st_now.get("ever_ready"):
                        _finalize_unready_stop(
                            svc,
                            state,
                            acct,
                            reason=f"exec exited ({reason}, code={exit_code}) before CT_READY",
                            emit=_emit_text,
                        )
                else:
                    exit_code = 0
                break

            # 4-branch watchdog decision on auto-restart
            live_state, status_info = _query_session_status(svc.name, acct)
            state.save(last_live_state=live_state)
            st_curr = state.load()
            _emit_text(f"[auto-restart] stream ended ({reason}, rc={rc}); remote live_state={live_state}\n")

            if live_state == "running":
                unreachable_streak = 0
                code = _probe_service_endpoint(svc, st_curr, timeout=10.0)
                if code.isdigit() and 200 <= int(code) < 400 and st_curr.get("ready"):
                    _emit_text(
                        f"[auto-restart] container running and HTTP {code} healthy; "
                        "reattaching read-only `kaggle session logs -f` without incrementing restarts\n"
                    )
                    next_mode = "logs-follow"
                    continue

                if restarts >= max_restarts:
                    _emit_text(f"[auto-restart] service unhealthy (HTTP {code}) and max_restarts={max_restarts} exhausted\n")
                    _stop_remote_session(svc.name, acct)
                    if not st_curr.get("ever_ready"):
                        _clear_ephemeral_session_state(state)
                        runtime_env.unlink(missing_ok=True)
                    exit_code = 1
                    break

                restarts += 1
                state.save(restarts=restarts, restart_count=restarts, ready=False)
                _emit_text(f"[auto-restart] container running but probe={code}; restarting kernel ({restarts}/{max_restarts})\n")
                res_rst = _run_kaggle(["session", "restart", svc.name, "--json"], account=acct, timeout=120.0, check=False)
                if res_rst.returncode != 0:
                    _emit_text(f"[auto-restart] kaggle session restart failed ({res_rst.returncode})\n")
                    time.sleep(restart_delay)
                    continue
                next_mode = "exec"
                continue

            if live_state == "unreachable":
                delay = min(MAX_RESTART_BACKOFF, restart_delay * (2 ** unreachable_streak))
                unreachable_streak += 1
                _emit_text(
                    f"[auto-restart] session unreachable (network/proxy blip); sleeping {delay:.1f}s without stopping VM\n"
                )
                stop_event.wait(delay)
                continue

            # Branch 4: reclaimed | missing_credentials
            unreachable_streak = 0
            if restarts >= max_restarts:
                _emit_text(f"[auto-restart] remote session {live_state} and max_restarts={max_restarts} exhausted\n")
                if not st_curr.get("ever_ready"):
                    _clear_ephemeral_session_state(state)
                runtime_env.unlink(missing_ok=True)
                exit_code = 1
                break

            if accelerator != "cpu":
                try:
                    _check_non_cpu_quota(accelerator, acct)
                except CliError as err:
                    _emit_text(f"[auto-restart] quota gate blocked recreate: {err}\n")
                    exit_code = 1
                    break

            restarts += 1
            recreate_count += 1
            state.save(ready=False, restarts=restarts, restart_count=restarts, session_recreate_count=recreate_count)
            _emit_text(f"[auto-restart] recreating reclaimed session {svc.name} ({restarts}/{max_restarts})\n")
            try:
                created = _run_kaggle_json(
                    [
                        "session",
                        "create",
                        svc.name,
                        "--accelerator",
                        accelerator,
                        "--internet" if enable_internet else "--no-internet",
                        "--startup-timeout",
                        "600",
                        "--json",
                    ],
                    account=acct,
                    timeout=command_timeout(660),
                )
            except CliError as err:
                _emit_text(f"[auto-restart] session recreate failed: {err}\n")
                stop_event.wait(restart_delay)
                continue

            state.save(
                kernel_session_id=created.get("kernel_session_id"),
                last_live_state="running",
            )
            next_mode = "exec"

    finally:
        _terminate_child(active_child)
        flushed = redactor.feed("", final=True)
        if flushed:
            for line in flushed.splitlines():
                logger.info(line)
            log_handler.flush()
            if is_foreground:
                sys.stdout.write(flushed)
                sys.stdout.flush()
        with contextlib.suppress(Exception):
            log_handler.close()
            logger.removeHandler(log_handler)
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)
        st_final = state.load()
        if st_final.get("runner_pid") == pid and st_final.get("runner_identity") == identity:
            state.save(
                runner_pid=None,
                runner_identity=None,
                runner_mode=None,
                exec_pid=None,
                exec_identity=None,
                ready=False,
                runner_exit=exit_code,
            )
    return exit_code


def _wait_ready(svc: KaggleService, proc: Any, *, timeout: float | None = None) -> None:
    state = _state_for(svc)
    deadline = time.monotonic() + float(timeout if timeout is not None else svc.ready_timeout)
    offset = 0
    mode = state.load().get("runner_mode")
    if mode == "mux":
        log(f"runner started in shared mux (pid {proc.pid}) -- native WebSocket keep-alive (log: {state.log_path})")
    else:
        log(f"runner started (pid {proc.pid}); waiting for CT_READY (log: {state.log_path})")

    def _flush() -> None:
        nonlocal offset
        if not state.log_path.is_file():
            return
        with state.log_path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        offset += len(data)
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()

    while time.monotonic() < deadline:
        _flush()
        st = state.load()
        if st.get("ready") and st.get("url"):
            show_info(svc, do_probe=True, reveal=False)
            return
        if proc.poll() is not None:
            _flush()
            st = state.load()
            if st.get("ready") and st.get("url"):
                show_info(svc, do_probe=True, reveal=False)
                return
            raise CliError(f"Kaggle 运行器提前退出 (code {proc.returncode})；查看日志 {state.log_path}", proc.returncode or 1)
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    raise TimeoutError("等待 Kaggle 服务 CT_READY 超时")


def _cleanup_after_unready_wait_failure(svc: KaggleService, proc: Any, acct: str | None) -> None:
    state = _state_for(svc)
    st = state.load()
    if st.get("ever_ready"):
        return
    if st.get("runner_mode") == "mux":
        mux.stop_in_mux(svc.name, state, state_root=_state_root(), provider="kaggle", timeout=5.0)
    else:
        ident = process_identity(proc.pid)
        stop_owned_process(proc.pid, ident, group=True, timeout=5)
    _finalize_unready_stop(svc, state, acct, reason="default `up` failed or interrupted before CT_READY")


def _resolve_exec_backend(
    session_name: str,
    acct: str | None,
    a: Any,
    *,
    foreground: bool,
    saved_backend: str | None = None,
) -> tuple[bool, bool]:
    req_backend = (
        getattr(a, "exec_backend", None)
        or os.environ.get("COLABLLM_EXEC_BACKEND")
        or saved_backend
        or "auto"
    )
    use_native = (req_backend == "native") or (
        req_backend == "auto" and kaggle_ws.has_native_kaggle_session(session_name, acct)
    )
    use_mux = bool(
        use_native
        and not foreground
        and not bool(getattr(a, "no_mux", False))
        and not bool(os.environ.get("COLABLLM_NO_MUX"))
    )
    return use_native, use_mux


def _mux_env_overrides() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k.startswith(("COLABLLM_", "KAGGLE_", "FAKE_"))
        or k in ("PATH", "HOME", "XDG_CONFIG_HOME", "REQUEST_TIMEOUT", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
    }


def _launch_runner(
    svc: KaggleService,
    script: Path,
    clean_nb: Path,
    state: LockedState,
    *,
    foreground: bool,
    detach: bool,
    ready_timeout: float | None,
    acct: str | None,
    use_native: bool = False,
    use_mux: bool = False,
    cleanup_on_launch_error: bool = False,
) -> int:
    if foreground:
        state.save(
            runner_pid=os.getpid(),
            runner_identity=process_identity(os.getpid()),
            runner_mode="foreground",
            exec_backend=("native" if use_native else "cli"),
        )
        return exec_runner(svc, clean_nb)

    private_dir(state.dir)
    state.log_path.write_bytes(b"")
    state.log_path.chmod(0o600)
    try:
        if use_mux:
            proc: Any = mux.start_in_mux(
                svc,
                clean_nb,
                state_root=_state_root(),
                exec_backend="native",
                env_overrides=_mux_env_overrides(),
                provider="kaggle",
                state=state,
            )
        else:
            cmd = [sys.executable, str(script), *(["-i", svc.instance] if svc.instance else []), "_exec", str(clean_nb)]
            with state.log_path.open("ab") as logfile:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
            ident = process_identity(proc.pid)
            if not ident:
                raise RuntimeError(f"后台进程在登记前退出；查看日志 {state.log_path}")
            state.save(
                runner_pid=proc.pid,
                runner_identity=ident,
                runner_mode="detach" if detach else "background",
                exec_backend=("native" if use_native else "cli"),
            )
    except BaseException:
        if cleanup_on_launch_error and not state.load().get("ever_ready"):
            with contextlib.suppress(Exception):
                _finalize_unready_stop(svc, state, acct, reason="runner failed to start before CT_READY")
        raise
    if detach:
        print(
            json.dumps(
                {
                    "provider": "kaggle",
                    "service": svc.name,
                    "runner_pid": proc.pid,
                    "runner_mode": "mux" if use_mux else "detach",
                    "exec_backend": "native" if use_native else "cli",
                    "log_path": str(state.log_path),
                    "detached": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    try:
        _wait_ready(svc, proc, timeout=ready_timeout)
        return 0
    except KeyboardInterrupt:
        st_now = state.load()
        if not st_now.get("ever_ready"):
            _cleanup_after_unready_wait_failure(svc, proc, acct)
            raise
        log("服务已进入就绪运行态，Ctrl-C 仅停止继续观察日志；远端容器保持后台运行（使用 down 停止）")
        return 130
    except (TimeoutError, CliError):
        _cleanup_after_unready_wait_failure(svc, proc, acct)
        raise


def cmd_up(svc: KaggleService, script: Path, a: argparse.Namespace) -> int:
    _check_reserved_ports(svc)
    validate_kaggle_session_name(svc.name)

    enable_internet = svc.enable_internet if a.internet is None else bool(a.internet)
    if not enable_internet:
        raise UsageError("Kaggle 公开隧道服务要求启用互联网出网，不能使用 --no-internet")

    accelerator = normalize_kaggle_accelerator(a.accelerator or svc.accelerator)
    if a.max_restarts is not None and a.max_restarts > 0 and not a.auto_restart:
        raise UsageError("--max-restarts 仅在启用 --auto-restart 时生效")
    effective_max_restarts = (
        svc.max_restarts if a.max_restarts is None else int(a.max_restarts)
    )
    effective_max_uptime = (
        int(a.max_uptime)
        if a.max_uptime is not None
        else (svc.max_uptime if accelerator != "cpu" else None)
    )

    if not shutil.which("kaggle"):
        raise CliError("未找到 `kaggle` 可执行文件；请安装 kaggle 与 kaggle-ext 扩展并加入 PATH")

    root = _state_root()
    acct, _ = resolve_effective_kaggle_account(explicit=svc.account, prefer_saved=False)
    transport = tunnel_transport(a, state_root=root)
    hostname = tunnel_hostname(svc, a, namespace=KAGGLE_NAMESPACE, state_root=root)

    nb_src = script.with_name(svc.notebook)
    if not nb_src.is_file():
        raise CliError(f"未找到笔记本文件: {nb_src}")

    if a.dry_run:
        plan = {
            "dry_run": True,
            "provider": "kaggle",
            "service": svc.base_name,
            "kaggle_session": svc.name,
            "notebook": str(nb_src),
            "account": acct,
            "accelerator": accelerator,
            "enable_internet": enable_internet,
            "transport": transport,
            "hostname": hostname,
            "service_port": svc.service_port,
            "public_port": svc.public_port,
            "auto_restart": bool(a.auto_restart),
            "max_restarts": effective_max_restarts,
            "max_uptime": effective_max_uptime,
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    for tool in (("cf",) if hostname else ()) + (("ssh",) if transport == "frp" else ()):
        if not shutil.which(tool):
            raise CliError(f"未找到 `{tool}` 可执行文件；请安装 `{tool}` 并加入 PATH")

    if accelerator != "cpu":
        _check_non_cpu_quota(accelerator, acct)

    state = _state_for(svc)
    with state.lock("operation", blocking=False):
        st = state.load()
        if runner_alive(st):
            raise CliError(f"Kaggle 实例 {svc.name} 已经在运行中 (pid={st.get('runner_pid')})")

        live_state, remote_info = _query_session_status(svc.name, acct)
        if live_state == "running":
            remote_accel = normalize_kaggle_accelerator(remote_info.get("accelerator") or accelerator)
            remote_inet = remote_info.get("enable_internet", enable_internet)
            remote_acct = remote_info.get("account") or acct
            if remote_accel != accelerator or bool(remote_inet) != bool(enable_internet) or (
                acct and remote_acct and remote_acct != acct
            ):
                raise CliError(
                    f"远端存活会话 {svc.name} 配置冲突 "
                    f"(remote=({remote_accel}, internet={remote_inet}, account={remote_acct}), "
                    f"requested=({accelerator}, internet={enable_internet}, account={acct}))；请先执行 down"
                )

        if a.new_credentials or not st.get("auth_pass") or not st.get("api_token"):
            auth_user = svc.auth_user
            auth_pass = secrets.token_urlsafe(18)
            api_token = "sk-" + secrets.token_urlsafe(32)
        else:
            auth_user = st.get("auth_user") or svc.auth_user
            auth_pass = st["auth_pass"]
            api_token = st["api_token"]

        env: dict[str, str] = {
            "CT_SERVICE": svc.name,
            "CT_SERVICE_PORT": str(svc.service_port),
            "CT_PUBLIC_PORT": str(svc.tunnel_port),
            "CT_AUTH": svc.auth,
            "CT_AUTH_USER": auth_user,
            "CT_AUTH_PASS": auth_pass,
            "CT_API_TOKEN": api_token,
            "CT_WORK_DIR": "/tmp/ct-runtime",
            "CT_CACHE_DIR": "/kaggle/working/.ct-cache",
            "CT_BIN_DIR": "/kaggle/working/.ct-cache/bin",
            **svc.env,
            "CT_TRANSPORT": transport,
            "CT_TUNNEL_TOKEN": "",
            "CT_PUBLIC_HOSTNAME": "",
            "CT_FRP_SERVER_IP": "",
            "CT_FRP_SERVER_ADDR": "",
            "CT_FRP_SERVER_PORT": "",
            "CT_FRP_REMOTE_PORT": "",
            "CT_FRP_PROXY_NAME": "",
            "CT_FRP_TOKEN": "",
            "CT_FRP_CA_CERT": "",
            "CT_FRP_SERVER_NAME": "",
            "CT_FRP_AUTH_SCOPES": "",
            "CT_FRP_VERSION": "",
        }

        tunnel_cfg = _core.load_tunnel_config(state_root=root)
        frp_cfg = tunnel_cfg.get("frp") if isinstance(tunnel_cfg.get("frp"), dict) else None
        created_cf_route: dict[str, Any] | None = None
        created_frp_route: dict[str, Any] | None = None
        cf_route = st.get("cloudflare_route")
        frp_route = st.get("frp_route")

        if transport == "frp":
            if not frp_cfg or not frp_cfg.get("enabled"):
                raise UsageError("尚未启用全局 FRP；请先执行 colabllm frp-config")
            if not hostname:
                raise UsageError("FRP 传输需要固定域名；请先配置 --base-domain 或传入 --domain")
            frp_cfg = frp_validate_config({**frp_cfg, "enabled": True})
            if isinstance(frp_route, dict) and (
                frp_route.get("hostname") != hostname
                or any(frp_route.get("frp", {}).get(k) != frp_cfg.get(k) for k in ("server_ip", "server_port", "ssh", "project_id"))
            ):
                raise ValueError("Existing FRP route targets another endpoint; use down --delete-tunnel first")
            route, frp_env = frp_ensure_route(svc, frp_cfg, hostname, root, namespace=KAGGLE_NAMESPACE)
            frp_env.setdefault("CT_FRP_SERVER_ADDR", frp_env.get("CT_FRP_SERVER_IP", ""))
            env.update(frp_env)
            if not frp_route:
                created_frp_route = route
            frp_route = route
        elif hostname:
            info = ensure_named_tunnel(svc, hostname, namespace=KAGGLE_NAMESPACE, state_root=root)
            env.update(CT_TUNNEL_TOKEN=info["token"], CT_PUBLIC_HOSTNAME=hostname)
            route = {k: v for k, v in info.items() if k != "token"}
            if not cf_route:
                created_cf_route = route
            cf_route = route

        try:
            created = _run_kaggle_json(
                [
                    "session",
                    "create",
                    svc.name,
                    "--accelerator",
                    accelerator,
                    "--internet" if enable_internet else "--no-internet",
                    "--startup-timeout",
                    f"{float(a.startup_timeout):g}",
                    "--json",
                ],
                account=acct,
                timeout=command_timeout(max(120.0, float(a.startup_timeout) + 60.0)),
            )
        except Exception:
            if created_frp_route is not None:
                with contextlib.suppress(Exception):
                    frp_remove_route(created_frp_route, svc.name, namespace=KAGGLE_NAMESPACE)
            if created_cf_route is not None:
                with contextlib.suppress(Exception):
                    delete_named_tunnel(created_cf_route, namespace=KAGGLE_NAMESPACE, svc_name=svc.name)
            raise

        clean_nb = _clean_notebook_path(state, nb_src.name)
        _strip_nb_for_exec(nb_src, clean_nb)
        write_private_env_file(_runtime_env_path(state), env)

        effective_acct = created.get("account") or acct
        use_native, use_mux = _resolve_exec_backend(
            svc.name,
            effective_acct,
            a,
            foreground=bool(a.foreground),
        )

        state.save(
            schema_version=1,
            provider="kaggle",
            service=svc.name,
            base_service=svc.base_name,
            catalog_service=f"kaggle-{svc.base_name}",
            kaggle_session=created.get("name") or svc.name,
            account=effective_acct,
            kernel_session_id=created.get("kernel_session_id"),
            accelerator=accelerator,
            enable_internet=enable_internet,
            url=f"https://{hostname}" if hostname else st.get("url"),
            hostname=hostname,
            ready=False,
            ever_ready=False,
            auth_mode=svc.auth,
            auth_user=auth_user,
            auth_pass=auth_pass,
            api_token=api_token,
            transport=transport,
            cloudflare_route=cf_route,
            frp_route=frp_route,
            runner_pid=None,
            runner_identity=None,
            runner_mode=None,
            exec_backend=("native" if use_native else "cli"),
            exec_pid=None,
            exec_identity=None,
            restarts=0,
            restart_count=0,
            session_recreate_count=0,
            started=time.strftime("%Y-%m-%d %H:%M:%S"),
            started_mono=time.monotonic(),
            last_live_state=live_state if live_state == "running" else "running",
            auto_restart=bool(a.auto_restart),
            restart_delay=float(a.restart_delay if a.restart_delay is not None else DEFAULT_RESTART_DELAY),
            idle_check_seconds=float(a.idle_check if a.idle_check is not None else DEFAULT_IDLE_CHECK_SECONDS),
            max_restarts=effective_max_restarts,
            max_uptime=effective_max_uptime,
            ready_timeout=float(a.ready_timeout or svc.ready_timeout),
            env=_strip_secret_env(env),
        )

    return _launch_runner(
        svc,
        script,
        clean_nb,
        state,
        foreground=bool(a.foreground),
        detach=bool(a.detach),
        ready_timeout=a.ready_timeout,
        acct=effective_acct,
        use_native=use_native,
        use_mux=use_mux,
        cleanup_on_launch_error=True,
    )


def _read_single_line_env_file(path: Path) -> dict[str, str]:
    """Read existing 0600 single-line quoted runtime.env preserving multiline values across restarts."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, _, raw_v = line.partition("=")
        key = k.strip()
        val = raw_v.strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            inner = val[1:-1]
            chars: list[str] = []
            idx = 0
            while idx < len(inner):
                ch = inner[idx]
                if ch == "\\" and idx + 1 < len(inner):
                    nxt = inner[idx + 1]
                    if nxt == "n":
                        chars.append("\n")
                    elif nxt == "r":
                        chars.append("\r")
                    elif nxt == "t":
                        chars.append("\t")
                    elif nxt in ('"', "\\"):
                        chars.append(nxt)
                    else:
                        chars.append("\\" + nxt)
                    idx += 2
                else:
                    chars.append(ch)
                    idx += 1
            val = "".join(chars)
        out[key] = val
    return out


def cmd_restart(svc: KaggleService, script: Path, a: argparse.Namespace) -> int:
    state = _state_for(svc)
    nb_src = script.with_name(svc.notebook)
    if not nb_src.is_file():
        raise CliError(f"未找到笔记本文件: {nb_src}")
    clean_nb = _clean_notebook_path(state, nb_src.name)
    runtime_env = _runtime_env_path(state)

    with state.lock("operation", blocking=False):
        st = state.load()
        if not st:
            raise CliError(f"未找到 Kaggle 实例 {svc.name} 的运行状态；请先执行 up")
        acct, _ = resolve_effective_kaggle_account(explicit=svc.account, saved=st.get("account"), prefer_saved=True)
        if getattr(a, "dry_run", False):
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "action": "restart",
                        "service": svc.name,
                        "account": acct,
                        "new_credentials": bool(a.new_credentials),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        prev_backend = st.get("exec_backend")
        stop_owned_process(st.get("exec_pid"), st.get("exec_identity"), group=True, timeout=5)
        if st.get("runner_mode") == "mux":
            mux.stop_in_mux(svc.name, state, state_root=_state_root(), provider="kaggle", timeout=5.0)
        else:
            stop_owned_process(st.get("runner_pid"), st.get("runner_identity"), group=True, timeout=5)

        if a.new_credentials:
            auth_pass = secrets.token_urlsafe(18)
            api_token = "sk-" + secrets.token_urlsafe(32)
            st = state.save(auth_pass=auth_pass, api_token=api_token)
            env = {
                **(st.get("env") or {}),
                **_read_single_line_env_file(runtime_env),
                "CT_AUTH_USER": st.get("auth_user") or svc.auth_user,
                "CT_AUTH_PASS": auth_pass,
                "CT_API_TOKEN": api_token,
            }
            write_private_env_file(runtime_env, env)
        elif not runtime_env.is_file():
            env = {
                **(st.get("env") or {}),
                "CT_AUTH_USER": st.get("auth_user") or svc.auth_user,
                "CT_AUTH_PASS": st.get("auth_pass") or "",
                "CT_API_TOKEN": st.get("api_token") or "",
            }
            write_private_env_file(runtime_env, env)

        _strip_nb_for_exec(nb_src, clean_nb)
        _run_kaggle(["session", "restart", svc.name, "--json"], account=acct, timeout=120.0, check=True)
        use_native, use_mux = _resolve_exec_backend(
            svc.name,
            acct,
            a,
            foreground=bool(a.foreground),
            saved_backend=prev_backend,
        )
        state.save(
            ready=False,
            runner_pid=None,
            runner_identity=None,
            runner_mode=None,
            exec_backend=("native" if use_native else "cli"),
            exec_pid=None,
            exec_identity=None,
            last_live_state="running",
        )

    return _launch_runner(
        svc,
        script,
        clean_nb,
        state,
        foreground=bool(a.foreground),
        detach=bool(a.detach),
        ready_timeout=a.ready_timeout,
        acct=acct,
        use_native=use_native,
        use_mux=use_mux,
    )


def cmd_down(svc: KaggleService, a: argparse.Namespace) -> None:
    if getattr(a, "all", False):
        items = list_service_instances(svc.base_name or svc.name)
        target_filter = resolve_effective_kaggle_account(explicit=svc.account)[0] if svc.account else None
        errors: list[tuple[str, Exception]] = []
        for it in items:
            if target_filter and it.get("account") and it.get("account") != target_filter:
                continue
            sub_svc = dataclasses.replace(svc, account=target_filter)
            apply_instance(sub_svc, it["instance_arg"] or None)
            sub_a = argparse.Namespace(delete_tunnel=a.delete_tunnel, all=False)
            try:
                cmd_down(sub_svc, sub_a)
            except (CliError, RuntimeError, OSError) as err:
                errors.append((sub_svc.name, err))
        if errors:
            summary = "; ".join(f"{name}: {err}" for name, err in errors)
            code = errors[0][1].code if isinstance(errors[0][1], CliError) else 1
            raise CliError(f"部分 Kaggle 实例停止失败：{summary}", code)
        return

    state = _state_for(svc)
    with state.lock("operation", blocking=False):
        st = state.load()
        acct, _ = resolve_effective_kaggle_account(explicit=svc.account, saved=st.get("account"), prefer_saved=True)
        session_name = st.get("kaggle_session") or svc.name
        log(f"kaggle session stop {session_name}" + (f" (account {acct})" if acct else ""))
        stopped, err = _stop_remote_session(session_name, acct)
        if not stopped:
            raise CliError(f"停止 Kaggle 会话 {session_name} 失败 ({err})；已保留运行状态，可重试 down", 1)

        stop_owned_process(st.get("exec_pid"), st.get("exec_identity"), group=True)
        if st.get("runner_mode") == "mux":
            mux.stop_in_mux(svc.name, state, state_root=_state_root(), provider="kaggle", timeout=5.0)
        else:
            stop_owned_process(st.get("runner_pid"), st.get("runner_identity"), group=True)

        if getattr(a, "delete_tunnel", False) and st.get("frp_route"):
            frp_remove_route(st["frp_route"], svc.name, namespace=KAGGLE_NAMESPACE)
            st.pop("frp_route", None)
            if not st.get("cloudflare_route"):
                st.pop("hostname", None)
                st.pop("transport", None)
        if getattr(a, "delete_tunnel", False) and st.get("cloudflare_route"):
            delete_named_tunnel(st["cloudflare_route"], namespace=KAGGLE_NAMESPACE, svc_name=svc.name)
            st.pop("cloudflare_route", None)
            if not st.get("frp_route"):
                st.pop("hostname", None)
                st.pop("transport", None)

        keep_keys = (
            "schema_version",
            "provider",
            "base_service",
            "catalog_service",
            "auth_mode",
            "auth_user",
            "auth_pass",
            "api_token",
            "hostname",
            "transport",
            "frp_route",
            "cloudflare_route",
            "account",
        )
        state.update(lambda _current: {k: st[k] for k in keep_keys if k in st and k not in _EPHEMERAL_SESSION_KEYS})
        _runtime_env_path(state).unlink(missing_ok=True)
        _clean_notebook_path(state, Path(svc.notebook).name).unlink(missing_ok=True)


def build_parser(svc: KaggleService, configure=None) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"colabllm kaggle {svc.base_name}",
        description=(getattr(sys.modules.get("__main__"), "__doc__", None) or "").strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    p.add_argument("-i", "--instance", metavar="ID", type=validate_kaggle_instance,
                   help="Kaggle 实例 ID（与服务名组合后总长 <= 48 位，如 -i 1）")
    p.add_argument("--account", metavar="ACCOUNT", type=validate_kaggle_account,
                   help="Kaggle 账户名或 @username（来自 `kaggle account list` 或 env COLABLLM_KAGGLE_ACCOUNT）")
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("up", help="创建并运行 Kaggle 交互式会话及安全隧道")
    u.add_argument("-a", "--account", default=argparse.SUPPRESS, metavar="ACCOUNT", type=validate_kaggle_account,
                   help="本次启动使用的 Kaggle 账户名或 @username")
    tunnel = u.add_mutually_exclusive_group()
    tunnel.add_argument("--domain", nargs="?", const=True, metavar="HOST", type=validate_domain,
                        help="完整固定域名；省略 HOST 时使用已配置的主域名")
    tunnel.add_argument("--base-domain", metavar="DOMAIN", type=validate_domain,
                        help="本次启动的主域名，如 example.com，自动生成 kaggle-<service>.example.com")
    tunnel.add_argument("--quick-tunnel", action="store_true", help="忽略主域名配置，使用动态 trycloudflare.com URL")
    u.add_argument("--transport", choices=("cloudflare", "frp"), help="本次启动覆盖全局隧道后端")
    u.add_argument("--accelerator", help="加速器类型: cpu | NvidiaTeslaT4 (t4/gpu) | NvidiaTeslaT4Highmem | TpuV5E8 (tpu)")
    u.add_argument("--internet", action=argparse.BooleanOptionalAction, default=None,
                   help="启用/禁用容器互联网访问（公网隧道需要 --internet，默认开启）")
    u.add_argument("--new-credentials", action="store_true", help="轮换 Caddy 基础认证密码与 API Token")
    mode = u.add_mutually_exclusive_group()
    mode.add_argument("--foreground", action="store_true", help="在前台终端持续运行")
    mode.add_argument("-d", "--detach", action="store_true", help="启动后台守护后立即返回")
    u.add_argument("--auto-restart", action="store_true", help="守护模式：按 live_state 执行 logs-follow / 重启 / 退避 / 有界重建")
    u.add_argument("--restart-delay", type=positive_seconds, metavar="SECONDS", help="自动恢复基础等待秒数（默认 5）")
    u.add_argument("--idle-check", type=positive_seconds, metavar="SECONDS", help="静默巡检超时秒数（默认 300）")
    u.add_argument("--max-restarts", type=nonnegative_int, default=None, metavar="N",
                   help="最大自动重启/重建次数（开启 --auto-restart 时默认 3，传 0 禁用重建）")
    u.add_argument("--max-uptime", type=duration_seconds, default=None, metavar="DURATION",
                   help="单次运行最大时长，如 6h 或 21600（非 CPU 会话默认 6h）")
    u.add_argument("--ready-timeout", type=positive_seconds, help="等待 CT_READY 的超时秒数")
    u.add_argument("--startup-timeout", type=positive_seconds, default=DEFAULT_STARTUP_TIMEOUT,
                   help="kaggle session create 启动等待超时秒数（默认 600）")
    u.add_argument("--exec-backend", choices=("auto", "native", "cli"), default="auto",
                   help="保活执行后端：auto 优先使用原生轻量 WebSocket 多路复用，无会话凭据时回退 kaggle session exec（默认: auto）")
    u.add_argument("--no-mux", action="store_true",
                   help="禁用单进程多路复用守护，为当前实例单独启动后台进程")
    u.add_argument("--dry-run", action="store_true", help="离线校验并打印执行计划，不调用 kaggle CLI")

    r = sub.add_parser("restart", help="重启远端 Kaggle Jupyter 内核并复用 /kaggle/working/.ct-cache")
    r.add_argument("-a", "--account", default=argparse.SUPPRESS, metavar="ACCOUNT", type=validate_kaggle_account,
                   help="Kaggle 账户名或 @username")
    r_mode = r.add_mutually_exclusive_group()
    r_mode.add_argument("--foreground", action="store_true", help="在前台终端运行重启后的执行流")
    r_mode.add_argument("-d", "--detach", action="store_true", help="触发重启并在后台守护运行")
    r.add_argument("--ready-timeout", type=positive_seconds, help="等待 CT_READY 的超时秒数")
    r.add_argument("--new-credentials", action="store_true", help="重启前轮换密码与 API Token")
    r.add_argument("--exec-backend", choices=("auto", "native", "cli"), default=None,
                   help="重启后使用的保活执行后端（默认继承实例配置或 auto）")
    r.add_argument("--no-mux", action="store_true", help="禁用单进程多路复用守护")
    r.add_argument("--dry-run", action="store_true", help="预览重启计划而不触发远端操作")

    d = sub.add_parser("down", help="停止本地执行器与远端 Kaggle 会话并清理 runtime.env")
    d.add_argument("-a", "--account", default=argparse.SUPPRESS, metavar="ACCOUNT", type=validate_kaggle_account,
                   help="Kaggle 账户名或 @username")
    d.add_argument("--delete-tunnel", action="store_true", help="同步清理 Cloudflare 隧道 / FRP 路由与 DNS 记录")
    d.add_argument("--all", action="store_true", help="停止该服务下所有 Kaggle 实例")

    for name, help_text in (
        ("info", "查看实例 URL 与本地状态"),
        ("ps", "列出该服务的所有 Kaggle 实例"),
        ("list", "ps 别名"),
        ("status", "查询远端 `kaggle session status` 与公网健康探针"),
    ):
        output = sub.add_parser(name, help=help_text)
        output.add_argument("-a", "--account", default=argparse.SUPPRESS, metavar="ACCOUNT", type=validate_kaggle_account,
                            help="Kaggle 账户名或 @username")
        output.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
        output.add_argument("--show-secrets", action="store_true", help="显示鉴权凭据")

    lg = sub.add_parser("logs", help="查看本地运行日志")
    lg.add_argument("-a", "--account", default=argparse.SUPPRESS, metavar="ACCOUNT", type=validate_kaggle_account,
                    help="Kaggle 账户名或 @username")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("-n", "--lines", type=int, default=80)

    tc = sub.add_parser("tunnel-config", help="查看或配置共享全局域名与传输后端")
    settings = tc.add_mutually_exclusive_group()
    settings.add_argument("--base-domain", metavar="DOMAIN", type=validate_domain)
    settings.add_argument("--clear", action="store_true", help="清除持久主域名配置")

    p.add_argument("--command-timeout", type=positive_seconds, help="外部命令超时秒数")
    p.add_argument("--debug", action="store_true", help="输出异常堆栈")
    p.add_argument("--quiet", action="store_true", help="静默诊断输出")
    p.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    if configure:
        configure(p, sub, u)
    public = list(sub.choices)
    sub.metavar = "{" + ",".join(public) + "}"
    ex = sub.add_parser("_exec")
    ex.add_argument("nb")
    return p


def main(svc: KaggleService, *, configure=None, handlers=None, prepare_up=None) -> int:
    script = Path(sys.argv[0]).resolve()
    inst, argv_after_inst = extract_instance_arg(sys.argv[1:])
    acct, cleaned_argv = extract_account_arg(argv_after_inst)
    apply_instance(svc, inst)
    svc.account = acct
    p = build_parser(svc, configure)
    a = p.parse_args(cleaned_argv)
    if getattr(a, "instance", None):
        apply_instance(svc, a.instance)
    if getattr(a, "account", None):
        svc.account = a.account
    if handlers and a.cmd in handlers:
        code = handlers[a.cmd](a)
        return int(code) if isinstance(code, int) else 0

    if a.cmd == "up":
        try:
            if prepare_up:
                prepare_up(a)
            return cmd_up(svc, script, a)
        except ValueError as error:
            if isinstance(error, UsageError):
                raise
            p.error(str(error))
    elif a.cmd == "restart":
        try:
            return cmd_restart(svc, script, a)
        except ValueError as error:
            if isinstance(error, UsageError):
                raise
            p.error(str(error))
    elif a.cmd == "tunnel-config":
        try:
            _core.cmd_tunnel_config(a, state_root=_state_root())
            return 0
        except (ValueError, OSError) as error:
            p.error(str(error))
    elif a.cmd == "_exec":
        return exec_runner(svc, Path(a.nb))
    elif a.cmd == "down":
        cmd_down(svc, a)
        return 0
    elif a.cmd == "info":
        show_info(svc, do_probe=False, reveal=a.show_secrets, output_json=a.json)
        return 0
    elif a.cmd in ("ps", "list"):
        show_instances(svc, reveal=a.show_secrets, output_json=a.json)
        return 0
    elif a.cmd == "status":
        st_curr = _state_for(svc).load()
        resolved_acct, _ = resolve_effective_kaggle_account(
            explicit=svc.account,
            saved=st_curr.get("account"),
            prefer_saved=True,
        )
        svc.account = resolved_acct
        session_name = st_curr.get("kaggle_session") or svc.name
        live_state, _ = _query_session_status(session_name, resolved_acct)
        if not show_info(svc, do_probe=True, reveal=a.show_secrets, output_json=a.json, live_state=live_state):
            return 1
        return 0
    elif a.cmd == "logs":
        lp = _state_for(svc).log_path
        if not lp.exists():
            die(f"no log at {lp}")
        os.execvp("tail", ["tail", "-n", str(a.lines), *(["-f"] if a.follow else []), str(lp)])
    return 0
