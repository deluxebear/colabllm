"""Timeout options bound external commands and polling without touching live services."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src/colabllm/lib'))
from cli_support import CliError, Deadline


class TimeoutTest(unittest.TestCase):
    def test_command_timeout_is_a_public_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root/'colab'
            executable.write_text('#!/bin/sh\nexec sleep 5\n')
            executable.chmod(0o755)
            env = {**os.environ, 'COLABLLM_HOME': str(root/'profile'),
                   'PATH': str(root)+os.pathsep+os.environ.get('PATH', '')}
            result = subprocess.run([str(ROOT/'colabllm'), '--command-timeout', '.05', 'ollama', 'status'],
                env=env, capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 124, result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertFalse((root/'profile/services/ollama/state.json').exists())

    def test_deadline_caps_each_operation(self):
        with patch('cli_support.time.monotonic', side_effect=[10, 10.75, 11.1]):
            deadline = Deadline(1)
            self.assertAlmostEqual(deadline.remaining(30), .25)
            with self.assertRaises(CliError) as error:
                deadline.remaining()
            self.assertEqual(error.exception.code, 124)

    def test_ocr_polling_times_out_and_restores_namespace(self):
        module = runpy.run_path(str(ROOT/'src/colabllm/services/unlimited-ocr.py'))
        job = {'id': 'test-job', 'status': 'queued', 'files': []}
        args = argparse.Namespace(timeout=.001, poll_interval=.1)
        with patch.dict(module['wait_job'].__globals__, {'request': lambda *args: job}), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(CliError) as error:
            module['wait_job'](job, args)
        self.assertEqual(error.exception.code, 124)
        self.assertIsNone(args._wait_deadline)

    def test_comfyui_polling_times_out_instead_of_retrying_deadline(self):
        module = runpy.run_path(str(ROOT/'src/colabllm/services/comfyui.py'))
        with patch.object(module['State'], 'load', return_value={'url': 'https://example.invalid', 'api_token': 'test-only'}), \
             patch.dict(module['_run_remote_pull_job'].__globals__, {'safe_urlopen': lambda *a, **kw: contextlib.closing(io.BytesIO(json.dumps({'job_id': 'test', 'status': 'running'}).encode()))}), \
             self.assertRaises(CliError) as error:
            module['_run_remote_pull_job']({'workflow': 'test'}, timeout=.001, poll=.1)
        self.assertEqual(error.exception.code, 124)


if __name__ == '__main__':
    unittest.main()
