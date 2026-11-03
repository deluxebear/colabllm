# Qwen Flash：LLM 与视觉推理

默认 **G4**，使用 llama.cpp 原生 Bearer 认证；提供 OpenAI 兼容接口，支持文本和视觉输入。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与凭据

```bash
colabllm qwen-flash up --ready-timeout 3600
colabllm qwen-flash status --json
colabllm qwen-flash info --json
colabllm qwen-flash info --json --show-secrets
colabllm qwen-flash logs -n 100
```

API Base URL 为返回的 `url` 后加 `/v1`；不要把网页地址误当成模型 ID。
`GET /v1/models` 返回实际 alias。`/health` 可以无需认证返回 200，校验 API Key 应使用 `/v1/models`：无密钥/错误密钥须失败。

## 调用聊天 API

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
           "max_tokens": 256, "stream": False}
with opener.open(urllib.request.Request(base + "/v1/chat/completions", headers=headers,
        data=json.dumps(payload).encode()), timeout=120) as response:
    print(json.load(response)["choices"][0]["message"]["content"])
```

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
当前模型是 IQ3_S GGUF 与 BF16 视觉投影器，配置为约 800K 上下文、YaRN 和全 GPU offload。
G4 上首次准备大模型及编译可能较久；内存/显存需求不适合随意改用 T4。
模型暂存 `/content/qwen-flash/model`，llama.cpp 编译缓存保存在 Drive 运行时目录；停止 VM 会丢失本地 SSD 文件。
API 不自动把聊天记录或响应存入 Drive，客户端需自行保存；服务的长上下文能力不代表单次请求应无上限生成。

更新指定 Key 时须同时更新私密状态和实际服务端认证；只编辑本机状态不会改变运行中 server。
单纯换 Key 不必释放 GPU，但应先检查在途请求，再按服务重载方式操作并验证新旧 Key。
