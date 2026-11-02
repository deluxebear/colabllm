"""Real frps/frpc TLS tunnel + Caddy, auth, SSE, WebSocket and reconnection.

Run with COLABSVC_FRP_TEST_BIN pointing at a directory containing the pinned
frps, frpc and caddy binaries. No external server, DNS or privileged ports.
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'templates'))
import frp_server
from tunnel_client import render_frpc, run_tunnel


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'

    def log_message(self,*args):pass

    def do_GET(self):
        if self.headers.get('Authorization')!='Bearer test-secret':
            self.send_response(401);self.send_header('Content-Length','0');self.end_headers();return
        if self.path=='/ws':
            accept=base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key']+
                '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101);self.send_header('Upgrade','websocket')
            self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
            header=self.rfile.read(2);length=header[1]&127;mask=self.rfile.read(4);payload=self.rfile.read(length)
            value=bytes(v^mask[i%4] for i,v in enumerate(payload))
            self.wfile.write(bytes([0x81,len(value)])+value);self.wfile.flush();self.close_connection=True
            return
        if self.path=='/stream':
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Transfer-Encoding','chunked');self.end_headers()
            for value in [b'data: first\n\n',b'data: last\n\n']:
                self.wfile.write(f'{len(value):x}\r\n'.encode()+value+b'\r\n');self.wfile.flush();time.sleep(.05)
            self.wfile.write(b'0\r\n\r\n');self.wfile.flush();return
        body=json.dumps(dict(host=self.headers['Host'],proto=self.headers.get('X-Forwarded-Proto'))).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)


@unittest.skipUnless(os.environ.get('COLABSVC_FRP_TEST_BIN'),'requires real frps/frpc/caddy binaries')
class RealTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.bin=Path(os.environ['COLABSVC_FRP_TEST_BIN'])
        self.children=[];self.handles=[]
        self.addCleanup(self.shutdown)
        self.app=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=self.app.serve_forever,daemon=True).start()
        self.addCleanup(self.app.server_close);self.addCleanup(self.app.shutdown)
        self.server_port=free_port();self.remote_port=free_port();self.http_port=free_port()
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-keyout',str(self.root/'server.key'),'-out',str(self.root/'server.crt'),
                        '-subj','/CN=test-frps','-addext','subjectAltName=IP:127.0.0.1'],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        with patch.object(frp_server,'ETC',self.root):
            server_config=frp_server.server_toml('127.0.0.1','frp-secret',self.server_port)
        # Test-only unprivileged ports; production limits remain 20000–29999.
        server_config=server_config.replace('allowPorts = [{start = 20000, end = 29999}]',
                                            f'allowPorts = [{{single = {self.remote_port}}}]')
        (self.root/'frps.toml').write_text(server_config)
        client=dict(server_ip='127.0.0.1',server_port=self.server_port,remote_port=self.remote_port,
                    proxy_name='test-avatar',token='frp-secret')
        self.client_config=render_frpc(client,self.app.server_port,self.root/'server.crt')
        (self.root/'frpc.toml').write_text(self.client_config)
        self.start('frps',['-c',str(self.root/'frps.toml')])
        self.wait_port(self.server_port)
        self.client=self.start('frpc',['-c',str(self.root/'frpc.toml')])
        self.wait_port(self.remote_port)
        site=frp_server.caddy_site(f'https://127.0.0.1:{self.http_port}',self.remote_port,cache_assets=True)
        site=site.replace('    reverse_proxy',f'    tls {self.root}/server.crt {self.root}/server.key\n    reverse_proxy',1)
        self.tls_context=ssl.create_default_context(cafile=str(self.root/'server.crt'))
        (self.root/'Caddyfile').write_text('{\n admin off\n auto_https disable_redirects\n}\n'+site)
        subprocess.run([str(self.bin/'caddy'),'validate','--config',str(self.root/'Caddyfile'),'--adapter','caddyfile'],
                       stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)
        self.start('caddy',['run','--config',str(self.root/'Caddyfile'),'--adapter','caddyfile'])
        self.wait_port(self.http_port)

    def start(self,name,args):
        log=open(self.root/(name+f'-{len(self.children)}.log'),'ab');self.handles.append(log)
        proc=subprocess.Popen([str(self.bin/name),*args],stdout=log,stderr=log)
        self.children.append(proc);return proc

    def shutdown(self):
        for proc in reversed(self.children):
            if proc.poll() is None:proc.terminate()
        for proc in reversed(self.children):
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        for handle in self.handles:handle.close()

    def wait_port(self,port):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.2):return
            except OSError:time.sleep(.05)
        logs='\n'.join(p.read_text(errors='replace') for p in self.root.glob('*.log'))
        self.fail(f'Port {port} not listening: {logs}')

    def test_hashed_assets_cache_only_successful_authenticated_responses(self):
        base=f'https://127.0.0.1:{self.http_port}'
        asset='/assets/vendor-other-DMCJdr63.js'
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base+asset,timeout=5,context=self.tls_context)
        self.assertEqual(error.exception.code,401)
        self.assertIsNone(error.exception.headers.get('Cache-Control'))
        error.exception.close()
        for path,cached in [(asset,True),('/api/system_stats',False),('/assets/extension.js',False)]:
            request=urllib.request.Request(base+path,headers={'Authorization':'Bearer test-secret'})
            with urllib.request.urlopen(request,timeout=5,context=self.tls_context) as response:
                self.assertEqual('immutable' in response.headers.get('Cache-Control',''),cached)

    def test_auth_sse_websocket_and_client_reconnect(self):
        base=f'https://127.0.0.1:{self.http_port}'
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(base,timeout=5,context=self.tls_context)
        self.assertEqual(error.exception.code,401)
        error.exception.close()
        req=urllib.request.Request(base,headers={'Authorization':'Bearer test-secret'})
        with urllib.request.urlopen(req,timeout=5,context=self.tls_context) as response:
            body=json.load(response)
        self.assertEqual(body['proto'],'https')
        self.assertEqual(body['host'],f'127.0.0.1:{self.http_port}')
        with urllib.request.urlopen(urllib.request.Request(base+'/stream',headers={'Authorization':'Bearer test-secret'}),timeout=5,context=self.tls_context) as response:
            self.assertEqual(response.readline(),b'data: first\n')
            self.assertEqual(response.read(),b'\ndata: last\n\n')
        with socket.create_connection(('127.0.0.1',self.http_port),timeout=5) as raw, self.tls_context.wrap_socket(raw,server_hostname='127.0.0.1') as sock:
            sock.sendall((f'GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{self.http_port}\r\n'
                          'Authorization: Bearer test-secret\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
            stream=sock.makefile('rb')
            self.assertIn(b'101',stream.readline())
            while stream.readline()!=b'\r\n':pass
            value=b'hello';mask=b'1234';sock.sendall(b'\x81\x85'+mask+bytes(v^mask[i%4] for i,v in enumerate(value)))
            self.assertEqual(stream.read(7),b'\x81\x05hello');stream.close()
        self.client.terminate();self.client.wait(timeout=5)
        self.client=self.start('frpc',['-c',str(self.root/'frpc.toml')]);self.wait_port(self.remote_port)
        with urllib.request.urlopen(req,timeout=5,context=self.tls_context) as response:self.assertEqual(response.status,200)

    def test_wrong_token_and_untrusted_certificate_are_rejected(self):
        self.client.terminate();self.client.wait(timeout=5)
        for source in [self.client_config.replace('frp-secret','wrong-secret'),
                       self.client_config.replace(str(self.root/'server.crt'),'/etc/ssl/certs/ca-certificates.crt')]:
            (self.root/'bad.toml').write_text(source.replace('loginFailExit = false','loginFailExit = true'))
            result=subprocess.run([str(self.bin/'frpc'),'-c',str(self.root/'bad.toml')],capture_output=True,timeout=10)
            self.assertNotEqual(result.returncode,0)

    def test_colab_transport_cell_registers_and_uses_existing_auth_proxy(self):
        self.client.terminate();self.client.wait(timeout=5)
        import shutil
        shutil.copyfile(self.bin/'frpc',self.root/'frpc-0.61.2')
        (self.root/'frpc-0.61.2').chmod(0o755)
        def spawn(name,args):
            log=open(self.root/(name+'.log'),'ab');self.handles.append(log)
            proc=subprocess.Popen(args,stdout=log,stderr=log);self.children.append(proc);return proc
        env=dict(CT_TRANSPORT='frp',CT_FRP_SERVER_IP='127.0.0.1',CT_FRP_SERVER_PORT=str(self.server_port),
                 CT_FRP_REMOTE_PORT=str(self.remote_port),CT_FRP_PROXY_NAME='test-avatar',CT_FRP_TOKEN='frp-secret',
                 CT_FRP_CA_CERT=(self.root/'server.crt').read_text(),CT_FRP_VERSION='0.61.2')
        from unittest.mock import Mock
        ctx=dict(AUTH_OK=True,WORK=str(self.root),PUBLIC_PORT=self.app.server_port,TUNNEL_TOKEN='stale-cloudflare',
                 PUBLIC_HOST=f'127.0.0.1:{self.http_port}',PROCS={},ct_spawn=spawn,
                 ct_tail=lambda name,n:(self.root/(name+'.log')).read_text(errors='replace'),
                 ct_fetch_binary=Mock(side_effect=AssertionError('must not download Cloudflare')))
        urlopen=urllib.request.urlopen
        def trusted_open(url,**kw):return urlopen(url,context=self.tls_context,**kw)
        import contextlib,io
        with patch.dict(os.environ,env),patch('tunnel_client.urllib.request.urlopen',side_effect=trusted_open), \
             contextlib.redirect_stdout(io.StringIO()):
            run_tunnel(ctx)
        self.assertEqual(ctx['PUBLIC_URL'],f'https://127.0.0.1:{self.http_port}')
        self.assertEqual(ctx['TUNNEL_PROCESS_KEY'],'frpc')
        self.assertEqual((self.root/'frpc.toml').stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
