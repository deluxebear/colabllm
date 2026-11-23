# Changelog

版本号以 `pyproject.toml` 为唯一来源，使用 `MAJOR.MINOR.PATCH`；Git 标签为 `vMAJOR.MINOR.PATCH`。

## [Unreleased]

### 新增

- **SoMark OXR-1.0 文档解析运行时（`oxr`）**：默认 A100（BF16，≥ sm80），在同一 GPU 上以独立 uv 虚拟环境运行 D-FINE 版面分析（`127.0.0.1:8091`）、`vllm==0.25.1` + 权重自带 out-of-tree 插件的 OXR VLM（`127.0.0.1:8090`，按显存自动设定 `max-num-seqs`）与上游 SoMark 兼容解析 API（`/v1/parse/*`、`/demo`、`/docs`）；权重固定修订版下载到本地 SSD，编译缓存经 `colab_cache.py` 归档到 Drive；就绪前要求三进程健康、真实图片解析预热与本地/公网鉴权验证。Caddy 提供 Basic Auth/Bearer，并新增仅限 `POST /v1/parse/*` 的 SoMark 表单 `api_key` 校验分支（`templates/oxr_auth_app.py`，常数时间、失败即拒绝），使 SoMark SDK/CLI 可直接使用；新增 `colabllm oxr parse`、`/health` 汇总探针、`colabllm web` “打开 Web UI”（`/` → `/demo`）、网关目录禁用登记、`docs/oxr.md` 与用户技能参考。`render_caddy_cell` 新增 `extra_handles` 参数（默认不改变既有 Notebook）。

## [1.2.4] - 2026-11-20

### 修复

- **消除 `r2-migrate` 并发文件/目录前缀冲突竞态**：在 `DriveDirCache` 中增加路径级活跃写入追踪与条件变量同步，使 `ensure_dir_locked` 在重定向同名冲突文件至 `<dir>/__r2_object__` 前等待在途写入完成；同时在 `write_file_direct` 中增加已知目录前置重定向、写入后 `S_ISDIR` 校验与多轮 `ENOENT`/`EISDIR` 冲突重试，彻底修复高并发小文件迁移在 macOS/Linux 上偶发 `[Errno 21] Is a directory` 的竞态失败，并同步重新生成 `r2-migrate.ipynb`。

## [1.2.3] - 2026-11-20

### 新增

- **Firecrawl Local v2 CPU 运行时（`firecrawl-local`）**：新增零 GPU Colab CPU 运行时，在本地 NVMe SSD 编译 `firecrawl-local` Rust 二进制并按源码提交 SHA、架构与 glibc 版本缓存至 Google Drive（二次启动约 1 秒恢复）；以非特权 `firecrawl` 用户运行服务并隔离无头 Chrome 原生沙箱，在 Caddy Basic/Bearer 鉴权后暴露完整的 Firecrawl v2 HTTP API（`/v2/scrape`、`/v2/map`、`/v2/crawl`、`/v2/batch/scrape`、`/health`）与对应 CLI 子命令，同时完成 `colabllm web` API 档案、网关目录注册及文档/用户技能同步。
- **`qwen-image` 模型变体注册表（`MODEL_VARIANTS`）与 `uncensored` 变体**：新增 `colabllm qwen-image variants` 与 `up --variant/--preset`（默认 `official`，内置 `uncensored` ChatR Labs Step 90,000 BF16 权重及自动匹配默认步数、True CFG、VAE Tiling 与 `--lora-backend` 策略），支持按注册表持续扩展新变体。

### 修复

- **修复 Colab `--auto-restart` 健康探针路径与 `busy` Kernel 死锁循环**：
  - 为 `Service` 新增可选 `health_path` 字段（并在 `firecrawl-local` 上设为 `/health`），统一 `up`、`status` 与 `--auto-restart` 的健康探测路径，避免根路径 `/` 返回 404 的 API 服务在瞬断重连时被误判为未就绪。
  - 原生 WebSocket 执行器（`colab_ws.py`）在 `initially_ready=True` 重连时改为只读 `iopub` 监听模式（`native-ws-watch`），不再向存活 Kernel 排队发送多余 `execute_request`，并在健康重连及 `up` 复用存活实例时跳过冗余 `colab dfs mount`（避免持续泄漏空闲 Kernel）；在服务确实不健康需要重跑 Notebook 时，若远端 Kernel 仍处于 `busy`，自动先执行 `interrupt`/`restart` 解除排队死锁，避免无限触发 `CT_READY` 超时。
  - 完善无包体 `POST` 请求的 `Content-Length: 0` 头部、WebSocket `IncompleteReadError` 报错提示及 `MuxDaemon` 服务字段兼容过滤。

### 文档

- 补充 `README.md` 中的 Kaggle 运行时安装与使用说明，新增 `docs/firecrawl-local.md` 并更新 `docs/qwen-image.md` 及随包分发的用户技能参考。

## [1.2.2] - 2026-11-18

### 新增与变更

- **`colabllm web` 多视图历史路由与跨平台页面精细化**：新增 `/fleet`、`/copilot`、`/workbench`、`/network`、`/doctor` 语义化路由、`pushState`/`popstate` 历史回退与视图切换过渡，补充实例/目录多平台筛选、工作台活跃实例快捷填充及结构化环境体检看板。
- **`colabllm web` AI 演练场（Copilot Studio）全 7 个子页面业务化升级**：支持子页面 URL 状态同步与按当前能力面板动态生成 cURL / Python OpenAI SDK / 标准库 `urllib` 代码；补全对话首字/总耗时遥测、消息重试/重新生成与 System 角色预设；新增图生图多参考图缩略图编排（一键插入 `<imageN>` / `<Picture N>` 标签、生成结果一键续编辑与可取消 4K 异步轮询）；新增 TTS 语速、`drama3` 批量多角色对白、`indextts` 8 维情绪滑块预设及合成音频一键送入转写/克隆联动；支持 ASR 时间戳播放实时高亮与 SRT/TXT 导出、Embeddings RAG Top-K 检索排序与余弦相似度热力矩阵、`kaggle-web-probe` 等专有运行时原生控制台增强及同源 HTTP 探针响应头与历史重放。

### 修复

- **区分 Colab 未纳管远端会话与体检状态配色**：在“隧道与账户”看板中将非 `colabllm` 纳管的远端 Colab 会话明确标注为 `未纳管远端`，并修正环境体检页 `PASS` 状态的绿色徽章映射与异常项统计。

## [1.2.1] - 2026-11-18

### 变更

- **`colabllm web` “隧道与账户”视图重构**：新增顶部 4 卡指标总览条（传输后端、全局主域名、Colab CCU 算力池、Kaggle 每周 GPU/TPU 配额池）、基于容器查询自适应的双栏全局隧道与 FRP 配置卡、多平台快速筛选栏以及全宽 Colab/Kaggle 账户配额明细表与原生 `<progress>` 配额条，消除原三列等宽挤压导致的表单控件拉伸与状态胶囊多行折行。

### 修复

- **修复 `colabllm web` 全局 FRP 配置显示与页签刷新触发**：对齐 `tunnel_overview()` 的 `frp.enabled`、`frp.server_ip`、`frp.server_port` 与 `frp.ssh` 字段（修复已启用全局 FRP 时仍显示“未配置”的问题），并修正切换至“隧道与账户”（`network`）视图时的按需配额刷新条件。
- **消除 macOS CI 看门狗测试时序竞态**：将 Kaggle 四分支看门狗单元测试由固定时长线程等待改为确定性状态队列驱动，并为测试内建 HTTP/WebSocket 服务器启用守护工作线程与有界读超时，避免高延迟 CI 节点阻塞超时。

## [1.2.0] - 2026-11-18

### 新增

- **Kaggle 运行时子命令命名空间（`colabllm kaggle <short_name> <subcmd>`）与纯 CPU 诊断运行时（`web-probe`）**：
  - 新增隔离状态树 `~/.colabllm/kaggle/services/<session>/state.json`、多账户注册表只读解析与优先级锁定（`kaggle_accounts.py`）、非 CPU 加速器每周剩余配额门禁（`remaining_hours > 0.15` fail-closed）、双向 Cloudflare Tunnel / FRP DNS 归属隔离（`kagglesvc` / `kagglesvc-frp`，自动前缀 `kaggle-<alias>` 子域名）、四分支 `--auto-restart` 看门狗、非 CPU `--max-uptime`（默认 `6h`）以及 `CT_READY` 前全模式反孤儿远端停机保护。
  - 随包分发纯 CPU 诊断探针服务 `web-probe`（`gateway_enabled=False`），采用 `/kaggle/working/.ct-cache/bin` 固定版本 SHA-256 校验缓存与 `/tmp/ct-runtime`（`0700`）运行时私密凭据分区，通过本地 `0600` `runtime.env` 在内核内存中静默注入隧道与鉴权密钥，避免云端占位 Notebook 留存明文凭据。
- **`colabllm web`、`colabllm gateway`、`doctor` 与 Shell 补全多平台纳管**：
  - `colabllm web` 同时自省 Colab 与 Kaggle 服务目录（双射前缀键名 `kaggle-<short_name>`），支持 Kaggle 专属启动参数表单、60 秒单飞缓存的多账户每周 GPU/TPU 配额看板、同源代理 JSONL 脱敏审计日志（`web.log`）与按平台隔离的环境体检（`colabllm doctor --provider {colab,kaggle,all}`）；更新离线 `bash` / `zsh` / `fish` 补全以支持 `colabllm kaggle` 实例与账户名补全。
  - `colabllm gateway` 扩展双状态根实例扫描与安全守卫（新增 `"/kaggle/"` 媒体路径前缀阻断、对未启用网关的数据面探针统一返回 `HTTP 501`），并在 `/gateway/status` 与 `gateway.log` 中标注副本 `provider` 与 `account`。

### 性能优化

- **原生 Kaggle Jupyter WebSocket 执行器（`kaggle_ws.py`）与跨平台单进程 `MuxDaemon` 复用（`mux.py`）**：
  - 新增纯标准库 RFC 6455 Kaggle Jupyter 代理客户端（支持静默隐藏帧注入 `runtime.env`、`Origin` 握手重试、只读 IOPub 心跳重连监听以及 `KGRT_` / `/k/<id>/<token>` 代理凭据日志脱敏），并将 `MuxDaemon` 升级为跨平台（`colab` + `kaggle`）多路复用守护进程，消除每实例 `kaggle session exec` 独立子进程常驻内存开销；支持能力握手与繁忙旧版守护进程零停机溢出槽位（`~/.colabllm/kaggle/mux`）滚动升级。

### 修复

- **Colab `--auto-restart` 就绪态保持与错误根因提取**：区分首次等待 `CT_READY` 超时与运行期瞬态网络/DNS 断连，在公网故障期间持续保留已就绪状态与远端存活 VM（不受初始重试上限误伤），并从外部 CLI 的 Rich 边框报错输出中提取单行根因信息。

### 文档

- 新增 Kaggle 运行时架构设计规范（`docs/designs/kaggle-runtime.md`）与 `web-probe` 运行指南（`docs/kaggle-web-probe.md`），同步更新 `README.md`、通用操作指南、随包分发用户技能（`skills/colabllm/`）及维护者开发技能（`.agents/skills/colab-llm-service/`）。

## [1.1.0] - 2026-11-17

### 新增

- **多运行时 OpenAI / Anthropic 兼容数据面网关（`colabllm gateway`）**：
  - 新增纯标准库本地 HTTP/1.1 聚合网关守护进程（默认监听 `127.0.0.1:8787`，支持 `-d`/`--detach`、`--status`、`--stop`、`--allow-remote` 与多级通配 `--allow-host`），通过 `<upstream_model>@<runtime>[:<instance_id|default>]` 显式目标语法与 `GET /r/{qwen3-tts,indextts,drama3}[:<inst>]/v1/audio/voices` 前缀音色路由，零重启自动发现本机 `READY` 的文本/视觉推理、生图、语音合成、音效及语音转写运行时，并在 JSON、SSE 流与 `multipart/form-data` 请求/响应间对称改写 `model` 标识。
  - 提供命名客户端凭据生命周期管理（`colabllm gateway token {create,list,rotate,revoke}`），`~/.colabllm/gateway/tokens.json`（`0600`）仅存储 `SHA-256` 摘要并支持运行时热加载（兼容 `Authorization: Bearer` 与 Anthropic `x-api-key`）；默认阻断 `/api/ct/*` 控制面、Google Drive 读写字段/请求头、外部 `http(s)://` 媒体引用、受限视频域名及生图 `response_format="url"`，并输出 `0600` 轮转 JSONL 审计日志（`gateway.log`）。
  - 内置按副本槽位信号量并发控制、30 秒单飞 `/v1/models` 探测缓存、协议感知根前缀 KV-Cache 亲和 LRU 与短宽限排队、分阶段 `connect` / `prefill` / `stream_idle` 超时控制（自动将 Cloudflare Quick Tunnel `524` 映射为诊断性 `504`）以及首包前跨副本故障切换。
- **Web 控制台多账户 Colab CCU 配额与活跃 VM 遥测**：
  - 新增 45 秒单飞缓存的账户配额采集器与 `GET /api/accounts/usage[?refresh=1]` 接口（兼容 `colab-ext` JSON 输出与原生 `colab usage` 回退），在隧道与账户看板及启动表单账户选择器中实时展示各账户 CCU 余额、消耗速率、远端活跃 VM 与汇总指标。

### 性能优化

- **原生 Colab WebSocket 保活多路复用器（`mux.py`）与 Web SSE 连接上限**：
  - 新增纯标准库 RFC 6455 `AsyncWebSocket` Jupyter Kernel 客户端（`colab_ws.py`）与单进程异步保活守护进程（`mux.py`），通过 `--exec-backend {auto,native,cli}` 与 `--no-mux` 将多个 Colab 会话保活收敛至单一本地进程（支持 `colab status` 自动刷新令牌、`.runner.lock` 文件锁存活校验、空闲自动退出及异常无缝回退 `colab exec`）。
  - 为 `colabllm web` 增加服务端 SSE 并发流上限淘汰、`BroadcastChannel` 多标签页状态共享与页面可见性感知流释放，消除浏览器同源 HTTP/1.1 六连接池耗尽阻塞。

### 修复

- **`colabllm web` 交互细节修复**：修复复制按钮中文文本换行挤压并统一三视图舰队卡片与工作台命令预览的声明式复制气泡反馈；将 Copilot 演练场目标下拉框收敛为仅展示当前运行中的实例。

### 文档

- 新增网关架构设计规范（`docs/designs/colabllm-gateway.md`），同步更新 `README.md`、通用操作指南、随包分发用户技能（`skills/colabllm/`）及仓库维护者开发技能（`.agents/skills/colab-llm-service/`）。

## [1.0.0] - 2026-11-16

### 新增

- **零依赖本机 Web 控制台（`colabllm web`）**：
  - 采用回环优先监听与一次性启动令牌认证，自动签发 `HttpOnly; SameSite=Strict` 会话 Cookie，并强制启用 `X-ColabLLM-CSRF`、来源校验及严格 `CSP + Trusted Types` 防护；支持 `--read-only`、`--port`、`-d`/`--detach`、`--status`、`--stop` 后台守护以及基于 `fcntl.flock` 的单实例互斥锁。
  - 提供卡片/紧凑表格/按服务聚合三视图舰队看板，支持多账户/分类/状态筛选、折叠已停止实例抽屉、声明式一键复制、右上角排队通知以及与后台任务状态联动的即时启动/停止进度与日志流追踪；同时内置基于 `argparse` 契约动态解析的工作台命令表单、隧道与多账户总览及一键环境体检 (`doctor`)。
- **AI 演练场（Copilot Studio）与全局快捷 `<CopilotSidebar />` 抽屉**：
  - 升级本机同源安全代理 `/api/proxy/<service>/<instance>/<subpath>`，支持 `GET`/`HEAD`/`POST`/`PUT`/`DELETE`、128 MiB 请求体、SSE (`text/event-stream`) 与 Ollama 原生 NDJSON (`application/x-ndjson`) 流式转发以及音频采样率 / Drive 保存路径响应头透传，浏览器全程零密钥暴露。
  - 覆盖全部 19 个现有运行时的 API 能力档案，并支持对后续新增 OpenAI 兼容或自定义服务按文档与子命令启发式自动发现；集成多会话历史、流式 Reasoning 思考折叠卡片、前端 Generative UI 工具调用循环（在聊天流内直接渲染 KPI 指标卡与对比表格）、图像/语音克隆/8 维情绪调节/Nemotron SALM CTC 时间戳转写/向量余弦矩阵/原生专有 API 控制台与通用 HTTP 探针，实时生成同源代理 cURL 与 Python OpenAI SDK 代码。
- **Cloudflare R2 → Google Drive 迁移运行时（`r2-migrate`）**：
  - 新增纯标准库 SigV4 S3/R2 客户端、CPU 高内存服务脚本与内置 Web UI，通过 Google Drive 挂载直通执行高并发 Direct 同步与 `512 MiB` PAX `.tar` 分片归档；内置 SQLite 检查点断点续传、R2 免费额度护栏、宽松 Drive 文件收敛、云端校验后安全清理与自适应删除控制。

### 变更

- **文本与代码推理运行时升级**：`qwen-flash`、`deepseek-coder`、`deepseek-qwen-9b` 统一升级至 `llama-server` `v1.0.0` 并更新 `deepseek-qwen-9b` GGUF 版本固定，同时在 `colabllm web` 舰队卡片中为 `llama-server` 系列暴露“打开 Web UI”快捷按钮。

### 修复

- **热内核模块化重载与 `colab exec` 预检容错**：Setup Cell 在导入嵌入式辅助模块前自动淘汰旧 `sys.modules` 缓存并失效导入缓存，避免温内核重复执行残留旧代码；为 `colab exec` 与 `colab dfs mount` 注入 `REQUEST_TIMEOUT=60` 防止过早握手超时，并可在远端 VM 存活时自动回收卡死进程后安全重试 Cell 1 前的瞬态断连。
- **`unsloth-studio` 首次启动凭据对账与密钥收敛**：在发出 `CT_READY` 前自动通过 `/api/auth/login` 与 `/api/auth/change-password` 将初始密码统一对账为 `CT_AUTH_PASS`，在 `info`/`ps --show-secrets` 中严格按服务认证模式收敛公开字段，并在同源代理登录 `/api/auth/login` 时自动注入本机 `state.json` 凭据。

### 文档

- 在 README 顶部加入使用实机 `indextts`（IndexTTS-2.5）合成中文旁白的 `1080p` 全景实操演示视频与封面预览，并新增可复现录制与配音合成脚本 `tools/record_walkthrough_video.py`。

## [0.1.10] - 2026-11-14

### 新增

- 原生集成 `colab-ext`（`https://github.com/deluxebear/colab-ext`）多账户与共享 Google Drive 绑定能力：
  - 全链路支持 `--account` / `COLABLLM_ACCOUNT` / `COLAB_ACCOUNT`（按档案名或邮箱解析）及 `up --drive-account` / `doctor --drive-account` 共享网盘账号覆写（`up` 与 `doctor` 同时支持短参数 `-a`，不影响 `minimax-h3-image` 等子命令自有的 `-a/--aspect`）。
  - `up` 启动时将解析后的 `account` 与 `drive_account` 写入实例 `state.json`，并在 `ensure_session` 读取 `~/.config/colab-cli/accounts/<账户>/sessions.json`；后续 `--auto-restart` 断线重连/停机重建、`status`、`down`、`nemotron-transcribe` 网盘操作均锁定原账户，不受之后终端 `colab account use` 切换活动账户影响，同时对未初始化 `accounts.json` 的旧版单账户环境保持 100% 向后兼容。
  - `info`、`ps`/`list` 与 `doctor` 展示账户与共享 Drive 状态，`doctor` 与离线 Shell 补全（读取账户目录/注册表，不触及凭据或 Keyring）原生适配 `accounts.json` 与分账户 `dfs_credentials.json`。

### 修复

- `qwen-flash`：移除 `llama-server` 启动前的 `evict_gguf_page_cache()` 调用（保留加载完成后的重复文件缓存释放），避免清空分片拉取阶段预热在主机 DDR5 内存中的 Linux Page Cache 而退化为限速磁盘冷读；同时改进就绪探针对 HTTP `503` 错误响应体的分类（区分 `"Loading model"` 与异步加载失败状态并立即触发显存溢出卸载递增或快速报错）。

### 文档

- 将 README、通用操作文档与分发/维护者 Agent 技能从 `colab-dfs-ext` 更新为 `colab-ext`（`colab account` / `colab drive` / `colab ext`），补充多账户轮换与跨号共享模型缓存示例。

## [0.1.9] - 2026-11-13

### 变更

- `--auto-restart` 增加秒级停机检测与立即恢复：将就绪后断连的默认 `--restart-delay` 从 `15s` 缩短至 `3s`，并在静默保活期间新增每 `10s` 一次的轻量公网边缘探针——一旦隧道网关返回 `502`/`503`/`530` 或连接异常即刻联动 `colab status` 核实（无需苦等 5 分钟 `--idle-check`），确认 VM 已回收时 `0s` 等待立即重建实例（若远端 VM 仍存活则维持 `60s` 冷却期避免误杀内部重启或高负载实例）。
- `up` 复用已存活且公网探针正常（`HTTP 2xx/3xx`）的远端 Colab 会话时，自动继承 `ready=True` 与服务地址并直接接管保活守护，支持本机升级 CLI 后零停机无缝切换守护进程。

## [0.1.8] - 2026-11-13

### 变更

- `--auto-restart` 半开连接静默巡检默认阈值从 `900s`（15 分钟）缩短为 `300s`（5 分钟，支持 `--idle-check SECONDS` 自定义），首次静默未检出 `missing` 时改为每 `60s` 快速复查直至收到新输出或确认回收，并将长周期运行时保活心跳统一加密至 `<= 4` 分钟。

### 修复

- `--auto-restart` 在本地网络闪断后重连仍存活的远端 VM 时，若服务端认证探针已返回 `200`，立即恢复 `ready=True` 且不再受 `ready_timeout` 影响重复断开重连（避免远端 Jupyter Kernel 阻塞在上一轮保活 Cell 时本地 `status` 误显示 `ready: false`）。

## [0.1.7] - 2026-11-12

### 新增

- 所有运行时 `up` 新增 `--auto-restart` 掉线自动重拉守护模式（可配合 `--restart-delay` 与 `--max-restarts`）：结合容器内 `ct_watchdog`、`colab exec` 退出监听、保活心跳静默半开连接巡检与 `colab status` 远端会话文本核实，自动区分本地网络闪断（复用原 VM 保住 SSD 缓存）与 Colab 强收停机（清理残留记录并按原 GPU 规格重新 `colab new` + `colab dfs mount` 恢复服务），首次未就绪支持指数退避与熔断保护，手动 `down` 时安全退出。

## [0.1.6] - 2026-11-12

### 新增

- 新增 `minimax-h3-image` 运行时（默认 G4，兼容 H100/A100，`SGLang Diffusion` Bearer API Key）：部署 `MiniMaxAI/MiniMax-H3-Image`（33.1B DiT + 33.4B Qwen3-VL 编码器 + MiniMaxH3ImageVAE，6 步融合 PDD 蒸馏调度 + 在线 AdaLN 预计算释放 24.6 GiB 显存），默认启用 `fp8-all` 全组件常驻显存（峰值 `~50.4 GiB`，G4 GPU 推理实测 2K 约 `2.6s`、原生 4K 16:9 约 `15.7s`）并支持 `--quantization bf16` 无损常驻（`~92.2 GiB`）；CLI 默认走网络友好的内部 `/api/ct/*` 协议（本地参考图裸二进制上传 + SHA-256 零字节去重、异步后台任务与轻量 JSON 轮询、`HTTP Range` 断点续传下载与服务端 WebP/JPEG 转码、Google Drive 零公网流量直读直写），同时完整保留标准 OpenAI `/v1/images/generations` 与 `/v1/images/edits` 兼容接口及 1–16 张 `<Picture 1>..<Picture 16>` 多参考图编辑。

### 文档

- 补充 G4 文生图 / 图编辑选型指南（`minimax-h3-image` vs `qwen-image` vs `flux-klein`），并同步更新使用文档与 Agent Skills。

## [0.1.5] - 2026-11-09

### 新增

- 新增 `deepseek-qwen-9b` 运行时（默认 L4，兼容 T4/A100/H100/G4，`llama-server` Bearer API Key）：部署 `xtools-at/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic` GGUF，默认加载 `i1-Q4_K_M` imatrix 权重 + 远程导出并缓存至 Google Drive 的原生 `Q8_0` MTP 草稿侧车（`--spec-type draft-mtp` 无损推测解码，L4 实测约 `57–68 tok/s`）+ `mmproj-f16` 视觉投影器与满血原生 `256K`（`262,144`）上下文，提供 `plan` 与 `models` 子命令。
- 新增 `tangoflux` 运行时（默认 L4，Bearer API Key）：部署 `declare-lab/TangoFlux` 44.1 kHz 立体声文生音效服务，提供单条 `generate`、多随机变体与断点续跑 `batch` 音效包生成命令。
- 新增 `drama3` 运行时（G4，SGLang-Omni Bearer API Key）：部署 `fishaudio/drama-3` 多语种零样本克隆语音合成服务，支持自定义音色库管理、小说分章有声书断点续合成与外接 OpenAI 兼容 LLM 的自动多角色台词拆解配音。
- `ollama` 运行时：新增 `--preset huihui-27b` 一键预设，自动匹配 `Huihui-Qwen3.8-27B-abliterated` 量化规格与上下文配置。

### 变更

- `qwen-flash` 升级为 `huihui-ai/Huihui-Qwen3.8-Flash-Next-abliterated-v2-GGUF`（`unsloth-UD-Q4_K_XL-V2` + Unsloth `bf16` 精度修复补丁 + `mmproj-BF16`），基于 `llama.cpp` `b11540` 在 G4 上启用 YaRN 4.0x 满血 `1M`（`1,048,576`）上下文、DDR5 主机内存 PLE 常驻与自适应 `--n-cpu-moe 3`，并新增离线 `plan` 与在线 `models` 子命令。

### 文档

- 补充跨机器迁移状态目录与固定域名配置的说明，以及 DeepSeek 系列运行时（`deepseek-coder` vs `deepseek-qwen-9b`）的选型对比说明。

## [0.1.4] - 2026-11-06

### 变更

- `deepseek-coder` 默认开启思考（`--reasoning on`），由客户端通过请求体 `chat_template_kwargs.enable_thinking` 自行决定是否思考；需要旧行为可在启动时传入 `--reasoning off`。

## [0.1.3] - 2026-11-05

### 新增

- 新增 `deepseek-coder` 运行时（G4，`llama-server` Bearer API Key）：部署 `DeepSeek-V4.1-Flash-1017` GSQ-RCO Coder GGUF + Engram 条件记忆表 + `deepseek41v` 视觉投影器，`--context auto` 按实际显存/内存自动规划最大上下文（G4 满血 1M，H100/A100 80G 约 624K，A100 40G 约 320K）并在冷启动显存不足时阶梯回退，Engram 优先常驻主机内存、不足时退回本地 SSD 分页，默认关闭思考并支持按请求开启，提供离线 `plan` 预览子命令。
- 新增 `qwen-image` 运行时（G4，vLLM-Omni Bearer 认证）：部署 `Qwen/Qwen-Image-2.1`，支持中英双语文生图、1–4 张 `<imageN>` 序号绑定参考图编辑、原生 RGBA 透明通道导出、LoRA 适配器与 Google Drive 图片直接读写，并补充无状态 CORS `OPTIONS` 预检的 ASGI 中间件。
- 新增 `flux-klein` 运行时（G4，vLLM-Omni Bearer 认证）：部署 `black-forest-labs/flux-3-image-klein`，支持 OpenAI 兼容文生图、1–10 张参考图编辑、LoRA、可选 PUP 提示词扩写与 Google Drive 图片直接读写。
- `ollama` 运行时：启动时自动拉取 Ollama/HF GGUF 模型与别名并预热显存，`--compat all|ollama|openai|anthropic` 兼容 API 路由（Basic Auth、Bearer、`x-api-key`）与 `--read-only-models` 保护，`OLLAMA_*` 调优参数、`--profile` 预设、`--models-storage ssd|drive`，以及 `endpoints`、`models`、`pull`、`run`、`preload`、`unload`、`cp`、`rm`、`show` 子命令。

### 修复

- `flux-klein`：规范化服务端模型别名，并去除 Drive 子目录重复前缀。

### 文档

- `openjutsu`：统一 H3 片段长度、步数与分辨率调优说明。

## [0.1.2] - 2026-11-04

### 新增

- 新增 `openjutsu` 运行时（G4，Caddy Basic Auth + Bearer Token）：同机运行 ComfyUI（含 13 个自定义节点包与 Linux 路径补丁）、`comfy-api-proxy` 与基于 `colab_openjutsu` 的中文 Next.js 16 生产工作台，提供完整命令行业务子命令（`swap`、`object`、`mask`、`face`、`color`、`job`、`download`、`cancel`、`check`）与实时 SSE 进度跟踪。
- `openjutsu` G4 运行与拉取优化：支持 `--profile full|core` 按需准备权重、独立环境原生 Rust `hf_xet` 高并发模型拉取（按体积从大到小 LPT 调度 + 多连接 HTTP `Range` 分片断点续传兜底）、免加载 CUDA 动态库的毫秒级 `sm_120` 架构/依赖探测、`--vram-mode auto`（在 `>=76 GiB` 显存上自动启用 `--highvram` 常驻显存）、ComfyUI `--cache-classic` 长视频递归分段系统内存即时回收，以及 `--chains-storage ssd` 本地 NVMe SSD 检查点绑定。

## [0.1.1] - 2026-11-04

撤回 0.1.0（其构建产物包含旧部署示例配置），并修复安全与稳定性问题。

### 安全

- 转写与语音合成服务的 API 认证改为常量时间比较且在 Token 为空时拒绝访问，移除本机 `/health` 免认证例外。
- Drive/数据路径统一限制在 MyDrive 或服务数据目录内，拒绝 `../`、`/etc`、任意 `file://` 等越界访问；校验音色名并过滤上传表单头字段。
- ComfyUI 模型下载目标限定在 `models/` 内；vLLM 允许读取的本地目录缩小到服务工作目录。
- `colab exec` 不再通过命令行参数传递密钥，改由 0600 运行时 notebook 注入。

### 修复

- 守护逻辑：进程组有界终止、重启后 900 秒宽限期、增量读取日志、qwen3-tts 辅助 worker 健康检查；公网认证失败或超时不再报告就绪。
- 下载均设置超时；Caddy/cloudflared 按 CPU 架构选择二进制；Quick Tunnel 重启后刷新公网地址。
- nemotron-transcribe：远端命令路径转义、fp8/nvfp4 本地转写模型名、`-o *.srt` 自动开启时间戳、`download` 回退本地 yt-dlp。
- indextts / qwen3-tts `--timeout 0` 表示不限时，超时退出码为 124；qwen3-tts `--variant`、`--preload-all` 校验取值。
- ComfyUI 新增 `--url` / `--api-token`，进度输出到 stderr；longcat-avatar 新增 `--api-token`；`up` 覆盖的环境变量不再跨调用残留。

### 变更

- notebook 内联代码拆分为 `templates/` 下可单测的独立模块，`build_notebooks.py` 改用构造函数生成；新增生成一致性测试。
- nemotron、longcat-video、longcat-avatar 的编译缓存统一使用 `colab_cache.py` 存档；支持 `--no-drive` 本地数据目录。
- 文档与示例移除个人部署信息，统一使用 `example.com`；README 链接改为 GitHub 绝对地址。
- 以 MIT 许可证开源，wheel 与 sdist 均包含 `LICENSE`。

## [0.1.0] - 2026-11-03

首次打包发布。

- 提供统一 `colabllm` CLI，通过 `uv tool` 安装和升级，本机无需模型计算依赖。
- 包含 10 个 Colab 运行时，支持推理、OCR、转写、语音合成、视频生成、ComfyUI 和 Unsloth Studio。
- 用户配置和状态统一放在 `~/.colabllm/`，支持多实例、Cloudflare Tunnel 和 FRP。
- 提供认证信息查询、诊断、命令补全、任务恢复及超时控制。
- 随包分发 Agent skill，提供全部运行时的独立使用指南和同步检查。
- GitHub Actions 验证 Linux/macOS 与 Python 3.12–3.14，并自动构建和发布 wheel、sdist。
