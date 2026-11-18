"""Author-reviewed plain chat presentation for the repaired continuous trial."""
import re

from .chat_display import protected_ranges
from .expression_contract import ProtectedSpan
from .expression_protocol import bubble_plan
from .expression_sources import protected
from .qq_text import is_code_line

VERSION = 'en5-jev-short-chat-v1'
SOCIAL_LIMIT = 72
BUBBLE_TARGET = 32
STOPS = re.compile(r'[。！？!?；;]+[”」』】]*[ \t]*|\r?\n')
PAUSES = re.compile(r'[，、：,]+[ \t]*|(?<=\w) (?=[A-Za-z0-9])')
ASIDE = re.compile(r'（([^（）\n]+)）|\(([^()\n]+)\)')
STAGE_DIRECTION = re.compile(
    r'(?:语气|语调|小声|低声|带着笑|不紧不慢地应|投来|立刻凑近|凑近|托着下巴|'
    r'眨眨眼|眨眼|噗嗤|轻轻一笑|轻笑|微笑|笑|叹气|好奇|歪头|点点头|摇摇头)'
)
NON_CHAT = re.compile(r'(?m)^[ \t]*(?:\d+[.)、]|[-*•]|#{1,6})[ \t]+|https?://')
FENCE = re.compile(r'(?m)^[ \t]*(?:\x60{3,}|~{3,})')


def technical(text):
    return bool(protected(text) or FENCE.search(text) or NON_CHAT.search(text)
                or any(is_code_line(line) or line.startswith(('    ','\t')) for line in text.splitlines()))


def without_directions(text):
    literals=[(s['start'],s['end']) for s in protected(text)]
    ranges=protected_ranges(text)
    edits=[]
    for m in ASIDE.finditer(text):
        content = (m.group(1) or m.group(2)).strip()
        if any(a<=m.start() and m.end()<=b for a,b in literals) or any(
                a<=m.start() and m.end()<=b and (a,b)!=m.span() for a,b in ranges):
            continue
        if len(content)<=32 and STAGE_DIRECTION.match(content):
            edits.append((m.start(),m.end(),''))
        elif m.group(1) and not technical(content) and not re.search(r'[0-9=+*/<>]',content) and (
                m.start()==0 or text[m.start()-1] in '\n。！？；'):
            edits.append((m.start(),m.end(),m.group(1)))
    if not edits:
        return text
    pieces = []
    pos = 0
    for start,end,value in edits:
        pieces.append(text[pos:start])
        pieces.append(value)
        if not value and start and end<len(text) and all(c.isascii() and c.isalnum() for c in (text[start-1],text[end])):
            pieces.append(' ')
        pos = end
    pieces.append(text[pos:])
    spoken = ''.join(pieces).strip()
    return spoken or text


def boundaries(pattern, text, ranges):
    for m in pattern.finditer(text):
        # A quote may end at the same cut, but its punctuation must remain inside the quote.
        if all(not a<m.end()<b and not a<=m.start()<b for a,b in ranges):
            yield m.end()


def speech(raw, scene):
    spoken=without_directions(raw)
    if scene=='help' or len(spoken)<=SOCIAL_LIMIT or technical(spoken):
        return spoken
    ends=[i for i in boundaries(STOPS,spoken,protected_ranges(spoken))
          if 30<=i<=SOCIAL_LIMIT and spoken[:i].rstrip()[-1:] in '。！？!?；;”」』】']
    return spoken[:ends[-1]].rstrip() if ends else spoken


def short_frames(body, split):
    spans=tuple(ProtectedSpan(**s) for s in protected(body))
    if not split or len(body)<=BUBBLE_TARGET or FENCE.search(body) or any(
            is_code_line(line) or line.startswith(('    ','\t')) for line in body.splitlines()):
        return bubble_plan(body,spans,[body])
    ranges=protected_ranges(body)+[(s.start,s.end) for s in spans]
    stops=set(boundaries(STOPS,body,ranges))
    pauses=set(boundaries(PAUSES,body,ranges))
    choices=sorted(stops|pauses)
    parts=[]
    start=0
    while len(body)-start>BUBBLE_TARGET and len(parts)<11:
        limit=start+BUBBLE_TARGET
        available=[i for i in choices if start<i<=limit and body[start:i].strip() and body[i:].strip()]
        sentences=[i for i in available if i in stops and i-start>=14]
        cut=sentences[-1] if sentences else available[-1] if available else None
        if cut is None:
            cut=next((i for i in choices if i>limit and body[i:].strip()),None)
        if cut is None:
            break
        parts.append(body[start:cut])
        start=cut
    if body[start:]:
        parts.append(body[start:])
    return bubble_plan(body,spans,parts)
