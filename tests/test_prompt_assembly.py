"""R0 equivalence, observation bounds and final-dispatch evidence; synthetic only."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from persona_runtime.archive import encoded, fingerprint
from persona_runtime.prompt_assembly import (PromptBlock, assemble, bounded, fit_manifest,
                                            byte_size, DIAGNOSTIC_BYTES, MANIFEST_BYTES)
from tests.prompt_baseline_fixture import capture_all
from tests import test_m762 as fixture


class AssemblyTests(unittest.TestCase):
    def test_budget_prunes_examples_history_memory_and_summary(self):
        from tests.prompt_baseline_fixture import capture
        from persona_runtime import context_m4
        for summary in (False,True):
            result=capture({'limit':1220,'summary':summary},builder=context_m4.build_context)
            self.assertNotIn('error_code',result)
            details=result['details'];removed={r['kind'] for r in details['removed']}
            self.assertTrue({'history','memory'}.issubset(removed))
            self.assertTrue(any(r['kind']=='example' for r in details['character']['removed']))
            if summary:self.assertIn('summary_item',removed)

    def test_frozen_old_requests_parameters_details_and_errors(self):
        saved=json.loads((Path(__file__).parent/'fixtures/prompt-baseline-r0.json').read_text(encoding='utf-8'))
        self.assertEqual(fingerprint(saved['scenarios']),saved['scenarios_hash'])
        actual=capture_all()
        self.assertEqual(set(actual),set(saved['scenarios']))
        for key,value in actual.items():
            with self.subTest(scenario=key):
                self.assertEqual(value,saved['scenarios'][key])
                self.assertEqual(json.dumps(value,ensure_ascii=False).encode('utf-8'),
                                 json.dumps(saved['scenarios'][key],ensure_ascii=False).encode('utf-8'))

    def test_pure_assembly_offsets_whitespace_unicode_and_no_body_in_diagnostic(self):
        blocks=[PromptBlock('a','  人工秘密😀\n','fixture','v1'),PromptBlock('b','\n\r\n尾部  ','fixture','v1')]
        history=[{'role':'user','content':' 不修正 '},{'role':'assistant','content':'一\n二'}]
        current={'role':'user','content':'\t更正'};before=deepcopy((blocks,history,current))
        messages,info=assemble(blocks,history,current)
        self.assertEqual((blocks,history,current),before)
        self.assertEqual(messages,[{'role':'system','content':''.join(b.content for b in blocks)}]+history+[current])
        for row in info['blocks']:
            content=messages[row['message_index']]['content'][row['start_character']:row['start_character']+row['characters']]
            self.assertEqual(row['content_hash'],fingerprint(content))
        self.assertEqual(info['input_snapshot_hash'],fingerprint(messages))
        self.assertNotIn('人工秘密',encoded(info))

    def test_diagnostic_limit_is_utf8_bytes_and_omits_detail(self):
        info={'assembler_version':'v1','input_snapshot_hash':'hash','input_characters':3,'blocks':[{'name':'人'*10000}],
              'source_refs':['中'*9000],'truncation_info':[]}
        result=bounded(info)
        self.assertTrue(result['details_omitted']);self.assertNotIn('blocks',result)
        self.assertLessEqual(byte_size(result),DIAGNOSTIC_BYTES)
        self.assertEqual(result['input_snapshot_hash'],'hash')

    def test_optional_field_never_breaks_original_manifest_limit(self):
        old={'original':'a'*(MANIFEST_BYTES-30)}
        self.assertLessEqual(byte_size(old),MANIFEST_BYTES)
        self.assertEqual(fit_manifest(old|{'prompt_assembly':{'extra':'x'*100}}),old)
        self.assertEqual(fit_manifest({'prompt_assembly':{'small':True}}),{'prompt_assembly':{'small':True}})
        oversized={'original':'x'*MANIFEST_BYTES}
        self.assertEqual(fit_manifest(oversized|{'prompt_assembly':{}}),oversized)


class Integrated(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.M762.asyncSetUp
    asyncTearDown=fixture.M762.asyncTearDown
    source=fixture.M762.source;document=fixture.M762.document;publish=fixture.M762.publish
    admit=fixture.M762.admit;mode=fixture.M762.mode;queued=fixture.M762.queued

    def check(self,prepared):
        manifest=prepared['manifest'];info=manifest['prompt_assembly'];messages=prepared['messages']
        self.assertEqual(info['input_snapshot_hash'],fingerprint(messages))
        self.assertEqual(info['input_snapshot_hash'],manifest['input_snapshot_hash'])
        self.assertEqual(info['input_characters'],sum(len(m['content']) for m in messages))
        self.assertLessEqual(byte_size(info),DIAGNOSTIC_BYTES)
        for row in info.get('blocks',[]):
            text=messages[row['message_index']]['content'][row['start_character']:row['start_character']+row['characters']]
            self.assertEqual(row['content_hash'],fingerprint(text))
        return info

    def raw(self,tid):
        with self.store.connection() as db:return dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())

    def finalize(self,tid,prepared):
        with self.store.connection() as db:return self.engine.cognitive.finalize(db,self.raw(tid),prepared)

    async def test_optional_attach_preserves_old_fields_and_bounded_summary(self):
        from persona_runtime.prompt_assembly import attach
        tid=await self.queued();prepared=self.engine.cognitive.prepare(tid);turn=self.raw(tid)
        with self.store.connection() as db,patch('persona_runtime.cognitive.identifier',return_value='fixed-context'),patch('persona_runtime.cognitive.timestamp',return_value='fixed-time'):
            with patch('persona_runtime.prompt_assembly.attach',side_effect=lambda m,t,d,*args:m):
                old=self.engine.cognitive._manifest(db,turn,prepared['messages'],prepared['details'])
            new=self.engine.cognitive._manifest(db,turn,prepared['messages'],prepared['details'])
        self.assertEqual({k:v for k,v in new.items() if k!='prompt_assembly'},old)
        details=deepcopy(prepared['details']);details['prompt_assembly']['blocks']*=1000
        summary=attach(old,turn,details)['prompt_assembly']
        self.assertTrue(summary['details_omitted']);self.assertLessEqual(byte_size(summary),DIAGNOSTIC_BYTES)
        self.assertEqual(summary['input_snapshot_hash'],old['input_snapshot_hash'])
        old['padding']='';old['padding']='x'*(MANIFEST_BYTES-byte_size(old)-10)
        self.assertEqual(attach(old,turn,prepared['details']),old)

    async def test_baseline_shadow_assist_effective_path_and_fake_client_hash(self):
        self.publish();self.admit()
        for mode in ('baseline','shadow','assist'):
            self.mode(mode);tid=await self.queued();prepared=self.engine.cognitive.prepare(tid)
            info=self.check(prepared);cap=info['effective']['capsule']
            self.assertEqual(cap['requested_mode'],mode);self.assertEqual(cap['applied'],mode=='assist')
            await self.engine._run(tid)
            with self.store.connection() as db:
                saved=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(tid,)).fetchone()[0])
                legacy=json.loads(db.execute('SELECT details_json FROM context_uses WHERE turn_id=?',(tid,)).fetchone()[0])
            self.assertNotIn('prompt_assembly',legacy)
            self.assertEqual(saved['prompt_assembly']['input_snapshot_hash'],fingerprint(self.fake.messages[-1]))
            self.assertNotIn('我想找人倾诉',encoded(saved['prompt_assembly']))

    async def test_withdrawal_rebuilds_capsule_diagnostic(self):
        head=self.publish();self.admit();self.mode('assist');tid=await self.queued()
        prepared=self.engine.cognitive.prepare(tid);before=self.check(prepared)
        self.dispositions.withdraw(self.pid,head['active_id'],head['revision'],'r0-withdraw')
        final=self.finalize(tid,prepared);after=self.check(final)
        self.assertNotEqual(before['input_snapshot_hash'],after['input_snapshot_hash'])
        self.assertFalse(after['effective']['capsule']['applied'])
        self.assertTrue(after['effective']['capsule']['reason'])
        self.assertEqual(next(b for b in after['blocks'] if b['name']=='capsule')['characters'],0)

    async def test_budget_fallback_reports_actual_empty_capsule(self):
        self.publish();self.admit();self.mode('assist');tid=await self.queued()
        before=self.engine.cognitive.prepare(tid)
        with self.store.connection(write=True) as db:
            model=json.loads(self.raw(tid)['model_snapshot']);model['max_input_chars']=sum(len(m['content']) for m in before['messages'])-1
            db.execute('UPDATE turns SET model_snapshot=? WHERE id=?',(json.dumps(model),tid))
        info=self.check(self.engine.cognitive.prepare(tid))
        self.assertFalse(info['effective']['capsule']['applied'])
        self.assertEqual(info['effective']['capsule']['reason'],'capsule_budget_unavailable')

    async def test_v2_snapshot_does_not_claim_all_components_v2(self):
        from persona_runtime.dialogue_voice import freeze
        from persona_runtime.reply_batch import expression_snapshot
        tid=await self.queued()
        with self.store.connection(write=True) as db:
            model=json.loads(self.raw(tid)['model_snapshot']);model['_dialogue_profile']=freeze('r0',['hello'])
            db.execute('UPDATE turns SET model_snapshot=?,reply_mode_snapshot=?,expression_snapshot=? WHERE id=?',
                       (json.dumps(model),'context_structured',json.dumps(expression_snapshot('context_structured',version=1)),tid))
        info=self.check(self.engine.cognitive.prepare(tid))
        self.assertEqual(info['effective']['expression'],'natural-dialogue-v2')
        self.assertEqual(info['effective']['format'],'segments-schema1-v1')
        self.assertFalse(info['effective']['capsule']['applied'])
        with self.store.connection(write=True) as db:db.execute("UPDATE turns SET channel='test' WHERE id=?",(tid,))
        probe=self.check(self.engine.cognitive.prepare(tid))
        self.assertEqual(probe['requested']['dialogue_voice'],'natural-dialogue-v2')
        self.assertEqual(probe['effective']['expression'],'provider-probe')

    async def test_expired_state_and_blocked_relationship_rebuild_final_diagnostic(self):
        from datetime import datetime,timedelta,timezone
        from persona_runtime.profile import append_version
        from persona_runtime.models import LOCAL_USER_ID
        from tests.m71_fixtures import insert
        tid=await self.queued();event=self.raw(tid)['user_event_id'];expiry=datetime.now(timezone.utc)+timedelta(minutes=1)
        with self.store.connection(write=True) as db:
            append_version(db,self.pid,'state',{'text':'人工过期状态','expires_at':expiry.isoformat()})
            relation=append_version(db,self.pid,'relationship',{'description':'人工关系来源'},source_ids=[event])
            profile=json.loads(self.raw(tid)['profile_snapshot']);profile['relationship']=relation
            db.execute('UPDATE turns SET profile_snapshot=? WHERE id=?',(json.dumps(profile),tid))
        prepared=self.engine.cognitive.prepare(tid);before=self.check(prepared)
        with self.store.connection(write=True) as db:
            insert(db,'source_exclusions',dict(event_id=event,persona_id=self.pid,user_id=LOCAL_USER_ID,reason='artificial',created_at=expiry.isoformat()))
        with patch('persona_runtime.profile.datetime',wraps=datetime) as clock:
            clock.now.return_value=expiry+timedelta(seconds=1)
            final=self.finalize(tid,prepared)
        after=self.check(final)
        self.assertNotEqual(before['input_snapshot_hash'],after['input_snapshot_hash'])
        self.assertTrue(after['source_checks']['relationship_requires_review'])
        self.assertIsNone(after['source_checks']['state_version'])
        self.assertNotIn('人工过期状态',encoded(final['messages']))

    async def test_preview_history_and_old_manifest_are_distinct(self):
        tid=await self.queued();await self.engine._run(tid)
        history=self.engine.cognitive.inspect(self.pid,'desktop',turn_id=tid)['manifests']
        with patch.object(self.engine.cognitive,'prepare',side_effect=AssertionError('history must not rebuild')):
            self.assertEqual(self.engine.cognitive.inspect(self.pid,'desktop',turn_id=tid)['manifests'],history)
        count=len(self.fake.calls)
        preview=self.engine.cognitive.preview(self.pid,'desktop',turn_id=tid)
        self.assertFalse(preview['creates_request']);self.assertFalse(preview['manifest']['persisted'])
        self.assertEqual(len(self.fake.calls),count)
        self.assertTrue(history[0]['persisted'])
        # Old rows are immutable: create an old-format synthetic record through existing persistence.
        old={k:v for k,v in history[0].items() if k!='prompt_assembly'};old['context_id']='old-r0'
        with self.store.connection(write=True) as db:self.engine.cognitive.persist(db,old,turn=self.raw(tid))
        saved=self.engine.cognitive.inspect(self.pid,'desktop',turn_id=tid)['manifests'][0]
        self.assertNotIn('prompt_assembly',saved)

    async def test_size_omission_after_shadow_persistence_preserves_old_error(self):
        tid=await self.queued();prepared=self.engine.cognitive.prepare(tid)
        old={k:v for k,v in prepared['manifest'].items() if k not in ('prompt_assembly','_shadow_prepared')}
        old['persisted']=True;old['padding']=''
        old['padding']='x'*(MANIFEST_BYTES-byte_size(old)-20)
        self.assertLessEqual(byte_size(old),MANIFEST_BYTES)
        with self.store.connection(write=True) as db:
            self.engine.cognitive.persist(db,old|{'prompt_assembly':prepared['manifest']['prompt_assembly']},turn=self.raw(tid))
        saved=self.engine.cognitive.inspect(self.pid,'desktop',turn_id=tid)['manifests'][0]
        self.assertNotIn('prompt_assembly',saved)
        old['context_id']='oversized';old['padding']+='x'*100
        with self.assertRaisesRegex(Exception,'context_manifest_too_large'),self.store.connection(write=True) as db:
            self.engine.cognitive.persist(db,old|{'prompt_assembly':{}},turn=self.raw(tid))
