"""Offline D0 regression checks. All synthetic input values are identifiers."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from experiments.en5_decision_contract import (ACTIONS, DEADLINE_SECONDS, STATES, STUDY, UNDETERMINED,
    DecisionWindow, compact, flash_base, flash_wire, persona_projection, pro_wire, target_list, validate_decision)
from experiments.en5_decision_packet import FORM, build, import_selection, position_targets, review_page, reviewed_inputs
from experiments.en5_decision_sources import COMMIT, material_text
from experiments.corpus_reply_study import read_private, save_once
from persona_runtime.archive import fingerprint, serialize_reference_block
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.turn_input import render_input


MODEL = {'base_url': 'https://api.deepseek.com/v1', 'name': 'deepseek-v4-pro'}
BASE = {'model': MODEL['name'], 'thinking': {'type': 'disabled'}, 'stream': True,
        'messages': [{'role': 'system', 'content': 'SYS'}, {'role': 'user', 'content': 'U0'},
                     {'role': 'assistant', 'content': 'A0'}, {'role': 'user', 'content': 'U1'}]}
PROFILE = {'core': {'data': {'core_traits': ['K'], 'initial_background': 'X-BIO'}},
           'expression': {'data': {'style': 'S', 'X': 'X-CACHE'}},
           'relationship': {'data': {'description': 'X-RELATION'}}}


def event(event_id, role, seq, origin, pro_index, **extra):
    return {'event_id': event_id, 'role': role, 'sequence': seq, 'text': event_id, 'source': origin,
            'session_id': 'S1', 'persona_id': 'P1', 'displayed': True,
            'pro_target': {'message_index': pro_index, 'role': role}, **extra}


def targets():
    return target_list('P1', 'S1', [event('U1', 'user', 3, 'current', 3),
        event('A0', 'assistant', 2, 'reference', 0), event('A0', 'assistant', 2, 'recent', 2),
        event('U0', 'user', 1, 'recent', 1)])


def chosen(target=3):
    return {'target': target, 'action': ACTIONS[0], 'task_state': STATES[0]}


def unknown():
    return {'target': None, 'action': UNDETERMINED, 'task_state': UNDETERMINED}


class Materials(unittest.TestCase):
    def test_flash_has_only_persona_projection_scoped_messages_and_transport_fields(self):
        approved = persona_projection(PROFILE)
        self.assertEqual(approved, {'core_traits': ['K'], 'expression': {'style': 'S'}})
        rows = targets()
        self.assertEqual([t['event_id'] for t in rows], ['U0', 'A0', 'U1'])
        self.assertEqual(rows[1]['origins'], ['reference', 'recent'])
        base = flash_base(MODEL, approved, rows)
        result = flash_wire(base, 'F')
        self.assertEqual(result['model'], 'deepseek-flash')
        self.assertEqual(result['thinking'], {'type': 'disabled'})
        self.assertEqual(result['response_format'], {'type': 'json_object'})
        self.assertNotIn('tools', result)
        self.assertEqual(result['messages'][0]['content'], 'F\n\n' + base['messages'][0]['content'])
        self.assertEqual([m['role'] for m in result['messages']], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(json.loads(result['messages'][-1]['content']), {'id': 3, 'source': ['current'], 'text': 'U1'})
        for forbidden in ('X-BIO', 'X-CACHE', 'X-RELATION', 'pro_target', 'event_id', 'message_index', 'persona_id'):
            self.assertNotIn(forbidden, compact(result))

    def test_explicit_reference_and_members_have_individual_shared_addresses(self):
        events = [event('OLD', 'assistant', 1, 'reference', 0,
                        pro_target={'message_index': 0, 'reference_event': 'OLD', 'role': 'assistant'}),
                  event('U1', 'user', 2, 'current', 3, pro_target={'message_index': 3, 'role': 'user', 'member': 1}),
                  event('U2', 'user', 3, 'current', 3, pro_target={'message_index': 3, 'role': 'user', 'member': 2})]
        rows = target_list('P1', 'S1', events)
        original = BASE | {'messages': [BASE['messages'][0] | {'content':'REF-OLD'}, *BASE['messages'][1:]]}
        for i, e in enumerate(events, 1):
            pro = pro_wire(original, 'P', chosen(i), rows)
            handed = json.loads(pro['messages'][0]['content'].split('\n\n')[-1])
            self.assertEqual(handed, chosen(i) | {'target': e['pro_target']})
            self.assertEqual(pro['messages'][1:], original['messages'][1:])
        self.assertEqual(pro_wire(original, 'P', unknown(), rows), original)
        with self.assertRaisesRegex(RuntimeErrorBase,'reference_address'):
            pro_wire(BASE,'P',chosen(1),rows)

    def test_wrong_scope_unpresented_and_conflicting_duplicates_are_rejected(self):
        current = event('U1', 'user', 2, 'current', 3)
        for change in ({'session_id':'S2'}, {'persona_id':'P2'}, {'displayed':False}, {'source':'retrieval'}):
            with self.subTest(change=change), self.assertRaises(RuntimeErrorBase):
                target_list('P1', 'S1', [current, event('A0','assistant',1,'recent',2) | change])
        with self.assertRaisesRegex(RuntimeErrorBase,'source_changed'):
            target_list('P1','S1',[current,current | {'text':'U2'}])

    def test_illegal_target_and_mixed_unknown_do_not_become_choices(self):
        for value in ([], chosen(True), chosen(4), chosen() | {'action':'X'}, unknown() | {'target': 1},
                      chosen() | {'extra':'X'}, chosen(None)):
            with self.subTest(value=value), self.assertRaises(RuntimeErrorBase):
                validate_decision(value,targets())

    def test_author_passages_use_unicode_codepoints_and_preserve_whitespace(self):
        items = [{'key':'A','stages':['flash'],'text':'A😀\nB'}]
        note = {'passages':[{'key':'A','start':1,'end':3}], 'addition':' Z '}
        self.assertEqual(material_text(items, 'flash', note), ('😀\n\n Z ', []))
        with self.assertRaisesRegex(RuntimeErrorBase,'passage_scope'):
            material_text(items,'pro',note)
        with self.assertRaisesRegex(RuntimeErrorBase,'passage_scope'):
            material_text(items,'flash',note | {'passages':[{'key':'A','start':0,'end':5}]})

    def test_edited_or_draft_form_cannot_be_imported_as_final_input(self):
        rows=targets(); f=flash_base(MODEL,persona_projection(PROFILE),rows)
        case={'position':'1','targets':rows,'flash_base':f,'original':BASE,'original_sha256':fingerprint(BASE),
              'capacity':60000,'scope':'SCOPE'}
        packet={'cases':[case], 'sources':[]}
        value={'version':FORM,'study':STUDY,'binding':'B','kind':'selection',
               'material':{k:{'passages':[],'addition':k.upper()+'-ID'} for k in ('flash','pro')},
               'decisions':{'1':chosen()},'previews':{'1':{'flash':fingerprint(flash_wire(f,'FLASH-ID')),
                                                          'pro':pro_wire(BASE,'PRO-ID',chosen(),rows)}}}
        self.assertEqual(reviewed_inputs(packet,value,'B')['use'],'awaiting_specific_input_review')
        for v in (value | {'kind':'draft'}, value | {'binding':'C'}, value | {'decisions':{}},
                  value | {'previews':{'1':{'flash':'C','pro':'P'}}}):
            with self.assertRaises(RuntimeErrorBase):
                reviewed_inputs(packet,v,'B')

    def test_import_preserves_original_export_and_builds_only_a_local_preview(self):
        rows = targets()
        flash = flash_base(MODEL, persona_projection(PROFILE), rows)
        case = {'position':'ID','scope':'SCOPE','targets':rows,'capacity':60000,
                'original':BASE,'original_sha256':fingerprint(BASE),'flash_base':flash}
        packet = {'study':STUDY,'sources':[],'cases':[case],'actions':list(ACTIONS),'states':list(STATES),
                  'commit':COMMIT,'undetermined':UNDETERMINED,'official_endpoint':MODEL['base_url'],
                  'prior':'QA-PRIOR','upstream':'QA-SOURCE'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checksum = save_once(root/'material.private.json', packet)
            (root/'index.html').write_bytes(review_page(packet, checksum).encode())
            form = {'version':FORM,'study':STUDY,'binding':checksum,'kind':'selection',
                    'material':{'flash':{'passages':[],'addition':'F'},'pro':{'passages':[],'addition':'P'}},
                    'decisions':{'ID':unknown()},
                    'previews':{'ID':{'flash':fingerprint(flash_wire(flash,'F')),'pro':fingerprint(BASE)}}}
            export = root/'offline-test.json'
            save_once(export, form)
            with patch('experiments.en5_decision_packet.build',return_value=packet):
                folder = import_selection(root,export)
                self.assertEqual(import_selection(root,export),folder)
            self.assertEqual((folder/'submitted.private.json').read_bytes(),export.read_bytes())
            result = read_private(folder/'requests.private.json')
            self.assertEqual((result['d1_unique_changed'],result['d1_reused_original']),(0,1))
            self.assertEqual(result['use'],'awaiting_specific_input_review')
            self.assertTrue((folder/'inputs.html').is_file())
            self.assertFalse((root/'calls.sqlite3').exists())

    def test_reference_projection_excludes_other_text_and_keeps_real_speaker(self):
        def member(ident, role):
            return {'id': ident, 'role': role, 'text': ident}
        def turn(ident, users, reply, shown, accepted):
            return {'turn': {'id': ident, 'persona_id': 'P1', 'session_id': 'S1', 'input_batch_id': ident},
                    'members': [member(u, 'user') for u in users], 'reply': member(reply, 'assistant'),
                    'shown': shown, 'accepted': accepted}
        turns = {'OLD': turn('OLD',['U0'],'A0',1,0), 'PREV': turn('PREV',['U1'],'A1',3,2),
                 'NOW': turn('NOW',['U2','U3'],'A2',6,5)}
        group = {'turn_id':'OLD','targets':[{'kind':'event','id':'A0'}], 'members': turns['OLD']['members'],
                 'replies':[{'kind':'generated_reply','id':'A0','text':'A0'}]}
        reference = serialize_reference_block([group], [])
        meta = {'references': [{'turn_id':'OLD'}]}
        details = {'history_groups':[{'kind':'recent','turn_id':'PREV'}], 'references':meta}
        messages = [{'role':'system','content':'S'+reference},{'role':'user','content':'U1'},
                    {'role':'assistant','content':'A1'},
                    {'role':'user','content':render_input(turns['NOW']['members'])}]
        record = {'turn_id':'NOW','wire':{'messages':messages},'input_sha256':fingerprint(messages)}
        from contextlib import closing
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory=sqlite3.Row
            db.executescript("""CREATE TABLE events(id,seq,persona_id,session_id,role,text);
                CREATE TABLE context_uses(attempt_id,turn_id,details_json);
                CREATE TABLE attempts(id,status); CREATE TABLE context_manifests(attempt_id,task_type,manifest_json);""")
            for seq, (ident, role) in enumerate([('U0','user'),('A0','persona'),('U1','user'),
                                                  ('A1','persona'),('U2','user'),('U3','user')]):
                db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',(ident,seq,'P1','S1',role,ident))
            db.execute("INSERT INTO attempts VALUES ('ATT','completed')")
            db.execute("INSERT INTO context_uses VALUES ('ATT','NOW',?)",(json.dumps(details),))
            db.execute("INSERT INTO context_manifests VALUES ('ATT','dialogue',?)",
                       (json.dumps({'input_snapshot_hash':fingerprint(messages)}),))
            with patch('experiments.en5_decision_packet.prepare_references', return_value=(reference, meta)):
                rows = position_targets(db, record, turns)
            self.assertEqual([t['text'] for t in rows],['A0','U1','A1','U2','U3'])
            self.assertEqual(rows[0]['pro_target'],{'message_index':0,'reference_event':'A0','role':'assistant'})
            self.assertEqual(rows[-1]['pro_target'],{'message_index':3,'role':'user','member':2})


class Deadline(unittest.IsolatedAsyncioTestCase):
    async def test_local_pro_consumer_rechecks_scope_and_uses_one_wire_after_timeout(self):
        now = [0.]
        window = DecisionWindow('TURN', targets(), clock=lambda: now[0])
        window.dispatched()
        now[0] = DEADLINE_SECONDS
        sent = []
        async def consume():
            wire, reason = await window.pro_request('TURN', BASE, 'P', lambda: None)
            sent.append(wire)
            return reason
        self.assertEqual(await consume(), 'deadline')
        self.assertFalse(window.received('TURN', compact(chosen())))
        self.assertEqual(sent, [BASE])
        with self.assertRaisesRegex(RuntimeErrorBase, 'wrong_turn'):
            await window.pro_request('OTHER', BASE, 'P', lambda: None)
        def withdrawn():
            raise RuntimeErrorBase('withdrawn')
        with self.assertRaisesRegex(RuntimeErrorBase,'withdrawn'):
            await window.pro_request('TURN', BASE, 'P', withdrawn)
        valid = DecisionWindow('TURN', targets())
        valid.dispatched()
        valid.received('TURN', compact(chosen()))
        wire, why = await valid.pro_request('TURN', BASE, 'P', lambda: None)
        self.assertEqual((wire, why), (pro_wire(BASE,'P',chosen(),targets()), 'selected'))
        closed = DecisionWindow('TURN', targets())
        closed.dispatched()
        closed.cancel()
        with self.assertRaisesRegex(RuntimeErrorBase,'turn_cancelled'):
            await closed.pro_request('TURN', BASE, 'P', lambda: None)

    async def test_dispatch_starts_clock_and_timely_response_survives_a_later_join(self):
        now=[0.0]
        window=DecisionWindow('S',targets(),clock=lambda:now[0])
        now[0]=40.0
        window.dispatched()
        self.assertFalse(window.received('OTHER',compact(chosen())))
        now[0]+=2.999
        self.assertTrue(window.received('S',compact(chosen())))
        now[0]+=7
        result=await window.resolve()
        self.assertEqual((result.reason,result.decision),('selected',chosen()))
        window.cancel()
        self.assertIs(await window.resolve(),result)
        with self.assertRaisesRegex(RuntimeErrorBase,'already_dispatched'):
            window.dispatched()

    async def test_deadline_late_unknown_invalid_duplicate_and_cancel(self):
        for raw, delta, why in [(compact(chosen()),3.0,'deadline'),(compact(unknown()),1.,'undetermined'),
                               ('{"target":3,"target":1}',0.,'invalid'),(compact(chosen(5)),0.,'invalid')]:
            now=[0.0]
            w=DecisionWindow('S',targets(),clock=lambda:now[0]);w.dispatched()
            now[0]=delta
            w.received('S',raw)
            result=await w.resolve()
            self.assertEqual((result.reason,result.decision),(why,None))
            self.assertFalse(w.received('S',compact(chosen())))
        self.assertEqual(DEADLINE_SECONDS,3.0)
        with patch('experiments.en5_decision_contract.DEADLINE_SECONDS',.015):
            w=DecisionWindow('S',targets());w.dispatched()
            results=await asyncio.gather(w.resolve(),w.resolve())
            self.assertIs(results[0],results[1])
            self.assertEqual(results[0].reason,'deadline')
            self.assertFalse(w.received('S',compact(chosen())))
        w=DecisionWindow('S',targets());w.dispatched();w.cancel()
        self.assertFalse(w.received('S',compact(chosen())))
        self.assertEqual((await w.resolve()).reason,'cancelled')


@unittest.skipUnless(os.environ.get('PCAR_EN5_DECISION_ROOT'),'private D0 packet not supplied')
class ReviewedSources(unittest.TestCase):
    def test_all_frozen_pro_wires_and_first_visible_bodies_are_reproducible(self):
        root=Path(os.environ['PCAR_EN5_DECISION_ROOT'])
        packet=read_private(root/'material.private.json')
        self.assertEqual(fingerprint(build(packet['prior'],packet['upstream'])),fingerprint(packet))
        self.assertEqual((len(packet['baseline_audit']),len(packet['cases'])),(30,16))
        self.assertEqual(len(set(r['request_sha256'] for r in packet['baseline_audit'])),30)
        for case in packet['cases']:
            self.assertEqual(fingerprint(case['original']),case['original_sha256'])
            self.assertEqual(len(case['flash_base']['messages'][1:]),len(case['targets']))
            self.assertTrue(all(t['origins'] in (['current'],['recent']) for t in case['targets']))
            self.assertEqual(sum('recent' in t['origins'] for t in case['targets']),4)
            self.assertEqual(case['targets'][-1]['text'],case['original']['messages'][-1]['content'])

    def test_cross_version_export_cannot_write_an_author_selection(self):
        root=Path(os.environ['PCAR_EN5_DECISION_ROOT'])
        with tempfile.TemporaryDirectory() as temp:
            bad=Path(temp)/'mismatch.json'
            bad.write_text('{"study":"OFFLINE-ID"}',encoding='utf-8')
            with self.assertRaisesRegex(RuntimeErrorBase,'export_binding'):
                import_selection(root,bad)
