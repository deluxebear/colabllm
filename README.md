# Google Colab 大模型服务

在 Google Colab GPU 上运行大模型服务并通过 Cloudflare Tunnel 暴露公网访问。
遵循 **“一个服务 = 一个 notebook (`services/<name>.ipynb`) + 一个执行脚本 (`services/<name>.py`)”** 的约定。

## 架构与职责

| 阶段 / 职责 | 执行位置 | 说明 |
| --- | --- | --- |
| 创建/复用 Colab 会话 (`colab new`) | 本地脚本 `services/<name>.py` | 会话名与服务名一致 |
| 免浏览器挂载网盘 (`colab dfs mount`) | 本地脚本 `services/<name>.py` | 挂载到 `/content/drive`；自动跳过 notebook 里的 `drive.mount()` |
| 固定域名 (`--domain`) | 本地脚本 (`cf` CLI) | 创建/复用 `colab-<name>` 隧道、配置 ingress、绑定 DNS CNAME（主域名可配置，未配置时使用动态 URL） |
| 密钥生成与注入 | 本地脚本 -> `--env` | 自动生成账号密码与 Bearer Token，保存在 `.state/<name>/state.json` (0600) |
| 安装与启动服务 | 远端 `services/<name>.ipynb` | 幂等安装，服务仅监听 `127.0.0.1` |
| 安全认证层 (Caddy) | 远端 `services/<name>.ipynb` | 对无自带认证的服务（如 Ollama）前置 Caddy：浏览器走 Basic Auth，API 走 `Authorization: Bearer <token>` |
| 启动 `cloudflared` 与保活 | 远端 `services/<name>.ipynb` | 最后一个 cell 持续阻塞并监控子进程；本地后台进程保持 `colab exec` 连接以防 Colab 回收会话 |

## 各运行时使用文档

| 服务名称 | 默认 GPU | 认证方式 | 固定域名示例（主域名配置为 `example.com`） | 详细使用说明 |
| --- | --- | --- | --- | --- |
| **NVIDIA-Nemotron-3.5-Transcribe-30B-A4B** | `G4` | `vLLM` Bearer API Key (`builtin`) | `https://transcribe.example.com` | [docs/nemotron-transcribe.md](docs/nemotron-transcribe.md) |
| **LongCat-Video-Avatar 1.5** | `G4` | Caddy (Basic Auth + Bearer) | `https://longcat-avatar.example.com` | [docs/longcat-avatar.md](docs/longcat-avatar.md) |
| **LongCat-Video** | `G4` | Caddy (Basic Auth + Bearer) | `https://longcat-video.example.com` | [docs/longcat-video.md](docs/longcat-video.md) |
| **ComfyUI + ComfyUI-Manager** | `L4` | Caddy (Basic Auth + Bearer) | `https://comfyui.example.com` | [docs/comfyui.md](docs/comfyui.md) |
| **Qwen3.8-Flash-Next (800K + Vision)** | `G4` | `llama-server` Bearer API Key | `https://qwen.example.com` | [docs/qwen-flash.md](docs/qwen-flash.md) |
| **Ollama** | `T4` | Caddy (Basic Auth + Bearer) | `https://ollama.example.com` | [docs/ollama.md](docs/ollama.md) |
| **Unsloth Studio** | `L4` | 内置 Web 登录 (`builtin`) | `https://unsloth-studio.example.com` | [docs/unsloth-studio.md](docs/unsloth-studio.md) |

## 目录结构

```
├── .agents/skills/colab-llm-service/   # 本项目 Agent Skill（最佳实践、排错、认证与隧道参考）
├── docs/                               # 各运行时详细使用文档
│   ├── nemotron-transcribe.md          # NVIDIA-Nemotron-3.5-Transcribe-30B-A4B (G4 / 远端 API + 网盘批量音频转写)
│   ├── comfyui.md                      # ComfyUI + ComfyUI-Manager 使用说明（含本地 SSD 模型按需下载）
│   ├── qwen-flash.md                   # Qwen3.8-Flash-Next (G4 / 800K / Vision) 使用说明
│   ├── ollama.md                       # Ollama 使用说明
│   └── unsloth-studio.md               # Unsloth Studio 使用说明
├── lib/colabsvc.py                     # 共享运行库（Service 定义与 CLI 实现）
├── services/
│   ├── nemotron-transcribe.py          # Nemotron-3.5-Transcribe-30B-A4B 执行脚本（G4，支持 transcribe 单文件/批量调用网盘音频）
│   ├── nemotron-transcribe.ipynb       # Nemotron-3.5-Transcribe-30B-A4B notebook（vLLM 0.32 + NeMo Speech SALM + MTP8 + CTC 时间戳）
│   ├── comfyui.py                      # ComfyUI + ComfyUI-Manager 执行脚本（Caddy 认证，默认 L4，支持 --workflow / pull-workflow / pull-model）
│   ├── comfyui.ipynb                   # ComfyUI + ComfyUI-Manager notebook（模型存本地高速 SSD，插件/工作流/输出持久化到 Google Drive）
│   ├── unsloth-studio.py               # Unsloth Studio 执行脚本（自带登录 auth="builtin"，默认 L4）
│   ├── unsloth-studio.ipynb            # Unsloth Studio notebook
│   ├── ollama.py                       # Ollama 执行脚本（Caddy 认证 auth="caddy"，默认 T4）
│   ├── ollama.ipynb                    # Ollama notebook
│   ├── qwen-flash.py                   # Qwen3.8-Flash-Next 执行脚本（llama-server API Key，G4 GPU，默认域名 qwen.example.com）
│   └── qwen-flash.ipynb                # Qwen3.8-Flash-Next notebook（800K YaRN 3.125x + BF16 视觉 + sm_120 编译缓存）
├── templates/
│   ├── service.py                      # 新服务脚本模板
│   ├── service.ipynb                   # 新服务 notebook 模板
│   └── build_notebooks.py              # 标准 notebook 生成器
└── .state/<name>/                      # 运行时状态与日志（已 gitignore）
```

## 前置准备

```bash
uv tool install google-colab-cli
colab dfs consent        # 一次性授权 Google Drive
cf auth login            # 使用 --domain 固定域名时需登录 Cloudflare CLI
```

## Cloudflare Tunnel 配置

通过项目根目录的 `colabctl.py` 统一配置；所有现有及新增运行时、所有实例均读取同一份主域名配置，保存在本机 `.state/tunnel-config.json`。配置为空时，`up`（包括不带 HOST 的 `up --domain`）使用 Cloudflare Quick Tunnel 的随机 `https://*.trycloudflare.com` URL，无需 `cf` 登录；每次启动 URL 可能变化。代码中的历史 `example.com` 后缀不再自动启用。

```bash
# 查看 / 设置 / 清除全局主域名（项目根目录统一入口）
./colabctl.py tunnel-config
./colabctl.py tunnel-config --base-domain example.com
./colabctl.py tunnel-config --clear

# 只为本次启动指定主域名，不写入配置
./services/longcat-avatar.py up --base-domain example.com
# -> https://longcat-avatar.example.com
./services/qwen-flash.py up -i 2 --base-domain example.com
# -> https://qwen-2.example.com（保留服务别名与实例后缀）

# 已配置主域名后：up 自动使用固定域名；也可以强制动态 URL
./services/longcat-avatar.py up
./services/longcat-avatar.py up --quick-tunnel

# 完整域名覆盖自动生成的服务域名
./services/longcat-avatar.py up --domain avatar.example.com
```

可设置环境变量 `COLABSVC_BASE_DOMAIN=example.com`。选择顺序：`--quick-tunnel` 强制动态；完整 `--domain HOST` > `--base-domain` > 环境变量 > 全局配置 > 动态 URL。三个启动选项互斥。域名只填 DNS 名称，不包含 `https://`、端口或路径。

固定域名要求 `cf auth login` 且账户有该域名的 DNS Zone 权限，启动时自动创建/复用 named tunnel、设置 ingress 和 DNS CNAME。配置修改仅影响之后的启动，不会重启运行中的服务；已有服务需自行择机重启。切换动态模式不会删除旧隧道和 DNS，可使用 `down --delete-tunnel` 显式清理。

## 常用命令

以 `unsloth-studio` 和 `ollama` 为例：

```bash
# 1. 启动服务（未配置主域名时使用随机 *.trycloudflare.com 临时链接）
./services/unsloth-studio.py up

# 2. 配置所有服务共享的主域名，再启动固定域名服务
./colabctl.py tunnel-config --base-domain example.com
./services/ollama.py up --domain
./services/qwen-flash.py up --domain     # 绑定 https://qwen.example.com（G4 GPU，800K 上下文 + 视觉）

# 3. 查看访问 URL、登录密码与 API Token
./services/ollama.py info

# 4. 查看实时日志 / 检查状态与公网连通性
./services/ollama.py logs -f
./services/ollama.py status

# 5. 停止服务并释放 Colab VM（加 --delete-tunnel 同时删除 Cloudflare 隧道和 DNS 记录）
./services/ollama.py down
./services/ollama.py down --delete-tunnel
```

## 多实例（多会话）并发支持（所有运行时通用）

所有运行时脚本（`nemotron-transcribe.py`、`comfyui.py`、`qwen-flash.py`、`ollama.py`、`unsloth-studio.py` 以及 `templates/service.py`）均原生支持通过 **`-i <ID>` / `--instance <ID>`**（或环境变量 `CT_INSTANCE=<ID>`）创建和管理多个独立的 Colab GPU 会话与隧道：

| 实例参数 | Colab 会话名 (`-s`) | 状态目录 (`.state/`) | 隧道名 (`--domain`) | 默认域名 (`--domain`) | 网盘共享目录 (`CT_DATA_DIR`) |
| --- | --- | --- | --- | --- | --- |
| 默认（不传 `-i`） | `<name>` | `.state/<name>/` | `colab-<name>` | `<host>.example.com` | `/content/drive/MyDrive/colab-llm/<name>` |
| `-i 1` | `<name>-1` | `.state/<name>-1/` | `colab-<name>-1` | `<host>-1.example.com` | `/content/drive/MyDrive/colab-llm/<name>`（共享缓存与数据） |
| `-i 2` | `<name>-2` | `.state/<name>-2/` | `colab-<name>-2` | `<host>-2.example.com` | `/content/drive/MyDrive/colab-llm/<name>`（共享缓存与数据） |

```bash
# 启动任意运行时的多个并发实例（各自拥有独立的 Colab GPU VM、Cloudflare 隧道与域名）
./services/comfyui.py up -i 1 --domain               # -> https://comfyui-1.example.com
./services/comfyui.py up -i 2 --domain               # -> https://comfyui-2.example.com
./services/qwen-flash.py up -i 2 --domain            # -> https://qwen-2.example.com
./services/ollama.py up -i 2 --domain                # -> https://ollama-2.example.com
./services/unsloth-studio.py up -i 2 --domain        # -> https://unsloth-studio-2.example.com
./services/nemotron-transcribe.py up -i 2 --domain   # -> https://transcribe-2.example.com

# 列出某个服务的所有实例状态、URL 与 API Token
./services/<name>.py ps

# 查看指定实例的详情 / 日志 / 状态
./services/<name>.py info -i 2
./services/<name>.py logs -i 2 -f
./services/<name>.py status -i 2

# 停止指定实例，或使用 --all 一键停止该服务的所有实例
./services/<name>.py down -i 2
./services/<name>.py down --all
```

## 新增一个大模型服务

1. 复制模板：
   ```bash
   cp templates/service.py services/<name>.py
   cp templates/service.ipynb services/<name>.ipynb
   chmod +x services/<name>.py
   ```
2. 修改 `services/<name>.py` 中的 `Service(...)` 配置（端口、`auth="caddy"` 或 `"builtin"`、GPU 类型）。
3. 修改 `services/<name>.ipynb` 的 **Cell 1（安装）** 和 **Cell 2（启动服务，绑定 `127.0.0.1`）**。
4. 先运行 `./services/<name>.py up --dry-run` 检查执行计划，再正式运行 `up`。

详细规范与排错指南见 [`.agents/skills/colab-llm-service/SKILL.md`](.agents/skills/colab-llm-service/SKILL.md)。
