# Firecrawl Local (`firecrawl-local`)

Runs [`firecrawl-local`](https://github.com/firecrawl/firecrawl/tree/main/apps/local) (the standalone Rust Firecrawl v2 HTTP API) on a **zero-GPU Google Colab CPU VM** (`gpu=None`, optional `--high-mem`) behind Caddy Basic Auth + Bearer token authentication, with **Google Drive binary caching** and **native-sandbox headless Chrome** for JavaScript / SPA rendering.

## Highlights

- **Single Rust binary cached on Google Drive**:
  - First launch sparse-clones `apps/local` + `apps/api/native/src` onto local NVMe SSD (`/content/build`), installs minimal `rustup`, compiles the release binary (`CARGO_PROFILE_RELEASE_LTO=thin` by default), and saves it atomically to `/content/drive/MyDrive/colab-llm/firecrawl-local/bin/<cache_key>` with `.sha256` verification.
  - Cache keys track the latest commit touching `apps/local` or `apps/api/native/src` + CPU arch + glibc version, so unrelated Firecrawl monorepo commits never invalidate the cache.
  - Subsequent sessions restore the binary from Drive in ~1–2 seconds and skip Rust installation completely.
- **Native-sandbox headless Chrome (non-root isolation)**:
  - Runs `firecrawl-local serve` as an unprivileged `firecrawl` user (`runuser -w ... -u firecrawl`), allowing `google-chrome-stable --headless=new` to run with its **full native sandbox** on Colab's Ubuntu 24.04 user namespaces while keeping `/content/drive` inaccessible to browser processes.
  - Pass `--browser never` if you only need fast static HTML, SSR, sitemap, PDF, and Office document extraction without installing Chrome.
- **Firecrawl v2 API & SDK compatible**:
  - Exposes `POST /v2/scrape`, `POST /v2/map`, `POST/GET/DELETE /v2/crawl`, `POST/GET/DELETE /v2/batch/scrape`, and `GET /health`.
  - Caddy handles Bearer token authentication (`Authorization: Bearer <token>`) and rewrites `Host` to `127.0.0.1:3002`, so fixed domains, FRP, and `--quick-tunnel` all work seamlessly.

## Quick start

```bash
# Start on a Colab CPU VM (first run compiles & caches to Drive; subsequent runs restore in ~1s)
colabllm firecrawl-local up

# Optional: high-RAM CPU VM for large crawls, or static-only mode without Chrome
colabllm firecrawl-local up --high-mem
colabllm firecrawl-local up --browser never

# Force recompiling from latest upstream and refreshing the Drive cache
colabllm firecrawl-local up --rebuild

# Pin a specific git ref or crates.io release
colabllm firecrawl-local up --ref crates:0.1.0
```

## CLI usage

```bash
colabllm firecrawl-local health --json
colabllm firecrawl-local scrape https://example.com --markdown
colabllm firecrawl-local scrape https://example.com -f markdown,links,screenshot -o page.json
colabllm firecrawl-local map https://docs.firecrawl.dev --limit 100
colabllm firecrawl-local crawl https://example.com --limit 20 --wait --out ./pages
colabllm firecrawl-local batch https://example.com https://iana.org --wait --json
colabllm firecrawl-local down
```

## Using official Firecrawl SDKs

Retrieve the endpoint URL and API token with `colabllm firecrawl-local info --show-secrets`, then point any Firecrawl SDK at it:

```python
from firecrawl import Firecrawl

app = Firecrawl(
    api_url="https://firecrawl.example.com",
    api_key="<CT_API_TOKEN>",
)
doc = app.scrape("https://example.com", formats=["markdown"])
print(doc.markdown)
```

## Options on `up`

| Flag | Default | Meaning |
| --- | --- | --- |
| `--high-mem` | off | Request high-RAM Colab CPU shape |
| `--browser {auto,never,always}` | `auto` | Headless Chrome JS rendering (`never` skips Chrome install) |
| `--cloud {off,search,auto}` | `off` | Firecrawl Cloud hand-off policy (`off` keeps all requests local) |
| `--ref REF` | `main` | Git branch/tag/SHA or `crates:<version>` |
| `--lto {thin,fat}` | `thin` | Rust release LTO mode on cache miss |
| `--rebuild` | off | Ignore Drive binary cache and recompile |
| `--user-agent UA` | — | Override outbound HTTP / Chrome User-Agent |
| `--scroll` | off | Scroll JS-rendered pages before extraction |
| `--firecrawl-api-key KEY` | — | Optional Firecrawl Cloud API key for `--cloud search\|auto` |

See also [Common runtime operations](runtime-common.md) for shared lifecycle, tunnel, multi-account, and troubleshooting flags.
