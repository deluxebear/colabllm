# FLUX 3 Image Klein：文生图、多图参考编辑与 Google Drive 直连

默认 **G4**（RTX PRO 6000 Blackwell，96 GB VRAM），兼容 **H100**（80 GB HBM3）与 **L4**（24 GB）；基于 vLLM-Omni 在线服务部署 `black-forest-labs/flux-3-image-klein`（5.6B 蒸馏 Flow-Map DiT + Qwen3-VL-4B 文本编码器 + VAE），原生 fail-closed Bearer 认证；支持 OpenAI 兼容文生图、1–10 张参考图编辑、LoRA 适配器、可选 `flux-3-image-klein-pup` 提示词扩写（Prompt Upsampler），以及直接读写挂载的 Google Drive 图片。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 前置条件

`black-forest-labs/flux-3-image-klein`（及非蒸馏版 `flux-3-image-klein-base`）在 Hugging Face 上受 **FLUX Kommunity License** 门控保护：

1. 先在 Hugging Face 模型主页（`https://huggingface.co/black-forest-labs/flux-3-image-klein`）同意许可协议。
2. 启动时通过 `--hf-token <TOKEN>`、环境变量 `HF_TOKEN`，或在云端硬盘 `MyDrive/colab-llm/hf_token.txt` 中存放有效 Token（若使用 Fine-grained Token，请勾选 *"Read access to contents of all public gated repos you can access"* 权限）。

## G4、L4、H100 硬件选型与启动调用详解

本服务默认分配 **G4**，并支持通过 `--gpu G4|L4|H100` 按场景随时切换：

| Colab GPU | 架构与显存 | 推荐精度 (`--variant`) | 是否支持同机 `--enable-pup` | 适用场景与特点 |
| --- | --- | --- | --- | --- |
| **G4（默认）** | Blackwell `SM120` · **96 GB GDDR7** | `bf16`（默认，约 19 GiB） | **支持**（合计约 43 GiB，余量 >50 GB） | **日常主力挂机首选**：Colab 大显存档每小时计算单元单价更低；满血 BF16 + 9B PUP + 10 张高分参考图 + LoRA 长期在线毫无显存压力 |
| **H100** | Hopper `SM90` · **80 GB HBM3** | `bf16`（默认）或 `fp8r` | **支持**（合计约 43 GiB，余量 ~37 GB） | **极致低延迟 / 官方基准**：约 3.35 TB/s HBM3 带宽 + `SM90` 原生算子，单张 16 步生图比 G4 快约 20%–35%，计算单元消耗最高 |
| **L4** | Ada `SM89` · **24 GB GDDR6** | `fp8r`（推荐，约 17 GiB） | **不支持**（24 GB 无法同时容纳 9B PUP） | **极低成本轻量档**：适合不开启 PUP 的文生图与中低并发图生图，计算单元扣点最省 |

> **精度与算子说明**：
> - `bf16`（默认）：根包完整 BF16 权重，画质基准，在 **G4** 与 **H100** 上均建议直接使用。
> - `fp8r`：加载 `variants/fp8r` 量化包（最低约 17 GB 显存），在 **L4** 上推荐必选以留出多图 VAE 编解码显存余量；在 `G4`（`SM120`）上运行时，Notebook 会自动屏蔽不兼容的 `FlashInferFP8ScaledMMLinearKernel` 回退至稳定算子。
> - `nvfp4` 与 `--fp8-attention`：面向数据中心 Blackwell `SM100`（如 B200），在 `G4`（`SM120`）、`H100`（`SM90`）、`L4`（`SM89`）上无需开启。

### 1. G4 调用方式（默认推荐：高性价比 + 96 GB 满血 BF16 + 可选 PUP）

不传 `--gpu` 即默认使用 **G4**：

```bash
# 默认 G4 + 满血 BF16（临时隧道）
colabllm flux-klein up --hf-token hf_example

# G4 + 固定域名 + 同机开启 Qwen3.5-9B 提示词扩写（PUP）
colabllm flux-klein up --domain --enable-pup

# G4 + 预加载 Google Drive 上的 LoRA 适配器
colabllm flux-klein up --domain --enable-pup --lora-path drive://loras/style.safetensors --lora-scale 1.0
```

### 2. L4 调用方式（低成本轻量档：24 GB 显存 + `fp8r` 量化）

在 **L4**（24 GB）上请搭配 `--variant fp8r` 预留充足的参考图编码与 VAE 解码显存，且**不要**传 `--enable-pup`（若需扩写提示词可在客户端预先准备详细英文描述）：

```bash
# L4 + FP8R 量化（约 17 GB 显存占用，剩余 ~7 GB 供参考图与激活值使用）
colabllm flux-klein up --gpu L4 --variant fp8r --hf-token hf_example

# L4 + 固定域名 + FP8R 量化
colabllm flux-klein up --gpu L4 --domain --variant fp8r
```

### 3. H100 调用方式（官方 Recipe 极速档：80 GB HBM3）

需要最快单张响应延迟、批量高频生图或对齐官方 H100 基准时，显式传入 `--gpu H100`：

```bash
# H100 + 满血 BF16 + 固定域名 + 同机开启 Qwen3.5-9B PUP
colabllm flux-klein up --gpu H100 --domain --enable-pup

# H100 + FP8R 量化变体（进一步提升高分辨率并发吞吐）
colabllm flux-klein up --gpu H100 --domain --variant fp8r --enable-pup
```

### 状态与模型模式检查

```bash
colabllm flux-klein status --json
colabllm flux-klein models
colabllm flux-klein models --json
```

蒸馏版 `flux-3-image-klein` 默认采用 **16 步** flow-map 采样（`flow_shift=6.93`），**不支持** `guidance_scale` 与非空 `negative_prompt`；若需要 50 步 CFG 与反向提示词，请在启动时指定 `--model black-forest-labs/flux-3-image-klein-base`（建议在 `G4` 或 `H100` 上运行）。

## 文生图（Text-to-Image）与保存到 Google Drive

```bash
# 生成并保存到本机文件
colabllm flux-klein generate 'a serene alpine meadow at golden hour, wildflowers in the foreground' -o ./meadow.png

# 直接将生成结果写入挂载的 Google Drive（不回传大图字节流，速度最快）
colabllm flux-klein generate 'minimalist ceramic tea set on warm oak table, soft morning window light' --save-drive outputs/tea-set.png

# 等价写法：-o 直接指定 drive:// 路径
colabllm flux-klein generate 'isometric cozy bookshop cutaway illustration' -o drive://outputs/bookshop.png

# 同时保存到 Google Drive 与本机目录，一次生成多张
colabllm flux-klein generate 'futuristic high-speed train crossing a snowy viaduct' --size 1536x1024 -n 2 --save-drive outputs/train.png -o ./train.png
```

分辨率宽高须为 `16` 的整数倍（范围 `64`–`4096`，默认 `1024x1024`）。
只传 `--save-drive`（或 `-o drive://...`）而未传本地 `-o` 时，服务端直接将 PNG 写入云端硬盘 `MyDrive/colab-llm/flux-klein/outputs/`（或指定子路径）并跳过 base64 下载；同时传入 `--save-drive` 与本地 `-o ./out.png` 时两端同时落盘。

## 图生图与多图参考编辑（直接使用 Google Drive 上的图片）

支持 **1–10 张**参考图按传入顺序条件化编辑，参考图可混合来自**挂载的 Google Drive**、本机文件或 `https://` URL：

```bash
# 查看云端硬盘上的输入图片、历史输出或 LoRA 权重
colabllm flux-klein ls-drive
colabllm flux-klein ls-drive 'MyDrive/photos/*.jpg'
colabllm flux-klein ls-drive --outputs
colabllm flux-klein ls-drive --loras

# 直接读取挂载在 Google Drive 上的参考图进行编辑，并将结果保存回 Google Drive（全程零上传、零下载）
colabllm flux-klein edit 'restyle the scene as a delicate studio Ghibli watercolor painting' \
  --image drive://inputs/street.jpg \
  --save-drive outputs/street-watercolor.png

# 强制将相对路径解析为 Google Drive 路径（即使本机当前目录存在同名文件）
colabllm flux-klein edit 'replace the daytime sky with dramatic aurora borealis over snowy peaks' \
  --drive-image inputs/mountain.jpg \
  --save-drive outputs/mountain-aurora.png \
  -o ./mountain-aurora.png

# 多参考图组合编辑（最多 10 张，严格保持命令行 image 1, image 2, image 3 顺序）
colabllm flux-klein edit 'dress the person from image 1 in the tailored jacket from image 2 inside the cafe from image 3' \
  --drive-image inputs/person.png inputs/jacket.png inputs/cafe.jpg \
  --save-drive outputs/dressed-in-cafe.png
```

省略 `--size` / `--width` / `--height` 时，FLUX 3 会自动按第一张参考图的宽高比计算约 1MP（`16` 像素对齐）的输出尺寸。若本机不存在 `--image` 指定的相对文件，CLI 也会自动将其作为云端硬盘路径交由远端 VM 在 `MyDrive/colab-llm/flux-klein/inputs/`、`MyDrive/colab-llm/flux-klein/` 或 `MyDrive/` 下安全解析。

## 提示词扩写（PUP）与 LoRA 适配器

在 `G4` 或 `H100` 上启用 `up --enable-pup` 后，可单独扩写短提示词，或在 `generate` / `edit` 时附加 `-u` / `--upsample` 自动扩写后再出图：

```bash
# 单独调用 Qwen3.5-9B PUP 扩写文生图或图生图提示词（支持 --bbox 输出 0-1000 布局框表）
colabllm flux-klein upsample 'A man walking with his dog, afternoon sun' --canvas 1536:1024
colabllm flux-klein upsample 'put a red bandana around the dog neck' --drive-image inputs/dog.jpg
colabllm flux-klein upsample 'artisan bakery display counter with price tags' --bbox

# 出图/编辑时内联自动扩写提示词，并加载存放在 Google Drive 上的 LoRA（支持原始 .safetensors 或 PEFT 目录）
colabllm flux-klein generate 'retro sci-fi poster of Saturn express' \
  --upsample \
  --lora drive://loras/retro-poster.safetensors \
  --lora-scale 0.9 \
  --save-drive outputs/saturn-poster.png
```

服务端首次收到 Drive 上的 LoRA 路径时，会自动校验目录边界并将其缓存到 VM 本地 NVMe SSD（`/content/flux-klein/loras/`），避免每次推理直接走云端硬盘 FUSE 读取。

## 完整 HTTP API 说明

先读取当前实例地址与 Bearer Token（仅在客户端调用需要时读取密钥）：

```bash
colabllm flux-klein info --json --show-secrets
```

所有端点（含 `/health`、`/v1/*`、`/api/ct/*`）均强制校验 `Authorization: Bearer <API_TOKEN>`（或 `X-API-Key: <API_TOKEN>`）：

| 方法与路径 | 用途 | Google Drive 扩展能力 |
| --- | --- | --- |
| `POST /v1/images/generations` | OpenAI 文生图（返回 `b64_json`） | 支持 `"save_to_drive"`、`"drive_only"`、`"upsample"`、Drive `lora.path` 及响应头 `X-Saved-Drive-Path` |
| `POST /v1/chat/completions` | vLLM-Omni 原生多参考图编辑（1–10 张 `image_url`） | `image_url.url` 可直接传 `drive://...` / `MyDrive/...`；支持 `"save_to_drive"` 与 `"drive_only"` |
| `POST /api/ct/edit` | 简洁 JSON 图生图/多图编辑（返回 OpenAI Images 结构） | `"images": ["drive://inputs/a.jpg", ...]` + `"save_to_drive": "outputs/b.png"` |
| `POST /v1/images/edits` | OpenAI SDK `client.images.edit` 兼容（`multipart/form-data` 或 JSON） | 支持上传文件字段或 `drive://...` 字符串字段，以及 `save_to_drive` |
| `POST /api/ct/upsample` | 调用同机 `flux-3-image-klein-pup` 扩写 T2I / I2I 提示词 | `"images"` 支持直接引用 `drive://...` |
| `GET /api/ct/drive-list` | 列出网盘图片、输出或 LoRA（`?path=...&kind=images\|outputs\|loras`） | 严格限制在 `MyDrive` 与服务数据目录内 |
| `GET /api/ct/info` / `GET /v1/models` | 查询当前模型、变体、默认采样参数、PUP 状态与网盘目录 | 返回 `drive_inputs_dir`、`drive_outputs_dir`、`drive_loras_dir` |

Python 调用示例（标准 `urllib` 或 OpenAI SDK 均可）：

```python
import json
import os
import urllib.request

base_url = os.environ["FLUX_KLEIN_URL"].rstrip("/")
token = os.environ["FLUX_KLEIN_API_TOKEN"]

payload = {
    "model": "flux-3-image-klein",
    "prompt": "turn this daylight street photo into rainy neon cyberpunk night, keeping architecture intact",
    "images": ["drive://inputs/street.jpg"],
    "save_to_drive": "outputs/street-cyberpunk.png",
    "drive_only": True,
    "seed": 42,
}
req = urllib.request.Request(
    f"{base_url}/api/ct/edit",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=300) as resp:
    result = json.loads(resp.read().decode("utf-8"))
print("Saved on Drive:", result["rel_drive_paths"])
```

## 本地 SSD 权重与编译缓存归档

- **按需精简下载**：Setup 会先读取 Hugging Face 仓库元数据，仅将当前 `--variant` 所需的根配置、`ae.safetensors`、`text_encoder/*` 以及对应 `model.safetensors`（或 `variants/<variant>/*`）下载至本地 NVMe SSD（`/content/flux-klein/model/`），跳过重复的 Diffusers 分片与未选中的量化包，节省超过 20 GB 下载量与本地磁盘空间。
- **按 GPU 架构隔离编译缓存归档**：FlashInfer、Triton 与 vLLM/TorchInductor 缓存在本地 SSD 上运行；预热推理完成后按 GPU 架构（`G4` 的 `sm120`、`H100` 的 `sm90`、`L4` 的 `sm89` 互不冲突）分别打包归档至 `MyDrive/colab-llm/flux-klein/cache-archives/`，切换不同 GPU 启动时均可直接命中各自归档。
