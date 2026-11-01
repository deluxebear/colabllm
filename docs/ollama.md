# Ollama 运行时使用说明

对应文件：
- 执行脚本：[services/ollama.py](services/ollama.py)
- 远端 Notebook：[services/ollama.ipynb](services/ollama.ipynb)（由 [templates/build_notebooks.py](templates/build_notebooks.py) 生成）

---

## 1. 架构与配置说明

| 配置项 | 默认值 / 说明 |
| --- | --- |
| **默认 GPU** | `T4`（可通过 `--gpu L4` / `--gpu A100` 覆盖） |
| **服务监听端口** | `127.0.0.1:11434`（仅本地回环，不直接对外暴露） |
| **安全认证层** | **Caddy (`:18080` -> `127.0.0.1:11434`)**，自动重写 `Host` 与 `Origin` 请求头以通过 Ollama 的来源校验；浏览器走 Basic Auth，API 客户端走 `Authorization: Bearer <token>` |
| **默认预拉取模型** | `CT_MODELS="qwen2.5:0.5b"`（支持逗号分隔配置多个模型） |
| **模型持久化目录** | `OLLAMA_MODELS=/content/drive/MyDrive/colab-llm/ollama/models`（缓存在 Google Drive，跨会话复用） |
| **默认固定域名** | `https://ollama.example.com`（使用 `--domain` 时） |

---

## 2. 启动与日常管理命令

```bash
# 1. 启动服务并绑定固定域名 https://ollama.example.com
./services/ollama.py up --domain

# 2. 使用临时 *.trycloudflare.com 链接启动，或指定 L4 GPU
./services/ollama.py up --gpu L4

# 3. 查看访问地址、Basic Auth 账号密码与 Bearer API Token
./services/ollama.py info

# 4. 查看实时日志 / 检查服务状态与安全探测
./services/ollama.py logs -f
./services/ollama.py status

# 5. 停止服务并释放 Colab VM
./services/ollama.py down
```

### 2.1 多实例（多会话）并发运行与启动时指定模型

```bash
# 启动时通过 --models 指定要预拉取的模型列表
./services/ollama.py up --domain --models qwen2.5:7b,llama3.2:3b

# 启动第 2 个独立实例（会话 ollama-2，绑定 https://ollama-2.example.com）
./services/ollama.py up -i 2 --domain --gpu L4 --models deepseek-r1:14b

# 查看所有 Ollama 实例列表与状态
./services/ollama.py ps

# 查看指定实例详情 / 日志 / 停止全部实例
./services/ollama.py info -i 2
./services/ollama.py logs -i 2 -f
./services/ollama.py down --all
```

---

## 3. API 调用与在线拉取新模型

先通过 `./services/ollama.py info` 获取 `URL` 与 `TOKEN`：

### 3.1 在线拉取其他 Ollama 模型 (`/api/pull`)

服务运行期间无需重启，可直接通过 API 拉取任意 Ollama 模型：

```bash
curl -H "Authorization: Bearer $TOKEN" https://ollama.example.com/api/pull \
  -d '{"name": "qwen2.5:7b"}'
```

### 3.2 查看已安装模型列表 (`/api/tags`)

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://ollama.example.com/api/tags | jq
```

### 3.3 原生生成接口 (`/api/generate` / `/api/chat`)

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://ollama.example.com/api/generate \
  -d '{
    "model": "qwen2.5:0.5b",
    "prompt": "你好，请用一句话介绍自己。",
    "stream": false
  }' | jq
```

### 3.4 OpenAI 兼容接口 (`/v1/chat/completions`)

Ollama 原生提供 `/v1/chat/completions`，可直接接入各类 OpenAI 客户端（将 Base URL 设为 `https://ollama.example.com/v1`，API Key 设为 `$TOKEN`）：

```bash
curl -s https://ollama.example.com/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5:0.5b",
    "messages": [{"role": "user", "content": "Hello!"}]
  }' | jq
```
