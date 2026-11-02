"""Standalone Linux/systemd provisioner, sent over SSH stdin (never a shell template)."""
import contextlib
import configparser
import fcntl
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import secrets
import shutil
import socket
import subprocess
import tarfile
import textwrap
import tempfile
import time
import urllib.request
from pathlib import Path

FRP_VERSION = '0.61.2'
CADDY_VERSION = '2.10.2'
ETC = Path('/etc/colabsvc-frp')
DATA = Path('/var/lib/colabsvc-frp')
BIN = Path('/usr/local/lib/colabsvc-frp')
UNITS = Path('/etc/systemd/system')
STANDARD_CADDY = Path('/etc/caddy/Caddyfile')


def config_argument(args):
    for index,arg in enumerate(args):
        if arg in ('-c','--config') and index+1<len(args):
            return args[index+1]
        if arg.startswith(('--config=','-c=')):
            return arg.split('=',1)[1]
    return None


def process_info(pid):
    path=Path('/proc')/str(pid)
    try:
        binary=str((path/'exe').resolve(strict=True))
        args=[x.decode() for x in (path/'cmdline').read_bytes().split(b'\0') if x]
        unit=re.search(r'/([^/]+\.service)(?:/|$)',(path/'cgroup').read_text())
        config=config_argument(args)
        cwd=(path/'cwd').resolve()
        return dict(binary=binary,config=str(cwd/config) if config and not Path(config).is_absolute() else config,
                    unit=unit.group(1) if unit else None,pid=int(pid),cwd=str(cwd))
    except (OSError,UnicodeError):
        return None


def listening_process(port):
    result=subprocess.run(['ss','-ltnpH',f'sport = :{int(port)}'],capture_output=True,text=True)
    pids=set(re.findall(r'pid=(\d+)',result.stdout))
    if len(pids)>1:
        raise ValueError(f'Multiple processes listen on port {port}; cannot select an existing service')
    return process_info(next(iter(pids))) if pids else None


def installed_services(kind):
    result=subprocess.run(['systemctl','list-unit-files','--type=service','--no-legend','--no-pager'],
                          capture_output=True,text=True)
    for line in result.stdout.splitlines():
        unit=line.split()[0]
        if kind not in unit.lower() or not unit.endswith('.service'):
            continue
        result=subprocess.run(['systemctl','show',unit,'--property=ExecStart,MainPID,WorkingDirectory','--no-pager'],
                              capture_output=True,text=True)
        pid=re.search(r'^MainPID=(\d+)$',result.stdout,re.M)
        if pid and int(pid.group(1)):
            info=process_info(int(pid.group(1)))
            if info:
                yield info
                continue
        command=re.search(r'argv\[\]=(.*?)(?:\s*;|$)',result.stdout)
        binary=re.search(r'\bpath=([^ ;]+)',result.stdout)
        if command and binary:
            args=shlex.split(command.group(1))
            working=re.search(r'^WorkingDirectory=(.*)$',result.stdout,re.M)
            cwd=working.group(1) if working and working.group(1) else '/'
            config=config_argument(args)
            yield dict(binary=binary.group(1),config=str(Path(cwd)/config) if config else None,
                       unit=unit,pid=None,cwd=cwd)


def read_frps_config(path):
    path=Path(path)
    if path.suffix.lower()=='.ini':
        parser=configparser.ConfigParser(interpolation=None)
        parser.read(path)
        common=parser['common']
        allowed=[]
        for item in common.get('allow_ports','').split(','):
            item=item.strip()
            if not item:continue
            parts=item.split('-',1)
            allowed.append(dict(start=int(parts[0]),end=int(parts[1])) if len(parts)==2 else dict(single=int(item)))
        tls={key:common[value] for key,value in [('certFile','tls_cert_file'),('keyFile','tls_key_file'),
                                                ('trustedCaFile','tls_trusted_ca_file')] if common.get(value)}
        return dict(bindPort=int(common.get('bind_port',7000)),
                    proxyBindAddr=common.get('proxy_bind_addr',common.get('bind_addr','0.0.0.0')),
                    auth=dict(method=common.get('authentication_method','token'),token=common.get('token','')),
                    transport=dict(tls=tls),allowPorts=allowed)
    try:
        import tomllib
    except ImportError:
        raise ValueError('Reading an existing FRP TOML requires server Python 3.11+') from None
    with path.open('rb') as source:
        return tomllib.load(source)


def discover_frps(port):
    if not port_free(port):
        info=listening_process(port)
        if not info or not Path(info['binary']).name.endswith('frps') or not info.get('config'):
            raise ValueError(f'Port {port} is occupied by a non-FRP or unidentifiable process')
        return info
    candidates=[]
    for info in installed_services('frp'):
        if Path(info['binary']).name.endswith('frps') and info.get('config'):
            if read_frps_config(info['config']).get('bindPort',7000)==port:
                candidates.append(info)
    if len(candidates)>1:
        raise ValueError('Multiple installed frps services match the requested port')
    return candidates[0] if candidates else None


def binary_version(binary):
    version=run([binary,'--version']).strip().removeprefix('v')
    if not re.fullmatch(r'\d+\.\d+\.\d+',version) or tuple(map(int,version.split('.')))<(0,61,0):
        raise ValueError('Existing frps must report a stable version >= 0.61.0')
    return version


def reuse_frps(info,ip,port):
    if not info.get('config'):
        raise ValueError('Cannot locate the existing frps configuration')
    config=read_frps_config(info['config'])
    if int(config.get('bindPort',7000))!=port:
        raise ValueError('Existing frps configuration uses a different port')
    if config.get('proxyBindAddr',config.get('bindAddr','0.0.0.0')) not in ('127.0.0.1','::1'):
        raise ValueError('Existing frps must bind proxy ports to loopback; its configuration was not changed')
    auth=config.get('auth',{})
    if auth.get('method','token')!='token':
        raise ValueError('Existing frps uses OIDC; automatic token integration is unavailable')
    token=auth.get('token')
    cwd=Path(info.get('cwd','/'))
    if not token:
        source=auth.get('tokenSource',{})
        if source.get('type')=='file':
            token=(cwd/source['file']['path']).read_text().strip()
    if not isinstance(token,str) or not token:
        raise ValueError('Existing frps has no reusable authentication token')
    tls=config.get('transport',{}).get('tls',{})
    if tls.get('trustedCaFile'):
        raise ValueError('Existing frps requires client certificates; automatic mTLS integration is unavailable')
    if not tls.get('certFile') or not tls.get('keyFile'):
        raise ValueError('Existing frps needs configured TLS certFile/keyFile for verified reuse; no configuration changed')
    cert=cwd/tls['certFile']
    run(['openssl','x509','-in',str(cert),'-checkend','86400','-noout'])
    check=subprocess.run(['openssl','x509','-in',str(cert),'-checkip',ip,'-noout'],capture_output=True,text=True)
    if check.returncode==0 and 'does match certificate' in check.stdout:
        server_name=ip
    else:
        names=run(['openssl','x509','-in',str(cert),'-noout','-ext','subjectAltName'])
        name=re.search(r'DNS:([^,\s]+)',names)
        if not name or name.group(1).startswith('*.'):
            raise ValueError('Existing FRP certificate has no usable IP/DNS SAN')
        server_name=name.group(1)
    version=binary_version(info['binary'])
    if not info.get('pid'):
        if not info.get('unit'):
            raise ValueError('Existing stopped frps has no systemd unit')
        run(['systemctl','start',info['unit']])
        run(['systemctl','is-active','--quiet',info['unit']])
    meta=dict(frps_binary=info['binary'],frps_config=info['config'],frps_unit=info.get('unit'),
              frp_proxy_addr=config.get('proxyBindAddr',config.get('bindAddr','0.0.0.0')),
              frp_version=version,frp_allow_ports=config.get('allowPorts') or [],frps_reused=True)
    credential=dict(token=token,ca_cert=cert.read_text(),server_name=server_name,frp_version=version,
                    auth_scopes=auth.get('additionalScopes',[]))
    return meta,credential


def discover_caddy():
    found=[]
    for port in (443,80):
        if not port_free(port):
            info=listening_process(port)
            if not info or not Path(info['binary']).name.endswith('caddy'):
                raise ValueError(f'Port {port} is occupied by a non-Caddy process')
            if not any(existing.get('pid')==info.get('pid') for existing in found):found.append(info)
    if not found:
        found=[info for info in installed_services('caddy') if info.get('config')]
    if len(found)>1:
        raise ValueError('Multiple existing Caddy services found; automatic integration is ambiguous')
    return found[0] if found else None


def run(args, timeout=None):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Never include argv/config contents: they may contain credentials.
        raise RuntimeError(f'{Path(args[0]).name} failed: {(result.stderr or result.stdout)[-1200:]}')
    return result.stdout


def atomic(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    owner=path.stat() if path.exists() else None
    try:
        temp.write_text(content)
        temp.chmod(mode)
        if owner is not None and os.geteuid()==0:
            os.chown(temp,owner.st_uid,owner.st_gid)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def download_binary(repo, version, asset, checksum_asset, members):
    """Verify published hashes (frp SHA-256 / Caddy SHA-512) before unpacking."""
    base = f'https://github.com/{repo}/releases/download/v{version}/'
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp)/asset
        urllib.request.urlretrieve(base+asset, archive)
        checks = urllib.request.urlopen(base+checksum_asset, timeout=60).read().decode()
        expected = next((line.split()[0] for line in checks.splitlines()
                         if line.split() and line.split()[-1].lstrip('*') == asset), None)
        algorithm={64:hashlib.sha256,128:hashlib.sha512}.get(len(expected or ''))
        if algorithm is None or algorithm(archive.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Checksum verification failed: {asset}')
        with tarfile.open(archive) as tar:
            for member, dest in members.items():
                entry = tar.getmember(member)
                if not entry.isfile():
                    raise ValueError('Release binary is not a regular file')
                dest=Path(dest)
                temporary=dest.with_name(dest.name+'.'+secrets.token_hex(6)+'.tmp')
                try:
                    with tar.extractfile(entry) as source, open(temporary, 'xb') as output:
                        shutil.copyfileobj(source, output)
                    temporary.chmod(0o755)
                    temporary.replace(dest)
                finally:
                    temporary.unlink(missing_ok=True)


def port_free(port):
    with socket.socket() as sock:
        try:
            sock.bind(('0.0.0.0', port))
            return True
        except OSError:
            return False


def server_toml(ip, token, port):
    bind = '::' if ipaddress.ip_address(ip).version == 6 else '0.0.0.0'
    return f'''bindAddr = {json.dumps(bind)}
bindPort = {int(port)}
proxyBindAddr = "127.0.0.1"
allowPorts = [{{start = 20000, end = 29999}}]
auth.method = "token"
auth.token = {json.dumps(token)}
auth.additionalScopes = ["HeartBeats", "NewWorkConns"]
transport.tls.force = true
transport.tls.certFile = {json.dumps(str(ETC/'server.crt'))}
transport.tls.keyFile = {json.dumps(str(ETC/'server.key'))}
log.to = "console"
log.level = "info"
'''


def caddy_site(host, port, address='127.0.0.1', cache_assets=False):
    address='['+address+']' if ':' in address else address
    cache = r'''    @hashed_assets {
        method GET HEAD
        path_regexp ^/assets/[^/]+-[A-Za-z0-9_-]{8}\.(js|css)$
    }
    header @hashed_assets {
        Cache-Control "private, max-age=31536000, immutable"
        match status 200
    }
''' if cache_assets else ''
    return f'''{host} {{
{cache}    reverse_proxy {address}:{int(port)} {{
        flush_interval -1
        header_up X-Forwarded-Proto https
        header_up X-Forwarded-Host {{host}}
    }}
}}
'''


def caddy_check_reload(meta):
    run([meta['caddy_binary'], 'validate', '--config', meta['caddy_config'], '--adapter', 'caddyfile'])
    try:
        run(['systemctl','is-active','--quiet',meta['caddy_unit']])
    except RuntimeError:
        run(['systemctl','start',meta['caddy_unit']])
    else:
        try:
            run(['systemctl','reload',meta['caddy_unit']],timeout=15)
        except (RuntimeError,subprocess.TimeoutExpired):
            # Some old packaged Caddy/Go builds panic during reload and leave
            # systemd stuck in reload-notify with no main process. Recover the
            # already validated config; never stop a still-running server.
            pid=subprocess.run(['systemctl','show',meta['caddy_unit'],'--property=MainPID','--value'],
                               capture_output=True,text=True).stdout.strip()
            if pid!='0':
                raise
            run(['systemctl','stop',meta['caddy_unit']],timeout=15)
            run(['systemctl','start',meta['caddy_unit']],timeout=15)
            run(['systemctl','is-active','--quiet',meta['caddy_unit']])


def ensure_unit_started(unit):
    try:
        run(['systemctl','is-active','--quiet',unit])
    except RuntimeError:
        run(['systemctl','start',unit])


def allocate(registry, owner, hostname, allowed=None):
    def permitted(port):
        return not allowed or any(item.get('single')==port or item.get('start',65536)<=port<=item.get('end',-1)
                                  for item in allowed)
    for key, value in registry.items():
        if value['hostname'] == hostname and key != owner:
            raise ValueError('Domain already belongs to another managed service')
    if owner in registry:
        if registry[owner]['hostname'] != hostname:
            raise ValueError('Service already has a different domain; remove its old route first')
        port=registry[owner]['port']
        if not permitted(port):
            raise ValueError('Existing frps no longer permits the registered proxy port')
        return port
    used = {value['port'] for value in registry.values()}
    for port in range(20000, 30000):
        if permitted(port) and port not in used and port_free(port):
            return port
    raise ValueError('No free FRP proxy port in 20000–29999')


def provision(payload):
    ip = str(ipaddress.ip_address(payload['server_ip']))
    port = int(payload.get('server_port',7000))
    if not 1024 <= port <= 65535 or 20000 <= port <= 29999:
        raise ValueError('Invalid FRP server port')
    for path in (ETC, DATA, BIN, ETC/'sites'):
        path.mkdir(parents=True, exist_ok=True)
    ETC.chmod(0o700)
    previous = json.loads((ETC/'server.json').read_text()) if (ETC/'server.json').exists() else None
    if previous and (previous['server_ip'] != ip or previous['server_port'] != port):
        raise ValueError('Server already provisioned with another IP/port; explicit migration required')
    arch = {'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
    if not arch:
        raise ValueError('Supported Linux architectures: amd64 and arm64')
    if not shutil.which('openssl'):
        if shutil.which('apt-get'):
            run(['apt-get','update','-qq'])
            run(['apt-get','install','-y','-qq','openssl'])
        elif shutil.which('dnf'):
            run(['dnf','install','-y','openssl'])
        else:
            raise ValueError('Install openssl on the server before provisioning')
    existing_frps=discover_frps(port)
    existing_caddy=discover_caddy()
    if existing_frps:
        frps_meta,credential=reuse_frps(existing_frps,ip,port)
    else:
        frps_binary=shutil.which('frps') or shutil.which('frpcolab-frps')
        installed_version=binary_version(frps_binary) if frps_binary else FRP_VERSION
        if not frps_binary and not (BIN/'frps').exists():
            asset = f'frp_{FRP_VERSION}_linux_{arch}.tar.gz'
            download_binary('fatedier/frp', FRP_VERSION, asset, 'frp_sha256_checksums.txt',
                            {f'frp_{FRP_VERSION}_linux_{arch}/frps': BIN/'frps'})
        frps_binary=frps_binary or str(BIN/'frps')
        run([frps_binary, '--version'])

        if not (ETC/'server.crt').exists():
            # A self-signed leaf pinned as the client's trust anchor; private key
            # stays on the SSH-managed server. No public FRP DNS name is needed.
            run(['openssl','req','-x509','-newkey','rsa:3072','-nodes','-days','3650',
                 '-keyout',str(ETC/'server.key'),'-out',str(ETC/'server.crt'),
                 '-subj','/CN=colabsvc-frps','-addext',f'subjectAltName=IP:{ip}'])
            (ETC/'server.key').chmod(0o600)
        token_path = ETC/'token'
        if not token_path.exists():
            atomic(token_path,secrets.token_urlsafe(48))
        token = token_path.read_text().strip()
        config = ETC/'frps.toml'
        old_config = config.read_text() if config.exists() else None
        new_config = server_toml(ip,token,port)
        candidate = ETC/'frps.check.toml'
        atomic(candidate,new_config)
        try:
            run([frps_binary,'verify','-c',str(candidate)])
        finally:
            candidate.unlink(missing_ok=True)
        unit = f'''[Unit]
    Description=Colab services FRP server
    After=network-online.target
    Wants=network-online.target
    [Service]
    ExecStart={frps_binary} -c {config}
    Restart=on-failure
    RestartSec=3
    UMask=0077
    NoNewPrivileges=true
    [Install]
    WantedBy=multi-user.target
    '''
        atomic(UNITS/'colabsvc-frps.service',textwrap.dedent(unit),0o644)
        atomic(config,new_config)
        run(['systemctl','daemon-reload'])
        run(['systemctl','enable','colabsvc-frps'])
        try:
            run(['systemctl','restart' if old_config != new_config else 'start','colabsvc-frps'])
            time.sleep(.3)
            run(['systemctl','is-active','--quiet','colabsvc-frps'])
        except Exception:
            if old_config is not None:
                atomic(config,old_config)
                run(['systemctl','restart','colabsvc-frps'])
            raise

        frps_meta=dict(frps_binary=frps_binary,frps_config=str(config),frps_unit='colabsvc-frps',
                       frp_version=installed_version,frp_allow_ports=[dict(start=20000,end=29999)],frps_reused=False)
        credential=dict(token=token,ca_cert=(ETC/'server.crt').read_text(),server_name=ip,
                        auth_scopes=['HeartBeats','NewWorkConns'],frp_version=installed_version)

    caddy_applied=False
    if previous:
        meta = previous
    elif existing_caddy or (STANDARD_CADDY.exists() and shutil.which('caddy')):
        info=existing_caddy or dict(binary=shutil.which('caddy'),config=str(STANDARD_CADDY),unit='caddy')
        if not info.get('config') or not info.get('unit'):
            raise ValueError('Existing Caddy requires a config file and a systemd unit for safe reload')
        if Path(info['config']).suffix.lower()=='.json':
            raise ValueError('Existing Caddy uses JSON configuration; automatic Caddyfile import is unavailable')
        meta=dict(caddy_binary=info['binary'],caddy_config=info['config'],caddy_unit=info['unit'],caddy_reused=True)
        sites=Path(info['config']).parent/'colabsvc-frp'
        sites.mkdir(exist_ok=True)
        original = Path(meta['caddy_config']).read_text()
        original_mode=Path(meta['caddy_config']).stat().st_mode & 0o777
        include = f'import {sites}/*.caddy'
        if include not in original.splitlines():
            candidate = original+'\n'+include+'\n'
            atomic(Path(meta['caddy_config']+'.colabsvc-backup'),original,original_mode)
            atomic(Path(meta['caddy_config']),candidate,original_mode)
            try:
                caddy_check_reload(meta)
                caddy_applied=True
            except Exception:
                atomic(Path(meta['caddy_config']),original,original_mode)
                caddy_check_reload(meta)
                raise
        meta['sites_dir'] = str(sites)
    elif (UNITS/'colabsvc-caddy.service').exists() and (ETC/'Caddyfile').exists():
        meta=dict(caddy_binary=str(BIN/'caddy'),caddy_config=str(ETC/'Caddyfile'),
                  caddy_unit='colabsvc-caddy',sites_dir=str(ETC/'sites'))
    else:
        if not all(port_free(p) for p in (80,443,2020)):
            raise ValueError('Ports 80/443/2020 are occupied; existing proxy needs explicit integration')
        caddy_binary=shutil.which('caddy')
        if not caddy_binary and not (BIN/'caddy').exists():
            asset=f'caddy_{CADDY_VERSION}_linux_{arch}.tar.gz'
            download_binary('caddyserver/caddy',CADDY_VERSION,asset,f'caddy_{CADDY_VERSION}_checksums.txt',
                            {'caddy':BIN/'caddy'})
        caddy_binary=caddy_binary or str(BIN/'caddy')
        # Caddy runs as root, with its own config/admin socket/cert storage.
        meta=dict(caddy_binary=caddy_binary,caddy_config=str(ETC/'Caddyfile'),
                  caddy_unit='colabsvc-caddy',sites_dir=str(ETC/'sites'))
        atomic(Path(meta['caddy_config']),'{\n    admin 127.0.0.1:2020\n}\nimport '+meta['sites_dir']+'/*.caddy\n')
        unit=f'''[Unit]
Description=Colab services HTTPS proxy
After=network-online.target
[Service]
ExecStart={caddy_binary} run --config {ETC}/Caddyfile --adapter caddyfile
ExecReload={caddy_binary} reload --config {ETC}/Caddyfile --adapter caddyfile --address 127.0.0.1:2020
Environment=XDG_DATA_HOME={DATA}/caddy
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
'''
        atomic(UNITS/'colabsvc-caddy.service',unit,0o644)
        run(['systemctl','daemon-reload'])
        run(['systemctl','enable','colabsvc-caddy'])
    meta.update(server_ip=ip,server_port=port,**frps_meta)
    if not caddy_applied:
        if previous or existing_caddy:
            ensure_unit_started(meta['caddy_unit'])
        else:
            caddy_check_reload(meta)
    run(['systemctl','is-active','--quiet',meta['caddy_unit']])
    atomic(ETC/'server.json',json.dumps(meta,indent=2))
    return credential


def route(payload):
    meta=json.loads((ETC/'server.json').read_text())
    registry_path=DATA/'routes.json'
    registry=json.loads(registry_path.read_text()) if registry_path.exists() else {}
    project,service,hostname=payload['project_id'],payload['service'],payload['hostname']
    if not re.fullmatch(r'[a-f0-9]{32}',project) or not re.fullmatch(r'[A-Za-z0-9_-]+',service):
        raise ValueError('Invalid project/service identity')
    if len(hostname)>253 or len(hostname.split('.'))<2 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in hostname.split('.')
    ):
        raise ValueError('Invalid hostname')
    owner=project+':'+service
    site=Path(meta['sites_dir'])/(project+'-'+service+'.caddy')
    old=site.read_text() if site.exists() else None
    removing=payload['action']=='remove'
    if removing:
        if owner not in registry:
            return {'removed':False}
        if registry[owner]['hostname'] != hostname:
            raise ValueError('Route identity mismatch')
        site.unlink(missing_ok=True)
    else:
        port=allocate(registry,owner,hostname,meta.get('frp_allow_ports'))
        cache_assets=False
        if payload.get('profile')=='comfyui':
            version=run([meta['caddy_binary'],'version'])
            match=re.search(r'\bv?(\d+)\.(\d+)\.(\d+)\b',version)
            cache_assets=bool(match and tuple(map(int,match.groups()))>=(2,10,2))
        content=caddy_site(hostname,port,meta.get('frp_proxy_addr','127.0.0.1'),cache_assets=cache_assets)
        atomic(site,content,0o644)
    try:
        if removing or old!=content:
            caddy_check_reload(meta)
        if removing:
            del registry[owner]
        else:
            registry[owner]=dict(hostname=hostname,port=port)
        atomic(registry_path,json.dumps(registry,indent=2))
    except Exception:
        if old is None:
            site.unlink(missing_ok=True)
        else:
            atomic(site,old,0o644)
        caddy_check_reload(meta)
        raise
    return {'removed':True} if removing else dict(remote_port=port,proxy_name=project+'-'+service)


def main(payload):
    if os.geteuid()!=0 or platform.system()!='Linux':
        raise ValueError('Server requires Linux with root or passwordless sudo')
    if not shutil.which('systemctl'):
        raise ValueError('Server requires systemd')
    DATA.mkdir(parents=True,exist_ok=True)
    with open(DATA/'lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if payload['action']=='provision':
            result=provision(payload)
        elif payload['action']=='status':
            meta=json.loads((ETC/'server.json').read_text())
            def state(unit):
                if not unit:return 'running' if not port_free(meta['server_port']) else 'inactive'
                return subprocess.run(['systemctl','is-active',unit],capture_output=True,text=True).stdout.strip()
            result=dict(frps=state(meta.get('frps_unit','colabsvc-frps')),
                        frps_unit=meta.get('frps_unit','colabsvc-frps'),frp_version=meta['frp_version'],
                        frps_reused=meta.get('frps_reused',False),
                        caddy=state(meta['caddy_unit']),caddy_unit=meta['caddy_unit'],
                        caddy_reused=meta.get('caddy_reused',False),
                        routes=json.loads((DATA/'routes.json').read_text()) if (DATA/'routes.json').exists() else {})
        elif payload['action'] in ('route','remove'):
            result=route(payload)
        else:
            raise ValueError('Unsupported server operation')
    print(json.dumps(result))
