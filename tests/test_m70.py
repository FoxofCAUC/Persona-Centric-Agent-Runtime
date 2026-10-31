"""M7.0 exact archive and durable references: no real models/platform sends."""
import asyncio
import json
import sqlite3
import unittest
from unittest.mock import patch
from tests import test_m61 as m61
from persona_runtime.archive import ArchiveService, scope, resolve, target_spec
from persona_runtime.delivery import DeliveryService
from persona_runtime.models import identifier, RuntimeErrorBase, generic_persona
from persona_runtime.memory_policy import exclude_sources
from persona_runtime.sdk_reference_guard import install
from persona_runtime.submissions import SubmissionJournal
from aiohttp.test_utils import TestServer
from aiohttp import ClientSession
from persona_runtime.server import create_app


class M70Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=m61.IngressTests.asyncSetUp
    finish=m61.IngressTests.finish
    natural=m61.IngressTests.natural
    accept=m61.IngressTests.accept
    seal=m61.IngressTests.seal
    batch=m61.IngressTests.batch

    async def seed(self, text='种子输入', **kw):
        r=self.accept(text,**kw);await self.engine.wait()
        return r,self.engine.get(self.batch(r)['turn_id'])

    async def test_reference_restores_full_turn_and_deduplicates_history(self):
        self.natural();a=self.accept('青禾书店');b=self.accept('预算三百元');c=self.accept('偏好蓝色');self.seal(a);await self.engine.wait()
        target=self.batch(a)['members'][2]['event_id']
        r=self.accept('结合这个回合回答',reference={'kind':'event','id':target});self.seal(r);await self.engine.wait()
        t=self.engine.get(self.batch(r)['turn_id']);self.assertEqual(t['status'],'completed')
        messages=self.fake.messages[-1];joined='\n'.join(m['content'] for m in messages)
        for text in ('青禾书店','预算三百元','偏好蓝色'):self.assertEqual(joined.count(text),1)
        self.assertEqual(len(t['context']['references']['references']),1)

    async def test_unknown_reference_fails_without_attempt_and_survives_retry(self):
        r=self.accept('未知引用',reference={'kind':'event','id':'missing'});await self.engine.wait()
        t=self.engine.get(self.batch(r)['turn_id']);self.assertEqual(t['status'],'failed');self.assertEqual(t['attempt_count'],0)
        self.assertEqual(t['error_code'],'reference_not_found');self.assertFalse(self.fake.messages)
        await self.engine.retry(t['id']);await self.engine.wait()
        with self.store.connection() as conn:
            rows=conn.execute('SELECT * FROM reference_checks WHERE turn_id=? ORDER BY rowid',(t['id'],)).fetchall()
            self.assertEqual(len(rows),2);self.assertEqual(rows[1]['trigger_kind'],'retry')

    async def test_nonfirst_retraction_invalidates_accepted_reference(self):
        self.natural();a=self.accept('一');b=self.accept('二');self.seal(a);await self.engine.wait()
        target=self.batch(a)['members'][0]['event_id'];excluded=self.batch(a)['members'][1]['event_id']
        r=self.accept('引用',reference={'kind':'event','id':target})
        with self.store.connection(write=True) as conn:exclude_sources(conn,self.pid,[excluded],'test')
        self.seal(r);await self.engine.wait();t=self.engine.get(self.batch(r)['turn_id'])
        self.assertEqual(t['error_code'],'reference_unavailable');self.assertEqual(t['attempt_count'],0);self.assertEqual(len(self.fake.messages),1)

    async def test_any_bad_member_blocks_whole_batch(self):
        self.natural();a=self.accept('正常');self.accept('失败',reference={'kind':'turn','id':'missing'});self.seal(a);await self.engine.wait()
        t=self.engine.get(self.batch(a)['turn_id']);self.assertEqual(t['raw_message_count'],2);self.assertEqual(t['attempt_count'],0);self.assertFalse(self.fake.messages)

    async def test_reference_fingerprint_conflict_and_duplicate(self):
        r=self.accept('正文',rid='same',reference={'kind':'event','id':'one'})
        self.assertEqual(self.accept('正文',rid='same',reference={'kind':'event','id':'one'})['receipt_id'],r['receipt_id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):self.accept('正文',rid='same',reference={'kind':'event','id':'two'})

    async def test_archive_scope_and_admin_preview_are_read_only(self):
        r,t=await self.seed();api=ArchiveService(self.store)
        result=api.preview(persona_id=self.pid,channel='desktop',target={'kind':'turn','id':t['id']})
        self.assertEqual(result['status'],'found');self.assertFalse(result['preview']['creates_request'])
        other=self.service.create_persona(generic_persona())['id']
        self.assertEqual(api.get(persona_id=other,channel='desktop',target={'kind':'turn','id':t['id']})['status'],'not_found')
        self.assertEqual(api.get(persona_id=self.pid,channel='qq',bot='456',peer='123',target={'kind':'turn','id':t['id']})['status'],'not_found')
        with self.store.connection() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM input_references').fetchone()[0],0)

    async def test_budget_fails_without_provider(self):
        _,t=await self.seed('长'*13000)
        r=self.accept('引用',reference={'kind':'turn','id':t['id']});await self.engine.wait()
        current=self.engine.get(self.batch(r)['turn_id']);self.assertEqual(current['error_code'],'reference_budget_exceeded');self.assertEqual(current['attempt_count'],0)

    async def test_preview_budget_includes_same_complete_envelope_as_execution(self):
        _,seed=await self.seed('预算边界')
        api=ArchiveService(self.store);target={'kind':'turn','id':seed['id']}
        args={'persona_id':self.pid,'channel':'desktop','target':target}
        before=api.preview(**args)['preview']['reference_characters']
        # Construct a source whose complete envelope, rather than just its body,
        # exceeds the reference budget by exactly one character.
        with self.store.connection(write=True) as conn:
            conn.execute('UPDATE events SET text=text || ? WHERE id=?',('界'*(12001-before),seed['user_event_id']))
        preview=api.preview(**args)['preview']
        self.assertEqual(preview['reference_characters'],12001)
        self.assertFalse(preview['within_reference_budget']);self.assertFalse(preview['total_input_checked'])
        receipt=self.accept('引用',reference=target);await self.engine.wait()
        turn=self.engine.get(self.batch(receipt)['turn_id'])
        self.assertEqual(turn['error_code'],'reference_budget_exceeded');self.assertEqual(turn['attempt_count'],0)
        with self.store.connection() as conn:
            row=conn.execute('SELECT manifest_json FROM reference_checks WHERE turn_id=?',(turn['id'],)).fetchone()
        self.assertEqual(json.loads(row[0])['reference_characters'],preview['reference_characters'])
        self.assertEqual(len(self.fake.messages),1)

    async def assert_reference_preflight_failure_recorded(self,model_update,error_code):
        _,seed=await self.seed('有效引用来源')
        self.config.update({'model':model_update})
        receipt=self.accept('引用',reference={'kind':'turn','id':seed['id']});await self.engine.wait()
        turn=self.engine.get(self.batch(receipt)['turn_id'])
        self.assertEqual(turn['error_code'],error_code);self.assertEqual(turn['attempt_count'],0)
        self.assertEqual(len(self.fake.messages),1)
        with self.store.connection() as conn:
            first=dict(conn.execute('SELECT * FROM reference_checks WHERE turn_id=?',(turn['id'],)).fetchone())
        manifest=json.loads(first['manifest_json'])
        self.assertEqual(first['trigger_kind'],'initial');self.assertIsNone(first['attempt_id'])
        self.assertEqual(first['error'],error_code);self.assertEqual(manifest['check_status'],'passed')
        self.assertEqual(manifest['execution_blocked'],error_code)
        self.assertTrue(all(ref['status']=='found' for ref in manifest['references']))
        self.config.update({'model':{'name':'mock','hourly_limit':30}})
        await self.engine.retry(turn['id']);await self.engine.wait()
        self.assertEqual(self.engine.get(turn['id'])['status'],'completed')
        with self.store.connection() as conn:
            checks=[dict(row) for row in conn.execute('SELECT * FROM reference_checks WHERE turn_id=? ORDER BY rowid',(turn['id'],))]
        self.assertEqual(len(checks),2);self.assertEqual(checks[0],first)
        self.assertEqual(checks[1]['trigger_kind'],'retry');self.assertIsNotNone(checks[1]['attempt_id'])
        self.assertIsNone(checks[1]['error']);self.assertEqual(len(self.fake.messages),2)

    async def test_valid_reference_check_survives_missing_model_and_retry(self):
        await self.assert_reference_preflight_failure_recorded({'name':''},'model_not_configured')

    async def test_valid_reference_check_survives_quota_limit_and_retry(self):
        await self.assert_reference_preflight_failure_recorded({'hourly_limit':1},'hourly_limit_reached')

    async def test_binding_does_not_follow_changed_original_text(self):
        self.natural();a=self.accept('原文');self.seal(a);await self.engine.wait();event=self.batch(a)['members'][0]['event_id']
        r=self.accept('引用',reference={'kind':'event','id':event})
        with self.store.connection(write=True) as conn:conn.execute('UPDATE events SET text=? WHERE id=?',('外部改动',event))
        self.seal(r);await self.engine.wait();self.assertEqual(self.engine.get(self.batch(r)['turn_id'])['error_code'],'reference_source_changed')

    async def test_qq_platform_input_part_and_ambiguity(self):
        self.config.update({'bot_qq':'456','owner_qq':'123'})
        r,t=await self.seed(channel='qq',bot='456',sender='123',platform_message_id='100')
        api=ArchiveService(self.store);s=scope(self.pid,'qq','456','123')
        with self.store.connection() as conn:self.assertEqual(resolve(conn,s,{'kind':'platform_message','message_id':'100'})['status'],'found')
        delivery=DeliveryService(self.store);stream=delivery.by_batch(t['reply_batch']['id']);part=stream['parts'][0]
        self.assertEqual(api.get(persona_id=self.pid,channel='qq',bot='456',peer='123',target={'kind':'part','id':part['id']})['status'],'unavailable')
        claim=delivery.claim(stream['id'],part['id'],stream['revision'],identifier(),persona_id=self.pid)
        delivery.result(stream['id'],part['id'],claim['claim_token'],'sent',claim['part_revision'],identifier(),platform_message_id='200',persona_id=self.pid)
        with self.store.connection() as conn:
            resolved=resolve(conn,s,{'kind':'platform_message','message_id':'200'});self.assertEqual(resolved['status'],'found');self.assertEqual(resolved['replies'][0]['text'],part['text'])
        with self.store.connection(write=True) as conn:conn.execute('UPDATE raw_events SET platform_message_id=? WHERE event_id=?',('200',t['user_event_id']))
        with self.store.connection() as conn:self.assertEqual(resolve(conn,s,{'kind':'platform_message','message_id':'200'})['status'],'ambiguous')

    async def test_export_and_reinitialization(self):
        r=self.accept('引用',reference={'kind':'turn','id':'missing'});await self.engine.wait();self.store.initialize()
        value=json.loads(__import__('pathlib').Path(self.service.export_persona()['path']).read_text(encoding='utf-8'))
        self.assertEqual(value['format_version'],16);self.assertEqual(len(value['input_references']),1);self.assertEqual(len(value['reference_checks']),1)

    async def test_draft_dedup_includes_reference(self):
        journal=SubmissionJournal(self.paths,'desktop');p={'channel':'desktop','persona_id':self.pid,'text':'same','request_id':'one','reference':{'kind':'event','id':'A'}}
        journal.remember(p,match_draft=True)
        q={**p,'request_id':'two','reference':{'kind':'event','id':'B'}}
        self.assertEqual(journal.remember(q,match_draft=True)['request_id'],'two')

    async def test_new_platform_collision_does_not_retarget_accepted_choice(self):
        self.config.update({'bot_qq':'456','owner_qq':'123'})
        _,seed=await self.seed(channel='qq',bot='456',sender='123',platform_message_id='42')
        pol=self.ingress.current_policy(self.pid,'qq');self.ingress.update_policy(self.pid,'qq','natural_batch',pol['id'],identifier())
        r=self.accept('引用',channel='qq',bot='456',sender='123',platform_message_id='43',reference={'kind':'platform_message','message_id':'42'})
        with self.store.connection(write=True) as conn:conn.execute('UPDATE raw_events SET platform_message_id=? WHERE event_id=?',('42',self.batch(r)['members'][0]['event_id']))
        self.seal(r);await self.engine.wait();self.assertEqual(self.engine.get(self.batch(r)['turn_id'])['status'],'completed')

    async def test_api_archive_is_admin_only_and_preview_has_no_writes(self):
        _,seed=await self.seed()
        app=create_app(self.paths,client=self.fake)
        async with TestServer(app) as server:
            async with ClientSession() as http:
                value={'action':'archive.preview','args':{'persona_id':self.pid,'channel':'desktop','target':{'kind':'turn','id':seed['id']}}}
                response=await http.post(server.make_url('/v1/manage'),json=value,headers={'Authorization':'Bearer '+self.config.token('bridge')})
                self.assertEqual(response.status,403)
                response=await http.post(server.make_url('/v1/manage'),json=value,headers={'Authorization':'Bearer '+self.config.token()})
                self.assertEqual(response.status,200);self.assertEqual((await response.json())['status'],'found')
        with self.store.connection() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM reference_checks').fetchone()[0],0)

    async def test_archive_audit_does_not_make_incomplete_source_usable(self):
        self.natural();a=self.accept('未完成');target=self.batch(a)['members'][0]['event_id']
        # A collecting event has no turn yet. It is not a normal reference target.
        api=ArchiveService(self.store)
        self.assertNotEqual(api.get(persona_id=self.pid,channel='desktop',target={'kind':'event','id':target})['status'],'found')
        self.seal(a);await self.engine.wait()
        with self.store.connection(write=True) as conn:exclude_sources(conn,self.pid,[target],'test')
        self.assertEqual(api.get(persona_id=self.pid,channel='desktop',target={'kind':'event','id':target})['status'],'unavailable')
        audit=api.get(persona_id=self.pid,channel='desktop',target={'kind':'event','id':target},audit=True)
        self.assertTrue(audit['audit_only']);self.assertFalse(audit['usable'])

    async def test_multiple_references_to_same_group_are_once_in_context(self):
        self.natural();a=self.accept('唯一种子');self.seal(a);await self.engine.wait();tid=self.batch(a)['turn_id']
        b=self.accept('引用一',reference={'kind':'turn','id':tid});self.accept('引用二',reference={'kind':'turn','id':tid});self.seal(b);await self.engine.wait()
        text='\n'.join(m['content'] for m in self.fake.messages[-1]);self.assertEqual(text.count('唯一种子'),1)

    async def test_selected_member_identity_is_present_beside_complete_group(self):
        self.natural();a=self.accept('第一条');self.accept('第二条');self.seal(a);await self.engine.wait()
        second=self.batch(a)['members'][1]['event_id'];b=self.accept('我引用哪条',reference={'kind':'event','id':second});self.seal(b);await self.engine.wait()
        messages=self.fake.messages[-1];content='\n'.join(m['content'] for m in messages)
        # Metadata identifies the selected target, not just an indistinguishable group.
        self.assertIn('"targets":[{"id":"'+second+'","kind":"event"}]',content)
        self.assertIn('第一条',content);self.assertIn('第二条',content)

    async def test_each_current_member_keeps_ordered_target_association(self):
        _,first=await self.seed('来源甲');_,second=await self.seed('来源乙')
        self.natural()
        targets=[{'kind':'event','id':first['user_event_id']},
                 {'kind':'event','id':second['user_event_id']},
                 {'kind':'event','id':first['user_event_id']}]
        receipt=self.accept('解释这条',reference=targets[0])
        self.accept('确认这条',reference=targets[1]);self.accept('再说明这条',reference=targets[2])
        members=self.batch(receipt)['members']
        self.seal(receipt);await self.engine.wait()
        system=self.fake.messages[-1][0]['content']
        block=json.loads(system.split('不递归展开旧引用）：\n',1)[1])
        self.assertEqual(block['selections'],[
            {'input_event_id':member['event_id'],'target':target,'turn_id':turn_id}
            for member,target,turn_id in zip(members,targets,[first['id'],second['id'],first['id']])])
        self.assertEqual(len(block['turns']),2)
        self.assertEqual(system.count('来源甲'),1);self.assertEqual(system.count('来源乙'),1)

    async def test_same_turn_unions_only_replies_fixed_by_each_accepted_member(self):
        self.config.update({'bot_qq':'456','owner_qq':'123'})
        async def seed_reply(model,messages):
            self.fake.messages.append(messages)
            return '甲'*450+'乙'*450+'未被任何引用固定的第三片',{'total_tokens':1}
        with patch.object(self.fake,'complete',side_effect=seed_reply):
            _,seed=await self.seed(channel='qq',bot='456',sender='123',platform_message_id='100')
        delivery=DeliveryService(self.store);stream=delivery.by_batch(seed['reply_batch']['id'])
        parts=stream['parts'];self.assertEqual(len(parts),3)
        def send(index):
            current=delivery.get(stream['id'])
            claim=delivery.claim(stream['id'],parts[index]['id'],current['revision'],identifier(),persona_id=self.pid)
            delivery.result(stream['id'],parts[index]['id'],claim['claim_token'],'sent',claim['part_revision'],identifier(),
                            platform_message_id=str(200+index),persona_id=self.pid)
        send(0)
        policy=self.ingress.current_policy(self.pid,'qq')
        self.ingress.update_policy(self.pid,'qq','natural_batch',policy['id'],identifier())
        receipt=self.accept('第一条引用',channel='qq',bot='456',sender='123',platform_message_id='101',
                            reference={'kind':'platform_message','message_id':'100'})
        send(1)
        self.accept('第二条引用',channel='qq',bot='456',sender='123',platform_message_id='102',
                    reference={'kind':'platform_message','message_id':'201'})
        send(2)  # Confirmed after both choices: it must not enter their snapshots.
        self.seal(receipt);await self.engine.wait()
        turn=self.engine.get(self.batch(receipt)['turn_id']);self.assertEqual(turn['status'],'completed')
        block=json.loads(self.fake.messages[-1][0]['content'].split('不递归展开旧引用）：\n',1)[1])
        self.assertEqual(len(block['turns']),1)
        self.assertEqual([r['id'] for r in block['turns'][0]['replies']],[parts[0]['id'],parts[1]['id']])
        self.assertEqual([r['text'] for r in block['turns'][0]['replies']],[parts[0]['text'],parts[1]['text']])
        self.assertEqual(block['selections'][1]['target'],{'kind':'part','id':parts[1]['id']})
        self.assertNotIn('未被任何引用固定的第三片',json.dumps(block,ensure_ascii=False))

    async def test_schema10_failure_rolls_back_new_tables_and_version(self):
        from contextlib import closing
        with closing(sqlite3.connect(self.store.path)) as conn:
            for table,columns in {'turns':['expression_snapshot','new_input_event_id'],
                                  'reply_batches':['behavior_intent','fallback_reason'],
                                  'reply_segments':['content_kind','cancel_if_new_input','wait_ms'],
                                  'reply_delivery_parts':['wait_started_at','wait_ms','cancel_reason','trigger_event_id']}.items():
                for column in columns:conn.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
            from persona_runtime.schema_m73 import TABLES13
            for trigger, in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                conn.execute('DROP TRIGGER "'+trigger+'"')
            # Remove later schemas before simulating a genuine v10 DB.
            from persona_runtime.schema_disposition import TABLES19
            for table in reversed(TABLES19):conn.execute('DROP TABLE "'+table+'"')
            conn.execute('DROP INDEX delivery_display_message')
            conn.execute('ALTER TABLE reply_delivery_parts DROP COLUMN display_message_id')
            conn.execute('DROP TABLE reply_display_messages')
            conn.execute('DROP TABLE persona_operations')
            conn.execute('DROP TABLE persona_lifecycle')
            conn.execute('DROP TABLE quota_reservations')
            conn.execute('DROP TABLE character_selection_attempts')
            conn.execute('ALTER TABLE turns DROP COLUMN execution_revision')
            conn.execute('ALTER TABLE turns DROP COLUMN selector_model_snapshot')
            conn.execute('ALTER TABLE input_batches DROP COLUMN selector_model_snapshot')
            for table in reversed(TABLES13):conn.execute('DROP TABLE "'+table+'"')
            conn.execute('ALTER TABLE summaries DROP COLUMN source_refs_json')
            conn.executescript('DROP TABLE context_manifests; ALTER TABLE turns DROP COLUMN context_policy_snapshot; DROP TABLE reference_checks; DROP TABLE input_references; DROP INDEX raw_platform_reference; DROP INDEX sent_platform_reference; PRAGMA user_version=10;');conn.commit()
        with patch('persona_runtime.storage.SCHEMA11','CREATE TABLE partial_m70(x); INSERT INTO table_missing VALUES (1);'):
            with self.assertRaises(sqlite3.Error):self.store.initialize()
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],10)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='partial_m70'").fetchone())
        self.store.initialize();self.assertEqual(self.store.status()['schema_version'],19)

    async def test_legacy_standalone_event_audit_reports_unknown_scope_without_fabrication(self):
        session={'id':identifier()};eid=identifier()
        from persona_runtime.models import LOCAL_USER_ID,timestamp
        with self.store.connection(write=True) as conn:
            conn.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(session['id'],self.pid,LOCAL_USER_ID,'private',timestamp()))
            conn.execute('INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES (?,?,?,?,?,?,?)',(eid,session['id'],self.pid,LOCAL_USER_ID,'user','旧规范化原文',timestamp()))
        api=ArchiveService(self.store);args={'persona_id':self.pid,'channel':'desktop','target':{'kind':'event','id':eid}}
        self.assertEqual(api.get(**args)['status'],'not_found')
        value=api.get(**args,audit=True)
        self.assertEqual(value['event']['text'],'旧规范化原文');self.assertFalse(value['scope_verified']);self.assertFalse(value['usable']);self.assertIsNone(value['source_channel'])
        other=self.service.create_persona(generic_persona())['id']
        self.assertEqual(api.get(**{**args,'persona_id':other},audit=True)['status'],'not_found')

    async def test_sdk_guard_suppresses_fetch_and_is_idempotent(self):
        class Adapter:
            async def _convert_handle_message_event(self,event,get_reply=True):
                if get_reply:raise AssertionError('get_msg called')
                return event
        install(Adapter);first=Adapter._convert_handle_message_event;install(Adapter)
        self.assertIs(first,Adapter._convert_handle_message_event)
        event={'message':[{'type':'reply','data':{'id':'1'}}]};self.assertEqual(await Adapter()._convert_handle_message_event(event),event)
        class Bad:
            async def _convert_handle_message_event(self,event):pass
        with self.assertRaisesRegex(RuntimeErrorBase,'incompatible'):install(Bad)

    def test_invalid_reference_fields_and_channel(self):
        for target in ({'kind':'event','id':'x','script':'x'},{'kind':'event','id':''},{'kind':'event','id':'x'*201},{'kind':'event','id':'\x00'}):
            with self.assertRaises(RuntimeErrorBase):target_spec(target,'desktop')
        with self.assertRaises(RuntimeErrorBase):target_spec({'kind':'event','id':'x'},'qq')


if __name__=='__main__':unittest.main()
