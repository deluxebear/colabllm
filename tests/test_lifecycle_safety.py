"""State and process lifecycle regressions, using isolated profiles and child processes."""
import argparse
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/colabllm/lib'))
import colabsvc as svc
import processes
from cli_support import CliError


def save_key(profile, number):
    svc.STATE_ROOT = Path(profile)
    svc.State(svc.Service('test', 'test.ipynb', 9000)).save(**{f'key_{number}': number})


class LifecycleSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(svc, 'STATE_ROOT', self.root / 'profile'))
        self.service = svc.Service('test', 'test.ipynb', 9000)
        self.state = svc.State(self.service)
        self.script = self.root / 'test.py'
        self.script.write_text('')
        self.script.with_suffix('.ipynb').write_text(json.dumps({'cells': []}))

    def args(self, **kw):
        return argparse.Namespace(**dict(domain=None, base_domain=None, quick_tunnel=True, transport=None,
            gpu=None, skip='', dry_run=False, new_credentials=False, hf_token=None, no_drive=True,
            foreground=False, **kw))

    def test_concurrent_process_updates_keep_every_field(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(save_key, str(svc.STATE_ROOT), n) for n in range(40)]
            for future in futures:
                future.result(timeout=15)
        self.assertEqual(self.state.load(), {f'key_{n}': n for n in range(40)})

    def test_concurrent_lifecycle_operation_is_rejected(self):
        with self.state.lock('operation'), self.assertRaisesRegex(RuntimeError, '生命周期'):
            svc.cmd_down(self.service, argparse.Namespace(all=False, delete_tunnel=False))

    def test_failed_down_preserves_all_tracking_and_does_not_kill_runner(self):
        before = self.state.save(runner_pid=2345, runner_identity='example', ready=True,
                                 url='https://example.invalid', api_token='test-only')
        with patch.object(svc, 'run', return_value=argparse.Namespace(returncode=42)), \
             patch.object(svc, 'stop_owned_process') as stop, self.assertRaises(CliError) as error:
            svc.cmd_down(self.service, argparse.Namespace(all=False, delete_tunnel=False))
        self.assertEqual(error.exception.code, 42)
        self.assertEqual(self.state.load(), before)
        stop.assert_not_called()

    def test_foreground_registers_running_identity_before_entering_runner(self):
        def run(service, notebook):
            st = self.state.load()
            self.assertEqual(st['runner_pid'], os.getpid())
            self.assertEqual(st['runner_mode'], 'foreground')
            self.assertTrue(svc.runner_alive(st))
            self.assertTrue(svc.list_service_instances('test')[0]['running'])
            # The operation lock must be available to a separate down command.
            with self.state.lock('operation', blocking=False):
                pass
            return 0
        args = self.args(); args.foreground = True
        with patch.object(svc.shutil, 'which', return_value='/bin/colab'), \
             patch.object(svc, 'ensure_session'), patch.object(svc, 'exec_runner', side_effect=run), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
            svc.cmd_up(self.service, self.script, args)
        self.assertEqual(result.exception.code, 0)
        self.assertIsNone(self.state.load()['runner_pid'])

    def test_readiness_timeout_is_a_failure_and_preserves_background_state(self):
        self.service.ready_timeout = 0
        self.state.save(runner_pid=2345, runner_identity='example', ready=False)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(TimeoutError):
            svc._wait_ready(self.service, Mock(pid=2345))
        self.assertEqual(self.state.load()['runner_pid'], 2345)

    def test_reused_pid_is_never_signalled(self):
        with patch.object(processes, 'process_identity', return_value='new-start'), \
             patch.object(processes.os, 'kill') as kill, patch.object(processes.os, 'killpg') as killpg:
            processes.stop_owned_process(123, 'old-start', group=True)
            kill.assert_not_called(); killpg.assert_not_called()
            self.assertFalse(processes.runner_alive({'runner_pid': 123, 'runner_identity': 'old-start'}))

    def test_macos_identity_survives_exec_and_process_state_changes(self):
        stamp = 'Mon Nov  3 10:00:00 2026'
        results = [subprocess.CompletedProcess([], 0, f'{state} {stamp}\n', '') for state in ('R', 'S', 'Z')]
        with patch.object(processes.sys, 'platform', 'darwin'),              patch.object(processes.subprocess, 'run', side_effect=results) as run:
            self.assertEqual(processes.process_identity(123), stamp)
            self.assertEqual(processes.process_identity(123), stamp)
            self.assertIsNone(processes.process_identity(123))
            self.assertNotIn('command=', run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['env']['TZ'], 'UTC')


    def test_owned_process_is_stopped_and_waited_for(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        identity = processes.process_identity(child.pid)
        self.assertIsNotNone(identity)
        processes.stop_owned_process(child.pid, identity, group=True)
        child.wait(timeout=5)
        self.assertFalse(processes.owned_process(child.pid, identity))

    def test_parse_colab_session_status_detects_missing_despite_zero_exit(self):
        self.assertEqual(
            svc.parse_colab_session_status(
                'qwen-flash',
                0,
                '[qwen-flash] gpu-g4-s-asso1c0 | Hardware: G4 | Status: BUSY',
            ),
            'alive',
        )
        self.assertEqual(
            svc.parse_colab_session_status(
                'minimax-h3-image',
                0,
                "[colab] Session 'minimax-h3-image' not found.",
            ),
            'missing',
        )
        self.assertEqual(svc.parse_colab_session_status('ollama', 0, ''), 'unknown')

    def test_restart_tuning_flags_require_auto_restart(self):
        args = self.args(auto_restart=False, restart_delay=10.0, max_restarts=0)
        with self.assertRaisesRegex(ValueError, '--auto-restart'):
            svc.cmd_up(self.service, self.script, args)
        args2 = self.args(auto_restart=False, restart_delay=None, max_restarts=2)
        with self.assertRaisesRegex(ValueError, '--auto-restart'):
            svc.cmd_up(self.service, self.script, args2)

    def test_ensure_session_recreates_when_cached_session_was_reclaimed(self):
        fake_home = self.root / 'home'
        cfg_dir = fake_home / '.config/colab-cli'
        cfg_dir.mkdir(parents=True)
        (cfg_dir / 'sessions.json').write_text(json.dumps({'test': {'id': 'stale'}}))
        with patch.object(svc.Path, 'home', return_value=fake_home), \
             patch.object(svc, 'check_remote_session', return_value='missing'), \
             patch.object(svc, 'run') as mock_run:
            svc.ensure_session(self.service)
        cmds = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(cmds, [['colab', 'stop', '-s', 'test'], ['colab', 'new', '-s', 'test', '--gpu', 'T4']])

    def test_extract_failure_reason_handles_rich_tracebacks_tagged_messages_and_plain_stderr(self):
        rich_stderr = (
            "╭───── Traceback (most recent call last) ─────╮\n"
            "│ /fake/colab_cli/client.py:229               │\n"
            "╰─────────────────────────────────────────────╯\n"
            "ConnectionError: HTTPSConnectionPool(host='colab.research.google.com', port=443):\n"
            "NameResolutionError(\"Failed to resolve 'colab.research.google.com'\")\n"
        )
        extracted_rich = svc._extract_failure_reason("", rich_stderr)
        self.assertIsNotNone(extracted_rich)
        self.assertIn("ConnectionError:", extracted_rich)
        self.assertIn("NameResolutionError", extracted_rich)

        quota_stderr = (
            "[colab] Creating session 'indextts'...\n"
            "[colab] Allocation refused (precondition failed).\n"
        )
        self.assertEqual(
            svc._extract_failure_reason("", quota_stderr),
            "[colab] Allocation refused (precondition failed).",
        )

        self.assertEqual(
            svc._extract_failure_reason("", "plain error line 1\nplain error line 2\n"),
            "plain error line 2",
        )

    def test_inspect_remote_session_distinguishes_error_from_missing_and_alive(self):
        dns_err = subprocess.CompletedProcess(
            ["colab", "status", "-s", "test"],
            1,
            "",
            "╰────╯\nNameResolutionError: Failed to resolve 'colab.research.google.com'\n",
        )
        with patch.object(svc, "run", return_value=dns_err):
            state_name, detail = svc.inspect_remote_session(self.service)
        self.assertEqual(state_name, "error")
        self.assertIn("NameResolutionError", detail or "")

        alive_res = subprocess.CompletedProcess(
            ["colab", "status", "-s", "test"], 0, "[test] ep-1 | Hardware: L4 | Status: BUSY\n", ""
        )
        with patch.object(svc, "run", return_value=alive_res):
            self.assertEqual(svc.inspect_remote_session(self.service), ("alive", None))

        missing_res = subprocess.CompletedProcess(
            ["colab", "status", "-s", "test"], 0, "[colab] Session 'test' not found.\n", ""
        )
        with patch.object(svc, "run", return_value=missing_res):
            self.assertEqual(svc.inspect_remote_session(self.service), ("missing", None))

    def test_assign_retry_classification(self):
        for transient in (
            "ColabRequestError: Failed to issue request POST https://colab.research.google.com/tun/m/assign: Service Unavailable",
            "HTTP 502 Bad Gateway",
            "HTTP 504 Gateway Timeout",
            "429 Too Many Requests",
            "500 Internal Server Error",
            "ReadTimeout: Read timed out",
            "ConnectionError: NameResolutionError",
            "GPU capacity temporarily unavailable",
        ):
            self.assertTrue(svc._is_retryable_assign_error(transient), transient)

        for permanent in (
            "[colab] Allocation refused (precondition failed).",
            "TooManyAssignmentsError",
            "[colab] Backend rejected accelerator 'A100'.",
            "401 Unauthorized",
            "403 Forbidden",
            "400 Bad Request",
            "some unknown error",
            "",
        ):
            self.assertFalse(svc._is_retryable_assign_error(permanent), permanent)

    def test_ensure_session_retries_transient_errors_and_reuses_recorded_session(self):
        fake_home = self.root / 'home'
        cfg_dir = fake_home / '.config/colab-cli'
        cfg_dir.mkdir(parents=True)

        err_503 = CliError("命令执行失败 (1): colab new -s test — Service Unavailable", 1)
        ok_res = subprocess.CompletedProcess(["colab", "new"], 0, "[colab] Creating session 'test'...\n", "")
        with patch.object(svc.Path, 'home', return_value=fake_home), \
             patch.object(svc, 'run', side_effect=[err_503, ok_res]) as mock_run, \
             patch.object(svc.time, 'sleep') as mock_sleep:
            self.assertFalse(svc.ensure_session(self.service, assign_retries=2, assign_retry_delay=0.5))
        self.assertEqual(mock_run.call_count, 2)
        mock_sleep.assert_called_once_with(0.5)
        self.assertEqual(mock_run.call_args.kwargs['env']['COLAB_CLI_ASSIGN_RETRIES'], '1')
        self.assertEqual(mock_run.call_args.kwargs['env']['COLAB_CLI_RETRY_BACKOFF'], '5')

        # Respect explicit COLAB_CLI_ASSIGN_RETRIES / COLAB_CLI_RETRY_BACKOFF overrides.
        with patch.dict(os.environ, {'COLAB_CLI_ASSIGN_RETRIES': '0', 'COLAB_CLI_RETRY_BACKOFF': '2'}), \
             patch.object(svc.Path, 'home', return_value=fake_home), \
             patch.object(svc, 'run', return_value=ok_res) as mock_run_env:
            svc.ensure_session(self.service, assign_retries=1, assign_retry_delay=0.0)
        self.assertEqual(mock_run_env.call_args.kwargs['env']['COLAB_CLI_ASSIGN_RETRIES'], '0')
        self.assertEqual(mock_run_env.call_args.kwargs['env']['COLAB_CLI_RETRY_BACKOFF'], '2')

        # Non-retryable 412 fails fast without retrying or sleeping.
        err_412 = CliError("命令执行失败 (1): colab new -s test — [colab] Allocation refused (precondition failed).", 1)
        with patch.object(svc.Path, 'home', return_value=fake_home), \
             patch.object(svc, 'run', side_effect=err_412) as mock_run_412, \
             patch.object(svc.time, 'sleep') as mock_sleep_412:
            with self.assertRaises(CliError):
                svc.ensure_session(self.service, assign_retries=3, assign_retry_delay=1.0)
        self.assertEqual(mock_run_412.call_count, 1)
        mock_sleep_412.assert_not_called()

        # Timeout on first attempt where session actually materialized before retry -> reuse without calling `new` again.
        def timeout_then_recorded(*args, **kwargs):
            (cfg_dir / 'sessions.json').write_text(json.dumps({'test': {'id': 'late-sess'}}))
            raise subprocess.TimeoutExpired(args[0], 300)

        with patch.object(svc.Path, 'home', return_value=fake_home), \
             patch.object(svc, 'run', side_effect=timeout_then_recorded) as mock_run_to, \
             patch.object(svc, 'check_remote_session', return_value='alive'), \
             patch.object(svc.time, 'sleep') as mock_sleep_to:
            self.assertTrue(svc.ensure_session(self.service, assign_retries=2, assign_retry_delay=0.25))
        self.assertEqual(mock_run_to.call_count, 1)
        mock_sleep_to.assert_called_once_with(0.25)

    def test_ensure_session_ignores_rich_traceback_source_context(self):
        fake_home = self.root / 'home'
        (fake_home / '.config/colab-cli').mkdir(parents=True)
        ok_res = subprocess.CompletedProcess(["colab", "new"], 0, "[colab] Creating session 'test'...\n", "")

        err_503 = CliError("命令执行失败 (1): colab new -s test — Service Unavailable", 1)
        err_503.stdout = ""
        err_503.stderr = (
            "╭─── Traceback (most recent call last) ───╮\n"
            "│   170 │   except TooManyAssignmentsError: │\n"
            "╰─────────────────────────────────────────╯\n"
            "ColabRequestError: Failed to issue request POST "
            "https://colab.research.google.com/tun/m/assign?nbh=abc&accelerator=A100: Service Unavailable\n"
        )
        with patch.object(svc.Path, 'home', return_value=fake_home), \
             patch.object(svc, 'run', side_effect=[err_503, ok_res]) as mock_run, \
             patch.object(svc.time, 'sleep') as mock_sleep:
            self.assertFalse(svc.ensure_session(self.service, assign_retries=2, assign_retry_delay=0.2))
        self.assertEqual(mock_run.call_count, 2)
        mock_sleep.assert_called_once_with(0.2)

        err_401 = CliError("命令执行失败 (1): colab new -s test — Unauthorized", 1)
        err_401.stdout = ""
        err_401.stderr = (
            "╭─── Traceback (most recent call last) ───╮\n"
            "│   300 │   e, requests.exceptions.ReadTimeout │\n"
            "│   301 │   ) or status in (502, 503, 504)     │\n"
            "╰──────────────────────────────────────────────╯\n"
            "ColabRequestError: Failed to issue request POST "
            "https://colab.research.google.com/tun/m/assign?nbh=abc: Unauthorized\n"
        )
        with patch.object(svc.Path, 'home', return_value=fake_home), \
             patch.object(svc, 'run', side_effect=err_401) as mock_run_401, \
             patch.object(svc.time, 'sleep') as mock_sleep_401:
            with self.assertRaises(CliError):
                svc.ensure_session(self.service, assign_retries=3, assign_retry_delay=0.2)
        self.assertEqual(mock_run_401.call_count, 1)
        mock_sleep_401.assert_not_called()


if __name__ == '__main__':
    unittest.main()
