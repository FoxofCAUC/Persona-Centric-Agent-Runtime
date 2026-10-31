"""Artificial identities only; no formal persona, QQ or cloud calls."""
import asyncio
import gc
import json
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import unittest
from unittest.mock import patch
from persona_runtime import storage
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.models import generic_persona,identifier,RuntimeErrorBase
from persona_runtime.persona_management import PersonaManagement
from persona_runtime.character import CharacterService,empty_character
from persona_runtime.config import ConfigStore
from persona_runtime.private_personas import PrivatePersonas,guard_cloud_dispatch
from tests import test_desktop as desktop_base


class ManagementTests(unittest.TestCase):
    def setUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='persona-crud-',dir=root);self.addCleanup(self.tmp.cleanup)
        self.paths=RuntimePaths(Path(self.tmp.name));self.store=storage.Store(self.paths);self.store.initialize()
        self.cfg=ConfigStore(self.paths);self.cfg.initialize()
        self.service=RuntimeService(self.store);self.a=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.a)
        self.b=self.service.create_persona(generic_persona())['id'];self.m=PersonaManagement(self.store)

    def clone(self,pid=None,op=None):
        pid=pid or self.a
        return self.m.clone(pid,self.m.inspect(pid)['versions'],self.service.show_persona()['id'],op or identifier())

    def test_clone_canon_without_state_history_policy_or_switch(self):
        c=CharacterService(self.store);v=c.current(self.a);data=empty_character();data['appearance']['height_cm']=168
        c.update(self.a,data,v['id'],identifier())
        session=self.service.create_session();event=self.service.append_event(session['id'],'user','人工历史')
        self.service.add_memory('shared_experience','人工记忆',event['id'])
        copied=self.clone()['id']
        self.assertNotEqual(copied,self.a);self.assertEqual(self.service.show_persona()['id'],self.a)
        self.assertEqual(self.service.show_persona(copied)['spec'],self.service.show_persona(self.a)['spec'])
        self.assertEqual(c.current(copied)['data'],data)
        with self.store.connection() as db:
            for table in ('events','memories','turns','sessions','context_manifests','organization_tasks'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE persona_id=?',(copied,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM events WHERE persona_id=?',(self.a,)).fetchone()[0],1)

    def test_copy_idempotency_concurrency_and_conflict(self):
        versions=self.m.inspect(self.a)['versions'];op=identifier()
        def run():return self.m.clone(self.a,versions,self.a,op)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:run(),range(2)))
        self.assertEqual(results[0]['id'],results[1]['id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):self.m.clone(self.b,versions,self.a,op)

    def test_stale_version_and_selection_rejected(self):
        with self.assertRaisesRegex(RuntimeErrorBase,'version_conflict'):self.m.clone(self.a,{},self.a,identifier())
        self.service.use_persona(self.b)
        with self.assertRaisesRegex(RuntimeErrorBase,'persona_selection_conflict'):self.m.clone(self.a,self.m.inspect(self.a)['versions'],self.a,identifier())
        with self.assertRaisesRegex(RuntimeErrorBase,'version_conflict'):self.m.trash(self.a,9,self.b,identifier())
        self.assertFalse(self.m.inspect(self.a)['deleted'])

    def test_trash_restore_retains_identity_and_history(self):
        session=self.service.create_session();self.service.append_event(session['id'],'user','人工保留历史')
        self.service.use_persona(self.b);op=identifier()
        result=self.m.trash(self.a,0,self.b,op)
        self.assertTrue(result['deleted']);self.assertTrue(self.m.trash(self.a,0,self.b,op)['already_applied'])
        self.assertNotIn(self.a,[p['id'] for p in self.service.list_personas()])
        self.assertIn(self.a,[p['id'] for p in self.service.list_personas(True)])
        with self.assertRaisesRegex(RuntimeErrorBase,'persona_in_trash'):self.service.use_persona(self.a)
        with self.assertRaisesRegex(RuntimeErrorBase,'persona_in_trash'):CharacterService(self.store).current(self.a)
        self.m.restore(self.a,1,self.b,identifier());self.service.use_persona(self.a)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM events WHERE persona_id=?',(self.a,)).fetchone()[0],1)

    def test_current_and_pending_work_rejected(self):
        with self.assertRaisesRegex(RuntimeErrorBase,'switch_persona_before_delete'):self.m.trash(self.a,0,self.a,identifier())
        from persona_runtime.chat import ChatEngine
        engine=ChatEngine(self.store,self.cfg)
        try:
            with patch.object(engine,'_schedule'):asyncio.run(engine.submit('desktop','人工未决输入',identifier()))
            self.service.use_persona(self.b)
            with self.assertRaisesRegex(RuntimeErrorBase,'persona_has_pending_work'):self.m.trash(self.a,0,self.b,identifier())
        finally:engine.cognitive.characters.close()

    def test_copy_transaction_failure_rolls_back(self):
        before=len(self.service.list_personas())
        with patch('persona_runtime.character.append',side_effect=OSError('disk fixture')):
            with self.assertRaises(OSError):self.clone()
        self.assertEqual(len(self.service.list_personas()),before)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM persona_operations').fetchone()[0],0)

    def test_disposed_legacy_delivery_does_not_block_trash(self):
        from persona_runtime.chat import ChatEngine
        from persona_runtime.models import LOCAL_USER_ID,timestamp
        engine=ChatEngine(self.store,self.cfg)
        try:
            with patch.object(engine,'_schedule'):turn=asyncio.run(engine.submit('desktop','人工历史回合',identifier()))
            with self.store.connection(write=True) as db:
                db.execute("UPDATE turns SET status='failed' WHERE id=?",(turn['id'],))
                db.execute("INSERT INTO deliveries VALUES(?,'uncertain',?)",(turn['id'],timestamp()))
            self.service.use_persona(self.b)
            with self.assertRaisesRegex(RuntimeErrorBase,'persona_has_pending_work'):self.m.trash(self.a,0,self.b,identifier())
            with self.store.connection(write=True) as db:
                db.execute('INSERT INTO legacy_delivery_dispositions VALUES(?,?,?,?,?,?)',(turn['id'],self.a,LOCAL_USER_ID,'人工放弃后续发送','admin',timestamp()))
            self.assertTrue(self.m.trash(self.a,0,self.b,identifier())['deleted'])
        finally:engine.cognitive.characters.close()

    def test_private_copy_inherits_privacy_initial_relationship_and_cloud_boundary(self):
        private=PrivatePersonas(self.paths);package={'format':'pcar_character_v1','persona_spec':generic_persona().to_dict(),'character':empty_character()}
        source=self.paths.home/'artificial.json';source.write_text(json.dumps(package),encoding='utf-8')
        archive=self.paths.home/'artificial.md';archive.write_text('人工文学关系',encoding='utf-8')
        rec=private.register(source,archive,'人工文学关系');rec=private.import_original(self.store,rec['id']);pid=rec['persona_id']
        cloned=self.clone(pid)['id'];self.assertTrue(private.protected(cloned))
        self.assertTrue(private.check(self.store,rec['id'],cloned)['literary_unchanged'])
        with self.assertRaisesRegex(RuntimeErrorBase,'private_original_delete_forbidden'):self.m.trash(pid,0,self.a,identifier())
        with self.assertRaisesRegex(RuntimeErrorBase,'private_persona_acceptance_forbidden'):private.assert_acceptance(cloned)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):guard_cloud_dispatch(self.store,cloned,'main',{'name':'other'})
        self.m.trash(cloned,0,self.a,identifier());self.m.restore(cloned,1,self.a,identifier())
        self.assertTrue(private.protected(cloned))

    def test_export_contains_metadata_without_credentials(self):
        pid=self.clone()['id'];self.service.use_persona(pid)
        value=json.loads(Path(self.service.export_persona()['path']).read_text(encoding='utf-8'))
        self.assertEqual(value['format_version'],16);self.assertEqual(value['persona_lifecycle'][0]['copy_of'],self.a)
        self.assertEqual(len(value['persona_operations']),1);self.assertNotIn('encrypted_key',json.dumps(value))

    def test_schema15_upgrade_repeat_and_failure_rollback(self):
        paths=RuntimePaths(self.paths.home/'old15');old=storage.Store(paths)
        with patch.object(storage,'SCHEMA_VERSION',15):
            old.initialize();service=RuntimeService(old);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
        with closing(sqlite3.connect(old.path)) as db:original=db.execute('SELECT * FROM personas').fetchall()
        real=storage.execute_schema
        def fail(db,sql):
            if 'CREATE TABLE persona_lifecycle' in sql:raise OSError('migration fixture')
            real(db,sql)
        with patch.object(storage,'execute_schema',side_effect=fail),self.assertRaises(OSError):old.initialize()
        with closing(sqlite3.connect(old.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],15)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='persona_lifecycle'").fetchone())
        old.initialize();old.initialize()
        with old.connection() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM personas')],original)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],19)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())


class PersonaUITests(unittest.TestCase):
    setUp=desktop_base.DesktopTests.setUp
    window=desktop_base.DesktopTests.window

    def doCleanups(self):
        for w in self.windows:w.hub.pool.shutdown(wait=True,cancel_futures=True);w.hub.drain()
        result=super().doCleanups();self.windows.clear();gc.collect();return result

    def test_buttons_and_cancel_does_not_write(self):
        w=self.window();w.show_page('persona');w.update()
        self.assertTrue({'复制选中人格','删除到回收站','恢复选中人格'}<={b.cget('text') for b,_ in w.buttons})
        w.persona_list.insert('end','人工');w.persona_rows=[{'id':'source'}];w.persona_list.selection_set(0);pending=[]
        with patch.object(w,'run_task',side_effect=lambda *a,**k:pending.append(a)),patch('persona_runtime.desktop.messagebox.askyesno',return_value=False):
            w.manage_selected_persona('clone');pending[0][3]({'id':'source','name':'人工','deleted':False})
        self.assertEqual(len(pending),1)

    def test_late_selection_does_not_open_confirmation_or_write(self):
        w=self.window();w.persona_list.insert('end','A','B');w.persona_rows=[{'id':'a'},{'id':'b'}];w.persona_list.selection_set(0);pending=[]
        with patch.object(w,'run_task',side_effect=lambda *a,**k:pending.append(a)),patch('persona_runtime.desktop.messagebox.askyesno') as confirm:
            w.manage_selected_persona('trash');w.persona_list.selection_clear(0,'end');w.persona_list.selection_set(1)
            pending[0][3]({'id':'a'});confirm.assert_not_called();self.assertEqual(len(pending),1)

    def test_confirmed_clone_sends_frozen_versions_and_operation(self):
        w=self.window();w.persona_list.insert('end','人工');w.persona_rows=[{'id':'source'}];w.persona_list.selection_set(0);pending=[]
        value={'id':'source','name':'人工','deleted':False,'versions':{'core':'c','expression':'e','character':'v'},'current_persona_id':'current'}
        with patch.object(w,'run_task',side_effect=lambda *a,**k:pending.append(a)),patch('persona_runtime.desktop.messagebox.askyesno',return_value=True),patch.object(w.client,'manage',return_value={}) as manage:
            w.manage_selected_persona('clone');pending[0][3](value);pending[1][2](None)
        args=manage.call_args.kwargs
        self.assertEqual(args['expected_versions'],value['versions']);self.assertEqual(args['expected_selected'],'current')
        self.assertTrue(args['operation_id']);self.assertEqual(manage.call_args.args[0],'persona.clone')

    def test_trash_never_enters_chat_combobox(self):
        w=self.window();w.show_deleted_personas.set(True);callbacks=[]
        with patch.object(w,'run_task',side_effect=lambda *a,**k:callbacks.append(a[3])):
            type(w).refresh_personas(w)
        callbacks[0]([{'id':'a','name':'人工A','selected':True},{'id':'b','name':'人工B','selected':False,'deleted':True}])
        self.assertEqual([r['id'] for r in w.personas],['a']);self.assertEqual(len(w.chat_persona['values']),1)
        w.persona_list.selection_set(1)
        with patch.object(w,'use_persona') as use:w.use_selected_persona();use.assert_not_called()


class ManagementAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ManagementTests.setUp(self)
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestServer
        from persona_runtime.server import create_app
        self.server=TestServer(create_app(self.paths));await self.server.start_server();self.addAsyncCleanup(self.server.close)
        self.client=ClientSession();self.addAsyncCleanup(self.client.close)

    async def call(self,action,args,role='admin'):
        async with self.client.post(self.server.make_url('/v1/manage'),json={'action':action,'args':args},headers={'Authorization':'Bearer '+self.cfg.token(role)}) as response:
            return response.status,await response.json()

    async def test_admin_clone_trash_restore_and_bridge_forbidden(self):
        status,view=await self.call('persona.inspect',{'persona_id':self.a});self.assertEqual(status,200)
        args={'persona_id':self.a,'expected_versions':view['versions'],'expected_selected':self.a,'operation_id':identifier()}
        status,copied=await self.call('persona.clone',args);self.assertEqual(status,200)
        status,again=await self.call('persona.clone',args);self.assertEqual(again['id'],copied['id'])
        params={'persona_id':copied['id'],'expected_revision':0,'expected_selected':self.a,'operation_id':identifier()}
        status,body=await self.call('persona.trash',params);self.assertEqual(status,200);self.assertTrue(body['deleted'])
        status,rows=await self.call('persona.list',{});self.assertNotIn(copied['id'],[r['id'] for r in rows])
        status,body=await self.call('persona.restore',params|{'expected_revision':1,'operation_id':identifier()});self.assertEqual(status,200)
        status,_=await self.call('persona.clone',args,role='bridge');self.assertEqual(status,403)


if __name__=='__main__':unittest.main()
