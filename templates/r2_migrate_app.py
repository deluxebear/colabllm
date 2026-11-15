"""Loopback HTTP API and built-in Web UI for the r2-migrate Colab service."""
from __future__ import annotations

import contextlib
import http.server
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
from typing import Any
import urllib.parse

from r2_migrate_engine import (
    DEFAULT_SPOOL_BYTES,
    DEFAULT_WORKERS,
    DEFAULT_WRITER_WORKERS,
    MigrationManager,
)
from r2_s3 import (
    DEFAULT_SAFE_FREE_CLASS_A,
    DEFAULT_SAFE_FREE_CLASS_B,
    R2Client,
    R2Credentials,
)

MAX_BODY_BYTES = 2 * 1024 * 1024


def _make_csp(nonce: str) -> str:
    return (
        f"default-src 'self'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        f"img-src 'self' data:; "
        f"connect-src 'self'; "
        f"frame-ancestors 'none'; "
        f"base-uri 'none'"
    )


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Cloudflare R2 → Google Drive 迁移控制台</title>
  <style nonce="__NONCE__">
    :root {
      --bg-light: #f6f8fb;
      --bg-dark: #0e131b;
      --surface-light: #ffffff;
      --surface-dark: #161d29;
      --surface-2-light: #eef2f7;
      --surface-2-dark: #1e2736;
      --text-light: #17202b;
      --text-dark: #e8eef7;
      --muted-light: #526071;
      --muted-dark: #9ba9bd;
      --border-light: #cfd8e3;
      --border-dark: #2c384a;
      --brand-light: #0b57d0;
      --brand-dark: #7cb3ff;
      --brand-text-light: #ffffff;
      --brand-text-dark: #071936;
      --danger-light: #b91c1c;
      --danger-dark: #fca5a5;
      --danger-bg-light: #fef2f2;
      --danger-bg-dark: #3b1518;
      --ok-light: #15803d;
      --ok-dark: #86efac;
      --warn-light: #a16207;
      --warn-dark: #fcd34d;

      --bg: var(--bg-light);
      --surface: var(--surface-light);
      --surface-2: var(--surface-2-light);
      --text: var(--text-light);
      --muted: var(--muted-light);
      --border: var(--border-light);
      --brand: var(--brand-light);
      --brand-text: var(--brand-text-light);
      --danger: var(--danger-light);
      --danger-bg: var(--danger-bg-light);
      --ok: var(--ok-light);
      --warn: var(--warn-light);

      @media (prefers-color-scheme: dark) {
        --bg: var(--bg-dark);
        --surface: var(--surface-dark);
        --surface-2: var(--surface-2-dark);
        --text: var(--text-dark);
        --muted: var(--muted-dark);
        --border: var(--border-dark);
        --brand: var(--brand-dark);
        --brand-text: var(--brand-text-dark);
        --danger: var(--danger-dark);
        --danger-bg: var(--danger-bg-dark);
        --ok: var(--ok-dark);
        --warn: var(--warn-dark);
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
        --danger: light-dark(var(--danger-light), var(--danger-dark));
        --danger-bg: light-dark(var(--danger-bg-light), var(--danger-bg-dark));
        --ok: light-dark(var(--ok-light), var(--ok-dark));
        --warn: light-dark(var(--warn-light), var(--warn-dark));
      }

      color-scheme: light dark;
      accent-color: var(--brand);
    }

    *, *::before, *::after { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      line-height: 1.5;
      background: var(--bg);
      color: var(--text);
    }
    .page {
      max-inline-size: 76rem;
      margin-inline: auto;
      padding: 1.25rem;
      display: grid;
      gap: 1.25rem;
    }
    .topbar {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 0.75rem;
      padding: 1rem 1.25rem;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 0.75rem;
    }
    .topbar h1 { margin: 0; font-size: 1.35rem; }
    .subtle { color: var(--muted); font-size: 0.9rem; margin: 0.15rem 0 0; }
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(13.5rem, 1fr));
      gap: 0.9rem;
    }
    .metric {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 0.75rem;
      padding: 0.9rem 1rem;
    }
    .metric-label { font-size: 0.85rem; color: var(--muted); }
    .metric-val { font-size: 1.35rem; font-weight: 700; margin-block: 0.2rem; font-variant-numeric: tabular-nums; }
    .metric-sub { font-size: 0.8rem; color: var(--muted); font-variant-numeric: tabular-nums; }
    .grid-2 {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(22rem, 1fr));
      gap: 1.25rem;
      align-items: start;
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 0.75rem;
      padding: 1.15rem 1.25rem;
    }
    .panel h2 { margin: 0 0 0.9rem; font-size: 1.1rem; }
    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.75rem;
      flex-wrap: wrap;
      margin-block-end: 0.85rem;
    }
    .panel-header h2 { margin: 0; }

    .field {
      display: flex;
      flex-direction: column;
      align-items: stretch;
      gap: 0.25rem;
      margin-block-end: 1rem;
    }
    .field label {
      font-weight: 600;
      font-size: 0.92rem;
    }
    .hint {
      font-size: 0.82rem;
      color: var(--muted);
    }
    input[type="text"],
    input[type="password"],
    input[type="url"],
    input[type="number"],
    textarea,
    select {
      font: inherit;
      font-size: max(1rem, 1em);
      padding: 0.6rem 0.75rem;
      min-height: 48px;
      border-radius: 0.5rem;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      inline-size: 100%;
    }
    [hidden] {
      display: none !important;
    }
    .tar-suboptions {
      margin-block-start: 0.65rem;
      padding: 0.75rem 0.85rem;
      border-radius: 0.5rem;
      border: 1px dashed var(--border);
      background: var(--bg);
    }
    .input-row {
      display: flex;
      gap: 0.5rem;
      align-items: center;
    }
    .input-row > input { flex: 1 1 auto; }
    .inline-2 {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
      gap: 0.75rem;
    }

    fieldset {
      border: 1px solid var(--border);
      border-radius: 0.6rem;
      padding: 0.75rem 0.9rem;
      margin-block-end: 1rem;
    }
    legend {
      font-weight: 600;
      font-size: 0.9rem;
      padding-inline: 0.35rem;
    }
    .choice-row {
      display: flex;
      align-items: flex-start;
      gap: 0.55rem;
      margin-block: 0.45rem;
    }
    .choice-row input[type="radio"],
    .choice-row input[type="checkbox"] {
      margin-block-start: 0.22rem;
      inline-size: 1.15rem;
      block-size: 1.15rem;
      flex-shrink: 0;
    }
    .choice-row label {
      font-weight: 500;
      cursor: pointer;
    }

    .error-msg {
      display: none;
      color: var(--danger);
      font-size: 0.84rem;
      margin-block-start: 0.2rem;
    }
    :is(input, textarea, select):is(:user-invalid, .user-invalid-fallback) {
      border-color: var(--danger);
      background: var(--danger-bg);
    }
    :is(input, textarea, select):is(:user-invalid, .user-invalid-fallback) + .error-msg {
      display: block;
    }
    :is(input, textarea, select):focus-visible,
    button:focus-visible,
    .table-wrapper:focus-visible {
      outline: 3px solid var(--brand);
      outline-offset: 2px;
    }

    .btn-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
      align-items: center;
    }
    button {
      font: inherit;
      font-weight: 600;
      min-height: 48px;
      padding: 0.5rem 0.95rem;
      border-radius: 0.5rem;
      border: 1px solid var(--border);
      background: var(--surface-2);
      color: var(--text);
      cursor: pointer;
    }
    @media (pointer: coarse) {
      input[type="text"],
      input[type="password"],
      input[type="url"],
      input[type="number"],
      textarea,
      select,
      button {
        min-height: 52px;
      }
    }
    button.primary {
      background: var(--brand);
      color: var(--brand-text);
      border-color: var(--brand);
    }
    button.danger {
      color: var(--danger);
      border-color: var(--danger);
    }
    button.sm {
      min-height: 36px;
      padding: 0.3rem 0.65rem;
      font-size: 0.85rem;
    }
    button:disabled { opacity: 0.55; cursor: not-allowed; }

    .status-banner {
      padding: 0.7rem 0.9rem;
      border-radius: 0.5rem;
      background: var(--surface-2);
      border: 1px solid var(--border);
      font-size: 0.9rem;
      min-height: 2.5rem;
    }
    .badge {
      display: inline-block;
      padding: 0.15rem 0.5rem;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      border: 1px solid var(--border);
      background: var(--surface-2);
      white-space: nowrap;
    }
    .badge.ok { color: var(--ok); border-color: var(--ok); }
    .badge.warn { color: var(--warn); border-color: var(--warn); }
    .badge.err { color: var(--danger); border-color: var(--danger); }

    /* Responsive container-queried data tables */
    .table-wrapper {
      overflow: auto;
      max-inline-size: 100%;
      max-block-size: min(460px, 72vh);
      container-type: inline-size;
      border: 1px solid var(--border);
      border-radius: 0.6rem;
    }
    table {
      inline-size: 100%;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 0.88rem;
      font-variant-numeric: tabular-nums;
    }
    thead {
      position: sticky;
      inset-block-start: 0;
      z-index: 2;
      background: var(--surface-2);
    }
    th, td {
      text-align: start;
      padding: 0.6rem 0.75rem;
      border-block-end: 1px solid var(--border);
      vertical-align: top;
    }
    tbody th {
      position: sticky;
      inset-inline-start: 0;
      z-index: 1;
      background: var(--surface);
      font-weight: 600;
    }
    .jobs-table {
      --label-2: "状态";
      --label-3: "目标路径";
      --label-4: "已迁移/已列举";
      --label-5: "速率与详情";
      --label-6: "操作";
    }
    .buckets-table {
      --label-2: "创建时间";
      --label-3: "已迁移/已列举";
      --label-4: "已删除/失败/IA";
    }
    @container (width < 640px) {
      table, thead, tbody, tr, th, td { display: block; }
      thead { display: none; }
      tr {
        margin-block-end: 0.85rem;
        border-block-end: 2px solid var(--border);
        background: var(--surface);
      }
      tbody th {
        position: sticky;
        inset-block-start: 0;
        background: var(--surface-2);
        z-index: 2;
      }
      td { border-block-end: 1px solid var(--border); }
      td:nth-child(2)::before { content: var(--label-2) ": "; font-weight: 600; color: var(--muted); }
      td:nth-child(3)::before { content: var(--label-3) ": "; font-weight: 600; color: var(--muted); }
      td:nth-child(4)::before { content: var(--label-4) ": "; font-weight: 600; color: var(--muted); }
      td:nth-child(5)::before { content: var(--label-5) ": "; font-weight: 600; color: var(--muted); }
      td:nth-child(6)::before { content: var(--label-6) ": "; font-weight: 600; color: var(--muted); }
    }

    dialog {
      border: 1px solid var(--border);
      border-radius: 0.75rem;
      background: var(--surface);
      color: var(--text);
      padding: 1.25rem;
      max-inline-size: min(32rem, 92vw);
    }
    dialog:modal {
      overscroll-behavior: contain;
    }
    dialog::backdrop {
      background: rgba(0, 0, 0, 0.55);
      overflow: hidden;
      overscroll-behavior: contain;
    }
    dialog h3 { margin: 0 0 0.6rem; }
    .dialog-actions {
      display: flex;
      justify-content: flex-end;
      gap: 0.6rem;
      margin-block-start: 1rem;
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
  </style>
</head>
<body>
  <main class="page">
    <header class="topbar">
      <div>
        <h1>Cloudflare R2 → Google Drive 迁移控制台</h1>
        <p class="subtle" id="drive-subtitle">加载运行环境信息中...</p>
      </div>
      <div class="btn-row">
        <span class="badge" id="cred-badge">凭据未检查</span>
        <button type="button" id="refresh-all-btn">刷新状态</button>
      </div>
    </header>

    <section class="cards" aria-label="R2 月度调用额度与估算费用">
      <article class="metric">
        <div class="metric-label">Class A 调用 (List)</div>
        <div class="metric-val" id="m-class-a">0</div>
        <div class="metric-sub" id="m-class-a-sub">免费额度 1,000,000 / 月</div>
      </article>
      <article class="metric">
        <div class="metric-label">Class B 调用 (Standard Get)</div>
        <div class="metric-val" id="m-class-b">0</div>
        <div class="metric-sub" id="m-class-b-sub">免费额度 10,000,000 / 月</div>
      </article>
      <article class="metric">
        <div class="metric-label">零成本优化命中 ($0.00)</div>
        <div class="metric-val" id="m-free-ops">0</div>
        <div class="metric-sub" id="m-free-sub">CDN命中 / Drive跳过 / 批量删除</div>
      </article>
      <article class="metric">
        <div class="metric-label">本月预估费用 (USD)</div>
        <div class="metric-val" id="m-cost">$0.0000</div>
        <div class="metric-sub" id="m-transferred">已迁移 0 B</div>
      </article>
    </section>

    <div role="status" aria-live="polite" class="status-banner" id="action-status">就绪。</div>

    <div class="grid-2">
      <section class="panel" aria-labelledby="creds-heading">
        <div class="panel-header">
          <h2 id="creds-heading">1. Cloudflare R2 连接凭据</h2>
          <button type="button" class="sm danger" id="clear-creds-btn">清除凭据</button>
        </div>
        <form id="creds-form" method="post" action="/api/credentials" aria-labelledby="creds-heading" novalidate>
          <div class="inline-2">
            <div class="field">
              <label for="account_id">Account ID</label>
              <span id="account_id-hint" class="hint">Cloudflare 账户 ID；或留空改填自定义 Endpoint</span>
              <input type="text" id="account_id" name="account_id" pattern="[A-Za-z0-9_-]{8,64}"
                     autocorrect="off" autocapitalize="none" spellcheck="false" enterkeyhint="next"
                     aria-describedby="account_id-hint" data-error="account_id-error">
              <div id="account_id-error" class="error-msg"><span role="img" aria-label="Error">❌</span> 请输入 8–64 位字母/数字/下划线 Account ID。</div>
            </div>
            <div class="field">
              <label for="jurisdiction">存储管辖区 (Jurisdiction)</label>
              <span id="jurisdiction-hint" class="hint">默认全球，欧盟可选 eu</span>
              <select id="jurisdiction" name="jurisdiction">
                <option value="">默认 (Global)</option>
                <option value="eu">欧盟 (eu)</option>
                <option value="fedramp">FedRAMP (fedramp)</option>
              </select>
            </div>
          </div>

          <div class="field">
            <label for="endpoint_url">自定义 S3 Endpoint URL（可选）</label>
            <span id="endpoint_url-hint" class="hint">示例：https://&lt;account&gt;.r2.cloudflarestorage.com</span>
            <input type="url" id="endpoint_url" name="endpoint_url"
                   autocorrect="off" autocapitalize="none" spellcheck="false" enterkeyhint="next"
                   aria-describedby="endpoint_url-hint" data-error="endpoint_url-error">
            <div id="endpoint_url-error" class="error-msg"><span role="img" aria-label="Error">❌</span> 请输入合法的 https:// 或本地回环 URL。</div>
          </div>

          <div class="field">
            <label for="access_key_id">R2 Access Key ID <span aria-hidden="true">*</span></label>
            <span id="access_key_id-hint" class="hint">仅迁移建议 Object Read Only；需删原文件请选 Object Read &amp; Write</span>
            <input type="text" id="access_key_id" name="access_key_id" required minlength="6"
                   autocomplete="username" autocorrect="off" autocapitalize="none" spellcheck="false" enterkeyhint="next"
                   aria-describedby="access_key_id-hint" data-error="access_key_id-error">
            <div id="access_key_id-error" class="error-msg"><span role="img" aria-label="Error">❌</span> Access Key ID 不能为空（至少 6 位）。</div>
          </div>

          <div class="field">
            <label for="secret_access_key">R2 Secret Access Key <span aria-hidden="true">*</span></label>
            <span id="secret_access_key-hint" class="hint">以 0600 权限保存于挂载 Drive，服务端不会在页面回显明文</span>
            <div class="input-row">
              <input type="password" id="secret_access_key" name="secret_access_key" required minlength="6"
                     autocomplete="current-password" enterkeyhint="done"
                     aria-describedby="secret_access_key-hint" data-error="secret_access_key-error">
              <button type="button" id="toggle-secret" class="sm" aria-pressed="false" aria-label="显示 Secret Access Key">显示</button>
            </div>
            <div id="secret_access_key-error" class="error-msg"><span role="img" aria-label="Error">❌</span> Secret Access Key 不能为空（至少 6 位）。</div>
          </div>

          <div class="choice-row">
            <input type="checkbox" id="persist_creds" name="persist" checked>
            <label for="persist_creds">保存到 Drive (0600)，Colab 抢占重启后自动断点续传</label>
          </div>

          <div class="btn-row">
            <button type="submit" class="primary" id="save-creds-btn">保存并测试连接</button>
            <button type="button" id="list-buckets-btn">拉取 Bucket 列表</button>
          </div>
        </form>
      </section>

      <section class="panel" aria-labelledby="job-heading">
        <div class="panel-header">
          <h2 id="job-heading">2. 创建或断点续传迁移任务</h2>
        </div>
        <form id="job-form" method="post" action="/api/jobs" aria-labelledby="job-heading" novalidate>
          <div class="inline-2">
            <div class="field">
              <label for="buckets">指定 Buckets（留空=全部）</label>
              <span id="buckets-hint" class="hint">多个桶用英文逗号分隔，如 assets,logs</span>
              <input type="text" id="buckets" name="buckets" autocorrect="off" autocapitalize="none" spellcheck="false"
                     aria-describedby="buckets-hint">
            </div>
            <div class="field">
              <label for="exclude_buckets">排除 Buckets（可选）</label>
              <span id="exclude_buckets-hint" class="hint">如 temp-cache</span>
              <input type="text" id="exclude_buckets" name="exclude_buckets" autocorrect="off" autocapitalize="none" spellcheck="false"
                     aria-describedby="exclude_buckets-hint">
            </div>
          </div>

          <div class="inline-2">
            <div class="field">
              <label for="output_dir">Google Drive 目标目录</label>
              <span id="output_dir-hint" class="hint">相对 MyDrive 路径，自动按 &lt;dir&gt;/&lt;bucket&gt;/&lt;key&gt; 保留结构</span>
              <input type="text" id="output_dir" name="output_dir" value="r2-backup" required
                     autocorrect="off" autocapitalize="none" spellcheck="false"
                     aria-describedby="output_dir-hint" data-error="output_dir-error">
              <div id="output_dir-error" class="error-msg"><span role="img" aria-label="Error">❌</span> 请填写 Google Drive 目标目录名。</div>
            </div>
            <div class="field">
              <label for="prefix">Key 前缀过滤（可选）</label>
              <span id="prefix-hint" class="hint">仅迁移该前缀下的对象，如 2026/</span>
              <input type="text" id="prefix" name="prefix" autocorrect="off" autocapitalize="none" spellcheck="false"
                     aria-describedby="prefix-hint">
            </div>
          </div>

          <div class="field">
            <label for="public_urls">自定义 CDN 公共域名映射（可选，零 Class B 下载）</label>
            <span id="public_urls-hint" class="hint">每行一条 bucket=https://cdn.example.com；边缘缓存命中 0 Class B，未命中自动回退 S3 签名读取</span>
            <textarea id="public_urls" name="public_urls" rows="2" autocorrect="off" autocapitalize="none" spellcheck="false"
                      aria-describedby="public_urls-hint"></textarea>
          </div>

          <fieldset>
            <legend>R2 API 调用费用模式</legend>
            <div class="choice-row">
              <input type="radio" id="mode-free-first" name="cost_mode" value="free-first" checked>
              <label for="mode-free-first"><strong>free-first（推荐）</strong>：先用 CDN / 免费额度 / STANDARD 层，耗尽后自动回退付费读取</label>
            </div>
            <div class="choice-row">
              <input type="radio" id="mode-free-only" name="cost_mode" value="free-only">
              <label for="mode-free-only"><strong>free-only</strong>：严格 $0.00，触及免费上限或低频 IA 对象前自动安全暂停</label>
            </div>
            <div class="choice-row">
              <input type="radio" id="mode-unlimited" name="cost_mode" value="unlimited">
              <label for="mode-unlimited"><strong>unlimited</strong>：所有存储类型一次跑完（仍启用零费用优化与成本统计）</label>
            </div>
          </fieldset>

          <fieldset>
            <legend>Drive 目标存储模式</legend>
            <div class="choice-row">
              <input type="radio" id="storage-tar" name="storage_mode" value="tar" checked>
              <label for="storage-tar"><strong>tar 分卷归档（推荐）</strong>：本地 NVMe 打包 512 MiB 无压缩 .tar 分卷整块写入 Drive，百万小文件绕过 DriveFUSE RPC 限流</label>
            </div>
            <div class="choice-row">
              <input type="radio" id="storage-files" name="storage_mode" value="files">
              <label for="storage-files"><strong>files 散列直写</strong>：逐文件按原目录结构直接写入 Drive（适合少量大文件或需即时浏览单文件）</label>
            </div>
            <div id="tar-options" class="tar-suboptions">
              <div class="field">
                <label for="tar_shard_mb">单卷 TAR 目标大小 (MiB)</label>
                <span id="tar-shard-hint" class="hint">达到该体积即切分下一卷（默认 512 MiB，支持 1-4096 MiB）</span>
                <input type="number" id="tar_shard_mb" name="tar_shard_mb" min="1" max="4096" value="512" inputmode="numeric" aria-describedby="tar-shard-hint">
              </div>
              <div class="choice-row">
                <input type="checkbox" id="consolidate_existing" name="consolidate_existing" checked>
                <label for="consolidate_existing">开始迁移前先将 Drive 目标目录中已下载的散列文件整合打包进 .tar 分卷并清理散列副本（保持完整性）</label>
              </div>
            </div>
          </fieldset>

          <div class="inline-2">
            <div class="field">
              <label for="workers">并发下载线程数</label>
              <input type="number" id="workers" name="workers" min="1" max="128" value="48" inputmode="numeric">
            </div>
            <div class="field">
              <label for="writer_workers">DriveFS 写入线程数</label>
              <input type="number" id="writer_workers" name="writer_workers" min="1" max="64" value="16" inputmode="numeric">
            </div>
          </div>

          <div class="choice-row">
            <input type="checkbox" id="flatten_single_bucket" name="flatten_single_bucket">
            <label for="flatten_single_bucket">仅迁移单个桶时省略顶层 bucket 目录</label>
          </div>
          <div class="choice-row">
            <input type="checkbox" id="overwrite" name="overwrite">
            <label for="overwrite">覆盖 Drive 中大小相同的已存在文件</label>
          </div>
          <div class="choice-row">
            <input type="checkbox" id="delete_source" name="delete_source">
            <label for="delete_source">核验大小与 MD5 ETag 后删除 R2 原文件（Batch DeleteObjects 为 $0.00）</label>
          </div>

          <div class="btn-row">
            <button type="submit" class="primary" id="start-job-btn">启动 / 断点续传迁移</button>
          </div>
        </form>
      </section>
    </div>

    <section class="panel" aria-labelledby="buckets-table-title">
      <div class="panel-header">
        <h2 id="buckets-table-title">已发现 Buckets</h2>
      </div>
      <div tabindex="0" role="region" aria-labelledby="buckets-table-title" class="table-wrapper">
        <table class="buckets-table">
          <thead>
            <tr>
              <th scope="col">Bucket 名称</th>
              <th scope="col">创建时间</th>
              <th scope="col">已迁移 / 已列举</th>
              <th scope="col">已删除 / 失败 / IA挂起</th>
            </tr>
          </thead>
          <tbody id="buckets-tbody">
            <tr><th scope="row">—</th><td colspan="3">点击“拉取 Bucket 列表”或启动迁移任务后显示</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel" aria-labelledby="jobs-table-title">
      <div class="panel-header">
        <h2 id="jobs-table-title">迁移任务队列与断点检查点</h2>
      </div>
      <div tabindex="0" role="region" aria-labelledby="jobs-table-title" class="table-wrapper">
        <table class="jobs-table">
          <thead>
            <tr>
              <th scope="col">任务 ID</th>
              <th scope="col">状态</th>
              <th scope="col">目标路径</th>
              <th scope="col">已迁移 / 已列举</th>
              <th scope="col">速率与说明</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody id="jobs-tbody">
            <tr><th scope="row">—</th><td colspan="5">暂无迁移任务</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <dialog id="confirm-dialog" aria-labelledby="confirm-title">
    <h3 id="confirm-title">确认操作</h3>
    <p id="confirm-body"></p>
    <form method="dialog">
      <div class="dialog-actions">
        <button value="cancel" autofocus>取消</button>
        <button value="confirm" class="danger" id="confirm-ok-btn">确认执行</button>
      </div>
    </form>
  </dialog>

  <script nonce="__NONCE__">
    const fmtBytes = (n) => {
      const v = Number(n || 0);
      if (v < 1024) return `${v} B`;
      const units = ['KB', 'MB', 'GB', 'TB'];
      let val = v / 1024;
      let idx = 0;
      while (val >= 1024 && idx < units.length - 1) { val /= 1024; idx += 1; }
      return `${val.toFixed(2)} ${units[idx]}`;
    };

    const statusBanner = document.getElementById('action-status');
    const setStatus = (msg) => { statusBanner.textContent = msg; };

    // Polite single live region for form field validation errors + aria-invalid synchronization
    const announcer = document.createElement('div');
    announcer.setAttribute('aria-live', 'polite');
    announcer.classList.add('visually-hidden');
    document.body.append(announcer);
    let announceTimer;
    let announcedField;

    const spokenText = (node) => {
      const copy = node.cloneNode(true);
      copy.querySelectorAll('[aria-hidden="true"]').forEach((n) => n.remove());
      copy.querySelectorAll('[role="img"][aria-label]').forEach((n) => n.replaceWith(`${n.getAttribute('aria-label')}: `));
      return copy.textContent.replace(/\\s+/g, ' ').trim();
    };
    const linkError = (el, shown) => {
      const errorId = el.dataset.error;
      if (!errorId) return;
      const ids = (el.getAttribute('aria-describedby') || '').split(/\\s+/).filter((id) => id && id !== errorId);
      if (shown) ids.push(errorId);
      if (ids.length) el.setAttribute('aria-describedby', ids.join(' '));
      else el.removeAttribute('aria-describedby');
    };
    const announceError = (el) => {
      const error = document.getElementById(el.dataset.error || '');
      if (!error) return;
      const label = el.labels?.[0] ? `${spokenText(el.labels[0])}: ` : '';
      clearTimeout(announceTimer);
      announcer.textContent = '';
      announcedField = el;
      announceTimer = setTimeout(() => { announcer.textContent = label + spokenText(error); }, 500);
    };

    const fallbackDirty = new WeakMap();
    const supportsUserInvalid = typeof CSS !== 'undefined' && CSS.supports && CSS.supports('selector(:user-invalid)');
    const syncAria = (el, announce = false) => {
      if (!el.matches?.('input, select, textarea') || el.type === 'checkbox' || el.type === 'radio') return;
      let invalid;
      if (supportsUserInvalid) {
        invalid = el.matches(':user-invalid');
      } else {
        const st = fallbackDirty.get(el);
        invalid = Boolean(st && st.submittedOrBlurred && !el.validity.valid);
        el.classList.toggle('user-invalid-fallback', invalid);
      }
      if (invalid) {
        if (announce) announceError(el);
        el.setAttribute('aria-invalid', 'true');
        linkError(el, true);
      } else {
        el.setAttribute('aria-invalid', 'false');
        linkError(el, false);
        if (announcedField === el) {
          clearTimeout(announceTimer);
          announcer.textContent = '';
        }
      }
    };
    document.querySelectorAll('input, select, textarea').forEach((el) => syncAria(el));
    document.addEventListener('blur', (e) => {
      if (!supportsUserInvalid && e.target?.matches?.('input, select, textarea')) {
        const st = fallbackDirty.get(e.target) || { touched: false, submittedOrBlurred: false };
        st.submittedOrBlurred = true;
        fallbackDirty.set(e.target, st);
      }
      syncAria(e.target, true);
    }, true);
    document.addEventListener('input', (e) => {
      if (!supportsUserInvalid && e.target?.matches?.('input, select, textarea')) {
        const st = fallbackDirty.get(e.target) || { touched: false, submittedOrBlurred: false };
        st.touched = true;
        fallbackDirty.set(e.target, st);
      }
      if (e.target?.hasAttribute?.('aria-invalid')) syncAria(e.target);
    });
    document.addEventListener('invalid', (e) => {
      if (!supportsUserInvalid) {
        fallbackDirty.set(e.target, { touched: true, submittedOrBlurred: true });
      }
      syncAria(e.target, false);
    }, true);

    // Password reveal toggle
    const secretInput = document.getElementById('secret_access_key');
    const toggleSecretBtn = document.getElementById('toggle-secret');
    toggleSecretBtn.addEventListener('click', () => {
      const showing = secretInput.type === 'text';
      secretInput.type = showing ? 'password' : 'text';
      toggleSecretBtn.setAttribute('aria-pressed', showing ? 'false' : 'true');
      toggleSecretBtn.textContent = showing ? '显示' : '隐藏';
    });

    // Native modal confirmation dialog
    const confirmDialog = document.getElementById('confirm-dialog');
    const confirmTitle = document.getElementById('confirm-title');
    const confirmBody = document.getElementById('confirm-body');
    const confirmModal = (title, body) => new Promise((resolve) => {
      confirmTitle.textContent = title;
      confirmBody.textContent = body;
      const onClose = () => {
        const result = confirmDialog.returnValue;
        confirmDialog.returnValue = '';
        confirmDialog.removeEventListener('close', onClose);
        resolve(result === 'confirm');
      };
      confirmDialog.addEventListener('close', onClose);
      confirmDialog.showModal();
    });

    async function apiJson(path, opts = {}) {
      const res = await fetch(path, {
        headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
        ...opts,
      });
      const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      return data;
    }

    let knownBuckets = [];

    function renderMetrics(usage, health) {
      if (health) {
        const driveTag = health.drive_mounted ? 'Google Drive 已挂载' : '本地回退模式 (未挂载 Drive)';
        document.getElementById('drive-subtitle').textContent =
          `${driveTag} · 数据目录: ${health.data_dir} · 月份: ${usage?.month || ''}`;
        const credBadge = document.getElementById('cred-badge');
        if (health.credentials?.configured) {
          credBadge.className = 'badge ok';
          credBadge.textContent = `R2 已配置 (${health.credentials.access_key_id_masked || health.credentials.source})`;
          if (!document.getElementById('account_id').value && health.credentials.account_id) {
            document.getElementById('account_id').value = health.credentials.account_id;
          }
          if (!document.getElementById('endpoint_url').value && health.credentials.endpoint_url) {
            document.getElementById('endpoint_url').value = health.credentials.endpoint_url;
          }
        } else {
          credBadge.className = 'badge warn';
          credBadge.textContent = '未配置 R2 凭据';
        }
      }
      if (!usage) return;
      document.getElementById('m-class-a').textContent = Number(usage.class_a_ops || 0).toLocaleString();
      document.getElementById('m-class-a-sub').textContent =
        `本月免费剩余 ${Number(usage.free_class_a_remaining || 0).toLocaleString()} / 1,000,000`;
      const stdB = Number(usage.class_b_ops || 0);
      const iaB = Number(usage.ia_class_b_ops || 0);
      document.getElementById('m-class-b').textContent = (stdB + iaB).toLocaleString();
      document.getElementById('m-class-b-sub').textContent =
        `标准剩余 ${Number(usage.free_class_b_remaining || 0).toLocaleString()} · IA检索 ${iaB.toLocaleString()}`;
      const freeTotal = Number(usage.cdn_hit_ops || 0) + Number(usage.skipped_existing_ops || 0)
        + Number(usage.zero_byte_ops || 0) + Number(usage.free_delete_ops || 0);
      document.getElementById('m-free-ops').textContent = freeTotal.toLocaleString();
      document.getElementById('m-free-sub').textContent =
        `CDN:${usage.cdn_hit_ops || 0} 跳过:${usage.skipped_existing_ops || 0} 零字节:${usage.zero_byte_ops || 0} 批量删:${usage.free_delete_ops || 0}`;
      document.getElementById('m-cost').textContent = `$${Number(usage.estimated_cost_usd || 0).toFixed(4)}`;
      document.getElementById('m-transferred').textContent = `已传输 ${fmtBytes(usage.bytes_transferred || 0)}`;
    }

    function renderBuckets(jobs) {
      const tbody = document.getElementById('buckets-tbody');
      tbody.replaceChildren();
      const statsMap = new Map();
      for (const j of jobs || []) {
        for (const b of j.buckets || []) {
          statsMap.set(b.bucket, b);
        }
      }
      const names = Array.from(new Set([...knownBuckets.map((b) => b.name), ...statsMap.keys()]));
      if (!names.length) {
        const tr = document.createElement('tr');
        const th = document.createElement('th');
        th.scope = 'row';
        th.textContent = '—';
        const td = document.createElement('td');
        td.colSpan = 3;
        td.textContent = '点击“拉取 Bucket 列表”或启动迁移任务后显示';
        tr.append(th, td);
        tbody.appendChild(tr);
        return;
      }
      const createdMap = new Map(knownBuckets.map((b) => [b.name, b.creation_date || '—']));
      for (const name of names) {
        const st = statsMap.get(name);
        const tr = document.createElement('tr');
        const th = document.createElement('th');
        th.scope = 'row';
        th.textContent = name;
        const c1 = document.createElement('td');
        c1.textContent = createdMap.get(name) || '—';
        const c2 = document.createElement('td');
        c2.textContent = st ? `${st.objects_migrated} / ${st.objects_listed}` : '—';
        const c3 = document.createElement('td');
        c3.textContent = st ? `${st.objects_deleted} / ${st.objects_failed} / ${st.objects_ia_deferred}` : '—';
        tr.append(th, c1, c2, c3);
        tbody.appendChild(tr);
      }
    }

    function makeActionButton(label, cls, onClick) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `sm ${cls || ''}`.trim();
      btn.textContent = label;
      btn.addEventListener('click', onClick);
      return btn;
    }

    function renderJobs(jobs) {
      const tbody = document.getElementById('jobs-tbody');
      tbody.replaceChildren();
      if (!jobs || !jobs.length) {
        const tr = document.createElement('tr');
        const th = document.createElement('th');
        th.scope = 'row';
        th.textContent = '—';
        const td = document.createElement('td');
        td.colSpan = 5;
        td.textContent = '暂无迁移任务';
        tr.append(th, td);
        tbody.appendChild(tr);
        return;
      }
      for (const j of jobs) {
        const tr = document.createElement('tr');
        const th = document.createElement('th');
        th.scope = 'row';
        th.textContent = j.id;

        const tdStatus = document.createElement('td');
        const badge = document.createElement('span');
        const cls = j.status === 'completed' ? 'ok' : (j.status === 'failed' || j.status === 'cancelled' ? 'err' : 'warn');
        badge.className = `badge ${cls}`;
        badge.textContent = j.status + (j.paid_fallback_used ? ' · 含付费回退' : '');
        tdStatus.appendChild(badge);
        const modeVal = j.progress?.storage_mode || j.config?.storage_mode || 'files';
        if (modeVal === 'tar') {
          const tarBadge = document.createElement('span');
          tarBadge.className = 'badge ok';
          tarBadge.style.marginInlineStart = '0.35rem';
          tarBadge.textContent = 'TAR 分卷';
          tdStatus.appendChild(tarBadge);
        }

        const tdDest = document.createElement('td');
        tdDest.textContent = `${j.config?.dest_root || ''}` + (j.config?.delete_source ? ' (删原)' : '');

        const p = j.progress || {};
        const tdProg = document.createElement('td');
        tdProg.textContent = `${p.objects_migrated || 0} / ${p.objects_listed || 0} (${fmtBytes(p.bytes_migrated || 0)})`
          + (p.tar_shards ? ` · 分卷:${p.tar_shards} (${fmtBytes(p.tar_bytes || 0)})` : '')
          + (p.consolidated_existing_files ? ` · 已归并散列:${p.consolidated_existing_files}` : '')
          + (p.objects_deleted ? ` · 已删:${p.objects_deleted}` : '')
          + (p.objects_failed ? ` · 失败:${p.objects_failed}` : '');

        const tdMsg = document.createElement('td');
        tdMsg.textContent = `${p.files_per_sec || 0} files/s · ${p.mb_per_sec || 0} MB/s · ${j.message || ''}`;

        const tdOps = document.createElement('td');
        const wrap = document.createElement('div');
        wrap.className = 'btn-row';
        if (j.status === 'running' || j.status === 'queued') {
          wrap.appendChild(makeActionButton('暂停', '', async () => {
            await apiJson(`/api/jobs/${encodeURIComponent(j.id)}/pause`, { method: 'POST', body: '{}' });
            setStatus(`已请求暂停任务 ${j.id}`);
            await refreshAll();
          }));
        } else if (j.status !== 'completed' && j.status !== 'cancelled') {
          wrap.appendChild(makeActionButton('继续', 'primary', async () => {
            await apiJson(`/api/jobs/${encodeURIComponent(j.id)}/resume`, { method: 'POST', body: '{}' });
            setStatus(`已恢复任务 ${j.id}`);
            await refreshAll();
          }));
        }
        if (modeVal !== 'tar' && j.status !== 'completed' && j.status !== 'cancelled') {
          wrap.appendChild(makeActionButton('转 TAR 高速续传', 'primary', async () => {
            const ok = await confirmModal(
              '切换为 TAR 分卷归档并整合散列文件',
              `将把任务 ${j.id} 切换为 512 MiB TAR 分卷模式（48 下载并发 + 16 写入并发）：若当前正在运行会先安全暂停，恢复后自动将 Drive 目标目录中已落盘的散列文件校验打包进 .tar 分卷并清理散列副本，再继续迁移剩余 R2 对象。确认切换？`
            );
            if (!ok) return;
            if (j.status === 'running' || j.status === 'queued') {
              await apiJson(`/api/jobs/${encodeURIComponent(j.id)}/pause`, { method: 'POST', body: '{}' });
            }
            await apiJson(`/api/jobs/${encodeURIComponent(j.id)}/resume`, {
              method: 'POST',
              body: JSON.stringify({
                storage_mode: 'tar',
                tar_shard_mb: 512,
                consolidate_existing: true,
                workers: 48,
                writer_workers: 16,
              }),
            });
            setStatus(`任务 ${j.id} 已切换为 TAR 分卷归档模式并恢复执行`);
            await refreshAll();
          }));
        }
        if (p.objects_copied_pending_delete > 0 && j.status !== 'running' && j.status !== 'queued') {
          wrap.appendChild(makeActionButton(`清理R2(${p.objects_copied_pending_delete})`, 'danger', async () => {
            const ok = await confirmModal(
              '确认批量删除 R2 原文件',
              `将核验 Drive 中已拷贝的 ${p.objects_copied_pending_delete} 个文件是否存在且大小一致，然后通过 Free DeleteObjects 从 Cloudflare R2 删除对应原对象。确认执行？`
            );
            if (!ok) return;
            await apiJson(`/api/jobs/${encodeURIComponent(j.id)}/cleanup`, { method: 'POST', body: '{}' });
            setStatus(`已完成任务 ${j.id} 的 R2 原文件清理`);
            await refreshAll();
          }));
        }
        if (j.status !== 'completed' && j.status !== 'cancelled') {
          wrap.appendChild(makeActionButton('取消', 'danger', async () => {
            const ok = await confirmModal('取消迁移任务', `确定要取消任务 ${j.id} 吗？已保存断点检查点，Drive 中已拷贝文件保留。`);
            if (!ok) return;
            await apiJson(`/api/jobs/${encodeURIComponent(j.id)}/cancel`, { method: 'POST', body: '{}' });
            setStatus(`已取消任务 ${j.id}`);
            await refreshAll();
          }));
        }
        tdOps.appendChild(wrap);
        tr.append(th, tdStatus, tdDest, tdProg, tdMsg, tdOps);
        tbody.appendChild(tr);
      }
    }

    async function refreshAll() {
      try {
        const [health, jobsResp] = await Promise.all([apiJson('/health'), apiJson('/api/jobs')]);
        renderMetrics(jobsResp.usage || health.usage, health);
        renderJobs(jobsResp.jobs || []);
        renderBuckets(jobsResp.jobs || []);
      } catch (err) {
        setStatus(`刷新失败: ${err.message}`);
      }
    }

    document.getElementById('refresh-all-btn').addEventListener('click', refreshAll);

    document.getElementById('clear-creds-btn').addEventListener('click', async () => {
      const ok = await confirmModal('清除已保存凭据', '将删除内存及 Google Drive (0600) 上保存的 R2 API 凭据。确认清除？');
      if (!ok) return;
      try {
        await apiJson('/api/credentials', { method: 'DELETE' });
        setStatus('已清除 R2 凭据');
        await refreshAll();
      } catch (err) {
        setStatus(`清除失败: ${err.message}`);
      }
    });

    const acctInput = document.getElementById('account_id');
    const epInput = document.getElementById('endpoint_url');
    const clearEndpointCustomErr = () => {
      acctInput.setCustomValidity('');
      epInput.setCustomValidity('');
      syncAria(acctInput);
      syncAria(epInput);
    };
    acctInput.addEventListener('input', clearEndpointCustomErr);
    epInput.addEventListener('input', clearEndpointCustomErr);

    document.getElementById('creds-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      acctInput.setCustomValidity('');
      epInput.setCustomValidity('');
      if (!acctInput.value.trim() && !epInput.value.trim()) {
        acctInput.setCustomValidity('请填写 Account ID 或自定义 Endpoint URL');
      }
      if (!form.reportValidity()) {
        form.querySelector(':invalid')?.focus();
        return;
      }
      const btn = document.getElementById('save-creds-btn');
      btn.disabled = true;
      try {
        const fd = new FormData(form);
        const payload = {
          account_id: String(fd.get('account_id') || '').trim(),
          jurisdiction: String(fd.get('jurisdiction') || '').trim(),
          endpoint_url: String(fd.get('endpoint_url') || '').trim(),
          access_key_id: String(fd.get('access_key_id') || '').trim(),
          secret_access_key: String(fd.get('secret_access_key') || '').trim(),
          persist: Boolean(document.getElementById('persist_creds').checked),
          verify: true,
        };
        const res = await apiJson('/api/credentials', { method: 'POST', body: JSON.stringify(payload) });
        knownBuckets = res.buckets || [];
        document.getElementById('secret_access_key').value = '';
        setStatus(`凭据验证成功，发现 ${knownBuckets.length} 个 Bucket`);
        await refreshAll();
      } catch (err) {
        setStatus(`凭据验证/保存失败: ${err.message}`);
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById('list-buckets-btn').addEventListener('click', async () => {
      try {
        const res = await apiJson('/api/buckets', { method: 'POST', body: '{}' });
        knownBuckets = res.buckets || [];
        setStatus(`已拉取 ${knownBuckets.length} 个 Bucket`);
        await refreshAll();
      } catch (err) {
        setStatus(`拉取 Bucket 失败: ${err.message}`);
      }
    });

    function syncTarOptionsVisibility() {
      const box = document.getElementById('tar-options');
      const isTar = document.getElementById('storage-tar')?.checked;
      if (!box) return;
      if (isTar) {
        box.hidden = false;
        box.inert = false;
      } else {
        box.hidden = true;
        box.inert = true;
      }
    }
    document.querySelectorAll('input[name="storage_mode"]').forEach((el) => {
      el.addEventListener('change', syncTarOptionsVisibility);
    });
    syncTarOptionsVisibility();

    document.getElementById('job-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      if (!form.reportValidity()) {
        form.querySelector(':invalid')?.focus();
        return;
      }
      const deleteSource = document.getElementById('delete_source').checked;
      if (deleteSource) {
        const ok = await confirmModal(
          '确认开启“迁移后删除 R2 原文件”',
          '迁移线程会在核验文件大小与 MD5 ETag 并写入 Google Drive 后，使用 Batch DeleteObjects 删除 Cloudflare R2 上的原始文件。是否继续？'
        );
        if (!ok) return;
      }
      const btn = document.getElementById('start-job-btn');
      btn.disabled = true;
      try {
        const fd = new FormData(form);
        const splitCsv = (s) => String(s || '').split(',').map((x) => x.trim()).filter(Boolean);
        const pubLines = String(fd.get('public_urls') || '').split(/\\r?\\n/).map((x) => x.trim()).filter(Boolean);
        const storageMode = String(fd.get('storage_mode') || 'tar');
        const payload = {
          buckets: splitCsv(fd.get('buckets')),
          exclude_buckets: splitCsv(fd.get('exclude_buckets')),
          output_dir: String(fd.get('output_dir') || 'r2-backup').trim(),
          prefix: String(fd.get('prefix') || '').trim(),
          public_urls: pubLines,
          cost_mode: String(fd.get('cost_mode') || 'free-first'),
          storage_mode: storageMode,
          tar_shard_mb: Number(fd.get('tar_shard_mb') || 512),
          consolidate_existing: Boolean(document.getElementById('consolidate_existing').checked),
          workers: Number(fd.get('workers') || 48),
          writer_workers: Number(fd.get('writer_workers') || 16),
          flatten_single_bucket: Boolean(document.getElementById('flatten_single_bucket').checked),
          overwrite: Boolean(document.getElementById('overwrite').checked),
          delete_source: deleteSource,
        };
        const res = await apiJson('/api/jobs', { method: 'POST', body: JSON.stringify(payload) });
        setStatus(`任务 ${res.id} 已启动 (${res.resumed_from_checkpoint ? '从检查点续传' : '新任务'})`);
        await refreshAll();
      } catch (err) {
        setStatus(`启动任务失败: ${err.message}`);
      } finally {
        btn.disabled = false;
      }
    });

    refreshAll();
    setInterval(refreshAll, 3000);
  </script>
</body>
</html>
"""


class R2MigrateHttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], manager: MigrationManager) -> None:
        self.manager = manager
        self.drive_mounted = manager.drive_mount.is_dir()
        super().__init__(server_address, R2MigrateRequestHandler)


class R2MigrateRequestHandler(http.server.BaseHTTPRequestHandler):
    server: R2MigrateHttpServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, status: int, html: str, nonce: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _make_csp(nonce))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}
        try:
            length = int(raw_len)
        except ValueError:
            raise ValueError("Invalid Content-Length") from None
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError(f"Request body exceeds {MAX_BODY_BYTES} bytes")
        data = self.rfile.read(length)
        if not data:
            return {}
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        mgr = self.server.manager
        if path == "/":
            nonce = secrets.token_urlsafe(16)
            self._send_html(200, HTML_TEMPLATE.replace("__NONCE__", nonce), nonce)
            return
        if path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "r2-migrate",
                    "version": "2.0",
                    "capabilities": ["files", "tar", "consolidate_existing"],
                    "drive_mounted": mgr.drive_mount.is_dir(),
                    "data_dir": str(mgr.data_dir),
                    "work_dir": str(mgr.work_dir),
                    "credentials": mgr.cred_store.summary(),
                    "active_jobs": mgr.active_jobs_count(),
                    "usage": mgr.ledger.snapshot(),
                },
            )
            return
        if path == "/api/credentials":
            self._send_json(200, mgr.cred_store.summary())
            return
        if path == "/api/usage":
            self._send_json(200, {"usage": mgr.ledger.snapshot()})
            return
        if path == "/api/jobs":
            self._send_json(200, {"jobs": mgr.list_jobs(), "usage": mgr.ledger.snapshot()})
            return
        if path.startswith("/api/jobs/"):
            job_id = urllib.parse.unquote(path[len("/api/jobs/") :])
            if "/" in job_id or not job_id:
                self._send_json(404, {"error": "Not found"})
                return
            try:
                self._send_json(200, mgr.get_job(job_id))
            except KeyError as err:
                self._send_json(404, {"error": str(err)})
            return
        self._send_json(404, {"error": "Not found"})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/credentials":
            self.server.manager.cred_store.clear()
            self._send_json(200, {"cleared": True, "credentials": self.server.manager.cred_store.summary()})
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/")
        mgr = self.server.manager
        try:
            body = self._read_json()
        except ValueError as err:
            self._send_json(400, {"error": str(err)})
            return

        try:
            if path == "/api/credentials":
                creds = R2Credentials.from_dict(body)
                creds.validate()
                buckets: list[dict[str, str]] | None = None
                if body.get("verify", True):
                    client = R2Client(creds, ledger=mgr.ledger, timeout=mgr.client_timeout)
                    try:
                        buckets = client.list_buckets()
                    finally:
                        client.close()
                        mgr.ledger.flush()
                summary = mgr.cred_store.set_credentials(creds, persist=bool(body.get("persist", True)))
                self._send_json(
                    200,
                    {"credentials": summary, "buckets": buckets, "usage": mgr.ledger.snapshot()},
                )
                return

            if path == "/api/buckets":
                buckets = mgr.list_buckets(body.get("credentials"))
                self._send_json(200, {"buckets": buckets, "usage": mgr.ledger.snapshot()})
                return

            if path == "/api/usage":
                usage = mgr.ledger.calibrate(
                    class_a_ops=body.get("class_a_ops"),
                    class_b_ops=body.get("class_b_ops"),
                    reset=bool(body.get("reset", False)),
                )
                self._send_json(200, {"usage": usage})
                return

            if path == "/api/jobs":
                raw_max_a = body.get("max_free_class_a")
                raw_max_b = body.get("max_free_class_b")
                raw_consolidate = body.get("consolidate_existing")
                job = mgr.create_or_resume_job(
                    buckets=body.get("buckets"),
                    exclude_buckets=body.get("exclude_buckets"),
                    prefix=str(body.get("prefix") or ""),
                    output_dir=body.get("output_dir"),
                    flatten_single_bucket=bool(body.get("flatten_single_bucket", False)),
                    delete_source=bool(body.get("delete_source", False)),
                    cost_mode=str(body.get("cost_mode") or "free-first"),
                    public_urls=body.get("public_urls"),
                    max_free_class_a=DEFAULT_SAFE_FREE_CLASS_A if raw_max_a is None else int(raw_max_a),
                    max_free_class_b=DEFAULT_SAFE_FREE_CLASS_B if raw_max_b is None else int(raw_max_b),
                    workers=body.get("workers"),
                    writer_workers=body.get("writer_workers"),
                    overwrite=bool(body.get("overwrite", False)),
                    storage_mode=body.get("storage_mode"),
                    tar_shard_mb=body.get("tar_shard_mb"),
                    consolidate_existing=None if raw_consolidate is None else bool(raw_consolidate),
                    new_job=bool(body.get("new_job", False)),
                    credentials=body.get("credentials"),
                )
                self._send_json(201, job)
                return

            if path == "/api/drive-list":
                res = mgr.list_drive(
                    inputs=body.get("inputs"),
                    recursive=bool(body.get("recursive", True)),
                    limit=max(1, min(5000, int(body.get("limit") or 500))),
                )
                self._send_json(200, res)
                return

            if path.startswith("/api/jobs/"):
                rest = path[len("/api/jobs/") :]
                if "/" not in rest:
                    self._send_json(404, {"error": "Not found"})
                    return
                raw_id, action = rest.split("/", 1)
                job_id = urllib.parse.unquote(raw_id)
                if action == "pause":
                    self._send_json(200, mgr.pause_job(job_id))
                    return
                if action == "resume":
                    raw_consolidate = body.get("consolidate_existing")
                    self._send_json(
                        200,
                        mgr.resume_job(
                            job_id,
                            cost_mode=body.get("cost_mode"),
                            delete_source=body.get("delete_source"),
                            workers=body.get("workers"),
                            writer_workers=body.get("writer_workers"),
                            max_free_class_a=body.get("max_free_class_a"),
                            max_free_class_b=body.get("max_free_class_b"),
                            overwrite=body.get("overwrite"),
                            public_urls=body.get("public_urls"),
                            storage_mode=body.get("storage_mode"),
                            tar_shard_mb=body.get("tar_shard_mb"),
                            consolidate_existing=None if raw_consolidate is None else bool(raw_consolidate),
                            credentials=body.get("credentials"),
                        ),
                    )
                    return
                if action == "cleanup":
                    self._send_json(200, mgr.cleanup_job(job_id, credentials=body.get("credentials")))
                    return
                if action == "cancel":
                    self._send_json(200, mgr.cancel_job(job_id))
                    return
                self._send_json(404, {"error": "Unknown job action"})
                return

            self._send_json(404, {"error": "Not found"})
        except KeyError as err:
            self._send_json(404, {"error": str(err)})
        except (ValueError, TypeError) as err:
            self._send_json(400, {"error": str(err)})
        except Exception as err:
            self._send_json(502, {"error": str(err)})


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    data_dir: Path | None = None,
    work_dir: Path | None = None,
    drive_mount: Path | None = None,
) -> tuple[R2MigrateHttpServer, MigrationManager]:
    svc_port = int(os.environ.get("CT_SERVICE_PORT", "8000")) if port is None else int(port)
    d_dir = Path(data_dir or os.environ.get("CT_DATA_DIR") or "/content/drive/MyDrive/colab-llm/r2-migrate")
    w_dir = Path(work_dir or os.environ.get("CT_R2_WORK_DIR") or "/content/r2-migrate")
    mount = Path(drive_mount or os.environ.get("CT_DRIVE_MOUNT") or "/content/drive/MyDrive")
    workers = max(1, int(os.environ.get("CT_R2_WORKERS", str(DEFAULT_WORKERS))))
    writers = max(1, int(os.environ.get("CT_R2_WRITER_WORKERS", str(DEFAULT_WRITER_WORKERS))))
    spool_gb = max(0.25, float(os.environ.get("CT_R2_SPOOL_GB", "4")))
    mgr = MigrationManager(
        data_dir=d_dir,
        work_dir=w_dir,
        drive_mount=mount,
        default_workers=workers,
        default_writer_workers=writers,
        spool_bytes_cap=int(spool_gb * 1024 * 1024 * 1024),
    )
    mgr.recover_on_startup()
    srv = R2MigrateHttpServer((host, svc_port), mgr)
    return srv, mgr


def main() -> None:
    srv, mgr = create_server()

    def handle_sig(signum: int, _frame: Any) -> None:
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handle_sig)

    try:
        srv.serve_forever()
    finally:
        mgr.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
