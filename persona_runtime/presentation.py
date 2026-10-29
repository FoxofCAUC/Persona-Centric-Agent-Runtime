"""One plain-chat presentation contract. Semantic records remain immutable."""
import hashlib
import json
import re

from .qq_text import plain_qq_text, is_code_line
from .reply_batch import ReplySchemaError

VERSION = 'plain-dialogue-v1'
CONTRACT = ('\n用户可见正文一律采用自然纯文本，这由系统负责，不要求用户提醒。'
    '不要Markdown标题、加粗、斜体、删除线、代码围栏、表格、引用块、装饰性列表或链接包装。'
    '需要步骤时用普通中文、换行或自然编号；链接写原始网址。'
    '代码直接写代码正文，保留缩进、运算符、字符串、数字和版本号，不能为去格式改坏数据。'
    '不要把内部回复JSON当聊天正文。结构回复仍遵守其JSON协议，但各正文项遵守上述纯文本规则。')


def chat_target_matches(persona_id, rows, index):
    return not (0<=index<len(rows)) or rows[index]['id']==persona_id


def code_lines(text):
    result=[]; fence=None
    for line in text.splitlines():
        marker=re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$',line)
        if marker:
            if fence is None:fence=marker[1]
            elif marker[1][0]==fence[0] and len(marker[1])>=len(fence) and not marker[2].strip():fence=None
        elif fence and line:result.append(line)
    return result


def violations(text, content_kind='chat', protected_lines=()):
    """Detect syntax, not occurrences of punctuation in code/URLs."""
    if content_kind == 'code' and not re.search(r'(?m)^\s*(`{3,}|~{3,})', text):
        return []
    found = []
    if re.search(r'(?m)^\s{0,3}(`{3,}|~{3,})', text):
        found.append('code_fence')
    # Code bodies are data. Fences themselves have already been reported.
    body = re.sub(r'(?ms)^\s{0,3}(`{3,}|~{3,})[^\n]*\n.*?^\s{0,3}\1\s*$', '', text)
    protected=set(protected_lines)
    body='\n'.join('CODE' if line in protected or is_code_line(line) else line for line in body.split('\n'))
    body=re.sub(r'\b[A-Za-z0-9_]+\s+\*\*\s+[A-Za-z0-9_]+','POWER',body)
    body = re.sub(r'https?://[^\s<>（）]+', 'URL', body)
    patterns = {
        'heading':r'(?m)^\s{0,3}#{1,6}\s+\S',
        'emphasis':r'(\*\*|__|~~)[^\n]+?\1|(?<!\w)\*[^*\n]+\*(?!\w)|(?<!\w)_[^_\n]+_(?!\w)',
        'inline_code':r'`[^`\n]+`',
        'quote_block':r'(?m)^\s{0,3}>\s*\S',
        'decorative_list':r'(?m)^\s*[-+*]\s+\S',
        'link_wrapper':r'!?\[[^\]\n]*\]\([^\n]*\)',
        'table':r'(?m)^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$',
        'setext_heading':r'(?m)^\s*(?:={3,}|-{3,})\s*$',
    }
    for name, pattern in patterns.items():
        if re.search(pattern, body):found.append(name)
    return found


def render(text, content_kind='chat'):
    if not isinstance(text, str) or '\x00' in text:
        raise ReplySchemaError('plain_presentation_invalid')
    if content_kind!='code' and text.lstrip().startswith('{'):
        try:container=json.loads(text)
        except ValueError:container=None
        if isinstance(container,dict) and 'schema_version' in container and ('segments' in container or 'messages' in container):
            raise ReplySchemaError('plain_presentation_internal_envelope')
    if re.search(r'(?m)^\s*(`{3,}|~{3,})', text):
        fence = None
        for line in text.splitlines():
            match = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
            if match:
                if fence is None:fence=match[1]
                elif match[1][0]==fence[0] and len(match[1])>=len(fence) and not match[2].strip():fence=None
        if fence:raise ReplySchemaError('plain_presentation_unclosed_fence')
    code = content_kind == 'code' and 'code_fence' not in violations(text, content_kind)
    value = text if code else plain_qq_text(text)
    if not code:
        value = re.sub(r'(?m)^\s*(?:={3,}|-{3,})\s*$', '', value)
        value = re.sub(r'(?m)^\s*\[[ xX]\]\s+', '', value)
        value = re.sub(r'<(https?://[^<>\s]+)>', r'\1', value)
        if content_kind!='code' and not re.fullmatch(r'\s*(`{3,}|~{3,})[^\n]*\n[\s\S]*\n\1\s*',text):value = value.strip()
    if not value.strip():raise ReplySchemaError('qq_plain_text_empty')
    remaining = violations(value, content_kind, code_lines(text))
    if remaining:raise ReplySchemaError('plain_presentation_residual_format')
    return value, {'version':VERSION,'converted':text!=value,
        'original_violations':violations(text,content_kind),'remaining_violations':remaining,
        'display_sha256':hashlib.sha256(value.encode('utf-8')).hexdigest()}


def batch_view(reply, segments=None, *, historical=False):
    if reply is None:return None, None
    try:
        items = segments or [{'text':reply,'content_kind':'chat'}]
        rendered = [render(s['text'],s.get('content_kind') or 'chat') for s in items]
        return '\n\n'.join(v[0] for v in rendered), {'version':VERSION,'segments':[v[1] for v in rendered]}
    except ReplySchemaError as exc:
        if not historical:raise
        return '这条历史回复无法安全显示，请在工程档案中核对原记录。', {'version':VERSION,'error':str(exc)}


def assert_transport(text, content_kind='chat', protected_lines=()):
    # Never rewrite a persisted/claimed part. Operators in explicitly typed code
    # remain data; legacy parts are checked conservatively without retransmission.
    if violations(text, content_kind, protected_lines):
        raise ReplySchemaError('plain_presentation_residual_format')
