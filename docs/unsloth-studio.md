# Unsloth Studio 运行时使用说明

> Tunnel 域名说明：以下固定域名示例需先设置主域名，例如 `colabllm tunnel-config --base-domain example.com`（所有服务共享）。未配置时使用动态 URL；`up --base-domain example.com` 可单次覆盖，`up --quick-tunnel` 强制动态。详见 [Tunnel 配置](../README.md#cloudflare-tunnel-配置)。

对应文件：
- 执行脚本：[src/colabllm/services/unsloth-studio.py](../src/colabllm/services/unsloth-studio.py)
- 远端 Notebook：[src/colabllm/services/unsloth-studio.ipynb](../src/colabllm/services/unsloth-studio.ipynb)（由 [templates/build_notebooks.py](../templates/build_notebooks.py) 生成）

---

## 1. 架构与配置说明

| 配置项 | 默认值 / 说明 |
| --- | --- |
| **默认 GPU** | `L4`（可通过 `--gpu A100` / `--gpu T4` / `--gpu H100` 覆盖） |
| **服务监听端口** | `127.0.0.1:8888` |
| **安全认证** | `auth="builtin"`（Unsloth Studio 自带 Web 登录鉴权，由启动脚本注入或由 Unsloth 生成并在终端输出登录凭据） |
| **数据持久化** | 自动挂载 Google Drive 至 `/content/drive`，训练权重与数据集可保存在 `/content/drive/MyDrive/colab-llm/unsloth-studio` |
| **默认固定域名** | `https://unsloth-studio.example.com`（使用 `--domain` 时） |

---

## 2. 启动与日常管理命令

```bash
# 1. 启动服务并绑定固定域名 https://unsloth-studio.example.com
colabllm unsloth-studio up --domain

# 2. 使用随机 *.trycloudflare.com 临时域名启动
colabllm unsloth-studio up --quick-tunnel

# 3. 指定更高规格 GPU（如 A100）并传入 Hugging Face Token（用于下载受限模型或推送权重）
colabllm unsloth-studio up --domain --gpu A100 --hf-token hf_xxx

# 4. 查看访问 URL 与 Web 登录账号密码
colabllm unsloth-studio info --show-secrets

# 5. 查看实时训练/运行日志
colabllm unsloth-studio logs -f

# 6. 检查 Colab 会话状态与公网连通性
colabllm unsloth-studio status

# 7. 停止服务并释放 Colab VM
colabllm unsloth-studio down
```

### 2.1 多实例（多会话）并发运行

当需要同时进行多个微调任务或对比实验时，可通过 `-i <ID>` / `--instance <ID>` 启动多个独立的 Unsloth Studio 会话：

```bash
# 启动第 2 个实例（会话 unsloth-studio-2，绑定 https://unsloth-studio-2.example.com）
colabllm unsloth-studio up -i 2 --domain --gpu A100

# 查看所有 Unsloth Studio 实例状态与 URL；凭据需显式 --show-secrets
colabllm unsloth-studio ps
colabllm unsloth-studio info --show-secrets -i 2

# 查看第 2 个实例日志 / 停止指定或全部实例
colabllm unsloth-studio logs -i 2 -f
colabllm unsloth-studio down -i 2
colabllm unsloth-studio down --all
```

---

## 3. Web 界面登录与使用

1. 运行 `colabllm unsloth-studio up --domain` 等待就绪，再运行 `colabllm unsloth-studio info --show-secrets` 获取登录信息：
   - `URL`：`https://unsloth-studio.example.com`
   - `app login`：显示的用户名与密码（`admin / <password>`）
2. 在浏览器中打开该 URL，输入上述登录凭据即可进入 Unsloth Studio 可视化微调、推理与模型导出界面。
3. 导出模型或保存 Checkpoint 时，建议将输出路径指向 `/content/drive/MyDrive/colab-llm/unsloth-studio/`，以确保会话结束后训练成果持久保留在 Google Drive 中。
