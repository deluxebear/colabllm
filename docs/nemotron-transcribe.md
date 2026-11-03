# NVIDIA-Nemotron-3.5-Transcribe-30B-A4B (G4 / vLLM + NeMo Speech) 运行时使用说明

> Tunnel 域名说明：以下固定域名示例需先设置主域名，例如 `colabllm tunnel-config --base-domain example.com`（所有服务共享）。未配置时使用动态 URL；`up --base-domain example.com` 可单次覆盖，`up --quick-tunnel` 强制动态。详见 [Tunnel 配置](../README.md#cloudflare-tunnel-配置)。

对应文件：
- 执行脚本：[src/colabllm/services/nemotron-transcribe.py](services/nemotron-transcribe.py)
- 远端 Notebook：[src/colabllm/services/nemotron-transcribe.ipynb](services/nemotron-transcribe.ipynb)（由 [templates/build_notebooks.py](templates/build_notebooks.py) 生成）

---

## 1. 服务特性与架构配置

| 配置项 | 默认值 / 说明 |
| --- | --- |
| **模型仓库** | [`nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B`](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B)（默认 BF16 全精度 ~67.5 GB；支持 `--precision fp8` ~34.7 GB 或 `--precision nvfp4` ~21.6 GB） |
| **模型架构** | Mamba-2–Transformer Hybrid MoE 语音大模型（总参数 ~34B，每步激活 ~4.3B）+ Parallel Expert Encoder（ASR 编码器 + Sortformer 说话人编码器）+ CTC 词级时间戳对齐头 |
| **硬件要求** | **Colab G4 GPU**（NVIDIA RTX PRO 6000 Blackwell `sm_120`，~96 GB VRAM，~176 GB RAM），默认不加 `--high-mem` |
| **上下文与音频长度** | **65,536 tokens** 上下文窗口，单次原生跨度 **1 小时（3600 秒）**；对于 **超过 40 分钟 / 超过 1 小时（如 2~5 小时播客/长视频/会议）** 的音视频，内置 **基于 `ffprobe` + `ffmpeg silencedetect` 的静音对齐自动切片与时间戳无缝拼接**（`--chunk-minutes 40`），支持 **40 种语言**（含中文、英文、日文、韩文、粤语/多语种混说 Code-Switching） |
| **推理引擎与加速** | `vllm serve` (0.32.0) + `NVIDIA-NeMo/Speech` (`VLLM_PLUGINS=nemo_speechlm`)，默认开启 **MTP8 投机解码**（`--mtp 8`）与 `ctc_timestamp_middleware` |
| **存储布局** | **模型权重（67.5 GB）直接高速下载至 Colab 本地 NVMe SSD**（`/content/nemotron-transcribe/model`，自动清理 `hf_xet` 临时缓存，绝不占用 Google Drive 空间）；FlashInfer 编译内核缓存与转写输出持久化在 Google Drive（`/content/drive/MyDrive/colab-llm/nemotron-transcribe`） |
| **安全认证** | Bearer Token 认证（`auth="builtin"`）：未授权访问任何接口均返回 `401 Unauthorized` |
| **默认固定域名** | `https://transcribe.example.com`（OpenAI 兼容 Base URL：`https://transcribe.example.com/v1`） |

---

## 2. 启动与日常管理命令

```bash
# 1. 启动服务并绑定默认域名 https://transcribe.example.com（默认 G4 GPU + BF16 权重）
colabllm nemotron-transcribe up --domain

# 2. 可选：使用 FP8 或 NVFP4 量化权重启动（体积更小、吞吐更高）
colabllm nemotron-transcribe up --domain --precision fp8
colabllm nemotron-transcribe up --domain --precision nvfp4

# 3. 使用随机临时域名 (*.trycloudflare.com) 启动
colabllm nemotron-transcribe up --quick-tunnel

# 4. 查看访问 URL 与 Bearer API Token
colabllm nemotron-transcribe info

# 5. 查看实时准备进度（NeMo Speech + vLLM 环境安装 / 模型下载 / FlashInfer 编译预热）
colabllm nemotron-transcribe logs -f

# 6. 检查 Colab 会话状态与公网认证连通性探测
colabllm nemotron-transcribe status

# 7. 停止服务并释放 G4 实例
colabllm nemotron-transcribe down
```

### 2.1 多实例部署与管理 (`-i / --instance` & `ps`)

所有命令均支持 `-i <ID>`（或 `--instance <ID>`）参数来创建和管理多个相互独立的 Colab G4 实例（每个实例拥有独立的 Colab 会话、独立的状态目录 `~/.colabllm/services/nemotron-transcribe-<ID>`、独立的 Cloudflare 隧道与子域名 `transcribe-<ID>.example.com`，同时共享 Google Drive 上的 `flashinfer-cache` 与音频目录）：

```bash
# 启动多个并行 G4 实例（自动映射为 transcribe-1.example.com, transcribe-2.example.com ...）
colabllm nemotron-transcribe up -i 1 --domain
colabllm nemotron-transcribe up -i 2 --domain

# 查看所有实例的运行状态、URL 与 API Token
colabllm nemotron-transcribe ps

# 查看或管理指定实例
colabllm nemotron-transcribe info -i 2
colabllm nemotron-transcribe logs -f -i 2
colabllm nemotron-transcribe status -i 2

# 停止指定实例，或一次性停止全部实例 (--all)
colabllm nemotron-transcribe down -i 2
colabllm nemotron-transcribe down --all
```

---

## 3. 通过脚本（命令）调用网盘音频文件与 Prompt（支持单文件与批量）

由于 Google Drive 已直接挂载在 Colab VM 的 `/content/drive/MyDrive` 下，`transcribe` 命令会**让 Colab 实例直接在远端本地读取网盘中的音频文件**（无需先把网盘大音频下载到本地再上传，也不受 Cloudflare 100 MB 请求体大小限制，长音频和 `.mp4`/`.mkv` 视频容器还会自动通过 `ffmpeg` 处理）。

### 3.1 浏览网盘上的音频文件 (`ls-drive`) 与查看内置 Prompt 预设 (`prompts`)

```bash
# 列出 Google Drive 上的音频文件（支持子目录或通配符）
colabllm nemotron-transcribe ls-drive
colabllm nemotron-transcribe ls-drive "meetings/*.wav"
colabllm nemotron-transcribe ls-drive "colab-llm/nemotron-transcribe/audio"

# 查看内置的 11 种 NVIDIA 官方推荐 Prompt 预设模板
colabllm nemotron-transcribe prompts
```

### 3.2 单个网盘音频文件 + 自定义 Prompt

```bash
# 指定网盘路径（相对于 /content/drive/MyDrive/ 或完整路径）和自定义 Prompt
colabllm nemotron-transcribe transcribe "audio/meeting01.wav" \
  --prompt "Audio transcription. Language: as spoken. Wording: verbatim, including fillers and repetitions. Formatting: punctuation and capitalization. Output: transcript only."

# 将结果同时保存回 Google Drive（同目录生成 .txt / .nemotron.json）并保存到本地文件
colabllm nemotron-transcribe transcribe "audio/meeting01.wav" \
  --preset clean \
  --save-drive \
  -o ./meeting01.txt
```

### 3.3 说话人分离 + 词级时间戳 + 自动导出 `.srt` 字幕 (`--preset speaker --timestamps`)

```bash
colabllm nemotron-transcribe transcribe "audio/panel_discussion.mp3" \
  --preset speaker --timestamps \
  --save-drive "audio/transcripts" \
  -O ./outputs
```
- 开启 `--timestamps`（或 `--preset speaker`）后，服务端通过 CTC 对齐头返回每个词的精确起止秒数（`start`/`end`）、说话人标签（`<spk:0>`, `<spk:1>`...）与 `diarization` 区间，并自动生成 `.srt` 字幕文件。

### 3.4 语音翻译、领域词汇偏置与音频理解问答 (`--preset translate / context / qa`)

```bash
# 1. 将外语录音直接翻译为中文
colabllm nemotron-transcribe transcribe "audio/keynote_en.m4a" \
  --preset translate --lang Chinese

# 2. 传入专有名词 / 术语表提升识别准确率
colabllm nemotron-transcribe transcribe "audio/earnings_call.wav" \
  --preset context --context "Nemotron, Blackwell, RTX PRO 6000, vLLM, FlashInfer"

# 3. 音频理解与问答（开启推理思考模式 --reasoning）
colabllm nemotron-transcribe transcribe "audio/interview.wav" \
  --prompt "请根据这段录音总结受访者提到的三个核心观点，并列出提到的所有公司名称。" \
  --reasoning
```

### 3.5 批量处理网盘目录 / 通配符 / 任务清单文件（支持并发 `-j`）

```bash
# 方式 A：批量转写网盘某个目录或通配符匹配的音频（并发数 2，结果写回网盘指定目录并存本地 ./out）
colabllm nemotron-transcribe transcribe "meetings/*.mp3" \
  --prompt "Audio transcription. Language: as spoken. Wording: omit fillers and accidental repetitions; keep the remaining spoken wording unchanged. Formatting: punctuation and capitalization. Output: transcript only." \
  --save-drive "meetings/transcripts" \
  -O ./out \
  -j 2

# 方式 B：直接传入网盘文件夹路径（默认递归扫描里面所有 .wav/.mp3/.flac/.m4a/.ogg/.mp4 等）
colabllm nemotron-transcribe transcribe "meetings/" \
  --preset verbatim \
  --save-drive \
  -O ./out

# 方式 C：通过批量任务清单文件 (--batch-file)，为不同音频指定不同的 Prompt / 模式
# 支持 .jsonl / .json / .csv / .tsv / .txt
colabllm nemotron-transcribe transcribe --batch-file tasks.jsonl \
  --save-drive "meetings/outputs" \
  -O ./out \
  -j 2
```

`tasks.jsonl` 示例（每行可独立配置 `audio` 或 `url`、`prompt`、`preset`、`reasoning`、`timestamps`、`context`、`lang`）：
```jsonl
{"audio": "meetings/call_en.wav", "preset": "speaker", "timestamps": true}
{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "download_dir": "video/voices", "preset": "verbatim"}
{"url": "https://x.com/user/status/1234567890", "preset": "translate", "lang": "Chinese"}
{"audio": "meetings/podcast.mp3", "prompt": "Summarize the key decisions in bullet points.", "reasoning": true}
{"audio": "meetings/tech_talk.m4a", "preset": "context", "context": "CUDA, Triton, Mamba-2, Sortformer"}
```

### 3.6 直接传入多媒体 URL（YouTube、Twitter/X、Bilibili、TikTok 等，`yt-dlp` 自动下载到网盘后识别）

当向 `transcribe` 传入任何 `http://`、`https://` 或 `ytdl://` 多媒体链接时，系统会自动使用 `yt-dlp` 下载提取音频并保存到 Google Drive 网盘（默认目录 `/content/drive/MyDrive/colab-llm/nemotron-transcribe/audio/yt-dlp`，或通过 `--download-dir` 指定网盘目录），然后直接在 Colab G4 GPU 上读取该网盘音频文件进行转写（若同一视频已在网盘缓存则直接复用，加 `--force-download` 可强制重下；若远端 Colab 数据中心 IP 遇到站点风控，还会自动回退到本机 `yt-dlp` 下载并通过 `colab upload` 上传至网盘后再识别）：

```bash
# 1. 直接转写 YouTube / Twitter(X) / Bilibili 视频或播客链接（自动下载到网盘再转写）
colabllm nemotron-transcribe transcribe "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --preset verbatim

# 2. 指定网盘保存目录 (--download-dir)，并开启说话人分离 + 词级时间戳 + 字幕保存回网盘 (--save-drive)
colabllm nemotron-transcribe transcribe "https://x.com/OpenAI/status/1234567890" \
  --download-dir "video/voices" \
  --preset speaker --timestamps \
  --save-drive

# 2b. 下载并保留完整视频文件 (.mp4) 到网盘 (--download-video，默认不加该参数时仅提取纯音频)
colabllm nemotron-transcribe transcribe "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --download-video \
  --download-dir "video/voices" \
  --preset verbatim

# 2c. 仅临时下载识别，不将音视频文件保留在 Google Drive 网盘上 (--no-save-media-drive)
colabllm nemotron-transcribe transcribe "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --no-save-media-drive \
  --preset verbatim

# 3. 批量转写整个 YouTube 播放列表 (--playlist) 或多条链接
colabllm nemotron-transcribe transcribe "https://www.youtube.com/playlist?list=PLxxxx" \
  --playlist \
  --download-dir "video/voices" \
  --preset clean \
  --save-drive

# 4. 如果视频需要登录 Cookie：
#    - 方式 A：使用网盘上的 cookies.txt（远端 Colab 直接下载）
colabllm nemotron-transcribe transcribe "https://www.youtube.com/watch?v=xxxx" \
  --cookies-drive "colab-llm/cookies.txt" --preset verbatim
#    - 方式 B：使用本地浏览器 Cookie 或本地 cookies.txt（本机 yt-dlp 下载后自动上传网盘并转写）
colabllm nemotron-transcribe transcribe "https://www.youtube.com/watch?v=xxxx" \
  --cookies-from-browser chrome --download-dir "video/voices" --preset verbatim

# 5. 仅使用 yt-dlp 将多媒体 URL 下载提取音频到 Google Drive 网盘（不立即转写）
colabllm nemotron-transcribe download "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --download-dir "video/voices"
```

---

## 4. 远端 API 调用示例

先通过 `colabllm nemotron-transcribe info` 获取服务地址（如 `https://transcribe.example.com`）和 `API_TOKEN`。

### 4.1 OpenAI Python SDK 标准调用（上传本地音频 Base64）

```python
import base64
from pathlib import Path
from openai import OpenAI

client = OpenAI(
    base_url="https://transcribe.example.com/v1",
    api_key="sk-...",  # colabllm nemotron-transcribe info 查看
)

audio_b64 = base64.b64encode(Path("sample.wav").read_bytes()).decode("ascii")

response = client.chat.completions.create(
    model="nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B",  # 或传简称 "nemotron-transcribe"
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Audio transcription. Language: as spoken. Wording: verbatim, including fillers "
                        "and repetitions. Formatting: punctuation and capitalization. Output: transcript only."
                    ),
                },
                {
                    "type": "audio_url",
                    "audio_url": {"url": f"data:audio/wav;base64,{audio_b64}"},
                },
            ],
        }
    ],
    temperature=0,
    max_tokens=16384,
    extra_body={
        "chat_template_kwargs": {"enable_thinking": False},
        "skip_special_tokens": False,
        "spaces_between_special_tokens": False,
        "seed": 0,
    },
)
print(response.choices[0].message.content)
```

### 4.2 OpenAI SDK 直接引用 Google Drive 网盘音频 (`drive://...`)

无需在客户端读取和上传音频文件，直接在 `audio_url.url` 中传入 `drive://<网盘路径>`，服务端中间件会自动从 Colab 实例的 `/content/drive/MyDrive/` 读取：

```python
response = client.chat.completions.create(
    model="nemotron-transcribe",
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Transcribe this audio with punctuation and capitalization. Include all spoken words "
                        "and verbal fillers. Write numbers in their standard written form. "
                        "Put speaker tags <spk:0> <spk:1> <spk:2> <spk:3> on the transcription."
                    ),
                },
                {
                    "type": "audio_url",
                    "audio_url": {"url": "drive://meetings/call01.wav"},
                },
            ],
        }
    ],
    temperature=0,
    extra_body={
        "chat_template_kwargs": {"enable_thinking": False},
        "skip_special_tokens": False,
        "mm_processor_kwargs": {"capture_ctc_timestamps": True},  # 开启词级时间戳与说话人分离
    },
)
print("Transcript:", response.choices[0].message.content)
print("CTC Timestamps:", response.model_dump().get("ctc_timestamps"))
```

### 4.3 专用网盘与多媒体 URL 转写 REST 接口 (`/api/ct/drive-transcribe` & `/api/ct/ytdlp-download`)

```bash
# 1. 调用网盘音频转写（支持 preset / prompt / timestamps / reasoning / save_to_drive）
curl -s "https://transcribe.example.com/api/ct/drive-transcribe" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "drive_path": "meetings/call01.wav",
    "preset": "speaker",
    "timestamps": true,
    "save_to_drive": true,
    "save_drive_dir": "meetings/transcripts"
  }' | jq

# 2. 直接传入多媒体 URL（YouTube / Twitter 等）：自动用 yt-dlp 下载到网盘再转写
curl -s "https://transcribe.example.com/api/ct/drive-transcribe" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "download_dir": "video/voices",
    "preset": "verbatim",
    "save_to_drive": true
  }' | jq
```
