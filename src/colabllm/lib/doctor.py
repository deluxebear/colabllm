"""Bounded, read-only environment diagnostics. Offline by default."""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from cli_support import command_timeout, positive_seconds
from colabsvc import STATE_ROOT, load_tunnel_config


def diagnose(*, transport=None, no_drive=False, online=False, timeout=10):
    timeout = min(positive_seconds(timeout), command_timeout())
    checks = []
    def add(name, status, message, required=True):
        checks.append(dict(name=name, status=status, message=message, required=required))
    def command(name, args, message, required=True):
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                # A successful no-op browser prevents webbrowser from falling back to a real browser.
                timeout=timeout, env={**os.environ, 'BROWSER': 'true', 'NO_COLOR': '1'})
            add(name, 'pass' if result.returncode == 0 else 'fail', message if result.returncode else '可用', required)
        except subprocess.TimeoutExpired:
            add(name, 'fail', f'检查超时（{timeout:g} 秒）；{message}', required)
        except OSError:
            add(name, 'fail', message, required)
    add('python', 'pass' if sys.version_info >= (3, 12) else 'fail', f'Python {sys.version.split()[0]}；要求 3.12+')
    config = {}
    try:
        config = load_tunnel_config()
        add('config', 'pass', str(STATE_ROOT / 'tunnel-config.json'))
    except (OSError, ValueError, RuntimeError):
        add('config', 'fail', '无法读取配置；请检查 tunnel-config.json 格式及权限')
    effective_domain = os.environ.get('COLABSVC_BASE_DOMAIN') or config.get('base_domain')
    selected = transport or ('frp' if (config.get('frp') or {}).get('enabled') else ('cloudflare' if effective_domain else 'quick'))
    needs_cf = selected == 'frp' or (selected == 'cloudflare' and bool(transport == 'cloudflare' or effective_domain))
    required = ['colab'] + (['cf'] if needs_cf else []) + (['ssh'] if selected == 'frp' else [])
    for executable in required:
        add(executable, 'pass' if shutil.which(executable) else 'fail',
            '已找到' if shutil.which(executable) else f'未找到 {executable}；请按 README 安装并加入 PATH')
    if selected == 'frp':
        from frp_backend import credentials_path
        frp = config.get('frp') or {}
        has_cfg = bool(frp.get('server_ip') and frp.get('ssh') and effective_domain)
        has_creds = bool(has_cfg and credentials_path(STATE_ROOT, frp).is_file())
        if has_cfg and has_creds:
            add('frp-config', 'pass', 'FRP 配置与本地凭据就绪')
        elif has_cfg:
            add('frp-config', 'fail', '缺少本地 FRP 凭据文件；请重新执行 colabllm frp-config')
        else:
            add('frp-config', 'fail', 'FRP 需要 server_ip、ssh 和 base_domain；使用 colabllm frp-config 配置')
    if shutil.which('colab'):
        command('colab-version', ['colab', 'version'], '无法运行 colab version；请检查 Colab CLI 安装')
        if not no_drive:
            command('drive-extension', ['colab', 'dfs', '--help'], '缺少或无法运行 Drive 扩展；请按 README 安装 colab dfs')
    if not no_drive:
        credentials = Path.home() / '.config/colab-cli/dfs_credentials.json'
        add('drive-consent', 'pass' if credentials.is_file() else 'warn',
            '发现 Drive 授权文件（离线检查）' if credentials.is_file() else '未发现 Drive 授权文件；请执行 colab dfs consent', False)
    # Inspect paths without creating or changing any files.
    parent = STATE_ROOT
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    add('state-writable', 'pass' if os.access(parent, os.W_OK | os.X_OK) else 'fail', str(STATE_ROOT))
    if STATE_ROOT.exists():
        private = stat.S_IMODE(STATE_ROOT.stat().st_mode) & 0o077 == 0
        add('state-permissions', 'pass' if private else 'warn',
            '用户目录权限私有' if private else '建议将用户目录权限设置为 0700', False)
    if online:
        if shutil.which('colab'):
            command('colab-auth', ['colab', 'sessions'], '无法只读查询会话；请先交互执行 colab sessions 完成授权')
        if 'cf' in required and shutil.which('cf'):
            command('cloudflare-auth', ['cf', 'zones', 'list'], '无法查询 DNS Zone；请执行 cf auth login 并检查权限')
        target = (config.get('frp') or {}).get('ssh') if selected == 'frp' else None
        if target and shutil.which('ssh'):
            command('ssh-auth', ['ssh', '-o', 'BatchMode=yes', '-o', f'ConnectTimeout={max(1, int(timeout))}', '--', target, 'true'],
                    'SSH 非交互认证失败；请先配置密钥和已核实的主机指纹')
    else:
        add('authentication', 'warn', '尚未联网验证授权；使用 doctor --online 做限时只读检查', False)
    ok = not any(item['required'] and item['status'] == 'fail' for item in checks)
    return dict(ok=ok, transport=selected, checks=checks)
