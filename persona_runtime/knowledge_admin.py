"""Bounded knowledge management views and versioned package rollback."""

import json
import importlib.util
import time

from .knowledge_service import POLICY, REGISTRY, read
from .knowledge_store import SOURCE_POLICY, source_policy
from .models import RuntimeErrorBase


def dependencies():
    missing = [name for name in ('numpy', 'jieba', 'opencc') if importlib.util.find_spec(name) is None]
    return {'ready': not missing, 'missing': missing, 'installation_extra': 'knowledge-ann'}


def overview(service, persona_id):
    service.current_policy(persona_id)
    result = service.status()
    result['dependencies'] = dependencies()
    with service.store.connection() as db:
        result['policy'] = read(db, 'policy.' + persona_id, POLICY)
        result['maintenance_policy'] = read(db, 'maintenance.' + persona_id, service.maintenance.defaults())
        result['maintenance_provider'] = service.maintenance.defaults()['provider']
        config = service.maintenance.engine.config.load()['organization']
        result['automatic_limits'] = {'hour': config['hourly_limit'], 'rolling_day': config['daily_limit']}
        result['source_policies'] = {row['id']: read(db, 'source_policy.' + row['id'], SOURCE_POLICY)
                                     for row in db.execute('SELECT id FROM knowledge_sources')}
        result['pages'] = [dict(row) for row in db.execute('SELECT p.id,p.source_id,p.title,p.state,p.latest_revision,'
            'p.last_checked,d.upstream_at,d.sha256 FROM knowledge_pages p '
            'LEFT JOIN knowledge_documents d ON d.id=p.head_document_id '
            'WHERE EXISTS(SELECT 1 FROM knowledge_fetch_jobs j JOIN knowledge_targets t ON t.id=j.target_id '
            'WHERE t.page_id=p.id AND j.owner_persona=?) OR EXISTS(SELECT 1 FROM knowledge_batches b '
            'JOIN knowledge_documents x ON x.id=b.document_id WHERE x.page_id=p.id AND b.owner_persona=?) '
            'ORDER BY p.last_checked DESC LIMIT 100', (persona_id, persona_id))]
        for page in result['pages']:
            page['freshness'] = 'fresh' if time.time() - page['last_checked'] <= source_policy(db, page['source_id'])['freshness_seconds'] else 'snapshot'
        result['targets'] = [dict(row) for row in db.execute('SELECT DISTINCT t.id,t.source_id,t.public_id,t.state '
            'FROM knowledge_targets t JOIN knowledge_fetch_jobs j ON j.target_id=t.id '
            'WHERE j.owner_persona=? ORDER BY t.last_used DESC LIMIT 100', (persona_id,))]
        result['fetch_jobs'] = [dict(row) for row in db.execute('SELECT id,target_id,state,attempts,ready_after,error '
            'FROM knowledge_fetch_jobs WHERE owner_persona=? ORDER BY created_epoch DESC LIMIT 100', (persona_id,))]
        result['batches'] = []
        for row in db.execute('SELECT b.*,e.error_code AS extract_error,r.error_code AS review_error '
                'FROM knowledge_batches b LEFT JOIN organization_tasks e ON e.id=b.extract_task_id '
                'LEFT JOIN organization_tasks r ON r.id=b.review_task_id WHERE b.owner_persona=? '
                'ORDER BY b.created_epoch DESC LIMIT 100', (persona_id,)):
            item = dict(row)
            data = json.loads(item.pop('data_json'))
            item['coverage'] = data.get('coverage')
            item['isolated'] = (data.get('proposals') or {}).get('quarantined', [])
            result['batches'].append(item)
        result['cost'] = {'calls': 0, 'tokens_known': 0, 'usage_unknown': 0, 'amount': None}
        for row in db.execute('SELECT a.usage_json FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id '
                "WHERE t.persona_id=? AND json_type(t.payload_json,'$.external_knowledge')='object'", (persona_id,)):
            result['cost']['calls'] += 1
            usage = json.loads(row['usage_json']) if row['usage_json'] else None
            if usage is None or type(usage.get('total_tokens')) is not int:
                result['cost']['usage_unknown'] += 1
            else:
                result['cost']['tokens_known'] += usage['total_tokens']
        result['restore'] = read(db, 'restore', {})
    return result


def rollback(service, expected_revision, operation_id):
    saved = service._receipt('registry', expected_revision, operation_id, ['rollback'])
    if saved is not None:
        return saved
    with service.store.connection() as db:
        registry = read(db, 'registry', REGISTRY)['data']
    previous = registry['previous']
    if not previous or previous not in registry['packages']:
        raise RuntimeErrorBase('knowledge_previous_package_missing')
    return service._activate_package(registry['packages'][previous]['directory'], expected_revision,
                                     operation_id, ['rollback'])
