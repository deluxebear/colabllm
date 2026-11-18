"""Hermetic unit and fake-CLI tests for Kaggle runtime, runtime_core, and multi-account control."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/colabllm/lib"))

from cli_support import CliError, UsageError, duration_seconds, validate_account_syntax  # noqa: E402
import colab_ws  # noqa: E402
import frp_backend  # noqa: E402
import kaggle_accounts  # noqa: E402
import kaggle_ws  # noqa: E402
import kagglesvc  # noqa: E402
import mux  # noqa: E402
from processes import owned_process, stop_owned_process  # noqa: E402
from protocol import NotebookProtocol  # noqa: E402
import runtime_core  # noqa: E402


def _load_module(rel_path: str, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / rel_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _unquote_env_like_kaggle_ext(raw_line: str) -> tuple[str, str]:
    key, _, raw_val = raw_line.partition("=")
    assert raw_val.startswith('"') and raw_val.endswith('"')
    inner = raw_val[1:-1]
    val = (
        inner.replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )
    return key, val


class RuntimeCoreAndProtocolTest(unittest.TestCase):
    def test_duration_seconds_and_account_syntax(self):
        self.assertEqual(duration_seconds("6h"), 21600)
        self.assertEqual(duration_seconds("15m"), 900)
        self.assertEqual(duration_seconds("45s"), 45)
        self.assertEqual(duration_seconds("120"), 120)
        for bad in ("0", "-5", "0h", "abc", "10d", True):
            with self.assertRaises(ValueError, msg=bad):
                duration_seconds(bad)
        self.assertEqual(validate_account_syntax(" @alice "), "@alice")
        for bad in ("../etc", "a/b", "a\\b", "", "-bad"):
            with self.assertRaises(UsageError, msg=bad):
                validate_account_syntax(bad)

    def test_locked_state_permissions_and_hostname_prefix_no_double_kaggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st = runtime_core.LockedState("web-probe", runtime_core.KAGGLE_NAMESPACE, state_root=root)
            st.save(provider="kaggle", ok=True)
            self.assertEqual(st.dir, root / "kaggle/services/web-probe")
            self.assertEqual(stat.S_IMODE((root / "kaggle").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(st.dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(st.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((st.dir / ".state.lock").stat().st_mode), 0o600)

            svc1 = kagglesvc.KaggleService("web-probe", "web-probe.ipynb", 18090, default_hostname="kaggle-web-probe")
            svc2 = kagglesvc.KaggleService("web-probe", "web-probe.ipynb", 18090, default_hostname=None)
            args = types.SimpleNamespace(quick_tunnel=False, domain=None, base_domain="example.com")
            self.assertEqual(
                runtime_core.tunnel_hostname(svc1, args, namespace=runtime_core.KAGGLE_NAMESPACE, state_root=root),
                "kaggle-web-probe.example.com",
            )
            self.assertEqual(
                runtime_core.tunnel_hostname(svc2, args, namespace=runtime_core.KAGGLE_NAMESPACE, state_root=root),
                "kaggle-web-probe.example.com",
            )

    def test_bidirectional_dns_ownership_blocks_cross_provider_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc = kagglesvc.KaggleService("web-probe", "web-probe.ipynb", 18090)
            # 1. FRP record owned by colabsvc-frp must NOT be claimed by Kaggle CF tunnel
            foreign_records = [{"id": "r1", "type": "A", "content": "1.2.3.4", "comment": "colabsvc-frp pid web-probe"}]

            def fake_cf(*args):
                if args[:2] == ("tunnels", "list"):
                    return [{"id": "tid-1", "name": "kaggle-web-probe"}]
                if args[:2] == ("zones", "list"):
                    return [{"id": "z1", "name": "example.com"}]
                if args[:2] == ("dns", "records") and args[2] == "list":
                    return foreign_records
                return {}

            with self.assertRaises(CliError):
                runtime_core.ensure_named_tunnel(
                    svc, "kaggle-web-probe.example.com", namespace=runtime_core.KAGGLE_NAMESPACE, state_root=root, cf_json_fn=fake_cf
                )

            # 2. Same-provider FRP record CAN be switched to CF tunnel
            k_state = runtime_core.LockedState("web-probe", runtime_core.KAGGLE_NAMESPACE, state_root=root)
            k_state.save(frp_route={"dns_owner": "kagglesvc-frp pid web-probe"})
            owned_records = [{"id": "r1", "type": "A", "content": "1.2.3.4", "comment": "kagglesvc-frp pid web-probe"}]
            edits = []

            def fake_cf_switch(*args):
                if args[:2] == ("tunnels", "list"):
                    return [{"id": "tid-1", "name": "kaggle-web-probe"}]
                if args[:2] == ("zones", "list"):
                    return [{"id": "z1", "name": "example.com"}]
                if args[:2] == ("dns", "records") and args[2] == "list":
                    return owned_records
                if args[:3] == ("dns", "records", "edit"):
                    edits.append(json.loads(args[-1]))
                    return {"id": "r1"}
                if args[:3] == ("tunnels", "token", "get"):
                    return "tok"
                return {}

            res = runtime_core.ensure_named_tunnel(
                svc, "kaggle-web-probe.example.com", namespace=runtime_core.KAGGLE_NAMESPACE, state_root=root, cf_json_fn=fake_cf_switch
            )
            self.assertEqual(res["tunnel_name"], "kaggle-web-probe")
            self.assertEqual(edits[0]["comment"], "kagglesvc web-probe")

    def test_single_line_env_escape_roundtrip_and_selective_redactor(self):
        ca = "-----BEGIN CERTIFICATE-----\nLINE\"TWO\\SLASH\tTAB\rRET\n-----END CERTIFICATE-----\n"
        formatted = runtime_core.format_single_line_env_file({
            "CT_SERVICE": "web-probe",
            "CT_FRP_CA_CERT": ca,
            "CT_AUTH_PASS": "s3cr3t\"pass",
            "CT_TUNNEL_TOKEN": "",
        })
        lines = formatted.strip().splitlines()
        self.assertEqual(len(lines), 4)
        parsed = dict(_unquote_env_like_kaggle_ext(line) for line in lines)
        self.assertEqual(parsed["CT_FRP_CA_CERT"], ca)
        self.assertEqual(parsed["CT_AUTH_PASS"], 's3cr3t"pass')
        self.assertEqual(parsed["CT_TUNNEL_TOKEN"], "")

        redactor = runtime_core.LineBufferedSecretRedactor(known_secrets=("my-super-pass", "sk-secret123456"))
        chunk1 = "CT_STAGE=install\nCT_CELL=1/5:install\nCT_AUTH_PASS=should"
        chunk2 = "-hide-me\nAuthorization: Bearer topsecret\nLiteral my-super-pass here sk-secret123456\nCT_READY\n"
        out = redactor.feed(chunk1) + redactor.feed(chunk2) + redactor.feed("", final=True)
        self.assertIn("CT_STAGE=install\n", out)
        self.assertIn("CT_CELL=1/5:install\n", out)
        self.assertIn("CT_READY\n", out)
        self.assertIn("CT_AUTH_PASS=[REDACTED]", out)
        self.assertIn("Authorization: Bearer [REDACTED]", out)
        self.assertNotIn("should-hide-me", out)
        self.assertNotIn("topsecret", out)
        self.assertNotIn("my-super-pass", out)
        self.assertNotIn("sk-secret123456", out)

    def test_notebook_protocol_kaggle_markers(self):
        proto = NotebookProtocol(provider="kaggle")
        u1 = proto.feed(
            b"CT_CELL=0/5:setup\nCT_STAGE=setup\nCT_DISK_STAT path=/kaggle/working free_mb=19200\n"
            b"CT_CACHE_EVENT artifact=caddy status=hit\nCT_PUBLIC_URL=https://probe.example.com\nCT_READY\n"
        )
        self.assertEqual(u1["stage"], "setup")
        self.assertEqual(u1["cell"], "0/5:setup")
        self.assertEqual(u1["disk_stats"], [{"path": "/kaggle/working", "free_mb": 19200}])
        self.assertEqual(u1["cache_events"], [{"artifact": "caddy", "status": "hit"}])
        self.assertEqual(u1["url"], "https://probe.example.com")
        self.assertTrue(u1["ready"])

        prefail = NotebookProtocol(provider="kaggle")
        prefail.feed(b"[session] Kernel session creation timed out\n")
        self.assertEqual(prefail.preflight_error, "[session] Kernel session creation timed out")


class KaggleAccountsTest(unittest.TestCase):
    def test_prefer_saved_precedence_and_env_sanitization(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "kaggle-ext"
            cfg.mkdir()
            (cfg / "accounts.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "active": "main",
                        "accounts": {
                            "main": {"username": "alice_main", "auth_method": "oauth"},
                            "gpu2": {"username": "bob_gpu2", "auth_method": "access_token"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "KAGGLE_EXT_CONFIG_DIR": str(cfg),
                "COLABLLM_KAGGLE_ACCOUNT": "gpu2",
                "KAGGLE_ACCOUNT": "main",
                "KAGGLE_USERNAME": "evil_legacy",
                "KAGGLE_KEY": "legacy_key",
                "KAGGLE_API_TOKEN": "KGAT_legacy",
                "KAGGLE_CONFIG_DIR": "/tmp/legacy",
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(kaggle_accounts.list_kaggle_account_names(), ["gpu2", "main"])
                self.assertEqual(
                    kaggle_accounts.resolve_effective_kaggle_account(explicit="@bob_gpu2"),
                    ("gpu2", "flag"),
                )
                self.assertEqual(
                    kaggle_accounts.resolve_effective_kaggle_account(saved="main", prefer_saved=False),
                    ("gpu2", "env"),
                )
                self.assertEqual(
                    kaggle_accounts.resolve_effective_kaggle_account(saved="main", prefer_saved=True),
                    ("main", "state"),
                )
                clean_env = kaggle_accounts.kaggle_subprocess_env("main")
                for key in ("KAGGLE_USERNAME", "KAGGLE_KEY", "KAGGLE_API_TOKEN", "KAGGLE_CONFIG_DIR"):
                    self.assertNotIn(key, clean_env)
                self.assertEqual(clean_env["KAGGLE_ACCOUNT"], "main")
                self.assertEqual(clean_env["KAGGLE_EXT_CONFIG_DIR"], str(cfg))


class KaggleCliAndLifecycleTest(unittest.TestCase):
    def test_cli_single_execv_dispatch_and_account_rules(self):
        cli = _load_module("src/colabllm/cli.py", "colabllm_cli_kaggle_test")
        with patch.object(cli.os, "execv") as execv:
            cli.main(["kaggle", "-a", "@alice", "web-probe", "up", "--quick-tunnel"])
            execv.assert_called_once_with(
                sys.executable,
                [
                    sys.executable,
                    str(ROOT / "src/colabllm/kaggle_services/web-probe.py"),
                    "--account",
                    "@alice",
                    "up",
                    "--quick-tunnel",
                ],
            )
        with patch.object(cli.os, "execv") as execv:
            cli.main(["kaggle", "services"])
            execv.assert_called_once_with(
                sys.executable,
                [sys.executable, str(ROOT / "src/colabllm/kaggle_cli.py"), "services"],
            )
        # Colab services still reject @-prefixed account syntax
        with patch.object(cli.os, "execv") as execv, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--account", "@alice", "ollama", "info"]), 2)
            execv.assert_not_called()

    def test_preflight_validation_dry_run_and_quota_gate(self):
        web_probe = _load_module("src/colabllm/kaggle_services/web-probe.py", "web_probe_under_test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "profile"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            calls_log = root / "kaggle-calls.log"
            (fake_bin / "kaggle").write_text(
                f"#!/bin/sh\necho \"$*\" >> {json.dumps(str(calls_log))}\nexit 99\n",
                encoding="utf-8",
            )
            (fake_bin / "kaggle").chmod(0o755)
            kcfg = root / "kaggle-ext"
            kcfg.mkdir()
            (kcfg / "accounts.json").write_text(json.dumps({"version": 1, "active": "acct_ok", "accounts": {"acct_ok": {}, "acct1": {}}}), encoding="utf-8")
            env = {
                **os.environ,
                "COLABLLM_HOME": str(home),
                "KAGGLE_EXT_CONFIG_DIR": str(kcfg),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            }

            # 1. web-probe dry-run allows --accelerator NvidiaTeslaT4 and creates zero state / zero kaggle calls
            res = subprocess.run(
                [str(ROOT / "colabllm"), "kaggle", "web-probe", "up", "--dry-run", "--quick-tunnel", "--accelerator", "NvidiaTeslaT4"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            plan = json.loads(res.stdout)
            self.assertEqual(plan["accelerator"], "NvidiaTeslaT4")
            self.assertFalse(home.exists())
            self.assertFalse(calls_log.exists())

            # 2. web-probe rejects non-CPU without --dry-run
            res_cpu_only = subprocess.run(
                [str(ROOT / "colabllm"), "kaggle", "web-probe", "up", "--accelerator", "NvidiaTeslaT4"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_cpu_only.returncode, 2)

            # 3. 48-char session limit rejected up front
            res_long = subprocess.run(
                [str(ROOT / "colabllm"), "kaggle", "web-probe", "up", "-i", "x" * 45, "--dry-run"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_long.returncode, 2)

            # 4. --no-internet rejected up front
            res_no_inet = subprocess.run(
                [str(ROOT / "colabllm"), "kaggle", "web-probe", "up", "--no-internet", "--dry-run"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_no_inet.returncode, 2)

            # 5. Reserved port 8888 blocked
            svc_bad_port = kagglesvc.KaggleService("bad-port", "web-probe.ipynb", 8888)
            with self.assertRaises(UsageError):
                kagglesvc._check_reserved_ports(svc_bad_port)

            # 6. Quota gate fail-closed for low / null / unauthenticated accounts
            low_quota = [{"account": "empty", "active": True, "authenticated": True, "gpu_quota": {"remaining_hours": 0.05}}]
            null_quota = [{"account": "empty", "active": True, "authenticated": True, "gpu_quota": None}]
            unauth = [{"account": "empty", "active": True, "authenticated": False, "gpu_quota": {"remaining_hours": 20.0}}]
            for payload in (low_quota, null_quota, unauth):
                with patch.object(kagglesvc, "_run_kaggle_json", return_value=payload):
                    with self.assertRaises(CliError):
                        kagglesvc._check_non_cpu_quota("NvidiaTeslaT4", "empty")
            ok_quota = [{"account": "ok", "active": True, "authenticated": True, "gpu_quota": {"remaining_hours": 20.0}}]
            with patch.object(kagglesvc, "_run_kaggle_json", return_value=ok_quota):
                self.assertEqual(kagglesvc._check_non_cpu_quota("NvidiaTeslaT4", "ok")["account"], "ok")

    def _write_fake_kaggle(self, fake_bin: Path, script_body: str) -> None:
        p = fake_bin / "kaggle"
        p.write_text("#!/usr/bin/env python3\n" + script_body, encoding="utf-8")
        p.chmod(0o755)

    def test_pre_ready_failure_stops_remote_session_and_marks_orphan_when_stop_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "profile"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            events = root / "events.log"
            self._write_fake_kaggle(
                fake_bin,
                f"""
import json, os, sys
events = open({json.dumps(str(events))}, "a")
events.write(" ".join(sys.argv[1:]) + "\\n")
events.close()
args = sys.argv[1:]
if "-a" in args:
    idx = args.index("-a")
    args = args[:idx] + args[idx+2:]
if args[:2] == ["session", "status"]:
    print(json.dumps({{"alive": False, "live_state": "reclaimed"}}))
    sys.exit(1)
if args[:2] == ["session", "create"]:
    print(json.dumps({{"name": args[2], "account": "acct1", "kernel_session_id": 777, "state": "active", "accelerator": "cpu", "enable_internet": True}}))
    sys.exit(0)
if args[:2] == ["session", "exec"]:
    print("[session] Failed before CT_READY", flush=True)
    sys.exit(1)
if args[:2] == ["session", "stop"]:
    if os.environ.get("FAKE_STOP_FAIL") == "1":
        print("network error", file=sys.stderr)
        sys.exit(2)
    print("[]")
    sys.exit(0)
""",
            )
            kcfg = root / "kaggle-ext"
            kcfg.mkdir()
            (kcfg / "accounts.json").write_text(json.dumps({"version": 1, "active": "acct_ok", "accounts": {"acct_ok": {}, "acct1": {}}}), encoding="utf-8")
            env = {
                **os.environ,
                "COLABLLM_HOME": str(home),
                "KAGGLE_EXT_CONFIG_DIR": str(kcfg),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            }

            # Case 1: stop succeeds -> runtime.env removed and kernel_session_id cleared
            res = subprocess.run(
                [str(ROOT / "colabllm"), "kaggle", "-a", "acct1", "web-probe", "up", "--quick-tunnel", "--foreground"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            recorded = events.read_text()
            self.assertIn("session stop web-probe --json", recorded)
            self.assertFalse((home / "kaggle/services/web-probe/runtime.env").exists())
            st = json.loads((home / "kaggle/services/web-probe/state.json").read_text())
            self.assertNotIn("kernel_session_id", st)

            # Case 2: stop fails -> state.json preserved and ORPHAN_SUSPECTED logged
            env_fail = {**env, "FAKE_STOP_FAIL": "1"}
            res2 = subprocess.run(
                [str(ROOT / "colabllm"), "kaggle", "-a", "acct1", "web-probe", "up", "--quick-tunnel", "--foreground"],
                env=env_fail,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res2.returncode, 0)
            run_log = (home / "kaggle/services/web-probe/run.log").read_text()
            self.assertIn("ORPHAN_SUSPECTED", run_log)
            self.assertIn("kernel_session_id=777", run_log)
            st2 = json.loads((home / "kaggle/services/web-probe/state.json").read_text())
            self.assertEqual(st2["kernel_session_id"], 777)

    def test_four_branch_watchdog_max_uptime_and_restart_handoff(self):
        import http.server

        class _HealthSrv(http.server.BaseHTTPRequestHandler):
            healthy = True

            def log_message(self, *args):
                return

            def do_GET(self):
                if self.path == "/health" and self.healthy:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
                else:
                    self.send_response(502)
                    self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthSrv)
        srv.daemon_threads = True
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(srv.shutdown)
        probe_url = f"http://127.0.0.1:{srv.server_address[1]}"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "profile"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            status_queue_file = root / "status_queue.json"
            status_queue_file.write_text(
                json.dumps(["running", "unreachable", "reclaimed", "reclaimed"]),
                encoding="utf-8",
            )
            calls_file = root / "calls.txt"
            self._write_fake_kaggle(
                fake_bin,
                f"""
import json, os, sys, time
calls = open({json.dumps(str(calls_file))}, "a")
calls.write(" ".join(sys.argv[1:]) + "\\n")
calls.close()
args = sys.argv[1:]
if "-a" in args:
    idx = args.index("-a")
    args = args[:idx] + args[idx+2:]
if args[:2] == ["session", "status"]:
    q_path = {json.dumps(str(status_queue_file))}
    try:
        q = json.loads(open(q_path, encoding="utf-8").read())
    except Exception:
        q = []
    live = q.pop(0) if q else "reclaimed"
    with open(q_path, "w", encoding="utf-8") as qf:
        json.dump(q, qf)
    print(json.dumps({{"alive": live == "running", "live_state": live}}))
    sys.exit(0 if live != "reclaimed" else 1)
if args[:2] == ["session", "create"]:
    print(json.dumps({{"name": args[2], "account": "acct_ok", "kernel_session_id": 888, "state": "active", "accelerator": "cpu", "enable_internet": True}}))
    sys.exit(0)
if args[:2] == ["session", "exec"]:
    print("CT_PUBLIC_URL={probe_url}", flush=True)
    print("CT_READY", flush=True)
    time.sleep(0.05)
    sys.exit(1)
if args[:3] == ["session", "logs", "-f"]:
    print("[heartbeat] still alive via logs -f", flush=True)
    time.sleep(0.05)
    sys.exit(0)
if args[:2] == ["session", "restart"]:
    print(json.dumps({{"restarted": True}}))
    sys.exit(0)
if args[:2] == ["session", "stop"]:
    print("[]")
    sys.exit(0)
""",
            )
            kcfg = root / "kaggle-ext"
            kcfg.mkdir()
            (kcfg / "accounts.json").write_text(json.dumps({"version": 1, "active": "acct_ok", "accounts": {"acct_ok": {}, "acct1": {}}}), encoding="utf-8")
            env = {
                **os.environ,
                "COLABLLM_HOME": str(home),
                "KAGGLE_EXT_CONFIG_DIR": str(kcfg),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env):
                svc = kagglesvc.KaggleService("web-probe", "web-probe.ipynb", 18090, auth="none")
                state = kagglesvc._state_for(svc)
                nb = root / "web-probe.ipynb"
                nb.write_text(json.dumps({"cells": []}))
                kagglesvc.write_private_env_file(kagglesvc._runtime_env_path(state), {"CT_SERVICE": "web-probe"})
                state.save(
                    provider="kaggle",
                    service="web-probe",
                    base_service="web-probe",
                    kaggle_session="web-probe",
                    account="acct_ok",
                    kernel_session_id=777,
                    accelerator="cpu",
                    auto_restart=True,
                    max_restarts=1,
                    restart_delay=0.05,
                    idle_check_seconds=2.0,
                    restarts=0,
                    ever_ready=False,
                )

                rc = kagglesvc.exec_runner(svc, nb)
                self.assertEqual(rc, 1)  # Stopped cleanly after max_restarts=1 exhausted on second reclaimed

                calls = calls_file.read_text()
                self.assertIn("session logs -f web-probe", calls)
                self.assertIn("session create web-probe", calls)
                # Ensure first logs -f reattach did NOT increment restarts past max_restarts=1 before recreate
                st_end = state.load()
                self.assertEqual(st_end.get("restarts"), 1)
                self.assertEqual(st_end.get("session_recreate_count"), 1)

            # Host-side --max-uptime stop check on non-CPU session
            calls_file.write_text("", encoding="utf-8")
            with patch.dict(os.environ, env):
                svc_gpu = kagglesvc.KaggleService("gpu-probe", "web-probe.ipynb", 18090, auth="none")
                state_gpu = kagglesvc._state_for(svc_gpu)
                kagglesvc.write_private_env_file(kagglesvc._runtime_env_path(state_gpu), {"CT_SERVICE": "gpu-probe"})
                state_gpu.save(
                    provider="kaggle",
                    service="gpu-probe",
                    base_service="gpu-probe",
                    kaggle_session="gpu-probe",
                    account="acct_ok",
                    kernel_session_id=999,
                    accelerator="NvidiaTeslaT4",
                    max_uptime=1,
                    started_mono=time.monotonic() - 10.0,
                    ever_ready=True,
                )
                rc_up = kagglesvc.exec_runner(svc_gpu, nb)
                self.assertEqual(rc_up, 0)
                self.assertIn("session stop gpu-probe", calls_file.read_text())
                self.assertFalse(kagglesvc._runtime_env_path(state_gpu).exists())

            # Test `restart` stops old local runner and preserves runtime.env secrets across --new-credentials
            proc_old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            self.addCleanup(lambda: proc_old.poll() is None and proc_old.kill())
            old_ident = kagglesvc.process_identity(proc_old.pid)
            svc_rst = kagglesvc.KaggleService("web-probe", "web-probe.ipynb", 18090, auth="caddy")
            with patch.dict(os.environ, env):
                st_rst = kagglesvc._state_for(svc_rst)
                kagglesvc.write_private_env_file(
                    kagglesvc._runtime_env_path(st_rst),
                    {"CT_SERVICE": "web-probe", "CT_TUNNEL_TOKEN": "keep-tunnel-tok", "CT_AUTH_PASS": "old-p", "CT_API_TOKEN": "sk-oldtok"},
                )
                st_rst.save(
                    provider="kaggle",
                    service="web-probe",
                    base_service="web-probe",
                    kaggle_session="web-probe",
                    account="acct_ok",
                    kernel_session_id=777,
                    auth_user="colabllm",
                    auth_pass="old-p",
                    api_token="sk-oldtok",
                    runner_pid=proc_old.pid,
                    runner_identity=old_ident,
                    ready=True,
                )
                nb_src = root / "web-probe.ipynb"
                nb_src.write_text(json.dumps({"cells": []}))
                script_src = root / "web-probe.py"
                script_src.write_text("")
                calls_file.write_text("", encoding="utf-8")
                with patch.object(kagglesvc, "_launch_runner", return_value=0) as launch_mock:
                    rc_r = kagglesvc.cmd_restart(
                        svc_rst,
                        script_src,
                        types.SimpleNamespace(dry_run=False, new_credentials=True, foreground=False, detach=True, ready_timeout=None),
                    )
                self.assertEqual(rc_r, 0)
                launch_mock.assert_called_once()
                self.assertIsNotNone(proc_old.wait(timeout=3))
                self.assertIn("session restart web-probe --json", calls_file.read_text())
                new_env = kagglesvc._read_single_line_env_file(kagglesvc._runtime_env_path(st_rst))
                self.assertEqual(new_env["CT_TUNNEL_TOKEN"], "keep-tunnel-tok")
                self.assertNotEqual(new_env["CT_AUTH_PASS"], "old-p")

    def test_cmd_up_uses_global_frp_tunnel_config_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "profile"
            home.mkdir(mode=0o700)
            frp_cfg = {
                "enabled": True,
                "server_ip": "203.0.113.10",
                "ssh": "root@frp.example.com",
                "server_port": 7000,
                "project_id": "a" * 32,
            }
            (home / "tunnel-config.json").write_text(
                json.dumps({"base_domain": "example.com", "transport": "frp", "frp": frp_cfg}),
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_kaggle(
                fake_bin,
                """import json, sys
args = sys.argv[1:]
while args and args[0] in ("-a", "--account"):
    args = args[2:]
if args[:2] == ["session", "status"]:
    print(json.dumps({"name": args[2], "account": "acct_ok", "state": "stopped"}))
    sys.exit(0)
if args[:2] == ["session", "create"]:
    print(json.dumps({"name": args[2], "account": "acct_ok", "kernel_session_id": 888, "state": "active", "accelerator": "cpu", "enable_internet": True}))
    sys.exit(0)
print("{}")
""",
            )
            for tool in ("cf", "ssh"):
                p = fake_bin / tool
                p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                p.chmod(0o755)

            kcfg = root / "kaggle-ext"
            kcfg.mkdir()
            (kcfg / "accounts.json").write_text(
                json.dumps({"version": 1, "active": "acct_ok", "accounts": {"acct_ok": {}}}),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "COLABLLM_HOME": str(home),
                "KAGGLE_EXT_CONFIG_DIR": str(kcfg),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            }

            # 1. Without --quick-tunnel, global FRP + base_domain are selected automatically.
            dry_frp = subprocess.run(
                [str(ROOT / "colabllm"), "kaggle", "web-probe", "up", "--dry-run"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(dry_frp.returncode, 0, dry_frp.stderr)
            plan_frp = json.loads(dry_frp.stdout)
            self.assertEqual(plan_frp["transport"], "frp")
            self.assertEqual(plan_frp["hostname"], "kaggle-web-probe.example.com")

            # 2. --quick-tunnel overrides global FRP back to Cloudflare quick tunnel.
            dry_qt = subprocess.run(
                [str(ROOT / "colabllm"), "kaggle", "web-probe", "up", "--dry-run", "--quick-tunnel"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(dry_qt.returncode, 0, dry_qt.stderr)
            plan_qt = json.loads(dry_qt.stdout)
            self.assertEqual(plan_qt["transport"], "cloudflare")
            self.assertIsNone(plan_qt["hostname"])

            # 3. Real cmd_up validates FRP config, invokes frp_ensure_route with KAGGLE_NAMESPACE,
            #    saves frp_route in state.json, and keeps secrets in 0600 runtime.env only.
            svc = kagglesvc.KaggleService("web-probe", "web-probe.ipynb", 18090)
            script = ROOT / "src/colabllm/kaggle_services/web-probe.py"
            args = kagglesvc.build_parser(svc).parse_args(["up", "--detach"])
            orig_root = kagglesvc.STATE_ROOT
            self.addCleanup(setattr, kagglesvc, "STATE_ROOT", orig_root)

            fake_route = {
                "transport": "frp",
                "hostname": "kaggle-web-probe.example.com",
                "remote_service": "kaggle-web-probe",
                "dns_owner": "kagglesvc-frp " + ("a" * 32) + " web-probe",
                "dns_record_id": "rec-1",
                "zone_id": "zone-1",
                "frp": {**frp_cfg, "remote_port": 24567, "proxy_name": "p1"},
            }
            fake_frp_env = {
                "CT_TRANSPORT": "frp",
                "CT_PUBLIC_HOSTNAME": "kaggle-web-probe.example.com",
                "CT_FRP_SERVER_IP": "203.0.113.10",
                "CT_FRP_SERVER_PORT": "7000",
                "CT_FRP_REMOTE_PORT": "24567",
                "CT_FRP_PROXY_NAME": "p1",
                "CT_FRP_TOKEN": "frp-secret-tok-123",
                "CT_FRP_CA_CERT": "-----BEGIN CERTIFICATE-----\nCERT\n-----END CERTIFICATE-----\n",
                "CT_FRP_SERVER_NAME": "203.0.113.10",
                "CT_FRP_AUTH_SCOPES": '["HeartBeats"]',
                "CT_FRP_VERSION": "0.65.0",
            }
            with patch.dict(os.environ, env, clear=True):
                kagglesvc.STATE_ROOT = home
                with patch.object(
                    kagglesvc, "frp_ensure_route", return_value=(fake_route, dict(fake_frp_env))
                ) as route_mock, patch.object(kagglesvc, "_launch_runner", return_value=0):
                    rc = kagglesvc.cmd_up(svc, script, args)
            self.assertEqual(rc, 0)
            route_mock.assert_called_once()
            self.assertEqual(route_mock.call_args.kwargs.get("namespace"), runtime_core.KAGGLE_NAMESPACE)
            self.assertEqual(route_mock.call_args.args[1], frp_cfg)
            self.assertEqual(route_mock.call_args.args[2], "kaggle-web-probe.example.com")

            st_obj = runtime_core.LockedState("web-probe", runtime_core.KAGGLE_NAMESPACE, state_root=home)
            st_data = st_obj.load()
            self.assertEqual(st_data.get("transport"), "frp")
            self.assertEqual(st_data.get("hostname"), "kaggle-web-probe.example.com")
            self.assertEqual(st_data.get("frp_route"), fake_route)
            self.assertNotIn("CT_FRP_TOKEN", st_data.get("env", {}))
            self.assertNotIn("CT_FRP_CA_CERT", st_data.get("env", {}))

            rt_env = kagglesvc._read_single_line_env_file(kagglesvc._runtime_env_path(st_obj))
            self.assertEqual(rt_env["CT_TRANSPORT"], "frp")
            self.assertEqual(rt_env["CT_FRP_TOKEN"], "frp-secret-tok-123")
            self.assertIn("BEGIN CERTIFICATE", rt_env["CT_FRP_CA_CERT"])
            self.assertNotIn("frp-secret-tok-123", st_obj.path.read_text(encoding="utf-8"))

            # 4. Missing ssh CLI is rejected before touching remote session creation when FRP is active.
            (fake_bin / "ssh").unlink()
            svc_missing = kagglesvc.KaggleService("web-probe", "web-probe.ipynb", 18090)
            kagglesvc.apply_instance(svc_missing, "nossh")
            env_nossh = {**env, "PATH": str(fake_bin)}
            with patch.dict(os.environ, env_nossh, clear=True):
                kagglesvc.STATE_ROOT = home
                with self.assertRaises(CliError):
                    kagglesvc.cmd_up(svc_missing, script, args)


_TEST_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_read_frame(sock: socket.socket, *, stop_check=None) -> tuple[bool, int, bytes] | None:
    def _exact(n: int, *, allow_idle_timeout: bool = False) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            if stop_check is not None and stop_check():
                raise EOFError("stopped")
            try:
                chunk = sock.recv(n - len(buf))
            except (TimeoutError, socket.timeout):
                if allow_idle_timeout and not buf:
                    continue
                raise EOFError("timeout")
            if not chunk:
                raise EOFError("closed")
            buf.extend(chunk)
        return bytes(buf)

    try:
        head = _exact(2, allow_idle_timeout=True)
    except EOFError:
        return None
    b0, b1 = head[0], head[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = int.from_bytes(_exact(2), "big")
    elif length == 127:
        length = int.from_bytes(_exact(8), "big")
    mask = _exact(4) if masked else b""
    payload = _exact(length) if length else b""
    if masked and payload:
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return fin, opcode, payload


def _ws_write_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
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


class _MockJupyterMuxServer:
    """Mock HTTP + RFC 6455 Jupyter proxy server supporting both Colab and `/k/<id>/<tok>/proxy` paths."""

    def __init__(self, root: Path, health_url: str, *, require_origin_once: bool = False) -> None:
        self.root = root
        self.health_url = health_url
        self.require_origin_once = require_origin_once
        self._origin_rejected_once = False
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._dropped_sockets: set[socket.socket] = set()
        self.active_sockets: list[socket.socket] = []
        self.executed: list[tuple[bool, bool, str]] = []
        self.watch_connects = 0
        self.pings_received = 0
        self.kernel_counter = 0
        self.kernels: dict[str, str] = {}
        srv_ref = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            @staticmethod
            def _strip_proxy_prefix(path: str) -> str:
                idx = path.find("/api/kernels")
                return path[idx:] if idx >= 0 else path

            def _send_json(self, code: int, payload) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                sub = self._strip_proxy_prefix(parsed.path)
                if sub.startswith("/api/kernels/") and sub.endswith("/channels"):
                    self._handle_ws(parsed, sub)
                    return
                if sub == "/api/kernels":
                    with srv_ref._lock:
                        items = [{"id": kid, "name": "python3", "execution_state": "idle"} for kid in srv_ref.kernels]
                    self._send_json(200, items)
                    return
                if sub.startswith("/api/kernels/"):
                    kid = urllib.parse.unquote(sub[len("/api/kernels/") :])
                    with srv_ref._lock:
                        exists = kid in srv_ref.kernels
                    if exists:
                        self._send_json(200, {"id": kid, "name": "python3", "execution_state": "idle"})
                    else:
                        self._send_json(404, {"error": "not found"})
                    return
                self._send_json(404, {"error": "not found"})

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)
                sub = self._strip_proxy_prefix(parsed.path)
                length = int(self.headers.get("Content-Length") or "0")
                if length:
                    self.rfile.read(length)
                if sub == "/api/kernels":
                    with srv_ref._lock:
                        srv_ref.kernel_counter += 1
                        kid = f"kkern-{srv_ref.kernel_counter}"
                        srv_ref.kernels[kid] = "idle"
                    self._send_json(201, {"id": kid, "name": "python3", "execution_state": "idle"})
                    return
                self._send_json(404, {"error": "not found"})

            def _handle_ws(self, parsed: urllib.parse.SplitResult, sub: str):
                qs = urllib.parse.parse_qs(parsed.query)
                sid = (qs.get("session_id") or [""])[0]
                if "/proxy/" in parsed.path and srv_ref.require_origin_once and not srv_ref._origin_rejected_once:
                    if self.headers.get("Origin") != "https://www.kaggle.com":
                        srv_ref._origin_rejected_once = True
                        self._send_json(403, {"error": "origin required"})
                        return

                ws_key = self.headers.get("Sec-WebSocket-Key") or ""
                accept = base64.b64encode(hashlib.sha1((ws_key + _TEST_WS_GUID).encode("ascii")).digest()).decode("ascii")
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.close_connection = True

                sock = self.connection
                sock.settimeout(0.2)
                stop_check = lambda: srv_ref._stopped.is_set() or sock in srv_ref._dropped_sockets
                with srv_ref._lock:
                    srv_ref.active_sockets.append(sock)
                is_watch_conn = sid.startswith("mon-") or (
                    "/proxy/" in parsed.path and (srv_ref.root / "expect-watch-only").exists()
                )
                if is_watch_conn:
                    with srv_ref._lock:
                        srv_ref.watch_connects += 1

                def _emit_iopub(parent_id: str, msg_type: str, content: dict):
                    msg = {
                        "channel": "iopub",
                        "header": {"msg_id": f"io-{time.monotonic_ns()}", "msg_type": msg_type},
                        "parent_header": {"msg_id": parent_id} if parent_id else {},
                        "metadata": {},
                        "content": content,
                    }
                    _ws_write_frame(sock, 0x1, json.dumps(msg).encode("utf-8"))

                def _emit_shell(parent_id: str, status: str = "ok"):
                    msg = {
                        "channel": "shell",
                        "header": {"msg_id": f"sh-{time.monotonic_ns()}", "msg_type": "execute_reply"},
                        "parent_header": {"msg_id": parent_id},
                        "metadata": {},
                        "content": {"status": status, "execution_count": 1},
                    }
                    _ws_write_frame(sock, 0x1, json.dumps(msg).encode("utf-8"))

                stop_hb = threading.Event()
                hb_started = threading.Event()

                def _hb_loop():
                    for _ in range(3):
                        if stop_hb.wait(0.04):
                            return
                        with contextlib.suppress(OSError):
                            _emit_iopub("", "stream", {"name": "stdout", "text": "[mock-kaggle] heartbeat\n"})

                if is_watch_conn:
                    hb_started.set()
                    threading.Thread(target=_hb_loop, daemon=True).start()

                try:
                    while not stop_check():
                        frame = _ws_read_frame(sock, stop_check=stop_check)
                        if frame is None:
                            break
                        _, opcode, payload = frame
                        if opcode == 0x8:
                            break
                        if opcode == 0x9:
                            with srv_ref._lock:
                                srv_ref.pings_received += 1
                            _ws_write_frame(sock, 0xA, payload)
                            continue
                        if opcode != 0x1:
                            continue
                        req = json.loads(payload.decode("utf-8"))
                        msg_id = (req.get("header") or {}).get("msg_id") or ""
                        content = req.get("content") or {}
                        code = str(content.get("code") or "")
                        silent = bool(content.get("silent"))
                        store_history = bool(content.get("store_history", True))

                        with srv_ref._lock:
                            srv_ref.executed.append((silent, store_history, code))

                        if silent:
                            _emit_iopub(msg_id, "status", {"execution_state": "busy"})
                            _emit_iopub(msg_id, "status", {"execution_state": "idle"})
                            _emit_shell(msg_id, "ok")
                            continue

                        if "os.chdir('/content')" in code:
                            _emit_iopub(msg_id, "status", {"execution_state": "busy"})
                            _emit_iopub(msg_id, "status", {"execution_state": "idle"})
                            _emit_shell(msg_id, "ok")
                            continue

                        if "# @title 1." in code and (srv_ref.root / "fail-cell-1").exists():
                            _emit_iopub(msg_id, "status", {"execution_state": "busy"})
                            _emit_iopub(
                                msg_id,
                                "error",
                                {"ename": "RuntimeError", "evalue": "cell1-boom", "traceback": ["RuntimeError: cell1-boom\n"]},
                            )
                            _emit_iopub(msg_id, "status", {"execution_state": "idle"})
                            _emit_shell(msg_id, "error")
                            continue

                        if "# @title 5." in code:
                            _emit_iopub(msg_id, "status", {"execution_state": "busy"})
                            _emit_iopub(msg_id, "stream", {"name": "stdout", "text": f"CT_PUBLIC_URL={srv_ref.health_url}\n"})
                            if (srv_ref.root / "leak-diagnostics").exists():
                                _emit_iopub(
                                    msg_id,
                                    "stream",
                                    {
                                        "name": "stdout",
                                        "text": (
                                            "CT_AUTH_PASS=should-redact-pass\n"
                                            "Authorization: Bearer sk-shouldredact12345\n"
                                            "proxy=https://mock.local/k/777/supersecretproxy999/proxy/api/kernels\n"
                                        ),
                                    },
                                )
                            _emit_iopub(msg_id, "stream", {"name": "stdout", "text": "CT_READY\n"})
                            if not hb_started.is_set():
                                hb_started.set()
                                threading.Thread(target=_hb_loop, daemon=True).start()
                            continue

                        _emit_iopub(msg_id, "status", {"execution_state": "busy"})
                        _emit_iopub(msg_id, "stream", {"name": "stdout", "text": "ok\n"})
                        _emit_iopub(msg_id, "status", {"execution_state": "idle"})
                        _emit_shell(msg_id, "ok")
                except (OSError, EOFError, ValueError):
                    pass
                finally:
                    stop_hb.set()
                    with srv_ref._lock:
                        if sock in srv_ref.active_sockets:
                            srv_ref.active_sockets.remove(sock)
                    with contextlib.suppress(OSError):
                        sock.close()

        class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                return

        self.server = _QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def drop_all_ws(self) -> None:
        with self._lock:
            socks = list(self.active_sockets)
            self._dropped_sockets.update(socks)
        for sock in socks:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def close(self) -> None:
        self._stopped.set()
        self.drop_all_ws()
        self.server.shutdown()
        self.server.server_close()


class KaggleWsMuxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home_dir = self.root / "home"
        self.profile_dir = self.root / "profile"
        self.colab_cfg = self.root / "colab_cfg"
        self.kaggle_cfg = self.root / "kaggle_cfg"
        self.fake_bin = self.root / "bin"
        for d in (self.home_dir, self.profile_dir, self.colab_cfg, self.kaggle_cfg, self.fake_bin):
            d.mkdir(parents=True, exist_ok=True)
        (self.kaggle_cfg / "accounts.json").write_text(
            json.dumps({"version": 1, "active": "acct_ok", "accounts": {"acct_ok": {"username": "alice"}}}),
            encoding="utf-8",
        )

        root_dir = self.root

        class _Health(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                code = 502 if (root_dir / "http-502").exists() else 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}' if code == 200 else b"bad gateway")

        self.health = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Health)
        self.health.daemon_threads = True
        threading.Thread(target=self.health.serve_forever, daemon=True).start()
        self.addCleanup(self.health.server_close)
        self.addCleanup(self.health.shutdown)
        self.health_url = f"http://127.0.0.1:{self.health.server_port}"

        self.mock_ws = _MockJupyterMuxServer(self.root, self.health_url, require_origin_once=True)
        self.addCleanup(self.mock_ws.close)

        # Fake `colab` CLI (populates sessions.json and forbids `exec`)
        fake_colab = self.fake_bin / "colab"
        fake_colab.write_text(
            f'''#!{sys.executable}
import json, os, sys
from pathlib import Path

root = Path(os.environ["FAKE_KAGGLE_ROOT"])
cfg_dir = Path(os.environ["COLAB_CLI_CONFIG_DIR"])
sessions_path = cfg_dir / "sessions.json"
calls_path = root / "colab-calls.jsonl"
with calls_path.open("a", encoding="utf-8") as logf:
    logf.write(json.dumps(sys.argv[1:]) + "\\n")

def load_s():
    return json.loads(sessions_path.read_text(encoding="utf-8")) if sessions_path.is_file() else {{}}

def save_s(d):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    sessions_path.write_text(json.dumps(d, indent=2), encoding="utf-8")

cmd = sys.argv[1] if len(sys.argv) > 1 else ""
sess = sys.argv[sys.argv.index("-s") + 1] if "-s" in sys.argv else None
if cmd == "new" and sess:
    d = load_s()
    d[sess] = {{
        "name": sess,
        "token": "tok-colab-1",
        "url": os.environ["MOCK_COLAB_WS_URL"],
        "endpoint": f"ep-{{sess}}",
        "token_expires_at": "2099-01-01T00:00:00Z",
    }}
    save_s(d)
    sys.exit(0)
if cmd == "status" and sess:
    print(f"[{{sess}}] ep-{{sess}} | Hardware: CPU | Status: BUSY", flush=True)
    sys.exit(0)
if cmd == "stop" and sess:
    d = load_s()
    d.pop(sess, None)
    save_s(d)
    sys.exit(0)
if cmd == "exec":
    print("UNEXPECTED_COLAB_EXEC", file=sys.stderr)
    sys.exit(97)
sys.exit(0)
''',
            encoding="utf-8",
        )
        fake_colab.chmod(0o755)

        # Fake `kaggle` CLI (populates accounts/acct_ok/sessions.json with `/k/777/supersecretproxy999/proxy` and forbids `session exec`)
        fake_kaggle = self.fake_bin / "kaggle"
        fake_kaggle.write_text(
            f'''#!{sys.executable}
import json, os, sys
from pathlib import Path

root = Path(os.environ["FAKE_KAGGLE_ROOT"])
kcfg = Path(os.environ["KAGGLE_EXT_CONFIG_DIR"])
sessions_path = kcfg / "accounts" / "acct_ok" / "sessions.json"
calls_path = root / "kaggle-calls.jsonl"
args = sys.argv[1:]
if "-a" in args:
    i = args.index("-a")
    args = args[:i] + args[i+2:]
with calls_path.open("a", encoding="utf-8") as logf:
    logf.write(json.dumps(args) + "\\n")

def load_doc():
    if sessions_path.is_file():
        return json.loads(sessions_path.read_text(encoding="utf-8"))
    return {{"version": 1, "sessions": {{}}}}

def save_doc(doc):
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    sessions_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

if args[:2] == ["session", "create"]:
    sname = args[2]
    doc = load_doc()
    ksid = 888 if (root / "next-recreate").exists() else 777
    base_url = os.environ["MOCK_KAGGLE_WS_URL"] + f"/k/{{ksid}}/supersecretproxy999/proxy"
    doc["sessions"][sname] = {{
        "name": sname,
        "account": "acct_ok",
        "kernel_session_id": ksid,
        "state": "active",
        "accelerator": "cpu",
        "enable_internet": True,
        "jupyter_url": base_url,
        "client_session_id": f"cid-{{sname}}",
    }}
    save_doc(doc)
    print(json.dumps({{
        "name": sname,
        "account": "acct_ok",
        "kernel_session_id": ksid,
        "state": "active",
        "accelerator": "cpu",
        "enable_internet": True,
    }}))
    sys.exit(0)

if args[:2] == ["session", "status"]:
    sname = args[2]
    live_file = root / "kaggle-live-state.txt"
    live = live_file.read_text(encoding="utf-8").strip() if live_file.is_file() else "running"
    if live == "queue":
        qfile = root / "kaggle-live-queue.json"
        q = json.loads(qfile.read_text(encoding="utf-8")) if qfile.is_file() else ["running"]
        live = q.pop(0) if q else "running"
        qfile.write_text(json.dumps(q), encoding="utf-8")
    print(json.dumps({{"name": sname, "account": "acct_ok", "alive": live == "running", "live_state": live, "accelerator": "cpu", "enable_internet": True}}))
    sys.exit(0 if live == "running" else 1)

if args[:2] == ["session", "restart"]:
    print(json.dumps({{"restarted": True}}))
    sys.exit(0)

if args[:2] == ["session", "stop"]:
    sname = args[2]
    doc = load_doc()
    doc["sessions"].pop(sname, None)
    save_doc(doc)
    print("[]")
    sys.exit(0)

if args[:2] == ["session", "exec"]:
    print("UNEXPECTED_KAGGLE_SESSION_EXEC", file=sys.stderr)
    sys.exit(98)

sys.exit(0)
''',
            encoding="utf-8",
        )
        fake_kaggle.chmod(0o755)

        self.env = {
            **os.environ,
            "HOME": str(self.home_dir),
            "COLABLLM_HOME": str(self.profile_dir),
            "COLAB_CLI_CONFIG_DIR": str(self.colab_cfg),
            "KAGGLE_EXT_CONFIG_DIR": str(self.kaggle_cfg),
            "FAKE_KAGGLE_ROOT": str(self.root),
            "MOCK_COLAB_WS_URL": self.mock_ws.url,
            "MOCK_KAGGLE_WS_URL": self.mock_ws.url,
            "PATH": str(self.fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        }
        self.env.pop("CT_INSTANCE", None)
        self.env.pop("COLABLLM_EXEC_BACKEND", None)
        self.env.pop("COLABLLM_NO_MUX", None)
        self.addCleanup(self._cleanup_mux)

    def _cleanup_mux(self):
        for rel in ("mux/state.json", "kaggle/mux/state.json"):
            mux_state = self.profile_dir / rel
            if mux_state.is_file():
                with contextlib.suppress(Exception):
                    st = json.loads(mux_state.read_text(encoding="utf-8"))
                    mux._rpc_sync(st["port"], st["token"], "shutdown", timeout=2.0)
                    stop_owned_process(st.get("pid"), st.get("identity"), timeout=2.0)

    def _cli(self, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(ROOT / "colabllm"), *args],
            env=self.env,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def _kaggle_calls(self) -> list[list[str]]:
        p = self.root / "kaggle-calls.jsonl"
        if not p.is_file():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_cross_provider_single_mux_sharing_restart_and_idle_exit(self):
        up_c1 = self._cli("ollama", "-i", "c1", "up", "--quick-tunnel", "--no-drive", "--detach")
        self.assertEqual(up_c1.returncode, 0, up_c1.stderr)
        up_k1 = self._cli("kaggle", "web-probe", "-i", "k1", "up", "--quick-tunnel", "--detach")
        self.assertEqual(up_k1.returncode, 0, up_k1.stderr)
        up_k2 = self._cli("kaggle", "web-probe", "-i", "k2", "up", "--quick-tunnel", "--detach")
        self.assertEqual(up_k2.returncode, 0, up_k2.stderr)

        c1_state = self.profile_dir / "services" / "ollama-c1" / "state.json"
        k1_state = self.profile_dir / "kaggle" / "services" / "web-probe-k1" / "state.json"
        k2_state = self.profile_dir / "kaggle" / "services" / "web-probe-k2" / "state.json"

        deadline = time.monotonic() + 20.0
        loaded: dict[str, dict] = {}
        while time.monotonic() < deadline:
            loaded = {
                name: json.loads(path.read_text(encoding="utf-8"))
                for name, path in (("c1", c1_state), ("k1", k1_state), ("k2", k2_state))
                if path.is_file()
            }
            if len(loaded) == 3 and all(st.get("ready") and st.get("url") == self.health_url for st in loaded.values()):
                break
            time.sleep(0.05)

        self.assertEqual(len(loaded), 3, loaded)
        for name, st in loaded.items():
            self.assertTrue(st.get("ready"), f"{name}: {st}")
            self.assertEqual(st.get("runner_mode"), "mux", f"{name}: {st}")
            self.assertEqual(st.get("exec_backend"), "native", f"{name}: {st}")
            self.assertIsNone(st.get("exec_pid"), f"{name}: {st}")

        pids = {st["runner_pid"] for st in loaded.values()}
        idents = {st["runner_identity"] for st in loaded.values()}
        self.assertEqual(len(pids), 1, loaded)
        self.assertEqual(len(idents), 1, loaded)
        mux_pid = next(iter(pids))
        mux_ident = next(iter(idents))
        self.assertTrue(owned_process(mux_pid, mux_ident))

        # Verify zero CLI exec processes spawned and MuxDaemon keys prevent collision.
        self.assertEqual([c for c in self._kaggle_calls() if c[:2] == ["session", "exec"]], [])
        mux_info = json.loads((self.profile_dir / "mux" / "state.json").read_text(encoding="utf-8"))
        pong = mux._rpc_sync(mux_info["port"], mux_info["token"], "ping", timeout=2.0)
        self.assertEqual(sorted(pong["sessions"]), ["kaggle/web-probe-k1", "kaggle/web-probe-k2", "ollama-c1"])

        # Restart k1 inside the same mux daemon without touching c1 or k2.
        rst_k1 = self._cli("kaggle", "web-probe", "-i", "k1", "restart", "--detach")
        self.assertEqual(rst_k1.returncode, 0, rst_k1.stderr)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            st_k1 = json.loads(k1_state.read_text(encoding="utf-8"))
            if st_k1.get("ready"):
                break
            time.sleep(0.05)
        self.assertEqual(json.loads(k1_state.read_text(encoding="utf-8")).get("runner_pid"), mux_pid)
        self.assertTrue(owned_process(mux_pid, mux_ident))
        self.assertTrue(json.loads(c1_state.read_text(encoding="utf-8")).get("ready"))
        self.assertTrue(json.loads(k2_state.read_text(encoding="utf-8")).get("ready"))

        # Stop k1 -> k2 and c1 still alive on the exact same mux_pid.
        self.assertEqual(self._cli("kaggle", "web-probe", "-i", "k1", "down").returncode, 0)
        self.assertTrue(owned_process(mux_pid, mux_ident))
        self.assertEqual(json.loads(self._cli("kaggle", "web-probe", "-i", "k2", "status", "--json").stdout)["ready"], True)

        # Stop k2 and c1 -> mux daemon idle-exits automatically.
        self.assertEqual(self._cli("kaggle", "web-probe", "-i", "k2", "down").returncode, 0)
        self.assertEqual(self._cli("ollama", "-i", "c1", "down").returncode, 0)
        exit_deadline = time.monotonic() + 10.0
        while owned_process(mux_pid, mux_ident) and time.monotonic() < exit_deadline:
            time.sleep(0.05)
        self.assertFalse(owned_process(mux_pid, mux_ident))

    def test_silent_env_injection_and_secret_proxy_redaction(self):
        (self.root / "leak-diagnostics").touch()
        up = self._cli("kaggle", "web-probe", "-i", "sec", "up", "--quick-tunnel")
        self.assertEqual(up.returncode, 0, up.stderr)

        st_path = self.profile_dir / "kaggle" / "services" / "web-probe-sec" / "state.json"
        st = json.loads(st_path.read_text(encoding="utf-8"))
        self.assertEqual(st.get("runner_mode"), "mux")
        self.assertTrue(st.get("ready"))

        # Silent env prelude executed before visible notebook cells.
        with self.mock_ws._lock:
            exec_list = list(self.mock_ws.executed)
        silent_preludes = [c for (silent, store_hist, c) in exec_list if silent and not store_hist]
        self.assertTrue(silent_preludes, f"Expected silent env prelude, got: {exec_list}")
        self.assertIn("CT_AUTH_PASS", silent_preludes[0])
        self.assertIn(st["auth_pass"], silent_preludes[0])
        self.assertIn(st["api_token"], silent_preludes[0])

        # Visible run.log & mux.log must mask secrets and Kaggle /k/<id>/<tok> proxy paths.
        run_log = (self.profile_dir / "kaggle" / "services" / "web-probe-sec" / "run.log").read_text(encoding="utf-8")
        mux_log = (self.profile_dir / "mux" / "mux.log").read_text(encoding="utf-8") if (self.profile_dir / "mux" / "mux.log").is_file() else ""
        for secret in (st["auth_pass"], st["api_token"], "should-redact-pass", "sk-shouldredact12345", "supersecretproxy999"):
            self.assertNotIn(secret, run_log)
            self.assertNotIn(secret, mux_log)
        self.assertIn("/k/777/[REDACTED]/proxy", run_log)
        self.assertIn("CT_READY", run_log)
        self.assertEqual(self._cli("kaggle", "web-probe", "-i", "sec", "down").returncode, 0)

    def test_auto_restart_four_branches_max_uptime_and_pre_ready_stop_in_mux(self):
        self.env["COLABLLM_FAST_PROBE_SECONDS"] = "0.08"
        self.env["COLABLLM_IDLE_CHECK_SECONDS"] = "300"

        up = self._cli(
            "kaggle", "web-probe", "-i", "ar", "up",
            "--quick-tunnel", "--auto-restart", "--restart-delay", "0.05", "--max-restarts", "4",
        )
        self.assertEqual(up.returncode, 0, up.stderr)
        sp = self.profile_dir / "kaggle" / "services" / "web-probe-ar" / "state.json"

        # Branch 1: drop WS while container live_state=running & /health=200 -> read-only WS iopub watch, 0 restarts added.
        (self.root / "expect-watch-only").touch()
        with self.mock_ws._lock:
            exec_count_before = len(self.mock_ws.executed)
        self.mock_ws.drop_all_ws()
        deadline = time.monotonic() + 20.0
        st_b1 = {}
        while time.monotonic() < deadline:
            st_b1 = json.loads(sp.read_text(encoding="utf-8"))
            if str(st_b1.get("exec_identity") or "").startswith("kaggle-ws-watch:") and st_b1.get("ready"):
                break
            time.sleep(0.04)
        self.assertTrue(str(st_b1.get("exec_identity") or "").startswith("kaggle-ws-watch:"), st_b1)
        self.assertEqual(st_b1.get("restarts"), 0)
        with self.mock_ws._lock:
            self.assertGreaterEqual(self.mock_ws.watch_connects, 1)
            self.assertEqual(len(self.mock_ws.executed), exec_count_before)
        (self.root / "expect-watch-only").unlink(missing_ok=True)

        # Branch 2: HTTP 502 while running -> kernel restart (restarts=1) + recover when HTTP 200 returns.
        rst_before = len([c for c in self._kaggle_calls() if c[:2] == ["session", "restart"]])
        (self.root / "http-502").touch()
        dl = time.monotonic() + 20.0
        while time.monotonic() < dl:
            if len([c for c in self._kaggle_calls() if c[:2] == ["session", "restart"]]) >= rst_before + 1:
                (self.root / "http-502").unlink(missing_ok=True)
                break
            time.sleep(0.03)
        deadline = time.monotonic() + 20.0
        st_b2 = {}
        while time.monotonic() < deadline:
            st_b2 = json.loads(sp.read_text(encoding="utf-8"))
            if int(st_b2.get("restarts") or 0) >= 1 and st_b2.get("ready") and str(st_b2.get("exec_identity") or "").startswith("kaggle-ws:"):
                break
            time.sleep(0.04)
        self.assertGreaterEqual(int(st_b2.get("restarts") or 0), 1, st_b2)
        self.assertTrue(st_b2.get("ready"), st_b2)

        # Branch 3 (unreachable backoff without recreate) -> Branch 4 (reclaimed recreate to ksid=888).
        (self.root / "kaggle-live-queue.json").write_text(json.dumps(["unreachable", "reclaimed", "running"]), encoding="utf-8")
        (self.root / "kaggle-live-state.txt").write_text("queue", encoding="utf-8")
        (self.root / "next-recreate").touch()
        create_before = len([c for c in self._kaggle_calls() if c[:2] == ["session", "create"]])
        (self.root / "http-502").touch()
        self.mock_ws.drop_all_ws()
        dl = time.monotonic() + 20.0
        while time.monotonic() < dl:
            if len([c for c in self._kaggle_calls() if c[:2] == ["session", "create"]]) >= create_before + 1:
                (self.root / "http-502").unlink(missing_ok=True)
                (self.root / "kaggle-live-state.txt").unlink(missing_ok=True)
                break
            time.sleep(0.03)
        deadline = time.monotonic() + 20.0
        st_b4 = {}
        while time.monotonic() < deadline:
            st_b4 = json.loads(sp.read_text(encoding="utf-8"))
            if st_b4.get("kernel_session_id") == 888 and st_b4.get("ready"):
                break
            time.sleep(0.04)
        self.assertEqual(st_b4.get("kernel_session_id"), 888, st_b4)
        self.assertGreaterEqual(int(st_b4.get("session_recreate_count") or 0), 1, st_b4)
        self.assertTrue(st_b4.get("ready"), st_b4)
        self.assertEqual(self._cli("kaggle", "web-probe", "-i", "ar", "down").returncode, 0)

        # Pre-CT_READY failure inside MuxDaemon calls `kaggle session stop` so no orphan VM remains.
        calls_before_fail = len(self._kaggle_calls())
        (self.root / "fail-cell-1").touch()
        fail_res = self._cli("kaggle", "web-probe", "-i", "prefail", "up", "--quick-tunnel")
        self.assertNotEqual(fail_res.returncode, 0)
        (self.root / "fail-cell-1").unlink(missing_ok=True)
        calls_after_fail = self._kaggle_calls()[calls_before_fail:]
        self.assertTrue(any(c[-3:-1] == ["stop", "web-probe-prefail"] or "stop" in c for c in calls_after_fail), calls_after_fail)

        # Host --max-uptime inside supervise_instance_async (native mux path) stops remote session & clears ephemeral keys.
        svc_up = kagglesvc.KaggleService("gpu-mux", "web-probe.ipynb", 18090, auth="none")
        orig_root = kagglesvc.STATE_ROOT
        self.addCleanup(setattr, kagglesvc, "STATE_ROOT", orig_root)
        with patch.dict(os.environ, self.env):
            kagglesvc.STATE_ROOT = self.profile_dir
            st_up = kagglesvc._state_for(svc_up)
            nb_up = self.root / "gpu-mux.ipynb"
            nb_up.write_text(json.dumps({"cells": []}), encoding="utf-8")
            kagglesvc.write_private_env_file(kagglesvc._runtime_env_path(st_up), {"CT_SERVICE": "gpu-mux"})
            st_up.save(
                provider="kaggle",
                service="gpu-mux",
                base_service="gpu-mux",
                kaggle_session="gpu-mux",
                account="acct_ok",
                kernel_session_id=999,
                accelerator="NvidiaTeslaT4",
                max_uptime=1,
                started_mono=time.monotonic() - 10.0,
                ever_ready=True,
                exec_backend="native",
                runner_mode="mux",
            )
            calls_before_mu = len(self._kaggle_calls())
            rc_mu = kagglesvc.asyncio.run(kagglesvc.supervise_instance_async(svc_up, nb_up))
            self.assertEqual(rc_mu, 0)
            mu_calls = self._kaggle_calls()[calls_before_mu:]
            self.assertTrue(any("stop" in c and "gpu-mux" in c for c in mu_calls), mu_calls)
            self.assertFalse(kagglesvc._runtime_env_path(st_up).exists())

    def test_legacy_busy_mux_overflow_and_launch_error_anti_orphan(self):
        # 1. Simulate an older v1 MuxDaemon busy with `r2-migrate` (`ping` omits `"providers"`).
        legacy_script = self.root / "legacy_v1_mux.py"
        legacy_state = self.profile_dir / "mux" / "state.json"
        legacy_state.parent.mkdir(parents=True, exist_ok=True)
        legacy_script.write_text(
            f'''import json, os, socket, sys
from pathlib import Path
sys.path.insert(0, {json.dumps(str(ROOT / "src/colabllm/lib"))})
from processes import process_identity

pid = os.getpid()
ident = process_identity(pid)
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.bind(("127.0.0.1", 0))
srv.listen(8)
port = srv.getsockname()[1]
Path({json.dumps(str(legacy_state))}).write_text(
    json.dumps({{"pid": pid, "identity": ident, "port": port, "token": "legacy-tok"}}),
    encoding="utf-8",
)
while True:
    conn, _ = srv.accept()
    line = conn.recv(4096)
    req = json.loads(line.decode("utf-8"))
    if req.get("action") == "ping":
        resp = {{"ok": True, "pid": pid, "sessions": ["r2-migrate"]}}
    elif req.get("action") == "shutdown":
        resp = {{"ok": True}}
        conn.sendall((json.dumps(resp) + "\\n").encode("utf-8"))
        conn.close()
        break
    else:
        resp = {{"ok": False, "error": "Service.__init__() got an unexpected keyword argument 'accelerator'"}}
    conn.sendall((json.dumps(resp) + "\\n").encode("utf-8"))
    conn.close()
''',
            encoding="utf-8",
        )
        legacy_proc = subprocess.Popen(
            [sys.executable, str(legacy_script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        def _stop_legacy():
            if legacy_proc.poll() is None:
                legacy_proc.kill()
            legacy_proc.wait(timeout=3.0)

        self.addCleanup(_stop_legacy)

        dl = time.monotonic() + 10.0
        legacy_info = {}
        while time.monotonic() < dl:
            if legacy_state.is_file():
                legacy_info = json.loads(legacy_state.read_text(encoding="utf-8"))
                if legacy_info.get("pid") == legacy_proc.pid:
                    break
            time.sleep(0.03)
        self.assertEqual(legacy_info.get("pid"), legacy_proc.pid)

        up_leg = self._cli("kaggle", "web-probe", "-i", "leg", "up", "--quick-tunnel", "--detach")
        self.assertEqual(up_leg.returncode, 0, up_leg.stderr)

        leg_sp = self.profile_dir / "kaggle" / "services" / "web-probe-leg" / "state.json"
        overflow_sp = self.profile_dir / "kaggle" / "mux" / "state.json"
        deadline = time.monotonic() + 20.0
        st_leg = {}
        while time.monotonic() < deadline:
            if leg_sp.is_file() and overflow_sp.is_file():
                st_leg = json.loads(leg_sp.read_text(encoding="utf-8"))
                if st_leg.get("ready"):
                    break
            time.sleep(0.05)

        self.assertTrue(st_leg.get("ready"), st_leg)
        self.assertEqual(st_leg.get("runner_mode"), "mux")
        self.assertEqual(st_leg.get("exec_backend"), "native")
        overflow_info = json.loads(overflow_sp.read_text(encoding="utf-8"))
        self.assertEqual(st_leg.get("runner_pid"), overflow_info["pid"])
        self.assertNotEqual(overflow_info["pid"], legacy_proc.pid)
        self.assertIsNone(legacy_proc.poll(), "Legacy busy MuxDaemon must remain untouched")

        self.assertEqual(self._cli("kaggle", "web-probe", "-i", "leg", "down").returncode, 0)
        exit_dl = time.monotonic() + 10.0
        while owned_process(overflow_info["pid"], overflow_info["identity"]) and time.monotonic() < exit_dl:
            time.sleep(0.05)
        self.assertFalse(owned_process(overflow_info["pid"], overflow_info["identity"]))
        self.assertIsNone(legacy_proc.poll(), "Legacy busy MuxDaemon must still be running after Kaggle down")

        # 2. If `mux.start_in_mux` fails inside `cmd_up`, pre-CT_READY cleanup stops remote session cleanly.
        svc_err = kagglesvc.KaggleService("web-probe", "web-probe.ipynb", 18090)
        kagglesvc.apply_instance(svc_err, "boom")
        nb_src = ROOT / "src/colabllm/kaggle_services/web-probe.py"
        orig_root = kagglesvc.STATE_ROOT
        self.addCleanup(setattr, kagglesvc, "STATE_ROOT", orig_root)
        with patch.dict(os.environ, self.env):
            kagglesvc.STATE_ROOT = self.profile_dir
            calls_before = len(self._kaggle_calls())
            args = types.SimpleNamespace(
                internet=True,
                accelerator=None,
                max_restarts=0,
                max_uptime=None,
                dry_run=False,
                quick_tunnel=True,
                domain=None,
                base_domain=None,
                transport=None,
                new_credentials=False,
                foreground=False,
                detach=True,
                auto_restart=False,
                restart_delay=None,
                idle_check=None,
                ready_timeout=None,
                startup_timeout=30.0,
                exec_backend="native",
                no_mux=False,
            )
            with patch.object(kagglesvc.mux, "start_in_mux", side_effect=CliError("simulated mux error")):
                with self.assertRaises(CliError):
                    kagglesvc.cmd_up(svc_err, nb_src, args)
            after_calls = self._kaggle_calls()[calls_before:]
            self.assertTrue(any(c[:3] == ["session", "stop", "web-probe-boom"] for c in after_calls), after_calls)
            st_boom = kagglesvc._state_for(svc_err).load()
            self.assertNotIn("kernel_session_id", st_boom)


if __name__ == "__main__":
    unittest.main()
