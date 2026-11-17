import argparse
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


SCRIPT = Path(__file__).resolve().parent.parent / 'skills/kp-conductor/scripts/conductor.py'
spec = importlib.util.spec_from_file_location('conductor_lifecycle_module', SCRIPT)
cx = importlib.util.module_from_spec(spec)
with tempfile.TemporaryDirectory() as import_home, patch.dict(os.environ, {'HOME': import_home}):
    spec.loader.exec_module(cx)


class SkillsLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.root / 'state'
        environment = patch.dict(os.environ, {'HOME': str(self.home), 'CONDUCTOR_HOME': str(self.state)})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = self.home / '.codex/config.toml'
        # Unexpected network access must fail even when a test forgot to patch run().
        self.real_run = cx.run
        runner = patch.object(cx, 'run', side_effect=AssertionError('unexpected external command'))
        runner.start()
        self.addCleanup(runner.stop)

    def skill(self, name='sample', root='.codex/skills', installation=None):
        directory = self.home / root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'SKILL.md').write_text('# ' + name + '\n')
        if installation is not None:
            (directory / 'INSTALLATION.json').write_text(json.dumps(installation))
        return directory

    def audit(self, remote=False):
        return cx.skills_audit(home=self.home, remote=remote)

    def delete(self, target, yes=False, dry_run=False):
        args = argparse.Namespace(target=str(target), yes=yes, dry_run=dry_run)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cx.command_skills_delete(args)
        return output.getvalue()

    def owned_config(self, *directories):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.original = 'model = "keep"\n# untouched user setting\n'
        self.config.write_text(cx.skills_region_candidate(self.original,
            {str(directory / 'SKILL.md') for directory in directories}))

    def test_installed_commit_uses_real_git_checkout_before_installation(self):
        directory = self.skill(installation={'source_commit': 'b' * 40})
        repo = directory.parent
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo)] + list(args), text=True,
                env=dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull))
        git('init', '-q')
        git('remote', 'add', 'origin', 'https://example.invalid/skills.git')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
            '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'fixture')
        expected = git('rev-parse', 'HEAD').strip()
        with patch.object(cx, 'run', side_effect=self.real_run) as runner:
            row = self.audit()['skills'][0]
        self.assertEqual(row['installed_commit'], expected)
        self.assertIn(unittest.mock.call(['git', '-C', str(directory), 'rev-parse', 'HEAD']), runner.call_args_list)

    def test_installed_commit_from_installation_and_unknown(self):
        self.skill('pinned', installation={'source_commit': 'a' * 40})
        self.skill('unknown')
        rows = {row['name']: row for row in self.audit()['skills']}
        self.assertEqual(rows['pinned']['installed_commit'], 'a' * 40)
        self.assertEqual(rows['unknown']['installed_commit'], '')
        self.assertNotIn('update', rows['unknown'])
        self.assertNotIn('update_available', self.audit()['counts'])

    def test_native_skill_reports_installation_commit_without_changing_origin(self):
        self.skill('native', '.codex/skills/.system', {'source_commit': 'a' * 40})
        row = self.audit()['skills'][0]
        self.assertEqual(row['installed_commit'], 'a' * 40)
        self.assertEqual((row['origin'], row['source'], row['verdict']), ('native', '', 'native'))

    def test_installed_commit_falls_back_to_state_record(self):
        self.skill()
        self.state.mkdir()
        record = self.state / 'skills-installed.json'
        for data in ({'sample': {'source': 'vendor/skills', 'commit': 'c' * 40}},
                     {'skills': {'sample': {'source_commit': 'c' * 40}}},
                     [{'name': 'sample', 'commit': 'c' * 40}]):
            with self.subTest(data=data):
                record.write_text(json.dumps(data))
                self.assertEqual(self.audit()['skills'][0]['installed_commit'], 'c' * 40)

    def test_remote_update_requires_complete_sha_and_preserves_verdict(self):
        for name, commit in (('available', 'a' * 40), ('current', 'b' * 40),
                             ('short', 'abc123'), ('unknown', ''), ('sha256', 'a' * 64)):
            self.skill(name, installation={'source': 'https://github.com/vendor/' + name,
                                          'source_commit': commit})
        def remote(command, timeout=20):
            self.assertEqual(command[:2], ['git', 'ls-remote'])
            return ('b' * (64 if command[2].endswith('/sha256') else 40)) + '\tHEAD\n'
        baseline = {row['name']: row['verdict'] for row in self.audit()['skills']}
        with patch.object(cx, 'run', side_effect=remote):
            audit = self.audit(remote=True)
        rows = {row['name']: row for row in audit['skills']}
        self.assertEqual({name: row['update'] for name, row in rows.items()},
            {'available': 'available', 'current': 'current', 'short': '미확인',
             'unknown': '미확인', 'sha256': 'available'})
        self.assertEqual(audit['counts']['update_available'], 2)
        self.assertEqual({name: row['verdict'] for name, row in rows.items()}, baseline)

    def test_remote_failure_and_missing_source_are_unconfirmed(self):
        self.skill('remote', installation={'source': 'https://github.com/vendor/sample', 'source_commit': 'a' * 40})
        self.skill('local')
        with patch.object(cx, 'run', return_value=None):
            audit = self.audit(remote=True)
        self.assertEqual([row['update'] for row in audit['skills']], ['미확인', '미확인'])
        self.assertEqual(audit['counts']['update_available'], 0)

    def test_remote_cli_exposes_installed_and_update(self):
        self.skill(installation={'source': 'https://github.com/vendor/sample', 'source_commit': 'a' * 40})
        for as_json in (False, True):
            with patch.object(cx, 'run', return_value='b' * 40 + '\tHEAD\n'), contextlib.redirect_stdout(io.StringIO()) as output:
                cx.command_skills_audit(argparse.Namespace(days=30, stale_days=30, remote=True, json=as_json))
            if as_json:
                row = json.loads(output.getvalue())['skills'][0]
                self.assertEqual((row['installed_commit'], row['update']), ('a' * 40, 'available'))
            else:
                self.assertIn(' installed |', output.getvalue().splitlines()[0])
                self.assertIn(' update |', output.getvalue().splitlines()[0])
                self.assertIn('a' * 40, output.getvalue())

    def test_status_is_unchanged_without_updates_and_appends_positive_count(self):
        directory = self.state / 'audits'
        directory.mkdir(parents=True)
        path = directory / 'skills-20261117.json'
        expected = 'skills: 1 review, 2 review-duplicate, 3 banned, 4 duplicates, native 5, local 6'
        counts = dict(zip(('review', 'review-duplicate', 'banned', 'duplicates', 'native', 'local'), range(1, 7)))
        for extra, suffix in (({}, ''), ({'update_available': 0}, ''), ({'update_available': 2}, ', 2 update available')):
            with self.subTest(extra=extra):
                path.write_text(json.dumps({'counts': dict(counts, **extra)}))
                self.assertEqual(cx.skills_status(), expected + suffix)

    def test_delete_refuses_engine_protocol_native_and_outside_roots(self):
        cases = [('kp-conductor', '.codex/skills'), ('kp-conductor-onboard', '.agents/skills'),
                 ('korean-protocol', '.agents/skills'), ('native', '.codex/skills/.system'),
                 ('plugin', '.codex/plugins/cache/vendor/skills'), ('outside', 'external')]
        for name, root in cases:
            with self.subTest(name=name):
                directory = self.skill(name, root)
                with self.assertRaisesRegex(SystemExit, 'refusing to delete'):
                    self.delete(directory / 'SKILL.md', yes=True)
                self.assertTrue((directory / 'SKILL.md').is_file())
        self.assertFalse(self.state.exists())

    def test_delete_requires_approval_after_printing_plan(self):
        directory = self.skill()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(SystemExit, 'skills delete requires --yes after the user approved the deletion'):
                cx.command_skills_delete(argparse.Namespace(target='sample', yes=False, dry_run=False))
        self.assertIn('delete plan: ' + str(directory), output.getvalue())
        self.assertIn('copy kept at: ' + str(self.state / 'backups/skills-deleted-'), output.getvalue())
        self.assertTrue(directory.is_dir())
        self.assertFalse(self.state.exists())

    def test_dry_run_does_not_change_files_with_or_without_yes(self):
        directory = self.skill()
        self.owned_config(directory)
        original = self.config.read_bytes()
        for yes in (False, True):
            with self.subTest(yes=yes):
                self.assertIn('delete plan: ', self.delete('sample', yes=yes, dry_run=True))
                self.assertTrue(directory.is_dir())
                self.assertEqual(self.config.read_bytes(), original)
                self.assertFalse(self.state.exists())

    def test_delete_moves_skill_removes_only_owned_entry_and_records_copy(self):
        directory, other = self.skill(), self.skill('other', '.agents/skills')
        self.owned_config(directory, other)
        output = self.delete('sample', yes=True)
        self.assertFalse(directory.exists())
        record = json.loads((self.state / 'skills-deleted.jsonl').read_text())
        copy = Path(record['moved_to'])
        self.assertEqual(copy.parent.parent, self.state / 'backups')
        self.assertRegex(copy.parent.name, r'^skills-deleted-\d{8}-\d{6}$')
        self.assertEqual((copy / 'SKILL.md').read_text(), '# sample\n')
        self.assertEqual((record['name'], record['path']), ('sample', str(directory)))
        self.assertIn('ts', record)
        self.assertEqual(self.config.read_text(), cx.skills_region_candidate(self.original, {str(other / 'SKILL.md')}))
        self.assertIn('skills delete: %s (복구용 사본: %s)' % (directory, copy), output)
        self.delete('other', yes=True)
        self.assertEqual(len((self.state / 'skills-deleted.jsonl').read_text().splitlines()), 2)

    def linked_skill(self):
        target = self.skill('actual', 'external')
        link = self.home / '.agents/skills/linked'
        link.parent.mkdir(parents=True)
        link.symlink_to(os.path.relpath(target, link.parent), target_is_directory=True)
        return link, target

    def test_symlink_delete_unlinks_only_keeps_target_and_cleans_alias(self):
        link, target = self.linked_skill()
        self.owned_config(link)
        output = self.delete('linked', yes=True)
        self.assertFalse(link.is_symlink())
        self.assertTrue((target / 'SKILL.md').is_file())
        record = json.loads((self.state / 'skills-deleted.jsonl').read_text())
        self.assertEqual(record['symlink_target'], os.path.relpath(target, link.parent))
        self.assertNotIn('moved_to', record)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertIn('symlink only: %s -> %s' % (link, record['symlink_target']), output)
        self.assertFalse((self.state / 'backups').exists())

    def test_symlink_delete_keeps_the_target_entry_off(self):
        link, target = self.linked_skill()
        self.owned_config(link, target)
        self.delete('linked', yes=True)
        # The target is still installed under another path, so its off entry must stay.
        self.assertEqual(self.config.read_text(), cx.skills_region_candidate(self.original, {str(target / 'SKILL.md')}))

    def test_deleting_a_link_moves_its_off_entry_to_a_target_still_in_a_skill_root(self):
        target = self.skill('foo', '.agents/skills')
        link = self.home / '.codex/skills/foo'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=True)
        self.owned_config(link)
        _, before = cx.disabled_skills(self.home)
        self.assertIn(str((target / 'SKILL.md').resolve()), before)
        output = self.delete(str(link / 'SKILL.md'), yes=True)
        self.assertFalse(link.is_symlink())
        self.assertEqual(self.config.read_text(),
                         cx.skills_region_candidate(self.original, {str((target / 'SKILL.md').resolve())}))
        _, after = cx.disabled_skills(self.home)
        self.assertIn(str((target / 'SKILL.md').resolve()), after)
        self.assertIn('kept off: ' + str((target / 'SKILL.md').resolve()), output)
        # Recreating the link from the log brings the skill back still off, as the restore guide says.
        record = json.loads((self.state / 'skills-deleted.jsonl').read_text())
        link.symlink_to(record['symlink_target'], target_is_directory=True)
        names, paths = cx.disabled_skills(self.home)
        self.assertIn(str((link / 'SKILL.md').resolve()), paths)

    def test_delete_lists_links_that_will_break(self):
        target = self.skill('foo', '.agents/skills')
        link = self.home / '.codex/skills/foo'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=True)
        output = self.delete(str(target / 'SKILL.md'), dry_run=True)
        self.assertIn('linked from: %s' % link, output)

    def test_delete_refuses_folders_inside_protected_skills(self):
        nested = self.skill('x', '.codex/skills/kp-conductor/references')
        with self.assertRaisesRegex(SystemExit, 'protected skill'):
            self.delete(str(nested / 'SKILL.md'), dry_run=True)

    def test_same_git_url_ignores_trailing_slash_and_dot_git(self):
        self.assertTrue(cx.same_git_url('https://github.com/a/b.git', 'https://github.com/a/b/'))
        self.assertFalse(cx.same_git_url('https://github.com/a/b', 'https://github.com/a/c'))
        self.assertFalse(cx.same_git_url('', 'https://github.com/a/b'))

    def test_config_failure_restores_moved_directory_and_symlink(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                if linked:
                    directory, target = self.linked_skill()
                    link_target = os.readlink(directory)
                else:
                    directory = self.skill()
                self.owned_config(directory)
                original = self.config.read_bytes()
                write = cx.atomic_write
                def fail_config(path, *args, **kwargs):
                    if Path(path) == self.config:
                        raise OSError('config write failed')
                    return write(path, *args, **kwargs)
                with patch.object(cx, 'atomic_write', side_effect=fail_config):
                    with self.assertRaisesRegex(OSError, 'config write failed'):
                        self.delete(directory / 'SKILL.md', yes=True)
                self.assertTrue((directory / 'SKILL.md').is_file())
                self.assertEqual(self.config.read_bytes(), original)
                self.assertFalse((self.state / 'skills-deleted.jsonl').exists())
                if linked:
                    self.assertEqual(os.readlink(directory), link_target)
                    self.assertTrue(target.is_dir())

    def test_delete_rejects_symlink_parent_and_backup_path(self):
        external = self.skill('actual', 'external')
        parent = self.home / '.codex/skills/pack'
        parent.parent.mkdir(parents=True)
        parent.symlink_to(external.parent, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, 'symlink'):
            self.delete(parent / 'actual/SKILL.md', yes=True)
        self.assertTrue(external.is_dir())
        directory = self.skill()
        self.state.mkdir()
        (self.state / 'backups').symlink_to(external.parent, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, 'symlink'):
            self.delete(directory / 'SKILL.md', yes=True)
        self.assertTrue(directory.is_dir())

    def test_concurrent_config_change_preserves_user_edit_and_restores_skill(self):
        directory = self.skill()
        self.owned_config(directory)
        external = self.config.read_text() + '# concurrent user edit\n'
        move = cx.shutil.move
        def concurrent_move(source, destination):
            result = move(source, destination)
            if Path(source) == directory:
                self.config.write_text(external)
            return result
        with patch.object(cx.shutil, 'move', side_effect=concurrent_move):
            with self.assertRaisesRegex(SystemExit, '다른 작업이 파일을 바꿨다'):
                self.delete('sample', yes=True)
        self.assertTrue((directory / 'SKILL.md').is_file())
        self.assertEqual(self.config.read_text(), external)
        self.assertFalse((self.state / 'skills-deleted.jsonl').exists())

    def test_log_failure_rolls_back_config_and_skill(self):
        directory = self.skill()
        self.owned_config(directory)
        original = self.config.read_bytes()
        write = cx.atomic_write
        def fail_log(path, *args, **kwargs):
            if Path(path) == self.state / 'skills-deleted.jsonl':
                raise OSError('log write failed')
            return write(path, *args, **kwargs)
        with patch.object(cx, 'atomic_write', side_effect=fail_log):
            with self.assertRaisesRegex(OSError, 'log write failed'):
                self.delete('sample', yes=True)
        self.assertTrue((directory / 'SKILL.md').is_file())
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.state / 'skills-deleted.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
