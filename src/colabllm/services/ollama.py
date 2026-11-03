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


def configure_cli(parser, commands, up):
    up.add_argument("--models", "--model", "-m", action="append", help="models to preload; repeat or separate with commas")


def prepare_up(args):
    if args.models:
        SERVICE.env["CT_MODELS"] = ",".join(args.models)


if __name__ == "__main__":
    from cli_support import run_cli
    run_cli(lambda: main(SERVICE, configure=configure_cli, prepare_up=prepare_up))
