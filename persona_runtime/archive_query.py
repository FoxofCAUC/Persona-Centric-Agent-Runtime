"""Bounded literal lexical queries. User text never becomes FTS syntax."""
from datetime import date, datetime, timedelta, timezone
import math
import re
import time
import unicodedata

from .archive import scope
from .models import RuntimeErrorBase

TOKENIZER_VERSION = 'nfkc-casefold-bigram-v1'
MAX_DOCUMENTS = 10000
MAX_SOURCE_CHARS = 2000000
MAX_CANDIDATES = 200
MAX_RESULT_CHARS = 12000
MAX_SECONDS = 5.0
PIECES = re.compile(r'[\u3400-\u9fff]+|[a-z0-9_]+')


class SearchFailure(RuntimeErrorBase):
    pass


class Budget:
    def __init__(self, seconds=None):
        self.started = time.monotonic()
        self.deadline = self.started + (MAX_SECONDS if seconds is None else seconds)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise SearchFailure('query_budget_exceeded')

    def watch(self, conn):
        conn.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 1000)


def normal(text):
    return unicodedata.normalize('NFKC', text).casefold()


def lexical(text, strategy='bigram', *, document=False):
    value = normal(text)
    pieces = PIECES.findall(value)
    unsupported = PIECES.sub('', value)
    if not document and (not pieces or any(not (c.isspace() or (c.isascii() and not c.isalnum())) for c in unsupported)):
        return [], True
    terms = []
    for piece in pieces:
        if '\u3400' <= piece[0] <= '\u9fff':
            width = 3 if strategy == 'trigram' else 2
            if len(piece) < width:
                if document:
                    terms.append(piece)
                    continue
                return [], True
            terms.extend(piece[i:i+width] for i in range(len(piece)-width+1))
        else:
            terms.append(piece)
    return list(dict.fromkeys(terms)), False


def encoded_terms(terms):
    return ['t'+term.encode('utf-8').hex() for term in terms]


def matches(text, query, mode='all_terms', strategy='bigram'):
    value = normal(text)
    if not query.strip():
        return True
    terms, literal = lexical(query, strategy)
    if mode == 'phrase' or literal or strategy == 'substring':
        return normal(query) in value
    document_terms = set(lexical(value, strategy, document=True)[0])
    return all(term in document_terms for term in terms)


def epoch(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def source_epoch(value, axis):
    if axis == 'platform' and isinstance(value,str):
        try:
            number = float(value)
            if math.isfinite(number) and 0 <= number <= 253402300799:
                return number
        except ValueError:
            pass
    return epoch(value)


def validate(args, *, preview=False):
    allowed = {'persona_id','channel','bot','peer','session_id','window'}
    allowed |= {'targets'} if preview else {'query','match_mode','time_axis','start','end','local_date','limit'}
    if not isinstance(args, dict) or set(args)-allowed:
        raise SearchFailure('invalid_query')
    try:
        selected = scope(args['persona_id'], args['channel'], args.get('bot',''), args.get('peer',''))
    except (KeyError, RuntimeErrorBase):
        raise SearchFailure('invalid_query') from None
    if any(not isinstance(v,str) or len(v)>200 or '\x00' in v for v in selected.values()):
        raise SearchFailure('invalid_query')
    try:
        for value in selected.values():
            value.encode('utf-8')
    except UnicodeError:
        raise SearchFailure('invalid_query') from None
    session = args.get('session_id')
    if session is not None and (not isinstance(session,str) or not session or len(session)>200 or '\x00' in session):
        raise SearchFailure('invalid_query')
    window = args.get('window', 'recent_50' if preview else 'all')
    if window not in ('all','recent_50'):
        raise SearchFailure('invalid_query')
    result = {'scope':selected, 'session_id':session, 'window':window}
    if preview:
        targets = args.get('targets', [])
        if not isinstance(targets,list) or len(targets)>20 or (not targets and window=='all'):
            raise SearchFailure('invalid_query')
        for target in targets:
            if (not isinstance(target,dict) or set(target)!={'kind','id'} or target['kind'] not in ('event','turn','part')
                    or not isinstance(target['id'],str) or not 0<len(target['id'])<=200 or '\x00' in target['id']):
                raise SearchFailure('invalid_query')
        return result | {'targets':targets}
    query = args.get('query','')
    if not isinstance(query,str) or len(query)>256 or '\x00' in query:
        raise SearchFailure('invalid_query')
    try:
        query.encode('utf-8')
    except UnicodeError:
        raise SearchFailure('invalid_query') from None
    terms, _ = lexical(query)
    if len(terms)>32:
        raise SearchFailure('invalid_query')
    mode, axis, limit = args.get('match_mode','all_terms'), args.get('time_axis','recorded'), args.get('limit',5)
    if mode not in ('all_terms','phrase') or axis not in ('recorded','received','platform','sent') or type(limit) is not int or not 1<=limit<=20:
        raise SearchFailure('invalid_query')
    start, end = args.get('start'), args.get('end')
    day = args.get('local_date')
    if day is not None:
        if start is not None or end is not None or not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
            raise SearchFailure('invalid_query')
        try:
            dt = datetime.combine(date.fromisoformat(day),datetime.min.time(),timezone(timedelta(hours=8)))
            start, end = dt.isoformat(), (dt+timedelta(days=1)).isoformat()
        except (ValueError, OverflowError):
            raise SearchFailure('invalid_query') from None
    if (start is not None and epoch(start) is None) or (end is not None and epoch(end) is None):
        raise SearchFailure('invalid_query')
    if start is not None and end is not None and epoch(start)>=epoch(end):
        raise SearchFailure('invalid_query')
    if not query.strip() and start is None and end is None:
        raise SearchFailure('invalid_query')
    return result | {'query':query, 'match_mode':mode, 'time_axis':axis, 'start':start,'end':end,'limit':limit}
