"""Registered public topics, documents and tools using the current main model."""
import copy
from datetime import datetime,timezone
import hashlib
import ipaddress
import json
import math
import re
import time
from urllib.parse import urlsplit,urlunsplit

from .archive import fingerprint
from .knowledge_service import read,POLICY
from .models import RuntimeErrorBase
from .private_personas import provider_identity

DEFAULT={'enabled':False,'domains':['zh.wikipedia.org','www.wikidata.org'],
         'topics':{},'freshness_seconds':86400,'wikimedia_fallback':True,'discover_public_pages':False}


def primary_model(engine):
    return dict(engine.config.load()['model'])


def host(value):
    if not isinstance(value,str):raise RuntimeErrorBase('web_domain_invalid')
    value=value.lower().encode('idna').decode()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',value) or '.' not in value:
        raise RuntimeErrorBase('web_domain_invalid')
    try:ipaddress.ip_address(value)
    except ValueError:return value
    raise RuntimeErrorBase('web_public_domain_required')


def public_url(value,domains):
    if not isinstance(value,str) or not 1<=len(value)<=8192 or any(ord(c)<32 for c in value):
        raise RuntimeErrorBase('web_url_invalid')
    try:
        parsed=urlsplit(value);port=parsed.port
        if (parsed.scheme not in ('http','https') or parsed.username or parsed.password or parsed.fragment
                or not parsed.hostname or host(parsed.hostname) not in domains
                or port not in (None,80 if parsed.scheme=='http' else 443)):
            raise ValueError()
    except ValueError:raise RuntimeErrorBase('web_url_not_authorized') from None
    return urlunsplit((parsed.scheme,parsed.netloc,parsed.path or '/',parsed.query,''))


def document_identity(row):
    parts=[row['owner_persona'],row['url'],row['sha256'],row.get('published_at')]
    if row.get('provider')=='wikimedia_api':parts=[row['owner_persona'],'wikimedia_api',row['url'],row['sha256'],row['upstream']['revision_id']]
    return fingerprint(parts)


def source_allowed(db,persona_id,channel,url):
    from .knowledge_store import enabled
    domain=urlsplit(url).hostname
    source='wikipedia_zh' if domain in ('zh.wikipedia.org','zh.m.wikipedia.org') else 'wikidata' if domain=='www.wikidata.org' else None
    return source is None or enabled(db,persona_id,channel,source)


def document(db,persona_id,channel,item):
    policy=read(db,'web.policy.'+persona_id,DEFAULT)['data']
    reader=read(db,'policy.'+persona_id,POLICY)['data']
    saved=read(db,'web.document.'+item.get('document_id',''),{})['data']
    withdrawn=read(db,'web.invalidations',{})['data']
    if (not saved or saved['owner_persona']!=persona_id or not policy['enabled']
            or not reader['enabled'] or channel not in reader['channels']
            or saved['domain'] not in policy['domains'] or saved['id'] in withdrawn
            or not source_allowed(db,persona_id,channel,saved['url'])
            or saved['sha256']!=item.get('document_sha256')
            or hashlib.sha256(saved['body'].encode()).hexdigest()!=saved['sha256']
            or time.time()-saved['fetched_epoch']>policy['freshness_seconds']):
        raise RuntimeErrorBase('knowledge_evidence_changed')
    from .persona_management import active
    active(db,persona_id)
    public_url(saved['url'],policy['domains'])
    return saved


def snapshot(db,persona_id=None):
    rows=[]
    for row in db.execute("SELECT value FROM knowledge_control WHERE key LIKE 'knowledge.web.document.%'"):
        value=json.loads(row[0])['data']
        if persona_id is None or value['owner_persona']==persona_id:rows.append(value)
    invalid=read(db,'web.invalidations',{})['data']
    return {'version':2 if any(r.get('provider')=='wikimedia_api' for r in rows) else 1,'documents':rows,'invalidations':{k:v for k,v in invalid.items() if persona_id is None or k in {r['id'] for r in rows}}}


def validate_snapshot(data):
    if not isinstance(data,dict) or set(data)!={'version','documents','invalidations'} or data['version'] not in (1,2) or not isinstance(data['documents'],list) or not isinstance(data['invalidations'],dict):
        raise RuntimeErrorBase('web_snapshot_invalid')
    ids=[row.get('id') for row in data['documents'] if isinstance(row,dict)]
    if len(ids)!=len(data['documents']) or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids) or not set(data['invalidations'])<=set(ids):
        raise RuntimeErrorBase('web_snapshot_invalid')
    for value in data['invalidations'].values():
        if not isinstance(value,dict) or value.get('state') not in ('withdrawn','corrected') or not isinstance(value.get('reason'),str) or len(value['reason'])>500:
            raise RuntimeErrorBase('web_snapshot_invalid')
    for row in data['documents']:
        try:
            if (not isinstance(row['body'],str) or not row['body'] or len(row['body'])>8*1024**2
                    or hashlib.sha256(row['body'].encode()).hexdigest()!=row['sha256']
                    or row['id']!=document_identity(row)
                    or type(row['fetched_epoch']) not in (int,float) or not math.isfinite(row['fetched_epoch'])
                    or not 0<=row['fetched_epoch']<=time.time()+300
                    or not isinstance(row['owner_persona'],str) or not row['owner_persona']):raise ValueError()
            if row.get('published_at') is not None:
                stamp=datetime.fromisoformat(row['published_at'].replace('Z','+00:00'))
                if stamp.tzinfo is None:raise ValueError()
            if row.get('provider')=='wikimedia_api':
                upstream=row['upstream']
                if data['version']!=2 or type(upstream['revision_id']) is not int or upstream['revision_id']<=0 or type(upstream['page_id']) is not int or upstream['page_id']<=0:raise ValueError()
                if upstream.get('revision_at') and datetime.fromisoformat(upstream['revision_at'].replace('Z','+00:00')).tzinfo is None:raise ValueError()
                public_url(row['url'],['zh.wikipedia.org','zh.m.wikipedia.org'])
                if upstream['version_url']!='https://zh.wikipedia.org/w/index.php?oldid='+str(upstream['revision_id']):raise ValueError()
                public_url(upstream['version_url'],['zh.wikipedia.org'])
            elif row.get('provider','deepseek_fetch')!='deepseek_fetch':raise ValueError()
            public_url(row['url'],[row['domain']])
        except (KeyError,ValueError,TypeError,AttributeError):raise RuntimeErrorBase('web_snapshot_invalid') from None
    return data


def merge_snapshot(db,data,imported_persona=None):
    validate_snapshot(data)
    from .knowledge_processing import put
    # These are source versions, never grants or execution/cache records.
    mapping={}
    for row in data['documents']:
        old_id=row['id']
        row=copy.deepcopy(row)
        if imported_persona:
            row['owner_persona']=imported_persona;row['id']=document_identity(row)
        mapping[old_id]=row['id']
        db.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',
            ('knowledge.web.document.'+row['id'],json.dumps({'revision':0,'data':row},ensure_ascii=False)))
    current=read(db,'web.invalidations',{})
    current['data'].update({mapping.get(k,k):v for k,v in data['invalidations'].items()})
    db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               ('knowledge.web.invalidations',json.dumps(current)))


class WebKnowledge:
    def __init__(self,engine,transport=None):
        self.engine,self.store=engine,engine.store
        self.transport=transport

    def policy(self,persona_id):
        self.engine.chat.knowledge.current_policy(persona_id)
        with self.store.connection() as db:return read(db,'web.policy.'+persona_id,DEFAULT)

    def update_policy(self,persona_id,data,expected_revision,operation_id):
        if (not isinstance(data,dict) or set(data)-{'wikimedia_fallback','discover_public_pages'}!=set(DEFAULT)-{'wikimedia_fallback','discover_public_pages'} or not set(data)<=set(DEFAULT) or type(data['enabled']) is not bool or type(data.get('wikimedia_fallback',False)) is not bool or type(data.get('discover_public_pages',False)) is not bool
                or not isinstance(data['domains'],list) or not data['domains'] or len(data['domains'])>30
                or len(set(data['domains']))!=len(data['domains'])
                or not isinstance(data['topics'],dict) or len(data['topics'])>100
                or type(data['freshness_seconds']) is not int or not 60<=data['freshness_seconds']<=2592000):
            raise RuntimeErrorBase('web_policy_invalid')
        if data['enabled'] and not data['topics'] and not data.get('discover_public_pages',False):raise RuntimeErrorBase('web_public_topic_required')
        if data.get('discover_public_pages') and (not data.get('wikimedia_fallback') or 'zh.wikipedia.org' not in data['domains']):raise RuntimeErrorBase('web_discovery_requires_wikipedia')
        for domain in data['domains']:
            if host(domain)!=domain:raise RuntimeErrorBase('web_domain_invalid')
        for key,query in data['topics'].items():
            if (not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',key) or not isinstance(query,str)
                    or not query.strip() or len(query)>128 or any(ord(c)<32 for c in query)):
                raise RuntimeErrorBase('web_topic_invalid')
        if data['enabled']:
            model=primary_model(self.engine);origin=urlsplit(model['base_url'])
            if origin.scheme!='https' or origin.hostname!='api.deepseek.com':raise RuntimeErrorBase('web_requires_official_main_configuration')
            if not model.get('encrypted_key'):raise RuntimeErrorBase('web_main_key_missing')
        from .private_personas import guard_acceptance_write
        guard_acceptance_write(self.store,persona_id)
        self.policy(persona_id)
        def apply(db):
            if data['enabled']:
                processor=self.engine.processing
                old=read(db,'processing.'+persona_id,processor.defaults())
                new=processor.defaults('main')|{'enabled':True,'prompt_profile':old['data'].get('prompt_profile','legacy_v1')}
                db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    ('knowledge.processing.'+persona_id,json.dumps({'revision':old['revision']+1,'data':new})))
        return self.engine.chat.knowledge._update('web.policy.'+persona_id,data,expected_revision,operation_id,apply=apply)

    def invalidate(self,persona_id,document_id,reason,expected_revision,operation_id):
        from .models import require_text
        require_text(reason,'reason')
        service=self.engine.chat.knowledge
        request=[persona_id,document_id,reason]
        saved=service._receipt('web.invalidations',expected_revision,operation_id,request)
        if saved is not None:return saved
        with self.store.connection() as db:
            row=read(db,'web.document.'+document_id,{})['data']
            if not row or row['owner_persona']!=persona_id:raise RuntimeErrorBase('web_document_not_found')
            invalid=read(db,'web.invalidations',{})['data']
        invalid[document_id]={'reason':reason[:500],'state':'withdrawn'}
        return service._update('web.invalidations',invalid,expected_revision,operation_id,request=request)

    def signature(self,db):
        from .knowledge_service import SOURCES
        from .knowledge_store import SOURCE_POLICY
        return fingerprint([read(db,'sources',SOURCES),read(db,'source_policy.wikipedia_zh',SOURCE_POLICY),read(db,'source_policy.wikidata',SOURCE_POLICY)])

    def check_inputs(self,db,turn,inputs):
        policy=read(db,'web.policy.'+turn['persona_id'],DEFAULT)
        if policy!=inputs['web_policy'] or not policy['data']['enabled']:raise RuntimeErrorBase('web_policy_changed')
        if inputs.get('source_signature') and inputs['source_signature']!=self.signature(db):raise RuntimeErrorBase('web_policy_changed')

    def wiki_allowed(self,db,turn,policy):
        from .knowledge_store import source_policy
        return bool(policy['data'].get('wikimedia_fallback',False) and 'zh.wikipedia.org' in policy['data']['domains']
            and source_allowed(db,turn['persona_id'],turn['channel'],'https://zh.wikipedia.org/') and source_policy(db,'wikipedia_zh')['store_body'])

    def discovery_allowed(self,db,turn,policy):
        return bool(policy['data'].get('discover_public_pages',False) and self.wiki_allowed(db,turn,policy))

    def check(self,db,turn,data):
        grant=read(db,'web.policy.'+turn['persona_id'],DEFAULT)
        self.check_inputs(db,turn,data['input'])
        if data['stage'] in ('wiki_search','wiki_fetch') and not self.wiki_allowed(db,turn,grant):raise RuntimeErrorBase('wiki_source_not_authorized')
        if data['stage'] in ('public_page_plan','public_page_review') or 'public_discovery' in data['input']:
            if not self.discovery_allowed(db,turn,grant):raise RuntimeErrorBase('web_discovery_not_authorized')
            from .knowledge_public_pages import check_review,check_fetch
            if data['stage']=='public_page_review':check_review(db,turn,grant,data['input'])
            if 'public_discovery' in data['input']:check_fetch(db,turn,grant,data['input'])
        model=primary_model(self.engine)
        origin=urlsplit(model['base_url'])
        if origin.scheme!='https' or origin.hostname!='api.deepseek.com' or origin.username or origin.password or origin.port not in (None,443):
            raise RuntimeErrorBase('web_requires_official_main_configuration')
        if not model.get('encrypted_key'):raise RuntimeErrorBase('web_main_key_missing')
        if data['stage'] in ('search','wiki_search'):
            topic=data['input'].get('topic_id')
            if topic not in grant['data']['topics']:raise RuntimeErrorBase('web_public_topic_required')
        elif data['stage'] in ('fetch','wiki_fetch'):
            url=public_url(data['input']['url'],grant['data']['domains'])
            if not source_allowed(db,turn['persona_id'],turn['channel'],url):raise RuntimeErrorBase('web_source_disabled')
            if urlsplit(url).hostname in ('zh.wikipedia.org','zh.m.wikipedia.org'):
                from .knowledge_store import source_policy
                if not source_policy(db,'wikipedia_zh')['store_body']:raise RuntimeErrorBase('web_source_storage_disabled')
            if data['stage']=='wiki_fetch':
                from .knowledge_wikimedia import article_title
                if not article_title(url):raise RuntimeErrorBase('wiki_article_not_authorized')

    async def execute(self,model,payload):
        data=payload['knowledge_processing'];params=data['input'];stage=data['stage']
        if stage in ('wiki_search','wiki_fetch'):
            from .knowledge_wikimedia import execute,public_query
            args={'url':params['url']} if stage=='wiki_fetch' else {'query':public_query(params['web_policy']['data']['topics'][params['topic_id']])}
            if stage=='wiki_search' and not args['query']:raise RuntimeErrorBase('wiki_topic_not_authorized')
            if getattr(self,'wikimedia_transport',None):return await self.wikimedia_transport(stage,args,model['timeout'])
            return await execute(stage,args,model['timeout'])
        if stage=='search':
            query=params['web_policy']['data']['topics'][params['topic_id']]
            args={'query':query,'count':5,'need_content':False}
        else:args={'url':public_url(params['url'],params['web_policy']['data']['domains'])}
        if self.transport:return await self.transport(stage,args,model)
        from .knowledge_web_transport import request
        return await request(stage,args,model)

    def accept(self,db,payload,output):
        data=payload['knowledge_processing'];pid=payload['persona_id'];policy=data['input']['web_policy']['data']
        if data['stage'] in ('search','wiki_search'):
            results=[]
            for index,row in enumerate(output['results']):
                try:url=public_url(row['url'],policy['domains'])
                except RuntimeErrorBase:continue
                if not source_allowed(db,pid,payload['channel'],url):continue
                if urlsplit(url).hostname in ('zh.wikipedia.org','zh.m.wikipedia.org'):
                    from .knowledge_wikimedia import article_title
                    if not article_title(url):continue
                results.append(dict(row,id=fingerprint([url,index]),url=url))
            return {'results':results,'reason':'source_policy_filtered' if output['results'] and not results else 'no_results' if not results else None}
        if data['stage']=='wiki_fetch' and output.get('not_found'):return {'not_found':True}
        body=output['content'];url=public_url(data['input']['url'],policy['domains'])
        digest=hashlib.sha256(body.encode()).hexdigest();ident=fingerprint([pid,url,digest,data['input'].get('published_at')])
        row={'id':ident,'owner_persona':pid,'domain':urlsplit(url).hostname,'url':url,
            'title':output['title'] or url,'body':body,'sha256':digest,'published_at':data['input'].get('published_at'),
            'fetched_epoch':time.time(),'license':None,'license_state':'unknown',
            'redirect_verification':'provider_does_not_report_final_url'}
        if data['stage']=='wiki_fetch':
            row['provider']='wikimedia_api';row['upstream']=output['upstream']
            rights=output['upstream'].get('rightsinfo')
            row['license']=rights;row['license_state']='declared' if rights else 'unknown'
            row['redirect_verification']='api_title_and_revision_reported'
            ident=row['id']=document_identity(row)
        invalid_version=read(db,'web.invalidations',{})
        invalid=invalid_version['data']
        if ident in invalid:raise RuntimeErrorBase('web_document_withdrawn')
        # A changed page retires prior answer evidence, without deleting history.
        for old in db.execute("SELECT value FROM knowledge_control WHERE key LIKE 'knowledge.web.document.%'"):
            saved=json.loads(old[0])['data']
            same_page=saved['url']==url or (row.get('provider')=='wikimedia_api' and saved.get('provider')=='wikimedia_api' and saved['upstream']['page_id']==row['upstream']['page_id'])
            same_revision=row.get('provider')=='wikimedia_api' and saved.get('provider')=='wikimedia_api' and saved['upstream']['revision_id']==row['upstream']['revision_id'] and saved['sha256']==digest
            if saved['owner_persona']==pid and same_page and saved['id']!=ident and not same_revision:
                invalid[saved['id']]={'state':'corrected','reason':'new_content_version'}
        db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('knowledge.web.invalidations',json.dumps({'revision':invalid_version['revision']+1,'data':invalid})))
        db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('knowledge.web.document.'+ident,json.dumps({'revision':0,'data':row},ensure_ascii=False)))
        return {'document_id':ident,'sha256':digest}

    def gather(self,processor,turn,question,goal):
        from .knowledge_wikimedia import article_title,public_query
        policy=self.policy(turn['persona_id'])
        if processor.policy(turn['persona_id'])['data'].get('model_source')!='main':raise RuntimeErrorBase('web_main_configuration_required')
        with self.store.connection() as db:
            source_signature=self.signature(db);wiki_allowed=self.wiki_allowed(db,turn,policy)
        common={'web_policy':policy,'source_signature':source_signature}
        selected,_=processor._call(turn,'web_plan',dict(common,question=question,goal=goal,topics=policy['data']['topics'])) if policy['data']['topics'] else ({'topic_ids':[]},None)
        epoch={'bucket':int(time.time()//policy['data']['freshness_seconds']),'current_turn':turn['id'] if goal.get('freshness')=='current' else None}
        candidates=[]
        for topic_id in selected['topic_ids']:
            found,_=processor._call(turn,'search',dict(common,topic_id=topic_id,freshness_epoch=epoch))
            candidates.extend(found['results'])
            if not found['results'] and wiki_allowed and public_query(policy['data']['topics'][topic_id]):
                found,_=processor._call(turn,'wiki_search',dict(common,topic_id=topic_id,freshness_epoch=epoch))
                candidates.extend(found['results'])
        with self.store.connection() as db:
            candidates=[r for r in candidates if source_allowed(db,turn['persona_id'],turn['channel'],r['url'])]
        candidates=list({r['url']:r for r in candidates}.values())
        if not candidates:
            from .knowledge_public_pages import discover
            return discover(self,processor,turn,question,goal,common,epoch)
        sources=[{k:r.get(k) for k in ('id','title','url','summary','snippet','date')} for r in candidates]
        chosen,_=processor._call(turn,'web_fetch_plan',dict(common,question=question,goal=goal,sources=sources))
        documents=[]
        for ident in chosen['fetch_ids']:
            row=next(r for r in candidates if r['id']==ident)
            inputs=dict(common,url=row['url'],published_at=row.get('date'),freshness_epoch=epoch)
            if row.get('provider')=='wikimedia_api':result,_=processor._call(turn,'wiki_fetch',inputs)
            else:
                try:result,_=processor._call(turn,'fetch',inputs)
                except RuntimeErrorBase as error:
                    if str(error) not in ('web_http_403','web_http_404') or not wiki_allowed or not article_title(row['url']):raise
                    result,_=processor._call(turn,'wiki_fetch',inputs)
            if not result.get('not_found'):documents.append(result)
        return self.materials(turn['persona_id'],turn['channel'],documents)

    def materials(self,persona_id,channel,documents):
        materials=[];seen=set()
        with self.store.connection() as db:
            for result in documents:
                item={'kind':'web_document','document_id':result['document_id'],'document_sha256':result['sha256']}
                row=document(db,persona_id,channel,item)
                identity=(row.get('provider'),row.get('upstream',{}).get('page_id'),row.get('upstream',{}).get('revision_id'),row['sha256']) if row.get('provider')=='wikimedia_api' else row['id']
                if identity in seen:continue
                seen.add(identity)
                evidence=item|{'article_id':fingerprint([persona_id,row['url']]),'article_title':row['title'],
                    'section_id':row['id'],'section_title':'网页正文','start':0,'end':len(row['body']),
                    'source_snapshot_at':None,'source_reported_date':row['published_at'],'source_snapshot_kind':'unknown',
                    'fetched_epoch':row['fetched_epoch'],
                    'source_name':row['domain'],'source_origin':'deepseek_fetch','url':row['url'],
                    'claim_kind':'source_claim','time_state':'fresh','license_state':row['license_state']}
                if row.get('provider')=='wikimedia_api':
                    evidence['article_id']=fingerprint([persona_id,'wikipedia_zh',row['upstream']['page_id']])
                    evidence.update(source_origin='wikimedia_api',source_snapshot_at=row['upstream'].get('revision_at'),
                        source_snapshot_kind='revision_time',source_revision_id=row['upstream']['revision_id'],version_url=row['upstream']['version_url'])
                headings=list(re.finditer(r'(?m)^#{1,6} [^\n]+$',row['body']))
                boundaries=[(0,'导语')]+[(m.start(),m[0].lstrip('# ').strip()) for m in headings if m.start()>0]
                for index,(start,title) in enumerate(boundaries):
                    end=boundaries[index+1][0] if index+1<len(boundaries) else len(row['body'])
                    if not row['body'][start:end].strip():continue
                    part=evidence|{'section_id':row['id']+':'+str(start),'section_title':title,'start':start,'end':end}
                    materials.append({'id':fingerprint(part),'text':row['body'][start:end],'evidence':part})
        return materials
