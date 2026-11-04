"""On-demand workflow and model downloader for ComfyUI on local NVMe SSD."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import site
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote

COMFY_DIR = Path(os.environ.get("CT_COMFY_DIR", "/content/ComfyUI"))
HF_URL_RE = re.compile(r"^https?://huggingface\.co/([^/]+/[^/]+)/(?:resolve|blob)/([^/]+)/(.+?)(?:\?.*)?$")
MD_LINK_RE = re.compile(r"\[([^\]]+\.(?:safetensors|ckpt|pt|pth|bin|gguf|sft))\]\((https?://[^)\s]+)\)", re.I)
MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft")
NODE_TYPE_DIR = {
    "CheckpointLoaderSimple": "checkpoints", "CheckpointLoader": "checkpoints", "unCLIPCheckpointLoader": "checkpoints",
    "ImageOnlyCheckpointLoader": "checkpoints", "UNETLoader": "diffusion_models",
    "DiffusionModelLoader": "diffusion_models", "CLIPLoader": "text_encoders", "DualCLIPLoader": "text_encoders",
    "TripleCLIPLoader": "text_encoders", "QuadrupleCLIPLoader": "text_encoders", "VAELoader": "vae",
    "LoraLoader": "loras", "LoraLoaderModelOnly": "loras", "ControlNetLoader": "controlnet",
    "DiffControlNetLoader": "controlnet", "CLIPVisionLoader": "clip_vision", "StyleModelLoader": "style_models",
    "UpscaleModelLoader": "upscale_models", "HypernetworkLoader": "hypernetworks", "GLIGENLoader": "gligen",
    "ModelPatchLoader": "model_patches", "AudioEncoderLoader": "audio_encoders",
}
TYPE_SAVE_DIR = {
    "checkpoint": "checkpoints", "unclip": "checkpoints", "diffusion_model": "diffusion_models",
    "clip": "text_encoders", "vae": "vae", "VAE": "vae", "lora": "loras", "controlnet": "controlnet",
    "T2I-Adapter": "controlnet", "clip_vision": "clip_vision", "upscale": "upscale_models",
    "embedding": "embeddings", "gligen": "gligen", "photomaker": "photomaker", "model_patch": "model_patches",
}
_MANAGER_INDEX: dict[str, dict[str, str]] | None = None


def _cleanup_dl_cache() -> None:
    for p in ("/content/hf-stage", "/root/.cache/huggingface/xet", "/root/.cache/huggingface/hub"):
        shutil.rmtree(p, ignore_errors=True)


def validate_model_destination(directory: str, name: str, comfy_dir: Path | None = None) -> Path:
    """Resolve and validate that <comfy_dir>/models/<directory>/<name> stays strictly inside models/."""
    base = (comfy_dir or COMFY_DIR)
    models_root = (base / "models").resolve()
    rel_dir = (directory or "").strip().strip("/")
    rel_name = (name or "").strip().lstrip("/")
    if not rel_dir or not rel_name or "\x00" in rel_dir or "\x00" in rel_name:
        raise ValueError(f"Invalid model destination: directory={directory!r}, name={name!r}")
    if ".." in Path(rel_dir).parts or ".." in Path(rel_name).parts:
        raise ValueError(f"Refusing path traversal in model destination: {rel_dir}/{rel_name}")
    dest = (models_root / rel_dir / rel_name).resolve()
    if not dest.is_relative_to(models_root) or dest == models_root:
        raise ValueError(f"Refusing model path outside {models_root}: {rel_dir}/{rel_name}")
    return dest


def _get_manager_index() -> dict[str, dict[str, str]]:
    global _MANAGER_INDEX
    if _MANAGER_INDEX is not None:
        return _MANAGER_INDEX
    _MANAGER_INDEX = {}
    candidates = [
        COMFY_DIR / "custom_nodes" / "ComfyUI-Manager" / "model-list.json",
        *[Path(sp) / "comfyui_manager" / "model-list.json" for sp in site.getsitepackages()],
    ]
    data = None
    for p in candidates:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                break
            except Exception:
                pass
    if data is None:
        try:
            req = urllib.request.Request(
                "https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager/main/model-list.json",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
        except Exception:
            data = {}
    for m in data.get("models", []):
        fn = m.get("filename")
        url = m.get("url")
        if not fn or not url:
            continue
        sp = m.get("save_path") or ""
        if not sp or sp == "default":
            sp = TYPE_SAVE_DIR.get(m.get("type", ""), "checkpoints")
        _MANAGER_INDEX[fn] = {"directory": sp.strip("/"), "name": fn, "url": url}
    return _MANAGER_INDEX


def resolve_workflow(wf_source: str, data_dir: str | Path | None = None) -> tuple[str, str, str]:
    spec = wf_source.strip()
    m = re.match(r"^https?://github\.com/([^/]+/[^/]+)/blob/(.+)$", spec)
    if m:
        spec = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}"
    if spec.startswith(("http://", "https://")):
        req = urllib.request.Request(spec, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        return Path(spec.split("?")[0]).name, raw, spec

    d_root = Path(data_dir) if data_dir else Path(os.environ.get("CT_DATA_DIR", "/content/drive/MyDrive/colab-llm/comfyui"))
    allowed_wf_roots = [
        r.resolve()
        for r in (COMFY_DIR, d_root, Path("/content/drive/MyDrive"))
        if r.exists()
    ]
    for sp in site.getsitepackages():
        for pkg_dir in glob.glob(f"{sp}/comfyui_workflow_templates*/templates"):
            allowed_wf_roots.append(Path(pkg_dir).resolve())

    if os.path.isfile(spec):
        p = Path(spec).resolve()
        if not any(p.is_relative_to(r) for r in allowed_wf_roots):
            raise ValueError(f"Workflow path '{spec}' is outside allowed workflow directories")
        return p.name, p.read_text(encoding="utf-8"), str(p)

    # Bare filename / template name:
    # 1) Try official Comfy-Org/workflow_templates online
    safe_base = Path(spec).name
    if ".." in Path(spec).parts or not safe_base:
        raise ValueError(f"Invalid workflow name: {wf_source!r}")
    name = spec if spec.endswith(".json") else f"{spec}.json"
    gh_url = f"https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/{name}"
    try:
        req = urllib.request.Request(gh_url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8")
        return Path(name).name, raw, gh_url
    except Exception as net_err:
        print(f"[workflow-dl] Online lookup ({gh_url}) failed ({net_err}); searching Drive & local workflow dirs...",
              flush=True)

    # 2) Fallback: search Google Drive workflow directories & local installed templates
    search_dirs = [
        COMFY_DIR / "user" / "default" / "workflows",
        d_root / "user" / "default" / "workflows",
        d_root / "workflows",
        COMFY_DIR / "user",
        d_root / "user",
    ]
    for sp in site.getsitepackages():
        for pkg_dir in glob.glob(f"{sp}/comfyui_workflow_templates*/templates"):
            search_dirs.append(Path(pkg_dir))
    for sdir in search_dirs:
        if not sdir.is_dir():
            continue
        sdir_resolved = sdir.resolve()
        direct = (sdir / name).resolve()
        if direct.is_relative_to(sdir_resolved) and direct.is_file():
            return direct.name, direct.read_text(encoding="utf-8"), str(direct)
        for hit in sdir.rglob(Path(name).name):
            if hit.is_file():
                return hit.name, hit.read_text(encoding="utf-8"), str(hit)
    raise FileNotFoundError(f"Workflow '{wf_source}' not found online ({gh_url}) or in Drive workflow directories")


def extract_workflow_models(wf_json: Any) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    md_urls: dict[str, str] = {}
    missing_widgets: list[tuple[str, str]] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            ntype = obj.get("type", "")
            props = obj.get("properties")
            if isinstance(props, dict) and isinstance(props.get("models"), list):
                for m in props["models"]:
                    if isinstance(m, dict) and m.get("url") and m.get("directory") and m.get("name"):
                        d, n = str(m["directory"]).strip("/"), str(m["name"]).lstrip("/")
                        validate_model_destination(d, n)
                        found[f"{d}/{n}"] = {"directory": d, "name": n, "url": str(m["url"]).strip(), "source": "properties.models"}
            wvals = obj.get("widgets_values")
            if isinstance(wvals, list):
                for v in wvals:
                    if isinstance(v, str):
                        for fn, link in MD_LINK_RE.findall(v):
                            clean_link = link.replace("/blob/", "/resolve/")
                            md_urls[Path(fn).name] = clean_link
                        if v.lower().endswith(MODEL_EXTS) and "\n" not in v and " " not in v and len(v) < 200:
                            missing_widgets.append((ntype, v.strip()))
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(wf_json)
    by_name = {Path(m["name"]).name: m for m in found.values()}

    for ntype, raw_val in missing_widgets:
        fname = Path(raw_val).name
        if fname in by_name:
            continue
        if fname in md_urls:
            url = md_urls[fname]
            d = NODE_TYPE_DIR.get(ntype)
            if not d:
                hm = HF_URL_RE.match(url)
                if hm:
                    parts = unquote(hm.group(3)).split("/")
                    if len(parts) > 1:
                        d = parts[0]
            d = d or "diffusion_models"
            validate_model_destination(d, raw_val)
            item = {"directory": d, "name": raw_val, "url": url, "source": "markdown_link"}
            found[f"{d}/{raw_val}"] = item
            by_name[fname] = item
            continue
        mgr_idx = _get_manager_index()
        if fname in mgr_idx:
            info = mgr_idx[fname]
            d = NODE_TYPE_DIR.get(ntype) or info["directory"]
            validate_model_destination(d, raw_val)
            item = {"directory": d, "name": raw_val, "url": info["url"], "source": "model-list.json"}
            found[f"{d}/{raw_val}"] = item
            by_name[fname] = item

    return found


def _stream_http_download(url: str, dest: Path, emit: Any) -> None:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Unsupported model URL scheme: {url!r}")
    headers = {"User-Agent": "Mozilla/5.0"}
    hf_tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if hf_tok and "huggingface.co" in url:
        headers["Authorization"] = f"Bearer {hf_tok}"
    req = urllib.request.Request(url, headers=headers)
    partial = dest.with_name(dest.name + ".partial")
    partial.unlink(missing_ok=True)
    with urllib.request.urlopen(req, timeout=120) as resp, open(partial, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        copied = 0
        t_start = last_log = time.monotonic()
        while True:
            chunk = resp.read(16 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            copied += len(chunk)
            now = time.monotonic()
            if now - last_log >= 10:
                speed = copied / 1e6 / max(now - t_start, 0.1)
                if total > 0:
                    emit(f"[workflow-dl]   ↳ {copied/1e9:.2f} / {total/1e9:.2f} GB ({copied*100/total:.1f}%) @ {speed:.1f} MB/s")
                else:
                    emit(f"[workflow-dl]   ↳ {copied/1e9:.2f} GB @ {speed:.1f} MB/s")
                last_log = now
    partial.replace(dest)


def ensure_model_on_ssd(item: dict[str, str], log_cb: Any = None, comfy_dir: Path | None = None) -> dict[str, Any]:
    def _emit(msg: str) -> None:
        print(msg, flush=True)
        if log_cb:
            log_cb(msg)

    base = comfy_dir or COMFY_DIR
    dest = validate_model_destination(item["directory"], item["name"], comfy_dir=base)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        _emit(f"[workflow-dl] SKIP (already on local SSD, {dest.stat().st_size/1e9:.2f} GB): {dest.relative_to(base.resolve())}")
        return {"model": f"{item['directory']}/{item['name']}", "status": "cached",
                "size": dest.stat().st_size, "source": item.get("source", "")}
    _cleanup_dl_cache()
    usage_target = str(base if base.exists() else Path("/content"))
    free_gb = shutil.disk_usage(usage_target).free / 1e9
    url = item["url"].replace("/blob/", "/resolve/")
    m = HF_URL_RE.match(url)
    t0 = time.monotonic()
    try:
        if m and free_gb > 45.0:
            repo_id, revision, filename = m.group(1), m.group(2), unquote(m.group(3))
            _emit(f"[workflow-dl] HF Xet -> local SSD (free: {free_gb:.1f} GB): {repo_id}/{filename} -> models/{item['directory']}/{item['name']}")
            dl_env = {**os.environ, "HF_XET_CHUNK_CACHE_SIZE_BYTES": "0"}
            dl_env.pop("HF_XET_HIGH_PERFORMANCE", None)
            code = (
                "import shutil, sys; from pathlib import Path; from huggingface_hub import hf_hub_download; "
                "stage = Path('/content/hf-stage'); stage.mkdir(parents=True, exist_ok=True); "
                "p = Path(hf_hub_download(repo_id=sys.argv[1], filename=sys.argv[2], revision=sys.argv[3], local_dir=stage)); "
                "shutil.move(str(p), sys.argv[4])"
            )
            proc = subprocess.run([sys.executable, "-c", code, repo_id, filename, revision, str(dest)], env=dl_env)
            if proc.returncode != 0 or not (dest.is_file() and dest.stat().st_size > 0):
                _cleanup_dl_cache()
                _emit(f"[workflow-dl] HF Xet subprocess exited with {proc.returncode}; falling back to direct HTTP stream...")
                _stream_http_download(url, dest, _emit)
        else:
            _emit(f"[workflow-dl] Direct HTTP stream -> local SSD (free: {free_gb:.1f} GB): {url} -> models/{item['directory']}/{item['name']}")
            _stream_http_download(url, dest, _emit)
    finally:
        _cleanup_dl_cache()
    dt = time.monotonic() - t0
    sz = dest.stat().st_size
    _emit(f"[workflow-dl] DONE ({sz/1e9:.2f} GB in {dt:.1f}s, {sz/1e6/max(dt,1):.1f} MB/s): {dest.name}")
    return {"model": f"{item['directory']}/{item['name']}", "status": "downloaded",
            "size": sz, "source": item.get("source", "")}


def list_ssd_models() -> dict[str, Any]:
    models_root = COMFY_DIR / "models"
    items = []
    if models_root.is_dir():
        for p in sorted(models_root.rglob("*")):
            if p.is_file() and p.name.lower().endswith(MODEL_EXTS):
                rel = p.relative_to(models_root)
                items.append({"path": str(rel), "size_gb": round(p.stat().st_size / 1e9, 2)})
    usage_target = str(COMFY_DIR if COMFY_DIR.exists() else Path("/content"))
    free_ssd_gb = round(shutil.disk_usage(usage_target).free / 1e9, 2)
    return {"free_ssd_gb": free_ssd_gb, "models": items}


def sync_workflow_models(
    wf_source: str = "",
    data_dir: str | Path | None = None,
    job: dict[str, Any] | None = None,
    wf_json_obj: dict[str, Any] | None = None,
    wf_label: str | None = None,
) -> dict[str, Any]:
    def _emit(msg: str) -> None:
        if job is not None:
            job["logs"].append(msg)

    if wf_json_obj is not None:
        wf_name = Path(wf_label or "current_canvas.json").name
        resolved_from = "browser_canvas"
        wf_json = wf_json_obj
        msg = f"[workflow-dl] Using active browser canvas workflow ({wf_name})"
        print(msg, flush=True)
        _emit(msg)
    else:
        wf_name, raw, resolved_from = resolve_workflow(wf_source, data_dir=data_dir)
        msg = f"[workflow-dl] Resolved workflow '{wf_source}' -> {resolved_from}"
        print(msg, flush=True)
        _emit(msg)
        wf_json = json.loads(raw)
        wf_dir = (COMFY_DIR / "user" / "default" / "workflows").resolve()
        wf_dir.mkdir(parents=True, exist_ok=True)
        safe_wf_name = Path(wf_name).name
        if safe_wf_name.endswith(".json"):
            target_wf = (wf_dir / safe_wf_name).resolve()
            if target_wf.is_relative_to(wf_dir):
                if not target_wf.is_file() or target_wf.read_text(encoding="utf-8", errors="ignore") != raw:
                    target_wf.write_text(raw, encoding="utf-8")
    models = list(extract_workflow_models(wf_json).values())
    msg = (f"[workflow-dl] Found {len(models)} required model(s) in {wf_name}: " +
           (", ".join(f"{m['directory']}/{m['name']} ({m.get('source')})" for m in models) if models else "none"))
    print(msg, flush=True)
    _emit(msg)
    if job is not None:
        job["workflow"] = wf_name
        job["resolved_from"] = resolved_from
        job["total"] = len(models)
    results = []
    for idx, m in enumerate(models, 1):
        step_msg = f"[workflow-dl] [{idx}/{len(models)}] Ensuring {m['directory']}/{m['name']} ..."
        print(step_msg, flush=True)
        _emit(step_msg)
        if job is not None:
            job["current"] = f"[{idx}/{len(models)}] {m['directory']}/{m['name']}"
        res = ensure_model_on_ssd(m, log_cb=_emit)
        results.append(res)
        if job is not None:
            job["completed"] = idx
            job["models"] = list(results)
    if job is not None:
        job["current"] = None
    usage_target = str(COMFY_DIR if COMFY_DIR.exists() else Path("/content"))
    free_ssd_gb = round(shutil.disk_usage(usage_target).free / 1e9, 2)
    return {"workflow": wf_name, "resolved_from": resolved_from, "free_ssd_gb": free_ssd_gb, "models": results}
