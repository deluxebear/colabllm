<!-- Generated from docs/kaggle-web-probe.md; edit the source and run tools/sync_runtime_docs.py. -->

# Kaggle 纯 CPU HTTP 连通性与缓存探针（`kaggle-web-probe`）

通过 `colabllm kaggle web-probe ...` 在 Kaggle 交互式 CPU 会话（`accelerator="cpu"`，不消耗 GPU/TPU 周配额）中启动纯 Python 标准库 HTTP 探针服务（`127.0.0.1:18090`），前置 Caddy Basic Auth / Bearer 鉴权与安全响应头（`18080`），经 Cloudflare Quick Tunnel / Named Tunnel 或共享 FRP 隧道暴露到公网。

适用于验证本机 `kaggle` CLI（含 `kaggle-ext` 扩展）、多账户隔离、`CT_WORK_DIR` / `CT_CACHE_DIR` 双区存储（重启复跑缓存命中）以及 `colabllm web` / 隧道端到端连通性。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

> 注意：`web-probe` 为工具诊断服务，**不暴露**在 `colabllm gateway` 数据面路由目录中（`gateway_enabled=False`，直连网关模型请求返 `HTTP 501`）。非 `--dry-run` 模式下传入非 CPU `--accelerator` 会被前置拒绝。

## 启动、重连与缓存复用

```bash
colabllm kaggle services
colabllm doctor --provider kaggle --online
colabllm kaggle web-probe up --dry-run --quick-tunnel -i check
colabllm kaggle web-probe up --quick-tunnel
colabllm kaggle web-probe up --transport frp --domain kaggle-probe.example.com
colabllm kaggle web-probe up --auto-restart --max-restarts 0
```

- **离线预览（`--dry-run`）**：仅校验参数与端口并在终端打印完整执行计划，不创建 `~/.colabllm/kaggle/` 状态目录，也不调用远端 `kaggle` CLI。
- **双区容器路径**：
  - 易失运行区 `CT_WORK_DIR=/tmp/ct-runtime`（`0700`）：存放 `Caddyfile`、`frpc.toml`、各类 `.proc.json` 与运行日志（均 `0600`），避免污染持久化存储。
  - 持久二进制缓存区 `CT_CACHE_DIR=/kaggle/working/.ct-cache` 与 `CT_BIN_DIR=/kaggle/working/.ct-cache/bin`：按固定版本及 SHA-256 `.manifest.json` 原子缓存 `caddy`、`cloudflared` 与 `frpc`。
- **共享 `mux` 原生 WebSocket 保活（默认）**：当本机 `kaggle-ext` `sessions.json` 已具备代理 URL/凭据时，`--exec-backend auto` 默认将后台/分离保活任务托管给跨平台共享单进程 `MuxDaemon`（`runner_mode: "mux"`，`exec_backend: "native"`，稳态零常驻 `kaggle session exec` 子进程；通过隐藏只读/无记录 `execute_request` 注入 `runtime.env`）；传入 `--no-mux` 可独立单进程运行，传入 `--exec-backend cli` 可强制回退 `kaggle session exec` 子进程模式。
- **`--max-restarts 0`**：与 `--auto-restart` 配合时保留断线重附（原生 `mux` 只读 WebSocket `iopub` 监听或 CLI `kaggle session logs -f`）与内核恢复，但禁用销毁新建容器重建。

## 内核重启与生命周期管理

```bash
colabllm kaggle web-probe status --json
colabllm kaggle web-probe info --json --show-secrets
colabllm kaggle web-probe logs -f
colabllm kaggle web-probe restart
colabllm kaggle web-probe down
colabllm kaggle web-probe down --delete-tunnel
```

- `restart`：在保留 `/kaggle/working/.ct-cache` 的前提下安全停掉本地旧执行器、触发 `kaggle session restart` 并重新执行探针 Notebook，观察 `CT_CACHE_EVENT artifact=... status=hit` 命中记录。
- `down`：取消远端 Kaggle 会话并清空当次运行字段与 `runtime.env`；加 `--delete-tunnel` 时一并删除 Cloudflare Named Tunnel / FRP 路由及 DNS CNAME 记录。

## HTTP 探针端点

| 路径 | 认证 | 说明 |
| --- | --- | --- |
| `GET /` | Caddy Basic Auth 或 `Authorization: Bearer` | 静态 HTML 诊断页，展示容器运行时长、`.ct-cache` 目录状态与磁盘剩余容量 |
| `GET /health` | Caddy Basic Auth 或 `Authorization: Bearer` | JSON 健康检查：`{"status": "ok", "service": "web-probe", "uptime_seconds", "cache_dir", "free_mb"}` |
| `/v1/*` | - | 显式返回 `HTTP 404 {"error": "not_supported"}` |
