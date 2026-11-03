import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import dialogue_communication_real as t
from persona_runtime.archive import encoded,fingerprint
from persona_runtime.communication_consumption import parse_joint_v2
from persona_runtime.dialogue_context import SharedEvidence
from persona_runtime.dialogue_communication_pipeline import CommunicationPipeline
from persona_runtime.chat import ModelCompletion


class Structural(unittest.TestCase):
    def test_orders_and_fixed_sampling(self):
        rows=t.order();self.assertEqual(len(rows),24)
        from collections import Counter
        self.assertEqual(set(Counter(tuple(r['conditions']) for r in rows).values()),{4})
        self.assertEqual(len({r['id'] for r in rows}),24)
        self.assertEqual([(r['case_id'],r['replicate']) for r in rows[:4]],[('G08',1),('G08',1),('G11',1),('G11',1)])

    def test_cli_has_no_implicit_real_action(self):
        import sys
        with patch.object(sys,'argv',['tool','execute']):
            with self.assertRaisesRegex(Exception,'explicit_real_required'):t.main()

    def test_ledger_success_cannot_repeat(self):
        with tempfile.TemporaryDirectory(dir=t.HOME/'tmp') as tmp:
            self.assertTrue(Path(tmp).resolve().is_relative_to((t.HOME/'tmp').resolve()))
            ledger=t.Ledger(Path(tmp),False);model={'name':'mock','max_tokens':10,'temperature':1,'top_p':1}
            row=ledger.reserve('x','main',model,[{'role':'user','content':'hello'}]);row['terminal']={'status':'completed'}
            with self.assertRaisesRegex(Exception,'no_success_replay'):ledger.reserve('x','main',model,[{'role':'user','content':'hello'}])

    def test_only_group_local_dedup_identity(self):
        rows=t.order();self.assertNotEqual(rows[0]['id'],rows[4]['id'])
        # Each group's seen map is newly allocated; independent replicate slots differ.
        for case in range(7,13):
            ids=[r['id'] for r in rows if r['case_id']==f'G{case:02d}']
            self.assertEqual(len(set(ids)),4)


class Adapter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration=100
        self.tmp=tempfile.TemporaryDirectory(dir=t.HOME/'tmp',prefix='cr31-test-')
        self.assertTrue(Path(self.tmp.name).resolve().is_relative_to((t.HOME/'tmp').resolve()))
        self.engines=[];self.p=t.read(t.OUT/'simulation-v1/prepared.json')
        self.p['runtime_root']=self.tmp.name
        self.item=deepcopy(self.p['items']['G12-process-r1']);self.ledger=t.Ledger(Path(self.tmp.name)/'receipts',False)
        self.raw=t.read(t.OUT/'simulation-v1/auxiliary/G12-process-r1.json')['raw']

    async def asyncTearDown(self):
        for e in self.engines:t.close(e)
        self.tmp.cleanup()

    def engine(self,name):
        e=t.engine_for(self.p,self.item,name,self.ledger);self.engines.append(e);return e

    async def views(self):
        from persona_runtime.dialogue_pipeline import collect
        e=self.engine('view-source');c=collect(e,self.item['attachment']['turn_id'],self.item['attachment'])
        return parse_joint_v2(self.raw,c['shared'],self.item['pool'])

    async def test_full_adapter_matches_original_consumer(self):
        shared=await self.views();e=self.engine('full');p=t.ViewPipeline(self.item,shared,'full')
        actual=await p.prepare(e,self.item['attachment']['turn_id'])
        e2=self.engine('original');raw=self.raw
        class Client:
            async def complete(self,*_):return ModelCompletion(raw,{'total_tokens':20},{'response_model':'mock'})
        e2.client=Client();original=CommunicationPipeline(self.item['attachment'],self.item['pool'])
        expected=await original.prepare(e2,self.item['attachment']['turn_id'])
        self.assertEqual(actual['messages'],expected['messages'])
        self.assertEqual(actual['manifest']['input_snapshot_hash'],expected['manifest']['input_snapshot_hash'])

    async def test_core_omits_only_optional_and_raw_omits_block(self):
        shared=await self.views();values={}
        for condition in ('raw','essential','full'):
            e=self.engine(condition);p=t.ViewPipeline(self.item,shared,condition)
            prepared=await p.prepare(e,self.item['attachment']['turn_id']);values[condition]=(prepared,p.record)
        self.assertEqual(values['raw'][1]['projection'],'')
        self.assertNotIn(shared['communication_spec']['optional_reactions'][0]['text'],values['essential'][1]['projection'])
        self.assertIn(shared['communication_spec']['optional_reactions'][0]['text'],values['full'][1]['projection'])
        self.assertEqual(values['raw'][0]['details']['character'],values['full'][0]['details']['character'])

    async def test_shared_result_cannot_cross_source_or_role(self):
        shared=await self.views();shared['evidence_hash']='other-source'
        with self.assertRaisesRegex(Exception,'provider_invalid_response'):
            await t.ViewPipeline(self.item,shared,'full').prepare(self.engine('wrong'),self.item['attachment']['turn_id'])

    async def test_empty_view_does_not_invent_spec(self):
        shared=await self.views();shared['communication_spec']['required_content']=[]
        shared['communication_spec']['state_constraints']=[];shared['communication_spec']['optional_reactions']=[]
        e=self.engine('empty');p=t.ViewPipeline(self.item,shared,'full')
        actual=await p.prepare(e,self.item['attachment']['turn_id'])
        self.assertEqual(p.record['projection'],'')
        self.assertNotIn('本次需要完成的交流内容',actual['messages'][0]['content'])

    async def test_stale_revision_prevents_main(self):
        e=self.engine('stale');shared=await self.views()
        with e.store.connection(write=True) as db:db.execute('UPDATE turns SET execution_revision=execution_revision+1 WHERE id=?',(self.item['attachment']['turn_id'],))
        with self.assertRaisesRegex(Exception,'execution_stale'):
            await t.ViewPipeline(self.item,shared,'full').prepare(e,self.item['attachment']['turn_id'])

    async def test_transport_source_freeze_failure_before_reserve(self):
        e=self.engine('frozen');transport=t.Transport(t.OUT/'simulation-v1',self.p,self.item,e,self.ledger,'test','main')
        with self.assertRaisesRegex(Exception,'execution_source_changed'):
            await transport.complete({},[])
        self.assertEqual(self.ledger.rows,[])

    async def test_no_credentials_in_snapshots(self):
        for name in ('model_snapshot','selector_model_snapshot','observation_model_snapshot'):
            self.assertFalse(json.loads(self.item['public_snapshots'][name]).get('encrypted_key'))


if __name__=='__main__':unittest.main()
