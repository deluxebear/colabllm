#!/usr/bin/env python3
"""ComfyUI + ComfyUI-Manager on Google Colab behind Caddy auth (notebook: comfyui.ipynb).

ComfyUI has no built-in authentication, so Caddy sits in front of it:
  browser -> basic auth (user/password),  API clients -> `Authorization: Bearer <token>`.
Models, custom_nodes, user settings, input, and output persist on Google Drive under
`/content/drive/MyDrive/colab-llm/comfyui`.

  ./services/comfyui.py up                     # quick tunnel (*.trycloudflare.com)
  ./services/comfyui.py up --domain            # https://comfyui.example.com
  ./services/comfyui.py up --gpu A100          # override GPU (default: L4)
  ./services/comfyui.py info | logs -f | status | down [--delete-tunnel]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from colabsvc import Service, main  # noqa: E402

SERVICE = Service(
    name="comfyui",
    notebook="comfyui.ipynb",
    service_port=8188,
    auth="caddy",            # ComfyUI has no built-in auth -> Caddy on public_port
    public_port=18080,
    gpu="L4",
    ready_timeout=1800,
    env={
        "CT_COMFY_LEGACY_UI": "1",
    },
)

if __name__ == "__main__":
    main(SERVICE)
