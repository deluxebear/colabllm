<!-- Generated from docs/qwen-image.md; edit the source and run tools/sync_runtime_docs.py. -->

# Qwen-Image-2.1：统一文生图、1–4 图 `<imageN>` 编辑、原生 RGBA 透明通道与 Google Drive 直连

默认 **G4**（RTX PRO 6000 Blackwell，96 GB VRAM），兼容 **H100**（80 GB HBM3）与 **A100**（40 GB / 80 GB）；基于 [vLLM-Omni 官方 Qwen-Image-2.1 Recipe](https://recipes.vllm.ai/Qwen/Qwen-Image-2.1) 在线部署 `Qwen/Qwen-Image-2.1`（7.1B 单流块因果注意力 DiT + 跨步精确 Prefix KV Cache + Qwen3-VL-8B 多模态编码器 + 16x 四通道 RGBA VAE），原生 fail-closed Bearer 认证（含无状态 CORS `OPTIONS` 预检）；支持中英双语文生图、1–4 张 `<image1>..<image4>` 序号绑定编辑、原生 RGBA 透明抠图/平铺导出、LoRA 适配器，以及直接读写挂载的 Google Drive 图片。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 前置条件

`Qwen/Qwen-Image-2.1` 采用 **Apache-2.0** 开源协议，在 Hugging Face 上**公开免门控**（无需同意许可协议即可下载；可选配置 `HF_TOKEN` 仅用于提升 Hugging Face Hub 下载速率限额）。

## G4、H100、A100 硬件选型与启动调用详解

模型磁盘权重共 **33.1 GB**（`Qwen3-VL-8B` 文本/视觉编码器 17.5 GB BF16 + `QwenImage21Transformer2DModel` 7.1B DiT 14.2 GB BF16 + 四通道 RGBA VAE 1.4 GB FP32），在开启默认 `--vae-use-tiling` 时 `1024x1024` 40 步 BF16 峰值显存约为 **34–40 GB**：

| Colab GPU | 架构与显存 | 推荐量化参数 (`--quantization`) | 峰值显存（1024²） | 适用场景与特点 |
| --- | --- | --- | --- | --- |
| **G4（默认）** | Blackwell `SM120` · **96 GB GDDR7** | `none`（默认满血 BF16） | ~35–40 GB（余量 >55 GB） | **日常主力挂机首选**：Colab 大显存档每小时计算单元单价更具性价比；满血 BF16 + 4 张高分参考图 + `--step-execution` 批量并发毫无显存压力 |
| **H100** | Hopper `SM90` · **80 GB HBM3** | `none`（默认满血 BF16） | ~35–40 GB（余量 ~40 GB） | **极致低延迟档**：3.35 TB/s HBM3 高带宽 + CUDA Graphs，单张 40 步 `1024x1024` 延迟最低 |
| **A100** | Ampere `SM80` · **40 GB / 80 GB** | `fp8-all`（40 GB 必选）或 `none`（80 GB） | ~28 GB（`fp8-all`） | **40 GB 兼容档**：同时对 Qwen3-VL-8B 与 DiT 非敏感层启用在线 FP8，显存降至约 28 GB |
| **L4（不支持）** | Ada `SM89` · **24 GB GDDR6** | — | 超出 24 GB 物理上限 | 33.1 GB 权重（最低在线 FP8 亦需 ~28 GB）无法常驻 24 GB 显存；24 GB 轻量场景请使用 [FLUX 3 Image Klein](flux-klein.md) `--gpu L4 --variant fp8r` |

### 1. G4 调用方式（默认推荐：96 GB 满血 BF16）

不传 `--gpu` 即默认分配 **G4** 并启用 `--vae-use-tiling` 自适应分块解码防 OOM：

```bash
# 默认 G4 + 满血 BF16（临时隧道）
colabllm qwen-image up

# G4 + 固定域名
colabllm qwen-image up --domain

# G4 + 固定域名 + 开启跨请求步级连续批处理（--step-execution，默认 max-num-seqs=8）
colabllm qwen-image up --domain --step-execution

# G4 + 预加载 Google Drive 上的 LoRA 适配器
colabllm qwen-image up --domain --lora-path drive://loras/anime-style.safetensors --lora-scale 1.0
```

### 2. H100 调用方式（80 GB HBM3 极速档）

```bash
# H100 + 满血 BF16 + 固定域名
colabllm qwen-image up --gpu H100 --domain

# H100 + 步级连续批处理 + 自定义最大并发序列数
colabllm qwen-image up --gpu H100 --domain --step-execution --max-num-seqs 8
```

### 3. A100 / 显存优化调用方式（在线 FP8 与 Prefix KV 量化）

```bash
# A100 (40 GB / 80 GB) + 在线全组件 FP8（保留构图敏感层 img_mlp 为 BF16，峰值显存约 28 GB）
colabllm qwen-image up --gpu A100 --domain --quantization fp8-all

# 长提示词 / 4 参考图高并发下进一步将跨步 Prefix KV Cache 的 V 压缩为 FP8
colabllm qwen-image up --domain --quantization fp8-text --prefix-kv-dtype fp8_v
```

### 状态与服务配置检查

```bash
colabllm qwen-image status --json
colabllm qwen-image models
colabllm qwen-image models --json
```

> **采样默认参数（自动注入）**：
> 上游 `vllm serve --omni` 通用默认值为 50 步、`true_cfg_scale=4.0`，而 `Qwen/Qwen-Image-2.1` 官方推荐 **40 步（`num_inference_steps=40`）且关闭无负向提示词时的多余 CFG（`true_cfg_scale=1.0`）**。本服务的中间件与 CLI 均已自动注入该最佳默认值：未传 `--negative-prompt` 时默认单路 `true_cfg_scale=1.0`（速度翻倍）；传入非空 `--negative-prompt` 时自动切换至 `true_cfg_scale=4.0` 启用 True CFG 双路引导。

## 文生图（Text-to-Image）与保存到 Google Drive

`Qwen-Image-2.1` 原生支持高质量**中文与英文**提示词及画面中英文字体排版渲染，分辨率自动按 `32` 像素网格对齐（默认 `1024x1024`）：

```bash
# 中文提示词生图并保存到本机
colabllm qwen-image generate '清晨阳光洒在木质茶桌上的一只青瓷茶壶，热气袅袅，写实摄影质感' -o ./teapot.png

# 直接将生成结果写入挂载的 Google Drive（不回传大图字节流）
colabllm qwen-image generate 'A retrofuturistic tram crossing a misty harbor bridge at dawn, crisp signage reading "HARBOR LINE 07"' --save-drive outputs/tram.png

# 等价写法：-o 直接指定 drive:// 路径
colabllm qwen-image generate '赛博朋克雨夜拉面馆招牌，霓虹灯清晰写着“深夜食堂”四个汉字' -o drive://outputs/ramen-sign.png

# 启用反向提示词（自动激活 true_cfg_scale=4.0）并同时保存到 Google Drive 与本机
colabllm qwen-image generate '精致的二次元少女角色立绘，柔和光影' \
  --size 1024x1536 \
  --negative-prompt '模糊，低画质，多余手指，畸变' \
  --save-drive outputs/anime-card.png \
  -o ./anime-card.png
```

## 1–4 图 `<imageN>` 参考编辑、原生 RGBA 透明抠图与 Google Drive 直连

支持 **1–4 张**参考图按传入顺序自动绑定为 `<image1>`、`<image2>`、`<image3>`、`<image4>`（传入第 5 张会返回 HTTP 400），且内置 16x **四通道 RGBA VAE** 可直接保留或生成透明背景 Alpha 通道：

```bash
# 查看云端硬盘上的输入图片、历史输出或 LoRA 权重
colabllm qwen-image ls-drive
colabllm qwen-image ls-drive 'MyDrive/photos/*.png'
colabllm qwen-image ls-drive --outputs
colabllm qwen-image ls-drive --loras

# 多参考图姿势迁移与换装（支持直接写中文 + <image1>..<image3> 精准指代，零上传读取 Google Drive）
colabllm qwen-image edit '让图1 <image1> 的人物摆出图2 <image2> 的动作并穿上图3 <image3> 的服装，严格保留图1的面部特征与发型，背景保持干净自然' \
  --drive-image inputs/person.jpg inputs/pose.jpg inputs/outfit.png \
  --size 1024x1536 \
  --save-drive outputs/pose-costume.png \
  -o ./pose-costume.png

# 服装提取 / 透明背景素材生成（--rgba 保留 4 通道 RGBA PNG 透明背景）
colabllm qwen-image edit '提取图1 <image1> 人物身穿的完整服装（上衣与下装），透明背景居中平铺展示，不要出现人物身体或衣架' \
  --drive-image inputs/model-photo.jpg \
  --rgba \
  --save-drive outputs/extracted-garment-rgba.png \
  -o ./extracted-garment-rgba.png

# 省略 --size 时（默认 auto），自动继承参考图宽高比（约 1024x1024 面积、32 像素对齐）
colabllm qwen-image edit 'Write the words "FRESH BASIL" on this ceramic plate in dark green hand-painted lettering' \
  --image drive://inputs/plate.png \
  --save-drive outputs/plate-basil.png
```

## 对接前端工作台（如 `open-pose-costume`）与 OpenAI Images SDK

中间件已内置以下兼容特性，可直接作为 OpenAI Images API 兼容后端接入 Web 前端（如 `open-pose-costume`）或官方 `openai` SDK：

- **CORS 跨域预检**：对 `OPTIONS` 预检返回 `204` 与 `Access-Control-Allow-*` 响应头，浏览器端携带 `Authorization: Bearer <API_TOKEN>` 即可直连。
- **模型别名自动映射**：请求中的 `"Qwen/Qwen-Image-2.1"`、`"qwen-image-2.1"`、`"qwen-image"` 以及 `"gpt-image-2"` / `"gpt-image-1.5"` / `"gpt-image-1"` 均自动映射至当前加载模型，不会触发 `Model mismatch`。
- **双格式 `/v1/images/edits`**：上游 vLLM-Omni 原生 `/v1/images/edits` 仅接受 `multipart/form-data`（直接 POST JSON 会断开连接，且 `url` 表单字段有 1024 KB 限制）；本服务中间件同时接受 **`multipart/form-data` 与 `application/json`**，自动将文件流、`data:image/...` 与 `drive://...` 转为无大小限制的本地回环二进制文件上传，完整保留 4 通道 RGBA 透明度与 `size="auto"` 自适应尺寸。

## 完整 HTTP API 说明

读取当前实例地址与 Bearer Token：

```bash
colabllm qwen-image info --json --show-secrets
```

除无状态 CORS `OPTIONS` 预检外，所有端点（含 `/health`、`/v1/*`、`/api/ct/*`）均强制校验 `Authorization: Bearer <API_TOKEN>`（或 `X-API-Key: <API_TOKEN>`）：

| 方法与路径 | 用途 | 核心参数与 Google Drive 扩展 |
| --- | --- | --- |
| `POST /v1/images/generations` | OpenAI 文生图（返回 4 通道 RGBA 或 RGB `b64_json`） | 默认 `num_inference_steps=40`、`true_cfg_scale=1.0`；支持 `"save_to_drive"`、`"drive_only"`、`"color_format"`、Drive `lora.path` |
| `POST /v1/images/edits` | OpenAI 兼容 1–4 图编辑（同时支持 `multipart/form-data` 与 JSON） | 支持 `image` / `image[]` 文件上传、`drive://...`、`size="auto"`、`background="transparent"`、`"save_to_drive"` |
| `POST /api/ct/edit` | 简洁 JSON 1–4 图编辑（返回 `{created, data: [{b64_json, ...}]}`） | `"images": ["drive://inputs/a.png", ...]` + `"save_to_drive": "outputs/b.png"` |
| `POST /v1/chat/completions` | vLLM-Omni 多模态 `image_url` 编辑（1–4 张参考图） | `image_url.url` 可直接传 `drive://...`；支持 `extra_body` 与 `"save_to_drive"` |
| `GET /api/ct/drive-list` | 列出网盘图片、输出或 LoRA（`?path=...&kind=images\|outputs\|loras`） | 严格限制在 `MyDrive` 与服务数据目录内 |
| `GET /api/ct/info` / `GET /v1/models` | 查询当前模型、量化、步级批处理状态、默认采样参数与网盘路径 | 返回 `rgba_vae`、`max_reference_images`、`drive_inputs_dir`、`drive_outputs_dir` |

Python 调用示例：

```python
import base64
import json
import os
import urllib.request

base_url = os.environ["QWEN_IMAGE_URL"].rstrip("/")
token = os.environ["QWEN_IMAGE_API_TOKEN"]

payload = {
    "model": "Qwen/Qwen-Image-2.1",
    "prompt": "让图1 <image1> 的人物摆出图2 <image2> 的姿势，保持人物面部与服装细节一致",
    "images": ["drive://inputs/person.png", "drive://inputs/pose.png"],
    "size": "auto",
    "num_inference_steps": 40,
    "true_cfg_scale": 1.0,
    "save_to_drive": "outputs/pose-transfer.png",
    "seed": 42,
}
req = urllib.request.Request(
    f"{base_url}/v1/images/edits",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=300) as resp:
    result = json.loads(resp.read().decode("utf-8"))

print("Saved on Drive:", result.get("rel_drive_paths"))
with open("pose-transfer.png", "wb") as f:
    f.write(base64.b64decode(result["data"][0]["b64_json"]))
```

## 本地 SSD 权重与编译缓存归档

- **本地 NVMe SSD 高速加载**：`Qwen/Qwen-Image-2.1`（约 33.1 GB）下载并缓存在 VM 本地 NVMe SSD（`/content/qwen-image/model/Qwen-Image-2.1`），禁用 Xet 临时块缓存膨胀并在下载后自动清理 `/root/.cache/huggingface`，确保 Colab 本地磁盘余量充裕。
- **跨步精确 Prefix KV Cache 与按架构编译缓存归档**：提示词与最多 4 张参考图编码仅在首个去噪步计算一次并在后续 39 步精确复用；FlashInfer、Triton 与 vLLM/TorchInductor 编译缓存在预热生图后按 GPU 架构（如 `sm120`、`sm90`、`sm80`）分别打包归档至 `MyDrive/colab-llm/qwen-image/cache-archives/`。
