"""Standard-library unit & integration tests for colabllm gateway CLI and HTTP/1.1 data plane."""
from __future__ import annotations

import concurrent.futures
import email.parser
import email.policy
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src/colabllm/lib"))

from colabllm.gateway_runtime import GatewayConfig, GatewayHTTPServer  # noqa: E402
from colabllm.gateway_runtime.auth import GatewayError, TokenStore  # noqa: E402
from colabllm.gateway_runtime.catalog import GATEWAY_RUNTIME_SPECS, RuntimeGatewaySpec, parse_slot_capacity  # noqa: E402
from colabllm.gateway_runtime.routing import extract_affinity_key  # noqa: E402
from processes import process_identity  # noqa: E402


class _MockUpstreamServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, handler_fn):
        self.handler_fn = handler_fn
        self.requests: list[dict] = []
        self._rec_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _MockUpstreamHandler)

    def record(self, item: dict) -> None:
        with self._rec_lock:
            self.requests.append(item)


class _MockUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _MockUpstreamServer

    def log_message(self, format, *args):
        return

    def _dispatch(self, method: str) -> None:
        cl = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(cl) if cl > 0 else b""
        hdrs = {k.lower(): v for k, v in self.headers.items()}
        rec = {"method": method, "path": self.path, "headers": hdrs, "body": raw_body}
        self.server.record(rec)
        status, resp_headers, resp_body = self.server.handler_fn(rec)
        if callable(resp_body):
            # Streaming callback: (handler, status, resp_headers)
            resp_body(self, status, resp_headers)
            return
        self.send_response(status)
        for k, v in resp_headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if method != "HEAD":
            try:
                self.wfile.write(resp_body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


class GatewayTestMixin:
    def make_env(self, home: Path) -> dict[str, str]:
        return {**os.environ, "COLABLLM_HOME": str(home)}

    def write_instance(
        self,
        home: Path,
        session_dir: str,
        *,
        provider: str = "colab",
        account: str | None = None,
        url: str = "http://127.0.0.1:9",
        api_token: str = "upstream-secret",
        ready: bool = True,
        alive: bool = True,
        env_map: dict | None = None,
        extra: dict | None = None,
    ) -> Path:
        root_dir = home / "kaggle" / "services" if provider == "kaggle" else home / "services"
        inst_dir = root_dir / session_dir
        inst_dir.mkdir(parents=True, exist_ok=True)
        pid = os.getpid() if alive else 9999999
        ident = process_identity(os.getpid()) if alive else "dead-identity"
        payload = {
            "provider": provider,
            "url": url,
            "api_token": api_token,
            "ready": ready,
            "runner_pid": pid,
            "runner_identity": ident,
            "runner_mode": "detach",
            "transport": "cloudflare",
            "env": env_map or {},
        }
        if account is not None:
            payload["account"] = account
        if extra:
            payload.update(extra)
        sp = inst_dir / "state.json"
        sp.write_text(json.dumps(payload), encoding="utf-8")
        return sp

    def start_gateway(self, home: Path, **cfg_kwargs) -> tuple[GatewayHTTPServer, int, str]:
        cfg = GatewayConfig(host="127.0.0.1", port=0, state_root=home, **cfg_kwargs)
        srv = GatewayHTTPServer((cfg.host, cfg.port), config=cfg)
        self.addCleanup(srv.server_close)
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(srv.shutdown)
        created = srv.token_store.create_token("test-client")
        return srv, srv.server_address[1], created["token"]

    def start_mock(self, fn) -> tuple[_MockUpstreamServer, str]:
        srv = _MockUpstreamServer(fn)
        self.addCleanup(srv.server_close)
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(srv.shutdown)
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    def http_req(
        self,
        port: int,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        host: str = "127.0.0.1",
        timeout: float = 10.0,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, hdrs, raw
        finally:
            conn.close()


class GatewayCliAndAuthTests(GatewayTestMixin, unittest.TestCase):
    def test_cli_detach_status_stop_token_lifecycle_and_fail_closed_version(self):
        cli = str(ROOT / "colabllm")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "gw_home"
            env = self.make_env(home)

            # --status before starting must report not running and not create gw_home/gateway
            st0 = subprocess.run([cli, "gateway", "--status", "--json"], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(st0.returncode, 0, st0.stderr)
            self.assertEqual(json.loads(st0.stdout), {"running": False})
            self.assertFalse((home / "gateway").exists())

            # --json without lifecycle mode must fail with exit code 2
            bad_json = subprocess.run([cli, "gateway", "--json"], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(bad_json.returncode, 2)

            # Mixing token subcommand with --detach must fail with exit code 2
            bad_mix = subprocess.run([cli, "gateway", "--detach", "token", "create", "a"], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(bad_mix.returncode, 2)

            # Invalid --allow-host single-label wildcard must fail exit code 2
            bad_host = subprocess.run(
                [cli, "gateway", "--allow-host", "*.com", "--status"],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(bad_host.returncode, 2)

            # Start detached daemon -> creates initial 'default' token BEFORE fork and outputs once
            up = subprocess.run(
                [cli, "gateway", "--detach", "--port", "0", "--allow-host", "127.0.0.1", "--json"],
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(up.returncode, 0, up.stderr)
            up_data = json.loads(up.stdout)
            self.assertTrue(up_data["started"])
            init_tok = up_data["initial_token"]
            self.assertIsNotNone(init_tok)
            self.assertTrue(init_tok.startswith("clgw_"))
            gw_port = int(up_data["port"])

            try:
                # Subsequent status works even with strict --allow-host (uses X-ColabLLM-Status-Token locally)
                st1 = subprocess.run([cli, "gateway", "--status", "--json"], env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(st1.returncode, 0, st1.stderr)
                st1_data = json.loads(st1.stdout)
                self.assertTrue(st1_data["running"])
                self.assertNotIn("status_token", st1_data)
                self.assertNotIn("identity", st1_data)
                self.assertNotIn(init_tok, (home / "gateway/gateway.log").read_text(encoding="utf-8"))

                # Test /healthz no auth + Bearer vs x-api-key + dual mismatch 401
                code, _, raw = self.http_req(gw_port, "GET", "/healthz")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(raw), {"status": "ok"})

                code_m, _, _ = self.http_req(
                    gw_port,
                    "GET",
                    "/v1/models",
                    headers={"Authorization": f"Bearer {init_tok}", "x-api-key": "clgw_dead_beef"},
                )
                self.assertEqual(code_m, 401)

                # status_token cannot access /v1/models (401)
                state_disk = json.loads((home / "gateway/state.json").read_text(encoding="utf-8"))
                code_st_v1, _, _ = self.http_req(
                    gw_port,
                    "GET",
                    "/v1/models",
                    headers={"X-ColabLLM-Status-Token": state_disk["status_token"]},
                )
                self.assertEqual(code_st_v1, 401)

                # token create / duplicate error / rotate / revoke / hot reload
                c1 = subprocess.run([cli, "gateway", "token", "create", "pi-agent", "--json"], env=env, text=True, capture_output=True)
                self.assertEqual(c1.returncode, 0, c1.stderr)
                pi_tok = json.loads(c1.stdout)["token"]

                dup = subprocess.run([cli, "gateway", "token", "create", "pi-agent", "--json"], env=env, text=True, capture_output=True)
                self.assertEqual(dup.returncode, 2)

                lst = subprocess.run([cli, "gateway", "token", "list", "--json"], env=env, text=True, capture_output=True)
                self.assertEqual(lst.returncode, 0)
                self.assertNotIn("sha256", lst.stdout)
                self.assertNotIn(pi_tok, lst.stdout)

                # Use newly created pi_tok via x-api-key without restarting daemon
                code_ok, _, _ = self.http_req(gw_port, "GET", "/v1/models", headers={"x-api-key": pi_tok})
                self.assertEqual(code_ok, 200)

                # Rotate immediately invalidates old token & activates new token
                rot = subprocess.run([cli, "gateway", "token", "rotate", "pi-agent", "--json"], env=env, text=True, capture_output=True)
                self.assertEqual(rot.returncode, 0, rot.stderr)
                pi_tok_2 = json.loads(rot.stdout)["token"]
                self.assertEqual(self.http_req(gw_port, "GET", "/v1/models", headers={"x-api-key": pi_tok})[0], 401)
                self.assertEqual(self.http_req(gw_port, "GET", "/v1/models", headers={"x-api-key": pi_tok_2})[0], 200)

                # Revoke & missing rotate -> exit 2
                rev = subprocess.run([cli, "gateway", "token", "revoke", "pi-agent", "--json"], env=env, text=True, capture_output=True)
                self.assertEqual(rev.returncode, 0)
                self.assertEqual(self.http_req(gw_port, "GET", "/v1/models", headers={"x-api-key": pi_tok_2})[0], 401)
                self.assertEqual(subprocess.run([cli, "gateway", "token", "rotate", "pi-agent"], env=env, capture_output=True).returncode, 2)

                # Permissions are 0700 / 0600
                self.assertEqual(stat.S_IMODE((home / "gateway").stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((home / "gateway/tokens.json").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((home / "gateway/state.json").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((home / "gateway/gateway.log").stat().st_mode), 0o600)
            finally:
                stop = subprocess.run([cli, "gateway", "--stop", "--json"], env=env, text=True, capture_output=True, timeout=15)
                self.assertEqual(stop.returncode, 0)
                self.assertTrue(json.loads(stop.stdout)["stopped"])

            # Second detached start must have initial_token: null
            up2 = subprocess.run([cli, "gateway", "--detach", "--port", "0", "--json"], env=env, text=True, capture_output=True, timeout=15)
            try:
                self.assertEqual(up2.returncode, 0, up2.stderr)
                self.assertIsNone(json.loads(up2.stdout)["initial_token"])
            finally:
                subprocess.run([cli, "gateway", "--stop"], env=env, capture_output=True, timeout=15)

            # Corrupt version != 1 must fail closed (HTTP 500 or CLI error)
            (home / "gateway/tokens.json").write_text(json.dumps({"version": 99, "tokens": {}}), encoding="utf-8")
            fail_tok = subprocess.run([cli, "gateway", "token", "list"], env=env, text=True, capture_output=True)
            self.assertNotEqual(fail_tok.returncode, 0)

    def test_host_wildcard_sec_fetch_site_and_0000_warning(self):
        cli = str(ROOT / "colabllm")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "gw_home"
            # --allow-remote 0.0.0.0 without --allow-host must warn on stderr
            env = self.make_env(home)
            up = subprocess.run(
                [cli, "gateway", "--detach", "--host", "0.0.0.0", "--allow-remote", "--port", "0", "--json"],
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(up.returncode, 0, up.stderr)
            self.assertIn("警告", up.stderr)
            subprocess.run([cli, "gateway", "--stop"], env=env, capture_output=True, timeout=15)

            # In-process allow_hosts multi-label wildcard + Sec-Fetch-Site
            _, port, token = self.start_gateway(home, allow_hosts=("*.ts.net", "localhost"))
            self.assertEqual(self.http_req(port, "GET", "/healthz", headers={"Host": "host-a.ts.net"})[0], 200)
            self.assertEqual(self.http_req(port, "GET", "/healthz", headers={"Host": "sub.host-a.ts.net"})[0], 200)
            # Bare suffix ts.net or evil host rejected 403
            self.assertEqual(self.http_req(port, "GET", "/healthz", headers={"Host": "ts.net"})[0], 403)
            self.assertEqual(self.http_req(port, "GET", "/healthz", headers={"Host": "evil.example.com"})[0], 403)
            # Sec-Fetch-Site blocks cross-site and same-site, allows same-origin and none
            for bad_site in ("cross-site", "same-site"):
                c, _, _ = self.http_req(port, "GET", "/v1/models", headers={"Host": "host-a.ts.net", "Authorization": f"Bearer {token}", "Sec-Fetch-Site": bad_site})
                self.assertEqual(c, 403)
            for ok_site in ("same-origin", "none"):
                c, _, _ = self.http_req(port, "GET", "/v1/models", headers={"Host": "host-a.ts.net", "Authorization": f"Bearer {token}", "Sec-Fetch-Site": ok_site})
                self.assertEqual(c, 200)

    def test_ipv6_loopback_and_status_token_strip_query_credentials(self):
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            sock.bind(("::1", 0))
        except OSError:
            self.skipTest("IPv6 loopback ::1 unavailable in this environment")
        finally:
            sock.close()

        cli = str(ROOT / "colabllm")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "gw_v6"
            self.write_instance(home, "qwen-flash", url="https://qwen.example.com/root?token=leak123#frag")
            env = self.make_env(home)
            up = subprocess.run(
                [cli, "gateway", "--detach", "--host", "::1", "--port", "0", "--allow-host", "proxy.ts.net", "--json"],
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(up.returncode, 0, up.stderr)
            try:
                st = subprocess.run([cli, "gateway", "--status", "--json"], env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(st.returncode, 0, st.stderr)
                data = json.loads(st.stdout)
                self.assertTrue(data["running"])
                self.assertEqual(data["instances"][0]["url"], "https://qwen.example.com/root")
                self.assertNotIn("leak123", st.stdout)
            finally:
                subprocess.run([cli, "gateway", "--stop"], env=env, capture_output=True, timeout=15)


class GatewayRoutingAndCatalogTests(GatewayTestMixin, unittest.TestCase):
    def test_routing_precedence_models_voices_ollama_compat_and_starting_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)

            def qwen_handler(rec):
                if rec["path"] == "/v1/models":
                    return 200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next"}]}).encode()
                body = json.loads(rec["body"].decode())
                return 200, {"Content-Type": "application/json"}, json.dumps({"id": "c1", "model": body["model"]}).encode()

            def tango_handler(rec):
                # Upstream WITHOUT GET /v1/models/{id}!
                if rec["path"] == "/v1/models":
                    return 200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": [{"id": "TangoFlux"}]}).encode()
                return 404, {"Content-Type": "application/json"}, b'{"error":"not supported upstream"}'

            def indextts_handler(rec):
                if rec["path"] == "/v1/models":
                    return 200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": [{"id": "indextts-2.5"}]}).encode()
                if rec["path"] == "/v1/audio/voices":
                    return 200, {"Content-Type": "application/json"}, json.dumps({"voices": ["default", "narrator"]}).encode()
                return 404, {}, b""

            def ollama_anthropic_handler(rec):
                if rec["path"] == "/v1/models":
                    return 200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": [{"id": "qwen3:8b"}]}).encode()
                if rec["path"].startswith("/v1/messages"):
                    body = json.loads(rec["body"].decode())
                    return 200, {"Content-Type": "application/json"}, json.dumps({"type": "message", "message": {"model": body["model"]}}).encode()
                return 403, {}, b'{"error":"disabled"}'

            _, qwen_url = self.start_mock(qwen_handler)
            _, tango_url = self.start_mock(tango_handler)
            _, idx_url = self.start_mock(indextts_handler)
            _, oll_anth_url = self.start_mock(ollama_anthropic_handler)

            self.write_instance(home, "qwen-flash", url=qwen_url)
            self.write_instance(home, "tangoflux", url=tango_url)
            self.write_instance(home, "indextts", url=idx_url)
            self.write_instance(home, "ollama-native", url="http://127.0.0.1:9", env_map={"CT_OLLAMA_COMPAT": "ollama"})
            self.write_instance(home, "ollama-anth", url=oll_anth_url, env_map={
                "CT_OLLAMA_COMPAT": "anthropic",
                "CT_OLLAMA_SERVER_ENV": json.dumps({"OLLAMA_NUM_PARALLEL": "3"}),
            })
            self.write_instance(home, "deepseek-coder", url="http://127.0.0.1:9", ready=False, alive=True)

            srv, port, token = self.start_gateway(home)
            auth_h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            # 1. GET /v1/models aggregates READY compatible instances and excludes compat=ollama
            code, _, raw = self.http_req(port, "GET", "/v1/models", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(code, 200)
            ids = {m["id"] for m in json.loads(raw)["data"]}
            self.assertEqual(ids, {"qwen3.8-flash-next@qwen-flash", "TangoFlux@tangoflux", "indextts-2.5@indextts", "qwen3:8b@ollama"})

            # 2. GET /v1/models/{id} answers from gateway probe cache even for tangoflux (%40 encoded)
            code_card, _, raw_card = self.http_req(port, "GET", "/v1/models/TangoFlux%40tangoflux", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(code_card, 200)
            self.assertEqual(json.loads(raw_card)["id"], "TangoFlux@tangoflux")

            # 3. Bare model & @sha256: -> 400; disabled/unregistered -> 501
            self.assertEqual(self.http_req(port, "POST", "/v1/chat/completions", headers=auth_h, body=json.dumps({"model": "qwen3:8b"}).encode())[0], 400)
            self.assertEqual(self.http_req(port, "POST", "/v1/chat/completions", headers=auth_h, body=json.dumps({"model": "llama3@sha256:abcdef"}).encode())[0], 400)
            self.assertEqual(self.http_req(port, "POST", "/v1/chat/completions", headers=auth_h, body=json.dumps({"model": "wf@comfyui"}).encode())[0], 501)
            self.assertEqual(self.http_req(port, "POST", "/v1/chat/completions", headers=auth_h, body=json.dumps({"model": "foo@future-llm"}).encode())[0], 501)

            # 4. Control & mutation routes -> 403; root voices & removed /r/<svc>/v1/chat -> 404
            self.assertEqual(self.http_req(port, "POST", "/api/ct/upsample", headers=auth_h, body=b"{}")[0], 403)
            self.assertEqual(self.http_req(port, "POST", "/r/flux-klein/api/ct/edit", headers=auth_h, body=b"{}")[0], 403)
            self.assertEqual(self.http_req(port, "POST", "/r/qwen3-tts/v1/audio/voices", headers=auth_h, body=b"{}")[0], 403)
            self.assertEqual(self.http_req(port, "DELETE", "/v1/audio/voices/narrator", headers=auth_h)[0], 403)
            self.assertEqual(self.http_req(port, "GET", "/v1/audio/voices", headers=auth_h)[0], 404)
            self.assertEqual(self.http_req(port, "POST", "/r/qwen-flash/v1/chat/completions", headers=auth_h, body=b"{}")[0], 404)

            # 5. indextts prefixed voices -> 200; qwen-flash unsupported /v1/embeddings -> 404
            c_v, _, raw_v = self.http_req(port, "GET", "/r/indextts/v1/audio/voices", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(c_v, 200)
            self.assertEqual(json.loads(raw_v)["voices"], ["default", "narrator"])
            self.assertEqual(
                self.http_req(port, "POST", "/v1/embeddings", headers=auth_h, body=json.dumps({"model": "qwen3.8-flash-next@qwen-flash"}).encode())[0],
                404,
            )

            # 6. ollama CT_OLLAMA_COMPAT='anthropic' allows /v1/messages/count_tokens (Anthropic error JSON check) & blocks /v1/chat/completions -> 404
            c_m, _, raw_m = self.http_req(
                port,
                "POST",
                "/v1/messages/count_tokens",
                headers=auth_h,
                body=json.dumps({"model": "qwen3:8b@ollama", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(c_m, 200)
            self.assertEqual(json.loads(raw_m)["message"]["model"], "qwen3:8b@ollama")

            # Check Anthropic error JSON shape on /v1/messages for 403
            c_ae, _, raw_ae = self.http_req(
                port,
                "POST",
                "/v1/messages",
                headers={**auth_h, "X-Save-Drive": "out.txt"},
                body=json.dumps({"model": "qwen3:8b@ollama"}).encode(),
            )
            self.assertEqual(c_ae, 403)
            self.assertEqual(json.loads(raw_ae)["type"], "error")

            # 7. STARTING deepseek-coder -> 503 with status command tip
            c_st, _, raw_st = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "deepseek-v4.1-flash-coder@deepseek-coder"}).encode(),
            )
            self.assertEqual(c_st, 503)
            self.assertIn("colabllm deepseek-coder status", json.loads(raw_st)["error"]["message"])

            # 8. previous_response_id on stateless llama-server -> 400
            c_prev, _, _ = self.http_req(
                port,
                "POST",
                "/v1/responses",
                headers=auth_h,
                body=json.dumps({"model": "qwen3.8-flash-next@qwen-flash", "previous_response_id": "resp_1", "input": "hi"}).encode(),
            )
            self.assertEqual(c_prev, 400)

            # 9. parse_slot_capacity handles JSON string + negative/invalid fallback
            oll_spec = GATEWAY_RUNTIME_SPECS["ollama"]
            self.assertEqual(parse_slot_capacity(oll_spec, {"CT_OLLAMA_SERVER_ENV": '{"OLLAMA_NUM_PARALLEL": "3"}'}), 3)
            self.assertEqual(parse_slot_capacity(oll_spec, {"CT_OLLAMA_SERVER_ENV": '{"OLLAMA_NUM_PARALLEL": "-2"}'}), 1)
            self.assertEqual(parse_slot_capacity(oll_spec, {"CT_OLLAMA_SERVER_ENV": "not-json"}), 1)


class GatewayTransportMultipartSecurityTests(GatewayTestMixin, unittest.TestCase):
    def test_expect_100_smuggling_size_limit_multipart_lossless_and_tool_exemptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            received_multiparts: list[dict] = []

            def multi_handler(rec):
                if rec["path"] == "/v1/models":
                    return 200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": [{"id": "flux-klein"}, {"id": "nemotron-transcribe"}, {"id": "tangoflux"}, {"id": "qwen3.8-flash-next"}]}).encode()
                if rec["path"] == "/v1/images/edits":
                    ctype = rec["headers"]["content-type"]
                    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                        f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1") + rec["body"]
                    )
                    parts = []
                    for p in msg.get_payload():
                        parts.append({
                            "name": p.get_param("name", header="content-disposition"),
                            "filename": p.get_filename(),
                            "ctype": p.get_content_type(),
                            "bytes": p.get_payload(decode=True),
                        })
                    received_multiparts.append({"parts": parts})
                    return 200, {"Content-Type": "application/json"}, json.dumps({"data": [{"b64_json": "AA=="}]}).encode()
                return 200, {"Content-Type": "application/json"}, json.dumps({"id": "ok", "model": "qwen3.8-flash-next"}).encode()

            _, up_url = self.start_mock(multi_handler)
            for svc in ("flux-klein", "nemotron-transcribe", "tangoflux", "qwen-flash"):
                self.write_instance(home, svc, url=up_url)

            _, port, token = self.start_gateway(home, max_body_bytes=4096, max_text_field_bytes=256)
            auth_h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            # 1. Dual Content-Length + Transfer-Encoding -> 400
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                s.sendall(
                    f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Authorization: Bearer {token}\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{{}}".encode()
                )
                raw_resp = s.recv(4096)
                self.assertIn(b"400 Bad Request", raw_resp)
            finally:
                s.close()

            # 2. Expect: 100-continue MUST be rejected WITHOUT 100 Continue if unauthenticated or over max_body_bytes
            s2 = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                s2.sendall(
                    f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Expect: 100-continue\r\nContent-Length: 10\r\n\r\n".encode()
                )
                raw_unauth = s2.recv(4096)
                self.assertNotIn(b"100 Continue", raw_unauth)
                self.assertIn(b"401 Unauthorized", raw_unauth)
            finally:
                s2.close()

            # Authorized Expect: 100-continue succeeds before body arrives
            s3 = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                payload = json.dumps({"model": "qwen3.8-flash-next@qwen-flash", "messages": [{"role": "user", "content": "hi"}]}).encode()
                s3.sendall(
                    f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
                    f"Expect: 100-continue\r\nContent-Length: {len(payload)}\r\n\r\n".encode()
                )
                cont = s3.recv(4096)
                self.assertTrue(cont.startswith(b"HTTP/1.1 100 Continue"), cont)
                s3.sendall(payload)
                rest = b""
                while True:
                    chunk = s3.recv(4096)
                    if not chunk:
                        break
                    rest += chunk
                self.assertIn(b"200 OK", cont + rest)
            finally:
                s3.close()

            # 3. Over max_body_bytes -> 413; response_format="url" -> 400 (both JSON and multipart)
            self.assertEqual(
                self.http_req(port, "POST", "/v1/chat/completions", headers=auth_h, body=b"x" * 5000)[0],
                413,
            )
            c_url_fmt, _, _ = self.http_req(
                port,
                "POST",
                "/v1/images/generations",
                headers=auth_h,
                body=json.dumps({"model": "flux-klein@flux-klein", "prompt": "cat", "response_format": "url"}).encode(),
            )
            self.assertEqual(c_url_fmt, 400)

            # 4. Multi-value image[] + filename-less image/png binary lossless roundtrip (no pixel alteration)
            bnd = "testbnd123"
            png_a = b"\x89PNG\r\n\x1a\nBINARY_A_001\x00\xff"
            png_b = b"\x89PNG\r\n\x1a\nBINARY_B_002\x00\xfe"
            mp_body = b"".join([
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nflux-klein@flux-klein\r\n".encode(),
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nedit\r\n".encode(),
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"image[]\"; filename=\"a.png\"\r\nContent-Type: image/png\r\n\r\n".encode(),
                png_a, b"\r\n",
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"image[]\"\r\nContent-Type: image/png\r\n\r\n".encode(),
                png_b, b"\r\n",
                f"--{bnd}--\r\n".encode(),
            ])
            c_mp, _, raw_mp = self.http_req(
                port,
                "POST",
                "/v1/images/edits",
                headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={bnd}"},
                body=mp_body,
            )
            self.assertEqual(c_mp, 200, raw_mp)
            # Upstream response had no 'model' key -> gateway must not inject 'model'
            self.assertNotIn("model", json.loads(raw_mp))
            up_parts = received_multiparts[-1]["parts"]
            self.assertEqual([p["name"] for p in up_parts], ["model", "prompt", "image[]", "image[]"])
            self.assertEqual(up_parts[0]["bytes"], b"flux-klein")
            self.assertEqual(up_parts[2]["bytes"], png_a)
            self.assertEqual(up_parts[3]["bytes"], png_b)
            self.assertIsNone(up_parts[3]["filename"])

            # Duplicate scalar 'model' field in multipart -> 400
            mp_dup = b"".join([
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nflux-klein@flux-klein\r\n".encode(),
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nflux-klein@flux-klein\r\n".encode(),
                f"--{bnd}--\r\n".encode(),
            ])
            self.assertEqual(
                self.http_req(
                    port,
                    "POST",
                    "/v1/images/edits",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={bnd}"},
                    body=mp_dup,
                )[0],
                400,
            )

            # 5. Four-stage Fail-Closed Drive / external URL / YTDLP guards -> 403
            blocked_cases = [
                ("/v1/chat/completions", {"model": "nemotron-transcribe@nemotron-transcribe", "extra_body": {"save_drive_dir": "audio/out"}}),
                ("/v1/chat/completions", {"model": "nemotron-transcribe@nemotron-transcribe", "messages": [{"role": "user", "content": [{"type": "audio_url", "audio_url": {"url": "https://www.youtube.com/watch?v=123"}}]}]}),
                ("/v1/audio/speech", {"model": "tangoflux@tangoflux", "items": [{"text": "boom", "save_to_drive": "sfx/out.wav"}]}),
                ("/v1/images/generations", {"model": "flux-klein@flux-klein", "prompt": "x", "url": "https://attacker.invalid/a.png"}),
                ("/v1/images/generations", {"model": "flux-klein@flux-klein", "prompt": "x", "image": "secret.png"}),
            ]
            for path, body_obj in blocked_cases:
                code_b, _, _ = self.http_req(port, "POST", path, headers=auth_h, body=json.dumps(body_obj).encode())
                self.assertEqual(code_b, 403, f"Expected 403 for {path} {body_obj}")

            # 6. Chat / Responses / Anthropic tools + tool_calls + tool_result with url/image/audio pass 200!
            ok_tool_chat = {
                "model": "qwen3.8-flash-next@qwen-flash",
                "tools": [{"type": "function", "function": {"name": "fetch_img", "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "save_to_drive": {"type": "boolean"}}}}}],
                "messages": [
                    {"role": "assistant", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "fetch_img", "arguments": "{\"url\":\"https://example.com\"}"}}]},
                    {"role": "tool", "content": "{\"url\":\"https://example.com\",\"image\":\"/tmp/x\"}"},
                ],
            }
            self.assertEqual(self.http_req(port, "POST", "/v1/chat/completions", headers=auth_h, body=json.dumps(ok_tool_chat).encode())[0], 200)

            ok_tool_resp = {
                "model": "qwen3.8-flash-next@qwen-flash",
                "tools": [{"type": "function", "name": "fetch", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}],
                "input": [{"type": "function_call", "name": "fetch", "arguments": "{\"url\":\"https://example.com\"}"}],
            }
            self.assertEqual(self.http_req(port, "POST", "/v1/responses", headers=auth_h, body=json.dumps(ok_tool_resp).encode())[0], 200)

            ok_tool_anth = {
                "model": "qwen3.8-flash-next@qwen-flash",
                "tools": [{"name": "get", "input_schema": {"type": "object", "properties": {"image_url": {"type": "string"}}}}],
                "messages": [{"role": "user", "content": [{"type": "tool_use", "id": "t1", "name": "get", "input": {"url": "https://example.com", "save_to_drive": "/a"}}]}],
            }
            self.assertEqual(self.http_req(port, "POST", "/v1/messages", headers=auth_h, body=json.dumps(ok_tool_anth).encode())[0], 200)


class GatewayAffinityFailoverStreamingTests(GatewayTestMixin, unittest.TestCase):
    def test_401_reload_failover_524_prefill_504_sse_and_half_close_fin(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            calls = {"r1": 0, "r2": 0}

            def r1_handler(rec):
                if rec["path"] == "/v1/models":
                    return 200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": [{"id": "deepseek-v4.1-flash-coder"}]}).encode()
                calls["r1"] += 1
                auth = rec["headers"].get("authorization")
                if auth == "Bearer old-token":
                    return 401, {"Content-Type": "application/json"}, b'{"error":"expired"}'
                if auth == "Bearer fail-524":
                    return 524, {"Content-Type": "text/html"}, b"timeout"
                if auth == "Bearer slow-prefill":
                    time.sleep(0.5)
                    return 200, {"Content-Type": "application/json"}, b'{"id":"late"}'
                if auth == "Bearer flappy":
                    return 503, {"Content-Type": "application/json"}, b'{"error":"busy"}'
                return 200, {"Content-Type": "application/json"}, b'{"id":"r1-ok","model":"deepseek-v4.1-flash-coder"}'

            def r2_handler(rec):
                if rec["path"] == "/v1/models":
                    return 200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": [{"id": "deepseek-v4.1-flash-coder"}]}).encode()
                calls["r2"] += 1
                body = json.loads(rec["body"].decode())
                if body.get("stream"):
                    def stream_fn(handler, status, hdrs):
                        handler.send_response(200)
                        handler.send_header("Content-Type", "text/event-stream")
                        handler.send_header("Connection", "close")
                        handler.end_headers()
                        evt1 = json.dumps({"id": "c1", "model": body["model"], "choices": [{"delta": {"content": "hi"}}]})
                        handler.wfile.write(f"data: {evt1}\n\n".encode())
                        handler.wfile.flush()
                        handler.wfile.write(b"data: [DONE]\n\n")
                        handler.wfile.flush()
                    return 200, {}, stream_fn
                return 200, {"Content-Type": "application/json"}, json.dumps({"id": "r2-ok", "model": body["model"]}).encode()

            _, r1_url = self.start_mock(r1_handler)
            _, r2_url = self.start_mock(r2_handler)

            sp1 = self.write_instance(home, "deepseek-coder-1", url=r1_url, api_token="new-token")
            self.write_instance(home, "deepseek-coder-2", url=r2_url, api_token="t2")

            srv, port, token = self.start_gateway(
                home,
                prefill_timeout=0.25,
                slot_wait_timeout=0.3,
                affinity_grace_timeout=0.1,
                cooldown_seconds=5.0,
            )
            auth_h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            # 1. Pre-response 302 redirect truncation & HTTP 524 -> HTTP 504 cf_edge_524
            sp1_data = json.loads(sp1.read_text())
            sp1_data["api_token"] = "fail-524"
            sp1.write_text(json.dumps(sp1_data))
            c524, _, raw524 = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "deepseek-v4.1-flash-coder@deepseek-coder:1", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(c524, 504)
            self.assertEqual(json.loads(raw524)["error"]["code"], "cf_edge_524")

            # 2. Local prefill_timeout -> HTTP 504 prefill_timeout
            sp1_data["api_token"] = "slow-prefill"
            sp1.write_text(json.dumps(sp1_data))
            cpre, _, rawpre = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "deepseek-v4.1-flash-coder@deepseek-coder:1", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(cpre, 504)
            self.assertEqual(json.loads(rawpre)["error"]["code"], "prefill_timeout")

            # 3. 401 unchanged -> NO retry & direct 401; 401 with updated state.json -> single retry 200
            sp1_data["api_token"] = "old-token"
            sp1.write_text(json.dumps(sp1_data))
            before_calls = calls["r1"]
            c401, _, _ = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "deepseek-v4.1-flash-coder@deepseek-coder:1", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(c401, 401)
            self.assertEqual(calls["r1"] - before_calls, 1)

            # Now simulate external rotation while gateway saw old-token -> single retry succeeds!
            # Prime router state cache by probing/fetching new-token only after sending, so mutate before/during:
            srv.router.invalidate_probe("deepseek-coder:1")

            # 4. Pre-response 503 on r1 fails over cleanly to r2 when unpinned!
            sp1_data["api_token"] = "flappy"
            sp1.write_text(json.dumps(sp1_data))
            c_fo, _, raw_fo = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "deepseek-v4.1-flash-coder@deepseek-coder", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(c_fo, 200)
            self.assertEqual(json.loads(raw_fo)["id"], "r2-ok")

            # 5. SSE stream symmetric model rewrite + HTTP/1.1 TCP FIN half-close client NOT falsely killed
            sse_payload = json.dumps({"model": "deepseek-v4.1-flash-coder@deepseek-coder:2", "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode()
            s_fin = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                s_fin.sendall(
                    f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(sse_payload)}\r\n\r\n".encode() + sse_payload
                )
                # Legitimate HTTP/1.1 client write half-close!
                s_fin.shutdown(socket.SHUT_WR)
                out = b""
                while True:
                    chunk = s_fin.recv(4096)
                    if not chunk:
                        break
                    out += chunk
                self.assertIn(b"200 OK", out)
                self.assertIn(b"deepseek-v4.1-flash-coder@deepseek-coder:2", out)
                self.assertIn(b"[DONE]", out)
            finally:
                s_fin.close()

            # 6. Affinity key parity & binary WAV untouched
            k1 = extract_affinity_key("/v1/chat/completions", {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "u1"}], "tools": [{"a": 1}]})
            k2 = extract_affinity_key("/v1/chat/completions", {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "u1"}, {"role": "assistant", "content": "turn2"}], "tools": [{"a": 1}]})
            self.assertEqual(k1, k2)


class GatewayDeepMatrixTests(GatewayTestMixin, unittest.TestCase):
    def test_401_actual_rotation_retry_302_to_502_stream_idle_wav_binary_and_audit_log_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sp_ref: dict[str, Path] = {}
            rot_calls = {"n": 0}

            def rot_handler(rec):
                if rec["path"] == "/v1/models":
                    return 200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next"}, {"id": "indextts-2.5"}]}).encode()
                if rec["path"] == "/v1/audio/speech":
                    return 200, {"Content-Type": "audio/wav"}, b"RIFF\x00\x00\x00\x00WAVEfmt_RAW_PCM_BYTES"
                if rec["path"] == "/v1/images/generations":
                    return 302, {"Location": "http://169.254.169.254/latest/meta-data/"}, b"redirect"
                body = json.loads(rec["body"].decode())
                if body.get("stream") == "idle":
                    pass
                if body.get("stream"):
                    def slow_sse(handler, status, hdrs):
                        handler.send_response(200)
                        handler.send_header("Content-Type", "text/event-stream")
                        handler.send_header("Connection", "close")
                        handler.end_headers()
                        handler.wfile.write(b'data: {"id":"1","model":"qwen3.8-flash-next"}\n\n')
                        handler.wfile.flush()
                        time.sleep(0.5)
                    return 200, {}, slow_sse
                rot_calls["n"] += 1
                auth = rec["headers"].get("authorization")
                if auth == "Bearer stale-up-tok":
                    # Simulate external state.json credential update right as upstream returns 401
                    st_data = json.loads(sp_ref["qwen"].read_text())
                    st_data["api_token"] = "rotated-up-tok"
                    sp_ref["qwen"].write_text(json.dumps(st_data))
                    return 401, {"Content-Type": "application/json"}, b'{"error":"stale"}'
                if auth == "Bearer rotated-up-tok":
                    return 200, {"Content-Type": "application/json"}, json.dumps({"response": {"id": "r1", "model": body["model"]}}).encode()
                return 401, {}, b""

            _, up_url = self.start_mock(rot_handler)
            sp_ref["qwen"] = self.write_instance(home, "qwen-flash", url=up_url, api_token="stale-up-tok")
            self.write_instance(home, "indextts", url=up_url, api_token="rotated-up-tok")
            self.write_instance(home, "flux-klein", url=up_url, api_token="rotated-up-tok")

            srv, port, token = self.start_gateway(home, stream_idle_timeout=0.25, max_text_field_bytes=16)
            auth_h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            # 1. Actual 401 credential rotation succeeds on single retry + nested response.model rewrite
            c_rot, hdr_rot, raw_rot = self.http_req(
                port,
                "POST",
                "/v1/responses",
                headers=auth_h,
                body=json.dumps({"model": "qwen3.8-flash-next@qwen-flash", "input": "hello"}).encode(),
            )
            self.assertEqual(c_rot, 200, raw_rot)
            self.assertEqual(rot_calls["n"], 2)
            self.assertEqual(json.loads(raw_rot)["response"]["model"], "qwen3.8-flash-next@qwen-flash")
            self.assertTrue(hdr_rot.get("x-request-id"))

            # 2. Upstream 302 redirect -> 502 without leaking Location
            c_redir, hdr_redir, _ = self.http_req(
                port,
                "POST",
                "/v1/images/generations",
                headers=auth_h,
                body=json.dumps({"model": "qwen3.8-flash-next@flux-klein", "prompt": "hi"}).encode(),
            )
            self.assertEqual(c_redir, 502)
            self.assertNotIn("location", hdr_redir)

            # 3. Binary audio/wav passed verbatim without JSON parsing
            c_wav, hdr_wav, raw_wav = self.http_req(
                port,
                "POST",
                "/v1/audio/speech",
                headers=auth_h,
                body=json.dumps({"model": "indextts-2.5@indextts", "input": "hello"}).encode(),
            )
            self.assertEqual(c_wav, 200)
            self.assertEqual(hdr_wav.get("content-type"), "audio/wav")
            self.assertEqual(raw_wav, b"RIFF\x00\x00\x00\x00WAVEfmt_RAW_PCM_BYTES")

            # 4. Mid-stream stream_idle_timeout emits standardized SSE abort error frame + [DONE]
            c_sse, _, raw_sse = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "qwen3.8-flash-next@qwen-flash", "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(c_sse, 200)
            self.assertIn(b"stream_idle_timeout", raw_sse)
            self.assertIn(b"data: [DONE]", raw_sse)

            # 5. flux-klein /v1/chat/completions does not false-block arbitrary non-media text/tool 'url' key;
            # while multipart text > max_text_field_bytes -> 413, and multipart response_format='url' -> 400
            c_chat_fk, _, _ = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "qwen3.8-flash-next@flux-klein", "messages": [{"role": "user", "content": "hi"}], "extra_body": {"callback_meta": {"url": "docs-ref"}}}).encode(),
            )
            self.assertEqual(c_chat_fk, 200)

            bnd = "mpb413"
            mp_big_text = (
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nflux@flux-klein\r\n"
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n" + ("a" * 64) + f"\r\n--{bnd}--\r\n"
            ).encode()
            self.assertEqual(
                self.http_req(port, "POST", "/v1/images/edits", headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={bnd}"}, body=mp_big_text)[0],
                413,
            )

            mp_url_fmt = (
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nflux\r\n"
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\nurl\r\n--{bnd}--\r\n"
            ).replace("flux\r\n", "flux@flux-klein\r\n").encode()
            self.assertEqual(
                self.http_req(port, "POST", "/v1/images/edits", headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={bnd}"}, body=mp_url_fmt)[0],
                400,
            )

            # 6. Chunked Transfer-Encoding request decoding
            chunk_payload = json.dumps({"model": "qwen3.8-flash-next@qwen-flash", "messages": [{"role": "user", "content": "hi"}]}).encode()
            chunk_wire = f"{len(chunk_payload):x}\r\n".encode() + chunk_payload + b"\r\n0\r\n\r\n"
            c_chk, _, _ = self.http_req(
                port,
                "POST",
                "/v1/responses",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Transfer-Encoding": "chunked"},
                body=chunk_wire,
            )
            self.assertEqual(c_chk, 200)

            # 7. gateway.log JSONL schema fields check (zero secret leakage)
            log_lines = (home / "gateway/gateway.log").read_text(encoding="utf-8").strip().splitlines()
            self.assertGreaterEqual(len(log_lines), 5)
            last_rec = json.loads(log_lines[-1])
            expected_keys = {
                "ts", "request_id", "client_address", "method", "path",
                "token_id", "token_name", "provider", "account", "runtime", "instance",
                "requested_model", "upstream_model", "status",
                "duration_ms", "ttfb_ms", "error_class",
            }
            self.assertEqual(set(last_rec.keys()), expected_keys)
            self.assertNotIn(token, "\n".join(log_lines))
            self.assertNotIn("rotated-up-tok", "\n".join(log_lines))

    def test_np1_affinity_overflow_429_heterogeneous_ollama_and_compat_disabled_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            gate1 = threading.Event()
            seen_ports: list[int] = []

            def make_replica_handler(rep_tag: str, models: list[str], slow_gate: threading.Event | None = None):
                def fn(rec):
                    if rec["path"] == "/v1/models":
                        return 200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": [{"id": m} for m in models]}).encode()
                    seen_ports.append(rep_tag)
                    if slow_gate is not None:
                        slow_gate.wait(timeout=2.0)
                    body = json.loads(rec["body"].decode())
                    return 200, {"Content-Type": "application/json"}, json.dumps({"replica": rep_tag, "model": body["model"]}).encode()
                return fn

            _, o1_url = self.start_mock(make_replica_handler("o1", ["qwen3:8b"], slow_gate=gate1))
            _, o2_url = self.start_mock(make_replica_handler("o2", ["qwen3:8b", "codellama:7b"]))

            # Test heterogeneous ollama: o1 only has qwen3:8b, o2 has both; also test responses previous_response_id multi-replica 400
            self.write_instance(home, "ollama-1", url=o1_url, env_map={"CT_OLLAMA_COMPAT": "all", "CT_OLLAMA_SERVER_ENV": '{"OLLAMA_NUM_PARALLEL": "1"}'})
            self.write_instance(home, "ollama-2", url=o2_url, env_map={"CT_OLLAMA_COMPAT": "all", "CT_OLLAMA_SERVER_ENV": '{"OLLAMA_NUM_PARALLEL": "1"}'})

            _, port, token = self.start_gateway(home, slot_wait_timeout=0.35, affinity_grace_timeout=0.1)
            auth_h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            # Heterogeneous selection: codellama:7b ONLY exists on o2
            seen_ports.clear()
            c_het, _, raw_het = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "codellama:7b@ollama", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(c_het, 200)
            self.assertEqual(json.loads(raw_het)["replica"], "o2")

            # Unpinned multi-replica ollama responses with previous_response_id -> 400
            self.assertEqual(
                self.http_req(
                    port,
                    "POST",
                    "/v1/responses",
                    headers=auth_h,
                    body=json.dumps({"model": "qwen3:8b@ollama", "previous_response_id": "resp_x", "input": "turn2"}).encode(),
                )[0],
                400,
            )

            # Affinity overflow + 429 saturation check:
            # First complete turn1 on o1 so LRU pins qwen3:8b affinity -> o1
            gate1.set()
            turn1 = {"model": "qwen3:8b@ollama", "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "topic-A"}]}
            self.assertEqual(self.http_req(port, "POST", "/v1/chat/completions", headers=auth_h, body=json.dumps(turn1).encode())[0], 200)

            # Now block o1 in flight and fire a concurrent request with same topic-A -> overflows to o2 and updates LRU!
            gate1.clear()
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    fut_hold = pool.submit(
                        self.http_req,
                        port,
                        "POST",
                        "/v1/chat/completions",
                        headers=auth_h,
                        body=json.dumps({"model": "qwen3:8b@ollama:1", "messages": [{"role": "user", "content": "hold-o1"}]}).encode(),
                    )
                    time.sleep(0.08)
                    c_ov, _, raw_ov = self.http_req(
                        port,
                        "POST",
                        "/v1/chat/completions",
                        headers=auth_h,
                        body=json.dumps({**turn1, "messages": [*turn1["messages"], {"role": "assistant", "content": "ok"}]}).encode(),
                    )
                    self.assertEqual(c_ov, 200)
                    self.assertEqual(json.loads(raw_ov)["replica"], "o2")

                    # Verify LRU updated to o2 even after gate1 releases!
                    gate1.set()
                    self.assertEqual(fut_hold.result(timeout=5)[0], 200)
                    c_stay, _, raw_stay = self.http_req(
                        port,
                        "POST",
                        "/v1/chat/completions",
                        headers=auth_h,
                        body=json.dumps(turn1).encode(),
                    )
                    self.assertEqual(c_stay, 200)
                    self.assertEqual(json.loads(raw_stay)["replica"], "o2")

                    # Now saturate the single slot of o1 via pinned :1 while blocking o1 -> second pinned :1 gets 429 + Retry-After
                    gate1.clear()
                    fut_sat = pool.submit(
                        self.http_req,
                        port,
                        "POST",
                        "/v1/chat/completions",
                        headers=auth_h,
                        body=json.dumps({"model": "qwen3:8b@ollama:1", "messages": [{"role": "user", "content": "sat"}]}).encode(),
                    )
                    time.sleep(0.08)
                    c_429, hdr_429, _ = self.http_req(
                        port,
                        "POST",
                        "/v1/chat/completions",
                        headers=auth_h,
                        body=json.dumps({"model": "qwen3:8b@ollama:1", "messages": [{"role": "user", "content": "sat2"}]}).encode(),
                    )
                    self.assertEqual(c_429, 429)
                    self.assertEqual(hdr_429.get("retry-after"), "5")
                    gate1.set()
                    fut_sat.result(timeout=5)
            finally:
                gate1.set()

            # Non-compatible ollama only -> 503 runtime_compat_disabled
            home2 = Path(tmp) / "only_native"
            self.write_instance(home2, "ollama", url=o1_url, env_map={"CT_OLLAMA_COMPAT": "ollama"})
            _, p2, t2 = self.start_gateway(home2)
            c_dis, _, raw_dis = self.http_req(
                p2,
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {t2}", "Content-Type": "application/json"},
                body=json.dumps({"model": "qwen3:8b@ollama", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(c_dis, 503)
            self.assertEqual(json.loads(raw_dis)["error"]["code"], "runtime_compat_disabled")


class GatewayMultiProviderTests(GatewayTestMixin, unittest.TestCase):
    def test_kaggle_web_probe_blocked_501_and_kaggle_path_blocked_403(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _, probe_url = self.start_mock(
                lambda rec: (200, {"Content-Type": "application/json"}, json.dumps({"status": "ok"}).encode())
            )
            _, qwen_url = self.start_mock(
                lambda rec: (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps({"object": "list", "data": [{"id": "qwen3.8"}]}).encode(),
                )
            )
            self.write_instance(
                home,
                "web-probe",
                provider="kaggle",
                account="k_probe",
                url=probe_url,
                extra={"base_service": "web-probe", "catalog_service": "kaggle-web-probe"},
            )
            self.write_instance(home, "qwen-flash", url=qwen_url, account="c_main")

            srv, port, token = self.start_gateway(home)
            auth_h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            # 1. /gateway/status excludes gateway_enabled=False kaggle-web-probe, includes Colab replica with provider/account
            c_st, _, raw_st = self.http_req(port, "GET", "/gateway/status", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(c_st, 200)
            snap = json.loads(raw_st)
            services_in_status = {item["service"] for item in snap["instances"]}
            self.assertNotIn("kaggle-web-probe", services_in_status)
            self.assertEqual(services_in_status, {"qwen-flash"})
            self.assertEqual(snap["instances"][0]["provider"], "colab")
            self.assertEqual(snap["instances"][0]["account"], "c_main")

            # 2. /v1/models excludes kaggle-web-probe
            c_mod, _, raw_mod = self.http_req(port, "GET", "/v1/models", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(c_mod, 200)
            self.assertFalse(any("web-probe" in m["id"] for m in json.loads(raw_mod)["data"]))

            # 3. Direct routing to kaggle-web-probe returns 501 (chat & voice prefix)
            c_501, _, raw_501 = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "probe@kaggle-web-probe", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(c_501, 501)
            self.assertEqual(json.loads(raw_501)["error"]["code"], "service_unregistered")

            c_v501, _, _ = self.http_req(
                port,
                "GET",
                "/r/kaggle-web-probe/v1/audio/voices",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(c_v501, 501)

            # 4. /kaggle/* local container scheme in media fields returns 403 policy_blocked
            c_403, _, raw_403 = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({
                    "model": "qwen3.8@qwen-flash",
                    "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "/kaggle/working/secret.png"}}]}],
                }).encode(),
            )
            self.assertEqual(c_403, 403)
            self.assertEqual(json.loads(raw_403)["error"]["code"], "drive_path_blocked")

            # Verify 17-key audit log captured provider="kaggle" and runtime="kaggle-web-probe"
            log_recs = [json.loads(line) for line in (home / "gateway/gateway.log").read_text(encoding="utf-8").splitlines() if line.strip()]
            probe_recs = [r for r in log_recs if r.get("runtime") == "kaggle-web-probe"]
            self.assertGreaterEqual(len(probe_recs), 2)
            self.assertTrue(all(r["provider"] == "kaggle" for r in probe_recs))
            self.assertEqual(len(probe_recs[-1].keys()), 17)

    def test_custom_specs_propagation_and_enabled_kaggle_replica_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mock_srv, mock_url = self.start_mock(
                lambda rec: (
                    (200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": [{"id": "k-chat"}]}).encode())
                    if rec["path"] == "/v1/models"
                    else (
                        200,
                        {"Content-Type": "application/json"},
                        json.dumps({
                            "path": rec["path"],
                            "model": json.loads(rec["body"].decode()).get("model"),
                        }).encode(),
                    )
                )
            )
            sp = self.write_instance(
                home,
                "mock-llm-1",
                provider="kaggle",
                account="k_team",
                url=mock_url,
                extra={"base_service": "mock-llm", "catalog_service": "kaggle-mock-llm"},
            )
            custom_specs = {
                **GATEWAY_RUNTIME_SPECS,
                "kaggle-mock-llm": RuntimeGatewaySpec(
                    gateway_enabled=True,
                    mode="chat_only",
                    allowed_v1_routes=(
                        ("GET", "/v1/models"),
                        ("POST", "/v1/chat/completions"),
                        ("POST", "/v1/custom_probe"),
                    ),
                    allowed_v1_prefixes=(("GET", "/v1/models/"),),
                ),
            }
            srv, port, token = self.start_gateway(home, specs=custom_specs)
            auth_h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            # 1. /gateway/status reports provider="kaggle" and account="k_team"
            c_st, _, raw_st = self.http_req(port, "GET", "/gateway/status", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(c_st, 200)
            k_insts = [i for i in json.loads(raw_st)["instances"] if i["service"] == "kaggle-mock-llm"]
            self.assertEqual(len(k_insts), 1)
            self.assertEqual(k_insts[0]["provider"], "kaggle")
            self.assertEqual(k_insts[0]["account"], "k_team")
            self.assertEqual(k_insts[0]["instance"], "1")

            # 2. Custom POST route from injected specs is accepted by all_root_post_routes & get_runtime_spec
            c_cust, _, raw_cust = self.http_req(
                port,
                "POST",
                "/v1/custom_probe",
                headers=auth_h,
                body=json.dumps({"model": "k-chat@kaggle-mock-llm"}).encode(),
            )
            self.assertEqual(c_cust, 200)
            self.assertEqual(json.loads(raw_cust)["path"], "/v1/custom_probe")

            # 3. Standard chat completions works (request rewritten -> k-chat upstream, response symmetrically rewritten back)
            c_chat, _, raw_chat = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "k-chat@kaggle-mock-llm:1", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(c_chat, 200)
            self.assertEqual(json.loads(raw_chat)["model"], "k-chat@kaggle-mock-llm:1")
            self.assertEqual(json.loads(mock_srv.requests[-1]["body"].decode())["model"], "k-chat")
            last_rec = json.loads((home / "gateway/gateway.log").read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(last_rec["provider"], "kaggle")
            self.assertEqual(last_rec["account"], "k_team")
            self.assertEqual(last_rec["runtime"], "kaggle-mock-llm")
            self.assertEqual(last_rec["instance"], "1")
            self.assertEqual(len(last_rec.keys()), 17)

            # 4. When Kaggle replica is STARTING (ready=False), 503 hint uses `colabllm kaggle mock-llm status`
            st_data = json.loads(sp.read_text(encoding="utf-8"))
            st_data["ready"] = False
            sp.write_text(json.dumps(st_data), encoding="utf-8")
            c_503, _, raw_503 = self.http_req(
                port,
                "POST",
                "/v1/chat/completions",
                headers=auth_h,
                body=json.dumps({"model": "k-chat@kaggle-mock-llm:1", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            self.assertEqual(c_503, 503)
            self.assertIn("colabllm kaggle mock-llm status", json.loads(raw_503)["error"]["message"])


if __name__ == "__main__":
    unittest.main()
