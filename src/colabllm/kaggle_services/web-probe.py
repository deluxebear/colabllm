#!/usr/bin/env python3
"""Pure-CPU HTTP connectivity and cache diagnostic probe for Kaggle sessions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from cli_support import UsageError, run_cli
from kagglesvc import KaggleService, main, normalize_kaggle_accelerator

SERVICE = KaggleService(
    name="web-probe",
    notebook="web-probe.ipynb",
    service_port=18090,
    auth="caddy",
    public_port=18080,
    accelerator="cpu",
    enable_internet=True,
    default_hostname="kaggle-web-probe",
    ready_timeout=600,
    has_web_ui=True,
)


def prepare_up(args) -> None:
    if getattr(args, "dry_run", False):
        return
    accel = normalize_kaggle_accelerator(getattr(args, "accelerator", None) or SERVICE.accelerator)
    if accel != "cpu":
        raise UsageError("web-probe 为纯 CPU 连通性诊断服务，仅在 --dry-run 下允许指定非 CPU --accelerator")


if __name__ == "__main__":
    run_cli(lambda: main(SERVICE, prepare_up=prepare_up))
