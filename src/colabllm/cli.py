#!/usr/bin/env python3
"""Unified CLI for packaged Colab model services (Python standard library only)."""
import math
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
GLOBAL_COMMANDS = ('tunnel-config', 'frp-config', 'frp-status')
UTILITY_COMMANDS = ('doctor', 'completion', 'skill')
_ACCOUNT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$')


def services():
    """Discover service pairs without importing or starting their implementation."""
    return {p.stem: p for p in sorted((ROOT / 'services').glob('*.py'))
            if p.with_suffix('.ipynb').is_file()}


def _valid_timeout(raw):
    try:
        val = float(raw)
        return math.isfinite(val) and val > 0
    except (TypeError, ValueError):
        return False


def _valid_account(raw):
    val = (raw or '').strip() if isinstance(raw, str) else ''
    return bool(val and '..' not in val and _ACCOUNT_RE.fullmatch(val))


def _package_version():
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version('colabllm')
    except PackageNotFoundError:
        pyproject = ROOT.parents[1] / 'pyproject.toml'
        if pyproject.is_file():
            import tomllib
            return tomllib.loads(pyproject.read_text(encoding='utf-8'))['project']['version']
        raise


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    flags = []
    while argv:
        if argv[0] in ('--debug', '--quiet', '--no-color'):
            flags.append(argv.pop(0))
        elif argv[0].startswith('--command-timeout='):
            arg = argv.pop(0)
            if not _valid_timeout(arg.split('=', 1)[1]):
                print('colabllm: --command-timeout 需要大于 0 的有限秒数', file=sys.stderr)
                return 2
            flags.append(arg)
        elif argv[0] == '--command-timeout':
            flags.append(argv.pop(0))
            if not argv or not _valid_timeout(argv[0]):
                print('colabllm: --command-timeout 需要大于 0 的有限秒数', file=sys.stderr)
                return 2
            flags.append(argv.pop(0))
        elif argv[0].startswith('--account='):
            arg = argv.pop(0)
            if not _valid_account(arg.split('=', 1)[1]):
                print('colabllm: --account 需要有效的 Colab 账户名或邮箱', file=sys.stderr)
                return 2
            flags.append(arg)
        elif argv[0] == '--account':
            flags.append(argv.pop(0))
            if not argv or not _valid_account(argv[0]):
                print('colabllm: --account 需要有效的 Colab 账户名或邮箱', file=sys.stderr)
                return 2
            flags.append(argv.pop(0))
        else:
            break
    if len(argv) > 1 and argv[0] == 'help':
        argv = [*argv[1:], '--help']
    available = services()
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print('用法: colabllm <运行时> <命令> [参数...]\n'
              '      colabllm <全局配置命令> [参数...]\n'
              '\n'
              '运行时: ' + ', '.join(available) + '\n'
              '全局命令: ' + ', '.join((*GLOBAL_COMMANDS, *UTILITY_COMMANDS)) + '\n\n'
              '示例:\n'
              '  colabllm unlimited-ocr up\n'
              '  colabllm unlimited-ocr up -a work --drive-account main\n'
              '  colabllm unlimited-ocr ocr ./documents -O ./results\n'
              '  colabllm qwen3-tts up -i 2\n'
              '  colabllm tunnel-config\n\n'
              '全局选项: --account ACCOUNT、--debug、--quiet、--no-color、--command-timeout SECONDS\n'
              'colabllm services 列出运行时；<运行时> --help 查看原有命令和参数。')
        return 0
    if argv == ['--version']:
        print('colabllm ' + _package_version())
        return 0
    command, *remaining = argv
    if command in ('services', '--list'):
        extra = [x for x in remaining if x not in ('--debug', '--quiet', '--no-color')]
        if extra:
            print('用法: colabllm services', file=sys.stderr)
            return 2
        print('\n'.join(available))
        return 0
    if command in available:
        script = available[command]
    elif command in (*UTILITY_COMMANDS, '_complete'):
        script = ROOT / 'utility.py'
        remaining = [command, *remaining]
    elif command in GLOBAL_COMMANDS:
        script = ROOT / 'control.py'
        remaining = [command, *remaining]
    else:
        print(f'未知运行时或命令: {command}\n使用 colabllm --help 查看可用命令。', file=sys.stderr)
        return 2
    # Replace this process: preserve signals, exit codes and the caller's cwd/local file paths.
    # Keep the actual service script as argv[0], since its detached runner uses that path.
    os.execv(sys.executable, [sys.executable, str(script), *flags, *remaining])


if __name__ == '__main__':
    sys.exit(main())
