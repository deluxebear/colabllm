#!/usr/bin/env python3
"""Dedicated incremental model downloader session for ComfyUI (notebook: comfyui-models.ipynb).

Downloads models, workflow templates, and example inputs on a Colab L4 VM (using fast HF Xet to local SSD,
SHA256 verification, and atomic move to `/content/drive/MyDrive/colab-llm/comfyui/`). Already-downloaded files
matching size & SHA256 are skipped automatically.

Default pack: `minimax-h3-native` (https://docs.comfy.org/zh/tutorials/video/minimax/minimax-h3-native):
  * diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors  (T2V / I2V / FL2VA)
  * diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors (R2V / Ref2VA)
  * text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
  * vae/minimax_h3_video_vae_int8_convrot.safetensors
  * vae/minimax_h3_audio_vae_fp32.safetensors
  * loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors
  * loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors
  * embeddings/minimaxh3_art_is_explosion.safetensors
  * user/default/workflows/video_minimax_h3_{t2v,i2v,r2v}.json + example input images

Incremental downloads:
  1. Add custom items to `.state/comfyui-models/queue.json` (synced via CT_EXTRA_MODELS) or
     `/content/drive/MyDrive/colab-llm/comfyui/models/download-queue.json`:
     `[{"repo": "org/model", "filename": "path/in/repo.safetensors", "dest": "models/loras/my.safetensors"}]`
     or `[{"url": "https://...", "dest": "models/checkpoints/foo.safetensors"}]`
  2. Or set `CT_EXTRA_MODELS='[{"repo": "...", "filename": "...", "dest": "models/..."}]' ./services/comfyui-models.py up`

Usage:
  ./services/comfyui-models.py up                # start L4 session & download all queued models to Drive
  ./services/comfyui-models.py logs -f           # watch download progress
  ./services/comfyui-models.py status | down
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from colabsvc import ROOT, Service, main  # noqa: E402

QUEUE_FILE = ROOT / ".state" / "comfyui-models" / "queue.json"
EXTRA_MODELS = os.environ.get("CT_EXTRA_MODELS", "")
if not EXTRA_MODELS and QUEUE_FILE.is_file():
    try:
        EXTRA_MODELS = json.dumps(json.loads(QUEUE_FILE.read_text(encoding="utf-8")))
    except Exception as exc:
        sys.stderr.write(f"[comfyui-models] warning: invalid {QUEUE_FILE}: {exc}\n")

SERVICE = Service(
    name="comfyui-models",
    notebook="comfyui-models.ipynb",
    service_port=8189,
    auth="caddy",
    public_port=18080,
    gpu="L4",
    ready_timeout=7200,
    env={
        "CT_COMFY_DATA_DIR": "/content/drive/MyDrive/colab-llm/comfyui",
        "CT_MODEL_PACKS": os.environ.get("CT_MODEL_PACKS", "minimax-h3-native"),
        **({"CT_EXTRA_MODELS": EXTRA_MODELS} if EXTRA_MODELS else {}),
    },
)

if __name__ == "__main__":
    main(SERVICE)
