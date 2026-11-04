"""Authenticated ASGI middleware and Google Drive / yt-dlp endpoints for Nemotron Transcribe."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any

from colab_asgi_common import (
    AUDIO_EXTS,
    list_drive_audio_files,
    rel_to_mydrive,
    resolve_drive_path,
    verify_bearer_auth,
)
from nemotron_audio import (
    PRESETS,
    YTDLP_DOMAIN_MARKERS,
    build_prompt_and_settings,
    cleanup_generation_loops,
    compute_chunk_ranges,
    fmt_srt_time,
    load_audio_as_data_url,
    probe_audio_duration_sec,
    shift_ctc_timestamps,
    words_to_srt,
)

try:
    from starlette.responses import JSONResponse
except ImportError:  # pragma: no cover - local unit-test fallback without starlette
    JSONResponse = None  # type: ignore[assignment,misc]

try:
    from nemo.collections.speechlm2.vllm.salm.ctc_serving import ctc_timestamp_middleware
except ImportError:  # pragma: no cover - local unit-test fallback without nemo
    ctc_timestamp_middleware = None  # type: ignore[assignment]

API_TOKEN = os.environ.get("CT_API_TOKEN", "")
SERVICE_PORT = int(os.environ.get("CT_SERVICE_PORT", "8000"))
DATA_DIR = Path(os.environ.get("CT_DATA_DIR", "/content/drive/MyDrive/colab-llm/nemotron-transcribe"))
DRIVE_ROOT = Path("/content/drive")
MYDRIVE_ROOT = Path("/content/drive/MyDrive")
MODEL_REPO = os.environ.get("CT_NEMOTRON_REPO", "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B")


def _check_auth(request: Any) -> bool:
    return verify_bearer_auth(request.headers, API_TOKEN)


def _resolve_path(raw_spec: str, default_subdir: str | None = "audio") -> Path:
    return resolve_drive_path(
        raw_spec,
        data_dir=DATA_DIR,
        default_subdir=default_subdir,
        mydrive_root=MYDRIVE_ROOT,
        drive_root=DRIVE_ROOT,
    )


def _call_local_chat_completions(payload: dict[str, Any], timeout: int = 1800) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{SERVICE_PORT}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": {"message": raw, "code": e.code}}


def _is_media_url(spec: str) -> bool:
    s = (spec or "").strip()
    return s.startswith(("http://", "https://", "ytdl://", "yt-dlp://"))


def _clean_media_url(spec: str) -> str:
    s = (spec or "").strip()
    for pfx in ("ytdl://", "yt-dlp://"):
        if s.startswith(pfx):
            return s[len(pfx):]
    return s


def _ensure_ytdlp() -> str:
    for cand in (
        shutil.which("yt-dlp"),
        "/content/nemo-speech/.venv/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
    ):
        if cand and os.path.isfile(cand):
            return cand
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"], check=True)
    return shutil.which("yt-dlp") or "yt-dlp"


def _prepare_cookies_file(cookies_spec: str = "") -> Path | None:
    candidates: list[Path] = []
    if cookies_spec:
        candidates.append(_resolve_path(cookies_spec, default_subdir=None))
    candidates.extend([
        DATA_DIR / "cookies.txt",
        MYDRIVE_ROOT / "colab-llm" / "cookies.txt",
        MYDRIVE_ROOT / "cookies.txt",
    ])
    for c in candidates:
        if c.is_file() and c.stat().st_size > 0:
            tmp_cookie = Path(f"/tmp/ct_ytdlp_cookies_{os.getpid()}_{uuid.uuid4().hex[:8]}.txt")
            tmp_cookie.touch(mode=0o600)
            shutil.copyfile(c, tmp_cookie)
            return tmp_cookie
    return None


def _sanitize_filename(name: str, max_len: int = 80) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    return (cleaned[:max_len] or "audio").strip("._ ")


def _download_media_url_to_drive(
    raw_url: str,
    download_dir: str = "",
    audio_format: str = "mp3",
    cookies_drive: str = "",
    playlist: bool = False,
    force_download: bool = False,
    download_video: bool = False,
    save_media_to_drive: bool = True,
) -> list[dict[str, Any]]:
    url = _clean_media_url(raw_url)
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Unsupported media URL scheme: {raw_url!r}")
    if save_media_to_drive:
        target_dir = _resolve_path(download_dir, default_subdir=None) if (download_dir and download_dir.strip()) else (DATA_DIR / "audio" / "yt-dlp")
    else:
        target_dir = Path("/content/nemotron-transcribe/ytdlp-temp") / uuid.uuid4().hex[:12]
    target_dir.mkdir(parents=True, exist_ok=True)
    fmt = (audio_format or "mp3").strip().lower().lstrip(".")
    if fmt not in ("mp3", "m4a", "wav", "opus", "flac"):
        fmt = "mp3"

    ytdlp_bin = _ensure_ytdlp()
    stage_dir = Path("/content/nemotron-transcribe/ytdlp-stage") / uuid.uuid4().hex[:12]
    stage_dir.mkdir(parents=True, exist_ok=True)
    cookie_file = _prepare_cookies_file(cookies_drive)

    try:
        out_tmpl = str(stage_dir / "%(extractor)s__%(id)s.%(ext)s")
        cmd = [
            ytdlp_bin,
            "--no-progress",
            "--no-warnings",
        ]
        if download_video:
            cmd.extend([
                "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                "--merge-output-format", "mp4",
            ])
        else:
            cmd.extend([
                "--extract-audio",
                "--audio-format", fmt,
                "--audio-quality", "0",
            ])
        cmd.extend([
            "--write-info-json",
            "--output", out_tmpl,
        ])
        if not playlist:
            cmd.append("--no-playlist")
        else:
            cmd.append("--yes-playlist")
        if cookie_file:
            cmd.extend(["--cookies", str(cookie_file)])
        cmd.append(url)

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1800, check=False)
        info_files = sorted(stage_dir.glob("*.info.json"))
        if res.returncode != 0 and not info_files:
            err = (res.stderr or res.stdout or "").strip()[-1200:]
            raise RuntimeError(f"yt-dlp failed for {url}: {err}")

        downloaded_items: list[dict[str, Any]] = []
        for info_path in info_files:
            try:
                info = json.loads(info_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                info = {}
            if info.get("_type") == "playlist":
                continue
            stem_prefix = info_path.name[:-len(".info.json")]
            audio_candidates = [
                p for p in stage_dir.glob(f"{stem_prefix}.*")
                if p.suffix.lower() in AUDIO_EXTS and not p.name.endswith(".info.json")
            ]
            if not audio_candidates:
                continue
            staged_audio = audio_candidates[0]
            extractor = _sanitize_filename(str(info.get("extractor") or info.get("extractor_key") or "media"), 24).lower()
            vid_id = _sanitize_filename(str(info.get("id") or uuid.uuid4().hex[:8]), 40)
            title = _sanitize_filename(str(info.get("title") or vid_id), 70)
            dest_name = f"{extractor}_{vid_id}_{title}{staged_audio.suffix.lower()}"
            dest_path = target_dir / dest_name

            if save_media_to_drive and not force_download and not dest_path.is_file():
                existing = [
                    p for p in target_dir.glob(f"{extractor}_{vid_id}_*{staged_audio.suffix.lower()}")
                    if p.is_file() and p.stat().st_size > 0
                ]
                if existing:
                    dest_path = existing[0]

            if force_download or not dest_path.is_file() or dest_path.stat().st_size == 0:
                partial_path = target_dir / f".{dest_name}.{os.getpid()}.partial"
                shutil.copyfile(staged_audio, partial_path)
                partial_path.replace(dest_path)

            st = dest_path.stat()
            downloaded_items.append({
                "source_url": raw_url,
                "webpage_url": info.get("webpage_url") or url,
                "video_id": info.get("id"),
                "extractor": info.get("extractor_key") or info.get("extractor"),
                "title": info.get("title"),
                "uploader": info.get("uploader"),
                "duration": info.get("duration"),
                "media_type": "video" if download_video else "audio",
                "saved_to_drive": save_media_to_drive,
                "drive_path": str(dest_path),
                "rel_path": rel_to_mydrive(dest_path, MYDRIVE_ROOT) if save_media_to_drive else dest_path.name,
                "name": dest_path.name,
                "size_bytes": st.st_size,
                "size_mb": round(st.st_size / 1e6, 2),
            })

        if not downloaded_items:
            for p in sorted(stage_dir.iterdir()):
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                    dest_path = target_dir / _sanitize_filename(p.stem, 90)
                    dest_path = dest_path.with_suffix(p.suffix.lower())
                    shutil.copyfile(p, dest_path)
                    st = dest_path.stat()
                    downloaded_items.append({
                        "source_url": raw_url,
                        "webpage_url": url,
                        "media_type": "video" if download_video else "audio",
                        "saved_to_drive": save_media_to_drive,
                        "drive_path": str(dest_path),
                        "rel_path": rel_to_mydrive(dest_path, MYDRIVE_ROOT) if save_media_to_drive else dest_path.name,
                        "name": dest_path.name,
                        "size_bytes": st.st_size,
                        "size_mb": round(st.st_size / 1e6, 2),
                    })

        if not downloaded_items:
            err = (res.stderr or res.stdout or "").strip()[-1200:]
            raise RuntimeError(f"yt-dlp did not produce any media files for {url}. Output: {err}")
        return downloaded_items
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        if cookie_file and cookie_file.is_file():
            cookie_file.unlink(missing_ok=True)


def _run_drive_transcribe_sync(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    t0 = time.monotonic()
    raw_path = (
        body.get("drive_path") or body.get("audio") or body.get("path") or body.get("url") or body.get("media_url") or ""
    ).strip()
    if not raw_path:
        return 400, {"error": "Missing 'drive_path' or 'url' (Google Drive audio file path or media URL)"}

    media_meta: dict[str, Any] | None = None
    save_media_to_drive = bool(body.get("save_media_to_drive", True))
    temp_media_path: Path | None = None
    try:
        if _is_media_url(raw_path):
            dl_items = _download_media_url_to_drive(
                raw_url=raw_path,
                download_dir=str(body.get("download_dir") or ""),
                audio_format=str(body.get("audio_format") or "mp3"),
                cookies_drive=str(body.get("cookies_drive") or ""),
                playlist=bool(body.get("playlist", False)),
                force_download=bool(body.get("force_download", False)),
                download_video=bool(body.get("download_video", False)),
                save_media_to_drive=save_media_to_drive,
            )
            media_meta = dl_items[0]
            audio_path = Path(media_meta["drive_path"])
            if not save_media_to_drive:
                temp_media_path = audio_path
        else:
            audio_path = _resolve_path(raw_path)
    except ValueError as err:
        return 400, {"error": str(err)}

    try:
        if not audio_path.is_file():
            return 404, {
                "error": f"Audio file not found on Google Drive: '{raw_path}' (resolved to '{audio_path}')",
                "resolved_path": str(audio_path),
                "drive_mounted": MYDRIVE_ROOT.is_dir(),
            }

        user_prompt, sys_prompt, reasoning, timestamps = build_prompt_and_settings(body)
        force_ffmpeg = bool(body.get("normalize_audio", False))

        total_duration = probe_audio_duration_sec(audio_path)
        raw_chunk_min = body.get("chunk_minutes")
        if raw_chunk_min is not None:
            max_chunk_sec = float(raw_chunk_min) * 60.0
        elif body.get("chunk_seconds") is not None:
            max_chunk_sec = float(body["chunk_seconds"])
        else:
            max_chunk_sec = 2400.0

        chunk_ranges = compute_chunk_ranges(audio_path, total_duration, max_chunk_sec=max_chunk_sec)
        is_multi_chunk = len(chunk_ranges) > 1

        extra_body: dict[str, Any] = {
            "chat_template_kwargs": {"enable_thinking": reasoning},
            "skip_special_tokens": bool(body.get("skip_special_tokens", False)),
            "spaces_between_special_tokens": False,
            "seed": int(body.get("seed", 0)),
        }
        if reasoning:
            extra_body.update(top_k=-1, thinking_token_budget=int(body.get("thinking_token_budget", 17408)))
            extra_body["chat_template_kwargs"]["reasoning_budget"] = int(body.get("reasoning_budget", 16384))
        if timestamps:
            extra_body["mm_processor_kwargs"] = {"capture_ctc_timestamps": True}

        temp = body.get("temperature")
        if temp is None:
            temp = 0.6 if reasoning else 0.0
        top_p = body.get("top_p")
        if top_p is None:
            top_p = 0.95 if reasoning else 1.0
        max_tokens = body.get("max_tokens")
        if max_tokens is None:
            max_tokens = 20480 if reasoning else 16384

        texts: list[str] = []
        reasonings: list[str] = []
        merged_words: list[dict[str, Any]] = []
        merged_segments: list[dict[str, Any]] = []
        merged_diarization: list[dict[str, Any]] = []
        last_ctc_ts: dict[str, Any] | None = None
        chunk_meta_list: list[dict[str, Any]] = []
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        for c_idx, (c_start, c_end) in enumerate(chunk_ranges, 1):
            c_t0 = time.monotonic()
            if is_multi_chunk:
                data_url = load_audio_as_data_url(audio_path, force_ffmpeg_16k=True, start_sec=c_start, end_sec=c_end)
            else:
                data_url = load_audio_as_data_url(audio_path, force_ffmpeg_16k=force_ffmpeg)

            messages: list[dict[str, Any]] = []
            if sys_prompt:
                messages.append({"role": "system", "content": sys_prompt})
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "audio_url", "audio_url": {"url": data_url}},
                ],
            })

            chat_payload = {
                "model": body.get("model") or MODEL_REPO,
                "messages": messages,
                "temperature": float(temp),
                "top_p": float(top_p),
                "max_tokens": int(max_tokens),
                **extra_body,
            }

            status_code, completion = _call_local_chat_completions(chat_payload)
            if status_code != 200:
                return status_code, completion

            choice = (completion.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            c_text = (msg.get("content") or "").strip()
            c_reasoning = msg.get("reasoning_content") or msg.get("reasoning") or None
            if not reasoning and body.get("cleanup_loops", True):
                c_text = cleanup_generation_loops(c_text)

            if c_text:
                if is_multi_chunk and reasoning:
                    texts.append(f"[{fmt_srt_time(c_start)[:8]} - {fmt_srt_time(c_end)[:8]}]\n{c_text}")
                else:
                    texts.append(c_text)
            if c_reasoning:
                if is_multi_chunk:
                    reasonings.append(f"[Chunk {c_idx}/{len(chunk_ranges)}: {fmt_srt_time(c_start)[:8]} - {fmt_srt_time(c_end)[:8]}]\n{c_reasoning}")
                else:
                    reasonings.append(str(c_reasoning))

            c_ctc = shift_ctc_timestamps(completion.get("ctc_timestamps"), c_start)
            if isinstance(c_ctc, dict):
                last_ctc_ts = c_ctc
                if isinstance(c_ctc.get("words"), list):
                    merged_words.extend(c_ctc["words"])
                if isinstance(c_ctc.get("segments"), list):
                    merged_segments.extend(c_ctc["segments"])
                if isinstance(c_ctc.get("diarization"), list):
                    merged_diarization.extend(c_ctc["diarization"])

            u = completion.get("usage")
            if isinstance(u, dict):
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    total_usage[k] += int(u.get(k) or 0)

            chunk_meta_list.append({
                "chunk": c_idx,
                "start_sec": round(c_start, 2),
                "end_sec": round(c_end, 2),
                "duration_sec": round(max(0.0, c_end - c_start), 2),
                "elapsed_sec": round(time.monotonic() - c_t0, 2),
            })

        text = ("\n\n" if is_multi_chunk else "").join(texts)
        reasoning_content = "\n\n".join(reasonings) if reasonings else None

        if last_ctc_ts is not None:
            ctc_ts: dict[str, Any] | None = dict(last_ctc_ts)
            if merged_words:
                ctc_ts["words"] = merged_words
            if merged_segments:
                ctc_ts["segments"] = merged_segments
            if merged_diarization:
                ctc_ts["diarization"] = merged_diarization
        else:
            ctc_ts = None

        srt_text = words_to_srt(merged_words) if merged_words else ""

        elapsed = round(time.monotonic() - t0, 2)
        result: dict[str, Any] = {
            "drive_path": str(audio_path),
            "rel_path": rel_to_mydrive(audio_path, MYDRIVE_ROOT),
            "source_url": media_meta["source_url"] if media_meta else None,
            "media_info": media_meta,
            "duration_sec": round(total_duration, 2) if total_duration > 0 else None,
            "chunk_count": len(chunk_ranges),
            "chunks": chunk_meta_list if is_multi_chunk else None,
            "prompt": user_prompt,
            "system_prompt": sys_prompt,
            "preset": body.get("preset") or None,
            "reasoning": reasoning,
            "timestamps": timestamps,
            "text": text,
            "reasoning_content": reasoning_content,
            "ctc_timestamps": ctc_ts,
            "srt": srt_text or None,
            "elapsed_sec": elapsed,
            "usage": total_usage if total_usage["total_tokens"] > 0 else None,
            "saved_drive_files": [],
        }

        save_to_drive = bool(body.get("save_to_drive", False))
        save_drive_dir = (body.get("save_drive_dir") or "").strip()
        if save_to_drive or save_drive_dir:
            if save_drive_dir:
                out_dir = _resolve_path(save_drive_dir, default_subdir=None)
            elif temp_media_path is not None:
                out_dir = DATA_DIR / "transcripts"
            else:
                out_dir = audio_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = audio_path.stem
            txt_file = out_dir / f"{stem}.txt"
            txt_file.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
            result["saved_drive_files"].append(str(txt_file))

            json_file = out_dir / f"{stem}.nemotron.json"
            json_file.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            result["saved_drive_files"].append(str(json_file))

            if srt_text:
                srt_file = out_dir / f"{stem}.srt"
                srt_file.write_text(srt_text, encoding="utf-8")
                result["saved_drive_files"].append(str(srt_file))

        if temp_media_path is not None:
            result["drive_path"] = None

        return 200, result
    except ValueError as err:
        return 400, {"error": str(err)}
    finally:
        if temp_media_path is not None:
            shutil.rmtree(temp_media_path.parent, ignore_errors=True)


def _rewrite_drive_urls_in_chat_body(raw_body: bytes) -> bytes | None:
    if (
        b"drive://" not in raw_body
        and b"gdrive://" not in raw_body
        and b"file://" not in raw_body
        and b"/content/" not in raw_body
        and not any(m.encode() in raw_body for m in YTDLP_DOMAIN_MARKERS)
    ):
        return None
    try:
        payload = json.loads(raw_body)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    changed = False
    for msg in payload.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "audio_url":
                continue
            aurl = item.get("audio_url")
            url_val = aurl.get("url") if isinstance(aurl, dict) else None
            if not isinstance(url_val, str):
                continue
            if url_val.startswith(("drive://", "gdrive://", "file://", "/content/")):
                ap = _resolve_path(url_val)
                aurl["url"] = load_audio_as_data_url(ap)
                changed = True
            elif any(m in url_val for m in YTDLP_DOMAIN_MARKERS):
                dl_items = _download_media_url_to_drive(url_val)
                ap = Path(dl_items[0]["drive_path"])
                aurl["url"] = load_audio_as_data_url(ap)
                changed = True
    if not changed:
        return None
    return json.dumps(payload).encode("utf-8")


async def nemotron_middleware(request: Any, call_next: Any) -> Any:
    path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))

    if not _check_auth(request):
        return JSONResponse(
            {"error": {"message": "Unauthorized: invalid or missing Bearer token", "type": "AuthenticationError", "code": 401}},
            status_code=HTTPStatus.UNAUTHORIZED,
        )

    if request.method == "GET" and path == "/api/ct/prompts":
        return JSONResponse({"presets": PRESETS, "default_preset": "verbatim"})

    if request.method == "GET" and path == "/api/ct/drive-list":
        spec = request.query_params.get("path", "")
        recursive = request.query_params.get("recursive", "1") not in ("0", "false", "False")
        try:
            files = await asyncio.to_thread(
                list_drive_audio_files,
                spec,
                data_dir=DATA_DIR,
                default_subdir="audio",
                recursive=recursive,
                mydrive_root=MYDRIVE_ROOT,
                drive_root=DRIVE_ROOT,
            )
            return JSONResponse({
                "drive_mounted": MYDRIVE_ROOT.is_dir(),
                "query": spec,
                "count": len(files),
                "files": files,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    if request.method == "POST" and path == "/api/ct/ytdlp-download":
        try:
            body = json.loads(await request.body())
            raw_url = (body.get("url") or body.get("media_url") or body.get("audio") or "").strip()
            if not raw_url:
                return JSONResponse({"error": "Missing 'url'"}, status_code=400)
            items = await asyncio.to_thread(
                _download_media_url_to_drive,
                raw_url=raw_url,
                download_dir=str(body.get("download_dir") or ""),
                audio_format=str(body.get("audio_format") or "mp3"),
                cookies_drive=str(body.get("cookies_drive") or ""),
                playlist=bool(body.get("playlist", False)),
                force_download=bool(body.get("force_download", False)),
                download_video=bool(body.get("download_video", False)),
                save_media_to_drive=bool(body.get("save_media_to_drive", True)),
            )
            return JSONResponse({"count": len(items), "files": items})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    if request.method == "POST" and path == "/api/ct/drive-transcribe":
        try:
            body = json.loads(await request.body())
            code, res = await asyncio.to_thread(_run_drive_transcribe_sync, body)
            return JSONResponse(res, status_code=code)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    if request.method == "POST" and path == "/v1/chat/completions":
        raw_body = await request.body()
        try:
            rewritten = await asyncio.to_thread(_rewrite_drive_urls_in_chat_body, raw_body)
        except ValueError as e:
            return JSONResponse(
                {"error": {"message": str(e), "type": "BadRequestError", "code": 400}},
                status_code=400,
            )
        if rewritten is not None:
            code, res = await asyncio.to_thread(_call_local_chat_completions, json.loads(rewritten))
            return JSONResponse(res, status_code=code)
        return await ctc_timestamp_middleware(request, call_next)

    return await call_next(request)
