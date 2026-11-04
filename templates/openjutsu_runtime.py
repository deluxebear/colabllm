"""Self-contained Colab setup and verification helpers for Openjutsu (ComfyUI + comfy-api-proxy + Next.js)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

NODE_VERSION = "22.14.0"
PROXY_VERSION = "0.1.6"

CUSTOM_NODES: tuple[tuple[str, str], ...] = (
    ("ComfyUI-MiniMaxH3-Context-Loop", "https://github.com/ethanfel/ComfyUI-MiniMaxH3-Context-Loop.git"),
    ("comfyui-h3-oneclick-swap", "https://github.com/D3voz/comfyui-h3-oneclick-swap.git"),
    ("ComfyUI-Fantastic-MiniMaxH3-PromptBuilder", "https://github.com/Adudeguyman/ComfyUI-Fantastic-MiniMaxH3-PromptBuilder.git"),
    ("ComfyUI-KJNodes", "https://github.com/kijai/ComfyUI-KJNodes.git"),
    ("ComfyUI-VideoHelperSuite", "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git"),
    ("comfyui-websocket-reconnect-fix", "https://github.com/D3voz/comfyui-websocket-reconnect-fix.git"),
    ("SolarRenderer", "https://github.com/Enderfga/SolarRenderer.git"),
    ("comfyui_controlnet_aux", "https://github.com/Fannovel16/comfyui_controlnet_aux.git"),
    ("ComfyUI-H3-FaceRefine", "https://github.com/Carasibana/ComfyUI-H3-FaceRefine.git"),
    ("ComfyUI-Impact-Pack", "https://github.com/ltdrdata/ComfyUI-Impact-Pack.git"),
    ("comfyui-image-detail-contrast", "https://github.com/D3voz/comfyui-image-detail-contrast.git"),
    ("Orion4D_FXMax", "https://github.com/orion4d/Orion4D_FXMax.git"),
    ("ComfyUI-Spectrum-MiniMax-H3", "https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3.git"),
)

CORE_MODELS: tuple[dict[str, object], ...] = (
    {
        "directory": "diffusion_models",
        "name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "urls": (
            "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        ),
    },
    {
        "directory": "text_encoders",
        "name": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors",
        "urls": (
            "https://huggingface.co/Merserk/MiniMax-H3-INT4-ConvRot/resolve/main/qwen3vl_32b_minimax_h3_int4_convrot.safetensors",
            "https://huggingface.co/Abiray/MiniMax-H3-GGUF/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_int4_convrot.safetensors",
        ),
    },
    {
        "directory": "text_encoders",
        "name": "qwen3vl_8b_int8_convrot.safetensors",
        "urls": (
            "https://huggingface.co/Comfy-Org/Qwen3-VL/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors",
        ),
    },
    {
        "directory": "vae",
        "name": "minimax_h3_video_vae_int8_convrot.safetensors",
        "urls": (
            "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_int8_convrot.safetensors",
        ),
    },
    {
        "directory": "vae",
        "name": "minimax_h3_audio_vae_fp32.safetensors",
        "urls": (
            "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors",
        ),
    },
    {
        "directory": "loras",
        "name": "minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors",
        "urls": (
            "https://huggingface.co/Kijai/MiniMax-H3-experimental/resolve/main/loras/minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors",
        ),
    },
    {
        "directory": "checkpoints",
        "name": "sam3.1_multiplex_fp16.safetensors",
        "urls": (
            "https://huggingface.co/Comfy-Org/sam3.1/resolve/main/checkpoints/sam3.1_multiplex_fp16.safetensors",
        ),
    },
)

EXTRA_FULL_MODELS: tuple[dict[str, object], ...] = (
    {
        "directory": "ultralytics/bbox",
        "name": "face_yolov8m.pt",
        "urls": (
            "https://huggingface.co/Bingsu/adetailer/resolve/main/face_yolov8m.pt",
        ),
    },
    {
        "directory": "ultralytics/segm",
        "name": "person_yolov8m-seg.pt",
        "urls": (
            "https://huggingface.co/Bingsu/adetailer/resolve/main/person_yolov8m-seg.pt",
        ),
    },
    {
        "directory": "sams",
        "name": "sam_vit_b_01ec64.pth",
        "urls": (
            "https://huggingface.co/comfyuistudio/sam_vit_b_01ec64.pth/resolve/main/sam_vit_b_01ec64.pth",
            "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth",
        ),
    },
    {
        "directory": "diffusion_models",
        "name": "flux3_image_klein_sd_int8_convrot.safetensors",
        "urls": (
            "https://huggingface.co/ml-resources/flux-3-image-klein-comfy/resolve/main/diffusion_models/flux3-image-klein-int8-convrot.safetensors",
            "https://huggingface.co/black-forest-labs/flux-3-image-klein-comfy/resolve/main/diffusion_models/flux3-image-klein-int8-convrot.safetensors",
        ),
    },
    {
        "directory": "text_encoders",
        "name": "qwen3.5_9b_flux3_prompt_upsampler_int8_convrot.safetensors",
        "urls": (
            "https://huggingface.co/ml-resources/flux-3-image-klein-comfy/resolve/main/text_encoders/qwen3.5_9b_flux3_prompt_upsampler_int8_convrot.safetensors",
            "https://huggingface.co/black-forest-labs/flux-3-image-klein-comfy/resolve/main/text_encoders/qwen3.5_9b_flux3_prompt_upsampler_int8_convrot.safetensors",
        ),
    },
    {
        "directory": "text_encoders",
        "name": "qwen3vl_4b_fp8_scaled.safetensors",
        "urls": (
            "https://huggingface.co/Comfy-Org/Qwen3-VL/resolve/main/text_encoders/qwen3vl_4b_fp8_scaled.safetensors",
        ),
    },
    {
        "directory": "vae",
        "name": "flux2-vae.safetensors",
        "urls": (
            "https://huggingface.co/Comfy-Org/Flux-2-Klein-9B/resolve/main/split_files/vae/flux2-vae.safetensors",
            "https://huggingface.co/ml-resources/flux-3-image-klein-comfy/resolve/main/vae/flux2-vae.safetensors",
        ),
    },
    {
        "directory": "loras",
        "name": "solarrenderer_4step_lora.safetensors",
        "urls": (
            "https://huggingface.co/Enderfga/SolarRenderer/resolve/main/solarrenderer_4step_lora.safetensors",
        ),
    },
    {
        "directory": "loras",
        "name": "solarrenderer_control_lora.safetensors",
        "urls": (
            "https://huggingface.co/Enderfga/SolarRenderer/resolve/main/solarrenderer_control_lora.safetensors",
        ),
    },
)

MODEL_PROFILES: dict[str, tuple[dict[str, object], ...]] = {
    "core": CORE_MODELS,
    "full": CORE_MODELS + EXTRA_FULL_MODELS,
}

EXTRA_NODE_PACKAGES: tuple[str, ...] = (
    "huggingface_hub",
    "hf_xet",
    "av>=17.0.0",
    "imageio-ffmpeg",
    "color-matcher",
    "mss",
    "segment-anything",
    "scikit-image",
    "scikit-learn",
    "piexif",
    "dill",
    "matplotlib",
    "ultralytics",
    "scipy",
    "insightface",
    "onnxruntime-gpu",
    "scenedetect>=0.7",
    "mediapipe>=0.8.0",
    "omegaconf",
    "addict",
    "yacs",
    "trimesh",
    "albumentationsx",
)


def olog(msg: str) -> None:
    print(time.strftime("[%F %T] [openjutsu] ") + msg, flush=True)


def validate_model_destination(directory: str, name: str, comfy_dir: Path) -> Path:
    """Resolve and verify that <comfy_dir>/models/<directory>/<name> stays strictly inside models/."""
    models_root = (comfy_dir / "models").resolve()
    rel_dir = (directory or "").strip().strip("/")
    rel_name = (name or "").strip().lstrip("/")
    if not rel_dir or not rel_name or "\x00" in rel_dir or "\x00" in rel_name:
        raise ValueError(f"Invalid model destination: directory={directory!r}, name={name!r}")
    if ".." in Path(rel_dir).parts or ".." in Path(rel_name).parts or "/" in rel_name or "\\" in rel_name:
        raise ValueError(f"Refusing path traversal in model destination: {rel_dir}/{rel_name}")
    dest = (models_root / rel_dir / rel_name).resolve()
    if not dest.is_relative_to(models_root) or dest == models_root:
        raise ValueError(f"Refusing model path outside {models_root}: {rel_dir}/{rel_name}")
    return dest


def models_for_profile(profile: str) -> tuple[dict[str, object], ...]:
    key = (profile or "full").strip().lower()
    if key not in MODEL_PROFILES:
        raise ValueError(f"Unsupported Openjutsu model profile {profile!r}; expected one of {sorted(MODEL_PROFILES)}")
    return MODEL_PROFILES[key]


def render_extra_model_paths(comfy_dir: Path) -> str:
    """Register `ultralytics` and `sams` model folders before custom nodes are imported."""
    base = str(comfy_dir.resolve())
    return (
        "openjutsu:\n"
        f"  base_path: {json.dumps(base)}\n"
        "  ultralytics: models/ultralytics\n"
        "  ultralytics_bbox: models/ultralytics/bbox\n"
        "  ultralytics_segm: models/ultralytics/segm\n"
        "  sams: models/sams\n"
    )


VRAM_MODES = ("auto", "highvram", "dynamic", "gpu-only")
CHAINS_STORAGE_MODES = ("ssd", "drive")


def detect_gpu_vram_mib() -> int:
    """Return total GPU0 VRAM in MiB, or 0 if unavailable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        first = out.splitlines()[0].strip()
        return int(float(first))
    except Exception:
        return 0


def select_comfy_vram_flags(vram_total_mib: int, mode: str = "auto") -> list[str]:
    """Select ComfyUI VRAM flags; enables `--highvram` automatically on >=76 GiB GPUs (e.g. G4 96 GB)."""
    clean = (mode or "auto").strip().lower()
    if clean not in VRAM_MODES:
        raise ValueError(f"Unsupported VRAM mode {mode!r}; expected one of {VRAM_MODES}")
    if clean == "highvram":
        return ["--highvram"]
    if clean == "gpu-only":
        return ["--gpu-only"]
    if clean == "dynamic":
        return []
    return ["--highvram"] if vram_total_mib >= 76000 else []


def configure_chains_storage(work_dir: Path, data_dir: Path, mode: str = "ssd") -> str:
    """Route `output/h3_chains` per-clip checkpoints (~450 MB/clip) to local NVMe SSD via VFS bind mount.

    Using `mount --bind` (instead of a symlink) keeps `os.path.realpath` inside `output/`,
    satisfying Context-Loop's `CheckpointGraphManager` path-confinement check while avoiding
    multi-GB synchronous FUSE writes and Google Drive quota bloat.
    """
    clean = (mode or "ssd").strip().lower()
    if clean not in CHAINS_STORAGE_MODES:
        raise ValueError(f"Unsupported chains storage mode {mode!r}; expected one of {CHAINS_STORAGE_MODES}")
    drive_chains = data_dir / "output" / "h3_chains"
    drive_chains.mkdir(parents=True, exist_ok=True)
    if clean == "drive":
        if os.path.ismount(drive_chains):
            subprocess.run(["umount", str(drive_chains)], check=False, timeout=30)
            olog(f"Unmounted SSD bind mount; h3_chains stored on Drive: {drive_chains}")
        return "drive"
    if not str(data_dir).startswith("/content/drive/"):
        return "ssd-native"
    local_chains = work_dir / "h3_chains"
    local_chains.mkdir(parents=True, exist_ok=True)
    if os.path.ismount(drive_chains):
        return "ssd-bind"
    res = subprocess.run(["mount", "--bind", str(local_chains), str(drive_chains)],
                         capture_output=True, text=True, check=False, timeout=30)
    if res.returncode == 0:
        olog(f"Bind-mounted local NVMe SSD for h3_chains checkpoints: {local_chains} -> {drive_chains}")
        return "ssd-bind"
    olog(f"Bind-mount unavailable ({res.stderr.strip() or res.returncode}); keeping h3_chains on {drive_chains}")
    return "drive-fallback"


def normalize_linux_workflows(app_dir: Path) -> list[str]:
    """Normalize Windows backslash relative model paths (e.g. face.api.json) for POSIX ComfyUI."""
    wf_root = app_dir / "workflows"
    updated: list[str] = []
    if not wf_root.is_dir():
        return updated
    for wf_path in sorted(wf_root.rglob("*.json")):
        raw = wf_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        changed = False
        if isinstance(data, dict):
            for node in data.values():
                if not isinstance(node, dict) or node.get("class_type") != "H3FaceTrackCrop":
                    continue
                inputs = node.get("inputs")
                if not isinstance(inputs, dict):
                    continue
                for field in ("detector", "fallback_detector"):
                    val = inputs.get(field)
                    if isinstance(val, str) and "\\" in val:
                        inputs[field] = val.replace("\\", "/")
                        changed = True
        if changed:
            wf_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            updated.append(str(wf_path.relative_to(app_dir)))
    return updated


def run_logged(cmd: list[str], log_path: Path, *, env: dict[str, str] | None = None,
               cwd: Path | str | None = None, check: bool = True, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {' '.join(str(x) for x in cmd)}\n")
        handle.flush()
        res = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT, env=env, cwd=cwd, text=True, timeout=timeout)
    if check and res.returncode != 0:
        tail = "".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-45:])
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(str(x) for x in cmd[:5])}\n{tail}")
    return res


def ensure_nodejs(work_dir: Path, log_path: Path) -> Path:
    node_dir = work_dir / f"node-v{NODE_VERSION}"
    node_bin = node_dir / "bin" / "node"
    if node_bin.is_file():
        return node_dir / "bin"
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
    if not arch:
        raise RuntimeError(f"Unsupported architecture for Node.js binary: {platform.machine()}")
    tar_name = f"node-v{NODE_VERSION}-linux-{arch}.tar.xz"
    base_url = f"https://nodejs.org/dist/v{NODE_VERSION}/"
    olog(f"Installing Node.js v{NODE_VERSION} ({arch}) -> {node_dir}")
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        archive = Path(tmp) / tar_name
        req_sums = urllib.request.Request(base_url + "SHASUMS256.txt", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req_sums, timeout=30) as resp:
            shasums = resp.read().decode("utf-8")
        expected = next(
            (line.split()[0] for line in shasums.splitlines() if line.split() and line.split()[-1] == tar_name),
            None,
        )
        if not expected:
            raise RuntimeError(f"Could not find SHA256 for {tar_name} in Node.js SHASUMS256.txt")
        req_tar = urllib.request.Request(base_url + tar_name, headers={"User-Agent": "Mozilla/5.0"})
        h = hashlib.sha256()
        with urllib.request.urlopen(req_tar, timeout=120) as src, archive.open("wb") as dst:
            while chunk := src.read(4 * 1024 * 1024):
                dst.write(chunk)
                h.update(chunk)
        if h.hexdigest() != expected:
            raise RuntimeError(f"Node.js archive SHA256 mismatch: expected {expected}, got {h.hexdigest()}")
        extract_root = Path(tmp) / "extracted"
        extract_root.mkdir()
        with tarfile.open(archive, "r:xz") as tf:
            tf.extractall(extract_root, filter="data")
        extracted_sub = extract_root / f"node-v{NODE_VERSION}-linux-{arch}"
        if node_dir.exists():
            shutil.rmtree(node_dir)
        extracted_sub.replace(node_dir)
    ver = subprocess.check_output([str(node_bin), "--version"], text=True, timeout=15).strip()
    olog(f"Node.js ready: {ver}")
    return node_dir / "bin"


def _symlink_dir(local_path: Path, target_path: Path) -> None:
    target_path.mkdir(parents=True, exist_ok=True)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.is_symlink():
        if os.readlink(local_path) != str(target_path):
            local_path.unlink()
            local_path.symlink_to(target_path)
    elif local_path.is_dir():
        subprocess.run(["cp", "-rn", f"{local_path}/.", f"{target_path}/"], check=False, timeout=120)
        shutil.rmtree(local_path)
        local_path.symlink_to(target_path)
    else:
        local_path.symlink_to(target_path)


def prepare_comfy_tree(work_dir: Path, comfy_dir: Path, data_dir: Path) -> Path:
    """Ensure ComfyUI repo, local SSD models/ root, and Drive symlinks exist before parallel tasks start."""
    env_log = work_dir / "env-setup.log"
    comfy_repo = os.environ.get("CT_COMFY_REPO", "https://github.com/comfyanonymous/ComfyUI.git")
    if not (comfy_dir / ".git").is_dir():
        if comfy_dir.exists():
            shutil.rmtree(comfy_dir)
        olog(f"Cloning ComfyUI -> {comfy_dir}")
        run_logged(["git", "clone", "--depth", "1", comfy_repo, str(comfy_dir)], env_log, timeout=600)
    else:
        run_logged(["git", "-C", str(comfy_dir), "pull", "--ff-only"], env_log, check=False, timeout=300)

    if (comfy_dir / "models").is_symlink():
        (comfy_dir / "models").unlink()
    (comfy_dir / "models" / "loras").mkdir(parents=True, exist_ok=True)
    (comfy_dir / "custom_nodes").mkdir(parents=True, exist_ok=True)
    for sub in ("output", "input", "user"):
        _symlink_dir(comfy_dir / sub, data_dir / sub)
    _symlink_dir(comfy_dir / "models" / "loras" / "drive", data_dir / "loras")
    configure_chains_storage(work_dir, data_dir, os.environ.get("CT_OPENJUTSU_CHAINS_STORAGE", "ssd"))

    extra_paths_file = work_dir / "extra_model_paths.yaml"
    extra_paths_file.write_text(render_extra_model_paths(comfy_dir), encoding="utf-8")
    return extra_paths_file


def setup_environment(work_dir: Path, comfy_dir: Path, data_dir: Path) -> dict[str, str]:
    env_log = work_dir / "env-setup.log"
    extra_paths_file = work_dir / "extra_model_paths.yaml"
    if not extra_paths_file.is_file():
        extra_paths_file = prepare_comfy_tree(work_dir, comfy_dir, data_dir)

    if not shutil.which("ffmpeg"):
        subprocess.run("apt-get update -qq && apt-get install -y -qq ffmpeg >/dev/null 2>&1",
                       shell=True, check=False, timeout=300)
    if not shutil.which("uv"):
        run_logged([sys.executable, "-m", "pip", "install", "-q", "-U", "uv"], env_log, timeout=300)
    uv_bin = shutil.which("uv") or "uv"

    # 1. Ensure Blackwell sm_120 PyTorch support if running on G4
    try:
        cap = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"], text=True, timeout=15
        ).splitlines()[0].strip()
    except Exception:
        cap = ""
    if cap.startswith("12."):
        arch_check = subprocess.run(
            [sys.executable, "-c", "import sys, torch; sys.exit(0 if 'sm_120' in torch.cuda.get_arch_list() else 1)"],
            capture_output=True, text=True, timeout=30,
        )
        if arch_check.returncode != 0:
            olog("Upgrading PyTorch to CUDA 13.0 build for Blackwell sm_120...")
            run_logged([
                uv_bin, "pip", "install", "--system", "-U",
                "torch", "torchvision", "torchaudio",
                "--index-url", "https://download.pytorch.org/whl/cu130",
            ], env_log, timeout=900)

    # 3. Clone Openjutsu repo + Node.js 22 + build production Next.js app
    app_dir = work_dir / "app"
    app_repo = os.environ.get("CT_OPENJUTSU_REPO", "https://github.com/D3voz/openjutsu.git")
    if not (app_dir / ".git").is_dir():
        olog(f"Cloning Openjutsu -> {app_dir}")
        run_logged(["git", "clone", "--depth", "1", app_repo, str(app_dir)], env_log, timeout=300)
    else:
        run_logged(["git", "-C", str(app_dir), "pull", "--ff-only"], env_log, check=False, timeout=300)

    patched_wfs = normalize_linux_workflows(app_dir)
    if patched_wfs:
        olog(f"Normalized POSIX detector paths in workflows: {', '.join(patched_wfs)}")

    # 4. Clone all 13 Openjutsu custom node packs onto local SSD
    olog(f"Installing {len(CUSTOM_NODES)} ComfyUI custom node packs...")
    for node_name, node_repo in CUSTOM_NODES:
        node_dir = comfy_dir / "custom_nodes" / node_name
        if not (node_dir / ".git").is_dir():
            if node_dir.exists():
                shutil.rmtree(node_dir)
            run_logged(["git", "clone", "--depth", "1", node_repo, str(node_dir)], env_log, timeout=300)
        else:
            run_logged(["git", "-C", str(node_dir), "pull", "--ff-only"], env_log, check=False, timeout=300)

    # Apply Openjutsu's SolarRenderer extended-silence patch idempotently
    solar_dir = comfy_dir / "custom_nodes" / "SolarRenderer"
    solar_patch = app_dir / "patches" / "solarrenderer-long-clips.patch"
    solar_nodes_py = solar_dir / "comfyui" / "nodes.py"
    if solar_patch.is_file() and solar_nodes_py.is_file():
        if "the held silence covers 170 frames" in solar_nodes_py.read_text(encoding="utf-8", errors="replace"):
            chk = subprocess.run(["git", "-C", str(solar_dir), "apply", "--check", str(solar_patch)],
                                 capture_output=True, text=True, timeout=30)
            if chk.returncode == 0:
                run_logged(["git", "-C", str(solar_dir), "apply", str(solar_patch)], env_log, timeout=30)
                olog("Applied SolarRenderer extended-silence patch for long clips")

    # 5. Install Python dependencies for ComfyUI + all custom nodes
    deps_marker = work_dir / ".comfy_nodes_deps_v1.ok"
    pkg_probe = subprocess.run(
        [sys.executable, "-c", "import comfy_kitchen, insightface, ultralytics, segment_anything, color_matcher"],
        capture_output=True, timeout=30,
    )
    if not deps_marker.is_file() or pkg_probe.returncode != 0:
        olog("Installing ComfyUI and custom-node Python dependencies via uv...")
        run_logged([uv_bin, "pip", "install", "--system", "-r", str(comfy_dir / "requirements.txt")],
                   env_log, timeout=900)
        run_logged([uv_bin, "pip", "install", "--system", *EXTRA_NODE_PACKAGES], env_log, timeout=900)
        # sam2 for Impact-Pack (if not already importable)
        sam2_chk = subprocess.run([sys.executable, "-c", "import sam2"], capture_output=True, timeout=20)
        if sam2_chk.returncode != 0:
            sam2_env = {**os.environ, "SAM2_BUILD_CUDA": "0"}
            run_logged([uv_bin, "pip", "install", "--system", "sam2", "--no-build-isolation"],
                       env_log, env=sam2_env, check=False, timeout=300)
            sam2_chk2 = subprocess.run([sys.executable, "-c", "import sam2"], capture_output=True, timeout=20)
            if sam2_chk2.returncode != 0:
                run_logged([uv_bin, "pip", "install", "--system", "--no-build-isolation",
                            "git+https://github.com/facebookresearch/sam2"],
                           env_log, env=sam2_env, check=False, timeout=600)
        for node_name, _ in CUSTOM_NODES:
            req = comfy_dir / "custom_nodes" / node_name / "requirements.txt"
            if req.is_file() and node_name != "ComfyUI-Impact-Pack":
                run_logged([uv_bin, "pip", "install", "--system", "-r", str(req)], env_log, check=False, timeout=600)
        deps_marker.write_text("ok\n", encoding="utf-8")

    # 6. Dedicated virtualenv for comfy-api-proxy==0.1.6 + Openjutsu's live-progress patch_proxy.py
    proxy_venv = work_dir / "venv-proxy"
    proxy_py = proxy_venv / "bin" / "python"
    proxy_bin = proxy_venv / "bin" / "comfy-api-proxy"
    if not proxy_bin.is_file():
        olog(f"Installing comfy-api-proxy=={PROXY_VERSION} -> {proxy_venv}")
        run_logged([uv_bin, "venv", "--python", sys.executable, str(proxy_venv)], env_log, timeout=120)
        run_logged([uv_bin, "pip", "install", "--python", str(proxy_py), f"comfy-api-proxy=={PROXY_VERSION}"],
                   env_log, timeout=300)
    run_logged([str(proxy_py), str(app_dir / "patch_proxy.py")], env_log, timeout=60)

    # 7. Node.js 22 + Next.js production build
    node_bin_dir = ensure_nodejs(work_dir, env_log)
    node_env = {**os.environ, "PATH": f"{node_bin_dir}:{os.environ.get('PATH', '')}", "NEXT_TELEMETRY_DISABLED": "1"}
    (app_dir / ".env.local").write_text(
        "COMFY_BASE_URL=http://127.0.0.1:8189\n"
        "COMFY_API_KEY=\n"
        "COMFYUI_URL=http://127.0.0.1:8188\n"
        "OPENJUTSU_PASSWORD=\n",
        encoding="utf-8",
    )
    head_sha = subprocess.check_output(["git", "-C", str(app_dir), "rev-parse", "HEAD"], text=True, timeout=15).strip()
    build_marker = work_dir / ".next_build_sha"
    if not (app_dir / ".next" / "BUILD_ID").is_file() or not build_marker.is_file() or build_marker.read_text().strip() != head_sha:
        olog("Building Openjutsu Next.js production app (`npm ci && npm run build`)...")
        run_logged([str(node_bin_dir / "npm"), "ci", "--no-audit", "--no-fund"], env_log,
                   env=node_env, cwd=app_dir, timeout=600)
        run_logged([str(node_bin_dir / "npm"), "run", "build"], env_log,
                   env=node_env, cwd=app_dir, timeout=600)
        build_marker.write_text(head_sha + "\n", encoding="utf-8")

    return {
        "node_bin_dir": str(node_bin_dir),
        "app_dir": str(app_dir),
        "proxy_bin": str(proxy_bin),
        "extra_model_paths": str(extra_paths_file),
    }


def _download_single_model(item: dict[str, object], comfy_dir: Path) -> dict[str, object]:
    directory = str(item["directory"])
    name = str(item["name"])
    urls = tuple(str(u) for u in item["urls"])  # type: ignore[arg-type]
    dest = validate_model_destination(directory, name, comfy_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        sz = dest.stat().st_size
        olog(f"[model] Cached ({sz/1e9:.2f} GB): {directory}/{name}")
        return {"model": f"{directory}/{name}", "status": "cached", "size": sz}

    hf_tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    last_err: Exception | None = None
    for url in urls:
        for attempt in range(1, 4):
            partial = dest.with_name(f".{dest.name}.{os.getpid()}.partial")
            partial.unlink(missing_ok=True)
            headers = {"User-Agent": "Mozilla/5.0"}
            if hf_tok and "huggingface.co" in url:
                headers["Authorization"] = f"Bearer {hf_tok}"
            t0 = last_log = time.monotonic()
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=120) as resp, partial.open("wb") as out:
                    total = int(resp.headers.get("Content-Length") or 0)
                    copied = 0
                    while True:
                        chunk = resp.read(16 * 1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        copied += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= 20:
                            mb_s = copied / 1e6 / max(now - t0, 0.1)
                            pct = f" ({copied*100/total:.0f}%)" if total > 0 else ""
                            olog(f"[model]   {name}: {copied/1e9:.2f}/{total/1e9:.2f} GB{pct} @ {mb_s:.0f} MB/s")
                            last_log = now
                if total > 0 and copied != total:
                    raise IOError(f"Incomplete read for {name}: got {copied} of {total} bytes")
                if copied == 0:
                    raise IOError(f"Empty download for {name}")
                partial.replace(dest)
                dt = max(time.monotonic() - t0, 0.1)
                olog(f"[model] Downloaded ({copied/1e9:.2f} GB in {dt:.1f}s, {copied/1e6/dt:.0f} MB/s): {directory}/{name}")
                return {"model": f"{directory}/{name}", "status": "downloaded", "size": copied}
            except Exception as err:
                partial.unlink(missing_ok=True)
                last_err = err
                olog(f"[model] Attempt {attempt}/3 failed for {name} ({url}): {err}")
                time.sleep(min(5 * attempt, 15))
    raise RuntimeError(f"Failed to download {directory}/{name}: {last_err}")


def stage_models(comfy_dir: Path, profile: str = "full", workers: int = 3) -> list[dict[str, object]]:
    items = models_for_profile(profile)
    free_gb = shutil.disk_usage(comfy_dir.parent if not comfy_dir.exists() else comfy_dir).free / 1e9
    olog(f"Staging {len(items)} model files (profile={profile}, free SSD={free_gb:.1f} GB)...")
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_download_single_model, item, comfy_dir): item for item in items}
        for fut in as_completed(future_map):
            results.append(fut.result())
    for p in ("/root/.cache/huggingface/xet", "/root/.cache/huggingface/hub"):
        shutil.rmtree(p, ignore_errors=True)
    total_gb = sum(int(r["size"]) for r in results) / 1e9
    olog(f"All {len(results)} model files ready on local SSD ({total_gb:.2f} GB total)")
    return results


DEFAULT_MODEL_SETTINGS: dict[str, object] = {
    "model": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "loras": [{"name": "minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors", "strength": 1}],
    "steps": 6,
    "textEncoder": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors",
    "videoVae": "minimax_h3_video_vae_int8_convrot.safetensors",
    "audioVae": "minimax_h3_audio_vae_fp32.safetensors",
    "acceleration": "none",
}


def setup_check_payloads(profile: str = "full") -> dict[str, dict[str, object]]:
    """Return representative `/api/setup` payloads covering Openjutsu's workflows."""
    payloads: dict[str, dict[str, object]] = {
        "mask": {"kind": "mask", "hatFix": True, "margin": "normal", "target": "person"},
        "people": {"kind": "people", "resolution": "736p"},
        "fit": {"kind": "fit", "resolution": "736p"},
        "color": {
            "kind": "color",
            "settings": {
                "detail": -0.2,
                "tone": {
                    "mode": "Reinhard",
                    "exposure": 0.09,
                    "contrast": 0.84,
                    "pivot": 0.69,
                    "highlight_rolloff": 0.27,
                    "shadow_lift": 0.15,
                    "black_floor": 0.03,
                    "saturation": 1.05,
                    "strength": 0.2,
                },
            },
        },
        "simple": {
            "kind": "simple",
            "settings": {
                "role": "",
                "resolution": "736p",
                "motion": 768,
                "hatFix": True,
                "noLipSync": True,
                "margin": "normal",
                "target": "person",
                "seed": 0,
                "clipSeconds": 6,
            },
            "model": DEFAULT_MODEL_SETTINGS,
        },
        "advanced": {
            "kind": "advanced",
            "pictures": 2,
            "settings": {
                "roles": ["", ""],
                "resolution": "736p",
                "motion": 768,
                "hatFix": True,
                "noLipSync": True,
                "margin": "normal",
                "seed": 0,
                "clipSeconds": 6,
            },
            "model": DEFAULT_MODEL_SETTINGS,
        },
        "object": {
            "kind": "object",
            "picture": True,
            "settings": {
                "mode": "change",
                "target": "jacket",
                "changeTo": "leather jacket",
                "biggestOnly": True,
                "keepOutline": False,
                "resolution": "736p",
                "motion": 768,
                "margin": "normal",
                "seed": 0,
                "clipSeconds": 6,
            },
            "model": DEFAULT_MODEL_SETTINGS,
        },
        "montage": {
            "kind": "montage",
            "pictures": 2,
            "review": None,
            "settings": {
                "roles": ["", ""],
                "resolution": "736p",
                "motion": 768,
                "hatFix": True,
                "noLipSync": True,
                "margin": "normal",
                "seed": 0,
                "clipSeconds": 6,
            },
            "model": DEFAULT_MODEL_SETTINGS,
        },
    }
    if (profile or "full").strip().lower() == "full":
        payloads["face"] = {
            "kind": "face",
            "settings": {"style": "mouth", "seed": 0, "chunkSeconds": 6.5},
            "model": DEFAULT_MODEL_SETTINGS,
        }
    return payloads
