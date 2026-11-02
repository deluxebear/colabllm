#!/usr/bin/env python3
"""Project-wide configuration shared by every Colab runtime service."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from colabsvc import cmd_tunnel_config, validate_domain


def main(argv=None):
    parser = argparse.ArgumentParser(description='本项目所有运行时共享的全局配置')
    commands = parser.add_subparsers(dest='command', required=True)
    tunnel = commands.add_parser('tunnel-config', help='查看、设置或清除项目全局主域名')
    settings = tunnel.add_mutually_exclusive_group()
    settings.add_argument('--base-domain', metavar='DOMAIN', type=validate_domain,
                          help='所有服务共用的主域名，例如 example.com')
    settings.add_argument('--clear', action='store_true', help='清除全局主域名，默认使用动态 URL')
    args = parser.parse_args(argv)
    try:
        cmd_tunnel_config(args)
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
