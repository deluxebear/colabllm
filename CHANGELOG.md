# Changelog

版本号以 `pyproject.toml` 为唯一来源，使用 `MAJOR.MINOR.PATCH`；Git 标签为 `vMAJOR.MINOR.PATCH`。

## [Unreleased]

### 新增

- 新增 `openjutsu` 运行时（G4，Caddy Basic Auth + Bearer Token）：同机运行 ComfyUI（含 13 个自定义节点包与 Linux 路径补丁）、`comfy-api-proxy` 与 Openjutsu Next.js 16 生产工作台，支持 `--profile full|core` 按需缓存模型至本地 SSD、`--vram-mode auto`（在 `>=76 GiB` 显存上自动启用 `--highvram` 常驻显存）与 `--chains-storage ssd`（将 `h3_chains` 分段检查点通过 VFS `mount --bind` 挂载至本地 NVMe SSD），并提供完整命令行业务子命令（`swap`、`object`、`mask`、`face`、`color`、`job`、`download`、`cancel`、`check`）与实时 SSE 进度跟踪。

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
