"""Human-first general retrieval: collect useful material, then let the persona think."""
from datetime import datetime,timedelta,timezone
import ipaddress,json,re
from urllib.parse import urlsplit,urlunsplit
from .archive import fingerprint
from .models import RuntimeErrorBase

PROFILE='conversational_web_v1'
STAGES={'conversation_web_decision','conversation_search_plan','conversation_search','conversation_fetch','conversation_organize'}
OUTPUT_PROFILE='retrieval-high-output-v1'
OUTPUT_FLOORS={'conversation_search_plan':4096,'conversation_organize':8192}

def processing_model(model,data):
    """Freeze a separate thinking/output allowance for newly registered work."""
    stage=data.get('stage')
    if data.get('request_profile')==OUTPUT_PROFILE and stage in OUTPUT_FLOORS:
        return model|{'_reasoning_effort':'high','max_tokens':max(model['max_tokens'],OUTPUT_FLOORS[stage])}
    return model

def reasoning_model(model,knowledge=None,*,plan=False):
    if plan or (knowledge or {}).get('mode')==PROFILE and not knowledge.get('translation') and (
            knowledge.get('source_ids') or knowledge.get('retrieval',{}).get('needed')):
        return model|{'_reasoning_effort':'high'}
    return model

PLAN=('Jev已决定本轮需要新增公开联网资料。你只负责规划如何搜索或读取，不再次判断是否需要联网。'
 '将当前问题及必要前文提炼成公开查询，不把私人经历、联系方式、账号密钥或人物设定拼进搜索。'
 '明确点名的平台要实际查找；官网、社区、普通帖子均可参考，排序不决定真假。'
 '只输出工具计划JSON：{"queries":["公开查询"],"read_urls":[],"reuse_previous":false,"official_domains":[],"community_domains":[]}。'
 '至少提供一条查询或链接，最多各3条。reuse_previous仅表示补充已有资料，不代替本轮联网。域名只影响参考排序，不限制网站。')

def public_url(value):
    try:
        p=urlsplit(value);host=(p.hostname or '').lower()
        if (p.scheme not in ('http','https') or not host or p.username or p.password or p.port not in (None,80,443)
                or host=='localhost' or host.endswith(('.localhost','.local','.internal')) or any(ord(c)<32 for c in value)):
            raise ValueError()
        try:address=ipaddress.ip_address(host)
        except ValueError:address=None
        if address is not None and not address.is_global:raise RuntimeErrorBase('public_web_address_required')
        return urlunsplit((p.scheme,p.netloc,p.path or '/',p.query,''))
    except (ValueError,TypeError,AttributeError):raise RuntimeErrorBase('public_web_address_required') from None

def _json(raw):
    if not isinstance(raw,str):return {}
    value=raw.strip()
    if value.startswith('```'):value=re.sub(r'^```(?:json)?\s*|\s*```$','',value,flags=re.I)
    try:return json.loads(value)
    except ValueError:
        try:return json.JSONDecoder().raw_decode(value[value.index('{'):])[0]
        except (ValueError,TypeError):return {}

def plan_output(raw):
    value=_json(raw)
    if not isinstance(value,dict):value={}
    def strings(key,maximum):
        rows=value.get(key,[]);rows=[rows] if isinstance(rows,str) else rows
        return list(dict.fromkeys(str(x).strip() for x in rows if isinstance(x,str) and x.strip()))[:maximum] if isinstance(rows,list) else []
    queries=[q[:128] for q in strings('queries',3)];urls=strings('read_urls',3)
    return {'queries':queries,'read_urls':urls,
        'reuse_previous':bool(value.get('reuse_previous')),'official_domains':strings('official_domains',8),
        'community_domains':strings('community_domains',8),'format_readable':bool(queries or urls)}

def messages(payload):
    data=payload['knowledge_processing']
    from .knowledge_translation import ORGANIZE
    instruction=PLAN if data['stage']=='conversation_search_plan' else ORGANIZE if data['stage']=='conversation_organize' else None
    if instruction is None:return []
    return [{'role':'system','content':instruction},{'role':'user','content':json.dumps(data['input'],ensure_ascii=False)}]

def rank(row,plan):
    host=(urlsplit(row['url']).hostname or '').lower()
    match=lambda domains:any(host==d.lower() or host.endswith('.'+d.lower()) for d in domains)
    official=match(plan.get('official_domains',[]))
    community=match(plan.get('community_domains',[])) or any(k in host for k in ('miyoushe','hoyolab','nga','tieba','bilibili','reddit','taptap'))
    text=' '.join(str(row.get(k,'')) for k in ('title','summary','content'))
    popular=bool(re.search(r'(?:点赞|收藏|评论|阅读|播放|浏览)\s*[:：]?\s*(?:[1-9][0-9]{2,}|[0-9.]+\s*[万wWkK])',text))
    base=row.get('rerank_score',row.get('rerank',2))
    base=float(base) if type(base) in (int,float) else 2
    bonus=.3 if official else .2 if community and popular else .05 if community else 0
    return (-base-bonus,0 if community else 1)

def validate_record(db,pid,channel,meta):
    from .knowledge_service import read
    record=read(db,'conversation.record.'+meta.get('record_id',''),{})['data']
    if not record or record.get('persona_id')!=pid or record.get('channel')!=channel :
        raise RuntimeErrorBase('knowledge_scope_changed')
    return record

class GeneralRetrieval:
    def __init__(self,processor):self.processor=processor;self.store=processor.store;self.service=processor.service
    def check(self,db,turn,data):
        from .knowledge_service import read,POLICY
        reader=read(db,'policy.'+turn['persona_id'],POLICY)['data']
        if not reader['enabled'] or turn['channel'] not in reader['channels']:raise RuntimeErrorBase('knowledge_permission_changed')
        if data['stage']=='conversation_fetch':public_url(data['input']['url'])
    async def execute(self,model,payload):
        data=payload['knowledge_processing'];args=data['input']
        from .knowledge_web_transport import request
        stage='search' if data['stage']=='conversation_search' else 'fetch'
        values={'query':args['query'][:128],'count':8,'need_content':True} if stage=='search' else {'url':public_url(args['url'])}
        if getattr(self,'transport',None):return await self.transport(stage,values,model)
        if urlsplit(model['base_url']).hostname!='api.deepseek.com':raise RuntimeErrorBase('search_provider_configuration_required')
        return await request(stage,values,model)
    def accept(self,payload,raw):
        stage=payload['knowledge_processing']['stage'];data=_json(raw)
        if stage=='conversation_web_decision':
            from .knowledge_web_decision import parse
            return parse(raw)
        if stage=='conversation_search_plan':return plan_output(raw)
        if stage=='conversation_organize':
            notes=data.get('notes') if isinstance(data,dict) else None
            return {'notes':notes if isinstance(notes,str) and notes.strip() else raw.strip()}
        if not isinstance(data,dict):return {'sources':[],'readable':False}
        if stage=='conversation_fetch':
            content=data.get('content')
            return {'sources':[{'title':str(data.get('title') or payload['knowledge_processing']['input']['url']),
                'url':public_url(payload['knowledge_processing']['input']['url']),'content':content,'kind':'page_content'}] if isinstance(content,str) and content.strip() else [],'readable':bool(content)}
        rows=[]
        for row in data.get('results',[])[:20] if isinstance(data.get('results'),list) else []:
            if not isinstance(row,dict):continue
            try:url=public_url(row.get('url'))
            except RuntimeErrorBase:continue
            content=row.get('content') if isinstance(row.get('content'),str) else ''
            summary=row.get('summary') or row.get('snippet') or ''
            rows.append({'url':url,'title':str(row.get('title') or url),'content':content,'summary':str(summary),
                'date':row.get('date') if isinstance(row.get('date'),str) else None,'kind':'search_content' if content.strip() else 'search_summary','rerank_score':row.get('rerank_score',2)})
        return {'sources':rows,'readable':True}
    def previous(self,db,turn):
        from .knowledge_service import read
        rows=db.execute("SELECT c.details_json FROM context_uses c JOIN turns t ON t.id=c.turn_id WHERE t.persona_id=? AND t.channel=? AND t.session_id=? AND t.status='completed' AND t.rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY t.rowid DESC LIMIT 4",(turn['persona_id'],turn['channel'],turn['session_id'],turn['id'])).fetchall()
        for row in rows:
            meta=json.loads(row[0]).get('knowledge',{})
            if meta.get('mode')!=PROFILE:continue
            try:record=validate_record(db,turn['persona_id'],turn['channel'],meta)
            except RuntimeErrorBase:continue
            ids=set(meta.get('source_ids',[]));sources=[s for s in record['sources'] if s['id'] in ids]
            if sources:return {'sources':sources,'organized':record.get('organized',{})}
        return {'sources':[],'organized':{}}
    def prepare(self,turn):
        from .knowledge_service import read,POLICY
        from .turn_input import input_text
        with self.store.connection() as db:
            reader=read(db,'policy.'+turn['persona_id'],POLICY)['data'];prior=self.previous(db,turn);previous=prior['sources']
            question=input_text(db,turn);web_policy=read(db,'web.policy.'+turn['persona_id'],{'enabled':False})['data']
            rows=db.execute("SELECT t.* FROM turns t WHERE t.persona_id=? AND t.channel=? AND t.session_id=? AND t.status='completed' AND t.rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY t.rowid DESC LIMIT 2",(turn['persona_id'],turn['channel'],turn['session_id'],turn['id'])).fetchall()
            recent=[];context=[]
            for row in reversed(rows):
                text=input_text(db,dict(row));recent.append(text)
                reply=db.execute('SELECT text FROM events WHERE id=?',(row['reply_event_id'],)).fetchone()
                context.append({'user':text,'assistant':reply[0] if reply else ''})
        sources=[];errors=[];calls=[];needed=False;used_previous=False
        decision={'need_web':False,'decision_source':'disabled'}
        def call(stage,args):
            try:
                result,tid=self.processor._call(turn,stage,args)
                if tid not in calls:calls.append(tid)
                return result
            except RuntimeErrorBase as error:
                tid=getattr(error,'task_id',None)
                if tid and tid not in calls:calls.append(tid)
                if any(part in str(error) for part in ('scope_changed','cancelled','knowledge_stopped','knowledge_request_inactive')):raise
                if stage!='conversation_web_decision' and 'private_cloud' in str(error):raise
                errors.append(str(error));return {}
        if reader['enabled'] and turn['channel'] in reader['channels'] and web_policy.get('enabled') and self.processor.policy(turn['persona_id'])['data'].get('enabled'):
            today=datetime.fromisoformat(turn['created_at']).astimezone(timezone(timedelta(hours=8))).date().isoformat()
            state={'question':question,'recent_context':context,'today':today,'available_sources':[{k:v.get(k) for k in ('title','url','date')} for v in previous],'cached_notes_available':bool(prior['organized'].get('notes'))}
            while state['recent_context'] and len(json.dumps(state,ensure_ascii=False))>self.processor.model()['max_input_chars']-1000:state['recent_context']=state['recent_context'][1:]
            decision=call('conversation_web_decision',state)
            if type(decision.get('need_web')) is not bool:
                decision={'need_web':True,'decision_source':'policy_fallback','fallback_reason':errors[-1] if errors else 'jev_web_decision_unavailable'}
            decision['task_id']=calls[-1] if calls else None
            needed=decision['need_web']
            if not needed:
                sources.extend(previous);used_previous=bool(previous)
            else:
                plan=call('conversation_search_plan',{'question':question,'recent_user_questions':recent,
                    'today':today,'available_sources':[{k:v.get(k) for k in ('title','url','summary','date')} for v in previous]})
                if not plan.get('format_readable'):errors.append('planner_format_unavailable')
                if plan.get('reuse_previous'):sources.extend(previous);used_previous=bool(previous)
                candidates=[];query_leads=[]
                for query in plan.get('queries',[]):
                    found=call('conversation_search',{'query':query}).get('sources',[]);candidates.extend(found)
                    if found:query_leads.append(found[0])
                candidates=list({v['url']:v for v in candidates}.values());candidates.sort(key=lambda v:rank(v,plan))
                for url in plan.get('read_urls',[]):
                    try:candidates.insert(0,{'url':public_url(url),'title':url,'content':'','summary':'','kind':'search_summary'})
                    except RuntimeErrorBase:errors.append('public_web_address_required')
                ordered=list({v['url']:v for v in query_leads+candidates}.values());selected=ordered[:4]
                community=[v for v in candidates[4:] if any(k in urlsplit(v['url']).hostname for k in ('miyoushe','hoyolab','nga','tieba','bilibili','reddit','taptap'))]
                if community and selected and not any(any(k in urlsplit(v['url']).hostname for k in ('miyoushe','hoyolab','nga','tieba','bilibili','reddit','taptap')) for v in selected):selected[-1]=community[0]
                for source in selected:
                    if len(source.get('content',''))<350:
                        fetched=call('conversation_fetch',{'url':source['url']}).get('sources',[])
                        if fetched:source=source|fetched[0]
                    source['priority_hint']='参考排序，不代表正确性';sources.append(source)
        sources=list({v['url']:v for v in sources}.values())[:6]
        for source in sources:
            source['content']=source.get('content','')[:18000];source['summary']=source.get('summary','')[:1800]
            source['id']=fingerprint([turn['persona_id'],source['url'],source.get('content'),source.get('summary')])
        organized={}
        if sources:
            if not needed or used_previous and prior['organized'].get('notes') and {v['id'] for v in sources}=={v['id'] for v in previous}:organized=prior['organized']
            else:
                from .knowledge_translation import organize_input
                inputs=organize_input(question,recent,sources,json.loads(turn['model_snapshot']))
                if inputs['sources']:
                    output=call('conversation_organize',inputs)
                    if output.get('notes'):organized={'notes':output['notes'],'task_id':calls[-1],'source_ids':[v['id'] for v in inputs['sources']]}
        record={'persona_id':turn['persona_id'],'channel':turn['channel'],'root_turn_id':turn['id'],'sources':sources,'organized':organized,
            'retrieval':{'needed':needed,'used_previous':used_previous,'task_ids':calls,'errors':errors,'web_decision':decision}}
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('knowledge.conversation.record.'+turn['id'],json.dumps({'revision':0,'data':record},ensure_ascii=False)))
        return {'mode':PROFILE,'state':'ready' if sources else 'unavailable' if needed else 'not_needed',
            'record_id':turn['id'],'record_hash':fingerprint(record),'sources':sources,'organized':organized,'retrieval':record['retrieval']}


def attach(messages,details,result,turn):
    from copy import deepcopy
    details=deepcopy(details)
    remaining=json.loads(turn['model_snapshot'])['max_input_chars']-sum(len(m['content']) for m in messages)
    from .knowledge_translation import context
    body,sources,translation=context(result,remaining)
    if body:
        messages=[dict(m) for m in messages];messages[0]['content']+=body
        if details.get('prompt_assembly'):
            assembly=details['prompt_assembly'];assembly.update(input_characters=sum(len(m['content']) for m in messages),input_snapshot_hash=fingerprint(messages))
            if assembly.get('character_consumption'):assembly['character_consumption']['final_input_hash']=fingerprint(messages)
    meta={k:deepcopy(result[k]) for k in ('mode','state','record_id','record_hash','retrieval') if k in result}
    meta.update(translation=translation,source_ids=[s['id'] for s in sources],sources=[{k:v for k,v in s.items() if k!='content' and k!='summary'} for s in sources],evidence=[])
    details['knowledge']=meta
    return messages,details
