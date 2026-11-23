#!/usr/bin/env python3
"""SoMark OXR-1.0 document parsing on Colab (A100 default; D-FINE + vLLM OXR + parse API behind Caddy).

  colabllm oxr up                               # A100; project-wide tunnel settings
  colabllm oxr up --gpu L4 --max-num-seqs 16    # smaller GPU / lower quota
  colabllm oxr parse ./document.pdf -O ./parsed --formats md,json
  colabllm oxr parse ./scan.png ./paper.pdf     # Markdown + JSON into ./oxr-output
  colabllm oxr status | info | logs -f | down

Browser: open <public-url>/demo (Basic Auth from `info --show-secrets`).
API: POST <public-url>/v1/parse/sync with `Authorization: Bearer <api_token>`, or SoMark SDK/CLI
with `--base-url <public-url>/v1 --api-key <api_token>` (the form `api_key` is verified).
See docs/oxr.md.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from cli_support import CliError, http_timeout, nonnegative_seconds, resolve_endpoint, safe_urlopen  # noqa: E402
from colabsvc import Service, State, main  # noqa: E402
from http_client import build_multipart_form  # noqa: E402

SERVICE = Service(
    name="oxr",
    notebook="oxr.ipynb",
    service_port=8000,
    auth="caddy",
    public_port=18080,
    gpu="A100",
    default_hostname="oxr",
    ready_timeout=3600,
    health_path="/health",
    env={},
)

SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
FORMAT_ALIASES = {"md": "markdown", "markdown": "markdown", "json": "json"}
UP_ENV_FLAGS = {
    "--gpu-mem-util": ("CT_OXR_GPU_MEM_UTIL", "vLLM --gpu-memory-utilization (default 0.70; D-FINE shares the GPU)"),
    "--max-num-seqs": ("CT_OXR_MAX_NUM_SEQS", "vLLM --max-num-seqs (default by VRAM: <32 GiB 16, <64 GiB 32, else 64)"),
    "--max-model-len": ("CT_OXR_MAX_MODEL_LEN", "vLLM --max-model-len (default 16384)"),
    "--concurrency": ("CT_OXR_CONCURRENCY", "parse API concurrent OXR requests (default = max-num-seqs)"),
}
_BASE_ENV = dict(SERVICE.env)


def parse_formats(value: str) -> list[str]:
    formats = []
    for item in value.split(","):
        key = item.strip().lower()
        if not key:
            continue
        if key not in FORMAT_ALIASES:
            raise argparse.ArgumentTypeError(f"不支持的输出格式 {item!r}；可选 md/markdown, json")
        if FORMAT_ALIASES[key] not in formats:
            formats.append(FORMAT_ALIASES[key])
    if not formats:
        raise argparse.ArgumentTypeError("至少指定一种输出格式")
    return formats


def json_object(value: str) -> str:
    try:
        parsed = json.loads(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"必须是 JSON 对象: {error}") from None
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("必须是 JSON 对象")
    return json.dumps(parsed, ensure_ascii=False)


def build_parse_request(path: Path, formats: list[str], *, keep_header_footer=False, element_formats=None):
    """Return ``(body, content_type)`` for SoMark ``POST /v1/parse/sync``.

    Authentication uses the Bearer header; ``api_key`` is a required upstream form field, so a
    non-secret placeholder is sent instead of duplicating the token in the body.
    """
    fields = [("api_key", "bearer"), *(("output_formats", item) for item in formats)]
    if keep_header_footer:
        fields.append(("feature_config", json.dumps({"keep_header_footer": True})))
    if element_formats:
        fields.append(("element_formats", element_formats))
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return build_multipart_form(fields, [("file", path.name, content_type, path.read_bytes())])


def collect_inputs(specs: list[str]) -> list[Path]:
    files: list[Path] = []
    for spec in specs:
        path = Path(spec).expanduser()
        if path.is_dir():
            found = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
            if not found:
                raise CliError(f"目录中没有可解析的 PDF/图片: {spec}")
            files.extend(found)
        elif path.is_file():
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise CliError(f"不支持的文件类型: {spec}（支持 PDF/PNG/JPEG/BMP/TIFF/WEBP）")
            files.append(path)
        else:
            raise CliError(f"本地文件不存在: {spec}")
    return files


def output_names(files: list[Path]) -> dict[Path, str]:
    names, used = {}, set()
    for path in files:
        base, index = path.stem or "document", 1
        candidate = base
        while candidate in used:
            index += 1
            candidate = f"{base}-{index}"
        used.add(candidate)
        names[path] = candidate
    return names


def post_parse(url: str, token: str, body: bytes, content_type: str, timeout):
    request = urllib.request.Request(url.rstrip("/") + "/v1/parse/sync", data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": content_type, "User-Agent": "colabllm-oxr/1.0"})
    try:
        with safe_urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise CliError(f"HTTP {error.code}: {detail}") from None


def cmd_parse(args) -> int:
    formats = args.formats
    url, token = resolve_endpoint(State(SERVICE).load(), args.url, args.api_token,
                                  url_env="OXR_URL", token_env="OXR_API_TOKEN")
    files = collect_inputs(args.inputs)
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    names = output_names(files)
    timeout = http_timeout(args.timeout, default=1800)
    results, failed = [], 0
    for path in files:
        print(f"解析: {path}", file=sys.stderr, flush=True)
        body, content_type = build_parse_request(path, formats, keep_header_footer=args.keep_header_footer,
                                                 element_formats=args.element_formats)
        payload = post_parse(url, token, body, content_type, timeout)
        entry = {"source": str(path), "code": payload.get("code"), "message": payload.get("message")}
        if payload.get("code") != 0:
            failed += 1
            entry["error"] = (payload.get("data") or {}).get("error") or payload.get("message")
            results.append(entry)
            continue
        outputs = ((payload.get("data") or {}).get("result") or {}).get("outputs") or {}
        saved = []
        if "markdown" in formats and isinstance(outputs.get("markdown"), str):
            target = out_dir / f"{names[path]}.md"
            target.write_text(outputs["markdown"], encoding="utf-8")
            saved.append(str(target))
        if "json" in formats:
            target = out_dir / f"{names[path]}.json"
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            saved.append(str(target))
        entry.update(saved=saved, warnings=payload.get("warnings") or [],
                     metadata=(payload.get("data") or {}).get("metadata") or {})
        results.append(entry)
    print(json.dumps({"out_dir": str(out_dir), "files": results}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def configure_cli(parser, commands, up):
    parse = commands.add_parser("parse", help="解析本机 PDF/图片为 Markdown/JSON（SoMark /v1/parse/sync）")
    parse.add_argument("inputs", nargs="+", help="本机 PDF/图片文件或目录（目录递归）")
    parse.add_argument("--formats", type=parse_formats, default=["markdown", "json"],
                       help="输出格式，逗号分隔：md,json（默认两者）")
    parse.add_argument("--out", "-O", default="oxr-output", help="本机输出目录（默认 ./oxr-output）")
    parse.add_argument("--keep-header-footer", action="store_true", help="保留页眉/页脚")
    parse.add_argument("--element-formats", type=json_object,
                       help='SoMark element_formats JSON，例如 \'{"table":"markdown","image":"none"}\'')
    parse.add_argument("--url", help="覆盖服务 URL（默认读取运行实例）")
    parse.add_argument("--api-token", help="显式 Bearer API token（用于 --url 指向的其他服务）")
    parse.add_argument("--timeout", type=nonnegative_seconds, help="单文件请求超时秒数（默认 1800；0 不限时）")
    parse.set_defaults(command="parse")
    for flag, (env, help_text) in UP_ENV_FLAGS.items():
        up.add_argument(flag, dest="up_" + env, metavar="VALUE", help=help_text)


def prepare_up(args):
    SERVICE.env.clear()
    SERVICE.env.update(_BASE_ENV)
    for env, _ in UP_ENV_FLAGS.values():
        value = getattr(args, "up_" + env, None)
        if value is not None:
            SERVICE.env[env] = str(value)


if __name__ == "__main__":
    from cli_support import run_cli
    run_cli(lambda: main(SERVICE, configure=configure_cli, prepare_up=prepare_up, handlers={"parse": cmd_parse}))
