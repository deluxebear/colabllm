# IndexTTS-2.5 操作说明

```bash
colabllm indextts up
colabllm indextts tts "你好，这是语音合成测试。" -o speech.wav
```

主域名在项目根目录统一设置：`colabllm tunnel-config --base-domain example.com`。未配置时使用动态 URL。详见 [项目操作说明](../README.md#cloudflare-tunnel-配置)。

## 启动与编译缓存

Setup 依次恢复缓存和已注册音色，再并行准备 Python 环境与模型。服务启动后加载两阶段模型并预热；JIT 缓存可命中时复用，否则按需生成。

编译缓存始终在本地 SSD 上运行，网盘只保存归档。以 L4 为例：

```text
我的云端硬盘/colab-llm/indextts/
  cache-archives/vllm-0.32.0-py312-sm89/
    flashinfer-cache.tar
    triton-cache.tar
    vllm-cache.tar
```

- 启动时每类缓存只从网盘读取一个 tar 文件，然后在本地解包，减少网盘逐文件操作。
- 旧版 `flashinfer-cache/`、`triton-cache/`、`vllm-cache/` 首次仍需迁移，之后自动保存归档；第一次迁移仍可能较慢。旧目录保留供回退。
- 每 10 秒输出恢复、迁移或保存进度；同一 VM 已恢复过的归档不重复读取，内容未变化时不重复上传。
- 预热后在本地打包，再上传临时文件并替换正式归档；上传失败保留旧归档。缓存错误会警告，服务继续使用本地缓存或重新生成。
- 不同 vLLM 版本和 GPU 架构使用不同目录。相同架构的并发实例最后成功保存的归档生效；缺少的条目可重新编译。
- 已注册音色和生成音频继续作为普通网盘文件保存，不放入编译缓存归档。

归档优化只减少编译缓存恢复/保存的耗时，不消除新 VM 的依赖安装、模型下载、模型加载以及必要的编译预热。当前运行中的旧版 notebook 不会热替换；新版恢复逻辑在下次 `up` 时生效。

验证记录：在真实 L4 Colab 会话中，将 577 个 Triton 文件（约 54 MB 数据）保存为约 55 MB tar，随后从网盘读取到独立本地目录并逐文件校验 SHA-256，全部一致。此次读取、解包和校验约 0.65 秒；测试发生在同一会话、归档刚写入之后，属于热缓存验证，不代表新 VM 的冷启动耗时。FlashInfer 和 vLLM 归档也已保存。
