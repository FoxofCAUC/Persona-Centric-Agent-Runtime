"""Project an approved direct reply to its reversible visible text."""
import re
import unicodedata

from persona_runtime.corpus_files import digest
from persona_runtime.qq_text import is_code_line


VERSION = 'el4-independent-action-display-v1'
# Limit recognition to short, unqualified stage gestures. Descriptions that need
# semantic interpretation are left intact for the reader.
GESTURE = re.compile(
    r'(?:轻轻地?|微微地?|缓缓地?|默默地?)?'
    r'(?:歪头|点头|摇头|抬头|垂头|眨眼|皱眉|挑眉|撇嘴|抿嘴|耸肩|摊手|摆手|挥手|低头|'
    r'歪了歪头|点了点头|摇了摇头|眨了眨眼|皱了皱眉|耸了耸肩|摊了摊手|'
    r'愣了一下|愣住|叹气|叹了口气|笑了一下|笑了笑)|(?:轻轻地?|微微地?)笑'
)
PAREN = re.compile(r'（([^（）()\r\n]{1,16})）|\(([^（）()\r\n]{1,16})\)')
BOUNDARY = '。！？!?；;…\r\n'
LITERAL_REQUEST = re.compile(
    r'原样|原文|原封不动|一字不|逐字|照抄|照录|抄写|照着写|复述|照搬|照发|复制|一模一样|'
    r'(?:不要|别|不许)(?:修改|改写|改动|删|改)|保留.{0,8}(?:括号|文字|原句|全文)|'
    r'verbatim|word.for.word|exactly as', re.I)
LITERAL_BODY = re.compile(
    r'`|~{3}|\$|<<|@[\x22\x27]|\\[()\[\]]|\\(?:begin|end|frac|sqrt)\b|'
    r'括号|旁白|原文|字符|正则|台词|舞台指示|代码|命令行|公式|举例|例如[：:]')
PUNCTUATION_PAIRS = {'“': '”', '‘': '’', '「': '」', '『': '』', '《': '》',
                     '"': '"', "'": "'", '（': '）', '(': ')', '[': ']', '【': '】', '{': '}'}
COMMAND_LINE = re.compile(
    r'^\s*(?:[a-zA-Z]+-[A-Za-z]+|[\w.-]+\.(?:exe|bat|sh|cmd|py)|'
    r'git|echo|printf|python\d*|pwsh|powershell|cmd|bash|sh|curl|rg|grep|cat|sed|awk|npm|node)\s',
    re.I)


def quoted_or_nested(text):
    """Mark opening offsets inside quotes or other grouping, including unfinished ones."""
    stack, nested = [], set()
    closers = set(PUNCTUATION_PAIRS.values())
    for index, char in enumerate(text):
        if stack:
            if char in '(（':
                nested.add(index)
            if char == stack[-1]:
                stack.pop()
                continue
        if char in PUNCTUATION_PAIRS:
            stack.append(PUNCTUATION_PAIRS[char])
        elif char in closers:
            # An unmatched closer makes the quotation boundary ambiguous.
            return None
    return None if stack else nested


def clause_action(body, match):
    start, end = match.span()
    before = body[:start].rstrip(' \t')
    after = body[end:].lstrip(' \t')
    if before and before[-1] not in BOUNDARY:
        return False
    if before.endswith(('\n', '\r')) and before.rstrip().endswith(('：', ':')):
        return False
    if after and (after[0] in '+-*/^_=<>:：〔【[{' or after[0].isascii() and after[0].isalnum()):
        return False
    if re.match(r'(?:是指|指的是|是(?:个|这|一|什么)|表示|的意思|这[个句段]|对应|等于|意味着|这个词)', after):
        return False
    line_end = body.find('\n', end)
    line = body[body.rfind('\n', 0, start) + 1:len(body) if line_end == -1 else line_end]
    return not (line.startswith(('    ', '\t')) or line.lstrip().startswith(('>', '#', '|'))
                or is_code_line(line) or COMMAND_LINE.match(line))


def project_display(body, *, user_text='', content_kind='chat', preserve_verbatim=False, protected_spans=()):
    """Return raw and displayed text with reversible half-open Unicode offsets."""
    if not isinstance(body, str) or not isinstance(user_text, str):
        raise TypeError('display text must be a string')
    protected = []
    for span in protected_spans:
        start, end = (span['start'], span['end']) if isinstance(span, dict) else span
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body):
            raise ValueError('invalid protected display span')
        protected.append((start, end))
    removed = []
    reason = 'no_independent_action'
    if content_kind != 'chat' or preserve_verbatim or LITERAL_REQUEST.search(user_text) or LITERAL_BODY.search(body):
        reason = 'literal_preserved'
    else:
        nested = quoted_or_nested(body)
        if nested is None:
            reason = 'ambiguous_delimiters'
        else:
            for match in PAREN.finditer(body):
                start, end = match.span()
                action = match[1] or match[2]
                if (start not in nested and GESTURE.fullmatch(action) and match[0] not in user_text
                        and not any(start < stop and begin < end for begin, stop in protected)
                        and clause_action(body, match)):
                    removed.append({'start': start, 'end': end, 'text': match[0]})
    displayed = body
    if removed:
        chunks, cursor = [], 0
        for span in removed:
            chunks.append(body[cursor:span['start']])
            cursor = span['end']
        displayed = ''.join(chunks) + body[cursor:]
        if any(unicodedata.category(char)[0] in 'LNS' for char in displayed):
            reason = 'independent_actions_removed'
        else:
            displayed, removed, reason = body, [], 'no_remaining_speech'
    return {'version': VERSION, 'body': body, 'body_sha256': digest(body.encode('utf-8')),
            'display_body': displayed, 'display_sha256': digest(displayed.encode('utf-8')),
            'coordinate': 'unicode_codepoints', 'removed_spans': removed, 'reason': reason}
