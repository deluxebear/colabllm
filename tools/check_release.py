#!/usr/bin/env python3
"""Check the release tag against package metadata and extract its changelog entry."""
import argparse
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def release_notes(root, tag):
    version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version):
        raise ValueError('Release version must use MAJOR.MINOR.PATCH')
    if tag != 'v' + version:
        raise ValueError(f'Tag {tag!r} does not match package version v{version}')
    changelog = (root / 'CHANGELOG.md').read_text(encoding='utf-8')
    entries = re.findall(r'^## \[' + re.escape(version) + r'\] - (\d{4}-\d{2}-\d{2})\n(.*?)(?=^## |\Z)',
                         changelog, re.M | re.S)
    if len(entries) != 1 or not entries[0][1].strip():
        raise ValueError(f'CHANGELOG.md needs exactly one nonempty dated entry for {version}')
    return entries[0][1].strip() + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--notes-output', type=Path)
    args = parser.parse_args()
    try:
        notes = release_notes(ROOT, args.tag)
    except ValueError as error:
        parser.error(str(error))
    if args.notes_output:
        args.notes_output.write_text(notes, encoding='utf-8')
    print(f'Release {args.tag} matches package metadata and changelog')


if __name__ == '__main__':
    main()
