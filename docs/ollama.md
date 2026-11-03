# Ollama：模型管理与推理

默认 **T4**，Caddy 提供 Bearer 与浏览器 Basic Auth；Ollama 自身的端口不直接暴露公网。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与预加载

```bash
colabllm ollama up --models qwen2.5:7b
colabllm ollama status --json
colabllm ollama info --json
colabllm ollama logs -n 100
```

`up --models` 可重复或用逗号分隔；不指定时脚本默认预加载 `qwen2.5:0.5b`。
这些是远端 Ollama 模型名；应根据所选 GPU 的内存选模型。`--models` 是启动参数，不是推理命令。

## 列模型、生成文本、拉取模型

下面使用已安装的 `curl` 和 `jq` 捕获连接信息；不打印 API Key：

```bash
URL=$(colabllm ollama info --json | jq -r '.url')
TOKEN=$(colabllm ollama info --json --show-secrets | jq -r '.api_token')
curl --fail-with-body -H "Authorization: Bearer $TOKEN" "$URL/api/tags"
curl --fail-with-body -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$URL/api/generate" -d '{"model":"qwen2.5:7b","prompt":"介绍一下你自己。","stream":false}'
```

正在运行时可通过模型 API 拉取另一个适配当前 GPU 的模型：

```bash
curl --fail-with-body -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$URL/api/pull" -d '{"model":"qwen2.5:0.5b","stream":false}'
```

`/api/chat` 适合多轮消息；兼容的 OpenAI 客户端可用 `<URL>/v1`，`api_key` 仍是 Caddy Token。
API 的模型名以 `/api/tags` 返回值为准。流式模式可能返回逐行 JSON，示例 `stream:false` 才是单个 JSON。
拉模型只下载权重，不等于已生成响应；没有 `colabllm ollama generate` 子命令。
请求字段与接口细节见 [Ollama 官方 API 文档](https://github.com/ollama/ollama/blob/main/docs/api.md#pull-a-model)。

## 输出、实例与停止

```bash
colabllm ollama up -i 2 --models qwen2.5:0.5b
colabllm ollama ps --json
colabllm ollama info -i 2 --json
colabllm ollama down -i 2
```

实例凭据各自独立。当前 notebook 默认 `OLLAMA_MODELS=/content/ollama/models`，权重在 VM 本地 SSD，
释放 VM 后需要重新拉取；不能假定挂载 Drive 就会自动保存 Ollama 模型。
模型响应由客户端保存。`up` 后模型拉取完成才进入共享就绪阶段；若 GPU 不足，检查 Ollama 日志，
优先使用更小模型或用户指定的适配 GPU。避免为了加载新模型停止正在服务的实例。
