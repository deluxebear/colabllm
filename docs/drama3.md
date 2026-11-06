# Fish Audio Drama 3：多参考克隆、情绪事件标签与小说长文朗读

默认 **G4**（96 GB VRAM，加载 `fishaudio/drama-3` BF16 39 GB 权重；显存小于 72 GiB 的机型或 `--variant int4` 自动切换 `fishaudio/drama-3-int4`），基于 `sglang-omni[fishaudio]==0.2.0` 三阶段流水线（`preprocessing` → `tts_engine` → `vocoder`），输出 **44.1 kHz** 单声道音频，原生 Bearer API Key 认证。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与单句合成

```bash
colabllm drama3 up
colabllm drama3 up --gpu H100
colabllm drama3 up --gpu A100 --variant int4
colabllm drama3 status --json
colabllm drama3 voices --json
colabllm drama3 tts '今天天气真好，[sighs] 终于可以安静读一会儿书了。' --voice alloy -o ./speech.wav
colabllm drama3 tts '流式合成测试，边生成边接收音频。' --voice alloy --stream -o ./stream.wav
```

启动完成后默认预注册参考音色 `alloy`；未传 `--voice` 且未传 `--ref-audio` 时由模型自由采样音色。文本中可直接嵌入英文方括号语气/副语言标签（如 `[sighs]`、`[laughs]`、`[whispers]`、`[gasp]`、`[pause]`）引导停顿与情绪表现。

## 单参考/多参考音色克隆与持久音色

```bash
colabllm drama3 tts '使用单段本地参考音频进行零样本克隆。' --ref-audio ./ref1.wav --ref-text '这是第一段参考音频对应的文字。' -o ./clone1.wav
colabllm drama3 tts '融合多段参考音频的音色与演绎风格。' --ref-audio ./calm.wav --ref-text '平静叙述的参考句。' --ref-audio ./excited.wav --ref-text '情绪激昂的参考句。' -o ./multi-ref.wav
colabllm drama3 tts '直接读取 Google Drive 上的参考音频。' --ref-audio 'drive://voices/narrator.wav' -o ./drive-clone.wav
colabllm drama3 upload-voice --name narrator --audio ./narrator.wav --ref-text '参考音频的准确逐字稿。' --description '有声书主旁白'
colabllm drama3 upload-voice --name heroine --drive 'drive://voices/heroine.wav' --ref-text '角色音色参考文本。'
colabllm drama3 voices --json
colabllm drama3 delete-voice heroine
colabllm drama3 ls-drive 'voices' --json
```

`--ref-audio` 可重复传入以构造 `references` 多参考克隆列表；`--ref-text` 按相同顺序一一对应（建议提供准确转写文本以提升起音稳定性与韵律还原度）。通过 `upload-voice` 注册的命名音色保存在本地 SSD `speakers/*.safetensors` 并自动同步至 `MyDrive/colab-llm/drama3/voices/`，后续重启实例可自动恢复，且免去每次请求重复上传与编码参考音频的开销。

## 小说与长篇文本有声书生成（`narrate`）

Drama 3 单次请求最大生成长度为 **2048 帧（约 95.1 秒音频）**，直接将整章长文本塞入单次请求会在约 95 秒处截断。长篇小说朗读请始终使用内置的 `narrate` 命令：

```bash
colabllm drama3 narrate ./novel.txt --voice narrator -O ./novel-audiobook
colabllm drama3 narrate ./novel.txt --voice narrator --chapters '1-5' --batch-size 8 --max-chars 180 --format mp3 -O ./novel-mp3
colabllm drama3 narrate ./novel.md --voice narrator --cast ./cast.json --turn-pause-ms 180 --pause-ms 280 --para-pause-ms 650 -O ./novel-cast
colabllm drama3 narrate ./novel.txt --cast ./cast.json --llm-service qwen-flash --llm-tags -O ./novel-drama
```

`narrate` 自动完成整书生产流水线：

1. **自动分章与句子切分**：自动识别中英文常见章节标题（`第X章/节/回/卷`、`楔子`、`尾声`、`Chapter N`、Markdown `#` 标题；传 `--single-chapter` 可整篇合为一章），并在段落内按句号/问号/感叹号/分号贪心打包为不超过 `--max-chars`（默认 `180` 字，约 35–45 秒语音，远低于 95 秒硬上限）的自然句块。
2. **多角色对白自动切分与说话人归属**：传入 `--cast`（或显式开启 `--split-dialogue`）时，自动将同一自然段内混排的叙述文字与引号对白（如 `林晚看着陈默，轻声问道：“你等很久了吧？”陈默抬头笑道：“刚泡好茶。”`）拆分为独立的旁白句与角色台词句（同时保留 `所谓的“规矩”` 这类行内强调引号不误切），支持前置提示语、后置提示语、打断式插话（`“快进来，”陈默笑道，“外面风大。”`）、角色别名匹配以及双人连续无提示语对白自动轮换，并在叙述过渡到台词处使用更紧凑的 `--turn-pause-ms`（默认 `180 ms`）。
3. **可选 LLM 剧本预标注（`--llm-service` / `--llm-url`）**：对省略姓名、多人混战或含大量代词指代的复杂小说段落，可指定已运行的 `colabllm` 文本模型服务（如 `--llm-service qwen-flash`、`--llm-service deepseek-coder`）或任意 OpenAI 兼容接口（`--llm-url` + `--llm-token` / `--llm-model`），在合成前按窗口将原文转换为 `[role:旁白]` / `[role:角色名]` 剧本并缓存在 `<output-dir>/annotated/<chapter>.txt`；内置逐字骨架校验确保不漏字、不改词（校验不通过的窗口自动回退规则切分），加 `--llm-tags` 还可让模型在情绪明显处少量插入 `[sighs]`、`[laughs]`、`[gasp]`。
4. **动态帧上限与句尾静音裁剪**：根据每块字数、语速、标点与语气标签自动估算 `max_new_tokens`，并在本地对每块 44.1 kHz WAV 执行句尾异常静音检测与裁剪（`--no-trim-silence` 可关闭），避免自回归生成偶发的句尾拖长静音。
5. **高吞吐批量并发**：默认以 `--batch-size 8` 调用服务端 `POST /v1/audio/speech/batch`（同一 batch 内不同句块可各自携带独立的角色 `voice`、`speed` 或 `ref_audio`；G4 GPU 实测 8 并发综合吞吐约 **22x 实时速度**，10 万字小说约 15–20 分钟即可合成完毕），单条失败自动降级重试。
6. **断点续跑与章节拼接**：每个句块先写入 `<output-dir>/chunks/<chapter>/chunk_XXXX.wav` 并更新 `<output-dir>/manifest.json`（含各章节音色分块统计 `voice_chunks`）；中途网络中断或暂停后重新执行同一命令即可从断点继续，已完成的章节与句块自动跳过（传 `--force` 强制重跑；传 `--clean-chunks` 在每章拼接完成后清理分块文件）。句间默认插入 `280 ms` 停顿、角色转换插入 `180 ms` 停顿、段间插入 `650 ms` 停顿。

角色配音映射 `--cast cast.json` 支持字符串音色名，也支持按角色独立配置 `voice`、`aliases`（别名/昵称）、`speed` 或零样本 `ref_audio`/`ref_text`（正文中同样可随时嵌入 `[role:林晚]` 显式指定说话人）：

```json
{
  "narrator": "narrator",
  "dialogue": "heroine",
  "characters": {
    "林晚": {
      "voice": "heroine",
      "aliases": ["小晚", "林小姐"],
      "speed": 1.02
    },
    "陈默": {
      "voice": "male_lead",
      "aliases": ["老陈"],
      "speed": 0.96
    }
  }
}
```

## 模型限制、输出与启动参数

- **单说话人限制**：`sglang-omni` 当前的 Drama 3 实现仅支持单说话人生成（`<|speaker:0|>`），不支持在同一条请求内使用 `<|speaker:1|>`…`<|speaker:15|>` 多说话人交替标签（服务端会返回 HTTP 400）；CLI 会自动清理不支持的 `<|speaker:1..|>` 标签，多角色有声书请使用分段切换音色（`--cast`）。
- **不支持的通用 TTS 字段**：Drama 3 流水线不接受 `seed`、`language`、`instructions` 及不等于 `1.0` 的 `repetition_penalty`；内置中间件会自动剥离通用客户端附带的 `seed/language/instructions` 字段并将模型别名（`drama3`、`tts-1` 等）映射为当前加载模型。
- **输出格式与网盘持久化**：`tts --format` 支持 `wav`、`mp3`、`flac`、`pcm`、`aac`、`opus`；`narrate --format` 支持 `wav`（默认，纯标准库拼接无额外依赖）及 `mp3`、`flac`、`opus`（拼接后调用本机 `ffmpeg` 转码）。`tts --save-drive` 可同时将输出音频写入 `MyDrive/colab-llm/drama3/outputs/`。
- **连接与启动覆盖**：通过 `--url` / `DRAMA3_URL` 与 `--api-token` / `DRAMA3_API_TOKEN` 覆盖目标服务与凭据。`up` 支持 `--variant auto|bf16|int4`、`--model`、`--alias`、`--engine-gpu-mem`、`--vocoder-gpu-mem`、`--max-running-reqs`、`--context-length`、`--batch-max-items`、`--max-ref-sec`，仅作用于下次启动。

```bash
colabllm drama3 up -i 2
colabllm drama3 tts '第二实例连通性验证。' --voice alloy -i 2 -o ./instance2.wav
colabllm drama3 down -i 2
```

## 启动与编译缓存

模型权重（`/content/drama3/models/`）与 S2-Pro 编解码器（`/content/drama3/codec/codec.pth`）始终下载在 Colab 本地 NVMe SSD（`HF_XET_CHUNK_CACHE_SIZE_BYTES=0`），避免 FUSE 网盘小文件读取瓶颈与临时缓存撑满磁盘。FlashInfer、Triton 与 TorchInductor 编译缓存同样运行在本地 SSD，并在预热完成后按环境与 GPU 架构打包归档至网盘：

```text
我的云端硬盘/colab-llm/drama3/
  voices/
  outputs/
  cache-archives/sglomni-0.2.0-py312-bf16-sm120/
    flashinfer-cache.tar
    triton-cache.tar
    inductor-cache.tar
```
