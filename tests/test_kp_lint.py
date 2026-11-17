import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / 'skills' / 'kp-conductor' / 'scripts'
spec = importlib.util.spec_from_file_location('kp_lint_module', ROOT / 'kp_lint.py')
kp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kp)


def rules(text, **kw):
    return [r for w in kp.lint(text, **kw)['warnings'] for r in w['rules']]


def notes(text):
    return ' '.join(n for w in kp.lint(text)['warnings'] for n in w['notes'])


class SentenceTests(unittest.TestCase):
    def test_bold_sentence_end_is_a_boundary(self):
        self.assertEqual(len(kp.sentences('**판정은 유지다.** 세 분석이 같은 판정을 냈다.')), 2)

    def test_numbered_list_date_and_version_are_not_boundaries(self):
        self.assertEqual(len(kp.sentences('2026. 11. 13. 회의에서 v0.11.1 판을 정했다.')), 1)

    def test_inline_code_counts_as_one_word(self):
        code = chr(96) + 'python3 conductor.py route' + chr(96)
        self.assertEqual(len(kp.sentences(code + '를 실행한다.')[0].split()), 2)

    def test_bold_label_is_not_counted(self):
        text = '- **다음 행동:** 하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 열하나 열둘.'
        self.assertNotIn('긴문장', rules(text))


class RuleTests(unittest.TestCase):
    def test_long_sentence_warns_over_twelve_words(self):
        text = '하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 열하나 열둘 열셋이다.'
        self.assertEqual(rules(text), ['긴문장'])
        self.assertNotIn('긴문장', rules('하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 열하나 열둘이다.'))

    def test_paragraph_and_list_item_warn_over_three_sentences(self):
        four = '가를 했다. 나를 했다. 다를 했다. 라를 했다.'
        self.assertIn('긴문단', rules(four))
        self.assertIn('긴항목', rules('- ' + four))
        self.assertEqual(rules('가를 했다. 나를 했다. 다를 했다.'), [])

    def test_multi_word_dot_chain_and_negative_exception_tag(self):
        text = '이 비교는 업체 선정·설계 승인·계약 확정·자재 발주를 대신하지 않는다.'
        self.assertIn('가로나열', rules(text))
        self.assertIn('KP 81행', notes(text))

    def test_dot_variants_are_one_character(self):
        self.assertEqual(kp.enum_items('AㆍB・C‧D∙E를 본다'), 5)

    def test_thousands_commas_are_not_items(self):
        self.assertEqual(kp.enum_items('금액은 1,200,000원과 3,400,000원이다'), 0)

    def test_filler_phrases_in_both_styles(self):
        self.assertIn('군더더기', rules('결과는 다음과 같습니다.'))
        self.assertIn('군더더기', rules('검토하는 것이 필요하다.'))
        self.assertNotIn('군더더기', rules('부록 2절에 근거를 둔다.'))

    def test_long_and_list_warnings_merge_on_one_sentence(self):
        text = '남길 것은 분류, 검증 하한, 독립 검토자, 기본 배정표, 위임 규칙과 대체 규칙 모두다.'
        merged = [w for w in kp.lint(text)['warnings'] if len(w['rules']) == 2]
        self.assertEqual(merged[0]['rules'], ['긴문장', '가로나열'])

    def test_style_option(self):
        self.assertIn('어체', rules('검사를 마쳤다.', style='chat'))
        self.assertEqual(rules('검사를 마쳤습니다.', style='chat'), [])
        self.assertIn('어체', rules('검사를 마쳤습니다.', style='doc'))

    def test_code_and_tables_rows_and_headings(self):
        fence = chr(96) * 3
        text = '# 하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 열하나 열둘 열셋\n' + fence + '\n가, 나, 다, 라, 마, 바\n' + fence + '\n| 항목 | 값 |\n|---|---|\n| 가 | 나 |\n'
        self.assertEqual(rules(text), [])

    def test_previous_chat_answer_violations_are_found(self):
        text = ('이 보고서는 작성에 참여하지 않은 Sol xhigh가 원자료와 대조해 검토했습니다. 지적 16건을 모두 반영했습니다. '
                '가장 큰 수정은 Claude 사본에 대한 잘못된 주장을 바로잡은 것과, 근거를 찾지 못한 "결함 7건 발견" 주장을 뺀 것입니다. '
                '검토자도 고친 판정은 유지할 수 있다고 봤습니다.\n\n'
                '2. SKILL.md를 약 120행으로 줄입니다. 분류, 검증 하한, 검토자 선정, 모델 배정, Fable 대체만 남기고 나머지는 별도 문서로 옮깁니다.\n')
        found = rules(text)
        self.assertIn('긴문단', found)
        self.assertIn('긴문장', found)
        self.assertIn('가로나열', found)


class CommandTests(unittest.TestCase):
    def run_cli(self, *args, stdin=''):
        return subprocess.run([sys.executable, str(ROOT / 'kp_lint.py'), *args], input=stdin,
                              capture_output=True, text=True)

    def test_exit_code_is_zero_with_warnings_and_reads_stdin(self):
        done = self.run_cli('-', stdin='가를 했다. 나를 했다. 다를 했다. 라를 했다.')
        self.assertEqual(done.returncode, 0)
        self.assertIn('긴문단', done.stdout)
        self.assertIn('기준 KP v0.11.1', done.stdout)

    def test_html_and_missing_file_exit_two(self):
        self.assertEqual(self.run_cli('page.html').returncode, 2)
        self.assertEqual(self.run_cli('/nonexistent/kp.md').returncode, 2)

    def test_conductor_wrapper_passes_arguments(self):
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as handle:
            handle.write('검사를 마쳤다.')
        self.addCleanup(os.unlink, handle.name)
        done = subprocess.run([sys.executable, str(ROOT / 'conductor.py'), 'kp-lint', handle.name, '--style', 'chat'],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0)
        self.assertIn('어체 1', done.stdout)

class ReviewReproTests(unittest.TestCase):
    """Astra 독립 검토(2026-11-13)의 재현 입력."""

    def test_three_short_items_and_numbered_items_warn(self):
        self.assertIn('가로나열', rules('설계·검증·배포를 수행한다.'))
        self.assertIn('가로나열', rules('A1·B2·C3·D4를 검토한다.'))
        self.assertIn('KP 81행', notes('설계·검증·배포를 대신하지 않는다.'))
        self.assertIn('숫자 참조 후보', notes('근거는 54·73·74·81행이다.'))

    def test_rewrite_length_warning_is_counted(self):
        over = kp.lint('가나다라', source='가나', mode='rewrite')
        self.assertEqual(over['stats']['warnings'], len(over['warnings']))
        self.assertEqual(over['counts'].get('길이초과'), 1)
        exact = kp.lint('가나다', source='가나', mode='rewrite')
        self.assertNotIn('길이초과', exact['counts'])

    def test_ascii_single_quotes_are_quotations(self):
        self.assertEqual(rules("'다음과 같습니다'를 그대로 인용한다."), [])
        self.assertEqual(rules("'설계, 검증, 배포, 운영'을 인용한다."), [])
        self.assertIn('가로나열', rules("user's 설계, 검증, 배포를 본다. don't"))

    def test_style_handles_anida(self):
        self.assertIn('어체', rules('검토 대상이 아니다.', style='chat'))
        self.assertEqual(rules('검토 대상이 아니다.', style='doc'), [])
        self.assertEqual(rules('검토 대상이 아닙니다.', style='chat'), [])
        self.assertIn('어체', rules('검토 대상이 아닙니다.', style='doc'))
        self.assertEqual(rules('확인했습니까?', style='chat'), [])
        self.assertEqual(rules('- 검토 항목', style='chat'), [])

    def test_heading_and_fence_close_paragraphs(self):
        self.assertEqual(rules('가를 했다. 나를 했다.\n# 둘째\n다를 했다. 라를 했다.'), [])
        fence = chr(96) * 3
        self.assertEqual(rules('가를 했다. 나를 했다.\n' + fence + '\ncode\n' + fence + '\n다를 했다. 라를 했다.'), [])
        self.assertEqual(len(kp.parse('#123 이슈를 닫았다.')), 1)

    def test_markdown_exclusions(self):
        long = '하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 열하나 열둘 열셋이다.'
        self.assertEqual(rules('> - ' + long), [])
        self.assertEqual(rules('~~~\n' + long + '\n~~~'), [])
        tick2 = chr(96) * 2
        self.assertEqual(rules(tick2 + long + tick2 + '를 실행한다.'), [])

    def test_same_sentence_twice_is_two_warnings(self):
        r = kp.lint('검사를 마쳤다. 검사를 마쳤다.', style='chat')
        self.assertEqual(r['stats']['warnings'], 2)
        self.assertEqual(r['counts']['어체'], 2)

    def test_section_end_notice_is_restored(self):
        self.assertIn('말미고지', rules('검사를 마쳤다.\n\n자료를 정리했다.\n\n확정 견적이 아니다.'))

    def test_source_compares_whole_tokens_and_literals(self):
        r = kp.lint('금액은 120원이다.', source='금액은 12원이다. src/a.py와 /tmp/original.md를 본다. 설계/검증은 경로가 아니다.')
        self.assertEqual(r['stats']['numbers_missing_candidates'], ['12'])
        self.assertEqual(r['stats']['literals_missing_candidates'], ['/tmp/original.md', 'src/a.py'])

    def test_filler_variants(self):
        self.assertIn('군더더기', rules('결과는 아래와 같습니다.'))
        self.assertIn('군더더기', rules('결과는 다음과 같다.'))

    def test_bad_installation_metadata_is_ignored(self):
        from unittest.mock import mock_open, patch
        for body in ('[]', 'null', '"v0.11.2"', '{"version": 3}'):
            with patch('builtins.open', mock_open(read_data=body)):
                self.assertIsNone(kp.installed_version())


class ReviewCommandTests(unittest.TestCase):
    def call(self, args, stdin=b''):
        return subprocess.run([sys.executable, *args], input=stdin, capture_output=True)

    def test_wrapper_passes_leading_options_help_and_stdin(self):
        cx = str(ROOT / 'conductor.py')
        done = self.call([cx, 'kp-lint', '--style', 'chat', '-'], '검사를 마쳤다.'.encode())
        self.assertEqual(done.returncode, 0)
        self.assertIn('어체 1', done.stdout.decode())
        self.assertEqual(self.call([cx, 'kp-lint', '--help']).returncode, 0)

    def test_non_utf8_input_exits_two(self):
        script = str(ROOT / 'kp_lint.py')
        self.assertEqual(self.call([script, '-'], b'\xff\xfe').returncode, 2)
        with tempfile.NamedTemporaryFile('wb', suffix='.md', delete=False) as handle:
            handle.write(b'\xff\xfe')
        self.addCleanup(os.unlink, handle.name)
        self.assertEqual(self.call([script, handle.name]).returncode, 2)


class SecondReviewTests(unittest.TestCase):
    """Astra 재검토(2026-11-13)의 재현 입력."""
    LONG = '하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 열하나 열둘 열셋이다.'

    def test_inline_code_at_line_start_is_not_a_fence(self):
        tick3 = chr(96) * 3
        r = kp.lint(tick3 + 'python foo' + tick3 + '를 실행한다.\n\n' + self.LONG)
        self.assertIn('긴문장', r['counts'])

    def test_multi_sentence_quotes_are_masked(self):
        self.assertEqual(rules('"설계, 검증, 배포를 수행한다. 다시 확인한다."를 인용한다.'), [])
        self.assertEqual(rules('"결과는 다음과 같습니다. 다시 확인합니다."를 인용한다.', style='doc'), [])

    def test_literals_compare_whole_tokens(self):
        tick = chr(96)
        r = kp.lint('src/a.py.bak와 ' + tick + 'foobar()' + tick + '를 고친다.', source='src/a.py와 ' + tick + 'foo' + tick + '를 본다.')
        self.assertEqual(r['stats']['literals_missing_candidates'], ['foo', 'src/a.py'])
        r = kp.lint('src/a.py를 수정한다.', source='경로는 src/a.py.')
        self.assertEqual(r['stats']['literals_missing_candidates'], [])

    def test_separators_without_or_with_extra_spaces(self):
        self.assertIn('가로나열', rules('설계,검증,배포를 수행한다.'))
        self.assertIn('가로나열', rules('설계 · 검증 · 배포를 수행한다.'))

    def test_unbalanced_backticks_stay_as_text(self):
        tick = chr(96)
        self.assertIn('긴문장', rules(tick * 2 + self.LONG + tick))

    def test_quote_placeholder_never_collides_with_text(self):
        self.assertEqual(kp.sentences('〔인용0〕를 확인한다.'), ['〔인용0〕를 확인한다.'])
        self.assertEqual(kp.sentences('〔인용0〕와 "실제 인용"을 확인한다.'), ['〔인용0〕와 "실제 인용"을 확인한다.'])
        # KP korean-rules.md: 인용 내부 부호는 문장 경계로 쓰지 않는다.
        self.assertEqual(len(kp.sentences('"가를 했다." 다음 문장이다.')), 1)


    def test_placeholder_exhaustion_falls_back_without_error(self):
        text = ''.join(chr(0xE000 + i) for i in range(256)) + '\ue0ff0\ue1ff를 확인한다.'
        self.assertEqual(len(kp.sentences(text)), 1)


    def test_numbers_keep_sign_and_single_digits(self):
        r = kp.lint('손익은 12원이다.', source='손익은 -12원이다.')
        self.assertEqual(r['stats']['numbers_missing_candidates'], ['-12'])
        r = kp.lint('수량은 8개다.', source='수량은 3개다.')
        self.assertEqual(r['stats']['numbers_missing_candidates'], ['3'])
        self.assertEqual(kp.numbers('2026-11-13, 3-5개'), {'2026', '11', '13', '3', '5'})


if __name__ == '__main__':
    unittest.main()
