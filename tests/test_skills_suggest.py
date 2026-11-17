import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parent.parent / 'skills/kp-conductor/scripts/conductor.py'
with tempfile.TemporaryDirectory(prefix='suggest-import-') as import_home:
    with patch.dict(os.environ, {'HOME': import_home, 'CONDUCTOR_HOME': import_home}):
        spec = importlib.util.spec_from_file_location('conductor_suggest_module', SCRIPT)
        cx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cx)


class SkillsSuggestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='suggest-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.root / 'state'
        self.refs = self.root / 'fixture-skill/references'
        self.refs.mkdir(parents=True)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            'HOME': str(self.home), 'CONDUCTOR_HOME': str(self.state)}))
        self.stack.enter_context(patch.object(cx, 'SKILL', self.refs.parent))
        self.stack.enter_context(patch.object(cx, 'run', side_effect=AssertionError('no subprocesses')))
        self.routing([])
        self.packs([])

    def routing(self, routes):
        rows = [dict(id=name, label='이름 ' + name, keywords=[name], skills=skills,
                     role='cx-build', risk='R2', note='') for name, skills in routes]
        (self.refs / 'routing.json').write_text(json.dumps({'mcp_usage': {}, 'routes': rows}))

    def packs(self, packs):
        (self.refs / 'recommended-skills.json').write_text(json.dumps({'packs': packs}))

    def skill(self, name, root='.agents/skills'):
        path = self.home / root / name / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# ' + name)
        return path

    def disable(self, text):
        directory = self.home / '.codex'
        directory.mkdir(exist_ok=True)
        (directory / 'config.toml').write_text(text)

    def receipt(self, name, fit, days_ago=0):
        stamp = cx.now() - cx.datetime.timedelta(days=days_ago)
        cx.append_receipt({'ts': stamp.isoformat(), 'skills': [name], 'fit': fit}, 'skill-receipts.jsonl')

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cx.main(['skills-suggest'] + list(args))
        return output.getvalue()

    def test_gaps_distinguish_empty_uninstalled_and_disabled_routes(self):
        self.routing([('empty', []), ('absent', ['missing']), ('disabled', ['off', 'missing']),
                      ('covered', ['off', 'active']), ('conductor-setup', [])])
        self.skill('off')
        self.skill('active')
        self.disable('[[skills.config]]\nname = "off"\nenabled = false\n')
        self.assertTrue(callable(getattr(cx, 'skills_suggest', None)), 'skills_suggest is missing')
        result = cx.skills_suggest(home=self.home)
        self.assertEqual(result['days'], 30)
        self.assertIsNotNone(cx.timestamp_seconds(result['ts']))
        self.assertEqual(result['gaps'], [
            {'route': 'absent', 'label': '이름 absent', 'reason': 'no skill installed', 'recommended': []},
            {'route': 'disabled', 'label': '이름 disabled', 'reason': 'installed but disabled', 'recommended': []}])
        # A route that lists no skill on purpose is reported only when a recommended tool exists for it.
        with patch.object(cx, 'missing_recommended', side_effect=lambda route, installed: (
                [{'name': 'pack', 'source': 'https://example.invalid/pack', 'install': {'command': 'install pack'}}]
                if route == 'empty' else [])):
            gaps = cx.skills_suggest(home=self.home)['gaps']
        self.assertEqual(gaps[0], {'route': 'empty', 'label': '이름 empty', 'reason': 'route lists no skills',
                                   'recommended': [{'name': 'pack', 'source': 'https://example.invalid/pack',
                                                    'install': 'install pack'}]})
        self.assertNotIn('misses', result)

    def test_disabled_file_directory_and_alias_match_routing(self):
        self.routing([('file', ['file-off']), ('directory', ['dir-off']), ('alias', ['duplicate'])])
        file_path = self.skill('file-off')
        directory = self.skill('dir-off').parent
        first_alias = self.skill('duplicate')
        self.skill('duplicate', '.codex/skills')
        self.disable(''.join('[[skills.config]]\npath = %s\nenabled = false\n' % json.dumps(str(path))
                             for path in (file_path, directory, first_alias)))
        self.assertTrue(callable(getattr(cx, 'skills_suggest', None)), 'skills_suggest is missing')
        result = cx.skills_suggest(home=self.home)
        self.assertEqual([(row['route'], row['reason']) for row in result['gaps']],
                         [('file', 'installed but disabled'), ('directory', 'installed but disabled')])
        matched = cx.route_matches('file directory alias', cx.load_routing(), {}, cx.role_skills(self.home),
                                   disabled=cx.disabled_skills(self.home))
        for route in matched:
            self.assertEqual(route['id'] in {row['route'] for row in result['gaps']},
                             all(skill.get('disabled', False) for skill in route['skills']))

    def test_recommendations_use_command_then_how_then_source_and_installed_pack_detection(self):
        self.routing([('absent', ['missing'])])
        packs = [
            {'name': 'command', 'source': 'https://example.test/c', 'install': {'command': 'install c', 'how': 'read c'}},
            {'name': 'how', 'source': 'https://example.test/h', 'install': {'command': None, 'how': 'read h'}},
            {'name': 'source', 'source': 'https://example.test/s'},
            {'name': 'present', 'source': 'https://example.test/p', 'skills': ['installed']},
        ]
        self.packs([dict(pack, routes=['absent']) for pack in packs])
        self.skill('installed')
        self.assertTrue(callable(getattr(cx, 'skills_suggest', None)), 'skills_suggest is missing')
        result = cx.skills_suggest()
        self.assertEqual(result['gaps'][0]['recommended'], [
            {'name': 'command', 'source': 'https://example.test/c', 'install': 'install c'},
            {'name': 'how', 'source': 'https://example.test/h', 'install': 'read h'},
            {'name': 'source', 'source': 'https://example.test/s', 'install': 'https://example.test/s'}])

    def test_weak_requires_two_poor_and_more_poor_than_good_with_period_filter(self):
        self.routing([('first', ['weak']), ('second', ['weak', 'tie'])])
        for name, fits in [('weak', ['poor', 'poor', 'good']), ('tie', ['poor', 'poor', 'good', 'good']),
                           ('once', ['poor']), ('unrouted', ['poor', 'poor'])]:
            for fit in fits:
                self.receipt(name, fit)
        self.receipt('once', 'poor', days_ago=8)
        self.assertTrue(callable(getattr(cx, 'skills_suggest', None)), 'skills_suggest is missing')
        result = cx.skills_suggest(7)
        self.assertEqual(result['days'], 7)
        self.assertEqual(result['weak'], [
            {'skill': 'unrouted', 'poor': 2, 'good': 0, 'routes': []},
            {'skill': 'weak', 'poor': 2, 'good': 1, 'routes': ['first', 'second']}])

    def test_cli_text_includes_tables_note_and_saved_audit(self):
        self.routing([('absent', ['weak'])])
        self.packs([{'name': 'Pack', 'source': 'https://example.test', 'routes': ['absent'],
                     'install': {'how': 'read | guide\nthen install'}}])
        self.receipt('weak', 'poor')
        self.receipt('weak', 'poor')
        output = self.cli('--days', '7')
        self.assertIn('skills-suggest: 1 gaps, 1 weak\n', output)
        self.assertIn('| 경로 | 이름 | 이유 | 권장 설치 |', output)
        self.assertIn('| absent | 이름 absent | no skill installed | Pack: read \\| guide then install |', output)
        self.assertIn('| 스킬 | poor | good | 경로 |', output)
        self.assertIn('| weak | 2 | 0 | absent |', output)
        self.assertIn('note: 제안만 한다. 설치는 사용자 동의 뒤 원본 안내를 따른다.\n', output)
        target = cx.owned_path(self.state / 'audits') / ('suggest-' + cx.now().strftime('%Y%m%d') + '.json')
        self.assertIn('saved: ' + str(target), output)
        saved = json.loads(target.read_text())
        self.assertEqual(saved['days'], 7)
        self.assertEqual(len(saved['gaps']), 1)
        self.assertEqual(len(saved['weak']), 1)
        self.assertFalse((self.home / '.agents').exists())

    def test_cli_json_prints_only_saved_json(self):
        self.routing([('absent', ['missing'])])
        result = json.loads(self.cli('--json', '--days', '5'))
        files = list((self.state / 'audits').glob('suggest-????????.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text()), result)
        self.assertEqual(result['days'], 5)
        self.assertEqual(result['weak'], [])
        self.assertNotIn('misses', result)

    def test_cli_rejects_negative_days_without_writing_audit(self):
        with self.assertRaisesRegex(SystemExit, 'days must be nonnegative'):
            self.cli('--days', '-1')
        self.assertFalse((self.state / 'audits').exists())

    def test_cli_refuses_symlinked_audit_directory(self):
        self.state.mkdir()
        target = self.root / 'outside'
        target.mkdir()
        (self.state / 'audits').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, 'refusing to write through symlink'):
            self.cli('--json')
        self.assertEqual(list(target.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
