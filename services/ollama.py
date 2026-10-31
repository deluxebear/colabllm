#!/usr/bin/env python3
"""Ollama API on Google Colab behind Caddy auth (notebook: ollama.ipynb).

Ollama has no authentication, so Caddy sits in front of it:
  browser -> basic auth (user/password),  API clients -> `Authorization: Bearer <token>`.

  ./services/ollama.py up                     # quick tunnel
  ./services/ollama.py up --domain            # https://ollama.example.com
  ./services/ollama.py info                   # URL, password, API token
  curl -H "Authorization: Bearer $TOKEN" $URL/api/generate -d '{"model":"qwen2.5:0.5b","prompt":"hi"}'
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from colabsvc import Service, main  # noqa: E402

SERVICE = Service(
    name="ollama",
    notebook="ollama.ipynb",
    service_port=11434,
    auth="caddy",            # no built-in auth -> Caddy on public_port
    public_port=18080,
    gpu="T4",
    env={"CT_MODELS": "qwen2.5:0.5b"},
)

if __name__ == "__main__":
    main(SERVICE)
