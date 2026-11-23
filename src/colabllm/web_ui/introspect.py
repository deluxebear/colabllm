"""Offline metadata and argparse schema introspection for all ColabLLM services."""
from __future__ import annotations

import argparse
import copy
from pathlib import Path
import re
import runpy
import shlex
import threading
from typing import Any

from cli_support import UsageError, duration_seconds, nonnegative_seconds, positive_seconds
from colab_accounts import validate_account
from colabsvc import build_parser, nonnegative_int, validate_domain, validate_instance
from instance_discovery import discover_all_provider_services, parse_catalog_name
from kaggle_accounts import validate_kaggle_account
from kagglesvc import (
    build_parser as build_kaggle_parser,
    validate_kaggle_instance,
    validate_kaggle_session_name,
)

from .api_catalog import CAPABILITY_SURFACES, build_api_profile

PKG_ROOT = Path(__file__).resolve().parents[1]
SERVICES_DIR = PKG_ROOT / "services"
KAGGLE_SERVICES_DIR = PKG_ROOT / "kaggle_services"

_CATEGORIES: dict[str, set[str]] = {
    "llm": {"qwen-flash", "deepseek-coder", "deepseek-qwen-9b", "ollama"},
    "image": {"minimax-h3-image", "flux-klein", "qwen-image"},
    "audio": {"qwen3-tts", "indextts", "drama3", "tangoflux", "nemotron-transcribe"},
    "video": {"comfyui", "openjutsu", "longcat-video", "longcat-avatar", "unsloth-studio"},
    "ocr": {"unlimited-ocr", "oxr"},
}

CATEGORY_LABELS: dict[str, str] = {
    "llm": "LLM 与推理",
    "image": "图像生成与编辑",
    "audio": "语音合成 / 转写 / 音效",
    "video": "视频与原生工作流 UI",
    "ocr": "文档 OCR",
    "other": "其他运行时",
}

LLAMA_SERVER_SERVICES = {"qwen-flash", "deepseek-coder", "deepseek-qwen-9b"}
WEB_UI_SERVICES = {
    "comfyui",
    "openjutsu",
    "longcat-video",
    "unsloth-studio",
    "unlimited-ocr",
    "oxr",
    "r2-migrate",
    "kaggle-web-probe",
    *LLAMA_SERVER_SERVICES,
}
CHAT_SERVICES = {*LLAMA_SERVER_SERVICES, "ollama"}

STANDARD_UP_DESTS = {
    "help",
    "account",
    "drive_account",
    "domain",
    "base_domain",
    "quick_tunnel",
    "transport",
    "gpu",
    "skip",
    "no_drive",
    "hf_token",
    "new_credentials",
    "foreground",
    "detach",
    "auto_restart",
    "restart_delay",
    "idle_check",
    "max_restarts",
    "ready_timeout",
    "dry_run",
}

KAGGLE_UP_DESTS = {
    "help",
    "account",
    "domain",
    "base_domain",
    "quick_tunnel",
    "transport",
    "accelerator",
    "internet",
    "new_credentials",
    "foreground",
    "detach",
    "auto_restart",
    "restart_delay",
    "idle_check",
    "max_restarts",
    "max_uptime",
    "ready_timeout",
    "startup_timeout",
    "dry_run",
}

LIFECYCLE_SUBCOMMANDS = {
    "up",
    "restart",
    "down",
    "info",
    "ps",
    "list",
    "status",
    "logs",
    "tunnel-config",
    "_exec",
}

_SENSITIVE_FLAGS = {"--hf-token", "--api-token", "--token", "--secret-access-key", "--firecrawl-api-key"}
_SKIP_RE = re.compile(r"^[0-9]+(,[0-9]+)*$")
_GPU_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

_CACHE_LOCK = threading.Lock()
_CATALOG_CACHE: dict[str, dict[str, Any]] | None = None
_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def discover_service_paths() -> dict[str, Path]:
    specs = discover_all_provider_services(
        services_dir=SERVICES_DIR,
        kaggle_services_dir=KAGGLE_SERVICES_DIR,
    )
    return {name: spec.script_path for name, spec in specs.items()}


def _category_for(name: str) -> str:
    for cat, names in _CATEGORIES.items():
        if name in names:
            return cat
    return "other"


def _doc_summary(doc: str) -> str:
    for line in (doc or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _prefer_long_flag(option_strings: list[str]) -> str:
    for flag in option_strings:
        if flag.startswith("--"):
            return flag
    return option_strings[0] if option_strings else ""


def _json_safe_default(value: Any) -> Any:
    if value is None or value is argparse.SUPPRESS:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe_default(v) for v in value]
    return str(value)


def _action_id(action: argparse.Action, seen_ids: set[str]) -> str:
    if action.option_strings:
        base = "opt:" + _prefer_long_flag(list(action.option_strings))
    else:
        base = "pos:" + action.dest
    candidate = base
    idx = 2
    while candidate in seen_ids:
        candidate = f"{base}#{idx}"
        idx += 1
    seen_ids.add(candidate)
    return candidate


def _serialize_action(action: argparse.Action, seen_ids: set[str]) -> dict[str, Any] | None:
    if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
        return None
    if action.help is argparse.SUPPRESS:
        return None

    flags = list(action.option_strings)
    positional = not flags
    field_id = _action_id(action, seen_ids)
    cls_name = type(action).__name__
    nargs = action.nargs
    choices = [_json_safe_default(c) for c in action.choices] if action.choices else None

    if isinstance(action, argparse.BooleanOptionalAction):
        kind = "boolean_optional"
    elif isinstance(action, argparse._StoreTrueAction):
        kind = "switch"
    elif isinstance(action, argparse._StoreFalseAction):
        kind = "store_false"
    elif choices is not None and nargs not in ("+", "*"):
        kind = "select"
    elif isinstance(action, argparse._AppendAction) or cls_name == "_OrderedImageAction" or nargs in ("+", "*"):
        kind = "repeatable"
    elif not positional and nargs == "?":
        kind = "optional_value"
    elif action.type is int or action.type is nonnegative_int:
        kind = "int"
    elif action.type is float or action.type in (positive_seconds, nonnegative_seconds):
        kind = "float"
    else:
        kind = "text"

    is_secret = any(f in _SENSITIVE_FLAGS for f in flags) or action.dest in ("hf_token", "api_token", "secret_access_key")
    help_text = ""
    if isinstance(action.help, str):
        # Expand %(default)s safely if present.
        try:
            help_text = action.help % {"default": action.default}
        except Exception:
            help_text = action.help

    return {
        "id": field_id,
        "name": action.dest,
        "flags": flags,
        "primary_flag": _prefer_long_flag(flags),
        "positional": positional,
        "kind": kind,
        "action_class": cls_name,
        "required": bool(getattr(action, "required", False)) or (positional and nargs not in ("?", "*")),
        "default": _json_safe_default(action.default),
        "has_const": action.const is not None,
        "const": _json_safe_default(action.const),
        "choices": choices,
        "metavar": action.metavar if isinstance(action.metavar, str) else None,
        "nargs": nargs if isinstance(nargs, str) else (str(nargs) if nargs is not None else None),
        "help": help_text,
        "secret": is_secret,
    }


def _extract_subparser_fields(subparser: argparse.ArgumentParser, *, exclude_dests: set[str] | None = None) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for action in subparser._actions:
        if exclude_dests and not action.option_strings and action.dest in exclude_dests:
            continue
        if exclude_dests and action.option_strings and action.dest in exclude_dests:
            continue
        item = _serialize_action(action, seen_ids)
        if item is not None:
            fields.append(item)
    return fields


def _load_service_record(name: str, path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    provider, short_name = parse_catalog_name(name)
    is_kaggle = provider == "kaggle"
    module = runpy.run_path(str(path))
    svc_orig = module["SERVICE"]
    svc = copy.deepcopy(svc_orig)
    parser_builder = build_kaggle_parser if is_kaggle else build_parser
    parser = parser_builder(svc, module.get("configure_cli"))

    sub_choices: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub_choices = dict(action.choices)
            break

    up_parser = sub_choices.get("up")
    up_dests = KAGGLE_UP_DESTS if is_kaggle else STANDARD_UP_DESTS
    up_options = _extract_subparser_fields(up_parser, exclude_dests=up_dests) if up_parser else []

    subcommands: list[dict[str, Any]] = []
    for sub_name, sub_parser in sub_choices.items():
        if sub_name in LIFECYCLE_SUBCOMMANDS or sub_name.startswith("_"):
            continue
        subcommands.append({
            "name": sub_name,
            "description": (sub_parser.description or "").strip(),
            "fields": _extract_subparser_fields(sub_parser),
        })

    doc = (module.get("__doc__") or "").strip()
    cat = _category_for(name)
    subcommand_names = [s["name"] for s in subcommands]
    has_web_ui = (name in WEB_UI_SERVICES) or bool(getattr(svc_orig, "has_web_ui", False))
    api_profile = build_api_profile(
        name,
        doc=doc,
        subcommand_names=subcommand_names,
        auth=svc_orig.auth,
        has_web_ui=has_web_ui,
    )
    gpu_val = getattr(svc_orig, "gpu", None)
    tpu_val = getattr(svc_orig, "tpu", None)
    high_mem_val = bool(getattr(svc_orig, "high_mem", False))
    accelerator_val = getattr(svc_orig, "accelerator", gpu_val or tpu_val or "cpu")
    meta = {
        "name": name,
        "provider": provider,
        "short_name": short_name,
        "script_path": str(path),
        "category": cat,
        "category_label": CATEGORY_LABELS.get(cat, cat),
        "summary": _doc_summary(doc),
        "doc": doc,
        "gpu": gpu_val,
        "tpu": tpu_val,
        "high_mem": high_mem_val,
        "accelerator": accelerator_val,
        "enable_internet": bool(getattr(svc_orig, "enable_internet", True)),
        "max_uptime": getattr(svc_orig, "max_uptime", None),
        "auth": svc_orig.auth,
        "service_port": svc_orig.service_port,
        "public_port": svc_orig.public_port,
        "default_hostname": svc_orig.default_hostname,
        "ready_timeout": svc_orig.ready_timeout,
        "has_web_ui": has_web_ui,
        "chat_compatible": name in CHAT_SERVICES,
        "openai_compatible": bool(api_profile.get("openai_compatible")),
        "api_profile": api_profile,
        "subcommand_names": subcommand_names,
        "has_plan": any(s["name"] == "plan" for s in subcommands),
        "supports_restart": is_kaggle,
    }
    schema = {
        "service": meta,
        "up_options": up_options,
        "subcommands": subcommands,
    }
    return meta, schema


def get_service_catalog() -> dict[str, dict[str, Any]]:
    global _CATALOG_CACHE
    with _CACHE_LOCK:
        if _CATALOG_CACHE is not None:
            return copy.deepcopy(_CATALOG_CACHE)
        paths = discover_service_paths()
        catalog: dict[str, dict[str, Any]] = {}
        for name, path in paths.items():
            meta, schema = _load_service_record(name, path)
            catalog[name] = meta
            _SCHEMA_CACHE[name] = schema
        _CATALOG_CACHE = catalog
        return copy.deepcopy(catalog)


def get_service_schema(service_name: str) -> dict[str, Any]:
    if service_name not in discover_service_paths():
        raise UsageError(f"未知运行时服务: {service_name}")
    with _CACHE_LOCK:
        if service_name not in _SCHEMA_CACHE:
            path = discover_service_paths()[service_name]
            meta, schema = _load_service_record(service_name, path)
            _SCHEMA_CACHE[service_name] = schema
            if _CATALOG_CACHE is not None:
                _CATALOG_CACHE[service_name] = meta
        return copy.deepcopy(_SCHEMA_CACHE[service_name])


def format_command_preview(service_name: str, argv: list[str], secret_indices: set[int] | None = None) -> str:
    redacted = list(argv)
    for idx in secret_indices or ():
        if 0 <= idx < len(redacted):
            redacted[idx] = "***"
    provider, short_name = parse_catalog_name(service_name)
    prefix = ["colabllm", "kaggle", short_name] if provider == "kaggle" else ["colabllm", service_name]
    return " ".join([*prefix, *(shlex.quote(arg) for arg in redacted)])


def _append_field_argv(
    field: dict[str, Any],
    raw_value: Any,
    opt_argv: list[str],
    pos_argv: list[str],
    secret_indices: set[int],
) -> None:
    kind = field["kind"]
    positional = field["positional"]
    flag = field["primary_flag"]
    is_secret = bool(field.get("secret"))

    if kind in ("switch", "store_false"):
        if bool(raw_value) and flag:
            opt_argv.append(flag)
        return

    if kind == "boolean_optional":
        flags = field.get("flags") or []
        pos_flag = flags[0] if flags else flag
        neg_flag = flags[1] if len(flags) > 1 else None
        if raw_value is True or raw_value == "true":
            if pos_flag:
                opt_argv.append(pos_flag)
        elif (raw_value is False or raw_value == "false") and neg_flag:
            opt_argv.append(neg_flag)
        return

    if kind == "optional_value":
        enabled = False
        text_val = ""
        if isinstance(raw_value, dict):
            enabled = bool(raw_value.get("enabled"))
            text_val = str(raw_value.get("value") or "").strip()
        elif isinstance(raw_value, bool):
            enabled = raw_value
        elif raw_value is not None and str(raw_value).strip():
            enabled = True
            text_val = str(raw_value).strip()
        if enabled and flag:
            if text_val:
                opt_argv.append(f"{flag}={text_val}")
                if is_secret:
                    secret_indices.add(len(opt_argv) - 1)
            else:
                opt_argv.append(flag)
        return

    if kind == "repeatable":
        if raw_value is None or raw_value == "":
            items: list[str] = []
        elif isinstance(raw_value, list):
            items = [str(v).strip() for v in raw_value if str(v).strip()]
        else:
            items = [line.strip() for line in str(raw_value).splitlines() if line.strip()]
        if not items:
            if field.get("required") and positional:
                raise UsageError(f"缺少必填参数: {field['name']}")
            return
        if positional:
            pos_argv.extend(items)
        elif field.get("action_class") in ("_AppendAction", "_OrderedImageAction"):
            for item in items:
                opt_argv.append(flag)
                opt_argv.append(item)
                if is_secret:
                    secret_indices.add(len(opt_argv) - 1)
        else:
            opt_argv.append(flag)
            for item in items:
                opt_argv.append(item)
                if is_secret:
                    secret_indices.add(len(opt_argv) - 1)
        return

    # Scalar fields: text, select, int, float
    if raw_value is None or str(raw_value).strip() == "":
        if field.get("required"):
            raise UsageError(f"缺少必填参数: {field.get('primary_flag') or field['name']}")
        return
    val_str = str(raw_value).strip()
    if kind == "int":
        try:
            int(val_str)
        except ValueError:
            raise UsageError(f"参数 {field.get('primary_flag') or field['name']} 需要整数") from None
    elif kind == "float":
        try:
            float(val_str)
        except ValueError:
            raise UsageError(f"参数 {field.get('primary_flag') or field['name']} 需要数字") from None
    elif kind == "select" and field.get("choices"):
        allowed = {str(c) for c in field["choices"]}
        if val_str not in allowed:
            raise UsageError(f"参数 {field.get('primary_flag') or field['name']} 取值无效: {val_str}")

    if positional:
        pos_argv.append(val_str)
    else:
        opt_argv.append(flag)
        opt_argv.append(val_str)
        if is_secret:
            secret_indices.add(len(opt_argv) - 1)


def _validate_provider_instance(provider: str, short_name: str, raw_instance: str) -> str:
    if provider == "kaggle":
        validated = validate_kaggle_instance(raw_instance)
        validate_kaggle_session_name(f"{short_name}-{validated}")
        return validated
    return validate_instance(raw_instance)


def _validate_provider_account(provider: str, raw_account: str) -> str:
    if provider == "kaggle":
        return validate_kaggle_account(raw_account)
    return validate_account(raw_account)


def build_up_argv(service_name: str, payload: dict[str, Any]) -> tuple[list[str], str]:
    schema = get_service_schema(service_name)
    provider, short_name = parse_catalog_name(service_name)
    is_kaggle = provider == "kaggle"
    argv: list[str] = []
    secret_indices: set[int] = set()

    inst = str(payload.get("instance") or "").strip()
    if inst:
        argv.extend(["-i", _validate_provider_instance(provider, short_name, inst)])
    account = str(payload.get("account") or "").strip()
    if account:
        argv.extend(["--account", _validate_provider_account(provider, account)])

    argv.append("up")

    drive_account = str(payload.get("drive_account") or "").strip()
    if drive_account:
        if is_kaggle:
            raise UsageError("Kaggle 服务不支持 --drive-account")
        argv.extend(["--drive-account", validate_account(drive_account)])

    tunnel_mode = str(payload.get("tunnel_mode") or "default").strip()
    if tunnel_mode == "quick":
        argv.append("--quick-tunnel")
    elif tunnel_mode == "base_domain":
        base_dom = str(payload.get("base_domain") or "").strip()
        if not base_dom:
            raise UsageError("选择单次主域名模式时必须填写主域名")
        argv.extend(["--base-domain", validate_domain(base_dom)])
    elif tunnel_mode == "domain":
        dom = str(payload.get("domain") or "").strip()
        if dom:
            argv.append(f"--domain={validate_domain(dom)}")
        else:
            argv.append("--domain")
    elif tunnel_mode != "default":
        raise UsageError(f"未知隧道模式: {tunnel_mode}")

    transport = str(payload.get("transport") or "").strip()
    if transport:
        if transport not in ("cloudflare", "frp"):
            raise UsageError(f"不支持的隧道后端: {transport}")
        argv.extend(["--transport", transport])

    gpu = str(payload.get("gpu") or "").strip()
    if gpu:
        if is_kaggle:
            raise UsageError("Kaggle 服务请使用 --accelerator 而非 --gpu")
        if not _GPU_RE.fullmatch(gpu):
            raise UsageError("GPU 参数格式无效")
        argv.extend(["--gpu", gpu])

    accelerator = str(payload.get("accelerator") or "").strip()
    if accelerator:
        if not is_kaggle:
            raise UsageError("Colab 服务不支持 --accelerator")
        if not _GPU_RE.fullmatch(accelerator):
            raise UsageError("accelerator 参数格式无效")
        argv.extend(["--accelerator", accelerator])

    internet = payload.get("internet")
    if internet is not None and internet != "":
        if not is_kaggle:
            raise UsageError("Colab 服务不支持 --internet/--no-internet")
        if internet is True or str(internet).strip().lower() == "true":
            argv.append("--internet")
        elif internet is False or str(internet).strip().lower() == "false":
            argv.append("--no-internet")
        else:
            raise UsageError(f"internet 参数取值无效: {internet}")

    skip = str(payload.get("skip") or "").strip()
    if skip:
        if is_kaggle:
            raise UsageError("Kaggle 服务不支持 --skip")
        if not _SKIP_RE.fullmatch(skip):
            raise UsageError("--skip 须为逗号分隔的 cell 序号，例如 1,2")
        argv.extend(["--skip", skip])

    if payload.get("no_drive"):
        if is_kaggle:
            raise UsageError("Kaggle 服务不支持 --no-drive")
        argv.append("--no-drive")

    hf_token = str(payload.get("hf_token") or "").strip()
    if hf_token:
        if is_kaggle:
            raise UsageError("Kaggle 服务不支持 --hf-token")
        argv.append("--hf-token")
        argv.append(hf_token)
        secret_indices.add(len(argv) - 1)

    if payload.get("new_credentials"):
        argv.append("--new-credentials")

    if payload.get("auto_restart"):
        argv.append("--auto-restart")

    for key, flag in (("restart_delay", "--restart-delay"), ("idle_check", "--idle-check"), ("ready_timeout", "--ready-timeout")):
        raw = payload.get(key)
        if raw is not None and str(raw).strip() != "":
            val = positive_seconds(str(raw).strip())
            argv.extend([flag, f"{val:g}"])

    raw_startup_timeout = payload.get("startup_timeout")
    if raw_startup_timeout is not None and str(raw_startup_timeout).strip() != "":
        if not is_kaggle:
            raise UsageError("Colab 服务不支持 --startup-timeout")
        val = positive_seconds(str(raw_startup_timeout).strip())
        argv.extend(["--startup-timeout", f"{val:g}"])

    raw_max_uptime = payload.get("max_uptime")
    if raw_max_uptime is not None and str(raw_max_uptime).strip() != "":
        if not is_kaggle:
            raise UsageError("Colab 服务不支持 --max-uptime")
        uptime_text = str(raw_max_uptime).strip()
        try:
            duration_seconds(uptime_text)
        except ValueError as exc:
            raise UsageError(str(exc)) from None
        argv.extend(["--max-uptime", uptime_text])

    max_restarts = payload.get("max_restarts")
    if max_restarts is not None and str(max_restarts).strip() != "":
        val = nonnegative_int(str(max_restarts).strip())
        if is_kaggle or val > 0:
            argv.extend(["--max-restarts", str(val)])

    extra = payload.get("extra") or {}
    if not isinstance(extra, dict):
        raise UsageError("extra 必须是对象")
    extra_opt_argv: list[str] = []
    extra_pos_argv: list[str] = []
    extra_secrets: set[int] = set()
    up_fields_by_id = {f["id"]: f for f in schema["up_options"]}
    up_fields_by_name = {f["name"]: f for f in schema["up_options"]}
    for key, val in extra.items():
        field = up_fields_by_id.get(key) or up_fields_by_name.get(key)
        if field is None:
            raise UsageError(f"{service_name} up 不支持参数: {key}")
        _append_field_argv(field, val, extra_opt_argv, extra_pos_argv, extra_secrets)
    base_len = len(argv)
    shifted_secrets = set(secret_indices) | {base_len + idx for idx in extra_secrets}
    argv.extend(extra_opt_argv)

    dry_run = bool(payload.get("dry_run"))
    argv.append("--dry-run" if dry_run else "--detach")

    preview = format_command_preview(service_name, argv, shifted_secrets)
    return argv, preview


def build_cli_argv(
    service_name: str,
    subcommand: str,
    *,
    instance: str | None = None,
    account: str | None = None,
    values: dict[str, Any] | None = None,
) -> tuple[list[str], str]:
    schema = get_service_schema(service_name)
    provider, short_name = parse_catalog_name(service_name)
    sub_map = {item["name"]: item for item in schema["subcommands"]}
    if subcommand not in sub_map:
        raise UsageError(f"{service_name} 不支持子命令: {subcommand}")

    argv: list[str] = []
    if instance and str(instance).strip():
        argv.extend(["-i", _validate_provider_instance(provider, short_name, str(instance).strip())])
    if account and str(account).strip():
        argv.extend(["--account", _validate_provider_account(provider, str(account).strip())])
    argv.append(subcommand)

    val_map = values or {}
    if not isinstance(val_map, dict):
        raise UsageError("values 必须是对象")

    fields = sub_map[subcommand]["fields"]
    fields_by_id = {f["id"]: f for f in fields}
    fields_by_name = {f["name"]: f for f in fields}
    provided_keys = set(val_map.keys())
    for key in provided_keys:
        if key not in fields_by_id and key not in fields_by_name:
            raise UsageError(f"{service_name} {subcommand} 不支持参数: {key}")

    opt_argv: list[str] = []
    pos_argv: list[str] = []
    opt_secrets: set[int] = set()
    for field in fields:
        if field["id"] in val_map:
            raw = val_map[field["id"]]
        elif field["name"] in val_map:
            raw = val_map[field["name"]]
        else:
            raw = None
        _append_field_argv(field, raw, opt_argv, pos_argv, opt_secrets)

    prefix_len = len(argv)
    secret_indices = {prefix_len + idx for idx in opt_secrets}
    argv.extend(opt_argv)
    if pos_argv:
        if any(item.startswith("-") for item in pos_argv):
            argv.append("--")
        argv.extend(pos_argv)

    preview = format_command_preview(service_name, argv, secret_indices)
    return argv, preview
