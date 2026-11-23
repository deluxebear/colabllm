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
import asyncio
import dataclasses
import contextlib
import fcntl
import signal
import selectors
import tempfile
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.request
from pathlib import Path

from paths import private_dir, user_home
from cli_support import CliError, UsageError, command_timeout, nonnegative_seconds, positive_seconds, diagnostic, safe_urlopen
from colab_accounts import (
    account_sessions_path,
    colab_subprocess_env,
    resolve_effective_account,
    resolve_effective_drive_account,
    validate_account,
)
from colab_ws import has_native_colab_session, run_single_ws_session
import mux
from protocol import NotebookProtocol
from processes import process_identity, runner_alive, stop_owned_process
import runtime_core as _core
from runtime_core import (
    COLAB_NAMESPACE,
    nonnegative_int,
    probe,
    validate_domain,
    validate_instance,
)

STATE_ROOT = user_home()
DEFAULT_ZONE = "example.com"

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
    zone: str = DEFAULT_ZONE        # legacy suffix used to extract service aliases
    default_hostname: str | None = None  # legacy host supplying an alias (e.g. qwen.<zone>)
    auth_user: str = "admin"
    env: dict = dataclasses.field(default_factory=dict)   # extra env for the notebook
    ready_timeout: int = 3600       # seconds `up` waits for CT_READY
    health_path: str = ""           # optional HTTP health probe path (defaults to "/" or "/v1/models")
    base_name: str = ""             # base service name before --instance suffix
    base_default_hostname: str | None = None
    instance: str | None = None     # optional instance id (-i / --instance)
    account: str | None = None      # optional Colab account name/email (--account)
    drive_account: str | None = None  # optional shared Google Drive storage account (--drive-account)

    def __post_init__(self) -> None:
        if not self.base_name:
            self.base_name = self.name
        if self.base_default_hostname is None:
            self.base_default_hostname = self.default_hostname

    @property
    def tunnel_port(self) -> int:
        return self.public_port if self.auth == "caddy" else self.service_port


def extract_account_arg(argv: list[str] | None = None) -> tuple[str | None, list[str]]:
    """Extract `--account <acct>` / `--account=<acct>` from anywhere before `--`.

    Short `-a` is intentionally NOT stripped here because business subcommands such as
    `minimax-h3-image generate -a 16:9` use `-a` for `--aspect`.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    acct: str | None = None
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            cleaned.extend(args[i:])
            break
        if a == "--account":
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                raise UsageError("--account 需要有效的 Colab 账户名或邮箱")
            acct = validate_account(args[i + 1])
            i += 2
        elif a.startswith("--account="):
            acct = validate_account(a.split("=", 1)[1])
            i += 1
        else:
            cleaned.append(a)
            i += 1
    return acct, cleaned


def extract_instance_arg(argv: list[str] | None = None) -> tuple[str | None, list[str]]:
    """Extract `-i <id>` / `--instance <id>` from anywhere in argv so it works before or after the subcommand."""
    return _core.extract_instance_arg(argv, validator=validate_instance)


def apply_instance(svc: Service, inst: str | None) -> Service:
    """Apply an instance suffix (`-i 2` -> `<base>-2`, domain `<host>-2.<zone>`) to `svc` in place."""
    return _core.apply_instance(svc, inst, validator=validate_instance)


def load_tunnel_config() -> dict:
    return _core.load_tunnel_config(state_root=STATE_ROOT)


def save_tunnel_config(data: dict) -> None:
    _core.save_tunnel_config(data, state_root=STATE_ROOT)


def tunnel_transport(args) -> str:
    return _core.tunnel_transport(args, state_root=STATE_ROOT)


def tunnel_hostname(svc: Service, args) -> str | None:
    return _core.tunnel_hostname(svc, args, namespace=COLAB_NAMESPACE, state_root=STATE_ROOT)


def cmd_tunnel_config(args) -> None:
    _core.cmd_tunnel_config(args, state_root=STATE_ROOT)


def _known_service_names() -> set[str]:
    from instance_discovery import discover_service_pairs
    return set(discover_service_pairs(Path(__file__).resolve().parents[1] / "services"))


def list_service_instances(base_name: str) -> list[dict]:
    """Return status dicts for the default instance and all `<base_name>-<id>` instances found in `~/.colabllm/services/`."""
    from instance_discovery import list_raw_instances
    raw_items = list_raw_instances(
        STATE_ROOT / "services",
        _known_service_names(),
        target_service=base_name,
        alive_fn=runner_alive,
        on_corrupt="skip",
    )
    items: list[dict] = []
    for raw in raw_items:
        st = raw["state"]
        alive = raw["alive"]
        items.append({
            "instance": raw["instance"],
            "instance_arg": raw["instance_arg"],
            "name": raw["name"],
            "alive": alive,
            "running": alive,
            "ready": raw["ready"],
            "auto_restart": bool(st.get("auto_restart")),
            "restart_count": int(st.get("restart_count", 0)),
            "runner_pid": st.get("runner_pid"),
            "url": st.get("url"),
            "account": st.get("account"),
            "drive_account": st.get("drive_account"),
            "api_token": st.get("api_token"),
            "auth_user": st.get("auth_user"),
            "auth_pass": st.get("auth_pass"),
            "login_user": st.get("login_user"),
            "login_pass": st.get("login_pass"),
            "started": st.get("started"),
        })
    return items


def colab_env_for(svc: Service, *, env_overrides: dict[str, str] | None = None) -> dict[str, str] | None:
    if not env_overrides:
        return colab_subprocess_env(svc.account, svc.drive_account)
    merged = {**os.environ, **env_overrides}
    return colab_subprocess_env(svc.account, svc.drive_account, base_env=merged) or merged


def colab_runtime_env_for(svc: Service, *, env_overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(colab_env_for(svc, env_overrides=env_overrides) or os.environ)
    env.setdefault("REQUEST_TIMEOUT", str(DEFAULT_COLAB_REQUEST_TIMEOUT))
    return env


def colab_env_for_session(session_name: str, explicit_account: str | None = None) -> dict[str, str] | None:
    st: dict = {}
    sp = STATE_ROOT / "services" / session_name / "state.json"
    if sp.is_file():
        with contextlib.suppress(OSError, ValueError, UnicodeError):
            loaded = json.loads(sp.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                st = loaded
    acct, _ = resolve_effective_account(explicit=explicit_account, saved=st.get("account"), prefer_saved=True)
    drv = st.get("drive_account") if (not explicit_account or acct == st.get("account")) else None
    return colab_subprocess_env(acct, drv)


# --------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    diagnostic(f"[colabsvc] {msg}", "36")


def warn(msg: str) -> None:
    diagnostic(f"[colabsvc] {msg}", "33")


def die(msg: str, code: int = 1) -> None:
    raise CliError(msg, code)


def banner(lines: list[str]) -> None:
    w = max(len(l) for l in lines) + 8
    print("\n" + "=" * w + "\n" + "\n".join(f"    {line}" for line in lines) + "\n" + "=" * w + "\n", flush=True)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _extract_failure_reason(
    stdout: str | None, stderr: str | None, *, max_len: int = 420
) -> str | None:
    """Extract a concise single-line root cause from captured CLI stderr/stdout (including Rich tracebacks)."""
    for raw in (stderr, stdout):
        if not raw:
            continue
        cleaned = _ANSI_ESCAPE_RE.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")
        lines = [ln.strip() for ln in cleaned.splitlines()]
        last_box_end = -1
        for idx, ln in enumerate(lines):
            if ln.startswith("╰"):
                last_box_end = idx
        if last_box_end >= 0:
            after = [ln for ln in lines[last_box_end + 1 :] if ln]
            if after:
                joined = " ".join(after)
                return joined if len(joined) <= max_len else joined[: max_len - 3] + "..."
        tagged_idx = next(
            (
                i
                for i, ln in enumerate(lines)
                if ln.startswith(("[colab]", "[drive]"))
                and not ln.startswith(("[colab] Creating session", "[colab] Stopping session"))
            ),
            None,
        )
        if tagged_idx is not None:
            joined = " ".join(
                ln for ln in lines[tagged_idx:] if ln and not ln.startswith(("╭", "│", "╰"))
            )
            if joined:
                return joined if len(joined) <= max_len else joined[: max_len - 3] + "..."
        non_box = [ln for ln in lines if ln and not ln.startswith(("╭", "│", "╰"))]
        if non_box:
            tail = non_box[-1]
            return tail if len(tail) <= max_len else tail[: max_len - 3] + "..."
    return None


def run(
    cmd: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    input: str | None = None,
    timeout=None,
    env: dict[str, str] | None = None,
    quiet_capture_on_error: bool = False,
):
    res = subprocess.run(cmd, text=True, input=input,
                         stdout=subprocess.PIPE if capture else None,
                         stderr=subprocess.PIPE if capture else None,
                         timeout=command_timeout() if timeout is None else timeout,
                         env=env)
    if check and res.returncode != 0:
        if capture and not quiet_capture_on_error:
            sys.stderr.write((res.stdout or "") + (res.stderr or ""))
        reason = _extract_failure_reason(res.stdout, res.stderr) if capture else None
        msg = f"命令执行失败 ({res.returncode}): {' '.join(cmd[:4])}"
        if reason:
            msg = f"{msg} — {reason}"
        err = CliError(msg, res.returncode if res.returncode > 0 else 1)
        if capture:
            err.stdout = res.stdout or ""
            err.stderr = res.stderr or ""
        raise err
    return res


def cf_json(*args: str):
    res = run(["cf", *args], capture=True)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        die(f"unexpected output from cf {' '.join(args[:3])}: {res.stdout[:300]}")


# --------------------------------------------------------------------------- state
class State(_core.LockedState):
    def __init__(self, svc: Service):
        super().__init__(svc.name, COLAB_NAMESPACE, state_root=STATE_ROOT)


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
    return _core.find_zone(hostname, cf_json_fn=cf_json)


def ensure_named_tunnel(svc: Service, hostname: str) -> dict:
    """Idempotently create tunnel `colab-<svc>`, point <hostname> at it, return id + token."""
    return _core.ensure_named_tunnel(
        svc,
        hostname,
        namespace=COLAB_NAMESPACE,
        state_root=STATE_ROOT,
        cf_json_fn=cf_json,
        find_zone_fn=find_zone,
        run_fn=run,
        log_fn=log,
    )


def delete_named_tunnel(st: dict) -> None:
    _core.delete_named_tunnel(
        st,
        namespace=COLAB_NAMESPACE,
        cf_json_fn=cf_json,
        run_fn=run,
        log_fn=log,
    )


def _write_private_notebook(nb_path: Path, built: dict, env: dict | None = None) -> None:
    notebook_copy = dict(built)
    cells = [dict(c) for c in built.get("cells", [])]
    if env and cells:
        env_inject = (
            "# [colabsvc] private runtime environment (injected locally via 0600 notebook; never exposed on ps argv)\n"
            f"import json as _ct_json, os as _ct_os\n"
            f"_ct_os.environ.update(_ct_json.loads({json.dumps(env, ensure_ascii=False)!r}))\n"
        )
        src0 = cells[0].get("source", "")
        if isinstance(src0, list):
            src0 = "".join(src0)
        if "_ct_os.environ.update(" not in src0:
            cells[0]["source"] = env_inject + src0
    notebook_copy["cells"] = cells
    fd, tmp_name = tempfile.mkstemp(prefix=".nb-", suffix=".tmp", dir=nb_path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(notebook_copy, handle, indent=1, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(nb_path)
    finally:
        tmp_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- runner (attached `colab exec`)
DEFAULT_RESTART_DELAY = 3.0
MAX_RESTART_BACKOFF = 300.0
DEFAULT_FAST_PROBE_SECONDS = 10.0
DEFAULT_FAST_PROBE_TIMEOUT = 5.0
DEFAULT_IDLE_CHECK_SECONDS = 300.0
DEFAULT_SILENT_RECHECK_SECONDS = 60.0
DEFAULT_ASSIGN_RETRIES = 5
DEFAULT_ASSIGN_RETRY_DELAY = 10.0
MAX_ASSIGN_RETRY_BACKOFF = 30.0
DEFAULT_MAX_STARTUP_RETRIES = 3
DEFAULT_COLAB_REQUEST_TIMEOUT = 60
DEFAULT_PREFLIGHT_RETRIES = 2
DEFAULT_PREFLIGHT_RETRY_DELAY = 2.0
DEFAULT_PREFLIGHT_HANG_GRACE = 0.5


def parse_colab_session_status(session_name: str, returncode: int, stdout: str = "", stderr: str = "") -> str:
    """Classify `colab status -s <name>` output into 'alive', 'missing', or 'unknown'.

    Note: `colab status` returns exit code 0 even when the VM was reclaimed
    (`[colab] Session '<name>' not found.`), so text inspection is required.
    """
    combined = f"{stdout or ''}\n{stderr or ''}"
    lower = combined.lower()
    if "not found" in lower or "no active session" in lower or "does not exist" in lower:
        return "missing"
    if returncode == 0 and f"[{session_name}]" in combined:
        return "alive"
    return "unknown"


def inspect_remote_session(
    svc: Service,
    timeout: float = 30.0,
    *,
    env_overrides: dict[str, str] | None = None,
) -> tuple[str, str | None]:
    """Query `colab status -s <name>` and return `(state, error_detail)`.

    `state` is `'alive'`, `'missing'`, `'unknown'` (command exited 0 with unrecognized text),
    or `'error'` (command failed / timed out, e.g. local DNS or network outage).
    """
    try:
        res = run(
            ["colab", "status", "-s", svc.name],
            capture=True,
            check=False,
            timeout=timeout,
            env=colab_env_for(svc, env_overrides=env_overrides),
        )
    except (OSError, subprocess.SubprocessError, CliError) as err:
        return "error", str(err)
    parsed = parse_colab_session_status(svc.name, res.returncode, res.stdout or "", res.stderr or "")
    if res.returncode != 0 and parsed != "missing":
        detail = _extract_failure_reason(res.stdout, res.stderr) or f"exit={res.returncode}"
        return "error", detail
    return parsed, None


def check_remote_session(
    svc: Service,
    timeout: float = 30.0,
    *,
    env_overrides: dict[str, str] | None = None,
) -> str:
    status_name, _ = inspect_remote_session(svc, timeout=timeout, env_overrides=env_overrides)
    return "unknown" if status_name == "error" else status_name


def finish_runner(state, pid, identity, exit_code=None):
    def finish(st):
        if st.get("runner_pid") == pid and st.get("runner_identity") == identity:
            st.update(
                runner_pid=None,
                runner_identity=None,
                runner_mode=None,
                runner_lock=None,
                exec_pid=None,
                exec_identity=None,
                ready=False,
                runner_exit=exit_code,
            )
        return st
    state.update(finish)


def _runner_still_owned(state: State, pid: int, identity: str | None) -> tuple[bool, dict]:
    st = state.load()
    return bool(identity and st.get("runner_pid") == pid and st.get("runner_identity") == identity), st


def _probe_service_endpoint(
    svc: Service, st: dict, url_override: str | None = None, *, timeout: float = 15.0
) -> str:
    url = (url_override or st.get("url") or "").strip()
    if not url:
        return "missing-url"
    custom_path = (svc.health_path or "").strip()
    if svc.auth == "caddy":
        return probe(url, (st.get("auth_user", ""), st.get("auth_pass", "")), path=custom_path, timeout=timeout)
    if svc.auth == "builtin" and st.get("api_token") and not st.get("login_user"):
        return probe(url, bearer=st["api_token"], path=custom_path or "/v1/models", timeout=timeout)
    return probe(url, path=custom_path, timeout=timeout)


def _run_single_exec(
    svc: Service,
    state: State,
    cmd: list[str],
    *,
    deadline: float | None,
    auto_restart: bool,
    idle_check_seconds: float,
    fast_probe_seconds: float = DEFAULT_FAST_PROBE_SECONDS,
    initially_ready: bool = False,
) -> tuple[int, bool, bool, str | None]:
    """Run one `colab exec` session and return `(exit_code, reached_ready, half_open_reclaimed, preflight_error)`."""
    proc = None
    child_identity = None
    selector = selectors.DefaultSelector()
    reached_ready = bool(initially_ready)
    half_open_reclaimed = False
    preflight_dead_at: float | None = None
    preflight_aborted = False
    st = state.save(ready=bool(initially_ready))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=colab_runtime_env_for(svc),
        )
        child_identity = process_identity(proc.pid)
        st = state.save(exec_pid=proc.pid, exec_identity=child_identity)
        protocol = NotebookProtocol()
        selector.register(proc.stdout, selectors.EVENT_READ)
        now0 = time.monotonic()
        next_idle_probe_at = now0 + idle_check_seconds
        next_fast_probe_at = now0 + fast_probe_seconds
        next_status_allowed_at = 0.0
        while True:
            now = time.monotonic()
            wait_timeout = None
            if deadline is not None and not st.get("ready"):
                remaining = max(0.0, deadline - now)
                if remaining == 0:
                    raise TimeoutError("等待 CT_READY 超时，前台连接已停止；可用 status 检查远端会话")
                wait_timeout = remaining
            elif auto_restart and st.get("ready"):
                wakeups: list[float] = []
                if idle_check_seconds > 0:
                    wakeups.append(next_idle_probe_at - now)
                if fast_probe_seconds > 0 and st.get("url"):
                    wakeups.append(next_fast_probe_at - now)
                if wakeups:
                    wait_timeout = max(0.0, min(wakeups))
            if preflight_dead_at is not None:
                preflight_rem = max(0.0, preflight_dead_at - now)
                wait_timeout = preflight_rem if wait_timeout is None else min(wait_timeout, preflight_rem)
            if not selector.select(wait_timeout):
                now = time.monotonic()
                if preflight_dead_at is not None and now >= preflight_dead_at:
                    if proc.poll() is None:
                        warn(f"colab exec 在启动首个 Cell 前连接中断且未退出 ({protocol.preflight_error})，正在终止残留进程")
                        preflight_aborted = True
                    break
                if deadline is not None and not st.get("ready") and now >= deadline:
                    continue
                if auto_restart and st.get("ready"):
                    need_remote_check = False
                    if idle_check_seconds > 0 and now >= next_idle_probe_at:
                        need_remote_check = True
                        next_idle_probe_at = now + min(idle_check_seconds, DEFAULT_SILENT_RECHECK_SECONDS)
                    if fast_probe_seconds > 0 and st.get("url") and now >= next_fast_probe_at:
                        code = _probe_service_endpoint(
                            svc, st, timeout=min(DEFAULT_FAST_PROBE_TIMEOUT, max(0.5, fast_probe_seconds))
                        )
                        next_fast_probe_at = time.monotonic() + fast_probe_seconds
                        if code.isdigit() and 200 <= int(code) < 500:
                            next_status_allowed_at = 0.0
                        elif time.monotonic() >= next_status_allowed_at:
                            need_remote_check = True
                    if need_remote_check:
                        if check_remote_session(svc) == "missing":
                            warn(f"[auto-restart] 远端会话 '{svc.name}' 已不存在（连接半开或边缘探针检出停机），正在立即断开并恢复")
                            half_open_reclaimed = True
                            break
                        cooldown = (
                            min(idle_check_seconds, DEFAULT_SILENT_RECHECK_SECONDS)
                            if idle_check_seconds > 0
                            else DEFAULT_SILENT_RECHECK_SECONDS
                        )
                        next_status_allowed_at = time.monotonic() + cooldown
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            now_read = time.monotonic()
            next_idle_probe_at = now_read + idle_check_seconds
            next_fast_probe_at = now_read + fast_probe_seconds
            sys.stdout.buffer.write(chunk)
            sys.stdout.flush()
            update = {key: value for key, value in protocol.feed(chunk).items() if st.get(key) != value}
            if update:
                st = state.save(**update)
                if st.get("ready"):
                    reached_ready = True
            if protocol.preflight_error and not protocol.cell_started and not reached_ready:
                if preflight_dead_at is None:
                    preflight_dead_at = time.monotonic() + DEFAULT_PREFLIGHT_HANG_GRACE
            else:
                preflight_dead_at = None
        update = protocol.feed(b"", final=True)
        if update:
            st = state.save(**update)
            if st.get("ready"):
                reached_ready = True
        preflight_error = (
            protocol.preflight_error
            if (protocol.preflight_error and not protocol.cell_started and not reached_ready)
            else None
        )
        if half_open_reclaimed or preflight_aborted:
            stop_owned_process(proc.pid, child_identity, group=True)
            rc = 1
        else:
            try:
                rc = proc.wait(timeout=2.0 if preflight_error else None)
            except subprocess.TimeoutExpired:
                stop_owned_process(proc.pid, child_identity, group=True)
                rc = 1
            if rc == 0 and not st.get("ready"):
                warn("Colab 在 CT_READY 之前退出，请查看日志中的启动错误")
                rc = 1
        return (rc if rc >= 0 else 128 - rc), reached_ready, half_open_reclaimed, preflight_error
    finally:
        selector.close()
        if proc is not None:
            stop_owned_process(proc.pid, child_identity, group=True)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()


def _provision_session_for_restart(
    svc: Service,
    state: State,
    pid: int,
    identity: str | None,
    *,
    previous_url: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> str:
    """Verify remote session state under `.operation.lock` and reconnect or recreate the Colab VM.

    Returns `'reattached_ready'` (live VM + live HTTP endpoint), `'ready'`, `'stopped'`, or `'failed'`.
    """
    with state.lock("operation", blocking=True):
        owned, st = _runner_still_owned(state, pid, identity)
        if not owned:
            return "stopped"
        target_svc = dataclasses.replace(
            svc,
            gpu=st["session_gpu"] if "session_gpu" in st else svc.gpu,
            tpu=st["session_tpu"] if "session_tpu" in st else svc.tpu,
            high_mem=bool(st["session_high_mem"]) if "session_high_mem" in st else svc.high_mem,
            account=st.get("account") or svc.account,
            drive_account=st.get("drive_account") or svc.drive_account,
        )
        target_env = colab_env_for(target_svc, env_overrides=env_overrides)
        runtime_env = colab_runtime_env_for(target_svc, env_overrides=env_overrides)
        mount_drive = bool(st.get("mount_drive", True))
        try:
            remote, status_err = inspect_remote_session(target_svc, env_overrides=env_overrides)
            if remote == "error":
                warn(
                    f"[auto-restart] 无法查询远端会话 '{target_svc.name}' 状态"
                    f"（{status_err or '网络或 Colab 接口异常'}），暂不销毁实例，稍后重试"
                )
                return "failed"
            if remote == "alive":
                candidate_url = previous_url or st.get("url")
                if candidate_url:
                    code = _probe_service_endpoint(svc, st, url_override=candidate_url)
                    if code.isdigit() and 200 <= int(code) < 400:
                        state.save(url=candidate_url, ready=True)
                        log(
                            f"[auto-restart] 远端会话 '{target_svc.name}' 仍存活且服务探针正常 "
                            f"(HTTP {code})，保持 ready=True 并继续附着保活"
                        )
                        return "reattached_ready"
                log(f"[auto-restart] 远端会话 '{target_svc.name}' 仍存活（本地连接闪断），正在恢复挂载与保活连接")
                remount_ok = True
                if mount_drive:
                    try:
                        log("colab dfs mount")
                        run(
                            ["colab", "dfs", "mount", "-s", target_svc.name],
                            capture=True,
                            quiet_capture_on_error=True,
                            env=runtime_env,
                        )
                    except (CliError, OSError, subprocess.SubprocessError) as err:
                        recheck, recheck_err = inspect_remote_session(target_svc, env_overrides=env_overrides)
                        if recheck == "error":
                            warn(
                                f"[auto-restart] 原会话挂载恢复失败且无法复查远端状态"
                                f"（{recheck_err or err}），暂不销毁实例，稍后重试"
                            )
                            return "failed"
                        warn(f"[auto-restart] 原会话挂载恢复失败 ({err})，转为清理并重建会话")
                        remount_ok = False
                if remount_ok:
                    if candidate_url:
                        code = _probe_service_endpoint(svc, st, url_override=candidate_url)
                        if code.isdigit() and 200 <= int(code) < 400:
                            state.save(url=candidate_url, ready=True)
                            log(f"[auto-restart] 远端服务探针正常 (HTTP {code})，保持 ready=True 并继续附着保活")
                            return "reattached_ready"
                    if st.get("exec_backend") != "native" and previous_url:
                        run(
                            ["colab", "restart-kernel", "-s", target_svc.name],
                            capture=True,
                            check=False,
                            quiet_capture_on_error=True,
                            env=target_env,
                        )
                    return "ready"
            log(f"[auto-restart] 远端会话 '{target_svc.name}' 已停机，正在清理残留记录并重新申请实例")
            run(["colab", "stop", "-s", target_svc.name], capture=True, check=False, env=target_env)
            args = ["colab", "new", "-s", target_svc.name]
            if target_svc.tpu:
                args += ["--tpu", target_svc.tpu]
            elif target_svc.gpu:
                args += ["--gpu", target_svc.gpu]
            if target_svc.high_mem:
                args += ["--high-mem"]
            log(" ".join(args))
            run(args, capture=True, quiet_capture_on_error=True, env=target_env)
            if mount_drive:
                log("colab dfs mount")
                run(
                    ["colab", "dfs", "mount", "-s", target_svc.name],
                    capture=True,
                    quiet_capture_on_error=True,
                    env=runtime_env,
                )
            return "ready"
        except (CliError, OSError, subprocess.SubprocessError) as err:
            warn(f"[auto-restart] 会话恢复失败：{err}")
            return "failed"


def _compute_backoff_delay(base_delay: float, consecutive_failures: int) -> float:
    if consecutive_failures <= 0:
        return base_delay
    exponent = min(consecutive_failures - 1, 8)
    return min(MAX_RESTART_BACKOFF, base_delay * (2 ** exponent))


def _runtime_tuning(st: dict[str, Any], env_overrides: dict[str, str] | None) -> tuple[float, float, int, int, float]:
    def _get(key: str) -> str | None:
        if env_overrides and key in env_overrides:
            return env_overrides[key]
        return os.environ.get(key)

    saved_idle_check = float(st.get("idle_check_seconds") or DEFAULT_IDLE_CHECK_SECONDS)
    try:
        idle_check_seconds = float(_get("COLABLLM_IDLE_CHECK_SECONDS") or saved_idle_check)
    except ValueError:
        idle_check_seconds = saved_idle_check
    try:
        fast_probe_seconds = float(_get("COLABLLM_FAST_PROBE_SECONDS") or DEFAULT_FAST_PROBE_SECONDS)
    except ValueError:
        fast_probe_seconds = DEFAULT_FAST_PROBE_SECONDS
    try:
        max_startup_retries = max(1, int(_get("COLABLLM_MAX_STARTUP_RETRIES") or DEFAULT_MAX_STARTUP_RETRIES))
    except ValueError:
        max_startup_retries = DEFAULT_MAX_STARTUP_RETRIES
    try:
        preflight_retries = max(0, int(_get("COLABLLM_PREFLIGHT_RETRIES") or DEFAULT_PREFLIGHT_RETRIES))
    except ValueError:
        preflight_retries = DEFAULT_PREFLIGHT_RETRIES
    try:
        raw_delay = _get("COLABLLM_PREFLIGHT_RETRY_DELAY")
        preflight_retry_delay = max(
            0.0, float(raw_delay if raw_delay is not None else DEFAULT_PREFLIGHT_RETRY_DELAY)
        )
    except ValueError:
        preflight_retry_delay = DEFAULT_PREFLIGHT_RETRY_DELAY
    return idle_check_seconds, fast_probe_seconds, max_startup_retries, preflight_retries, preflight_retry_delay


async def supervise_instance_async(
    svc: Service,
    nb_path: Path,
    *,
    owner_pid: int | None = None,
    owner_identity: str | None = None,
    output_sink: Callable[[bytes], None] | None = None,
    env_overrides: dict[str, str] | None = None,
) -> int:
    """Native asyncio WebSocket supervisor matching `exec_runner`'s retry & auto-restart semantics."""
    state = State(svc)
    st = state.load()
    svc.account = svc.account or st.get("account")
    svc.drive_account = svc.drive_account or st.get("drive_account")
    pid = owner_pid if owner_pid is not None else os.getpid()
    identity = owner_identity if owner_identity is not None else process_identity(pid)
    if nb_path.is_file() and st.get("env"):
        with contextlib.suppress(OSError, ValueError):
            current_nb = json.loads(nb_path.read_text(encoding="utf-8"))
            _write_private_notebook(nb_path, current_nb, st["env"])

    merged_env = {**os.environ, **(env_overrides or {})}
    auto_restart = bool(st.get("auto_restart"))
    base_delay = float(st.get("restart_delay") or DEFAULT_RESTART_DELAY)
    max_restarts = int(st.get("max_restarts") or 0)
    (
        idle_check_seconds,
        fast_probe_seconds,
        max_startup_retries,
        preflight_retries,
        preflight_retry_delay,
    ) = _runtime_tuning(st, env_overrides)

    def _emit(chunk: bytes) -> None:
        if not chunk:
            return
        if output_sink is not None:
            output_sink(chunk)
        else:
            sys.stdout.buffer.write(chunk)
            sys.stdout.flush()

    async def _probe_async(target_s: Service, curr_st: dict[str, Any], timeout: float) -> str:
        return await asyncio.to_thread(_probe_service_endpoint, target_s, curr_st, timeout=timeout)

    async def _check_remote_async() -> str:
        return await asyncio.to_thread(check_remote_session, svc, env_overrides=env_overrides)

    async def _refresh_token_async() -> None:
        await asyncio.to_thread(check_remote_session, svc, env_overrides=env_overrides)

    ready_timeout = st.get("ready_timeout", svc.ready_timeout)
    is_foreground = st.get("runner_mode") == "foreground"
    already_ready = bool(st.get("ready") and st.get("url"))
    conn_seq = 0

    if not auto_restart:
        deadline = time.monotonic() + ready_timeout if (is_foreground and not already_ready) else None
        for preflight_attempt in range(preflight_retries + 1):
            conn_seq += 1
            rc, reached_ready, _, preflight_error = await run_single_ws_session(
                svc,
                state,
                nb_path,
                _emit,
                env=merged_env,
                deadline=deadline,
                auto_restart=False,
                idle_check_seconds=0.0,
                fast_probe_seconds=0.0,
                initially_ready=already_ready,
                probe_fn=_probe_async,
                check_remote_fn=_check_remote_async,
                refresh_token_fn=_refresh_token_async,
                conn_seq=conn_seq,
            )
            if rc == 0 or reached_ready or not preflight_error or preflight_attempt >= preflight_retries:
                return rc
            owned, _ = _runner_still_owned(state, pid, identity)
            if not owned:
                return 0
            if await _check_remote_async() == "missing":
                warn(f"远端 Colab 会话 '{svc.name}' 已不存在，停止预连接重试")
                return rc
            state.save(exec_pid=None, exec_identity=None)
            warn(
                f"原生 WebSocket 预连接中断 ({preflight_error})；远端会话仍存活，"
                f"将在 {preflight_retry_delay:g}s 后自动重连 ({preflight_attempt + 1}/{preflight_retries})"
            )
            if preflight_retry_delay > 0:
                await asyncio.sleep(preflight_retry_delay)
        return rc

    ever_ready = already_ready
    consecutive_failures = 0
    restart_count = int(st.get("restart_count") or 0)
    attempt = 0
    reattached_ready = already_ready
    while True:
        deadline = (
            time.monotonic() + ready_timeout
            if (not reattached_ready and (is_foreground or attempt > 0))
            else None
        )
        last_timeout_err: TimeoutError | None = None
        reached_ready = False
        half_open_reclaimed = False
        conn_seq += 1
        try:
            rc, reached_ready, half_open_reclaimed, _ = await run_single_ws_session(
                svc,
                state,
                nb_path,
                _emit,
                env=merged_env,
                deadline=deadline,
                auto_restart=True,
                idle_check_seconds=idle_check_seconds,
                fast_probe_seconds=fast_probe_seconds,
                initially_ready=reattached_ready,
                probe_fn=_probe_async,
                check_remote_fn=_check_remote_async,
                refresh_token_fn=_refresh_token_async,
                conn_seq=conn_seq,
            )
        except TimeoutError as err:
            last_timeout_err = err
            rc = 124
            warn(f"[auto-restart] 等待 CT_READY 超时 ({ready_timeout}s)")

        owned, st = _runner_still_owned(state, pid, identity)
        if not owned:
            return 0

        if reached_ready:
            ever_ready = True
            consecutive_failures = 0
        else:
            consecutive_failures += 1

        if not ever_ready and consecutive_failures >= max_startup_retries:
            warn(f"[auto-restart] 首次启动连续 {consecutive_failures} 次未就绪，停止自动重试")
            if last_timeout_err is not None:
                raise last_timeout_err
            return rc or 1

        if max_restarts > 0 and restart_count >= max_restarts:
            warn(f"[auto-restart] 已达到最大重启次数 ({max_restarts})，停止自动守护")
            if last_timeout_err is not None and not reached_ready:
                raise last_timeout_err
            return rc or 1

        restart_count += 1
        previous_url = st.get("url")
        keep_url = previous_url if st.get("hostname") else None
        st = state.save(
            ready=False,
            url=keep_url,
            exec_pid=None,
            exec_identity=None,
            last_cell_started=False,
            restart_count=restart_count,
        )

        delay = 0.0 if half_open_reclaimed else _compute_backoff_delay(
            base_delay, 0 if reached_ready else consecutive_failures
        )
        if delay > 0:
            log(f"[auto-restart #{restart_count}] 检测到运行时已停止 (exit={rc})，将在 {delay:g}s 后检查并恢复会话")
            await asyncio.sleep(delay)
        else:
            log(f"[auto-restart #{restart_count}] 已确认远端会话停机，立即恢复会话")

        reattached_ready = False
        while True:
            status_result = await asyncio.to_thread(
                _provision_session_for_restart,
                svc,
                state,
                pid,
                identity,
                previous_url=previous_url,
                env_overrides=env_overrides,
            )
            if status_result == "stopped":
                return 0
            if status_result in ("ready", "reattached_ready"):
                reattached_ready = status_result == "reattached_ready"
                break
            consecutive_failures += 1
            if not ever_ready and consecutive_failures >= max_startup_retries:
                warn(f"[auto-restart] 首次启动连续 {consecutive_failures} 次失败，停止自动重试")
                return 1
            retry_delay = _compute_backoff_delay(base_delay, consecutive_failures)
            warn(f"[auto-restart #{restart_count}] 将在 {retry_delay:g}s 后重试会话重建")
            await asyncio.sleep(retry_delay)

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        state.save(started=now_str, last_restart=now_str)
        header = f"\n=== [colabsvc] auto-restart #{restart_count} at {now_str} ({svc.name}) ===\n"
        _emit(header.encode("utf-8"))
        attempt += 1


def exec_runner(svc: Service, nb_path: Path) -> int:
    """Own the Colab connection, reap its process group, and optionally supervise auto-restarts."""
    state = State(svc)
    st = state.load()
    svc.account = svc.account or st.get("account")
    svc.drive_account = svc.drive_account or st.get("drive_account")
    pid, identity = os.getpid(), process_identity(os.getpid())
    st = state.save(runner_pid=pid, runner_identity=identity)
    if nb_path.is_file() and st.get("env"):
        try:
            current_nb = json.loads(nb_path.read_text(encoding="utf-8"))
            _write_private_notebook(nb_path, current_nb, st["env"])
        except (OSError, ValueError):
            pass

    old_handler = signal.getsignal(signal.SIGTERM)
    def terminated(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminated)
    rc = None
    try:
        if st.get("exec_backend") == "native":
            rc = asyncio.run(
                supervise_instance_async(
                    svc,
                    nb_path,
                    owner_pid=pid,
                    owner_identity=identity,
                )
            )
            return rc

        # Legacy subprocess `colab exec` fallback (used when no native session token is available or --exec-backend cli).
        cmd = [
            "colab", "exec", "-s", svc.name, "-f", str(nb_path),
            "--timeout", str(7 * 24 * 3600),
            "--env", f"CT_SERVICE={svc.name}",
        ]
        auto_restart = bool(st.get("auto_restart"))
        base_delay = float(st.get("restart_delay") or DEFAULT_RESTART_DELAY)
        max_restarts = int(st.get("max_restarts") or 0)
        saved_idle_check = float(st.get("idle_check_seconds") or DEFAULT_IDLE_CHECK_SECONDS)
        try:
            idle_check_seconds = float(os.environ.get("COLABLLM_IDLE_CHECK_SECONDS", saved_idle_check))
        except ValueError:
            idle_check_seconds = saved_idle_check
        try:
            fast_probe_seconds = float(os.environ.get("COLABLLM_FAST_PROBE_SECONDS", DEFAULT_FAST_PROBE_SECONDS))
        except ValueError:
            fast_probe_seconds = DEFAULT_FAST_PROBE_SECONDS
        try:
            max_startup_retries = max(1, int(os.environ.get("COLABLLM_MAX_STARTUP_RETRIES", DEFAULT_MAX_STARTUP_RETRIES)))
        except ValueError:
            max_startup_retries = DEFAULT_MAX_STARTUP_RETRIES
        try:
            preflight_retries = max(0, int(os.environ.get("COLABLLM_PREFLIGHT_RETRIES", DEFAULT_PREFLIGHT_RETRIES)))
        except ValueError:
            preflight_retries = DEFAULT_PREFLIGHT_RETRIES
        try:
            preflight_retry_delay = max(0.0, float(os.environ.get("COLABLLM_PREFLIGHT_RETRY_DELAY", DEFAULT_PREFLIGHT_RETRY_DELAY)))
        except ValueError:
            preflight_retry_delay = DEFAULT_PREFLIGHT_RETRY_DELAY
        ready_timeout = st.get("ready_timeout", svc.ready_timeout)
        is_foreground = st.get("runner_mode") == "foreground"
        already_ready = bool(st.get("ready") and st.get("url"))
        if not auto_restart:
            deadline = time.monotonic() + ready_timeout if (is_foreground and not already_ready) else None
            for preflight_attempt in range(preflight_retries + 1):
                rc, reached_ready, _, preflight_error = _run_single_exec(
                    svc,
                    state,
                    cmd,
                    deadline=deadline,
                    auto_restart=False,
                    idle_check_seconds=0.0,
                    initially_ready=already_ready,
                )
                if rc == 0 or reached_ready or not preflight_error or preflight_attempt >= preflight_retries:
                    return rc
                owned, _ = _runner_still_owned(state, pid, identity)
                if not owned:
                    return 0
                if check_remote_session(svc) == "missing":
                    warn(f"远端 Colab 会话 '{svc.name}' 已不存在，停止预连接重试")
                    return rc
                state.save(exec_pid=None, exec_identity=None)
                warn(
                    f"colab exec 预连接中断 ({preflight_error})；远端会话仍存活，"
                    f"将在 {preflight_retry_delay:g}s 后自动重连 ({preflight_attempt + 1}/{preflight_retries})"
                )
                if preflight_retry_delay > 0:
                    time.sleep(preflight_retry_delay)
            return rc

        ever_ready = already_ready
        consecutive_failures = 0
        restart_count = int(st.get("restart_count") or 0)
        attempt = 0
        reattached_ready = already_ready
        while True:
            deadline = (
                time.monotonic() + ready_timeout
                if (not reattached_ready and (is_foreground or attempt > 0))
                else None
            )
            last_timeout_err: TimeoutError | None = None
            reached_ready = False
            half_open_reclaimed = False
            try:
                rc, reached_ready, half_open_reclaimed, _ = _run_single_exec(
                    svc,
                    state,
                    cmd,
                    deadline=deadline,
                    auto_restart=True,
                    idle_check_seconds=idle_check_seconds,
                    fast_probe_seconds=fast_probe_seconds,
                    initially_ready=reattached_ready,
                )
            except TimeoutError as err:
                last_timeout_err = err
                rc = 124
                warn(f"[auto-restart] 等待 CT_READY 超时 ({ready_timeout}s)")

            owned, st = _runner_still_owned(state, pid, identity)
            if not owned:
                rc = 0
                return 0

            if reached_ready:
                ever_ready = True
                consecutive_failures = 0
            else:
                consecutive_failures += 1

            if not ever_ready and consecutive_failures >= max_startup_retries:
                warn(f"[auto-restart] 首次启动连续 {consecutive_failures} 次未就绪，停止自动重试")
                if last_timeout_err is not None:
                    raise last_timeout_err
                return rc or 1

            if max_restarts > 0 and restart_count >= max_restarts:
                warn(f"[auto-restart] 已达到最大重启次数 ({max_restarts})，停止自动守护")
                if last_timeout_err is not None and not reached_ready:
                    raise last_timeout_err
                return rc or 1

            restart_count += 1
            previous_url = st.get("url")
            keep_url = previous_url if st.get("hostname") else None
            st = state.save(
                ready=False,
                url=keep_url,
                exec_pid=None,
                exec_identity=None,
                last_cell_started=False,
                restart_count=restart_count,
            )

            delay = 0.0 if half_open_reclaimed else _compute_backoff_delay(
                base_delay, 0 if reached_ready else consecutive_failures
            )
            if delay > 0:
                log(f"[auto-restart #{restart_count}] 检测到运行时已停止 (exit={rc})，将在 {delay:g}s 后检查并恢复会话")
                time.sleep(delay)
            else:
                log(f"[auto-restart #{restart_count}] 已确认远端会话停机，立即恢复会话")

            reattached_ready = False
            while True:
                status_result = _provision_session_for_restart(
                    svc, state, pid, identity, previous_url=previous_url
                )
                if status_result == "stopped":
                    rc = 0
                    return 0
                if status_result in ("ready", "reattached_ready"):
                    reattached_ready = status_result == "reattached_ready"
                    break
                consecutive_failures += 1
                if not ever_ready and consecutive_failures >= max_startup_retries:
                    warn(f"[auto-restart] 首次启动连续 {consecutive_failures} 次失败，停止自动重试")
                    return 1
                retry_delay = _compute_backoff_delay(base_delay, consecutive_failures)
                warn(f"[auto-restart #{restart_count}] 将在 {retry_delay:g}s 后重试会话重建")
                time.sleep(retry_delay)

            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            state.save(started=now_str, last_restart=now_str)
            header = f"\n=== [colabsvc] auto-restart #{restart_count} at {now_str} ({svc.name}) ===\n"
            sys.stdout.buffer.write(header.encode("utf-8"))
            sys.stdout.flush()
            attempt += 1
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        finish_runner(state, pid, identity, rc)


# --------------------------------------------------------------------------- commands
_NON_RETRYABLE_ASSIGN_RE = re.compile(
    r"precondition failed|allocation refused|toomanyassignmentserror|"
    r"backend rejected accelerator|bad request|unauthorized|forbidden|"
    r"not authenticated|invalid_grant|unknown accelerator|invalid value",
    re.IGNORECASE,
)
_RETRYABLE_ASSIGN_RE = re.compile(
    r"service unavailable|\b503\b|bad gateway|\b502\b|gateway time-?out|\b504\b|"
    r"too many requests|\b429\b|internal server error|\b500\b|"
    r"temporarily unavailable|readtimeout|read timed out|connecttimeout|"
    r"timed out|connectionerror|connection reset|connection aborted|"
    r"remotedisconnected|nameresolutionerror|temporary failure in name resolution|"
    r"sslerror|eof occurred in violation of protocol",
    re.IGNORECASE,
)


def _is_retryable_assign_error(text: str) -> bool:
    if not text:
        return False
    if _NON_RETRYABLE_ASSIGN_RE.search(text):
        return False
    return bool(_RETRYABLE_ASSIGN_RE.search(text))


def _assign_retry_settings(retries: int | None = None, retry_delay: float | None = None) -> tuple[int, float]:
    if retries is None:
        raw_retries = os.environ.get("COLABLLM_ASSIGN_RETRIES")
        if raw_retries is not None and raw_retries.strip():
            try:
                retries = nonnegative_int(raw_retries.strip())
            except (ValueError, argparse.ArgumentTypeError) as err:
                raise UsageError(f"COLABLLM_ASSIGN_RETRIES 无效: {err}") from None
        else:
            retries = DEFAULT_ASSIGN_RETRIES
    if retry_delay is None:
        raw_delay = os.environ.get("COLABLLM_ASSIGN_RETRY_DELAY")
        if raw_delay is not None and raw_delay.strip():
            try:
                retry_delay = nonnegative_seconds(raw_delay.strip())
            except ValueError as err:
                raise UsageError(f"COLABLLM_ASSIGN_RETRY_DELAY 无效: {err}") from None
        else:
            retry_delay = DEFAULT_ASSIGN_RETRY_DELAY
    return int(retries), float(retry_delay)


def _assign_backoff_delay(base_delay: float, failed_attempt: int) -> float:
    if base_delay <= 0:
        return 0.0
    return min(MAX_ASSIGN_RETRY_BACKOFF, base_delay * (1.5 ** max(0, failed_attempt - 1)))


def ensure_session(
    svc: Service,
    *,
    force_new: bool = False,
    assign_retries: int | None = None,
    assign_retry_delay: float | None = None,
) -> bool:
    env = colab_env_for(svc)

    def _has_recorded_session() -> bool:
        try:
            sessions = json.loads(account_sessions_path(svc.account).read_text(encoding="utf-8"))
        except Exception:
            sessions = {}
        return svc.name in sessions

    if not force_new and _has_recorded_session():
        if check_remote_session(svc) != "missing":
            log(f"reusing Colab session '{svc.name}'")
            return True
        log(f"stale Colab session '{svc.name}' was reclaimed remotely; recreating")
        run(["colab", "stop", "-s", svc.name], capture=True, check=False, env=env)
    args = ["colab", "new", "-s", svc.name]
    if svc.tpu:
        args += ["--tpu", svc.tpu]
    elif svc.gpu:
        args += ["--gpu", svc.gpu]
    if svc.high_mem:
        args += ["--high-mem"]
    retries, base_delay = _assign_retry_settings(assign_retries, assign_retry_delay)
    if retries > 0:
        assign_env = dict(env if env is not None else os.environ)
        assign_env.setdefault("COLAB_CLI_ASSIGN_RETRIES", "1")
        assign_env.setdefault("COLAB_CLI_RETRY_BACKOFF", "5")
    else:
        assign_env = env
    for attempt in range(1, retries + 2):
        if attempt > 1 and not force_new and _has_recorded_session():
            if check_remote_session(svc) != "missing":
                log(f"reusing Colab session '{svc.name}'")
                return True
            run(["colab", "stop", "-s", svc.name], capture=True, check=False, env=env)
        log(" ".join(args) if attempt == 1 else f"{' '.join(args)} (第 {attempt}/{retries + 1} 次尝试)")
        try:
            res = run(args, capture=True, quiet_capture_on_error=True, env=assign_env)
            stdout_text = getattr(res, "stdout", None)
            if isinstance(stdout_text, str):
                for line in stdout_text.splitlines():
                    if line.strip():
                        diagnostic(line)
            return False
        except (CliError, subprocess.TimeoutExpired) as err:
            if isinstance(err, subprocess.TimeoutExpired):
                retryable = True
                reason = f"命令超时 ({err.timeout}s)"
            else:
                combined = "\n".join(
                    part for part in (str(err), getattr(err, "stderr", "") or "", getattr(err, "stdout", "") or "")
                    if part
                )
                retryable = _is_retryable_assign_error(combined)
                reason = str(err)
            if not retryable or attempt > retries:
                if isinstance(err, subprocess.TimeoutExpired):
                    raise CliError(f"申请 Colab VM 超时 ({err.timeout}s): {' '.join(args)}", 124) from None
                raise
            delay = _assign_backoff_delay(base_delay, attempt)
            warn(f"申请 Colab VM 暂时失败（{reason}），{delay:g}s 后重试（第 {attempt + 1}/{retries + 1} 次）")
            if delay > 0:
                time.sleep(delay)
    return False


def cmd_up(svc: Service, script: Path, a) -> None:
    state = State(svc)
    if a.dry_run:
        return _start_up(svc, script, a)
    with state.lock("operation", blocking=False):
        launched = _start_up(svc, script, a)
    if a.foreground:
        try:
            raise SystemExit(exec_runner(svc, launched))
        finally:
            if state.load().get("exec_pid") is None:
                finish_runner(state, os.getpid(), process_identity(os.getpid()))
    if getattr(a, "detach", False):
        show_info(svc, reveal=False)
        return
    _wait_ready(svc, launched, timeout=getattr(a, "ready_timeout", None))


def _start_up(svc: Service, script: Path, a):
    auto_restart = bool(getattr(a, "auto_restart", False))
    restart_delay = getattr(a, "restart_delay", None)
    idle_check = getattr(a, "idle_check", None)
    max_restarts = int(getattr(a, "max_restarts", 0) or 0)
    if (restart_delay is not None or idle_check is not None or max_restarts > 0) and not auto_restart:
        raise ValueError("--restart-delay、--idle-check 与 --max-restarts 需配合 --auto-restart 使用")
    explicit_drive = getattr(a, "drive_account", None)
    if explicit_drive and a.no_drive:
        raise ValueError("--drive-account 不能与 --no-drive 同时使用")
    hostname = tunnel_hostname(svc, a)
    transport=tunnel_transport(a)
    frp_config=None
    if transport=='frp':
        frp_config=load_tunnel_config().get('frp')
        if not frp_config or not frp_config.get('server_ip') or not hostname:
            raise ValueError('FRP requires server configuration and a public domain; run colabllm frp-config')
        from frp_backend import validate_config
        frp_config=validate_config({**frp_config,'enabled':True})
    if a.gpu:
        svc.gpu = None if a.gpu.lower() in ("none", "cpu") else a.gpu
    for tool in ("colab",) + (("cf",) if hostname else ()) + (("ssh",) if transport=='frp' else ()):
        if not shutil.which(tool):
            die(f"`{tool}` CLI not found")
    state = State(svc)
    st = state.load()
    busy = {8080: "Colab runtime server", 8012: "colab dfs creds shim"}
    for port in {svc.service_port, svc.tunnel_port} & busy.keys():
        die(f"port {port} is used on Colab VMs by the {busy[port]}; pick another port in services/{svc.name}.py")
    if runner_alive(st):
        die(f"'{svc.name}' is already running (runner pid {st['runner_pid']}); use `down` first")

    acct, _ = resolve_effective_account(
        explicit=svc.account,
        saved=st.get("account"),
        prefer_saved=False,
        reattach_session=svc.name,
    )
    svc.account = acct
    if a.no_drive:
        svc.drive_account = None
    else:
        saved_drv = st.get("drive_account") if (acct and acct == st.get("account")) else None
        drv, _ = resolve_effective_drive_account(
            acct,
            explicit_drive=explicit_drive,
            saved_drive=saved_drv,
            prefer_saved=False,
        )
        svc.drive_account = drv

    nb_src = (script.parent / svc.notebook).resolve()
    skip = {int(x) for x in a.skip.split(",")} if a.skip else set()
    built, plan = build_notebook(nb_src, skip)
    acct_desc = ""
    if svc.account:
        acct_desc = f", account {svc.account}"
        if svc.drive_account and svc.drive_account != svc.account:
            acct_desc += f" (drive {svc.drive_account})"
    log(f"service {svc.name}: notebook {nb_src.name}, gpu {svc.tpu or svc.gpu or 'cpu'}, auth {svc.auth}, "
        f"transport {transport}, {'domain ' + hostname if hostname else 'quick tunnel'}{acct_desc}")
    print("\n".join(plan))
    if a.dry_run:
        return

    private_dir(STATE_ROOT)
    private_dir(state.dir.parent)
    private_dir(state.dir)
    nb_path = state.dir / nb_src.name

    # credentials: stable across restarts, regenerated with --new-credentials
    if a.new_credentials or not st.get("auth_pass"):
        st = state.save(auth_user=svc.auth_user, auth_pass=secrets.token_urlsafe(18),
                        api_token="sk-" + secrets.token_urlsafe(32))
    env = {"CT_SERVICE": svc.name, "CT_SERVICE_PORT": str(svc.service_port),
           "CT_PUBLIC_PORT": str(svc.tunnel_port), "CT_AUTH": svc.auth,
           "CT_API_TOKEN": st["api_token"],
           "CT_MOUNT_DRIVE": "0" if a.no_drive else "1",
           "CT_DATA_DIR": (
               f"/content/local-data/{svc.base_name or svc.name}"
               if a.no_drive else f"/content/drive/MyDrive/colab-llm/{svc.base_name or svc.name}"
           ),
           **svc.env}
    # Explicitly clear previous notebook-kernel values when switching to quick
    # tunnels; reused Colab kernels retain env vars from earlier executions.
    env.update(CT_TRANSPORT=transport,CT_TUNNEL_TOKEN="", CT_PUBLIC_HOSTNAME="")
    for key in ('SERVER_IP','SERVER_PORT','REMOTE_PORT','PROXY_NAME','TOKEN','CA_CERT','VERSION','SERVER_NAME','AUTH_SCOPES'):
        env['CT_FRP_'+key]=''
    host_hf_tok = None
    for p in (Path.home() / ".cache/huggingface/token", Path.home() / ".huggingface/token"):
        if p.is_file() and p.read_text().strip():
            host_hf_tok = p.read_text().strip()
            break
    hf_tok = (
        a.hf_token
        or os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        or host_hf_tok
        or (None if a.new_credentials else st.get("hf_token"))
    )
    if a.hf_token:
        st = state.save(hf_token=a.hf_token)
    elif a.new_credentials and "hf_token" in st:
        st = state.update(lambda current: {k: v for k, v in current.items() if k != "hf_token"})
    if hf_tok:
        env.update(HF_TOKEN=hf_tok, HUGGING_FACE_HUB_TOKEN=hf_tok)
    if svc.auth == "caddy" or (svc.base_name or svc.name) == "unsloth-studio":
        env.update(CT_AUTH_USER=st["auth_user"], CT_AUTH_PASS=st["auth_pass"])

    # 1. named tunnel + DNS (local, cf CLI)
    if transport=='frp':
        from frp_backend import ensure_route
        if st.get('frp_route'):
            previous=st['frp_route']
            if previous['hostname']!=hostname or any(previous['frp'].get(k)!=frp_config.get(k)
                for k in ('server_ip','server_port','ssh','project_id')):
                raise ValueError('Existing FRP route targets another endpoint; use down --delete-tunnel first')
        route,frp_env=ensure_route(svc,frp_config,hostname,STATE_ROOT)
        env.update(frp_env)
        st=state.save(transport=transport,hostname=hostname,frp_route=route)
    elif hostname:
        info = ensure_named_tunnel(svc, hostname)
        env.update(CT_TUNNEL_TOKEN=info["token"], CT_PUBLIC_HOSTNAME=hostname)
        route={k: v for k, v in info.items() if k != 'token'}
        st = state.save(transport=transport,cloudflare_route=route,**route)
    else:
        st=state.save(transport=transport)
    _write_private_notebook(nb_path, built, env)
    # 2. session + 3. Drive
    previous_url = st.get("url")
    reused_session = ensure_session(
        svc,
        assign_retries=getattr(a, "assign_retries", None),
        assign_retry_delay=getattr(a, "assign_retry_delay", None),
    ) is True
    initial_url = None
    initial_ready = False
    if reused_session and not a.new_credentials:
        candidate_url = f"https://{hostname}" if hostname else previous_url
        if candidate_url:
            code = _probe_service_endpoint(svc, st, url_override=candidate_url, timeout=5.0)
            if code.isdigit() and 200 <= int(code) < 400:
                log(f"远端服务已在运行 (HTTP {code})，直接复用现有实例并接管保活")
                initial_url = candidate_url
                initial_ready = True
    if not a.no_drive and not initial_ready:
        drv_note = f" (drive {svc.drive_account})" if svc.drive_account and svc.drive_account != svc.account else ""
        log(f"colab dfs mount{drv_note}")
        run(["colab", "dfs", "mount", "-s", svc.name], env=colab_runtime_env_for(svc))
    req_backend = getattr(a, "exec_backend", None) or os.environ.get("COLABLLM_EXEC_BACKEND") or "auto"
    use_native = (req_backend == "native") or (
        req_backend == "auto" and has_native_colab_session(svc.name, svc.account)
    )
    use_mux = (
        use_native
        and not bool(getattr(a, "no_mux", False))
        and not bool(os.environ.get("COLABLLM_NO_MUX"))
    )

    st = state.save(
        base_service=svc.base_name or svc.name,
        account=svc.account,
        drive_account=svc.drive_account,
        env=env, url=initial_url, ready=initial_ready, last_cell_started=False, login_user=None, login_pass=None,
        runner_exit=None, ready_timeout=getattr(a, "ready_timeout", None) or svc.ready_timeout,
        started=time.strftime("%Y-%m-%d %H:%M:%S"),
        session_gpu=svc.gpu, session_tpu=svc.tpu, session_high_mem=bool(svc.high_mem),
        mount_drive=not a.no_drive,
        auto_restart=auto_restart,
        restart_delay=float(restart_delay if restart_delay is not None else DEFAULT_RESTART_DELAY),
        idle_check_seconds=float(idle_check if idle_check is not None else DEFAULT_IDLE_CHECK_SECONDS),
        max_restarts=max_restarts,
        restart_count=0,
        last_restart=None,
        exec_backend=("native" if use_native else "cli"),
    )

    # 4. notebook through native WebSocket mux or attached `colab exec` (keep-alive)
    if a.foreground:
        state.save(runner_pid=os.getpid(), runner_identity=process_identity(os.getpid()), runner_mode="foreground")
        return nb_path
    state.log_path.write_bytes(b"")
    state.log_path.chmod(0o600)
    if use_mux:
        env_overrides = {
            k: v
            for k, v in os.environ.items()
            if k.startswith(("COLABLLM_", "COLAB_"))
            or k in ("PATH", "HOME", "XDG_CONFIG_HOME", "REQUEST_TIMEOUT", "FAKE_COLAB_ROOT", "FAKE_COLAB_URL")
        }
        return mux.start_in_mux(
            svc,
            nb_path,
            state_root=STATE_ROOT,
            exec_backend="native",
            env_overrides=env_overrides,
        )
    exec_cmd = [sys.executable, str(script), *(["-i", svc.instance] if svc.instance else []), "_exec", str(nb_path)]
    with state.log_path.open("ab") as logfile:
        proc = subprocess.Popen(exec_cmd, stdin=subprocess.DEVNULL, stdout=logfile,
                                stderr=subprocess.STDOUT, start_new_session=True)
    identity = process_identity(proc.pid)
    if not identity:
        raise RuntimeError(f"后台进程在登记前退出；请查看 {state.log_path}")
    state.save(runner_pid=proc.pid, runner_identity=identity, runner_mode="background")
    return proc


def _wait_ready(svc, proc, timeout=None):
    state = State(svc)
    mode = state.load().get("runner_mode")
    if mode == "mux":
        log(f"runner started in shared mux (pid {proc.pid}) -- native WebSocket keep-alive; log: {state.log_path}")
    else:
        log(f"runner started (pid {proc.pid}) -- it keeps `colab exec` attached; log: {state.log_path}")
    log("streaming progress until the service reports CT_READY (Ctrl-C only stops watching)")
    offset, deadline = 0, time.monotonic() + (svc.ready_timeout if timeout is None else timeout)

    def _flush_log():
        nonlocal offset
        if not state.log_path.is_file():
            return
        with open(state.log_path, "rb") as f:
            f.seek(offset)
            data = f.read()
        offset += len(data)
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()

    try:
        while time.monotonic() < deadline:
            _flush_log()
            st = state.load()
            if st.get("ready") and st.get("url"):
                break
            if proc.poll() is not None:
                _flush_log()
                st = state.load()
                if st.get("ready") and st.get("url"):
                    break
                die(f"runner exited early (code {proc.returncode}); see {state.log_path}")
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        else:
            raise TimeoutError("等待 CT_READY 超时；后台进程继续运行，请使用 status 或 logs -f 检查")
    except KeyboardInterrupt:
        raise
    show_info(svc, do_probe=True, reveal=False)


def show_info(svc: Service, do_probe=False, reveal=False, output_json=False, remote_session: str | None = None):
    st = State(svc).load()
    alive = runner_alive(st)
    ready = bool(alive and st.get("ready") and remote_session != "missing")
    acct = svc.account or st.get("account")
    drv = svc.drive_account or (st.get("drive_account") if (not svc.account or svc.account == st.get("account")) else None)
    data = dict(service=svc.name, running=alive, ready=ready, url=st.get("url"))
    if acct:
        data["account"] = acct
    if drv:
        data["drive_account"] = drv
    mode_suffix = ", auto-restart" if st.get("auto_restart") else ""
    lines = [f"service : {svc.name}   (runner {'alive' if alive else 'stopped'}{mode_suffix})"]
    if acct:
        drv_note = f"   (drive: {drv})" if drv and drv != acct else ""
        lines.append(f"account : {acct}{drv_note}")
    lines.append(f"ready   : {ready}")
    if st.get("auto_restart"):
        data["auto_restart"] = True
        data["restart_count"] = int(st.get("restart_count") or 0)
        if st.get("restart_count"):
            lines.append(f"restarts: {st['restart_count']}")
    if remote_session == "missing":
        data["remote_session"] = "missing"
        lines.append("colab   : session not found")
    if st.get("url"):
        lines.append(f"URL     : {st['url']}")
    if reveal:
        for key in _service_secret_keys(svc):
            if st.get(key):
                data[key] = st[key]
                lines.append(f"{key}: {st[key]}")
    healthy = ready
    if do_probe and st.get("url"):
        result = _probe_service_endpoint(svc, st)
        data["http_status"] = result
        lines.append(f"probe   : {result}")
        healthy = bool(ready and result.isdigit() and 200 <= int(result) < 400)
    elif do_probe:
        healthy = False
    if output_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        banner(lines)
    return healthy


_ALL_SECRET_KEYS = ("auth_user", "auth_pass", "api_token", "login_user", "login_pass")


def _service_secret_keys(svc: Service) -> tuple[str, ...]:
    if (svc.base_name or svc.name) == "unsloth-studio":
        return ("login_user", "login_pass")
    if svc.auth == "caddy":
        return _ALL_SECRET_KEYS
    return ("api_token", "login_user", "login_pass")


def show_instances(svc: Service, reveal=False, output_json=False):
    items = list_service_instances(svc.base_name or svc.name)
    hidden = set(_ALL_SECRET_KEYS) if not reveal else (set(_ALL_SECRET_KEYS) - set(_service_secret_keys(svc)))
    if hidden:
        items = [{key: value for key, value in item.items() if key not in hidden}
                 for item in items]
    if output_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        log(f"no instances found for '{svc.base_name or svc.name}'")
        return
    print(f"{'INSTANCE':<12} {'ACCOUNT':<18} {'SESSION':<28} {'STATE':<12} URL" + (" / CREDENTIAL" if reveal else ""))
    for item in items:
        label = "READY" if item["ready"] else ("STARTING" if item["alive"] else "STOPPED")
        acct = item.get("account")
        drv = item.get("drive_account")
        acct_col = f"{acct}->{drv}" if (acct and drv and drv != acct) else (acct or drv or "-")
        if reveal:
            cred = (
                item.get("api_token")
                or (f"{item['login_user']}:{item['login_pass']}" if item.get("login_user") and item.get("login_pass") else "")
                or (f"{item['auth_user']}:{item['auth_pass']}" if item.get("auth_user") and item.get("auth_pass") else "")
                or "-"
            )
            suffix = f" / {cred}"
        else:
            suffix = ""
        print(f"{item['instance']:<12} {acct_col:<18} {item['name']:<28} {label:<12} {item.get('url') or '-'}{suffix}")


def cmd_down(svc: Service, a) -> None:
    if getattr(a, "all", False):
        items = list_service_instances(svc.base_name or svc.name)
        target_filter = resolve_effective_account(explicit=svc.account)[0] if svc.account else None
        errors: list[tuple[str, Exception]] = []
        for it in items:
            if target_filter and it.get("account") and it.get("account") != target_filter:
                continue
            sub_svc = dataclasses.replace(svc, account=target_filter, drive_account=None)
            apply_instance(sub_svc, it["instance_arg"] or None)
            sub_a = argparse.Namespace(delete_tunnel=a.delete_tunnel, all=False)
            try:
                cmd_down(sub_svc, sub_a)
            except (CliError, RuntimeError, OSError) as err:
                errors.append((sub_svc.name, err))
        if errors:
            summary = "; ".join(f"{name}: {err}" for name, err in errors)
            code = errors[0][1].code if isinstance(errors[0][1], CliError) else 1
            raise CliError(f"部分实例停止失败：{summary}", code)
        return
    state = State(svc)
    with state.lock("operation", blocking=False):
        st = state.load()
        acct, _ = resolve_effective_account(explicit=svc.account, saved=st.get("account"), prefer_saved=True)
        drv = st.get("drive_account") if (not svc.account or acct == st.get("account")) else None
        target_env = colab_subprocess_env(acct, drv)
        log(f"colab stop -s {svc.name}" + (f" (account {acct})" if acct else ""))
        result = run(["colab", "stop", "-s", svc.name], check=False, env=target_env)
        if result.returncode != 0:
            raise CliError(f"停止 {svc.name} 失败；已保留运行状态，可重试 down", result.returncode if result.returncode > 0 else 1)
        stop_owned_process(st.get("exec_pid"), st.get("exec_identity"), group=True)
        if st.get("runner_mode") == "mux":
            mux.stop_in_mux(svc.name, state, state_root=STATE_ROOT)
        else:
            stop_owned_process(st.get("runner_pid"), st.get("runner_identity"), group=st.get("runner_mode") == "background")
        if a.delete_tunnel and st.get('frp_route'):
            from frp_backend import remove_route
            remove_route(st['frp_route'],svc.name)
            st.pop('frp_route',None)
            if not (st.get("tunnel_id") or st.get("cloudflare_route")):
                st.pop("hostname", None)
                st.pop("transport", None)
        if a.delete_tunnel and (st.get("tunnel_id") or st.get('cloudflare_route')):
            delete_named_tunnel(st.get('cloudflare_route') or st)
            st.pop('cloudflare_route',None)
            drop = {"tunnel_id", "tunnel_name", "zone_id"}
            if not st.get("frp_route"):
                drop.update({"hostname", "transport"})
            st = {k: v for k, v in st.items() if k not in drop}
        keys = ("base_service", "auth_user", "auth_pass", "api_token", "hf_token", "tunnel_id", "tunnel_name",
                "zone_id", "hostname", "transport", "frp_route", "cloudflare_route")
        state.update(lambda current: {k: st[k] for k in keys if k in st})


def build_parser(svc: Service, configure=None):
    p = argparse.ArgumentParser(prog=f"colabllm {svc.base_name}", description=(sys.modules["__main__"].__doc__ or "").strip(),
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    p.add_argument("-i", "--instance", metavar="ID", type=validate_instance,
                   help="instance ID to create/manage multiple parallel instances (e.g. -i 1, -i 2)")
    p.add_argument("--account", metavar="ACCOUNT", type=validate_account,
                   help="Colab account name or email (from `colab account list`, or env COLABLLM_ACCOUNT / COLAB_ACCOUNT)")
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up", help="session -> Drive -> (cf tunnel) -> run notebook, keep it alive")
    u.add_argument("-a", "--account", default=argparse.SUPPRESS, metavar="ACCOUNT", type=validate_account,
                   help="Colab account name or email for this session")
    u.add_argument("--drive-account", metavar="ACCOUNT", type=validate_account,
                   help="shared Google Drive storage account to mount")
    tunnel = u.add_mutually_exclusive_group()
    tunnel.add_argument("--domain", nargs="?", const=True, metavar="HOST", type=validate_domain,
                        help="完整固定域名；省略 HOST 时使用已配置的主域名，未配置则使用动态 URL")
    tunnel.add_argument("--base-domain", metavar="DOMAIN", type=validate_domain,
                        help="本次启动的主域名，如 example.com，自动生成 <service>.example.com")
    tunnel.add_argument("--quick-tunnel", action="store_true", help="忽略主域名配置，强制使用动态 trycloudflare.com URL")
    u.add_argument('--transport',choices=('cloudflare','frp'),help='本次启动覆盖全局隧道后端')
    u.add_argument("--gpu", help="override GPU (T4|L4|G4|H100|A100|none)")
    u.add_argument("--skip", default="", help="1-based code cells to skip, e.g. 1,2")
    u.add_argument("--no-drive", action="store_true", help="do not mount Google Drive")
    u.add_argument("--hf-token", metavar="TOKEN",
                   help="Hugging Face token (saved in ~/.colabllm and forwarded as HF_TOKEN; or set env HF_TOKEN)")
    u.add_argument("--new-credentials", action="store_true", help="rotate the Caddy password / API token")
    mode = u.add_mutually_exclusive_group()
    mode.add_argument("--foreground", action="store_true", help="stay attached in this terminal instead")
    mode.add_argument("--detach", action="store_true", help="return after starting the runner without waiting for readiness")
    u.add_argument("--auto-restart", action="store_true",
                   help="守护模式：监听运行时停机或断连并自动重连/重建会话")
    u.add_argument("--restart-delay", type=positive_seconds, metavar="SECONDS",
                   help="自动重启基础等待秒数，连续失败时指数退避（默认: 3）")
    u.add_argument("--idle-check", type=positive_seconds, metavar="SECONDS",
                   help="半开连接静默巡检秒数，超时无输出时主动核实远端会话（默认: 300）")
    u.add_argument("--max-restarts", type=nonnegative_int, default=0, metavar="N",
                   help="最大自动重启次数，0 表示就绪后不限次数（默认: 0）")
    u.add_argument("--ready-timeout", type=positive_seconds, help="seconds to wait for readiness (default: service-specific)")
    u.add_argument("--assign-retries", type=nonnegative_int, default=None, metavar="N",
                   help="申请 Colab VM 遇临时不可用(503/超时/网络波动)时的最大重试次数（默认: 5，或 COLABLLM_ASSIGN_RETRIES；0 不重试）")
    u.add_argument("--assign-retry-delay", type=nonnegative_seconds, default=None, metavar="SECONDS",
                   help="申请 Colab VM 重试基础等待秒数，逐步退避至 30s（默认: 10，或 COLABLLM_ASSIGN_RETRY_DELAY）")
    u.add_argument("--exec-backend", choices=("auto", "native", "cli"), default="auto",
                   help="保活执行后端：auto 优先使用原生轻量 WebSocket 多路复用，无会话令牌时回退 colab exec（默认: auto）")
    u.add_argument("--no-mux", action="store_true",
                   help="禁用单进程多路复用守护，为当前实例单独启动后台进程")
    u.add_argument("--dry-run", action="store_true", help="only show the cell plan")
    d = sub.add_parser("down", help="stop runner + Colab session")
    d.add_argument("--delete-tunnel", action="store_true", help="also delete the named tunnel and its DNS record")
    d.add_argument("--all", action="store_true", help="stop all instances of this service")
    for name, help_text in (("info", "URL and state"), ("ps", "list instances"), ("list", "alias for ps"), ("status", "Colab status and authenticated health probe")):
        output = sub.add_parser(name, help=help_text)
        output.add_argument("--json", action="store_true", help="output machine-readable JSON")
        output.add_argument("--show-secrets", action="store_true", help="include credentials in output")
    lg = sub.add_parser("logs", help="notebook output")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("-n", "--lines", type=int, default=80)
    tc = sub.add_parser("tunnel-config", help="查看或设置所有服务共享的 Cloudflare 主域名；未设置时使用动态 URL")
    settings = tc.add_mutually_exclusive_group()
    settings.add_argument("--base-domain", metavar="DOMAIN", type=validate_domain)
    settings.add_argument("--clear", action="store_true", help="移除持久主域名配置，恢复动态 URL 默认值")
    p.add_argument("--command-timeout", type=positive_seconds, help="external command timeout in seconds (default: 300, or COLABLLM_COMMAND_TIMEOUT)")
    p.add_argument("--debug", action="store_true", help="show exception tracebacks")
    p.add_argument("--quiet", action="store_true", help="suppress diagnostic progress")
    p.add_argument("--no-color", action="store_true", help="disable diagnostic colors")
    if configure:
        configure(p, sub, u)
    public = list(sub.choices)
    sub.metavar = "{" + ",".join(public) + "}"
    ex = sub.add_parser("_exec")
    ex.add_argument("nb")
    return p


def main(svc: Service, *, configure=None, handlers=None, prepare_up=None) -> None:
    script = Path(sys.argv[0]).resolve()
    inst, argv_after_inst = extract_instance_arg(sys.argv[1:])
    acct, cleaned_argv = extract_account_arg(argv_after_inst)
    apply_instance(svc, inst)
    svc.account = acct
    svc.drive_account = None
    p = build_parser(svc, configure)
    a = p.parse_args(cleaned_argv)
    if getattr(a, "instance", None):
        apply_instance(svc, a.instance)
    if getattr(a, "account", None):
        svc.account = a.account
    if getattr(a, "drive_account", None):
        svc.drive_account = a.drive_account
    if handlers and a.cmd in handlers:
        code = handlers[a.cmd](a)
        if isinstance(code, int) and code:
            raise SystemExit(code)
        return

    if a.cmd == "up":
        try:
            if prepare_up:
                prepare_up(a)
            cmd_up(svc, script, a)
        except ValueError as error:
            p.error(str(error))
    elif a.cmd == "tunnel-config":
        try:
            cmd_tunnel_config(a)
        except (ValueError, OSError) as error:
            p.error(str(error))
    elif a.cmd == "_exec":
        sys.exit(exec_runner(svc, Path(a.nb)))
    elif a.cmd == "down":
        cmd_down(svc, a)
    elif a.cmd == "info":
        show_info(svc, reveal=a.show_secrets, output_json=a.json)
    elif a.cmd in ("ps", "list"):
        show_instances(svc, reveal=a.show_secrets, output_json=a.json)
    elif a.cmd == "status":
        st_curr = State(svc).load()
        explicit_acct = svc.account
        resolved_acct, _ = resolve_effective_account(
            explicit=explicit_acct,
            saved=st_curr.get("account"),
            prefer_saved=True,
        )
        svc.account = resolved_acct
        svc.drive_account = (
            st_curr.get("drive_account")
            if (not explicit_acct or resolved_acct == st_curr.get("account"))
            else None
        )
        result = run(["colab", "status", "-s", svc.name], capture=True, check=False, env=colab_env_for(svc))
        if result.returncode != 0:
            raise CliError((result.stderr or "Colab 状态查询失败").strip(), result.returncode if result.returncode > 0 else 1)
        remote_state = parse_colab_session_status(svc.name, result.returncode, result.stdout or "", result.stderr or "")
        if not show_info(svc, do_probe=True, reveal=a.show_secrets, output_json=a.json, remote_session=remote_state):
            raise SystemExit(1)
    elif a.cmd == "logs":
        lp = State(svc).log_path
        if not lp.exists():
            die(f"no log at {lp}")
        os.execvp("tail", ["tail", "-n", str(a.lines), *(["-f"] if a.follow else []), str(lp)])
