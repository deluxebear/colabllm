#!/usr/bin/env python3
"""Verify a built wheel with uv tool install, without touching the user's installation."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import zipfile


def check(wheel):
    wheel = Path(wheel).resolve(strict=True)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        services = sorted(Path(n).stem for n in names if n.startswith('colabllm/services/') and n.endswith('.py'))
        assert services, 'Wheel contains no services'
        assert 'colabllm/lib/frp_server.py' in names, 'Missing SSH provisioning source'
        for service in services:
            notebook = json.loads(archive.read(f'colabllm/services/{service}.ipynb'))
            assert notebook['cells'], f'Empty notebook: {service}'
            assert all(not cell.get('outputs') for cell in notebook['cells']), f'Notebook outputs: {service}'
        assert not any('/.state/' in n or n.endswith('state.json') for n in names)

    with tempfile.TemporaryDirectory(prefix='colabllm-wheel-') as temp:
        root = Path(temp)
        home = root / 'home'
        home.mkdir()
        env = {**os.environ, 'HOME': str(home), 'UV_TOOL_DIR': str(root / 'tools'),
               'UV_TOOL_BIN_DIR': str(root / 'bin'), 'UV_CACHE_DIR': str(root / 'cache')}
        env.pop('COLABLLM_HOME', None)
        env.pop('PYTHONPATH', None)
        env.pop('COLABSVC_BASE_DOMAIN', None)
        env.pop('CT_INSTANCE', None)
        # All subprocesses run outside the source checkout.
        def run(args):
            result = subprocess.run(args, env=env, cwd=root, text=True, capture_output=True, timeout=120)
            if result.returncode:
                raise RuntimeError(f'{args}:\n{result.stdout}\n{result.stderr}')
            return result.stdout

        run(['uv', 'tool', 'install', '--python', sys.executable, str(wheel)])
        command = str(root / 'bin/colabllm')
        assert run([command, '--version']).startswith('colabllm ')
        assert run([command, 'services']).splitlines() == services
        business = {
            'qwen3-tts': ('tts', 'voices', 'design-voice'),
            'indextts': ('tts', 'voices'),
            'comfyui': ('pull-workflow', 'pull-model'),
            'unlimited-ocr': ('ocr', 'job', 'resume'),
            'longcat-avatar': ('generate', 'job', 'download'),
            'nemotron-transcribe': ('transcribe', 'download', 'ls-drive'),
        }
        for service in services:
            help_text = run([command, service, '--help'])
            assert '_exec' not in help_text
            for subcommand in business.get(service, ()):
                assert subcommand in help_text
                run([command, service, subcommand, '--help'])
            json.loads(run([command, service, 'info', '--json']))
        run([command, 'doctor', '--help'])
        for shell in ('bash', 'zsh', 'fish'):
            script = run([command, 'completion', shell])
            assert 'colabllm _complete' in script
        assert 'tts' in run([command, '_complete', '--', 'qwen3-tts', 't']).splitlines()
        run([command, 'frp-config', '--help'])
        skill_text = run([command, 'skill'])
        assert 'name: colabllm' in skill_text
        skill_parent = root / 'agent-skills'
        exported = Path(run([command, 'skill', '--output', str(skill_parent)]).strip())
        assert exported == skill_parent / 'colabllm'
        assert (exported / 'SKILL.md').read_text() == skill_text
        for reference in (*services, 'runtime-common', 'ocr', 'speech', 'inference', 'media', 'operations'):
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
        # A stub ensures dry-run cannot accidentally contact real Colab.
        stub = root / 'bin/colab'
        stub.write_text('#!/bin/sh\nexit 99\n')
        stub.chmod(0o755)
        env['PATH'] = str(root / 'bin') + os.pathsep + env.get('PATH', '')
        for service in services:
            run([command, service, 'up', '--dry-run', '--quick-tunnel', '-i', 'check'])
        assert not override.exists(), 'Dry-run unexpectedly wrote state'
    print(f'Wheel verified: {len(services)} services; isolated uv installation, notebook resources, user config and dry-runs passed.')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python tools/check_distribution.py path/to/colabllm-VERSION-py3-none-any.whl')
    check(sys.argv[1])
