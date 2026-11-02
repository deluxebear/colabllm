---
name: colab-llm-service
description: >-
  Run a large-model service (Unsloth Studio, Ollama, vLLM, llama.cpp, ComfyUI, Gradio apps...) on a
  Google Colab GPU via the Colab CLI and expose it through Cloudflare Tunnel, following this repo's
  "one service = one notebook + one Python script" convention. Use when adding a new service,
  starting/stopping one, giving it a fixed domain with the `cf` CLI, putting Caddy auth in front of a
  service without its own login, or debugging Colab session / tunnel / keep-alive problems.
---

# Colab LLM service

Every model service in this repo is a pair of files in `services/`, sharing one library:

```
services/<name>.py       execution script (local): session, Drive, cf tunnel, credentials, keep-alive
services/<name>.ipynb    notebook (Colab VM): setup, service, Caddy auth, cloudflared, keep-alive loop
lib/colabsvc.py          shared runner used by every script (Service dataclass + CLI)
templates/               service.py + service.ipynb to copy; build_notebooks.py regenerates notebooks
.state/<name>/           runtime state (chmod 600): url, passwords, tunnel id, run.log  -- gitignored
```

Existing services: `unsloth-studio` (built-in login, L4), `ollama` (Caddy auth, T4),
`comfyui` (ComfyUI + ComfyUI-Manager, Caddy auth, L4, on-demand workflow model download to local SSD via `--workflow` / `pull-workflow`),
`qwen-flash` (`SC117/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-GGUF` IQ3_S, 800K YaRN 3.125x + BF16 vision,
`llama-server --api-key-file` built-in auth, G4 GPU, default domain `qwen.example.com`),
`nemotron-transcribe` (`nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B` via `vllm serve` + `nemo_speechlm`,
Bearer API key auth, G4 GPU, default domain `transcribe.example.com`, single & batch Drive audio CLI `transcribe`).

## Daily use

```bash
./services/<name>.py up                 # quick tunnel  https://<random>.trycloudflare.com
./services/<name>.py up --domain        # fixed domain  https://<name>.example.com   (cf CLI)
./services/<name>.py up --domain llm.example.com --gpu A100
./services/<name>.py info               # URL + credentials        logs -f   status (probes the URL)
./services/<name>.py down               # stop runner + Colab VM  (--delete-tunnel: also tunnel + DNS)
```

`up` returns once the notebook prints `CT_READY`; a detached local runner keeps `colab exec`
attached (= keep-alive). Requirements: `colab` CLI logged in, `colab dfs consent` done once,
`cf` CLI logged in (`cf auth whoami`) for `--domain`.

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
9. **Secrets never go into the notebook**: the script generates them, stores them in `.state/`
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
| `CT_PUBLIC_PORT` | what cloudflared targets: Caddy port if `caddy`, else the service port |
| `CT_AUTH_USER`, `CT_AUTH_PASS`, `CT_API_TOKEN` | Caddy basic-auth login and Bearer token |
| `CT_TUNNEL_TOKEN`, `CT_PUBLIC_HOSTNAME` | set with `--domain`: run the named tunnel |
| `CT_DATA_DIR` | `/content/drive/MyDrive/colab-llm/<name>` -- cache models/outputs here |

Markers the notebook prints (parsed by `colabsvc`): `CT_PUBLIC_URL=<url>`,
`CT_CREDENTIALS user=<u> pass=<p>` (app's own login, e.g. Unsloth), `CT_READY`.

Standard cell order (generated by `templates/build_notebooks.py`):
`0 config` → `1 setup (idempotent)` → `2 service: def start_service(); PROCS["service"]=...` →
`3 Caddy auth` → `4 cloudflared (token → fixed domain, else quick)` → `5 CT_READY + watchdog loop`.
Put `# @ct:skip` in a cell (or `up --skip N`) to skip it.

## Add a new service

1. Copy the pair: `cp templates/service.py services/<name>.py; cp templates/service.ipynb services/<name>.ipynb`
   (`<name>` = file name = Colab session = tunnel `colab-<name>` = host `<name>.example.com`).
2. Edit `services/<name>.py`: `name`, `notebook`, `service_port`, `auth`, `gpu`, extra `env`.
3. Edit the notebook's **cell 1 (setup)** and **cell 2 (`start_service`)** only; bind to `127.0.0.1`.
   Cache large downloads in `DATA_DIR`. Adjust the health path in cell 5 if `/` is not a good probe.
   For a reusable recipe add it to `templates/build_notebooks.py` instead and regenerate.
4. Pick auth -- see [references/auth.md](references/auth.md). Rule: **never expose a service without
   auth**; if it has none, `auth="caddy"`.
5. `./services/<name>.py up --dry-run`, then a real `up` (CPU `--gpu none` for wiring tests), check
   `status` (unauthenticated probe must be 401 for Caddy services), then `down`.

## References

- [references/auth.md](references/auth.md) -- choosing builtin vs Caddy, client usage, hardening
- [references/cf-tunnel.md](references/cf-tunnel.md) -- what `--domain` does with the `cf` CLI, cleanup
- [references/troubleshooting.md](references/troubleshooting.md) -- symptoms → causes → fixes
