# Google Colab 大模型服务

在 Google Colab GPU 上运行大模型服务并通过 Cloudflare Tunnel 暴露公网访问。
遵循 **“一个服务 = 一个 notebook (`services/<name>.ipynb`) + 一个执行脚本 (`services/<name>.py`)”** 的约定。

## 架构与职责

| 阶段 / 职责 | 执行位置 | 说明 |
| --- | --- | --- |
| 创建/复用 Colab 会话 (`colab new`) | 本地脚本 `services/<name>.py` | 会话名与服务名一致 |
| 免浏览器挂载网盘 (`colab dfs mount`) | 本地脚本 `services/<name>.py` | 挂载到 `/content/drive`；自动跳过 notebook 里的 `drive.mount()` |
| 固定域名 (`--domain`) | 本地脚本 (`cf` CLI) | 创建/复用 `colab-<name>` 隧道、配置 ingress、绑定 DNS CNAME（默认后缀 `example.com`） |
| 密钥生成与注入 | 本地脚本 -> `--env` | 自动生成账号密码与 Bearer Token，保存在 `.state/<name>/state.json` (0600) |
| 安装与启动服务 | 远端 `services/<name>.ipynb` | 幂等安装，服务仅监听 `127.0.0.1` |
| 安全认证层 (Caddy) | 远端 `services/<name>.ipynb` | 对无自带认证的服务（如 Ollama）前置 Caddy：浏览器走 Basic Auth，API 走 `Authorization: Bearer <token>` |
| 启动 `cloudflared` 与保活 | 远端 `services/<name>.ipynb` | 最后一个 cell 持续阻塞并监控子进程；本地后台进程保持 `colab exec` 连接以防 Colab 回收会话 |

## 目录结构

```
├── .agents/skills/colab-llm-service/   # 本项目 Agent Skill（最佳实践、排错、认证与隧道参考）
├── lib/colabsvc.py                     # 共享运行库（Service 定义与 CLI 实现）
├── services/
│   ├── comfyui.py                      # ComfyUI + ComfyUI-Manager 执行脚本（Caddy 认证，默认 L4，支持 --workflow / pull-workflow 按工作流即时下载模型到本地 SSD）
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

## 常用命令

以 `unsloth-studio` 和 `ollama` 为例：

```bash
# 1. 启动服务（默认使用随机 *.trycloudflare.com 临时链接）
./services/unsloth-studio.py up

# 2. 启动并绑定固定域名（默认 <name>.example.com，或服务自定义的 default_hostname）
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
