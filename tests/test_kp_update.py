import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_HOME = tempfile.TemporaryDirectory(prefix='kp-test-home-')
os.environ['HOME'] = TEST_HOME.name
ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'skills/kp-conductor/scripts/conductor.py'
spec = importlib.util.spec_from_file_location('kp_conductor_module', SCRIPT)
cx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cx)
SYNC = ROOT / 'tools/sync_kp.py'
REQUIRED = ('LICENSE', 'NOTICE.md', 'lexicon/core.json',
            'lexicon/domains/software.json', 'specification/rules.json')
NOTE = 'Conductor 패키지에 넣은 고정 사본이다. 내용을 고치지 않는다. 갱신은 KP 릴리스를 받아 통째로 바꾼다.'


class KpUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kp-update-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.root / 'state'
        self.env = patch.dict(os.environ, {
            'HOME': str(self.home), 'CONDUCTOR_HOME': str(self.state),
            'GIT_AUTHOR_NAME': 'KP Test', 'GIT_AUTHOR_EMAIL': 'kp@localhost',
            'GIT_COMMITTER_NAME': 'KP Test', 'GIT_COMMITTER_EMAIL': 'kp@localhost',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = self.root / 'upstream'
        self.repo.mkdir()
        self.git('init')
        for relative in REQUIRED:
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative + '\n')
        self.skill = self.repo / 'skills/korean-protocol'
        (self.skill / 'references').mkdir(parents=True)
        (self.skill / 'SKILL.md').write_text('# KP v0.11.1\n')
        (self.skill / 'references/guide.md').write_text('guide\n')
        (self.skill / '__pycache__').mkdir()
        (self.skill / '__pycache__/ignored.pyc').write_bytes(b'ignored')
        (self.repo / 'unrelated.txt').write_text('not bundled\n')
        self.git('add', '.')
        self.git('commit', '-m', 'initial')
        self.git('tag', 'v0.11.1')
        self.old_commit = self.git('rev-parse', 'HEAD').strip()
        (self.skill / 'SKILL.md').write_text('# KP v0.12.0\n')
        self.git('add', '.')
        self.git('commit', '-m', 'next')
        self.git('tag', '-a', 'v0.12.0', '-m', 'next release')
        self.git('tag', 'v0.9.0')
        self.git('tag', 'v99.0.0-rc1')
        self.git('tag', 'v9.0')
        self.new_commit = self.git('rev-parse', 'HEAD').strip()
        self.bundled = self.home / '.codex/skills/korean-protocol'
        self.separate = self.home / '.agents/skills/korean-protocol'

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo)] + list(args),
                              capture_output=True, text=True, check=True).stdout

    def install(self, separate=False, version='v0.11.1'):
        target = self.separate if separate else self.bundled
        target.mkdir(parents=True)
        (target / 'SKILL.md').write_text('installed old copy\n')
        metadata = {'version': version, 'source': str(self.repo)}
        name = 'INSTALLATION.json' if separate else 'PINNED.json'
        (target / name).write_text(json.dumps(metadata))
        return target

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cx.main(['kp'] + list(args))
        return output.getvalue()

    def snapshot(self, root):
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob('*') if path.is_file()}

    def test_build_copy_file_set_and_pinned_bytes(self):
        dest = self.root / 'build'
        cx.kp_build_copy(self.repo, dest, 'v0.12.0', self.new_commit, str(self.repo))
        expected = set(REQUIRED) | {'SKILL.md', 'references/guide.md', 'PINNED.json'}
        self.assertEqual(set(self.snapshot(dest)), expected)
        metadata = {'name': 'korean-protocol', 'version': 'v0.12.0', 'source': str(self.repo),
                    'source_commit': self.new_commit,
                    'release': str(self.repo) + '/releases/tag/v0.12.0',
                    'license': 'MIT', 'note': NOTE}
        self.assertEqual((dest / 'PINNED.json').read_bytes(),
                         (json.dumps(metadata, ensure_ascii=False, indent=1) + '\n').encode())
        for relative in REQUIRED:
            self.assertEqual((dest / relative).read_bytes(), (self.repo / relative).read_bytes())

    def test_build_refuses_missing_required_file(self):
        for relative in REQUIRED + ('skills/korean-protocol/SKILL.md',):
            path = self.repo / relative
            content = path.read_bytes()
            path.unlink()
            try:
                with self.assertRaisesRegex(SystemExit, relative):
                    cx.kp_build_copy(self.repo, self.root / 'build', 'v0.12.0',
                                     self.new_commit, str(self.repo))
            finally:
                path.write_bytes(content)

    def test_status_absent_and_bundled_precedence(self):
        self.assertEqual(self.cli('status'),
                         'kp bundled: absent\nkp separate: absent\nkp active: none\n')
        self.install(separate=True, version='v0.10.0')
        self.assertIn('kp active: separate\n', self.cli('status'))
        self.install()
        self.assertEqual(self.cli('status'),
                         'kp bundled: %s v0.11.1\nkp separate: %s v0.10.0\nkp active: bundled\n'
                         % (self.bundled, self.separate))

    def test_status_remote_highest_semver_and_current(self):
        self.install()
        self.assertIn('kp latest: v0.12.0 (update available)\n', self.cli('status', '--remote'))
        (self.bundled / 'PINNED.json').write_text(json.dumps({
            'version': 'v0.12.0', 'source': str(self.repo)}))
        self.assertIn('kp latest: v0.12.0 (current)\n', self.cli('status', '--remote'))

    def test_remote_uses_separate_git_url_and_rejects_non_url(self):
        self.install(separate=True)
        metadata = self.separate / 'INSTALLATION.json'
        metadata.write_text(json.dumps({'version': 'v0.11.1', 'source': self.repo.as_uri()}))
        self.assertIn('kp latest: v0.12.0 (update available)', self.cli('status', '--remote'))
        # A non-URL or missing source falls back to the public KP repository; the local value is never queried.
        for data in ({'version': 'v0.11.1', 'source': 'vendor/kp'}, {'version': 'v0.11.1'}):
            metadata.write_text(json.dumps(data))
            calls = []
            def fake_run(command, *rest, **options):
                calls.append(command)
                return 'abc\trefs/tags/v0.12.0\n'
            with patch.object(cx, 'run', side_effect=fake_run):
                self.assertIn('kp latest: v0.12.0 (update available)', self.cli('status', '--remote'))
            self.assertEqual(calls, [['git', 'ls-remote', '--tags', cx.KP_SOURCE]])

    def test_remote_without_any_kp_install_queries_nothing(self):
        with patch.object(cx, 'run', side_effect=AssertionError('must not query without an install')):
            self.assertIn('kp latest: unknown (미확인)', self.cli('status', '--remote'))

    def test_remote_failure_is_unconfirmed(self):
        self.install()
        with patch.object(cx, 'run', return_value=None):
            self.assertIn('(미확인)', self.cli('status', '--remote'))

    def test_update_backs_up_old_copy_and_rewrites_pinned(self):
        self.install()
        before = self.snapshot(self.bundled)
        output = self.cli('update')
        metadata = json.loads((self.bundled / 'PINNED.json').read_text())
        self.assertEqual(metadata['version'], 'v0.12.0')
        self.assertEqual(metadata['source_commit'], self.new_commit)
        backups = list((self.state / 'backups').glob('kp-*/korean-protocol'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.snapshot(backups[0]), before)
        self.assertIn('kp update: v0.11.1 -> v0.12.0 (이전 사본: %s)' % backups[0], output)
        self.assertEqual(list((self.state / 'staging').iterdir()), [])

    def test_update_dry_run_leaves_installed_copy_untouched(self):
        self.install()
        before = self.snapshot(self.bundled)
        output = self.cli('update', '--to', 'v0.12.0', '--dry-run')
        self.assertIn('kp update dry-run: v0.11.1 -> v0.12.0 (8 files)', output)
        self.assertEqual(self.snapshot(self.bundled), before)
        self.assertFalse((self.state / 'backups').exists())
        self.assertEqual(list((self.state / 'staging').iterdir()), [])

    def test_update_unknown_tag_cleans_staging_and_preserves_install(self):
        self.install()
        before = self.snapshot(self.bundled)
        with self.assertRaisesRegex(SystemExit, 'KP release tag not found: v0.99.0'):
            self.cli('update', '--to', 'v0.99.0')
        self.assertEqual(self.snapshot(self.bundled), before)
        self.assertFalse((self.state / 'backups').exists())
        self.assertEqual(list((self.state / 'staging').iterdir()), [])

    def test_update_refuses_a_branch_that_only_looks_like_a_release(self):
        self.install()
        self.git('branch', 'v99.0.0')
        before = self.snapshot(self.bundled)
        with self.assertRaisesRegex(SystemExit, 'KP release tag not found: v99.0.0'):
            self.cli('update', '--to', 'v99.0.0')
        self.assertEqual(self.snapshot(self.bundled), before)
        self.assertFalse((self.state / 'backups').exists())

    def test_build_refuses_symlinks_that_could_pull_outside_files(self):
        outside = self.root / 'private.txt'
        outside.write_text('private\n')
        for link in (self.skill / 'references/leak.md', self.repo / 'lexicon/domains/software.json'):
            original = link.read_bytes() if link.exists() else None
            if link.exists():
                link.unlink()
            link.symlink_to(outside)
            try:
                with self.assertRaisesRegex(SystemExit, 'KP source contains a symlink'):
                    cx.kp_build_copy(self.repo, self.root / 'build', 'v0.12.0', self.new_commit, str(self.repo))
                self.assertFalse((self.root / 'build').exists())
            finally:
                link.unlink()
                if original is not None:
                    link.write_bytes(original)

    def test_update_leaves_separate_copy_untouched(self):
        self.install()
        self.install(separate=True)
        before = self.snapshot(self.separate)
        self.cli('update', '--to', 'v0.12.0')
        self.assertEqual(self.snapshot(self.separate), before)

    def test_update_separate_only_refusal(self):
        self.install(separate=True)
        with self.assertRaises(SystemExit) as caught:
            self.cli('update')
        self.assertEqual(str(caught.exception),
                         '따로 설치한 KP는 그 저장소의 방법으로 갱신한다: ' + str(self.separate))

    def test_update_absent_refusal(self):
        with self.assertRaisesRegex(SystemExit, 'KP'):
            self.cli('update')

    def test_update_same_version_is_noop_without_clone(self):
        self.install()
        with patch.object(cx, 'run', side_effect=AssertionError('must not clone')):
            self.assertEqual(self.cli('update', '--to', 'v0.11.1'),
                             'kp update: 변경 없음 (v0.11.1)\n')
        self.assertFalse((self.state / 'staging').exists())

    def test_update_invalid_tag_refusal(self):
        self.install()
        with self.assertRaises(SystemExit):
            self.cli('update', '--to=--unsafe')
        self.assertFalse((self.state / 'staging').exists())

    def test_update_restores_on_failed_activation(self):
        self.install()
        before = self.snapshot(self.bundled)
        original = os.rename

        def fail_build(source, target):
            if Path(source).name == 'build':
                raise OSError('activation failed')
            return original(source, target)

        with patch.object(cx.os, 'rename', side_effect=fail_build):
            with self.assertRaisesRegex(OSError, 'activation failed'):
                self.cli('update', '--to', 'v0.12.0')
        self.assertEqual(self.snapshot(self.bundled), before)
        self.assertEqual(list((self.state / 'staging').iterdir()), [])

    def test_sync_tool_dry_run_is_ascii_and_leaves_bundle_untouched(self):
        self.assertTrue(SYNC.exists(), 'sync_kp.py must exist')
        SYNC.read_bytes().decode('ascii')
        before = self.snapshot(ROOT / 'skills/korean-protocol')
        result = subprocess.run([os.sys.executable, '-B', str(SYNC), '--repo', str(self.repo),
                                 '--tag', 'v0.12.0', '--dry-run'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('v0.12.0', result.stdout)
        self.assertIn('next: bump skills/kp-conductor/VERSION and add a CHANGELOG section '
                      '(RELEASING.md: 포함한 KP 사본을 바꾸면 Conductor 버전도 올린다)', result.stdout)
        self.assertEqual(self.snapshot(ROOT / 'skills/korean-protocol'), before)

    def test_sync_tool_replaces_the_destination_from_the_tag(self):
        dest = self.root / 'repo-copy/skills/korean-protocol'
        dest.mkdir(parents=True)
        (dest / 'SKILL.md').write_text('old bundle\n')
        result = subprocess.run([os.sys.executable, '-B', str(SYNC), '--repo', str(self.repo),
                                 '--tag', 'v0.12.0', '--dest', str(dest)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((dest / 'SKILL.md').read_text(), '# KP v0.12.0\n')
        pinned = json.loads((dest / 'PINNED.json').read_text())
        self.assertEqual((pinned['version'], pinned['source_commit']), ('v0.12.0', self.new_commit))
        self.assertEqual(pinned['source'], 'https://github.com/project820/korean-protocol')
        # The staging folder sat beside the destination and is gone afterwards.
        self.assertEqual(sorted(path.name for path in dest.parent.iterdir()), ['korean-protocol'])
        self.assertIn('next: update the KP version text in README.md', result.stdout)


if __name__ == '__main__':
    unittest.main()
