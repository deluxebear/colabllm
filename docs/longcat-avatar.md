# LongCat-Video-Avatar 1.5（Colab G4）

> Tunnel 域名说明：以下固定域名示例需先设置主域名，例如 `./colabctl.py tunnel-config --base-domain example.com`（所有服务共享）。未配置时使用动态 URL；`up --base-domain example.com` 可单次覆盖，`up --quick-tunnel` 强制动态。详见 [Tunnel 配置](../README.md#cloudflare-tunnel-配置)。

官方模型：[meituan-longcat/LongCat-Video-Avatar-1.5](https://huggingface.co/meituan-longcat/LongCat-Video-Avatar-1.5)。独立于基础版 `longcat-video`，默认 G4、BF16 DiT + Whisper-Large-v3 + 官方 DMD LoRA，固定 8 步、25 fps。

```bash
./services/longcat-avatar.py up --domain
./services/longcat-avatar.py info       # 查看网页登录账号密码、API Token
./services/longcat-avatar.py status
./services/longcat-avatar.py logs -f
./services/longcat-avatar.py down
```

## CLI 生成与下载

命令在本机执行，视频仍由 Colab GPU 后台生成。只需 Python 标准库，不需要安装 Gradio SDK。默认从 `.state/longcat-avatar/state.json` 读取地址和凭据。

```bash
# 异步提交：按完整音频长度生成，立即返回 JSON 中的 job_id
./services/longcat-avatar.py generate \
  --audio speech.wav --image person.png \
  --prompt "A person speaking naturally in a bright room."

# 一条命令等待完成并下载；省略 --image 可用音频＋描述模式
./services/longcat-avatar.py generate \
  --audio speech.wav --image person.png \
  --prompt "A person speaking naturally in a bright room." \
  -o result.mp4

# 查询现有任务，或等待它完成并下载
./services/longcat-avatar.py job JOB_ID
./services/longcat-avatar.py job JOB_ID --wait -o result.mp4
./services/longcat-avatar.py download JOB_ID -o result.mp4

# 手动只生成短片：93 帧 / 25 fps，即约 3.72 秒
./services/longcat-avatar.py generate \
  --audio speech.wav --image person.png --prompt-file prompt.txt \
  --frames 93 --segments 1 -o preview.mp4
```

默认不指定 `--frames` / `--segments` 时覆盖完整音频。指定其中任意一个即使用手动时长模式；`--frames` 默认为 93、`--segments` 默认为 1。`--resolution` 支持 480p / 720p，`--seed` 默认 42，-1 为随机。

`-o` 在 `generate` / `job` 命令中会自动等待；也可使用 `--wait` 只等待而不下载。Ctrl+C 或等待超时只停止本机等待，已提交的任务仍继续运行。`--timeout 0` 默认不限等待时间；可设置 `--poll` 调整查询间隔。

多实例使用 `-i ID`，与其他服务命令一致。在其他机器上可通过 `--url` 和 `LONGCAT_AVATAR_TOKEN` 环境变量指定服务；也支持 `LONGCAT_AVATAR_URL`。更换地址时需显式提供对应 Token，不会自动把本机保存的密钥转发到其他地址。

标准输出为 JSON，进度写入标准错误，方便脚本处理。此 CLI 使用稳定的后台任务 JSON 接口，提交后不依赖网页连接。

## 使用 Google Drive 音频、图片和输出目录

`drive://` 表示 Colab 已挂载的「我的云端硬盘」，不是本机路径，也不是 Google Drive 网页分享链接。文件会直接在服务端读取，无需先下载到本机。

```bash
# 列出网盘目录，查找素材路径
./services/longcat-avatar.py ls-drive
./services/longcat-avatar.py ls-drive "素材/数字人"

# 直接使用网盘音频、图片，结果保存到指定网盘目录
./services/longcat-avatar.py generate \
  --audio "drive://素材/数字人/讲话.wav" \
  --image "drive://素材/数字人/人像.png" \
  --prompt "A person speaking naturally in a bright room." \
  --drive-output "成片/数字人"

# 也可显式使用网盘参数；本地与网盘输入可以混用
./services/longcat-avatar.py generate \
  --audio-drive "素材/数字人/讲话.wav" \
  --image ./person.png \
  --prompt-file prompt.txt \
  --drive-output "成片/数字人" --wait
```

支持 `drive://...`、`gdrive://...` 或完整的 `/content/drive/MyDrive/...` 路径。`--audio-drive` / `--image-drive` 可以直接填写 MyDrive 内的相对路径。

`--drive-output`（别名 `--output-drive`）指定目录，目录不存在时自动创建；生成的 `<任务编号>.mp4` 和同名 JSON 都保存到该目录。未指定时继续保存到 `colab-llm/longcat-avatar/output/`。如还需要本地副本，可再加 `-o result.mp4`，任务完成后从网盘输出直接下载。

任务提交时会在 Drive 的任务目录保存输入快照，后续移动原素材不会破坏已提交任务。每个任务的查询结果中，`result.path` 和 `metadata.drive_output` 给出实际网盘输出位置。

CLI 使用 `POST /jobs` 提交任务，`GET /jobs/<编号>` 查询，`GET /jobs/<编号>/video` 下载，`GET /drive/files?path=drive://...` 列目录。所有接口均经过原有 Caddy 认证。自定义目录的视频在网页查看时复制到本地发布缓存，不会把整个网盘目录暴露给 Gradio 文件接口。

## 网页使用

打开 https://longcat-avatar.example.com ，使用 `info` 显示的账号密码登录。

1. 上传清晰人声音频（或点击网页中的官方示例）。
2. 上传人物参考图片；若不使用图片，切换到「音频＋描述」。
3. 描述人物外观、场景和动作，建议使用完整英文句子。
4. 保持默认 480p 和「按音频完整时长生成」勾选，点击「提交生成任务」。系统立即返回任务编号，完成后右侧可播放、下载。

默认读取完整音频时长并自动分段，页面显示音频秒数、预计段数和生成进度。官方示例约 82.04 秒，对应 26 段；末段按剩余时长缩短，最终去掉重叠帧和尾部补齐，输出覆盖完整音频并包含声音。完整长视频需要逐段生成，耗时会相应增加。

关闭「按音频完整时长生成」后，可手动设置帧数和段数以生成短片。每段 93 帧在 25 fps 下为 3.72 秒；每次续写去掉 13 帧重叠，因此 2 段约 6.92 秒、3 段约 10.12 秒。手动模式仅使用设定时长的音频。

此入口支持单人、单路音频。使用原始上传音频提取 Whisper 特征，不进行人声分离，因此建议上传无背景音乐的清晰人声。此版本的蒸馏模式按官方示例固定 text/audio guidance 为 1。

## 存储与环境

- 仅下载 Avatar BF16 DiT、DMD LoRA、Whisper 权重，以及基础模型共享的 T5、tokenizer、VAE；不下载基础版 DiT 或 INT8 的重复权重。
- 模型位于本地 SSD `/content/longcat-avatar/model`，不占用 Drive。
- 输出 MP4（H.264 + AAC）与参数 JSON 保存到 Google Drive：`colab-llm/longcat-avatar/output/`。
- 输入在本地转换为 16 kHz 单声道供 Whisper 使用，任务结束后清理转换文件。
- Python 3.11、PyTorch 2.11 / CUDA 13。单卡 SDPA 适配保留官方音频的分帧与 RoPE 处理。
- 启动会实际经过 Whisper → 音频条件 DiT（8 步）→ VAE，成功后 `/health` 才返回就绪。
- Caddy 提供 Basic Auth、Bearer API 和浏览器会话 Cookie；Gradio 的 `submit` API 提交持久任务，`GET /jobs/<任务编号>` 查询状态。后台只有一个 GPU worker，任务串行执行。

## 开发文件

- `services/longcat-avatar.py`、`services/longcat-avatar.ipynb`：服务入口与自包含 Notebook。
- `templates/longcat_avatar_app.py`：Avatar 应用与音频注意力适配。
- `templates/longcat_avatar_jobs.py`：持久后台任务、状态与重启恢复。
- `templates/longcat_avatar_drive.py`：网盘路径、上传缓存及目录列表校验。
- `templates/longcat_video_app.py`：共享基础注意力适配；生成器将两份源码一起嵌入 Notebook。
- `templates/build_notebooks.py` 中的 `LONGCAT_AVATAR_*`：复用 G4 环境、代理和缓存标准。
- `tests/test_longcat_avatar_attention.py`：GPU 回归测试，覆盖布局 / 缩放、音频帧隔离和边缘音频窗口。

远端日志：`/content/longcat-avatar/install.log`、`download.log`、`/content/ct/service.log`。

## 验证记录

2026-11-02，Colab G4：

- Whisper → 8 步音频条件 DiT → VAE 启动检查通过。
- 默认 93 帧、8 步、480p 尺寸桶生成成功：官方示例输出 768×512、3.72 秒有声视频，本次耗时约 80.9 秒。
- 音频＋图片、音频＋描述均完成 17 帧有声视频生成；MP4 的视频流为 25 fps，并含音轨。
- 两段各 17 帧成功衔接，去除 13 帧重叠后得到 21 帧有声视频。
- 无凭据 / 错误 Bearer 返回 401，Basic / Bearer 成功，Gradio 根地址为公网 HTTPS。
- 3 项 GPU 适配测试通过：BSHD 布局及缩放、帧间音频隔离、边缘窗口补齐。


## 关闭网页、查询与恢复

提交后生成任务由独立的服务端 worker 执行，关闭或刷新网页不取消任务。请保留返回的任务编号；重新打开网页后可从「历史任务」选择，或粘贴编号查询。输入、请求与状态保存在 Drive 的 `colab-llm/longcat-avatar/jobs/<任务编号>/`。

每段完成后保存片段、潜变量和随机数生成器状态。正常退出请求会等待已接收任务完成；进程异常退出后，下次启动会把运行中的任务重新排队并从最近检查点继续。尚未完成的当前段需要重算。若 Colab VM 被释放，需先重新启动服务并下载模型，之后才能续跑。

页面不再承担 GPU 计算任务的生命周期。视频与参数 JSON 仍保存到 `output/`。

后台任务升级验证：

- 未建立网页会话的后台任务可以完成有声视频生成。
- 活动任务收到正常退出信号后，服务进入等待状态并保留任务。
- 在第 1 段检查点保存后强制停止测试进程；重启后自动恢复第 2 段，最终生成完整 21 帧有声视频。
- 4.1 秒音频自动规划为两段，最终裁去尾部补齐，得到 103 帧、带音轨且覆盖完整音频的 MP4。

## 加速实验（2026-11-02）

在当前 G4 上，用真实 Avatar 1.5 第 0 个 DiT 块与 DMD LoRA 权重、固定随机输入测试。序列形状为 `(24, 32, 48)`，对应 480p 尺寸桶的 93 帧输入；每种路径预热后取 5 次 GPU 计时的中位数。

| 路径 | 单层耗时 |
| --- | ---: |
| 当前动态 LoRA | 197.0 ms |
| LoRA 合并到基础权重 | 168.2 ms |
| LoRA 合并 + torch.compile | 121.5 ms |

单层加速约 1.62 倍、耗时减少 38.3%。相对当前单层输出的相对 RMSE：融合 LoRA 0.114%，再编译后 0.179%。这不是视频像素误差或感知质量评分。

这些数据仅验证单层计算的潜力，不能直接等同整段视频速度或质量。测试中的调制层按官方 FP32 计算语义处理；激活值为合成输入。尚未做完整视频的画质、口型和长段衔接 A/B 验证，线上未启用这些优化。编译缓存已被先前试验预热，因此本次约 2.0 秒的首次调用不代表冷编译时间。

复现脚本：`tools/bench_longcat_avatar.py`，在当前 Avatar 环境的空闲 GPU 上执行；脚本在独立进程里加载一个块，不改在线模型。编译测试需将 SDPA 适配函数注册到原 attention 模块，以便 Dynamo 正确建立全局函数检查。

## 口型与采样设置

网页的「口型与采样设置」和 CLI 都支持两种模式：

- `distilled`（默认）：8 步，DMD LoRA 开启，文本和音频 CFG 固定 1.0。与已有任务兼容。
- `standard`：关闭 DMD LoRA，使用非蒸馏采样，默认 50 步、文本 CFG 4.0、音频 CFG 3.0。后者是本服务的实验起点，不是最佳口型保证；运行显著慢于快速模式。

音频 CFG 是音频引导强度，不是独立的嘴部幅度控制。降低后可能影响同步。当前适配要求文本和音频 CFG 均在 1–10，避免小于 1 时因上游 CFG 分支条件而不生效。快速模式拒绝不兼容的步数或 CFG，不会静默忽略。

```bash
./services/longcat-avatar.py generate \
  --audio speech.wav --image person.png --prompt "A person speaking calmly." \
  --sampling-mode standard --steps 30 --text-cfg 4 --audio-cfg 1 \
  --frames 49 --segments 1 -o cfg-1.mp4
```

对照实验应固定素材、提示词、步数和种子，只改变 `--audio-cfg`。上例是约 1.96 秒的短片实验；省略 `--frames` / `--segments` 则按完整音频生成。

`POST /jobs` 对应字段：`sampling_mode`、`steps`、`text_guidance_scale`、`audio_guidance_scale`。参数写入持久请求、输出 JSON 和任务结果，恢复任务时保持一致。旧请求缺少这些字段时继续使用快速模式。

实测记录（2026-11-02）：同一咖啡广告音频/图片、克制表情提示词、种子 42、480p、49 帧、30 步、文本 CFG 4.0，分别运行音频 CFG 1.0 和 3.0。两个任务均完成并输出带音轨的 1.96 秒 MP4，耗时分别为 346.9 和 346.8 秒。结果确认非蒸馏控制链路有效，但没有做口型同步或张嘴幅度的量化评测，不能据此认定某一 CFG 最佳。
