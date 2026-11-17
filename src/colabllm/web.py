#!/usr/bin/env python3
"""Local visual Web console for managing ColabLLM runtimes."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import webbrowser

_PKG_DIR = Path(__file__).resolve().parent
_LIB_DIR = _PKG_DIR / 'lib'
_SRC_DIR = _PKG_DIR.parent
for _path in (str(_LIB_DIR), str(_SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from cli_support import CliError, UsageError, positive_seconds, run_cli, validate_account_syntax  # noqa: E402
from colab_accounts import validate_account  # noqa: E402
from local_daemon import (  # noqa: E402
    _display_host,
    _normalize_host,
    _validate_bind_args,
    acquire_instance_lock as _acquire_lock_shared,
    bind_http_server,
    cleanup_own_daemon_state as _cleanup_own_daemon_state_shared,
    is_loopback_host,
    load_live_daemon_state as _load_live_daemon_state_shared,
    stop_daemon as _stop_daemon_shared,
)
from paths import private_dir, user_home, write_private  # noqa: E402
from processes import process_identity, stop_owned_process  # noqa: E402

_DAEMON_ENV = '_COLABLLM_WEB_DAEMON'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='colabllm', allow_abbrev=False)
    parser.add_argument('--command-timeout', type=positive_seconds, help='外部命令超时秒数')
    parser.add_argument('--account', metavar='ACCOUNT', type=validate_account_syntax, help='默认 Colab 账户名或邮箱（全局选项）')
    parser.add_argument('--debug', action='store_true', help='输出异常堆栈')
    parser.add_argument('--quiet', action='store_true', help='静默诊断输出')
    parser.add_argument('--no-color', action='store_true', help='关闭彩色诊断输出')

    commands = parser.add_subparsers(dest='command', required=True)
    web = commands.add_parser('web', help='启动本地可视化 Web 控制台')
    web.add_argument('-a', '--account', default=argparse.SUPPRESS, metavar='ACCOUNT', type=validate_account,
                     help='默认使用的 Colab 账户名或邮箱')
    web.add_argument('--host', default='127.0.0.1', help='监听地址（默认 127.0.0.1）')
    web.add_argument('-p', '--port', type=int, default=8765, help='监听端口（默认 8765，0 表示随机空闲端口）')
    web.add_argument('--strict-port', action='store_true', help='端口占用时直接报错，不自动顺延')
    web.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    web.add_argument('--read-only', action='store_true',
                     help='只读模式：禁止启停实例、运行任务、修改配置或读取密钥')
    web.add_argument('--allow-remote', action='store_true',
                     help='允许绑定非本机回环地址（仍强制启用会话与 CSRF 校验）')
    mode = web.add_mutually_exclusive_group()
    mode.add_argument('-d', '--detach', action='store_true',
                      help='在后台守护运行 Web 控制台，启动后立即返回终端')
    mode.add_argument('--status', action='store_true',
                      help='查看后台运行的 Web 控制台状态与访问链接')
    mode.add_argument('--stop', action='store_true',
                      help='安全停止后台运行的 Web 控制台')
    web.add_argument('--json', action='store_true',
                     help='以 JSON 输出结果（配合 --detach、--status 或 --stop）')
    commands.metavar = '{web}'
    return parser


def _should_auto_open(no_open: bool) -> bool:
    if no_open:
        return False
    if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY'):
        return False
    if os.environ.get('BROWSER') == 'true':
        return False
    if sys.platform.startswith('linux') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return False
    return True


def _daemon_paths() -> tuple[Path, Path, Path, Path]:
    web_dir = user_home() / 'web'
    return web_dir, web_dir / 'state.json', web_dir / 'web.log', web_dir / 'instance.lock'


def _write_private_json(path: Path, payload: dict) -> None:
    write_private(path, payload)


def _load_live_daemon_state(*, unlink_stale: bool = True) -> dict | None:
    _, state_path, _, _ = _daemon_paths()
    return _load_live_daemon_state_shared(state_path, unlink_stale=unlink_stale)


def _cleanup_own_daemon_state(pid: int, identity: str) -> None:
    _, state_path, _, _ = _daemon_paths()
    _cleanup_own_daemon_state_shared(state_path, pid, identity)


def _already_running_error(st: dict | None) -> CliError:
    if isinstance(st, dict) and st.get('pid') and st.get('auth_url'):
        is_fg = st.get('mode') == 'foreground'
        mode_label = '前台模式' if is_fg else '后台模式'
        stop_hint = 'colabllm web --stop（或在原终端按 Ctrl+C）' if is_fg else 'colabllm web --stop'
        return CliError(
            f"ColabLLM Web 控制台已在运行 (pid {st['pid']}，{mode_label})：{st['auth_url']}\n"
            f'如需重启或切换运行模式，请先执行：{stop_hint}'
        )
    return CliError(
        'ColabLLM Web 控制台已有实例正在启动或运行；请执行 colabllm web --status 查看或 colabllm web --stop 停止'
    )


def _acquire_instance_lock(*, probe_only: bool = False):
    web_dir, state_path, _, lock_path = _daemon_paths()
    return _acquire_lock_shared(
        web_dir,
        lock_path,
        state_path,
        already_running_error=_already_running_error,
        probe_only=probe_only,
    )


def _public_daemon_payload(st: dict) -> dict:
    return {'running': True, **{k: v for k, v in st.items() if k != 'identity'}}


def _print_access_notices(*, auth_url: str, actual_host: str, actual_port: int, read_only: bool, prefix: str) -> None:
    print(f'{prefix}：{auth_url}', flush=True)
    if read_only:
        print('当前模式：只读观察 (--read-only)，所有启停、配置修改与密钥读取已禁用。', flush=True)
    if not is_loopback_host(actual_host):
        print('警告：当前监听非回环地址，请仅在受信任网络或 SSH 隧道中使用并妥善保管启动链接。', file=sys.stderr, flush=True)
    if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY'):
        print(f'检测到 SSH 会话；如需本地访问，请在客户端执行：ssh -N -L {actual_port}:127.0.0.1:{actual_port} <host>', flush=True)


def create_bound_server(
    host: str,
    port: int,
    *,
    strict_port: bool = False,
    read_only: bool = False,
    default_account: str | None = None,
    allow_remote: bool = False,
):
    from colabllm.web_ui.server import WebConsoleServer

    return bind_http_server(
        host,
        port,
        server_factory=lambda addr: WebConsoleServer(
            addr,
            read_only=read_only,
            default_account=default_account,
            allow_remote=allow_remote,
        ),
        allow_remote=allow_remote,
        strict_port=strict_port,
        port_span=25,
        min_port=0,
    )


def _cmd_status(output_json: bool) -> int:
    st = _load_live_daemon_state()
    if st is None:
        if output_json:
            print(json.dumps({'running': False}, ensure_ascii=False))
        else:
            print('Web 控制台未运行。')
        return 0
    if output_json:
        print(json.dumps(_public_daemon_payload(st), ensure_ascii=False, indent=2))
        return 0
    run_mode = '前台模式' if st.get('mode') == 'foreground' else '后台模式'
    perm_label = '只读观察 (--read-only)' if st.get('read_only') else '标准读写'
    print(f"Web 控制台运行中 (pid {st['pid']}，{run_mode})")
    print(f"访问地址：{st['auth_url']}")
    print(f'模式：{perm_label}')
    if st.get('started'):
        print(f"启动时间：{st['started']}")
    if st.get('log_path'):
        print(f"日志文件：{st['log_path']}")
    return 0


def _cmd_stop(output_json: bool) -> int:
    _, state_path, _, _ = _daemon_paths()
    pid = _stop_daemon_shared(state_path, timeout=5)
    if pid is None:
        if output_json:
            print(json.dumps({'stopped': False, 'running': False}, ensure_ascii=False))
        else:
            print('Web 控制台未运行。')
        return 0
    if output_json:
        print(json.dumps({'stopped': True, 'running': False, 'pid': pid}, ensure_ascii=False))
    else:
        print(f'已停止 Web 控制台 (pid {pid})。')
    return 0


def _build_daemon_child_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, str(Path(__file__).resolve()), '--no-color']
    if getattr(args, 'command_timeout', None) is not None:
        cmd.extend(['--command-timeout', str(args.command_timeout)])
    if getattr(args, 'debug', False):
        cmd.append('--debug')
    if getattr(args, 'quiet', False):
        cmd.append('--quiet')
    cmd.extend(['web', '--host', args.host, '--port', str(args.port), '--no-open'])
    if getattr(args, 'account', None):
        cmd.extend(['--account', args.account])
    if args.strict_port:
        cmd.append('--strict-port')
    if args.read_only:
        cmd.append('--read-only')
    if args.allow_remote:
        cmd.append('--allow-remote')
    return cmd


def _start_detached(args: argparse.Namespace) -> int:
    _validate_bind_args(args.host, args.port, args.allow_remote)
    existing = _load_live_daemon_state()
    if existing is not None:
        raise _already_running_error(existing)
    _acquire_instance_lock(probe_only=True)
    web_dir, state_path, log_path, _ = _daemon_paths()
    private_dir(web_dir)
    state_path.unlink(missing_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    log_path.chmod(0o600)

    cmd = _build_daemon_child_cmd(args)
    env = {**os.environ, _DAEMON_ENV: '1'}
    with log_path.open('ab') as logfile:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

    deadline = time.monotonic() + 10.0
    st: dict | None = None
    while time.monotonic() < deadline:
        loaded = _load_live_daemon_state(unlink_stale=False)
        if loaded is not None and loaded.get('pid') == proc.pid:
            st = loaded
            break
        if proc.poll() is not None:
            err_text = log_path.read_text(encoding='utf-8', errors='replace').strip()
            raise CliError(err_text or f'后台 Web 控制台启动失败 (exit {proc.returncode})')
        time.sleep(0.03)

    if st is None:
        ident = process_identity(proc.pid)
        if ident:
            stop_owned_process(proc.pid, ident, group=True, timeout=3)
        elif proc.poll() is None:
            proc.terminate()
        raise CliError(f'等待后台 Web 控制台就绪超时；请查看日志 {log_path}')

    if args.json:
        print(json.dumps(_public_daemon_payload(st), ensure_ascii=False, indent=2))
    else:
        _print_access_notices(
            auth_url=st['auth_url'],
            actual_host=st['host'],
            actual_port=int(st['port']),
            read_only=bool(st.get('read_only')),
            prefix=f"ColabLLM Web 控制台已在后台启动 (pid {st['pid']})",
        )
        print(f'日志文件：{log_path}', flush=True)
        print('查看状态：colabllm web --status  |  停止服务：colabllm web --stop', flush=True)

    if _should_auto_open(args.no_open):
        webbrowser.open(st['auth_url'])
    return 0


def _serve_single_instance(args: argparse.Namespace, *, is_daemon_child: bool) -> int:
    _validate_bind_args(args.host, args.port, args.allow_remote)
    existing = _load_live_daemon_state()
    if existing is not None:
        raise _already_running_error(existing)

    lock_file = _acquire_instance_lock(probe_only=False)
    server = None
    own_pid = os.getpid()
    own_identity = None
    serve_thread = None
    shutdown_event = threading.Event()
    prev_sigterm = None
    try:
        own_identity = process_identity(own_pid)
        if not own_identity:
            raise CliError('无法获取当前进程身份标识')

        server = create_bound_server(
            args.host,
            args.port,
            strict_port=args.strict_port,
            read_only=args.read_only,
            default_account=getattr(args, 'account', None),
            allow_remote=args.allow_remote,
        )
        actual_host, actual_port = server.server_address[:2]
        disp_host = _display_host(actual_host)
        base_url = f'http://{disp_host}:{actual_port}'
        auth_url = f'{base_url}/auth?token={server.bootstrap_token}'

        _, state_path, log_path, _ = _daemon_paths()
        _write_private_json(state_path, {
            'pid': own_pid,
            'identity': own_identity,
            'mode': 'background' if is_daemon_child else 'foreground',
            'host': disp_host,
            'port': actual_port,
            'base_url': base_url,
            'auth_url': auth_url,
            'read_only': bool(args.read_only),
            'allow_remote': bool(args.allow_remote),
            'account': getattr(args, 'account', None),
            'started': time.strftime('%Y-%m-%d %H:%M:%S'),
            'log_path': str(log_path) if is_daemon_child else None,
        })

        if is_daemon_child:
            print(f'ColabLLM Web 控制台后台服务已就绪：{base_url} (pid {own_pid})', flush=True)
        else:
            _print_access_notices(
                auth_url=auth_url,
                actual_host=actual_host,
                actual_port=actual_port,
                read_only=bool(args.read_only),
                prefix='ColabLLM Web 控制台已启动',
            )
            print('按 Ctrl+C 退出 Web 控制台（已启动的后台保活服务不受影响）。', flush=True)

        serve_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.25}, daemon=True)
        serve_thread.start()

        if not is_daemon_child and _should_auto_open(args.no_open):
            timer = threading.Timer(0.15, webbrowser.open, args=(auth_url,))
            timer.daemon = True
            timer.start()

        def _request_stop(_signum, _frame):
            shutdown_event.set()

        prev_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _request_stop)
        try:
            while not shutdown_event.wait(0.5):
                if not serve_thread.is_alive():
                    break
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        if prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)
        if server is not None:
            server.shutdown()
            if serve_thread is not None:
                serve_thread.join(timeout=3.0)
            server.server_close()
        if own_identity:
            _cleanup_own_daemon_state(own_pid, own_identity)
        with contextlib.suppress(OSError):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, 'account', None) is not None:
        args.account = validate_account(args.account)
    is_daemon_child = os.environ.get(_DAEMON_ENV) == '1'
    if not is_daemon_child:
        if args.json and not (args.detach or args.status or args.stop):
            raise UsageError('--json 仅支持配合 --detach、--status 或 --stop 使用')
        if args.status:
            return _cmd_status(args.json)
        if args.stop:
            return _cmd_stop(args.json)
        if args.detach:
            return _start_detached(args)

    return _serve_single_instance(args, is_daemon_child=is_daemon_child)


if __name__ == '__main__':
    run_cli(main)
