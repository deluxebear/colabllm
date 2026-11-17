# Gateway Guide: Upstream Passthrough Contract & `GATEWAY_RUNTIME_SPECS`

`colabllm gateway` (`src/colabllm/gateway.py` + `src/colabllm/gateway_runtime/`) exposes a single-entry local OpenAI/Anthropic HTTP/1.1 data-plane proxy across running Colab runtimes without leaking control-plane operations, Google Drive access, or upstream credentials.

Clients route requests with explicit `<upstream_model>@<runtime>[:<instance_id|default>]` model names, or read-only TTS voice lookups at `GET /r/<runtime>[:<inst>]/v1/audio/voices`. Every new service pair in `src/colabllm/services/` must define its gateway policy explicitly in `src/colabllm/gateway_runtime/catalog.py`.

---

## Part 1 — Upstream Runtime & Middleware Contract

Any runtime intended for API/SDK consumption through `colabllm gateway` (for example `llama-server`, `vllm`, `vllm-omni`, `sglang`, Diffusion, TTS, or ASR backends) must satisfy the contracts below in its notebook cells and `templates/*_middleware.py`:

### 1. Mandatory `GET /v1/models` & Readiness Gate

- Serve authenticated `GET /v1/models` returning HTTP `200` with OpenAI-compatible JSON:
  ```json
  {"object": "list", "data": [{"id": "<model_or_alias>", "object": "model"}]}
  ```
- Register **both** the canonical repo/checkpoint ID (e.g. `Org/Model-Name`) and configured short alias (`CT_*_ALIAS`) in `/v1/models` when an upstream supports aliases so clients can use either form behind `@<runtime>`.
- `colabllm gateway` dynamically discovers `READY` instances with zero gateway restart by polling `state.json` and singleflight-probing `GET /v1/models` (`30s` TTL cache keyed by `state.json` `(mtime_ns, size, url)`). Verify authenticated `GET /v1/models` returns `200` in the notebook readiness gate **before** printing `CT_READY`.
- Always return direct HTTP `200`/`4xx`/`5xx` responses on `/v1/*`; redirects (`3xx`) are terminated by the gateway with HTTP `502` to protect upstream credentials.

### 2. Standard `/v1/*` Route Families

- **Text / VLM / Reasoning (`llama-server`, `vllm`, `vllm-omni`, `sglang`)**:
  - Support `GET /v1/models`, `GET /v1/models/<id>`, `POST /v1/chat/completions`, `POST /v1/completions`, `POST /v1/responses`, `POST /v1/messages`, and optionally `POST /v1/embeddings`.
  - Streaming (`stream: true`) must emit `Content-Type: text/event-stream` with SSE `data: {...}\n\n` JSON lines containing top-level `"model"` (rewritten symmetrically back to `<model>@<runtime>[:<inst>]` by the gateway) and end with `data: [DONE]\n\n`.
  - Preserve client Bearer (`Authorization: Bearer ...`) and Anthropic (`x-api-key`) compatibility behind auth: Caddy (`CT_AUTH="caddy"`) already bridges `Authorization: Bearer` into backend services; custom `llama-server` / ASGI middlewares using `CT_AUTH="builtin"` must accept `Authorization: Bearer <CT_API_TOKEN>` (and `x-api-key` if native `/v1/messages` bypasses Caddy).
- **Image Generation & Editing (`vllm-omni`, `sglang` Diffusion, custom ASGI)**:
  - Expose `POST /v1/images/generations` and `POST /v1/images/edits` alongside `GET /v1/models` (and optionally `POST /v1/chat/completions` when backed by `vllm-omni` chat-to-image).
  - Return inline `b64_json` image outputs by default; `colabllm gateway` rejects `response_format="url"` with HTTP `400`.
  - On `/v1/images/edits` (and multimodal `/v1/chat/completions`), accept **both** standard `multipart/form-data` binary file uploads (`image` / `image[]`) and JSON inline `data:image/...;base64,...` URIs. Never force callers onto custom async `/api/ct/*` routes for synchronous OpenAI image requests.
- **Audio TTS & Sound Effects (`vllm-omni`, `sglang-omni`, custom ASGI)**:
  - Expose `POST /v1/audio/speech` (`model`, `input`, `voice`, `response_format`, `speed`, plus optional backend-specific `extra_params` or top-level parameters unwrapped cleanly in middleware) and/or `POST /v1/audio/generations`.
  - Ensure plain OpenAI SDK requests **without** `extra_params` succeed out-of-the-box by falling back to sensible voice defaults (e.g. `"default"` / `"alloy"`) and auto-detecting language when omitted.
  - When serving custom registered voices, expose read-only `GET /v1/audio/voices` (prefixed cleanly at `GET /r/<runtime>[:<inst>]/v1/audio/voices` through the gateway). Keep `POST` / `DELETE` voice mutation blocked at the gateway.
  - If supporting batch speech synthesis (`POST /v1/audio/speech/batch`), document whether `items[*].model` is accepted so `rewrite_batch_items_model` keeps per-item target IDs aligned.
- **Audio ASR / Transcription (`vllm` speech, `ollama`, custom ASGI)**:
  - Expose `POST /v1/audio/transcriptions` and/or `POST /v1/audio/translations` (multipart binary `file` + `model`), or `POST /v1/chat/completions` accepting inline `data:audio/...;base64,...` / `input_audio` payloads.

### 3. Strict Isolation from Google Drive, Local Paths & Control-Plane APIs

`colabllm gateway` intentionally treats the gateway listener as an untrusted inference data-plane boundary (`HTTP 403` fail-closed):

- Blocked headers: `X-Save-Drive`, `X-Drive-Only`.
- Blocked routes: `/api/ct/*`, browser UI routes (`/`), voice uploads/deletes (`POST`/`DELETE /v1/audio/voices*`), and any route not declared in `RuntimeGatewaySpec`.
- Blocked payload fields (`BASELINE_DRIVE_FORBIDDEN_FIELDS` plus per-runtime extras): `save_to_drive`, `save_drive_dir`, `save_drive_path`, `drive_only`, `drive_image`, `drive_images`, `lora`, `lora_path`, and any custom Drive reference fields declared on `spec.drive_forbidden_fields`.
- Blocked media values on inspected `media_fields`: external `http://` / `https://` URLs, `drive://` / `gdrive://` / `file://` / `upload://` / `ytdl://` / `yt-dlp://` schemes, POSIX/Windows path prefixes (`/content/`, `mydrive/`, `./`, `../`, `~/`, `/`, `\`), bare filenames, and yt-dlp domains when `forbid_ytdlp_urls=True`. Only inline `data:(image|audio)/...;base64,...` URIs and multipart binary uploads are permitted.

Design rule for `templates/*_middleware.py`:

- Always keep Drive storage (`save_to_drive`, `drive://` audio/image inputs, `/api/ct/*` jobs) strictly **optional** convenience layers for the direct `colabllm <name>` CLI.
- Never require a Drive path, local server path, `X-Save-Drive`, or remote URL fetch inside standard `/v1/*` handlers; always keep an inline `data:` / multipart binary path working.
- Keep `extra_body` / `extra_params` / multipart JSON fields cleanly parseable in middleware—note `MULTIPART_JSON_FIELDS` (`extra_body`, `extra_params`, `items`, `references`, `lora`, `lora_path`, `messages`, `tools`, `response_format`) are also inspected recursively by the gateway when sent in `multipart/form-data`.

---

## Part 2 — Registering `<name>` in `src/colabllm/gateway_runtime/catalog.py`

Every service directory `src/colabllm/services/<name>.py` (+ `.ipynb`) MUST have a matching entry in `GATEWAY_RUNTIME_SPECS` inside `src/colabllm/gateway_runtime/catalog.py`. `tests/test_repo_hygiene.py::RepoHygieneTest.test_every_service_is_registered_in_gateway_catalog` enforces exact set equality between discovered service pairs and `GATEWAY_RUNTIME_SPECS.keys()`, plus non-empty `allowed_v1_routes` containing `("GET", "/v1/models")` whenever `gateway_enabled=True`.

### Choosing a `RuntimeGatewaySpec` Profile

1. **Non-OpenAI UI, Workflow, or Utility Runtimes** (`comfyui`, `openjutsu`, `r2-migrate`, `unsloth-studio`, `unlimited-ocr`, `longcat-video`, `longcat-avatar`):
   ```python
   "<name>": RuntimeGatewaySpec(),  # gateway_enabled=False
   ```

2. **`llama-server` Text / Vision LLM Runtimes** (`qwen-flash`, `deepseek-coder`, `deepseek-qwen-9b`, etc.):
   Reuse the shared constants `_LLAMA_ROUTES`, `_LLAMA_PREFIXES`, `_LLAMA_MEDIA`, and `_LLAMA_DRIVE_FORBIDDEN`:
   ```python
   "<name>": RuntimeGatewaySpec(
       gateway_enabled=True,
       mode="chat_only",
       allowed_v1_routes=_LLAMA_ROUTES,
       allowed_v1_prefixes=_LLAMA_PREFIXES,
       media_fields=_LLAMA_MEDIA,
       drive_forbidden_fields=_LLAMA_DRIVE_FORBIDDEN,
       default_slot_capacity=1,
       slot_capacity_env="CT_<PREFIX>_PARALLEL",
       stateless_responses_only=True,
   ),
   ```

3. **`vllm` / `vllm-omni` / `sglang` / Custom Multimodal, Image, Audio, or ASR Runtimes**:
   Explicitly declare every permitted route and security boundary:
   ```python
   "<name>": RuntimeGatewaySpec(
       gateway_enabled=True,
       mode="multimodal",  # or "chat_only" for text/vision-only OpenAI endpoints
       allowed_v1_routes=(
           ("GET", "/v1/models"),
           ("POST", "/v1/images/generations"),
           ("POST", "/v1/images/edits"),
       ),
       allowed_v1_prefixes=(("GET", "/v1/models/"),),
       prefixed_only_routes=(),  # e.g. (("GET", "/v1/audio/voices"),) for TTS read-only voice lists
       media_fields=("image", "images", "image_url", "source.url"),
       check_bare_url_on_images_api=True,   # enforce scheme check on `url` under /v1/images/*
       check_file_on_asr_api=False,         # set True on /v1/audio/{transcriptions,translations}
       drive_forbidden_fields=(
           "save_to_drive",
           "save_drive_path",
           "drive_only",
           "drive_image",
           "drive_images",
       ),
       default_slot_capacity=1,
       slot_capacity_env="CT_<PREFIX>_PARALLEL",  # or "ENV_JSON:INNER_KEY"
       rewrite_batch_items_model=False,     # True if /v1/audio/speech/batch sends items[*].model
       forbid_ytdlp_urls=False,             # True if backend ever resolves video/yt-dlp URLs
       reject_url_response_format=True,     # True on image runtimes so response_format="url" -> 400
       stateless_responses_only=False,      # True if /v1/responses lacks server-side previous_response_id state
   ),
   ```

### Field Checklist When Adding a Runtime

- **`media_fields` completeness**: List every JSON or multipart field key in your new runtime's API that accepts an image, audio, or video payload (`image`, `images`, `image_url`, `audio`, `audio_url`, `ref_audio`, `emo_audio`, `audio_path`, `source.url`, etc.). Only `messages[].role == "tool"` text results are exempt from content-scanning; top-level and `user`/`system` media keys listed here are always validated.
- **`drive_forbidden_fields` completeness**: Add any runtime-specific parameters that read/write Google Drive or VM local weights (e.g. `drive_image`, `drive_images`, `save_drive_dir`, `lora`, `lora_path`, `ref_audio_drive`). Never silently drop security checks—unknown Drive aliases must be blocked explicitly.
- **`slot_capacity_env` mapping**: Point to the `CT_*` env var controlling concurrent request slots (`--parallel` / `--max-num-seqs`). Supports plain integer env strings or nested JSON keys via `"CONTAINER_ENV:INNER_KEY"` (e.g. `"CT_OLLAMA_SERVER_ENV:OLLAMA_NUM_PARALLEL"`). Defaults to `default_slot_capacity=1`, which pairs with 1 KB root-prefix KV-cache affinity grace waiting (`--affinity-grace-timeout`).
- **`stateless_responses_only`**: Set `True` whenever `POST /v1/responses` is exposed without cross-request stateful `previous_response_id` storage so clients receive an immediate HTTP `400` instead of silently failing or corrupting multi-replica routing.
- **Compat-gated multi-protocol runtimes**: Follow the `ollama_is_gateway_compatible` / `runtime_allows_v1_route` pattern in `catalog.py` if startup flags can disable OpenAI/Anthropic compatibility at launch time.

---

## Part 3 — Verification & Refresh Checklist

1. Add test coverage in `tests/test_gateway.py` when introducing new `RuntimeGatewaySpec` flags, media fields, Drive fields, or prefixed routes.
2. Run repository hygiene and gateway test suites:
   ```bash
   python3 -m unittest tests/test_repo_hygiene.py tests/test_gateway.py -v
   ```
3. Update user-facing gateway capability tables/docs if relevant (`docs/designs/colabllm-gateway.md`, `docs/runtime-common.md`, `skills/colabllm/SKILL.md` + `skills/colabllm/references/runtime-common.md`).
4. **Refresh any active `colabllm gateway` daemon after `catalog.py` edits**:
   - While new Colab instances of *already-registered* runtimes are discovered dynamically with zero restart, `GATEWAY_RUNTIME_SPECS` is loaded when `colabllm gateway` boots.
   - Reinstall the local `colabllm` CLI if using a non-editable `uv tool` install, then restart the daemon (existing hashed client credentials in `~/.colabllm/gateway/tokens.json` are preserved):
     ```bash
     colabllm gateway --status --json
     colabllm gateway --stop && colabllm gateway --detach
     ```
