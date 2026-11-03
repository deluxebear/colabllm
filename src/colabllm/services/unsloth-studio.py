#!/usr/bin/env python3
"""Unsloth Studio on Google Colab (built-in login, notebook: unsloth-studio.ipynb).

  colabllm unsloth-studio up                      # quick tunnel (*.trycloudflare.com)
  colabllm unsloth-studio up --domain             # https://unsloth-studio.example.com
  colabllm unsloth-studio up -i 2 --domain        # 2nd instance -> https://unsloth-studio-2.example.com
  colabllm unsloth-studio up --domain ai.example.com --gpu A100
  colabllm unsloth-studio ps | info [-i ID] | logs [-i ID] -f | status [-i ID] | down [-i ID | --all] [--delete-tunnel]
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
    from cli_support import run_cli
    run_cli(lambda: main(SERVICE))
