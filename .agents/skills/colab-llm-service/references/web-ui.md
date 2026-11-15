# Web UI Guide: Per-Runtime Web UIs & `colabllm web` Console

ColabLLM has two cooperating Web UI layers:

1. **Per-runtime Web UI** served from the Colab VM on `127.0.0.1:CT_SERVICE_PORT` (`GET /`) and exposed through the public Caddy/tunnel URL.
2. **Local `colabllm web` console** (`src/colabllm/web_ui/`) running on the user's machine (`127.0.0.1:8765`), which discovers every runtime, generates Launch & CLI Workbench forms, opens **"打开 Web UI"** buttons for registered runtimes, and proxies OpenAI-compatible chat.

---

## Part 1 — Deciding & Building a Runtime's Own Web UI

### When a runtime should expose a Web UI

- **Upstream UI already exists**: `llama-server` built-in chat Web UI (`qwen-flash`, `deepseek-coder`, `deepseek-qwen-9b`), Unsloth Studio (`unsloth-studio`), ComfyUI (`comfyui`), Openjutsu (`openjutsu`), and Gradio (`longcat-video`) already serve `GET /`. Expose them directly and register them in `WEB_UI_SERVICES`.
- **Job / batch / storage / interactive workflow runtimes without an upstream UI**: build a zero-dependency Web UI at `GET /` alongside `/health` and `/api/*` in `templates/<name>_app.py` (embedded via `embed_source(...)` in `templates/build_notebooks.py`) whenever live progress dashboards, Drive/bucket browsing, parameter forms, pause/resume/cleanup controls, or result previews improve usability (e.g. `unlimited-ocr`, `r2-migrate`).
- **Pure headless API/SDK runtimes**: pure `/v1/*` audio/image/transcription ASGI middlewares (`colab_asgi_common.verify_bearer_auth`) can remain API-only when CLI + OpenAI SDK calls are the sole workflow; do not add empty placeholder pages.

### Choosing auth for a runtime with a Web UI

Never expose an unauthenticated Web UI on a public tunnel URL. Choose one of the three verified repository patterns:

| Pattern | Runtimes | How browser & API access work |
| --- | --- | --- |
| **`auth="caddy"` (preferred for custom UIs)** | `comfyui`, `openjutsu`, `longcat-video`, `unlimited-ocr`, `r2-migrate` | Service binds strictly to `127.0.0.1:CT_SERVICE_PORT`; Caddy protects `GET /` + browser assets with Basic Auth (`CT_AUTH_USER`/`CT_AUTH_PASS`) and protects API callers with `Authorization: Bearer <CT_API_TOKEN>`. `flush_interval -1` preserves SSE/WebSockets; pass `rewrite_host=False` to `render_caddy_cell` if the app constructs absolute URLs from `Host`. |
| **`auth="builtin"` + `--api-key-file`** | `qwen-flash`, `deepseek-coder`, `deepseek-qwen-9b` (`llama-server`) | `GET /` loads the built-in browser Web UI while `/v1/*` enforces the Bearer key (`CT_API_TOKEN`). Users enter `api_token` once inside the Web UI settings; `colabllm web` shows this hint automatically when `has_web_ui && has_api_token`. |
| **`auth="builtin"` + native login** | `unsloth-studio` | Notebook prints `CT_CREDENTIALS user=<u> pass=<p>`; `colabsvc` saves `login_user`/`login_pass` into `state.json` (`0600`) for `info --show-secrets` and the `colabllm web` credentials dialog. |

For custom `auth="caddy"` Web UIs (such as `templates/r2_migrate_app.py`), `ct_public_gate` / `_verify_auth` in the keep-alive cell must still probe unauthenticated `GET /` (`401` with no `Set-Cookie`), invalid Bearer token (`401`), and authenticated `/health` (`200`) before printing `CT_READY`.

### Security & modern frontend rules for embedded runtime UIs (`templates/*_app.py`)

When writing HTML/CSS/JS inside an embedded runtime app (`templates/<name>_app.py`):

1. **Consult `modern-web-guidance` first**: inspect relevant UI guides before authoring controls (`dark-mode`, `dialog`, `responsive-table`, `validate-input-after-interaction`, `forms`, etc.).
2. **Dark mode & native viewport theming**: include `<meta name="color-scheme" content="light dark">` in `<head>` and apply `color-scheme: light dark` on `:root` (with `light-dark()` tokens and `@media (prefers-color-scheme: dark)` fallback).
3. **Zero external network assets**: bundle all HTML/CSS/JS inline in the module (no external `<script>`, `<link>`, or font CDN URLs) so it works offline and through restrictive proxies.
4. **Per-request cryptographic CSP `nonce`**: generate a fresh random nonce on every `GET /` response (`secrets.token_urlsafe(16)`) and emit strict headers:
   ```text
   Content-Security-Policy: default-src 'self'; script-src 'nonce-<nonce>'; style-src 'nonce-<nonce>'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'
   X-Content-Type-Options: nosniff
   ```
5. **Safe DOM updates**: render job/bucket/Drive data with `document.createElement`, `textContent`, and `replaceChildren`—never `innerHTML` with API responses, object keys, or Drive filenames.
6. **Confirmations & forms**: use modal `<dialog>` (`showModal()`) with `autofocus` on Cancel and `dialog.returnValue = ''` reset on `close` for destructive actions; use `@container` responsive tables; validate with `:user-invalid` + dynamic `aria-describedby` error linking and `autocorrect="off" autocapitalize="none" spellcheck="false"` on IDs/URLs/paths.
7. **Secret hygiene & Drive path containment**:
   - Never return raw cloud secrets, API tokens, or passwords from `GET` endpoints (return redacted booleans/masks like `configured` / `access_key_id_masked`).
   - Contain every user-supplied Drive or output path (`resolve_drive_path` or `Path.resolve().is_relative_to(...)`), mapping out-of-root escapes (`ValueError`) to HTTP `400`.

---

## Part 2 — Registering a Runtime in `colabllm web` (`src/colabllm/web_ui/`)

### How the local console works

`colabllm web` (`-d/--detach`, `--status`, `--stop`, `--port`, `--read-only`, `--allow-remote`) runs a loopback stdlib server (`src/colabllm/web_ui/server.py` + `runner_bridge.py` + `static/{index.html,styles.css,app.js}`):

- `src/colabllm/web_ui/introspect.py` discovers every `src/colabllm/services/<name>.py` with a matching `.ipynb`, executes `<name>.py` via `runpy.run_path` (`__name__ != "__main__"`), and converts `SERVICE` + `build_parser(SERVICE, configure_cli)` into dynamic **Launch (`up`)** and **Workbench (business CLI)** forms plus live redacted command previews.
- `src/colabllm/web_ui/state_store.py` reads `~/.colabllm/services/<name>[-<inst>]/state.json` and `run.log` for fleet cards, log streaming, and on-demand secret reveal (`POST /api/instances/<svc>/secret`).
- Note that `static/` is verified by `tests/test_web_ui.py::WebServerIntegrationTest.test_static_frontend_offline_and_trusted_types_hygiene`: external `http(s)://` URLs and `innerHTML` / `outerHTML` / `insertAdjacentHTML` are forbidden in `src/colabllm/web_ui/static/`.

### Checklist whenever adding or updating `<name>`

1. **Expose the **"打开 Web UI"** button (`src/colabllm/web_ui/introspect.py`)**:
   - For `llama-server` runtimes, add `<name>` to `LLAMA_SERVER_SERVICES` (automatically enables both `WEB_UI_SERVICES` and `CHAT_SERVICES`).
   - For all other runtimes serving a browser Web UI at their public URL, add `<name>` to `WEB_UI_SERVICES`. This sets `meta["has_web_ui"] = True`, causing `app.js` to render the **"打开 Web UI"** button when an instance is running and `url` is assigned.
2. **Chat Playground support (`CHAT_SERVICES`)**:
   - If the runtime exposes an OpenAI `/v1/chat/completions`-compatible endpoint, add `<name>` to `CHAT_SERVICES` so the console Chat tab proxies messages via `/api/proxy/chat`.
3. **Service category (`_CATEGORIES` & `CATEGORY_LABELS`)**:
   - Place `<name>` in `_CATEGORIES` (`"llm"`, `"image"`, `"audio"`, `"video"`, `"ocr"`; uncategorized services fall back to `"other"`). If introducing a new category key, define its display label in `CATEGORY_LABELS` in **both** `src/colabllm/web_ui/introspect.py` and `src/colabllm/web_ui/static/app.js`.
4. **Secret CLI flags (`_SENSITIVE_FLAGS` & `action.dest`)**:
   - If `configure_cli` registers any password, secret, or token flag (e.g. `--secret-access-key`, `--hf-token`, `--api-token`), add the flag string to `_SENSITIVE_FLAGS` and its `dest` to the `is_secret` check in `_serialize_action()` inside `src/colabllm/web_ui/introspect.py`. The Workbench will render a password input and redact the secret (`***`) in CLI command previews.
5. **Instance secret reveal scope (`src/colabllm/web_ui/state_store.py`)**:
   - Check `_allowed_secret_keys(service_name, auth_mode)` against `SERVICE.auth`:
     - `auth="caddy"` exposes `auth_user`, `auth_pass`, `api_token` (+ `login_*` if present).
     - `auth="builtin"` exposes `api_token`, `login_user`, `login_pass` (excluding Caddy Basic Auth).
     - Pure login UIs like `unsloth-studio` restrict to `("login_user", "login_pass")`.
6. **Introspection-safe service module conventions (`src/colabllm/services/<name>.py`)**:
   - Importing `<name>.py` under `runpy.run_path` and calling `configure_cli(parser, sub, up)` must perform zero network requests, GPU probes, or filesystem writes outside standard CLI handlers.
   - Make the first non-empty line of the module docstring a clear one-line summary (`introspect._doc_summary` displays it as the service subtitle).
   - Capture `_BASE_ENV = dict(SERVICE.env)` (and `_BASE_HIGH_MEM = SERVICE.high_mem` if used) and reset them inside `prepare_up(args)` so repeated invocations do not mutate shared module defaults.
   - Keep `DEFAULT`-style hostnames short aliases in `SERVICE.default_hostname` (`"." not in alias`), matching `tests/test_repo_hygiene.py`.
7. **Packaging & tests**:
   - Register `<name>`'s business subcommands in `tools/check_distribution.py`.
   - If new static files are added under `src/colabllm/web_ui/static/`, register MIME types in `STATIC_FILES` in `src/colabllm/web_ui/server.py` (glob `"colabllm.web_ui" = ["static/*"]` in `pyproject.toml` and `MANIFEST.in` already package them).
   - Add `<name>` assertions to `tests/test_web_ui.py` (`has_web_ui`, `chat_compatible`, `gpu`, secret flag redaction) and `tests/test_repo_hygiene.py` (`prepare_up`, `default_hostname`), then run `python3 -m unittest tests/test_repo_hygiene.py tests/test_web_ui.py -v`.
8. **Refresh running `colabllm web` daemon**:
   - `introspect.py` caches `_CATALOG_CACHE` and `_SCHEMA_CACHE` in memory for the lifetime of the server process. After adding or updating a runtime, check `colabllm web --status --json` and restart if active:
     ```bash
     colabllm web --stop && colabllm web --detach --no-open
     ```
