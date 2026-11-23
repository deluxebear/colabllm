"""Self-contained Unlimited-OCR 2 interactive test/demo Web UI and raw layout parser."""
from __future__ import annotations

import re

PAGE_SPLIT_RE = re.compile(r'<\s*/?\s*PAGE\s*\d*\s*>', re.I)
MERGE_RE = re.compile(r'<\s*/?\s*MERGE\s*>', re.I)
STRAY_TOKEN_RE = re.compile(
    r'<[|｜]/?[a-zA-Z_]+[|｜]>|<image>'
    r'|<\s*/?\s*PAGE\s*\d*\s*>|<\s*/?\s*BLOCK\s*>|<\s*/?\s*FORK\s*>'
    r'|<\s*/?\s*LEVEL\s*\d*\s*[>}]?|<\s*/?\s*MERGE\s*>',
    re.I,
)
BLOCK_RE = re.compile(
    r'<\s*BLOCK\s*>\s*([a-zA-Z_]+)'
    r'(?:'
    r'\s*<\s*/?\s*LEVEL\s*(\d*)\s*[>}]?(?:\s*\[\s*(-?\d+)\s*[,，]\s*(-?\d+)\s*[,，]\s*(-?\d+)\s*[,，]\s*(-?\d+)'
    r'(?:\s*[,，]\s*-?\d+)*\s*\])?'
    r'|'
    r'\s*\[\s*(-?\d+)\s*[,，]\s*(-?\d+)\s*[,，]\s*(-?\d+)\s*[,，]\s*(-?\d+)'
    r'(?:\s*[,，]\s*-?\d+)*\s*\](?:\s*<\s*/?\s*LEVEL\s*(\d*)\s*[>}]?)?'
    r')?'
    r'\s*(?:<\s*FORK\s*>)?',
    re.I,
)
_BASE_LABELS = {
    'text', 'title', 'header', 'footer', 'page_number', 'image', 'figure',
    'chart', 'seal', 'table', 'image_caption', 'image_footnote',
    'page_footnote', 'equation', 'ref_text', 'aside_text', 'algorithm',
}
_BARE_LABELS = _BASE_LABELS | {f'{p}{l}' for p in ('cross_', 'append_') for l in _BASE_LABELS}
BARE_BLOCK_RE = re.compile(
    r'(?:(?<=<FORK>)|(?<=<PAGE>)|(?<=\n)|^)'
    r'(?:<[|｜]\s*det\s*[|｜]>)?'
    r'((?:[a-zA-Z_][a-zA-Z_0-9]*)(?:\s*<\s*/?\s*LEVEL\s*\d*\s*[>}]?)?\s*'
    r'\[\s*-?\d+(?:\s*[,，]\s*-?\d+){3,}[^\]]*\])',
    re.I,
)


def _scale(value: int) -> int:
    return max(0, min(1000, int(value)))


def _base_label(label: str) -> str:
    for prefix in ('append_', 'cross_'):
        if label.startswith(prefix):
            return label[len(prefix):]
    return label


def _fix_bare_blocks(text: str) -> str:
    def _repl(match: re.Match[str]) -> str:
        name = re.match(r'[a-zA-Z_][a-zA-Z_0-9]*', match.group(1)).group(0)
        return f'<BLOCK>{match.group(1)}' if name.lower() in _BARE_LABELS else match.group(0)

    fixed = BARE_BLOCK_RE.sub(_repl, text)
    return re.sub(r'\n\s*<FORK>\s*(?=<BLOCK>)', '\n', fixed, flags=re.I)


def parse_raw_blocks(raw_text: str) -> list[dict]:
    """Parse Unlimited-OCR 2 raw block output into normalized 0..1000 layout boxes."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        return []
    text = _fix_bare_blocks(raw_text)
    segments = PAGE_SPLIT_RE.split(text)
    if segments and not segments[0].strip():
        segments = segments[1:]
    blocks: list[dict] = []
    for page_idx, seg in enumerate(segments, start=1):
        if not seg.strip():
            continue
        matches = list(BLOCK_RE.finditer(seg))
        if not matches:
            cleaned = STRAY_TOKEN_RE.sub('', MERGE_RE.sub('', seg)).strip()
            if cleaned:
                blocks.append({
                    'index': len(blocks) + 1,
                    'page': page_idx,
                    'label': '__raw__',
                    'base_label': '__raw__',
                    'level': None,
                    'bbox': None,
                    'content': cleaned,
                })
            continue
        for idx, m in enumerate(matches):
            if idx == 0:
                pre = STRAY_TOKEN_RE.sub('', MERGE_RE.sub('', seg[:m.start()])).strip()
                if pre:
                    blocks.append({
                        'index': len(blocks) + 1,
                        'page': page_idx,
                        'label': '__raw__',
                        'base_label': '__raw__',
                        'level': None,
                        'bbox': None,
                        'content': pre,
                    })
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(seg)
            content = STRAY_TOKEN_RE.sub('', MERGE_RE.sub('', seg[m.end():end])).strip()
            if m.group(2) is not None:
                level = int(m.group(2)) if m.group(2) else None
                bbox = ([_scale(m.group(k)) for k in range(3, 7)] if m.group(3) is not None else None)
            else:
                bbox = ([_scale(m.group(k)) for k in range(7, 11)] if m.group(7) is not None else None)
                level = int(m.group(11)) if m.group(11) else None
            label = m.group(1).lower()
            blocks.append({
                'index': len(blocks) + 1,
                'page': page_idx,
                'label': label,
                'base_label': _base_label(label),
                'level': level,
                'bbox': bbox,
                'content': content,
            })
    return blocks


def make_csp(nonce: str) -> str:
    """Return a strict per-request CSP header without external origins."""
    return (
        "default-src 'self'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'"
    )


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Unlimited-OCR 2 Online Demo</title>
  <style nonce="__NONCE__">
    :root {
      --bg-light: #f4f6fa;
      --bg-dark: #0d121a;
      --surface-light: #ffffff;
      --surface-dark: #151c27;
      --surface-2-light: #eef2f8;
      --surface-2-dark: #1d2635;
      --text-light: #16202e;
      --text-dark: #e6edf7;
      --muted-light: #556377;
      --muted-dark: #97a6bc;
      --border-light: #d2dae6;
      --border-dark: #2c384b;
      --brand-light: #c86300;
      --brand-dark: #f59e0b;
      --brand-text-light: #ffffff;
      --brand-text-dark: #1b1102;
      --ok-light: #15803d;
      --ok-dark: #86efac;
      --ok-bg-light: #eafaf0;
      --ok-bg-dark: #122d1f;
      --danger-light: #b91c1c;
      --danger-dark: #fca5a5;
      --danger-bg-light: #fef2f2;
      --danger-bg-dark: #3a1418;
      --scroll-track-light: #e7ecf4;
      --scroll-track-dark: #18202c;
      --scroll-thumb-light: #69778b;
      --scroll-thumb-dark: #8594aa;

      --bg: var(--bg-light);
      --surface: var(--surface-light);
      --surface-2: var(--surface-2-light);
      --text: var(--text-light);
      --muted: var(--muted-light);
      --border: var(--border-light);
      --brand: var(--brand-light);
      --brand-text: var(--brand-text-light);
      --ok: var(--ok-light);
      --ok-bg: var(--ok-bg-light);
      --danger: var(--danger-light);
      --danger-bg: var(--danger-bg-light);
      --scroll-track: var(--scroll-track-light);
      --scroll-thumb: var(--scroll-thumb-light);

      @media (prefers-color-scheme: dark) {
        --bg: var(--bg-dark);
        --surface: var(--surface-dark);
        --surface-2: var(--surface-2-dark);
        --text: var(--text-dark);
        --muted: var(--muted-dark);
        --border: var(--border-dark);
        --brand: var(--brand-dark);
        --brand-text: var(--brand-text-dark);
        --ok: var(--ok-dark);
        --ok-bg: var(--ok-bg-dark);
        --danger: var(--danger-dark);
        --danger-bg: var(--danger-bg-dark);
        --scroll-track: var(--scroll-track-dark);
        --scroll-thumb: var(--scroll-thumb-dark);
      }

      @supports (color: light-dark(white, black)) {
        --bg: light-dark(var(--bg-light), var(--bg-dark));
        --surface: light-dark(var(--surface-light), var(--surface-dark));
        --surface-2: light-dark(var(--surface-2-light), var(--surface-2-dark));
        --text: light-dark(var(--text-light), var(--text-dark));
        --muted: light-dark(var(--muted-light), var(--muted-dark));
        --border: light-dark(var(--border-light), var(--border-dark));
        --brand: light-dark(var(--brand-light), var(--brand-dark));
        --brand-text: light-dark(var(--brand-text-light), var(--brand-text-dark));
        --ok: light-dark(var(--ok-light), var(--ok-dark));
        --ok-bg: light-dark(var(--ok-bg-light), var(--ok-bg-dark));
        --danger: light-dark(var(--danger-light), var(--danger-dark));
        --danger-bg: light-dark(var(--danger-bg-light), var(--danger-bg-dark));
        --scroll-track: light-dark(var(--scroll-track-light), var(--scroll-track-dark));
        --scroll-thumb: light-dark(var(--scroll-thumb-light), var(--scroll-thumb-dark));
      }

      --left-ratio: 50%;
      color-scheme: light dark;
      accent-color: var(--brand);
      scrollbar-color: var(--scroll-thumb) var(--scroll-track);
    }

    @supports not (scrollbar-color: auto) {
      .scroller { scrollbar-width: auto; }
      .scroller::-webkit-scrollbar { width: 10px; height: 10px; }
      .scroller::-webkit-scrollbar-thumb { background: var(--scroll-thumb); border-radius: 999px; }
      .scroller::-webkit-scrollbar-track { background: var(--scroll-track); }
    }

    *, *::before, *::after { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      line-height: 1.5;
      background: var(--bg);
      color: var(--text);
    }

    .visually-hidden:where(:not(:focus-within, :active)) {
      position: absolute !important;
      clip-path: inset(50%) !important;
      overflow: hidden !important;
      width: 1px !important;
      height: 1px !important;
      margin: -1px !important;
      padding: 0 !important;
      border: 0 !important;
      white-space: nowrap !important;
    }

    .app-shell {
      display: grid;
      grid-template-rows: auto 1fr;
      height: 100dvh;
      min-height: 540px;
    }

    .app-header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      padding: 0.7rem 1rem;
      background: var(--surface);
      border-block-end: 1px solid var(--border);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      min-width: 0;
    }
    .brand h1 {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 700;
      letter-spacing: -0.01em;
    }
    .brand-accent { color: var(--brand); }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      padding: 0.15rem 0.55rem;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      background: var(--ok-bg);
      color: var(--ok);
      border: 1px solid color-mix(in srgb, var(--ok) 35%, transparent);
    }
    .status-pill.is-error {
      background: var(--danger-bg);
      color: var(--danger);
      border-color: color-mix(in srgb, var(--danger) 35%, transparent);
    }

    .header-actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.45rem;
    }
    button, .btn {
      font: inherit;
      font-size: 0.85rem;
      font-weight: 600;
      padding: 0.42rem 0.78rem;
      border-radius: 0.5rem;
      border: 1px solid var(--border);
      background: var(--surface-2);
      color: var(--text);
      cursor: pointer;
    }
    button:hover:not(:disabled) {
      border-color: var(--brand);
    }
    button.primary {
      background: var(--brand);
      color: var(--brand-text);
      border-color: var(--brand);
    }
    button:disabled {
      opacity: 0.48;
      cursor: not-allowed;
    }

    .workspace {
      display: grid;
      grid-template-columns: 18.5rem minmax(0, 1fr);
      min-height: 0;
    }

    .config-panel {
      display: flex;
      flex-direction: column;
      min-height: 0;
      min-width: 0;
      background: var(--surface);
      border-inline-end: 1px solid var(--border);
    }

    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      padding: 0.5rem 0.8rem;
      min-height: 2.65rem;
      background: var(--surface-2);
      border-block-end: 1px solid var(--border);
      font-size: 0.85rem;
      font-weight: 700;
    }

    .config-scroll {
      flex: 1;
      overflow-x: hidden;
      overflow-y: auto;
      padding: 0.85rem;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 1rem;
      align-content: start;
    }

    .section-title {
      margin: 0 0 0.55rem;
      font-size: 0.8rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
    }

    .field {
      display: grid;
      gap: 0.2rem;
      margin-block-end: 0.65rem;
    }
    .field label {
      font-size: 0.82rem;
      font-weight: 600;
    }
    .hint {
      font-size: 0.75rem;
      color: var(--muted);
    }
    .field input[type="number"],
    .field textarea,
    .panel-header select {
      font: inherit;
      font-size: 0.84rem;
      padding: 0.38rem 0.55rem;
      border-radius: 0.45rem;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      width: 100%;
    }
    .field textarea {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      resize: vertical;
    }
    .check-row {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.83rem;
      margin-block: 0.4rem 0.65rem;
    }
    .adv-params summary {
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--muted);
      cursor: pointer;
      margin-block-end: 0.45rem;
    }

    .error-msg {
      display: none;
      font-size: 0.77rem;
      color: var(--danger);
      margin-top: 0.15rem;
    }
    :is(:user-invalid, .user-invalid-fallback) {
      border-color: var(--danger) !important;
      background-color: var(--danger-bg) !important;
    }
    :is(:user-invalid, .user-invalid-fallback) + .error-msg {
      display: block;
    }
    :is(:user-valid, .user-valid-fallback) {
      border-color: var(--ok);
    }

    .jobs-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 0.45rem;
    }
    .jobs-list {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 0.45rem;
    }
    .job-card {
      text-align: left;
      padding: 0.5rem 0.6rem;
      border-radius: 0.5rem;
      border: 1px solid var(--border);
      background: var(--surface-2);
      display: grid;
      min-width: 0;
      gap: 0.2rem;
      width: 100%;
      font-weight: 400;
    }
    .job-card.is-active {
      border-color: var(--brand);
      outline: 1px solid var(--brand);
    }
    .job-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      min-width: 0;
      gap: 0.4rem;
      font-size: 0.8rem;
      font-weight: 600;
    }
    .job-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .job-meta {
      font-size: 0.74rem;
      color: var(--muted);
      display: flex;
      justify-content: space-between;
      gap: 0.35rem;
    }
    .job-badge {
      font-size: 0.7rem;
      padding: 0.05rem 0.38rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      flex: none;
    }
    .job-badge.completed, .job-badge.skipped { color: var(--ok); border-color: var(--ok); }
    .job-badge.failed { color: var(--danger); border-color: var(--danger); }

    .about-card {
      font-size: 0.79rem;
      color: var(--muted);
      padding: 0.65rem;
      border-radius: 0.5rem;
      background: var(--surface-2);
      border: 1px solid var(--border);
      overflow-wrap: anywhere;
    }
    .about-card p { margin: 0 0 0.4rem; }
    .about-card p:last-child { margin-bottom: 0; }

    .document-panels {
      display: grid;
      grid-template-columns: minmax(14rem, var(--left-ratio)) 8px minmax(14rem, 1fr);
      min-height: 0;
      min-width: 0;
    }
    .file-panel, .result-panel {
      display: flex;
      flex-direction: column;
      min-height: 0;
      min-width: 0;
      background: var(--surface);
    }

    .splitter {
      background: var(--border);
      cursor: col-resize;
      position: relative;
    }
    .splitter:hover, .splitter:focus-visible {
      background: var(--brand);
      outline: none;
    }

    .file-tools {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      font-weight: 500;
      font-size: 0.8rem;
    }
    .file-tools select {
      max-width: 14rem;
      width: auto;
      padding: 0.2rem 0.4rem;
    }
    .inline-check {
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      cursor: pointer;
    }

    .file-viewport {
      flex: 1;
      overflow: auto;
      padding: 0.9rem;
      display: grid;
      gap: 0.9rem;
      align-content: start;
    }

    .upload-dropzone {
      width: 100%;
      min-height: 15rem;
      border: 2px dashed var(--border);
      border-radius: 0.75rem;
      background: var(--surface-2);
      color: var(--muted);
      display: grid;
      place-content: center;
      justify-items: center;
      gap: 0.45rem;
      padding: 1.5rem;
      text-align: center;
    }
    .upload-dropzone.is-compact {
      min-height: auto;
      padding: 0.7rem 1rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .upload-dropzone:hover {
      border-color: var(--brand);
      color: var(--text);
    }
    .upload-title { font-size: 0.95rem; font-weight: 700; color: var(--text); }

    .page-card {
      margin: 0;
      border: 1px solid var(--border);
      border-radius: 0.65rem;
      overflow: hidden;
      background: var(--surface-2);
    }
    .page-cap {
      padding: 0.35rem 0.65rem;
      font-size: 0.78rem;
      font-weight: 600;
      color: var(--muted);
      border-block-end: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
    }
    .page-stage {
      position: relative;
      background: #fff;
      line-height: 0;
    }
    .page-img {
      display: block;
      width: 100%;
      height: auto;
    }
    .overlay-svg {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
    }
    .overlay-svg.is-hidden { display: none; }
    .bbox-group { pointer-events: auto; cursor: pointer; }
    .bbox-rect {
      fill-opacity: 0.1;
      stroke-width: 2.2;
      vector-effect: non-scaling-stroke;
    }
    .bbox-group.is-selected .bbox-rect {
      fill-opacity: 0.25;
      stroke-width: 4;
    }
    .bbox-tag {
      font-size: 17px;
      font-weight: 700;
      font-family: ui-monospace, SFMono-Regular, monospace;
      fill: #ffffff;
    }

    .tabs {
      display: flex;
      gap: 0.25rem;
      overflow-x: auto;
      align-items: stretch;
    }
    [role="tab"] {
      position: relative;
      flex: none;
      border: 0;
      border-radius: 0.35rem 0.35rem 0 0;
      background: transparent;
      color: var(--muted);
      padding: 0.38rem 0.75rem;
      font-size: 0.85rem;
      font-weight: 600;
    }
    [role="tab"][aria-selected="true"] {
      color: var(--brand);
    }
    [role="tab"][aria-selected="true"]::after {
      content: "";
      position: absolute;
      inset: auto 0 0;
      border-block-end: 3px solid var(--brand);
    }

    .panel-stack {
      display: grid;
      flex: 1;
      min-height: 0;
    }
    [role="tabpanel"] {
      grid-area: 1 / 1;
      min-height: 0;
      overflow: auto;
    }
    .panel-inner {
      padding: 1rem 1.15rem;
    }

    .empty-result {
      padding: 3rem 1rem;
      text-align: center;
      color: var(--muted);
      font-size: 0.92rem;
    }

    .md-body {
      font-size: 0.93rem;
      line-height: 1.68;
      overflow-wrap: anywhere;
    }
    .md-body h1, .md-body h2, .md-body h3, .md-body h4 {
      margin: 1.1em 0 0.45em;
      line-height: 1.3;
    }
    .md-body h1 { font-size: 1.45rem; border-block-end: 1px solid var(--border); padding-block-end: 0.25rem; }
    .md-body h2 { font-size: 1.22rem; }
    .md-body h3 { font-size: 1.08rem; }
    .md-body pre {
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 0.5rem;
      padding: 0.75rem;
      overflow-x: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.83rem;
    }
    .md-body code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      background: var(--surface-2);
      padding: 0.08rem 0.3rem;
      border-radius: 0.25rem;
      font-size: 0.87em;
    }
    .md-body pre code { background: transparent; padding: 0; }
    .md-body blockquote {
      margin: 0.7rem 0;
      padding: 0.3rem 0.85rem;
      border-inline-start: 3px solid var(--brand);
      background: var(--surface-2);
      color: var(--muted);
    }
    .table-wrap {
      overflow-x: auto;
      margin: 0.85rem 0;
    }
    .md-body table {
      border-collapse: collapse;
      width: 100%;
      font-size: 0.84rem;
    }
    .md-body th, .md-body td {
      border: 1px solid var(--border);
      padding: 0.38rem 0.55rem;
      text-align: left;
    }
    .md-body th { background: var(--surface-2); font-weight: 700; }
    .md-body img {
      max-width: 100%;
      height: auto;
      border-radius: 0.4rem;
      border: 1px solid var(--border);
      margin-block: 0.45rem;
    }

    .source-pre {
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.83rem;
      line-height: 1.55;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 0.55rem;
      padding: 0.9rem;
    }

    .layout-list {
      display: grid;
      gap: 0.55rem;
    }
    .block-card {
      border: 1px solid var(--border);
      border-radius: 0.55rem;
      padding: 0.55rem 0.7rem;
      background: var(--surface-2);
      cursor: pointer;
      display: grid;
      gap: 0.28rem;
      text-align: left;
      width: 100%;
      font-weight: 400;
    }
    .block-card.is-selected {
      border-color: var(--brand);
      outline: 2px solid color-mix(in srgb, var(--brand) 50%, transparent);
    }
    .block-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.45rem;
      font-size: 0.78rem;
    }
    .block-order {
      display: inline-grid;
      place-items: center;
      min-width: 1.45rem;
      height: 1.45rem;
      padding: 0 0.3rem;
      border-radius: 999px;
      background: var(--brand);
      color: var(--brand-text);
      font-weight: 700;
      font-size: 0.74rem;
    }
    .block-label { font-weight: 700; }
    .block-coords {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      color: var(--muted);
      font-size: 0.72rem;
    }
    .block-snippet {
      font-size: 0.82rem;
      color: var(--text);
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .raw-details { margin-top: 1rem; }
    .raw-details summary { cursor: pointer; font-weight: 600; font-size: 0.84rem; color: var(--muted); margin-bottom: 0.5rem; }

    .drop-overlay {
      position: fixed;
      inset: 0;
      z-index: 50;
      display: grid;
      place-items: center;
      background: color-mix(in srgb, var(--bg) 82%, transparent);
      backdrop-filter: blur(4px);
    }
    .drop-overlay[hidden] { display: none; }
    .drop-box {
      padding: 2rem 3rem;
      border-radius: 1rem;
      border: 2px dashed var(--brand);
      background: var(--surface);
      text-align: center;
      display: grid;
      gap: 0.35rem;
    }

    .progress-toast {
      position: fixed;
      right: 1.1rem;
      bottom: 1.1rem;
      z-index: 40;
      min-width: 16.5rem;
      max-width: 24rem;
      padding: 0.75rem 0.95rem;
      border-radius: 0.7rem;
      background: var(--surface);
      border: 1px solid var(--border);
      box-shadow: 0 8px 24px rgb(0 0 0 / 0.16);
      font-size: 0.83rem;
    }
    .progress-toast[hidden] { display: none; }
    .progress-steps {
      margin: 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 0.38rem;
    }
    .progress-steps li {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      color: var(--muted);
    }
    .progress-steps li.is-active { color: var(--text); font-weight: 700; }
    .progress-steps li.is-done { color: var(--ok); }
    .progress-steps li.is-fail { color: var(--danger); }
    .dot {
      width: 0.58rem;
      height: 0.58rem;
      border-radius: 999px;
      background: currentColor;
      flex: none;
    }

    .banner-err {
      padding: 0.65rem 0.8rem;
      border-radius: 0.5rem;
      background: var(--danger-bg);
      color: var(--danger);
      border: 1px solid color-mix(in srgb, var(--danger) 40%, transparent);
      font-size: 0.83rem;
      margin-bottom: 0.85rem;
    }

    @media (max-width: 960px) {
      .workspace {
        grid-template-columns: 1fr;
        grid-template-rows: auto 1fr;
      }
      .config-panel {
        border-inline-end: 0;
        border-block-end: 1px solid var(--border);
        max-height: 40vh;
      }
      .document-panels {
        grid-template-columns: 1fr;
        grid-template-rows: minmax(14rem, 1fr) minmax(16rem, 1fr);
      }
      .splitter { display: none; }
    }
  </style>
</head>
<body>
  <main class="app-shell">
    <header class="app-header">
      <div class="brand">
        <h1>Unlimited-<span class="brand-accent">OCR 2</span> Online Demo</h1>
        <span class="status-pill" id="health-pill">连接中...</span>
      </div>
      <div class="header-actions">
        <button type="button" class="primary" id="pick-file-btn">上传 PDF / 图片</button>
        <button type="button" id="copy-md-btn" disabled>复制 Markdown</button>
        <button type="button" id="dl-md-btn" disabled>下载 Markdown</button>
        <button type="button" id="dl-raw-btn" disabled>下载 Raw</button>
      </div>
    </header>

    <div class="workspace">
      <aside class="config-panel" aria-label="参数与历史任务">
        <div class="panel-header"><span>识别配置与任务</span></div>
        <div class="config-scroll scroller">
          <form id="ocr-form" novalidate>
            <h2 class="section-title">转换参数</h2>
            <div class="field">
              <label for="opt-dpi">DPI</label>
              <span id="dpi-hint" class="hint">范围 72–400（默认 200）</span>
              <input id="opt-dpi" name="dpi" type="number" min="72" max="400" step="1" value="200" required
                     aria-describedby="dpi-hint" data-error="dpi-error">
              <div id="dpi-error" class="error-msg"><span role="img" aria-label="错误">⚠</span> 请输入 72–400 的整数 DPI。</div>
            </div>

            <div class="field">
              <label for="opt-chunk">每组页数 (pages_per_chunk)</label>
              <span id="chunk-hint" class="hint">范围 1–100（默认 10，越大越占显存）</span>
              <input id="opt-chunk" name="pages_per_chunk" type="number" min="1" max="100" step="1" value="10" required
                     aria-describedby="chunk-hint" data-error="chunk-error">
              <div id="chunk-error" class="error-msg"><span role="img" aria-label="错误">⚠</span> 请输入 1–100 的整数页数。</div>
            </div>

            <div class="field">
              <label for="opt-tokens">每页 Token 上限 (tokens_per_page)</label>
              <span id="tokens-hint" class="hint">范围 32–20000（默认 8000）</span>
              <input id="opt-tokens" name="tokens_per_page" type="number" min="32" max="20000" step="1" value="8000" required
                     aria-describedby="tokens-hint" data-error="tokens-error">
              <div id="tokens-error" class="error-msg"><span role="img" aria-label="错误">⚠</span> 请输入 32–20000 的整数。</div>
            </div>

            <label class="check-row">
              <input id="opt-overwrite" type="checkbox">
              <span>强制重跑已有结果 (overwrite)</span>
            </label>

            <details class="adv-params">
              <summary>高级推理参数 (inference_params JSON)</summary>
              <div class="field">
                <label for="opt-params">JSON 对象（留空使用默认）</label>
                <span id="params-hint" class="hint">例: {"temperature": 0.0, "crop_mode": false}</span>
                <textarea id="opt-params" rows="3" autocorrect="off" autocapitalize="none" spellcheck="false"
                          placeholder="{}" aria-describedby="params-hint" data-error="params-error"></textarea>
                <div id="params-error" class="error-msg"><span role="img" aria-label="错误">⚠</span> 请输入合法的 JSON 对象。</div>
              </div>
            </details>
          </form>

          <section aria-label="最近任务">
            <div class="jobs-head">
              <h2 class="section-title" style="margin:0">最近任务 (Drive)</h2>
              <button type="button" id="refresh-jobs-btn">刷新</button>
            </div>
            <div class="jobs-list" id="jobs-list">
              <div class="hint">正在加载任务列表...</div>
            </div>
          </section>

          <section class="about-card" aria-label="关于模型">
            <p><strong>模型</strong>：baidu/Unlimited-OCR-2（Transformers BF16）</p>
            <p>上传的文件分块写入 Google Drive 并持久化保存结果、裁剪图片与版面块坐标。</p>
          </section>
        </div>
      </aside>

      <div class="document-panels" id="document-panels">
        <section class="file-panel" aria-label="原文件与版面">
          <div class="panel-header">
            <span id="file-panel-title">原文件</span>
            <div class="file-tools">
              <select id="file-select" aria-label="选择任务中的文件" hidden></select>
              <label class="inline-check">
                <input type="checkbox" id="toggle-overlay" checked>
                <span>版面框</span>
              </label>
              <span id="page-count-badge" class="hint"></span>
            </div>
          </div>
          <div class="file-viewport scroller" id="file-viewport" tabindex="0">
            <input id="file-input" type="file" hidden accept=".pdf,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff">
            <button type="button" class="upload-dropzone" id="upload-dropzone">
              <span class="upload-title">拖拽 PDF 或图片到此处测试</span>
              <span>或点击选择本地文件（支持 PDF / PNG / JPEG / WEBP / BMP / TIFF）</span>
            </button>
            <div id="file-pages"></div>
          </div>
        </section>

        <div class="splitter" id="splitter" role="separator" aria-label="调整左右面板宽度"
             aria-orientation="vertical" aria-valuemin="20" aria-valuemax="80" aria-valuenow="50" tabindex="0"></div>

        <section class="result-panel" aria-label="解析结果">
          <div class="panel-header">
            <div class="tabs" id="result-tabs" role="tablist" focusgroup="tablist nomemory" aria-label="解析结果视图">
              <button type="button" class="tab" role="tab" id="tab-preview" aria-selected="true" focusgroupstart data-tab="preview">预览</button>
              <button type="button" class="tab" role="tab" id="tab-source" aria-selected="false" data-tab="source">源码</button>
              <button type="button" class="tab" role="tab" id="tab-layout" aria-selected="false" data-tab="layout">版面</button>
            </div>
            <div class="file-tools">
              <span id="result-meta" class="hint"></span>
              <button type="button" id="resume-job-btn" hidden>断点恢复</button>
            </div>
          </div>
          <div class="panel-stack">
            <div role="tabpanel" id="panel-preview" class="scroller" aria-labelledby="tab-preview" tabindex="0">
              <div class="panel-inner" id="preview-inner">
                <div class="empty-result">上传文件或选择左侧已有任务，即可查看渲染后的 Markdown 与表格结果。</div>
              </div>
            </div>
            <div role="tabpanel" id="panel-source" class="scroller" aria-labelledby="tab-source" hidden="until-found">
              <div class="panel-inner" id="source-inner">
                <div class="empty-result">暂无 Markdown 源码。</div>
              </div>
            </div>
            <div role="tabpanel" id="panel-layout" class="scroller" aria-labelledby="tab-layout" hidden="until-found">
              <div class="panel-inner" id="layout-inner">
                <div class="empty-result">暂无版面块与阅读顺序数据。</div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  </main>

  <div class="drop-overlay" id="drop-overlay" hidden>
    <div class="drop-box">
      <strong>释放文件以开始 Unlimited-OCR 2 识别</strong>
      <span class="hint">支持 PDF / PNG / JPEG / WEBP / BMP / TIFF</span>
    </div>
  </div>

  <div class="progress-toast" id="progress-toast" role="status" aria-live="polite" aria-atomic="true" hidden>
    <ol class="progress-steps">
      <li id="step-upload"><span class="dot" aria-hidden="true"></span><span id="step-upload-text">等待上传...</span></li>
      <li id="step-parse"><span class="dot" aria-hidden="true"></span><span id="step-parse-text">等待推理...</span></li>
    </ol>
  </div>

  <script nonce="__NONCE__">
    (() => {
      const qs = (id) => document.getElementById(id);
      const state = {
        job: null,
        fileIndex: 0,
        preview: null,
        selectedBlock: null,
        busy: false,
      };

      const LABEL_COLORS = {
        title: "#c86300",
        text: "#0b57d0",
        table: "#0f766e",
        image: "#7c3aed",
        figure: "#7c3aed",
        chart: "#7c3aed",
        equation: "#be185d",
        algorithm: "#4338ca",
        header: "#64748b",
        footer: "#64748b",
        page_number: "#64748b",
      };
      const colorForLabel = (label) => LABEL_COLORS[label] || "#2563eb";

      // --- Pure-JS SHA-256 fallback when crypto.subtle is unavailable (e.g. non-localhost HTTP) ---
      const K256 = new Uint32Array([
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
      ]);
      function sha256FallbackHex(bytes) {
        const len = bytes.length;
        const bitLen = len * 8;
        const total = ((len + 9 + 63) >> 6) << 6;
        const buf = new Uint8Array(total);
        buf.set(bytes);
        buf[len] = 0x80;
        const view = new DataView(buf.buffer);
        view.setUint32(total - 8, Math.floor(bitLen / 0x100000000), false);
        view.setUint32(total - 4, bitLen >>> 0, false);
        let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
        let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
        const w = new Uint32Array(64);
        const rotr = (x, n) => (x >>> n) | (x << (32 - n));
        for (let off = 0; off < total; off += 64) {
          for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4, false);
          for (let i = 16; i < 64; i++) {
            const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
            const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
            w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
          }
          let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
          for (let i = 0; i < 64; i++) {
            const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            const ch = (e & f) ^ ((~e) & g);
            const t1 = (h + S1 + ch + K256[i] + w[i]) >>> 0;
            const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            const maj = (a & b) ^ (a & c) ^ (b & c);
            const t2 = (S0 + maj) >>> 0;
            h = g; g = f; f = e; e = (d + t1) >>> 0;
            d = c; c = b; b = a; a = (t1 + t2) >>> 0;
          }
          h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0;
          h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0; h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0;
        }
        return [h0, h1, h2, h3, h4, h5, h6, h7].map((v) => v.toString(16).padStart(8, "0")).join("");
      }
      async function sha256Hex(arrayBuffer) {
        if (globalThis.crypto && crypto.subtle && typeof crypto.subtle.digest === "function") {
          const digest = await crypto.subtle.digest("SHA-256", arrayBuffer);
          return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
        }
        return sha256FallbackHex(new Uint8Array(arrayBuffer));
      }
      function bytesToBase64(bytes) {
        let bin = "";
        for (let i = 0; i < bytes.length; i += 0x8000) {
          bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        }
        return btoa(bin);
      }

      // --- Form validation after interaction + ARIA error announcement ---
      const form = qs("ocr-form");
      const paramsTextarea = qs("opt-params");
      function validateJsonField() {
        const raw = paramsTextarea.value.trim();
        if (!raw) {
          paramsTextarea.setCustomValidity("");
          return;
        }
        try {
          const parsed = JSON.parse(raw);
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            paramsTextarea.setCustomValidity("必须是 JSON 对象");
          } else {
            paramsTextarea.setCustomValidity("");
          }
        } catch {
          paramsTextarea.setCustomValidity("JSON 语法错误");
        }
      }
      paramsTextarea.addEventListener("input", validateJsonField);

      const announcer = document.createElement("div");
      announcer.setAttribute("aria-live", "polite");
      announcer.className = "visually-hidden";
      document.body.append(announcer);
      let announceTimer;
      let announcedField;
      const flagged = new WeakSet();
      const announced = new WeakSet();
      const dirtyState = new WeakMap();
      const supportsUserInvalid = CSS.supports("selector(:user-invalid)");

      const linkError = (el, shown) => {
        const errorId = el.dataset.error;
        if (!errorId) return;
        const ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== errorId);
        if (shown) ids.push(errorId);
        if (ids.length) el.setAttribute("aria-describedby", ids.join(" "));
        else el.removeAttribute("aria-describedby");
      };
      const announceError = (el) => {
        const errEl = qs(el.dataset.error);
        if (!errEl) return;
        const label = el.labels && el.labels[0] ? el.labels[0].textContent.trim() + ": " : "";
        clearTimeout(announceTimer);
        announcer.textContent = "";
        announcedField = el;
        announceTimer = setTimeout(() => { announcer.textContent = label + errEl.textContent.trim(); }, 450);
      };
      const isFieldUserInvalid = (el) => {
        if (supportsUserInvalid) return el.matches(":user-invalid");
        const st = dirtyState.get(el);
        return Boolean(st && st.hasBlurred && !el.validity.valid);
      };
      const syncAria = (el, allowAnnounce = false) => {
        if (!el || !el.matches || !el.matches("input, select, textarea")) return;
        if (el === paramsTextarea) validateJsonField();
        const invalid = isFieldUserInvalid(el);
        if (!supportsUserInvalid) {
          const st = dirtyState.get(el);
          el.classList.toggle("user-invalid-fallback", invalid);
          el.classList.toggle("user-valid-fallback", Boolean(st && st.hasBlurred && el.validity.valid));
        }
        if (invalid) {
          if (allowAnnounce && !announced.has(el)) {
            announceError(el);
            announced.add(el);
          }
          el.setAttribute("aria-invalid", "true");
          linkError(el, true);
          flagged.add(el);
        } else if (flagged.has(el) || el.getAttribute("aria-invalid") !== "true") {
          el.setAttribute("aria-invalid", "false");
          linkError(el, false);
          flagged.delete(el);
          announced.delete(el);
          if (announcedField === el) {
            clearTimeout(announceTimer);
            announcer.textContent = "";
          }
        }
      };
      form.querySelectorAll("input, textarea").forEach((el) => syncAria(el));
      form.addEventListener("blur", (e) => {
        if (!supportsUserInvalid && e.target.checkValidity) {
          const st = dirtyState.get(e.target) || { hasInteracted: false, hasBlurred: false };
          st.hasBlurred = true;
          dirtyState.set(e.target, st);
        }
        syncAria(e.target, true);
      }, true);
      form.addEventListener("input", (e) => {
        if (!supportsUserInvalid && e.target.checkValidity) {
          const st = dirtyState.get(e.target) || { hasInteracted: false, hasBlurred: false };
          st.hasInteracted = true;
          dirtyState.set(e.target, st);
        }
        if (e.target.hasAttribute("aria-invalid")) syncAria(e.target);
      });
      form.addEventListener("invalid", (e) => {
        if (!supportsUserInvalid) dirtyState.set(e.target, { hasInteracted: true, hasBlurred: true });
        announced.add(e.target);
        syncAria(e.target);
      }, true);

      // --- Accessible Tabs (focusgroup + fallback + hidden="until-found" + beforematch) ---
      const tablist = qs("result-tabs");
      const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
      const panels = {
        preview: qs("panel-preview"),
        source: qs("panel-source"),
        layout: qs("panel-layout"),
      };
      const hasFocusGroup = Object.hasOwn(HTMLElement.prototype, "focusGroup");
      function selectTab(targetTab, { focusTab = false } = {}) {
        const name = targetTab.dataset.tab;
        for (const tab of tabs) {
          const active = tab === targetTab;
          tab.setAttribute("aria-selected", active ? "true" : "false");
          tab.toggleAttribute("focusgroupstart", active);
          if (!hasFocusGroup) tab.tabIndex = active ? 0 : -1;
        }
        for (const [key, panel] of Object.entries(panels)) {
          const active = key === name;
          if (active) {
            panel.removeAttribute("hidden");
            panel.setAttribute("tabindex", "0");
          } else {
            panel.setAttribute("hidden", "until-found");
            panel.removeAttribute("tabindex");
          }
        }
        targetTab.scrollIntoView({ block: "nearest", inline: "nearest", container: "nearest" });
        if (focusTab) targetTab.focus();
      }
      if (!hasFocusGroup) {
        tabs.forEach((t, idx) => { t.tabIndex = idx === 0 ? 0 : -1; });
        tablist.addEventListener("keydown", (e) => {
          const cur = tabs.indexOf(document.activeElement);
          if (cur < 0) return;
          let next = -1;
          if (e.key === "ArrowRight") next = (cur + 1) % tabs.length;
          else if (e.key === "ArrowLeft") next = (cur - 1 + tabs.length) % tabs.length;
          else if (e.key === "Home") next = 0;
          else if (e.key === "End") next = tabs.length - 1;
          if (next >= 0) {
            e.preventDefault();
            selectTab(tabs[next], { focusTab: true });
          }
        });
      }
      tablist.addEventListener("click", (e) => {
        const btn = e.target.closest('[role="tab"]');
        if (btn) selectTab(btn);
      });
      tablist.addEventListener("focusin", (e) => {
        const btn = e.target.closest('[role="tab"]');
        if (btn && btn.getAttribute("aria-selected") !== "true") selectTab(btn);
      });
      for (const [key, panel] of Object.entries(panels)) {
        panel.addEventListener("beforematch", () => {
          const tab = tabs.find((t) => t.dataset.tab === key);
          if (tab) selectTab(tab);
        });
      }

      // --- Safe HTML table sanitization (Element.setHTML + DOMParser whitelist fallback) ---
      const TABLE_TAGS = new Set([
        "table", "thead", "tbody", "tfoot", "tr", "th", "td",
        "caption", "colgroup", "col",
        "br", "sup", "sub", "b", "strong", "i", "em", "code", "p", "span",
      ]);
      function cloneSafeNode(node) {
        if (node.nodeType === Node.TEXT_NODE) return document.createTextNode(node.textContent || "");
        if (node.nodeType !== Node.ELEMENT_NODE) return null;
        const tag = node.tagName.toLowerCase();
        if (!TABLE_TAGS.has(tag)) {
          const frag = document.createDocumentFragment();
          for (const child of node.childNodes) {
            const c = cloneSafeNode(child);
            if (c) frag.append(c);
          }
          return frag;
        }
        const el = document.createElement(tag);
        if (tag === "td" || tag === "th") {
          for (const attr of ["colspan", "rowspan"]) {
            const val = node.getAttribute(attr);
            if (val && /^[0-9]{1,3}$/.test(val.trim())) el.setAttribute(attr, val.trim());
          }
          const align = (node.getAttribute("align") || "").trim().toLowerCase();
          if (align === "left" || align === "center" || align === "right") el.setAttribute("align", align);
        }
        for (const child of node.childNodes) {
          const c = cloneSafeNode(child);
          if (c) el.append(c);
        }
        return el;
      }
      function appendSafeTable(container, tableHtml) {
        const wrap = document.createElement("div");
        wrap.className = "table-wrap";
        if ("setHTML" in Element.prototype) {
          wrap.setHTML(tableHtml, {
            sanitizer: {
              elements: Array.from(TABLE_TAGS),
              attributes: ["colspan", "rowspan", "align"],
              replaceWithChildrenElements: ["div", "font", "center"],
            },
          });
        } else {
          const doc = new DOMParser().parseFromString(tableHtml, "text/html");
          if (doc.body) {
            for (const child of doc.body.childNodes) {
              const safe = cloneSafeNode(child);
              if (safe) wrap.append(safe);
            }
          }
        }
        container.append(wrap);
      }

      // --- Safe Markdown DOM builder (DOM nodes only) ---
      function resolveAssetUrl(jobId, fileIndex, rel) {
        if (!rel || /^([a-z]+:|\/\/|\/)/i.test(rel)) return null;
        const parts = rel.split("/").filter(Boolean);
        if (!parts.length || parts.includes("..")) return null;
        return `/api/jobs/${encodeURIComponent(jobId)}/files/${fileIndex}/asset/${parts.map(encodeURIComponent).join("/")}`;
      }
      function appendInlineNodes(parent, text, jobId, fileIndex) {
        const pattern = /(!\[([^\]]*)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*)/g;
        let last = 0;
        for (const m of text.matchAll(pattern)) {
          if (m.index > last) parent.append(document.createTextNode(text.slice(last, m.index)));
          if (m[2] !== undefined) {
            const url = resolveAssetUrl(jobId, fileIndex, m[3].trim());
            if (url) {
              const img = document.createElement("img");
              img.src = url;
              img.alt = m[2] || "识别插图";
              img.loading = "lazy";
              parent.append(img);
            } else {
              parent.append(document.createTextNode(`[图片: ${m[2] || m[3]}]`));
            }
          } else if (m[4] !== undefined) {
            const code = document.createElement("code");
            code.textContent = m[4];
            parent.append(code);
          } else if (m[5] !== undefined) {
            const strong = document.createElement("strong");
            strong.textContent = m[5];
            parent.append(strong);
          } else if (m[6] !== undefined) {
            const em = document.createElement("em");
            em.textContent = m[6];
            parent.append(em);
          }
          last = m.index + m[0].length;
        }
        if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
      }
      function renderMarkdownSafe(container, markdown, jobId, fileIndex) {
        container.replaceChildren();
        const lines = (markdown || "").replace(/\r\n/g, "\n").split("\n");
        let i = 0;
        while (i < lines.length) {
          const line = lines[i];
          if (!line.trim()) { i++; continue; }
          if (line.trimStart().startsWith("```")) {
            const codeLines = [];
            i++;
            while (i < lines.length && !lines[i].trimStart().startsWith("```")) {
              codeLines.push(lines[i]);
              i++;
            }
            if (i < lines.length) i++;
            const pre = document.createElement("pre");
            const code = document.createElement("code");
            code.textContent = codeLines.join("\n");
            pre.append(code);
            container.append(pre);
            continue;
          }
          if (/^\s*<table\b/i.test(line)) {
            const tblLines = [line];
            if (!/<\/table>/i.test(line)) {
              i++;
              while (i < lines.length) {
                tblLines.push(lines[i]);
                if (/<\/table>/i.test(lines[i])) break;
                i++;
              }
            }
            i++;
            appendSafeTable(container, tblLines.join("\n"));
            continue;
          }
          const h = line.match(/^(#{1,6})\s+(.*)$/);
          if (h) {
            const tag = "h" + Math.min(6, h[1].length);
            const el = document.createElement(tag);
            appendInlineNodes(el, h[2], jobId, fileIndex);
            container.append(el);
            i++;
            continue;
          }
          if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
            container.append(document.createElement("hr"));
            i++;
            continue;
          }
          if (line.startsWith(">")) {
            const bq = document.createElement("blockquote");
            const buf = [];
            while (i < lines.length && lines[i].startsWith(">")) {
              buf.push(lines[i].replace(/^>\s?/, ""));
              i++;
            }
            appendInlineNodes(bq, buf.join(" "), jobId, fileIndex);
            container.append(bq);
            continue;
          }
          const listMatch = line.match(/^\s*([-*+]|[0-9]+\.)\s+(.*)$/);
          if (listMatch) {
            const ordered = /^[0-9]+\./.test(listMatch[1]);
            const listEl = document.createElement(ordered ? "ol" : "ul");
            const itemRe = ordered ? /^\s*[0-9]+\.\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
            while (i < lines.length) {
              const m = lines[i].match(itemRe);
              if (!m) break;
              const li = document.createElement("li");
              appendInlineNodes(li, m[1], jobId, fileIndex);
              listEl.append(li);
              i++;
            }
            container.append(listEl);
            continue;
          }
          const para = [];
          while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|>|```|\s*<table\b|\s*([-*+]|[0-9]+\.)\s)/i.test(lines[i])) {
            para.push(lines[i]);
            i++;
          }
          const p = document.createElement("p");
          appendInlineNodes(p, para.join(" "), jobId, fileIndex);
          container.append(p);
        }
      }

      // --- API helpers ---
      async function apiJson(url, opts = {}) {
        const res = await fetch(url, opts);
        const text = await res.text();
        let data;
        try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        return data;
      }

      async function checkHealth() {
        const pill = qs("health-pill");
        try {
          const info = await apiJson("/health");
          pill.textContent = info.model || "已就绪";
          pill.classList.remove("is-error");
        } catch {
          pill.textContent = "服务未就绪";
          pill.classList.add("is-error");
        }
      }

      function formatTime(ts) {
        if (!ts) return "";
        const d = new Date(ts * 1000);
        return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      }

      async function loadJobs(selectFirstIfIdle = false) {
        const container = qs("jobs-list");
        try {
          const data = await apiJson("/api/jobs");
          const jobs = data.jobs || [];
          container.replaceChildren();
          if (!jobs.length) {
            const empty = document.createElement("div");
            empty.className = "hint";
            empty.textContent = "暂无识别记录，请先上传文件。";
            container.append(empty);
            return;
          }
          for (const j of jobs) {
            const card = document.createElement("button");
            card.type = "button";
            card.className = "job-card" + (state.job && state.job.id === j.id ? " is-active" : "");
            const top = document.createElement("div");
            top.className = "job-top";
            const first = (j.files && j.files[0]) || {};
            const nameSpan = document.createElement("span");
            nameSpan.className = "job-name";
            nameSpan.textContent = (j.files && j.files.length > 1)
              ? `${first.filename || j.id.slice(0, 8)} (+${j.files.length - 1})`
              : (first.filename || j.id.slice(0, 8));
            const badge = document.createElement("span");
            badge.className = `job-badge ${j.status || ""}`;
            badge.textContent = j.status || "";
            top.append(nameSpan, badge);
            const meta = document.createElement("div");
            meta.className = "job-meta";
            const idSpan = document.createElement("span");
            idSpan.textContent = `${j.id.slice(0, 8)} · DPI ${j.options?.dpi ?? 200}`;
            const timeSpan = document.createElement("span");
            timeSpan.textContent = formatTime(j.created);
            meta.append(idSpan, timeSpan);
            card.append(top, meta);
            card.addEventListener("click", () => openJob(j, 0));
            container.append(card);
          }
          if (selectFirstIfIdle && !state.job && !state.busy && jobs[0]) {
            await openJob(jobs[0], 0);
          }
        } catch (err) {
          container.replaceChildren();
          const msg = document.createElement("div");
          msg.className = "hint";
          msg.textContent = `加载任务失败: ${err.message}`;
          container.append(msg);
        }
      }

      function highlightBlock(blockIdx) {
        state.selectedBlock = blockIdx;
        document.querySelectorAll(".bbox-group").forEach((g) => {
          g.classList.toggle("is-selected", Number(g.dataset.block) === blockIdx);
        });
        document.querySelectorAll(".block-card").forEach((c) => {
          const match = Number(c.dataset.block) === blockIdx;
          c.classList.toggle("is-selected", match);
          if (match) c.scrollIntoView({ block: "nearest" });
        });
      }

      function renderPages(preview) {
        const pagesWrap = qs("file-pages");
        const dropzone = qs("upload-dropzone");
        pagesWrap.replaceChildren();
        const count = Number(preview?.page_count || 0);
        qs("page-count-badge").textContent = count ? `共 ${count} 页` : "";
        dropzone.classList.toggle("is-compact", Boolean(preview));
        if (!preview) return;
        if (!preview.source_available) {
          const note = document.createElement("div");
          note.className = "hint";
          note.textContent = "原始文件已不在 Drive 中，仅展示识别结果。";
          pagesWrap.append(note);
          return;
        }
        const byPage = new Map();
        for (const blk of preview.blocks || []) {
          if (!blk.bbox) continue;
          if (!byPage.has(blk.page)) byPage.set(blk.page, []);
          byPage.get(blk.page).push(blk);
        }
        const showBoxes = qs("toggle-overlay").checked;
        const pageTotal = Math.max(count, 1);
        for (let p = 1; p <= pageTotal; p++) {
          const fig = document.createElement("figure");
          fig.className = "page-card";
          const cap = document.createElement("figcaption");
          cap.className = "page-cap";
          const leftCap = document.createElement("span");
          leftCap.textContent = `第 ${p} / ${pageTotal} 页`;
          const rightCap = document.createElement("span");
          const pageBlocks = byPage.get(p) || [];
          rightCap.textContent = pageBlocks.length ? `${pageBlocks.length} 个版面块` : "";
          cap.append(leftCap, rightCap);

          const stage = document.createElement("div");
          stage.className = "page-stage";
          const img = document.createElement("img");
          img.className = "page-img";
          img.alt = `${preview.filename || "文档"} 第 ${p} 页`;
          img.loading = "lazy";
          img.src = `/api/jobs/${encodeURIComponent(preview.job_id)}/files/${preview.index}/pages/${p}`;

          const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
          svg.setAttribute("viewBox", "0 0 1000 1000");
          svg.setAttribute("preserveAspectRatio", "none");
          svg.setAttribute("class", "overlay-svg" + (showBoxes ? "" : " is-hidden"));
          for (const blk of pageBlocks) {
            const [x1, y1, x2, y2] = blk.bbox;
            const col = colorForLabel(blk.base_label);
            const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
            g.setAttribute("class", "bbox-group");
            g.dataset.block = String(blk.index);
            const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
            rect.setAttribute("x", String(x1));
            rect.setAttribute("y", String(y1));
            rect.setAttribute("width", String(Math.max(4, x2 - x1)));
            rect.setAttribute("height", String(Math.max(4, y2 - y1)));
            rect.setAttribute("fill", col);
            rect.setAttribute("stroke", col);
            rect.setAttribute("class", "bbox-rect");

            const tagBg = document.createElementNS("http://www.w3.org/2000/svg", "rect");
            const labelTxt = String(blk.index);
            const tagW = Math.max(24, 14 + labelTxt.length * 11);
            tagBg.setAttribute("x", String(x1));
            tagBg.setAttribute("y", String(Math.max(0, y1 - 22)));
            tagBg.setAttribute("width", String(tagW));
            tagBg.setAttribute("height", "22");
            tagBg.setAttribute("rx", "3");
            tagBg.setAttribute("fill", col);

            const txt = document.createElementNS("http://www.w3.org/2000/svg", "text");
            txt.setAttribute("x", String(x1 + 5));
            txt.setAttribute("y", String(Math.max(16, y1 - 6)));
            txt.setAttribute("class", "bbox-tag");
            txt.textContent = labelTxt;

            g.append(rect, tagBg, txt);
            g.addEventListener("click", () => {
              highlightBlock(blk.index);
              selectTab(qs("tab-layout"));
            });
            svg.append(g);
          }
          stage.append(img, svg);
          fig.append(cap, stage);
          pagesWrap.append(fig);
        }
      }

      function renderPreviewResult(preview) {
        const prevInner = qs("preview-inner");
        const srcInner = qs("source-inner");
        const layInner = qs("layout-inner");
        prevInner.replaceChildren();
        srcInner.replaceChildren();
        layInner.replaceChildren();

        const hasMd = Boolean(preview && typeof preview.markdown === "string" && preview.markdown.length > 0);
        const hasRaw = Boolean(preview && typeof preview.raw === "string" && preview.raw.length > 0);
        qs("copy-md-btn").disabled = !hasMd;
        qs("dl-md-btn").disabled = !hasMd;
        qs("dl-raw-btn").disabled = !hasRaw;

        const metaParts = [];
        if (preview?.status) metaParts.push(preview.status);
        if (preview?.pages) metaParts.push(`${preview.pages} 页`);
        if (preview?.tokens) metaParts.push(`${preview.tokens} tokens`);
        qs("result-meta").textContent = metaParts.join(" · ");

        const canResume = preview && (preview.status === "failed" || preview.status === "interrupted");
        qs("resume-job-btn").hidden = !canResume;

        if (!preview) return;
        if (preview.error) {
          const errDiv = document.createElement("div");
          errDiv.className = "banner-err";
          errDiv.textContent = `错误：${preview.error}`;
          prevInner.append(errDiv);
        }

        if (hasMd) {
          const pre = document.createElement("pre");
          pre.className = "source-pre";
          pre.textContent = preview.markdown;
          srcInner.append(pre);
        } else {
          const emptyS = document.createElement("div");
          emptyS.className = "empty-result";
          emptyS.textContent = "暂无 Markdown 源码。";
          srcInner.append(emptyS);
        }

        const blocks = preview.blocks || [];
        if (blocks.length) {
          const list = document.createElement("div");
          list.className = "layout-list";
          for (const blk of blocks) {
            const card = document.createElement("button");
            card.type = "button";
            card.className = "block-card";
            card.dataset.block = String(blk.index);
            const head = document.createElement("div");
            head.className = "block-head";
            const left = document.createElement("span");
            left.style.display = "inline-flex";
            left.style.alignItems = "center";
            left.style.gap = "0.4rem";
            const ord = document.createElement("span");
            ord.className = "block-order";
            ord.style.background = colorForLabel(blk.base_label);
            ord.textContent = String(blk.index);
            const lbl = document.createElement("span");
            lbl.className = "block-label";
            lbl.textContent = blk.level ? `${blk.label} (H${blk.level}) · P${blk.page}` : `${blk.label} · P${blk.page}`;
            left.append(ord, lbl);
            const coords = document.createElement("span");
            coords.className = "block-coords";
            coords.textContent = blk.bbox ? `[${blk.bbox.join(", ")}]` : "无坐标";
            head.append(left, coords);
            const snippet = document.createElement("div");
            snippet.className = "block-snippet";
            snippet.textContent = blk.content || "(图像/空块)";
            card.append(head, snippet);
            card.addEventListener("click", () => {
              highlightBlock(blk.index);
              const targetBox = document.querySelector(`.bbox-group[data-block="${blk.index}"]`);
              if (targetBox) targetBox.scrollIntoView({ block: "center", behavior: "smooth" });
            });
            list.append(card);
          }
          layInner.append(list);
        } else {
          const emptyL = document.createElement("div");
          emptyL.className = "empty-result";
          emptyL.textContent = "暂无版面块数据。";
          layInner.append(emptyL);
        }

        if (hasRaw) {
          const det = document.createElement("details");
          det.className = "raw-details";
          const sum = document.createElement("summary");
          sum.textContent = "查看模型原始输出 (result.raw.txt)";
          const pre = document.createElement("pre");
          pre.className = "source-pre";
          pre.textContent = preview.raw;
          det.append(sum, pre);
          layInner.append(det);
        }

        if (hasMd) {
          const body = document.createElement("div");
          body.className = "md-body";
          try {
            renderMarkdownSafe(body, preview.markdown, preview.job_id, preview.index);
            prevInner.append(body);
          } catch (err) {
            const errDiv = document.createElement("div");
            errDiv.className = "banner-err";
            errDiv.textContent = `预览渲染异常，已切换纯文本展示：${err && err.message ? err.message : err}`;
            const fallbackPre = document.createElement("pre");
            fallbackPre.className = "source-pre";
            fallbackPre.textContent = preview.markdown;
            prevInner.append(errDiv, fallbackPre);
          }
        } else {
          const emptyP = document.createElement("div");
          emptyP.className = "empty-result";
          emptyP.textContent = (preview.status === "queued" || preview.status === "running")
            ? `任务正在处理中 (${preview.pages_done ?? 0}/${preview.pages_total ?? "?"} 页)...`
            : "暂无 Markdown 输出。";
          prevInner.append(emptyP);
        }
      }

      async function loadFilePreview(jobId, fileIndex) {
        const preview = await apiJson(`/api/jobs/${encodeURIComponent(jobId)}/files/${fileIndex}/preview`);
        state.fileIndex = fileIndex;
        state.preview = preview;
        qs("file-panel-title").textContent = preview.filename ? `原文件 · ${preview.filename}` : "原文件";
        renderPages(preview);
        renderPreviewResult(preview);
      }

      async function openJob(job, fileIndex = 0) {
        state.job = job;
        const sel = qs("file-select");
        const files = job.files || [];
        sel.replaceChildren();
        files.forEach((f, idx) => {
          const opt = document.createElement("option");
          opt.value = String(idx);
          opt.textContent = f.filename || f.relative_output || `文件 ${idx + 1}`;
          sel.append(opt);
        });
        sel.hidden = files.length <= 1;
        sel.value = String(fileIndex);
        await loadFilePreview(job.id, fileIndex);
        await loadJobs(false);
      }

      // --- Upload & Poll flow ---
      function setProgress({ visible = true, uploadText = "", uploadState = "", parseText = "", parseState = "" }) {
        const toast = qs("progress-toast");
        toast.hidden = !visible;
        if (!visible) return;
        const upLi = qs("step-upload");
        const paLi = qs("step-parse");
        upLi.className = uploadState;
        paLi.className = parseState;
        if (uploadText) qs("step-upload-text").textContent = uploadText;
        if (parseText) qs("step-parse-text").textContent = parseText;
      }

      function readFormOptions() {
        validateJsonField();
        const valid = form.checkValidity();
        if (!valid) {
          form.reportValidity();
          return null;
        }
        const rawParams = paramsTextarea.value.trim();
        return {
          dpi: Number(qs("opt-dpi").value),
          pages_per_chunk: Number(qs("opt-chunk").value),
          tokens_per_page: Number(qs("opt-tokens").value),
          overwrite: qs("opt-overwrite").checked,
          inference_params: rawParams ? JSON.parse(rawParams) : {},
        };
      }

      async function uploadChunkedFile(file) {
        const buf = await file.arrayBuffer();
        const bytes = new Uint8Array(buf);
        const digest = await sha256Hex(buf);
        const CHUNK = 384 * 1024;
        let offset = 0;
        if (bytes.length === 0) throw new Error("空文件无法识别");
        while (offset < bytes.length) {
          const slice = bytes.subarray(offset, offset + CHUNK);
          const nextOffset = offset + slice.length;
          const final = nextOffset >= bytes.length;
          const pct = Math.round((nextOffset / bytes.length) * 100);
          setProgress({
            visible: true,
            uploadText: `正在分块上传 (${pct}%)...`,
            uploadState: "is-active",
            parseText: "等待推理...",
            parseState: "",
          });
          const res = await apiJson("/api/upload", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              sha256: digest,
              name: file.name,
              offset,
              data: bytesToBase64(slice),
              final,
            }),
          });
          if (res.complete) return res.path;
          offset = nextOffset;
        }
        throw new Error("上传未完成");
      }

      async function pollJobUntilDone(jobId) {
        while (true) {
          const job = await apiJson(`/api/jobs/${encodeURIComponent(jobId)}`);
          const first = (job.files && job.files[0]) || {};
          const pgDone = first.pages_done ?? 0;
          const pgTot = first.pages_total ?? "?";
          if (job.status === "queued" || job.status === "running") {
            setProgress({
              visible: true,
              uploadText: "上传完成",
              uploadState: "is-done",
              parseText: job.status === "queued" ? "排队等待 GPU..." : `正在识别 (${pgDone}/${pgTot} 页)...`,
              parseState: "is-active",
            });
            await new Promise((r) => setTimeout(r, 1500));
            continue;
          }
          await openJob(job, 0);
          const failed = job.status === "failed";
          setProgress({
            visible: true,
            uploadText: "上传完成",
            uploadState: "is-done",
            parseText: failed ? `任务失败: ${first.error || job.error || ""}` : "识别完成",
            parseState: failed ? "is-fail" : "is-done",
          });
          setTimeout(() => setProgress({ visible: false }), failed ? 5000 : 2500);
          return;
        }
      }

      async function handleFiles(fileList) {
        if (!fileList || !fileList.length || state.busy) return;
        const opts = readFormOptions();
        if (!opts) return;
        const file = fileList[0];
        state.busy = true;
        qs("pick-file-btn").disabled = true;
        let uploaded = false;
        try {
          const remotePath = await uploadChunkedFile(file);
          uploaded = true;
          setProgress({
            visible: true,
            uploadText: "上传完成",
            uploadState: "is-done",
            parseText: "已提交 GPU 队列...",
            parseState: "is-active",
          });
          const job = await apiJson("/api/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              inputs: [remotePath],
              relative_outputs: { [remotePath]: `demo/${file.name}` },
              dpi: opts.dpi,
              pages_per_chunk: opts.pages_per_chunk,
              tokens_per_page: opts.tokens_per_page,
              overwrite: opts.overwrite,
              inference_params: opts.inference_params,
            }),
          });
          await openJob(job, 0);
          await pollJobUntilDone(job.id);
        } catch (err) {
          setProgress({
            visible: true,
            uploadText: uploaded ? "上传完成" : "上传中断",
            uploadState: uploaded ? "is-done" : "is-fail",
            parseText: uploaded ? `预览或提交失败: ${err.message}` : err.message,
            parseState: "is-fail",
          });
          setTimeout(() => setProgress({ visible: false }), 8000);
        } finally {
          state.busy = false;
          qs("pick-file-btn").disabled = false;
        }
      }

      // --- UI Event bindings ---
      const fileInput = qs("file-input");
      qs("pick-file-btn").addEventListener("click", () => fileInput.click());
      qs("upload-dropzone").addEventListener("click", () => fileInput.click());
      fileInput.addEventListener("change", () => {
        if (fileInput.files && fileInput.files.length) {
          const files = fileInput.files;
          handleFiles(files).finally(() => { fileInput.value = ""; });
        }
      });

      let dragDepth = 0;
      const overlay = qs("drop-overlay");
      window.addEventListener("dragenter", (e) => {
        if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
          e.preventDefault();
          dragDepth++;
          overlay.hidden = false;
        }
      });
      window.addEventListener("dragover", (e) => {
        if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) e.preventDefault();
      });
      window.addEventListener("dragleave", () => {
        dragDepth = Math.max(0, dragDepth - 1);
        if (dragDepth === 0) overlay.hidden = true;
      });
      window.addEventListener("drop", (e) => {
        e.preventDefault();
        dragDepth = 0;
        overlay.hidden = true;
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
          handleFiles(e.dataTransfer.files);
        }
      });

      qs("file-select").addEventListener("change", (e) => {
        if (state.job) loadFilePreview(state.job.id, Number(e.target.value));
      });
      qs("toggle-overlay").addEventListener("change", (e) => {
        document.querySelectorAll(".overlay-svg").forEach((svg) => {
          svg.classList.toggle("is-hidden", !e.target.checked);
        });
      });
      qs("refresh-jobs-btn").addEventListener("click", () => loadJobs(false));
      qs("resume-job-btn").addEventListener("click", async () => {
        if (!state.job || state.busy) return;
        state.busy = true;
        try {
          await apiJson(`/api/jobs/${encodeURIComponent(state.job.id)}/resume`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          });
          await pollJobUntilDone(state.job.id);
        } catch (err) {
          setProgress({ visible: true, uploadText: "恢复任务", uploadState: "is-done", parseText: err.message, parseState: "is-fail" });
        } finally {
          state.busy = false;
        }
      });

      qs("copy-md-btn").addEventListener("click", async () => {
        if (!state.preview?.markdown) return;
        await navigator.clipboard.writeText(state.preview.markdown);
        const btn = qs("copy-md-btn");
        const old = btn.textContent;
        btn.textContent = "已复制";
        setTimeout(() => { btn.textContent = old; }, 1400);
      });
      function triggerAssetDownload(rel, fallbackName, content) {
        if (!state.preview) return;
        const url = resolveAssetUrl(state.preview.job_id, state.preview.index, rel);
        const a = document.createElement("a");
        if (url && (state.preview.assets || []).includes(rel)) {
          a.href = url + "?download=1";
          a.download = fallbackName;
          a.click();
          return;
        }
        const blob = new Blob([content || ""], { type: "text/plain;charset=utf-8" });
        a.href = URL.createObjectURL(blob);
        a.download = fallbackName;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      }
      qs("dl-md-btn").addEventListener("click", () => {
        const stem = (state.preview?.filename || "result").replace(/\.[^.]+$/, "");
        triggerAssetDownload("result.md", `${stem}.md`, state.preview?.markdown || "");
      });
      qs("dl-raw-btn").addEventListener("click", () => {
        const stem = (state.preview?.filename || "result").replace(/\.[^.]+$/, "");
        triggerAssetDownload("result.raw.txt", `${stem}.raw.txt`, state.preview?.raw || "");
      });

      // --- Resizable vertical splitter (pointer + keyboard) ---
      const splitter = qs("splitter");
      const docPanels = qs("document-panels");
      function setRatio(pct) {
        const clamped = Math.max(20, Math.min(80, pct));
        document.documentElement.style.setProperty("--left-ratio", clamped.toFixed(1) + "%");
        splitter.setAttribute("aria-valuenow", String(Math.round(clamped)));
      }
      splitter.addEventListener("pointerdown", (e) => {
        splitter.setPointerCapture(e.pointerId);
        const rect = docPanels.getBoundingClientRect();
        const move = (ev) => setRatio(((ev.clientX - rect.left) / rect.width) * 100);
        const up = () => {
          splitter.removeEventListener("pointermove", move);
          splitter.removeEventListener("pointerup", up);
        };
        splitter.addEventListener("pointermove", move);
        splitter.addEventListener("pointerup", up);
      });
      splitter.addEventListener("keydown", (e) => {
        const cur = Number(splitter.getAttribute("aria-valuenow") || 50);
        if (e.key === "ArrowLeft") setRatio(cur - 4);
        else if (e.key === "ArrowRight") setRatio(cur + 4);
      });

      checkHealth();
      loadJobs(true);
    })();
  </script>
</body>
</html>
"""


def render_demo_html(nonce: str) -> bytes:
    """Render the interactive Unlimited-OCR 2 demo page with a per-request CSP nonce."""
    return HTML_TEMPLATE.replace('__NONCE__', nonce).encode('utf-8')
