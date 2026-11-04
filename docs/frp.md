# 项目全局 FRP 隧道

默认后端仍为 Cloudflare Tunnel。全局启用 FRP 后，所有服务及实例的下一次 `up` 自动使用 FRP，由指定服务器上的 Caddy 提供 HTTPS。配置不会切换或重启已经运行的服务。

## 配置与服务端安装

本机需要已登录的 `cf` CLI 和可用的 SSH 密钥/SSH config。服务端需要公网 IP、Linux amd64/arm64、Python 3、systemd、root 或免密 sudo。公网 TCP 7000 用于接收 Colab 连接，80/443 用于网站和 HTTPS 证书；安全组/防火墙需允许这些端口。

```bash
cf auth login

# 替换示例 IP 和域名；--ssh 支持 ~/.ssh/config 的别名、端口、密钥和跳板
colabllm frp-config \
  --server-ip 203.0.113.10 \
  --ssh root@frpip \
  --base-domain example.com

colabllm frp-config       # 查看全局配置，不展示 Token
colabllm frp-status       # SSH 检查 frps、Caddy 和路由
```

命令先验证 Cloudflare Zone 权限，再通过 SSH 检查监听进程、systemd 单元、配置路径和已安装二进制；不会仅因 7000 被占用就认定冲突。

- 已有 frps 正在运行：识别包括 `frpcolab-frps` 在内的自定义二进制/服务名称，读取原 Token、TLS 证书、端口限制和版本，兼容时直接复用，不重启或改写现有 frps。
- 已有 frps 已安装但停止：检查原配置后，启动原 systemd 单元。
- 仅安装了二进制：优先复用它创建项目专属配置/服务，不重复下载。
- 尚未安装：安装 frps 0.61.2、Caddy 2.10.2，校验官方发布的哈希，再生成配置和 systemd 服务。

Colab frpc 下载与服务端匹配的稳定版本（要求 >= 0.61.0）；既有证书使用 DNS SAN 时，即使通过 IP 连接，也使用证书中的 DNS 名称校验 TLS。服务端私钥和原 Token 都不会替换。

既有 frps 须使用 Token 认证、显式 TLS 证书和回环代理监听。自动复用不支持 OIDC 或要求客户端证书的 mTLS 配置；遇到这些配置会说明原因，不自动修改共享服务。7000 由其他程序占用时仍会明确报错。首次配置必须同时提供 IP、SSH 目标和主域名。

已有 Caddy 时，从监听进程或已安装 systemd 单元定位二进制和 Caddyfile，包括自定义路径和服务名。备份原文件，只追加项目站点目录的 import；保留文件权限和所有者，先校验再 reload，失败恢复原配置。已有 Caddy 二进制不会重复安装。JSON/API-only 配置或不明确的多实例布局会明确报错。新安装的 Caddy 使用独立的 `colabsvc-caddy.service`，不替换其他程序。

更改已部署服务器的 IP/监听端口需要显式迁移，命令不会直接重启共享 frps 并打断所有连接。

## 运行时使用

```bash
# 全局 FRP 已配置：自动走 FRP，域名为 indextts.example.com
colabllm indextts up

# 多实例保留原来的别名和实例后缀，例如 qwen-2.example.com
colabllm qwen-flash up -i 2

# 单次选择后端，或覆盖本次服务域名
colabllm longcat-avatar up --transport frp --domain avatar.example.com
colabllm longcat-avatar up --transport cloudflare
colabllm longcat-avatar up --quick-tunnel

# 关闭全局 FRP；主域名仍保留，因此之后默认是 Cloudflare 固定域名
colabllm frp-config --disable

# 如需恢复默认 Cloudflare 动态 URL，再清除主域名及环境变量覆盖
colabllm tunnel-config --clear
```

选择规则：`--quick-tunnel` 强制 Cloudflare 动态 URL；`--transport` 覆盖全局后端；否则启用 FRP 时用 FRP，未启用时用 Cloudflare。`--quick-tunnel` 与 `--transport frp` 不能同时使用。`--base-domain`、`--domain` 和 `COLABSVC_BASE_DOMAIN` 的主机名覆盖规则仍适用。

显式选择 FRP 时，必须已经通过 `frp-config` 完成服务器配置；配置不完整或连接失败不会自动降级到其他后端。

## 数据路径与认证

```text
客户端 -> HTTPS :443 / Caddy（FRP 服务器）
       -> 127.0.0.1:独立映射端口 / frps
       -> TLS 隧道 / Colab frpc
       -> Colab 原有认证入口 -> 模型服务
```

Colab frpc 主动连接服务器 7000；FRP 的映射端口固定分配在 20000–29999，仅监听服务器回环地址。分配由服务端加锁管理，使用项目 ID 和服务/实例名称区分，重启复用原端口。

本机通过 `cf` 自动创建或更新 A/AAAA 记录，默认 `proxied=false`（DNS only）。域名解析到 FRP 服务器，Caddy 自动申请/续期公开 HTTPS 证书，适配 WebSocket、SSE 和大文件。FRP 的 7000 连接直接使用服务器 IP。

DNS 更新仅接受该服务拥有的记录；其他用途的记录会导致冲突报错。Caddy 路由更新失败时回滚站点和本次 DNS 修改。

frpc 使用 Token 和固定信任证书验证服务器，服务器私钥不离开服务器。FRP 不是服务认证的替代：已有 Bearer、Basic Auth 和应用登录继续生效。Notebook 先检查认证层，再注册隧道；隧道单元验证公开 TLS 响应，服务原来的健康检查继续决定 `CT_READY`。大型模型加载过程中暂时出现 502 不会阻止后续加载。

frpc 加入现有 watchdog，退出后自动重启；网络断开时由 frpc 自动重连。所有 Notebook 共用同一套实现。

## 配置、状态与清理

- 全局主域名和 FRP 元数据：`~/.colabllm/tunnel-config.json`。
- 客户端 Token/信任证书：`~/.colabllm/frp/<服务器标识>.json`，权限 `0600`。
- 每个运行时的后端和独立资源记录：`~/.colabllm/services/<service>/state.json`，权限 `0600`。
- 服务端 frps 和专用 Caddy 配置：`/etc/colabsvc-frp/`。
- 路由注册表和证书数据：`/var/lib/colabsvc-frp/`。
- 新装二进制：`/usr/local/lib/colabsvc-frp/`。
- 已有标准 Caddy 的项目站点：`/etc/caddy/colabsvc-frp/`。
- Colab 客户端日志：`/content/ct/frpc.log`，配置文件 `/content/ct/frpc.toml` 权限 `0600`。

```bash
colabllm indextts down                   # 保留路由和 DNS，便于重启
colabllm indextts down --delete-tunnel   # 删除该实例的项目路由/DNS
```

清理根据实例保存的后端资源操作，不根据后来修改的全局配置猜测。Cloudflare 和 FRP 资源分别记录；只删除仍匹配所有权的 DNS 记录。更换该实例的 FRP 服务器或域名前，需先使用 `down --delete-tunnel` 清理旧路由。共享 frps/Caddy 不随单个实例停止而卸载。

`frp-status` 会显示实际复用的单元、版本和复用状态。例如已有服务可能是 `frpcolab-frps.service` 和 `caddy.service`，而非项目新建的服务名。

服务器诊断（按 `frp-status` 返回的实际单元调整）：

```bash
ssh root@frpip 'systemctl status colabsvc-frps'
ssh root@frpip 'journalctl -u colabsvc-frps -n 80 --no-pager'
# 新安装的 Caddy 使用 colabsvc-caddy；复用标准安装时使用 caddy
ssh root@frpip 'journalctl -u colabsvc-caddy -n 80 --no-pager'
```

## 验证

`tests/test_frp_backend.py` 验证配置、DNS 所有权、回滚、幂等安装和后端切换清理；`tests/test_frp_transport.py` 使用真实 frps/frpc/Caddy 在本机非特权端口验证 HTTPS、FRP TLS、认证、SSE、WebSocket、重连及错误凭据拒绝。

```bash
python tests/test_frp_backend.py
COLABSVC_FRP_TEST_BIN=/path/to/verified/binaries python tests/test_frp_transport.py
```

本地测试使用专用测试证书。另已在既有 frps 0.71.1 / Caddy 2.6.2 的服务器上完成原服务复用及真实 TLS 隧道请求往返验证，两个原进程 PID 保持不变；公网 DNS 和 ACME 签发仍需在具体运行时启动时验证。


## ComfyUI 页面长时间空白

先区分服务不可用与前端资源加载慢：经认证的 `/api/system_stats` 能返回，并不代表 JavaScript、工作流工具栏与 `/ws` 已加载成功。应检查浏览器 Network 中最慢的 `/assets/` 文件，再对比服务器本机 FRP 回源与公网下载耗时。

Caddy 2.10.2 及以上版本的 ComfyUI FRP 路由对 `/assets/` 下带八位内容哈希的 JS/CSS 设置浏览器私有长期缓存，仅限成功的 GET/HEAD 响应。首页、API、自定义扩展以及 401 响应不采用此缓存规则。升级前端后文件哈希变化，会自动下载新文件；首次访问仍取决于公网带宽。已有运行时需更新服务端站点配置后才生效。

部分旧版 Caddy/Go 组合在 reload 时可能出现 `context: internal error: missing cancel error` 并退出，systemd 停留在 reload 状态。共享后端会限制 reload 等待时间，且仅在 MainPID 已为 0 时 stop/start 恢复经过验证的配置；仍有主进程时不会自动强停。相同路由配置不会重复 reload。应保留配置、证书和服务单元，单独升级有问题的 Caddy 二进制。

排障建议：若页面加载明显慢于预期，可使用校验过发布哈希的较新 Caddy 版本（通过 systemd drop-in 覆盖执行程序，保留原二进制、站点与证书），并优先核对访问端网络（代理/网关分流规则、实际出口、TCP 重传），不要仅凭单个 HTTPS 文件下载慢推断服务器总带宽不足。若本地代理将项目域名整体设为直连而直连链路质量差，可为具体服务域名（例如 `comfyui.example.com`）添加优先级更高的代理规则；修改代理配置前保留备份并运行其自带的配置校验命令。
