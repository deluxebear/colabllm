# Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Service worked, then the URL died after a while | `colab exec` disconnected (Ctrl-C, terminal closed, laptop slept, network) -> Colab reclaimed the VM | Use the default detached `up`; keep the local machine awake / run on a server. `colab sessions` shows whether the VM still exists |
| Later cells never ran | Disconnected before the last cell started (cells are sent one by one by the client) | Re-run `up` (session is reused; `--skip` finished cells) |
| `colab exec` / `colab dfs status` hangs | The kernel is busy with the blocking last cell; new execs queue behind it | Expected. Use `logs`, `status` (HTTP probe), or `colab stop` / `down` |
| `up` waits forever, no `CT_READY` | A cell raised (exit code stays 0) | `logs -n 200`; look for the traceback; fix the notebook; `down`, `up` |
| Unauthenticated probe returns 404/200 on a Caddy service | Caddy is not the thing answering on `CT_PUBLIC_PORT` | Never use 8080 (Colab runtime server) or 8012 (dfs shim). The notebook now fails closed: cell 3 requires a 401 from Caddy and cell 4 refuses to start the tunnel otherwise |
| `caddy exited -- is :PORT in use?` | Port collision on the VM | Change `public_port` in `services/<name>.py` |
| Ollama returns 403 through the proxy | Ollama rejects foreign `Host` headers | Keep `REWRITE_HOST = True` in cell 3 |
| App generates wrong absolute links behind Caddy | `Host` rewritten to `127.0.0.1:<port>` | Set `REWRITE_HOST = False` for that notebook |
| Notebook hangs at `drive.mount()` | Browser consent flow | Use `colab dfs mount` (done by the script); the injected prelude skips `drive.mount()` |
| `Error while fetching HF_TOKEN secret value from your vault` / `unauthenticated requests to the HF Hub` | `huggingface_hub` on Colab calls `google.colab.userdata.get('HF_TOKEN')`, which times out in CLI runs | Fixed automatically: the prelude patches `userdata.get` to read `os.environ`, and `colabsvc` forwards local `HF_TOKEN` / `~/.cache/huggingface/token` / `--hf-token <tok>` (or reads `/content/drive/MyDrive/colab-llm/hf_token.txt`) |
| `fatal: destination path ... already exists` | Re-running a non-idempotent setup cell on a reused session | `test -d x/.git && git -C x pull --ff-only \|\| git clone ...` |
| `cf ... --tunnel-config-origin-request-access-* are required` | `cf` beta flag validation | Use `--body` JSON (already done in `lib/colabsvc.py`) |
| `cf` errors about auth | Not logged in / wrong profile | `cf auth whoami`, `cf auth login` |
| Fixed domain shows Cloudflare error 1033 | Tunnel has no connector (VM down or cloudflared died) | `status`; watchdog restarts cloudflared; otherwise `up` again |
| Fixed domain shows 502 | cloudflared up, but nothing on `CT_PUBLIC_PORT` | Caddy/service died; check `logs`; watchdog restarts registered processes |
| `colab new` fails for a GPU | Quota / tier / availability | Try another `--gpu`, check `colab usage` |

Useful commands: `colab sessions`, `colab status -s <name>`, `colab usage`, `colab log -s <name>`,
`cf tunnels list --name colab-<name>`, `cf dns records list -z example.com --name <host>`.
Remote logs live in `/content/ct/<process>.log` on the VM (readable only while the kernel is free,
or via `colab download -s <name> /content/ct/caddy.log ./caddy.log`).
