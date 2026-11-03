---
name: colab-llm-service
description: >-
  Deploy and operate large-model services on Google Colab through this repository's
  one-notebook/one-script convention and unified colabllm CLI. Use for service setup, lifecycle, project-global
  domains, Cloudflare Tunnel or SSH-managed FRP/Caddy, API key lookup and rotation,
  uv package installation checks, and session or tunnel troubleshooting.
---

# Colab LLM service

Every model service in this repo is a pair of files in `src/colabllm/services/`, sharing one library:

```
src/colabllm/cli.py     packaged CLI: service dispatch and project configuration
src/colabllm/services/<name>.py       execution script (local): session, Drive, selected tunnel, credentials, keep-alive
src/colabllm/services/<name>.ipynb    notebook (Colab VM): setup, service, Caddy auth, cloudflared/frpc, keep-alive loop
src/colabllm/lib/colabsvc.py          shared runner used by every script (Service dataclass + CLI)
src/colabllm/control.py             project-global domain and FRP configuration
src/colabllm/lib/frp_backend.py      local SSH/DNS orchestration; frp_server.py provisions/adopts the server
templates/tunnel_client.py  shared cloudflared/frpc implementation embedded in all notebooks
templates/               service.py + service.ipynb to copy; build_notebooks.py regenerates notebooks
~/.colabllm/services/<name>/           private runtime state and resource identities (outside the checkout)
~/.colabllm/tunnel-config.json  shared base domain and optional FRP metadata
~/.colabllm/frp/             private per-server client credentials (0600)
```

Existing services: `unsloth-studio` (built-in login, L4), `ollama` (Caddy auth, T4),
`comfyui` (ComfyUI + ComfyUI-Manager, Caddy auth, L4, on-demand workflow model download to local SSD via `--workflow` / `pull-workflow`),
`qwen-flash` (`SC117/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-GGUF` IQ3_S, 800K YaRN 3.125x + BF16 vision,
`llama-server --api-key-file` built-in auth, G4 GPU, domain alias `qwen`),
`nemotron-transcribe` (`nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B` via `vllm serve` + `nemo_speechlm`,
Bearer API key auth, G4 GPU, domain alias `transcribe`, single & batch Drive audio CLI `transcribe`),
`indextts` (`IndexTeam/IndexTTS-2.5` via `vllm serve --omni` 2-stage pipeline, Bearer API key auth, L4 GPU,
domain alias `indextts`, zero-shot voice cloning, persistent Drive voices, native speed 0.5-2.0x,
multilingual `zh`/`en`/`zhen`/`ja`/`es`/`ar`/`yue` & emotion control CLI `tts` / `voices` / `upload-voice`),
`qwen3-tts` (`Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` / `VoiceDesign` / `Base` via `vllm serve --omni` 2-stage pipeline + auxiliary multi-task GPU worker, Bearer API key auth, L4 GPU, domain alias `qwen3-tts`, 9 built-in speakers, natural-language instruction control, VoiceDesign persona synthesis, 3-second zero-shot voice cloning, and `design-voice` Voice-Design-then-Clone workflow).

Also available: `unlimited-ocr` (A100, Caddy auth, persistent Drive OCR jobs),
`longcat-avatar` (G4, Caddy auth, audio-driven avatar generation with job/download commands),
and `longcat-video` (G4, Caddy auth, Gradio text/image-to-video and continuation UI).
For each runtime's current concrete usage, read `docs/<name>.md`; portable copies are bundled in
`skills/colabllm/references/<name>.md`.

## Unified CLI convention

Use `colabllm <name> <command> [args...]` for runtime operations and user-facing examples.
Use `colabllm tunnel-config`, `colabllm frp-config`, and `colabllm frp-status` for project configuration.
Run `colabllm services` to discover runtimes and `colabllm <name> --help` for their commands.

Install a distributed wheel with `uv tool install /path/to/colabllm-VERSION-py3-none-any.whl`.
After publication, users can use `uv tool install colabllm` and `uv tool upgrade colabllm`.
Developers use `uv tool install --editable .`; `uv build --no-sources` produces wheel and sdist.
Run `python tools/check_distribution.py dist/colabllm-VERSION-py3-none-any.whl` to verify distribution.
For actual local installation, inspect `command -v colabllm` and `uv tool list` first. Replace the
old checkout symlink with `uv tool install --force /path/to/package.whl` when switching installation
methods. Test from outside the checkout: version, service help, JSON info, dry-run, offline doctor
and completion. Keep configuration-write tests in a temporary `COLABLLM_HOME`.
Local regression tests are `python -m unittest discover -s tests` and need only the standard library;
`tests_colab/` belongs in the model environment. See `.github/workflows/ci.yml` for the platform matrix.

The package includes each service script and matching notebook. The dispatcher executes the actual
installed script so the detached `_exec` runner retains the script path. All application state is
in `~/.colabllm/` (override: `COLABLLM_HOME`); instances use `services/<name>/`, global tunnel settings
use `tunnel-config.json`, and FRP credentials use `frp/`. External CLI authentication keeps its own
locations. Relative input/output paths remain relative to the caller's working directory.
Stop services before upgrading and restart them afterwards. Existing repository `.state/` is unused;
there is no migration. New service pairs are discovered automatically and must be included in builds.

Runtime command registration belongs in the shared parser (`main(..., configure=..., handlers=...)`),
and script entry points use `cli_support.run_cli` for consistent errors. Local authenticated HTTP
clients use `safe_urlopen` or `SameOriginRedirect`. Keep diagnostics on stderr and JSON on stdout.
Instance IDs are validated strictly; parsing stops at `--`. State updates are locked transactions;
startup/shutdown also use a per-instance operation lock. Both foreground and detached runners record
PID plus start identity, and shutdown verifies ownership before signalling. `down` preserves state
on remote stop failure; readiness timeout is nonzero, while `up --detach` explicitly skips waiting.
`info/ps/status --json` default to redacted output; credentials require `--show-secrets`.

Readiness is parsed incrementally as complete protocol lines; preserve the exact `CT_READY` line
when changing notebook output. Endpoint overrides reuse stored tokens only for the same origin;
other origins require an explicit token. External CLI calls honor `--command-timeout` /
`COLABLLM_COMMAND_TIMEOUT`, while `up --ready-timeout` bounds readiness waiting. Task polling
supports `--timeout` (`0` unlimited); timeout exits use code 124. `doctor` is offline by default;
`--online` performs bounded read-only authorization probes. `completion bash|zsh|fish` generates
scripts using offline `_complete` introspection; completion reads instance directory names only.

## Daily use and transport selection

Read `colabllm tunnel-config` before choosing a public URL. It reports project-wide
settings, not settings for one runtime. `up` follows these settings:

- FRP enabled: use FRP and the configured base domain.
- Otherwise use Cloudflare: configured domain -> named tunnel; no domain -> dynamic Quick Tunnel URL.
- `--transport cloudflare|frp` overrides the backend for one launch. `--quick-tunnel` forces
  Cloudflare's dynamic URL and conflicts with `--transport frp`.
- `--domain HOST` supplies a full hostname; `--base-domain` supplies a one-launch suffix.
  Bare `--domain` uses the configured suffix or a dynamic URL if none exists. Historical
  `Service.zone` / `default_hostname` values retain aliases, not an implicit `example.com` deployment.

```bash
colabllm tunnel-config
colabllm tunnel-config --base-domain example.com  # shared by all services/instances
colabllm <name> up                                # selected global backend
colabllm <name> up --quick-tunnel
colabllm <name> up --transport cloudflare --domain llm.example.com --gpu A100
colabllm <name> info --show-secrets               # prints credentials; avoid echoing them in tool output
colabllm <name> logs -f
colabllm <name> down               # stop runner + VM; --delete-tunnel cleans this instance's resources
```

For **API key lookup, user-supplied keys or rotation**, read [references/auth.md](references/auth.md).
For **Cloudflare setup, DNS ownership or cleanup**, read [references/cf-tunnel.md](references/cf-tunnel.md).
For **FRP setup, project-ID/DNS conflicts, existing server adoption or connection failures**, read
[references/frp.md](references/frp.md). Use the user's actual server/domain values, not examples.
Configuration changes affect future starts; preserve running generation tasks when changing code or transport.

`up` normally returns once the notebook prints `CT_READY`; `--detach` returns before readiness.
A detached local runner keeps `colab exec`
attached (= keep-alive). Requirements: `colab` CLI logged in, `colab dfs consent` done once.
Fixed domains use the local `cf` login for DNS in either backend. FRP server management also needs SSH.

## Hard-won rules (verified on real runs -- do not "simplify" them away)

1. **The session lives only while `colab exec` stays connected.** Disconnecting (Ctrl-C, closing the
   terminal, laptop sleep) gets the VM reclaimed a while later even if a cell is still running. The
   Colab CLI has no keep-alive. Hence the detached runner; run `up` on an always-on machine for long jobs.
2. **`colab exec -f` sends cells one at a time from the client.** Disconnect before the last cell
   starts and the remaining cells never run.
3. **The last cell must block forever** (keep-alive/watchdog loop, or e.g. Unsloth's `start()`).
4. **Background work must not depend on a thread being "the cell"**: start servers as subprocesses
   (`ct_spawn`) and register them in `PROCS` so the watchdog restarts them.
5. **Setup cells must be idempotent** -- sessions are reused (`git clone` fails on an existing dir:
   use `test -d x/.git && git -C x pull || git clone ...`; skip installs that already exist).
6. **Mount Drive with `colab dfs mount`** (browser-free, ~10 s). Notebook `drive.mount()` would wait
   forever for consent; `colabsvc` injects a prelude that turns it into a no-op once Drive is mounted.
7. **A failing cell does not change `colab exec`'s exit code** -> readiness is signalled with markers
   (`CT_READY`), never inferred from exit codes.
8. **Each `colab exec` call starts in `/content`**; `%cd` only persists within one exec.
9. **Secrets never go into the notebook**: the script generates them, stores them in `~/.colabllm/services/`
   (0600) and injects them with `colab exec --env`.
10. **`cf` (beta) quirk**: `cf tunnels config update --tunnel-config-ingress` demands unrelated Access
    flags -> send `--body '{"config":{"ingress":[...]}}'` instead.
11. **vLLM services MUST use the two-tier JIT kernel precompilation cache (Local SSD $\leftrightarrow$ Google Drive)**:
    vLLM compiles FlashInfer JIT (`fused_moe_<sm>`, etc.), Triton kernels, and TorchInductor/vLLM CUDA graphs on first startup (taking 5–10 minutes on Blackwell `sm_120` / Hopper `sm_90`). Never point compiler cache dirs directly at `/content/drive` (FUSE lockfile/rename bottlenecks slow down `ninja`/`nvcc`), and never leave them only in ephemeral `/root/.cache`. Always stage them on local NVMe SSD and sync with `DATA_DIR` on Google Drive (see **vLLM deployment standard** below).

## vLLM deployment standard (precompiled JIT kernel cache & storage layout)

Whenever deploying a model with **vLLM** (`vllm serve` or `vllm.LLM`), follow this unified storage and precompilation cache pattern (reference implementation: `NEMOTRON_TRANSCRIBE_*` in `templates/build_notebooks.py`):

1. **Model weights on local NVMe SSD (`/content/<service>/model`), never on Google Drive**:
   - Download weights directly from Hugging Face to `/content/<service>/model` with `HF_XET_CHUNK_CACHE_SIZE_BYTES="0"` (and delete `/root/.cache/huggingface/xet` afterwards) to avoid exhausting either the 15–100 GB Google Drive quota or the Colab SSD.
2. **Two-tier JIT kernel cache (`flashinfer-cache`, `triton-cache`, `vllm-cache`)**:
   - **Why multi-GPU safe**: FlashInfer (`*_120`, `*_90`, `*_80`), Triton, and vLLM/TorchInductor automatically namespace/hash compiled `.so`/`.cubin` artifacts by GPU compute capability (`sm_120` for G4, `sm_90` for H100, `sm_80` for A100). Different GPU types coexist in the same Drive cache directory without overwriting each other.
   - **Step A — Cell 1 (`Setup`)**: Restore cached kernels from Google Drive (`DATA_DIR`) to local NVMe SSD before starting `vllm`:
     ```python
     WORK_DIR = Path(f"/content/{SERVICE}")
     FLASHINFER_DRIVE_DIR, FLASHINFER_CACHE_DIR = Path(DATA_DIR) / "flashinfer-cache", WORK_DIR / "flashinfer-cache"
     TRITON_DRIVE_DIR,     TRITON_CACHE_DIR     = Path(DATA_DIR) / "triton-cache",     WORK_DIR / "triton-cache"
     VLLM_DRIVE_DIR,       VLLM_CACHE_DIR       = Path(DATA_DIR) / "vllm-cache",       WORK_DIR / "vllm-cache"
     for d in (FLASHINFER_DRIVE_DIR, FLASHINFER_CACHE_DIR, TRITON_DRIVE_DIR, TRITON_CACHE_DIR, VLLM_DRIVE_DIR, VLLM_CACHE_DIR):
         d.mkdir(parents=True, exist_ok=True)
     for src_d, dst_d in ((FLASHINFER_DRIVE_DIR, FLASHINFER_CACHE_DIR), (TRITON_DRIVE_DIR, TRITON_CACHE_DIR), (VLLM_DRIVE_DIR, VLLM_CACHE_DIR)):
         if any(src_d.iterdir()):
             subprocess.run(["cp", "-rn", f"{src_d}/.", f"{dst_d}/"], check=False)
     ```
   - **Step B — Cell 2 (`Service`)**: Inject the local SSD cache paths into `vllm serve`'s environment:
     ```python
     VLLM_ENV = {
         **os.environ,
         "FLASHINFER_WORKSPACE_BASE": str(FLASHINFER_CACHE_DIR),
         "TRITON_CACHE_DIR": str(TRITON_CACHE_DIR),
         "VLLM_CACHE_ROOT": str(VLLM_CACHE_DIR),
         "TORCHINDUCTOR_CACHE_DIR": str(VLLM_CACHE_DIR / "inductor"),
     }
     ```
   - **Step C — Cell 5 (`Ready + keep-alive`)**: Once `/health` returns `200` (all JIT warmup compilations complete), incrementally merge (`cp -ru`) the local SSD cache back to Google Drive so all future sessions (and parallel instances `-i 1`, `-i 2`, or different `--gpu` architectures) reuse the compiled binaries:
     ```python
     for local_c, drive_c in ((FLASHINFER_CACHE_DIR, FLASHINFER_DRIVE_DIR), (TRITON_CACHE_DIR, TRITON_DRIVE_DIR), (VLLM_CACHE_DIR, VLLM_DRIVE_DIR)):
         if local_c.is_dir() and drive_c.is_dir():
             subprocess.run(["cp", "-ru", f"{local_c}/.", f"{drive_c}/"], check=False)
     ```

## Notebook contract

Env injected by the script (`CT_*`), defaults let the notebook also run pasted into the Colab UI:

| Var | Meaning |
| --- | --- |
| `CT_SERVICE`, `CT_SERVICE_PORT` | service name; port the service listens on **127.0.0.1** |
| `CT_AUTH` | `caddy` (service has no auth) / `builtin` (own login or API key) / `none` |
| `CT_PUBLIC_PORT` | transport target: Colab authentication Caddy port if `caddy`, else the service port |
| `CT_AUTH_USER`, `CT_AUTH_PASS`, `CT_API_TOKEN` | Caddy basic-auth login and Bearer token |
| `CT_TRANSPORT` | `cloudflare` (default) or `frp`, chosen by the local runner |
| `CT_TUNNEL_TOKEN`, `CT_PUBLIC_HOSTNAME` | Cloudflare named-tunnel token, and public hostname for either fixed-domain backend |
| `CT_FRP_*` | server, token, CA, TLS name, scopes, client version and proxy identity; runner supplies these privately |
| `CT_DATA_DIR` | `/content/drive/MyDrive/colab-llm/<name>` -- cache models/outputs here |

Markers the notebook prints (parsed by `colabsvc`): `CT_PUBLIC_URL=<url>`,
`CT_CREDENTIALS user=<u> pass=<p>` (app's own login, e.g. Unsloth), `CT_READY`.

Standard cell order (generated by `templates/build_notebooks.py`):
`0 config` → `1 setup (idempotent)` → `2 service: def start_service(); PROCS["service"]=...` →
`3 Caddy auth` → `4 shared transport (cloudflared or frpc)` → `5 CT_READY + watchdog loop`.
Use `TUNNEL_PROCESS_KEY` to access the active transport in `PROCS`; its key is `cloudflared` or `frpc`.
The runner clears unused backend variables when reusing a Colab kernel. A registered tunnel or
HTTPS 502 during model loading is not service readiness; keep the service health/warmup gate before `CT_READY`.
Put `# @ct:skip` in a cell (or `up --skip N`) to skip it.

## Add a new service

1. Copy the pair: `cp templates/service.py src/colabllm/services/<name>.py; cp templates/service.ipynb src/colabllm/services/<name>.ipynb`
   (`<name>` = file name = Colab session; hostname uses the project base domain).
2. Edit `src/colabllm/services/<name>.py`: `name`, `notebook`, `service_port`, `auth`, `gpu`, extra `env`.
3. Edit the notebook's **cell 1 (setup)** and **cell 2 (`start_service`)** only; bind to `127.0.0.1`.
   Cache large downloads in `DATA_DIR`. Adjust the health path in cell 5 if `/` is not a good probe.
   For a reusable recipe add it to `templates/build_notebooks.py` instead and regenerate.
4. Pick auth -- see [references/auth.md](references/auth.md). Rule: **never expose a service without
   auth**; if it has none, `auth="caddy"`.
5. Update the distributed user skill in `skills/colabllm/` in the same change. Add the runtime to
   `SKILL.md`'s task/runtime/reference table and document its actual user workflow in an appropriate
   per-runtime reference. Canonical usage guides live in `docs/<name>.md`; run
   `python tools/sync_runtime_docs.py` to update the matching portable skill references. Cover prerequisites/GPU,
   startup, inference or UI access, input/output locations, authentication, waiting/recovery and
   service-specific limits. Use installed CLI commands and portable examples, not checkout paths,
   personal domains or credentials. Update the skill description if the new capability changes
   when an agent should invoke it. Runtime removals and command/behavior changes require the same
   reconciliation; the distributed skill is part of runtime delivery, not optional follow-up work.
6. Verify discovery with `colabllm services` and argument forwarding with `colabllm <name> --help`.
   Run `colabllm <name> up --dry-run`, then a real `up` (CPU `--gpu none` for wiring tests), check
   `status` (unauthenticated probe must be 401 for Caddy services), then `down`.
7. Run `python tools/sync_runtime_docs.py --check`, check user-skill links and parse its new examples against the actual CLI without submitting jobs.
   Build with `uv build --no-sources` and run `python tools/check_distribution.py <wheel>`; verify the
   new runtime and complete skill references are included and `colabllm skill --output <temporary-dir>`
   works outside the checkout. Adjust packaging rules and skill-export checks when adding resource
   types or changing the skill layout. Completion requires both the runtime and its exported user
   guidance to be usable from the built package.

## References

- [references/auth.md](references/auth.md) -- choosing builtin vs Caddy, client usage, hardening
- [references/cf-tunnel.md](references/cf-tunnel.md) -- Cloudflare quick/named tunnels and project-domain selection
- [references/frp.md](references/frp.md) -- FRP provisioning/adoption, credentials, DNS, diagnostics and cleanup
- [references/troubleshooting.md](references/troubleshooting.md) -- symptoms → causes → fixes
