<!-- Generated from docs/longcat-avatar.md; edit the source and run tools/sync_runtime_docs.py. -->

# LongCat Avatar：音频驱动人物视频

默认 **G4**，Caddy Bearer/Basic Auth；独立 Avatar 1.5 模型，支持本机或 Drive 音频/图片及持久后台任务。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与本机生成

```bash
colabllm longcat-avatar up
colabllm longcat-avatar status --json
colabllm longcat-avatar generate --audio ./speech.wav --image ./person.png \
  --prompt 'A person speaking naturally in a bright room.' -o ./avatar.mp4
```

描述可用 `--prompt` 或 UTF-8 `--prompt-file`，二者选一。音频必需，图片可省略（按音频＋描述模式处理）。
本机素材上传到服务端，不是把本机路径直接交给模型。`-o` 下载本机文件，并使 generate/job 等到生成完成。
生成有内容、显存和耗时限制，建议先短片确认构图与动作。

## Drive 输入及异步任务

```bash
colabllm longcat-avatar ls-drive 'materials'
colabllm longcat-avatar generate --audio-drive 'materials/speech.wav' --image-drive 'materials/person.png' \
  --prompt-file ./prompt.txt --drive-output 'videos/avatar'
colabllm longcat-avatar job JOB_ID
colabllm longcat-avatar job JOB_ID --wait --timeout 600 -o ./avatar.mp4
colabllm longcat-avatar download JOB_ID -o ./avatar.mp4
```

不带 `-o/--wait` 的 generate 返回 JSON 中 `job_id`，不代表视频已完成。
Drive 输入可写 `drive://`、`gdrive://` 或远端完整路径；`--audio-drive/--image-drive` 也可直接填写 MyDrive 相对路径。
`--drive-output`（`--output-drive`）是远端输出目录，与 `-o` 本机下载不同，可同时使用。

任务保存输入快照，返回的 `result.path` / 元数据给出实际 Drive 文件。默认视频位于服务 Drive `output/`。
`--timeout 0` 默认不限等待，`--poll` 控制查询频率；Ctrl+C/超时只停止等待，已提交任务继续。
不要因为客户端断开重复生成，使用原 ID 查询或下载。failed/cancelled 状态应作为失败报告。

## 时长与采样

```bash
# 明确的短预览，93 帧 / 25 fps 约 3.72 秒
colabllm longcat-avatar generate --audio ./speech.wav --image ./person.png --prompt 'A person speaking.' \
  --frames 93 --segments 1 -o ./preview.mp4

# 标准模式才开放采样步数与 CFG
colabllm longcat-avatar generate --audio ./speech.wav --image ./person.png --prompt 'A person speaking.' \
  --sampling-mode standard --steps 50 --text-cfg 4 --audio-cfg 3 -o ./standard.mp4
```

不指定 `--frames/--segments` 默认覆盖完整音频；任意指定一个即切换手动时长模式（缺省另一项分别为 93 帧、1 段）。
每段帧数是 17–161 的 `4n+1`，段数 1–20；重叠拼接会去除重复帧，不能直接用“帧数×段数”估算总长度。
`distilled` 为固定 8 步快速模式，标准模式才调整 CFG；`--audio-cfg` 不是嘴部幅度滑块。
`--resolution` 为 480p/720p；`--seed -1` 随机。大分辨率、更多段会增加显存和时间。

## API、网页和管理

```bash
colabllm longcat-avatar up -i 2
colabllm longcat-avatar ps --json
colabllm longcat-avatar info --show-secrets
colabllm longcat-avatar logs -n 100
colabllm longcat-avatar down -i 2
```

网页使用 Basic Auth，CLI/API 使用 Bearer。接口为 `POST /jobs`、`GET /jobs/ID`、`GET /jobs/ID/video` 和 Drive 列目录接口。
只公开生成结果和允许的文件，不应为了下载把整份 Drive 暴露为静态目录。
跨机器可设 `LONGCAT_AVATAR_URL` / `LONGCAT_AVATAR_TOKEN`，或 `--url/--token`。
基础 `longcat-video` 没有这里的 generate/job/download CLI，不要混用。
