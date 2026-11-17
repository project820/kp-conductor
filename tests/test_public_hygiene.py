"""공개 저장소에 개인 정보가 섞이지 않았는지 검사한다.

이 파일은 공개된다. 그래서 일반 패턴만 담는다. 작성자 개인 금지어는 저장소 밖의
CONDUCTOR_HYGIENE_PATTERNS 파일(기본 ~/.codex/kp-conductor/local/hygiene-patterns.txt)에 둔다.
그 파일은 한 줄에 정규식 하나이고, #으로 시작하는 줄은 주석이다.
"""
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
GENERIC = [
    ('macOS 홈 경로', re.compile(r'/Users/(?!(?:x|me|user|example|<[^>]*>)(?:/|$))[A-Za-z0-9._-]+', re.M)),
    ('Linux 홈 경로', re.compile(r'/home/(?!(?:x|me|user|example|<[^>]*>)(?:/|$))[a-z][A-Za-z0-9._-]+', re.M)),
    ('tailnet 이름', re.compile(r'[A-Za-z0-9-]+[.]ts[.]net')),
    ('CGNAT 주소', re.compile(r'(?<![0-9.])100[.](?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])[.][0-9]{1,3}[.][0-9]{1,3}(?![0-9])')),
    ('사설 주소', re.compile(r'(?<![0-9.])(?:192[.]168|10)[.][0-9]{1,3}[.][0-9]{1,3}(?:[.][0-9]{1,3})?(?![0-9.])')),
    ('앱 id', re.compile(r'app-[0-9a-f]{32}')),
    ('이메일', re.compile(r'(?<![A-Za-z0-9._%+-])(?!git@)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+[.][A-Za-z]{2,}')),
]
TEXT_SUFFIXES = {'.md', '.json', '.py', '.yaml', '.yml', '.toml', '.txt', '.html', '.sh', ''}


def private_patterns():
    candidates = [Path.home() / '.codex' / name / 'local' / 'hygiene-patterns.txt' for name in ('kp-conductor', 'conductor')]
    path = Path(os.environ.get('CONDUCTOR_HYGIENE_PATTERNS')
                or next((item for item in candidates if item.is_file()), candidates[0]))
    if not path.is_file():
        return []
    patterns = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if line and not line.startswith('#'):
            patterns.append(('개인 금지어 %d행' % number, re.compile(line)))
    return patterns


def public_files():
    for path in sorted(ROOT.rglob('*')):
        if {'.git', '__pycache__', '.pytest_cache'} & set(path.parts):
            continue
        if path.is_file() and path.resolve() != SELF and path.suffix in TEXT_SUFFIXES:
            yield path


def scan(patterns):
    hits = []
    for path in public_files():
        text = path.read_text(errors='replace')
        for label, pattern in patterns:
            for match in pattern.finditer(text):
                line = text.count(chr(10), 0, match.start()) + 1
                hits.append('%s:%d %s' % (path.relative_to(ROOT), line, label))
    return hits


class PublicHygieneTests(unittest.TestCase):
    def test_no_generic_personal_identifiers(self):
        hits = scan(GENERIC)
        self.assertEqual(hits, [], chr(10).join(hits[:80]))

    def test_no_private_identifiers(self):
        patterns = private_patterns()
        if not patterns:
            self.skipTest('개인 금지어 파일 없음')
        hits = scan(patterns)
        self.assertEqual(hits, [], chr(10).join(hits[:80]))

    def test_no_personal_records_or_measurements(self):
        forbidden = ('receipts.jsonl', 'skill-receipts.jsonl', 'aa-data.csv', 'last.json', 'INSTALLATION.json',
                     'plan.json', 'routing-draft.json', 'detect.json', 'events.jsonl')
        found = [str(path.relative_to(ROOT)) for path in public_files() if path.name in forbidden]
        self.assertEqual(found, [])

    def test_no_doubled_rename_prefix(self):
        hits = [str(path.relative_to(ROOT)) for path in public_files()
                if path.suffix in TEXT_SUFFIXES and 'kp-kp-' in path.read_text(encoding='utf-8', errors='replace')]
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
