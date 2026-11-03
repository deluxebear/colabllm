# LLM inference and model UIs

## Qwen Flash

`qwen-flash` defaults to G4 and offers an OpenAI-compatible API, including vision support.
Discover the installed instance URL and token via `info --json` and, privately, `--show-secrets`.
The API base is `<public-url>/v1`; model alias is obtained from authenticated `GET /v1/models`.
Do not hard-code a personal deployment domain or model alias.

A standard-library client can capture credentials without displaying them:

```python
import json
import subprocess
import urllib.request

info = json.loads(subprocess.check_output(
    ["colabllm", "qwen-flash", "info", "--json", "--show-secrets"], text=True))
base = info["url"].rstrip("/")
headers = {"Authorization": "Bearer " + info["api_token"], "Content-Type": "application/json"}
# Reject redirects: do not forward authentication to another origin.
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
opener = urllib.request.build_opener(NoRedirect())
with opener.open(urllib.request.Request(base + "/v1/models", headers=headers), timeout=30) as response:
    model = json.load(response)["data"][0]["id"]
request = urllib.request.Request(base + "/v1/chat/completions", headers=headers,
    data=json.dumps({"model": model, "messages": [{"role": "user", "content": "Say hello."}],
                     "max_tokens": 128, "stream": False}).encode())
with opener.open(request, timeout=120) as response:
    answer = json.load(response)
print(answer["choices"][0]["message"]["content"])
```

Use an existing OpenAI-compatible client if already installed. For images, use the server's supported
chat content format and accessible image URL or encoded data. Local filenames do not identify files
inside the VM. Constrain token budgets to the task. `/health` can be public: validate authentication
on `/v1/models`, where missing and incorrect keys must fail.

## Ollama

```bash
colabllm ollama up --models qwen2.5:7b
colabllm ollama status --json
```

Use the instance's Caddy Bearer token for `/api/tags`, `/api/generate` and supported `/v1` endpoints.
A requested model must be available remotely. `up --models` controls preload; it is not a command
to invoke inference on a running instance. When an instance already runs, inspect its model API
instead of stopping it solely to change startup arguments.

## Unsloth Studio

`unsloth-studio` provides its own login UI. Start it, query `info`, then supply the URL to the user
and explain how to retrieve credentials privately with `info --show-secrets`. Use installed documented
interfaces for automation; do not invent a fine-tuning CLI subcommand. Training work can be expensive
and long-running; retain the VM and connection while it is active.
