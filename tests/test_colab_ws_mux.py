"""Tests for the pure-stdlib Colab WebSocket client and single-process shared mux daemon."""
from __future__ import annotations

import base64
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/colabllm/lib"))

import colab_ws  # noqa: E402
import mux  # noqa: E402
from processes import owned_process, stop_owned_process  # noqa: E402

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_unmask_frame(sock: socket.socket) -> tuple[bool, int, bytes] | None:
    def _recv_exact(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("closed")
            buf.extend(chunk)
        return bytes(buf)

    try:
        head = _recv_exact(2)
    except EOFError:
        return None
    b0, b1 = head[0], head[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = int.from_bytes(_recv_exact(2), "big")
    elif length == 127:
        length = int.from_bytes(_recv_exact(8), "big")
    mask = _recv_exact(4) if masked else b""
    payload = _recv_exact(length) if length else b""
    if masked and payload:
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return fin, opcode, payload


def _ws_send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n <= 0xFFFF:
        header.append(126)
        header.extend(n.to_bytes(2, "big"))
    else:
        header.append(127)
        header.extend(n.to_bytes(8, "big"))
    sock.sendall(bytes(header) + payload)


def _encode_ws_binary_jupyter(msg: dict) -> bytes:
    """Encode a Jupyter message using JupyterSubprotocol.DEFAULT binary header framing."""
    json_bytes = json.dumps(msg).encode("utf-8")
    nbufs = 1
    header = (
        nbufs.to_bytes(4, "big")
        + (4 * (nbufs + 1)).to_bytes(4, "big")
    )
    return header + json_bytes


class _MockColabRuntimeServer:
    """Mock Jupyter Kernel HTTP + RFC 6455 WebSocket server for native WS/mux tests."""

    def __init__(self, root: Path, health_url: str) -> None:
        self.root = root
        self.health_url = health_url
        self.pings_received = 0
        self.active_ws_sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self.kernels_by_token: dict[str, str] = {}
        self.executed_codes: list[str] = []
        srv_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _check_auth(self) -> str | None:
                tok = self.headers.get("X-Colab-Runtime-Proxy-Token") or ""
                if not tok:
                    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    tok = (qs.get("token") or [""])[0]
                valid_file = srv_ref.root / "valid_tokens.json"
                if valid_file.is_file():
                    valid = set(json.loads(valid_file.read_text(encoding="utf-8")))
                    if tok not in valid:
                        self._send_json(401, {"error": "unauthorized"})
                        return None
                return tok

            def _send_json(self, code: int, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path.startswith("/api/kernels/") and parsed.path.endswith("/channels"):
                    self._handle_ws(parsed)
                    return
                tok = self._check_auth()
                if tok is None:
                    return
                if parsed.path == "/api/kernels":
                    with srv_ref._lock:
                        kid = srv_ref.kernels_by_token.get(tok)
                    if kid:
                        self._send_json(200, [{"id": kid, "name": "python3", "execution_state": "idle"}])
                    else:
                        self._send_json(200, [])
                    return
                if parsed.path.startswith("/api/kernels/"):
                    req_id = urllib.parse.unquote(parsed.path[len("/api/kernels/") :])
                    with srv_ref._lock:
                        known = set(srv_ref.kernels_by_token.values())
                    if req_id in known:
                        self._send_json(200, {"id": req_id, "name": "python3", "execution_state": "idle"})
                    else:
                        self._send_json(404, {"error": "kernel not found"})
                    return
                self._send_json(404, {"error": "not found"})

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)
                tok = self._check_auth()
                if tok is None:
                    return
                length = int(self.headers.get("Content-Length") or "0")
                if length:
                    self.rfile.read(length)
                if parsed.path == "/api/kernels":
                    kid = f"kern-{len(srv_ref.kernels_by_token) + 1}"
                    with srv_ref._lock:
                        srv_ref.kernels_by_token[tok] = kid
                    self._send_json(201, {"id": kid, "name": "python3", "execution_state": "idle"})
                    return
                self._send_json(404, {"error": "not found"})

            def _handle_ws(self, parsed: urllib.parse.SplitResult):
                tok = self._check_auth()
                if tok is None:
                    return
                ws_key = self.headers.get("Sec-WebSocket-Key") or ""
                accept = base64.b64encode(hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.close_connection = True

                sock = self.connection
                with srv_ref._lock:
                    srv_ref.active_ws_sockets.append(sock)

                def _emit_iopub(parent_id: str, msg_type: str, content: dict, *, binary: bool = False):
                    msg = {
                        "channel": "iopub",
                        "header": {"msg_id": f"io-{time.monotonic_ns()}", "msg_type": msg_type},
                        "parent_header": {"msg_id": parent_id} if parent_id else {},
                        "metadata": {},
                        "content": content,
                    }
                    if binary:
                        _ws_send_frame(sock, 0x2, _encode_ws_binary_jupyter(msg))
                    else:
                        _ws_send_frame(sock, 0x1, json.dumps(msg).encode("utf-8"))

                def _emit_shell(parent_id: str, status: str = "ok"):
                    msg = {
                        "channel": "shell",
                        "header": {"msg_id": f"sh-{time.monotonic_ns()}", "msg_type": "execute_reply"},
                        "parent_header": {"msg_id": parent_id},
                        "metadata": {},
                        "content": {"status": status, "execution_count": 1},
                    }
                    _ws_send_frame(sock, 0x1, json.dumps(msg).encode("utf-8"))

                reattach_mode = (srv_ref.root / "reattach-busy-kernel").exists()
                stop_heartbeat = threading.Event()
                heartbeat_started = threading.Event()

                def _heartbeat_loop():
                    for _ in range(2):
                        if stop_heartbeat.wait(0.05):
                            return
                        with contextlib.suppress(OSError):
                            _emit_iopub("", "stream", {"name": "stdout", "text": "[mock-kernel] heartbeat\n"})

                try:
                    while True:
                        frame = _ws_unmask_frame(sock)
                        if frame is None:
                            break
                        _, opcode, payload = frame
                        if opcode == 0x8:
                            break
                        if opcode == 0x9:
                            srv_ref.pings_received += 1
                            _ws_send_frame(sock, 0xA, payload)
                            continue
                        if opcode != 0x1:
                            continue
                        req = json.loads(payload.decode("utf-8"))
                        msg_id = (req.get("header") or {}).get("msg_id") or ""
                        code = (req.get("content") or {}).get("code") or ""

                        if reattach_mode:
                            # Simulate busy Cell 5 blocking further shell replies while streaming heartbeats.
                            if not heartbeat_started.is_set():
                                heartbeat_started.set()
                                threading.Thread(target=_heartbeat_loop, daemon=True).start()
                            continue

                        if (srv_ref.root / "kernel-dead").exists():
                            _emit_iopub("", "status", {"execution_state": "dead"})
                            break

                        # Stream stdout (use binary Jupyter DEFAULT frame on Setup/ready to test both encodings!).
                        is_watchdog_cell = "# @title 5." in code
                        is_service_cell = "CT_SERVICE" in code and "os.chdir('/content')" not in code
                        if "os.chdir('/content')" in code:
                            _emit_iopub(msg_id, "status", {"execution_state": "busy"})
                            _emit_iopub(msg_id, "status", {"execution_state": "idle"})
                            _emit_shell(msg_id, "ok")
                            continue

                        with srv_ref._lock:
                            srv_ref.executed_codes.append(code)

                        if "RAISE_MOCK_CELL_ERROR" in code or (
                            is_service_cell and "# @title 1." in code and (srv_ref.root / "fail-cell-1").exists()
                        ):
                            _emit_iopub(msg_id, "status", {"execution_state": "busy"})
                            _emit_iopub(
                                msg_id,
                                "error",
                                {"ename": "RuntimeError", "evalue": "boom", "traceback": ["RuntimeError: boom\n"]},
                            )
                            _emit_iopub(msg_id, "status", {"execution_state": "idle"})
                            _emit_shell(msg_id, "error")
                            continue

                        if is_service_cell and is_watchdog_cell:
                            if (srv_ref.root / "exit-before-ready").exists():
                                _emit_iopub(msg_id, "error", {"ename": "RuntimeError", "evalue": "boom", "traceback": ["RuntimeError: boom\n"]})
                                _emit_iopub(msg_id, "status", {"execution_state": "idle"})
                                _emit_shell(msg_id, "error")
                                continue
                            _emit_iopub(
                                msg_id,
                                "stream",
                                {"name": "stdout", "text": f"CT_PUBLIC_URL={srv_ref.health_url}\n"},
                                binary=True,
                            )
                            _emit_iopub(msg_id, "stream", {"name": "stdout", "text": "CT_READY\n"}, binary=False)
                            if (srv_ref.root / "fail-after-ready").exists():
                                _emit_iopub(
                                    msg_id,
                                    "error",
                                    {"ename": "RuntimeError", "evalue": "boom", "traceback": ["RuntimeError: boom\n"]},
                                )
                                _emit_iopub(msg_id, "status", {"execution_state": "idle"})
                                _emit_shell(msg_id, "error")
                                continue
                            if (srv_ref.root / "crash-ws-once").exists():
                                (srv_ref.root / "crash-ws-once").unlink()
                                time.sleep(0.05)
                                break
                            # Last cell stays executing (idle never emitted), streaming optional heartbeat!
                            if not heartbeat_started.is_set():
                                heartbeat_started.set()
                                threading.Thread(target=_heartbeat_loop, daemon=True).start()
                            continue

                        # Regular cell: run quickly and return idle + shell reply.
                        _emit_iopub(msg_id, "status", {"execution_state": "busy"})
                        _emit_iopub(msg_id, "stream", {"name": "stdout", "text": "cell ok\n"}, binary=True)
                        _emit_iopub(msg_id, "status", {"execution_state": "idle"})
                        _emit_shell(msg_id, "ok")
                except (OSError, EOFError, ValueError):
                    pass
                finally:
                    stop_heartbeat.set()
                    with srv_ref._lock:
                        if sock in srv_ref.active_ws_sockets:
                            srv_ref.active_ws_sockets.remove(sock)
                    with contextlib.suppress(OSError):
                        sock.close()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def drop_all_ws(self) -> None:
        with self._lock:
            socks = list(self.active_ws_sockets)
        for sock in socks:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def close(self) -> None:
        self.drop_all_ws()
        self.server.shutdown()
        self.server.server_close()


class ColabWsMuxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home_dir = self.root / "home"
        self.profile_dir = self.root / "profile"
        self.cfg_dir = self.root / "colab_cfg"
        for d in (self.home_dir, self.profile_dir, self.cfg_dir):
            d.mkdir(parents=True, exist_ok=True)

        root_dir = self.root

        class Health(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                code = 502 if (root_dir / "http-502").exists() else 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"ok" if code == 200 else b"bad gateway")

        self.health = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        threading.Thread(target=self.health.serve_forever, daemon=True).start()
        self.addCleanup(self.health.server_close)
        self.addCleanup(self.health.shutdown)
        self.health_url = f"http://127.0.0.1:{self.health.server_port}"

        self.mock_ws = _MockColabRuntimeServer(self.root, self.health_url)
        self.addCleanup(self.mock_ws.close)

        self.valid_tokens_path = self.root / "valid_tokens.json"
        self.valid_tokens_path.write_text(json.dumps(["tok-initial"]), encoding="utf-8")

        fake_colab = self.root / "colab"
        fake_colab.write_text(
            '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["FAKE_COLAB_ROOT"])
cfg_dir = Path(os.environ["COLAB_CLI_CONFIG_DIR"])
sessions_path = cfg_dir / "sessions.json"
calls_path = root / "colab-calls.jsonl"
with calls_path.open("a", encoding="utf-8") as logf:
    logf.write(json.dumps(sys.argv[1:]) + "\\n")

def load_sessions():
    if sessions_path.is_file():
        return json.loads(sessions_path.read_text(encoding="utf-8"))
    return {}

def save_sessions(data):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    sessions_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

cmd = sys.argv[1] if len(sys.argv) > 1 else ""
sess = None
if "-s" in sys.argv:
    idx = sys.argv.index("-s")
    if idx + 1 < len(sys.argv):
        sess = sys.argv[idx + 1]

if cmd == "new" and sess:
    data = load_sessions()
    tok = "tok-initial"
    if (root / "use-expired-initial-token").exists():
        tok = "tok-stale"
        exp = "2020-01-01T00:00:00Z"
    else:
        exp = "2099-01-01T00:00:00Z"
    data[sess] = {
        "name": sess,
        "token": tok,
        "url": os.environ["MOCK_COLAB_WS_URL"],
        "endpoint": f"ep-{sess}",
        "token_expires_at": exp,
        "variant": "DEFAULT",
        "accelerator": "NONE",
        "machine_shape": "STANDARD",
        "kernel_id": None,
        "session_id": None,
    }
    save_sessions(data)
    (root / "session-missing").unlink(missing_ok=True)
    sys.exit(0)

if cmd == "status":
    missing = set()
    if (root / "session-missing").exists():
        missing.add(sess)
    data = load_sessions()
    if sess in missing or (sess and sess not in data):
        print(f"[colab] Session '{sess}' not found.", flush=True)
        sys.exit(0)
    # Always refresh token on status just like real `colab_cli.common.sync_sessions()`.
    refreshed = os.environ.get("MOCK_REFRESHED_TOKEN", "tok-fresh")
    for k, rec in data.items():
        if rec.get("token") != refreshed and ((root / "refresh-on-status").exists() or rec.get("token") == "tok-stale"):
            rec["token"] = refreshed
            rec["token_expires_at"] = "2099-01-01T00:00:00Z"
    save_sessions(data)
    if sess:
        print(f"[{sess}] ep-{sess} | Hardware: CPU | Status: BUSY", flush=True)
    sys.exit(0)

if cmd == "stop" and sess:
    data = load_sessions()
    data.pop(sess, None)
    save_sessions(data)
    sys.exit(0)

if cmd == "exec":
    # Fail loudly if native WS mux accidentally fell back to subprocess `colab exec`!
    print("UNEXPECTED_COLAB_EXEC_CALLED", file=sys.stderr, flush=True)
    sys.exit(97)

sys.exit(0)
''',
            encoding="utf-8",
        )
        fake_colab.chmod(0o755)

        self.env = {
            **os.environ,
            "HOME": str(self.home_dir),
            "COLABLLM_HOME": str(self.profile_dir),
            "COLAB_CLI_CONFIG_DIR": str(self.cfg_dir),
            "FAKE_COLAB_ROOT": str(self.root),
            "MOCK_COLAB_WS_URL": self.mock_ws.url,
            "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
        }
        self.env.pop("CT_INSTANCE", None)
        self.env.pop("COLABLLM_EXEC_BACKEND", None)
        self.env.pop("COLABLLM_NO_MUX", None)
        self.addCleanup(self._cleanup_mux_and_children)

    def _cleanup_mux_and_children(self):
        mux_state = self.profile_dir / "mux" / "state.json"
        if mux_state.is_file():
            with contextlib.suppress(Exception):
                st = json.loads(mux_state.read_text(encoding="utf-8"))
                mux._rpc_sync(st["port"], st["token"], "shutdown", timeout=2.0)
                stop_owned_process(st.get("pid"), st.get("identity"), timeout=2.0)
        services_dir = self.profile_dir / "services"
        if services_dir.is_dir():
            for sp in services_dir.glob("*/state.json"):
                with contextlib.suppress(Exception):
                    st = json.loads(sp.read_text(encoding="utf-8"))
                    if st.get("runner_mode") != "mux":
                        stop_owned_process(st.get("runner_pid"), st.get("runner_identity"), group=True)

    def cli(self, service: str, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(ROOT / "colabllm"), service, *args],
            env=self.env,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def colab_calls(self) -> list[list[str]]:
        p = self.root / "colab-calls.jsonl"
        if not p.is_file():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_multi_instance_shares_single_mux_process_with_zero_colab_exec_and_idle_exits(self):
        # Launch 3 concurrent instances across different services without awaiting readiness one-by-one.
        for svc_name, inst in (("ollama", "m1"), ("ollama", "m2"), ("qwen-flash", "m3")):
            res = self.cli(svc_name, "-i", inst, "up", "--quick-tunnel", "--no-drive", "--detach")
            self.assertEqual(res.returncode, 0, res.stderr)

        sessions = ("ollama-m1", "ollama-m2", "qwen-flash-m3")
        deadline = time.monotonic() + 10.0
        states: dict[str, dict] = {}
        while time.monotonic() < deadline:
            states = {}
            for sname in sessions:
                sp = self.profile_dir / "services" / sname / "state.json"
                if sp.is_file():
                    states[sname] = json.loads(sp.read_text(encoding="utf-8"))
            if len(states) == 3 and all(st.get("ready") and st.get("url") == self.health_url for st in states.values()):
                break
            time.sleep(0.05)
        self.assertEqual(len(states), 3)
        for sname, st in states.items():
            self.assertTrue(st.get("ready"), f"{sname} not ready: {st}")
            self.assertEqual(st.get("runner_mode"), "mux")
            self.assertEqual(st.get("exec_backend"), "native")
            self.assertIsNone(st.get("exec_pid"))

        # All 3 instances must share the exact same mux PID & identity.
        pids = {st["runner_pid"] for st in states.values()}
        identities = {st["runner_identity"] for st in states.values()}
        self.assertEqual(len(pids), 1, states)
        self.assertEqual(len(identities), 1, states)
        mux_pid = next(iter(pids))
        mux_identity = next(iter(identities))
        self.assertTrue(owned_process(mux_pid, mux_identity))

        # Verify 0 `colab exec` subprocesses were spawned.
        self.assertEqual([c for c in self.colab_calls() if c[:1] == ["exec"]], [])

        # Verify each instance got its own private run.log with CT_READY & heartbeat.
        for sname in sessions:
            log_text = (self.profile_dir / "services" / sname / "run.log").read_text(encoding="utf-8")
            self.assertIn("CT_READY", log_text)

        # Verify CLI `ps --json` & `status --json` report running+ready cleanly.
        ps_res = self.cli("ollama", "ps", "--json")
        self.assertEqual(ps_res.returncode, 0, ps_res.stderr)
        ps_items = {it["name"]: it for it in json.loads(ps_res.stdout)}
        self.assertTrue(ps_items["ollama-m1"]["ready"])
        self.assertTrue(ps_items["ollama-m2"]["ready"])

        # Stopping m1 leaves m2 & m3 alive on the same mux daemon.
        down_m1 = self.cli("ollama", "-i", "m1", "down")
        self.assertEqual(down_m1.returncode, 0, down_m1.stderr)
        self.assertTrue(owned_process(mux_pid, mux_identity))
        st_m2 = self.cli("ollama", "-i", "m2", "status", "--json")
        self.assertEqual(st_m2.returncode, 0, st_m2.stderr)
        self.assertTrue(json.loads(st_m2.stdout)["ready"])

        # Stopping m2 & m3 causes the mux daemon to auto-exit once idle.
        self.assertEqual(self.cli("ollama", "-i", "m2", "down").returncode, 0)
        self.assertEqual(self.cli("qwen-flash", "-i", "m3", "down").returncode, 0)
        exit_deadline = time.monotonic() + 5.0
        while owned_process(mux_pid, mux_identity) and time.monotonic() < exit_deadline:
            time.sleep(0.05)
        self.assertFalse(owned_process(mux_pid, mux_identity), "mux daemon failed to idle-exit after all instances stopped")

    def test_expired_token_refreshes_automatically_via_colab_status_before_ws_connect(self):
        (self.root / "use-expired-initial-token").touch()
        (self.root / "refresh-on-status").touch()
        self.valid_tokens_path.write_text(json.dumps(["tok-fresh"]), encoding="utf-8")
        # Also return 401 once unless `colab status` updated sessions.json to `tok-fresh`.
        up = self.cli("ollama", "-i", "tok", "up", "--quick-tunnel", "--no-drive")
        self.assertEqual(up.returncode, 0, up.stderr)
        st = json.loads((self.profile_dir / "services" / "ollama-tok" / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(st["ready"])
        self.assertEqual(st["runner_mode"], "mux")
        status_calls = [c for c in self.colab_calls() if c[:1] == ["status"]]
        self.assertGreaterEqual(len(status_calls), 1)
        self.assertEqual(self.cli("ollama", "-i", "tok", "down").returncode, 0)

    def test_auto_restart_reattaches_live_vm_and_recreates_reclaimed_vm_inside_mux(self):
        self.env["COLABLLM_FAST_PROBE_SECONDS"] = "0.08"
        self.env["COLABLLM_IDLE_CHECK_SECONDS"] = "300"
        (self.root / "crash-ws-once").touch()
        up = self.cli(
            "ollama", "-i", "ar", "up", "--quick-tunnel", "--no-drive", "--auto-restart", "--restart-delay", "0.05"
        )
        self.assertEqual(up.returncode, 0, up.stderr)

        # Wait for the simulated transient WS drop (`crash-ws-once`) -> reattach without recreating VM.
        (self.root / "reattach-busy-kernel").touch()
        deadline = time.monotonic() + 8.0
        reattached = {}
        while time.monotonic() < deadline:
            sp = self.profile_dir / "services" / "ollama-ar" / "state.json"
            reattached = json.loads(sp.read_text(encoding="utf-8"))
            if int(reattached.get("restart_count") or 0) >= 1 and reattached.get("ready"):
                break
            time.sleep(0.05)
        self.assertGreaterEqual(int(reattached.get("restart_count") or 0), 1)
        self.assertTrue(reattached["ready"])
        new_before = len([c for c in self.colab_calls() if c[:1] == ["new"]])
        self.assertEqual(new_before, 1)

        # Now simulate remote VM reclamation (`502` + `session-missing`) -> mux detects via fast edge probe & recreates VM.
        (self.root / "reattach-busy-kernel").unlink(missing_ok=True)
        (self.root / "http-502").touch()
        (self.root / "session-missing").touch()

        def _clear_502_after_new():
            dl = time.monotonic() + 8.0
            while time.monotonic() < dl:
                if len([c for c in self.colab_calls() if c[:1] == ["new"]]) >= 2:
                    (self.root / "http-502").unlink(missing_ok=True)
                    return
                time.sleep(0.03)

        t = threading.Thread(target=_clear_502_after_new, daemon=True)
        t.start()
        deadline = time.monotonic() + 10.0
        recreated = {}
        while time.monotonic() < deadline:
            sp = self.profile_dir / "services" / "ollama-ar" / "state.json"
            recreated = json.loads(sp.read_text(encoding="utf-8"))
            if int(recreated.get("restart_count") or 0) >= 2 and recreated.get("ready"):
                break
            time.sleep(0.05)
        t.join(timeout=2.0)
        self.assertGreaterEqual(int(recreated.get("restart_count") or 0), 2)
        self.assertTrue(recreated["ready"])
        self.assertEqual(len([c for c in self.colab_calls() if c[:1] == ["new"]]), 2)
        self.assertEqual(self.cli("ollama", "-i", "ar", "down").returncode, 0)

    def test_ws_ping_and_binary_frame_decoding_and_foreground_mode(self):
        # Exercise native WS in foreground mode + verify Jupyter binary frames & RFC 6455 encoding/decoding.
        fg = subprocess.Popen(
            [str(ROOT / "colabllm"), "ollama", "-i", "fg", "up", "--quick-tunnel", "--no-drive", "--foreground"],
            env=self.env,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.addCleanup(lambda: fg.poll() is None and fg.kill())
        deadline = time.monotonic() + 10.0
        st = {}
        sp = self.profile_dir / "services" / "ollama-fg" / "state.json"
        while time.monotonic() < deadline:
            if sp.is_file():
                st = json.loads(sp.read_text(encoding="utf-8"))
                if st.get("ready") and st.get("url") == self.health_url:
                    break
            time.sleep(0.05)
        self.assertTrue(st.get("ready"), st)
        self.assertEqual(st.get("runner_mode"), "foreground")
        self.assertEqual(st.get("exec_backend"), "native")
        self.assertEqual(self.cli("ollama", "-i", "fg", "down").returncode, 0)
        fg.wait(timeout=5.0)
        if fg.stdout:
            fg.stdout.close()

        # Validate binary frame decoder directly as well.
        sample = {"channel": "iopub", "header": {"msg_type": "stream"}, "content": {"text": "hi"}}
        raw_bin = _encode_ws_binary_jupyter(sample)
        decoded = colab_ws.decode_jupyter_ws_frame(0x2, raw_bin)
        self.assertEqual(decoded["content"]["text"], "hi")

    def test_cell_error_stops_remaining_cells_and_returns_nonzero(self):
        # 1) Early cell failure (`status="error"`) stops remaining cells and records non-zero runner_exit.
        (self.root / "fail-cell-1").touch()
        res_early = self.cli("ollama", "-i", "err1", "up", "--quick-tunnel", "--no-drive")
        self.assertNotEqual(res_early.returncode, 0)
        (self.root / "fail-cell-1").unlink(missing_ok=True)

        sp_early = self.profile_dir / "services" / "ollama-err1" / "state.json"
        deadline = time.monotonic() + 5.0
        st_early = {}
        while time.monotonic() < deadline:
            st_early = json.loads(sp_early.read_text(encoding="utf-8"))
            if st_early.get("runner_exit") is not None and st_early.get("runner_pid") is None:
                break
            time.sleep(0.05)
        self.assertEqual(st_early.get("runner_exit"), 1, st_early)
        self.assertFalse(st_early.get("ready"))
        # Only PRELUDE, Cell 0, and Cell 1 ran; Cells 2..5 must NOT execute after Cell 1 errored.
        with self.mock_ws._lock:
            ran = list(self.mock_ws.executed_codes)
        self.assertEqual(len(ran), 3, f"unexpected cells ran after failure: {len(ran)}")
        self.assertIn("_ct_os.environ.update", ran[0])
        self.assertIn("# @title 0.", ran[1])
        self.assertIn("# @title 1.", ran[2])
        self.assertFalse(any("# @title 2." in c for c in ran))
        self.assertFalse(any("# @title 5." in c for c in ran))
        self.assertEqual(self.cli("ollama", "-i", "err1", "down").returncode, 0)

        # 2) Post-ready Cell 5 error (`status="error"`) also exits with runner_exit=1 instead of 0.
        with self.mock_ws._lock:
            self.mock_ws.executed_codes.clear()
        (self.root / "fail-after-ready").touch()
        up_post = self.cli("ollama", "-i", "err5", "up", "--quick-tunnel", "--no-drive", "--detach")
        self.assertEqual(up_post.returncode, 0, up_post.stderr)
        sp_post = self.profile_dir / "services" / "ollama-err5" / "state.json"
        deadline = time.monotonic() + 8.0
        st_post = {}
        while time.monotonic() < deadline:
            if sp_post.is_file():
                st_post = json.loads(sp_post.read_text(encoding="utf-8"))
                if st_post.get("runner_exit") is not None and st_post.get("runner_pid") is None:
                    break
            time.sleep(0.05)
        self.assertEqual(st_post.get("runner_exit"), 1, st_post)
        log_post = (self.profile_dir / "services" / "ollama-err5" / "run.log").read_text(encoding="utf-8")
        self.assertIn("CT_READY", log_post)
        self.assertIn("RuntimeError: boom", log_post)
        self.assertEqual(self.cli("ollama", "-i", "err5", "down").returncode, 0)


if __name__ == "__main__":
    unittest.main()
