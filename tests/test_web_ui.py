"""Tests for the zero-dependency colabllm web console CLI, schema introspection, security, and HTTP/SSE API."""
from __future__ import annotations

import http.client
import http.cookies
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
LIB = SRC / 'colabllm' / 'lib'
for _p in (str(SRC), str(LIB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cli_support import CliError, UsageError  # noqa: E402
from colabllm import web as colabllm_web  # noqa: E402
from colabllm.web_ui import api_catalog, introspect, runner_bridge, server as web_server, state_store  # noqa: E402


def _read_sse_until(resp: http.client.HTTPResponse, predicate, *, timeout: float = 10.0) -> list[tuple[str, str]]:
    """Read SSE frames until predicate(events) is true."""
    events: list[tuple[str, str]] = []
    event_name = 'message'
    data_lines: list[str] = []
    resp.fp.raw._sock.settimeout(timeout)  # type: ignore[attr-defined]
    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
        if line.startswith(':'):
            continue
        if line == '':
            if data_lines:
                events.append((event_name, '\n'.join(data_lines)))
                event_name = 'message'
                data_lines = []
                if predicate(events):
                    break
            continue
        if line.startswith('event:'):
            event_name = line[6:].strip()
        elif line.startswith('data:'):
            data_lines.append(line[5:].lstrip())
    return events


class WebCliBindingTest(unittest.TestCase):
    def test_rejects_remote_bind_without_allow_remote(self):
        with self.assertRaises(UsageError):
            colabllm_web.create_bound_server('0.0.0.0', 0, allow_remote=False)

    def test_port_fallback_and_strict_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.listen(1)
        busy_port = sock.getsockname()[1]
        srv = None
        try:
            with self.assertRaises(CliError):
                colabllm_web.create_bound_server('127.0.0.1', busy_port, strict_port=True)
            srv = colabllm_web.create_bound_server('127.0.0.1', busy_port, strict_port=False)
            self.assertNotEqual(srv.server_address[1], busy_port)
            self.assertGreater(srv.server_address[1], busy_port)
        finally:
            if srv is not None:
                srv.server_close()
            sock.close()

    def test_auto_open_guards(self):
        with patch.dict(os.environ, {'DISPLAY': ':0'}, clear=True):
            self.assertTrue(colabllm_web._should_auto_open(False))
            self.assertFalse(colabllm_web._should_auto_open(True))
        with patch.dict(os.environ, {'DISPLAY': ':0', 'SSH_CONNECTION': '127.0.0.1 1 127.0.0.1 22'}, clear=True):
            self.assertFalse(colabllm_web._should_auto_open(False))
        with patch.dict(os.environ, {'DISPLAY': ':0', 'BROWSER': 'true'}, clear=True):
            self.assertFalse(colabllm_web._should_auto_open(False))

    def test_detach_status_and_stop_lifecycle(self):
        cli = str(ROOT / 'colabllm')
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'colabllm_home'
            env = {**os.environ, 'COLABLLM_HOME': str(home)}

            bad = subprocess.run([cli, 'web', '--json'], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(bad.returncode, 2)

            initial = subprocess.run([cli, 'web', '--status', '--json'], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(initial.returncode, 0, initial.stderr)
            self.assertEqual(json.loads(initial.stdout), {'running': False})

            started = subprocess.run(
                [cli, 'web', '--detach', '--port', '0', '--no-open', '--read-only', '--json'],
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            payload = json.loads(started.stdout)
            pid = int(payload['pid'])
            try:
                self.assertTrue(payload['running'])
                self.assertTrue(payload['read_only'])
                auth_url = payload['auth_url']
                parsed = urllib.parse.urlsplit(auth_url)
                token = urllib.parse.parse_qs(parsed.query)['token'][0]

                state_file = home / 'web' / 'state.json'
                log_file = home / 'web' / 'web.log'
                self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(log_file.stat().st_mode), 0o600)
                self.assertNotIn(token, log_file.read_text(encoding='utf-8'))

                # Verify the detached server serves HTTP requests and enforces read-only.
                conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
                try:
                    conn.request('GET', f'{parsed.path}?{parsed.query}')
                    r = conn.getresponse()
                    r.read()
                    self.assertEqual(r.status, 303)
                    jar = http.cookies.SimpleCookie()
                    jar.load(r.getheader('Set-Cookie') or '')
                    cookie_hdr = f"colabllm_sid={jar['colabllm_sid'].value}"

                    conn.request('GET', '/api/bootstrap', headers={'Cookie': cookie_hdr})
                    boot_resp = conn.getresponse()
                    boot = json.loads(boot_resp.read().decode('utf-8'))
                    self.assertEqual(boot_resp.status, 200)
                    self.assertTrue(boot['read_only'])
                finally:
                    conn.close()

                # Second --detach and foreground start both refuse to start a second instance.
                dup = subprocess.run(
                    [cli, 'web', '--detach', '--port', '0', '--no-open'],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertNotEqual(dup.returncode, 0)
                self.assertIn('--stop', dup.stderr)

                fg_dup = subprocess.run(
                    [cli, 'web', '--port', '0', '--no-open'],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertNotEqual(fg_dup.returncode, 0)
                self.assertIn('--stop', fg_dup.stderr)
                self.assertIn(str(pid), fg_dup.stderr)

                # --status reports live process & matching auth_url.
                status = subprocess.run([cli, 'web', '--status', '--json'], env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(status.returncode, 0, status.stderr)
                status_payload = json.loads(status.stdout)
                self.assertTrue(status_payload['running'])
                self.assertEqual(status_payload['pid'], pid)
                self.assertEqual(status_payload['mode'], 'background')
                self.assertEqual(status_payload['auth_url'], auth_url)

                # --stop cleanly stops the daemon and removes state.json.
                stopped = subprocess.run([cli, 'web', '--stop', '--json'], env=env, text=True, capture_output=True, timeout=15)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertEqual(json.loads(stopped.stdout), {'stopped': True, 'running': False, 'pid': pid})
                self.assertFalse(state_file.exists())

                after = subprocess.run([cli, 'web', '--status', '--json'], env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(after.returncode, 0, after.stderr)
                self.assertEqual(json.loads(after.stdout), {'running': False})

                # Foreground mode also holds the single-instance lock against both -d and foreground.
                fg_proc = subprocess.Popen(
                    [cli, 'web', '--port', '0', '--no-open'],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    import time as _time

                    deadline = _time.monotonic() + 5.0
                    fg_status = {}
                    while _time.monotonic() < deadline:
                        res = subprocess.run([cli, 'web', '--status', '--json'], env=env, text=True, capture_output=True, timeout=5)
                        fg_status = json.loads(res.stdout)
                        if fg_status.get('running'):
                            break
                        self.assertIsNone(fg_proc.poll())
                        _time.sleep(0.05)
                    self.assertTrue(fg_status.get('running'))
                    self.assertEqual(fg_status.get('mode'), 'foreground')

                    bg_while_fg = subprocess.run(
                        [cli, 'web', '--detach', '--port', '0', '--no-open'],
                        env=env,
                        text=True,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertNotEqual(bg_while_fg.returncode, 0)
                    self.assertIn('--stop', bg_while_fg.stderr)

                    fg_while_fg = subprocess.run(
                        [cli, 'web', '--port', '0', '--no-open'],
                        env=env,
                        text=True,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertNotEqual(fg_while_fg.returncode, 0)
                    self.assertIn('--stop', fg_while_fg.stderr)

                    stop_fg = subprocess.run([cli, 'web', '--stop', '--json'], env=env, text=True, capture_output=True, timeout=15)
                    self.assertEqual(stop_fg.returncode, 0, stop_fg.stderr)
                    self.assertTrue(json.loads(stop_fg.stdout)['stopped'])
                    self.assertEqual(fg_proc.wait(timeout=5), 0)
                    self.assertFalse(state_file.exists())
                finally:
                    if fg_proc.poll() is None:
                        fg_proc.terminate()
                        fg_proc.wait(timeout=5)
                    if fg_proc.stdout:
                        fg_proc.stdout.close()
                    if fg_proc.stderr:
                        fg_proc.stderr.close()
            finally:
                subprocess.run([cli, 'web', '--stop'], env=env, text=True, capture_output=True, timeout=10)


class IntrospectionTest(unittest.TestCase):
    def test_catalog_and_schemas_cover_all_services(self):
        catalog = introspect.get_service_catalog()
        self.assertGreaterEqual(len(catalog), 18)
        for name in catalog:
            schema = introspect.get_service_schema(name)
            self.assertEqual(schema['service']['name'], name)
            self.assertIsInstance(schema['subcommands'], list)
        self.assertTrue(introspect.get_service_schema('qwen-flash')['subcommands'])
        self.assertTrue(introspect.get_service_schema('flux-klein')['subcommands'])
        for web_svc in ('qwen-flash', 'deepseek-coder', 'deepseek-qwen-9b', 'unsloth-studio', 'r2-migrate'):
            self.assertTrue(catalog[web_svc]['has_web_ui'], f'{web_svc} should expose has_web_ui')
        self.assertIsNone(catalog['r2-migrate']['gpu'])
        self.assertFalse(catalog['ollama']['has_web_ui'])
        self.assertTrue(catalog['ollama']['chat_compatible'])

        valid_surfaces = set(api_catalog.SURFACE_IDS)
        for name, svc_meta in catalog.items():
            self.assertIn('openai_compatible', svc_meta)
            profile = svc_meta.get('api_profile')
            self.assertIsInstance(profile, dict, f'{name} missing api_profile')
            self.assertIn(profile.get('primary_surface'), valid_surfaces, f'{name} invalid primary_surface')
            self.assertTrue(profile.get('surfaces'), f'{name} missing surfaces')
            self.assertTrue(set(profile['surfaces']).issubset(valid_surfaces), f'{name} unknown surface')
            self.assertIsInstance(profile.get('features'), dict)
            self.assertIsInstance(profile.get('suggestions'), list)
            self.assertIsInstance(profile.get('endpoint_presets'), list)

        for oai_svc in (
            'qwen-flash', 'deepseek-coder', 'deepseek-qwen-9b', 'ollama',
            'flux-klein', 'qwen-image', 'minimax-h3-image',
            'qwen3-tts', 'indextts', 'drama3', 'tangoflux', 'nemotron-transcribe',
        ):
            self.assertTrue(catalog[oai_svc]['openai_compatible'], f'{oai_svc} should be openai_compatible')

        for native_svc in ('comfyui', 'unlimited-ocr', 'unsloth-studio', 'r2-migrate'):
            self.assertFalse(catalog[native_svc]['openai_compatible'], f'{native_svc} should be native API')
            self.assertEqual(catalog[native_svc]['api_profile']['primary_surface'], 'specialized')

        h3_profile = catalog['minimax-h3-image']['api_profile']
        self.assertTrue(h3_profile['features']['rejects_negative_prompt'])
        self.assertEqual(h3_profile['features']['fixed_steps'], 6)

        nemotron_profile = catalog['nemotron-transcribe']['api_profile']
        self.assertTrue(nemotron_profile['features']['salm_chat_audio'])
        self.assertEqual(nemotron_profile['primary_surface'], 'transcribe')

    def test_auto_discovery_for_future_openai_and_custom_runtimes(self):
        oai_profile = api_catalog.build_api_profile(
            'future-vllm-omni',
            doc='OpenAI compatible /v1/chat/completions, /v1/embeddings and /v1/images/generations server.',
            subcommand_names=['up', 'down', 'status'],
            auth='builtin',
        )
        self.assertTrue(oai_profile['openai_compatible'])
        self.assertIn('chat', oai_profile['surfaces'])
        self.assertIn('embeddings', oai_profile['surfaces'])
        self.assertIn('image', oai_profile['surfaces'])
        self.assertIn('inspector', oai_profile['surfaces'])

        custom_profile = api_catalog.build_api_profile(
            'future-custom-tool',
            doc='Specialized internal /api/jobs workflow runner.',
            subcommand_names=['up', 'jobs'],
            auth='caddy',
        )
        self.assertFalse(custom_profile['openai_compatible'])
        self.assertEqual(custom_profile['primary_surface'], 'specialized')
        self.assertIn('inspector', custom_profile['surfaces'])

    def test_ordered_image_actions_and_special_argparse_types(self):
        flux = introspect.get_service_schema('flux-klein')
        edit_cmd = next(s for s in flux['subcommands'] if s['name'] == 'edit')
        fields_by_flag = {fl: f for f in edit_cmd['fields'] for fl in f['flags']}
        self.assertIn('--image', fields_by_flag)
        self.assertIn('--drive-image', fields_by_flag)
        self.assertNotEqual(fields_by_flag['--image']['id'], fields_by_flag['--drive-image']['id'])
        prompt_field = next(f for f in edit_cmd['fields'] if f['positional'])

        argv, preview = introspect.build_cli_argv(
            'flux-klein',
            'edit',
            values={
                prompt_field['id']: 'render studio lighting',
                fields_by_flag['--image']['id']: ['local_a.png'],
                fields_by_flag['--drive-image']['id']: ['drive_b.png'],
            },
        )
        self.assertIn('--image', argv)
        self.assertIn('--drive-image', argv)
        self.assertIn('local_a.png', argv)
        self.assertIn('drive_b.png', argv)
        self.assertIn('render studio lighting', preview)

        # BooleanOptionalAction, _StoreFalseAction, nargs='?' handling & secret redaction.
        all_schemas = [introspect.get_service_schema(n) for n in introspect.get_service_catalog()]
        kinds_seen: set[str] = set()
        for s in all_schemas:
            for f in s['up_options']:
                kinds_seen.add(f['kind'])
            for sub in s['subcommands']:
                for f in sub['fields']:
                    kinds_seen.add(f['kind'])
        self.assertTrue({'switch', 'store_false', 'boolean_optional', 'optional_value', 'repeatable', 'select'}.issubset(kinds_seen))

        up_argv, up_preview = introspect.build_up_argv(
            'ollama',
            {'hf_token': 'hf_secret_abc', 'tunnel_mode': 'quick', 'dry_run': True},
        )
        self.assertIn('hf_secret_abc', up_argv)
        self.assertNotIn('hf_secret_abc', up_preview)
        self.assertIn('***', up_preview)

        qwen_argv, _ = introspect.build_cli_argv('qwen-flash', 'plan', values={'opt:--json': True})
        self.assertEqual(qwen_argv, ['plan', '--json'])

        r2_argv, r2_preview = introspect.build_cli_argv(
            'r2-migrate',
            'auth',
            values={'opt:--account-id': '0123456789abcdef0123456789abcdef', 'opt:--access-key-id': 'ak_test', 'opt:--secret-access-key': 'r2_super_secret_xyz'},
        )
        self.assertIn('r2_super_secret_xyz', r2_argv)
        self.assertNotIn('r2_super_secret_xyz', r2_preview)
        self.assertIn('***', r2_preview)


class WebServerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home_dir = self.tmp / 'colabllm_home'
        self.xdg_dir = self.tmp / 'xdg_config'
        self.user_dir = self.tmp / 'user_home'
        self.bin_dir = self.tmp / 'bin'
        for d in (self.home_dir, self.xdg_dir, self.user_dir, self.bin_dir):
            d.mkdir(parents=True, exist_ok=True)
        colab_stub = self.bin_dir / 'colab'
        colab_stub.write_text('#!/bin/sh\nexit 99\n', encoding='utf-8')
        colab_stub.chmod(0o755)
        self._env_patch = patch.dict(
            os.environ,
            {
                'COLABLLM_HOME': str(self.home_dir),
                'XDG_CONFIG_HOME': str(self.xdg_dir),
                'HOME': str(self.user_dir),
                'PATH': str(self.bin_dir) + os.pathsep + os.environ.get('PATH', ''),
            },
            clear=False,
        )
        self._env_patch.start()
        runner_bridge.clear_accounts_usage_cache()
        self._servers: list[tuple[web_server.WebConsoleServer, threading.Thread]] = []

    def tearDown(self):
        for srv, th in reversed(self._servers):
            srv.shutdown()
            th.join(timeout=2.0)
            srv.server_close()
        runner_bridge.clear_accounts_usage_cache()
        self._env_patch.stop()
        self._tmp.cleanup()

    def _start_console(self, *, read_only: bool = False) -> tuple[web_server.WebConsoleServer, int]:
        srv = web_server.WebConsoleServer(('127.0.0.1', 0), read_only=read_only)
        th = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
        th.start()
        self._servers.append((srv, th))
        return srv, srv.server_address[1]

    def _authenticate(self, srv: web_server.WebConsoleServer, port: int) -> tuple[str, str]:
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            conn.request('GET', f'/auth?token={srv.bootstrap_token}')
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 303)
            set_cookie = resp.getheader('Set-Cookie') or ''
            self.assertIn('HttpOnly', set_cookie)
            self.assertIn('SameSite=Strict', set_cookie)
            jar = http.cookies.SimpleCookie()
            jar.load(set_cookie)
            sid = jar['colabllm_sid'].value
            cookie_hdr = f'colabllm_sid={sid}'

            conn.request('GET', '/', headers={'Cookie': cookie_hdr})
            idx = conn.getresponse()
            html = idx.read().decode('utf-8')
            self.assertEqual(idx.status, 200)
            csp = idx.getheader('Content-Security-Policy') or ''
            self.assertIn("require-trusted-types-for 'script'", csp)
            m_nonce = re.search(r"script-src 'nonce-([^']+)' 'strict-dynamic'", csp)
            self.assertIsNotNone(m_nonce)
            self.assertIn(f'nonce="{m_nonce.group(1)}"', html)
            m_csrf = re.search(r'<meta name="colabllm-csrf" content="([^"]+)">', html)
            self.assertIsNotNone(m_csrf)
            return cookie_hdr, m_csrf.group(1)
        finally:
            conn.close()

    def test_security_guards_and_read_only_mode(self):
        srv, port = self._start_console(read_only=False)
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            # 1. Forbidden Host header.
            conn.request('GET', '/', headers={'Host': 'evil.invalid'})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 403)

            # 2. Fetch Metadata rejects cross-site BEFORE authentication check (403, not 401).
            conn.request('GET', '/api/bootstrap', headers={'Sec-Fetch-Site': 'cross-site'})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 403)

            # 3. Unauthenticated API returns 401; unauthenticated GET / renders auth gate with CSP + Trusted Types.
            conn.request('GET', '/api/bootstrap')
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 401)

            conn.request('GET', '/')
            r = conn.getresponse()
            gate_html = r.read().decode('utf-8')
            self.assertEqual(r.status, 200)
            self.assertIn('<form method="get" action="/auth"', gate_html)
            csp = r.getheader('Content-Security-Policy') or ''
            self.assertIn("require-trusted-types-for 'script'", csp)
            self.assertIn("script-src 'nonce-", csp)
        finally:
            conn.close()

        cookie_hdr, csrf = self._authenticate(srv, port)
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            # 4. Missing CSRF fails with 403; valid CSRF saves tunnel config in COLABLLM_HOME.
            body = json.dumps({'base_domain': 'example.com'}).encode('utf-8')
            conn.request('POST', '/api/tunnel', body=body, headers={'Cookie': cookie_hdr, 'Content-Type': 'application/json'})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 403)

            conn.request(
                'POST',
                '/api/tunnel',
                body=body,
                headers={'Cookie': cookie_hdr, 'X-ColabLLM-CSRF': csrf, 'Content-Type': 'application/json'},
            )
            r = conn.getresponse()
            payload = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            self.assertEqual(payload['base_domain'], 'example.com')
            self.assertEqual(json.loads((self.home_dir / 'tunnel-config.json').read_text(encoding='utf-8'))['base_domain'], 'example.com')
        finally:
            conn.close()

        # 5. --read-only blocks all state-changing & secret endpoints.
        ro_srv, ro_port = self._start_console(read_only=True)
        ro_cookie, ro_csrf = self._authenticate(ro_srv, ro_port)
        conn = http.client.HTTPConnection('127.0.0.1', ro_port, timeout=5)
        try:
            ro_headers = {'Cookie': ro_cookie, 'X-ColabLLM-CSRF': ro_csrf, 'Content-Type': 'application/json'}
            for endpoint, payload in (
                ('/api/instances/ollama/up', {'dry_run': False}),
                ('/api/instances/ollama/secret', {'instance': ''}),
                ('/api/tasks/run', {'service': 'qwen-flash', 'subcommand': 'plan'}),
                ('/api/tunnel', {'base_domain': 'example.org'}),
            ):
                conn.request('POST', endpoint, body=json.dumps(payload), headers=ro_headers)
                r = conn.getresponse()
                r.read()
                self.assertEqual(r.status, 403, f'Expected 403 in read-only for {endpoint}')

            conn.request('GET', '/api/proxy/qwen-flash/default/v1/models', headers={'Cookie': ro_cookie})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 403)
        finally:
            conn.close()

    def test_secrets_dry_run_tasks_sse_logs_and_proxy(self):
        svc_dir = self.home_dir / 'services' / 'qwen-flash'
        svc_dir.mkdir(parents=True, exist_ok=True)
        state_file = svc_dir / 'state.json'
        state_file.write_text(
            json.dumps({
                'url': 'https://demo.trycloudflare.com',
                'api_token': 'sk-secret-test-123',
                'auth_user': 'colab',
                'auth_pass': 'pw-secret-456',
                'ready': True,
            }),
            encoding='utf-8',
        )
        log_file = svc_dir / 'run.log'
        log_file.write_text('line1 boot\n', encoding='utf-8')

        unsloth_dir = self.home_dir / 'services' / 'unsloth-studio'
        unsloth_dir.mkdir(parents=True, exist_ok=True)
        (unsloth_dir / 'state.json').write_text(
            json.dumps({
                'url': 'https://studio-demo.trycloudflare.com',
                'api_token': 'unused-studio-token',
                'auth_user': 'admin',
                'auth_pass': 'unused-caddy-pass',
                'login_user': 'unsloth',
                'login_pass': 'studio-secret-789',
                'ready': True,
            }),
            encoding='utf-8',
        )

        comfy_dir = self.home_dir / 'services' / 'comfyui'
        comfy_dir.mkdir(parents=True, exist_ok=True)
        (comfy_dir / 'state.json').write_text(
            json.dumps({
                'url': 'https://comfy-demo.trycloudflare.com',
                'auth_user': 'admin',
                'auth_pass': 'caddy-secret-999',
                'ready': True,
            }),
            encoding='utf-8',
        )

        srv, port = self._start_console(read_only=False)
        cookie_hdr, csrf = self._authenticate(srv, port)
        hdrs = {'Cookie': cookie_hdr, 'X-ColabLLM-CSRF': csrf, 'Content-Type': 'application/json'}

        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
        try:
            # 1. Bootstrap strictly redacts secrets and scopes flags per service auth mode.
            conn.request('GET', '/api/bootstrap', headers={'Cookie': cookie_hdr})
            r = conn.getresponse()
            raw_boot = r.read().decode('utf-8')
            self.assertEqual(r.status, 200)
            self.assertNotIn('sk-secret-test-123', raw_boot)
            self.assertNotIn('pw-secret-456', raw_boot)
            self.assertNotIn('studio-secret-789', raw_boot)
            self.assertNotIn('caddy-secret-999', raw_boot)
            self.assertNotIn('unused-caddy-pass', raw_boot)
            self.assertNotIn('unused-studio-token', raw_boot)
            boot = json.loads(raw_boot)
            inst = next(i for i in boot['instances'] if i['service'] == 'qwen-flash')
            self.assertTrue(inst['has_api_token'])
            self.assertFalse(inst['has_basic_auth'])
            self.assertTrue(inst['has_web_ui'])
            self.assertTrue(inst['chat_compatible'])
            unsloth_inst = next(i for i in boot['instances'] if i['service'] == 'unsloth-studio')
            self.assertTrue(unsloth_inst['has_app_login'])
            self.assertFalse(unsloth_inst['has_api_token'])
            self.assertFalse(unsloth_inst['has_basic_auth'])
            self.assertTrue(unsloth_inst['has_web_ui'])
            comfy_inst = next(i for i in boot['instances'] if i['service'] == 'comfyui')
            self.assertTrue(comfy_inst['has_basic_auth'])

            # 2. On-demand secret reveal returns secrets scoped to each service when authorized.
            conn.request('POST', '/api/instances/qwen-flash/secret', body=json.dumps({'instance': ''}), headers=hdrs)
            r = conn.getresponse()
            sec = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            self.assertEqual(sec['api_token'], 'sk-secret-test-123')
            self.assertNotIn('auth_pass', sec)

            conn.request('POST', '/api/instances/unsloth-studio/secret', body=json.dumps({'instance': ''}), headers=hdrs)
            r = conn.getresponse()
            unsloth_sec = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            self.assertEqual(unsloth_sec['login_user'], 'unsloth')
            self.assertEqual(unsloth_sec['login_pass'], 'studio-secret-789')
            self.assertNotIn('auth_user', unsloth_sec)
            self.assertNotIn('auth_pass', unsloth_sec)
            self.assertNotIn('api_token', unsloth_sec)

            conn.request('POST', '/api/instances/comfyui/secret', body=json.dumps({'instance': ''}), headers=hdrs)
            r = conn.getresponse()
            comfy_sec = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            self.assertEqual(comfy_sec['auth_user'], 'admin')
            self.assertEqual(comfy_sec['auth_pass'], 'caddy-secret-999')

            # 3. Dry-run up executes subprocess and leaves ollama instance state untouched.
            conn.request(
                'POST',
                '/api/instances/ollama/up',
                body=json.dumps({'dry_run': True, 'tunnel_mode': 'quick'}),
                headers=hdrs,
            )
            r = conn.getresponse()
            dry = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            self.assertTrue(dry['dry_run'])
            self.assertIn('cell 1: run', dry['output'].lower())
            self.assertFalse((self.home_dir / 'services' / 'ollama' / 'state.json').exists())

            # 4. Run business task + stream task SSE until done.
            conn.request(
                'POST',
                '/api/tasks/run',
                body=json.dumps({'service': 'qwen-flash', 'subcommand': 'plan', 'values': {'opt:--json': True}}),
                headers=hdrs,
            )
            r = conn.getresponse()
            task = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 202)

            conn.request('GET', f"/api/tasks/{task['task_id']}/stream", headers={'Cookie': cookie_hdr})
            sse_resp = conn.getresponse()
            self.assertEqual(sse_resp.status, 200)
            events = _read_sse_until(sse_resp, lambda evs: any(ev[0] == 'done' for ev in evs), timeout=10.0)
            sse_resp.close()
            self.assertTrue(any(ev[0] == 'chunk' for ev in events))
            done_data = json.loads(next(ev[1] for ev in events if ev[0] == 'done'))
            self.assertEqual(done_data['status'], 'succeeded')
            self.assertEqual(done_data['returncode'], 0)

            # 4b. GET /api/doctor propagates dynamic COLABLLM_HOME through subprocess boundary.
            conn.request('GET', '/api/doctor?no_drive=1', headers={'Cookie': cookie_hdr})
            r = conn.getresponse()
            doc = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            self.assertIn('checks', doc)
            self.assertTrue(doc['checks'])
        finally:
            conn.close()

        # 5. Live log SSE init + appended chunk + clean server.shutdown() cancellation.
        log_conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        try:
            log_conn.request('GET', '/api/instances/qwen-flash/logs?stream=1&lines=50', headers={'Cookie': cookie_hdr})
            log_resp = log_conn.getresponse()
            self.assertEqual(log_resp.status, 200)
            init_events = _read_sse_until(log_resp, lambda evs: any(ev[0] == 'init' for ev in evs), timeout=5.0)
            self.assertIn('line1 boot', json.loads(init_events[0][1])['content'])
            with log_file.open('a', encoding='utf-8') as handle:
                handle.write('line2 CT_READY\n')
            chunk_events = _read_sse_until(log_resp, lambda evs: any(ev[0] == 'chunk' for ev in evs), timeout=5.0)
            self.assertIn('line2 CT_READY', json.loads(chunk_events[0][1])['text'])
            # Leave log_resp open across shutdown to verify shutdown() terminates SSE loop promptly.
            srv.shutdown()
            th = next(t for s, t in self._servers if s is srv)
            th.join(timeout=2.0)
            self.assertFalse(th.is_alive())
            srv.server_close()
            self._servers = [(s, t) for s, t in self._servers if s is not srv]
        finally:
            log_conn.close()

        # 6. /api/proxy credential injection, cross-origin redirect protection & sandbox CSP.
        received_auth: list[str] = []

        class _MockUpstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                received_auth.append(self.headers.get('Authorization', ''))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', 'https://other-origin.invalid/steal')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                body = json.dumps({'object': 'list', 'data': [{'id': 'mock-model'}]}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _MockUpstream)
        up_th = threading.Thread(target=upstream.serve_forever, daemon=True)
        up_th.start()
        try:
            up_port = upstream.server_address[1]
            srv2, port2 = self._start_console(read_only=False)
            cookie2, _ = self._authenticate(srv2, port2)
            c2 = http.client.HTTPConnection('127.0.0.1', port2, timeout=5)
            try:
                # Unsafe URL scheme is rejected by origin() before connecting.
                state_file.write_text(
                    json.dumps({'url': 'file:///etc/passwd', 'api_token': 'sk-secret-test-123'}),
                    encoding='utf-8',
                )
                c2.request('GET', '/api/proxy/qwen-flash/default/v1/models', headers={'Cookie': cookie2})
                r = c2.getresponse()
                r.read()
                self.assertIn(r.status, (400, 502))
                self.assertEqual(received_auth, [])

                # Valid upstream injects Bearer & sandbox CSP; cross-origin redirect is blocked by safe_urlopen.
                state_file.write_text(
                    json.dumps({'url': f'http://127.0.0.1:{up_port}', 'api_token': 'sk-secret-test-123'}),
                    encoding='utf-8',
                )
                c2.request('GET', '/api/proxy/qwen-flash/default/v1/models', headers={'Cookie': cookie2})
                r = c2.getresponse()
                proxied = json.loads(r.read().decode('utf-8'))
                self.assertEqual(r.status, 200)
                self.assertEqual(received_auth[-1], 'Bearer sk-secret-test-123')
                self.assertEqual(r.getheader('Content-Security-Policy'), 'sandbox')
                self.assertEqual(proxied['data'][0]['id'], 'mock-model')

                c2.request('GET', '/api/proxy/qwen-flash/default/redirect', headers={'Cookie': cookie2})
                r = c2.getresponse()
                r.read()
                self.assertEqual(r.status, 502)
            finally:
                c2.close()
        finally:
            upstream.shutdown()
            up_th.join(timeout=2.0)
            upstream.server_close()

    def test_proxy_extended_methods_streaming_headers_and_unsloth_login(self):
        recorded: list[tuple[str, str, str, bytes]] = []

        class _MockExtendedUpstream(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, format, *args):
                return

            def do_HEAD(self):
                recorded.append(('HEAD', self.path, self.headers.get('Authorization', ''), b''))
                self.send_response(200)
                self.send_header('Content-Type', 'audio/wav')
                self.send_header('X-Audio-Sample-Rate', '44100')
                self.send_header('X-Saved-Drive-Path', '/colabllm/out.wav')
                self.send_header('Content-Length', '128')
                self.end_headers()

            def do_PUT(self):
                length = int(self.headers.get('Content-Length') or '0')
                body = self.rfile.read(length) if length else b''
                recorded.append(('PUT', self.path, self.headers.get('Authorization', ''), body))
                out = json.dumps({'updated': True}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_DELETE(self):
                recorded.append(('DELETE', self.path, self.headers.get('Authorization', ''), b''))
                out = json.dumps({'deleted': True}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or '0')
                body = self.rfile.read(length) if length else b''
                recorded.append(('POST', self.path, self.headers.get('Authorization', ''), body))
                if self.path == '/api/pull':
                    lines = b'{"status":"pulling"}\n{"status":"success"}\n'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/x-ndjson')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(lines)
                    return
                out = json.dumps({'received': json.loads(body.decode('utf-8')) if body else None}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _MockExtendedUpstream)
        up_th = threading.Thread(target=upstream.serve_forever, daemon=True)
        up_th.start()
        try:
            up_port = upstream.server_address[1]
            unsloth_dir = self.home_dir / 'services' / 'unsloth-studio'
            unsloth_dir.mkdir(parents=True, exist_ok=True)
            (unsloth_dir / 'state.json').write_text(
                json.dumps({
                    'url': f'http://127.0.0.1:{up_port}',
                    'login_user': 'studio_admin',
                    'login_pass': 'studio_pass_99',
                }),
                encoding='utf-8',
            )
            ollama_dir = self.home_dir / 'services' / 'ollama'
            ollama_dir.mkdir(parents=True, exist_ok=True)
            (ollama_dir / 'state.json').write_text(
                json.dumps({
                    'url': f'http://127.0.0.1:{up_port}',
                    'api_token': 'tok-ollama-1',
                }),
                encoding='utf-8',
            )

            srv, port = self._start_console(read_only=False)
            cookie, csrf = self._authenticate(srv, port)
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                # 1. Bootstrap includes capability_surfaces + new static files are served.
                conn.request('GET', '/api/bootstrap', headers={'Cookie': cookie})
                boot_resp = conn.getresponse()
                boot = json.loads(boot_resp.read().decode('utf-8'))
                self.assertEqual(boot_resp.status, 200)
                self.assertEqual(len(boot['capability_surfaces']), 7)
                for asset in ('copilot.css', 'copilot.js', 'copilot_studios.js'):
                    conn.request('GET', f'/static/{asset}', headers={'Cookie': cookie})
                    sr = conn.getresponse()
                    sr.read()
                    self.assertEqual(sr.status, 200, f'/static/{asset} failed')

                # 2. CSRF enforcement on PUT / DELETE.
                conn.request('PUT', '/api/proxy/ollama/default/v1/item', body='{}', headers={'Cookie': cookie})
                r = conn.getresponse()
                r.read()
                self.assertEqual(r.status, 403)

                conn.request('DELETE', '/api/proxy/ollama/default/v1/audio/voices/demo', headers={'Cookie': cookie})
                r = conn.getresponse()
                r.read()
                self.assertEqual(r.status, 403)

                # 3. HEAD preserves passthrough headers & sandbox CSP.
                conn.request('HEAD', '/api/proxy/ollama/default/api/ct/jobs/1', headers={'Cookie': cookie})
                r = conn.getresponse()
                body = r.read()
                self.assertEqual(r.status, 200)
                self.assertEqual(body, b'')
                self.assertEqual(r.getheader('Content-Security-Policy'), 'sandbox')
                self.assertEqual(r.getheader('X-Audio-Sample-Rate'), '44100')
                self.assertEqual(r.getheader('X-Saved-Drive-Path'), '/colabllm/out.wav')

                # 4. PUT & DELETE with CSRF + X-ColabLLM-Proxy-Authorization override.
                conn.request(
                    'PUT',
                    '/api/proxy/ollama/default/v1/item',
                    body=b'{"x":1}',
                    headers={
                        'Cookie': cookie,
                        'X-ColabLLM-CSRF': csrf,
                        'Content-Type': 'application/json',
                        'X-ColabLLM-Proxy-Authorization': 'Bearer custom-jwt-xyz',
                    },
                )
                r = conn.getresponse()
                self.assertEqual(r.status, 200)
                self.assertTrue(json.loads(r.read().decode('utf-8'))['updated'])
                self.assertEqual(recorded[-1][2], 'Bearer custom-jwt-xyz')

                conn.request(
                    'DELETE',
                    '/api/proxy/ollama/default/v1/audio/voices/demo',
                    headers={'Cookie': cookie, 'X-ColabLLM-CSRF': csrf},
                )
                r = conn.getresponse()
                self.assertEqual(r.status, 200)
                self.assertTrue(json.loads(r.read().decode('utf-8'))['deleted'])
                self.assertEqual(recorded[-1][2], 'Bearer tok-ollama-1')

                # 5. NDJSON streaming passthrough.
                conn.request(
                    'POST',
                    '/api/proxy/ollama/default/api/pull',
                    body=b'{"model":"qwen3"}',
                    headers={'Cookie': cookie, 'X-ColabLLM-CSRF': csrf, 'Content-Type': 'application/json'},
                )
                r = conn.getresponse()
                ndjson_body = r.read().decode('utf-8')
                self.assertEqual(r.status, 200)
                self.assertIn('application/x-ndjson', r.getheader('Content-Type') or '')
                self.assertEqual(len(ndjson_body.strip().splitlines()), 2)

                # Need a fresh HTTP connection after streaming Connection: close.
                conn.close()
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)

                # 6. Unsloth Studio /api/auth/login credential injection on empty {} body.
                conn.request(
                    'POST',
                    '/api/proxy/unsloth-studio/default/api/auth/login',
                    body=b'{}',
                    headers={'Cookie': cookie, 'X-ColabLLM-CSRF': csrf, 'Content-Type': 'application/json'},
                )
                r = conn.getresponse()
                res = json.loads(r.read().decode('utf-8'))
                self.assertEqual(r.status, 200)
                self.assertEqual(res['received'], {'username': 'studio_admin', 'password': 'studio_pass_99'})
            finally:
                conn.close()
        finally:
            upstream.shutdown()
            up_th.join(timeout=2.0)
            upstream.server_close()

    def test_instance_sorting_and_enriched_metadata(self):
        catalog = introspect.get_service_catalog()
        svc_root = self.home_dir / 'services'

        def _make_inst(name: str, state: dict | str, *, mtime: float, log_mtime: float | None = None) -> None:
            d = svc_root / name
            d.mkdir(parents=True, exist_ok=True)
            st_file = d / 'state.json'
            if isinstance(state, str):
                st_file.write_text(state, encoding='utf-8')
            else:
                st_file.write_text(json.dumps(state), encoding='utf-8')
            os.utime(st_file, (mtime, mtime))
            if log_mtime is not None:
                log_file = d / 'run.log'
                log_file.write_text('boot line\n', encoding='utf-8')
                os.utime(log_file, (log_mtime, log_mtime))

        _make_inst('r2-migrate', {'runner_exit': 0}, mtime=1700000100.1234)
        _make_inst('r2-migrate-2', {'runner_exit': None}, mtime=1700000200.0, log_mtime=1700000300.4567)
        _make_inst('ollama-3', {'runner_exit': 0}, mtime=1700000050.0)
        _make_inst('ollama-2', {'runner_exit': 0}, mtime=1700000050.0)
        _make_inst('comfyui', {'runner_exit': 1}, mtime=1700000010.0)
        _make_inst('flux-klein', '{corrupt json', mtime=1700000020.0)
        _make_inst('ollama', {'runner_pid': 102, 'runner_identity': 'live:102', 'ready': False}, mtime=1700000005.0)
        _make_inst('qwen-flash', {'runner_pid': 101, 'runner_identity': 'live:101', 'ready': True}, mtime=1700000001.0)

        live_pids = {101, 102}
        with patch.object(state_store, 'runner_alive', side_effect=lambda st: st.get('runner_pid') in live_pids):
            items = state_store.list_all_instances(catalog)
            self.assertEqual(
                [i['session_name'] for i in items],
                [
                    'qwen-flash',    # status_rank 0 (READY)
                    'ollama',        # status_rank 1 (STARTING)
                    'flux-klein',    # status_rank 2 (corrupt, newer mtime)
                    'comfyui',       # status_rank 2 (failed_exit, older mtime)
                    'r2-migrate-2',  # status_rank 3 (clean STOPPED, run.log mtime 300.457)
                    'r2-migrate',    # status_rank 3 (clean STOPPED, mtime 100.123)
                    'ollama-2',      # status_rank 3 (tie on mtime 50.0 -> service, instance_arg)
                    'ollama-3',      # status_rank 3
                ],
            )

            required_keys = {
                'service', 'instance', 'instance_arg', 'session_name', 'running', 'ready',
                'state_label', 'stage_hint', 'auto_restart', 'restart_count', 'url', 'hostname',
                'transport', 'account', 'drive_account', 'started', 'session_gpu', 'mount_drive',
                'runner_exit', 'log_exists', 'has_api_token', 'has_basic_auth', 'has_app_login',
                'auth_mode', 'has_web_ui', 'chat_compatible', 'openai_compatible', 'primary_surface',
                'corrupt', 'category', 'category_label', 'failed_exit', 'updated_ts', 'status_rank',
            }
            by_name = {i['session_name']: i for i in items}
            for item in items:
                self.assertTrue(required_keys.issubset(item.keys()))

            self.assertEqual(by_name['qwen-flash']['status_rank'], 0)
            self.assertEqual(by_name['qwen-flash']['category'], 'llm')
            self.assertEqual(by_name['qwen-flash']['category_label'], 'LLM 与推理')
            self.assertTrue(by_name['qwen-flash']['openai_compatible'])
            self.assertEqual(by_name['qwen-flash']['primary_surface'], 'chat')
            self.assertFalse(by_name['qwen-flash']['failed_exit'])

            self.assertEqual(by_name['ollama']['status_rank'], 1)
            self.assertEqual(by_name['ollama']['state_label'], 'STARTING')

            self.assertEqual(by_name['comfyui']['status_rank'], 2)
            self.assertTrue(by_name['comfyui']['failed_exit'])
            self.assertEqual(by_name['comfyui']['state_label'], 'STOPPED')
            self.assertFalse(by_name['comfyui']['openai_compatible'])
            self.assertEqual(by_name['comfyui']['primary_surface'], 'specialized')

            self.assertEqual(by_name['flux-klein']['status_rank'], 2)
            self.assertTrue(by_name['flux-klein']['corrupt'])
            self.assertFalse(by_name['flux-klein']['failed_exit'])
            self.assertEqual(by_name['flux-klein']['state_label'], 'ERROR')

            self.assertEqual(by_name['r2-migrate']['status_rank'], 3)
            self.assertEqual(by_name['r2-migrate']['category'], 'other')
            self.assertEqual(by_name['r2-migrate']['category_label'], '其他运行时')
            self.assertAlmostEqual(by_name['r2-migrate']['updated_ts'], 1700000100.123, places=3)
            self.assertAlmostEqual(by_name['r2-migrate-2']['updated_ts'], 1700000300.457, places=3)
            self.assertTrue(by_name['r2-migrate-2']['log_exists'])

            srv, port = self._start_console(read_only=False)
            cookie_hdr, _ = self._authenticate(srv, port)
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                conn.request('GET', '/api/bootstrap', headers={'Cookie': cookie_hdr})
                r = conn.getresponse()
                boot = json.loads(r.read().decode('utf-8'))
                self.assertEqual(r.status, 200)
                self.assertEqual(
                    [i['session_name'] for i in boot['instances']],
                    [i['session_name'] for i in items],
                )
            finally:
                conn.close()

    def test_instance_mtime_cache_invalidation_and_runner_flip(self):
        catalog = introspect.get_service_catalog()
        svc_root = self.home_dir / 'services'

        stopped_dir = svc_root / 'ollama'
        stopped_dir.mkdir(parents=True, exist_ok=True)
        stopped_state = stopped_dir / 'state.json'
        stopped_state.write_text(json.dumps({'runner_exit': 0}), encoding='utf-8')

        ready_dir = svc_root / 'qwen-flash'
        ready_dir.mkdir(parents=True, exist_ok=True)
        (ready_dir / 'state.json').write_text(
            json.dumps({'runner_pid': 202, 'runner_identity': 'live:202', 'ready': True}),
            encoding='utf-8',
        )

        starting_dir = svc_root / 'ollama-2'
        starting_dir.mkdir(parents=True, exist_ok=True)
        (starting_dir / 'state.json').write_text(
            json.dumps({'runner_pid': 203, 'runner_identity': 'live:203', 'ready': False}),
            encoding='utf-8',
        )
        starting_log = starting_dir / 'run.log'
        starting_log.write_text('cell 1\n', encoding='utf-8')

        live_pids = {202, 203}
        with (
            patch.object(state_store, 'runner_alive', side_effect=lambda st: st.get('runner_pid') in live_pids),
            patch.object(state_store, '_load_state_json', wraps=state_store._load_state_json) as load_spy,
            patch.object(state_store, '_read_tail_bytes', wraps=state_store._read_tail_bytes) as tail_spy,
        ):
            items1 = state_store.list_all_instances(catalog)
            self.assertEqual(len(items1), 3)
            self.assertEqual(load_spy.call_count, 3)
            self.assertEqual(tail_spy.call_count, 1)

            # Unchanged files: zero state.json or run.log reads.
            items2 = state_store.list_all_instances(catalog)
            self.assertEqual(load_spy.call_count, 3)
            self.assertEqual(tail_spy.call_count, 1)
            self.assertEqual([i['session_name'] for i in items2], [i['session_name'] for i in items1])

            # Mutating state.json invalidates only that entry immediately.
            stopped_state.write_text(json.dumps({'runner_exit': 2}), encoding='utf-8')
            items3 = state_store.list_all_instances(catalog)
            self.assertEqual(load_spy.call_count, 4)
            ollama_item = next(i for i in items3 if i['session_name'] == 'ollama')
            self.assertTrue(ollama_item['failed_exit'])
            self.assertEqual(ollama_item['status_rank'], 2)

            # Mutating run.log invalidates only that entry immediately.
            starting_log.write_text('cell 1\nCT_PUBLIC_URL=https://demo.trycloudflare.com\n', encoding='utf-8')
            items4 = state_store.list_all_instances(catalog)
            self.assertEqual(load_spy.call_count, 5)
            self.assertEqual(tail_spy.call_count, 2)
            ol2_item = next(i for i in items4 if i['session_name'] == 'ollama-2')
            self.assertIn('隧道已连通', ol2_item['stage_hint'])

            # Recorded runner_pid death flips running->stopped promptly without re-reading files.
            live_pids.remove(202)
            items5 = state_store.list_all_instances(catalog)
            self.assertEqual(load_spy.call_count, 5)
            self.assertEqual(tail_spy.call_count, 2)
            qwen_item = next(i for i in items5 if i['session_name'] == 'qwen-flash')
            self.assertFalse(qwen_item['running'])
            self.assertFalse(qwen_item['ready'])
            self.assertEqual(qwen_item['state_label'], 'STOPPED')
            self.assertEqual(qwen_item['status_rank'], 3)

            # Deleting an instance directory evicts it from cache.
            resolved_starting = starting_dir.resolve()
            shutil.rmtree(starting_dir)
            items6 = state_store.list_all_instances(catalog)
            self.assertNotIn('ollama-2', [i['session_name'] for i in items6])
            self.assertNotIn(resolved_starting, state_store._INSTANCE_CACHE)

    def test_events_sse_snapshot_cache_and_invalidation(self):
        svc_dir = self.home_dir / 'services' / 'ollama-2'
        svc_dir.mkdir(parents=True, exist_ok=True)
        (svc_dir / 'state.json').write_text(json.dumps({'runner_exit': 0}), encoding='utf-8')

        srv, port = self._start_console(read_only=False)
        self.assertEqual(web_server.WebConsoleServer.EVENTS_SNAPSHOT_TTL, 0.75)

        fake_now = [100.0]
        with (
            patch.object(web_server.time, 'monotonic', side_effect=lambda: fake_now[0]),
            patch.object(state_store, 'list_all_instances', wraps=state_store.list_all_instances) as list_spy,
        ):
            enc1, dig1 = srv.get_events_snapshot()
            enc2, dig2 = srv.get_events_snapshot()
            self.assertEqual((enc1, dig1), (enc2, dig2))
            self.assertEqual(list_spy.call_count, 1)

            # Still within 0.75s TTL.
            fake_now[0] = 100.50
            srv.get_events_snapshot()
            self.assertEqual(list_spy.call_count, 1)

            # Explicit invalidation forces immediate recompute even within TTL.
            srv.invalidate_events_snapshot()
            srv.get_events_snapshot()
            self.assertEqual(list_spy.call_count, 2)

            # Crossing 0.75s TTL expires cache.
            fake_now[0] = 101.30
            srv.get_events_snapshot()
            self.assertEqual(list_spy.call_count, 3)

        cookie_hdr, csrf = self._authenticate(srv, port)
        sse1 = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        sse2 = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        ctrl = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            sse1.request('GET', '/api/events', headers={'Cookie': cookie_hdr})
            sse2.request('GET', '/api/events', headers={'Cookie': cookie_hdr})
            r1 = sse1.getresponse()
            r2 = sse2.getresponse()
            self.assertEqual(r1.status, 200)
            self.assertEqual(r2.status, 200)

            ev1 = _read_sse_until(r1, lambda evs: any(e[0] == 'state' for e in evs), timeout=5.0)
            ev2 = _read_sse_until(r2, lambda evs: any(e[0] == 'state' for e in evs), timeout=5.0)
            p1 = json.loads(ev1[0][1])
            p2 = json.loads(ev2[0][1])
            self.assertEqual([i['session_name'] for i in p1['instances']], ['ollama-2'])
            self.assertEqual(p1, p2)
            self.assertIn('status_rank', p1['instances'][0])
            self.assertIn('category', p1['instances'][0])

            # Removing instance invalidates snapshot cache and pushes updated SSE state frame.
            ctrl.request(
                'POST',
                '/api/instances/ollama/remove',
                body=json.dumps({'instance': '2'}),
                headers={'Cookie': cookie_hdr, 'X-ColabLLM-CSRF': csrf, 'Content-Type': 'application/json'},
            )
            rm_resp = ctrl.getresponse()
            rm_resp.read()
            self.assertEqual(rm_resp.status, 200)

            ev1_next = _read_sse_until(r1, lambda evs: any(e[0] == 'state' for e in evs), timeout=5.0)
            self.assertEqual(json.loads(ev1_next[0][1])['instances'], [])
        finally:
            ctrl.close()
            sse1.close()
            sse2.close()

    def test_sse_connection_pool_cap_prevents_http11_starvation(self):
        srv, port = self._start_console(read_only=False)
        cookie_hdr, csrf = self._authenticate(srv, port)
        hdrs = {'Cookie': cookie_hdr, 'X-ColabLLM-CSRF': csrf, 'Content-Type': 'application/json'}

        conns: list[http.client.HTTPConnection] = []
        resps: list[http.client.HTTPResponse] = []
        ctrl = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            for _ in range(5):
                c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                conns.append(c)
                c.request('GET', '/api/events', headers={'Cookie': cookie_hdr})
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                evs = _read_sse_until(r, lambda items: any(e[0] == 'state' for e in items), timeout=5.0)
                self.assertTrue(evs)
                resps.append(r)

            deadline = time.monotonic() + 2.0
            while len(srv._events_sse) > 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertLessEqual(len(srv._events_sse), 3)

            # Oldest 2 streams must have been evicted and closed cleanly so HTTP/1.1 sockets stay free.
            for evicted_resp in resps[:2]:
                evicted_resp.fp.raw._sock.settimeout(2.0)  # type: ignore[attr-defined]
                self.assertEqual(evicted_resp.readline(), b'')

            # While newest SSE streams remain open, dry-run up still responds 200 immediately.
            ctrl.request(
                'POST',
                '/api/instances/ollama/up',
                body=json.dumps({'dry_run': True, 'tunnel_mode': 'quick'}),
                headers=hdrs,
            )
            dry_resp = ctrl.getresponse()
            dry_data = json.loads(dry_resp.read().decode('utf-8'))
            self.assertEqual(dry_resp.status, 200)
            self.assertTrue(dry_data['dry_run'])

            # Running a task populates output_tail and pushes updated task state on /api/events.
            ctrl.request(
                'POST',
                '/api/tasks/run',
                body=json.dumps({'service': 'qwen-flash', 'subcommand': 'plan', 'values': {'opt:--json': True}}),
                headers=hdrs,
            )
            task_resp = ctrl.getresponse()
            task = json.loads(task_resp.read().decode('utf-8'))
            self.assertEqual(task_resp.status, 202)
            task_id = task['task_id']

            deadline = time.monotonic() + 10.0
            listed = None
            while time.monotonic() < deadline:
                items = srv.task_manager.list_tasks()
                match = next((it for it in items if it['task_id'] == task_id), None)
                if match and match['status'] != 'running':
                    listed = match
                    break
                time.sleep(0.05)
            self.assertIsNotNone(listed)
            self.assertIn('output_tail', listed)
            self.assertIn('truncated', listed)
            self.assertTrue(listed['output_tail'].strip())

            latest_events = _read_sse_until(
                resps[-1],
                lambda items: any(
                    e[0] == 'state'
                    and any(
                        t['task_id'] == task_id and t['status'] != 'running' and t.get('output_tail')
                        for t in json.loads(e[1]).get('tasks', [])
                    )
                    for e in items
                ),
                timeout=5.0,
            )
            self.assertTrue(latest_events)
        finally:
            ctrl.close()
            for c in conns:
                c.close()

    def test_remove_and_prune_stopped_instance_endpoints(self):
        catalog = introspect.get_service_catalog()
        svc_root = self.home_dir / 'services'

        def _create(name: str, payload: dict | str) -> Path:
            d = svc_root / name
            d.mkdir(parents=True, exist_ok=True)
            content = payload if isinstance(payload, str) else json.dumps(payload)
            (d / 'state.json').write_text(content, encoding='utf-8')
            return d

        ol_default = _create('ollama', {'runner_exit': 0})
        ol_rep2 = _create('ollama-2', {'runner_exit': 0})
        ol_corrupt = _create('ollama-3', '{corrupt json')
        qw_default = _create('qwen-flash', {'runner_exit': 1})
        qw_rep1 = _create('qwen-flash-r1', {'runner_exit': 0})
        comfy_running = _create('comfyui-2', {'runner_pid': 303, 'runner_identity': 'live:303', 'ready': True})
        corrupt_running = _create('flux-klein-live', '{"runner_pid": 303, "runner_identity": "live:303"')
        unknown_dir = _create('custom-unknown', {'runner_exit': 0})

        with patch.object(state_store, 'runner_alive', side_effect=lambda st: st.get('runner_pid') == 303):
            # Symlink directory must be rejected even if pointing inside services_root.
            sym_dir = svc_root / 'ollama-sym'
            sym_dir.symlink_to(ol_default)
            with self.assertRaises(UsageError):
                state_store.remove_stopped_instance('ollama', 'sym', catalog)
            self.assertTrue(ol_default.is_dir())
            sym_dir.unlink()

            # Corrupt JSON that still contains a live runner PID is refused.
            with self.assertRaises(CliError):
                state_store.remove_stopped_instance('flux-klein', 'live', catalog)
            self.assertTrue(corrupt_running.is_dir())
            shutil.rmtree(corrupt_running)

            srv, port = self._start_console(read_only=False)
            cookie_hdr, csrf = self._authenticate(srv, port)
            hdrs = {'Cookie': cookie_hdr, 'X-ColabLLM-CSRF': csrf, 'Content-Type': 'application/json'}

            ro_srv, ro_port = self._start_console(read_only=True)
            ro_cookie, ro_csrf = self._authenticate(ro_srv, ro_port)
            ro_hdrs = {'Cookie': ro_cookie, 'X-ColabLLM-CSRF': ro_csrf, 'Content-Type': 'application/json'}

            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            ro_conn = http.client.HTTPConnection('127.0.0.1', ro_port, timeout=5)
            try:
                # Missing CSRF -> 403; unauthenticated -> 401; read-only -> 403.
                for path, body in (
                    ('/api/instances/ollama/remove', {'instance': '2'}),
                    ('/api/instances/prune', {'replicas_only': True}),
                ):
                    conn.request('POST', path, body=json.dumps(body))
                    r = conn.getresponse()
                    r.read()
                    self.assertEqual(r.status, 401)

                    conn.request('POST', path, body=json.dumps(body), headers={'Cookie': cookie_hdr, 'Content-Type': 'application/json'})
                    r = conn.getresponse()
                    r.read()
                    self.assertEqual(r.status, 403)

                    ro_conn.request('POST', path, body=json.dumps(body), headers=ro_hdrs)
                    r = ro_conn.getresponse()
                    r.read()
                    self.assertEqual(r.status, 403)

                # Running instance refusal -> 502 from CliError.
                conn.request('POST', '/api/instances/comfyui/remove', body=json.dumps({'instance': '2'}), headers=hdrs)
                r = conn.getresponse()
                r.read()
                self.assertEqual(r.status, 502)
                self.assertTrue(comfy_running.is_dir())

                # Remove stopped replica succeeds -> 200.
                conn.request('POST', '/api/instances/ollama/remove', body=json.dumps({'instance': '2'}), headers=hdrs)
                r = conn.getresponse()
                rm_payload = json.loads(r.read().decode('utf-8'))
                self.assertEqual(r.status, 200)
                self.assertEqual(rm_payload, {'ok': True, 'removed': ['ollama-2'], 'count': 1})
                self.assertFalse(ol_rep2.exists())
                self.assertTrue(ol_default.is_dir())

                # Removing already-deleted dir fails with 400 (UsageError).
                conn.request('POST', '/api/instances/ollama/remove', body=json.dumps({'instance': '2'}), headers=hdrs)
                r = conn.getresponse()
                r.read()
                self.assertEqual(r.status, 400)

                # Prune default (replicas_only=True) removes stopped replicas incl. corrupt, keeps defaults & running.
                conn.request('POST', '/api/instances/prune', body=b'', headers=hdrs)
                r = conn.getresponse()
                prune_payload = json.loads(r.read().decode('utf-8'))
                self.assertEqual(r.status, 200)
                self.assertEqual(prune_payload['ok'], True)
                self.assertEqual(prune_payload['removed'], ['ollama-3', 'qwen-flash-r1'])
                self.assertEqual(prune_payload['count'], 2)
                self.assertFalse(ol_corrupt.exists())
                self.assertFalse(qw_rep1.exists())
                self.assertTrue(ol_default.is_dir())
                self.assertTrue(qw_default.is_dir())
                self.assertTrue(comfy_running.is_dir())
                self.assertTrue(unknown_dir.is_dir())

                # Prune with unknown service -> 400.
                conn.request('POST', '/api/instances/prune', body=json.dumps({'service': 'unknown-svc'}), headers=hdrs)
                r = conn.getresponse()
                r.read()
                self.assertEqual(r.status, 400)

                # Prune with replicas_only=False and service='ollama' removes stopped default ollama only.
                conn.request(
                    'POST',
                    '/api/instances/prune',
                    body=json.dumps({'replicas_only': False, 'service': 'ollama'}),
                    headers=hdrs,
                )
                r = conn.getresponse()
                prune2 = json.loads(r.read().decode('utf-8'))
                self.assertEqual(r.status, 200)
                self.assertEqual(prune2, {'ok': True, 'removed': ['ollama'], 'count': 1})
                self.assertFalse(ol_default.exists())
                self.assertTrue(qw_default.is_dir())
                self.assertTrue(comfy_running.is_dir())
                self.assertTrue(unknown_dir.is_dir())
            finally:
                conn.close()
                ro_conn.close()

    def test_accounts_usage_endpoint_and_caching(self):
        cfg_dir = self.user_dir / '.config' / 'colab-cli'
        cfg_dir.mkdir(parents=True, exist_ok=True)
        accounts_json = cfg_dir / 'accounts.json'
        accounts_json.write_text(
            json.dumps({
                'active': 'free',
                'accounts': {
                    'main': {'email': 'main@example.com', 'drive_account': 'main'},
                    'free': {'email': 'free@example.com', 'drive_account': 'main'},
                },
            }),
            encoding='utf-8',
        )
        mode_flag = self.tmp / 'plain-mode'
        calls_log = self.tmp / 'usage-calls.jsonl'
        colab_stub = self.bin_dir / 'colab'
        colab_stub.write_text(
            f'''#!{sys.executable}
import json
from pathlib import Path
import sys

calls_path = Path({str(calls_log)!r})
with calls_path.open('a', encoding='utf-8') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')

args = sys.argv[1:]
plain = Path({str(mode_flag)!r}).exists()
if not plain and args[:3] == ['account', 'status', '--json']:
    print(json.dumps([
        {{
            'account': 'main',
            'active': False,
            'email': 'main@example.com',
            'usage': {{'balance': 617.7, 'rate_hourly': 2.5, 'assignments_count': 1}},
            'assignments': [{{'name': 'qwen-flash', 'endpoint': 'ep-1', 'hardware': 'T4', 'shape': 'Standard'}}],
            'error': None,
        }},
        {{
            'account': 'free',
            'active': True,
            'email': 'free@example.com',
            'usage': {{'balance': 0.0, 'rate_hourly': 0.08, 'assignments_count': 1}},
            'assignments': [{{'name': 'r2-migrate', 'endpoint': 'ep-2', 'hardware': 'CPU', 'shape': 'Standard'}}],
            'error': None,
        }},
    ]))
    sys.exit(0)
if args == ['usage']:
    print('Current balance: 120.50 compute units')
    print('Usage rate: 1.25/hr')
    print('Active assignments: 1')
    sys.exit(0)
sys.exit(2)
''',
            encoding='utf-8',
        )
        colab_stub.chmod(0o755)

        srv, port = self._start_console(read_only=False)
        cookie, _csrf = self._authenticate(srv, port)
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            # 1. Initial bootstrap returns immediately without invoking `colab` CLI.
            conn.request('GET', '/api/bootstrap', headers={'Cookie': cookie})
            r = conn.getresponse()
            boot0 = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            self.assertIsNone(boot0['accounts']['usage_summary'])
            self.assertFalse(calls_log.exists())

            # 2. First GET /api/accounts/usage runs `colab account status --json` and computes metrics + totals.
            conn.request('GET', '/api/accounts/usage', headers={'Cookie': cookie})
            r = conn.getresponse()
            ov1 = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            by_name = {a['name']: a for a in ov1['accounts']}
            self.assertEqual(by_name['main']['quota']['usage']['balance'], 617.7)
            self.assertEqual(by_name['main']['quota']['usage']['estimated_hours'], 247.1)
            self.assertEqual(by_name['main']['quota']['assignments'][0]['hardware'], 'T4')
            self.assertEqual(by_name['free']['quota']['usage']['tier'], 'free')
            self.assertEqual(ov1['usage_summary']['source'], 'colab-ext')
            self.assertEqual(ov1['usage_summary']['totals']['total_balance'], 617.7)
            self.assertEqual(ov1['usage_summary']['totals']['total_rate_hourly'], 2.58)
            self.assertEqual(ov1['usage_summary']['totals']['total_assignments'], 2)
            self.assertEqual(len(calls_log.read_text(encoding='utf-8').splitlines()), 1)

            # 3. Cached read on /api/accounts/usage & /api/bootstrap does not respawn CLI.
            conn.request('GET', '/api/accounts/usage', headers={'Cookie': cookie})
            r = conn.getresponse()
            ov2 = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            self.assertEqual(ov2['usage_summary']['totals']['total_assignments'], 2)
            self.assertEqual(len(calls_log.read_text(encoding='utf-8').splitlines()), 1)

            conn.request('GET', '/api/bootstrap', headers={'Cookie': cookie})
            r = conn.getresponse()
            boot1 = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            self.assertIsNotNone(boot1['accounts']['usage_summary'])
            self.assertEqual(len(calls_log.read_text(encoding='utf-8').splitlines()), 1)

            # 4. ?refresh=1 forces fresh CLI query.
            conn.request('GET', '/api/accounts/usage?refresh=1', headers={'Cookie': cookie})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 200)
            self.assertEqual(len(calls_log.read_text(encoding='utf-8').splitlines()), 2)

            # 5. Plain single-account fallback (`colab usage`) works without `accounts.json` / `colab-ext`.
            accounts_json.unlink()
            mode_flag.touch()
            conn.request('GET', '/api/accounts/usage?refresh=1', headers={'Cookie': cookie})
            r = conn.getresponse()
            plain_ov = json.loads(r.read().decode('utf-8'))
            self.assertEqual(r.status, 200)
            self.assertEqual(plain_ov['accounts'], [])
            self.assertEqual(plain_ov['usage_summary']['source'], 'colab-usage')
            self.assertEqual(plain_ov['legacy_quota']['usage']['balance'], 120.5)
            self.assertEqual(plain_ov['legacy_quota']['usage']['estimated_hours'], 96.4)
            self.assertEqual(plain_ov['legacy_quota']['usage']['assignments_count'], 1)
        finally:
            conn.close()

    def test_static_frontend_offline_and_trusted_types_hygiene(self):
        static_dir = SRC / 'colabllm' / 'web_ui' / 'static'
        index_html = (static_dir / 'index.html').read_text(encoding='utf-8')
        styles_css = (static_dir / 'styles.css').read_text(encoding='utf-8')
        copilot_css = (static_dir / 'copilot.css').read_text(encoding='utf-8')
        app_js = (static_dir / 'app.js').read_text(encoding='utf-8')
        copilot_js = (static_dir / 'copilot.js').read_text(encoding='utf-8')
        copilot_studios_js = (static_dir / 'copilot_studios.js').read_text(encoding='utf-8')

        for js_name, js_src in (('app.js', app_js), ('copilot.js', copilot_js), ('copilot_studios.js', copilot_studios_js)):
            for forbidden_sink in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write'):
                self.assertNotIn(forbidden_sink, js_src, f'{js_name} must not contain {forbidden_sink}')

        self.assertIn('id="logs-copy-btn"', index_html)
        self.assertIn('id="accounts-usage-refresh-btn"', index_html)
        self.assertIn('role="region"', index_html)
        self.assertIn('command="--copy"', index_html)
        self.assertIn('commandfor="wb-cmd-preview"', index_html)
        self.assertIn('commandfor="logs-viewer"', index_html)
        self.assertIn('command="--copy-token"', index_html)
        self.assertIn('command="--copy-curl"', index_html)
        self.assertIn('command="--copy-py"', index_html)
        self.assertIn('commandfor="connect-snippet"', index_html)
        self.assertIn('<span class="copy-tip" role="status"></span>', index_html)
        self.assertIn('id="view-copilot"', index_html)
        self.assertIn('id="copilot-drawer"', index_html)
        self.assertIn('id="ck-surface-tablist"', index_html)
        self.assertIn('inset: calc(4.5rem + var(--toast-offset, 0px)) 1.25rem auto auto;', styles_css)
        self.assertIn('.badge.is-busy::before', styles_css)
        self.assertIn('flex-shrink: 0;', styles_css)
        self.assertIn('@container (width < 580px)', styles_css)
        self.assertIn('field-sizing: content', copilot_css)
        self.assertIn('@supports (position-area: block-end)', copilot_css)
        self.assertIn('@supports (animation-timeline: scroll())', copilot_css)
        self.assertIn('::-webkit-progress-value', copilot_css)
        self.assertIn('::-moz-progress-bar', copilot_css)
        self.assertIn('popovertargetaction', app_js)
        self.assertIn('createUrlCopyRow', app_js)
        self.assertIn('reconcileTasksAndInstances', app_js)
        self.assertIn('inferLifecycleTaskHint', app_js)
        self.assertIn('initCopilotStudio', app_js)

        self.assertNotRegex(index_html, r'(?i)(src|href)=["\']https?://')
        self.assertNotRegex(styles_css, r'(?i)(@import|url\()\s*["\']?https?://')
        self.assertNotRegex(copilot_css, r'(?i)(@import|url\()\s*["\']?https?://')
        self.assertRegex(
            styles_css,
            r'\[hidden\]:not\(\[hidden="until-found"\]\)\s*\{\s*display:\s*none\s*!important;',
        )

    def test_copilot_target_dropdown_filters_running_instances_only(self):
        if not shutil.which('node'):
            self.skipTest('node not available')

        copilot_js = SRC / 'colabllm' / 'web_ui' / 'static' / 'copilot.js'
        js_script = r"""
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

class NodeEl {
  constructor(tag) {
    this.tagName = String(tag || '').toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.dataset = {};
    this.style = { setProperty() {} };
    this.classList = { toggle: () => false };
    this.className = '';
    this.textContent = '';
    this._value = '';
    this.hidden = false;
    this.disabled = false;
    this.selected = false;
    this.parentNode = null;
  }
  get options() { return this.children.filter((c) => c.tagName === 'OPTION'); }
  get selectedOptions() { return this.options.filter((o) => o.value === this.value); }
  get value() {
    if (this.tagName === 'SELECT') {
      if (this.options.some((o) => o.value === this._value)) return this._value;
      const selOpt = this.options.find((o) => o.selected) || this.options[0];
      return selOpt ? selOpt.value : '';
    }
    return this._value;
  }
  set value(v) { this._value = String(v ?? ''); }
  setAttribute(k, v) {
    this.attributes.set(k, String(v));
    if (k === 'value') this._value = String(v);
    if (k === 'hidden') this.hidden = true;
    if (k === 'disabled') this.disabled = true;
    if (k === 'selected') this.selected = true;
    if (k === 'class') this.className = String(v);
  }
  removeAttribute(k) {
    this.attributes.delete(k);
    if (k === 'hidden') this.hidden = false;
    if (k === 'disabled') this.disabled = false;
    if (k === 'selected') this.selected = false;
  }
  getAttribute(k) { return this.attributes.get(k) ?? null; }
  appendChild(c) {
    if (c && typeof c === 'object') {
      c.parentNode = this;
      this.children.push(c);
    }
    return c;
  }
  replaceChildren(...nodes) {
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    for (const n of nodes) this.appendChild(n);
    if (this.tagName === 'SELECT' && !this.options.some((o) => o.value === this._value)) {
      const selOpt = this.options.find((o) => o.selected) || this.options[0];
      this._value = selOpt ? selOpt.value : '';
    }
  }
  remove() {
    if (this.parentNode) {
      this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
      this.parentNode = null;
    }
  }
  addEventListener() {}
  querySelector(sel) {
    if (sel === 'option[value=""]') return this.options.find((o) => o.value === '') || null;
    return null;
  }
  querySelectorAll() { return []; }
  closest() { return null; }
}

const ids = new Map();
for (const id of [
  'ck-target',
  'ck-drawer-target',
  'ck-model',
  'ck-status-pill',
  'ck-proxy-url-hint',
  'ck-capabilities-summary',
  'ck-welcome-title',
  'ck-drawer-model-badge',
]) {
  const tag = id.includes('target') ? 'select' : id === 'ck-model' ? 'input' : 'span';
  ids.set(id, new NodeEl(tag));
}

globalThis.window = { location: { origin: 'http://127.0.0.1:8765' } };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.document = {
  getElementById: (id) => ids.get(id) || null,
  querySelector: () => null,
  createElement: (t) => new NodeEl(t),
  createTextNode: (s) => ({ textContent: String(s) }),
  createDocumentFragment: () => new NodeEl('frag'),
  body: new NodeEl('body'),
};

function el(tag, attrs = null, ...children) {
  const node = new NodeEl(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'className') node.className = String(v);
      else if (k === 'textContent') node.textContent = String(v);
      else if (v === true) node.setAttribute(k, '');
      else node.setAttribute(k, String(v));
    }
  }
  for (const ch of children) {
    if (ch === null || ch === undefined || ch === false) continue;
    if (typeof ch === 'string' || typeof ch === 'number') node.textContent += String(ch);
    else node.appendChild(ch);
  }
  return node;
}

const { initCopilotStudio } = await import(pathToFileURL(process.env.COPILOT_JS_PATH).href);

const appState = {
  services: [
    { name: 'r2-migrate', api_profile: { default_model: 'r2-migrate', primary_surface: 'specialized' } },
    { name: 'qwen-flash', api_profile: { default_model: 'qwen3.8-flash', primary_surface: 'chat' } },
    { name: 'ollama', api_profile: { default_model: 'qwen3:8b', primary_surface: 'chat' } },
    { name: 'indextts', api_profile: { default_model: 'indextts-2.5', primary_surface: 'audio' } },
    { name: 'flux-klein', api_profile: { default_model: 'flux-3-image-klein', primary_surface: 'image' } },
  ],
  instances: [
    { session_name: 'r2-migrate', service: 'r2-migrate', running: true, ready: true, stopping: false, state_label: 'READY', url: 'https://r2-migrate.example.com' },
    { session_name: 'qwen-flash', service: 'qwen-flash', running: true, ready: false, stopping: false, state_label: 'STARTING', url: null },
    { session_name: 'ollama', service: 'ollama', running: true, ready: false, stopping: true, state_label: 'STOPPING', url: 'https://ollama.example.com' },
    { session_name: 'indextts', service: 'indextts', running: false, ready: false, stopping: false, state_label: 'STOPPED', url: 'https://indextts.example.com' },
    { session_name: 'flux-klein', service: 'flux-klein', running: false, ready: false, stopping: false, state_label: 'ERROR', url: null },
  ],
};

const ctrl = initCopilotStudio({
  appState,
  el,
  toast() {},
  copyText() {},
  switchView() {},
});

const ckTarget = ids.get('ck-target');
const ckDrawerTarget = ids.get('ck-drawer-target');
const ckModel = ids.get('ck-model');
const ckStatusPill = ids.get('ck-status-pill');

// 1. Only READY and STARTING (non-stopping) running instances appear
assert.deepEqual(ckTarget.options.map((o) => o.value), ['r2-migrate', 'qwen-flash']);
assert.deepEqual(ckDrawerTarget.options.map((o) => o.value), ['r2-migrate', 'qwen-flash']);
assert.equal(ckTarget.value, 'r2-migrate');
assert.equal(ckDrawerTarget.value, 'r2-migrate');
assert.equal(ckModel.value, 'r2-migrate');

// 2. When r2-migrate stops, syncCopilotState updates dropdowns and falls back to qwen-flash
appState.instances[0].running = false;
appState.instances[0].ready = false;
appState.instances[0].state_label = 'STOPPED';
ctrl.syncCopilotState();
assert.deepEqual(ckTarget.options.map((o) => o.value), ['qwen-flash']);
assert.deepEqual(ckDrawerTarget.options.map((o) => o.value), ['qwen-flash']);
assert.equal(ckTarget.value, 'qwen-flash');
assert.equal(ckDrawerTarget.value, 'qwen-flash');
assert.equal(ckModel.value, 'qwen3.8-flash');

// 3. When all running instances stop, dropdown shows empty placeholder and resets model + pill
appState.instances[1].running = false;
appState.instances[1].state_label = 'STOPPED';
ctrl.syncCopilotState();
assert.deepEqual(ckTarget.options.map((o) => ({ value: o.value, text: o.textContent })), [{ value: '', text: '暂无运行中实例' }]);
assert.deepEqual(ckDrawerTarget.options.map((o) => ({ value: o.value, text: o.textContent })), [{ value: '', text: '暂无运行中实例' }]);
assert.equal(ckModel.value, '');
assert.equal(ckStatusPill.textContent, '暂无运行中实例');

// 4. openServiceInCopilot for a stopped service while another service is running
appState.instances[0].running = true;
appState.instances[0].ready = true;
appState.instances[0].state_label = 'READY';
ctrl.syncCopilotState();
ctrl.openServiceInCopilot('indextts');
assert.deepEqual(ckTarget.options.filter((o) => !o.hidden).map((o) => o.value), ['r2-migrate']);
assert.deepEqual(ckDrawerTarget.options.filter((o) => !o.hidden).map((o) => o.value), ['r2-migrate']);
assert.equal(ckTarget.value, '');
assert.equal(ckStatusPill.textContent, 'indextts (未启动实例)');
assert.equal(ckModel.value, 'indextts-2.5');

// Once indextts starts running, syncCopilotState promotes it automatically
appState.instances[3].running = true;
appState.instances[3].ready = true;
appState.instances[3].state_label = 'READY';
ctrl.syncCopilotState();
assert.deepEqual(ckTarget.options.filter((o) => !o.hidden).map((o) => o.value), ['r2-migrate', 'indextts']);
assert.equal(ckTarget.value, 'indextts');
assert.equal(ckDrawerTarget.value, 'indextts');
"""
        proc = subprocess.run(
            ['node', '--input-type=module', '-e', js_script],
            env={**os.environ, 'COPILOT_JS_PATH': str(copilot_js)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f'Node copilot test failed (rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}',
        )


if __name__ == '__main__':
    unittest.main()
