<!-- Generated from docs/indextts.md; edit the source and run tools/sync_runtime_docs.py. -->

# IndexTTS-2.5：克隆、情绪和语速控制

默认 **L4**，vLLM-Omni 两阶段语音合成，原生 Bearer 认证；支持参考音色、注册音色、多语言及情绪控制。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与基础合成

```bash
colabllm indextts up
colabllm indextts status --json
colabllm indextts voices --json
colabllm indextts tts '你好，这是语音合成测试。' -o ./speech.wav
colabllm indextts tts --text-file ./script.txt --voice default --lang zh -o ./narration.wav
```

不传 `--voice/--ref-audio` 时使用服务默认音色 `default`。`--ref-audio` 优先于指定音色名。
文本可用位置参数、`--text` 或 UTF-8 文本文件。长文本应控制长度和生成 token 上限。

## 克隆与持久音色

```bash
colabllm indextts tts '用参考音色说这句话。' --ref-audio ./reference.wav -o ./clone.wav
colabllm indextts tts '直接使用网盘参考音频。' --ref-audio 'drive://voices/ref.wav' -o ./drive-clone.wav
colabllm indextts upload-voice --name narrator --audio ./reference.wav --description '获准使用的旁白参考'
colabllm indextts upload-voice --name drivevoice --drive 'drive://voices/ref.wav'
colabllm indextts voices --json
colabllm indextts tts '继续生成旁白。' --voice narrator -o ./registered.wav
colabllm indextts ls-drive 'voices' --json
```

注册前检查名称，删除使用 `delete-voice NAME`。本机参考音频会上传；Drive 输入由远端读取。
参考音频可用服务支持的 URL，或通过 `--ref-drive` 强制作为 Drive 路径解释。
音色注册、删除是持久变更，不是临时生成参数。

## 情绪、语言与速度

```bash
colabllm indextts tts '太让人惊喜了！' --voice narrator --emo-preset happy --emo-alpha 0.8 -o ./happy.wav
colabllm indextts tts '故事即将开始。' --voice narrator --emo-text '温和且略带期待' -o ./emotion.wav
colabllm indextts tts '请慢慢听我说。' --voice narrator --speed 0.8 --lang zh -o ./slow.wav
```

速度原生范围 0.5–2.0；语言为 `zh/en/zhen/ja/es/ar/yue`，省略时 CLI 根据中英文字符自动选择。
`--emo-preset` 有 happy、angry、sad、afraid、disgusted、melancholic、surprised、calm。
`--emo-vector` 顺序为上述八维，`--emo-alpha` 控制融合（0–1）；提供情绪但不指定融合时默认 0.6。
可用 `--emo-audio` / `--emo-audio-drive` 指定情绪参考。建议一次选择一种主要情绪来源，避免混合参数的优先级不清。
`--no-text-norm` 关闭文本归一化，`--use-random` 开启随机采样，`--seed` 可用于复现实验。

## 输出、认证及启动配置

`-o` 是本机文件；缺省输出为当前目录带时间戳的 WAV。`--format` 支持 WAV、MP3、FLAC、Opus、PCM，
注意文件扩展名要与格式一致。`--save-drive` 另存服务端输出，返回路径优先于旧版帮助中的目录示例。
当前 notebook 使用 `MyDrive/colab-llm/indextts/` 持久保存音色/输出；权重在 SSD，编译缓存按 GPU 和环境归档到 Drive。
`--timeout` 默认 300 秒，是 HTTP 请求限时；TTS 不返回 OCR/Avatar 那样的持久 job ID。

通过 `--url` / `INDEXTTS_URL` 选择服务，`--api-token` / `INDEXTTS_API_TOKEN` 提供显式凭据。
API 主要为 `POST /v1/audio/speech`、`GET /v1/audio/voices` 及注册/删除音色接口。
`up --model/--alias/--stage0-gpu-mem/--stage1-gpu-mem/--max-model-len/--max-seqs` 是下一次启动配置。
先看 `up --help`，不要把它们误用为运行中模型的重配置。

```bash
colabllm indextts up -i 2
colabllm indextts tts '测试第二实例。' --voice default -i 2 -o ./instance2.wav
colabllm indextts logs -i 2 -n 100
colabllm indextts down -i 2
```

## 启动与编译缓存

Setup 依次恢复缓存和已注册音色，再并行准备 Python 环境与模型。服务启动后加载两阶段模型并预热；JIT 缓存可命中时复用，否则按需生成。

编译缓存始终在本地 SSD 上运行，网盘只保存归档。以 L4 为例：

```text
我的云端硬盘/colab-llm/indextts/
  cache-archives/vllm-0.32.0-py312-sm89/
    flashinfer-cache.tar
    triton-cache.tar
    vllm-cache.tar
```

- 启动时每类缓存只从网盘读取一个 tar 文件，然后在本地解包，减少网盘逐文件操作。
- 旧版 `flashinfer-cache/`、`triton-cache/`、`vllm-cache/` 首次仍需迁移，之后自动保存归档；第一次迁移仍可能较慢。旧目录保留供回退。
- 每 10 秒输出恢复、迁移或保存进度；同一 VM 已恢复过的归档不重复读取，内容未变化时不重复上传。
- 预热后在本地打包，再上传临时文件并替换正式归档；上传失败保留旧归档。缓存错误会警告，服务继续使用本地缓存或重新生成。
- 不同 vLLM 版本和 GPU 架构使用不同目录。相同架构的并发实例最后成功保存的归档生效；缺少的条目可重新编译。
- 已注册音色和生成音频继续作为普通网盘文件保存，不放入编译缓存归档。

归档优化只减少编译缓存恢复/保存的耗时，不消除新 VM 的依赖安装、模型下载、模型加载以及必要的编译预热。当前运行中的旧版 notebook 不会热替换；新版恢复逻辑在下次 `up` 时生效。

验证记录：在真实 L4 Colab 会话中，将 577 个 Triton 文件（约 54 MB 数据）保存为约 55 MB tar，随后从网盘读取到独立本地目录并逐文件校验 SHA-256，全部一致。此次读取、解包和校验约 0.65 秒；测试发生在同一会话、归档刚写入之后，属于热缓存验证，不代表新 VM 的冷启动耗时。FlashInfer 和 vLLM 归档也已保存。
