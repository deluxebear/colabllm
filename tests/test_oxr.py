import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/colabllm/lib'))
spec = importlib.util.spec_from_file_location('oxr_auth_app', ROOT / 'templates/oxr_auth_app.py')
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)

TOKEN = 'sk-test-token'
SECRET = 'proxy-secret'


def multipart(fields, files=()):
    boundary = 'testboundary123'
    parts = []
    for key, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for key, name, data in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{name}"\r\n'
                     'Content-Type: application/pdf\r\n\r\n'.encode() + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        message = await receive()
        self.calls.append((scope['path'], message.get('body', b'')))
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})


def run(app, path, *, method='POST', body=b'', content_type='', headers=(), chunk=None):
    scope_headers = [(b'content-type', content_type.encode())] if content_type else []
    scope_headers += [(k.encode(), v.encode()) for k, v in headers]
    scope = {'type': 'http', 'path': path, 'method': method, 'headers': scope_headers}
    pieces = [body[i:i + chunk] for i in range(0, len(body), chunk)] if chunk and body else [body]
    queue = [{'type': 'http.request', 'body': p, 'more_body': i < len(pieces) - 1} for i, p in enumerate(pieces)]
    sent = []

    async def receive():
        return queue.pop(0) if queue else {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m['status'] for m in sent if m['type'] == 'http.response.start')
    payload = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
    return status, payload, sent


class OxrAuthMiddlewareTest(unittest.TestCase):
    def setUp(self):
        self.inner = Recorder()
        self.app = auth.OxrAuthMiddleware(self.inner, TOKEN, SECRET, health=lambda: {'dfine': True, 'vllm': True})

    def test_extract_multipart_and_urlencoded(self):
        body, ctype = multipart([('output_formats', 'markdown'), ('api_key', TOKEN)], [('file', 'a.pdf', b'%PDF api_key')])
        self.assertEqual(auth.extract_form_field(ctype, body), TOKEN)
        body, ctype = multipart([('output_formats', 'json')], [('api_key', 'x.pdf', b'not-a-field')])
        self.assertIsNone(auth.extract_form_field(ctype, body))
        encoded = urllib.parse.urlencode({'task_id': 't', 'api_key': TOKEN}).encode()
        self.assertEqual(auth.extract_form_field('application/x-www-form-urlencoded', encoded), TOKEN)
        self.assertIsNone(auth.extract_form_field('application/json', b'{"api_key": "x"}'))
        self.assertIsNone(auth.extract_form_field('multipart/form-data', body))

    def test_form_api_key_is_required_on_parse_routes(self):
        body, ctype = multipart([('api_key', TOKEN)], [('file', 'a.pdf', b'%PDF-1.7' * 1000)])
        status, _, _ = run(self.app, '/v1/parse/sync', body=body, content_type=ctype, chunk=1000)
        self.assertEqual(status, 200)
        self.assertEqual(self.inner.calls[-1], ('/v1/parse/sync', body))  # body replayed intact
        for wrong in ('wrong', ''):
            body, ctype = multipart([('api_key', wrong)], [('file', 'a.pdf', b'%PDF')])
            status, payload, sent = run(self.app, '/v1/parse/async', body=body, content_type=ctype)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(payload)['code'], 1107)
        self.assertEqual(len(self.inner.calls), 1)
        encoded = urllib.parse.urlencode({'task_id': 't', 'api_key': TOKEN}).encode()
        status, _, _ = run(self.app, '/v1/parse/async_check', body=encoded,
                           content_type='application/x-www-form-urlencoded')
        self.assertEqual(status, 200)

    def test_trusted_proxy_header_bypasses_form_check_only_with_exact_secret(self):
        body, ctype = multipart([('api_key', 'placeholder')], [('file', 'a.pdf', b'%PDF')])
        status, _, _ = run(self.app, '/v1/parse/sync', body=body, content_type=ctype,
                           headers=[('X-CT-Proxy-Auth', SECRET)])
        self.assertEqual(status, 200)
        status, _, _ = run(self.app, '/v1/parse/sync', body=body, content_type=ctype,
                           headers=[('X-CT-Proxy-Auth', SECRET + 'x')])
        self.assertEqual(status, 401)

    def test_fail_closed_without_configuration_and_size_limit(self):
        body, ctype = multipart([('api_key', '')], [('file', 'a.pdf', b'%PDF')])
        for token, secret in (('', SECRET), (TOKEN, '')):
            app = auth.OxrAuthMiddleware(Recorder(), token, secret)
            status, _, _ = run(app, '/v1/parse/sync', body=body, content_type=ctype,
                               headers=[('X-CT-Proxy-Auth', '')])
            self.assertEqual(status, 401)
        small = auth.OxrAuthMiddleware(Recorder(), TOKEN, SECRET, max_body=10)
        body, ctype = multipart([('api_key', TOKEN)], [('file', 'a.pdf', b'x' * 100)])
        status, _, _ = run(small, '/v1/parse/sync', body=body, content_type=ctype, chunk=8)
        self.assertEqual(status, 413)

    def test_other_routes_pass_through_and_health_redirect(self):
        status, _, _ = run(self.app, '/v1/demo/jobs', body=b'x', content_type='multipart/form-data; boundary=b')
        self.assertEqual(status, 200)
        status, _, _ = run(self.app, '/v1/parse/sync', method='GET')
        self.assertEqual(status, 200)
        status, payload, _ = run(self.app, '/health', method='GET')
        self.assertEqual((status, json.loads(payload)['ok']), (200, True))
        degraded = auth.OxrAuthMiddleware(Recorder(), TOKEN, SECRET, health=lambda: {'dfine': True, 'vllm': False})
        status, payload, _ = run(degraded, '/health', method='GET')
        self.assertEqual((status, json.loads(payload)['vllm']), (503, False))
        status, _, sent = run(self.app, '/', method='GET')
        self.assertEqual(status, 302)
        self.assertIn((b'location', b'/demo'), sent[0]['headers'])


class OxrNotebookAndCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nb = json.loads((ROOT / 'src/colabllm/services/oxr.ipynb').read_text())
        cls.cells = [''.join(c['source']) if isinstance(c['source'], list) else c['source'] for c in nb['cells']]

    def test_services_bind_loopback_and_avoid_colab_port(self):
        service = self.cells[3]
        self.assertIn('"--host", "127.0.0.1", "--port", str(DFINE_PORT)', service)
        self.assertIn('"--host", "127.0.0.1", "--port", str(VLLM_PORT)', service)
        self.assertIn('"--host", "127.0.0.1", "--port", str(SERVICE_PORT)', service)
        self.assertNotIn('0.0.0.0', service)
        setup = self.cells[2]
        self.assertIn('"CT_OXR_VLLM_PORT", "8090"', setup)
        self.assertIn('HF_XET_CHUNK_CACHE_SIZE_BYTES', setup)
        for proc in ('PROCS["dfine"]', 'PROCS["vllm"]', 'PROCS["service"]'):
            self.assertIn(proc, service)

    def test_caddy_form_branch_strips_trusted_header(self):
        caddy = self.cells[4]
        self.assertIn('header_up X-CT-Proxy-Auth {OXR_PROXY_SECRET}', caddy)
        branch = caddy[caddy.index('@ct_form_auth {{'):caddy.index('@cookie header')]
        self.assertIn('method POST', branch)
        self.assertIn('path /v1/parse/sync /v1/parse/async /v1/parse/async_check', branch)
        self.assertIn('header_up -X-CT-Proxy-Auth', branch)
        self.assertLess(caddy.index('handle @bearer'), caddy.index('handle @ct_form_auth'))

    def test_keepalive_requires_warmup_and_auth_before_ready(self):
        keepalive = self.cells[-1]
        for marker in ('/v1/parse/sync', 'oxr_verify_auth(CADDY_LOCAL)', 'ct_public_gate', 'save_cache'):
            self.assertLess(keepalive.index(marker), keepalive.index('print("CT_READY"'))
        self.assertIn('ct_watchdog(', keepalive)

    def test_cli_parse_request_and_output_names(self):
        sys.path.insert(0, str(ROOT / 'src/colabllm/lib'))
        mod_spec = importlib.util.spec_from_file_location('oxr_cli', ROOT / 'src/colabllm/services/oxr.py')
        cli = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(cli)
        self.assertEqual(cli.SERVICE.gpu, 'A100')
        self.assertEqual(cli.parse_formats('md, json,markdown'), ['markdown', 'json'])
        with self.assertRaises(Exception):
            cli.parse_formats('zip')
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / 'doc.pdf'
            doc.write_bytes(b'%PDF-1.7')
            body, ctype = cli.build_parse_request(doc, ['markdown'], keep_header_footer=True)
            self.assertEqual(auth.extract_form_field(ctype, body, 'output_formats'), 'markdown')
            self.assertEqual(auth.extract_form_field(ctype, body, 'api_key'), 'bearer')
            self.assertIn(b'filename="doc.pdf"', body)
            other = Path(tmp) / 'sub'
            other.mkdir()
            (other / 'doc.png').write_bytes(b'png')
            files = cli.collect_inputs([str(doc), str(other)])
            self.assertEqual(sorted(cli.output_names(files).values()), ['doc', 'doc-2'])


if __name__ == '__main__':
    unittest.main()
