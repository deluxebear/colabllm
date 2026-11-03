#!/usr/bin/env python3
"""my-service on Google Colab (notebook: my-service.ipynb).  <- copy of templates/service.py

  colabllm my-service up [-i ID] [--domain [HOST]] [--gpu L4]
  colabllm my-service ps | info [-i ID] | logs [-i ID] -f | status [-i ID] | down [-i ID | --all] [--delete-tunnel]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from colabsvc import Service, main  # noqa: E402

SERVICE = Service(
    name="my-service",            # = file name = Colab session name; tunnel colab-<name>, host <name>.example.com
    notebook="my-service.ipynb",
    service_port=8000,            # where the service listens on 127.0.0.1
    auth="caddy",                 # caddy: service has no auth | builtin: it has its own login / api key
    public_port=18080,             # Caddy port (only used with auth="caddy")
    gpu="T4",                     # T4 | L4 | G4 | H100 | A100 | None (CPU)
    env={},                       # extra CT_* settings for the notebook, e.g. {"CT_MODELS": "..."}
)

if __name__ == "__main__":
    from cli_support import run_cli
    run_cli(lambda: main(SERVICE))
