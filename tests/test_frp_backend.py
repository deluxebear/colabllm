"""FRP selection, DNS ownership, secret handling and server routing transactions."""
import argparse
import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/colabllm/lib'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'templates'))
import colabsvc
import frp_backend as backend
import frp_server as server
from tunnel_client import render_frpc, run_tunnel

CONFIG=dict(enabled=True,server_ip='203.0.113.10',server_port=7000,ssh='root@frpip',project_id='a'*32)
DISCOVER_FRPS=server.discover_frps


class FrpTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(colabsvc,'STATE_ROOT',self.root))
        self.stack.enter_context(patch.dict(os.environ,{},clear=True))
        self.stack.enter_context(patch.object(server,'discover_frps',return_value=None))
        self.stack.enter_context(patch.object(server,'discover_caddy',return_value=None))
        # provision executes on a remote Linux server, regardless of the CLI host.
        self.stack.enter_context(patch.object(server.platform,'machine',return_value='x86_64'))
        self.svc=colabsvc.Service('avatar','avatar.ipynb',7860)

    def args(self,**kw):
        return argparse.Namespace(**dict(domain=None,base_domain=None,quick_tunnel=False,transport=None,**kw))

    def test_caddy_reload_recovers_only_after_main_process_has_exited(self):
        meta=dict(caddy_binary='/bin/caddy',caddy_config='/etc/caddy/Caddyfile',caddy_unit='caddy.service')
        for pid in ('0','123'):
            with self.subTest(pid=pid):
                def run(args, **kwargs):
                    if args[:2]==['systemctl','reload']:
                        raise server.subprocess.TimeoutExpired(args,15)
                    return ''
                with patch.object(server,'run',side_effect=run) as command, \
                     patch.object(server.subprocess,'run',return_value=argparse.Namespace(stdout=pid)):
                    if pid=='0':
                        server.caddy_check_reload(meta)
                    else:
                        with self.assertRaises(server.subprocess.TimeoutExpired):
                            server.caddy_check_reload(meta)
                stopped=any(c.args[0][:2]==['systemctl','stop'] for c in command.call_args_list)
                self.assertEqual(stopped,pid=='0')

    def test_configure_is_global_preserves_domain_and_redacts_credentials(self):
        args=argparse.Namespace(disable=False,server_ip=CONFIG['server_ip'],ssh=CONFIG['ssh'],base_domain='example.com',server_port=None)
        with patch.object(backend,'remote',return_value=dict(token='secret',ca_cert='BEGIN CERTIFICATE',frp_version='0.61.2')), \
             patch.object(colabsvc,'find_zone'),patch.object(backend.shutil,'which',return_value='/bin/tool'):
            result=backend.configure(args,self.root)
        self.assertEqual(result['base_domain'],'example.com')
        self.assertNotIn('secret',(self.root/'tunnel-config.json').read_text())
        credential=backend.credentials_path(self.root,result['frp'])
        self.assertEqual(credential.stat().st_mode&0o777,0o600)
        self.assertEqual(colabsvc.tunnel_transport(self.args()),'frp')
        with contextlib.redirect_stdout(io.StringIO()):
            colabsvc.cmd_tunnel_config(argparse.Namespace(clear=False,base_domain='new.example'))
        self.assertTrue(colabsvc.load_tunnel_config()['frp']['enabled'])
        args.disable=True
        backend.configure(args,self.root)
        self.assertEqual(colabsvc.tunnel_transport(self.args()),'cloudflare')

    def test_config_not_enabled_when_server_provisioning_fails(self):
        args=argparse.Namespace(disable=False,server_ip=CONFIG['server_ip'],ssh=CONFIG['ssh'],base_domain='example.com',server_port=None)
        with patch.object(backend,'remote',side_effect=RuntimeError('SSH failed')),patch.object(colabsvc,'find_zone'), \
             patch.object(backend.shutil,'which',return_value='/bin/tool'),self.assertRaises(RuntimeError):
            backend.configure(args,self.root)
        self.assertFalse((self.root/'tunnel-config.json').exists())

    def test_selection_overrides_and_domain_requirement(self):
        colabsvc.save_tunnel_config(dict(base_domain='example.com',frp=CONFIG))
        args=self.args();args.transport='cloudflare'
        self.assertEqual(colabsvc.tunnel_transport(args),'cloudflare')
        args.quick_tunnel=True
        self.assertIsNone(colabsvc.tunnel_hostname(self.svc,args))
        args.transport='frp'
        with self.assertRaises(ValueError):colabsvc.tunnel_transport(args)
        with self.assertRaises(ValueError):
            colabsvc.cmd_tunnel_config(argparse.Namespace(clear=True,base_domain=None))

    def test_auth_guard_and_default_cloudflare_quick_tunnel(self):
        with self.assertRaises(RuntimeError):run_tunnel({'AUTH_OK':False})
        from unittest.mock import Mock
        process=Mock(pid=999999999);process.poll.return_value=None
        ctx=dict(AUTH_OK=True,WORK=str(self.root),PUBLIC_PORT=18080,TUNNEL_TOKEN='',PUBLIC_HOST='',PROCS={},
                 ct_fetch_binary=Mock(return_value='/bin/cloudflared'),ct_spawn=Mock(return_value=process),
                 ct_tail=Mock(return_value='https://example-random.trycloudflare.com'))
        with contextlib.redirect_stdout(io.StringIO()):run_tunnel(ctx)
        self.assertEqual(ctx['PUBLIC_URL'],'https://example-random.trycloudflare.com')
        self.assertEqual(ctx['TUNNEL_PROCESS_KEY'],'cloudflared')
        self.assertIn('--url',ctx['ct_spawn'].call_args.args[1])
        self.assertNotIn('--token',ctx['ct_spawn'].call_args.args[1])

    def test_cloudflare_named_tunnel_still_registers(self):
        from unittest.mock import Mock
        process=Mock(pid=999999999);process.poll.return_value=None
        ctx=dict(AUTH_OK=True,WORK=str(self.root),PUBLIC_PORT=18080,TUNNEL_TOKEN='test-token',
                 PUBLIC_HOST='avatar.example.com',PROCS={},ct_fetch_binary=Mock(return_value='/bin/cloudflared'),
                 ct_spawn=Mock(return_value=process),ct_tail=Mock(return_value='Registered tunnel connection'))
        with contextlib.redirect_stdout(io.StringIO()):run_tunnel(ctx)
        self.assertEqual(ctx['PUBLIC_URL'],'https://avatar.example.com')
        self.assertIn('--token',ctx['ct_spawn'].call_args.args[1])

    def test_invalid_ssh_ip_and_ports(self):
        for updates in [dict(ssh='-oProxyCommand=evil'),dict(ssh='root@host;evil'),dict(server_ip='host.example'),
                        dict(server_port=80),dict(server_port=20001),dict(project_id='../etc')]:
            with self.subTest(updates=updates),self.assertRaises(ValueError):backend.validate_config({**CONFIG,**updates})

    def test_dns_only_and_unowned_conflict(self):
        calls=[]
        def cf(*args):
            calls.append(args)
            if 'list' in args:return []
            return {'id':'dns-id'}
        with patch.object(colabsvc,'find_zone',return_value={'id':'zone'}),patch.object(colabsvc,'cf_json',side_effect=cf):
            dns=backend.ensure_dns(self.svc,CONFIG,'avatar.example.com')
        body=json.loads(calls[-1][-1]);self.assertFalse(body['proxied'])
        self.assertEqual((body['type'],body['content']),('A',CONFIG['server_ip']))
        self.assertEqual(dns['dns_record_id'],'dns-id')
        with patch.object(colabsvc,'find_zone',return_value={'id':'zone'}), \
             patch.object(colabsvc,'cf_json',return_value=[{'id':'foreign','comment':'unrelated'}]),self.assertRaises(ValueError):
            backend.ensure_dns(self.svc,CONFIG,'avatar.example.com')

    def test_failed_route_restores_dns(self):
        backend.write_private(backend.credentials_path(self.root,CONFIG),{'token':'test'})
        dns=dict(zone_id='zone',dns_record_id='id',dns_owner='owned',_previous={'type':'CNAME'})
        with patch.object(backend,'ensure_dns',return_value=dns),patch.object(backend,'remote',side_effect=RuntimeError()), \
             patch.object(backend,'rollback_dns') as rollback,self.assertRaises(RuntimeError):
            backend.ensure_route(self.svc,CONFIG,'avatar.example.com',self.root)
        rollback.assert_called_once_with(dns,'avatar.example.com')

    def test_cleanup_only_deletes_owned_record(self):
        state=dict(frp=CONFIG,hostname='avatar.example.com',zone_id='zone',dns_record_id='id',dns_owner='owned')
        with patch.object(backend,'remote'),patch.object(colabsvc,'cf_json',return_value=[{'id':'id','comment':'foreign'}]) as cf:
            backend.remove_route(state,'avatar')
        self.assertEqual(cf.call_count,1)

    def test_frpc_toml_roundtrips_and_keeps_auth_entrypoint(self):
        import tomllib
        client={**CONFIG, 'token':'quote"\\secret','proxy_name':'project-avatar','remote_port':20003}
        parsed=tomllib.loads(render_frpc(client,18080,Path('/tmp/ca.crt')))
        self.assertEqual(parsed['auth']['token'],client['token'])
        self.assertEqual(parsed['proxies'][0]['localPort'],18080)
        self.assertEqual(parsed['proxies'][0]['localIP'],'127.0.0.1')
        self.assertEqual(parsed['transport']['tls']['serverName'],CONFIG['server_ip'])

    def test_port_allocator_avoids_other_services_and_system_ports(self):
        registry={'other':dict(hostname='other.example',port=20000)}
        with patch.object(server,'port_free',side_effect=lambda p:p!=20001):
            self.assertEqual(server.allocate(registry,'new','new.example'),20002)
        with self.assertRaises(ValueError):server.allocate(registry,'new','other.example')
        with self.assertRaises(ValueError):server.allocate(registry,'other','different.example')

    def test_server_route_rolls_back_site_on_invalid_config(self):
        etc=self.root/'etc';data=self.root/'data';sites=etc/'sites'
        sites.mkdir(parents=True);data.mkdir()
        (etc/'server.json').write_text(json.dumps(dict(sites_dir=str(sites))))
        payload=dict(action='route',project_id=CONFIG['project_id'],service='avatar',hostname='avatar.example.com')
        site=sites/(CONFIG['project_id']+'-avatar.caddy')
        with patch.object(server,'ETC',etc),patch.object(server,'DATA',data),patch.object(server,'port_free',return_value=True), \
             patch.object(server,'caddy_check_reload',side_effect=[RuntimeError('validation failed'),None]),self.assertRaises(RuntimeError):
            server.route(payload)
        self.assertFalse(site.exists());self.assertFalse((data/'routes.json').exists())

    def test_provision_is_idempotent_and_reuses_token_certificate_and_service(self):
        etc=self.root/'etc';data=self.root/'data';binary=self.root/'bin';units=self.root/'units';units.mkdir()
        def download(repo,version,asset,checksum,members):
            for target in members.values():Path(target).write_text('test binary')
        def run(args, **kwargs):
            if args[0]=='openssl':
                (etc/'server.crt').write_text('BEGIN CERTIFICATE')
                (etc/'server.key').write_text('private key')
            return ''
        with patch.object(server,'ETC',etc),patch.object(server,'DATA',data),patch.object(server,'BIN',binary), \
             patch.object(server,'UNITS',units),patch.object(server,'STANDARD_CADDY',self.root/'not-existing'), \
             patch.object(server,'port_free',return_value=True),patch.object(server,'download_binary',side_effect=download) as dl, \
             patch.object(server,'run',side_effect=run) as command,patch.object(server.time,'sleep'):
            first=server.provision(CONFIG)
            self.assertEqual(dl.call_count,2)
            command.reset_mock()
            second=server.provision(CONFIG)
        self.assertEqual(first,second)
        self.assertEqual(dl.call_count,2)
        self.assertNotIn(['systemctl','restart','colabsvc-frps'],[c.args[0] for c in command.call_args_list])
        self.assertNotIn(['systemctl','reload','colabsvc-caddy'],[c.args[0] for c in command.call_args_list])
        self.assertEqual((etc/'server.key').stat().st_mode&0o777,0o600)
        self.assertIn('proxyBindAddr = "127.0.0.1"',(etc/'frps.toml').read_text())
        self.assertTrue((units/'colabsvc-caddy.service').exists())

    def test_existing_standard_caddy_config_survives_integration(self):
        etc=self.root/'etc';data=self.root/'data';binary=self.root/'bin';units=self.root/'units';units.mkdir()
        standard=self.root/'caddy'/'Caddyfile';standard.parent.mkdir()
        original='existing.example.com {\n respond "existing"\n}\n';standard.write_text(original);standard.chmod(0o640)
        def download(repo,version,asset,checksum,members):
            for target in members.values():Path(target).write_text('test binary')
        def run(args, **kwargs):
            if args[0]=='openssl':
                (etc/'server.crt').write_text('BEGIN CERTIFICATE');(etc/'server.key').write_text('private key')
            if args[:2]==['systemctl','show']:return str(standard)
            return ''
        with patch.object(server,'ETC',etc),patch.object(server,'DATA',data),patch.object(server,'BIN',binary), \
             patch.object(server,'UNITS',units),patch.object(server,'STANDARD_CADDY',standard), \
             patch.object(server.shutil,'which',side_effect=lambda name:'/bin/tool' if name in ('openssl','caddy') else None),patch.object(server,'port_free',return_value=True), \
             patch.object(server,'download_binary',side_effect=download),patch.object(server,'run',side_effect=run),patch.object(server.time,'sleep'):
            server.provision(CONFIG)
        self.assertTrue(standard.read_text().startswith(original))
        self.assertEqual(standard.stat().st_mode&0o777,0o640)
        self.assertEqual(Path(str(standard)+'.colabsvc-backup').read_text(),original)
        self.assertFalse((units/'colabsvc-caddy.service').exists())

    def test_detect_existing_frps_from_port_owner_with_custom_binary_name(self):
        info=dict(binary='/usr/local/bin/frpcolab-frps',config='/etc/frpcolab/frps.toml',unit='frpcolab-frps.service')
        with patch.object(server,'port_free',return_value=False),patch.object(server,'listening_process',return_value=info):
            self.assertEqual(DISCOVER_FRPS(7000),info)
        with patch.object(server,'port_free',return_value=False),patch.object(server,'listening_process',return_value=dict(binary='/bin/other')):
            with self.assertRaises(ValueError):DISCOVER_FRPS(7000)

    def existing_frps(self, running=True):
        cert=self.root/'existing.crt';cert.write_text('BEGIN CERTIFICATE')
        config=self.root/'existing.toml'
        config.write_text('''bindPort = 7000
proxyBindAddr = "127.0.0.1"
auth.method = "token"
auth.token = "existing-secret"
transport.tls.certFile = '''+json.dumps(str(cert))+'''
transport.tls.keyFile = "/existing/server.key"
''')
        return dict(binary='/usr/local/bin/frpcolab-frps',config=str(config),
                    unit='frpcolab-frps.service',pid=123 if running else None,cwd='/')

    def test_reuse_existing_token_tls_dns_name_and_version_without_restart(self):
        info=self.existing_frps()
        def command(args):
            if args[-1]=='--version':return '0.71.1\n'
            if 'subjectAltName' in args:return 'DNS:frpcolab\n'
            return ''
        import types
        with patch.object(server,'run',side_effect=command) as run, \
             patch.object(server.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout='IP 203.0.113.10 does NOT match certificate')):
            meta,credential=server.reuse_frps(info,CONFIG['server_ip'],7000)
        self.assertEqual((credential['token'],credential['server_name'],credential['frp_version']),
                         ('existing-secret','frpcolab','0.71.1'))
        self.assertEqual(meta['frps_unit'],'frpcolab-frps.service')
        self.assertFalse(any(c.args[0][0]=='systemctl' for c in run.call_args_list))

    def test_existing_stopped_frps_is_started_using_its_original_unit(self):
        info=self.existing_frps(False)
        def command(args):
            if args[-1]=='--version':return '0.71.1'
            return ''
        import types
        with patch.object(server,'run',side_effect=command) as run, \
             patch.object(server.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout='IP does match certificate')):
            server.reuse_frps(info,CONFIG['server_ip'],7000)
        self.assertIn(['systemctl','start','frpcolab-frps.service'],[c.args[0] for c in run.call_args_list])

    def test_provision_reuses_both_existing_services_and_does_not_install_duplicate(self):
        etc=self.root/'managed';data=self.root/'data';binary=self.root/'bin';units=self.root/'units';units.mkdir()
        caddy=self.root/'custom-caddy'/'Caddyfile';caddy.parent.mkdir();caddy.write_text('original.example {\nrespond "ok"\n}\n')
        frps_info=self.existing_frps()
        caddy_info=dict(binary='/usr/bin/caddy',config=str(caddy),unit='custom-caddy.service',pid=456)
        credential=dict(token='existing-secret',ca_cert='BEGIN CERTIFICATE',frp_version='0.71.1',server_name='frpcolab')
        meta=dict(frps_unit='frpcolab-frps.service',frps_reused=True,frp_version='0.71.1')
        with patch.object(server,'ETC',etc),patch.object(server,'DATA',data),patch.object(server,'BIN',binary), \
             patch.object(server,'UNITS',units),patch.object(server,'discover_frps',return_value=frps_info), \
             patch.object(server,'discover_caddy',return_value=caddy_info),patch.object(server,'reuse_frps',return_value=(meta,credential)), \
             patch.object(server,'download_binary') as download,patch.object(server,'run') as run:
            result=server.provision(CONFIG)
        download.assert_not_called()
        self.assertEqual(result,credential)
        self.assertFalse((units/'colabsvc-frps.service').exists())
        self.assertFalse((units/'colabsvc-caddy.service').exists())
        self.assertTrue(caddy.read_text().startswith('original.example'))
        self.assertFalse(any(c.args[0][:2]==['systemctl','restart'] for c in run.call_args_list))

    def test_existing_frps_allow_ports_limits_allocation(self):
        with patch.object(server,'port_free',return_value=True):
            self.assertEqual(server.allocate({},'owner','host.example',[dict(single=21000)]),21000)
            with self.assertRaises(ValueError):server.allocate({},'owner','host.example',[dict(single=19000)])

    def test_reused_server_tls_name_and_scopes_reach_client(self):
        import tomllib
        client={**CONFIG,'token':'old-token','proxy_name':'check','remote_port':21000,'server_name':'frpcolab','auth_scopes':[]}
        parsed=tomllib.loads(render_frpc(client,8000,Path('/tmp/ca.crt')))
        self.assertEqual(parsed['transport']['tls']['serverName'],'frpcolab')
        self.assertEqual(parsed['auth']['additionalScopes'],[])

    def test_up_injects_frp_and_down_uses_saved_route_when_global_changes(self):
        colabsvc.save_tunnel_config(dict(base_domain='example.com',frp=CONFIG))
        script=self.root/'avatar.py';script.write_text('')
        (self.root/'avatar.ipynb').write_text(json.dumps({'cells':[]}))
        args=self.args();args.__dict__.update(gpu=None,skip='',dry_run=False,new_credentials=False,
                                             hf_token=None,no_drive=True,foreground=True)
        route=dict(hostname='avatar.example.com',frp={**CONFIG,'remote_port':20000},zone_id='zone')
        env=dict(CT_TRANSPORT='frp',CT_PUBLIC_HOSTNAME='avatar.example.com',CT_FRP_TOKEN='test-token')
        with patch.object(colabsvc.shutil,'which',return_value='/bin/tool'),patch.object(colabsvc,'ensure_session'), \
             patch.object(backend,'ensure_route',return_value=(route,env)),patch.object(colabsvc,'ensure_named_tunnel') as named, \
             patch.object(colabsvc,'exec_runner',return_value=0),contextlib.redirect_stdout(io.StringIO()),self.assertRaises(SystemExit):
            colabsvc.cmd_up(self.svc,script,args)
        named.assert_not_called()
        state=colabsvc.State(self.svc).load()
        self.assertEqual(state['env']['CT_TUNNEL_TOKEN'],'')
        self.assertEqual(state['env']['CT_FRP_TOKEN'],'test-token')
        colabsvc.save_tunnel_config({})
        with patch.object(backend,'remove_route') as remove,patch.object(colabsvc,'run',return_value=argparse.Namespace(returncode=0)),contextlib.redirect_stdout(io.StringIO()):
            colabsvc.cmd_down(self.svc,argparse.Namespace(all=False,delete_tunnel=True))
        remove.assert_called_once_with(route,'avatar')

    def test_cleanup_remembers_cloudflare_hostname_after_frp_switch(self):
        old=dict(hostname='old.example.com',tunnel_id='tunnel',zone_id='zone')
        colabsvc.State(self.svc).save(hostname='new.example.com',transport='frp',cloudflare_route=old,tunnel_id='tunnel')
        with patch.object(colabsvc,'run',return_value=argparse.Namespace(returncode=0)),patch.object(colabsvc,'delete_named_tunnel') as delete, \
             contextlib.redirect_stdout(io.StringIO()):
            colabsvc.cmd_down(self.svc,argparse.Namespace(all=False,delete_tunnel=True))
        delete.assert_called_once_with(old)


if __name__=='__main__':unittest.main()
