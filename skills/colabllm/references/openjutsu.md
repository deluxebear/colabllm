<!-- Generated from docs/openjutsu.md; edit the source and run tools/sync_runtime_docs.py. -->

# Openjutsu：MiniMax H3 角色动作迁移、局部视频编辑、人脸/色彩修复与图片驱动准备

默认 **G4**（RTX PRO 6000 Blackwell，约 96 GB 显存），前置 Caddy Basic Auth / 会话 Cookie / Bearer Token，在同一台 Colab GPU VM 内同机运行三层本地服务：

1. **ComfyUI（`127.0.0.1:8188`）**：预装 Openjutsu 所需的全部 13 个自定义节点包、SolarRenderer 长片段静音补丁，以及 Linux 路径规范化。
2. **已打补丁的 `comfy-api-proxy`（`127.0.0.1:8189`）**：转发实时生成进度并持久化任务元数据（单文件上传上限 500 MB）。
3. **Openjutsu Next.js 16 生产服务（`127.0.0.1:3000`）**：基于 [`colab_openjutsu`](https://github.com/deluxebear/colab_openjutsu) 提供完整中文网页工作台与 `/api/*` 接口。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动、模型档位与健康检查

```bash
# 默认 full 档位（约 80.7 GB 权重缓存至本地 NVMe SSD，G4 自动开启 --highvram，分段检查点写入本地 SSD）
colabllm openjutsu up --ready-timeout 3600

# 显式指定检查点存储与显存策略（--chains-storage drive 可跨 VM 持久化分段检查点）
colabllm openjutsu up --chains-storage ssd --vram-mode auto --ready-timeout 3600

# 仅准备核心 H3 动作迁移/局部替换/Montage/调色权重（约 50.8 GB，更快启动）
colabllm openjutsu up --profile core --ready-timeout 3600

# 查看状态、连接信息与日志
colabllm openjutsu status --json
colabllm openjutsu info --json
colabllm openjutsu info --show-secrets
colabllm openjutsu logs -n 100

# 通过 API 校验全部已安装模型与各工作流节点（/api/models + /api/setup）
colabllm openjutsu check
colabllm openjutsu check --mode simple
colabllm openjutsu check --profile core
```

从 `info` 返回的 URL 打开网页，浏览器使用 `auth_user` / `auth_pass` 完成一次 Basic Auth 登录（Caddy 自动下发 `ct_session` Cookie，保证后续 SSE 进度流与 `<video>` 分片拖动播放正常携带会话）；程序化调用使用 `Authorization: Bearer <api_token>`（亦可通过环境变量 `OPENJUTSU_URL` 与 `OPENJUTSU_API_TOKEN` 覆盖）。

## 命令行业务调用（无需打开网页，直接提交、跟踪进度并下载成片）

指定 `-o/--output` 时命令会自动等待渲染完成（通过 SSE 实时打印当前分段 `clip N` 与采样步数进度，`Ctrl+C` 仅退出本机等待而不中断远端任务），完成后自动下载到本地文件；若不加 `-o` 也不加 `--wait`，则上传提交后立即返回任务 JSON（含 `id`），后续可用 `job` / `download` / `cancel` 管理。

```bash
# 1. 先跑几秒钟 SAM 3.1 遮罩预览（可选，快速确认人物追踪准确度）
colabllm openjutsu mask --video input.mp4 --length 6 -o mask_preview.mp4

# 2. 单人角色动作迁移（simple 模式；默认挂载 DMAD 4-step LoRA，提速推荐 --steps 4 --clip-seconds 7.5，分辨率匹配原片短边如 576p/736p）
colabllm openjutsu swap --video input.mp4 -p character.png --steps 4 --clip-seconds 7.5 -o swap_result.mp4

# 3. 多人角色动作迁移（传 2–4 张 -p 自动切换 advanced 模式；双人可加 --max-quality 分两遍精渲）
colabllm openjutsu swap --video duet.mp4 -p left.png -p right.png --role "left dancer" --role "right dancer" --steps 4 --clip-seconds 7.5 -o duet_swap.mp4

# 4. 后台异步提交（立即返回任务 ID，不阻塞当前终端）与后续进度跟踪 / 下载 / 取消
colabllm openjutsu swap --video input.mp4 -p character.png --steps 4 --clip-seconds 7.5
colabllm openjutsu job JOB_ID --wait -o swap_result.mp4
colabllm openjutsu download JOB_ID -o swap_result.mp4
colabllm openjutsu cancel JOB_ID

# 5. 局部视频编辑（change 换装/换道具、remove 消除物体、background 换背景、add 添加物体）
colabllm openjutsu object --video input.mp4 --edit-mode change --target jacket --change-to "black leather jacket" --no-keep-outline -o jacket_edit.mp4
colabllm openjutsu object --video input.mp4 --edit-mode remove --target microphone -o clean.mp4
colabllm openjutsu object --video input.mp4 --edit-mode background --target person -p studio_bg.png -o new_bg.mp4

# 6. 成片后处理：Fix faces 人脸精修（需 full 档位）与 Fix colours 细节柔化 + DCTL 调色
colabllm openjutsu face --video swap_result.mp4 -p character.png --style mouth -o face_fixed.mp4
colabllm openjutsu color --video face_fixed.mp4 --detail -0.2 --tone-mode Reinhard -o final_graded.mp4
```

若已在网盘 `MyDrive/colab-llm/openjutsu/loras/` 放入自定义 `.safetensors`，可通过 `--extra-lora drive/my_lora.safetensors:0.8` 叠加在默认 DMAD 加速 LoRA 之上（或用 `--lora` 完全替换默认 LoRA）。

## 两个模型档位对比

| `--profile` | 本地 SSD 体积 | 包含模型与能力 |
| --- | --- | --- |
| `full`（默认） | ~80.7 GB（16 个文件） | 核心全部能力 + **Fix faces**（YOLOv8m 人脸/人体分割 + SAM ViT-B）+ **From picture**（FLUX.3 Image Klein INT8、Qwen3.5-9B Upsampler、Qwen3-VL-4B FP8、Flux2 VAE、SolarRenderer 4-step/control LoRA） |
| `core` | ~50.8 GB（7 个文件） | **Simple / Advanced / Montage 动作迁移**、**Object swap 局部编辑/移除/换背景**、**Check mask / Find people**、**Fix colours**（MiniMax H3 Ref2VA INT8 ConvRot + DMAD 4-step LoRA + Qwen3-VL 32B INT4 / 8B INT8 + SAM 3.1 Multiplex FP16 + H3 Video/Audio VAE） |

## 网页工作台模式（主页 `/` 顶部标签切换）

1. **Simple / Advanced**：上传参考视频与 1–4 张角色参考图；支持先点 **Check mask** 用 SAM 3.1 预览遮罩，再按 6 秒左右分段链式生成并自动拼接完整视频。
2. **Montage（多人剪辑模式）**：支持先 **Find people** 追踪并标注各镜头人物，再逐人匹配参考照生成。
3. **Object edit（局部替换 / 移除 / 换背景 / 添加物体）**：通过文字目标、放置框或涂抹关键帧驱动 SAM 3.1 + MiniMax H3 局部重绘。
4. **From picture（需 `full` 档位）**：用 FLUX.3 Klein 将角色照重打光/融入首帧，再由 SolarRenderer 或 H3 生成连续动作视频。
5. **Fix faces / Fix colours**：对成片做分块人脸重绘融合（**Fix faces** 需 `full` 档位）或细节柔化与 Orion4D DCTL 调色（**Fix colours**）。

## G4 性能调优、存储与断点续跑

- **显存常驻与系统内存即时回收（`--vram-mode auto|highvram|dynamic|gpu-only`，默认 `auto`；ComfyUI 默认开启 `--cache-classic`）**：Openjutsu 单任务活跃模型总计约 48 GB（H3 20 GB + Qwen3-VL 32B/8B 23 GB + VAE 3.3 GB + SAM 3.1 1.7 GB）。`auto` 在显存 `>=76 GiB` 的 GPU（如 G4 96 GB）上自动向 ComfyUI 传入 `--highvram`，让权重全程常驻显存，省去每个分段约 8 秒的 DynamicVRAM 换页开销与约 50 秒冷启动拷贝；同时传入 `--cache-classic` 让每个 `Context-Loop` 递归分段结束后立即释放 CPU 中间图像张量，避免 ComfyUI 0.44 默认 `--cache-ram` 在长视频或 `10s` 分段 + Spectrum 加速下跨分段累积系统内存导致 VM cgroup OOM。
- **分段检查点 SSD 加速（`--chains-storage ssd|drive`，默认 `ssd`）**：`MiniMaxH3-Context-Loop` 每生成一段会写出约 450 MB `.safetensors` 潜空间检查点（3 分钟视频约 14 GB）。默认 `ssd` 通过 Linux VFS `mount --bind` 将 `output/h3_chains` 绑定至本地 NVMe SSD（`/content/openjutsu/h3_chains`），既满足节点路径沙箱校验并保留同一 VM 内的断点续跑，又避免每段 Google Drive FUSE 同步写盘阻塞与网盘配额膨胀；**最终拼接成片仍直接保存在 Google Drive**（`output/h3_oneclick_swap/`、`h3_oneclick_edit/`、`openjutsu_color/`、`openjutsu_face/` 等）。若需跨不同 Colab VM 续跑超长任务，可传 `--chains-storage drive`。
- **推理速度与网页/CLI 参数甜点**：MiniMax H3 是 **100 层 3D 音视频全注意力 DiT**，单步耗时随总 Token 数（`分辨率像素 × 分段帧数`）呈 **`O(S²)` 二次方**增长。提速关键有三点：① **分辨率不超过原片短边**（例如 `608×1076` 竖屏原片选 `576p` 即 `576×1024`，若选默认 `736p` 超分至 `736×1312` 会使像素增加 48%、注意力计算量翻 2.2 倍）；② **采样步数设为 `4`**（默认已加载 `DMAD 4-step` 蒸馏加速 LoRA，从默认 `6` 步改为 `4` 步可直接省去 33% 扩散采样时间）；③ **分段时长（Clip length）保持在 `7s–8s` 甜点区间**（过短则固定 `39 帧` 重叠税占比高，拉到 `10s` 则单步序列达 ~5.6 万 Token 导致单步耗时从 `~7s` 激增至 `18.6s`）。在 `576p + 4 steps + 7.5s` 下，1 分钟视频在 G4 热显存下约 **8–9 分钟**即可完成。
- **高速模型并发拉取（Rust `hf_xet` + 多连接分片续传）**：启动阶段默认在独立环境启用 `huggingface_hub` + `hf_xet`（`HF_XET_HIGH_PERFORMANCE=1`），按模型体积从大到小（LPT）并发拉取 Xet 数据块（单 worker 实测约 600 MB/s），非 HF 源自动回退多连接 HTTP `Range` 分片与断点续传。与所有运行时统一一致，可在网盘放入 `MyDrive/colab-llm/hf_token.txt`（或本机 `~/.cache/huggingface/token` / `HF_TOKEN` / `--hf-token`），启动时会自动读取以解除 Hugging Face 匿名限速告警。
- **本地高速 NVMe SSD**：`/content/ComfyUI/models`（模型权重）、`/content/ComfyUI/custom_nodes`、`/content/openjutsu/proxy-state`（代理 SQLite 状态）与 Triton/Inductor 编译缓存均位于 VM 本地 SSD；编译缓存归档会同步至网盘加速后续启动。
- **Google Drive 持久化（`MyDrive/colab-llm/openjutsu/`）**：
  - `output/`：生成的最终 MP4 成片（以及 `--chains-storage drive` 模式下的 `h3_chains/` 分段检查点）。
  - `input/`、`user/`：上传素材与用户配置。
  - `loras/`：挂载到 `ComfyUI/models/loras/drive/`，放入此目录的自定义 `.safetensors` LoRA 会自动出现在 Openjutsu 网页的模型下拉列表中。

## 多实例与排错

```bash
colabllm openjutsu up -i 2
colabllm openjutsu check -i 2
colabllm openjutsu ps --json
colabllm openjutsu logs -i 2 -f
colabllm openjutsu down -i 2
```

远端日志分别位于 `/content/ct/comfyui.log`（ComfyUI 与节点推理）、`/content/ct/comfy_proxy.log`（API 代理）、`/content/ct/service.log`（Next.js）和 `/content/openjutsu/env-setup.log`（安装构建日志）。生成长视频时请保持会话在线，确认下载保存最终 MP4 后再执行 `down`。
