"""Check the narrow cloud purpose and the three D2 body routes without sending a request."""
import json
import os
from pathlib import Path
import sqlite3
import unittest

from experiments.corpus_reply_study import read_private
from experiments.en5_decision_contract import ACTIONS,STATES,UNDETERMINED,pro_wire
from experiments.en5_decision_d2 import body_request,grant_file,interpret,materials
from persona_runtime.archive import fingerprint
from persona_runtime.corpus_files import digest
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch
from persona_runtime.storage import Store


class Routing(unittest.TestCase):
    def test_strict_fields_and_reuse_by_complete_wire(self):
        base={'model':'deepseek-v4-pro','messages':[{'role':'system','content':'S'},{'role':'user','content':'U'}]}
        target={'id':1,'event_id':'U','role':'user','pro_target':{'message_index':1,'role':'user'}}
        case={'original':base,'targets':[target]}
        decision={'target':1,'action':ACTIONS[0],'task_state':STATES[0]}
        prior=pro_wire(base,'P',decision,[target])
        row={'decision':decision,'original':fingerprint(base),'pro_sha256':fingerprint(prior)}
        for raw in ('{}','{"id":1,"source":[],"text":"T"}','{"target":1,"target":2}', '[]','{'):
            value,status=interpret({'state':'completed','content':raw},case)
            self.assertEqual(status,'invalid')
            self.assertEqual(body_request(case,row,'P',value,status),('v8',base))
        u={'target':None,'action':UNDETERMINED,'task_state':UNDETERMINED}
        for choice, expected in ((decision,'D1'),(decision|{'action':ACTIONS[1]},'D2'),(u,'v8')):
            actual,status=interpret({'state':'completed','content':json.dumps(choice)},case)
            origin,wire=body_request(case,row,'P',actual,status)
            self.assertEqual(origin,expected)
            self.assertEqual(wire['messages'][1:],base['messages'][1:])


@unittest.skipUnless(os.environ.get('PCAR_EN5_D2_ROOT'),'private D2 approval not supplied')
class FixedUse(unittest.TestCase):
    def test_actual_receipts_and_independent_scope(self):
        root=Path(os.environ['PCAR_EN5_D2_ROOT'])
        _,req,_,_,source,model,permit=materials(root)
        paths=RuntimePaths(source/'home')
        store=Store(paths)
        path,_,grant=grant_file(root/'d2',paths,permit)
        self.assertEqual(digest(path.read_bytes()),grant)
        row=req['positions'][0];slot=row['position']+'.decision';wire=row['flash']
        guard_cloud_dispatch(store,permit['persona_id'],'dialogue_decision',model,decision_use=(grant,slot,wire))
        for context in (None,(grant,'OTHER',wire),(grant,slot,wire|{'max_tokens':127})):
            with self.assertRaisesRegex(RuntimeErrorBase,'specific_use_unavailable'):
                guard_cloud_dispatch(store,permit['persona_id'],'dialogue_decision',model,decision_use=context)
        saved=read_private(root/'d2/results.private.json')
        from contextlib import closing
        with closing(sqlite3.connect((root/'d2/calls.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            receipts=db.execute('SELECT slot,wire,raw,raw_hash,http FROM uses JOIN requests USING(hash)').fetchall()
        plans={r['position']:r for r in req['positions']}
        for slot,content,raw,sha,http in receipts:
            pos,stage=slot.split('.');wire=json.loads(content)
            result=saved['cases'][pos]
            if raw is not None:self.assertEqual(digest(raw),sha)
            if stage=='decision':
                self.assertEqual(wire,plans[pos]['flash'])
                if result['flash']['state']=='completed':
                    self.assertEqual(http,200)
                    self.assertEqual(parse_receipt(raw,False)['body'],result['flash']['content'])
            else:
                self.assertEqual(stage,'body')
                self.assertEqual(result['body_origin'],'D2')
                self.assertEqual(fingerprint(wire),result['body']['request_hash'])
