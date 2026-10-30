"""Versioned presentation, not a second generation or editable source of truth."""
import hashlib
import json
import re

from .models import identifier, LOCAL_USER_ID
from .presentation import render
from .reply_batch import ReplySchemaError, expression_version
from .qq_text import is_code_line
from .bubble_contract import V3

VERSION = 'chat-sentences-v1'
CAPABILITY = 'reply_display_messages_v1'
SEPARATORS = set('\n\r。！？!?；;，,．.')


def enabled(expression, mode):
    return mode=='natural_structured' and expression_version(expression) in (2,3)


def protected_ranges(text):
    """Conservative literal-data spans; do not infer or paraphrase their meaning."""
    patterns = [
        r'https?://[^\s<>“”「」『』（）。，！？；]+',
        r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',
        r'(?:[A-Za-z]:[\\/]|\\\\)[^\s，。！？；“”]+',
        r'(?:\.{0,2}/)[\w./+%?=&~-]+',
        r'\b(?:[A-Za-z0-9_-]+\.)+[A-Za-z][A-Za-z0-9_-]*\b',
        r'[vV]?[+-]?\d+(?:[.,:/+%=-]\d+)*(?:[eE][+-]?\d+)?(?:[-+][A-Za-z][A-Za-z0-9.-]*)?',
        r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"',
        r'（[^）]*）|\([^()]*\)|\[[^\]\n]*\]|\{[^{}\n]*\}',
        r'\b[A-Za-z]+[\x27’][A-Za-z]+\b',
        r'[:;=8][-^]?[)(DPp/]|[>TQ^][_.-]?[<TQ^]',
    ]
    spans=[(m.start(),m.end()) for pattern in patterns for m in re.finditer(pattern,text)]
    offset=0
    for line in text.splitlines(keepends=True):
        if is_code_line(line) or line.startswith(('    ','\t')):
            spans.append((offset,offset+len(line.rstrip('\r\n'))))
        offset+=len(line)
    return spans


def split_chat(text):
    spans=protected_ranges(text)
    def protected(i):return any(a<=i<b for a,b in spans)
    result=[];start=0
    for i,ch in enumerate(text):
        if ch in SEPARATORS and not protected(i):
            value=text[start:i].strip()
            if value:result.append((value,start,i))
            start=i+1
    value=text[start:].strip()
    if value:result.append((value,start,len(text)))
    if not result:raise ReplySchemaError('chat_display_empty')
    return result


def plan(segments, expression=None):
    if expression_version(expression)==3:return bubble_plan(segments)
    result=[];chat_count=0
    for index,segment in enumerate(segments,1):
        kind=segment.get('content_kind') or 'chat'
        original=segment['text']
        text,diagnostic=render(original,kind)
        # Fenced code in a mixed body is rendered by the plain-text component;
        # preserve the whole mixed body when its removed fences carried typing.
        protected_body=kind!='chat' or bool(re.search(r'(?m)^\s*(`{3,}|~{3,})',original)) or any(is_code_line(line) or line.startswith(('    ','\t')) for line in original.splitlines())
        pieces=[(text,0,len(text))] if protected_body else split_chat(text)
        if not protected_body:chat_count+=len(pieces)
        if chat_count>12:raise ReplySchemaError('chat_display_message_limit')
        for value,start,end in pieces:
            result.append({'semantic_index':index,'display_index':len(result)+1,
                'text':value,'text_hash':hashlib.sha256(value.encode('utf-8')).hexdigest(),
                'content_kind':kind,'transform_version':VERSION,
                'source':{'coordinate':'plain_rendered','start':start,'end':end,
                          'semantic_sha256':hashlib.sha256(original.encode('utf-8')).hexdigest(),
                          'rendered_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest()},
                'diagnostics':{'plain':diagnostic,'protected_body':protected_body,
                               'not_further_split':len(value)>40,'converted':value!=original},
                'wait_ms':min(1800,600+15*len(result[-1]['text'])) if result else 0})
    return result


def persist(conn,batch_id,turn,messages):
    for message in messages:
        conn.execute('INSERT INTO reply_display_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identifier(),batch_id,turn['persona_id'],LOCAL_USER_ID,message['semantic_index'],
             message['display_index'],message['text'],message['text_hash'],message['content_kind'],
             message['transform_version'],json.dumps(message['source'],sort_keys=True),
             json.dumps(message['diagnostics'],ensure_ascii=False,sort_keys=True),message['wait_ms']))


def read(conn,batch_id):
    result=[]
    for row in conn.execute('SELECT * FROM reply_display_messages WHERE batch_id=? ORDER BY display_index',(batch_id,)):
        value=dict(row)
        for key in ('persona_id','user_id'):value.pop(key,None)
        value['source']=json.loads(value.pop('source_json'))
        value['diagnostics']=json.loads(value.pop('diagnostics_json'))
        result.append(value)
    return result


def bubble_plan(segments):
    if not 1<=len(segments)<=V3.max_messages:raise ReplySchemaError('chat_display_message_limit')
    result=[]
    for index,segment in enumerate(segments,1):
        original=segment['text'];kind=segment.get('content_kind') or 'chat'
        text,plain=render(original,kind)
        spans=protected_ranges(text)
        protected_body=kind!='chat' or bool(re.search(r'(?m)^\s*(`{3,}|~{3,})',original)) or any(is_code_line(line) or line.startswith(('    ','\t')) for line in original.splitlines())
        end=len(text);trimmed=False
        # Only one ordinary terminal full stop; ellipses and literal-data spans stay.
        if (not protected_body and text.endswith(('。','.','．'))
                and not text.endswith(('..','。。','．．'))
                and not any(a<=end-1<b for a,b in spans)):
            end-=1;trimmed=True
        value=text[:end]
        if not value.strip():raise ReplySchemaError('chat_display_empty')
        findings=[]
        if not protected_body:
            if len(re.sub(r'\s','',value))>50:findings.append('long_bubble_review')
            if '\n' in value or '\r' in value:findings.append('internal_linebreak_review')
            if any(ch in '。；;' and not any(a<=i<b for a,b in spans) for i,ch in enumerate(value)):
                findings.append('internal_sentence_boundary_review')
        result.append({'semantic_index':index,'display_index':index,'text':value,
            'text_hash':hashlib.sha256(value.encode()).hexdigest(),'content_kind':kind,
            'transform_version':V3.presentation,
            'source':{'coordinate':'plain_rendered','start':0,'end':end,
                'semantic_sha256':hashlib.sha256(original.encode()).hexdigest(),
                'rendered_sha256':hashlib.sha256(text.encode()).hexdigest()},
            'diagnostics':{'plain':plain,'protected_body':protected_body,'terminal_period_removed':trimmed,
                'quality_findings':findings,'non_whitespace_chars':len(re.sub(r'\s','',value)),
                'converted':value!=original},
            'wait_ms':min(1800,600+15*len(result[-1]['text'])) if result else 0})
    return result
