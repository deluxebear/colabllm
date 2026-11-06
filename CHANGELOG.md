# Changelog

版本号以 `pyproject.toml` 为唯一来源，使用 `MAJOR.MINOR.PATCH`；Git 标签为 `vMAJOR.MINOR.PATCH`。

## [Unreleased]

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
