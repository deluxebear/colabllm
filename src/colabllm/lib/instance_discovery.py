"""Stateless discovery of paired service definitions and local runtime instance state."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Callable, Iterable, Literal

from processes import runner_alive

INSTANCE_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
KAGGLE_CATALOG_PREFIX = "kaggle-"
KAGGLE_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,46}[A-Za-z0-9])?$")
KAGGLE_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,47}$")


@dataclass(frozen=True)
class ProviderServiceSpec:
    provider: Literal["colab", "kaggle"]
    short_name: str
    catalog_name: str
    script_path: Path
    notebook_path: Path


def kaggle_catalog_name(short_name: str) -> str:
    return f"{KAGGLE_CATALOG_PREFIX}{short_name}"


def parse_catalog_name(name: str) -> tuple[Literal["colab", "kaggle"], str]:
    if name.startswith(KAGGLE_CATALOG_PREFIX) and len(name) > len(KAGGLE_CATALOG_PREFIX):
        return ("kaggle", name[len(KAGGLE_CATALOG_PREFIX):])
    return ("colab", name)


def discover_service_pairs(services_dir: Path | None = None) -> tuple[str, ...]:
    """Return sorted Colab service names having paired .py and .ipynb definitions."""
    target = services_dir if services_dir is not None else Path(__file__).resolve().parents[1] / "services"
    if not target.is_dir():
        return ()
    return tuple(sorted(p.stem for p in target.glob("*.py") if p.with_suffix(".ipynb").is_file()))


def discover_kaggle_service_pairs(kaggle_services_dir: Path | None = None) -> tuple[str, ...]:
    """Return sorted Kaggle short service names having paired .py and .ipynb definitions."""
    target = (
        kaggle_services_dir
        if kaggle_services_dir is not None
        else Path(__file__).resolve().parents[1] / "kaggle_services"
    )
    if not target.is_dir():
        return ()
    return tuple(sorted(p.stem for p in target.glob("*.py") if p.name != "__init__.py" and p.with_suffix(".ipynb").is_file()))


def discover_all_provider_services(
    *,
    services_dir: Path | None = None,
    kaggle_services_dir: Path | None = None,
) -> dict[str, ProviderServiceSpec]:
    """Return mapping of catalog_name -> ProviderServiceSpec across Colab and Kaggle."""
    pkg_dir = Path(__file__).resolve().parents[1]
    colab_dir = services_dir if services_dir is not None else pkg_dir / "services"
    kaggle_dir = kaggle_services_dir if kaggle_services_dir is not None else pkg_dir / "kaggle_services"
    specs: dict[str, ProviderServiceSpec] = {}
    for name in discover_service_pairs(colab_dir):
        specs[name] = ProviderServiceSpec(
            provider="colab",
            short_name=name,
            catalog_name=name,
            script_path=colab_dir / f"{name}.py",
            notebook_path=colab_dir / f"{name}.ipynb",
        )
    for short in discover_kaggle_service_pairs(kaggle_dir):
        cat = kaggle_catalog_name(short)
        specs[cat] = ProviderServiceSpec(
            provider="kaggle",
            short_name=short,
            catalog_name=cat,
            script_path=kaggle_dir / f"{short}.py",
            notebook_path=kaggle_dir / f"{short}.ipynb",
        )
    return specs


def match_service_name(session_name: str, service_names: Iterable[str]) -> tuple[str, str] | None:
    """Split '<service>' or '<service>-<instance>' using longest known service prefix."""
    for svc in sorted(set(service_names), key=lambda name: (-len(name), name)):
        if session_name == svc:
            return (svc, "")
        if session_name.startswith(svc + "-"):
            return (svc, session_name[len(svc) + 1:])
    return None


def list_instance_dir_names(
    services_dir: Path,
    service: str,
    service_names: Iterable[str],
    *,
    instance_re: re.Pattern[str] = INSTANCE_ID_RE,
) -> list[str]:
    """List instance IDs for a service purely from directory names without reading state.json."""
    if not services_dir.is_dir():
        return []
    names = set(service_names) | {service}
    longer = {name for name in names if len(name) > len(service) and name.startswith(service + "-")}
    prefix = service + "-"
    results: list[str] = []
    for entry in services_dir.iterdir():
        if not entry.is_dir() or not entry.name.startswith(prefix):
            continue
        if any(entry.name == other or entry.name.startswith(other + "-") for other in longer):
            continue
        inst = entry.name[len(prefix):]
        if instance_re.fullmatch(inst):
            results.append(inst)
    return sorted(results)


def list_raw_instances(
    services_dir: Path,
    service_names: Iterable[str],
    *,
    target_service: str | None = None,
    alive_fn: Callable[[dict], bool] = runner_alive,
    on_corrupt: Literal["skip", "error"] = "skip",
) -> list[dict]:
    """Read raw instance state.json dicts without caching or filtering credentials."""
    if not services_dir.is_dir():
        return []
    known = set(service_names)
    if target_service is not None:
        known = known | {target_service}
    longer = (
        {name for name in known if len(name) > len(target_service) and name.startswith(target_service + "-")}
        if target_service is not None
        else set()
    )

    items: list[dict] = []
    for d in sorted(services_dir.iterdir()):
        if not d.is_dir():
            continue
        if target_service is not None:
            if d.name == target_service:
                dir_inst = ""
            elif d.name.startswith(target_service + "-"):
                dir_inst = d.name[len(target_service) + 1:]
            else:
                continue
        sp = d / "state.json"
        if not sp.is_file():
            continue
        try:
            st_stat = sp.stat()
            st = json.loads(sp.read_text(encoding="utf-8"))
        except Exception as err:
            if on_corrupt == "error":
                raise RuntimeError(f"无法读取状态文件 {sp}: {err}") from err
            continue
        if not isinstance(st, dict):
            if on_corrupt == "error":
                raise RuntimeError(f"状态文件格式无效 {sp}")
            continue

        recorded_base = st.get("base_service")
        if target_service is not None:
            if recorded_base is not None:
                if recorded_base != target_service:
                    continue
            elif any(d.name == other or d.name.startswith(other + "-") for other in longer):
                continue
            svc_name = target_service
            inst_id = dir_inst
        else:
            if isinstance(recorded_base, str) and recorded_base:
                svc_name = recorded_base
                if d.name == svc_name:
                    inst_id = ""
                elif d.name.startswith(svc_name + "-"):
                    inst_id = d.name[len(svc_name) + 1:]
                else:
                    inst_id = d.name
            else:
                matched = match_service_name(d.name, known)
                if matched is None:
                    continue
                svc_name, inst_id = matched

        alive = bool(alive_fn(st))
        ready = bool(st.get("ready") is True and alive)
        items.append({
            "service": svc_name,
            "instance": inst_id or "default",
            "instance_arg": inst_id,
            "name": d.name,
            "session_name": d.name,
            "alive": alive,
            "ready": ready,
            "state_mtime_ns": st_stat.st_mtime_ns,
            "state_size": st_stat.st_size,
            "state_path": sp,
            "state": st,
        })
    return items
