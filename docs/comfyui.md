# ComfyUI：工作流、模型下载与生成

默认 **L4**，包含 ComfyUI-Manager，Caddy 提供网页 Basic Auth 与 API Bearer 认证。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与网页

```bash
colabllm comfyui up
colabllm comfyui status --json
colabllm comfyui info --show-secrets
colabllm comfyui logs -n 100
```

打开 `info` 返回 URL，使用浏览器用户名密码登录。导入工作流，确认节点和模型可用，再入队生成。
API 客户端使用 Bearer Token；只有 URL 而没有密钥无法调 API。

## 启动前或运行时下载工作流模型

```bash
colabllm comfyui up --workflow video_example.json
colabllm comfyui pull-workflow 'https://example.com/workflow.json' --timeout 600
colabllm comfyui pull-model 'https://example.com/model.safetensors' --dir loras --name example.safetensors --timeout 600
colabllm comfyui pull-workflow --help
```

`example.com` 和示例模板名需要替换为真实、可获取的工作流/模型。
`up --workflow` 可重复或提供逗号分隔值；`pull-workflow` 在已运行 VM 中下载工作流引用的模型到 SSD。
这些命令**不提交生成任务**。没有模型清单或不受支持的引用不应假定都能自动推断，按下载结果和节点缺失提示处理。
`pull-model --dir` 应使用模型类别，如 `loras/checkpoints/diffusion_models/vae/text_encoders`，名称须与工作流引用一致。
下载目标是远端 ComfyUI，不是本机当前目录。`--timeout 0` 默认不限等待，超时不保证取消远端下载。

## 运行工作流与取得结果

UI 工作流文件和 API prompt JSON 不是同一格式。自动化时从 ComfyUI 导出 API 格式，替换输入、种子、文本和模型引用，
通过受认证的 ComfyUI API 提交 prompt、跟踪队列/历史，再下载产物；不要把 UI JSON 原样当 prompt。
通常使用 `/prompt` 提交、`/history/<prompt_id>` 查询，文件访问按返回的 filename/subfolder/type 取回。
接口及自定义节点参数以当前服务实际支持为准；需要时查询 `/object_info`，不猜节点参数。

保留 prompt ID，任务仍在队列或运行时不要重复入队。输出文件应下载到用户要求的本机目录，
或者确认已持久保存后报告 Drive 路径。导入的自定义节点可能需要安装、重启；在用户活动任务完成后再操作。

## 存储、多实例与恢复

```bash
colabllm comfyui up -i 2
colabllm comfyui pull-workflow video_example.json -i 2 --timeout 600
colabllm comfyui pull-model 'https://example.com/model.safetensors' --dir loras --instances 1,2 --timeout 600
colabllm comfyui ps --json
colabllm comfyui down -i 2
```

`--instances` / `-A/--all-instances` 会对多个已有实例下载，先确认目标，避免重复占盘。
输入、输出、user 设置和 custom_nodes 保存在服务 Drive 目录；模型文件默认在 `/content/ComfyUI/models` 的本地 SSD。
释放 VM 后模型需重下，工作流应保存其模型链接、文件名和节点版本。共享 Drive 不代表队列任务会在新 VM 自动恢复。
无模型/错误文件名先核对下载结果；502 先判断模型或服务是否仍在加载。读取日志后再决定是否重启。
