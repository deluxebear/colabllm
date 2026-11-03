# LongCat Video：文生视频、图生视频与续写

默认 **G4**，Caddy Bearer/Basic Auth，提供 Gradio 网页与其公开的生成接口；与独立 Avatar 模型不同。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动和登录

```bash
colabllm longcat-video up --ready-timeout 3600
colabllm longcat-video status --json
colabllm longcat-video info --json
colabllm longcat-video info --show-secrets
colabllm longcat-video logs -n 100
```

从 `info` 的 URL 打开网页，使用 `auth_user/auth_pass` 登录；API 使用 `api_token`。
就绪前要完成模型加载和小尺寸推理预热，首次准备约 80 GB 级权重可能耗时较长。

## 网页生成流程

1. **文生视频**：选择该模式，填写镜头、人物、动作与场景描述，设置尺寸和种子，点击生成。
2. **图生视频**：上传本机参考图，选择模式及 480p/720p，描述希望的动作。
3. **视频续写**：上传参考视频，选择模式及分辨率。参考视频按 15 fps 采样后需至少 13 帧。
4. 生成后查看结果视频、下载控件和参数 JSON；保存 MP4 及元数据，不能仅交付临时网页链接。

默认快速模式启用 CFG/step LoRA，固定 16 步；标准模式默认 50 步、CFG 4。
每段默认 93 帧、15 fps，约 6.2 秒。帧数为 17–161 的 `4n+1`，宽高为 256–1024 的 16 倍数。
连续段数为 1–10，后续段复用末尾 13 帧并移除重叠。图生/续写及多段会使用分辨率尺寸桶，不一定等于手填尺寸。
种子 -1 随机；较大分辨率、更多帧/段增加 GPU 占用，先用短预览检查。

## API 与输出

应用的 Gradio API 名为 `generate`；从网页 API 页面读取**当前版本的参数顺序和类型**后调用。
不要把 Avatar 的 `colabllm longcat-avatar generate` 误认为本运行时命令，本运行时只有共享生命周期 CLI。
程序化调用需要在客户端附上 Caddy Bearer 或 Basic Auth，并按 Gradio 队列协议等待。
API 和 UI 共享串行生成队列，提交成功不等于生成完成。

生成的 MP4 与记录提示词、参数、种子的 JSON 写入
`MyDrive/colab-llm/longcat-video/output/`；网页上传的输入可能只是临时文件，需另行保存原素材。
模型权重位于 VM SSD `/content/longcat-video/model`；缓存可持久化，但停止 VM 后需重新安装/下载/加载部分环境。
离开网页不等于取消 GPU 生成，也不能据此提前停止 VM。下载后验证文件存在且能播放。

## 多实例与排错

```bash
colabllm longcat-video up -i 2
colabllm longcat-video info -i 2 --json
colabllm longcat-video ps --json
colabllm longcat-video logs -i 2 -f
colabllm longcat-video down -i 2
```

远端日志包括 `/content/ct/service.log` 及工作目录的安装/下载日志。
`/health` 成功表示已完成基础模型和小推理验证，不代表所有长视频/高分辨率参数都保证成功。
显存不足先减小分辨率、帧数或段数，不在有任务运行时反复重启。
