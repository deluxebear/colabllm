"""Static runtime capability & security policy catalog for colabllm gateway."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Literal, Mapping

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from ollama_runtime import parse_compat_apis  # noqa: E402


@dataclass(frozen=True)
class RuntimeGatewaySpec:
    gateway_enabled: bool = False
    mode: Literal["chat_only", "multimodal"] = "chat_only"
    allowed_v1_routes: tuple[tuple[str, str], ...] = ()
    allowed_v1_prefixes: tuple[tuple[str, str], ...] = ()
    prefixed_only_routes: tuple[tuple[str, str], ...] = ()
    media_fields: tuple[str, ...] = ()
    check_bare_url_on_images_api: bool = False
    check_file_on_asr_api: bool = False
    drive_forbidden_fields: tuple[str, ...] = ()
    default_slot_capacity: int = 1
    slot_capacity_env: str | None = None
    rewrite_batch_items_model: bool = False
    forbid_ytdlp_urls: bool = False
    reject_url_response_format: bool = False
    stateless_responses_only: bool = False


_LLAMA_ROUTES = (
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/responses"),
    ("POST", "/v1/completions"),
    ("POST", "/v1/messages"),
)
_LLAMA_PREFIXES = (("GET", "/v1/models/"),)
_LLAMA_MEDIA = ("image_url", "audio_url", "source.url")
_LLAMA_DRIVE_FORBIDDEN = ("save_to_drive", "save_drive_path", "drive_only", "lora", "lora_path")

_IMAGE_GEN_ROUTES = (
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/images/generations"),
    ("POST", "/v1/images/edits"),
)
_IMAGE_GEN_MEDIA = ("image", "images", "image_url", "source.url")
_IMAGE_DRIVE_FORBIDDEN = (
    "save_to_drive",
    "save_drive_path",
    "drive_only",
    "lora",
    "lora_path",
    "drive_image",
    "drive_images",
)

GATEWAY_RUNTIME_SPECS: dict[str, RuntimeGatewaySpec] = {
    "qwen-flash": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="chat_only",
        allowed_v1_routes=_LLAMA_ROUTES,
        allowed_v1_prefixes=_LLAMA_PREFIXES,
        media_fields=_LLAMA_MEDIA,
        drive_forbidden_fields=_LLAMA_DRIVE_FORBIDDEN,
        default_slot_capacity=1,
        slot_capacity_env="CT_QWEN_PARALLEL",
        stateless_responses_only=True,
    ),
    "deepseek-coder": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="chat_only",
        allowed_v1_routes=_LLAMA_ROUTES,
        allowed_v1_prefixes=_LLAMA_PREFIXES,
        media_fields=_LLAMA_MEDIA,
        drive_forbidden_fields=_LLAMA_DRIVE_FORBIDDEN,
        default_slot_capacity=1,
        slot_capacity_env="CT_DS_PARALLEL",
        stateless_responses_only=True,
    ),
    "deepseek-qwen-9b": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="chat_only",
        allowed_v1_routes=_LLAMA_ROUTES,
        allowed_v1_prefixes=_LLAMA_PREFIXES,
        media_fields=_LLAMA_MEDIA,
        drive_forbidden_fields=_LLAMA_DRIVE_FORBIDDEN,
        default_slot_capacity=1,
        slot_capacity_env="CT_DS9B_PARALLEL",
        stateless_responses_only=True,
    ),
    "ollama": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="multimodal",
        allowed_v1_routes=(
            ("GET", "/v1/models"),
            ("POST", "/v1/chat/completions"),
            ("POST", "/v1/completions"),
            ("POST", "/v1/responses"),
            ("POST", "/v1/embeddings"),
            ("POST", "/v1/audio/speech"),
            ("POST", "/v1/audio/transcriptions"),
            ("POST", "/v1/audio/translations"),
            ("POST", "/v1/messages"),
        ),
        allowed_v1_prefixes=(
            ("GET", "/v1/models/"),
            ("POST", "/v1/messages/"),
        ),
        media_fields=("image_url", "audio_url", "images", "source.url"),
        check_file_on_asr_api=True,
        drive_forbidden_fields=_LLAMA_DRIVE_FORBIDDEN,
        default_slot_capacity=1,
        slot_capacity_env="CT_OLLAMA_SERVER_ENV:OLLAMA_NUM_PARALLEL",
    ),
    "flux-klein": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="multimodal",
        allowed_v1_routes=_IMAGE_GEN_ROUTES,
        allowed_v1_prefixes=(("GET", "/v1/models/"),),
        media_fields=_IMAGE_GEN_MEDIA,
        check_bare_url_on_images_api=True,
        drive_forbidden_fields=_IMAGE_DRIVE_FORBIDDEN,
        default_slot_capacity=1,
        reject_url_response_format=True,
    ),
    "qwen-image": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="multimodal",
        allowed_v1_routes=_IMAGE_GEN_ROUTES,
        allowed_v1_prefixes=(("GET", "/v1/models/"),),
        media_fields=_IMAGE_GEN_MEDIA,
        check_bare_url_on_images_api=True,
        drive_forbidden_fields=_IMAGE_DRIVE_FORBIDDEN,
        default_slot_capacity=1,
        reject_url_response_format=True,
    ),
    "minimax-h3-image": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="multimodal",
        allowed_v1_routes=(
            ("GET", "/v1/models"),
            ("POST", "/v1/images/generations"),
            ("POST", "/v1/images/edits"),
        ),
        allowed_v1_prefixes=(("GET", "/v1/models/"),),
        media_fields=("image", "images", "image_url"),
        check_bare_url_on_images_api=True,
        drive_forbidden_fields=(
            "save_to_drive",
            "save_drive_path",
            "drive_only",
            "drive_image",
            "drive_images",
        ),
        default_slot_capacity=1,
        reject_url_response_format=True,
    ),
    "qwen3-tts": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="multimodal",
        allowed_v1_routes=(
            ("GET", "/v1/models"),
            ("POST", "/v1/audio/speech"),
        ),
        allowed_v1_prefixes=(("GET", "/v1/models/"),),
        prefixed_only_routes=(("GET", "/v1/audio/voices"),),
        media_fields=("ref_audio", "audio"),
        drive_forbidden_fields=("save_to_drive", "save_drive_path", "drive_only"),
        default_slot_capacity=1,
    ),
    "indextts": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="multimodal",
        allowed_v1_routes=(
            ("GET", "/v1/models"),
            ("POST", "/v1/audio/speech"),
        ),
        allowed_v1_prefixes=(("GET", "/v1/models/"),),
        prefixed_only_routes=(("GET", "/v1/audio/voices"),),
        media_fields=("ref_audio", "emo_audio"),
        drive_forbidden_fields=("save_to_drive", "save_drive_path", "drive_only"),
        default_slot_capacity=1,
    ),
    "drama3": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="multimodal",
        allowed_v1_routes=(
            ("GET", "/v1/models"),
            ("POST", "/v1/audio/speech"),
            ("POST", "/v1/audio/speech/batch"),
        ),
        allowed_v1_prefixes=(("GET", "/v1/models/"),),
        prefixed_only_routes=(("GET", "/v1/audio/voices"),),
        media_fields=("ref_audio", "audio_path", "audio"),
        drive_forbidden_fields=("save_to_drive", "save_drive_path", "drive_only"),
        default_slot_capacity=1,
        rewrite_batch_items_model=True,
    ),
    "tangoflux": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="multimodal",
        allowed_v1_routes=(
            ("GET", "/v1/models"),
            ("POST", "/v1/audio/generations"),
            ("POST", "/v1/audio/speech"),
        ),
        allowed_v1_prefixes=(("GET", "/v1/models/"),),
        media_fields=(),
        drive_forbidden_fields=("save_to_drive", "save_drive_path", "drive_only"),
        default_slot_capacity=1,
    ),
    "nemotron-transcribe": RuntimeGatewaySpec(
        gateway_enabled=True,
        mode="multimodal",
        allowed_v1_routes=(
            ("GET", "/v1/models"),
            ("POST", "/v1/chat/completions"),
        ),
        allowed_v1_prefixes=(("GET", "/v1/models/"),),
        media_fields=("audio_url", "image_url"),
        drive_forbidden_fields=("save_to_drive", "save_drive_dir", "save_drive_path", "drive_only"),
        default_slot_capacity=1,
        forbid_ytdlp_urls=True,
    ),
    # Non-OpenAI workflow / web-UI services explicitly excluded from gateway exposure.
    "comfyui": RuntimeGatewaySpec(),
    "openjutsu": RuntimeGatewaySpec(),
    "longcat-video": RuntimeGatewaySpec(),
    "longcat-avatar": RuntimeGatewaySpec(),
    "unlimited-ocr": RuntimeGatewaySpec(),
    "r2-migrate": RuntimeGatewaySpec(),
    "unsloth-studio": RuntimeGatewaySpec(),
    "kaggle-web-probe": RuntimeGatewaySpec(),
}


def get_runtime_spec(runtime: str, specs: Mapping[str, RuntimeGatewaySpec] | None = None) -> RuntimeGatewaySpec | None:
    """Return RuntimeGatewaySpec for a runtime name, or None if unregistered."""
    table = GATEWAY_RUNTIME_SPECS if specs is None else specs
    return table.get(runtime)


def all_root_post_routes(specs: Mapping[str, RuntimeGatewaySpec] | None = None) -> tuple[set[str], tuple[str, ...]]:
    """Return exact POST paths and POST prefix paths enabled on any runtime."""
    table = GATEWAY_RUNTIME_SPECS if specs is None else specs
    exact: set[str] = set()
    prefixes: list[str] = []
    for spec in table.values():
        if not spec.gateway_enabled:
            continue
        for method, route in spec.allowed_v1_routes:
            if method == "POST":
                exact.add(route)
        for method, prefix in spec.allowed_v1_prefixes:
            if method == "POST" and prefix not in prefixes:
                prefixes.append(prefix)
    return exact, tuple(prefixes)


def parse_slot_capacity(spec: RuntimeGatewaySpec, env: dict[str, Any] | None) -> int:
    """Resolve effective slot capacity from state['env'], falling back on invalid or non-positive values."""
    if not isinstance(env, dict) or not spec.slot_capacity_env:
        return spec.default_slot_capacity
    raw_val: Any = None
    if ":" in spec.slot_capacity_env:
        container_key, inner_key = spec.slot_capacity_env.split(":", 1)
        container_raw = env.get(container_key)
        if isinstance(container_raw, dict):
            raw_val = container_raw.get(inner_key)
        elif isinstance(container_raw, str) and container_raw.strip():
            try:
                parsed = json.loads(container_raw)
            except (ValueError, TypeError):
                return spec.default_slot_capacity
            if isinstance(parsed, dict):
                raw_val = parsed.get(inner_key)
    else:
        raw_val = env.get(spec.slot_capacity_env)

    if raw_val is None or isinstance(raw_val, bool):
        return spec.default_slot_capacity
    try:
        parsed_int = int(str(raw_val).strip()) if isinstance(raw_val, str) else int(raw_val)
    except (ValueError, TypeError):
        return spec.default_slot_capacity
    return parsed_int if parsed_int > 0 else spec.default_slot_capacity


def ollama_active_compat(env: dict[str, Any] | None) -> tuple[str, ...]:
    """Return normalized CT_OLLAMA_COMPAT tuple for an ollama instance."""
    raw = "all"
    if isinstance(env, dict) and env.get("CT_OLLAMA_COMPAT"):
        raw = str(env.get("CT_OLLAMA_COMPAT"))
    try:
        return parse_compat_apis(raw)
    except ValueError:
        return ()


def ollama_is_gateway_compatible(env: dict[str, Any] | None) -> bool:
    """Return True if an ollama instance exposes openai or anthropic compatible endpoints."""
    compat = ollama_active_compat(env)
    return ("openai" in compat) or ("anthropic" in compat)


def runtime_allows_v1_route(
    runtime: str,
    spec: RuntimeGatewaySpec,
    method: str,
    normalized_v1_path: str,
    *,
    env: dict[str, Any] | None = None,
) -> bool:
    """Check whether (method, normalized_v1_path) is allowed for runtime and optional instance env."""
    if not spec.gateway_enabled:
        return False
    routes = spec.allowed_v1_routes
    prefixes = spec.allowed_v1_prefixes
    if runtime == "ollama" and env is not None:
        compat = ollama_active_compat(env)
        allowed_r: list[tuple[str, str]] = []
        allowed_p: list[tuple[str, str]] = []
        if "openai" in compat or "anthropic" in compat:
            allowed_r.append(("GET", "/v1/models"))
            allowed_p.append(("GET", "/v1/models/"))
        if "openai" in compat:
            allowed_r.extend([
                ("POST", "/v1/chat/completions"),
                ("POST", "/v1/completions"),
                ("POST", "/v1/responses"),
                ("POST", "/v1/embeddings"),
                ("POST", "/v1/audio/speech"),
                ("POST", "/v1/audio/transcriptions"),
                ("POST", "/v1/audio/translations"),
            ])
        if "anthropic" in compat:
            allowed_r.append(("POST", "/v1/messages"))
            allowed_p.append(("POST", "/v1/messages/"))
        routes = tuple(allowed_r)
        prefixes = tuple(allowed_p)
    if (method, normalized_v1_path) in routes:
        return True
    return any(method == m and normalized_v1_path.startswith(prefix) for m, prefix in prefixes)
