#!/usr/bin/env python3
"""Sync canonical runtime guides into the portable user skill; --check is read-only."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
COMMON = 'runtime-common'


def runtime_names():
    return sorted(path.stem for path in (ROOT/'src/colabllm/services').glob('*.py')
                  if path.with_suffix('.ipynb').is_file())


def synchronize(check=False):
    failures = []
    for name in [COMMON, *runtime_names()]:
        source = ROOT/'docs'/(name+'.md')
        target = ROOT/'skills/colabllm/references'/(name+'.md')
        if not source.is_file():
            failures.append(f'Missing runtime guide: {source.relative_to(ROOT)}')
            continue
        content = '<!-- Generated from docs/' + name + '.md; edit the source and run tools/sync_runtime_docs.py. -->\n\n' + source.read_text(encoding='utf-8')
        if check:
            if not target.is_file() or target.read_text(encoding='utf-8') != content:
                failures.append(f'Outdated skill reference: {target.relative_to(ROOT)}')
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
    if failures:
        raise RuntimeError('\n'.join(failures))
    return len(runtime_names())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='fail if any guide is missing or out of sync')
    args = parser.parse_args()
    try:
        count = synchronize(args.check)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    print(f'{count} runtime guides and common operations ' + ('are synchronized' if args.check else 'synchronized'))
