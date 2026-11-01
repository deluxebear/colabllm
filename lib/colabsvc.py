"""colabsvc -- shared runner for "one LLM service = one notebook + one script" on Google Colab.

A service script (``services/<name>.py``) declares a :class:`Service` and calls :func:`main`.
Responsibilities are split like this:

  service script (local)                       notebook (Colab VM)
  ---------------------------------------      --------------------------------------------
  colab new / reuse session                    idempotent setup (models cached on Drive)
  colab dfs mount (browser-free Drive)         start the model service on 127.0.0.1
  --domain: cf CLI -> named tunnel, ingress,   AUTH=caddy: Caddy basic-auth / bearer proxy
            DNS CNAME, tunnel token            cloudflared (token -> fixed domain, else quick)
  generate credentials, pass via --env         print CT_* markers, then block forever
  keep `colab exec` attached (keep-alive)

Notebook contract (env injected with ``colab exec --env``):
  CT_SERVICE, CT_SERVICE_PORT, CT_PUBLIC_PORT, CT_AUTH (caddy|builtin|none),
  CT_AUTH_USER, CT_AUTH_PASS, CT_API_TOKEN, CT_TUNNEL_TOKEN, CT_PUBLIC_HOSTNAME, CT_DATA_DIR
Markers printed by the notebook (parsed here):
  CT_PUBLIC_URL=<url>   CT_CREDENTIALS user=<u> pass=<p>   CT_READY

Lessons baked in (verified on real runs):
  * `colab exec -f` sends cells one by one from the client: disconnecting early means later cells never run.
  * The session survives only while a `colab exec` connection stays open, so `up` keeps it attached
    in a detached local runner process; `down` stops both.
  * `drive.mount()` waits for browser consent forever -> patched to a no-op once `colab dfs` mounted Drive.
  * A failing cell does not change `colab exec`'s exit code -> readiness comes from the CT_READY marker.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = ROOT / ".state"
DEFAULT_ZONE = "example.com"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CELL_RE = re.compile(r"\[colab\] Executing cell (\d+)/(\d+)")
URL_MARK_RE = re.compile(r"CT_PUBLIC_URL=(https?://\S+)")
QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CRED_RE = re.compile(r"CT_CREDENTIALS user=(\S+) pass=(\S+)")
SKIP_RE = re.compile(r"^[ \t]*#[ \t]*@ct:skip\b", re.M)


@dataclasses.dataclass
class Service:
    name: str                       # also the Colab session name and the tunnel name suffix
    notebook: str                   # path relative to the service script
    service_port: int               # port the model service listens on (127.0.0.1)
    auth: str = "caddy"             # caddy (no built-in auth) | builtin | none
    public_port: int = 18080        # Caddy port when auth == "caddy" (not 8080/8012: used by Colab)
    gpu: str | None = "T4"          # T4|L4|G4|H100|A100 or None for CPU
    tpu: str | None = None
    high_mem: bool = False
    zone: str = DEFAULT_ZONE        # default zone for `--domain` without a value
    default_hostname: str | None = None  # optional override for `--domain` without a value
    auth_user: str = "admin"
    env: dict = dataclasses.field(default_factory=dict)   # extra env for the notebook
    ready_timeout: int = 3600       # seconds `up` waits for CT_READY
    base_name: str = ""             # base service name before --instance suffix
    base_default_hostname: str | None = None
    instance: str | None = None     # optional instance id (-i / --instance)

    def __post_init__(self) -> None:
        if not self.base_name:
            self.base_name = self.name
        if self.base_default_hostname is None:
            self.base_default_hostname = self.default_hostname

    @property
    def tunnel_port(self) -> int:
        return self.public_port if self.auth == "caddy" else self.service_port


def extract_instance_arg(argv: list[str] | None = None) -> tuple[str | None, list[str]]:
    """Extract `-i <id>` / `--instance <id>` from anywhere in argv so it works before or after the subcommand."""
    args = list(sys.argv[1:] if argv is None else argv)
    inst: str | None = os.environ.get("CT_INSTANCE") or None
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-i", "--instance") and i + 1 < len(args):
            inst = args[i + 1].strip()
            i += 2
        elif a.startswith("--instance="):
            inst = a.split("=", 1)[1].strip()
            i += 1
        else:
            cleaned.append(a)
            i += 1
    if inst:
        inst = re.sub(r"[^a-zA-Z0-9_-]+", "-", inst).strip("-") or None
    return inst, cleaned


def apply_instance(svc: Service, inst: str | None) -> Service:
    """Apply an instance suffix (`-i 2` -> `<base>-2`, domain `<host>-2.<zone>`) to `svc` in place."""
    base = svc.base_name or svc.name
    svc.base_name = base
    base_host = svc.base_default_hostname
    if base_host is None:
        base_host = svc.default_hostname
        svc.base_default_hostname = base_host
    svc.instance = inst
    if not inst:
        svc.name = base
        svc.default_hostname = base_host
        return svc
    suffix = f"-{inst}"
    svc.name = base if base.endswith(suffix) else f"{base}{suffix}"
    if base_host:
        if "." in base_host:
            sub, rest = base_host.split(".", 1)
            svc.default_hostname = f"{sub}{suffix}.{rest}" if not sub.endswith(suffix) else base_host
        else:
            svc.default_hostname = f"{base_host}{suffix}" if not base_host.endswith(suffix) else base_host
    return svc


def list_service_instances(base_name: str) -> list[dict]:
    """Return status dicts for the default instance and all `<base_name>-<id>` instances found in `.state/`."""
    if not STATE_ROOT.is_dir():
        return []
    items: list[dict] = []
    for d in sorted(STATE_ROOT.iterdir()):
        if not d.is_dir():
            continue
        if d.name == base_name:
            inst_id = ""
        elif d.name.startswith(base_name + "-"):
            inst_id = d.name[len(base_name) + 1:]
        else:
            continue
        sp = d / "state.json"
        if not sp.is_file():
            continue
        try:
            st = json.loads(sp.read_text())
        except Exception:
            continue
        alive = pid_alive(st.get("runner_pid"))
        items.append({
            "instance": inst_id or "default",
            "instance_arg": inst_id,
            "name": d.name,
            "alive": alive,
            "running": alive,
            "ready": bool(st.get("ready") and alive),
            "runner_pid": st.get("runner_pid"),
            "url": st.get("url"),
            "api_token": st.get("api_token"),
            "auth_user": st.get("auth_user"),
            "auth_pass": st.get("auth_pass"),
            "started": st.get("started"),
        })
    return items


# --------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    print(f"\033[36m[colabsvc]\033[0m {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"\033[33m[colabsvc] {msg}\033[0m", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"\033[31m[colabsvc] {msg}\033[0m", file=sys.stderr, flush=True)
    sys.exit(code)


def banner(lines: list[str]) -> None:
    w = max(len(l) for l in lines) + 8
    print("\n\033[32m" + "=" * w + "\n" + "\n".join(f"    {l}" for l in lines) + "\n" + "=" * w + "\033[0m\n",
          flush=True)


def run(cmd: list[str], *, capture: bool = False, check: bool = True, input: str | None = None):
    res = subprocess.run(cmd, text=True, input=input,
                         stdout=subprocess.PIPE if capture else None,
                         stderr=subprocess.PIPE if capture else None)
    if check and res.returncode != 0:
        if capture:
            sys.stderr.write((res.stdout or "") + (res.stderr or ""))
        die(f"command failed ({res.returncode}): {' '.join(cmd[:4])} ...")
    return res


def cf_json(*args: str):
    res = run(["cf", *args], capture=True)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        die(f"unexpected output from cf {' '.join(args[:3])}: {res.stdout[:300]}")


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- state
class State:
    def __init__(self, svc: Service):
        self.dir = STATE_ROOT / svc.name
        self.path = self.dir / "state.json"

    def load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, **kw) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        st = self.load()
        st.update(kw)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False))
        os.chmod(tmp, 0o600)            # holds passwords and tunnel tokens
        tmp.replace(self.path)
        return st

    @property
    def log_path(self) -> Path:
        return self.dir / "run.log"


# --------------------------------------------------------------------------- notebook
PRELUDE = r'''# [colabsvc] prelude: drive.mount() is a no-op once `colab dfs` has mounted Drive;
# google.colab.userdata.get() returns None immediately in headless CLI runs instead of timing out (~10s).
import os
try:
    from google.colab import drive as _ct_drive
    if not getattr(_ct_drive.mount, "_ct_patched", False):
        _ct_orig_mount = _ct_drive.mount
        def _ct_mount(mountpoint="/content/drive", *a, **kw):
            if os.path.isdir(os.path.join(mountpoint, "MyDrive")):
                print(f"[colabsvc] Drive already mounted at {mountpoint}; skipping drive.mount()")
                return
            return _ct_orig_mount(mountpoint, *a, **kw)
        _ct_mount._ct_patched = True
        _ct_drive.mount = _ct_mount
    from google.colab import userdata as _ct_userdata
    if not getattr(_ct_userdata.get, "_ct_patched", False):
        def _ct_userdata_get(key, *a, **kw):
            return os.environ.get(key)
        _ct_userdata_get._ct_patched = True
        _ct_userdata.get = _ct_userdata_get
except Exception as _e:
    print("[colabsvc] colab patch skipped:", _e)
for _p in ("/content/drive/MyDrive/colab-llm/hf_token.txt",
           "/content/drive/MyDrive/colab-services/hf_token.txt",
           "/content/drive/MyDrive/colab-services/qwen-flash/hf_token.txt"):
    if not os.environ.get("HF_TOKEN") and os.path.isfile(_p):
        _tok = open(_p).read().strip()
        if _tok:
            os.environ["HF_TOKEN"] = os.environ["HUGGING_FACE_HUB_TOKEN"] = _tok
            print(f"[colabsvc] loaded HF_TOKEN from {_p}")
            break
'''


def build_notebook(src: Path, skip: set[int]) -> tuple[dict, list[str]]:
    nb = json.loads(src.read_text(encoding="utf-8"))
    cells = [{"cell_type": "code", "metadata": {}, "outputs": [], "execution_count": None, "source": PRELUDE}]
    plan, n = [], 0
    for c in nb.get("cells", []):
        source = "".join(c.get("source", ""))
        if c.get("cell_type") != "code" or not source.strip():
            continue
        n += 1
        title = re.search(r"#\s*@title\s+(.+)", source)
        first = (title.group(1) if title else
                 next((l for l in source.strip().splitlines() if l.strip() and not l.startswith("#")),
                      source.strip().splitlines()[0]))[:72]
        if n in skip or SKIP_RE.search(source):
            plan.append(f"  cell {n}: SKIP  {first}")
            continue
        plan.append(f"  cell {n}: run   {first}")
        cells.append(dict(c, outputs=[], execution_count=None))
    for k, c in enumerate(cells):
        c["id"] = c.get("id") or f"ct-{k}"
    return dict(nb, cells=cells, nbformat=4, nbformat_minor=5), plan


# --------------------------------------------------------------------------- Cloudflare (cf CLI)
def find_zone(hostname: str) -> dict:
    zones = cf_json("zones", "list")
    match = [z for z in zones if hostname == z["name"] or hostname.endswith("." + z["name"])]
    if not match:
        die(f"no Cloudflare zone in this account matches {hostname}")
    return max(match, key=lambda z: len(z["name"]))


def ensure_named_tunnel(svc: Service, hostname: str) -> dict:
    """Idempotently create tunnel `colab-<svc>`, point <hostname> at it, return id + token."""
    name = f"colab-{svc.name}"
    found = [t for t in cf_json("tunnels", "list", "--name", name, "--is-deleted", "false") if t["name"] == name]
    if found:
        tunnel = found[0]
        log(f"cf: reusing tunnel {name} ({tunnel['id']})")
    else:
        tunnel = cf_json("tunnels", "create", "--name", name, "--config-source", "cloudflare")
        log(f"cf: created tunnel {name} ({tunnel['id']})")
    tid = tunnel["id"]

    ingress = [{"hostname": hostname, "service": f"http://localhost:{svc.tunnel_port}"},
               {"service": "http_status:404"}]
    # NB: the --tunnel-config-* flags of `cf` (beta) demand unrelated Access flags -> use --body
    cf_json("tunnels", "config", "update", tid, "--body", json.dumps({"config": {"ingress": ingress}}))
    log(f"cf: ingress {hostname} -> http://localhost:{svc.tunnel_port}")

    zone = find_zone(hostname)
    target = f"{tid}.cfargotunnel.com"
    body = json.dumps({"type": "CNAME", "name": hostname, "content": target, "proxied": True,
                       "comment": f"colabsvc {svc.name}"})
    records = cf_json("dns", "records", "list", "-z", zone["id"], "--name", hostname)
    if not records:
        cf_json("dns", "records", "create", "-z", zone["id"], "--body", body)
        log(f"cf: DNS CNAME {hostname} -> {target} (created)")
    elif records[0]["type"] == "CNAME" and records[0]["content"] == target:
        log(f"cf: DNS CNAME {hostname} already points at the tunnel")
    elif records[0]["type"] == "CNAME":
        cf_json("dns", "records", "edit", records[0]["id"], "-z", zone["id"], "--body", body)
        log(f"cf: DNS CNAME {hostname} -> {target} (updated from {records[0]['content']})")
    else:
        die(f"{hostname} already has a {records[0]['type']} record; refusing to overwrite it")

    token = cf_json("tunnels", "token", "get", tid)
    return {"tunnel_id": tid, "tunnel_name": name, "zone_id": zone["id"], "hostname": hostname, "token": token}


def delete_named_tunnel(st: dict) -> None:
    host, tid = st.get("hostname"), st.get("tunnel_id")
    if host and st.get("zone_id"):
        for r in cf_json("dns", "records", "list", "-z", st["zone_id"], "--name", host):
            if r["type"] == "CNAME" and r["content"].startswith(tid):
                run(["cf", "dns", "records", "delete", r["id"], "-z", st["zone_id"], "--force"], capture=True)
                log(f"cf: deleted DNS record {host}")
    if tid:
        run(["cf", "tunnels", "connections", "cleanup", tid, "--force"], capture=True, check=False)
        run(["cf", "tunnels", "delete", tid, "--force"], capture=True)
        log(f"cf: deleted tunnel {st.get('tunnel_name')}")


# --------------------------------------------------------------------------- runner (attached `colab exec`)
def exec_runner(svc: Service, nb_path: Path) -> int:
    """Owns the `colab exec` connection (= the session's keep-alive) and records CT_* markers."""
    state = State(svc)
    st = state.load()
    cmd = ["colab", "exec", "-s", svc.name, "-f", str(nb_path), "--timeout", str(7 * 24 * 3600)]
    for k, v in st.get("env", {}).items():
        cmd += ["--env", f"{k}={v}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail, last_cell = "", False
    while True:
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.flush()
        tail = (tail + ANSI_RE.sub("", chunk.decode("utf-8", "replace")))[-16000:]
        upd = {}
        for cur, total in CELL_RE.findall(tail):
            if cur == total and not last_cell:
                last_cell = upd["last_cell_started"] = True
        m = URL_MARK_RE.findall(tail) or QUICK_URL_RE.findall(tail)
        if m and m[-1] != st.get("url"):
            upd["url"] = m[-1]
        c = CRED_RE.findall(tail)
        if c and list(c[-1]) != [st.get("login_user"), st.get("login_pass")]:
            upd["login_user"], upd["login_pass"] = c[-1]
        if "CT_READY" in tail and not st.get("ready"):
            upd["ready"] = True
        if upd:
            st = state.save(**upd)
    rc = proc.wait()
    state.save(runner_exit=rc, ready=False)
    print(f"\n[colabsvc] colab exec exited with code {rc}", flush=True)
    return rc


# --------------------------------------------------------------------------- commands
def ensure_session(svc: Service) -> None:
    try:
        sessions = json.loads((Path.home() / ".config/colab-cli/sessions.json").read_text())
    except Exception:
        sessions = {}
    if svc.name in sessions:
        log(f"reusing Colab session '{svc.name}'")
        return
    args = ["colab", "new", "-s", svc.name]
    if svc.tpu:
        args += ["--tpu", svc.tpu]
    elif svc.gpu:
        args += ["--gpu", svc.gpu]
    if svc.high_mem:
        args += ["--high-mem"]
    log(" ".join(args))
    run(args)


def cmd_up(svc: Service, script: Path, a) -> None:
    if a.gpu:
        svc.gpu = None if a.gpu.lower() in ("none", "cpu") else a.gpu
    for tool in ("colab",) + (("cf",) if a.domain else ()):
        if not shutil.which(tool):
            die(f"`{tool}` CLI not found")
    state = State(svc)
    st = state.load()
    busy = {8080: "Colab runtime server", 8012: "colab dfs creds shim"}
    for port in {svc.service_port, svc.tunnel_port} & busy.keys():
        die(f"port {port} is used on Colab VMs by the {busy[port]}; pick another port in services/{svc.name}.py")
    if pid_alive(st.get("runner_pid")):
        die(f"'{svc.name}' is already running (runner pid {st['runner_pid']}); use `down` first")

    nb_src = (script.parent / svc.notebook).resolve()
    skip = {int(x) for x in a.skip.split(",")} if a.skip else set()
    built, plan = build_notebook(nb_src, skip)
    default_host = svc.default_hostname or f"{svc.name}.{svc.zone}"
    hostname = (a.domain if a.domain not in (None, "@default") else default_host) if a.domain else None
    log(f"service {svc.name}: notebook {nb_src.name}, gpu {svc.tpu or svc.gpu or 'cpu'}, auth {svc.auth}, "
        f"{'domain ' + hostname if hostname else 'quick tunnel'}")
    print("\n".join(plan))
    if a.dry_run:
        return

    state.dir.mkdir(parents=True, exist_ok=True)
    nb_path = state.dir / nb_src.name
    nb_path.write_text(json.dumps(built, indent=1, ensure_ascii=False), encoding="utf-8")

    # credentials: stable across restarts, regenerated with --new-credentials
    if a.new_credentials or not st.get("auth_pass"):
        st = state.save(auth_user=svc.auth_user, auth_pass=secrets.token_urlsafe(18),
                        api_token="sk-" + secrets.token_urlsafe(32))
    env = {"CT_SERVICE": svc.name, "CT_SERVICE_PORT": str(svc.service_port),
           "CT_PUBLIC_PORT": str(svc.tunnel_port), "CT_AUTH": svc.auth,
           "CT_API_TOKEN": st["api_token"],
           "CT_DATA_DIR": f"/content/drive/MyDrive/colab-llm/{svc.base_name or svc.name}", **svc.env}
    hf_tok = a.hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or st.get("hf_token")
    if not hf_tok:
        for p in (Path.home() / ".cache/huggingface/token", Path.home() / ".huggingface/token"):
            if p.is_file() and p.read_text().strip():
                hf_tok = p.read_text().strip()
                break
    if hf_tok:
        st = state.save(hf_token=hf_tok)
        env.update(HF_TOKEN=hf_tok, HUGGING_FACE_HUB_TOKEN=hf_tok)
    if svc.auth == "caddy":
        env.update(CT_AUTH_USER=st["auth_user"], CT_AUTH_PASS=st["auth_pass"])

    # 1. named tunnel + DNS (local, cf CLI)
    if hostname:
        info = ensure_named_tunnel(svc, hostname)
        env.update(CT_TUNNEL_TOKEN=info["token"], CT_PUBLIC_HOSTNAME=hostname)
        st = state.save(**{k: v for k, v in info.items() if k != "token"})
    # 2. session + 3. Drive
    ensure_session(svc)
    if not a.no_drive:
        log("colab dfs mount")
        run(["colab", "dfs", "mount", "-s", svc.name])
    st = state.save(env=env, url=None, ready=False, last_cell_started=False, login_user=None, login_pass=None,
                    runner_exit=None, started=time.strftime("%Y-%m-%d %H:%M:%S"))

    # 4. notebook through an attached `colab exec` (keep-alive)
    if a.foreground:
        os.environ["COLABSVC_FOREGROUND"] = "1"
        rc = exec_runner(svc, nb_path)
        sys.exit(rc)
    state.log_path.write_bytes(b"")
    exec_cmd = [sys.executable, str(script), *(["-i", svc.instance] if svc.instance else []), "_exec", str(nb_path)]
    proc = subprocess.Popen(exec_cmd, stdin=subprocess.DEVNULL, stdout=open(state.log_path, "ab"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    state.save(runner_pid=proc.pid)
    log(f"runner started (pid {proc.pid}) -- it keeps `colab exec` attached; log: {state.log_path.relative_to(ROOT)}")
    log("streaming progress until the service reports CT_READY (Ctrl-C only stops watching)")
    offset, deadline = 0, time.time() + svc.ready_timeout
    try:
        while time.time() < deadline:
            with open(state.log_path, "rb") as f:
                f.seek(offset)
                data = f.read()
            offset += len(data)
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            st = state.load()
            if st.get("ready") and st.get("url"):
                break
            if proc.poll() is not None:
                die(f"runner exited early (code {proc.returncode}); see {state.log_path}")
            time.sleep(1)
        else:
            warn("timed out waiting for CT_READY; the runner keeps going -- check `logs -f`")
    except KeyboardInterrupt:
        print()
    show_info(svc, do_probe=True)


def probe(url: str, auth: tuple[str, str] | None = None, bearer: str | None = None) -> str:
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    elif bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return str(r.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception as e:
        return f"error ({type(e).__name__})"


def show_info(svc: Service, do_probe: bool = False) -> None:
    st = State(svc).load()
    lines = [f"service : {svc.name}   (runner {'alive' if pid_alive(st.get('runner_pid')) else 'stopped'})"]
    if st.get("url"):
        lines.append(f"URL     : {st['url']}")
    if svc.auth == "caddy" and st.get("auth_pass"):
        lines.append(f"login   : {st['auth_user']} / {st['auth_pass']}   (Caddy basic auth)")
        lines.append(f"API     : Authorization: Bearer {st['api_token']}")
    elif svc.auth == "builtin" and st.get("api_token") and not st.get("login_user"):
        lines.append(f"API     : Authorization: Bearer {st['api_token']}")
    if st.get("login_user"):
        lines.append(f"app login: {st['login_user']} / {st['login_pass']}")
    if do_probe and st.get("url"):
        lines.append(f"probe   : GET {st['url']} -> {probe(st['url'])} (no credentials)")
        if svc.auth == "caddy" and st.get("auth_pass"):
            lines.append(f"probe   : GET {st['url']} -> {probe(st['url'], (st['auth_user'], st['auth_pass']))} "
                         "(basic auth)")
        elif svc.auth == "builtin" and st.get("api_token") and not st.get("login_user"):
            lines.append(f"probe   : GET {st['url']}/v1/models -> "
                         f"{probe(st['url'].rstrip('/') + '/v1/models', bearer=st['api_token'])} (bearer)")
    banner(lines)


def show_instances(svc: Service) -> None:
    items = list_service_instances(svc.base_name or svc.name)
    if not items:
        log(f"no instances found in .state/ for '{svc.base_name or svc.name}'")
        return
    print(f"\nInstances of {svc.base_name or svc.name}:")
    print(f"  {'INSTANCE':<12} {'SESSION':<28} {'STATE':<14} {'URL':<42} {'API TOKEN'}")
    print("  " + "-" * 116)
    for it in items:
        state_str = "READY" if it["ready"] else ("STARTING" if it["alive"] else "STOPPED")
        url_str = it.get("url") or "-"
        tok = it.get("api_token") or "-"
        print(f"  {it['instance']:<12} {it['name']:<28} {state_str:<14} {url_str:<42} {tok}")
    print()


def cmd_down(svc: Service, a) -> None:
    if getattr(a, "all", False):
        items = list_service_instances(svc.base_name or svc.name)
        for it in items:
            sub_svc = dataclasses.replace(svc)
            apply_instance(sub_svc, it["instance_arg"] or None)
            sub_a = argparse.Namespace(delete_tunnel=a.delete_tunnel, all=False)
            cmd_down(sub_svc, sub_a)
        return
    state = State(svc)
    st = state.load()
    if pid_alive(st.get("runner_pid")):
        log(f"stopping runner (pid {st['runner_pid']})")
        try:
            os.killpg(int(st["runner_pid"]), 15)
        except OSError:
            pass
    log(f"colab stop -s {svc.name}")
    run(["colab", "stop", "-s", svc.name], check=False)
    if a.delete_tunnel and st.get("tunnel_id"):
        delete_named_tunnel(st)
        st = {k: v for k, v in st.items() if k not in ("tunnel_id", "tunnel_name", "zone_id", "hostname")}
    keep = {k: st[k] for k in ("auth_user", "auth_pass", "api_token", "hf_token", "tunnel_id", "tunnel_name",
                               "zone_id", "hostname") if k in st}
    state.dir.mkdir(parents=True, exist_ok=True)
    state.path.write_text(json.dumps(keep, indent=2))
    os.chmod(state.path, 0o600)


def main(svc: Service) -> None:
    script = Path(sys.argv[0]).resolve()
    inst, cleaned_argv = extract_instance_arg(sys.argv[1:])
    apply_instance(svc, inst)

    p = argparse.ArgumentParser(prog=script.name, description=(sys.modules["__main__"].__doc__ or "").strip(),
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-i", "--instance", metavar="ID",
                   help="instance ID to create/manage multiple parallel instances (e.g. -i 1, -i 2)")
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up", help="session -> Drive -> (cf tunnel) -> run notebook, keep it alive")
    default_host = svc.default_hostname or f"{svc.name}.{svc.zone}"
    u.add_argument("--domain", nargs="?", const="@default", metavar="HOST",
                   help=f"fixed domain via a named tunnel (default host: {default_host})")
    u.add_argument("--gpu", help="override GPU (T4|L4|G4|H100|A100|none)")
    u.add_argument("--skip", default="", help="1-based code cells to skip, e.g. 1,2")
    u.add_argument("--no-drive", action="store_true", help="do not mount Google Drive")
    u.add_argument("--hf-token", metavar="TOKEN",
                   help="Hugging Face token (saved in .state and forwarded as HF_TOKEN; or set env HF_TOKEN)")
    u.add_argument("--new-credentials", action="store_true", help="rotate the Caddy password / API token")
    u.add_argument("--foreground", action="store_true", help="stay attached in this terminal instead")
    u.add_argument("--dry-run", action="store_true", help="only show the cell plan")
    d = sub.add_parser("down", help="stop runner + Colab session")
    d.add_argument("--delete-tunnel", action="store_true", help="also delete the named tunnel and its DNS record")
    d.add_argument("--all", action="store_true", help="stop all instances of this service")
    sub.add_parser("info", help="URL and credentials")
    sub.add_parser("ps", help="list all instances of this service and their URLs / states")
    sub.add_parser("list", help="alias for `ps`")
    lg = sub.add_parser("logs", help="notebook output")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("-n", "--lines", type=int, default=80)
    sub.add_parser("status", help="colab status + public URL probe")
    ex = sub.add_parser("_exec")          # internal: detached runner
    ex.add_argument("nb")
    a = p.parse_args(cleaned_argv)

    if a.cmd == "up":
        cmd_up(svc, script, a)
    elif a.cmd == "_exec":
        sys.exit(exec_runner(svc, Path(a.nb)))
    elif a.cmd == "down":
        cmd_down(svc, a)
    elif a.cmd == "info":
        show_info(svc)
    elif a.cmd in ("ps", "list"):
        show_instances(svc)
    elif a.cmd == "status":
        run(["colab", "status", "-s", svc.name], check=False)
        show_info(svc, do_probe=True)
    elif a.cmd == "logs":
        lp = State(svc).log_path
        if not lp.exists():
            die(f"no log at {lp}")
        os.execvp("tail", ["tail", "-n", str(a.lines), *(["-f"] if a.follow else []), str(lp)])
