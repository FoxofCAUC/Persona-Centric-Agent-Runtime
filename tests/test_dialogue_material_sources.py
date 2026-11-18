"""Current source authority, candidate scope and opt-in offline replay of approved materials."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from persona_runtime.archive import scope
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_learning import CorpusLearning
from persona_runtime.dialogue_assembly import DialogueAssembly, load_bundle, load_offline_bundle
from persona_runtime.expression_assets import asset, ExpressionAssetError
from persona_runtime.expression_corpus_sources import approval_fields, document, REPLAY, register_bundle, revoke_bundle, scope_record
from persona_runtime.models import generic_persona, identifier, PersonaSpec, RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from tests import test_dialogue_outlet as outlet
from tests import test_m9r_native_retrieval as native


class SourceScope(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(RuntimePaths(self.root/'runtime'))
        self.store.initialize()
        self.pid = RuntimeService(self.store).create_persona(generic_persona())['id']
        self.corpus = DatasetCorpusStore(self.root/'corpus')
        self.corpus.initialize()
        self.learning = CorpusLearning(self.root/'learning', self.corpus)
        self.learning.initialize()
        self.review = self.root/'review'
        self.review.write_bytes(b'R')
        with self.store.connection() as db, self.corpus.connection() as src:
            spec = db.execute('SELECT spec_json FROM personas WHERE id=?', (self.pid,)).fetchone()[0]
            model = {'base_url':'https://api.deepseek.com','name':'deepseek-flash','max_tokens':1,
                     'max_input_chars':128,'timeout':1}
            manifest = {'version':'dialogue-reviewed-material-v1','corpus_root':str(self.corpus.root),
                        'learning_root':str(self.learning.root),'corpus_id':src.execute('SELECT corpus_id FROM corpus_meta').fetchone()[0],
                        'persona_spec_hash':digest(spec.encode()), 'supports':[], 'candidate_numbers':{},
                        'speaker':'0','persona':'1','flash_system':'0','flash_user':'1',
                        'flash_model':model,'selector_model':model|{'_json_output':True},
                        'selector_system':{'role':'system','content':'0'},
                        'variables':{'time_block':{'prefix':''},'dialogue_prompt':{'line_format':''},
                                     'expression_habits_block':{'item_format':'','prefix':'','suffix_if_present':''},
                                     'planner_reasoning':{'format':''}},
                        'review_files':[{'path':str(self.review),'sha256':digest(b'R')}]}
            self.target = scope_record(db, self.store, self.pid)
        self.path = self.root/'material.json'
        self.path.write_text(json.dumps(manifest), encoding='utf-8')
        self.approval = self.root/'scope.json'
        self.approval.write_text(json.dumps(approval_fields(digest(self.path.read_bytes()),self.target)),encoding='utf-8')

    def test_research_and_candidate_approval_are_independent_and_rechecked(self):
        replay = register_bundle(self.store,self.pid,self.path)
        approved = register_bundle(self.store,self.pid,self.path,approval_path=self.approval)
        self.assertNotEqual(replay,approved)
        with self.store.connection() as db:
            load_offline_bundle(self.store,db,self.pid,replay)
            with self.assertRaisesRegex(RuntimeErrorBase,'binding_unavailable'):
                load_bundle(self.store,db,self.pid,replay)
            load_bundle(self.store,db,self.pid,approved)
        self.approval.write_bytes(b'{}')
        with self.store.connection() as db, self.assertRaisesRegex(RuntimeErrorBase,'document_changed'):
            load_bundle(self.store,db,self.pid,approved)

    def test_removal_and_local_revocation_close_existing_binding(self):
        approved = register_bundle(self.store,self.pid,self.path,approval_path=self.approval)
        self.review.unlink()
        with self.store.connection() as db, self.assertRaisesRegex(RuntimeErrorBase,'source_unavailable'):
            load_bundle(self.store,db,self.pid,approved)
        self.review.write_bytes(b'R')
        revoke_bundle(self.store,self.pid,approved)
        revoke_bundle(self.store,self.pid,approved)
        with self.store.connection() as db, self.assertRaisesRegex(RuntimeErrorBase,'binding_unavailable'):
            load_bundle(self.store,db,self.pid,approved)
        with self.assertRaisesRegex(ValueError,'previous_binding_unavailable'):
            register_bundle(self.store,self.pid,self.path,approval_path=self.approval)


class LocalTransport:
    def __init__(self):
        self.wires = []

    async def complete(self, model, wire, *, save_raw):
        self.wires.append(wire)
        body = json.dumps({'selected_ids':[1]}) if wire.get('response_format') else str(len(self.wires))
        name = wire['model']
        if wire['stream']:
            value = {'model':name,'choices':[{'finish_reason':'stop','delta':{'content':body}}]}
            raw = ('data: '+json.dumps(value)+'\n\ndata: [DONE]\n').encode()
        else:
            raw = json.dumps({'model':name,'choices':[{'finish_reason':'stop','message':{'content':body}}]}).encode()
        save_raw(raw,http_status=200,diagnostics={})


def backup(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as origin:
        with closing(sqlite3.connect(destination)) as target:
            origin.backup(target)


@unittest.skipUnless(os.environ.get('PCAR_DIALOGUE_REPLAY_STATE'), 'private offline materials not supplied')
class ReviewedIngress(unittest.IsolatedAsyncioTestCase):
    decision_transport = native.Integration.decision_transport
    transport = native.Integration.transport
    wait_for_plan = outlet.DirectOutlet.wait_for_plan
    present = outlet.DirectOutlet.present
    asyncTearDown = outlet.DirectOutlet.asyncTearDown

    async def asyncSetUp(self):
        native.Integration.setUp(self)
        from experiments.dialogue_source_replay import export_reviewed
        path, old, manifest, _, _ = export_reviewed(Path(os.environ['PCAR_DIALOGUE_REPLAY_STATE']), self.paths.home/'replay')
        material, _ = document(path)
        self.texts = [item['text'] for scene in manifest['scenes'] for item in scene['turns']]
        self.corpus = DatasetCorpusStore(self.paths.home/'local-source')
        self.learning = CorpusLearning(self.paths.home/'local-learning', self.corpus)
        backup(Path(material['corpus_root'])/'corpus.sqlite3', self.corpus.path)
        backup(Path(material['learning_root'])/'learning.sqlite3', self.learning.path)
        self.material = material | {'corpus_root':str(self.corpus.root), 'learning_root':str(self.learning.root)}
        self.file = self.paths.home/'copy.private.json'
        self.file.write_text(json.dumps(self.material,ensure_ascii=False),encoding='utf-8')
        self.persona = self.service.create_persona(PersonaSpec.from_dict(old.snapshot['persona_spec']))
        self.pid = self.persona['id']
        self.service.use_persona(self.pid)
        native.Integration.enable(self)
        self.cfg.update({'model':{'base_url':'https://api.deepseek.com'}})
        self.bundle = register_bundle(self.store,self.pid,self.file)
        self.g = self.chat.direct.generation
        self.client = self.g.client = LocalTransport()
        class OfflineAssembly(DialogueAssembly):
            # The local transport cannot contact a model. Keep the research-use guard active for every read.
            def _resolve(self, db, persona_id, bundle_id, purpose):
                return super()._resolve(db,persona_id,bundle_id,REPLAY)
        def local(db,pid,bundle):
            asm = OfflineAssembly(self.store)
            asm._resolve(db,pid,bundle,REPLAY)
            return asm
        self.g.load_assembly = local
        self.pub = self.chat.direct.publication
        self.g.configure(self.pid,{'enabled':True,'bundle_id':self.bundle},0,identifier())

    async def test_desktop_notice_followup_and_batched_ingress(self):
        self.need_web = True
        turn = await self.chat.submit('desktop',self.texts[0],identifier(),public_task={'question':'P1'})
        notice = await self.wait_for_plan(turn,'search_notice')
        self.assertFalse(self.provider.calls)
        self.assertEqual(len(self.client.wires),1)
        actual_notice = self.present(turn,notice)
        main = await self.wait_for_plan(turn,'main')
        self.assertEqual(self.chat.get(turn['id'])['search_notice']['text'],actual_notice)
        self.assertEqual(len(self.client.wires),4)
        actual = self.present(turn,main)
        await self.chat.wait()
        self.need_web = False
        from persona_runtime.ingress import IngressService
        ingress = IngressService(self.chat)
        for text in self.texts[1:3]:
            batch = ingress.accept({'text':text,'request_id':identifier(),'channel':'desktop',
                                    'session_id':turn['session_id'],'persona_id':self.pid})
        linked = ingress.action(batch['batch_id'],'seal',batch['batch_revision'],identifier(),self.pid)
        nxt = self.chat.get(linked['turn_id'])
        next_plan = await self.wait_for_plan(nxt,'main')
        self.assertEqual(len(self.client.wires),7)
        flash = self.client.wires[-3]['messages'][-1]['content']
        self.assertIn(actual,flash)
        for text in self.texts[1:3]:
            self.assertIn(text,flash)
            self.assertIn(text,self.client.wires[-2]['messages'][2]['content'])
        self.assertNotIn('event_id',flash)
        self.present(nxt,next_plan)
        await self.chat.wait()
        self.assertEqual(self.chat.get(nxt['id'])['status'],'completed')

    async def test_authoritative_dataset_and_learning_revocations(self):
        item = self.material['supports'][0]
        with self.store.connection() as db:
            assembly = self.g.load_assembly(db,self.pid,self.bundle)
            internal = db.execute('SELECT asset_id FROM dialogue_corpus_supports WHERE bundle_id=? LIMIT 1',(self.bundle,)).fetchone()[0]
            with self.assertRaises(ExpressionAssetError):
                asset(db,self.pid,scope(self.pid,'desktop'),internal)
        with self.learning.connection() as db:
            grant = db.execute('SELECT approval_id FROM learning_jobs WHERE id=?',(item['job_id'],)).fetchone()[0]
        # All mutations target SQLite backups under this test's temporary instance.
        self.assertTrue(self.learning.path.is_relative_to(self.paths.home))
        self.assertTrue(self.corpus.path.is_relative_to(self.paths.home))
        self.learning.revoke(grant)
        with self.store.connection() as db, self.assertRaisesRegex(RuntimeErrorBase,'learning_approval_unavailable'):
            assembly._resolve(db,self.pid,self.bundle,REPLAY)
        self.corpus.revoke_dataset(item['dataset_id'],item['dataset_revision'],identifier())
        with self.corpus.connection() as db, self.assertRaisesRegex(ValueError,'dataset_revoked'):
            self.corpus._view_status(db,item['source_binding']['view_id'])
        self.assertEqual(self.client.wires,[])

    async def test_authoritative_withdrawal_during_reply_keeps_receipt_and_blocks_publication(self):
        self.need_web = False
        sent, release = asyncio.Event(), asyncio.Event()
        original = self.client.complete
        async def delayed(model, wire, *, save_raw):
            if wire.get('stream'):
                sent.set()
                await release.wait()
            await original(model, wire, save_raw=save_raw)
        self.client.complete = delayed
        turn = await self.chat.submit('desktop',self.texts[0],identifier())
        await asyncio.wait_for(sent.wait(),10)
        ref = self.material['supports'][0]
        self.assertTrue(self.corpus.path.is_relative_to(self.paths.home))
        try:
            self.corpus.revoke_dataset(ref['dataset_id'],ref['dataset_revision'],identifier())
        finally:
            release.set()
        await asyncio.wait_for(self.chat.wait(),10)
        with self.store.connection() as db:
            row = db.execute("SELECT raw_response, state FROM dialogue_generation_stages WHERE stage='reply'").fetchone()
            self.assertIsNotNone(row['raw_response'])
            self.assertEqual(row['state'],'completed')
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona'").fetchone()[0],0)
            with self.assertRaisesRegex(RuntimeErrorBase,'dataset_revoked'):
                self.g.load_assembly(db,self.pid,self.bundle)
        self.assertFalse(self.pub.list(self.pid,turn['session_id'],turn['id']))
        self.assertEqual(len(self.client.wires),3)
