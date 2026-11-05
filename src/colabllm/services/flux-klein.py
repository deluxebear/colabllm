#!/usr/bin/env python3
"""FLUX 3 Image Klein on Colab G4 / H100 / L4 GPUs (notebook: flux-klein.ipynb).

Follows https://recipes.vllm.ai/black-forest-labs/flux-3-image-klein?hardware=h100:
  - Defaults to Colab `G4` (RTX PRO 6000 Blackwell, 96 GB VRAM) for optimal cost/VRAM headroom;
    also supports `--gpu H100` (80 GB HBM3, fastest latency) and `--gpu L4` (24 GB, low-cost `--variant fp8r`).
  - Serves `black-forest-labs/flux-3-image-klein` (5.6B flow-map distilled DiT + Qwen3-VL-4B text
    encoder + VAE; BF16 by default, optional `fp8r` / `nvfp4` quantized variants, or undistilled
    `black-forest-labs/flux-3-image-klein-base`) via `vllm serve --omni` on loopback `:8099`.
  - Enforces fail-closed Bearer / `X-API-Key` authentication across every HTTP route.
  - Exposes complete OpenAI-compatible & Google Drive-native APIs:
      * `POST /v1/images/generations` (Text-to-Image + Drive output saving + LoRA + PUP)
      * `POST /v1/chat/completions`   (Image-to-Image / multi-reference editing up to 10 images,
                                       accepting `data:image/...`, `https://...`, or `drive://...`)
      * `POST /v1/images/edits` & `POST /api/ct/edit` (OpenAI Images-style JSON / multipart edit)
      * `POST /api/ct/upsample`       (Prompt expansion via optional `flux-3-image-klein-pup`)
      * `GET  /api/ct/drive-list`     (List images, outputs, and LoRA adapters on Google Drive)
      * `GET  /api/ct/info` & `GET /v1/models` & `GET /health`
  - CLI (`generate`/`t2i`, `edit`/`i2i`, `upsample`, `ls-drive`, `models`) reads reference images
    directly from mounted Google Drive without client-side re-upload and saves generated/edited
    images directly to Google Drive (`--save-drive` or `-o drive://...`) and/or local disk.

Usage:
  colabllm flux-klein up --hf-token hf_xxx                  # Default G4 (96 GB, BF16) + quick tunnel
  colabllm flux-klein up --domain --enable-pup              # G4 + fixed domain + Qwen3.5-9B PUP
  colabllm flux-klein up --gpu H100 --domain --enable-pup   # H100 (80 GB HBM3) fastest single-image latency
  colabllm flux-klein up --gpu L4 --variant fp8r            # L4 (24 GB) low-cost lightweight tier (no PUP)
  colabllm flux-klein ls-drive                              # List images on Google Drive
  colabllm flux-klein generate "alpine meadow at golden hour" --save-drive outputs/meadow.png
  colabllm flux-klein edit "restyle as watercolor" --image drive://inputs/photo.jpg --save-drive outputs/wc.png
  colabllm flux-klein edit "dress person from image 1 in outfit from image 2" \\
      --drive-image inputs/person.jpg inputs/outfit.jpg --save-drive outputs/composite.png -o composite.png
  colabllm flux-klein down
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
from pathlib import Path
import re
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from cli_support import UsageError, http_timeout, nonnegative_seconds, resolve_endpoint, safe_urlopen  # noqa: E402
from colabsvc import (  # noqa: E402
    Service,
    State,
    die,
    log,
    main,
)

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
MAX_REFERENCE_IMAGES = 10
DRIVE_PREFIXES = ("drive://", "gdrive://", "MyDrive/", "/content/drive/")

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

SERVICE = Service(
    name="flux-klein",
    notebook="flux-klein.ipynb",
    service_port=8099,
    auth="builtin",
    public_port=8099,
    gpu="G4",
    high_mem=False,
    default_hostname="flux-klein",
    ready_timeout=3600,
    env={
        "CT_FLUX_MODEL": "black-forest-labs/flux-3-image-klein",
        "CT_FLUX_VARIANT": "bf16",
        "CT_FLUX_ALIAS": "flux-3-image-klein",
        "CT_FLUX_LORA_PATH": "",
        "CT_FLUX_LORA_SCALE": "1.0",
        "CT_FLUX_FP8_ATTENTION": "0",
        "CT_FLUX_ENABLE_PUP": "0",
        "CT_FLUX_PUP_GPU_MEM": "0.35",
    },
)

UP_ENV_FLAGS = {
    "--model": "CT_FLUX_MODEL",
    "--variant": "CT_FLUX_VARIANT",
    "--alias": "CT_FLUX_ALIAS",
    "--lora-path": "CT_FLUX_LORA_PATH",
    "--lora-scale": "CT_FLUX_LORA_SCALE",
    "--pup-gpu-mem": "CT_FLUX_PUP_GPU_MEM",
}
_BASE_ENV = dict(SERVICE.env)


def _get_endpoint(url_override: str | None = None, token_override: str | None = None) -> tuple[str, str]:
    return resolve_endpoint(
        State(SERVICE).load(),
        url_override,
        token_override,
        url_env="FLUX_KLEIN_URL",
        token_env="FLUX_KLEIN_API_TOKEN",
    )


def _http_json(
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    method: str | None = None,
    timeout: int | float | None = 300,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": BROWSER_UA,
        **(extra_headers or {}),
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method or ("POST" if data is not None else "GET"),
    )
    eff_timeout = http_timeout(timeout) if isinstance(timeout, (int, float)) else timeout
    try:
        with safe_urlopen(req, timeout=eff_timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            resp_hdrs = {k.lower(): v for k, v in resp.headers.items()}
            parsed = json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw}
            return resp.status, parsed, resp_hdrs
    except urllib.error.HTTPError as err:
        raw_err = err.read().decode("utf-8", errors="replace")
        resp_hdrs = {k.lower(): v for k, v in err.headers.items()} if err.headers else {}
        try:
            return err.code, json.loads(raw_err), resp_hdrs
        except Exception:
            return err.code, {"error": raw_err}, resp_hdrs


def _format_error(code: int, obj: dict[str, Any]) -> str:
    err = obj.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or json.dumps(err, ensure_ascii=False)
    elif err:
        msg = str(err)
    else:
        msg = json.dumps(obj, ensure_ascii=False)
    return f"HTTP {code}: {msg}"


def _is_drive_target(spec: str | None) -> bool:
    if not spec:
        return False
    s = spec.strip()
    return s.startswith(DRIVE_PREFIXES)


def _normalize_drive_spec(spec: str) -> str:
    s = spec.strip()
    if s.startswith(("drive://", "gdrive://", "/content/drive/")):
        return s
    return f"drive://{s}"


def _encode_local_image_to_data_url(path: Path) -> str:
    raw = path.read_bytes()
    if not raw:
        raise UsageError(f"Local image file is empty: {path}")
    ext = path.suffix.lower()
    mime = MIME_BY_EXT.get(ext) or mimetypes.guess_type(str(path))[0] or "image/png"
    if not mime.startswith("image/"):
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _resolve_reference_image_spec(spec: str, *, force_drive: bool = False) -> tuple[str, str]:
    """Return (resolved_url_or_data_uri, source_label) for a local, URL, or Google Drive image."""
    s = (spec or "").strip()
    if not s:
        raise UsageError("Reference image path must not be empty")
    if force_drive or _is_drive_target(s):
        d = _normalize_drive_spec(s)
        return d, f"drive ({d})"
    if s.startswith(("http://", "https://", "data:image/")):
        label = "data-uri" if s.startswith("data:") else s
        return s, label
    local_p = Path(s).expanduser()
    if local_p.is_file():
        return _encode_local_image_to_data_url(local_p), f"local ({local_p})"
    # Non-existent locally -> resolve on mounted Google Drive inside Colab VM
    d = _normalize_drive_spec(s)
    return d, f"drive ({d})"


def _flatten_groups(groups: list[list[str]] | None) -> list[str]:
    if not groups:
        return []
    return [item for grp in groups for item in grp if item and item.strip()]


class _OrderedImageAction(argparse.Action):
    """Preserve exact CLI argument order across mixed --image and --drive-image flags."""

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: Any, option_string: str | None = None) -> None:
        force_drive = option_string == "--drive-image"
        items = values if isinstance(values, list) else [values]
        ordered: list[tuple[str, bool]] = list(getattr(namespace, "ordered_images", None) or [])
        for raw in items:
            if raw and str(raw).strip():
                ordered.append((str(raw).strip(), force_drive))
        setattr(namespace, "ordered_images", ordered)


def _collect_ordered_images(args: argparse.Namespace) -> list[tuple[str, bool]]:
    ordered = getattr(args, "ordered_images", None)
    if ordered:
        return list(ordered)
    # Fallback if Namespace was constructed manually in Python
    out: list[tuple[str, bool]] = []
    for s in _flatten_groups(getattr(args, "image", None)):
        out.append((s, False))
    for s in _flatten_groups(getattr(args, "drive_image", None)):
        out.append((s, True))
    return out


def _resolve_prompt(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if getattr(args, "prompt_opt", None):
        parts.append(str(args.prompt_opt).strip())
    if getattr(args, "prompt", None):
        parts.append(str(args.prompt).strip())
    if getattr(args, "prompt_file", None):
        pf = Path(args.prompt_file).expanduser()
        if not pf.is_file():
            raise UsageError(f"Prompt file not found: {pf}")
        parts.append(pf.read_text(encoding="utf-8").strip())
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        raise UsageError("Missing prompt: pass a prompt argument, `--prompt`, or `--prompt-file FILE`")
    return text


def _load_boxes_arg(boxes_arg: str | None) -> Any:
    if not boxes_arg:
        return None
    s = boxes_arg.strip()
    if s.startswith("@"):
        bp = Path(s[1:]).expanduser()
        if not bp.is_file():
            raise UsageError(f"Boxes JSON file not found: {bp}")
        s = bp.read_text(encoding="utf-8").strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError as err:
        raise UsageError(f"Invalid --boxes JSON: {err}") from err


def _parse_dimensions(args: argparse.Namespace, *, default_size: str | None) -> tuple[int | None, int | None]:
    size_val = getattr(args, "size", None)
    width_val = getattr(args, "width", None)
    height_val = getattr(args, "height", None)
    if size_val:
        m = re.fullmatch(r"\s*(\d+)\s*[xX:]\s*(\d+)\s*", str(size_val))
        if not m:
            raise UsageError(f"Invalid --size '{size_val}': expected WIDTHxHEIGHT (e.g. 1024x1024)")
        w, h = int(m.group(1)), int(m.group(2))
    elif width_val is not None or height_val is not None:
        w = int(width_val if width_val is not None else 1024)
        h = int(height_val if height_val is not None else 1024)
    elif default_size:
        w_s, h_s = default_size.split("x", 1)
        w, h = int(w_s), int(h_s)
    else:
        return None, None

    if w < 64 or h < 64 or w > 4096 or h > 4096 or w % 16 != 0 or h % 16 != 0:
        raise UsageError(f"Invalid resolution {w}x{h}: width and height must be multiples of 16 in [64, 4096]")
    return w, h


def _resolve_output_targets(args: argparse.Namespace, *, prefix: str) -> tuple[Path | None, str, bool]:
    """Return (local_output_path_or_None, drive_save_spec, drive_only_flag)."""
    raw_out = getattr(args, "output", None)
    raw_save_drive = getattr(args, "save_drive", None)
    explicit_drive_only = bool(getattr(args, "drive_only", False))

    drive_spec = ""
    local_out: Path | None = None

    if raw_out and _is_drive_target(raw_out):
        # `-o drive://outputs/foo.png` saves directly on Google Drive
        drive_spec = raw_out.strip()
        if raw_save_drive and raw_save_drive not in ("auto",):
            drive_spec = raw_save_drive.strip()
    else:
        if raw_save_drive:
            drive_spec = raw_save_drive.strip()
        if raw_out:
            local_out = Path(raw_out).expanduser()

    if explicit_drive_only:
        if not drive_spec:
            drive_spec = "auto"
        return None, drive_spec, True

    if drive_spec and local_out is None:
        # User specified `--save-drive ...` (or `-o drive://...`) without a local `-o`: save directly on Drive only
        return None, drive_spec, True

    if not drive_spec and local_out is None:
        stamp = f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        local_out = Path(stamp)

    return local_out, drive_spec, False


def _write_local_images(images_b64: list[str], target: Path, prefix: str) -> list[Path]:
    written: list[Path] = []
    is_dir = target.is_dir() or str(target).endswith("/") or (len(images_b64) > 1 and target.suffix == "")
    if is_dir:
        target.mkdir(parents=True, exist_ok=True)
        stamp = f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}"
        for idx, b64 in enumerate(images_b64, 1):
            suffix = f"-{idx:02d}.png" if len(images_b64) > 1 else ".png"
            out_p = target / f"{stamp}{suffix}"
            out_p.write_bytes(base64.b64decode(b64))
            written.append(out_p)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        ext = target.suffix or ".png"
        stem = target.stem if target.suffix else target.name
        for idx, b64 in enumerate(images_b64, 1):
            out_p = target.parent / (f"{stem}-{idx:02d}{ext}" if len(images_b64) > 1 else f"{stem}{ext}")
            out_p.write_bytes(base64.b64decode(b64))
            written.append(out_p)
    return written


def _build_lora_dict(args: argparse.Namespace) -> dict[str, Any] | None:
    lpath = getattr(args, "lora", None)
    lname = getattr(args, "lora_name", None)
    lscale = getattr(args, "lora_scale", 1.0)
    if lname and not lpath:
        raise UsageError("--lora-name requires --lora")
    if not lpath:
        return None
    spec = lpath.strip()
    return {
        "path": spec,
        "name": lname or Path(spec.removeprefix("drive://").removeprefix("gdrive://")).stem,
        "scale": float(lscale),
    }


def _apply_common_sampling(payload: dict[str, Any], args: argparse.Namespace) -> None:
    if args.seed is not None:
        payload["seed"] = int(args.seed)
    if args.steps is not None:
        payload["num_inference_steps"] = int(args.steps)
    if args.flow_shift is not None:
        payload["flow_shift"] = float(args.flow_shift)
    if args.guidance_scale is not None:
        payload["guidance_scale"] = float(args.guidance_scale)
    if args.negative_prompt:
        payload["negative_prompt"] = str(args.negative_prompt)
    lora_cfg = _build_lora_dict(args)
    if lora_cfg:
        payload["lora"] = lora_cfg
    if args.upsample:
        payload["upsample"] = True
    if args.bbox:
        payload["bbox"] = True
    boxes = _load_boxes_arg(args.boxes)
    if boxes is not None:
        payload["boxes"] = boxes


def cmd_generate(args: argparse.Namespace) -> None:
    prompt = _resolve_prompt(args)
    w, h = _parse_dimensions(args, default_size="1024x1024")
    local_out, drive_spec, drive_only = _resolve_output_targets(args, prefix="flux-t2i")
    base_url, token = _get_endpoint(args.url, args.api_token)

    payload: dict[str, Any] = {
        "model": args.model,
        "prompt": prompt,
        "size": f"{w}x{h}",
        "n": int(args.num_images),
        "response_format": "b64_json",
    }
    _apply_common_sampling(payload, args)
    if drive_spec:
        payload["save_to_drive"] = drive_spec
    if drive_only:
        payload["drive_only"] = True

    t0 = time.monotonic()
    code, res, hdrs = _http_json(
        f"{base_url}/v1/images/generations",
        token,
        payload=payload,
        timeout=args.timeout,
    )
    elapsed = time.monotonic() - t0
    if code != 200:
        die(_format_error(code, res))

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return

    data_items = res.get("data") or []
    b64_list = [it["b64_json"] for it in data_items if isinstance(it, dict) and it.get("b64_json")]
    local_written: list[Path] = []
    if local_out is not None and b64_list:
        local_written = _write_local_images(b64_list, local_out, prefix="flux-t2i")

    saved_drive = res.get("saved_drive_paths") or (
        [hdrs["x-saved-drive-path"]] if hdrs.get("x-saved-drive-path") else []
    )
    rel_drive = res.get("rel_drive_paths") or saved_drive

    if res.get("revised_prompt"):
        log(f"upsampled prompt: {res['revised_prompt']}")
    for lp in local_written:
        log(f"saved local image: {lp} ({lp.stat().st_size / 1024:.1f} KB, {elapsed:.2f}s)")
    for dp in rel_drive:
        log(f"saved Google Drive image: {dp} ({elapsed:.2f}s)")


def cmd_edit(args: argparse.Namespace) -> None:
    prompt = _resolve_prompt(args)
    ordered_specs = _collect_ordered_images(args)
    total_refs = len(ordered_specs)
    if total_refs == 0:
        raise UsageError("At least 1 reference image is required: pass `--image PATH` or `--drive-image DRIVE_PATH`")
    if total_refs > MAX_REFERENCE_IMAGES:
        raise UsageError(f"At most {MAX_REFERENCE_IMAGES} reference images are supported (got {total_refs})")

    resolved_urls: list[str] = []
    source_labels: list[str] = []
    for spec, force_drive in ordered_specs:
        u, lbl = _resolve_reference_image_spec(spec, force_drive=force_drive)
        resolved_urls.append(u)
        source_labels.append(lbl)

    w, h = _parse_dimensions(args, default_size=None)
    local_out, drive_spec, drive_only = _resolve_output_targets(args, prefix="flux-edit")
    base_url, token = _get_endpoint(args.url, args.api_token)

    if args.endpoint == "chat":
        payload: dict[str, Any] = {
            "model": args.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        *[{"type": "image_url", "image_url": {"url": u}} for u in resolved_urls],
                    ],
                }
            ],
        }
        if w is not None and h is not None:
            payload["width"] = w
            payload["height"] = h
        target_path = "/v1/chat/completions"
    else:
        payload = {
            "model": args.model,
            "prompt": prompt,
            "images": resolved_urls,
        }
        if w is not None and h is not None:
            payload["size"] = f"{w}x{h}"
        target_path = "/api/ct/edit"

    _apply_common_sampling(payload, args)
    if drive_spec:
        payload["save_to_drive"] = drive_spec
    if drive_only:
        payload["drive_only"] = True

    if not args.json:
        log(f"editing with {len(resolved_urls)} reference image(s): {', '.join(source_labels)}")

    t0 = time.monotonic()
    code, res, hdrs = _http_json(
        f"{base_url}{target_path}",
        token,
        payload=payload,
        timeout=args.timeout,
    )
    elapsed = time.monotonic() - t0
    if code != 200:
        die(_format_error(code, res))

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return

    b64_list: list[str] = []
    if args.endpoint == "chat":
        for ch in res.get("choices") or []:
            content = (ch.get("message") or {}).get("content") or []
            for part in content if isinstance(content, list) else []:
                u = ((part.get("image_url") or {}).get("url") or "") if isinstance(part, dict) else ""
                if u.startswith("data:image/") and "," in u:
                    b64_list.append(u.split(",", 1)[1])
    else:
        for it in res.get("data") or []:
            if isinstance(it, dict) and it.get("b64_json"):
                b64_list.append(it["b64_json"])

    local_written: list[Path] = []
    if local_out is not None and b64_list:
        local_written = _write_local_images(b64_list, local_out, prefix="flux-edit")

    saved_drive = res.get("saved_drive_paths") or (
        [hdrs["x-saved-drive-path"]] if hdrs.get("x-saved-drive-path") else []
    )
    rel_drive = res.get("rel_drive_paths") or saved_drive

    revised = res.get("revised_prompt") or ((res.get("data") or [{}])[0].get("revised_prompt") if res.get("data") else None)
    if revised:
        log(f"upsampled prompt: {revised}")
    for lp in local_written:
        log(f"saved local edited image: {lp} ({lp.stat().st_size / 1024:.1f} KB, {elapsed:.2f}s)")
    for dp in rel_drive:
        log(f"saved Google Drive edited image: {dp} ({elapsed:.2f}s)")


def cmd_upsample(args: argparse.Namespace) -> None:
    prompt = _resolve_prompt(args)
    ordered_specs = _collect_ordered_images(args)
    resolved_urls = [
        _resolve_reference_image_spec(s, force_drive=fd)[0]
        for s, fd in ordered_specs
    ]
    if len(resolved_urls) > MAX_REFERENCE_IMAGES:
        raise UsageError(f"At most {MAX_REFERENCE_IMAGES} reference images are supported")

    payload: dict[str, Any] = {
        "prompt": prompt,
        "images": resolved_urls,
        "bbox": bool(args.bbox),
        "max_tokens": int(args.max_tokens),
    }
    if args.canvas:
        payload["canvas"] = args.canvas
    boxes = _load_boxes_arg(args.boxes)
    if boxes is not None:
        payload["boxes"] = boxes

    base_url, token = _get_endpoint(args.url, args.api_token)
    code, res, _ = _http_json(f"{base_url}/api/ct/upsample", token, payload=payload, timeout=args.timeout)
    if code != 200:
        die(_format_error(code, res))
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return
    print(res.get("upsampled_prompt", ""))


def cmd_ls_drive(args: argparse.Namespace) -> None:
    base_url, token = _get_endpoint(args.url, args.api_token)
    kind = args.kind
    if args.loras:
        kind = "loras"
    elif args.outputs:
        kind = "outputs"
    elif args.all:
        kind = "all"

    qs = urllib.parse.urlencode({
        "path": args.path or "",
        "kind": kind,
        "recursive": "0" if args.no_recursive else "1",
    })
    code, res, _ = _http_json(f"{base_url}/api/ct/drive-list?{qs}", token, timeout=60)
    if code != 200:
        die(_format_error(code, res))
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return
    files = res.get("files") or []
    log(f"Google Drive mounted={res.get('drive_mounted')} kind={kind} matched={len(files)}")
    for item in files:
        print(f"  {item.get('rel_path', item.get('drive_path'))}  [{item.get('type', 'image')}, {item.get('size_mb', 0):.2f} MB]")


def cmd_models(args: argparse.Namespace) -> None:
    base_url, token = _get_endpoint(args.url, args.api_token)
    code_i, info_res, _ = _http_json(f"{base_url}/api/ct/info", token, timeout=30)
    code_m, mod_res, _ = _http_json(f"{base_url}/v1/models", token, timeout=30)
    if code_i != 200 and code_m != 200:
        die(_format_error(code_i, info_res))
    combined = {"info": info_res, "models": [m.get("id") for m in (mod_res.get("data") or []) if isinstance(m, dict)]}
    if args.json:
        print(json.dumps(combined, indent=2, ensure_ascii=False))
        return
    print(f"Service:            {info_res.get('service', 'flux-klein')}")
    print(f"Model repo:         {info_res.get('model_repo')}")
    print(f"Served names:       {', '.join(combined['models'] or info_res.get('served_models') or [])}")
    print(f"Variant:            {info_res.get('variant')} (distilled={info_res.get('is_distilled')})")
    print(f"Default sampling:   steps={info_res.get('default_steps')} shift={info_res.get('default_flow_shift')} size={info_res.get('default_size')}")
    print(f"Prompt upsampler:   enabled={info_res.get('pup_enabled')} ready={info_res.get('pup_ready')}")
    print(f"Preloaded LoRA:     {info_res.get('preloaded_lora') or 'none'}")
    print(f"Drive inputs dir:   {info_res.get('drive_inputs_dir')}")
    print(f"Drive outputs dir:  {info_res.get('drive_outputs_dir')}")
    print(f"Drive loras dir:    {info_res.get('drive_loras_dir')}")


def _add_endpoint_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--url", help="Override endpoint URL (or set FLUX_KLEIN_URL)")
    p.add_argument("--api-token", help="Override Bearer API token (or set FLUX_KLEIN_API_TOKEN)")


def _add_generation_common_flags(p: argparse.ArgumentParser, *, is_edit: bool) -> None:
    p.add_argument("prompt", nargs="?", help="Generation / editing prompt")
    p.add_argument("-p", "--prompt", dest="prompt_opt", help="Prompt text (alternative to positional argument)")
    p.add_argument("--prompt-file", help="Read prompt text from a UTF-8 file")
    size_help = (
        "Output resolution WIDTHxHEIGHT (multiples of 16; omit to preserve first reference image aspect ratio)"
        if is_edit
        else "Output resolution WIDTHxHEIGHT (multiples of 16, default: 1024x1024)"
    )
    p.add_argument("--size", help=size_help)
    p.add_argument("--width", type=int, help="Output width in pixels (multiple of 16)")
    p.add_argument("--height", type=int, help="Output height in pixels (multiple of 16)")
    p.add_argument("--steps", type=int, help="Sampling steps override (distilled default: 16; base default: 50)")
    p.add_argument("--flow-shift", type=float, help="Timestep flow shift alpha override (default: 6.93)")
    p.add_argument("--guidance-scale", type=float, help="CFG scale (base model only; rejected by distilled model)")
    p.add_argument("--negative-prompt", default="", help="Negative prompt (base model only; rejected by distilled model)")
    p.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    p.add_argument("--lora", dest="lora", help="LoRA adapter on Google Drive (e.g. drive://loras/style.safetensors) or staged server path")
    p.add_argument("--lora-name", help="Optional adapter label (defaults to LoRA filename stem)")
    p.add_argument("--lora-scale", type=float, default=1.0, help="Per-request LoRA scale (default: 1.0)")
    p.add_argument("-u", "--upsample", action="store_true", help="Expand prompt via co-located flux-3-image-klein-pup (--enable-pup)")
    p.add_argument("--bbox", action="store_true", help="Use structured bounding-box PUP prompt (*_bbox)")
    p.add_argument("--boxes", help="JSON box array (or @file.json) for grounded PUP layout")
    p.add_argument(
        "-o", "--output",
        help="Local output path (file or directory) OR Google Drive path (drive://outputs/out.png)",
    )
    p.add_argument(
        "--save-drive",
        nargs="?",
        const="auto",
        help="Save output directly on mounted Google Drive (optional path, default: colab-llm/flux-klein/outputs/)",
    )
    p.add_argument(
        "--drive-only", "--no-local",
        dest="drive_only",
        action="store_true",
        help="Save only on Google Drive and skip downloading image bytes locally",
    )
    p.add_argument("--model", default="flux-3-image-klein", help="Served model name (default: flux-3-image-klein)")
    p.add_argument(
        "--timeout",
        type=nonnegative_seconds,
        default=300,
        help="HTTP request timeout in seconds; 0 = unlimited (default: 300)",
    )
    p.add_argument("--json", action="store_true", help="Print raw JSON response")
    _add_endpoint_flags(p)


def _build_custom_parser(_parser: argparse.ArgumentParser, sub: Any) -> None:
    for cmd_name, help_msg in (
        ("generate", "Generate image(s) from text (/v1/images/generations) with optional Google Drive output"),
        ("t2i", "Alias for 'generate' (Text-to-Image)"),
    ):
        gp = sub.add_parser(cmd_name, help=help_msg)
        gp.add_argument("-n", "--num-images", type=int, default=1, help="Number of images to generate (default: 1)")
        _add_generation_common_flags(gp, is_edit=False)

    for cmd_name, help_msg in (
        ("edit", "Edit or combine 1-10 local/URL/Google Drive reference images (/api/ct/edit or /v1/chat/completions)"),
        ("i2i", "Alias for 'edit' (Image-to-Image / multi-reference editing)"),
    ):
        ep = sub.add_parser(cmd_name, help=help_msg)
        ep.add_argument(
            "-r", "--image", "--ref",
            dest="ordered_images",
            action=_OrderedImageAction,
            nargs="+",
            help="Reference image(s) in order (1-10): local file, https:// URL, drive:// path, or Drive relative path",
        )
        ep.add_argument(
            "--drive-image",
            dest="ordered_images",
            action=_OrderedImageAction,
            nargs="+",
            help="Reference image(s) resolved strictly on mounted Google Drive (zero client upload)",
        )
        ep.add_argument(
            "--endpoint",
            choices=("edit", "chat"),
            default="edit",
            help="Target API route: 'edit' (/api/ct/edit) or 'chat' (/v1/chat/completions) (default: edit)",
        )
        _add_generation_common_flags(ep, is_edit=True)

    up_p = sub.add_parser("upsample", help="Expand a short T2I or I2I prompt via flux-3-image-klein-pup (/api/ct/upsample)")
    up_p.add_argument("prompt", nargs="?", help="Short prompt to upsample")
    up_p.add_argument("-p", "--prompt", dest="prompt_opt", help="Short prompt (alternative to positional argument)")
    up_p.add_argument("--prompt-file", help="Read prompt from a UTF-8 file")
    up_p.add_argument("-r", "--image", "--ref", dest="ordered_images", action=_OrderedImageAction, nargs="+", help="Optional reference image(s) (local, URL, or Drive)")
    up_p.add_argument("--drive-image", dest="ordered_images", action=_OrderedImageAction, nargs="+", help="Optional reference image(s) on mounted Google Drive")
    up_p.add_argument("--bbox", action="store_true", help="Return caption + JSON bounding-box table (*_bbox)")
    up_p.add_argument("--boxes", help="JSON array of bounding boxes (or @file.json) to honor")
    up_p.add_argument("--canvas", help="Target T2I canvas ratio W:H (e.g. 1024:1024 or 1536:1024)")
    up_p.add_argument("--max-tokens", type=int, default=3584, help="Max tokens for PUP generation (default: 3584)")
    up_p.add_argument("--timeout", type=nonnegative_seconds, default=300, help="HTTP timeout in seconds (default: 300)")
    up_p.add_argument("--json", action="store_true", help="Print raw JSON response")
    _add_endpoint_flags(up_p)

    lsd = sub.add_parser("ls-drive", help="List images, generated outputs, or LoRA adapters on mounted Google Drive")
    lsd.add_argument("path", nargs="?", default="", help="Drive folder or glob (e.g. inputs/*.jpg, MyDrive/photos)")
    lsd.add_argument("--kind", choices=("images", "outputs", "loras", "all"), default="images", help="File category to list (default: images)")
    lsd.add_argument("--loras", action="store_true", help="Shortcut for --kind loras (list .safetensors & PEFT dirs)")
    lsd.add_argument("--outputs", action="store_true", help="Shortcut for --kind outputs (list generated images)")
    lsd.add_argument("--all", action="store_true", help="Shortcut for --kind all")
    lsd.add_argument("--no-recursive", action="store_true", help="Do not recurse into subdirectories")
    lsd.add_argument("--json", action="store_true", help="Print raw JSON response")
    _add_endpoint_flags(lsd)

    mp = sub.add_parser("models", help="Show active FLUX 3 model, variant, PUP status & Google Drive directories")
    mp.add_argument("--json", action="store_true", help="Print raw JSON response")
    _add_endpoint_flags(mp)


def configure_cli(parser: argparse.ArgumentParser, commands: Any, up: argparse.ArgumentParser) -> None:
    _build_custom_parser(parser, commands)
    up.add_argument(
        "--model",
        dest="up_CT_FLUX_MODEL",
        help="Hugging Face repo (default: black-forest-labs/flux-3-image-klein; or black-forest-labs/flux-3-image-klein-base)",
    )
    up.add_argument(
        "--variant",
        dest="up_CT_FLUX_VARIANT",
        choices=("bf16", "fp8r", "nvfp4"),
        help="DiT precision package variant: bf16 (default, ~19 GB VRAM), fp8r (~17 GB VRAM), nvfp4 (SM100+, ~13 GB VRAM)",
    )
    up.add_argument(
        "--alias",
        dest="up_CT_FLUX_ALIAS",
        help="Served model alias (default: flux-3-image-klein)",
    )
    up.add_argument(
        "--lora-path",
        dest="up_CT_FLUX_LORA_PATH",
        help="Preload a LoRA adapter at server startup (supports drive://loras/adapter.safetensors)",
    )
    up.add_argument(
        "--lora-scale",
        dest="up_CT_FLUX_LORA_SCALE",
        help="Default scale for preloaded LoRA adapter (default: 1.0)",
    )
    up.add_argument(
        "--fp8-attention",
        dest="up_fp8_attention",
        action="store_true",
        help="Enable FlashInfer/cuDNN FP8 DiT attention (SM100 GPUs with cuDNN >= 9.17.1)",
    )
    up.add_argument(
        "--enable-pup",
        dest="up_enable_pup",
        action="store_true",
        help="Co-locate black-forest-labs/flux-3-image-klein-pup (Qwen3.5-9B prompt upsampler) on :8098",
    )
    up.add_argument(
        "--pup-gpu-mem",
        dest="up_CT_FLUX_PUP_GPU_MEM",
        help="GPU memory utilization for PUP worker when --enable-pup is set (default: 0.35)",
    )


def prepare_up(args: argparse.Namespace) -> None:
    SERVICE.env.clear()
    SERVICE.env.update(_BASE_ENV)
    for _flag, env_key in UP_ENV_FLAGS.items():
        val = getattr(args, "up_" + env_key, None)
        if val is not None:
            SERVICE.env[env_key] = str(val)
    if getattr(args, "up_fp8_attention", False):
        SERVICE.env["CT_FLUX_FP8_ATTENTION"] = "1"
    if getattr(args, "up_enable_pup", False):
        SERVICE.env["CT_FLUX_ENABLE_PUP"] = "1"


if __name__ == "__main__":
    from cli_support import run_cli

    run_cli(
        lambda: main(
            SERVICE,
            configure=configure_cli,
            prepare_up=prepare_up,
            handlers={
                "generate": cmd_generate,
                "t2i": cmd_generate,
                "edit": cmd_edit,
                "i2i": cmd_edit,
                "upsample": cmd_upsample,
                "ls-drive": cmd_ls_drive,
                "models": cmd_models,
            },
        )
    )
