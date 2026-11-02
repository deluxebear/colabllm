# LongCat-Video（Colab G4）

> Tunnel 域名说明：以下固定域名示例需先设置主域名，例如 `./colabctl.py tunnel-config --base-domain example.com`（所有服务共享）。未配置时使用动态 URL；`up --base-domain example.com` 可单次覆盖，`up --quick-tunnel` 强制动态。详见 [Tunnel 配置](../README.md#cloudflare-tunnel-配置)。

基于 [meituan-longcat/LongCat-Video](https://github.com/meituan-longcat/LongCat-Video)，部署基础视频生成模型，提供带认证的 Gradio 网页：文生视频、图生视频、视频续写，以及多段连续生成。此服务不加载独立的 Avatar 模型。

```bash
./services/longcat-video.py up --domain   # 默认 G4，https://longcat-video.example.com
./services/longcat-video.py info          # 查看地址、登录账号、密码、API Token
./services/longcat-video.py status
./services/longcat-video.py logs -f
./services/longcat-video.py down

# 多实例与 GPU 覆盖仍使用共享运行库的标准参数
./services/longcat-video.py up -i 2 --domain --gpu G4
```

默认快速模式启用官方 CFG/step LoRA，16 步生成 93 帧、15 fps 视频（约 6.2 秒）。标准模式默认 50 步、CFG 4；随机种子设为 -1 可随机生成。连续生成会用上一段末尾 13 帧作为下一段的条件，拼接时去除重叠帧。多段文生视频会根据所选 480p / 720p 分辨率和宽高比例使用官方尺寸桶，使各段尺寸一致。

## 运行环境

- 默认 G4，单 GPU BF16 推理；上游源码固定在 `6b3f4b8582a8bc3f20f795735f5383716c4ba794`。
- 独立 Python 3.11 环境，PyTorch 2.11 / CUDA 13，支持 Blackwell；不用上游面向旧硬件的 PyTorch 2.6 / CUDA 12.4 固定依赖。
- 原生 PyTorch 融合 SDPA 代替上游 FlashAttention-2。自注意力保持原有缩放，交叉注意力按每个样本的真实文本长度分别计算，保留变长掩码语义。部署仅支持单 GPU。
- 初次安装会并行下载约 80 GB 权重与安装环境；不下载此服务未使用的 refinement LoRA。权重全部存放在本地 SSD `/content/longcat-video/model`。
- 生成的 MP4 和记录提示词、参数、种子的 JSON 写入 `/content/drive/MyDrive/colab-llm/longcat-video/output`。输入文件由 Gradio 临时保存，不保证跨会话持久化。
- Triton / Inductor 缓存先在本地 SSD 使用，并在就绪后增量同步到 Drive。
- 模型加载并成功执行 256×256、9 帧、1 步推理后，`/health` 才返回成功；这是基础推理检查，不代表所有分辨率和长视频参数都已验证。

## 认证与接口

服务只监听 `127.0.0.1:7860`，公网经 Caddy `:18080`：浏览器 Basic Auth，API 使用 `Authorization: Bearer <API_TOKEN>`。凭据在本地 `.state/longcat-video/state.json`，不写入 Notebook。

带认证访问 `/health` 返回模型和 GPU 信息。Gradio 的 API 页面提供 `generate` 接口参数说明；API 与网页共享串行生成队列，避免并发争用显存。

## 文件与日志

- [services/longcat-video.py](../services/longcat-video.py)：会话、认证、隧道和保活入口。
- [services/longcat-video.ipynb](../services/longcat-video.ipynb)：自包含部署 Notebook。
- [templates/longcat_video_app.py](../templates/longcat_video_app.py)：Gradio 应用及 SDPA 适配器；生成器将源码嵌入 Notebook。
- `templates/build_notebooks.py`：`LONGCAT_VIDEO_*` 部署单元。
- 远端日志：`/content/longcat-video/install.log`、`download.log`、`/content/ct/service.log`。

修改应用或部署单元后运行 `python templates/build_notebooks.py`，再启动服务。大分辨率、更多帧和更多段会增加显存与生成时间；默认不启用超分辨率或 BSA refinement。

## 已验证

2026-11-02 在 Colab G4（RTX PRO 6000 Blackwell、约 96 GB 显存）完成验证：

- 无凭据 / 错误 Bearer 返回 401，且不返回会话 Cookie；Basic Auth、Bearer、登录后 Cookie 均可用。
- Gradio 配置及视频文件链接使用公网 HTTPS，带认证可下载有效 MP4。
- 默认 832×480、93 帧、16 步快速文生视频成功，本次耗时约 145 秒（6.2 秒视频；单次样本，不是性能保证）。
- 256×256、17 帧、16 步快速文生视频成功；480p 尺寸桶下的 17 帧图生视频与视频续写成功。
- 两段各 17 帧连续生成成功，去除 13 帧重叠后输出 21 帧，尺寸保持一致。
- GPU 回归测试覆盖自注意力的不同查询 / 缓存长度，以及打包文本的不同长度与样本隔离。

GPU 测试命令（在仓库目录执行，环境需能导入上游 LongCat-Video）：

```bash
PYTHONPATH=/content/longcat-video/repo \
  /content/longcat-video/venv/bin/python tests/test_longcat_attention.py
```
