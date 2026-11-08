<!-- Generated from docs/deepseek-qwen-9b.md; edit the source and run tools/sync_runtime_docs.py. -->

# DeepSeek-Qwen-9B：L4/T4 高吞吐 MTP 推测解码、256K 长上下文与多模态视觉服务

默认 **L4**（NVIDIA L4 `sm_89`，`23,034 MiB ≈ 22.5 GiB` VRAM），兼容 `--gpu T4`（`16 GB` VRAM）及 `A100` / `H100` / `G4`。通过 `llama-server`（`b11540`）原生 Bearer 认证部署 `xtools-at/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic` 的 GGUF 量化版（底座为 DeepSeek-V4-Pro Max Effect 25 万条数学/STEM 蒸馏模型，经 Heretic v2 ARA 低损去审查），默认加载 **imatrix 校准 4-bit 权重 `i1-Q4_K_M`（`5.24 GiB`）** + **原生 Multi-Token Prediction（`MTP`）草稿侧车 `mtp-Q8_0.gguf`（`2.27 GiB`，`--spec-type draft-mtp` 无损推测解码加速）** + 多模态视觉投影器 **`mmproj-f16.gguf`（`0.86 GiB`）**，并在 L4 上开启满血原生 **`262,144`（256K）** 上下文窗口。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## L4 / T4 混合架构 + MTP 推测解码显存规划

`Qwen3.5-9B`（`qwen35` 架构）包含 `32` 层主干混合模块 + `1` 层原生 `nextn` 多词预测（MTP）头：**`24` 层 Gated DeltaNet 线性注意力（状态空间仅占固定 `~52 MiB`，不随上下文增长）+ `8` 层主干全注意力 + `1` 层 MTP 全注意力**（`4` KV heads，`head_dim=256`）：

- **超低显存原生 256K 上下文（主干 + MTP 共 `4.83 GiB` KV）**：`-ctk q8_0 -ctv q8_0 -ctkd q8_0 -ctvd q8_0 -fa on` 在 `262,144`（256K）满窗口下仅占 **`4,948 MiB`（`4.83 GiB`）** 显存（若加 `--no-mtp` 则为 `4.30 GiB`）。
- **L4 默认极速档（`--quant auto` -> `i1-Q4_K_M` + 原生 `MTP`）**：`i1-Q4_K_M`（`5,369 MiB`）+ `mmproj-f16`（`876 MiB`）+ `mtp-Q8_0`（`2,320 MiB`）+ `256K` `q8_0` KV（`4,948 MiB`）实测常驻显存约 **`14,968 / 23,034 MiB`**（仍预留约 **`7.9 GiB`** 显存余量）；通过 `i1-Q4_K_M` 减半访存体积叠加 `--spec-type draft-mtp` 多词并行验证，在 L4 上实测数学推理/代码/视觉解码吞吐达 **`57–68 tok/s`**（MTP 接受率 `78%–93%`，相较单 `Q8_0` 的 `27.3 tok/s` 提速 **`2.1x–2.5x`**），且保持输出逐 token 分布无损。
- **L4 高精度可选档（`--quant Q8_0`）**：`Q8_0`（`9,087 MiB`）+ `mmproj-f16` + `mtp-Q8_0` + `256K` `q8_0` KV 共 `17.23 GiB`，同样可在 L4 上全驻显存运行。
- **T4 自动适配档（`--gpu T4`）**：`i1-Q4_K_M` + `mmproj-f16` + `mtp-Q8_0` 自动将上下文对齐至 **`212,992`（208K）**；若加 `--no-mtp` 则在 T4 上也可直开完整 **256K** 上下文。

可在本地离线预览各 GPU 档位或自定义量化/MTP 的显存规划：

```bash
colabllm deepseek-qwen-9b plan
colabllm deepseek-qwen-9b plan --preset L4
colabllm deepseek-qwen-9b plan --preset L4 --quant Q8_0
colabllm deepseek-qwen-9b plan --preset T4
colabllm deepseek-qwen-9b plan --preset T4 --no-mtp
```

## 启动与凭据

```bash
# 默认 L4 + i1-Q4_K_M + 原生 MTP 推测解码 + 256K (262,144) 上下文 + q8_0 KV + F16 视觉
colabllm deepseek-qwen-9b up --ready-timeout 2400

# 绑定项目固定子域名（默认别名 deepseek-9b.<base-domain>）
colabllm deepseek-qwen-9b up --domain

# 切换 8-bit Q8_0 量化（仍同时启用原生 MTP + 256K 上下文）
colabllm deepseek-qwen-9b up --domain --quant Q8_0

# 低配额 T4 模式（i1-Q4_K_M + MTP 自动 208K 上下文；加 --no-mtp 可开满 256K）
colabllm deepseek-qwen-9b up --gpu T4 --domain

# 自定义 MTP 草稿步长或通过 YaRN 扩展至 512K 上下文
colabllm deepseek-qwen-9b up --domain --mtp-draft 4
colabllm deepseek-qwen-9b up --domain --context 512k

# 查询运行状态、实际分配的 n_ctx (262144) 与认证密钥
colabllm deepseek-qwen-9b status --json
colabllm deepseek-qwen-9b models
colabllm deepseek-qwen-9b info --json
colabllm deepseek-qwen-9b info --json --show-secrets
colabllm deepseek-qwen-9b logs -n 100
```

API Base URL 为返回的 `url` 后加 `/v1`；不要把网页地址误当成模型 ID。
`colabllm deepseek-qwen-9b models`（或 `GET /v1/models`）返回实际 alias（默认 `deepseek-v4-pro-qwen3.5-9b`）、`context_allocated` 与 `modalities.vision`。`/health` 可无需认证返回 200，校验 API Key 应使用 `/v1/models`：无密钥/错误密钥须返回 `401`。

## 调用聊天、推理与视觉 API

下例使用 Python 标准库，不安装 OpenAI SDK，不显示密钥，并拒绝重定向：

```python
import json
import subprocess
import urllib.request

info = json.loads(subprocess.check_output(
    ["colabllm", "deepseek-qwen-9b", "info", "--json", "--show-secrets"], text=True))
base = info["url"].rstrip("/")
headers = {"Authorization": "Bearer " + info["api_token"], "Content-Type": "application/json"}
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
opener = urllib.request.build_opener(NoRedirect())
with opener.open(urllib.request.Request(base + "/v1/models", headers=headers), timeout=30) as response:
    model = json.load(response)["data"][0]["id"]
payload = {"model": model, "messages": [{"role": "user", "content": "求 x^3 - 6x^2 + 11x - 6 = 0 的全部实根。"}],
           "max_tokens": 512, "stream": False,
           "chat_template_kwargs": {"enable_thinking": True}}
with opener.open(urllib.request.Request(base + "/v1/chat/completions", headers=headers,
        data=json.dumps(payload).encode()), timeout=120) as response:
    msg = json.load(response)["choices"][0]["message"]
    print(msg.get("content") or msg.get("reasoning_content"))
```

服务端默认 `--reasoning on`；客户端可在单次请求中通过 `chat_template_kwargs: {"enable_thinking": false}` 切换低延迟直出，或设为 `true` 开启深度思考链（思考过程位于 `message.reasoning_content`）。

视觉输入将消息 `content` 改成内容块列表：

```json
[{"type":"text","text":"解析图中的公式与图表含义"},{"type":"image_url","image_url":{"url":"data:image/png;base64,IMAGE_BASE64"}}]
```

图片使用服务支持的可访问 URL 或真实 base64 数据；本机文件路径不能直接传给远端模型。
`IMAGE_BASE64` 为占位值。服务没有 `chat` CLI 子命令，请调用 API。
流式调用使用 `stream: true` 并处理 SSE，不能把流式返回一次性当 JSON 解析。

## 多实例与存储

```bash
colabllm deepseek-qwen-9b up -i 2
colabllm deepseek-qwen-9b info -i 2 --json
colabllm deepseek-qwen-9b ps --json
colabllm deepseek-qwen-9b down -i 2
```

调用实例 2 时，示例 Python 的 `info` 参数同样加 `-i 2`。
模型权重（默认 `i1-Q4_K_M` `5.63 GB`）、`mmproj-f16.gguf`（`0.92 GB`）与 `mtp-Q8_0.gguf`（约 `2.4 GB`）暂存在本地高速 NVMe SSD `/content/deepseek-qwen-9b/model`（开启 `HF_XET_CHUNK_CACHE_SIZE_BYTES=0` 防双倍占盘）；首次远程 HTTP Range 导出的 `mtp-Q8_0.gguf` 与按 GPU 架构（L4 `sm_89` / T4 `sm_75`）编译的 `llama.cpp` 运行时均缓存在 Drive（`/content/drive/MyDrive/models/`），后续启动数秒即可恢复；停止 VM 会释放本地 SSD 文件。
API 不自动把聊天记录或响应存入 Drive，客户端需自行保存。

更新指定 Key 时须同时更新私密状态和实际服务端认证；只编辑本机状态不会改变运行中 server。
单纯换 Key 不必释放 GPU，但应先检查在途请求，再按服务重载方式操作并验证新旧 Key。
