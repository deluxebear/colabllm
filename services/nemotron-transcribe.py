#!/usr/bin/env python3
"""NVIDIA-Nemotron-3.5-Transcribe-30B-A4B on a Colab G4 GPU (notebook: nemotron-transcribe.ipynb).

Serves `nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B` (BF16 reference weights by default; optional FP8 / NVFP4)
via `vllm serve` (0.32.0) + `NVIDIA-NeMo/Speech` (`VLLM_PLUGINS=nemo_speechlm` + `ctc_timestamp_middleware`)
on a Colab G4 GPU (NVIDIA RTX PRO 6000 Blackwell, ~96 GB VRAM), exposed through Cloudflare Tunnel with
Bearer token authentication (`auth="builtin"`, default fixed domain `https://transcribe.example.com`).

Capabilities:
  1. Remote OpenAI-compatible API (`/v1/models`, `/v1/chat/completions`):
     - Accepts standard `data:audio/<ext>;base64,...` or `https://...` audio URLs, plus direct Google Drive
       paths (`drive://path/to/audio.wav` or `/content/drive/MyDrive/path/to/audio.wav`).
     - Supports custom instructions (`prompt` / `system_prompt`), reasoning mode (`enable_thinking=True`),
       and CTC word-level timestamps + speaker diarization (`mm_processor_kwargs={"capture_ctc_timestamps": True}`).
  2. Remote Google Drive endpoints (`/api/ct/drive-list`, `/api/ct/drive-transcribe`, `/api/ct/prompts`):
     - Reads audio files directly from Google Drive inside the Colab VM (avoiding local upload & Cloudflare's
       100 MB body limit even for 1-hour recordings) and optionally writes `.txt` / `.srt` / `.nemotron.json`
       back to Google Drive.
  3. CLI single & batch execution (`transcribe` / `run`, `ls-drive`, `prompts`):
     - Specify one or multiple Google Drive audio files, directories, globs (`meetings/*.wav`), or a batch
       manifest (`--batch-file tasks.jsonl` / `.csv` / `.txt` with per-file prompts), or local audio files.
     - Supports concurrent batch execution (`-j / --concurrency N`), custom `--prompt` or NVIDIA `--preset`,
       word timestamps (`--timestamps`), reasoning (`--reasoning`), saving to Google Drive (`--save-drive`),
       and saving locally (`--output` / `--output-dir`).

Usage:
  # 1. Lifecycle
  ./services/nemotron-transcribe.py up --domain                  # https://transcribe.example.com (G4, BF16)
  ./services/nemotron-transcribe.py up --domain --precision fp8  # optional FP8 / NVFP4 variant
  ./services/nemotron-transcribe.py info | logs -f | status | down [--delete-tunnel]

  # 2. Browse audio files on Google Drive & inspect prompt presets
  ./services/nemotron-transcribe.py ls-drive [MyDrive/path/or/*.wav]
  ./services/nemotron-transcribe.py prompts

  # 3. Single or batch transcription / audio QA on Google Drive (or local files)
  ./services/nemotron-transcribe.py transcribe "audio/sample.wav" --prompt "Transcribe the following audio."
  ./services/nemotron-transcribe.py transcribe "meetings/*.mp3" --preset speaker --timestamps --save-drive -O ./out -j 2
  ./services/nemotron-transcribe.py transcribe --batch-file tasks.jsonl --save-drive -O ./out
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

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
    warn,
)

PRECISION_REPOS = {
    "bf16": "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B",
    "fp8": "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-FP8",
    "nvfp4": "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-NVFP4",
}

LOCAL_PRESETS: dict[str, dict[str, Any]] = {
    "verbatim": {
        "description": "逐字转写（保留语气词、重复、标点与大小写，多语种通用默认推荐）",
        "user_prompt": (
            "Audio transcription. Language: as spoken. Wording: verbatim, including fillers and repetitions. "
            "Formatting: punctuation and capitalization. Output: transcript only."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "clean": {
        "description": "整洁书面转写（去除口头禅与无意重复，保留原意与标点大小写）",
        "user_prompt": (
            "Audio transcription. Language: as spoken. Wording: omit fillers and accidental repetitions; "
            "keep the remaining spoken wording unchanged. Formatting: punctuation and capitalization. "
            "Output: transcript only."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "speaker": {
        "description": "说话人分离转写 + 词级时间戳（带 <spk:0> <spk:1> 标签，自动开启 timestamps）",
        "user_prompt": (
            "Transcribe this audio with punctuation and capitalization. Include all spoken words and verbal fillers. "
            "Write numbers in their standard written form. Put speaker tags <spk:0> <spk:1> <spk:2> <spk:3> on the transcription."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": True,
    },
    "multispeaker": {
        "description": "多说话人纯文本转写（无标点小写，每轮讲话前标注 <spk:N>）",
        "user_prompt": (
            "Transcribe this audio. Include all spoken words and verbal fillers. Write numbers as spoken. "
            "Use no punctuation and no capitalization. Start every speaker turn with a speaker tag such as "
            "<spk:0>, <spk:1>, <spk:2>, etc.; assign one stable tag per speaker and keep speaker identities "
            "consistent. Output only the speaker-tagged transcript."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "rich": {
        "description": "富文本转写（包含非语音事件 <ev:*> 与说话风格 <sty:*> 标签）",
        "user_prompt": (
            "Transcribe this audio with punctuation and capitalization. Include all spoken words and verbal fillers. "
            "Write numbers in their standard written form. Use <ev:*> and <sty:*> special tokens to recognize all "
            "non-speech verbatims and speaking style."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "near-field": {
        "description": "仅转写距离麦克风最近的主讲人（过滤远场背景人声）",
        "user_prompt": (
            "Transcribe this audio. Include all spoken words and verbal fillers. Write numbers as spoken. "
            "No punctuation or capitalization. Transcribe only the speaker nearest to the microphone. "
            "Do not transcribe any other voices."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "structured": {
        "description": "结构化内容转写（精确保留邮箱、URL、文件路径、命令、编号与标点分隔符）",
        "user_prompt": (
            "Transcribe as a clean written transcript. Preserve exact values and punctuation/separators in emails, "
            "URLs, file paths, commands, and identifiers. Use conventional written forms."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "context": {
        "description": "带领域词汇/专有名词提示的上下文偏置转写（配合 --context 参数）",
        "user_prompt": "Transcribe with punctuation and capitalization. Context terms: {context}.",
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "translate": {
        "description": "语音直接翻译为目标语言文本（配合 --lang 参数，如 Chinese / English / Japanese）",
        "user_prompt": "Please translate the given speech to {lang}.",
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "lid": {
        "description": "语种识别（输出录音对应的语言标签）",
        "user_prompt": (
            "Identify the language spoken in this audio. Respond with exactly one of the following locale labels "
            "and nothing else: ar_eg, bg_bg, cmn_hans_cn, hr_hr, cs_cz, da_dk, nl_nl, en_us, et_ee, fil_ph, fi_fi, "
            "fr_fr, de_de, el_gr, he_il, hi_in, hu_hu, id_id, it_it, ja_jp, ko_kr, lv_lv, lt_lt, mt_mt, pl_pl, "
            "pt_br, ro_ro, ru_ru, sk_sk, sl_si, es_419, sv_se, th_th, tr_tr, uk_ua, vi_vn."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "qa": {
        "description": "音频理解与问答（自动开启推理模式 --reasoning，根据录音内容回答问题）",
        "user_prompt": "What is happening in this audio?",
        "system_prompt": (
            "Answer the question directly using evidence from every provided audio clip in order. "
            "Answer every requested part, preserve exact names and numbers, and give a concise, specific answer first. "
            "Do not invent details."
        ),
        "reasoning": True,
        "timestamps": False,
    },
}

AUDIO_EXTS = (
    ".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac", ".opus", ".webm", ".mp4", ".mkv", ".wma", ".mpga", ".mpeg",
)
MIME_BY_EXT = {
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    ".mp4": "audio/mp4",
}

CHAR_RUN = re.compile(r"(.)\1{20,}", re.DOTALL)
BLOCK_RUN = re.compile(r"(.{1,4096}?)\1{19,}", re.DOTALL)

SERVICE = Service(
    name="nemotron-transcribe",
    notebook="nemotron-transcribe.ipynb",
    service_port=8000,
    auth="builtin",                   # vLLM --api-key + ct_nemotron_middleware enforce Bearer token auth
    public_port=8000,
    gpu="G4",                         # Colab G4 (RTX PRO 6000 Blackwell ~96 GB VRAM), without --high-mem
    high_mem=False,
    default_hostname="transcribe.example.com",
    ready_timeout=3600,
    env={
        "CT_NEMOTRON_PRECISION": "bf16",
        "CT_NEMOTRON_REPO": "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B",
        "CT_NEMOTRON_ALIAS": "nemotron-transcribe",
        "CT_NEMOTRON_MTP": "8",
        "CT_NEMOTRON_MAX_MODEL_LEN": "65536",
    },
)


def _extract_up_extra_args() -> None:
    """Allow `--precision bf16|fp8|nvfp4`, `--repo <hf_repo>`, and `--mtp <N>` on `up`."""
    argv = sys.argv[1:]
    cleaned = [sys.argv[0]]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--precision" and i + 1 < len(argv):
            prec = argv[i + 1].strip().lower()
            if prec not in PRECISION_REPOS:
                die(f"unsupported --precision '{prec}'; choose from: {', '.join(PRECISION_REPOS)}")
            SERVICE.env["CT_NEMOTRON_PRECISION"] = prec
            SERVICE.env["CT_NEMOTRON_REPO"] = PRECISION_REPOS[prec]
            i += 2
        elif a.startswith("--precision="):
            prec = a.split("=", 1)[1].strip().lower()
            if prec not in PRECISION_REPOS:
                die(f"unsupported --precision '{prec}'; choose from: {', '.join(PRECISION_REPOS)}")
            SERVICE.env["CT_NEMOTRON_PRECISION"] = prec
            SERVICE.env["CT_NEMOTRON_REPO"] = PRECISION_REPOS[prec]
            i += 1
        elif a == "--repo" and i + 1 < len(argv):
            SERVICE.env["CT_NEMOTRON_REPO"] = argv[i + 1].strip()
            i += 2
        elif a.startswith("--repo="):
            SERVICE.env["CT_NEMOTRON_REPO"] = a.split("=", 1)[1].strip()
            i += 1
        elif a == "--mtp" and i + 1 < len(argv):
            SERVICE.env["CT_NEMOTRON_MTP"] = str(int(argv[i + 1]))
            i += 2
        elif a.startswith("--mtp="):
            SERVICE.env["CT_NEMOTRON_MTP"] = str(int(a.split("=", 1)[1]))
            i += 1
        else:
            cleaned.append(a)
            i += 1
    sys.argv = cleaned


def _get_endpoint(url_override: str | None = None, token_override: str | None = None) -> tuple[str, str]:
    st = State(SERVICE).load()
    url = (url_override or os.environ.get("NEMOTRON_URL") or st.get("url") or "").rstrip("/")
    token = (token_override or os.environ.get("NEMOTRON_API_TOKEN") or st.get("api_token") or "").strip()
    if not url or not token:
        die(
            f"service URL or API token not found in .state/{SERVICE.name}/state.json.\n"
            f"Start the service first (`./services/nemotron-transcribe.py up --domain` or `-i <id>`) "
            f"or pass `--url` and `--api-token`."
        )
    return url, token


def _get_endpoint_pool(cli_args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """Return list of (instance_name, base_url, api_token) for single or multi-instance load balancing."""
    if cli_args.url or cli_args.api_token:
        u, t = _get_endpoint(cli_args.url, cli_args.api_token)
        return [(SERVICE.name, u, t)]

    base_name = SERVICE.base_name or "nemotron-transcribe"
    all_items = list_service_instances(base_name)

    if getattr(cli_args, "all_instances", False):
        pool = [
            (it["name"], it["url"].rstrip("/"), it["api_token"])
            for it in all_items
            if it.get("ready") and it.get("url") and it.get("api_token")
        ]
        if not pool:
            pool = [
                (it["name"], it["url"].rstrip("/"), it["api_token"])
                for it in all_items
                if it.get("alive") and it.get("url") and it.get("api_token")
            ]
        if not pool:
            die(f"未在 .state/ 中找到任何运行中的 {base_name} 实例；请先执行 `up` 启动至少一个实例。")
        return pool

    if getattr(cli_args, "instances", None):
        wanted = [x.strip() for x in cli_args.instances.split(",") if x.strip()]
        by_id = {it["instance"]: it for it in all_items}
        by_arg = {it["instance_arg"]: it for it in all_items if it["instance_arg"]}
        by_name = {it["name"]: it for it in all_items}
        pool = []
        for w in wanted:
            it = by_id.get(w) or by_arg.get(w) or by_name.get(w) or by_name.get(f"{base_name}-{w}")
            if not it or not it.get("url") or not it.get("api_token"):
                die(f"未找到可用实例 '{w}'（请通过 `./services/nemotron-transcribe.py ps` 查看已启动实例）")
            pool.append((it["name"], it["url"].rstrip("/"), it["api_token"]))
        return pool

    u, t = _get_endpoint()
    return [(SERVICE.name, u, t)]


def _http_request(url: str, token: str, payload: dict[str, Any] | None = None, timeout: int = 1800) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) NemotronTranscribeCLI/1.0",
    }
    data = None
    method = "GET"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            err_obj = json.loads(raw)
        except Exception:
            err_obj = raw
        raise RuntimeError(f"HTTP {e.code} from {url}: {err_obj}") from e


def _cleanup_loops(text: str) -> str:
    if not text:
        return text
    text = CHAR_RUN.sub(lambda m: m.group(1), text)
    return BLOCK_RUN.sub(lambda m: m.group(1), text)


def _fmt_srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_to_srt(words: list[dict[str, Any]]) -> str:
    if not words:
        return ""
    segments = []
    cur_words: list[str] = []
    cur_start = 0.0
    cur_end = 0.0
    cur_spk = None

    def flush() -> None:
        nonlocal cur_words, cur_start, cur_end, cur_spk
        if not cur_words:
            return
        prefix = f"[{cur_spk}] " if cur_spk else ""
        text = prefix + " ".join(cur_words).strip()
        segments.append((cur_start, max(cur_end, cur_start + 0.2), text))
        cur_words = []

    for w in words:
        w_text = str(w.get("word") or "").strip()
        if not w_text:
            continue
        w_start = float(w.get("start") or 0.0)
        w_end = float(w.get("end") or w_start)
        w_spk = w.get("speaker")
        if not cur_words:
            cur_start = w_start
            cur_end = w_end
            cur_spk = w_spk
            cur_words.append(w_text)
            continue
        gap = w_start - cur_end
        if (w_spk != cur_spk) or gap > 1.2 or len(cur_words) >= 14 or (
            len(cur_words) >= 6 and cur_words[-1].endswith((".", "?", "!", "。", "？", "！"))
        ):
            flush()
            cur_start = w_start
            cur_end = w_end
            cur_spk = w_spk
            cur_words.append(w_text)
        else:
            cur_end = max(cur_end, w_end)
            cur_words.append(w_text)
    flush()
    return "\n".join(
        f"{idx}\n{_fmt_srt_time(st_t)} --> {_fmt_srt_time(ed_t)}\n{txt}\n"
        for idx, (st_t, ed_t, txt) in enumerate(segments, 1)
    )


def _cmd_prompts() -> None:
    print("\n内置 NVIDIA Nemotron 3.5 Transcribe 推荐 Prompt 预设 (--preset <name>):\n")
    for name, info in LOCAL_PRESETS.items():
        flags = []
        if info["reasoning"]:
            flags.append("reasoning=True")
        if info["timestamps"]:
            flags.append("timestamps=True")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  * {name:<14} {info['description']}{flag_str}")
        print(f"    user_prompt  : {info['user_prompt']}")
        if info["system_prompt"]:
            print(f"    system_prompt: {info['system_prompt']}")
        print()


SKIP_DRIVE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "custom_nodes",
    "models", ".runtimes", "Takeout", ".Trash", ".ipynb_checkpoints",
}


def _normalize_drive_dir(raw_spec: str) -> str:
    spec = (raw_spec or "").strip()
    for pfx in ("drive://", "gdrive://", "file://"):
        if spec.startswith(pfx):
            spec = spec[len(pfx):]
    spec = spec.strip("/")
    if not spec or spec == "MyDrive":
        return "/content/drive/MyDrive"
    if spec.startswith("content/drive/"):
        return "/" + spec
    if spec.startswith("drive/"):
        return "/content/" + spec
    if spec.startswith("MyDrive/"):
        return "/content/drive/" + spec
    return f"/content/drive/MyDrive/{spec}"


def _colab_ls_single(session_name: str, remote_path: str) -> tuple[list[str], list[str]]:
    """Use `colab ls -s <session> <remote_path>` directly (no HTTP tunnel roundtrip)."""
    res = subprocess.run(
        ["colab", "ls", "-s", session_name, remote_path],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(f"`colab ls -s {session_name} {remote_path}` failed: {err}")
    dirs: list[str] = []
    files: list[str] = []
    for line in res.stdout.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("[colab]"):
            continue
        if entry.endswith("/"):
            dirs.append(entry[:-1])
        else:
            files.append(entry)
    return sorted(dirs), sorted(files)


def _list_drive_via_colab_cli(
    session_name: str,
    raw_spec: str,
    recursive: bool = False,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    """List directories and audio files on the mounted Colab Drive via `colab ls`."""
    has_glob = any(ch in (raw_spec or "") for ch in ("*", "?", "["))
    if has_glob:
        full_pattern = _normalize_drive_dir(raw_spec)
        parent_dir = str(Path(full_pattern).parent)
        name_pat = Path(full_pattern).name
        dirs, files = _colab_ls_single(session_name, parent_dir)
        import fnmatch
        matched_audio = []
        for f in files:
            if fnmatch.fnmatch(f, name_pat) and Path(f).suffix.lower() in AUDIO_EXTS:
                dp = f"{parent_dir.rstrip('/')}/{f}"
                rel = dp.removeprefix("/content/drive/MyDrive/")
                matched_audio.append({"drive_path": dp, "rel_path": rel, "name": f})
        return parent_dir, [], matched_audio

    base_dir = _normalize_drive_dir(raw_spec)
    if Path(base_dir).suffix.lower() in AUDIO_EXTS:
        rel = base_dir.removeprefix("/content/drive/MyDrive/")
        return str(Path(base_dir).parent), [], [{"drive_path": base_dir, "rel_path": rel, "name": Path(base_dir).name}]

    top_dirs, top_files = _colab_ls_single(session_name, base_dir)
    audio_items: list[dict[str, Any]] = []
    for f in top_files:
        if Path(f).suffix.lower() in AUDIO_EXTS:
            dp = f"{base_dir.rstrip('/')}/{f}"
            rel = dp.removeprefix("/content/drive/MyDrive/")
            audio_items.append({"drive_path": dp, "rel_path": rel, "name": f})

    if recursive:
        queue = [f"{base_dir.rstrip('/')}/{d}" for d in top_dirs if d not in SKIP_DRIVE_DIRS and not d.startswith(".")]
        while queue:
            cur = queue.pop(0)
            try:
                sub_dirs, sub_files = _colab_ls_single(session_name, cur)
            except Exception:
                continue
            for f in sub_files:
                if Path(f).suffix.lower() in AUDIO_EXTS:
                    dp = f"{cur.rstrip('/')}/{f}"
                    rel = dp.removeprefix("/content/drive/MyDrive/")
                    audio_items.append({"drive_path": dp, "rel_path": rel, "name": f})
            for d in sub_dirs:
                if d not in SKIP_DRIVE_DIRS and not d.startswith("."):
                    queue.append(f"{cur.rstrip('/')}/{d}")

    return base_dir, top_dirs, audio_items


def _cmd_ls_drive(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} ls-drive",
        description="直接通过 Colab 已挂载的 /content/drive 查询网盘目录与音频文件（无需走公网 HTTP 隧道）",
    )
    p.add_argument("path", nargs="?", default="", help="网盘相对路径、目录或通配符（如 'vidio' 或 'meetings/*.wav'，留空默认 MyDrive 根目录）")
    p.add_argument("-r", "--recursive", action="store_true", default=False, help="递归搜索子目录中的音频文件（默认仅列出当前目录，避免全盘扫描超时）")
    p.add_argument("--no-recursive", dest="recursive", action="store_false", help="仅列出当前层级目录")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    a = p.parse_args(argv)

    base_dir, sub_dirs, audio_files = _list_drive_via_colab_cli(SERVICE.name, a.path, recursive=a.recursive)
    if a.json:
        print(json.dumps({
            "session": SERVICE.name,
            "base_dir": base_dir,
            "directories": sub_dirs,
            "audio_files": audio_files,
        }, indent=2, ensure_ascii=False))
        return
    log(f"会话 [{SERVICE.name}] 网盘目录: {base_dir}")
    if sub_dirs and not a.recursive:
        print(f"\n  子目录 ({len(sub_dirs)} 个):")
        for d in sub_dirs:
            print(f"    📁 {d}/")
    print(f"\n  音频文件 ({len(audio_files)} 个):")
    if not audio_files:
        print("    (当前层级无音频文件；可指定子目录如 `ls-drive vidio` 或加 `-r` 递归搜索子目录)")
    else:
        for item in audio_files:
            print(f"    🎵 {item['rel_path']}   ({item['drive_path']})")
    print()


def _resolve_task_prompt(task: dict[str, Any], cli_args: argparse.Namespace) -> dict[str, Any]:
    preset_name = (task.get("preset") or cli_args.preset or "").strip().lower()
    preset = LOCAL_PRESETS.get(preset_name) if preset_name else None

    prompt = task.get("prompt")
    if prompt is None:
        prompt = cli_args.prompt
    if prompt is None or str(prompt).strip() == "":
        prompt = preset["user_prompt"] if preset else LOCAL_PRESETS["verbatim"]["user_prompt"]
    prompt = str(prompt)

    ctx = (task.get("context") or cli_args.context or "").strip()
    lang = (task.get("lang") or task.get("language") or cli_args.lang or "Chinese").strip()
    if "{context}" in prompt:
        prompt = prompt.replace("{context}", ctx or "general")
    elif ctx and "Context terms:" not in prompt:
        prompt = f"{prompt.rstrip()} Context terms: {ctx}."
    if "{lang}" in prompt:
        prompt = prompt.replace("{lang}", lang)

    sys_prompt = task.get("system_prompt")
    if sys_prompt is None:
        sys_prompt = cli_args.system_prompt
    if sys_prompt is None:
        sys_prompt = preset["system_prompt"] if preset else ""

    reasoning = task.get("reasoning")
    if reasoning is None:
        reasoning = cli_args.reasoning
    if reasoning is None:
        reasoning = bool(preset["reasoning"]) if preset else False
    else:
        reasoning = bool(reasoning)

    timestamps = task.get("timestamps")
    if timestamps is None:
        timestamps = cli_args.timestamps
    if timestamps is None:
        timestamps = bool(preset["timestamps"]) if preset else False
    else:
        timestamps = bool(timestamps)

    return {
        "prompt": prompt,
        "system_prompt": str(sys_prompt),
        "preset": preset_name or None,
        "context": ctx or None,
        "lang": lang,
        "reasoning": reasoning,
        "timestamps": timestamps,
        "temperature": task.get("temperature", cli_args.temperature),
        "max_tokens": task.get("max_tokens", cli_args.max_tokens),
    }


def _load_batch_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    if not manifest_path.is_file():
        die(f"batch file not found: {manifest_path}")
    ext = manifest_path.suffix.lower()
    raw = manifest_path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    tasks: list[dict[str, Any]] = []
    if ext == ".jsonl":
        for idx, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            obj = json.loads(line)
            if isinstance(obj, str):
                tasks.append({"audio": obj})
            elif isinstance(obj, dict):
                tasks.append(obj)
            else:
                die(f"invalid JSONL entry at {manifest_path}:{idx}")
    elif ext == ".json":
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            data = data["tasks"]
        if not isinstance(data, list):
            die(f"expected a JSON list in {manifest_path}")
        for item in data:
            tasks.append({"audio": item} if isinstance(item, str) else dict(item))
    elif ext in (".csv", ".tsv"):
        delim = "\t" if ext == ".tsv" else ","
        reader = csv.DictReader(raw.splitlines(), delimiter=delim)
        for row in reader:
            cleaned = {k.strip(): v.strip() for k, v in row.items() if k and v is not None and v.strip() != ""}
            if "reasoning" in cleaned:
                cleaned["reasoning"] = cleaned["reasoning"].lower() in ("1", "true", "yes")
            if "timestamps" in cleaned:
                cleaned["timestamps"] = cleaned["timestamps"].lower() in ("1", "true", "yes")
            tasks.append(cleaned)
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                parts = line.split("\t", 1)
                tasks.append({"audio": parts[0].strip(), "prompt": parts[1].strip()})
            else:
                tasks.append({"audio": line})
    return tasks


def _expand_tasks(cli_args: argparse.Namespace, base_url: str, token: str) -> list[dict[str, Any]]:
    raw_items: list[dict[str, Any]] = []
    for p in cli_args.paths:
        raw_items.append({"audio": p})
    if cli_args.batch_file:
        raw_items.extend(_load_batch_manifest(Path(cli_args.batch_file)))
    if not raw_items:
        die("请指定至少一个网盘/本地音频路径，或使用 `--batch-file <文件>` 提供批量任务清单。")

    expanded: list[dict[str, Any]] = []
    for item in raw_items:
        spec = (item.get("audio") or item.get("drive_path") or item.get("path") or item.get("file") or "").strip()
        if not spec:
            die(f"batch item is missing 'audio' / 'drive_path': {item}")

        is_local = False
        if cli_args.local and not spec.startswith(("drive://", "gdrive://", "/content/drive/")):
            is_local = True
        elif not cli_args.drive and not spec.startswith(("drive://", "gdrive://", "/content/drive/")):
            local_candidate = Path(spec)
            if local_candidate.exists():
                is_local = True

        if is_local:
            lp = Path(spec)
            if lp.is_file():
                expanded.append({**item, "source_type": "local", "local_path": str(lp.resolve()), "label": lp.name})
            elif lp.is_dir():
                it = lp.rglob("*") if cli_args.recursive else lp.glob("*")
                matched = [f.resolve() for f in sorted(it) if f.is_file() and f.suffix.lower() in AUDIO_EXTS]
                if not matched:
                    warn(f"本地目录未找到支持的音频文件: {lp}")
                for f in matched:
                    expanded.append({**item, "source_type": "local", "local_path": str(f), "label": str(f.relative_to(lp.resolve()))})
            else:
                die(f"local path not found: {lp}")
        else:
            # Check if it's a directory or glob on Google Drive via /api/ct/drive-list
            clean_spec = spec
            for pfx in ("drive://", "gdrive://"):
                if clean_spec.startswith(pfx):
                    clean_spec = clean_spec[len(pfx):]
            needs_list = any(ch in clean_spec for ch in ("*", "?", "[")) or clean_spec.endswith("/") or (
                Path(clean_spec).suffix.lower() not in AUDIO_EXTS
            )
            if needs_list:
                try:
                    _, _, files = _list_drive_via_colab_cli(SERVICE.name, spec, recursive=cli_args.recursive)
                except Exception:
                    qs = urllib.parse.urlencode({"path": spec, "recursive": "1" if cli_args.recursive else "0"})
                    res = _http_request(f"{base_url}/api/ct/drive-list?{qs}", token, timeout=60)
                    files = res.get("files") or []
                if not files:
                    die(f"在 Google Drive 上未找到匹配的音频文件: '{spec}'")
                for f in files:
                    expanded.append({
                        **item,
                        "source_type": "drive",
                        "drive_path": f["drive_path"],
                        "label": f.get("rel_path") or f["name"],
                    })
            else:
                expanded.append({
                    **item,
                    "source_type": "drive",
                    "drive_path": spec,
                    "label": clean_spec,
                })
    return expanded


def _run_one_task(
    idx: int,
    total: int,
    task: dict[str, Any],
    cli_args: argparse.Namespace,
    base_url: str,
    token: str,
) -> dict[str, Any]:
    cfg = _resolve_task_prompt(task, cli_args)
    label = task.get("id") or task.get("label") or task.get("drive_path") or task.get("local_path") or f"task-{idx}"
    t0 = time.monotonic()

    if task["source_type"] == "drive":
        body: dict[str, Any] = {
            "drive_path": task["drive_path"],
            "prompt": cfg["prompt"],
            "system_prompt": cfg["system_prompt"],
            "preset": cfg["preset"],
            "reasoning": cfg["reasoning"],
            "timestamps": cfg["timestamps"],
            "normalize_audio": cli_args.normalize_audio,
            "save_to_drive": bool(cli_args.save_drive is not None),
            "save_drive_dir": (
                cli_args.save_drive if isinstance(cli_args.save_drive, str) and cli_args.save_drive != "@same" else ""
            ),
        }
        if cfg["temperature"] is not None:
            body["temperature"] = cfg["temperature"]
        if cfg["max_tokens"] is not None:
            body["max_tokens"] = cfg["max_tokens"]
        res = _http_request(f"{base_url}/api/ct/drive-transcribe", token, payload=body, timeout=cli_args.timeout)
        res["index"] = idx
        res["label"] = label
        return res

    # Local file -> call /v1/chat/completions
    lp = Path(task["local_path"])
    mime = MIME_BY_EXT.get(lp.suffix.lower(), "audio/wav")
    b64 = base64.b64encode(lp.read_bytes()).decode("ascii")
    messages: list[dict[str, Any]] = []
    if cfg["system_prompt"]:
        messages.append({"role": "system", "content": cfg["system_prompt"]})
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": cfg["prompt"]},
            {"type": "audio_url", "audio_url": {"url": f"data:{mime};base64,{b64}"}},
        ],
    })
    reasoning = cfg["reasoning"]
    timestamps = cfg["timestamps"]
    extra_body: dict[str, Any] = {
        "chat_template_kwargs": {"enable_thinking": reasoning},
        "skip_special_tokens": False,
        "spaces_between_special_tokens": False,
        "seed": 0,
    }
    if reasoning:
        extra_body.update(top_k=-1, thinking_token_budget=17408)
        extra_body["chat_template_kwargs"]["reasoning_budget"] = 16384
    if timestamps:
        extra_body["mm_processor_kwargs"] = {"capture_ctc_timestamps": True}

    payload = {
        "model": SERVICE.env["CT_NEMOTRON_REPO"],
        "messages": messages,
        "temperature": float(cfg["temperature"]) if cfg["temperature"] is not None else (0.6 if reasoning else 0.0),
        "top_p": 0.95 if reasoning else 1.0,
        "max_tokens": int(cfg["max_tokens"]) if cfg["max_tokens"] is not None else (20480 if reasoning else 16384),
        **extra_body,
    }
    completion = _http_request(f"{base_url}/v1/chat/completions", token, payload=payload, timeout=cli_args.timeout)
    choice = (completion.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    if not reasoning:
        text = _cleanup_loops(text)
    ctc_ts = completion.get("ctc_timestamps")
    words = (ctc_ts or {}).get("words") or [] if isinstance(ctc_ts, dict) else []
    srt_text = _words_to_srt(words) if words else None
    return {
        "index": idx,
        "label": label,
        "local_path": str(lp),
        "prompt": cfg["prompt"],
        "system_prompt": cfg["system_prompt"],
        "preset": cfg["preset"],
        "reasoning": reasoning,
        "timestamps": timestamps,
        "text": text,
        "reasoning_content": msg.get("reasoning_content") or msg.get("reasoning"),
        "ctc_timestamps": ctc_ts,
        "srt": srt_text,
        "elapsed_sec": round(time.monotonic() - t0, 2),
        "usage": completion.get("usage"),
    }


def _save_local_result(res: dict[str, Any], out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_name = Path(res.get("label") or res.get("drive_path") or res.get("local_path") or f"item-{res['index']}").stem
    safe_stem = re.sub(r"[^A-Za-z0-9._-\u4e00-\u9fff]+", "_", raw_name).strip("_") or f"item_{res['index']}"
    saved = []

    txt_path = out_dir / f"{safe_stem}.txt"
    text = res.get("text") or ""
    txt_path.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
    saved.append(str(txt_path))

    json_path = out_dir / f"{safe_stem}.nemotron.json"
    json_path.write_text(json.dumps(res, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    saved.append(str(json_path))

    if res.get("srt"):
        srt_path = out_dir / f"{safe_stem}.srt"
        srt_path.write_text(res["srt"], encoding="utf-8")
        saved.append(str(srt_path))
    return saved


def _cmd_transcribe(argv: list[str]) -> None:
    p = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} transcribe",
        description="调用 Colab G4 上的 Nemotron-3.5-Transcribe-30B-A4B 处理 Google Drive 网盘（或本地）单个/批量音频文件与自定义 Prompt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "paths",
        nargs="*",
        metavar="AUDIO_PATH",
        help="Google Drive 网盘上的音频文件路径、目录或通配符（如 'meetings/a.wav', 'meetings/*.mp3', 'drive://audio/'），或本地路径",
    )
    p.add_argument("-p", "--prompt", help="自定义用户指令 Prompt（未指定时默认使用 --preset 对应的官方推荐 Prompt）")
    p.add_argument("--prompt-file", help="从本地文本文件读取用户 Prompt")
    p.add_argument(
        "--preset",
        choices=list(LOCAL_PRESETS.keys()),
        help="使用 NVIDIA 官方任务预设模板 (verbatim|clean|speaker|multispeaker|rich|near-field|structured|context|translate|lid|qa)",
    )
    p.add_argument("--context", help="领域词汇 / 人名术语提示（用于上下文偏置转写 Context terms）")
    p.add_argument("--lang", default="Chinese", help="当 --preset translate 时的目标翻译语言（默认: Chinese）")
    p.add_argument("-s", "--system-prompt", help="自定义 System Prompt（转写/翻译建议留空；音频问答/助手任务可指定）")
    p.add_argument("--reasoning", dest="reasoning", action="store_true", default=None, help="开启推理思考模式 (enable_thinking=True)")
    p.add_argument("--no-reasoning", dest="reasoning", action="store_false", help="关闭推理思考模式 (enable_thinking=False)")
    p.add_argument("-t", "--timestamps", action="store_true", default=None, help="开启 CTC 词级时间戳与说话人分离对齐 (同时生成 .srt 字幕)")
    p.add_argument("-b", "--batch-file", metavar="FILE", help="批量任务清单文件 (.jsonl / .json / .csv / .tsv / .txt，每行可含独立 audio 与 prompt)")
    p.add_argument("-r", "--recursive", action="store_true", default=False, help="传入目录时递归查找子目录中的音频（默认仅当前目录，加 -r 递归）")
    p.add_argument("--no-recursive", dest="recursive", action="store_false", help="传入目录时不递归子目录")
    p.add_argument("-j", "--concurrency", type=int, default=1, help="批量处理时的并发请求数（默认: 1）")
    p.add_argument(
        "--save-drive",
        nargs="?",
        const="@same",
        metavar="DRIVE_DIR",
        help="将转写结果 (.txt / .nemotron.json / .srt) 直接保存到 Google Drive（不带参数保存在原音频同目录，或指定网盘目标目录）",
    )
    p.add_argument("-o", "--output", metavar="FILE", help="将结果保存到本地指定文件（单文件存文本/JSON/SRT，多文件存 .jsonl/.json）")
    p.add_argument("-O", "--output-dir", metavar="DIR", help="将每个音频的 .txt / .nemotron.json / .srt 及 batch_summary.jsonl 保存到本地目录")
    p.add_argument("--drive", action="store_true", help="强制将输入路径作为 Google Drive 网盘路径解析")
    p.add_argument("--local", action="store_true", help="强制将输入路径作为本地计算机路径解析")
    p.add_argument("--normalize-audio", action="store_true", help="在远端用 ffmpeg 统一重采样为 16kHz 单声道 WAV 后再送入模型")
    p.add_argument("--temperature", type=float, help="覆盖采样温度（默认: 转写 0.0 / 推理 0.6）")
    p.add_argument("--max-tokens", type=int, help="覆盖最大生成 token 数（默认: 转写 16384 / 推理 20480）")
    p.add_argument("--timeout", type=int, default=1800, help="单个音频请求超时时间（秒，默认: 1800）")
    p.add_argument("-i", "--instance", metavar="ID", help="指定使用某个实例（如 -i 1 或 -i 2）")
    p.add_argument("--instances", metavar="ID1,ID2", help="指定多个实例并行分摊批量任务（如 --instances 1,2,3）")
    p.add_argument("-A", "--all-instances", action="store_true", help="自动发现所有运行中的实例并并行分摊批量任务")
    p.add_argument("--url", help="覆盖服务公网 URL")
    p.add_argument("--api-token", help="覆盖 Bearer API Token")
    p.add_argument("--json", action="store_true", help="向标准输出打印完整 JSON 结果")
    a = p.parse_args(argv)

    if a.instance:
        apply_instance(SERVICE, a.instance)
    if a.prompt_file:
        a.prompt = Path(a.prompt_file).read_text(encoding="utf-8").strip()

    endpoints = _get_endpoint_pool(a)
    first_url, first_token = endpoints[0][1], endpoints[0][2]
    tasks = _expand_tasks(a, first_url, first_token)
    total = len(tasks)
    workers = max(a.concurrency, len(endpoints)) if len(endpoints) > 1 and a.concurrency == 1 else max(1, a.concurrency)
    ep_desc = ", ".join(f"{name}({url})" for name, url, _ in endpoints)
    log(f"准备执行 {total} 个音频任务 (并发数={workers}, 实例数={len(endpoints)}: {ep_desc})")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    out_dir = Path(a.output_dir) if a.output_dir else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        fut_map = {}
        for idx, task in enumerate(tasks, 1):
            inst_name, ep_url, ep_tok = endpoints[(idx - 1) % len(endpoints)]
            fut = pool.submit(_run_one_task, idx, total, task, a, ep_url, ep_tok)
            fut_map[fut] = (idx, task, inst_name)
        for fut in concurrent.futures.as_completed(fut_map):
            idx, task, inst_name = fut_map[fut]
            label = task.get("id") or task.get("label") or task.get("drive_path") or task.get("local_path")
            try:
                res = fut.result()
                res["instance"] = inst_name
                if out_dir:
                    res["saved_local_files"] = _save_local_result(res, out_dir)
                results.append(res)
                if not a.json:
                    inst_tag = f" [{inst_name}]" if len(endpoints) > 1 else ""
                    hdr = f"[{idx}/{total}]{inst_tag} {res['label']} ({res.get('elapsed_sec', 0):.2f}s)"
                    print(f"\n\033[32m=== {hdr} ===\033[0m", flush=True)
                    print(res.get("text") or "", flush=True)
                    if res.get("saved_drive_files"):
                        log(f"已写入网盘: {', '.join(res['saved_drive_files'])}")
                    if res.get("saved_local_files"):
                        log(f"已写入本地: {', '.join(res['saved_local_files'])}")
            except Exception as e:
                err_rec = {"index": idx, "instance": inst_name, "label": label, "error": str(e), "task": task}
                errors.append(err_rec)
                warn(f"[{idx}/{total}] [{inst_name}] {label} 失败: {e}")

    results.sort(key=lambda r: r.get("index", 0))

    if out_dir:
        summary_path = out_dir / "batch_summary.jsonl"
        with summary_path.open("w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        log(f"批量汇总已保存至: {summary_path}")

    if a.output:
        out_file = Path(a.output)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        if len(results) == 1 and out_file.suffix.lower() in (".txt", ".md"):
            txt = results[0].get("text") or ""
            out_file.write_text(txt + ("\n" if not txt.endswith("\n") else ""), encoding="utf-8")
        elif len(results) == 1 and out_file.suffix.lower() == ".srt" and results[0].get("srt"):
            out_file.write_text(results[0]["srt"], encoding="utf-8")
        elif out_file.suffix.lower() == ".jsonl":
            with out_file.open("w", encoding="utf-8") as f:
                for r in results:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        else:
            payload_out = results[0] if len(results) == 1 else {"results": results, "errors": errors}
            out_file.write_text(json.dumps(payload_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log(f"输出已保存至: {out_file}")

    if a.json:
        out_obj = results[0] if (len(results) == 1 and not errors) else {"results": results, "errors": errors}
        print(json.dumps(out_obj, indent=2, ensure_ascii=False))

    if errors:
        die(f"完成 {len(results)}/{total} 个任务，{len(errors)} 个任务失败。")


if __name__ == "__main__":
    inst_id, rem_argv = extract_instance_arg(sys.argv[1:])
    if inst_id:
        apply_instance(SERVICE, inst_id)
    if len(rem_argv) >= 1 and rem_argv[0] in ("transcribe", "run"):
        _cmd_transcribe(rem_argv[1:])
        sys.exit(0)
    if len(rem_argv) >= 1 and rem_argv[0] == "ls-drive":
        _cmd_ls_drive(rem_argv[1:])
        sys.exit(0)
    if len(rem_argv) >= 1 and rem_argv[0] == "prompts":
        _cmd_prompts()
        sys.exit(0)
    _extract_up_extra_args()
    main(SERVICE)
