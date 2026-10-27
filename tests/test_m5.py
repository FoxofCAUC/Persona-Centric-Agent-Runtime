"""Persona continuity and explicit-review contracts; all data is fictional and isolated."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from persona_runtime.chat import ChatEngine,ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.models import generic_persona,RuntimeErrorBase,LOCAL_USER_ID
from persona_runtime.profile import ProfileService,active_state
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.server import create_app
from persona_runtime.storage import Store,SCHEMA,SCHEMA2,SCHEMA3,SCHEMA4,SCHEMA5,execute_schema


class Model:
    def __init__(self):self.calls=[];self.gate=None;self.fail=False
    async def complete(self,model,messages):
        self.calls.append((model,messages))
        if self.gate:await self.gate.wait()
        if self.fail:raise ModelError('provider_timeout')
        return '离线回应',None


class M5Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='m5-test-',dir=root);self.addCleanup(self.temp.cleanup)
        self.paths=RuntimePaths(Path(self.temp.name));self.cfg=ConfigStore(self.paths);self.cfg.initialize()
        self.cfg.update({'model':{'base_url':'http://127.0.0.1:1','name':'offline','hourly_limit':1000}})
        self.store=Store(self.paths);self.store.initialize();self.service=RuntimeService(self.store)
        self.persona=self.service.create_persona(generic_persona());self.service.use_persona(self.persona['id'])
        self.profile=ProfileService(self.store);self.model=Model();self.chat=ChatEngine(self.store,self.cfg,self.model)

    async def asyncTearDown(self):
        if self.model.gate:self.model.gate.set()
        await self.chat.wait()

    def edit(self,kind,data,key='write'):
        return self.profile.update(kind,data,self.profile.current(kind)['id'],key)

    def note(self,text='这是虚构的关系证据'):
        session=self.service.create_session()
        return self.service.append_event(session['id'],'note',text)

    async def send(self,text='虚构测试',key='chat',channel='desktop'):
        turn=await self.chat.submit(channel,text,key);await self.chat.wait();return self.chat.get(turn['id'])

    async def test_core_edit_keeps_identity_history_and_import_creates_new_identity(self):
        session=self.service.create_session();event=self.service.append_event(session['id'],'note','虚构记忆')
        self.service.add_memory('user_fact','虚构记忆',event['id'])
        core=self.profile.current('core');data=core['data']|{'name':'清和'}
        version=self.profile.update('core',data,core['id'],'stable')
        self.assertEqual(version['revision'],2)
        self.assertEqual(self.service.show_persona()['id'],self.persona['id'])
        self.assertEqual(self.service.show_persona()['name'],'清和')
        self.assertEqual(len(self.service.show_session(session['id'])['events']),1)
        self.assertEqual(self.profile.update('core',data,core['id'],'stable')['id'],version['id'])
        new=self.service.create_persona(generic_persona());self.service.use_persona(new['id'])
        self.assertNotEqual(new['id'],self.persona['id']);self.assertEqual(self.service.search_memories(),[])
        self.assertEqual(self.profile.current('core')['revision'],1)

    async def test_expression_is_independent_and_versions_compare(self):
        core=self.profile.current('core')['id'];old=self.profile.current('expression')
        new=self.edit('expression',{'style':'虚构风格：简短自然，不使用固定口头禅。'})
        self.assertEqual(self.profile.current('core')['id'],core)
        self.assertEqual(self.service.show_persona()['spec']['expression_style'],new['data']['style'])
        self.assertIn('style',self.profile.compare(old['id'],new['id'])['changes'])
        with self.assertRaisesRegex(RuntimeErrorBase,'profile_kind_mismatch'):
            self.profile.compare(core,new['id'])

    async def test_optimistic_conflicts_and_operation_reuse_never_partially_write(self):
        old=self.profile.current('expression')
        self.profile.update('expression',{'style':'第一版'},old['id'],'same')
        for operation in ('same','other'):
            with self.assertRaises(RuntimeErrorBase):
                self.profile.update('expression',{'style':'冲突版'},old['id'],operation)
        self.assertEqual(len(self.profile.history('expression')),2)
        self.assertEqual(self.profile.current('expression')['data']['style'],'第一版')

    async def test_core_restore_creates_revision_without_restoring_retracted_memory(self):
        old=self.profile.current('core');event=self.note();mem=self.service.add_memory('user_fact','撤回测试',event['id'])
        self.edit('core',old['data']|{'name':'新名字'})
        self.service.retract_memory(mem['id'])
        new=self.profile.restore(old['id'],self.profile.current('core')['id'],'restore')
        self.assertEqual(new['revision'],3);self.assertEqual(new['restored_from'],old['id'])
        self.assertEqual(self.service.search_memories(),[])

    async def test_legacy_relationship_set_creates_versions_and_unchanged_write_is_noop(self):
        self.service.set_relationship('虚构朋友关系');self.service.set_relationship('虚构朋友关系')
        self.assertEqual(len(self.profile.history('relationship')),2)
        self.service.set_relationship('虚构学习伙伴')
        self.assertEqual(self.profile.current('relationship')['revision'],3)
        self.assertEqual(self.service.show_relationship()['description'],'虚构学习伙伴')

    async def test_manual_state_expiry_and_clear_do_not_change_core(self):
        core=self.profile.current('core')['id'];expiry=datetime.now(timezone.utc)+timedelta(hours=24)
        state=self.edit('state',{'text':'今天希望语气简短','expires_at':expiry.isoformat()})
        self.assertTrue(active_state(state));self.assertFalse(active_state(state,now=expiry+timedelta(seconds=1)))
        first=await self.send();self.assertEqual(first['context']['state_version'],state['id'])
        with patch('persona_runtime.profile.datetime',wraps=datetime) as clock:
            clock.now.return_value=expiry+timedelta(seconds=1)
            second=await self.send(key='expired')
            self.assertIsNone(second['context']['state_version'])
            with self.assertRaises(RuntimeErrorBase):self.profile.restore(state['id'],state['id'],'expired-restore')
        self.edit('state',{'text':'','expires_at':None},'clear')
        self.assertEqual(self.profile.current('core')['id'],core)
        self.assertFalse(self.profile.current('state')['active'])

    async def test_invalid_state_dates_rejected_without_new_versions(self):
        for expiry in ('2026-01-01T00:00:00',None,'not-a-date',(datetime.now(timezone.utc)+timedelta(days=8)).isoformat()):
            with self.assertRaises(RuntimeErrorBase):self.edit('state',{'text':'测试','expires_at':expiry})
        self.assertEqual(len(self.profile.history('state')),1)

    async def test_queued_core_and_relationship_are_fixed_at_acceptance(self):
        before=self.profile.current();self.model.gate=asyncio.Event()
        first=await self.chat.submit('desktop','首条','one');await asyncio.sleep(.01)
        second=await self.chat.submit('desktop','排队','two')
        self.edit('core',before['core']['data']|{'name':'改变后的名字'})
        self.service.set_relationship('改变后的关系')
        self.model.gate.set();await self.chat.wait()
        for turn in (first,second):
            context=self.chat.get(turn['id'])['context']
            self.assertEqual(context['profile_versions']['core']['id'],before['core']['id'])
            self.assertEqual(context['profile_versions']['relationship']['id'],before['relationship']['id'])
        third=await self.send(key='new')
        self.assertNotEqual(third['context']['profile_versions']['core']['id'],before['core']['id'])
        self.assertIn('改变后的名字',self.model.calls[-1][1][0]['content'])

    async def test_state_expiring_while_queued_is_not_used_for_next_call(self):
        expiry=datetime.now(timezone.utc)+timedelta(minutes=1)
        state=self.edit('state',{'text':'即将过期的虚构状态','expires_at':expiry.isoformat()})
        self.model.gate=asyncio.Event();await self.chat.submit('desktop','先生成','one');await asyncio.sleep(.01)
        queued=await self.chat.submit('desktop','后生成','two')
        with patch('persona_runtime.profile.datetime',wraps=datetime) as clock:
            clock.now.return_value=expiry+timedelta(seconds=1)
            self.model.gate.set();await self.chat.wait()
        self.assertIsNone(self.chat.get(queued['id'])['context']['state_version'])
        self.assertNotIn(state['data']['text'],self.model.calls[-1][1][0]['content'])

    async def test_manual_retry_uses_current_versions_and_retains_old_attempt_trace(self):
        self.model.fail=True;turn=await self.send()
        old=turn['context']['profile_versions']['expression']['id']
        self.edit('expression',{'style':'重试的新风格'})
        self.model.fail=False;await self.chat.retry(turn['id']);await self.chat.wait()
        result=self.chat.get(turn['id'])
        self.assertNotEqual(result['context']['profile_versions']['expression']['id'],old)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM context_uses WHERE turn_id=?',(turn['id'],)).fetchone()[0],2)
        self.assertEqual(result['user_event_id'],turn['user_event_id'])

    async def test_model_switch_and_two_channels_share_profile_not_recent_history(self):
        expression=self.edit('expression',{'style':'固定的虚构表达规则'})
        desktop=await self.send();self.cfg.update({'model':{'name':'second-offline-model'}})
        qq=await self.send(key='qq',channel='qq')
        self.assertNotEqual(desktop['session_id'],qq['session_id'])
        self.assertEqual(desktop['context']['profile_versions'],qq['context']['profile_versions'])
        self.assertEqual(self.model.calls[-1][0]['name'],'second-offline-model')
        self.assertIn(expression['data']['style'],self.model.calls[-1][1][0]['content'])

    async def test_relation_candidate_requires_explicit_review_and_duplicate_is_idempotent(self):
        event=self.note();old=self.profile.current('relationship')
        proposal=self.profile.propose_relationship('虚构手账伙伴',event['id'],'proposal')
        self.assertEqual(self.profile.current('relationship')['id'],old['id'])
        accepted=self.profile.review_relationship(proposal['id'],True,'approve')
        self.assertEqual(self.profile.review_relationship(proposal['id'],True,'again')['version_id'],accepted['version_id'])
        self.assertEqual(self.profile.current('relationship')['source_ids'],[event['id']])
        with self.assertRaises(RuntimeErrorBase):self.profile.review_relationship(proposal['id'],True,'change',text='换内容')

    async def test_relation_candidate_conflict_reject_and_source_stale(self):
        event=self.note();p=self.profile.propose_relationship('虚构伙伴',event['id'],'proposal')
        self.service.set_relationship('先手动修改')
        with self.assertRaisesRegex(RuntimeErrorBase,'profile_version_conflict'):self.profile.review_relationship(p['id'],True,'stale-target')
        self.profile.review_relationship(p['id'],False,'reject')
        self.assertEqual(self.profile.current('relationship')['data']['description'],'先手动修改')
        p2=self.profile.propose_relationship('待失效候选',event['id'],'proposal2')
        mem=self.service.add_memory('user_fact','来源撤回',event['id']);self.service.retract_memory(mem['id'])
        self.assertEqual(self.profile.proposal_show(p2['id'])['status'],'stale')
        with self.assertRaises(RuntimeErrorBase):self.profile.review_relationship(p2['id'],True,'no')

    async def test_approved_relation_with_revoked_source_needs_review_and_is_omitted(self):
        event=self.note();p=self.profile.propose_relationship('不应再次进入模型的旧关系',event['id'],'proposal')
        self.profile.review_relationship(p['id'],True,'approve')
        memory=self.service.add_memory('user_fact','依据',event['id']);self.service.retract_memory(memory['id'])
        self.assertTrue(self.profile.current('relationship')['source_blocked'])
        turn=await self.send()
        self.assertTrue(turn['context']['relationship_requires_review'])
        self.assertNotIn('不应再次进入模型的旧关系',self.model.calls[-1][1][0]['content'])
        with self.assertRaisesRegex(RuntimeErrorBase,'source_expired'):
            self.profile.restore(self.profile.current('relationship')['id'],self.profile.current('relationship')['id'],'restore')

    async def test_cross_persona_versions_sources_and_persona_speech_rejected(self):
        event=self.note();version=self.profile.current('core');other=self.service.create_persona(generic_persona())
        self.service.use_persona(other['id'])
        with self.assertRaises(RuntimeErrorBase):self.profile.restore(version['id'],self.profile.current('core')['id'],'cross')
        with self.assertRaises(RuntimeErrorBase):self.profile.propose_relationship('跨人格',event['id'],'cross-source')
        session=self.service.create_session();fake=self.service.append_event(session['id'],'persona','我宣布我们是多年好友')
        with self.assertRaises(RuntimeErrorBase):self.profile.propose_relationship('伪造经历',fake['id'],'fake')

    async def test_v5_upgrade_preserves_rows_and_rollback_is_atomic(self):
        legacy=Store(RuntimePaths(self.paths.home/'v5'));legacy.paths.initialize()
        db=sqlite3.connect(legacy.path,isolation_level=None)
        for schema in (SCHEMA,SCHEMA2,SCHEMA3,SCHEMA4,SCHEMA5):execute_schema(db,schema)
        db.execute('INSERT INTO users VALUES (?,?,?)',(LOCAL_USER_ID,'本地用户','old-time'))
        db.execute('INSERT INTO personas VALUES (?,?,?,?,?)',('old','旧人格',1,json.dumps(generic_persona().to_dict()),'old-time'))
        db.execute('INSERT INTO relationships VALUES (?,?,?,?)',('old',LOCAL_USER_ID,'旧关系','old-time'))
        db.execute('PRAGMA user_version=5');db.close()
        with patch('persona_runtime.storage.SCHEMA6','CREATE TABLE partial(id TEXT);\nCREATE TABLE personas(id TEXT);\n'):
            with self.assertRaises(sqlite3.Error):legacy.initialize()
        db=sqlite3.connect(legacy.path)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],5)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone())
        finally:db.close()
        legacy.initialize();legacy.initialize()
        with legacy.connection() as conn:
            self.assertEqual(conn.execute('SELECT updated_at FROM relationships').fetchone()[0],'old-time')
            self.assertEqual(conn.execute('SELECT count(*) FROM profile_versions').fetchone()[0],4)
            self.assertEqual(conn.execute('SELECT actor FROM profile_versions LIMIT 1').fetchone()[0],'migration')
        self.assertTrue(list((legacy.paths.home/'backups').glob('before-m5-*.sqlite3')))

    async def test_export_and_backup_include_versions_but_not_credentials(self):
        self.edit('expression',{'style':'可导出的虚构风格'})
        data=json.loads(Path(self.service.export_persona()['path']).read_text(encoding='utf-8'))
        self.assertEqual(data['format_version'],7);self.assertEqual(len(data['profile_versions']),5)
        self.assertNotIn('encrypted_key',json.dumps(data))
        db=sqlite3.connect(self.service.backup()['path'])
        try:self.assertEqual(db.execute('SELECT count(*) FROM profile_heads').fetchone()[0],4)
        finally:db.close()

    async def test_profile_management_admin_only(self):
        app=create_app(self.paths,self.model)
        async with TestServer(app) as server,ClientSession() as client:
            for role,status in (('bridge',403),('admin',200)):
                async with client.post(server.make_url('/v1/manage'),headers={'Authorization':'Bearer '+self.cfg.token(role)},json={'action':'profile.current'}) as response:
                    self.assertEqual(response.status,status)

    async def test_explicit_restore_records_new_version_even_when_content_matches(self):
        old=self.profile.current('core')
        new=self.profile.restore(old['id'],old['id'],'restore-same-content')
        self.assertEqual(new['revision'],2)
        self.assertEqual(new['restored_from'],old['id'])
        self.assertEqual(new['data'],old['data'])

    async def test_daily_memory_update_does_not_edit_profile_or_relation(self):
        from persona_runtime.organization import OrganizationEngine
        await self.send('虚构偏好：喜欢桂花茶')
        self.cfg.update({'organization':{'model':{'base_url':'http://127.0.0.1:1','name':'offline-organizer'}}})
        class Organizer:
            async def complete(self,model,messages):
                event=next(e for e in json.loads(messages[-1]['content'])['events'] if e['role']=='user')
                sources=[{'event_id':event['id'],'quote':event['text']}]
                return json.dumps({'format_version':1,'summary':[{'text':event['text'],'sources':sources}],
                    'candidates':[{'category':'preference','text':event['text'],'sources':sources,'conflicts':[]}]}),None
        org=OrganizationEngine(self.store,self.cfg,Organizer(),self.chat)
        before={kind:version['id'] for kind,version in self.profile.current().items()}
        org.schedule.run_daily_now();await org.run_next();org.schedule.daily_tick()
        self.assertEqual(len(self.service.search_memories()),1)
        self.assertEqual(before,{kind:version['id'] for kind,version in self.profile.current().items()})


if __name__=='__main__':unittest.main()
