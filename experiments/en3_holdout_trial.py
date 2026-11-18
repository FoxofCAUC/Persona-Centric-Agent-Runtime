"""Run the approved Ling holdout study with separate branch histories and first receipts."""
import argparse
import asyncio
from contextlib import closing, nullcontext
import json
from pathlib import Path
import time
from uuid import uuid4

from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import encoded, fingerprint, outside_repository
from persona_runtime.dialogue_assembly import load_bundle
from persona_runtime.dialogue_generation import GenerationHTTPClient
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.expression_corpus_sources import approval_fields, register_bundle, scope_record
from persona_runtime.instance import InstanceLock
from persona_runtime.models import PersonaSpec
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request
from persona_runtime.provider_policy import require_generation_provider
from persona_runtime.private_personas import guard_cloud_dispatch
from persona_runtime.reply_batch import validate_reply
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from .corpus_reply_study import read_private, save_once
from .en3_holdout_packet import ReplayAssembly, first_request
from .fresh_scene_journal import Journal


BRANCHES = ('A', 'B', 'R')
USE_VERSION = 'en3-ling-author-use-v1'
RESULT_VERSION = 'en3-ling-results-v1'


def require(condition, reason):
    if not condition:
        raise CorpusError('en3_trial_' + reason)


def checksum(path):
    return digest(path.read_bytes())


def use_scope(contract, contract_hash):
    return {'version':USE_VERSION, 'study_id':contract['study_id'], 'input_contract_sha256':contract_hash,
            'materials_sha256':contract['materials_sha256'], 'source_bundle_sha256':contract['source_bundle_sha256'],
            'inputs_html_sha256':contract['inputs_html_sha256'], 'blind_key_sha256':contract['blind_key_sha256'],
            'persona':contract['persona'], 'provider':'deepseek_official', 'split':'final_holdout', 'branches':list(BRANCHES),
            'approved':True, 'candidate_runtime_for_this_study':True, 'own_displayed_history':True,
            'selection_and_first_understanding':True, 'online_retrieval':False, 'formal_activation':False,
            'retries':0}


def validate_use(root, contract, contract_hash, expected_approval=None):
    path = root / 'author-use.private.json'
    require(path.is_file(), 'specific_author_approval_required')
    raw = path.read_bytes()
    use = json.loads(raw)
    scope = use_scope(contract, contract_hash)
    require(all(key in use and encoded(use[key]) == encoded(value) for key, value in scope.items())
            and isinstance(use.get('author_message'), str) and use['author_message'].strip(), 'wrong_author_scope')
    require(expected_approval is None or digest(raw) == expected_approval, 'approval_changed')
    require(checksum(root/'input-contract.private.json') == contract_hash and
            all(checksum(root / file) == contract[field] for file, field in (
                ('inputs.html','inputs_html_sha256'),('materials.private.json','materials_sha256'),
                ('blind-key.private.json','blind_key_sha256'))), 'frozen_inputs_changed')
    require(checksum(outside_repository(contract['source_bundle_path'])) == contract['source_bundle_sha256'],
            'source_bundle_changed')
    return digest(raw)


ROUTES = {'A.body':('main','deepseek-flash'), 'B.body':('main','deepseek-v4-pro'),
          'R.understanding':('dialogue_understanding','deepseek-flash'),
          'R.selection':('dialogue_selection','deepseek-flash'), 'R.body':('dialogue_reply','deepseek-v4-pro')}


class CandidateAssembly(ReplayAssembly):
    """Install the study's approved candidate authority on its own isolated home."""
    runtime_bound = False

    def bind_runtime(self, contract, contract_hash, approval_hash):
        store = Store(RuntimePaths(self.root / 'candidate-runtime'))
        store.initialize()
        service = RuntimeService(store)
        with store.connection() as db:
            pid = store.current(db, required=False)
        if pid is None:
            pid = service.create_persona(PersonaSpec.from_dict(self.original.snapshot['persona_spec']))['id']
            service.use_persona(pid)
        with store.connection() as db:
            target = scope_record(db, store, pid)
        approval = self.root / 'scoped-bundle-use.private.json'
        anchor = self.root/'research-permits.private.json'
        if anchor.exists():
            permits=read_private(anchor)
            require(permit_files_valid(self.root,permits),'research_permission_withdrawn')
        else:
            require(not (self.root/'calls.sqlite3').exists() and not (self.root.parent/
                    (self.root.name+'-original-ledger.private.json')).exists(), 'original_permissions_required')
        save_once(approval, approval_fields(self.bundle_hash, target))
        self.route_permits={}
        for route,(role,name) in ROUTES.items():
            if route=='R.understanding':
                base=self.bundle['flash_model']['base_url']
            elif route=='R.selection':
                base=self.bundle['selector_model']['base_url']
            else:
                base=self.original.v8_model['base_url']
            expected={'scope':target,'input_contract_sha256':contract_hash,'author_approval_sha256':approval_hash,
                      'study_id':contract['study_id'],'route':route,'permission':role,'model':name,
                      'endpoint':base,'approved':True}
            path=self.root/('permit-'+route+'.private.json')
            self.route_permits[route]=(path,save_once(path,expected))
        permit_hashes={path.name:sha for path,sha in self.route_permits.values()}
        permit_hashes[approval.name]=checksum(approval)
        save_once(anchor,permit_hashes)
        self.binding = register_bundle(store,pid,self.bundle_path,approval_path=approval)
        self.store, self.pid, self.runtime_bound = store, pid, True
        self.guard()

    def guard_route(self, slot, stage, model):
        parts=slot.rsplit('.', 2)
        require(len(parts)==3 and parts[2]==stage,'route_position')
        route=parts[1]+'.'+stage
        require(route in self.route_permits,'route_not_approved')
        path,expected=self.route_permits[route]
        require(path.is_file() and checksum(path)==expected,'research_permission_withdrawn')
        permit=read_private(path)
        require(model['base_url']==permit['endpoint'] and model['name']==permit['model'],'route_model_changed')
        guard_cloud_dispatch(self.store,self.pid,permit['permission'],model)

    def guard(self):
        if not self.runtime_bound:
            return super().guard()
        self.original.guard()
        with self.store.connection() as db:
            self.direct = load_bundle(self.store, db, self.pid, self.binding)


def permit_files_valid(root, permits):
    names={'permit-'+route+'.private.json' for route in ROUTES} | {'scoped-bundle-use.private.json'}
    return set(permits)==names and all((root/name).is_file() and checksum(root/name)==permits[name] for name in names)


class FirstReceipts(Journal):
    """Each physical position owns a separate first response, even for identical wire bytes."""
    def __init__(self,root,binding,*,client,guard,authority_window=nullcontext,guard_route=None):
        self.authority_window=authority_window
        self.guard_route=guard_route
        super().__init__(root,binding,client=client,guard=guard)

    @staticmethod
    def receipt(raw, wire):
        return parse_receipt(raw,wire.get('stream',False))

    @classmethod
    def finish(cls, db, key):
        row = db.execute('SELECT * FROM requests WHERE hash=?', (key,)).fetchone()
        if row['state'] != 'sending':
            return
        if row['raw'] is None:
            Journal.fail(db,key,'unknown','first_response_missing')
            return
        require(digest(row['raw']) == row['raw_hash'], 'response_hash_changed')
        wire = json.loads(row['wire'])
        value = cls.receipt(row['raw'],wire)
        valid = row['http']==200 and value['valid'] and value['model']==wire['model']
        state = 'completed' if valid else 'unknown' if value['incomplete_stream'] and row['http']==200 else 'failed'
        db.execute('UPDATE requests SET ended=?,state=?,result=?,error=? WHERE hash=?',
                   (time.time(),state,encoded(value),None if valid else value.get('error_code') or 'invalid_first_response',key))

    def record(self, slot, stage, model, wire, deps):
        key = fingerprint([slot,wire])
        with closing(self.open()) as db:
            row = db.execute('SELECT * FROM requests WHERE hash=?',(key,)).fetchone()
        value = json.loads(row['result'] or '{}') if row else {}
        ms = json.loads(row['diagnostics'] or '{}').get('elapsed_ms') if row else None
        if ms is None and row and row['ended'] is not None:
            ms = round(max(0,row['ended']-row['started'])*1000,3)
        return {'stage':stage,'state':row['state'] if row else 'blocked','model':model['name'],
                'attempt_id':row['attempt_id'] if row else None, 'dependencies':list(deps),
                'elapsed_ms':ms,'usage':value.get('usage'), 'actual_charge':None,
                'error_code':row['error'] if row else 'study_halted','request_hash':fingerprint(wire),
                'raw_hash':row['raw_hash'] if row else None,
                'content':value.get('body') if row and row['state']=='completed' else None}

    def check_wire(self, stage, model, wire):
        require_generation_provider(model)
        require(wire == build_request(model,wire['messages'])[0] and not wire.get('tools') and
                not model.get('encrypted_key') and wire['model']==model['name'], 'wire_changed')

    async def send(self, slot, stage, pair, deps, credentials):
        model, wire = pair
        self.check_wire(stage,model,wire)
        key = fingerprint([slot,wire])
        dep_string = encoded(deps)
        with self.authority_window(), closing(self.open()) as db:
            self.guard(slot)
            if self.guard_route:self.guard_route(slot,stage,model)
            old = db.execute('SELECT hash,deps FROM uses WHERE slot=?',(slot,)).fetchone()
            if old:
                require(tuple(old)==(key,dep_string),'history_or_request_changed')
                return self.record(slot,stage,model,wire,deps)
            if self.stop:
                return self.record(slot,stage,model,wire,deps)
            require_generation_provider(dict(credentials,name=model['name']))
            live = dict(model,encrypted_key=credentials['encrypted_key'])
            self.client.preflight(live,wire)
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO requests(hash,wire,attempt_id,state,started) VALUES(?,?,?,'sending',?)",
                       (key,encoded(wire),str(uuid4()),time.time()))
            db.execute('INSERT INTO uses VALUES(?,?,?)',(slot,key,dep_string))
            db.commit()

        def save_raw(raw, *, http_status, diagnostics):
            raw = bytes(raw)
            with closing(self.open()) as db:
                db.execute('UPDATE requests SET raw=?,raw_hash=?,http=?,diagnostics=? WHERE hash=? AND raw IS NULL',
                           (raw,digest(raw),http_status,encoded(diagnostics),key))
                db.commit()
        try:
            await self.client.complete(live,wire,save_raw=save_raw)
        except (Exception,asyncio.CancelledError) as error:
            diagnostics = getattr(error,'diagnostics',{}) or {}
            with closing(self.open()) as db:
                if db.execute('SELECT raw FROM requests WHERE hash=?',(key,)).fetchone()[0] is None:
                    unsent = diagnostics.get('request_headers_sent') is False and diagnostics.get('response_headers_received') is False
                    self.fail(db,key,'failed' if unsent else 'unknown','transport_interrupted')
                    db.execute('UPDATE requests SET diagnostics=? WHERE hash=?',(encoded(diagnostics),key))
                db.commit()
            if isinstance(error, asyncio.CancelledError):
                raise
        with closing(self.open()) as db:
            self.finish(db,key)
            row = db.execute('SELECT http,state FROM requests WHERE hash=?',(key,)).fetchone()
            db.commit()
        self.consecutive_failures = self.consecutive_failures+1 if row['state']=='failed' else 0
        self.stop |= row['state']=='unknown' or row['http'] in (401,403,429) or self.consecutive_failures>=3
        with self.authority_window():
            self.guard(slot)
            if self.guard_route:self.guard_route(slot,stage,model)
        return self.record(slot,stage,model,wire,deps)


def empty(reason=None):
    return {'state':'blocked','error_code':reason,'body':None,'segments':[],'stages':[],
            'understanding':None,'selected_ids':[],'raw_body':None,'planned_display_wait_ms':None}


async def position(assembly, journal, auth, scene, number, item, branch, history, lineage, openings):
    result = empty()
    deps = list(lineage)

    async def call(stage, pair):
        if number == 1 and (stage == 'understanding' or branch != 'R'):
            require(first_request(branch,stage,pair)==openings[branch],'opening_request_changed')
        slot = f'{scene}.{number:02d}.{branch}.{stage}'
        current = await journal.send(slot,stage,pair,deps,auth)
        current = dict(current)
        content = current.pop('content')
        result['stages'].append(current)
        if current['state'] != 'completed':
            result.update(state=current['state'],error_code=current['error_code'])
            return None
        deps.append(current['raw_hash'])
        return content

    baseline = await assembly.baseline(scene,number,item,history,branch)
    if branch == 'R':
        material = assembly.candidate_material(scene,number,item,history,baseline)
        thought = await call('understanding', assembly.request('understanding',material))
        if thought is None:return result
        result['understanding'] = thought
        selection = await call('selection', assembly.request('selection',material,thought))
        if selection is None:return result
        try:
            ids = assembly.parse_selection(selection,material)
        except (KeyError,TypeError,ValueError):
            result.update(state='failed',error_code='selection_invalid')
            return result
        result['selected_ids'] = ids
        result['selected_assets'] = [{k:assembly.original.assets[asset_id][k] for k in ('id','situation','style')} for asset_id in ids]
        raw = await call('body',assembly.request('reply',material,thought,ids))
        if raw is None:return result
        result['raw_body'] = raw
        try:
            body = assembly.projection(raw,material)['display_body']
            plan = assembly.segments(body,material)
            segments = [body[p['start']:p['end']] for p in plan]
            require(bool(body.strip()) and ''.join(segments)==body,'display_incomplete')
            delay = sum(p['delay_ms'] for p in plan)
        except (KeyError,ValueError,TypeError):
            result.update(state='failed',error_code='presentation_invalid')
            return result
    else:
        raw = await call('body',baseline)
        if raw is None:return result
        result['raw_body'] = raw
        try:
            model = baseline[0]
            batch = validate_reply(raw,model['_reply_mode'],model.get('_expression_snapshot'))
            require(len(batch.segments)==1,'unexpected_baseline_display_contract')
            body, segments, delay = batch.text, [batch.text], 0
        except ValueError:
            result.update(state='failed',error_code='baseline_presentation_invalid')
            return result
    result.update(state='completed',body=body,segments=segments,planned_display_wait_ms=delay)
    lineage.extend(s['raw_hash'] for s in result['stages'])
    lineage.append(digest(body.encode()))
    history.append((item,body,item['accepted_at']))
    return result


async def perform(assembly,journal,credentials,contract,materials,contract_hash):
    scenes = []
    for scene in materials['scenes']:
        rows, histories, lineage = [], {b:[] for b in BRANCHES}, {b:[] for b in BRANCHES}
        openings = {r['branch']:r for r in contract['opening_requests'][scene['id']]}
        require(set(openings)==set(BRANCHES),'opening_set')
        for number,item in enumerate(scene['turns'],1):
            bodies = {}
            # Rotate branch order between positions while retaining separate committed histories.
            offset = (number-1+len(scenes)) % len(BRANCHES)
            for branch in BRANCHES[offset:]+BRANCHES[:offset]:
                if len(histories[branch])!=number-1:
                    bodies[branch]=empty('prior_turn_unavailable')
                    continue
                bodies[branch]=await position(assembly,journal,credentials,scene['id'],number,item,branch,
                    histories[branch],lineage[branch],openings)
            rows.append({'index':number,'user':item['text'],'bodies':bodies})
        scenes.append({'id':scene['id'],'turns':rows})
    with closing(journal.open()) as db:
        states = {row['state']:row['n'] for row in db.execute('SELECT state,count(*) AS n FROM requests GROUP BY state')}
        breakdown = {}
        for scene in scenes:
            for turn in scene['turns']:
                for branch,outcome in turn['bodies'].items():
                    stages=[s for s in outcome['stages'] if s['attempt_id']]
                    part=breakdown.setdefault(branch,{'requests':0, 'observed_model_ms':0,
                        'unknown_model_waits':0, 'planned_display_ms':0, 'known_tokens':0,
                        'unknown_usage_requests':0, 'actual_charge':None})
                    part['requests']+=len(stages)
                    part['observed_model_ms']+=sum(s['elapsed_ms'] for s in stages if s['elapsed_ms'] is not None)
                    part['unknown_model_waits']+=sum(s['elapsed_ms'] is None for s in stages)
                    part['planned_display_ms']+=outcome['planned_display_wait_ms'] or 0
                    counts=[(s['usage'] or {}).get('total_tokens') for s in stages]
                    part['known_tokens']+=sum(n for n in counts if type(n) is int)
                    part['unknown_usage_requests']+=sum(type(n) is not int for n in counts)
        tokens, unknown = {}, 0
        for row in db.execute('SELECT result FROM requests'):
            usage = json.loads(row[0] or '{}').get('usage')
            if not usage or not isinstance(usage.get('total_tokens'),int):
                unknown += 1
            for k,v in (usage or {}).items():
                if type(v) is int:tokens[k]=tokens.get(k,0)+v
    return {'version':RESULT_VERSION,'study_id':contract['study_id'],'manifest_sha256':contract_hash,
            'input_contract_sha256':contract_hash,'scenes':scenes,
            'accounting':{'requests':sum(states.values()),'states':states,'by_branch':breakdown,'known_token_totals':tokens,
                          'unknown_usage_requests':unknown,'actual_charge':None,'provider':'deepseek_official',
                          'presentation':'local_simulation','study_clock':'frozen_source_turn_time'}}


async def run(root):
    from .en3_holdout_material import verify_materials
    from .en3_holdout_packet import VERSION, assembly_sources
    from .en3_holdout_blind import freeze
    from persona_runtime.config import ConfigStore
    root = outside_repository(root)
    with closing(InstanceLock(root/'run.lock')):
        contract = read_private(root/'input-contract.private.json')
        contract_hash = checksum(root/'input-contract.private.json')
        require(contract['version']==VERSION and outside_repository(contract['root'])==root and
                set(contract['branches'])==set(BRANCHES)
                and contract['split']=='final_holdout' and contract['online_retrieval'] is False,'study_scope')
        approval_hash = validate_use(root,contract,contract_hash)
        materials = read_private(root/'materials.private.json')
        require(len(materials['scenes'])==2 and all(len(s['turns'])==6 for s in materials['scenes'])
                and freeze(root,materials)==contract['blind_key_sha256'],'wrong_positions')
        with CandidateAssembly(contract['state_path'],root,contract['source_bundle_path']) as assembly:
            require(contract['persona']==assembly.original.persona,'wrong_persona')
            state=read_private(contract['state_path'])
            prior=outside_repository(state['fresh_scene_study_root'])/'materials.private.json'
            corpus=DatasetCorpusStore(assembly.original.plan['corpus_root'])

            def guard(slot):
                validate_use(root,contract,contract_hash,approval_hash)
                require(assembly_sources()==contract['assembly_sources'],'assembler_changed')
                assembly.guard()
                verify_materials(corpus,materials,assembly.original.plan,read_private(prior))

            guard(None)
            assembly.bind_runtime(contract,contract_hash,approval_hash)
            guard(None)
            journal=FirstReceipts(root,fingerprint([contract_hash,approval_hash]),client=GenerationHTTPClient(),guard=guard,
                authority_window=lambda:assembly.direct.authority_window(),guard_route=assembly.guard_route)
            config=ConfigStore(RuntimePaths(Path(assembly.original.plan['source_home'])))
            result=await perform(assembly,journal,config.load()['model'],contract,materials,contract_hash)
            guard(None)
            save_once(root/'results.private.json',result)
            return result['accounting']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    print(json.dumps(asyncio.run(run(parser.parse_args().root)),ensure_ascii=False))
