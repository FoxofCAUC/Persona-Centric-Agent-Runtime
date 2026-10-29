"""Monitor two natural-input calls. Never dispatch/replay a user message."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import generic_persona,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.presentation import violations
from persona_runtime.private_personas import PrivatePersonas
from persona_runtime.quotas import QuotaService
from persona_runtime.storage import Store


def main(action,slot):
    paths=RuntimePaths.from_environment();root=paths.home/'exports/acceptance/M8-preflight-20261029'
    cfg=ConfigStore(paths);store=Store(paths);client=LocalClient(cfg);quota=QuotaService(store,cfg);file=root/'generic-state.json'
    state=json.loads(file.read_text(encoding='utf-8')) if file.exists() else None
    if action=='setup':
        if state:return {'resumed':True}
        original=client.manage('persona.show')['id'];spec=generic_persona().to_dict();spec['name']='自然表达独立验收'
        pid=client.manage('persona.create',spec=spec)['id'];client.manage('persona.use',persona_id=pid)
        for channel in ('desktop','qq'):
            old=client.manage('interaction.current',persona_id=pid,channel=channel)
            client.manage('interaction.update',persona_id=pid,channel=channel,mode='legacy_per_message' if channel=='desktop' else 'natural_batch',
                reply_mode='natural_structured',context_mode='working_set',cross_session=False,character_retrieval_mode='lexical',expected_version=old['id'],operation_id=identifier())
        state={'persona_id':pid,'original_persona_id':original,'slots':{}};atomic_json(file,state)
        return {'test_persona_ready':True,'private_persona_used':False}
    if state is None:raise RuntimeError('Run setup first')
    if action=='arm':
        if slot in state['slots']:raise RuntimeError('Slot already armed; observe instead of replay')
        PrivatePersonas(paths).assert_acceptance(state['persona_id'])
        client.manage('persona.use',persona_id=state['persona_id'])
        with store.connection() as db:seq=db.execute('SELECT coalesce(max(rowid),0) FROM turns').fetchone()[0]
        state['slots'][slot]={'after_rowid':seq,'status':'armed','channel':'desktop' if slot=='window' else 'qq'}
        atomic_json(file,state);quota.begin_acceptance()
        return {'armed':slot,'no_message_dispatched':True}
    if action=='capture':
        record=state['slots'][slot]
        if record['status']!='armed':return {'already_completed':True,'passed':record.get('passed')}
        with store.connection() as db:
            rows=db.execute('SELECT id,status FROM turns WHERE persona_id=? AND channel=? AND rowid>? ORDER BY rowid',
                (state['persona_id'],record['channel'],record['after_rowid'])).fetchall()
        if not rows:return {'waiting':True}
        if len(rows)!=1:quota.end_acceptance();raise RuntimeError('Unexpected extra input; reconcile ledger')
        turn=client.call('GET','/v1/requests/'+rows[0]['id'])
        if turn['status'] in ('queued','running'):return {'waiting':True,'phase':turn.get('phase')}
        try:
            segments=(turn.get('reply_batch') or {}).get('segments',[])
            raw_errors=[{'index':i,'violations':violations(s['text'],s.get('content_kind','chat'))} for i,s in enumerate(segments)
                if violations(s['text'],s.get('content_kind','chat'))]
            presentation=turn.get('presentation') or {};passed=turn['status']=='completed' and turn['attempt_count']==1 and not raw_errors and not presentation.get('error')
            sent=None
            if record['channel']=='qq' and turn.get('delivery_stream'):
                view=client.manage('delivery.show',stream_id=turn['delivery_stream']['id'],persona_id=state['persona_id'])
                parts=[p for p in view['parts'] if p['turn_id']==turn['id']]
                if any(p['state'] in ('pending','sending') for p in parts):return {'waiting':True,'phase':'platform_delivery'}
                sent=bool(parts) and all(p['state']=='sent' and p['attempt']==1 and p['platform_message_id'] is not None for p in parts)
                passed=passed and sent;atomic_json(root/'generic-qq-delivery.json',view)
            record.update(status='completed',turn_id=turn['id'],passed=passed,raw_format_errors=raw_errors,sent_once=sent,
                user_read='unknown',main_calls=turn['attempt_count'],selector_calls=len(turn.get('selection_attempts',[])))
            atomic_json(root/('generic-'+slot+'.json'),turn);atomic_json(file,state)
            return {k:record[k] for k in ('status','passed','main_calls','selector_calls','raw_format_errors','sent_once')}
        finally:
            if record['status']=='completed':quota.end_acceptance()
    if action=='restore':
        quota.end_acceptance();client.manage('persona.use',persona_id=state['original_persona_id'])
        return {'selection_restored':True,'quotas_restored':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('setup','arm','capture','restore'))
    parser.add_argument('--slot',choices=('window','qq'));args=parser.parse_args()
    print(json.dumps(main(args.action,args.slot),ensure_ascii=False))
