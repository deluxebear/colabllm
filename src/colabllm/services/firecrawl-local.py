#!/usr/bin/env python3
"""Firecrawl Local v2 API service on Colab CPU with Google Drive binary caching.

Runs `firecrawl-local` (from `firecrawl/firecrawl/apps/local`) on a zero-GPU Colab CPU
VM (`gpu=None`, optional `--high-mem`) behind Caddy Basic Auth + Bearer token auth:
  - Compiles the standalone Rust binary once on local NVMe SSD and caches it on Google
    Drive (`$CT_DATA_DIR/bin/`), keyed by `apps/local` + `apps/api/native/src` commit
    SHA + arch + glibc version, so subsequent launches restore in ~1s without Rust.
  - Runs `firecrawl-local serve` as an unprivileged `firecrawl` user with native-sandbox
    headless Chrome (`google-chrome-stable`) for JavaScript / SPA rendering and
    screenshots while keeping `/content/drive` isolated from browser processes.
  - Exposes the full Firecrawl v2 HTTP API (`/v2/scrape`, `/v2/map`, `/v2/crawl`,
    `/v2/batch/scrape`, `/health`), directly compatible with official Firecrawl SDKs.

Usage:
  colabllm firecrawl-local up                             # CPU VM + Drive binary cache + Chrome
  colabllm firecrawl-local up --browser never             # fast static/PDF/Office mode (no Chrome)
  colabllm firecrawl-local up --rebuild                   # force recompile and refresh Drive cache
  colabllm firecrawl-local health --json
  colabllm firecrawl-local scrape https://example.com --markdown
  colabllm firecrawl-local map https://docs.firecrawl.dev --limit 100
  colabllm firecrawl-local crawl https://example.com --limit 20 --wait --out ./pages
  colabllm firecrawl-local batch https://example.com https://iana.org --wait --json
  colabllm firecrawl-local down
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from cli_support import (  # noqa: E402
    CliError,
    Deadline,
    nonnegative_seconds,
    positive_seconds,
    resolve_endpoint,
    safe_urlopen,
)
from colabsvc import Service, State, main  # noqa: E402

SERVICE = Service(
    name="firecrawl-local",
    notebook="firecrawl-local.ipynb",
    service_port=3002,
    public_port=18080,
    auth="caddy",
    gpu=None,
    high_mem=False,
    default_hostname="firecrawl",
    ready_timeout=1800,
    env={
        "CT_FIRECRAWL_REF": "main",
        "CT_FIRECRAWL_LTO": "thin",
        "CT_FIRECRAWL_REBUILD": "0",
        "CT_FIRECRAWL_BROWSER": "auto",
        "CT_FIRECRAWL_CLOUD": "off",
        "CT_FIRECRAWL_SCROLL": "0",
    },
)

_BASE_ENV = dict(SERVICE.env)
_BASE_HIGH_MEM = SERVICE.high_mem
UP_ENV_FLAGS = {
    "--ref": "CT_FIRECRAWL_REF",
    "--lto": "CT_FIRECRAWL_LTO",
    "--browser": "CT_FIRECRAWL_BROWSER",
    "--cloud": "CT_FIRECRAWL_CLOUD",
    "--user-agent": "CT_FIRECRAWL_USER_AGENT",
    "--firecrawl-api-key": "FIRECRAWL_API_KEY",
}
_SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _request(
    path: str,
    args: argparse.Namespace,
    payload: dict[str, Any] | None = None,
    *,
    method: str | None = None,
) -> dict[str, Any]:
    url, token = resolve_endpoint(
        State(SERVICE).load(),
        getattr(args, "url", None),
        getattr(args, "api_token", None),
        url_env="FIRECRAWL_API_URL",
        token_env="FIRECRAWL_API_KEY",
    )
    req_method = method or ("POST" if payload is not None else "GET")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "FirecrawlLocalCLI/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url + path, data=data, headers=headers, method=req_method)
    deadline = getattr(args, "_wait_deadline", None)
    timeout = deadline.remaining(120) if deadline else 120
    try:
        with safe_urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        raise CliError(f"HTTP {err.code}: {raw}") from None


def _add_endpoint_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--url", help="override service base URL (env: FIRECRAWL_API_URL)")
    p.add_argument("--api-token", help="override Bearer API token (env: FIRECRAWL_API_KEY)")


def _add_poll_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--wait", action="store_true", help="poll until the job finishes")
    p.add_argument("--timeout", type=nonnegative_seconds, default=600.0,
                   help="max wait seconds (0 for unlimited, default: 600)")
    p.add_argument("--poll-interval", type=positive_seconds, default=2.0,
                   help="seconds between job status polls (default: 2)")


def _parse_formats(raw: str | None) -> list[str]:
    if not raw:
        return ["markdown"]
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    return parts or ["markdown"]


def _poll_job(job_path: str, args: argparse.Namespace) -> dict[str, Any]:
    deadline = Deadline(getattr(args, "timeout", 600.0))
    args._wait_deadline = deadline
    interval = float(getattr(args, "poll_interval", 2.0))
    while True:
        res = _request(job_path, args, method="GET")
        status = str(res.get("status") or "").lower()
        if status in ("completed", "failed", "cancelled", "canceled"):
            return res
        completed = res.get("completed", 0)
        total = res.get("total", 0)
        print(f"status={status or 'running'} completed={completed}/{total}", file=sys.stderr)
        deadline.sleep(interval)


def _write_crawl_pages(out_dir: Path, pages: list[dict[str, Any]]) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for idx, item in enumerate(pages, 1):
        md = item.get("markdown")
        if not isinstance(md, str):
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        src_url = str(meta.get("sourceURL") or meta.get("url") or f"page-{idx}")
        slug = _SAFE_FILE_RE.sub("_", re.sub(r"^https?://", "", src_url)).strip("_")[:100] or f"page-{idx}"
        (out_dir / f"{idx:04d}_{slug}.md").write_text(md, encoding="utf-8")
        written += 1
    return written


def cmd_health(args: argparse.Namespace) -> None:
    res = _request("/health", args, method="GET")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print(f"status={res.get('status')} engine={res.get('engine')} version={res.get('version')}")


def cmd_scrape(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {
        "url": args.target_url,
        "formats": _parse_formats(args.format),
        "onlyMainContent": not args.full_page,
    }
    if args.timeout_ms is not None:
        payload["timeout"] = args.timeout_ms
    if args.wait_for is not None:
        payload["waitFor"] = args.wait_for
    res = _request("/v2/scrape", args, payload=payload, method="POST")
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if args.markdown:
            out_path.write_text(str(data.get("markdown") or ""), encoding="utf-8")
        else:
            out_path.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved output to {out_path}", file=sys.stderr)
    if args.markdown:
        print(data.get("markdown") or "")
        return
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_map(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {"url": args.target_url, "limit": args.limit}
    if args.search:
        payload["search"] = args.search
    res = _request("/v2/map", args, payload=payload, method="POST")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    links = res.get("links") or []
    for entry in links:
        if isinstance(entry, dict):
            print(entry.get("url") or "")
        else:
            print(entry)


def cmd_crawl(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {
        "url": args.target_url,
        "limit": args.limit,
        "scrapeOptions": {
            "formats": _parse_formats(args.format),
            "onlyMainContent": not args.full_page,
        },
    }
    if args.max_depth is not None:
        payload["maxDiscoveryDepth"] = args.max_depth
    if args.include:
        payload["includePaths"] = list(args.include)
    if args.exclude:
        payload["excludePaths"] = list(args.exclude)
    if args.ignore_sitemap:
        payload["ignoreSitemap"] = True
    res = _request("/v2/crawl", args, payload=payload, method="POST")
    job_id = res.get("id")
    if args.wait and job_id:
        res = _poll_job(f"/v2/crawl/{job_id}", args)
        if args.out:
            n = _write_crawl_pages(Path(args.out), res.get("data") or [])
            print(f"Wrote {n} markdown files to {args.out}", file=sys.stderr)
    if args.json or not args.wait:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print(f"id={job_id} status={res.get('status')} completed={res.get('completed')}/{res.get('total')}")


def cmd_crawl_status(args: argparse.Namespace) -> None:
    path = f"/v2/crawl/{args.id}"
    if args.cancel:
        res = _request(path, args, method="DELETE")
    elif args.wait:
        res = _poll_job(path, args)
    else:
        res = _request(path, args, method="GET")
    if args.out and isinstance(res.get("data"), list):
        n = _write_crawl_pages(Path(args.out), res["data"])
        print(f"Wrote {n} markdown files to {args.out}", file=sys.stderr)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_batch(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {
        "urls": list(args.urls),
        "formats": _parse_formats(args.format),
        "onlyMainContent": not args.full_page,
    }
    res = _request("/v2/batch/scrape", args, payload=payload, method="POST")
    job_id = res.get("id")
    if args.wait and job_id:
        res = _poll_job(f"/v2/batch/scrape/{job_id}", args)
        if args.out:
            n = _write_crawl_pages(Path(args.out), res.get("data") or [])
            print(f"Wrote {n} markdown files to {args.out}", file=sys.stderr)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def configure_cli(parser: argparse.ArgumentParser, sub: argparse._SubParsersAction, up_p: argparse.ArgumentParser) -> None:
    up_p.add_argument("--high-mem", dest="up_high_mem", action="store_true",
                      help="request a high-RAM Colab CPU runtime (recommended for heavy crawls + Chrome)")
    up_p.add_argument("--browser", dest="up_CT_FIRECRAWL_BROWSER", choices=("auto", "never", "always"), default=None,
                      help="headless Chrome JS rendering mode (default: auto; 'never' skips Chrome install)")
    up_p.add_argument("--cloud", dest="up_CT_FIRECRAWL_CLOUD", choices=("off", "search", "auto"), default=None,
                      help="Firecrawl Cloud hand-off policy (default: off)")
    up_p.add_argument("--ref", dest="up_CT_FIRECRAWL_REF", default=None,
                      help="source ref to build/cache: git branch/tag/SHA (default: main) or crates:<version>")
    up_p.add_argument("--lto", dest="up_CT_FIRECRAWL_LTO", choices=("off", "thin", "fat"), default=None,
                      help="Rust release LTO mode on cache miss (default: thin; 'off' is fastest)")
    up_p.add_argument("--rebuild", dest="up_rebuild", action="store_true",
                      help="ignore Drive binary cache, recompile from source, and refresh cache")
    up_p.add_argument("--user-agent", dest="up_CT_FIRECRAWL_USER_AGENT", default=None,
                      help="override outbound HTTP/Chrome User-Agent")
    up_p.add_argument("--scroll", dest="up_scroll", action="store_true",
                      help="scroll JS-rendered pages before extracting content")
    up_p.add_argument("--firecrawl-api-key", dest="up_FIRECRAWL_API_KEY", default=None,
                      help="optional Firecrawl Cloud API key when using --cloud search|auto")

    health_p = sub.add_parser("health", help="check remote firecrawl-local /health endpoint")
    health_p.add_argument("--json", action="store_true", help="output JSON")
    _add_endpoint_flags(health_p)

    scrape_p = sub.add_parser("scrape", help="scrape a single URL via POST /v2/scrape")
    scrape_p.add_argument("target_url", metavar="URL", help="URL to scrape")
    scrape_p.add_argument("-f", "--format", default="markdown",
                          help="comma-separated formats: markdown,html,rawHtml,links,screenshot (default: markdown)")
    scrape_p.add_argument("--markdown", action="store_true", help="print only the extracted markdown")
    scrape_p.add_argument("--full-page", action="store_true", help="keep nav/header/footer (onlyMainContent=false)")
    scrape_p.add_argument("--timeout-ms", type=int, default=None, help="per-page scrape timeout in milliseconds")
    scrape_p.add_argument("--wait-for", type=int, default=None, help="wait milliseconds after page load")
    scrape_p.add_argument("-o", "--out", default=None, help="save output to file")
    scrape_p.add_argument("--json", action="store_true", help="output JSON (default unless --markdown)")
    _add_endpoint_flags(scrape_p)

    map_p = sub.add_parser("map", help="discover URLs on a site via POST /v2/map")
    map_p.add_argument("target_url", metavar="URL", help="site URL to map")
    map_p.add_argument("--search", default=None, help="filter discovered URLs containing this text")
    map_p.add_argument("-l", "--limit", type=int, default=5000, help="max URLs to return (default: 5000)")
    map_p.add_argument("--json", action="store_true", help="output JSON")
    _add_endpoint_flags(map_p)

    crawl_p = sub.add_parser("crawl", help="crawl a site via POST /v2/crawl")
    crawl_p.add_argument("target_url", metavar="URL", help="starting URL")
    crawl_p.add_argument("-l", "--limit", type=int, default=100, help="max pages to crawl (default: 100)")
    crawl_p.add_argument("--max-depth", type=int, default=None, help="maximum link discovery depth")
    crawl_p.add_argument("--include", action="append", default=None, help="regex path filter to include (repeatable)")
    crawl_p.add_argument("--exclude", action="append", default=None, help="regex path filter to exclude (repeatable)")
    crawl_p.add_argument("--ignore-sitemap", action="store_true", help="do not use sitemap.xml")
    crawl_p.add_argument("-f", "--format", default="markdown", help="scrape formats (default: markdown)")
    crawl_p.add_argument("--full-page", action="store_true", help="keep nav/header/footer")
    crawl_p.add_argument("-o", "--out", default=None, help="write one markdown file per page into directory (with --wait)")
    crawl_p.add_argument("--json", action="store_true", help="output JSON")
    _add_poll_flags(crawl_p)
    _add_endpoint_flags(crawl_p)

    cstatus_p = sub.add_parser("crawl-status", help="check, wait for, or cancel a crawl job")
    cstatus_p.add_argument("id", help="crawl job ID")
    cstatus_p.add_argument("--cancel", action="store_true", help="cancel the crawl job (DELETE /v2/crawl/{id})")
    cstatus_p.add_argument("-o", "--out", default=None, help="write markdown files into directory")
    cstatus_p.add_argument("--json", action="store_true", help="output JSON")
    _add_poll_flags(cstatus_p)
    _add_endpoint_flags(cstatus_p)

    batch_p = sub.add_parser("batch", help="batch-scrape multiple URLs via POST /v2/batch/scrape")
    batch_p.add_argument("urls", nargs="+", metavar="URL", help="one or more URLs to scrape")
    batch_p.add_argument("-f", "--format", default="markdown", help="scrape formats (default: markdown)")
    batch_p.add_argument("--full-page", action="store_true", help="keep nav/header/footer")
    batch_p.add_argument("-o", "--out", default=None, help="write markdown files into directory (with --wait)")
    batch_p.add_argument("--json", action="store_true", help="output JSON")
    _add_poll_flags(batch_p)
    _add_endpoint_flags(batch_p)


def prepare_up(args: argparse.Namespace) -> None:
    SERVICE.env.clear()
    SERVICE.env.update(_BASE_ENV)
    SERVICE.high_mem = bool(getattr(args, "up_high_mem", _BASE_HIGH_MEM))
    for env_key in UP_ENV_FLAGS.values():
        val = getattr(args, "up_" + env_key, None)
        if val is not None:
            SERVICE.env[env_key] = str(val)
    if getattr(args, "up_rebuild", False):
        SERVICE.env["CT_FIRECRAWL_REBUILD"] = "1"
    if getattr(args, "up_scroll", False):
        SERVICE.env["CT_FIRECRAWL_SCROLL"] = "1"


HANDLERS = {
    "health": cmd_health,
    "scrape": cmd_scrape,
    "map": cmd_map,
    "crawl": cmd_crawl,
    "crawl-status": cmd_crawl_status,
    "batch": cmd_batch,
}

if __name__ == "__main__":
    from cli_support import run_cli

    run_cli(lambda: main(SERVICE, configure=configure_cli, prepare_up=prepare_up, handlers=HANDLERS))
