"""Environment diagnostics and offline shell completion commands."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent/'lib'))
from cli_support import positive_seconds, run_cli
from colab_accounts import validate_account
from doctor import diagnose


def build_parser():
    parser = argparse.ArgumentParser(prog='colabllm', allow_abbrev=False)
    parser.add_argument('--command-timeout', type=positive_seconds, help='外部命令超时秒数')
    parser.add_argument('--account', metavar='ACCOUNT', type=validate_account, help='Colab 账户名或邮箱')
    parser.add_argument('--debug', action='store_true', help='输出异常堆栈')
    parser.add_argument('--quiet', action='store_true', help='静默诊断输出')
    parser.add_argument('--no-color', action='store_true', help='关闭彩色诊断输出')
    commands = parser.add_subparsers(dest='command', required=True)
    doctor = commands.add_parser('doctor', help='检查本地依赖、配置及授权')
    doctor.add_argument('--json', action='store_true', help='输出 JSON 检查结果')
    doctor.add_argument('-a', '--account', default=argparse.SUPPRESS, metavar='ACCOUNT', type=validate_account,
                        help='检查指定的 Colab 账户名或邮箱')
    doctor.add_argument('--drive-account', metavar='ACCOUNT', type=validate_account,
                        help='检查指定的共享 Google Drive 凭据账户')
    doctor.add_argument('--transport', choices=('quick', 'cloudflare', 'frp'), help='检查指定隧道后端；默认读取配置')
    doctor.add_argument('--no-drive', action='store_true', help='跳过 Drive 扩展检查')
    doctor.add_argument('--online', action='store_true', help='额外执行限时只读授权检查；不打开浏览器或创建会话')
    doctor.add_argument('--timeout', type=positive_seconds, default=10, help='每项命令检查的超时秒数，默认 10')
    completion = commands.add_parser('completion', help='输出 Shell 补全脚本')
    completion.add_argument('shell', choices=('bash', 'zsh', 'fish'))
    skill = commands.add_parser('skill', help='查看或导出随包提供的 Agent 技能')
    skill.add_argument('--output', metavar='DIR', help='将完整技能导出到 DIR/colabllm；不覆盖已有目录')
    commands.metavar = '{doctor,completion,skill}'
    hidden = commands.add_parser('_complete')
    hidden.add_argument('words', nargs=argparse.REMAINDER)
    return parser


def main():
    args = build_parser().parse_args()
    if args.command == 'doctor':
        result = diagnose(
            transport=args.transport,
            no_drive=args.no_drive,
            online=args.online,
            timeout=args.timeout,
            account=getattr(args, 'account', None),
            drive_account=args.drive_account,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for check in result['checks']:
                print(f"[{check['status'].upper()}] {check['name']}: {check['message']}")
        return 0 if result['ok'] else 1
    if args.command == 'skill':
        from skill_bundle import bundle_root, export_skill
        if args.output:
            print(export_skill(args.output))
        else:
            print((bundle_root() / 'SKILL.md').read_text(encoding='utf-8'), end='')
        return 0
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
