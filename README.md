# Google Colab 大模型服务

在 Google Colab GPU 上运行大模型服务，通过 Cloudflare Tunnel（默认）或 FRP + Caddy 提供公网访问。
遵循 **“一个服务 = 一个 notebook (`src/colabllm/services/<name>.ipynb`) + 一个执行脚本 (`src/colabllm/services/<name>.py`)”** 的约定。

## 统一命令入口

用户通过分发包安装，无需克隆源码。从 [GitHub Releases](https://github.com/deluxebear/colabllm/releases) 下载 wheel 后安装（私有仓库需要相应访问权限）：

```bash
uv tool install ./colabllm-0.1.0-py3-none-any.whl
# 如果命令目录尚未加入 PATH
uv tool update-shell
```

版本发布到 PyPI 后，也可以直接安装和升级：

```bash
uv tool install colabllm
uv tool upgrade colabllm
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

所有运行脚本和 notebook 都包含在安装包中。本地相对输入/输出路径按调用时所在目录解析。
本项目的配置和运行状态统一写入 `~/.colabllm/`，不读取仓库旧 `.state/`，不提供旧配置迁移。
可设置 `COLABLLM_HOME=/absolute/path` 使用独立目录。

```text
~/.colabllm/
├── tunnel-config.json           # 全局主域名与 FRP 设置
├── services/<name>/state.json   # 实例凭据、会话、隧道与 runner PID
├── services/<name>/run.log      # 日志；执行用 notebook 也在实例目录
└── frp/<server-id>.json         # FRP 私密凭据
```

`colab`、`cf` 和 `ssh` 的授权/配置仍由各自工具管理；Google Drive 数据仍保存在远端。
卸载使用 `uv tool uninstall colabllm`，用户配置目录保留。
升级前先停止运行中的服务，升级后重新启动，避免后台 runner 依赖的安装文件被替换。

## 运行时使用指南

每个运行时的具体命令、API/UI 工作流、输入输出及恢复语义见下方“各运行时使用文档”表。
共享规则集中在 [运行时通用操作](https://github.com/deluxebear/colabllm/blob/master/docs/runtime-common.md)，可先阅读以理解认证、Drive 路径、超时和多实例。
`docs/<运行时>.md` 是用法说明的维护来源，用户技能内的对应参考由同步工具生成：

```bash
python tools/sync_runtime_docs.py
python tools/sync_runtime_docs.py --check
```

修改用法或新增运行时后需同步；本地测试及 CI 会检查指南完整性、示例参数和技能内容一致。

## 安装 Agent Skill

根目录 [skills/colabllm](https://github.com/deluxebear/colabllm/blob/master/skills/colabllm/SKILL.md) 提供面向使用者的独立技能，覆盖本项目所有运行时。
它包含按任务加载的 OCR、语音、推理、媒体和排错参考，不依赖源码仓库或个人部署配置。

wheel 和源码包均包含完整技能，安装 CLI 后即可导出：

```bash
# 查看主技能说明
colabllm skill

# Codex：将完整 colabllm/ 技能目录导出到个人技能目录
colabllm skill --output "${CODEX_HOME:-$HOME/.codex}/skills"

# 支持 ~/.agents/skills 的 Agent
colabllm skill --output ~/.agents/skills

# 其他 Agent：指定其实际技能目录
colabllm skill --output /path/to/agent/skills
```

`--output` 是技能父目录，实际生成 `<目录>/colabllm/SKILL.md` 和 `references/`。
目标已存在时返回错误，不覆盖用户修改。更新时先导出到新目录，比较后替换；CLI 升级不会自动覆盖已安装的技能。
也可以直接从源码复制整个 `skills/colabllm/` 目录。安装后按 Agent 的方式重新加载技能或开启新会话，
再请求“使用 colabllm 将本地 PDF 转为 Markdown”等任务。

仓库内 `.agents/skills/colab-llm-service/` 是维护者技能；分发给使用者的是 `skills/colabllm/`。

## 开发、构建与发布

```bash
# 开发安装：修改源码立即生效，源码目录需要保留
uv tool install --editable .

# 普通本地安装 / 更新
uv tool install --force .

# 构建 wheel 和源码分发包（dist/）
uv build --no-sources

# 验证 wheel 在隔离环境中的安装与运行
python tools/check_distribution.py dist/colabllm-0.1.0-py3-none-any.whl
```

版本号以 `pyproject.toml` 为唯一来源，CLI 从已安装包元数据读取版本。
采用 `MAJOR.MINOR.PATCH`：修复递增 PATCH、新功能递增 MINOR、不兼容变更递增 MAJOR；
在 `0.x` 开发阶段，不兼容变更递增 MINOR，并在 [CHANGELOG.md](https://github.com/deluxebear/colabllm/blob/master/CHANGELOG.md) 中说明。
当前首发版本为 `0.1.0`，对应 Git 标签 `v0.1.0`；后续发布示例：

```bash
uv version --bump patch
# 将 CHANGELOG.md 的 Unreleased 内容整理到本次版本的日期条目
python tools/check_release.py --tag v0.1.1
git add pyproject.toml CHANGELOG.md
git commit -m "chore: release 0.1.1"
git tag -a v0.1.1 -m "Release 0.1.1"
git push origin master
git push origin v0.1.1
```

推送 `v*` 标签触发 [release.yml](https://github.com/deluxebear/colabllm/blob/master/.github/workflows/release.yml)：

1. 验证标签、包版本和变更记录一致，复用 Linux/macOS × Python 3.12–3.14 CI。
2. 构建 wheel 和 sdist，验证隔离安装，生成 SHA256SUMS 并保存 Actions artifact。
3. 发布 GitHub Release，附带安装包及校验文件，发布说明取自当前版本的 CHANGELOG 条目。
4. 将同一组已验证的包通过 Trusted Publishing 上传 PyPI。

首次启用 PyPI 需要项目所有者在 [PyPI Publishing](https://pypi.org/manage/account/publishing/)
添加 Pending Publisher：项目 `colabllm`、Owner `deluxebear`、Repository `colabllm`、
Workflow `release.yml`、Environment `pypi`。已有 PyPI 项目则在项目 Publishing 设置添加同样的发布者。
Actions 使用 `pypi` environment 和 OIDC，无需配置长期 PyPI API Token。
参考 [PyPI Trusted Publisher 说明](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)。

PyPI 上传失败时，GitHub Release 仍可下载；修复身份配置后在 Actions 选择 **Re-run failed jobs**。
也可以针对已有标签手动运行 release workflow；已发布的 GitHub Release 会拒绝被覆盖，应优先只重跑失败任务。
已发布版本不移动标签或替换安装包，修复后发布新版本。
通过本地 wheel 安装的用户可安装新 wheel 更新；通过 PyPI 安装的用户使用 `uv tool upgrade colabllm`。

## CLI 行为约定

- `colabllm <服务> --help` 列出生命周期和服务专用命令；`colabllm help <服务> <命令>` 查看详细帮助。
- `-i/--instance` 可放在服务参数的任意位置，`--` 之后按字面解析；实例 ID 必须为 1–64 位字母、数字、下划线或连字符，且不能以连字符开头。
- 普通结果写入 stdout，进度和告警写入 stderr；`--json` 的 stdout 只包含 JSON。
- `info`、`ps`、`list`、`status` 支持 `--json`，默认隐藏凭据；需要时显式加 `--show-secrets`。
- `--quiet` 抑制诊断进度，错误仍然显示；`--no-color` 或 `NO_COLOR` 关闭诊断颜色。非终端输出自动关闭诊断颜色。
- 常见网络、文件和配置错误默认给出简洁提示；`--debug` 显示异常堆栈。
- 成功退出码为 0；操作失败为非零，参数用法错误为 2；外部 Colab 命令失败时保留其正退出码；Ctrl+C 为 130。
- 就绪标记只接受完整的 `CT_READY` 协议行；日志中的普通文字提及不会被视为就绪。
- `up` 默认等待 `CT_READY`；等待超时返回失败，但后台 runner 保留。`up --detach` 仅启动后台 runner，立即返回；`up --foreground` 在当前终端保持连接。
- 同一实例的启动和停止互斥；状态事务使用文件锁和原子替换。前台/后台 runner 都记录进程启动身份，停止前校验身份并等待退出。
- `down` 先确认远端停止成功，再停止本地连接并清理运行状态；远端停止失败时保留连接和追踪信息，便于重试。
- 携带认证信息的 HTTP 请求拒绝跨来源重定向。`--url` 或服务 URL 环境变量切换来源时，需要显式提供 Token，不能自动借用原服务的凭据。来源按协议、主机和有效端口比较。

## 超时与环境诊断

```bash
# 外部命令上限：可放在服务名前后，也可设置 COLABLLM_COMMAND_TIMEOUT
colabllm --command-timeout 60 ollama status

# 默认模式超时只停止等待，后台 runner 保留；前台模式超时会断开本地连接
colabllm ollama up --ready-timeout 600
colabllm ollama up --foreground --ready-timeout 600

# 轮询等待上限：0 保留不限时行为；每次请求也会受剩余时间约束
colabllm unlimited-ocr job JOB_ID --wait --timeout 300
colabllm comfyui pull-workflow TEMPLATE --timeout 300 --poll 2
colabllm longcat-avatar job JOB_ID --wait --timeout 300

# 默认只检查本机依赖、Drive 扩展、授权文件存在性和用户配置
colabllm doctor
colabllm doctor --json --transport quick --no-drive

# 额外验证 Colab、Cloudflare、SSH 授权，按选中的后端执行限时只读检查
colabllm doctor --online --timeout 10
```

超时退出码为 `124`。长驻的 `colab exec` 保活连接不受通用外部命令上限影响。
未指定 `--command-timeout` 时，共享外部命令默认 300 秒，个别下载或 SSH 操作保留专属默认上限。
`doctor` 不创建 GPU 会话、不挂载 Drive、不修改隧道，也不显示凭据或外部命令的原始输出。
离线检查不会证明授权仍有效；需要时使用 `--online`，该模式禁用浏览器交互并限制每项检查时间。

## Shell 补全

命令、选项和枚举值来自实际解析器；实例 ID 从 `~/.colabllm/services/` 的目录名读取，补全过程不读取凭据、不联网。

```bash
# Bash：在当前 Shell 加载
source <(colabllm completion bash)

# Zsh：先启用补全系统，再加载
# autoload -Uz compinit && compinit
source <(colabllm completion zsh)
```

Fish 中执行 `colabllm completion fish | source`。需要持久启用时，将相应加载命令加入自己的 Shell 初始化文件，
或将脚本保存在 `~/.colabllm/completions/` 后从初始化文件加载。

## 测试

本地 CLI 与控制逻辑使用 Python 标准库，默认测试无需 Torch、NumPy 或 GPU：

```bash
python -m unittest discover -s tests
```

`tests_colab/` 单独保存 Colab 端模型代码的回归测试（如 LongCat attention 和音频分段）。
它们会导入模型运行代码，仅在对应的 Colab 服务环境中运行；attention 测试还需要 CUDA 和上游 LongCat 源码。
在该环境中，将项目源码放入 VM 后执行：

```bash
# LongCat-Video VM 使用 /content/longcat-video/repo；Avatar VM 使用 /content/longcat-avatar/repo
PYTHONPATH=/content/longcat-video/repo python -m unittest discover -s tests_colab
```

GitHub Actions 在 Linux/macOS、Python 3.12/3.13/3.14 上运行本地测试、三种 Shell 补全测试、构建和隔离 wheel 安装验证。工作流不执行模型测试、不启动 Colab、不自动发布包。

本地缺少 Zsh/Fish 时跳过对应 Shell 集成测试；CI 会安装这些 Shell。
本地测试中的真实 FRP 集成测试需额外指定 `COLABSVC_FRP_TEST_BIN`，默认跳过。

## 架构与职责

| 阶段 / 职责 | 执行位置 | 说明 |
| --- | --- | --- |
| 创建/复用 Colab 会话 (`colab new`) | 本地脚本 `src/colabllm/services/<name>.py` | 会话名与服务名一致 |
| 免浏览器挂载网盘 (`colab dfs mount`) | 本地脚本 `src/colabllm/services/<name>.py` | 挂载到 `/content/drive`；自动跳过 notebook 里的 `drive.mount()` |
| Cloudflare 固定域名 | 本地脚本 (`cf` CLI) | 创建/复用 named tunnel、配置 ingress 和 DNS CNAME；未配置主域名时使用动态 URL |
| FRP 固定域名 | 本地脚本 (`ssh` + `cf` CLI) | SSH 安装或复用服务端 frps/Caddy，通过 Cloudflare DNS 将域名指向服务器 IP |
| 密钥生成与注入 | 本地脚本 -> `--env` | 自动生成账号密码与 Bearer Token，保存在 `~/.colabllm/services/<name>/state.json` (0600) |
| 安装与启动服务 | 远端 `src/colabllm/services/<name>.ipynb` | 幂等安装，服务仅监听 `127.0.0.1` |
| 安全认证层 (Caddy) | 远端 `src/colabllm/services/<name>.ipynb` | 对无自带认证的服务（如 Ollama）前置 Caddy：浏览器走 Basic Auth，API 走 `Authorization: Bearer <token>` |
| 启动 `cloudflared` / `frpc` 与保活 | 远端 `src/colabllm/services/<name>.ipynb` | 最后一个 cell 持续阻塞并监控子进程；本地后台进程保持 `colab exec` 连接以防 Colab 回收会话 |

## 各运行时使用文档

| 服务名称 | 默认 GPU | 认证方式 | 固定域名示例（主域名配置为 `example.com`） | 详细使用说明 |
| --- | --- | --- | --- | --- |
| **NVIDIA-Nemotron-3.5-Transcribe-30B-A4B** | `G4` | `vLLM` Bearer API Key (`builtin`) | `https://transcribe.example.com` | [docs/nemotron-transcribe.md](https://github.com/deluxebear/colabllm/blob/master/docs/nemotron-transcribe.md) |
| **Qwen3-TTS** | `L4` | Bearer API Key (`builtin`) | `https://qwen3-tts.example.com` | [docs/qwen3-tts.md](https://github.com/deluxebear/colabllm/blob/master/docs/qwen3-tts.md) |
| **IndexTTS-2.5** | `L4` | Bearer API Key (`builtin`) | `https://indextts.example.com` | [docs/indextts.md](https://github.com/deluxebear/colabllm/blob/master/docs/indextts.md) |
| **Unlimited-OCR 2** | `A100` | Caddy (Basic Auth + Bearer) | `https://unlimited-ocr.example.com` | [docs/unlimited-ocr.md](https://github.com/deluxebear/colabllm/blob/master/docs/unlimited-ocr.md) |
| **LongCat-Video-Avatar 1.5** | `G4` | Caddy (Basic Auth + Bearer) | `https://longcat-avatar.example.com` | [docs/longcat-avatar.md](https://github.com/deluxebear/colabllm/blob/master/docs/longcat-avatar.md) |
| **LongCat-Video** | `G4` | Caddy (Basic Auth + Bearer) | `https://longcat-video.example.com` | [docs/longcat-video.md](https://github.com/deluxebear/colabllm/blob/master/docs/longcat-video.md) |
| **ComfyUI + ComfyUI-Manager** | `L4` | Caddy (Basic Auth + Bearer) | `https://comfyui.example.com` | [docs/comfyui.md](https://github.com/deluxebear/colabllm/blob/master/docs/comfyui.md) |
| **Qwen3.8-Flash-Next (800K + Vision)** | `G4` | `llama-server` Bearer API Key | `https://qwen.example.com` | [docs/qwen-flash.md](https://github.com/deluxebear/colabllm/blob/master/docs/qwen-flash.md) |
| **Ollama** | `T4` | Caddy (Basic Auth + Bearer) | `https://ollama.example.com` | [docs/ollama.md](https://github.com/deluxebear/colabllm/blob/master/docs/ollama.md) |
| **Unsloth Studio** | `L4` | 内置 Web 登录 (`builtin`) | `https://unsloth-studio.example.com` | [docs/unsloth-studio.md](https://github.com/deluxebear/colabllm/blob/master/docs/unsloth-studio.md) |

## 目录结构

```text
LICENSE                        # MIT 开源许可证
pyproject.toml                 # 包元数据、版本、colabllm 命令入口
src/colabllm/
├── cli.py                     # 统一命令分发
├── control.py                 # 全局域名与 FRP 管理
├── utility.py                 # 通用工具子命令
├── lib/                       # 本地共享 runner、状态、HTTP/音频工具与 FRP 实现
└── services/                  # <name>.py + <name>.ipynb；随包安装
templates/                     # notebook 生成器 build_notebooks.py 与嵌入 notebook 的独立模块
docs/                          # 各运行时与 FRP/通用操作文档
skills/colabllm/               # 随包分发的用户 Agent Skill（references 由 docs 同步生成）
tools/                         # 文档同步、分发包校验、基准测试等开发工具
tests/                         # 本地测试（Python 标准库）
tests_colab/                   # 仅在 Colab 模型环境运行的回归测试
```

## 本地环境与前置准备

本机负责创建 Colab 会话、配置隧道、挂载网盘和维持后台连接；模型在远端运行，**本机无需 GPU、CUDA、PyTorch 或模型权重**。

### 必需与可选依赖

| 本机环境 / 工具 | 何时需要 | 说明 |
| --- | --- | --- |
| Linux 或 macOS | 必需 | Colab CLI 当前支持的平台；本项目后台进程管理也使用 Unix 进程组 |
| Python 3.12+ | 必需 | 执行 `src/colabllm/services/*.py`、`src/colabllm/control.py`，满足 `google-colab-cli` 的 Python 要求 |
| `uv` | 包安装与升级 | 隔离安装 colabllm 和 Colab CLI；Git 仅开发时需要 |
| `colab` CLI | 必需 | PyPI 包 `google-colab-cli`，用于会话、文件操作及执行 Notebook |
| `colab dfs` 扩展 | 默认网盘流程必需 | [colab-dfs-ext](https://github.com/deluxebear/colab-dfs-ext)，提供 `consent`、`mount`、`doctor`；需单独安装 |
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

本项目启动时默认调用 `colab dfs mount`，不依赖 Notebook 内交互式的 `drive.mount()`。扩展来自 [deluxebear/colab-dfs-ext](https://github.com/deluxebear/colab-dfs-ext)，需在安装 Colab CLI 后单独安装：

```bash
curl -fsSL https://raw.githubusercontent.com/deluxebear/colab-dfs-ext/main/install.sh | sh
```

也可以克隆扩展仓库后运行 `python3 install.py`。安装器将源码保存到 `~/.colab-dfs-ext/`，并自动接入提供 `colab` 命令的 Python 环境；详细选项见[扩展中文说明](https://github.com/deluxebear/colab-dfs-ext/blob/main/README.zh-CN.md)。安装后检查并进行一次性 Drive 授权：

```bash
colab dfs --help
colab dfs version
colab dfs doctor
colab dfs consent     # 浏览器完成一次授权，后续挂载无需再次交互
colab dfs show        # 查看授权摘要，不打印 refresh token
```

Drive 授权与 Colab 会话认证分别进行。扩展凭据保存在 `~/.config/colab-cli/dfs_credentials.json`。正常使用时，服务脚本自动挂载远端 `/content/drive`，无需手工调用 `mount`；已有空闲会话可用 `colab dfs mount -s <会话名>` 验证。

升级 Colab CLI 后，可执行 `colab dfs --help` 检查扩展是否仍在；命令可用时可运行 `colab dfs install` 重新接入。若出现 `No such command: dfs`，且扩展仍在 `~/.colab-dfs-ext/`，可重新运行安装器修复：

```bash
python3 "$HOME/.colab-dfs-ext/install.py"
colab dfs --help
```

若扩展目录缺失，重新运行上面的安装命令。`up --no-drive` 可跳过自动挂载，但需要网盘音色、任务持久化和输出保存的功能仍依赖 Drive，不适合作为这些功能的替代配置。

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

`root@frpip` 是示例，替换为实际 SSH 目标；首次连接应先核实服务器主机指纹。服务器需要 Linux、systemd、Python 3，以及 root 或免密 sudo；读取既有 FRP TOML 配置需要服务器 Python 3.11+。服务端需允许公网 TCP 7000、80 和 443。配置命令会安装或复用服务器上的 frps/Caddy，本机不需要预先安装它们，详见 [FRP 操作说明](https://github.com/deluxebear/colabllm/blob/master/docs/frp.md)。

### 启动前自检

在项目根目录、实际用于运行服务的终端中执行：

```bash
python3 --version
command -v colab
colab version
colab dfs --help
colab dfs show
colabllm tunnel-config
# 固定域名 / FRP 模式还需检查
command -v cf
cf --version
cf auth whoami
# FRP 模式还需检查
ssh -V
colabllm frp-status
```

认证文件保存在本机用户目录，运行时状态保存在用户目录 `~/.colabllm/services/`。执行脚本的本机需要持续在线且避免休眠：后台 runner 维持 `colab exec` 连接，断开可能导致 Colab 会话被回收。远端所需模型依赖、`cloudflared`/`frpc` 和认证 Caddy 由 Notebook 安装，不需要在本机重复安装。

## Cloudflare Tunnel 配置

通过 `colabllm tunnel-config` 统一配置；所有现有及新增运行时、所有实例均读取同一份主域名配置，保存在本机 `~/.colabllm/tunnel-config.json`。未启用 FRP 且配置为空时，`up`（包括不带 HOST 的 `up --domain`）使用 Cloudflare Quick Tunnel 的随机 `https://*.trycloudflare.com` URL，无需 `cf` 登录；每次启动 URL 可能变化。代码中的历史 `example.com` 后缀不再自动启用。

```bash
# 查看 / 设置 / 清除全局主域名（项目根目录统一入口）
colabllm tunnel-config
colabllm tunnel-config --base-domain example.com
colabllm tunnel-config --clear

# 只为本次启动指定主域名，不写入配置
colabllm longcat-avatar up --base-domain example.com
# -> https://longcat-avatar.example.com
colabllm qwen-flash up -i 2 --base-domain example.com
# -> https://qwen-2.example.com（保留服务别名与实例后缀）

# 已配置主域名后：up 自动使用固定域名；也可以强制动态 URL
colabllm longcat-avatar up
colabllm longcat-avatar up --quick-tunnel

# 完整域名覆盖自动生成的服务域名
colabllm longcat-avatar up --domain avatar.example.com
```

可设置环境变量 `COLABSVC_BASE_DOMAIN=example.com`。选择顺序：`--quick-tunnel` 强制动态；完整 `--domain HOST` > `--base-domain` > 环境变量 > 全局配置 > 动态 URL。三个启动选项互斥。域名只填 DNS 名称，不包含 `https://`、端口或路径。

Cloudflare 固定域名要求 `cf auth login` 且账户有该域名的 DNS Zone 权限，启动时自动创建/复用 named tunnel、设置 ingress 和 DNS CNAME。配置修改仅影响之后的启动，不会重启运行中的服务；已有服务需自行择机重启。切换动态模式不会删除旧隧道和 DNS，可使用 `down --delete-tunnel` 显式清理。

## FRP 后端（项目全局）

默认使用 Cloudflare Tunnel；全局配置 FRP 后，所有运行时的新启动自动使用 FRP + 服务端 Caddy HTTPS。

```bash
# 配置并通过 SSH 自动安装服务端（替换示例 IP、SSH 目标和域名）
colabllm frp-config --server-ip 203.0.113.10 --ssh root@frpip --base-domain example.com
colabllm frp-status
colabllm indextts up

# 本次覆盖 / 关闭全局 FRP
colabllm indextts up --transport cloudflare
colabllm indextts up --quick-tunnel
colabllm frp-config --disable
```

FRP 服务端默认 TCP 7000，Caddy 使用 80/443；DNS 经本机 `cf` 自动配置为 DNS only。配置、自动安装、认证及清理说明见 [FRP 操作说明](https://github.com/deluxebear/colabllm/blob/master/docs/frp.md)。现有运行时不会自动切换。

## 常用命令

以 `unsloth-studio` 和 `ollama` 为例：

```bash
# 1. 启动服务（未配置主域名时使用随机 *.trycloudflare.com 临时链接）
colabllm unsloth-studio up

# 2. 配置所有服务共享的主域名，再启动固定域名服务
colabllm tunnel-config --base-domain example.com
colabllm ollama up --domain
colabllm qwen-flash up --domain     # 绑定 https://qwen.example.com（G4 GPU，800K 上下文 + 视觉）

# 3. 查看访问 URL、登录密码与 API Token
colabllm ollama info --show-secrets

# 4. 查看实时日志 / 检查状态与公网连通性
colabllm ollama logs -f
colabllm ollama status

# 5. 停止服务并释放 Colab VM（加 --delete-tunnel 同时删除 Cloudflare 隧道和 DNS 记录）
colabllm ollama down
colabllm ollama down --delete-tunnel
```

## 多实例（多会话）并发支持（所有运行时通用）

所有运行时脚本（`nemotron-transcribe.py`、`comfyui.py`、`qwen-flash.py`、`ollama.py`、`unsloth-studio.py` 以及 `templates/service.py`）均原生支持通过 **`-i <ID>` / `--instance <ID>`**（或环境变量 `CT_INSTANCE=<ID>`）创建和管理多个独立的 Colab GPU 会话与隧道：

| 实例参数 | Colab 会话名 (`-s`) | 状态目录 (`~/.colabllm/services/`) | 隧道名 (`--domain`) | 默认域名 (`--domain`) | 网盘共享目录 (`CT_DATA_DIR`) |
| --- | --- | --- | --- | --- | --- |
| 默认（不传 `-i`） | `<name>` | `~/.colabllm/services/<name>/` | `colab-<name>` | `<host>.example.com` | `/content/drive/MyDrive/colab-llm/<name>` |
| `-i 1` | `<name>-1` | `~/.colabllm/services/<name>-1/` | `colab-<name>-1` | `<host>-1.example.com` | `/content/drive/MyDrive/colab-llm/<name>`（共享缓存与数据） |
| `-i 2` | `<name>-2` | `~/.colabllm/services/<name>-2/` | `colab-<name>-2` | `<host>-2.example.com` | `/content/drive/MyDrive/colab-llm/<name>`（共享缓存与数据） |

```bash
# 启动任意运行时的多个并发实例（各自拥有独立的 Colab GPU VM、Cloudflare 隧道与域名）
colabllm comfyui up -i 1 --domain               # -> https://comfyui-1.example.com
colabllm comfyui up -i 2 --domain               # -> https://comfyui-2.example.com
colabllm qwen-flash up -i 2 --domain            # -> https://qwen-2.example.com
colabllm ollama up -i 2 --domain                # -> https://ollama-2.example.com
colabllm unsloth-studio up -i 2 --domain        # -> https://unsloth-studio-2.example.com
colabllm nemotron-transcribe up -i 2 --domain   # -> https://transcribe-2.example.com

# 列出某个服务的所有实例状态、URL 与 API Token
colabllm <name> ps

# 查看指定实例的详情 / 日志 / 状态
colabllm <name> info --show-secrets -i 2
colabllm <name> logs -i 2 -f
colabllm <name> status -i 2

# 停止指定实例，或使用 --all 一键停止该服务的所有实例
colabllm <name> down -i 2
colabllm <name> down --all
```

## 新增一个大模型服务

1. 复制模板：
   ```bash
   cp templates/service.py src/colabllm/services/<name>.py
   cp templates/service.ipynb src/colabllm/services/<name>.ipynb
   chmod +x src/colabllm/services/<name>.py
   ```
2. 修改 `src/colabllm/services/<name>.py` 中的 `Service(...)` 配置（端口、`auth="caddy"` 或 `"builtin"`、GPU 类型）。
3. 修改 `src/colabllm/services/<name>.ipynb` 的 **Cell 1（安装）** 和 **Cell 2（启动服务，绑定 `127.0.0.1`）**。
4. 先运行 `colabllm <name> up --dry-run` 检查执行计划，再正式运行 `up`。
5. 同一变更中更新 `docs/<name>.md`，运行 `python tools/sync_runtime_docs.py` 生成对应的技能参考文档，并更新 `skills/colabllm/SKILL.md` 的运行时索引，覆盖启动、调用、输入输出、认证和任务恢复。新增、删除运行时或修改命令行为都需要同步维护用户技能。
6. 核对技能示例与实际 CLI 参数一致，构建分发包，并用 `tools/check_distribution.py` 验证安装、运行时发现和完整技能导出；新增文件类型时同步调整打包规则。运行时及其用户技能均可从安装包使用后，才算完成新增流程。

详细规范与排错指南见 [`.agents/skills/colab-llm-service/SKILL.md`](https://github.com/deluxebear/colabllm/blob/master/.agents/skills/colab-llm-service/SKILL.md)。

## 修改 notebook

`src/colabllm/services/*.ipynb` 与 `templates/service.ipynb` 由 `templates/build_notebooks.py` 生成，请勿直接手改。
中间件、缓存、隧道等逻辑以独立模块保存在 `templates/`（如 `colab_asgi_common.py`、`colab_cache.py`、`tunnel_client.py`），
可在本地单元测试；修改后运行 `python3 templates/build_notebooks.py` 重新生成，测试会校验生成结果与提交内容一致。

## License

本项目基于 [MIT License](https://github.com/deluxebear/colabllm/blob/master/LICENSE) 开源。
各运行时下载的第三方模型、镜像与软件遵循其各自的许可证与使用条款，请在使用前自行确认。
