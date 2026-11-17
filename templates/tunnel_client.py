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

CLOUDFLARED_VERSION = '2026.11.0'
CLOUDFLARED_SHA256 = {
    'amd64': '51f7c68b57ed7269164075d7e789efce5b59355982f06392deeb037d982a51fd',
    'arm64': 'abe54a006acf9faf2d19ee9d953cb0d0804e92f59c5382a162e57b84d086f56f',
}


def _write_private_text(path, content):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _touch_private_log(path):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    os.close(fd)


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _check_cached_binary(destination, artifact, version, cache_dir):
    if not destination.exists():
        return None
    if not cache_dir:
        return 'hit'
    manifest = Path(cache_dir) / 'manifests' / f'{artifact}.json'
    if not manifest.is_file():
        return 'miss'
    try:
        meta = json.loads(manifest.read_text(encoding='utf-8'))
        if (
            meta.get('artifact') == artifact
            and meta.get('version') == version
            and meta.get('sha256') == _file_sha256(destination)
        ):
            return 'hit'
    except (OSError, ValueError):
        pass
    destination.unlink(missing_ok=True)
    return 'corrupt_rebuilt'


def _write_manifest(cache_dir, artifact, version, destination):
    if not cache_dir:
        return
    manifest_dir = Path(cache_dir) / 'manifests'
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest_dir / f'{artifact}.json'
    tmp = manifest.with_name(f'.{artifact}.{os.getpid()}.tmp')
    payload = {
        'artifact': artifact,
        'version': version,
        'sha256': _file_sha256(destination),
        'bin_path': str(destination),
        'verified_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    tmp.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, manifest)


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


def install_frpc(destination, version, *, cache_dir=None):
    destination = Path(destination)
    cache_state = _check_cached_binary(destination, 'frpc', version, cache_dir)
    if cache_state == 'hit':
        if cache_dir:
            print(f'CT_CACHE_EVENT artifact=frpc status=hit', flush=True)
        return str(destination)
    status_label = cache_state or 'miss'
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
            partial=destination.with_name(f'{destination.name}.{os.getpid()}.partial')
            try:
                with tar.extractfile(entry) as source, open(partial, 'wb') as out_bin:
                    shutil.copyfileobj(source, out_bin)
                partial.chmod(0o755)
                os.replace(partial, destination)
            finally:
                partial.unlink(missing_ok=True)
    _write_manifest(cache_dir, 'frpc', version, destination)
    if cache_dir:
        print(f'CT_CACHE_EVENT artifact=frpc status={status_label}', flush=True)
    return str(destination)


def install_cloudflared(destination, *, cache_dir=None, fetch_fn=None):
    destination = Path(destination)
    cf_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine(), 'amd64')
    url = f'https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}/cloudflared-linux-{cf_arch}'
    expected_sha = CLOUDFLARED_SHA256.get(cf_arch)
    if not expected_sha:
        raise ValueError(f'Unsupported cloudflared architecture: {platform.machine()}')
    if cache_dir:
        cache_state = _check_cached_binary(destination, 'cloudflared', CLOUDFLARED_VERSION, cache_dir)
        if cache_state == 'hit':
            print('CT_CACHE_EVENT artifact=cloudflared status=hit', flush=True)
            return str(destination)
        status_label = cache_state or 'miss'
        if destination.exists() and _file_sha256(destination) != expected_sha:
            destination.unlink(missing_ok=True)
            status_label = 'corrupt_rebuilt'
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f'{destination.name}.{os.getpid()}.partial')
        try:
            with urllib.request.urlopen(url, timeout=60) as src, open(partial, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            if _file_sha256(partial) != expected_sha:
                raise ValueError('cloudflared checksum verification failed')
            partial.chmod(0o755)
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
        _write_manifest(cache_dir, 'cloudflared', CLOUDFLARED_VERSION, destination)
        print(f'CT_CACHE_EVENT artifact=cloudflared status={status_label}', flush=True)
        return str(destination)
    if fetch_fn is not None:
        return fetch_fn(str(destination), url)
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f'{destination.name}.{os.getpid()}.partial')
        try:
            with urllib.request.urlopen(url, timeout=60) as src, open(partial, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            if _file_sha256(partial) != expected_sha:
                raise ValueError('cloudflared checksum verification failed')
            partial.chmod(0o755)
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
    return str(destination)


def run_tunnel(ctx):
    if not ctx.get('AUTH_OK'):
        raise RuntimeError('Auth layer not ready; refusing to expose the service')
    work=Path(ctx.get('WORK') or os.environ.get('CT_WORK_DIR') or '/content/ct')
    work.mkdir(parents=True, exist_ok=True)
    bin_dir_explicit = ctx.get('BIN_DIR') or os.environ.get('CT_BIN_DIR') or ''
    bin_dir = Path(bin_dir_explicit or '/usr/local/bin')
    cache_dir = ctx.get('CACHE_DIR') or os.environ.get('CT_CACHE_DIR') or ''
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
    if ctx.get('ct_stop_marked_proc') is not None:
        stop_fn = ctx['ct_stop_marked_proc']
        stop_fn('tunnel-process')
    elif marker.exists():
        previous=json.loads(marker.read_text())
        if previous.get('start_time') and identity(previous['pid'])==previous['start_time']:
            os.kill(previous['pid'],signal.SIGTERM)
            for _ in range(50):
                if identity(previous['pid']) is None:
                    break
                time.sleep(.1)
    def spawn(name,args):
        _touch_private_log(work/(name+'.log'))
        proc=ctx['ct_spawn'](name,args)
        _write_private_text(marker, json.dumps(dict(pid=proc.pid,start_time=identity(proc.pid))))
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
        _write_private_text(ca_path, os.environ['CT_FRP_CA_CERT'])
        config_path=work/'frpc.toml'
        _write_private_text(config_path, render_frpc(config,ctx['PUBLIC_PORT'],ca_path))
        frpc_name = f"frpc-{os.environ['CT_FRP_VERSION']}"
        frpc_dest = (bin_dir / frpc_name) if bin_dir_explicit else ((work / frpc_name) if (work / frpc_name).exists() else (bin_dir / frpc_name))
        binary=install_frpc(frpc_dest, os.environ['CT_FRP_VERSION'], cache_dir=cache_dir or None)
        result=subprocess.run([binary,'verify','-c',str(config_path)],capture_output=True)
        if result.returncode:
            raise RuntimeError('FRP client configuration validation failed')
        def start():
            return spawn('frpc',[binary,'-c',str(config_path)])
        key='frpc'
    else:
        binary=install_cloudflared(bin_dir / 'cloudflared', cache_dir=cache_dir or None, fetch_fn=ctx.get('ct_fetch_binary'))
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
            raise RuntimeError(f'{key} exited before registration; see {work}/{key}.log')
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
