<!-- Generated from docs/unsloth-studio.md; edit the source and run tools/sync_runtime_docs.py. -->

# Unsloth Studio：训练、推理与模型导出

默认 **L4**，使用 Unsloth Studio 自带登录，监听 Colab 内部端口 8888，通过选定隧道发布网页。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动和账号

```bash
colabllm unsloth-studio up
colabllm unsloth-studio status --json
colabllm unsloth-studio info --json
colabllm unsloth-studio info --show-secrets
colabllm unsloth-studio logs -n 100
```

从返回的 URL 打开 Studio，使用实际 `login_user/login_pass`（若应用已报告）或其 UI 登录流程。
不要把其他 Caddy 服务的账号字段或随机 `api_token` 当成 Studio 必定接受的 API Key。
当前 notebook 启动 Studio 后才可能产生应用凭据，启动标记与登录字段的出现时间不同；界面不可用时继续查日志。

## 微调流程

1. 在界面选择基础模型与训练任务；受限模型需要有权限的 Hugging Face 账号。
2. 导入数据集并核对列映射、聊天模板、训练/验证拆分和少量样本，避免错误格式进入长训练。
3. 设置 LoRA/QLoRA、序列长度、批大小、梯度累积和输出位置；内存需求由模型及设置决定，L4 不适合所有模型。
4. 先小步训练验证 loss、显存及输出，再按用户要求运行完整训练。
5. 保存 checkpoint、adapter 和配置，确认写入持久目录；需要合并或量化时在 UI 明确选择导出格式。

受限仓库的 Token 可通过已设置的 `HF_TOKEN` 由启动器读取；避免把实际 Token 写进命令历史或文档。
安装/下载失败时先确认权限、网络和剩余磁盘，不反复新建 VM。

## 推理与导出

Studio 的推理和训练工作流在 Web UI 中操作。若其版本公开可自动化接口，先读取实际接口说明；
项目没有 `colabllm unsloth-studio train/chat/export` 子命令。
推理模型、adapter 和 tokenizer 要与训练配置匹配，不能把仅 LoRA adapter 当作完整基础模型。
导出到 `MyDrive/colab-llm/unsloth-studio/` 下的用户目录，并记录基础模型版本、adapter、tokenizer 和训练配置。
默认工作目录或 UI 临时下载不保证持久，停止前确认 Drive 上实际存在所需文件，必要时下载本机副本。

## 实例、较大 GPU 与停止

```bash
colabllm unsloth-studio up -i 2 --gpu A100
colabllm unsloth-studio info -i 2 --json
colabllm unsloth-studio ps --json
colabllm unsloth-studio logs -i 2 -f
colabllm unsloth-studio down -i 2
```

GPU 覆盖须匹配用户额度和模型需求。不同 VM 独立运行，但可能共享同一个 Drive 服务目录，
不同实验应设置不同输出子目录，防止覆盖 checkpoint。
本机 runner 和 VM 都需存活到训练/导出结束；关闭网页不代表训练取消。`down` 会释放 VM，
恢复能力取决于实际保存的 checkpoint 及 UI 支持，不能承诺训练自动续跑。
