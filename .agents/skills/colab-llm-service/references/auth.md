# Auth for Colab LLM services

**Rule: nothing is exposed without authentication.** A public `trycloudflare.com` or custom-domain
URL is reachable by anyone who learns it.

## Decision

| Service | `auth=` | What protects it |
| --- | --- | --- |
| Has its own login (Unsloth Studio, Open WebUI, ComfyUI-Manager w/ login...) | `builtin` | the app; credentials are reported via `CT_CREDENTIALS` |
| OpenAI-compatible server with a native key (vLLM `--api-key`, llama.cpp `--api-key`, TGI) | `builtin` | pass the key from `CT_API_TOKEN`-like env you add in `Service.env` |
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

## Client examples

```bash
eval "$(colabllm ollama info --show-secrets >/dev/null; python3 - <<'EOF'
import json; from pathlib import Path; s=json.loads((Path.home() / '.colabllm/services/ollama/state.json').read_text()); print(f"URL={s['url']} TOKEN={s['api_token']}")
EOF
)"
curl -H "Authorization: Bearer $TOKEN" "$URL/api/generate" -d '{"model":"qwen2.5:0.5b","prompt":"hi","stream":false}'
curl -u admin:<password> "$URL/api/tags"
```

```python
from openai import OpenAI      # Ollama exposes /v1 (OpenAI compatible)
client = OpenAI(base_url=f"{URL}/v1", api_key=TOKEN)
```

## Extra hardening (optional)

- Fixed domain + Cloudflare Access (Zero Trust, email OTP) in front of Caddy:
  `cf zero-trust access applications create --body '{...self_hosted, domain: <host>...}'`.
  Browser-only; API clients then need Access service tokens.
  This requires traffic to pass through Cloudflare; FRP's default DNS-only records
  route requests directly to the server Caddy.
- Rotate credentials after sharing a URL; `down` the service when not in use.
