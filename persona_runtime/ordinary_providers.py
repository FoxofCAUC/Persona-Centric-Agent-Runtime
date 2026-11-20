"""Opt-in transports for an externally reviewed private-chat assembly."""
import json
import math
import os
from contextlib import closing, nullcontext
from tempfile import NamedTemporaryFile
from hashlib import sha256
from functools import partial
from pathlib import Path
import re

from .archive import encoded
from .chat import ChatCompletionsClient
from .knowledge_routing import configured_model, binding as jev_binding
from .knowledge_web_decision import usage
from .models import RuntimeErrorBase
from .provider_compat import build_request
from .scene_jev import JevTransport


class ReviewedPrivateChatProviders:
    """The invoking candidate must supply the hash of the exact author-reviewed bundle."""
    def __init__(self,config,path,reviewed_sha256,*,writer=None,jev_factory=JevTransport,organizer=None):
        raw=Path(path).read_bytes()
        self.version=sha256(raw).hexdigest()
        if self.version!=reviewed_sha256:
            raise RuntimeErrorBase('private_chat_review_version_changed')
        document=json.loads(raw)
        from .private_reply_output import (
            ADVISORY_VERSIONS, FACT_REVIEW_VERSION, SEARCH_SPEECH_V1, SEARCH_SPEECH_VERSION, SEMANTIC_REVIEW_VERSION, VERSION,
        )
        self.output_contract_version = document.get('output_contract_version')
        if self.output_contract_version not in (None, VERSION, *ADVISORY_VERSIONS):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.output_contract_version and not (
            isinstance(document.get('short_answer_system'), str) and document['short_answer_system'].strip()
        ):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        nodes={'J1','J2.action','J2.stance','J2.task','J3','J6.mode','J6.coverage','J6.ending'}
        # Old reviewed bundles and their hashes remain readable. Retired review
        # material is excluded from the executable assembly.
        if isinstance(document.get('nodes'),dict):
            document['nodes'].pop('J7',None)
        document.pop('notice_review',None)
        self.research=document.get('protocol')=='maibot-private-v3'
        self.grounded_search = document.get('research_context_version') == 'search-evidence-v2'
        from .local_knowledge import VERSION as LOCAL_LOOKUP_VERSION
        local_version=document.get('local_lookup_version')
        from .knowledge_revisions import VERSION as REVISION_VERSION
        from . import knowledge_coverage
        self.structured_study = local_version==knowledge_coverage.VERSION
        dependency_version=document.get('requirement_dependency_version')
        self.conditional_coverage = dependency_version in ('conditional-v1','conditional-v2')
        self.relation_judge = dependency_version=='conditional-v2'
        if self.relation_judge:
            nodes.add('J5.relation')
        if 'requirement_dependency_version' in document and not (self.structured_study and self.conditional_coverage):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        self.knowledge_revisions = local_version==REVISION_VERSION or self.structured_study
        self.local_first = local_version in (LOCAL_LOOKUP_VERSION,'local-first-v2') or self.knowledge_revisions
        self.semantic_lookup = self.local_first
        self.fact_review = self.output_contract_version == FACT_REVIEW_VERSION
        search_speech_version = document.get('search_speech_version')
        self.phase_scoped_search = search_speech_version in (SEARCH_SPEECH_V1, SEARCH_SPEECH_VERSION)
        self.plain_speech_output = search_speech_version == SEARCH_SPEECH_VERSION
        continuation_templates = ('post_search_system', 'post_search_long_system')
        if 'search_speech_version' in document or any(k in document for k in continuation_templates):
            if not (self.phase_scoped_search and self.research and self.grounded_search and self.fact_review
                    and document.get('notice') and all(isinstance(document.get(k), str) and document[k].strip()
                                                      for k in continuation_templates)):
                raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.fact_review and local_version!='local-first-v2' and not self.knowledge_revisions:
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.knowledge_revisions and not self.fact_review:
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if 'local_lookup_version' in document and not (self.local_first and self.grounded_search
                and self.output_contract_version in (SEMANTIC_REVIEW_VERSION, FACT_REVIEW_VERSION)):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if 'research_context_version' in document and not (self.grounded_search and self.research):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.research:
            nodes.update(('J4','J5'))
            if not all(isinstance(document.get(k),str) and document[k].strip()
                       for k in ('notice_system','long_system','search_system')):
                raise RuntimeErrorBase('private_chat_review_incomplete')
        self.styles=document.get('styles',{})
        if not isinstance(self.styles,dict) or any(not isinstance(values,dict) for values in self.styles.values()):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if any(self.styles.values()):
            nodes.add('J2.style')
        if (document.get('protocol') not in ('maibot-private-v2','maibot-private-v3') or set(document.get('nodes',{}))!=nodes
                or not isinstance(document.get('reply_system'),str) or not document['reply_system'].strip()):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        scope=document.get('scope',{})
        if (scope.get('instance')!=config.load()['instance_id'] or not isinstance(scope.get('personas'),list)
                or not scope['personas'] or not all(isinstance(i,str) and i for i in scope['personas'])):
            raise RuntimeErrorBase('private_chat_review_scope_changed')
        self.bundle=document
        self.text_flash_account=document.get('text_flash_account')
        if self.text_flash_account not in (None,'organization'):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        if 'notice' in document and (not self.research or
                                     not self.text_flash_account and document['notice']!=document.get('search')):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        for preset in document['nodes'].values():
            if (not isinstance(preset,dict) or not isinstance(preset.get('instructions'),str)
                    or not preset['instructions'].strip() or not isinstance(preset.get('criteria'),dict)):
                raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.local_first:
            choices=document['nodes']['J4']['criteria']
            if set(choices)!={'retrieve','none'} or not all(isinstance(v,str) and v.strip() for v in choices.values()):
                raise RuntimeErrorBase('private_chat_review_incomplete')
        self.study_enabled='knowledge_study' in document
        if self.study_enabled!=(local_version=='local-first-v2' or self.knowledge_revisions):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.study_enabled:
            study=document['knowledge_study']
            if (not self.local_first or not isinstance(study,dict) or set(study)!={'version','model','instructions'}
                    or study['version']!=(knowledge_coverage.STUDY_VERSION if self.structured_study else 'source-facts-v1')
                    or study['model']!=document.get('notice' if self.text_flash_account else 'search')
                    or not isinstance(study['instructions'],str) or 'json' not in study['instructions'].casefold()):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            self.study_instructions=study['instructions']
            self.organizer=organizer or ChatCompletionsClient()
        if self.structured_study:
            selector=document['nodes']['J5']
            if (set(selector['criteria'])!={'complete','insufficient'} or
                    not all(isinstance(v,str) and v.strip() for v in [*selector['criteria'].values(),selector.get('source_criterion')])):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            if self.conditional_coverage and not (isinstance(selector.get('correction_criterion'),str)
                                                   and selector['correction_criterion'].strip()):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            if self.relation_judge:
                relation=document['nodes']['J5.relation']
                if (set(relation['criteria'])!={'independent'} or not all(isinstance(v,str) and v.strip()
                    for v in (relation['criteria']['independent'],relation.get('source_criterion')))):
                    raise RuntimeErrorBase('private_chat_review_incomplete')
        self.config=config
        self.compatible_writer = document.get('writer_transport')=='openai-chat-completions-v1'
        settings = config.load()
        from .provider_policy import is_deepseek_text_relay,is_private_compatible_writer
        main_target=document.get('main') or {}
        gateway_writer=is_private_compatible_writer(main_target)
        relay_writer=is_deepseek_text_relay(main_target)
        research_account=settings.get('private_research_account','main')
        if 'writer_transport' in document and not (
            self.compatible_writer and self.research and
            (gateway_writer and research_account=='organization' and not self.text_flash_account or
             relay_writer and research_account=='native_web' and self.text_flash_account=='organization')
        ):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        if document.get('search_account','main')!=research_account:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        if self.research:
            self.search_model()
        if self.text_flash_account:
            if not relay_writer or not self.compatible_writer or 'notice' not in document:
                raise RuntimeErrorBase('private_chat_review_recipient_changed')
            self.notice_model()
        self.writer=writer or ChatCompletionsClient(allow_private_compatible_writer=self.compatible_writer)
        self.jev_factory=jev_factory
        self.connections={}

    async def close(self):
        for transport in self.connections.values():
            await transport.close()
        self.connections.clear()

    def check_scope(self,base):
        if base['instance']!=self.bundle['scope']['instance'] or base['persona'] not in self.bundle['scope']['personas']:
            raise RuntimeErrorBase('private_chat_review_scope_changed')
        if self.compatible_writer and not base['consent']['enabled']:
            raise RuntimeErrorBase('private_cloud_provider_not_authorized')
        self._model('jev')
        self._model('main')

    def _model(self,role):
        current=configured_model(self.config.paths) if role=='jev' else self.config.load()['model']
        expected=self.bundle.get(role)
        if expected!={k:current[k] for k in ('base_url','name')}:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        return current

    def bind_choose(self):
        return partial(self._choose,self._model('jev'))

    async def choose(self,node,attempt,state,options):
        return await self.bind_choose()(node,attempt,state,options)

    async def _choose(self,model,node,attempt,state,options):
        if self.knowledge_revisions:
            from .knowledge_revisions import model_material
            state=model_material(state)
        preset=self.bundle['nodes'].get(node)
        if preset is None:
            raise RuntimeErrorBase('private_reply_decision_unavailable')
        fixed=preset['criteria']
        criteria={k:fixed.get(k,preset.get('source_criterion')) for k in options}
        if node=='J5' and getattr(self,'conditional_coverage',False):
            criteria.update((k,preset['correction_criterion']) for k in options if k.startswith('correct/'))
        if (not isinstance(preset.get('instructions'),str) or not preset['instructions'].strip()
                or not all(isinstance(v,str) and v.strip() for v in criteria.values())):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        body={'model':model['name'],'state':state,
              'questions':{node:{'type':'choice','instructions':preset['instructions'],'criteria':criteria}}}
        if len(encoded(body))>model['max_input_chars']:
            raise RuntimeErrorBase('jev_input_budget_exceeded')
        key=jev_binding(model)
        if key not in self.connections:
            self.connections[key]=self.jev_factory(model)
        timing={}
        status,raw=await self.connections[key].complete(body,on_dispatch=lambda _:None,on_receipt=lambda _:None,
                                                        diagnostics=timing.update)
        audit={'model':model['name'],'status':status,'timing':timing,'raw_sha256':sha256(raw.encode()).hexdigest(),
               'options':list(options),'preset_version':self.version}
        try:
            result=json.loads(raw)
        except ValueError:
            return None,audit
        answers=result.get('answers') if isinstance(result,dict) else None
        if status!=200 or not isinstance(answers,dict) or set(answers)!={node} or not isinstance(answers[node],dict):
            return None,audit
        answer=answers[node]
        choice=answer.get('choice')
        weights=answer.get('probabilities',{})
        if (answer.get('type')!='choice' or choice not in options or not isinstance(weights,dict) or set(weights)!=set(options)
                or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in weights.values())
                or not math.isclose(sum(weights.values()),1,abs_tol=.001)):
            return None,audit
        return choice,audit|{'usage':usage(result),'probabilities':weights,'confidence':answer.get('confidence')}

    def notice_model(self):
        if 'notice' not in self.bundle:
            return None
        return self.study_model()

    def study_model(self):
        if not self.text_flash_account:
            return self.search_model()
        settings=self.config.load()
        model=settings['organization']['model']
        from .provider_policy import is_deepseek_text_relay
        if (settings['organization']['reuse_chat'] or model['name']!='deepseek-flash'
                or not is_deepseek_text_relay(model)
                or self.bundle.get('notice')!={k:model[k] for k in ('base_url','name')}):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        return model

    def search_model(self):
        from .deepseek_native_contract import official_endpoint,private_research_model
        model=private_research_model(self.config)
        if self.bundle.get('search_account','main')!=self.config.load().get('private_research_account','main'):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        if self.bundle.get('search')!={k:model[k] for k in ('base_url','name')}:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        official_endpoint(model)
        return model

    def bind_phase_reply(self,phase):
        model=(self.notice_model() if phase=='search_notice' else None) or self._model('main')
        return partial(self._reply,model)

    def bind_reply(self):
        return self.bind_phase_reply('main')

    async def reply(self,attempt,packet):
        return await self.bind_phase_reply(packet.get('phase','main'))(attempt,packet)

    async def _reply(self,model,attempt,packet):
        if self.knowledge_revisions:
            from .knowledge_revisions import model_material
            packet=model_material(packet)
        if self.output_contract_version:
            from .private_reply_output import limits
            if packet.get('output_constraints') != limits(packet['phase'], packet['mode'],
                                                          compact_notice=self.phase_scoped_search):
                raise RuntimeErrorBase('private_reply_output_contract_missing')
        long=packet.get('mode')=='long_detail'
        template='reply_system'
        if self.phase_scoped_search and packet.get('phase')=='search_notice' and (
            set(packet)!={'phase','mode','output_constraints'} or packet['mode']!='short_answer'
        ):
            raise RuntimeErrorBase('private_search_notice_input_invalid')
        if packet.get('phase')=='search_notice' and not long:
            template='notice_system'
        elif self.phase_scoped_search and packet.get('search_notice_presented') is True:
            template='post_search_long_system' if long else 'post_search_system'
        elif long:
            template='long_system'
        elif self.output_contract_version and packet.get('mode')=='short_answer':
            template='short_answer_system'
        if self.plain_speech_output and not long:
            # Plain text already has a single output slot. Field names are only needed for the JSON form.
            packet=packet|{'output_constraints':{key:value for key,value in packet['output_constraints'].items()
                                                if key!='output_fields'}}
        messages=[{'role':'system','content':self.bundle[template]},
                  {'role':'user','content':encoded(packet)}]
        if sum(len(m['content']) for m in messages)>model['max_input_chars']:
            raise RuntimeErrorBase('private_reply_input_budget_exceeded')
        from .provider_policy import is_private_compatible_writer
        if self.compatible_writer and is_private_compatible_writer(model):
            model=model|{'_private_writer_stream':True}
        request,_=build_request(model,messages)
        if request.get('tools') or request.get('tool_choice'):
            raise RuntimeErrorBase('private_reply_tools_forbidden')
        response=await self.writer.complete(model,messages)
        raw,usage=response
        usage=dict(usage or {},requested_model=model['name'])
        echoed=(getattr(response,'diagnostics',None) or {}).get('requested_model_echoed')
        if self.compatible_writer and type(echoed) is bool:
            usage['requested_model_echoed']=echoed
        result=raw,usage
        if not long:return result
        raw,usage=result
        def unique(items):
            obj=dict(items)
            if len(obj)!=len(items):raise ValueError('duplicate_long_reply_field')
            return obj
        try:slots=json.loads(raw,object_pairs_hook=unique)
        except ValueError:slots=raw
        return slots,dict(usage or {},long_receipt_sha256=sha256(raw.encode()).hexdigest())

    async def organize(self,attempt,packet,model):
        from .deepseek_native_contract import MODEL
        search=self.study_model()
        if (not self.study_enabled or model['name']!=MODEL or model['base_url']!=search['base_url']
                or model.get('encrypted_key')!=search.get('encrypted_key')
                or self.bundle['knowledge_study']['model']!={k:model[k] for k in ('name','base_url')}):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        expanded=model.get('_study_length_retry',False)
        model=search|{'_json_output':True}
        if getattr(self,'structured_study',False):
            from . import knowledge_coverage
            minimum=knowledge_coverage.RETRY_OUTPUT_TOKENS if expanded else knowledge_coverage.OUTPUT_TOKENS
            model['max_tokens']=max(model.get('max_tokens',0),minimum)
        messages=[{'role':'system','content':self.study_instructions},
                  {'role':'user','content':encoded(packet)}]
        if sum(len(m['content']) for m in messages)>model['max_input_chars']:
            raise RuntimeErrorBase('knowledge_study_input_too_large')
        raw,usage=await self.organizer.complete(model,messages)
        return {'raw':raw},usage

    async def retrieve(self,step,attempt,query,model):
        from .deepseek_native_contract import VERSION,MODEL,official_endpoint,OUTPUT_TOKENS
        from .deepseek_native_transport import DeepSeekNativeClient
        from .knowledge_general import IndependentWebTools
        from .knowledge_native import NativeRetrieval
        if step not in ('native','search','fetch'):
            raise RuntimeErrorBase('private_lookup_invalid_step')
        if not self.research or self.bundle.get('search')!={'base_url':model['base_url'],'name':MODEL}:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        if model.get('encrypted_key')!=self.search_model().get('encrypted_key'):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        official_endpoint(model)
        if step=='native':
            actual=model|{'max_tokens':max(model['max_tokens'],OUTPUT_TOKENS['deep']),
                          '_native_web_profile':{'version':VERSION,'mode':'deep','tools':'search','stream':True}}
            question=query['question']
            if self.grounded_search and query.get('as_of'):
                question=f"查询日期：{query['as_of']}\n{question}"
            if query.get('url') and query['url'] not in question:
                question=question+'\n'+query['url']
            messages=[{'role':'system','content':self.bundle['search_system']},{'role':'user','content':question}]
            if sum(len(m['content']) for m in messages)>actual['max_input_chars']:
                raise RuntimeErrorBase('native_input_budget_exceeded')
            received=await DeepSeekNativeClient().complete(actual,messages)
            body,usage=received
            page=query.get('url') if self.study_enabled and query.get('intent')=='read_link' else None
            return NativeRetrieval.result_from_receipt(received.native,body,attempt,prefer_text=self.study_enabled,
                    requested_page=page),{'usage':usage,'native':received.native,'transport':received.diagnostics}
        from .knowledge_web_transport import request
        args=query|{'count':8,'need_content':True} if step=='search' else query
        received=await request(step,args,model)
        raw,usage=received
        result=IndependentWebTools.accept({'knowledge_processing':{'stage':'conversation_'+step,'input':args}},raw)
        return result,usage


def registered(config):
    root=config.paths.home/'private/ordinary-reply'
    suspension=root/'retired.json'
    if suspension.is_file():
        from .private_reply_entry import ORDINARY, RESEARCH, ROUTE
        from .storage import Store
        record=json.loads(suspension.read_bytes())
        instance=config.load()['instance_id']
        if record.get('instance')==instance:
            if record.get('route') not in (ROUTE,ORDINARY,RESEARCH):
                raise RuntimeErrorBase('private_reply_route_changed')
            with Store(config.paths).connection(write=True) as db:
                db.execute("INSERT OR IGNORE INTO private_reply_instances VALUES(?,?)",(instance,record['route']))
                db.execute("INSERT OR IGNORE INTO knowledge_control VALUES(?,?)",('private.reply.retired.'+instance,'true'))
            return None
    marker=root/'registration.json'
    if not marker.is_file():
        return None
    grant=json.loads(marker.read_bytes())
    if grant.get('instance')!=config.load()['instance_id']:
        return None
    digest=grant.get('reviewed_sha256','')
    if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
        raise RuntimeErrorBase('private_chat_review_version_changed')
    return ReviewedPrivateChatProviders(config,root/(digest+'.json'),digest)


def _registration_lock(config):
    from .instance import InstanceLock
    return closing(InstanceLock(config.paths.home/'runtime/private-reply-registration.lock'))


def retire(config):
    """Stop candidate dispatch on a closed instance while retaining its frozen routes and history."""
    from .migration import _offline
    from .character_embedding import private_directory
    from .config import atomic_json
    from .private_reply_entry import selected
    from .storage import Store
    with _registration_lock(config), _offline(config.paths), Store(config.paths).connection(write=True) as db:
        instance=config.load()['instance_id']
        route=selected(db,instance)
        if not route:
            raise RuntimeErrorBase('private_reply_route_changed')
        root=private_directory(config.paths.home,config.paths.home/'private/ordinary-reply')
        atomic_json(root/'retired.json',{'instance':instance,'route':route},durable=True)
        db.execute("INSERT OR IGNORE INTO knowledge_control VALUES(?,?)",
                   ('private.reply.retired.'+instance,'true'))


def register(config,path,reviewed_sha256):
    from .migration import _offline
    with _registration_lock(config):
        retired=(config.paths.home/'private/ordinary-reply/retired.json').is_file()
        with _offline(config.paths) if retired else nullcontext():
            _register(config,path,reviewed_sha256)


def _register(config,path,reviewed_sha256):
    from .config import atomic_json
    from .character_embedding import private_directory
    from .private_personas import safe
    providers=ReviewedPrivateChatProviders(config,path,reviewed_sha256)
    providers._model('jev')
    providers._model('main')
    root=private_directory(config.paths.home,config.paths.home/'private/ordinary-reply')
    target=root/(reviewed_sha256+'.json')
    raw=Path(path).read_bytes()
    if sha256(raw).hexdigest()!=reviewed_sha256:
        raise RuntimeErrorBase('private_chat_review_version_changed')
    safe(target,root)
    if not target.exists() or target.read_bytes()!=raw:
        with NamedTemporaryFile(dir=root,delete=False) as output:
            temporary=Path(output.name)
        try:
            with temporary.open('wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
    if target.read_bytes()!=raw:
        raise RuntimeErrorBase('private_chat_review_version_changed')
    atomic_json(root/'registration.json',{'instance':config.load()['instance_id'],'reviewed_sha256':reviewed_sha256},durable=True)
    from .storage import Store
    store=Store(config.paths)
    if (root/'retired.json').is_file() and store.path.is_file():
        store.initialize()
        with store.connection(write=True) as db:
            db.execute("DELETE FROM knowledge_control WHERE key=?",('private.reply.retired.'+config.load()['instance_id'],))
    (root/'retired.json').unlink(missing_ok=True)


if __name__=='__main__':
    import argparse
    from .config import ConfigStore
    from .paths import RuntimePaths
    parser=argparse.ArgumentParser()
    parser.add_argument('--home',required=True,type=Path)
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--bundle',type=Path)
    action.add_argument('--retire',action='store_true')
    parser.add_argument('--reviewed-sha256')
    args=parser.parse_args()
    config=ConfigStore(RuntimePaths(args.home.resolve()))
    if args.retire:
        retire(config)
    elif args.reviewed_sha256:
        register(config,args.bundle,args.reviewed_sha256)
    else:
        parser.error('--bundle requires --reviewed-sha256')
