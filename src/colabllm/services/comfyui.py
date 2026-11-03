#!/usr/bin/env python3
"""ComfyUI + ComfyUI-Manager on Google Colab behind Caddy auth (notebook: comfyui.ipynb).

ComfyUI has no built-in authentication, so Caddy sits in front of it:
  browser -> basic auth (user/password),  API clients -> `Authorization: Bearer <token>`.

Storage layout:
  * Models (`/content/ComfyUI/models`): stored on fast local NVMe SSD (never on Google Drive).
    Downloaded on demand per workflow via `--workflow <url-or-template>` at startup, or live while
    running via `colabllm comfyui pull-workflow <url-or-template>`.
  * User data (`custom_nodes`, `user`, `input`, `output`): persisted on Google Drive under
    `/content/drive/MyDrive/colab-llm/comfyui`.

Usage:
  colabllm comfyui up --domain --workflow https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_t2v.json
  colabllm comfyui up -i 2 --domain --workflow video_minimax_h3_i2v.json   # 2nd instance (comfyui-2.example.com)
  colabllm comfyui pull-workflow [-i ID | -A] video_minimax_h3_i2v.json
  colabllm comfyui pull-model [-i ID | -A] https://huggingface.co/.../model.safetensors --dir loras [--name model.safetensors]
  colabllm comfyui ps | info [-i ID] | logs [-i ID] -f | status [-i ID] | down [-i ID | --all] [--delete-tunnel]
"""
import argparse
import copy
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from cli_support import safe_urlopen
from colabsvc import (  # noqa: E402
    Service,
    State,
    apply_instance,
    die,
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
            die(f"no running {base_name} instances found; run `colabllm comfyui ps` or `up` first")
        return targets
    return [SERVICE]


def _run_remote_pull_job(body_dict: dict, svc: Service | None = None) -> None:
    svc = svc or SERVICE
    st = State(svc).load()
    url = (st.get("url") or "").rstrip("/")
    token = st.get("api_token")
    if not url or not token:
        die(f"{svc.name} is not running (no URL/token in {State(svc).path}); run `up` first")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    payload = json.dumps(body_dict).encode("utf-8")
    req = urllib.request.Request(f"{url}/api/ct/pull-workflow", data=payload, headers=headers, method="POST")
    try:
        with safe_urlopen(req, timeout=60) as resp:
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
            with safe_urlopen(poll_req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8"))
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= 10:
                die(f"[{svc.name}] lost connection polling job {job_id}: {e}")
            time.sleep(3)


def _cmd_pull_workflow(raw_args: list[str]) -> None:
    """Trigger live on-demand model download into the running Colab VM's local SSD via /api/ct/pull-workflow."""
    args = raw_args if isinstance(raw_args, argparse.Namespace) else pull_parser("pull-workflow").parse_args(raw_args)
    wf_specs, all_instances, instances_csv = args.workflows, args.all_instances, args.instances
    if not wf_specs:
        die("usage: colabllm comfyui pull-workflow [-i ID | -A | --instances 1,2] <workflow-url-or-template-name> [...]")
    targets = _resolve_target_services(all_instances=all_instances, instances_csv=instances_csv)
    for svc in targets:
        for spec in wf_specs:
            log(f"[{svc.name}] requesting live SSD model download for workflow: {spec}")
            _run_remote_pull_job({"workflow": spec}, svc=svc)


def _cmd_pull_model(raw_args: list[str]) -> None:
    """Download a single model URL directly into /content/ComfyUI/models/<directory>/<filename> on local SSD."""
    args = raw_args if isinstance(raw_args, argparse.Namespace) else pull_parser("pull-model").parse_args(raw_args)
    all_instances, instances_csv = args.all_instances, args.instances
    model_url = args.model_url
    target_dir = (args.directory or args.directory_pos or "").strip("/")
    filename = (args.filename or "").lstrip("/")
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



def pull_parser(name, parser=None):
    parser = parser or argparse.ArgumentParser(prog=f"colabllm comfyui {name}")
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("-A", "--all-instances", action="store_true", help="target all running instances")
    targets.add_argument("--instances", default="", help="comma-separated instance IDs")
    if name == "pull-workflow":
        parser.add_argument("workflows", nargs="+", help="workflow URLs or template names")
    else:
        parser.add_argument("model_url")
        parser.add_argument("directory_pos", nargs="?", help="model directory (or use --dir)")
        parser.add_argument("--dir", "-d", "--directory", dest="directory")
        parser.add_argument("--name", "-n", "--filename", dest="filename")
    return parser


def configure_cli(parser, commands, up):
    up.add_argument("--workflow", "-w", action="append", help="workflow URL or template; repeat to download multiple")
    for name in ("pull-workflow", "pull-model"):
        pull_parser(name, commands.add_parser(name, help="download workflow models" if name == "pull-workflow" else "download a model"))


def prepare_up(args):
    if args.workflow:
        SERVICE.env["CT_WORKFLOW"] = ",".join(args.workflow)


if __name__ == "__main__":
    from cli_support import run_cli
    run_cli(lambda: main(SERVICE, configure=configure_cli, prepare_up=prepare_up,
        handlers={"pull-workflow": _cmd_pull_workflow, "pull-model": _cmd_pull_model}))
