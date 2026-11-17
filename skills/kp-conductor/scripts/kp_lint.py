#!/usr/bin/env python3
"""kp_lint: Korean Protocol 문체 경고 검사기.

KP 저장소 evaluation/kp-lean-pilot-20261113/candidate/scripts/kp_lint.py(commit 7810679)를
conductor용으로 고쳤다. 기준은 korean-protocol v0.11.1 SKILL.md다.
54행(세 항목 나열), 73행(12어절), 74행(문단 3문장), 81행(부정문 대상은 한 줄)을 따른다.
셀 수 있는 문체 규칙만 경고한다. 뜻 보존과 근거 구별은 검사하지 않는다.

사용: kp_lint.py <파일|-> [--source 원자료] [--mode rewrite|compose] [--style chat|doc] [--json]
종료 코드: 경고가 있어도 0이다. 입력을 읽지 못하거나(UTF-8 아님 포함) 범위 밖 형식(HTML)이면 2다.
"""
import argparse
import json
import os
import re
import sys

BASIS = 'v0.11.1'
MAX_WORDS = 12
MAX_PARA = 3
MAX_ITEM = 3
MIN_ITEMS = 3
REWRITE_RATIO = 1.5
BT = chr(96)
FENCE_RE = re.compile('^(' + BT + '{3,}|~{3,})')
INLINE_CODE = re.compile('(?<!' + BT + ')(' + BT + '+)(?!' + BT + ')(.+?)(?<!' + BT + ')\\1(?!' + BT + ')')
CODE = '〔코드〕'
DOT_VARIANTS = re.compile('[ㆍ・‧∙]')
THOUSANDS = re.compile(r'(?<=\d),(?=\d{3}(?!\d))')
BOLD_LABEL = re.compile(r'^\*\*[^*]{1,40}?[:：]\*\*\s*|^\*\*[^*]{1,40}?\*\*[:：]\s*')
QUOTED = re.compile('“[^”]*”|"[^"]*"|「[^」]*」|‘[^’]*’|' + "(?<![A-Za-z0-9])'[^'\n]+'(?![A-Za-z0-9])")
NOTICE = re.compile(r'(아닙니다|아니다|아님|아니며|아니므로)[.)”"]*$|뜻하지 않|의미하지 않|대신하지 않|볼 수 없|참고(만|용|로만)|단정할 수 없|확정할 수 없|보장하지 않')
FUSED = re.compile(r'(이며|이고|이지만|으나|지만|으며|,)\s[^.]*?(아니다|아닙니다|볼 수 없다|볼 수 없습니다|뜻하지 않는다|뜻하지 않습니다|대신하지 않는다|대신하지 않습니다)[.]?$|(?<!뿐 )(?<!뿐만 )아니라[ ,]')
DOUBLE = re.compile(r'(않으면|없으면|아니면|없이는|않고는|않는 한)[^.]*?(없다|없습니다|않는다|않습니다|못한다|못합니다|안 된다|안 됩니다)')
FILLER = re.compile(r'(다음과|아래와) 같(다|습니다)|아래에 (제시|정리|설명)(한다|합니다|했다|했습니다)|살펴보면|하는 것이 필요(하다|합니다)|[라다]는 점(?!수)')
NEG_PREDICATE = re.compile(r'(않는다|않습니다|아니다|아닙니다|없다|없습니다|못한다|못합니다)[.?!]?$')
NEGNP = re.compile(r'((?:\S+\s){0,2}\S+?)(?:이|가|은|는|도)?\s(?:아닙니다|아니다|아니라|아니며|아니고|아닌|아님)')
NUM = re.compile(r'\d[\d,]*(?:\.\d+)?')
PATH = re.compile(r'(?:~|\.{1,2})?/[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@-]+)*|[A-Za-z0-9_.-]+/[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@-]+)*')
SENT = re.compile('(?<=[가-힣)\\]”"’\'〕\ue100-\ue1ff][.?!])\\s+')
ITEM = r'[^\s·,]+(?:\s[^\s·,]+)?'
DOT_CHAIN = re.compile(ITEM + r'(?:\s*·\s*' + ITEM + r')+')


def clean(text):
    """인라인 코드를 자리표 하나로, 굵은 표식을 지운다. 문장 경계와 어절 계수는 이 결과로 한다."""
    text = INLINE_CODE.sub(CODE, text)
    text = BOLD_LABEL.sub('', text)
    return text.replace('**', '')


def sentences(text):
    return [full for full, _ in split_sentences(text)]


def split_sentences(text):
    """(원문 문장, 인용을 가린 문장) 쌍을 돌려준다. 인용 안의 마침표는 문장 경계로 보지 않는다."""
    body = clean(text).strip()
    # 자리표는 원문에 없는 사용자 정의 영역 문자 쌍으로 만든다. 원문 글자와 충돌하지 않는다.
    pairs = [(chr(0xE000 + k), chr(0xE100 + k)) for k in range(256)]
    free = next(((a, b) for a, b in pairs if a not in body and b not in body), None)
    if free is None:
        return [(p.strip(), p.strip()) for p in SENT.split(body) if len(p.strip()) > 1]
    left, right = free
    marker = re.compile(re.escape(left) + r'(\d+)' + re.escape(right))
    quotes = []

    def hide(m):
        quotes.append(m.group(0))
        return '%s%d%s' % (left, len(quotes) - 1, right)

    masked = QUOTED.sub(hide, body)
    out = []
    for part in SENT.split(masked):
        part = part.strip()
        if len(part) > 1:
            out.append((marker.sub(lambda m: quotes[int(m.group(1))], part), part))
    return out


def hapnida(s):
    """'합니다'체 종결인지 본다. '니다' 앞 음절의 받침이 ㅂ이어야 한다('아니다'는 제외)."""
    m = re.search(r'([가-힣])니다[.!?]?$', s)
    return bool(m) and (ord(m.group(1)) - 0xAC00) % 28 == 17


def parse(text):
    """본문을 블록(para, item, cell, quote)으로 나눈다. 코드 블록과 제목은 빼고, 둘 다 앞 블록을 닫는다."""
    blocks, fence, section = [], None, 0

    def close():
        if blocks:
            blocks[-1]['closed'] = True

    for no, raw in enumerate(text.split('\n'), 1):
        s = raw.strip()
        if fence:
            if s and set(s) == {fence[0]} and len(s) >= len(fence):
                fence = None
            continue
        opened = FENCE_RE.match(s)
        if opened and (opened.group(1)[0] == '~' or BT not in s[len(opened.group(1)):]):
            fence = opened.group(1)
            close()
            continue
        if not s:
            close()
            continue
        if re.match(r'#{1,6}\s', s) or re.fullmatch(r'\*\*[^*]+\*\*[:：]?', s):
            section += 1
            close()
            continue
        if s.startswith('|'):
            close()
            for cell in s.strip('|').split('|'):
                cell = cell.strip()
                if cell and not set(cell) <= set('-: '):
                    blocks.append(dict(kind='cell', line=no, section=section, text=cell, closed=True))
            continue
        if s.startswith('>'):
            close()
            body = s.lstrip('> ').strip()
            if body:
                blocks.append(dict(kind='quote', line=no, section=section, text=body, closed=True))
            continue
        m = re.match(r'([-*•]|\d+[.)])\s+', s)
        last = blocks[-1] if blocks else None
        if m:
            blocks.append(dict(kind='item', line=no, section=section, text=s[m.end():], closed=False))
        elif last and last['kind'] in ('para', 'item') and not last['closed'] and last['section'] == section:
            last['text'] += ' ' + s
        else:
            blocks.append(dict(kind='para', line=no, section=section, text=s, closed=False))
    for b in blocks:
        pairs = split_sentences(b['text']) or [(clean(b['text']), clean(b['text']))]
        b['sents'] = [full for full, _ in pairs]
        b['masked'] = [masked for _, masked in pairs]
    return blocks


def enum_items(s):
    return enum_detail(s)[0]


def enum_detail(s):
    """나열 항목 수와, 가장 긴 가운뎃점 사슬이 숫자 참조(54·73·74행)인지를 돌려준다."""
    s = THOUSANDS.sub('', DOT_VARIANTS.sub('·', QUOTED.sub('', s)))
    chains = [m.group(0) for m in DOT_CHAIN.finditer(s)]
    longest = max(chains, key=lambda c: c.count('·'), default='')
    chain = longest.count('·') + 1 if longest else 0
    commas = s.count(',') + 1 if s.count(',') >= 2 else 0
    parts = longest.split('·')
    numeric = chain >= commas and chain > 0 and parts[0].split()[-1][:1].isdigit() and all(
        part.strip()[:1].isdigit() for part in parts[1:])
    return max(chain, commas), numeric


def stem(w):
    w = re.sub(r'[“”"\'‘’()\[\],.*]', '', w)
    return re.sub(r'(으로|에서|에게|까지|부터|이나|이며|이고|은|는|이|가|을|를|의|도|만|와|과|로|에)$', '', w)


def numbers(text):
    """수치 토큰 집합. 앞이 영숫자가 아닌 '-', '+', '−'는 부호로 붙인다(날짜·범위의 하이픈은 제외)."""
    found = set()
    for m in re.finditer(r'\d[\d,]*(?:\.\d+)?', text):
        start, sign = m.start(), ''
        if start and text[start - 1] in '-+−' and (start == 1 or not text[start - 2].isalnum()):
            sign = text[start - 1].replace('−', '-')
        found.add(sign + m.group(0).replace(',', ''))
    return found


def literals(text):
    found = {m.group(2).strip() for m in INLINE_CODE.finditer(text)}
    found |= {p.rstrip('.,') for p in PATH.findall(INLINE_CODE.sub(' ', text)) if re.search(r'[A-Za-z]', p)}
    return {x for x in found if x}


def lint(text, source=None, mode=None, style=None):
    blocks = [b for b in parse(text) if b['kind'] != 'quote']
    found = {}

    def add(rule, key, line, s, note=''):
        entry = found.setdefault(key, dict(rules=[], notes=[], line=line, text=s[:90]))
        entry['rules'].append(rule)
        if note:
            entry['notes'].append(note)

    for bi, b in enumerate(blocks):
        ss = b['sents']
        for si, s in enumerate(ss):
            key, line = (bi, si), b['line']
            bare = b['masked'][si]
            words = len(s.split())
            heavy = bool(QUOTED.search(s)) or len(NUM.findall(THOUSANDS.sub('', s))) >= 3
            if words > MAX_WORDS:
                add('긴문장', key, line, s, '%d어절%s' % (words, ' (인용·수치 포함)' if heavy else ''))
            n, numeric = enum_detail(bare)
            if b['kind'] != 'cell' and n >= MIN_ITEMS:
                tag = ' (부정문 대상 예외 후보, KP 81행)' if NEG_PREDICATE.search(bare) else ''
                tag += ' (숫자 참조 후보)' if numeric else ''
                add('가로나열', key, line, s, '%d항목%s' % (n, tag))
            if FUSED.search(bare):
                add('부정결합', key, line, s)
            if DOUBLE.search(bare):
                add('이중부정', key, line, s)
            if FILLER.search(bare):
                add('군더더기', key, line, s)
            if style == 'chat' and re.search(r'다[.!]?$', bare) and not hapnida(bare):
                add('어체', key, line, s, '대화는 합니다체')
            if style == 'doc' and hapnida(bare):
                add('어체', key, line, s, '문서는 한다체')
            if source is not None:
                for m in NEGNP.finditer(bare):
                    keys = [k for k in (stem(w) for w in m.group(1).split()) if len(k) >= 2]
                    if keys and keys[-1][:2] not in source:
                        add('원문밖부정', key, line, s, keys[-1])
        if b['kind'] == 'para' and len(ss) > MAX_PARA:
            add('긴문단', (bi, 'block'), b['line'], clean(b['text']), '%d문장' % len(ss))
        if b['kind'] == 'item' and len(ss) > MAX_ITEM:
            add('긴항목', (bi, 'block'), b['line'], clean(b['text']), '%d문장' % len(ss))
        if b['kind'] != 'cell' and len(ss) >= 2 and NOTICE.search(ss[-1].rstrip()):
            add('말미고지', (bi, 'notice'), b['line'], ss[-1], '블록 끝')
    by_section = {}
    for bi, b in enumerate(blocks):
        if b['kind'] != 'cell':
            by_section.setdefault(b['section'], []).append((bi, b))
    for body in by_section.values():
        bi, last = body[-1]
        if len(body) >= 3 and len(last['sents']) == 1 and NOTICE.search(last['sents'][0].rstrip()):
            add('말미고지', (bi, 'notice'), last['line'], last['sents'][0], '구획 끝')
    warnings = sorted(found.values(), key=lambda x: x['line'])
    stats = {}
    if source is not None:
        source_numbers = numbers(source)
        missing = sorted(source_numbers - numbers(text))
        stats['length_ratio'] = round(len(text) / max(1, len(source)), 2)
        stats['numbers_kept'] = '%d/%d' % (len(source_numbers) - len(missing), len(source_numbers))
        stats['numbers_missing_candidates'] = missing[:20]
        stats['literals_missing_candidates'] = sorted(literals(source) - literals(text))[:20]
        if mode == 'rewrite' and len(text) > REWRITE_RATIO * len(source):
            warnings.append(dict(rules=['길이초과'], notes=['원문의 %s배' % stats['length_ratio']], line=0, text=''))
    all_s = [s for b in blocks for s in b['sents']]
    stats.update(chars=len(text), sentences=len(all_s),
                 mean_words=round(sum(len(s.split()) for s in all_s) / max(1, len(all_s)), 1),
                 warnings=len(warnings),
                 per_1000_chars=round(len(warnings) * 1000 / max(1, len(text)), 1),
                 basis='KP ' + BASIS)
    counts = {}
    for w in warnings:
        for rule in w['rules']:
            counts[rule] = counts.get(rule, 0) + 1
    return dict(stats=stats, counts=counts, warnings=warnings)


def installed_version():
    # The bundled copy records its version in PINNED.json; a separate install uses INSTALLATION.json.
    for path in ('~/.codex/skills/korean-protocol/PINNED.json', '~/.codex/skills/korean-protocol/INSTALLATION.json',
                 '~/.agents/skills/korean-protocol/INSTALLATION.json'):
        try:
            with open(os.path.expanduser(path), encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        version = data.get('version') if isinstance(data, dict) else None
        if isinstance(version, str):
            return version if version.startswith('v') else 'v' + version
    return None


def read_text(path):
    if path == '-':
        return sys.stdin.buffer.read().decode('utf-8')
    with open(path, encoding='utf-8-sig') as handle:
        return handle.read()


def main(argv=None):
    ap = argparse.ArgumentParser(prog='kp-lint')
    ap.add_argument('output', help='검사할 파일. - 이면 표준 입력(UTF-8)')
    ap.add_argument('--source')
    ap.add_argument('--mode', choices=['rewrite', 'compose'])
    ap.add_argument('--style', choices=['chat', 'doc'])
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    if a.output.lower().endswith(('.html', '.htm')):
        print('kp-lint: HTML은 1차 검사 범위 밖이다. Markdown이나 일반 텍스트를 넘긴다.')
        return 2
    try:
        text = read_text(a.output)
        source = read_text(a.source) if a.source else None
    except (OSError, UnicodeError) as exc:
        print('kp-lint: 읽지 못했다: %s' % exc)
        return 2
    r = lint(text, source, a.mode, a.style)
    version = installed_version()
    if version and version != BASIS:
        r['stats']['installed_kp'] = version
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    if version and version != BASIS:
        print('참고: 설치된 KP는 %s이고 검사 기준은 %s다.' % (version, BASIS))
    print('경고: ' + (', '.join('%s %d' % kv for kv in r['counts'].items()) if r['counts'] else '없음'))
    for w in r['warnings']:
        print('  %4d행 [%s] %s | %s' % (w['line'], '+'.join(w['rules']), ', '.join(w['notes']), w['text']))
    st = r['stats']
    print('요약: 문장 %d, 경고 %d건, 1,000자당 %.1f건, 기준 %s' % (st['sentences'], st['warnings'], st['per_1000_chars'], st['basis']))
    if source is not None:
        print('원자료 수치 보존: %s, 수치 누락 후보: %s' % (st['numbers_kept'], ', '.join(st['numbers_missing_candidates']) or '없음'))
        print('원자료 경로·코드 누락 후보: %s' % (', '.join(st['literals_missing_candidates']) or '없음'))
    return 0


if __name__ == '__main__':
    sys.exit(main())

