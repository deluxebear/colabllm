#!/usr/bin/env python3
"""Ollama API on Google Colab behind Caddy auth (notebook: ollama.ipynb).

Ollama has no authentication, so Caddy sits in front of it:
  browser -> basic auth (user/password),  API clients -> `Authorization: Bearer <token>`.

  colabllm ollama up                     # quick tunnel
  colabllm ollama up --domain            # https://ollama.example.com
  colabllm ollama up -i 2 --domain       # 2nd instance -> https://ollama-2.example.com
  colabllm ollama up --models qwen2.5:7b,llama3.2:3b --domain
  colabllm ollama ps | info [-i ID] | logs [-i ID] -f | status [-i ID] | down [-i ID | --all] [--delete-tunnel]
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


def _extract_models_arg() -> None:
    argv = sys.argv[1:]
    cleaned = [sys.argv[0]]
    models: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] in ("--models", "--model", "-m") and i + 1 < len(argv):
            models.append(argv[i + 1])
            i += 2
        elif argv[i].startswith(("--models=", "--model=")):
            models.append(argv[i].split("=", 1)[1])
            i += 1
        else:
            cleaned.append(argv[i])
            i += 1
    if models:
        SERVICE.env["CT_MODELS"] = ",".join(models)
    sys.argv = cleaned


if __name__ == "__main__":
    _extract_models_arg()
    main(SERVICE)
