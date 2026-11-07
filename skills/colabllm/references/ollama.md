<!-- Generated from docs/ollama.md; edit the source and run tools/sync_runtime_docs.py. -->

# Ollama：模型自动拉取、兼容接口与推理调优

默认 **T4**（可按模型规模通过 `--gpu L4|A100|G4` 升级），前置 Caddy 认证层（浏览器 Basic Auth、`Authorization: Bearer <token>` 与 Anthropic `x-api-key: <token>`），`ollama serve` 仅监听远端回环 `127.0.0.1:11434`。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。部署与环境变量参考 [Ollama 官方文档](https://docs.ollama.com/)。以下命令均在本机执行。

## 1. 指定模型启动（自动下载、别名与显存预热）

```bash
# 默认拉取并预热 qwen2.5:0.5b，开启全部兼容接口（ollama + openai + anthropic）
colabllm ollama up

# 指定单个或多个模型（自动拉取权重，并将首个模型预热加载至 GPU 显存）
colabllm ollama up --models qwen3:8b,nomic-embed-text

# 直接拉取 Hugging Face GGUF 模型，或将全部指定模型预热进显存
colabllm ollama up --gpu L4 --model hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M --preload-all

# 为 OpenAI / Claude Code 工具创建模型别名（自动下载源模型并执行 ollama cp）
colabllm ollama up --gpu L4 --model qwen2.5-coder:7b --alias claude-3-5-sonnet=qwen2.5-coder:7b,gpt-4o=qwen2.5-coder:7b

# L4 (24GB) 或 G4/A100 运行更大的 Qwen3-Coder 30B MoE（Ollama 官方标签为 qwen3-coder:30b）
colabllm ollama up --gpu L4 --model qwen3-coder:30b --context-length 32768 --kv-cache-type q8_0 --keep-alive -1

# 仅启动服务、暂不预下载模型（后续通过 pull 或 run 按需自动拉取）
colabllm ollama up --no-models
```

- `--models` / `--model` / `-m`：可重复或用逗号分隔；默认在 `CT_READY` 前流式完成拉取并自动发送空请求预热主模型至 GPU 显存（可用 `--no-preload` 跳过显存预热，或 `--preload-all` 预热全部启动模型）。
- `--models-storage ssd|drive`：默认 `ssd`（`/content/ollama/models`，NVMe 本地高速盘，释放 VM 后需重拉）；指定 `drive` 可将模型缓存保存在 Google Drive（`<DATA_DIR>/models`）以跨会话复用。
- `--ollama-version 0.17.7`：可按需固定安装的 Ollama 版本。

### 一键模型预设（`--preset`）

预设一次性指定 GPU、模型、别名和服务端调优参数；命令行显式传入的参数优先于预设值。

```bash
# Huihui Qwen3.8-27B abliterated：BF16 满血权重 + 原生 262K 上下文 + 视觉/工具/思考，默认 G4
colabllm ollama up --preset huihui-27b --domain
```

| 预设 | GPU | 模型 | 别名 | 服务端参数 |
| :--- | :--- | :--- | :--- | :--- |
| `huihui-27b` | G4 | `huihui_ai/Qwen3.8-abliterated:27b-bf16`（约 56 GB） | `huihui-qwen3.8-27b` | 上下文 262144、Flash Attention、f16 KV、单并发、常驻显存 |

客户端模型名可使用别名 `huihui-qwen3.8-27b`。需要更省资源时可覆盖预设，例如 L4 上使用 Q4 量化与 64K 上下文：

```bash
colabllm ollama up --preset huihui-27b --gpu L4 -m huihui_ai/Qwen3.8-abliterated:27b-q4_K_M --context-length 65536 --kv-cache-type q8_0
```

通过 `-m` 覆盖模型时不会创建预设别名。Ollama 默认上下文仅 4096，超出部分会被静默截断；不使用预设时请显式设置 `--context-length`。

## 2. 兼容接口选择（支持全开或按需限定）

Ollama 原生同时提供三套接口；默认 `--compat all` **全部开启**，也可在启动时按需限定对外暴露的接口族：

```bash
# 默认全开（Native Ollama /api/* + OpenAI /v1/* + Anthropic /v1/messages）
colabllm ollama up --compat all --models qwen3:8b

# 仅对外暴露 OpenAI 与 Anthropic 兼容接口（屏蔽 /api/delete、/api/pull 等管理端）
colabllm ollama up --compat openai,anthropic --models qwen3:8b

# 全开接口但设为只读模型模式（禁止公网调用 /api/pull、/api/delete、/api/create、/api/copy、/api/push）
colabllm ollama up --compat all --read-only-models --models qwen3:8b

# 查看当前实例启用的兼容接口、Base URL 与路由列表（默认隐藏密钥）
colabllm ollama endpoints
colabllm ollama endpoints --json
```

各兼容接口的客户端配置方式（同一 `api_token` 通用）：

| 接口类型 | `--compat` 值 | Base URL | 认证头 | 主要覆盖路由 |
| --- | --- | --- | --- | --- |
| **Ollama 原生** | `ollama` | `<URL>` | `Authorization: Bearer <TOKEN>` | `/api/chat`, `/api/generate`, `/api/embed`, `/api/tags`, `/api/ps` |
| **OpenAI 兼容** | `openai` | `<URL>/v1` | `Authorization: Bearer <TOKEN>` | `/v1/chat/completions`, `/v1/responses`, `/v1/completions`, `/v1/embeddings`, `/v1/models`, `/v1/audio/*` |
| **Anthropic 兼容** | `anthropic` | `<URL>` | `x-api-key: <TOKEN>` 或 `Authorization: Bearer <TOKEN>` | `/v1/messages`, `/v1/models`（适配 Anthropic SDK 与 Claude Code） |

Python SDK 调用示例（OpenAI Chat / Responses 与 Anthropic Messages）：

```python
import os
from openai import OpenAI
import anthropic

url = os.environ["OLLAMA_PUBLIC_URL"]
token = os.environ["OLLAMA_API_TOKEN"]

# 1. OpenAI SDK（支持 Chat Completions、Responses API 与 Embeddings）
oai = OpenAI(base_url=f"{url}/v1", api_key=token)
chat = oai.chat.completions.create(
    model="qwen3:8b",
    messages=[{"role": "user", "content": "用一句话介绍你自己。"}],
)
resp = oai.responses.create(model="qwen3:8b", input="Write one haiku.")

# 2. Anthropic SDK / Claude Code（Caddy 原生支持 x-api-key 与 Bearer）
claude = anthropic.Anthropic(base_url=url, api_key=token)
msg = claude.messages.create(
    model="qwen3:8b",
    max_tokens=512,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

## 3. 启动优化参数调配（`OLLAMA_*`）

支持预设档位 `--profile`（`balanced`、`low-vram`、`coding`、`agent`、`throughput`）以及细粒度参数覆盖：

```bash
# 低显存高上下文调优（适合 16GB T4：开启 Flash Attention + q8_0 KV Cache + 单并发）
colabllm ollama up --models qwen3:8b --profile low-vram --context-length 16384

# Coding / Agent 长上下文预设（64K 上下文 + q8_0 KV Cache + 常驻显存 keep_alive=-1）
colabllm ollama up --gpu L4 --models qwen2.5-coder:7b --profile coding

# 细粒度手动调配服务端参数
colabllm ollama up --gpu L4 \
  --models qwen3:8b,nomic-embed-text \
  --context-length 32768 \
  --flash-attention \
  --kv-cache-type q8_0 \
  --num-parallel 2 \
  --max-loaded-models 2 \
  --max-queue 512 \
  --keep-alive -1 \
  --load-timeout 15m \
  --gpu-overhead 512M \
  --max-transfer-streams 8 \
  --no-cloud \
  --ollama-env OLLAMA_NOPRUNE=1
```

常用调优参数对照：

- `--context-length` (`OLLAMA_CONTEXT_LENGTH`)：默认上下文窗口大小（Tokens）；Agent / Coding 场景建议 `>= 65536` 并配合较大显存 GPU。
- `--flash-attention` / `--no-flash-attention` (`OLLAMA_FLASH_ATTENTION`)：显式开关 Flash Attention，长上下文显著降低显存占用。
- `--kv-cache-type f16|q8_0|q4_0` (`OLLAMA_KV_CACHE_TYPE`)：KV Cache 量化精度；选择 `q8_0` 约省一半 KV 显存且几乎无损（未显式指定时自动开启 Flash Attention）。
- `--num-parallel` (`OLLAMA_NUM_PARALLEL`)：单模型并发处理请求数（总 KV 显存随 `num_parallel * context_length` 增长；单用户省显存设为 `1`）。
- `--max-loaded-models` (`OLLAMA_MAX_LOADED_MODELS`)：同时驻留显存的模型上限（例如聊天模型 + Embedding 模型同时驻留可设为 `2`）。
- `--keep-alive` (`OLLAMA_KEEP_ALIVE`)：模型空闲驻留时长（默认 `30m`，`-1` 常驻不卸载，`0` 响应后立即释放显存）。
- `--load-timeout` (`OLLAMA_LOAD_TIMEOUT`)、`--gpu-overhead` (`OLLAMA_GPU_OVERHEAD`)、`--sched-spread`、`--origins` (`OLLAMA_ORIGINS`)、`--no-cloud` (`OLLAMA_NO_CLOUD=1`)、`--ollama-env KEY=VALUE`（安全透传其余 `OLLAMA_*` 变量，禁止覆盖回环绑定 `OLLAMA_HOST`）。

## 4. 运行中模型管理与快速推理（自动按需下载）

实例就绪后，无需重启即可通过 CLI 管理模型或直接验证三套兼容接口：

```bash
# 查看已下载模型与当前 GPU 显存驻留状态（/api/tags + /api/ps）
colabllm ollama models
colabllm ollama models --json

# 在运行实例（或 -A 全部实例）上在线拉取新模型，可选创建别名并预热至显存
colabllm ollama pull qwen2.5:7b --alias gpt-4o-mini --preload

# 指定模型直接运行提示词：若模型尚未下载会自动先拉取（--no-auto-pull 可关闭自动下载）
colabllm ollama run qwen3:8b "用一句话解释KV Cache量化"
colabllm ollama run qwen3:8b "Say hello" --api openai
colabllm ollama run qwen3:8b "Say hello" --api anthropic

# 显存预热 / 立即卸载释放显存 / 复制别名 / 查看详情 / 删除模型
colabllm ollama preload qwen2.5:7b --keep-alive -1
colabllm ollama unload qwen2.5:7b
colabllm ollama cp qwen2.5:7b my-assistant
colabllm ollama show qwen2.5:7b --json
colabllm ollama rm my-assistant
```

若启用了 `--compat openai` 等裁剪策略屏蔽了 `/api/*`，`models` 会自动回退至 `/v1/models` 查询列表，而依赖 `/api/*` 的远程管理子命令（`pull`、`cp`、`rm`、`preload`）将按策略返回 403。

## 5. 多实例与停止

```bash
colabllm ollama up -i 2 --models qwen2.5:0.5b
colabllm ollama ps --json
colabllm ollama status --json
colabllm ollama down -i 2
```
