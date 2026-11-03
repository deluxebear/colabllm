"""User directory permissions, instance isolation and detached runner launch contract."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/colabllm/lib'))
import colabsvc as svc
from paths import user_home


class UserStateTest(unittest.TestCase):
    def test_default_and_override_home_do_not_depend_on_cwd(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'home', return_value=Path('/users/test')):
            self.assertEqual(user_home(), Path('/users/test/.colabllm'))
            with patch.dict(os.environ, {'COLABLLM_HOME': '/tmp/profile'}):
                self.assertEqual(user_home(), Path('/tmp/profile'))

    def test_private_state_and_instance_discovery(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(svc, 'STATE_ROOT', Path(temp) / 'profile'):
            base = svc.Service('example', 'example.ipynb', 7860)
            instance = svc.Service('example', 'example.ipynb', 7860)
            svc.apply_instance(instance, '2')
            for service in (base, instance):
                state = svc.State(service)
                state.save(api_token='test-only')
                self.assertEqual(state.path, svc.STATE_ROOT / 'services' / service.name / 'state.json')
                self.assertEqual(stat.S_IMODE(state.path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(state.dir.stat().st_mode), 0o700)
            self.assertEqual(len(svc.list_service_instances('example')), 2)
            self.assertEqual(svc.list_service_instances('unrelated'), [])

    def test_detached_runner_uses_service_script_and_user_notebook(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            root = Path(temp)
            stack.enter_context(patch.object(svc, 'STATE_ROOT', root / 'profile'))
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            script = root / 'installed/services/example.py'
            script.parent.mkdir(parents=True)
            script.write_text('')
            script.with_suffix('.ipynb').write_text(json.dumps({'cells': []}))
            service = svc.Service('example', 'example.ipynb', 7860)
            svc.apply_instance(service, '2')
            args = argparse.Namespace(domain=None, base_domain=None, quick_tunnel=True, transport=None,
                gpu=None, skip='', dry_run=False, new_credentials=False, hf_token=None, no_drive=True, foreground=False)
            def launch(command, **kwargs):
                self.assertEqual(command, [sys.executable, str(script), '-i', '2', '_exec',
                    str(root / 'profile/services/example-2/example.ipynb')])
                self.assertTrue(kwargs['start_new_session'])
                svc.State(service).save(ready=True, url='https://test.example')
                return Mock(pid=12345)
            stack.enter_context(patch.object(svc.shutil, 'which', return_value='/bin/colab'))
            stack.enter_context(patch.object(svc, 'ensure_session'))
            stack.enter_context(patch.object(svc, 'process_identity', return_value='test-start'))
            stack.enter_context(patch.object(svc, 'probe', return_value='200'))
            stack.enter_context(patch.object(svc.subprocess, 'Popen', side_effect=launch))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            svc.cmd_up(service, script, args)
            self.assertEqual(svc.State(service).load()['runner_pid'], 12345)


if __name__ == '__main__':
    unittest.main()
