"""Identify numbered entries in an evidenced catalogue and keep their original citations."""
import re
import unicodedata


def _norm(text):
    return unicodedata.normalize('NFKC', text).strip().casefold()


_FIELD = re.compile(r'(?:章节|课程|教材|图书|内容)?目录(?:条目)?|章节列表|章节名称|课程大纲|教学大纲|课程提纲|(?:课程大纲)?周次安排|'
                    r'table of contents|chapter list|syllabus')
_OVERVIEW = re.compile(r'目录|大纲|提纲|脉络|章节安排|章节列表|哪些章|课程框架|'
                       r'table of contents|chapter list|syllabus|course outline')
_DIGITS = '零一二三四五六七八九'
_NUMBER = r'(?:[0-9]{1,4}|[零〇一二两三四五六七八九十百千]{1,8})'
_CN = re.compile(r'第\s*('+_NUMBER+r')\s*(章|周|课|讲|单元|篇|部分|编)')
_EN = re.compile(r'\b(chapter|week|lesson|unit|part)\s+([0-9]{1,4}(?:\.[0-9]{1,4})*|[ivxlcdm]{1,15})\b')
_ROMAN = re.compile(r'm{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})')
_APPENDIX = re.compile(r'(?:appendix\s+([a-z])|([a-z])(?=\.\d))((?:\.\d{1,4})*)(?=[\s.:、)—])')
_SECTION = re.compile(r'\s*第\s*('+_NUMBER+r')\s*节')
_SESSION = re.compile(r'\s*,\s*session\s+([0-9]{1,4})\b(?!\.\d)')
_INDEX = re.compile(r'([0-9]{1,4}(?:\.[0-9]{1,4})*)(?:[、):]|\.(?!\d)|\s+)')
_UNITS = {'chapter':'章','week':'周','lesson':'课','unit':'单元','part':'部分'}
_SUMMARY = {'主要内容','内容概要','课程概述','概述','内容简介','overview','summary'}


def is_overview(question):
    text = _norm(question)
    return bool(_OVERVIEW.search(text) and not (_CN.search(text) or _EN.search(text) or _APPENDIX.match(text)))


def _number(text):
    if text.isascii():
        return int(text)
    text = text.replace('〇','零').replace('两','二')
    units = {'千':1000,'百':100,'十':10}
    if all(c in _DIGITS for c in text):
        return int(''.join(str(_DIGITS.index(c)) for c in text)) if len(text)<=4 else None
    total, previous = 0, 10000
    digit = None
    for i, c in enumerate(text):
        if c in units:
            unit = units[c]
            if i==0 and c=='十':
                digit=1
            if not digit or unit>=previous:
                return None
            total += digit*unit
            previous, digit = unit, None
        else:
            if digit:
                return None
            digit = _DIGITS.index(c)
    if digit and previous >= 100 and text[-2] in units:
        return None
    return total + (digit or 0)


def _roman_value(text):
    if not _ROMAN.fullmatch(text):
        return None
    weights = dict(i=1,v=5,x=10,l=50,c=100,d=500,m=1000)
    values = [weights[letter] for letter in text]
    return sum(-n if i+1<len(values) and n<values[i+1] else n for i,n in enumerate(values))


def entry(fact):
    if not _FIELD.fullmatch(_norm(fact['predicate'])):
        return None
    original = unicodedata.normalize('NFKC', fact['value']).strip()
    value = original.casefold()
    match = _CN.match(value)
    if match:
        number, unit = match.groups()
        position = (_number(number),)
        if unit=='章' and (section := _SECTION.match(value,match.end())):
            position += (_number(section[1]),)
            match = section
    elif match := _EN.match(value):
        label, number = match.groups()
        unit = _UNITS[label]
        if number[0].isdigit():
            position = tuple(int(n) for n in number.split('.'))
        elif original[match.start(2):match.end(2)].isupper():
            position = (_roman_value(number),)
        else:
            return None
        if label=='week' and len(position)==1 and (session := _SESSION.match(value,match.end())):
            position += (int(session[1]),)
            match = session
    elif match := _APPENDIX.match(value):
        unit = '附录'
        letter = match[1] or match[2]
        position = (ord(letter)-ord('a')+1,)
        if match[3]:
            position += tuple(int(n) for n in match[3][1:].split('.'))
    elif match := _INDEX.match(value):
        # Dotted outline numbers share chapter positions (Chapter 1, 1.1, 1.2).
        unit = '章'
        position = tuple(int(n) for n in match[1].split('.'))
    else:
        return None
    if None in position:
        return None
    title = value[match.end():].lstrip(' \t:、.-—')
    next_label = _CN.match(title)
    if not title or (next_label and next_label[2]==unit) or re.match(r'(?:至|到|~|–)\s*第?', title):
        return None
    # Some publishers pad Han titles for typesetting (总　则 / 总则).
    title = re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', title)
    return unit, position, ' '.join(title.split())


def _context(fact):
    return (_norm(fact['subject']), tuple(fact['qualifications']), fact['kind'], fact.get('unit'))


def _family(unit):
    return '章' if unit=='附录' else unit


def catalog_order(facts):
    """Return a stable position order only if all facts can coexist in the same catalogue."""
    positions = {}
    order = {}
    family = None
    for fact in facts:
        parsed = entry(fact)
        if parsed is None:
            return None
        unit, position, title = parsed
        identity = (_context(fact), _norm(fact['predicate']), _family(unit))
        if family is not None and identity != family:
            return None
        family = identity
        position = (unit=='附录', *position)
        if positions.setdefault(position, title) != title:
            return None
        order[fact['id']] = position
    return order


def outline_groups(facts):
    """J5 can choose a source's ordered catalogue with that source's introductory fact."""
    groups = {}
    summaries = {}
    for fact in facts:
        parsed = entry(fact)
        documents = [(e['document_id'],) for e in fact['evidence']] if 'revision_id' in fact else [tuple(fact['source_ids'])]
        for document in documents:
            source = (_context(fact), document)
            if parsed:
                key = (source, _norm(fact['predicate']), _family(parsed[0]))
                groups.setdefault(key, []).append(fact)
            elif _norm(fact['predicate']) in _SUMMARY:
                summaries.setdefault(source, fact)
    alternatives = []
    for (source, _, _unit), items in groups.items():
        order = catalog_order(items)
        if not order:
            continue
        by_position = {}
        for item in items:
            by_position.setdefault(order[item['id']], item)
        group = [by_position[n] for n in sorted(by_position)]
        if source in summaries:
            group.append(summaries[source])
        alternatives.append((len(by_position),group))
    # Prefer sources offering multiple positions. Keep single-source options separate even for sparse outlines.
    multiple = any(size>1 for size,_ in alternatives)
    eligible = {tuple(f['id'] for f in group):group for size,group in alternatives if size>1 or not multiple}
    return {f'fact/{index}':group for index,group in enumerate(eligible.values())}
