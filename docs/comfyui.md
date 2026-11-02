# ComfyUI + ComfyUI-Manager 运行时使用说明

> Tunnel 域名说明：以下固定域名示例需先设置主域名，例如 `./colabctl.py tunnel-config --base-domain example.com`（所有服务共享）。未配置时使用动态 URL；`up --base-domain example.com` 可单次覆盖，`up --quick-tunnel` 强制动态。详见 [Tunnel 配置](../README.md#cloudflare-tunnel-配置)。

对应文件：
- 执行脚本：[services/comfyui.py](services/comfyui.py)
- 远端 Notebook：[services/comfyui.ipynb](services/comfyui.ipynb)（由 [templates/build_notebooks.py](templates/build_notebooks.py) 生成）

---

## 1. 架构与存储设计

| 目录 / 组件 | 存储位置 | 说明 |
| --- | --- | --- |
| **模型权重 (`/content/ComfyUI/models`)** | **Colab 本地 NVMe SSD** | **绝不存入 Google Drive**。按工作流或模型 URL 即时高速下载（300~360 MB/s），避免网盘 I/O 瓶颈与空间占用；已下载模型自动跳过，且自动清理 `hf_xet` 临时分块缓存 |
| **生成结果 (`/content/ComfyUI/output`)** | **Google Drive** (`/content/drive/MyDrive/colab-llm/comfyui/output`) | 软链接至网盘，生成的图片/视频/音频自动持久化保存 |
| **工作流与用户配置 (`/content/ComfyUI/user`)** | **Google Drive** (`/content/drive/MyDrive/colab-llm/comfyui/user`) | 工作流保存目录为 `user/default/workflows/`，跨会话持久保留 |
| **输入素材 (`/content/ComfyUI/input`)** | **Google Drive** (`/content/drive/MyDrive/colab-llm/comfyui/input`) | 上传的参考图/视频自动持久化保存 |
| **自定义节点 (`/content/ComfyUI/custom_nodes`)** | **Google Drive** (`/content/drive/MyDrive/colab-llm/comfyui/custom_nodes`) | 默认预装 `ComfyUI-Manager`，通过 Manager 安装的节点跨会话保留 |
| **安全认证层** | **Caddy (`:18080` -> `127.0.0.1:8188`)** | 浏览器访问走 Basic Auth（登录一次后自动种入 `ct_session` Cookie），API 请求走 `Authorization: Bearer <token>` |

---

## 2. 启动与日常管理命令

```bash
# 1. 启动服务并绑定固定域名 https://comfyui.example.com（默认使用 L4 GPU）
./services/comfyui.py up --domain

# 2. 启动时直接指定一个或多个工作流，自动预下载所需模型到本地 SSD
./services/comfyui.py up --domain --workflow video_minimax_h3_t2v
./services/comfyui.py up --domain \
  --workflow video_minimax_h3_t2v \
  --workflow https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_r2v.json

# 3. 指定更高规格 GPU（如 A100 / H100 / G4）或传入 Hugging Face Token
./services/comfyui.py up --domain --gpu A100 --hf-token hf_xxx

# 4. 查看访问地址、登录账号密码与 API Token
./services/comfyui.py info

# 5. 查看实时日志 / 检查运行状态与公网连通性
./services/comfyui.py logs -f
./services/comfyui.py status

# 6. 停止服务并释放 Colab VM（加 --delete-tunnel 同时清理 Cloudflare 隧道与 DNS）
./services/comfyui.py down
```

### 2.1 多实例（多会话）并发运行

支持通过 `-i <ID>` / `--instance <ID>` 同时启动多个独立的 ComfyUI GPU 实例（共享网盘中的 `user` 工作流、`custom_nodes`、`input` 和 `output`，各自拥有独立的本地高速 SSD `/content/ComfyUI/models`）：

```bash
# 启动第 1、第 2 个实例（分别绑定 comfyui-1.example.com 与 comfyui-2.example.com）
./services/comfyui.py up -i 1 --domain --workflow video_minimax_h3_t2v
./services/comfyui.py up -i 2 --domain --workflow video_minimax_h3_i2v

# 查看所有 ComfyUI 实例状态与 URL
./services/comfyui.py ps

# 向指定实例（-i 2）或所有运行中实例（-A / --all-instances）同步拉取工作流/模型到本地 SSD
./services/comfyui.py pull-workflow -i 2 video_minimax_h3_r2v
./services/comfyui.py pull-workflow -A video_minimax_h3_r2v
./services/comfyui.py pull-model -A https://huggingface.co/.../model.safetensors --dir loras

# 停止单个实例或全部实例
./services/comfyui.py down -i 2
./services/comfyui.py down --all
```

---

## 3. 按需下载模型到本地 SSD（三种方式）

### 方式一：ComfyUI 网页端顶部「⬇️ SSD 模型下载」按钮（推荐）

打开 `https://comfyui.example.com` 登录后，点击右上角工具栏的 **「⬇️ SSD 模型下载」** 按钮即可打开管理面板：

1. **⚡ 一键解析并下载【当前画布工作流】所需模型到本地 SSD**
   - 在 ComfyUI 中打开任意工作流（从左侧模板库、网盘 Workflows 列表，或直接拖入 `.json` 文件到画布），直接点击该按钮即可提取画布中所有模型并下载到本地 SSD。
2. **📥 按工作流 URL / 官方模板名 / 网盘文件名批量下载**
   - 输入框支持粘贴 GitHub 工作流链接、输入官方模板名（如 `video_minimax_h3_r2v.json`）或 Google Drive 中保存的工作流文件名。
3. **🎯 指定单个模型 URL 下载到对应目录**
   - 粘贴 Hugging Face `blob` / `resolve` 链接或直链后，界面会**自动提取文件名并自动匹配目标子目录**（如 `loras`、`diffusion_models`、`vae`、`text_encoders`、`checkpoints` 等，也支持选择 `✏️ 自定义目录...` 输入任意路径）。
   - 点击 **「🎯 下载到该目录」** 即可实时查看进度，下载完成后**自动刷新所有节点的模型下拉框**（`app.refreshComboInNodes()`）。

---

### 方式二：命令行按工作流增量下载（`pull-workflow`）

在会话运行期间，随时可通过命令行将新工作流所需的模型增量拉取到当前实例的本地 SSD：

```bash
# 1. 传入完整的工作流 GitHub URL 或直链
./services/comfyui.py pull-workflow https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_r2v.json

# 2. 传入官方工作流模板名（可省略 .json 后缀）
./services/comfyui.py pull-workflow video_minimax_h3_i2v

# 3. 传入保存在 Google Drive 网盘工作流目录中的自定义工作流文件名
./services/comfyui.py pull-workflow my_custom_workflow.json
```

**工作流与模型解析规则**：
- **工作流查找顺序**：若仅传入文件名，优先从官方 [`Comfy-Org/workflow_templates`](https://github.com/Comfy-Org/workflow_templates/tree/main/templates) 仓库拉取；若网络不存在该模板，自动回退搜索 Google Drive 工作流目录（`/content/drive/MyDrive/colab-llm/comfyui/user/default/workflows`、`workflows/`、`user/`）及本地已安装模板包。
- **三级模型链接提取**：
  1. 优先读取节点及子图（Subgraphs）中的 `properties.models` 元数据（含精确 `url`、`directory`、`name`）；
  2. 若旧工作流无 `properties.models`，自动提取工作流内 `MarkdownNote` 备注节点中的 Markdown 下载链接 `[filename.safetensors](https://...)`；
  3. 兜底匹配 `ComfyUI-Manager` 的 `model-list.json` 索引库。
- **自动去重**：不同工作流共享的相同模型（只要 `/content/ComfyUI/models/<目录>/<文件名>` 已存在且大小 > 0）会直接打印 `SKIP (already on local SSD)` 并跳过，绝不重复下载。

---

### 方式三：命令行指定单个模型 URL 与目录下载（`pull-model`）

```bash
# 指定模型 URL 与目标子目录（--dir / -d）
./services/comfyui.py pull-model \
  https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors \
  --dir loras

# 若 URL 路径本身已包含标准目录名（如 /loras/、/vae/、/diffusion_models/），可省略 --dir 自动推断
./services/comfyui.py pull-model \
  https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/vae/minimax_h3_audio_vae_fp32.safetensors

# 自定义保存文件名（--name / -n）
./services/comfyui.py pull-model \
  https://huggingface.co/example/repo/resolve/main/weights.safetensors \
  --dir checkpoints \
  --name custom_name.safetensors
```

---

## 4. API 接口说明

当通过程序调用 ComfyUI 或其扩展接口时，在请求头带上 `Authorization: Bearer <API_TOKEN>`（可通过 `./services/comfyui.py info` 查看）：

```bash
# 查询当前本地 SSD 剩余容量与已缓存模型列表
curl -s -H "Authorization: Bearer $TOKEN" https://comfyui.example.com/api/ct/ssd-status | jq

# 触发异步下载任务（返回 202 Accepted 与 job_id）
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"workflow": "video_minimax_h3_t2v"}' \
  https://comfyui.example.com/api/ct/pull-workflow | jq
```
