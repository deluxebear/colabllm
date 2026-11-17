"""Offline completion from the real argparse registrations; never read credential contents."""
import argparse
from pathlib import Path
import re
import runpy

from cli_support import UsageError
from colab_accounts import list_account_names
from colabsvc import build_parser
from instance_discovery import list_instance_dir_names
from paths import user_home

ROOT = Path(__file__).resolve().parents[1]
GLOBAL = ('services', 'help', 'doctor', 'completion', 'skill', 'web', 'gateway', 'tunnel-config', 'frp-config', 'frp-status')
FLAGS = ('--help', '--version', '--account', '--debug', '--quiet', '--no-color', '--command-timeout')


def service_scripts():
    return {p.stem: p for p in (ROOT/'services').glob('*.py') if p.with_suffix('.ipynb').is_file()}


def instance_ids(service):
    return list_instance_dir_names(user_home() / 'services', service, service_scripts())


def _normalize_wordbreaks(words):
    """Rejoin ['--flag', '=', 'val'] or ['--flag', '='] produced by Bash COMP_WORDBREAKS."""
    merged = []
    idx = 0
    while idx < len(words):
        tok = words[idx]
        if tok == '=' and merged and merged[-1].startswith('-'):
            if idx + 1 < len(words):
                merged[-1] = merged[-1] + '=' + words[idx + 1]
                idx += 2
                continue
            merged[-1] = merged[-1] + '='
            idx += 1
            continue
        merged.append(tok)
        idx += 1
    return merged


def candidates(words):
    words = _normalize_wordbreaks(list(words) or [''])
    if words[0] == 'help' and len(words) > 1:
        words = words[1:]
    prefix, done = words[-1], words[:-1]
    scripts = service_scripts()
    # Leading global flags can precede the service name.
    while done and done[0].startswith('-'):
        flag = done.pop(0)
        if flag == '--command-timeout':
            if not done:
                return []
            done.pop(0)
        elif flag == '--account':
            if not done:
                return [value for value in list_account_names() if value.startswith(prefix)]
            done.pop(0)
    if not done:
        if prefix.startswith('--account='):
            val_prefix = prefix.split('=', 1)[1]
            return ['--account=' + value for value in list_account_names() if value.startswith(val_prefix)]
        return sorted(value for value in (*scripts, *GLOBAL, *FLAGS) if value.startswith(prefix))
    service, *done = done
    if service == 'completion':
        return [shell for shell in ('bash', 'zsh', 'fish') if shell.startswith(prefix)]
    if service not in scripts:
        parser = None
        append_gateway_token = False
        if service in ('doctor', 'skill'):
            parser = runpy.run_path(str(ROOT/'utility.py'))['build_parser']()
        elif service == 'web':
            parser = runpy.run_path(str(ROOT/'web.py'))['build_parser']()
        elif service == 'gateway':
            gw_mod = runpy.run_path(str(ROOT/'gateway.py'))
            if 'token' in done:
                parser = gw_mod['build_token_parser']()
            else:
                parser = gw_mod['build_parser']()
                append_gateway_token = True
        elif service in ('tunnel-config', 'frp-config', 'frp-status'):
            # Global controller parsers are also shared with completion.
            parser = runpy.run_path(str(ROOT/'control.py'))['build_parser']()
        if parser is None:
            return []
        done.insert(0, service)
    else:
        append_gateway_token = False
        module = runpy.run_path(str(scripts[service]))
        parser = build_parser(module['SERVICE'], module.get('configure_cli'))
    root_options = {flag: action for action in parser._actions for flag in action.option_strings}
    expected = None
    for token in done:
        if token == '--':
            return []
        if expected is not None:
            if expected.nargs == '?' and token.startswith('-'):
                expected = None
            else:
                expected = None
                continue
        options = {**root_options, **{flag: action for action in parser._actions for flag in action.option_strings}}
        if token.split('=', 1)[0] in options:
            action = options[token.split('=', 1)[0]]
            if '=' not in token and action.nargs != 0:
                expected = action
            continue
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction) and token in action.choices:
                parser = action.choices[token]
                break
    options = {**root_options, **{flag: action for action in parser._actions for flag in action.option_strings}}
    flag_prefix = ''
    if '=' in prefix and prefix.split('=', 1)[0] in options:
        flag, prefix = prefix.split('=', 1)
        expected = options[flag]
        flag_prefix = flag + '='
    if expected is not None and not (expected.nargs == '?' and prefix.startswith('-') and not flag_prefix):
        if expected.dest == 'instance':
            values = instance_ids(service)
        elif expected.dest in ('account', 'drive_account'):
            values = list_account_names()
        else:
            values = list(expected.choices or ())
        return [flag_prefix + str(value) for value in values if str(value).startswith(prefix)]
    values = list(options)
    if append_gateway_token:
        values.append('token')
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            values.extend(name for name in action.choices if not name.startswith('_'))
    return sorted(set(value for value in values if value.startswith(prefix)))


SCRIPTS = {
    'bash': r'''# colabllm Bash completion
_colabllm_complete() {
    local candidate
    COMPREPLY=()
    while IFS= read -r candidate; do
        [[ -n "$candidate" ]] && COMPREPLY+=("$candidate")
    done < <(command colabllm _complete -- "${COMP_WORDS[@]:1:COMP_CWORD}" 2>/dev/null)
}
complete -o default -F _colabllm_complete colabllm
''',
    'zsh': r'''#compdef colabllm
_colabllm_complete() {
    local out
    local -a candidates
    out="$(command colabllm _complete -- "${words[@]:1:$((CURRENT-1))}" 2>/dev/null)"
    candidates=(${(f)out})
    (( ${#candidates[@]} )) && _describe 'colabllm' candidates || _files
}
compdef _colabllm_complete colabllm
''',
    'fish': r'''function __colabllm_candidates
    set -l words (commandline -opc)
    set -l current (commandline -ct)
    command colabllm _complete -- $words[2..-1] "$current" 2>/dev/null
end
complete -c colabllm -f -a '(__colabllm_candidates)'
''',
}


def shell_script(shell):
    try:
        return SCRIPTS[shell]
    except KeyError:
        raise UsageError('支持的 Shell：bash、zsh、fish') from None
