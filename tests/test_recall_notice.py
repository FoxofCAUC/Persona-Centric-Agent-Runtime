import json,unittest
from persona_runtime.recall_runtime import RecallRuntime
from persona_runtime.models import identifier
from tests import test_m9r_native_retrieval as fixture

class NativeBridge(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.Integration.setUp
    asyncTearDown=fixture.Integration.asyncTearDown
    enable=fixture.Integration.enable
    ask=fixture.Integration.ask
    decision_transport=fixture.Integration.decision_transport
    transport=fixture.Integration.transport

    async def test_notice_and_main_share_one_reinforcement_per_unit(self):
        self.enable();pid=self.persona['id'];runtime=RecallRuntime(self.store)
        sid=self.chat.session()['session_id']
        event=self.chat.service.append_event(sid,'user','我喜欢乌龙茶。')
        memory=self.chat.service.add_memory('preference','我喜欢乌龙茶。',event['id'])
        runtime.update(pid,True,0,identifier())
        for _ in range(10):runtime.maintain(pid,pages=3)
        turn=await self.ask('帮我查查乌龙茶怎么泡',public={'question':'乌龙茶泡法'})
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        runtime.feedback()
        with self.store.connection() as db:
            rows=list(db.execute('SELECT e.* FROM recall_support_events e JOIN recall_support_units u ON u.id=e.unit_id WHERE e.root_id=? AND u.source_id=?',
                (turn['id'],memory['id'])))
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['alpha'],.12)
            feedback=list(db.execute('SELECT stage,state FROM recall_runtime_feedback WHERE turn_id=?',(turn['id'],)))
            self.assertEqual({r[0] for r in feedback},{'main','notice'})
            self.assertTrue(all(r[1]=='completed' for r in feedback))

if __name__=='__main__':unittest.main()
