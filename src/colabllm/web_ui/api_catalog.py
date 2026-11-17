"""Runtime HTTP API catalog and Copilot Studio surface discovery."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

CAPABILITY_SURFACES: tuple[dict[str, str], ...] = (
    {
        "id": "chat",
        "label": "对话与推理",
        "short_label": "Chat",
        "desc": "OpenAI /v1/chat/completions、/v1/responses、Reasoning 与工具调用",
    },
    {
        "id": "image",
        "label": "图像生成与编辑",
        "short_label": "Image",
        "desc": "OpenAI /v1/images/generations、/edits、参考图与异步 4K 任务",
    },
    {
        "id": "audio",
        "label": "语音与音效合成",
        "short_label": "Audio",
        "desc": "OpenAI /v1/audio/speech、音色克隆与音效生成",
    },
    {
        "id": "transcribe",
        "label": "音频转写与理解",
        "short_label": "ASR",
        "desc": "SALM 音文对话、CTC 时间戳与 /v1/audio/transcriptions",
    },
    {
        "id": "embeddings",
        "label": "向量嵌入",
        "short_label": "Embed",
        "desc": "OpenAI /v1/embeddings、批量向量与余弦相似度矩阵",
    },
    {
        "id": "specialized",
        "label": "专有 API 控制台",
        "short_label": "Native",
        "desc": "各运行时原生 REST / Gradio / SSE 工作流与任务控制接口",
    },
    {
        "id": "inspector",
        "label": "同源 HTTP 探针",
        "short_label": "HTTP",
        "desc": "任意 HTTP 方法、JSON/Multipart 请求、流式响应调试与 SDK 代码生成",
    },
)

SURFACE_IDS: tuple[str, ...] = tuple(item["id"] for item in CAPABILITY_SURFACES)


def _llama_presets(default_model: str, *, include_effort: bool = False) -> list[dict[str, Any]]:
    """Build standard llama-server OpenAI + Anthropic preset requests."""
    chat_body: dict[str, Any] = {
        "model": default_model,
        "messages": [{"role": "user", "content": "你好，请简要介绍你的能力。"}],
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    if include_effort:
        chat_body["reasoning_effort"] = "medium"
    return [
        {"label": "模型列表 (/v1/models)", "method": "GET", "path": "v1/models"},
        {"label": "服务属性 (/props)", "method": "GET", "path": "props"},
        {"label": "健康检查 (/health)", "method": "GET", "path": "health"},
        {
            "label": "Chat Completions",
            "method": "POST",
            "path": "v1/chat/completions",
            "body": chat_body,
        },
        {
            "label": "Text Completions",
            "method": "POST",
            "path": "v1/completions",
            "body": {"model": default_model, "prompt": "def fibonacci(n: int) -> int:\n", "max_tokens": 128},
        },
        {
            "label": "Anthropic Messages",
            "method": "POST",
            "path": "v1/messages",
            "body": {
                "model": default_model,
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "用一句话解释同源代理。"}],
            },
        },
    ]


def _image_presets(default_model: str, *, async_jobs: bool = False, upsample: bool = False) -> list[dict[str, Any]]:
    """Build OpenAI image + /api/ct/* preset requests."""
    presets: list[dict[str, Any]] = [
        {"label": "模型列表 (/v1/models)", "method": "GET", "path": "v1/models"},
        {"label": "运行时信息 (/api/ct/info)", "method": "GET", "path": "api/ct/info"},
        {"label": "Drive 文件列表", "method": "GET", "path": "api/ct/drive-list"},
        {"label": "近期推理日志", "method": "GET", "path": "api/ct/logs"},
        {
            "label": "OpenAI 文生图 (/v1/images/generations)",
            "method": "POST",
            "path": "v1/images/generations",
            "body": {
                "model": default_model,
                "prompt": "A serene lakeside cabin at sunrise, crisp architectural lighting, 35mm film",
                "response_format": "b64_json",
            },
        },
    ]
    if upsample:
        presets.append(
            {
                "label": "PUP 提示词扩写 (/api/ct/upsample)",
                "method": "POST",
                "path": "api/ct/upsample",
                "body": {"prompt": "a cyberpunk street cat with neon glasses"},
            }
        )
    if async_jobs:
        presets.extend(
            [
                {
                    "label": "提交 4K 异步生图任务 (/api/ct/jobs)",
                    "method": "POST",
                    "path": "api/ct/jobs",
                    "body": {
                        "prompt": "Minimalist industrial product render of a ceramic espresso machine",
                        "aspect": "1:1",
                        "tier": "4k",
                    },
                },
                {"label": "异步任务队列 (/api/ct/jobs)", "method": "GET", "path": "api/ct/jobs"},
            ]
        )
    return presets


def _audio_tts_presets(default_model: str, *, extras: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build OpenAI TTS + voice clone management presets."""
    presets: list[dict[str, Any]] = [
        {"label": "模型列表 (/v1/models)", "method": "GET", "path": "v1/models"},
        {"label": "已保存音色列表 (/v1/audio/voices)", "method": "GET", "path": "v1/audio/voices"},
        {"label": "运行时信息 (/api/ct/info)", "method": "GET", "path": "api/ct/info"},
        {"label": "Drive 音频文件 (/api/ct/drive-list)", "method": "GET", "path": "api/ct/drive-list"},
        {
            "label": "语音合成 (/v1/audio/speech)",
            "method": "POST",
            "path": "v1/audio/speech",
            "body": {
                "model": default_model,
                "input": "你好，欢迎使用 ColabLLM Copilot Studio。",
                "voice": "default",
                "response_format": "wav",
            },
        },
        {"label": "近期推理日志 (/api/ct/logs)", "method": "GET", "path": "api/ct/logs"},
    ]
    if extras:
        presets.extend(extras)
    return presets


RUNTIME_API_PROFILES: dict[str, dict[str, Any]] = {
    "qwen-flash": {
        "primary_surface": "chat",
        "surfaces": ["chat", "inspector"],
        "openai_compatible": True,
        "default_model": "qwen3.8-flash-next",
        "api_style": "llama-server",
        "health_path": "health",
        "models_path": "v1/models",
        "features": {
            "streaming": True,
            "thinking": True,
            "thinking_param": "chat_template_kwargs",
            "tools": True,
            "vision": True,
            "audio_input": False,
            "responses_api": False,
            "completions_api": True,
            "embeddings": False,
            "anthropic_messages": True,
            "props_endpoint": True,
        },
        "suggestions": [
            "检查当前在线实例并在聊天里生成舰队概览卡片",
            "开启深度思考模式，分析一段复杂的系统日志",
            "使用表格对比各图生图运行时的参考图与步数差异",
        ],
        "endpoint_presets": _llama_presets("qwen3.8-flash-next"),
    },
    "deepseek-coder": {
        "primary_surface": "chat",
        "surfaces": ["chat", "inspector"],
        "openai_compatible": True,
        "default_model": "deepseek-v4.1-flash-coder",
        "api_style": "llama-server",
        "health_path": "health",
        "models_path": "v1/models",
        "features": {
            "streaming": True,
            "thinking": True,
            "thinking_param": "reasoning_effort",
            "reasoning_effort": True,
            "reasoning_budget": True,
            "tools": True,
            "vision": True,
            "audio_input": False,
            "responses_api": False,
            "completions_api": True,
            "embeddings": False,
            "anthropic_messages": True,
            "props_endpoint": True,
        },
        "suggestions": [
            "编写一个带流式 SSE 解析的 Python OpenAI 客户端脚本",
            "查看当前所有可用运行时并生成能力矩阵卡片",
            "开启高推理强度 (reasoning_effort=high) 排查竞态 Bug",
        ],
        "endpoint_presets": _llama_presets("deepseek-v4.1-flash-coder", include_effort=True),
    },
    "deepseek-qwen-9b": {
        "primary_surface": "chat",
        "surfaces": ["chat", "inspector"],
        "openai_compatible": True,
        "default_model": "deepseek-v4-pro-qwen3.5-9b",
        "api_style": "llama-server",
        "health_path": "health",
        "models_path": "v1/models",
        "features": {
            "streaming": True,
            "thinking": True,
            "thinking_param": "chat_template_kwargs",
            "tools": True,
            "vision": True,
            "audio_input": False,
            "responses_api": False,
            "completions_api": True,
            "embeddings": False,
            "anthropic_messages": True,
            "props_endpoint": True,
        },
        "suggestions": [
            "用简洁步骤说明如何在本机配置 colabllm 同源代理",
            "生成当前运行时的关键指标卡片",
            "上传图片并测试多模态视觉理解能力",
        ],
        "endpoint_presets": _llama_presets("deepseek-v4-pro-qwen3.5-9b"),
    },
    "ollama": {
        "primary_surface": "chat",
        "surfaces": ["chat", "embeddings", "specialized", "inspector"],
        "openai_compatible": True,
        "default_model": "",
        "api_style": "ollama",
        "health_path": "api/version",
        "models_path": "v1/models",
        "features": {
            "streaming": True,
            "thinking": True,
            "tools": True,
            "vision": True,
            "audio_input": False,
            "responses_api": True,
            "completions_api": True,
            "embeddings": True,
            "anthropic_messages": True,
            "ollama_native": True,
            "model_management": True,
        },
        "suggestions": [
            "探测已拉取模型并列出当前显存驻留状态",
            "使用 /v1/responses 接口完成多轮推理任务",
            "切换到「向量嵌入」面板测试语义相似度计算",
        ],
        "endpoint_presets": [
            {"label": "OpenAI 模型列表 (/v1/models)", "method": "GET", "path": "v1/models"},
            {"label": "Ollama 版本 (/api/version)", "method": "GET", "path": "api/version"},
            {"label": "本地模型列表 (/api/tags)", "method": "GET", "path": "api/tags"},
            {"label": "运行中模型 (/api/ps)", "method": "GET", "path": "api/ps"},
            {
                "label": "OpenAI Responses (/v1/responses)",
                "method": "POST",
                "path": "v1/responses",
                "body": {"model": "", "input": "用三点概括同源反向代理的价值。", "stream": False},
            },
            {
                "label": "OpenAI Embeddings (/v1/embeddings)",
                "method": "POST",
                "path": "v1/embeddings",
                "body": {"model": "", "input": ["ColabLLM 同源代理", "Ollama 本地模型"]},
            },
            {
                "label": "拉取模型 (/api/pull)",
                "method": "POST",
                "path": "api/pull",
                "body": {"model": "qwen3:8b", "stream": True},
            },
        ],
    },
    "flux-klein": {
        "primary_surface": "image",
        "surfaces": ["image", "chat", "inspector"],
        "openai_compatible": True,
        "default_model": "flux-klein",
        "api_style": "openai-image",
        "health_path": "api/ct/info",
        "models_path": "v1/models",
        "features": {
            "image_generation": True,
            "image_edit": True,
            "max_refs": 10,
            "default_steps": 16,
            "flow_shift": 6.93,
            "pup_upsample": True,
            "chat_image_shim": True,
            "drive_io": True,
            "supports_negative_prompt": False,
            "supports_cfg": False,
        },
        "suggestions": [
            "生成一张未来主义玻璃温室建筑摄影图",
            "使用 PUP 扩写简短提示词后再出图",
            "上传最多 10 张参考图进行多图融合编辑",
        ],
        "endpoint_presets": _image_presets("flux-klein", upsample=True),
    },
    "qwen-image": {
        "primary_surface": "image",
        "surfaces": ["image", "chat", "inspector"],
        "openai_compatible": True,
        "default_model": "qwen-image",
        "api_style": "openai-image",
        "health_path": "api/ct/info",
        "models_path": "v1/models",
        "features": {
            "image_generation": True,
            "image_edit": True,
            "max_refs": 4,
            "ref_tag_style": "<imageN>",
            "default_steps": 40,
            "supports_cfg": True,
            "supports_negative_prompt": True,
            "chat_image_shim": True,
            "drive_io": True,
        },
        "suggestions": [
            "生成带中文字体排版设计感的海报插画",
            "用 <image1> 和 <image2> 进行双图人物场景融合编辑",
            "调节 true_cfg_scale 与 negative_prompt 对比出图风格",
        ],
        "endpoint_presets": _image_presets("qwen-image"),
    },
    "minimax-h3-image": {
        "primary_surface": "image",
        "surfaces": ["image", "inspector"],
        "openai_compatible": True,
        "default_model": "minimax-h3-image",
        "api_style": "openai-image-async-4k",
        "health_path": "api/ct/info",
        "models_path": "v1/models",
        "features": {
            "image_generation": True,
            "image_edit": True,
            "max_refs": 16,
            "ref_tag_style": "<Picture N>",
            "default_steps": 6,
            "fixed_steps": 6,
            "async_jobs": True,
            "tiers": ["4k", "2k", "1k"],
            "aspect_presets": ["auto", "1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9", "9:21"],
            "drive_io": True,
            "rejects_negative_prompt": True,
            "rejects_lora": True,
        },
        "suggestions": [
            "提交一张 16:9 4K 级产品商业静物异步出图任务",
            "上传最多 16 张参考图并用 <Picture 1> 标签编排构图",
            "检查异步任务队列并下载 WebP/PNG 成品",
        ],
        "endpoint_presets": _image_presets("minimax-h3-image", async_jobs=True),
    },
    "qwen3-tts": {
        "primary_surface": "audio",
        "surfaces": ["audio", "inspector"],
        "openai_compatible": True,
        "default_model": "qwen3-tts",
        "api_style": "openai-audio-tts",
        "health_path": "api/ct/info",
        "models_path": "v1/models",
        "features": {
            "tts": True,
            "voice_clone": True,
            "voice_design": True,
            "sfx": False,
            "voices_crud": True,
            "drive_io": True,
        },
        "suggestions": [
            "用自然中文女声合成一段产品开场旁白",
            "通过文字描述设计一个全新的自定义音色 (/api/ct/design-voice)",
            "上传参考音频并管理持久化音色库",
        ],
        "endpoint_presets": _audio_tts_presets(
            "qwen3-tts",
            extras=[
                {
                    "label": "文本描述捏音色 (/api/ct/design-voice)",
                    "method": "POST",
                    "path": "api/ct/design-voice",
                    "body": {
                        "name": "calm_narrator",
                        "consent": True,
                        "voice_description": "温润沉稳的青年男声，语速适中，适合纪录片旁白",
                        "sample_text": "你好，这是一段通过文本描述生成的参考音色。",
                    },
                }
            ],
        ),
    },
    "indextts": {
        "primary_surface": "audio",
        "surfaces": ["audio", "inspector"],
        "openai_compatible": True,
        "default_model": "indextts",
        "api_style": "openai-audio-tts",
        "health_path": "api/ct/info",
        "models_path": "v1/models",
        "features": {
            "tts": True,
            "voice_clone": True,
            "emotion_vector_8d": True,
            "emo_alpha": True,
            "emo_text": True,
            "voices_crud": True,
            "drive_io": True,
        },
        "suggestions": [
            "上传一段干声样本并克隆目标说话人音色",
            "调节 8 维情绪向量（喜/怒/哀/惧等）测试情感配音",
            "开启 emo_text 由文本情绪自动驱动语调起伏",
        ],
        "endpoint_presets": _audio_tts_presets("indextts"),
    },
    "drama3": {
        "primary_surface": "audio",
        "surfaces": ["audio", "inspector"],
        "openai_compatible": True,
        "default_model": "drama3",
        "api_style": "openai-audio-tts",
        "health_path": "api/ct/info",
        "models_path": "v1/models",
        "features": {
            "tts": True,
            "voice_clone": True,
            "inline_tags": ["[sighs]", "[laughs]", "[whispers]", "[gasp]", "[pause]"],
            "multi_reference_dialogue": True,
            "batch_speech": True,
            "voices_crud": True,
            "drive_io": True,
        },
        "suggestions": [
            "在文本中插入 [whispers] 与 [laughs] 生成戏剧感对白",
            "配置多角色参考音频并测试批量分段配音 (/v1/audio/speech/batch)",
            "管理已克隆的角色音色库并试听 WAV 音频",
        ],
        "endpoint_presets": _audio_tts_presets(
            "drama3",
            extras=[
                {
                    "label": "批量对话合成 (/v1/audio/speech/batch)",
                    "method": "POST",
                    "path": "v1/audio/speech/batch",
                    "body": {
                        "model": "drama3",
                        "items": [
                            {"input": "[whispers] 听见了吗？系统已经就绪了。", "voice": "default"},
                            {"input": "[laughs] 太棒了，我们马上开始测试。", "voice": "default"},
                        ],
                    },
                }
            ],
        ),
    },
    "tangoflux": {
        "primary_surface": "audio",
        "surfaces": ["audio", "inspector"],
        "openai_compatible": True,
        "default_model": "tangoflux",
        "api_style": "openai-audio-sfx",
        "health_path": "api/ct/info",
        "models_path": "v1/models",
        "features": {
            "tts": False,
            "sfx": True,
            "duration_range": [1, 30],
            "sample_rate": 44100,
            "stereo": True,
            "batch_sfx": True,
            "drive_io": True,
        },
        "suggestions": [
            "生成一段 10 秒 44.1kHz 立体声雨夜木屋壁炉音效",
            "对比 /v1/audio/generations 单条二进制返回与批量 JSON 返回",
            "生成机械键盘高速敲击声并直接将结果保存到 Drive",
        ],
        "endpoint_presets": [
            {"label": "模型列表 (/v1/models)", "method": "GET", "path": "v1/models"},
            {"label": "运行时信息 (/api/ct/info)", "method": "GET", "path": "api/ct/info"},
            {
                "label": "音效生成 (/v1/audio/generations)",
                "method": "POST",
                "path": "v1/audio/generations",
                "body": {
                    "model": "tangoflux",
                    "prompt": "Heavy rain falling on a wooden roof with distant thunder and fireplace crackling",
                    "duration": 10.0,
                    "steps": 25,
                },
            },
            {"label": "Drive 列表 (/api/ct/drive-list)", "method": "GET", "path": "api/ct/drive-list"},
        ],
    },
    "nemotron-transcribe": {
        "primary_surface": "transcribe",
        "surfaces": ["transcribe", "chat", "inspector"],
        "openai_compatible": True,
        "default_model": "nemotron-transcribe",
        "api_style": "openai-audio-salm",
        "health_path": "api/ct/prompts",
        "models_path": "v1/models",
        "features": {
            "audio_input": True,
            "salm_chat_audio": True,
            "ctc_timestamps": True,
            "thinking": True,
            "prompt_presets": True,
            "drive_transcribe": True,
            "ytdlp_download": True,
            "no_audio_transcriptions_endpoint": True,
        },
        "suggestions": [
            "上传本地音频并通过 /v1/chat/completions 获取文字与 CTC 字/段级时间戳",
            "从 Google Drive 音频路径批量执行长音频转写",
            "拉取内置任务提示词模板 (/api/ct/prompts) 并做跨语种翻译问答",
        ],
        "endpoint_presets": [
            {"label": "模型列表 (/v1/models)", "method": "GET", "path": "v1/models"},
            {"label": "内置提示词 (/api/ct/prompts)", "method": "GET", "path": "api/ct/prompts"},
            {"label": "Drive 音视频列表 (/api/ct/drive-list)", "method": "GET", "path": "api/ct/drive-list"},
            {
                "label": "Drive 批量转写 (/api/ct/drive-transcribe)",
                "method": "POST",
                "path": "api/ct/drive-transcribe",
                "body": {"drive_path": "drive://audio/sample.wav", "capture_ctc_timestamps": True},
            },
        ],
    },
    "unlimited-ocr": {
        "primary_surface": "specialized",
        "surfaces": ["specialized", "inspector"],
        "openai_compatible": False,
        "default_model": "unlimited-ocr",
        "api_style": "unlimited-ocr",
        "health_path": "health",
        "models_path": "",
        "features": {
            "chunked_upload": True,
            "max_body_bytes": 1048576,
            "async_jobs": True,
            "drive_list": True,
            "web_ui": True,
        },
        "suggestions": [
            "查看 /health 状态并浏览 Drive 上的 PDF 与扫描件目录",
            "分片上传本地 PDF/图片并发起 PaddleOCR-VL 识别任务",
            "查询历史任务状态、断点续传并导出 Markdown/JSON 结果",
        ],
        "endpoint_presets": [
            {"label": "健康检查 (/health)", "method": "GET", "path": "health"},
            {"label": "列出 Drive 目录 (/api/drive-list)", "method": "POST", "path": "api/drive-list", "body": {"path": ""}},
            {"label": "提交 OCR 任务 (/api/jobs)", "method": "POST", "path": "api/jobs", "body": {"source": ""}},
        ],
    },
    "longcat-avatar": {
        "primary_surface": "specialized",
        "surfaces": ["specialized", "inspector"],
        "openai_compatible": False,
        "default_model": "longcat-avatar",
        "api_style": "longcat-avatar",
        "health_path": "health",
        "models_path": "",
        "features": {
            "gradio_upload": True,
            "async_jobs": True,
            "video_output": True,
            "drive_list": True,
        },
        "suggestions": [
            "查看 Drive 可用数字人驱动素材 (/drive/files)",
            "提交人像+音频驱动任务并轮询生成数字人说话视频",
        ],
        "endpoint_presets": [
            {"label": "健康检查 (/health)", "method": "GET", "path": "health"},
            {"label": "Drive 文件列表 (/drive/files)", "method": "GET", "path": "drive/files"},
            {
                "label": "提交数字人任务 (/jobs)",
                "method": "POST",
                "path": "jobs",
                "body": {"prompt": "A presenter speaking naturally to the camera", "resolution": "480p"},
            },
        ],
    },
    "longcat-video": {
        "primary_surface": "specialized",
        "surfaces": ["specialized", "inspector"],
        "openai_compatible": False,
        "default_model": "longcat-video",
        "api_style": "gradio-queue",
        "health_path": "health",
        "models_path": "",
        "features": {
            "gradio_upload": True,
            "gradio_sse": True,
            "video_output": True,
            "web_ui": True,
        },
        "suggestions": [
            "检查 Gradio 视频服务健康状态并触发视频生成 SSE 队列",
            "通过同源探针上传首帧参考图 (/gradio_api/upload)",
        ],
        "endpoint_presets": [
            {"label": "健康检查 (/health)", "method": "GET", "path": "health"},
            {
                "label": "提交 Gradio 生成队列 (/gradio_api/call/generate)",
                "method": "POST",
                "path": "gradio_api/call/generate",
                "body": {"data": ["A golden retriever running on a sunlit beach, cinematic 24fps"]},
            },
        ],
    },
    "comfyui": {
        "primary_surface": "specialized",
        "surfaces": ["specialized", "inspector"],
        "openai_compatible": False,
        "default_model": "comfyui",
        "api_style": "comfyui",
        "health_path": "system_stats",
        "models_path": "",
        "features": {
            "workflow_graph": True,
            "object_info": True,
            "upload_image": True,
            "queue": True,
            "pull_workflow": True,
            "ssd_status": True,
            "web_ui": True,
        },
        "suggestions": [
            "检查 GPU 显存与本地 SSD 模型缓存 (/system_stats & /api/ct/ssd-status)",
            "提交 ComfyUI API Workflow JSON 到 /prompt 并查看队列与产物",
            "从外部链接拉取工作流资源 (/api/ct/pull-workflow)",
        ],
        "endpoint_presets": [
            {"label": "系统与显存 (/system_stats)", "method": "GET", "path": "system_stats"},
            {"label": "SSD 缓存状态 (/api/ct/ssd-status)", "method": "GET", "path": "api/ct/ssd-status"},
            {"label": "当前任务队列 (/queue)", "method": "GET", "path": "queue"},
            {"label": "节点类型清单 (/object_info)", "method": "GET", "path": "object_info"},
            {"label": "中断当前生成 (/interrupt)", "method": "POST", "path": "interrupt", "body": {}},
        ],
    },
    "openjutsu": {
        "primary_surface": "specialized",
        "surfaces": ["specialized", "inspector"],
        "openai_compatible": False,
        "default_model": "openjutsu",
        "api_style": "openjutsu",
        "health_path": "api/models",
        "models_path": "api/models",
        "features": {
            "model_setup": True,
            "multipart_jobs": True,
            "sse_events": True,
            "video_output": True,
            "web_ui": True,
        },
        "suggestions": [
            "列出可选视频模型权重与下载状态 (/api/models)",
            "提交文本/参考视频生成任务并订阅 SSE 实时进度流",
        ],
        "endpoint_presets": [
            {"label": "可用模型状态 (/api/models)", "method": "GET", "path": "api/models"},
            {"label": "任务列表与提交 (/api/jobs)", "method": "GET", "path": "api/jobs"},
        ],
    },
    "r2-migrate": {
        "primary_surface": "specialized",
        "surfaces": ["specialized", "inspector"],
        "openai_compatible": False,
        "default_model": "r2-migrate",
        "api_style": "r2-migrate",
        "health_path": "health",
        "models_path": "",
        "features": {
            "credentials": True,
            "buckets": True,
            "usage": True,
            "async_jobs": True,
            "job_controls": True,
            "drive_list": True,
            "web_ui": True,
        },
        "suggestions": [
            "查询已保存 R2 凭证列表并枚举可用存储桶",
            "查看当前 Drive 目录并创建/暂停/恢复迁移任务",
        ],
        "endpoint_presets": [
            {"label": "健康检查 (/health)", "method": "GET", "path": "health"},
            {"label": "已保存凭证 (/api/credentials)", "method": "GET", "path": "api/credentials"},
            {"label": "迁移任务列表 (/api/jobs)", "method": "GET", "path": "api/jobs"},
            {"label": "Drive 根目录 (/api/drive-list)", "method": "POST", "path": "api/drive-list", "body": {"path": ""}},
        ],
    },
    "unsloth-studio": {
        "primary_surface": "specialized",
        "surfaces": ["specialized", "inspector"],
        "openai_compatible": False,
        "default_model": "unsloth-studio",
        "api_style": "unsloth-studio",
        "health_path": "api/health",
        "models_path": "",
        "features": {
            "jwt_login": True,
            "auto_login_proxy": True,
            "auth_status": True,
            "web_ui": True,
        },
        "suggestions": [
            "测试同源后端自动注入凭据并登录 Unsloth Studio (/api/auth/login)",
            "检查 Unsloth Studio 运行状态与认证状态",
        ],
        "endpoint_presets": [
            {"label": "健康检查 (/api/health)", "method": "GET", "path": "api/health"},
            {"label": "认证状态 (/api/auth/status)", "method": "GET", "path": "api/auth/status"},
            {"label": "自动登录获取 JWT (/api/auth/login)", "method": "POST", "path": "api/auth/login", "body": {}},
        ],
    },
    "kaggle-web-probe": {
        "primary_surface": "inspector",
        "surfaces": ["inspector"],
        "openai_compatible": False,
        "default_model": "kaggle-web-probe",
        "api_style": "kaggle-web-probe",
        "health_path": "health",
        "models_path": "",
        "features": {
            "web_ui": True,
            "cpu_only": True,
            "cache_probe": True,
        },
        "suggestions": [
            "检查 Kaggle web-probe 健康状态 (/health) 与 /kaggle/working/.ct-cache 状态",
            "通过内置网页首页 (/) 验证 Caddy Basic Auth 与隧道端到端连通性",
        ],
        "endpoint_presets": [
            {"label": "健康检查 (/health)", "method": "GET", "path": "health"},
            {"label": "探针网页 (/)", "method": "GET", "path": ""},
        ],
    },
}


def build_api_profile(
    name: str,
    *,
    doc: str = "",
    subcommand_names: list[str] | None = None,
    auth: str = "",
    has_web_ui: bool = False,
) -> dict[str, Any]:
    """Return the normalized API profile for a known or newly discovered runtime.

    Known runtimes use curated endpoint and capability metadata. Unknown future
    runtimes are automatically classified from their module docstrings and
    subcommands, defaulting to probeable OpenAI-compatible + HTTP inspector
    surfaces whenever OpenAI/v1 indicators appear.
    """
    if name in RUNTIME_API_PROFILES:
        return deepcopy(RUNTIME_API_PROFILES[name])

    text = f"{name}\n{doc}\n{' '.join(subcommand_names or [])}".lower()
    surfaces: list[str] = []
    features: dict[str, Any] = {"web_ui": has_web_ui, "auto_discovered": True, "auth": auth}

    has_openai_hint = any(
        tok in text
        for tok in ("openai", "/v1/", "v1/models", "chat/completions", "vllm", "sglang", "llama-server", "responses")
    )
    has_chat = any(tok in text for tok in ("/v1/chat/completions", "/v1/responses", "chat", "llm", "reasoning"))
    has_image = any(tok in text for tok in ("/v1/images", "images/generations", "images/edits", "text-to-image", "t2i", "i2i"))
    has_audio = any(tok in text for tok in ("/v1/audio/speech", "/v1/audio/generations", "tts", "voice clone", "sound effect"))
    has_asr = any(tok in text for tok in ("/v1/audio/transcriptions", "transcribe", "whisper", "asr", "ctc_timestamps"))
    has_embed = any(tok in text for tok in ("/v1/embeddings", "embeddings"))

    if has_chat or (has_openai_hint and not (has_image or has_audio or has_asr or has_embed)):
        surfaces.append("chat")
        features["streaming"] = True
        features["tools"] = True
    if has_image:
        surfaces.append("image")
        features["image_generation"] = True
        features["image_edit"] = "edits" in text or "edit" in text
    if has_audio:
        surfaces.append("audio")
        features["tts"] = "speech" in text or "tts" in text
        features["sfx"] = "generations" in text or "sfx" in text or "sound" in text
    if has_asr:
        surfaces.append("transcribe")
        features["audio_input"] = True
    if has_embed:
        surfaces.append("embeddings")
        features["embeddings"] = True

    openai_compatible = has_openai_hint or any(s in surfaces for s in ("chat", "image", "audio", "transcribe", "embeddings"))
    if not surfaces:
        # Unknown custom runtimes expose the universal HTTP inspector plus OpenAI probeable tabs
        surfaces = ["chat", "inspector"] if has_openai_hint else ["specialized", "chat", "inspector"]

    if "specialized" not in surfaces and ("/api/" in text or "/jobs" in text or "gradio" in text):
        surfaces.append("specialized")
    if "inspector" not in surfaces:
        surfaces.append("inspector")

    primary_surface = surfaces[0]
    presets: list[dict[str, Any]] = []
    if openai_compatible:
        presets.append({"label": "模型列表 (/v1/models)", "method": "GET", "path": "v1/models"})
    if "/health" in text or "health" in text:
        presets.append({"label": "健康检查 (/health)", "method": "GET", "path": "health"})
    if "chat" in surfaces:
        presets.append(
            {
                "label": "Chat Completions",
                "method": "POST",
                "path": "v1/chat/completions",
                "body": {"model": name, "messages": [{"role": "user", "content": "你好"}], "stream": False},
            }
        )
    if "embeddings" in surfaces:
        presets.append(
            {
                "label": "Embeddings",
                "method": "POST",
                "path": "v1/embeddings",
                "body": {"model": name, "input": ["ColabLLM"]},
            }
        )

    return {
        "primary_surface": primary_surface,
        "surfaces": surfaces,
        "openai_compatible": openai_compatible,
        "default_model": name if openai_compatible else name,
        "api_style": "openai-auto" if openai_compatible else "custom-auto",
        "health_path": "health" if "health" in text else ("v1/models" if openai_compatible else ""),
        "models_path": "v1/models" if openai_compatible else "",
        "features": features,
        "suggestions": [
            f"探测 {name} 的 /v1/models 与可用端点",
            "向当前运行时发送一次结构化对话或自定义 HTTP 测试请求",
        ],
        "endpoint_presets": presets,
    }
