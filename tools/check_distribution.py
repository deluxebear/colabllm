#!/usr/bin/env python3
"""Verify a built wheel or sdist with uv tool install, without touching the user's installation."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def _inspect_wheel(wheel: Path) -> tuple[list[str], list[str]]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        services = sorted(Path(n).stem for n in names if n.startswith('colabllm/services/') and n.endswith('.py'))
        notebooks = sorted(Path(n).stem for n in names if n.startswith('colabllm/services/') and n.endswith('.ipynb'))
        assert services, 'Wheel contains no services'
        assert services == notebooks, f'Service script/notebook mismatch: {services} != {notebooks}'
        k_services = sorted(
            Path(n).stem
            for n in names
            if n.startswith('colabllm/kaggle_services/') and n.endswith('.py') and Path(n).name != '__init__.py'
        )
        k_notebooks = sorted(
            Path(n).stem
            for n in names
            if n.startswith('colabllm/kaggle_services/') and n.endswith('.ipynb')
        )
        assert k_services, 'Wheel contains no kaggle_services'
        assert k_services == k_notebooks, f'Kaggle script/notebook mismatch: {k_services} != {k_notebooks}'
        assert 'colabllm/kaggle_services/__init__.py' in names, 'Missing kaggle_services/__init__.py'
        assert 'colabllm/kaggle_cli.py' in names, 'Missing kaggle_cli module'
        assert 'colabllm/lib/frp_server.py' in names, 'Missing SSH provisioning source'
        assert 'colabllm/web.py' in names, 'Missing web CLI module'
        assert 'colabllm/web_ui/server.py' in names, 'Missing web_ui server module'
        assert 'colabllm/web_ui/api_catalog.py' in names, 'Missing web_ui api_catalog module'
        assert 'colabllm/gateway.py' in names, 'Missing gateway CLI module'
        for gw_mod in ('__init__.py', 'catalog.py', 'auth.py', 'routing.py', 'server.py'):
            assert f'colabllm/gateway_runtime/{gw_mod}' in names, f'Missing gateway_runtime/{gw_mod}'
        for lib_mod in ('local_daemon.py', 'instance_discovery.py', 'runtime_core.py', 'kaggle_accounts.py', 'kagglesvc.py', 'kaggle_ws.py', 'mux.py'):
            assert f'colabllm/lib/{lib_mod}' in names, f'Missing lib/{lib_mod}'
        for asset in ('index.html', 'styles.css', 'copilot.css', 'app.js', 'copilot.js', 'copilot_studios.js', 'favicon.svg'):
            assert f'colabllm/web_ui/static/{asset}' in names, f'Missing web_ui static asset: {asset}'
        assert any(n.endswith('/licenses/LICENSE') or n.endswith('/LICENSE') for n in names), 'Missing LICENSE in wheel'
        meta_files = [n for n in names if n.endswith('.dist-info/METADATA')]
        assert meta_files, 'Missing METADATA in wheel'
        metadata = archive.read(meta_files[0]).decode('utf-8')
        assert 'License-Expression: MIT' in metadata, 'Missing MIT License-Expression in wheel METADATA'
        for service in services:
            notebook = json.loads(archive.read(f'colabllm/services/{service}.ipynb'))
            assert notebook['cells'], f'Empty notebook: {service}'
            assert all(not cell.get('outputs') for cell in notebook['cells']), f'Notebook outputs: {service}'
        for ksvc in k_services:
            notebook = json.loads(archive.read(f'colabllm/kaggle_services/{ksvc}.ipynb'))
            assert notebook['cells'], f'Empty Kaggle notebook: {ksvc}'
            assert all(not cell.get('outputs') for cell in notebook['cells']), f'Kaggle notebook outputs: {ksvc}'
        forbidden_endings = ('state.json', 'tokens.json', 'gateway.log', 'runtime.env', '.env', 'web.log')
        assert not any('/.state/' in n or n.endswith(forbidden_endings) for n in names)
        return services, k_services


def _inspect_sdist(sdist: Path) -> tuple[list[str], list[str]]:
    with tarfile.open(sdist, 'r:gz') as archive:
        members = {m.name: m for m in archive.getmembers() if m.isfile()}
        names = sorted(members)
        services = sorted(Path(n).stem for n in names if '/src/colabllm/services/' in n and n.endswith('.py'))
        notebooks = sorted(Path(n).stem for n in names if '/src/colabllm/services/' in n and n.endswith('.ipynb'))
        assert services, 'Source distribution contains no services'
        assert services == notebooks, f'Service script/notebook mismatch in sdist: {services} != {notebooks}'
        k_services = sorted(
            Path(n).stem
            for n in names
            if '/src/colabllm/kaggle_services/' in n and n.endswith('.py') and Path(n).name != '__init__.py'
        )
        k_notebooks = sorted(
            Path(n).stem
            for n in names
            if '/src/colabllm/kaggle_services/' in n and n.endswith('.ipynb')
        )
        assert k_services, 'Source distribution contains no kaggle_services'
        assert k_services == k_notebooks, f'Kaggle script/notebook mismatch in sdist: {k_services} != {k_notebooks}'
        assert any(n.endswith('/src/colabllm/kaggle_services/__init__.py') for n in names), 'Missing kaggle_services/__init__.py in sdist'
        assert any(n.endswith('/src/colabllm/kaggle_cli.py') for n in names), 'Missing kaggle_cli module in sdist'
        assert any(n.endswith('/src/colabllm/lib/frp_server.py') for n in names), 'Missing SSH provisioning source in sdist'
        assert any(n.endswith('/src/colabllm/web.py') for n in names), 'Missing web CLI module in sdist'
        assert any(n.endswith('/src/colabllm/web_ui/server.py') for n in names), 'Missing web_ui server module in sdist'
        assert any(n.endswith('/src/colabllm/web_ui/api_catalog.py') for n in names), 'Missing web_ui api_catalog module in sdist'
        assert any(n.endswith('/src/colabllm/gateway.py') for n in names), 'Missing gateway CLI module in sdist'
        for gw_mod in ('__init__.py', 'catalog.py', 'auth.py', 'routing.py', 'server.py'):
            assert any(n.endswith(f'/src/colabllm/gateway_runtime/{gw_mod}') for n in names), f'Missing gateway_runtime/{gw_mod} in sdist'
        for lib_mod in ('local_daemon.py', 'instance_discovery.py', 'runtime_core.py', 'kaggle_accounts.py', 'kagglesvc.py', 'kaggle_ws.py', 'mux.py'):
            assert any(n.endswith(f'/src/colabllm/lib/{lib_mod}') for n in names), f'Missing lib/{lib_mod} in sdist'
        for asset in ('index.html', 'styles.css', 'copilot.css', 'app.js', 'copilot.js', 'copilot_studios.js', 'favicon.svg'):
            assert any(n.endswith(f'/src/colabllm/web_ui/static/{asset}') for n in names), f'Missing web_ui static asset in sdist: {asset}'
        assert any(n.endswith('/LICENSE') for n in names), 'Missing LICENSE in sdist'
        pkg_infos = [n for n in names if n.endswith('/PKG-INFO')]
        assert pkg_infos, 'Missing PKG-INFO in sdist'
        pkg_info = archive.extractfile(members[pkg_infos[0]]).read().decode('utf-8')
        assert 'License-Expression: MIT' in pkg_info, 'Missing MIT License-Expression in sdist PKG-INFO'
        for name in names:
            if ('/src/colabllm/services/' in name or '/src/colabllm/kaggle_services/' in name) and name.endswith('.ipynb'):
                notebook = json.loads(archive.extractfile(members[name]).read())
                assert notebook['cells'], f'Empty notebook in sdist: {name}'
                assert all(not cell.get('outputs') for cell in notebook['cells']), f'Notebook outputs in sdist: {name}'
        forbidden_endings = ('state.json', 'tokens.json', 'gateway.log', 'runtime.env', '.env', 'web.log')
        assert not any('/.state/' in n or n.endswith(forbidden_endings) for n in names)
        return services, k_services


def check(artifact):
    artifact = Path(artifact).resolve(strict=True)
    if artifact.name.endswith('.whl'):
        services, k_services = _inspect_wheel(artifact)
    elif artifact.name.endswith('.tar.gz'):
        services, k_services = _inspect_sdist(artifact)
    else:
        raise ValueError(f'Unsupported distribution artifact: {artifact}')

    repo_services_dir = ROOT / 'src/colabllm/services'
    if repo_services_dir.is_dir():
        expected = sorted(p.stem for p in repo_services_dir.glob('*.py') if p.with_suffix('.ipynb').is_file())
        assert services == expected, f'Packaged services {services} do not match checkout {expected}'
    repo_kaggle_dir = ROOT / 'src/colabllm/kaggle_services'
    if repo_kaggle_dir.is_dir():
        expected_k = sorted(
            p.stem for p in repo_kaggle_dir.glob('*.py') if p.name != '__init__.py' and p.with_suffix('.ipynb').is_file()
        )
        assert k_services == expected_k, f'Packaged kaggle_services {k_services} do not match checkout {expected_k}'

    with tempfile.TemporaryDirectory(prefix='colabllm-dist-') as temp:
        root = Path(temp).resolve()
        home = root / 'home'
        home.mkdir()
        xdg = root / 'xdg'
        xdg.mkdir()
        k_cfg = root / 'kaggle-ext-cfg'
        k_cfg.mkdir()
        env = {
            **os.environ,
            'HOME': str(home),
            'XDG_CONFIG_HOME': str(xdg),
            'KAGGLE_EXT_CONFIG_DIR': str(k_cfg),
            'UV_TOOL_DIR': str(root / 'tools'),
            'UV_TOOL_BIN_DIR': str(root / 'bin'),
            'UV_CACHE_DIR': str(root / 'cache'),
        }
        for var in (
            'COLABLLM_HOME',
            'PYTHONPATH',
            'COLABSVC_BASE_DOMAIN',
            'CT_INSTANCE',
            'COLABLLM_KAGGLE_ACCOUNT',
            'KAGGLE_ACCOUNT',
        ):
            env.pop(var, None)

        # All subprocesses run outside the source checkout.
        def run(args):
            result = subprocess.run(args, env=env, cwd=root, text=True, capture_output=True, timeout=120)
            if result.returncode:
                raise RuntimeError(f'{args}:\n{result.stdout}\n{result.stderr}')
            return result.stdout

        run(['uv', 'tool', 'install', '--python', sys.executable, str(artifact)])
        command = str(root / 'bin/colabllm')
        assert run([command, '--version']).startswith('colabllm ')
        assert run([command, 'services']).splitlines() == services
        assert run([command, 'kaggle', 'services']).splitlines() == k_services
        business = {
            'ollama': ('endpoints', 'models', 'pull', 'run', 'preload', 'unload', 'cp', 'rm', 'show'),
            'qwen3-tts': ('tts', 'voices', 'design-voice'),
            'indextts': ('tts', 'voices'),
            'drama3': ('tts', 'narrate', 'voices', 'upload-voice', 'delete-voice', 'ls-drive'),
            'tangoflux': ('generate', 'sfx', 'batch', 'models', 'ls-drive'),
            'comfyui': ('pull-workflow', 'pull-model'),
            'openjutsu': ('check',),
            'unlimited-ocr': ('ocr', 'job', 'resume'),
            'oxr': ('parse',),
            'longcat-avatar': ('generate', 'job', 'download'),
            'nemotron-transcribe': ('transcribe', 'download', 'ls-drive'),
            'flux-klein': ('generate', 'edit', 'upsample', 'ls-drive', 'models'),
            'qwen-image': ('generate', 'edit', 'ls-drive', 'models'),
            'minimax-h3-image': ('generate', 'edit', 'job', 'download', 'ls-drive', 'models'),
            'qwen-flash': ('models', 'plan'),
            'deepseek-coder': ('models', 'plan'),
            'deepseek-qwen-9b': ('models', 'plan'),
            'r2-migrate': ('auth', 'buckets', 'sync', 'jobs', 'job', 'pause', 'resume', 'cleanup', 'cancel', 'usage', 'ls-drive'),
        }
        for service in services:
            help_text = run([command, service, '--help'])
            assert '_exec' not in help_text
            for subcommand in business.get(service, ()):
                assert subcommand in help_text
                run([command, service, subcommand, '--help'])
            json.loads(run([command, service, 'info', '--json']))
        for ksvc in k_services:
            k_help = run([command, 'kaggle', ksvc, '--help'])
            assert '_exec' not in k_help
            assert 'restart' in k_help
            json.loads(run([command, 'kaggle', ksvc, 'info', '--json']))
        doc_help = run([command, 'doctor', '--help'])
        assert '--provider' in doc_help
        for shell in ('bash', 'zsh', 'fish'):
            script = run([command, 'completion', shell])
            assert 'colabllm _complete' in script
        assert 'tts' in run([command, '_complete', '--', 'qwen3-tts', 't']).splitlines()
        assert 'web-probe' in run([command, '_complete', '--', 'kaggle', 'w']).splitlines()
        assert 'web' in run([command, '_complete', '--', 'w']).splitlines()
        assert 'gateway' in run([command, '_complete', '--', 'g']).splitlines()
        assert 'create' in run([command, '_complete', '--', 'gateway', 'token', '']).splitlines()
        run([command, 'web', '--help'])
        run([command, 'gateway', '--help'])
        run([command, 'gateway', 'token', '--help'])
        run([command, 'frp-config', '--help'])
        skill_text = run([command, 'skill'])
        assert 'name: colabllm' in skill_text
        skill_parent = root / 'agent-skills'
        exported = Path(run([command, 'skill', '--output', str(skill_parent)]).strip())
        assert exported == skill_parent / 'colabllm'
        assert (exported / 'SKILL.md').read_text() == skill_text
        for reference in (*services, *(f'kaggle-{k}' for k in k_services), 'runtime-common', 'ocr', 'speech', 'inference', 'media', 'operations'):
            assert (exported / 'references' / (reference + '.md')).is_file()
        config = json.loads(run([command, 'tunnel-config', '--base-domain', 'example.com']))
        state_home = home / '.colabllm'
        assert Path(config['config_file']) == state_home / 'tunnel-config.json'
        assert json.loads((state_home / 'tunnel-config.json').read_text())['base_domain'] == 'example.com'
        assert stat.S_IMODE(state_home.stat().st_mode) == 0o700
        assert stat.S_IMODE((state_home / 'tunnel-config.json').stat().st_mode) == 0o600
        override = root / 'other-profile'
        env['COLABLLM_HOME'] = str(override)
        config = json.loads(run([command, 'tunnel-config']))
        assert Path(config['config_file']) == override / 'tunnel-config.json'
        # Stubs ensure dry-run cannot accidentally contact real Colab or Kaggle.
        stub = root / 'bin/colab'
        stub.write_text('#!/bin/sh\nexit 99\n')
        stub.chmod(0o755)
        k_stub = root / 'bin/kaggle'
        k_stub.write_text('#!/bin/sh\nexit 99\n')
        k_stub.chmod(0o755)
        env['PATH'] = str(root / 'bin') + os.pathsep + env.get('PATH', '')
        for service in services:
            run([command, service, 'up', '--dry-run', '--quick-tunnel', '-i', 'check'])
        for ksvc in k_services:
            run([command, 'kaggle', ksvc, 'up', '--dry-run', '--quick-tunnel', '-i', 'check'])
        assert not override.exists(), 'Dry-run unexpectedly wrote state'
        gw_empty = root / 'empty-gw-home'
        env['COLABLLM_HOME'] = str(gw_empty)
        assert json.loads(run([command, 'gateway', '--status', '--json'])) == {'running': False}
        assert json.loads(run([command, 'gateway', 'token', 'list', '--json'])) == {'tokens': []}
        assert not gw_empty.exists(), 'Gateway status/token list unexpectedly created COLABLLM_HOME'
    kind = 'Wheel' if artifact.name.endswith('.whl') else 'Source distribution'
    print(f'{kind} verified: {len(services)} Colab + {len(k_services)} Kaggle services; isolated uv installation, LICENSE, notebook resources, user config and dry-runs passed.')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python tools/check_distribution.py path/to/colabllm-VERSION.(whl|tar.gz)')
    check(sys.argv[1])
