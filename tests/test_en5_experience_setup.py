"""Isolated setup, consent, exact input admission and the foreground-only worker."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from experiments.corpus_reply_study import read_private, save_once
from experiments.en5_experience_replay import plan_for
from experiments import en5_experience_setup as setup
from persona_runtime.character import CharacterService
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.knowledge_routing import configured_model
from persona_runtime.models import RuntimeErrorBase, generic_persona
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch
from persona_runtime.security import protect
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class ExperienceHome(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder=Path(self.tmp.name)
        self.source=RuntimePaths(folder/'source')
        cfg=ConfigStore(self.source);cfg.initialize()
        model={'base_url':'https://api.deepseek.com','name':'deepseek-v4-pro','max_tokens':700,
               'timeout':120,'max_input_chars':24000}
        cfg.update({'model':model|{'api_key':'ID-LOCAL-KEY'}})
        atomic_json(self.source.home/'private/knowledge-router/config.json',
                    {'base_url':'https://example.invalid','name':'ID-J','encrypted_key':protect('ID-J-KEY')})
        store=Store(self.source);store.initialize()
        pid=RuntimeService(store).create_persona(generic_persona())['id']
        self.source_material={'spec':generic_persona().to_dict(),'character':CharacterService(store).current(pid)['data'],
             'source_private_id':'ID-X','source_persona_id':pid,'source_character_version':'ID-V',
             'contract_sha256':'0'*64,'model':model}
        self.enterContext(patch.object(setup,'material_source',return_value=self.source_material))
        self.root=folder/'trial';self.state=folder/'state.json'
        self.b=setup.prepare(self.root,self.state,self.source.home,mode='author_material_replay')
        self.paths=RuntimePaths(self.root/'home')
        self.material={'sessions':[{'part':part,'turns':[{'n':n,'text':f'ID-{part}-{n}',
             'bubbles':[{'text':f'ID-{part}-{n}'}], 'public_task':{'question':'ID-P'} if n==2 else None}
              for n in range(1,16)]} for part in (1,2)]}
        self.mhash=save_once(self.root/'materials.private.json',self.material)
        self.chash=save_once(self.root/'input-contract.private.json',{'source':self.mhash})

    def consent(self):
        info=setup.use_scope(self.root,'author_material_replay',self.mhash)
        save_once(self.root/'author-use.private.json',info|{'approved':True,'author_message':'ID-APPROVAL',
                                                           'input_contract_sha256':self.chash})
        setup.activate(self.root)

    def test_zero_call_prepare_is_idempotent_and_starts_in_first_session(self):
        self.assertEqual(setup.prepare(self.root,self.state,self.source.home,mode='author_material_replay'),self.b)
        self.assertEqual(setup.select_part(self.paths,self.b,1),self.b['sessions'][0])
        with self.assertRaisesRegex(RuntimeErrorBase,'first_part_incomplete'):setup.select_part(self.paths,self.b,2)
        self.assertFalse(ConfigStore(self.paths).load()['model']['encrypted_key'])
        self.assertFalse((self.paths.home/'private/knowledge-router/config.json').exists())
        with self.assertRaisesRegex(RuntimeErrorBase,'specific_use_approval'):setup.activate(self.root)
        self.assertFalse((self.root/'approved-slots.private.json').exists())
        with Store(self.paths).connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT session_id FROM session_heads').fetchone()[0],self.b['sessions'][0])

    async def test_replay_approval_enforces_exact_order_and_current_source(self):
        self.consent()
        result=setup.activate(self.root)
        self.assertEqual(result['new_model_requests'],0)
        store=Store(self.paths);cfg=ConfigStore(self.paths);pid=self.b['persona_id']
        main=cfg.load()['model']
        for role,model in [('main',main),('knowledge_router',configured_model(self.paths)),('knowledge_native',setup.flash_model(main))]:
            guard_cloud_dispatch(store,pid,role,model)
        with self.assertRaises(RuntimeErrorBase):guard_cloud_dispatch(store,pid,'main',setup.flash_model(main))
        engine=ChatEngine(store,cfg); self.addCleanup(engine.knowledge.close)
        with self.assertRaisesRegex(RuntimeErrorBase,'en5_desktop_ingress_required'):
            await engine.submit('desktop','ID-1-1','BAD-ID')
        ingress=IngressService(engine)
        batch=plan_for(self.material,self.b,1,self.mhash)
        def value(index):
            entry=batch[index][0]
            v={'text':entry['text'],'request_id':entry['request_id'],'channel':'desktop','persona_id':pid,
               'session_id':self.b['sessions'][0],'desktop_presentation':'tk-visible-v1'}
            if entry['public_task']:v['public_task']=entry['public_task']
            return v
        first=value(0)
        for bad in (value(1),first|{'request_id':'BAD-ID'},first|{'text':'BAD-TEXT'},first|{'public_task':{'question':'BAD-TASK'}}):
            with self.assertRaisesRegex(RuntimeErrorBase,'replay_input_not_approved'):ingress.accept(bad)
        result=ingress.accept(first)
        self.assertEqual(ingress.accept(first)['receipt_id'],result['receipt_id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'replay_input_not_approved'):ingress.accept(value(1))
        (self.root/'author-use.private.json').unlink()
        with self.assertRaisesRegex(RuntimeErrorBase,'en5_specific_use_unavailable'):guard_cloud_dispatch(store,pid,'main',main)
        with self.assertRaisesRegex(RuntimeErrorBase,'replay_input_not_approved'):ingress.accept(value(2))
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM ingress_receipts').fetchone()[0],1)

    async def test_real_loopback_launcher_opens_bound_session_and_cleans_owned_server(self):
        self.consent()
        seen=[]
        async def arrived(window,app,binding,part):
            seen.append((window.session_id,window.persona_id,window.chat_persona.current(),part,
                         app['organization'].foreground_only,app.get('session_worker')))
            return 'ID-READY'
        import asyncio
        from persona_runtime.desktop import Workbench
        class OffscreenWorkbench(Workbench):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs)
                self.geometry('900x680+20000+20000')
        with patch('persona_runtime.desktop.Workbench',OffscreenWorkbench):
            result=await asyncio.wait_for(setup.run_desktop(self.root,1,arrived,material_sha256=self.mhash),14)
        self.assertEqual(result,'ID-READY')
        self.assertEqual(seen,[(self.b['sessions'][0],self.b['persona_id'],0,1,True,None)])
        with Store(self.paths).connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0],0)

    def test_setup_rejects_an_existing_unowned_home(self):
        foreign=self.root.parent/'other'
        (foreign/'home').mkdir(parents=True)
        (foreign/'home'/'original').write_text('ID',encoding='utf8')
        with self.assertRaises((OSError,ValueError)):setup.prepare(foreign,self.state,self.source.home)
        self.assertEqual((foreign/'home'/'original').read_text(encoding='utf8'),'ID')
