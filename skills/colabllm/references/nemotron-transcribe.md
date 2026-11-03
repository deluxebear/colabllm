<!-- Generated from docs/nemotron-transcribe.md; edit the source and run tools/sync_runtime_docs.py. -->

# Nemotron Transcribe：音频转写、字幕与批量处理

默认 **G4**，原生 Bearer 认证；支持本机/Drive 音频、媒体 URL、批量清单、说话人和时间戳。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与数据定位

```bash
colabllm nemotron-transcribe up
colabllm nemotron-transcribe status --json
colabllm nemotron-transcribe ls-drive 'audio' --json
colabllm nemotron-transcribe prompts
colabllm nemotron-transcribe transcribe --help
```

默认使用 BF16 模型；`up --precision fp8` 或 `nvfp4` 选择对应模型版本，`--repo` 覆盖仓库、`--mtp` 覆盖 MTP。
这些参数只影响启动，不会热替换运行中的模型。首次下载和编译可能较久。
`ls-drive` 直接通过 Colab 查询 Drive，不依赖公网 HTTP；默认只列当前层，加 `-r` 递归。

## 本机、Drive 与字幕

```bash
colabllm nemotron-transcribe transcribe ./meeting.wav --local --preset verbatim --json -O ./transcripts
colabllm nemotron-transcribe transcribe 'drive://audio/interview.wav' --drive \
  --preset speaker --timestamps --save-drive 'audio/transcripts' --json -O ./transcripts
colabllm nemotron-transcribe transcribe ./english.wav --local --preset translate --lang Chinese -o ./translation.txt
colabllm nemotron-transcribe transcribe 'audio/lecture.wav' --drive \
  --preset context --context '项目名, 专有名词, 人名' --json -O ./transcripts
```

`--local` / `--drive` 消除输入路径歧义；不要同时指定。`drive://` 是 MyDrive 路径。
`--preset speaker` 默认启用时间戳；`--timestamps` 可用于其他任务。`--preset clean` 清理口头语，`verbatim` 保留逐字表达。
自定义 `--prompt` / `--prompt-file` 可用于录音问答或总结；只有需要推理任务才考虑 `--reasoning`。
纯转写不要无意加入问答指令。`prompts` 列出本版本全部预设，包括翻译、语言识别、说话人和上下文提示。

## 目录、清单与多实例

```bash
# 目录默认仅当前层，递归须加 -r；将通配符加引号交给远端解析
colabllm nemotron-transcribe transcribe 'meetings/*.mp3' --drive --preset clean --json -O ./transcripts
colabllm nemotron-transcribe transcribe 'meetings' --drive -r --preset verbatim --json -O ./transcripts
colabllm nemotron-transcribe transcribe --batch-file ./tasks.jsonl --json -O ./transcripts -j 2
```

`tasks.jsonl` 每行一个任务，例如：

```jsonl
{"audio":"meetings/call.wav","preset":"speaker","timestamps":true}
{"audio":"meetings/lecture.wav","preset":"context","context":"项目名, 专有名词"}
```

还接受 JSON、CSV、TSV、TXT 清单；输入解析按字段内容和任务参数决定本机/Drive 来源。
同一服务并发可用 `-j`，但 GPU 内存仍有限。已存在多个实例时可用 `--instances 1,2` 或 `--all-instances` 分摊请求。
它们不自动创建更多 VM。`-i 2` 只选一个实例。

```bash
colabllm nemotron-transcribe up -i 2
colabllm nemotron-transcribe transcribe ./meeting.wav --local -i 2 --json -O ./instance2
colabllm nemotron-transcribe ps --json
colabllm nemotron-transcribe down -i 2
```

## 媒体 URL 与长音频

```bash
colabllm nemotron-transcribe download 'https://www.youtube.com/watch?v=VIDEO_ID' --download-dir 'audio/media' --json
colabllm nemotron-transcribe transcribe 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --preset verbatim --download-dir 'audio/media' --json -O ./transcripts
```

`VIDEO_ID` 为占位。可用 `--download-video` 保存视频、`--playlist` 处理播放列表。
登录源使用 `--cookies-drive`；强制本机 yt-dlp 用 `--local-ytdlp`，可配合 `--cookies` 或 `--cookies-from-browser`。
本机下载路径需要本机 yt-dlp/FFmpeg；Cookie 和媒体访问凭据不得写进共享日志。
缓存下载通常可复用，`--force-download` 会强制重下。

默认超过 40 分钟按静音点切片并合并文本/时间戳，`--chunk-minutes 0` 禁用。
`--normalize-audio` 可要求远端规范化；切片不意味着客户端可以从任意位置自动断点恢复。

## 输出与失败语义

`-O` 在本机保存各文件 `.txt/.nemotron.json/.srt`（字幕存在时）和 `batch_summary.jsonl`；
`-o` 指定单个文本、SRT、JSON 或批量汇总文件。`--save-drive` 不带值保存到原音频目录，带值指定 Drive 目录。
`--json` stdout 只有 JSON，进度在 stderr；批次可能部分成功，检查 `errors` 和退出码后再宣告完成。

`--timeout` 默认 3600 秒，是每个请求/音频任务限时，不是整批总时长。
该命令不是 OCR/Avatar 的持久 job 接口，不要在网络中断后假定能用 `job/resume` 恢复；保存已完成结果再重试失败项。
`NEMOTRON_URL` / `--url` 与 `NEMOTRON_API_TOKEN` / `--api-token` 支持远端调用，换来源必须显式给 Token。
