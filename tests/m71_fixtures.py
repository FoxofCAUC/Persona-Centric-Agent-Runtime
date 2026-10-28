"""Artificial authoritative archives; never use this seeder against a user home."""
import json
from pathlib import Path

from persona_runtime.archive import fingerprint
from persona_runtime.models import LOCAL_USER_ID, generic_persona
from persona_runtime.service import RuntimeService

FIXTURE = Path(__file__).parent/'fixtures/m71/corpus.json'


def insert(db,table,values):
    # Table/column names below are developer constants, never query input.
    names=','.join(values);marks=','.join('?' for _ in values)
    db.execute(f'INSERT INTO {table}({names}) VALUES({marks})',list(values.values()))


def seed_turn(db,pid,sid,key,texts,*,day='2026-10-20T04:00:00+00:00',channel='desktop',bot='',peer='',reply=None,normalized=False,status='completed'):
    batch=key+'-batch' if len(texts)>1 or normalized or channel=='qq' else None
    if batch:
        insert(db,'input_batches',dict(id=batch,persona_id=pid,user_id=LOCAL_USER_ID,session_id=sid,
            channel=channel,bot=bot,peer=peer,scope_key=key,state='linked',revision=1,first_seq=1,
            first_epoch=0,last_epoch=0,deadline_epoch=0,max_deadline_epoch=0,policy_json='{}',
            profile_snapshot='{}',character_snapshot='{}',created_at=day,updated_at=day))
    ids=[]
    for i,text in enumerate(texts,1):
        eid=key+f'-e{i}';ids.append(eid)
        insert(db,'events',dict(id=eid,session_id=sid,persona_id=pid,user_id=LOCAL_USER_ID,role='user',text=text,created_at=day))
        if batch:
            rawid=eid+'-raw';receipt=eid+'-receipt'
            insert(db,'raw_events',dict(id=rawid,persona_id=pid,user_id=LOCAL_USER_ID,event_id=eid,
                channel=channel,bot=bot,peer=peer,platform_message_id=eid+'-platform' if channel=='qq' else None,
                platform_time=str(1792468800) if channel=='qq' else None,received_at=day,
                representation='normalized_payload',parser_version=1,payload_json=json.dumps({'fixture':True,'text':text}),fingerprint=fingerprint(text)))
            insert(db,'ingress_receipts',dict(id=receipt,source_key=receipt,request_id=receipt,persona_id=pid,
                user_id=LOCAL_USER_ID,channel=channel,bot=bot,peer=peer,session_id=sid,fingerprint=fingerprint(text),
                raw_event_id=rawid,event_id=eid,batch_id=batch,received_at=day))
            insert(db,'input_batch_members',dict(batch_id=batch,persona_id=pid,user_id=LOCAL_USER_ID,
                receipt_id=receipt,event_id=eid,member_order=i))
    reply_id=key+'-reply' if reply else None
    if reply:
        insert(db,'events',dict(id=reply_id,session_id=sid,persona_id=pid,user_id=LOCAL_USER_ID,
                               role='persona',text=reply,created_at=day))
    insert(db,'turns',dict(id=key,source_key=key,persona_id=pid,session_id=sid,channel=channel,
        user_event_id=ids[0],reply_event_id=reply_id,status=status,model_snapshot='{}',created_at=day,updated_at=day,
        origin_json=json.dumps({'bot':bot,'sender':peer}) if channel=='qq' else '{}',input_batch_id=batch))
    return ids


def seed_fixture(store):
    from persona_runtime.paths import RuntimePaths
    if store.paths.home.resolve()==RuntimePaths.from_environment().home.resolve():
        raise ValueError('Synthetic fixtures must not be loaded into the user runtime')
    service=RuntimeService(store)
    pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
    sid=service.create_session()['id']
    data=json.loads(FIXTURE.read_text(encoding='utf-8'))
    with store.connection(write=True) as db:
        for item in data['turns']:
            ids=seed_turn(db,pid,sid,item['id'],item['members'],day=item.get('day','2026-10-20T04:00:00+00:00'),
                          normalized=item.get('normalized',False))
            if item.get('excluded'):
                insert(db,'source_exclusions',dict(event_id=ids[-1],persona_id=pid,user_id=LOCAL_USER_ID,
                    reason='artificial M7.1 fixture',created_at='2026-10-28T00:00:00+00:00'))
        for i in range(4):
            insert(db,'events',dict(id=f'orphan{i}',session_id=sid,persona_id=pid,user_id=LOCAL_USER_ID,
                role='user',text=f'审计专用未完成 orphan_{i}',created_at='2026-10-20T04:00:00+00:00'))
    other=service.create_persona(generic_persona())['id'];service.use_persona(other)
    other_session=service.create_session()['id']
    with store.connection(write=True) as db:
        for i in range(8):
            seed_turn(db,other,other_session,f'other-persona-secret-{i}',[f'不应存在的记录 forbidden_{i}'])
    service.use_persona(pid)
    return pid,sid,data
