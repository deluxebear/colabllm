# MiniMax-H3-Image：SGLang Diffusion 6 步 PDD 原生 4K 文生图、1–16 图编辑与网络友好内部协议

默认 **G4**（`NVIDIA RTX PRO 6000 Blackwell Server Edition`，96 GB GDDR7，`sm_120`），兼容 **H100**（80 GB HBM3）与 **A100**（80 GB）；基于 **SGLang Diffusion** 在线部署 `MiniMaxAI/MiniMax-H3-Image`（33.1B DiT + 33.4B Qwen3-VL 编码器 + MiniMaxH3ImageVAE，6 步融合 PDD 蒸馏调度 + 在线 AdaLN 预计算释放 24.6 GiB 显存），前置 fail-closed Bearer 认证网关。

针对 4K（16 MP）原图体积达 30–50 MB、Base64 JSON 膨胀至 40–67 MB 且长连接易受隧道/代理抖动影响的特点，CLI 默认走**网络友好的内部协议（`/api/ct/*`）**：异步提交任务 + 极小 JSON 轮询 + 本地参考图 SHA-256 去重裸二进制上传 + HTTP `Range` 断点续传下载 + 服务端按需 WebP/JPEG 转码 + Google Drive 零流量直读直写；同时完整保留标准 OpenAI `/v1/images/generations` 与 `/v1/images/edits` 兼容接口。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 前置条件

`MiniMaxAI/MiniMax-H3-Image` 在 Hugging Face 上公开提供（可选配置 `HF_TOKEN` 用于提升下载速率限额）。启动时自动跳过仓库中重复的 `transformer_diffusers/` 与 `vae_diffusers/` 副本并将 `text_encoder` 指向本地子目录，仅下载 **135.3 GB** 核心权重至 Colab 本地 NVMe SSD（为 `/content` 预留约 110 GB 余量），加载至显存后自动调用 `POSIX_FADV_DONTNEED` 释放主机内存页缓存。

## G4、H100、A100 硬件选型与启动调用

| Colab GPU | 架构与显存 | 推荐量化与驻留策略 | 峰值显存 | 实测速度参考（G4） |
| --- | --- | --- | --- | --- |
| **G4（默认推荐）** | Blackwell `sm_120` · **96 GB GDDR7** | `--quantization fp8-all`（全组件常驻 + 在线 AdaLN） | **~50.4 GiB**（余量 ~45 GiB） | **2K 实测约 2.6s / 原生 4K 16:9 实测约 15.7s**，1–16 张参考图 4K 编辑毫无显存压力 |
| **G4（无损精度档）** | Blackwell `sm_120` · **96 GB GDDR7** | `--quantization bf16`（原生 BF16 全常驻 + 在线 AdaLN） | **~92.2 GiB** | **2K 约 3.43s / 原生 4K 约 18.54s**，保留 100% 原始发布精度 |
| **H100 / A100-80GB** | Hopper / Ampere · **80 GB HBM** | `fp8-all`（全常驻）或 `bf16`（自动卸载 `text_encoder`） | ~50.4 GiB（FP8）/ ~66.1 GiB（BF16 Offload） | H100 HBM3 带宽高，FP8 全常驻延迟极低 |

```bash
# 默认 G4 + FP8 全组件常驻（50.4 GiB 显存，4K 约 14.5s、2K 约 2.3s）
colabllm minimax-h3-image up

# G4 + 原生无损 BF16 全组件常驻（92.2 GiB 显存，在线 AdaLN 零损省 24.6 GiB）
colabllm minimax-h3-image up --quantization bf16

# 跳过启动阶段的 PDD 网格预热以加快首次就绪速度（首条请求再初始化）
colabllm minimax-h3-image up --no-warmup

# H100 80 GB + FP8 全组件常驻
colabllm minimax-h3-image up --gpu H100 --quantization fp8-all

# 查看服务状态、量化模式与原生 4K 预设分辨率
colabllm minimax-h3-image status --json
colabllm minimax-h3-image models
colabllm minimax-h3-image models --json
```

> **模型采样契约（网关自动校验与归一化）**：
> `MiniMax-H3-Image` 采用 6 个融合 PDD 输出头与单正分支蒸馏架构：步数固定为 **6 步**（`num_inference_steps=6`），`guidance_scale=1`，**不支持 `negative_prompt` 或 LoRA**。默认最小生成画布面积 `min_generation_area=4194304`（`2048x2048`）。

## 原生 4K / 极速 2K 文生图（Text-to-Image）

默认通过内部异步接口 `/api/ct/jobs` 提交并轮询小 JSON；当输出扩展名为 `.webp` 或 `.jpg` 时，网关自动在远端完成编码再通过裸二进制流回传，可将 40 MB 的 4K PNG 压缩至 **2–4 MB**，显著提升跨网下载速度：

```bash
# 默认原生 4K（1:1 4096x4096），远端转码为高画质 WebP 回传本机（仅约 2~4 MB 网络流量）
colabllm minimax-h3-image generate 'Rain-soaked Tokyo alley at dusk, warm izakaya lanterns reflected on wet asphalt, detailed editorial photograph' -o ./alley-4k.webp

# 使用官方 16:9 原生 4K 预设（5472x3072）并输出无损压缩 PNG
colabllm minimax-h3-image generate 'Aerial photograph of winding Alpine pass at golden hour, crisp snow ridges' --aspect 16:9 --tier 4k -o ./alpine-16x9.png

# 极速 2K 档（--tier 2k，2048x2048，G4 FP8 仅约 2.3 秒）快速构图验证
colabllm minimax-h3-image generate 'Minimalist ceramic coffee dripper on walnut counter, morning sunlight' --tier 2k -o ./coffee-2k.webp

# 1K 快速草图预览（--tier 1k --fast-preview 同步将 min_generation_area 降至画布像素面积）
colabllm minimax-h3-image generate 'Isometric cozy bookshop cutaway illustration' --tier 1k --fast-preview -o ./sketch-1k.webp

# 零下载流量：直接将原生 4K PNG 写入挂载的 Google Drive（--drive-only 不回传图片字节）
colabllm minimax-h3-image generate 'Macro shot of mechanical tourbillon watch movement' --aspect 3:2 --save-drive outputs/watch-4k.png --drive-only

# 异步后台提交（--no-wait 立即返回 job_id，断网不影响 Colab 端继续推理）
colabllm minimax-h3-image generate 'Futuristic botanical conservatory dome at night' --aspect 21:9 --no-wait
```

官方预设宽高比（`--aspect`）：

| 比例 | `--tier 4k`（原生 16 MP） | `--tier 2k`（极速 4 MP） | `--tier 1k`（1 MP） |
| --- | --- | --- | --- |
| `1:1` | `4096x4096` | `2048x2048` | `1024x1024` |
| `4:3` / `3:4` | `4736x3552` / `3552x4736` | `2368x1760` / `1760x2368` | `1184x896` / `896x1184` |
| `3:2` / `2:3` | `5024x3360` / `3360x5024` | `2496x1664` / `1664x2496` | `1248x832` / `832x1248` |
| `16:9` / `9:16` | `5472x3072` / `3072x5472` | `2752x1536` / `1536x2752` | `1376x768` / `768x1376` |
| `21:9` / `9:21` | `6272x2688` / `2688x6272` | `3136x1344` / `1344x3136` | `1568x672` / `672x1568` |

## 1–16 张参考图编辑（`<Picture 1>..<Picture 16>`）与上传去重

参考图按命令行出现顺序绑定为 `<Picture 1>`、`<Picture 2>` …… 最高 **16 张**（若习惯写 `<image1>`，网关也会自动转换为 `<Picture 1>`）。本地参考图首次上传采用裸二进制流（免除 Base64 `+33%` 体积膨胀），且按 SHA-256 在服务端缓存——对同一参考图反复调优提示词时**二次上传流量为 0 字节**：

```bash
# 列出 Google Drive 上的输入参考图或历史输出
colabllm minimax-h3-image ls-drive
colabllm minimax-h3-image ls-drive --outputs

# 本地多参考图融合编辑（首次二进制上传，重复编辑同一本地文件自动 0 字节命中缓存；默认匹配 <Picture 1> 宽高比）
colabllm minimax-h3-image edit 'Place the character from <Picture 1> inside the sunlit studio from <Picture 2>, wearing the jacket from <Picture 3>, natural shadows' \
  -r ./person.png -r ./studio.jpg -r ./jacket.png \
  --tier 2k \
  -o ./composite.webp

# Google Drive 零上传 + 零下载全云端 4K 编辑
colabllm minimax-h3-image edit 'Keep the product bottle in <Picture 1> unchanged and place it on mossy volcanic rock beside a mountain stream' \
  --drive-image inputs/bottle.png \
  --tier 4k \
  --save-drive outputs/bottle-outdoor-4k.png \
  --drive-only
```

## 异步任务管理与断点续传下载（`job` / `download`）

当使用 `--no-wait` 提交任务，或在等待中途按 `Ctrl-C` / 遇到网络波动中断时，远端 GPU 任务会继续完成并将产物保留在本地 NVMe 队列中：

```bash
# 列出最近的任务状态与耗时
colabllm minimax-h3-image job

# 查询指定任务或阻塞等待其完成
colabllm minimax-h3-image job job_0123456789abcdef --wait

# 下载已完成任务（支持 HTTP Range 断点续传，且可按需在服务端实时转码为 WebP/JPEG）
colabllm minimax-h3-image download job_0123456789abcdef -o ./result.webp --quality 95
colabllm minimax-h3-image download job_0123456789abcdef -o ./result-lossless.png
```

## HTTP API 调用示例（内部轻量协议与 OpenAI 兼容协议）

除 CLI 默认使用的内部协议（`POST /api/ct/uploads`、`POST /api/ct/jobs`、`GET /api/ct/jobs/{id}`、`GET /api/ct/jobs/{id}/content`）外，外部 SDK 也可直接调用标准 OpenAI Images 接口（所有路由均需 `Authorization: Bearer <API_TOKEN>`）：

```python
import base64
import json
import urllib.request

BASE_URL = "https://minimax-h3-image.example.com"
API_TOKEN = "sk-your-token"

req = urllib.request.Request(
    f"{BASE_URL}/v1/images/generations",
    headers={
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    },
    data=json.dumps({
        "model": "MiniMaxAI/MiniMax-H3-Image",
        "prompt": "Architectural concrete pavilion beside still reflection pool at twilight",
        "size": "2048x2048",
        "seed": 42,
        "output_format": "webp",
        "output_quality": 92,
    }).encode("utf-8"),
)
with urllib.request.urlopen(req, timeout=300) as resp:
    result = json.loads(resp.read().decode("utf-8"))
    with open("pavilion.webp", "wb") as f:
        f.write(base64.b64decode(result["data"][0]["b64_json"]))
```

## 停止服务

```bash
colabllm minimax-h3-image down
```
