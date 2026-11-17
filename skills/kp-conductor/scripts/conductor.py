#!/usr/bin/env python3
"""Conductor helper: status, checkpoints, receipts, profiles, retrospective, lineup, doctor."""
import argparse
import contextlib
import fcntl
import stat
import tempfile
import csv
import datetime
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.9 and 3.10 use conservative lexical checks.
    tomllib = None

SKILL = Path(__file__).resolve().parent.parent
SESSIONS = Path.home() / '.codex' / 'sessions'
USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')
SKILL_PATH = re.compile(r'skills/(?:\.system/)?([A-Za-z0-9_.:/-]+)/SKILL\.md')
MCP_CALL = re.compile(r'^mcp__(.+?)__')
MCP_EXEC = re.compile(r'tools\.mcp__(.+?)__')
EXPLICIT = re.compile(r'\[\$([A-Za-z0-9_.:-]+)\]\(')
AUDIT_MARKER = '[conductor-audit]'
ROUTE_REMINDER = 'reminder: 이 세션에서 ~/.codex/skills/kp-conductor/SKILL.md를 아직 읽지 않았으면 분류하기 전에 처음부터 끝까지 읽는다'
FENCE = chr(96) * 3
AA_FIELDS = ('model', 'effort', 'intelligence_index', 'cost_per_task_usd', 'time_per_task_min',
             'output_tokens_per_task', 'finance_accounting', 'strategy_ops', 'legal', 'healthcare',
             'engineering', 'economics')
UNSET = object()


def state():
    return Path(os.environ.get('CONDUCTOR_HOME') or user_home() / '.codex' / 'kp-conductor')


def engine_version():
    # VERSION sits beside SKILL.md and is installed with the engine (RELEASING.md).
    try:
        return (Path(__file__).resolve().parent.parent / 'VERSION').read_text(encoding='utf-8').strip() or 'unknown'
    except OSError:
        return 'unknown'


# The entry skill never evaluates or hands itself to subagents; onboarding is also engine-owned.
ENGINE_SKILL = 'kp-conductor'
ENGINE_SKILLS = (ENGINE_SKILL, 'kp-conductor-onboard')



def lexical_path(path):
    # macOS exposes these OS directories as aliases. Normalize only these roots;
    # never resolve a user-controlled component before checking it.
    path = Path(os.path.abspath(str(path)))
    if sys.platform == 'darwin' and path.parts[1:2] in (('var',), ('tmp',)):
        alias = Path('/') / path.parts[1]
        if alias.is_symlink() and alias.resolve() == Path('/private') / path.parts[1]:
            path = Path('/private').joinpath(*path.parts[1:])
    return path


def owned_path(path):
    path = lexical_path(path)
    for item in (*reversed(path.parents), path):
        if item.is_symlink():
            raise SystemExit('refusing to write through symlink: ' + str(item))
    return path


def user_file(path):
    path = Path(path)
    actual = path.resolve()
    if path.is_symlink() or lexical_path(path) != actual:
        print('resolved target: %s -> %s' % (path, actual))
    return actual


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None


ACTIVE_BACKUP = None
LAST_WRITES = {}
ACTIVE_CHANGES = None


def record_change(path, before, mode, after):
    key = str(path.resolve())
    LAST_WRITES[key] = after
    if ACTIVE_CHANGES is not None:
        first = ACTIVE_CHANGES.get(key, (before, mode, after))
        ACTIVE_CHANGES[key] = (first[0], first[1], after)


def atomic_write(path, content, expected=UNSET, mode=None):
    path = Path(path)
    owned_path(path)
    if isinstance(content, str):
        content = content.encode('utf-8')
    initial = file_hash(path) if expected is UNSET else expected
    before = path.read_bytes() if path.exists() else None
    permissions = stat.S_IMODE(path.stat().st_mode) if path.exists() else (mode if mode is not None else 0o600)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), permissions)
        owned_path(path)
        if file_hash(path) != initial:
            raise SystemExit('다른 작업이 파일을 바꿨다: ' + str(path))
        os.replace(temporary, path)
        record_change(path, before, permissions, hashlib.sha256(content).hexdigest())
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_remove(path, expected=UNSET):
    path = owned_path(path)
    initial = file_hash(path) if expected is UNSET else expected
    if file_hash(path) != initial:
        raise SystemExit('다른 작업이 파일을 바꿨다: ' + str(path))
    before = path.read_bytes() if path.exists() else None
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    if before is not None:
        owned_path(path)
        if file_hash(path) != initial:
            raise SystemExit('다른 작업이 파일을 바꿨다: ' + str(path))
        path.unlink()
        record_change(path, before, mode, None)


def rollback_changes(changes):
    """Undo this command only; a concurrent writer always takes precedence."""
    failed = []
    if not changes:
        print('변경 없음: rollback')
    for key, (before, mode, expected) in reversed(list(changes.items())):
        try:
            path = owned_path(key)
            if file_hash(path) != expected:
                print('복구하지 않음(외부 변경): ' + key)
                failed.append(key)
                continue
            current = path.read_bytes() if path.exists() else None
            if current == before:
                print('변경 없음: ' + key)
            elif before is None:
                atomic_remove(path, expected)
            else:
                atomic_write(path, before, expected, mode=mode)
        except (Exception, SystemExit) as error:
            failed.append(key)
            print('복구하지 못함: %s: %s' % (key, error))
    if failed:
        print('rollback incomplete: ' + ', '.join(failed))


@contextlib.contextmanager
def mutation_lock():
    path = owned_path(state() / '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def change_units(extra=None):
    """A command takes one snapshot; internal helpers reuse it without locking."""
    global ACTIVE_BACKUP, ACTIVE_CHANGES
    if ACTIVE_BACKUP is not None:
        yield ACTIVE_BACKUP
        return
    backup = overlay_backup(extra)
    ACTIVE_BACKUP = backup
    ACTIVE_CHANGES = {}
    try:
        yield backup
    except BaseException:
        # Ctrl-C must not leave half of a command's writes behind.
        changes = ACTIVE_CHANGES
        ACTIVE_CHANGES = None
        rollback_changes(changes)
        raise
    finally:
        ACTIVE_BACKUP = None
        ACTIVE_CHANGES = None

def now():
    return datetime.datetime.now().astimezone()


def project_slug(project):
    path = Path(project).expanduser().resolve()
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', path.name).strip('-') or 'root'
    return name[:40] + '-' + hashlib.sha1(str(path).encode()).hexdigest()[:8]


def run(args, timeout=20):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    return result.stdout if result.returncode == 0 else None


def normalize_mode(value):
    aliases = {'속도': '속도', '균형': '균형', '프론티어': '프론티어',
               '빠르게': '속도', '꼼꼼하게': '프론티어', '아껴서': '균형'}
    if value not in aliases:
        raise SystemExit('unknown mode: %s' % value)
    return aliases[value], value == '아껴서'


# ---- personal configuration and quota --------------------------------------

HOME_OVERRIDE = None
DEFAULT_SETTINGS = {
    'version': 1, 'orchestrator': None, 'language': 'ko', 'entry': 'agents_md',
    'retro': {'apply': 'propose'}, 'proxy': 'http://127.0.0.1:10100',
    'thresholds': {'no_expand': 70, 'restrict': 80, 'stop': 95, 'orchestrator_notice': 85,
                   'builder_one': 70, 'builder_stop': 90, 'burn_off': 0.7, 'burn_down': 1.0},
    'onboarding': None, 'banned_skills': [],
}
OVERLAY_FILES = ('routing.json', 'roles.json', 'tiers.json', 'settings.json', 'notes.md')
ENTRY_BEGIN = '<!-- conductor:begin v1 -->'
ENTRY_END = '<!-- conductor:end -->'
ENTRY_PATTERN = re.compile(r'<!-- conductor:begin v[^\r\n]*? -->\r?\n(.*?)<!-- conductor:end -->(?:\r?\n)?', re.S)


def user_home():
    return HOME_OVERRIDE if HOME_OVERRIDE is not None else Path.home()


def expand_home(value):
    return str(user_home() / value[2:]) if value.startswith('~/') else value


def overlay_path(name):
    local = state() / 'local' / name
    return local if local.exists() else SKILL / 'references' / name


def json_object(path):
    data = read_json(path)
    if not isinstance(data, dict):
        raise SystemExit('invalid JSON object: ' + str(path))
    return data


def merge_settings(base, override):
    result = dict(base)
    for key, value in override.items():
        result[key] = merge_settings(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return result


def validate_settings(data):
    if not isinstance(data, dict) or data.get('version', 1) != 1:
        raise SystemExit('invalid settings version')
    for key in ('orchestrator', 'ocx'):
        if key in data and data[key] is not None and (not isinstance(data[key], str) or not data[key]):
            raise SystemExit('invalid settings: ' + key)
    for key in ('proxy', 'language'):
        if key in data and (not isinstance(data[key], str) or not data[key]):
            raise SystemExit('invalid settings: ' + key)
    if data.get('entry', 'agents_md') not in ('agents_md', 'none'):
        raise SystemExit('invalid settings entry')
    if not isinstance(data.get('retro', {}), dict) or data.get('retro', {}).get('apply', 'propose') not in ('propose', 'notify'):
        raise SystemExit('invalid settings retro.apply')
    thresholds = data.get('thresholds', {})
    if not isinstance(thresholds, dict) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < float('inf') for v in thresholds.values()):
        raise SystemExit('invalid settings thresholds')
    banned = data.get('banned_skills', [])
    if not isinstance(banned, list) or not all(isinstance(pattern, str) for pattern in banned):
        raise SystemExit('invalid settings banned_skills')
    quota = data.get('quota', [])
    if not isinstance(quota, list):
        raise SystemExit('invalid settings quota')
    keys = set()
    for row in quota:
        if (not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ('key', 'provider', 'window'))
                or row['key'] in keys or not isinstance(row.get('models', []), list)
                or not all(isinstance(pattern, str) for pattern in row.get('models', []))):
            raise SystemExit('invalid quota key')
        overrides = row.get('thresholds', {})
        if (not isinstance(overrides, dict) or any(name not in DEFAULT_SETTINGS['thresholds'] for name in overrides)
                or any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0)
                       for value in overrides.values())):
            raise SystemExit('invalid quota thresholds: ' + row['key'])
        substitutes = row.get('substitutes', [])
        if not isinstance(substitutes, list) or not all(isinstance(item, str) and item.strip() for item in substitutes):
            raise SystemExit('invalid quota substitutes: ' + row['key'])
        keys.add(row['key'])
    parents = {row['key']: row.get('parent') for row in quota}
    for key in parents:
        seen = set()
        current = key
        while current is not None:
            if current in seen or current not in parents:
                raise SystemExit('invalid quota parent: ' + key)
            seen.add(current)
            current = parents[current]
    if data.get('onboarding') is not None and not isinstance(data['onboarding'], dict):
        raise SystemExit('invalid settings onboarding')
    maintainer = data.get('maintainer')
    if maintainer is not None:
        hosts = maintainer.get('hosts', []) if isinstance(maintainer, dict) else None
        if (not isinstance(maintainer, dict) or not isinstance(maintainer.get('repo'), str) or not maintainer['repo']
                or not isinstance(maintainer.get('branch', 'main'), str) or not maintainer.get('branch', 'main')
                or not isinstance(hosts, list)
                or not all(isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k] for k in ('name', 'ssh'))
                           for row in hosts)):
            raise SystemExit('invalid settings maintainer')
    return data


def load_settings():
    path = overlay_path('settings.json')
    data = json_object(path) if path.exists() else {}
    return merge_settings(DEFAULT_SETTINGS, validate_settings(data))


def load_tiers():
    package = SKILL / 'references' / 'tiers.json'
    base = json_object(package) if package.exists() else {}
    local = state() / 'local' / 'tiers.json'
    if local.exists():
        for key, value in json_object(local).items():
            base[key] = dict(base.get(key, {}), **value) if isinstance(value, dict) and isinstance(base.get(key, {}), dict) else value
    validate_tiers(base)
    return base



EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
SANDBOXES = (None, 'read-only', 'workspace-write', 'danger-full-access')


def validate_tiers(data):
    models, seats, constraints = data.get('models', {}), data.get('seats', {}), data.get('constraints', [])
    if (not isinstance(models, dict) or not isinstance(seats, dict) or not isinstance(constraints, list)
            or any(not isinstance(name, str) or not isinstance(row, dict) for name, row in models.items())):
        raise SystemExit('invalid tiers models, seats or constraints')
    for name, candidates in seats.items():
        if (not isinstance(name, str) or not isinstance(candidates, list)
                or any(not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
                       or pair[0] not in models or pair[1] not in EFFORTS for pair in candidates)):
            raise SystemExit('invalid tiers seat: ' + str(name))
    for rule in constraints:
        if (not isinstance(rule, dict) or not isinstance(rule.get('role'), str)
                or not isinstance(rule.get('differs_from'), list)
                or not all(isinstance(name, str) for name in rule['differs_from'])):
            raise SystemExit('invalid tiers constraint')
    return data

def ocx_path():
    return expand_home(load_settings().get('ocx') or shutil.which('ocx') or str(user_home() / '.local/bin/ocx'))


def judge_call_count(data):
    value = data.get('judge_calls', data.get('fable_calls'))
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def model_provider(model):
    return model.split('/', 1)[0] if '/' in model else 'openai'


def quota_definitions(quota):
    """Settings win. Otherwise each provider's weekly window becomes a key, and every other plain-word
    window (for example a model-specific 'Fable' window) becomes a child key for models whose name contains it."""
    settings = load_settings()
    if 'quota' in settings:
        return settings['quota']
    rows = []
    for provider, windows in quota.items():
        if 'weekly' in windows:
            rows.append({'key': provider, 'provider': provider, 'window': 'weekly', 'label': provider, 'derived': 'provider'})
    names = [label.lower() for windows in quota.values() for label in windows
             if label != 'weekly' and re.fullmatch(r'[A-Za-z][A-Za-z0-9.-]*', label)]
    for provider, windows in quota.items():
        for label in windows:
            if label == 'weekly' or not re.fullmatch(r'[A-Za-z][A-Za-z0-9.-]*', label):
                continue
            key = label.lower() if names.count(label.lower()) == 1 else '%s-%s' % (provider, label.lower())
            row = {'key': key, 'provider': provider, 'window': label, 'label': '%s %s' % (provider, label),
                   'derived': label.lower()}
            if 'weekly' in windows:
                row['parent'] = provider
            rows.append(row)
    return rows


def model_quota_keys(model, quota):
    definitions = quota_definitions(quota)
    if 'quota' in load_settings():
        return [row['key'] for row in definitions
                if any(fnmatch.fnmatchcase(model, pattern) for pattern in row.get('models', []))]
    provider = model_provider(model)
    name = model.split('/', 1)[-1].lower()
    return [row['key'] for row in definitions if row['provider'] == provider
            and (row['derived'] == 'provider' or row['derived'] in name)]


def with_parents(keys, quota):
    """A model also spends every parent window of its keys."""
    parents = {row['key']: row.get('parent') for row in quota_definitions(quota)}
    found = set()
    for key in keys:
        while key is not None and key not in found:
            found.add(key)
            key = parents.get(key)
    return found


def parse_quota(text):
    quota = {}
    for line in (text or '').splitlines():
        windows = {label: float(percent) for label, percent in re.findall(r'(\S+) (\d+(?:\.\d+)?)% resets', line)}
        if windows:
            quota[line.split()[0]] = windows
    return quota


def quota_burn(text, at=None):
    at = now() if at is None else at
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.timezone.utc)
    burn = {}
    week = 7 * 86400
    for line in (text or '').splitlines():
        for label, percent, reset in re.findall(r'(\S+) (\d+(?:\.\d+)?)% resets (\S+)', line):
            reset_stamp = timestamp_seconds(reset)
            if reset_stamp is None:
                continue
            elapsed = max((at.timestamp() - (reset_stamp - week)) / week, 1 / 168)
            burn.setdefault(line.split()[0], {})[label] = float(percent) / 100 / elapsed
    return burn


def quota_advice(quota, burn=None):
    if not quota:
        return ['한도 조회 실패: 배정표를 그대로 쓰되 구현 작업자는 2개까지 띄운다. 확대 자리를 끄고 추론 프론티어에 들어가지 않는다']
    advice = []
    settings = load_settings()
    builder = load_roles().get('cx-build', {}).get('model', '')
    builder_keys = model_quota_keys(builder, quota) if builder else []
    orchestrator = settings.get('orchestrator')
    main_keys = model_quota_keys(orchestrator, quota) if orchestrator else []
    definitions = quota_definitions(quota)
    stopped = {row['key'] for row in definitions
               if reached(quota.get(row['provider'], {}).get(row['window']), row_limits(row)['stop'])}
    for row in definitions:
        key, provider, window = row['key'], row['provider'], row['window']
        label = row.get('label', key)
        limits = row_limits(row)
        p = quota.get(provider, {}).get(window)
        b = (burn or {}).get(provider, {}).get(window)
        if key in builder_keys and p is not None:
            if reached(p, limits['builder_stop']):
                advice.append('%s 한도 %g%%: 서브에이전트를 띄우지 않고 오케스트레이터가 직접 처리한다' % (label, p))
            elif reached(p, limits['builder_one']):
                advice.append('%s 한도 %g%%: 구현 작업자는 1개, 경쟁 실행은 하지 않는다' % (label, p))
        if p is not None:
            if reached(p, limits['stop']):
                # A substitute is offered only if none of its own quota keys is stopped too.
                usable = [item for item in row.get('substitutes', [])
                          if not with_parents(model_quota_keys(item.split()[0], quota), quota) & stopped]
                advice.append('%s 한도 %g%%: 이 키 모델을 배정하지 않고 같은 역할의 대체 순서로 넘긴다' % (label, p)
                              + ('. 대체: ' + ', '.join(usable) if usable else ''))
            elif reached(p, limits['restrict']):
                advice.append('%s 한도 %g%%: 이 키 모델은 유형 E 본분석과 R3 검토에만 쓴다' % (label, p))
            elif reached(p, limits['no_expand']):
                advice.append('%s 한도 %g%%: 이 키 모델의 확대 자리를 끄고 추론 프론티어에 들어가지 않는다' % (label, p))
            if key in main_keys and reached(p, limits['orchestrator_notice']):
                advice.append('%s 한도 %g%%: 메인 세션도 이 한도를 쓰므로 사용자에게 알린다' % (label, p))
        if p is None or b is None:
            advice.append('%s 한도 미확인: 이 한도 모델의 확대 자리를 끄고 추론 프론티어에 들어가지 않는다. 구현 작업자는 2개까지 띄운다' % label)
        elif limits['burn_down'] is not None and b > limits['burn_down']:
            advice.append('%s 소진 속도 %.2f: 이 한도 모델의 확대 자리를 끄고, 사용자가 말한 프론티어도 이 한도 모델의 자리를 균형 배정으로 낮춘다. 추론 프론티어에 들어가지 않는다' % (label, b))
        elif reached(b, limits['burn_off']):
            advice.append('%s 소진 속도 %.2f: 이 한도 모델의 확대 자리를 끄고 추론 프론티어에 들어가지 않는다' % (label, b))
    return advice


def row_limits(row):
    """Global thresholds with this key's overrides; None switches a threshold off for the key."""
    return dict(load_settings()['thresholds'], **(row.get('thresholds') or {}))


def reached(value, limit):
    return value is not None and limit is not None and value >= limit


def expansion_state(quota, burn):
    definitions = {row['key']: row for row in quota_definitions(quota)}

    def enabled(key, seen):
        if key not in definitions or key in seen:
            return False
        row = definitions[key]
        limits = row_limits(row)
        p = quota.get(row['provider'], {}).get(row['window'])
        b = (burn or {}).get(row['provider'], {}).get(row['window'])
        # Any active percentage limit closes expansion, even if no_expand is switched off for the key.
        closed = any(reached(p, limits[name]) for name in ('no_expand', 'restrict', 'stop'))
        fast = reached(b, limits['burn_off']) or limits['burn_down'] is not None and b is not None and b > limits['burn_down']
        return (p is not None and b is not None and not closed and not fast
                and (not row.get('parent') or enabled(row['parent'], seen | {key})))

    return {key: 'on' if enabled(key, set()) else 'off' for key in definitions}


# ---- checkpoints, receipts, profiles ---------------------------------------

def runs_dir(project):
    return state() / 'runs' / project_slug(project)


def load_checkpoint(project, identifier=None):
    directory = runs_dir(project)
    for path in sorted(directory.glob('*.json'), reverse=True) if directory.exists() else []:
        data = json.loads(path.read_text())
        if identifier and data['id'] != identifier:
            continue
        if identifier or data['status'] == 'open':
            return path, data
    return None, None


def progress_line(data):
    total = len(data['steps'])
    done = max(0, min(int(data['done']), total))
    percent = round(100 * done / total) if total else 100
    label = data['steps'][done - 1] if done else '시작'
    return '진행 %d%% (%d/%d) — %s' % (percent, done, total, label)


def checkpoint_start(project, title, steps, task_type=None, mode=None, mode_source=None, visual=False):
    if mode is None or mode_source is None:
        raise SystemExit('checkpoint start requires --mode and --mode-source (예: --mode 균형 --mode-source default)')
    mode, saving = normalize_mode(mode)
    if mode_source not in ('user', 'inferred', 'default'):
        raise SystemExit('mode-source must be user, inferred or default')
    steps = [step.strip() for step in steps.split('|') if step.strip()]
    if not steps:
        raise SystemExit('steps are required, separated by |')
    stamp = now()
    data = {'id': stamp.strftime('%Y%m%d-%H%M%S'), 'project': str(Path(project).expanduser().resolve()),
            'title': title, 'steps': steps, 'done': 0, 'status': 'open', 'outcome': None,
            'started': stamp.isoformat(timespec='seconds'), 'updated': stamp.isoformat(timespec='seconds'), 'notes': []}
    if task_type is not None:
        data['type'] = task_type
    data.update(mode=mode, mode_source=mode_source, saving=saving)
    if visual:
        data['visual'] = {'iterations': 0, 'defects': None, 'accepted': None}
    directory = runs_dir(project)
    atomic_write(directory / (data['id'] + '.json'), json.dumps(data, ensure_ascii=False, indent=1))
    ensure_profile(project)
    return data


def checkpoint_update(project, identifier, done, note=None, outcome=None, delegated=None,
                      skills=None, skill_fit=None, fable_calls=None, review_changed=None,
                      visual_iterations=None, visual_defects=None, visual_accepted=UNSET, judge_calls=None):
    if delegated is not None and delegated < 0:
        raise SystemExit('delegated must be nonnegative')
    judge_calls = judge_calls if judge_calls is not None else fable_calls
    for name, value in [('judge_calls', judge_calls), ('visual_iterations', visual_iterations),
                        ('visual_defects', visual_defects)]:
        if value is not None and value < 0:
            raise SystemExit('%s must be nonnegative' % name)
    path, data = load_checkpoint(project, identifier)
    expected = file_hash(owned_path(path)) if path is not None else None
    if not data:
        raise SystemExit('no open checkpoint for this project')
    if done is not None:
        data['done'] = max(0, min(int(done), len(data['steps'])))
    if note:
        data['notes'].append(note)
    if outcome:
        data['status'], data['outcome'] = 'done', outcome
        if outcome == 'pass':
            data['done'] = len(data['steps'])
    if delegated is not None:
        data['delegated'] = delegated
    if judge_calls is not None:
        data.pop('fable_calls', None)
        data['judge_calls'] = judge_calls
    if review_changed is not None:
        data['review_changed'] = review_changed
    visual_values = {'iterations': visual_iterations, 'defects': visual_defects}
    if any(value is not None for value in visual_values.values()) or visual_accepted is not UNSET:
        visual = data.setdefault('visual', {'iterations': 0, 'defects': None, 'accepted': None})
        visual.update({name: value for name, value in visual_values.items() if value is not None})
        if visual_accepted is not UNSET:
            visual['accepted'] = visual_accepted
    if skills is not None:
        values = skills.split(',') if isinstance(skills, str) else skills
        data['skills'] = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if skills is not None or skill_fit is not None:
        data['skill_fit'] = skill_fit
    data['updated'] = now().isoformat(timespec='seconds')
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1), expected)
    if outcome and data.get('skills'):
        append_receipt({'ts': data['updated'], 'project': data['project'], 'checkpoint': data['id'],
                        'type': data.get('type'), 'skills': data['skills'],
                        'fit': data.get('skill_fit'), 'outcome': outcome}, 'skill-receipts.jsonl')
    return data


def append_receipt(record, filename='receipts.jsonl'):
    path = owned_path(state() / filename)
    expected = file_hash(path)
    original = path.read_bytes() if path.exists() else b''
    atomic_write(path, original + (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8'), expected)


def read_receipts(days, filename='receipts.jsonl'):
    path = state() / filename
    until = now().timestamp()
    since = until - days * 86400
    rows = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        stamp = timestamp_seconds(row.get('ts')) if isinstance(row, dict) else None
        if stamp is not None and since <= stamp <= until:
            rows.append(row)
    return rows


def skill_performance(days):
    """Use the last appended receipt for each identified project/checkpoint pair."""
    latest = {}
    for index, row in enumerate(read_receipts(days, 'skill-receipts.jsonl')):
        project, checkpoint = row.get('project'), row.get('checkpoint')
        key = ('checkpoint', project, checkpoint) if project is not None and checkpoint is not None else ('legacy', index)
        latest[key] = row
    performance = {}
    for row in latest.values():
        fit = row.get('fit')
        fit = fit if fit in ('good', 'poor', 'none') else 'unrecorded'
        for name in set(row.get('skills', [])):
            entry = performance.setdefault(name, dict.fromkeys(('good', 'poor', 'none', 'unrecorded'), 0))
            entry[fit] += 1
    return performance


def profile_path(project):
    return state() / 'profiles' / (project_slug(project) + '.json')


def read_profile(project):
    path = profile_path(project)
    if not path.exists():
        return None
    profile = json.loads(path.read_text())
    if profile.get('by') == 'fable':
        profile['by'] = 'judge'
    profile['mode'], saving = normalize_mode(profile.get('mode', '균형'))
    profile['saving'] = saving or bool(profile.get('saving', False))
    return profile


def ensure_profile(project):
    path = profile_path(project)
    if path.exists():
        return False
    profile = {'project': str(Path(project).expanduser().resolve()), 'mode': '균형', 'saving': False, 'risk': 'R2',
               'domain': 'engineering', 'verify': '', 'note': '자동 생성, 회고가 영수증 5건 이상에서 조정',
               'by': 'auto', 'updated': now().isoformat(timespec='seconds')}
    atomic_write(path, json.dumps(profile, ensure_ascii=False, indent=1))
    return True


# ---- usage analysis --------------------------------------------------------

def skill_usage(days, sessions=None, include_mcp=False, skill_paths=()):
    """Count agent reads and user calls; optionally include MCP calls per server."""
    root = Path(sessions) if sessions else (user_home() / '.codex' / 'sessions' if HOME_OVERRIDE is not None else SESSIONS)
    cutoff = time.time() - days * 86400
    since = (now() - datetime.timedelta(days=days)).astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    reads, explicit, mcp = {}, {}, {}
    aliases = {}
    for name, path in skill_paths:
        for value in (str(path), str(Path(path).resolve())):
            aliases[value] = name
            for marker in ('/.codex/', '/.agents/'):
                if marker in value:
                    aliases['~' + marker + value.split(marker, 1)[1]] = name
    totals = (reads, explicit, mcp)
    for path in root.rglob('*.jsonl') if root.exists() else []:
        if path.stat().st_mtime < cutoff:
            continue
        # Evaluation sessions (Council, rebuttal) read skills to judge them; they are not usage.
        reads, explicit, mcp = {}, {}, {}
        audit_session = False
        with path.open(errors='replace') as handle:
            for line in handle:
                if 'SKILL.md' not in line and '$' not in line and 'mcp__' not in line and AUDIT_MARKER not in line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if str(item.get('timestamp', '')) < since:
                    continue
                payload = item.get('payload') or {}
                kind = payload.get('type')
                if kind in ('function_call', 'custom_tool_call'):
                    body = str(payload.get('arguments') or payload.get('input') or '')
                    names = set()
                    for match in SKILL_PATH.finditer(body):
                        prefix = body[:match.end()]
                        name = next((name for path, name in aliases.items() if prefix.endswith(path)),
                                    match.group(1).replace('/', ':'))
                        names.add(name)
                    for name in names:
                        reads[name] = reads.get(name, 0) + 1
                    tool = payload.get('name', '')
                    direct = MCP_CALL.match(tool)
                    servers = [direct.group(1)] if direct else []
                    if tool in ('exec', 'custom_exec'):
                        servers += MCP_EXEC.findall(str(payload.get('input') or ''))
                    for server in servers:
                        mcp[server] = mcp.get(server, 0) + 1
                elif kind == 'message' and payload.get('role') == 'user':
                    body = json.dumps(payload.get('content'), ensure_ascii=False)
                    if AUDIT_MARKER in body:
                        audit_session = True
                        break
                    names = set(EXPLICIT.findall(body)) | set(re.findall(r'(?<![\w$])\$([A-Za-z0-9_.:-]+)', body))
                    for name in names - {'app-name'}:
                        explicit[name] = explicit.get(name, 0) + 1
        if not audit_session:
            for total, part in zip(totals, (reads, explicit, mcp)):
                for name, count in part.items():
                    total[name] = total.get(name, 0) + count
    reads, explicit, mcp = totals
    return (reads, explicit, mcp) if include_mcp else (reads, explicit)


def usage_models(days):
    text = run([ocx_path(), 'usage', '--range', '%dd' % days, '--top', '15'], 30)
    if not text:
        return None
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith('MODEL ')), None)
    if start is None:
        return None
    block = []
    for line in lines[start:]:
        if not line.strip():
            break
        block.append(line.rstrip())
    return block


def timestamp_seconds(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value / 1000 if value > 100000000000 else value
        stamp = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return stamp.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def opencodex_logs():
    text = run([ocx_path(), 'logs', '--limit', '2000', '--json'], 30)
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        return None
    if isinstance(payload, dict):
        payload = payload.get('logs', payload.get('data'))
        if isinstance(payload, dict):
            payload = payload.get('logs')
    if not isinstance(payload, list):
        return None
    return payload


def opencodex_delegations(days, logs=UNSET):
    payload = opencodex_logs() if logs is UNSET else logs
    if payload is None:
        return None
    records, main = [], {}
    undated = 0
    for row in payload:
        if not isinstance(row, dict) or not row.get('model'):
            continue
        stamp = timestamp_seconds(row.get('timestamp'))
        if stamp is None:
            undated += 1
            continue
        records.append((stamp, row))
        conversation = row.get('conversationId')
        if conversation and (conversation not in main or stamp < main[conversation][0]):
            main[conversation] = (stamp, row['model'])
    groups, uncorrelated = {}, 0
    cutoff = time.time() - days * 86400
    for stamp, row in records:
        if stamp < cutoff:
            continue
        conversation = row.get('conversationId')
        role = ('미확인' if not conversation else '메인' if row['model'] == main[conversation][1] else '서브에이전트 요청')
        uncorrelated += not bool(conversation)
        try:
            status = '2xx' if 200 <= int(row.get('status', 0)) < 300 else '기타'
        except (ValueError, TypeError):
            status = '기타'
        effort = row.get('requestedEffort') or row.get('effort') or row.get('effectiveEffort') or row.get('reasoning_effort') or '미확인'
        key = (role, row['model'], effort, status)
        groups[key] = groups.get(key, 0) + 1
    return {'groups': groups, 'uncorrelated': uncorrelated, 'undated': undated}


def opencodex_errors(days, logs=UNSET):
    payload = opencodex_logs() if logs is UNSET else logs
    if payload is None:
        return None
    cutoff = time.time() - days * 86400
    groups = {}
    for row in payload:
        if not isinstance(row, dict) or not row.get('model'):
            continue
        stamp = timestamp_seconds(row.get('timestamp'))
        if stamp is None or stamp < cutoff:
            continue
        try:
            status = int(row.get('status', 0))
        except (ValueError, TypeError):
            status = row.get('status') or '미기록'
        if isinstance(status, int) and 200 <= status < 300:
            continue
        code = next((attempt['errorCode'] for attempt in (row.get('attempts') or [])
                     if isinstance(attempt, dict) and attempt.get('errorCode') is not None), '미기록')
        key = (row['model'], str(status), str(code))
        entry = groups.setdefault(key, {'count': 0, 'first': stamp, 'last': stamp})
        entry['count'] += 1
        entry['first'], entry['last'] = min(entry['first'], stamp), max(entry['last'], stamp)
    return groups


def sample_requests(days, limit, sessions=None):
    root = Path(sessions) if sessions is not None else SESSIONS
    cutoff = time.time() - days * 86400
    samples = {}
    for path in root.rglob('*.jsonl') if root.is_dir() else []:
        if path.stat().st_mtime < cutoff:
            continue
        with path.open(errors='replace') as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                payload = item.get('payload') or {}
                stamp = timestamp_seconds(item.get('timestamp'))
                if stamp is None or stamp < cutoff:
                    continue
                if payload.get('type') == 'message' and payload.get('role') == 'user':
                    content = payload.get('content', '')
                    if isinstance(content, str):
                        body = content
                    elif isinstance(content, list):
                        body = '\n'.join(part['text'] for part in content if isinstance(part, dict) and isinstance(part.get('text'), str))
                    else:
                        body = ''
                elif payload.get('type') == 'user_message':
                    body = payload.get('message', '')
                else:
                    continue
                if not isinstance(body, str):
                    continue
                cleaned = body.lstrip().lstrip('#').lstrip()
                if not cleaned or cleaned.startswith(('AGENTS.md instructions', '<')):
                    continue
                key = ' '.join(body.split())
                if key not in samples or stamp > samples[key][0]:
                    samples[key] = (stamp, {'timestamp': item['timestamp'], 'text': body[:300], 'session': str(path)})
    return [row for _, row in sorted(samples.values(), key=lambda item: item[0], reverse=True)[:limit]]


def retro_report(days, sessions=None):
    receipts = read_receipts(days)
    groups = {}
    for row in receipts:
        key = (row.get('type', '?'), row.get('model', '?'), row.get('effort', '?'), row.get('run', 'single'))
        entry = groups.setdefault(key, [0, 0, 0])
        entry[0] += 1
        entry[1 if row.get('outcome') == 'pass' else 2] += 1
    out = ['# Conductor 회고 %s (최근 %d일)' % (now().strftime('%Y-%m-%d'), days), '', '## 위임 기록']
    if groups:
        out += ['| 유형 | 모델 | 강도 | 방식 | 건수 | 성공 | 실패 |', '|---|---|---|---|---|---|---|']
        out += ['| %s | %s | %s | %s | %d | %d | %d |' % (key + tuple(value)) for key, value in sorted(groups.items())]
    else:
        out.append('기록 없음.')
    performance = skill_performance(days)
    out += ['', '## 스킬 성과']
    if performance:
        out += ['| 스킬 | 사용 수 | good | poor | none | 미기록 |', '|---|---|---|---|---|---|']
        out += ['| %s | %d | %d | %d | %d | %d |' % ((name, sum(values.values())) +
                 tuple(values[key] for key in ('good', 'poor', 'none', 'unrecorded')))
                for name, values in sorted(performance.items())]
    else:
        out.append('기록 없음.')
    logs = opencodex_logs()
    delegated = opencodex_delegations(days, logs)
    out += ['', '## OpenCodex 로그 기준 위임']
    if delegated is None:
        out.append('미확인: ocx logs 조회 또는 JSON 해석 실패.')
    else:
        out += ['최대 2,000개 관측 요청 기준이며, conversationId별 가장 이른 관측 요청 모델을 메인으로 구분한다.',
                '| 구분 | 모델 | 강도 | HTTP 상태 | 요청 수 |', '|---|---|---|---|---|']
        out += ['| %s | %s | %s | %s | %d |' % (key + (count,)) for key, count in sorted(delegated['groups'].items())]
        out.append('conversationId 미확인 %d건, 시각 미확인으로 기간 집계 제외 %d건. 2xx는 HTTP 상태이며 작업 성공 판정이 아니다.'
                   % (delegated['uncorrelated'], delegated['undated']))
    errors = opencodex_errors(days, logs)
    out += ['', '## OpenCodex 오류 원인']
    if errors is None:
        out.append('미확인: ocx logs 조회 또는 JSON 해석 실패.')
    elif errors:
        out += ['| 모델 | 상태 | 오류 코드 | 건수 | 최초 | 최후 |', '|---|---|---|---|---|---|']
        for key, entry in sorted(errors.items()):
            times = tuple(datetime.datetime.fromtimestamp(entry[name]).astimezone().strftime('%m-%d %H:%M')
                          for name in ('first', 'last'))
            out.append('| %s | %s | %s | %d | %s | %s |' % (key + (entry['count'],) + times))
    else:
        out.append('기록 없음.')
    failures = {}
    for row in receipts:
        counts = failures.setdefault(row.get('model', '?'), dict.fromkeys(('fail', 'refused', 'error', 'unclassified_refused'), 0))
        outcome = row.get('outcome')
        if outcome == 'refused' and row.get('classified') is not True:
            outcome = 'unclassified_refused'
        if outcome in counts:
            counts[outcome] += 1
    out += ['', '## 실패 원인']
    if failures:
        out += ['| 모델 | fail | refused | error | 미분류 refused |', '|---|---|---|---|---|']
        out += ['| %s | %d | %d | %d | %d |' % ((model,) + tuple(counts[name] for name in ('fail', 'refused', 'error', 'unclassified_refused')))
                for model, counts in sorted(failures.items())]
    else:
        out.append('기록 없음.')
    outcomes = {}
    open_count = 0
    cutoff = time.time() - days * 86400
    classifications, unclassified = {}, 0
    checkpoints = []
    for path in (state() / 'runs').glob('*/*.json') if (state() / 'runs').exists() else []:
        data = json.loads(path.read_text())
        stamp = timestamp_seconds(data.get('updated'))
        if stamp is None or stamp < cutoff:
            continue
        checkpoints.append(data)
        if not data.get('type'):
            unclassified += 1
        else:
            entry = classifications.setdefault(data['type'], {'count': 0, 'delegated': [], 'outcomes': {}})
            entry['count'] += 1
            if isinstance(data.get('delegated'), int) and not isinstance(data['delegated'], bool):
                entry['delegated'].append(data['delegated'])
            outcome = data.get('outcome') or data.get('status', '미확인')
            entry['outcomes'][outcome] = entry['outcomes'].get(outcome, 0) + 1
        if data['status'] == 'open':
            open_count += 1
        else:
            outcomes[data.get('outcome')] = outcomes.get(data.get('outcome'), 0) + 1
    judge = load_roles().get('cx-hard', {}).get('model') or (load_settings().get('onboarding') or {}).get('judge_model')
    refused = failures.get(judge, {}).get('refused', 0)
    calls = sum(judge_call_count(data) for data in checkpoints if judge_call_count(data) is not None)
    summary = '판단 역할 내용 거절 %d건(분류된 refused), 판단 역할 호출 %d회(체크포인트 judge_calls 합)' % (refused, calls)
    out.append(summary)
    out.append('judge: model=%s calls=%d refused=%d' % (judge or 'unassigned', calls, refused))
    if refused >= 3:
        out.append('판단 역할 내용 거절 점검: 거절된 지시문을 SKILL.md 판단 역할 운용 규칙과 비교')
    out += ['', '## 체크포인트', '완료 %s, 미완료 %d' % (json.dumps(outcomes, ensure_ascii=False), open_count)]
    out += ['', '## 분류', '| 유형 | 건수 | delegated 평균 | outcome 분포 |', '|---|---|---|---|']
    for task_type, entry in sorted(classifications.items()):
        values = entry['delegated']
        average = '%.2f' % (sum(values) / len(values)) if values else '미기록'
        out.append('| %s | %d | %s | %s |' % (task_type, entry['count'], average, json.dumps(entry['outcomes'], ensure_ascii=False, sort_keys=True)))
    out += ['type 미기록 %d개.' % unclassified, 'delegated 평균은 값이 기록된 체크포인트만 계산한다. 기간은 updated 기준이다.']
    out += checkpoint_mode_report(checkpoints, days)
    models = usage_models(days)
    out += ['', '## 모델 사용량 (ocx usage)'] + ([FENCE] + models + [FENCE] if models else ['조회 실패.'])
    reads, explicit, mcp = skill_usage(days, sessions, include_mcp=True)
    names = sorted((set(reads) | set(explicit)) - set(ENGINE_SKILLS),
                   key=lambda name: (-explicit.get(name, 0), -reads.get(name, 0), name))
    out += ['', '## 스킬 사용', '| 스킬 | 사용자가 직접 호출 | 에이전트가 읽음 |', '|---|---|---|']
    out += ['| %s | %d | %d |' % (name, explicit.get(name, 0), reads.get(name, 0)) for name in names[:25]]
    out += ['', '## MCP 사용', '| 서버 | 호출 수 |', '|---|---|']
    out += ['| %s | %d |' % item for item in sorted(mcp.items(), key=lambda item: (-item[1], item[0]))]
    quota = parse_quota(run([ocx_path(), 'provider', 'quota'], 20))
    out += ['', '## 한도', json.dumps(quota, ensure_ascii=False) if quota else '조회 실패.']
    return '\n'.join(out) + '\n'


def checkpoint_mode_report(checkpoints, days):
    modes, frontier, visuals, calls = {}, [], [], []
    for data in checkpoints:
        mode = normalize_mode(data['mode'])[0] if data.get('mode') else '미기록'
        key = (mode, data.get('mode_source') or '미기록')
        modes[key] = modes.get(key, 0) + 1
        if mode == '프론티어':
            frontier.append(data)
        if isinstance(data.get('visual'), dict):
            visuals.append(data['visual'])
        if judge_call_count(data) is not None:
            calls.append(judge_call_count(data))
    out = ['', '## 모드', '| 모드 | 출처 | 건수 |', '|---|---|---|']
    out += ['| %s | %s | %d |' % (key + (count,)) for key, count in sorted(modes.items())]
    if days >= 28:
        empty = [mode for mode in ('속도', '균형', '프론티어') if not any(key[0] == mode for key in modes)]
        if empty:
            out.append('0건 모드: ' + ', '.join(empty))
    if checkpoints and len(frontier) / len(checkpoints) > 0.5:
        out.append('프론티어 비율 %.0f%%: 추론 조건 축소 검토' % (100 * len(frontier) / len(checkpoints)))
    iterations = [row['iterations'] for row in visuals if isinstance(row.get('iterations'), int)]
    defects = [row['defects'] for row in visuals if isinstance(row.get('defects'), int)]
    average = '%.2f' % (sum(iterations) / len(iterations)) if iterations else '미기록'
    out.append('비주얼 트랙 %d건, iterations 평균 %s, defects 합 %s' %
               (len(visuals), average, str(sum(defects)) if defects else '미기록'))
    out.append('accepted 분포: yes %d, no %d, unknown %d' %
               (sum(row.get('accepted') is True for row in visuals),
                sum(row.get('accepted') is False for row in visuals),
                sum(row.get('accepted') is None for row in visuals)))
    reviews = [row['review_changed'] for row in frontier if isinstance(row.get('review_changed'), bool)]
    changed = sum(reviews)
    ratio = 100 * changed / len(reviews) if reviews else None
    out += ['', '## 두 번째 검토의 효과', 'review_changed 기록 %d건, true %d건, 비율 %s' %
            (len(reviews), changed, '%.0f%%' % ratio if ratio is not None else '미기록')]
    if len(reviews) >= 8 and ratio < 10:
        out.append('제안: 프론티어 2회 검토를 1회로 줄이는 안을 사용자에게 제안')
    out += ['', '## 판단 역할 호출', 'judge_calls 기록 %d건, 합계 %d회, 최대 %d회' %
            (len(calls), sum(calls), max(calls) if calls else 0)]
    return out


# ---- installed skill audit ------------------------------------------------

def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def requirement_groups(requires):
    if isinstance(requires, list) and requires and all(isinstance(item, list) for item in requires):
        return requires
    if isinstance(requires, list) and requires and all(isinstance(item, str) for item in requires):
        return [requires]
    return []


def route_gate_open(route, matched):
    found = set(matched)
    if found & set(route.get('sufficient', [])):
        return True
    return all(found & set(group) for group in requirement_groups(route.get('requires', [])))


def load_routing(path=None, data=None):
    path = Path(path) if path is not None else overlay_path('routing.json')
    data = read_json(path) if data is None else data

    def invalid():
        raise SystemExit('invalid routing definitions: ' + str(path))

    def strings(value):
        return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)

    if (not isinstance(data, dict) or not isinstance(data.get('mcp_usage'), dict)
            or not isinstance(data.get('routes'), list)):
        invalid()
    for pattern, servers in data['mcp_usage'].items():
        if (not pattern or '*' in pattern.rstrip('*') or pattern.count('*') > 1
                or not strings(servers)):
            invalid()
    identifiers = set()
    for route in data['routes']:
        if (not isinstance(route, dict)
                or not all(isinstance(route.get(key), str) and route[key].strip()
                           for key in ('id', 'label', 'role', 'risk'))
                or not isinstance(route.get('note'), str)
                or not strings(route.get('keywords')) or not strings(route.get('skills'))
                or route['id'] in identifiers):
            invalid()
        # Optional gate. requires is a list of keywords (any one must match) or a list of keyword groups
        # (every group needs one match). A matched sufficient keyword turns the route on by itself.
        if 'requires' in route:
            groups = requirement_groups(route['requires'])
            if not groups or not all(group and strings(group) and set(group) <= set(route['keywords']) for group in groups):
                invalid()
        if 'sufficient' in route and (not strings(route['sufficient']) or not route['sufficient']
                                      or not set(route['sufficient']) <= set(route['keywords'])):
            invalid()
        identifiers.add(route['id'])
    return data


def route_matches(text, routing, roles, skills, top=3, disabled=None):
    if top < 1:
        raise SystemExit('top must be positive')
    disabled_names, disabled_paths = disabled if disabled is not None else (set(), set())
    paths, installed = {}, set()
    for name, path in skills:
        installed.add(name)
        if (name not in disabled_names and str(Path(path).resolve()) not in disabled_paths
                and str(Path(path).parent.resolve()) not in disabled_paths):
            paths.setdefault(name, path)
    results = []
    for route in routing['routes']:
        matched = []
        for keyword in dict.fromkeys(route['keywords']):
            if keyword.isascii():
                found = re.search(r'(?<![A-Za-z0-9])' + re.escape(keyword) + r'(?![A-Za-z0-9])',
                                  text, re.I | re.ASCII)
            else:
                pattern = re.escape(keyword.lower())
                if re.match(r'[A-Za-z0-9]', keyword[0]):
                    pattern = r'(?<![A-Za-z0-9])' + pattern
                if re.match(r'[A-Za-z0-9]', keyword[-1]):
                    pattern += r'(?![A-Za-z0-9])'
                found = re.search(pattern, text.lower(), re.ASCII)
            if found:
                matched.append(keyword)
        if not matched or not route_gate_open(route, matched):
            continue
        allowed = roles.get(route['role'], {}).get('skills', [])
        resolved = []
        for name in route['skills']:
            off = name in disabled_names or name in installed and name not in paths
            entry = {'name': name, 'path': '비활성' if off else paths.get(name, '미설치'),
                     'allowed': name in allowed and not off}
            if off:
                entry['disabled'] = True
            resolved.append(entry)
        results.append({**route, 'keywords': matched, 'score': len(matched), 'skills': resolved})
    return sorted(results, key=lambda row: -row['score'])[:top]


def command_route(args):
    home = user_home()
    skills = role_skills(home)
    installed_names = {name for name, _ in skills}
    results = route_matches(args.text, load_routing(), load_roles(), skills, args.top, disabled_skills(home))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    elif not results:
        print('route: none')
    else:
        for row in results:
            print('%s (%s) [%s] %s %s — %s' % (row['label'], row['id'], ', '.join(row['keywords']),
                                                   row['role'], row['risk'], row['note']))
            for skill in row['skills']:
                print('  %s: %s (allowed=%s)' % (skill['name'], skill['path'], str(skill['allowed']).lower()))
            for pack in missing_recommended(row['id'], installed_names):
                install = pack.get('install') or {}
                print('  recommended (미설치): %s — 설치: `%s`. 설치 전에는 다른 도구로 진행하고, 최종 보고에 미설치를 한 줄로 적는다.'
                      % (pack.get('name'), install.get('command') or install.get('how') or pack.get('source')))
    if not args.json:
        print(ROUTE_REMINDER)


def missing_recommended(route_id, installed_names):
    return [pack for pack in recommended_packs()
            if route_id in (pack.get('routes') or []) and not any(pack_owns(pack, name) for name in installed_names)]


def pack_owns(pack, name):
    """A pack's skill may arrive bare, with its own prefix, or inside a plugin namespace (plugin:skill)."""
    own, prefix = set(pack.get('skills') or []), pack.get('prefix')
    return name in own or name.split(':')[-1] in own or bool(prefix) and name.startswith(prefix)


def disabled_skills(home):
    """Read the skills.config subset on Python versions without tomllib too."""
    try:
        text = (home / '.codex' / 'config.toml').read_text()
    except OSError:
        return set(), set()
    names, paths = set(), set()
    for block in re.split(r'^\s*\[\[skills\.config\]\]\s*(?:#.*)?$', text, flags=re.M)[1:]:
        block = re.split(r'^\s*\[', block, maxsplit=1, flags=re.M)[0]
        if not re.search(r'^\s*enabled\s*=\s*false\s*(?:#.*)?$', block, re.M):
            continue
        for key, value in re.findall(r'''^\s*(name|path)\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')''', block, re.M):
            value = json.loads(value) if value.startswith('"') else value[1:-1]
            if key == 'name':
                names.add(value)
            else:
                paths.add(str(Path(value).expanduser().resolve()))
    return names, paths


def installed_skill_dirs(root):
    """Follow skill-pack links while avoiding loops and skill-internal examples."""
    def visit(directory, parts, source, ancestors):
        resolved = directory.resolve()
        if resolved in ancestors or not directory.is_dir():
            return
        if directory.is_symlink():
            source = str(resolved)
        if (directory / 'SKILL.md').is_file():
            yield ':'.join(parts), directory, source
            return
        for child in sorted(directory.iterdir()):
            if not child.name.startswith('.'):
                yield from visit(child, parts + [child.name], source, ancestors | {resolved})

    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.name.startswith('.'):
                yield from visit(child, [child.name], None, set())


def skill_origin(directory, cache):
    for parent in (directory.resolve(), *directory.resolve().parents):
        if (parent / '.git').exists():
            if parent not in cache:
                cache[parent] = (run(['git', '-C', str(parent), 'config', '--get', 'remote.origin.url']) or '').strip()
            return cache[parent]
    return ''


def duplicate_skills(home):
    disabled_names, _ = disabled_skills(home)
    config = home / '.codex' / 'config.toml'
    configured_paths = {str(Path(path).expanduser().absolute()) for path in role_hidden_paths(config.read_text() if config.exists() else '')}
    by_name = {}
    for root in (home / '.codex' / 'skills', home / '.agents' / 'skills'):
        entries = list(installed_skill_dirs(root))
        if root.parent.name == '.codex':
            entries += list(installed_skill_dirs(root / '.system'))
        for name, directory, _ in entries:
            path = (directory / 'SKILL.md').absolute()
            realpath = path.resolve()
            by_name.setdefault(name, []).append({'root': str(root), 'path': str(path), 'realpath': str(realpath),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'disabled': name in disabled_names or any(candidate in configured_paths for candidate in
                    (str(path), str(realpath), str(directory.absolute()), str(directory.resolve())))})
    duplicates = []
    for name, paths in sorted(by_name.items()):
        if len({entry['root'] for entry in paths}) < 2:
            continue
        newest = max(paths, key=lambda entry: Path(entry['path']).stat().st_mtime)
        duplicates.append({'name': name, 'same': len({entry['sha256'] for entry in paths}) == 1,
                           'same_realpath': len({entry['realpath'] for entry in paths}) == 1, 'paths': paths,
                           'canonical_suggestion': newest['path'],
                           'reason': '가장 최근에 수정된 SKILL.md를 제안한다. 자동 적용하지 않는다.'})
    return duplicates


def skills_audit(days=30, stale_days=30, remote=False, home=None, sessions=None, routing_path=None):
    home = Path(home) if home else user_home()
    lock = read_json(home / '.agents' / '.skill-lock.json', {})
    locked = lock.get('skills', {})
    disabled_names, disabled_paths = disabled_skills(home)
    installed = role_skills(home)
    performance = skill_performance(days)
    reads, explicit, mcp = skill_usage(days, sessions if sessions is not None else home / '.codex' / 'sessions',
                                      include_mcp=True, skill_paths=installed)
    routing = load_routing(routing_path)
    routed = {name for route in routing['routes'] for name in route['skills']}
    routed.update(name for role in load_roles().values() for name in role['skills'])
    skills, origins, upstream = {}, {}, {}
    linked_paths = {}
    for root in (home / '.codex' / 'skills', home / '.agents' / 'skills'):
        for _, directory, linked in installed_skill_dirs(root):
            linked_paths[str((directory / 'SKILL.md').absolute())] = linked
    native_roots = (home / '.codex' / 'skills' / '.system', home / '.codex' / 'plugins' / 'cache')
    cutoff = time.time() - stale_days * 86400
    for name, path in installed:
        if name.split(':')[-1] in ENGINE_SKILLS:
            continue
        skill_file = Path(path)
        directory = skill_file.parent
        native = any(root in skill_file.parents for root in native_roots)
        metadata = {} if native else locked.get(name, locked.get(directory.name, {}))
        installation = {} if native else read_json(directory / 'INSTALLATION.json', {})
        linked = linked_paths.get(path)
        origin_url = '' if native else skill_origin(directory, origins)
        source = metadata.get('source') or metadata.get('sourceUrl') or linked or origin_url
        if not source and installation:
            source = installation.get('source') or installation.get('release') or 'INSTALLATION.json'
        origin = 'native' if native else 'external' if source else 'local'
        modified = skill_file.stat().st_mtime
        disabled = (name in disabled_names or str(skill_file.resolve()) in disabled_paths
                    or str(directory.resolve()) in disabled_paths)
        servers = {server for pattern, values in routing['mcp_usage'].items()
                   if name == pattern or pattern.endswith('*') and name.startswith(pattern[:-1])
                   for server in values}
        row = skills.setdefault(name, {'name': name, 'source': source or '', 'origin': origin, 'paths': [],
            'modified_at': '', 'modified_timestamp': modified, 'user_calls': explicit.get(name, 0),
            'agent_reads': reads.get(name, 0), 'mcp_calls': sum(mcp.get(server, 0) for server in servers),
            'disabled': False, 'banned': False,
            'fit': performance.get(name, dict.fromkeys(('good', 'poor', 'none', 'unrecorded'), 0))})
        # Preserve provenance when another installed alias has an external source.
        if origin == 'external' or origin == 'local' and row['origin'] == 'native':
            row['origin'], row['source'] = origin, source or ''
        row['paths'].append(str(directory))
        row['modified_timestamp'] = max(row['modified_timestamp'], modified)
        row['disabled'] |= disabled
        row['banned'] |= any(fnmatch.fnmatchcase(name, pattern) for pattern in load_settings()['banned_skills'])
        for key in ('sourceUrl', 'updatedAt'):
            if metadata.get(key):
                row[key] = metadata[key]
        for key in ('source_commit', 'update_policy'):
            if installation.get(key):
                row[key] = installation[key]
        if remote:
            installed_url = str(installation.get('source') or '')
            url = (metadata.get('sourceUrl') or origin_url
                   or (installed_url if re.match(r'https://github\.com/[\w.-]+/[\w.-]+/?$', installed_url) else ''))
            if url and url not in upstream:
                result = run(['git', 'ls-remote', url, 'HEAD'], 15)
                match = re.search(r'^([0-9a-f]{40,64})\s+HEAD$', result or '', re.M)
                upstream[url] = match.group(1) if match else '미확인'
            row['upstream_head'] = upstream.get(url, '미확인')
    duplicates = duplicate_skills(home)
    different = {row['name'] for row in duplicates if not row['same']}
    counts = dict.fromkeys(('review', 'review-duplicate', 'banned', 'disabled', 'keep', 'local', 'native'), 0)
    for row in skills.values():
        modified = row.pop('modified_timestamp')
        row['modified_at'] = datetime.datetime.fromtimestamp(modified).astimezone().isoformat(timespec='seconds')
        banned = row.pop('banned')
        if row['disabled']:
            verdict, reason = 'disabled', 'globally disabled'
        elif banned:
            verdict, reason = 'banned', 'settings.banned_skills'
        elif row['origin'] != 'native' and row['name'] in different:
            verdict, reason = 'review-duplicate', '같은 이름의 SKILL.md 내용이 다르다'
        elif row['origin'] in ('local', 'native'):
            verdict = reason = row['origin']
        elif row['name'] in routed:
            verdict, reason = 'keep', 'routed'
        elif row['user_calls'] + row['agent_reads'] + row['mcp_calls'] + row['fit']['good']:
            verdict, reason = 'keep', 'used'
        elif modified < cutoff:
            verdict, reason = 'review', 'unused and stale'
        else:
            verdict, reason = 'keep', 'recently modified'
        row['verdict'], row['reason'] = verdict, reason
        counts[verdict] += 1
    counts['duplicates'] = len(duplicates)
    return {'ts': now().isoformat(timespec='seconds'), 'days': days, 'stale_days': stale_days,
            'remote': remote, 'counts': counts, 'skills': sorted(skills.values(), key=lambda row: row['name']),
            'duplicates': duplicates}


def skills_status():
    files = sorted((state() / 'audits').glob('skills-????????.json'), reverse=True)
    audit = read_json(files[0]) if files else None
    if not audit:
        return 'skills: not audited'
    counts = audit.get('counts', {})
    return 'skills: %d review, %d review-duplicate, %d banned, %d duplicates, native %d, local %d' % (
        counts.get('review', 0), counts.get('review-duplicate', 0), counts.get('banned', 0),
        counts.get('duplicates', 0), counts.get('native', 0), counts.get('local', 0))


def command_skills_audit(args):
    if args.days < 0 or args.stale_days < 0:
        raise SystemExit('days and stale-days must be nonnegative')
    audit = skills_audit(args.days, args.stale_days, args.remote)
    directory = state() / 'audits'
    owned_path(directory)
    target = directory / ('skills-' + now().strftime('%Y%m%d') + '.json')
    atomic_write(target, json.dumps(audit, ensure_ascii=False, indent=1))
    if args.json:
        print(json.dumps(audit, ensure_ascii=False, indent=1))
        return
    print('| 스킬 | 출처 | origin | 로컬 최종 수정 | 사용자 호출 | 에이전트 읽기 | mcp_calls | 성과(g/p/n/-) | 비활성화 | 판정 | reason |' + (' upstream HEAD |' if args.remote else ''))
    print('|---|---|---|---|---|---|---|---|---|---|---|' + ('---|' if args.remote else ''))
    for row in audit['skills']:
        cells = [row['name'], row['source'], row['origin'], row['modified_at'], row['user_calls'],
                 row['agent_reads'], row['mcp_calls'],
                 '/'.join(str(row['fit'][key]) for key in ('good', 'poor', 'none', 'unrecorded')),
                 row['disabled'], row['verdict'], row['reason']]
        if args.remote:
            cells.append(row['upstream_head'])
        print('| ' + ' | '.join(str(cell).replace('|', '\\|').replace('\n', ' ') for cell in cells) + ' |')
    print('\n## duplicates\n| 스킬 | 같은 내용 | 경로 | realpath | SKILL.md sha256 | 비활성화 |\n|---|---|---|---|---|---|')
    for duplicate in audit['duplicates']:
        for path in duplicate['paths']:
            cells = [duplicate['name'], duplicate['same'], path['path'], path['realpath'], path['sha256'], path['disabled']]
            print('| ' + ' | '.join(str(cell).replace('|', '\\|').replace('\n', ' ') for cell in cells) + ' |')
    print('counts: ' + ', '.join('%s=%d' % item for item in audit['counts'].items()))
    print('saved: ' + str(target))


# ---- role configurations --------------------------------------------------

ROLE_HEADER = '# generated by conductor roles sync'


def load_roles(path=None, data=None):
    path = Path(path) if path is not None else overlay_path('roles.json')
    data = read_json(path) if data is None else data
    if not isinstance(data, dict) or not isinstance(data.get('roles'), dict):
        raise SystemExit('invalid role definitions: ' + str(path))
    for name, role in data['roles'].items():
        if (not re.fullmatch(r'[A-Za-z0-9_-]+', name) or not isinstance(role, dict)
                or not all(isinstance(role.get(key), str) for key in ('model', 'effort', 'job'))
                or not isinstance(role.get('skills'), list)
                or not all(isinstance(skill, str) for skill in role['skills'])
                or not role['model'] or role['effort'] not in EFFORTS or role.get('sandbox') not in SANDBOXES):
            raise SystemExit('invalid role definition: ' + name)
    return data['roles']


def plugin_skill_name(collection, directory, cache):
    parent = collection.parent
    versioned = bool(re.fullmatch(r'v?\d+(?:[.\w+-]*\d[.\w+-]*)?|[0-9a-f]{7,64}', parent.name))
    if versioned:
        return parent.parent.name + ':' + directory.name
    if len(parent.relative_to(cache).parts) <= 2:
        return parent.name + ':' + directory.name
    plugin = parent.name
    for ancestor in (parent, *parent.parents):
        if ancestor == cache or cache not in ancestor.parents:
            break
        manifest = read_json(ancestor / '.codex-plugin' / 'plugin.json') or read_json(ancestor / 'plugin.json')
        if isinstance(manifest, dict) and isinstance(manifest.get('name'), str):
            plugin = manifest['name']
            break
    frontmatter = '\n'.join(skill_frontmatter(directory / 'SKILL.md'))
    match = re.search(r'^name:\s*(.+?)\s*$', frontmatter, re.M)
    name = match.group(1).strip().strip('"\'') if match else directory.name
    return plugin + ':' + name


def role_skills(home):
    """Retain lexical paths so every installed alias can be disabled separately."""
    skills = {}
    for root in (home / '.codex' / 'skills', home / '.codex' / 'skills' / '.system', home / '.agents' / 'skills'):
        for name, directory, _ in installed_skill_dirs(root):
            skills[str((directory / 'SKILL.md').absolute())] = name
    cache = home / '.codex' / 'plugins' / 'cache'

    def collections(directory, ancestors):
        resolved = directory.resolve()
        if not directory.is_dir() or resolved in ancestors:
            return
        if directory.name == 'skills':
            yield directory
            return
        for child in sorted(directory.iterdir()):
            if child.is_dir() and not child.name.startswith('.'):
                yield from collections(child, ancestors | {resolved})

    for collection in collections(cache, set()):
        for directory in sorted(collection.iterdir()):
            skill_file = directory / 'SKILL.md'
            if skill_file.is_file():
                skills[str(skill_file.absolute())] = plugin_skill_name(collection, directory, cache)
    return sorted((name, path) for path, name in skills.items())


ROLES_BEGIN = '# conductor:roles:begin v1'
ROLES_END = '# conductor:roles:end'


def toml_tokens(text):
    """Lex only: strings are opaque tokens, including multiline TOML strings.

    Offsets refer to the original text, so edits never normalize user bytes.
    This scanner is also used with tomllib: a regex cannot locate TOML tables
    or ownership comments safely inside developer_instructions.
    """
    tokens = []
    i = 0
    while i < len(text):
        start = i
        char = text[i]
        if char in ' \t\r':
            i += 1
            continue
        if char == '#':
            i = text.find('\n', i)
            if i < 0:
                i = len(text)
            kind = 'comment'
        elif char in "\"'":
            delimiter = char * (3 if text.startswith(char * 3, i) else 1)
            i += len(delimiter)
            while i < len(text):
                if char == '"' and text[i] == '\\':
                    i += 2
                elif text.startswith(delimiter, i):
                    i += len(delimiter)
                    if len(delimiter) == 3:
                        # TOML permits one/two quote characters before the close.
                        while i < len(text) and text[i] == char:
                            i += 1
                    break
                else:
                    i += 1
            else:
                raise SystemExit('invalid config.toml: unterminated string')
            kind = 'string'
        elif char in '\n[]=.,{}':
            i += 1
            kind = char
        else:
            while i < len(text) and text[i] not in " \t\r\n#\"'[]=.,{}":
                i += 1
            kind = 'bare'
        tokens.append((kind, text[start:i], start, i))
    return tokens


def toml_string(literal):
    if tomllib is not None:
        return tomllib.loads('value = ' + literal)['value']
    triple = literal.startswith(('"""', "'''"))
    value = literal[3:-3] if triple else literal[1:-1]
    if triple:
        value = re.sub(r'^\r?\n', '', value)
    if literal.startswith("'"):
        return value
    if triple:
        value = re.sub(r'\\[ \t\r]*\n\s*', '', value)
    escapes = {'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r', '"': '"', '\\': '\\'}
    def decode(match):
        escaped = match.group(1)
        return chr(int(escaped[1:], 16)) if escaped[0] in 'uU' else escapes[escaped]
    return re.sub(r'\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[btnfr"\\])', decode, value)


def toml_key(tokens):
    return tuple(toml_string(token[1]) if token[0] == 'string' else token[1]
                 for token in tokens if token[0] != '.')


def config_statements(config):
    """Yield table/assignment statements, keeping exact line spans."""
    row, depth = [], 0
    for token in toml_tokens(config) + [('\n', '', len(config), len(config))]:
        kind = token[0]
        if kind == 'comment':
            continue
        if kind == '\n' and depth == 0:
            if row:
                start = config.rfind('\n', 0, row[0][2]) + 1
                yield row, start, token[3]
                row = []
            continue
        if kind in ('[', '{'):
            depth += 1
        elif kind in (']', '}'):
            depth -= 1
        if kind != '\n':
            row.append(token)


def config_declarations(config):
    """Read key paths, not string values, for the Python 3.9 fallback."""
    declarations = []
    context = ()
    def value_keys(path, tokens):
        declarations.append((path, tokens))
        if not tokens or tokens[0][0] != '{':
            return
        start, depth = 1, 0
        for i in range(1, len(tokens)):
            kind = tokens[i][0]
            if depth == 0 and kind in (',', '}'):
                item = tokens[start:i]
                equals = next((j for j, t in enumerate(item) if t[0] == '='), None)
                if equals is not None:
                    value_keys(path + toml_key(item[:equals]), item[equals + 1:])
                start = i + 1
            if kind in ('[', '{'):
                depth += 1
            elif kind in (']', '}'):
                depth -= 1
    for tokens, _, _ in config_statements(config):
        if tokens[0][0] == '[':
            width = 2 if len(tokens) > 1 and tokens[1][0] == '[' else 1
            context = toml_key(tokens[width:-width])
            declarations.append((context, None))
        else:
            equals = next((i for i, token in enumerate(tokens) if token[0] == '='), None)
            if equals is not None:
                value_keys(context + toml_key(tokens[:equals]), tokens[equals + 1:])
    return declarations


def roles_marker_spans(config):
    return region_marker_spans(config, ROLES_BEGIN, ROLES_END, 'roles')


def region_marker_spans(config, begin, finish, label):
    markers = []
    begins = (begin, begin + ' +newline') if label == 'skills' else (begin,)
    for kind, text, start, end in toml_tokens(config):
        if kind == 'comment' and text.rstrip('\r') in begins + (finish,):
            line_start = config.rfind('\n', 0, start) + 1
            if not config[line_start:start].strip():
                markers.append((text.rstrip('\r'), line_start, end + (end < len(config))))
    if not markers:
        return []
    if len(markers) != 2 or markers[0][0] not in begins or markers[1][0] != finish:
        raise SystemExit('malformed conductor %s region' % label)
    return [(start, end) for _, start, end in markers]


def roles_region(config):
    spans = roles_marker_spans(config)
    return (spans[0][0], spans[1][1]) if spans else None


def role_blocks(config):
    blocks = {}
    name = None
    for tokens, start, end in config_statements(config):
        if tokens[0][0] == '[':
            key = toml_key(tokens[1:-1])
            name = key[1] if len(key) == 2 and key[0] == 'agents' else None
            if name is not None:
                marker = re.search(r'^# conductor:role[ \t]*\r?\n\Z', config[:start], re.M)
                if marker:
                    start = marker.start()
                blocks[name] = (start, end, config[start:end])
        elif name is not None:
            start = blocks[name][0]
            blocks[name] = (start, end, config[start:end])
    return blocks


def registered_roles(config):
    if tomllib is not None:
        try:
            agents = tomllib.loads(config).get('agents', {})
            return {name for name, row in agents.items() if isinstance(row, dict)} if isinstance(agents, dict) else set()
        except (ValueError, TypeError) as error:
            raise SystemExit('invalid config.toml: ' + str(error))
    return {path[1] for path, value in config_declarations(config)
            if path[:1] == ('agents',) and len(path) >= 2
            and (len(path) > 2 or value is None or value and value[0][0] == '{')}


def generated_file(path):
    path = Path(path)
    if not path.is_file():
        return False
    with path.open() as handle:
        generated = handle.readline().rstrip('\r\n') == ROLE_HEADER
    if generated:
        owned_path(path)
    return generated


def registration_file(block):
    for path, tokens in config_declarations(block):
        if len(path) == 3 and path[0] == 'agents' and path[2] == 'config_file' and tokens and tokens[0][0] == 'string':
            value = expand_home(toml_string(tokens[0][1]))
            return Path(value) if Path(value).is_absolute() else user_home() / '.codex' / value
    return None


def owned_registrations(config, require_file=False):
    result = {}
    region = roles_region(config)
    for name, (start, end, block) in role_blocks(config).items():
        path = registration_file(block)
        generated = path is not None and generated_file(path)
        marked = re.match(r'^# conductor:role[ \t]*\r?\n', block)
        in_region = region is not None and region[0] <= start < end <= region[1]
        if generated or not require_file and (marked or in_region and managed_shape(name, block, path)):
            result[name] = (start, end, block)
    return result


def registration_extras(name, block):
    """Keys other than config_file; deleting the table would lose them."""
    return [path for path, _ in config_declarations(block)
            if path not in (('agents', name), ('agents', name, 'config_file'))]


def managed_shape(name, block, path):
    """Inside the region, position alone is not ownership: only the exact table
    roles sync writes, pointing into ~/.codex/agents at no user file, counts."""
    directory = user_home() / '.codex' / 'agents'
    return (path is not None and not registration_extras(name, block)
            and lexical_path(path).parent == lexical_path(directory) and not path.exists())


def require_plain_registrations(blocks):
    names = sorted(name for name, (_, _, block) in blocks.items() if registration_extras(name, block))
    if names:
        raise SystemExit('owned role registration has other keys; move them to another table first: ' + ', '.join(names))


def apply_edits(config, edits):
    ordered = sorted(set(edits))
    for (_, end, _), (start, _, _) in zip(ordered, ordered[1:]):
        if start < end:
            raise SystemExit('conductor roles region overlaps a role table; fix config.toml by hand')
    for start, end, replacement in reversed(ordered):
        config = config[:start] + replacement + config[end:]
    return config


def region_edits(config, owned):
    """Drop the region markers and owned role tables inside it; other text stays in place."""
    region = roles_region(config)
    if region is None:
        return []
    inside = {name: row for name, row in owned.items() if region[0] <= row[0] < row[1] <= region[1]}
    require_plain_registrations(inside)
    return ([(start, end, '') for start, end in roles_marker_spans(config)]
            + [(start, end, '') for start, end, _ in inside.values()])


def validate_config_change(original, candidate, owned):
    if tomllib is None:
        # Same rule as below, using the scanner: only owned agents entries may differ.
        # Statements the scanner cannot read are compared too, so a broken candidate fails.
        def declarations(text):
            rows = [(path, None if tokens is None else [token[:2] for token in tokens])
                    for path, tokens in config_declarations(text)
                    if not (path[:1] == ('agents',) and len(path) >= 2 and path[1] in owned)]
            rows += [(('<unparsed>',), [token[:2] for token in tokens]) for tokens, _, _ in config_statements(text)
                     if tokens[0][0] != '[' and not any(token[0] == '=' for token in tokens)]
            return rows
        if declarations(original) != declarations(candidate):
            raise SystemExit('refusing to change non-owned config.toml values')
        return
    try:
        before, after = tomllib.loads(original), tomllib.loads(candidate)
    except (ValueError, TypeError) as error:
        raise SystemExit('invalid config.toml: ' + str(error))
    for data in (before, after):
        agents = data.get('agents')
        if isinstance(agents, dict):
            for name in owned:
                agents.pop(name, None)
            if not agents:
                data.pop('agents', None)
    if before != after:
        raise SystemExit('refusing to change non-owned config.toml values')


def registration_conflict(config, roles):
    owned = owned_registrations(config)
    if tomllib is not None:
        try:
            agents = tomllib.loads(config).get('agents', {})
        except (ValueError, TypeError) as error:
            raise SystemExit('invalid config.toml: ' + str(error))
        existing = set(agents) if isinstance(agents, dict) else set(roles)
    else:
        existing = {path[1] for path, _ in config_declarations(config) if path[:1] == ('agents',) and len(path) >= 2}
        # [[agents]] makes agents a list; tomllib then reports every role as a conflict.
        if any(len(tokens) > 1 and tokens[0][0] == '[' and tokens[1][0] == '[' and toml_key(tokens[2:-2]) == ('agents',)
               for tokens, _, _ in config_statements(config)):
            existing = set(roles)
    conflicts = set(roles) & (existing - set(owned))
    if conflicts:
        for name in sorted(conflicts):
            print('[agents.%s]\nconfig_file = "./agents/%s.toml"' % (name, name))
        raise SystemExit('role registration conflict: ' + ', '.join(sorted(conflicts)))
    # An inline agents table is sealed by TOML, even for a new role name.
    if roles and any(path == ('agents',) and value is not None for path, value in config_declarations(config)):
        for name in sorted(roles):
            print('[agents.%s]\nconfig_file = "./agents/%s.toml"' % (name, name))
        raise SystemExit('cannot extend inline/scalar agents; use an [agents] table '
                         '(an inline table converts with: cx config convert-agents)')


def remove_blocks(config, blocks):
    for start, end, _ in sorted(blocks.values(), reverse=True):
        config = config[:start] + config[end:]
    return config


def register_roles(config, roles):
    registration_conflict(config, roles)
    owned = owned_registrations(config)
    region = roles_region(config)
    legacy = {name for name, (start, end, _) in owned.items()
              if region is None or not region[0] <= start < end <= region[1]}
    newline = '\r\n' if '\r\n' in config else '\n'
    outside = apply_edits(config, region_edits(config, owned))
    blocks = ''.join('[agents.%s]%sconfig_file = "./agents/%s.toml"%s' % (name, newline, name, newline)
                     for name in sorted(set(roles) - legacy))
    candidate = outside
    if blocks:
        candidate += (newline if candidate and not candidate.endswith('\n') else '') + ROLES_BEGIN + newline + blocks + ROLES_END + newline
    validate_config_change(config, candidate, set(owned) | set(roles))
    return candidate


def role_toml(name, role, skills):
    allowed = set(role['skills'])
    hidden = set()
    allowed_count = 0
    for skill, path in skills:
        if skill in allowed and skill.split(':')[-1] != ENGINE_SKILL:
            allowed_count += 1
        else:
            hidden.update((path, str(Path(path).resolve())))
    result_rule = ('긴 결과는 요약하고 근거 경로를 붙여 돌려준다.' if role.get('sandbox') == 'read-only'
                   else '긴 결과는 파일로 저장해 경로만 돌려준다.')
    instructions = ('[conductor-subagent] 너는 conductor가 띄운 서브에이전트다. conductor를 적용하지 않고 '
                    '서브에이전트를 띄우지 않는다. 받은 지시의 범위만 수행한다. 허용된 스킬: %s. '
                    '반환은 지시문이 정한 형식을 따르고, %s'
                    % (', '.join(role['skills']) or '없음', result_rule))
    fields = [('name', name), ('description', role['job']), ('model', role['model']),
              ('model_reasoning_effort', role['effort'])]
    if role.get('sandbox') is not None:
        fields.append(('sandbox_mode', role['sandbox']))
    fields.append(('developer_instructions', instructions))
    lines = [ROLE_HEADER] + [key + ' = ' + json.dumps(value, ensure_ascii=False) for key, value in fields]
    for path in sorted(hidden):
        lines += ['', '[[skills.config]]', 'path = ' + json.dumps(path, ensure_ascii=False), 'enabled = false']
    return '\n'.join(lines) + '\n', allowed_count, len(hidden)


def role_hidden_paths(text):
    paths = set()
    for block in re.split(r'^\s*\[\[skills\.config\]\]\s*(?:#.*)?$', text, flags=re.M)[1:]:
        block = re.split(r'^\s*\[', block, maxsplit=1, flags=re.M)[0]
        if re.search(r'^\s*enabled\s*=\s*false\s*(?:#.*)?$', block, re.M):
            match = re.search(r'''^\s*path\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')''', block, re.M)
            if match:
                value = match.group(1)
                paths.add(json.loads(value) if value.startswith('"') else value[1:-1])
    return paths


def role_warnings(home, roles, skills):
    names = {name for name, _ in skills}
    disabled_names, disabled_paths = disabled_skills(home)
    warnings = []
    for name, role in roles.items():
        for skill in role['skills']:
            if skill not in names:
                warnings.append('WARN role %s: allowed skill not found: %s' % (name, skill))
            elif skill in disabled_names or any(candidate == skill and str(Path(path).resolve()) in disabled_paths
                                               for candidate, path in skills):
                warnings.append('WARN role %s: allowed skill is globally disabled: %s' % (name, skill))
    return warnings


def role_health(home, roles):
    config = home / '.codex' / 'config.toml'
    registered = registered_roles(config.read_text() if config.exists() else '')
    skills = role_skills(home)
    health = []
    for name, role in roles.items():
        target = home / '.codex' / 'agents' / (name + '.toml')
        exists = target.is_file()
        expected = role_hidden_paths(role_toml(name, role, skills)[0])
        drift = exists and role_hidden_paths(target.read_text()) != expected
        health.append((name, exists, name in registered, drift))
    return health


def proposals_status():
    directory = state() / 'proposals'
    if not directory.is_dir():
        return 'proposals: none'
    pending = 0
    for path in directory.glob('*.md'):
        if not path.is_file():
            continue
        with path.open(errors='replace') as handle:
            first = handle.readline()
        if '상태: 적용됨' not in first and '상태: 거절됨' not in first:
            pending += 1
    return 'proposals: %d pending (~/.codex/kp-conductor/proposals)' % pending


def command_roles(args, definitions=None):
    home = user_home()
    roles = load_roles() if definitions is None else definitions
    skills = role_skills(home)
    for warning in role_warnings(home, roles, skills):
        print(warning)
    config = user_file(home / '.codex/config.toml')
    original_hash = file_hash(config)
    original = config.read_bytes().decode('utf-8') if config.exists() else ''
    updated = register_roles(original, roles)
    owned_path(home / '.codex/agents')
    plans = []
    for name, role in roles.items():
        target = owned_path(home / '.codex/agents' / (name + '.toml'))
        text, allowed, hidden = role_toml(name, role, skills)
        if target.exists() and not generated_file(target):
            raise SystemExit('refusing to overwrite non-generated role file: ' + str(target))
        plans.append((name, target, text, allowed, hidden, file_hash(target)))
    if not args.dry_run:
        # change_units keeps the config.toml copy inside the private overlay backup.
        with change_units({'config.toml': config}):
            for _, target, text, _, _, expected in plans:
                atomic_write(target, text, expected)
            if updated != original:
                atomic_write(config, updated, original_hash)
    for name, target, _, allowed, hidden, _ in plans:
        print('%s: 허용 %d, 숨김 %d, %s' % (name, allowed, hidden, target))
    print('역할 %d개, 스킬 총 %d개, 역할별 숨김 수: %s' % (len(roles), len(skills), ', '.join('%s=%d' % (name, hidden) for name, _, _, _, hidden, _ in plans)))

# ---- entry, overlays and onboarding ----------------------------------------


def entry_source():
    path = SKILL / 'references' / 'entry-block.md'
    if not path.is_file():
        raise SystemExit('entry block source missing: ' + str(path))
    return path.read_bytes().decode('utf-8')


def entry_status(home):
    target = home / '.codex' / 'AGENTS.md'
    text = target.read_bytes().decode('utf-8') if target.exists() else ''
    matches = list(ENTRY_PATTERN.finditer(text))
    if not matches:
        return 'outdated' if '<!-- conductor:begin' in text or ENTRY_END in text else 'absent'
    if len(matches) != 1:
        return 'outdated'
    try:
        source = entry_source()
    except SystemExit:
        return 'outdated'
    match = matches[0]
    expected = source
    same = hashlib.sha256(match.group(1).encode()).digest() == hashlib.sha256(expected.encode()).digest()
    return 'present' if same and match.group().startswith(ENTRY_BEGIN + '\n') else 'outdated'


def overlay_backup(extra=None):
    root = owned_path(state() / 'local')
    # Validate every source before creating a partially usable snapshot.
    for name in OVERLAY_FILES:
        owned_path(root / name)
    agents = owned_path(user_home() / '.codex/agents')
    generated = [path for path in agents.glob('*.toml') if generated_file(path)]
    config = user_file(user_home() / '.codex/config.toml')
    entry = user_file(user_home() / '.codex/AGENTS.md')
    config_text = config.read_bytes().decode('utf-8') if config.exists() else ''
    entry_text = entry.read_bytes().decode('utf-8') if entry.exists() else ''
    registrations = {name: block for name, (_, _, block) in owned_registrations(config_text).items()}
    region = roles_region(config_text)
    legacy = [name for name, (start, end, _) in owned_registrations(config_text).items()
              if region is None or not region[0] <= start < end <= region[1]]
    stamp = now()
    target = owned_path(root / 'backups' / stamp.strftime('%Y%m%d-%H%M%S'))
    while target.exists():
        stamp += datetime.timedelta(seconds=1)
        target = root / 'backups' / stamp.strftime('%Y%m%d-%H%M%S')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    target.mkdir(mode=0o700)
    present = []
    for name in OVERLAY_FILES:
        source = root / name
        if source.exists():
            atomic_write(target / name, source.read_bytes(), expected=None, mode=0o600)
            present.append(name)
    role_modes = {}
    if generated:
        (target / 'agents').mkdir(mode=0o700)
    for source in generated:
        atomic_write(target / 'agents' / source.name, source.read_bytes(), expected=None, mode=0o600)
        role_modes[source.name] = stat.S_IMODE(source.stat().st_mode)
    matches = list(ENTRY_PATTERN.finditer(entry_text))
    manifest = {'version': 2, 'files': present, 'roles': role_modes,
                'registrations': registrations, 'legacy_registrations': legacy,
                'entry': matches[0].group() if matches else None,
                'user_files': {name: {'exists': path.exists(), 'hash': file_hash(path),
                                     'mode': stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600}
                               for name, path in [('config.toml', config), ('AGENTS.md', entry)]}}
    for name, source in dict({'config.toml': config, 'AGENTS.md': entry}, **(extra or {})).items():
        source = Path(source)
        if source.is_file():
            atomic_write(target / name, source.read_bytes(), expected=None, mode=0o600)
    atomic_write(target / 'manifest.json', json.dumps(manifest, indent=1), expected=None, mode=0o600)
    return target


def overlay_backups():
    root = state() / 'local' / 'backups'
    return sorted((path for path in root.glob('*') if path.is_dir() and (path / 'manifest.json').is_file()), reverse=True)


def restore_user_unit(target, manifest, name, registration_snapshot=None):
    path = user_file(user_home() / '.codex' / name)
    expected = file_hash(path)
    previous = manifest.get('user_files', {}).get(name)
    current = path.read_bytes().decode('utf-8') if path.exists() else ''
    if previous is not None and expected == previous['hash']:
        print('변경 없음: ' + str(path))
        return
    # Whole-file restoration is safe only if our most recent write still owns it.
    if previous is not None and str(path) in LAST_WRITES and expected == LAST_WRITES[str(path)]:
        if previous['exists']:
            atomic_write(path, owned_path(target / name).read_bytes(), expected, mode=previous.get('mode'))
        else:
            atomic_remove(path, expected)
        return
    if name == 'AGENTS.md':
        matches = list(ENTRY_PATTERN.finditer(current))
        leftover = ENTRY_PATTERN.sub('', current)
        if len(matches) > 1 or '<!-- conductor:begin' in leftover or ENTRY_END in leftover:
            raise SystemExit('malformed conductor entry block: ' + str(path))
        saved = manifest.get('entry') or ''
        if matches:
            match = matches[0]
            candidate = current[:match.start()] + saved + current[match.end():]
        else:
            candidate = saved + current
    else:
        if registration_snapshot is not None:
            snapshot_hash, owned = registration_snapshot
            if expected != snapshot_hash:
                print('복구하지 않음(외부 변경): ' + str(path))
                raise SystemExit('config.toml changed during restoration')
        else:
            owned = owned_registrations(current)
        saved = manifest.get('registrations', {})
        region = roles_region(current)
        newline = '\r\n' if '\r\n' in current else '\n'
        def normalized(text):
            return text.replace('\r\n', '\n').replace('\n', newline)
        legacy = set(manifest.get('legacy_registrations', saved))
        edits, retained = [], set()
        for role, (start, end, block) in owned.items():
            if region and region[0] <= start < end <= region[1]:
                continue
            replacement = normalized(saved[role]) if role in saved and role in legacy else ''
            if replacement != block:
                require_plain_registrations({role: (start, end, block)})
            if replacement:
                retained.add(role)
            edits.append((start, end, replacement))
        edits.extend(region_edits(current, owned))
        candidate = apply_edits(current, edits)
        missing = {role: block for role, block in saved.items() if role not in retained}
        registration_conflict(candidate, missing)
        if missing:
            candidate += newline if candidate and not candidate.endswith('\n') else ''
            # Older snapshots retain their legacy registration spelling.
            candidate += ''.join(normalized(block) for role, block in missing.items() if role in legacy)
            managed = ''.join(normalized(block) for role, block in missing.items() if role not in legacy)
            if managed:
                candidate += ROLES_BEGIN + newline + managed + ROLES_END + newline
        validate_config_change(current, candidate, set(owned) | set(saved))
    if candidate != current:
        atomic_write(path, candidate, expected)
    else:
        print('변경 없음: ' + str(path))
    # Only the owned portion was restored; report preserved external edits.
    saved_file = owned_path(target / name)
    saved_text = saved_file.read_bytes().decode('utf-8') if saved_file.exists() else ''
    if previous is not None and candidate != saved_text:
        print('복구하지 않음(외부 변경): 전체 파일 ' + str(path))



def backup_matches(path, identifier):
    return (path.name == identifier or str(path) == identifier
            or Path(identifier).is_absolute() and lexical_path(path) == lexical_path(identifier))

def overlay_restore(identifier=None):
    backups = overlay_backups()
    if identifier:
        # Accept the old path output as well as an ID, without permitting traversal.
        backups = [path for path in backups if backup_matches(path, identifier)]
    if not backups:
        raise SystemExit('no matching overlay backup')
    target = owned_path(backups[0])
    manifest = json_object(owned_path(target / 'manifest.json'))
    files = manifest.get('files')
    if not isinstance(files, list) or any(name not in OVERLAY_FILES or not (target / name).is_file() for name in files):
        raise SystemExit('invalid overlay backup: ' + str(target))
    # Keep the state about to be replaced, so a mistaken restore can be undone.
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            before = overlay_backup()
        print('복구 전 상태 백업: ' + before.name)
    except (Exception, SystemExit) as error:
        print('복구 전 상태 백업 실패: %s' % error)
    errors = []
    def attempt(label, operation):
        try:
            operation()
        except (Exception, SystemExit) as error:
            errors.append(label)
            print('복구하지 못함: %s: %s' % (label, error))
    # Freeze registration ownership before any generated role can be removed.
    registration_snapshot = None
    if manifest.get('version', 1) >= 2:
        def snapshot_registrations():
            nonlocal registration_snapshot
            config = user_file(user_home() / '.codex/config.toml')
            expected = file_hash(config)
            text = config.read_bytes().decode('utf-8') if config.exists() else ''
            registration_snapshot = (expected, owned_registrations(text))
        attempt('config.toml ownership', snapshot_registrations)
        if registration_snapshot is not None:
            attempt('config.toml', lambda: restore_user_unit(target, manifest, 'config.toml', registration_snapshot))
        attempt('AGENTS.md', lambda: restore_user_unit(target, manifest, 'AGENTS.md'))
    root = state() / 'local'
    def restore_file(path, source=None, mode=None):
        path = owned_path(path)
        expected = file_hash(path)
        content = owned_path(source).read_bytes() if source is not None else None
        desired = hashlib.sha256(content).hexdigest() if content is not None else None
        if expected == desired:
            print('변경 없음: ' + str(path))
        elif content is None:
            atomic_remove(path, expected)
        else:
            atomic_write(path, content, expected, mode=mode)
    for name in OVERLAY_FILES:
        path = root / name
        def restore_overlay(path=path, name=name):
            restore_file(path, target / name if name in files else None)
        attempt(str(path), restore_overlay)
    if manifest.get('version', 1) >= 2:
        directory = user_home() / '.codex/agents'
        saved = manifest.get('roles', {})
        names = set(saved) | {path.name for path in directory.glob('*.toml')}
        for name in sorted(names):
            path = directory / name
            def restore_role(path=path, name=name):
                owned_path(directory)
                if path.exists() and not generated_file(path):
                    if name in saved:
                        raise SystemExit('non-generated role file: ' + str(path))
                    return
                if name in saved:
                    if Path(name).name != name or not name.endswith('.toml'):
                        raise SystemExit('invalid backup role name')
                    restore_file(path, target / 'agents' / name, mode=saved[name])
                else:
                    restore_file(path)
            attempt(str(path), restore_role)
        def check_dangling_registrations():
            config = user_file(user_home() / '.codex/config.toml')
            text = config.read_bytes().decode('utf-8') if config.exists() else ''
            for role, (_, _, block) in role_blocks(text).items():
                path = registration_file(block)
                if path is not None and path.parent.resolve() == directory.resolve() and path.name in names - set(saved) and not path.exists():
                    raise SystemExit('registration remains for removed role: ' + role)
        attempt('config.toml dangling registrations', check_dangling_registrations)
    if errors:
        raise SystemExit(1)
    return target


def command_overlay(args):
    if args.action == 'backup':
        with contextlib.redirect_stdout(io.StringIO()) as details:
            target = overlay_backup()
        print('overlay backup: ' + str(target))
        print(details.getvalue(), end='')
    elif args.action == 'restore':
        print('overlay restored: ' + str(overlay_restore(args.id)))
    elif args.action == 'list':
        for path in overlay_backups():
            files = json_object(path / 'manifest.json')['files']
            print('%s: %s' % (path.name, ', '.join(files) or 'empty'))
    else:
        if not args.id:
            raise SystemExit('overlay verify requires --id')
        # Validate the requested snapshot even if the current settings are healthy.
        if not any(backup_matches(path, args.id) for path in overlay_backups()):
            raise SystemExit('no matching overlay backup')
        def inspect():
            global LAST_DOCTOR_PROBLEMS
            LAST_DOCTOR_PROBLEMS = []
            try:
                command_doctor(args)
                return set()
            except SystemExit as error:
                if error.code in (None, 0):
                    return set()
                # A failure without a named check is treated as an overlay problem.
                return set(LAST_DOCTOR_PROBLEMS) or {'doctor'}
        def setting(path, key):
            try:
                return json_object(path).get(key) if path.is_file() else None
            except (Exception, SystemExit):
                return ('unreadable',)
        selected = next(path for path in overlay_backups() if backup_matches(path, args.id))
        def environment(problems):
            # ocx and the proxy belong to the machine unless the overlay changed their settings.
            changed = {key for key in ('ocx', 'proxy')
                       if setting(selected / 'settings.json', key) != setting(state() / 'local' / 'settings.json', key)}
            result = {'ocx', 'proxy'} - changed
            return result | ({'routed models in catalog'} if 'proxy' in result and 'proxy' in problems else set())
        problems = inspect()
        if problems and not problems - environment(problems):
            print('overlay not restored: environment checks failed: ' + ', '.join(sorted(problems)))
            raise SystemExit(2)
        if problems:
            try:
                target = overlay_restore(args.id)
                print('overlay restored: ' + str(target))
            except (Exception, SystemExit) as error:
                print('overlay restoration failed: ' + str(error))
            remaining = inspect()
            healthy = not remaining - environment(remaining)
            print('overlay recheck: ' + ('passed' if healthy else 'failed'))
            raise SystemExit(1)
        print('overlay verified')


def command_entry(args):
    home = user_home()
    if args.action == 'status':
        print('entry: ' + entry_status(home))
        return
    source = entry_source() if args.action == 'install' else None
    target = user_file(home / '.codex/AGENTS.md')
    expected = file_hash(target)
    text = target.read_bytes().decode('utf-8') if target.exists() else ''
    matches = list(ENTRY_PATTERN.finditer(text))
    if ('<!-- conductor:begin' in ENTRY_PATTERN.sub('', text) or ENTRY_END in ENTRY_PATTERN.sub('', text)
            or len(matches) > 1):
        raise SystemExit('malformed or duplicate conductor entry block')
    block = ENTRY_BEGIN + '\n' + source + ENTRY_END + '\n' if source is not None else ''
    if matches:
        match = matches[0]
        updated = text[:match.start()] + block + text[match.end():]
    elif source is not None:
        updated = block + text
    else:
        updated = text
    if updated != text:
        with change_units({'AGENTS.md': target}):
            if updated or source is not None or (home / '.codex/AGENTS.md').is_symlink():
                atomic_write(target, updated, expected)
            else:
                # The block was the whole file, so the file did not exist before install.
                atomic_remove(target, expected)
    print('entry: ' + entry_status(home))


def command_settings(args):
    if args.action == 'show':
        print(json.dumps(load_settings(), ensure_ascii=False, indent=1))
        return
    if args.key not in ('retro.apply', 'orchestrator', 'language', 'entry') or args.value is None:
        raise SystemExit('settings set accepts retro.apply, orchestrator, language, entry')
    path = state() / 'local' / 'settings.json'
    owned_path(path)
    expected = file_hash(path)
    data = json_object(path) if path.exists() else {}
    if args.key == 'retro.apply':
        data.setdefault('retro', {})['apply'] = args.value
    else:
        data[args.key] = None if args.key == 'orchestrator' and args.value == 'null' else args.value
    validate_settings(data)
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1) + '\n', expected)
    print('settings saved: ' + str(path))


def default_model(home):
    path = home / '.codex' / 'config.toml'
    text = path.read_text() if path.exists() else ''
    root = re.split(r'^\s*\[', text, maxsplit=1, flags=re.M)[0]
    match = re.search(r'''^\s*model\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')''', root, re.M)
    if not match:
        return None
    return json.loads(match.group(1)) if match.group(1).startswith('"') else match.group(1)[1:-1]


def skill_frontmatter(path):
    with Path(path).open(errors='replace') as handle:
        if handle.readline().strip() != '---':
            return []
        lines = []
        for line in handle:
            if line.strip() in ('---', '...'):
                break
            lines.append(line.rstrip('\n'))
    return lines


def skill_description(path):
    lines = skill_frontmatter(path)
    for index, line in enumerate(lines):
        match = re.match(r'^description:\s*(.*)$', line)
        if not match:
            continue
        value = match.group(1).strip()
        if value in ('>', '|', '>-', '|-', '>+', '|+'):
            parts = []
            for continuation in lines[index + 1:]:
                if continuation and not continuation[0].isspace():
                    break
                parts.append(continuation.strip())
            return ('\n' if value.startswith('|') else ' ').join(parts).strip()
        return value.strip('"\'')
    return ''


def onboard_detect(home=None):
    home = user_home() if home is None else Path(home)
    try:
        ocx = ocx_path()
    except (OSError, ValueError, SystemExit):
        ocx = None
    version = run([ocx, '--version']) if ocx else None
    text = run([ocx, 'provider', 'quota']) if ocx else None
    try:
        models = live_models()
    except (OSError, ValueError, SystemExit):
        models = None
    try:
        tiers = load_tiers()
    except (OSError, ValueError, SystemExit):
        tiers = None
    agents = home / '.codex' / 'AGENTS.md'
    other, skills = [], []
    try:
        found = role_skills(home)
    except OSError:
        found = None
    for name, path in found or []:
        directory = Path(path).parent
        try:
            description = skill_description(path)
        except OSError:
            description = None
        origin = 'native' if '.system' in directory.parts else 'external' if 'plugins' in directory.parts else 'local'
        skills.append({'name': name, 'path': path, 'description': description, 'origin': origin})
        if name.split(':')[-1] == 'using-superpowers':
            other.append({'kind': 'skill', 'name': name, 'path': path, 'evidence': name})
    hooks = home / '.codex' / 'hooks.json'
    hook_data = read_json(hooks, {})
    if isinstance(hook_data, dict):
        start = hook_data.get('hooks', hook_data)
        start = start.get('SessionStart') if isinstance(start, dict) else None
        if start:
            other.append({'kind': 'hook', 'name': 'SessionStart', 'path': str(hooks), 'evidence': json.dumps(start, ensure_ascii=False)})
    try:
        for line in agents.read_text().splitlines() if agents.exists() else []:
            if 'using-superpowers' in line:
                other.append({'kind': 'agents_md', 'name': 'using-superpowers', 'path': str(agents), 'evidence': line})
        entry, model = entry_status(home), default_model(home)
    except OSError:
        entry, model = None, None
    kp = next((row['path'] for row in skills if row['name'] == 'korean-protocol'), None)
    local = state() / 'local'
    servers = mcp_servers(home)
    try:
        config_path = home / '.codex' / 'config.toml'
        inline = inline_agents(config_path.read_text()) is not None if config_path.is_file() else False
    except (OSError, ValueError, SystemExit):
        inline = None
    return {'host': {'codex_home': str(home / '.codex'), 'default_model': model, 'agents_md': str(agents),
                     'entry_block': entry, 'other_entry': other, 'inline_agents': inline},
            'ocx': {'path': ocx, 'version': version.strip() if version else None,
                    'proxy': 'reachable' if models is not None else 'unreachable'},
            'models': models, 'quota': parse_quota(text) if text is not None else None,
            'burn': quota_burn(text) if text is not None else None, 'skills': skills if found is not None else None,
            'mcp_servers': servers,
            'capabilities': capability_report(skills, servers) if found is not None else None,
            'kp': {'installed': bool(kp), 'path': kp}, 'overlay': {'exists': local.exists(), 'path': str(local)},
            'unknown_models': sorted(set(models) - set(tiers.get('models', {}))) if models is not None and tiers is not None else None}


def recommended_packs():
    data = read_json(SKILL / 'references' / 'recommended-skills.json', {})
    packs = data.get('packs') if isinstance(data, dict) else None
    return [pack for pack in packs if isinstance(pack, dict)] if isinstance(packs, list) else []


def mcp_servers(home):
    path = Path(home) / '.codex' / 'config.toml'
    try:
        text = path.read_text() if path.is_file() else ''
        return sorted({key[1] for key, _ in config_declarations(text) if len(key) >= 2 and key[0] == 'mcp_servers'})
    except (OSError, ValueError, SystemExit):
        return None


def capability_report(skills, servers):
    """For each recommended pack: is it installed, and which installed tools already do the same job."""
    report = []
    for pack in recommended_packs():
        try:
            pattern = re.compile(pack.get('detect') or r'(?!)', re.I)
        except re.error:
            pattern = re.compile(r'(?!)')
        def ours(name, pack=pack):
            return pack_owns(pack, name)
        installed = sorted(row['name'] for row in skills if ours(row['name']))
        substitutes = sorted(row['name'] for row in skills if not ours(row['name'])
                             and row['name'].split(':')[-1] not in ENGINE_SKILLS
                             and pattern.search('%s %s' % (row['name'], row.get('description') or '')))
        report.append({'capability': pack.get('capability'), 'label': pack.get('label'), 'recommended': pack.get('name'),
                       'installed': installed, 'substitutes': substitutes,
                       'mcp': [name for name in servers or [] if ours(name) or pattern.search(name)],
                       'routes': pack.get('routes') or [], 'source': pack.get('source'), 'pin': pack.get('pin'),
                       'license': pack.get('license'), 'install': pack.get('install')})
    return report


def inline_agents(config):
    """Find a root-level agents = {...}; return (start, end, items, comments) or None."""
    for tokens, start, end in config_statements(config):
        if tokens[0][0] == '[':
            return None
        equals = next((i for i, token in enumerate(tokens) if token[0] == '='), None)
        if equals is None or toml_key(tokens[:equals]) != ('agents',):
            continue
        value = tokens[equals + 1:]
        if len(value) < 2 or value[0][0] != '{' or value[-1][0] != '}':
            return None
        items, depth, begin, last = [], 0, None, None
        spans = []
        for token in value[1:-1]:
            if token[0] == ',' and depth == 0:
                if begin is not None:
                    items.append(config[begin:last])
                    spans.append((begin, last))
                begin = None
                continue
            if token[0] in ('[', '{'):
                depth += 1
            elif token[0] in (']', '}'):
                depth -= 1
            begin = token[2] if begin is None else begin
            last = token[3]
        if begin is not None:
            items.append(config[begin:last])
            spans.append((begin, last))
        # Comments inside an item travel with the item; only the others move above the new table.
        comments = [text.rstrip('\r') for kind, text, first, _ in toml_tokens(config)
                    if kind == 'comment' and start <= first < end and not any(a <= first < b for a, b in spans)]
        return start, end, items, comments
    return None


def convert_inline_agents(config):
    found = inline_agents(config)
    if found is None:
        raise SystemExit('no top-level inline agents table to convert')
    start, end, items, comments = found
    newline = '\r\n' if '\r\n' in config else '\n'
    table = ''.join(comment + newline for comment in comments) + '[agents]' + newline + ''.join(item + newline for item in items)
    rest = config[:start] + config[end:]
    region = roles_region(rest)
    at = region[0] if region else len(rest)
    head = rest[:at]
    if head and not head.endswith('\n'):
        head += newline
    candidate = head + (newline if head else '') + table + rest[at:]
    if tomllib is not None:
        try:
            same = tomllib.loads(config) == tomllib.loads(candidate)
        except (ValueError, TypeError) as error:
            raise SystemExit('invalid config.toml: ' + str(error))
    else:
        def declarations(text):
            rows = [(path, None if tokens is None else [token[:2] for token in tokens])
                    for path, tokens in config_declarations(text) if path != ('agents',)]
            return rows + [(('<unparsed>',), [token[:2] for token in tokens]) for tokens, _, _ in config_statements(text)
                           if tokens[0][0] != '[' and not any(token[0] == '=' for token in tokens)]
        same = sorted(map(repr, declarations(config))) == sorted(map(repr, declarations(candidate)))
    if not same:
        raise SystemExit('refusing to convert agents: the result would change config.toml values')
    return candidate, config[start:end], table


def command_config(args):
    path = user_file(user_home() / '.codex/config.toml')
    expected = file_hash(path)
    text = path.read_bytes().decode('utf-8') if path.exists() else ''
    candidate, removed, table = convert_inline_agents(text)
    print('remove:\n' + removed.rstrip('\r\n'))
    print('add:\n' + table.rstrip('\r\n'))
    if args.dry_run:
        print('config convert-agents: dry-run')
        return
    with change_units({'config.toml': path}):
        atomic_write(path, candidate, expected)
    print('config convert-agents: converted ' + str(path))


def propose_plan(routing, detected):
    tiers = load_tiers()
    templates = load_roles(SKILL / 'references' / 'roles.json')
    ranked = tiers.get('models', {})
    live = set(detected.get('models') or [])
    installed = {row['name'] for row in detected.get('skills') or []}
    unknown = sorted(live - set(ranked))
    warnings = ['등급표 밖 모델: ' + model for model in unknown]
    assignments, roles = {}, {}
    order = tiers.get('tier_order', [])

    def compatible(name, model):
        for rule in tiers.get('constraints', []):
            if rule['role'] == name and any(assignments.get(other) == model for other in rule.get('differs_from', [])):
                return False
            if name in rule.get('differs_from', []) and assignments.get(rule['role']) == model:
                return False
        return True

    seats = tiers.get('seats', {})
    pending = list(templates)
    while pending:
        ready = next((name for name in pending if not any(rule['role'] == name and any(other in pending for other in rule.get('differs_from', []))
                                                       for rule in tiers.get('constraints', []))), pending[0])
        pending.remove(ready)
        candidates = seats.get(ready, [])
        chosen = next(((model, effort) for model, effort in candidates if model in live and model in ranked and compatible(ready, model)), None)
        if chosen is None:
            minimum = next((ranked[model].get('tier') for model, _ in candidates if model in ranked), None)
            ceiling = order.index(minimum) if minimum in order else len(order) - 1
            fallback = [model for model in sorted(live & set(ranked)) if ranked[model].get('tier') in order
                        and order.index(ranked[model]['tier']) <= ceiling and compatible(ready, model)]
            if fallback:
                kind = tiers.get('seat_kind', {}).get(ready, 'judge')
                chosen = (fallback[0], tiers.get('default_effort', {}).get(kind, 'xhigh'))
                warnings.append('임시 배정 %s: 기본 후보 없음, %s' % (ready, chosen[0]))
            else:
                warnings.append('임시 미배정 %s: 살아 있는 같은 등급 이상 모델이 제약을 충족하지 않음' % ready)
                continue
        assignments[ready] = chosen[0]
        template = templates[ready]
        allowed = {skill for route in routing['routes'] if route['role'] == ready for skill in route['skills']
                   if skill in installed}
        allowed.update(set(template['skills']) & installed)
        if 'korean-protocol' in installed:
            allowed.add('korean-protocol')
        roles[ready] = dict(template, model=chosen[0], effort=chosen[1], skills=sorted(allowed))
    orchestrator = next((model for model, _ in seats.get('orchestrator', []) if model in live and model in ranked), None)
    if orchestrator is None:
        warnings.append('임시 미배정 orchestrator: 살아 있는 후보 없음')
    # The onboarding agent fills method, judge_model and reviewer_model with what actually happened.
    settings = dict(load_settings(), **{'version': 1, 'orchestrator': orchestrator,
                'onboarding': {'method': '', 'judge_model': '', 'reviewer_model': '',
                               'tiers_version': tiers.get('version', ''), 'date': now().date().isoformat()}})
    pruned = json.loads(json.dumps(routing))
    dropped = 0
    for route in pruned['routes']:
        kept = [skill for skill in route['skills'] if skill in installed]
        dropped += len(route['skills']) - len(kept)
        route['skills'] = kept
    if dropped:
        warnings.append('미설치 스킬 %d개를 경로 지도에서 뺐다' % dropped)
    for row in detected.get('capabilities') or []:
        if row.get('installed'):
            continue
        others = list(row.get('substitutes') or []) + ['mcp:' + name for name in row.get('mcp') or []]
        if others:
            warnings.append('권장 도구 대체 확인 %s: %s 미설치, 대체 후보 %s' % (row.get('capability'), row.get('recommended'), ', '.join(others)))
        else:
            warnings.append('권장 도구 설치 권유 %s: %s (%s)' % (row.get('capability'), row.get('recommended'), row.get('source')))
    if (detected.get('host') or {}).get('inline_agents'):
        warnings.append('config.toml 최상위 agents 한 줄 표: 동의를 받아 cx config convert-agents로 바꾼 뒤 적용한다')
    return {'version': 1, 'routing': pruned, 'roles': {'roles': roles}, 'settings': settings,
            'notes': '\n'.join('등급표 밖 모델: ' + model for model in unknown), 'entry': load_settings()['entry'], 'warnings': warnings}


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get('version') != 1:
        raise SystemExit('invalid plan version')
    load_routing(data=plan.get('routing', {}))
    roles = load_roles(data=plan.get('roles', {}))
    settings = validate_settings(plan.get('settings', {}))
    if plan.get('entry') not in ('agents_md', 'none') or not isinstance(plan.get('notes'), str):
        raise SystemExit('invalid plan entry or notes')
    if not isinstance(plan.get('warnings'), list) or not all(isinstance(row, str) for row in plan['warnings']):
        raise SystemExit('invalid plan warnings')
    for rule in load_tiers().get('constraints', []):
        if rule['role'] in roles:
            for other in rule.get('differs_from', []):
                if other in roles and roles[rule['role']]['model'] == roles[other]['model']:
                    raise SystemExit('plan violates role constraint: ' + rule['role'])
    return roles, settings


def command_onboard(args):
    if args.action == 'detect':
        print(json.dumps(onboard_detect(), ensure_ascii=False, indent=1))
        return
    if args.action == 'propose':
        if not args.routing:
            raise SystemExit('onboard propose requires --routing')
        plan = propose_plan(load_routing(args.routing), onboard_detect())
        text = json.dumps(plan, ensure_ascii=False, indent=1) + '\n'
        if args.out:
            atomic_write(Path(args.out), text)
        else:
            print(text, end='')
        return
    if not args.plan:
        raise SystemExit('onboard apply requires --plan')
    plan = json_object(Path(args.plan))
    roles, settings = validate_plan(plan)
    settings = dict(settings, entry=plan['entry'])
    if plan['entry'] == 'agents_md':
        entry_source()
    command_roles(argparse.Namespace(dry_run=True), roles)
    home = user_home()
    if args.dry_run:
        print('onboard apply: dry-run')
        return
    root = state() / 'local'
    expected = {name: file_hash(owned_path(root / name)) for name in OVERLAY_FILES}
    with change_units():
        for name, value in [('routing.json', plan['routing']), ('roles.json', plan['roles']), ('settings.json', settings)]:
            atomic_write(root / name, json.dumps(value, ensure_ascii=False, indent=1) + '\n', expected[name])
        atomic_write(root / 'notes.md', plan['notes'], expected['notes.md'])
        command_roles(argparse.Namespace(dry_run=False), roles)
        command_entry(argparse.Namespace(action='install' if plan['entry'] == 'agents_md' else 'remove'))
    print('onboard applied: ' + str(root))


def unregister_roles(config, names):
    owned = owned_registrations(config, require_file=True)
    removed = {name: row for name, row in owned.items() if name in names}
    require_plain_registrations(removed)
    candidate = remove_blocks(config, removed)
    region = roles_region(candidate)
    remaining = owned_registrations(candidate).values() if region else ()
    if region and not any(region[0] <= start < end <= region[1] for start, end, _ in remaining):
        # Remove only the markers; a table another tool put inside the region stays.
        candidate = apply_edits(candidate, [(start, end, '') for start, end in roles_marker_spans(candidate)])
    validate_config_change(config, candidate, removed)
    return candidate


def command_uninstall(args):
    home = user_home()
    directory = owned_path(home / '.codex/agents')
    generated = [path for path in directory.glob('*.toml') if generated_file(path)]
    config = user_file(home / '.codex/config.toml')
    expected = file_hash(config)
    text = config.read_bytes().decode('utf-8') if config.exists() else ''
    updated = unregister_roles(text, set(owned_registrations(text, require_file=True)))
    # Skills turned off by cx skills off come back on with the uninstall.
    updated = skills_region_candidate(updated, set())
    if not args.dry_run:
        role_hashes = {path: file_hash(path) for path in generated}
        with change_units({'config.toml': config, 'AGENTS.md': home / '.codex/AGENTS.md'}):
            command_entry(argparse.Namespace(action='remove'))
            if updated != text:
                atomic_write(config, updated, expected)
            for path in generated:
                atomic_remove(path, role_hashes[path])
    print('uninstall%s: generated roles %d; retained state: %s' % (' dry-run' if args.dry_run else '', len(generated), state()))


# ---- skills off/on (owned [[skills.config]] region) -------------------------

SKILLS_BEGIN = '# conductor:skills:begin v1'
SKILLS_END = '# conductor:skills:end'


def skills_config_blocks(config):
    blocks = []
    for tokens, start, end in config_statements(config):
        if tokens[0][0] == '[':
            array = len(tokens) > 1 and tokens[1][0] == '['
            key = toml_key(tokens[2:-2] if array else tokens[1:-1])
            blocks.append([start, end, array and key == ('skills', 'config')])
        elif blocks:
            blocks[-1][1] = end
    return [(start, end, config[start:end]) for start, end, wanted in blocks if wanted]


def owned_skill_offs(config):
    """Region markers and the exact entries cx skills off writes inside them: path plus enabled = false."""
    spans = region_marker_spans(config, SKILLS_BEGIN, SKILLS_END, 'skills')
    if not spans:
        return [], []
    rows = []
    for start, end, text in skills_config_blocks(config):
        if not spans[0][0] <= start < end <= spans[1][1]:
            continue
        found = dict(config_declarations(text))
        enabled, value = found.get(('skills', 'config', 'enabled')), found.get(('skills', 'config', 'path'))
        if (set(found) == {('skills', 'config'), ('skills', 'config', 'path'), ('skills', 'config', 'enabled')}
                and enabled and enabled[0][1] == 'false' and value and value[0][0] == 'string'):
            rows.append((start, end, toml_string(value[0][1])))
    return spans, rows


def skills_region_candidate(config, paths):
    spans, rows = owned_skill_offs(config)
    if not spans and not paths:
        return config
    newline = '\r\n' if '\r\n' in config else '\n'
    added_newline = bool(spans and config[spans[0][0]:spans[0][1]].rstrip('\r\n') == SKILLS_BEGIN + ' +newline')
    edits = [(start, end, '') for start, end in spans] + [(start, end, '') for start, end, _ in rows]
    if added_newline:
        start, end, _ = edits[0]
        edits[0] = (start - len(newline), end, '')
    rest = apply_edits(config, edits)
    if paths:
        body = ''.join('[[skills.config]]%spath = %s%senabled = false%s' % (newline, json.dumps(item, ensure_ascii=False), newline, newline)
                       for item in sorted(paths))
        needs_newline = bool(rest and not rest.endswith('\n'))
        begin = SKILLS_BEGIN + (' +newline' if needs_newline else '')
        rest += (newline if needs_newline else '') + begin + newline + body + SKILLS_END + newline
    def outside(text):
        marks, owned = owned_skill_offs(text)
        stripped = apply_edits(text, [(s, e, '') for s, e in marks] + [(s, e, '') for s, e, _ in owned])
        return [(path, None if tokens is None else [token[:2] for token in tokens]) for path, tokens in config_declarations(stripped)]
    if outside(config) != outside(rest):
        raise SystemExit('refusing to change config.toml outside the conductor skills region')
    if tomllib is not None:
        try:
            tomllib.loads(rest)
        except (ValueError, TypeError) as error:
            raise SystemExit('invalid config.toml: ' + str(error))
    return rest


def resolve_skill_target(home, value):
    if value.endswith('SKILL.md') or '/' in value:
        return str(Path(os.path.expanduser(value)).absolute())
    found = sorted({path for name, path in role_skills(home) if name == value or name.split(':')[-1] == value})
    if not found:
        raise SystemExit('skill not found: ' + value)
    if len(found) > 1:
        raise SystemExit('several copies; pass one SKILL.md path:\n' + '\n'.join(found))
    return str(Path(found[0]))


def command_skills(args):
    home = user_home()
    path = user_file(home / '.codex/config.toml')
    expected = file_hash(path)
    text = path.read_bytes().decode('utf-8') if path.exists() else ''
    ours = {row[2] for row in owned_skill_offs(text)[1]}
    if args.action == 'list':
        print('\n'.join('off: ' + item for item in sorted(ours)) or 'off: none')
        return
    if not args.target:
        raise SystemExit('skills %s requires a skill name or SKILL.md path' % args.action)
    target = resolve_skill_target(home, args.target)
    _, disabled_paths = disabled_skills(home)
    resolved = str(Path(target).resolve())
    matching = {item for item in ours if str(Path(item).resolve()) == resolved}
    elsewhere = resolved in disabled_paths and not matching
    if args.action == 'off':
        if matching or elsewhere:
            print('변경 없음: 이미 꺼져 있다: ' + target)
            return
        updated = skills_region_candidate(text, ours | {target})
    else:
        if elsewhere:
            raise SystemExit('turned off outside the conductor region; edit config.toml by hand: ' + target)
        if not matching:
            print('변경 없음: 꺼져 있지 않다: ' + target)
            return
        updated = skills_region_candidate(text, ours - matching)
    if args.dry_run:
        print('skills %s dry-run: %s' % (args.action, target))
        return
    with change_units({'config.toml': path}):
        atomic_write(path, updated, expected)
    print('skills %s: %s' % (args.action, target))


# ---- maintainer: install and deploy the engine from a local checkout -------

ENGINE_FOLDERS = ('kp-conductor', 'kp-conductor-onboard')


def maintainer_settings():
    data = load_settings().get('maintainer')
    if not data:
        raise SystemExit('settings.maintainer is not set')
    return Path(os.path.expanduser(data['repo'])), data.get('hosts', [])


def repo_state(repo):
    head = run(['git', '-C', str(repo), 'rev-parse', 'HEAD'])
    dirty = run(['git', '-C', str(repo), 'status', '--porcelain'])
    if head is None or dirty is None:
        raise SystemExit('not a git checkout: ' + str(repo))
    return head.strip(), dirty.strip()


def repo_tests(repo):
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as conductor:
        env = dict(os.environ, HOME=home, CONDUCTOR_HOME=conductor)
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests'], cwd=str(repo),
                                env=env, capture_output=True, text=True, timeout=1800)
    lines = (result.stderr or result.stdout or '').strip().splitlines()
    return result.returncode == 0, ' '.join(line for line in lines[-3:] if line.strip())


def export_head(repo, head, target):
    import tarfile
    result = subprocess.run(['git', '-C', str(repo), 'archive', head], capture_output=True, timeout=120)
    if result.returncode != 0:
        raise SystemExit('git archive failed: ' + (result.stderr or b'').decode('utf-8', errors='replace')[-300:])
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode='r:') as archive:
        options = {'filter': 'data'} if sys.version_info >= (3, 12) else {}
        archive.extractall(str(target), **options)
    return target


def rollback_engine(folders, backup, done):
    skills = owned_path(user_home() / '.codex' / 'skills')
    failed = []
    for name in folders:
        # Keep restoring the other folders when one of them fails, then report every failure.
        try:
            target, saved = owned_path(skills / name), backup / name
            if name in done and target.exists():
                os.rename(str(target), str(backup / ('new-' + name)))
            if saved.exists() and not target.exists():
                os.rename(str(saved), str(target))
        except (OSError, SystemExit) as error:
            failed.append('%s (%s)' % (name, error))
    if failed:
        print('FAIL engine rollback incomplete, copies stay in %s: %s' % (backup, '; '.join(failed)))
    return failed


def install_engine(source_root, folders, stamp):
    skills = owned_path(user_home() / '.codex' / 'skills')
    # Validate every destination before moving any old folder, including dangling links.
    for name in folders:
        owned_path(skills / name)
    skills.mkdir(parents=True, exist_ok=True)
    backup = owned_path(state() / 'backups' / ('engine-' + stamp))
    backup.mkdir(parents=True, mode=0o700)
    done = []
    try:
        for name in folders:
            source = source_root / name
            if not source.is_dir():
                raise SystemExit('missing in repository: ' + str(source))
            target = owned_path(skills / name)
            staging = owned_path(skills / ('.%s-%s' % (name, stamp)))
            shutil.copytree(str(source), str(staging), ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            if target.exists():
                os.rename(str(target), str(backup / name))
            os.rename(str(staging), str(target))
            done.append(name)
    except BaseException:
        rollback_engine(folders, backup, done)
        for leftover in skills.glob('.*-' + stamp):
            shutil.rmtree(str(leftover), ignore_errors=True)
        raise
    return backup


def engine_command(*arguments):
    script = user_home() / '.codex/skills/kp-conductor/scripts/conductor.py'
    command = [sys.executable, str(script), '--home', str(user_home())] + list(arguments)
    if arguments == ('entry', 'install'):
        # The parent receive holds the mutation lock. Run the installed entry handler
        # in a subprocess without taking that same lock a second time.
        loader = ("import argparse, pathlib, runpy, sys; "
                  "engine = runpy.run_path(sys.argv[1]); "
                  "handler = engine['command_entry']; "
                  "handler.__globals__['HOME_OVERRIDE'] = pathlib.Path(sys.argv[3]); "
                  "handler(argparse.Namespace(action='install', dry_run=False))")
        command = [sys.executable, '-c', loader] + command[1:]
    return subprocess.run(command, env=dict(os.environ, HOME=str(user_home())),
                          capture_output=True, text=True, timeout=120)


def receive_engine(source_root, stamp, commit=None):
    source_root = Path(source_root)
    folders = list(ENGINE_FOLDERS) + ([] if (user_home() / '.agents/skills/korean-protocol').exists() else ['korean-protocol'])
    record = owned_path(state() / 'engine.json')
    expected = file_hash(record)
    installed = read_json(record, {}) if record.exists() else {}
    installed = installed if isinstance(installed, dict) else {}
    # Read everything a rollback needs before the first folder moves.
    agents = user_file(user_home() / '.codex/AGENTS.md')
    try:
        agents_before = agents.read_bytes() if agents.exists() else None
    except OSError as error:
        raise SystemExit('cannot read AGENTS.md before install, nothing changed: %s' % error)
    backup = install_engine(source_root, folders, stamp)
    entry_changed = False
    try:
        status = engine_command('entry', 'status')
        if status.returncode != 0:
            raise SystemExit('entry status failed: ' + (status.stderr or status.stdout or '')[-300:])
        if 'outdated' in (status.stdout or ''):
            entry_changed = True
            entry = engine_command('entry', 'install')
            if entry.returncode != 0:
                raise SystemExit('entry install failed: ' + (entry.stderr or entry.stdout or '')[-300:])
        check = engine_command('doctor')
        crashed = check.returncode not in (0, 1) or 'Traceback (most recent call last)' in (check.stderr or '')
        if crashed:
            print(check.stderr or check.stdout or 'doctor crashed')
            raise SystemExit('doctor did not finish: ' + (check.stderr or check.stdout or '')[-300:])
        if check.returncode != 0:
            failures = '\n'.join(line for line in (check.stdout or '').splitlines() if line.startswith('FAIL'))
            labels = set(re.findall(r'^FAIL (.+?): ', check.stdout or '', re.M))
            # ocx and the proxy belong to the machine, as in overlay verify; they do not judge the engine.
            environment = {'ocx', 'proxy'} | ({'routed models in catalog'} if 'proxy' in labels else set())
            # A machine that never had the entry block installed is not broken by this install.
            if 'absent' in (status.stdout or ''):
                environment.add('AGENTS.md pointer')
            if not labels or labels - environment:
                print(failures or check.stderr or check.stdout or 'doctor failed')
                raise SystemExit(failures or 'doctor failed: ' + (check.stderr or '')[-300:])
            print('WARN environment checks failed after install, engine kept: ' + ', '.join(sorted(labels)))
        version_path = source_root / 'kp-conductor/VERSION'
        version = version_path.read_text(encoding='utf-8').strip() if version_path.exists() else None
        data = dict(installed, commit=commit, version=version, installed_at=now().isoformat(), folders=folders, backup=str(backup))
        atomic_write(record, json.dumps(data, ensure_ascii=False, indent=1) + '\n', expected, mode=0o600)
    except BaseException:
        rollback_engine(folders, backup, folders)
        if entry_changed:
            # Put back the exact AGENTS.md bytes from before this install.
            try:
                if agents_before is not None:
                    atomic_write(agents, agents_before, file_hash(agents))
            except (OSError, SystemExit) as error:
                print('FAIL AGENTS.md restore: %s' % str(error)[-300:])
        raise
    print('maintainer receive: %s%s; previous copy in %s' % ((commit or 'unknown')[:7], ' v' + version if version else '', backup))
    return backup


def command_maintainer(args):
    import shlex
    stamp = now().strftime('%Y%m%d-%H%M%S-%f')
    if args.action == 'receive':
        if not args.source:
            raise SystemExit('maintainer receive requires --source')
        source = Path(args.source).expanduser()
        if args.dry_run:
            print('maintainer receive dry-run: %s -> %s' % (source, user_home() / '.codex/skills'))
            return
        try:
            return receive_engine(source, stamp, args.commit)
        finally:
            staging = lexical_path(state() / 'staging')
            candidate = lexical_path(source)
            if candidate != staging and staging in candidate.parents and candidate.exists():
                shutil.rmtree(str(owned_path(candidate)))
    repo, hosts = maintainer_settings()
    head, dirty = repo_state(repo)
    branch = run(['git', '-C', str(repo), 'rev-parse', '--abbrev-ref', 'HEAD'])
    branch = (branch or '').strip()
    expected_branch = load_settings()['maintainer'].get('branch', 'main')
    record = owned_path(state() / 'engine.json')
    installed = read_json(record, {}) if record.exists() else {}
    installed = installed if isinstance(installed, dict) else {}
    if args.action == 'status':
        print('repo: %s' % repo)
        print('head: %s%s' % (head[:7], ' (uncommitted changes)' if dirty else ''))
        print('branch: %s (required: %s)' % (branch, expected_branch))
        print('installed: %s%s' % ((installed.get('commit') or 'none')[:7], ' v' + installed['version'] if installed.get('version') else ''))
        for host in hosts:
            print('host %s (%s): %s' % (host['name'], host['ssh'], ((installed.get('hosts') or {}).get(host['name']) or 'none')[:7]))
        return
    if dirty:
        raise SystemExit('commit the repository changes first: ' + str(repo))
    if branch != expected_branch:
        raise SystemExit('branch %s does not match required branch %s' % (branch, expected_branch))
    if args.action == 'install':
        if args.dry_run:
            print('maintainer install dry-run: export %s, test export, receive -> %s' % (head[:7], user_home() / '.codex/skills'))
            return
        with tempfile.TemporaryDirectory(prefix='conductor-export-') as temporary:
            exported = export_head(repo, head, Path(temporary) / 'export')
            passed, summary = repo_tests(exported)
            print('tests: %s %s' % ('passed' if passed else 'failed', summary))
            if not passed:
                raise SystemExit(1)
            receive_engine(exported / 'skills', stamp, head)
        return
    host = next((row for row in hosts if row['name'] == args.host), None)
    if host is None:
        raise SystemExit('unknown host: %s' % args.host)
    if installed.get('commit') != head:
        raise SystemExit('install this commit here first: cx maintainer install')
    remote_relative = '.codex/kp-conductor/staging/engine-' + stamp
    remote = '~/' + shlex.quote(remote_relative)
    def commands(exported):
        return [
            ('staging', ['ssh', host['ssh'], 'mkdir -p ' + remote]),
            ('rsync', ['rsync', '-a', '--exclude', '__pycache__', str(exported / 'skills') + '/',
                       '%s:%s/' % (shlex.quote(host['ssh']), shlex.quote(remote_relative))]),
            ('receive', ['ssh', host['ssh'], 'python3 %s/kp-conductor/scripts/conductor.py maintainer receive --source %s --commit %s'
                         % (remote, remote, shlex.quote(head))]),
        ]
    if args.dry_run:
        print('\n'.join(shlex.join(command) for _, command in commands(Path('<export>'))))
        return
    with tempfile.TemporaryDirectory(prefix='conductor-export-') as temporary:
        try:
            exported = export_head(repo, head, Path(temporary) / 'export')
        except (OSError, subprocess.TimeoutExpired, SystemExit) as error:
            raise SystemExit('deploy failed at export: ' + str(error)[-300:])
        for stage, command in commands(exported):
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=600)
            except (OSError, subprocess.TimeoutExpired) as error:
                detail = getattr(error, 'stderr', None) or str(error)
                if isinstance(detail, bytes):
                    detail = detail.decode('utf-8', errors='replace')
                raise SystemExit('deploy failed at %s: %s' % (stage, detail.strip()[-300:]))
            if result.returncode != 0:
                raise SystemExit('deploy failed at %s: %s' % (stage, (result.stderr or '').strip()[-300:]))
            if stage == 'receive':
                # Pass on the remote warnings (environment checks, absent entry block) for the report.
                remote = '\n'.join(line for line in (result.stdout or '').splitlines() if line.startswith(('WARN', 'FAIL', 'maintainer receive')))
                if remote:
                    print('remote: ' + remote.replace('\n', '\nremote: '))
    data = dict(installed, hosts=dict(installed.get('hosts') or {}, **{host['name']: head}))
    atomic_write(record, json.dumps(data, ensure_ascii=False, indent=1) + '\n', file_hash(record), mode=0o600)
    print('maintainer deploy: %s to %s' % (head[:7], host['name']))


# ---- lineup and Artificial Analysis ---------------------------------------

def known_models():
    return load_tiers().get('models', {})


def live_models():
    try:
        with urllib.request.urlopen(load_settings()['proxy'].rstrip('/') + '/v1/models', timeout=8) as response:
            data = json.load(response)
        rows = data['data']
        if not isinstance(rows, list):
            return None
        return sorted({row['id'].split('--')[0] for row in rows if not row['id'].startswith('combo/')})
    except (Exception, SystemExit):
        return None


def lineup_diff(live, known):
    return {'new': sorted(set(live) - set(known)), 'missing': sorted(set(known) - set(live))}


def aa_variants(page, slug):
    chunks = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', page)
    payload = ''.join(json.loads(chunk) for chunk in chunks)
    decoder, found = json.JSONDecoder(), {}
    for match in re.finditer(r'"(?:initialModels|models)":\[\{"id"', payload):
        try:
            rows, _ = decoder.raw_decode(payload[payload.index('[', match.start()):])
        except ValueError:
            continue
        for row in rows:
            release = row.get('release') if isinstance(row, dict) else None
            if isinstance(release, dict) and release.get('slug') == slug and 'intelligenceIndex' in row:
                found[row.get('shortName') or row.get('name')] = row
    return found


def aa_row(label, row):
    match = re.match(r'(.*) \((.*)\)$', label)
    cost = row.get('intelligenceIndexCostPerTask') or {}
    cost = (cost.get('cost') or {}).get('total') if isinstance(cost.get('cost'), dict) else cost.get('total')
    tokens = (row.get('intelligenceIndexOutputTokensPerTask') or {}).get('output')
    seconds = row.get('intelligenceIndexTimePerTask')
    scores = row.get('capabilityScores') or {}

    def rounded(value, digits=None):
        return '' if value is None else round(value, digits) if digits else round(value)

    return {'model': match.group(1) if match else label, 'effort': match.group(2) if match else '',
            'intelligence_index': rounded(row.get('intelligenceIndex'), 1), 'cost_per_task_usd': rounded(cost, 2),
            'time_per_task_min': rounded(seconds / 60 if seconds else None, 1), 'output_tokens_per_task': rounded(tokens),
            'finance_accounting': rounded(scores.get('financeAndAccounting')), 'strategy_ops': rounded(scores.get('strategyAndOps')),
            'legal': rounded(scores.get('legal')), 'healthcare': rounded(scores.get('healthcareAndMedical')),
            'engineering': rounded(scores.get('engineering')), 'economics': rounded(scores.get('economics'))}


def aa_diff(old_rows, new_rows):
    old = {(row['model'], row['effort']): row for row in old_rows}
    new = {(row['model'], row['effort']): row for row in new_rows}
    changed = []
    for key in sorted(set(old) & set(new)):
        try:
            before, after = float(old[key]['intelligence_index']), float(new[key]['intelligence_index'])
        except (TypeError, ValueError):
            continue
        if abs(after - before) >= 1:
            changed.append('%s (%s): %.1f -> %.1f' % (key + (before, after)))
    return {'added': ['%s (%s)' % key for key in sorted(set(new) - set(old))],
            'removed': ['%s (%s)' % key for key in sorted(set(old) - set(new))], 'changed': changed}


def aa_refresh(extra_slugs):
    slugs = sorted({row['aa_slug'] for row in known_models().values() if row.get('aa_slug')} | set(extra_slugs))
    rows, failed = [], []
    for slug in slugs:
        try:
            request = urllib.request.Request('https://artificialanalysis.ai/models/releases/' + slug,
                                             headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(request, timeout=45) as response:
                variants = aa_variants(response.read().decode('utf-8', 'replace'), slug)
        except Exception as error:
            failed.append('%s: %s' % (slug, str(error)[:80]))
            continue
        if not variants:
            failed.append(slug + ': no variants parsed')
        rows += [aa_row(label, row) for label, row in variants.items()]
    rows.sort(key=lambda row: -(row['intelligence_index'] or 0))
    directory = owned_path(state() / 'aa')
    previous = sorted(directory.glob('aa-*.csv'), key=lambda path: (path.stat().st_mtime_ns, path.name))
    reference = []
    if previous:
        with previous[-1].open() as handle:
            reference = list(csv.DictReader(handle))
    target = directory / ('aa-%s.csv' % now().strftime('%Y-%m-%d'))
    with io.StringIO(newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=AA_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        atomic_write(target, handle.getvalue())
    return target, rows, failed, aa_diff(reference, rows)


# ---- commands --------------------------------------------------------------

def command_status(args):
    text = run([ocx_path(), 'provider', 'quota'], 20)
    quota, burn = parse_quota(text), quota_burn(text)
    print('quota: ' + ('; '.join('%s %s' % (name, ' '.join('%s %g%%' % item for item in windows.items()))
                                 for name, windows in quota.items()) if quota else 'unavailable'))
    print('burn: ' + ('; '.join('%s %s' % (name, ' '.join('%s %.2f' % item for item in windows.items()))
                                for name, windows in burn.items()) if burn else 'unavailable'))
    expansion = expansion_state(quota, burn)
    print('expansion: ' + ('; '.join('%s %s' % item for item in expansion.items()) if expansion else 'unavailable'))
    for line in quota_advice(quota, burn):
        print('advice: ' + line)
    _, checkpoint = load_checkpoint(args.project)
    print('checkpoint: ' + ('%s "%s" %s (id %s)' % (checkpoint['updated'], checkpoint['title'],
                                                    progress_line(checkpoint), checkpoint['id'])
                            if checkpoint else 'none'))
    profile = read_profile(args.project)
    print('profile: ' + ('mode=%s risk=%s domain=%s by=%s verify=%s' % tuple(profile.get(key, '-') for key in ('mode', 'risk', 'domain', 'by', 'verify'))
                         + (' saving=true' if profile.get('saving') else '')
                         if profile else 'none'))
    last = state() / 'retro' / 'last.json'
    days = (now() - datetime.datetime.fromisoformat(json.loads(last.read_text())['ts'])).days if last.exists() else None
    try:
        onboarded = datetime.date.fromisoformat((load_settings().get('onboarding') or {}).get('date') or '')
        since = (now().date() - onboarded).days
    except (TypeError, ValueError):
        since = None
    # The first retro is due a week after onboarding, so a fresh install does not start with one.
    print('retro: ' + ('ok, first run in %d days' % (7 - since) if days is None and since is not None and since < 7
                       else 'never run, due' if days is None
                       else 'due, last run %d days ago' % days if days >= 7 else 'ok, last run %d days ago' % days))
    print(skills_status())
    print(proposals_status())
    live = live_models()
    diff = lineup_diff(live, known_models()) if live is not None else None
    print('lineup: ' + ('unavailable' if diff is None else 'no change' if not (diff['new'] or diff['missing'])
                        else 'new=%s missing=%s' % (','.join(diff['new']) or '-', ','.join(diff['missing']) or '-')))
    settings = load_settings()
    roles = load_roles()
    print('roles: ' + '; '.join('%s %s %s [%s]' % (name, role['model'], role['effort'], ','.join(model_quota_keys(role['model'], quota))) for name, role in roles.items()))
    print('overlay: ' + ('present' if any((state() / 'local' / name).is_file() for name in OVERLAY_FILES) else 'absent'))
    print('entry: ' + entry_status(user_home()))
    onboarding = settings.get('onboarding')
    print('onboarding: ' + ('needed' if not onboarding else
                            'method=%s judge=%s reviewer=%s date=%s%s' % (
                                tuple(onboarding.get(key) or '-' for key in ('method', 'judge_model', 'reviewer_model', 'date'))
                                + ('' if onboarding.get('method') else ' (기록 미완료)',))))
    print('retro_apply: ' + settings['retro']['apply'])


def command_checkpoint(args):
    if args.action == 'start':
        auto_profile = not profile_path(args.project).exists()
        data = checkpoint_start(args.project, args.title, args.steps, args.type, args.mode, args.mode_source, args.visual)
        print('checkpoint %s' % data['id'])
        if auto_profile:
            print('profile: auto-created')
    else:
        accepted = {'yes': True, 'no': False, 'unknown': None}
        data = checkpoint_update(args.project, args.id, args.done if args.action == 'update' else None,
                                 args.note, args.outcome if args.action == 'update' else args.outcome or 'pass',
                                 args.delegated, args.skills, args.skill_fit,
                                 judge_calls=args.judge_calls,
                                 review_changed=None if args.review_changed is None else args.review_changed == 'yes',
                                 visual_iterations=args.visual_iterations, visual_defects=args.visual_defects,
                                 visual_accepted=accepted[args.visual_accepted] if args.visual_accepted is not None else UNSET)
    print(progress_line(data) + (' [%s]' % data['outcome'] if data['outcome'] else ''))


def command_receipt(args):
    append_receipt({'ts': now().isoformat(timespec='seconds'), 'project': str(Path(args.project).expanduser().resolve()),
                    'type': args.type, 'model': args.model, 'effort': args.effort, 'outcome': args.outcome,
                    'run': args.run, 'note': args.note, 'classified': True})
    print('receipt recorded')


def command_profile(args):
    if args.action == 'show':
        print(json.dumps(read_profile(args.project), ensure_ascii=False, indent=1))
        return
    path = profile_path(args.project)
    mode, saving = normalize_mode(args.mode)
    expected = file_hash(owned_path(path))
    profile = {'project': str(Path(args.project).expanduser().resolve()), 'mode': mode, 'saving': saving, 'risk': args.risk,
               'domain': args.domain, 'verify': args.verify, 'note': args.note, 'by': 'judge' if args.by == 'fable' else args.by,
               'updated': now().isoformat(timespec='seconds')}
    atomic_write(path, json.dumps(profile, ensure_ascii=False, indent=1), expected)
    print('profile saved: ' + str(path))


def command_retro(args):
    if args.sample < 0:
        raise SystemExit('sample must be nonnegative')
    report = retro_report(args.days)
    directory = state() / 'retro'
    owned_path(directory)
    target = directory / (now().strftime('%Y-%m-%d') + '.md')
    atomic_write(target, report)
    atomic_write(directory / 'last.json', json.dumps({'ts': now().isoformat(timespec='seconds'), 'report': str(target)}))
    print(report + '\nsaved: ' + str(target))
    suggestions = [line for line in report.splitlines() if line.startswith(('제안:', '프론티어 비율 '))]
    if load_settings()['retro']['apply'] == 'propose' and suggestions:
        proposals = state() / 'proposals'
        owned_path(proposals)
        proposal = proposals / (now().strftime('%Y%m%d-%H%M%S-%f') + '.md')
        atomic_write(proposal, '상태: 제안\n\n' + '\n'.join(suggestions) + '\n')
        print('proposal: ' + str(proposal))
    if args.sample:
        sample = directory / ('sample-' + now().strftime('%Y-%m-%d') + '.jsonl')
        rows = sample_requests(args.days, args.sample)
        atomic_write(sample, ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
        print('sample: %s (%d requests)' % (sample, len(rows)))


def command_lineup(args):
    live = live_models()
    if live is None:
        raise SystemExit('OpenCodex proxy is unreachable')
    print(json.dumps(lineup_diff(live, known_models()), ensure_ascii=False))


def command_aa(args):
    target, rows, failed, diff = aa_refresh(args.slug or [])
    print('saved %d rows: %s' % (len(rows), target))
    print(json.dumps(dict(diff, failed=failed), ensure_ascii=False, indent=1))
    if failed:
        raise SystemExit(1)


def command_backup(args):
    with change_units() as snapshot:
        stamp = now()
        target = owned_path(state() / 'backups' / stamp.strftime('%Y%m%d-%H%M%S'))
        while target.exists():
            stamp += datetime.timedelta(seconds=1)
            target = target.with_name(stamp.strftime('%Y%m%d-%H%M%S'))
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.chmod(0o700)
        target.mkdir(mode=0o700)
        for root, prefix in ((SKILL, Path()), (snapshot, Path('overlay'))):
            for source in root.rglob('*'):
                relative = prefix / source.relative_to(root)
                destination = target / relative
                if source.is_dir():
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination.chmod(0o700)
                elif source.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    atomic_write(destination, source.read_bytes(), expected=None, mode=0o600)
        for name in ('config.toml', 'AGENTS.md'):
            if (snapshot / name).is_file():
                atomic_write(target / name, (snapshot / name).read_bytes(), expected=None, mode=0o600)
    print('backup: ' + str(target))
    print('scope: engine; personal settings; generated role files; owned registrations; conductor entry block; config.toml; global AGENTS.md')


LAST_DOCTOR_PROBLEMS = []


def command_doctor(args):
    global LAST_DOCTOR_PROBLEMS
    problems = LAST_DOCTOR_PROBLEMS = []
    home = user_home()

    def check(ok, label, detail):
        if not ok:
            problems.append(label)
        print('%s %s: %s' % ('OK  ' if ok else 'FAIL', label, detail))

    check(True, 'engine', engine_version())
    try:
        settings = load_settings()
        check(True, 'settings.json', 'valid')
    except SystemExit as error:
        check(False, 'settings.json', str(error))
        settings = DEFAULT_SETTINGS
    try:
        load_tiers()
        check(True, 'tiers.json', 'valid')
    except SystemExit as error:
        check(False, 'tiers.json', str(error))
    try:
        executable = ocx_path()
    except SystemExit:
        executable = str(home / '.local/bin/ocx')
    check(Path(executable).is_file() and os.access(executable, os.X_OK), 'ocx', executable)
    try:
        live = live_models()
    except SystemExit:
        live = None
    check(live is not None, 'proxy', 'reachable' if live is not None else 'unreachable')
    try:
        roles = load_roles()
        check(True, 'roles.json', 'valid')
    except SystemExit as error:
        check(False, 'roles.json', str(error))
        roles = {}
    routed = sorted({role['model'] for role in roles.values() if role.get('model')})
    missing = [model for model in routed if model not in (live or [])]
    check(not missing, 'routed models in catalog', 'all present' if not missing else 'missing ' + ', '.join(missing))
    config = read_json(home / '.opencodex' / 'config.json', {})
    config = config if isinstance(config, dict) else {}
    roster = config.get('subagentModels') or []
    absent = [model for model in routed if model not in roster]
    print('%s routed models in subagent roster: %s' % ('WARN' if absent else 'OK  ', ', '.join(absent) or 'all present'))
    catalog = read_json(home / '.codex' / 'opencodex-catalog.json', {})
    try:
        visible = sorted((row for row in catalog['models'] if row.get('visibility') == 'list'), key=lambda row: row.get('priority', 999))
        listed = [row.get('slug') for row in visible[:5]]
        print('INFO spawn_agent model list (first five in picker order): ' + ', '.join(listed))
        print('INFO routed models outside that list, still accepted when named: ' + (', '.join(model for model in routed if model not in listed) or 'none'))
    except (KeyError, TypeError):
        print('INFO spawn_agent model list: catalog unreadable')
    advisor = config.get('advisor') or {}
    print('INFO advisor: %s %s %s' % (advisor.get('model'), advisor.get('effort'), advisor.get('policy')))
    print('INFO subagent effort cap: ' + str(config.get('subagentEffortCap')))
    model = default_model(home)
    expected = settings.get('orchestrator')
    print('%s default model: %s (orchestrator=%s)' % ('OK  ' if expected is not None and model == expected else 'WARN', model or 'not set', expected))
    entry = entry_status(home)
    if settings.get('entry') == 'agents_md':
        check(entry == 'present', 'AGENTS.md pointer', entry)
    else:
        print('INFO AGENTS.md pointer: entry=none')
    installed = {name for name, _ in role_skills(home)}
    if settings.get('language') == 'ko':
        print('%s KP installed: %s' % ('OK  ' if 'korean-protocol' in installed else 'WARN', 'korean-protocol' in installed))
    present = any((state() / 'local' / name).is_file() for name in OVERLAY_FILES)
    print('%s overlay: %s' % ('OK  ' if present else 'WARN', 'present' if present else '온보딩 전'))
    for name, role in roles.items():
        path = home / '.codex/agents' / (name + '.toml')
        if path.is_file():
            text = path.read_text()
            values = dict(re.findall(r'^(model|model_reasoning_effort)\s*=\s*("(?:[^"\\]|\\.)*")\s*$', text, re.M))
            consistent = (values.get('model') == json.dumps(role['model'], ensure_ascii=False)
                          and values.get('model_reasoning_effort') == json.dumps(role['effort']))
            check(consistent, 'role declaration ' + name, 'matches' if consistent else 'model/effort mismatch')
    for name, exists, registered, drift in role_health(home, roles):
        print('%s role %s: file=%s registered=%s' % ('OK  ' if exists and registered else 'WARN', name, exists, registered))
        if drift:
            print('WARN roles drift: cx roles sync 재실행 (%s)' % name)
    for warning in role_warnings(home, roles, role_skills(home)):
        print(warning)
    try:
        routing = load_routing()
    except SystemExit as error:
        check(False, 'routing.json', str(error))
    else:
        check(True, 'routing.json', 'valid')
        route_skills = {name for route in routing['routes'] for name in route['skills']}
        missing = sorted(route_skills - installed)
        print('%s route skills installed: %s' % ('WARN' if missing else 'OK  ', ', '.join(missing) if missing else 'all present'))
        disallowed = []
        for route in routing['routes']:
            allowed = roles.get(route['role'], {}).get('skills', [])
            absent = [name for name in route['skills'] if name not in allowed]
            if absent:
                disallowed.append('%s/%s: %s' % (route['id'], route['role'], ', '.join(absent)))
        print('%s route skills allowed: %s' % ('WARN' if disallowed else 'OK  ', '; '.join(disallowed) if disallowed else 'all allowed'))
        unrouted = sorted({name for role in roles.values() for name in role['skills']} - route_skills)
        print('INFO role skills without routes: ' + (', '.join(unrouted) or 'none'))
    raise SystemExit(1 if problems else 0)


def command_kp_lint(rest):
    script = SKILL / 'scripts' / 'kp_lint.py'
    raise SystemExit(subprocess.call([sys.executable, str(script)] + list(rest)))


def main(argv=None):
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw[:1] == ['kp-lint']:
        command_kp_lint(raw[1:])
    global HOME_OVERRIDE
    home_parser = argparse.ArgumentParser(add_help=False)
    home_parser.add_argument('--home')
    home_args, raw = home_parser.parse_known_args(raw)
    parser = argparse.ArgumentParser(prog='cx')
    parser.add_argument('--version', action='version', version='cx (kp-conductor) ' + engine_version())
    sub = parser.add_subparsers(dest='command', required=True)
    status = sub.add_parser('status')
    status.add_argument('--project', default=os.getcwd())
    status.set_defaults(handler=command_status)
    checkpoint = sub.add_parser('checkpoint')
    checkpoint.add_argument('action', choices=('start', 'update', 'finish'))
    checkpoint.add_argument('--project', default=os.getcwd())
    checkpoint.add_argument('--title', default='작업')
    checkpoint.add_argument('--steps', default='')
    checkpoint.add_argument('--id')
    checkpoint.add_argument('--done', type=int)
    checkpoint.add_argument('--note')
    checkpoint.add_argument('--type', choices=tuple('ABCDE'))
    checkpoint.add_argument('--mode')
    checkpoint.add_argument('--mode-source', choices=('user', 'inferred', 'default'))
    checkpoint.add_argument('--visual', action='store_true')
    checkpoint.add_argument('--judge-calls', '--fable-calls', dest='judge_calls', type=int)
    checkpoint.add_argument('--review-changed', choices=('yes', 'no'))
    checkpoint.add_argument('--visual-iterations', type=int)
    checkpoint.add_argument('--visual-defects', type=int)
    checkpoint.add_argument('--visual-accepted', choices=('yes', 'no', 'unknown'))
    checkpoint.add_argument('--delegated', type=int)
    checkpoint.add_argument('--outcome', choices=('pass', 'partial', 'fail'))
    checkpoint.add_argument('--skills')
    checkpoint.add_argument('--skill-fit', choices=('good', 'poor', 'none'))
    checkpoint.set_defaults(handler=command_checkpoint)
    receipt = sub.add_parser('receipt')
    receipt.add_argument('--project', default=os.getcwd())
    receipt.add_argument('--type', required=True, choices=tuple('ABCDE'))
    receipt.add_argument('--model', required=True)
    receipt.add_argument('--effort', required=True)
    receipt.add_argument('--outcome', required=True, choices=('pass', 'fail', 'refused', 'error'),
                         help='refused: content refusal; error: authentication, overload, timeout or empty response')
    receipt.add_argument('--run', default='single', choices=('single', 'parallel', 'tournament'))
    receipt.add_argument('--note')
    receipt.set_defaults(handler=command_receipt)
    profile = sub.add_parser('profile')
    profile.add_argument('action', choices=('show', 'set'))
    profile.add_argument('--project', default=os.getcwd())
    profile.add_argument('--mode', default='균형', choices=('속도', '균형', '프론티어', '빠르게', '꼼꼼하게', '아껴서'))
    profile.add_argument('--risk', default='R1', choices=('R1', 'R2', 'R3'))
    profile.add_argument('--domain', default='engineering')
    profile.add_argument('--verify', default='')
    profile.add_argument('--note', default='')
    profile.add_argument('--by', default='self', choices=('judge', 'fable', 'self'))
    profile.set_defaults(handler=command_profile)
    retro = sub.add_parser('retro')
    retro.add_argument('--days', type=int, default=7, choices=(1, 7, 30))
    retro.add_argument('--sample', type=int, default=0)
    retro.set_defaults(handler=command_retro)
    audit = sub.add_parser('skills-audit')
    audit.add_argument('--days', type=int, default=30)
    audit.add_argument('--stale-days', type=int, default=30)
    audit.add_argument('--json', action='store_true')
    audit.add_argument('--remote', action='store_true')
    audit.set_defaults(handler=command_skills_audit)
    route = sub.add_parser('route')
    route.add_argument('text')
    route.add_argument('--top', type=int, default=3)
    route.add_argument('--json', action='store_true')
    route.set_defaults(handler=command_route)
    roles = sub.add_parser('roles')
    roles.add_argument('action', choices=('sync',))
    roles.add_argument('--dry-run', action='store_true')
    roles.set_defaults(handler=command_roles)
    sub.add_parser('lineup').set_defaults(handler=command_lineup)
    aa = sub.add_parser('aa-refresh')
    aa.add_argument('--slug', action='append')
    aa.set_defaults(handler=command_aa)
    sub.add_parser('backup').set_defaults(handler=command_backup)
    sub.add_parser('doctor').set_defaults(handler=command_doctor)
    sub.add_parser('kp-lint', help='KP 문체 경고 검사. 인자는 scripts/kp_lint.py로 그대로 넘긴다')
    onboard = sub.add_parser('onboard')
    onboard.add_argument('action', choices=('detect', 'propose', 'apply'))
    onboard.add_argument('--json', action='store_true')
    onboard.add_argument('--routing')
    onboard.add_argument('--out')
    onboard.add_argument('--plan')
    onboard.add_argument('--dry-run', action='store_true')
    onboard.set_defaults(handler=command_onboard)
    entry = sub.add_parser('entry')
    entry.add_argument('action', choices=('install', 'remove', 'status'))
    entry.set_defaults(handler=command_entry)
    overlay = sub.add_parser('overlay')
    overlay.add_argument('action', choices=('backup', 'restore', 'list', 'verify'))
    overlay.add_argument('--id')
    overlay.set_defaults(handler=command_overlay)
    settings = sub.add_parser('settings')
    settings.add_argument('action', choices=('show', 'set'))
    settings.add_argument('key', nargs='?')
    settings.add_argument('value', nargs='?')
    settings.set_defaults(handler=command_settings)
    uninstall = sub.add_parser('uninstall')
    uninstall.add_argument('--dry-run', action='store_true')
    uninstall.set_defaults(handler=command_uninstall)
    config_parser = sub.add_parser('config')
    config_parser.add_argument('action', choices=('convert-agents',))
    config_parser.add_argument('--dry-run', action='store_true')
    config_parser.set_defaults(handler=command_config)
    skills_parser = sub.add_parser('skills')
    skills_parser.add_argument('action', choices=('off', 'on', 'list'))
    skills_parser.add_argument('target', nargs='?')
    skills_parser.add_argument('--dry-run', action='store_true')
    skills_parser.set_defaults(handler=command_skills)
    maintainer_parser = sub.add_parser('maintainer')
    maintainer_parser.add_argument('action', choices=('status', 'install', 'deploy', 'receive'))
    maintainer_parser.add_argument('--source')
    maintainer_parser.add_argument('--commit')
    maintainer_parser.add_argument('--host')
    maintainer_parser.add_argument('--dry-run', action='store_true')
    maintainer_parser.set_defaults(handler=command_maintainer)
    args = parser.parse_args(raw)
    args.home = home_args.home
    previous = HOME_OVERRIDE
    HOME_OVERRIDE = Path(home_args.home).expanduser().absolute() if home_args.home else None
    try:
        mutating = ((args.command == 'onboard' and args.action == 'apply')
                    or (args.command == 'entry' and args.action in ('install', 'remove'))
                    or (args.command == 'overlay' and args.action in ('backup', 'restore', 'verify'))
                    or (args.command == 'settings' and args.action == 'set')
                    or (args.command == 'skills' and args.action in ('off', 'on'))
                    or (args.command == 'maintainer' and args.action in ('install', 'deploy', 'receive'))
                    or args.command in ('roles', 'uninstall', 'backup', 'config')) and not getattr(args, 'dry_run', False)
        if args.command == 'entry' and args.action == 'install':
            entry_source()
        if args.command == 'overlay' and args.action == 'verify' and not args.id:
            raise SystemExit('overlay verify requires --id')
        with mutation_lock() if mutating else contextlib.nullcontext():
            args.handler(args)
    finally:
        HOME_OVERRIDE = previous


if __name__ == '__main__':
    main()

