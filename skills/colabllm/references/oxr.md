<!-- Generated from docs/oxr.md; edit the source and run tools/sync_runtime_docs.py. -->

# SoMark OXR-1.0：PDF 与图片文档解析（SoMark 兼容 API + Demo）

默认 **A100**，Caddy Basic Auth / Bearer；SoMark SDK/CLI 的表单 `api_key` 同样经过校验。
OXR-1.0 是 1.3B BF16 文档解析模型，输出 Markdown / JSON、版面坐标与阅读顺序。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 架构与 GPU

同一张 GPU 上运行三个只绑定 `127.0.0.1` 的进程，公网只暴露经过 Caddy 鉴权的解析 API：

| 进程 | 端口 | 说明 |
| --- | --- | --- |
| D-FINE 版面分析 | `8091` | 权重 `layout_analysis/last.pth`，CUDA Graph，torch 2.11 cu130 |
| OXR VLM | `8090` | `vllm==0.25.1` + 权重目录自带的 out-of-tree 插件，BF16，`TRITON_ATTN` |
| 解析 API | `8000` | 上游 `oxr` FastAPI：`/demo`、`/docs`、`/v1/parse/*`，经公网 URL 访问 |

三个进程分别使用本地 SSD 上的独立 uv 虚拟环境；权重固定到 Hugging Face 修订版并下载到
`/content/oxr/model`；vLLM/Triton/FlashInfer/Inductor 编译缓存按 GPU 架构归档到
`MyDrive/colab-llm/oxr/cache-archives/`，首次启动后再次启动更快。

BF16 是硬性要求（compute capability ≥ 8.0），因此不支持 T4。默认 A100（40 GB）：vLLM
`--gpu-memory-utilization 0.70`、`--max-num-seqs 32`，其余显存留给 D-FINE。低并发试用可改用
`--gpu L4`（24 GB，自动 `max-num-seqs=16`），长文档/更高并发可用 `H100` 或 `G4`（≥64 GB 自动提高到 `64`）。

```bash
colabllm oxr up
colabllm oxr up --gpu L4                      # 自动 max-num-seqs=16
colabllm oxr up --max-num-seqs 16 --gpu-mem-util 0.6
colabllm oxr status --json
```

`--max-model-len`（默认 16384）与 `--concurrency`（解析 API 并发，默认等于 `max-num-seqs`）也可覆盖。
首次启动需要安装三个环境并下载约 3.2 GB 权重，通常 10–20 分钟。只有 D-FINE、vLLM、解析 API
全部健康，且一次真实的图片解析预热成功、公网鉴权验证通过后，`up` 才返回就绪。

## 命令行解析

```bash
colabllm oxr parse ./document.pdf -O ./parsed
colabllm oxr parse ./scans --formats md -O ./parsed
colabllm oxr parse ./paper.pdf --keep-header-footer --element-formats '{"table":"markdown"}'
colabllm oxr parse --help
```

支持 PDF、PNG、JPEG、BMP、TIFF、WEBP；目录递归处理。每个文件在 `-O` 目录（默认 `./oxr-output`）
生成 `<名称>.md` 与完整响应 `<名称>.json`（`--formats md,json` 控制），同名文件自动追加 `-2`。
标准输出打印每个文件的保存路径、警告与元数据；任一文件失败时退出码为 1。
请求是同步的，单文件默认超时 1800 秒，可用 `--timeout` 调整（`0` 不限时）。

## 浏览器 Demo

在 `colabllm web` 中点 **打开 Web UI**，或打开 `<公网地址>/demo`（根路径 `/` 会跳转到 `/demo`），
使用 `info --show-secrets` 中的 Basic Auth 用户名/密码登录。Demo 可查看原文件、Markdown 预览、源码、
版面框与阅读顺序并下载 Markdown；结果缓存 1 小时，过期需重新解析。接口文档在 `<公网地址>/docs`。

## HTTP API 与 SoMark SDK/CLI

解析接口与 SoMark API 兼容：`POST /v1/parse/sync`、`/v1/parse/async`、`/v1/parse/async_check`，
multipart 字段 `file`、`api_key`、可重复的 `output_formats=markdown|json`，以及可选的
`feature_config` / `element_formats` / `extra` JSON。响应在 `data.result.outputs.markdown/json`。

```bash
URL=$(colabllm oxr info --json | jq -r '.url')
TOKEN=$(colabllm oxr info --json --show-secrets | jq -r '.api_token')

curl --fail-with-body "$URL/v1/parse/sync" -H "Authorization: Bearer $TOKEN" \
  -F 'file=@document.pdf' -F 'api_key=unused' \
  -F 'output_formats=markdown' -F 'output_formats=json'

pip install somark
mkdir -p ./parsed-output   # somark 多格式输出要求目录已存在
somark --base-url "$URL/v1" --api-key "$TOKEN" parse document.pdf --formats md,json --out ./parsed-output/
```

鉴权方式：

- 所有路径：`Authorization: Bearer <api_token>`，或浏览器 Basic Auth / 登录后的会话 Cookie。
- 仅 `POST /v1/parse/sync|async|async_check`：SoMark SDK/CLI 无法发送请求头，只在表单里发送 `api_key`；
  该字段必须等于 `api_token`（常数时间比较，未配置时一律拒绝）。用 Bearer 调用时 `api_key` 只是
  上游要求的必填占位值。
- 无凭据、错误 Bearer 或错误 `api_key` 均返回 HTTP 401；`GET /health` 汇总三个进程状态，未就绪时 503。

本服务不是 OpenAI 兼容接口，`colabllm gateway` 不代理它；直接使用公网地址。

## 限制与排查

- 自然场景照片不是 OXR 的重点；不支持 GIF/HEIC、跨页表格拼接与 `output_formats=zip`。
- 解析 API 的异步任务保存在进程内存中，服务重启后旧的 `task_id` 失效；同步接口不受影响。
- 大 PDF 占用显存与时间，遇到显存紧张或超时可降低并发（`--max-num-seqs 16`，L4 上可降到 `8`）或换更大 GPU（`H100`/`G4`）。
- 日志：`colabllm oxr logs -f`；VM 上的进程日志位于 `/content/ct/{dfine,vllm,service}.log`，
  环境安装日志在 `/content/oxr/*-env.log`。vLLM 引擎死亡会由看门狗自动重启，其他进程退出也会被重启。
- 不用时执行 `colabllm oxr down` 释放 GPU。
