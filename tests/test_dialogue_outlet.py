"""Exercise desktop submission, Jev, direct speech, native retrieval and delivery on an isolated instance."""
import asyncio
import json
import unittest
from unittest.mock import patch

from persona_runtime.models import RuntimeErrorBase, identifier
from persona_runtime.expression_publication import ExpressionPublication
from persona_runtime.knowledge_handoff import VERSION as SEARCH_HANDOFF
from tests import test_m9r_native_retrieval as native
from tests.test_dialogue_generation_stages import BASE, BUNDLE, BoundAssembly, Client


class OutletAssembly(BoundAssembly):
    def __init__(self):
        self.frames = []

    def freeze(self, db, turn, bundle, model, messages, *, phase, details=None):
        self.frames.append((phase, messages))
        return {'bundle_id': bundle, 'assets': False}

    def projection(self, body, material):
        return {'display_body': body.replace('ID-OUTPUT-', 'ID-VIEW-'), 'removed_spans': []}

    def segments(self, body, material):
        return [{'start': 0, 'end': len(body), 'delay_ms': 100}]


class DirectOutlet(unittest.IsolatedAsyncioTestCase):
    decision_transport = native.Integration.decision_transport
    transport = native.Integration.transport

    async def asyncSetUp(self):
        native.Integration.setUp(self)
        native.Integration.enable(self)
        self.cfg.update({'model': {'base_url': BASE}})
        self.g = self.chat.direct.generation
        self.client = self.g.client = Client()
        self.a = OutletAssembly()
        self.g.load_assembly = lambda db, pid, bundle: self.a
        self.pub = self.chat.direct.publication
        self.pid = self.persona['id']
        self.g.configure(self.pid, {'enabled': True, 'bundle_id': BUNDLE}, 0, identifier())

    async def asyncTearDown(self):
        for task in list(self.chat.tasks.values()):
            if not task.done():
                task.cancel()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await native.Integration.asyncTearDown(self)

    async def wait_for_plan(self, turn, phase):
        async with asyncio.timeout(10):
            while True:
                for unit in self.pub.list(self.pid, turn['session_id'], turn['id']):
                    if unit['phase'] == phase and self.pub.plan(self.pid, turn['session_id'], unit['id']):
                        return unit['id']
                view = self.chat.get(turn['id'])
                if view['status'] in ('failed', 'interrupted'):
                    self.fail(str((view['status'], view['error_code'], self.g.status(turn['id']))))
                await asyncio.sleep(.025)

    def present(self, turn, uid):
        args = (self.pid, turn['session_id'], uid)
        plan = self.pub.plan(*args)
        parts = []
        for item in plan['items']:
            offer = self.pub.offer(*args, plan['id'], plan['revision'], item['ordinal'], 'ID-DESKTOP')
            self.assertTrue(offer.get('lease_id'), str(offer))
            self.pub.acknowledge(*args, offer['id'], offer['lease_id'], 'ID-DESKTOP', identifier(), offer['body_hash'])
            parts.append(offer['body'])
        return ''.join(parts)

    def check_legacy_zero(self):
        self.assertEqual(self.main.calls, [])
        self.assertEqual(self.main.notice_calls, [])
        with self.store.connection() as db:
            for table in ('expression_main_receipts', 'expression_runs', 'expression_speech_runs'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    async def test_web_waits_for_notice_then_main_and_followup_use_displayed_speech(self):
        turn = await self.chat.submit('desktop', 'ID-U-1', identifier(), public_task={'question':'ID-P-1'})
        notice = await self.wait_for_plan(turn, 'search_notice')
        self.assertFalse(self.provider.calls)
        self.assertIsNone(self.chat.get(turn['id'])['search_notice'])
        self.assertEqual(len(self.client.wires), 1)
        self.assertTrue(all(i['delay_ms'] == 0 for i in self.pub.plan(self.pid, turn['session_id'], notice)['items']))
        notice_body = self.present(turn, notice)
        main = await self.wait_for_plan(turn, 'main')
        self.assertEqual(self.chat.get(turn['id'])['search_notice']['text'], notice_body)
        baseline = json.dumps(self.a.frames[-1][1], ensure_ascii=False)
        self.assertNotIn(notice_body, baseline)
        self.assertNotIn('ID-OUTPUT-1', baseline)
        self.assertEqual(self.chat.get(turn["id"])["context"]["knowledge"]["translation"]["handoff_version"],
                         SEARCH_HANDOFF)
        self.assertEqual(len(self.provider.calls), 1)
        main_body = self.present(turn, main)
        await asyncio.wait_for(self.chat.wait(), 10)
        self.assertEqual(self.chat.get(turn['id'])['status'], 'completed')
        self.need_web = False
        next_turn = await self.chat.submit('desktop', 'ID-U-2', identifier(), session_id=turn['session_id'])
        next_main = await self.wait_for_plan(next_turn, 'main')
        self.assertIn(main_body, json.dumps(self.a.frames[-1][1], ensure_ascii=False))
        self.assertNotIn('ID-OUTPUT-', json.dumps(self.a.frames[-1][1], ensure_ascii=False))
        self.present(next_turn, next_main)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.client.wires), 5)
        self.check_legacy_zero()

    async def test_offline_unknown_understanding_is_never_replayed_or_shown(self):
        self.need_web = False
        self.client.failure = True
        turn = await self.chat.submit('desktop', 'ID-U-3', identifier())
        await asyncio.wait_for(self.chat.wait(), 10)
        view = self.chat.get(turn['id'])
        self.assertIn(view['status'], ('failed', 'interrupted'))
        self.assertFalse(self.pub.list(self.pid, turn['session_id'], turn['id']))
        for action in (self.chat.resume_direct, self.chat.retry):
            with self.assertRaises(RuntimeErrorBase):
                await action(turn['id'])
        self.assertEqual(self.g.status(turn['id'])['stages'][0]['state'], 'unknown')
        self.assertEqual(len(self.client.wires), 1)
        self.assertFalse(self.provider.calls)
        self.check_legacy_zero()

    async def test_native_publication_refines_oversize_plan_and_preserves_body(self):
        self.need_web = False
        body = "R" * 151
        self.a.projection = lambda reply, material: {"display_body": body, "removed_spans": []}
        turn = await self.chat.submit("desktop", "ID-U-3", identifier())
        uid = await self.wait_for_plan(turn, "main")
        self.assertEqual(self.present(turn, uid), body)
        await self.chat.wait()
        view = self.chat.get(turn["id"])
        self.assertEqual(view["reply"], body)
        plan = next(item for item in view["desktop_publications"] if item["phase"] == "main")
        self.assertEqual(plan["bubble_limit"], 75)
        self.assertEqual([len(part["text"]) for part in plan["visible"]], [75, 75, 1])

    async def test_oversized_unoffered_legacy_fragment_cannot_receive_lease(self):
        self.need_web = False
        body = "R" * 76
        self.a.projection = lambda reply, material: {"display_body": body, "removed_spans": []}
        with patch("persona_runtime.expression_utterance_publication.limit_ranges",
                   side_effect=lambda text, parts: parts):
            turn = await self.chat.submit("desktop", "ID-U-4", identifier())
            uid = await self.wait_for_plan(turn, "main")
        args = (self.pid, turn["session_id"], uid)
        plan = self.pub.plan(*args)
        self.assertEqual(len(plan["items"]), 1)
        offer = self.pub.offer(*args, plan["id"], plan["revision"], 0, "ID-DESKTOP")
        self.assertEqual(offer["reason"], "reply_bubble_limit")
        self.assertNotIn("lease_id", offer)

    async def test_notice_wrong_model_prevents_native_search(self):
        original = self.client.complete
        async def complete(model, wire, *, save_raw):
            if wire['messages'][0]['content'] == 'ID-N':
                self.client.wires.append(wire)
                packet = {'model':'deepseek-flash', 'choices':[{'finish_reason':'stop',
                           'message':{'content':'ID-WRONG-MODEL'}}]}
                save_raw(json.dumps(packet).encode(), http_status=200, diagnostics={})
                return
            return await original(model, wire, save_raw=save_raw)
        self.client.complete = complete
        turn = await self.chat.submit('desktop', 'ID-U-5', identifier(), public_task={'question':'ID-P-5'})
        await asyncio.wait_for(self.chat.wait(), 10)
        self.assertFalse(self.provider.calls)
        self.assertIsNone(self.chat.get(turn['id'])['search_notice'])
        notices = [s for s in self.g.status(turn['id'])['stages'] if s['phase'] == 'search_notice']
        self.assertEqual([(s['stage'],s['state']) for s in notices], [('reply','failed')])
        self.assertFalse(any(u['phase']=='search_notice' for u in self.pub.list(self.pid,turn['session_id'],turn['id'])))
        self.check_legacy_zero()

    async def test_legacy_turn_plan_and_receipt_share_the_v2_publication(self):
        self.need_web = False
        turn = await self.chat.submit('desktop', 'ID-U-6', identifier())
        uid = await self.wait_for_plan(turn, 'main')
        legacy = ExpressionPublication(self.chat.expression)
        args = (self.pid,turn['session_id'],turn['id'])
        plan = legacy.plan(*args)
        v2_plan = self.pub.plan(self.pid, turn['session_id'], uid)
        self.assertEqual(plan['id'], v2_plan['id'])
        offer = legacy.offer(*args, plan['id'], plan['revision'], 0, 'ID-READER')
        receipt = identifier()
        legacy.acknowledge(*args, offer['id'], offer['lease_id'], 'ID-READER', receipt, offer['body_hash'])
        repeated = self.pub.acknowledge(self.pid, turn['session_id'], uid, offer['id'],offer['lease_id'],
                                        'ID-READER', receipt, offer['body_hash'])
        self.assertTrue(repeated['already_applied'])
        self.assertEqual(self.chat.get(turn['id'])['status'], 'completed')
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona'").fetchone()[0], 1)
        self.assertEqual(len(self.client.wires), 2)
        self.check_legacy_zero()

    async def test_cancel_unseen_notice_keeps_search_and_history_empty(self):
        turn = await self.chat.submit('desktop', 'ID-U-4', identifier(), public_task={'question':'ID-P-4'})
        await self.wait_for_plan(turn, 'search_notice')
        await self.chat.cancel(turn['id'])
        self.assertIsNone(self.chat.get(turn['id'])['search_notice'])
        self.assertFalse(self.provider.calls)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona'").fetchone()[0], 0)
        self.assertEqual(len(self.client.wires), 1)
        self.check_legacy_zero()

    async def test_notice_resumes_from_pro_receipt_before_organization_commit(self):
        original=self.chat.direct.notice_result
        async def interrupted(uid, model):
            await original(uid,model)
            raise RuntimeErrorBase('ID-INTERRUPTED')
        self.chat.direct.notice_result=interrupted
        turn=await self.chat.submit('desktop','ID-U-7',identifier(),public_task={'question':'ID-P-7'})
        await asyncio.wait_for(self.chat.wait(),10)
        self.assertFalse(self.provider.calls)
        self.assertEqual(len(self.client.wires),1)
        self.chat.direct.notice_result=original
        await self.chat.resume_direct(turn['id'])
        notice=await self.wait_for_plan(turn,'search_notice')
        self.assertEqual(len(self.client.wires),1)
        self.assertFalse(self.provider.calls)
        self.present(turn,notice)
        main=await self.wait_for_plan(turn,'main')
        self.present(turn,main)
        self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(len(self.client.wires),3)
        self.check_legacy_zero()

    async def test_reopened_main_keeps_offered_lease_and_confirmed_prefix(self):
        self.need_web=False
        self.a.segments=lambda body,material: [{'start':0,'end':2},{'start':2,'end':len(body)}]
        turn=await self.chat.submit('desktop','ID-U-8',identifier())
        uid=await self.wait_for_plan(turn,'main')
        args=(self.pid, turn['session_id'], uid)
        plan=self.pub.plan(*args)
        offer=self.pub.offer(*args,plan['id'],plan['revision'],0,'ID-READER')
        self.chat.recover()
        self.assertEqual(self.pub.pending_receipts(*args,'ID-READER')[0]['lease_id'],offer['lease_id'])
        again=self.pub.offer(*args,plan['id'],plan['revision'],0,'ID-READER')
        self.assertFalse(again['new_offer'])
        self.pub.acknowledge(*args,offer['id'],offer['lease_id'],'ID-READER',identifier(),offer['body_hash'])
        self.pub.cancel(uid)
        self.assertEqual(self.chat.get(turn['id'])['reply'],offer['body'])
        self.assertEqual(self.pub.plan(*args)['state'],'closed')
        await self.chat.resume_direct(turn['id'])
        self.assertEqual(len(self.client.wires),2)
