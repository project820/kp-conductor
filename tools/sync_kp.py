#!/usr/bin/env python3
"""Rebuild the bundled KP copy from a tagged upstream release (stdlib only)."""
import argparse
import importlib.util
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = 'https://github.com/project820/korean-protocol'
NEXT = ('next: bump skills/kp-conductor/VERSION and add a CHANGELOG section '
        '(RELEASING.md: \ud3ec\ud568\ud55c KP \uc0ac\ubcf8\uc744 \ubc14\uafb8\uba74 '
        'Conductor \ubc84\uc804\ub3c4 \uc62c\ub9b0\ub2e4)')
VERSION_FILES = ('README.md', 'NOTICE.md', 'DESIGN.md', 'skills/kp-conductor/scripts/kp_lint.py')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='upstream checkout path or git URL')
    parser.add_argument('--tag', required=True, help='release tag vMAJOR.MINOR.PATCH')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--dest', default=str(ROOT / 'skills/korean-protocol'), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    spec = importlib.util.spec_from_file_location('kp_conductor', ROOT / 'skills/kp-conductor/scripts/conductor.py')
    cx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cx)
    if not cx.KP_TAG.fullmatch(args.tag):
        raise SystemExit('invalid KP release tag: ' + args.tag)
    # Local paths supply release bytes; PINNED keeps the public upstream URL.
    source = str(Path(args.repo).resolve()) if Path(args.repo).exists() else args.repo
    if not source or source.startswith('-'):
        raise SystemExit('invalid KP repository: ' + source)
    dest = cx.owned_path(Path(args.dest).absolute())
    # Stage next to the destination so the final renames never cross file systems.
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sync-kp-', dir=str(dest.parent)) as directory:
        staging = Path(directory)
        commit = cx.kp_clone(source, args.tag, staging / 'src')
        build = staging / 'build'
        cx.kp_build_copy(staging / 'src', build, args.tag, commit, SOURCE)
        count = sum(path.is_file() for path in build.rglob('*'))
        if args.dry_run:
            print('kp sync dry-run: %s (%d files)' % (args.tag, count))
        elif dest.exists():
            # The tracked copy stays in git history; the old folder is removed with the staging folder.
            cx.kp_replace(build, dest, staging / 'previous')
            print('kp sync: %s (%d files)' % (args.tag, count))
        else:
            os.rename(str(build), str(dest))
            print('kp sync: %s (%d files)' % (args.tag, count))
    print(NEXT)
    print('next: update the KP version text in ' + ', '.join(VERSION_FILES))


if __name__ == '__main__':
    main()
