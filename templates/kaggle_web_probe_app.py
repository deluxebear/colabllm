"""Pure-stdlib loopback HTTP connectivity and cache probe application for Kaggle."""
from __future__ import annotations

from functools import partial
import html
import http.server
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any


_START_MONO = time.monotonic()


def _load_manifests(cache_dir: str) -> list[dict[str, Any]]:
    manifest_dir = Path(cache_dir) / "manifests"
    if not manifest_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for p in sorted(manifest_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if isinstance(data, dict):
            items.append(data)
    return items


def _free_mb(path_str: str) -> int:
    target = Path(path_str)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return int(shutil.disk_usage(target).free // (1024 * 1024))
    except OSError:
        return 0


def build_health_payload(service_name: str, cache_dir: str, work_dir: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": service_name,
        "provider": "kaggle",
        "uptime_seconds": int(time.monotonic() - _START_MONO),
        "cache_dir": cache_dir,
        "work_dir": work_dir,
        "free_mb": _free_mb(cache_dir or "/kaggle/working"),
        "cached_artifacts": [
            {
                "artifact": str(item.get("artifact") or ""),
                "version": str(item.get("version") or ""),
                "sha256": str(item.get("sha256") or "")[:16],
                "verified_at": str(item.get("verified_at") or ""),
            }
            for item in _load_manifests(cache_dir)
        ],
    }


def render_status_html(payload: dict[str, Any]) -> str:
    svc = html.escape(str(payload["service"]))
    uptime = html.escape(str(payload["uptime_seconds"]))
    cache_dir = html.escape(str(payload["cache_dir"]))
    free_mb = html.escape(str(payload["free_mb"]))
    rows_html = []
    for item in payload["cached_artifacts"]:
        art = html.escape(str(item.get("artifact") or "-"))
        ver = html.escape(str(item.get("version") or "-"))
        sha = html.escape(str(item.get("sha256") or "-"))
        ver_at = html.escape(str(item.get("verified_at") or "-"))
        rows_html.append(
            f"<tr><th scope=\"row\">{art}</th><td>{ver}</td><td><code>{sha}...</code></td><td>{ver_at}</td></tr>"
        )
    if not rows_html:
        rows_html.append('<tr><th scope="row">(none)</th><td>-</td><td>-</td><td>-</td></tr>')
    tbody = "\n          ".join(rows_html)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Kaggle Runtime Probe · {svc}</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --fg: #0f172a;
      --card: #ffffff;
      --border: #cbd5e1;
      --muted: #475569;
      --accent: #0284c7;
      color-scheme: light dark;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0f172a;
        --fg: #f1f5f9;
        --card: #1e293b;
        --border: #334155;
        --muted: #94a3b8;
        --accent: #38bdf8;
      }}
    }}
    body {{
      font-family: system-ui, -apple-system, sans-serif;
      background: var(--bg);
      color: var(--fg);
      margin: 0;
      padding: 1.5rem;
      line-height: 1.5;
    }}
    main {{ max-inline-size: 56rem; margin-inline: auto; }}
    .meta {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
      gap: 0.75rem;
      margin-block: 1rem 1.5rem;
    }}
    .stat {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 0.5rem;
      padding: 0.75rem 1rem;
    }}
    .stat strong {{ display: block; font-size: 0.85rem; color: var(--muted); }}
    .table-wrapper {{
      overflow: auto;
      max-inline-size: 100%;
      max-block-size: min(420px, 70vh);
      container-type: inline-size;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 0.5rem;
    }}
    table {{ width: 100%; border-collapse: separate; border-spacing: 0; text-align: left; }}
    caption {{ padding: 0.75rem 1rem; font-weight: 600; text-align: left; }}
    th, td {{ padding: 0.6rem 1rem; border-block-start: 1px solid var(--border); white-space: nowrap; }}
    thead {{ position: sticky; inset-block-start: 0; z-index: 1; background: var(--card); }}
    tbody th {{ position: sticky; inset-inline-start: 0; background: var(--card); }}
    @container (width < 600px) {{
      table, thead, tbody, tr, th, td {{ display: block; }}
      thead {{ display: none; }}
      tr {{ margin-block-end: 0.75rem; border-block-end: 1px solid var(--border); }}
      tbody th {{ position: sticky; inset-block-start: 0; z-index: 2; font-weight: 600; }}
      td:nth-child(2)::before {{ content: "Version: "; color: var(--muted); }}
      td:nth-child(3)::before {{ content: "SHA-256: "; color: var(--muted); }}
      td:nth-child(4)::before {{ content: "Verified: "; color: var(--muted); }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Kaggle Web Probe: {svc}</h1>
    <p>Pure-CPU connectivity, cache, and isolation diagnostic endpoint.</p>
    <section class="meta" aria-label="Runtime Metrics">
      <div class="stat"><strong>Status</strong><span>OK</span></div>
      <div class="stat"><strong>Uptime</strong><span>{uptime}s</span></div>
      <div class="stat"><strong>Cache Free Space</strong><span>{free_mb} MiB</span></div>
      <div class="stat"><strong>Cache Dir</strong><code>{cache_dir}</code></div>
    </section>
    <div tabindex="0" role="region" aria-labelledby="probe-cache-caption" class="table-wrapper">
      <table>
        <caption id="probe-cache-caption">Verified Cache Artifacts</caption>
        <thead>
          <tr><th>Artifact</th><th>Version</th><th>SHA-256</th><th>Verified At</th></tr>
        </thead>
        <tbody>
          {tbody}
        </tbody>
      </table>
    </div>
  </main>
</body>
</html>
"""


class ProbeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, service_name: str, cache_dir: str, work_dir: str, **kwargs: Any) -> None:
        self.service_name = service_name
        self.cache_dir = cache_dir
        self.work_dir = work_dir
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _send_json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
        self.close_connection = True

    def _send_html(self, status: int, html_text: str) -> None:
        raw = html_text.encode("utf-8")
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
        self.close_connection = True

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0] or "/"
        if path == "/v1" or path.startswith("/v1/"):
            self._send_json(404, {"error": "not_supported"})
            return
        if path == "/health" and self.command in ("GET", "HEAD"):
            payload = build_health_payload(self.service_name, self.cache_dir, self.work_dir)
            self._send_json(200, payload)
            return
        if path == "/" and self.command in ("GET", "HEAD"):
            payload = build_health_payload(self.service_name, self.cache_dir, self.work_dir)
            self._send_html(200, render_status_html(payload))
            return
        self._send_json(404, {"error": "not_found"})

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()


def serve_probe(
    *,
    host: str = "127.0.0.1",
    port: int = 18090,
    service_name: str = "web-probe",
    cache_dir: str = "/kaggle/working/.ct-cache",
    work_dir: str = "/tmp/ct-runtime",
) -> http.server.ThreadingHTTPServer:
    handler = partial(
        ProbeHandler,
        service_name=service_name,
        cache_dir=cache_dir,
        work_dir=work_dir,
    )
    server = http.server.ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> int:
    port = int(os.environ.get("CT_SERVICE_PORT", "18090"))
    service_name = os.environ.get("CT_SERVICE", "web-probe")
    cache_dir = os.environ.get("CT_CACHE_DIR", "/kaggle/working/.ct-cache")
    work_dir = os.environ.get("CT_WORK_DIR", "/tmp/ct-runtime")
    srv = serve_probe(host="127.0.0.1", port=port, service_name=service_name, cache_dir=cache_dir, work_dir=work_dir)
    print(f"[web-probe] listening on http://127.0.0.1:{srv.server_address[1]} cache={cache_dir}", flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
