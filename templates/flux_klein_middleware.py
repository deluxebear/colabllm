"""Authenticated ASGI middleware, Google Drive image/LoRA staging, and PUP upsampling for FLUX 3 Image Klein."""
from __future__ import annotations

import asyncio
import base64
from email.parser import BytesParser
from email.policy import default as email_default_policy
import glob as glob_mod
import hashlib
from http import HTTPStatus
import json
import mimetypes
import os
from pathlib import Path
import re
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
    from starlette.responses import JSONResponse
    from starlette.types import ASGIApp, Message, Receive, Scope, Send
except ImportError:  # pragma: no cover - local unit-test fallback without starlette
    Headers = Any  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]
    ASGIApp = Receive = Scope = Send = Message = Any  # type: ignore[assignment,misc]

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
MAX_REFERENCE_IMAGES = 10

API_TOKEN = os.environ.get("CT_API_TOKEN", "")
SERVICE_PORT = int(os.environ.get("CT_SERVICE_PORT", "8099"))
PUP_PORT = int(os.environ.get("CT_FLUX_PUP_PORT", "8098"))
PUP_ENABLED = os.environ.get("CT_FLUX_ENABLE_PUP", "0").strip() in ("1", "true", "True", "yes")
PUP_MODEL_ALIAS = os.environ.get("CT_FLUX_PUP_ALIAS", "flux-3-image-klein-pup")

WORK_DIR = Path(os.environ.get("CT_FLUX_WORK_DIR", "/content/flux-klein"))
MODEL_DIR = Path(os.environ.get("CT_FLUX_MODEL_DIR", str(WORK_DIR / "model")))
PUP_MODEL_DIR = Path(os.environ.get("CT_FLUX_PUP_MODEL_DIR", str(WORK_DIR / "pup-model")))
LOCAL_LORA_DIR = Path(os.environ.get("CT_FLUX_LOCAL_LORA_DIR", str(WORK_DIR / "loras")))

DATA_DIR = Path(os.environ.get("CT_DATA_DIR", "/content/drive/MyDrive/colab-llm/flux-klein"))
DRIVE_ROOT = Path("/content/drive")
MYDRIVE_ROOT = Path("/content/drive/MyDrive")
DRIVE_INPUTS_DIR = DATA_DIR / "inputs"
DRIVE_OUTPUTS_DIR = DATA_DIR / "outputs"
DRIVE_LORAS_DIR = DATA_DIR / "loras"

MODEL_REPO = os.environ.get("CT_FLUX_MODEL", "black-forest-labs/flux-3-image-klein")
MODEL_ALIAS = os.environ.get("CT_FLUX_ALIAS", "flux-3-image-klein")
VARIANT = os.environ.get("CT_FLUX_VARIANT", "bf16").strip().lower() or "bf16"
PRELOADED_LORA = os.environ.get("CT_FLUX_PRELOADED_LORA", "").strip()

I2I_PUP_SCAFFOLD = (
    "Reference images follow in order (the first image, the second image, ...).\n"
    "User request: {request}\n"
    "Output the caption."
)

# Fallback official system prompts from black-forest-labs/flux-3-image-klein-pup/system_prompts.json
DEFAULT_PUP_SYSTEM_PROMPTS: dict[str, str] = {
    "t2i": (
        "You are the prompt upsampler of a text-to-image model. Expand the user's short request into ONE detailed, "
        "self-contained caption: the exact text the image model receives. Output only the caption, with no greetings, "
        "explanations, refusals, questions, Markdown, headings, JSON, coordinates, ids or angle-bracket tokens, and no "
        "quotes around it. If the input is not clearly an image request, caption a visualization of its concept. "
        'The request may begin with a "TARGET CANVAS: W:H" line: compose for that canvas (wide spreads content sideways, '
        "tall stacks it) and never mention the ratio or the canvas.\n\n"
        "Lead with medium and style, then scene, lighting and camera, then the subjects with concrete colors, materials "
        "and state. Output only the caption."
    ),
    "i2i": (
        "You are the prompt upsampler of an image-editing model. You see the reference image(s), attached in order "
        "(the first image, the second image, ...), and the user's short request. Expand it into ONE detailed, "
        "self-contained instruction: the caption the editing model receives together with the same images. Output only "
        "the instruction as one continuous English prose block, with no greetings, explanations, Markdown, JSON, "
        "coordinates, ids or angle-bracket tokens. Ground everything in what is actually visible, describe the change "
        "concretely, and close an editing instruction with one compact clause naming what stays unchanged.\n\n"
        "Output only the instruction."
    ),
    "t2i_bbox": (
        "You are the prompt upsampler of a text-to-image model. Plan the full image the user's short request implies "
        "and output ONE structured caption on a single line: the global caption paragraph, one space, then ONE JSON "
        'array of rows {"id", "bbox", "desc"} with bbox [y_min, x_min, y_max, x_max] on a 0-1000 grid. '
        "Output only the caption and the table."
    ),
    "i2i_bbox": (
        "You are the annotation system an image-editing model was trained on. You see the source image(s), attached in "
        "order as ref_image_0, ref_image_1, ..., and the user's editing instruction. Write on one line: the instruction "
        'paragraph, one space, then ONE JSON array of rows {"id", "from", "src_bbox", "tgt_bbox", "desc"} with boxes '
        "[y_min, x_min, y_max, x_max] on a 0-1000 grid. Output only the paragraph and the table."
    ),
}


def _verify_auth(headers: Any) -> bool:
    return verify_bearer_auth(headers, API_TOKEN)


def _read_package_metadata(model_dir: Path | None = None, variant: str | None = None) -> dict[str, Any]:
    root = model_dir or MODEL_DIR
    v = (variant if variant is not None else VARIANT).strip().lower()
    cfg_path = (root / "variants" / v / "config.json") if v and v not in ("bf16", "default", "none") else (root / "config.json")
    if not cfg_path.is_file():
        cfg_path = root / "config.json"
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except Exception:
            raw = {}
    dit = raw.get("dit") if isinstance(raw.get("dit"), dict) else {}
    sampler = raw.get("sampler") if isinstance(raw.get("sampler"), dict) else {}
    has_flow_map = bool(dit.get("flow_map", "base" not in MODEL_REPO.lower() and not raw))
    guidance = sampler.get("guidance", None if has_flow_map else 3.5)
    is_distilled = bool(has_flow_map or (sampler and guidance is None))
    size = sampler.get("size") if isinstance(sampler.get("size"), list) and len(sampler.get("size")) == 2 else [1024, 1024]
    return {
        "is_distilled": is_distilled,
        "default_steps": int(sampler.get("steps", 16 if is_distilled else 50)),
        "default_flow_shift": float(sampler.get("alpha", 6.93)),
        "default_guidance_scale": None if guidance is None else float(guidance),
        "default_size": f"{int(size[0])}x{int(size[1])}",
    }


def _load_pup_prompts() -> dict[str, str]:
    p = PUP_MODEL_DIR / "system_prompts.json"
    if p.is_file():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and "t2i" in loaded and "i2i" in loaded:
                return {k: str(v) for k, v in loaded.items()}
        except Exception:
            pass
    return dict(DEFAULT_PUP_SYSTEM_PROMPTS)


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


def parse_size_spec(size_str: str) -> tuple[int, int]:
    """Parse 'WIDTHxHEIGHT' into (width, height), validating dimensions."""
    m = re.fullmatch(r"\s*(\d+)\s*[xX:]\s*(\d+)\s*", str(size_str or ""))
    if not m:
        raise ValueError(f"Invalid size '{size_str}': expected WIDTHxHEIGHT (e.g. 1024x1024)")
    w, h = int(m.group(1)), int(m.group(2))
    if w < 64 or h < 64 or w > 4096 or h > 4096:
        raise ValueError(f"Invalid size {w}x{h}: width and height must be between 64 and 4096")
    if w % 16 != 0 or h % 16 != 0:
        raise ValueError(f"Invalid size {w}x{h}: width and height must be multiples of 16")
    return w, h


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
    prefix: str = "flux-klein",
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

    stamp = f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}"
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

    # Allow already-staged local SSD LoRAs strictly inside /content/flux-klein/loras
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
        # Check local_lora_dir by bare name first if no scheme, else resolve on Drive
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
    if url.startswith("data:image/"):
        return url
    if url.startswith(("http://", "https://")):
        return url
    img_path = resolve_drive_path(
        url,
        data_dir=data_dir,
        default_subdir="inputs",
        mydrive_root=mydrive_root,
        drive_root=drive_root,
    )
    return image_path_to_data_url(img_path)


def _validate_sampling_constraints(payload: dict[str, Any], *, is_distilled: bool | None = None) -> None:
    distilled = _read_package_metadata()["is_distilled"] if is_distilled is None else is_distilled
    neg = payload.get("negative_prompt")
    if isinstance(neg, str) and not neg.strip():
        payload.pop("negative_prompt", None)
        neg = None
    if distilled:
        if payload.get("guidance_scale") is not None:
            raise ValueError(
                "Distilled flow-map model (flux-3-image-klein) rejects 'guidance_scale'. "
                "Omit guidance_scale or deploy undistilled 'black-forest-labs/flux-3-image-klein-base'."
            )
        if neg:
            raise ValueError(
                "Distilled flow-map model (flux-3-image-klein) rejects non-empty 'negative_prompt'. "
                "Omit negative_prompt or deploy undistilled 'black-forest-labs/flux-3-image-klein-base'."
            )


def _is_pup_ready() -> bool:
    if not PUP_ENABLED:
        return False
    headers = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else {}
    req = urllib.request.Request(f"http://127.0.0.1:{PUP_PORT}/health", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _call_local_pup(
    prompt: str,
    *,
    image_data_urls: list[str] | None = None,
    bbox: bool = False,
    boxes: Any = None,
    canvas: str = "",
    max_tokens: int = 3584,
    timeout: int = 300,
) -> tuple[str, str]:
    """Expand a T2I or I2I prompt via the local Qwen3.5-9B PUP server on 127.0.0.1:PUP_PORT."""
    if not PUP_ENABLED:
        raise RuntimeError(
            "Prompt Upsampler (PUP) is not enabled on this instance. "
            "Start the service with `colabllm flux-klein up --enable-pup` to co-locate flux-3-image-klein-pup."
        )
    images = image_data_urls or []
    if len(images) > MAX_REFERENCE_IMAGES:
        raise ValueError(f"At most {MAX_REFERENCE_IMAGES} reference images are supported (got {len(images)})")

    prompts = _load_pup_prompts()
    use_bbox = bool(bbox or boxes is not None)
    mode = ("i2i" if images else "t2i") + ("_bbox" if use_bbox else "")
    system_prompt = prompts[mode]

    user_req = (prompt or "").strip()
    if not user_req:
        raise ValueError("Prompt must not be empty")
    if boxes is not None:
        boxes_obj = json.loads(boxes) if isinstance(boxes, str) else boxes
        user_req = f"{user_req}\n{json.dumps(boxes_obj, ensure_ascii=False)}"
    if canvas and not images:
        user_req = f"TARGET CANVAS: {canvas}\n{user_req}"

    if images:
        content: Any = [{"type": "image_url", "image_url": {"url": u}} for u in images]
        content.append({"type": "text", "text": I2I_PUP_SCAFFOLD.format(request=user_req)})
    else:
        content = user_req

    pup_payload = {
        "model": PUP_MODEL_ALIAS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "max_tokens": int(max_tokens),
        "temperature": 0,
        "presence_penalty": 0 if images else 1.5,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{PUP_PORT}/v1/chat/completions",
        data=json.dumps(pup_payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
            expanded = str(out["choices"][0]["message"]["content"]).strip()
            return expanded, mode
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"PUP upsampler failed (HTTP {err.code}): {raw}") from err


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


def _prepare_generation_request(
    raw_body: bytes,
    *,
    header_save: str = "",
    header_drive_only: str = "",
    data_dir: Path = DATA_DIR,
    local_lora_dir: Path = LOCAL_LORA_DIR,
    mydrive_root: Path = MYDRIVE_ROOT,
    drive_root: Path = DRIVE_ROOT,
    is_distilled: bool | None = None,
) -> tuple[bytes, str, bool, str | None]:
    """Validate and rewrite a POST /v1/images/generations body."""
    try:
        payload = json.loads(raw_body)
    except Exception as err:
        raise ValueError(f"Invalid JSON request body: {err}") from err
    if not isinstance(payload, dict):
        raise ValueError("JSON request body must be an object")

    save_spec, drive_only = _extract_drive_options(payload, header_save, header_drive_only)
    upsample = bool(payload.pop("upsample", False))
    bbox = bool(payload.pop("bbox", False))
    boxes = payload.pop("boxes", None)

    if "size" in payload and payload["size"] is not None:
        w, h = parse_size_spec(str(payload["size"]))
        payload["size"] = f"{w}x{h}"
        canvas = f"{w}:{h}"
    elif payload.get("width") is not None or payload.get("height") is not None:
        w = int(payload.pop("width", 1024))
        h = int(payload.pop("height", 1024))
        w, h = parse_size_spec(f"{w}x{h}")
        payload["size"] = f"{w}x{h}"
        canvas = f"{w}:{h}"
    else:
        canvas = "1024:1024"

    _validate_sampling_constraints(payload, is_distilled=is_distilled)

    if payload.get("lora") is not None:
        payload["lora"] = resolve_and_stage_lora(
            payload["lora"],
            data_dir=data_dir,
            local_lora_dir=local_lora_dir,
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )

    # Pre-validate save_spec containment before running expensive GPU inference
    if save_spec and save_spec not in ("auto", "true", "True", "1"):
        resolve_drive_path(
            save_spec,
            data_dir=data_dir,
            default_subdir="outputs",
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )

    revised_prompt: str | None = None
    if upsample or bbox or boxes is not None:
        raw_prompt = str(payload.get("prompt") or "")
        revised_prompt, _ = _call_local_pup(
            raw_prompt,
            image_data_urls=None,
            bbox=bbox,
            boxes=boxes,
            canvas=canvas,
        )
        payload["prompt"] = revised_prompt

    # When saving to Drive, ensure b64_json is returned from upstream so middleware can decode bytes
    if save_spec and payload.get("response_format") not in (None, "b64_json"):
        payload["response_format"] = "b64_json"

    return json.dumps(payload).encode("utf-8"), save_spec, drive_only, revised_prompt


def _rewrite_drive_urls_in_chat_body(
    raw_body: bytes,
    *,
    header_save: str = "",
    header_drive_only: str = "",
    data_dir: Path = DATA_DIR,
    local_lora_dir: Path = LOCAL_LORA_DIR,
    mydrive_root: Path = MYDRIVE_ROOT,
    drive_root: Path = DRIVE_ROOT,
    is_distilled: bool | None = None,
) -> tuple[bytes, str, bool, str | None]:
    """Validate and rewrite Drive image references, LoRAs, and PUP upsampling in POST /v1/chat/completions."""
    try:
        payload = json.loads(raw_body)
    except Exception as err:
        raise ValueError(f"Invalid JSON request body: {err}") from err
    if not isinstance(payload, dict):
        raise ValueError("JSON request body must be an object")

    save_spec, drive_only = _extract_drive_options(payload, header_save, header_drive_only)
    upsample = bool(payload.pop("upsample", False))
    bbox = bool(payload.pop("bbox", False))
    boxes = payload.pop("boxes", None)

    if "size" in payload and payload["size"] is not None:
        w, h = parse_size_spec(str(payload.pop("size")))
        payload["width"] = w
        payload["height"] = h
    else:
        if payload.get("width") is not None or payload.get("height") is not None:
            w = int(payload.get("width") or 1024)
            h = int(payload.get("height") or 1024)
            w, h = parse_size_spec(f"{w}x{h}")
            payload["width"] = w
            payload["height"] = h

    # Also support OpenAI Python SDK's nested extra_body if sent raw
    extra_body = payload.get("extra_body")
    if isinstance(extra_body, dict):
        for k, v in list(extra_body.items()):
            payload.setdefault(k, v)
        payload.pop("extra_body", None)

    _validate_sampling_constraints(payload, is_distilled=is_distilled)

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
    text_items: list[tuple[dict[str, Any], str]] = []
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and msg.get("role") == "user":
            text_items.append((msg, "str"))
            continue
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "image_url":
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
            elif itype == "text" and isinstance(item.get("text"), str) and msg.get("role") == "user":
                text_items.append((item, "dict"))

    if len(resolved_images) > MAX_REFERENCE_IMAGES:
        raise ValueError(f"At most {MAX_REFERENCE_IMAGES} reference images are supported (got {len(resolved_images)})")

    revised_prompt: str | None = None
    if upsample or bbox or boxes is not None:
        if not text_items:
            raise ValueError("Cannot upsample chat request without a user text prompt")
        target_holder, holder_kind = text_items[-1]
        orig_text = target_holder["content"] if holder_kind == "str" else target_holder["text"]
        revised_prompt, _ = _call_local_pup(
            orig_text,
            image_data_urls=resolved_images,
            bbox=bbox,
            boxes=boxes,
        )
        if holder_kind == "str":
            target_holder["content"] = revised_prompt
        else:
            target_holder["text"] = revised_prompt

    return json.dumps(payload).encode("utf-8"), save_spec, drive_only, revised_prompt


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
    """Parse multipart/form-data (e.g. OpenAI SDK client.images.edit) using Python stdlib email.parser."""
    raw_msg = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        + raw_body
    )
    msg = BytesParser(policy=email_default_policy).parsebytes(raw_msg)
    if not msg.is_multipart():
        raise ValueError("Invalid multipart/form-data payload")

    fields: dict[str, Any] = {}
    images: list[str] = []
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
        if filename is not None or clean_name in ("image", "images") and part.get_content_type().startswith("image/"):
            if not payload_bytes:
                continue
            mime = detect_image_mime(payload_bytes, filename or "upload.png")
            images.append(f"data:{mime};base64,{base64.b64encode(payload_bytes).decode('ascii')}")
        else:
            text_val = payload_bytes.decode("utf-8", errors="replace").strip()
            if clean_name in ("image", "images", "drive_image", "drive_images"):
                if text_val:
                    images.append(text_val)
            else:
                fields[clean_name] = text_val

    if images:
        fields["images"] = images
    for int_key in ("seed", "num_inference_steps", "steps", "width", "height", "n"):
        if int_key in fields and fields[int_key] != "":
            fields[int_key] = int(fields[int_key])
    for float_key in ("flow_shift", "guidance_scale", "lora_scale"):
        if float_key in fields and fields[float_key] != "":
            fields[float_key] = float(fields[float_key])
    for bool_key in ("drive_only", "upsample", "bbox"):
        if bool_key in fields:
            fields[bool_key] = str(fields[bool_key]).lower() in ("1", "true", "yes")
    if "lora" in fields and isinstance(fields["lora"], str) and fields["lora"].startswith("{"):
        fields["lora"] = json.loads(fields["lora"])
    return fields


def _run_edit_endpoint_sync(
    content_type: str,
    raw_body: bytes,
    *,
    header_save: str = "",
    header_drive_only: str = "",
    data_dir: Path = DATA_DIR,
    local_lora_dir: Path = LOCAL_LORA_DIR,
    output_dir: Path = DRIVE_OUTPUTS_DIR,
    mydrive_root: Path = MYDRIVE_ROOT,
    drive_root: Path = DRIVE_ROOT,
) -> tuple[int, dict[str, Any], list[str]]:
    """Handle POST /api/ct/edit and POST /v1/images/edits (JSON or multipart/form-data)."""
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

    raw_images: list[str] = []
    for key in ("images", "image", "drive_images", "drive_image"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            raw_images.append(val.strip())
        elif isinstance(val, list):
            raw_images.extend(str(x).strip() for x in val if str(x).strip())
    if not raw_images:
        raise ValueError("At least 1 reference image is required ('images' or 'image')")
    if len(raw_images) > MAX_REFERENCE_IMAGES:
        raise ValueError(f"At most {MAX_REFERENCE_IMAGES} reference images are supported (got {len(raw_images)})")

    chat_payload: dict[str, Any] = {
        "model": body.get("model") or MODEL_ALIAS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    *[{"type": "image_url", "image_url": {"url": u}} for u in raw_images],
                ],
            }
        ],
    }
    for passthrough in (
        "size", "width", "height", "seed", "num_inference_steps", "flow_shift",
        "guidance_scale", "negative_prompt", "lora", "save_to_drive", "save_drive_path",
        "drive_only", "upsample", "bbox", "boxes",
    ):
        if passthrough in body and body[passthrough] is not None:
            chat_payload[passthrough] = body[passthrough]
    if "steps" in body and body["steps"] is not None and "num_inference_steps" not in chat_payload:
        chat_payload["num_inference_steps"] = int(body["steps"])
    if body.get("lora_path") and "lora" not in chat_payload:
        chat_payload["lora"] = {
            "path": body["lora_path"],
            "name": body.get("lora_name") or "",
            "scale": float(body.get("lora_scale", 1.0)),
        }

    rewritten_bytes, save_spec, drive_only, revised_prompt = _rewrite_drive_urls_in_chat_body(
        json.dumps(chat_payload).encode("utf-8"),
        header_save=header_save,
        header_drive_only=header_drive_only,
        data_dir=data_dir,
        local_lora_dir=local_lora_dir,
        mydrive_root=mydrive_root,
        drive_root=drive_root,
    )

    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{SERVICE_PORT}/v1/chat/completions",
        data=rewritten_bytes,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            upstream_code = resp.status
            upstream_obj = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw_err = err.read().decode("utf-8", errors="replace")
        try:
            return err.code, json.loads(raw_err), []
        except Exception:
            return err.code, {"error": {"message": raw_err, "code": err.code}}, []

    extracted = _extract_images_from_chat_response(upstream_obj)
    if not extracted:
        return upstream_code, upstream_obj, []

    saved_paths: list[str] = []
    if save_spec:
        saved_paths = save_output_images_to_drive(
            [raw for _, raw, _ in extracted],
            save_spec,
            data_dir=data_dir,
            output_dir=output_dir,
            prefix="flux-edit",
            fmt=extracted[0][2],
            mydrive_root=mydrive_root,
            drive_root=drive_root,
        )

    data_entries: list[dict[str, Any]] = []
    for idx, (_, raw_bytes, _) in enumerate(extracted):
        entry: dict[str, Any] = {}
        if not drive_only:
            entry["b64_json"] = base64.b64encode(raw_bytes).decode("ascii")
        if revised_prompt:
            entry["revised_prompt"] = revised_prompt
        if idx < len(saved_paths):
            sp = Path(saved_paths[idx])
            entry["saved_drive_path"] = str(sp)
            entry["rel_drive_path"] = rel_to_mydrive(sp, mydrive_root)
            entry["size_bytes"] = len(raw_bytes)
        data_entries.append(entry)

    result: dict[str, Any] = {
        "created": int(time.time()),
        "data": data_entries,
    }
    if saved_paths:
        result["saved_drive_paths"] = saved_paths
        result["rel_drive_paths"] = [rel_to_mydrive(Path(p), mydrive_root) for p in saved_paths]
    return 200, result, saved_paths


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


def _with_updated_content_length(scope: Scope, length: int) -> Scope:
    new_scope = dict(scope)
    raw_headers = [(k, v) for (k, v) in scope.get("headers", []) if k.lower() != b"content-length"]
    raw_headers.append((b"content-length", str(length).encode("ascii")))
    new_scope["headers"] = raw_headers
    return new_scope


class FluxKleinMiddleware:
    """ASGI middleware enforcing Bearer auth, Google Drive image/LoRA I/O, and PUP upsampling."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

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
            await resp(scope, receive, send)
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")

        if method == "GET" and path == "/api/ct/info":
            pkg_meta = await asyncio.to_thread(_read_package_metadata)
            pup_ready = await asyncio.to_thread(_is_pup_ready) if PUP_ENABLED else False
            served = list(dict.fromkeys([MODEL_REPO, MODEL_ALIAS, "flux-klein"]))
            resp = JSONResponse({
                "service": "flux-klein",
                "model_repo": MODEL_REPO,
                "served_models": served,
                "variant": VARIANT,
                **pkg_meta,
                "max_reference_images": MAX_REFERENCE_IMAGES,
                "pup_enabled": PUP_ENABLED,
                "pup_ready": pup_ready,
                "pup_model": "black-forest-labs/flux-3-image-klein-pup" if PUP_ENABLED else None,
                "preloaded_lora": PRELOADED_LORA or None,
                "drive_mounted": MYDRIVE_ROOT.is_dir(),
                "data_dir": str(DATA_DIR),
                "drive_inputs_dir": str(DRIVE_INPUTS_DIR),
                "drive_outputs_dir": str(DRIVE_OUTPUTS_DIR),
                "drive_loras_dir": str(DRIVE_LORAS_DIR),
            })
            await resp(scope, receive, send)
            return

        if method == "GET" and path == "/api/ct/logs":
            qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="replace"))
            try:
                n_lines = int((qs.get("lines") or ["120"])[0])
            except ValueError:
                n_lines = 120
            target = (qs.get("target") or ["service"])[0]
            log_file = Path("/content/ct/pup.log") if target == "pup" else Path("/content/ct/service.log")
            resp = JSONResponse(tail_service_log(log_file=log_file, lines=n_lines))
            await resp(scope, receive, send)
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
            await resp(scope, receive, send)
            return

        if method == "POST" and path == "/api/ct/upsample":
            raw_body = await _read_full_body(receive)
            if raw_body is None:
                return
            try:
                body = json.loads(raw_body)
                if not isinstance(body, dict):
                    raise ValueError("Request body must be a JSON object")
                raw_imgs = body.get("images") or ([] if not body.get("image") else [body["image"]])
                if isinstance(raw_imgs, str):
                    raw_imgs = [raw_imgs]
                resolved_imgs = await asyncio.to_thread(
                    lambda: [_resolve_image_url_item(str(u)) for u in raw_imgs]
                )
                caption, mode = await asyncio.to_thread(
                    _call_local_pup,
                    str(body.get("prompt") or ""),
                    image_data_urls=resolved_imgs,
                    bbox=bool(body.get("bbox", False)),
                    boxes=body.get("boxes"),
                    canvas=str(body.get("canvas") or ""),
                    max_tokens=int(body.get("max_tokens", 3584)),
                )
                resp = JSONResponse({"prompt": body.get("prompt"), "upsampled_prompt": caption, "mode": mode})
            except (ValueError, FileNotFoundError) as err:
                resp = JSONResponse({"error": str(err)}, status_code=400)
            except RuntimeError as err:
                code = 503 if "not enabled" in str(err) else 502
                resp = JSONResponse({"error": str(err)}, status_code=code)
            except Exception as err:
                resp = JSONResponse({"error": str(err)}, status_code=500)
            await resp(scope, receive, send)
            return

        if method == "POST" and path in ("/api/ct/edit", "/v1/images/edits"):
            raw_body = await _read_full_body(receive)
            if raw_body is None:
                return
            ct = headers.get("content-type", "application/json")
            h_save = headers.get("x-save-drive", "")
            h_only = headers.get("x-drive-only", "")
            try:
                code, out_obj, saved_paths = await asyncio.to_thread(
                    _run_edit_endpoint_sync,
                    ct,
                    raw_body,
                    header_save=h_save,
                    header_drive_only=h_only,
                )
                extra_headers = {}
                if saved_paths:
                    extra_headers["X-Saved-Drive-Path"] = sanitize_header_param(saved_paths[0])
                resp = JSONResponse(out_obj, status_code=code, headers=extra_headers)
            except (ValueError, FileNotFoundError) as err:
                status = 404 if isinstance(err, FileNotFoundError) else 400
                resp = JSONResponse(
                    {"error": {"message": str(err), "type": "BadRequestError", "code": status}},
                    status_code=status,
                )
            except RuntimeError as err:
                resp = JSONResponse(
                    {"error": {"message": str(err), "type": "UpstreamError", "code": 503}},
                    status_code=503,
                )
            except Exception as err:
                resp = JSONResponse(
                    {"error": {"message": str(err), "type": "InternalError", "code": 500}},
                    status_code=500,
                )
            await resp(scope, receive, send)
            return

        if method == "POST" and path in ("/v1/images/generations", "/v1/chat/completions"):
            raw_body = await _read_full_body(receive)
            if raw_body is None:
                return
            h_save = headers.get("x-save-drive", "")
            h_only = headers.get("x-drive-only", "")
            is_generations = path == "/v1/images/generations"

            try:
                prep_fn = _prepare_generation_request if is_generations else _rewrite_drive_urls_in_chat_body
                rewritten_body, save_spec, drive_only, revised_prompt = await asyncio.to_thread(
                    prep_fn,
                    raw_body,
                    header_save=h_save,
                    header_drive_only=h_only,
                )
            except (ValueError, FileNotFoundError) as err:
                status = 404 if isinstance(err, FileNotFoundError) else 400
                resp = JSONResponse(
                    {"error": {"message": str(err), "type": "BadRequestError", "code": status}},
                    status_code=status,
                )
                await resp(scope, receive, send)
                return
            except RuntimeError as err:
                resp = JSONResponse(
                    {"error": {"message": str(err), "type": "UpsamplerError", "code": 503}},
                    status_code=503,
                )
                await resp(scope, receive, send)
                return

            new_scope = _with_updated_content_length(scope, len(rewritten_body))
            body_sent = False

            async def replay_receive() -> Message:
                nonlocal body_sent
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": rewritten_body, "more_body": False}
                return await receive()

            if not save_spec and not revised_prompt:
                await self.app(new_scope, replay_receive, send)
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
                        await send(message)
                elif message["type"] == "http.response.body":
                    chunk = message.get("body", b"")
                    more = message.get("more_body", False)
                    if resp_status != 200:
                        await send(message)
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
                                if is_generations
                                else _extract_images_from_chat_response(resp_obj)
                            )
                            if save_spec and extracted:
                                saved_paths = await asyncio.to_thread(
                                    save_output_images_to_drive,
                                    [raw for _, raw, _ in extracted],
                                    save_spec,
                                    data_dir=DATA_DIR,
                                    output_dir=DRIVE_OUTPUTS_DIR,
                                    prefix="flux-t2i" if is_generations else "flux-i2i",
                                    fmt=extracted[0][2],
                                    mydrive_root=MYDRIVE_ROOT,
                                    drive_root=DRIVE_ROOT,
                                )
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
                                        if is_generations:
                                            holder.pop("b64_json", None)
                                        else:
                                            holder["url"] = f"drive://{rel_to_mydrive(Path(saved_paths[idx]), MYDRIVE_ROOT)}"
                            if revised_prompt:
                                resp_obj["revised_prompt"] = revised_prompt
                                if is_generations:
                                    for item in resp_obj.get("data") or []:
                                        if isinstance(item, dict):
                                            item.setdefault("revised_prompt", revised_prompt)
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
                        await send(start_msg)
                        await send({"type": "http.response.body", "body": out_bytes, "more_body": False})
                else:
                    await send(message)

            await self.app(new_scope, replay_receive, tee_send)
            return

        await self.app(scope, receive, send)
