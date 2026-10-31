#!/usr/bin/env python3
"""Unsloth Studio on Google Colab (built-in login, notebook: unsloth-studio.ipynb).

  ./services/unsloth-studio.py up                      # quick tunnel (*.trycloudflare.com)
  ./services/unsloth-studio.py up --domain             # https://unsloth-studio.example.com
  ./services/unsloth-studio.py up --domain ai.example.com --gpu A100
  ./services/unsloth-studio.py info | logs -f | status | down [--delete-tunnel]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from colabsvc import Service, main  # noqa: E402

SERVICE = Service(
    name="unsloth-studio",
    notebook="unsloth-studio.ipynb",
    service_port=8888,
    auth="builtin",          # Unsloth has its own login -> no Caddy
    gpu="L4",
)

if __name__ == "__main__":
    main(SERVICE)
