"""Dynamic state, logs, credentials, account, and tunnel store under COLABLLM_HOME."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
from typing import Any

from cli_support import UsageError
from colab_accounts import (
    account_dfs_credentials_path,
    list_account_names,
    load_accounts_registry,
    resolve_effective_account,
    resolve_effective_drive_account,
)
from colabsvc import validate_domain, validate_instance
from frp_backend import validate_config as validate_frp_config, write_private
from paths import user_home
from processes import runner_alive

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CELL_RE = re.compile(r"(?:cell\s+(\d+)|#\s*@title\s+([^\r\n]+))", re.IGNORECASE)
_SECRET_KEYS = ("auth_user", "auth_pass", "api_token", "login_user", "login_pass")


def _allowed_secret_keys(service_name: str, auth_mode: str | None) -> tuple[str, ...]:
    if service_name == "unsloth-studio":
        return ("login_user", "login_pass")
    if auth_mode == "caddy":
        return _SECRET_KEYS
    return ("api_token", "login_user", "login_pass")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _services_root() -> Path:
    return user_home() / "services"


def resolve_instance_dir(
    service_name: str,
    instance_arg: str = "",
    known_services: set[str] | None = None,
) -> tuple[str, str, Path]:
    if known_services is not None and service_name not in known_services:
        raise UsageError(f"未知运行时服务: {service_name}")
    if not validate_instance(service_name):
        raise UsageError(f"无效服务名: {service_name}")

    inst = (instance_arg or "").strip()
    if inst == "default":
        inst = ""
    if inst:
        validate_instance(inst)
        session_name = f"{service_name}-{inst}"
    else:
        session_name = service_name

    services_root = _services_root().resolve()
    target = (services_root / session_name).resolve()
    if not target.is_relative_to(services_root):
        raise UsageError("非法的实例目录路径")
    return inst, session_name, target


def _load_state_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as err:
        raise RuntimeError(f"状态文件损坏：{path}；请修复该 JSON 文件或从备份恢复") from err
    if not isinstance(data, dict):
        raise ValueError(f"状态文件格式无效：{path}；必须是 JSON 对象")
    return data


def _read_tail_bytes(path: Path, max_bytes: int = 8192) -> str:
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
        return strip_ansi(raw.decode("utf-8", errors="replace"))
    except OSError:
        return ""


def _infer_stage_hint(st: dict[str, Any], running: bool, log_path: Path) -> str:
    if running and st.get("ready"):
        return "已就绪 (CT_READY)"
    if not running:
        exit_code = st.get("runner_exit")
        if exit_code not in (None, 0):
            return f"已停止 (退出码 {exit_code})"
        return "已停止"

    tail = _read_tail_bytes(log_path, 8192)
    if st.get("url") and not st.get("ready"):
        return "已分配公网地址，等待服务就绪检查"
    if "[auto-restart]" in tail.splitlines()[-5:] if tail else False:
        return "守护进程正在重连或重建会话"

    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    for line in reversed(lines[-30:]):
        if "CT_READY" in line:
            return "已就绪 (CT_READY)"
        if "CT_PUBLIC_URL=" in line:
            return "隧道已连通，等待服务健康检查"
        if "预连接中断" in line:
            return "连接闪断，正在自动重连 Colab..."
        if "colab dfs mount" in line:
            return "正在挂载 Google Drive"
        if "colab new" in line or "reusing Colab session" in line or "申请" in line:
            return "正在准备 Colab 会话"
        match = _CELL_RE.search(line)
        if match:
            if match.group(2):
                return f"正在执行：{match.group(2)[:48]}"
            return f"正在执行 Cell {match.group(1)}"
    return "正在启动并连接 Colab..."


def _match_service_name(dir_name: str, st: dict[str, Any], known_names: set[str]) -> tuple[str, str] | None:
    recorded_base = st.get("base_service")
    if isinstance(recorded_base, str) and recorded_base in known_names:
        if dir_name == recorded_base:
            return recorded_base, ""
        if dir_name.startswith(recorded_base + "-"):
            return recorded_base, dir_name[len(recorded_base) + 1 :]
        return recorded_base, ""

    candidates = [name for name in known_names if dir_name == name or dir_name.startswith(name + "-")]
    if not candidates:
        return None
    base = max(candidates, key=len)
    inst = "" if dir_name == base else dir_name[len(base) + 1 :]
    return base, inst


def list_all_instances(catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    root = _services_root()
    if not root.is_dir():
        return []
    known_names = set(catalog.keys())
    items: list[dict[str, Any]] = []

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        state_path = entry / "state.json"
        if not state_path.is_file():
            continue
        try:
            st = _load_state_json(state_path)
            corrupt_error = None
        except (RuntimeError, ValueError) as err:
            st = {}
            corrupt_error = str(err)

        matched = _match_service_name(entry.name, st, known_names)
        if matched is None:
            continue
        service_name, inst_arg = matched
        meta = catalog[service_name]
        allowed_keys = _allowed_secret_keys(service_name, meta.get("auth"))
        running = runner_alive(st) if not corrupt_error else False
        ready = bool(st.get("ready") and running)
        state_label = "ERROR" if corrupt_error else ("READY" if ready else ("STARTING" if running else "STOPPED"))
        log_path = entry / "run.log"
        stage_hint = corrupt_error if corrupt_error else _infer_stage_hint(st, running, log_path)

        items.append({
            "service": service_name,
            "instance": inst_arg or "default",
            "instance_arg": inst_arg,
            "session_name": entry.name,
            "running": running,
            "ready": ready,
            "state_label": state_label,
            "stage_hint": stage_hint,
            "auto_restart": bool(st.get("auto_restart")),
            "restart_count": int(st.get("restart_count") or 0),
            "url": st.get("url"),
            "hostname": st.get("hostname"),
            "transport": st.get("transport"),
            "account": st.get("account"),
            "drive_account": st.get("drive_account"),
            "started": st.get("started"),
            "session_gpu": st.get("session_gpu") or meta.get("gpu"),
            "mount_drive": bool(st.get("mount_drive", True)),
            "runner_exit": st.get("runner_exit"),
            "log_exists": log_path.is_file(),
            "has_api_token": bool(st.get("api_token")) and "api_token" in allowed_keys,
            "has_basic_auth": (
                bool(st.get("auth_user") and st.get("auth_pass"))
                and "auth_user" in allowed_keys
                and "auth_pass" in allowed_keys
            ),
            "has_app_login": (
                bool(st.get("login_user") and st.get("login_pass"))
                and "login_user" in allowed_keys
                and "login_pass" in allowed_keys
            ),
            "auth_mode": meta.get("auth"),
            "has_web_ui": bool(meta.get("has_web_ui")),
            "chat_compatible": bool(meta.get("chat_compatible")),
            "corrupt": bool(corrupt_error),
        })
    return items


def load_instance_state(
    service_name: str,
    instance_arg: str = "",
    known_services: set[str] | None = None,
) -> tuple[str, str, Path, dict[str, Any]]:
    inst, session_name, inst_dir = resolve_instance_dir(service_name, instance_arg, known_services)
    st = _load_state_json(inst_dir / "state.json")
    return inst, session_name, inst_dir, st


def reveal_secrets(
    service_name: str,
    instance_arg: str = "",
    catalog: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    known = set(catalog.keys()) if catalog is not None else None
    inst, session_name, _, st = load_instance_state(service_name, instance_arg, known)
    if not st:
        raise UsageError(f"未找到实例状态：{session_name}")
    payload: dict[str, Any] = {
        "service": service_name,
        "instance": inst or "default",
        "instance_arg": inst,
        "session_name": session_name,
        "url": st.get("url"),
        "auth_mode": (catalog or {}).get(service_name, {}).get("auth"),
    }
    allowed_keys = _allowed_secret_keys(service_name, payload.get("auth_mode"))
    for key in allowed_keys:
        val = st.get(key)
        if val:
            payload[key] = val
    return payload


def read_log_tail(
    service_name: str,
    instance_arg: str = "",
    lines: int = 200,
    known_services: set[str] | None = None,
) -> dict[str, Any]:
    lines = max(1, min(int(lines), 2000))
    inst, session_name, inst_dir = resolve_instance_dir(service_name, instance_arg, known_services)
    log_path = inst_dir / "run.log"
    if not log_path.is_file():
        return {
            "service": service_name,
            "instance": inst or "default",
            "session_name": session_name,
            "exists": False,
            "log_path": str(log_path),
            "size": 0,
            "content": "",
        }
    size = log_path.stat().st_size
    # Read a bounded tail (up to 512 KiB) so giant logs never overwhelm memory.
    read_bytes = min(size, 512 * 1024)
    with log_path.open("rb") as handle:
        handle.seek(max(0, size - read_bytes))
        raw = handle.read()
    text = strip_ansi(raw.decode("utf-8", errors="replace"))
    tail_lines = text.splitlines()[-lines:]
    return {
        "service": service_name,
        "instance": inst or "default",
        "session_name": session_name,
        "exists": True,
        "log_path": str(log_path),
        "size": size,
        "content": "\n".join(tail_lines),
    }


def accounts_overview(default_account: str | None = None) -> dict[str, Any]:
    registry = None
    error_msg = None
    try:
        registry = load_accounts_registry()
    except Exception as err:
        error_msg = str(err)

    names = list_account_names()
    effective_account = None
    effective_source = "none"
    effective_drive = None
    drive_source = "none"
    if not error_msg:
        with contextlib.suppress(Exception):
            effective_account, effective_source = resolve_effective_account(
                explicit=default_account,
                registry=registry,
            )
            effective_drive, drive_source = resolve_effective_drive_account(
                effective_account,
                registry=registry,
            )

    accounts_meta = (registry.get("accounts") or {}) if isinstance(registry, dict) else {}
    active_name = str(registry.get("active") or "").strip() if isinstance(registry, dict) else ""
    default_drv = str(registry.get("default_drive_account") or "").strip() if isinstance(registry, dict) else ""

    items: list[dict[str, Any]] = []
    for name in names:
        rec = accounts_meta.get(name) if isinstance(accounts_meta.get(name), dict) else {}
        bound_drive = str(rec.get("drive_account") or "").strip() or None
        creds_path = account_dfs_credentials_path(name, bound_drive or name, registry=registry)
        items.append({
            "name": name,
            "email": rec.get("email"),
            "active": name == active_name,
            "effective": name == effective_account,
            "drive_account": bound_drive,
            "has_drive_consent": creds_path.is_file(),
        })

    legacy_consent = False
    if not items:
        with contextlib.suppress(Exception):
            legacy_consent = account_dfs_credentials_path(None, None, registry=None).is_file()

    return {
        "multi_account_initialized": registry is not None,
        "active_account": active_name or None,
        "default_drive_account": default_drv or None,
        "effective_account": effective_account,
        "effective_source": effective_source,
        "effective_drive_account": effective_drive,
        "drive_source": drive_source,
        "legacy_drive_consent": legacy_consent,
        "accounts": items,
        "error": error_msg,
    }


def _load_tunnel_config_dynamic() -> tuple[Path, dict[str, Any]]:
    path = user_home() / "tunnel-config.json"
    if not path.exists():
        return path, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as err:
        raise RuntimeError(f"配置文件损坏：{path}；请修复该 JSON 文件或从备份恢复") from err
    if not isinstance(data, dict):
        raise ValueError("tunnel-config.json 必须是 JSON 对象")
    if data.get("base_domain"):
        data["base_domain"] = validate_domain(data["base_domain"])
    if data.get("frp") is not None:
        data["frp"] = validate_frp_config(data["frp"])
    return path, data


def tunnel_overview() -> dict[str, Any]:
    path, saved = _load_tunnel_config_dynamic()
    frp_cfg = saved.get("frp") if isinstance(saved.get("frp"), dict) else None
    env_override = os.environ.get("COLABSVC_BASE_DOMAIN") or None
    return {
        "base_domain": saved.get("base_domain"),
        "transport": "frp" if (frp_cfg or {}).get("enabled") else "cloudflare",
        "frp": frp_cfg,
        "environment_override": env_override,
        "config_file": str(path),
    }


def update_tunnel_config(*, base_domain: str | None = None, clear: bool = False) -> dict[str, Any]:
    path, saved = _load_tunnel_config_dynamic()
    if clear and base_domain:
        raise UsageError("clear 与 base_domain 不能同时指定")
    if clear:
        if (saved.get("frp") or {}).get("enabled"):
            raise UsageError("已启用 FRP 时必须保留全局主域名；请先禁用 FRP 再清除")
        saved.pop("base_domain", None)
    elif base_domain is not None:
        cleaned = str(base_domain).strip()
        if not cleaned:
            raise UsageError("主域名不能为空；如需清除请使用清除操作")
        try:
            saved["base_domain"] = validate_domain(cleaned)
        except ValueError as err:
            raise UsageError(str(err)) from None

    if saved:
        write_private(path, saved)
    else:
        path.unlink(missing_ok=True)
    return tunnel_overview()
