"""First-return and per-branch history checks with a transport stub."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from experiments.corpus_reply_trial import public_model
from experiments.fresh_scene_journal import Journal
from persona_runtime.corpus_files import CorpusError
from persona_runtime.provider_compat import build_request


class Stub:
    def __init__(self,unknown=False):
        self.count=0
        self.unknown=unknown
    def preflight(self,*_):pass
    async def complete(self,model,wire,*,save_raw):
        self.count+=1
        await asyncio.sleep(0)
        if self.unknown:raise TimeoutError()
        raw=json.dumps({'model':model['name'],'choices':[{'message':{'content':'BODY-1'},'finish_reason':'stop'}]}).encode()
        save_raw(raw,http_status=200,diagnostics={'physical_dispatch_index':1})


class FirstReturns(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()/'study'
        self.root.mkdir()
        self.model=public_model('deepseek-flash',.3)
        self.wire=build_request(self.model,[{'role':'user','content':'ID-1'}])[0]
        self.credentials={'base_url':self.model['base_url'],'encrypted_key':'ID-2'}

    def test_two_identical_branch_requests_share_physical_receipt_even_concurrently(self):
        client=Stub()
        journal=Journal(self.root,'HASH',client=client)
        async def run():
            return await asyncio.gather(*(journal.call(key,self.model,self.wire,[],self.credentials) for key in ('S.1','R.1')))
        left,right=asyncio.run(run())
        self.assertEqual(left,right)
        self.assertEqual(client.count,1)
        resumed=Journal(self.root,'HASH',client=client)
        asyncio.run(resumed.call('R.1',self.model,self.wire,[],{}))
        self.assertEqual(client.count,1)

    def test_changed_dependency_rejected_before_using_old_slot(self):
        journal=Journal(self.root,'HASH',client=Stub())
        asyncio.run(journal.call('S.1',self.model,self.wire,['OLD'],self.credentials))
        with self.assertRaises(CorpusError):
            asyncio.run(journal.call('S.1',self.model,self.wire,['NEW'],self.credentials))

    def test_unknown_is_never_resent_and_halt_persists(self):
        client=Stub(unknown=True)
        journal=Journal(self.root,'HASH',client=client)
        first=asyncio.run(journal.call('B.1',self.model,self.wire,[],self.credentials))
        self.assertEqual(first['state'],'unknown')
        resumed=Journal(self.root,'HASH',client=client)
        new=build_request(self.model,[{'role':'user','content':'ID-3'}])[0]
        self.assertEqual(asyncio.run(resumed.call('B.2',self.model,new,[],self.credentials))['state'],'blocked')
        self.assertEqual(client.count,1)

    def test_existing_binding_cannot_open_empty_replacement_ledger(self):
        journal=Journal(self.root,'HASH',client=Stub())
        journal.path.unlink()
        with self.assertRaises(CorpusError):Journal(self.root,'HASH',client=Stub())


if __name__=='__main__':unittest.main()
