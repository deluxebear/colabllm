#!/usr/bin/env python3
"""ComfyUI + ComfyUI-Manager on Google Colab behind Caddy auth (notebook: comfyui.ipynb).

ComfyUI has no built-in authentication, so Caddy sits in front of it:
  browser -> basic auth (user/password),  API clients -> `Authorization: Bearer <token>`.

Storage layout:
  * Models (`/content/ComfyUI/models`): stored on fast local NVMe SSD (never on Google Drive).
    Downloaded on demand per workflow via `--workflow <url-or-template>` at startup, or live while
    running via `./services/comfyui.py pull-workflow <url-or-template>`.
  * User data (`custom_nodes`, `user`, `input`, `output`): persisted on Google Drive under
    `/content/drive/MyDrive/colab-llm/comfyui`.

Usage:
  ./services/comfyui.py up --domain --workflow https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_t2v.json
  ./services/comfyui.py up -i 2 --domain --workflow video_minimax_h3_i2v.json   # 2nd instance (comfyui-2.example.com)
  ./services/comfyui.py pull-workflow [-i ID | -A] video_minimax_h3_i2v.json
  ./services/comfyui.py pull-model [-i ID | -A] https://huggingface.co/.../model.safetensors --dir loras [--name model.safetensors]
  ./services/comfyui.py ps | info [-i ID] | logs [-i ID] -f | status [-i ID] | down [-i ID | --all] [--delete-tunnel]
"""
import copy
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from colabsvc import (  # noqa: E402
    Service,
    State,
    apply_instance,
    die,
    extract_instance_arg,
    list_service_instances,
    log,
    main,
)

SERVICE = Service(
    name="comfyui",
    notebook="comfyui.ipynb",
    service_port=8188,
    auth="caddy",            # ComfyUI has no built-in auth -> Caddy on public_port
    public_port=18080,
    gpu="L4",
    ready_timeout=2400,
    env={
        "CT_COMFY_LEGACY_UI": "1",
    },
)

KNOWN_MODEL_DIRS = (
    "diffusion_models", "loras", "vae", "text_encoders", "checkpoints",
    "clip_vision", "controlnet", "upscale_models", "audio_encoders",
    "model_patches", "embeddings", "hypernetworks", "gligen", "style_models",
)


def _extract_workflow_arg() -> None:
    """Support `--workflow <url-or-template>` (can be passed multiple times or comma-separated) on `up`."""
    argv = sys.argv[1:]
    workflows: list[str] = []
    cleaned = [sys.argv[0]]
    i = 0
    while i < len(argv):
        if argv[i] in ("--workflow", "-w") and i + 1 < len(argv):
            workflows.append(argv[i + 1])
            i += 2
        elif argv[i].startswith("--workflow="):
            workflows.append(argv[i].split("=", 1)[1])
            i += 1
        else:
            cleaned.append(argv[i])
            i += 1
    if workflows:
        SERVICE.env["CT_WORKFLOW"] = ",".join(workflows)
    sys.argv = cleaned


def _resolve_target_services(all_instances: bool = False, instances_csv: str = "") -> list[Service]:
    """Return target Service instance(s) for pull-workflow / pull-model."""
    base_name = getattr(SERVICE, "base_name", SERVICE.name)
    if instances_csv:
        targets: list[Service] = []
        for raw_inst in instances_csv.split(","):
            inst = raw_inst.strip()
            s = copy.deepcopy(SERVICE)
            s.name = base_name
            s.base_name = base_name
            if inst and inst not in ("default", "0", base_name):
                apply_instance(s, inst)
            targets.append(s)
        if targets:
            return targets
    if all_instances:
        targets = []
        for rec in list_service_instances(base_name):
            if rec["running"] and rec["url"] and rec["api_token"]:
                s = copy.deepcopy(SERVICE)
                s.name = rec["name"]
                s.base_name = base_name
                targets.append(s)
        if not targets:
            die(f"no running {base_name} instances found; run `./services/comfyui.py ps` or `up` first")
        return targets
    return [SERVICE]


def _extract_multi_instance_flags(args: list[str]) -> tuple[list[str], bool, str]:
    """Strip `-A / --all-instances` and `--instances <csv>` from args list."""
    cleaned: list[str] = []
    all_instances = False
    instances_csv = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--all-instances", "-A"):
            all_instances = True
            i += 1
        elif a == "--instances" and i + 1 < len(args):
            instances_csv = args[i + 1]
            i += 2
        elif a.startswith("--instances="):
            instances_csv = a.split("=", 1)[1]
            i += 1
        else:
            cleaned.append(a)
            i += 1
    return cleaned, all_instances, instances_csv


def _run_remote_pull_job(body_dict: dict, svc: Service | None = None) -> None:
    svc = svc or SERVICE
    st = State(svc).load()
    url = (st.get("url") or "").rstrip("/")
    token = st.get("api_token")
    if not url or not token:
        die(f"{svc.name} is not running (no URL/token in .state/{svc.name}/state.json); run `up` first")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    payload = json.dumps(body_dict).encode("utf-8")
    req = urllib.request.Request(f"{url}/api/ct/pull-workflow", data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        die(f"[{svc.name}] HTTP {e.code} starting download job: {body}")
    if "job_id" not in res:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return
    job_id = res["job_id"]
    seen_logs = 0
    consecutive_errors = 0
    while True:
        for line in res.get("logs", [])[seen_logs:]:
            print(f"[{svc.name}] {line}", flush=True)
            seen_logs += 1
        status = res.get("status")
        if status == "done":
            print(json.dumps(res.get("result", res), indent=2, ensure_ascii=False))
            break
        if status == "error":
            die(f"[{svc.name}] download failed: {res.get('error', 'unknown error')}")
        time.sleep(2)
        poll_req = urllib.request.Request(f"{url}/api/ct/pull-workflow/{job_id}", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(poll_req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8"))
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= 10:
                die(f"[{svc.name}] lost connection polling job {job_id}: {e}")
            time.sleep(3)


def _cmd_pull_workflow(raw_args: list[str]) -> None:
    """Trigger live on-demand model download into the running Colab VM's local SSD via /api/ct/pull-workflow."""
    wf_specs, all_instances, instances_csv = _extract_multi_instance_flags(raw_args)
    if not wf_specs:
        die("usage: ./services/comfyui.py pull-workflow [-i ID | -A | --instances 1,2] <workflow-url-or-template-name> [...]")
    targets = _resolve_target_services(all_instances=all_instances, instances_csv=instances_csv)
    for svc in targets:
        for spec in wf_specs:
            log(f"[{svc.name}] requesting live SSD model download for workflow: {spec}")
            _run_remote_pull_job({"workflow": spec}, svc=svc)


def _cmd_pull_model(raw_args: list[str]) -> None:
    """Download a single model URL directly into /content/ComfyUI/models/<directory>/<filename> on local SSD."""
    args, all_instances, instances_csv = _extract_multi_instance_flags(raw_args)
    model_url = ""
    target_dir = ""
    filename = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--dir", "-d", "--directory") and i + 1 < len(args):
            target_dir = args[i + 1].strip().strip("/")
            i += 2
        elif a.startswith(("--dir=", "--directory=")):
            target_dir = a.split("=", 1)[1].strip().strip("/")
            i += 1
        elif a in ("--name", "-n", "--filename") and i + 1 < len(args):
            filename = args[i + 1].strip().lstrip("/")
            i += 2
        elif a.startswith(("--name=", "--filename=")):
            filename = a.split("=", 1)[1].strip().lstrip("/")
            i += 1
        elif not model_url and not a.startswith("-"):
            model_url = a.strip()
            i += 1
        elif not target_dir and not a.startswith("-"):
            target_dir = a.strip().strip("/")
            i += 1
        else:
            die(f"unknown argument: {a}\nusage: ./services/comfyui.py pull-model [-i ID | -A] <model-url> [--dir <directory>] [--name <filename>]")
    if not model_url:
        die("usage: ./services/comfyui.py pull-model [-i ID | -A] <model-url> [--dir <directory>] [--name <filename>]")
    clean_parts = [p for p in urllib.parse.unquote(model_url.split("?")[0]).split("/") if p]
    if not filename and clean_parts:
        filename = clean_parts[-1]
    if not target_dir:
        for seg in clean_parts:
            if seg in KNOWN_MODEL_DIRS:
                target_dir = seg
                break
    if not target_dir:
        die("could not auto-detect target directory from URL; please specify `--dir <directory>` (e.g. --dir loras, --dir diffusion_models)")
    if not filename:
        die("could not auto-detect filename from URL; please specify `--name <filename>`")
    targets = _resolve_target_services(all_instances=all_instances, instances_csv=instances_csv)
    for svc in targets:
        log(f"[{svc.name}] requesting live SSD model download: {model_url} -> models/{target_dir}/{filename}")
        _run_remote_pull_job({
            "model_url": model_url,
            "directory": target_dir,
            "filename": filename,
            "workflow_name": f"direct_model:{target_dir}/{filename}",
            "workflow_json": {
                "properties": {
                    "models": [{"url": model_url, "directory": target_dir, "name": filename}],
                }
            },
        }, svc=svc)


if __name__ == "__main__":
    SERVICE.base_name = SERVICE.name
    _inst_id, _rem_argv = extract_instance_arg(sys.argv[1:])
    if _inst_id:
        apply_instance(SERVICE, _inst_id)
    if len(_rem_argv) >= 1 and _rem_argv[0] == "pull-workflow":
        _cmd_pull_workflow(_rem_argv[1:])
        sys.exit(0)
    if len(_rem_argv) >= 1 and _rem_argv[0] == "pull-model":
        _cmd_pull_model(_rem_argv[1:])
        sys.exit(0)
    _extract_workflow_arg()
    main(SERVICE)
