# Qwen Flash：Qwen3.8-Flash-Next 1M 长上下文与多模态视觉推理

默认 **G4**（RTX PRO 6000 Blackwell，`97,887 MiB ≈ 95.6 GiB` VRAM，`~176 GiB` DDR5 RAM），通过 `llama-server`（`b11540`）原生 Bearer 认证部署 `huihui-ai/Huihui-Qwen3.8-Flash-Next-abliterated-v2-GGUF`（默认 `unsloth-UD-Q4_K_XL-V2` 校准 4-bit MoE 量化，磁盘共 4 分片 `103.69 GiB` + `mmproj-Qwen3.8-Flash-Next-BF16.gguf` 视觉投影器 `0.85 GiB`），默认开启满血 **1,048,576（1M）** 上下文窗口（YaRN `4.0x` + `q8_0` 压缩 KV/QSA Indexer Cache）与多模态视觉输入。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## G4 显存/内存规划与档位预览

`Qwen3.8-Flash-Next`（`qwen4exp` 架构）包含 `48` 层混合模块（`12` 层 QSA 全注意力 + `36` 层线性注意力 SSM）以及一张 `26.82 GiB` 的 3-gram PLE 词表（`per_layer_token_embd.weight`）：

- **PLE 查表常驻 DDR5 主机内存（0 MiB 显存）**：通过 `--override-tensor per_layer_token_embd.weight=CPU --load-mode none --lazy-mode off --no-host` 将 `26.82 GiB` PLE 表全量加载到主机 DDR5 内存并释放重复的 Linux Page Cache，零磁盘缺页延迟且不占用 GPU 显存。
- **1M 上下文 `q8_0` KV + QSA k-pool Indexer 缓存（`16.05 GiB`）**：`-ctk q8_0 -ctv q8_0 -fa on` 将 1M 窗口下的 `12` 层 QSA KV 与 MLA 索引器缓存从 `f16` 的 `30.1 GiB` 压缩至 `16.05 GiB`。
- **自适应专家层分流（`--cpu-moe auto`）**：在 `G4`（`95.6 GiB` VRAM）上，`UD-Q4_K_XL-V2` 的 GPU 主干 + BF16 视觉共 `77.7 GiB`；自动卸载仅 `3/48` 层路由专家（`4.39 GiB`）至 DDR5 内存后，**`45/48` 层（93.75%）+ 全部注意力/SSM/共享专家 + 视觉投影器常驻 GPU（`73.3 GiB`）**，叠加 `16.05 GiB` 1M KV 后仍预留约 **`6.2 GiB`** 安全显存余量。

可在本地离线预览不同 GPU 或量化档位的显存/内存规划：

```bash
colabllm qwen-flash plan
colabllm qwen-flash plan --preset G4
colabllm qwen-flash plan --preset G4 --context 512k
colabllm qwen-flash plan --preset G4 --quant unsloth-UD-IQ4_XS
```

## 启动与凭据

```bash
# 默认 G4 + UD-Q4_K_XL-V2 + 1M (1,048,576) 上下文 + q8_0 KV + BF16 视觉
colabllm qwen-flash up --ready-timeout 5400

# 绑定项目固定子域名（默认别名 qwen.<base-domain>）
colabllm qwen-flash up --domain

# 若希望 0 层 CPU 卸载（48/48 层 100% 纯 GPU），可选 512K 上下文或 UD-IQ4_XS 量化
colabllm qwen-flash up --domain --context 512k
colabllm qwen-flash up --domain --quant unsloth-UD-IQ4_XS

# 查询运行状态、实际分配的 n_ctx (1048576) 与认证密钥
colabllm qwen-flash status --json
colabllm qwen-flash models
colabllm qwen-flash info --json
colabllm qwen-flash info --json --show-secrets
colabllm qwen-flash logs -n 100
```

API Base URL 为返回的 `url` 后加 `/v1`；不要把网页地址误当成模型 ID。
`colabllm qwen-flash models`（或 `GET /v1/models`）返回实际 alias、`context_allocated` 与 `modalities.vision`。`/health` 可无需认证返回 200，校验 API Key 应使用 `/v1/models`：无密钥/错误密钥须返回 `401`。

## 调用聊天与视觉 API

下例使用 Python 标准库，不安装 OpenAI SDK，不显示密钥，并拒绝重定向：

```python
import json
import subprocess
import urllib.request

info = json.loads(subprocess.check_output(
    ["colabllm", "qwen-flash", "info", "--json", "--show-secrets"], text=True))
base = info["url"].rstrip("/")
headers = {"Authorization": "Bearer " + info["api_token"], "Content-Type": "application/json"}
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
opener = urllib.request.build_opener(NoRedirect())
with opener.open(urllib.request.Request(base + "/v1/models", headers=headers), timeout=30) as response:
    model = json.load(response)["data"][0]["id"]
payload = {"model": model, "messages": [{"role": "user", "content": "用一句话解释向量数据库。"}],
           "max_tokens": 256, "stream": False,
           "chat_template_kwargs": {"enable_thinking": False}}
with opener.open(urllib.request.Request(base + "/v1/chat/completions", headers=headers,
        data=json.dumps(payload).encode()), timeout=120) as response:
    msg = json.load(response)["choices"][0]["message"]
    print(msg.get("content") or msg.get("reasoning_content"))
```

服务端默认 `--reasoning on`；客户端可在单次请求中通过 `chat_template_kwargs: {"enable_thinking": false}` 切换低延迟直出，或设为 `true` 开启深度思考（思考内容位于 `message.reasoning_content`）。

视觉输入将消息 `content` 改成内容块列表：

```json
[{"type":"text","text":"描述图片"},{"type":"image_url","image_url":{"url":"data:image/png;base64,IMAGE_BASE64"}}]
```

图片使用服务支持的可访问 URL 或真实 base64 数据；本机文件路径不能直接传给远端模型。
`IMAGE_BASE64` 为占位值。服务没有 `chat` CLI 子命令，请调用 API。
流式调用使用 `stream: true` 并处理 SSE，不能把流式返回一次性当 JSON 解析。

## 多实例与存储

```bash
colabllm qwen-flash up -i 2
colabllm qwen-flash info -i 2 --json
colabllm qwen-flash ps --json
colabllm qwen-flash down -i 2
```

调用实例 2 时，示例 Python 的 `info` 参数同样加 `-i 2`。
G4 上首次下载约 `104.5 GiB` 模型分片并编译 `sm_120` `llama.cpp` 耗时较久，建议保持 `--ready-timeout 5400`；显存与主机内存需求不适合改用 `T4` 或 `L4`。
模型分片暂存在本地高速 NVMe SSD `/content/qwen-flash/model`（自动跳过 `imatrix_micro3.gguf` 并设置 `HF_XET_CHUNK_CACHE_SIZE_BYTES=0` 防双倍占盘），`llama.cpp` 编译产物归档缓存在 Drive（`/content/drive/MyDrive/models/.runtimes/llama.cpp`）；停止 VM 会释放本地 SSD 文件。
API 不自动把聊天记录或响应存入 Drive，客户端需自行保存；服务的 1M 长上下文能力用于超长输入与多轮上下文，不代表单次请求应无上限生成。

更新指定 Key 时须同时更新私密状态和实际服务端认证；只编辑本机状态不会改变运行中 server。
单纯换 Key 不必释放 GPU，但应先检查在途请求，再按服务重载方式操作并验证新旧 Key。
