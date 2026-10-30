"""M6.0 canon, migration, isolation and request contracts. Fictional isolated D-drive data."""
import asyncio
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from persona_runtime.character import CharacterService, empty_character, validate_character, read_fact
from persona_runtime.character_context import select_context, render_parts
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.models import LOCAL_USER_ID, RuntimeErrorBase, generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store, SCHEMA, SCHEMA2, SCHEMA3, SCHEMA4, SCHEMA5, SCHEMA6, execute_schema


def lore(entry='book', **values):
    return {'id':entry,'title':'书店朋友','category':'人物','keywords':['书店'],
            'text':'角色设定中的朋友经营书店；不是用户真实共同经历。','priority':0,
            'enabled':True,'visibility':'conversation','dependencies':[],**values}


class CharacterTests(unittest.TestCase):
    def setUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='m6-test-',dir=root);self.addCleanup(self.tmp.cleanup)
        self.paths=RuntimePaths(Path(self.tmp.name));self.store=Store(self.paths);self.store.initialize()
        self.service=RuntimeService(self.store);self.persona=self.service.create_persona(generic_persona())
        self.pid=self.persona['id'];self.service.use_persona(self.pid);self.characters=CharacterService(self.store)

    def save(self,data,expected=None,operation=None):
        return self.characters.update(self.pid,data,expected or self.characters.current(self.pid)['id'],operation or identifier())

    def test_empty_identity_and_noop(self):
        current=self.characters.current(self.pid)
        self.assertEqual(current['data'],empty_character())
        self.assertEqual(current['actor'],'create')
        self.assertEqual(self.save(current['data'])['id'],current['id'])
        self.assertEqual(len(self.characters.history(self.pid)),1)

    def test_edit_idempotency_conflict_restore(self):
        old=self.characters.current(self.pid);data=deepcopy(old['data']);data['appearance']['height_cm']=162
        operation=identifier();v=self.save(data,old['id'],operation)
        self.assertEqual(self.save(data,old['id'],operation)['id'],v['id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):self.save(empty_character(),old['id'],operation)
        with self.assertRaisesRegex(RuntimeErrorBase,'version_conflict'):self.save(data,old['id'])
        restored=self.characters.restore(self.pid,old['id'],v['id'],identifier())
        self.assertEqual(restored['data'],old['data']);self.assertEqual(restored['restored_from'],old['id'])
        again=self.characters.restore(self.pid,old['id'],restored['id'],identifier())
        self.assertNotEqual(again['id'],restored['id'])
        self.assertIn('appearance.height_cm',self.characters.compare(self.pid,old['id'],v['id'])['changes'])

    def test_scope_and_selection(self):
        old=self.characters.current(self.pid)
        other=self.service.create_persona(generic_persona())['id'];self.service.use_persona(other)
        with self.assertRaisesRegex(RuntimeErrorBase,'selection_conflict'):self.save(old['data'],old['id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'record_not_in_selected'):self.characters.show(other,old['id'])

    def test_fact_unset_unknown_and_old_version(self):
        old=self.characters.current(self.pid)
        self.assertEqual(self.characters.fact(self.pid,'appearance.blood_type')['status'],'unset')
        with self.assertRaisesRegex(RuntimeErrorBase,'unknown_character_path'):self.characters.fact(self.pid,'__class__')
        data=empty_character();data['appearance']['height_cm']=162;self.save(data)
        self.assertEqual(self.characters.fact(self.pid,'appearance.height_cm')['value'],162)
        self.assertIsNone(self.characters.fact(self.pid,'appearance.height_cm',old['id'])['value'])

    def test_field_and_numeric_validation(self):
        for value in (True,-1,0,float('inf'),float('nan'),'162'):
            with self.subTest(value=value):
                data=empty_character();data['appearance']['height_cm']=value
                with self.assertRaises(RuntimeErrorBase):validate_character(data)
        data=empty_character();data['identity']['name']='duplicate owner'
        with self.assertRaises(RuntimeErrorBase):validate_character(data)
        data=empty_character();data['script']='open_private_data()'
        with self.assertRaises(RuntimeErrorBase):validate_character(data)

    def test_age_policies_and_leap_day(self):
        data=empty_character();data['age'].update(policy='calendar',birth_date='2000-02-29')
        with patch('persona_runtime.character.today_shanghai',return_value=date(2026,10,27)):
            v=self.save(data)
        self.assertEqual(read_fact(v,'age.years',date(2025,2,27))['value'],24)
        self.assertEqual(read_fact(v,'age.years',date(2025,2,28))['value'],25)
        self.assertEqual(read_fact(v,'age.years',date(2024,2,28))['value'],23)
        data['age']['birthday']='03-01'
        with self.assertRaisesRegex(RuntimeErrorBase,'birthday_conflict'):validate_character(data)
        data=empty_character();data['age'].update(policy='fixed',fixed_age=20,birthday='02-29')
        self.assertEqual(read_fact(self.save(data),'age.years')['value'],20)
        for age in ({'policy':'calendar','fixed_age':None,'birth_date':None,'birthday':None},
                    {'policy':'fixed','fixed_age':None,'birth_date':None,'birthday':None},
                    {'policy':'unspecified','fixed_age':1,'birth_date':None,'birthday':'02-30'}):
            data=empty_character();data['age']=age
            with self.assertRaises(RuntimeErrorBase):validate_character(data)

    def test_lore_dependency_validation(self):
        cases=[ [lore(dependencies=['missing'])],
                [lore('a',dependencies=['b']),lore('b',dependencies=['a'])],
                [lore('a',dependencies=['b']),lore('b',visibility='creator_only')],
                [lore('a'),lore('a')] ]
        for rows in cases:
            data=empty_character();data['lorebook']=rows
            with self.assertRaises(RuntimeErrorBase):validate_character(data)

    def test_lore_privacy_order_atomic_groups_and_depth(self):
        data=empty_character();data['lorebook']=[lore('b'),lore('a'),lore('private',visibility='creator_only',priority=100)]
        result=select_context(self.save(data),'书店')
        self.assertEqual(result['lore_ids'],['a','b']);self.assertNotIn('private',result['text'])
        data['lorebook']=[lore('a',dependencies=['b']),lore('b',text='长'*2000)]
        result=select_context(self.save(data),'书店')
        self.assertEqual(result['lore_ids'],[])
        data['lorebook']=[lore('a',dependencies=['b']),lore('b',keywords=[],dependencies=['c']),
                          lore('c',keywords=[],dependencies=['d']),lore('d',keywords=[])]
        result=select_context(self.save(data),'书店')
        self.assertEqual(result['lore_ids'],[])

    def test_example_limits_and_casefold(self):
        data=empty_character();data['examples']=[{'id':str(i),'scenario':'鼓励','keywords':['BUG'],'text':'这次找到原因了','enabled':True} for i in range(4)]
        result=select_context(self.save(data),'bug')
        self.assertEqual(result['example_ids'],['0','1']);self.assertLessEqual(len(result['text']),3000)
        self.assertEqual(select_context(self.characters.current(self.pid),'早上好')['text'],'')

    def test_exact_unset_and_not_user_fact(self):
        data=empty_character();data['appearance']['height_cm']=162;v=self.save(data)
        result=select_context(v,'你身高和血型是什么')
        self.assertIn('appearance.height_cm',result['fact_paths']);self.assertIn('unset',result['text'])
        self.assertEqual(select_context(v,'我身高175')['fact_paths'],[])
        self.assertEqual(self.characters.current(self.pid)['data']['appearance']['height_cm'],162)

    def test_capacity_without_truncation(self):
        data=empty_character();data['lorebook']=[lore(str(i),text='设'*2000) for i in range(100)]
        with self.assertRaisesRegex(RuntimeErrorBase,'too_large'):self.save(data)
        self.assertEqual(self.characters.current(self.pid)['revision'],1)
        for field,rows in [('lorebook',[lore(str(i)) for i in range(101)]),
                           ('examples',[{'id':str(i),'scenario':'a','keywords':[],'text':'b','enabled':True} for i in range(51)])]:
            data=empty_character();data[field]=rows
            with self.assertRaises(RuntimeErrorBase):validate_character(data)

    def test_package_export_import_and_private_exclusion(self):
        data=empty_character();data['appearance']['height_cm']=162;self.save(data)
        session=self.service.create_session();self.service.append_event(session['id'],'user','私人历史')
        exported=self.characters.export(self.pid)
        package=json.loads(Path(exported['path']).read_text(encoding='utf-8'))
        self.assertEqual(set(package),{'format','persona_spec','character'})
        self.assertNotIn('私人历史',json.dumps(package,ensure_ascii=False))
        operation=identifier();other=self.characters.import_package(self.pid,package,operation)
        self.assertNotEqual(other['id'],self.pid)
        self.assertEqual(self.characters.import_package(self.pid,package,operation)['id'],other['id'])
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM events WHERE persona_id=?',(other['id'],)).fetchone()[0],0)
            self.assertEqual(Store.current(conn),self.pid)
        self.assertEqual(self.characters.fact(other['id'],'appearance.height_cm')['value'],162)
        full=json.loads(Path(self.service.export_persona()['path']).read_text(encoding='utf-8'))
        self.assertEqual(full['format_version'],15);self.assertIn('character_versions',full)

    def test_migrations_one_through_six_preserve_old_columns(self):
        schemas=[SCHEMA,SCHEMA2,SCHEMA3,SCHEMA4,SCHEMA5,SCHEMA6]
        for version in range(1,7):
            with self.subTest(version=version):
                paths=RuntimePaths(self.paths.home/('legacy'+str(version)));paths.initialize();store=Store(paths)
                conn=sqlite3.connect(store.path)
                for schema in schemas[:version]:execute_schema(conn,schema)
                conn.execute('INSERT INTO users VALUES (?,?,?)',(LOCAL_USER_ID,'old','old-time'))
                spec=generic_persona();pid=identifier()
                conn.execute('INSERT INTO personas VALUES (?,?,?,?,?)',(pid,spec.name,1,json.dumps(spec.to_dict()),'old-time'))
                if version==6:
                    conn.row_factory=sqlite3.Row
                    from persona_runtime.profile import initialize_profiles
                    initialize_profiles(conn)
                conn.execute('PRAGMA user_version='+str(version));conn.commit()
                before=conn.execute('SELECT * FROM personas').fetchall();conn.close()
                store.initialize();store.initialize()
                with store.connection() as conn:
                    self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM personas')],[tuple(r) for r in before])
                    self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],18)
                    self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                    self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
                    self.assertEqual(conn.execute('SELECT actor FROM character_versions').fetchone()[0],'migration')
                backups=list((paths.home/'backups').glob('*.sqlite3'));self.assertEqual(len(backups),1)
                copy=sqlite3.connect(backups[0]);self.assertEqual(copy.execute('PRAGMA user_version').fetchone()[0],version);copy.close()

    def test_migration_rollback(self):
        # Build an actual schema-6 database and make character initialization fail after DDL.
        paths=RuntimePaths(self.paths.home/'rollback');paths.initialize();store=Store(paths)
        conn=sqlite3.connect(store.path)
        for schema in [SCHEMA,SCHEMA2,SCHEMA3,SCHEMA4,SCHEMA5,SCHEMA6]:execute_schema(conn,schema)
        conn.execute('INSERT INTO users VALUES (?,?,?)',(LOCAL_USER_ID,'old','time'))
        conn.execute('PRAGMA user_version=6');conn.commit();conn.close()
        with patch('persona_runtime.character.initialize_characters',side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):store.initialize()
        conn=sqlite3.connect(store.path)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],6)
        self.assertNotIn('character_snapshot',[r[1] for r in conn.execute('PRAGMA table_info(turns)')]);conn.close()


class CaptureModel:
    def __init__(self):self.calls=[];self.started=asyncio.Event();self.gate=asyncio.Event();self.fail=False
    async def complete(self,model,messages):
        self.calls.append(messages);self.started.set();await self.gate.wait()
        if self.fail:
            from persona_runtime.chat import ModelError
            raise ModelError('provider_timeout')
        return '离线角色回答',None


class CharacterChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='m6-chat-',dir=root);self.addCleanup(self.tmp.cleanup)
        self.paths=RuntimePaths(Path(self.tmp.name));self.store=Store(self.paths);self.store.initialize()
        self.cfg=ConfigStore(self.paths);self.cfg.initialize();self.cfg.update({'model':{'base_url':'http://127.0.0.1:1','name':'offline','hourly_limit':1000}})
        self.service=RuntimeService(self.store);self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.characters=CharacterService(self.store);self.model=CaptureModel();self.chat=ChatEngine(self.store,self.cfg,self.model)

    async def asyncTearDown(self):
        self.model.gate.set();await self.chat.wait()

    def save_height(self,height):
        current=self.characters.current(self.pid);data=current['data'];data['appearance']['height_cm']=height
        return self.characters.update(self.pid,data,current['id'],identifier())

    async def test_queued_character_snapshot_and_retry_history(self):
        v1=self.save_height(162)
        first=await self.chat.submit('desktop','你好',identifier());await self.model.started.wait()
        second=await self.chat.submit('desktop','你多高',identifier())
        self.save_height(165);self.model.gate.set();await self.chat.wait()
        self.assertIn('162',json.dumps(self.model.calls[1],ensure_ascii=False))
        self.assertNotIn('165',json.dumps(self.model.calls[1],ensure_ascii=False))
        self.assertEqual(self.chat.get(second['id'])['context']['character']['version_id'],v1['id'])
        self.model.fail=True
        third=await self.chat.submit('desktop','你多高',identifier());await self.chat.wait()
        self.save_height(168);self.model.fail=False
        await self.chat.retry(third['id']);await self.chat.wait()
        self.assertIn('168',json.dumps(self.model.calls[-1],ensure_ascii=False))
        with self.store.connection() as conn:
            contexts=list(conn.execute('SELECT details_json FROM context_uses WHERE turn_id=?',(third['id'],)))
            self.assertEqual(len(contexts),2)
            self.assertNotEqual(json.loads(contexts[0][0])['character']['version_id'],json.loads(contexts[1][0])['character']['version_id'])

    async def test_legacy_snapshot_not_backfilled(self):
        self.save_height(162)
        async with self.chat.locks[self.pid]:
            turn=await self.chat.submit('desktop','你多高',identifier())
            with self.store.connection(write=True) as conn:conn.execute('UPDATE turns SET character_snapshot=NULL WHERE id=?',(turn['id'],))
        self.model.gate.set();await self.chat.wait()
        context=self.chat.get(turn['id'])['context']
        self.assertIsNone(context['character']);self.assertEqual(context['character_snapshot_origin'],'legacy_without_character')

    async def test_api_admin_only_preview_no_events(self):
        async with TestServer(create_app(self.paths,client=self.model)) as server:
            async with ClientSession() as client:
                url=server.make_url('/v1/manage');payload={'action':'character.current','args':{'persona_id':self.pid}}
                response=await client.post(url,json=payload,headers={'Authorization':'Bearer '+self.cfg.token('bridge')})
                self.assertEqual(response.status,403)
                response=await client.post(url,json=payload,headers={'Authorization':'Bearer '+self.cfg.token()})
                self.assertEqual(response.status,200)
                payload['action']='character.preview';payload['args']['text']='你多高'
                response=await client.post(url,json=payload,headers={'Authorization':'Bearer '+self.cfg.token()})
                self.assertEqual(response.status,200);self.assertIn('fact_paths',await response.json())
        self.assertEqual(self.model.calls,[])
        with self.store.connection() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM events').fetchone()[0],0)

    async def test_global_budget_drops_optional_canon_first(self):
        current=self.characters.current(self.pid);data=current['data'];data['social']['public_image']='温柔'*500
        data['lorebook']=[lore(text='书店'*400)]
        self.characters.update(self.pid,data,current['id'],identifier())
        self.cfg.update({'model':{'max_input_chars':1100}})
        self.model.gate.set();turn=await self.chat.submit('desktop','你多高，书店怎么样',identifier());await self.chat.wait()
        view=self.chat.get(turn['id']);self.assertEqual(view['status'],'completed')
        self.assertIn('appearance.height_cm',view['context']['character']['fact_paths'])
        self.assertTrue(view['context']['character']['removed']);self.assertLessEqual(view['context']['input_characters'],1100)


if __name__=='__main__':unittest.main()
