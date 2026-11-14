"""Human-first general retrieval: collect useful material, then let the persona think."""
from datetime import datetime,timedelta,timezone
import ipaddress,json,re
from urllib.parse import urlsplit,urlunsplit
from .archive import fingerprint
from .models import RuntimeErrorBase

PROFILE='conversational_web_v1'
STAGES={'conversation_plan','conversation_search','conversation_fetch','conversation_organize'}

def reasoning_model(model,knowledge=None,*,plan=False):
    if plan or (knowledge or {}).get('mode')==PROFILE and not knowledge.get('translation') and (
            knowledge.get('source_ids') or knowledge.get('retrieval',{}).get('needed')):
        return model|{'_reasoning_effort':'high'}
    return model

PLAN=('你在帮助一个有自己口吻和判断的聊天人物按需找资料。普通聊天不用搜索；涉及外部信息时可搜索任意相关公开网站。'
+ '用户明确要求搜索、上网找或读取链接时应执行，不因你认为是常识就擅自省略。用户明确点名的平台要实际查找，不拿别的平台完全替代。'
 '把问题提炼为简洁的公开查询，不把私人经历、联系方式、账号密钥或人物设定拼进搜索。'
 '已有资料够用就复用。官网、官方社媒、社区、玩家攻略、普通帖子都可以参考；官方也有利益立场，热度也不等于正确，排序不是结论。'
 '资料用于帮助交流，主回复可以结合常识、社区观察、实际复现和自身判断表达观点。争议问题兼顾不同立场，不预设官方必然正确。'
 '只输出工具计划JSON：{"search":true,"queries":["公开查询"],"read_urls":[],"reuse_previous":false,"official_domains":[],"community_domains":[]}。'
 'search=false表示直接交流；最多3条查询/链接。域名仅用于相关结果的参考排序，不限制可访问网站。')

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
    return {'search':bool(value.get('search',value.get('need_search',False))),
        'queries':[q[:128] for q in strings('queries',3)],'read_urls':strings('read_urls',3),
        'reuse_previous':bool(value.get('reuse_previous')),'official_domains':strings('official_domains',8),
        'community_domains':strings('community_domains',8),'format_readable':('search' in value or 'need_search' in value)}

def messages(payload):
    data=payload['knowledge_processing']
    from .knowledge_translation import ORGANIZE
    instruction=PLAN if data['stage']=='conversation_plan' else ORGANIZE if data['stage']=='conversation_organize' else None
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
        if stage=='conversation_plan':return plan_output(raw)
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
            recent=[r[0] for r in db.execute("SELECT e.text FROM turns t JOIN events e ON e.id=t.user_event_id WHERE t.persona_id=? AND t.session_id=? AND t.rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY t.rowid DESC LIMIT 2",(turn['persona_id'],turn['session_id'],turn['id']))]
        sources=[];errors=[];calls=[];needed=False;used_previous=False
        def call(stage,args):
            try:
                result,tid=self.processor._call(turn,stage,args);calls.append(tid);return result
            except RuntimeErrorBase as error:
                if any(part in str(error) for part in ('private_cloud','scope_changed','cancelled','knowledge_stopped')):raise
                errors.append(str(error));return {}
        if reader['enabled'] and turn['channel'] in reader['channels'] and web_policy.get('enabled') and self.processor.policy(turn['persona_id'])['data'].get('enabled'):
            today=datetime.fromisoformat(turn['created_at']).astimezone(timezone(timedelta(hours=8))).date().isoformat()
            plan=call('conversation_plan',{'question':question,'recent_user_questions':list(reversed(recent)),
                'today':today,'available_sources':[{k:s.get(k) for k in ('title','url','summary','date')} for s in previous]})
            if not plan.get('format_readable'):errors.append('planner_format_unavailable')
            needed=bool(plan.get('search') or plan.get('read_urls') or plan.get('reuse_previous') or errors)
            if plan.get('reuse_previous'):sources.extend(previous);used_previous=bool(previous)
            candidates=[];query_leads=[]
            if plan.get('search'):
                for query in plan.get('queries',[]):
                    found=call('conversation_search',{'query':query}).get('sources',[])
                    candidates.extend(found)
                    if found:query_leads.append(found[0])
            candidates=list({s['url']:s for s in candidates}.values());candidates.sort(key=lambda s:rank(s,plan))
            for url in plan.get('read_urls',[]):
                try:candidates.insert(0,{'url':public_url(url),'title':url,'content':'','summary':'','kind':'search_summary'})
                except RuntimeErrorBase:errors.append('public_web_address_required')
            # Preserve a lead from every query angle before filling by soft
            # preference. One official-heavy query must not erase community work.
            ordered=list({v['url']:v for v in query_leads+candidates}.values())
            selected=ordered[:4]
            community=[v for v in candidates[4:] if any(k in urlsplit(v['url']).hostname for k in ('miyoushe','hoyolab','nga','tieba','bilibili','reddit','taptap'))]
            if community and selected and not any(any(k in urlsplit(v['url']).hostname for k in ('miyoushe','hoyolab','nga','tieba','bilibili','reddit','taptap')) for v in selected):selected[-1]=community[0]
            for source in selected:
                if len(source.get('content',''))<350:
                    fetched=call('conversation_fetch',{'url':source['url']}).get('sources',[])
                    if fetched:source=source|fetched[0]
                source['priority_hint']='参考排序，不代表正确性';sources.append(source)
        sources=list({s['url']:s for s in sources}.values())[:6]
        for source in sources:
            source['content']=source.get('content','')[:18000]
            source['summary']=source.get('summary','')[:1800]
            source['id']=fingerprint([turn['persona_id'],source['url'],source.get('content'),source.get('summary')])
        organized={}
        if sources:
            if used_previous and prior['organized'].get('notes') and {s['id'] for s in sources}=={s['id'] for s in previous}:
                organized=prior['organized']
            else:
                from .knowledge_translation import organize_input
                inputs=organize_input(question,list(reversed(recent)),sources,json.loads(turn['model_snapshot']))
                if inputs['sources']:
                    output=call('conversation_organize',inputs)
                    if output.get('notes'):
                        organized={'notes':output['notes'],'task_id':calls[-1],'source_ids':[s['id'] for s in inputs['sources']]}
        record={'persona_id':turn['persona_id'],'channel':turn['channel'],'root_turn_id':turn['id'],'sources':sources,
            'organized':organized,'retrieval':{'needed':needed,'used_previous':used_previous,'task_ids':calls,'errors':errors}}
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
