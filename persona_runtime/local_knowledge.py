"""Scoped local evidence. Call writers inside Store.connection(write=True)."""
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone

from .archive import encoded, fingerprint
from .knowledge_general import public_url
from .knowledge_service import POLICY, SOURCES, read
from .models import RuntimeErrorBase, timestamp
from . import knowledge_revisions as revisions


VERSION = 'local-first-v1'
_WORDS = re.compile(r'[\u3400-\u9fff]+|[a-z0-9]+')
_AS_OF = re.compile(r'(?:截至|截止到?|在)\s*(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?')
_REFRESH = re.compile(r'(?:上网|联网|在线).{0,8}(?:查|搜|核|确认)|(?:重新|再去|实时).{0,4}(?:核实|核对|查询|搜)|https?://', re.I)
_LOCAL_ONLY = re.compile(r'(?:不用|不要|不必|无需|别|请勿).{0,3}(?:联网|上网)|'
                         r'(?:只|仅)(?:用|查|看).{0,5}(?:已有|本地|本机|之前|现有)(?:的)?(?:资料|知识库|记录|信息|库)?')
_FILLER = re.compile(r'再?说回|回到刚才|我刚才问的|你刚才说[的它她他]|来着|叫什么|请问|麻烦你|能不能|帮我|查一下|查查|查询|告诉我|说一下|我想知道|到底|具体|'
                     r'什么地方|什么时候|哪一天|是什么|是什么时候|是多少|是哪个|哪个|多少|什么|哪里|哪天|几点|'
                     r'请|的|是|在|从|我|呢|呀|吗|了|啊|为|时|有')
_SCOPES = {'session': 0, 'persona': 1, 'instance': 2}


def enabled(db, pid):
    policy = read(db, 'policy.' + pid, POLICY)['data']
    return policy.get('enabled') is True and 'desktop' in policy.get('channels', [])


def local_only(question):
    return bool(_LOCAL_ONLY.search(question))


def _norm(text):
    return unicodedata.normalize('NFKC', text).strip().casefold()


def _tokens(text):
    words = set()
    for word in _WORDS.findall(_norm(text)):
        if '\u3400' <= word[0] <= '\u9fff':
            words.update(word)
            words.update(word[i:i+2] for i in range(len(word)-1))
        else:
            words.add(word)
    return sorted(words)


def _utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            if len(value) != 10:
                raise ValueError()
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise RuntimeErrorBase('local_knowledge_invalid_time') from None


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value)>limit or '\0' in value:
        raise RuntimeErrorBase('local_knowledge_invalid_text')
    return value


def _names(values):
    if not isinstance(values, (list, tuple)) or len(values)>32:
        raise RuntimeErrorBase('local_knowledge_invalid_names')
    return sorted({_text(v, 128).strip() for v in values})


def _grant(db,instance,pid,sid,revision):
    row=db.execute("""SELECT revision,enabled,operation_hash FROM session_privacy_events
        WHERE instance_id=? AND persona_id=? AND session_id=? ORDER BY revision DESC LIMIT 1""",
        (instance,pid,sid)).fetchone()
    return row['operation_hash'] if row and row['enabled'] and row['revision']==revision else None


def put_document(db, *, scope, url, title, body, receipt, acquired_at, source_kind,
                 persona_id=None, session_id=None, consent_revision=0, instance=None, published_at=None,reuse_content=False,
                 versioned=False):
    """The caller explicitly admits material into this scope. Historical sources use admit_legacy."""
    kinds=('page_content','search_content','native_search_summary','search_summary','source_claim')
    if scope not in _SCOPES or (source_kind not in kinds and not (versioned and source_kind=='user_correction')):
        raise RuntimeErrorBase('local_knowledge_invalid_document')
    if scope=='session' and not db.execute('SELECT 1 FROM sessions WHERE id=? AND persona_id=?',
                                          (session_id,persona_id)).fetchone():
        raise RuntimeErrorBase('local_knowledge_scope_changed')
    body = _text(body, 4_000_000)
    grant=_grant(db,instance,persona_id,session_id,consent_revision) if consent_revision else None
    if consent_revision and not grant:raise RuntimeErrorBase('local_knowledge_scope_changed')
    url=public_url(url)
    if versioned:url=revisions.canonical_url(url)
    fields = (scope,persona_id,session_id,consent_revision,grant,url,_text(title,1000),
              hashlib.sha256(body.encode()).hexdigest(),source_kind,_utc(published_at) if published_at else None,
              _utc(acquired_at),_text(receipt,512))
    if versioned:
        prior=db.execute("""SELECT id FROM local_knowledge_documents WHERE scope=? AND persona_id IS ? AND session_id IS ?
            AND consent_revision=? AND grant_hash IS ? AND url=? AND sha256=?
            ORDER BY acquired_at,id LIMIT 1""",(*fields[:6],fields[7])).fetchone()
        if not prior:
            legacy=db.execute("""SELECT id,url FROM local_knowledge_documents WHERE scope=? AND persona_id IS ? AND session_id IS ?
                AND consent_revision=? AND grant_hash IS ? AND sha256=? ORDER BY acquired_at,id""",(*fields[:5],fields[7]))
            prior=next((r for r in legacy if revisions.canonical_url(r['url'])==url),None)
        doc_id=prior[0] if prior else fingerprint(fields)
        check=encoded(dict(document=doc_id,receipt=fields[-1],at=fields[-2],title=fields[6],published_at=fields[9],source_kind=source_kind))
        key='private.study.acquisition.'+fingerprint([doc_id,fields[-1]])
        db.execute("INSERT OR IGNORE INTO knowledge_control VALUES(?,?)",(key,check))
        if db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()[0]!=check:
            raise RuntimeErrorBase('local_knowledge_verification_changed')
        if prior:return doc_id
    elif reuse_content:
        old=db.execute("""SELECT id FROM local_knowledge_documents WHERE scope=? AND persona_id IS ? AND session_id IS ?
            AND consent_revision=? AND grant_hash IS ? AND url=? AND title=? AND sha256=? AND source_kind=? AND published_at IS ?
            ORDER BY acquired_at,id LIMIT 1""",fields[:10]).fetchone()
        if old:return old['id']
    doc_id = fingerprint(fields)
    db.execute("""INSERT INTO local_knowledge_documents
        (id,scope,persona_id,session_id,consent_revision,grant_hash,url,title,sha256,source_kind,published_at,acquired_at,receipt,body)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""", (doc_id,*fields,body))
    return doc_id


def put_fact(db, document_id, *, subject, predicate, value, quote, start, end, aliases=(), attributes=(),
             conditions=(), valid_from=None, valid_until=None, expires_at=None, evidence_state='fact',
             unit=None, previous_id=None, legacy_claim=None, turn_source=None, verified_at=None,versioned=False,
             verification_receipt=None,witnesses=None):
    doc = db.execute('SELECT * FROM local_knowledge_documents WHERE id=?',(document_id,)).fetchone()
    if doc and db.execute('SELECT 1 FROM knowledge_control WHERE key=?',('private.study.withdrawn_document.'+document_id,)).fetchone():
        raise RuntimeErrorBase('local_knowledge_document_withdrawn')
    if not doc or type(start) is not int or type(end) is not int or not 0<=start<end<=len(doc['body']):
        raise RuntimeErrorBase('local_knowledge_invalid_span')
    quote = _text(quote,2000)
    value = _text(value,600)
    if doc['body'][start:end]!=quote or (evidence_state!='source_excerpt' and _norm(value) not in _norm(quote)):
        raise RuntimeErrorBase('local_knowledge_evidence_mismatch')
    if unit is not None and _norm(_text(unit,64)) not in _norm(quote):
        raise RuntimeErrorBase('local_knowledge_evidence_mismatch')
    if evidence_state not in ('fact','announced','observed','rumor','source_excerpt'):
        raise RuntimeErrorBase('local_knowledge_invalid_fact')
    if evidence_state=='source_excerpt' and value!=quote:
        raise RuntimeErrorBase('local_knowledge_evidence_mismatch')
    if valid_from and valid_until and _utc(valid_from)>=_utc(valid_until):
        raise RuntimeErrorBase('local_knowledge_invalid_time')
    if previous_id is not None:
        prior=db.execute("""SELECT f.spec_json,d.scope,d.persona_id,d.session_id,d.consent_revision,d.grant_hash FROM local_knowledge_facts f
            JOIN local_knowledge_documents d ON d.id=f.document_id WHERE f.id=?""",(previous_id,)).fetchone()
        if not prior or any(prior[k]!=doc[k] for k in ('scope','persona_id','session_id','consent_revision','grant_hash')):
            raise RuntimeErrorBase('local_knowledge_invalid_revision')
        fields=json.loads(prior['spec_json'])
    spec=dict(subject=_text(subject,128),predicate=_text(predicate,128),value=value,unit=unit,quote=quote,start=start,end=end,
              aliases=_names(aliases),attributes=_names(attributes),conditions=_names(conditions),state=evidence_state,
              valid_from=_utc(valid_from) if valid_from else None,valid_until=_utc(valid_until) if valid_until else None,
              expires_at=_utc(expires_at) if expires_at else None,legacy_claim=legacy_claim,turn_source=turn_source)
    if witnesses is not None:
        spec['witnesses']=witnesses
    if verified_at is not None:spec['verified_at']=_utc(verified_at)
    if verification_receipt is not None:
        key='private.study.acquisition.'+fingerprint([document_id,verification_receipt])
        check=db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()
        if not check:raise RuntimeErrorBase('local_knowledge_verification_missing')
        check=json.loads(check[0])
        spec['verification']=check
        spec['verified_at']=check['at']
    if previous_id and revisions.subject_key(fields)!=revisions.subject_key(spec):
        raise RuntimeErrorBase('local_knowledge_invalid_revision')
    fact_id=fingerprint([document_id,previous_id,spec])
    terms=''
    entities=properties='[]'
    if evidence_state!='source_excerpt':
        words=' '.join([subject,predicate,*spec['aliases'],*spec['attributes'],*spec['conditions'],quote])
        terms=' '.join(_tokens(words))
        entities=encoded([_norm(s) for s in (subject,*spec['aliases'])])
        properties=encoded([_norm(s) for s in (predicate,*spec['attributes'])])
    inserted=db.execute("""INSERT INTO local_knowledge_facts(id,document_id,previous_id,spec_json,entities,attributes,terms)
                  VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
               (fact_id,document_id,previous_id,encoded(spec),entities,properties,terms))
    if versioned and inserted.rowcount and evidence_state!='source_excerpt':
        revisions.link_observation(db,fact_id,doc,spec)
    return fact_id


def withdraw(db, fact_id):
    if not db.execute('UPDATE local_knowledge_facts SET withdrawn=1 WHERE id=?',(fact_id,)).rowcount:
        raise RuntimeErrorBase('local_knowledge_fact_missing')


def withdraw_document(db,document_id):
    doc=db.execute('SELECT * FROM local_knowledge_documents WHERE id=?',(document_id,)).fetchone()
    if not doc:
        raise RuntimeErrorBase('local_knowledge_invalid_document')
    copies=db.execute("""SELECT id,url FROM local_knowledge_documents WHERE scope=? AND persona_id IS ? AND session_id IS ?
        AND grant_hash IS ? AND sha256=?""",(doc['scope'],doc['persona_id'],doc['session_id'],doc['grant_hash'],doc['sha256']))
    for ident,url in copies:
        if revisions.canonical_url(url)==revisions.canonical_url(doc['url']):
            db.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',('private.study.withdrawn_document.'+ident,encoded(timestamp())))
            db.execute('UPDATE local_knowledge_facts SET withdrawn=1 WHERE document_id=? AND withdrawn=0',(ident,))


def rebuild(db):
    db.execute("INSERT INTO local_knowledge_fts(local_knowledge_fts) VALUES('rebuild')")
    db.execute("INSERT INTO local_knowledge_fts(local_knowledge_fts,rank) VALUES('integrity-check',1)")


def _legacy(db, pid, claim_id):
    row=db.execute("""SELECT c.*,p.url,p.title,p.source_id,p.last_checked,d.body,d.sha256,d.upstream_at,d.fetched_epoch,
        e.start_character,e.end_character,e.quote FROM knowledge_claims c JOIN knowledge_batches b ON b.id=c.batch_id
        JOIN knowledge_pages p ON p.id=c.page_id JOIN knowledge_documents d ON d.id=c.document_id
        JOIN knowledge_evidence e ON e.claim_id=c.id WHERE c.id=? AND b.owner_persona=? AND c.state='active'
        AND c.kind IN ('source_claim','fact') AND b.state='committed' AND b.document_id=d.id AND e.document_id=d.id
        AND p.state='active' AND p.head_document_id=d.id AND d.page_id=p.id AND p.latest_revision=d.upstream_revision""",
        (claim_id,pid)).fetchone()
    if not row:return None
    from .knowledge_store import source_policy
    sources=read(db,'sources',SOURCES)['data']
    if not sources.get(row['source_id'],{}).get('enabled') or not source_policy(db,row['source_id'])['enabled']:return None
    invalid=read(db,'invalidations',{})['data']
    for target in db.execute('SELECT bindings_json FROM knowledge_targets WHERE page_id=?',(row['page_id'],)):
        for binding in json.loads(target[0]):
            if str(binding['article_id']) in invalid.get(binding['package_sha256'],{}):return None
    if hashlib.sha256(row['body'].encode()).hexdigest()!=row['sha256']:return None
    if not 0<=row['start_character']<row['end_character']<=len(row['body']):return None
    if row['body'][row['start_character']:row['end_character']]!=row['quote']:return None
    return row


def admit_legacy(db, pid, claim_id, *, aliases=(), attributes=()):
    from .knowledge_store import source_policy
    row=_legacy(db,pid,claim_id)
    if not row:raise RuntimeErrorBase('local_knowledge_legacy_ineligible')
    acquired=datetime.fromtimestamp(row['fetched_epoch'],timezone.utc).isoformat()
    expires=datetime.fromtimestamp((row['last_checked'] or row['fetched_epoch'])+
        source_policy(db,row['source_id'])['freshness_seconds'],timezone.utc).isoformat()
    doc=put_document(db,scope='persona',persona_id=pid,url=row['url'],title=row['title'],body=row['body'],
        receipt=row['document_id'],source_kind='source_claim',acquired_at=acquired,published_at=row['upstream_at'])
    return put_fact(db,doc,subject=row['subject'],predicate=row['predicate'],value=row['value'],quote=row['quote'],
        start=row['start_character'],end=row['end_character'],valid_from=row['valid_from'],valid_until=row['valid_until'],
        expires_at=expires,legacy_claim=claim_id,aliases=aliases,attributes=attributes)


def _turn_source(db,pid,sid,turn_id,source_id,*,active_turn=None):
    from .private_lookup import evidence
    row=db.execute('SELECT * FROM turns WHERE id=? AND persona_id=? AND session_id=?',(turn_id,pid,sid)).fetchone()
    if not row:return None
    failed=row['status']=='interrupted' and row['error_code']=='private_reply_review_rejected'
    committed=False
    if row['status']!='completed' and db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_study_commits'").fetchone():
        committed=db.execute("""SELECT 1 FROM knowledge_study_commits WHERE turn_id=? AND status='committed'""",(turn_id,)).fetchone()
    data=evidence(db,pid,sid,turn_id,active_turn=turn_id if failed or committed else active_turn)
    return next((s for s in (data or {}).get('sources',[]) if s['id']==source_id),None)


def turn_document(db,pid,sid,turn_id,source_id,*,instance=None,consent=None,active_turn=None,study_receipt=False,versioned=False):
    from .private_lookup import intent
    source=_turn_source(db,pid,sid,turn_id,source_id,active_turn=active_turn)
    if not source:raise RuntimeErrorBase('local_knowledge_legacy_ineligible')
    it=intent(db,turn_id)
    # Only private-reply records have an explicit, inspectable query scope.
    if not it or it['origin'] not in ('session','confirmed','explicit','derived'):
        raise RuntimeErrorBase('local_knowledge_scope_changed')
    record=db.execute("SELECT created_at FROM private_lookup_records WHERE turn_id=? AND outcome='acquired'",(turn_id,)).fetchone()
    if not record:raise RuntimeErrorBase('local_knowledge_legacy_ineligible')
    revision=it['consent_revision'] if it['origin']=='session' else 0
    if it['origin']=='session':
        source_instance=db.execute('SELECT instance_id FROM private_reply_turns WHERE turn_id=?',(turn_id,)).fetchone()[0]
        if not revision or not consent or not consent['enabled'] or consent['revision']!=revision or source_instance!=instance:
            raise RuntimeErrorBase('local_knowledge_scope_changed')
    published=None if source['kind']=='user_correction' else source.get('date')
    study_receipt=study_receipt or active_turn==turn_id
    if published and study_receipt:
        date=re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日',published)
        if date:
            year,month,day=map(int,date.groups())
            published=f'{year:04}-{month:02}-{day:02}'
        try:published=_utc(published)
        except RuntimeErrorBase:published=None
    doc=put_document(db,scope='session',persona_id=pid,session_id=sid,consent_revision=revision,
        instance=instance,reuse_content=study_receipt,versioned=versioned,
        url=source['url'],title=source['title'],body=source.get('content') or source.get('summary'),
        published_at=published,acquired_at=record[0],source_kind=source['kind'],receipt=turn_id)
    return doc,source,record[0],study_receipt


def admit_turn_source(db,pid,sid,turn_id,source_id,*,instance=None,consent=None,active_turn=None,study_receipt=False,versioned=False,**fact):
    doc,source,acquired,study_receipt=turn_document(db,pid,sid,turn_id,source_id,instance=instance,consent=consent,
        active_turn=active_turn,study_receipt=study_receipt,versioned=versioned)
    return put_fact(db,doc,turn_source=[turn_id,source_id,fingerprint(source)],versioned=versioned,
                    verification_receipt=turn_id if versioned else None,
                    verified_at=acquired if study_receipt else None,**fact)


def _matches(text, choices):
    for label in sorted(choices,key=len,reverse=True):
        term=_norm(label)
        pattern=(r'(?<![a-z0-9])' if term[:1].isascii() and term[:1].isalnum() else '')+re.escape(term)
        if term[-1:].isascii() and term[-1:].isalnum():pattern+=r'(?![a-z0-9]|\.\d)'
        if re.search(pattern,text):
            return re.sub(pattern,' ',text)
    return None


def search(db,pid,sid,consent,question,*,instance=None,now=None,after_commit=None,fact_id=None,versioned=False,intent_question=None,for_selection=False):
    """Legacy structured reader for previously bound turns. New workbench reads use semantic_knowledge."""
    try:
        return _search(db,pid,sid,consent,question,instance=instance,now=now,after_commit=after_commit,fact_id=fact_id,
                       versioned=versioned,intent_question=intent_question,for_selection=for_selection)
    except sqlite3.DatabaseError as exc:
        raise storage_error(exc) from exc


def storage_error(exc):
    code=getattr(exc,'sqlite_errorcode',0)
    if code==sqlite3.SQLITE_CORRUPT_VTAB:
        return RuntimeErrorBase('local_knowledge_index_corrupt')
    transient=(sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED,sqlite3.SQLITE_FULL,sqlite3.SQLITE_IOERR,sqlite3.SQLITE_READONLY)
    return RuntimeErrorBase('local_knowledge_storage_wait' if (code & 255) in transient else 'local_knowledge_store_unavailable')


def _calendar_value(value):
    """Compare literal date spellings without inventing a missing year."""
    value=_norm(value)
    match=re.fullmatch(r'(?:(\d{4})[-年/])?(\d{1,2})[-月/](\d{1,2})日?',value)
    if match:
        year,month,day=match.groups()
        y,m,d=int(year) if year else None,int(month),int(day)
    else:
        value=re.sub(r'^(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),\s*','',value)
        match=re.fullmatch(r'([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})',value)
        reverse=re.fullmatch(r'(\d{1,2})\s+([a-z]+)\s+(\d{4})',value)
        if match:month,day,year=match.groups()
        elif reverse:day,month,year=reverse.groups()
        else:return None
        months=('january','february','march','april','may','june','july','august','september','october','november','december')
        names={v:i for i,name in enumerate(months,1) for v in (name,name[:3])}
        y,m,d=int(year),names.get(month,0),int(day)
    try:datetime(y or 2000,m,d)
    except ValueError:return None
    return y,m,d


def clock_precision(value):
    """Compare literal clock precision without assigning a publication date to 'today'."""
    text=_norm(value)
    zone='utc+8' if text.startswith('北京时间') else None
    text=text.removeprefix('北京时间').strip()
    clock=re.search(r'(?<!\d)(\d{1,2})\s*(?:时|点|:)\s*(\d{1,2})\s*分?$',text)
    if not clock:return None
    hour,minute=map(int,clock.groups())
    if hour>23 or minute>59:return None
    prefix=text[:clock.start()].strip()
    date=_calendar_value(prefix)
    if date and date[0]:
        return 'absolute',(hour,minute),zone
    if prefix in ('今天','今日','当天','当日','today','that day'):
        return 'relative',(hour,minute),zone
    return None


def compatible_dates(facts):
    if not all(re.search('日期|时间|时刻|date',f['predicate'],re.I) and f['kind'] in ('fact','observed','announced') for f in facts):
        return False
    def property_key(f):
        name=_norm(f['predicate'])
        if re.fullmatch(r'(?:版本|正式版|正式|版本正式)?(?:发布|发行)(?:日期|时间)',name):
            return 'release-date'
        return name
    if len({(_norm(f['subject']),property_key(f),tuple(f['qualifications']),f.get('unit')) for f in facts})!=1:
        return False
    def components(value):
        text=_norm(value)
        zone='utc+8' if text.startswith('北京时间') else None
        text=text.removeprefix('北京时间').lstrip()
        clock=re.search(r'(?<!\d)(\d{1,2})\s*(?:时|点|:)\s*(\d{1,2})\s*分?$',text)
        if clock:
            hour,minute=(int(v) for v in clock.groups())
            if hour>23 or minute>59:return None
            date=text[:clock.start()].strip()
            day=_calendar_value(date) if date else (None,None,None)
            return (*day,(hour,minute),zone) if day else None
        day=_calendar_value(text)
        return (*day,None,zone) if day else None
    values=[components(f['value']) for f in facts]
    return bool(values) and all(values) and all(len({part for part in field if part is not None})<=1 for field in zip(*values))


def compatible_launch_sites(facts):
    if not all(re.search(r'发射场|发射地点|起飞地点',f['predicate']) and f['kind'] in ('fact','observed') for f in facts):
        return False
    scope={(_norm(f['subject']),_norm(f['predicate']),tuple(f['qualifications']),f.get('unit')) for f in facts}
    names=sorted({_norm(f['value']) for f in facts},key=len)
    return (len(scope)==1 and len(names)==2 and names[0].endswith('发射中心') and names[1].startswith(names[0])
            and names[1][len(names[0]):] in ('发射场','载人航天发射场'))

def _search(db,pid,sid,consent,question,*,instance,now,after_commit,fact_id=None,versioned=False,intent_question=None,for_selection=False):
    """Only claim a hit when an in-scope entity, attribute and all requested qualifiers agree."""
    if not enabled(db,pid):return 'disabled',[]
    text=_norm(question)
    urls=set()
    if not local_only(text) and _REFRESH.search(text):
        if not after_commit:return 'refresh_requested',[]
        for link in re.findall(r'https?://[^\s，。；！？）)]+',question):
            urls.add(public_url(link))
            text=text.replace(_norm(link),' ')
        text=re.sub(r'(?:重新|再去|实时|上网|联网|在线)\s*(?:再|重新)?(?:核实|核对|确认|查询|搜索|查一下|搜一下|查|搜)', ' ',text)
    selector=_norm(intent_question or question)
    if versioned:
        links=re.findall(r'https?://[^\s，。；！？）)]+',intent_question or question)
        urls={revisions.canonical_url(public_url(link)) for link in links}
        selector=re.sub(r'https?://[^\s，。；！？）)]+',' ',selector)
    historical=_AS_OF.search(selector if versioned else text)
    today=_utc(now or timestamp())
    when=today
    if historical:
        try:
            when=datetime(*(int(x) for x in historical.groups()),tzinfo=timezone.utc).isoformat()
        except ValueError:return 'insufficient',[]
        text=_AS_OF.sub(' ',text,count=1)
    text=_LOCAL_ONLY.sub(' ',text)
    latest='最新' in text
    text=re.sub('目前|当前|现在|最新',' ',text)
    state=revisions.requested_state(selector) if versioned else None
    wanted_versions=revisions.query_parts(selector)[0] if versioned else set()
    if len(wanted_versions)>1:return 'insufficient',[]
    words=_tokens(text)
    if not words or len(words)>256:return 'insufficient',[]
    expr=' OR '.join('"'+word+'"' for word in words)
    revision=consent['revision'] if consent['enabled'] else 0
    grant=_grant(db,instance,pid,sid,revision) if revision else None
    sql="""SELECT f.id fact_id,f.previous_id,f.spec_json,d.id,d.scope,d.persona_id,d.session_id,d.consent_revision,d.grant_hash,
        d.receipt,d.sha256,d.url,d.title,d.source_kind,
        d.published_at,d.acquired_at FROM local_knowledge_fts
        JOIN local_knowledge_facts f ON f.rowid=local_knowledge_fts.rowid
        JOIN local_knowledge_documents d ON d.id=f.document_id WHERE local_knowledge_fts MATCH ? AND f.withdrawn=0
        AND (d.scope='instance' OR (d.persona_id=? AND (d.scope='persona' OR
        (d.session_id=? AND (d.consent_revision=0 OR (d.consent_revision=? AND d.grant_hash=?))))))
        AND EXISTS (SELECT 1 FROM json_each(f.entities) WHERE instr(?,value)>0)
        AND EXISTS (SELECT 1 FROM json_each(f.attributes) WHERE instr(?,value)>0)
        ORDER BY rank,f.id"""
    rows=db.execute(sql,(expr,pid,sid,revision,grant,text,text))
    matches=[]
    reason='miss'
    for row in rows:
        spec=json.loads(row['spec_json'])
        if versioned and spec.get('verification'):
            row=dict(row)
            check=spec['verification']
            row.update(title=check['title'],published_at=check['published_at'],source_kind=check['source_kind'])
        if urls and (revisions.canonical_url(row['url']) if versioned else row['url']) not in urls:continue
        rest=_matches(text,[spec['subject'],*spec['aliases']])
        if rest is None:continue
        reason='insufficient'
        rest=_matches(rest,[spec['predicate'],*spec['attributes']])
        if rest is None:continue
        conditions=spec['conditions']
        if versioned:
            versions,conditions=revisions.split_conditions(conditions)
            if len(versions)>1:continue
            _,rest=revisions.query_parts(rest)
        for condition in conditions:
            rest=revisions.match_condition(rest,condition) if versioned else _matches(rest,[condition])
            if rest is None:break
        if rest is None:continue
        if spec['state']=='rumor' and not versioned:
            if not re.search('爆料|传闻',rest):continue
            rest=re.sub('爆料|传闻',' ',rest)
        if re.search(r'\w',_FILLER.sub('',rest)):continue
        if historical and not spec['valid_from'] and not versioned:continue
        if latest and not versioned and not (spec['expires_at'] or spec['valid_until']):continue
        if (row['acquired_at']>today or (spec['valid_from'] and when<spec['valid_from']) or
                (not versioned and ((spec['valid_until'] and when>=spec['valid_until']) or (spec['expires_at'] and today>=spec['expires_at'])))):
            reason='expired';continue
        if versioned:
            if spec.get('verified_at',row['acquired_at'])>today:continue
            if historical and spec['state'] in ('rumor','announced') and (row['published_at'] or row['acquired_at'])>when:continue
            start=spec['valid_from'] or row['published_at'] or row['acquired_at']
            if when<start or revisions.superseded(db,row['fact_id'],when,wanted_versions,state):continue
            row=dict(row)
            effective=db.execute('SELECT value FROM knowledge_control WHERE key=?',('private.study.effective.'+row['fact_id'],)).fetchone()
            row['effective_since']=json.loads(effective[0]) if effective else start if row['previous_id'] else None
        if spec['legacy_claim']:
            old=_legacy(db,pid,spec['legacy_claim'])
            if not old or (old['document_id'],old['subject'],old['predicate'],old['value'],old['quote'],old['sha256'])!=(
                    row['receipt'],spec['subject'],spec['predicate'],spec['value'],spec['quote'],row['sha256']):continue
            if any((_utc(old[k]) if old[k] else None)!=spec[k] for k in ('valid_from','valid_until')):continue
        if spec['turn_source']:
            turn_id,source_id,signature=spec['turn_source']
            source=_turn_source(db,pid,sid,turn_id,source_id)
            if not source or fingerprint(source)!=signature:continue
        fact={'id':row['fact_id'],'text':spec['quote'],'value':spec['value'],'subject':spec['subject'],'predicate':spec['predicate'],
              'qualifications':spec['conditions'],'kind':spec['state'],'date':row['published_at'],'url':row['url'],
              'title':row['title'],'source_ids':[row['id']],'source_kind':row['source_kind'],'verified_at':spec.get('verified_at',row['acquired_at']),
              'valid_from':spec['valid_from'],'valid_until':spec['valid_until'],'span':[spec['start'],spec['end']]}
        if spec['unit'] is not None:fact['unit']=spec['unit']
        if 'witnesses' in spec:fact['witnesses']=spec['witnesses']
        from_commit=bool(spec['turn_source'] and spec['turn_source'][0]==after_commit)
        matches.append((_SCOPES[row['scope']],fact,from_commit,row,spec))
    if not matches:return reason,[]
    if for_selection and fact_id and versioned:
        # An attribute is a search alias. Two different claims can legitimately share it.
        # Compare revisions of the requested claim before deciding conflict or source priority.
        target=next((spec for _,f,_,_,spec in matches if f['id']==fact_id),None)
        if target is None:return 'insufficient',[]
        family=revisions.subject_key(target)
        matches=[item for item in matches if revisions.subject_key(item[4])==family]
    best=min(item[0] for item in matches)
    if versioned:
        options=dict(when=when,today=today,historical=bool(historical),
            archived_state=bool(state and re.search('原来|当时|之前|原先|此前|以前',selector)),
            current_only=bool(re.search('现在|当前|目前|最新|如今',selector)),
            state=state,versions=wanted_versions,
            allow_rumor=for_selection or bool(re.search('快要|即将|前瞻|下[个一]版本|预告',selector)))
        for level in sorted({item[0] for item in matches}):
            candidates=[(r,s,f) for priority,f,_,r,s in matches if priority==level]
            outcome,items=revisions.project(candidates,after_commit=None,fact_id=None,**options)
            if outcome=='insufficient':continue
            if outcome=='hit' and (after_commit or fact_id):
                return revisions.project(candidates,after_commit=after_commit,fact_id=fact_id,**options)
            return outcome,items
        return 'insufficient',[]
    facts=[f for priority,f,_,_,_ in matches if priority==best]
    order=None
    if len({(_norm(f['subject']),_norm(f['predicate']),_norm(f['value']),f.get('unit'),tuple(f['qualifications']),f['kind']) for f in facts})!=1:
        if not (compatible_dates(facts) or compatible_launch_sites(facts)):
            from .knowledge_catalog import catalog_order
            order=catalog_order(facts)
            if order is None:return 'conflict',[]
    if after_commit:
        facts=[f for priority,f,fresh,_,_ in matches if priority==best and fresh]
    if fact_id is not None:
        facts=[f for f in facts if f['id']==fact_id]
    if order is not None:
        facts.sort(key=lambda f:order[f['id']])
        if len(facts)>8:
            # Fill the bounded result with distinct entries before repeat attestations.
            first={}
            for fact in facts:first.setdefault(order[fact['id']],fact['id'])
            representatives=set(first.values())
            facts.sort(key=lambda f:f['id'] not in representatives)
    return ('hit',facts[:8]) if facts else ('insufficient',[])
