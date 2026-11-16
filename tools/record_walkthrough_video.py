#!/usr/bin/env python3
"""Generate docs/assets/colabllm-walkthrough.mp4 from live CLI output and live colabllm web."""
from __future__ import annotations

import argparse
from array import array
import base64
from contextlib import contextmanager
import http.server
import io
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import wave
import zlib

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
LIB_DIR = SRC_DIR / "colabllm" / "lib"

for _p in (str(SRC_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from colabllm.web_ui import server as web_server  # noqa: E402
from processes import process_identity  # noqa: E402


# ---------------------------------------------------------------------------
# Pure-stdlib media generators for the sandbox upstream server
# ---------------------------------------------------------------------------

def _make_demo_png_bytes(width: int = 768, height: int = 512) -> bytes:
    """Render a procedural sunlit architectural glass atrium PNG via stdlib zlib."""
    rows = bytearray()
    cx = width * 0.50
    horizon = height * 0.56
    for y in range(height):
        rows.append(0)  # filter type 0
        ny = y / height
        sky_r = int(14 + 34 * (1.0 - ny) + 70 * max(0.0, 1.0 - abs(ny - 0.52) * 3.0))
        sky_g = int(28 + 68 * (1.0 - ny) + 88 * max(0.0, 1.0 - abs(ny - 0.52) * 2.4))
        sky_b = int(58 + 95 * (1.0 - ny) + 76 * max(0.0, 1.0 - abs(ny - 0.52) * 2.5))
        for x in range(width):
            nx = (x - cx) / (width * 0.5)
            dx = abs(nx)
            r, g, b = sky_r, sky_g, sky_b

            # Radial sunlit dome glow
            radial = max(0.0, 1.0 - math.hypot(nx * 1.15, (y - horizon * 0.78) / (height * 0.55)))
            glow = radial * radial * radial
            r += int(155 * glow)
            g += int(130 * glow)
            b += int(75 * glow)

            # Futuristic vaulted glass ribs converging toward upper dome
            arch_y = 0.08 + 0.44 * (nx * nx)
            if ny >= arch_y and y < horizon:
                rib = abs((nx * 6.5) - round(nx * 6.5))
                ring = abs(((ny - arch_y) * 9.0) - round((ny - arch_y) * 9.0))
                if rib < 0.055 or ring < 0.075:
                    r = int(r * 0.52 + 26)
                    g = int(g * 0.55 + 48)
                    b = int(b * 0.62 + 78)

            # Water reflection plane & emerald botanical silhouettes below horizon
            if y >= horizon:
                t = (y - horizon) / max(1.0, height - horizon)
                ripple = math.sin(y * 0.35 + x * 0.03) * 0.5 + 0.5
                r = int(18 + 38 * (1.0 - t) + 85 * glow * (0.65 + 0.35 * ripple))
                g = int(42 + 62 * (1.0 - t) + 92 * glow * (0.7 + 0.3 * ripple))
                b = int(68 + 56 * (1.0 - t) + 55 * (1.0 - t))
                if 0.16 < dx < 0.92 and abs(math.sin(x * 0.045) * 0.18 + 0.14) > t * 0.65:
                    # Stylized atrium planter terraces
                    if y < horizon + 48 * (0.4 + 0.6 * math.cos(nx * 3.0)):
                        r, g, b = 22, 92, 86

            # Bright center promenade highlight
            if y >= horizon and dx < 0.14 * (0.35 + 1.8 * ((y - horizon) / (height - horizon))):
                r = min(255, r + 42)
                g = min(255, g + 48)
                b = min(255, b + 44)

            rows.extend((max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(rows), level=6)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_demo_wav_bytes(sample_rate: int = 24000, duration: float = 2.4) -> bytes:
    """Synthesize a clean polyphonic chime WAV for TTS studio demos."""
    total = int(sample_rate * duration)
    samples = array("h")
    freqs = (261.63, 329.63, 392.00, 523.25)
    for i in range(total):
        t = i / sample_rate
        env = min(1.0, t / 0.06) * math.exp(-t * 1.1)
        val = sum(math.sin(2.0 * math.pi * f * t) for f in freqs) / len(freqs)
        samples.append(max(-32767, min(32767, int(val * env * 14000))))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


_DEMO_PNG_B64 = base64.b64encode(_make_demo_png_bytes()).decode("ascii")
_DEMO_WAV_BYTES = _make_demo_wav_bytes()


# ---------------------------------------------------------------------------
# Mock upstream runtime server (OpenAI /v1/* + multimodal + ComfyUI stats)
# ---------------------------------------------------------------------------

def _make_mock_handler():
    class MockUpstreamHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, code: int, payload: object, extra_headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def _send_sse_chunks(self, chunks: list[dict], delay: float = 0.04) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for ch in chunks:
                line = f"data: {json.dumps(ch, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(line)
                self.wfile.flush()
                if delay > 0:
                    time.sleep(delay)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/v1/models":
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {"id": "qwen3.8-flash-next", "object": "model"},
                            {"id": "deepseek-v4.1-flash-coder", "object": "model"},
                        ],
                    },
                )
            elif path in ("/health", "/api/ct/info"):
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "gpu": "NVIDIA RTX PRO 6000 Blackwell (G4 96 GiB)",
                        "max_context_tokens": 1048576,
                        "proxy_auth": "verified",
                    },
                )
            elif path == "/v1/audio/voices":
                self._send_json(
                    200,
                    {
                        "voices": [
                            {"name": "podcast_female_zh", "sample_rate": 24000},
                            {"name": "calm_narrator", "sample_rate": 24000},
                            {"name": "story_hero", "sample_rate": 24000},
                        ]
                    },
                )
            elif path == "/system_stats":
                self._send_json(
                    200,
                    {
                        "system": {"os": "posix", "comfyui_version": "0.3.26", "python_version": "3.12"},
                        "devices": [
                            {
                                "name": "cuda:0 NVIDIA L4 : cudaMallocAsync",
                                "type": "cuda",
                                "vram_total": 24152899584,
                                "vram_free": 19421772800,
                            }
                        ],
                    },
                )
            else:
                self._send_json(200, {"ok": True, "path": path})

        def do_POST(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            data = self._read_json_body()

            if path == "/v1/images/generations":
                self._send_json(
                    200,
                    {"created": int(time.time()), "data": [{"b64_json": _DEMO_PNG_B64}]},
                    extra_headers={"X-Saved-Drive-Path": "/content/drive/MyDrive/colabllm/atrium-4k.png"},
                )
                return

            if path == "/v1/audio/speech":
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("X-Audio-Sample-Rate", "24000")
                self.send_header("X-Saved-Drive-Path", "/content/drive/MyDrive/colabllm/speech-demo.wav")
                self.send_header("Content-Length", str(len(_DEMO_WAV_BYTES)))
                self.end_headers()
                self.wfile.write(_DEMO_WAV_BYTES)
                return

            if path == "/v1/chat/completions":
                messages = data.get("messages") or []
                has_audio = any(
                    isinstance(m.get("content"), list)
                    and any(isinstance(p, dict) and p.get("type") == "audio_url" for p in m["content"])
                    for m in messages
                )
                if has_audio:
                    self._send_json(
                        200,
                        {
                            "id": "chatcmpl-asr-demo",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "欢迎使用 ColabLLM：本机零 GPU 依赖，一键编排远端大模型与多模态工作流。",
                                    },
                                }
                            ],
                            "ctc_timestamps": [
                                {"word": "欢迎使用", "start": 0.15, "end": 0.72},
                                {"word": "ColabLLM", "start": 0.78, "end": 1.45},
                                {"word": "本机零GPU依赖", "start": 1.55, "end": 2.60},
                                {"word": "一键编排多模态工作流", "start": 2.70, "end": 4.20},
                            ],
                        },
                    )
                    return

                has_tool_reply = any(m.get("role") == "tool" for m in messages)
                tools = data.get("tools") or []
                last_user = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user = m.get("content") if isinstance(m.get("content"), str) else ""
                        break

                if tools and not has_tool_reply and ("概览卡片" in last_user or "检查当前在线" in last_user or "对比" in last_user):
                    reasoning_steps = [
                        "正在通过前端 tool-calling 收集当前 Colab 舰队在线状态、",
                        "GPU 显存规格与 OpenAI 兼容端点能力...",
                        "准备调用 render_ui_widget 直接在对话框中渲染结构化指标卡与对比表。",
                    ]
                    chunks = [
                        {"choices": [{"index": 0, "delta": {"reasoning_content": txt}}]}
                        for txt in reasoning_steps
                    ]
                    kpi_args = json.dumps(
                        {
                            "widget_type": "kpi_grid",
                            "title": "Colab GPU 舰队实时概览",
                            "items": [
                                {"label": "在线就绪实例", "value": "7 / 8 Ready"},
                                {"label": "主力推理上下文", "value": "1M Tokens (G4)"},
                                {"label": "同源代理认证", "value": "Bearer 自动注入"},
                            ],
                        },
                        ensure_ascii=False,
                    )
                    table_args = json.dumps(
                        {
                            "widget_type": "table",
                            "title": "多模态运行时能力矩阵",
                            "columns": ["运行时", "默认 GPU", "核心特长", "接口协议"],
                            "rows": [
                                ["qwen-flash", "G4 96G", "1M 上下文 · 视觉思考", "OpenAI /v1"],
                                ["minimax-h3-image", "G4 96G", "6步原生4K · 16图参考", "OpenAI + /api/ct"],
                                ["qwen3-tts", "L4 24G", "音色克隆 · 自然语言捏声", "/v1/audio/*"],
                                ["nemotron-transcribe", "G4 96G", "SALM 理解 · CTC 时间戳", "/v1/chat/completions"],
                            ],
                        },
                        ensure_ascii=False,
                    )
                    chunks.append(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {"index": 0, "id": "call_kpi_1", "type": "function", "function": {"name": "render_ui_widget", "arguments": kpi_args}},
                                            {"index": 1, "id": "call_tbl_2", "type": "function", "function": {"name": "render_ui_widget", "arguments": table_args}},
                                        ]
                                    },
                                }
                            ]
                        }
                    )
                    self._send_sse_chunks(chunks, delay=0.05)
                    return

                if has_tool_reply:
                    answer_parts = [
                        "以上**实时指标卡**与**能力矩阵**已由前端 Generative UI 工具直接渲染。\n\n",
                        "- 本机后端通过 `/api/proxy/<service>/<instance>/...` 自动注入凭据\n",
                        "- 浏览器全程零密钥暴露，可直接切换至图像、语音、转写或原生控制台继续调试。",
                    ]
                    self._send_sse_chunks(
                        [{"choices": [{"index": 0, "delta": {"content": p}}]} for p in answer_parts],
                        delay=0.05,
                    )
                    return

                # Quick drawer / general chat response
                chunks = [
                    {"choices": [{"index": 0, "delta": {"reasoning_content": "已通过本机同源代理安全连接目标运行时。"}}]},
                    {"choices": [{"index": 0, "delta": {"content": "所有请求均经由本地 CSRF + Bearer 同源转发，当前舰队运行稳定，可随时在主演练场展开完整多模态工具链。"}}]},
                ]
                self._send_sse_chunks(chunks, delay=0.04)
                return

            self._send_json(200, {"ok": True})

    return MockUpstreamHandler


# ---------------------------------------------------------------------------
# Sandbox COLABLLM_HOME setup & real CLI output capture
# ---------------------------------------------------------------------------

def _populate_sandbox_home(home_dir: Path, mock_port: int) -> None:
    """Create isolated demo instances using example.com domains and current PID identity."""
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "tunnel-config.json").write_text(
        json.dumps({"base_domain": "ai.example.com", "transport": "cloudflare"}, indent=2),
        encoding="utf-8",
    )

    pid = os.getpid()
    ident = process_identity(pid)
    now = int(time.time())
    mock_url = f"http://127.0.0.1:{mock_port}"

    ready_services = [
        ("qwen-flash", "G4", "builtin", "qwen.ai.example.com"),
        ("deepseek-coder", "G4", "builtin", "deepseek.ai.example.com"),
        ("minimax-h3-image", "G4", "builtin", "h3-image.ai.example.com"),
        ("indextts", "L4", "builtin", "indextts.ai.example.com"),
        ("qwen3-tts", "L4", "builtin", "qwen3-tts.ai.example.com"),
        ("nemotron-transcribe", "G4", "builtin", "transcribe.ai.example.com"),
        ("ollama", "G4", "caddy", "ollama.ai.example.com"),
        ("comfyui", "L4", "caddy", "comfyui.ai.example.com"),
    ]

    for idx, (svc, gpu, auth, host) in enumerate(ready_services):
        sdir = home_dir / "services" / svc
        sdir.mkdir(parents=True, exist_ok=True)
        state = {
            "service": svc,
            "instance": "",
            "runner_pid": pid,
            "runner_identity": ident,
            "ready": True,
            "url": mock_url,
            "hostname": host,
            "transport": "cloudflare",
            "account": "gpu-pro-1",
            "drive_account": "main-drive",
            "session_gpu": gpu,
            "mount_drive": True,
            "auto_restart": True,
            "restart_count": 0,
            "started": now - 900 + idx * 35,
            "api_token": f"sk-demo-{svc}-masked-token",
            "auth_mode": auth,
        }
        if auth == "caddy":
            state["auth_user"] = "colab"
            state["auth_pass"] = f"pass-{svc}-demo"
        (sdir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        (sdir / "run.log").write_text(
            "\n".join(
                [
                    f"[colabsvc] {svc}: provisioning Colab runtime (gpu={gpu}, account=gpu-pro-1)",
                    "[colabsvc] drive mounted cleanly via colab-ext (drive_account=main-drive)",
                    f"[tunnel] cloudflare tunnel ingress ready -> https://{host}",
                    f"[health] probing local runtime endpoint on {gpu}...",
                    f"CT_PUBLIC_URL=https://{host}",
                    f"CT_READY url=https://{host} port=8000",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    # One STARTING service + one cleanly STOPPED service to showcase fleet states
    ocr_dir = home_dir / "services" / "unlimited-ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    (ocr_dir / "state.json").write_text(
        json.dumps(
            {
                "service": "unlimited-ocr",
                "runner_pid": pid,
                "runner_identity": ident,
                "ready": False,
                "hostname": "ocr.ai.example.com",
                "transport": "cloudflare",
                "account": "gpu-pro-2",
                "session_gpu": "A100",
                "mount_drive": True,
                "started": now - 45,
                "api_token": "sk-demo-ocr-token",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (ocr_dir / "run.log").write_text(
        "[colabsvc] unlimited-ocr: pulling PaddleOCR-VL v2 weights into fast NVMe...\n",
        encoding="utf-8",
    )

    r2_dir = home_dir / "services" / "r2-migrate"
    r2_dir.mkdir(parents=True, exist_ok=True)
    (r2_dir / "state.json").write_text(
        json.dumps(
            {
                "service": "r2-migrate",
                "ready": False,
                "runner_exit": 0,
                "hostname": "r2.ai.example.com",
                "transport": "cloudflare",
                "account": "main-drive",
                "started": now - 7200,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (r2_dir / "run.log").write_text("[colabsvc] r2-migrate cleanly stopped.\n", encoding="utf-8")


def _run_real_cli_samples(home_dir: Path, tmp_dir: Path) -> dict[str, str]:
    """Capture real CLI command outputs against the isolated sandbox COLABLLM_HOME."""
    env = {**os.environ, "COLABLLM_HOME": str(home_dir), "NO_COLOR": "1"}
    cli = str(ROOT / "colabllm")

    def capture(*args: str) -> str:
        res = subprocess.run(
            [sys.executable, cli, *args],
            env=env,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=25,
            check=True,
        )
        return (res.stdout + res.stderr).strip()

    skills_out = tmp_dir / "exported-skills"
    skills_out.mkdir(parents=True, exist_ok=True)

    return {
        "services": capture("services"),
        "dry_run": capture("qwen-flash", "up", "-i", "demo", "--quick-tunnel", "--auto-restart", "--dry-run"),
        "skill": capture("skill", "--output", str(skills_out)),
    }


# ---------------------------------------------------------------------------
# Tool discovery (without any hardcoded user-specific path literals)
# ---------------------------------------------------------------------------

def _find_playwright_node_modules() -> str:
    env_path = os.environ.get("PLAYWRIGHT_NODE_PATH", "").strip()
    if env_path and Path(env_path, "playwright", "package.json").is_file():
        return env_path

    candidates: list[Path] = []
    pw_bin = shutil.which("playwright")
    if pw_bin:
        resolved = Path(pw_bin).resolve()
        for parent in (resolved.parent, resolved.parent.parent, resolved.parent.parent.parent):
            candidates.append(parent / "node_modules")
            candidates.append(parent / "lib" / "node_modules")

    # Dynamically check tool manager locations under Path.home()
    mise_pw = Path.home() / ".local" / "share" / "mise" / "installs" / "playwright"
    if mise_pw.is_dir():
        for child in (mise_pw / "latest", *mise_pw.iterdir()):
            candidates.append(child / "node_modules")

    for cand in candidates:
        if (cand / "playwright" / "package.json").is_file():
            return str(cand)
    raise RuntimeError("Could not locate Node 'playwright' package; install playwright or set PLAYWRIGHT_NODE_PATH.")


def _find_chromium_executable() -> str:
    for name in ("chromium", "google-chrome-stable", "google-chrome", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("Could not locate Chromium/Chrome binary in PATH.")


# ---------------------------------------------------------------------------
# Stage HTTP server (Intro / Terminal / Outro cards)
# ---------------------------------------------------------------------------

def _build_stage_html(cli_samples: dict[str, str], web_url: str, web_port: int) -> dict[str, str]:
    def esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    base_css = """
      * { box-sizing: border-box; }
      html, body {
        margin: 0; padding: 0; width: 100%; height: 100%;
        background: radial-gradient(circle at 20% 20%, #1e293b 0%, #0b1120 62%, #050814 100%);
        color: #f1f5f9;
        font-family: "Noto Sans CJK SC", "Inter", system-ui, sans-serif;
        overflow: hidden;
      }
      code, pre {
        font-family: "Maple Mono NF CN", ui-monospace, SFMono-Regular, Menlo, monospace;
      }
    """

    intro_html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>ColabLLM Intro</title>
    <style>{base_css}
      .wrap {{
        height: 100%; padding: 72px 96px 128px; display: grid; gap: 36px; align-content: center;
      }}
      .eyebrow {{
        display: inline-flex; align-items: center; gap: 10px; justify-self: start;
        padding: 8px 18px; border-radius: 999px; font-size: 20px; font-weight: 600;
        background: rgba(56, 189, 248, 0.14); border: 1px solid rgba(56, 189, 248, 0.45); color: #7dd3fc;
      }}
      h1 {{
        margin: 0; font-size: 64px; line-height: 1.15; letter-spacing: -0.02em;
        background: linear-gradient(92deg, #f8fafc 0%, #7dd3fc 52%, #a5b4fc 100%);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      }}
      .lead {{ margin: 0; font-size: 27px; color: #cbd5e1; max-width: 1180px; line-height: 1.5; }}
      .arch-grid {{
        display: grid; grid-template-columns: 1fr 90px 1fr 90px 1fr; align-items: stretch; gap: 12px;
        margin-top: 12px;
      }}
      .arch-card {{
        background: rgba(15, 23, 42, 0.85); border: 1px solid rgba(148, 163, 184, 0.25);
        border-radius: 20px; padding: 28px; display: grid; gap: 12px; align-content: start;
        transition: border-color .35s ease, transform .35s ease;
      }}
      .arch-card.active {{
        border-color: #38bdf8; box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.28), 0 22px 50px rgba(2, 132, 199, 0.24);
        transform: translateY(-4px);
      }}
      .arch-tag {{ font-size: 16px; font-weight: 700; color: #38bdf8; text-transform: uppercase; letter-spacing: 0.06em; }}
      .arch-title {{ font-size: 28px; font-weight: 700; color: #f8fafc; }}
      .arch-list {{ margin: 0; padding-left: 20px; font-size: 19px; color: #cbd5e1; line-height: 1.7; }}
      .arrow {{ display: grid; place-items: center; font-size: 38px; color: #38bdf8; font-weight: 700; }}
      .pills {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 8px; }}
      .pill {{
        padding: 10px 18px; border-radius: 12px; background: rgba(30, 41, 59, 0.9);
        border: 1px solid rgba(148, 163, 184, 0.28); font-size: 19px; color: #e2e8f0;
      }}
    </style></head><body>
      <div class="wrap">
        <div class="eyebrow">Python 3.12+ 零依赖控制平面 · 19 个大模型与多模态运行时</div>
        <h1>ColabLLM：把 Google Colab GPU<br>变为安全可控的云端 AI 服务集群</h1>
        <p class="lead">本机无需 GPU、CUDA 或 PyTorch。通过统一 CLI 与全新 Copilot Studio Web 控制台，一键在远端 G4 96GB / A100 / L4 上拉起 OpenAI 兼容服务与多模态工作流。</p>
        <div class="arch-grid">
          <div class="arch-card active" data-step="0">
            <div class="arch-tag">01 · 本机控制平面</div>
            <div class="arch-title">统一 CLI + 同源 Web</div>
            <ul class="arch-list">
              <li>状态与凭据隔离写入 0600 私有目录</li>
              <li><code>colabllm web</code> 同源代理免泄露密钥</li>
              <li>可导出独立 Agent Skill 供智能体调用</li>
            </ul>
          </div>
          <div class="arrow">➜</div>
          <div class="arch-card" data-step="1">
            <div class="arch-tag">02 · 隧道与数据桥梁</div>
            <div class="arch-title">Cloudflare / FRP + Drive</div>
            <ul class="arch-list">
              <li><code>colab-ext</code> 免浏览器挂载与多账户隔离</li>
              <li>Cloudflare / FRP 自动绑定 HTTPS 固定域名</li>
              <li><code>--auto-restart</code> 守护掉线秒级重建</li>
            </ul>
          </div>
          <div class="arrow">➜</div>
          <div class="arch-card" data-step="2">
            <div class="arch-tag">03 · 远端 GPU 运行时</div>
            <div class="arch-title">1 个服务 = 1 Notebook + 脚本</div>
            <ul class="arch-list">
              <li>1M 上下文推理 (Qwen3.8 / DeepSeek-V4.1)</li>
              <li>4K 生图 (MiniMax-H3 / FLUX / Qwen-Image)</li>
              <li>TTS / 音色克隆 / SALM 转写 / ComfyUI / OCR</li>
            </ul>
          </div>
        </div>
        <div class="pills">
          <div class="pill">📦 <code>uv tool install colabllm</code> 即装即用</div>
          <div class="pill">🔐 Bearer + Basic + CSRF + Trusted Types 纵深防御</div>
          <div class="pill">⚡ OpenAI /v1 兼容接口开箱对接任意客户端</div>
        </div>
      </div>
    </body></html>"""

    cli_services_lines = "\n".join(cli_samples["services"].splitlines()[:22])
    cli_dry_lines = "\n".join(cli_samples["dry_run"].splitlines()[:14])
    cli_skill_lines = "\n".join(cli_samples["skill"].splitlines()[:8])

    terminal_html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>ColabLLM CLI</title>
    <style>{base_css}
      .stage {{ height: 100%; padding: 54px 86px 124px; display: grid; grid-template-rows: auto 1fr; gap: 22px; }}
      .term-head {{ display: flex; justify-content: space-between; align-items: center; }}
      .term-head h2 {{ margin: 0; font-size: 32px; color: #f8fafc; }}
      .term-head span {{ font-size: 19px; color: #94a3b8; }}
      .terminal {{
        background: #060a14; border: 1px solid rgba(148, 163, 184, 0.3); border-radius: 18px;
        box-shadow: 0 30px 70px rgba(0, 0, 0, 0.55); display: flex; flex-direction: column; overflow: hidden;
      }}
      .term-bar {{
        display: flex; align-items: center; gap: 10px; padding: 14px 20px;
        background: #0f172a; border-bottom: 1px solid rgba(148, 163, 184, 0.18);
      }}
      .dot {{ width: 13px; height: 13px; border-radius: 50%; }}
      .dot.r {{ background: #ef4444; }} .dot.y {{ background: #f59e0b; }} .dot.g {{ background: #22c55e; }}
      .term-title {{ margin-left: 10px; font-size: 16px; color: #94a3b8; font-family: "Maple Mono NF CN", monospace; }}
      pre#term-out {{
        margin: 0; padding: 24px 28px; flex: 1; font-size: 20px; line-height: 1.5;
        color: #e2e8f0; white-space: pre-wrap; overflow: hidden;
      }}
      .cmd {{ color: #38bdf8; font-weight: 700; }}
      .ok {{ color: #4ade80; }}
      a#term-web-link {{
        display: inline-block; margin-top: 8px; padding: 8px 16px; border-radius: 10px;
        background: rgba(56, 189, 248, 0.18); border: 1px solid #38bdf8; color: #7dd3fc;
        text-decoration: none; font-weight: 700; font-size: 21px;
      }}
    </style></head><body>
      <div class="stage">
        <div class="term-head">
          <h2>统一命令入口：全部运行时共享一致操作语义</h2>
          <span>实际本机命令输出录制（隔离沙盒环境）</span>
        </div>
        <div class="terminal">
          <div class="term-bar">
            <span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
            <span class="term-title">bash · COLABLLM_HOME=/sandbox/.colabllm</span>
          </div>
          <pre id="term-out"></pre>
        </div>
      </div>
      <script id="term-data" type="application/json">{json.dumps({
          "services": cli_services_lines,
          "dry_run": cli_dry_lines,
          "skill": cli_skill_lines,
          "web_url": f"http://127.0.0.1:{web_port}/?token=...",
          "real_auth_url": web_url,
      })}</script>
    </body></html>"""

    outro_html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>ColabLLM Outro</title>
    <style>{base_css}
      .wrap {{
        height: 100%; padding: 80px 100px 128px; display: grid; place-items: center; text-align: center;
      }}
      .box {{ display: grid; gap: 24px; justify-items: center; max-width: 1120px; }}
      h1 {{
        margin: 0; font-size: 56px;
        background: linear-gradient(90deg, #f8fafc, #7dd3fc, #c4b5fd);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      }}
      p {{ margin: 0; font-size: 25px; color: #cbd5e1; }}
      .cmds {{
        display: grid; gap: 14px; text-align: left; width: 780px; padding: 26px 32px;
        border-radius: 18px; background: rgba(15, 23, 42, 0.88); border: 1px solid rgba(56, 189, 248, 0.36);
        font-size: 22px; color: #7dd3fc;
      }}
      .badge-row {{ display: flex; gap: 14px; flex-wrap: wrap; justify-content: center; }}
      .b {{ padding: 8px 16px; border-radius: 999px; background: rgba(30, 41, 59, 0.88); border: 1px solid rgba(148, 163, 184, 0.3); font-size: 18px; }}
    </style></head><body>
      <div class="wrap"><div class="box">
        <h1>ColabLLM · 立即开始云端模型编排</h1>
        <p>一套轻量标准库工具链 · 19 个开箱即用运行时 · 全能 Copilot Studio Web 演练场</p>
        <div class="cmds">
          <code>$ uv tool install colabllm</code>
          <code>$ colabllm doctor</code>
          <code>$ colabllm qwen-flash up --domain --auto-restart</code>
          <code>$ colabllm web</code>
        </div>
        <div class="badge-row">
          <span class="b">MIT 开源许可证</span>
          <span class="b">docs/&lt;service&gt;.md 完整指南</span>
          <span class="b">Copilot Studio + 同源安全代理</span>
        </div>
      </div></div>
    </body></html>"""

    return {"/intro": intro_html, "/terminal": terminal_html, "/outro": outro_html}


def _start_stage_server(pages: dict[str, str]) -> tuple[http.server.ThreadingHTTPServer, int]:
    class StageHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            html = pages.get(path, pages["/intro"]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StageHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------------------
# Node.js / Playwright choreography script generator
# ---------------------------------------------------------------------------

_CHOREOGRAPHER_CJS = r"""
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const { chromium } = require("playwright");

(async () => {
const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const FPS = cfg.fps;
const WIDTH = 1920;
const HEIGHT = 1080;

const ff = spawn(
  cfg.ffmpegPath,
  [
    "-y",
    "-f", "image2pipe",
    "-vcodec", "mjpeg",
    "-framerate", String(FPS),
    "-i", "-",
    "-an",
    "-c:v", "libx264",
    "-preset", "fast",
    "-crf", "18",
    "-pix_fmt", "yuv420p",
    cfg.rawVideoPath,
  ],
  { stdio: ["pipe", "inherit", "inherit"] }
);

const ffDone = new Promise((resolve, reject) => {
  ff.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg exited ${code}`))));
});

const browser = await chromium.launch({
  executablePath: cfg.chromiumPath,
  headless: true,
  args: [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--font-render-hinting=none",
  ],
});

const context = await browser.newContext({
  viewport: { width: WIDTH, height: HEIGHT },
  deviceScaleFactor: 1,
});
await context.grantPermissions(["clipboard-read", "clipboard-write"]);
const page = await context.newPage();

const events = [];
const narrationTimeline = [];
const hud = {
  chapter: "01 / 06 · 架构亮点",
  subtitle: "",
  cursorX: 960,
  cursorY: 540,
  cursorVisible: false,
  ripples: [], // { x, y, birthFrame }
  spotlight: null, // { x, y, w, h, label }
};

let frameIdx = 0;
let activeCueEndFrame = 0;
const cueDurations = cfg.cueDurations || {};
const totalSpeechFrames = Object.values(cueDurations).reduce(
  (sum, d) => sum + Math.ceil((Number(d) || 0) * FPS) + Math.round(0.35 * FPS),
  0
);
const EST_TOTAL_FRAMES = Math.max(cfg.fps * 95, totalSpeechFrames || cfg.fps * 95);

function beginCue(cueId, subtitle) {
  hud.subtitle = subtitle || "";
  narrationTimeline.push({
    cueId,
    startFrame: frameIdx,
    startTime: frameIdx / FPS,
  });
  const speechDur = Number(cueDurations[cueId] || 0);
  const minHold = speechDur > 0 ? speechDur + 0.38 : 3.4;
  activeCueEndFrame = frameIdx + Math.ceil(minHold * FPS);
}

async function finishCue(extraTailSec = 0) {
  const target = activeCueEndFrame + Math.round(extraTailSec * FPS);
  if (frameIdx < target) {
    await captureFrames(target - frameIdx);
  }
}

function emitSfx(kind, minGapFrames = 0) {
  if (minGapFrames > 0) {
    const prev = [...events].reverse().find((e) => e.kind === kind);
    if (prev && frameIdx - prev.frame < minGapFrames) return;
  }
  events.push({ frame: frameIdx, time: frameIdx / FPS, kind });
}

async function drawHudFrame() {
  const activeRipples = hud.ripples
    .map((r) => ({ ...r, age: frameIdx - r.birthFrame }))
    .filter((r) => r.age >= 0 && r.age < 16);
  hud.ripples = hud.ripples.filter((r) => frameIdx - r.birthFrame < 16);

  const payload = {
    chapter: hud.chapter,
    subtitle: hud.subtitle,
    cursorX: hud.cursorX,
    cursorY: hud.cursorY,
    cursorVisible: hud.cursorVisible,
    ripples: activeRipples,
    spotlight: hud.spotlight,
    progress: Math.min(1, frameIdx / EST_TOTAL_FRAMES),
  };

  await page.evaluate((st) => {
    let c = document.getElementById("ck-video-hud");
    if (!c) {
      c = document.createElement("canvas");
      c.id = "ck-video-hud";
      c.width = 1920;
      c.height = 1080;
      c.popover = "manual";
      c.style.position = "fixed";
      c.style.inset = "0";
      c.style.width = "100vw";
      c.style.height = "100vh";
      c.style.margin = "0";
      c.style.padding = "0";
      c.style.border = "0";
      c.style.background = "transparent";
      c.style.pointerEvents = "none";
      c.style.zIndex = "2147483647";
      document.documentElement.appendChild(c);
      try { c.showPopover(); } catch {}
    }
    // Keep HUD canvas on top of any newly opened <dialog>.showModal() or [popover] top-layer entry
    const topSignature = `${document.querySelectorAll("dialog[open]").length}:${document.querySelectorAll("[popover]:popover-open").length}`;
    if (window.__hudTopSig !== topSignature) {
      window.__hudTopSig = topSignature;
      try {
        if (c.matches(":popover-open")) c.hidePopover();
        c.showPopover();
      } catch {}
    }
    const ctx = c.getContext("2d");
    ctx.clearRect(0, 0, 1920, 1080);

    // Subtle spotlight highlight box if active
    if (st.spotlight) {
      const s = st.spotlight;
      ctx.save();
      ctx.strokeStyle = "rgba(56, 189, 248, 0.95)";
      ctx.lineWidth = 3;
      ctx.shadowColor = "rgba(56, 189, 248, 0.45)";
      ctx.shadowBlur = 16;
      ctx.beginPath();
      ctx.roundRect(s.x - 6, s.y - 6, s.w + 12, s.h + 12, 12);
      ctx.stroke();
      ctx.restore();
    }

    // Chapter badge pill (top-center clear of top-left title and top-right buttons)
    if (st.chapter) {
      ctx.save();
      ctx.font = '600 17px "Noto Sans CJK SC", sans-serif';
      const tw = ctx.measureText(st.chapter).width;
      const pw = tw + 36;
      const px = (1920 - pw) / 2;
      const py = 12;
      ctx.fillStyle = "rgba(15, 23, 42, 0.88)";
      ctx.strokeStyle = "rgba(56, 189, 248, 0.45)";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.roundRect(px, py, pw, 34, 999);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = "#e0f2fe";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(st.chapter, 960, py + 17.5);
      ctx.restore();
    }

    // Subtitle bar (bottom-center frosted glass pill)
    if (st.subtitle) {
      ctx.save();
      ctx.font = '600 25px "Noto Sans CJK SC", "Maple Mono NF CN", sans-serif';
      const tw = ctx.measureText(st.subtitle).width;
      const pw = Math.min(1760, tw + 56);
      const px = (1920 - pw) / 2;
      const py = 992;
      ctx.fillStyle = "rgba(9, 13, 24, 0.88)";
      ctx.strokeStyle = "rgba(56, 189, 248, 0.46)";
      ctx.lineWidth = 1.5;
      ctx.shadowColor = "rgba(0, 0, 0, 0.48)";
      ctx.shadowBlur = 22;
      ctx.beginPath();
      ctx.roundRect(px, py, pw, 56, 14);
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.stroke();
      ctx.fillStyle = "#f8fafc";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(st.subtitle, 960, py + 28.5);
      ctx.restore();
    }

    // Progress line at very bottom
    ctx.fillStyle = "rgba(56, 189, 248, 0.85)";
    ctx.fillRect(0, 1076, 1920 * st.progress, 4);

    // Click ripple rings
    for (const r of st.ripples) {
      const t = r.age / 16;
      const rad = 8 + t * 38;
      ctx.save();
      ctx.strokeStyle = `rgba(56, 189, 248, ${(1 - t) * 0.9})`;
      ctx.lineWidth = 3 * (1 - t * 0.5);
      ctx.beginPath();
      ctx.arc(r.x, r.y, rad, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }

    // Smooth vector cursor pointer
    if (st.cursorVisible) {
      ctx.save();
      ctx.translate(st.cursorX, st.cursorY);
      ctx.shadowColor = "rgba(0, 0, 0, 0.45)";
      ctx.shadowBlur = 8;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(0, 26);
      ctx.lineTo(7, 20);
      ctx.lineTo(13, 30);
      ctx.lineTo(17, 28);
      ctx.lineTo(11, 18);
      ctx.lineTo(20, 18);
      ctx.closePath();
      ctx.fillStyle = "#ffffff";
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#0f172a";
      ctx.stroke();
      ctx.restore();
    }
  }, payload);
}

async function captureFrames(count) {
  for (let i = 0; i < count; i += 1) {
    await drawHudFrame();
    const buf = await page.screenshot({ type: "jpeg", quality: 93 });
    if (!ff.stdin.write(buf)) {
      await new Promise((r) => ff.stdin.once("drain", r));
    }
    frameIdx += 1;
  }
}

async function holdSeconds(sec, subtitle = null, chapter = null) {
  if (subtitle !== null) hud.subtitle = subtitle;
  if (chapter !== null) hud.chapter = chapter;
  await captureFrames(Math.max(1, Math.round(sec * FPS)));
}

async function moveCursorTo(x, y, frames = 14) {
  hud.cursorVisible = true;
  const sx = hud.cursorX;
  const sy = hud.cursorY;
  for (let i = 1; i <= frames; i += 1) {
    const t = i / frames;
    const ease = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    hud.cursorX = sx + (x - sx) * ease;
    hud.cursorY = sy + (y - sy) * ease;
    await captureFrames(1);
  }
}

async function spotlightBox(locator) {
  const box = await locator.boundingBox();
  hud.spotlight = box ? { x: box.x, y: box.y, w: box.width, h: box.height } : null;
  return box;
}

async function moveAndClick(selectorOrLoc, { subtitle = null, moveFrames = 14, postDelaySec = 0.7 } = {}) {
  if (subtitle !== null) hud.subtitle = subtitle;
  const loc = typeof selectorOrLoc === "string" ? page.locator(selectorOrLoc).first() : selectorOrLoc;
  await loc.scrollIntoViewIfNeeded().catch(() => {});
  const box = await loc.boundingBox();
  if (!box) {
    throw new Error(`Target element not visible for click: ${selectorOrLoc}`);
  }
  const tx = box.x + Math.min(box.width * 0.5, Math.max(14, box.width - 14));
  const ty = box.y + box.height * 0.5;
  hud.spotlight = { x: box.x, y: box.y, w: box.width, h: box.height };
  await moveCursorTo(tx, ty, moveFrames);
  hud.ripples.push({ x: tx, y: ty, birthFrame: frameIdx });
  emitSfx("click");
  await loc.click();
  await captureFrames(6);
  hud.spotlight = null;
  if (postDelaySec > 0.25) {
    await holdSeconds(postDelaySec - 0.25);
  }
}

/* ==================== CHAPTER 1: Intro & Architecture ==================== */
await page.goto(`${cfg.stageUrl}/intro`);
emitSfx("chime");
hud.chapter = "01 / 06 · 架构设计亮点";
hud.cursorVisible = false;
beginCue("c01", cfg.cueSubtitles.c01);
await captureFrames(Math.round(1.6 * FPS));
await finishCue();

await page.evaluate(() => {
  document.querySelectorAll(".arch-card").forEach((c, idx) => c.classList.toggle("active", idx === 1));
});
emitSfx("success");
beginCue("c02", cfg.cueSubtitles.c02);
await captureFrames(Math.round(1.5 * FPS));
await finishCue();

await page.evaluate(() => {
  document.querySelectorAll(".arch-card").forEach((c, idx) => c.classList.toggle("active", idx === 2));
});
beginCue("c03", cfg.cueSubtitles.c03);
await captureFrames(Math.round(1.5 * FPS));
await finishCue();

/* ==================== CHAPTER 2: Real CLI Terminal Demo ==================== */
await page.goto(`${cfg.stageUrl}/terminal`);
emitSfx("chime");
hud.chapter = "02 / 06 · 统一 CLI 与 Skill 导出";

const termData = await page.evaluate(() => JSON.parse(document.getElementById("term-data").textContent));

async function appendTermText(text, cls = "") {
  await page.evaluate(
    ({ txt, c }) => {
      const pre = document.getElementById("term-out");
      if (c) {
        const sp = document.createElement("span");
        sp.className = c;
        sp.textContent = txt;
        pre.appendChild(sp);
      } else {
        pre.appendChild(document.createTextNode(txt));
      }
      pre.scrollTop = pre.scrollHeight;
    },
    { txt: text, c: cls }
  );
}

async function typeTermCmd(cmd) {
  await appendTermText("$ ", "cmd");
  for (let i = 0; i < cmd.length; i += 3) {
    await appendTermText(cmd.slice(i, i + 3), "cmd");
    emitSfx("key", 3);
    await captureFrames(1);
  }
  await appendTermText("\n");
}

beginCue("c04", cfg.cueSubtitles.c04);
await typeTermCmd("colabllm services");
await appendTermText(termData.services + "\n\n", "ok");
await finishCue();

beginCue("c05", cfg.cueSubtitles.c05);
await typeTermCmd("colabllm qwen-flash up -i demo --quick-tunnel --auto-restart --dry-run");
await appendTermText(termData.dry_run + "\n\n");
await finishCue();

beginCue("c06", cfg.cueSubtitles.c06);
await typeTermCmd("colabllm skill --output ~/.agents/skills");
await appendTermText(termData.skill + "\n\n");
await typeTermCmd("colabllm web");
await appendTermText("ColabLLM Web 控制台已就绪，一次性访问链接：\n");
await page.evaluate((urlText) => {
  const pre = document.getElementById("term-out");
  const a = document.createElement("a");
  a.id = "term-web-link";
  a.href = "#";
  a.textContent = `➜ 打开本地控制台：${urlText}`;
  pre.appendChild(a);
}, termData.web_url);
await captureFrames(Math.round(1.4 * FPS));
await moveAndClick("#term-web-link", { postDelaySec: 0.5 });
await finishCue();

/* ==================== CHAPTER 3: Web Fleet, Logs & Secrets ==================== */
await page.goto(cfg.webAuthUrl, { waitUntil: "networkidle" });
await page.waitForSelector("#instances-grid article.card");
emitSfx("chime");
hud.chapter = "03 / 06 · 舰队管控、日志与凭据";
beginCue("c07", cfg.cueSubtitles.c07);
await captureFrames(Math.round(2.4 * FPS));
await finishCue();

const qwenFlashCard = page.locator("#instances-grid article.card", { hasText: "qwen-flash" }).first();
beginCue("c08", cfg.cueSubtitles.c08);
await moveAndClick(qwenFlashCard.getByRole("button", { name: "实时日志" }), { postDelaySec: 1.6 });
await moveAndClick('#logs-dialog .dialog-header button[command="close"]', { postDelaySec: 0.4 });
await moveAndClick(qwenFlashCard.locator('button[aria-label^="更多操作"]'), { postDelaySec: 0.4 });
await moveAndClick(qwenFlashCard.locator(".action-popover").getByRole("button", { name: "连接/凭据" }), { postDelaySec: 0.6 });
await moveAndClick("#connect-reveal-btn", { postDelaySec: 1.0 });
await moveAndClick("#connect-copy-py", { postDelaySec: 0.8 });
await moveAndClick('#connect-dialog .dialog-header button[command="close"]', { postDelaySec: 0.4 });
await finishCue();

beginCue("c09", cfg.cueSubtitles.c09);
await moveAndClick("#fleet-tab-catalog", { postDelaySec: 1.4 });
await moveAndClick("button[data-open-up]", { postDelaySec: 1.6 });
await moveAndClick('#up-dialog .dialog-header button[command="close"]', { postDelaySec: 0.4 });
await finishCue();

/* ==================== CHAPTER 4: Copilot Studio (Chat + GenUI) ==================== */
beginCue("c10", cfg.cueSubtitles.c10);
await moveAndClick('button.nav-btn[data-view="copilot"]', { postDelaySec: 0.7 });
await page.selectOption("#ck-target", "qwen-flash");
await captureFrames(Math.round(0.4 * FPS));
emitSfx("chime");
hud.chapter = "04 / 06 · AI 演练场与同源代理";
await moveAndClick("#ck-probe-btn", { postDelaySec: 1.1 });
await finishCue();

beginCue("c11", cfg.cueSubtitles.c11);
await moveAndClick("#ck-suggestions button.ck-suggestion-pill", { postDelaySec: 0.3 });
for (let step = 0; step < 34; step += 1) {
  await captureFrames(3);
  const done = await page.evaluate(() => {
    const cards = document.querySelectorAll("#ck-chat-feed .ck-genui-card");
    const stopBtn = document.getElementById("ck-chat-stop");
    return cards.length >= 2 && Boolean(stopBtn?.hidden);
  });
  if (done) break;
}
emitSfx("success");
await captureFrames(Math.round(1.6 * FPS));
await finishCue();

beginCue("c12", cfg.cueSubtitles.c12);
await moveAndClick("#ck-code-btn", { postDelaySec: 0.8 });
await page.selectOption("#ck-code-lang", "python-openai");
await captureFrames(Math.round(1.2 * FPS));
await moveAndClick("#ck-code-copy", { postDelaySec: 0.6 });
await page.keyboard.press("Escape");
await finishCue();

/* ==================== CHAPTER 5: Multimodal & Native Studios ==================== */
emitSfx("chime");
hud.chapter = "05 / 06 · 多模态与专有 API 控制台";

// 5a. Image Studio (minimax-h3-image)
beginCue("c13", cfg.cueSubtitles.c13);
await page.selectOption("#ck-target", "minimax-h3-image");
await captureFrames(Math.round(0.9 * FPS));
await page.fill("#ck-img-prompt", "Futuristic sunlit glass botanical atrium, architectural photography, 4K");
await moveAndClick('#ck-image-root button[type="submit"]', { postDelaySec: 0.3 });
await page.waitForSelector("#ck-img-gallery .ck-media-card");
emitSfx("success");
await captureFrames(Math.round(1.2 * FPS));
await finishCue();

// 5b. IndexTTS Audio & 8D Emotion Studio (indextts)
beginCue("c14", cfg.cueSubtitles.c14);
await page.selectOption("#ck-target", "indextts");
await captureFrames(Math.round(0.8 * FPS));
await page.fill("#ck-aud-input", "这正是我本人，IndexTTS-2.5！支持八维情感调控与零样本音色克隆。");
await moveAndClick('#ck-audio-root form details.ck-accordion > summary', { postDelaySec: 0.6 });
await page.fill("#ck-emo-happy", "0.8");
await moveAndClick('#ck-audio-root form button[type="submit"]', { postDelaySec: 0.3 });
await page.waitForSelector("#ck-audio-root .ck-media-card audio");
emitSfx("success");
await moveAndClick('#ck-audio-root > .ck-studio-grid > div:nth-child(2) details.ck-accordion > summary', { postDelaySec: 0.4 });
await moveAndClick('#ck-audio-root button:has-text("刷新音色列表")', { postDelaySec: 0.9 });
await finishCue();

// 5c. Transcribe SALM + CTC Timestamps (nemotron-transcribe)
beginCue("c15", cfg.cueSubtitles.c15);
await page.selectOption("#ck-target", "nemotron-transcribe");
await captureFrames(Math.round(0.8 * FPS));
await page.fill("#ck-asr-drive", "drive://audio/demo.wav");
await moveAndClick('#ck-transcribe-root button[type="submit"]', { postDelaySec: 0.3 });
await page.waitForSelector("#ck-transcribe-root .ck-ctc-btn");
emitSfx("success");
await captureFrames(Math.round(1.2 * FPS));
await finishCue();

// 5d. Native Specialized Console (comfyui)
beginCue("c16", cfg.cueSubtitles.c16);
await page.selectOption("#ck-target", "comfyui");
await captureFrames(Math.round(0.8 * FPS));
await moveAndClick('#ck-specialized-root button:has-text("/system_stats")', { postDelaySec: 1.4 });
await finishCue();

/* ==================== CHAPTER 6: Quick CopilotSidebar Drawer & Outro ==================== */
emitSfx("chime");
hud.chapter = "06 / 06 · 快捷 Copilot 与总结";
beginCue("c17", cfg.cueSubtitles.c17);
await moveAndClick('button.nav-btn[data-view="fleet"]', { postDelaySec: 0.6 });
await moveAndClick("#copilot-sidebar-btn", { postDelaySec: 0.5 });
await page.selectOption("#ck-drawer-target", "qwen-flash");
await captureFrames(Math.round(1.6 * FPS));
await finishCue();
await moveAndClick("#ck-drawer-close-btn", { postDelaySec: 0.7 });

await page.goto(`${cfg.stageUrl}/outro`);
emitSfx("success");
hud.cursorVisible = false;
beginCue("c18", cfg.cueSubtitles.c18);
await captureFrames(Math.round(1.5 * FPS));
await finishCue(0.9);

// Close streams cleanly
await browser.close();
ff.stdin.end();
await ffDone;
fs.writeFileSync(cfg.eventsPath, JSON.stringify({ frameCount: frameIdx, fps: FPS, events, narrationTimeline }, null, 2));
console.log(`Video frames encoded: ${frameIdx} frames (${(frameIdx / FPS).toFixed(2)}s)`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""


# ---------------------------------------------------------------------------
# Narration cues (subtitle on-screen + spoken voiceover script) & IndexTTS prep
# ---------------------------------------------------------------------------

NARRATION_CUES: tuple[tuple[str, str, str], ...] = (
    (
        "c01",
        "ColabLLM：本机零 GPU 依赖，一键将 Google Colab GPU 变为持久化 AI 服务集群",
        "欢迎来到 Colab LLM。无需本地显卡，只需一条命令，就能把 Google Colab 变为持久可控的云端大模型集群。",
    ),
    (
        "c02",
        "结合 Cloudflare / FRP 固定域名隧道与 colab-ext 免浏览器共享网盘挂载",
        "通过 Cloudflare 或 FRP 自动绑定固定 HTTPS 域名，并结合免浏览器扩展直接挂载 Google 网盘。",
    ),
    (
        "c03",
        "支持 19 个开箱即用运行时，遵循“1 服务 = 1 Notebook + 1 执行脚本”隔离约定",
        "项目内置十九个开箱即用运行时，遵循一个服务对应一个笔记本和脚本的清晰约定，凭据与状态全隔离。",
    ),
    (
        "c04",
        "colabllm services 一览全部分类运行时与默认 GPU / 传输端口",
        "首先看统一命令行。输入 services 可以列出所有已打包的模型服务，共享一致的启停与状态接口。",
    ),
    (
        "c05",
        "支持 --dry-run 免启动预览计划、--auto-restart 掉线自动拉起与多实例 (-i)",
        "通过 dry run 参数免消耗预览执行计划，还能开启掉线自动重建守护与多实例隔离运行。",
    ),
    (
        "c06",
        "一行 colabllm web 启动零依赖本地控制台，一次性 Token 鉴权保护",
        "既能一键导出 Agent Skill 给编程助手使用，也能启动本地 Web 控制台，点击一次性令牌链接即可安全登录。",
    ),
    (
        "c07",
        "实时 KPI 看板与卡片/表格多视图，全局掌控多账户 GPU 实例与隧道状态",
        "进入舰队首页，实时看板汇总所有运行中实例、健康状态、多账户归属与传输隧道。",
    ),
    (
        "c08",
        "点击“实时日志”流式查看 Colab 权重加载，默认对 Token 脱敏并可按需揭示",
        "点击实时日志追踪远端模型加载进度。系统默认将密码和密钥脱敏，仅在需要时按需揭示并复制接入代码。",
    ),
    (
        "c09",
        "服务目录按能力自动打标，启动表单由各服务 argparse 契约动态解析",
        "服务目录自动标注 OpenAI 接口兼容能力。点击启动新实例，高级参数表单均由命令行契约动态生成。",
    ),
    (
        "c10",
        "进入 AI 演练场：所有请求经本机同源代理 /api/proxy/... 注入密钥，浏览器零留存",
        "接下来来到全新 AI 演练场。每次请求都经由本机同源代理自动补全认证，浏览器端全程不保存密钥。",
    ),
    (
        "c11",
        "流式思考轨迹可随时折叠；模型直接通过 Generative UI 渲染实时 KPI 卡片与对比表格",
        "开启对话后，不仅流式展现推理思考过程，还能通过前端工具直接在聊天内渲染实时指标卡片和对比表格。",
    ),
    (
        "c12",
        "实时导出与当前实例和模型同步的同源代理、公网直连 cURL 与 Python SDK 代码",
        "右上角可以随时导出与当前模型同步的 cURL 和 Python OpenAI 客户端模板。",
    ),
    (
        "c13",
        "切换 minimax-h3-image：面板自动适配为图像生成与编辑，自动回传成图与 Drive 路径",
        "切换到图像模型时，界面自动变为图像工作台，支持多图参考编辑并直接回传网盘保存路径。",
    ),
    (
        "c14",
        "切换 indextts：支持 8 维情感控制、语音合成与零样本音色克隆库管理",
        "这正是我本人，Index TTS 二点五！在语音面板可以直接调节八维情感向量、克隆音色并试听合成效果。",
    ),
    (
        "c15",
        "切换 nemotron-transcribe：SALM 音文转写返回词级 CTC 时间戳切片并联动播放器",
        "切换到 Nemotron 语音识别模型，除了完整转写文本，还能点击词级时间戳切片跳转试听。",
    ),
    (
        "c16",
        "非 OpenAI 服务（ComfyUI、Unlimited-OCR、R2 迁移、Unsloth）自动切入专用原生控制台",
        "而对于 ComfyUI、OCR 与迁移工具等非 OpenAI 服务，演练场会自动切入专用原生控制台。",
    ),
    (
        "c17",
        "任意页面顶栏点击“快捷 Copilot”呼出右侧滑出抽屉，无缝衔接当前会话上下文",
        "在任意管理页面，随时点击顶栏呼出右侧快捷 Copilot 抽屉，共享完整会话历史。",
    ),
    (
        "c18",
        "ColabLLM · 立即安装并开启高效的 Google Colab GPU 大模型服务编排体验",
        "现在就使用 uv tool install colabllm，轻松开启高效的云端大模型服务之旅吧！",
    ),
)


def _prepare_indextts_narration(
    tts_dir: Path,
    ffmpeg_bin: str,
    *,
    mode: str,
    instance: str,
    voice: str,
    speed: float,
    wait_ready: int,
) -> dict[str, dict[str, object]]:
    """Synthesize normalized 48kHz mono WAV narration cues via the live indextts runtime."""
    if mode == "none":
        return {}

    cli = str(ROOT / "colabllm")
    inst_args = ["-i", instance] if instance else []

    def check_status() -> dict:
        res = subprocess.run(
            [sys.executable, cli, "indextts", *inst_args, "status", "--json"],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=30,
        )
        try:
            return json.loads(res.stdout) if res.stdout.strip().startswith("{") else {}
        except ValueError:
            return {}

    deadline = time.monotonic() + max(0, wait_ready)
    status = check_status()
    if mode == "auto" and not status.get("running") and not status.get("ready"):
        print("[video-tts] No active indextts instance detected; falling back to instrumental audio.")
        return {}

    while not status.get("ready"):
        if not status.get("running"):
            raise RuntimeError(
                f"indextts runtime is not running (status: {status}); start it with `colabllm indextts up` or use --tts none."
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out after {wait_ready}s waiting for indextts readiness.")
        print("[video-tts] Waiting for live indextts L4 runtime to report ready...")
        time.sleep(5.0)
        status = check_status()

    chosen_voice = voice.strip() or "default"
    voices_res = subprocess.run(
        [sys.executable, cli, "indextts", *inst_args, "voices", "--json"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if voices_res.returncode == 0 and voices_res.stdout.strip().startswith("{"):
        try:
            vdata = json.loads(voices_res.stdout)
            names = [
                v.get("name") if isinstance(v, dict) else str(v)
                for v in (vdata.get("voices") or vdata.get("data") or [])
            ]
            names = [n for n in names if n]
            if names and not voice.strip():
                non_default = [n for n in names if n != "default"]
                chosen_voice = non_default[0] if non_default else names[0]
            print(f"[video-tts] indextts ready; available voices={names}, using={chosen_voice}")
        except ValueError:
            print(f"[video-tts] indextts ready; using voice={chosen_voice}")

    tts_dir.mkdir(parents=True, exist_ok=True)
    clips: dict[str, dict[str, object]] = {}

    for idx, (cue_id, _, spoken) in enumerate(NARRATION_CUES, 1):
        raw_wav = tts_dir / f"raw_{cue_id}.wav"
        trimmed_wav = tts_dir / f"trimmed_{cue_id}.wav"
        norm_wav = tts_dir / f"norm_{cue_id}.wav"

        print(f"[video-tts] Synthesizing cue {idx}/{len(NARRATION_CUES)} ({cue_id})...")
        subprocess.run(
            [
                sys.executable,
                cli,
                "indextts",
                *inst_args,
                "tts",
                "--text",
                spoken,
                "--voice",
                chosen_voice,
                "--speed",
                f"{speed:.2f}",
                "--lang",
                "zhen",
                "-o",
                str(raw_wav),
            ],
            cwd=str(ROOT),
            check=True,
            timeout=120,
        )

        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-i",
                str(raw_wav),
                "-af",
                "silenceremove=start_periods=1:start_silence=0.04:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_silence=0.06:start_threshold=-45dB,areverse",
                "-ar",
                "48000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(trimmed_wav),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with wave.open(str(trimmed_wav), "rb") as wf:
            nframes = wf.getnframes()

        if nframes < int(0.35 * 48000):
            subprocess.run(
                [ffmpeg_bin, "-y", "-i", str(raw_wav), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(trimmed_wav)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        with wave.open(str(trimmed_wav), "rb") as wf:
            pcm_bytes = wf.readframes(wf.getnframes())

        pcm = array("h")
        pcm.frombytes(pcm_bytes)
        if not pcm:
            raise RuntimeError(f"Synthesized audio for {cue_id} is empty.")
        peak = max(abs(s) for s in pcm) or 1
        gain = min(6.0, 26800.0 / peak)
        norm = array("h", (max(-32767, min(32767, int(s * gain))) for s in pcm))
        with wave.open(str(norm_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(norm.tobytes())

        dur_sec = len(norm) / 48000.0
        clips[cue_id] = {"path": norm_wav, "duration": dur_sec}
        print(f"[video-tts]   -> {cue_id}: {dur_sec:.2f}s")

    return clips


# ---------------------------------------------------------------------------
# Pure-stdlib ambient music + UI SFX stereo audio synthesizer
# ---------------------------------------------------------------------------

def _synthesize_soundtrack(
    events_path: Path,
    wav_out: Path,
    *,
    narration_clips: dict[str, dict[str, object]] | None = None,
) -> None:
    """Generate 48kHz stereo soundtrack matching exact video frame count + ducked BGM + SFX + VO."""
    meta = json.loads(events_path.read_text(encoding="utf-8"))
    frame_count = int(meta["frameCount"])
    fps = float(meta["fps"])
    duration = max(1.0, frame_count / fps)
    sr = 48000
    total_samples = int(round(duration * sr))
    clips = narration_clips or {}

    # 1. Place normalized voiceover clips at their recorded cue startFrame & build ducking envelope
    voice_mono = [0.0] * total_samples
    voice_mask = [0.0] * total_samples
    pre_samples = int(0.18 * sr)
    rel_samples = int(0.28 * sr)

    if clips:
        for entry in meta.get("narrationTimeline", []):
            cue_id = entry.get("cueId")
            clip = clips.get(cue_id)
            if not clip:
                continue
            start_idx = max(0, min(total_samples - 1, int(round(float(entry["startTime"]) * sr))))
            with wave.open(str(clip["path"]), "rb") as wf:
                raw_pcm = wf.readframes(wf.getnframes())
            pcm = array("h")
            pcm.frombytes(raw_pcm)
            n = len(pcm)
            end_idx = min(total_samples, start_idx + n)
            for j in range(end_idx - start_idx):
                voice_mono[start_idx + j] += pcm[j] / 32768.0
                voice_mask[start_idx + j] = 1.0
            for k in range(pre_samples):
                idx = start_idx - pre_samples + k
                if 0 <= idx < total_samples:
                    voice_mask[idx] = max(voice_mask[idx], (k + 1) / pre_samples)
            for k in range(rel_samples):
                idx = end_idx + k
                if 0 <= idx < total_samples:
                    voice_mask[idx] = max(voice_mask[idx], 1.0 - (k + 1) / rel_samples)

    # Fast 4096-point sine LUT keeps Python synthesis under ~1.5s for ~110s stereo
    lut_size = 4096
    sin_lut = [math.sin(2.0 * math.pi * i / lut_size) for i in range(lut_size)]
    lut_mask = lut_size - 1

    # Cmaj9 -> Am9 -> Fmaj7 -> G6 ambient progression (4s per chord)
    chords = [
        (130.81, 196.00, 246.94, 293.66),
        (110.00, 164.81, 246.94, 293.66),
        (87.31, 174.61, 220.00, 261.63),
        (98.00, 196.00, 246.94, 329.63),
    ]
    chord_sec = 4.0
    chord_samples = int(chord_sec * sr)

    left = [0.0] * total_samples
    right = [0.0] * total_samples

    # 2. Warm ambient pad layer with sidechain ducking whenever narration is speaking
    phase_steps = [[(f * lut_size) / sr for f in ch] for ch in chords]
    detune_steps = [[(f * 1.0022 * lut_size) / sr for f in ch] for ch in chords]

    p_l = [0.0, 0.0, 0.0, 0.0]
    p_r = [0.0, 0.0, 0.0, 0.0]
    fade_in = int(1.6 * sr)
    fade_out = int(2.4 * sr)
    has_vo = bool(clips)

    for i in range(total_samples):
        c_idx = (i // chord_samples) % len(chords)
        in_chord = i % chord_samples
        c_env = min(1.0, in_chord / 4800.0, (chord_samples - in_chord) / 4800.0)
        global_env = 1.0
        if i < fade_in:
            global_env = i / fade_in
        elif i > total_samples - fade_out:
            global_env = max(0.0, (total_samples - i) / fade_out)

        sl = phase_steps[c_idx]
        sr_st = detune_steps[c_idx]
        acc_l = 0.0
        acc_r = 0.0
        for v in range(4):
            p_l[v] = (p_l[v] + sl[v]) % lut_size
            p_r[v] = (p_r[v] + sr_st[v]) % lut_size
            acc_l += sin_lut[int(p_l[v]) & lut_mask]
            acc_r += sin_lut[int(p_r[v]) & lut_mask]

        base_amp = (0.068 - 0.044 * voice_mask[i]) if has_vo else 0.085
        amp = base_amp * global_env * (0.45 + 0.55 * c_env)
        left[i] = acc_l * amp
        right[i] = acc_r * amp

    # 3. Mix frame-locked UI micro-SFX cleanly onto both channels
    def add_tone(start_sample: int, dur_sec: float, f0: float, f1: float, gain: float) -> None:
        n = min(int(dur_sec * sr), total_samples - start_sample)
        if n <= 0:
            return
        ph = 0.0
        for j in range(n):
            u = j / n
            freq = f0 + (f1 - f0) * u
            env = min(1.0, j / 120.0) * math.exp(-u * 4.2)
            ph = (ph + (freq * lut_size) / sr) % lut_size
            v = sin_lut[int(ph) & lut_mask] * env * gain
            idx = start_sample + j
            left[idx] += v
            right[idx] += v

    sfx_scale = 0.52 if has_vo else 1.0
    for evt in meta.get("events", []):
        s0 = max(0, min(total_samples - 1, int(float(evt["time"]) * sr)))
        kind = evt["kind"]
        if kind == "click":
            add_tone(s0, 0.045, 920.0, 280.0, 0.22 * sfx_scale)
        elif kind == "key":
            add_tone(s0, 0.018, 1480.0, 860.0, 0.09 * sfx_scale)
        elif kind == "chime":
            add_tone(s0, 0.26, 523.25, 523.25, 0.14 * sfx_scale)
            add_tone(s0 + int(0.09 * sr), 0.32, 783.99, 783.99, 0.14 * sfx_scale)
        elif kind == "success":
            for step_i, freq in enumerate((523.25, 659.25, 783.99, 1046.50)):
                add_tone(s0 + int(step_i * 0.055 * sr), 0.34, freq, freq, 0.11 * sfx_scale)

    # 4. Blend narration voiceover front-and-center with soft tanh safety limiter
    interleaved = array("h")
    for idx_s, (l_val, r_val) in enumerate(zip(left, right)):
        vo = voice_mono[idx_s] * 0.85
        l_clamped = max(-32767, min(32767, int(math.tanh(l_val + vo) * 30500)))
        r_clamped = max(-32767, min(32767, int(math.tanh(r_val + vo) * 30500)))
        interleaved.append(l_clamped)
        interleaved.append(r_clamped)

    with wave.open(str(wav_out), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(interleaved.tobytes())


# ---------------------------------------------------------------------------
# Entrypoint & orchestration
# ---------------------------------------------------------------------------

@contextmanager
def _patched_env(updates: dict[str, str]):
    saved = {k: os.environ.get(k) for k in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _generate_poster_png(
    video_path: Path,
    poster_path: Path,
    *,
    ffmpeg_bin: str,
    node_bin: str,
    chromium_path: str,
    pw_node_modules: str,
) -> None:
    """Extract a representative frame from video_path and composite a crisp play-button poster."""
    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    probe = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    dur_sec = max(1.0, float(probe.stdout.strip() or "90"))
    seek_sec = min(max(1.0, dur_sec - 2.0), dur_sec * 0.61)
    mm, ss = divmod(int(round(dur_sec)), 60)
    dur_label = f"{mm:02d}:{ss:02d}"

    with tempfile.TemporaryDirectory(prefix="colabllm-poster-") as tmp:
        tmp_dir = Path(tmp)
        raw_frame = tmp_dir / "frame.png"
        render_js = tmp_dir / "poster.cjs"
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-ss",
                f"{seek_sec:.2f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                str(raw_frame),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        frame_b64 = base64.b64encode(raw_frame.read_bytes()).decode("ascii")
        render_js.write_text(
            """
const fs = require("node:fs");
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.COLAB_CHROMIUM_BIN,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  const frameUrl = "data:image/png;base64," + fs.readFileSync(process.env.COLAB_FRAME_B64_PATH, "utf8");
  await page.setContent(`<!doctype html><html><body style="margin:0;overflow:hidden;background:#090d16;">
    <canvas id="c" width="1920" height="1080"></canvas>
  </body></html>`);
  await page.evaluate(async ({ url, durText }) => {
    const img = new Image();
    img.src = url;
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; });
    const c = document.getElementById("c");
    const ctx = c.getContext("2d");
    ctx.drawImage(img, 0, 0, 1920, 1080);

    // Light atmospheric center scrim so UI stays crisp while play button stands out
    const grad = ctx.createRadialGradient(960, 540, 80, 960, 540, 760);
    grad.addColorStop(0, "rgba(15, 23, 42, 0.42)");
    grad.addColorStop(1, "rgba(15, 23, 42, 0.08)");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 1920, 1080);

    // Play circle + shadow
    ctx.save();
    ctx.shadowColor = "rgba(0, 0, 0, 0.48)";
    ctx.shadowBlur = 34;
    ctx.fillStyle = "rgba(14, 165, 233, 0.94)";
    ctx.beginPath();
    ctx.arc(960, 515, 68, 0, Math.PI * 2);
    ctx.fill();
    ctx.lineWidth = 4;
    ctx.strokeStyle = "rgba(248, 250, 252, 0.92)";
    ctx.stroke();
    ctx.restore();

    // Triangle play glyph
    ctx.fillStyle = "#ffffff";
    ctx.beginPath();
    ctx.moveTo(940, 483);
    ctx.lineTo(940, 547);
    ctx.lineTo(995, 515);
    ctx.closePath();
    ctx.fill();

    // Frosted caption pill under play icon
    const label = "点击播放有声全景实操演示 · " + durText + "（IndexTTS-2.5 配音）";
    ctx.save();
    ctx.font = '700 28px "Noto Sans CJK SC", sans-serif';
    const tw = ctx.measureText(label).width;
    const pw = tw + 58;
    const px = (1920 - pw) / 2;
    const py = 610;
    ctx.fillStyle = "rgba(15, 23, 42, 0.90)";
    ctx.strokeStyle = "rgba(56, 189, 248, 0.65)";
    ctx.lineWidth = 2;
    ctx.shadowColor = "rgba(0, 0, 0, 0.45)";
    ctx.shadowBlur = 24;
    ctx.beginPath();
    ctx.roundRect(px, py, pw, 62, 999);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.stroke();
    ctx.fillStyle = "#f8fafc";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, 960, py + 31);
    ctx.restore();
  }, { url: frameUrl, durText: process.env.COLAB_DUR_LABEL });
  await page.screenshot({ path: process.env.COLAB_POSTER_OUT, type: "png" });
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
""",
            encoding="utf-8",
        )
        b64_path = tmp_dir / "frame.b64"
        b64_path.write_text(frame_b64, encoding="utf-8")
        poster_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [node_bin, str(render_js)],
            env={
                **os.environ,
                "NODE_PATH": pw_node_modules,
                "COLAB_CHROMIUM_BIN": chromium_path,
                "COLAB_FRAME_B64_PATH": str(b64_path),
                "COLAB_POSTER_OUT": str(poster_path),
                "COLAB_DUR_LABEL": dur_label,
            },
            check=True,
        )


def generate_video(
    output_path: Path,
    *,
    fps: int = 24,
    tts_mode: str = "auto",
    tts_instance: str = "",
    tts_voice: str = "",
    tts_speed: float = 1.05,
    wait_ready: int = 600,
) -> None:
    node_bin = shutil.which("node")
    ffmpeg_bin = shutil.which("ffmpeg")
    if not node_bin or not ffmpeg_bin:
        raise RuntimeError("Both 'node' and 'ffmpeg' must be available in PATH.")
    chromium_path = _find_chromium_executable()
    pw_node_modules = _find_playwright_node_modules()

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="colabllm-video-") as tmp:
        tmp_dir = Path(tmp)
        sandbox_home = tmp_dir / "sandbox_home"
        tts_clips = _prepare_indextts_narration(
            tmp_dir / "tts",
            ffmpeg_bin,
            mode=tts_mode,
            instance=tts_instance,
            voice=tts_voice,
            speed=tts_speed,
            wait_ready=wait_ready,
        )
        cue_durations = {k: float(v["duration"]) for k, v in tts_clips.items()}
        cue_subtitles = {cue_id: sub for cue_id, sub, _ in NARRATION_CUES}

        mock_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_mock_handler())
        mock_th = threading.Thread(target=mock_srv.serve_forever, daemon=True)
        mock_th.start()
        mock_port = mock_srv.server_address[1]

        stage_srv = None
        console_srv = None
        try:
            _populate_sandbox_home(sandbox_home, mock_port)
            cli_samples = _run_real_cli_samples(sandbox_home, tmp_dir)

            with _patched_env({"COLABLLM_HOME": str(sandbox_home)}):
                console_srv = web_server.WebConsoleServer(("127.0.0.1", 0), read_only=False)
                console_th = threading.Thread(
                    target=console_srv.serve_forever,
                    kwargs={"poll_interval": 0.1},
                    daemon=True,
                )
                console_th.start()
                web_port = console_srv.server_address[1]
                web_auth_url = f"http://127.0.0.1:{web_port}/auth?token={console_srv.bootstrap_token}"

                stage_pages = _build_stage_html(cli_samples, web_auth_url, web_port)
                stage_srv, stage_port = _start_stage_server(stage_pages)

                raw_video_path = tmp_dir / "raw_video.mp4"
                events_path = tmp_dir / "events.json"
                soundtrack_path = tmp_dir / "soundtrack.wav"
                cfg_path = tmp_dir / "choreographer_cfg.json"
                script_path = tmp_dir / "choreographer.cjs"

                cfg_path.write_text(
                    json.dumps(
                        {
                            "fps": fps,
                            "ffmpegPath": ffmpeg_bin,
                            "chromiumPath": chromium_path,
                            "rawVideoPath": str(raw_video_path),
                            "eventsPath": str(events_path),
                            "stageUrl": f"http://127.0.0.1:{stage_port}",
                            "webAuthUrl": web_auth_url,
                            "cueDurations": cue_durations,
                            "cueSubtitles": cue_subtitles,
                        }
                    ),
                    encoding="utf-8",
                )
                script_path.write_text(_CHOREOGRAPHER_CJS, encoding="utf-8")

                subprocess.run(
                    [node_bin, str(script_path), str(cfg_path)],
                    env={**os.environ, "NODE_PATH": pw_node_modules},
                    check=True,
                )

                _synthesize_soundtrack(events_path, soundtrack_path, narration_clips=tts_clips)

                subprocess.run(
                    [
                        ffmpeg_bin,
                        "-y",
                        "-i",
                        str(raw_video_path),
                        "-i",
                        str(soundtrack_path),
                        "-c:v",
                        "libx264",
                        "-preset",
                        "slow",
                        "-crf",
                        "20",
                        "-pix_fmt",
                        "yuv420p",
                        "-movflags",
                        "+faststart",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-ar",
                        "48000",
                        "-shortest",
                        str(output_path),
                    ],
                    check=True,
                )

                poster_path = output_path.with_name(f"{output_path.stem}-poster.png")
                _generate_poster_png(
                    output_path,
                    poster_path,
                    ffmpeg_bin=ffmpeg_bin,
                    node_bin=node_bin,
                    chromium_path=chromium_path,
                    pw_node_modules=pw_node_modules,
                )
        finally:
            if console_srv is not None:
                console_srv.shutdown()
                console_srv.server_close()
            if stage_srv is not None:
                stage_srv.shutdown()
                stage_srv.server_close()
            mock_srv.shutdown()
            mock_srv.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Record the ColabLLM walkthrough video.")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "assets" / "colabllm-walkthrough.mp4",
        help="Destination MP4 file path",
    )
    parser.add_argument("--fps", type=int, default=24, help="Video frame rate (default: 24)")
    parser.add_argument(
        "--tts",
        choices=("auto", "indextts", "none"),
        default="auto",
        help="Voiceover backend: auto (use live indextts if running), indextts (require), or none",
    )
    parser.add_argument("--tts-instance", default="", help="Target indextts instance ID (-i)")
    parser.add_argument("--tts-voice", default="", help="indextts registered voice name (default: auto)")
    parser.add_argument("--tts-speed", type=float, default=1.05, help="indextts speaking rate multiplier (default: 1.05)")
    parser.add_argument("--wait-ready", type=int, default=600, help="Seconds to wait for indextts readiness")
    parser.add_argument(
        "--poster-from",
        type=Path,
        default=None,
        help="Only generate <stem>-poster.png from an existing video without re-recording",
    )
    args = parser.parse_args()
    if args.poster_from is not None:
        ffmpeg_bin = shutil.which("ffmpeg")
        node_bin = shutil.which("node")
        if not ffmpeg_bin or not node_bin:
            raise RuntimeError("Both 'node' and 'ffmpeg' must be available in PATH.")
        poster_out = args.poster_from.with_name(f"{args.poster_from.stem}-poster.png")
        _generate_poster_png(
            args.poster_from.resolve(),
            poster_out.resolve(),
            ffmpeg_bin=ffmpeg_bin,
            node_bin=node_bin,
            chromium_path=_find_chromium_executable(),
            pw_node_modules=_find_playwright_node_modules(),
        )
        print(f"Poster PNG generated at: {poster_out}")
        return 0
    generate_video(
        args.output,
        fps=args.fps,
        tts_mode=args.tts,
        tts_instance=args.tts_instance,
        tts_voice=args.tts_voice,
        tts_speed=args.tts_speed,
        wait_ready=args.wait_ready,
    )
    print(f"Walkthrough video generated at: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
