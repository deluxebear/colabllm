"""Writable application data, independent of the installation directory."""
import json
import os
from pathlib import Path
import secrets
from typing import Any


def user_home() -> Path:
    override = os.environ.get("COLABLLM_HOME")
    return Path(override).expanduser().resolve() if override else Path.home() / ".colabllm"


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_private(path: str | Path, data: Any) -> None:
    """Atomically write JSON data with 0600 permissions inside a 0700 parent directory."""
    target = Path(path)
    private_dir(target.parent)
    temp = target.with_name(f"{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as output:
            os.chmod(temp, 0o600)
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.write("\n")
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
