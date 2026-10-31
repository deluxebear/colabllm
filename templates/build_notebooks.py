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
    @cookie header Cookie *ct_session={API_TOKEN}*
    handle @cookie {{
        {upstream}
    }}
    handle {{
        basic_auth {{
            {AUTH_USER} {hashed}
        }}
        header +Set-Cookie "ct_session={API_TOKEN}; Path=/; HttpOnly; SameSite=Lax"
        {upstream}
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
    # Must be 401 from *our* Caddy; anything else means something else answers on that port.
    ct_wait_http(f"http://127.0.0.1:{PUBLIC_PORT}/", timeout=60, ok=lambda c: c == 401)
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
    tmp = path.with_name(path.name + ".tmp")
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
        partial = cache / "runtime.tar.partial"
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
        partial = backup.with_suffix(".gguf.partial")
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
            write_json_atomic(SERVICE_DIR / "last-verification.json", {
                "verified_at": time.strftime("%FT%T%z"), "base_url": PUBLIC_URL + "/v1",
                "context_allocated": allocated, "vision_enabled": vision_enabled, "reply": reply,
            })
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
        "kernel_pid": os.getpid(), "server_pid": PROCS["service"][0].pid,
        "tunnel_pid": PROCS["cloudflared"][0].pid, "context": CFG["context"],
        "alias": CFG["alias"], "url": PUBLIC_URL + "/v1",
        "updated_at": time.strftime("%FT%T%z"), "uptime_sec": int(time.monotonic() - started),
        "gpu_memory": gpu_mem,
    }
    try:
        write_json_atomic(SERVICE_DIR / "last-status.json", status_rec)
    except OSError:
        pass
    qlog("heartbeat | " + gpu_mem)
'''

# --------------------------------------------------------------------------- comfyui (Caddy auth + ComfyUI-Manager)
COMFYUI_CFG = dict(name="comfyui", service_port=8188, auth="caddy", public_port=18080,
                   health_path="/system_stats", ready_timeout=900)
COMFYUI_SETUP = r'''# @title 1. Setup -- install ComfyUI + ComfyUI-Manager once per VM (idempotent) + link Drive dirs
COMFY_DIR = "/content/ComfyUI"
COMFY_REPO = os.environ.get("CT_COMFY_REPO", "https://github.com/comfyanonymous/ComfyUI.git")
MANAGER_REPO = os.environ.get("CT_COMFY_MANAGER_REPO", "https://github.com/Comfy-Org/ComfyUI-Manager.git")
UI_MODE = os.environ.get("CT_COMFY_MANAGER_UI", "both")   # v4 | legacy | both (custom_nodes)

if not os.path.isdir(f"{COMFY_DIR}/.git"):
    subprocess.run(["git", "clone", "--depth", "1", COMFY_REPO, COMFY_DIR], check=True)
else:
    subprocess.run(["git", "-C", COMFY_DIR, "pull", "--ff-only"], check=False)

# Persist models, custom_nodes, user, input, and output in DATA_DIR (Google Drive when mounted)
for sub in ("models", "custom_nodes", "output", "input", "user"):
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

# Install ComfyUI core requirements + ComfyUI-Manager (core v4 package + custom_nodes for full compatibility)
req_marker = f"{WORK}/comfyui_deps.ok"
if not os.path.exists(req_marker):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{COMFY_DIR}/requirements.txt"], check=True)
    if os.path.exists(f"{COMFY_DIR}/manager_requirements.txt"):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{COMFY_DIR}/manager_requirements.txt"],
                       check=True)
    open(req_marker, "w").write("ok\n")

# Patch ComfyUI frontend so <script type="module">, <link rel="modulepreload">, and Vite's preload helper
# use crossorigin="use-credentials" (otherwise browsers omit Basic Auth / cookies and get 401 behind Caddy)
import glob, site
for sp in site.getsitepackages():
    fe_dir = f"{sp}/comfyui_frontend_package/static"
    if not os.path.isdir(fe_dir):
        continue
    idx = f"{fe_dir}/index.html"
    if os.path.isfile(idx):
        html = open(idx, encoding="utf-8").read()
        patched = re.sub(r'\bcrossorigin(?:="")?(?!="use-credentials")(?=[\s>/])',
                         'crossorigin="use-credentials"', html)
        if patched != html:
            open(idx, "w", encoding="utf-8").write(patched)
            print("Patched crossorigin=use-credentials in", idx)
    for js_path in glob.glob(f"{fe_dir}/assets/*.js"):
        js = open(js_path, encoding="utf-8", errors="ignore").read()
        patched_js = js.replace('crossOrigin=``', 'crossOrigin=`use-credentials`').replace(
            'crossOrigin=""', 'crossOrigin="use-credentials"')
        if patched_js != js:
            open(js_path, "w", encoding="utf-8").write(patched_js)
            print("Patched crossOrigin in", js_path)

if UI_MODE in ("legacy", "both"):
    mgr_dir = f"{COMFY_DIR}/custom_nodes/ComfyUI-Manager"
    if not os.path.isdir(f"{mgr_dir}/.git"):
        subprocess.run(["git", "clone", "--depth", "1", MANAGER_REPO, mgr_dir], check=True)
    else:
        subprocess.run(["git", "-C", mgr_dir, "pull", "--ff-only"], check=False)
    if os.path.exists(f"{mgr_dir}/requirements.txt"):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{mgr_dir}/requirements.txt"], check=True)

# Install requirements of any other persisted custom_nodes on Drive
for entry in sorted(os.listdir(f"{COMFY_DIR}/custom_nodes")):
    req = f"{COMFY_DIR}/custom_nodes/{entry}/requirements.txt"
    if entry != "ComfyUI-Manager" and os.path.isfile(req):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req], check=False)

print("ComfyUI + ComfyUI-Manager setup complete; data_dir =", DATA_DIR)
'''
COMFYUI_SERVICE = r'''# @title 2. Service -- ComfyUI + ComfyUI-Manager on 127.0.0.1:8188 (Caddy auth in front)
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

if __name__ == "__main__":
    write(ROOT / "templates/service.ipynb",
          notebook("my-service (template)", "Template: replace cells 1 and 2 with the real model service.",
                   TEMPLATE_CFG, TEMPLATE_SETUP, TEMPLATE_SERVICE))
    write(ROOT / "services/comfyui.ipynb",
          notebook("ComfyUI + ComfyUI-Manager",
                   "ComfyUI with ComfyUI-Manager enabled behind Caddy auth (basic auth for browser UI, "
                   "Bearer token for API clients). Models, custom_nodes, user settings, input, and output "
                   "persist in `CT_DATA_DIR` (`/content/drive/MyDrive/colab-llm/comfyui`).",
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
