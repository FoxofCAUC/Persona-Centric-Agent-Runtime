"""Candidate context/consumption adapter, ready for package .3 integration.

Caller appends the returned block to its real model input and binds the returned
metadata to that same attempt's context_uses. Merely previewing never reinforces.
"""
import json
from .archive import fingerprint,turn_scope
from .models import RuntimeErrorBase
from .recall_support import RecallSupport
from .recall_sources import epoch

def prepare(store,turn,query,*,clock=None,**options):
    service=RecallSupport(store,**({'clock':clock} if clock else {}))
    with store.connection() as db:selected=turn_scope(db,turn)
    result=service.select(selected,query,root_id=turn['id'],**options)
    pieces=['\n本轮可用的既有资料：']
    for item in result['selected']:
        pieces.append(item['source_type']+' '+item['id']+'：\n'+item['text'])
    block='\n'.join(pieces) if result['selected'] else ''
    metadata={'version':result['policy_version'],'selection_id':result['id'],'root_id':turn['id'],
        'selected_ids':[i['id'] for i in result['selected']],'material_block_hash':fingerprint(block),
        'searched_layers':result['searched_layers'],'cold_status':result['cold_status']}
    return block,metadata,result

def record_completed(store,turn_id,*,clock=None):
    service=RecallSupport(store,**({'clock':clock} if clock else {}))
    with store.connection() as db:
        turn=db.execute('SELECT * FROM turns WHERE id=?',(turn_id,)).fetchone()
        if not turn or turn['status']!='completed':raise RuntimeErrorBase('recall_consumption_unconfirmed')
        use=db.execute('''SELECT c.attempt_id,c.details_json FROM context_uses c
            JOIN attempts a ON a.id=c.attempt_id WHERE c.turn_id=? AND a.status='completed'
            ORDER BY a.rowid DESC LIMIT 1''',(turn_id,)).fetchone()
        if not use:return []
        meta=json.loads(use['details_json']).get('recall_support')
        if not meta:return []
        record=db.execute('SELECT * FROM recall_support_searches WHERE id=?',(meta['selection_id'],)).fetchone()
        if not record or record['root_id']!=turn_id:raise RuntimeErrorBase('recall_selection_identity_invalid')
        data=json.loads(record['result_json']);selected=turn_scope(db,dict(turn))
        ids=[i['id'] for i in data['selected']]
        if ids!=meta['selected_ids'] or record['scope_key']!=fingerprint(selected):raise RuntimeErrorBase('recall_selection_identity_invalid')
        when=epoch(turn['created_at']);aid=use['attempt_id']
    return [service.record(ident,turn_id,selected,kind='context_adopted',status='completed',occurred_at=when,
        evidence={'attempt_id':aid,'selection_id':meta['selection_id'],'observable_proxy':'successful_context_adoption'}) for ident in ids]
