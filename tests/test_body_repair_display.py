"""Offline display-only fixtures. They never enter model inputs."""
import unittest
from hashlib import sha256

from experiments.body_repair_display import project_display


class ActionDisplayTests(unittest.TestCase):
    def test_author_reviewed_action_and_aside_shapes(self):
        for action in ('（歪头）', '（愣了一下）', '(轻轻笑)', '（点了点头）'):
            with self.subTest(action=action):
                raw = action + '你到了吗？'
                shown = project_display(raw)
                self.assertEqual(shown['body'], raw)
                self.assertEqual(shown['display_body'], '你到了吗？')
                self.assertEqual(shown['removed_spans'], [dict(start=0, end=len(action), text=action)])
        text = '听到了（刚才声音有些小）'
        self.assertEqual(project_display(text)['display_body'], text)

    def test_offsets_restore_multiple_actions_including_non_bmp_text(self):
        raw = '🚲到了。 （点了点头）好！\r\n（摇头）还没。'
        result = project_display(raw)
        self.assertEqual(result['display_body'], '🚲到了。 好！\r\n还没。')
        cursor = 0
        for span in result['removed_spans']:
            self.assertLessEqual(cursor, span['start'])
            self.assertEqual(raw[span['start']:span['end']], span['text'])
            cursor = span['end']
        rebuilt = result['display_body']
        for span in result['removed_spans']:
            pos = span['start']
            rebuilt = rebuilt[:pos] + span['text'] + rebuilt[pos:]
        self.assertEqual(rebuilt, raw)
        for key, digest_key in [('body', 'body_sha256'), ('display_body', 'display_sha256')]:
            self.assertEqual(result[digest_key], sha256(result[key].encode()).hexdigest())

    def test_quoted_code_commands_and_math(self):
        for raw in (
            '“好。 （点头）收到。”', '他说：「好！\n（点头）收到。」', "'a!（点头）好'",
            '`a!（点头）好`', '~~~text\n（点头）好\n~~~', '$x=1!（点头）好$',
            'echo "好！ （点头）好"', 'printf 收到！ （点头）好', '> （点头）好', '    （点头）好', '（点头）+ 1 = 4',
            '请复制：\n（点头）好', '《收到！ （点头）好', '（点头）是指动作。',
            '（笑）好', '（）好', '（解释（点头））好', '方案（点头）确认。',
            '（我有点想笑）好', '（点头）', '（点头）！', '（点头）\n（摇头）',
        ):
            with self.subTest(raw=raw):
                self.assertEqual(project_display(raw)['display_body'], raw)

    def test_verbatim_request_and_known_source_ranges_win(self):
        raw = '（耸耸肩）好。\n（挥手）再见。'
        for options in [dict(content_kind='command'), dict(preserve_verbatim=True),
                        dict(user_text='请逐字显示'), dict(user_text='我说的是（挥手）'),
                        dict(protected_spans=[dict(start=raw.index('（挥手）'), end=len(raw))])]:
            with self.subTest(options=options):
                self.assertEqual(project_display(raw, **options)['display_body'], raw)
        with self.assertRaises(ValueError):
            project_display(raw, protected_spans=[(-1, 3)])

    def test_an_outside_action_can_be_removed_while_quote_is_untouched(self):
        raw = '（歪头）你说“好！ （点头）好”了吗？'
        self.assertEqual(project_display(raw)['display_body'], raw[len('（歪头）'):])
