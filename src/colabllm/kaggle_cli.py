#!/usr/bin/env python3
"""Helper subcommands and help listing for `colabllm kaggle`."""
from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def kaggle_services() -> dict[str, Path]:
    kdir = ROOT / "kaggle_services"
    if not kdir.is_dir():
        return {}
    return {
        p.stem: p
        for p in sorted(kdir.glob("*.py"))
        if p.name != "__init__.py" and p.with_suffix(".ipynb").is_file()
    }


def _print_help(available: dict[str, Path]) -> int:
    print(
        "用法: colabllm kaggle [-a ACCOUNT] <服务名> <命令> [参数...]\n"
        "      colabllm kaggle services\n"
        "\n"
        "可用 Kaggle 运行时: " + (", ".join(available) or "(无)") + "\n"
        "\n"
        "示例:\n"
        "  colabllm kaggle services\n"
        "  colabllm kaggle web-probe up --quick-tunnel\n"
        "  colabllm kaggle -a work web-probe status --json\n"
        "  colabllm kaggle web-probe restart\n"
        "  colabllm kaggle web-probe down --delete-tunnel\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Strip leading global flags forwarded from cli.py
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in ("--debug", "--quiet", "--no-color"):
            i += 1
        elif tok in ("--command-timeout", "--account"):
            i += 2
        elif tok.startswith(("--command-timeout=", "--account=")):
            i += 1
        else:
            cleaned = args[i:]
            break

    available = kaggle_services()
    if not cleaned or cleaned[0] in ("-h", "--help"):
        return _print_help(available)
    if cleaned[0] == "help":
        if len(cleaned) >= 2 and cleaned[1] in available:
            os.execv(sys.executable, [sys.executable, str(available[cleaned[1]]), "--help"])
        return _print_help(available)
    if cleaned[0] in ("services", "--list"):
        extra = [x for x in cleaned[1:] if x not in ("--debug", "--quiet", "--no-color")]
        if extra:
            print("用法: colabllm kaggle services", file=sys.stderr)
            return 2
        for name in available:
            print(name)
        return 0
    print(f"未知 Kaggle 运行时或命令: {cleaned[0]}\n使用 colabllm kaggle --help 查看可用运行时。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
