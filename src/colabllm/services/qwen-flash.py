#!/usr/bin/env python3
"""Qwen3.8-Flash-Next (IQ3_S, 800K YaRN 3.125x, BF16 Vision) on a Colab G4 GPU.

Uses the "one service = one notebook + one script"
convention (`src/colabllm/services/qwen-flash.ipynb` + `src/colabllm/services/qwen-flash.py`).

Key properties preserved from the original deployment:
  * Hardware: G4 (RTX PRO 6000 Blackwell sm_120, ~96 GB VRAM, 176 GB RAM) -- requested WITHOUT `--high-mem`.
  * Runtime cache: pinned llama.cpp commit built for CUDA sm_120 and cached at
    `/content/drive/MyDrive/models/.runtimes/llama.cpp/sm120-<fingerprint>/runtime.tar` (+ sha256).
  * Model staging: concurrent resumable HF Xet download to `/content/qwen-flash/model` with SHA256
    verification and per-shard fallback to `/content/drive/MyDrive/models/SC117/...`.
  * Vision: `mmproj-Qwen3.8-Flash-Next-BF16.gguf` verified and passed via `--mmproj`.
  * Server flags: `--load-mode none --lazy-mode off` (27.5 GiB n-gram table resident in CPU RAM),
    `-c 819200 --rope-scaling yarn --rope-scale 3.125 --yarn-orig-ctx 262144 -ctk f16 -ctv f16 -fa on`.
  * Auth: native `llama-server --api-key-file` (`auth="builtin"`); credentials are stored in the user directory.
  * Default domain when `--domain` is passed without a value: `qwen.example.com`.

Usage:
  colabllm qwen-flash up --domain          # https://qwen.example.com (recommended)
  colabllm qwen-flash up -i 2 --domain     # 2nd instance -> https://qwen-2.example.com
  colabllm qwen-flash up                   # quick tunnel (*.trycloudflare.com)
  colabllm qwen-flash ps | info [-i ID] | logs [-i ID] -f | status [-i ID] | down [-i ID | --all] [--delete-tunnel]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from colabsvc import Service, apply_instance, extract_instance_arg, main  # noqa: E402

SERVICE = Service(
    name="qwen-flash",
    notebook="qwen-flash.ipynb",
    service_port=8000,
    auth="builtin",                   # llama-server --api-key-file handles Bearer auth natively
    public_port=8000,
    gpu="G4",                         # G4 without --high-mem (high_mem=False)
    high_mem=False,
    default_hostname="qwen.example.com",
    ready_timeout=3600,               # first-time 84 GB staging + load can take ~15-25 min
    env={
        "CT_QWEN_REPO": "SC117/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-GGUF",
        "CT_QWEN_QUANT": "IQ3_S",
        "CT_QWEN_ALIAS": "qwen3.8-flash-next",
        "CT_QWEN_CONTEXT": "819200",  # override to 524288 (rope_scale 2.0) or 262144 if desired
        "CT_QWEN_ROPE_SCALING": "yarn",
        "CT_QWEN_ROPE_SCALE": "3.125",
        "CT_QWEN_YARN_ORIG_CTX": "262144",
    },
)


if __name__ == "__main__":
    _inst_id, _rem = extract_instance_arg(sys.argv[1:])
    if _inst_id:
        apply_instance(SERVICE, _inst_id)
    main(SERVICE)
