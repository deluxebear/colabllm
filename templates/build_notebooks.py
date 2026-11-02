#!/usr/bin/env python3
"""Generate service notebooks from the standard cells.

Every service notebook is self-contained (runs from the service script *or* pasted into the
Colab UI) and follows the same cell order:

  0. config        -- read CT_* env injected by `colab exec --env`, helpers
  1. setup         -- idempotent install                         (service specific)
  2. service       -- def start_service() -> Popen on 127.0.0.1  (service specific)
  3. auth          -- Caddy basic auth + Bearer token when CT_AUTH == "caddy"
  4. tunnel        -- cloudflared: token -> fixed domain, else quick tunnel; prints CT_PUBLIC_URL
  5. keep-alive    -- prints CT_READY, watchdog restarts dead processes, never returns

Usage:  python3 templates/build_notebooks.py      (rewrites templates/ and services/*.ipynb)
Edit the generated notebooks directly if a service needs something special; re-running this
script overwrites them.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CONFIG = r'''# @title 0. Config -- values come from `colab exec --env` (defaults allow running in the Colab UI)
import json, os, re, secrets, shutil, stat, subprocess, sys, time, urllib.request, urllib.error

SERVICE      = os.environ.get("CT_SERVICE", "%(name)s")
SERVICE_PORT = int(os.environ.get("CT_SERVICE_PORT", "%(service_port)d"))
AUTH         = os.environ.get("CT_AUTH", "%(auth)s")            # caddy | builtin | none
PUBLIC_PORT  = int(os.environ.get("CT_PUBLIC_PORT", "%(public_port)d"))  # what cloudflared targets
AUTH_USER    = os.environ.get("CT_AUTH_USER", "admin")
AUTH_PASS    = os.environ.get("CT_AUTH_PASS") or secrets.token_urlsafe(18)
API_TOKEN    = os.environ.get("CT_API_TOKEN") or "sk-" + secrets.token_urlsafe(32)
TUNNEL_TOKEN = os.environ.get("CT_TUNNEL_TOKEN", "")            # set -> fixed domain (named tunnel)
PUBLIC_HOST  = os.environ.get("CT_PUBLIC_HOSTNAME", "")
CREDS_FROM_ENV = bool(os.environ.get("CT_AUTH_PASS"))

WORK = "/content/ct"                                            # logs + binaries of this notebook
os.makedirs(WORK, exist_ok=True)
for _p in ("/content/drive/MyDrive/colab-llm/hf_token.txt",
           "/content/drive/MyDrive/colab-services/hf_token.txt",
           "/content/drive/MyDrive/colab-services/qwen-flash/hf_token.txt"):
    if not os.environ.get("HF_TOKEN") and os.path.isfile(_p):
        _tok = open(_p).read().strip()
        if _tok:
            os.environ["HF_TOKEN"] = os.environ["HUGGING_FACE_HUB_TOKEN"] = _tok
            break
DATA_DIR = os.environ.get("CT_DATA_DIR", f"/content/drive/MyDrive/colab-llm/{SERVICE}")
if not os.path.isdir("/content/drive/MyDrive"):                 # Drive not mounted -> local fallback
    DATA_DIR = f"/content/data/{SERVICE}"
os.makedirs(DATA_DIR, exist_ok=True)
PROCS = {}                                                      # name -> (Popen, restart function)

def ct_spawn(name, args, env=None, cwd=None):
    """Start a background process; output goes to /content/ct/<name>.log."""
    logf = open(f"{WORK}/{name}.log", "a")
    return subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, cwd=cwd,
                            env={**os.environ, **(env or {})}, start_new_session=True)

def ct_wait_http(url, timeout=600, ok=lambda code: code < 500):
    """Wait until url answers with a non-5xx status (401/404 count as 'listening')."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if ok(r.status): return r.status
        except urllib.error.HTTPError as e:
            if ok(e.code): return e.code
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f"{url} not ready after {timeout}s")

def ct_tail(name, n=30):
    p = f"{WORK}/{name}.log"
    return "".join(open(p, errors="replace").readlines()[-n:]) if os.path.exists(p) else ""

def ct_fetch_binary(path, url):
    if not os.path.exists(path):
        urllib.request.urlretrieve(url, path)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path

print(f"service={SERVICE} port={SERVICE_PORT} auth={AUTH} public_port={PUBLIC_PORT} "
      f"tunnel={'named ' + PUBLIC_HOST if TUNNEL_TOKEN else 'quick'} data={DATA_DIR}")
'''

CADDY = r'''# @title 3. Auth layer -- Caddy: basic auth for browsers, `Authorization: Bearer <token>` for API clients
# PUBLIC_PORT must be free on the VM: Colab itself uses 8080 (runtime server) and 8012 (colab dfs shim).
# Only used when the service has no auth of its own (CT_AUTH=caddy). The service itself listens on
# 127.0.0.1 only, cloudflared targets Caddy, so every public request passes through auth.
REWRITE_HOST = True   # send Host: 127.0.0.1:<port> upstream (Ollama & co. reject foreign Host headers)

def start_caddy():
    if not os.path.exists("/usr/local/bin/caddy"):
        tag = json.loads(urllib.request.urlopen(
            "https://api.github.com/repos/caddyserver/caddy/releases/latest", timeout=30).read())["tag_name"]
        tgz = "/tmp/caddy.tgz"
        urllib.request.urlretrieve(f"https://github.com/caddyserver/caddy/releases/download/{tag}/"
                                   f"caddy_{tag.lstrip('v')}_linux_amd64.tar.gz", tgz)
        subprocess.run(["tar", "-xzf", tgz, "-C", "/usr/local/bin", "caddy"], check=True)
    import hashlib
    cookie_tok = hashlib.sha256(f"ct-cookie-v2:{AUTH_USER}:{AUTH_PASS}:{API_TOKEN}".encode()).hexdigest()
    hashed = subprocess.check_output(["caddy", "hash-password", "--plaintext", AUTH_PASS], text=True).strip()
    upstream = f"""reverse_proxy 127.0.0.1:{SERVICE_PORT} {{
            {"header_up Host {upstream_hostport}" if REWRITE_HOST else ""}
            {"header_up Origin http://{upstream_hostport}" if REWRITE_HOST else ""}
            header_up -Authorization
            flush_interval -1
        }}"""
    caddyfile = f"""{{
    admin off
    auto_https off
}}
:{PUBLIC_PORT} {{
    encode zstd gzip
    @bearer header Authorization "Bearer {API_TOKEN}"
    handle @bearer {{
        {upstream}
    }}
    @cookie header Cookie *ct_session={cookie_tok}*
    handle @cookie {{
        {upstream}
    }}
    handle {{
        route {{
            basic_auth {{
                {AUTH_USER} {hashed}
            }}
            header +Set-Cookie "ct_session={cookie_tok}; Path=/; HttpOnly; SameSite=Lax"
            {upstream}
        }}
    }}
}}
"""
    open(f"{WORK}/Caddyfile", "w").write(caddyfile)
    subprocess.run(["pkill", "-f", "caddy run"], check=False)
    return ct_spawn("caddy", ["caddy", "run", "--config", f"{WORK}/Caddyfile", "--adapter", "caddyfile"])

AUTH_OK = False   # cell 4 refuses to open the tunnel unless this becomes True (fail closed)
if AUTH == "caddy":
    PROCS["caddy"] = (start_caddy(), start_caddy)
    time.sleep(2)
    if PROCS["caddy"][0].poll() is not None:   # e.g. port taken (Colab uses 8080 itself)
        raise RuntimeError(f"caddy exited -- is :{PUBLIC_PORT} in use?\n" + ct_tail("caddy"))
    # Must be 401 from *our* Caddy and must NOT leak Set-Cookie on unauthenticated 401 responses.
    ct_wait_http(f"http://127.0.0.1:{PUBLIC_PORT}/", timeout=60, ok=lambda c: c == 401)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PUBLIC_PORT}/", timeout=5)
    except urllib.error.HTTPError as _e:
        if _e.headers.get("Set-Cookie"):
            raise RuntimeError("Caddy leaked Set-Cookie on unauthenticated 401 response!")
    AUTH_OK = True
    print(f"caddy on :{PUBLIC_PORT} -> 127.0.0.1:{SERVICE_PORT} (unauthenticated request -> 401)")
else:
    AUTH_OK = True
    print(f"auth={AUTH}: no proxy, cloudflared targets the service on :{PUBLIC_PORT} directly")
'''

TUNNEL = r'''# @title 4. Cloudflare tunnel -- token: fixed domain (named tunnel, created by the service script via `cf`)
#                               no token: temporary *.trycloudflare.com URL
CLOUDFLARED = ct_fetch_binary("/usr/local/bin/cloudflared",
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64")

def start_cloudflared():
    subprocess.run(["pkill", "-f", "cloudflared tunnel"], check=False)
    open(f"{WORK}/cloudflared.log", "w").close()
    if TUNNEL_TOKEN:   # ingress (hostname -> localhost:PUBLIC_PORT) lives in the Cloudflare config
        args = [CLOUDFLARED, "tunnel", "--no-autoupdate", "--protocol", "http2", "run", "--token", TUNNEL_TOKEN]
    else:
        args = [CLOUDFLARED, "tunnel", "--no-autoupdate", "--protocol", "http2",
                "--url", f"http://127.0.0.1:{PUBLIC_PORT}"]
    return ct_spawn("cloudflared", args)

def wait_public_url(timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        log = ct_tail("cloudflared", 400)
        if TUNNEL_TOKEN and "Registered tunnel connection" in log:
            return f"https://{PUBLIC_HOST}"
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", log)
        if not TUNNEL_TOKEN and m:
            return m.group(0)
        time.sleep(1)
    raise RuntimeError("cloudflared did not come up:\n" + ct_tail("cloudflared"))

# A failing cell does not stop `colab exec`, so guard explicitly: no auth layer -> no tunnel.
if not globals().get("AUTH_OK"):
    raise RuntimeError("auth layer not ready (cell 3 failed) -- refusing to expose the service")
PROCS["cloudflared"] = (start_cloudflared(), start_cloudflared)
PUBLIC_URL = wait_public_url()
print(f"CT_PUBLIC_URL={PUBLIC_URL}")
'''

KEEPALIVE = r'''# @title 5. Ready + keep-alive -- KEEP THIS CELL RUNNING
# The Colab session only lives while the `colab exec` connection is open and this cell blocks.
# The watchdog restarts any process from PROCS that died; a heartbeat line is printed every 10 min.
ct_wait_http(f"http://127.0.0.1:{SERVICE_PORT}%(health_path)s", timeout=%(ready_timeout)d)
if AUTH == "caddy" and not CREDS_FROM_ENV:   # started from the Colab UI: show generated credentials
    print(f"CT_CREDENTIALS user={AUTH_USER} pass={AUTH_PASS}")
    print(f"API token: {API_TOKEN}")
print(f"{SERVICE} is up: {PUBLIC_URL}")
print("CT_READY", flush=True)

last_beat = time.time()
while True:
    time.sleep(30)
    for name, (proc, restart) in list(PROCS.items()):
        if proc.poll() is not None:
            print(f"[watchdog] {name} exited with {proc.returncode}; restarting\n{ct_tail(name, 15)}", flush=True)
            PROCS[name] = (restart(), restart)
    if time.time() - last_beat > 600:
        last_beat = time.time()
        print(f"[heartbeat] {time.strftime('%%H:%%M:%%S')} " +
              " ".join(f"{n}={'up' if p.poll() is None else 'down'}" for n, (p, _) in PROCS.items()), flush=True)
'''


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text}


def code(text):
    return {"cell_type": "code", "metadata": {}, "outputs": [], "execution_count": None, "source": text}


def notebook(title, intro, cfg, setup, service, keepalive=None, extra_after_tunnel=None):
    cells = [md(f"# {title}\n\n{intro}\n\n"
                "Run it with the service script (`services/<name>.py up`), which creates the Colab session, "
                "mounts Drive with `colab dfs`, optionally creates a named Cloudflare tunnel, and injects "
                "`CT_*` settings. Cell order is the standard contract -- see "
                "`.agents/skills/colab-llm-service/SKILL.md`."),
             code(CONFIG % cfg), code(setup), code(service), code(CADDY), code(TUNNEL)]
    if extra_after_tunnel:
        cells.append(code(extra_after_tunnel))
    cells.append(code(keepalive if keepalive is not None else KEEPALIVE % cfg))
    for i, c in enumerate(cells):
        c["id"] = f"cell-{i}"
    return {"cells": cells, "metadata": {"accelerator": "GPU", "colab": {"provenance": []},
                                         "kernelspec": {"display_name": "Python 3", "name": "python3"}},
            "nbformat": 4, "nbformat_minor": 5}


def write(path, nb):
    import ast
    for idx, c in enumerate(nb["cells"]):
        if c["cell_type"] == "code":
            src = c["source"] if isinstance(c["source"], str) else "".join(c["source"])
            py_src = "\n".join(
                ("# " + line if line.lstrip().startswith(("%", "!")) else line)
                for line in src.splitlines()
            )
            ast.parse(py_src, filename=f"{path.name}:cell-{idx}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote", path.relative_to(ROOT))


# --------------------------------------------------------------------------- template (no-auth demo service)
TEMPLATE_CFG = dict(name="my-service", service_port=8000, auth="caddy", public_port=18080,
                    health_path="/", ready_timeout=600)
TEMPLATE_SETUP = r'''# @title 1. Setup -- idempotent: safe to re-run on a reused session
# Install the service here. Skip work that is already done, cache big downloads in DATA_DIR (Drive).
# Example:
#   if not shutil.which("llama-server"): subprocess.run("pip install -q ...", shell=True, check=True)
print("nothing to install for the demo service")
'''
TEMPLATE_SERVICE = r'''# @title 2. Service -- must listen on 127.0.0.1:SERVICE_PORT; register it in PROCS for the watchdog
def start_service():
    # replace with the real server, e.g. ["vllm", "serve", MODEL, "--host", "127.0.0.1", "--port", str(SERVICE_PORT)]
    return ct_spawn("service", [sys.executable, "-m", "http.server", str(SERVICE_PORT), "--bind", "127.0.0.1"],
                    cwd=DATA_DIR)

PROCS["service"] = (start_service(), start_service)
print(f"service started on 127.0.0.1:{SERVICE_PORT}")
'''

# --------------------------------------------------------------------------- ollama (no built-in auth -> caddy)
OLLAMA_CFG = dict(name="ollama", service_port=11434, auth="caddy", public_port=18080,
                  health_path="/api/version", ready_timeout=900)
OLLAMA_SETUP = r'''# @title 1. Setup -- install Ollama once per VM (idempotent)
MODELS = os.environ.get("CT_MODELS", "qwen2.5:0.5b").split(",")   # pulled after the server starts
if not shutil.which("ollama"):
    subprocess.run("apt-get install -y -qq zstd pciutils >/dev/null 2>&1; "
                   "curl -fsSL https://ollama.com/install.sh | sh", shell=True, check=True)
print(subprocess.run(["ollama", "--version"], capture_output=True, text=True).stdout.strip())
'''
OLLAMA_SERVICE = r'''# @title 2. Service -- ollama serve on 127.0.0.1 (no auth of its own -> Caddy in front)
OLLAMA_ENV = {"OLLAMA_HOST": f"127.0.0.1:{SERVICE_PORT}",
              "OLLAMA_MODELS": os.environ.get("CT_OLLAMA_MODELS", "/content/ollama/models"),
              "OLLAMA_KEEP_ALIVE": "30m"}

def start_service():
    subprocess.run(["pkill", "-f", "ollama serve"], check=False)
    return ct_spawn("service", ["ollama", "serve"], env=OLLAMA_ENV)

PROCS["service"] = (start_service(), start_service)
ct_wait_http(f"http://127.0.0.1:{SERVICE_PORT}/api/version", timeout=120)
for m in MODELS:
    print(f"pulling {m} ...", flush=True)
    subprocess.run(["ollama", "pull", m], env={**os.environ, **OLLAMA_ENV}, check=True,
                   stdout=subprocess.DEVNULL)
print(subprocess.run(["ollama", "list"], env={**os.environ, **OLLAMA_ENV}, capture_output=True, text=True).stdout)
'''

# --------------------------------------------------------------------------- unsloth studio (built-in auth)
UNSLOTH_CFG = dict(name="unsloth-studio", service_port=8888, auth="builtin", public_port=8888,
                   health_path="/api/health", ready_timeout=900)
UNSLOTH_SETUP = r'''# @title 1. Setup -- clone or update the repo, then Unsloth's own installer (re-runs are fast)
%cd /content
!test -d unsloth/.git && git -C unsloth pull --ff-only || git clone --depth 1 --branch main https://github.com/unslothai/unsloth.git
%cd /content/unsloth
!chmod +x studio/setup.sh && ./studio/setup.sh --local
'''
UNSLOTH_SERVICE = r'''# @title 2. Service -- Unsloth Studio is started by its own blocking start() in cell 5;
# this cell only prepares the import path. It has built-in auth (CT_AUTH=builtin), so no Caddy.
sys.path.insert(0, "/content/unsloth/studio/backend")
print("Unsloth Studio will listen on port", SERVICE_PORT)
'''
UNSLOTH_KEEPALIVE = r'''# @title 5. Ready + keep-alive -- KEEP THIS CELL RUNNING
# start() blocks forever (its own keep-alive loop). Our tunnel from cell 4 is used, so cloudflare=False.
# A watcher thread reports the admin password + CT_READY and restarts cloudflared if it dies.
import threading

def _watch():
    ct_wait_http(f"http://127.0.0.1:{SERVICE_PORT}/api/health", timeout=900)
    auth_dir = "/root/.unsloth/studio/auth"
    for _ in range(60):
        for fn in (".bootstrap_password", ".colab_notebook_login"):
            p = os.path.join(auth_dir, fn)
            if os.path.exists(p):
                lines = open(p).read().split()
                pw = lines[-1] if lines else ""
                print(f"CT_CREDENTIALS user=unsloth pass={pw}", flush=True)
                break
        else:
            time.sleep(2); continue
        break
    print(f"{SERVICE} is up: {PUBLIC_URL}")
    print("CT_READY", flush=True)
    while True:
        time.sleep(30)
        for name, (proc, restart) in list(PROCS.items()):
            if proc.poll() is not None:
                print(f"[watchdog] {name} exited; restarting", flush=True)
                PROCS[name] = (restart(), restart)

threading.Thread(target=_watch, daemon=True).start()
from colab import start
start(port=SERVICE_PORT, cloudflare=False)
'''

# --------------------------------------------------------------------------- qwen-flash (G4, llama.cpp sm_120, 800K + vision)
QWEN_FLASH_CFG = dict(name="qwen-flash", service_port=8000, auth="builtin", public_port=8000,
                      health_path="/health", ready_timeout=2400)

QWEN_FLASH_SETUP = r'''# @title 1. Setup -- parallel llama.cpp sm_120 build/cache restore + 84 GB GGUF + BF16 mmproj staging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib, platform, tarfile

CFG = {
    "repo": os.environ.get("CT_QWEN_REPO", "SC117/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-GGUF"),
    "quant": os.environ.get("CT_QWEN_QUANT", "IQ3_S"),
    "alias": os.environ.get("CT_QWEN_ALIAS", "qwen3.8-flash-next"),
    "context": int(os.environ.get("CT_QWEN_CONTEXT", "819200")),
    "rope_scaling": os.environ.get("CT_QWEN_ROPE_SCALING", "yarn"),
    "rope_scale": float(os.environ.get("CT_QWEN_ROPE_SCALE", "3.125")),
    "yarn_orig_ctx": int(os.environ.get("CT_QWEN_YARN_ORIG_CTX", "262144")),
    "cache_type": os.environ.get("CT_QWEN_CACHE_TYPE", "f16"),
    "batch": int(os.environ.get("CT_QWEN_BATCH", "512")),
    "ubatch": int(os.environ.get("CT_QWEN_UBATCH", "128")),
    "llama_repo": os.environ.get("CT_LLAMA_REPO", "https://github.com/ggml-org/llama.cpp.git"),
    "llama_commit": os.environ.get("CT_LLAMA_COMMIT", "a0e6dc7384edb8f5d927155e1d26c10b4a310e6d"),
    "service_dir": os.environ.get("CT_QWEN_SERVICE_DIR", "/content/drive/MyDrive/colab-services/qwen-flash"),
    "model_root": os.environ.get("CT_QWEN_MODEL_ROOT", "/content/drive/MyDrive/models"),
    "work_dir": os.environ.get("CT_QWEN_WORK_DIR", "/content/qwen-flash"),
    "vision_projector": {
        "name": "mmproj-Qwen3.8-Flash-Next-BF16.gguf",
        "revision": "eec4e10a2c29b440a2ae85591fa11569ce752963",
        "size": 907542944,
        "sha256": "2e788f8c511d8093c7b43cb87b2fd7e14228340318057f8fb20c86df2efe2355",
    },
}

DEFAULT_MODEL_LOCK = {
    "repo": "SC117/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-GGUF",
    "revision": "eec4e10a2c29b440a2ae85591fa11569ce752963",
    "files": [
        {
            "name": "IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00001-of-00002.gguf",
            "size": 55063448064,
            "sha256": "40f369eca301ec6ea318dbb671b3132c26a2f0b89aa5458a7051b33215ee296c",
        },
        {
            "name": "IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00002-of-00002.gguf",
            "size": 28800138432,
            "sha256": "316b46f3a2dbd68c900f43136ab9449f9dcc3725dfd8c794847c204bc161e113",
        },
    ],
}

def qlog(msg):
    print(time.strftime("[%F %T] ") + msg, flush=True)

def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{SERVICE}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)

def cmd_out(command, **kwargs):
    return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, **kwargs).strip()

def run_logged(command, filename, env=None):
    with open(filename, "a") as handle:
        res = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, env=env)
    if res.returncode:
        raise RuntimeError(f"Command failed ({res.returncode}); see {filename}")

def digest_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()

def require_space(path, needed):
    free = shutil.disk_usage(path).free
    if free < needed:
        raise RuntimeError(f"{path}: need {needed/1e9:.1f} GB, free {free/1e9:.1f} GB")

def restore_valid_bin(bin_dir, env):
    server = bin_dir / "llama-server"
    if not server.is_file():
        return False
    try:
        server.chmod(0o755)
        cmd_out([str(server), "--version"], env=env)
        devices = cmd_out([str(server), "--list-devices"], env=env)
        return "CUDA" in devices and "RTX PRO 6000" in devices
    except (OSError, subprocess.CalledProcessError):
        return False

def prepare_runtime(config, service, work):
    commit_file = service / "llama-commit.txt"
    if not commit_file.exists():
        commit = config.get("llama_commit") or cmd_out(["git", "ls-remote", config["llama_repo"], "HEAD"]).split()[0]
        commit_file.write_text(commit + "\n")
    commit = commit_file.read_text().strip()
    gpu = cmd_out(["nvidia-smi", "--query-gpu=name,compute_cap", "--format=csv,noheader"]).splitlines()[0]
    if "RTX PRO 6000" not in gpu or "12.0" not in gpu:
        raise RuntimeError("This deployment requires G4 / RTX PRO 6000 sm_120: " + gpu)
    signature = {
        "llama_commit": commit, "gpu": gpu, "cuda": cmd_out(["nvcc", "--version"]),
        "os_release": Path("/etc/os-release").read_text(), "arch": platform.machine(),
        "glibc": platform.libc_ver(), "cuda_arch": "120", "ggml_native": False,
    }
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    cache = Path(config["model_root"]) / ".runtimes" / "llama.cpp" / ("sm120-" + key)
    cache.mkdir(parents=True, exist_ok=True)
    bin_dir = work / "runtime" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, LD_LIBRARY_PATH=str(bin_dir) + ":" + os.environ.get("LD_LIBRARY_PATH", ""))
    local_manifest = work / "runtime-manifest.json"
    if bin_dir.exists() and any(bin_dir.iterdir()) and (
        not local_manifest.exists() or json.loads(local_manifest.read_text()) != json.loads(json.dumps(signature))
    ):
        shutil.rmtree(bin_dir)
        bin_dir.mkdir()
    archive = cache / "runtime.tar"
    checksum = cache / "runtime.sha256"
    if not restore_valid_bin(bin_dir, env) and archive.exists() and checksum.exists():
        qlog("Restoring compatible build cache from Drive: " + str(cache))
        local_tar = work / "runtime.tar"
        shutil.copyfile(archive, local_tar)
        if digest_file(local_tar) != checksum.read_text().strip():
            raise RuntimeError("Build cache checksum mismatch; refusing corrupt archive")
        with tarfile.open(local_tar) as bundle:
            bundle.extractall(work / "runtime", filter="data")
        local_tar.unlink()
    if not restore_valid_bin(bin_dir, env):
        qlog("Building pinned llama.cpp for CUDA sm_120; progress: " + str(work / "build.log"))
        source = work / "llama.cpp"
        if not (source / ".git").exists():
            run_logged(["git", "clone", "--no-checkout", "--filter=blob:none", config["llama_repo"], str(source)], work / "build.log")
        run_logged(["git", "-C", str(source), "fetch", "--depth", "1", "origin", commit], work / "build.log")
        run_logged(["git", "-C", str(source), "checkout", "--detach", commit], work / "build.log")
        run_logged(["cmake", "-S", str(source), "-B", str(source / "build"),
                    "-DGGML_CUDA=ON", "-DCMAKE_CUDA_ARCHITECTURES=120",
                    "-DGGML_NATIVE=OFF", "-DCMAKE_BUILD_TYPE=Release",
                    "-DLLAMA_CURL=OFF", "-DLLAMA_OPENSSL=OFF", "-DLLAMA_BUILD_TESTS=OFF"], work / "build.log")
        run_logged(["cmake", "--build", str(source / "build"), "--config", "Release",
                    "-j", str(min(12, os.cpu_count() or 4))], work / "build.log")
        shutil.copytree(source / "build" / "bin", bin_dir, dirs_exist_ok=True, symlinks=False)
        if not restore_valid_bin(bin_dir, env):
            raise RuntimeError("Built runtime did not enumerate the G4 CUDA device")
        local_tar = work / "runtime.tar"
        with tarfile.open(local_tar, "w", dereference=True) as bundle:
            bundle.add(bin_dir, arcname="bin")
        checksum_value = digest_file(local_tar)
        partial = cache / f"runtime.tar.{SERVICE}.{os.getpid()}.partial"
        shutil.copyfile(local_tar, partial)
        partial.replace(archive)
        checksum.write_text(checksum_value + "\n")
        write_json_atomic(cache / "manifest.json", signature)
        local_tar.unlink()
        qlog("Complete runtime cached on Drive: " + str(cache))
    help_text = cmd_out([str(bin_dir / "llama-server"), "--help"], env=env)
    for flag in ("--lazy-mode", "--load-mode", "--api-key-file"):
        if flag not in help_text:
            raise RuntimeError("Pinned llama.cpp lacks required option: " + flag)
    write_json_atomic(local_manifest, signature)
    return bin_dir, env

def hf_download_client(work):
    os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")
    try:
        import hf_xet
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError:
        run_logged([sys.executable, "-m", "pip", "install", "-U", "huggingface_hub", "hf_xet"], work / "download.log")
        from huggingface_hub import HfApi, hf_hub_download
    return HfApi, hf_hub_download

def valid_shard(path, item):
    return path.is_file() and path.stat().st_size == item["size"] and digest_file(path) == item["sha256"]

def prepare_shard(item, config, revision, local_dir, download):
    target = local_dir / item["name"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if download is not None:
        try:
            qlog("Downloading HF -> local SSD (resumable): " + item["name"])
            downloaded = Path(download(repo_id=config["repo"], filename=item["name"],
                                       revision=revision, local_dir=local_dir / ".hf-download"))
            if not valid_shard(downloaded, item):
                downloaded.unlink(missing_ok=True)
                raise RuntimeError("HF shard size/SHA256 mismatch")
            downloaded.replace(target)
            qlog("SHA256 OK (HF): " + target.name)
            return target
        except Exception as error:
            qlog(f"HF download failed ({type(error).__name__}); trying Drive backup: " + item["name"])

    source = Path(config["model_root"]) / config["repo"] / item["name"]
    if not source.is_file() or source.stat().st_size != item["size"]:
        raise RuntimeError("HF download unavailable and no complete Drive backup: " + item["name"])
    qlog("Copying Drive backup -> local SSD and verifying SHA256: " + item["name"])
    partial = target.with_suffix(".gguf.drive.partial")
    h = hashlib.sha256()
    copied, last_report = 0, time.monotonic()
    with source.open("rb") as src, partial.open("wb") as dst:
        while chunk := src.read(16 * 1024 * 1024):
            dst.write(chunk)
            h.update(chunk)
            copied += len(chunk)
            if time.monotonic() - last_report >= 30:
                qlog(f"Copy {target.name}: {copied/1e9:.1f}/{item['size']/1e9:.1f} GB")
                last_report = time.monotonic()
    if copied != item["size"] or h.hexdigest() != item["sha256"]:
        partial.unlink(missing_ok=True)
        raise RuntimeError("Drive backup size/SHA256 mismatch: " + item["name"])
    partial.replace(target)
    qlog("SHA256 OK (Drive): " + target.name)
    return target

def prepare_model(config, service, work):
    download = None
    lock_path = service / ("model-" + config["quant"] + ".json")
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
    elif config["repo"] == DEFAULT_MODEL_LOCK["repo"] and config["quant"] == "IQ3_S":
        lock = DEFAULT_MODEL_LOCK
        write_json_atomic(lock_path, lock)
    else:
        HfApi, download = hf_download_client(work)
        info = HfApi().model_info(config["repo"], files_metadata=True)
        files = []
        for entry in info.siblings:
            if entry.rfilename.startswith(config["quant"] + "/") and entry.rfilename.endswith(".gguf"):
                if not entry.lfs:
                    raise RuntimeError("Expected LFS SHA256 metadata")
                files.append({"name": entry.rfilename, "size": entry.size, "sha256": entry.lfs.sha256})
        if len(files) != 2:
            raise RuntimeError(f"Expected two GGUF shards, got {len(files)}")
        lock = {"repo": config["repo"], "revision": info.sha, "files": sorted(files, key=lambda x: x["name"])}
        write_json_atomic(lock_path, lock)
    if lock["repo"] != config["repo"]:
        raise RuntimeError("Model lock belongs to a different repository")
    local_dir = work / "model"
    local_dir.mkdir(exist_ok=True)
    marker = local_dir / "verified.json"
    expected = {item["name"]: item["sha256"] for item in lock["files"]}
    if marker.exists() and json.loads(marker.read_text()) == expected and all(
        (local_dir / item["name"]).is_file() and (local_dir / item["name"]).stat().st_size == item["size"]
        for item in lock["files"]
    ):
        qlog("Reusing previously verified local model shards")
        return local_dir / lock["files"][0]["name"]
    pending = [item for item in lock["files"] if not valid_shard(local_dir / item["name"], item)]
    if pending:
        require_space(work, sum(item["size"] for item in pending) + 15 * 1024**3)
        if download is None:
            try:
                _, download = hf_download_client(work)
            except Exception as error:
                qlog(f"HF client unavailable ({type(error).__name__}); using Drive backups")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(prepare_shard, item, config, lock["revision"], local_dir, download)
                       for item in pending]
            for future in futures:
                future.result()
    write_json_atomic(marker, expected)
    return local_dir / lock["files"][0]["name"]

def prepare_projector(config, service, work):
    item = config.get("vision_projector")
    if not item:
        return None
    lock = json.loads((service / ("model-" + config["quant"] + ".json")).read_text())
    if lock["repo"] != config["repo"] or lock["revision"] != item["revision"]:
        raise RuntimeError("Vision projector must match the pinned model repository/revision")
    local_dir = work / "model"
    local_dir.mkdir(exist_ok=True)
    target = local_dir / item["name"]
    if valid_shard(target, item):
        qlog("Reusing verified local vision projector")
        return target
    require_space(work, item["size"] + 1024**3)
    try:
        _, download = hf_download_client(work)
    except Exception as error:
        qlog(f"HF client unavailable ({type(error).__name__}); using Drive projector backup")
        download = None
    target = prepare_shard(item, config, item["revision"], local_dir, download)
    backup = Path(config["model_root"]) / config["repo"] / item["name"]
    if not valid_shard(backup, item):
        backup.parent.mkdir(parents=True, exist_ok=True)
        partial = backup.with_suffix(f".gguf.{SERVICE}.{os.getpid()}.partial")
        shutil.copyfile(target, partial)
        if not valid_shard(partial, item):
            partial.unlink(missing_ok=True)
            raise RuntimeError("Vision projector backup size/SHA256 mismatch")
        partial.replace(backup)
    return target

if not Path("/content/drive/MyDrive").is_dir():
    raise RuntimeError("Drive is not mounted. Run via `./services/qwen-flash.py up`.")
SERVICE_DIR = Path(CFG["service_dir"])
SERVICE_DIR.mkdir(parents=True, exist_ok=True)
QWEN_WORK = Path(CFG["work_dir"])
QWEN_WORK.mkdir(parents=True, exist_ok=True)

qlog("Preparing model and runtime concurrently; durable state: " + str(SERVICE_DIR))
with ThreadPoolExecutor(max_workers=2) as pool:
    f_build = pool.submit(prepare_runtime, CFG, SERVICE_DIR, QWEN_WORK)
    f_model = pool.submit(prepare_model, CFG, SERVICE_DIR, QWEN_WORK)
    while not (f_build.done() and f_model.done()):
        for fut in (f_build, f_model):
            if fut.done() and fut.exception():
                raise fut.exception()
        qlog(f"Preparation heartbeat | build={'done' if f_build.done() else 'running'} "
             f"model={'done' if f_model.done() else 'running'}")
        time.sleep(30)
    BIN_DIR, LLAMA_ENV = f_build.result()
    MODEL_PATH = f_model.result()
PROJECTOR_PATH = prepare_projector(CFG, SERVICE_DIR, QWEN_WORK)
qlog(f"Setup complete: model={MODEL_PATH} projector={PROJECTOR_PATH}")
'''

QWEN_FLASH_SERVICE = r'''# @title 2. Service -- llama-server on 127.0.0.1 (--load-mode none --lazy-mode off, YaRN 800K, vision, --api-key-file)
KEY_FILE = QWEN_WORK / "api_key.txt"
KEY_FILE.write_text(API_TOKEN + "\n")
KEY_FILE.chmod(0o600)

SERVER_ARGS = [
    str(BIN_DIR / "llama-server"), "-m", str(MODEL_PATH), "--alias", CFG["alias"],
    "--host", "127.0.0.1", "--port", str(SERVICE_PORT), "-ngl", "all",
    "-c", str(CFG["context"]), "-np", "1", "-lm", "none", "--lazy-mode", "off",
    "-ctk", CFG["cache_type"], "-ctv", CFG["cache_type"], "-fa", "on",
    "-b", str(CFG["batch"]), "-ub", str(CFG["ubatch"]),
    "--jinja", "--api-key-file", str(KEY_FILE), "--no-context-shift", "-lv", "4",
]
if PROJECTOR_PATH is not None:
    SERVER_ARGS += ["--mmproj", str(PROJECTOR_PATH)]
if CFG.get("rope_scaling"):
    SERVER_ARGS += [
        "--rope-scaling", CFG["rope_scaling"],
        "--rope-scale", str(CFG["rope_scale"]),
        "--yarn-orig-ctx", str(CFG["yarn_orig_ctx"]),
    ]

def start_service():
    subprocess.run(["pkill", "-f", "llama-server"], check=False)
    qlog(f"Starting llama-server: ctx={CFG['context']}, KV={CFG['cache_type']}")
    return ct_spawn("llama-server", SERVER_ARGS, env=LLAMA_ENV)

PROCS["service"] = (start_service(), start_service)
'''

QWEN_FLASH_KEEPALIVE = r'''# @title 5. Ready + keep-alive -- verify local/public auth, 800K context, vision, smoke test, then supervise
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"

def http_json(url, key=None, payload=None, timeout=15):
    headers = {"Content-Type": "application/json", "User-Agent": BROWSER_UA}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, headers=headers,
                                 data=None if payload is None else json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, {}

def auth_check(base, key):
    for candidate, expected in ((None, 401), ("invalid-deployment-test-key", 401), (key, 200)):
        code, _ = http_json(base + "/v1/models", candidate)
        if code != expected:
            raise RuntimeError(f"Authentication test failed on {base}: expected {expected}, got {code}")

LOCAL_BASE = f"http://127.0.0.1:{SERVICE_PORT}"
deadline = time.monotonic() + 1800
last_notice = 0
while time.monotonic() < deadline:
    proc = PROCS["service"][0]
    if proc.poll() is not None:
        raise RuntimeError("llama-server exited during load:\n" + ct_tail("llama-server", 40))
    try:
        if http_json(LOCAL_BASE + "/health", API_TOKEN)[0] == 200:
            break
    except (OSError, ValueError):
        pass
    if time.monotonic() - last_notice > 60:
        qlog("Waiting for model load / warmup | " +
             cmd_out(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader"]))
        last_notice = time.monotonic()
    time.sleep(5)
else:
    raise RuntimeError("Model load timeout")

auth_check(LOCAL_BASE, API_TOKEN)
code, props = http_json(LOCAL_BASE + "/props", API_TOKEN)
allocated = props.get("default_generation_settings", {}).get("n_ctx")
if code != 200 or allocated != CFG["context"]:
    raise RuntimeError(f"Actual context differs from requested context: {allocated}")
vision_enabled = props.get("modalities", {}).get("vision", False)
if PROJECTOR_PATH is not None and not vision_enabled:
    raise RuntimeError("Vision projector configured but server does not advertise vision")
qlog(f"Local verified: auth=401/401/200, n_ctx={allocated}, vision={vision_enabled}")

# Public endpoint verification (DNS/edge propagation can take a few seconds)
pub_deadline = time.monotonic() + 120
while time.monotonic() < pub_deadline:
    try:
        code, _ = http_json(PUBLIC_URL + "/v1/models", API_TOKEN, timeout=15)
        if code == 200:
            auth_check(PUBLIC_URL, API_TOKEN)
            code, resp = http_json(PUBLIC_URL + "/v1/chat/completions", API_TOKEN, {
                "model": CFG["alias"],
                "messages": [{"role": "user", "content": "只输出四个字：部署成功"}],
                "max_tokens": 64, "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }, timeout=180)
            reply = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            qlog(f"PUBLIC VERIFIED: {PUBLIC_URL}/v1 | reply={reply[:60]}")
            ver_rec = {
                "service": SERVICE,
                "verified_at": time.strftime("%FT%T%z"), "base_url": PUBLIC_URL + "/v1",
                "context_allocated": allocated, "vision_enabled": vision_enabled, "reply": reply,
            }
            write_json_atomic(SERVICE_DIR / "last-verification.json", ver_rec)
            if SERVICE != "qwen-flash":
                write_json_atomic(SERVICE_DIR / f"last-verification-{SERVICE}.json", ver_rec)
            break
    except Exception as err:
        qlog(f"Public probe pending ({type(err).__name__}: {err})")
    time.sleep(10)

print(f"{SERVICE} is up: {PUBLIC_URL}")
print("CT_READY", flush=True)

started = time.monotonic()
failures = 0
while True:
    time.sleep(60)
    for name, (proc, restart) in list(PROCS.items()):
        if proc.poll() is not None:
            qlog(f"[watchdog] {name} exited with {proc.returncode}; restarting\n{ct_tail(name, 15)}")
            PROCS[name] = (restart(), restart)
    try:
        code, _ = http_json(LOCAL_BASE + "/health", API_TOKEN)
        failures = 0 if code == 200 else failures + 1
    except Exception:
        failures += 1
    if failures >= 6:
        qlog("[watchdog] 6 failed health checks; restarting llama-server")
        PROCS["service"][0].terminate()
        PROCS["service"] = (start_service(), start_service)
        failures = 0
    gpu_mem = cmd_out(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader"])
    status_rec = {
        "service": SERVICE,
        "kernel_pid": os.getpid(), "server_pid": PROCS["service"][0].pid,
        "tunnel_pid": PROCS["cloudflared"][0].pid, "context": CFG["context"],
        "alias": CFG["alias"], "url": PUBLIC_URL + "/v1",
        "updated_at": time.strftime("%FT%T%z"), "uptime_sec": int(time.monotonic() - started),
        "gpu_memory": gpu_mem,
    }
    try:
        write_json_atomic(SERVICE_DIR / "last-status.json", status_rec)
        if SERVICE != "qwen-flash":
            write_json_atomic(SERVICE_DIR / f"last-status-{SERVICE}.json", status_rec)
    except OSError:
        pass
    qlog("heartbeat | " + gpu_mem)
'''

# --------------------------------------------------------------------------- comfyui (Caddy auth + ComfyUI-Manager)
COMFYUI_CFG = dict(name="comfyui", service_port=8188, auth="caddy", public_port=18080,
                   health_path="/system_stats", ready_timeout=2400)
COMFYUI_SETUP = r'''# @title 1. Setup -- install ComfyUI + ComfyUI-Manager (models on fast local SSD, outputs/workflows on Drive)
from pathlib import Path
from urllib.parse import unquote

COMFY_DIR = "/content/ComfyUI"
COMFY_REPO = os.environ.get("CT_COMFY_REPO", "https://github.com/comfyanonymous/ComfyUI.git")
MANAGER_REPO = os.environ.get("CT_COMFY_MANAGER_REPO", "https://github.com/Comfy-Org/ComfyUI-Manager.git")
UI_MODE = os.environ.get("CT_COMFY_MANAGER_UI", "both")   # v4 | legacy | both (custom_nodes)

if not os.path.isdir(f"{COMFY_DIR}/.git"):
    subprocess.run(["git", "clone", "--depth", "1", COMFY_REPO, COMFY_DIR], check=True)
else:
    subprocess.run(["git", "-C", COMFY_DIR, "pull", "--ff-only"], check=False)

# Remove any legacy models stored on Google Drive so Drive is never bloated by multi-GB weights
if os.path.isdir(f"{DATA_DIR}/models") and not os.path.islink(f"{DATA_DIR}/models"):
    shutil.rmtree(f"{DATA_DIR}/models", ignore_errors=True)
if os.path.islink(f"{COMFY_DIR}/models"):
    os.unlink(f"{COMFY_DIR}/models")
os.makedirs(f"{COMFY_DIR}/models", exist_ok=True)

# Persist only lightweight user data (custom_nodes, output, input, user) on Google Drive
for sub in ("custom_nodes", "output", "input", "user"):
    drive_sub = f"{DATA_DIR}/{sub}"
    local_sub = f"{COMFY_DIR}/{sub}"
    os.makedirs(drive_sub, exist_ok=True)
    if os.path.islink(local_sub):
        if os.readlink(local_sub) != drive_sub:
            os.unlink(local_sub)
            os.symlink(drive_sub, local_sub)
    elif os.path.isdir(local_sub):
        subprocess.run(["cp", "-rn", f"{local_sub}/.", f"{drive_sub}/"], check=True)
        shutil.rmtree(local_sub)
        os.symlink(drive_sub, local_sub)
    else:
        os.symlink(drive_sub, local_sub)

# Install ComfyUI core requirements + ComfyUI-Manager + hf_xet for fast local SSD model downloads
req_marker = f"{WORK}/comfyui_deps.ok"
if not os.path.exists(req_marker):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "huggingface_hub", "hf_xet"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{COMFY_DIR}/requirements.txt"], check=True)
    if os.path.exists(f"{COMFY_DIR}/manager_requirements.txt"):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{COMFY_DIR}/manager_requirements.txt"],
                       check=True)
    open(req_marker, "w").write("ok\n")

# Patch ComfyUI frontend so <script type="module">, <link rel="modulepreload">, and Vite's preload helper
# use crossorigin="use-credentials", and strip embedded URL credentials (https://user:pass@host) before
# Vue Router calls history.replaceState() (which throws SecurityError when user:pass@ is in location.href)
import glob, site
_URL_CRED_GUARD = ("<script>;(function(){if(location.username||location.password){"
                   "location.replace(location.origin+location.pathname+location.search+location.hash);"
                   "}})()</script>")
_SSD_UI_SCRIPT = '<script defer src="/api/ct/ssd-downloader.js"></script>'
for sp in site.getsitepackages():
    fe_dir = f"{sp}/comfyui_frontend_package/static"
    if not os.path.isdir(fe_dir):
        continue
    idx = f"{fe_dir}/index.html"
    if os.path.isfile(idx):
        html = open(idx, encoding="utf-8").read()
        patched = re.sub(r'\bcrossorigin(?:="")?(?!="use-credentials")(?=[\s>/])',
                         'crossorigin="use-credentials"', html)
        if _URL_CRED_GUARD not in patched:
            patched = patched.replace("<head>", "<head>" + _URL_CRED_GUARD, 1)
        if _SSD_UI_SCRIPT not in patched:
            patched = patched.replace("</body>", _SSD_UI_SCRIPT + "</body>", 1)
        if patched != html:
            open(idx, "w", encoding="utf-8").write(patched)
            print("Patched crossorigin, URL credential guard, and SSD UI button in", idx)
    for js_path in glob.glob(f"{fe_dir}/assets/*.js"):
        js = open(js_path, encoding="utf-8", errors="ignore").read()
        patched_js = js.replace('crossOrigin=``', 'crossOrigin=`use-credentials`').replace(
            'crossOrigin=""', 'crossOrigin="use-credentials"')
        if patched_js != js:
            open(js_path, "w", encoding="utf-8").write(patched_js)
            print("Patched crossOrigin in", js_path)

if UI_MODE in ("legacy", "both"):
    mgr_dir = f"{COMFY_DIR}/custom_nodes/ComfyUI-Manager"
    if not os.path.isdir(mgr_dir) or not os.listdir(mgr_dir):
        subprocess.run(["git", "clone", "--depth", "1", MANAGER_REPO, mgr_dir], check=True)
    if os.path.exists(f"{mgr_dir}/requirements.txt"):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{mgr_dir}/requirements.txt"], check=True)

# Built-in custom nodes always provisioned before ComfyUI starts
BUILTIN_CUSTOM_NODES = [
    ("ComfyUI-MiniMaxH3-PrefixStream", "https://github.com/knoic/ComfyUI-MiniMaxH3-PrefixStream.git"),
    ("ComfyUI-VideoHelperSuite", "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git"),
]
for node_name, node_repo in BUILTIN_CUSTOM_NODES:
    # Manager may install a registry package using a lowercase directory name.
    # Loading both copies registers the same frontend twice and breaks VHSHelp.
    node_root = Path(COMFY_DIR) / "custom_nodes"
    for duplicate in sorted(node_root.iterdir()):
        if duplicate.name != node_name and duplicate.name.casefold() == node_name.casefold():
            backup_root = Path(DATA_DIR) / "custom_nodes-disabled"
            backup_root.mkdir(parents=True, exist_ok=True)
            import uuid
            backup = backup_root / f"{duplicate.name}-{uuid.uuid4().hex}"
            shutil.move(str(duplicate), str(backup))
            print(f"Archived duplicate custom node: {duplicate} -> {backup}")
    node_dir = f"{COMFY_DIR}/custom_nodes/{node_name}"
    if not os.path.isdir(f"{node_dir}/.git"):
        if os.path.isdir(node_dir):
            shutil.rmtree(node_dir)
        subprocess.run(["git", "clone", "--depth", "1", node_repo, node_dir], check=True)
    else:
        subprocess.run(["git", "-C", node_dir, "pull", "--ff-only"], check=False)
    if os.path.isfile(f"{node_dir}/requirements.txt"):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{node_dir}/requirements.txt"], check=True)

# Install requirements of any other persisted custom_nodes on Drive
for entry in sorted(os.listdir(f"{COMFY_DIR}/custom_nodes")):
    req = f"{COMFY_DIR}/custom_nodes/{entry}/requirements.txt"
    if entry not in ("ComfyUI-Manager", *(n for n, _ in BUILTIN_CUSTOM_NODES)) and os.path.isfile(req):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req], check=False)

# --- On-demand workflow model downloader (downloads directly to local NVMe SSD /content/ComfyUI/models) ---
open("/content/ComfyUI/ct_workflow_helper.py", "w", encoding="utf-8").write(r"""import glob, json, os, re, shutil, site, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import unquote

COMFY_DIR = Path("/content/ComfyUI")
HF_URL_RE = re.compile(r"^https?://huggingface\.co/([^/]+/[^/]+)/(?:resolve|blob)/([^/]+)/(.+?)(?:\?.*)?$")
MD_LINK_RE = re.compile(r"\[([^\]]+\.(?:safetensors|ckpt|pt|pth|bin|gguf|sft))\]\((https?://[^)\s]+)\)", re.I)
MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft")
NODE_TYPE_DIR = {
    "CheckpointLoaderSimple": "checkpoints", "CheckpointLoader": "checkpoints", "unCLIPCheckpointLoader": "checkpoints",
    "ImageOnlyCheckpointLoader": "checkpoints", "UNETLoader": "diffusion_models",
    "DiffusionModelLoader": "diffusion_models", "CLIPLoader": "text_encoders", "DualCLIPLoader": "text_encoders",
    "TripleCLIPLoader": "text_encoders", "QuadrupleCLIPLoader": "text_encoders", "VAELoader": "vae",
    "LoraLoader": "loras", "LoraLoaderModelOnly": "loras", "ControlNetLoader": "controlnet",
    "DiffControlNetLoader": "controlnet", "CLIPVisionLoader": "clip_vision", "StyleModelLoader": "style_models",
    "UpscaleModelLoader": "upscale_models", "HypernetworkLoader": "hypernetworks", "GLIGENLoader": "gligen",
    "ModelPatchLoader": "model_patches", "AudioEncoderLoader": "audio_encoders",
}
TYPE_SAVE_DIR = {
    "checkpoint": "checkpoints", "unclip": "checkpoints", "diffusion_model": "diffusion_models",
    "clip": "text_encoders", "vae": "vae", "VAE": "vae", "lora": "loras", "controlnet": "controlnet",
    "T2I-Adapter": "controlnet", "clip_vision": "clip_vision", "upscale": "upscale_models",
    "embedding": "embeddings", "gligen": "gligen", "photomaker": "photomaker", "model_patch": "model_patches",
}
_MANAGER_INDEX = None

def _cleanup_dl_cache():
    for p in ("/content/hf-stage", "/root/.cache/huggingface/xet", "/root/.cache/huggingface/hub"):
        shutil.rmtree(p, ignore_errors=True)

def _get_manager_index():
    global _MANAGER_INDEX
    if _MANAGER_INDEX is not None:
        return _MANAGER_INDEX
    _MANAGER_INDEX = {}
    candidates = [
        COMFY_DIR / "custom_nodes" / "ComfyUI-Manager" / "model-list.json",
        *[Path(sp) / "comfyui_manager" / "model-list.json" for sp in site.getsitepackages()],
    ]
    data = None
    for p in candidates:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                break
            except Exception:
                pass
    if data is None:
        try:
            req = urllib.request.Request(
                "https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager/main/model-list.json",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
        except Exception:
            data = {}
    for m in data.get("models", []):
        fn = m.get("filename")
        url = m.get("url")
        if not fn or not url:
            continue
        sp = m.get("save_path") or ""
        if not sp or sp == "default":
            sp = TYPE_SAVE_DIR.get(m.get("type", ""), "checkpoints")
        _MANAGER_INDEX[fn] = {"directory": sp.strip("/"), "name": fn, "url": url}
    return _MANAGER_INDEX

def resolve_workflow(wf_source, data_dir=None):
    spec = wf_source.strip()
    m = re.match(r"^https?://github\.com/([^/]+/[^/]+)/blob/(.+)$", spec)
    if m:
        spec = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}"
    if spec.startswith(("http://", "https://")):
        req = urllib.request.Request(spec, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        return Path(spec.split("?")[0]).name, raw, spec
    if os.path.isfile(spec):
        p = Path(spec)
        return p.name, p.read_text(encoding="utf-8"), str(p)

    # Bare filename / template name:
    # 1) Try official Comfy-Org/workflow_templates online
    name = spec if spec.endswith(".json") else f"{spec}.json"
    gh_url = f"https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/{name}"
    try:
        req = urllib.request.Request(gh_url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8")
        return Path(name).name, raw, gh_url
    except Exception as net_err:
        print(f"[workflow-dl] Online lookup ({gh_url}) failed ({net_err}); searching Drive & local workflow dirs...",
              flush=True)

    # 2) Fallback: search Google Drive workflow directories & local installed templates
    d_root = Path(data_dir) if data_dir else Path(os.environ.get("CT_DATA_DIR", "/content/drive/MyDrive/colab-llm/comfyui"))
    search_dirs = [
        COMFY_DIR / "user" / "default" / "workflows",
        d_root / "user" / "default" / "workflows",
        d_root / "workflows",
        COMFY_DIR / "user",
        d_root / "user",
    ]
    for sp in site.getsitepackages():
        for pkg_dir in glob.glob(f"{sp}/comfyui_workflow_templates*/templates"):
            search_dirs.append(Path(pkg_dir))
    for sdir in search_dirs:
        if not sdir.is_dir():
            continue
        direct = sdir / name
        if direct.is_file():
            return direct.name, direct.read_text(encoding="utf-8"), str(direct)
        for hit in sdir.rglob(Path(name).name):
            if hit.is_file():
                return hit.name, hit.read_text(encoding="utf-8"), str(hit)
    raise FileNotFoundError(f"Workflow '{wf_source}' not found online ({gh_url}) or in Drive workflow directories")

def extract_workflow_models(wf_json):
    found = {}
    md_urls = {}
    missing_widgets = []

    def _walk(obj):
        if isinstance(obj, dict):
            ntype = obj.get("type", "")
            props = obj.get("properties")
            if isinstance(props, dict) and isinstance(props.get("models"), list):
                for m in props["models"]:
                    if isinstance(m, dict) and m.get("url") and m.get("directory") and m.get("name"):
                        d, n = m["directory"].strip("/"), m["name"].lstrip("/")
                        found[f"{d}/{n}"] = {"directory": d, "name": n, "url": m["url"].strip(), "source": "properties.models"}
            wvals = obj.get("widgets_values")
            if isinstance(wvals, list):
                for v in wvals:
                    if isinstance(v, str):
                        for fn, link in MD_LINK_RE.findall(v):
                            clean_link = link.replace("/blob/", "/resolve/")
                            md_urls[Path(fn).name] = clean_link
                        if v.lower().endswith(MODEL_EXTS) and "\n" not in v and " " not in v and len(v) < 200:
                            missing_widgets.append((ntype, v.strip()))
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(wf_json)
    by_name = {Path(m["name"]).name: m for m in found.values()}

    for ntype, raw_val in missing_widgets:
        fname = Path(raw_val).name
        if fname in by_name:
            continue
        # Tier 2: match filename against MarkdownNote links in the same workflow
        if fname in md_urls:
            url = md_urls[fname]
            d = NODE_TYPE_DIR.get(ntype)
            if not d:
                hm = HF_URL_RE.match(url)
                if hm:
                    parts = unquote(hm.group(3)).split("/")
                    if len(parts) > 1:
                        d = parts[0]
            d = d or "diffusion_models"
            item = {"directory": d, "name": raw_val, "url": url, "source": "markdown_link"}
            found[f"{d}/{raw_val}"] = item
            by_name[fname] = item
            continue
        # Tier 3: match filename against ComfyUI-Manager model-list.json
        mgr_idx = _get_manager_index()
        if fname in mgr_idx:
            info = mgr_idx[fname]
            d = NODE_TYPE_DIR.get(ntype) or info["directory"]
            item = {"directory": d, "name": raw_val, "url": info["url"], "source": "model-list.json"}
            found[f"{d}/{raw_val}"] = item
            by_name[fname] = item

    return found

def _stream_http_download(url, dest, emit):
    headers = {"User-Agent": "Mozilla/5.0"}
    hf_tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if hf_tok and "huggingface.co" in url:
        headers["Authorization"] = f"Bearer {hf_tok}"
    req = urllib.request.Request(url, headers=headers)
    partial = dest.with_name(dest.name + ".partial")
    partial.unlink(missing_ok=True)
    with urllib.request.urlopen(req, timeout=120) as resp, open(partial, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        copied = 0
        t_start = last_log = time.monotonic()
        while True:
            chunk = resp.read(16 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            copied += len(chunk)
            now = time.monotonic()
            if now - last_log >= 10:
                speed = copied / 1e6 / max(now - t_start, 0.1)
                if total > 0:
                    emit(f"[workflow-dl]   ↳ {copied/1e9:.2f} / {total/1e9:.2f} GB ({copied*100/total:.1f}%) @ {speed:.1f} MB/s")
                else:
                    emit(f"[workflow-dl]   ↳ {copied/1e9:.2f} GB @ {speed:.1f} MB/s")
                last_log = now
    partial.replace(dest)

def ensure_model_on_ssd(item, log_cb=None):
    def _emit(msg):
        print(msg, flush=True)
        if log_cb:
            log_cb(msg)
    dest = COMFY_DIR / "models" / item["directory"] / item["name"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        _emit(f"[workflow-dl] SKIP (already on local SSD, {dest.stat().st_size/1e9:.2f} GB): {dest.relative_to(COMFY_DIR)}")
        return {"model": f"{item['directory']}/{item['name']}", "status": "cached",
                "size": dest.stat().st_size, "source": item.get("source", "")}
    _cleanup_dl_cache()
    free_gb = shutil.disk_usage("/content").free / 1e9
    url = item["url"].replace("/blob/", "/resolve/")
    m = HF_URL_RE.match(url)
    t0 = time.monotonic()
    try:
        # Use isolated subprocess for hf_hub_download when plenty of SSD headroom (>45 GB) exists;
        # otherwise (or if subprocess fails), stream directly to dest.partial with zero staging duplication.
        if m and free_gb > 45.0:
            repo_id, revision, filename = m.group(1), m.group(2), unquote(m.group(3))
            _emit(f"[workflow-dl] HF Xet -> local SSD (free: {free_gb:.1f} GB): {repo_id}/{filename} -> models/{item['directory']}/{item['name']}")
            dl_env = {**os.environ, "HF_XET_CHUNK_CACHE_SIZE_BYTES": "0"}
            dl_env.pop("HF_XET_HIGH_PERFORMANCE", None)
            code = (
                "import shutil, sys; from pathlib import Path; from huggingface_hub import hf_hub_download; "
                "stage = Path('/content/hf-stage'); stage.mkdir(parents=True, exist_ok=True); "
                "p = Path(hf_hub_download(repo_id=sys.argv[1], filename=sys.argv[2], revision=sys.argv[3], local_dir=stage)); "
                "shutil.move(str(p), sys.argv[4])"
            )
            proc = subprocess.run([sys.executable, "-c", code, repo_id, filename, revision, str(dest)], env=dl_env)
            if proc.returncode != 0 or not (dest.is_file() and dest.stat().st_size > 0):
                _cleanup_dl_cache()
                _emit(f"[workflow-dl] HF Xet subprocess exited with {proc.returncode}; falling back to direct HTTP stream...")
                _stream_http_download(url, dest, _emit)
        else:
            _emit(f"[workflow-dl] Direct HTTP stream -> local SSD (free: {free_gb:.1f} GB): {url} -> models/{item['directory']}/{item['name']}")
            _stream_http_download(url, dest, _emit)
    finally:
        _cleanup_dl_cache()
    dt = time.monotonic() - t0
    sz = dest.stat().st_size
    _emit(f"[workflow-dl] DONE ({sz/1e9:.2f} GB in {dt:.1f}s, {sz/1e6/max(dt,1):.1f} MB/s): {dest.name}")
    return {"model": f"{item['directory']}/{item['name']}", "status": "downloaded",
            "size": sz, "source": item.get("source", "")}

def list_ssd_models():
    models_root = COMFY_DIR / "models"
    items = []
    if models_root.is_dir():
        for p in sorted(models_root.rglob("*")):
            if p.is_file() and p.name.lower().endswith(MODEL_EXTS):
                rel = p.relative_to(models_root)
                items.append({"path": str(rel), "size_gb": round(p.stat().st_size / 1e9, 2)})
    free_ssd_gb = round(shutil.disk_usage("/content").free / 1e9, 2)
    return {"free_ssd_gb": free_ssd_gb, "models": items}

def sync_workflow_models(wf_source="", data_dir=None, job=None, wf_json_obj=None, wf_label=None):
    def _emit(msg):
        if job is not None:
            job["logs"].append(msg)
    if wf_json_obj is not None:
        wf_name = wf_label or "current_canvas.json"
        resolved_from = "browser_canvas"
        wf_json = wf_json_obj
        msg = f"[workflow-dl] Using active browser canvas workflow ({wf_name})"
        print(msg, flush=True); _emit(msg)
    else:
        wf_name, raw, resolved_from = resolve_workflow(wf_source, data_dir=data_dir)
        msg = f"[workflow-dl] Resolved workflow '{wf_source}' -> {resolved_from}"
        print(msg, flush=True); _emit(msg)
        wf_json = json.loads(raw)
        wf_dir = COMFY_DIR / "user" / "default" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        if wf_name.endswith(".json"):
            target_wf = wf_dir / wf_name
            if not target_wf.is_file() or target_wf.read_text(encoding="utf-8", errors="ignore") != raw:
                target_wf.write_text(raw, encoding="utf-8")
    models = list(extract_workflow_models(wf_json).values())
    msg = (f"[workflow-dl] Found {len(models)} required model(s) in {wf_name}: " +
           (", ".join(f"{m['directory']}/{m['name']} ({m.get('source')})" for m in models) if models else "none"))
    print(msg, flush=True); _emit(msg)
    if job is not None:
        job["workflow"] = wf_name
        job["resolved_from"] = resolved_from
        job["total"] = len(models)
    results = []
    for idx, m in enumerate(models, 1):
        step_msg = f"[workflow-dl] [{idx}/{len(models)}] Ensuring {m['directory']}/{m['name']} ..."
        print(step_msg, flush=True); _emit(step_msg)
        if job is not None:
            job["current"] = f"[{idx}/{len(models)}] {m['directory']}/{m['name']}"
        res = ensure_model_on_ssd(m, log_cb=_emit)
        results.append(res)
        if job is not None:
            job["completed"] = idx
            job["models"] = list(results)
    if job is not None:
        job["current"] = None
    free_ssd_gb = round(shutil.disk_usage("/content").free / 1e9, 2)
    return {"workflow": wf_name, "resolved_from": resolved_from, "free_ssd_gb": free_ssd_gb, "models": results}
""")

sys.path.insert(0, "/content/ComfyUI")
from ct_workflow_helper import sync_workflow_models

for wf_spec in [x.strip() for x in os.environ.get("CT_WORKFLOW", "").split(",") if x.strip()]:
    sync_workflow_models(wf_spec, data_dir=DATA_DIR)

print("ComfyUI + ComfyUI-Manager setup complete; local SSD models =", f"{COMFY_DIR}/models", "drive data =", DATA_DIR)
'''
COMFYUI_SERVICE = r'''# @title 2. Service -- ComfyUI + ComfyUI-Manager on 127.0.0.1:8188 (with async /api/ct/pull-workflow + UI toolbar button)
open("/content/ComfyUI/ct_ssd_downloader.js", "w", encoding="utf-8").write(r"""(function() {
  if (window.__ctSsdDownloaderLoaded) return;
  window.__ctSsdDownloaderLoaded = true;

  const MODEL_DIRS = [
    ["diffusion_models", "diffusion_models (UNET / 扩散模型)"],
    ["loras", "loras (LoRA 模型)"],
    ["vae", "vae (VAE 模型)"],
    ["text_encoders", "text_encoders (CLIP / T5 / Qwen 文本编码器)"],
    ["checkpoints", "checkpoints (Checkpoints 大模型)"],
    ["clip_vision", "clip_vision (CLIP Vision 视觉编码器)"],
    ["controlnet", "controlnet (ControlNet 模型)"],
    ["upscale_models", "upscale_models (放大模型)"],
    ["audio_encoders", "audio_encoders (音频编码器)"],
    ["model_patches", "model_patches (Model Patches)"],
    ["embeddings", "embeddings (Embeddings)"],
    ["__custom__", "✏️ 自定义目录..."]
  ];

  const css = `
    #ct-ssd-btn {
      display: inline-flex; align-items: center; gap: 6px;
      background: linear-gradient(135deg, #2563eb 0%, #0d9488 100%);
      color: #fff; border: 1px solid rgba(255,255,255,0.2);
      border-radius: 6px; padding: 5px 11px; font-size: 12px; font-weight: 600;
      cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.3);
      margin: 0 6px; white-space: nowrap; z-index: 9999;
    }
    #ct-ssd-btn:hover { filter: brightness(1.12); }
    #ct-ssd-btn.ct-floating { position: fixed; top: 8px; right: 240px; }
    #ct-ssd-modal-backdrop {
      position: fixed; inset: 0; background: rgba(0,0,0,0.65);
      display: none; align-items: center; justify-content: center; z-index: 100000;
      font-family: system-ui, -apple-system, sans-serif;
    }
    #ct-ssd-modal {
      background: #18181b; color: #f4f4f5; width: 720px; max-width: 95vw;
      max-height: 90vh; border-radius: 10px; border: 1px solid #3f3f46;
      box-shadow: 0 20px 50px rgba(0,0,0,0.6); display: flex; flex-direction: column;
      overflow: hidden;
    }
    .ct-ssd-hdr {
      display: flex; align-items: center; justify-content: space-between;
      padding: 13px 18px; background: #27272a; border-bottom: 1px solid #3f3f46;
    }
    .ct-ssd-hdr h3 { margin: 0; font-size: 15px; display: flex; align-items: center; gap: 8px; }
    .ct-ssd-badge {
      background: #065f46; color: #a7f3d0; padding: 2px 8px; border-radius: 999px;
      font-size: 11px; font-weight: 600;
    }
    .ct-ssd-body { padding: 14px 18px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }
    .ct-ssd-section {
      background: #202024; border: 1px solid #323238; border-radius: 8px;
      padding: 10px 12px; display: flex; flex-direction: column; gap: 8px;
    }
    .ct-ssd-sec-title { font-size: 12px; font-weight: 600; color: #a1a1aa; }
    .ct-ssd-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .ct-ssd-input, .ct-ssd-select {
      background: #09090b; color: #f4f4f5; border: 1px solid #3f3f46;
      border-radius: 6px; padding: 7px 10px; font-size: 12.5px;
    }
    .ct-ssd-input { flex: 1; min-width: 180px; }
    .ct-ssd-action {
      background: #2563eb; color: #fff; border: none; border-radius: 6px;
      padding: 7px 13px; font-size: 12.5px; font-weight: 600; cursor: pointer;
      white-space: nowrap;
    }
    .ct-ssd-action.canvas-btn {
      background: linear-gradient(135deg, #059669 0%, #0284c7 100%);
      width: 100%; padding: 9px 14px; font-size: 13px;
    }
    .ct-ssd-action.model-btn { background: #0d9488; }
    .ct-ssd-action:disabled { opacity: 0.5; cursor: not-allowed; }
    .ct-ssd-progress-wrap {
      background: #27272a; border-radius: 6px; height: 8px; overflow: hidden; display: none;
    }
    .ct-ssd-progress-bar {
      background: linear-gradient(90deg, #10b981, #3b82f6); height: 100%; width: 0%;
      transition: width 0.3s ease;
    }
    #ct-ssd-logs {
      background: #09090b; color: #d4d4d8; border: 1px solid #27272a; border-radius: 6px;
      padding: 9px 10px; font-family: ui-monospace, monospace; font-size: 11.5px;
      height: 145px; overflow-y: auto; white-space: pre-wrap; line-height: 1.45;
    }
    .ct-ssd-models {
      background: #121215; border: 1px solid #27272a; border-radius: 6px;
      padding: 7px 10px; max-height: 115px; overflow-y: auto; font-size: 11.5px;
    }
    .ct-ssd-model-item {
      display: flex; justify-content: space-between; padding: 3px 0;
      border-bottom: 1px solid rgba(255,255,255,0.05); color: #a1a1aa;
    }
    .ct-ssd-close {
      background: transparent; color: #a1a1aa; border: none; font-size: 18px; cursor: pointer;
    }
  `;
  const styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  const dirOptionsHtml = MODEL_DIRS.map(([v, label]) => `<option value="${v}">${label}</option>`).join("");

  const backdrop = document.createElement("div");
  backdrop.id = "ct-ssd-modal-backdrop";
  backdrop.innerHTML = `
    <div id="ct-ssd-modal">
      <div class="ct-ssd-hdr">
        <h3>⚡ Colab 本地 SSD 模型按需下载 <span id="ct-ssd-free" class="ct-ssd-badge">SSD 检测中...</span></h3>
        <button class="ct-ssd-close" id="ct-ssd-close-btn">✕</button>
      </div>
      <div class="ct-ssd-body">
        <div class="ct-ssd-section">
          <div class="ct-ssd-sec-title">1️⃣ 按工作流批量下载所需模型（自动识别工作流内全部模型并去重）</div>
          <button class="ct-ssd-action canvas-btn" id="ct-ssd-canvas-btn">
            ⚡ 一键解析并下载【当前画布工作流】所需模型到本地 SSD
          </button>
          <div class="ct-ssd-row">
            <input class="ct-ssd-input" id="ct-ssd-spec-input"
              placeholder="或输入工作流 GitHub URL / 官方模板名 / 网盘文件名 (如 video_minimax_h3_r2v.json)" />
            <button class="ct-ssd-action" id="ct-ssd-url-btn">📥 下载工作流模型</button>
          </div>
        </div>

        <div class="ct-ssd-section">
          <div class="ct-ssd-sec-title">2️⃣ 指定单个模型 URL 下载到对应目录 (/content/ComfyUI/models/&lt;目录&gt;)</div>
          <div class="ct-ssd-row">
            <input class="ct-ssd-input" id="ct-ssd-model-url"
              placeholder="粘贴模型下载 URL (HuggingFace blob/resolve 链接或直链，粘贴后自动识别目录和文件名)" />
          </div>
          <div class="ct-ssd-row">
            <span style="font-size:12px;color:#a1a1aa;">目标目录:</span>
            <select class="ct-ssd-select" id="ct-ssd-dir-select">${dirOptionsHtml}</select>
            <input class="ct-ssd-input" id="ct-ssd-custom-dir" placeholder="输入自定义子目录名 (如 xlabs/ipadapters)" style="display:none;max-width:190px;" />
            <input class="ct-ssd-input" id="ct-ssd-model-name" placeholder="保存文件名 (留空则从 URL 自动提取)" style="max-width:220px;" />
            <button class="ct-ssd-action model-btn" id="ct-ssd-model-btn">🎯 下载到该目录</button>
          </div>
        </div>

        <div class="ct-ssd-progress-wrap" id="ct-ssd-prog-wrap">
          <div class="ct-ssd-progress-bar" id="ct-ssd-prog-bar"></div>
        </div>
        <div id="ct-ssd-logs">等待指令...（已存在于本地 SSD 的模型会自动跳过，下载完成后自动刷新节点列表）</div>
        <div>
          <div style="font-size:12px;color:#a1a1aa;margin-bottom:4px;font-weight:600;">
            📦 当前本地 SSD 已缓存模型 (/content/ComfyUI/models)：
          </div>
          <div class="ct-ssd-models" id="ct-ssd-model-list">加载中...</div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(backdrop);

  const btn = document.createElement("button");
  btn.id = "ct-ssd-btn";
  btn.innerHTML = "⬇️ SSD 模型下载";
  btn.title = "按工作流或模型 URL 下载到 Colab 本地高速 SSD (已存在的模型自动跳过)";

  function mountButton() {
    const rightMenu = document.querySelector(".comfyui-menu-right") ||
                      document.querySelector(".actionbar-container") ||
                      document.querySelector(".comfyui-menu");
    if (rightMenu && btn.parentElement !== rightMenu) {
      btn.classList.remove("ct-floating");
      rightMenu.prepend(btn);
    } else if (!btn.parentElement) {
      btn.classList.add("ct-floating");
      document.body.appendChild(btn);
    }
  }
  mountButton();
  setInterval(mountButton, 2000);

  const logBox = document.getElementById("ct-ssd-logs");
  const progWrap = document.getElementById("ct-ssd-prog-wrap");
  const progBar = document.getElementById("ct-ssd-prog-bar");
  const freeBadge = document.getElementById("ct-ssd-free");
  const modelListEl = document.getElementById("ct-ssd-model-list");
  const canvasBtn = document.getElementById("ct-ssd-canvas-btn");
  const urlBtn = document.getElementById("ct-ssd-url-btn");
  const specInput = document.getElementById("ct-ssd-spec-input");
  const modelUrlInput = document.getElementById("ct-ssd-model-url");
  const dirSelect = document.getElementById("ct-ssd-dir-select");
  const customDirInput = document.getElementById("ct-ssd-custom-dir");
  const modelNameInput = document.getElementById("ct-ssd-model-name");
  const modelBtn = document.getElementById("ct-ssd-model-btn");
  let pollTimer = null;

  dirSelect.onchange = () => {
    customDirInput.style.display = dirSelect.value === "__custom__" ? "inline-block" : "none";
  };

  // Auto-detect target directory and filename when user pastes/inputs a model URL
  modelUrlInput.oninput = () => {
    const raw = modelUrlInput.value.trim();
    if (!raw) return;
    try {
      const clean = decodeURIComponent(raw.split("?")[0]);
      const parts = clean.split("/").filter(Boolean);
      const last = parts[parts.length - 1] || "";
      if (last && /\.(safetensors|ckpt|pt|pth|bin|gguf|sft)$/i.test(last)) {
        modelNameInput.value = last;
      }
      const knownDirs = MODEL_DIRS.map(d => d[0]).filter(d => d !== "__custom__");
      for (const seg of parts) {
        if (knownDirs.includes(seg)) {
          dirSelect.value = seg;
          customDirInput.style.display = "none";
          break;
        }
      }
    } catch (e) {}
  };

  async function refreshStatus() {
    try {
      const r = await fetch("/api/ct/ssd-status", { credentials: "include" });
      const d = await r.json();
      freeBadge.textContent = `SSD 剩余: ${d.free_ssd_gb} GB`;
      if (!d.models || d.models.length === 0) {
        modelListEl.innerHTML = '<div style="color:#71717a;">暂无已下载模型</div>';
      } else {
        modelListEl.innerHTML = d.models.map(m =>
          `<div class="ct-ssd-model-item"><span>${m.path}</span><span>${m.size_gb} GB</span></div>`
        ).join("");
      }
      if (d.active_job && !pollTimer) {
        watchJob(d.active_job.job_id);
      }
    } catch (e) {}
  }

  function setBusy(busy) {
    canvasBtn.disabled = busy;
    urlBtn.disabled = busy;
    modelBtn.disabled = busy;
    btn.innerHTML = busy ? "⏳ SSD 下载中..." : "⬇️ SSD 模型下载";
  }

  function watchJob(jobId) {
    if (pollTimer) clearInterval(pollTimer);
    setBusy(true);
    progWrap.style.display = "block";
    pollTimer = setInterval(async () => {
      try {
        const r = await fetch(`/api/ct/pull-workflow/${jobId}`, { credentials: "include" });
        const job = await r.json();
        if (job.logs && job.logs.length) {
          logBox.textContent = job.logs.join("\n");
          logBox.scrollTop = logBox.scrollHeight;
        }
        const pct = job.total > 0 ? Math.round((job.completed / job.total) * 100) : 15;
        progBar.style.width = `${pct}%`;
        if (job.status === "done" || job.status === "error") {
          clearInterval(pollTimer);
          pollTimer = null;
          setBusy(false);
          progBar.style.width = job.status === "done" ? "100%" : "0%";
          await refreshStatus();
          if (job.status === "done") {
            logBox.textContent += "\n✅ 全部模型已就绪！正在自动刷新 ComfyUI 节点模型列表...";
            logBox.scrollTop = logBox.scrollHeight;
            try { await window.app?.refreshComboInNodes?.(); } catch (e) {}
          }
        }
      } catch (e) {}
    }, 1500);
  }

  async function startJob(payload) {
    setBusy(true);
    logBox.textContent = "正在提交下载任务...";
    progWrap.style.display = "block";
    progBar.style.width = "10%";
    try {
      const r = await fetch("/api/ct/pull-workflow", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok) {
        logBox.textContent = `❌ 错误: ${data.error || r.statusText}`;
        setBusy(false);
        return;
      }
      watchJob(data.job_id);
    } catch (e) {
      logBox.textContent = `❌ 请求失败: ${e}`;
      setBusy(false);
    }
  }

  btn.onclick = () => {
    backdrop.style.display = "flex";
    refreshStatus();
  };
  document.getElementById("ct-ssd-close-btn").onclick = () => {
    backdrop.style.display = "none";
  };
  backdrop.onclick = (e) => {
    if (e.target === backdrop) backdrop.style.display = "none";
  };
  canvasBtn.onclick = () => {
    const wfJson = window.app?.graph?.serialize?.();
    if (!wfJson || !wfJson.nodes || !wfJson.nodes.length) {
      logBox.textContent = "⚠️ 当前画布为空，请先在画布中打开一个工作流，或在下方输入工作流 URL / 模板名。";
      return;
    }
    startJob({ workflow_json: wfJson, workflow_name: "current_canvas_workflow.json" });
  };
  urlBtn.onclick = () => {
    const spec = specInput.value.trim();
    if (!spec) {
      logBox.textContent = "⚠️ 请输入工作流 GitHub URL、官方模板名或网盘工作流文件名。";
      return;
    }
    startJob({ workflow: spec });
  };
  modelBtn.onclick = () => {
    const mUrl = modelUrlInput.value.trim();
    if (!mUrl || !/^https?:\/\//i.test(mUrl)) {
      logBox.textContent = "⚠️ 请输入有效的模型下载 URL (http:// 或 https://)。";
      return;
    }
    const targetDir = (dirSelect.value === "__custom__" ? customDirInput.value : dirSelect.value).trim().replace(/^\/+|\/+$/g, "");
    if (!targetDir) {
      logBox.textContent = "⚠️ 请选择或输入目标模型子目录（如 loras, diffusion_models, vae）。";
      return;
    }
    let mName = modelNameInput.value.trim().replace(/^\/+/, "");
    if (!mName) {
      try {
        mName = decodeURIComponent(mUrl.split("?")[0]).split("/").filter(Boolean).pop() || "";
      } catch (e) {}
    }
    if (!mName) {
      logBox.textContent = "⚠️ 无法从 URL 自动提取文件名，请在「保存文件名」输入框中指定文件名（如 model.safetensors）。";
      return;
    }
    startJob({
      model_url: mUrl,
      directory: targetDir,
      filename: mName,
      workflow_name: `direct_model:${targetDir}/${mName}`,
      workflow_json: {
        properties: {
          models: [{ url: mUrl, directory: targetDir, name: mName }]
        }
      }
    });
  };
})();""")

os.makedirs("/content/ComfyUI/custom_nodes", exist_ok=True)
open("/content/ComfyUI/custom_nodes/ct_workflow_downloader.py", "w", encoding="utf-8").write(r"""import secrets, sys, threading, time
from pathlib import Path
from urllib.parse import unquote
from aiohttp import web
from server import PromptServer
sys.path.insert(0, "/content/ComfyUI")
from ct_workflow_helper import list_ssd_models, sync_workflow_models

JOBS = {}
_DL_LOCK = threading.Lock()

def _run_job(job_id, wf, wf_json_obj=None, wf_label=None):
    job = JOBS[job_id]
    with _DL_LOCK:
        try:
            res = sync_workflow_models(wf, job=job, wf_json_obj=wf_json_obj, wf_label=wf_label)
            job["result"] = res
            job["status"] = "done"
        except Exception as e:
            job["error"] = str(e)
            job["logs"].append(f"[workflow-dl] ERROR: {e}")
            job["status"] = "error"
        job["finished_at"] = time.time()

@PromptServer.instance.routes.post("/api/ct/pull-workflow")
async def ct_pull_workflow(request):
    body = await request.json()
    wf = (body.get("workflow") or "").strip()
    wf_json_obj = body.get("workflow_json")
    wf_label = body.get("workflow_name") or "current_canvas.json"
    model_url = (body.get("model_url") or "").strip()
    if model_url and not isinstance(wf_json_obj, dict):
        d = (body.get("directory") or "diffusion_models").strip().strip("/")
        fn = (body.get("filename") or unquote(model_url.split("?")[0]).rstrip("/").split("/")[-1]).strip().lstrip("/")
        wf_label = f"direct_model:{d}/{fn}"
        wf_json_obj = {"properties": {"models": [{"url": model_url, "directory": d, "name": fn}]}}
    if not wf and not isinstance(wf_json_obj, dict):
        return web.json_response({"error": "missing 'workflow', 'workflow_json', or 'model_url'"}, status=400)
    job_id = secrets.token_hex(8)
    JOBS[job_id] = {
        "job_id": job_id, "workflow_spec": wf or wf_label, "status": "running",
        "total": 0, "completed": 0, "current": None, "models": [], "logs": [],
        "started_at": time.time(),
    }
    threading.Thread(target=_run_job, args=(job_id, wf, wf_json_obj, wf_label), daemon=True).start()
    return web.json_response(JOBS[job_id], status=202)

@PromptServer.instance.routes.get("/api/ct/pull-workflow/{job_id}")
async def ct_pull_workflow_status(request):
    job_id = request.match_info["job_id"]
    if job_id not in JOBS:
        return web.json_response({"error": "unknown job_id"}, status=404)
    return web.json_response(JOBS[job_id])

@PromptServer.instance.routes.get("/api/ct/ssd-status")
async def ct_ssd_status(request):
    info = list_ssd_models()
    active = [j for j in JOBS.values() if j.get("status") == "running"]
    info["active_job"] = active[-1] if active else None
    return web.json_response(info)

@PromptServer.instance.routes.get("/api/ct/ssd-downloader.js")
async def ct_ssd_downloader_js(request):
    js = Path("/content/ComfyUI/ct_ssd_downloader.js").read_text(encoding="utf-8")
    return web.Response(text=js, content_type="application/javascript")

NODE_CLASS_MAPPINGS = {}
""")

EXTRA_ARGS = os.environ.get("CT_COMFY_ARGS", "").split()

def start_service():
    subprocess.run(["pkill", "-f", "ComfyUI/main.py"], check=False)
    args = [sys.executable, f"{COMFY_DIR}/main.py",
            "--listen", "127.0.0.1", "--port", str(SERVICE_PORT),
            "--enable-manager", "--preview-method", "auto", *EXTRA_ARGS]
    if os.environ.get("CT_COMFY_LEGACY_UI", "1") == "1":
        args.append("--enable-manager-legacy-ui")
    return ct_spawn("service", args, cwd=COMFY_DIR)

PROCS["service"] = (start_service(), start_service)
ct_wait_http(f"http://127.0.0.1:{SERVICE_PORT}/system_stats", timeout=300)
print(f"ComfyUI listening on 127.0.0.1:{SERVICE_PORT}")
'''

# --------------------------------------------------------------------------- nemotron-transcribe (G4, vLLM + NeMo Speech SALM)
NEMOTRON_TRANSCRIBE_CFG = dict(
    name="nemotron-transcribe",
    service_port=8000,
    auth="builtin",
    public_port=8000,
    health_path="/health",
    ready_timeout=3600,
)

NEMOTRON_TRANSCRIBE_SETUP = r'''# @title 1. Setup -- parallel NeMo Speech + vLLM 0.32 env & local SSD model download (67.5 GB BF16 / FP8 / NVFP4)
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib, platform

PRECISION_REPOS = {
    "bf16": "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B",
    "fp8": "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-FP8",
    "nvfp4": "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-NVFP4",
}
PRECISION = os.environ.get("CT_NEMOTRON_PRECISION", "bf16").strip().lower()
MODEL_REPO = os.environ.get("CT_NEMOTRON_REPO", "").strip() or PRECISION_REPOS.get(PRECISION, PRECISION_REPOS["bf16"])
MODEL_ALIAS = os.environ.get("CT_NEMOTRON_ALIAS", "nemotron-transcribe").strip()
NEMO_REPO = os.environ.get("CT_NEMO_SPEECH_REPO", "https://github.com/NVIDIA-NeMo/Speech.git").strip()

NT_WORK = Path("/content/nemotron-transcribe")
NEMO_DIR = Path("/content/nemo-speech")
VENV_DIR = NEMO_DIR / ".venv"
MODEL_DIR = NT_WORK / "model" / MODEL_REPO.split("/")[-1]
DRIVE_AUDIO_DIR = Path(DATA_DIR) / "audio"
DRIVE_OUTPUT_DIR = Path(DATA_DIR) / "outputs"
FLASHINFER_DRIVE_DIR = Path(DATA_DIR) / "flashinfer-cache"
FLASHINFER_CACHE_DIR = NT_WORK / "flashinfer-cache"
TRITON_DRIVE_DIR = Path(DATA_DIR) / "triton-cache"
TRITON_CACHE_DIR = NT_WORK / "triton-cache"
VLLM_DRIVE_DIR = Path(DATA_DIR) / "vllm-cache"
VLLM_CACHE_DIR = NT_WORK / "vllm-cache"

for d in (
    NT_WORK, MODEL_DIR.parent, DRIVE_AUDIO_DIR, DRIVE_OUTPUT_DIR,
    FLASHINFER_DRIVE_DIR, FLASHINFER_CACHE_DIR,
    TRITON_DRIVE_DIR, TRITON_CACHE_DIR,
    VLLM_DRIVE_DIR, VLLM_CACHE_DIR,
):
    d.mkdir(parents=True, exist_ok=True)
for _src_d, _dst_d in (
    (FLASHINFER_DRIVE_DIR, FLASHINFER_CACHE_DIR),
    (TRITON_DRIVE_DIR, TRITON_CACHE_DIR),
    (VLLM_DRIVE_DIR, VLLM_CACHE_DIR),
):
    if any(_src_d.iterdir()):
        subprocess.run(["cp", "-rn", f"{_src_d}/.", f"{_dst_d}/"], check=False)

def nlog(msg):
    print(time.strftime("[%F %T] [nemotron] ") + msg, flush=True)

def run_logged(cmd, log_path, env=None, cwd=None, check=True):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n$ {' '.join(str(x) for x in cmd)}\n")
        f.flush()
        res = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env, cwd=cwd, text=True)
    if check and res.returncode != 0:
        tail = "".join(open(log_path, errors="replace").readlines()[-40:])
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(str(x) for x in cmd[:4])}\n{tail}")
    return res

gpu_info = subprocess.check_output(
    ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader"], text=True
).strip()
nlog(f"GPU detected: {gpu_info} | repo={MODEL_REPO} precision={PRECISION}")

if not shutil.which("ffmpeg"):
    subprocess.run("apt-get update -qq && apt-get install -y -qq ffmpeg >/dev/null 2>&1", shell=True, check=False)

if not shutil.which("yt-dlp"):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"], check=False)

if not shutil.which("uv"):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "uv"], check=True)

def prepare_env():
    env_log = NT_WORK / "env-setup.log"
    marker = VENV_DIR / ".ct_env_ready"
    vllm_bin = VENV_DIR / "bin" / "vllm"
    py_bin = VENV_DIR / "bin" / "python"
    if marker.is_file() and vllm_bin.is_file() and py_bin.is_file():
        nlog("Reusing existing NeMo Speech + vLLM 0.32 virtualenv")
        return VENV_DIR

    nlog(f"Setting up NeMo Speech + vLLM 0.32 environment (log: {env_log})...")
    if not (NEMO_DIR / ".git").is_dir():
        run_logged(["git", "clone", "--depth", "1", NEMO_REPO, str(NEMO_DIR)], env_log)
    else:
        run_logged(["git", "-C", str(NEMO_DIR), "pull", "--ff-only"], env_log, check=False)

    uv_bin = shutil.which("uv") or "uv"
    run_logged([uv_bin, "venv", "--python", "3.13", str(VENV_DIR), "--clear"], env_log, cwd=str(NEMO_DIR))
    sync_env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(VENV_DIR)}
    res = run_logged(
        [uv_bin, "sync", "--extra", "asr", "--extra", "vllm", "--locked", "--no-install-project"],
        env_log, env=sync_env, cwd=str(NEMO_DIR), check=False
    )
    if res.returncode != 0:
        nlog("`uv sync --locked` exited non-zero; retrying without `--locked`...")
        run_logged(
            [uv_bin, "sync", "--extra", "asr", "--extra", "vllm", "--no-install-project"],
            env_log, env=sync_env, cwd=str(NEMO_DIR)
        )
    run_logged([uv_bin, "pip", "install", "--python", str(py_bin), "--no-deps", "-e", str(NEMO_DIR)], env_log)
    run_logged(
        [uv_bin, "pip", "install", "--python", str(py_bin), "--no-deps", "av==19.0.2", "opencv-python-headless==5.0.0.93"],
        env_log,
    )
    run_logged([uv_bin, "pip", "install", "--python", str(py_bin), "-U", "yt-dlp"], env_log, check=False)

    # Apply official NVIDIA DFlash2 safety patch to vLLM 0.32 if applicable
    patch_code = r"""
from pathlib import Path
import vllm
path = Path(vllm.__file__).parent / "model_executor/models/qwen3_dflash2.py"
if path.is_file():
    source = path.read_text()
    old = "    successors = successor_table[candidate_ids]\n"
    anchor = "            anchor_token_ids[:, None, None].expand(-1, 1, top_k),"
    if "safe_anchor_token_ids" not in source and source.count(old) == 1 and source.count(anchor) == 1:
        source = source.replace(old, (
            "    safe_anchor_token_ids = anchor_token_ids.clamp(\n"
            "        min=0, max=predecessor_table.shape[0] - 1\n"
            "    )\n" + old
        )).replace(anchor, "            safe_anchor_token_ids[:, None, None].expand(-1, 1, top_k),")
        path.write_text(source)
"""
    run_logged([str(py_bin), "-c", patch_code], env_log, check=False)
    run_logged([str(vllm_bin), "download-kernels"], env_log, env=sync_env, check=False)
    marker.write_text("ok\n", encoding="utf-8")
    nlog("NeMo Speech + vLLM 0.32 environment ready")
    return VENV_DIR

def _cleanup_hf_temp():
    for p in ("/root/.cache/huggingface/xet", "/root/.cache/huggingface/hub", str(MODEL_DIR / ".cache")):
        shutil.rmtree(p, ignore_errors=True)

def prepare_model():
    dl_log = NT_WORK / "model-download.log"
    marker = MODEL_DIR / ".ct_model_ready"
    has_weights = any(MODEL_DIR.glob("*.safetensors")) if MODEL_DIR.is_dir() else False
    if marker.is_file() and (MODEL_DIR / "config.json").is_file() and has_weights:
        nlog(f"Reusing local SSD model at {MODEL_DIR}")
        return MODEL_DIR

    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "huggingface_hub", "hf_xet"], check=True)
    _cleanup_hf_temp()
    free_gb = shutil.disk_usage("/content").free / 1e9
    nlog(f"Downloading {MODEL_REPO} -> local SSD {MODEL_DIR} (free SSD: {free_gb:.1f} GB; log: {dl_log})...")
    dl_env = {**os.environ, "HF_XET_CHUNK_CACHE_SIZE_BYTES": "0", "HF_HUB_ENABLE_HF_TRANSFER": "0"}
    dl_env.pop("HF_XET_HIGH_PERFORMANCE", None)
    dl_code = (
        "import sys; from huggingface_hub import snapshot_download; "
        "snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2])"
    )
    try:
        run_logged([sys.executable, "-c", dl_code, MODEL_REPO, str(MODEL_DIR)], dl_log, env=dl_env)
    finally:
        _cleanup_hf_temp()

    if not (MODEL_DIR / "config.json").is_file() or not any(MODEL_DIR.glob("*.safetensors")):
        raise RuntimeError(f"Model download incomplete in {MODEL_DIR}")
    marker.write_text(MODEL_REPO + "\n", encoding="utf-8")
    total_gb = sum(p.stat().st_size for p in MODEL_DIR.rglob("*") if p.is_file()) / 1e9
    nlog(f"Model ready on local SSD: {MODEL_DIR} ({total_gb:.2f} GB)")
    return MODEL_DIR

with ThreadPoolExecutor(max_workers=2) as pool:
    f_env = pool.submit(prepare_env)
    f_mod = pool.submit(prepare_model)
    while not (f_env.done() and f_mod.done()):
        for fut in (f_env, f_mod):
            if fut.done() and fut.exception():
                raise fut.exception()
        free_gb = shutil.disk_usage("/content").free / 1e9
        nlog(f"Setup heartbeat | env={'done' if f_env.done() else 'running'} "
             f"model={'done' if f_mod.done() else 'running'} | free_ssd={free_gb:.1f} GB")
        time.sleep(30)
    VENV_DIR = f_env.result()
    MODEL_DIR = f_mod.result()

nlog(f"Setup complete: venv={VENV_DIR} model={MODEL_DIR} drive_data={DATA_DIR}")
'''

NEMOTRON_TRANSCRIBE_SERVICE = r'''# @title 2. Service -- vLLM + NeMo Speech SALM on 127.0.0.1:8000 with Bearer Auth + Google Drive Audio API
(NT_WORK / "ct_nemotron_middleware.py").write_text(r"""from __future__ import annotations

import asyncio
import base64
import glob as glob_mod
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse
from nemo.collections.speechlm2.vllm.salm.ctc_serving import ctc_timestamp_middleware

API_TOKEN = os.environ.get("CT_API_TOKEN", "")
SERVICE_PORT = int(os.environ.get("CT_SERVICE_PORT", "8000"))
DATA_DIR = Path(os.environ.get("CT_DATA_DIR", "/content/drive/MyDrive/colab-llm/nemotron-transcribe"))
DRIVE_ROOT = Path("/content/drive")
MYDRIVE_ROOT = Path("/content/drive/MyDrive")
MODEL_REPO = os.environ.get("CT_NEMOTRON_REPO", "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B")

AUDIO_EXTS = (
    ".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac", ".opus", ".webm", ".mp4", ".mkv", ".wma", ".mpga", ".mpeg",
)
DIRECT_MIMES = {
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}

PRESETS: dict[str, dict[str, Any]] = {
    "verbatim": {
        "description": "逐字转写（保留语气词、重复、标点与大小写，多语种通用默认推荐）",
        "user_prompt": (
            "Audio transcription. Language: as spoken. Wording: verbatim, including fillers and repetitions. "
            "Formatting: punctuation and capitalization. Output: transcript only."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "clean": {
        "description": "整洁书面转写（去除口头禅与无意重复，保留原意与标点大小写）",
        "user_prompt": (
            "Audio transcription. Language: as spoken. Wording: omit fillers and accidental repetitions; "
            "keep the remaining spoken wording unchanged. Formatting: punctuation and capitalization. "
            "Output: transcript only."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "speaker": {
        "description": "说话人分离转写 + 词级时间戳（带 <spk:0> <spk:1> 标签，配合 timestamps=True）",
        "user_prompt": (
            "Transcribe this audio with punctuation and capitalization. Include all spoken words and verbal fillers. "
            "Write numbers in their standard written form. Put speaker tags <spk:0> <spk:1> <spk:2> <spk:3> on the transcription."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": True,
    },
    "multispeaker": {
        "description": "多说话人纯文本转写（无标点小写，每轮讲话前标注 <spk:N>）",
        "user_prompt": (
            "Transcribe this audio. Include all spoken words and verbal fillers. Write numbers as spoken. "
            "Use no punctuation and no capitalization. Start every speaker turn with a speaker tag such as "
            "<spk:0>, <spk:1>, <spk:2>, etc.; assign one stable tag per speaker and keep speaker identities "
            "consistent. Output only the speaker-tagged transcript."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "rich": {
        "description": "富文本转写（包含非语音事件 <ev:*> 与说话风格 <sty:*> 标签）",
        "user_prompt": (
            "Transcribe this audio with punctuation and capitalization. Include all spoken words and verbal fillers. "
            "Write numbers in their standard written form. Use <ev:*> and <sty:*> special tokens to recognize all "
            "non-speech verbatims and speaking style."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "near-field": {
        "description": "仅转写距离麦克风最近的主讲人（过滤远场背景人声）",
        "user_prompt": (
            "Transcribe this audio. Include all spoken words and verbal fillers. Write numbers as spoken. "
            "No punctuation or capitalization. Transcribe only the speaker nearest to the microphone. "
            "Do not transcribe any other voices."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "structured": {
        "description": "结构化内容转写（精确保留邮箱、URL、文件路径、命令、编号与标点分隔符）",
        "user_prompt": (
            "Transcribe as a clean written transcript. Preserve exact values and punctuation/separators in emails, "
            "URLs, file paths, commands, and identifiers. Use conventional written forms."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "context": {
        "description": "带领域词汇/专有名词提示的上下文偏置转写（配合 context 参数）",
        "user_prompt": "Transcribe with punctuation and capitalization. Context terms: {context}.",
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "translate": {
        "description": "语音直接翻译为目标语言文本（配合 lang 参数，如 Chinese / English / Japanese）",
        "user_prompt": "Please translate the given speech to {lang}.",
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "lid": {
        "description": "语种识别（输出录音对应的语言标签）",
        "user_prompt": (
            "Identify the language spoken in this audio. Respond with exactly one of the following locale labels "
            "and nothing else: ar_eg, bg_bg, cmn_hans_cn, hr_hr, cs_cz, da_dk, nl_nl, en_us, et_ee, fil_ph, fi_fi, "
            "fr_fr, de_de, el_gr, he_il, hi_in, hu_hu, id_id, it_it, ja_jp, ko_kr, lv_lv, lt_lt, mt_mt, pl_pl, "
            "pt_br, ro_ro, ru_ru, sk_sk, sl_si, es_419, sv_se, th_th, tr_tr, uk_ua, vi_vn."
        ),
        "system_prompt": "",
        "reasoning": False,
        "timestamps": False,
    },
    "qa": {
        "description": "音频理解与问答（开启推理模式 enable_thinking=True，根据录音内容回答问题）",
        "user_prompt": "What is happening in this audio?",
        "system_prompt": (
            "Answer the question directly using evidence from every provided audio clip in order. "
            "Answer every requested part, preserve exact names and numbers, and give a concise, specific answer first. "
            "Do not invent details."
        ),
        "reasoning": True,
        "timestamps": False,
    },
}

CHAR_RUN = re.compile(r"(.)\1{20,}", re.DOTALL)
BLOCK_RUN = re.compile(r"(.{1,4096}?)\1{19,}", re.DOTALL)

def cleanup_generation_loops(text: str) -> str:
    if not text:
        return text
    text = CHAR_RUN.sub(lambda m: m.group(1), text)
    return BLOCK_RUN.sub(lambda m: m.group(1), text)

def _check_auth(request: Any) -> bool:
    if not API_TOKEN:
        return True
    auth = request.headers.get("authorization") or ""
    if auth == f"Bearer {API_TOKEN}":
        return True
    xkey = request.headers.get("x-api-key") or ""
    if xkey == API_TOKEN:
        return True
    return False

def _resolve_drive_path(raw_spec: str) -> Path:
    spec = (raw_spec or "").strip()
    for prefix in ("drive://", "gdrive://", "file://"):
        if spec.startswith(prefix):
            spec = spec[len(prefix):]
    spec = spec.lstrip("/")
    if not spec:
        return MYDRIVE_ROOT if MYDRIVE_ROOT.is_dir() else DATA_DIR

    candidates = []
    if spec.startswith("content/drive/"):
        candidates.append(Path("/") / spec)
    elif spec.startswith("drive/"):
        candidates.append(Path("/content") / spec)
    elif spec.startswith("MyDrive/"):
        candidates.append(DRIVE_ROOT / spec)
    elif spec == "MyDrive":
        candidates.append(MYDRIVE_ROOT)
    else:
        candidates.append(MYDRIVE_ROOT / spec)
        candidates.append(DATA_DIR / spec)
        candidates.append(DATA_DIR / "audio" / spec)
        candidates.append(Path("/") / spec)

    for c in candidates:
        if c.exists():
            return c.resolve()
    return candidates[0].resolve()

def _rel_to_mydrive(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(MYDRIVE_ROOT.resolve()))
    except Exception:
        return str(p)

SKIP_DRIVE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "custom_nodes",
    "models", ".runtimes", "Takeout", ".Trash", ".ipynb_checkpoints",
}

def _list_drive_audio_files(spec: str, recursive: bool = False) -> list[dict[str, Any]]:
    raw = (spec or "").strip()
    for prefix in ("drive://", "gdrive://", "file://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]

    has_glob = any(ch in raw for ch in ("*", "?", "["))
    matched_paths: list[Path] = []

    if has_glob:
        clean = raw.lstrip("/")
        bases = []
        if clean.startswith("content/drive/"):
            bases.append(Path("/") / clean)
        elif clean.startswith("MyDrive/"):
            bases.append(DRIVE_ROOT / clean)
        else:
            bases.append(MYDRIVE_ROOT / clean)
            bases.append(DATA_DIR / clean)
            bases.append(DATA_DIR / "audio" / clean)
        seen = set()
        for pattern_path in bases:
            for hit in sorted(glob_mod.glob(str(pattern_path), recursive=True)):
                hp = Path(hit).resolve()
                if hp.is_file() and hp.suffix.lower() in AUDIO_EXTS and str(hp) not in seen:
                    seen.add(str(hp))
                    matched_paths.append(hp)
    else:
        target = _resolve_drive_path(raw)
        if target.is_file():
            matched_paths.append(target)
        elif target.is_dir():
            if not recursive:
                for hp in sorted(target.iterdir()):
                    if hp.is_file() and hp.suffix.lower() in AUDIO_EXTS:
                        matched_paths.append(hp.resolve())
            else:
                for root_dir, dirnames, filenames in os.walk(target):
                    dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DRIVE_DIRS and not d.startswith("."))
                    for fn in sorted(filenames):
                        hp = Path(root_dir) / fn
                        if hp.suffix.lower() in AUDIO_EXTS:
                            matched_paths.append(hp.resolve())

    items = []
    for p in matched_paths:
        st = p.stat()
        items.append({
            "drive_path": str(p),
            "rel_path": _rel_to_mydrive(p),
            "name": p.name,
            "size_bytes": st.st_size,
            "size_mb": round(st.st_size / 1e6, 2),
            "mtime": int(st.st_mtime),
        })
    return items

def _probe_audio_duration_sec(audio_path: Path) -> float:
    # Return audio/video duration in seconds using ffprobe (0.0 if unknown).
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        val = (res.stdout or "").strip()
        return float(val) if val and val != "N/A" else 0.0
    except Exception:
        return 0.0


def _find_silence_near(audio_path: Path, target_sec: float, window_sec: float = 45.0) -> float:
    # Search for a natural speech pause within [target_sec - window_sec, target_sec + window_sec].
    win_start = max(0.0, target_sec - window_sec)
    win_dur = window_sec * 2.0
    try:
        res = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner",
                "-ss", f"{win_start:.3f}", "-t", f"{win_dur:.3f}",
                "-i", str(audio_path),
                "-vn", "-af", "silencedetect=n=-35dB:d=0.35",
                "-f", "null", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=25,
            check=False,
        )
        log_out = res.stderr or ""
        starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", log_out)]
        ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", log_out)]
        candidates: list[float] = []
        for idx, st in enumerate(starts):
            ed = ends[idx] if idx < len(ends) else (st + 0.35)
            mid = win_start + (st + ed) / 2.0
            candidates.append(mid)
        if candidates:
            return min(candidates, key=lambda c: abs(c - target_sec))
    except Exception:
        pass
    return target_sec


def _compute_chunk_ranges(
    audio_path: Path,
    total_duration: float,
    max_chunk_sec: float = 2400.0,
) -> list[tuple[float, float]]:
    # Split audio longer than max_chunk_sec into balanced, silence-aligned (start_sec, end_sec) chunks.
    if max_chunk_sec <= 0 or total_duration <= 0 or total_duration <= max_chunk_sec:
        return [(0.0, max(total_duration, 0.0))]

    import math
    num_chunks = max(2, int(math.ceil(total_duration / max_chunk_sec)))
    ideal_len = total_duration / num_chunks
    boundaries = [0.0]
    for k in range(1, num_chunks):
        target = k * ideal_len
        split_pt = _find_silence_near(audio_path, target, window_sec=min(45.0, ideal_len * 0.15))
        split_pt = max(boundaries[-1] + 30.0, min(split_pt, total_duration - 30.0))
        boundaries.append(round(split_pt, 3))
    boundaries.append(round(total_duration, 3))
    return [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]


def _load_audio_as_data_url(
    audio_path: Path,
    force_ffmpeg_16k: bool = False,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> str:
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found on Google Drive: {audio_path}")
    ext = audio_path.suffix.lower()
    sz = audio_path.stat().st_size
    is_slice = (start_sec is not None and start_sec > 0.0) or (end_sec is not None and end_sec > 0.0)
    if not is_slice and not force_ffmpeg_16k and ext in DIRECT_MIMES and sz <= 140 * 1024 * 1024:
        b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        return f"data:{DIRECT_MIMES[ext]};base64,{b64}"
    # Convert video containers, slices, or large files to 16kHz mono PCM16 WAV in memory via ffmpeg
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start_sec is not None and start_sec > 0.0:
        cmd.extend(["-ss", f"{start_sec:.3f}"])
    if end_sec is not None and end_sec > 0.0:
        dur = max(0.1, end_sec - (start_sec or 0.0))
        cmd.extend(["-t", f"{dur:.3f}"])
    cmd.extend([
        "-i", str(audio_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
    ])
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if res.returncode != 0 or not res.stdout:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed decoding {audio_path.name}: {err}")
    b64 = base64.b64encode(res.stdout).decode("ascii")
    return f"data:audio/wav;base64,{b64}"


def _shift_ctc_timestamps(ctc_ts: dict[str, Any] | None, offset_sec: float) -> dict[str, Any] | None:
    if not isinstance(ctc_ts, dict) or offset_sec <= 0.0:
        return ctc_ts
    shifted: dict[str, Any] = dict(ctc_ts)
    for key in ("words", "segments", "diarization"):
        items = ctc_ts.get(key)
        if isinstance(items, list):
            new_items = []
            for item in items:
                if isinstance(item, dict):
                    cp = dict(item)
                    if cp.get("start") is not None:
                        cp["start"] = round(float(cp["start"]) + offset_sec, 3)
                    if cp.get("end") is not None:
                        cp["end"] = round(float(cp["end"]) + offset_sec, 3)
                    new_items.append(cp)
                else:
                    new_items.append(item)
            shifted[key] = new_items
    return shifted

def _fmt_srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def _words_to_srt(words: list[dict[str, Any]]) -> str:
    if not words:
        return ""
    segments = []
    cur_words: list[str] = []
    cur_start = 0.0
    cur_end = 0.0
    cur_spk = None

    def flush():
        nonlocal cur_words, cur_start, cur_end, cur_spk
        if not cur_words:
            return
        prefix = f"[{cur_spk}] " if cur_spk else ""
        text = prefix + " ".join(cur_words).strip()
        segments.append((cur_start, max(cur_end, cur_start + 0.2), text))
        cur_words = []

    for w in words:
        w_text = str(w.get("word") or "").strip()
        if not w_text:
            continue
        w_start = float(w.get("start") or 0.0)
        w_end = float(w.get("end") or w_start)
        w_spk = w.get("speaker")
        if not cur_words:
            cur_start = w_start
            cur_end = w_end
            cur_spk = w_spk
            cur_words.append(w_text)
            continue
        gap = w_start - cur_end
        spk_changed = (w_spk != cur_spk)
        if spk_changed or gap > 1.2 or len(cur_words) >= 14 or (len(cur_words) >= 6 and cur_words[-1].endswith((".", "?", "!", "。", "？", "！"))):
            flush()
            cur_start = w_start
            cur_end = w_end
            cur_spk = w_spk
            cur_words.append(w_text)
        else:
            cur_end = max(cur_end, w_end)
            cur_words.append(w_text)
    flush()

    lines = []
    for idx, (st_t, ed_t, txt) in enumerate(segments, 1):
        lines.append(f"{idx}\n{_fmt_srt_time(st_t)} --> {_fmt_srt_time(ed_t)}\n{txt}\n")
    return "\n".join(lines)

def _build_prompt_and_settings(body: dict[str, Any]) -> tuple[str, str, bool, bool]:
    preset_name = (body.get("preset") or "").strip().lower()
    preset = PRESETS.get(preset_name) if preset_name else None

    user_prompt = body.get("prompt")
    if user_prompt is None or str(user_prompt).strip() == "":
        user_prompt = preset["user_prompt"] if preset else PRESETS["verbatim"]["user_prompt"]
    user_prompt = str(user_prompt)

    context_terms = (body.get("context") or "").strip()
    target_lang = (body.get("lang") or body.get("language") or "Chinese").strip()
    if "{context}" in user_prompt:
        user_prompt = user_prompt.replace("{context}", context_terms or "general")
    elif context_terms and "Context terms:" not in user_prompt:
        user_prompt = f"{user_prompt.rstrip()} Context terms: {context_terms}."
    if "{lang}" in user_prompt:
        user_prompt = user_prompt.replace("{lang}", target_lang)

    sys_prompt = body.get("system_prompt")
    if sys_prompt is None:
        sys_prompt = preset["system_prompt"] if preset else ""
    sys_prompt = str(sys_prompt)

    reasoning = body.get("reasoning")
    if reasoning is None:
        reasoning = bool(preset["reasoning"]) if preset else False
    else:
        reasoning = bool(reasoning)

    timestamps = body.get("timestamps")
    if timestamps is None:
        timestamps = bool(preset["timestamps"]) if preset else False
    else:
        timestamps = bool(timestamps)

    return user_prompt, sys_prompt, reasoning, timestamps

def _call_local_chat_completions(payload: dict[str, Any], timeout: int = 1800) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{SERVICE_PORT}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": {"message": raw, "code": e.code}}

def _is_media_url(spec: str) -> bool:
    s = (spec or "").strip()
    return s.startswith(("http://", "https://", "ytdl://", "yt-dlp://"))


def _clean_media_url(spec: str) -> str:
    s = (spec or "").strip()
    for pfx in ("ytdl://", "yt-dlp://"):
        if s.startswith(pfx):
            return s[len(pfx):]
    return s


def _ensure_ytdlp() -> str:
    for cand in (
        shutil.which("yt-dlp"),
        "/content/nemo-speech/.venv/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
    ):
        if cand and os.path.isfile(cand):
            return cand
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"], check=True)
    return shutil.which("yt-dlp") or "yt-dlp"


def _prepare_cookies_file(cookies_spec: str = "") -> Path | None:
    candidates = []
    if cookies_spec:
        candidates.append(_resolve_drive_path(cookies_spec))
    candidates.extend([
        DATA_DIR / "cookies.txt",
        MYDRIVE_ROOT / "colab-llm" / "cookies.txt",
        MYDRIVE_ROOT / "cookies.txt",
    ])
    for c in candidates:
        if c.is_file() and c.stat().st_size > 0:
            tmp_cookie = Path(f"/tmp/ct_ytdlp_cookies_{os.getpid()}.txt")
            shutil.copyfile(c, tmp_cookie)
            return tmp_cookie
    return None


def _sanitize_filename(name: str, max_len: int = 80) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    return (cleaned[:max_len] or "audio").strip("._ ")


def _download_media_url_to_drive(
    raw_url: str,
    download_dir: str = "",
    audio_format: str = "mp3",
    cookies_drive: str = "",
    playlist: bool = False,
    force_download: bool = False,
    download_video: bool = False,
    save_media_to_drive: bool = True,
) -> list[dict[str, Any]]:
    # Download media URL(s) (YouTube, Twitter/X, Bilibili, TikTok, etc.) via yt-dlp onto Google Drive (or local SSD temp).
    url = _clean_media_url(raw_url)
    if save_media_to_drive:
        target_dir = _resolve_drive_path(download_dir) if (download_dir and download_dir.strip()) else (DATA_DIR / "audio" / "yt-dlp")
    else:
        target_dir = Path("/content/nemotron-transcribe/ytdlp-temp") / uuid.uuid4().hex[:12]
    target_dir.mkdir(parents=True, exist_ok=True)
    fmt = (audio_format or "mp3").strip().lower().lstrip(".")
    if fmt not in ("mp3", "m4a", "wav", "opus", "flac"):
        fmt = "mp3"

    ytdlp_bin = _ensure_ytdlp()
    stage_dir = Path("/content/nemotron-transcribe/ytdlp-stage") / uuid.uuid4().hex[:12]
    stage_dir.mkdir(parents=True, exist_ok=True)
    cookie_file = _prepare_cookies_file(cookies_drive)

    try:
        out_tmpl = str(stage_dir / "%(extractor)s__%(id)s.%(ext)s")
        cmd = [
            ytdlp_bin,
            "--no-progress",
            "--no-warnings",
        ]
        if download_video:
            cmd.extend([
                "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                "--merge-output-format", "mp4",
            ])
        else:
            cmd.extend([
                "--extract-audio",
                "--audio-format", fmt,
                "--audio-quality", "0",
            ])
        cmd.extend([
            "--write-info-json",
            "--output", out_tmpl,
        ])
        if not playlist:
            cmd.append("--no-playlist")
        else:
            cmd.append("--yes-playlist")
        if cookie_file:
            cmd.extend(["--cookies", str(cookie_file)])
        cmd.append(url)

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1800, check=False)
        info_files = sorted(stage_dir.glob("*.info.json"))
        if res.returncode != 0 and not info_files:
            err = (res.stderr or res.stdout or "").strip()[-1200:]
            raise RuntimeError(f"yt-dlp failed for {url}: {err}")

        downloaded_items: list[dict[str, Any]] = []
        for info_path in info_files:
            try:
                info = json.loads(info_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                info = {}
            if info.get("_type") == "playlist":
                continue
            stem_prefix = info_path.name[:-len(".info.json")]
            audio_candidates = [
                p for p in stage_dir.glob(f"{stem_prefix}.*")
                if p.suffix.lower() in AUDIO_EXTS and not p.name.endswith(".info.json")
            ]
            if not audio_candidates:
                continue
            staged_audio = audio_candidates[0]
            extractor = _sanitize_filename(str(info.get("extractor") or info.get("extractor_key") or "media"), 24).lower()
            vid_id = _sanitize_filename(str(info.get("id") or uuid.uuid4().hex[:8]), 40)
            title = _sanitize_filename(str(info.get("title") or vid_id), 70)
            dest_name = f"{extractor}_{vid_id}_{title}{staged_audio.suffix.lower()}"
            dest_path = target_dir / dest_name

            # Check if an earlier download for the same extractor_id already exists on Drive
            if save_media_to_drive and not force_download and not dest_path.is_file():
                existing = [
                    p for p in target_dir.glob(f"{extractor}_{vid_id}_*{staged_audio.suffix.lower()}")
                    if p.is_file() and p.stat().st_size > 0
                ]
                if existing:
                    dest_path = existing[0]

            if force_download or not dest_path.is_file() or dest_path.stat().st_size == 0:
                partial_path = target_dir / f".{dest_name}.{os.getpid()}.partial"
                shutil.copyfile(staged_audio, partial_path)
                partial_path.replace(dest_path)

            st = dest_path.stat()
            downloaded_items.append({
                "source_url": raw_url,
                "webpage_url": info.get("webpage_url") or url,
                "video_id": info.get("id"),
                "extractor": info.get("extractor_key") or info.get("extractor"),
                "title": info.get("title"),
                "uploader": info.get("uploader"),
                "duration": info.get("duration"),
                "media_type": "video" if download_video else "audio",
                "saved_to_drive": save_media_to_drive,
                "drive_path": str(dest_path),
                "rel_path": _rel_to_mydrive(dest_path) if save_media_to_drive else dest_path.name,
                "name": dest_path.name,
                "size_bytes": st.st_size,
                "size_mb": round(st.st_size / 1e6, 2),
            })

        # Fallback if yt-dlp produced an audio/video file without a matching per-entry info.json
        if not downloaded_items:
            for p in sorted(stage_dir.iterdir()):
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                    dest_path = target_dir / _sanitize_filename(p.stem, 90)
                    dest_path = dest_path.with_suffix(p.suffix.lower())
                    shutil.copyfile(p, dest_path)
                    st = dest_path.stat()
                    downloaded_items.append({
                        "source_url": raw_url,
                        "webpage_url": url,
                        "media_type": "video" if download_video else "audio",
                        "saved_to_drive": save_media_to_drive,
                        "drive_path": str(dest_path),
                        "rel_path": _rel_to_mydrive(dest_path) if save_media_to_drive else dest_path.name,
                        "name": dest_path.name,
                        "size_bytes": st.st_size,
                        "size_mb": round(st.st_size / 1e6, 2),
                    })

        if not downloaded_items:
            err = (res.stderr or res.stdout or "").strip()[-1200:]
            raise RuntimeError(f"yt-dlp did not produce any media files for {url}. Output: {err}")
        return downloaded_items
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        if cookie_file and cookie_file.is_file():
            cookie_file.unlink(missing_ok=True)


def _run_drive_transcribe_sync(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    t0 = time.monotonic()
    raw_path = (
        body.get("drive_path") or body.get("audio") or body.get("path") or body.get("url") or body.get("media_url") or ""
    ).strip()
    if not raw_path:
        return 400, {"error": "Missing 'drive_path' or 'url' (Google Drive audio file path or media URL)"}

    media_meta: dict[str, Any] | None = None
    save_media_to_drive = bool(body.get("save_media_to_drive", True))
    temp_media_path: Path | None = None
    if _is_media_url(raw_path):
        dl_items = _download_media_url_to_drive(
            raw_url=raw_path,
            download_dir=str(body.get("download_dir") or ""),
            audio_format=str(body.get("audio_format") or "mp3"),
            cookies_drive=str(body.get("cookies_drive") or ""),
            playlist=bool(body.get("playlist", False)),
            force_download=bool(body.get("force_download", False)),
            download_video=bool(body.get("download_video", False)),
            save_media_to_drive=save_media_to_drive,
        )
        media_meta = dl_items[0]
        audio_path = Path(media_meta["drive_path"])
        if not save_media_to_drive:
            temp_media_path = audio_path
    else:
        audio_path = _resolve_drive_path(raw_path)

    if not audio_path.is_file():
        return 404, {
            "error": f"Audio file not found on Google Drive: '{raw_path}' (resolved to '{audio_path}')",
            "resolved_path": str(audio_path),
            "drive_mounted": MYDRIVE_ROOT.is_dir(),
        }

    user_prompt, sys_prompt, reasoning, timestamps = _build_prompt_and_settings(body)
    force_ffmpeg = bool(body.get("normalize_audio", False))

    # Detect total duration and automatically split long audio (> chunk_minutes, default 40 min = 2400s)
    total_duration = _probe_audio_duration_sec(audio_path)
    raw_chunk_min = body.get("chunk_minutes")
    if raw_chunk_min is not None:
        max_chunk_sec = float(raw_chunk_min) * 60.0
    elif body.get("chunk_seconds") is not None:
        max_chunk_sec = float(body["chunk_seconds"])
    else:
        max_chunk_sec = 2400.0  # 40 minutes default (safe headroom inside 65536 context window & 1h encoder limit)

    chunk_ranges = _compute_chunk_ranges(audio_path, total_duration, max_chunk_sec=max_chunk_sec)
    is_multi_chunk = len(chunk_ranges) > 1

    extra_body: dict[str, Any] = {
        "chat_template_kwargs": {"enable_thinking": reasoning},
        "skip_special_tokens": bool(body.get("skip_special_tokens", False)),
        "spaces_between_special_tokens": False,
        "seed": int(body.get("seed", 0)),
    }
    if reasoning:
        extra_body.update(top_k=-1, thinking_token_budget=int(body.get("thinking_token_budget", 17408)))
        extra_body["chat_template_kwargs"]["reasoning_budget"] = int(body.get("reasoning_budget", 16384))
    if timestamps:
        extra_body["mm_processor_kwargs"] = {"capture_ctc_timestamps": True}

    temp = body.get("temperature")
    if temp is None:
        temp = 0.6 if reasoning else 0.0
    top_p = body.get("top_p")
    if top_p is None:
        top_p = 0.95 if reasoning else 1.0
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = 20480 if reasoning else 16384

    texts: list[str] = []
    reasonings: list[str] = []
    merged_words: list[dict[str, Any]] = []
    merged_segments: list[dict[str, Any]] = []
    merged_diarization: list[dict[str, Any]] = []
    last_ctc_ts: dict[str, Any] | None = None
    chunk_meta_list: list[dict[str, Any]] = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    for c_idx, (c_start, c_end) in enumerate(chunk_ranges, 1):
        c_t0 = time.monotonic()
        if is_multi_chunk:
            data_url = _load_audio_as_data_url(audio_path, force_ffmpeg_16k=True, start_sec=c_start, end_sec=c_end)
        else:
            data_url = _load_audio_as_data_url(audio_path, force_ffmpeg_16k=force_ffmpeg)

        messages: list[dict[str, Any]] = []
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {"type": "audio_url", "audio_url": {"url": data_url}},
            ],
        })

        chat_payload = {
            "model": body.get("model") or MODEL_REPO,
            "messages": messages,
            "temperature": float(temp),
            "top_p": float(top_p),
            "max_tokens": int(max_tokens),
            **extra_body,
        }

        status_code, completion = _call_local_chat_completions(chat_payload)
        if status_code != 200:
            return status_code, completion

        choice = (completion.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        c_text = (msg.get("content") or "").strip()
        c_reasoning = msg.get("reasoning_content") or msg.get("reasoning") or None
        if not reasoning and body.get("cleanup_loops", True):
            c_text = cleanup_generation_loops(c_text)

        if c_text:
            if is_multi_chunk and reasoning:
                texts.append(f"[{_fmt_srt_time(c_start)[:8]} - {_fmt_srt_time(c_end)[:8]}]\n{c_text}")
            else:
                texts.append(c_text)
        if c_reasoning:
            if is_multi_chunk:
                reasonings.append(f"[Chunk {c_idx}/{len(chunk_ranges)}: {_fmt_srt_time(c_start)[:8]} - {_fmt_srt_time(c_end)[:8]}]\n{c_reasoning}")
            else:
                reasonings.append(str(c_reasoning))

        c_ctc = _shift_ctc_timestamps(completion.get("ctc_timestamps"), c_start)
        if isinstance(c_ctc, dict):
            last_ctc_ts = c_ctc
            if isinstance(c_ctc.get("words"), list):
                merged_words.extend(c_ctc["words"])
            if isinstance(c_ctc.get("segments"), list):
                merged_segments.extend(c_ctc["segments"])
            if isinstance(c_ctc.get("diarization"), list):
                merged_diarization.extend(c_ctc["diarization"])

        u = completion.get("usage")
        if isinstance(u, dict):
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                total_usage[k] += int(u.get(k) or 0)

        chunk_meta_list.append({
            "chunk": c_idx,
            "start_sec": round(c_start, 2),
            "end_sec": round(c_end, 2),
            "duration_sec": round(max(0.0, c_end - c_start), 2),
            "elapsed_sec": round(time.monotonic() - c_t0, 2),
        })

    text = ("\n\n" if is_multi_chunk else "").join(texts)
    reasoning_content = "\n\n".join(reasonings) if reasonings else None

    if last_ctc_ts is not None:
        ctc_ts: dict[str, Any] | None = dict(last_ctc_ts)
        if merged_words:
            ctc_ts["words"] = merged_words
        if merged_segments:
            ctc_ts["segments"] = merged_segments
        if merged_diarization:
            ctc_ts["diarization"] = merged_diarization
    else:
        ctc_ts = None

    srt_text = _words_to_srt(merged_words) if merged_words else ""

    elapsed = round(time.monotonic() - t0, 2)
    result: dict[str, Any] = {
        "drive_path": str(audio_path),
        "rel_path": _rel_to_mydrive(audio_path),
        "source_url": media_meta["source_url"] if media_meta else None,
        "media_info": media_meta,
        "duration_sec": round(total_duration, 2) if total_duration > 0 else None,
        "chunk_count": len(chunk_ranges),
        "chunks": chunk_meta_list if is_multi_chunk else None,
        "prompt": user_prompt,
        "system_prompt": sys_prompt,
        "preset": body.get("preset") or None,
        "reasoning": reasoning,
        "timestamps": timestamps,
        "text": text,
        "reasoning_content": reasoning_content,
        "ctc_timestamps": ctc_ts,
        "srt": srt_text or None,
        "elapsed_sec": elapsed,
        "usage": total_usage if total_usage["total_tokens"] > 0 else None,
        "saved_drive_files": [],
    }

    save_to_drive = bool(body.get("save_to_drive", False))
    save_drive_dir = (body.get("save_drive_dir") or "").strip()
    if save_to_drive or save_drive_dir:
        if save_drive_dir:
            out_dir = _resolve_drive_path(save_drive_dir)
        elif temp_media_path is not None:
            out_dir = DATA_DIR / "transcripts"
        else:
            out_dir = audio_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = audio_path.stem
        txt_file = out_dir / f"{stem}.txt"
        txt_file.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
        result["saved_drive_files"].append(str(txt_file))

        json_file = out_dir / f"{stem}.nemotron.json"
        json_file.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        result["saved_drive_files"].append(str(json_file))

        if srt_text:
            srt_file = out_dir / f"{stem}.srt"
            srt_file.write_text(srt_text, encoding="utf-8")
            result["saved_drive_files"].append(str(srt_file))

    if temp_media_path is not None:
        shutil.rmtree(temp_media_path.parent, ignore_errors=True)
        result["drive_path"] = None

    return 200, result

YTDLP_DOMAIN_MARKERS = (
    "youtube.com/", "youtu.be/", "twitter.com/", "x.com/", "t.co/",
    "bilibili.com/", "b23.tv/", "tiktok.com/", "soundcloud.com/",
    "vimeo.com/", "twitch.tv/", "xiaoyuzhoufm.com/", "podcasts.apple.com/",
    "ytdl://", "yt-dlp://",
)

def _rewrite_drive_urls_in_chat_body(raw_body: bytes) -> bytes | None:
    if (
        b"drive://" not in raw_body
        and b"gdrive://" not in raw_body
        and b"/content/drive/" not in raw_body
        and not any(m.encode() in raw_body for m in YTDLP_DOMAIN_MARKERS)
    ):
        return None
    try:
        payload = json.loads(raw_body)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    changed = False
    for msg in payload.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "audio_url":
                continue
            aurl = item.get("audio_url")
            url_val = aurl.get("url") if isinstance(aurl, dict) else None
            if not isinstance(url_val, str):
                continue
            if url_val.startswith(("drive://", "gdrive://", "/content/drive/", "file:///content/drive/")):
                ap = _resolve_drive_path(url_val)
                aurl["url"] = _load_audio_as_data_url(ap)
                changed = True
            elif any(m in url_val for m in YTDLP_DOMAIN_MARKERS):
                dl_items = _download_media_url_to_drive(url_val)
                ap = Path(dl_items[0]["drive_path"])
                aurl["url"] = _load_audio_as_data_url(ap)
                changed = True
    if not changed:
        return None
    return json.dumps(payload).encode("utf-8")

async def nemotron_middleware(request: Any, call_next: Any) -> Any:
    path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
    client_host = (request.client.host if request.client else "") or ""

    # Allow local watchdog health probe without auth
    if path == "/health" and client_host in ("127.0.0.1", "::1", "localhost") and not request.headers.get("authorization"):
        return await call_next(request)

    if not _check_auth(request):
        return JSONResponse(
            {"error": {"message": "Unauthorized: invalid or missing Bearer token", "type": "AuthenticationError", "code": 401}},
            status_code=HTTPStatus.UNAUTHORIZED,
        )

    if request.method == "GET" and path == "/api/ct/prompts":
        return JSONResponse({"presets": PRESETS, "default_preset": "verbatim"})

    if request.method == "GET" and path == "/api/ct/drive-list":
        spec = request.query_params.get("path", "")
        recursive = request.query_params.get("recursive", "1") not in ("0", "false", "False")
        try:
            files = await asyncio.to_thread(_list_drive_audio_files, spec, recursive)
            return JSONResponse({
                "drive_mounted": MYDRIVE_ROOT.is_dir(),
                "query": spec,
                "count": len(files),
                "files": files,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    if request.method == "POST" and path == "/api/ct/ytdlp-download":
        try:
            body = json.loads(await request.body())
            raw_url = (body.get("url") or body.get("media_url") or body.get("audio") or "").strip()
            if not raw_url:
                return JSONResponse({"error": "Missing 'url'"}, status_code=400)
            items = await asyncio.to_thread(
                _download_media_url_to_drive,
                raw_url=raw_url,
                download_dir=str(body.get("download_dir") or ""),
                audio_format=str(body.get("audio_format") or "mp3"),
                cookies_drive=str(body.get("cookies_drive") or ""),
                playlist=bool(body.get("playlist", False)),
                force_download=bool(body.get("force_download", False)),
                download_video=bool(body.get("download_video", False)),
                save_media_to_drive=bool(body.get("save_media_to_drive", True)),
            )
            return JSONResponse({"count": len(items), "files": items})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    if request.method == "POST" and path == "/api/ct/drive-transcribe":
        try:
            body = json.loads(await request.body())
            code, res = await asyncio.to_thread(_run_drive_transcribe_sync, body)
            return JSONResponse(res, status_code=code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    if request.method == "POST" and path == "/v1/chat/completions":
        raw_body = await request.body()
        rewritten = await asyncio.to_thread(_rewrite_drive_urls_in_chat_body, raw_body)
        if rewritten is not None:
            code, res = await asyncio.to_thread(_call_local_chat_completions, json.loads(rewritten))
            return JSONResponse(res, status_code=code)
        return await ctc_timestamp_middleware(request, call_next)

    return await call_next(request)
""", encoding="utf-8")

GPU_MEM_UTIL = os.environ.get("CT_NEMOTRON_GPU_MEM", "0.92" if PRECISION == "bf16" else "0.85")
MAX_MODEL_LEN = int(os.environ.get("CT_NEMOTRON_MAX_MODEL_LEN", "65536"))
MAX_NUM_SEQS = int(os.environ.get("CT_NEMOTRON_MAX_SEQS", "64"))
MTP_TOKENS = int(os.environ.get("CT_NEMOTRON_MTP", "8"))
LIMIT_MM = os.environ.get(
    "CT_NEMOTRON_LIMIT_MM",
    '{"audio":{"count":6,"length":120000000}}' if PRECISION == "bf16" else '{"audio":{"count":1,"length":57600000}}',
)

VLLM_ENV = {
    "PATH": f"{VENV_DIR}/bin:/usr/local/cuda/bin:{os.environ.get('PATH', '')}",
    "PYTHONPATH": f"{NT_WORK}:{os.environ.get('PYTHONPATH', '')}",
    "VIRTUAL_ENV": str(VENV_DIR),
    "VLLM_PLUGINS": "nemo_speechlm",
    "VLLM_MAX_AUDIO_DECODE_DURATION_S": "3600",
    "VLLM_MAX_AUDIO_CLIP_FILESIZE_MB": "180",
    "VLLM_USE_V2_MODEL_RUNNER": "0",
    "FLASHINFER_WORKSPACE_BASE": str(FLASHINFER_CACHE_DIR),
    "TRITON_CACHE_DIR": str(TRITON_CACHE_DIR),
    "VLLM_CACHE_ROOT": str(VLLM_CACHE_DIR),
    "TORCHINDUCTOR_CACHE_DIR": str(VLLM_CACHE_DIR / "inductor"),
    "CT_API_TOKEN": API_TOKEN,
    "CT_SERVICE_PORT": str(SERVICE_PORT),
    "CT_DATA_DIR": str(DATA_DIR),
    "CT_NEMOTRON_REPO": MODEL_REPO,
}
if PRECISION in ("fp8", "nvfp4"):
    VLLM_ENV["VLLM_DISABLED_KERNELS"] = "FlashInferFP8ScaledMMLinearKernel"

SERVER_ARGS = [
    str(VENV_DIR / "bin" / "vllm"), "serve", str(MODEL_DIR),
    "--served-model-name", MODEL_REPO, MODEL_ALIAS,
    "--host", "127.0.0.1",
    "--port", str(SERVICE_PORT),
    "--api-key", API_TOKEN,
    "--trust-remote-code",
    "--dtype", "bfloat16",
    "--gpu-memory-utilization", str(GPU_MEM_UTIL),
    "--max-model-len", str(MAX_MODEL_LEN),
    "--max-num-batched-tokens", "4096",
    "--max-num-seqs", str(MAX_NUM_SEQS),
    "--enable-chunked-prefill",
    "--no-enable-prefix-caching",
    "--use-replayssm",
    "--mamba-backend", "flashinfer",
    "--mamba-cache-mode", "none",
    "--mm-processor-cache-gb", "0",
    "--no-async-scheduling",
    "--limit-mm-per-prompt", LIMIT_MM,
    "--reasoning-parser", "nemotron_v3",
    "--trust-request-mm-kwargs",
    "--middleware", "ct_nemotron_middleware.nemotron_middleware",
]
if PRECISION in ("fp8", "nvfp4"):
    SERVER_ARGS += ["--kv-cache-dtype", "auto"]
if MTP_TOKENS > 0:
    SERVER_ARGS += [
        "--speculative-config",
        json.dumps({"method": "mtp", "num_speculative_tokens": MTP_TOKENS}),
    ]

def start_service():
    subprocess.run(["pkill", "-f", "vllm serve"], check=False)
    nlog(f"Starting vLLM server on 127.0.0.1:{SERVICE_PORT} (model={MODEL_REPO}, mtp={MTP_TOKENS}, ctx={MAX_MODEL_LEN})")
    return ct_spawn("service", SERVER_ARGS, env=VLLM_ENV, cwd=str(NT_WORK))

PROCS["service"] = (start_service(), start_service)
'''

NEMOTRON_TRANSCRIBE_KEEPALIVE = r'''# @title 5. Ready + keep-alive -- wait for model load/FlashInfer warmup, verify Bearer auth, supervise
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"

def http_json(url, key=None, payload=None, timeout=20):
    headers = {"Content-Type": "application/json", "User-Agent": BROWSER_UA}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        url, headers=headers, data=None if payload is None else json.dumps(payload).encode("utf-8")
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {})
    except urllib.error.HTTPError as err:
        return err.code, {}

def auth_check(base, key):
    for candidate, expected in ((None, 401), ("invalid-token-probe", 401), (key, 200)):
        code, _ = http_json(base + "/v1/models", candidate)
        if code != expected:
            raise RuntimeError(f"Auth test failed on {base}/v1/models: expected {expected}, got {code}")

LOCAL_BASE = f"http://127.0.0.1:{SERVICE_PORT}"
deadline = time.monotonic() + 2400
last_notice = 0
while time.monotonic() < deadline:
    proc = PROCS["service"][0]
    if proc.poll() is not None:
        raise RuntimeError("vLLM exited during startup:\n" + ct_tail("service", 60))
    try:
        code, _ = http_json(LOCAL_BASE + "/health", API_TOKEN, timeout=10)
        if code == 200:
            break
    except Exception:
        pass
    if time.monotonic() - last_notice >= 30:
        gpu_stat = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader"],
            text=True,
        ).strip()
        last_line = (ct_tail("service", 3).strip().splitlines() or ["starting..."])[-1]
        nlog(f"Waiting for vLLM load / FlashInfer kernel warmup | GPU: {gpu_stat} | {last_line[:100]}")
        last_notice = time.monotonic()
    time.sleep(5)
else:
    raise RuntimeError("Timed out waiting for vLLM server to become ready:\n" + ct_tail("service", 60))

auth_check(LOCAL_BASE, API_TOKEN)
for _local_c, _drive_c in (
    (FLASHINFER_CACHE_DIR, FLASHINFER_DRIVE_DIR),
    (TRITON_CACHE_DIR, TRITON_DRIVE_DIR),
    (VLLM_CACHE_DIR, VLLM_DRIVE_DIR),
):
    if _local_c.is_dir() and _drive_c.is_dir():
        subprocess.run(["cp", "-ru", f"{_local_c}/.", f"{_drive_c}/"], check=False)
code, presets_info = http_json(LOCAL_BASE + "/api/ct/prompts", API_TOKEN)
if code != 200:
    raise RuntimeError(f"Local /api/ct/prompts check failed: HTTP {code}")
nlog(f"Local endpoint verified: /v1/models (401/401/200) & /api/ct/prompts ({ len(presets_info.get('presets', {})) } presets)")

pub_deadline = time.monotonic() + 120
while time.monotonic() < pub_deadline:
    try:
        code, _ = http_json(PUBLIC_URL + "/v1/models", API_TOKEN, timeout=15)
        if code == 200:
            auth_check(PUBLIC_URL, API_TOKEN)
            nlog(f"Public endpoint verified: {PUBLIC_URL}/v1/models")
            break
    except Exception as err:
        nlog(f"Public probe pending ({type(err).__name__}: {err})")
    time.sleep(8)

print(f"{SERVICE} is up: {PUBLIC_URL}")
print("CT_READY", flush=True)

started = time.monotonic()
failures = 0
while True:
    time.sleep(60)
    for name, (proc, restart) in list(PROCS.items()):
        if proc.poll() is not None:
            nlog(f"[watchdog] {name} exited with {proc.returncode}; restarting\n{ct_tail(name, 20)}")
            PROCS[name] = (restart(), restart)
    try:
        code, _ = http_json(LOCAL_BASE + "/health", API_TOKEN, timeout=10)
        failures = 0 if code == 200 else failures + 1
    except Exception:
        failures += 1
    if failures >= 6:
        nlog("[watchdog] 6 failed health checks; restarting vLLM service")
        PROCS["service"][0].terminate()
        PROCS["service"] = (start_service(), start_service)
        failures = 0
    gpu_mem = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader"],
        text=True,
    ).strip()
    nlog(f"heartbeat | up={int(time.monotonic() - started)}s | GPU: {gpu_mem}")
'''

# ---------------------------------------------------------------------------
# IndexTTS-2.5 (L4 GPU / vLLM-Omni 2-stage zero-shot voice cloning & emotion TTS)
# ---------------------------------------------------------------------------

INDEXTTS_CFG = dict(
    name="indextts",
    service_port=8000,
    auth="builtin",
    public_port=8000,
    health_path="/health",
    ready_timeout=3600,
)

INDEXTTS_SETUP = r'''# @title 1. Setup -- install vLLM-Omni + pre-stage IndexTTS-2.5 & sub-models on local SSD
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODEL_REPO = os.environ.get("CT_INDEXTTS_MODEL", "IndexTeam/IndexTTS-2.5").strip() or "IndexTeam/IndexTTS-2.5"
MODEL_ALIAS = os.environ.get("CT_INDEXTTS_ALIAS", "indextts-2.5").strip() or "indextts-2.5"
STAGE0_GPU_MEM = float(os.environ.get("CT_INDEXTTS_GPU_MEM_STAGE0", "0.4"))
STAGE1_GPU_MEM = float(os.environ.get("CT_INDEXTTS_GPU_MEM_STAGE1", "0.4"))
MAX_MODEL_LEN = int(os.environ.get("CT_INDEXTTS_MAX_MODEL_LEN", "2560"))
MAX_NUM_SEQS = int(os.environ.get("CT_INDEXTTS_MAX_NUM_SEQS", "4"))

IT_WORK = Path("/content/indextts")
VENV_DIR = IT_WORK / "venv"
MODEL_DIR = IT_WORK / "model" / "IndexTTS-2.5"
SPEAKERS_DIR = IT_WORK / "speakers"
DEFAULT_REF_WAV = IT_WORK / "default_ref.wav"

DRIVE_VOICES_DIR = Path(DATA_DIR) / "voices"
DRIVE_OUTPUT_DIR = Path(DATA_DIR) / "outputs"
FLASHINFER_DRIVE_DIR = Path(DATA_DIR) / "flashinfer-cache"
FLASHINFER_CACHE_DIR = IT_WORK / "flashinfer-cache"
TRITON_DRIVE_DIR = Path(DATA_DIR) / "triton-cache"
TRITON_CACHE_DIR = IT_WORK / "triton-cache"
VLLM_DRIVE_DIR = Path(DATA_DIR) / "vllm-cache"
VLLM_CACHE_DIR = IT_WORK / "vllm-cache"

for d in (
    IT_WORK, MODEL_DIR, SPEAKERS_DIR,
    DRIVE_VOICES_DIR, DRIVE_OUTPUT_DIR,
    FLASHINFER_DRIVE_DIR, FLASHINFER_CACHE_DIR,
    TRITON_DRIVE_DIR, TRITON_CACHE_DIR,
    VLLM_DRIVE_DIR, VLLM_CACHE_DIR,
):
    d.mkdir(parents=True, exist_ok=True)

for _src_d, _dst_d in (
    (FLASHINFER_DRIVE_DIR, FLASHINFER_CACHE_DIR),
    (TRITON_DRIVE_DIR, TRITON_CACHE_DIR),
    (VLLM_DRIVE_DIR, VLLM_CACHE_DIR),
    (DRIVE_VOICES_DIR, SPEAKERS_DIR),
):
    if any(_src_d.iterdir()):
        subprocess.run(["cp", "-rn", f"{_src_d}/.", f"{_dst_d}/"], check=False)

def ilog(msg):
    print(time.strftime("[%F %T] [indextts] ") + msg, flush=True)

def run_logged(cmd, log_path, env=None, cwd=None, check=True):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n$ {' '.join(str(x) for x in cmd)}\n")
        f.flush()
        res = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env, cwd=cwd, text=True)
    if check and res.returncode != 0:
        tail = "".join(open(log_path, errors="replace").readlines()[-45:])
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(str(x) for x in cmd[:4])}\n{tail}")
    return res

gpu_info = subprocess.check_output(
    ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader"], text=True
).strip()
ilog(f"GPU detected: {gpu_info} | repo={MODEL_REPO} stage0_mem={STAGE0_GPU_MEM} stage1_mem={STAGE1_GPU_MEM}")

if not shutil.which("ffmpeg"):
    subprocess.run("apt-get update -qq && apt-get install -y -qq ffmpeg >/dev/null 2>&1", shell=True, check=False)

if not shutil.which("uv"):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "uv"], check=True)

def prepare_env():
    env_log = IT_WORK / "env-setup.log"
    marker = VENV_DIR / ".ct_env_ready"
    vllm_bin = VENV_DIR / "bin" / "vllm"
    py_bin = VENV_DIR / "bin" / "python"
    if marker.is_file() and vllm_bin.is_file() and py_bin.is_file():
        chk = subprocess.run(
            [str(py_bin), "-c", "import vllm, vllm_omni, tiktoken, fugashi"],
            capture_output=True,
        )
        if chk.returncode == 0:
            ilog("Reusing existing vLLM + vLLM-Omni[indextts2] virtualenv")
            return VENV_DIR

    ilog(f"Setting up vLLM 0.32 + vLLM-Omni[indextts2] environment (log: {env_log})...")
    uv_bin = shutil.which("uv") or "uv"
    run_logged([uv_bin, "venv", "--python", "3.12", str(VENV_DIR), "--clear"], env_log)
    run_logged(
        [
            uv_bin, "pip", "install",
            "--python", str(py_bin),
            "vllm==0.32.0",
            "vllm-omni[indextts2]==0.32.0",
            "tiktoken",
            "soundfile",
        ],
        env_log,
    )
    subprocess.run([uv_bin, "cache", "clean"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run_logged([str(vllm_bin), "download-kernels"], env_log, check=False)
    marker.write_text("ok\n", encoding="utf-8")
    ilog("vLLM 0.32 + vLLM-Omni[indextts2] environment ready")
    return VENV_DIR

def _cleanup_hf_temp():
    for p in (
        "/root/.cache/huggingface/xet",
        "/root/.cache/huggingface/hub",
        str(MODEL_DIR / ".cache"),
        str(MODEL_DIR / "w2v-bert-2.0" / ".cache"),
        str(MODEL_DIR / "bigvgan" / ".cache"),
    ):
        shutil.rmtree(p, ignore_errors=True)

def _ensure_default_ref_wav():
    if DEFAULT_REF_WAV.is_file() and DEFAULT_REF_WAV.stat().st_size > 1024:
        return
    drive_copy = DRIVE_VOICES_DIR / "default_ref.wav"
    if drive_copy.is_file() and drive_copy.stat().st_size > 1024:
        shutil.copyfile(drive_copy, DEFAULT_REF_WAV)
        return
    ref_url = "https://raw.githubusercontent.com/vllm-project/vllm-omni/main/tests/assets/indextts2/ref_audio.wav"
    try:
        urllib.request.urlretrieve(ref_url, str(DEFAULT_REF_WAV))
    except Exception:
        import math, struct, wave
        sr = 24000
        dur = 3.0
        n = int(sr * dur)
        with wave.open(str(DEFAULT_REF_WAV), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            samples = [
                int(12000 * (0.6 * math.sin(2 * math.pi * 180 * i / sr) + 0.4 * math.sin(2 * math.pi * 360 * i / sr)))
                for i in range(n)
            ]
            wf.writeframes(struct.pack(f"<{n}h", *samples))
    if not drive_copy.is_file():
        shutil.copyfile(DEFAULT_REF_WAV, drive_copy)

def prepare_model():
    dl_log = IT_WORK / "model-download.log"
    marker = MODEL_DIR / ".ct_model_ready"
    required_files = (
        MODEL_DIR / "gpt.pth",
        MODEL_DIR / "s2mel.pth",
        MODEL_DIR / "codec.pth",
        MODEL_DIR / "multilingual_zh_ja_yue_char_del.tiktoken",
        MODEL_DIR / "qwen0.6bemo4-merge" / "model.safetensors",
        MODEL_DIR / "w2v-bert-2.0" / "model.safetensors",
        MODEL_DIR / "campplus_cn_common.bin",
        MODEL_DIR / "bigvgan" / "bigvgan_generator.pt",
    )
    _ensure_default_ref_wav()
    if marker.is_file() and all(p.is_file() for p in required_files):
        ilog(f"Reusing local SSD IndexTTS-2.5 bundle at {MODEL_DIR}")
        return MODEL_DIR

    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "huggingface_hub", "hf_xet"], check=True)
    _cleanup_hf_temp()
    free_gb = shutil.disk_usage("/content").free / 1e9
    ilog(f"Downloading {MODEL_REPO} + sub-models -> local SSD {MODEL_DIR} (free SSD: {free_gb:.1f} GB; log: {dl_log})...")
    dl_env = {**os.environ, "HF_XET_CHUNK_CACHE_SIZE_BYTES": "0", "HF_HUB_ENABLE_HF_TRANSFER": "0"}
    dl_env.pop("HF_XET_HIGH_PERFORMANCE", None)
    dl_script = r"""
import shutil, sys
from pathlib import Path
from huggingface_hub import hf_hub_download, snapshot_download

model_repo = sys.argv[1]
model_dir = Path(sys.argv[2])
model_dir.mkdir(parents=True, exist_ok=True)

def clean():
    for p in ("/root/.cache/huggingface/xet", "/root/.cache/huggingface/hub", str(model_dir / ".cache")):
        shutil.rmtree(p, ignore_errors=True)

print(f"[dl] 1/4 Downloading {model_repo} -> {model_dir}", flush=True)
snapshot_download(repo_id=model_repo, local_dir=str(model_dir), max_workers=4)
clean()

w2v_dir = model_dir / "w2v-bert-2.0"
print(f"[dl] 2/4 Downloading facebook/w2v-bert-2.0 -> {w2v_dir}", flush=True)
snapshot_download(
    repo_id="facebook/w2v-bert-2.0",
    local_dir=str(w2v_dir),
    allow_patterns=["config.json", "preprocessor_config.json", "model.safetensors"],
    max_workers=4,
)
clean()

print(f"[dl] 3/4 Downloading funasr/campplus -> {model_dir / 'campplus_cn_common.bin'}", flush=True)
hf_hub_download(repo_id="funasr/campplus", filename="campplus_cn_common.bin", local_dir=str(model_dir))
clean()

bigvgan_dir = model_dir / "bigvgan"
print(f"[dl] 4/4 Downloading nvidia/bigvgan_v2_22khz_80band_256x -> {bigvgan_dir}", flush=True)
snapshot_download(
    repo_id="nvidia/bigvgan_v2_22khz_80band_256x",
    local_dir=str(bigvgan_dir),
    allow_patterns=["config.json", "bigvgan_generator.pt"],
    max_workers=4,
)
clean()
"""
    try:
        run_logged([sys.executable, "-c", dl_script, MODEL_REPO, str(MODEL_DIR)], dl_log, env=dl_env)
    finally:
        _cleanup_hf_temp()

    missing = [str(p) for p in required_files if not p.is_file()]
    if missing:
        raise RuntimeError(f"Model download incomplete; missing files: {missing}")
    marker.write_text(MODEL_REPO + "\n", encoding="utf-8")
    total_gb = sum(p.stat().st_size for p in MODEL_DIR.rglob("*") if p.is_file()) / 1e9
    ilog(f"IndexTTS-2.5 + sub-models ready on local SSD: {MODEL_DIR} ({total_gb:.2f} GB)")
    return MODEL_DIR

with ThreadPoolExecutor(max_workers=2) as pool:
    f_env = pool.submit(prepare_env)
    f_mod = pool.submit(prepare_model)
    while not (f_env.done() and f_mod.done()):
        for fut in (f_env, f_mod):
            if fut.done() and fut.exception():
                raise fut.exception()
        free_gb = shutil.disk_usage("/content").free / 1e9
        ilog(f"Setup heartbeat | env={'done' if f_env.done() else 'running'} "
             f"model={'done' if f_mod.done() else 'running'} | free_ssd={free_gb:.1f} GB")
        time.sleep(30)
    VENV_DIR = f_env.result()
    MODEL_DIR = f_mod.result()

ilog(f"Setup complete: venv={VENV_DIR} model={MODEL_DIR} drive_data={DATA_DIR}")
'''

INDEXTTS_SERVICE = r'''# @title 2. Service -- vLLM-Omni IndexTTS-2.5 on 127.0.0.1:8000 (L4 2-stage pipeline + Bearer Auth + Google Drive Voices)
DEPLOY_YAML = IT_WORK / "indextts2_5.yaml"
DEPLOY_YAML.write_text(f"""pipeline: indextts2_5
async_chunk: false

stages:
  - stage_id: 0
    devices: "0"
    attention_backend: TRITON_ATTN
    max_num_seqs: {MAX_NUM_SEQS}
    max_model_len: {MAX_MODEL_LEN}
    max_num_batched_tokens: {MAX_MODEL_LEN}
    gpu_memory_utilization: {STAGE0_GPU_MEM}
    enable_prefix_caching: false
    enable_chunked_prefill: false
    hf_overrides:
      use_gpt_latent: false
    default_sampling_params:
      temperature: 0.8
      top_p: 0.8
      top_k: 30
      max_tokens: 1815
      repetition_penalty: 10.0
      seed: 42

  - stage_id: 1
    devices: "0"
    max_num_seqs: {MAX_NUM_SEQS}
    gpu_memory_utilization: {STAGE1_GPU_MEM}
    enforce_eager: true
    hf_overrides:
      use_gpt_latent: false
      s2mel_dit_bf16: true
      s2mel_cfm_batch_size: 4
      s2mel_dit_torch_compile: false
    default_sampling_params:
      temperature: 0.0
      top_p: 1.0
      top_k: -1
      max_tokens: 1815
      seed: 42
""", encoding="utf-8")

(IT_WORK / "ct_indextts_middleware.py").write_text(r"""from __future__ import annotations

import asyncio
import base64
import glob as glob_mod
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_TOKEN = os.environ.get("CT_API_TOKEN", "")
SERVICE_PORT = int(os.environ.get("CT_SERVICE_PORT", "8000"))
DATA_DIR = Path(os.environ.get("CT_DATA_DIR", "/content/drive/MyDrive/colab-data/indextts"))
SPEAKERS_DIR = Path(os.environ.get("SPEAKER_SAMPLES_DIR", "/content/indextts/speakers"))
DEFAULT_REF_WAV = Path(os.environ.get("CT_DEFAULT_REF_WAV", "/content/indextts/default_ref.wav"))
DRIVE_ROOT = Path("/content/drive")
MYDRIVE_ROOT = Path("/content/drive/MyDrive")
DRIVE_VOICES_DIR = DATA_DIR / "voices"
DRIVE_OUTPUT_DIR = DATA_DIR / "outputs"

AUDIO_EXTS = (
    ".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac", ".opus", ".webm", ".mp4", ".mkv", ".wma", ".mpga", ".mpeg",
)
SUPPORTED_LANGS = {
    "zh": "Chinese (Mandarin)",
    "en": "English",
    "zhen": "Chinese-English code-switching",
    "ja": "Japanese",
    "es": "Spanish",
    "ar": "Arabic",
    "yue": "Cantonese",
}
EMOTION_LABELS = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]


def _verify_auth(headers: Headers) -> bool:
    if not API_TOKEN:
        return True
    auth = headers.get("authorization") or ""
    if auth == f"Bearer {API_TOKEN}":
        return True
    xkey = headers.get("x-api-key") or ""
    if xkey == API_TOKEN:
        return True
    return False


def _resolve_drive_path(raw_spec: str) -> Path:
    spec = (raw_spec or "").strip()
    for prefix in ("drive://", "gdrive://", "file://"):
        if spec.startswith(prefix):
            spec = spec[len(prefix):]
    spec = spec.lstrip("/")
    if not spec:
        return DRIVE_VOICES_DIR if DRIVE_VOICES_DIR.is_dir() else MYDRIVE_ROOT

    candidates = []
    if spec.startswith("content/drive/"):
        candidates.append(Path("/") / spec)
    elif spec.startswith("drive/"):
        candidates.append(Path("/content") / spec)
    elif spec.startswith("MyDrive/"):
        candidates.append(DRIVE_ROOT / spec)
    elif spec == "MyDrive":
        candidates.append(MYDRIVE_ROOT)
    else:
        candidates.append(DRIVE_VOICES_DIR / spec)
        candidates.append(DATA_DIR / spec)
        candidates.append(MYDRIVE_ROOT / spec)
        candidates.append(Path("/") / spec)

    for c in candidates:
        if c.exists():
            return c.resolve()
    return candidates[0].resolve()


def _rel_to_mydrive(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(MYDRIVE_ROOT.resolve()))
    except Exception:
        return str(p)


SKIP_DRIVE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "custom_nodes",
    "models", ".runtimes", "Takeout", ".Trash", ".ipynb_checkpoints",
    "flashinfer-cache", "triton-cache", "vllm-cache",
}


def _list_drive_audio_files(spec: str, recursive: bool = True) -> list[dict[str, Any]]:
    raw = (spec or "").strip()
    for prefix in ("drive://", "gdrive://", "file://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]

    has_glob = any(ch in raw for ch in ("*", "?", "["))
    matched_paths: list[Path] = []

    if has_glob:
        clean = raw.lstrip("/")
        bases = []
        if clean.startswith("content/drive/"):
            bases.append(Path("/") / clean)
        elif clean.startswith("MyDrive/"):
            bases.append(DRIVE_ROOT / clean)
        else:
            bases.append(DRIVE_VOICES_DIR / clean)
            bases.append(DATA_DIR / clean)
            bases.append(MYDRIVE_ROOT / clean)
        seen = set()
        for pattern_path in bases:
            for hit in sorted(glob_mod.glob(str(pattern_path), recursive=True)):
                hp = Path(hit).resolve()
                if hp.is_file() and hp.suffix.lower() in AUDIO_EXTS and str(hp) not in seen:
                    seen.add(str(hp))
                    matched_paths.append(hp)
    else:
        target = _resolve_drive_path(raw)
        if target.is_file():
            matched_paths.append(target)
        elif target.is_dir():
            if not recursive:
                for hp in sorted(target.iterdir()):
                    if hp.is_file() and hp.suffix.lower() in AUDIO_EXTS:
                        matched_paths.append(hp.resolve())
            else:
                for root_dir, dirnames, filenames in os.walk(target):
                    dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DRIVE_DIRS and not d.startswith("."))
                    for fn in sorted(filenames):
                        hp = Path(root_dir) / fn
                        if hp.suffix.lower() in AUDIO_EXTS:
                            matched_paths.append(hp.resolve())

    items = []
    for p in matched_paths:
        st = p.stat()
        items.append({
            "drive_path": str(p),
            "rel_path": _rel_to_mydrive(p),
            "name": p.name,
            "size_bytes": st.st_size,
            "size_mb": round(st.st_size / 1e6, 2),
            "mtime": int(st.st_mtime),
        })
    return items


def _audio_path_to_wav_data_url(audio_path: Path, max_duration_sec: float = 24.0) -> str:
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-t", f"{max_duration_sec:.1f}",
        "-i", str(audio_path),
        "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if res.returncode != 0 or not res.stdout:
        if audio_path.suffix.lower() == ".wav":
            b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
            return f"data:audio/wav;base64,{b64}"
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed decoding {audio_path.name}: {err}")
    b64 = base64.b64encode(res.stdout).decode("ascii")
    return f"data:audio/wav;base64,{b64}"


def _sync_speakers_to_drive() -> None:
    try:
        DRIVE_VOICES_DIR.mkdir(parents=True, exist_ok=True)
        if SPEAKERS_DIR.is_dir():
            for sf in SPEAKERS_DIR.glob("*.safetensors"):
                dst = DRIVE_VOICES_DIR / sf.name
                if not dst.is_file() or sf.stat().st_mtime > dst.stat().st_mtime:
                    shutil.copy2(sf, dst)
    except Exception:
        pass


def _rewrite_speech_request_body(raw_body: bytes) -> tuple[bytes, str | None]:
    if not raw_body:
        return raw_body, None
    try:
        payload = json.loads(raw_body)
    except Exception:
        return raw_body, None
    if not isinstance(payload, dict):
        return raw_body, None

    changed = False
    save_drive_spec: str | None = None

    # Allow top-level or extra_params shortcuts for lang, emo_*, save_to_drive
    extras = payload.get("extra_params")
    if not isinstance(extras, dict):
        extras = {}

    for top_key in ("lang", "text_normalization", "emo_audio", "emo_vector", "emo_alpha", "emo_text", "use_emo_text", "use_random"):
        if top_key in payload and top_key not in extras:
            extras[top_key] = payload.pop(top_key)
            changed = True

    for save_key in ("save_to_drive", "save_drive_path"):
        val = payload.pop(save_key, None)
        if val is None and save_key in extras:
            val = extras.pop(save_key, None)
            changed = True
        elif val is not None:
            changed = True
        if val:
            save_drive_spec = str(val) if not isinstance(val, bool) else "auto"

    # Resolve drive:// or bare Drive paths in ref_audio
    ref_audio = payload.get("ref_audio")
    if isinstance(ref_audio, str) and ref_audio.strip():
        ra = ref_audio.strip()
        if ra.startswith(("drive://", "gdrive://", "/content/")) or not ra.startswith(("http://", "https://", "data:", "file://")):
            ap = _resolve_drive_path(ra)
            payload["ref_audio"] = _audio_path_to_wav_data_url(ap)
            changed = True
    elif ref_audio is None and (not payload.get("voice") or str(payload.get("voice")).strip() == "default"):
        # vLLM-Omni reserves 'default' as a built-in placeholder name and requires either an uploaded voice (e.g. 'alloy') or ref_audio
        if DEFAULT_REF_WAV.is_file():
            payload["ref_audio"] = f"file://{DEFAULT_REF_WAV}"
            payload.pop("voice", None)
            changed = True

    # Resolve drive:// or bare Drive paths in extra_params.emo_audio
    emo_audio = extras.get("emo_audio")
    if isinstance(emo_audio, str) and emo_audio.strip():
        ea = emo_audio.strip()
        if ea.startswith(("drive://", "gdrive://", "/content/")) or not ea.startswith(("http://", "https://", "data:", "file://")):
            ap = _resolve_drive_path(ea)
            extras["emo_audio"] = _audio_path_to_wav_data_url(ap)
            changed = True

    if extras:
        payload["extra_params"] = extras
    elif "extra_params" in payload and not extras:
        payload.pop("extra_params", None)

    if not changed:
        return raw_body, save_drive_spec
    return json.dumps(payload, ensure_ascii=False).encode("utf-8"), save_drive_spec


def _save_output_audio_to_drive(audio_bytes: bytes, spec: str, fmt: str = "wav") -> str:
    DRIVE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ext = "." + (fmt or "wav").lstrip(".")
    if spec and spec not in ("auto", "true", "True", "1"):
        dest = _resolve_drive_path(spec)
        if dest.is_dir() or spec.endswith("/"):
            dest.mkdir(parents=True, exist_ok=True)
            dest = dest / f"indextts-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.suffix:
                dest = dest.with_suffix(ext)
    else:
        dest = DRIVE_OUTPUT_DIR / f"indextts-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
    dest.write_bytes(audio_bytes)
    return str(dest)


def _upload_drive_voice_sync(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    name = str(body.get("name") or "").strip()
    raw_path = str(body.get("drive_path") or body.get("audio") or body.get("path") or "").strip()
    consent = str(body.get("consent") or "user-consent").strip()
    ref_text = body.get("ref_text")
    speaker_desc = body.get("speaker_description")
    if not name or not raw_path:
        return 400, {"error": "Both 'name' and 'drive_path' are required"}
    ap = _resolve_drive_path(raw_path)
    if not ap.is_file():
        return 404, {"error": f"Audio file not found on Google Drive: {raw_path} (resolved to {ap})"}

    # Convert to clean mono WAV bytes via ffmpeg
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-t", "24.0",
        "-i", str(ap), "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    wav_bytes = res.stdout if (res.returncode == 0 and res.stdout) else ap.read_bytes()

    boundary = f"----ctvoice{int(time.time() * 1000)}"
    parts: list[bytes] = []

    def add_field(k: str, v: str):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
        )

    add_field("name", name)
    add_field("consent", consent)
    if ref_text:
        add_field("ref_text", str(ref_text))
    if speaker_desc:
        add_field("speaker_description", str(speaker_desc))

    parts.append(
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"audio_sample\"; filename=\"{ap.stem}.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
        + wav_bytes
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    body_bytes = b"".join(parts)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {API_TOKEN}",
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{SERVICE_PORT}/v1/audio/voices",
        data=body_bytes,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode("utf-8", errors="replace"))
            _sync_speakers_to_drive()
            return resp.status, out
    except urllib.error.HTTPError as err:
        raw_err = err.read().decode("utf-8", errors="replace")
        try:
            return err.code, json.loads(raw_err)
        except Exception:
            return err.code, {"error": raw_err}


class IndexTTSMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        root_path = scope.get("root_path", "")
        path = scope["path"].removeprefix(root_path)
        headers = Headers(scope=scope)
        client = scope.get("client")
        client_host = (client[0] if client else "") or ""

        if path == "/health" and client_host in ("127.0.0.1", "::1", "localhost") and not headers.get("authorization"):
            await self.app(scope, receive, send)
            return

        if not _verify_auth(headers):
            resp = JSONResponse(
                {"error": {"message": "Unauthorized: invalid or missing Bearer token", "type": "AuthenticationError", "code": 401}},
                status_code=HTTPStatus.UNAUTHORIZED,
            )
            await resp(scope, receive, send)
            return

        if scope["type"] == "http":
            method = scope.get("method", "GET")

            if method == "GET" and path == "/api/ct/info":
                resp = JSONResponse({
                    "service": "indextts",
                    "model": "IndexTeam/IndexTTS-2.5",
                    "languages": SUPPORTED_LANGS,
                    "emotion_labels": EMOTION_LABELS,
                    "speed_range": [0.5, 2.0],
                    "default_ref_wav": str(DEFAULT_REF_WAV),
                    "drive_voices_dir": str(DRIVE_VOICES_DIR),
                    "drive_output_dir": str(DRIVE_OUTPUT_DIR),
                })
                await resp(scope, receive, send)
                return

            if method == "GET" and path == "/api/ct/logs":
                from urllib.parse import parse_qs
                qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="replace"))
                n_lines = max(1, min(int((qs.get("lines") or ["120"])[0]), 2000))
                log_file = Path("/content/ct/service.log")
                lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-n_lines:] if log_file.is_file() else []
                resp = JSONResponse({"lines": len(lines), "log": "\n".join(lines)})
                await resp(scope, receive, send)
                return

            if method == "GET" and path == "/api/ct/drive-list":
                from urllib.parse import parse_qs
                qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="replace"))
                spec = (qs.get("path") or [""])[0]
                recursive = (qs.get("recursive") or ["1"])[0] not in ("0", "false", "False")
                try:
                    files = await asyncio.to_thread(_list_drive_audio_files, spec, recursive)
                    resp = JSONResponse({
                        "drive_mounted": MYDRIVE_ROOT.is_dir(),
                        "query": spec,
                        "count": len(files),
                        "files": files,
                    })
                except Exception as e:
                    resp = JSONResponse({"error": str(e)}, status_code=400)
                await resp(scope, receive, send)
                return

            if method == "POST" and path == "/api/ct/drive-upload-voice":
                chunks: list[bytes] = []
                while True:
                    msg = await receive()
                    if msg["type"] == "http.request":
                        chunks.append(msg.get("body", b""))
                        if not msg.get("more_body", False):
                            break
                    elif msg["type"] == "http.disconnect":
                        return
                try:
                    body = json.loads(b"".join(chunks))
                    code, out = await asyncio.to_thread(_upload_drive_voice_sync, body)
                    resp = JSONResponse(out, status_code=code)
                except Exception as e:
                    resp = JSONResponse({"error": str(e)}, status_code=500)
                await resp(scope, receive, send)
                return

            if method == "POST" and path == "/v1/audio/speech":
                chunks = []
                while True:
                    msg = await receive()
                    if msg["type"] == "http.request":
                        chunks.append(msg.get("body", b""))
                        if not msg.get("more_body", False):
                            break
                    elif msg["type"] == "http.disconnect":
                        return
                raw_body = b"".join(chunks)
                try:
                    rewritten_body, save_drive_spec = await asyncio.to_thread(_rewrite_speech_request_body, raw_body)
                except Exception as e:
                    resp = JSONResponse(
                        {"error": {"message": str(e), "type": "BadRequestError", "code": 400}},
                        status_code=400,
                    )
                    await resp(scope, receive, send)
                    return

                header_save = headers.get("x-save-drive")
                if header_save and not save_drive_spec:
                    save_drive_spec = header_save

                body_sent = False

                async def replay_receive() -> Message:
                    nonlocal body_sent
                    if not body_sent:
                        body_sent = True
                        return {"type": "http.request", "body": rewritten_body, "more_body": False}
                    return await receive()

                if not save_drive_spec:
                    await self.app(scope, replay_receive, send)
                    return

                # Tee audio response to Google Drive when save_drive_spec is requested
                resp_status = 200
                resp_headers: list[tuple[bytes, bytes]] = []
                resp_chunks: list[bytes] = []
                start_msg: Message | None = None

                async def tee_send(message: Message) -> None:
                    nonlocal resp_status, resp_headers, start_msg
                    if message["type"] == "http.response.start":
                        resp_status = message.get("status", 200)
                        resp_headers = list(message.get("headers", []))
                        start_msg = message
                        if resp_status != 200:
                            await send(message)
                    elif message["type"] == "http.response.body":
                        chunk = message.get("body", b"")
                        more = message.get("more_body", False)
                        if resp_status != 200:
                            await send(message)
                            return
                        if chunk:
                            resp_chunks.append(chunk)
                        if not more and start_msg is not None:
                            full_audio = b"".join(resp_chunks)
                            saved_path = await asyncio.to_thread(
                                _save_output_audio_to_drive, full_audio, save_drive_spec, "wav"
                            )
                            new_headers = [
                                (k, v) for (k, v) in resp_headers if k.lower() != b"x-saved-drive-path"
                            ]
                            new_headers.append((b"x-saved-drive-path", saved_path.encode("utf-8")))
                            start_msg["headers"] = new_headers
                            await send(start_msg)
                            await send({"type": "http.response.body", "body": full_audio, "more_body": False})
                    else:
                        await send(message)

                await self.app(scope, replay_receive, tee_send)
                return

            if method in ("POST", "DELETE") and path.startswith("/v1/audio/voices"):
                await self.app(scope, receive, send)
                await asyncio.to_thread(_sync_speakers_to_drive)
                return

        await self.app(scope, receive, send)
""", encoding="utf-8")

VLLM_ENV = {
    "PATH": f"{VENV_DIR}/bin:/usr/local/cuda/bin:{os.environ.get('PATH', '')}",
    "PYTHONPATH": f"{IT_WORK}:{os.environ.get('PYTHONPATH', '')}",
    "VIRTUAL_ENV": str(VENV_DIR),
    "FLASHINFER_DISABLE_VERSION_CHECK": "1",
    "FLASHINFER_WORKSPACE_BASE": str(FLASHINFER_CACHE_DIR),
    "TRITON_CACHE_DIR": str(TRITON_CACHE_DIR),
    "VLLM_CACHE_ROOT": str(VLLM_CACHE_DIR),
    "TORCHINDUCTOR_CACHE_DIR": str(VLLM_CACHE_DIR / "inductor"),
    "SPEAKER_SAMPLES_DIR": str(SPEAKERS_DIR),
    "CT_API_TOKEN": API_TOKEN,
    "CT_SERVICE_PORT": str(SERVICE_PORT),
    "CT_DATA_DIR": str(DATA_DIR),
    "CT_DEFAULT_REF_WAV": str(DEFAULT_REF_WAV),
}

SERVER_ARGS = [
    str(VENV_DIR / "bin" / "vllm"), "serve", str(MODEL_DIR),
    "--omni",
    "--served-model-name", MODEL_REPO, MODEL_ALIAS,
    "--host", "127.0.0.1",
    "--port", str(SERVICE_PORT),
    "--api-key", API_TOKEN,
    "--trust-remote-code",
    "--allowed-local-media-path", "/content",
    "--stage-init-timeout", "900",
    "--deploy-config", str(DEPLOY_YAML),
    "--middleware", "ct_indextts_middleware.IndexTTSMiddleware",
]

def start_service():
    subprocess.run(["pkill", "-9", "-f", "vllm|EngineCore"], check=False)
    time.sleep(1)
    ilog(f"Starting vLLM-Omni IndexTTS-2.5 server on 127.0.0.1:{SERVICE_PORT} (model={MODEL_REPO})")
    return ct_spawn("service", SERVER_ARGS, env=VLLM_ENV, cwd=str(IT_WORK))

PROCS["service"] = (start_service(), start_service)
'''

INDEXTTS_KEEPALIVE = r'''# @title 5. Ready + keep-alive -- wait for 2-stage load, register default voice, warmup synthesis & sync caches
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"

def http_json(url, key=None, payload=None, timeout=30):
    headers = {"Content-Type": "application/json", "User-Agent": BROWSER_UA}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        url, headers=headers, data=None if payload is None else json.dumps(payload).encode("utf-8")
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {})
    except urllib.error.HTTPError as err:
        return err.code, {}

def auth_check(base, key):
    for candidate, expected in ((None, 401), ("invalid-token-probe", 401), (key, 200)):
        code, _ = http_json(base + "/v1/models", candidate)
        if code != expected:
            raise RuntimeError(f"Auth test failed on {base}/v1/models: expected {expected}, got {code}")

LOCAL_BASE = f"http://127.0.0.1:{SERVICE_PORT}"
deadline = time.monotonic() + 2400
last_notice = 0
while time.monotonic() < deadline:
    proc = PROCS["service"][0]
    if proc.poll() is not None:
        raise RuntimeError("vLLM-Omni exited during startup:\n" + ct_tail("service", 80))
    try:
        code, _ = http_json(LOCAL_BASE + "/health", API_TOKEN, timeout=10)
        if code == 200:
            break
    except Exception:
        pass
    if time.monotonic() - last_notice >= 30:
        gpu_stat = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader"],
            text=True,
        ).strip()
        last_line = (ct_tail("service", 3).strip().splitlines() or ["starting..."])[-1]
        ilog(f"Waiting for 2-stage IndexTTS-2.5 init | GPU: {gpu_stat} | {last_line[:100]}")
        last_notice = time.monotonic()
    time.sleep(5)
else:
    raise RuntimeError("Timed out waiting for vLLM-Omni server to become ready:\n" + ct_tail("service", 80))

auth_check(LOCAL_BASE, API_TOKEN)

# Register 'alloy' voice preset from DEFAULT_REF_WAV if not already registered
code, voices_data = http_json(LOCAL_BASE + "/v1/audio/voices", API_TOKEN, timeout=20)
existing_voices = set(voices_data.get("voices") or [])
for preset_voice in ("alloy",):
    if preset_voice not in existing_voices and DEFAULT_REF_WAV.is_file():
        v_code, v_res = http_json(
            LOCAL_BASE + "/api/ct/drive-upload-voice",
            API_TOKEN,
            payload={
                "name": preset_voice,
                "drive_path": str(DEFAULT_REF_WAV),
                "consent": "builtin-default-reference",
                "speaker_description": "Default neutral reference speaker for IndexTTS-2.5",
            },
            timeout=60,
        )
        ilog(f"Registered preset voice '{preset_voice}': HTTP {v_code}")

# Warmup synthesis to verify Stage 0 Talker + Stage 1 S2Mel DiT / BigVGAN and sync JIT caches to Drive
warmup_req = urllib.request.Request(
    LOCAL_BASE + "/v1/audio/speech",
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_TOKEN}"},
    data=json.dumps({
        "model": MODEL_REPO,
        "input": "Hello, IndexTTS is ready.",
        "voice": "default",
        "response_format": "wav",
        "extra_params": {"lang": "en"},
    }).encode("utf-8"),
)
try:
    with urllib.request.urlopen(warmup_req, timeout=180) as wresp:
        wav_bytes = wresp.read()
        ilog(f"Warmup synthesis succeeded ({len(wav_bytes)} bytes WAV)")
except Exception as werr:
    ilog(f"Warmup synthesis warning: {werr}")

for _local_c, _drive_c in (
    (FLASHINFER_CACHE_DIR, FLASHINFER_DRIVE_DIR),
    (TRITON_CACHE_DIR, TRITON_DRIVE_DIR),
    (VLLM_CACHE_DIR, VLLM_DRIVE_DIR),
    (SPEAKERS_DIR, DRIVE_VOICES_DIR),
):
    if _local_c.is_dir() and _drive_c.is_dir():
        subprocess.run(["cp", "-ru", f"{_local_c}/.", f"{_drive_c}/"], check=False)

pub_deadline = time.monotonic() + 120
while time.monotonic() < pub_deadline:
    try:
        code, _ = http_json(PUBLIC_URL + "/v1/models", API_TOKEN, timeout=15)
        if code == 200:
            auth_check(PUBLIC_URL, API_TOKEN)
            ilog(f"Public endpoint verified: {PUBLIC_URL}/v1/models")
            break
    except Exception as err:
        ilog(f"Public probe pending ({type(err).__name__}: {err})")
    time.sleep(8)

print(f"{SERVICE} is up: {PUBLIC_URL}")
print("CT_READY", flush=True)

started = time.monotonic()
failures = 0
while True:
    time.sleep(60)
    for name, (proc, restart) in list(PROCS.items()):
        if proc.poll() is not None:
            ilog(f"[watchdog] {name} exited with {proc.returncode}; restarting\n{ct_tail(name, 20)}")
            PROCS[name] = (restart(), restart)
    recent_log = ct_tail("service", 30)
    if "replica-0 is dead" in recent_log:
        ilog("[watchdog] Detected dead stage replica in service.log; restarting vLLM-Omni service")
        PROCS["service"][0].terminate()
        PROCS["service"] = (start_service(), start_service)
        failures = 0
    try:
        code, _ = http_json(LOCAL_BASE + "/health", API_TOKEN, timeout=10)
        failures = 0 if code == 200 else failures + 1
    except Exception:
        failures += 1
    if failures >= 6:
        ilog("[watchdog] 6 failed health checks; restarting vLLM-Omni service")
        PROCS["service"][0].terminate()
        PROCS["service"] = (start_service(), start_service)
        failures = 0
    gpu_mem = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader"],
        text=True,
    ).strip()
    ilog(f"heartbeat | up={int(time.monotonic() - started)}s | GPU: {gpu_mem}")
'''

# --------------------------------------------------------------------------- qwen3-tts (L4, vLLM-Omni 2-stage Qwen3-TTS CustomVoice / VoiceDesign / Base Voice Clone)
QWEN3_TTS_CFG = dict(
    name="qwen3-tts",
    service_port=8000,
    auth="builtin",
    public_port=8000,
    health_path="/health",
    ready_timeout=3600,
)

QWEN3_TTS_SETUP = r'''# @title 1. Setup -- install vLLM-Omni + Qwen3-TTS & pre-stage 12Hz models on local SSD
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VARIANT_REPOS = {
    "customvoice": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "1.7b-customvoice": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "voicedesign": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "1.7b-voicedesign": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    "1.7b-base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    "0.6b-customvoice": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
    "0.6b-base": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
}
VARIANT = os.environ.get("CT_QWEN3_TTS_VARIANT", "customvoice").strip().lower()
MODEL_REPO = os.environ.get("CT_QWEN3_TTS_MODEL", "").strip() or VARIANT_REPOS.get(VARIANT, VARIANT_REPOS["customvoice"])
MODEL_ALIAS = os.environ.get("CT_QWEN3_TTS_ALIAS", "qwen3-tts").strip()
PRELOAD_ALL = os.environ.get("CT_QWEN3_TTS_PRELOAD_ALL", "1").strip()
STAGE0_GPU_MEM = float(os.environ.get("CT_QWEN3_TTS_GPU_MEM_STAGE0", "0.30"))
STAGE1_GPU_MEM = float(os.environ.get("CT_QWEN3_TTS_GPU_MEM_STAGE1", "0.20"))
MAX_MODEL_LEN = int(os.environ.get("CT_QWEN3_TTS_MAX_MODEL_LEN", "4096"))
MAX_NUM_SEQS = int(os.environ.get("CT_QWEN3_TTS_MAX_NUM_SEQS", "16"))
AUX_PORT = int(os.environ.get("CT_QWEN3_TTS_AUX_PORT", "8001"))

QT_WORK = Path("/content/qwen3-tts")
VENV_DIR = QT_WORK / "venv"
AUX_VENV_DIR = QT_WORK / "aux-venv"
MODELS_ROOT = QT_WORK / "model"
MODEL_DIR = MODELS_ROOT / MODEL_REPO.split("/")[-1]
SPEAKERS_DIR = QT_WORK / "speakers"
DRIVE_VOICES_DIR = Path(DATA_DIR) / "voices"
DRIVE_OUTPUT_DIR = Path(DATA_DIR) / "outputs"
DEFAULT_REF_WAV = QT_WORK / "default_ref.wav"
DEFAULT_REF_TEXT = (
    "Okay. Yeah. I resent you. I love you. I respect you. "
    "But you know what? You blew it! And thanks to you."
)

# Two-tier JIT kernel precompilation cache (Local NVMe SSD <-> Google Drive)
FLASHINFER_DRIVE_DIR = Path(DATA_DIR) / "flashinfer-cache"
FLASHINFER_CACHE_DIR = QT_WORK / "flashinfer-cache"
TRITON_DRIVE_DIR = Path(DATA_DIR) / "triton-cache"
TRITON_CACHE_DIR = QT_WORK / "triton-cache"
VLLM_DRIVE_DIR = Path(DATA_DIR) / "vllm-cache"
VLLM_CACHE_DIR = QT_WORK / "vllm-cache"

for d in (
    QT_WORK, MODELS_ROOT, SPEAKERS_DIR, DRIVE_VOICES_DIR, DRIVE_OUTPUT_DIR,
    FLASHINFER_DRIVE_DIR, FLASHINFER_CACHE_DIR,
    TRITON_DRIVE_DIR, TRITON_CACHE_DIR,
    VLLM_DRIVE_DIR, VLLM_CACHE_DIR,
):
    d.mkdir(parents=True, exist_ok=True)

for _src_d, _dst_d in (
    (FLASHINFER_DRIVE_DIR, FLASHINFER_CACHE_DIR),
    (TRITON_DRIVE_DIR, TRITON_CACHE_DIR),
    (VLLM_DRIVE_DIR, VLLM_CACHE_DIR),
    (DRIVE_VOICES_DIR, SPEAKERS_DIR),
):
    if any(_src_d.iterdir()):
        subprocess.run(["cp", "-rn", f"{_src_d}/.", f"{_dst_d}/"], check=False)

def qlog(msg):
    print(time.strftime("[%F %T] [qwen3-tts] ") + msg, flush=True)

def run_logged(cmd, log_path, env=None, cwd=None, check=True):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n$ {' '.join(str(x) for x in cmd)}\n")
        f.flush()
        res = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env, cwd=cwd, text=True)
    if check and res.returncode != 0:
        tail = "".join(open(log_path, errors="replace").readlines()[-40:])
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(str(x) for x in cmd[:4])}\n{tail}")
    return res

gpu_info = subprocess.check_output(
    ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader"], text=True
).strip()
qlog(f"GPU detected: {gpu_info} | repo={MODEL_REPO} variant={VARIANT} stage0_mem={STAGE0_GPU_MEM} stage1_mem={STAGE1_GPU_MEM}")

if not shutil.which("ffmpeg") or not shutil.which("sox"):
    subprocess.run("apt-get update -qq && apt-get install -y -qq ffmpeg sox libsox-fmt-all >/dev/null 2>&1", shell=True, check=False)

if not shutil.which("uv"):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "uv"], check=True)

def prepare_env():
    env_log = QT_WORK / "env-setup.log"
    marker = VENV_DIR / ".ct_qwen3_tts_env_ready"
    vllm_bin = VENV_DIR / "bin" / "vllm"
    py_bin = VENV_DIR / "bin" / "python"
    aux_py_bin = AUX_VENV_DIR / "bin" / "python"
    if marker.is_file() and vllm_bin.is_file() and py_bin.is_file() and aux_py_bin.is_file():
        qlog("Reusing existing vLLM-Omni + Qwen3-TTS virtualenvs")
        return VENV_DIR

    qlog(f"Installing vLLM + vLLM-Omni + Qwen3-TTS environments (log: {env_log})...")
    uv_bin = shutil.which("uv") or "uv"
    run_logged([uv_bin, "venv", "--python", "3.12", str(VENV_DIR), "--clear"], env_log)
    run_logged(
        [
            uv_bin, "pip", "install", "--python", str(py_bin),
            "vllm==0.32.0", "vllm-omni==0.32.0",
            "tiktoken", "soundfile", "huggingface_hub", "hf_xet",
        ],
        env_log,
    )

    # Lightweight auxiliary venv (reusing system PyTorch) for cross-task Qwen3TTSModel fallback
    run_logged([uv_bin, "venv", "--python", "3.12", "--system-site-packages", str(AUX_VENV_DIR), "--clear"], env_log)
    run_logged(
        [
            uv_bin, "pip", "install", "--python", str(aux_py_bin),
            "transformers==4.57.3", "accelerate==1.12.0", "qwen-tts",
            "soundfile", "librosa", "einops", "onnxruntime", "sox",
        ],
        env_log,
    )
    marker.write_text("ok\n", encoding="utf-8")
    qlog("vLLM-Omni + Qwen3-TTS virtualenvs ready")
    return VENV_DIR

def _cleanup_hf_temp():
    for p in ("/root/.cache/huggingface/xet", "/root/.cache/huggingface/hub"):
        shutil.rmtree(p, ignore_errors=True)
    for mp in MODELS_ROOT.glob("*/.cache"):
        shutil.rmtree(mp, ignore_errors=True)

def _ensure_default_ref_wav():
    if DEFAULT_REF_WAV.is_file() and DEFAULT_REF_WAV.stat().st_size > 1024:
        return
    drive_copy = DRIVE_VOICES_DIR / "default_ref.wav"
    if drive_copy.is_file() and drive_copy.stat().st_size > 1024:
        shutil.copyfile(drive_copy, DEFAULT_REF_WAV)
        return
    for ref_url in (
        "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-TTS-Repo/clone.wav",
        "https://raw.githubusercontent.com/vllm-project/vllm-omni/main/tests/assets/indextts2/ref_audio.wav",
    ):
        try:
            urllib.request.urlretrieve(ref_url, str(DEFAULT_REF_WAV))
            if DEFAULT_REF_WAV.is_file() and DEFAULT_REF_WAV.stat().st_size > 1024:
                break
        except Exception:
            pass
    if not DEFAULT_REF_WAV.is_file() or DEFAULT_REF_WAV.stat().st_size <= 1024:
        import math, struct, wave
        sr = 24000
        n = sr * 3
        with wave.open(str(DEFAULT_REF_WAV), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            samples = [int(12000 * math.sin(2 * math.pi * 220 * i / sr)) for i in range(n)]
            wf.writeframes(struct.pack(f"<{n}h", *samples))
    if not drive_copy.is_file():
        shutil.copyfile(DEFAULT_REF_WAV, drive_copy)

def prepare_model():
    dl_log = QT_WORK / "model-download.log"
    _ensure_default_ref_wav()
    repos_to_fetch = [MODEL_REPO]
    if PRELOAD_ALL == "1":
        for extra_repo in (
            "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
            "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        ):
            if extra_repo not in repos_to_fetch:
                repos_to_fetch.append(extra_repo)

    def _is_repo_ready(r_id: str) -> bool:
        d = MODELS_ROOT / r_id.split("/")[-1]
        return (
            (d / ".ct_model_ready").is_file()
            and (d / "config.json").is_file()
            and (d / "model.safetensors").is_file()
            and (d / "speech_tokenizer" / "model.safetensors").is_file()
        )

    missing_repos = [r for r in repos_to_fetch if not _is_repo_ready(r)]
    if not missing_repos:
        qlog(f"Reusing local SSD Qwen3-TTS models in {MODELS_ROOT}: {[r.split('/')[-1] for r in repos_to_fetch]}")
        return MODEL_DIR

    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "huggingface_hub", "hf_xet"], check=True)
    _cleanup_hf_temp()
    free_gb = shutil.disk_usage("/content").free / 1e9
    qlog(f"Downloading {missing_repos} -> local SSD {MODELS_ROOT} (free SSD: {free_gb:.1f} GB; log: {dl_log})...")
    dl_env = {**os.environ, "HF_XET_CHUNK_CACHE_SIZE_BYTES": "0", "HF_HUB_ENABLE_HF_TRANSFER": "0"}
    dl_env.pop("HF_XET_HIGH_PERFORMANCE", None)
    dl_script = r"""
import shutil, sys
from pathlib import Path
from huggingface_hub import snapshot_download

models_root = Path(sys.argv[1])
repos = sys.argv[2:]

def clean():
    for p in ("/root/.cache/huggingface/xet", "/root/.cache/huggingface/hub"):
        shutil.rmtree(p, ignore_errors=True)
    for mp in models_root.glob("*/.cache"):
        shutil.rmtree(mp, ignore_errors=True)

for idx, repo_id in enumerate(repos, 1):
    target_dir = models_root / repo_id.split("/")[-1]
    target_dir.mkdir(parents=True, exist_ok=True)
    print(f"[dl] {idx}/{len(repos)} Downloading {repo_id} -> {target_dir}", flush=True)
    snapshot_download(repo_id=repo_id, local_dir=str(target_dir), max_workers=4)
    clean()
    if (target_dir / "model.safetensors").is_file() and (target_dir / "speech_tokenizer" / "model.safetensors").is_file():
        (target_dir / ".ct_model_ready").write_text(repo_id + "\n", encoding="utf-8")
"""
    try:
        run_logged([sys.executable, "-c", dl_script, str(MODELS_ROOT), *missing_repos], dl_log, env=dl_env)
    finally:
        _cleanup_hf_temp()

    if not _is_repo_ready(MODEL_REPO):
        raise RuntimeError(f"Primary model download incomplete in {MODEL_DIR}")
    total_gb = sum(p.stat().st_size for p in MODELS_ROOT.rglob("*") if p.is_file()) / 1e9
    qlog(f"Qwen3-TTS models ready on local SSD: {MODELS_ROOT} ({total_gb:.2f} GB total)")
    return MODEL_DIR

with ThreadPoolExecutor(max_workers=2) as pool:
    f_env = pool.submit(prepare_env)
    f_mod = pool.submit(prepare_model)
    while not (f_env.done() and f_mod.done()):
        for fut in (f_env, f_mod):
            if fut.done() and fut.exception():
                raise fut.exception()
        free_gb = shutil.disk_usage("/content").free / 1e9
        qlog(f"Setup heartbeat | env={'done' if f_env.done() else 'running'} "
             f"model={'done' if f_mod.done() else 'running'} | free_ssd={free_gb:.1f} GB")
        time.sleep(30)
    VENV_DIR = f_env.result()
    MODEL_DIR = f_mod.result()

qlog(f"Setup complete: venv={VENV_DIR} model={MODEL_DIR} drive_data={DATA_DIR}")
'''

QWEN3_TTS_SERVICE = r'''# @title 2. Service -- vLLM-Omni Qwen3-TTS on 127.0.0.1:8000 + Aux Multi-Task Worker on :8001 + Bearer Auth
DEPLOY_YAML = QT_WORK / "qwen3_tts.yaml"
DEPLOY_YAML.write_text(f"""pipeline: qwen3_tts
model_runner: v2
async_chunk: true

connectors:
  connector_of_shared_memory:
    name: SharedMemoryConnector
    extra:
      talker_first_audio: true
      codec_streaming: true
      connector_get_sleep_s: 0.01
      connector_get_max_wait_first_chunk: 3000
      connector_get_max_wait: 300
      codec_chunk_frames: 25
      codec_left_context_frames: 72
      initial_codec_chunk_frames: 1
      decode_batch_max_size: 4

stages:
  - stage_id: 0
    devices: "0"
    max_num_seqs: {MAX_NUM_SEQS}
    gpu_memory_utilization: {STAGE0_GPU_MEM}
    trust_remote_code: true
    enable_prefix_caching: false
    async_scheduling: true
    max_num_batched_tokens: 512
    max_model_len: {MAX_MODEL_LEN}
    output_connectors:
      to_stage_1: connector_of_shared_memory
    default_sampling_params:
      temperature: 0.9
      top_k: 50
      max_tokens: {MAX_MODEL_LEN}
      min_tokens: 2
      repetition_penalty: 1.05
    silence_ban_frames: 0
    subtalker_sampling_params:
      do_sample: true
      temperature: 0.9
      top_k: 50
      top_p: 1.0

  - stage_id: 1
    devices: "0"
    dtype: bfloat16
    max_num_seqs: {MAX_NUM_SEQS}
    gpu_memory_utilization: {STAGE1_GPU_MEM}
    enforce_eager: false
    trust_remote_code: true
    enable_prefix_caching: false
    async_scheduling: true
    max_num_batched_tokens: 65536
    max_model_len: 65536
    input_connectors:
      from_stage_0: connector_of_shared_memory
    default_sampling_params:
      temperature: 0.0
      top_p: 1.0
      top_k: -1
      max_tokens: 65536
      repetition_penalty: 1.0
""", encoding="utf-8")

# Auxiliary multi-task worker on 127.0.0.1:8001 so CustomVoice, VoiceDesign, and Base (Voice Clone)
# all work seamlessly regardless of which checkpoint is loaded as the primary vLLM-Omni engine.
(QT_WORK / "ct_qwen3_aux_worker.py").write_text(r"""from __future__ import annotations

import base64
import gc
import io
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import torch
from huggingface_hub import snapshot_download
from qwen_tts import Qwen3TTSModel

MODELS_ROOT = Path(os.environ.get("CT_MODELS_ROOT", "/content/qwen3-tts/model"))
AUX_PORT = int(os.environ.get("CT_QWEN3_TTS_AUX_PORT", "8001"))

TASK_REPOS = {
    "CustomVoice": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "VoiceDesign": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "Base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
}

_LOCK = threading.Lock()
_LOADED_TASK: str | None = None
_LOADED_MODEL: Qwen3TTSModel | None = None


def _get_or_load_model(task_type: str) -> Qwen3TTSModel:
    global _LOADED_TASK, _LOADED_MODEL
    if _LOADED_TASK == task_type and _LOADED_MODEL is not None:
        return _LOADED_MODEL
    if _LOADED_MODEL is not None:
        del _LOADED_MODEL
        _LOADED_MODEL = None
        _LOADED_TASK = None
        gc.collect()
        torch.cuda.empty_cache()

    repo_id = TASK_REPOS.get(task_type, TASK_REPOS["Base"])
    local_dir = MODELS_ROOT / repo_id.split("/")[-1]
    if not (local_dir / "model.safetensors").is_file():
        local_dir.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=repo_id, local_dir=str(local_dir), max_workers=4)

    _LOADED_MODEL = Qwen3TTSModel.from_pretrained(
        str(local_dir),
        device_map="cuda:0",
        dtype=torch.bfloat16,
    )
    _LOADED_TASK = task_type
    return _LOADED_MODEL


def _encode_audio(wav: np.ndarray, sr: int, fmt: str = "wav", speed: float = 1.0) -> tuple[bytes, int]:
    wav_f32 = np.asarray(wav, dtype=np.float32).reshape(-1)
    buf = io.BytesIO()
    sf.write(buf, wav_f32, sr, format="WAV", subtype="PCM_16")
    raw_wav = buf.getvalue()
    fmt = (fmt or "wav").lower()
    if abs(speed - 1.0) < 1e-3 and fmt == "wav":
        return raw_wav, sr

    af_filters = []
    rem = max(0.25, min(4.0, float(speed)))
    while rem > 2.0 + 1e-3:
        af_filters.append("atempo=2.0")
        rem /= 2.0
    while rem < 0.5 - 1e-3:
        af_filters.append("atempo=0.5")
        rem /= 0.5
    if abs(rem - 1.0) >= 1e-3:
        af_filters.append(f"atempo={rem:.4f}")

    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0"]
    if af_filters:
        cmd += ["-filter:a", ",".join(af_filters)]
    if fmt == "mp3":
        cmd += ["-f", "mp3", "pipe:1"]
    elif fmt == "flac":
        cmd += ["-f", "flac", "pipe:1"]
    elif fmt == "opus":
        cmd += ["-c:a", "libopus", "-f", "ogg", "pipe:1"]
    elif fmt == "pcm":
        cmd += ["-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    else:
        cmd += ["-f", "wav", "-acodec", "pcm_s16le", "pipe:1"]
    res = subprocess.run(cmd, input=raw_wav, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if res.returncode == 0 and res.stdout:
        return res.stdout, sr
    return raw_wav, sr


class AuxHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "loaded_task": _LOADED_TASK})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/synthesize":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw)
            task_type = str(req.get("task_type") or "Base")
            text = str(req.get("input") or req.get("text") or "").strip()
            language = str(req.get("language") or "Auto")
            instruct = str(req.get("instructions") or req.get("instruct") or "").strip()
            speaker = str(req.get("voice") or req.get("speaker") or "vivian").strip()
            ref_audio = req.get("ref_audio")
            if isinstance(ref_audio, str) and ref_audio.startswith("file://"):
                ref_audio = ref_audio[len("file://"):]
            ref_text = req.get("ref_text")
            x_vec_only = bool(req.get("x_vector_only_mode", not bool(ref_text and str(ref_text).strip())))
            fmt = str(req.get("response_format") or "wav").lower()
            speed = float(req.get("speed") or 1.0)
            max_new_tokens = int(req.get("max_new_tokens") or 2048)

            with _LOCK:
                model = _get_or_load_model(task_type)
                if task_type == "VoiceDesign":
                    wavs, sr = model.generate_voice_design(
                        text=text,
                        language=language,
                        instruct=instruct or "Natural, expressive human voice.",
                        max_new_tokens=max_new_tokens,
                    )
                elif task_type == "CustomVoice":
                    wavs, sr = model.generate_custom_voice(
                        text=text,
                        language=language,
                        speaker=speaker,
                        instruct=instruct,
                        max_new_tokens=max_new_tokens,
                    )
                else:
                    wavs, sr = model.generate_voice_clone(
                        text=text,
                        language=language,
                        ref_audio=ref_audio,
                        ref_text=ref_text if not x_vec_only else None,
                        x_vector_only_mode=x_vec_only,
                        max_new_tokens=max_new_tokens,
                    )
            audio_bytes, out_sr = _encode_audio(wavs[0], int(sr), fmt=fmt, speed=speed)
            mime_map = {
                "wav": "audio/wav",
                "mp3": "audio/mpeg",
                "flac": "audio/flac",
                "opus": "audio/ogg",
                "pcm": "audio/pcm",
            }
            self.send_response(200)
            self.send_header("Content-Type", mime_map.get(fmt, "audio/wav"))
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.send_header("X-Audio-Sample-Rate", str(out_sr))
            self.send_header("X-Qwen3-TTS-Engine", f"aux-{task_type}")
            self.end_headers()
            self.wfile.write(audio_bytes)
        except Exception as e:
            import traceback
            self._send_json(500, {"error": {"message": str(e), "traceback": traceback.format_exc()[-800:]}})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", AUX_PORT), AuxHandler)
    print(f"[aux-worker] listening on 127.0.0.1:{AUX_PORT}", flush=True)
    srv.serve_forever()
""", encoding="utf-8")

(QT_WORK / "ct_qwen3_tts_middleware.py").write_text(r"""from __future__ import annotations

import asyncio
import base64
import glob as glob_mod
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_TOKEN = os.environ.get("CT_API_TOKEN", "")
SERVICE_PORT = int(os.environ.get("CT_SERVICE_PORT", "8000"))
AUX_PORT = int(os.environ.get("CT_QWEN3_TTS_AUX_PORT", "8001"))
DATA_DIR = Path(os.environ.get("CT_DATA_DIR", "/content/drive/MyDrive/colab-llm/qwen3-tts"))
SPEAKERS_DIR = Path(os.environ.get("SPEAKER_SAMPLES_DIR", "/content/qwen3-tts/speakers"))
DEFAULT_REF_WAV = Path(os.environ.get("CT_DEFAULT_REF_WAV", "/content/qwen3-tts/default_ref.wav"))
DEFAULT_REF_TEXT = os.environ.get(
    "CT_DEFAULT_REF_TEXT",
    "Okay. Yeah. I resent you. I love you. I respect you. But you know what? You blew it! And thanks to you.",
)
PRIMARY_MODEL_REPO = os.environ.get("CT_QWEN3_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
DEFAULT_SPEAKER = os.environ.get("CT_QWEN3_DEFAULT_SPEAKER", "vivian").strip().lower()

DRIVE_ROOT = Path("/content/drive")
MYDRIVE_ROOT = Path("/content/drive/MyDrive")
DRIVE_VOICES_DIR = DATA_DIR / "voices"
DRIVE_OUTPUT_DIR = DATA_DIR / "outputs"
CUSTOM_VOICES_META_FILE = SPEAKERS_DIR / "custom_voices_meta.json"

AUDIO_EXTS = (
    ".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac", ".opus", ".webm", ".mp4", ".mkv", ".wma", ".mpga", ".mpeg",
)

BUILTIN_SPEAKERS = {
    "vivian": {"description": "Bright, slightly edgy young female voice", "native_language": "Chinese"},
    "serena": {"description": "Warm, gentle young female voice", "native_language": "Chinese"},
    "uncle_fu": {"description": "Seasoned male voice with a low, mellow timbre", "native_language": "Chinese"},
    "dylan": {"description": "Youthful Beijing male voice with a clear, natural timbre", "native_language": "Chinese (Beijing Dialect)"},
    "eric": {"description": "Lively Chengdu male voice with a slightly husky brightness", "native_language": "Chinese (Sichuan Dialect)"},
    "ryan": {"description": "Dynamic male voice with strong rhythmic drive", "native_language": "English"},
    "aiden": {"description": "Sunny American male voice with a clear midrange", "native_language": "English"},
    "ono_anna": {"description": "Playful Japanese female voice with a light, nimble timbre", "native_language": "Japanese"},
    "sohee": {"description": "Warm Korean female voice with rich emotion", "native_language": "Korean"},
}

LANG_MAP = {
    "auto": "Auto",
    "zhen": "Auto",
    "zh": "Chinese",
    "cn": "Chinese",
    "chinese": "Chinese",
    "mandarin": "Chinese",
    "en": "English",
    "english": "English",
    "ja": "Japanese",
    "jp": "Japanese",
    "japanese": "Japanese",
    "ko": "Korean",
    "kr": "Korean",
    "korean": "Korean",
    "de": "German",
    "german": "German",
    "fr": "French",
    "french": "French",
    "ru": "Russian",
    "russian": "Russian",
    "pt": "Portuguese",
    "portuguese": "Portuguese",
    "es": "Spanish",
    "spanish": "Spanish",
    "it": "Italian",
    "italian": "Italian",
}


def _detect_primary_task_type() -> str:
    low = PRIMARY_MODEL_REPO.lower()
    if "voicedesign" in low or "voice-design" in low or "voice_design" in low:
        return "VoiceDesign"
    if "base" in low:
        return "Base"
    return "CustomVoice"


PRIMARY_TASK_TYPE = _detect_primary_task_type()


def _verify_auth(headers: Headers) -> bool:
    if not API_TOKEN:
        return True
    auth = headers.get("authorization") or ""
    if auth == f"Bearer {API_TOKEN}":
        return True
    xkey = headers.get("x-api-key") or ""
    if xkey == API_TOKEN:
        return True
    return False


def _load_voices_meta() -> dict[str, dict[str, Any]]:
    for p in (CUSTOM_VOICES_META_FILE, DRIVE_VOICES_DIR / "custom_voices_meta.json"):
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def _save_voices_meta(meta: dict[str, dict[str, Any]]) -> None:
    try:
        SPEAKERS_DIR.mkdir(parents=True, exist_ok=True)
        DRIVE_VOICES_DIR.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(meta, indent=2, ensure_ascii=False)
        CUSTOM_VOICES_META_FILE.write_text(raw, encoding="utf-8")
        (DRIVE_VOICES_DIR / "custom_voices_meta.json").write_text(raw, encoding="utf-8")
    except Exception:
        pass


def _resolve_drive_path(raw_spec: str) -> Path:
    spec = (raw_spec or "").strip()
    for prefix in ("drive://", "gdrive://", "file://"):
        if spec.startswith(prefix):
            spec = spec[len(prefix):]
    spec = spec.lstrip("/")
    if not spec:
        return DRIVE_VOICES_DIR if DRIVE_VOICES_DIR.is_dir() else MYDRIVE_ROOT

    candidates = []
    if spec.startswith("content/drive/"):
        candidates.append(Path("/") / spec)
    elif spec.startswith("drive/"):
        candidates.append(Path("/content") / spec)
    elif spec.startswith("MyDrive/"):
        candidates.append(DRIVE_ROOT / spec)
    elif spec == "MyDrive":
        candidates.append(MYDRIVE_ROOT)
    else:
        candidates.append(DRIVE_VOICES_DIR / spec)
        candidates.append(DATA_DIR / spec)
        candidates.append(MYDRIVE_ROOT / spec)
        candidates.append(Path("/") / spec)

    for c in candidates:
        if c.exists():
            return c.resolve()
    return candidates[0].resolve()


def _rel_to_mydrive(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(MYDRIVE_ROOT.resolve()))
    except Exception:
        return str(p)


SKIP_DRIVE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "aux-venv", "custom_nodes",
    "models", ".runtimes", "Takeout", ".Trash", ".ipynb_checkpoints",
    "flashinfer-cache", "triton-cache", "vllm-cache",
}


def _list_drive_audio_files(spec: str, recursive: bool = True) -> list[dict[str, Any]]:
    raw = (spec or "").strip()
    for prefix in ("drive://", "gdrive://", "file://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]

    has_glob = any(ch in raw for ch in ("*", "?", "["))
    matched_paths: list[Path] = []

    if has_glob:
        clean = raw.lstrip("/")
        bases = []
        if clean.startswith("content/drive/"):
            bases.append(Path("/") / clean)
        elif clean.startswith("MyDrive/"):
            bases.append(DRIVE_ROOT / clean)
        else:
            bases.append(DRIVE_VOICES_DIR / clean)
            bases.append(DATA_DIR / clean)
            bases.append(MYDRIVE_ROOT / clean)
        seen = set()
        for pattern_path in bases:
            for hit in sorted(glob_mod.glob(str(pattern_path), recursive=True)):
                hp = Path(hit).resolve()
                if hp.is_file() and hp.suffix.lower() in AUDIO_EXTS and str(hp) not in seen:
                    seen.add(str(hp))
                    matched_paths.append(hp)
    else:
        target = _resolve_drive_path(raw)
        if target.is_file():
            matched_paths.append(target)
        elif target.is_dir():
            if not recursive:
                for hp in sorted(target.iterdir()):
                    if hp.is_file() and hp.suffix.lower() in AUDIO_EXTS:
                        matched_paths.append(hp.resolve())
            else:
                for root_dir, dirnames, filenames in os.walk(target):
                    dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DRIVE_DIRS and not d.startswith("."))
                    for fn in sorted(filenames):
                        hp = Path(root_dir) / fn
                        if hp.suffix.lower() in AUDIO_EXTS:
                            matched_paths.append(hp.resolve())

    items = []
    for p in matched_paths:
        st = p.stat()
        items.append({
            "drive_path": str(p),
            "rel_path": _rel_to_mydrive(p),
            "name": p.name,
            "size_bytes": st.st_size,
            "size_mb": round(st.st_size / 1e6, 2),
            "mtime": int(st.st_mtime),
        })
    return items


def _audio_path_to_wav_data_url(audio_path: Path, max_duration_sec: float = 24.0) -> str:
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-t", f"{max_duration_sec:.1f}",
        "-i", str(audio_path),
        "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if res.returncode != 0 or not res.stdout:
        if audio_path.suffix.lower() == ".wav":
            b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
            return f"data:audio/wav;base64,{b64}"
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed decoding {audio_path.name}: {err}")
    b64 = base64.b64encode(res.stdout).decode("ascii")
    return f"data:audio/wav;base64,{b64}"


def _sync_speakers_to_drive() -> None:
    try:
        DRIVE_VOICES_DIR.mkdir(parents=True, exist_ok=True)
        if SPEAKERS_DIR.is_dir():
            for sf in SPEAKERS_DIR.iterdir():
                if sf.is_file():
                    dst = DRIVE_VOICES_DIR / sf.name
                    if not dst.is_file() or sf.stat().st_mtime > dst.stat().st_mtime:
                        shutil.copy2(sf, dst)
    except Exception:
        pass


def _save_output_audio_to_drive(audio_bytes: bytes, spec: str, fmt: str = "wav") -> str:
    DRIVE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ext = "." + (fmt or "wav").lstrip(".")
    if spec and spec not in ("auto", "true", "True", "1"):
        dest = _resolve_drive_path(spec)
        if dest.is_dir() or spec.endswith("/"):
            dest.mkdir(parents=True, exist_ok=True)
            dest = dest / f"qwen3-tts-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.suffix:
                dest = dest.with_suffix(ext)
    else:
        dest = DRIVE_OUTPUT_DIR / f"qwen3-tts-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
    dest.write_bytes(audio_bytes)
    return str(dest)


def _register_custom_voice_bytes(
    name: str,
    wav_bytes: bytes,
    ref_text: str | None = None,
    speaker_desc: str | None = None,
    consent: str = "user-consent",
) -> tuple[int, dict[str, Any]]:
    vname = name.strip().lower()
    SPEAKERS_DIR.mkdir(parents=True, exist_ok=True)
    DRIVE_VOICES_DIR.mkdir(parents=True, exist_ok=True)
    wav_file = SPEAKERS_DIR / f"{vname}.wav"
    wav_file.write_bytes(wav_bytes)
    shutil.copy2(wav_file, DRIVE_VOICES_DIR / f"{vname}.wav")

    meta = _load_voices_meta()
    meta[vname] = {
        "id": vname,
        "wav_path": str(wav_file),
        "drive_wav_path": str(DRIVE_VOICES_DIR / f"{vname}.wav"),
        "ref_text": ref_text,
        "speaker_description": speaker_desc or "Custom cloned voice for Qwen3-TTS",
        "file_size": len(wav_bytes),
        "mime_type": "audio/wav",
        "created_at": int(time.time()),
    }
    _save_voices_meta(meta)

    # Also register in vLLM-Omni's /v1/audio/voices
    boundary = f"----ctvoice{int(time.time() * 1000)}"
    parts: list[bytes] = []

    def add_field(k: str, v: str):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
        )

    add_field("name", vname)
    add_field("consent", consent)
    if ref_text:
        add_field("ref_text", str(ref_text))
    if speaker_desc:
        add_field("speaker_description", str(speaker_desc))

    parts.append(
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"audio_sample\"; filename=\"{vname}.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
        + wav_bytes
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    body_bytes = b"".join(parts)
    req = urllib.request.Request(
        f"http://127.0.0.1:{SERVICE_PORT}/v1/audio/voices",
        data=body_bytes,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {API_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode("utf-8", errors="replace"))
            _sync_speakers_to_drive()
            return resp.status, out
    except Exception:
        return 200, {"voice": meta[vname]}


def _upload_drive_voice_sync(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    name = str(body.get("name") or "").strip().lower()
    raw_path = str(body.get("drive_path") or body.get("audio") or body.get("path") or "").strip()
    audio_b64 = str(body.get("audio_base64") or "").strip()
    consent = str(body.get("consent") or "user-consent").strip()
    ref_text = body.get("ref_text")
    speaker_desc = body.get("speaker_description")
    if not name or (not raw_path and not audio_b64):
        return 400, {"error": "'name' and either 'drive_path' or 'audio_base64' are required"}

    if audio_b64:
        if "," in audio_b64 and audio_b64.startswith("data:"):
            audio_b64 = audio_b64.split(",", 1)[1]
        raw_bytes = base64.b64decode(audio_b64)
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error", "-t", "24.0",
            "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
        ]
        res = subprocess.run(cmd, input=raw_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        wav_bytes = res.stdout if (res.returncode == 0 and res.stdout) else raw_bytes
    else:
        ap = _resolve_drive_path(raw_path)
        if not ap.is_file():
            return 404, {"error": f"Audio file not found on Google Drive: {raw_path} (resolved to {ap})"}
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error", "-t", "24.0",
            "-i", str(ap), "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        wav_bytes = res.stdout if (res.returncode == 0 and res.stdout) else ap.read_bytes()

    return _register_custom_voice_bytes(name, wav_bytes, ref_text=ref_text, speaker_desc=speaker_desc, consent=consent)


def _prepare_speech_request(raw_body: bytes) -> tuple[bytes, dict[str, Any], str, str | None]:
    payload = json.loads(raw_body) if raw_body else {}
    if not isinstance(payload, dict):
        payload = {}

    save_drive_spec: str | None = None
    extras = payload.get("extra_params")
    if not isinstance(extras, dict):
        extras = {}

    for save_key in ("save_to_drive", "save_drive_path"):
        val = payload.pop(save_key, None)
        if val is None and save_key in extras:
            val = extras.pop(save_key, None)
        if val:
            save_drive_spec = str(val) if not isinstance(val, bool) else "auto"

    # Support top-level or extra_params shortcuts
    for k in ("language", "lang", "task_type", "instructions", "instruct", "ref_audio", "ref_text", "x_vector_only_mode"):
        if k in extras and k not in payload:
            payload[k] = extras.pop(k)

    if "instruct" in payload and not payload.get("instructions"):
        payload["instructions"] = payload.pop("instruct")
    else:
        payload.pop("instruct", None)

    raw_lang = str(payload.pop("lang", None) or payload.get("language") or "Auto").strip()
    payload["language"] = LANG_MAP.get(raw_lang.lower(), raw_lang.title() if raw_lang else "Auto")

    # Resolve ref_audio if present
    ref_audio = payload.get("ref_audio")
    if isinstance(ref_audio, str) and ref_audio.strip():
        ra = ref_audio.strip()
        if ra.startswith(("drive://", "gdrive://", "/content/")) or not ra.startswith(("http://", "https://", "data:", "file://")):
            ap = _resolve_drive_path(ra)
            payload["ref_audio"] = _audio_path_to_wav_data_url(ap)

    # Check if voice refers to a custom uploaded voice
    meta = _load_voices_meta()
    raw_voice = str(payload.get("voice") or payload.get("speaker") or "").strip().lower()
    payload.pop("speaker", None)
    is_custom_uploaded_voice = bool(raw_voice and raw_voice not in BUILTIN_SPEAKERS and raw_voice not in ("default", "alloy"))
    if raw_voice in meta and not payload.get("ref_audio"):
        vinfo = meta[raw_voice]
        wpath = Path(vinfo.get("wav_path") or "")
        if not wpath.is_file():
            wpath = Path(vinfo.get("drive_wav_path") or "")
        if wpath.is_file():
            payload["ref_audio"] = f"file://{wpath}"
            if vinfo.get("ref_text") and not payload.get("ref_text"):
                payload["ref_text"] = vinfo["ref_text"]
            is_custom_uploaded_voice = True

    # Determine effective task_type: CustomVoice, VoiceDesign, or Base
    req_task = str(payload.get("task_type") or "").strip()
    if req_task.lower() in ("voicedesign", "voice_design", "voice-design"):
        task_type = "VoiceDesign"
    elif req_task.lower() in ("base", "voiceclone", "voice_clone", "voice-clone") or payload.get("ref_audio") or is_custom_uploaded_voice:
        task_type = "Base"
    elif req_task.lower() in ("customvoice", "custom_voice", "custom-voice"):
        task_type = "CustomVoice"
    else:
        task_type = PRIMARY_TASK_TYPE

    payload["task_type"] = task_type

    if task_type == "CustomVoice":
        if not raw_voice or raw_voice in ("default", "alloy"):
            raw_voice = "ryan" if payload.get("language") == "English" else DEFAULT_SPEAKER
        payload["voice"] = raw_voice
        payload.pop("ref_audio", None)
        payload.pop("ref_text", None)
        payload.pop("x_vector_only_mode", None)
    elif task_type == "VoiceDesign":
        payload.pop("voice", None)
        payload.pop("ref_audio", None)
        payload.pop("ref_text", None)
        payload.pop("x_vector_only_mode", None)
        if not payload.get("instructions"):
            payload["instructions"] = "Natural, expressive human voice."
    elif task_type == "Base":
        if not payload.get("ref_audio") and (not raw_voice or raw_voice in ("default", "alloy") or raw_voice in BUILTIN_SPEAKERS):
            payload["ref_audio"] = f"file://{DEFAULT_REF_WAV}"
            if not payload.get("ref_text") and payload.get("x_vector_only_mode") is None:
                payload["ref_text"] = DEFAULT_REF_TEXT
            payload.pop("voice", None)
        elif payload.get("ref_audio"):
            payload.pop("voice", None)
        if payload.get("x_vector_only_mode") is None:
            payload["x_vector_only_mode"] = not bool(payload.get("ref_text") and str(payload.get("ref_text")).strip())

    if extras:
        payload["extra_params"] = extras
    else:
        payload.pop("extra_params", None)

    return json.dumps(payload, ensure_ascii=False).encode("utf-8"), payload, task_type, save_drive_spec


def _call_aux_worker_sync(payload: dict[str, Any]) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{AUX_PORT}/synthesize",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            headers = {
                "content-type": resp.headers.get("Content-Type") or "audio/wav",
                "x-audio-sample-rate": resp.headers.get("X-Audio-Sample-Rate") or "24000",
                "x-qwen3-tts-engine": resp.headers.get("X-Qwen3-TTS-Engine") or "aux",
            }
            return resp.status, resp.read(), headers
    except urllib.error.HTTPError as err:
        return err.code, err.read(), {"content-type": "application/json"}


def _design_and_register_voice_sync(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    instruct = str(body.get("instructions") or body.get("instruct") or "").strip()
    sample_text = str(body.get("sample_text") or body.get("text") or body.get("input") or "").strip()
    language = LANG_MAP.get(str(body.get("language") or body.get("lang") or "Auto").strip().lower(), "Auto")
    voice_name = str(body.get("name") or "").strip().lower()
    if not instruct:
        return 400, {"error": "'instructions' (voice persona description) is required for Voice Design"}
    if not sample_text:
        sample_text = (
            "Hello! This is a sample of my designed voice, ready for speech synthesis."
            if language == "English"
            else "你好！这是根据你的描述生成的专属定制音色示例。"
        )

    status, audio_bytes, headers = _call_aux_worker_sync({
        "task_type": "VoiceDesign",
        "input": sample_text,
        "language": language,
        "instructions": instruct,
        "response_format": "wav",
    })
    if status != 200:
        try:
            return status, json.loads(audio_bytes.decode("utf-8", errors="replace"))
        except Exception:
            return status, {"error": audio_bytes.decode("utf-8", errors="replace")}

    result: dict[str, Any] = {
        "status": "ok",
        "task_type": "VoiceDesign",
        "instructions": instruct,
        "sample_text": sample_text,
        "language": language,
        "sample_rate": int(headers.get("x-audio-sample-rate") or 24000),
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
    }
    if voice_name:
        reg_code, reg_out = _register_custom_voice_bytes(
            voice_name,
            audio_bytes,
            ref_text=sample_text,
            speaker_desc=f"VoiceDesign: {instruct}",
            consent="voice-design",
        )
        result["registered_voice"] = voice_name
        result["registration"] = reg_out
    if body.get("save_to_drive"):
        result["saved_drive_path"] = _save_output_audio_to_drive(
            audio_bytes, str(body.get("save_to_drive")), "wav"
        )
    return 200, result


class Qwen3TTSMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        root_path = scope.get("root_path", "")
        path = scope["path"].removeprefix(root_path)
        headers = Headers(scope=scope)
        client = scope.get("client")
        client_host = (client[0] if client else "") or ""

        if path == "/health" and client_host in ("127.0.0.1", "::1", "localhost") and not headers.get("authorization"):
            await self.app(scope, receive, send)
            return

        if not _verify_auth(headers):
            resp = JSONResponse(
                {"error": {"message": "Unauthorized: invalid or missing Bearer token", "type": "AuthenticationError", "code": 401}},
                status_code=HTTPStatus.UNAUTHORIZED,
            )
            await resp(scope, receive, send)
            return

        if scope["type"] == "http":
            method = scope.get("method", "GET")

            if method == "GET" and path == "/api/ct/info":
                meta = _load_voices_meta()
                resp = JSONResponse({
                    "service": "qwen3-tts",
                    "primary_model": PRIMARY_MODEL_REPO,
                    "primary_task_type": PRIMARY_TASK_TYPE,
                    "supported_tasks": ["CustomVoice", "VoiceDesign", "Base"],
                    "builtin_speakers": BUILTIN_SPEAKERS,
                    "custom_voices": list(meta.keys()),
                    "languages": sorted(set(LANG_MAP.values())),
                    "default_speaker": DEFAULT_SPEAKER,
                    "drive_voices_dir": str(DRIVE_VOICES_DIR),
                    "drive_output_dir": str(DRIVE_OUTPUT_DIR),
                })
                await resp(scope, receive, send)
                return

            if method == "GET" and path == "/api/ct/logs":
                from urllib.parse import parse_qs
                qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="replace"))
                n_lines = max(1, min(int((qs.get("lines") or ["120"])[0]), 2000))
                log_file = Path("/content/ct/service.log")
                lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-n_lines:] if log_file.is_file() else []
                resp = JSONResponse({"lines": len(lines), "log": "\n".join(lines)})
                await resp(scope, receive, send)
                return

            if method == "GET" and path == "/api/ct/drive-list":
                from urllib.parse import parse_qs
                qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="replace"))
                spec = (qs.get("path") or [""])[0]
                recursive = (qs.get("recursive") or ["1"])[0] not in ("0", "false", "False")
                try:
                    files = await asyncio.to_thread(_list_drive_audio_files, spec, recursive)
                    resp = JSONResponse({
                        "drive_mounted": MYDRIVE_ROOT.is_dir(),
                        "query": spec,
                        "count": len(files),
                        "files": files,
                    })
                except Exception as e:
                    resp = JSONResponse({"error": str(e)}, status_code=400)
                await resp(scope, receive, send)
                return

            if method == "POST" and path == "/api/ct/drive-upload-voice":
                chunks: list[bytes] = []
                while True:
                    msg = await receive()
                    if msg["type"] == "http.request":
                        chunks.append(msg.get("body", b""))
                        if not msg.get("more_body", False):
                            break
                    elif msg["type"] == "http.disconnect":
                        return
                try:
                    body = json.loads(b"".join(chunks))
                    code, out = await asyncio.to_thread(_upload_drive_voice_sync, body)
                    resp = JSONResponse(out, status_code=code)
                except Exception as e:
                    resp = JSONResponse({"error": str(e)}, status_code=500)
                await resp(scope, receive, send)
                return

            if method == "POST" and path == "/api/ct/design-voice":
                chunks = []
                while True:
                    msg = await receive()
                    if msg["type"] == "http.request":
                        chunks.append(msg.get("body", b""))
                        if not msg.get("more_body", False):
                            break
                    elif msg["type"] == "http.disconnect":
                        return
                try:
                    body = json.loads(b"".join(chunks))
                    code, out = await asyncio.to_thread(_design_and_register_voice_sync, body)
                    resp = JSONResponse(out, status_code=code)
                except Exception as e:
                    resp = JSONResponse({"error": str(e)}, status_code=500)
                await resp(scope, receive, send)
                return

            if method == "GET" and path == "/v1/audio/voices":
                meta = _load_voices_meta()
                all_voices = sorted(set(BUILTIN_SPEAKERS.keys()) | set(meta.keys()))
                resp = JSONResponse({
                    "voices": all_voices,
                    "builtin_speakers": BUILTIN_SPEAKERS,
                    "uploaded_voices": list(meta.values()),
                })
                await resp(scope, receive, send)
                return

            if method == "POST" and path == "/v1/audio/speech":
                chunks = []
                while True:
                    msg = await receive()
                    if msg["type"] == "http.request":
                        chunks.append(msg.get("body", b""))
                        if not msg.get("more_body", False):
                            break
                    elif msg["type"] == "http.disconnect":
                        return
                raw_body = b"".join(chunks)
                try:
                    rewritten_body, payload_dict, task_type, save_drive_spec = await asyncio.to_thread(
                        _prepare_speech_request, raw_body
                    )
                except Exception as e:
                    resp = JSONResponse(
                        {"error": {"message": str(e), "type": "BadRequestError", "code": 400}},
                        status_code=400,
                    )
                    await resp(scope, receive, send)
                    return

                header_save = headers.get("x-save-drive")
                if header_save and not save_drive_spec:
                    save_drive_spec = header_save

                # If the request's task_type differs from the primary vLLM-Omni checkpoint,
                # route transparently to the auxiliary GPU worker on 127.0.0.1:8001.
                if task_type != PRIMARY_TASK_TYPE:
                    status, audio_bytes, aux_headers = await asyncio.to_thread(_call_aux_worker_sync, payload_dict)
                    if status == 200 and save_drive_spec:
                        saved_path = await asyncio.to_thread(
                            _save_output_audio_to_drive,
                            audio_bytes,
                            save_drive_spec,
                            str(payload_dict.get("response_format") or "wav"),
                        )
                        aux_headers["x-saved-drive-path"] = saved_path
                    resp = Response(content=audio_bytes, status_code=status, headers=aux_headers)
                    await resp(scope, receive, send)
                    return

                body_sent = False

                async def replay_receive() -> Message:
                    nonlocal body_sent
                    if not body_sent:
                        body_sent = True
                        return {"type": "http.request", "body": rewritten_body, "more_body": False}
                    return await receive()

                if not save_drive_spec:
                    await self.app(scope, replay_receive, send)
                    return

                resp_status = 200
                resp_headers: list[tuple[bytes, bytes]] = []
                resp_chunks: list[bytes] = []
                start_msg: Message | None = None

                async def tee_send(message: Message) -> None:
                    nonlocal resp_status, resp_headers, start_msg
                    if message["type"] == "http.response.start":
                        resp_status = message.get("status", 200)
                        resp_headers = list(message.get("headers", []))
                        start_msg = message
                        if resp_status != 200:
                            await send(message)
                    elif message["type"] == "http.response.body":
                        chunk = message.get("body", b"")
                        more = message.get("more_body", False)
                        if resp_status != 200:
                            await send(message)
                            return
                        if chunk:
                            resp_chunks.append(chunk)
                        if not more and start_msg is not None:
                            full_audio = b"".join(resp_chunks)
                            saved_path = await asyncio.to_thread(
                                _save_output_audio_to_drive,
                                full_audio,
                                save_drive_spec,
                                str(payload_dict.get("response_format") or "wav"),
                            )
                            new_headers = [
                                (k, v) for (k, v) in resp_headers if k.lower() != b"x-saved-drive-path"
                            ]
                            new_headers.append((b"x-saved-drive-path", saved_path.encode("utf-8")))
                            start_msg["headers"] = new_headers
                            await send(start_msg)
                            await send({"type": "http.response.body", "body": full_audio, "more_body": False})
                    else:
                        await send(message)

                await self.app(scope, replay_receive, tee_send)
                return

            if method in ("POST", "DELETE") and path.startswith("/v1/audio/voices"):
                if method == "DELETE":
                    vname = path.rstrip("/").split("/")[-1].lower()
                    meta = _load_voices_meta()
                    if vname in meta:
                        meta.pop(vname, None)
                        _save_voices_meta(meta)
                        for d in (SPEAKERS_DIR, DRIVE_VOICES_DIR):
                            (d / f"{vname}.wav").unlink(missing_ok=True)
                await self.app(scope, receive, send)
                await asyncio.to_thread(_sync_speakers_to_drive)
                return

        await self.app(scope, receive, send)
""", encoding="utf-8")

VLLM_ENV = {
    "PATH": f"{VENV_DIR}/bin:/usr/local/cuda/bin:{os.environ.get('PATH', '')}",
    "PYTHONPATH": f"{QT_WORK}:{os.environ.get('PYTHONPATH', '')}",
    "VIRTUAL_ENV": str(VENV_DIR),
    "FLASHINFER_DISABLE_VERSION_CHECK": "1",
    "FLASHINFER_WORKSPACE_BASE": str(FLASHINFER_CACHE_DIR),
    "TRITON_CACHE_DIR": str(TRITON_CACHE_DIR),
    "VLLM_CACHE_ROOT": str(VLLM_CACHE_DIR),
    "TORCHINDUCTOR_CACHE_DIR": str(VLLM_CACHE_DIR / "inductor"),
    "SPEAKER_SAMPLES_DIR": str(SPEAKERS_DIR),
    "CT_API_TOKEN": API_TOKEN,
    "CT_SERVICE_PORT": str(SERVICE_PORT),
    "CT_QWEN3_TTS_AUX_PORT": str(AUX_PORT),
    "CT_DATA_DIR": str(DATA_DIR),
    "CT_MODELS_ROOT": str(MODELS_ROOT),
    "CT_DEFAULT_REF_WAV": str(DEFAULT_REF_WAV),
    "CT_DEFAULT_REF_TEXT": DEFAULT_REF_TEXT,
    "CT_QWEN3_TTS_MODEL": MODEL_REPO,
}

SERVER_ARGS = [
    str(VENV_DIR / "bin" / "vllm"), "serve", str(MODEL_DIR),
    "--omni",
    "--served-model-name", MODEL_REPO, MODEL_ALIAS,
    "--host", "127.0.0.1",
    "--port", str(SERVICE_PORT),
    "--api-key", API_TOKEN,
    "--trust-remote-code",
    "--allowed-local-media-path", "/content",
    "--stage-init-timeout", "900",
    "--deploy-config", str(DEPLOY_YAML),
    "--middleware", "ct_qwen3_tts_middleware.Qwen3TTSMiddleware",
]

AUX_ARGS = [
    str(AUX_VENV_DIR / "bin" / "python"),
    str(QT_WORK / "ct_qwen3_aux_worker.py"),
]

def start_aux():
    subprocess.run(["pkill", "-9", "-f", "ct_qwen3_aux_worker.py"], check=False)
    return ct_spawn("aux", AUX_ARGS, env=VLLM_ENV, cwd=str(QT_WORK))

def start_service():
    subprocess.run(["pkill", "-9", "-f", "vllm|EngineCore"], check=False)
    time.sleep(1)
    qlog(f"Starting vLLM-Omni Qwen3-TTS server on 127.0.0.1:{SERVICE_PORT} (model={MODEL_REPO})")
    return ct_spawn("service", SERVER_ARGS, env=VLLM_ENV, cwd=str(QT_WORK))

PROCS["aux"] = (start_aux(), start_aux)
PROCS["service"] = (start_service(), start_service)
'''

QWEN3_TTS_KEEPALIVE = r'''# @title 5. Ready + keep-alive -- wait for 2-stage load, warmup synthesis & sync JIT caches
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"

def http_json(url, key=None, payload=None, timeout=30):
    headers = {"Content-Type": "application/json", "User-Agent": BROWSER_UA}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        url, headers=headers, data=None if payload is None else json.dumps(payload).encode("utf-8")
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {})
    except urllib.error.HTTPError as err:
        return err.code, {}

def auth_check(base, key):
    for candidate, expected in ((None, 401), ("invalid-token-probe", 401), (key, 200)):
        code, _ = http_json(base + "/v1/models", candidate)
        if code != expected:
            raise RuntimeError(f"Auth test failed on {base}/v1/models: expected {expected}, got {code}")

LOCAL_BASE = f"http://127.0.0.1:{SERVICE_PORT}"
deadline = time.monotonic() + 2400
last_notice = 0
while time.monotonic() < deadline:
    proc = PROCS["service"][0]
    if proc.poll() is not None:
        raise RuntimeError("vLLM-Omni exited during startup:\n" + ct_tail("service", 80))
    try:
        code, _ = http_json(LOCAL_BASE + "/health", API_TOKEN, timeout=10)
        if code == 200:
            break
    except Exception:
        pass
    if time.monotonic() - last_notice >= 30:
        gpu_stat = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader"],
            text=True,
        ).strip()
        last_line = (ct_tail("service", 3).strip().splitlines() or ["starting..."])[-1]
        qlog(f"Waiting for 2-stage Qwen3-TTS init | GPU: {gpu_stat} | {last_line[:100]}")
        last_notice = time.monotonic()
    time.sleep(5)
else:
    raise RuntimeError("Timed out waiting for vLLM-Omni server to become ready:\n" + ct_tail("service", 80))

auth_check(LOCAL_BASE, API_TOKEN)

# Warmup synthesis to verify Stage 0 Talker + Stage 1 Code2Wav and sync JIT caches to Drive
warmup_req = urllib.request.Request(
    LOCAL_BASE + "/v1/audio/speech",
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_TOKEN}"},
    data=json.dumps({
        "model": MODEL_REPO,
        "input": "你好，Qwen3 TTS 语音合成服务已经准备就绪。",
        "voice": "vivian",
        "language": "Chinese",
        "response_format": "wav",
    }).encode("utf-8"),
)
try:
    with urllib.request.urlopen(warmup_req, timeout=180) as wresp:
        wav_bytes = wresp.read()
        qlog(f"Warmup synthesis succeeded ({len(wav_bytes)} bytes WAV)")
except Exception as werr:
    qlog(f"Warmup synthesis warning: {werr}")

for _local_c, _drive_c in (
    (FLASHINFER_CACHE_DIR, FLASHINFER_DRIVE_DIR),
    (TRITON_CACHE_DIR, TRITON_DRIVE_DIR),
    (VLLM_CACHE_DIR, VLLM_DRIVE_DIR),
    (SPEAKERS_DIR, DRIVE_VOICES_DIR),
):
    if _local_c.is_dir() and _drive_c.is_dir():
        subprocess.run(["cp", "-ru", f"{_local_c}/.", f"{_drive_c}/"], check=False)

pub_deadline = time.monotonic() + 120
while time.monotonic() < pub_deadline:
    try:
        code, _ = http_json(PUBLIC_URL + "/v1/models", API_TOKEN, timeout=15)
        if code == 200:
            auth_check(PUBLIC_URL, API_TOKEN)
            qlog(f"Public endpoint verified: {PUBLIC_URL}/v1/models")
            break
    except Exception as err:
        qlog(f"Public probe pending ({type(err).__name__}: {err})")
    time.sleep(8)

print(f"{SERVICE} is up: {PUBLIC_URL}")
print("CT_READY", flush=True)

started = time.monotonic()
failures = 0
while True:
    time.sleep(60)
    for name, (proc, restart) in list(PROCS.items()):
        if proc.poll() is not None:
            qlog(f"[watchdog] {name} exited with {proc.returncode}; restarting\n{ct_tail(name, 20)}")
            PROCS[name] = (restart(), restart)
    recent_log = ct_tail("service", 30)
    if "replica-0 is dead" in recent_log:
        qlog("[watchdog] Detected dead stage replica in service.log; restarting vLLM-Omni service")
        PROCS["service"][0].terminate()
        PROCS["service"] = (start_service(), start_service)
        failures = 0
    try:
        code, _ = http_json(LOCAL_BASE + "/health", API_TOKEN, timeout=10)
        failures = 0 if code == 200 else failures + 1
    except Exception:
        failures += 1
    if failures >= 6:
        qlog("[watchdog] 6 failed health checks; restarting vLLM-Omni service")
        PROCS["service"][0].terminate()
        PROCS["service"] = (start_service(), start_service)
        failures = 0
    gpu_mem = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader"],
        text=True,
    ).strip()
    qlog(f"heartbeat | up={int(time.monotonic() - started)}s | GPU: {gpu_mem}")
'''

if __name__ == "__main__":
    write(ROOT / "templates/service.ipynb",
          notebook("my-service (template)", "Template: replace cells 1 and 2 with the real model service.",
                   TEMPLATE_CFG, TEMPLATE_SETUP, TEMPLATE_SERVICE))
    write(ROOT / "services/comfyui.ipynb",
          notebook("ComfyUI + ComfyUI-Manager",
                   "ComfyUI with ComfyUI-Manager enabled behind Caddy auth (basic auth for browser UI, "
                   "Bearer token for API clients). Models live on fast local NVMe SSD (`/content/ComfyUI/models`) "
                   "and are downloaded on demand per workflow (`--workflow <url-or-template>` or `pull-workflow`); "
                   "custom_nodes, user settings, input, and output persist on Google Drive.",
                   COMFYUI_CFG, COMFYUI_SETUP, COMFYUI_SERVICE))
    write(ROOT / "services/ollama.ipynb",
          notebook("Ollama", "Ollama API behind Caddy auth (Ollama has no auth of its own). "
                   "Models: `CT_MODELS` (comma separated, default `qwen2.5:0.5b`).",
                   OLLAMA_CFG, OLLAMA_SETUP, OLLAMA_SERVICE))
    write(ROOT / "services/unsloth-studio.ipynb",
          notebook("Unsloth Studio", "Based on https://github.com/unslothai/unsloth/blob/main/studio/"
                   "Unsloth_Studio_Colab.ipynb -- built-in login, our own cloudflared (quick or fixed domain).",
                   UNSLOTH_CFG, UNSLOTH_SETUP, UNSLOTH_SERVICE, keepalive=UNSLOTH_KEEPALIVE))
    write(ROOT / "services/qwen-flash.ipynb",
          notebook("Qwen3.8-Flash-Next (G4 / 800K / Vision)",
                   "Migrated from `~/legacy/qwen-flash-colab`. Runs `SC117/Qwen3.8-Flash-Next-GSQ-RCO-"
                   "abliterated-GGUF` (IQ3_S, 819200 context with YaRN 3.125x, BF16 vision projector, "
                   "`--load-mode none --lazy-mode off`) on a Colab G4 (RTX PRO 6000 Blackwell sm_120) with "
                   "`llama-server --api-key-file` authentication.",
                   QWEN_FLASH_CFG, QWEN_FLASH_SETUP, QWEN_FLASH_SERVICE, keepalive=QWEN_FLASH_KEEPALIVE))
    write(ROOT / "services/nemotron-transcribe.ipynb",
          notebook("NVIDIA-Nemotron-3.5-Transcribe-30B-A4B (G4 / vLLM + NeMo Speech)",
                   "Serves `nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B` (BF16 / FP8 / NVFP4, 65536 context, "
                   "up to 1-hour audio, 40 languages, MTP8 speculative decoding, CTC word timestamps & speaker "
                   "diarization) on a Colab G4 GPU via `vllm serve` + `nemo_speechlm`, with Bearer token auth "
                   "and direct Google Drive audio batch/single transcription endpoints.",
                   NEMOTRON_TRANSCRIBE_CFG, NEMOTRON_TRANSCRIBE_SETUP, NEMOTRON_TRANSCRIBE_SERVICE,
                   keepalive=NEMOTRON_TRANSCRIBE_KEEPALIVE))
    write(ROOT / "services/indextts.ipynb",
          notebook("IndexTTS-2.5 (L4 / vLLM-Omni 2-Stage Zero-Shot Voice Cloning & Emotion TTS)",
                   "Serves `IndexTeam/IndexTTS-2.5` on a Colab L4 GPU via `vllm serve --omni` (Stage 0 Talker + "
                   "Stage 1 EnhancedCodec/S2Mel DiT/BigVGAN) with Bearer token auth, zero-shot voice cloning, "
                   "reusable uploaded voices (`/v1/audio/voices`), native speed control (0.5-2.0x), multilingual "
                   "synthesis (`zh`, `en`, `zhen`, `ja`, `es`, `ar`, `yue`), emotion control (`emo_vector`, "
                   "`emo_text`, `emo_audio`, `emo_alpha`), and Google Drive voice/output persistence.",
                   INDEXTTS_CFG, INDEXTTS_SETUP, INDEXTTS_SERVICE,
                   keepalive=INDEXTTS_KEEPALIVE))
    write(ROOT / "services/qwen3-tts.ipynb",
          notebook("Qwen3-TTS (L4 / vLLM-Omni 2-Stage CustomVoice, VoiceDesign & 3s Voice Clone)",
                   "Serves `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`, `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign`, and "
                   "`Qwen/Qwen3-TTS-12Hz-1.7B-Base` on a Colab L4 GPU via `vllm serve --omni` (Stage 0 Talker + "
                   "Stage 1 Code2Wav 12Hz Tokenizer) plus an auxiliary multi-task GPU worker, supporting 9 built-in "
                   "premium timbres (`Vivian`, `Serena`, `Uncle_Fu`, `Dylan`, `Eric`, `Ryan`, `Aiden`, `Ono_Anna`, "
                   "`Sohee`), natural-language instruction control, VoiceDesign persona creation, 3-second zero-shot "
                   "voice cloning, Voice-Design-then-Clone workflow, 10 languages, and Google Drive voice/output persistence.",
                   QWEN3_TTS_CFG, QWEN3_TTS_SETUP, QWEN3_TTS_SERVICE,
                   keepalive=QWEN3_TTS_KEEPALIVE))
