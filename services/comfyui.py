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
  ./services/comfyui.py pull-workflow video_minimax_h3_i2v.json
  ./services/comfyui.py info | logs -f | status | down [--delete-tunnel]
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from colabsvc import Service, State, die, log, main  # noqa: E402

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


def _cmd_pull_workflow(wf_specs: list[str]) -> None:
    """Trigger live on-demand model download into the running Colab VM's local SSD via /api/ct/pull-workflow."""
    if not wf_specs:
        die("usage: ./services/comfyui.py pull-workflow <workflow-url-or-template-name> [...]")
    st = State(SERVICE).load()
    url = (st.get("url") or "").rstrip("/")
    token = st.get("api_token")
    if not url or not token:
        die("comfyui is not running (no URL/token in .state/comfyui/state.json); run `up` first")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    for spec in wf_specs:
        log(f"requesting live SSD model download for workflow: {spec}")
        payload = json.dumps({"workflow": spec}).encode("utf-8")
        req = urllib.request.Request(f"{url}/api/ct/pull-workflow", data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                res = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            die(f"HTTP {e.code} starting pull-workflow: {body}")
        if "job_id" not in res:
            # Backward-compatible synchronous response
            print(json.dumps(res, indent=2, ensure_ascii=False))
            continue
        job_id = res["job_id"]
        seen_logs = 0
        consecutive_errors = 0
        while True:
            for line in res.get("logs", [])[seen_logs:]:
                print(line, flush=True)
                seen_logs += 1
            status = res.get("status")
            if status == "done":
                print(json.dumps(res.get("result", res), indent=2, ensure_ascii=False))
                break
            if status == "error":
                die(f"pull-workflow failed: {res.get('error', 'unknown error')}")
            time.sleep(2)
            poll_req = urllib.request.Request(f"{url}/api/ct/pull-workflow/{job_id}", headers=headers, method="GET")
            try:
                with urllib.request.urlopen(poll_req, timeout=30) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    die(f"lost connection polling job {job_id}: {e}")
                time.sleep(3)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "pull-workflow":
        _cmd_pull_workflow(sys.argv[2:])
        sys.exit(0)
    _extract_workflow_arg()
    main(SERVICE)
