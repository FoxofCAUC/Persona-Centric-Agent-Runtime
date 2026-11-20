"""Learn from scoped web excerpts, commit validated revisions, then read them back."""
from datetime import datetime, timedelta
import json
import re

from . import local_knowledge as library, local_lookup_route as route
from . import knowledge_catalog as catalog
from . import knowledge_coverage as coverage
from . import semantic_knowledge as semantic
from .archive import encoded, fingerprint
from .models import RuntimeErrorBase, timestamp
from .progress import record

VERSION = 'source-facts-v1'
_BOUNDARY=re.compile(r'[。！？!?；;\n]|\s*\.{3,}\s*|(?<=[a-zA-Z])\.(?=\s+[A-Z])')
_LIMIT=re.compile(r'仅限|截至|暂定|预计|传闻|爆料|据称|推测|尚未|并非|不再|可能')
_EVENT_DATE=re.compile(r'(?<!\d)\d{4}(?:年\d{1,2}月\d{1,2}日|[-/]\d{1,2}[-/]\d{1,2}(?!\d))')
_EVENT_ACTION=re.compile(r'发射|起飞|发布|发行|上映|开播|首映|建成|成立|出生')
_NON_EVENT=re.compile(r'计划|瞄准|预定|将于|拟于|预计|预测|如果|尚未|未能|取消|原定|并未|并非|可能|传闻|否认|推迟')


def _dated_excerpt(source,subject,action,day):
    body=source.get('content') or source.get('summary') or ''
    # Match the event in the same clause as the literal date. Publication metadata is not event evidence.
    actor=re.escape(subject)+(r'(?![a-zA-Z0-9_-])' if subject[-1:].isascii() else '')
    clock=r'(?:\s*\d{1,2}\s*(?:时|点|:)\s*\d{1,2}\s*分?)?'
    lead=clock+r'[\s，,]*(?:搭载|由)?\s*'+actor
    carrier=r'(?:载人飞船|飞船|航天器)?(?:的[^。，,；;！？\n]{0,36}?(?:火箭|运载器))?'
    location=r'(?:(?:在|从)[^。，,；;！？\n]{1,24}?(?:中心|场|基地|站|机场))?'
    tail=carrier+location+r'(?:正式|成功|点火|首次|顺利)*'+re.escape(action)
    for span in re.finditer(r'[^。；;！？!?\n…]+',body):
        sentence=span[0]
        if len(sentence)>600 or '...' in sentence:continue
        dates=list(_EVENT_DATE.finditer(sentence))
        if len(dates)!=1:continue
        date=dates[0]
        parsed=library._calendar_value(date[0])
        if not parsed or parsed[1:]!=day:continue
        before,after=sentence[:date.start()],sentence[date.end():]
        date_first=re.match(lead+tail,after)
        subject_first=re.search(actor+carrier+r'(?:于|在)\s*$',before)
        match=date_first or (subject_first and re.match(clock+r'\s*(?:正式|成功)?'+action,after))
        if match:
            event=before+date[0]+match[0]+re.split(r'[，,]',after[match.end():],1)[0]
            if _NON_EVENT.search(event):continue
            yield parsed[0],date[0],sentence,span.start()


def _complete_date(db,turn,base,ident,*,active_turn=None):
    """Store an independently quoted date from the same scoped acquisition; retain the original fact."""
    from .private_lookup import evidence,intent
    row=db.execute('SELECT spec_json FROM local_knowledge_facts WHERE id=? AND withdrawn=0',(ident,)).fetchone()
    if not row:return ident
    spec=json.loads(row[0])
    parsed=library._calendar_value(spec['value'])
    action=_EVENT_ACTION.search(spec['predicate'])
    if (not parsed or parsed[0] is not None or not action or not spec['turn_source']
            or not re.search('日期|时间',spec['predicate']) or spec['state']!='fact'
            or any(spec[k] for k in ('conditions','unit','valid_from','valid_until'))
            or _NON_EVENT.search(spec['quote'])):return ident
    key='private.study.date.'+ident
    prior=db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()
    if prior:return json.loads(prior[0])
    origin=spec['turn_source'][0]
    pid,sid=turn['persona_id'],turn['session_id']
    data=evidence(db,pid,sid,origin,active_turn=active_turn)
    if not data:return ident
    query=json.loads(intent(db,origin)['query_json'])
    matches=[]
    for source in data['sources']:
        if query.get('url') and source['url']!=query['url']:continue
        if source.get('kind') not in ('page_content','search_content','native_search_summary','search_summary'):continue
        if _NON_EVENT.search(source['title']):continue
        for year,date,quote,start in _dated_excerpt(source,spec['subject'],action[0],parsed[1:]):
            matches.append((year,date,quote,start,source['id']))
    if len({match[0] for match in matches})!=1:return ident
    _,date,quote,start,source_id=matches[0]
    attributes=spec['attributes']
    if action[0] in ('发射','起飞'):
        clocks=[]
        for _,day,passage,offset,source_key in matches:
            clock=re.search(re.escape(day)+r'\s*(\d{1,2})\s*(?:时|点|:)\s*(\d{1,2})\s*分?',passage)
            if clock:
                clocks.append((tuple(map(int,clock.groups())),clock[0],passage,offset,source_key))
        if len({entry[0] for entry in clocks})==1:
            _,date,quote,start,source_id=clocks[0]
            attributes=[*attributes,'几点起飞','几点发射','几点升空']
    full=library.admit_turn_source(db,pid,sid,origin,source_id,instance=base['instance'],consent=base['consent'],
        versioned=base.get('versioned_knowledge',False) or route.revision_mode(db,turn['id']),
        active_turn=active_turn,study_receipt=True,subject=spec['subject'],predicate=spec['predicate'],value=date,
        quote=quote,start=start,end=start+len(quote),attributes=attributes,expires_at=spec['expires_at'])
    db.execute('INSERT INTO knowledge_control VALUES(?,?)',(key,encoded(full)))
    return full


def preferred_dates(db,turn,base,question,facts,*,learn=False):
    """Upgrade new selections only; displayed or pending selections retain their original IDs."""
    selected={}
    for fact in facts:
        current=fact
        day=library._calendar_value(fact['value'])
        if day and day[0] is None:
            if learn:
                full=_complete_date(db,turn,base,fact['id'])
            else:
                row=db.execute('SELECT value FROM knowledge_control WHERE key=?',('private.study.date.'+fact['id'],)).fetchone()
                full=json.loads(row[0]) if row else fact['id']
            if full!=fact['id']:
                verified=_bound_facts(db,turn,base,question,[full])
                if verified:current=verified[0]
        selected[current['id']]=current
    return list(selected.values())


def _commit(db,tid):
    return db.execute('SELECT * FROM knowledge_study_commits WHERE turn_id=?',(tid,)).fetchone()


def _bound_facts(db,turn,base,question,ids,*,after_commit=None):
    return _bound_read(db,turn,base,question,ids,after_commit=after_commit)[1]


def preview_provenance(condition):
    return bool(re.search('前瞻|预告|公告|节目',condition) and re.search('释出|透露|披露|提及|公布',condition)
                and not re.search('仅|适用|平台|地区|服|渠道|上线',condition))


def _bound_read(db,turn,base,question,ids,*,after_commit=None,for_selection=False):
    """Check each indexed revision independently before J5 can judge compound question coverage."""
    text=library._norm(question)
    historical=library._AS_OF.search(text)
    urls={library.public_url(v) for v in re.findall(r'https?://[^\s，。；！？）)]+',question)}
    found=[]
    versioned=base['versioned_knowledge'] if 'versioned_knowledge' in base else route.revision_mode(db,turn['id'])
    seen=set()
    outline=catalog.is_overview(question)
    conflict=False
    for ident in ids:
        row=db.execute('SELECT spec_json FROM local_knowledge_facts WHERE id=? AND withdrawn=0',(ident,)).fetchone()
        if not row:continue
        spec=json.loads(row[0])
        def applicable(condition):
            normalized=library._norm(condition)
            if normalized in text:return True
            if spec['state']=='rumor' and normalized in {
                    'likely','probably','possibly','reportedly','unconfirmed','预计','可能','据称','推测','暂未确认','尚未确认'}:
                # Certainty travels with the evidence; it does not constrain the user's query.
                return True
            if spec['state']=='rumor' and re.fullmatch(
                    r'(?:据|按|基于)?(?:玩家|社区|媒体|网友)(?:们)?(?:猜测|推测|认为|推断|猜想|传闻)'
                    r'|(?:according to |based on )(?:community|player|media) (?:reports?|speculation|rumou?rs?)',normalized):
                return True
            if spec['state'] in ('rumor','announced') and preview_provenance(normalized):
                return True
            if versioned:
                if library.revisions.match_condition(text,condition) is not None:return True
                dimension=re.match(r'^(平台|渠道|地区|区服|服务器)\s*[:是为]',normalized)
                if dimension:
                    name=dimension[1]
                    return bool(re.search(r'(?:哪个|哪些|什么)'+name+'|'+name+'(?:是什么|有哪些)',text))
            label=re.match(r'^(主题|版本|平台|地区)(?:为|是|[:：])',normalized)
            return bool(label and label[1] in text)
        conditions=library.revisions.split_conditions(spec['conditions'])[1] if versioned else spec['conditions']
        typed=for_selection and 'witnesses' in spec
        if not typed and not all(applicable(c) for c in conditions):continue
        canonical=' '.join([spec['subject'],spec['predicate'],*spec['conditions']])
        if historical:canonical=historical.group()+' '+canonical
        if '最新' in text:canonical+=' 最新'
        if re.search('爆料|传闻',text) or (spec['state']=='rumor' and re.search('即将|快要|下[个一]|前瞻|预告',text)):
            canonical+=' 传闻'
        outcome,items=library.search(db,turn['persona_id'],turn['session_id'],base['consent'],canonical,
                                     instance=base['instance'],after_commit=after_commit,fact_id=ident,
                                     versioned=versioned,intent_question=question,for_selection=typed)
        if outcome=='hit':
            for f in items:
                key=f.get('revision_id',f['id'])
                if f['id']==ident and (versioned or not urls or f['url'] in urls) and key not in seen:
                    found.append(f);seen.add(key)
        elif outcome=='conflict' and outline and catalog.entry(spec):
            conflict=True
    if conflict:return 'conflict',[]
    return ('hit' if found else 'insufficient'),found


def asks_date_and_place(question):
    wants_date=re.search(r'哪天|哪一天|哪一年|哪年|何时|什么时间|什么时候|日期',question)
    wants_place=re.search(r'哪个.{0,5}(?:场|站|中心)|哪里|地点|何处',question)
    return bool(wants_date and wants_place)


def forward_question(question):
    return bool(re.search(r'快要|即将|下个版本|下一个版本|下次更新|下一次更新',question))


def forward_hints(question,notes):
    if not forward_question(question):return set()
    hints=set()
    for line in notes[:3600].splitlines():
        if not re.search(r'即将|下[个一]|下次|后续|未来|预告|前瞻',line):continue
        for phrase in re.findall(r'\*\*([^*\n]{2,42})\*\*',line):
            hints.update(word for word in re.findall(r'[\u3400-\u9fff]{2,12}|[A-Za-z][\w-]{2,16}',phrase)
                         if word not in question and not re.search(r'版本|预告|前瞻|干员|(?:已|上|登|亮)场|上线|根据|分析|信息|总结|媒体|社区',word))
    return hints


def forward_fetch_target(question,notes,sources):
    hints=forward_hints(question,notes)
    candidates=[]
    for source in sources:
        if not isinstance(source,dict) or source.get('content'):continue
        title=source.get('title') or ''
        summary=source.get('summary') or ''
        if not isinstance(title,str) or not isinstance(summary,str):continue
        missing={word for word in hints if word in title and word not in summary}
        if missing:
            try:url=library.public_url(source.get('url'))
            except RuntimeErrorBase:continue
            candidates.append((sum(map(len,missing)),url))
    return max(candidates,default=(0,None))[1]


def forward_sources_have_names(hints,sources):
    for source in sources:
        if not isinstance(source,dict):continue
        body=source.get('content') or source.get('summary') or ''
        if not isinstance(body,str):continue
        for clause in _BOUNDARY.split(body):
            if (len(clause)<=1200 and any(word in clause for word in hints)
                    and re.search(r'未来|预计|预告|即将|来袭|将(?:会|于|在|登场|加入)|前瞻|upcoming|expected|forthcoming',clause,re.I)):
                return True
    return False


def selection_groups(facts,question):
    additional=[]
    people=[]
    if forward_question(question):
        additional=facts
        facts=[f for f in facts if f['kind']=='rumor' or re.search(
            r'即将|将(?:于|会|在|登场|上线|公布|加入)|下(?:一|个|次)版本|后续|未来|前瞻(?:主题|直播|特别节目)|预计|今後|upcoming|will (?:join|appear|be released)',
            f['text']+' '+f['predicate'],re.I) or f['kind']=='announced' and '早期情报' in f['predicate']]
        if re.search('谁|哪位',question) and re.search('干员|角色|成员|人物|选手',question):
            people=[f for f in facts if re.search('角色|干员|成员|人选|身份|所属|出身|operator|character',f['predicate'],re.I)
                    or (library._norm(f['subject'])==library._norm(f['value']) and re.search('登场|亮相|加入|上线',f['predicate']))]
            if people:facts=people+[f for f in facts if f not in people]
    if catalog.is_overview(question):
        groups=catalog.outline_groups(facts)
        if groups:return groups|{'insufficient':[]}
    if asks_date_and_place(question):
        dates=[f for f in facts if re.search('日期|时间|时刻|哪天',f['predicate']) and re.search(r'\d{1,2}[-月/]\d{1,2}',f['value'])]
        places=[f for f in facts if re.search('场|站|地点|地址|位置',f['predicate'])]
        pairs=[(date,place) for date in dates for place in places
               if library._norm(date['subject'])==library._norm(place['subject'])]
        if pairs:
            date,place=max(pairs,key=lambda p:(p[0]['kind']!='announced' and not re.search('瞄准|计划|拟于',p[0]['text']),
                                              bool(re.search(r'\d{4}',p[0]['value'])),
                                              bool(re.search(r'\d{1,2}(?:时|点|:)\d{1,2}',p[0]['value']))))
            return {'first_pair':[date,place],'insufficient':[]}
        return {'insufficient':[]}
    options={f'fact/{i}':[f] for i,f in enumerate(people or facts)}
    if len(facts)>=2:options['first_pair']=facts[:2]
    if additional:
        def names(item):
            return {library._norm(name) for name in re.split(r'[()（）]',item['subject']) if len(name.strip())>=2}
        pair=next(([future,identity] for future in facts for identity in additional if identity not in facts
                   and names(future)&names(identity)
                   and re.search('阵营|身份|所属|归属|affiliation|identity',identity['predicate'],re.I)),None)
        if pair:
            if re.search(r'谁|哪位',question) and re.search(r'来自|属于|所属',question):
                return {'first_pair':pair,'insufficient':[]}
            options['first_pair']=pair
        elif people:
            timing=next((f for f in facts if f not in people and re.search('时间|日期|time|date',f['predicate'],re.I)),None)
            if timing:options['first_pair']=[people[0],timing]
    return options|{'insufficient':[]}


def previous_answer(db,turn,base,question,outcome):
    if outcome not in ('miss','insufficient'):return []
    row=db.execute("""SELECT r.fact_ids,r.turn_id FROM knowledge_study_reads r JOIN turns t ON t.id=r.turn_id
        WHERE t.persona_id=? AND t.session_id=? AND t.cancelled_at IS NULL AND r.query_bound=1
        AND r.outcome='hit' AND r.query_hash=? AND t.rowid<(SELECT rowid FROM turns WHERE id=?)
        ORDER BY t.rowid DESC LIMIT 1""",(turn['persona_id'],turn['session_id'],fingerprint(question),turn['id'])).fetchone()
    if not row:return []
    ids=json.loads(row[0])
    facts=_bound_facts(db,turn,base,question,ids)
    if [f['id'] for f in facts]!=ids:return []
    if catalog.is_overview(question):
        committed=_commit(db,row['turn_id'])
        status,candidates=_bound_read(db,turn,base,question,json.loads(committed['fact_ids']))
        if status=='conflict':return []
        if catalog.outline_groups(candidates):return candidates
    return facts


def related_facts(db,turn,base,question,outcome):
    """Offer scoped revisions for a named topic revisit; only J5 may accept this partial match."""
    if outcome not in ('miss','insufficient') or not re.search(r'说回|回到|刚才|前面|之前|方才',question):
        return []
    text=library._norm(question)
    rows=db.execute("""SELECT c.turn_id,c.fact_ids,i.query_json FROM knowledge_study_commits c
        JOIN turns t ON t.id=c.turn_id JOIN private_lookup_intents i ON i.turn_id=t.id
        WHERE c.status='committed' AND t.session_id=? AND t.persona_id=? AND t.cancelled_at IS NULL
        AND t.rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY t.rowid DESC LIMIT 24""",
        (turn['session_id'],turn['persona_id'],turn['id']))
    best=[]
    best_score=0
    for row in rows:
        ids=json.loads(row['fact_ids'])
        facts=_bound_facts(db,turn,base,question,ids)
        if not facts:continue
        # Named field questions require some evidence for that field, even on a topic match.
        facet=re.search(r'(?:什么|哪种)([\u3400-\u9fff]{2,12}?)(?:有关|相关|呢|呀|嘛|吗|来着|[？?\s]|$)',text)
        if facet:
            facts=[f for f in facts if facet[1] in library._norm(f['predicate']+' '+f['text']+' '+' '.join(f['qualifications']))]
        if not facts:continue
        old_question=json.loads(row['query_json'])['question']
        labels=[f['subject'] for f in facts]
        for title in re.findall(r'《([^《》]{1,64})》',old_question):
            labels.extend([title,*re.split('[:：]',title)])
        score=max((len(s) for s in labels if len(s)>=2 and library._matches(text,[s]) is not None),default=0)
        if score>best_score:
            best_score,best=score,facts
    return best


def superseded_outline_replies(db,tid,history,facts):
    """Exclude a prior answer as evidence when this exact query now has a strict superset of its facts."""
    if not catalog.outline_groups(facts):
        return set()
    current=route.answer_receipt(db,tid)
    selected={fact['id'] for fact in facts}
    excluded=set()
    for origin in history:
        prior=route.answer_receipt(db,origin)
        if not prior or prior['outcome']!='hit' or prior['query_hash']!=current['query_hash']:
            continue
        old=set(json.loads(prior['fact_ids']))
        if old and old<selected:
            reply=db.execute('SELECT reply_event_id FROM turns WHERE id=?',(origin,)).fetchone()[0]
            if reply:excluded.add(reply)
    return excluded


def freeze_input(db,aid,request):
    spans=[(s['id'],s['start'],len(s['text'])) for s in request['sources']]
    db.execute('INSERT INTO knowledge_control VALUES(?,?)',('private.study.input.'+aid,encoded({
        'spans':spans,'hash':fingerprint(request)})))


def packet(flow,db,turn,base,query,model,*,attempt=None):
    from .private_lookup import evidence, intent, research_citations
    from .private_personas import guard_cloud_dispatch
    from .session_privacy import destination, recipients
    from .chat import terms
    from .knowledge_translation import readable_excerpt

    config=flow.entry.config
    if base['consent']['enabled']:
        cfg=config.load()
        account=getattr(flow.providers,'text_flash_account',None) or cfg.get('private_research_account','main')
        if destination(config,model)!=recipients(config,cfg).get(account):
            raise RuntimeErrorBase('private_lookup_scope_changed')
    else:
        guard_cloud_dispatch(flow.store,turn['persona_id'],'knowledge_study',model,db=db)
    tid=turn['id']
    it=intent(db,tid)
    if it['origin']=='session' and (not base['consent']['enabled'] or base['consent']['revision']!=it['consent_revision']):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    question=query['question']
    data=evidence(db,turn['persona_id'],turn['session_id'],tid,active_turn=tid)
    result={'question':question,'as_of':query.get('as_of'),'sources':[]}
    saved=db.execute('SELECT value FROM knowledge_control WHERE key=?',('private.study.input.'+attempt,)).fetchone() if attempt else None
    if saved:
        manifest=json.loads(saved['value'])
        sources={s['id']:s for s in (data or {}).get('sources',[])}
        for key,start,size in manifest['spans']:
            if key not in sources:raise RuntimeErrorBase('private_reply_source_changed')
            s=sources[key]
            raw=s.get('content') or s.get('summary') or ''
            result['sources'].append({'id':key,'start':start,'text':raw[start:start+size],
                'kind':s.get('kind'),'title':s['title'][:256],'url':s['url'],'date':s.get('date')})
        if fingerprint(result)!=manifest['hash']:raise RuntimeErrorBase('private_reply_source_changed')
        return result
    if semantic.active(db,tid):
        budget=model['max_input_chars']-len(getattr(flow.providers,'study_instructions',''))
        for source in (data or {}).get('sources',[]):
            if source.get('kind') not in ('page_content','search_content','native_search_summary','search_summary','user_correction'):
                continue
            if source.get('kind')=='user_correction' and (it['origin']!='session' or not base['consent']['enabled']):
                continue
            if query.get('url') and (source['url']!=query['url'] or not source.get('content')):continue
            raw=source.get('content') or source.get('summary') or ''
            item={'id':source['id'],'start':0,'text':'','kind':source['kind'],'title':source['title'][:256],
                  'url':source['url'],'date':source.get('date')}
            room=budget-len(encoded(result))-len(encoded(item))-2
            if room<=0:continue
            item['text']=raw[:min(1200,room)]
            if item['text'].strip() and len(encoded(result|{'sources':result['sources']+[item]}))<=budget:
                result['sources'].append(item)
            if len(result['sources'])==8:break
        return result
    candidates=[]
    topic=terms(question)
    structured=getattr(flow.providers,'structured_study',False)
    upcoming=set() if structured else forward_hints(question,(data or {}).get('notes',''))
    for source in (data or {}).get('sources',[]):
        kind=source.get('kind')
        if kind not in ('page_content','search_content','native_search_summary','search_summary') and not (
                kind=='user_correction' and route.revision_mode(db,tid) and base['consent']['enabled']
                and it['origin']=='session'):continue
        if query.get('url') and (source['url']!=query['url'] or not source.get('content')):continue
        original=source.get('content') or source.get('summary') or ''
        if not original.strip():continue
        keywords=sorted(w for w in upcoming if w in original)
        excerpt=readable_excerpt(original,question+' '+' '.join(keywords),1200)
        candidates.append((len(topic & terms(source['title']+' '+excerpt)),{
            'id':source['id'],'kind':kind,'title':source['title'][:256],'url':source['url'],'date':source.get('date'),
            'start':original.index(excerpt),'text':excerpt}))
    candidates.sort(key=lambda row:-row[0])
    # Use citation offsets to prioritize the underlying excerpts; the synthesis stays local.
    cited=research_citations(db,data) if data else []
    by_id={item['id']:item for _,item in candidates}
    leading=[by_id[s['id']] for _,_,s in sorted(cited,key=lambda span:span[0]) if s['id'] in by_id]
    if structured:
        # Favor cited URLs and full content. Offer distinct pages before second excerpts from the same page.
        cited_urls={s['url']:i for i,s in enumerate(reversed(leading))}
        ranked=sorted(candidates,key=lambda pair:(-cited_urls.get(pair[1]['url'],-1),
                      pair[1]['kind']!='page_content',-pair[0],pair[1]['id']))
        first={}
        rest=[]
        for _,item in ranked:
            if item['url'] in first:rest.append(item)
            else:first[item['url']]=item
        ordered=[*first.values(),*rest][:8]
    else:
        recent=[]
        timely=re.search('最近|即将|下个|下一|最新|快要',question)
        if timely:
            recent=sorted((s for s in by_id.values() if isinstance(s.get('date'),str)),key=lambda s:s['date'],reverse=True)
        def distinct(items):
            by_source={}
            for item in items:by_source.setdefault(item['id'],item)
            return list(by_source.values())
        # Search summaries often name an entity missing from the user's question. Use those names to
        # locate original excerpts in the query language, never as evidence themselves.
        hints={}
        for line in (data or {}).get('notes','')[:1600].splitlines():
            score=len(topic & terms(line))
            if timely and re.search('即将|下[个一]|后续|预告',line):score+=3
            for emphasis in re.findall(r'\*\*([^*\n]{2,75})\*\*',line):
                for term in re.findall(r'[\u3400-\u9fff]{2,16}|[A-Za-z][\w-]{2,20}',emphasis):
                    if term not in question:
                        hints[term]=max(hints.get(term,0),score)
        def focus(item):
            labels=item['title']+' '+item['text']
            return max((v for k,v in hints.items() if k in labels),default=0)
        focused=sorted((item for _,item in candidates if focus(item) or any(w in item['text'] for w in upcoming)),key=lambda s:(
            not forward_sources_have_names(upcoming,[{'summary':s['text']}]),
            -sum(len(w) for w in upcoming if w in s['text']),-focus(s),
            not bool(timely and re.search('即将|预告|前瞻|下版本|下个版本|upcoming',s['text'],re.I))))
        ordered=distinct(focused[:3]+distinct(leading)[:3]+recent[:2]+[item for _,item in candidates])
    budget=model['max_input_chars']-len(getattr(flow.providers,'study_instructions',''))
    for index,item in enumerate(ordered):
        if len(result['sources'])==8:break
        remaining=budget-len(encoded(result))-len(encoded(item|{'text':''}))-2
        if structured and len(ordered)>index+1:
            overhead=sum(len(encoded(other|{'text':''}))+2 for other in ordered[index+1:])
            # Share room amongst the remaining sources when their metadata fits.
            if remaining>overhead:remaining=(remaining-overhead)//(len(ordered)-index)
        if remaining<=0:continue
        piece=item|{'text':item['text'][:remaining]}
        if piece['text'].strip() and len(encoded(result|{'sources':result['sources']+[piece]}))<=budget:
            result['sources'].append(piece)
    if candidates and not result['sources']:
        raise RuntimeErrorBase('knowledge_study_input_too_large')
    return result


def _unique(items):
    result=dict(items)
    if len(result)!=len(items):raise ValueError('duplicate field')
    return result


def _facts(raw):
    try:
        data=json.loads(raw,object_pairs_hook=_unique)
        if not isinstance(data,dict) or set(data)!={'facts'} or not isinstance(data['facts'],list) or len(data['facts'])>8:
            raise ValueError()
        return data['facts']
    except (ValueError,TypeError,RecursionError) as exc:
        raise RuntimeErrorBase('knowledge_study_invalid_result') from exc


def _admit(db,turn,base,request,candidate,verified_at):
    from .knowledge_store import SOURCE_POLICY
    fields={'source_id','subject','predicate','value','quote','state'}
    optional={'aliases','attributes','conditions','unit','valid_from','valid_until'}
    if not isinstance(candidate,dict) or not fields<=candidate.keys() or candidate.keys()-fields-optional:
        raise RuntimeErrorBase('knowledge_study_invalid_fact')
    source=next((s for s in request['sources'] if s['id']==candidate['source_id']),None)
    quote=library._text(candidate['quote'],1200)
    if not source:raise RuntimeErrorBase('knowledge_study_source_not_offered')
    if source['text'].count(quote)!=1:raise RuntimeErrorBase('knowledge_study_quote_not_unique')
    begin=source['text'].index(quote)
    prefix=_BOUNDARY.split(source['text'][:begin])[-1]
    suffix='' if _BOUNDARY.fullmatch(quote[-1]) else _BOUNDARY.split(source['text'][begin+len(quote):])[0]
    if _LIMIT.search(prefix) or _LIMIT.search(suffix):
        raise RuntimeErrorBase('knowledge_study_omitted_qualifier')
    if re.search(r'传闻|爆料|据称|推测',source['title']+' '+quote) and candidate['state']!='rumor':
        raise RuntimeErrorBase('local_knowledge_evidence_mismatch')
    value=library._text(candidate['value'],600)
    subject=library._text(candidate['subject'],128)
    if (candidate['state']=='rumor' and library._norm(value)==library._norm(subject)
            and library._norm(value) not in library._norm(quote)):
        # Some lists have a separate literal announcement for the named entry.
        mentions=[m for m in re.finditer(re.escape(value)+r'\s*(?:即将|预计|可能)(?:上线|登场|加入)',source['text'])]
        if len(mentions)==1:
            quote=mentions[0][0]
            begin=mentions[0].start()
            prefix=_BOUNDARY.split(source['text'][:begin])[-1]
            suffix=_BOUNDARY.split(source['text'][begin+len(quote):])[0]
            if _LIMIT.search(prefix) or _LIMIT.search(suffix):
                raise RuntimeErrorBase('knowledge_study_omitted_qualifier')
    if re.search(r'(?:并非|不是|不再是|尚未达到)\s*'+re.escape(value),prefix+quote):
        raise RuntimeErrorBase('local_knowledge_evidence_mismatch')
    predicate=library._text(candidate['predicate'],128)
    scope=library._norm(source['title']+' '+quote)
    conditions=library._names(candidate.get('conditions',[]))
    if library._norm(subject) not in scope or any(library._norm(c) not in scope for c in conditions):
        extended=prefix+quote+suffix
        if len(extended)<=1200:
            quote=extended
            begin-=len(prefix)
            scope=library._norm(source['title']+' '+quote)
    names=library._names(candidate.get('aliases',[]))
    paired=re.fullmatch(r'([^()（）]{1,64})[（(]([^()（）]{1,64})[)）]',subject)
    if paired and library._norm(subject) not in scope:
        grounded=[name.strip() for name in paired.groups() if library._norm(name) in scope]
        if grounded:
            subject=grounded[0]
            names.extend(grounded[1:])
    if library._norm(subject) not in scope:
        # An explicitly named work may appear under its distinctive subtitle in a source.
        named=re.findall(r'《([^《》]+)》',request['question'])
        clean=subject.strip('《》')
        if len(named)==1 and library._norm(clean)==library._norm(named[0]):
            parts=re.split(r'[:：]',clean)
            grounded=next((p.strip() for p in reversed(parts) if len(p.strip())>=3
                           and library._matches(library._norm(source['title']),[p.strip()]) is not None),None)
            if grounded:
                subject=grounded
                names.append(clean)
            elif re.match(r'^\s*(?:在先前|此前|其中|本次|官方)',quote):
                previous=source['text'][:begin]
                adjacent=re.search(r'[^。！？!?；;\n]+[。！？!?]\s*$',previous)
                if (adjacent and '...' not in adjacent[0] and '…' not in adjacent[0]
                        and library._norm(clean) in library._norm(adjacent[0]) and len(adjacent[0])+len(quote)<=1200):
                    begin=adjacent.start()
                    quote=adjacent[0]+quote
                    scope=library._norm(source['title']+' '+quote)
    event=re.fullmatch(r'(.{2,48}?)\s*(\d+(?:\.\d+)+)\s*(揭幕前瞻(?:特别节目)?|前瞻特别节目|前瞻)',subject)
    event_grounded=False
    if event:
        work,version,occasion=event.groups()
        normalize=lambda v:re.sub(r'[\s《》·&＆]+','',library._norm(v))
        event_grounded=(library._norm(work) in scope and
                        any(re.search(r'(?<![\d.])'+re.escape(normalize(version+occasion)),normalize(part))
                            for part in (source['title'],quote)))
    if not event_grounded:
        labeled=re.fullmatch(r'(\d+(?:\.\d+)+)\s*(剧情|前瞻|活动|版本)',subject)
        if labeled:
            event_grounded=bool(re.search(r'(?<![\d.])'+re.escape(labeled[1])+r'\s*(?:的\s*)?'+labeled[2],quote))
    if library._norm(subject) not in scope and not event_grounded:
        raise RuntimeErrorBase('knowledge_study_ungrounded_subject')
    aliases=[name for name in names if library._norm(name) in scope]
    context=scope+' '+library._norm(request['question'])
    attributes=[name for name in library._names(candidate.get('attributes',[])) if library._norm(name) in context]
    if (re.search(r'发射|升空|起飞',predicate) and re.search(r'时间|时刻|时分',predicate)
            and candidate['state'] in ('fact','observed') and not _NON_EVENT.search(quote)
            and re.search(r'\d{1,2}\s*(?:时|点|[:：])\s*\d{1,2}',value)):
        attributes.extend(('几点起飞','几点发射','几点升空'))
    if re.search('日期|时间',predicate):
        for action in ('正式发布','发布','发行','起飞','发射','上映','开播'):
            if action in request['question'] and (action in predicate or action.removeprefix('正式') in predicate):
                when=r'(?:哪一天|哪天|什么时候|何时|几点)'
                phrase=re.search(when+r'(?:正式)?'+action+'|'+action+r'(?:是|在|于)?'+when,request['question'])
                if phrase:attributes.append(phrase.group())
    # The predicate is an index label; the value and quote are literal evidence.
    for anchor in re.findall(r'[A-Za-z][A-Za-z0-9_+-]*(?:[ ]+[a-zA-Z][a-zA-Z0-9_-]*){0,2}\s+\d+(?:\.\d+)+|[\u3400-\u9fff]{1,8}?[零〇一二三四五六七八九十百千\d]+号',request['question']):
        if len(anchor)>=3 and library._matches(library._norm(subject),[anchor]) is not None:
            aliases.append(subject)
            subject=anchor
            break
    grounded_conditions=[]
    for condition in conditions:
        if library._norm(condition) in scope:
            grounded_conditions.append(condition)
            continue
        versions=re.findall(r'\d+(?:\.\d+)+',condition)
        if not (candidate['state'] in ('rumor','announced') and preview_provenance(condition)
                and versions and all(re.search(r'(?<![\d.])'+re.escape(v)+r'(?![\d.])',quote) for v in versions)
                and re.search('前瞻|预告|公告|节目',quote) and re.search('释出|透露|披露|提及|公布',quote)):
            raise RuntimeErrorBase('local_knowledge_evidence_mismatch')
    for key in ('valid_from','valid_until'):
        date=candidate.get(key)
        if date is not None and (not isinstance(date,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date) or date not in scope):
            raise RuntimeErrorBase('local_knowledge_invalid_time')
    start=source['start']+begin
    options={key:candidate[key] for key in optional if key in candidate}
    if 'conditions' in options:options['conditions']=grounded_conditions
    versioned=route.revision_mode(db,turn['id'])
    if versioned and not library.revisions.split_conditions(conditions)[0]:
        labels=library.revisions.quoted_versions(quote)
        if len({library.revisions.version_number(v) for v in labels})>1:
            raise RuntimeErrorBase('knowledge_study_omitted_qualifier')
        if labels:options['conditions']=library._names(conditions+[sorted(labels)[0]])
    options.update(aliases=sorted(set(aliases)-{subject}),attributes=attributes)
    # A dated past event remains historical; current claims use the existing source freshness window.
    verified=datetime.fromisoformat(verified_at)
    event_date=re.search(r'(\d{4})[-年](\d{1,2})[-月](\d{1,2})(?:日|\b)',value)
    historical=False
    dated_event=re.search('发射|起飞|首映|成立|创立|发行|发布|建成|出生',predicate)
    if event_date and dated_event and candidate['state']=='fact' and not re.search(r'最新|预计|下个|即将|快要',request['question']):
        try:
            historical=datetime(*(int(v) for v in event_date.groups())).date()<=verified.date()
        except ValueError:pass
    expiry=None if historical else (verified+timedelta(seconds=SOURCE_POLICY['freshness_seconds'])).isoformat()
    if versioned and predicate in ('定义','含义','基本原理') and candidate['state']=='fact' and not options.get('conditions'):
        expiry=None
    return library.admit_turn_source(db,turn['persona_id'],turn['session_id'],turn['id'],source['id'],versioned=versioned,
        active_turn=turn['id'],instance=base['instance'],consent=base['consent'],subject=subject,predicate=predicate,
        value=value,quote=quote,start=start,end=start+len(quote),evidence_state=candidate['state'],
        expires_at=expiry,**options)


_INVALID_FACT={'knowledge_study_invalid_fact','knowledge_study_source_not_offered','knowledge_study_quote_not_unique',
               'knowledge_study_ungrounded_subject','knowledge_study_omitted_qualifier','local_knowledge_invalid_text','local_knowledge_invalid_names',
               'local_knowledge_invalid_time','local_knowledge_invalid_fact','local_knowledge_invalid_span',
               'local_knowledge_evidence_mismatch','local_knowledge_document_withdrawn'}


def commit(flow,turn,base,state,result,*,step='organize'):
    from .private_lookup import _tool_scope
    tid=turn['id']
    with flow.store.connection(write=True) as db:
        if _commit(db,tid):return
        query,model=_tool_scope(flow,db,turn,base)
        attempt=db.execute("SELECT * FROM private_lookup_calls WHERE turn_id=? AND step=?",(tid,step)).fetchone()
        request=packet(flow,db,turn,base,query,model,attempt=attempt['attempt_id'] if attempt else None)
        signature=fingerprint([base,model,step,request,flow.providers.version])
        if semantic.active(db,tid):
            if state in ('received','failed'):
                if not attempt or attempt['state']!=state or attempt['binding']!=signature or json.loads(attempt['result_json'])!=result:
                    raise RuntimeErrorBase('private_reply_source_changed')
            elif state!='empty' or attempt or request['sources']:
                raise RuntimeErrorBase('private_reply_source_changed')
            _store_semantic(db,turn,base,query,signature,attempt['attempt_id'] if attempt else None,result)
            return
        facts=[]
        rejected=[]
        plan=None
        status='insufficient'
        if state=='received':
            if not attempt or attempt['state']!='received' or attempt['binding']!=signature or json.loads(attempt['result_json'])!=result:
                raise RuntimeErrorBase('private_reply_source_changed')
            paper=db.execute('SELECT created_at FROM private_lookup_records WHERE turn_id=?',(tid,)).fetchone()
            try:
                if getattr(flow.providers,'structured_study',False):
                    data=json.loads(result['raw'],object_pairs_hook=_unique)
                    conditional=getattr(flow.providers,'conditional_coverage',False)
                    deps=data.pop('dependencies',[]) if conditional and isinstance(data,dict) else []
                    needed,rows=coverage.requirements(data,request['question'])
                    plan={'requirements':needed,'supports':{}}
                    if conditional:
                        plan['dependencies']=coverage.premise_dependencies(deps,needed)
                else:
                    rows=_facts(result['raw'])
            except (ValueError,TypeError,RecursionError,RuntimeErrorBase):
                status='invalid_result'
                rows=[]
            for ordinal,entry in enumerate(rows):
                db.execute('SAVEPOINT study_fact')
                try:
                    if plan is None:
                        fact=_admit(db,turn,base,request,entry,paper['created_at'])
                        fact=_complete_date(db,turn,base,fact,active_turn=tid)
                    else:
                        fact,parts=coverage.admit(db,turn,base,request,entry,needed,paper['created_at'])
                        plan['supports'][fact]=sorted(set(plan['supports'].get(fact,[]))|set(parts))
                    if fact not in facts:facts.append(fact)
                except RuntimeErrorBase as exc:
                    db.execute('ROLLBACK TO study_fact')
                    if str(exc) not in _INVALID_FACT:raise
                    rejected.append({'ordinal':ordinal,'code':str(exc)})
                finally:db.execute('RELEASE study_fact')
            if facts:status='committed'
            elif rejected:status='invalid_evidence'
        elif state!='empty' or request['sources'] or attempt:
            raise RuntimeErrorBase('private_reply_source_changed')
        db.execute('INSERT INTO knowledge_study_commits VALUES(?,?,?,?,?,?,?)',
                   (tid,attempt['attempt_id'] if attempt else None,signature,status,encoded(facts),len(rejected),timestamp()))
        if rejected:
            db.execute('INSERT INTO knowledge_control VALUES(?,?)',('private.study.rejections.'+tid,encoded(rejected)))
        if plan is not None:
            db.execute('INSERT INTO knowledge_control VALUES(?,?)',('private.study.coverage.'+tid,encoded(plan)))
        record(db,tid,'knowledge_study_committed' if facts else 'knowledge_study_insufficient')


def _store_semantic(db,turn,base,query,signature,attempt_id,result):
    from .private_lookup import evidence
    tid=turn['id']
    data=evidence(db,turn['persona_id'],turn['session_id'],tid,active_turn=tid)
    ids=semantic.store_sources(db,turn,base,data,query,semantic.organizer_labels(result.get('raw')))
    db.execute('INSERT INTO knowledge_study_commits VALUES(?,?,?,?,?,?,?)',
               (tid,attempt_id,signature,'committed' if ids else 'insufficient',encoded(ids),0,timestamp()))
    db.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',('private.semantic.sources.'+tid,'true'))
    record(db,tid,'knowledge_study_committed' if ids else 'knowledge_study_insufficient')


async def _semantic_reread(flow,turn,base,material,context):
    import asyncio
    from .private_lookup import intent
    tid=turn['id']
    with flow.store.connection() as db:
        saved=db.execute('SELECT * FROM knowledge_study_reads WHERE turn_id=?',(tid,)).fetchone()
        if saved:
            facts=route.current_facts(db,turn,intent(db,tid),base['consent'],saved,after_commit=tid) if saved['outcome']=='hit' else []
            return context|{'status':'local_hit' if facts else 'insufficient','lookup_outcome':'studied','facts':facts,'j5_receipt':None}
        original=route.query(db,turn,context['target'])
        question=route.resolved_query(db,turn,context['target'],original,base['consent'],base['instance'])
        ids=json.loads(_commit(db,tid)['fact_ids'])
        candidates=semantic.materials(db,turn,base,original,ids=ids)
    facts=await asyncio.to_thread(semantic.rank,flow.store.paths,question,candidates)
    selected=[f['id'] for f in facts]
    with flow.store.connection(write=True) as db:
        _,live,actual=flow._snapshot(db,tid)
        if live!=base or actual!=material or (selected and semantic.pinned(db,turn,base,original,selected)!=facts):
            raise RuntimeErrorBase('private_reply_source_changed')
        db.execute('INSERT INTO knowledge_study_reads VALUES(?,?,?,?,?,?,?)',(tid,fingerprint(question),
                   'hit' if facts else 'insufficient',encoded(selected),timestamp(),None,0))
        record(db,tid,'knowledge_study_read')
    return context|{'status':'local_hit' if facts else 'insufficient','lookup_outcome':'studied','facts':facts,'j5_receipt':None}


async def reread(flow,turn,base,material,context):
    from .private_lookup import intent
    tid=turn['id']
    with flow.store.connection() as db:
        vector=semantic.active(db,tid)
    if vector:
        return await _semantic_reread(flow,turn,base,material,context)
    structured=getattr(flow.providers,'structured_study',False)
    def lookup(db):
        versioned=route.revision_mode(db,tid)
        query=route.resolved_query(db,turn,context['target'],route.query(db,turn,context['target']),base['consent'],base['instance'],versioned=versioned)
        committed=_commit(db,tid)
        if structured:
            outcome,facts=_bound_read(db,turn,base,query,json.loads(committed['fact_ids']),after_commit=tid,for_selection=True)
            return query,outcome,facts,True
        outcome,facts=library.search(db,turn['persona_id'],turn['session_id'],base['consent'],query,
                                     instance=base['instance'],after_commit=tid,versioned=versioned)
        ids=set(json.loads(committed['fact_ids']))
        facts=[f for f in facts if f['id'] in ids]
        query_bound=False
        if not facts and outcome in ('miss','insufficient','conflict') and ids:
            bound_outcome,facts=_bound_read(db,turn,base,query,json.loads(committed['fact_ids']),after_commit=tid)
            if bound_outcome=='conflict':outcome='conflict'
            query_bound=bool(facts)
        return query,'hit' if facts else 'insufficient' if outcome=='hit' else outcome,facts,query_bound
    with flow.store.connection() as db:
        committed=_commit(db,tid)
        saved=db.execute('SELECT * FROM knowledge_study_reads WHERE turn_id=?',(tid,)).fetchone()
        if saved:
            if saved['outcome']!='hit':return context|{'status':'insufficient','lookup_outcome':'studied','j5_receipt':None}
            facts=route.current_facts(db,turn,intent(db,tid),base['consent'],saved,after_commit=tid)
            return context|{'status':'local_hit','lookup_outcome':'studied','facts':facts,'j5_receipt':saved['selection_id']}
        question,outcome,facts,query_bound=lookup(db)
        plan=coverage.saved_plan(db,tid) if structured else None
    selected=facts
    selection_id=None
    if structured:
        plan=await coverage.resolve_dependencies(flow,turn,base,material,question,plan,facts)
        selected,selection_id=await coverage.select(flow,turn,base,material,question,facts,plan,'study')
    elif query_bound or len(facts)>1 or (facts and (facts[0]['qualifications'] or asks_date_and_place(question) or forward_question(question))):
        options=selection_groups(facts,question)
        selected=[]
        if len(options)>1:
            choice=await flow.choice(tid,0,'J5',{'target':context['target'],'question':question,'options':options,'repair':None},
                                     tuple(options),base,material,receipt_node='study.J5')
            selected=options[choice]
            with flow.store.connection() as db:
                selection_id=db.execute("SELECT attempt_id FROM private_reply_nodes WHERE turn_id=? AND draft=0 AND node='study.J5'",(tid,)).fetchone()[0]
    with flow.store.connection(write=True) as db:
        _,live,actual=flow._snapshot(db,tid)
        if live!=base or actual!=material or lookup(db)!=(question,outcome,facts,query_bound):
            raise RuntimeErrorBase('private_reply_source_changed')
        read_outcome=('hit' if selected else 'insufficient' if facts else outcome) if committed['status']=='committed' else committed['status']
        db.execute('INSERT INTO knowledge_study_reads VALUES(?,?,?,?,?,?,?)',(tid,fingerprint(question),read_outcome,
                   encoded([f['id'] for f in selected]),timestamp(),selection_id,int(query_bound)))
        record(db,tid,'knowledge_study_read')
    return context|{'status':'local_hit' if selected else 'insufficient','lookup_outcome':'studied',
                    'facts':selected,'j5_receipt':selection_id if selected else None}


async def finish(flow,turn,base,material,context):
    from .private_lookup import _call
    tid=turn['id']
    with flow.store.connection() as db:
        paper=db.execute('SELECT outcome FROM private_lookup_records WHERE turn_id=?',(tid,)).fetchone()
        if not paper or paper['outcome']!='acquired':
            outcome=paper['outcome'] if paper else 'unavailable'
            return context|{'status':outcome,'lookup_outcome':outcome}
        committed=_commit(db,tid)
        vector=semantic.active(db,tid)
    source_only=vector and (not getattr(flow.providers,'study_enabled',False) or getattr(flow.providers,'structured_study',False))
    if not committed and source_only:
        from .private_lookup import _tool_scope
        with flow.store.connection(write=True) as db:
            if not semantic.active(db,tid):raise RuntimeErrorBase('private_lookup_scope_changed')
            query,model=_tool_scope(flow,db,turn,base)
            if not _commit(db,tid):_store_semantic(db,turn,base,query,fingerprint([base,query,'semantic']),None,{})
    elif not committed:
        step='organize'
        state,result=await _call(flow,turn,base,step)
        if (not vector and getattr(flow.providers,'structured_study',False) and state in ('failed','unknown')
                and coverage.truncated_response(result)):
            step='organize_retry'
            state,result=await _call(flow,turn,base,step)
        if state=='parked':return None
        if state not in ('received','empty') and not (vector and state=='failed'):
            raise RuntimeErrorBase('knowledge_study_unknown' if state=='unknown' else 'knowledge_study_failed')
        commit(flow,turn,base,state,result,step=step)
    return await reread(flow,turn,base,material,context)
