"""Self-contained Colab transport cell; injected into every service notebook."""
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def render_frpc(config, local_port, ca_path):
    """TOML strings use JSON quoting, never shell interpolation."""
    return f'''serverAddr = {json.dumps(config['server_ip'])}
serverPort = {int(config['server_port'])}
loginFailExit = false
auth.method = "token"
auth.token = {json.dumps(config['token'])}
auth.additionalScopes = {json.dumps(config.get('auth_scopes',['HeartBeats','NewWorkConns']))}
transport.tls.enable = true
transport.tls.trustedCaFile = {json.dumps(str(ca_path))}
transport.tls.serverName = {json.dumps(config.get('server_name') or config['server_ip'])}
log.to = "console"
log.level = "info"
[[proxies]]
name = {json.dumps(config['proxy_name'])}
type = "tcp"
localIP = "127.0.0.1"
localPort = {int(local_port)}
remotePort = {int(config['remote_port'])}
'''


def install_frpc(destination, version):
    if destination.exists():
        return str(destination)
    arch={'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
    if not arch:
        raise ValueError('Unsupported FRP client architecture')
    if not re.fullmatch(r'\d+\.\d+\.\d+',version) or tuple(map(int,version.split('.')))<(0,61,0):
        raise ValueError('Unsupported FRP client version; expected stable >= 0.61.0')
    asset=f'frp_{version}_linux_{arch}.tar.gz'
    base=f'https://github.com/fatedier/frp/releases/download/v{version}/'
    with tempfile.TemporaryDirectory() as tmp:
        archive=Path(tmp)/asset
        with urllib.request.urlopen(base+asset,timeout=60) as response, open(archive,'wb') as out:
            shutil.copyfileobj(response,out)
        checks=urllib.request.urlopen(base+'frp_sha256_checksums.txt',timeout=60).read().decode()
        expected=next((line.split()[0] for line in checks.splitlines()
                       if line.split() and line.split()[-1].lstrip('*')==asset),None)
        if expected is None or hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:
            raise ValueError('FRP client checksum verification failed')
        with tarfile.open(archive) as tar:
            entry=tar.getmember(f'frp_{version}_linux_{arch}/frpc')
            if not entry.isfile():
                raise ValueError('Invalid FRP client binary')
            destination.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(entry) as source:
                destination.write_bytes(source.read())
            destination.chmod(0o755)
    return str(destination)


def run_tunnel(ctx):
    if not ctx.get('AUTH_OK'):
        raise RuntimeError('Auth layer not ready; refusing to expose the service')
    work=Path(ctx['WORK'])
    transport=os.environ.get('CT_TRANSPORT','cloudflare')
    if transport not in ('cloudflare','frp'):
        raise ValueError('Unsupported tunnel transport')
    # Stop only the tunnel previously started by this notebook (PID + proc
    # start time prevent accidental termination after PID reuse).
    marker=work/'tunnel-process.json'
    def identity(pid):
        try:
            stat=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
            return stat[19]
        except OSError:
            return None
    if marker.exists():
        previous=json.loads(marker.read_text())
        if previous.get('start_time') and identity(previous['pid'])==previous['start_time']:
            os.kill(previous['pid'],signal.SIGTERM)
            for _ in range(50):
                if identity(previous['pid']) is None:
                    break
                time.sleep(.1)
    def spawn(name,args):
        (work/(name+'.log')).write_text('')
        proc=ctx['ct_spawn'](name,args)
        marker.write_text(json.dumps(dict(pid=proc.pid,start_time=identity(proc.pid))))
        return proc

    if transport=='frp':
        config={key:os.environ['CT_FRP_'+key.upper()] for key in
                ('server_ip','server_port','remote_port','proxy_name','token')}
        config['server_name']=os.environ.get('CT_FRP_SERVER_NAME') or config['server_ip']
        config['auth_scopes']=json.loads(os.environ.get('CT_FRP_AUTH_SCOPES') or '["HeartBeats","NewWorkConns"]')
        public_host=ctx['PUBLIC_HOST']
        if not public_host:
            raise ValueError('FRP requires a public hostname')
        ca_path=work/'frp-ca.crt'
        ca_path.write_text(os.environ['CT_FRP_CA_CERT'])
        config_path=work/'frpc.toml'
        config_path.touch(mode=0o600,exist_ok=True)
        config_path.chmod(0o600)
        config_path.write_text(render_frpc(config,ctx['PUBLIC_PORT'],ca_path))
        binary=install_frpc(work/('frpc-'+os.environ['CT_FRP_VERSION']),os.environ['CT_FRP_VERSION'])
        result=subprocess.run([binary,'verify','-c',str(config_path)],capture_output=True)
        if result.returncode:
            raise RuntimeError('FRP client configuration validation failed')
        def start():
            return spawn('frpc',[binary,'-c',str(config_path)])
        key='frpc'
    else:
        cf_arch={'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine(),'amd64')
        binary=ctx['ct_fetch_binary']('/usr/local/bin/cloudflared',
            f'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{cf_arch}')
        def refresh_quick_url(proc_ref, wait_seconds=60):
            deadline_q=time.monotonic()+wait_seconds
            while time.monotonic()<deadline_q:
                if proc_ref.poll() is not None:
                    return None
                match_q=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',ctx['ct_tail']('cloudflared',100))
                if match_q:
                    new_url=match_q.group(0)
                    if new_url!=ctx.get('PUBLIC_URL'):
                        ctx['PUBLIC_URL']=new_url
                        print(f'CT_PUBLIC_URL={new_url}',flush=True)
                    return new_url
                time.sleep(1)
            return None
        ctx['ct_refresh_public_url']=refresh_quick_url
        def start():
            args=[binary,'tunnel','--no-autoupdate','--protocol','http2']
            if ctx['TUNNEL_TOKEN']:
                args+=['run','--token',ctx['TUNNEL_TOKEN']]
            else:
                args+=['--url',f'http://127.0.0.1:{ctx["PUBLIC_PORT"]}']
            proc_new=spawn('cloudflared',args)
            if not ctx['TUNNEL_TOKEN'] and ctx.get('PUBLIC_URL'):
                threading.Thread(target=refresh_quick_url,args=(proc_new,),daemon=True).start()
            return proc_new
        key='cloudflared'
    proc=start()
    ctx['PROCS'][key]=(proc,start)
    ctx['TUNNEL_PROCESS_KEY']=key
    deadline=time.monotonic()+180
    registered=False
    while time.monotonic()<deadline:
        if proc.poll() is not None:
            raise RuntimeError(f'{key} exited before registration; see /content/ct/{key}.log')
        log=ctx['ct_tail'](key,100)
        if transport=='frp':
            registered=registered or ('start proxy success' in log and config['proxy_name'] in log)
            if registered:
                url='https://'+ctx['PUBLIC_HOST']
                try:
                    with urllib.request.urlopen(url,timeout=5) as response:
                        if response.status<600:
                            break
                except urllib.error.HTTPError as error:
                    # A valid public TLS response proves the transport/site.
                    # 502 is expected while a large model is still loading;
                    # the service's keep-alive cell gates CT_READY on health.
                    code=error.code
                    error.close()
                    if code<600:
                        break
                except OSError:
                    pass
        elif ctx['TUNNEL_TOKEN'] and 'Registered tunnel connection' in log:
            url='https://'+ctx['PUBLIC_HOST']
            break
        elif not ctx['TUNNEL_TOKEN']:
            match=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',log)
            if match:
                url=match.group(0)
                break
        time.sleep(1)
    else:
        raise RuntimeError(f'{key} not ready; check tunnel logs, DNS, HTTPS and server ports')
    ctx['PUBLIC_URL']=url
    print(f'CT_PUBLIC_URL={url}',flush=True)
