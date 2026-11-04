"""ComfyUI custom node registering /api/ct/* endpoints for local SSD model downloads."""
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web
from server import PromptServer

sys.path.insert(0, "/content/ComfyUI")
from ct_workflow_helper import list_ssd_models, sync_workflow_models, validate_model_destination

JOBS = {}
_DL_LOCK = threading.Lock()
_MAX_JOBS = 100


def _prune_jobs() -> None:
    if len(JOBS) <= _MAX_JOBS:
        return
    finished = sorted(
        (jid for jid, rec in JOBS.items() if rec.get("status") != "running"),
        key=lambda jid: JOBS[jid].get("finished_at", 0),
    )
    for jid in finished[: max(0, len(JOBS) - _MAX_JOBS)]:
        JOBS.pop(jid, None)


def _run_job(job_id, wf, wf_json_obj=None, wf_label=None):
    job = JOBS[job_id]
    with _DL_LOCK:
        try:
            res = sync_workflow_models(wf, job=job, wf_json_obj=wf_json_obj, wf_label=wf_label)
            job["result"] = res
            job["status"] = "done"
        except Exception as e:
            job["error"] = str(e)
            job["logs"].append(f"[workflow-dl] ERROR: {e}")
            job["status"] = "error"
        job["finished_at"] = time.time()


@PromptServer.instance.routes.post("/api/ct/pull-workflow")
async def ct_pull_workflow(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    wf = (body.get("workflow") or "").strip()
    wf_json_obj = body.get("workflow_json")
    wf_label = body.get("workflow_name") or "current_canvas.json"
    model_url = (body.get("model_url") or "").strip()
    if model_url and not isinstance(wf_json_obj, dict):
        d = (body.get("directory") or "diffusion_models").strip().strip("/")
        fn = (body.get("filename") or unquote(model_url.split("?")[0]).rstrip("/").split("/")[-1]).strip().lstrip("/")
        try:
            validate_model_destination(d, fn)
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)
        wf_label = f"direct_model:{d}/{fn}"
        wf_json_obj = {"properties": {"models": [{"url": model_url, "directory": d, "name": fn}]}}
    elif isinstance(wf_json_obj, dict):
        try:
            props = wf_json_obj.get("properties")
            if isinstance(props, dict) and isinstance(props.get("models"), list):
                for m in props["models"]:
                    if isinstance(m, dict) and ("directory" in m or "name" in m):
                        validate_model_destination(str(m.get("directory") or ""), str(m.get("name") or ""))
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)
    if not wf and not isinstance(wf_json_obj, dict):
        return web.json_response({"error": "missing 'workflow', 'workflow_json', or 'model_url'"}, status=400)
    _prune_jobs()
    job_id = secrets.token_hex(8)
    JOBS[job_id] = {
        "job_id": job_id, "workflow_spec": wf or wf_label, "status": "running",
        "total": 0, "completed": 0, "current": None, "models": [], "logs": [],
        "started_at": time.time(),
    }
    threading.Thread(target=_run_job, args=(job_id, wf, wf_json_obj, wf_label), daemon=True).start()
    return web.json_response(JOBS[job_id], status=202)


@PromptServer.instance.routes.get("/api/ct/pull-workflow/{job_id}")
async def ct_pull_workflow_status(request):
    job_id = request.match_info["job_id"]
    if job_id not in JOBS:
        return web.json_response({"error": "unknown job_id"}, status=404)
    return web.json_response(JOBS[job_id])


@PromptServer.instance.routes.get("/api/ct/ssd-status")
async def ct_ssd_status(request):
    info = list_ssd_models()
    active = [j for j in JOBS.values() if j.get("status") == "running"]
    info["active_job"] = active[-1] if active else None
    return web.json_response(info)


@PromptServer.instance.routes.get("/api/ct/ssd-downloader.js")
async def ct_ssd_downloader_js(request):
    js = Path("/content/ComfyUI/ct_ssd_downloader.js").read_text(encoding="utf-8")
    return web.Response(text=js, content_type="application/javascript")


NODE_CLASS_MAPPINGS = {}
