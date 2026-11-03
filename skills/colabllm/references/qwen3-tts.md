<!-- Generated from docs/qwen3-tts.md; edit the source and run tools/sync_runtime_docs.py. -->

# Qwen3-TTS：合成、音色设计与克隆

默认 **L4**，原生 Bearer 认证；支持 CustomVoice、VoiceDesign、Base 克隆及持久音色注册。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与内置音色

```bash
colabllm qwen3-tts up
colabllm qwen3-tts status --json
colabllm qwen3-tts voices --json
colabllm qwen3-tts tts --help
```

默认预加载多任务所需模型，首次安装、下载和编译较久。内置说话人有 `vivian`、`serena`、`uncle_fu`、
`dylan`、`eric`、`ryan`、`aiden`、`ono_anna`、`sohee`；用 `voices` 查询本实例实际列表。

## 普通合成与风格控制

```bash
colabllm qwen3-tts tts '你好，欢迎使用语音服务。' --voice serena --lang Chinese -o ./speech.wav
colabllm qwen3-tts tts --text-file ./script.txt --voice vivian \
  --instruct '语速平稳，像在讲故事。' --speed 1.1 --save-drive -o ./narration.wav
```

文本可用位置参数、`--text` 或 UTF-8 `--text-file`，建议只选一种。
`-o` 是本机文件；省略时在当前目录生成带时间戳的 WAV 文件名。
`--format` 支持 `wav/mp3/flac/opus/pcm`，显式格式应搭配正确的输出扩展名。
速度范围为 0.25–4.0；`--max-new-tokens` 限制生成量，不能把它理解成文本字符数。

## 临时设计与复用音色

```bash
# 本次按描述生成，默认不注册名字
colabllm qwen3-tts tts '欢迎收听今天的故事。' --voice-design '温暖、清晰的成年女声' -o ./designed.wav

# 设计参考音频并注册，之后用 --voice 复用
colabllm qwen3-tts design-voice --instruct '平静、自然的中文男旁白' \
  --text '这是旁白音色的参考句。' --lang Chinese --name narrator -o ./narrator-ref.wav
colabllm qwen3-tts tts '这是另一段旁白。' --voice narrator -o ./reuse.wav
```

`tts --voice-design` 是当前文本的 VoiceDesign；`design-voice --name` 是设计后注册再克隆的工作流。
命名操作会修改持久音色，先查 `voices` 避免覆盖已有名称。

## 参考音频克隆与注册

```bash
colabllm qwen3-tts tts '使用参考音色说这句话。' --ref-audio ./reference.wav \
  --ref-text '参考音频中真实说出的文字。' --lang Chinese -o ./clone.wav
colabllm qwen3-tts upload-voice --name myvoice --audio ./reference.wav --ref-text '参考音频中真实说出的文字。'
colabllm qwen3-tts upload-voice --name drivevoice --drive 'drive://voices/reference.wav'
colabllm qwen3-tts tts '下次继续使用同一音色。' --voice myvoice -o ./registered.wav
colabllm qwen3-tts ls-drive 'voices' --json
```

仅使用获准使用的音频。`--ref-audio` 会选择 Base 克隆任务；省略转写文本时使用说话人嵌入模式，
`--x-vector-only` 可显式选择；完整参考文本可用于 ICL。参考音频支持本机、`drive://` 或服务支持的 URL。
`--ref-drive` 可将参数按 Drive 路径解释。
删除注册音色使用 `delete-voice NAME`，只对用户明确要删除的名称执行。

## 参数、存储与失败恢复

`up --variant`、`--model`、`--preload-all`、`--stage0-gpu-mem`、`--stage1-gpu-mem`、`--max-model-len`、
`--max-seqs` 是下次启动参数；不是当前 `tts` 请求的热切换命令。CustomVoice/VoiceDesign/Base 任务与已加载模型能力须匹配。
模型 API 入口包括 `POST /v1/audio/speech`、`GET /v1/audio/voices` 及音色管理接口。

`--save-drive` 另存到 `MyDrive/colab-llm/qwen3-tts/outputs/`，也可提供目录；实际路径以服务响应/诊断为准。
注册音色和元数据位于服务的 Drive `voices/`；模型权重在本地 SSD，编译缓存分 GPU/环境归档到 Drive。
缓存只节省编译和传输，不能保证新 VM 免安装、免加载。
`--timeout` 默认 300 秒，是请求限时，不是可恢复任务 ID。超时后先检查结果，避免盲目重复生成。

## 多实例与管理

```bash
colabllm qwen3-tts up -i 2
colabllm qwen3-tts tts '测试第二个实例。' --voice serena -i 2 -o ./instance2.wav
colabllm qwen3-tts logs -i 2 -n 100
colabllm qwen3-tts down -i 2
```

跨机器调用使用 `--url` 及 `QWEN3_TTS_API_TOKEN`，或 `QWEN3_TTS_URL` / `--api-token`；切换来源必须显式给 Token。

## 启动缓存优化

Qwen3-TTS 与 IndexTTS 共用归档缓存实现，但缓存数据保存在各自的服务目录中。以 L4 为例：

```text
我的云端硬盘/colab-llm/qwen3-tts/
  cache-archives/vllm-0.32.0-py312-sm89/
    flashinfer-cache.tar
    triton-cache.tar
    vllm-cache.tar
```

启动时先读取单个归档，再在本地 SSD 解包；推理编译仍使用本地 SSD。预热后重新打包新增缓存，上传临时文件成功后才替换旧归档。内容未变时不重复上传，同一 VM 的归档已恢复时不重复读取。

首次兼容旧版网盘缓存时仍需逐文件迁移，之后自动生成归档。恢复、迁移和保存每 10 秒输出进度。归档损坏时尝试旧目录回退；缓存恢复或保存失败会警告，服务继续使用本地缓存或按需编译。

不同 vLLM 版本和 GPU 架构分目录。相同架构的并发实例以最后成功上传的归档为准，缺少的缓存条目可重新生成。已注册音色及生成音频继续作为普通网盘文件保存。

此优化减少缓存传输耗时，不缓存 Python 虚拟环境和模型权重。新 VM 仍需安装 vLLM 环境、辅助 Qwen3-TTS 环境，下载模型并加载/预热。新版逻辑在下一次 `up` 时生效。
