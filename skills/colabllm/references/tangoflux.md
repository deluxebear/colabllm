<!-- Generated from docs/tangoflux.md; edit the source and run tools/sync_runtime_docs.py. -->

# TangoFlux：44.1 kHz 立体声音效与文本生音频

默认 **T4**（16 GB VRAM 即可流畅运行，模型总显存占用约 4.3 GB；传 `--gpu L4` 或 `--gpu G4` 生成更快），默认加载经过 CRPO 偏好对齐的 `declare-lab/TangoFlux`（515M FluxTransformer + `google/flan-t5-large` 文本编码器 + `AutoencoderOobleck` 立体声 VAE；传 `--variant base` 可切换 `declare-lab/TangoFlux-base`），支持生成 **1–30 秒、44.1 kHz 双声道立体声** 音效（SFX）、环境氛围音、拟音（Foley）与背景音乐片段，原生 Bearer API Key 认证。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与单条音效生成

```bash
colabllm tangoflux up
colabllm tangoflux up --gpu L4
colabllm tangoflux up --variant base
colabllm tangoflux status --json
colabllm tangoflux models --json
colabllm tangoflux generate 'Hammer slowly hitting the wooden table' -d 10 -s 50 -o ./hammer.wav
colabllm tangoflux sfx 'Heavy rain and distant thunder against a cabin window' -d 15 --guidance-scale 4.5 --seed 42 -o ./rain.wav
colabllm tangoflux generate 'Futuristic spacecraft warp engine powering up' -d 8 -n 3 --format mp3 --save-drive -o ./warp.mp3
colabllm tangoflux ls-drive --json
```

`generate` 与 `sfx` 完全等价：

- **提示词建议**：由于底层文本编码器为 `google/flan-t5-large` 且训练语料以英文声学描述为主，建议使用具体、有层次的英文声学描述词（如声源材质、动作节奏、空间混响与远近层次：`'Crisp footsteps walking slowly on dry autumn leaves in a quiet forest'`）。
- **核心采样参数**：`-d` / `--duration` 控制音频时长（`1.0`–`30.0` 秒，默认 `10` 秒）；`-s` / `--steps` 控制整流流匹配（Rectified Flow）步数（默认 `50` 步，追求速度可设为 `25` 步）；`-g` / `--guidance-scale` 控制分类器自由引导强度（默认 `4.5`）；`--seed` 固定随机种子以便精确复现。
- **一次生成多条候选（`-n`）**：传入 `-n 2`…`4` 可在单次请求内并行采样多条不同变体，自动按 `<stem>_01.<ext>`、`<stem>_02.<ext>` 保存到本地。
- **输出格式与网盘保存**：`--format` 支持 `wav`（默认 44.1 kHz 16-bit 立体声 PCM）、`mp3`、`flac`、`ogg`、`opus`、`aac`。加 `--save-drive`（或 `--save-drive 'sfx/rain.wav'`）在返回音频的同时写入 `MyDrive/colab-llm/tangoflux/outputs/`。

## 批量音效包生成与断点续跑（`batch`）

为有声书后期、视频剪辑或游戏制作整套音效包时，可将多条音效写入 `.txt`（每行一条提示词，或 `名称 | 提示词`）、`.json` 或 `.jsonl` 清单文件后用 `batch` 批量生成：

```bash
colabllm tangoflux batch ./cues.txt -d 8 -s 50 -O ./audiobook-sfx
colabllm tangoflux batch ./cues.json --batch-size 4 --format mp3 --save-drive -O ./game-sfx
colabllm tangoflux batch ./cues.json -O ./game-sfx --force --json
```

`cues.json` 示例（单条未指定的 `duration` / `steps` / `guidance_scale` 自动继承命令行默认值）：

```json
[
  {
    "name": "teahouse_fire",
    "prompt": "Warm crackling charcoal fire in a quiet wooden room",
    "duration": 15,
    "steps": 50,
    "seed": 101
  },
  {
    "name": "wooden_door_snow",
    "prompt": "Creaking old wooden door opening with howling winter wind outside",
    "duration": 6
  }
]
```

每批生成完成后实时更新 `<output-dir>/manifest.json`；中途中断后重新运行同一命令会自动跳过已完成的音效条目（传 `--force` 强制重跑）。

## HTTP 接口兼容

所有接口均需携带 `Authorization: Bearer <API_TOKEN>`（或 `X-API-Key`）：

- `POST /api/ct/generate`、`POST /v1/audio/generations`、`POST /v1/audio/speech`：接收 JSON `{"prompt": "...", "duration": 10, "steps": 50, "guidance_scale": 4.5, "seed": 42, "num_waveforms": 1, "response_format": "wav", "save_to_drive": "auto"}`（为兼容通用 OpenAI 音频客户端，`prompt` 也可写作 `input`）。`num_waveforms=1` 默认直接返回二进制音频流并通过响应头 `X-Audio-Seed`、`X-Audio-Duration`、`X-Saved-Drive-Path` 返回元数据；传 `"return_json": true` 或 `num_waveforms > 1` 返回 Base64 JSON。
- `POST /api/ct/batch`：批量接收 `{"items": [...], "response_format": "wav"}` 并按序返回每条音效的 Base64 数据与种子信息。
- `GET /v1/models`、`GET /api/ct/info`、`GET /api/ct/drive-list`、`GET /health`。

## 许可说明、连接覆盖与本地 SSD 缓存

- **模型许可限制**：上游 `declare-lab/TangoFlux` 受 **Stability AI Community License** 与 **WavCaps** 数据集条款约束，仅限非商业与学术研究用途（商业使用需向 Stability AI 另行申请授权）。
- **连接与启动覆盖**：通过 `--url` / `TANGOFLUX_URL` 与 `--api-token` / `TANGOFLUX_API_TOKEN` 覆盖目标服务与凭据；`up` 支持 `--variant crpo|base`、`--model`、`--alias`，多实例支持 `-i ID`。

```bash
colabllm tangoflux up -i 2
colabllm tangoflux sfx 'Soft ocean waves on pebbles' -d 5 -i 2 -o ./waves.wav
colabllm tangoflux down -i 2
```

模型权重（`/content/tangoflux/models/`，仅拉取 `safetensors` 权重，跳过 `flan-t5-large` 冗余的 Flax/TF 副本以节省约 9.7 GB 下载量）与 Triton/Inductor 编译缓存均位于 Colab 本地 NVMe SSD，预热后归档至网盘：

```text
我的云端硬盘/colab-llm/tangoflux/
  outputs/
  cache-archives/tangoflux-diffusers0303-py312-sm75/
    triton-cache.tar
    inductor-cache.tar
```
