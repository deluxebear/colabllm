#!/usr/bin/env python3
"""Openjutsu (MiniMax H3 motion transfer, object edit, face/colour post-processing & FLUX.3 picture prep) on Colab G4.

Co-locates all three loopback tiers inside one Colab G4 (RTX PRO 6000 Blackwell, 96 GB VRAM) VM behind Caddy auth:
  1. ComfyUI (`127.0.0.1:8188`) + all 13 Openjutsu custom node packs + local NVMe SSD models
  2. Patched `comfy-api-proxy` (`127.0.0.1:8189`) for live progress & multi-clip resume
  3. Openjutsu Next.js 16 production web UI & API (`127.0.0.1:3000`)

Usage:
  colabllm openjutsu up --domain
  colabllm openjutsu up --domain --profile core
  colabllm openjutsu check [--mode all|simple|advanced|object|face|color|montage|people|mask|fit]
  colabllm openjutsu swap --video input.mp4 -p person.png --clip-seconds 9.5 -o result.mp4
  colabllm openjutsu object --video input.mp4 --target jacket --change-to "leather jacket" -o edited.mp4
  colabllm openjutsu mask --video input.mp4 -o mask.mp4
  colabllm openjutsu face --video result.mp4 -p person.png -o fixed_face.mp4
  colabllm openjutsu color --video result.mp4 -o graded.mp4
  colabllm openjutsu job JOB_ID --wait -o result.mp4
  colabllm openjutsu download JOB_ID -o result.mp4
  colabllm openjutsu cancel JOB_ID
  colabllm openjutsu info | status | logs -f | down
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from cli_support import (  # noqa: E402
    CliError,
    Deadline,
    SameOriginRedirect,
    UsageError,
    diagnostic,
    nonnegative_seconds,
    positive_seconds,
    resolve_endpoint,
)
from colabsvc import Service, State, main  # noqa: E402

SERVICE = Service(
    name="openjutsu",
    notebook="openjutsu.ipynb",
    service_port=3000,
    auth="caddy",
    public_port=18080,
    gpu="G4",
    ready_timeout=3600,
    env={
        "CT_OPENJUTSU_PROFILE": "full",
        "CT_OPENJUTSU_CHAINS_STORAGE": "ssd",
        "CT_COMFY_VRAM_MODE": "auto",
    },
)

DEFAULT_LORA = ("minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors", 1.0)

DEFAULT_MODEL_SETTINGS: dict[str, object] = {
    "model": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "loras": [{"name": DEFAULT_LORA[0], "strength": 1}],
    "steps": 6,
    "textEncoder": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors",
    "videoVae": "minimax_h3_video_vae_int8_convrot.safetensors",
    "audioVae": "minimax_h3_audio_vae_fp32.safetensors",
    "acceleration": "none",
}

DEFAULT_TONE: dict[str, object] = {
    "mode": "Reinhard",
    "exposure": 0.09,
    "contrast": 0.84,
    "pivot": 0.69,
    "highlight_rolloff": 0.27,
    "shadow_lift": 0.15,
    "black_floor": 0.03,
    "saturation": 1.05,
    "strength": 0.2,
}

TONE_MODES = ("Filmic Soft", "Filmic Strong", "ACES Approx", "Reinhard", "Cineon-ish")
TONE_RANGES: dict[str, tuple[float, float]] = {
    "exposure": (-5.0, 5.0),
    "contrast": (0.0, 3.0),
    "pivot": (0.01, 1.0),
    "highlight_rolloff": (0.0, 2.0),
    "shadow_lift": (-1.0, 1.0),
    "black_floor": (0.0, 0.25),
    "saturation": (0.0, 3.0),
    "strength": (0.0, 2.0),
}

SETUP_PAYLOADS: dict[str, dict[str, object]] = {
    "mask": {"kind": "mask", "hatFix": True, "margin": "normal", "target": "person"},
    "people": {"kind": "people", "resolution": "736p"},
    "fit": {"kind": "fit", "resolution": "736p"},
    "color": {"kind": "color", "settings": {"detail": -0.2, "tone": dict(DEFAULT_TONE)}},
    "simple": {
        "kind": "simple",
        "settings": {
            "role": "",
            "resolution": "736p",
            "motion": 768,
            "hatFix": True,
            "noLipSync": True,
            "margin": "normal",
            "target": "person",
            "seed": 0,
            "clipSeconds": 6,
        },
        "model": DEFAULT_MODEL_SETTINGS,
    },
    "advanced": {
        "kind": "advanced",
        "pictures": 2,
        "settings": {
            "roles": ["", ""],
            "resolution": "736p",
            "motion": 768,
            "hatFix": True,
            "noLipSync": True,
            "margin": "normal",
            "seed": 0,
            "clipSeconds": 6,
        },
        "model": DEFAULT_MODEL_SETTINGS,
    },
    "object": {
        "kind": "object",
        "picture": True,
        "settings": {
            "mode": "change",
            "target": "jacket",
            "changeTo": "leather jacket",
            "biggestOnly": True,
            "keepOutline": False,
            "resolution": "736p",
            "motion": 768,
            "margin": "normal",
            "seed": 0,
            "clipSeconds": 6,
        },
        "model": DEFAULT_MODEL_SETTINGS,
    },
    "montage": {
        "kind": "montage",
        "pictures": 2,
        "review": None,
        "settings": {
            "roles": ["", ""],
            "resolution": "736p",
            "motion": 768,
            "hatFix": True,
            "noLipSync": True,
            "margin": "normal",
            "seed": 0,
            "clipSeconds": 6,
        },
        "model": DEFAULT_MODEL_SETTINGS,
    },
    "face": {
        "kind": "face",
        "settings": {"style": "mouth", "seed": 0, "chunkSeconds": 6.5},
        "model": DEFAULT_MODEL_SETTINGS,
    },
}

MODE_CHOICES = ("all", *SETUP_PAYLOADS.keys())
RESOLUTION_PRESETS = ("480p", "576p", "736p", "768p", "1080p")
MOTION_CHOICES = (512, 768, 1376)
TERMINAL_STATUSES = frozenset(("succeeded", "failed", "canceled", "expired"))
TRANSIENT_HTTP_CODES = frozenset((429, 502, 503, 504, 530))

_SPEED_LORA_RE = re.compile(
    r"dmad|light[ _-]?x2v|turbo|(?:^|[/ _-])(?:[12468][ _-]?steps?|lightning)(?:[_. -]|$)",
    re.IGNORECASE,
)
_VIDEO_EXT_MIME = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
}
_IMAGE_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def is_speed_lora(name: str) -> bool:
    return bool(_SPEED_LORA_RE.search(name.replace("\\", "/")))


def validate_resolution(value: str) -> str:
    clean = (value or "").strip()
    if clean in RESOLUTION_PRESETS:
        return clean
    match = re.fullmatch(r"(\d+)x(\d+)", clean)
    if match:
        w = round(int(match.group(1)) / 32) * 32
        h = round(int(match.group(2)) / 32) * 32
        if min(w, h) >= 256 and max(w, h) <= 2048:
            return clean
    raise UsageError(f"不支持的分辨率 {value!r}；可选 {RESOLUTION_PRESETS} 或 256–2048 范围内的 WIDTHxHEIGHT（如 736x1312）")


def validate_section(start: float, length: float) -> tuple[float, float]:
    if start < 0:
        raise UsageError("--start 必须 >= 0 秒")
    if length < 0 or (0 < length < 2.0):
        raise UsageError("--length 须为 0（完整视频）或 >= 2.0 秒")
    return start, length


def validate_clip_seconds(seconds: float, *, flag: str = "--clip-seconds") -> float:
    if not (2.5 <= seconds <= 15.0):
        raise UsageError(f"{flag} 必须在 2.5 到 15.0 秒之间")
    return seconds


def validate_seed(seed: int) -> int:
    if not (0 <= seed <= 0xFFFFFFFF):
        raise UsageError("--seed 必须是 0 到 4294967295 之间的整数")
    return seed


def validate_steps(steps: int) -> int:
    if not (1 <= steps <= 100):
        raise UsageError("--steps 必须是 1 到 100 之间的整数")
    return steps


def parse_lora_spec(spec: str) -> tuple[str, float]:
    raw = (spec or "").strip()
    if not raw:
        raise UsageError("LoRA 名称不能为空")
    if ":" in raw:
        name, strength_str = raw.rsplit(":", 1)
        try:
            strength = float(strength_str)
        except ValueError:
            name, strength = raw, 1.0
    else:
        name, strength = raw, 1.0
    name = name.strip()
    if not name:
        raise UsageError(f"无效的 LoRA 参数：{spec!r}")
    if not (-4.0 <= strength <= 4.0):
        raise UsageError(f"LoRA 权重必须在 -4 到 4 之间：{spec!r}")
    return name, strength


def resolve_loras(
    *,
    loras: list[str] | None,
    extra_loras: list[str] | None,
    no_lora: bool,
    acceleration: str,
) -> list[tuple[str, float]]:
    if no_lora:
        if loras or extra_loras:
            raise UsageError("--no-lora 不能与 --lora 或 --extra-lora 同时使用")
        resolved: list[tuple[str, float]] = []
    elif loras:
        resolved = [parse_lora_spec(item) for item in loras]
        if extra_loras:
            resolved.extend(parse_lora_spec(item) for item in extra_loras)
    else:
        resolved = [] if acceleration == "spectrum" else [DEFAULT_LORA]
        if extra_loras:
            resolved.extend(parse_lora_spec(item) for item in extra_loras)
    if len(resolved) > 4:
        raise UsageError("最多同时加载 4 个 LoRA")
    if acceleration == "spectrum" and any(strength != 0 and is_speed_lora(name) for name, strength in resolved):
        raise UsageError("开启 --acceleration spectrum 时不能同时启用加速类 LoRA（如 DMAD）；请改用 --no-lora 或移除加速 LoRA")
    return resolved


def validate_media_file(path_str: str, *, kind: str) -> tuple[Path, str]:
    path = Path(path_str).expanduser().resolve()
    if not path.is_file():
        raise UsageError(f"文件不存在：{path}")
    size = path.stat().st_size
    if size <= 0:
        raise UsageError(f"文件为空：{path}")
    if size > 500 * 1024 * 1024:
        raise UsageError(f"单文件超过 500 MB 上传上限：{path}")
    with path.open("rb") as fh:
        head = fh.read(12)
    ext = path.suffix.lower()
    if kind == "video":
        is_mp4_mov = len(head) >= 8 and head[4:8] == b"ftyp"
        is_mkv_webm = head.startswith(b"\x1a\x45\xdf\xa3")
        is_avi = len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI "
        if not (is_mp4_mov or is_mkv_webm or is_avi):
            raise UsageError(f"不支持的视频文件格式（需 MP4/MOV/MKV/WebM/AVI）：{path}")
        mime = _VIDEO_EXT_MIME.get(ext) or mimetypes.guess_type(path.name)[0]
        if not mime or not mime.startswith("video/"):
            mime = "video/mp4"
        return path, mime
    if kind == "image":
        is_png = head.startswith(b"\x89PNG\r\n\x1a\n")
        is_jpeg = head.startswith(b"\xff\xd8\xff")
        is_webp = len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        if not (is_png or is_jpeg or is_webp):
            raise UsageError(f"不支持的图片格式（需 PNG/JPEG/WebP）：{path}")
        if is_png:
            mime = "image/png"
        elif is_jpeg:
            mime = "image/jpeg"
        else:
            mime = _IMAGE_EXT_MIME.get(ext, "image/webp")
        return path, mime
    raise ValueError(f"Unsupported media kind: {kind}")


def _safe_part_filename(name: str) -> str:
    return name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")


class OpenjutsuClient:
    """Authenticated HTTP/multipart/SSE client for Openjutsu's Next.js API behind Caddy."""

    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0",
        }
        self.opener = urllib.request.build_opener(SameOriginRedirect())

    def open(
        self,
        path: str,
        *,
        method: str = "GET",
        data: object = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = 60.0,
    ):
        req = urllib.request.Request(
            self.url + path,
            data=data,
            headers={**self.headers, **(headers or {})},
            method=method,
        )
        try:
            return self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
                detail = parsed.get("error") or raw
            except json.JSONDecodeError:
                detail = raw.strip() or err.reason
            err_out = CliError(f"[{SERVICE.name}] HTTP {err.code} ({path}): {detail}")
            err_out.http_status = err.code  # type: ignore[attr-defined]
            err_out.retry_after = err.headers.get("Retry-After")  # type: ignore[attr-defined]
            raise err_out from None

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, object] | None = None,
        timeout: float | None = 60.0,
    ) -> dict[str, object]:
        headers = {"Accept": "application/json"}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        with self.open(path, method=method, data=payload, headers=headers, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def submit_form(
        self,
        fields: list[tuple[str, str]],
        files: list[tuple[str, Path, str]],
        *,
        timeout: float | None = 300.0,
    ) -> dict[str, object]:
        boundary = "openjutsu-" + uuid.uuid4().hex
        total_bytes = sum(path.stat().st_size for _, path, _ in files)
        if total_bytes > 512 * 1024 * 1024:
            raise UsageError("单次请求所有上传文件总大小不能超过 512 MB")

        for attempt in range(4):
            with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as body:
                for key, val in fields:
                    safe_key = _safe_part_filename(key)
                    body.write(
                        (
                            f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="{safe_key}"\r\n\r\n'
                            f"{val}\r\n"
                        ).encode("utf-8")
                    )
                for field_name, file_path, mime in files:
                    safe_key = _safe_part_filename(field_name)
                    safe_name = _safe_part_filename(file_path.name)
                    body.write(
                        (
                            f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="{safe_key}"; filename="{safe_name}"\r\n'
                            f"Content-Type: {mime}\r\n\r\n"
                        ).encode("utf-8")
                    )
                    with file_path.open("rb") as src:
                        shutil.copyfileobj(src, body)
                    body.write(b"\r\n")
                body.write(f"--{boundary}--\r\n".encode("utf-8"))
                length = body.tell()
                body.seek(0)
                try:
                    with self.open(
                        "/api/jobs",
                        method="POST",
                        data=body,
                        headers={
                            "Accept": "application/json",
                            "Content-Type": f"multipart/form-data; boundary={boundary}",
                            "Content-Length": str(length),
                        },
                        timeout=timeout,
                    ) as resp:
                        result = json.loads(resp.read().decode("utf-8"))
                        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
                            raise CliError("服务端返回了无效的任务响应")
                        return result
                except CliError as err:
                    status = getattr(err, "http_status", None)
                    if status == 429 and attempt < 3:
                        wait_s = min(int(getattr(err, "retry_after", None) or 5), 15)
                        diagnostic(f"[{SERVICE.name}] 服务端正在接收其他上传，{wait_s}s 后重试...")
                        Deadline(wait_s + 1).sleep(wait_s)
                        continue
                    raise
        raise CliError("提交任务失败：超过最大重试次数")

    def job(self, job_id: str, *, timeout: float | None = 30.0) -> dict[str, object]:
        safe_id = urllib.parse.quote(job_id, safe="")
        return self.request_json(f"/api/jobs/{safe_id}", timeout=timeout)

    def cancel(self, job_id: str, *, timeout: float | None = 30.0) -> dict[str, object]:
        safe_id = urllib.parse.quote(job_id, safe="")
        return self.request_json(f"/api/jobs/{safe_id}", method="DELETE", timeout=timeout)

    def _consume_events_once(self, job_id: str, kind: str, deadline: Deadline, last_msg: list[str | None]) -> dict[str, object] | None:
        safe_id = urllib.parse.quote(job_id, safe="")
        query = urllib.parse.urlencode({"kind": kind})
        with self.open(
            f"/api/jobs/{safe_id}/events?{query}",
            headers={"Accept": "text/event-stream"},
            timeout=deadline.remaining(90),
        ) as resp:
            while True:
                raw_line = resp.readline()
                if not raw_line:
                    return None
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "progress":
                    node = event.get("node") or {}
                    title = node.get("title") or "Running"
                    clip = node.get("clip")
                    step, steps = event.get("step"), event.get("steps")
                    prefix = f"clip {clip} | " if clip else ""
                    step_txt = f" ({step}/{steps})" if step is not None and steps else ""
                    msg = f"[{SERVICE.name}] {prefix}{title}{step_txt}"
                    if msg != last_msg[0]:
                        diagnostic(msg)
                        last_msg[0] = msg
                elif etype == "status":
                    st = event.get("status")
                    qpos = event.get("queuePosition")
                    msg = f"[{SERVICE.name}] 状态: {st}" + (f" (排队 #{qpos})" if qpos is not None else "")
                    if msg != last_msg[0]:
                        diagnostic(msg)
                        last_msg[0] = msg
                elif etype == "job" and isinstance(event.get("job"), dict):
                    job_obj = event["job"]
                    if job_obj.get("status") in TERMINAL_STATUSES:
                        return job_obj
        return None

    def wait(
        self,
        job_id: str,
        *,
        kind: str = "simple",
        poll: float = 3.0,
        timeout: float = 0.0,
        use_events: bool = True,
    ) -> dict[str, object]:
        deadline = Deadline(timeout, f"等待超时；远端任务仍在继续运行：{job_id}")
        last_msg: list[str | None] = [None]
        sse_supported = use_events
        while True:
            state: dict[str, object] | None = None
            if sse_supported:
                try:
                    state = self._consume_events_once(job_id, kind, deadline, last_msg)
                except CliError as err:
                    code = getattr(err, "http_status", None)
                    if code == 404:
                        sse_supported = False
                    elif code not in TRANSIENT_HTTP_CODES:
                        raise
                except (urllib.error.URLError, TimeoutError, OSError):
                    pass

            if state is None:
                try:
                    state = self.job(job_id, timeout=deadline.remaining(30))
                except CliError as err:
                    if getattr(err, "http_status", None) not in TRANSIENT_HTTP_CODES:
                        raise
                    msg = f"[{SERVICE.name}] 连接暂时波动，正在重连（后台任务不受影响）..."
                    if msg != last_msg[0]:
                        diagnostic(msg)
                        last_msg[0] = msg
                    deadline.sleep(poll)
                    continue
                except (urllib.error.URLError, TimeoutError, OSError):
                    msg = f"[{SERVICE.name}] 连接暂时波动，正在重连（后台任务不受影响）..."
                    if msg != last_msg[0]:
                        diagnostic(msg)
                        last_msg[0] = msg
                    deadline.sleep(poll)
                    continue

            status = str(state.get("status") or "")
            if status == "succeeded":
                return state
            if status in ("failed", "canceled", "expired"):
                reason = state.get("error") or status
                raise CliError(f"任务 {job_id} 结束状态为 {status}：{reason}")

            qpos = state.get("queuePosition")
            msg = f"[{SERVICE.name}] 状态: {status}" + (f" (排队 #{qpos})" if qpos is not None else "")
            if msg != last_msg[0]:
                diagnostic(msg)
                last_msg[0] = msg
            if not sse_supported:
                deadline.sleep(poll)

    def download(self, state: dict[str, object], destination: str) -> str:
        if state.get("status") != "succeeded":
            raise UsageError("任务尚未成功完成；请加 --wait 等待完成后下载")
        video_info = state.get("video")
        data_url = state.get("data")
        rel_url: str | None = None
        if isinstance(video_info, dict) and isinstance(video_info.get("url"), str):
            rel_url = video_info["url"]
        elif isinstance(data_url, str):
            rel_url = data_url
        if not rel_url:
            raise CliError(f"任务 {state.get('id')} 未返回可下载的输出文件")
        sep = "&" if "?" in rel_url else "?"
        dl_path = f"{rel_url}{sep}download=1"
        output = Path(destination).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=output.parent,
                prefix="." + output.name + ".",
                suffix=".part",
                delete=False,
            ) as target:
                temporary = Path(target.name)
                with self.open(dl_path, timeout=300) as resp:
                    shutil.copyfileobj(resp, target)
            temporary.replace(output)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        return str(output)


def _resolve_client(args: argparse.Namespace) -> OpenjutsuClient:
    explicit_token = getattr(args, "api_token", None) or os.environ.get("OPENJUTSU_API_TOKEN") or os.environ.get("OPENJUTSU_TOKEN")
    url, token = resolve_endpoint(
        State(SERVICE).load(),
        getattr(args, "url", None),
        explicit_token,
        url_env="OPENJUTSU_URL",
    )
    return OpenjutsuClient(url, token)


def _add_endpoint_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", help="覆盖服务 URL（默认读取当前实例状态或 $OPENJUTSU_URL）")
    parser.add_argument("--api-token", "--token", dest="api_token",
                        help="Bearer API Token（默认读取当前实例状态或 $OPENJUTSU_API_TOKEN）")


def _add_wait_download_flags(parser: argparse.ArgumentParser, *, include_output: bool = True) -> None:
    parser.add_argument("--wait", action="store_true",
                        help="持续等待任务完成并实时显示分段/采样步数进度（Ctrl+C 仅退出本机等待）")
    if include_output:
        parser.add_argument("-o", "--output", help="完成后自动下载成片到本地路径（指定 -o 会自动开启等待）")
    parser.add_argument("--poll", type=positive_seconds, default=3.0, help="轮询回退间隔秒数（默认: 3）")
    parser.add_argument("--timeout", type=nonnegative_seconds, default=0.0,
                        help="最长等待秒数；0 表示不限时直至完成（默认: 0）")
    parser.add_argument("--no-events", action="store_true", help="禁用 SSE 实时进度流，仅使用轮询查询状态")


def _add_section_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", type=nonnegative_seconds, default=0.0, help="截取原视频起始秒数（默认: 0）")
    parser.add_argument("--length", type=nonnegative_seconds, default=0.0,
                        help="截取原视频时长秒数；0 表示处理至视频结尾（默认: 0，若非 0 须 >= 2）")


def _add_model_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--steps", type=int, default=6, help="扩散采样步数 1–100（默认: 6；追求速度可设 4）")
    parser.add_argument("--model", default=str(DEFAULT_MODEL_SETTINGS["model"]), help="H3 扩散底模文件名")
    parser.add_argument("--lora", action="append", dest="loras", metavar="NAME[:STRENGTH]",
                        help="替换默认 LoRA 列表（可重复最多 4 次，如 drive/my.safetensors:0.8）")
    parser.add_argument("--extra-lora", action="append", dest="extra_loras", metavar="NAME[:STRENGTH]",
                        help="在默认 DMAD 加速 LoRA 基础上追加额外 LoRA（如 drive/style.safetensors:0.7）")
    parser.add_argument("--no-lora", action="store_true", help="不加载任何 LoRA")
    parser.add_argument("--text-encoder", default=str(DEFAULT_MODEL_SETTINGS["textEncoder"]), help="H3 文本编码器文件名")
    parser.add_argument("--describer", help="可选视觉描述器模型文件名（models/text_encoders）")
    parser.add_argument("--video-vae", default=str(DEFAULT_MODEL_SETTINGS["videoVae"]), help="视频 VAE 文件名")
    parser.add_argument("--audio-vae", default=str(DEFAULT_MODEL_SETTINGS["audioVae"]), help="音频 VAE 文件名")
    parser.add_argument("--acceleration", choices=("none", "spectrum"), default="none",
                        help="推理加速选项（默认: none；启用 spectrum 需配合 --no-lora）")


def _add_h3_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--resolution", default="736p",
                        help="输出分辨率预设（480p|576p|736p|768p|1080p）或自定义 WIDTHxHEIGHT（默认: 736p）")
    parser.add_argument("--motion", type=int, choices=MOTION_CHOICES, default=512,
                        help="动作参考长边像素（默认: 512；可选 512 / 768 / 1376）")
    parser.add_argument("--clip-seconds", type=float, default=6.0,
                        help="单段窗口秒数 2.5–15（默认: 6.0；G4 96GB 推荐 9.5–10 以减少重叠损耗）")
    parser.add_argument("--seed", type=int, default=0, help="随机种子 0–4294967295（默认: 0）")


def _append_section_fields(fields: list[tuple[str, str]], args: argparse.Namespace) -> None:
    start, length = validate_section(args.start, args.length)
    if start > 0 or length > 0:
        fields.append(("start", f"{start:.2f}"))
        fields.append(("length", f"{length:.2f}"))


def _append_model_fields(fields: list[tuple[str, str]], args: argparse.Namespace) -> None:
    steps = validate_steps(args.steps)
    loras = resolve_loras(
        loras=args.loras,
        extra_loras=args.extra_loras,
        no_lora=args.no_lora,
        acceleration=args.acceleration,
    )
    fields.extend([
        ("steps", str(steps)),
        ("model", args.model),
        ("text_encoder", args.text_encoder),
        ("video_vae", args.video_vae),
        ("audio_vae", args.audio_vae),
        ("acceleration", args.acceleration),
    ])
    if args.describer:
        fields.append(("describer", args.describer))
    for name, strength in loras:
        fields.append(("lora", name))
        fields.append(("lora_strength", str(strength)))
        fields.append(("lora_speed", "true" if is_speed_lora(name) else "false"))


def build_submission(args: argparse.Namespace) -> tuple[str, list[tuple[str, str]], list[tuple[str, Path, str]]]:
    """Validate CLI arguments locally and build `(sse_kind, form_fields, form_files)` before network upload."""
    cmd = args.cmd
    video_path, video_mime = validate_media_file(args.video, kind="video")
    files: list[tuple[str, Path, str]] = [("video", video_path, video_mime)]
    fields: list[tuple[str, str]] = []

    if cmd == "swap":
        if not args.pictures or len(args.pictures) > 4:
            raise UsageError("请通过 -p/--picture 提供 1 到 4 张角色参考图")
        pic_entries = [validate_media_file(p, kind="image") for p in args.pictures]
        roles = list(args.roles or [])
        if len(roles) > len(pic_entries):
            raise UsageError(f"--role 数量（{len(roles)}）不能超过参考图数量（{len(pic_entries)}）")
        while len(roles) < len(pic_entries):
            roles.append("")

        if args.montage:
            if any(not r.strip() for r in roles):
                raise UsageError("--montage 模式要求为每张参考图指定非空 --role（例如 'dancer - white shirt'）")
            if args.max_quality:
                raise UsageError("--max-quality 仅适用于双人 advanced 模式，不能与 --montage 同时使用")
            mode, sse_kind = "montage", "montage"
        elif len(pic_entries) == 1 and not args.max_quality:
            mode, sse_kind = "simple", "simple"
        else:
            if args.max_quality and len(pic_entries) != 2:
                raise UsageError("--max-quality 需要恰好 2 张角色参考图")
            mode = "advanced"
            sse_kind = "advanced_maxq" if args.max_quality else "advanced"

        resolution = validate_resolution(args.resolution)
        clip_seconds = validate_clip_seconds(args.clip_seconds)
        seed = validate_seed(args.seed)

        fields.append(("mode", mode))
        _append_section_fields(fields, args)
        fields.extend([
            ("hat_fix", "true" if args.hat_fix else "false"),
            ("no_lip_sync", "true" if args.no_lip_sync else "false"),
            ("margin", args.margin),
            ("target", args.target),
            ("resolution", resolution),
            ("motion", str(args.motion)),
            ("seed", str(seed)),
            ("clip_seconds", str(clip_seconds)),
        ])
        if mode == "simple":
            files.append(("picture", pic_entries[0][0], pic_entries[0][1]))
            fields.append(("role", roles[0]))
        else:
            for (pic_path, pic_mime), role in zip(pic_entries, roles):
                files.append(("picture", pic_path, pic_mime))
                fields.append(("role", role))
            if mode == "advanced":
                fields.append(("max_quality", "true" if args.max_quality else "false"))
        _append_model_fields(fields, args)
        return sse_kind, fields, files

    if cmd == "object":
        target = (args.target or "").strip()
        if not target:
            raise UsageError("--target 不能为空")
        change_to = (args.change_to or "").strip()
        pic_entry = validate_media_file(args.picture, kind="image") if args.picture else None
        if args.edit_mode != "remove" and not change_to and not pic_entry:
            raise UsageError(f"编辑模式 {args.edit_mode!r} 需要提供 --change-to 文字描述或 -p/--picture 参考图（或两者同时提供）")
        if args.edit_mode == "add" and not args.placement:
            raise UsageError("--edit-mode add 需要通过 --placement 提供放置区域 JSON，例如 '[{\"frame\":0,\"x\":0.25,\"y\":0.2,\"width\":0.5,\"height\":0.6}]'")
        if args.placement:
            try:
                parsed_pl = json.loads(args.placement)
            except json.JSONDecodeError as exc:
                raise UsageError(f"--placement 不是合法的 JSON：{exc}") from None
            if not isinstance(parsed_pl, list) or not parsed_pl:
                raise UsageError("--placement 必须是非空关键帧数组")

        resolution = validate_resolution(args.resolution)
        clip_seconds = validate_clip_seconds(args.clip_seconds)
        seed = validate_seed(args.seed)

        fields.append(("mode", "object"))
        _append_section_fields(fields, args)
        fields.extend([
            ("edit_mode", args.edit_mode),
            ("target", target),
            ("change_to", change_to),
            ("margin", args.margin),
            ("biggest_only", "true" if args.biggest_only else "false"),
            ("keep_outline", "true" if args.keep_outline else "false"),
            ("resolution", resolution),
            ("motion", str(args.motion)),
            ("seed", str(seed)),
            ("clip_seconds", str(clip_seconds)),
        ])
        if pic_entry and args.edit_mode != "remove":
            files.append(("picture", pic_entry[0], pic_entry[1]))
        if args.edit_mode == "add":
            fields.append(("placement", args.placement))
            fields.append(("action", (args.action or "").strip()))
        _append_model_fields(fields, args)
        return "object", fields, files

    if cmd == "mask":
        fields.append(("mode", "mask"))
        _append_section_fields(fields, args)
        fields.extend([
            ("hat_fix", "true" if args.hat_fix else "false"),
            ("margin", args.margin),
            ("target", args.target),
        ])
        return "mask", fields, files

    if cmd == "face":
        pic_path, pic_mime = validate_media_file(args.picture, kind="image")
        files.append(("picture", pic_path, pic_mime))
        chunk_seconds = validate_clip_seconds(args.clip_seconds, flag="--clip-seconds")
        seed = validate_seed(args.seed)
        fields.append(("mode", "face"))
        _append_section_fields(fields, args)
        fields.extend([
            ("style", args.style),
            ("seed", str(seed)),
            ("clip_seconds", str(chunk_seconds)),
            ("motion", "512"),
        ])
        _append_model_fields(fields, args)
        return "face", fields, files

    if cmd == "color":
        if args.no_detail and args.no_tone:
            raise UsageError("不能同时指定 --no-detail 和 --no-tone（至少开启一项调色步骤）")
        fields.append(("mode", "color"))
        _append_section_fields(fields, args)
        if not args.no_detail:
            if not (-1.0 <= args.detail <= 1.0):
                raise UsageError("--detail 必须在 -1.0 到 1.0 之间")
            fields.append(("detail", str(args.detail)))
        if not args.no_tone:
            fields.append(("tone_mode", args.tone_mode))
            for key, (low, high) in TONE_RANGES.items():
                val = getattr(args, f"tone_{key}")
                if not (low <= val <= high):
                    raise UsageError(f"--tone-{key.replace('_', '-')} 必须在 {low} 到 {high} 之间")
                fields.append((f"tone_{key}", str(val)))
        return "color", fields, files

    raise ValueError(f"Unsupported submission command: {cmd}")


def check_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(prog="colabllm openjutsu check")
    parser.add_argument("--mode", choices=MODE_CHOICES, default="all",
                        help="Openjutsu workflow mode to verify via /api/setup (default: all)")
    parser.add_argument("--profile", choices=("full", "core"), default="full",
                        help="expected model profile when --mode=all (default: full)")
    _add_endpoint_flags(parser)
    parser.add_argument("--timeout", type=positive_seconds, default=60, help="HTTP timeout seconds (default: 60)")
    return parser


def custom_parser(
    parser: argparse.ArgumentParser | None = None,
    commands: argparse._SubParsersAction | None = None,
) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(
        prog="colabllm openjutsu",
        description="Openjutsu：命令行提交动作迁移、局部视频编辑、遮罩预览、人脸/色彩修复及查询下载任务",
    )
    commands = commands or parser.add_subparsers(dest="cmd", required=True)

    # 1. swap (simple / advanced / montage)
    swap = commands.add_parser("swap", help="角色动作迁移（1 张参考图走 simple，2–4 张走 advanced，--montage 走多人剪辑）")
    swap.add_argument("--video", required=True, help="本地源视频文件路径（MP4/MOV/MKV/WebM/AVI，<= 500 MB）")
    swap.add_argument("-p", "--picture", action="append", dest="pictures", required=True,
                      help="本地角色参考图路径（PNG/JPEG/WebP；可重复指定 1–4 张）")
    swap.add_argument("--role", action="append", dest="roles",
                      help="对应每张参考图替换的人物描述（如 'left dancer'；--montage 模式必填）")
    swap.add_argument("--montage", action="store_true", help="启用 Montage 多镜头人物自动追踪与匹配模式")
    swap.add_argument("--max-quality", action="store_true", help="双人模式下按人物分两遍独立渲染（最高画质）")
    swap.add_argument("--target", default="person", help="SAM 3.1 追踪目标类别（默认: person）")
    swap.add_argument("--margin", choices=("tight", "normal", "loose"), default="normal",
                      help="遮罩外扩边缘宽度（默认: normal）")
    swap.add_argument("--hat-fix", action="store_true", help="开启帽子/头饰区域遮罩保护")
    swap.add_argument("--no-lip-sync", action="store_true", help="关闭口型同步（人物不跟唱/不张嘴配音）")
    _add_section_flags(swap)
    _add_h3_run_flags(swap)
    _add_model_flags(swap)
    _add_wait_download_flags(swap)
    _add_endpoint_flags(swap)

    # 2. object (change / remove / background / add)
    obj = commands.add_parser("object", help="局部视频编辑（替换服饰道具 change / 移除物体 remove / 换背景 background / 添加物体 add）")
    obj.add_argument("--video", required=True, help="本地源视频文件路径")
    obj.add_argument("--edit-mode", choices=("change", "remove", "background", "add"), default="change",
                     help="局部编辑模式（默认: change）")
    obj.add_argument("--target", required=True,
                     help="追踪目标（change/remove 填要改删的物体如 'jacket'；background 填保留主体如 'person'；add 填位置如 'on the table'）")
    obj.add_argument("--change-to", default="", help="目标变成什么或添加什么（如 'black leather jacket'）")
    obj.add_argument("-p", "--picture", help="可选外观参考图路径（PNG/JPEG/WebP）")
    obj.add_argument("--margin", choices=("tight", "normal", "loose"), default="normal", help="遮罩边缘宽度（默认: normal）")
    obj.add_argument("--biggest-only", action="store_true", help="同类目标出现多个时仅编辑面积最大的一个")
    outline_grp = obj.add_mutually_exclusive_group()
    outline_grp.add_argument("--keep-outline", dest="keep_outline", action="store_true", default=True,
                             help="保留原物体轮廓动作引导（默认开启）")
    outline_grp.add_argument("--no-keep-outline", dest="keep_outline", action="store_false",
                             help="不保留原物体轮廓（适合版型差异较大的换装）")
    obj.add_argument("--placement", help="add 模式必填：归一化放置框关键帧 JSON 数组")
    obj.add_argument("--action", default="", help="add 模式可选：新增物体的动作描述")
    _add_section_flags(obj)
    _add_h3_run_flags(obj)
    _add_model_flags(obj)
    _add_wait_download_flags(obj)
    _add_endpoint_flags(obj)

    # 3. mask (fast SAM 3.1 mask preview)
    mask = commands.add_parser("mask", help="快速生成 SAM 3.1 追踪遮罩预览视频")
    mask.add_argument("--video", required=True, help="本地源视频文件路径")
    mask.add_argument("--target", default="person", help="追踪目标（默认: person）")
    mask.add_argument("--margin", choices=("tight", "normal", "loose"), default="normal", help="遮罩边缘宽度（默认: normal）")
    mask.add_argument("--hat-fix", action="store_true", help="开启帽子/头饰遮罩保护")
    _add_section_flags(mask)
    _add_wait_download_flags(mask)
    _add_endpoint_flags(mask)

    # 4. face (Fix faces post-processing)
    face = commands.add_parser("face", help="成片人脸精修重绘后处理（需 full 模型档位）")
    face.add_argument("--video", required=True, help="待修复人脸的视频路径")
    face.add_argument("-p", "--picture", required=True, help="清晰正脸参考图路径（PNG/JPEG/WebP）")
    face.add_argument("--style", choices=("photo", "mouth"), default="photo",
                      help="人脸融合模式：photo 贴合照片五官（默认），mouth 严格跟随原视频嘴型")
    face.add_argument("--clip-seconds", type=float, default=6.5, help="分块精修时长秒数 2.5–15（默认: 6.5）")
    face.add_argument("--seed", type=int, default=0, help="随机种子（默认: 0）")
    _add_section_flags(face)
    _add_model_flags(face)
    _add_wait_download_flags(face)
    _add_endpoint_flags(face)

    # 5. color (Fix colours post-processing)
    color = commands.add_parser("color", help="成片细节柔化与 Orion4D DCTL 调色后处理")
    color.add_argument("--video", required=True, help="待调色视频路径")
    color.add_argument("--detail", type=float, default=-0.2, help="细节对比度 -1.0–1.0（默认: -0.2 柔化高频噪点）")
    color.add_argument("--no-detail", action="store_true", help="关闭细节对比度调节")
    color.add_argument("--tone-mode", choices=TONE_MODES, default=str(DEFAULT_TONE["mode"]),
                       help="DCTL 色调映射曲线（默认: Reinhard）")
    color.add_argument("--no-tone", action="store_true", help="关闭 DCTL 色调映射")
    for key, val in DEFAULT_TONE.items():
        if key == "mode":
            continue
        color.add_argument(f"--tone-{key.replace('_', '-')}", dest=f"tone_{key}", type=float, default=float(val),
                           help=f"色调参数 {key}（默认: {val}）")
    _add_section_flags(color)
    _add_wait_download_flags(color)
    _add_endpoint_flags(color)

    # 6. job / download / cancel
    job = commands.add_parser("job", help="查询任务状态；加 --wait 实时跟踪进度，加 -o 下载结果")
    job.add_argument("job_id", help="任务 ID")
    job.add_argument("--kind", default="simple", help="SSE 节点标题解析工作流类型（默认: simple）")
    _add_wait_download_flags(job)
    _add_endpoint_flags(job)

    dl = commands.add_parser("download", help="下载已完成任务的输出文件（加 --wait 可先等待完成）")
    dl.add_argument("job_id", help="任务 ID")
    dl.add_argument("--kind", default="simple", help="等待时的工作流类型（默认: simple）")
    _add_wait_download_flags(dl)
    _add_endpoint_flags(dl)

    cancel = commands.add_parser("cancel", help="取消排队中或运行中的任务")
    cancel.add_argument("job_id", help="任务 ID")
    _add_endpoint_flags(cancel)

    check_parser(commands.add_parser("check", help="verify /api/models and /api/setup readiness"))
    return parser


def _cmd_check(raw_args: list[str] | argparse.Namespace) -> None:
    """Verify Openjutsu model inventory (`/api/models`) and ComfyUI custom-node/model readiness (`/api/setup`)."""
    args = raw_args if isinstance(raw_args, argparse.Namespace) else check_parser().parse_args(raw_args)
    deadline = Deadline(args.timeout)
    client = _resolve_client(args)
    models = client.request_json("/api/models", timeout=deadline.remaining(30))
    if args.mode == "all":
        modes = [m for m in SETUP_PAYLOADS if args.profile == "full" or m != "face"]
    else:
        modes = [args.mode]
    mode_results: dict[str, dict[str, object]] = {}
    all_ok = not bool(models.get("error"))
    for mode in modes:
        res = client.request_json(
            "/api/setup",
            method="POST",
            body=SETUP_PAYLOADS[mode],
            timeout=deadline.remaining(30),
        )
        mode_results[mode] = res
        if not res.get("ok"):
            all_ok = False
    report = {
        "ok": all_ok,
        "url": client.url,
        "models": models,
        "modes": mode_results,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not all_ok:
        raise CliError(f"[{SERVICE.name}] setup check reported missing models or custom node packs")


def _run_business_command(raw_args: list[str] | argparse.Namespace, cmd_name: str) -> None:
    if isinstance(raw_args, argparse.Namespace):
        args = raw_args
    else:
        args = custom_parser().parse_args([cmd_name, *raw_args])

    if args.cmd == "cancel":
        client = _resolve_client(args)
        print(json.dumps(client.cancel(args.job_id), indent=2, ensure_ascii=False))
        return

    if args.cmd in ("swap", "object", "mask", "face", "color"):
        sse_kind, fields, files = build_submission(args)
        client = _resolve_client(args)
        diagnostic(f"[{SERVICE.name}] 正在上传素材并提交 {args.cmd} 任务...")
        submitted = client.submit_form(fields, files)
        job_id = str(submitted["id"])
        diagnostic(f"[{SERVICE.name}] 已提交任务：{job_id}")
        if not args.wait and not args.output:
            print(json.dumps(submitted, indent=2, ensure_ascii=False))
            return
    else:
        client = _resolve_client(args)
        job_id = args.job_id
        sse_kind = getattr(args, "kind", "simple") or "simple"

    should_wait = bool(args.wait or (args.output and args.cmd != "download"))
    state = (
        client.wait(
            job_id,
            kind=sse_kind,
            poll=args.poll,
            timeout=args.timeout,
            use_events=not args.no_events,
        )
        if should_wait
        else client.job(job_id)
    )
    if args.output or args.cmd == "download":
        default_ext = ".mp4" if state.get("video") or not state.get("data") else ".json"
        dest = args.output or f"{job_id}{default_ext}"
        saved = client.download(state, dest)
        state = {**state, "local_output": saved}
        diagnostic(f"[{SERVICE.name}] 已保存到本地：{saved}")
    print(json.dumps(state, indent=2, ensure_ascii=False))


def _make_handler(cmd_name: str):
    return lambda raw_args: _run_business_command(raw_args, cmd_name)


BUSINESS_COMMANDS = ("swap", "object", "mask", "face", "color", "job", "download", "cancel")


def configure_cli(parser, commands, up) -> None:
    up.add_argument(
        "--profile",
        choices=("full", "core"),
        default="full",
        help="model bundle to stage onto local SSD: full (~80.7 GB, all modes) or core (~50.8 GB, H3 swap/edit/montage/color)",
    )
    up.add_argument(
        "--chains-storage",
        choices=("ssd", "drive"),
        default="ssd",
        help="where to store H3 Context-Loop per-clip checkpoints (~450 MB/clip): ssd (default, fast bind-mount) or drive",
    )
    up.add_argument(
        "--vram-mode",
        choices=("auto", "highvram", "dynamic", "gpu-only"),
        default="auto",
        help="ComfyUI VRAM policy: auto enables --highvram on >=76 GiB GPUs such as G4 (default: auto)",
    )
    custom_parser(parser, commands)


_BASE_ENV = dict(SERVICE.env)


def prepare_up(args) -> None:
    SERVICE.env.clear()
    SERVICE.env.update(_BASE_ENV)
    SERVICE.env["CT_OPENJUTSU_PROFILE"] = getattr(args, "profile", None) or "full"
    SERVICE.env["CT_OPENJUTSU_CHAINS_STORAGE"] = getattr(args, "chains_storage", None) or "ssd"
    SERVICE.env["CT_COMFY_VRAM_MODE"] = getattr(args, "vram_mode", None) or "auto"


HANDLERS = {
    "check": _cmd_check,
    **{name: _make_handler(name) for name in BUSINESS_COMMANDS},
}


if __name__ == "__main__":
    from cli_support import run_cli
    run_cli(lambda: main(SERVICE, configure=configure_cli, prepare_up=prepare_up, handlers=HANDLERS))
