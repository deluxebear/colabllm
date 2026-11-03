# Qwen3.8-Flash-Next (G4 / 800K / Vision) 运行时使用说明

> Tunnel 域名说明：以下固定域名示例需先设置主域名，例如 `colabllm tunnel-config --base-domain example.com`（所有服务共享）。未配置时使用动态 URL；`up --base-domain example.com` 可单次覆盖，`up --quick-tunnel` 强制动态。详见 [Tunnel 配置](../README.md#cloudflare-tunnel-配置)。

对应文件：
- 执行脚本：[src/colabllm/services/qwen-flash.py](../src/colabllm/services/qwen-flash.py)
- 远端 Notebook：[src/colabllm/services/qwen-flash.ipynb](../src/colabllm/services/qwen-flash.ipynb)（由 [templates/build_notebooks.py](../templates/build_notebooks.py) 生成）

---

## 1. 服务特性与架构配置

| 配置项 | 默认值 / 说明 |
| --- | --- |
| **模型仓库** | `SC117/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-GGUF` (`IQ3_S` 双分片，约 84.3 GB) |
| **多模态视觉投影器** | `mmproj-Qwen3.8-Flash-Next-BF16.gguf` (BF16 精度，约 1.13 GB，通过 `--mmproj` 加载) |
| **硬件要求** | **Colab G4 GPU**（NVIDIA RTX PRO 6000 Blackwell `sm_120`，~96 GB VRAM，~176 GB 系统内存），**不加 `--high-mem`** |
| **超长上下文** | **800K (`819200` tokens)**，基于 YaRN (`--rope-scaling yarn --rope-scale 3.125 --yarn-orig-ctx 262144`)，KV Cache `f16` + Flash Attention (`-fa on`) |
| **加载模式** | `--load-mode none --lazy-mode off`（将 27.5 GiB 的 `token_embd_ngram` 表常驻 CPU RAM，GPU 显存容纳主权重与 800K KV Cache） |
| **编译与模型缓存** | 并发准备：① `llama.cpp` 针对 CUDA `sm_120` 编译并缓存于 Google Drive (`/content/drive/MyDrive/models/.runtimes/llama.cpp/`)；② 模型优先通过 HF Xet 下载至本地高速 SSD (`/content/qwen-flash/model`) 并做 SHA256 校验，若 HF 不可用自动回退从 Google Drive 备份分片拷贝 |
| **安全认证** | `llama-server --api-key-file` 原生 Bearer Token 认证（`auth="builtin"`，无需前置 Caddy） |
| **默认固定域名** | `https://qwen.example.com`（OpenAI 兼容 Base URL：`https://qwen.example.com/v1`） |

---

## 2. 启动与日常管理命令

```bash
# 1. 启动服务并绑定默认域名 https://qwen.example.com（自动申请 G4 GPU）
colabllm qwen-flash up --domain

# 2. 使用临时 *.trycloudflare.com 域名启动
colabllm qwen-flash up --quick-tunnel

# 3. 查看访问地址与 Bearer API Token
colabllm qwen-flash info --show-secrets

# 4. 查看实时准备进度（HF 下载 / llama.cpp 编译 / 模型加载心跳）
colabllm qwen-flash logs -f

# 5. 检查 Colab 会话状态与 /v1/models 连通性探测
colabllm qwen-flash status

# 6. 轮换生成新的 API Key
colabllm qwen-flash up --domain --new-credentials

# 7. 停止服务并释放 G4 实例
colabllm qwen-flash down
```

### 2.1 多实例（多会话）并发运行

支持通过 `-i <ID>` / `--instance <ID>` 同时启动多个 Qwen3.8-Flash-Next G4 实例（共享 Google Drive 上的 `sm_120` 编译缓存与模型分片备份，无需重复编译）：

```bash
# 启动第 2 个实例（自动分配会话 qwen-flash-2，绑定 https://qwen-2.example.com）
colabllm qwen-flash up -i 2 --domain

# 查看所有实例状态、URL 与 API Token
colabllm qwen-flash ps

# 查看第 2 个实例的日志或状态
colabllm qwen-flash logs -i 2 -f
colabllm qwen-flash status -i 2

# 停止第 2 个实例，或停止所有实例
colabllm qwen-flash down -i 2
colabllm qwen-flash down --all
```

> **提示**：首次启动时需将 ~85 GB 模型分片下载到本地 SSD 并加载进显存/内存，耗时约 5~15 分钟，可通过 `colabllm qwen-flash logs -f` 查看实时进度。

---

## 3. OpenAI 兼容 API 调用示例

先通过 `colabllm qwen-flash info --show-secrets` 获取 `API_TOKEN`：

### 3.1 查看模型列表 (`/v1/models`)

```bash
curl -s https://qwen.example.com/v1/models \
  -H "Authorization: Bearer $API_TOKEN" | jq
```

### 3.2 文本对话 (`/v1/chat/completions`)

```bash
curl -s https://qwen.example.com/v1/chat/completions \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.8-flash-next",
    "messages": [
      {"role": "user", "content": "请用三句话概括 Blackwell 架构的主要特点。"}
    ],
    "temperature": 0.6,
    "max_tokens": 512
  }' | jq
```

### 3.3 多模态视觉理解 (`image_url`)

```bash
curl -s https://qwen.example.com/v1/chat/completions \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.8-flash-next",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "描述这张图片里的内容"},
          {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"}}
        ]
      }
    ],
    "max_tokens": 256
  }' | jq
```

---

## 4. 可选环境变量覆盖

如需调整上下文长度或量化版本，可在 [`src/colabllm/services/qwen-flash.py`](../src/colabllm/services/qwen-flash.py) 的 `SERVICE.env` 中修改：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CT_QWEN_CONTEXT` | `819200` | 上下文窗口大小（可改为 `524288` 或原生 `262144`） |
| `CT_QWEN_ROPE_SCALE` | `3.125` | YaRN 缩放倍数（800K 对应 `3.125`，512K 对应 `2.0`） |
| `CT_QWEN_QUANT` | `IQ3_S` | 模型量化子目录名 |
| `CT_QWEN_ALIAS` | `qwen3.8-flash-next` | OpenAI API 中的 `model` 名称 |
