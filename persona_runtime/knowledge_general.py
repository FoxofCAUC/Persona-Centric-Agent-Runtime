"""Historical source codecs and bounded independent tools; native retrieval owns orchestration."""
import ipaddress,json,re
from urllib.parse import urlsplit,urlunsplit
from .archive import fingerprint
from .models import RuntimeErrorBase

# Existing conversation records retain their format identifier.
PROFILE='conversational_web_v1'
EXECUTOR='native_two_stage_v1'
STAGES={'conversation_web_decision','conversation_search','conversation_fetch','conversation_native','conversation_search_notice'}
RETIRED_STAGES={'conversation_search_plan','conversation_organize'}
OUTPUT_PROFILE='native-two-stage-output-v1'

def processing_model(model,data,*,config=None):
    stage=data.get('stage')
    if config is not None and stage in ('conversation_native','conversation_search','conversation_fetch'):
        from .provider_policy import is_deepseek_text_relay
        if is_deepseek_text_relay(model):
            settings=config.load()
            if settings.get('private_research_account')=='native_web':
                model=settings['native_web_model']
    if stage in RETIRED_STAGES:raise RuntimeErrorBase('legacy_retrieval_frozen')
    if stage=='conversation_search_notice':
        from .knowledge_search_notice import request_model
        return request_model(model,data['input']['notice_profile'])
    if stage=='conversation_native':
        from .knowledge_native import request_model
        return request_model(model,data['input'])
    return model

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

def messages(payload):
    data=payload['knowledge_processing']
    if data['stage'] in RETIRED_STAGES:raise RuntimeErrorBase('legacy_retrieval_frozen')
    if data['stage']=='conversation_native':
        from .knowledge_native import NativeRetrieval
        return NativeRetrieval.messages(payload)
    return []

def validate_record(db,pid,channel,meta):
    from .knowledge_service import read
    record=read(db,'conversation.record.'+meta.get('record_id',''),{})['data']
    if not record or record.get('persona_id')!=pid or record.get('channel')!=channel :
        raise RuntimeErrorBase('knowledge_scope_changed')
    return record

class IndependentWebTools:
    def __init__(self,processor):self.processor=processor;self.store=processor.store;self.service=processor.service
    def check(self,db,turn,data):
        from .knowledge_service import read,POLICY
        reader=read(db,'policy.'+turn['persona_id'],POLICY)['data']
        if not reader['enabled'] or turn['channel'] not in reader['channels']:raise RuntimeErrorBase('knowledge_permission_changed')
        if data['stage']=='conversation_fetch':public_url(data['input']['url'])
    async def execute(self,model,payload):
        data=payload['knowledge_processing'];args=data['input']
        if data['stage'] not in ('conversation_search','conversation_fetch'):raise RuntimeErrorBase('legacy_retrieval_frozen')
        from .knowledge_web_transport import request
        stage='search' if data['stage']=='conversation_search' else 'fetch'
        values={'query':args['query'][:128],'count':8,'need_content':True} if stage=='search' else {'url':public_url(args['url'])}
        if getattr(self,'transport',None):return await self.transport(stage,values,model)
        if urlsplit(model['base_url']).hostname!='api.deepseek.com':raise RuntimeErrorBase('search_provider_configuration_required')
        return await request(stage,values,model)
    @staticmethod
    def accept(payload,raw):
        stage=payload['knowledge_processing']['stage'];data=_json(raw)
        if stage=='conversation_web_decision':
            from .knowledge_web_decision import parse
            return parse(raw)
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

def attach(messages,details,result,turn):
    from copy import deepcopy
    from .knowledge_handoff import completed_blocks,VERSION as HANDOFF
    from .prompt_assembly import PromptBlock,assemble
    details=deepcopy(details)
    remaining=json.loads(turn['model_snapshot'])['max_input_chars']-sum(len(m['content']) for m in messages)
    blocks=completed_blocks(messages,details,result,turn)
    if blocks is not None:
        remaining+=len(messages[0]['content'])-sum(len(b.content) for b in blocks)
    from .knowledge_translation import context
    body,sources,translation=context(result,remaining,scene_voice=bool(details.get('scene_voice')),completed_handoff=blocks is not None)
    if blocks is not None and body and translation['notes_characters']>0:
        position=next((i for i,b in enumerate(blocks) if b.name=='history_recorded_at'),len(blocks))
        blocks.insert(position,PromptBlock('retrieved_material',body,'knowledge_translation',HANDOFF))
        messages,layout=assemble(blocks,messages[1:-1],messages[-1])
        details['prompt_assembly'].update(layout)
        details['input_characters']=layout['input_characters']
        if details['prompt_assembly'].get('character_consumption'):
            details['prompt_assembly']['character_consumption']['final_input_hash']=layout['input_snapshot_hash']
        translation['handoff_version']=HANDOFF
    elif body:
        messages=[dict(m) for m in messages]
        system = messages[0]['content']
        time_meta = next((b for b in (details.get('prompt_assembly') or {}).get('blocks', [])
                          if b['name']=='history_recorded_at' and b['role']=='system'), None)
        if time_meta:
            offset = time_meta['start_character']
            messages[0]['content'] = system[:offset] + body + system[offset:]
            time_meta['start_character'] += len(body)
        else:
            messages[0]['content'] += body
        if details.get('prompt_assembly'):
            assembly=details['prompt_assembly'];assembly.update(input_characters=sum(len(m['content']) for m in messages),input_snapshot_hash=fingerprint(messages))
            if assembly.get('character_consumption'):assembly['character_consumption']['final_input_hash']=fingerprint(messages)
    meta={k:deepcopy(result[k]) for k in ('mode','state','record_id','record_hash','retrieval') if k in result}
    meta.update(translation=translation,source_ids=[s['id'] for s in sources],sources=[{k:v for k,v in s.items() if k!='content' and k!='summary'} for s in sources],evidence=[])
    details['knowledge']=meta
    return messages,details
