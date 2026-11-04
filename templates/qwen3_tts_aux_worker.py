"""Auxiliary loopback multi-task GPU worker for Qwen3-TTS (CustomVoice / VoiceDesign / Base)."""
from __future__ import annotations

import gc
import io
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import torch
from huggingface_hub import snapshot_download
from qwen_tts import Qwen3TTSModel

MODELS_ROOT = Path(os.environ.get("CT_MODELS_ROOT", "/content/qwen3-tts/model"))
AUX_PORT = int(os.environ.get("CT_QWEN3_TTS_AUX_PORT", "8001"))

TASK_REPOS = {
    "CustomVoice": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "VoiceDesign": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "Base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
}

_LOCK = threading.Lock()
_LOADED_TASK: str | None = None
_LOADED_MODEL: Qwen3TTSModel | None = None


def _get_or_load_model(task_type: str) -> Qwen3TTSModel:
    global _LOADED_TASK, _LOADED_MODEL
    if _LOADED_TASK == task_type and _LOADED_MODEL is not None:
        return _LOADED_MODEL
    if _LOADED_MODEL is not None:
        del _LOADED_MODEL
        _LOADED_MODEL = None
        _LOADED_TASK = None
        gc.collect()
        torch.cuda.empty_cache()

    repo_id = TASK_REPOS.get(task_type, TASK_REPOS["Base"])
    local_dir = MODELS_ROOT / repo_id.split("/")[-1]
    if not (local_dir / "model.safetensors").is_file():
        local_dir.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=repo_id, local_dir=str(local_dir), max_workers=4)

    _LOADED_MODEL = Qwen3TTSModel.from_pretrained(
        str(local_dir),
        device_map="cuda:0",
        dtype=torch.bfloat16,
    )
    _LOADED_TASK = task_type
    return _LOADED_MODEL


def _encode_audio(wav: np.ndarray, sr: int, fmt: str = "wav", speed: float = 1.0) -> tuple[bytes, int]:
    wav_f32 = np.asarray(wav, dtype=np.float32).reshape(-1)
    buf = io.BytesIO()
    sf.write(buf, wav_f32, sr, format="WAV", subtype="PCM_16")
    raw_wav = buf.getvalue()
    fmt = (fmt or "wav").lower()
    if abs(speed - 1.0) < 1e-3 and fmt == "wav":
        return raw_wav, sr

    af_filters = []
    rem = max(0.25, min(4.0, float(speed)))
    while rem > 2.0 + 1e-3:
        af_filters.append("atempo=2.0")
        rem /= 2.0
    while rem < 0.5 - 1e-3:
        af_filters.append("atempo=0.5")
        rem /= 0.5
    if abs(rem - 1.0) >= 1e-3:
        af_filters.append(f"atempo={rem:.4f}")

    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0"]
    if af_filters:
        cmd += ["-filter:a", ",".join(af_filters)]
    if fmt == "mp3":
        cmd += ["-f", "mp3", "pipe:1"]
    elif fmt == "flac":
        cmd += ["-f", "flac", "pipe:1"]
    elif fmt == "opus":
        cmd += ["-c:a", "libopus", "-f", "ogg", "pipe:1"]
    elif fmt == "pcm":
        cmd += ["-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    else:
        cmd += ["-f", "wav", "-acodec", "pcm_s16le", "pipe:1"]
    res = subprocess.run(cmd, input=raw_wav, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
    if res.returncode == 0 and res.stdout:
        return res.stdout, sr
    return raw_wav, sr


class AuxHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "loaded_task": _LOADED_TASK})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/synthesize":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw)
            task_type = str(req.get("task_type") or "Base")
            text = str(req.get("input") or req.get("text") or "").strip()
            language = str(req.get("language") or "Auto")
            instruct = str(req.get("instructions") or req.get("instruct") or "").strip()
            speaker = str(req.get("voice") or req.get("speaker") or "vivian").strip()
            ref_audio = req.get("ref_audio")
            if isinstance(ref_audio, str) and ref_audio.startswith("file://"):
                ref_audio = ref_audio[len("file://"):]
            ref_text = req.get("ref_text")
            x_vec_only = bool(req.get("x_vector_only_mode", not bool(ref_text and str(ref_text).strip())))
            fmt = str(req.get("response_format") or "wav").lower()
            speed = float(req.get("speed") or 1.0)
            max_new_tokens = int(req.get("max_new_tokens") or 2048)

            with _LOCK:
                model = _get_or_load_model(task_type)
                if task_type == "VoiceDesign":
                    wavs, sr = model.generate_voice_design(
                        text=text,
                        language=language,
                        instruct=instruct or "Natural, expressive human voice.",
                        max_new_tokens=max_new_tokens,
                    )
                elif task_type == "CustomVoice":
                    wavs, sr = model.generate_custom_voice(
                        text=text,
                        language=language,
                        speaker=speaker,
                        instruct=instruct,
                        max_new_tokens=max_new_tokens,
                    )
                else:
                    wavs, sr = model.generate_voice_clone(
                        text=text,
                        language=language,
                        ref_audio=ref_audio,
                        ref_text=ref_text if not x_vec_only else None,
                        x_vector_only_mode=x_vec_only,
                        max_new_tokens=max_new_tokens,
                    )
            audio_bytes, out_sr = _encode_audio(wavs[0], int(sr), fmt=fmt, speed=speed)
            mime_map = {
                "wav": "audio/wav",
                "mp3": "audio/mpeg",
                "flac": "audio/flac",
                "opus": "audio/ogg",
                "pcm": "audio/pcm",
            }
            self.send_response(200)
            self.send_header("Content-Type", mime_map.get(fmt, "audio/wav"))
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.send_header("X-Audio-Sample-Rate", str(out_sr))
            self.send_header("X-Qwen3-TTS-Engine", f"aux-{task_type}")
            self.end_headers()
            self.wfile.write(audio_bytes)
        except Exception as e:
            import traceback
            self._send_json(500, {"error": {"message": str(e), "traceback": traceback.format_exc()[-800:]}})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", AUX_PORT), AuxHandler)
    print(f"[aux-worker] listening on 127.0.0.1:{AUX_PORT}", flush=True)
    srv.serve_forever()
