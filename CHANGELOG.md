# Changelog

版本号以 `pyproject.toml` 为唯一来源，使用 `MAJOR.MINOR.PATCH`；Git 标签为 `vMAJOR.MINOR.PATCH`。

## [Unreleased]

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
