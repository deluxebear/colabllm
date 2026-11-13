<!-- Generated from docs/runtime-common.md; edit the source and run tools/sync_runtime_docs.py. -->

# 运行时通用操作

所有运行时均通过已安装的 `colabllm` 使用；不必切换到源码目录。命令以当前版本 `--help` 为准。
本机只负责控制、文件传输和保活，不需要安装 Torch、CUDA 或 NumPy。

## 首次准备与启动

```bash
colabllm --version
colabllm services
colabllm doctor
colabllm tunnel-config
```

`colab` CLI、独立的 `colab dfs` 扩展和 Google/Drive 授权需要另行准备。
`doctor` 默认离线检查；`doctor --online --timeout 10` 增加限时只读授权检查。
固定域名需要 Cloudflare 授权；FRP 还需要用户配置的服务器及 SSH 认证。
安装包和前置依赖的完整说明以发行版 README 为准。包内 Agent 技能可用 `colabllm skill --output <技能父目录>` 导出。

先查看目标运行时的 `ps --json` / `status --json`，复用现有就绪实例。`up` 可能新建付费 GPU 会话。
默认启动读取全局配置：启用 FRP 则使用 FRP，否则配置域名时使用 Cloudflare named tunnel，未配置时使用动态 Quick Tunnel。
`up --quick-tunnel` 强制动态地址；`up --base-domain example.com` 单次指定后缀；`up --domain service.example.com` 指定完整域名。
域名示例不是部署地址，实际 URL 以 `info --json` 为准。一次启动的三个域名选项互斥。

## 生命周期、超时与实例

每个运行时指南给出了具体服务名。共享命令为 `up/down/info/ps/list/status/logs/tunnel-config`，服务专用命令见各自指南。

- `up` 等到完整的 `CT_READY` 协议行；`up --detach` 仅提交后台启动，需要继续检查 `status --json`。
- `up --auto-restart` 开启守护模式（可配合 `--restart-delay SECONDS`、`--idle-check SECONDS` 与 `--max-restarts N`）：当 `colab exec` 断开或默认 `300s`（5 分钟）无心跳输出后核实远端会话已不存在时，若远端 VM 仍存活则直接重连挂载，若已被回收则自动清理残留记录并重新 `colab new` 申卡挂载恢复，固定域名与 `api_token` 保持不变；手动 `down` 时正常退出不重拉。
- `up --ready-timeout SECONDS` 限制就绪等待。默认后台模式超时保留 runner；前台 `--foreground` 超时断开连接。
- `--command-timeout SECONDS` 或 `COLABLLM_COMMAND_TIMEOUT` 限制外部控制命令；长驻保活连接不受该通用上限影响。
- `status --json` 返回运行、就绪、URL 和探测信息；`running: true` 只代表本地 runner 活着，不能替代模型就绪检查。
- `logs -n 100` 查看近期日志，`logs -f` 跟随输出；日志可能含第三方程序打印的敏感信息，分享前应脱敏。
- `-i ID` 可用于启动及后续调用。实例 ID 为 1–64 位字母、数字、下划线或连字符，不以连字符开头；默认实例不传 `-i`。
- `ps --json` 列出该服务的各实例。独立实例使用独立 VM、隧道和凭据，但相同服务的 Drive 数据目录可能共享。
- `down -i ID` 释放该实例 VM；`down --all` 停止该服务所有实例；`--delete-tunnel` 还删除该实例拥有的隧道/DNS。

本机必须持续联网且避免休眠，后台 runner 保持 `colab exec` 连接；断开后 VM 可能被回收。
不要因客户端等待超时就重复 `up`。先查日志和会话，避免重复模型加载或提交任务。
`down` 失败时保留跟踪信息，排除错误后重试；升级安装包前先在合适的空闲时间停止服务。

## 认证、路径和输出

`info/ps/status` 默认隐藏凭据；`info --json --show-secrets` 才返回 `api_token` 等字段。
使用 Bearer Token 调 API；Caddy 服务的网页可使用 `auth_user/auth_pass`，Unsloth 使用应用自己的登录字段。
查询密钥只为客户端捕获，不将其写入源码、命令示例或共享日志。FRP Token 不是模型 API Key。
切换到不同来源的 `--url` / URL 环境变量时，须显式提供匹配 Token；协议、主机、有效端口共同决定来源。
当前没有公共命令设置任意持久 API Key；`up --new-credentials` 对停止的实例生成随机新凭据，不能接受指定值。

本机状态在 `~/.colabllm/services/<服务[-实例]>/`，可用 `COLABLLM_HOME` 选择隔离配置。
全局配置在用户目录的 `tunnel-config.json`；CLI 的外部授权仍由 Colab/Cloudflare/SSH 管理。
本机相对输入输出路径基于当前目录；`drive://...` 表示远端 MyDrive 路径，不接受 Drive 分享网页或文件 ID 代替路径。
Drive 数据通常在 `MyDrive/colab-llm/<服务>/`；各服务具体存储以指南和返回结果为准。

JSON 结果写 stdout，诊断写 stderr。成功退出 0、参数用法错误 2、等待/命令超时 124、Ctrl+C 130；底层操作失败为非零。
TTS 和转写的 `--timeout` 是请求限时；OCR/Avatar/Openjutsu/MiniMax-H3-Image/ComfyUI 的等待限时不会取消已提交的后台任务。

## 排错顺序

依赖或授权错误先查 `doctor`。502 可能来自加载中、隧道断开或 VM 回收，不能只凭状态码决定重启。
查 `logs`、`status --json`、`colab sessions`；最后一个 notebook cell 阻塞是保活设计，新的 `colab exec` 可能排队。
DNS 归属冲突先核对 `tunnel-config` 与 `frp-status` 的项目 ID/服务路由；不能覆盖不明归属的记录。
停止或重置前先确认没有正在生成、训练或转写的任务，并保留任务 ID、输出和恢复信息。
