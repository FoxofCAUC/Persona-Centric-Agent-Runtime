"""Bind a local-first read and any subsequent web admission to one desktop turn."""
import json
import asyncio
import re
import sqlite3

from . import local_knowledge as knowledge
from . import knowledge_coverage as coverage
from . import semantic_knowledge as semantic
from .archive import encoded, fingerprint
from .models import RuntimeErrorBase, timestamp


def query(db,turn,target):
    from .turn_input import load_turn_input
    members=load_turn_input(db,dict(turn))
    selected=[item for item in members if item['id']==target] or members
    return '\n'.join(item['text'] for item in selected)


def revision_mode(db,tid):
    return db.execute('SELECT 1 FROM knowledge_control WHERE key=?',('private.study.revisions.'+tid,)).fetchone() is not None


def preceding_context(db,turn,target,consent):
    from .knowledge_native_memory import eligible
    refs=db.execute("""SELECT r.binding_json FROM input_references r JOIN input_batch_members b ON b.event_id=r.event_id
        WHERE b.batch_id=?""",(turn['input_batch_id'],)).fetchall()
    if refs:
        bindings=[json.loads(ref[0]) for ref in refs]
        if any(b.get('status')!='found' for b in bindings):return None
        selected=[b for b in bindings if target in b['member_ids']+b['reply_ids']]
        origins={b['turn_id'] for b in selected or bindings}
        if len(origins)!=1:return None
        origin=origins.pop()
    else:
        previous=db.execute("""SELECT id FROM turns WHERE persona_id=? AND session_id=? AND channel='desktop'
            AND rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY rowid DESC LIMIT 1""",
            (turn['persona_id'],turn['session_id'],turn['id'])).fetchone()
        origin=previous[0] if previous else None
    if not origin or not eligible(db,origin):return None
    old=db.execute('SELECT * FROM turns WHERE id=? AND session_id=? AND persona_id=?',
                   (origin,turn['session_id'],turn['persona_id'])).fetchone()
    it=db.execute('SELECT query_json,origin,consent_revision,choice,target_json FROM private_lookup_intents WHERE turn_id=?',(origin,)).fetchone()
    prior=None
    if old and it and (it['origin']!='session' or consent['enabled'] and consent['revision']==it['consent_revision']):
        if it['choice']=='retrieve':
            prior=json.loads(it['query_json'])['question'] if it['query_json'] else query(db,old,'batch')
        elif it['choice']=='none':
            read=receipt(db,origin)
            if read and read['outcome']=='continued':
                prior=query(db,old,json.loads(it['target_json']))
    return origin,prior,bool(refs)


def resolved_query(db,turn,target,question,consent,instance,*,versioned=False,public_only=False):
    if semantic.active(db,turn['id']):
        if public_only:return question
        selected=db.execute('SELECT value FROM knowledge_control WHERE key=?',('private.semantic.context.'+turn['id'],)).fetchone()
        if not selected:return question
        previous=preceding_context(db,turn,target,consent)
        if not previous or json.loads(selected[0])!=[previous[0],fingerprint(previous[1])]:
            raise RuntimeErrorBase('private_reply_source_changed')
        return question+'\n'+previous[1]
    # Old receipts were bound by the pre-vector referent resolver.
    pronoun=re.match(r'^\s*(?:那|那么)?(?:它|她|他|这个|该项)(?:的)?',question)
    if not pronoun:return question
    previous=preceding_context(db,turn,target,consent)
    if not previous:return question
    origin=previous[0]
    selected=answer_receipt(db,origin)
    if not selected or selected['outcome']!='hit':return question
    ids=json.loads(selected['fact_ids'])
    referents={}
    for ident in ids:
        prior=db.execute("""SELECT f.spec_json FROM local_knowledge_facts f JOIN turns t ON t.id=?
            WHERE f.id=? AND t.persona_id=? AND t.session_id=? AND f.withdrawn=0""",
            (origin,ident,turn['persona_id'],turn['session_id'])).fetchone()
        if not prior:return question
        spec=json.loads(prior[0])
        if public_only:
            source=spec.get('turn_source')
            if not source or spec['conditions']:return question
            scope=db.execute('SELECT origin,query_json FROM private_lookup_intents WHERE turn_id=?',(source[0],)).fetchone()
            if not scope or scope['origin'] not in ('explicit','derived','confirmed') or not scope['query_json']:
                return question
            authorized=json.loads(scope['query_json'])['question']
            if knowledge._matches(knowledge._norm(authorized),[spec['subject']]) is None:return question
        subject=' '.join([spec['subject'],*spec['conditions']])
        _,facts=knowledge.search(db,turn['persona_id'],turn['session_id'],consent,subject+' '+spec['predicate'],
                                 instance=instance,versioned=versioned,fact_id=ident)
        if not any(f['id']==ident for f in facts):return question
        referents[knowledge._norm(subject)]=subject
    if len(referents)!=1:return question
    return next(iter(referents.values()))+' '+question[pronoun.end():]


async def decide(flow,turn,base,material,target):
    from .private_lookup import NO_QUERY, _basis, enabled, projection, previous_reply, refers_back
    tid=turn['id']
    vector=getattr(flow.providers,'semantic_lookup',True)
    with flow.store.connection() as db:
        question=query(db,turn,target)
        permitted=knowledge.enabled(db,turn['persona_id'])
    choice='none'
    continuation=None
    if target!='wait' and permitted and (not NO_QUERY.search(question) or knowledge.local_only(question)):
        state={'target':target,'question':question}
        referent=previous_reply(material) if refers_back(question) else ''
        if vector:
            continuation=await _research_continuation(flow,turn,target,base,question,material)
            if not referent and continuation:
                referent=continuation[1]
        if referent:
            state['previous_reply']=referent
        choice=await flow.choice(tid,0,'J4',state,('retrieve','none'),base,material)
    outcome='not_requested' if permitted else 'disabled'
    facts=[]
    selected=None
    local_query=question
    versioned=getattr(flow.providers,'knowledge_revisions',False)
    structured=getattr(flow.providers,'structured_study',False)
    coverage_id=None
    plan=None
    chosen_context=None
    refresh=not knowledge.local_only(question) and knowledge._REFRESH.search(question)
    read_base=base|{'versioned_knowledge':versioned}
    def lookup(db,text,*,learn=False):
        outcome,items=knowledge.search(db,turn['persona_id'],turn['session_id'],base['consent'],text,instance=base['instance'],versioned=versioned)
        match=False
        if structured and outcome not in ('disabled','refresh_requested'):
            items={f['id']:f for f in items}
            for f in coverage.warm_candidates(db,turn,read_base,text):
                items.setdefault(f['id'],f)
            if items:return 'hit',list(items.values())[:8],'coverage'
        if getattr(flow.providers,'study_enabled',False):
            from .knowledge_study import previous_answer,related_facts,preferred_dates
            if not items:
                items=previous_answer(db,turn,read_base,text,outcome)
                if items:match='exact'
                else:
                    items=related_facts(db,turn,read_base,text,outcome)
                    if items:match='topic'
            if items:
                preferred=preferred_dates(db,turn,read_base,text,items,learn=learn)
                if learn and preferred!=items:
                    # New date evidence can change a fallback into a direct FTS hit.
                    return lookup(db,text)
                items=preferred
                outcome='hit'
        return outcome,items,match
    if choice=='retrieve' and vector:
        with flow.store.connection(write=True) as db:
            previous=preceding_context(db,turn,target,base['consent'])
            if not refresh:
                semantic.import_prior_sources(db,turn,base)
            candidates=[] if refresh else semantic.materials(db,turn,base,question)
            will_need_index=not candidates and enabled(db,turn['persona_id']) and not knowledge.local_only(question)
        anchors=[]
        if continuation and previous and continuation[0]==previous[0]:
            verified={f['id']:semantic.public_material(f) for f in candidates}
            anchors=[f for f in continuation[2][:1] if verified.get(f['id'])==f]
        if previous and previous[1] and previous[1]!=question and (anchors or previous[2] or refers_back(question) or candidates and
                await asyncio.to_thread(semantic.related_questions,flow.store.paths,question,previous[1])):
            chosen_context=[previous[0],fingerprint(previous[1])]
            local_query=question+'\n'+previous[1]
        if will_need_index:
            await asyncio.to_thread(semantic.require_encoder,flow.store.paths)
        facts=await asyncio.to_thread(semantic.rank,flow.store.paths,local_query,candidates)
        if anchors:
            identities={(f['url'],f['text']) for f in anchors}
            facts=(anchors+[f for f in facts if (f['url'],f['text']) not in identities])[:semantic.PASSAGE_LIMIT]
        outcome='hit' if facts else 'refresh_requested' if refresh else 'miss'
    elif choice=='retrieve':
        with flow.store.connection(write=True) as db:
            local_query=resolved_query(db,turn,target,question,base['consent'],base['instance'],versioned=versioned)
            outcome,facts,query_bound=lookup(db,local_query,learn=True)
            if structured:
                plan=coverage.warm_plan(db,turn,local_query,facts)
        from .knowledge_study import asks_date_and_place,forward_question,selection_groups
        study=getattr(flow.providers,'study_enabled',False)
        need_pair=study and (asks_date_and_place(local_query) or forward_question(local_query))
        if structured and facts:
            selected,coverage_id=await coverage.select(flow,turn,base,material,local_query,facts,plan,'local')
        elif query_bound=='topic' or len(facts)>1 or (facts and (facts[0]['qualifications'] or need_pair)):
            groups={f'fact/{i}':[fact] for i,fact in enumerate(facts)}
            if study:
                groups=selection_groups(facts,local_query)
                if query_bound=='exact' and len(facts)==2 and 'first_pair' in groups:groups={'first_pair':groups['first_pair']}
            groups['insufficient']=[]
            selected=[]
            if len(groups)>1:
                option=await flow.choice(tid,0,'J5',{'target':target,'question':question,'options':groups,'repair':None},
                                         tuple(groups),base,material,receipt_node='local.J5')
                selected=groups[option]
    elif vector and continuation and not refresh:
        facts=continuation[2]
        outcome='continued'
    with flow.store.connection(write=True) as db:
        _,live,actual=flow._snapshot(db,tid)
        if live!=base or actual!=material:
            raise RuntimeErrorBase('private_lookup_scope_changed')
        if vector and (choice=='retrieve' or outcome=='continued'):
            if chosen_context:
                previous=preceding_context(db,turn,target,base['consent'])
                if not previous or chosen_context!=[previous[0],fingerprint(previous[1])]:
                    raise RuntimeErrorBase('private_reply_source_changed')
                db.execute('INSERT INTO knowledge_control VALUES(?,?)',('private.semantic.context.'+tid,encoded(chosen_context)))
            if outcome=='continued':
                previous=preceding_context(db,turn,target,base['consent'])
                if not previous or not previous[1] or previous[0]!=continuation[0]:
                    raise RuntimeErrorBase('private_reply_source_changed')
                db.execute('INSERT INTO knowledge_control VALUES(?,?)',
                           ('private.semantic.continued.'+tid,encoded([previous[0],fingerprint(previous[1])])))
            if facts and semantic.pinned(db,turn,base,question,[f['id'] for f in facts])!=facts:
                raise RuntimeErrorBase('private_reply_source_changed')
        elif choice=='retrieve':
            current_query=resolved_query(db,turn,target,question,base['consent'],base['instance'],versioned=versioned)
            current=lookup(db,current_query)
            if (current_query!=local_query or current!=(outcome,facts,query_bound)
                    or structured and plan!=coverage.warm_plan(db,turn,current_query,current[1])):
                raise RuntimeErrorBase('private_reply_source_changed')
        selection_id=None
        if selected is not None:
            choice_receipt=db.execute("""SELECT attempt_id FROM private_reply_nodes WHERE turn_id=? AND
                node='local.J5' AND draft=0 AND state='received'""",(tid,)).fetchone()
            selection_id=coverage_id or (choice_receipt[0] if choice_receipt else None)
            facts=selected
            if not facts:outcome='insufficient'
        task=None
        origin=None
        state='none'
        if choice=='retrieve' and outcome!='hit' and enabled(db,turn['persona_id']) and not knowledge.local_only(question):
            ids=material['current'] if target=='batch' else [target]
            task,origin=projection(db,turn,ids,base['consent']['enabled'],grounded=True)
            if origin!='declined':
                state='ready' if task else 'waiting'
        db.execute("""INSERT INTO private_lookup_intents VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (tid,encoded(target),origin,choice,_basis(base,material),None,state,
             encoded(task) if task else None,None,base['consent']['revision']))
        db.execute('INSERT INTO local_knowledge_reads VALUES(?,?,?,?,?,?)',
            (tid,fingerprint(local_query),outcome,encoded([f['id'] for f in facts]),timestamp(),selection_id))
        if getattr(flow.providers,'study_enabled',False):
            db.execute('INSERT INTO knowledge_control VALUES(?,?)',('private.study.version.'+tid,encoded(flow.providers.version)))
        if versioned:
            db.execute('INSERT INTO knowledge_control VALUES(?,?)',('private.study.revisions.'+tid,'true'))
        if structured:
            db.execute('INSERT INTO knowledge_control VALUES(?,?)',('private.study.structured.'+tid,'true'))
        if vector:
            db.execute('INSERT INTO knowledge_control VALUES(?,?)',('private.semantic.turn.'+tid,'true'))


async def _research_continuation(flow,turn,target,base,question,material):
    """Resolve related material once for J4 and the subsequent authorized local read."""
    from .private_lookup import previous_reply
    reply=previous_reply(material)
    if not reply:
        return None
    with flow.store.connection() as db:
        previous=preceding_context(db,turn,target,base['consent'])
        if not previous or not previous[1] or previous[2] or not semantic.active(db,previous[0]):
            return None
        proof=answer_receipt(db,previous[0])
        if not has_local_material(proof):
            return None
        ids=json.loads(proof['fact_ids'])
        original=semantic.materials(db,turn,base,previous[1],ids=ids)
        if not original or {f['id'] for f in original}!=set(ids):
            return None
        candidates=semantic.materials(db,turn,base,question,ids=_research_source_ids(db,turn,base,previous[0],original))
    try:
        matching=await asyncio.to_thread(semantic.rank,flow.store.paths,question,candidates)
    except RuntimeErrorBase as exc:
        if str(exc)!='local_knowledge_embedding_unavailable':
            raise
        return None
    return (previous[0],reply,matching) if matching else None


def _research_source_ids(db,turn,base,previous,selected):
    """Read the acquisition recorded for each selected source, including on warm reads."""
    from .knowledge_native_memory import eligible
    ids=[f['id'] for f in selected]
    origins={previous}
    recorded=set()
    for (origin,) in db.execute("""SELECT json_extract(spec_json,'$.turn_source[0]') FROM local_knowledge_facts
        WHERE id IN (SELECT value FROM json_each(?))""",(encoded(ids),)):
        if origin:origins.add(origin)
    for origin,scope,revision,facts in db.execute("""SELECT t.id,i.origin,i.consent_revision,c.fact_ids
        FROM knowledge_study_commits c JOIN turns t ON t.id=c.turn_id JOIN private_lookup_intents i ON i.turn_id=t.id
        WHERE c.status='committed' AND t.persona_id=? AND t.session_id=? AND t.channel='desktop'
        AND t.cancelled_at IS NULL AND t.rowid<(SELECT rowid FROM turns WHERE id=?)
        AND t.id IN (SELECT value FROM json_each(?))""",
        (turn['persona_id'],turn['session_id'],turn['id'],encoded(sorted(origins)))):
        consent=base['consent']
        if eligible(db,origin) and (scope!='session' or consent['enabled'] and revision==consent['revision']):
            recorded.update(json.loads(facts))
    ids.extend(sorted(recorded))
    # Old or manually admitted records can still recall other eligible excerpts of the same document.
    docs={doc for fact in selected if fact['id'] not in recorded for doc in fact['source_ids']}
    ids.extend(row[0] for row in db.execute("""SELECT id FROM local_knowledge_facts
        WHERE document_id IN (SELECT value FROM json_each(?))""",(encoded(sorted(docs)),)))
    return list(dict.fromkeys(ids))


def receipt(db,tid):
    return db.execute('SELECT * FROM local_knowledge_reads WHERE turn_id=?',(tid,)).fetchone()


def has_local_material(record):
    return bool(record and record['outcome'] in ('hit','continued'))


def answer_receipt(db,tid):
    prior=receipt(db,tid)
    if prior and not has_local_material(prior) and db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_study_reads'").fetchone():
        reread=db.execute('SELECT * FROM knowledge_study_reads WHERE turn_id=?',(tid,)).fetchone()
        if reread:return reread
    return prior

def recover_storage(store,turn,error):
    from .private_reply_entry import stop
    from .private_transport_recovery import park_turn
    if error=='local_knowledge_index_corrupt':
        try:
            with store.connection(write=True) as db:
                knowledge.rebuild(db)
        except sqlite3.DatabaseError:
            with store.connection(write=True) as db:
                stop(db,turn['id'],'local_knowledge_store_unavailable')
            return
    with store.connection(write=True) as db:
        park_turn(db,turn,'storage')


def current_facts(db,turn,it,consent,record,*,after_commit=None):
    target=json.loads(it['target_json'])
    instance=db.execute('SELECT instance_id FROM private_reply_turns WHERE turn_id=?',(turn['id'],)).fetchone()[0]
    versioned=revision_mode(db,turn['id'])
    question=resolved_query(db,turn,target,query(db,turn,target),consent,instance,versioned=versioned)
    if record['query_hash']!=fingerprint(question):
        raise RuntimeErrorBase('private_reply_source_changed')
    if semantic.active(db,turn['id']):
        return semantic.pinned(db,turn,{'instance':instance,'consent':consent},query(db,turn,target),json.loads(record['fact_ids']))
    outcome,facts=knowledge.search(db,turn['persona_id'],turn['session_id'],consent,question,instance=instance,after_commit=after_commit,versioned=versioned)
    ids=json.loads(record['fact_ids'])
    structured=db.execute('SELECT 1 FROM knowledge_control WHERE key=?',('private.study.structured.'+turn['id'],)).fetchone()
    if structured:
        from .knowledge_study import _bound_read
        _,pinned=_bound_read(db,turn,{'instance':instance,'consent':consent,'versioned_knowledge':True},question,ids,
                             after_commit=after_commit,for_selection=True)
        found={f['id']:f for f in (pinned if after_commit else [*facts,*pinned])}
        if not ids or not all(i in found for i in ids):
            raise RuntimeErrorBase('private_reply_source_changed')
        selected=[found[i] for i in ids]
        if after_commit:
            plan=coverage.saved_plan(db,turn['id'])
            if not coverage.complete(plan,selected):
                receipt=db.execute("""SELECT result_json FROM private_reply_nodes WHERE attempt_id=? AND turn_id=?
                    AND state='received' AND node LIKE 'study.J5.coverage.%'""",(record['selection_id'],turn['id'])).fetchone()
                if not receipt or coverage.corrections(plan,selected).get(json.loads(receipt[0]))!=selected:
                    raise RuntimeErrorBase('private_reply_source_changed')
        return selected
    if after_commit and record['query_bound']:
        from .knowledge_study import _bound_facts
        facts=_bound_facts(db,turn,{'instance':instance,'consent':consent},question,ids,after_commit=after_commit)
        outcome='hit' if facts else 'insufficient'
    elif not after_commit and not facts and db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_study_reads'").fetchone():
        from .knowledge_study import previous_answer,related_facts
        base={'instance':instance,'consent':consent}
        facts=previous_answer(db,turn,base,question,outcome)
        if not facts and record['selection_id']:facts=related_facts(db,turn,base,question,outcome)
        if facts:outcome='hit'
    found={f['id']:f for f in facts}
    if versioned and facts and any(i not in found for i in ids):
        # A later corroboration can change the preferred citation without invalidating the old one.
        supported={ident for f in facts for e in f.get('evidence',[]) for ident in e['receipts']}
        from .knowledge_study import _bound_facts
        base={'instance':instance,'consent':consent,'versioned_knowledge':True}
        for ident in ids:
            if ident in supported and ident not in found:
                verified=_bound_facts(db,turn,base,question,[ident],after_commit=after_commit)
                found.update((f['id'],f) for f in verified)
    if not after_commit and facts and any(i not in found for i in ids):
        from .knowledge_study import preferred_dates
        found.update((f['id'],f) for f in preferred_dates(db,turn,{'instance':instance,'consent':consent},question,facts))
    if outcome!='hit' or not ids or not all(i in found for i in ids):
        raise RuntimeErrorBase('private_reply_source_changed')
    return [found[i] for i in ids]


def validate_hit(db,turn,it,consent,context):
    record=answer_receipt(db,turn['id'])
    after=turn['id'] if context.get('lookup_outcome')=='studied' else None
    if not has_local_material(record) or not it or record['selection_id']!=context.get('j5_receipt'):
        raise RuntimeErrorBase('private_reply_source_changed')
    if record['outcome']=='continued':
        _validate_continuation(db,turn,it,consent,context,after)
    elif it['choice']!='retrieve':
        raise RuntimeErrorBase('private_reply_source_changed')
    actual=current_facts(db,turn,it,consent,record,after_commit=after)
    if revision_mode(db,turn['id']) and not semantic.active(db,turn['id']):
        def pinned(rows):
            return [(f['claim_id'],f['revision_id'],knowledge.revisions.model_material(f)) for f in rows]
        matches=pinned(actual)==pinned(context['facts'])
    else:
        matches=actual==context['facts']
    if not matches:raise RuntimeErrorBase('private_reply_source_changed')
    return actual


def _validate_continuation(db,turn,it,consent,context,after):
    tid=turn['id']
    if after or it['choice']!='none' or it['state']!='none' or not semantic.active(db,tid):
        raise RuntimeErrorBase('private_reply_source_changed')
    if not db.execute("""SELECT 1 FROM private_reply_nodes WHERE turn_id=? AND attempt_id=? AND node='J4'
        AND draft=0 AND state='received' AND result_json=?""",(tid,context.get('j4_receipt'),encoded('none'))).fetchone():
        raise RuntimeErrorBase('private_reply_source_changed')
    binding=db.execute('SELECT value FROM knowledge_control WHERE key=?',('private.semantic.continued.'+tid,)).fetchone()
    previous=preceding_context(db,turn,context['target'],consent)
    if not binding or not previous or not previous[1] or json.loads(binding[0])!=[previous[0],fingerprint(previous[1])]:
        raise RuntimeErrorBase('private_reply_source_changed')
    proof=answer_receipt(db,previous[0])
    if not has_local_material(proof):
        raise RuntimeErrorBase('private_reply_source_changed')
    instance=db.execute('SELECT instance_id FROM private_reply_turns WHERE turn_id=?',(tid,)).fetchone()[0]
    semantic.pinned(db,turn,{'instance':instance,'consent':consent},previous[1],json.loads(proof['fact_ids']))


def details(db,store,turn,context):
    from . import session_privacy
    from .config import ConfigStore
    from .private_lookup import intent
    cfg=ConfigStore(store.paths)
    settings=cfg.load()
    consent=session_privacy.current(db,cfg,settings,turn['persona_id'],turn['session_id'],
                                   session_privacy.recipients(cfg,settings))
    actual=validate_hit(db,turn,intent(db,turn['id']),consent,context)
    if revision_mode(db,turn['id']) and not semantic.active(db,turn['id']):
        sources=[{'id':e['document_id'],'title':e['title'],'url':e['url'],'content':e['text'],'date':e['date'],
                  'kind':e['source_kind'],'span':e['span'],'relation':e['relation']}
                 for f in actual for e in f['evidence']]
        return {'notes':'\n'.join(f['text'] for f in context['facts']),'sources':sources}
    return {'notes':'\n'.join(f['text'] for f in context['facts']),
            'sources':[{'id':f['source_ids'][0],'title':f['title'],'url':f['url'],'content':f['text'],
                        'date':f['date'],'kind':f['source_kind']} for f in context['facts']]}
