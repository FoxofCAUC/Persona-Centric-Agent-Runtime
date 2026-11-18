"""Durable first-display acknowledgement for an opted-in desktop search preface."""
import json
import time

from .archive import fingerprint
from .models import RuntimeErrorBase, identifier
from .progress import record


CONTRACT = 'tk-visible-v1'
PREFIX = 'knowledge.desktop_notice.'


def required(db, turn):
    if turn['channel'] != 'desktop' or not turn.get('input_batch_id'):
        return False
    row=db.execute('SELECT policy_json FROM input_batches WHERE id=?',(turn['input_batch_id'],)).fetchone()
    return bool(row and json.loads(row[0]).get('desktop_presentation') == CONTRACT)


def freeze_sources(db,turn,task_id,details):
    from .expression_sources import capture
    if details.get('history_rows') and not details.get('history_groups'):
        groups=[]
        for ordinal in details['history_rows']:
            row=db.execute('SELECT id FROM turns WHERE rowid=? AND persona_id=? AND session_id=?',
                           (ordinal,turn['persona_id'],turn['session_id'])).fetchone()
            if not row:raise RuntimeErrorBase('search_notice_history_changed')
            groups.append({'turn_id':row[0]})
        details=dict(details,history_groups=groups)
    value={'guard':capture(db,turn,details),'details':{k:details.get(k) for k in ('knowledge','recall_support')}}
    key='knowledge.desktop_notice_sources.'+task_id
    old=db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()
    if old:
        if json.loads(old[0])!=value:raise RuntimeErrorBase('search_notice_sources_changed')
    else:db.execute('INSERT INTO knowledge_control VALUES(?,?)',(key,json.dumps(value)))


def validate_sources(db,store,turn,task_id):
    from .expression_sources import validate
    row=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.desktop_notice_sources.'+task_id,)).fetchone()
    if not row:raise RuntimeErrorBase('search_notice_sources_missing')
    context=json.loads(row[0])
    # A manually resumed root can reuse the same paid preface after its original
    # inputs, persona, recall and references have been rechecked.
    validate(store,db,turn|{'execution_revision':context['guard']['execution_revision']},context)


def already_presented(db, turn_id):
    row=db.execute("SELECT json_extract(value,'$.data.fully_presented') FROM knowledge_control WHERE key=?",
                   ('knowledge.search_notice.turn.'+turn_id,)).fetchone()
    return bool(row and row[0])


def _pending(db, turn_id):
    row=db.execute('SELECT value FROM knowledge_control WHERE key=?',(PREFIX+turn_id,)).fetchone()
    return json.loads(row[0]) if row else None


def prepare(db, turn, task_id, response):
    value=_pending(db,turn['id'])
    if value:
        if (value['task_id'],value['response_hash']) != (task_id,fingerprint(response)):
            raise RuntimeErrorBase('search_notice_receipt_conflict')
    else:
        value={'notice_id':identifier(),'task_id':task_id,'response_hash':fingerprint(response)}
        db.execute('INSERT INTO knowledge_control VALUES(?,?)',(PREFIX+turn['id'],json.dumps(value)))
        record(db,turn['id'],'knowledge_search_notice')
    return {'notice_id':value['notice_id'],'published':False}


def waiting(db, session_id):
    rows=db.execute("""SELECT t.id,json_extract(c.value,'$.notice_id') FROM turns t
        JOIN knowledge_control c ON c.key=?||t.id
        WHERE t.session_id=? AND t.status='queued' AND t.cancelled_at IS NULL
        AND NOT EXISTS(SELECT 1 FROM knowledge_control n WHERE n.key='knowledge.search_notice.turn.'||t.id)
        ORDER BY t.rowid""",(PREFIX,session_id))
    return [{'turn_id':r[0],'notice_id':r[1]} for r in rows]


def _active(db,store,turn):
    if store.current(db,required=False)!=turn['persona_id'] or turn['status']!='queued' or turn.get('cancelled_at'):
        raise RuntimeErrorBase('knowledge_request_inactive')
    # Once a newer user input is accepted, an unseen preface can no longer begin this search.
    newer=db.execute("""SELECT 1 FROM ingress_receipts WHERE session_id=? AND accepted_seq >
          (SELECT max(accepted_seq) FROM ingress_receipts WHERE batch_id=?) LIMIT 1""",
          (turn['session_id'],turn['input_batch_id'])).fetchone()
    if newer:raise RuntimeErrorBase('search_notice_superseded')


def display(notice, persona_id, session_id, turn_id, notice_id, *, acknowledge=False):
    store=notice.store
    with store.connection(write=acknowledge) as db:
        row=db.execute("SELECT * FROM turns WHERE id=? AND session_id=? AND persona_id=? AND channel='desktop'",
                       (turn_id,session_id,persona_id)).fetchone()
        if not row or store.current(db,required=False)!=persona_id:raise RuntimeErrorBase('knowledge_scope_changed')
        turn=dict(row)
        pending=_pending(db,turn_id)
        if not pending or pending['notice_id']!=notice_id:raise RuntimeErrorBase('desktop_notice_not_pending')
        canonical=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.search_notice.turn.'+turn_id,)).fetchone()
        if canonical:
            old=json.loads(canonical[0])['data']
            if old.get('notice_id')!=notice_id:raise RuntimeErrorBase('search_notice_receipt_conflict')
            return {'published':True,'event_id':old['event_id']}
        _active(db,store,turn)
        task=db.execute('SELECT * FROM organization_tasks WHERE id=?',(pending['task_id'],)).fetchone()
        if not task or task['status']!='completed':raise RuntimeErrorBase('desktop_notice_not_pending')
        payload=json.loads(task['payload_json'])
        managed=notice.processor.engine.managed
        contract=managed.contract(db,task['id'])
        managed.check(db,dict(task),contract,payload)
        validate_sources(db,store,turn,task['id'])
        receipt=db.execute("""SELECT r.* FROM organization_receipts r JOIN organization_attempts a ON a.id=r.attempt_id
            WHERE a.task_id=? AND a.status='completed' ORDER BY a.rowid DESC LIMIT 1""",(task['id'],)).fetchone()
        if not receipt or fingerprint(receipt['response_text'])!=pending['response_hash']:
            raise RuntimeErrorBase('search_notice_receipt_conflict')
        if acknowledge:
            return notice.publish(db,dict(task),payload,receipt['response_text'],json.loads(receipt['diagnostic_json']),presented=notice_id)
        notice.validated_recall(db,turn,task['id'])
        return {'notice_id':notice_id,'turn_id':turn_id,'text':notice.display_body(db,turn,receipt['response_text'])}


def authorize_search(db,store,turn):
    if not required(db,turn):return
    _active(db,store,turn)
    row=db.execute("SELECT json_extract(value,'$.data.fully_presented') FROM knowledge_control WHERE key=?",
                   ('knowledge.search_notice.turn.'+turn['id'],)).fetchone()
    if not row or not row[0]:raise RuntimeErrorBase('search_notice_not_fully_presented')


def wait(service,turn):
    from .knowledge_wait import check
    verify_at=0
    while True:
        check(service,turn)
        with service.store.connection() as db:
            confirmed=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.search_notice.turn.'+turn['id'],)).fetchone()
            _active(db,service.store,turn)
            if confirmed:
                if json.loads(confirmed[0])['data'].get('fully_presented'):return
                raise RuntimeErrorBase('search_notice_not_fully_presented')
            pending=_pending(db,turn['id'])
            if not pending:raise RuntimeErrorBase('search_notice_not_fully_presented')
        if time.monotonic()>=verify_at:
            display(service.processing.search_notice,turn['persona_id'],turn['session_id'],turn['id'],pending['notice_id'])
            verify_at=time.monotonic()+2
        service.closed.wait(.1)
