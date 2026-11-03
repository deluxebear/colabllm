"""Writable application data, independent of the installation directory."""
import os
from pathlib import Path


def user_home() -> Path:
    override = os.environ.get("COLABLLM_HOME")
    return Path(override).expanduser().resolve() if override else Path.home() / ".colabllm"


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
