"""Offline completion from the real argparse registrations; never read credential contents."""
import argparse
from pathlib import Path
import re
import runpy

from cli_support import UsageError
from colabsvc import build_parser
from paths import user_home

ROOT = Path(__file__).resolve().parents[1]
GLOBAL = ('services', 'help', 'doctor', 'completion', 'skill', 'tunnel-config', 'frp-config', 'frp-status')
FLAGS = ('--help', '--version', '--debug', '--quiet', '--no-color', '--command-timeout')


def service_scripts():
    return {p.stem: p for p in (ROOT/'services').glob('*.py') if p.with_suffix('.ipynb').is_file()}


def instance_ids(service):
    directory = user_home() / 'services'
    if not directory.is_dir():
        return []
    prefix = service + '-'
    return sorted(p.name[len(prefix):] for p in directory.iterdir()
                  if p.is_dir() and p.name.startswith(prefix) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,63}', p.name[len(prefix):]))


def candidates(words):
    words = list(words) or ['']
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
    if not done:
        return sorted(value for value in (*scripts, *GLOBAL, *FLAGS) if value.startswith(prefix))
    service, *done = done
    if service == 'completion':
        return [shell for shell in ('bash', 'zsh', 'fish') if shell.startswith(prefix)]
    if service not in scripts:
        parser = runpy.run_path(str(ROOT/'utility.py'))['build_parser']() if service in ('doctor', 'skill') else None
        if service in ('tunnel-config', 'frp-config', 'frp-status'):
            # Global controller parsers are also shared with completion.
            parser = runpy.run_path(str(ROOT/'control.py'))['build_parser']()
        if parser is None:
            return []
        done.insert(0, service)
    else:
        module = runpy.run_path(str(scripts[service]))
        parser = build_parser(module['SERVICE'], module.get('configure_cli'))
    root_options = {flag: action for action in parser._actions for flag in action.option_strings}
    expected = None
    for token in done:
        if token == '--':
            return []
        if expected is not None:
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
    if expected is not None:
        if expected.dest == 'instance':
            values = instance_ids(service)
        else:
            values = list(expected.choices or ())
        return [flag_prefix + str(value) for value in values if str(value).startswith(prefix)]
    values = list(options)
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
        COMPREPLY+=("$candidate")
    done < <(command colabllm _complete -- "${COMP_WORDS[@]:1:COMP_CWORD}" 2>/dev/null)
}
complete -o default -F _colabllm_complete colabllm
''',
    'zsh': r'''#compdef colabllm
_colabllm_complete() {
    local -a candidates
    candidates=("${(@f)$(command colabllm _complete -- "${words[@]:1:$((CURRENT-1))}" 2>/dev/null)}")
    _describe 'colabllm' candidates || _files
}
compdef _colabllm_complete colabllm
''',
    'fish': r'''function __colabllm_candidates
    set -l words (commandline -opc)
    set -l current (commandline -ct)
    command colabllm _complete -- $words[2..-1] "$current" 2>/dev/null
end
complete -c colabllm -a '(__colabllm_candidates)'
''',
}


def shell_script(shell):
    try:
        return SCRIPTS[shell]
    except KeyError:
        raise UsageError('支持的 Shell：bash、zsh、fish') from None
