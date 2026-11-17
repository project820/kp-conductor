#!/usr/bin/env python3
"""KP Conductor release helper.

check    VERSION, CHANGELOG.md and the release tags agree, and the commit carries a new version.
version  print the version recorded in a commit.
notes    print the CHANGELOG.md section of one version; the GitHub release body.

The rules are in RELEASING.md. Files are read from a git commit (default HEAD), so
uncommitted edits never decide what gets released.
"""
import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = 'skills/kp-conductor/VERSION'
CHANGELOG = 'CHANGELOG.md'
NUMBER = r'(0|[1-9][0-9]*)'
SEMVER = re.compile(r'%s[.]%s[.]%s' % (NUMBER, NUMBER, NUMBER))
HEADING = re.compile(r'## \[([^\]]+)\] - ([0-9]{4}-[0-9]{2}-[0-9]{2})')
TAG = re.compile(r'v(%s[.]%s[.]%s)' % (NUMBER, NUMBER, NUMBER))


class ReleaseError(Exception):
    pass


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root)] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        raise ReleaseError('git %s failed: %s' % (' '.join(args), result.stderr.strip()))
    return result.stdout


def key(version):
    return tuple(int(part) for part in version.split('.'))


def read_file(root, commit, name):
    try:
        return git(root, 'show', '%s:%s' % (commit, name))
    except ReleaseError:
        raise ReleaseError('%s is missing in %s' % (name, commit))


def parse_version(text):
    value = text.strip()
    if text != value + '\n' or not SEMVER.fullmatch(value):
        raise ReleaseError('%s must hold one line MAJOR.MINOR.PATCH, found %r' % (VERSION_FILE, text))
    return value


def parse_changelog(text):
    """Return [(version, date, body)] in file order and validate the structure."""
    sections = []
    for line in text.splitlines():
        if line.startswith('## '):
            match = HEADING.fullmatch(line.rstrip())
            if not match or not SEMVER.fullmatch(match.group(1)):
                raise ReleaseError('CHANGELOG.md heading must be "## [MAJOR.MINOR.PATCH] - YYYY-MM-DD": %s' % line)
            try:
                datetime.date.fromisoformat(match.group(2))
            except ValueError:
                raise ReleaseError('CHANGELOG.md has an invalid date: %s' % line)
            sections.append([match.group(1), match.group(2), []])
        elif sections:
            sections[-1][2].append(line)
    if not sections:
        raise ReleaseError('CHANGELOG.md has no release section')
    result = [(version, date, '\n'.join(body).strip()) for version, date, body in sections]
    for (newer, newer_date, _), (older, older_date, _) in zip(result, result[1:]):
        if key(newer) <= key(older):
            raise ReleaseError('CHANGELOG.md versions must go from newest to oldest: %s before %s' % (newer, older))
        if newer_date < older_date:
            raise ReleaseError('CHANGELOG.md dates must not go back: %s %s before %s %s' % (newer, newer_date, older, older_date))
    for version, _, body in result:
        if not any(line.lstrip().startswith('- ') for line in body.splitlines()):
            raise ReleaseError('CHANGELOG.md section %s needs at least one "- " item' % version)
    return result


def release_tags(root, remote=None):
    """Map version to the commit its tag points at."""
    if remote:
        lines = git(root, 'ls-remote', '--tags', remote).splitlines()
        found = {}
        for line in lines:
            sha, _, ref = line.partition('\t')
            name = ref[len('refs/tags/'):]
            peeled = name.endswith('^{}')
            match = TAG.fullmatch(name[:-3] if peeled else name)
            if match and (peeled or match.group(1) not in found):
                found[match.group(1)] = sha
        return found
    found = {}
    for name in git(root, 'tag', '--list', 'v*').split():
        match = TAG.fullmatch(name)
        if match:
            found[match.group(1)] = git(root, 'rev-list', '-n', '1', name).strip()
    return found


def is_ancestor(root, older, newer):
    """True when older is in the history of newer; an unknown commit is not."""
    result = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', older, newer],
                            capture_output=True, text=True)
    return result.returncode == 0


def check(root=ROOT, commit='HEAD', remote=None):
    sha = git(root, 'rev-parse', '--verify', commit + '^{commit}').strip()
    version = parse_version(read_file(root, sha, VERSION_FILE))
    sections = parse_changelog(read_file(root, sha, CHANGELOG))
    if sections[0][0] != version:
        raise ReleaseError('VERSION is %s but the newest CHANGELOG.md section is %s' % (version, sections[0][0]))
    tags = release_tags(root, remote)
    if version in tags:
        if tags[version] != sha:
            raise ReleaseError('v%s is already released from another commit; raise VERSION and add its notes' % version)
        return version, 'already released from this commit'
    # Compare with the releases this commit builds on. A later push released first does not
    # block a delayed run for an earlier commit.
    earlier = [found for found, tagged in tags.items() if is_ancestor(root, tagged, sha)]
    latest = max(earlier, key=key) if earlier else None
    if latest and key(version) <= key(latest):
        raise ReleaseError('VERSION %s must be greater than the latest release v%s' % (version, latest))
    return version, 'new release' + (' after v%s' % latest if latest else '')


def notes(root=ROOT, commit='HEAD', version=None):
    version = version or parse_version(read_file(root, commit, VERSION_FILE))
    for found, _, body in parse_changelog(read_file(root, commit, CHANGELOG)):
        if found == version:
            return body
    raise ReleaseError('CHANGELOG.md has no section for %s' % version)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='release.py')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('check', 'version', 'notes'):
        command = sub.add_parser(name)
        command.add_argument('--commit', default='HEAD')
        if name == 'check':
            command.add_argument('--remote', help='compare with the tags on this remote instead of local tags')
        if name == 'notes':
            command.add_argument('--version')
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            version, state = check(ROOT, args.commit, args.remote)
            print('release check: v%s ok (%s)' % (version, state))
        elif args.command == 'version':
            print(parse_version(read_file(ROOT, args.commit, VERSION_FILE)))
        else:
            print(notes(ROOT, args.commit, args.version))
    except ReleaseError as error:
        print('release check failed: %s' % error, file=sys.stderr)
        print('RELEASING.md: raise skills/kp-conductor/VERSION and write its CHANGELOG.md section before pushing.',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
