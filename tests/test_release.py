"""tools/release.py: version, release notes and tag rules from RELEASING.md."""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('release_module', ROOT / 'tools' / 'release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

GIT = ['git', '-c', 'user.name=t', '-c', 'user.email=tester@localhost', '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false']
NOTES_010 = '## [0.1.0] - 2026-11-17\n\n첫 공개판이다.\n\n### 추가\n\n- 진입 절차\n'
NOTES_020 = '## [0.2.0] - 2026-11-18\n\n### 추가\n\n- 버전 규칙\n\n'


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name)
        self.git('init', '-q', '-b', 'main')

    def git(self, *args):
        return subprocess.run(GIT + ['-C', str(self.repo)] + list(args), check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, version, changelog):
        target = self.repo / release.VERSION_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(version)
        (self.repo / release.CHANGELOG).write_text('# 변경 기록\n\n' + changelog)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'release ' + version.strip())
        return self.git('rev-parse', 'HEAD')

    def assertFails(self, text, commit='HEAD'):
        with self.assertRaises(release.ReleaseError) as caught:
            release.check(self.repo, commit)
        self.assertIn(text, str(caught.exception))

    def test_first_release_passes_and_notes_are_the_section_body(self):
        self.commit('0.1.0\n', NOTES_010)
        self.assertEqual(release.check(self.repo), ('0.1.0', 'new release'))
        self.assertEqual(release.notes(self.repo), '첫 공개판이다.\n\n### 추가\n\n- 진입 절차')

    def test_push_without_a_new_version_fails(self):
        self.commit('0.1.0\n', NOTES_010)
        self.git('tag', 'v0.1.0')
        (self.repo / 'README.md').write_text('changed')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'docs')
        self.assertFails('already released from another commit')

    def test_rerun_on_the_released_commit_passes(self):
        self.commit('0.1.0\n', NOTES_010)
        self.git('tag', '-a', 'v0.1.0', '-m', 'v0.1.0')
        self.assertEqual(release.check(self.repo), ('0.1.0', 'already released from this commit'))

    def test_next_version_passes_and_must_be_greater_than_latest_tag(self):
        self.commit('0.1.0\n', NOTES_010)
        self.git('tag', 'v0.1.0')
        self.commit('0.2.0\n', NOTES_020 + NOTES_010)
        self.assertEqual(release.check(self.repo), ('0.2.0', 'new release after v0.1.0'))
        self.git('tag', 'v0.3.0', 'HEAD~1')
        self.assertFails('must be greater than the latest release v0.3.0')

    def test_delayed_run_for_an_earlier_commit_ignores_later_releases(self):
        self.commit('0.1.0\n', NOTES_010)
        self.git('tag', 'v0.1.0')
        middle = self.commit('0.2.0\n', NOTES_020 + NOTES_010)
        self.commit('0.3.0\n', '## [0.3.0] - 2026-11-18\n\n- 다음\n\n' + NOTES_020 + NOTES_010)
        self.git('tag', '-a', 'v0.3.0', '-m', 'v0.3.0')
        self.assertEqual(release.check(self.repo, middle), ('0.2.0', 'new release after v0.1.0'))

    def test_version_and_changelog_must_agree(self):
        self.commit('0.2.0\n', NOTES_010)
        self.assertFails('newest CHANGELOG.md section is 0.1.0')

    def test_malformed_inputs_fail(self):
        cases = [
            ('v0.1.0\n', NOTES_010, 'MAJOR.MINOR.PATCH'),
            ('0.1\n', NOTES_010, 'MAJOR.MINOR.PATCH'),
            ('0.1.0', NOTES_010, 'MAJOR.MINOR.PATCH'),
            ('0.1.0\n', '## [Unreleased]\n\n- x\n', 'heading must be'),
            ('0.1.0\n', '## [0.1.0] - 2026-13-01\n\n- x\n', 'invalid date'),
            ('0.1.0\n', '## [0.1.0] - 2026-11-17\n\n변경 없음\n', 'needs at least one'),
            ('0.2.0\n', NOTES_010 + NOTES_020, 'newest to oldest'),
            ('0.2.0\n', NOTES_020.replace('2026-11-18', '2026-11-01') + NOTES_010, 'must not go back'),
            ('0.1.0\n', '첫 줄만 있다\n', 'no release section'),
        ]
        for version, changelog, text in cases:
            with self.subTest(version=version, text=text):
                self.commit(version, changelog)
                self.assertFails(text)

    def test_check_reads_the_commit_and_ignores_uncommitted_edits(self):
        first = self.commit('0.1.0\n', NOTES_010)
        (self.repo / release.VERSION_FILE).write_text('broken')
        self.assertEqual(release.check(self.repo, first)[0], '0.1.0')

    def test_remote_tags_are_compared_after_peeling(self):
        remote = tempfile.TemporaryDirectory()
        self.addCleanup(remote.cleanup)
        subprocess.run(GIT + ['init', '-q', '--bare', remote.name], check=True, capture_output=True)
        self.commit('0.1.0\n', NOTES_010)
        self.git('tag', '-a', 'v0.1.0', '-m', 'v0.1.0')
        self.git('remote', 'add', 'origin', remote.name)
        self.git('push', '-q', 'origin', 'main', '--tags')
        self.git('tag', '-d', 'v0.1.0')
        self.assertEqual(release.check(self.repo, remote='origin')[1], 'already released from this commit')
        self.commit('0.1.1\n', '## [0.1.1] - 2026-11-17\n\n- 수정\n\n' + NOTES_010)
        self.assertEqual(release.check(self.repo, remote='origin'), ('0.1.1', 'new release after v0.1.0'))

    def test_cli_reports_failure_with_exit_code_one(self):
        self.commit('0.2.0\n', NOTES_010)
        with patch_root(self.repo), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release.main(['check']), 1)
        self.assertIn('RELEASING.md', error.getvalue())


@contextlib.contextmanager
def patch_root(path):
    saved, release.ROOT = release.ROOT, path
    try:
        yield
    finally:
        release.ROOT = saved


class RepositoryVersionTests(unittest.TestCase):
    def test_working_tree_version_matches_the_newest_changelog_section(self):
        version = release.parse_version((ROOT / release.VERSION_FILE).read_text(encoding='utf-8'))
        sections = release.parse_changelog((ROOT / release.CHANGELOG).read_text(encoding='utf-8'))
        self.assertEqual(sections[0][0], version)

    def test_engine_reports_the_same_version(self):
        version = (ROOT / release.VERSION_FILE).read_text(encoding='utf-8').strip()
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'skills/kp-conductor/scripts/conductor.py'), '--version'],
                                    capture_output=True, text=True, env=dict(os.environ, HOME=home, CONDUCTOR_HOME=home))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'cx (kp-conductor) ' + version)


if __name__ == '__main__':
    unittest.main()
