#!/usr/bin/env python3
"""LongCat-Video on Colab G4, with Caddy-protected Gradio UI.

  ./services/longcat-video.py up --domain
  ./services/longcat-video.py info | status | logs -f | down
  ./services/longcat-video.py up -i 2 --domain --gpu G4
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from colabsvc import Service, main  # noqa: E402

SERVICE = Service(
    name="longcat-video",
    notebook="longcat-video.ipynb",
    service_port=7860,
    public_port=18080,
    auth="caddy",
    gpu="G4",
    ready_timeout=5400,
)

if __name__ == "__main__":
    main(SERVICE)
