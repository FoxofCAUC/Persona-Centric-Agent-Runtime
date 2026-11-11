"""Managed natural-language knowledge, exact sources and no raw fallback."""
import asyncio
from contextlib import closing
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

from persona_runtime.config import atomic_json
from persona_runtime.knowledge_package import file_digest
from persona_runtime.knowledge_processing import KnowledgeProcessing
from persona_runtime.knowledge_processing_output import parse
from persona_runtime.models import RuntimeErrorBase
from tests import test_knowledge_integration as integration


class Processor:
    def __init__(self):
        self.calls=[];self.reject=False;self.gate=None;self.fail_numbers=False

    async def complete(self, model, messages):
        data=json.loads(messages[-1]['content'])
        self.calls.append(copy.deepcopy(messages))
        if self.gate:await self.gate.wait()
        if 'recent_context' in data:
            result={'format_version':1,'queries':['月球'],'freshness':'snapshot'}
        elif 'sources' in data:
            result={'format_version':1,'section_ids':[s['id'] for s in data['sources'][:1]]}
        elif 'units' in data and '独立核对' in messages[0]['content']:
            result={'format_version':1,'decisions':[{'index':i,'supported':not self.reject,
                'temporal_ok':True,'version_ok':True,'note':'依据本次原文核对。'} for i in range(len(data['units']))]}
        elif 'units' in data:
            result={'format_version':1,'units':data['units'][:3]}
        else:
            units=[]
            for material in data['materials']:
                quote=material['text']
                if '尾部事实' in quote:
                    quote=quote[quote.index('尾部事实'):]
                elif len(quote)>500:continue
                units.append({'text':'整理结论：'+('2002年' if self.fail_numbers else quote),'kind':'fact',
                    'refs':[{'material_id':material['id'],'quote':quote}]})
            result={'format_version':1,'units':units}
        return json.dumps(result,ensure_ascii=False),{'total_tokens':13}


class ProcessingTests(unittest.IsolatedAsyncioTestCase):
    setUp=integration.KnowledgeIntegrationTests.setUp
    ask=integration.KnowledgeIntegrationTests.ask

    def enable(self, body=None):
        integration.KnowledgeIntegrationTests.enable(self)
        self.body=body or '月球是地球唯一的天然卫星。'
        with closing(sqlite3.connect(self.package/'body.db')) as db,db:
            db.execute("ALTER TABLE sections ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            db.execute('UPDATE sections SET content=? WHERE id=11',(self.body,))
        manifest=json.loads((self.package/'manifest.json').read_text(encoding='utf-8'))
        manifest['database']['sha256']=file_digest(self.package/'body.db')
        manifest['source_snapshot_at']=None
        atomic_json(self.package/'manifest.json',manifest)
        self.knowledge.activate_package('fixture',1,'processing-package')
        self.identity=manifest['database']['sha256']
        self.processing=self.org.processing=KnowledgeProcessing(self.org)
        self.processor=self.org.client=Processor()
        policy=self.processing.policy(self.persona['id'])
        self.processing.update_policy(self.persona['id'],policy['data']|{'enabled':True,'prompt_profile':'question_focused_v3'},0,'processing-on')
        self.worker=asyncio.create_task(self.org.worker())

    async def asyncTearDown(self):
        if getattr(self,'processor',None) and self.processor.gate:self.processor.gate.set()
        self.chat.knowledge.close()
        self.org.draining=True
        if getattr(self,'worker',None):
            self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)
        await self.chat.wait()

    async def test_goal_translation_review_and_main_manifest(self):
        self.enable()
        result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.processor.calls),4)
        main=self.capture.calls[0][0]['content']
        self.assertIn('整理结论：月球是地球唯一的天然卫星。',main)
        self.assertNotIn('公共百科参考资料',main)
        self.assertNotIn('"text": "月球',main)
        self.assertIn('processing',result['context']['knowledge'])
        shown=self.knowledge.show_evidence(self.persona['id'],result['id'])
        self.assertEqual(shown['evidence'][0]['quote'],self.body)
        processed=self.processing.show(self.persona['id'],result['id'])
        self.assertEqual(processed['state'],'ready')
        self.assertIn('整理结论',processed['knowledge']['translation'][0]['text'])
        with self.store.connection() as db:
            rows=db.execute("SELECT t.automatic,a.status FROM organization_tasks t JOIN organization_attempts a ON a.task_id=t.id").fetchall()
            self.assertEqual(len(rows),4)
            self.assertTrue(all(r['automatic']==1 and r['status']=='completed' for r in rows))

    async def test_chitchat_skips_all_processing_and_source_followup_reuses(self):
        self.enable()
        plain=await self.ask('晚上好。')
        self.assertEqual(plain['context']['knowledge']['state'],'not_triggered')
        self.assertEqual(self.processor.calls,[])
        first=await self.ask('月球是什么？')
        followup=await self.ask('给我出处。',session_id=first['session_id'])
        self.assertEqual(followup['status'],'completed')
        self.assertEqual(len(self.processor.calls),4)
        self.assertIn('https://zh.wikipedia.org/wiki/月球',self.capture.calls[-1][0]['content'])

    async def test_independent_review_rejects_no_main_or_raw_fallback(self):
        self.enable();self.processor.reject=True
        result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['error_code'],'knowledge_translation_review_rejected')
        self.assertEqual(self.capture.calls,[])
        self.assertEqual(len(self.processor.calls),4)

    async def test_unsupported_number_is_quarantined_before_review(self):
        self.enable();self.processor.fail_numbers=True
        result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.capture.calls,[])
        with self.store.connection() as db:
            row=db.execute("SELECT error_code FROM organization_tasks WHERE status='failed'").fetchone()
            self.assertEqual(row[0],'knowledge_processing_output_invalid')
        self.assertEqual(len(self.processor.calls),3)

    async def test_full_selected_long_section_reaches_tail(self):
        self.enable('长段背景。'*5000+'尾部事实：月球开放时间为09:00。')
        result=await self.ask('月球开放时间是什么？')
        self.assertEqual(result['status'],'completed',result['error_code'])
        shown=self.knowledge.show_evidence(self.persona['id'],result['id'])
        self.assertGreater(shown['evidence'][0]['start'],8000)
        self.assertIn('09:00',self.capture.calls[-1][0]['content'])
        record_id=result['context']['knowledge']['processing']['record_id']
        with self.store.connection() as db:
            from persona_runtime.knowledge_service import read
            chunks=read(db,'processing.record.'+record_id,{})['data']['coverage']['chunks']
        self.assertEqual(chunks[0]['start'],0)
        self.assertEqual(chunks[-1]['end'],len(self.body))
        self.assertTrue(all(a['end']>=b['start'] for a,b in zip(chunks,chunks[1:])))

    async def test_retraction_invalidates_translation_and_its_old_answer(self):
        self.enable()
        first=await self.ask('月球是什么？')
        self.knowledge.invalidate(self.identity,1,'withdrawn','fixture',0,'withdraw-translated')
        self.assertIsNone(self.chat.get(first['id'])['reply'])
        follow=await self.ask('给我出处。',session_id=first['session_id'])
        self.assertEqual(follow['context']['knowledge']['evidence'],[])
        self.assertEqual(len(self.processor.calls),4)

    async def test_reference_budget_coalesces_contiguous_source_windows(self):
        self.enable('甲句。乙句。丙句。丁句。戊句。己句。庚句。辛句。')
        original=self.processor.complete
        async def complete(model,messages):
            data=json.loads(messages[-1]['content'])
            if 'materials' in data and 'units' not in data:
                self.processor.calls.append(copy.deepcopy(messages))
                material=data['materials'][0]
                quotes=['甲句。','乙句。','丙句。','丁句。','戊句。','己句。','庚句。','辛句。']
                units=[{'text':quotes[i]+quotes[i+1],'kind':'fact','refs':[{'material_id':material['id'],'quote':q} for q in quotes[i:i+2]]} for i in range(6)]
                return json.dumps({'format_version':1,'units':units},ensure_ascii=False),{'total_tokens':13}
            return await original(model,messages)
        with patch.object(self.processor,'complete',side_effect=complete):
            result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.processor.calls),4)
        self.assertEqual(len(result['context']['knowledge']['processing']['translation_task_ids']),1)
        self.assertLessEqual(len(result['context']['knowledge']['evidence']),6)

    async def test_saved_receipt_resume_has_no_second_goal_call(self):
        self.enable()
        original=self.processing.published
        interrupted=False
        def publish(db,task,payload,response,audit):
            nonlocal interrupted
            if payload['knowledge_processing']['stage']=='goal' and not interrupted:
                interrupted=True
                raise RuntimeErrorBase('knowledge_processing_request_inactive')
            return original(db,task,payload,response,audit)
        with patch.object(self.processing,'published',side_effect=publish):
            first=await self.ask('月球是什么？')
        self.assertEqual(first['status'],'failed')
        self.assertEqual(len(self.processor.calls),1)
        await self.chat.retry(first['id']);await self.chat.wait()
        done=self.chat.get(first['id'])
        self.assertEqual(done['status'],'completed',done['error_code'])
        self.assertEqual(len(self.processor.calls),4)
        with self.store.connection() as db:
            count=db.execute("SELECT count(*) FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id WHERE json_extract(t.payload_json,'$.knowledge_processing.stage')='goal'").fetchone()[0]
        self.assertEqual(count,1)

    async def test_unknown_call_is_not_replayed_by_turn_retry(self):
        from persona_runtime.chat import ModelError
        self.enable()
        async def unknown(model,messages):
            self.processor.calls.append(messages)
            raise ModelError('provider_network_error',diagnostics={'request_headers_sent':True})
        with patch.object(self.processor,'complete',side_effect=unknown):
            first=await self.ask('月球是什么？')
            await self.chat.retry(first['id']);await self.chat.wait()
        self.assertEqual(len(self.processor.calls),1)
        self.assertEqual(self.chat.get(first['id'])['error_code'],'knowledge_processing_blocked_unknown')
        self.assertEqual(self.capture.calls,[])

    async def test_completed_work_reuses_models_and_policy_change_blocks_derived_answer(self):
        self.enable()
        first=await self.ask('月球是什么？')
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(first['id'],)).fetchone())
        result=await asyncio.to_thread(self.processing.prepare,turn,'月球是什么？')
        self.assertEqual(len(self.processor.calls),4)
        self.assertTrue(result['translation'])
        policy=self.processing.policy(self.persona['id'])
        self.processing.update_policy(self.persona['id'],policy['data']|{'enabled':False},policy['revision'],'stop-translation')
        self.assertIsNone(self.chat.get(first['id'])['reply'])

    async def test_existing_channel_policy_cannot_be_widened_by_processing(self):
        self.enable()
        policy=self.org.managed.current_policy(self.persona['id'])
        data=policy['data']|{'channels':['desktop']}
        self.org.managed.update_policy(self.persona['id'],data,policy['id'],self.persona['id'],'desktop-only-processing')
        result=await self.ask('月球是什么？',channel='qq',persona_id=self.persona['id'],origin={'bot':'456','sender':'123'})
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.processor.calls,[])
        self.assertEqual(self.capture.calls,[])

    async def test_cancel_while_goal_pending_never_starts_main(self):
        self.enable();self.processor.gate=asyncio.Event()
        turn=await self.chat.submit('desktop','月球是什么？','cancel-goal')
        for _ in range(200):
            if self.processor.calls:break
            await asyncio.sleep(.01)
        self.assertTrue(self.processor.calls)
        await self.chat.cancel(turn['id'])
        self.processor.gate.set()
        await self.chat.wait()
        self.assertEqual(self.chat.get(turn['id'])['phase'],'cancelled')
        self.assertEqual(self.capture.calls,[])


class OutputTests(unittest.TestCase):
    def payload(self, stage, inputs):
        return {'knowledge_processing':{'stage':stage,'input':inputs}}

    def test_selection_cannot_invent_section_ids(self):
        with self.assertRaisesRegex(RuntimeErrorBase,'knowledge_processing_output_invalid'):
            parse(self.payload('select',{'sources':[{'id':'known'}]}),json.dumps({'format_version':1,'section_ids':['invented']}))

    def test_unique_existing_section_number_normalizes_but_ambiguity_fails(self):
        sources=[{'id':'known','evidence':{'section_id':123}}]
        raw=json.dumps({'format_version':1,'section_ids':['123']})
        out=parse(self.payload('select',{'sources':sources}),raw)
        self.assertEqual(out['section_ids'],['known'])
        self.assertEqual(out['_normalizations'][0]['kind'],'unique_section_id_alias')
        with self.assertRaisesRegex(RuntimeErrorBase,'knowledge_processing_output_invalid'):
            parse(self.payload('select',{'sources':sources+[{'id':'other','evidence':{'section_id':123}}]}),raw)

    def test_conflict_and_source_claim_cannot_be_promoted(self):
        for metadata in ({'claim_state':'conflict'},{'claim_kind':'source_claim'}):
            material={'id':'known','text':'来源称每天09:00开放。','evidence':metadata}
            out={'format_version':1,'units':[{'text':'每天09:00开放。','kind':'fact','refs':[{'material_id':'known','quote':material['text']}]}]}
            with self.assertRaisesRegex(RuntimeErrorBase,'knowledge_processing_output_invalid'):
                parse(self.payload('translate',{'materials':[material]}),json.dumps(out))

    def test_review_requires_complete_unique_coverage(self):
        decision={'index':0,'supported':True,'temporal_ok':True,'version_ok':True,'note':'核对'}
        with self.assertRaisesRegex(RuntimeErrorBase,'knowledge_processing_output_invalid'):
            parse(self.payload('verify',{'units':[{},{}]}),json.dumps({'format_version':1,'decisions':[decision,decision]}))
