import asyncio
from copy import deepcopy
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.archive import encoded,fingerprint
from persona_runtime.dialogue_context import (SharedEvidence,EVIDENCE_VERSION,route,parse_joint,projection,decision_input,FIELDS)
from persona_runtime.dialogue_pipeline import DialoguePipeline,collect,FROZEN,VERSION
from persona_runtime.config import ConfigStore,DEFAULTS,atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService
from persona_runtime.models import generic_persona,identifier,RuntimeErrorBase
from persona_runtime.character import CharacterService,empty_character
from persona_runtime.chat import ChatEngine
from scripts import dialogue_context as tool
from scripts.dialogue_refactor import close,NoDispatch,offline
from tests.dialogue_context_fixture import materials,evaluation
from tests.dialogue_context_responses import joint


def artificial(case):
    sources=[];ids=[]
    for n,text in enumerate(case['members']):
        ident=f'current:{n}';ids.append(ident)
        sources.append({'id':ident,'text':text,'kind':'current','authority':'user','hash':fingerprint(text)})
    for n,(text,answer) in enumerate(case['history']):
        for speaker,body in [('user',text),('assistant',answer)]:
            sources.append({'id':f'history:{n}:{speaker}','text':body,'kind':'history','authority':speaker,'hash':fingerprint(body)})
    value={'version':EVIDENCE_VERSION,'current':ids,'sources':sources,'references':['REF'] if case['reference'] else [],'open_slots':[]}
    return SharedEvidence(encoded(value))


POOL={'payload':{'roots':{'lore':[],'example':[]},'groups':{},'sources':[]},
      'ranked':{'lore':[],'example':[]},'rejected':[],'removed':[],'fingerprint':fingerprint({})}


class Contracts(unittest.TestCase):
    def test_independent_router_labels_all_scenes(self):
        labels=evaluation()['expected_paths']
        for case in materials()['cases']:self.assertEqual(route(artificial(case))['path'],labels[case['id']],case['id'])

    def test_history_assistant_and_quoted_cues_do_not_trigger(self):
        case={'members':['早上好'], 'history':[('早','先别庆祝，改成明天。')],'reference':False}
        self.assertEqual(route(artificial(case))['path'],'direct')
        case['members']=['这是一段文字：“改成，别给建议”。代码是`先别`。']
        self.assertEqual(route(artificial(case))['path'],'direct')

    def test_evidence_version_hash_and_immutability(self):
        e=artificial(materials()['cases'][0])
        with self.assertRaises(Exception):e.body_json='{}'
        with self.assertRaisesRegex(RuntimeErrorBase,'version_unknown'):SharedEvidence(e.body_json,version='bad')
        body=e.document();body['sources'][0]['text']='changed'
        with self.assertRaisesRegex(RuntimeErrorBase,'hash_mismatch'):SharedEvidence(encoded(body))

    def test_joint_valid_unknown_and_explicit(self):
        for ident in ('S11','S12','S13'):
            case=next(c for c in materials()['cases'] if c['id']==ident);e=artificial(case)
            value=joint(ident,e.document()|{'digest':e.digest})
            self.assertEqual(parse_joint(encoded(value),e,POOL),value)

    def test_joint_rejects_duplicate_keys_cross_scope_and_forged_quote(self):
        e=artificial(materials()['cases'][8]);r=joint('S09',e.document()|{'digest':e.digest})
        for change in ('foreign','quote','digest','extra'):
            bad=deepcopy(r)
            if change=='foreign':bad['frame']['requests'][0]['evidence'][0]['source_id']='another-person'
            if change=='quote':bad['frame']['requests'][0]['evidence'][0]['quote']='不存在的原句'
            if change=='digest':bad['evidence_hash']='x'
            if change=='extra':bad['selection']['example_ids']=['outside']
            with self.assertRaisesRegex(RuntimeErrorBase,'provider_invalid_response'):parse_joint(encoded(bad),e,POOL)
        with self.assertRaises(RuntimeErrorBase):parse_joint('{"schema_version":1,"schema_version":1}',e,POOL)

    def test_semantically_bad_legal_output_retained(self):
        e=artificial(materials()['cases'][8]);r=joint('S09',e.document()|{'digest':e.digest})
        r['frame']['requests'][0]['text']='故意误判：仍为周二两小时。'
        self.assertEqual(parse_joint(encoded(r),e,POOL),r)

    def test_assistant_guess_cannot_be_explicit_user_fact(self):
        e=artificial(materials()['cases'][10]);r=joint('S11',e.document()|{'digest':e.digest})
        source=next(s for s in e.document()['sources'] if s['authority']=='assistant')
        r['frame']['progress'][0]['evidence']=[{'source_id':source['id'],'quote':source['text']}]
        with self.assertRaises(RuntimeErrorBase):parse_joint(encoded(r),e,POOL)

    def test_projection_essential_budget_and_optional_trimming(self):
        frame={k:[] for k in FIELDS};r={'frame':frame,'gap_status':'unknown'}
        frame['uncertainty']=[{'text':'可选解释'*100}]
        text,removed=projection(r,maximum=180)
        self.assertLessEqual(len(text),180);self.assertTrue(removed)
        frame['willingness']=[{'text':'必要意愿'*100}]
        with self.assertRaisesRegex(RuntimeErrorBase,'projection_budget'):projection(r,maximum=180)

    def test_concrete_missing_value_is_preserved(self):
        frame={k:[] for k in FIELDS};frame['missing_slots']=[{'text':'横梁厚度','certainty':'explicit','evidence':[]}]
        r={'frame':frame,'gap_status':'needed','evidence_hash':'source'}
        self.assertEqual(decision_input(r)['missing_slots'][0]['text'],'横梁厚度')


class Runtime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration=100
        self.tmp=tempfile.TemporaryDirectory(dir=tool.HOME/'tmp',prefix='cr2-test-');self.root=Path(self.tmp.name)
        self.engine=None

    async def asyncTearDown(self):
        import os
        folder=os.environ.get('CR2_TEST_EVIDENCE_DIR')
        if folder and self.engine:
            out=Path(folder);out.mkdir(parents=True,exist_ok=True)
            with self.engine.store.connection() as db:
                turns=[dict(r) for r in db.execute("SELECT id,status,error_code FROM turns WHERE id NOT LIKE 'cr2-%'")]
                active=db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
            tool.write_once(out/(self._testMethodName+'.json'),{'unit_test':self.id(),'simulation':True,'real_calls':0,
                'turns':turns,'counts':self.engine.client.counts,'ledger':self.engine.client.ledger.rows,'active_reservations':active})
        if self.engine:close(self.engine)
        self.tmp.cleanup()

    async def make(self,ident='S11',fault=None,with_example=False):
        case=deepcopy(next(c for c in materials()['cases'] if c['id']==ident))
        store=Store(RuntimePaths(self.root/'runtime'));store.initialize()
        models={k:deepcopy(DEFAULTS[k]) for k in ('model','character_selector')}
        for model in models.values():model.update(base_url='https://example.invalid/v1',name='grok-4.7',encrypted_key='')
        models['model'].update(max_tokens=8192,max_input_chars=24000,timeout=180,hourly_limit=0)
        models['character_selector'].update(enabled=True,timeout=240)
        cfg=tool.configure(store.paths,models,case)
        service=RuntimeService(store);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
        chars=CharacterService(store);data=empty_character();data['identity']['species']='人类'
        pool=deepcopy(POOL)
        if with_example:
            data['examples']=[{'id':'e1','scenario':'局部进展','keywords':['局部'],
                'text':'先留住已经确认的那一小段，别急着替整件事盖章。','enabled':True}]
            pool['payload']['roots']['example']=['e1'];pool['ranked']['example']=[{'item_id':'e1'}]
        chars.update(pid,data,chars.current(pid)['id'],identifier())
        attachment=await tool.admit(store,cfg,pid,case,'process');attachment['pool_hash']=fingerprint(pool)
        tool.write_once(store.paths.home/'artificial-cr2.json',attachment)
        ledger=tool.Ledger(self.root/'receipts');client=tool.MockClient(ident,ident,'automatic',ledger,fault)
        pipeline=DialoguePipeline(attachment,pool,ledger.audit)
        engine=ChatEngine(store,cfg,client,dialogue_pipeline=pipeline,generation_audit_sink=ledger.audit)
        self.engine=engine
        return engine,attachment,pipeline,client,ledger

    async def test_ordinary_direct_one_call_and_native_persistence(self):
        e,a,p,c,l=await self.make('S01');await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'],'completed');self.assertEqual(c.counts,{'main':1})
        self.assertEqual(p.record['understanding_status'],'not_executed')
        self.assertEqual(l.rows[0]['status'],'completed')

    async def test_shared_snapshot_preserves_legacy_serialization_order(self):
        e,a,p,c,l=await self.make('S01')
        actual=await p.prepare(e,a['turn_id'])
        old=e.cognitive.prepare(a['turn_id'])
        self.assertEqual(actual['messages'],old['messages'])

    async def test_joint_selected_example_reaches_actual_main_request(self):
        e,a,p,c,l=await self.make('S13',with_example=True)
        with patch('tests.dialogue_context_responses.selection',return_value={'schema_version':1,'lore_ids':[],'example_ids':['e1']}):
            await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'],'completed')
        self.assertEqual(p.record['selection_returned']['example_ids'],['e1'])
        self.assertIn('e1',p.record['character']['example_ids'])
        self.assertIn('先留住已经确认的那一小段',c.received[-1]['messages'][0]['content'])

    async def test_history_reaches_actual_joint_and_main_and_gap_consumer(self):
        e,a,p,c,l=await self.make();await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'],'completed',e.get(a['turn_id']).get('error_code'))
        self.assertEqual(c.counts,{'joint':1,'main':1})
        self.assertIn('18厘米',c.received[0]['messages'][1]['content'])
        self.assertIn('18厘米',p.record['projection']);self.assertIn('已提供',p.record['decision_input']['progress'][0]['text'])
        self.assertNotIn('selection',c.counts)

    async def test_aux_success_main_transient_retry_does_not_replay_aux(self):
        def fault(kind,n,messages):
            if kind=='main' and n==1:raise RuntimeErrorBase('provider_timeout')
        e,a,p,c,l=await self.make(fault=fault)
        with patch('persona_runtime.generation_retry.retry_delay',new=instant):await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'],'completed',e.get(a['turn_id']).get('error_code'))
        self.assertEqual(c.counts,{'joint':1,'main':2});self.assertEqual([r['status'] for r in l.rows],['completed','failed','completed'])

    async def test_aux_three_retries_then_abort(self):
        def fault(*_):raise RuntimeErrorBase('provider_timeout')
        e,a,p,c,l=await self.make(fault=fault)
        with patch('persona_runtime.generation_retry.retry_delay',new=instant):await e._run(a['turn_id'])
        self.assertEqual(c.counts,{'joint':4});self.assertEqual(e.get(a['turn_id'])['status'],'failed')
        self.assertTrue(all(r['status']=='failed' for r in l.rows))

    async def test_revision_change_invalidates_before_dispatch(self):
        e,a,p,c,l=await self.make()
        with e.store.connection(write=True) as db:db.execute('UPDATE turns SET execution_revision=execution_revision+1 WHERE id=?',(a['turn_id'],))
        await e._run(a['turn_id']);self.assertEqual(c.counts,{})

    async def test_source_withdrawal_after_aux_stops_before_main(self):
        e,a,p,c,l=await self.make()
        def fault(kind,n,messages):
            if kind=='joint':
                with e.store.connection(write=True) as db:
                    from persona_runtime.models import LOCAL_USER_ID
                    db.execute("INSERT INTO source_exclusions VALUES(?,?,?,?,?)",('cr2-S11-process-h0-e1',a['persona_id'],LOCAL_USER_ID,'test','2026-11-02T00:00:00+00:00'))
        c.fault=fault
        await e._run(a['turn_id']);self.assertEqual(c.counts,{'joint':1});self.assertEqual(e.get(a['turn_id'])['status'],'failed')
        self.assertEqual(e.get(a['turn_id'])['error_code'],'cr2_source_changed')

    async def test_invalid_reference_never_dispatches(self):
        e,a,p,c,l=await self.make('S14')
        with e.store.connection(write=True) as db:db.execute("UPDATE events SET text='changed' WHERE id='cr2-S14-process-h0-e1'")
        await e._run(a['turn_id']);self.assertEqual(c.counts,{})

    async def test_mutated_foreword_changes_shared_and_aux_input(self):
        e,a,p,c,l=await self.make();before=collect(e,a['turn_id'],a)['shared']
        with e.store.connection(write=True) as db:db.execute("UPDATE events SET text='长方形布片长20厘米、宽9厘米。' WHERE id='cr2-S11-process-h0-e1'")
        after=collect(e,a['turn_id'],a)['shared'];self.assertNotEqual(before.digest,after.digest)
        self.assertIn('20厘米',after.body_json)

    async def test_reservation_write_failure_does_not_generate(self):
        e,a,p,c,l=await self.make()
        with patch.object(tool,'write_once',side_effect=OSError('injected')):await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'],'failed');self.assertEqual(l.rows,[]);self.assertTrue(l.broken)

    async def test_cancel_stops_and_terminal_is_interrupted(self):
        def fault(*_):raise asyncio.CancelledError()
        e,a,p,c,l=await self.make(fault=fault)
        with self.assertRaises(asyncio.CancelledError):await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'],'interrupted');self.assertEqual(l.rows[0]['status'],'interrupted')

    async def test_no_replay_after_success(self):
        e,a,p,c,l=await self.make('S01');await e._run(a['turn_id']);await e._run(a['turn_id'])
        self.assertEqual(c.counts,{'main':1})

    async def test_changed_config_and_cross_role_marker_are_rejected(self):
        e,a,p,c,l=await self.make()
        cfg=e.config.load();cfg['model']['temperature']=0.7;atomic_json(e.config.file,cfg)
        await e._run(a['turn_id']);self.assertEqual(c.counts,{})
        self.assertEqual(e.get(a['turn_id'])['error_code'],'cr2_config_changed')

    async def test_valid_open_slot_routes_and_stale_binding_rejects(self):
        e,a,p,c,l=await self.make('S07');collected=collect(e,a['turn_id'],a)
        src=next(s for s in collected['shared'].document()['sources'] if s['kind']=='history')
        previous={'scope':collected['shared'].document()['scope'],'source_refs':[{'id':src['id'],'hash':src['hash']}],
                  'missing_slots':[{'name':'票夹规格','status':'unknown'}]}
        updated=a|{'previous_context':previous}
        atomic_json(e.store.paths.home/'artificial-cr2.json',updated)
        new=collect(e,a['turn_id'],updated)['shared'];self.assertEqual(route(new)['path'],'joint')
        updated['previous_context']['source_refs'][0]['hash']='old'
        atomic_json(e.store.paths.home/'artificial-cr2.json',updated)
        with self.assertRaisesRegex(RuntimeErrorBase,'previous_context_stale'):collect(e,a['turn_id'],updated)

    async def test_actual_state_expiry_invalidates_derived_objects(self):
        e,a,p,c,l=await self.make()
        from persona_runtime.profile import append_version
        with e.store.connection(write=True) as db:
            append_version(db,a['persona_id'],'state',{'text':'临时专注中','expires_at':'2099-01-01T00:00:00+00:00'})
        gathered=collect(e,a['turn_id'],a)
        with patch('persona_runtime.cognitive.active_state',return_value=False),e.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(a['turn_id'],)).fetchone())
            with self.assertRaisesRegex(RuntimeErrorBase,'source_changed'):p.recheck(e,db,turn,gathered)

    async def test_input_overflow_before_any_dispatch(self):
        e,a,p,c,l=await self.make()
        with e.store.connection(write=True) as db:
            db.execute('UPDATE events SET text=? WHERE id=(SELECT user_event_id FROM turns WHERE id=?)',('大输入'*10000,a['turn_id']))
        await e._run(a['turn_id']);self.assertEqual(c.counts,{})

    async def test_terminal_write_failure_leaves_unsettled_not_success(self):
        e,a,p,c,l=await self.make('S01');writer=tool.write_once
        def fail_terminal(path,value):
            if '.terminal.' in Path(path).name:raise OSError('terminal failed')
            writer(path,value)
        with patch.object(tool,'write_once',side_effect=fail_terminal):await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'],'failed')
        self.assertTrue(l.broken);self.assertEqual(l.rows[0]['status'],'reserved')

    async def test_removed_frame_leaves_no_projection(self):
        e,a,p,c,l=await self.make()
        prepared=await p.prepare(e,a['turn_id'])
        self.assertIn('当前交流情境',prepared['messages'][0]['content'])
        # Build from the same frozen sources without the projection, not by text replacement.
        from persona_runtime.context_m4 import build_context
        collected=prepared['_shared_collected']
        with e.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(a['turn_id'],)).fetchone())
            messages,_=build_context(e,db,turn,collected['archive_evidence'],prepared['character_prepared'],shared_materials=collected['materials_json'])
        self.assertNotIn('当前交流情境',messages[0]['content'])


class LedgerFailures(unittest.TestCase):
    def test_slow_failure_is_single_physical_time_not_accumulated_retries(self):
        with tempfile.TemporaryDirectory(dir=tool.HOME/'tmp',prefix='cr2-ledger-') as path:
            ledger=tool.Ledger(Path(path));row=ledger.reserve('one',{'name':'grok-4.7','base_url':'https://example.invalid','max_tokens':10},[{'role':'user','content':'人工'}])
            attempt={'id':'a','status':'failed','request_hash':row['request_hash'],'started_epoch':0,'finished_epoch':601}
            with self.assertRaisesRegex(RuntimeErrorBase,'slow_physical_failure'):ledger.audit({'generation_retry':{'attempts':[attempt]}})
            self.assertEqual(ledger.rows[0]['status'],'failed');self.assertTrue(ledger.broken)

    def test_duplicate_and_cross_category_selection_and_budget_pruning(self):
        from persona_runtime.character_selector_protocol import parse_output,apply_selection
        from persona_runtime.character import empty_character
        data=empty_character();data['examples']=[{'id':key,'scenario':'人工','keywords':[],'text':'样'*300,'enabled':True} for key in ('e1','e2')]
        version={'data':data,'id':'version','persona_id':'artificial','revision':1}
        pool=deepcopy(POOL);pool['payload']['roots']['example']=['e1','e2'];pool['ranked']['example']=[{'item_id':key} for key in ('e1','e2')]
        for ids in (['e1','e1'],['lore1'],['outside']):
            with self.assertRaises(Exception):parse_output(encoded({'schema_version':1,'lore_ids':[],'example_ids':ids}),pool,4)
        answer={'schema_version':1,'lore_ids':[],'example_ids':['e1','e2']}
        applied=apply_selection(version,['人工'],pool,answer,4)
        self.assertEqual(applied['selector_returned']['example_ids'],['e1','e2'])
        self.assertEqual(len([p for p in applied['parts'] if p['kind']=='example']),1)


async def instant(*_):pass


class Offline(unittest.TestCase):
    def test_real_client_and_external_network_are_blocked(self):
        with offline() as record:
            with socket.socket() as sock,self.assertRaises(RuntimeError):sock.connect(('203.0.113.1',443))
            async def call():
                from persona_runtime.chat import ChatCompletionsClient
                with self.assertRaises(RuntimeError):await ChatCompletionsClient().complete({},[])
            asyncio.run(call())
        self.assertEqual(record['external_attempts'],1);self.assertEqual(record['http_attempts'],1)

    def test_cli_has_no_real_action(self):
        with patch.object(__import__('sys'),'argv',['dialogue_context.py','execute']),self.assertRaises(SystemExit):tool.main()


if __name__=='__main__':unittest.main()
