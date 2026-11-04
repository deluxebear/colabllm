#!/usr/bin/env python3
"""Project-wide configuration shared by every Colab runtime service."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from cli_support import UsageError, positive_seconds, run_cli
import colabsvc
from colabsvc import cmd_tunnel_config, validate_domain


def build_parser():
    parser = argparse.ArgumentParser(description='本项目所有运行时共享的全局配置', allow_abbrev=False)
    parser.add_argument('--command-timeout', type=positive_seconds, help='外部命令超时秒数')
    parser.add_argument('--debug', action='store_true', help='输出异常堆栈')
    parser.add_argument('--quiet', action='store_true', help='静默诊断输出')
    parser.add_argument('--no-color', action='store_true', help='关闭彩色诊断输出')
    commands = parser.add_subparsers(dest='command', required=True)
    tunnel = commands.add_parser('tunnel-config', help='查看、设置或清除项目全局主域名')
    settings = tunnel.add_mutually_exclusive_group()
    settings.add_argument('--base-domain', metavar='DOMAIN', type=validate_domain,
                          help='所有服务共用的主域名，例如 example.com')
    settings.add_argument('--clear', action='store_true', help='清除全局主域名，默认使用动态 URL')
    frp = commands.add_parser('frp-config', help='配置并通过 SSH 安装 FRP 服务端；无参数时查看配置')
    frp.add_argument('--server-ip', help='FRP 服务器公网 IPv4/IPv6 地址')
    frp.add_argument('--ssh', help='SSH 目标，例如 root@frpip 或 ~/.ssh/config 中的别名')
    frp.add_argument('--base-domain', type=validate_domain, help='项目全局主域名，DNS 由 cf CLI 管理')
    frp.add_argument('--server-port', type=int, help='FRP 服务端端口，默认 7000')
    frp.add_argument('--disable', action='store_true', help='关闭全局 FRP，之后启动恢复使用 Cloudflare')
    commands.add_parser('frp-status', help='通过 SSH 检查 FRP/Caddy 服务和已分配路由')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'tunnel-config':
            cmd_tunnel_config(args)
        elif args.command == 'frp-config':
            from frp_backend import configure
            if args.disable and any(getattr(args, k) is not None for k in ('server_ip', 'ssh', 'base_domain', 'server_port')):
                parser.error('--disable 不能与服务器配置参数同时使用')
            result = configure(args, colabsvc.STATE_ROOT)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            from frp_backend import remote
            config = colabsvc.load_tunnel_config().get('frp')
            if not config:
                raise UsageError('尚未配置 FRP 服务端；请先运行 colabllm frp-config')
            print(json.dumps(remote(config, {'action': 'status'}), ensure_ascii=False, indent=2))
    except ValueError as error:
        if isinstance(error, UsageError):
            raise
        raise UsageError(str(error)) from None


if __name__ == '__main__':
    run_cli(main)
