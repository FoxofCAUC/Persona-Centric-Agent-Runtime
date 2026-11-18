"""Offline marker tests for versioned asset scope, speaker roles and projected history."""
import asyncio
import json
import os
from pathlib import Path
import re
import sqlite3
import unittest
from unittest.mock import patch

from persona_runtime.dialogue_assembly import DialogueAssembly
from persona_runtime.dialogue_generation import DialogueGeneration
from persona_runtime.dialogue_portable import PROFILE, adapt_bundle, review_manifest
from persona_runtime.corpus_files import CorpusError


STAMP = '2026-11-18T12:00:00+08:00'
KEYS = ('ID-0', 'ID-1', 'ID-2')


def bundle():
    source = DialogueAssembly(None)
    model = {'name':'deepseek-flash','base_url':'https://api.deepseek.com','max_tokens':100,'max_input_chars':60000,
             '_companion_profile':{'version':'companion-runtime-v8'}}
    assets = {key:{'id':key, 'style':f'P{i}', 'situation':f'S{i}', 'source_quote':f'Q{i}'}
              for i,key in enumerate(KEYS,1)}
    source._current_bundle_id = 'X'
    source._bundles['X'] = ({
        'candidate_numbers':{str(i):key for i,key in enumerate(KEYS,1)}, 'flash_system':'0',
        'flash_user':'{time_block}\n{dialogue_prompt}\n{extra_info_block}', 'persona':'1', 'speaker':'2',
        'selector_system':{'role':'system','content':'JSON\n1: S1 P1\n2: S2 P2\n3: S3 P3\n'},
        'flash_model':model, 'selector_model':model|{'_json_output':True},
        'variables':{'time_block':{'prefix':'0'},'dialogue_prompt':{'line_format':'{time}/{speaker}/{text}'},
                     'planner_reasoning':{'format':'3{thought}'},'expression_habits_block':{
                        'prefix':'4', 'item_format':'{situation}|{style}', 'suffix_if_present':''}}
    },assets)
    return source


class Portable(unittest.TestCase):
    def setUp(self):
        self.previous = bundle()
        manifest, assets = self.previous._bundles['X']
        self.manifest = review_manifest(manifest, KEYS[:2], {KEYS[2]:'source_speaker_state'})
        src = bundle()
        src._bundles['X'] = self.manifest, assets
        with patch('persona_runtime.dialogue_assembly.load_bundle',return_value=src):
            self.direct = DialogueGeneration(None).load_assembly(None, 'P', 'X')
        self.turn = {'id':'T','persona_id':'P','session_id':'S'}
        self.messages = [{'role':'system','content':'Z0'}, {'role':'user','content':'U1'},
                         {'role':'assistant','content':'P0'}, {'role':'user','content':'U2'}]
        self.material = self.direct.material(self.turn, 'X', manifest['flash_model'], self.messages,
                                             'main', [('U1','P0',STAMP,STAMP)], 'U2', STAMP)

    def test_positive_scope_masks_candidates_at_both_boundaries(self):
        result=self.direct.selection_audit('{"selected_ids":[1,3]}',self.material)
        self.assertEqual(result, {'requested_ids':[KEYS[0],KEYS[2]],'applied_ids':[KEYS[0]],
                                  'omitted':{KEYS[2]:'source_speaker_state'}})
        _, selector=self.direct.request('selection',self.turn,self.material,'T0',[])
        self.assertEqual(selector['messages'][0]['content'],'JSON\n1: S1 P1\n2: S2 P2\n')
        self.assertIn('T0',selector['messages'][-1]['content'])
        with self.assertRaises(CorpusError):
            self.direct.request('reply',self.turn,self.material,'T0',[KEYS[2]])
        with self.assertRaises(CorpusError):
            self.direct.parse_selection('{"selected_ids":[9]}',self.material)

    def test_recent_scope_expires_and_unknown_candidates_abstain(self):
        old=self.direct.with_recent(self.material,[[KEYS[1]],[KEYS[0]]])
        self.assertFalse(self.direct.selectable(old))
        another=self.direct.with_recent(self.material,[[KEYS[0]],[]])
        self.assertEqual(self.direct.eligible_ids(another),[KEYS[1]])
        self.assertEqual(self.direct.parse_selection('{"selected_ids":[1,2]}',another),[KEYS[1]])
        self.assertEqual(self.direct.eligible_ids(self.material),list(KEYS[:2]))
        self.assertEqual(self.direct.eligible_ids(self.direct.with_recent(self.material,[[],[]])),list(KEYS[:2]))
        with self.assertRaises(CorpusError):
            self.direct.with_recent(self.material,[['unknown']])

    def test_role_channels_and_replyer_thought_isolation(self):
        _, thought=self.direct.request('understanding',self.turn,self.material,None,[])
        self.assertEqual([m['role'] for m in thought['messages']],['system','user','assistant','user'])
        self.assertIn('P0',thought['messages'][2]['content'])
        self.assertNotIn('P0',thought['messages'][-1]['content'])
        _, a=self.direct.request('reply',self.turn,self.material,'T-BAD',[KEYS[0]])
        _, b=self.direct.request('reply',self.turn,self.material,'T-OTHER',[KEYS[0]])
        self.assertEqual(a,b)
        self.assertEqual(a['messages'][:-2],self.messages[:-1])
        self.assertIn('S1|P1',a['messages'][-2]['content'])
        self.assertEqual(a['messages'][-1],self.messages[-1])
        _, plain=self.direct.request('reply',self.turn,self.material,'T-BAD',[])
        self.assertEqual(plain['messages'],self.messages)

    def test_loader_preserves_legacy_contract_and_requires_valid_manifest(self):
        self.assertIs(adapt_bundle(self.previous),self.previous)
        manifest, assets = self.previous._bundles['X']
        material=self.previous.material(self.turn,'X',manifest['flash_model'],self.messages,'main',[], 'U2',STAMP)
        _, wire=self.previous.request('reply',self.turn,material,'T0',[KEYS[2]])
        self.assertIn('S3|P3',wire['messages'][-2]['content'])
        self.assertIn('T0',wire['messages'][-2]['content'])
        self.assertNotEqual(material['version'],PROFILE)
        with self.assertRaises(CorpusError):
            self.direct.request('reply',self.turn,material,'T0',[])
        self.previous._bundles['X']=(self.manifest|{'expression_projection':{}},assets)
        with self.assertRaises(CorpusError):
            adapt_bundle(self.previous)

    def test_live_recent_uses_only_confirmed_replies_in_same_scope(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE turns(id,persona_id,session_id,status,reply_event_id);
            CREATE TABLE expression_speech_units(id,turn_id,source_id,source_kind,phase);
            CREATE TABLE expression_speech_publications(utterance_id,canonical_event_id,state);
            CREATE TABLE dialogue_generation_units(id,turn_id,phase,material_json);
            CREATE TABLE dialogue_generation_stages(id,unit_id,stage,state,validation_error,artifact_json);
        ''')
        def add(tid,session,pid,status,confirmed):
            event=tid+'e' if confirmed else None
            db.execute('INSERT INTO turns VALUES(?,?,?,?,?)',(tid,pid,session,status,event))
            db.execute('INSERT INTO expression_speech_units VALUES(?,?,?,?,?)',(tid,tid,tid+'r','dialogue_reply','main'))
            db.execute('INSERT INTO expression_speech_publications VALUES(?,?,?)',(tid,event,'complete'))
            db.execute('INSERT INTO dialogue_generation_units VALUES(?,?,?,?)',
                       (tid,tid,'main',json.dumps({'version':PROFILE,'bundle_id':'X'})))
            db.execute('INSERT INTO dialogue_generation_stages VALUES(?,?,?,?,?,?)',(tid+'r',tid,'reply','completed',None,None))
            db.execute('INSERT INTO dialogue_generation_stages VALUES(?,?,?,?,?,?)',
                       (tid+'s',tid,'selection','completed',None,json.dumps([KEYS[0]])))
        add('a','S','P','completed',True)
        add('b','other','P','completed',True)
        add('c','S','other','completed',True)
        add('d','S','P','running',False)
        db.execute("INSERT INTO turns VALUES('T','P','S','running',NULL)")
        def freeze(usable=True):
            with patch('persona_runtime.dialogue_portable.turn_scope',return_value={}), \
                    patch('persona_runtime.dialogue_portable.read_exact',return_value={'usable':usable}), \
                    patch.object(DialogueAssembly,'_freeze',return_value=self.material):
                return self.direct._freeze(db,self.turn,'X',{},[],'main',{})['recent_projection_ids']
        self.assertEqual(freeze(),[[KEYS[0]]])
        self.assertEqual(freeze(usable=False),[[]])
        db.execute("UPDATE expression_speech_publications SET state='closed' WHERE utterance_id='a'")
        self.assertEqual(freeze(),[[]])
        db.execute("UPDATE expression_speech_publications SET state='complete',canonical_event_id=NULL WHERE utterance_id='a'")
        self.assertEqual(freeze(),[[]])

@unittest.skipUnless(os.environ.get('PCAR_DIALOGUE_REPLAY_STATE') and os.environ.get('PCAR_PORTABLE_REVIEW_BUNDLE'),
                     'private offline materials not supplied')
class DesktopPortable(unittest.IsolatedAsyncioTestCase):
    from tests import test_dialogue_material_sources as fixtures
    decision_transport = fixtures.ReviewedIngress.decision_transport
    transport = fixtures.ReviewedIngress.transport
    wait_for_plan = fixtures.ReviewedIngress.wait_for_plan
    present = fixtures.ReviewedIngress.present
    asyncTearDown = fixtures.ReviewedIngress.asyncTearDown

    async def asyncSetUp(self):
        from persona_runtime.expression_corpus_sources import approval_fields, document, register_bundle, scope_record
        from persona_runtime.corpus_files import digest
        from persona_runtime.models import identifier
        await self.fixtures.ReviewedIngress.asyncSetUp(self)
        self.need_web = False
        # Use the actual loader; both authorities and the transport below stay in this temporary fixture.
        del self.g.load_assembly
        copied, _ = document(Path(os.environ['PCAR_PORTABLE_REVIEW_BUNDLE']))
        copied = copied | {'corpus_root':str(self.corpus.root), 'learning_root':str(self.learning.root)}
        path = self.paths.home/'portable.json'
        path.write_text(json.dumps(copied,ensure_ascii=False),encoding='utf-8')
        with self.store.connection() as db:
            target = scope_record(db,self.store,self.pid)
        approval = self.paths.home/'offline-fixture.json'
        approval.write_text(json.dumps(approval_fields(digest(path.read_bytes()),target)),encoding='utf-8')
        self.bundle = register_bundle(self.store,self.pid,path,approval_path=approval)
        self.g.configure(self.pid,{'enabled':True,'bundle_id':self.bundle},1,identifier())
        class MarkerTransport:
            def __init__(self):self.wires=[]
            async def complete(self, model, wire, *, save_raw):
                self.wires.append(wire)
                if wire.get('response_format'):
                    first = re.search(r'^([1-9][0-9]*):',wire['messages'][0]['content'],re.M)
                    body = json.dumps({'selected_ids':[int(first[1])]})
                else:
                    body = f"ID-RECEIPT-{len(self.wires)}"
                if wire['stream']:
                    value = {'model':wire['model'],'choices':[{'delta':{'content':body},'finish_reason':'stop'}]}
                    packet = 'data: '+json.dumps(value)+'\n\ndata: [DONE]\n'
                else:
                    value = {'model':wire['model'],'choices':[{'message':{'content':body},'finish_reason':'stop'}]}
                    packet = json.dumps(value)
                save_raw(packet.encode(),http_status=200,diagnostics={})
        self.client = self.g.client = MarkerTransport()

    async def test_confirmed_selection_cools_down_in_batched_desktop_session(self):
        from persona_runtime.ingress import IngressService
        from persona_runtime.models import identifier
        turn = await self.chat.submit('desktop','ID-U1',identifier())
        uid = await self.wait_for_plan(turn,'main')
        spoken = self.present(turn,uid)
        await asyncio.wait_for(self.chat.wait(),10)
        ingress = IngressService(self.chat)
        for marker in ('ID-U2A','ID-U2B'):
            batch = ingress.accept({'text':marker,'request_id':identifier(),'channel':'desktop',
                                    'session_id':turn['session_id'],'persona_id':self.pid})
        linked = ingress.action(batch['batch_id'],'seal',batch['batch_revision'],identifier(),self.pid)
        second = self.chat.get(linked['turn_id'])
        uid = await self.wait_for_plan(second,'main')
        self.present(second,uid)
        await asyncio.wait_for(self.chat.wait(),10)
        first_u,first_s,first_r,next_u,next_s,next_r=self.client.wires
        def candidates(wire):
            return re.findall(r'^([1-9][0-9]*):',wire['messages'][0]['content'],re.M)
        initial=candidates(first_s)
        self.assertTrue(initial)
        self.assertEqual(candidates(next_s),initial[1:])
        self.assertEqual([m['role'] for m in next_u['messages']],['system','user','assistant','user'])
        self.assertIn(spoken,next_u['messages'][2]['content'])
        self.assertNotIn(spoken,next_u['messages'][-1]['content'])
        for marker in ('ID-U2A','ID-U2B'):
            self.assertIn(marker,next_u['messages'][-1]['content'])
            self.assertIn(marker,next_r['messages'][-1]['content'])
        self.assertNotIn('ID-RECEIPT-4',str(next_r['messages']))
        self.assertEqual(self.chat.get(second['id'])['status'],'completed')
        self.assertEqual(self.main.calls,[])
        self.assertEqual(self.main.notice_calls,[])
