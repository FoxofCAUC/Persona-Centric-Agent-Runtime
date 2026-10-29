"""Natural presentation and private source safeguards; only artificial data."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from persona_runtime.character import empty_character
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.models import generic_persona,identifier,RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.presentation import render,violations,CONTRACT,batch_view,assert_transport
from persona_runtime.private_personas import PrivatePersonas,publication_scan,install_hook
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


CASES=[
 ('普通中文','chat','普通中文'),('# 标题','chat','标题'),('## 标题','chat','标题'),
 ('**重点**','chat','重点'),('__重点__','chat','重点'),('*轻声*','chat','轻声'),('_轻声_','chat','轻声'),
 ('~~旧话~~','chat','旧话'),('用 `sum`','chat','用 sum'),('- 蓝色','chat','蓝色'),('+ 蓝色','chat','蓝色'),
 ('* 蓝色','chat','蓝色'),('> 原话','quote','原话'),('>> 原话','chat','原话'),
 ('[资料](https://example.com/a_b)','chat','资料（https://example.com/a_b）'),
 ('![图](https://example.com/a.png)','chat','图（https://example.com/a.png）'),
 ('https://example.com/a/**b**?q=__c__','chat','https://example.com/a/**b**?q=__c__'),
 ('预算300，身高165，v0.7.5','chat','预算300，身高165，v0.7.5'),('1. 第一\n2. 第二','chat','1. 第一\n2. 第二'),
 ('| 项 | 值 |\n| --- | --- |\n| 蓝 | 3 |','chat','项；值\n\n蓝；3'),
 ('标题\n===','chat','标题'),('- [x] 做完','chat','做完'),
 ('<https://example.com>','chat','https://example.com'),
 ('```py\nx = 2 ** 3\n```','code','x = 2 ** 3'),
 ('~~~py\n# 注释\nprint("**数据**")\n~~~','code','# 注释\nprint("**数据**")'),
 ('例子：\n```py\n# 注释\nx = 2 ** 3\n```','technical','例子：\n# 注释\nx = 2 ** 3'),
 ('    return a_b ** 2','code','    return a_b ** 2'),
 ('print("**数据**")','chat','print("**数据**")'),
 ('a ** 2 + b ** 3','technical','a ** 2 + b ** 3'),
 ('---','chat',None),('```py\nx=1','code',None),('hello\x00bad','chat',None),
]


class PlainTests(unittest.TestCase):
    def test_colloquial_age_uses_calendar_fact_in_both_selectors(self):
        from datetime import date
        from unittest.mock import patch
        from persona_runtime.character_context import select_context
        from persona_runtime.character_retrieval import assemble
        data=empty_character()
        data['age'].update(policy='calendar',birth_date='2000-06-02',birthday='06-02')
        version={'id':'artificial-version','revision':1,'data':data}
        with patch('persona_runtime.character.today_shanghai',return_value=date(2025,6,2)):
            for question in ('你多大呀？','你今年多大啦？','你现在多大了？'):
                for result in (select_context(version,question),assemble(version,[question],{'lore':[],'example':[]},2)):
                    self.assertIn('age.years',result['fact_paths'])
                    fact=next(p for p in result['parts'] if p['id']=='age.years')
                    self.assertEqual(json.loads(fact['text'])['data']['value'],25)

    def test_private_workbench_model_factory_precedes_activation(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from scripts.private_copy_workbench import configure_embedding
        runtime=SimpleNamespace(indexes={});formal=RuntimePaths(Path('D:/artificial-private-model-test'))
        with patch('scripts.private_copy_workbench.CharacterEmbeddingProvider',return_value='provider'),patch('scripts.private_copy_workbench.CharacterSemanticIndex',return_value='local-index') as index:
            configure_embedding(runtime,'store',formal)
            self.assertEqual(runtime.index_factory({}),'local-index');index.assert_called_once_with('store','provider')
        runtime.indexes={'old':'wrong-path'}
        with self.assertRaisesRegex(RuntimeError,'private_copy_embedding_setup_order'):configure_embedding(runtime,'store',formal)

    def test_displayed_persona_must_match_send_target(self):
        from persona_runtime.presentation import chat_target_matches
        rows=[{'id':'new'}]
        self.assertFalse(chat_target_matches('old',rows,0))
        self.assertTrue(chat_target_matches('new',rows,0))

    def test_32_fixed_cases(self):
        self.assertEqual(len(CASES),32)
        for raw,kind,expected in CASES:
            with self.subTest(raw=raw,kind=kind):
                if expected is None:
                    with self.assertRaises(RuntimeErrorBase):render(raw,kind)
                else:
                    value,diagnostic=render(raw,kind)
                    self.assertEqual(value,expected)
                    self.assertFalse(diagnostic['remaining_violations'])
                    if kind=='code':self.assertEqual(render(value,kind)[0],value)

    def test_raw_and_presentation_gates_are_distinct(self):
        self.assertIn('emphasis',violations('**蓝色**'))
        value,diag=render('**蓝色**');self.assertEqual(value,'蓝色')
        self.assertTrue(diag['original_violations']);self.assertFalse(diag['remaining_violations'])
        with self.assertRaises(RuntimeErrorBase):assert_transport('**蓝色**')
        assert_transport('x = 2 ** 3','code')

    def test_history_is_derived_not_overwritten(self):
        raw='**过去**';value,meta=batch_view(raw,historical=True)
        self.assertEqual(raw,'**过去**');self.assertEqual(value,'过去')
        value,meta=batch_view('```broken',historical=True)
        self.assertIn('无法安全显示',value);self.assertIn('error',meta)


class PrivateTests(unittest.TestCase):
    def setUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root,prefix='private-unit-');self.addCleanup(self.temp.cleanup)
        self.paths=RuntimePaths(Path(self.temp.name)/'home');self.paths.initialize()
        self.store=Store(self.paths);self.store.initialize();self.service=RuntimeService(self.store)
        self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.cfg=ConfigStore(self.paths);self.cfg.initialize();self.private=PrivatePersonas(self.paths)
        self.package={'format':'pcar_character_v1','persona_spec':generic_persona().to_dict(),'character':empty_character()}
        self.package['persona_spec']['name']='人工私有示例角色'
        self.package['character']['lorebook']=[{'id':'artificial','title':'人工资料','category':'fixture','keywords':['人工'],
            'text':'这是完全人工构造且只用于测试隐私发布防护的独特长句，不属于任何用户角色。','priority':0,'enabled':True,'visibility':'conversation','dependencies':[]}]
        self.source=Path(self.temp.name)/'source.json';self.source.write_text(json.dumps(self.package,ensure_ascii=False),encoding='utf-8')
        self.archive=Path(self.temp.name)/'source.md';self.archive.write_text('人工作者专用档案，仅供测试。',encoding='utf-8')
        self.rec=self.private.register(self.source,self.archive,'人工初始关系，不是实际经历。')

    def test_source_idempotency_import_and_zero_history(self):
        self.assertEqual(self.private.register(self.source,self.archive,'人工初始关系，不是实际经历。')['id'],self.rec['id'])
        r=self.private.import_original(self.store,self.rec['id']);again=self.private.import_original(self.store,self.rec['id'])
        self.assertEqual(r['persona_id'],again['persona_id']);self.assertNotEqual(r['persona_id'],self.pid)
        self.assertTrue(self.private.check(self.store,r['id'])['literary_unchanged'])
        with self.store.connection() as db:
            for table in ('events','memories','turns','summaries'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE persona_id=?',(r['persona_id'],)).fetchone()[0],0)

    def test_literary_or_original_change_fails(self):
        r=self.private.import_original(self.store,self.rec['id'])
        with self.store.connection(write=True) as db:
            db.execute("UPDATE personas SET spec_json='{}' WHERE id=?",(r['persona_id'],))
        with self.assertRaisesRegex(RuntimeErrorBase,'private_literary_content_changed'):self.private.check(self.store,r['id'])
        self.source.write_text('changed',encoding='utf-8')
        # Archived originals are independent of the external upload location.
        self.assertEqual(self.private.package(r),self.package)
        (self.private.root/r['id']/'source-package.json').write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(RuntimeErrorBase,'private_original_changed'):self.private.package(r)

    def test_original_acceptance_rejected_and_selection_guard(self):
        from persona_runtime.quotas import QuotaService
        r=self.private.import_original(self.store,self.rec['id']);self.service.use_persona(r['persona_id'])
        quota=QuotaService(self.store,self.cfg)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_persona_acceptance_forbidden'):quota.begin_acceptance()
        self.service.use_persona(self.pid);quota.begin_acceptance()
        try:
            with self.assertRaisesRegex(RuntimeErrorBase,'private_persona_acceptance_forbidden'):self.service.use_persona(r['persona_id'])
        finally:quota.end_acceptance()

    def test_clone_and_checked_cleanup(self):
        copy=self.private.create_sandbox(self.rec['id']);folder=Path(copy['home'])
        self.assertFalse((folder/'source-archive.md').exists())
        self.assertNotEqual(copy['persona_id'],self.pid)
        with self.assertRaises(RuntimeErrorBase):self.private.cleanup_sandbox(self.paths.home)
        self.assertTrue(self.private.cleanup_sandbox(folder)['local_copy_deleted'])
        self.assertTrue(self.private.file.exists())

    def test_private_cloud_provider_frozen_and_copy_protected(self):
        from persona_runtime.private_personas import guard_cloud_dispatch
        r=self.private.import_original(self.store,self.rec['id']);model=self.cfg.load()['model']
        guard_cloud_dispatch(self.store,r['persona_id'],'main',model)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(self.store,r['persona_id'],'main',{**model,'base_url':'https://other.invalid'})
        copy=self.private.create_sandbox(self.rec['id'])
        try:
            with self.assertRaises(RuntimeErrorBase):guard_cloud_dispatch(Store(RuntimePaths(Path(copy['home']))),copy['persona_id'],'selector',{})
        finally:self.private.cleanup_sandbox(copy['home'])

    def test_publication_private_blob_and_missing_registry(self):
        repo=Path(self.temp.name)/'repo';repo.mkdir();subprocess.run(['git','init','-q',str(repo)],check=True)
        (repo/'safe.txt').write_text('Generic code.',encoding='utf-8')
        self.assertTrue(publication_scan(self.paths,repo)['passed'])
        (repo/'renamed.txt').write_text(self.package['character']['lorebook'][0]['text'],encoding='utf-8')
        result=publication_scan(self.paths,repo);self.assertFalse(result['passed'])
        self.assertNotIn(self.package['character']['lorebook'][0]['text'],json.dumps(result,ensure_ascii=False))
        with self.assertRaisesRegex(RuntimeErrorBase,'private_registry_unavailable'):publication_scan(RuntimePaths(Path(self.temp.name)/'missing'),repo)

    def test_hook_preserves_predecessor_on_reinstall(self):
        repo=Path(self.temp.name)/'hooks-repo';repo.mkdir();subprocess.run(['git','init','-q',str(repo)],check=True)
        hook=repo/'.git/hooks/pre-push';hook.write_text('#!/bin/sh\nexit 0\n',encoding='utf-8')
        install_hook(self.paths,repo);install_hook(self.paths,repo)
        meta=json.loads((hook.parent/'pcar-private-guard.json').read_text())
        self.assertEqual(Path(meta['previous']).read_text(),'#!/bin/sh\nexit 0\n')
        self.assertIn(meta['previous'].replace('\\','/'),hook.read_text())
        self.assertIn(' -B ',hook.read_text())


class DialogueTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_modes_two_channels(self):
        from tests.test_m63 import M63Tests
        await M63Tests.asyncSetUp(self)
        from persona_runtime.ingress import IngressService
        policies=IngressService(self.engine)
        for channel in ('desktop','qq'):
            for mode in ('legacy_full','context_full','context_structured','natural_structured'):
                with self.subTest(channel=channel,mode=mode):
                    old=policies.current_policy(self.persona,channel)
                    policies.update_policy(self.persona,channel,'legacy_per_message',old['id'],identifier(),reply_mode=mode)
                    text='**蓝色**'
                    self.fake.text=(json.dumps({'schema_version':1,'segments':[{'text':text,'format':'plain'}]},ensure_ascii=False) if mode=='context_structured' else
                        json.dumps({'schema_version':2,'behavior_intent':'respond','messages':[{'content':text,'content_kind':'chat','cancel_if_new_input':False}]},ensure_ascii=False) if mode=='natural_structured' else text)
                    payload={'channel':channel,'text':'书桌旁放点什么好？','request_id':identifier(),'persona_id':self.persona}
                    if channel=='qq':payload.update(bot='456',sender='123',platform_message_id=identifier())
                    receipt=policies.accept(payload);turn=self.engine.get(receipt['turn_id'])
                    await self.engine.wait();turn=self.engine.get(turn['id'])
                    self.assertEqual(turn['status'],'completed');self.assertEqual(turn['display_reply'],'蓝色');self.assertEqual(turn['reply'],text)
                    self.assertTrue(turn['context']['presentation']['segments'][0]['original_violations'])
                    with self.store.connection() as db:
                        messages=self.engine._context(db,dict(db.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone()))
                    self.assertIn(CONTRACT,messages[0]['content'])
                    if channel=='qq':self.assertEqual(self.delivery.by_batch(turn['reply_batch']['id'])['parts'][-1]['text'],'蓝色')


if __name__=='__main__':unittest.main()
