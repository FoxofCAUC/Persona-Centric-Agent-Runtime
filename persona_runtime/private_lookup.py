"""Turn-scoped lookup authority, durable tool receipts and readable workbench evidence."""
import json
import re
import sqlite3

from . import local_lookup_route
from .archive import encoded, fingerprint
from .call_limits import gate, turn_scope
from .knowledge_general import public_url
from .knowledge_native import NO_QUERY, QUERY_REQUEST, SENSITIVE_QUERY, public_query_span
from .knowledge_native_memory import apply_states, apply_source_states, eligible
from .knowledge_native_topic import TopicQuery, is_followup
from .knowledge_service import POLICY, read
from .models import RuntimeErrorBase,identifier,timestamp
from .progress import record
from .turn_input import load_turn_input

LONG = re.compile(r'(?:写|给|完成|提供|输出|展开|列出|生成|撰写).{0,14}(?:长文|全文|(?:完整|详细).{0,6}(?:报告|文章|代码|推导|说明|方案|文档|教程))|(?:^|[，。！？\s]|请|想要|需要|给我)(?:你)?(?:详细|完整)(?:讲讲|讲解|解释|展开|说明)')
NO_LONG = re.compile(r'(?:别|不用|不要|无需|不想|不用再).{0,12}(?:长文|全文|详细|展开|完整)')
SENTENCE_END = re.compile(r'[。！？!?]+|\.(?!\d)')
QUALIFIER = re.compile(r'截至|尚未|仅限|可能|未确认|推测|为准|预计|暂定|尚无|未公布')
# A preceding public release question can carry both the work and the time being asked about.
RELEASE_TOPIC = re.compile(
    r'(?:最近|近期|现在|听说)?\s*《[^《》\n]{1,64}》\s*(?:的)?'
    r'(?:下(?:一|个)(?:次|版本|季|期)?|新版本)?(?:是不是|是否|会不会|有消息说)?'
    r'(?:马上|即将|快要|就要|快|要|刚刚|刚|已经)?(?:更新|上线|公测|发布|上映|开播|发行)(?:了|了吗|吗)?'
)


def sentence_count(body):
    from .chat_display import protected_ranges
    if not body.strip():
        return 0
    spans=protected_ranges(body)
    return 1 + sum(bool(body[m.end():].strip(' ”」』）)')) for m in SENTENCE_END.finditer(body)
                   if not any(a<=m.start()<b for a,b in spans))


def single_sentence(body):
    from .bubble_limits import MAX_BUBBLE_CHARS
    return len(body)<=MAX_BUBBLE_CHARS and '\n' not in body.strip() and sentence_count(body)==1


def wants_long(messages):
    texts=[item['text'] for item in messages]
    return any(LONG.search(t) for t in texts) and not any(NO_LONG.search(t) for t in texts)


def enabled(db,pid):
    reader=read(db,'policy.'+pid,POLICY)['data']
    return bool(reader.get('enabled') and 'desktop' in reader.get('channels',[])
                and read(db,'web.policy.'+pid,{})['data'].get('enabled'))


def notice(db,tid):
    return db.execute("""SELECT p.*,u.id unit FROM expression_speech_units u JOIN expression_speech_publications p
        ON p.utterance_id=u.id WHERE u.turn_id=? AND u.source_kind='maibot_private_reply' AND u.phase='search_notice'""",
        (tid,)).fetchone()


def presented_notice(db,tid):
    row=notice(db,tid)
    if row and row['state']=='complete' and row['canonical_event_id'] and not db.execute("""
        SELECT 1 FROM expression_speech_items WHERE publication_id=? AND state!='presented'""",(row['id'],)).fetchone():
        return row['canonical_event_id']
    return None


def visible_notice(db,tid):
    return presented_notice(db,tid) is not None


def _task(question, *, url=None, retain_page=False):
    result={'question':question}
    if url:
        result.update(intent='read_link',url=public_url(url),retain_page=bool(retain_page))
    return result


def projection(db,turn,ids,full,*,grounded=False):
    members=load_turn_input(db,turn)
    chosen=[m for m in members if m['id'] in ids] or members
    if any(NO_QUERY.search(m['text']) for m in members):
        return None,'declined'
    spans=[]
    explicit=False
    for m in chosen:
        saved=db.execute("SELECT value FROM knowledge_control WHERE key=?",('private.lookup.span.'+m['id'],)).fetchone()
        legacy=read(db,'native.public_event.'+m['id'],{})['data']
        if saved:
            selected=json.loads(saved[0])
            if selected['hash']!=fingerprint(m['text']) or selected['session']!=turn['session_id']:
                raise RuntimeErrorBase('private_lookup_scope_changed')
            if full or not SENSITIVE_QUERY.search(selected['text']):
                spans.append(_task(selected['text']))
                continue
        if legacy:
            if (legacy['input_hash']!=fingerprint(m['text']) or legacy['persona_id']!=turn['persona_id']
                    or legacy['session_id']!=turn['session_id']):
                raise RuntimeErrorBase('private_lookup_scope_changed')
            value=legacy['task']
            spans.append(_task(value['question'],url=value.get('url'),retain_page=value.get('retain_page',False)))
            explicit=True
    if spans:
        task=spans[0] if len(spans)==1 else _task('\n'.join(q['question'] for q in spans))
        return task,'session' if full and (not explicit or len(spans)>1) else 'explicit'
    if full:
        question='\n'.join(m['text'] for m in chosen)
        urls=re.findall(r'https?://[^\s，。；！？）)]+',question)
        return _task(question,url=urls[0] if len(urls)==1 and re.search('读取|阅读|读一下',question) else None),'session'
    if len(chosen)==1 and not SENSITIVE_QUERY.search(chosen[0]['text']):
        value=public_query_span(chosen[0]['text'])
        if value:
            matches=list(QUERY_REQUEST.finditer(chosen[0]['text']))
            subject=chosen[0]['text'][matches[-1].end():].strip() if matches else ''
            if subject and single_query(subject):
                if grounded and '《' not in subject:
                    prefix=chosen[0]['text'][:matches[-1].start()].rstrip('，, \t')
                    preceding=re.split(r'[。！？!?\n]',prefix.rstrip('？?'))[-1]
                    if RELEASE_TOPIC.fullmatch(preceding.strip()):
                        subject=preceding.strip()+'？'+subject
                return _task(subject,url=value.get('url')),'derived'
    return None,None


def single_query(subject):
    return not re.search(r'[\n。！？!?].*\S',subject.strip().rstrip('。！？!?'))


def intent(db,tid):
    return db.execute("SELECT * FROM private_lookup_intents WHERE turn_id=?",(tid,)).fetchone()


def bind_span(db,cfg,sid,eid,text,span):
    from .private_reply_entry import selected,RESEARCH
    config=cfg.load()
    if (selected(db,config['instance_id'])!=RESEARCH or not isinstance(span,str) or not span.strip()
            or span not in text):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    # This is the player's selection from the composer. External permission is checked when forming the query.
    db.execute("INSERT INTO knowledge_control VALUES(?,?)",('private.lookup.span.'+eid,
               encoded({'hash':fingerprint(text),'text':span,'session':sid})))


def composer_span(store,config,request_id,task,*,batch):
    """Preserve old receipts while accepting a legacy composer on the v3 route."""
    from .private_reply_entry import selected,RESEARCH
    from .knowledge_native import public_task
    if not isinstance(task,dict) or task.get('request_search') is not True:
        return None
    with store.connection() as db:
        if batch:
            prior=db.execute("""SELECT json_extract(b.policy_json,'$.private_reply_route') FROM ingress_receipts r
                JOIN input_batches b ON b.id=r.batch_id WHERE r.channel='desktop' AND r.request_id=?""",(request_id,)).fetchone()
        else:
            prior=db.execute("""SELECT r.route FROM turns t LEFT JOIN private_reply_turns r ON r.turn_id=t.id
                WHERE t.source_key=?""",('desktop:'+request_id,)).fetchone()
        route=prior[0] if prior else selected(db,config.load()['instance_id'],store.current(db,required=False))
    if route!=RESEARCH:return None
    structural=public_task({k:v for k,v in task.items() if k!='request_search'})
    if structural['intent']!='search' or structural.get('url'):
        raise RuntimeErrorBase('native_public_task_invalid')
    return structural['question']


def bind_long(db,config,eid,text):
    from .private_reply_entry import selected,RESEARCH
    if selected(db,config.load()['instance_id'])!=RESEARCH:
        raise RuntimeErrorBase('private_reply_phase_unavailable')
    db.execute("INSERT INTO knowledge_control VALUES(?,?)",('private.lookup.long.'+eid,encoded({'hash':fingerprint(text)})))


def explicit_long(db,members):
    for m in members:
        row=db.execute("SELECT value FROM knowledge_control WHERE key=?",('private.lookup.long.'+m['id'],)).fetchone()
        if row and json.loads(row[0])['hash']==fingerprint(m['text']):
            return not any(NO_LONG.search(v['text']) for v in members)
    return wants_long(members)


def edit_source(service,pid,source_id,state,expected_revision,operation_id,replacement):
    with service.store.connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='private_lookup_records'").fetchone():
            return None
        row=db.execute("""SELECT r.session_id,json_extract(s.value,'$.url') FROM private_lookup_records r,
            json_each(r.sources_json) s WHERE r.persona_id=? AND json_extract(s.value,'$.id')=? LIMIT 1""",
            (pid,source_id)).fetchone()
        if not row:return None
        key='private.lookup.sources.'+pid+'.'+row[0]
        states=read(db,key,{})['data']
    states[source_id]={'state':state,'url':row[1],'policy_revision':expected_revision+1,
                       **({'replacement':replacement.strip()} if state=='corrected' else {})}
    return service._update(key,states,expected_revision,operation_id,
                           request=[source_id,state,expected_revision,replacement])


def confirm(store,pid,sid,tid,expected,op,text=None):
    from .workbench_timeline import require_session
    if not isinstance(op,str) or not 1<=len(op)<=200:
        raise RuntimeErrorBase('private_lookup_confirmation_invalid')
    with store.connection(write=True) as db:
        require_session(db,pid,sid)
        t=db.execute("SELECT * FROM turns WHERE id=? AND session_id=? AND persona_id=? AND channel='desktop'",(tid,sid,pid)).fetchone()
        it=intent(db,tid)
        if not t or not it or it['binding']!=expected:
            raise RuntimeErrorBase('private_lookup_scope_changed')
        task=None
        if text is not None:
            if not isinstance(text,str) or not text.strip() or len(text)>4000 or SENSITIVE_QUERY.search(text) or NO_QUERY.search(text):
                raise RuntimeErrorBase('native_public_task_invalid')
            if not any(text in m['text'] for m in load_turn_input(db,dict(t))):
                raise RuntimeErrorBase('private_lookup_scope_changed')
            task=_task(text.strip())
            if local_lookup_route.receipt(db,tid):
                from .config import ConfigStore
                instance=ConfigStore(store.paths).load()['instance_id']
                resolved=local_lookup_route.resolved_query(db,t,json.loads(it['target_json']),text.strip(),
                    {'enabled':False,'revision':0},instance,public_only=True,
                    versioned=local_lookup_route.revision_mode(db,tid))
                if resolved!=text.strip() and not SENSITIVE_QUERY.search(resolved):
                    task=_task(resolved)|{'confirmed_span':text.strip()}
        raw=encoded(task) if task else None
        if it['confirmation_id']==op and it['query_json']==raw:
            return True
        if t['cancelled_at'] or it['state']!='waiting' or t['status']=='completed':
            raise RuntimeErrorBase('private_lookup_confirmation_closed')
        from .desktop_recovery import validate_turn
        validate_turn(db,t)
        db.execute("UPDATE private_lookup_intents SET state=?,origin=?,query_json=?,confirmation_id=? WHERE turn_id=?",
                   ('ready' if task else 'skipped','confirmed' if task else 'skipped',raw,op,tid))
        db.execute("UPDATE turns SET status='queued',error_code=NULL,updated_at=? WHERE id=?",(timestamp(),tid))
        record(db,tid,'retry_queued')
        return True


def evidence(db,pid,sid,origin, *, active_turn=None):
    if not origin:
        return None
    root=db.execute("SELECT * FROM turns WHERE id=? AND persona_id=? AND session_id=? AND channel='desktop'",(origin,pid,sid)).fetchone()
    if not root or root['cancelled_at'] or origin!=active_turn and not eligible(db,origin):
        return None
    if db.execute("""SELECT 1 FROM source_exclusions WHERE event_id IN (?,?) OR event_id IN
        (SELECT event_id FROM input_batch_members WHERE batch_id=?)""",(root['user_event_id'],root['reply_event_id'],root['input_batch_id'])).fetchone():
        return None
    row=db.execute("SELECT * FROM private_lookup_records WHERE turn_id=? AND persona_id=? AND session_id=?",(origin,pid,sid)).fetchone()
    if row:
        if row['outcome']!='acquired':
            return None
        sources=json.loads(row['sources_json']); notes=row['notes']
        states=read(db,'private.lookup.sources.'+pid+'.'+sid,{})['data']
        sources, organized=apply_source_states(states,sources,{'notes':notes})
    else:
        data=read(db,'conversation.record.'+origin,{})['data']
        outcome=data.get('retrieval',{})
        if (data.get('session_id')!=sid or data.get('persona_id')!=pid or data.get('root_turn_id')!=origin
                or outcome.get('search_outcome')!='completed' or not outcome.get('material_acquired')):
            return None
        sources=data.get('sources',[]);notes=data.get('organized',{}).get('notes','')
        sources, organized=apply_states(db,pid,sources,{'notes':notes})
    if not sources:return None
    return {'origin':origin,'sources':sources,'notes':organized.get('notes','')}


def refers_back(question):
    return '《' not in question and '://' not in question and (
        is_followup(question) or re.match(r'^(?:那|所以|刚才|你说的|它|她|他|这[个次场])',question.strip()) is not None)


def previous(db,turn,question,allow_private,*,grounded=False):
    topic=TopicQuery(question)
    if grounded and refers_back(question):
        prior=db.execute("""SELECT t.id,r.outcome,r.source_turn FROM turns t
            LEFT JOIN private_lookup_records r ON r.turn_id=t.id
            WHERE t.session_id=? AND t.persona_id=? AND t.channel='desktop' AND t.status='completed'
            AND t.rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY t.rowid DESC LIMIT 1""",
            (turn['session_id'],turn['persona_id'],turn['id'])).fetchone()
        if prior and prior['outcome'] in ('acquired','reused') and eligible(db,prior['id']):
            origin=prior['id'] if prior['outcome']=='acquired' else prior['source_turn']
            if allow_private or not _private_origin(db,origin):
                prior_data=evidence(db,turn['persona_id'],turn['session_id'],origin)
                if prior_data:return prior_data
    rows=db.execute("""SELECT t.id FROM turns t WHERE t.persona_id=? AND t.session_id=? AND t.status='completed'
        AND t.rowid<(SELECT rowid FROM turns WHERE id=?) AND (EXISTS(SELECT 1 FROM private_lookup_records r
        WHERE r.turn_id=t.id AND r.outcome='acquired') OR EXISTS(SELECT 1 FROM knowledge_control k
        WHERE k.key='knowledge.conversation.record.'||t.id)) ORDER BY t.rowid DESC LIMIT 24""",
        (turn['persona_id'],turn['session_id'],turn['id']))
    best=None;rank=0
    for row in rows:
        if not allow_private and _private_origin(db,row[0]):continue
        data=evidence(db,turn['persona_id'],turn['session_id'],row[0])
        if not data:continue
        prior=dict(db.execute("SELECT * FROM turns WHERE id=?",(row[0],)).fetchone())
        old='\n'.join(m['text'] for m in load_turn_input(db,prior))
        score=4 if old==question else topic.score(previous_question=old,titles=[s['title'] for s in data['sources']],
                    urls=[s['url'] for s in data['sources']],notes=data['notes'])
        if is_followup(question) and best is None:score=max(1,score)
        if score>rank:rank,best=score,data
    return best


def research_citations(db,data):
    row=db.execute("""SELECT result_json FROM private_lookup_calls
        WHERE turn_id=? AND step='native' AND state='received'""",(data['origin'],)).fetchone()
    if not row:return []
    receipt=json.loads(row[0])
    if receipt.get('notes')!=data['notes']:return []
    sources={s['url']:s for s in data['sources']}
    spans=[]
    for annotation in receipt.get('annotations',[]):
        cite=annotation.get('url_citation') if isinstance(annotation,dict) else None
        if not isinstance(cite,dict) or not isinstance(cite.get('url'),str):continue
        try:
            url=public_url(cite['url'])
        except RuntimeErrorBase:
            continue
        source=sources.get(url)
        start,end=cite.get('start_index'),cite.get('end_index')
        if source and type(start) is int and type(end) is int and 0<=start<end<=len(data['notes']):
            spans.append((start,end,source))
    return spans


def ranked(data,question,*,grounded=False,citations=()):
    """Keep intact passages with their provenance, dates and supplied qualifications."""
    from .chat import terms
    query=terms(question); candidates=[]
    for s in data['sources']:
        raw=s.get('content') or s.get('summary') or ''
        if grounded and s.get('title') and s['title']!=s['url']:
            title=s['title']
            item={'id':fingerprint([s['id'],'title',title]),'text':title,'source_ids':[s['id']],
                  'kind':'source_title','date':s.get('date'),'qualifications':[],'url':s['url']}
            candidates.append((len(query & terms(title)),item))
        parts=[p for p in re.split(r'\n+|(?<=[。！？])',raw) if p.strip() and len(p)<=600]
        for index,passage in enumerate(parts):
            limitations=[s['qualification']] if s.get('qualification') else []
            limitations.extend(p for p in parts[max(0,index-1):index+2] if p!=passage and QUALIFIER.search(p))
            item={'id':fingerprint([s['id'],index,passage,s.get('state_revision',0)]),
                  'text':passage,'source_ids':[s['id']],'kind':s.get('kind'),'date':s.get('date'),
                  'qualifications':limitations,'url':s['url']}
            candidates.append((len(query & terms(passage+' '+s['title'])),item))
    # A synthesis is identified separately from the pages it summarizes.
    dates=list(dict.fromkeys(s.get('date') for s in data['sources'] if s.get('date')))
    passages=[m for m in re.finditer(r'[^\n。！？]*[。！？]|[^\n。！？]+',data['notes'])
              if m.group().strip() and len(m.group())<=600]
    parts=[m.group() for m in passages]
    for index,passage in enumerate(parts):
        if grounded and passage.lstrip().startswith('#'):continue
        item={'id':fingerprint([data['origin'],passage]),'text':passage,
              'record_id':data['origin'],'kind':'model_synthesis','record_source_dates':dates,
              'qualifications':[p for p in parts[max(0,index-1):index+2] if p!=passage and QUALIFIER.search(p)]}
        if grounded:
            start,end=passages[index].span()
            cited={s['id']:s for a,b,s in citations if a<end and b>start}
            item.pop('record_source_dates')
            item['qualifications']=[parts[i] for i in (index-1,index+1) if 0<=i<len(parts)
                and QUALIFIER.search(parts[i]) and not parts[i].lstrip().startswith('#')
                and '\n' not in data['notes'][passages[min(i,index)].end():passages[max(i,index)].start()]]
            item['citations']=[{k:s.get(k) for k in ('id','url','title','date')} for s in list(cited.values())[:2]]
        candidates.append((len(query & terms(passage)),item))
    if grounded:
        wants_time=re.search(r'哪天|几点|何时|什么时候|什么时间|日期|日期和时间',question)
        def relevance(row):
            score,item=row
            time_detail=bool(wants_time and re.search(r'\d{1,2}[:：]\d{2}|\d{1,2}月\d{1,2}日',item['text']))
            return (time_detail,score,item.get('kind')!='source_title')
        ordered=sorted(candidates,key=relevance,reverse=True)
        # Keep room for the search model's answer as well as independently attributable excerpts.
        notes=[v for _,v in ordered if v.get('record_id') and v.get('citations')]
        excerpts=[];seen=set()
        for _,item in ordered:
            if item.get('url') and item['url'] not in seen:
                seen.add(item['url']);excerpts.append(item)
        return excerpts[:5]+notes[:max(3,8-len(excerpts))]
    return [v for _,v in sorted(candidates,key=lambda x:x[0],reverse=True)[:8]]


def saved_failed_research(db,turn):
    if (turn['status']!='interrupted' or turn['error_code'] not in
            ('private_reply_review_rejected','knowledge_study_failed','knowledge_study_unknown','knowledge_study_input_too_large')
            or not enabled(db,turn['persona_id'])):
        return None
    return evidence(db,turn['persona_id'],turn['session_id'],turn['id'],active_turn=turn['id'])


def details(store,pid,sid,tid,kind):
    from .workbench_timeline import require_session
    with store.connection() as db:
        require_session(db,pid,sid)
        turn=db.execute("SELECT * FROM turns WHERE id=? AND persona_id=? AND session_id=? AND channel='desktop'",(tid,pid,sid)).fetchone()
        if not turn:
            raise RuntimeErrorBase('retrieved_notes_unavailable')
        stored=None
        if kind=='research':
            stored=saved_failed_research(db,turn)
            if (not stored and turn['status']=='completed' and enabled(db,pid)
                    and db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_study_reads'").fetchone()
                    and db.execute("SELECT 1 FROM knowledge_study_reads WHERE turn_id=? AND outcome!='hit'",(tid,)).fetchone()):
                stored=evidence(db,pid,sid,tid)
        if stored:
            it=intent(db,tid)
            if not it:
                raise RuntimeErrorBase('retrieved_notes_unavailable')
            if it['origin']=='session':
                from . import session_privacy
                from .config import ConfigStore
                config=ConfigStore(store.paths)
                cfg=config.load()
                grant=session_privacy.current(db,config,cfg,pid,sid,session_privacy.recipients(config,cfg))
                if not grant['enabled'] or grant['revision']!=it['consent_revision']:
                    raise RuntimeErrorBase('retrieved_notes_unavailable')
            return {'notes':stored['notes'],'sources':stored['sources']}
        if not eligible(db,tid):
            raise RuntimeErrorBase('retrieved_notes_unavailable')
        accepted=db.execute("""SELECT a.detail,d.contract_json FROM private_reply_attempts a JOIN private_reply_decisions d ON d.id=a.decision_id
            JOIN private_reply_reviews v ON v.attempt_id=a.id AND v.approved=1 JOIN expression_speech_units u ON u.source_id=a.id
            JOIN expression_speech_publications p ON p.utterance_id=u.id AND p.state='complete'
            WHERE a.turn_id=? AND a.phase='main'""",(tid,)).fetchone()
        if not accepted:raise RuntimeErrorBase('retrieved_notes_unavailable')
        binding=json.loads(accepted['contract_json'])
        proof=binding['ordinary']
        context=proof.get('research') or {}
        for source_id,digest in proof['selected'].items():
            if db.execute("SELECT 1 FROM source_exclusions WHERE event_id=?",(source_id,)).fetchone():
                raise RuntimeErrorBase('retrieved_notes_unavailable')
            memory=db.execute("SELECT text,revision,status FROM memories WHERE id=?",(source_id,)).fetchone()
            if memory:
                if memory['status']!='active' or fingerprint({'id':source_id,'text':memory['text'],'role':'memory',
                                                              'revision':memory['revision']})!=digest:
                    raise RuntimeErrorBase('retrieved_notes_unavailable')
        if context.get('status')=='local_hit':
            document=local_lookup_route.details(db,store,turn,context)
            if kind=='research':return document
        origin=context.get('origin')
        data=evidence(db,pid,sid,origin) if origin and enabled(db,pid) else None
        if origin and (not data or fingerprint(data)!=context.get('record_hash')):
            raise RuntimeErrorBase('retrieved_notes_unavailable')
        if kind=='long' and accepted['detail']:return {'body':accepted['detail']}
        if kind=='research' and data:return {'notes':data['notes'],'sources':data['sources']}
        raise RuntimeErrorBase('retrieved_notes_unavailable')


def _basis(base,material):
    return fingerprint([{k:v for k,v in base.items() if k!='consent'},base['consent']['recipient_hash'],
                        material['current'],material['references']])


def _query_text(material,target=None):
    ids=[target] if target in material['current'] else material['current']
    return '\n'.join(material['catalog'][i]['text'] for i in ids)


def previous_reply(material):
    for key in reversed(material['previous_exchange']):
        item=material['catalog'][key]
        if item['role'] in ('assistant','persona'):
            return item['text'][:300]
    return ''


def _private_origin(db,tid):
    row=intent(db,tid) if tid else None
    return row is not None and row['origin']=='session'


def validate_context(db,turn,base,context):
    tid=turn['id']; it=intent(db,tid)
    if not it or json.loads(it['target_json'])!=context['target'] or context['phase']!=base['phase']:
        raise RuntimeErrorBase('private_lookup_scope_changed')
    for node in ('J4','J5'):
        aid=context.get(node.lower()+'_receipt')
        key='local.J5' if node=='J5' and context.get('status')=='local_hit' else node
        if key=='local.J5' and context.get('lookup_outcome')=='studied':key='study.J5'
        if aid:
            receipt=db.execute("""SELECT node FROM private_reply_nodes WHERE attempt_id=? AND turn_id=?
                AND state='received'""",(aid,tid)).fetchone()
            if not receipt or not (receipt[0]==key or (key in ('local.J5','study.J5')
                    and re.fullmatch(re.escape(key)+r'\.coverage\.(?:(?:correction|compact)\.)?\d+',receipt[0]))):
                raise RuntimeErrorBase('private_lookup_scope_changed')
    if it['choice'] in ('web','retrieve') and not context.get('j4_receipt'):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    if context.get('status')=='local_hit':
        local_lookup_route.validate_hit(db,turn,it,base['consent'],context)
    elif context['facts'] and not context.get('j5_receipt'):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    if context['phase']=='search_notice':
        if it['state']!='ready' or not enabled(db,turn['persona_id']):
            raise RuntimeErrorBase('private_lookup_scope_changed')
        if it['origin']=='session' and (not base['consent']['enabled'] or it['consent_revision']!=base['consent']['revision']):
            raise RuntimeErrorBase('private_lookup_scope_changed')
    origin=context.get('origin')
    if origin:
        if not enabled(db,turn['persona_id']) or _private_origin(db,origin) and not base['consent']['enabled']:
            raise RuntimeErrorBase('private_lookup_scope_changed')
        data=evidence(db,turn['persona_id'],turn['session_id'],origin,active_turn=tid)
        if not data or fingerprint(data)!=context['record_hash']:
            raise RuntimeErrorBase('private_reply_source_changed')


def _candidate_lookup(db,turn,base,question,*,grounded=False):
    return previous(db,turn,question,base['consent']['enabled'],grounded=grounded)


async def _decide_lookup(flow,turn,base,mat):
    tid=turn['id']; rows=mat['catalog']
    initial={'current':[rows[i] for i in mat['current']], 'references':[rows[i] for i in mat['references']],
             'recent':[rows[i] for i in mat['recent']], 'repair':None}
    target=await flow.choice(tid,0,'J1',initial,('wait','batch',*dict.fromkeys(mat['current']+mat['references'])),base,mat)
    if getattr(flow.providers,'local_first',False):
        return await local_lookup_route.decide(flow,turn,base,mat,target)
    targets=mat['current'] if target=='batch' else [target]
    with flow.store.connection() as db:
        allowed=enabled(db,turn['persona_id'])
        grounded=getattr(flow.providers,'grounded_search',False)
        task,origin=projection(db,turn,targets,base['consent']['enabled'],grounded=grounded)
        old=_candidate_lookup(db,turn,base,_query_text(mat,target),grounded=grounded) if allowed else None
        selected_in_composer=any(db.execute("SELECT 1 FROM knowledge_control WHERE key=?",
            ('private.lookup.span.'+i,)).fetchone() for i in mat['current'])
    outcome='none'
    if allowed and target!='wait' and origin!='declined':
        hints=[{k:s.get(k) for k in ('title','date','url')} for s in old['sources'][:8]] if old else []
        state={'current':initial['current'],'target':targets,'available_sources':hints,
            'explicit_request':selected_in_composer or origin=='explicit' or bool(QUERY_REQUEST.search(_query_text(mat,target))),
            'query_available':bool(task)}
        if grounded and old and not state['explicit_request']:
            referent=previous_reply(mat) if refers_back(_query_text(mat,target)) else ''
            offers=ranked(old,_query_text(mat,target)+' '+referent,grounded=True)
            state['available_evidence']=[item['text'][:300] for item in
                ([x for x in offers if x.get('url')][:1]+[x for x in offers if x.get('record_id')][:1])]
        outcome=await flow.choice(tid,0,'J4',state,('web','none'),base,mat)
    state=('ready' if task else 'waiting') if outcome=='web' else 'none'
    with flow.store.connection(write=True) as db:
        _,live,actual=flow._snapshot(db,tid)
        if base!=live or mat!=actual:raise RuntimeErrorBase('private_lookup_scope_changed')
        db.execute("""INSERT INTO private_lookup_intents VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (tid,encoded(target),origin,outcome,_basis(base,mat),old['origin'] if old else None,
                    state,encoded(task) if task else None,None,base['consent']['revision']))


def _tool_scope(flow,db,turn,base):
    tid=turn['id']
    root,actual=flow.entry._binding(db,tid,'main')
    if actual!=base or not enabled(db,turn['persona_id']) or not visible_notice(db,tid):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    it=intent(db,tid)
    if it['state']!='ready' or not it['query_json']:
        raise RuntimeErrorBase('private_lookup_scope_changed')
    choice='web'
    if getattr(flow.providers,'local_first',False):
        read_receipt=local_lookup_route.receipt(db,tid)
        if not read_receipt or local_lookup_route.has_local_material(read_receipt) or read_receipt['outcome'] in ('disabled','not_requested'):
            raise RuntimeErrorBase('private_lookup_scope_changed')
        choice='retrieve'
    if it['choice']!=choice or not db.execute("""SELECT 1 FROM private_reply_nodes WHERE turn_id=? AND draft=0 AND node='J4'
            AND state='received' AND result_json=?""",(tid,encoded(choice))).fetchone():
        raise RuntimeErrorBase('private_lookup_scope_changed')
    from .deepseek_native_contract import official_endpoint,private_research_model
    from .private_personas import guard_cloud_dispatch
    search_model=getattr(flow.providers,'search_model',None)
    model=search_model() if search_model else private_research_model(flow.entry.config)
    official_endpoint(model)
    if it['origin']=='session':
        if not actual['consent']['enabled'] or it['consent_revision']!=actual['consent']['revision']:
            raise RuntimeErrorBase('private_lookup_scope_changed')
    elif not actual['consent']['enabled']:
        guard_cloud_dispatch(flow.store,root['persona_id'],'knowledge_native',model,db=db)
    query=json.loads(it['query_json'])
    if 'confirmed_span' in query:
        original=query.pop('confirmed_span')
        resolved=local_lookup_route.resolved_query(db,root,json.loads(it['target_json']),original,
            {'enabled':False,'revision':0},base['instance'],public_only=True,
            versioned=local_lookup_route.revision_mode(db,tid))
        if resolved!=query['question'] or SENSITIVE_QUERY.search(resolved):
            raise RuntimeErrorBase('private_lookup_scope_changed')
    if getattr(flow.providers,'grounded_search',False):
        from datetime import datetime
        query['as_of']=datetime.fromisoformat(turn['created_at']).astimezone().date().isoformat()
    return query,model


async def _call(flow,turn,base,step,args=None):
    tid=turn['id']
    with flow.store.connection(write=True) as db:
        query,model=_tool_scope(flow,db,turn,base)
        request=args if args is not None else query
        study=step in ('organize','organize_retry')
        if study and callable(getattr(flow.providers,'study_model',None)):
            model=flow.providers.study_model()
        old=db.execute("SELECT * FROM private_lookup_calls WHERE turn_id=? AND step=?",(tid,step)).fetchone()
        if study:
            from .knowledge_study import packet,freeze_input
            from .knowledge_coverage import truncated_response
            frozen=old['attempt_id'] if old else None
            if step=='organize_retry':
                primary=db.execute("SELECT * FROM private_lookup_calls WHERE turn_id=? AND step='organize'",(tid,)).fetchone()
                if (not getattr(flow.providers,'structured_study',False) or not primary or primary['state'] not in ('failed','unknown')
                        or not truncated_response(json.loads(primary['result_json'] or '{}'))):
                    raise RuntimeErrorBase('private_lookup_scope_changed')
                frozen=primary['attempt_id']
            request=packet(flow,db,turn,base,query,model,attempt=frozen)
            if step=='organize_retry' and primary['binding']!=fingerprint([base,model,'organize',request,flow.providers.version]):
                raise RuntimeErrorBase('private_lookup_scope_changed')
        signature=fingerprint([base,model,step,request,flow.providers.version] if study else [base,model,step,request])
        if old:
            if old['binding']!=signature:raise RuntimeErrorBase('private_lookup_scope_changed')
            return old['state'],json.loads(old['result_json']) if old['result_json'] else {}
        if study and not request['sources']:return 'empty',{}
        meter=gate(db,flow.store,'knowledge_study' if study else 'private_lookup',turn_scope(turn))
        aid=identifier()
        db.execute("INSERT INTO private_lookup_calls VALUES(?,?,?,?,'sending',NULL,NULL)",(tid,step,aid,signature))
        if study:
            freeze_input(db,aid,request)
            record(db,tid,'knowledge_studying')
    try:
        if study:
            dispatch=model|{'_study_length_retry':True} if step=='organize_retry' else model
            result,usage=await flow.providers.organize(aid,request,dispatch)
            if not isinstance(result,dict) or not isinstance(result.get('raw'),str):
                raise RuntimeErrorBase('knowledge_study_invalid_result')
        else:
            result,usage=await flow.providers.retrieve(step,aid,request,model)
            if not isinstance(result,dict) or not isinstance(result.get('sources'),list) or not isinstance(result.get('notes',''),str):
                raise RuntimeErrorBase('private_lookup_invalid_result')
        with flow.store.connection(write=True) as db:
            db.execute("UPDATE private_lookup_calls SET state='received',result_json=?,usage_json=? WHERE attempt_id=?",
                       (encoded(result),encoded(usage),aid))
            if study:record(db,tid,'knowledge_study_received')
        return 'received',result
    except (OSError,ValueError,RuntimeErrorBase) as error:
        diagnostics=getattr(error,'diagnostics',{}) or {}
        native=getattr(error,'native',None)
        state='failed' if diagnostics.get('provider_terminal_error') or (native or {}).get('outcome')=='failed' else 'unknown'
        status=diagnostics.get('http_status')
        if not study and diagnostics.get('read_only_tool') is True and type(status) is int and 400<=status<600:
            state='failed'
        audit={'code':type(error).__name__,'native':native,'diagnostics':diagnostics}
        if study and truncated_response(audit):
            state='failed'
        from .private_transport_recovery import park
        with flow.store.connection(write=True) as db:
            if park(db,turn,aid,'lookup',signature,meter,error):
                return 'parked',{}
            db.execute("UPDATE private_lookup_calls SET state=?,result_json=?,usage_json=? WHERE attempt_id=?",
                       (state,encoded(audit),encoded(getattr(error,'usage',None)),aid))
        return state,audit if study else {}
    finally:
        with flow.store.connection(write=True) as db:
            db.execute("UPDATE private_lookup_calls SET state='unknown' WHERE attempt_id=? AND state='sending'",(aid,))


async def _acquire(flow,turn,base,it):
    from .deepseek_native_contract import supplement
    tid=turn['id'];query=json.loads(it['query_json'])
    from .semantic_knowledge import active
    with flow.store.connection() as db:vector=active(db,tid)
    state,result=await _call(flow,turn,base,'native')
    if state=='parked':return False
    sources=result.get('sources',[]) if state=='received' else []
    notes=result.get('notes','') if state=='received' else ''
    fetched=False
    if state=='received' and not sources:
        action=supplement(enabled=True,decision='web',outcome='completed',used=0,tool_calls=result.get('tool_calls'),
                          intent='explicit_link' if query.get('url') else 'explicit_search',
                          public_query=query['question'],url=query.get('url'))
        if action:
            fetched=action['tool']=='fetch'
            state,extra=await _call(flow,turn,base,action['tool'],action['input'])
            if state=='parked':return False
            sources=extra.get('sources',[]) if state=='received' else []
            # A tool-free synthesis precedes this independent acquisition.
            notes=extra.get('notes','') if state=='received' else ''
    read_page=query.get('retain_page') or (getattr(flow.providers,'study_enabled',False) and query.get('intent')=='read_link')
    if (state=='received' and not fetched and read_page and query.get('url')
            and not any(s.get('url')==query['url'] and s.get('content') for s in sources if isinstance(s,dict))):
        retention,page=await _call(flow,turn,base,'fetch',{'url':query['url']})
        if retention=='parked':return False
        if retention=='received':sources=[*sources,*page.get('sources',[])]
    elif state=='received' and result.get('sources') and not fetched and not query.get('url') and (
            getattr(flow.providers,'structured_study',False) or vector):
        if not any(isinstance(s,dict) and s.get('content') for s in sources):
            for s in sources:
                if not isinstance(s,dict):continue
                try:target=public_url(s.get('url'))
                except RuntimeErrorBase:continue
                retention,page=await _call(flow,turn,base,'fetch',{'url':target})
                if retention=='parked':return False
                if retention=='received':sources=[*sources,*page.get('sources',[])]
                break
    elif state=='received' and result.get('sources') and not fetched and not query.get('url') and getattr(flow.providers,'study_enabled',False):
        from .knowledge_study import forward_fetch_target,forward_hints,forward_sources_have_names
        hints=forward_hints(query['question'],notes)
        target=forward_fetch_target(query['question'],notes,sources) if hints and not forward_sources_have_names(hints,sources) else None
        retention='received'
        if target:
            retention,page=await _call(flow,turn,base,'fetch',{'url':target})
            if retention=='parked':return False
            pages=page.get('sources',[]) if retention=='received' else []
            sources=[*sources,*pages]
        if hints and retention=='received' and not forward_sources_have_names(hints,sources):
            supplement_state,supplemented=await _call(flow,turn,base,'search',{'query':query['question'][:128]})
            if supplement_state=='parked':return False
            if supplement_state=='received':sources=[*sources,*supplemented.get('sources',[])]
    verified={}
    for source in sources:
        if not isinstance(source,dict):continue
        try:url=public_url(source.get('url'))
        except RuntimeErrorBase:continue
        row={k:source.get(k) if isinstance(source.get(k),str) else None
             for k in ('content','summary','date','qualification','kind')}
        row.update(url=url,title=source['title'] if isinstance(source.get('title'),str) and source['title'] else url)
        row['id']=fingerprint([turn['persona_id'],turn['session_id'],url,row['content'],row['summary']])
        verified[row['id'] if vector or getattr(flow.providers,'structured_study',False) else url]=row
    outcome='acquired' if verified else 'unconfirmed' if state in ('sending','unknown') else 'unavailable'
    with flow.store.connection(write=True) as db:
        _tool_scope(flow,db,turn,base)
        db.execute("INSERT OR IGNORE INTO private_lookup_records VALUES(?,?,?,?,?,?,?,?)",
                   (tid,turn['persona_id'],turn['session_id'],outcome,notes,encoded(list(verified.values())),
                    None if verified else it['cached_turn'],timestamp()))
        record(db,tid,'knowledge_ready' if verified else 'knowledge_unavailable')
    return True


async def prepare(flow,turn,base,material):
    from .semantic_knowledge import active
    tid=turn['id']
    with flow.store.connection() as db:it=intent(db,tid)
    if not it:
        await _decide_lookup(flow,turn,base,material)
    with flow.store.connection(write=True) as db:
        it=intent(db,tid)
        vector=active(db,tid)
        version=db.execute('SELECT value FROM knowledge_control WHERE key=?',('private.study.version.'+tid,)).fetchone()
        expected=flow.providers.version if getattr(flow.providers,'study_enabled',False) else None
        if (json.loads(version[0]) if version else None)!=expected:
            raise RuntimeErrorBase('private_chat_review_version_changed')
        if it['binding']!=_basis(base,material):raise RuntimeErrorBase('private_lookup_scope_changed')
        target=json.loads(it['target_json'])
        full=base['consent']['enabled']
        if it['state']=='waiting' and full:
            task,origin=projection(db,turn,material['current'] if target=='batch' else [target],True,
                                   grounded=getattr(flow.providers,'grounded_search',False))
            db.execute("UPDATE private_lookup_intents SET state=?,origin=?,query_json=?,consent_revision=? WHERE turn_id=?",
                       ('ready' if task else 'skipped',origin,encoded(task) if task else None,base['consent']['revision'],tid))
            it=intent(db,tid)
        if it['state']=='waiting':
            changed=db.execute("""UPDATE turns SET status='interrupted',error_code='private_lookup_confirmation_required'
                WHERE id=? AND coalesce(error_code,'')!='private_lookup_confirmation_required'""",(tid,)).rowcount
            if changed:record(db,tid,'knowledge_waiting_scope')
            return None
        addressed=[target] if target in material['current'] else material['current']
        j4=db.execute("SELECT attempt_id FROM private_reply_nodes WHERE turn_id=? AND node='J4' AND state='received'",(tid,)).fetchone()
        context={'target':target,'phase':'main','status':'ordinary','facts':[],'j4_receipt':j4[0] if j4 else None,
                 'explicit_long':explicit_long(db,[material['catalog'][i] for i in addressed])}
        if getattr(flow.providers,'local_first',False):
            read_receipt=local_lookup_route.receipt(db,tid)
            if not read_receipt:raise RuntimeErrorBase('private_lookup_scope_changed')
            if local_lookup_route.has_local_material(read_receipt):
                facts=local_lookup_route.current_facts(db,turn,it,base['consent'],read_receipt)
                return context|{'status':'local_hit','facts':facts,'j5_receipt':read_receipt['selection_id']}
            if it['choice']=='retrieve' and it['state'] in ('none','skipped'):
                return context|{'status':'insufficient' if it['state']=='none' else 'skipped'}
        paper=db.execute("SELECT outcome FROM private_lookup_records WHERE turn_id=?",(tid,)).fetchone()
        if it['state']=='ready' and not paper:
            if not enabled(db,turn['persona_id']):raise RuntimeErrorBase('knowledge_permission_changed')
            pub=notice(db,tid)
            if not pub:return context|{'phase':'search_notice','status':'before_search','explicit_long':False}
            if not visible_notice(db,tid):
                if pub['state'] in ('closed','unknown'):raise RuntimeErrorBase('search_notice_not_fully_presented')
                return None
    if it['state']=='ready' and not paper and not await _acquire(flow,turn,base,it):
        return None
    if (getattr(flow.providers,'study_enabled',False) or vector) and it['state']=='ready':
        from .knowledge_study import finish
        try:
            return await finish(flow,turn,base,material,context)
        except sqlite3.DatabaseError as exc:
            from .local_knowledge import storage_error
            raise storage_error(exc) from exc
    with flow.store.connection(write=True) as db:
        paper=db.execute("SELECT outcome FROM private_lookup_records WHERE turn_id=?",(tid,)).fetchone()
        origin=tid if paper and paper['outcome']=='acquired' else it['cached_turn']
        data=evidence(db,turn['persona_id'],turn['session_id'],origin,active_turn=tid) if enabled(db,turn['persona_id']) else None
        if _private_origin(db,origin) and not full:data=None
        if not paper and data and target!='wait':
            outcome='skipped' if it['state']=='skipped' else 'reused'
            db.execute("INSERT OR IGNORE INTO private_lookup_records VALUES(?,?,?,?,?,?,?,?)",
                       (tid,turn['persona_id'],turn['session_id'],outcome,'','[]',origin,timestamp()))
    context['status']=paper['outcome'] if paper else 'skipped' if it['state']=='skipped' else 'reused' if data else 'ordinary'
    if getattr(flow.providers,'grounded_search',False):
        context['lookup_outcome']=context['status']
    if context['status']=='acquired' and not data:context['status']='unavailable'
    task=json.loads(it['query_json'] or '{}')
    if it['choice'] in ('web','retrieve') and task.get('url'):
        matching=[s for s in (data or {}).get('sources',[]) if s['url']==task['url']]
        context['requested_page']={'url':task['url'],'found':bool(matching),
                                   'readable':any(s.get('content') for s in matching)}
    if not data or target=='wait':return context
    return await select_facts(flow,turn,base,material,data,context,0,None)


async def select_facts(flow,turn,base,material,data,context,draft,reason):
    tid=turn['id'];target=context['target']
    context=context|{'facts':[]}
    grounded=getattr(flow.providers,'grounded_search',False)
    citations=[]
    if grounded:
        with flow.store.connection() as db:citations=research_citations(db,data)
    question=_query_text(material,target)
    state={'target':target,'question':question,'repair':reason}
    referent=previous_reply(material) if grounded and context['status']=='reused' and refers_back(question) else ''
    if referent:
        state['previous_reply']=referent
    candidates=ranked(data,question+(' '+referent if referent else ''),grounded=grounded,citations=citations)
    if candidates:
        groups={f"fact/{i}":[item] for i,item in enumerate(candidates)}
        if len(candidates)>1:groups['first_pair']=candidates[:2]
        groups['insufficient']=[]
        chosen=await flow.choice(tid,draft,'J5',state|{'options':groups},
                                 tuple(groups),base,material)
        context['facts']=groups[chosen]
        with flow.store.connection() as db:
            context['j5_receipt']=db.execute("SELECT attempt_id FROM private_reply_nodes WHERE turn_id=? AND node='J5' AND draft=?",(tid,draft)).fetchone()[0]
    if not context['facts'] and context['status'] in ('acquired','reused'):context['status']='insufficient'
    return context|{'origin':data['origin'],'record_hash':fingerprint(data)}
