# Auth for Colab LLM services

**Rule: nothing is exposed without authentication.** A public `trycloudflare.com` or custom-domain
URL is reachable by anyone who learns it.

## Decision

| Service | `auth=` | What protects it |
| --- | --- | --- |
| Has its own login (Unsloth Studio, Open WebUI, ComfyUI-Manager w/ login...) | `builtin` | the app; credentials are reported via `CT_CREDENTIALS` |
| OpenAI-compatible server with a native key (vLLM `--api-key`, llama.cpp `--api-key`, TGI) | `builtin` | use the runner-injected `CT_API_TOKEN`; keep secrets out of `Service.env` in source |
| No auth at all (Ollama, Gradio demos, ComfyUI, Jupyter-less HTTP servers, `http.server`) | `caddy` (default) | Caddy in the VM |

## Caddy layer (cell 3)

```
internet -> Cloudflare -> cloudflared -> Caddy :CT_PUBLIC_PORT -> service 127.0.0.1:CT_SERVICE_PORT
internet -> server HTTPS Caddy -> frps -> Colab frpc -> Caddy :CT_PUBLIC_PORT -> service
```

With FRP, the server Caddy terminates public HTTPS; the Colab Caddy still enforces
the service's authentication. FRP's connection Token is separate from the model API
Token. For `auth=builtin`, frpc targets the app's authenticated port directly.

- Browsers: HTTP basic auth (`CT_AUTH_USER` / `CT_AUTH_PASS`, bcrypt-hashed in the Caddyfile).
- API clients: `Authorization: Bearer <CT_API_TOKEN>` (works with OpenAI SDKs: `api_key=<token>`).
- The `Authorization` header is stripped before reaching the service; `Host` is rewritten to the
  upstream (`REWRITE_HOST`, needed by Ollama; set `False` for apps that build absolute URLs from Host).
- `flush_interval -1` so token streaming (SSE / chunked) and WebSockets pass through unbuffered.
- The service must bind to `127.0.0.1`, otherwise the proxy can be bypassed inside the VM network.
- Credentials are generated once per service by the script, kept in `~/.colabllm/services/<name>/state.json`
  (0600) and reused across restarts; rotate with `up --new-credentials`.

Verified behaviour: no credentials → 401, wrong password → 401, basic auth → 200, Bearer → 200,
wrong Bearer → 401.

## API key lookup and clients

The default `info`, `ps` and `status` output hides credentials. A missing printed key does not mean
that authentication is disabled. Use the CLI so `COLABLLM_HOME` and instance selection are honored:

```bash
colabllm qwen-flash info --show-secrets
colabllm qwen-flash info --json --show-secrets | jq -r '.api_token'
colabllm qwen-flash info -i 2 --json --show-secrets | jq -r '.api_token'
```

For client calls, capture credentials without echoing them or using `eval`:

```bash
URL=$(colabllm ollama info --json | jq -r '.url')
TOKEN=$(colabllm ollama info --json --show-secrets | jq -r '.api_token')
curl -H "Authorization: Bearer $TOKEN" "$URL/api/generate" \
  -d '{"model":"qwen2.5:0.5b","prompt":"hi","stream":false}'
```

`qwen-flash` exposes the OpenAI-compatible base URL `<public-url>/v1`, with native
`llama-server --api-key-file` authentication. Check `/v1/models` when validating its key;
`/health` can return 200 without authentication and is not proof of API access control.
Use fake keys in examples and tests; real keys belong in private runtime state, not skill files,
source, notebook cells, printed commands, logs, or version control.

## User-supplied keys and rotation

There is currently no public CLI command to set a user-supplied API key. `up --new-credentials`
generates a random replacement and requires a stopped instance; it does not accept the supplied key.
For a manual update, use the installed package's `State.update` transaction, not a direct JSON
rewrite: change `api_token` and the saved `env.CT_API_TOKEN` together. This changes future startup
injection but does not update the already running server or notebook kernel.

Before applying a live update, inspect active requests/jobs and retain the Colab session and
keep-alive connection. Choose the service-specific reload/restart path; avoid `down` when only the
model process needs a restart, because `down` releases the VM. Transfer secrets privately, without
embedding them in SSH command arguments. Verify the requested key succeeds and both no key and
the previous key fail on an authenticated endpoint; then verify `status --json`. Keep failure state
recoverable if applying the new key to the server fails.

For an idle `qwen-flash`, the verified procedure is to inspect `/slots` for in-flight work, derive
the key-file path from the running server's `--api-key-file` argument, replace that file atomically
with mode 0600, and restart only that llama-server process via its existing watchdog. The key is
read at server startup. Preserve the runner and kernel. Check the current watchdog implementation:
its in-memory `API_TOKEN` remains old after a file-only change; any authenticated watchdog requests
must be updated coherently or the rotation will cause restart loops. Reused notebook variables are
refreshed by normal startup injection on the next full launch.

A blocking keep-alive cell prevents queued `colab exec` code from running immediately. For live
inspection, use the supported SSH channel with a finite local timeout; send Python via a quoted
`python3 -c` command, reserving stdin for private data. A stale diagnostic SSH connection can cause
HTTP 429 on the next attempt: close only the diagnostic connection you own before retrying.

## Extra hardening (optional)

- Fixed domain + Cloudflare Access (Zero Trust, email OTP) in front of Caddy:
  `cf zero-trust access applications create --body '{...self_hosted, domain: <host>...}'`.
  Browser-only; API clients then need Access service tokens.
  This requires traffic to pass through Cloudflare; FRP's default DNS-only records
  route requests directly to the server Caddy.
- Rotate credentials after sharing a URL; `down` the service when not in use.
