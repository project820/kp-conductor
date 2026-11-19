import importlib.util
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Every test runs against a throwaway home; no test may reach the real ~/.codex.
TEST_HOME = tempfile.TemporaryDirectory(prefix='conductor-test-home-')
os.environ['HOME'] = TEST_HOME.name
os.environ.pop('OPENCODEX_HOME', None)

SCRIPT = Path(__file__).resolve().parent.parent / 'skills' / 'kp-conductor' / 'scripts' / 'conductor.py'
spec = importlib.util.spec_from_file_location('conductor_module', SCRIPT)
cx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cx)


class ConductorTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'CONDUCTOR_HOME': self.home.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.home.cleanup)
        self.skill_root = Path(self.home.name) / 'fixture-skill'
        references = self.skill_root / 'references'
        references.mkdir(parents=True)
        (references / 'roles.json').write_text('{"roles": {}}')
        (references / 'routing.json').write_text('{"mcp_usage": {}, "routes": []}')
        skill_patch = patch.object(cx, 'SKILL', self.skill_root)
        skill_patch.start()
        self.addCleanup(skill_patch.stop)
        local = Path(self.home.name) / 'local'
        local.mkdir()
        (local / 'settings.json').write_text(json.dumps({'onboarding': {'judge_model': 'anthropic/claude-fable-5-2'}, 'quota': [
            {'key': 'openai', 'provider': 'openai', 'window': 'weekly', 'models': ['gpt-*']},
            {'key': 'anthropic', 'provider': 'anthropic', 'window': 'weekly', 'models': ['anthropic/*']},
            {'key': 'fable', 'provider': 'anthropic', 'window': 'Fable', 'models': ['anthropic/claude-fable-*'], 'parent': 'anthropic'}]}))
        self.quota = {'openai': {'weekly': 1}, 'anthropic': {'weekly': 10, 'Fable': 18}}
        self.burn = {'openai': {'weekly': 0.1}, 'anthropic': {'weekly': 0.1, 'Fable': 0.1}}

    def test_quota_line_with_model_window_is_parsed_per_provider(self):
        text = ('openai weekly 1% resets 2026-11-18T15:33:33.000Z\n'
                'anthropic weekly 10% resets 2026-11-15T16:59:59.268Z Fable 18% resets 2026-11-15T16:59:59.268Z\n'
                'xai weekly 3% resets 2026-11-18T02:37:08.539Z\n')
        self.assertEqual(cx.parse_quota(text), {'openai': {'weekly': 1}, 'anthropic': {'weekly': 10, 'Fable': 18}, 'xai': {'weekly': 3}})
        self.assertEqual(cx.parse_quota(None), {})

    def test_checkpoint_reports_percent_and_survives_as_open_until_finished(self):
        project = self.home.name
        started = cx.checkpoint_start(project, '병렬 구현', '계약|구현|통합|검토', mode='균형', mode_source='default')
        self.assertEqual(cx.progress_line(started), '진행 0% (0/4) — 시작')
        self.assertEqual(cx.progress_line(cx.checkpoint_update(project, None, 2)), '진행 50% (2/4) — 구현')
        _, reopened = cx.load_checkpoint(project)
        self.assertEqual((reopened['id'], reopened['done'], reopened['status']), (started['id'], 2, 'open'))
        finished = cx.checkpoint_update(project, None, None, outcome='pass')
        self.assertEqual((finished['status'], finished['done']), ('done', 4))
        self.assertEqual(cx.load_checkpoint(project), (None, None))

    def test_partial_outcome_keeps_the_real_progress(self):
        project = self.home.name
        cx.checkpoint_start(project, '작업', 'a|b|c', mode='균형', mode_source='default')
        cx.checkpoint_update(project, None, 1)
        self.assertEqual(cx.checkpoint_update(project, None, None, outcome='partial')['done'], 1)

    def test_projects_with_the_same_folder_name_do_not_share_state(self):
        self.assertNotEqual(cx.project_slug('/tmp/a/app'), cx.project_slug('/tmp/b/app'))
        self.assertEqual(cx.project_slug('/tmp/a/app'), cx.project_slug('/tmp/a/app/'))

    def test_skill_usage_counts_tool_reads_and_explicit_calls_but_not_the_skill_listing(self):
        sessions = Path(self.home.name) / 'sessions'
        sessions.mkdir()
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
        rows = [
            {'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec',
                'input': 'cat /Users/x/.agents/skills/deep-research-fusion/SKILL.md'}},
            {'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'shell',
                'arguments': '{"cmd":"cat ~/.codex/skills/.system/skill-creator/SKILL.md"}'}},
            {'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': '[$deep-research-fusion](/Users/x/.agents/skills/deep-research-fusion/SKILL.md) 로 해'}]}},
            {'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer',
                'content': [{'type': 'input_text', 'text': '- pdf: ... (file: /Users/x/.codex/skills/pdf/SKILL.md)'}]}},
            {'timestamp': '2020-01-01T00:00:00.000Z', 'type': 'response_item', 'payload': {'type': 'custom_tool_call',
                'input': 'cat ~/.codex/skills/doc-tool/SKILL.md'}},
        ]
        (sessions / 'rollout.jsonl').write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows))
        reads, explicit = cx.skill_usage(7, sessions)
        self.assertEqual(reads, {'deep-research-fusion': 1, 'skill-creator': 1})
        self.assertEqual(explicit, {'deep-research-fusion': 1})

    def test_skill_usage_skips_audit_sessions_but_not_the_orchestrator_that_spawned_them(self):
        sessions = Path(self.home.name) / 'sessions'
        sessions.mkdir()
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
        read = {'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec',
            'input': 'cat ~/.codex/skills/second-review/SKILL.md'}}
        audit = [{'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': '[conductor-subagent] [conductor-audit] Council 평가'}]}}, read, read]
        spawn = {'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec',
            'input': 'spawn_agent message: [conductor-audit] ~/.codex/skills/second-review/SKILL.md'}}
        (sessions / 'audit.jsonl').write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in audit))
        (sessions / 'main.jsonl').write_text(json.dumps(spawn, ensure_ascii=False))
        reads, _ = cx.skill_usage(7, sessions)
        self.assertEqual(reads, {'second-review': 1})

    def test_lineup_diff_ignores_nothing_and_reports_both_directions(self):
        diff = cx.lineup_diff(['gpt-6.1-sol', 'gpt-7-nova'], {'gpt-6.1-sol': 'gpt-6-1-sol', 'xai/grok-4.7': 'grok-4-7'})
        self.assertEqual(diff, {'new': ['gpt-7-nova'], 'missing': ['xai/grok-4.7']})

    def test_aa_diff_reports_index_moves_of_one_point_or_more(self):
        old = [{'model': 'GPT-6.1 Sol', 'effort': 'high', 'intelligence_index': '50.3'},
               {'model': 'GPT-6 Luna', 'effort': 'low', 'intelligence_index': '9.7'}]
        new = [{'model': 'GPT-6.1 Sol', 'effort': 'high', 'intelligence_index': 52.0},
               {'model': 'GPT-7 Nova', 'effort': 'high', 'intelligence_index': 66.0}]
        self.assertEqual(cx.aa_diff(old, new), {'added': ['GPT-7 Nova (high)'], 'removed': ['GPT-6 Luna (low)'],
                                                'changed': ['GPT-6.1 Sol (high): 50.3 -> 52.0']})

    def test_retro_groups_receipts_by_type_model_effort_and_run(self):
        for outcome in ('pass', 'pass', 'fail'):
            cx.append_receipt({'ts': cx.now().isoformat(timespec='seconds'), 'type': 'C', 'model': 'gpt-6.1-sol',
                               'effort': 'high', 'outcome': outcome, 'run': 'parallel'})
        with patch.object(cx, 'run', return_value=None):
            report = cx.retro_report(7, Path(self.home.name) / 'none')
        self.assertIn('| C | gpt-6.1-sol | high | parallel | 3 | 2 | 1 |', report)

    def write_sessions(self, rows):
        sessions = Path(self.home.name) / '.codex' / 'sessions'
        sessions.mkdir(parents=True, exist_ok=True)
        stamp = cx.now().astimezone(cx.datetime.timezone.utc).isoformat()
        (sessions / 'rollout.jsonl').write_text('\n'.join(json.dumps({'timestamp': stamp, 'payload': row}) for row in rows))
        return sessions

    def make_skill(self, name, *, stale=True, root='.agents/skills'):
        directory = Path(self.home.name) / root / name
        directory.mkdir(parents=True)
        (directory / 'SKILL.md').write_text('# ' + name)
        if stale:
            stamp = time.time() - 45 * 86400
            os.utime(directory / 'SKILL.md', (stamp, stamp))
        return directory

    def test_mcp_usage_counts_direct_and_wrapped_calls_without_counting_messages(self):
        sessions = self.write_sessions([
            {'type': 'function_call', 'name': 'mcp__team_tool__list', 'arguments': '{}'},
            {'type': 'custom_tool_call', 'name': 'mcp__team_tool__read', 'input': '{}'},
            {'type': 'custom_tool_call', 'name': 'exec', 'input': 'await tools.mcp__team_tool__get({}); await tools.mcp__rec__list({}); await tools.mcp__rec__get({});'},
            {'type': 'function_call', 'name': 'custom_exec', 'input': 'await tools.mcp__rec__read({})'},
            {'type': 'message', 'role': 'user', 'content': 'tools.mcp__ignored__list'},
            {'type': 'function_call_output', 'name': 'mcp__ignored__list'},
        ])
        self.assertEqual(cx.skill_usage(30, sessions, include_mcp=True), ({}, {}, {'team_tool': 3, 'rec': 3}))
        with patch.object(cx, 'run', return_value=None):
            report = cx.retro_report(30, sessions)
        self.assertIn('## MCP 사용\n| 서버 | 호출 수 |', report)
        self.assertIn('| team_tool | 3 |', report)

    def test_skills_audit_classifies_external_and_local_sources(self):
        home = Path(self.home.name)
        settings = cx.state() / 'local/settings.json'
        data = json.loads(settings.read_text())
        data['banned_skills'] = ['banned', 'orca-*']
        settings.write_text(json.dumps(data))
        lock = {}
        for name in ('old', 'used', 'fresh', 'disabled', 'banned', 'orca-extra', 'kp-conductor'):
            self.make_skill(name, stale=name != 'fresh')
            lock[name] = {'source': 'stablyai/orca' if name == 'banned' else 'vendor/skills',
                          'sourceUrl': 'https://example.com/skills.git', 'updatedAt': '2026-01-01'}
        self.make_skill('local')
        installation = self.make_skill('pinned')
        (installation / 'INSTALLATION.json').write_text(json.dumps({'source_commit': 'abc123', 'update_policy': 'manual'}))
        target = self.make_skill('linked', root='external')
        (home / '.agents/skills/linked').symlink_to(target)
        (home / '.codex/skills').mkdir(parents=True)
        (home / '.codex/skills/used').symlink_to(home / '.agents/skills/used')
        (home / '.agents/.skill-lock.json').write_text(json.dumps({'skills': lock}))
        disabled_path = str(home / '.agents/skills/disabled/SKILL.md')
        (home / '.codex/config.toml').write_text('[[skills.config]]\npath = ' + json.dumps(disabled_path) + '\nenabled = false\n')
        self.write_sessions([
            {'type': 'custom_tool_call', 'name': 'exec', 'input': 'cat ~/.agents/skills/used/SKILL.md'},
            {'type': 'message', 'role': 'user', 'content': '$used'},
        ])
        with patch.object(cx, 'run', side_effect=AssertionError('offline audit must not invoke a network command')):
            audit = cx.skills_audit(home=home)
        rows = {row['name']: row for row in audit['skills']}
        self.assertEqual(audit['counts'], {'review': 3, 'review-duplicate': 0, 'banned': 2, 'disabled': 1, 'keep': 2, 'local': 1, 'native': 0, 'duplicates': 1})
        self.assertEqual(rows['local']['verdict'], 'local')
        self.assertNotIn('kp-conductor', rows)
        self.assertEqual((rows['used']['user_calls'], rows['used']['agent_reads']), (1, 1))
        self.assertEqual(len(rows['used']['paths']), 2)
        self.assertEqual(rows['old']['updatedAt'], '2026-01-01')
        self.assertEqual(rows['pinned']['source_commit'], 'abc123')
        self.assertEqual(rows['linked']['source'], str(target.resolve()))

    def test_skills_audit_disabled_wins_over_banned_setting(self):
        settings = cx.state() / 'local/settings.json'
        data = json.loads(settings.read_text())
        data['banned_skills'] = ['orca-*']
        settings.write_text(json.dumps(data))
        home = Path(self.home.name)
        self.make_skill('orca-cli')
        (home / '.agents/.skill-lock.json').write_text(json.dumps({'skills': {'orca-cli': {'source': 'stablyai/orca', 'sourceUrl': 'https://example.com/o.git', 'updatedAt': '2026-01-01'}}}))
        p = str(home / '.agents/skills/orca-cli/SKILL.md')
        (home / '.codex').mkdir(parents=True, exist_ok=True)
        (home / '.codex/config.toml').write_text('[[skills.config]]\npath = ' + json.dumps(p) + '\nenabled = false\n')
        self.write_sessions([])
        with patch.object(cx, 'run', side_effect=AssertionError('offline')):
            audit = cx.skills_audit(home=home)
        self.assertEqual({r['name']: r['verdict'] for r in audit['skills']}, {'orca-cli': 'disabled'})

    def test_pack_symlink_supports_qualified_names_reads_and_disabled_config(self):
        home = Path(self.home.name)
        target = self.make_skill('unused', root='external/pack')
        self.make_skill('active', root='external/pack')
        (home / '.agents/skills').mkdir(parents=True)
        (home / '.agents/skills/pack').symlink_to(target.parent)
        (home / '.codex').mkdir()
        (home / '.codex/config.toml').write_text("[[skills.config]]\nname = 'pack:unused'\nenabled = false # explicit\n")
        self.write_sessions([{'type': 'custom_tool_call', 'name': 'exec',
                              'input': 'cat ~/.agents/skills/pack/active/SKILL.md'}])
        rows = {row['name']: row for row in cx.skills_audit(home=home)['skills']}
        self.assertEqual(rows['pack:unused']['verdict'], 'disabled')
        self.assertEqual(rows['pack:active']['verdict'], 'keep')
        self.assertEqual(rows['pack:active']['agent_reads'], 1)

    def test_git_origins_are_local_and_remote_head_is_opt_in_with_failure_marked(self):
        directory = self.make_skill('git-skill')
        (directory / '.git').mkdir()
        with patch.object(cx, 'run', return_value='https://example.com/repo.git\n') as runner:
            audit = cx.skills_audit(home=self.home.name)
        runner.assert_called_once_with(['git', '-C', str(directory.resolve()), 'config', '--get', 'remote.origin.url'])
        self.assertEqual(audit['skills'][0]['source'], 'https://example.com/repo.git')
        with patch.object(cx, 'run', side_effect=['https://example.com/repo.git\n', None]) as runner:
            audit = cx.skills_audit(home=self.home.name, remote=True)
        self.assertEqual(audit['skills'][0]['upstream_head'], '미확인')
        self.assertEqual(runner.call_args.args[0], ['git', 'ls-remote', 'https://example.com/repo.git', 'HEAD'])

    def test_remote_head_uses_a_github_source_from_installation_json(self):
        directory = self.make_skill('pinned')
        (directory / 'INSTALLATION.json').write_text(json.dumps({'source': 'https://github.com/owner/repo',
                                                                'source_commit': 'a' * 40, 'update_policy': 'pinned'}))
        head = 'b' * 40
        with patch.object(cx, 'run', return_value=head + '\tHEAD\n') as runner:
            audit = cx.skills_audit(home=self.home.name, remote=True)
        row = audit['skills'][0]
        self.assertEqual((row['origin'], row['source_commit'], row['upstream_head']), ('external', 'a' * 40, head))
        self.assertEqual(runner.call_args.args[0], ['git', 'ls-remote', 'https://github.com/owner/repo', 'HEAD'])

    def test_skills_audit_cli_saves_json_and_status_uses_only_latest_saved_audit(self):
        home = Path(self.home.name)
        directory = self.make_skill('old')
        (directory / 'INSTALLATION.json').write_text('{"source_commit":"123"}')
        self.assertEqual(cx.skills_status(), 'skills: not audited')
        output = io.StringIO()
        with patch.object(cx.Path, 'home', return_value=home), contextlib.redirect_stdout(output):
            cx.main(['skills-audit', '--days', '7', '--stale-days', '40', '--json'])
        audit = json.loads(output.getvalue())
        saved = list((cx.state() / 'audits').glob('skills-????????.json'))
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0].read_text()), audit)
        self.assertEqual(audit['days'], 7)
        with patch.object(cx, 'skills_audit', side_effect=AssertionError('status must not scan')):
            self.assertEqual(cx.skills_status(), 'skills: 1 review, 0 review-duplicate, 0 banned, 0 duplicates, native 0, local 0')

    def role_definitions(self):
        return {
            'cx-read': {'model': 'model-read', 'effort': 'low', 'sandbox': 'read-only',
                        'job': '검토 "인용"과 \\경로', 'skills': ['allowed', 'kp-conductor']},
            'cx-write': {'model': 'model-write', 'effort': 'high', 'sandbox': None,
                         'job': '수정', 'skills': ['superpowers:testing']},
        }

    def sync_roles(self, *options):
        output = io.StringIO()
        with patch.object(cx, 'load_roles', return_value=self.role_definitions()), contextlib.redirect_stdout(output):
            cx.main(['roles', 'sync', '--home', self.home.name, *options])
        return output.getvalue()

    def test_roles_sync_generates_hidden_paths_registration_backup_and_escaped_strings(self):
        home = Path(self.home.name)
        allowed = self.make_skill('allowed', root='.codex/skills')
        conductor = self.make_skill('kp-conductor', root='.codex/skills')
        hidden = self.make_skill('hidden "한글"\\ 경로', root='.codex/skills')
        system = self.make_skill('builtin', root='.codex/skills/.system')
        duplicate = self.make_skill('hidden "한글"\\ 경로')
        config = home / '.codex/config.toml'
        original = '[agents]\nmax_threads = 4\n\n[agents.existing]\nconfig_file = "./existing.toml"\n\n[tools]\nmode = "keep"\n'
        config.write_text(original)
        output = self.sync_roles()
        content = (home / '.codex/agents/cx-read.toml').read_text()
        paths = cx.role_hidden_paths(content)
        self.assertNotIn(str(allowed / 'SKILL.md'), paths)
        self.assertIn(str(conductor / 'SKILL.md'), paths)
        for directory in (hidden, system, duplicate):
            self.assertIn(str(directory / 'SKILL.md'), paths)
        fields = dict((key, json.loads(value)) for key, value in cx.re.findall(r'^(\w+) = (".*")$', content, cx.re.M))
        self.assertEqual(fields['description'], self.role_definitions()['cx-read']['job'])
        self.assertEqual(fields['sandbox_mode'], 'read-only')
        self.assertIn('긴 결과는 요약하고 근거 경로를 붙여 돌려준다', fields['developer_instructions'])
        self.assertNotIn('파일로 저장', fields['developer_instructions'])
        write_content = (home / '.codex/agents/cx-write.toml').read_text()
        self.assertNotIn('sandbox_mode', write_content)
        self.assertIn('긴 결과는 파일로 저장', write_content)
        self.assertTrue(config.read_text().startswith(original))
        self.assertEqual(cx.registered_roles(config.read_text()), {'existing', 'cx-read', 'cx-write'})
        self.assertEqual(list(config.parent.glob('config.toml.before-roles-*')), [])
        self.assertEqual((cx.overlay_backups()[0] / 'config.toml').read_text(), original)
        self.assertIn('역할 2개, 스킬 총 5개', output)

    def test_roles_scan_plugin_layouts_and_pack_links_and_hide_alias_and_real_paths(self):
        home = Path(self.home.name)
        target = self.make_skill('testing', root='external/pack')
        (home / '.agents/skills').mkdir(parents=True)
        (home / '.agents/skills/superpowers').symlink_to(target.parent)
        linked = self.make_skill('private', root='external')
        (home / '.agents/skills/private').symlink_to(linked)
        versioned = self.make_skill('one', root='.codex/plugins/cache/vendor/plugin/1.2.3/skills')
        unversioned = self.make_skill('two', root='.codex/plugins/cache/vendor/plain/skills')
        hashed = self.make_skill('three', root='.codex/plugins/cache/vendor/hash-plugin/d6169bef/skills')
        ambiguous = self.make_skill('four', root='.codex/plugins/cache/vendor/opaque/current/skills')
        (ambiguous / 'SKILL.md').write_text('---\nname: actual-skill\n---\n')
        manifest = ambiguous.parent.parent / '.codex-plugin/plugin.json'
        manifest.parent.mkdir()
        manifest.write_text('{"name":"actual-plugin"}')
        skills = cx.role_skills(home)
        self.assertEqual({name for name, _ in skills}, {'superpowers:testing', 'private', 'plugin:one',
                         'plain:two', 'hash-plugin:three', 'actual-plugin:actual-skill'})
        self.sync_roles()
        content = (home / '.codex/agents/cx-write.toml').read_text()
        paths = cx.role_hidden_paths(content)
        self.assertNotIn(str(home / '.agents/skills/superpowers/testing/SKILL.md'), paths)
        self.assertNotIn(str(target.resolve() / 'SKILL.md'), paths)
        self.assertIn(str(home / '.agents/skills/private/SKILL.md'), paths)
        self.assertIn(str(linked.resolve() / 'SKILL.md'), paths)
        for directory in (versioned, unversioned, hashed, ambiguous):
            self.assertIn(str(directory / 'SKILL.md'), paths)

    def test_roles_dry_run_does_not_write_any_configuration_or_backup(self):
        home = Path(self.home.name)
        self.make_skill('allowed', root='.codex/skills')
        config = home / '.codex/config.toml'
        config.write_text('model = "existing"\n')
        output = self.sync_roles('--dry-run')
        self.assertIn('cx-read: 허용 1, 숨김 0, ', output)
        self.assertFalse((home / '.codex/agents').exists())
        self.assertEqual(config.read_text(), 'model = "existing"\n')
        self.assertEqual(list(config.parent.glob('config.toml.before-roles-*')), [])

    def test_roles_sync_rejects_existing_unowned_registration(self):
        home = Path(self.home.name)
        self.make_skill('allowed', root='.codex/skills')
        config = home / '.codex/config.toml'
        original = '[agents."cx-read"]\nconfig_file = "./custom.toml"\n'
        config.write_text(original)
        with self.assertRaises(SystemExit):
            self.sync_roles()
        self.assertEqual(config.read_text(), original)
        self.assertFalse((home / '.codex/agents/cx-read.toml').exists())

    def test_roles_sync_protects_non_generated_files_before_writing_any_role(self):
        home = Path(self.home.name)
        agents = home / '.codex/agents'
        agents.mkdir(parents=True)
        protected = agents / 'cx-write.toml'
        protected.write_text('name = "hand-written"\n')
        with self.assertRaisesRegex(SystemExit, 'non-generated role file'):
            self.sync_roles()
        self.assertEqual(protected.read_text(), 'name = "hand-written"\n')
        self.assertFalse((agents / 'cx-read.toml').exists())
        self.assertFalse((home / '.codex/config.toml').exists())

    def test_roles_warn_about_missing_and_globally_disabled_allowed_skills(self):
        home = Path(self.home.name)
        allowed = self.make_skill('allowed', root='.codex/skills')
        config = home / '.codex/config.toml'
        config.write_text('[[skills.config]]\npath = ' + json.dumps(str(allowed / 'SKILL.md')) + '\nenabled = false\n')
        output = self.sync_roles('--dry-run')
        self.assertIn('WARN role cx-read: allowed skill is globally disabled: allowed', output)
        self.assertIn('WARN role cx-write: allowed skill not found: superpowers:testing', output)

    def test_roles_health_detects_hidden_list_drift_against_current_skill_tree(self):
        home = Path(self.home.name)
        self.make_skill('allowed', root='.codex/skills')
        self.sync_roles()
        self.assertEqual(cx.role_health(home, self.role_definitions()), [('cx-read', True, True, False), ('cx-write', True, True, False)])
        self.make_skill('new-skill', root='.codex/skills')
        self.assertEqual(cx.role_health(home, self.role_definitions()), [('cx-read', True, True, True), ('cx-write', True, True, True)])
        self.sync_roles()
        self.assertTrue(all(not drift for _, _, _, drift in cx.role_health(home, self.role_definitions())))

    def test_proposals_status_counts_only_pending_first_lines(self):
        self.assertEqual(cx.proposals_status(), 'proposals: none')
        directory = cx.state() / 'proposals'
        directory.mkdir()
        (directory / 'applied.md').write_text('상태: 적용됨\n본문\n')
        (directory / 'rejected.md').write_text('# 상태: 거절됨\n')
        (directory / 'pending.md').write_text('# 검토 제안\n상태: 적용됨\n')
        (directory / 'empty.md').write_text('')
        (directory / 'ignored.txt').write_text('대기 중')
        self.assertEqual(cx.proposals_status(), 'proposals: 2 pending (~/.codex/kp-conductor/proposals)')

    def test_checkpoint_cli_records_classification_and_delegation_and_creates_default_profile_once(self):
        project = str(Path(self.home.name) / 'project')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cx.main(['checkpoint', 'start', '--project', project, '--steps', 'read|verify', '--type', 'C', '--mode', '꼼꼼하게', '--mode-source', 'user'])
        self.assertIn('profile: auto-created', output.getvalue())
        _, started = cx.load_checkpoint(project)
        self.assertEqual((started['type'], started['mode']), ('C', '프론티어'))
        profile = cx.read_profile(project)
        self.assertEqual({key: profile[key] for key in ('mode', 'risk', 'domain', 'verify', 'note', 'by')},
                         {'mode': '균형', 'risk': 'R2', 'domain': 'engineering', 'verify': '',
                          'note': '자동 생성, 회고가 영수증 5건 이상에서 조정', 'by': 'auto'})
        with contextlib.redirect_stdout(io.StringIO()):
            cx.main(['checkpoint', 'finish', '--project', project, '--delegated', '2'])
        _, finished = cx.load_checkpoint(project, started['id'])
        self.assertEqual((finished['delegated'], finished['outcome']), (2, 'pass'))
        profile['mode'], profile['by'] = '아껴서', 'self'
        cx.profile_path(project).write_text(json.dumps(profile))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cx.main(['checkpoint', 'start', '--project', project, '--steps', 'legacy', '--mode', '균형', '--mode-source', 'default'])
        self.assertNotIn('profile: auto-created', output.getvalue())
        self.assertEqual(cx.read_profile(project), {**profile, 'mode': '균형', 'saving': True})
        self.assertEqual(json.loads(cx.profile_path(project).read_text()), profile)
        _, legacy = cx.load_checkpoint(project)
        self.assertNotIn('type', legacy)
        self.assertEqual((legacy['mode'], legacy['mode_source']), ('균형', 'default'))
        with self.assertRaisesRegex(SystemExit, 'delegated must be nonnegative'):
            cx.checkpoint_update(project, None, None, outcome='pass', delegated=-1)
        self.assertEqual(cx.checkpoint_update(project, None, None, outcome='pass', delegated=0)['delegated'], 0)

    def test_opencodex_logs_group_each_conversation_by_earliest_model_and_http_status(self):
        current = time.time()
        rows = [
            {'conversationId': 'one', 'timestamp': current * 1000, 'model': 'worker', 'requestedEffort': 'high', 'status': 201},
            {'conversationId': 'two', 'timestamp': current - 30, 'model': 'worker', 'effectiveEffort': 'low', 'status': 200},
            {'conversationId': 'one', 'timestamp': current - 10 * 86400, 'model': 'main', 'requestedEffort': 'max', 'status': 200},
            {'conversationId': 'one', 'timestamp': current - 5, 'model': 'worker', 'requestedEffort': 'high', 'status': 503},
            {'conversationId': 'two', 'timestamp': cx.now().isoformat(), 'model': 'main', 'effort': 'medium', 'status': '204'},
            {'timestamp': current, 'model': 'unlinked', 'status': 0},
            {'conversationId': 'one', 'timestamp': 'bad', 'model': 'worker', 'status': 200},
        ]
        with patch.object(cx, 'run', return_value=json.dumps({'logs': rows})) as runner:
            result = cx.opencodex_delegations(7)
        runner.assert_called_once_with([cx.ocx_path(), 'logs', '--limit', '2000', '--json'], 30)
        self.assertEqual(result['groups'], {
            ('서브에이전트 요청', 'worker', 'high', '2xx'): 1,
            ('서브에이전트 요청', 'worker', 'high', '기타'): 1,
            ('메인', 'worker', 'low', '2xx'): 1,
            ('서브에이전트 요청', 'main', 'medium', '2xx'): 1,
            ('미확인', 'unlinked', '미확인', '기타'): 1,
        })
        self.assertEqual((result['uncorrelated'], result['undated']), (1, 1))
        with patch.object(cx, 'run', side_effect=lambda args, timeout=20: json.dumps({'logs': rows}) if args[1] == 'logs' else None):
            report = cx.retro_report(7, Path(self.home.name) / 'none')
        self.assertIn('## OpenCodex 로그 기준 위임', report)
        self.assertIn('| 서브에이전트 요청 | worker | high | 기타 | 1 |', report)
        for bad in (None, 'not json', '{"unrelated":true}'):
            with patch.object(cx, 'run', return_value=bad):
                self.assertIsNone(cx.opencodex_delegations(7))
        with patch.object(cx, 'run', return_value=None):
            self.assertIn('미확인: ocx logs', cx.retro_report(7, Path(self.home.name) / 'none'))

    def test_retro_classification_counts_missing_types_and_averages_only_recorded_delegations(self):
        directory = cx.state() / 'runs' / 'fixture'
        directory.mkdir(parents=True)
        for index, row in enumerate([
            {'type': 'C', 'delegated': 0, 'outcome': 'pass', 'status': 'done'},
            {'type': 'C', 'delegated': 2, 'outcome': 'fail', 'status': 'done'},
            {'type': 'C', 'outcome': 'partial', 'status': 'done'},
            {'type': 'D', 'outcome': None, 'status': 'open'},
            {'outcome': None, 'status': 'open'},
            {'type': 'B', 'updated': '2020-01-01T00:00:00Z', 'status': 'open'},
        ]):
            row.setdefault('updated', cx.now().isoformat())
            (directory / ('%d.json' % index)).write_text(json.dumps(row))
        with patch.object(cx, 'run', return_value=None):
            report = cx.retro_report(7, Path(self.home.name) / 'none')
        self.assertIn('## 분류', report)
        self.assertIn('| C | 3 | 1.00 | {"fail": 1, "partial": 1, "pass": 1} |', report)
        self.assertIn('| D | 1 | 미기록 | {"open": 1} |', report)
        self.assertNotIn('| B |', report)
        self.assertIn('type 미기록 1개.', report)

    def test_retro_sample_exports_recent_unique_user_text_and_excludes_injected_blocks(self):
        root = Path(self.home.name) / 'sessions'
        root.mkdir()
        current = cx.now().astimezone(cx.datetime.timezone.utc)
        rows = []
        bodies = ['old request', '중복 요청', '# AGENTS.md instructions\n시스템 안내', '<environment_context>injected</environment_context>',
                  '중복   요청', '가' * 350]
        for index, body in enumerate(bodies):
            stamp = current - cx.datetime.timedelta(days=50) if index == 0 else current - cx.datetime.timedelta(seconds=100 - index)
            rows.append({'timestamp': stamp.isoformat(), 'payload': {'type': 'message', 'role': 'user',
                         'content': [{'type': 'input_text', 'text': body}]}})
        rows.append({'timestamp': (current - cx.datetime.timedelta(seconds=200)).isoformat(),
                     'payload': {'type': 'user_message', 'message': '이전 요청'}})
        rows.append({'timestamp': current.isoformat(), 'payload': {'type': 'message', 'role': 'assistant', 'content': '제외'}})
        (root / 'one.jsonl').write_text('\n'.join(json.dumps(row) for row in rows) + '\nnot json\n')
        samples = cx.sample_requests(7, 10, root)
        self.assertEqual([row['text'] for row in samples], ['가' * 300, '중복   요청', '이전 요청'])
        output = io.StringIO()
        with patch.object(cx, 'SESSIONS', root), patch.object(cx, 'retro_report', return_value='# report\n'), contextlib.redirect_stdout(output):
            cx.main(['retro', '--sample', '2'])
        saved = list((cx.state() / 'retro').glob('sample-*.jsonl'))
        self.assertEqual(len(saved), 1)
        exported = [json.loads(line) for line in saved[0].read_text().splitlines()]
        self.assertEqual([row['text'] for row in exported], ['가' * 300, '중복   요청'])
        self.assertIn('sample: ' + str(saved[0]), output.getvalue())
        with patch.object(cx, 'sample_requests', side_effect=AssertionError('default sample must not scan')):
            with patch.object(cx, 'retro_report', return_value='# report\n'), contextlib.redirect_stdout(io.StringIO()):
                cx.main(['retro'])

    def test_duplicate_audit_includes_local_content_differences_links_and_disabled_paths(self):
        home = Path(self.home.name)
        for root in ('.codex/skills', '.agents/skills'):
            self.make_skill('same', root=root)
            self.make_skill('different', root=root)
        (home / '.agents/skills/different/SKILL.md').write_text('different content')
        target = self.make_skill('linked', root='.agents/skills')
        (home / '.codex/skills/linked').symlink_to(target)
        disabled = home / '.codex/skills/same/SKILL.md'
        (home / '.codex/config.toml').write_text('[[skills.config]]\npath = ' + json.dumps(str(disabled)) + '\nenabled = false\n')
        audit = cx.skills_audit(home=home)
        duplicates = {row['name']: row for row in audit['duplicates']}
        self.assertEqual(audit['counts']['duplicates'], 3)
        self.assertEqual({row['name']: row['verdict'] for row in audit['skills']}['same'], 'disabled')
        self.assertTrue(duplicates['same']['same'])
        self.assertFalse(duplicates['same']['same_realpath'])
        self.assertFalse(duplicates['different']['same'])
        self.assertTrue(duplicates['linked']['same_realpath'])
        self.assertEqual([entry['disabled'] for entry in duplicates['same']['paths']], [True, False])
        for duplicate in duplicates.values():
            for entry in duplicate['paths']:
                self.assertEqual(entry['realpath'], str(Path(entry['path']).resolve()))
                self.assertEqual(entry['sha256'], cx.hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest())
        directory = cx.state() / 'audits'
        directory.mkdir()
        (directory / 'skills-20261113.json').write_text(json.dumps(audit))
        self.assertEqual(cx.skills_status(), 'skills: 1 review, 1 review-duplicate, 0 banned, 3 duplicates, native 0, local 0')


    def write_routing(self, routes=None, mcp_usage=None):
        data = {'mcp_usage': mcp_usage or {}, 'routes': routes or []}
        path = self.skill_root / 'references/routing.json'
        path.write_text(json.dumps(data, ensure_ascii=False))
        return path

    def route_definition(self, identifier='ui-ux', **changes):
        route = {'id': identifier, 'label': 'UI·UX 설계', 'keywords': ['ui', 'ux', '화면', '디자인'],
                 'skills': ['ui-combo', 'plugin:one', 'missing'], 'role': 'cx-build',
                 'risk': 'R1', 'note': '한 줄'}
        route.update(changes)
        return route

    def test_audit_includes_local_system_and_plugin_skills_with_usage_and_disable(self):
        home = Path(self.home.name)
        self.make_skill('local')
        self.make_skill('builtin', root='.codex/skills/.system')
        self.make_skill('one', root='.codex/plugins/cache/vendor/plugin/1.0/skills')
        self.make_skill('off', root='.codex/skills/.system')
        (home / '.codex/config.toml').write_text("[[skills.config]]\nname = 'off'\nenabled = false\n")
        self.write_sessions([
            {'type': 'message', 'role': 'user', 'content': '$local $builtin $plugin:one'},
            {'type': 'custom_tool_call', 'name': 'exec', 'input': 'cat ~/.agents/skills/local/SKILL.md'},
        ])
        audit = cx.skills_audit(home=home)
        rows = {row['name']: row for row in audit['skills']}
        self.assertEqual({name: row['verdict'] for name, row in rows.items()},
                         {'local': 'local', 'builtin': 'native', 'plugin:one': 'native', 'off': 'disabled'})
        self.assertEqual(rows['local']['origin'], 'local')
        self.assertEqual(rows['plugin:one']['origin'], 'native')
        self.assertEqual(rows['builtin']['origin'], 'native')
        self.assertTrue(all(row['reason'] and row['mcp_calls'] == 0 for row in rows.values()))
        self.assertTrue(all(row['user_calls'] == 1 for name, row in rows.items() if name != 'off'))
        self.assertEqual(rows['local']['agent_reads'], 1)
        self.assertEqual((audit['counts']['local'], audit['counts']['native']), (1, 2))
        output = io.StringIO()
        with patch.object(cx.Path, 'home', return_value=home), contextlib.redirect_stdout(output):
            cx.main(['skills-audit'])
        for column in ('origin', 'mcp_calls', 'reason'):
            self.assertIn(column, output.getvalue().splitlines()[0])


    def test_audit_plugin_tool_reads_use_qualified_role_skill_names(self):
        home = Path(self.home.name)
        directory = self.make_skill('one', root='.codex/plugins/cache/vendor/plugin/1.0/skills')
        self.write_sessions([{'type': 'custom_tool_call', 'name': 'exec',
                              'input': 'cat ' + str(directory / 'SKILL.md')}])
        row = cx.skills_audit(home=home)['skills'][0]
        self.assertEqual((row['name'], row['agent_reads']), ('plugin:one', 1))

    def test_audit_maps_mcp_prefixes_and_protects_routed_and_role_allowed_skills(self):
        home = Path(self.home.name)
        names = ('rec-read', 'rec-cli', 'doc-tool', 'ui-combo', 'routed', 'allowed', 'unused')
        for name in names:
            self.make_skill(name)
        (home / '.agents/.skill-lock.json').write_text(json.dumps({'skills': {name: {'source': 'vendor/skills'} for name in names}}))
        self.write_routing([self.route_definition(skills=['routed'])],
                           {'rec-*': ['rec'], 'doc-tool': ['doc-tool'], 'ui-combo': ['stylegallery', 'mobbin']})
        (self.skill_root / 'references/roles.json').write_text(json.dumps({'roles': self.role_definitions()}))
        self.write_sessions([{'type': 'function_call', 'name': 'mcp__%s__read' % name} for name in
                             ('rec', 'rec', 'doc-tool', 'stylegallery', 'mobbin')])
        rows = {row['name']: row for row in cx.skills_audit(home=home)['skills']}
        self.assertEqual([rows[name]['mcp_calls'] for name in names], [2, 2, 1, 2, 0, 0, 0])
        for name in names[:-1]:
            self.assertEqual(rows[name]['verdict'], 'keep')
        for name in ('routed', 'allowed'):
            self.assertEqual(rows[name]['reason'], 'routed')
        self.assertEqual(rows['unused']['verdict'], 'review')
        self.assertTrue(all(row['origin'] == 'external' and row['reason'] for row in rows.values()))

    def test_audit_duplicate_suggests_newest_content_without_modifying_files(self):
        home = Path(self.home.name)
        for name in ('local-dupe', 'external-dupe', 'off-dupe'):
            self.make_skill(name, root='.codex/skills')
            other = self.make_skill(name)
            (other / 'SKILL.md').write_text('new content')
        (home / '.agents/.skill-lock.json').write_text('{"skills":{"external-dupe":{"source":"vendor/skills"}}}')
        (home / '.codex/config.toml').write_text("[[skills.config]]\nname = 'off-dupe'\nenabled = false\n")
        audit = cx.skills_audit(home=home)
        self.assertEqual({row['name']: row['verdict'] for row in audit['skills']},
                         {'local-dupe': 'review-duplicate', 'external-dupe': 'review-duplicate', 'off-dupe': 'disabled'})
        for duplicate in audit['duplicates']:
            expected = home / '.agents/skills' / duplicate['name'] / 'SKILL.md'
            self.assertEqual(duplicate['canonical_suggestion'], str(expected))
            self.assertTrue(duplicate['reason'])
            self.assertEqual(expected.read_text(), 'new content')

    def test_checkpoint_skill_receipts_on_finish_and_update_and_null_fit(self):
        project = self.home.name
        cx.checkpoint_start(project, '작업', 'a|b', task_type='C', mode='균형', mode_source='default')
        with contextlib.redirect_stdout(io.StringIO()):
            cx.main(['checkpoint', 'update', '--project', project, '--done', '1'])
        _, opened = cx.load_checkpoint(project)
        self.assertEqual(opened['status'], 'open')
        self.assertFalse((cx.state() / 'skill-receipts.jsonl').exists())
        with contextlib.redirect_stdout(io.StringIO()):
            cx.main(['checkpoint', 'finish', '--project', project, '--skills', ' a, b,a, ', '--skill-fit', 'good'])
        _, finished = cx.load_checkpoint(project, opened['id'])
        self.assertEqual(finished['skills'], ['a', 'b'])
        self.assertEqual(finished['skill_fit'], 'good')
        receipt = json.loads((cx.state() / 'skill-receipts.jsonl').read_text())
        self.assertEqual(receipt, {'ts': finished['updated'], 'project': str(Path(project).resolve()),
                                  'checkpoint': finished['id'], 'type': 'C', 'skills': ['a', 'b'],
                                  'fit': 'good', 'outcome': 'pass'})
        cx.checkpoint_start(project, '작업', 'a|b', mode='균형', mode_source='default')
        with contextlib.redirect_stdout(io.StringIO()):
            cx.main(['checkpoint', 'update', '--project', project, '--done', '1', '--outcome', 'partial', '--skills', 'a'])
        receipts = [json.loads(line) for line in (cx.state() / 'skill-receipts.jsonl').read_text().splitlines()]
        self.assertEqual((receipts[1]['fit'], receipts[1]['type'], receipts[1]['outcome']), (None, None, 'partial'))
        cx.checkpoint_start(project, '작업', 'a', mode='균형', mode_source='default')
        with contextlib.redirect_stdout(io.StringIO()):
            cx.main(['checkpoint', 'finish', '--project', project, '--skills', ', ,'])
        self.assertEqual(len((cx.state() / 'skill-receipts.jsonl').read_text().splitlines()), 2)

    def test_retro_skill_performance_filters_period_and_counts_all_fit_values(self):
        rows = [{'ts': cx.now().isoformat(), 'skills': ['a', 'b'], 'fit': fit}
                for fit in ('good', 'poor', 'none', None)]
        rows.append({'ts': '2020-01-01T00:00:00Z', 'skills': ['old'], 'fit': 'good'})
        (cx.state() / 'skill-receipts.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
        with patch.object(cx, 'run', return_value=None):
            report = cx.retro_report(7, Path(self.home.name) / 'none')
        self.assertIn('## 스킬 성과', report)
        self.assertIn('| a | 4 | 1 | 1 | 1 | 1 |', report)
        self.assertIn('| b | 4 | 1 | 1 | 1 | 1 |', report)
        self.assertNotIn('| old |', report)
        with patch.object(cx, 'run', return_value=None):
            report = cx.retro_report(1, Path(self.home.name) / 'none')
        self.assertIn('| a | 4 |', report)

    def test_retro_skill_performance_without_receipts(self):
        with patch.object(cx, 'run', return_value=None):
            report = cx.retro_report(7, Path(self.home.name) / 'none')
        self.assertIn('## 스킬 성과\n기록 없음.', report)

    def test_routing_loader_accepts_explicit_path_and_rejects_invalid_shapes(self):
        path = self.write_routing([self.route_definition()])
        self.assertEqual(cx.load_routing(path)['routes'][0]['id'], 'ui-ux')
        bad_values = [None, {}, {'mcp_usage': [], 'routes': []},
                      {'mcp_usage': {'bad*pattern': ['server']}, 'routes': []},
                      {'mcp_usage': {'skill': 'server'}, 'routes': []},
                      {'mcp_usage': {}, 'routes': [{'id': 'broken'}]},
                      {'mcp_usage': {}, 'routes': [self.route_definition(keywords=[''])]}]
        for value in bad_values:
            path.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, 'invalid routing'):
                cx.load_routing(path)
        path.write_text('not json')
        with self.assertRaisesRegex(SystemExit, 'invalid routing'):
            cx.load_routing(path)

    def test_route_matches_ascii_boundaries_korean_substrings_paths_and_allowlist(self):
        home = Path(self.home.name)
        skill = self.make_skill('ui-combo', root='.codex/skills')
        self.make_skill('ui-combo')
        plugin = self.make_skill('one', root='.codex/plugins/cache/vendor/plugin/1.0/skills')
        self.write_routing([self.route_definition()])
        roles = {'cx-build': {'skills': ['ui-combo', 'missing']}}
        with patch.object(cx, 'load_roles', return_value=roles), patch.object(cx.Path, 'home', return_value=home):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cx.main(['route', 'build'])
            self.assertEqual(output.getvalue(), 'route: none\n' + cx.ROUTE_REMINDER + '\n')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cx.main(['route', 'UI 화면설계를 UX로 build', '--json'])
            result = json.loads(output.getvalue())[0]
            self.assertEqual(result['keywords'], ['ui', 'ux', '화면'])
            self.assertEqual(result['score'], 3)
            self.assertEqual({key: result[key] for key in ('id', 'label', 'role', 'risk', 'note')},
                             {key: self.route_definition()[key] for key in ('id', 'label', 'role', 'risk', 'note')})
            self.assertEqual(result['skills'], [
                {'name': 'ui-combo', 'path': next(path for name, path in cx.role_skills(home) if name == 'ui-combo'), 'allowed': True},
                {'name': 'plugin:one', 'path': str(plugin / 'SKILL.md'), 'allowed': False},
                {'name': 'missing', 'path': '미설치', 'allowed': True}])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cx.main(['route', '화면설계'])
            self.assertIn('미설치', output.getvalue())
            self.assertIn('allowed=false', output.getvalue())
            self.assertIn('UI·UX 설계', output.getvalue())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cx.main(['route', 'build', '--json'])
            self.assertEqual(json.loads(output.getvalue()), [])

    def test_route_orders_by_score_then_file_order_and_limits_top(self):
        home = Path(self.home.name)
        self.write_routing([self.route_definition('first', keywords=['ui']),
                            self.route_definition('second', keywords=['ui', 'ux']),
                            self.route_definition('third', keywords=['ui', 'ux'])])
        with patch.object(cx.Path, 'home', return_value=home), contextlib.redirect_stdout(io.StringIO()) as output:
            cx.main(['route', 'UI UX ui', '--top', '2', '--json'])
        results = json.loads(output.getvalue())
        self.assertEqual([(row['id'], row['score']) for row in results], [('second', 2), ('third', 2)])
        with self.assertRaisesRegex(SystemExit, 'top must be positive'):
            cx.main(['route', 'ui', '--top', '0'])

    def test_route_requires_gates_a_route_on_at_least_one_listed_keyword(self):
        home = Path(self.home.name)
        self.write_routing([
            self.route_definition('general', keywords=['화면', '디자인']),
            self.route_definition('gated', keywords=['ai 티', '화면', '대시보드', '디자인'], requires=['ai 티'])])
        def ids(text):
            with patch.object(cx.Path, 'home', return_value=home), contextlib.redirect_stdout(io.StringIO()) as output:
                cx.main(['route', text, '--json'])
            return [row['id'] for row in json.loads(output.getvalue())]
        # Screen words alone never turn the gated route on, however many match.
        self.assertEqual(ids('대시보드 화면 디자인 만들어줘'), ['general'])
        # One required word turns it on, and the screen words then add to its score.
        self.assertEqual(ids('이 대시보드 화면 AI 티 없애줘'), ['gated', 'general'])

    def test_route_requires_groups_need_one_match_each_unless_a_sufficient_keyword_matched(self):
        home = Path(self.home.name)
        self.write_routing([self.route_definition('gated', keywords=['unslop', 'ai 티', '화면', '대시보드'],
                                                  sufficient=['unslop'], requires=[['ai 티'], ['화면', '대시보드']])])
        def ids(text):
            with patch.object(cx.Path, 'home', return_value=home), contextlib.redirect_stdout(io.StringIO()) as output:
                cx.main(['route', text, '--json'])
            return [row['id'] for row in json.loads(output.getvalue())]
        self.assertEqual(ids('이 글 AI 티 없애줘'), [])
        self.assertEqual(ids('대시보드 화면 만들어줘'), [])
        self.assertEqual(ids('이 대시보드 AI 티 없애줘'), ['gated'])
        self.assertEqual(ids('unslop this'), ['gated'])
        for bad in ({'requires': [['ai 티'], []]}, {'requires': [['ai 티'], ['missing']]}, {'sufficient': []},
                    {'sufficient': ['missing']}, {'requires': [['ai 티'], '화면']}):
            path = self.write_routing([self.route_definition('gated', keywords=['unslop', 'ai 티', '화면'], **bad)])
            with self.assertRaisesRegex(SystemExit, 'invalid routing definitions'):
                cx.load_routing(path)

    def test_load_routing_rejects_requires_outside_keywords_or_not_a_string_list(self):
        for requires in (['missing'], [], 'ai 티', [1]):
            path = self.write_routing([self.route_definition('gated', keywords=['ai 티'], requires=requires)])
            with self.assertRaisesRegex(SystemExit, 'invalid routing definitions'):
                cx.load_routing(path)

    def test_route_exclude_keyword_turns_a_matched_route_off(self):
        home = Path(self.home.name)
        self.write_routing([self.route_definition('data', keywords=['차트', '대시보드'], exclude=['플로우차트', '흐름도'])])
        def ids(text):
            with patch.object(cx.Path, 'home', return_value=home), contextlib.redirect_stdout(io.StringIO()) as output:
                cx.main(['route', text, '--json'])
            return [row['id'] for row in json.loads(output.getvalue())]
        self.assertEqual(ids('매출 차트 그려줘'), ['data'])
        # "차트" sits inside "플로우차트"; the exclude keyword vetoes the route.
        self.assertEqual(ids('README 플로우차트 고치기'), [])
        self.assertEqual(ids('대시보드 흐름도 정리'), [])
        for exclude in ([], ['차트'], 'x', [1]):
            path = self.write_routing([self.route_definition('data', keywords=['차트'], exclude=exclude)])
            with self.assertRaisesRegex(SystemExit, 'invalid routing definitions'):
                cx.load_routing(path)

    def test_shipped_routing_keeps_flowchart_requests_out_of_data(self):
        routing = cx.load_routing(SCRIPT.parent.parent / 'references' / 'routing.json')
        ids = lambda text: [row['id'] for row in cx.route_matches(text, routing, {}, [])]
        self.assertNotIn('data', ids('README 플로우차트 고치기'))
        self.assertIn('data', ids('매출 차트 만들어줘'))

    def test_shipped_routing_sends_screen_ai_look_to_unslop_and_text_to_korean_writing(self):
        routing = cx.load_routing(SCRIPT.parent.parent / 'references' / 'routing.json')
        def first(text):
            rows = cx.route_matches(text, routing, {}, [])
            return rows[0]['id'] if rows else None
        cases = {
            '이 대시보드 AI 티 없애줘': 'ui-unslop',
            '이 화면 AI스러운 느낌 좀 빼줘': 'ui-unslop',
            '템플릿 같은 화면 고쳐줘': 'ui-unslop',
            '언슬롭 영상 만들어줘': 'ui-unslop',
            'unslop this dashboard': 'ui-unslop',
            '랜딩 페이지가 양산형 같아': 'ui-unslop',
            '이 대시보드 슬롭 없애줘': 'ui-unslop',
            '이 글 AI 티 없애줘': 'korean-writing',
            '이 글 AI티 없애줘': 'korean-writing',
            '보도자료 번역투 윤문해줘': 'korean-writing',
        }
        for text, expected in cases.items():
            self.assertEqual(first(text), expected, text)
        for text in ('바이브코딩으로 앱 만들어줘', '대시보드 화면 만들어줘', '랜딩 페이지 디자인 다듬어줘',
                     '양산형 설비 견적 비교해줘', 'AI 느낌의 이미지 만들어줘', 'AI가 만든 보고서 요약해줘',
                     '이 글 템플릿 느낌 없애줘', '보고서 AI 느낌 빼줘', '이 글 AI스러운 느낌 없애줘', '이 보고서가 템플릿 같아',
                     '이 글 슬롭 없애줘', '보고서 바이브코딩 티 없애줘', 'remove the slop from this essay'):
            ids = [row['id'] for row in cx.route_matches(text, routing, {}, [])]
            self.assertNotIn('ui-unslop', ids, text)

    def doctor_output(self):
        home = Path(self.home.name)
        with patch.object(cx.Path, 'home', return_value=home), patch.object(cx, 'live_models', return_value=['worker']), \
                patch.object(cx, 'ocx_path', return_value='/bin/sh'), patch.object(cx, 'role_health', return_value=[]), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                cx.main(['doctor'])
        return output.getvalue(), result.exception.code

    def write_opencodex_config(self, config):
        path = Path(self.home.name) / '.opencodex' / 'config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config if isinstance(config, str) else json.dumps(config))
        return Path(self.home.name)

    def test_web_search_sidecars_follow_opencodex_request_paths(self):
        home = self.write_opencodex_config({
            'webSearchSidecar': {'backend': 'anthropic', 'model': 'claude-haiku-5-5'},
            'claudeCode': {'webSearchSidecar': {'backend': 'openai', 'model': 'gpt-6.1-terra'}}})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENCODEX_HOME', None)
            self.assertEqual(cx.web_search_sidecars(home), {'codex': ('anthropic', 'claude-haiku-5-5'),
                                                            'claude-code': ('openai', 'gpt-6.1-terra')})
            self.write_opencodex_config({'webSearchSidecar': {'model': 'gpt-6.1-terra'}, 'claudeCode': {'webSearchSidecar': {}}})
            self.assertEqual(cx.web_search_sidecars(home), {'codex': ('openai', 'gpt-6.1-terra'),
                                                            'claude-code': ('openai', 'gpt-6.1-terra', 'inherited')})
            for claude in (None, {'webSearchSidecar': None}, {}):
                self.write_opencodex_config({'claudeCode': claude})
                self.assertEqual(cx.web_search_sidecars(home), {'codex': ('openai', 'default')}, claude)
            self.write_opencodex_config({'claudeCode': {'webSearchSidecar': 'anthropic'}})
            self.assertEqual(cx.web_search_sidecars(home), {'codex': ('openai', 'default'), 'claude-code': ('unknown', 'unknown')})
            self.write_opencodex_config({'webSearchSidecar': {'backend': 'bogus', 'enabled': True}})
            self.assertEqual(cx.web_search_sidecars(home), {'codex': ('openai', 'default')})
            self.write_opencodex_config({'webSearchSidecar': {'backend': 'gemini', 'enabled': False}})
            self.assertEqual(cx.web_search_sidecars(home), {'codex': ('disabled', 'default')})
            self.write_opencodex_config('{')
            self.assertEqual(cx.web_search_sidecars(home), {'codex': ('unknown', 'unknown')})
            other = Path(self.home.name) / 'ocx-home'
            other.mkdir()
            (other / 'config.json').write_text(json.dumps({'webSearchSidecar': {'backend': 'xai', 'model': 'grok-4.7'}}))
            os.environ['OPENCODEX_HOME'] = str(other)
            self.assertEqual(cx.web_search_sidecars(home), {'codex': ('xai', 'grok-4.7')})

    def test_web_search_switch_targets_the_block_that_selects_anthropic(self):
        self.assertEqual(cx.websearch_status({'codex': ('openai', 'gpt-6.1-terra')}), 'websearch: codex openai/gpt-6.1-terra')
        line = cx.websearch_status({'codex': ('gemini', 'gemini-3.8-flash'), 'claude-code': ('anthropic', 'claude-haiku-5-5')})
        self.assertTrue(line.startswith('websearch: codex gemini/gemini-3.8-flash; claude-code anthropic/claude-haiku-5-5 ('))
        self.assertIn(cx.WEB_SEARCH_CLAUDE_SWITCH, line)
        self.assertNotIn(cx.WEB_SEARCH_SWITCH, line)
        inherited = cx.websearch_status({'codex': ('anthropic', 'm'), 'claude-code': ('anthropic', 'm', 'inherited')})
        self.assertIn(cx.WEB_SEARCH_SWITCH, inherited)
        self.assertNotIn(cx.WEB_SEARCH_CLAUDE_SWITCH, inherited)

    def test_doctor_warns_on_anthropic_web_search_without_changing_exit_code(self):
        base = {'subagentModels': ['worker'], 'advisor': {'enabled': True, 'model': 'claude-fable'}, 'subagentEffortCap': 'xhigh'}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENCODEX_HOME', None)
            self.write_opencodex_config(dict(base, webSearchSidecar={'backend': 'openai', 'model': 'gpt-6.1-terra'}))
            output, code = self.doctor_output()
            self.assertIn('OK   web search sidecar (codex): backend=openai model=gpt-6.1-terra\n', output)
            self.assertIn('INFO web search sidecar check reads the config only', output)
            self.write_opencodex_config(dict(base, webSearchSidecar={'backend': 'gemini', 'model': 'gemini-3.8-flash'},
                                             claudeCode={'webSearchSidecar': {'backend': 'anthropic', 'model': 'claude-haiku-5-5'}}))
            output, warned = self.doctor_output()
        self.assertIn('OK   web search sidecar (codex): backend=gemini model=gemini-3.8-flash; shares short-term quota', output)
        self.assertIn('WARN web search sidecar (claude-code): backend=anthropic model=claude-haiku-5-5; 64KB response cap', output)
        self.assertIn('switch: ' + cx.WEB_SEARCH_CLAUDE_SWITCH, output)
        self.assertEqual(warned, code)

    def test_doctor_validates_routing_and_warns_without_failing_on_missing_or_disallowed(self):
        home = Path(self.home.name)
        self.make_skill('ui-combo')
        config_dir = home / '.opencodex'
        config_dir.mkdir()
        (config_dir / 'config.json').write_text(json.dumps({
            'subagentModels': ['worker'], 'advisor': {'enabled': True, 'model': 'claude-fable'},
            'subagentEffortCap': 'xhigh'}))
        (home / '.codex').mkdir(exist_ok=True)
        (home / '.codex/config.toml').write_text('model = ' + json.dumps('worker'))
        (self.skill_root / 'references/entry-block.md').write_text('fixture entry\n')
        cx.main(['entry', 'install', '--home', str(home)])
        self.write_routing([self.route_definition(skills=['ui-combo', 'missing'])])
        with patch.object(cx, 'load_roles', return_value={'cx-build': {'model': 'worker', 'skills': ['ui-combo', 'unused']}}):
            output, code = self.doctor_output()
        self.assertEqual(code, 0)
        self.assertIn('OK   routing.json:', output)
        self.assertIn('WARN route skills installed: missing', output)
        self.assertIn('WARN route skills allowed: ui-ux/cx-build: missing', output)
        self.assertIn('INFO role skills without routes: unused', output)
        self.write_routing([self.route_definition(skills=['ui-combo'])])
        with patch.object(cx, 'load_roles', return_value={'cx-build': {'model': 'worker', 'skills': ['ui-combo']}}):
            output, _ = self.doctor_output()
        self.assertIn('OK   route skills installed:', output)
        self.assertIn('OK   route skills allowed:', output)
        (self.skill_root / 'references/routing.json').write_text('{')
        output, code = self.doctor_output()
        self.assertIn('FAIL routing.json:', output)
        self.assertEqual(code, 1)


    def test_route_skips_disabled_copy_and_marks_all_disabled_by_path_or_name(self):
        home = Path(self.home.name)
        first = self.make_skill('ui-combo')
        second = self.make_skill('ui-combo', root='.codex/skills')
        config = home / '.codex/config.toml'
        self.write_routing([self.route_definition(skills=['ui-combo'])])
        roles = {'cx-build': {'model': 'worker', 'skills': ['ui-combo']}}
        configs = [
            ('[[skills.config]]\npath = ' + json.dumps(str(first / 'SKILL.md')) + '\nenabled = false\n',
             str(second / 'SKILL.md'), True, False),
            ('[[skills.config]]\npath = ' + json.dumps(str(first / 'SKILL.md')) + '\nenabled = false\n'
             '[[skills.config]]\npath = ' + json.dumps(str(second)) + '\nenabled = false\n',
             '비활성', False, True),
            ("[[skills.config]]\nname = 'ui-combo'\nenabled = false\n", '비활성', False, True),
        ]
        with patch.object(cx, 'load_roles', return_value=roles), patch.object(cx.Path, 'home', return_value=home):
            for text, expected_path, allowed, disabled in configs:
                config.write_text(text)
                with self.subTest(config=text), contextlib.redirect_stdout(io.StringIO()) as output:
                    cx.main(['route', 'UI', '--json'])
                skill = json.loads(output.getvalue())[0]['skills'][0]
                self.assertEqual(skill['path'], expected_path)
                self.assertEqual(skill['allowed'], allowed)
                self.assertEqual(skill.get('disabled', False), disabled)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cx.main(['route', 'UI'])
            self.assertIn('비활성', output.getvalue())
            self.assertIn('allowed=false', output.getvalue())

    def test_route_mixed_keywords_ignore_case_but_respect_ascii_edge_boundaries(self):
        home = Path(self.home.name)
        self.write_routing([self.route_definition('x', keywords=['x 동향', 'x 실시간']),
                            self.route_definition('grok', keywords=['grok 검색']),
                            self.route_definition('end', keywords=['검색 grok'])])
        cases = [('UX 동향', []), ('linux 실시간', []), ('UX 실시간', []),
                 ('X 실시간 동향', ['x']), ('Grok 검색해', ['grok']),
                 ('검색 GROK', ['end']), ('검색 GROKing', []), ('prefix검색 grok', ['end'])]
        with patch.object(cx.Path, 'home', return_value=home):
            for request, expected in cases:
                with self.subTest(request=request), contextlib.redirect_stdout(io.StringIO()) as output:
                    cx.main(['route', request, '--json'])
                self.assertEqual([row['id'] for row in json.loads(output.getvalue())], expected)

    def test_repeated_finish_keeps_append_only_receipts_and_retro_uses_latest_per_project(self):
        project = str(Path(self.home.name) / 'project')
        started = cx.checkpoint_start(project, '작업', 'a', task_type='C', mode='균형', mode_source='default')
        with contextlib.redirect_stdout(io.StringIO()):
            cx.main(['checkpoint', 'finish', '--project', project, '--id', started['id'],
                     '--skills', 'before,a', '--skill-fit', 'good'])
            cx.main(['checkpoint', 'finish', '--project', project, '--id', started['id'],
                     '--skills', 'a', '--skill-fit', 'poor'])
        path = cx.state() / 'skill-receipts.jsonl'
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual([record['fit'] for record in records], ['good', 'poor'])
        with patch.object(cx, 'run', return_value=None):
            report = cx.retro_report(7, Path(self.home.name) / 'none')
        self.assertIn('| a | 1 | 0 | 1 | 0 | 0 |', report)
        self.assertNotIn('| before |', report)
        # Identical checkpoint IDs in separate projects are separate uses.
        cx.append_receipt({**records[-1], 'project': project + '-other', 'fit': 'good'}, 'skill-receipts.jsonl')
        with patch.object(cx, 'run', return_value=None):
            report = cx.retro_report(7, Path(self.home.name) / 'none')
        self.assertIn('| a | 2 | 1 | 1 | 0 | 0 |', report)

    def test_audit_fit_counts_latest_receipts_and_only_good_protects_from_review(self):
        home = Path(self.home.name)
        names = ('good', 'poor', 'none', 'unrecorded', 'expired', 'replaced', 'empty')
        for name in names:
            self.make_skill(name)
        (home / '.agents/.skill-lock.json').write_text(json.dumps({
            'skills': {name: {'source': 'vendor/skills'} for name in names}}))
        stamp = cx.now().isoformat()
        records = [
            {'project': '/fixture', 'checkpoint': name, 'ts': stamp, 'skills': [name], 'fit': fit}
            for name, fit in (('good', 'good'), ('poor', 'poor'), ('none', 'none'), ('unrecorded', None))
        ]
        records += [
            {'project': '/fixture', 'checkpoint': 'edit', 'ts': stamp, 'skills': ['good', 'replaced'], 'fit': 'good'},
            {'project': '/fixture', 'checkpoint': 'edit', 'ts': stamp, 'skills': ['poor'], 'fit': 'poor'},
            {'project': '/fixture', 'checkpoint': 'expired', 'ts': '2020-01-01T00:00:00Z',
             'skills': ['expired'], 'fit': 'good'},
        ]
        for record in records:
            cx.append_receipt(record, 'skill-receipts.jsonl')
        audit = cx.skills_audit(days=7, home=home)
        rows = {row['name']: row for row in audit['skills']}
        zero = {'good': 0, 'poor': 0, 'none': 0, 'unrecorded': 0}
        expected = {'good': {**zero, 'good': 1}, 'poor': {**zero, 'poor': 2},
                    'none': {**zero, 'none': 1}, 'unrecorded': {**zero, 'unrecorded': 1},
                    'expired': zero, 'replaced': zero, 'empty': zero}
        self.assertEqual({name: row['fit'] for name, row in rows.items()}, expected)
        self.assertEqual(rows['good']['verdict'], 'keep')
        self.assertTrue(all(row['verdict'] == 'review' for name, row in rows.items() if name != 'good'))
        self.assertTrue(all(row['user_calls'] == row['agent_reads'] == row['mcp_calls'] == 0 for row in rows.values()))
        with patch.object(cx.Path, 'home', return_value=home), contextlib.redirect_stdout(io.StringIO()) as output:
            cx.main(['skills-audit', '--days', '7'])
        self.assertIn('성과(g/p/n/-)', output.getvalue().splitlines()[0])
        self.assertIn('1/0/0/0', output.getvalue())
        self.assertIn('0/2/0/0', output.getvalue())

    def test_normalize_mode_names_aliases_and_unknown(self):
        for value, expected in [('속도', ('속도', False)), ('균형', ('균형', False)),
                                ('프론티어', ('프론티어', False)), ('빠르게', ('속도', False)),
                                ('꼼꼼하게', ('프론티어', False)), ('아껴서', ('균형', True))]:
            with self.subTest(value=value):
                self.assertEqual(cx.normalize_mode(value), expected)
        for value in ('unknown', '', None):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                cx.normalize_mode(value)

    def test_quota_burn_uses_reset_elapsed_week_and_minimum_one_hour(self):
        at = cx.datetime.datetime(2026, 11, 13, tzinfo=cx.datetime.timezone.utc)
        text = ('openai weekly 10% resets 2026-11-16T12:00:00Z\n'
                'anthropic weekly 20% resets 2026-11-16T12:00:00+00:00 '
                'Fable 30% resets 2026-11-16T12:00:00Z\n')
        self.assertEqual(cx.quota_burn(text, at), {'openai': {'weekly': 0.2},
                         'anthropic': {'weekly': 0.4, 'Fable': 0.6}})
        with patch.object(cx, 'now', return_value=at):
            self.assertEqual(cx.quota_burn(text), cx.quota_burn(text, at))
        for reset in ('2026-11-20T00:00:00Z', '2026-11-21T00:00:00Z', '2026-11-19T23:30:00Z'):
            self.assertAlmostEqual(cx.quota_burn('openai weekly 1% resets ' + reset, at)['openai']['weekly'], 1.68)
        self.assertEqual(cx.quota_burn('openai weekly 33% resets 2026-11-14T00:00:00Z', at),
                         {'openai': {'weekly': 0.33 / (6 / 7)}})
        self.assertEqual(cx.quota_burn(None, at), {})
        self.assertEqual(cx.quota_burn('openai weekly 30% resets invalid', at), {})

    def test_quota_burn_keeps_raw_value_below_guardrail(self):
        at = cx.datetime.datetime(2026, 11, 13, tzinfo=cx.datetime.timezone.utc)
        text = ('openai weekly 29% resets 2026-11-17T02:00:00Z\n'
                'anthropic weekly 1% resets 2026-11-13T00:00:00Z '
                'Fable 1% resets 2026-11-13T00:00:00Z')
        quota, burn = cx.parse_quota(text), cx.quota_burn(text, at)
        self.assertAlmostEqual(burn['openai']['weekly'], 0.696)
        self.assertEqual(cx.quota_advice(quota, burn), [])
        self.assertEqual(cx.expansion_state(quota, burn), {'openai': 'on', 'anthropic': 'on', 'fable': 'on'})
        with patch.object(cx, 'now', return_value=at), patch.object(cx, 'run', return_value=text), \
                patch.object(cx, 'live_models', return_value=None), contextlib.redirect_stdout(io.StringIO()) as output:
            cx.main(['status', '--project', self.home.name])
        self.assertIn('burn: openai weekly 0.70;', output.getvalue())
        self.assertIn('expansion: openai on; anthropic on; fable on', output.getvalue())
        self.assertNotIn('advice:', output.getvalue())

    def test_status_places_burn_and_expansion_after_quota_and_normalizes_profile(self):
        at = cx.datetime.datetime(2026, 11, 13, tzinfo=cx.datetime.timezone.utc)
        text = ('openai weekly 19% resets 2026-11-13T00:00:00Z\n'
                'anthropic weekly 38% resets 2026-11-13T00:00:00Z Fable 56% resets 2026-11-13T00:00:00Z')
        cx.ensure_profile(self.home.name)
        path = cx.profile_path(self.home.name)
        profile = json.loads(path.read_text())
        profile['mode'] = '아껴서'
        path.write_text(json.dumps(profile))
        with patch.object(cx, 'now', return_value=at), patch.object(cx, 'run', return_value=text), \
                patch.object(cx, 'live_models', return_value=None), contextlib.redirect_stdout(io.StringIO()) as output:
            cx.main(['status', '--project', self.home.name])
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[1:3], ['burn: openai weekly 0.19; anthropic weekly 0.38 Fable 0.56', 'expansion: openai on; anthropic on; fable on'])
        self.assertIn('profile: mode=균형', output.getvalue())
        self.assertTrue(next(line for line in lines if line.startswith('profile:')).endswith(' saving=true'))
        with patch.object(cx, 'run', return_value=None), patch.object(cx, 'live_models', return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            cx.main(['status', '--project', self.home.name])
        self.assertEqual(output.getvalue().splitlines()[1:3], ['burn: unavailable', 'expansion: openai off; anthropic off; fable off'])
        self.assertTrue(output.getvalue().splitlines()[3].startswith('advice:'))

    def test_checkpoint_start_requires_mode_and_source_before_writing(self):
        for options in ([], ['--mode', '균형'], ['--mode-source', 'default']):
            with self.subTest(options=options), self.assertRaisesRegex(SystemExit, r'^checkpoint start requires --mode and --mode-source \(예: --mode 균형 --mode-source default\)$'):
                cx.main(['checkpoint', 'start', '--project', self.home.name, '--steps', 'a'] + options)
        with self.assertRaisesRegex(SystemExit, 'checkpoint start requires --mode and --mode-source'):
            cx.checkpoint_start(self.home.name, 'a', 'a')
        self.assertFalse((cx.state() / 'runs').exists())

    def test_checkpoint_start_records_normalized_mode_source_saving_and_visual(self):
        for index, (value, mode, saving) in enumerate([('빠르게', '속도', False), ('아껴서', '균형', True), ('프론티어', '프론티어', False)]):
            project = str(Path(self.home.name) / str(index))
            with contextlib.redirect_stdout(io.StringIO()):
                cx.main(['checkpoint', 'start', '--project', project, '--steps', 'a', '--mode', value,
                         '--mode-source', 'user', '--visual'])
            _, row = cx.load_checkpoint(project)
            self.assertEqual((row['mode'], row['mode_source'], row['saving']), (mode, 'user', saving))
            self.assertEqual(row['visual'], {'iterations': 0, 'defects': None, 'accepted': None})
        plain = cx.checkpoint_start(self.home.name, 'a', 'a', mode='균형', mode_source='default')
        self.assertNotIn('visual', plain)

    def test_checkpoint_update_finish_new_fields_overwrite_without_fable_cap(self):
        started = cx.checkpoint_start(self.home.name, 'a', 'a', mode='프론티어', mode_source='inferred')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cx.main(['checkpoint', 'update', '--project', self.home.name, '--fable-calls', '4',
                     '--review-changed', 'no', '--visual-iterations', '2', '--visual-defects', '1', '--visual-accepted', 'unknown'])
        self.assertNotIn('warning:', output.getvalue())
        _, row = cx.load_checkpoint(self.home.name)
        self.assertEqual((row['judge_calls'], row['review_changed']), (4, False))
        self.assertEqual(row['visual'], {'iterations': 2, 'defects': 1, 'accepted': None})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cx.main(['checkpoint', 'finish', '--project', self.home.name, '--fable-calls', '5',
                     '--review-changed', 'yes', '--visual-iterations', '3', '--visual-defects', '0', '--visual-accepted', 'yes'])
        # 2026-11-16: the per-task Fable cap was removed, so five calls no longer warn.
        self.assertNotIn('warning:', output.getvalue())
        _, row = cx.load_checkpoint(self.home.name, started['id'])
        self.assertEqual((row['judge_calls'], row['review_changed'], row['outcome']), (5, True, 'pass'))
        self.assertEqual(row['visual'], {'iterations': 3, 'defects': 0, 'accepted': True})
        cx.checkpoint_update(self.home.name, started['id'], None, fable_calls=1, visual_accepted=False)
        _, row = cx.load_checkpoint(self.home.name, started['id'])
        self.assertEqual(row['judge_calls'], 1)
        self.assertEqual(row['visual'], {'iterations': 3, 'defects': 0, 'accepted': False})

    def test_legacy_checkpoint_without_mode_accepts_update_and_finish_with_new_options(self):
        for action in ('update', 'finish'):
            for task_type in (None, 'C'):
                with self.subTest(action=action, task_type=task_type):
                    project = str(Path(self.home.name) / ('%s-%s' % (action, task_type)))
                    stamp = cx.now().isoformat(timespec='seconds')
                    legacy = {'id': 'legacy', 'project': project, 'title': '옛 체크포인트',
                              'steps': ['a', 'b'], 'done': 0, 'status': 'open', 'outcome': None,
                              'started': stamp, 'updated': stamp, 'notes': []}
                    if task_type is not None:
                        legacy['type'] = task_type
                    directory = cx.runs_dir(project)
                    directory.mkdir(parents=True)
                    (directory / 'legacy.json').write_text(json.dumps(legacy))
                    options = ['--done', '1'] if action == 'update' else []
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        cx.main(['checkpoint', action, '--project', project, '--id', 'legacy',
                                 '--note', '추가 기록', '--delegated', '2', '--skills', 'allowed', '--skill-fit', 'good',
                                 '--fable-calls', '3', '--review-changed', 'yes', '--visual-iterations', '2',
                                 '--visual-defects', '0', '--visual-accepted', 'yes'] + options)
                    _, row = cx.load_checkpoint(project, 'legacy')
                    self.assertEqual(row['id'], legacy['id'])
                    self.assertEqual(row['started'], legacy['started'])
                    self.assertEqual(row['title'], legacy['title'])
                    self.assertNotIn('mode', row)
                    self.assertEqual(row.get('type'), task_type)
                    self.assertEqual((row['delegated'], row['skills'], row['skill_fit']), (2, ['allowed'], 'good'))
                    self.assertEqual((row['judge_calls'], row['review_changed']), (3, True))
                    self.assertEqual(row['visual'], {'iterations': 2, 'defects': 0, 'accepted': True})
                    self.assertEqual(row['notes'], ['추가 기록'])
                    self.assertEqual((row['status'], row['outcome'], row['done']),
                                     ('open', None, 1) if action == 'update' else ('done', 'pass', 2))
                    self.assertIn('진행 50%' if action == 'update' else '진행 100%', output.getvalue())

    def test_checkpoint_rejects_negative_metrics_without_persisting_updates(self):
        cx.checkpoint_start(self.home.name, 'a', 'a', mode='균형', mode_source='default')
        path, _ = cx.load_checkpoint(self.home.name)
        before = path.read_text()
        for option in ('--fable-calls', '--visual-iterations', '--visual-defects'):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                cx.main(['checkpoint', 'update', '--project', self.home.name, '--done', '1', option, '-1'])
            self.assertEqual(path.read_text(), before)

    def test_profile_set_accepts_aliases_and_read_normalizes_without_rewriting(self):
        for value, mode, saving in [('속도', '속도', False), ('균형', '균형', False),
                                    ('프론티어', '프론티어', False), ('빠르게', '속도', False),
                                    ('꼼꼼하게', '프론티어', False), ('아껴서', '균형', True)]:
            with contextlib.redirect_stdout(io.StringIO()):
                cx.main(['profile', 'set', '--project', self.home.name, '--mode', value])
            path = cx.profile_path(self.home.name)
            saved = json.loads(path.read_text())
            self.assertEqual((saved['mode'], saved['saving']), (mode, saving))
            saved['mode'] = value
            path.write_text(json.dumps(saved))
            before = path.read_text()
            profile = cx.read_profile(self.home.name)
            self.assertEqual((profile['mode'], profile['saving']), (mode, saving))
            self.assertEqual(path.read_text(), before)

    def test_receipt_records_error_separately_from_content_refusal(self):
        for outcome in ('error', 'refused'):
            with contextlib.redirect_stdout(io.StringIO()):
                cx.main(['receipt', '--type', 'E', '--model', 'anthropic/claude-fable-5-2',
                         '--effort', 'high', '--outcome', outcome])
        rows = cx.read_receipts(7)
        self.assertEqual([row['outcome'] for row in rows], ['error', 'refused'])
        self.assertTrue(all(row.get('classified') is True for row in rows))

    def mode_report(self, rows, days=30):
        directory = cx.state() / 'runs' / 'mode-fixture'
        directory.mkdir(parents=True, exist_ok=True)
        for index, row in enumerate(rows):
            row = {'status': 'done', 'outcome': 'pass', 'updated': cx.now().isoformat(), **row}
            (directory / ('%d.json' % index)).write_text(json.dumps(row))
        with patch.object(cx, 'run', return_value=None):
            return cx.retro_report(days, Path(self.home.name) / 'none')

    def test_retro_modes_normalize_legacy_and_count_unrecorded_with_visual_summary(self):
        report = self.mode_report([
            {'mode': '빠르게', 'mode_source': 'user', 'visual': {'iterations': 1, 'defects': 2, 'accepted': False}},
            {'mode': '아껴서', 'mode_source': 'default', 'visual': {'iterations': 3, 'defects': None, 'accepted': None}},
            {'mode': '균형', 'mode_source': 'default', 'visual': {'iterations': 2, 'defects': 1, 'accepted': True}},
            {}, {'mode': '프론티어', 'updated': '2020-01-01T00:00:00Z'}])
        self.assertIn('## 모드', report)
        self.assertIn('| 속도 | user | 1 |', report)
        self.assertIn('| 균형 | default | 2 |', report)
        self.assertIn('| 미기록 | 미기록 | 1 |', report)
        self.assertIn('0건 모드: 프론티어', report)
        self.assertIn('비주얼 트랙 3건, iterations 평균 2.00, defects 합 3', report)
        self.assertIn('accepted 분포: yes 1, no 1, unknown 1', report)

    def test_retro_review_change_and_frontier_ratio_trigger_thresholds(self):
        rows = [{'mode': '꼼꼼하게', 'mode_source': 'inferred', 'review_changed': False,
                 'fable_calls': 4} for _ in range(8)]
        rows += [{'mode': '프론티어', 'mode_source': 'user', 'fable_calls': 5}, {'mode': '균형', 'fable_calls': 1}]
        report = self.mode_report(rows)
        self.assertIn('프론티어 비율 90%: 추론 조건 축소 검토', report)
        self.assertIn('## 두 번째 검토의 효과', report)
        self.assertIn('review_changed 기록 8건, true 0건, 비율 0%', report)
        self.assertIn('제안: 프론티어 2회 검토를 1회로 줄이는 안을 사용자에게 제안', report)
        self.assertIn('## 판단 역할 호출', report)
        self.assertIn('judge_calls 기록 10건, 합계 38회, 최대 5회', report)

    def test_retro_mode_and_review_thresholds_do_not_trigger_at_boundaries(self):
        report = self.mode_report([{'mode': '프론티어', 'review_changed': index == 0} for index in range(10)] +
                                 [{'mode': '균형'} for _ in range(10)], days=7)
        self.assertNotIn('0건 모드:', report)
        self.assertNotIn('추론 조건 축소 검토', report)
        self.assertIn('review_changed 기록 10건, true 1건, 비율 10%', report)
        self.assertNotIn('2회 검토를 1회로', report)

    def test_retro_failures_count_classified_refusals_and_checkpoint_calls(self):
        for outcome in ('pass', 'fail', 'refused', 'refused', 'error'):
            cx.append_receipt({'ts': cx.now().isoformat(), 'type': 'E', 'model': 'anthropic/claude-fable-5-2',
                               'effort': 'high', 'outcome': outcome, 'classified': True})
        for _ in range(5):
            cx.append_receipt({'ts': cx.now().isoformat(), 'model': 'anthropic/claude-fable-5-2',
                               'outcome': 'refused'})
        report = self.mode_report([{'fable_calls': 4}, {'fable_calls': 6}, {},
                                  {'fable_calls': 100, 'updated': '2020-01-01T00:00:00Z'}])
        self.assertIn('## 실패 원인', report)
        self.assertIn('| 모델 | fail | refused | error | 미분류 refused |', report)
        self.assertIn('| anthropic/claude-fable-5-2 | 1 | 2 | 1 | 5 |', report)
        self.assertIn('| E | anthropic/claude-fable-5-2 | high | single | 5 | 1 | 4 |', report)
        self.assertIn('판단 역할 내용 거절 2건(분류된 refused), 판단 역할 호출 10회(체크포인트 judge_calls 합)', report)
        self.assertNotIn('판단 역할 내용 거절 점검:', report)
        self.assertNotIn('판단 역할 내용 거절률', report)

    def test_retro_legacy_refused_receipts_do_not_count_as_content_refusals(self):
        for _ in range(5):
            cx.append_receipt({'ts': cx.now().isoformat(), 'model': 'anthropic/claude-fable-5-2',
                               'outcome': 'refused'})
        cx.append_receipt({'ts': cx.now().isoformat(), 'model': 'anthropic/claude-fable-5-2',
                           'outcome': 'refused', 'classified': False})
        report = self.mode_report([])
        self.assertIn('| anthropic/claude-fable-5-2 | 0 | 0 | 0 | 6 |', report)
        self.assertIn('판단 역할 내용 거절 0건(분류된 refused), 판단 역할 호출 0회(체크포인트 judge_calls 합)', report)
        summary = next(line for line in report.splitlines() if line.startswith('판단 역할 내용 거절 '))
        self.assertNotIn('비율', summary)
        self.assertNotIn('판단 역할 내용 거절 점검:', report)

    def test_retro_refusal_summary_uses_call_threshold_and_count_trigger(self):
        cases = [(2, 9, False), (3, 0, True), (3, 9, True), (3, 10, True), (3, 15, True), (1, 10, False)]
        for index, (refused, calls, warning) in enumerate(cases):
            with self.subTest(refused=refused, calls=calls), \
                    patch.dict(os.environ, {'CONDUCTOR_HOME': str(Path(self.home.name) / str(index))}):
                local = cx.state() / 'local'
                local.mkdir(parents=True)
                (local / 'settings.json').write_text(json.dumps({'onboarding': {'judge_model': 'anthropic/claude-fable-5-2'}}))
                for _ in range(refused):
                    cx.append_receipt({'ts': cx.now().isoformat(), 'model': 'anthropic/claude-fable-5-2',
                                       'outcome': 'refused', 'classified': True})
                # Other models and successful receipts are not content refusals.
                cx.append_receipt({'ts': cx.now().isoformat(), 'model': 'other',
                                   'outcome': 'refused', 'classified': True})
                cx.append_receipt({'ts': cx.now().isoformat(), 'model': 'anthropic/claude-fable-5-2',
                                   'outcome': 'pass', 'classified': True})
                report = self.mode_report([{'fable_calls': calls}])
                summary = next(line for line in report.splitlines() if line.startswith('판단 역할 내용 거절 '))
                expected = '판단 역할 내용 거절 %d건(분류된 refused), 판단 역할 호출 %d회(체크포인트 judge_calls 합)' % (refused, calls)
                self.assertEqual(summary, expected)
                self.assertEqual('판단 역할 내용 거절 점검: 거절된 지시문을 SKILL.md 판단 역할 운용 규칙과 비교' in report, warning)

    def test_retro_opencodex_errors_group_first_non_null_code_and_local_time(self):
        current = time.time()
        rows = [
            {'timestamp': current - 120, 'model': 'fable', 'status': 503, 'attempts': [{'errorCode': None}, {'errorCode': 'overloaded'}, {'errorCode': 'later'}]},
            {'timestamp': current - 60, 'model': 'fable', 'status': '503', 'attempts': [{'errorCode': 'overloaded'}]},
            {'timestamp': current, 'model': 'fable', 'status': 401, 'attempts': []},
            {'timestamp': current, 'model': 'fable', 'status': 200},
            {'timestamp': current - 40 * 86400, 'model': 'old', 'status': 500},
        ]
        with patch.object(cx, 'run', side_effect=lambda args, timeout=20: json.dumps({'logs': rows}) if args[1] == 'logs' else None) as runner:
            report = cx.retro_report(7, Path(self.home.name) / 'none')
        first = cx.datetime.datetime.fromtimestamp(current - 120).astimezone().strftime('%m-%d %H:%M')
        last = cx.datetime.datetime.fromtimestamp(current - 60).astimezone().strftime('%m-%d %H:%M')
        self.assertIn('## OpenCodex 오류 원인', report)
        self.assertIn('| fable | 503 | overloaded | 2 | %s | %s |' % (first, last), report)
        self.assertIn('| fable | 401 | 미기록 | 1 |', report)
        self.assertNotIn('| old |', report)
        self.assertNotIn('| fable | 200 |', report)
        self.assertEqual(sum(call.args[0][1] == 'logs' for call in runner.call_args_list), 1)
        with patch.object(cx, 'run', return_value=None):
            report = cx.retro_report(7, Path(self.home.name) / 'none')
        self.assertIn('## OpenCodex 오류 원인\n미확인', report)

    def test_audit_excludes_only_conductor_component_names(self):
        home = Path(self.home.name)
        self.make_skill('kp-conductor')
        self.make_skill('kp-conductor', root='.codex/plugins/cache/vendor/plugin/1.0/skills')
        self.make_skill('my-conductor')
        rows = cx.skills_audit(home=home)['skills']
        self.assertEqual([row['name'] for row in rows], ['my-conductor'])




class PublicContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        self.skill = Path(self.temp.name) / 'package'
        self.refs = self.skill / 'references'
        self.refs.mkdir(parents=True)
        for name in ('roles.json', 'routing.json', 'tiers.json'):
            (self.refs / name).write_bytes((SCRIPT.parent.parent / 'references' / name).read_bytes())
        (self.refs / 'entry-block.md').write_text('Read the installed conductor skill.\n')
        self.patches = [patch.object(cx, 'SKILL', self.skill),
                        patch.dict(os.environ, {'CONDUCTOR_HOME': str(self.home / '.codex/kp-conductor')})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cx.main(list(args) + ['--home', str(self.home)])
        return out.getvalue()

    def local(self, name, value):
        target = cx.state() / 'local' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))
        return target

    def plan(self, models):
        detected = {'models': models, 'skills': [{'name': 'korean-protocol'}]}
        with patch.object(cx, 'onboard_detect', return_value=detected):
            return json.loads(self.cli('onboard', 'propose', '--routing', str(self.refs / 'routing.json')))

    def test_overlay_precedes_package_and_tiers_merge_entries(self):
        role = {'model': 'local-model', 'effort': 'low', 'job': 'test', 'skills': [], 'sandbox': None}
        self.local('roles.json', {'roles': {'local': role}})
        self.local('routing.json', {'mcp_usage': {}, 'routes': []})
        self.local('tiers.json', {'models': {'new': {'tier': 'high'}}, 'seats': {'local': [['new', 'low']]}})
        self.assertEqual(cx.load_roles(), {'local': role})
        self.assertEqual(cx.load_routing()['routes'], [])
        merged = cx.load_tiers()
        self.assertIn('gpt-6-astra', merged['models'])
        self.assertIn('new', merged['models'])
        self.assertIn('cx-build', merged['seats'])
        self.assertEqual(merged['seats']['local'], [['new', 'low']])

    def test_generic_quota_float_parsing_parents_and_thresholds(self):
        settings = {'quota': [
            {'key': 'common', 'provider': 'vendor', 'window': 'weekly', 'label': '공통', 'models': ['vendor/*']},
            {'key': 'special', 'provider': 'vendor', 'window': 'special', 'models': ['vendor/judge'], 'parent': 'common'}],
            'orchestrator': 'vendor/main'}
        self.local('settings.json', settings)
        self.local('roles.json', {'roles': {'cx-build': {'model': 'vendor/build', 'effort': 'high', 'job': '', 'skills': []}}})
        text = 'vendor weekly 8.7% resets 2026-11-18T00:00:00Z special 9.1% resets 2026-11-18T00:00:00Z'
        quota = cx.parse_quota(text)
        self.assertEqual(quota['vendor']['weekly'], 8.7)
        self.assertIn('weekly', cx.quota_burn(text)['vendor'])
        burn = {'vendor': {'weekly': 0.1, 'special': 0.1}}
        self.assertEqual(cx.expansion_state(quota, burn), {'common': 'on', 'special': 'on'})
        for percent, phrase in [(70, '작업자는 1개'), (80, '유형 E'), (85, '메인 세션'), (90, '직접 처리'), (95, '배정하지')]:
            quota['vendor']['weekly'] = percent
            self.assertIn(phrase, ' '.join(cx.quota_advice(quota, burn)))
            self.assertEqual(cx.expansion_state(quota, burn), {'common': 'off', 'special': 'off'})
        quota['vendor']['weekly'] = 1
        for rate, phrase in [(0.7, '확대 자리를'), (1.01, '균형 배정')]:
            burn['vendor']['weekly'] = rate
            self.assertIn(phrase, ' '.join(cx.quota_advice(quota, burn)))
        self.assertIn('미확인', ' '.join(cx.quota_advice(quota, {})))

    def test_quota_defaults_discover_all_provider_weekly_windows(self):
        q = {'other': {'weekly': 1}, 'openai': {'weekly': 2}}
        b = {'other': {'weekly': .1}, 'openai': {'weekly': .2}}
        self.assertEqual(cx.expansion_state(q, b), {'other': 'on', 'openai': 'on'})
        self.assertEqual(cx.model_quota_keys('other/model', q), ['other'])
        self.assertEqual(cx.model_quota_keys('plain-model', q), ['openai'])

    def test_quota_defaults_add_model_specific_windows_as_child_keys(self):
        text = ('anthropic weekly 20% resets 2026-11-22T17:00:00Z Fable 23% resets 2026-11-22T17:00:00Z\n'
                'google-antigravity Gem 8.7% resets 2026-11-17T05:31:11Z Gem (Weekly) 15.6% resets 2026-11-23T01:46:08Z\n')
        q = cx.parse_quota(text)
        keys = {row['key']: row for row in cx.quota_definitions(q)}
        self.assertEqual(keys['fable']['parent'], 'anthropic')
        self.assertNotIn('parent', keys['gem'])
        self.assertEqual(cx.model_quota_keys('anthropic/claude-fable-5-2', q), ['anthropic', 'fable'])
        self.assertEqual(cx.model_quota_keys('anthropic/claude-opus-5-5', q), ['anthropic'])
        self.assertEqual(cx.model_quota_keys('google-antigravity/gemini-4-carbon', q), ['gem'])
        burn = {'anthropic': {'weekly': .1, 'Fable': .1}, 'google-antigravity': {'Gem': .1}}
        self.assertEqual(cx.expansion_state(q, burn)['fable'], 'on')
        burn['anthropic']['weekly'] = .9
        self.assertEqual(cx.expansion_state(q, burn)['fable'], 'off')

    def test_propose_leaves_actual_judges_blank_and_prunes_uninstalled_skills(self):
        routing = {'mcp_usage': {}, 'routes': [{'id': 'r', 'label': 'r', 'keywords': ['k'], 'skills': ['present', 'absent'],
                                                  'role': 'cx-build', 'risk': 'R1', 'note': ''}]}
        source = self.home / 'draft.json'
        source.write_text(json.dumps(routing))
        detected = {'models': ['gpt-6.1-sol', 'gpt-6-astra'], 'skills': [{'name': 'present'}]}
        with patch.object(cx, 'onboard_detect', return_value=detected):
            plan = json.loads(self.cli('onboard', 'propose', '--routing', str(source)))
        onboarding = plan['settings']['onboarding']
        self.assertEqual((onboarding['method'], onboarding['judge_model'], onboarding['reviewer_model']), ('', '', ''))
        self.assertEqual(plan['routing']['routes'][0]['skills'], ['present'])
        self.assertNotIn('absent', plan['roles']['roles']['cx-build']['skills'])
        self.assertIn('미설치 스킬 1개', ' '.join(plan['warnings']))

    def test_author_assignment_is_fixed_independently_of_roles_defaults(self):
        expected = json.loads((SCRIPT.parents[3] / 'tests/fixtures/author_assignment.json').read_text())
        models = sorted({value[0] for key, value in expected.items() if key != 'orchestrator'})
        plan = self.plan(models)
        actual = {name: [role['model'], role['effort']] for name, role in plan['roles']['roles'].items()}
        actual['orchestrator'] = plan['settings']['orchestrator']
        self.assertEqual(actual, expected)
        self.assertTrue(all('korean-protocol' in role['skills'] for role in plan['roles']['roles'].values()))

    def test_propose_keeps_declared_service_tier(self):
        expected = json.loads((SCRIPT.parents[3] / 'tests/fixtures/author_assignment.json').read_text())
        models = sorted({value[0] for key, value in expected.items() if key != 'orchestrator'})
        pinned = {'model': expected['cx-review'][0], 'effort': 'high', 'job': 'review', 'skills': [], 'service_tier': 'default'}
        self.local('roles.json', {'roles': {'cx-review': pinned}})
        roles = self.plan(models)['roles']['roles']
        self.assertEqual(roles['cx-review']['service_tier'], 'default')
        self.assertNotIn('service_tier', roles['cx-build'])

    def test_single_provider_assignment_never_violates_constraints(self):
        tiers = cx.load_tiers()
        for vendor in ('openai', 'anthropic'):
            models = [name for name, row in tiers['models'].items() if row['vendor'] == vendor]
            plan = self.plan(models)
            roles = plan['roles']['roles']
            for rule in tiers['constraints']:
                if rule['role'] in roles:
                    for other in rule['differs_from']:
                        if other in roles:
                            self.assertNotEqual(roles[rule['role']]['model'], roles[other]['model'])
            self.assertIn('임시', ' '.join(plan['warnings']))
            self.assertTrue(all(role['model'] in models for role in roles.values()))

    def test_unknown_models_are_never_assigned(self):
        plan = self.plan(['unranked/model'])
        self.assertEqual(plan['roles']['roles'], {})
        self.assertIsNone(plan['settings']['orchestrator'])
        self.assertIn('unranked/model', plan['notes'])

    def test_entry_is_idempotent_preserves_outside_and_backs_up(self):
        agents = self.home / '.codex/AGENTS.md'
        agents.parent.mkdir()
        agents.write_text('before\n')
        self.cli('entry', 'install')
        installed = agents.read_text()
        agents.write_text(installed + 'after\n')
        self.cli('entry', 'install')
        self.assertEqual(agents.read_text(), installed + 'after\n')
        self.assertIn('present', self.cli('entry', 'status'))
        (self.refs / 'entry-block.md').write_text('Changed entry.\n')
        self.assertIn('outdated', self.cli('entry', 'status'))
        self.cli('entry', 'remove')
        self.assertEqual(agents.read_text(), 'before\nafter\n')
        self.assertTrue(list((cx.state() / 'local/backups').glob('*/AGENTS.md')))

    def test_entry_install_missing_source_does_not_change_home(self):
        (self.refs / 'entry-block.md').unlink()
        before = list(self.home.rglob('*'))
        with self.assertRaises(SystemExit):
            self.cli('entry', 'install')
        self.assertEqual(list(self.home.rglob('*')), before)

    def test_overlay_restore_removes_new_settings_and_verify_rolls_back(self):
        target = self.local('settings.json', {'language': 'en'})
        self.cli('overlay', 'backup')
        target.write_text('{')
        new_file = self.local('notes.md', 'new note')
        with patch.object(cx, 'command_doctor', side_effect=SystemExit(1)), self.assertRaises(SystemExit) as result:
            self.cli('overlay', 'verify', '--id', cx.overlay_backups()[0].name)
        self.assertEqual(result.exception.code, 1)
        self.assertEqual(json.loads(target.read_text()), {'language': 'en'})
        self.assertFalse(new_file.exists())
        self.assertIn('settings.json', self.cli('overlay', 'list'))

    def test_settings_allow_only_documented_keys(self):
        self.cli('settings', 'set', 'retro.apply', 'notify')
        self.assertEqual(json.loads(self.cli('settings', 'show'))['retro']['apply'], 'notify')
        self.cli('settings', 'set', 'retro.apply', 'off')
        self.assertEqual(json.loads(self.cli('settings', 'show'))['retro']['apply'], 'off')
        for key, value in [('quota', '[]'), ('entry', 'invalid'), ('retro.apply', 'unsafe')]:
            with self.assertRaises(SystemExit):
                self.cli('settings', 'set', key, value)

    def test_apply_dry_run_writes_nothing_then_apply_syncs_and_uninstall_preserves_users(self):
        plan = self.plan(['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6.1-terra'])
        target = Path(self.temp.name) / 'plan.json'
        target.write_text(json.dumps(plan))
        before = sorted(str(p) for p in self.home.rglob('*'))
        self.cli('onboard', 'apply', '--plan', str(target), '--dry-run')
        self.assertEqual(sorted(str(p) for p in self.home.rglob('*')), before)
        self.cli('onboard', 'apply', '--plan', str(target))
        self.assertTrue((cx.state() / 'local/routing.json').exists())
        self.assertIn('present', self.cli('entry', 'status'))
        agents = self.home / '.codex/agents'
        user = agents / 'custom.toml'
        user.write_text('# user owned\n')
        config = self.home / '.codex/config.toml'
        config.write_text(config.read_text() + '[agents.custom]\nconfig_file = "./agents/custom.toml"\n')
        snapshot = config.read_text()
        self.cli('uninstall', '--dry-run')
        self.assertEqual(config.read_text(), snapshot)
        self.cli('uninstall')
        self.assertEqual([p.name for p in agents.iterdir()], ['custom.toml'])
        self.assertIn('[agents.custom]', config.read_text())
        self.assertNotIn('[agents.cx-', config.read_text())
        self.assertTrue(cx.state().exists())
        self.assertTrue(user.exists())
        self.assertIn('absent', self.cli('entry', 'status'))

    def test_detect_is_read_only_handles_missing_tools_and_reads_frontmatter_only(self):
        skill = self.home / '.agents/skills/using-superpowers'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: using-superpowers\ndescription: Entry helper\n---\nBody description: ignore me\n')
        codex = self.home / '.codex'
        codex.mkdir()
        (codex / 'hooks.json').write_text(json.dumps({'hooks': {'SessionStart': [{'command': 'helper'}]}}))
        (codex / 'AGENTS.md').write_text('Use using-superpowers.\n')
        before = sorted(str(p) for p in self.home.rglob('*'))
        with patch.object(cx, 'run', return_value=None), patch.object(cx, 'live_models', return_value=None):
            detected = json.loads(self.cli('onboard', 'detect', '--json'))
        self.assertIsNone(detected['models'])
        self.assertIsNone(detected['quota'])
        self.assertEqual(detected['skills'][0]['description'], 'Entry helper')
        self.assertEqual({r['kind'] for r in detected['host']['other_entry']}, {'hook', 'skill', 'agents_md'})
        self.assertEqual(sorted(str(p) for p in self.home.rglob('*')), before)

    def test_judge_aliases_store_new_key_and_profile_normalizes_alias(self):
        cx.checkpoint_start(str(self.home), 'a', 'a', mode='균형', mode_source='default')
        self.cli('checkpoint', 'update', '--project', str(self.home), '--judge-calls', '3')
        _, row = cx.load_checkpoint(str(self.home))
        self.assertEqual(row['judge_calls'], 3)
        self.assertNotIn('fable_calls', row)
        self.cli('profile', 'set', '--project', str(self.home), '--by', 'fable')
        self.assertEqual(cx.read_profile(str(self.home))['by'], 'judge')

    def test_status_adds_overlay_entry_roles_onboarding_and_retro_lines(self):
        with patch.object(cx, 'run', return_value=None), patch.object(cx, 'live_models', return_value=None):
            output = self.cli('status', '--project', str(self.home))
        for label in ('roles:', 'overlay: absent', 'entry: absent', 'onboarding: needed', 'retro_apply: propose'):
            self.assertIn(label, output)


    def test_entry_preserves_exact_source_and_outside_bytes(self):
        source = b'entry without final newline'
        (self.refs / 'entry-block.md').write_bytes(source)
        target = self.home / '.codex/AGENTS.md'
        target.parent.mkdir()
        original = b'User text\r\nLast line without newline'
        target.write_bytes(original)
        self.cli('entry', 'install')
        content = target.read_bytes()
        self.assertEqual(content.split(cx.ENTRY_BEGIN.encode() + b'\n', 1)[1].split(cx.ENTRY_END.encode(), 1)[0], source)
        self.assertIn('present', self.cli('entry', 'status'))
        self.cli('entry', 'remove')
        self.assertEqual(target.read_bytes(), original)

    def test_detect_corrupt_optional_overlay_still_returns_json(self):
        self.local('settings.json', {}).write_text('{')
        self.local('tiers.json', {}).write_text('{')
        with patch.object(cx, 'run', return_value=None), patch.object(cx, 'live_models', return_value=None):
            result = json.loads(self.cli('onboard', 'detect', '--json'))
        self.assertIsNone(result['models'])
        self.assertIsNone(result['unknown_models'])

    def test_doctor_failure_severity_is_independent_of_advisor_and_roster(self):
        self.local('settings.json', {'entry': 'none', 'language': 'en', 'orchestrator': 'other'})
        role = {'model': 'worker', 'effort': 'high', 'job': 'test', 'skills': []}
        self.local('roles.json', {'roles': {'cx-build': role}})
        self.local('routing.json', {'mcp_usage': {}, 'routes': []})
        with patch.object(cx, 'ocx_path', return_value='/bin/sh'), patch.object(cx, 'live_models', return_value=['worker']):
            with self.assertRaises(SystemExit) as result, contextlib.redirect_stdout(io.StringIO()) as output:
                cx.main(['doctor', '--home', str(self.home)])
        self.assertEqual(result.exception.code, 0)
        self.assertIn('WARN routed models in subagent roster:', output.getvalue())
        self.assertIn('INFO advisor:', output.getvalue())
        self.assertIn('WARN default model:', output.getvalue())
        self.local('roles.json', {'invalid': {}})
        with patch.object(cx, 'ocx_path', return_value='/bin/sh'), patch.object(cx, 'live_models', return_value=['worker']):
            with self.assertRaises(SystemExit) as result:
                self.cli('doctor')
        self.assertEqual(result.exception.code, 1)

    def test_quota_custom_thresholds_and_every_boundary(self):
        self.local('settings.json', {'quota': [{'key': 'k', 'provider': 'v', 'window': 'weekly', 'models': ['v/*']}],
                                    'thresholds': {'no_expand': 20, 'burn_off': .4, 'burn_down': .6}})
        q, b = {'v': {'weekly': 19.9}}, {'v': {'weekly': .399}}
        self.assertEqual(cx.expansion_state(q, b), {'k': 'on'})
        q['v']['weekly'] = 20
        self.assertEqual(cx.expansion_state(q, b), {'k': 'off'})
        self.assertIn('확대 자리', ' '.join(cx.quota_advice(q, b)))
        q['v']['weekly'] = 1
        b['v']['weekly'] = .4
        self.assertEqual(cx.expansion_state(q, b), {'k': 'off'})
        self.assertNotIn('균형 배정', ' '.join(cx.quota_advice(q, b)))
        b['v']['weekly'] = .6
        self.assertNotIn('균형 배정', ' '.join(cx.quota_advice(q, b)))
        b['v']['weekly'] = .601
        self.assertIn('균형 배정', ' '.join(cx.quota_advice(q, b)))
        self.assertIn('미확인', ' '.join(cx.quota_advice({'v': {}}, b)))

    POLICY = [
        {'key': 'openai', 'provider': 'openai', 'window': 'weekly', 'models': ['gpt-*'],
         'thresholds': {'no_expand': None, 'restrict': None, 'stop': None}},
        {'key': 'anthropic', 'provider': 'anthropic', 'window': 'weekly', 'models': ['anthropic/*'],
         'thresholds': {'no_expand': 70, 'restrict': None, 'stop': 85, 'orchestrator_notice': 85},
         'substitutes': ['gpt-6-astra xhigh']},
        {'key': 'fable', 'provider': 'anthropic', 'window': 'Fable', 'models': ['anthropic/claude-fable-*'], 'parent': 'anthropic',
         'thresholds': {'no_expand': 80, 'restrict': 80, 'stop': 95},
         'substitutes': ['anthropic/claude-opus-5-5 xhigh', 'gpt-6-astra xhigh']}]

    def test_quota_keys_carry_their_own_thresholds_and_substitutes(self):
        self.local('settings.json', {'quota': self.POLICY, 'orchestrator': 'anthropic/claude-opus-5-5'})
        self.local('roles.json', {'roles': {'cx-build': {'model': 'gpt-6.1-sol', 'effort': 'high', 'job': 'build', 'skills': []}}})
        burn = {'openai': {'weekly': .3}, 'anthropic': {'weekly': .3, 'Fable': .3}}
        quota = {'openai': {'weekly': 75}, 'anthropic': {'weekly': 86, 'Fable': 50}}
        advice = ' | '.join(cx.quota_advice(quota, burn))
        self.assertIn('anthropic 한도 86%: 이 키 모델을 배정하지 않고 같은 역할의 대체 순서로 넘긴다. 대체: gpt-6-astra xhigh', advice)
        self.assertIn('anthropic 한도 86%: 메인 세션도 이 한도를 쓰므로', advice)
        self.assertIn('openai 한도 75%: 구현 작업자는 1개', advice)
        self.assertNotIn('openai 한도 75%: 이 키 모델의 확대 자리', advice)
        self.assertEqual(cx.expansion_state(quota, burn), {'openai': 'on', 'anthropic': 'off', 'fable': 'off'})
        quota = {'openai': {'weekly': 10}, 'anthropic': {'weekly': 60, 'Fable': 96}}
        advice = ' | '.join(cx.quota_advice(quota, burn))
        self.assertIn('fable 한도 96%: 이 키 모델을 배정하지 않고 같은 역할의 대체 순서로 넘긴다. '
                      '대체: anthropic/claude-opus-5-5 xhigh, gpt-6-astra xhigh', advice)
        quota = {'openai': {'weekly': 10}, 'anthropic': {'weekly': 90, 'Fable': 96}}
        advice = ' | '.join(cx.quota_advice(quota, burn))
        self.assertIn('fable 한도 96%: 이 키 모델을 배정하지 않고 같은 역할의 대체 순서로 넘긴다. 대체: gpt-6-astra xhigh', advice)
        quota = {'openai': {'weekly': 10}, 'anthropic': {'weekly': 60, 'Fable': 82}}
        self.assertIn('fable 한도 82%: 이 키 모델은 유형 E 본분석과 R3 검토에만 쓴다', ' '.join(cx.quota_advice(quota, burn)))
        self.assertEqual(cx.expansion_state(quota, burn), {'openai': 'on', 'anthropic': 'on', 'fable': 'off'})

    def test_stopped_key_never_expands_and_parent_stop_removes_substitutes(self):
        self.local('settings.json', {'quota': [
            {'key': 'common', 'provider': 'p', 'window': 'weekly', 'models': [], 'thresholds': {'no_expand': None, 'stop': 85},
             'substitutes': []},
            {'key': 'child', 'provider': 'p', 'window': 'Child', 'models': ['p/child'], 'parent': 'common'},
            {'key': 'other', 'provider': 'o', 'window': 'weekly', 'models': ['o/*'], 'thresholds': {'stop': 50},
             'substitutes': ['p/child high', 'o2/next high']}]})
        burn = {'p': {'weekly': .1, 'Child': .1}, 'o': {'weekly': .1}}
        quota = {'p': {'weekly': 96, 'Child': 10}, 'o': {'weekly': 60}}
        self.assertEqual(cx.expansion_state(quota, burn), {'common': 'off', 'child': 'off', 'other': 'off'})
        advice = ' '.join(cx.quota_advice(quota, burn))
        self.assertIn('other 한도 60%: 이 키 모델을 배정하지 않고 같은 역할의 대체 순서로 넘긴다. 대체: o2/next high', advice)
        self.assertNotIn('p/child high', advice)

    def test_quota_key_thresholds_are_validated(self):
        for row in ({'thresholds': {'unknown': 1}}, {'thresholds': {'stop': 'high'}}, {'thresholds': {'stop': True}},
                    {'substitutes': 'gpt-6-astra'}, {'substitutes': ['']}):
            with self.subTest(row=row), self.assertRaises(SystemExit):
                cx.validate_settings({'quota': [dict({'key': 'k', 'provider': 'v', 'window': 'weekly'}, **row)]})

    def test_route_names_missing_recommended_tool(self):
        source = SCRIPT.parent.parent / 'references' / 'recommended-skills.json'
        (self.refs / 'recommended-skills.json').write_bytes(source.read_bytes())
        self.local('routing.json', {'mcp_usage': {}, 'routes': [
            {'id': 'ui-unslop', 'label': 'unslop', 'keywords': ['unslop'], 'skills': [], 'role': 'cx-build', 'risk': 'R2', 'note': 'n'}]})
        output = self.cli('route', 'unslop 해줘')
        self.assertIn('recommended (미설치): unslop — 설치: `$skill-installer install', output)
        self.assertIn('recommended (미설치): StyleGallery', output)
        plugin = self.home / '.codex/plugins/cache/vendor/unslop/1.0.0/skills/unslop/SKILL.md'
        plugin.parent.mkdir(parents=True)
        plugin.write_text('---\nname: unslop\ndescription: Remove the AI-generated look.\n---\n')
        self.assertNotIn('recommended (미설치): unslop', self.cli('route', 'unslop 해줘'))

    def test_first_retro_is_due_a_week_after_onboarding(self):
        self.local('settings.json', {'onboarding': {'method': 'B', 'date': cx.now().date().isoformat()}})
        with patch.object(cx, 'live_models', return_value=None), patch.object(cx, 'run', return_value=None):
            output = self.cli('status', '--project', str(self.home))
        self.assertIn('retro: ok, first run in 7 days', output)
        old = (cx.now().date() - cx.datetime.timedelta(days=8)).isoformat()
        self.local('settings.json', {'onboarding': {'method': 'B', 'date': old}})
        with patch.object(cx, 'live_models', return_value=None), patch.object(cx, 'run', return_value=None):
            output = self.cli('status', '--project', str(self.home))
        self.assertIn('retro: never run, due', output)
        # A computer that only receives the engine turns its retrospective off.
        self.local('settings.json', {'onboarding': {'method': 'B', 'date': old}, 'retro': {'apply': 'off'}})
        with patch.object(cx, 'live_models', return_value=None), patch.object(cx, 'run', return_value=None):
            output = self.cli('status', '--project', str(self.home))
        self.assertIn('retro: off', output)
        self.assertIn('retro_apply: off', output)
        with self.assertRaisesRegex(SystemExit, 'retro.apply=off'):
            self.cli('retro', '--days', '7')
        self.assertFalse((Path(os.environ['CONDUCTOR_HOME']) / 'retro' / 'last.json').exists())

    def test_overlay_restore_selected_snapshot_and_ignores_unmanaged_files(self):
        path = self.local('settings.json', {'language': 'en'})
        self.cli('overlay', 'backup')
        first = cx.overlay_backups()[0].name
        path.write_text(json.dumps({'language': 'ko'}))
        self.cli('overlay', 'backup')
        unmanaged = path.parent / 'custom.txt'
        unmanaged.write_text('user data')
        self.cli('overlay', 'restore', '--id', first)
        self.assertEqual(json.loads(path.read_text()), {'language': 'en'})
        self.assertEqual(unmanaged.read_text(), 'user data')

    def test_apply_rejects_invalid_plan_and_preserves_existing_overlay_on_entry_failure(self):
        plan = self.plan(['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6.1-terra'])
        path = Path(self.temp.name) / 'plan.json'
        original = self.local('settings.json', {'language': 'en'})
        before = original.read_bytes()
        plan['roles']['roles']['cx-build']['model'] = 42
        path.write_text(json.dumps(plan))
        with self.assertRaises(SystemExit):
            self.cli('onboard', 'apply', '--plan', str(path))
        self.assertEqual(original.read_bytes(), before)
        plan['roles']['roles']['cx-build']['model'] = 'gpt-6.1-sol'
        path.write_text(json.dumps(plan))
        with patch.object(cx, 'command_entry', side_effect=SystemExit('entry failed')), self.assertRaises(SystemExit):
            self.cli('onboard', 'apply', '--plan', str(path))
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse((self.home / '.codex/config.toml').exists())
        self.assertFalse(list((self.home / '.codex/agents').glob('*.toml')))

    def test_aa_refresh_compares_previous_state_csv_and_reads_tier_slugs(self):
        self.local('tiers.json', {'models': {'local/model': {'tier': 'frontier', 'aa_slug': 'local-slug'}}})
        directory = cx.state() / 'aa'
        directory.mkdir(parents=True)
        (directory / 'aa-previous.csv').write_text('model,effort,intelligence_index\nOld,high,10\n')
        requested = []

        def response(request, timeout):
            requested.append(request.full_url)
            return contextlib.closing(io.BytesIO(b'page'))

        row = dict.fromkeys(cx.AA_FIELDS, '')
        row.update(model='New', effort='high', intelligence_index=20)
        with patch.object(cx.urllib.request, 'urlopen', side_effect=response), \
                patch.object(cx, 'aa_variants', return_value={'high': {}}), patch.object(cx, 'aa_row', return_value=row):
            _, _, _, diff = cx.aa_refresh([])
        self.assertIn('Old (high)', diff['removed'])
        self.assertTrue(any(url.endswith('/local-slug') for url in requested))


    def test_detect_plugin_reads_frontmatter_without_loading_skill_body(self):
        directory = self.home / '.codex/plugins/cache/vendor/pkg/revision/skills/helper'
        directory.mkdir(parents=True)
        skill = directory / 'SKILL.md'
        skill.write_text('---\nname: helper\ndescription: >\n  First line\n  second line\n---\nBody\n')
        original = Path.read_text

        def guard(path, *args, **kwargs):
            if path.name == 'SKILL.md':
                raise AssertionError('must not read the complete skill body')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', guard), patch.object(cx, 'run', return_value=None), \
                patch.object(cx, 'live_models', return_value=None):
            detected = json.loads(self.cli('onboard', 'detect', '--json'))
        self.assertEqual(detected['skills'][0]['description'], 'First line second line')

    def test_home_option_controls_default_state_without_environment_override(self):
        with patch.dict(os.environ):
            os.environ.pop('CONDUCTOR_HOME', None)
            self.cli('settings', 'set', 'language', 'en')
        self.assertTrue((self.home / '.codex/kp-conductor/local/settings.json').is_file())

    def test_verify_uses_real_doctor_and_restores_invalid_tier_overlay(self):
        self.local('settings.json', {'entry': 'none', 'language': 'en'})
        self.local('roles.json', {'roles': {}})
        self.local('routing.json', {'mcp_usage': {}, 'routes': []})
        target = self.local('tiers.json', {'models': {}})
        before = target.read_bytes()
        self.cli('overlay', 'backup')
        target.write_text('{')
        with patch.object(cx, 'ocx_path', return_value='/bin/sh'), patch.object(cx, 'live_models', return_value=[]):
            with self.assertRaises(SystemExit) as result:
                self.cli('overlay', 'verify', '--id', cx.overlay_backups()[0].name)
        self.assertEqual(result.exception.code, 1)
        self.assertEqual(target.read_bytes(), before)

    def test_configured_proxy_and_malformed_catalog_are_handled(self):
        self.local('settings.json', {'proxy': 'http://localhost:1234/'})
        with patch.object(cx.urllib.request, 'urlopen', return_value=contextlib.closing(io.BytesIO(b'[]'))) as opened:
            self.assertIsNone(cx.live_models())
        self.assertEqual(opened.call_args.args[0], 'http://localhost:1234/v1/models')


    def test_retro_reads_judge_role_and_both_checkpoint_call_keys(self):
        self.local('roles.json', {'roles': {'cx-hard': {'model': 'vendor/judge', 'effort': 'high', 'job': '', 'skills': []}}})
        cx.append_receipt({'ts': cx.now().isoformat(), 'model': 'vendor/judge', 'outcome': 'refused', 'classified': True})
        root = cx.state() / 'runs/fixture'
        root.mkdir(parents=True)
        for index, counts in enumerate([{'judge_calls': 3}, {'fable_calls': 2}, {'judge_calls': 4, 'fable_calls': 100}]):
            (root / ('%d.json' % index)).write_text(json.dumps({'status': 'done', 'updated': cx.now().isoformat(), **counts}))
        with patch.object(cx, 'run', return_value=None):
            report = cx.retro_report(7, self.home / 'sessions')
        self.assertIn('judge: model=vendor/judge calls=9 refused=1', report)

    def test_retro_default_writes_suggestions_to_proposals_without_engine_changes(self):
        engine_before = {p: p.read_bytes() for p in self.skill.rglob('*') if p.is_file()}
        with patch.object(cx, 'retro_report', return_value='Report\n제안: 설정을 검토한다\n'):
            self.cli('retro')
        proposals = list((cx.state() / 'proposals').glob('*.md'))
        self.assertEqual(len(proposals), 1)
        self.assertIn('제안: 설정을 검토한다', proposals[0].read_text())
        self.assertEqual({p: p.read_bytes() for p in self.skill.rglob('*') if p.is_file()}, engine_before)
        self.assertFalse((cx.state() / 'local').exists())

    def test_ocx_precedence_settings_then_path_then_home(self):
        with patch.object(cx.shutil, 'which', return_value='/tools/ocx'):
            self.assertEqual(cx.ocx_path(), '/tools/ocx')
            self.local('settings.json', {'ocx': '/custom/ocx'})
            self.assertEqual(cx.ocx_path(), '/custom/ocx')
        self.local('settings.json', {})
        with patch.object(cx.shutil, 'which', return_value=None), patch.object(cx.Path, 'home', return_value=self.home):
            self.assertEqual(cx.ocx_path(), str(self.home / '.local/bin/ocx'))


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        PublicContractTests.setUp(self)
        home = patch.dict(os.environ, {'HOME': str(self.home)})
        home.start()
        self.addCleanup(home.stop)
    cli = PublicContractTests.cli
    local = PublicContractTests.local
    # Keep the contract fixture, without rerunning inherited tests.
    def fixture(self):
        self.local('settings.json', {'entry': 'none', 'language': 'en'})
        self.role = {'model': 'old-model', 'effort': 'high', 'job': 'fixture', 'skills': []}
        self.local('roles.json', {'roles': {'cx-demo': self.role}})
        self.local('routing.json', {'mcp_usage': {}, 'routes': []})
        self.config = self.home / '.codex/config.toml'
        self.agents = self.home / '.codex/AGENTS.md'
        self.runtime = self.home / '.codex/agents/cx-demo.toml'

    def backup_id(self):
        self.cli('overlay', 'backup')
        return cx.overlay_backups()[0].name

    def test_review_1_settings_and_parent_symlinks_are_rejected(self):
        self.fixture()
        outside = self.home / 'outside.json'
        outside.write_text('{"language":"en"}')
        path = cx.state() / 'local/settings.json'
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(SystemExit):
            self.cli('settings', 'set', 'language', 'ko')
        self.assertEqual(outside.read_text(), '{"language":"en"}')
        path.unlink()
        self.cli('settings', 'set', 'language', 'en')
        directory = self.home / 'outside-agents'
        directory.mkdir()
        (self.home / '.codex/agents').symlink_to(directory)
        with self.assertRaises(SystemExit):
            self.cli('roles', 'sync')
        self.assertEqual(list(directory.iterdir()), [])

    def test_review_1_apply_rejects_symlink_before_mutation(self):
        self.fixture()
        plan = {'version': 1, 'routing': {'routes': [], 'mcp_usage': {}},
                'roles': {'roles': {'cx-demo': self.role}}, 'settings': {'language': 'en'},
                'entry': 'none', 'notes': 'new', 'warnings': []}
        path = self.home / 'plan.json'
        path.write_text(json.dumps(plan))
        outside = self.home / 'outside-settings'
        outside.write_text('{}')
        settings = cx.state() / 'local/settings.json'
        settings.unlink()
        settings.symlink_to(outside)
        with self.assertRaises(SystemExit):
            self.cli('onboard', 'apply', '--plan', str(path))
        self.assertEqual(outside.read_text(), '{}')
        self.assertFalse(self.runtime.exists())
        self.assertFalse(self.config.exists())

    def test_review_1_restore_continues_after_one_unit_fails(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        role_file = cx.state() / 'local/roles.json'
        role_file.write_text('{"roles":{}}')
        self.runtime.write_text(cx.ROLE_HEADER + '\nmodel = "bad"\n')
        settings = cx.state() / 'local/settings.json'
        settings.unlink()
        outside = self.home / 'outside'
        outside.write_text('untouched')
        settings.symlink_to(outside)
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()) as out:
            self.cli('overlay', 'restore', '--id', identifier)
        self.assertEqual(json.loads(role_file.read_text())['roles']['cx-demo'], self.role)
        self.assertIn('old-model', self.runtime.read_text())
        self.assertEqual(outside.read_text(), 'untouched')

    def test_review_2_existing_registration_survives_sync_and_uninstall(self):
        self.fixture()
        original = '[agents."cx-demo"]\nconfig_file = "./custom.toml"\n'
        self.config.write_text(original)
        with self.assertRaises(SystemExit):
            self.cli('roles', 'sync')
        self.assertFalse(self.runtime.exists())
        self.runtime.parent.mkdir()
        self.runtime.write_text(cx.ROLE_HEADER + '\nmodel = "old-model"\n')
        self.cli('uninstall')
        self.assertEqual(self.config.read_text(), original)

    def test_review_3_all_nonowned_agents_spellings_abort_before_writes(self):
        self.fixture()
        examples = [
            '[agents]\ncx-demo = {config_file = "./custom.toml"}\n',
            'agents.cx-demo.config_file = "./custom.toml"\n',
            'agents = {cx-demo = {config_file = "./custom.toml"}}\n',
            '[ agents . "cx-demo" ]\nconfig_file = "./custom.toml"\n',
            '[agents]\n"cx-demo".config_file = "./custom.toml"\n',
        ]
        for original in examples:
            with self.subTest(original=original):
                self.config.write_text(original)
                with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()) as out:
                    cx.main(['roles', 'sync', '--home', str(self.home)])
                self.assertIn('config_file', out.getvalue())
                self.assertEqual(self.config.read_text(), original)
                self.assertFalse(self.runtime.exists())

    def test_review_4_atomic_replace_failure_preserves_original_and_mode(self):
        self.fixture()
        self.config.write_text('model = "mine"\n')
        self.config.chmod(0o640)
        replace = os.replace
        def fail(source, target):
            if Path(target) == self.config.resolve():
                raise OSError('injected atomic write interruption')
            return replace(source, target)
        with patch.object(cx.os, 'replace', side_effect=fail), self.assertRaises((OSError, SystemExit)):
            self.cli('roles', 'sync')
        self.assertEqual(self.config.read_text(), 'model = "mine"\n')
        self.assertFalse(self.runtime.exists())
        self.cli('roles', 'sync')
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)

    def test_review_4_entry_detects_external_edit_during_backup(self):
        self.fixture()
        self.agents.write_text('original\n')
        backup = cx.overlay_backup
        def concurrent(extra=None):
            result = backup(extra)
            self.agents.write_text('original\nconcurrent rule\n')
            return result
        with patch.object(cx, 'overlay_backup', side_effect=concurrent), self.assertRaises(SystemExit):
            self.cli('entry', 'install')
        self.assertEqual(self.agents.read_text(), 'original\nconcurrent rule\n')

    def test_review_4_single_cli_lock_and_single_multifile_backup(self):
        self.fixture()
        import fcntl
        with patch.object(cx.fcntl, 'flock', wraps=fcntl.flock) as lock:
            self.cli('roles', 'sync')
        self.assertEqual(sum(call.args[1] == fcntl.LOCK_EX for call in lock.call_args_list), 1)
        self.assertEqual(len(cx.overlay_backups()), 1)
        self.assertEqual((cx.state() / '.lock').is_file(), True)

    def test_review_4_uninstall_failure_rolls_back_all_owned_units(self):
        self.fixture()
        self.cli('roles', 'sync')
        self.cli('entry', 'install')
        original = {p: p.read_bytes() for p in (self.config, self.agents, self.runtime)}
        replace = os.replace
        def fail(source, target):
            if Path(target) == self.config.resolve():
                raise OSError('injected uninstall failure')
            return replace(source, target)
        with patch.object(cx.os, 'replace', side_effect=fail), self.assertRaises((SystemExit, OSError)):
            self.cli('uninstall')
        self.assertEqual({p: p.read_bytes() for p in original}, original)

    def test_review_5_verify_restores_runtime_registration_and_checks_again(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        self.local('roles.json', {'roles': {'cx-demo': dict(self.role, model='gone-model')}})
        self.cli('roles', 'sync')
        self.config.write_text('model = "user-edit"\n' + self.config.read_text())
        with patch.object(cx, 'ocx_path', return_value='/bin/sh'), patch.object(cx, 'live_models', return_value=['old-model']), \
                patch.object(cx, 'command_doctor', wraps=cx.command_doctor) as doctor:
            with self.assertRaises(SystemExit) as error:
                self.cli('overlay', 'verify', '--id', identifier)
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(doctor.call_count, 2)
        self.assertIn('old-model', self.runtime.read_text())
        self.assertNotIn('gone-model', self.runtime.read_text())
        self.assertTrue(self.config.read_text().startswith('model = "user-edit"'))

    def test_review_6_verify_requires_explicit_id_settings_does_not_backup(self):
        self.fixture()
        identifier = self.backup_id()
        before = cx.overlay_backups()
        self.cli('settings', 'set', 'retro.apply', 'notify')
        self.assertEqual(cx.overlay_backups(), before)
        with self.assertRaises(SystemExit):
            self.cli('overlay', 'verify')
        self.assertEqual(cx.overlay_backups()[0].name, identifier)

    def test_review_7_tiers_and_role_values_are_validated(self):
        self.fixture()
        for data in ({'seats': []}, {'models': []}, {'constraints': {}},
                     {'seats': {'cx-demo': [['old-model']]}},
                     {'seats': {'cx-demo': [['unknown-model', 'high']]}},
                     {'constraints': [{'role': 'cx-demo', 'differs_from': 'x'}]}):
            with self.subTest(data=data):
                self.local('tiers.json', data)
                with self.assertRaises(SystemExit):
                    cx.load_tiers()
        for field, value in [('effort', 'invalid'), ('sandbox', 'invalid'), ('model', ''), ('service_tier', 'priority')]:
            with self.subTest(field=field), self.assertRaises(SystemExit):
                cx.load_roles(data={'roles': {'cx-demo': dict(self.role, **{field: value})}})

    def test_role_service_tier_is_written_only_when_declared(self):
        role = {'model': 'reviewer-model', 'effort': 'high', 'job': 'review', 'skills': [], 'sandbox': 'read-only'}
        self.assertNotIn('service_tier', cx.role_toml('cx-demo', role, [])[0])
        text = cx.role_toml('cx-demo', dict(role, service_tier='default'), [])[0]
        self.assertIn('service_tier = "default"', text)
        self.assertEqual(cx.load_roles(data={'roles': {'cx-demo': dict(role, service_tier='default')}})['cx-demo']['service_tier'], 'default')

    def test_role_service_tier_reaches_role_file_and_doctor_detects_drift(self):
        self.fixture()
        self.local('roles.json', {'roles': {'cx-demo': dict(self.role, service_tier='default')}})
        self.cli('roles', 'sync')
        self.assertIn('service_tier = "default"', self.runtime.read_text())

        def doctor():
            with patch.object(cx, 'ocx_path', return_value='/bin/sh'), patch.object(cx, 'live_models', return_value=['old-model']):
                with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()) as output:
                    cx.main(['doctor', '--home', str(self.home)])
            return output.getvalue()
        self.assertIn('OK   role declaration cx-demo: matches', doctor())
        self.local('roles.json', {'roles': {'cx-demo': self.role}})
        self.assertIn('FAIL role declaration cx-demo: model/effort/sandbox/service_tier mismatch', doctor())

    def test_review_7_verify_rejects_well_formed_broken_tiers_and_runtime_drift(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        for corrupt in ('tiers', 'runtime'):
            if corrupt == 'tiers':
                self.local('tiers.json', {'seats': []})
            else:
                self.runtime.write_text(self.runtime.read_text().replace('"high"', '"low"'))
            with patch.object(cx, 'ocx_path', return_value='/bin/sh'), patch.object(cx, 'live_models', return_value=['old-model']):
                with self.assertRaises(SystemExit) as error:
                    self.cli('overlay', 'verify', '--id', identifier)
            self.assertEqual(error.exception.code, 1)
        self.assertIn('"high"', self.runtime.read_text())

    def test_review_8_apply_none_removes_only_entry_block(self):
        self.fixture()
        self.agents.write_bytes(b'user rules\r\n')
        self.cli('entry', 'install')
        plan = {'version': 1, 'routing': {'mcp_usage': {}, 'routes': []},
                'roles': {'roles': {'cx-demo': self.role}}, 'settings': {'language': 'en'},
                'entry': 'none', 'notes': '', 'warnings': []}
        path = self.home / 'plan.json'
        path.write_text(json.dumps(plan))
        self.cli('onboard', 'apply', '--plan', str(path))
        self.assertEqual(self.agents.read_bytes(), b'user rules\r\n')

    def test_user_file_symlinks_are_preserved_and_report_real_targets(self):
        self.fixture()
        actual_config = self.home / 'actual-config'
        actual_entry = self.home / 'actual-entry'
        actual_config.write_text('model = "mine"\n')
        actual_entry.write_text('user rule\n')
        self.config.symlink_to(actual_config)
        self.agents.symlink_to(actual_entry)
        output = self.cli('roles', 'sync') + self.cli('entry', 'install')
        self.assertIn(str(actual_config.resolve()), output)
        self.assertIn(str(actual_entry.resolve()), output)
        self.cli('uninstall')
        self.assertTrue(self.config.is_symlink())
        self.assertTrue(self.agents.is_symlink())
        self.assertEqual(actual_entry.read_text(), 'user rule\n')
        self.assertEqual(actual_config.read_text().strip(), 'model = "mine"')

    def test_review_10_backup_includes_engine_owned_units_and_user_files(self):
        self.fixture()
        self.cli('roles', 'sync')
        self.cli('entry', 'install')
        output = self.cli('backup')
        directory = sorted((cx.state() / 'backups').iterdir())[-1]
        for name in ('config.toml', 'AGENTS.md'):
            self.assertEqual((directory / name).read_bytes(), (self.home / '.codex' / name).read_bytes())
        self.assertTrue(list(directory.rglob('roles.json')))
        self.assertTrue(list(directory.rglob('cx-demo.toml')))
        self.assertTrue(list(directory.rglob('manifest.json')))
        self.assertIn('scope:', output)
        for item in directory.rglob('*'):
            self.assertEqual(item.stat().st_mode & 0o777, 0o700 if item.is_dir() else 0o600)

    def test_review_11_banned_skills_are_only_personal_patterns(self):
        self.fixture()
        directory = self.home / '.agents/skills/orca-example'
        directory.mkdir(parents=True)
        (directory / 'SKILL.md').write_text('---\nname: orca-example\n---\n')
        with patch.object(cx, 'skill_usage', return_value=({}, {}, {})):
            audit = cx.skills_audit(home=self.home)
            self.assertNotEqual(audit['skills'][0]['verdict'], 'banned')
            self.local('settings.json', {'entry': 'none', 'banned_skills': ['orca-*']})
            audit = cx.skills_audit(home=self.home)
        self.assertEqual(audit['skills'][0]['verdict'], 'banned')


    def test_uninstall_removes_owned_alias_but_preserves_marked_user_file(self):
        self.fixture()
        self.cli('roles', 'sync')
        self.config.write_text(self.config.read_text() + '[agents.alias]\nconfig_file = "./agents/cx-demo.toml"\n'
                               '# conductor:role\n[agents.marked-user]\nconfig_file = "./custom.toml"\n')
        self.cli('uninstall')
        self.assertNotIn('[agents.alias]', self.config.read_text())
        self.assertIn('[agents.marked-user]', self.config.read_text())

    def test_receipt_write_refuses_owned_symlink(self):
        self.fixture()
        outside = self.home / 'outside-receipt'
        outside.write_text('original\n')
        (cx.state() / 'receipts.jsonl').symlink_to(outside)
        with self.assertRaises(SystemExit):
            cx.append_receipt({'outcome': 'pass'})
        self.assertEqual(outside.read_text(), 'original\n')

    def test_settings_detects_external_change_and_preserves_mode(self):
        self.fixture()
        path = cx.state() / 'local/settings.json'
        path.chmod(0o640)
        validate = cx.validate_settings
        def edit(data):
            result = validate(data)
            path.write_text('{"language":"fr"}')
            return result
        with patch.object(cx, 'validate_settings', side_effect=edit), self.assertRaises(SystemExit):
            self.cli('settings', 'set', 'language', 'ko')
        self.assertEqual(json.loads(path.read_text()), {'language': 'fr'})
        self.cli('settings', 'set', 'language', 'en')
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_overlay_backup_first_line_and_private_permissions(self):
        self.fixture()
        actual = self.home / 'actual-config'
        actual.write_text('model = "user"\n')
        self.config.symlink_to(actual)
        output = self.cli('overlay', 'backup')
        self.assertTrue(output.splitlines()[0].startswith('overlay backup: '))
        self.assertIn(str(actual.resolve()), output)
        target = cx.overlay_backups()[0]
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        for item in (target, *target.rglob('*')):
            self.assertEqual(item.stat().st_mode & 0o777, 0o700 if item.is_dir() else 0o600)

    def test_legacy_generated_registration_is_preserved_without_duplicate(self):
        self.fixture()
        self.runtime.parent.mkdir()
        self.runtime.write_text(cx.ROLE_HEADER + '\nmodel = "old-model"\n')
        original = '[ agents . "cx-demo" ]\nconfig_file = "./agents/cx-demo.toml"\n'
        self.config.write_text(original)
        self.cli('roles', 'sync')
        self.assertEqual(self.config.read_text(), original)
        self.cli('roles', 'sync')
        self.assertEqual(self.config.read_text(), original)


    def test_unrelated_user_registration_can_point_to_symlink(self):
        self.fixture()
        custom = self.home / 'custom-role'
        custom.write_text('# user role\n')
        linked = self.home / '.codex/custom-linked.toml'
        linked.symlink_to(custom)
        original = '[agents.custom]\nconfig_file = "./custom-linked.toml"\n'
        self.config.write_text(original)
        self.cli('roles', 'sync')
        self.cli('uninstall')
        self.assertTrue(linked.is_symlink())
        self.assertEqual(custom.read_text(), '# user role\n')
        self.assertTrue(self.config.read_text().startswith(original))

    def test_python39_refuses_top_level_inline_agents_before_mutation(self):
        self.fixture()
        original = 'agents = {other = {config_file = "./other.toml"}}\n'
        self.config.write_text(original)
        with patch.object(cx, 'tomllib', None), self.assertRaises(SystemExit):
            self.cli('roles', 'sync')
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.config.read_text(), original)

    def test_tomllib_checks_semantics_preserving_nonowned_values(self):
        if cx.tomllib is None:
            self.skipTest('tomllib is available on Python 3.11+')
        self.fixture()
        original = 'model = "mine"\n[agents]\nmax_threads = 7\n[agents.user]\nconfig_file="./user.toml"\n'
        self.config.write_text(original)
        self.cli('roles', 'sync')
        before, after = cx.tomllib.loads(original), cx.tomllib.loads(self.config.read_text())
        del after['agents']['cx-demo']
        self.assertEqual(after, before)
        with self.assertRaises(SystemExit):
            cx.validate_config_change(original, original.replace('max_threads = 7', 'max_threads = 9'), {'cx-demo'})


    def test_marked_registrations_remain_owned_when_multiple_runtime_files_missing(self):
        self.fixture()
        self.local('roles.json', {'roles': {'cx-demo': self.role, 'second': self.role}})
        self.config.write_text('# conductor:role\n[agents.cx-demo]\nconfig_file = "./agents/cx-demo.toml"\n\n'
                               '# conductor:role\n[agents.second]\nconfig_file = "./agents/second.toml"\n')
        self.cli('roles', 'sync')
        first = self.config.read_bytes()
        self.cli('roles', 'sync')
        self.assertEqual(self.config.read_bytes(), first)
        self.assertEqual(self.config.read_text().count('# conductor:role'), 2)

    def test_settings_process_waits_for_cli_lock(self):
        self.fixture()
        import fcntl
        import subprocess
        import sys
        path = cx.state() / 'local/settings.json'
        original = path.read_bytes()
        with (cx.state() / '.lock').open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            process = subprocess.Popen([sys.executable, str(SCRIPT), 'settings', 'set', 'language', 'fr',
                                        '--home', str(self.home)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=dict(os.environ, HOME=str(self.home)))
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=.2)
                self.assertEqual(path.read_bytes(), original)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(json.loads(path.read_text())['language'], 'fr')

    def test_verify_uses_selected_good_backup_even_when_newer_bad_backup_exists(self):
        self.fixture()
        self.cli('roles', 'sync')
        first = self.backup_id()
        self.local('roles.json', {'roles': {'cx-demo': dict(self.role, model='gone-model')}})
        self.cli('roles', 'sync')
        self.backup_id()
        with patch.object(cx, 'ocx_path', return_value='/bin/sh'), patch.object(cx, 'live_models', return_value=['old-model']):
            with self.assertRaises(SystemExit) as error:
                self.cli('overlay', 'verify', '--id', first)
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(cx.load_roles()['cx-demo']['model'], 'old-model')
        self.assertIn('old-model', self.runtime.read_text())


    def test_adjacent_marker_owns_role_after_earlier_standalone_comment(self):
        self.fixture()
        self.config.write_text('# conductor:role\n\n# user comment\n# conductor:role\n'
                               '[agents.cx-demo]\nconfig_file = "./agents/cx-demo.toml"\n')
        self.cli('roles', 'sync')
        self.assertTrue(self.runtime.exists())

    def test_restore_preserves_unowned_role_symlink(self):
        self.fixture()
        self.cli('roles', 'sync')
        first = self.backup_id()
        outside = self.home / 'custom-user'
        outside.write_text('# user owned role\n')
        link = self.runtime.parent / 'custom.toml'
        link.symlink_to(outside)
        self.cli('overlay', 'restore', '--id', first)
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(), '# user owned role\n')


    def test_backup_output_path_can_be_used_as_verify_id(self):
        self.fixture()
        identifier = self.cli('overlay', 'backup').splitlines()[0].split(': ', 1)[1]
        with patch.object(cx, 'command_doctor', return_value=None):
            self.assertIn('overlay verified', self.cli('overlay', 'verify', '--id', identifier))

    def test_review2_normal_toml_mentions_are_not_registration_conflicts(self):
        self.fixture()
        examples = [
            '[agents]\nmax_threads = 6\n# cx-demo is managed\n',
            'developer_instructions = "Delegate to cx-demo"\n[agents]\nmax_threads = 6\n',
            'developer_instructions = """\n[agents.cx-demo]\nconfig_file="fake"\n'
            '# conductor:roles:begin v1\n# conductor:roles:end\n"""\n[agents]\nmax_threads=6\n',
            "developer_instructions = '''\n[agents.cx-demo]\n'''\n[agents]\nmax_threads=6\n",
            "[agents.user]\nconfig_file = '''./user's-role.toml'''\n",
            'agents.other.config_file = "cx-demo"\n',
            '[agents]\nother = {description="cx-demo", config_file="./other.toml"}\n',
        ]
        for original in examples:
            with self.subTest(original=original):
                self.config.write_bytes(original.encode())
                self.cli('roles', 'sync')
                self.assertTrue(self.config.read_bytes().startswith(original.encode()))
                self.assertEqual(cx.registered_roles(self.config.read_text()) & {'cx-demo'}, {'cx-demo'})

    def test_review2_registration_region_preserves_bytes_and_crlf(self):
        self.fixture()
        for newline in ('\n', '\r\n'):
            for ending in ('', newline):
                with self.subTest(newline=newline, ending=ending):
                    original = ('# 사용자 주석' + newline + '[agents]' + newline + 'max_threads=6' + ending).encode()
                    self.config.write_bytes(original)
                    self.cli('roles', 'sync')
                    result = self.config.read_bytes()
                    self.assertTrue(result.startswith(original))
                    self.assertIn(('# conductor:roles:begin v1' + newline).encode(), result)
                    self.assertTrue(result.endswith(('# conductor:roles:end' + newline).encode()))
                    self.assertNotIn(b'# conductor:role' + newline.encode(), result)
                    self.cli('roles', 'sync')
                    self.assertEqual(self.config.read_bytes(), result)

    def test_review2_scalar_and_quoted_role_keys_conflict(self):
        self.fixture()
        for original in ('[agents]\ncx-demo = 1\n',
                         '["agents"."cx-demo"]\nconfig_file="x"\n',
                         '"agents"."cx-demo".config_file="x"\n',
                         'agents = {"cx-demo" = {config_file="x"}}\n'):
            with self.subTest(original=original):
                self.config.write_text(original)
                with self.assertRaises(SystemExit):
                    self.cli('roles', 'sync')
                self.assertEqual(self.config.read_text(), original)
                self.assertFalse(self.runtime.exists())

    def test_review2_automatic_rollback_never_touches_unwritten_external_role(self):
        self.fixture()
        self.cli('roles', 'sync')
        backup = cx.overlay_backup
        external = self.runtime.read_bytes().replace(b'old-model', b'external-model')
        def concurrent(extra=None):
            target = backup(extra)
            self.runtime.write_bytes(external)
            return target
        with patch.object(cx, 'overlay_backup', side_effect=concurrent), self.assertRaises(SystemExit):
            self.cli('roles', 'sync')
        self.assertEqual(self.runtime.read_bytes(), external)

    def test_review2_automatic_rollback_preserves_external_edit_after_write(self):
        self.fixture()
        self.cli('roles', 'sync')
        self.local('roles.json', {'roles': {'cx-demo': dict(self.role, model='new-model'), 'second': self.role}})
        write = cx.atomic_write
        external = cx.ROLE_HEADER + '\nmodel="external-model"\n'
        def interrupt(path, content, *args, **kwargs):
            if Path(path) == self.config.resolve():
                self.runtime.write_text(external)
                raise OSError('after role writes')
            return write(path, content, *args, **kwargs)
        with patch.object(cx, 'atomic_write', side_effect=interrupt), contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(OSError):
                cx.main(['roles', 'sync', '--home', str(self.home)])
        self.assertEqual(self.runtime.read_text(), external)
        self.assertFalse((self.runtime.parent / 'second.toml').exists())
        self.assertIn('복구하지 않음(외부 변경)', out.getvalue())
        self.assertIn('rollback incomplete', out.getvalue())

    def test_review2_automatic_rollback_restores_only_this_commands_writes(self):
        self.fixture()
        self.cli('roles', 'sync')
        settings = cx.state() / 'local/settings.json'
        original = self.runtime.read_bytes()
        with patch.object(cx, 'HOME_OVERRIDE', self.home), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                with cx.change_units():
                    cx.atomic_write(self.runtime, original.replace(b'old-model', b'new-model'))
                    settings.write_text('{"language":"external"}')
                    raise RuntimeError('fail')
        self.assertEqual(self.runtime.read_bytes(), original)
        self.assertEqual(settings.read_text(), '{"language":"external"}')

    def test_review2_restore_removes_alias_before_deleting_generated_file(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        self.local('roles.json', {'roles': {'cx-demo': self.role, 'second': self.role}})
        self.cli('roles', 'sync')
        self.config.write_text(self.config.read_text() + '[agents.alias]\nconfig_file="./agents/second.toml"\n')
        cx.LAST_WRITES.clear()
        self.cli('overlay', 'restore', '--id', identifier)
        self.assertFalse((self.runtime.parent / 'second.toml').exists())
        self.assertNotIn('[agents.alias]', self.config.read_text())
        self.assertNotIn('[agents.second]', self.config.read_text())

    def test_review2_restore_crlf_preserves_user_edit(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        current = self.config.read_text().replace('\n', '\r\n')
        prefix = 'model="user-edit"\r\n# 사용자\r\n'
        self.config.write_bytes((prefix + current).encode())
        cx.LAST_WRITES.clear()
        self.cli('overlay', 'restore', '--id', identifier)
        self.assertTrue(self.config.read_bytes().startswith(prefix.encode()))
        self.assertNotIn(b'\n', self.config.read_bytes().replace(b'\r\n', b''))

    def test_review2_unchanged_restore_reports_no_change(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        cx.LAST_WRITES.clear()
        output = self.cli('overlay', 'restore', '--id', identifier)
        self.assertIn('변경 없음', output)
        self.assertIn('변경 없음: ' + str(self.runtime.resolve()), output)
        self.assertIn('변경 없음: ' + str((cx.state() / 'local/settings.json').resolve()), output)
        self.assertNotIn('복구하지 않음', output)

    def test_review2_rollback_checks_last_write_and_preserves_recreated_files(self):
        self.fixture()
        self.cli('roles', 'sync')
        original = self.runtime.read_bytes()
        with self.assertRaises(RuntimeError), contextlib.redirect_stdout(io.StringIO()):
            with cx.change_units():
                cx.atomic_write(self.runtime, 'first')
                cx.atomic_write(self.runtime, 'second')
                raise RuntimeError('fail after second write')
        self.assertEqual(self.runtime.read_bytes(), original)
        with self.assertRaises(RuntimeError), contextlib.redirect_stdout(io.StringIO()) as out:
            with cx.change_units():
                cx.atomic_remove(self.runtime)
                self.runtime.write_text('recreated externally')
                raise RuntimeError('fail after deletion')
        self.assertEqual(self.runtime.read_text(), 'recreated externally')
        self.assertIn('복구하지 않음(외부 변경)', out.getvalue())

    def test_review2_rollback_preserves_concurrent_user_files(self):
        self.fixture()
        self.cli('roles', 'sync')
        self.cli('entry', 'install')
        for path in (self.config, self.agents):
            original = path.read_bytes()
            external = original + b'\n# external user edit\n'
            with self.subTest(path=path), contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(RuntimeError):
                    with cx.change_units():
                        cx.atomic_write(path, original + b'\n# command change\n')
                        path.write_bytes(external)
                        raise RuntimeError('fail after external edit')
            self.assertEqual(path.read_bytes(), external)
            self.assertIn('복구하지 않음(외부 변경)', out.getvalue())

    def test_review2_sync_replaces_only_region_and_uninstall_preserves_comments(self):
        self.fixture()
        self.runtime.parent.mkdir()
        self.runtime.write_text(cx.ROLE_HEADER + '\nmodel="old-model"\n')
        original = ('# before\r\n["agents" . "cx-demo"]\r\nconfig_file="./agents/cx-demo.toml"\r\n'
                    '# keep this comment\r\n[tools]\r\nmode="keep"\r\n')
        self.config.write_bytes(original.encode())
        self.local('roles.json', {'roles': {'cx-demo': self.role, 'second': self.role}})
        self.cli('roles', 'sync')
        text = self.config.read_bytes()
        self.assertTrue(text.startswith(original.encode()))
        self.assertEqual(text.count(b'cx-demo.toml'), 1)
        self.local('roles.json', {'roles': {'cx-demo': self.role, 'third': self.role}})
        self.cli('roles', 'sync')
        text = self.config.read_bytes()
        self.assertTrue(text.startswith(original.encode()))
        self.assertNotIn(b'[agents.second]', text)
        self.assertIn(b'[agents.third]', text)
        self.cli('uninstall')
        self.assertEqual(self.config.read_bytes(), b'# before\r\n# keep this comment\r\n[tools]\r\nmode="keep"\r\n')

    def test_review2_restore_fails_if_removed_file_registration_remains(self):
        self.fixture()
        identifier = self.backup_id()
        self.cli('roles', 'sync')
        with patch.object(cx, 'restore_user_unit', return_value=None), contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                cx.main(['overlay', 'restore', '--id', identifier, '--home', str(self.home)])
        self.assertIn('registration remains for removed role', out.getvalue())

    def test_review2_restore_owned_change_does_not_report_external_change(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        self.config.write_text(self.config.read_text().replace('./agents/cx-demo.toml', './agents/missing.toml'))
        cx.LAST_WRITES.clear()
        output = self.cli('overlay', 'restore', '--id', identifier)
        self.assertNotIn('복구하지 않음', output)

    def test_review2_remove_rechecks_hash_immediately_before_deletion(self):
        self.fixture()
        self.cli('roles', 'sync')
        expected = cx.file_hash(self.runtime)
        read = Path.read_bytes
        reads = 0
        def external_edit(path):
            nonlocal reads
            content = read(path)
            if path.resolve() == self.runtime.resolve():
                reads += 1
                if reads == 2:
                    self.runtime.write_text('external edit before unlink')
            return content
        with patch.object(Path, 'read_bytes', external_edit), self.assertRaises(SystemExit):
            cx.atomic_remove(self.runtime, expected)
        self.assertEqual(self.runtime.read_text(), 'external edit before unlink')

    FOREIGN = '[mcp_servers.newserver]\ncommand = "echo"\n'

    def put_foreign_table_inside_region(self):
        # codex mcp add places a new table before the trailing end-marker comment.
        text = self.config.read_text()
        self.assertTrue(text.rstrip().endswith(cx.ROLES_END))
        self.config.write_text(text.replace(cx.ROLES_END, self.FOREIGN + cx.ROLES_END))

    def assert_foreign_table_survives_sync_and_uninstall(self):
        self.fixture()
        self.cli('roles', 'sync')
        self.put_foreign_table_inside_region()
        self.local('roles.json', {'roles': {'cx-demo': self.role, 'second': self.role}})
        self.cli('roles', 'sync')
        text = self.config.read_text()
        self.assertEqual(text.count(self.FOREIGN), 1)
        self.assertLess(text.index(self.FOREIGN), text.index(cx.ROLES_BEGIN))
        self.assertTrue({'cx-demo', 'second'} <= cx.registered_roles(text))
        self.cli('uninstall')
        text = self.config.read_text()
        self.assertEqual(text.count(self.FOREIGN), 1)
        self.assertNotIn('conductor:roles', text)
        self.assertNotIn('[agents.', text)

    def test_review3_foreign_table_inside_region_survives_sync_and_uninstall(self):
        self.assert_foreign_table_survives_sync_and_uninstall()

    def test_review3_foreign_table_inside_region_survives_without_tomllib(self):
        with patch.object(cx, 'tomllib', None):
            self.assert_foreign_table_survives_sync_and_uninstall()

    def test_review3_restore_keeps_foreign_table_inside_region(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        self.put_foreign_table_inside_region()
        cx.LAST_WRITES.clear()
        self.cli('overlay', 'restore', '--id', identifier)
        text = self.config.read_text()
        self.assertEqual(text.count(self.FOREIGN), 1)
        self.assertIn('cx-demo', cx.registered_roles(text))

    def test_review3_scanner_rejects_nonowned_change(self):
        original = 'model = "m"\n[tools]\nmode = "keep"\n'
        with patch.object(cx, 'tomllib', None), self.assertRaises(SystemExit):
            cx.validate_config_change(original, original.replace('keep', 'lost'), {'cx-demo'})
        with patch.object(cx, 'tomllib', None):
            cx.validate_config_change(original, original + '[agents.cx-demo]\nconfig_file = "x"\n', {'cx-demo'})

    def test_review3_owned_registration_with_user_keys_is_kept(self):
        self.fixture()
        self.cli('roles', 'sync')
        line = 'config_file = "./agents/cx-demo.toml"\n'
        text = self.config.read_text().replace(line, line + 'description = "mine"\n')
        self.config.write_text(text)
        for command in (('roles', 'sync'), ('uninstall',)):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                self.cli(*command)
            self.assertEqual(self.config.read_text(), text)

    def test_review3_agents_array_stops_without_tomllib(self):
        self.fixture()
        original = '[[agents]]\nname = "x"\n'
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(original)
        with patch.object(cx, 'tomllib', None), self.assertRaises(SystemExit):
            self.cli('roles', 'sync')
        self.assertEqual(self.config.read_text(), original)
        self.assertFalse(self.runtime.exists())

    def test_review3_verify_does_not_restore_for_environment_failures(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        notes = cx.state() / 'local/notes.md'
        notes.write_text('written after backup\n')
        with patch.object(cx, 'ocx_path', return_value='/nonexistent/ocx'), \
                patch.object(cx, 'live_models', return_value=None), self.assertRaises(SystemExit) as caught:
            self.cli('overlay', 'verify', '--id', identifier)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(notes.read_text(), 'written after backup\n')

    def test_review3_restore_backs_up_current_state_first(self):
        self.fixture()
        identifier = self.backup_id()
        notes = cx.state() / 'local/notes.md'
        notes.write_text('written after backup\n')
        output = self.cli('overlay', 'restore', '--id', identifier)
        self.assertFalse(notes.exists())
        before = output.split('복구 전 상태 백업: ')[1].split()[0]
        self.assertNotEqual(before, identifier)
        self.assertEqual((cx.state() / 'local/backups' / before / 'notes.md').read_text(), 'written after backup\n')

    def test_review3_entry_remove_deletes_file_install_created(self):
        self.fixture()
        self.assertFalse(self.agents.exists())
        self.cli('entry', 'install')
        self.assertTrue(self.agents.exists())
        self.cli('entry', 'remove')
        self.assertFalse(self.agents.exists())
        self.agents.write_text('# 내 규칙\n')
        self.cli('entry', 'install')
        self.cli('entry', 'remove')
        self.assertEqual(self.agents.read_text(), '# 내 규칙\n')

    def test_review3_interrupt_rolls_back_written_units(self):
        self.fixture()
        self.local('roles.json', {'roles': {'cx-demo': self.role, 'second': self.role}})
        write = cx.atomic_write
        def interrupt(path, *args, **kwargs):
            if Path(path).name == 'second.toml':
                raise KeyboardInterrupt
            return write(path, *args, **kwargs)
        with patch.object(cx, 'atomic_write', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.cli('roles', 'sync')
        self.assertFalse(self.runtime.exists())
        self.assertFalse(self.config.exists())

    def assert_user_tables_inside_region_are_kept(self):
        self.fixture()
        self.cli('roles', 'sync')
        (self.home / '.codex/agents/mine.toml').write_text('name = "mine"\n')
        users = ['[agents.user]\nconfig_file = "./user.toml"\n', '[agents.mine]\nconfig_file = "./agents/mine.toml"\n']
        self.config.write_text(self.config.read_text().replace(cx.ROLES_END, ''.join(users) + cx.ROLES_END))
        for command in (('roles', 'sync'), ('uninstall',)):
            self.cli(*command)
            text = self.config.read_text()
            for user in users:
                self.assertEqual(text.count(user), 1, command)
        self.assertNotIn('conductor:roles', text)

    def test_review4_user_tables_inside_region_are_kept(self):
        self.assert_user_tables_inside_region_are_kept()

    def test_review4_user_tables_inside_region_are_kept_without_tomllib(self):
        with patch.object(cx, 'tomllib', None):
            self.assert_user_tables_inside_region_are_kept()

    def assert_overlapping_marker_stops_restore(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        broken = ('# keep me outside\n[agents.cx-demo]\n' + cx.ROLES_BEGIN
                  + '\nconfig_file = "./agents/cx-demo.toml"\n' + cx.ROLES_END + '\n')
        self.config.write_text(broken)
        cx.LAST_WRITES.clear()
        with self.assertRaises(SystemExit):
            self.cli('overlay', 'restore', '--id', identifier)
        self.assertEqual(self.config.read_text(), broken)

    def test_review4_overlapping_marker_stops_restore(self):
        self.assert_overlapping_marker_stops_restore()

    def test_review4_overlapping_marker_stops_restore_without_tomllib(self):
        with patch.object(cx, 'tomllib', None):
            self.assert_overlapping_marker_stops_restore()

    def test_review4_scanner_rejects_unreadable_candidate(self):
        original = 'model = "m"\n'
        with patch.object(cx, 'tomllib', None), self.assertRaises(SystemExit):
            cx.validate_config_change(original, original + 'roles:begin v1\n', {'cx-demo'})

    def test_review4_verify_restores_changed_proxy_setting(self):
        self.fixture()
        self.cli('roles', 'sync')
        identifier = self.backup_id()
        self.local('settings.json', {'entry': 'none', 'language': 'en', 'proxy': 'not-a-url'})
        with patch.object(cx, 'ocx_path', return_value='/bin/sh'), \
                patch.object(cx, 'live_models', return_value=None), self.assertRaises(SystemExit) as caught:
            self.cli('overlay', 'verify', '--id', identifier)
        self.assertEqual(caught.exception.code, 1)
        self.assertNotIn('proxy', json.loads((cx.state() / 'local/settings.json').read_text()))

    def write_skill(self, relative, name, description):
        path = self.home / relative / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\nname: %s\ndescription: %s\n---\n' % (name, description))

    def test_onboarding_reports_substitute_tools_and_install_advice(self):
        self.fixture()
        source = SCRIPT.parent.parent / 'references' / 'recommended-skills.json'
        (self.refs / 'recommended-skills.json').write_bytes(source.read_bytes())
        self.write_skill('.codex/plugins/cache/vendor/figma/1.0.0/skills/design-review', 'design-review',
                         'Review UI/UX screens against a design system.')
        self.write_skill('.agents/skills/superpowers/systematic-debugging', 'systematic-debugging', 'Debug before fixing.')
        self.write_skill('.agents/skills/playwright', 'playwright', 'Automate a browser for UI-flow debugging.')
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text('[mcp_servers.mobbin]\ncommand = "x"\n')
        with patch.object(cx, 'ocx_path', side_effect=SystemExit('none')), patch.object(cx, 'live_models', return_value=[]):
            detected = cx.onboard_detect(self.home)
        rows = {row['capability']: row for row in detected['capabilities']}
        self.assertEqual(rows['ui-ux']['installed'], [])
        self.assertEqual(rows['ui-ux']['substitutes'], ['figma:design-review'])
        self.assertEqual(rows['ui-ux']['mcp'], ['mobbin'])
        self.assertIn('stylegallery setup', rows['ui-ux']['install']['command'])
        self.assertTrue(any(name.startswith('superpowers:') for name in rows['debug-review']['installed']))
        self.assertEqual(rows['ui-unslop']['substitutes'], [])
        plan = cx.propose_plan(cx.load_routing(), detected)
        self.assertTrue(any(line.startswith('권장 도구 대체 확인 ui-ux: StyleGallery 미설치, 대체 후보 figma:design-review, mcp:mobbin')
                            for line in plan['warnings']))
        self.assertIn('권장 도구 설치 권유 ui-unslop: unslop (https://github.com/heydamir/unslop)', plan['warnings'])
        self.assertFalse(any('debug-review' in line for line in plan['warnings']))

    def assert_inline_agents_convert(self):
        self.fixture()
        self.config.parent.mkdir(parents=True, exist_ok=True)
        original = ('model = "m"\nagents = { mine = { config_file = "./mine.toml" }, other = { config_file = "./o.toml" } } # mine\n'
                    '[tools]\nmode = "keep"\n')
        self.config.write_text(original)
        with self.assertRaises(SystemExit):
            self.cli('roles', 'sync')
        self.assertIn('[agents]', self.cli('config', 'convert-agents', '--dry-run'))
        self.assertEqual(self.config.read_text(), original)
        self.cli('config', 'convert-agents')
        text = self.config.read_text()
        self.assertIn('# mine\n[agents]\nmine = { config_file = "./mine.toml" }\nother = { config_file = "./o.toml" }\n', text)
        self.assertTrue(text.startswith('model = "m"\n[tools]\nmode = "keep"\n'))
        self.cli('roles', 'sync')
        self.assertIn('cx-demo', cx.registered_roles(self.config.read_text()))
        self.cli('uninstall')
        self.assertEqual(self.config.read_text(), text)

    def test_inline_agents_convert_preserves_values(self):
        self.assert_inline_agents_convert()
        if cx.tomllib is not None:
            text = self.config.read_text()
            self.assertEqual(cx.tomllib.loads(text)['agents']['mine'], {'config_file': './mine.toml'})

    def test_inline_agents_convert_preserves_values_without_tomllib(self):
        with patch.object(cx, 'tomllib', None):
            self.assert_inline_agents_convert()

    def test_convert_agents_keeps_comments_inside_items_once(self):
        config = 'agents = { list = [\n# inside\n"a,b"\n], mine = { config_file = "./a,b.toml" } } # outer\n'
        for tomllib in ((cx.tomllib, None) if cx.tomllib is not None else (None,)):
            with self.subTest(tomllib=tomllib is not None), patch.object(cx, 'tomllib', tomllib):
                candidate, _, _ = cx.convert_inline_agents(config)
                self.assertEqual(candidate.count('# inside'), 1)
                self.assertEqual(candidate.count('# outer'), 1)
                self.assertIn('mine = { config_file = "./a,b.toml" }', candidate)
        if cx.tomllib is not None:
            self.assertEqual(cx.tomllib.loads(candidate), cx.tomllib.loads(config))

    def test_convert_agents_refuses_without_inline_table(self):
        self.fixture()
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text('[agents.mine]\nconfig_file = "./mine.toml"\n')
        with self.assertRaises(SystemExit):
            self.cli('config', 'convert-agents')
        self.assertEqual(self.config.read_text(), '[agents.mine]\nconfig_file = "./mine.toml"\n')


if __name__ == '__main__':
    unittest.main()


class SkillsMaintainerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skills-maintainer-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.root / 'state'
        self.skill = self.root / 'package'
        (self.skill / 'references').mkdir(parents=True)
        (self.skill / 'references/roles.json').write_text('{"roles": {}}')
        (self.skill / 'references/routing.json').write_text('{"routes": [], "mcp_usage": {}}')
        for item in (patch.dict(os.environ, {'HOME': str(self.home), 'CONDUCTOR_HOME': str(self.state)}),
                     patch.object(cx, 'SKILL', self.skill)):
            item.start()
            self.addCleanup(item.stop)
        self.real_process = cx.subprocess.run
        process = patch.object(cx.subprocess, 'run')
        self.process = process.start()
        self.addCleanup(process.stop)
        self.process.side_effect = AssertionError('external commands must be mocked explicitly')
        self.config = self.home / '.codex/config.toml'
        self.config.parent.mkdir(parents=True)
        self.skills = self.home / '.codex/skills'
        self.repo = self.root / 'checkout'
        self.head = 'a' * 40
        self.dirty = ''
        self.branch = 'main'
        runner = patch.object(cx, 'run', side_effect=self.git_output)
        runner.start()
        self.addCleanup(runner.stop)
        tests = patch.object(cx, 'repo_tests', return_value=(True, 'fixture tests passed'))
        self.repo_tests = tests.start()
        self.addCleanup(tests.stop)

    def git_output(self, command, **kwargs):
        if command == ['git', '-C', str(self.repo), 'rev-parse', 'HEAD']:
            return self.head + '\n'
        if command == ['git', '-C', str(self.repo), 'rev-parse', '--abbrev-ref', 'HEAD']:
            return self.branch + '\n'
        if command == ['git', '-C', str(self.repo), 'status', '--porcelain']:
            return self.dirty
        raise AssertionError('unexpected command: %r' % command)

    def cli(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cx.main(['--home', str(self.home)] + list(arguments))
        return output.getvalue()

    def skill_file(self, name, root='.agents/skills'):
        path = self.home / root / name / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# ' + name + '\n')
        return path

    def user_config(self, newline='\n'):
        user = self.skill_file('user-disabled')
        text = ('# user settings\nmodel = "keep"\n\n[[skills.config]] # user entry\n'
                'path = %s\nenabled = false # user decision\n\n'
                '[tools]\nmode = "keep" # trailing comment\n') % json.dumps(str(user))
        content = text.replace('\n', newline).encode()
        self.config.write_bytes(content)
        return user, content

    def export_fixture(self, repo, head, target):
        self.assertEqual((repo, head), (self.repo, self.head))
        cx.shutil.copytree(repo, target, dirs_exist_ok=True)
        return target

    def repository(self):
        for name in ('kp-conductor', 'kp-conductor-onboard', 'korean-protocol'):
            folder = self.repo / 'skills' / name
            (folder / '__pycache__').mkdir(parents=True)
            (folder / 'SKILL.md').write_text('new ' + name)
            (folder / '__pycache__/cached.pyc').write_bytes(b'cache')
            (folder / 'module.pyc').write_bytes(b'cache')
        (self.repo / 'skills/kp-conductor/VERSION').write_text('9.8.7\n')
        self.export_patch = patch.object(cx, 'export_head', side_effect=self.export_fixture, create=True)
        self.export = self.export_patch.start()
        self.addCleanup(self.export_patch.stop)
        self.process.side_effect = None
        self.process.return_value = self.deploy_result(stdout='entry: present\n')
        settings = self.state / 'local/settings.json'
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({'maintainer': {'repo': str(self.repo),
                            'hosts': [{'name': 'lab', 'ssh': 'fixture-host'}]}}))

    def engine_record(self, commit=None):
        self.state.mkdir(parents=True, exist_ok=True)
        record = self.state / 'engine.json'
        record.write_text(json.dumps({'commit': commit or self.head, 'hosts': {}}))
        return record

    def snapshot(self, directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob('*') if path.is_file()} if directory.exists() else {}

    def test_skills_off_accumulates_and_last_on_removes_region(self):
        _, original = self.user_config()
        first, second = self.skill_file('first'), self.skill_file('second')
        self.cli('skills', 'off', 'first')
        expected = ('# conductor:skills:begin v1\n[[skills.config]]\npath = %s\nenabled = false\n'
                    '# conductor:skills:end\n') % json.dumps(str(first))
        self.assertEqual(self.config.read_bytes(), original + expected.encode())
        self.cli('skills', 'off', str(second))
        text = self.config.read_text()
        self.assertEqual(text.count(cx.SKILLS_BEGIN), 1)
        self.assertEqual({row[2] for row in cx.owned_skill_offs(text)[1]}, {str(first), str(second)})
        self.assertTrue(self.config.read_bytes().startswith(original))
        self.cli('skills', 'on', str(first))
        self.assertIn(cx.SKILLS_BEGIN, self.config.read_text())
        self.cli('skills', 'on', 'second')
        self.assertEqual(self.config.read_bytes(), original)

    def test_skills_external_off_is_noop_and_on_is_refused(self):
        user, original = self.user_config()
        before = self.config.stat().st_mtime_ns
        self.assertIn('변경 없음', self.cli('skills', 'off', str(user)))
        with self.assertRaisesRegex(SystemExit, 'outside the conductor region'):
            self.cli('skills', 'on', str(user))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.config.stat().st_mtime_ns, before)

    def test_skills_resolves_unique_name_and_explicit_path(self):
        skill = self.skill_file('unique')
        self.assertEqual(cx.resolve_skill_target(self.home, 'unique'), str(skill))
        self.assertEqual(cx.resolve_skill_target(self.home, str(skill)), str(skill))

    def test_skills_rejects_duplicate_and_missing_names(self):
        first = self.skill_file('duplicate')
        second = self.skill_file('duplicate', root='.codex/skills')
        with self.assertRaisesRegex(SystemExit, 'several copies') as error:
            self.cli('skills', 'off', 'duplicate')
        self.assertIn(str(first), str(error.exception))
        self.assertIn(str(second), str(error.exception))
        with self.assertRaisesRegex(SystemExit, 'skill not found'):
            self.cli('skills', 'off', 'missing')
        self.assertFalse(self.config.exists())

    def linked_skill(self):
        target = self.skill_file('actual', root='external')
        link = self.home / '.agents/skills/linked'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target.parent, target_is_directory=True)
        return link / 'SKILL.md', target

    def test_skills_symlink_off_keeps_readable_path_and_alias_off_is_noop(self):
        link, target = self.linked_skill()
        self.cli('skills', 'off', str(link))
        original = self.config.read_bytes()
        self.assertIn(json.dumps(str(link)), original.decode())
        self.assertIn(str(target.resolve()), cx.disabled_skills(self.home)[1])
        self.assertIn('변경 없음', self.cli('skills', 'off', str(target)))
        self.assertEqual(self.config.read_bytes(), original)

    def test_skills_on_via_resolved_symlink_path_removes_owned_entry(self):
        link, target = self.linked_skill()
        self.cli('skills', 'off', str(link))
        self.cli('skills', 'on', str(target))
        self.assertEqual(self.config.read_bytes(), b'')

    def test_skills_uninstall_removes_only_owned_region(self):
        _, original = self.user_config()
        self.skill_file('owned')
        self.cli('skills', 'off', 'owned')
        before = self.config.read_bytes()
        self.cli('uninstall', '--dry-run')
        self.assertEqual(self.config.read_bytes(), before)
        self.cli('uninstall')
        self.assertEqual(self.config.read_bytes(), original)

    def test_skills_crlf_round_trip_preserves_user_bytes(self):
        _, original = self.user_config(newline='\r\n')
        self.skill_file('crlf')
        self.cli('skills', 'off', 'crlf')
        changed = self.config.read_bytes()
        self.assertTrue(changed.startswith(original))
        self.assertNotIn(b'\n', changed.replace(b'\r\n', b''))
        self.cli('skills', 'on', 'crlf')
        self.assertEqual(self.config.read_bytes(), original)

    def test_skills_off_on_without_tomllib(self):
        _, original = self.user_config()
        self.skill_file('fallback')
        with patch.object(cx, 'tomllib', None):
            self.cli('skills', 'off', 'fallback')
            self.assertIn('enabled = false', self.config.read_text())
            self.cli('skills', 'on', 'fallback')
        self.assertEqual(self.config.read_bytes(), original)

    def test_skills_dry_run_list_and_repeated_off_preserve_config(self):
        skill = self.skill_file('dry')
        self.assertEqual(self.cli('skills', 'list').strip(), 'off: none')
        self.assertIn('dry-run', self.cli('skills', 'off', 'dry', '--dry-run'))
        self.assertFalse(self.config.exists())
        self.cli('skills', 'off', 'dry')
        original = self.config.read_bytes()
        before = self.config.stat().st_mtime_ns
        self.assertEqual(self.cli('skills', 'list').strip(), 'off: ' + str(skill))
        self.assertIn('변경 없음', self.cli('skills', 'off', 'dry'))
        self.assertIn('dry-run', self.cli('skills', 'on', 'dry', '--dry-run'))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.config.stat().st_mtime_ns, before)

    def test_maintainer_settings_accept_valid_values(self):
        for value in ({'repo': str(self.repo)}, {'repo': str(self.repo), 'hosts': []},
                      {'repo': str(self.repo), 'branch': 'release'},
                      {'repo': str(self.repo), 'hosts': [{'name': 'lab', 'ssh': 'fixture-host'}]}):
            with self.subTest(value=value):
                settings = {'maintainer': value}
                self.assertEqual(cx.validate_settings(settings), settings)

    def test_maintainer_settings_reject_missing_repo_and_bad_hosts(self):
        for value in ({'repo': str(self.repo), 'branch': 1}, {'repo': str(self.repo), 'branch': ''}, {}, [], {'repo': ''}, {'repo': 1}, {'hosts': []},
                      {'repo': str(self.repo), 'hosts': {}},
                      {'repo': str(self.repo), 'hosts': ['lab']},
                      {'repo': str(self.repo), 'hosts': [{'name': 'lab'}]},
                      {'repo': str(self.repo), 'hosts': [{'name': '', 'ssh': 'fixture-host'}]},
                      {'repo': str(self.repo), 'hosts': [{'name': 'lab', 'ssh': 4}]}):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, 'invalid settings maintainer'):
                cx.validate_settings({'maintainer': value})

    def test_maintainer_dirty_repo_blocks_install_and_deploy_but_status_works(self):
        self.repository()
        self.dirty = ' M skills/kp-conductor/SKILL.md\n'
        for action in ('install', 'deploy'):
            with self.subTest(action=action), self.assertRaisesRegex(SystemExit, 'commit the repository changes first'):
                self.cli('maintainer', action, *(['--host', 'lab'] if action == 'deploy' else []))
        output = self.cli('maintainer', 'status')
        self.assertIn('uncommitted changes', output)
        self.assertIn(self.head[:7], output)
        self.assertIn('installed: none', output)
        self.assertIn('host lab (fixture-host): none', output)
        self.repo_tests.assert_not_called()
        self.process.assert_not_called()
        self.assertFalse(self.skills.exists())

    def test_maintainer_failed_repo_tests_leave_installed_skills_unchanged(self):
        self.repository()
        self.skill_file('kp-conductor', root='.codex/skills')
        original = self.snapshot(self.skills)
        self.repo_tests.return_value = (False, 'fixture test failure')
        with self.assertRaises(SystemExit) as error:
            self.cli('maintainer', 'install')
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(self.repo_tests.call_args.args[0], self.export.call_args.args[2])
        self.assertNotEqual(self.repo_tests.call_args.args[0], self.repo)
        self.assertEqual(self.snapshot(self.skills), original)
        self.assertFalse((self.state / 'engine.json').exists())
        self.assertFalse((self.state / 'backups').exists())

    def test_maintainer_install_copies_engine_backs_up_and_records_commit(self):
        self.repository()
        old = self.skill_file('kp-conductor', root='.codex/skills').read_bytes()
        self.skill_file('kp-conductor-onboard', root='.codex/skills')
        self.cli('maintainer', 'install')
        record = json.loads((self.state / 'engine.json').read_text())
        self.assertEqual(record['commit'], self.head)
        self.assertEqual(record['version'], '9.8.7')
        self.assertEqual(record['folders'], ['kp-conductor', 'kp-conductor-onboard', 'korean-protocol'])
        backup = Path(record['backup'])
        self.assertEqual(backup.parent, self.state / 'backups')
        self.assertTrue(backup.name.startswith('engine-'))
        self.assertEqual((backup / 'kp-conductor/SKILL.md').read_bytes(), old)
        self.assertTrue((backup / 'kp-conductor-onboard/SKILL.md').exists())
        for name in record['folders']:
            self.assertEqual((self.skills / name / 'SKILL.md').read_text(), 'new ' + name)
        self.assertFalse(any(path.name == '__pycache__' or path.suffix == '.pyc' for path in self.skills.rglob('*')))
        self.assertEqual(self.repo_tests.call_args.args[0], self.export.call_args.args[2])
        self.assertNotEqual(self.repo_tests.call_args.args[0], self.repo)

    def test_maintainer_install_skips_separately_installed_korean_protocol(self):
        self.repository()
        protocol = self.skill_file('korean-protocol')
        original = protocol.read_bytes()
        self.cli('maintainer', 'install')
        self.assertFalse((self.skills / 'korean-protocol').exists())
        self.assertEqual(protocol.read_bytes(), original)
        record = json.loads((self.state / 'engine.json').read_text())
        self.assertEqual(record['folders'], ['kp-conductor', 'kp-conductor-onboard'])

    def test_maintainer_install_keeps_a_newer_kp_from_cx_kp_update(self):
        self.repository()
        (self.repo / 'skills/korean-protocol/PINNED.json').write_text(json.dumps({'version': 'v0.11.1'}))
        installed = self.skill_file('korean-protocol', root='.codex/skills')
        (installed.parent / 'PINNED.json').write_text(json.dumps({'version': 'v0.12.0'}))
        before = self.snapshot(installed.parent)
        self.cli('maintainer', 'install')
        self.assertEqual(self.snapshot(installed.parent), before)
        record = json.loads((self.state / 'engine.json').read_text())
        self.assertEqual(record['folders'], ['kp-conductor', 'kp-conductor-onboard'])
        # An older or equal installed copy is replaced by the repository copy as before.
        (installed.parent / 'PINNED.json').write_text(json.dumps({'version': 'v0.10.0'}))
        self.engine_record()
        self.cli('maintainer', 'install')
        self.assertEqual(json.loads((installed.parent / 'PINNED.json').read_text())['version'], 'v0.11.1')
        # Versions compare as numbers: v0.9.0 is older than v0.11.1 although it sorts later as text.
        (installed.parent / 'PINNED.json').write_text(json.dumps({'version': 'v0.9.0'}))
        self.engine_record()
        self.cli('maintainer', 'install')
        self.assertEqual(json.loads((installed.parent / 'PINNED.json').read_text())['version'], 'v0.11.1')
        self.assertEqual(cx.kp_version_key(installed.parent), (0, 11, 1))

    def test_maintainer_install_engine_failure_restores_previous_folders_and_cleans_staging(self):
        self.repository()
        folders = ['kp-conductor', 'kp-conductor-onboard']
        for name in folders:
            self.skill_file(name, root='.codex/skills')
        original = self.snapshot(self.skills)
        copytree = cx.shutil.copytree
        def fail_second(source, target, *arguments, **options):
            if Path(source) == self.repo / 'skills/kp-conductor-onboard':
                Path(target).mkdir()
                (Path(target) / 'partial').write_text('incomplete')
                raise OSError('second folder failed')
            return copytree(source, target, *arguments, **options)
        with patch.object(cx, 'HOME_OVERRIDE', self.home), patch.object(cx.shutil, 'copytree', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'second folder failed'):
                cx.install_engine(self.repo / 'skills', folders, 'fixture')
        self.assertEqual(self.snapshot(self.skills), original)
        self.assertEqual(sorted(path.name for path in self.skills.iterdir()), sorted(folders))

    def test_maintainer_deploy_dry_run_prints_commands_without_subprocess(self):
        self.repository()
        record = self.engine_record()
        original = record.read_bytes()
        lines = self.cli('maintainer', 'deploy', '--host', 'lab', '--dry-run').splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn('mkdir -p ~/.codex/kp-conductor/staging/engine-', lines[0])
        self.assertTrue(lines[1].startswith('rsync -a --exclude __pycache__ '))
        self.assertNotIn('--delete', lines[1])
        self.assertIn('fixture-host:.codex/kp-conductor/staging/engine-', lines[1])
        self.assertIn('maintainer receive --source ~/.codex/kp-conductor/staging/engine-', lines[2])
        self.assertIn('--commit ' + self.head, lines[2])
        self.export.assert_not_called()
        self.process.assert_not_called()
        self.assertEqual(record.read_bytes(), original)

    def test_maintainer_deploy_rejects_unknown_host(self):
        self.repository()
        self.engine_record()
        with self.assertRaisesRegex(SystemExit, 'unknown host'):
            self.cli('maintainer', 'deploy', '--host', 'unknown')
        self.process.assert_not_called()

    def test_maintainer_deploy_rejects_commit_not_installed(self):
        self.repository()
        record = self.engine_record('b' * 40)
        original = record.read_bytes()
        with self.assertRaisesRegex(SystemExit, 'install this commit here first'):
            self.cli('maintainer', 'deploy', '--host', 'lab')
        self.process.assert_not_called()
        self.assertEqual(record.read_bytes(), original)

    def deploy_result(self, returncode=0, stdout='', stderr=''):
        return cx.subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_maintainer_deploy_success_records_host_commit(self):
        self.repository()
        record = self.engine_record()
        self.process.side_effect = None
        self.process.return_value = self.deploy_result(stdout='WARN environment checks failed after install, engine kept: proxy\n')
        output = self.cli('maintainer', 'deploy', '--host', 'lab')
        self.assertIn('remote: WARN environment checks failed after install, engine kept: proxy', output)
        self.assertEqual(json.loads(record.read_text())['hosts'], {'lab': self.head})
        self.assertEqual([call.args[0][0] for call in self.process.call_args_list], ['ssh', 'rsync', 'ssh'])
        self.assertIn('maintainer receive', self.process.call_args_list[-1].args[0][-1])
        self.assertNotIn('--delete', self.process.call_args_list[1].args[0])

    def test_maintainer_deploy_rsync_failure_does_not_record_host(self):
        self.repository()
        record = self.engine_record()
        original = record.read_bytes()
        self.process.side_effect = [self.deploy_result(), self.deploy_result(23, stderr='copy failed')]
        with self.assertRaisesRegex(SystemExit, 'deploy failed at rsync: copy failed'):
            self.cli('maintainer', 'deploy', '--host', 'lab')
        self.assertEqual(self.process.call_count, 2)
        self.assertEqual(record.read_bytes(), original)

    def test_maintainer_deploy_remote_receive_failure_does_not_record_host(self):
        self.repository()
        record = self.engine_record()
        original = record.read_bytes()
        self.process.side_effect = [self.deploy_result()] * 2 + [self.deploy_result(1, stderr='FAIL fixture\n')]
        with self.assertRaisesRegex(SystemExit, 'deploy failed'):
            self.cli('maintainer', 'deploy', '--host', 'lab')
        self.assertEqual(record.read_bytes(), original)

    def test_skills_no_final_newline_round_trip_preserves_bytes(self):
        for original in (b'model = "keep"', b'# comment', b'model = "keep"\r\n# end'):
            with self.subTest(original=original):
                self.config.write_bytes(original)
                self.skill_file('no-newline')
                self.skill_file('another')
                self.cli('skills', 'off', 'no-newline')
                self.assertIn(b'# conductor:skills:begin v1 +newline', self.config.read_bytes())
                self.cli('skills', 'off', 'another')
                self.cli('skills', 'on', 'no-newline')
                self.cli('skills', 'on', 'another')
                self.assertEqual(self.config.read_bytes(), original)

    def test_export_head_omits_ignored_private_files(self):
        self.repo.mkdir()
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=tester@localhost', '-C', str(self.repo)]
        def command(*args):
            return self.real_process(git + list(args), check=True, capture_output=True, text=True).stdout.strip()
        command('init', '-q', '-b', 'main')
        (self.repo / '.gitignore').write_text('plan.json\n')
        (self.repo / 'tracked').write_text('committed')
        (self.repo / 'plan.json').write_text('private')
        command('add', '.')
        tree = command('write-tree')
        head = command('commit-tree', tree, '-m', 'fixture')
        command('update-ref', 'refs/heads/main', head)
        (self.repo / 'tracked').write_text('uncommitted')
        with patch.object(cx.subprocess, 'run', self.real_process):
            target = cx.export_head(self.repo, head, self.root / 'export')
        self.assertEqual((target / 'tracked').read_text(), 'committed')
        self.assertFalse((target / 'plan.json').exists())
        self.assertFalse((target / '.git').exists())

    def test_maintainer_non_main_branch_blocks_install_and_deploy(self):
        self.repository()
        self.engine_record()
        self.branch = 'unreviewed'
        self.assertIn('branch: unreviewed', self.cli('maintainer', 'status'))
        for action in ('install', 'deploy'):
            with self.subTest(action=action), self.assertRaisesRegex(SystemExit, 'branch.*main'):
                self.cli('maintainer', action, *(['--host', 'lab'] if action == 'deploy' else []))
        self.export.assert_not_called()
        self.process.assert_not_called()

    def test_maintainer_configured_branch_allows_install(self):
        self.repository()
        path = self.state / 'local/settings.json'
        settings = json.loads(path.read_text())
        settings['maintainer']['branch'] = 'release'
        path.write_text(json.dumps(settings))
        self.branch = 'release'
        self.cli('maintainer', 'install', '--dry-run')
        self.export.assert_not_called()

    def receive(self):
        return self.cli('maintainer', 'receive', '--source', str(self.repo / 'skills'), '--commit', self.head)

    def test_receive_outdated_entry_is_installed_but_present_is_not(self):
        self.repository()
        for status in ('outdated', 'present'):
            with self.subTest(status=status):
                self.process.reset_mock()
                if status == 'outdated':
                    self.process.side_effect = [self.deploy_result(stdout='entry: outdated'), self.deploy_result(), self.deploy_result()]
                else:
                    self.process.side_effect = [self.deploy_result(stdout='entry: present'), self.deploy_result()]
                with patch.object(cx, 'now', return_value=cx.datetime.datetime(2026, 1, 1, microsecond=1 if status == 'outdated' else 2)):
                    self.receive()
                commands = [call.args[0] for call in self.process.call_args_list]
                self.assertEqual(sum(command[-2:] == ['entry', 'install'] for command in commands), status == 'outdated')
                self.assertEqual(commands[-1][-1], 'doctor')
                for call in self.process.call_args_list:
                    self.assertEqual(call.args[0][0], cx.sys.executable)
                    self.assertIn('--home', call.args[0])
                    self.assertEqual(call.kwargs['env']['HOME'], str(self.home))
                    self.assertGreater(call.kwargs['timeout'], 0)

    def test_receive_doctor_failure_restores_folders_and_entry(self):
        self.repository()
        self.skill_file('kp-conductor', root='.codex/skills')
        original = self.snapshot(self.skills)
        agents = self.agents_with_old_block()
        before = agents.read_bytes()
        self.process.side_effect = self.entry_rewriting([self.deploy_result(stdout='entry: outdated'), self.deploy_result(),
                                                         self.deploy_result(1, stdout='PASS other\nFAIL fixture\n')])
        with self.assertRaisesRegex(SystemExit, 'FAIL fixture'):
            self.receive()
        self.assertEqual(self.snapshot(self.skills), original)
        self.assertFalse((self.state / 'engine.json').exists())
        commands = [call.args[0] for call in self.process.call_args_list]
        self.assertEqual([command[-2:] for command in commands].count(['entry', 'install']), 1)
        self.assertEqual(agents.read_bytes(), before)
        self.assertTrue(list((self.state / 'backups').glob('engine-*/new-kp-conductor/SKILL.md')))

    def agents_with_old_block(self):
        agents = self.home / '.codex/AGENTS.md'
        agents.write_text('# mine\n<!-- conductor:begin v1 -->\nolder wording\n<!-- conductor:end -->\n')
        return agents

    def entry_rewriting(self, results):
        results = iter(results)
        def run(command, **options):
            if command[-2:] == ['entry', 'install']:
                (self.home / '.codex/AGENTS.md').write_text('rewritten by the new engine\n')
            return next(results)
        return run

    def test_receive_unreadable_agents_stops_before_any_folder_moves(self):
        self.repository()
        self.skill_file('kp-conductor', root='.codex/skills')
        original = self.snapshot(self.skills)
        agents = self.agents_with_old_block()
        reader = Path.read_bytes
        def unreadable(path):
            if path.name == 'AGENTS.md':
                raise PermissionError('AGENTS.md unreadable')
            return reader(path)
        with patch.object(Path, 'read_bytes', unreadable), self.assertRaisesRegex(SystemExit, 'nothing changed'):
            self.receive()
        self.assertEqual(self.snapshot(self.skills), original)
        self.process.assert_not_called()
        self.assertTrue(agents.exists())

    def test_receive_doctor_crash_rolls_back_even_after_environment_failures(self):
        self.repository()
        self.process.side_effect = [self.deploy_result(stdout='entry: present'),
                                    self.deploy_result(1, stdout='FAIL proxy: unreachable\n',
                                                       stderr='Traceback (most recent call last):\nAttributeError: x\n')]
        with self.assertRaisesRegex(SystemExit, 'doctor did not finish'):
            self.receive()
        self.assertFalse((self.state / 'engine.json').exists())

    def test_rollback_continues_after_one_folder_fails(self):
        self.repository()
        for name in ('kp-conductor', 'kp-conductor-onboard'):
            self.skill_file(name, root='.codex/skills')
        original = self.snapshot(self.skills)
        self.process.side_effect = [self.deploy_result(stdout='entry: present'), self.deploy_result(1, stdout='FAIL fixture: x\n')]
        rename = cx.os.rename
        def flaky(source, target):
            if str(target).endswith('new-kp-conductor'):
                raise OSError('busy')
            return rename(source, target)
        with patch.object(cx.os, 'rename', side_effect=flaky), contextlib.redirect_stdout(io.StringIO()) as output, \
                self.assertRaises(SystemExit):
            cx.main(['--home', str(self.home), 'maintainer', 'receive', '--source', str(self.repo / 'skills'), '--commit', self.head])
        self.assertIn('engine rollback incomplete', output.getvalue())
        self.assertEqual((self.skills / 'kp-conductor-onboard/SKILL.md').read_bytes(), original['kp-conductor-onboard/SKILL.md'])

    def test_receive_environment_only_doctor_failure_keeps_engine(self):
        self.repository()
        self.process.side_effect = [self.deploy_result(stdout='entry: present'),
                                    self.deploy_result(1, stdout='FAIL proxy: unreachable\nFAIL routed models in catalog: missing x\n')]
        output = self.receive()
        self.assertIn('environment checks failed after install, engine kept', output)
        self.assertEqual(json.loads((self.state / 'engine.json').read_text())['commit'], self.head)
        self.assertEqual((self.skills / 'kp-conductor/SKILL.md').read_text(), 'new kp-conductor')

    def test_receive_absent_entry_on_fresh_machine_keeps_engine(self):
        self.repository()
        self.process.side_effect = [self.deploy_result(stdout='entry: absent'),
                                    self.deploy_result(1, stdout='FAIL AGENTS.md pointer: absent\n')]
        self.assertIn('engine kept: AGENTS.md pointer', self.receive())
        self.assertEqual(json.loads((self.state / 'engine.json').read_text())['commit'], self.head)
        self.process.side_effect = [self.deploy_result(stdout='entry: present'),
                                    self.deploy_result(1, stdout='FAIL AGENTS.md pointer: absent\n')]
        with patch.object(cx, 'now', return_value=cx.datetime.datetime(2026, 1, 2)), self.assertRaisesRegex(SystemExit, 'AGENTS.md'):
            self.receive()

    def test_receive_engine_failure_with_proxy_down_still_rolls_back(self):
        self.repository()
        self.process.side_effect = [self.deploy_result(stdout='entry: present'),
                                    self.deploy_result(1, stdout='FAIL proxy: unreachable\nFAIL roles.json: invalid\n')]
        with self.assertRaisesRegex(SystemExit, 'roles.json'):
            self.receive()
        self.assertFalse((self.state / 'engine.json').exists())

    def test_receive_record_write_failure_rolls_back_engine_and_entry(self):
        self.repository()
        self.skill_file('kp-conductor', root='.codex/skills')
        original = self.snapshot(self.skills)
        agents = self.agents_with_old_block()
        before = agents.read_bytes()
        self.process.side_effect = self.entry_rewriting([self.deploy_result(stdout='entry: outdated'), self.deploy_result(),
                                                         self.deploy_result()])
        write = cx.atomic_write
        def fail_record(path, *args, **kwargs):
            if Path(path).name == 'engine.json':
                raise OSError('record write failed')
            return write(path, *args, **kwargs)
        with patch.object(cx, 'atomic_write', side_effect=fail_record), self.assertRaisesRegex(OSError, 'record write failed'):
            self.receive()
        self.assertEqual(self.snapshot(self.skills), original)
        self.assertFalse((self.state / 'engine.json').exists())
        self.assertEqual(self.process.call_args_list[-1].args[0][-1], 'doctor')
        self.assertEqual(agents.read_bytes(), before)

    def test_receive_rejects_engine_symlink_without_touching_target(self):
        self.repository()
        target = self.root / 'external'
        target.mkdir()
        (target / 'keep').write_text('untouched')
        self.skills.mkdir()
        (self.skills / 'kp-conductor').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, 'symlink'):
            self.receive()
        self.assertEqual((target / 'keep').read_text(), 'untouched')
        self.assertTrue((self.skills / 'kp-conductor').is_symlink())
        self.process.assert_not_called()

    def test_receive_needs_no_maintainer_settings_and_cleans_only_staging(self):
        self.repository()
        (self.state / 'local/settings.json').write_text('{}')
        source = self.state / 'staging' / 'engine-fixture'
        cx.shutil.copytree(self.repo / 'skills', source)
        self.cli('maintainer', 'receive', '--source', str(source))
        self.assertFalse(source.exists())
        self.assertTrue((self.repo / 'skills').exists())
        self.assertIsNone(json.loads((self.state / 'engine.json').read_text())['commit'])

    def test_receive_is_guarded_by_mutation_lock(self):
        self.repository()
        with patch.object(cx, 'mutation_lock', wraps=cx.mutation_lock) as lock:
            self.receive()
        lock.assert_called_once()

    def test_maintainer_deploy_each_stage_failure_leaves_hosts_unchanged(self):
        self.repository()
        record = self.engine_record()
        original = record.read_bytes()
        for index, stage in enumerate(('staging', 'rsync', 'receive')):
            with self.subTest(stage=stage):
                self.process.reset_mock()
                self.process.side_effect = [self.deploy_result()] * index + [self.deploy_result(1, stderr='stage failed')]
                with self.assertRaisesRegex(SystemExit, 'deploy failed at ' + stage + ': stage failed'):
                    self.cli('maintainer', 'deploy', '--host', 'lab')
                self.assertEqual(self.process.call_count, index + 1)
                self.assertEqual(record.read_bytes(), original)

    def test_maintainer_deploy_timeout_leaves_hosts_unchanged(self):
        self.repository()
        record = self.engine_record()
        original = record.read_bytes()
        self.process.side_effect = cx.subprocess.TimeoutExpired(['ssh'], 600, stderr='timed out')
        with self.assertRaisesRegex(SystemExit, 'deploy failed at staging: timed out'):
            self.cli('maintainer', 'deploy', '--host', 'lab')
        self.assertEqual(record.read_bytes(), original)

    def test_skills_added_newline_is_removed_before_later_user_content(self):
        original = b'model = "keep"'
        self.config.write_bytes(original)
        self.skill_file('no-newline')
        self.cli('skills', 'off', 'no-newline')
        with self.config.open('ab') as stream:
            stream.write(b'\n# later user comment')
        self.cli('skills', 'on', 'no-newline')
        self.assertEqual(self.config.read_bytes(), original + b'\n# later user comment')

    def test_receive_dry_run_does_not_replace_engine(self):
        self.repository()
        self.cli('maintainer', 'receive', '--source', str(self.repo / 'skills'), '--dry-run')
        self.assertFalse(self.skills.exists())
        self.process.assert_not_called()
        self.assertFalse((self.state / 'engine.json').exists())

    def test_maintainer_deploy_export_failure_leaves_hosts_unchanged(self):
        self.repository()
        record = self.engine_record()
        original = record.read_bytes()
        self.export.side_effect = SystemExit('git archive failed: fixture')
        with self.assertRaisesRegex(SystemExit, 'deploy failed at export: git archive failed: fixture'):
            self.cli('maintainer', 'deploy', '--host', 'lab')
        self.process.assert_not_called()
        self.assertEqual(record.read_bytes(), original)

    def test_receive_rollback_entry_timeout_keeps_original_failure(self):
        self.repository()
        self.skill_file('kp-conductor', root='.codex/skills')
        original = self.snapshot(self.skills)
        self.process.side_effect = [self.deploy_result(stdout='entry: outdated'), self.deploy_result(),
                                    self.deploy_result(1, stdout='FAIL original'),
                                    cx.subprocess.TimeoutExpired(['entry'], 120)]
        with self.assertRaisesRegex(SystemExit, 'FAIL original'):
            self.receive()
        self.assertEqual(self.snapshot(self.skills), original)
        self.assertFalse((self.state / 'engine.json').exists())

    def test_receive_entry_handler_uses_installed_script_without_nested_lock(self):
        import runpy
        self.repository()
        self.skill_file('kp-conductor', root='.codex/skills')
        script = self.skills / 'kp-conductor/scripts/conductor.py'
        with patch.object(cx, 'HOME_OVERRIDE', self.home):
            cx.engine_command('entry', 'install')
        command = self.process.call_args.args[0]
        self.assertEqual(command[1], '-c')
        def handler(args):
            self.assertEqual(args.action, 'install')
            self.assertFalse(args.dry_run)
            self.assertEqual(handler.__globals__['HOME_OVERRIDE'], self.home)
        # Execute only the subprocess bootstrap; the installed handler is mocked.
        with patch.object(runpy, 'run_path', return_value={'command_entry': handler}) as loader, \
                patch.object(cx.sys, 'argv', ['-c'] + command[3:]), \
                patch.dict(handler.__globals__, {'HOME_OVERRIDE': None}):
            exec(command[2], {})
        loader.assert_called_once_with(str(script))

    def test_receive_home_override_is_forwarded_when_home_environment_differs(self):
        self.repository()
        with patch.dict(os.environ, {'HOME': str(self.root / 'other-home')}):
            self.receive()
        for call in self.process.call_args_list:
            self.assertEqual(call.kwargs['env']['HOME'], str(self.home))
            command = call.args[0]
            self.assertEqual(command[command.index('--home') + 1], str(self.home))
