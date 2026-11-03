# Changelog

版本号以 `pyproject.toml` 为唯一来源，使用 `MAJOR.MINOR.PATCH`；Git 标签为 `vMAJOR.MINOR.PATCH`。

## [Unreleased]

## [0.1.0] - 2026-11-03

首次打包发布。

- 提供统一 `colabllm` CLI，通过 `uv tool` 安装和升级，本机无需模型计算依赖。
- 包含 10 个 Colab 运行时，支持推理、OCR、转写、语音合成、视频生成、ComfyUI 和 Unsloth Studio。
- 用户配置和状态统一放在 `~/.colabllm/`，支持多实例、Cloudflare Tunnel 和 FRP。
- 提供认证信息查询、诊断、命令补全、任务恢复及超时控制。
- 随包分发 Agent skill，提供全部运行时的独立使用指南和同步检查。
- GitHub Actions 验证 Linux/macOS 与 Python 3.12–3.14，并自动构建和发布 wheel、sdist。
