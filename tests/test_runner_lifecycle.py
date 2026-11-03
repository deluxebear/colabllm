"""Exercise real CLI/runner subprocesses using a fake Colab CLI and local HTTP server."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/colabllm/lib'))
from processes import owned_process, stop_owned_process


class RunnerLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        class Health(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Health)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        executable = self.root / 'colab'
        executable.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
import sys
import time
root = Path(os.environ['FAKE_COLAB_ROOT'])
if sys.argv[1] == 'exec':
    if (root / 'exit-before-ready').exists():
        sys.exit(0)
    print('CT_PUBLIC_URL=' + os.environ['FAKE_COLAB_URL'], flush=True)
    print('CT_READY', flush=True)
    while True:
        time.sleep(1)
if sys.argv[1] == 'stop' and (root / 'fail-stop').exists():
    print('simulated remote failure', file=sys.stderr)
    sys.exit(42)
''')
        executable.chmod(0o755)
        self.env = {**os.environ, 'COLABLLM_HOME': str(self.root/'profile'),
            'FAKE_COLAB_ROOT': str(self.root), 'FAKE_COLAB_URL': f'http://127.0.0.1:{self.server.server_port}',
            'PATH': str(self.root) + os.pathsep + os.environ.get('PATH', '')}
        self.env.pop('CT_INSTANCE', None)
        self.state_path = self.root/'profile/services/ollama-test/state.json'
        self.addCleanup(self.stop_children)

    def stop_children(self):
        if self.state_path.exists():
            st = json.loads(self.state_path.read_text())
            for prefix in ('exec', 'runner'):
                stop_owned_process(st.get(prefix+'_pid'), st.get(prefix+'_identity'), group=True)

    def cli(self, *args):
        return subprocess.run([str(ROOT/'colabllm'), 'ollama', *args, '-i', 'test'], env=self.env,
            cwd=self.root, text=True, capture_output=True, timeout=10)

    def wait_ready(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.state_path.exists():
                st = json.loads(self.state_path.read_text())
                if st.get('ready') and st.get('exec_identity'):
                    return st
            time.sleep(0.02)
        self.fail('fake Colab runner never became ready')

    def test_background_up_status_failed_down_retry_and_cleanup(self):
        start = self.cli('up', '--quick-tunnel', '--no-drive', '--detach')
        self.assertEqual(start.returncode, 0, start.stderr)
        st = self.wait_ready()
        status = self.cli('status', '--json')
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertTrue(json.loads(status.stdout)['ready'])
        self.assertNotIn('api_token', status.stdout)
        (self.root/'fail-stop').touch()
        failed = self.cli('down')
        self.assertEqual(failed.returncode, 42, failed.stderr)
        self.assertTrue(owned_process(st['runner_pid'], st['runner_identity']))
        self.assertEqual(json.loads(self.state_path.read_text())['runner_pid'], st['runner_pid'])
        (self.root/'fail-stop').unlink()
        stop = self.cli('down')
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse(owned_process(st['runner_pid'], st['runner_identity']))
        self.assertFalse(owned_process(st['exec_pid'], st['exec_identity']))
        self.assertNotIn('runner_pid', json.loads(self.state_path.read_text()))

    def test_foreground_early_zero_exit_is_still_a_startup_failure(self):
        (self.root/'exit-before-ready').touch()
        result = self.cli('up', '--quick-tunnel', '--no-drive', '--foreground')
        self.assertEqual(result.returncode, 1, result.stderr)
        st = json.loads(self.state_path.read_text())
        self.assertFalse(st['ready'])
        self.assertEqual(st['runner_exit'], 1)


    def test_foreground_ps_duplicate_rejection_and_down(self):
        front = subprocess.Popen([str(ROOT/'colabllm'), 'ollama', 'up', '--quick-tunnel', '--no-drive',
            '--foreground', '-i', 'test'], env=self.env, cwd=self.root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: front.poll() is None and front.kill())
        st = self.wait_ready()
        self.assertEqual(st['runner_pid'], front.pid)
        listing = self.cli('ps', '--json')
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertTrue(json.loads(listing.stdout)[0]['running'])
        duplicate = self.cli('up', '--quick-tunnel', '--no-drive', '--detach')
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn('already running', duplicate.stderr)
        stop = self.cli('down')
        self.assertEqual(stop.returncode, 0, stop.stderr)
        front.wait(timeout=5)
        self.assertFalse(owned_process(st['exec_pid'], st['exec_identity']))


if __name__ == '__main__':
    unittest.main()
