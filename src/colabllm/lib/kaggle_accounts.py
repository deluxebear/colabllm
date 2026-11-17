"""Read-only helpers for kaggle-ext multi-account state (Python stdlib only)."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
from typing import Mapping

from cli_support import CliError, UsageError

KAGGLE_ACCOUNT_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
_UNSET = object()


def kaggle_ext_config_dir() -> Path:
    explicit = os.environ.get("KAGGLE_EXT_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "kaggle-ext"


def validate_kaggle_account(value: str) -> str:
    val = value.strip() if isinstance(value, str) else ""
    if not val or ".." in val or "/" in val or "\\" in val or "\x00" in val or not KAGGLE_ACCOUNT_RE.fullmatch(val):
        raise UsageError("Kaggle 账户标识须为 1–64 位字母、数字、.、_、@ 或 -，不能包含 '..' 或路径分隔符")
    return val


def load_kaggle_accounts_registry() -> dict | None:
    path = kaggle_ext_config_dir() / "accounts.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise CliError(f"Kaggle 账户配置损坏：{path}；请运行 kaggle account list --json 检查或修复") from error
    if not isinstance(data, dict) or not isinstance(data.get("accounts", {}), dict):
        raise CliError(f"Kaggle 账户配置格式无效：{path}；accounts 必须是对象")
    return data


def resolve_kaggle_account_identifier(identifier: str, registry: dict | None = None) -> str:
    ident = validate_kaggle_account(identifier)
    if registry is None:
        return ident
    accounts = registry.get("accounts") or {}
    if ident in accounts:
        return ident
    target_user = ident.lstrip("@").lower()
    matches = [
        name
        for name, rec in accounts.items()
        if isinstance(rec, dict) and str(rec.get("username") or "").strip().lstrip("@").lower() == target_user
    ]
    if len(matches) == 1:
        return validate_kaggle_account(matches[0])
    if len(matches) > 1:
        raise UsageError(f"Kaggle 用户名 '{ident}' 匹配多个账户 ({', '.join(sorted(matches))})；请直接指定账户名")
    available = ", ".join(sorted(accounts)) or "(无)"
    raise UsageError(
        f"未找到 Kaggle 账户 '{ident}'；可用账户：{available}（使用 kaggle account list 查看或 kaggle account login 添加）"
    )


def list_kaggle_account_names() -> list[str]:
    """Return candidate Kaggle account names offline without reading credentials."""
    path = kaggle_ext_config_dir() / "accounts.json"
    if not path.is_file():
        return []
    with contextlib.suppress(OSError, UnicodeError, ValueError):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
            return sorted(
                key
                for key in data["accounts"]
                if isinstance(key, str) and ".." not in key and "/" not in key and "\\" not in key and KAGGLE_ACCOUNT_RE.fullmatch(key)
            )
    return []


def resolve_effective_kaggle_account(
    *,
    explicit: str | None = None,
    saved: str | None = None,
    prefer_saved: bool = False,
    registry: dict | None = _UNSET,
) -> tuple[str | None, str]:
    reg = load_kaggle_accounts_registry() if registry is _UNSET else registry
    if explicit:
        return resolve_kaggle_account_identifier(explicit, reg), "flag"

    if prefer_saved and saved:
        return resolve_kaggle_account_identifier(saved, reg), "state"

    colabllm_env = os.environ.get("COLABLLM_KAGGLE_ACCOUNT", "").strip()
    if colabllm_env:
        return resolve_kaggle_account_identifier(colabllm_env, reg), "env"

    kaggle_env = os.environ.get("KAGGLE_ACCOUNT", "").strip()
    if kaggle_env:
        return resolve_kaggle_account_identifier(kaggle_env, reg), "kaggle_env"

    if not prefer_saved and saved:
        return validate_kaggle_account(saved), "state"

    if reg is not None:
        accounts = reg.get("accounts") or {}
        active = str(reg.get("active") or "").strip()
        if active and (active in accounts or not accounts):
            return validate_kaggle_account(active), "active"

    return None, "none"


def kaggle_subprocess_env(
    account: str | None = None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct clean subprocess env for `kaggle` CLI, stripping conflicting legacy auth vars."""
    env = dict(os.environ if base_env is None else base_env)
    for conflicting in ("KAGGLE_USERNAME", "KAGGLE_KEY", "KAGGLE_API_TOKEN", "KAGGLE_CONFIG_DIR", "KAGGLE_ACCOUNT"):
        env.pop(conflicting, None)
    env["PYTHONUNBUFFERED"] = "1"
    env["BROWSER"] = "true"
    env["NO_COLOR"] = "1"
    if account:
        env["KAGGLE_ACCOUNT"] = validate_kaggle_account(account)
    return env
