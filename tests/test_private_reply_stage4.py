"""Event-based fault combinations for the v3 desktop route; no external providers."""
import asyncio
from contextlib import closing
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime import desktop_recovery
from persona_runtime.chat import ModelError
from persona_runtime.ingress import IngressService
from persona_runtime.models import identifier
from tests import test_private_reply_stage3 as stage3


class CandidateRecovery(unittest.IsolatedAsyncioTestCase):
    setUp=stage3.ResearchTests.setUp
    reopen=stage3.ResearchTests.reopen
    submit=stage3.ResearchTests.submit
    paint=stage3.ResearchTests.paint
    show=stage3.ResearchTests.show
    consent=stage3.ResearchTests.consent

    def offline(self):
        return ModelError('provider_network_error',diagnostics={'request_headers_sent':False})

    async def cycle(self):
        self.reopen()
        self.chat.recover()
        ingress=IngressService(self.chat)
        ingress.recover()
        ingress.network_wake()
        ingress.schedule_committed()
        await self.chat.wait()

    async def exercise_node(self,node):
        self.consent(True)
        self.providers.route='none'
        original=self.providers.choose
        seen=[]
        async def choose(name,attempt,state,choices):
            if name==node:
                seen.append(attempt)
                if len(seen)==1:raise self.offline()
            return await original(name,attempt,state,choices)
        self.providers.choose=choose
        tid=await self.submit('910')
        self.assertEqual(self.chat.get(tid)['error_code'],'desktop_network_wait')
        await self.cycle()
        self.show(tid,'main')
        self.assertEqual(self.chat.get(tid)['status'],'completed')
        self.assertEqual(len(set(seen)),2)
        self.assertEqual(sum(1 for n,*_ in self.providers.calls if n==node),1)
        self.assertEqual(len(self.providers.bodies),1)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM model_call_meter').fetchone()[0],
                             len(self.providers.calls)+len(self.providers.bodies))
    async def test_offline_before_j1_then_restart(self):await self.exercise_node('J1')
    async def test_offline_in_parallel_j3_then_restart(self):await self.exercise_node('J3')
    async def test_offline_at_final_planner_choice_then_restart(self):await self.exercise_node('J6.ending')

    async def test_writer_preflight_offline_releases_quota_and_resumes_original_notice(self):
        self.consent(True)
        original=self.providers.reply
        tries=[]
        async def reply(attempt,packet):
            tries.append((attempt,packet['phase']))
            if len(tries)==1:raise self.offline()
            return await original(attempt,packet)
        self.providers.reply=reply
        tid=await self.submit()
        self.assertEqual(self.chat.get(tid)['error_code'],'desktop_network_wait')
        await self.cycle()
        self.show(tid,'search_notice')
        await self.cycle()
        self.show(tid,'main')
        self.assertEqual([p for _,p in tries],['search_notice','search_notice','main'])
        self.assertEqual(len(self.providers.bodies),2)
        self.assertEqual(len(self.providers.lookups),1)

    async def test_dead_owner_after_notice_publication_cannot_strand_search(self):
        self.consent(True)
        tid=await self.submit()
        with self.store.connection(write=True) as db:
            turn=dict(db.execute("SELECT * FROM turns WHERE id=?",(tid,)).fetchone())
            self.assertTrue(desktop_recovery.claim(db,turn,self.config.load(),'previous-service'))
        self.reopen()
        with patch('persona_runtime.instance.process_identity_alive',return_value=False):self.chat.recover()
        self.show(tid,'search_notice')
        IngressService(self.chat).schedule_committed()
        await self.chat.wait()
        self.show(tid,'main')
        self.assertEqual(len(self.providers.lookups),1)

    async def test_unknown_judge_is_never_resent_after_multiple_reopens(self):
        self.providers.route='none'; original=self.providers.choose; sent=[]
        async def choose(name,attempt,state,choices):
            if name=='J6.ending':
                sent.append(attempt)
                raise ModelError('provider_timeout',diagnostics={'request_headers_sent':True})
            return await original(name,attempt,state,choices)
        self.providers.choose=choose
        tid=await self.submit()
        self.providers.choose=original
        for _ in range(2):
            await self.cycle();await self.chat.retry(tid)
        self.assertEqual(len(sent),1)
        self.assertFalse(self.providers.bodies)
        self.assertFalse(self.out.list(self.pid,self.sid,tid))
        self.assertEqual(self.chat.get(tid)['error_code'],'reply_dispatch_unknown')

    async def test_revoke_or_replace_recipient_while_planner_is_parked(self):
        from persona_runtime import session_privacy
        self.providers.route='none'
        original=self.providers.choose
        async def pause(node,*args):
            if node=='J6.ending':raise self.offline()
            return await original(node,*args)
        for action in ('revoke','replace'):
            self.consent(True);self.providers.choose=pause
            tid=await self.submit('912')
            self.assertEqual(self.chat.get(tid)['error_code'],'desktop_network_wait')
            if action=='revoke':self.consent(False)
            else:
                args=(self.store,self.config,'jev','https://example.invalid/v2','jev-1.14','713')
                preview=session_privacy.connection(*args)
                session_privacy.connection(*args,confirm=preview['confirmation'])
            self.providers.choose=original
            before=(len(self.providers.calls),len(self.providers.bodies))
            await self.cycle()
            self.assertEqual((len(self.providers.calls),len(self.providers.bodies)),before)
            self.assertEqual(self.chat.get(tid)['status'],'interrupted')
            self.assertFalse(self.out.list(self.pid,self.sid,tid))
            self.show(await self.submit('913'),'main')

    async def test_retirement_preserves_completed_and_pending_inputs_after_restart(self):
        from persona_runtime.ordinary_providers import registered,retire
        from persona_runtime import session_privacy
        self.consent(True);self.providers.route='none'
        tid=await self.submit('914');body=self.show(tid,'main')
        before=(len(self.providers.calls),len(self.providers.bodies))
        with self.store.connection() as db,closing(sqlite3.connect(self.paths.home/'pre-retirement.sqlite3')) as copy:
            db.backup(copy)
        retire(self.config);retire(self.config)
        # A database restored on the same machine must also consume the later retirement.
        with closing(sqlite3.connect(self.paths.home/'pre-retirement.sqlite3')) as copy,closing(sqlite3.connect(self.store.path)) as db:
            copy.backup(db)
        self.assertIsNone(registered(self.config))
        await self.cycle()
        self.assertFalse(session_privacy.status(self.store,self.config,self.pid,self.sid)['candidate'])
        paused=await self.submit('915')
        self.assertEqual(self.chat.get(paused)['error_code'],'private_reply_candidate_retired')
        self.assertEqual(self.chat.get(tid)['reply'],body)
        self.assertEqual((len(self.providers.calls),len(self.providers.bodies)),before)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM private_reply_turns WHERE turn_id IN (?,?)",(tid,paused)).fetchone()[0],2)

    async def test_parallel_unknown_takes_precedence_over_offline_retry(self):
        self.providers.route='none';original=self.providers.choose
        async def mixed(node,*args):
            if node=='J2.action':raise self.offline()
            if node=='J3':raise ModelError('provider_timeout',diagnostics={'request_headers_sent':True})
            return await original(node,*args)
        self.providers.choose=mixed
        tid=await self.submit('916')
        self.assertEqual(self.chat.get(tid)['error_code'],'reply_dispatch_unknown')
        self.providers.choose=original
        await self.cycle();await self.chat.retry(tid)
        self.assertFalse(self.providers.bodies)
        self.assertFalse(any(n=='J3' for n,*_ in self.providers.calls))

    async def test_process_loss_after_writer_send_requires_original_receipt(self):
        worker=subprocess.run([sys.executable,'-B','-m',__name__,'crash-writer',str(self.paths.home),self.sid],
                              capture_output=True,timeout=40)
        self.assertEqual(worker.returncode,73,worker.stderr.decode(errors='replace'))
        attempt=(self.paths.home/'writer-sent').read_text()
        await self.cycle()
        with self.store.connection() as db:
            tid,state=db.execute("SELECT turn_id,state FROM private_reply_attempts WHERE id=?",(attempt,)).fetchone()
        self.assertEqual(state,'unknown')
        await self.chat.retry(tid)
        self.assertEqual(self.chat.get(tid)['error_code'],'reply_dispatch_unknown')
        self.assertFalse(self.providers.calls)
        self.assertFalse(self.out.list(self.pid,self.sid,tid))

    async def test_manual_buffer_survives_process_exit_until_duplicate_confirmation(self):
        worker=subprocess.run([sys.executable,'-B','-m',__name__,'hold-buffer',str(self.paths.home),self.sid],
                              capture_output=True,timeout=40)
        self.assertEqual(worker.returncode,73,worker.stderr.decode(errors='replace'))
        self.providers.route='none'
        await self.cycle()
        ingress=IngressService(self.chat)
        with self.store.connection() as db:
            self.assertFalse(db.execute("SELECT 1 FROM model_call_meter").fetchone())
            bid,rev=db.execute("SELECT id,revision FROM input_batches WHERE state='interrupted_buffer' AND sealed_at IS NULL").fetchone()
        action=(bid,'confirm',rev,identifier(),self.pid,self.sid)
        confirm=ingress.action(*action);self.assertEqual(ingress.action(*action),confirm)
        await self.chat.wait()
        tid=ingress.lookup('desktop','941')['turn_id']
        self.assertEqual(tid,ingress.lookup('desktop','942')['turn_id'])
        self.show(tid,'main')
        self.assertEqual(len(self.providers.bodies),1)

    async def test_scoped_quota_waiver_survives_offline_restart_until_last_bubble(self):
        from persona_runtime import call_limits
        self.assertFalse(self.config.load()['call_limits']['enabled'])
        self.consent(True);tid=await self.submit('920');self.show(tid,'search_notice')
        self.config.update({'call_limits':{'enabled':True,'hourly':1,'daily':0}})
        original=self.providers.retrieve
        async def offline(*args):raise self.offline()
        self.providers.retrieve=offline
        pending=asyncio.create_task(self.chat.retry(tid))
        try:
            async with asyncio.timeout(6):
                while True:
                    with self.store.connection() as db:waits=call_limits.pending(db)
                    if waits:break
                    await asyncio.sleep(.02)
            self.assertFalse(self.providers.lookups)
            call_limits.decide(self.store,[waits[0]['id']],True)
            await pending;await self.chat.wait()
            self.assertEqual(self.chat.get(tid)['error_code'],'desktop_network_wait')
            self.providers.retrieve=original
            await self.cycle()
            with self.store.connection() as db:
                self.assertTrue(db.execute("SELECT 1 FROM model_call_waits WHERE state='granted'").fetchone())
            self.show(tid,'main')
            with self.store.connection() as db:self.assertFalse(db.execute('SELECT 1 FROM model_call_waits').fetchone())
            self.assertEqual(len(self.providers.lookups),1)
            self.assertTrue(self.config.load()['call_limits']['enabled'])
        finally:
            if not pending.done():pending.cancel()

    async def test_session_switch_and_unknown_fragment_preserve_only_visible_prefix(self):
        from persona_runtime.models import PersonaSpec
        from persona_runtime.service import RuntimeService
        self.providers.route='none';self.providers.body='921。'*45
        tid=await self.submit('922')
        uid=self.out.list(self.pid,self.sid,tid)[0]['id'];plan=self.out.plan(self.pid,self.sid,uid)
        first=self.paint(uid,plan,0);self.paint(uid,plan,1,'unknown')
        service=RuntimeService(self.store)
        other=service.create_persona(PersonaSpec(1,'923','924',['925'],'926',['927'],'928'))['id']
        service.use_persona(other)
        sid=self.chat.session(persona_id=other)['session_id']
        await self.cycle()
        self.assertEqual(self.chat.get(tid)['reply'],first)
        with self.store.connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM events WHERE session_id=? AND text=?",(sid,first)).fetchone())
        service.use_persona(self.pid)
        await self.cycle()
        self.assertEqual(len(self.providers.bodies),1)
        self.assertEqual(self.chat.get(tid)['reply'],first)

    async def test_cancel_parked_turn_then_start_another_session_for_same_persona(self):
        from persona_runtime import session_privacy
        self.providers.route='none';self.consent(True)
        original=self.providers.choose
        async def pause(node,*args):
            if node=='J1':raise self.offline()
            return await original(node,*args)
        self.providers.choose=pause
        tid=await self.submit('992346721638572')
        self.assertEqual(self.chat.get(tid)['error_code'],'desktop_network_wait')
        await self.chat.cancel(tid);old=self.sid
        self.sid=self.chat.session(fresh=True,persona_id=self.pid)['session_id']
        self.assertNotEqual(self.sid,old)
        self.providers.choose=original
        await self.cycle()
        self.assertFalse(self.providers.bodies)
        self.assertEqual(self.chat.get(tid)['error_code'],'cancelled_by_user')
        self.assertFalse(session_privacy.status(self.store,self.config,self.pid,self.sid)['enabled'])
        self.assertTrue(session_privacy.status(self.store,self.config,self.pid,old)['enabled'])
        self.show(await self.submit('930'),'main')
        self.assertNotIn('992346721638572',str(self.providers.bodies))

    async def test_native_search_and_fetch_detect_refused_local_proxy_before_send(self):
        from persona_runtime.deepseek_native_transport import DeepSeekNativeClient
        from persona_runtime.knowledge_web_transport import request
        from persona_runtime.generation_retry import pause_before_send
        from tests.test_m9r0_native_contract import model
        native=model();live=self.config.load()['model']
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));proxy=f'http://127.0.0.1:{sock.getsockname()[1]}'
            with patch('persona_runtime.deepseek_native_transport.outbound_proxy',return_value=proxy),patch('persona_runtime.network_proxy.outbound_proxy',return_value=proxy):
                for task in (lambda:DeepSeekNativeClient().complete(native,[{'role':'user','content':'929'}]),
                             lambda:request('search',{'query':'929'},live),
                             lambda:request('fetch',{'url':'https://example.org/929'},live)):
                    with self.assertRaises(ModelError) as err:await task()
                    failure=err.exception
                    self.assertEqual(pause_before_send(str(failure),failure.diagnostics,failure.usage),'network')

    async def test_jev_local_socket_refusal_and_lost_response(self):
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        from persona_runtime.generation_retry import pause_before_send
        from persona_runtime.scene_jev import JevTransport
        bodies=[]
        async def drop(request):
            bodies.append(await request.json());request.transport.close()
            return web.Response()
        app=web.Application();app.router.add_post('/systemone',drop)
        with socket.socket() as refused:
            refused.bind(('127.0.0.1',0))
            async with TestServer(app) as server:
                for url,sent in [(f'http://127.0.0.1:{refused.getsockname()[1]}',False),(str(server.make_url('/')),True)]:
                    client=JevTransport(self.config.load()['model']|{'base_url':url})
                    try:
                        with self.assertRaises(ModelError) as err:
                            await client.complete({'model':'917'},on_dispatch=lambda _:None,on_receipt=lambda _:None,diagnostics=lambda _:None)
                        failure=err.exception
                        self.assertIs(failure.diagnostics['request_headers_sent'],sent)
                        self.assertEqual(pause_before_send(str(failure),failure.diagnostics,failure.usage),None if sent else 'network')
                    finally:await client.close()
        self.assertEqual(bodies,[{'model':'917'}])


class CandidateRollback(unittest.TestCase):
    def test_schema33_snapshot_runs_separately_and_preserves_post_upgrade_history(self):
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.storage import Store
        from persona_runtime.service import RuntimeService
        from tests.test_dialogue_schema_migration import DialogueMigration
        commit='c8b1e64781cfc6b351491960dfa3b639caf88245'
        with tempfile.TemporaryDirectory() as temp:
            self.root=Path(temp);paths=RuntimePaths(self.root/'candidate');store=Store(paths)
            rev,*old_events=DialogueMigration.old_program(self,commit,paths.home)
            self.assertEqual(rev,33)
            store.initialize()
            with store.connection() as db:
                sid=db.execute("SELECT session_id FROM events WHERE id=?",(old_events[0],)).fetchone()[0]
                from persona_runtime.storage import SCHEMA_VERSION
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],SCHEMA_VERSION)
            new_event=RuntimeService(store).append_event(sid,'user','918')['id']
            snapshot,=(paths.home/'backups').glob('before-private-reply-*.sqlite3')
            other=RuntimePaths(self.root/'rollback');other.initialize()
            shutil.copyfile(snapshot,Store(other).path)
            rev,*after=DialogueMigration.old_program(self,commit,other.home,sid)
            self.assertEqual(rev,33)
            with store.connection() as db,closing(sqlite3.connect(Store(other).path.as_uri()+'?mode=ro',uri=True)) as rollback:
                for conn in (db,rollback):
                    self.assertEqual(conn.execute('SELECT count(*) FROM events WHERE id IN (?,?)',old_events).fetchone()[0],2)
                    self.assertIsNone(conn.execute('PRAGMA foreign_key_check').fetchone())
                    self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertIsNotNone(db.execute('SELECT 1 FROM events WHERE id=?',(new_event,)).fetchone())
                self.assertIsNone(rollback.execute('SELECT 1 FROM events WHERE id=?',(new_event,)).fetchone())
                self.assertEqual(db.execute('SELECT count(*) FROM events WHERE id IN (?,?)',after).fetchone()[0],0)
                self.assertEqual(rollback.execute('SELECT count(*) FROM events WHERE id IN (?,?)',after).fetchone()[0],2)


async def crash_worker(mode,home,sid):
    from persona_runtime.config import ConfigStore
    from persona_runtime.paths import RuntimePaths
    from persona_runtime.storage import Store
    from persona_runtime.chat import ChatEngine
    paths=RuntimePaths(home);store=Store(paths);cfg=ConfigStore(paths)
    provider=stage3.ResearchProviders();provider.route='none'
    if mode=='crash-writer':
        async def lose(attempt,packet):
            (home/'writer-sent').write_text(attempt)
            os._exit(73)
        provider.reply=lose
    chat=ChatEngine(store,cfg,private_chat_providers=provider)
    if mode=='hold-buffer':
        ingress=IngressService(chat)
        with store.connection() as db:pid=store.current(db)
        policy=ingress.current_policy(pid,'desktop')
        ingress.update_policy(pid,'desktop','natural_batch',policy['id'],identifier(),manual_confirmation=True)
        for rid in ('941','942'):
            ingress.accept({'persona_id':pid,'channel':'desktop','session_id':sid,'text':rid,'request_id':rid})
        os._exit(73)
    await chat.submit('desktop','919',identifier(),sid);await chat.wait()
    raise AssertionError('crash boundary was not reached')


if __name__=='__main__':
    asyncio.run(crash_worker(sys.argv[1],Path(sys.argv[2]),sys.argv[3]))
