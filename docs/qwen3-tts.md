# Qwen3-TTS 操作说明

```bash
colabllm qwen3-tts up
colabllm qwen3-tts tts "给忙碌的早晨，留一点香气。" \
  --voice serena --lang Chinese -o speech.wav
```

使用项目全局主域名：`colabllm tunnel-config --base-domain example.com`。未配置时使用动态 URL。详见 [项目操作说明](../README.md#cloudflare-tunnel-配置)。

## 启动缓存优化

Qwen3-TTS 与 IndexTTS 共用归档缓存实现，但缓存数据保存在各自的服务目录中。以 L4 为例：

```text
我的云端硬盘/colab-llm/qwen3-tts/
  cache-archives/vllm-0.32.0-py312-sm89/
    flashinfer-cache.tar
    triton-cache.tar
    vllm-cache.tar
```

启动时先读取单个归档，再在本地 SSD 解包；推理编译仍使用本地 SSD。预热后重新打包新增缓存，上传临时文件成功后才替换旧归档。内容未变时不重复上传，同一 VM 的归档已恢复时不重复读取。

首次兼容旧版网盘缓存时仍需逐文件迁移，之后自动生成归档。恢复、迁移和保存每 10 秒输出进度。归档损坏时尝试旧目录回退；缓存恢复或保存失败会警告，服务继续使用本地缓存或按需编译。

不同 vLLM 版本和 GPU 架构分目录。相同架构的并发实例以最后成功上传的归档为准，缺少的缓存条目可重新生成。已注册音色及生成音频继续作为普通网盘文件保存。

此优化减少缓存传输耗时，不缓存 Python 虚拟环境和模型权重。新 VM 仍需安装 vLLM 环境、辅助 Qwen3-TTS 环境，下载模型并加载/预热。新版逻辑在下一次 `up` 时生效。
