"""Zero-dependency local Web UI backend for ColabLLM."""
from __future__ import annotations

from pathlib import Path
import sys

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
