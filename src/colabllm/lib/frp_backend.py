"""Project FRP configuration, SSH provisioning and owned Cloudflare DNS records."""
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
from cli_support import command_timeout
from pathlib import Path


def validate_config(config):
    if not isinstance(config,dict) or not isinstance(config.get('enabled',False),bool):
        raise ValueError('frp configuration must be an object with a boolean enabled field')
    enabled=bool(config.get('enabled'))
    has_endpoint_fields=any(k in config for k in ('server_ip','ssh','server_port','project_id'))
    if not enabled and not has_endpoint_fields:
        return {**config,'enabled':False}
    result=dict(config,enabled=enabled)
    result['server_ip']=str(ipaddress.ip_address(config.get('server_ip','')))
    target=config.get('ssh','')
    if not isinstance(target,str) or target.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.:@\[\]-]+',target):
        raise ValueError('Invalid SSH target; use root@server or an SSH config alias')
    port=config.get('server_port',7000)
    if isinstance(port,bool) or not isinstance(port,int) or not 1024<=port<=65535 or 20000<=port<=29999:
        raise ValueError('FRP server port must be 1024–65535 outside proxy range 20000–29999')
    result['server_port']=port
    if not re.fullmatch(r'[a-f0-9]{32}',config.get('project_id','')):
        raise ValueError('Invalid FRP project identity')
    return result


def remote(config, payload):
    target=config['ssh']
    if target.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.:@\[\]-]+',target):
        raise ValueError('Invalid SSH target')
    source=(Path(__file__).parent/'frp_server.py').read_text()
    source+='\nmain('+repr(payload)+')\n'
    # Static remote shell command; all input, including source, travels on stdin.
    command='if [ "$(id -u)" = 0 ]; then exec python3 -; else exec sudo -n python3 -; fi'
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','--',target,command],
                          input=source,capture_output=True,text=True,timeout=command_timeout(600))
    if result.returncode:
        raise RuntimeError('FRP server operation failed: '+result.stderr[-1600:])
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise RuntimeError('FRP server returned an invalid response') from None


def credentials_path(state_root, config):
    identity=config['ssh']+'|'+config['server_ip']+'|'+str(config['server_port'])
    return Path(state_root)/'frp'/ (hashlib.sha256(identity.encode()).hexdigest()[:24]+'.json')


def write_private(path, data):
    path=Path(path)
    from paths import private_dir
    private_dir(path.parent)
    temp=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    try:
        with temp.open('x') as output:
            os.chmod(temp,0o600)
            json.dump(data,output,indent=2)
            output.write('\n')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def configure(args, state_root):
    from colabsvc import load_tunnel_config, save_tunnel_config, validate_domain, find_zone
    saved=load_tunnel_config()
    if args.disable:
        if saved.get('frp'):
            saved['frp']['enabled']=False
            save_tunnel_config(saved)
        return saved
    supplied=any(getattr(args,k,None) is not None for k in ('server_ip','ssh','base_domain','server_port'))
    if not supplied:
        return saved
    previous=saved.get('frp',{})
    cfg=dict(enabled=True,server_ip=args.server_ip or previous.get('server_ip'),
             ssh=args.ssh or previous.get('ssh'),server_port=args.server_port or previous.get('server_port',7000),
             project_id=previous.get('project_id') or secrets.token_hex(16))
    cfg=validate_config(cfg)
    base=validate_domain(args.base_domain or saved.get('base_domain',''))
    for executable in ('ssh','cf'):
        if not shutil.which(executable):
            raise ValueError(f'{executable} CLI not found')
    find_zone(base)  # Verify Cloudflare account/zone before touching the server.
    result=remote(cfg,dict(action='provision',server_ip=cfg['server_ip'],server_port=cfg['server_port']))
    if not result.get('token') or 'BEGIN CERTIFICATE' not in result.get('ca_cert',''):
        raise RuntimeError('FRP provisioner did not return client credentials')
    write_private(credentials_path(state_root,cfg),result)
    saved.update(base_domain=base,frp=cfg)
    save_tunnel_config(saved)
    return saved


def ensure_dns(svc, config, hostname):
    from colabsvc import cf_json, find_zone
    zone=find_zone(hostname)
    records=cf_json('dns','records','list','-z',zone['id'],'--name',hostname)
    owner='colabsvc-frp '+config['project_id']+' '+svc.name
    # Permit switching this project's existing Cloudflare named-tunnel record;
    # it must carry our existing service ownership comment.
    allowed={owner,'colabsvc '+svc.name}
    if any(record.get('comment') not in allowed for record in records) or len(records)>1:
        raise ValueError(f'DNS conflict for {hostname}; existing records are not exclusively managed by this service')
    record_type='AAAA' if ipaddress.ip_address(config['server_ip']).version==6 else 'A'
    body=dict(type=record_type,name=hostname,content=config['server_ip'],proxied=False,ttl=1,comment=owner)
    if records:
        record=records[0]
        previous={k:record[k] for k in ('type','name','content','proxied','ttl','comment') if k in record}
        if any(record.get(k)!=v for k,v in body.items() if k!='ttl'):
            cf_json('dns','records','edit',record['id'],'-z',zone['id'],'--body',json.dumps(body))
    else:
        record=cf_json('dns','records','create','-z',zone['id'],'--body',json.dumps(body))
    return dict(zone_id=zone['id'],dns_owner=owner,dns_record_id=record['id'],
                _previous=previous if records else None)


def rollback_dns(dns, hostname):
    from colabsvc import cf_json
    for record in cf_json('dns','records','list','-z',dns['zone_id'],'--name',hostname):
        if record['id']!=dns['dns_record_id'] or record.get('comment')!=dns['dns_owner']:
            continue
        if dns['_previous'] is None:
            cf_json('dns','records','delete',record['id'],'-z',dns['zone_id'],'--force')
        else:
            cf_json('dns','records','edit',record['id'],'-z',dns['zone_id'],
                    '--body',json.dumps(dns['_previous']))


def ensure_route(svc, config, hostname, state_root):
    # Read credentials before mutating remote routes or DNS.
    credential=json.loads(credentials_path(state_root,config).read_text())
    dns=ensure_dns(svc,config,hostname)
    try:
        result=remote(config,dict(action='route',project_id=config['project_id'],service=svc.name,hostname=hostname,
                                  profile='comfyui' if svc.base_name=='comfyui' else None))
    except Exception:
        rollback_dns(dns,hostname)
        raise
    dns.pop('_previous')
    state=dict(transport='frp',hostname=hostname,frp={**config,**result},**dns)
    env=dict(CT_TRANSPORT='frp',CT_PUBLIC_HOSTNAME=hostname,
             CT_FRP_SERVER_IP=config['server_ip'],CT_FRP_SERVER_PORT=str(config['server_port']),
             CT_FRP_REMOTE_PORT=str(result['remote_port']),CT_FRP_PROXY_NAME=result['proxy_name'],
             CT_FRP_TOKEN=credential['token'],CT_FRP_CA_CERT=credential['ca_cert'],
             CT_FRP_SERVER_NAME=credential.get('server_name') or config['server_ip'],
             CT_FRP_AUTH_SCOPES=json.dumps(credential.get('auth_scopes',['HeartBeats','NewWorkConns'])),
             CT_FRP_VERSION=credential['frp_version'])
    return state,env


def remove_route(state, service_name):
    from colabsvc import cf_json
    config=state['frp']
    remote(config,dict(action='remove',project_id=config['project_id'],service=service_name,hostname=state['hostname']))
    for record in cf_json('dns','records','list','-z',state['zone_id'],'--name',state['hostname']):
        if record['id']==state.get('dns_record_id') and record.get('comment')==state.get('dns_owner'):
            cf_json('dns','records','delete',record['id'],'-z',state['zone_id'],'--force')
