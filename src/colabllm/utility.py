"""Environment diagnostics and offline shell completion commands."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent/'lib'))
from cli_support import positive_seconds, run_cli
from doctor import diagnose


def build_parser():
    parser = argparse.ArgumentParser(prog='colabllm')
    commands = parser.add_subparsers(dest='command', required=True)
    doctor = commands.add_parser('doctor', help='检查本地依赖、配置及授权')
    doctor.add_argument('--json', action='store_true', help='输出 JSON 检查结果')
    doctor.add_argument('--transport', choices=('quick', 'cloudflare', 'frp'), help='检查指定隧道后端；默认读取配置')
    doctor.add_argument('--no-drive', action='store_true', help='跳过 Drive 扩展检查')
    doctor.add_argument('--online', action='store_true', help='额外执行限时只读授权检查；不打开浏览器或创建会话')
    doctor.add_argument('--timeout', type=positive_seconds, default=10, help='每项命令检查的超时秒数，默认 10')
    completion = commands.add_parser('completion', help='输出 Shell 补全脚本')
    completion.add_argument('shell', choices=('bash', 'zsh', 'fish'))
    commands.metavar = '{doctor,completion}'
    hidden = commands.add_parser('_complete')
    hidden.add_argument('words', nargs=argparse.REMAINDER)
    return parser


def main():
    args = build_parser().parse_args()
    if args.command == 'doctor':
        result = diagnose(transport=args.transport, no_drive=args.no_drive, online=args.online, timeout=args.timeout)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for check in result['checks']:
                print(f"[{check['status'].upper()}] {check['name']}: {check['message']}")
        return 0 if result['ok'] else 1
    from completion import candidates, shell_script
    if args.command == 'completion':
        print(shell_script(args.shell), end='')
    else:
        words = args.words[1:] if args.words[:1] == ['--'] else args.words
        for value in candidates(words):
            print(value)
    return 0


if __name__ == '__main__':
    run_cli(main)
