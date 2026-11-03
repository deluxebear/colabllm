# Unlimited-OCR 2：PDF 与图片批量转 Markdown

默认 **A100**，Caddy Bearer/Basic Auth；模型在 Colab SSD，任务与转换结果持久保存在 Drive。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与最短转换

```bash
colabllm unlimited-ocr up
colabllm unlimited-ocr status --json
colabllm unlimited-ocr ocr ./document.pdf -O ./ocr-results
colabllm unlimited-ocr ocr --help
```

`ocr` 的别名为 `convert/run`。本机文件按块上传到 Drive 再在 GPU 处理；`-O` 将结果下载回本机。
服务就绪要求模型加载及真实推理预热，域名可访问不等于 OCR 可用。

## 输入与输出目录

```bash
colabllm unlimited-ocr ocr ./documents --local -O ./ocr-results
colabllm unlimited-ocr ocr 'drive://scans' --output-dir 'OCR/results' -O ./ocr-results
colabllm unlimited-ocr ocr ./document.pdf 'drive://scans' -O ./ocr-results
colabllm unlimited-ocr ls-drive 'scans'
```

PDF、PNG、JPEG、WEBP、BMP、TIFF 可处理；目录默认递归，`--no-recursive` 限制为当前层。
存在的本机路径自动按本机处理，`drive://`、`MyDrive/` 或远端完整 MyDrive 路径按 Drive 处理。
其他相对路径可能按 Drive 解释；本机文件推荐写 `./` 或使用 `--local`。Drive 目录不是网页分享链接或文件 ID。
`--output-dir` 是 **Drive** 根目录；`-O/--local-output-dir` 是 **本机** 根目录，可同时指定。

默认 Drive 输出为 `MyDrive/colab-llm/unlimited-ocr/outputs/`。本机上传副本在服务 Drive `uploads/` 中保留，
需等任务不再依赖再清理。本机文件夹保留文件夹名/相对子目录；同批本机输出名称冲突会报错。

## 后台任务、查询与恢复

```bash
colabllm unlimited-ocr ocr 'drive://scans' --no-wait
colabllm unlimited-ocr job JOB_ID
colabllm unlimited-ocr job JOB_ID --wait --timeout 600 -O ./ocr-results
colabllm unlimited-ocr resume JOB_ID --wait --timeout 600 -O ./ocr-results
```

上传结束且返回任务 ID 后，客户端断开不取消服务端任务。默认 `ocr` 等待完成，`--no-wait` 只提交。
`job --wait` 的 `--timeout 0` 默认不限时；`--poll-interval` 改查询频率。超时退出 124，任务仍在后台，继续查原 ID。
`resume` 适用于失败/中断任务，已排队、运行或完成的任务不能重复恢复。
服务重启会将持久记录里的 queued/running 任务重新排队，复用已完成页面组；不是从头盲目重跑。

## 参数与结果验收

```bash
colabllm unlimited-ocr ocr ./documents --local --dpi 200 --pages-per-chunk 10 \
  --tokens-per-page 8000 -O ./ocr-results
colabllm unlimited-ocr ocr ./document.pdf --overwrite -O ./ocr-results
```

DPI 为 72–400，页面组 1–100，页 token 预算 32–20000。大页面组增加显存并保留组内跨页上下文，
不是组越大越好；组间不保证自动合并段落/表格。默认每组 10 页，预算有组级上限，截断时调整参数并检查输出。
高级模型推理参数以 `ocr --help` 为准；自定义 prompt 或布局参数可能改变结果结构，应在小样本验证后处理整批。
未变更的已完成文件默认跳过；`--overwrite` 强制重新转换。

每份文件目录通常含 `result.md`、`result.raw.txt`、`chunk-*/` 裁剪图片和 `complete.json`。
Markdown 的图片引用依赖子目录，交付时保留结构。任务可部分失败，检查每个文件状态和错误；failed/interrupted CLI 返回 1。
任务记录保存在 `MyDrive/colab-llm/unlimited-ocr/jobs/`，输出路径以任务响应为准。

## 多实例与远端调用

```bash
colabllm unlimited-ocr up -i 2
colabllm unlimited-ocr ocr ./document.pdf -i 2 -O ./instance2
colabllm unlimited-ocr logs -i 2 -n 100
colabllm unlimited-ocr down -i 2
```

`UNLIMITED_OCR_URL` / `--url` 和 `UNLIMITED_OCR_API_TOKEN` / `--api-token` 可连接已有服务。
API 包括 `POST /api/jobs`、`GET /api/jobs/ID`、`POST /api/jobs/ID/resume`、上传和结果接口；小请求按块传输大文件。
多个实例可能共享任务与输出所在 Drive 目录，不要把它们当成互相隔离的数据账户。
