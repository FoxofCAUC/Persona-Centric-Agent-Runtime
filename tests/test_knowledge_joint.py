"""Combined B-route freshness, outages, stop-learning and recovery checks."""
import asyncio,copy,json,threading,unittest
from tests import test_knowledge_web_maintenance as bridge
from persona_runtime.knowledge_fetch import FetchError
from persona_runtime.knowledge_service import read
from persona_runtime.knowledge_store import search
from persona_runtime.knowledge_transfer import restore

class JointTests(unittest.IsolatedAsyncioTestCase):
    setUp=bridge.BridgeTests.setUp
    asyncTearDown=bridge.BridgeTests.asyncTearDown
    enable=bridge.BridgeTests.enable
    put=bridge.BridgeTests.put
    ask=bridge.BridgeTests.ask
    learned=bridge.BridgeTests.learned

    async def wait_for(self,predicate):
        for _ in range(600):
            if predicate():return
            await asyncio.sleep(.025)
        self.fail('product transition did not finish')

    async def test_explicit_current_rechecks_even_with_fresh_claims_and_same_question(self):
        self.enable();await self.ask();await self.learned();before=len(self.source_calls)
        self.processor.current=True
        for _ in range(2):
            result=await self.ask('请重新核对测试行星目前的颜色。')
            self.assertEqual(result['status'],'completed',result['error_code'])
            self.assertTrue(all(e['kind']=='web_document' for e in result['context']['knowledge']['evidence']))
        self.assertEqual(len(self.source_calls),before+2)
        self.assertEqual(len(self.processor.background),2,'unchanged source must not repeat learning')

    async def test_transient_outage_stop_inflight_and_regrant_preserve_old_backup_rules(self):
        self.enable();first=await self.ask();await self.learned();self.service.backup('joint-old.sqlite3')
        original=self.source.fetch;entered=threading.Event();release=threading.Event();count=[]
        def source_fetch(source,public_id):
            count.append(public_id)
            if len(count)==1:raise FetchError('knowledge_http_503',retry_after=60)
            entered.set();release.wait(10)
            return original(source,public_id)
        self.source.fetch=source_fetch;self.color='红色';self.revision=2;self.put();self.now+=86401
        p=self.maintenance.policy(self.pid);self.maintenance.update_policy(self.pid,p['data']|{'daily_scan':True},p['revision'],'daily')
        await self.wait_for(lambda:bool(count));self.assertEqual(len(count),1)
        self.now+=61
        try:
            await self.wait_for(entered.is_set)
            p=self.maintenance.policy(self.pid);self.maintenance.update_policy(self.pid,p['data']|{'enabled':False},p['revision'],'stop-inflight')
        finally:release.set()
        await self.wait_for(lambda:not self.maintenance.pending_io)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT latest_revision FROM knowledge_pages').fetchone()[0],1)
        self.assertEqual(len(self.processor.background),2)
        self.now+=301;await self.maintenance.tick();self.assertEqual(len(count),2)
        p=self.maintenance.policy(self.pid);self.maintenance.update_policy(self.pid,p['data']|{'enabled':True,'daily_scan':False},p['revision'],'resume')
        with self.store.connection() as db:revision=read(db,'refresh',{})['revision']
        self.maintenance.submit(self.pid,[{'source_id':'wikipedia_zh','public_id':'测试行星'}],revision,'after-stop-new-job')
        await self.learned();self.assertEqual(len(count),3)
        restore(self.chat.knowledge,'joint-old.sqlite3',0,'restore-old-after-recovery')
        self.assertIsNone(self.chat.get(first['id'])['reply'])
        result=await self.ask();self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertTrue(all('红色' in e['quote'] for e in self.chat.knowledge.show_evidence(self.pid,result['id'])['evidence']))

    async def test_new_page_and_backup_cannot_downgrade_prior_withdrawal(self):
        self.enable();first=await self.ask();await self.learned();self.service.backup('before-explicit-withdrawal.sqlite3')
        old_id=first['context']['knowledge']['evidence'][0]['document_id']
        with self.store.connection() as db:revision=read(db,'web.invalidations',{})['revision']
        self.org.web.invalidate(self.pid,old_id,'撤回版本一',revision,'explicit-withdrawal')
        self.color='红色';self.revision=2;self.put();self.now+=2;self.processor.current=True
        updated=await self.ask('重新核对测试行星目前的颜色。');await self.learned()
        self.assertEqual(updated['status'],'completed',updated['error_code'])
        restore(self.chat.knowledge,'before-explicit-withdrawal.sqlite3',0,'restore-after-replacement')
        with self.store.connection() as db:
            self.assertEqual(read(db,'web.invalidations',{})['data'][old_id]['state'],'withdrawn')
            self.assertTrue(all('红色' in e['quote'] for e in search(db,self.pid,'desktop','测试行星')))
