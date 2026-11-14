#!/usr/bin/env python3
"""Local visual Web console for managing ColabLLM runtimes."""
from __future__ import annotations

import argparse
import errno
import ipaddress
import os
from pathlib import Path
import signal
import sys
import threading
import webbrowser

_PKG_DIR = Path(__file__).resolve().parent
_LIB_DIR = _PKG_DIR / 'lib'
_SRC_DIR = _PKG_DIR.parent
for _path in (str(_LIB_DIR), str(_SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from cli_support import CliError, UsageError, positive_seconds, run_cli  # noqa: E402
from colab_accounts import validate_account  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='colabllm', allow_abbrev=False)
    parser.add_argument('--command-timeout', type=positive_seconds, help='外部命令超时秒数')
    parser.add_argument('--account', metavar='ACCOUNT', type=validate_account, help='Colab 账户名或邮箱')
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
    commands.metavar = '{web}'
    return parser


def _normalize_host(raw_host: str) -> str:
    host = (raw_host or '').strip()
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    if not host:
        raise UsageError('监听地址不能为空')
    return host


def is_loopback_host(host: str) -> bool:
    normalized = _normalize_host(host)
    if normalized.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _display_host(host: str) -> str:
    normalized = _normalize_host(host)
    if normalized in ('0.0.0.0', '::'):
        return '127.0.0.1' if normalized == '0.0.0.0' else '[::1]'
    if ':' in normalized:
        return f'[{normalized}]'
    return normalized


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

    if not (0 <= port <= 65535):
        raise UsageError('端口号必须在 0–65535 之间')
    normalized_host = _normalize_host(host)
    if not is_loopback_host(normalized_host) and not allow_remote:
        raise UsageError('默认仅允许绑定本机回环地址；若确需监听其他地址，请显式指定 --allow-remote')

    attempts = [port] if (strict_port or port == 0) else [port + offset for offset in range(25) if port + offset <= 65535]
    last_error: OSError | None = None
    for candidate_port in attempts:
        try:
            return WebConsoleServer(
                (normalized_host, candidate_port),
                read_only=read_only,
                default_account=default_account,
                allow_remote=allow_remote,
            )
        except OSError as err:
            last_error = err
            if err.errno != errno.EADDRINUSE or strict_port or port == 0:
                raise CliError(f'无法监听 {normalized_host}:{candidate_port}: {err.strerror or err}') from None
    raise CliError(f'从 {port} 起连续端口均已被占用：{last_error}')


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
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

    print(f'ColabLLM Web 控制台已启动：{auth_url}', flush=True)
    if args.read_only:
        print('当前模式：只读观察 (--read-only)，所有启停、配置修改与密钥读取已禁用。', flush=True)
    if not is_loopback_host(actual_host):
        print('警告：当前监听非回环地址，请仅在受信任网络或 SSH 隧道中使用并妥善保管启动链接。', file=sys.stderr, flush=True)
    if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY'):
        print(f'检测到 SSH 会话；如需本地访问，请在客户端执行：ssh -N -L {actual_port}:127.0.0.1:{actual_port} <host>', flush=True)
    print('按 Ctrl+C 退出 Web 控制台（已启动的后台保活服务不受影响）。', flush=True)

    shutdown_event = threading.Event()
    serve_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.25}, daemon=True)
    serve_thread.start()

    if _should_auto_open(args.no_open):
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
    finally:
        signal.signal(signal.SIGTERM, prev_sigterm)
        server.shutdown()
        serve_thread.join(timeout=3.0)
        server.server_close()
    return 0


if __name__ == '__main__':
    run_cli(main)
