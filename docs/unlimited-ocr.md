# Unlimited-OCR 2：Drive 批量 OCR

[官方项目](https://github.com/baidu/Unlimited-OCR) / [模型](https://huggingface.co/baidu/Unlimited-OCR-2)。使用官方 Transformers 原生推理接口和 Markdown 后处理，默认 **A100**、BF16。Python 3.10 隔离环境；依赖、模型及后处理代码固定版本。模型下载到 Colab 本地 SSD；Triton/Inductor 缓存在 SSD 编译并按 GPU/环境归档到 Drive；OCR 输出和任务状态保存在 Drive。

## 启动

```bash
colabllm unlimited-ocr up
colabllm unlimited-ocr status
colabllm unlimited-ocr logs -f
colabllm unlimited-ocr info  # 含凭证，请勿分享输出
```

遵循 `colabllm tunnel-config` 的项目级设置；当前配置的域名对应 `https://unlimited-ocr.example.com`。支持 `up --gpu A100`、`--quick-tunnel`、`--transport cloudflare|frp`、`-i ID` 等共享参数。Caddy 提供 Basic / Bearer 认证。`CT_READY` 前必须完成模型加载和真实 GPU 推理预热。

## 文件与目录批量转换

输入同时支持**本机文件/文件夹与 Google Drive 文件/文件夹**。默认：本机存在的路径自动按本地处理；`drive://...`、`MyDrive/...`、`/content/drive/MyDrive/...` 按 Drive 处理；其他相对路径沿用 Drive 用法。可用 `--local` / `--drive` 强制指定所有输入类型，或使用 `local://...` / `drive://...` 混合指定。`./...`、`../...`、本机绝对路径不存在时会报错。Drive 不接受网页分享链接或文件 ID。

`--output-dir` 指定 **Drive 输出根目录**；`--local-output-dir` / `-O` 指定**本机输出根目录**，两者可同时使用。本地输入分块上传（每块 512 KiB，SHA256 校验）到 Drive 的服务 uploads 目录，转换结果保存在 Drive 后分块下载；大文件不受单次 HTTP 请求体大小限制。本地 CLI 仅依赖 Python 标准库。上传副本保留在 `colab-llm/unlimited-ocr/uploads/`，可在任务结束后自行清理。

```bash
# 本地单文件或文件夹，自动上传并下载完整结果到本机
colabllm unlimited-ocr ocr ./合同.pdf -O ./ocr-results
colabllm unlimited-ocr ocr ./documents --local -O ./ocr-results

# 本地输入 -> 指定 Drive 目录，同时下载到本机
colabllm unlimited-ocr ocr ./documents --output-dir 'OCR/results' -O ./ocr-results

# Drive 输入 -> 本地输出
colabllm unlimited-ocr ocr 'drive://documents' -O ./ocr-results

# 本地和 Drive 输入可混合
colabllm unlimited-ocr ocr ./合同.pdf 'drive://scans' -O ./ocr-results

# 查看可处理文件（PDF、PNG、JPEG、WEBP、BMP、TIFF）
colabllm unlimited-ocr ls-drive 'documents'

# 单文件；输出仍在 Drive
colabllm unlimited-ocr ocr 'documents/合同.pdf' --output-dir 'OCR/results'

# 多个文件或文件夹；默认递归、去重；单个文件失败后继续其余文件
colabllm unlimited-ocr ocr 'documents/books' 'scans/照片.jpg' --output-dir 'OCR/results'

# 后台提交，关闭本次客户端也不会停止任务（本地输入上传完成后）
colabllm unlimited-ocr ocr 'documents' --output-dir 'OCR/results' --no-wait
colabllm unlimited-ocr job JOB_ID --wait
# 稍后查询并下载本地结果（支持客户端断开后重新下载）
colabllm unlimited-ocr job JOB_ID --wait -O ./ocr-results

# 可选：非递归、重新识别、PDF 分辨率和页面窗口
colabllm unlimited-ocr ocr 'documents' --no-recursive --overwrite --dpi 200 --pages-per-chunk 10

# 多实例（每个实例内部顺序执行，避免多个任务同时占满显存）
colabllm unlimited-ocr up -i 2
colabllm unlimited-ocr ocr -i 2 'documents'
```

`convert` / `run` 是 `ocr` 的别名。默认输出根目录为 `MyDrive/colab-llm/unlimited-ocr/outputs`。

例如 `documents/books/书.pdf` 写入：

```text
OCR/results/documents/books/书.pdf/
  result.md                # 可阅读 Markdown，表格为 HTML
  result.raw.txt           # 官方布局标记原始输出
  chunk-0001/*.jpg          # Markdown 引用的图表/图片裁剪
  complete.json            # 源文件签名、参数、页数和 token 数
```

Drive 输入保留 MyDrive 相对目录及原扩展名。本地文件夹保留输入文件夹名及子目录（如 `./books/sub/a.pdf` -> `<输出根>/books/sub/a.pdf/result.md`），单文件使用文件名；同批输出名称冲突会报错。转换开始前清除旧完成标记，成功后才写入新标记；源文件大小/修改时间、模型版本或参数变化会触发重新识别。重跑同一批次会跳过未变更的已完成文件；`--overwrite` 强制重做。任务记录位于 `colab-llm/unlimited-ocr/jobs/<JOB_ID>.json`；服务启动后自动将持久化的 `queued` / `running` 任务重新排队，保留原任务 ID，从最后完成的页面组继续。已完成文件跳过；失败和旧版 `interrupted` 任务可通过 `resume JOB_ID` 重试。失败/中断任务的 CLI 退出码为 1。

默认 **10 页为一组**，组内使用 `infer_multi` 保留跨页上下文，表格/段落合并只发生在组内，组间独立处理。`--pages-per-chunk 1–100` 可调整；扩大窗口增加显存占用。PDF 默认 200 DPI（72–400）；多帧 TIFF 按帧顺序处理。`--tokens-per-page` 默认 8000（32–20000），每组总输出上限 120000 tokens；预算耗尽可能截断内容。文件夹中的图片各作为一份文档处理。长文档只渲染当前窗口，临时页图在本地 SSD 使用后清理。

## API 与运行生命周期

API 需 `Authorization: Bearer <API_TOKEN>`，CLI 默认读取对应实例 `~/.colabllm/services/` 中的 URL 和 Token。可用 `UNLIMITED_OCR_URL` / `UNLIMITED_OCR_API_TOKEN` 环境变量及 `--url` 覆盖连接信息。

- `POST /api/jobs`：JSON `{"inputs":["documents"],"output_dir":"OCR/results","pages_per_chunk":10}`，立即返回 job 记录（含 ID）。
- `GET /api/jobs/<ID>`：任务和逐文件状态、当前页进度、错误及 Drive 输出目录。
- `POST /api/drive-list`：JSON `{"inputs":["documents"],"recursive":true}`。
- `POST /api/upload`：本地文件分块上传（SHA256、偏移校验）。
- `POST /api/results`：按 job ID 和文件序号列出结果文件或分块读取。
- `GET /health`：模型和 GPU 预热完成后返回 200。

队列在服务端执行，客户端断开后任务继续；Colab 会话仍依赖本机后台 runner 保持连接。不要关闭主机或运行 `down`，直到任务结束。关闭服务：

```bash
colabllm unlimited-ocr down
# 同时删除本实例域名/隧道资源：
colabllm unlimited-ocr down --delete-tunnel
```

源码：`templates/unlimited_ocr_app.py`、`templates/unlimited_ocr_setup.py`；通过 `python templates/build_notebooks.py` 再生成独立 notebook。官方模型以 `trust_remote_code=True` 加载，固定模型 revision 为 `0480966938291445591f69f08a13b041c073a96a`，后处理固定于上游提交 `ea42e6d15ff93fdd0e8041fc0ac70880513453d3`。

## 已验证（2026-11-03）

实际在 Colab A100 启动并通过公网 FRP 域名验证：匿名/错误 Token 均为 401，正确 Token 为 200；两页 PDF 单文件转换、重复提交跳过、包含子目录的两文件/四页批处理、后台提交及独立 `job --wait` 查询均通过，Markdown 和原始结果写回 Drive。样本文字主体正确，但有一处多余识别内容，实际文档仍应复核 OCR 结果。

`python -m unittest discover -s tests -p test_unlimited_ocr.py -v`：6 项通过。全仓 71 项测试运行中，3 项既有 LongCat 测试因本机缺少 `torch` / `numpy` 导入失败，另有 4 项跳过。notebook 生成、CLI dry-run 及 `git diff --check` 通过。

本地文件支持补充验证：本地子目录 PDF 自动上传并下载结果、超过 512 KiB 的 PDF 分块上传及校验、Drive 输入下载到本地均在当前 A100 服务上完成实测。当前针对本服务的 8 项测试全部通过。

## 完整推理参数透传

固定版本模型 `infer()` 的 **19 个推理选项全部支持**。CLI 使用横线参数名，API / JSON 使用官方下划线名称：

| 类别 | JSON 参数名 |
| --- | --- |
| 提示词 | `prompt` |
| 图像 | `base_size`, `image_size`, `crop_mode`, `max_crops` |
| 生成 | `temperature`, `max_length`, `max_new_tokens` |
| 防重复 | `no_repeat_ngram_size`, `ngram_window`, `ngram_whitelist_token_ids` |
| 页面保护 | `page_count_guard`, `max_page_tokens` |
| 稀疏注意力 | `dash_topm` |
| 保存/统计/返回 | `save_results`, `test_compress`, `eval_mode`, `tps_interval`, `return_dict` |

```bash
colabllm unlimited-ocr ocr ./documents -O ./results \
  --prompt 'Multi page parsing.' --temperature 0 \
  --crop-mode --max-crops 8 --image-size 640 \
  --max-new-tokens 16000 --no-repeat-ngram-size 35 --ngram-window 1024 \
  --dash-topm 16 --max-page-tokens 20000

# JSON 文件或内联 JSON；未指定项沿用服务默认/模型默认
colabllm unlimited-ocr ocr 'drive://documents' --params-file ./ocr-params.json
colabllm unlimited-ocr ocr ./合同.pdf \
  --params-json '{"crop_mode":null,"max_new_tokens":null,"temperature":0}'

# 单项 JSON 覆盖，可重复；字符串需要 JSON 引号，数组和 null 原样支持
colabllm unlimited-ocr ocr ./合同.pdf \
  --param 'prompt="Multi page parsing."' \
  --param 'ngram_whitelist_token_ids=[1,128827]' \
  --param 'crop_mode=null' --no-page-count-guard --save-results
```

合并优先级（从低到高）：服务默认 → `--params-file` → `--params-json` → 独立 CLI 开关 → `--param`（重复同一项时取最后值）。布尔开关支持 `--crop-mode` / `--no-crop-mode` 等正反写法；`crop_mode=null` 通过 JSON 或 `--param` 表示。未知参数和类型错误在提交前拒绝，API 同样校验。

API：`POST /api/jobs` 的 `inference_params` 字段直接传推理参数：

```json
{
  "inputs": ["documents/合同.pdf"],
  "output_dir": "OCR/results",
  "pages_per_chunk": 10,
  "inference_params": {
    "prompt": "Multi page parsing.",
    "crop_mode": false,
    "max_new_tokens": 16000,
    "temperature": 0,
    "page_count_guard": true
  }
}
```

参数按每个页面组调用 `model.infer()` 传入；不再经过会改写提示词和强制返回格式的 `infer_multi()` 封装。`tokenizer`、`image_file`（或 `image_files`）、`output_path` 属于模型对象和任务文件管理，由服务填充，不能通过 `inference_params` 覆盖；`save_results=true` 的官方原生输出放在每份结果的 `native-0001/` 等分组目录，照常可下载。`return_dict` 控制模型内部返回格式，API 始终返回统一的任务 JSON。

为兼容此前行为，服务默认覆盖值为：`eval_mode=true`、`crop_mode=false`、`return_dict=true`、`no_repeat_ngram_size=35`、`ngram_window=1024`（单页 128），`max_new_tokens=min(120000,tokens_per_page*组内页数)`；其余项沿用固定模型版本默认。显式 `max_new_tokens` 完全覆盖自动预算，传 `null` 使用上游的 `max_length - prompt_len` 预算。`max_page_tokens<=0` 按上游逻辑关闭单页 token 保护。增大上下文、裁剪数量或输出预算可能增加显存和运行时间。

完整参数写入任务和 `complete.json` 的 `options.inference_params`，参数变化会重新转换。`save_results=false` 只关闭官方额外保存，服务的 Markdown、原始文本和图片输出仍保留。

参数透传验证：11 项本地测试通过（含完整参数传到模型、两种返回格式、配置合并优先级、错误参数拒绝及参数变化重跑）。当前 A100 服务已实测一次传入全部 19 个参数、官方 `save_results` 文件下载，以及 JSON 文件传入 `crop_mode=null` / `max_new_tokens=null`；两次识别均完成。公网 API 对未知参数、覆盖 `output_path`、错误布尔类型均返回 400。


## 服务重启与断点续跑

每个页面组在 Markdown、原始文本、图片及官方额外输出全部保存后，才原子写入 Drive 检查点（结果目录下 `.checkpoints/`）。服务进程重启后，原 `queued` / `running` 任务自动恢复原 ID 和参数并重新排队；复用已完成组，仅重算正在推理且尚未提交检查点的组。默认 10 页一组，可用 `--pages-per-chunk 1` 将恢复粒度缩小到单页。

如果 Colab VM 被回收，先重新 `up` 创建/恢复服务并挂载同一个 Drive；模型加载完毕后自动恢复队列。仅重启 API 进程由现有 watchdog 自动拉起。恢复单位是页面组，不保存组内逐 token GPU 状态。

```bash
# 原任务 ID 持续有效；服务暂时返回 5xx 或网络中断时，已开始的轮询会继续等待
colabllm unlimited-ocr job JOB_ID --wait -O ./results

# 显式恢复 failed 或旧版本 interrupted 任务；默认只入队，--wait 等待完成
colabllm unlimited-ocr resume JOB_ID --wait -O ./results
```

API：`POST /api/jobs/JOB_ID/resume`，请求体 `{}`，返回 202 和原任务记录。运行/排队/已完成任务拒绝重复恢复。查询结果包含 `resume_count`、`automatic_resume` 和各文件 `resumed_chunks`（本次执行复用的组数）。普通模型/文件错误仍标记 failed，避免无限自动重试；可修正源文件后手动恢复。

源文件大小/修改时间、模型版本或参数变化时，旧组检查点失效。组输出丢失、校验失败或检查点 JSON 损坏时，只重算对应组；损坏的任务记录不会阻止其他任务恢复。`--overwrite` 新提交任务从头计算，但该任务自身重启后仍可续跑。旧版本没有页面组检查点的文件首次恢复需从头处理；已有完整文件仍可跳过。检查点保留在 Drive，不随本地下载输出导出。需保留 Drive 源文件/本地上传副本和检查点，才能恢复。

恢复功能验证：15 项本地测试通过。实际 A100 上对一个两组 PDF 任务在第一组检查点完成后执行 SIGKILL；watchdog 重启服务后，同一任务 ID 自动继续，最终 `resume_count=1`、`automatic_resume=true`、`resumed_chunks=1`、`chunks=2`，完整结果已下载验证。未重新推理已提交的第一组。
