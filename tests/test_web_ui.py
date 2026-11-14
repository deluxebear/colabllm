"""Tests for the zero-dependency colabllm web console CLI, schema introspection, security, and HTTP/SSE API."""
from __future__ import annotations

import http.client
import http.cookies
import http.server
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
LIB = SRC / 'colabllm' / 'lib'
for _p in (str(SRC), str(LIB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cli_support import CliError, UsageError  # noqa: E402
from colabllm import web as colabllm_web  # noqa: E402
from colabllm.web_ui import introspect, server as web_server  # noqa: E402


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


class WebServerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home_dir = self.tmp / 'colabllm_home'
        self.xdg_dir = self.tmp / 'xdg_config'
        self.user_dir = self.tmp / 'user_home'
        for d in (self.home_dir, self.xdg_dir, self.user_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._env_patch = patch.dict(
            os.environ,
            {
                'COLABLLM_HOME': str(self.home_dir),
                'XDG_CONFIG_HOME': str(self.xdg_dir),
                'HOME': str(self.user_dir),
            },
            clear=False,
        )
        self._env_patch.start()
        self._servers: list[tuple[web_server.WebConsoleServer, threading.Thread]] = []

    def tearDown(self):
        for srv, th in reversed(self._servers):
            srv.shutdown()
            th.join(timeout=2.0)
            srv.server_close()
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
            unsloth_inst = next(i for i in boot['instances'] if i['service'] == 'unsloth-studio')
            self.assertTrue(unsloth_inst['has_app_login'])
            self.assertFalse(unsloth_inst['has_api_token'])
            self.assertFalse(unsloth_inst['has_basic_auth'])
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

    def test_static_frontend_offline_and_trusted_types_hygiene(self):
        static_dir = SRC / 'colabllm' / 'web_ui' / 'static'
        index_html = (static_dir / 'index.html').read_text(encoding='utf-8')
        styles_css = (static_dir / 'styles.css').read_text(encoding='utf-8')
        app_js = (static_dir / 'app.js').read_text(encoding='utf-8')

        for forbidden_sink in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write'):
            self.assertNotIn(forbidden_sink, app_js)

        self.assertNotRegex(index_html, r'(?i)(src|href)=["\']https?://')
        self.assertNotRegex(styles_css, r'(?i)(@import|url\()\s*["\']?https?://')


if __name__ == '__main__':
    unittest.main()
