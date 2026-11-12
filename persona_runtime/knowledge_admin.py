"""Bounded knowledge management views and versioned package rollback."""

import json
import importlib.util
import time

from .knowledge_service import POLICY, REGISTRY, read
from .knowledge_store import SOURCE_POLICY, source_policy
from .models import RuntimeErrorBase
from .private_personas import provider_identity


def dependencies():
    missing = [name for name in ('numpy', 'jieba', 'opencc') if importlib.util.find_spec(name) is None]
    return {'ready': not missing, 'missing': missing, 'installation_extra': 'knowledge-ann'}


def overview(service, persona_id):
    service.current_policy(persona_id)
    result = service.status()
    result['dependencies'] = dependencies()
    if service.processing:
        result['processing_policy']=service.processing.policy(persona_id)
        result['routing_policy']=service.processing.engine.routing.policy(persona_id)
        result['routing_configuration']=service.processing.engine.routing.configuration()
        result['dependencies']['required_for_selected_route']=result['routing_policy']['data']['variant']=='local_web'
    with service.store.connection() as db:
        result['policy'] = read(db, 'policy.' + persona_id, POLICY)
        result['maintenance_policy'] = read(db, 'maintenance.' + persona_id, service.maintenance.defaults())
        result['maintenance_provider'] = provider_identity(service.maintenance.model(result['maintenance_policy']['data']))
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
        result['processing_tasks']=[dict(r) for r in db.execute("SELECT id,status,error_code,json_extract(payload_json,'$.knowledge_processing.stage') AS stage FROM organization_tasks WHERE persona_id=? AND json_type(payload_json,'$.knowledge_processing')='object' ORDER BY rowid DESC LIMIT 100",(persona_id,))]
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
        from .knowledge_web import DEFAULT as WEB_DEFAULT
        result['web_policy']=read(db,'web.policy.'+persona_id,WEB_DEFAULT)
        from .knowledge_web import snapshot as web_snapshot
        web_data=web_snapshot(db,persona_id)
        result['web_invalidations']=read(db,'web.invalidations',{})|{'data':web_data['invalidations']}
        result['web_documents']=[{k:v for k,v in row.items() if k!='body'}|{'validity':web_data['invalidations'].get(row['id'],{}).get('state','active')}
            for row in sorted(web_data['documents'],key=lambda row:row['fetched_epoch'],reverse=True)]
        result['tool_cost']={'calls':0,'billable_calls':0,'known_amount_usd':'0','unknown_effect':0,'amount_is_estimate':True,'actual_billed_amount_usd':None}
        from decimal import Decimal
        tool_amount=Decimal('0')
        result['cost'] = {'calls': 0, 'tokens_known': 0, 'usage_unknown': 0, 'amount': None}
        result['routing_cost']={'calls':0,'tokens_known':0,'usage_unknown':0,'amount':None,'included_in_model_cost':True}
        result['source_requests']={'tasks':0,'completed_http_requests':0,'model_provider_tool_amount_usd':'0'}
        for row in db.execute("SELECT a.usage_json,json_extract(t.payload_json,'$.knowledge_processing.stage') AS tool_stage FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id "
                "WHERE t.persona_id=? AND (json_type(t.payload_json,'$.external_knowledge')='object' OR json_type(t.payload_json,'$.knowledge_processing')='object')", (persona_id,)):
            usage = json.loads(row['usage_json']) if row['usage_json'] else None
            if row['tool_stage'] in ('wiki_search','wiki_fetch'):
                result['source_requests']['tasks']+=1
                result['source_requests']['completed_http_requests']+=(usage or {}).get('source_network_requests',0)
                continue
            if row['tool_stage'] in ('search','fetch') or usage and usage.get('tool'):
                result['tool_cost']['calls']+=1
                usage=usage or {}
                result['tool_cost']['billable_calls']+=int(usage.get('billable') is True)
                if usage.get('amount_usd') is not None:tool_amount+=Decimal(usage['amount_usd'])
                else:result['tool_cost']['unknown_effect']+=1
                continue
            if row['tool_stage'] in ('route','route_retry'):
                result['routing_cost']['calls']+=1
                if usage is None or type(usage.get('total_tokens')) is not int:result['routing_cost']['usage_unknown']+=1
                else:result['routing_cost']['tokens_known']+=usage['total_tokens']
            result['cost']['calls'] += 1
            if usage is None or type(usage.get('total_tokens')) is not int:
                result['cost']['usage_unknown'] += 1
            else:
                result['cost']['tokens_known'] += usage['total_tokens']
        result['tool_cost']['known_amount_usd']=str(tool_amount)
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
