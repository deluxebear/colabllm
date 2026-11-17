"""Bounded, read-only environment diagnostics. Offline by default."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from cli_support import CliError, UsageError, command_timeout, positive_seconds
from colab_accounts import (
    account_dfs_credentials_path,
    colab_subprocess_env,
    load_accounts_registry,
    resolve_effective_account,
    resolve_effective_drive_account,
    validate_account,
)
from colabsvc import STATE_ROOT, load_tunnel_config
from instance_discovery import discover_kaggle_service_pairs, list_raw_instances
from kaggle_accounts import (
    kaggle_subprocess_env,
    load_kaggle_accounts_registry,
    resolve_effective_kaggle_account,
    validate_kaggle_account,
)
from paths import user_home
from processes import runner_alive

_INITIAL_STATE_ROOT = STATE_ROOT


def _effective_state_root() -> Path:
    return user_home() if STATE_ROOT == _INITIAL_STATE_ROOT else Path(STATE_ROOT)


def diagnose(*, transport=None, no_drive=False, online=False, timeout=10, account=None, drive_account=None, provider='colab'):
    if provider not in ('colab', 'kaggle', 'all'):
        raise UsageError(f'不支持的诊断平台: {provider!r}（可选 colab | kaggle | all）')
    timeout = min(positive_seconds(timeout), command_timeout())
    state_root = _effective_state_root()
    checks = []
    def add(name, status, message, required=True):
        checks.append(dict(name=name, status=status, message=message, required=required))

    check_colab = provider in ('colab', 'all')
    check_kaggle = provider in ('kaggle', 'all')

    registry = None
    account_error = None
    resolved_account = None
    resolved_drive = None
    if check_colab:
        try:
            acct_colab = validate_account(account) if account is not None else None
            registry = load_accounts_registry()
            resolved_account, _ = resolve_effective_account(explicit=acct_colab, registry=registry)
            if not no_drive:
                resolved_drive, _ = resolve_effective_drive_account(
                    resolved_account,
                    explicit_drive=drive_account,
                    registry=registry,
                )
        except (CliError, UsageError) as err:
            account_error = str(err)
    extra_env = colab_subprocess_env(resolved_account, resolved_drive) or {}

    k_registry = None
    k_account_error = None
    k_resolved_account = None
    if check_kaggle:
        try:
            acct_kaggle = validate_kaggle_account(account) if account is not None else None
            k_registry = load_kaggle_accounts_registry()
            k_resolved_account, _ = resolve_effective_kaggle_account(explicit=acct_kaggle, registry=k_registry)
        except (CliError, UsageError) as err:
            k_account_error = str(err)

    def command(name, args, message, required=True, use_colab_env=False, custom_env=None, check_stderr_marker=None):
        try:
            if custom_env is not None:
                env = custom_env
            else:
                env = {**os.environ, **(extra_env if use_colab_env else {}), 'BROWSER': 'true', 'NO_COLOR': '1'}
            result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                # A successful no-op browser prevents webbrowser from falling back to a real browser.
                timeout=timeout, env=env)
            if result.returncode != 0:
                add(name, 'fail', message, required)
                return None
            if check_stderr_marker and check_stderr_marker in (result.stderr or ''):
                add(name, 'fail', message, required)
                return None
            add(name, 'pass', '可用', required)
            return result
        except subprocess.TimeoutExpired:
            add(name, 'fail', f'检查超时（{timeout:g} 秒）；{message}', required)
        except OSError:
            add(name, 'fail', message, required)
        return None

    add('python', 'pass' if sys.version_info >= (3, 12) else 'fail', f'Python {sys.version.split()[0]}；要求 3.12+')

    if check_colab:
        if account_error:
            add('colab-account', 'fail', account_error, True)
        elif registry is not None:
            if not (registry.get('accounts') or {}) and not resolved_account:
                add('colab-account', 'warn', '已初始化多账户配置，但尚未登录任何 Colab 账户；请执行 colab account add <名称>', False)
            else:
                acct_msg = f'当前检查账户: {resolved_account}'
                if not no_drive and resolved_drive:
                    acct_msg += (
                        f'（共享 Drive 账户: {resolved_drive}）'
                        if resolved_drive != resolved_account
                        else f'（Drive 账户: {resolved_drive}）'
                    )
                add('colab-account', 'pass', acct_msg)
        elif resolved_account or (not no_drive and resolved_drive):
            acct_msg = f'已指定账户: {resolved_account}' if resolved_account else '已指定账户环境'
            if not no_drive and resolved_drive and resolved_drive != resolved_account:
                acct_msg += f'（共享 Drive 账户: {resolved_drive}）'
            add('colab-account', 'pass', acct_msg)

    if check_kaggle:
        if k_account_error:
            add('kaggle-account', 'fail', k_account_error, True)
        elif k_registry is not None:
            if not (k_registry.get('accounts') or {}) and not k_resolved_account:
                add('kaggle-account', 'warn', '已初始化 kaggle-ext 配置，但尚未登录账户；请执行 kaggle account login <名称>', False)
            else:
                add('kaggle-account', 'pass', f'当前检查 Kaggle 账户: {k_resolved_account}')
        elif k_resolved_account:
            add('kaggle-account', 'pass', f'已指定 Kaggle 账户: {k_resolved_account}')
        else:
            add('kaggle-account', 'warn', '未检测到 kaggle-ext accounts.json，将使用默认环境变量或等待登录后创建', False)

    config = {}
    try:
        config = load_tunnel_config()
        add('config', 'pass', str(state_root / 'tunnel-config.json'))
    except (OSError, ValueError, RuntimeError):
        add('config', 'fail', '无法读取配置；请检查 tunnel-config.json 格式及权限')
    effective_domain = os.environ.get('COLABSVC_BASE_DOMAIN') or config.get('base_domain')
    selected = transport or ('frp' if (config.get('frp') or {}).get('enabled') else ('cloudflare' if effective_domain else 'quick'))
    needs_cf = selected == 'frp' or (selected == 'cloudflare' and bool(transport == 'cloudflare' or effective_domain))
    required_tools = (['colab'] if check_colab else []) + (['kaggle'] if check_kaggle else []) + (['cf'] if needs_cf else []) + (['ssh'] if selected == 'frp' else [])
    for executable in required_tools:
        add(executable, 'pass' if shutil.which(executable) else 'fail',
            '已找到' if shutil.which(executable) else f'未找到 {executable}；请按 README 安装并加入 PATH')
    if selected == 'frp':
        from frp_backend import credentials_path
        frp = config.get('frp') or {}
        has_cfg = bool(frp.get('server_ip') and frp.get('ssh') and effective_domain)
        has_creds = bool(has_cfg and credentials_path(state_root, frp).is_file())
        if has_cfg and has_creds:
            add('frp-config', 'pass', 'FRP 配置与本地凭据就绪')
        elif has_cfg:
            add('frp-config', 'fail', '缺少本地 FRP 凭据文件；请重新执行 colabllm frp-config')
        else:
            add('frp-config', 'fail', 'FRP 需要 server_ip、ssh 和 base_domain；使用 colabllm frp-config 配置')

    if check_colab:
        if shutil.which('colab'):
            command('colab-version', ['colab', 'version'], '无法运行 colab version；请检查 Colab CLI 安装', use_colab_env=True)
            if not no_drive:
                command('drive-extension', ['colab', 'dfs', '--help'], '缺少或无法运行 Drive 扩展；请按 README 安装 colab-ext', use_colab_env=True)
        if not no_drive:
            credentials = account_dfs_credentials_path(resolved_account, resolved_drive, registry=registry)
            target_drv = resolved_drive or resolved_account
            if credentials.is_file():
                msg = f'发现 Drive 授权文件（离线检查，账户: {target_drv}）' if target_drv else '发现 Drive 授权文件（离线检查）'
                add('drive-consent', 'pass', msg, False)
            else:
                consent_cmd = f'colab -a {target_drv} drive consent' if target_drv else 'colab drive consent'
                add('drive-consent', 'warn', f'未发现 Drive 授权文件；请执行 {consent_cmd}', False)

    k_env = kaggle_subprocess_env(k_resolved_account) if check_kaggle else {}
    if check_kaggle and shutil.which('kaggle'):
        try:
            sess_res = subprocess.run(['kaggle', 'session', '--help'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=k_env)
            acct_res = subprocess.run(['kaggle', 'account', '--help'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=k_env)
            fail_open = '[kaggle-ext]' in (sess_res.stderr or '') or '[kaggle-ext]' in (acct_res.stderr or '')
            if sess_res.returncode == 0 and acct_res.returncode == 0 and not fail_open:
                add('kaggle-ext-cli', 'pass', 'kaggle session/account 扩展子命令可用')
            else:
                add('kaggle-ext-cli', 'fail', '未检测到可用 kaggle-ext 扩展或触发 [kaggle-ext] fail-open 回退；请确认已安装 kaggle-ext')
        except subprocess.TimeoutExpired:
            add('kaggle-ext-cli', 'fail', f'检查超时（{timeout:g} 秒）；无法验证 kaggle-ext 扩展命令')
        except OSError:
            add('kaggle-ext-cli', 'fail', '无法执行 kaggle 扩展命令')

        try:
            ext_doc = subprocess.run(['kaggle', 'ext', 'doctor', '--json'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=k_env)
            if ext_doc.returncode != 0:
                add('kaggle-ext-doctor', 'fail', 'kaggle ext doctor 检查失败；请运行 `kaggle ext doctor` 排查')
            else:
                parsed_doc = json.loads((ext_doc.stdout or '{}').strip() or '{}')
                if isinstance(parsed_doc, dict) and parsed_doc.get('ok', True):
                    add('kaggle-ext-doctor', 'pass', 'kaggle-ext 自检通过')
                else:
                    add('kaggle-ext-doctor', 'fail', 'kaggle ext doctor 报告扩展异常；请重新安装 kaggle-ext')
        except subprocess.TimeoutExpired:
            add('kaggle-ext-doctor', 'fail', f'检查超时（{timeout:g} 秒）；kaggle ext doctor 无响应')
        except (OSError, ValueError):
            add('kaggle-ext-doctor', 'fail', '无法解析 kaggle ext doctor JSON 输出；请更新 kaggle-ext')

    # Inspect paths without creating or changing any files.
    parent = state_root
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    add('state-writable', 'pass' if os.access(parent, os.W_OK | os.X_OK) else 'fail', str(state_root))
    if state_root.exists():
        private = stat.S_IMODE(state_root.stat().st_mode) & 0o077 == 0
        add('state-permissions', 'pass' if private else 'warn',
            '用户目录权限私有' if private else '建议将用户目录权限设置为 0700', False)
    if check_kaggle:
        k_state_dir = state_root / 'kaggle'
        if k_state_dir.exists():
            k_priv = stat.S_IMODE(k_state_dir.stat().st_mode) & 0o077 == 0
            add('kaggle-state-permissions', 'pass' if k_priv else 'warn',
                'Kaggle 状态目录权限私有' if k_priv else f'建议将 {k_state_dir} 权限设置为 0700', False)

    if online:
        if check_colab and shutil.which('colab') and not account_error:
            sessions_cmd = f'colab -a {resolved_account} sessions' if resolved_account else 'colab sessions'
            command('colab-auth', ['colab', 'sessions'], f'无法只读查询会话；请先交互执行 {sessions_cmd} 完成授权', use_colab_env=True)
        if check_kaggle and shutil.which('kaggle') and not k_account_error:
            try:
                st_res = subprocess.run(['kaggle', 'account', 'status', '--json'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=k_env)
                rows = json.loads((st_res.stdout or '[]').strip() or '[]') if st_res.returncode == 0 else None
                if st_res.returncode != 0 or not isinstance(rows, list) or not rows:
                    add('kaggle-auth', 'fail', '无法查询 Kaggle 账户在线状态；请先执行 kaggle account login 完成授权')
                else:
                    target_row = next(
                        (r for r in rows if isinstance(r, dict) and (r.get('account') == k_resolved_account if k_resolved_account else r.get('active'))),
                        rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None,
                    )
                    if target_row and target_row.get('authenticated') is True:
                        add('kaggle-auth', 'pass', f"Kaggle 账户 {target_row.get('account') or k_resolved_account} 在线授权正常")
                    else:
                        add('kaggle-auth', 'fail', '目标 Kaggle 账户未通过在线认证；请重新执行 kaggle account login')
            except subprocess.TimeoutExpired:
                add('kaggle-auth', 'fail', f'检查超时（{timeout:g} 秒）；kaggle account status 无响应')
            except (OSError, ValueError):
                add('kaggle-auth', 'fail', '无法解析 kaggle account status 输出')

            try:
                ls_res = subprocess.run(['kaggle', 'session', 'list', '--all-accounts', '--json'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=kaggle_subprocess_env(None))
                if ls_res.returncode == 0:
                    remote_sessions = json.loads((ls_res.stdout or '[]').strip() or '[]')
                    local_raw = list_raw_instances(state_root / 'kaggle' / 'services', discover_kaggle_service_pairs(), alive_fn=runner_alive)
                    live_local = {item['state'].get('kaggle_session') or item['name'] for item in local_raw if item['alive']}
                    orphans = [
                        f"{r.get('account') or '-'}:{r.get('name')}"
                        for r in (remote_sessions if isinstance(remote_sessions, list) else [])
                        if isinstance(r, dict) and r.get('state') == 'active' and r.get('name') not in live_local
                    ]
                    if orphans:
                        add('kaggle-orphan-sessions', 'warn', f"发现 {len(orphans)} 个本地无存活守护的云端活跃 Kaggle 会话: {', '.join(orphans[:5])}（可执行 kaggle session stop 清理）", False)
                    else:
                        add('kaggle-orphan-sessions', 'pass', '云端活跃 Kaggle 会话均与本地守护一致', False)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if 'cf' in required_tools and shutil.which('cf'):
            command('cloudflare-auth', ['cf', 'zones', 'list'], '无法查询 DNS Zone；请执行 cf auth login 并检查权限')
        target = (config.get('frp') or {}).get('ssh') if selected == 'frp' else None
        if target and shutil.which('ssh'):
            command('ssh-auth', ['ssh', '-o', 'BatchMode=yes', '-o', f'ConnectTimeout={max(1, int(timeout))}', '--', target, 'true'],
                    'SSH 非交互认证失败；请先配置密钥和已核实的主机指纹')
    else:
        add('authentication', 'warn', '尚未联网验证授权；使用 doctor --online 做限时只读检查', False)
    ok = not any(item['required'] and item['status'] == 'fail' for item in checks)
    report = dict(ok=ok, transport=selected, checks=checks)
    if provider != 'colab':
        report['provider'] = provider
    if check_colab and resolved_account:
        report['account'] = resolved_account
    if check_colab and not no_drive and resolved_drive:
        report['drive_account'] = resolved_drive
    if check_kaggle and k_resolved_account:
        report['kaggle_account'] = k_resolved_account
    return report
