# DeepSeek Coder：DeepSeek-V4.1-Flash 编程与视觉推理（硬件自适应最大上下文）

默认 **G4**（RTX PRO 6000 Blackwell，96 GB VRAM），兼容 **H100**（80 GB HBM3）与 **A100**（80 GB / 40 GB）；通过 `llama-server` 原生 Bearer 认证部署 `Abiray/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-GGUF`（50% 路由专家剪枝 `192` 专家/层 `IQ2_XXS` 主干 72.1 GB + `IQ4_NL` Engram 条件记忆表 110.6 GB + `BF16` `deepseek41v` 视觉投影器 0.97 GB），并在启动时根据实际 GPU 显存与系统内存自动设置最大安全上下文窗口（`G4` 默认满血 **1,048,576（1M）** 上下文 + **103 GiB Engram 全量常驻主机 DDR5 内存** + **默认关闭思考直出**）。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 硬件档位与自动上下文窗口

默认 `--context auto` 会实时探测 Colab VM 的显存与系统内存，结合 `deepseek41` 跨层共享压缩 KV Cache（`f16` 仅约 `3.125 KiB/token`，1M 上下文 KV 约 `3.125 GiB`）与图工作区预算自动对齐最大窗口，并在遇到冷启动显存峰值不足时按阶梯自动回退：

| GPU 规格 | 权重与 Engram 放置 | 默认自动窗口 (`--context auto`) | 说明 |
| :--- | :--- | :--- | :--- |
| **G4（默认，96 GB VRAM / ~176 GiB RAM）** | `full-gpu` + `engram_in_ram=true` | **`1,048,576`（满血 1M）** | 73.1 GB 主干+视觉全驻显存，103 GiB Engram 全驻主机 DDR5 内存（零云盘缺页中断），`-b 4096 -ub 512` 高速 Prefill |
| **H100 / A100（80 GB VRAM / ~167 GiB RAM）** | `full-gpu` + `engram_in_ram=true` | **`~638,976`（~624K）** | 全 GPU + Engram 常驻内存；显式传 `--context 1m` 时自动卸载 3 层 MoE 专家到内存以容纳 1M 窗口 |
| **A100（40 GB VRAM / ~83.5 GiB RAM）** | `hybrid-cpu-moe` + `mmap/lazy` | **`~327,680`（320K）** | 注意力与共享专家常驻 GPU，路由专家卸载至系统内存，Engram 自动退回本地 SSD 按需分页读取 |

本地离线预览各规格或自定义显存的规划结果：

```bash
colabllm deepseek-coder plan
colabllm deepseek-coder plan --preset G4
colabllm deepseek-coder plan --preset H100 --context 1m
colabllm deepseek-coder plan --vram-gb 80 --ram-gb 167
```

## 启动与凭据

```bash
# 默认 G4 + 自动满血 1M 上下文 + 默认关闭思考（--reasoning off）
colabllm deepseek-coder up --ready-timeout 5400

# G4 + 绑定项目固定子域名（默认别名 deepseek.<base-domain>）
colabllm deepseek-coder up --domain

# H100 (80 GB) 自动最大全 GPU 窗口或指定 1M 窗口（自动卸载少量 MoE 层）
colabllm deepseek-coder up --gpu H100 --domain
colabllm deepseek-coder up --gpu H100 --domain --context 1m

# 若希望全局默认开启思考，可显式指定 --reasoning on 与强度（minimal|low|medium|high|xhigh|max 或 1..100）
colabllm deepseek-coder up --domain --reasoning on --reasoning-effort medium

# 查询运行状态、实际分配的 n_ctx 与认证密钥
colabllm deepseek-coder status --json
colabllm deepseek-coder models
colabllm deepseek-coder info --json --show-secrets
colabllm deepseek-coder logs -n 100
```

首次启动需并发编译对应 GPU 架构的 `llama-server`（自动归档缓存至 `MyDrive/colab-llm/deepseek-coder/runtime-cache/`）并将约 183.7 GB 模型分片下载至 Colab 本地 NVMe SSD（`/content/deepseek-coder/model`，自动建立第 6 分片 `Engram` 软链接并开启 `HF_XET_CHUNK_CACHE_SIZE_BYTES=0` 防双倍占盘），建议保留 `--ready-timeout 5400`。

API Base URL 为返回的 `url` 后加 `/v1`；`colabllm deepseek-coder models` 会同时校验鉴权并返回服务端实际生效的模型 alias、`context_allocated`（`n_ctx`）和 `vision` 模态状态。`/health` 可无需认证返回 200，验证 API Key 请访问 `/v1/models`。

## 调用代码与多模态 API（兼容 Claude Code / OpenAI 客户端）

下例使用 Python 标准库调用 `/v1/chat/completions`，不安装第三方 SDK，不打印密钥并拒绝重定向：

```python
import json
import subprocess
import urllib.request

info = json.loads(subprocess.check_output(
    ["colabllm", "deepseek-coder", "info", "--json", "--show-secrets"], text=True))
base = info["url"].rstrip("/")
headers = {"Authorization": "Bearer " + info["api_token"], "Content-Type": "application/json"}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect())
with opener.open(urllib.request.Request(base + "/v1/models", headers=headers), timeout=30) as response:
    model = json.load(response)["data"][0]["id"]

payload = {
    "model": model,
    "messages": [{"role": "user", "content": "用 Python 实现带超时控制的异步 TCP 端口连通性探测函数。"}],
    "temperature": 0.2,
    "max_tokens": 2048,
    "stream": False,
}
with opener.open(urllib.request.Request(
        base + "/v1/chat/completions", headers=headers, data=json.dumps(payload).encode()), timeout=180) as response:
    msg = json.load(response)["choices"][0]["message"]
    print(msg.get("content") or msg.get("reasoning_content"))
```

服务默认关闭思考（`--reasoning off --reasoning-format deepseek`）以降低 Coding Agent 高频工具调用延迟；Agent 或客户端可在**任意单次请求**中通过 `/v1/chat/completions` 请求体动态开启思考、调节思考强度（`minimal`/`low`/`medium`/`high`/`xhigh`/`max` 或整数 `1..100`，默认校准值 `50`）或设置思考 Token 硬上限（思考内容自动分离至 `message.reasoning_content`）：

```json
{
  "chat_template_kwargs": {
    "enable_thinking": true,
    "reasoning_effort": "medium"
  },
  "reasoning_budget_tokens": 1024
}
```

多模态视觉输入（如截图转代码、架构图解析）将消息 `content` 设为内容块列表：

```json
[{"type": "text", "text": "根据界面截图编写对应的 HTML 与 Tailwind CSS"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,IMAGE_BASE64"}}]
```

`IMAGE_BASE64` 为真实图像的 base64 占位符；远端服务无法直接读取本机磁盘路径。若无需视觉能力、希望额外腾出约 1 GB 显存，可在启动时传入 `--no-vision`。

## 多实例、存储限制与停止

```bash
colabllm deepseek-coder up -i 2 --domain
colabllm deepseek-coder models -i 2
colabllm deepseek-coder ps --json
colabllm deepseek-coder down -i 2
```

- **主机内存常驻 Engram 与 SSD 自动回退**：在系统内存充足的机型（如 `G4` ~176 GiB RAM、`H100` ~167 GiB RAM）上，110.6 GB 的 `DeepSeek-V4.1-Flash-Engram-IQ4_NL.gguf` 通过 `--load-mode none --lazy-mode off --override-tensor per_layer_token_embd.weight=CPU` 全量读入主机 DDR5 内存并释放重复的文件 Page Cache，彻底消除云盘随机缺页延迟；在较小内存机型（如 `A100-40G`）上自动切换为 `--load-mode mmap --lazy-mode on` 按需分页读取。切勿将工作目录改指 `/content/drive`。
- **低显存机型限制**：`T4`（16 GB）与常规 `L4`（24 GB VRAM + ~53 GB RAM）总内存不足以容纳 72.1 GB 主干权重，请使用 `G4`、`H100` 或 `A100`。
- **持久性**：编译好的 `llama-server` 归档与校验状态持久保存在 `MyDrive/colab-llm/deepseek-coder/`；执行 `down` 会释放 Colab VM 及本地 SSD 模型副本，聊天请求内容不会自动写入 Drive。
