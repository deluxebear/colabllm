"""Authenticated ASGI middleware, Google Drive image/LoRA staging, and RGBA/edit routing for Qwen-Image-2.1."""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
from email.parser import BytesParser
from email.policy import default as email_default_policy
import glob as glob_mod
import hashlib
from http import HTTPStatus
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from typing import Any
import urllib.error
from urllib.parse import parse_qs
import urllib.request

from colab_asgi_common import (
    DEFAULT_SKIP_DIRS,
    allowed_drive_roots,
    rel_to_mydrive,
    resolve_drive_path,
    sanitize_header_param,
    tail_service_log,
    verify_bearer_auth,
)

try:
    from starlette.datastructures import Headers
    from starlette.responses import JSONResponse, Response
    from starlette.types import ASGIApp, Message, Receive, Scope, Send
except ImportError:  # pragma: no cover - local unit-test fallback without starlette
    ASGIApp = Receive = Scope = Send = Message = Any  # type: ignore[assignment,misc]

    class Headers:  # type: ignore[no-redef]
        def __init__(self, scope: dict[str, Any]) -> None:
            self._d = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}

        def get(self, key: str, default: str = "") -> str:
            return self._d.get(key.lower(), default)

    class Response:  # type: ignore[no-redef]
        def __init__(self, content: bytes = b"", status_code: int = 200, media_type: str | None = None) -> None:
            self.body = content
            self.status_code = int(status_code)
            self.media_type = media_type

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            hdrs = [(b"content-length", str(len(self.body)).encode("ascii"))]
            if self.media_type:
                hdrs.append((b"content-type", self.media_type.encode("ascii")))
            await send({"type": "http.response.start", "status": self.status_code, "headers": hdrs})
            await send({"type": "http.response.body", "body": self.body, "more_body": False})

    class JSONResponse(Response):  # type: ignore[no-redef]
        def __init__(self, content: Any, status_code: int = 200) -> None:
            super().__init__(json.dumps(content).encode("utf-8"), status_code=status_code, media_type="application/json")

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif")
LORA_EXTS = (".safetensors", ".pt", ".bin")
MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}
EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

# Qwen-Image-2.1 supports 1-4 reference images (<image1> .. <image4>); a 5th is rejected with 400.
MAX_REFERENCE_IMAGES = 4
DIM_ALIGNMENT = 32
DEFAULT_VARIANT = "official"

MODEL_VARIANTS: dict[str, dict[str, Any]] = {
    "official": {
        "label": "Official Qwen-Image-2.1 (BF16)",
        "description": (
            "Alibaba Qwen/Qwen-Image-2.1 official checkpoint "
            "(40 steps, true_cfg_scale=1.0 / 4.0 with negative prompt, VAE tiling enabled)"
        ),
        "base_repo": "Qwen/Qwen-Image-2.1",
        "alias": "qwen-image-2.1",
        "transformer_repo": "",
        "transformer_subfolder": "",
        "local_dir_name": "Qwen-Image-2.1",
        "vae_tiling": True,
        "default_steps": 40,
        "default_true_cfg_no_neg": 1.0,
        "default_true_cfg_with_neg": 4.0,
        "default_negative_prompt": "",
        "lora_backend": "peft",
    },
    "uncensored": {
        "label": "ChatR Labs Qwen-Image-2.1 Uncensored Omni (Step 90,000 BF16)",
        "description": (
            "ChatR Labs Omni rank-128 merged BF16 full transformer (step 90,000) on Qwen/Qwen-Image-2.1 base "
            "(35 steps, true_cfg_scale=5.0 + single-space negative prompt, VAE tiling disabled)"
        ),
        "base_repo": "Qwen/Qwen-Image-2.1",
        "alias": "qwen-image-2.1-uncensored",
        "transformer_repo": "chatrlabs/Qwen-Image-2.1-Uncensored-Omni-Quantized",
        "transformer_subfolder": "qwen-image-2.1-step90000-BF16",
        "local_dir_name": "Qwen-Image-2.1-Uncensored-Omni-Step90000",
        "vae_tiling": False,
        "default_steps": 35,
        "default_true_cfg_no_neg": 5.0,
        "default_true_cfg_with_neg": 5.0,
        "default_negative_prompt": " ",
        "lora_backend": "peft",
    },
}


def resolve_variant_config(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Resolve active variant settings from MODEL_VARIANTS and CT_QWEN_IMAGE_* env overrides."""
    env = os.environ if environ is None else environ
    raw_variant = str(env.get("CT_QWEN_IMAGE_VARIANT") or "").strip().lower()
    variant = raw_variant or DEFAULT_VARIANT
    if variant not in MODEL_VARIANTS:
        raise ValueError(f"Unknown Qwen-Image variant {variant!r}; expected one of {tuple(MODEL_VARIANTS)}")

    vspec = MODEL_VARIANTS[variant]
    base_repo = str(env.get("CT_QWEN_IMAGE_MODEL") or "").strip() or str(vspec["base_repo"])
    alias = str(env.get("CT_QWEN_IMAGE_ALIAS") or "").strip() or str(vspec["alias"])

    if "CT_QWEN_IMAGE_TRANSFORMER_REPO" in env:
        transformer_repo = str(env["CT_QWEN_IMAGE_TRANSFORMER_REPO"] or "").strip()
    else:
        transformer_repo = str(vspec["transformer_repo"])

    if "CT_QWEN_IMAGE_TRANSFORMER_SUBFOLDER" in env:
        transformer_subfolder = str(env["CT_QWEN_IMAGE_TRANSFORMER_SUBFOLDER"] or "").strip().strip("/")
    else:
        transformer_subfolder = str(vspec["transformer_subfolder"]).strip("/")

    lora_backend = str(env.get("CT_QWEN_IMAGE_LORA_BACKEND") or "").strip().lower() or str(vspec["lora_backend"])

    raw_tiling = str(env.get("CT_QWEN_IMAGE_VAE_TILING") or "").strip().lower()
    if raw_tiling in ("", "auto"):
        vae_tiling = bool(vspec["vae_tiling"])
    else:
        vae_tiling = raw_tiling not in ("0", "false", "no")

    raw_steps = str(env.get("CT_QWEN_IMAGE_DEFAULT_STEPS") or "").strip()
    default_steps = int(raw_steps) if raw_steps else int(vspec["default_steps"])

    raw_cfg_no_neg = str(env.get("CT_QWEN_IMAGE_DEFAULT_TRUE_CFG_NO_NEG") or "").strip()
    default_true_cfg_no_neg = float(raw_cfg_no_neg) if raw_cfg_no_neg else float(vspec["default_true_cfg_no_neg"])

    raw_cfg_with_neg = str(env.get("CT_QWEN_IMAGE_DEFAULT_TRUE_CFG_WITH_NEG") or "").strip()
    default_true_cfg_with_neg = float(raw_cfg_with_neg) if raw_cfg_with_neg else float(vspec["default_true_cfg_with_neg"])

    if "CT_QWEN_IMAGE_DEFAULT_NEGATIVE_PROMPT" in env:
        default_negative_prompt = str(env["CT_QWEN_IMAGE_DEFAULT_NEGATIVE_PROMPT"])
    else:
        default_negative_prompt = str(vspec["default_negative_prompt"])

    if (
        base_repo == vspec["base_repo"]
        and transformer_repo == vspec["transformer_repo"]
        and transformer_subfolder == str(vspec["transformer_subfolder"]).strip("/")
    ):
        local_dir_name = str(vspec["local_dir_name"])
    elif not transformer_repo:
        local_dir_name = base_repo.split("/")[-1]
    else:
        tf_hash = hashlib.sha256(f"{transformer_repo}/{transformer_subfolder}".encode("utf-8")).hexdigest()[:8]
        local_dir_name = f"{base_repo.split('/')[-1]}--tf-{tf_hash}"

    expected_stamp = f"{base_repo}|{transformer_repo}|{transformer_subfolder}"
    return {
        "variant": variant,
        "label": str(vspec["label"]),
        "description": str(vspec["description"]),
        "base_repo": base_repo,
        "alias": alias,
        "transformer_repo": transformer_repo,
        "transformer_subfolder": transformer_subfolder,
        "local_dir_name": local_dir_name,
        "expected_stamp": expected_stamp,
        "vae_tiling": vae_tiling,
        "default_steps": default_steps,
        "default_true_cfg_no_neg": default_true_cfg_no_neg,
        "default_true_cfg_with_neg": default_true_cfg_with_neg,
        "default_negative_prompt": default_negative_prompt,
        "lora_backend": lora_backend,
    }


def is_checkpoint_ready(cfg: Mapping[str, Any], models_root: Path | str) -> bool:
    """Return True if the requested variant/transformer checkpoint is completely staged on SSD."""
    model_dir = Path(models_root) / str(cfg["local_dir_name"])
    ready_stamp = model_dir / ".ct_model_ready"
    if not ready_stamp.is_file():
        return False
    try:
        if ready_stamp.read_text(encoding="utf-8").strip() != str(cfg["expected_stamp"]):
            return False
    except OSError:
        return False
    tf_dir = model_dir / "transformer"
    return (
        (model_dir / "model_index.json").is_file()
        and (model_dir / "vae" / "diffusion_pytorch_model.safetensors").is_file()
        and (tf_dir / "config.json").is_file()
        and any(tf_dir.glob("*.safetensors"))
    )


def _reuse_base_from_sibling(model_dir: Path, models_root: Path, base_repo: str) -> bool:
    """Hardlink shared base components (text_encoder, vae, processor, scheduler) from a verified sibling."""
    if not models_root.is_dir():
        return False
    for sib in sorted(models_root.iterdir()):
        if not sib.is_dir() or sib == model_dir:
            continue
        stamp_p = sib / ".ct_model_ready"
        if not stamp_p.is_file():
            continue
        try:
            stamp_txt = stamp_p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not stamp_txt.startswith(f"{base_repo}|"):
            continue
        if not (
            (sib / "model_index.json").is_file()
            and (sib / "vae" / "diffusion_pytorch_model.safetensors").is_file()
            and (sib / "text_encoder" / "config.json").is_file()
        ):
            continue
        for src_path in sib.rglob("*"):
            rel = src_path.relative_to(sib)
            if not rel.parts:
                continue
            if rel.parts[0] in ("transformer", ".cache") or rel.name == ".ct_model_ready":
                continue
            dst_path = model_dir / rel
            if src_path.is_dir():
                dst_path.mkdir(parents=True, exist_ok=True)
                continue
            if not src_path.is_file():
                continue
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if dst_path.exists():
                if dst_path.stat().st_size == src_path.stat().st_size:
                    continue
                dst_path.unlink()
            try:
                os.link(src_path, dst_path)
            except OSError:
                shutil.copy2(src_path, dst_path)
        return True
    return False


def stage_variant_checkpoint(
    cfg: Mapping[str, Any],
    models_root: Path | str,
    snapshot_download_fn: Callable[..., Any],
    *,
    token: str | None = None,
    cleanup_hf_cache: bool = True,
) -> Path:
    """Download/stage base components and optional replacement transformer onto local SSD."""
    root = Path(models_root)
    root.mkdir(parents=True, exist_ok=True)
    model_dir = root / str(cfg["local_dir_name"])
    if is_checkpoint_ready(cfg, root):
        return model_dir

    model_dir.mkdir(parents=True, exist_ok=True)
    ready_stamp = model_dir / ".ct_model_ready"
    ready_stamp.unlink(missing_ok=True)

    base_repo = str(cfg["base_repo"])
    tf_repo = str(cfg.get("transformer_repo") or "").strip()
    tf_subfolder = str(cfg.get("transformer_subfolder") or "").strip().strip("/")

    if tf_repo:
        reused = _reuse_base_from_sibling(model_dir, root, base_repo)
        if not reused:
            snapshot_download_fn(
                repo_id=base_repo,
                local_dir=str(model_dir),
                token=token,
                max_workers=4,
                ignore_patterns=["*.md", ".gitattributes", "transformer/*", "transformer/**"],
            )

        tf_dir = model_dir / "transformer"
        shutil.rmtree(tf_dir, ignore_errors=True)
        tf_dir.mkdir(parents=True, exist_ok=True)

        tmp_tf = root / f".tmp_tf_{os.getpid()}"
        shutil.rmtree(tmp_tf, ignore_errors=True)
        tmp_tf.mkdir(parents=True, exist_ok=True)
        try:
            allow_patterns = (
                [f"{tf_subfolder}/*"]
                if tf_subfolder
                else ["config.json", "*.safetensors", "*.index.json", "transformer/*"]
            )
            snapshot_download_fn(
                repo_id=tf_repo,
                local_dir=str(tmp_tf),
                token=token,
                max_workers=4,
                allow_patterns=allow_patterns,
                ignore_patterns=["*.md", ".gitattributes"],
            )
            if tf_subfolder:
                src_tf = tmp_tf / tf_subfolder
            elif (tmp_tf / "transformer" / "config.json").is_file():
                src_tf = tmp_tf / "transformer"
            else:
                src_tf = tmp_tf

            if not (src_tf / "config.json").is_file() or not any(src_tf.glob("*.safetensors")):
                raise RuntimeError(
                    f"Custom transformer {tf_repo!r} (subfolder={tf_subfolder!r}) is missing "
                    f"config.json or *.safetensors in {src_tf}"
                )
            for item in src_tf.iterdir():
                if item.is_file() and (
                    item.name == "config.json"
                    or item.suffix == ".safetensors"
                    or item.name.endswith(".index.json")
                ):
                    os.replace(item, tf_dir / item.name)
        finally:
            shutil.rmtree(tmp_tf, ignore_errors=True)
    else:
        snapshot_download_fn(
            repo_id=base_repo,
            local_dir=str(model_dir),
            token=token,
            max_workers=4,
            ignore_patterns=["*.md", ".gitattributes"],
        )

    shutil.rmtree(model_dir / ".cache", ignore_errors=True)
    if cleanup_hf_cache:
        for cache_p in ("/root/.cache/huggingface/xet", "/root/.cache/huggingface/hub"):
            shutil.rmtree(cache_p, ignore_errors=True)

    ready_stamp.write_text(f"{cfg['expected_stamp']}\n", encoding="utf-8")
    return model_dir


_VARIANT_CFG = resolve_variant_config(os.environ)

API_TOKEN = os.environ.get("CT_API_TOKEN", "")
SERVICE_PORT = int((os.environ.get("CT_SERVICE_PORT") or "").strip() or "8091")

VARIANT = str(_VARIANT_CFG["variant"])
VARIANT_LABEL = str(_VARIANT_CFG["label"])
MODEL_REPO = str(_VARIANT_CFG["base_repo"])
MODEL_ALIAS = str(_VARIANT_CFG["alias"])
TRANSFORMER_REPO = str(_VARIANT_CFG["transformer_repo"])
TRANSFORMER_SUBFOLDER = str(_VARIANT_CFG["transformer_subfolder"])
VAE_TILING = bool(_VARIANT_CFG["vae_tiling"])
LORA_BACKEND = str(_VARIANT_CFG["lora_backend"])
DEFAULT_STEPS = int(_VARIANT_CFG["default_steps"])
DEFAULT_TRUE_CFG_NO_NEG = float(_VARIANT_CFG["default_true_cfg_no_neg"])
DEFAULT_TRUE_CFG_WITH_NEG = float(_VARIANT_CFG["default_true_cfg_with_neg"])
DEFAULT_NEGATIVE_PROMPT = str(_VARIANT_CFG["default_negative_prompt"])

WORK_DIR = Path(os.environ.get("CT_QWEN_IMAGE_WORK_DIR", "/content/qwen-image"))
MODEL_DIR = Path(
    (os.environ.get("CT_QWEN_IMAGE_MODEL_DIR") or "").strip()
    or str(WORK_DIR / "model" / _VARIANT_CFG["local_dir_name"])
)
LOCAL_LORA_DIR = Path(os.environ.get("CT_QWEN_IMAGE_LOCAL_LORA_DIR", str(WORK_DIR / "loras")))

DATA_DIR = Path(os.environ.get("CT_DATA_DIR", "/content/drive/MyDrive/colab-llm/qwen-image"))
DRIVE_ROOT = Path("/content/drive")
MYDRIVE_ROOT = Path("/content/drive/MyDrive")
DRIVE_INPUTS_DIR = DATA_DIR / "inputs"
DRIVE_OUTPUTS_DIR = DATA_DIR / "outputs"
DRIVE_LORAS_DIR = DATA_DIR / "loras"

QUANTIZATION = os.environ.get("CT_QWEN_IMAGE_QUANTIZATION", "none").strip().lower() or "none"
PREFIX_KV_DTYPE = os.environ.get("CT_QWEN_IMAGE_PREFIX_KV_DTYPE", "auto").strip().lower() or "auto"
STEP_EXECUTION = os.environ.get("CT_QWEN_IMAGE_STEP_EXECUTION", "0").strip() in ("1", "true", "True", "yes")
_RAW_MAX_NUM_SEQS = (os.environ.get("CT_QWEN_IMAGE_MAX_NUM_SEQS") or "").strip()
MAX_NUM_SEQS = int(_RAW_MAX_NUM_SEQS) if _RAW_MAX_NUM_SEQS else (8 if STEP_EXECUTION else 4)
PRELOADED_LORA = os.environ.get("CT_QWEN_IMAGE_PRELOADED_LORA", "").strip()

CORS_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"access-control-allow-origin", b"*"),
    (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
    (
        b"access-control-allow-headers",
        b"Authorization, Content-Type, X-API-Key, X-Save-Drive, X-Drive-Only",
    ),
    (b"access-control-expose-headers", b"X-Saved-Drive-Path"),
    (b"access-control-max-age", b"86400"),
)


def _verify_auth(headers: Any) -> bool:
    return verify_bearer_auth(headers, API_TOKEN)


def detect_image_mime(data: bytes, fallback_name: str = "") -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if fallback_name:
        ext = Path(fallback_name).suffix.lower()
        if ext in MIME_BY_EXT:
            return MIME_BY_EXT[ext]
        guessed, _ = mimetypes.guess_type(fallback_name)
        if guessed and guessed.startswith("image/"):
            return guessed
    return "image/png"


def image_path_to_data_url(image_path: Path) -> str:
    """Read an image file and encode it as a base64 data URI."""
    if not image_path.is_file():
        raise FileNotFoundError(f"Image file not found on Google Drive: {image_path}")
    raw = image_path.read_bytes()
    if not raw:
        raise ValueError(f"Image file is empty: {image_path}")
    mime = detect_image_mime(raw, image_path.name)
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def parse_size_spec(
    size_str: str,
    *,
    allow_auto: bool = False,
    align_to_32: bool = True,
) -> tuple[int | None, int | None]:
    """Parse 'WIDTHxHEIGHT' (or 'auto' when allow_auto=True), aligning to 32px."""
    s = str(size_str or "").strip()
    if allow_auto and s.lower() in ("", "auto"):
        return None, None
    m = re.fullmatch(r"(\d+)\s*[xX:]\s*(\d+)", s)
    if not m:
        expected = "WIDTHxHEIGHT or 'auto'" if allow_auto else "WIDTHxHEIGHT (e.g. 1024x1024)"
        raise ValueError(f"Invalid size '{size_str}': expected {expected}")
    w, h = int(m.group(1)), int(m.group(2))
    if w < 64 or h < 64 or w > 4096 or h > 4096:
        raise ValueError(f"Invalid size {w}x{h}: width and height must be between 64 and 4096")
    if align_to_32:
        w = max(64, (w // DIM_ALIGNMENT) * DIM_ALIGNMENT)
        h = max(64, (h // DIM_ALIGNMENT) * DIM_ALIGNMENT)
    elif w % DIM_ALIGNMENT != 0 or h % DIM_ALIGNMENT != 0:
        raise ValueError(f"Invalid size {w}x{h}: width and height must be multiples of {DIM_ALIGNMENT}")
    return w, h


def _maybe_convert_color_format(raw_bytes: bytes, color_format: str | None) -> bytes:
    """Convert 4-channel RGBA PNG bytes to 3-channel RGB PNG only when color_format=='RGB'."""
    norm = (color_format or "").strip().upper()
    if norm != "RGB":
        return raw_bytes
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw_bytes)) as im:
            if im.mode == "RGB":
                return raw_bytes
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                alpha = im.convert("RGBA")
                flat = Image.new("RGB", alpha.size, (255, 255, 255))
                flat.paste(alpha, mask=alpha.getchannel("A"))
            else:
                flat = im.convert("RGB")
            buf = io.BytesIO()
            flat.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return raw_bytes


def _ensure_within_roots(candidate: Path, roots: tuple[Path, ...], raw_spec: str) -> Path:
    resolved = candidate.resolve()
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError(f"Path '{raw_spec}' escapes allowed Drive/data directory")
    return resolved


def list_drive_files_by_kind(
    spec: str,
    *,
    kind: str = "images",
    data_dir: Path = DATA_DIR,
    recursive: bool = True,
    mydrive_root: Path = MYDRIVE_ROOT,
    drive_root: Path = DRIVE_ROOT,
    skip_dirs: set[str] | None = None,
) -> list[dict[str, Any]]:
    """List image or LoRA files matching a Drive path or glob strictly inside allowed roots."""
    if "\x00" in (spec or ""):
        raise ValueError("Invalid NUL byte in path")
    norm_kind = (kind or "images").strip().lower()
    if norm_kind in ("lora", "loras"):
        default_subdir = "loras"
        allowed_exts = LORA_EXTS
        include_peft_dirs = True
    elif norm_kind in ("output", "outputs"):
        default_subdir = "outputs"
        allowed_exts = IMAGE_EXTS
        include_peft_dirs = False
    elif norm_kind == "all":
        default_subdir = None
        allowed_exts = IMAGE_EXTS + LORA_EXTS
        include_peft_dirs = True
    else:
        default_subdir = "inputs" if not spec and (data_dir / "inputs").is_dir() else None
        allowed_exts = IMAGE_EXTS
        include_peft_dirs = False

    roots = allowed_drive_roots(data_dir, mydrive_root)
    ignored = skip_dirs if skip_dirs is not None else DEFAULT_SKIP_DIRS
    raw = (spec or "").strip()
    is_file_scheme = raw.startswith("file://")
    for prefix in ("drive://", "gdrive://", "file://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break

    has_glob = any(ch in raw for ch in ("*", "?", "["))
    matched_paths: list[tuple[Path, str]] = []

    if has_glob:
        if is_file_scheme:
            bases = [Path("/" + raw.lstrip("/"))]
        else:
            clean = raw.lstrip("/")
            if raw.startswith("/") and not clean.startswith(("content/drive/", "drive/", "MyDrive/")):
                bases = [Path("/" + clean)]
            elif clean.startswith("content/drive/"):
                bases = [Path("/") / clean]
            elif clean.startswith("MyDrive/"):
                bases = [drive_root / clean]
            else:
                bases = []
                if default_subdir:
                    bases.append(data_dir / default_subdir / clean)
                bases.append(data_dir / clean)
                if mydrive_root.is_dir():
                    bases.append(mydrive_root / clean)

        for pattern_path in bases:
            prefix_parts: list[str] = []
            for part in pattern_path.parts:
                if any(ch in part for ch in ("*", "?", "[")):
                    break
                prefix_parts.append(part)
            if prefix_parts:
                _ensure_within_roots(Path(*prefix_parts), roots, spec)

        seen: set[str] = set()
        for pattern_path in bases:
            for hit in sorted(glob_mod.glob(str(pattern_path), recursive=True)):
                hp = Path(hit).resolve()
                if not any(hp.is_relative_to(r) for r in roots) or str(hp) in seen:
                    continue
                if hp.is_file() and hp.suffix.lower() in allowed_exts:
                    seen.add(str(hp))
                    matched_paths.append((hp, "lora" if hp.suffix.lower() in LORA_EXTS else "image"))
                elif include_peft_dirs and hp.is_dir() and (hp / "adapter_config.json").is_file():
                    seen.add(str(hp))
                    matched_paths.append((hp, "peft_lora_dir"))
    else:
        target = resolve_drive_path(
            spec,
            data_dir=data_dir,
            default_subdir=default_subdir,
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )
        if target.is_file():
            if target.suffix.lower() in allowed_exts:
                matched_paths.append((target, "lora" if target.suffix.lower() in LORA_EXTS else "image"))
        elif target.is_dir():
            if include_peft_dirs and (target / "adapter_config.json").is_file():
                matched_paths.append((target, "peft_lora_dir"))
            elif not recursive:
                for hp in sorted(target.iterdir()):
                    resolved = hp.resolve()
                    if not any(resolved.is_relative_to(r) for r in roots):
                        continue
                    if resolved.is_file() and resolved.suffix.lower() in allowed_exts:
                        matched_paths.append((resolved, "lora" if resolved.suffix.lower() in LORA_EXTS else "image"))
                    elif include_peft_dirs and resolved.is_dir() and (resolved / "adapter_config.json").is_file():
                        matched_paths.append((resolved, "peft_lora_dir"))
            else:
                for root_dir, dirnames, filenames in os.walk(target):
                    cur_root = Path(root_dir).resolve()
                    if include_peft_dirs and cur_root != target and (cur_root / "adapter_config.json").is_file():
                        if any(cur_root.is_relative_to(r) for r in roots):
                            matched_paths.append((cur_root, "peft_lora_dir"))
                        dirnames[:] = []
                        continue
                    dirnames[:] = sorted(d for d in dirnames if d not in ignored and not d.startswith("."))
                    for fn in sorted(filenames):
                        hp = (Path(root_dir) / fn).resolve()
                        if any(hp.is_relative_to(r) for r in roots) and hp.is_file() and hp.suffix.lower() in allowed_exts:
                            matched_paths.append((hp, "lora" if hp.suffix.lower() in LORA_EXTS else "image"))

    items: list[dict[str, Any]] = []
    for p, ftype in matched_paths:
        st = p.stat()
        size_bytes = st.st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        items.append({
            "drive_path": str(p),
            "rel_path": rel_to_mydrive(p, mydrive_root),
            "name": p.name,
            "type": ftype,
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / 1e6, 2),
            "mtime": int(st.st_mtime),
        })
    return items


def save_output_images_to_drive(
    images_bytes: list[bytes],
    spec: str,
    *,
    data_dir: Path = DATA_DIR,
    output_dir: Path = DRIVE_OUTPUTS_DIR,
    prefix: str = "qwen-image",
    fmt: str = "png",
    mydrive_root: Path = MYDRIVE_ROOT,
    drive_root: Path = DRIVE_ROOT,
) -> list[str]:
    """Persist one or more generated image byte buffers safely inside Google Drive."""
    if not images_bytes:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    ext = "." + re.sub(r"[^a-z0-9]", "", (fmt or "png").lower())
    if ext not in IMAGE_EXTS:
        ext = ".png"

    stamp = f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    raw_spec = (spec or "").strip()
    is_auto = not raw_spec or raw_spec in ("auto", "true", "True", "1")
    treat_as_dir = False

    if is_auto:
        base_dir = output_dir
        stem = stamp
        treat_as_dir = True
    else:
        dest = resolve_drive_path(
            raw_spec,
            data_dir=data_dir,
            default_subdir="outputs",
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )
        if dest.is_dir() or raw_spec.endswith("/"):
            dest.mkdir(parents=True, exist_ok=True)
            base_dir = dest
            stem = stamp
            treat_as_dir = True
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.suffix:
                dest = dest.with_suffix(ext)
            base_dir = dest.parent
            stem = dest.stem
            ext = dest.suffix

    saved: list[str] = []
    total = len(images_bytes)
    for idx, img_bytes in enumerate(images_bytes, 1):
        if total == 1 and not treat_as_dir:
            out_path = base_dir / f"{stem}{ext}"
        elif total == 1:
            out_path = base_dir / f"{stem}{ext}"
            if out_path.exists():
                out_path = base_dir / f"{stem}-{int(time.time() * 1000) % 10000:04d}{ext}"
        else:
            out_path = base_dir / f"{stem}-{idx:02d}{ext}"
        out_path.write_bytes(img_bytes)
        saved.append(str(out_path))
    return saved


def resolve_and_stage_lora(
    lora_spec: Any,
    *,
    data_dir: Path = DATA_DIR,
    local_lora_dir: Path = LOCAL_LORA_DIR,
    mydrive_root: Path = MYDRIVE_ROOT,
    drive_root: Path = DRIVE_ROOT,
) -> dict[str, Any]:
    """Validate a LoRA spec against allowed roots and stage Drive LoRAs onto local SSD."""
    if isinstance(lora_spec, str):
        raw_path = lora_spec.strip()
        raw_name = ""
        scale = 1.0
    elif isinstance(lora_spec, dict):
        raw_path = str(lora_spec.get("path") or "").strip()
        raw_name = str(lora_spec.get("name") or "").strip()
        scale = float(lora_spec.get("scale", 1.0))
    else:
        raise ValueError("Invalid 'lora' parameter: expected object {'path', 'name'?, 'scale'?} or Drive path string")

    if not raw_path:
        raise ValueError("LoRA 'path' must not be empty")
    if "\x00" in raw_path:
        raise ValueError("Invalid NUL byte in LoRA path")

    local_root = local_lora_dir.resolve()
    cand_local = Path(raw_path)
    if cand_local.is_absolute() and not raw_path.startswith(("/content/drive/", "/drive/")):
        resolved_local = cand_local.resolve()
        if resolved_local.is_relative_to(local_root):
            if not resolved_local.exists():
                raise FileNotFoundError(f"Staged LoRA not found: {resolved_local}")
            name = raw_name or resolved_local.stem or resolved_local.name
            return {"name": name, "path": str(resolved_local), "scale": scale}
        src_drive = resolve_drive_path(
            raw_path,
            data_dir=data_dir,
            default_subdir="loras",
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )
    else:
        bare = raw_path
        for pfx in ("drive://", "gdrive://", "file://"):
            if bare.startswith(pfx):
                bare = ""
                break
        if bare and "/" not in bare and ".." not in bare and local_lora_dir.is_dir() and (local_lora_dir / bare).exists():
            resolved_local = (local_lora_dir / bare).resolve()
            if resolved_local.is_relative_to(local_root):
                name = raw_name or resolved_local.stem or resolved_local.name
                return {"name": name, "path": str(resolved_local), "scale": scale}
        src_drive = resolve_drive_path(
            raw_path,
            data_dir=data_dir,
            default_subdir="loras",
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )

    if not src_drive.exists():
        raise FileNotFoundError(f"LoRA adapter not found on Google Drive: {raw_path} (resolved to {src_drive})")

    local_lora_dir.mkdir(parents=True, exist_ok=True)
    key_hash = hashlib.sha256(str(src_drive).encode("utf-8")).hexdigest()[:10]
    safe_base = re.sub(r"[^A-Za-z0-9._-]+", "_", src_drive.name)
    staged_target = local_lora_dir / f"{key_hash}_{safe_base}"
    stamp_file = local_lora_dir / f".{key_hash}_{safe_base}.stamp"

    st = src_drive.stat()
    sig = f"{st.st_size}:{st.st_mtime_ns}"
    if not (staged_target.exists() and stamp_file.is_file() and stamp_file.read_text(encoding="utf-8") == sig):
        if src_drive.is_file():
            shutil.copyfile(src_drive, staged_target)
        elif src_drive.is_dir():
            if staged_target.exists():
                shutil.rmtree(staged_target)
            shutil.copytree(src_drive, staged_target, symlinks=False)
        else:
            raise ValueError(f"Unsupported LoRA path type: {src_drive}")
        stamp_file.write_text(sig, encoding="utf-8")

    name = raw_name or src_drive.stem or src_drive.name
    return {"name": name, "path": str(staged_target.resolve()), "scale": scale}


def _resolve_image_url_item(
    raw_url: str,
    *,
    data_dir: Path = DATA_DIR,
    mydrive_root: Path = MYDRIVE_ROOT,
    drive_root: Path = DRIVE_ROOT,
) -> str:
    url = (raw_url or "").strip()
    if not url:
        raise ValueError("Empty image URL in messages")
    if url.startswith(("data:image/", "http://", "https://")):
        return url
    img_path = resolve_drive_path(
        url,
        data_dir=data_dir,
        default_subdir="inputs",
        mydrive_root=mydrive_root,
        drive_root=drive_root,
    )
    return image_path_to_data_url(img_path)


def _extract_drive_options(payload: dict[str, Any], header_save: str = "", header_drive_only: str = "") -> tuple[str, bool]:
    save_val = payload.pop("save_to_drive", None)
    if save_val is None:
        save_val = payload.pop("save_drive_path", None)
    drive_only_val = payload.pop("drive_only", False)

    save_spec = ""
    if isinstance(save_val, bool):
        save_spec = "auto" if save_val else ""
    elif isinstance(save_val, str) and save_val.strip():
        save_spec = save_val.strip()
    if not save_spec and header_save:
        save_spec = header_save.strip()

    drive_only = bool(drive_only_val) or (header_drive_only.strip().lower() in ("1", "true", "yes"))
    if drive_only and not save_spec:
        save_spec = "auto"
    return save_spec, drive_only


def _normalize_served_model(payload: dict[str, Any]) -> None:
    """Map short model aliases and common OpenAI image aliases to primary MODEL_REPO for vLLM-Omni."""
    req_model = str(payload.get("model") or "").strip()
    accepted_aliases = {
        "",
        MODEL_REPO,
        MODEL_ALIAS,
        "qwen-image",
        "qwen-image-2.1",
        "Qwen-Image-2.1",
        "qwen-image-uncensored",
        *(v["alias"] for v in MODEL_VARIANTS.values()),
        MODEL_REPO.split("/")[-1],
        # Seamless drop-in compatibility for OpenAI Images SDK / Studio apps (e.g. open-pose-costume)
        "gpt-image-1",
        "gpt-image-1.5",
        "gpt-image-2",
    }
    if req_model in accepted_aliases:
        payload["model"] = MODEL_REPO


def _apply_qwen_sampling_defaults(payload: dict[str, Any]) -> str | None:
    """Inject variant-specific sampling / True CFG defaults and pop custom color_format."""
    color_format_raw = payload.pop("color_format", None)
    color_format = str(color_format_raw).strip().upper() if color_format_raw else None
    if color_format and color_format not in ("RGB", "RGBA"):
        raise ValueError(f"Invalid color_format '{color_format_raw}': expected 'RGB' or 'RGBA'")

    if payload.get("steps") is not None and payload.get("num_inference_steps") is None:
        payload["num_inference_steps"] = int(payload.pop("steps"))
    else:
        payload.pop("steps", None)
    if payload.get("num_inference_steps") is None:
        payload["num_inference_steps"] = DEFAULT_STEPS

    # Qwen-Image-2.1 uses true_cfg_scale, not guidance_scale.
    cfg_alias = payload.pop("cfg_scale", None)
    guidance_val = payload.pop("guidance_scale", None)
    if payload.get("true_cfg_scale") is None and cfg_alias is not None:
        payload["true_cfg_scale"] = float(cfg_alias)

    neg_raw = payload.get("negative_prompt")
    if neg_raw is None or neg_raw == "":
        payload.pop("negative_prompt", None)
        has_explicit_neg = False
    else:
        payload["negative_prompt"] = str(neg_raw)
        has_explicit_neg = True

    if payload.get("true_cfg_scale") is None:
        if has_explicit_neg:
            payload["true_cfg_scale"] = (
                float(guidance_val) if guidance_val is not None else DEFAULT_TRUE_CFG_WITH_NEG
            )
        else:
            payload["true_cfg_scale"] = DEFAULT_TRUE_CFG_NO_NEG

    if not has_explicit_neg and DEFAULT_NEGATIVE_PROMPT and float(payload["true_cfg_scale"]) > 1.0:
        payload["negative_prompt"] = DEFAULT_NEGATIVE_PROMPT

    # Strip OpenAI SDK 'quality' if sent by generic clients so vLLM-Omni doesn't reject/warn
    payload.pop("quality", None)
    return color_format


def _prepare_generation_request(
    raw_body: bytes,
    *,
    header_save: str = "",
    header_drive_only: str = "",
    data_dir: Path = DATA_DIR,
    local_lora_dir: Path = LOCAL_LORA_DIR,
    mydrive_root: Path = MYDRIVE_ROOT,
    drive_root: Path = DRIVE_ROOT,
) -> tuple[bytes, str, bool, str | None]:
    """Validate and rewrite a POST /v1/images/generations JSON body."""
    try:
        payload = json.loads(raw_body)
    except Exception as err:
        raise ValueError(f"Invalid JSON request body: {err}") from err
    if not isinstance(payload, dict):
        raise ValueError("JSON request body must be an object")

    _normalize_served_model(payload)
    save_spec, drive_only = _extract_drive_options(payload, header_save, header_drive_only)
    color_format = _apply_qwen_sampling_defaults(payload)

    if "size" in payload and payload["size"] is not None:
        w, h = parse_size_spec(str(payload["size"]), allow_auto=True)
        if w is None or h is None:
            payload["size"] = "1024x1024"
        else:
            payload["size"] = f"{w}x{h}"
    elif payload.get("width") is not None or payload.get("height") is not None:
        w_in = int(payload.pop("width", 1024))
        h_in = int(payload.pop("height", 1024))
        w, h = parse_size_spec(f"{w_in}x{h_in}")
        payload["size"] = f"{w}x{h}"
    else:
        payload["size"] = "1024x1024"

    if payload.get("lora") is not None:
        payload["lora"] = resolve_and_stage_lora(
            payload["lora"],
            data_dir=data_dir,
            local_lora_dir=local_lora_dir,
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )

    if save_spec and save_spec not in ("auto", "true", "True", "1"):
        resolve_drive_path(
            save_spec,
            data_dir=data_dir,
            default_subdir="outputs",
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )

    if save_spec and payload.get("response_format") not in (None, "b64_json"):
        payload["response_format"] = "b64_json"

    return json.dumps(payload).encode("utf-8"), save_spec, drive_only, color_format


def _rewrite_drive_urls_in_chat_body(
    raw_body: bytes,
    *,
    header_save: str = "",
    header_drive_only: str = "",
    data_dir: Path = DATA_DIR,
    local_lora_dir: Path = LOCAL_LORA_DIR,
    mydrive_root: Path = MYDRIVE_ROOT,
    drive_root: Path = DRIVE_ROOT,
) -> tuple[bytes, str, bool, str | None]:
    """Validate and rewrite Drive image references and LoRAs in POST /v1/chat/completions."""
    try:
        payload = json.loads(raw_body)
    except Exception as err:
        raise ValueError(f"Invalid JSON request body: {err}") from err
    if not isinstance(payload, dict):
        raise ValueError("JSON request body must be an object")

    extra_body = payload.get("extra_body")
    if isinstance(extra_body, dict):
        for k, v in list(extra_body.items()):
            payload.setdefault(k, v)
        payload.pop("extra_body", None)

    _normalize_served_model(payload)
    save_spec, drive_only = _extract_drive_options(payload, header_save, header_drive_only)
    color_format = _apply_qwen_sampling_defaults(payload)

    if "size" in payload and payload["size"] is not None:
        w, h = parse_size_spec(str(payload.pop("size")), allow_auto=True)
        if w is not None and h is not None:
            payload["width"] = w
            payload["height"] = h
    elif payload.get("width") is not None or payload.get("height") is not None:
        w_in = int(payload.get("width") or 1024)
        h_in = int(payload.get("height") or 1024)
        w, h = parse_size_spec(f"{w_in}x{h_in}")
        payload["width"] = w
        payload["height"] = h

    if payload.get("lora") is not None:
        payload["lora"] = resolve_and_stage_lora(
            payload["lora"],
            data_dir=data_dir,
            local_lora_dir=local_lora_dir,
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )

    if save_spec and save_spec not in ("auto", "true", "True", "1"):
        resolve_drive_path(
            save_spec,
            data_dir=data_dir,
            default_subdir="outputs",
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )

    resolved_images: list[str] = []
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                continue
            iurl = item.get("image_url")
            if isinstance(iurl, str):
                item["image_url"] = {"url": iurl}
                iurl = item["image_url"]
            if not isinstance(iurl, dict) or not isinstance(iurl.get("url"), str):
                raise ValueError("Invalid image_url entry in messages")
            resolved_url = _resolve_image_url_item(
                iurl["url"],
                data_dir=data_dir,
                mydrive_root=mydrive_root,
                drive_root=drive_root,
            )
            iurl["url"] = resolved_url
            resolved_images.append(resolved_url)

    if len(resolved_images) > MAX_REFERENCE_IMAGES:
        raise ValueError(
            f"Qwen-Image-2.1 supports at most {MAX_REFERENCE_IMAGES} reference images (<image1>..<image4>), "
            f"got {len(resolved_images)}"
        )

    return json.dumps(payload).encode("utf-8"), save_spec, drive_only, color_format


def _extract_images_from_generations_response(resp_obj: dict[str, Any]) -> list[tuple[dict[str, Any], bytes, str]]:
    out: list[tuple[dict[str, Any], bytes, str]] = []
    for item in resp_obj.get("data") or []:
        if not isinstance(item, dict):
            continue
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            raw = base64.b64decode(b64)
            mime = detect_image_mime(raw)
            out.append((item, raw, EXT_BY_MIME.get(mime, "png")))
    return out


def _extract_images_from_chat_response(resp_obj: dict[str, Any]) -> list[tuple[dict[str, Any], bytes, str]]:
    out: list[tuple[dict[str, Any], bytes, str]] = []
    for choice in resp_obj.get("choices") or []:
        msg = choice.get("message") if isinstance(choice, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            iurl = part.get("image_url")
            url_val = iurl.get("url") if isinstance(iurl, dict) else None
            if isinstance(url_val, str) and url_val.startswith("data:image/") and "," in url_val:
                header, b64 = url_val.split(",", 1)
                raw = base64.b64decode(b64)
                mime = header.split(";")[0].removeprefix("data:") or detect_image_mime(raw)
                out.append((iurl, raw, EXT_BY_MIME.get(mime, "png")))
    return out


def _parse_multipart_edit_body(content_type: str, raw_body: bytes) -> dict[str, Any]:
    """Parse multipart/form-data (e.g. OpenAI SDK / curl -F / browser FormData) preserving exact image order."""
    raw_msg = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        + raw_body
    )
    msg = BytesParser(policy=email_default_policy).parsebytes(raw_msg)
    if not msg.is_multipart():
        raise ValueError("Invalid multipart/form-data payload")

    fields: dict[str, Any] = {}
    ordered_inputs: list[tuple[str, Any]] = []
    for part in msg.iter_parts():
        disp = part.get("Content-Disposition", "")
        if "form-data" not in disp:
            continue
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload_bytes = part.get_payload(decode=True) or b""
        if not name:
            continue
        clean_name = name.rstrip("[]")
        if filename is not None or (clean_name in ("image", "images") and part.get_content_type().startswith("image/")):
            if not payload_bytes:
                continue
            mime = detect_image_mime(payload_bytes, filename or "upload.png")
            ordered_inputs.append(("bytes", (payload_bytes, filename or f"image.{EXT_BY_MIME.get(mime, 'png')}", mime)))
        else:
            raw_text = payload_bytes.decode("utf-8", errors="replace")
            text_val = (
                raw_text
                if (clean_name == "negative_prompt" and raw_text and not raw_text.strip())
                else raw_text.strip()
            )
            if clean_name in ("image", "images", "url", "drive_image", "drive_images"):
                if text_val:
                    ordered_inputs.append(("spec", text_val))
            else:
                fields[clean_name] = text_val

    if ordered_inputs:
        fields["_ordered_inputs"] = ordered_inputs
    for int_key in ("seed", "num_inference_steps", "steps", "width", "height", "n", "output_compression"):
        if int_key in fields and fields[int_key] != "":
            fields[int_key] = int(fields[int_key])
    for float_key in ("true_cfg_scale", "cfg_scale", "guidance_scale", "lora_scale"):
        if float_key in fields and fields[float_key] != "":
            fields[float_key] = float(fields[float_key])
    for bool_key in ("drive_only",):
        if bool_key in fields:
            fields[bool_key] = str(fields[bool_key]).lower() in ("1", "true", "yes")
    if "lora" in fields and isinstance(fields["lora"], str) and fields["lora"].startswith("{"):
        fields["lora"] = json.loads(fields["lora"])
    return fields


def _encode_multipart_formdata(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, str, bytes]],
) -> tuple[str, bytes]:
    """Build a multipart/form-data body for loopback forwarding to vLLM-Omni /v1/images/edits."""
    boundary = f"----ColabQwenImageBoundary{secrets.token_hex(12)}"
    buf = bytearray()
    for fname, fval in fields:
        buf.extend(f"--{boundary}\r\n".encode("ascii"))
        buf.extend(f'Content-Disposition: form-data; name="{fname}"\r\n\r\n'.encode("utf-8"))
        buf.extend(str(fval).encode("utf-8"))
        buf.extend(b"\r\n")
    for field_name, filename, mime, raw in files:
        safe_fn = re.sub(r'["\r\n\\]', "_", filename or "image.png")
        buf.extend(f"--{boundary}\r\n".encode("ascii"))
        buf.extend(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_fn}"\r\n'.encode("utf-8")
        )
        buf.extend(f"Content-Type: {mime}\r\n\r\n".encode("ascii"))
        buf.extend(raw)
        buf.extend(b"\r\n")
    buf.extend(f"--{boundary}--\r\n".encode("ascii"))
    return f"multipart/form-data; boundary={boundary}", bytes(buf)


def _resolve_edit_input_item(
    kind: str,
    item: Any,
    idx: int,
    *,
    data_dir: Path,
    mydrive_root: Path,
    drive_root: Path,
) -> tuple[str, Any]:
    """Resolve an edit input into either ('file', (filename, mime, bytes)) or ('http', url_str)."""
    if kind == "bytes":
        raw_bytes, filename, mime = item
        return "file", (filename, mime, raw_bytes)
    spec = str(item or "").strip()
    if not spec:
        raise ValueError("Empty reference image entry")
    if spec.startswith(("http://", "https://")):
        return "http", spec
    if spec.startswith("data:image/") and "," in spec:
        header, b64 = spec.split(",", 1)
        raw_bytes = base64.b64decode(b64)
        if not raw_bytes:
            raise ValueError("Decoded data:image URI is empty")
        mime = header.split(";")[0].removeprefix("data:") or detect_image_mime(raw_bytes)
        ext = EXT_BY_MIME.get(mime, "png")
        return "file", (f"ref_{idx}.{ext}", mime, raw_bytes)
    img_path = resolve_drive_path(
        spec,
        data_dir=data_dir,
        default_subdir="inputs",
        mydrive_root=mydrive_root,
        drive_root=drive_root,
    )
    if not img_path.is_file():
        raise FileNotFoundError(f"Image file not found on Google Drive: {img_path}")
    raw_bytes = img_path.read_bytes()
    if not raw_bytes:
        raise ValueError(f"Image file is empty: {img_path}")
    mime = detect_image_mime(raw_bytes, img_path.name)
    return "file", (img_path.name, mime, raw_bytes)


def _prepare_edit_request(
    content_type: str,
    raw_body: bytes,
    *,
    header_save: str = "",
    header_drive_only: str = "",
    data_dir: Path = DATA_DIR,
    local_lora_dir: Path = LOCAL_LORA_DIR,
    mydrive_root: Path = MYDRIVE_ROOT,
    drive_root: Path = DRIVE_ROOT,
) -> tuple[str, str, bytes, str, bool, str | None]:
    """Prepare POST /v1/images/edits or POST /api/ct/edit for direct in-process ASGI dispatch.

    Returns (target_path, new_content_type, new_body_bytes, save_spec, drive_only, color_format).
    Pure binary/Drive/data-URI or pure HTTP-URL batches rewrite into multipart/form-data for upstream
    /v1/images/edits (preserving 4-channel RGBA input PNGs, size='auto', and multi-MB uploads with
    zero loopback HTTP calls). Mixed HTTP-URL + binary/Drive batches rewrite into /v1/chat/completions
    JSON to preserve exact <image1>..<image4> ordering without middleware SSRF fetches.
    """
    if "multipart/form-data" in (content_type or "").lower():
        body = _parse_multipart_edit_body(content_type, raw_body)
    else:
        try:
            body = json.loads(raw_body)
        except Exception as err:
            raise ValueError(f"Invalid JSON body: {err}") from err
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")

    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("Missing required 'prompt'")

    ordered_raw: list[tuple[str, Any]] = list(body.pop("_ordered_inputs", None) or [])
    if not ordered_raw:
        for key in ("images", "image", "drive_images", "drive_image", "url"):
            val = body.get(key)
            if isinstance(val, str) and val.strip():
                ordered_raw.append(("spec", val.strip()))
            elif isinstance(val, list):
                for x in val:
                    if str(x).strip():
                        ordered_raw.append(("spec", str(x).strip()))

    if not ordered_raw:
        raise ValueError("At least 1 reference image is required ('image' or 'images')")
    if len(ordered_raw) > MAX_REFERENCE_IMAGES:
        raise ValueError(
            f"Qwen-Image-2.1 supports at most {MAX_REFERENCE_IMAGES} reference images (<image1>..<image4>), "
            f"got {len(ordered_raw)}"
        )

    _normalize_served_model(body)
    save_spec, drive_only = _extract_drive_options(body, header_save, header_drive_only)
    color_format = _apply_qwen_sampling_defaults(body)

    if save_spec and save_spec not in ("auto", "true", "True", "1"):
        resolve_drive_path(
            save_spec,
            data_dir=data_dir,
            default_subdir="outputs",
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )

    staged_lora: dict[str, Any] | None = None
    if body.get("lora") is not None:
        staged_lora = resolve_and_stage_lora(
            body["lora"],
            data_dir=data_dir,
            local_lora_dir=local_lora_dir,
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )
    elif body.get("lora_path"):
        staged_lora = resolve_and_stage_lora(
            {
                "path": body["lora_path"],
                "name": body.get("lora_name") or "",
                "scale": float(body.get("lora_scale", 1.0)),
            },
            data_dir=data_dir,
            local_lora_dir=local_lora_dir,
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )

    size_str = "auto"
    if body.get("size") is not None:
        w, h = parse_size_spec(str(body["size"]), allow_auto=True)
        size_str = "auto" if (w is None or h is None) else f"{w}x{h}"
    elif body.get("width") is not None or body.get("height") is not None:
        w_in = int(body.get("width") or 1024)
        h_in = int(body.get("height") or 1024)
        w, h = parse_size_spec(f"{w_in}x{h_in}")
        size_str = f"{w}x{h}"

    resolved_items = [
        _resolve_edit_input_item(
            kind,
            item,
            idx,
            data_dir=data_dir,
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )
        for idx, (kind, item) in enumerate(ordered_raw, 1)
    ]
    has_files = any(rk == "file" for rk, _ in resolved_items)
    has_http = any(rk == "http" for rk, _ in resolved_items)

    if has_files and has_http:
        content_parts: list[dict[str, Any]] = []
        for rk, rv in resolved_items:
            if rk == "http":
                content_parts.append({"type": "image_url", "image_url": {"url": rv}})
            else:
                _, mime, raw_b = rv
                b64_u = f"data:{mime};base64,{base64.b64encode(raw_b).decode('ascii')}"
                content_parts.append({"type": "image_url", "image_url": {"url": b64_u}})
        content_parts.append({"type": "text", "text": prompt})
        chat_payload: dict[str, Any] = {
            "model": MODEL_REPO,
            "messages": [{"role": "user", "content": content_parts}],
            "num_inference_steps": int(body["num_inference_steps"]),
            "true_cfg_scale": float(body["true_cfg_scale"]),
        }
        if size_str != "auto":
            w_s, h_s = size_str.split("x", 1)
            chat_payload["width"] = int(w_s)
            chat_payload["height"] = int(h_s)
        if body.get("negative_prompt"):
            chat_payload["negative_prompt"] = str(body["negative_prompt"])
        if body.get("seed") is not None:
            chat_payload["seed"] = int(body["seed"])
        if staged_lora is not None:
            chat_payload["lora"] = staged_lora
        return (
            "/v1/chat/completions",
            "application/json",
            json.dumps(chat_payload).encode("utf-8"),
            save_spec,
            drive_only,
            color_format,
        )

    form_fields: list[tuple[str, str]] = [
        ("model", MODEL_REPO),
        ("prompt", prompt),
        ("size", size_str),
        ("response_format", "b64_json"),
        ("num_inference_steps", str(int(body["num_inference_steps"]))),
        ("true_cfg_scale", str(float(body["true_cfg_scale"]))),
    ]
    if body.get("n") is not None:
        form_fields.append(("n", str(int(body["n"]))))
    if body.get("seed") is not None:
        form_fields.append(("seed", str(int(body["seed"]))))
    if body.get("negative_prompt"):
        form_fields.append(("negative_prompt", str(body["negative_prompt"])))
    if body.get("output_format"):
        form_fields.append(("output_format", str(body["output_format"])))
    if body.get("background"):
        form_fields.append(("background", str(body["background"])))
    if body.get("output_compression") is not None:
        form_fields.append(("output_compression", str(int(body["output_compression"]))))
    if staged_lora is not None:
        form_fields.append(("lora", json.dumps(staged_lora)))

    form_files: list[tuple[str, str, str, bytes]] = []
    for rk, rv in resolved_items:
        if rk == "http":
            form_fields.append(("url", str(rv)))
        else:
            fn, mime, raw_b = rv
            form_files.append(("image", fn, mime, raw_b))

    mp_ct, mp_bytes = _encode_multipart_formdata(form_fields, form_files)
    return "/v1/images/edits", mp_ct, mp_bytes, save_spec, drive_only, color_format


async def _read_full_body(receive: Receive) -> bytes | None:
    chunks: list[bytes] = []
    while True:
        msg = await receive()
        if msg["type"] == "http.request":
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body", False):
                return b"".join(chunks)
        elif msg["type"] == "http.disconnect":
            return None


def _with_rewritten_request(scope: Scope, *, path: str, content_type: str, length: int) -> Scope:
    new_scope = dict(scope)
    new_scope["path"] = path
    new_scope["raw_path"] = path.encode("utf-8")
    raw_headers = [
        (k, v)
        for (k, v) in scope.get("headers", [])
        if k.lower() not in (b"content-length", b"content-type")
    ]
    raw_headers.append((b"content-type", content_type.encode("utf-8")))
    raw_headers.append((b"content-length", str(length).encode("ascii")))
    new_scope["headers"] = raw_headers
    return new_scope


def _inject_cors_headers(raw_headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    existing = {k.lower() for k, _ in raw_headers}
    merged = list(raw_headers)
    for ck, cv in CORS_HEADERS:
        if ck not in existing:
            merged.append((ck, cv))
    return merged


class QwenImageMiddleware:
    """ASGI middleware enforcing Bearer auth, CORS preflight, Google Drive I/O, and Qwen-Image-2.1 defaults."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            resp = Response(status_code=HTTPStatus.NO_CONTENT)
            async def cors_options_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    message = dict(message)
                    message["headers"] = _inject_cors_headers(list(message.get("headers", [])))
                await send(message)
            await resp(scope, receive, cors_options_send)
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = _inject_cors_headers(list(message.get("headers", [])))
            await send(message)

        root_path = scope.get("root_path", "")
        path = scope["path"].removeprefix(root_path)
        headers = Headers(scope=scope)

        if not _verify_auth(headers):
            resp = JSONResponse(
                {
                    "error": {
                        "message": "Unauthorized: invalid or missing Bearer token",
                        "type": "AuthenticationError",
                        "code": 401,
                    }
                },
                status_code=HTTPStatus.UNAUTHORIZED,
            )
            await resp(scope, receive, send_with_cors)
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")

        if method == "GET" and path == "/api/ct/info":
            served = list(dict.fromkeys([MODEL_REPO, MODEL_ALIAS, "qwen-image", "qwen-image-2.1"]))
            resp = JSONResponse({
                "service": "qwen-image",
                "variant": VARIANT,
                "variant_label": VARIANT_LABEL,
                "model_repo": MODEL_REPO,
                "transformer_repo": (
                    f"{TRANSFORMER_REPO}/{TRANSFORMER_SUBFOLDER}".strip("/") if TRANSFORMER_REPO else None
                ),
                "served_models": served,
                "quantization": QUANTIZATION,
                "prefix_kv_cache_dtype": PREFIX_KV_DTYPE,
                "step_execution": STEP_EXECUTION,
                "vae_use_tiling": VAE_TILING,
                "max_num_seqs": MAX_NUM_SEQS,
                "default_steps": DEFAULT_STEPS,
                "default_true_cfg_scale": DEFAULT_TRUE_CFG_NO_NEG,
                "default_true_cfg_scale_with_negative_prompt": DEFAULT_TRUE_CFG_WITH_NEG,
                "default_negative_prompt": DEFAULT_NEGATIVE_PROMPT or None,
                "lora_backend": LORA_BACKEND,
                "default_size": "1024x1024",
                "dimension_alignment": DIM_ALIGNMENT,
                "rgba_vae": True,
                "max_reference_images": MAX_REFERENCE_IMAGES,
                "preloaded_lora": PRELOADED_LORA or None,
                "drive_mounted": MYDRIVE_ROOT.is_dir(),
                "data_dir": str(DATA_DIR),
                "drive_inputs_dir": str(DRIVE_INPUTS_DIR),
                "drive_outputs_dir": str(DRIVE_OUTPUTS_DIR),
                "drive_loras_dir": str(DRIVE_LORAS_DIR),
            })
            await resp(scope, receive, send_with_cors)
            return

        if method == "GET" and path == "/api/ct/logs":
            qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="replace"))
            try:
                n_lines = int((qs.get("lines") or ["120"])[0])
            except ValueError:
                n_lines = 120
            resp = JSONResponse(tail_service_log(log_file=Path("/content/ct/service.log"), lines=n_lines))
            await resp(scope, receive, send_with_cors)
            return

        if method == "GET" and path == "/api/ct/drive-list":
            qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="replace"))
            spec = (qs.get("path") or [""])[0]
            kind = (qs.get("kind") or ["images"])[0]
            recursive = (qs.get("recursive") or ["1"])[0] not in ("0", "false", "False")
            try:
                files = await asyncio.to_thread(
                    list_drive_files_by_kind,
                    spec,
                    kind=kind,
                    data_dir=DATA_DIR,
                    recursive=recursive,
                    mydrive_root=MYDRIVE_ROOT,
                    drive_root=DRIVE_ROOT,
                )
                resp = JSONResponse({
                    "drive_mounted": MYDRIVE_ROOT.is_dir(),
                    "kind": kind,
                    "query": spec,
                    "count": len(files),
                    "files": files,
                })
            except ValueError as err:
                resp = JSONResponse({"error": str(err)}, status_code=400)
            except Exception as err:
                resp = JSONResponse({"error": str(err)}, status_code=500)
            await resp(scope, receive, send_with_cors)
            return

        if method == "POST" and path in (
            "/api/ct/edit",
            "/v1/images/edits",
            "/v1/images/generations",
            "/v1/chat/completions",
        ):
            raw_body = await _read_full_body(receive)
            if raw_body is None:
                return
            ct = headers.get("content-type", "application/json")
            h_save = headers.get("x-save-drive", "")
            h_only = headers.get("x-drive-only", "")
            is_edit_route = path in ("/api/ct/edit", "/v1/images/edits")

            try:
                if is_edit_route:
                    target_path, new_ct, rewritten_body, save_spec, drive_only, color_format = await asyncio.to_thread(
                        _prepare_edit_request,
                        ct,
                        raw_body,
                        header_save=h_save,
                        header_drive_only=h_only,
                        data_dir=DATA_DIR,
                        local_lora_dir=LOCAL_LORA_DIR,
                        mydrive_root=MYDRIVE_ROOT,
                        drive_root=DRIVE_ROOT,
                    )
                    wants_images_shape = True
                    save_prefix = "qwen-edit"
                elif path == "/v1/images/generations":
                    rewritten_body, save_spec, drive_only, color_format = await asyncio.to_thread(
                        _prepare_generation_request,
                        raw_body,
                        header_save=h_save,
                        header_drive_only=h_only,
                        data_dir=DATA_DIR,
                        local_lora_dir=LOCAL_LORA_DIR,
                        mydrive_root=MYDRIVE_ROOT,
                        drive_root=DRIVE_ROOT,
                    )
                    target_path = "/v1/images/generations"
                    new_ct = "application/json"
                    wants_images_shape = True
                    save_prefix = "qwen-t2i"
                else:
                    rewritten_body, save_spec, drive_only, color_format = await asyncio.to_thread(
                        _rewrite_drive_urls_in_chat_body,
                        raw_body,
                        header_save=h_save,
                        header_drive_only=h_only,
                        data_dir=DATA_DIR,
                        local_lora_dir=LOCAL_LORA_DIR,
                        mydrive_root=MYDRIVE_ROOT,
                        drive_root=DRIVE_ROOT,
                    )
                    target_path = "/v1/chat/completions"
                    new_ct = "application/json"
                    wants_images_shape = False
                    save_prefix = "qwen-i2i"
            except (ValueError, FileNotFoundError) as err:
                status = 404 if isinstance(err, FileNotFoundError) else 400
                resp = JSONResponse(
                    {"error": {"message": str(err), "type": "BadRequestError", "code": status}},
                    status_code=status,
                )
                await resp(scope, receive, send_with_cors)
                return

            new_scope = _with_rewritten_request(
                scope,
                path=root_path + target_path,
                content_type=new_ct,
                length=len(rewritten_body),
            )
            body_sent = False

            async def replay_receive() -> Message:
                nonlocal body_sent
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": rewritten_body, "more_body": False}
                return await receive()

            upstream_is_images = target_path in ("/v1/images/generations", "/v1/images/edits")
            convert_chat_to_images = wants_images_shape and not upstream_is_images
            need_postprocess = bool(save_spec or color_format == "RGB" or convert_chat_to_images)
            if not need_postprocess:
                await self.app(new_scope, replay_receive, send_with_cors)
                return

            resp_status = 200
            resp_headers: list[tuple[bytes, bytes]] = []
            resp_chunks: list[bytes] = []
            start_msg: Message | None = None

            async def tee_send(message: Message) -> None:
                nonlocal resp_status, resp_headers, start_msg
                if message["type"] == "http.response.start":
                    resp_status = message.get("status", 200)
                    resp_headers = list(message.get("headers", []))
                    start_msg = message
                    if resp_status != 200:
                        await send_with_cors(message)
                elif message["type"] == "http.response.body":
                    chunk = message.get("body", b"")
                    more = message.get("more_body", False)
                    if resp_status != 200:
                        await send_with_cors(message)
                        return
                    if chunk:
                        resp_chunks.append(chunk)
                    if not more and start_msg is not None:
                        full_raw = b"".join(resp_chunks)
                        saved_paths: list[str] = []
                        out_bytes = full_raw
                        try:
                            resp_obj = json.loads(full_raw.decode("utf-8"))
                            extracted = (
                                _extract_images_from_generations_response(resp_obj)
                                if upstream_is_images
                                else _extract_images_from_chat_response(resp_obj)
                            )
                            if extracted and color_format == "RGB":
                                updated_extracted: list[tuple[dict[str, Any], bytes, str]] = []
                                for holder, img_bytes, fmt in extracted:
                                    rgb_bytes = await asyncio.to_thread(
                                        _maybe_convert_color_format, img_bytes, "RGB"
                                    )
                                    b64_rgb = base64.b64encode(rgb_bytes).decode("ascii")
                                    if upstream_is_images:
                                        holder["b64_json"] = b64_rgb
                                    else:
                                        holder["url"] = f"data:image/png;base64,{b64_rgb}"
                                    updated_extracted.append((holder, rgb_bytes, fmt))
                                extracted = updated_extracted

                            if save_spec and extracted:
                                saved_paths = await asyncio.to_thread(
                                    save_output_images_to_drive,
                                    [raw for _, raw, _ in extracted],
                                    save_spec,
                                    data_dir=DATA_DIR,
                                    output_dir=DRIVE_OUTPUTS_DIR,
                                    prefix=save_prefix,
                                    fmt=extracted[0][2],
                                    mydrive_root=MYDRIVE_ROOT,
                                    drive_root=DRIVE_ROOT,
                                )

                            if convert_chat_to_images:
                                data_entries: list[dict[str, Any]] = []
                                for idx, (_, img_bytes, _) in enumerate(extracted):
                                    entry: dict[str, Any] = {}
                                    if not drive_only:
                                        entry["b64_json"] = base64.b64encode(img_bytes).decode("ascii")
                                    if idx < len(saved_paths):
                                        sp = Path(saved_paths[idx])
                                        entry["saved_drive_path"] = str(sp)
                                        entry["rel_drive_path"] = rel_to_mydrive(sp, MYDRIVE_ROOT)
                                        entry["size_bytes"] = len(img_bytes)
                                    data_entries.append(entry)
                                resp_obj = {"created": int(time.time()), "data": data_entries}
                                if saved_paths:
                                    resp_obj["saved_drive_paths"] = saved_paths
                                    resp_obj["rel_drive_paths"] = [
                                        rel_to_mydrive(Path(p), MYDRIVE_ROOT) for p in saved_paths
                                    ]
                            elif save_spec and extracted:
                                resp_obj["saved_drive_paths"] = saved_paths
                                resp_obj["rel_drive_paths"] = [
                                    rel_to_mydrive(Path(p), MYDRIVE_ROOT) for p in saved_paths
                                ]
                                for idx, (holder, img_bytes, _) in enumerate(extracted):
                                    if idx < len(saved_paths):
                                        holder["saved_drive_path"] = saved_paths[idx]
                                        holder["rel_drive_path"] = rel_to_mydrive(Path(saved_paths[idx]), MYDRIVE_ROOT)
                                        holder["size_bytes"] = len(img_bytes)
                                    if drive_only:
                                        if upstream_is_images:
                                            holder.pop("b64_json", None)
                                        else:
                                            holder["url"] = f"drive://{rel_to_mydrive(Path(saved_paths[idx]), MYDRIVE_ROOT)}"
                            out_bytes = json.dumps(resp_obj).encode("utf-8")
                        except Exception:
                            out_bytes = full_raw

                        new_headers = [
                            (k, v)
                            for (k, v) in resp_headers
                            if k.lower() not in (b"content-length", b"x-saved-drive-path")
                        ]
                        new_headers.append((b"content-length", str(len(out_bytes)).encode("ascii")))
                        if saved_paths:
                            safe_hdr = sanitize_header_param(saved_paths[0]).encode("utf-8")
                            new_headers.append((b"x-saved-drive-path", safe_hdr))
                        start_msg["headers"] = new_headers
                        await send_with_cors(start_msg)
                        await send_with_cors({"type": "http.response.body", "body": out_bytes, "more_body": False})
                else:
                    await send_with_cors(message)

            await self.app(new_scope, replay_receive, tee_send)
            return

        await self.app(scope, receive, send_with_cors)
