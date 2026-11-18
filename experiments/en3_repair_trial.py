"""Execute only the approved comparison. Every branch retains its own first receipts."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path

from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import encoded, fingerprint, outside_repository
from persona_runtime.dialogue_generation import GenerationHTTPClient
from persona_runtime.expression_corpus_sources import approval_fields, register_bundle, scope_record
from persona_runtime.instance import InstanceLock
from persona_runtime.models import PersonaSpec
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch
from persona_runtime.reply_batch import validate_reply
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from .corpus_reply_study import read_private, save_once
from .en3_holdout_packet import first_request
from .en3_holdout_trial import FirstReceipts, empty
from .en3_repair_packet import (BRANCHES, STUDY, VERSION, ComparisonAssembly, original_regression,
                                 require, source_materials, sources)


RESULT_VERSION='en3-ling-repair-results-v2'
ROUTES={'B.body':('main','deepseek-v4-pro')}
for branch in ('O','R'):
    for stage,role,name in [('understanding','dialogue_understanding','deepseek-flash'),
                            ('selection','dialogue_selection','deepseek-flash'),('body','dialogue_reply','deepseek-v4-pro')]:
        ROUTES[branch+'.'+stage]=(role,name)


def use_scope(c,checksum):
    keys=('study_id','materials_sha256','source_bundle_sha256','repair_bundle_sha256',
          'blind_key_sha256','inputs_html_sha256','prior_feedback_sha256')
    return {**{k:c[k] for k in keys}, 'version':'en3-ling-repair-author-use-v2','approved':True,
            'input_contract_sha256':checksum,'persona':c['persona'],'provider':'deepseek_official',
            'branches':list(BRANCHES),'split':'final_holdout','allow_dynamic_branch_receipts':True,
            'original_regression':c['regression']['source_results_sha256'],
            'online_retrieval':False,'retries':0,'formal_activation':False}


def validate_use(root,contract,checksum,expected=None):
    path=root/'author-use.private.json'
    require(path.is_file(),'specific_author_approval_required')
    raw=path.read_bytes()
    use=json.loads(raw)
    require(all(k in use and encoded(use[k])==encoded(v) for k,v in use_scope(contract,checksum).items())
            and isinstance(use.get('author_message'),str) and bool(use['author_message'].strip()),'wrong_approval')
    require(expected is None or digest(raw)==expected,'approval_changed')
    checks={root/'input-contract.private.json':checksum,root/'materials.private.json':contract['materials_sha256'],
            root/'blind-key.private.json':contract['blind_key_sha256'],root/'inputs.html':contract['inputs_html_sha256'],
            outside_repository(contract['source_bundle_path']):contract['source_bundle_sha256'],
            outside_repository(contract['repair_bundle_path']):contract['repair_bundle_sha256']}
    require(all(p.is_file() and digest(p.read_bytes())==sha for p,sha in checks.items()),'frozen_input_changed')
    return digest(raw)


class LiveComparison(ComparisonAssembly):
    def bind(self,contract,checksum,approval_hash):
        store=Store(RuntimePaths(self.root/'candidate-runtime'))
        store.initialize()
        service=RuntimeService(store)
        with store.connection() as db:
            pid=store.current(db,required=False)
        if pid is None:
            pid=service.create_persona(PersonaSpec.from_dict(self.original.snapshot['persona_spec']))['id']
            service.use_persona(pid)
        with store.connection() as db:
            target=scope_record(db,store,pid)
        names={f'permit-{r}.private.json' for r in ROUTES}|{'approved-old-bundle.private.json','approved-repair-bundle.private.json'}
        anchor=self.root/'research-permits.private.json'
        if anchor.exists():
            old=read_private(anchor)
            require(set(old)==names and all((self.root/name).is_file() and
                    digest((self.root/name).read_bytes())==old[name] for name in names),'withdrawn_permit')
        else:
            require(not (self.root/'calls.sqlite3').exists() and not (self.root.parent/(self.root.name+
                        '-original-ledger.private.json')).exists(),'original_permits_required')
        self.route_permits={}
        approvals={}
        for route,(role,model) in ROUTES.items():
            stage=route.split('.')[1]
            endpoint={'body':self.original.v8_model,'understanding':self.bundle['flash_model'],
                      'selection':self.bundle['selector_model']}[stage]['base_url']
            value={'target':target,'study':STUDY,'contract':checksum,'approval':approval_hash,
                   'route':route,'role':role,'endpoint':endpoint,'model':model}
            path=self.root/f'permit-{route}.private.json'
            approvals[path.name]=save_once(path,value)
            self.route_permits[route]=(path,approvals[path.name])
        ids=[]
        for kind,p,sha in [('old',self.bundle_path,self.bundle_hash),('repair',self.new_bundle_path,self.repair_hash)]:
            grant=self.root/f'approved-{kind}-bundle.private.json'
            approvals[grant.name]=save_once(grant,approval_fields(sha,target))
            ids.append(register_bundle(store,pid,p,approval_path=grant))
        save_once(anchor,approvals)
        self.binding,self.repair_binding=ids
        self.store,self.pid,self.runtime_bound=store,pid,True
        self.guard()

    def guard_route(self,slot,stage,model):
        _,branch,suffix=slot.rsplit('.',2)
        route=branch+'.'+stage
        require(stage==suffix and route in self.route_permits,'route_scope')
        path,sha=self.route_permits[route]
        require(path.is_file() and digest(path.read_bytes())==sha,'withdrawn_permit')
        value=read_private(path)
        require((model['name'],model['base_url'])==(value['model'],value['endpoint']),'route_model_changed')
        guard_cloud_dispatch(self.store,self.pid,value['role'],model)


async def position(assembly,journal,credentials,scene,number,item,branch,history,lineage,recent,opening):
    out=empty()
    out['branch_version']={'B':'companion-runtime-v8','O':'direct-reviewed-assembly-v1',
                           'R':'direct-reviewed-speaker-roles-v2'}[branch]
    deps=list(lineage)
    async def call(stage,pair):
        if number==1 and (stage=='understanding' or branch=='B'):
            require(first_request(branch,stage,pair)==opening,'opening_changed')
        value=await journal.send(f'{scene}.{number:02}.{branch}.{stage}',stage,pair,deps,credentials)
        value=dict(value)
        body=value.pop('content')
        out['stages'].append(value)
        if value['state']!='completed':
            out.update(state=value['state'],error_code=value['error_code'])
            return None
        deps.append(value['raw_hash'])
        return body

    baseline=await assembly.baseline(scene,number,item,history,branch)
    if branch=='B':
        raw=await call('body',baseline)
        if raw is None:return out
        out['raw_body']=raw
        try:
            model=baseline[0]
            batch=validate_reply(raw,model['_reply_mode'],model.get('_expression_snapshot'))
            require(len(batch.segments)==1,'changed_control_display')
            body,segments,delay=batch.text,[batch.text],0
        except ValueError:
            return dict(out,state='failed',error_code='baseline_display_invalid')
    else:
        material=assembly.for_turn(branch,scene,number,item,history,baseline,recent)
        engine=assembly.engine(branch)
        is_repaired=branch=='R'
        pool=(engine.candidate_pool(material) if is_repaired else
              {'eligible_ids':list(assembly.original.assets),'omitted':{}})
        out.update(candidate_pool=pool,understanding_consumers=(['selection'] if is_repaired else ['selection','body']))
        thought=await call('understanding',assembly.wire(branch,'understanding',material))
        if thought is None:return out
        out['understanding']=thought
        if engine.selectable(material):
            chosen=await call('selection',assembly.wire(branch,'selection',material,thought))
            if chosen is None:return out
            try:
                audit=assembly.applied(branch,chosen,material,thought)
            except (ValueError,KeyError,TypeError):
                return dict(out,state='failed',error_code='selection_invalid')
        else:
            audit={'requested_ids':[],'applied_ids':[],'omitted':{}}
            out['understanding_consumers']=[] if is_repaired else ['body']
        ids=audit['applied_ids']
        out.update(selection_audit=audit,selected_ids=ids,
                   selected_assets=[{k:assembly.original.assets[i][k] for k in ('id','situation','style')} for i in ids])
        raw=await call('body',assembly.wire(branch,'reply',material,thought,
                                           ids if is_repaired else audit['requested_ids']))
        if raw is None:return out
        out['raw_body']=raw
        try:
            body=engine.projection(raw,material)['display_body']
            plan=engine.segments(body,material)
            segments=[body[p['start']:p['end']] for p in plan]
            require(body.strip() and ''.join(segments)==body,'display_changed')
            delay=sum(p['delay_ms'] for p in plan)
        except (ValueError,KeyError,TypeError):
            return dict(out,state='failed',error_code='display_invalid')
        recent.append(list(ids))
        del recent[:-2]
    history.append((item,body,item['accepted_at']))
    lineage.extend(x['raw_hash'] for x in out['stages'])
    lineage.append(digest(body.encode()))
    return dict(out,state='completed',body=body,segments=segments,planned_display_wait_ms=delay)


async def run_scene(assembly,journal,credentials,scene,openings,offset,prior=None):
    histories={b:[] for b in BRANCHES}
    lineage={b:[] for b in BRANCHES}
    projected={b:[] for b in BRANCHES}
    branches=tuple(BRANCHES)
    first={v['branch']:v for v in openings}
    rows=[]
    for n,item in enumerate(scene['turns'],1):
        row={'index':n,'user':item['text'],'bodies':{}}
        rotate=(offset+n-1)%len(branches)
        for branch in branches[rotate:]+branches[:rotate]:
            if prior and branch in ('O','B'):
                source='R' if branch=='O' else 'B'
                old=prior['turns'][n-1]
                require(old['index']==n and old['user']==item['text'],'prior_turn_changed')
                result=deepcopy(old['bodies'][source])
                result['reused_from']={'study':'en3-ling-01','scene':'G02','index':n,'branch':source}
            elif len(histories[branch])!=n-1:
                result=empty('prior_turn_unavailable')
            else:
                result=await position(assembly,journal,credentials,scene['id'],n,item,branch,
                                      histories[branch],lineage[branch],projected[branch],first[branch])
            row['bodies'][branch]=result
        rows.append(row)
    return {'id':scene['id'],'title':scene['title'],'turns':rows}


def account(journal,scenes):
    branches={}
    for scene in scenes:
        for turn in scene['turns']:
            for branch,value in turn['bodies'].items():
                total=branches.setdefault(branch,{'requests':0,'known_tokens':0,'observed_model_ms':0,
                    'planned_display_ms':0,'unknown_model_waits':0,'unknown_usage_requests':0,
                    'reused_positions':0,'actual_charge':None})
                if 'reused_from' in value:
                    total['reused_positions']+=1
                    continue
                if value['planned_display_wait_ms'] is not None:
                    total['planned_display_ms']+=value['planned_display_wait_ms']
                for s in value['stages']:
                    if not s['attempt_id']:continue
                    total['requests']+=1
                    if s['elapsed_ms'] is None:total['unknown_model_waits']+=1
                    else:total['observed_model_ms']+=s['elapsed_ms']
                    tokens=(s['usage'] or {}).get('total_tokens')
                    if type(tokens) is int:total['known_tokens']+=tokens
                    else:total['unknown_usage_requests']+=1
    with closing(journal.open()) as db:
        states=dict(db.execute('SELECT state,count(*) FROM requests GROUP BY state'))
        usage=Counter()
        for row in db.execute('SELECT result FROM requests'):
            for k,v in (json.loads(row[0] or '{}').get('usage') or {}).items():
                if type(v) is int:usage[k]+=v
    return {'requests':sum(states.values()),'states':states,'by_branch':branches,
            'known_token_totals':dict(usage),'unknown_usage_requests':sum(b['unknown_usage_requests'] for b in branches.values()),
            'actual_charge':None,'presentation':'local_simulation','study_clock':'frozen_source_turn_time'}


async def perform(assembly,journal,credentials,contract,materials,reg_material,reg_results,contract_hash):
    previous=await run_scene(assembly,journal,credentials,reg_material,[contract['regression']['opening_request']],
                             0,prior=reg_results)
    previous['source_scene_id']='G02'
    scenes=[]
    for i,scene in enumerate(materials['scenes']):
        scenes.append(await run_scene(assembly,journal,credentials,scene,contract['opening_requests'][scene['id']],i))
    return {'version':RESULT_VERSION,'study_id':STUDY,'input_contract_sha256':contract_hash,'scenes':scenes,
            'regression':previous,'accounting':account(journal,[previous,*scenes])}


async def run(root):
    from .en3_repair_blind import freeze
    root=outside_repository(root)
    with closing(InstanceLock(root/'run.lock')):
        path=root/'input-contract.private.json'
        contract=read_private(path)
        sha=digest(path.read_bytes())
        require(contract['version']==VERSION and contract['study_id']==STUDY and outside_repository(contract['root'])==root
                and contract['branches']==BRANCHES and contract['split']=='final_holdout'
                and not contract['online_retrieval'] and not contract['jev_enabled'],'wrong_study')
        approval=validate_use(root,contract,sha)
        material=read_private(root/'materials.private.json')
        require(freeze(root,material,contract['regression']['source_results_sha256'])==contract['blind_key_sha256'],'blind_identity_changed')
        with LiveComparison(contract['state_path'],root,contract['source_bundle_path'],contract['repair_bundle_path']) as assembly:
            require(contract['persona']==assembly.original.persona,'wrong_persona')
            state=read_private(contract['state_path'])
            reg_material,reg_results,reg_hashes=original_regression(state)
            require(all(contract['regression'][k]==v for k,v in reg_hashes.items()),'wrong_regression')
            def guard(slot):
                validate_use(root,contract,sha,approval)
                require(sources()==contract['assembly_sources'],'assembler_changed')
                assembly.guard()
                source_materials(assembly,state,material)
            guard(None)
            assembly.bind(contract,sha,approval)
            guard(None)
            journal=FirstReceipts(root,fingerprint([sha,approval]),client=GenerationHTTPClient(),guard=guard,
                 authority_window=lambda:assembly.direct.authority_window(),guard_route=assembly.guard_route)
            credentials=ConfigStore(RuntimePaths(Path(assembly.original.plan['source_home']))).load()['model']
            result=await perform(assembly,journal,credentials,contract,material,reg_material,reg_results,sha)
            guard(None)
            save_once(root/'results.private.json',result)
            return result['accounting']


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True)
    print(json.dumps(asyncio.run(run(cli.parse_args().root)),ensure_ascii=False))
