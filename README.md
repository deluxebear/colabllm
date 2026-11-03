# Google Colab 大模型服务

在 Google Colab GPU 上运行大模型服务，通过 Cloudflare Tunnel（默认）或 FRP + Caddy 提供公网访问。
遵循 **“一个服务 = 一个 notebook (`services/<name>.ipynb`) + 一个执行脚本 (`services/<name>.py`)”** 的约定。

## 统一命令入口

在项目目录执行一次安装（无额外 Python 依赖、无需 sudo）：

```bash
./colabllm install
# 默认链接到 ~/.local/bin/colabllm；若目录不在 PATH，按安装输出设置 PATH
```

之后可以在任意目录使用 `colabllm <运行时名称> <命令> [参数...]`：

```bash
colabllm services                       # 列出全部运行时
colabllm unlimited-ocr up                # 默认 A100
colabllm unlimited-ocr ocr ./documents -O ./results
colabllm unlimited-ocr resume JOB_ID --wait
colabllm qwen3-tts up -i 2
colabllm comfyui up
colabllm ollama up --dry-run
colabllm unlimited-ocr --help

# 项目级配置也使用同一入口
colabllm tunnel-config
colabllm frp-config --help
colabllm frp-status
```

适用于所有 `services/` 中的运行时，现有参数直接透传，包含各服务专用命令和 `-i` 多实例参数。原 `./services/<名称>.py ...` 和 `./colabctl.py ...` 仍可使用。新命令使用当前 checkout 的脚本、notebook 和 `.state/`；本地相对文件路径按调用时所在目录解析。

安装链接指向当前项目目录；部署到其他机器时克隆项目、准备下文的 Colab/Drive/隧道依赖，再执行 `./colabllm install`。更新当前 checkout 后命令立即使用新代码，无需重新安装。项目移动后重新安装；已有同名命令会被保护，可用 `./colabllm install --force` 显式替换，或使用 `--bin-dir /path/to/bin` 指定安装目录。删除安装链接即可卸载入口，项目文件和运行时不受影响。

## 架构与职责

| 阶段 / 职责 | 执行位置 | 说明 |
| --- | --- | --- |
| 创建/复用 Colab 会话 (`colab new`) | 本地脚本 `services/<name>.py` | 会话名与服务名一致 |
| 免浏览器挂载网盘 (`colab dfs mount`) | 本地脚本 `services/<name>.py` | 挂载到 `/content/drive`；自动跳过 notebook 里的 `drive.mount()` |
| Cloudflare 固定域名 | 本地脚本 (`cf` CLI) | 创建/复用 named tunnel、配置 ingress 和 DNS CNAME；未配置主域名时使用动态 URL |
| FRP 固定域名 | 本地脚本 (`ssh` + `cf` CLI) | SSH 安装或复用服务端 frps/Caddy，通过 Cloudflare DNS 将域名指向服务器 IP |
| 密钥生成与注入 | 本地脚本 -> `--env` | 自动生成账号密码与 Bearer Token，保存在 `.state/<name>/state.json` (0600) |
| 安装与启动服务 | 远端 `services/<name>.ipynb` | 幂等安装，服务仅监听 `127.0.0.1` |
| 安全认证层 (Caddy) | 远端 `services/<name>.ipynb` | 对无自带认证的服务（如 Ollama）前置 Caddy：浏览器走 Basic Auth，API 走 `Authorization: Bearer <token>` |
| 启动 `cloudflared` / `frpc` 与保活 | 远端 `services/<name>.ipynb` | 最后一个 cell 持续阻塞并监控子进程；本地后台进程保持 `colab exec` 连接以防 Colab 回收会话 |

## 各运行时使用文档

| 服务名称 | 默认 GPU | 认证方式 | 固定域名示例（主域名配置为 `example.com`） | 详细使用说明 |
| --- | --- | --- | --- | --- |
| **NVIDIA-Nemotron-3.5-Transcribe-30B-A4B** | `G4` | `vLLM` Bearer API Key (`builtin`) | `https://transcribe.example.com` | [docs/nemotron-transcribe.md](docs/nemotron-transcribe.md) |
| **Qwen3-TTS** | `L4` | Bearer API Key (`builtin`) | `https://qwen3-tts.example.com` | [docs/qwen3-tts.md](docs/qwen3-tts.md) |
| **IndexTTS-2.5** | `L4` | Bearer API Key (`builtin`) | `https://indextts.example.com` | [docs/indextts.md](docs/indextts.md) |
| **Unlimited-OCR 2** | `A100` | Caddy (Basic Auth + Bearer) | `https://unlimited-ocr.example.com` | [docs/unlimited-ocr.md](docs/unlimited-ocr.md) |
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

## 本地环境与前置准备

本机负责创建 Colab 会话、配置隧道、挂载网盘和维持后台连接；模型在远端运行，**本机无需 GPU、CUDA、PyTorch 或模型权重**。

### 必需与可选依赖

| 本机环境 / 工具 | 何时需要 | 说明 |
| --- | --- | --- |
| Linux 或 macOS | 必需 | Colab CLI 当前支持的平台；本项目后台进程管理也使用 Unix 进程组 |
| Python 3.12+ | 必需 | 执行 `services/*.py`、`colabctl.py`，满足 `google-colab-cli` 的 Python 要求 |
| `uv`、Git | 推荐安装方式 / 获取项目 | `uv` 用于隔离安装 Colab CLI；项目脚本本身使用 Python 标准库 |
| `colab` CLI | 必需 | PyPI 包 `google-colab-cli`，用于会话、文件操作及执行 Notebook |
| `colab dfs` 扩展 | 默认网盘流程必需 | 额外扩展，提供 `consent`、`mount`、`doctor`；**不是安装 Colab CLI 后自带的标准功能** |
| Node.js、npm、Cloudflare `cf` CLI | 固定域名或 FRP 模式 | 本机调用 Cloudflare API 管理 DNS；Cloudflare named tunnel 也由它配置。Quick Tunnel 不需要它 |
| OpenSSH 客户端 `ssh` | FRP 模式；或 Colab SSH 排错 | 用于连接 FRP 服务器，复用 `~/.ssh/config` 的密钥、端口和跳板配置 |

准备可使用所需 GPU 运行时的 Google/Colab 账号，以及有可用空间的 Google Drive。GPU 配额、计算单元余额和机型可用性以账号当前状态为准。首次 Google、Drive 和 Cloudflare 授权需要浏览器完成。

### 1. 安装 Colab CLI

先按 [uv 官方说明](https://docs.astral.sh/uv/getting-started/installation/) 安装 `uv`，并准备 Python 3.12+。使用独立工具环境安装：

```bash
uv tool install google-colab-cli
uv tool update-shell
# 重新打开终端，或按 update-shell 的提示加载 PATH
colab version
colab --help
```

安装来源：[google-colab-cli](https://github.com/googlecolab/google-colab-cli)。首次运行 `colab sessions` 时，按 CLI 的提示完成 Google 认证；需要自行提供 OAuth 客户端配置的环境，应按上游说明放在默认的 `~/.colab-cli-oauth-config.json`。本项目后台调用 `colab`，因此应先在同一用户环境中完成授权。

```bash
colab sessions       # 确认可访问 Colab；不创建新 VM
colab usage          # 查看账号计算单元用量/余额
```

### 2. 安装并授权 `colab dfs` 扩展

本项目启动时默认调用 `colab dfs mount`，不依赖 Notebook 内交互式的 `drive.mount()`。`colab dfs` 是**独立的本地扩展**；本仓库目前未分发扩展源码或独立安装包。新机器需先取得兼容的扩展并按其安装方式注入到提供 `colab` 命令的 Python 环境，不能只执行 `uv tool install google-colab-cli` 就认为网盘依赖已齐备。

当前部署使用的扩展源码位于 `~/.colab-dfs-ext/`。安装好扩展后检查并进行一次性 Drive 授权：

```bash
colab dfs --help
colab dfs version
colab dfs doctor
colab dfs consent     # 浏览器完成一次授权，后续挂载无需再次交互
colab dfs show        # 查看授权摘要，不打印 refresh token
```

Drive 授权与 Colab 会话认证分别进行。扩展凭据保存在 `~/.config/colab-cli/dfs_credentials.json`。正常使用时，服务脚本自动挂载远端 `/content/drive`，无需手工调用 `mount`；已有空闲会话可用 `colab dfs mount -s <会话名>` 验证。

升级 Colab CLI 后，应再次执行 `colab dfs --help` 检查扩展是否仍在；命令可用时可运行 `colab dfs install` 重新注入。若出现 `No such command: dfs`，且原扩展源码仍在 `~/.colab-dfs-ext/`，可从源码恢复注入：

```bash
PYTHONPATH="$HOME/.colab-dfs-ext" python3 - <<'PYDFS'
from colab_dfs_ext.inject import ensure
result = ensure()
if result.get("error"):
    raise SystemExit(result["error"])
print("colab dfs 注入完成")
PYDFS
colab dfs --help
```

上面的恢复命令以已有扩展源码为前提，不会下载缺失的扩展。`up --no-drive` 可跳过自动挂载，但需要网盘音色、任务持久化和输出保存的功能仍依赖 Drive，不适合作为这些功能的替代配置。

### 3. 安装 Cloudflare `cf` CLI

使用固定域名时，无论选择 Cloudflare Tunnel 还是 FRP，都需要在**本机**安装并授权 `cf`。这里的 `cf` 是 Cloudflare CLI，不是 Cloud Foundry CLI，也不是远端运行的 `cloudflared`。

准备符合所安装 `cf` 版本要求的 Node.js/npm，然后安装：

```bash
node --version
npm --version
npm install --global cf
cf --version
cf auth login
cf auth whoami
```

安装和认证说明见 [Cloudflare 官方文档](https://developers.cloudflare.com/cf/get-started/)。账号需有目标域名的 Zone/DNS 管理权限；使用 named tunnel 时还需有隧道管理权限。`cf zones list` 可检查域名是否在当前账号中。只使用 Cloudflare 动态 URL 时，可跳过本节。

本机已验证的组合为 `google-colab-cli 0.7.5`、`colab dfs 0.1.0`、`cf 1.0.0-beta.26` 和 Node.js `26.13.0`。其中该 `cf` 版本的 Node.js 要求为 `^22.23.0 || ^24.22.0 || ^26.12.0`；升级版本时以 npm 包的 `engines` 声明为准，避免仅按大版本判断兼容性。

### 4. FRP 模式额外要求

本机需要 OpenSSH，以及能够非交互连接服务器的密钥配置：

```bash
ssh -V
ssh -o BatchMode=yes root@frpip 'id -u'
```

`root@frpip` 是示例，替换为实际 SSH 目标；首次连接应先核实服务器主机指纹。服务器需要 Linux、systemd、Python 3，以及 root 或免密 sudo；读取既有 FRP TOML 配置需要服务器 Python 3.11+。服务端需允许公网 TCP 7000、80 和 443。配置命令会安装或复用服务器上的 frps/Caddy，本机不需要预先安装它们，详见 [FRP 操作说明](docs/frp.md)。

### 启动前自检

在项目根目录、实际用于运行服务的终端中执行：

```bash
python3 --version
command -v colab
colab version
colab dfs --help
colab dfs show
./colabctl.py tunnel-config
# 固定域名 / FRP 模式还需检查
command -v cf
cf --version
cf auth whoami
# FRP 模式还需检查
ssh -V
./colabctl.py frp-status
```

认证文件保存在本机用户目录，运行时状态保存在已被 Git 忽略的 `.state/`。执行脚本的本机需要持续在线且避免休眠：后台 runner 维持 `colab exec` 连接，断开可能导致 Colab 会话被回收。远端所需模型依赖、`cloudflared`/`frpc` 和认证 Caddy 由 Notebook 安装，不需要在本机重复安装。

## Cloudflare Tunnel 配置

通过项目根目录的 `colabctl.py` 统一配置；所有现有及新增运行时、所有实例均读取同一份主域名配置，保存在本机 `.state/tunnel-config.json`。未启用 FRP 且配置为空时，`up`（包括不带 HOST 的 `up --domain`）使用 Cloudflare Quick Tunnel 的随机 `https://*.trycloudflare.com` URL，无需 `cf` 登录；每次启动 URL 可能变化。代码中的历史 `example.com` 后缀不再自动启用。

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

Cloudflare 固定域名要求 `cf auth login` 且账户有该域名的 DNS Zone 权限，启动时自动创建/复用 named tunnel、设置 ingress 和 DNS CNAME。配置修改仅影响之后的启动，不会重启运行中的服务；已有服务需自行择机重启。切换动态模式不会删除旧隧道和 DNS，可使用 `down --delete-tunnel` 显式清理。

## FRP 后端（项目全局）

默认使用 Cloudflare Tunnel；全局配置 FRP 后，所有运行时的新启动自动使用 FRP + 服务端 Caddy HTTPS。

```bash
# 配置并通过 SSH 自动安装服务端（替换示例 IP、SSH 目标和域名）
./colabctl.py frp-config --server-ip 203.0.113.10 --ssh root@frpip --base-domain example.com
./colabctl.py frp-status
./services/indextts.py up

# 本次覆盖 / 关闭全局 FRP
./services/indextts.py up --transport cloudflare
./services/indextts.py up --quick-tunnel
./colabctl.py frp-config --disable
```

FRP 服务端默认 TCP 7000，Caddy 使用 80/443；DNS 经本机 `cf` 自动配置为 DNS only。配置、自动安装、认证及清理说明见 [FRP 操作说明](docs/frp.md)。现有运行时不会自动切换。

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
