"""Stateless discovery of paired service definitions and local runtime instance state."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Callable, Iterable, Literal

from processes import runner_alive

INSTANCE_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")


def discover_service_pairs(services_dir: Path | None = None) -> tuple[str, ...]:
    """Return sorted service names having paired .py and .ipynb definitions."""
    target = services_dir if services_dir is not None else Path(__file__).resolve().parents[1] / "services"
    if not target.is_dir():
        return ()
    return tuple(sorted(p.stem for p in target.glob("*.py") if p.with_suffix(".ipynb").is_file()))


def match_service_name(session_name: str, service_names: Iterable[str]) -> tuple[str, str] | None:
    """Split '<service>' or '<service>-<instance>' using longest known service prefix."""
    for svc in sorted(set(service_names), key=lambda name: (-len(name), name)):
        if session_name == svc:
            return (svc, "")
        if session_name.startswith(svc + "-"):
            return (svc, session_name[len(svc) + 1:])
    return None


def list_instance_dir_names(services_dir: Path, service: str, service_names: Iterable[str]) -> list[str]:
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
        if INSTANCE_ID_RE.fullmatch(inst):
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
