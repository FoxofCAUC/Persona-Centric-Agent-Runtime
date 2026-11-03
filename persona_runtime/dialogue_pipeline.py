"""Opt-in artificial-only pipeline; released policies and legacy renderers stay default."""
from copy import deepcopy
import json
from pathlib import Path

from .archive import encoded, fingerprint, read_exact, binding, turn_scope
from .dialogue_context import (SharedEvidence, EVIDENCE_VERSION, UNDERSTANDING_VERSION,
    require, route, joint_messages, parse_joint, projection, decision_input)
from .context_m4 import collect_context_materials, build_context
from .turn_input import load_turn_input
from .character_context import select_context, render_parts
from .character_selector_protocol import apply_selection
from .character_runtime import validate_snapshot
from .generation_retry import audited_complete

VERSION = 'cr2-shared-context-v1'
FROZEN = ('profile_snapshot','character_snapshot','context_policy_snapshot','model_snapshot',
          'selector_model_snapshot','observation_model_snapshot','expression_snapshot','reply_mode_snapshot')
HOME = Path('D:/Persona-Centric Agent Runtime')
OUT = HOME / 'exports/acceptance/PCAR-dialogue-refactor/CR2-shared-context'


def isolation(engine, attachment):
    home = engine.store.paths.home.resolve()
    require(home.is_relative_to(OUT.resolve()) or home.is_relative_to((HOME/'tmp').resolve()), 'cr2_isolation_required')
    require(home != OUT.resolve(), 'cr2_isolation_required')
    marker = home/'artificial-cr2.json'
    require(marker.is_file() and json.loads(marker.read_text('utf-8')) == attachment, 'cr2_attachment_mismatch')
    require(attachment['version'] == VERSION and attachment['private'] is False, 'cr2_attachment_invalid')


def current(engine, tid, attachment, conn=None):
    import hashlib
    isolation(engine, attachment)
    if conn is None:
        with engine.store.connection() as db: return current(engine,tid,attachment,db)
    turn = dict(conn.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
    require(turn['id'] == attachment['turn_id'] and turn['persona_id'] == attachment['persona_id'], 'cr2_target_mismatch')
    require(turn['status'] in ('queued','running') and not turn['cancelled_at'], 'cr2_request_inactive')
    require(turn['execution_revision'] == attachment['execution_revision'], 'cr2_execution_stale')
    require(hashlib.sha256(engine.config.file.read_bytes()).hexdigest() == attachment['config_hash'], 'cr2_config_changed')
    require({k:fingerprint(turn[k]) for k in FROZEN} == attachment['snapshots'], 'cr2_snapshot_changed')
    for name in ('model_snapshot','selector_model_snapshot','observation_model_snapshot'):
        require(not json.loads(turn[name] or '{}').get('encrypted_key'), 'cr2_credentials_forbidden')
    return turn


def material_details(mat):
    return {'history_groups':[{'turn_id':i['group']['turn_id']} for i in mat['history_items']],
            'memories':[{'id':r[3]['id']} for r in mat['memories']],
            'summary_id':mat['summary']['id'] if mat['summary'] else None,
            'state_version':mat['state']['id'] if mat['state_active'] else None}


def source_guard(engine, conn, turn, archive_evidence, details):
    selected = turn_scope(conn,turn)
    for item in (archive_evidence or {}).get('items',[]):
        group = read_exact(conn,selected,{'kind':'turn','id':item['group']['turn_id']})
        require(group['status'] == 'found' and binding(group) == binding(item['group']), 'cr2_source_changed')
    validate_snapshot(conn,turn)
    return engine.cognitive._guard(conn,turn,details)


def collect(engine, tid, attachment):
    """Source collection happens before local routing, cloud selection or observation."""
    turn = current(engine,tid,attachment)
    evidence = engine.cognitive.dialogue.evidence(turn)
    with engine.store.connection() as db:
        mat = collect_context_materials(engine,db,turn,evidence)
        mat['covered'] = sorted(mat['covered'])
        guard_details = material_details(mat)
        guard = source_guard(engine,db,turn,evidence,guard_details)
        version = validate_snapshot(db,turn)
        texts = [m['text'] for m in load_turn_input(db,turn)]
        from .behavior import resolve_behavior
        scenarios = [s['act'] for s in resolve_behavior('\n\n'.join(texts))['signals']]
        local = select_context(version,'\n\n'.join(texts),members=texts,scenario=scenarios,example_limit=4)
        required = deepcopy(local); required['parts'] = [p for p in local['parts'] if p['required']]
        reserved = 3000 - len(render_parts(required['parts'])) + 2000
        require(reserved >= 2000, 'cr2_required_character_budget')
        # Reuse the original pruning order, reserving space before selection.
        _, plan = build_context(engine,db,turn,evidence,required,shared_materials=json.dumps(mat,ensure_ascii=False),reserve_chars=reserved)
        keep = {r['turn_id'] for r in plan['history_groups']}
        mat['history_items'] = [i for i in mat['history_items'] if i['group']['turn_id'] in keep]
        mat['history'] = [i['pair'] for i in mat['history_items']]
        mat['history_rows'] = [evidence['orders'][i['group']['turn_id']] for i in mat['history_items']]
        ids = {r['id'] for r in plan['memories']}
        mat['memories'] = [r for r in mat['memories'] if r[3]['id'] in ids]
        count = sum(r['kind']=='summary_item' for r in plan['removed'])
        mat['summary_items'] = mat['summary_items'][count:]
        sources = []
        def add(ident, kind, text, authority, version='1', created_at=''):
            sources.append({'id':ident,'kind':kind,'text':text,'hash':fingerprint(text),'authority':authority,'version':str(version),'created_at':created_at})
        members = load_turn_input(db,turn)
        for m in members: add('current:'+m['id'],'current',m['text'],'user',created_at=m.get('created_at',''))
        for i in mat['history_items']:
            for speaker,key in (('user','members'),('assistant','replies')):
                for m in i['group'][key]: add('history:'+m['id'],'history',m['text'],speaker,created_at=m.get('created_at',''))
        if mat['reference_block']: add('references:accepted','reference',mat['reference_block'],'user',fingerprint(mat['reference_manifest']))
        for name,v in mat['frozen'].items():
            if name=='state' or name=='relationship' and mat['relation_blocked']: continue
            add('profile:'+name,'profile',encoded(v['data']),'accepted_profile',v['id'])
        if mat['state_active']: add('state:'+mat['state']['id'],'state',encoded(mat['state']['data']),'accepted_profile',mat['state']['id'])
        for r in mat['memories']: add('memory:'+r[3]['id'],'memory',r[3]['text'],'reference',r[3]['revision'])
        if mat['summary_items']: add('summary:'+mat['summary']['id'],'lossy_summary',encoded(mat['summary_items']),'lossy',mat['summary']['revision'])
        previous = attachment.get('previous_context')
        open_slots = []
        if previous:
            require(previous['scope'] == turn_scope(db,turn), 'cr2_previous_scope_mismatch')
            catalog = {s['id']:s for s in sources}
            for ref in previous['source_refs']:
                require(ref['id'] in catalog and catalog[ref['id']]['hash'] == ref['hash'], 'cr2_previous_context_stale')
            open_slots = [r['id'] for r in previous['source_refs']] if previous['missing_slots'] else []
        body = {'version':EVIDENCE_VERSION,'turn_id':tid,'execution_revision':turn['execution_revision'],
                'scope':turn_scope(db,turn),'accepted':attachment['snapshots'],'sources':sources,
                'current':['current:'+m['id'] for m in members],
                'references':['references:accepted'] if mat['reference_block'] else [],'open_slots':open_slots,
                'previous_missing_slots':previous['missing_slots'] if previous else [],
                'excluded':plan['removed'], 'source_guard':guard,
                'budget':{'version':'dialogue-budget-v1','total_characters':json.loads(turn['model_snapshot'])['max_input_chars'],
                          'character_reserve':3000,'projection_reserve':2000,'history_limit':6000,'example_limit':600}}
    return {'shared':SharedEvidence(encoded(body)), 'materials_json':json.dumps(mat,ensure_ascii=False),'archive_evidence':evidence,
            'source_guard':guard,'guard_details':guard_details,'character_version':version,'local':local,
            'members':texts,'scenarios':scenarios,'budget':body['budget']}


class DialoguePipeline:
    def __init__(self, attachment, pool, save_audit=None, *, expected_guard=None):
        self.attachment_json = encoded(attachment)
        self.pool_json = encoded(pool)
        self.save_audit = save_audit or (lambda _:None)
        self.record = None
        self.expected_guard = expected_guard

    @property
    def attachment(self): return json.loads(self.attachment_json)

    def recheck(self, engine, conn, turn, collected):
        current(engine,turn['id'],self.attachment,conn)
        require(source_guard(engine,conn,turn,collected['archive_evidence'],collected['guard_details']) == collected['source_guard'], 'cr2_source_changed')

    async def prepare(self, engine, tid):
        import asyncio
        c = await asyncio.to_thread(collect,engine,tid,self.attachment)
        require(self.expected_guard is None or c['source_guard'] == self.expected_guard, 'cr2_prepared_sources_changed')
        shared = c['shared']; routing = route(shared); pool = json.loads(self.pool_json)
        require(fingerprint(pool) == self.attachment['pool_hash'], 'cr2_candidate_changed')
        result = None; context = ''; projection_removed = []
        turn = current(engine,tid,self.attachment)
        if routing['path'] == 'joint':
            model = json.loads(turn['selector_model_snapshot']) | {'timeout':240,'max_tokens':2048,
                '_json_output':True,'_disposition_compile':True,'_situation_observation':True}
            payload = joint_messages(shared,pool,4,model['max_input_chars'])
            def before(state):
                with engine.store.connection() as db: self.recheck(engine,db,current(engine,tid,self.attachment,db),c)
            value = await audited_complete(engine.client,model,payload,
                validate=lambda text:parse_joint(text,shared,pool),save=self.save_audit,before_attempt=before)
            result = parse_joint(value[0],shared,pool)
            character = apply_selection(c['character_version'],c['members'],pool,result['selection'],4)
            character.update(requested_mode='joint',effective_mode='joint',degrade_reason=None)
            context, projection_removed = projection(result)
        else:
            character = c['local'] | {'requested_mode':'direct_local','effective_mode':'lexical','degrade_reason':None}
        with engine.store.connection() as db:
            turn = current(engine,tid,self.attachment,db); self.recheck(engine,db,turn,c)
        character = engine.cognitive.characters.prepare(turn,character)
        with engine.store.connection() as db:
            messages,details = build_context(engine,db,turn,c['archive_evidence'],character,
                shared_materials=c['materials_json'],experimental_context=context)
            # No second pruning may silently remove a source used by the helper.
            require(details['history_rows'] == json.loads(c['materials_json'])['history_rows'], 'cr2_budget_plan_changed')
            require(len(render_parts(character['parts'])) <= 3000, 'cr2_character_budget')
            manifest = engine.cognitive._manifest(db,turn,messages,details)
        manifest['_shadow_prepared'] = None
        self.record = {'version':VERSION,'shared':shared.document(),'evidence_hash':shared.digest,'routing':routing,
            'understanding':result,'understanding_status':'completed' if result else 'not_executed',
            'decision_input':decision_input(result) if result else None,'projection':context,'projection_removed':projection_removed,
            'selection_returned':result['selection'] if result else None,
            'character':details['character'],'budget':c['budget'],'truncation':details['removed'],
            'input_hash':fingerprint(messages),'messages':messages,
            'consumers':{'joint_input':shared.digest if result else None,'expression_input':shared.digest,
                         'gap_input':fingerprint(decision_input(result)) if result else None}}
        return {'messages':messages,'details':details,'manifest':manifest,'character_prepared':character,
                'guard':c['source_guard'],'accepted_policy_hash':fingerprint(turn['context_policy_snapshot']),
                '_dialogue_pipeline':True,'_shared_collected':c}

    def finalize(self,engine,conn,turn,prepared):
        self.recheck(engine,conn,turn,prepared['_shared_collected'])
        fresh = engine.cognitive.characters.recheck(conn,turn,prepared['character_prepared'])
        require(fingerprint(fresh) == fingerprint(prepared['character_prepared']), 'cr2_character_changed')
        from .prompt_assembly import fit_manifest
        prepared['manifest'] = fit_manifest(prepared['manifest'])
        require(prepared['manifest']['input_snapshot_hash'] == fingerprint(prepared['messages']), 'cr2_manifest_mismatch')
        return prepared
