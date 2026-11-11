"""Wait for useful knowledge without imposing a business completion deadline.

Only progress and dependency identities are persisted, without query bodies. Cached futures remain shared;
cancelling one consumer never cancels work used by another request.
"""

import json

from .models import RuntimeErrorBase
from .progress import record


def is_waiting(store, turn):
    if turn['status'] != 'queued':
        return False
    with store.connection() as db:
        row = db.execute('SELECT event FROM request_activity WHERE turn_id=? ORDER BY seq DESC LIMIT 1',
                         (turn['id'],)).fetchone()
    return bool(row and row[0] in ('knowledge_search', 'knowledge_refresh', 'knowledge_waiting_quota','knowledge_goal','knowledge_selection','knowledge_translation','knowledge_review'))


def check(service, turn):
    if service.closed.is_set():
        raise RuntimeErrorBase('knowledge_stopped')
    with service.store.connection() as db:
        row = db.execute('SELECT status,cancelled_at,execution_revision FROM turns WHERE id=?',
                         (turn['id'],)).fetchone()
    if (not row or row['cancelled_at'] or row['status'] in ('interrupted', 'failed')
            or row['execution_revision'] != turn['execution_revision']):
        raise RuntimeErrorBase('knowledge_request_inactive')


def mark(service, turn, event):
    with service.store.connection(write=True) as db:
        row = db.execute('SELECT status,cancelled_at,execution_revision FROM turns WHERE id=?',
                         (turn['id'],)).fetchone()
        if (row and row['status'] == 'queued' and not row['cancelled_at']
                and row['execution_revision'] == turn['execution_revision']):
            record(db, turn['id'], event)


def search_turn(service, turn, query):
    mark(service, turn, 'knowledge_search')
    package = None
    try:
        while True:
            check(service, turn)
            # This interval only releases the waiter to check cancellation and
            # current permissions; it never discards or restarts the future.
            result = service.search(turn['persona_id'], query, turn['channel'], timeout=.2, package_sha256=package)
            package = package or result.get('package_sha256')
            check(service, turn)
            if result['state'] not in ('pending', 'busy'):
                break
            service.closed.wait(.05)
        if result['state'] in ('failed', 'stopped'):
            raise RuntimeErrorBase('knowledge_retrieval_' + result['state'])
        mark(service, turn, 'knowledge_ready' if result.get('evidence') else
             'knowledge_no_matches' if result['state'] == 'ready' else 'knowledge_unavailable')
        return result
    except Exception as error:
        mark(service, turn, 'knowledge_failed')
        if isinstance(error, RuntimeErrorBase):
            raise
        raise RuntimeErrorBase('knowledge_retrieval_failed') from error


def refresh_turn(service, turn, refresh):
    """Observe managed fetch/extract/review; their existing worker owns work."""
    check(service, turn)
    with service.store.connection(write=True) as db:
        db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('knowledge.wait.' + turn['id'], json.dumps({'revision': 0, 'data': {
                'execution_revision': turn['execution_revision'], 'job_ids': refresh['job_ids']}})))
    mark(service, turn, 'knowledge_refresh')
    phase = 'knowledge_refresh'
    try:
        while True:
            check(service, turn)
            with service.store.connection() as db:
                from .knowledge_store import enabled
                from .knowledge_service import read
                if not read(db,'maintenance.'+turn['persona_id'],service.maintenance.defaults())['data']['enabled']:
                    raise RuntimeErrorBase('knowledge_refresh_disabled')
                waiting = False
                quota = False
                for job_id in refresh['job_ids']:
                    job = db.execute('SELECT j.*,t.source_id,t.state AS target_state FROM knowledge_fetch_jobs j '
                        'JOIN knowledge_targets t ON t.id=j.target_id WHERE j.id=?', (job_id,)).fetchone()
                    if (not job or job['target_state'] == 'withdrawn'
                            or not enabled(db, turn['persona_id'], turn['channel'], job['source_id'])
                            or job['state'] not in ('queued', 'fetching', 'completed')):
                        raise RuntimeErrorBase('knowledge_refresh_unavailable')
                    if job['state'] != 'completed':
                        waiting = True
                        continue
                    batches = db.execute('SELECT state,extract_task_id,review_task_id FROM knowledge_batches WHERE document_id=? AND owner_persona=?',
                                         (job['document_id'], turn['persona_id'])).fetchall()
                    if any(row['state'] not in ('waiting_extract', 'extracting', 'waiting_review', 'reviewing', 'committed') for row in batches):
                        raise RuntimeErrorBase('knowledge_refresh_rejected')
                    waiting |= any(row['state'] != 'committed' for row in batches)
                    for batch in batches:
                        task_id = batch['review_task_id'] or batch['extract_task_id']
                        task = db.execute('SELECT status FROM organization_tasks WHERE id=?', (task_id,)).fetchone()
                        quota |= bool(task and task['status'] == 'waiting_quota')
            if not waiting:
                mark(service, turn, 'knowledge_ready')
                return dict(refresh, state='checked')
            current_phase = 'knowledge_waiting_quota' if quota else 'knowledge_refresh'
            if current_phase != phase:
                mark(service, turn, current_phase)
                phase = current_phase
            service.closed.wait(.2)
    except Exception as error:
        mark(service, turn, 'knowledge_failed')
        if isinstance(error, RuntimeErrorBase):
            raise
        raise RuntimeErrorBase('knowledge_retrieval_failed') from error


def required_tasks(db):
    """Only dependencies bound to a current, waiting turn bypass chat priority."""
    tasks = set()
    for turn in db.execute("SELECT id,execution_revision FROM turns WHERE status='queued' AND cancelled_at IS NULL"):
        event = db.execute('SELECT event FROM request_activity WHERE turn_id=? ORDER BY seq DESC LIMIT 1', (turn['id'],)).fetchone()
        if not event or event[0] not in ('knowledge_refresh', 'knowledge_waiting_quota'):
            continue
        saved = db.execute('SELECT value FROM knowledge_control WHERE key=?', ('knowledge.wait.' + turn['id'],)).fetchone()
        binding = json.loads(saved[0])['data'] if saved else {}
        if binding.get('execution_revision') != turn['execution_revision']:
            continue
        for job_id in binding.get('job_ids', []):
            for row in db.execute('SELECT b.extract_task_id,b.review_task_id FROM knowledge_batches b '
                    'JOIN knowledge_fetch_jobs j ON j.document_id=b.document_id '
                    'WHERE j.id=? AND b.owner_persona=j.owner_persona', (job_id,)):
                tasks.update(task for task in row if task)
    for turn in db.execute("SELECT id,persona_id,execution_revision FROM turns WHERE status='queued' AND cancelled_at IS NULL"):
        event=db.execute('SELECT event FROM request_activity WHERE turn_id=? ORDER BY seq DESC LIMIT 1',(turn['id'],)).fetchone()
        if not event or event[0] not in ('knowledge_goal','knowledge_selection','knowledge_translation','knowledge_review','knowledge_waiting_quota'):continue
        saved=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.processing.wait.'+turn['id'],)).fetchone()
        value=json.loads(saved[0])['data'] if saved else {}
        if value.get('execution_revision')!=turn['execution_revision']:continue
        for task_id in value.get('task_ids',[]):
            task=db.execute('SELECT persona_id,payload_json FROM organization_tasks WHERE id=?',(task_id,)).fetchone()
            if task and task['persona_id']==turn['persona_id'] and json.loads(task['payload_json']).get('knowledge_processing',{}).get('root_turn_id')==turn['id']:tasks.add(task_id)
    return tasks
