"""Public acquisition coordination. Model dispatch belongs to ManagedOrganization."""

import asyncio
from contextlib import closing
import copy
import hashlib
import json
import re
import sqlite3
import threading
import time

from .archive import fingerprint
from .instance import current_process_identity, process_identity_alive
from .knowledge_fetch import FetchError, WikimediaFetcher, public_identifier
from .knowledge_output import VERSION, RELATIVE_TIME, messages, parse, retain_candidates
from .knowledge_service import read, POLICY, SOURCES, package_path
from .knowledge_store import SOURCE_POLICY, check_document, enabled, key, source_policy
from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier
from .private_personas import provider_identity


class KnowledgeMaintenance:
    def __init__(self, engine, fetcher=None):
        self.engine, self.store = engine, engine.store
        self.service = engine.chat.knowledge
        self.service.maintenance = self
        self.clock = time.time
        self.process = current_process_identity()
        self.stopped = threading.Event()
        self.fetcher = fetcher or WikimediaFetcher(self.stopped)

    def defaults(self):
        return {'enabled': False, 'provider': provider_identity(self.engine.model()),
                'sources': ['wikipedia_zh', 'wikidata'], 'daily_scan': True}

    def policy(self, persona_id):
        self.service.current_policy(persona_id)
        with self.store.connection() as db:
            return read(db, 'maintenance.' + persona_id, self.defaults())

    def update_policy(self, persona_id, data, expected_revision, operation_id):
        from .private_personas import guard_acceptance_write
        guard_acceptance_write(self.store, persona_id)
        self.policy(persona_id)
        if (not isinstance(data, dict) or set(data) != set(self.defaults())
                or type(data['enabled']) is not bool or type(data['daily_scan']) is not bool
                or data['provider'] != provider_identity(self.engine.model())
                or not isinstance(data['sources'], list) or not data['sources']
                or any(source not in SOURCES for source in data['sources'])
                or len(data['sources']) != len(set(data['sources']))):
            raise RuntimeErrorBase('knowledge_maintenance_policy_invalid')
        return self.service._update('maintenance.' + persona_id, data, expected_revision, operation_id)

    def current_source(self, source_id):
        if source_id not in SOURCES:
            raise RuntimeErrorBase('knowledge_source_unknown')
        with self.store.connection() as db:
            return read(db, 'source_policy.' + source_id, SOURCE_POLICY)

    def update_source(self, source_id, data, expected_revision, operation_id):
        self.current_source(source_id)
        if (not isinstance(data, dict) or set(data) != set(SOURCE_POLICY)
                or any(type(data[field]) is not bool for field in ('enabled', 'store_body'))
                or type(data['freshness_seconds']) is not int or not 60 <= data['freshness_seconds'] <= 30 * 86400
                or type(data['min_interval_seconds']) is not int or not 1 <= data['min_interval_seconds'] <= 3600):
            raise RuntimeErrorBase('knowledge_source_policy_invalid')
        return self.service._update('source_policy.' + source_id, data, expected_revision, operation_id)

    def grant(self, db, persona_id, source_id):
        data = read(db, 'maintenance.' + persona_id, self.defaults())['data']
        if (self.stopped.is_set() or self.engine.draining or self.engine.chat.draining
                or not data['enabled'] or source_id not in data['sources']
                or data['provider'] != provider_identity(self.engine.model())
                or not any(enabled(db, persona_id, channel, source_id) for channel in ('desktop','qq'))
                or not source_policy(db, source_id)['store_body']):
            raise RuntimeErrorBase('organization_policy_changed')
        return data

    def source_signature(self, db, source_id):
        return fingerprint([source_policy(db, source_id),
            read(db, 'sources', SOURCES)['data'][source_id],
            dict(db.execute('SELECT * FROM knowledge_sources WHERE id=?', (source_id,)).fetchone())])

    def _enqueue(self, db, persona_id, item, automatic, reason):
        source_id = item['source_id']
        public_id = public_identifier(source_id, item['public_id'])
        self.grant(db, persona_id, source_id)
        target_id = fingerprint([source_id, public_id])
        now = self.clock()
        target = db.execute('SELECT * FROM knowledge_targets WHERE id=?', (target_id,)).fetchone()
        bindings = json.loads(target['bindings_json']) if target else []
        binding = item.get('base_binding')
        if binding:
            registry = read(db, 'registry', {'packages': {}})['data']['packages']
            record = registry.get(binding.get('package_sha256'))
            if not record or type(binding.get('article_id')) is not int:
                raise RuntimeErrorBase('knowledge_base_binding_invalid')
            directory = package_path(self.store.paths, record)
            from .character_embedding import safe_path
            database = safe_path(directory, directory / record['manifest']['database']['file'])
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
                article = source.execute('SELECT title FROM articles WHERE id=?', (binding['article_id'],)).fetchone()
            if not article or article[0].replace('_', ' ') != public_id:
                raise RuntimeErrorBase('knowledge_base_binding_invalid')
            if binding not in bindings:
                bindings.append(binding)
                if target and target['page_id']:
                    self.invalidate_bindings(db, [binding], 'bound_to_observed_public_document')
        last_used = target['last_used'] if target and reason == 'daily' else now
        db.execute('INSERT INTO knowledge_targets VALUES(?,?,?,NULL,?,?,NULL,?) '
            'ON CONFLICT(id) DO UPDATE SET bindings_json=excluded.bindings_json,last_used=excluded.last_used',
            (target_id, source_id, public_id, json.dumps(bindings), last_used, 'registered'))
        old = db.execute("SELECT * FROM knowledge_fetch_jobs WHERE target_id=? AND state IN ('queued','fetching') "
                         'ORDER BY created_epoch LIMIT 1', (target_id,)).fetchone()
        if old:
            return old['id']
        if target and target['last_checked'] is not None:
            age = now - target['last_checked']
            if age < (1 if reason in ('current','manual') else source_policy(db, source_id)['freshness_seconds']):
                previous = db.execute("SELECT id FROM knowledge_fetch_jobs WHERE target_id=? AND state='completed' "
                                      'ORDER BY created_epoch DESC LIMIT 1', (target_id,)).fetchone()
                if previous:
                    return previous['id']
        job_id = identifier()
        db.execute('INSERT INTO knowledge_fetch_jobs(id,target_id,owner_persona,automatic,state,reason,ready_after,created_epoch) '
            "VALUES(?,?,?,?,'queued',?,?,?)", (job_id, target_id, persona_id, int(automatic), reason, now, now))
        return job_id

    def submit(self, persona_id, items, expected_revision, operation_id):
        if (not isinstance(items, list) or not 1 <= len(items) <= 5
                or any(not isinstance(item, dict) or set(item) - {'source_id', 'public_id', 'base_binding'}
                       or not {'source_id', 'public_id'} <= set(item) for item in items)):
            raise RuntimeErrorBase('knowledge_refresh_scope_invalid')
        data = {'persona_id': persona_id, 'job_ids': []}
        def apply(db):
            for item in items:
                data['job_ids'].append(self._enqueue(db, persona_id, item, False, 'manual'))
        return self.service._update('refresh', data, expected_revision, operation_id,
                                    request=[persona_id, items], apply=apply)

    @staticmethod
    def invalidate_bindings(db, bindings, reason):
        current = read(db, 'invalidations', {})
        changed = False
        for binding in bindings:
            records = current['data'].setdefault(binding['package_sha256'], {})
            if str(binding['article_id']) not in records:
                records[str(binding['article_id'])] = {'state': 'corrected', 'reason': reason}
                changed = True
        if changed:
            current['revision'] += 1
            db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('knowledge.invalidations', json.dumps(current)))

    def withdraw(self, persona_id, target_id, reason, expected_revision, operation_id):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise RuntimeErrorBase('knowledge_withdrawal_invalid')
        self.service.current_policy(persona_id)
        def apply(db):
            target = db.execute('SELECT * FROM knowledge_targets WHERE id=?', (target_id,)).fetchone()
            if not target:
                raise RuntimeErrorBase('knowledge_target_not_found')
            self._withdraw(db, target, reason)
        return self.service._update('withdrawals', {'target_id': target_id, 'reason': reason,'persona_id':persona_id},
            expected_revision, operation_id, apply=apply)

    def _withdraw(self, db, target, reason):
        bindings = json.loads(target['bindings_json'])
        if target['page_id']:
            db.execute("UPDATE knowledge_pages SET state='withdrawn' WHERE id=?", (target['page_id'],))
            for row in db.execute('SELECT bindings_json FROM knowledge_targets WHERE page_id=?', (target['page_id'],)):
                bindings.extend(json.loads(row['bindings_json']))
        db.execute("UPDATE knowledge_targets SET state='withdrawn' WHERE id=?", (target['id'],))
        self.invalidate_bindings(db, bindings, reason)

    def fetch_one(self, job_id=None):
        if self.stopped.is_set() or self.engine.draining or self.engine.chat.draining:
            return False
        now = self.clock()
        with self.store.connection(write=True) as db:
            for row in db.execute("SELECT * FROM knowledge_fetch_jobs WHERE state='fetching'").fetchall():
                lease = json.loads(row['lease_json'])
                if row['lease_until'] > now and process_identity_alive(lease['process']):
                    return False
                state = 'queued' if row['attempts'] < 3 else 'unavailable'
                db.execute('UPDATE knowledge_fetch_jobs SET state=?,lease_json=NULL,lease_until=0 WHERE id=?', (state,row['id']))
            sql = "SELECT j.*,t.source_id,t.public_id FROM knowledge_fetch_jobs j JOIN knowledge_targets t "
            sql += "ON t.id=j.target_id WHERE j.state='queued' AND j.ready_after<=?"
            args = [now]
            if job_id:
                sql += ' AND j.id=?'
                args.append(job_id)
            row = db.execute(sql + " ORDER BY CASE j.reason WHEN 'current' THEN 0 WHEN 'manual' THEN 1 ELSE 2 END,j.created_epoch LIMIT 1", args).fetchone()
            if not row:
                return False
            job = dict(row)
            try:
                self.grant(db, job['owner_persona'], job['source_id'])
            except RuntimeErrorBase:
                db.execute("UPDATE knowledge_fetch_jobs SET state='blocked_authorization' WHERE id=?", (job['id'],))
                return True
            rate = read(db, 'network.' + job['source_id'], {'next_epoch': 0})
            if rate['data']['next_epoch'] > now:
                return False
            rate['data']['next_epoch'] = now + source_policy(db, job['source_id'])['min_interval_seconds']
            db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('knowledge.network.' + job['source_id'], json.dumps(rate)))
            token = identifier()
            db.execute("UPDATE knowledge_fetch_jobs SET state='fetching',attempts=attempts+1,lease_json=?,lease_until=? WHERE id=?",
                (json.dumps({'token': token, 'process': self.process}), now + 30, job['id']))
            source = dict(db.execute('SELECT * FROM knowledge_sources WHERE id=?', (job['source_id'],)).fetchone())
            source['interactive'] = not bool(job['automatic']) or job['reason'] == 'current'
        try:
            document = self.fetcher.fetch(source, job['public_id'])
            if self.stopped.is_set():
                raise FetchError('knowledge_stopped')
            with self.store.connection(write=True) as db:
                current = db.execute('SELECT * FROM knowledge_fetch_jobs WHERE id=?', (job['id'],)).fetchone()
                if (not current or current['state'] != 'fetching' or not current['lease_json']
                        or json.loads(current['lease_json'])['token'] != token):
                    return False
                self.grant(db, job['owner_persona'], job['source_id'])
                self.ingest(db, job, document)
        except (FetchError, OSError, ValueError) as error:
            with self.store.connection(write=True) as db:
                current = db.execute('SELECT * FROM knowledge_fetch_jobs WHERE id=?', (job['id'],)).fetchone()
                if (not current or current['state'] != 'fetching' or not current['lease_json']
                        or json.loads(current['lease_json'])['token'] != token):
                    return False
                target = db.execute('SELECT * FROM knowledge_targets WHERE id=?', (job['target_id'],)).fetchone()
                withdrawn = isinstance(error, FetchError) and error.withdrawn
                if withdrawn:
                    self._withdraw(db, target, str(error))
                retry = not withdrawn and job['attempts'] + 1 < 3 and not self.stopped.is_set()
                delay = max(getattr(error, 'retry_after', 60), (60, 300, 1800)[min(job['attempts'], 2)])
                if str(error) in ('knowledge_http_429','knowledge_http_503','knowledge_upstream_maxlag'):
                    rate = read(db, 'network.' + job['source_id'], {'next_epoch': 0})
                    rate['data']['next_epoch'] = max(rate['data']['next_epoch'], self.clock() + delay)
                    db.execute('UPDATE knowledge_control SET value=? WHERE key=?',
                               (json.dumps(rate), 'knowledge.network.' + job['source_id']))
                state = 'queued' if retry else 'withdrawn' if withdrawn else 'unavailable'
                db.execute('UPDATE knowledge_fetch_jobs SET state=?,ready_after=?,error=?,lease_json=NULL,lease_until=0 WHERE id=?',
                           (state, self.clock() + delay, type(error).__name__ + ':' + str(error)[:100], job['id']))
        return True

    def ingest(self, db, job, material):
        if type(material['revision']) is not int or material['revision'] <= 0:
            raise FetchError('knowledge_revision_invalid')
        page_id = fingerprint([job['source_id'], material['upstream_id']])
        old = db.execute('SELECT * FROM knowledge_pages WHERE id=?', (page_id,)).fetchone()
        if old and material['revision'] < old['latest_revision']:
            raise FetchError('knowledge_older_upstream_revision')
        target = db.execute('SELECT * FROM knowledge_targets WHERE id=?', (job['target_id'],)).fetchone()
        if target['state'] == 'withdrawn' or old and old['state'] == 'withdrawn':
            raise FetchError('knowledge_target_withdrawn', withdrawn=True)
        now = self.clock()
        body_hash = hashlib.sha256(material['body'].encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO knowledge_pages VALUES(?,?,?,?,?,NULL,?,?,?)',
            (page_id, job['source_id'], material['upstream_id'], material['title'], material['url'],
             material['revision'], 'active', now))
        existing = db.execute('SELECT * FROM knowledge_documents WHERE page_id=? AND upstream_revision=?',
                              (page_id, material['revision'])).fetchone()
        if existing and existing['sha256'] != body_hash:
            raise FetchError('knowledge_revision_content_mismatch')
        document_id = existing['id'] if existing else identifier()
        if not existing:
            db.execute('INSERT INTO knowledge_documents VALUES(?,?,?,?,?,?,?,?,?)',
                (document_id, page_id, material['revision'], body_hash, material['body'],
                 json.dumps(material['raw'], ensure_ascii=False), json.dumps(material['metadata'], ensure_ascii=False),
                 material['upstream_at'], now))
        db.execute('UPDATE knowledge_pages SET title=?,url=?,head_document_id=?,latest_revision=?,last_checked=? WHERE id=?',
            (material['title'], material['url'], document_id, material['revision'], now, page_id))
        db.execute("UPDATE knowledge_targets SET page_id=?,last_checked=?,state='active' WHERE id=?",
                   (page_id, now, job['target_id']))
        self.invalidate_bindings(db, json.loads(target['bindings_json']), 'upstream_version_observed')
        excerpt_length = min(8000, self.engine.model()['max_input_chars'] // 3)
        work_identity = [page_id, body_hash, VERSION, provider_identity(self.engine.model())]
        if re.search(RELATIVE_TIME, material['body'][:excerpt_length]):
            work_identity.append({'temporal_anchor': material['upstream_at']})
        work_key = fingerprint(work_identity)
        batch = db.execute('SELECT id FROM knowledge_batches WHERE work_key=?', (work_key,)).fetchone()
        if not batch:
            data = {'automatic': bool(job['automatic']), 'proposals': None,
                    'coverage': {'start': 0, 'end': min(len(material['body']), excerpt_length),
                                 'document_characters': len(material['body'])},
                    'source_signature': self.source_signature(db, job['source_id']),
                    'grant_signature': fingerprint(self.grant(db, job['owner_persona'], job['source_id']))}
            db.execute("INSERT INTO knowledge_batches VALUES(?,?,?,?,'waiting_extract',NULL,NULL,?,?)",
                (identifier(), document_id, job['owner_persona'], work_key, json.dumps(data), now))
        db.execute("UPDATE knowledge_fetch_jobs SET state='completed',document_id=?,lease_json=NULL,lease_until=0,error=NULL WHERE id=?",
                   (document_id, job['id']))

    def payload(self, db, batch, stage):
        stored = json.loads(batch['data_json'])
        document = dict(db.execute('SELECT d.*,p.source_id,p.title FROM knowledge_documents d '
            'JOIN knowledge_pages p ON p.id=d.page_id WHERE d.id=?', (batch['document_id'],)).fetchone())
        limit = self.engine.model()['max_input_chars']
        existing = [dict(row) for row in db.execute('SELECT id,subject,predicate,value,kind,valid_from,valid_until '
            'FROM knowledge_claims WHERE page_id=? ORDER BY rowid DESC LIMIT 12', (document['page_id'],))]
        metadata = json.loads(document['metadata_json'])
        provenance = metadata.get('pcar_provenance') or {'kind': 'public_wikimedia',
            'name': '中文Wikipedia' if document['source_id'] == 'wikipedia_zh' else 'Wikidata'}
        data = {'stage': stage, 'batch_id': batch['id'], 'document_id': document['id'],
            'document_sha256': document['sha256'], 'source_id': document['source_id'],
            'source_signature': stored['source_signature'], 'grant_signature': stored['grant_signature'],
            'pipeline': VERSION, 'document': {'title': document['title'],
                'upstream_revision': document['upstream_revision'], 'upstream_at': document['upstream_at'],
                'excerpt': document['body'][:min(8000, limit // 3)], 'source_provenance': provenance}, 'existing_claims': existing}
        if stage == 'extract':
            data['retention_candidates'] = []
            rows = db.execute('SELECT c.*,e.quote,d.upstream_at FROM knowledge_claims c '
                'JOIN knowledge_evidence e ON e.claim_id=c.id JOIN knowledge_documents d ON d.id=c.document_id '
                "WHERE c.state='active' AND c.rowid IN (SELECT max(rowid) FROM knowledge_claims WHERE page_id=? "
                'GROUP BY claim_key,valid_from,valid_until) ORDER BY c.rowid DESC', (document['page_id'],))
            for row in rows:
                item = dict(row)
                start = document['body'].find(item['quote'])
                if start < 0 or re.search(RELATIVE_TIME, item['quote']) and item['upstream_at'] != document['upstream_at']:
                    continue
                left, right = max(0, start - 180), min(len(document['body']), start + len(item['quote']) + 180)
                item['context'] = {'start': left, 'end': right, 'text': document['body'][left:right]}
                data['retention_candidates'].append(item)
        if stage == 'review':
            data['proposals'] = stored['proposals']
            data['document']['supporting_fragments'] = stored.get('retained_context', [])
        payload = {'persona_id': batch['owner_persona'], 'user_id': LOCAL_USER_ID, 'session_id': None,
            'channel': 'desktop', 'mode': 'knowledge', 'scope': {'persona_id': batch['owner_persona'],
                'user_id': LOCAL_USER_ID, 'channel': 'desktop', 'bot': '', 'peer': ''}, 'external_knowledge': data}
        payload['fingerprint'] = fingerprint(payload)
        if sum(len(message['content']) for message in messages(payload)) > limit:
            raise RuntimeErrorBase('knowledge_input_budget')
        return payload

    def check(self, db, task, contract, payload):
        data = payload['external_knowledge']
        batch = db.execute('SELECT * FROM knowledge_batches WHERE id=?', (data['batch_id'],)).fetchone()
        column = 'extract_task_id' if data['stage'] == 'extract' else 'review_task_id'
        if (not batch or batch['owner_persona'] != task['persona_id'] or batch[column] != task['id']
                or batch['document_id'] != data['document_id']):
            raise RuntimeErrorBase('organization_scope_mismatch')
        if data['stage'] == 'review' and json.loads(batch['data_json'])['proposals'] != data['proposals']:
            raise RuntimeErrorBase('source_expired')
        grant = self.grant(db, task['persona_id'], data['source_id'])
        if (fingerprint(grant) != data['grant_signature']
                or self.source_signature(db, data['source_id']) != data['source_signature']):
            raise RuntimeErrorBase('organization_policy_changed')
        document = check_document(db, payload)
        for fragment in data['document'].get('supporting_fragments', []):
            if document['body'][fragment['start']:fragment['end']] != fragment['text']:
                raise RuntimeErrorBase('source_expired')
        if contract['scope'] != payload['scope'] or data['pipeline'] != VERSION:
            raise RuntimeErrorBase('organization_scope_mismatch')

    def published(self, db, task, payload, response, audit):
        output = parse(payload, response, isolate_items=True)
        data = payload['external_knowledge']
        batch = db.execute('SELECT * FROM knowledge_batches WHERE id=?', (data['batch_id'],)).fetchone()
        stored = json.loads(batch['data_json'])
        if data['stage'] == 'extract':
            output, fragments = retain_candidates(payload, output)
            stored['retained_context'] = fragments
            stored['proposals'] = output
            state = 'waiting_review' if output['claims'] else 'committed'
            if not output['claims']:
                stored['review_disposition'] = 'no_claims_no_action'
            db.execute('UPDATE knowledge_batches SET state=?,data_json=? WHERE id=?',
                       (state, json.dumps(stored), batch['id']))
            return
        document = check_document(db, payload)
        for decision in output['decisions']:
            proposal = data['proposals']['claims'][decision['index']]
            state = ('active' if all(decision[field] for field in ('supported', 'temporal_ok', 'version_ok'))
                     and decision['kind'] in ('fact', 'source_claim') and proposal['kind'] in ('fact', 'source_claim') else 'quarantined')
            claim_key = key(proposal['subject'], proposal['predicate'])
            prior = db.execute('SELECT id FROM knowledge_claims WHERE page_id=? AND claim_key=? '
                               'ORDER BY rowid DESC LIMIT 1', (document['page_id'], claim_key)).fetchone()
            claim_id = fingerprint([batch['id'], decision['index']])
            kind = 'source_claim' if proposal['kind'] == 'source_claim' and decision['kind'] == 'fact' else decision['kind']
            db.execute('INSERT OR IGNORE INTO knowledge_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (claim_id, document['page_id'], document['id'], batch['id'], claim_key,
                 proposal['subject'], proposal['predicate'], proposal['value'], kind, state,
                 proposal['valid_from'], proposal['valid_until'], prior['id'] if prior else None, self.clock()))
            start = document['body'].find(proposal['quote'])
            if start < 0:
                raise RuntimeErrorBase('source_expired')
            db.execute('INSERT OR IGNORE INTO knowledge_evidence VALUES(?,?,?,?,?)',
                (claim_id, document['id'], start, start + len(proposal['quote']), proposal['quote']))
        stored['review'] = output
        db.execute("UPDATE knowledge_batches SET state='committed',data_json=? WHERE id=?",
                   (json.dumps(stored), batch['id']))

    def coordinate(self, db):
        now = self.clock()
        owners = list(db.execute("SELECT key,value FROM knowledge_control WHERE key LIKE 'knowledge.maintenance.%'"))
        for owner in owners:
            pid = owner['key'].removeprefix('knowledge.maintenance.')
            policy = json.loads(owner['value'])['data']
            if not policy['enabled'] or not policy['daily_scan']:
                continue
            scan = read(db, 'scan.' + pid, {'last_epoch': 0})
            if now - scan['data']['last_epoch'] < 86400:
                continue
            for row in db.execute("SELECT * FROM knowledge_targets WHERE state!='withdrawn' ORDER BY last_used DESC LIMIT 100").fetchall():
                try:
                    self._enqueue(db, pid, {'source_id': row['source_id'], 'public_id': row['public_id']}, True, 'daily')
                except RuntimeErrorBase:
                    continue
            scan['data']['last_epoch'] = now
            db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('knowledge.scan.' + pid, json.dumps(scan)))

    def advance(self):
        with self.store.connection(write=True) as db:
            for row in db.execute("SELECT b.id,t.status,t.error_code FROM knowledge_batches b JOIN organization_tasks t "
                    "ON t.id=CASE b.state WHEN 'extracting' THEN b.extract_task_id ELSE b.review_task_id END "
                    "WHERE b.state IN ('extracting','reviewing') AND t.status NOT IN ('queued','waiting_quota','running','completed')").fetchall():
                db.execute('UPDATE knowledge_batches SET state=? WHERE id=?', (row['status'], row['id']))
            self.coordinate(db)
            for row in db.execute("SELECT * FROM knowledge_batches WHERE state IN ('waiting_extract','waiting_review') ORDER BY created_epoch LIMIT 5").fetchall():
                batch = dict(row)
                stage = 'extract' if batch['state'] == 'waiting_extract' else 'review'
                try:
                    payload = self.payload(db, batch, stage)
                    task = self.engine.managed.register(db, payload,
                        'knowledge-' + stage + '-' + batch['work_key'],
                        automatic=int(json.loads(batch['data_json'])['automatic']), trigger='incremental')
                except RuntimeErrorBase as error:
                    if str(error) == 'organization_queue_full':
                        break
                    db.execute("UPDATE knowledge_batches SET state='quarantined' WHERE id=?", (batch['id'],))
                    continue
                column = 'extract_task_id' if stage == 'extract' else 'review_task_id'
                db.execute('UPDATE knowledge_batches SET state=?,' + column + '=? WHERE id=?',
                           ('extracting' if stage == 'extract' else 'reviewing', task['id'], batch['id']))

    async def tick(self):
        if self.stopped.is_set() or self.engine.draining or self.engine.chat.draining:
            return
        await asyncio.to_thread(self.advance)
        await asyncio.to_thread(self.fetch_one)

    def on_result(self, turn, result, latest):
        jobs = []
        with self.store.connection(write=True) as db:
            if not read(db, 'maintenance.' + turn['persona_id'], self.defaults())['data']['enabled']:
                return {'state': 'disabled', 'job_ids': []}
            for item in result.get('evidence', [])[:6]:
                if item['kind'] != 'encyclopedia':
                    target = db.execute('SELECT * FROM knowledge_targets WHERE page_id=? LIMIT 1', (item['article_id'],)).fetchone()
                    if not target:
                        continue
                    request = {'source_id': target['source_id'], 'public_id': target['public_id']}
                else:
                    request = {'source_id': 'wikipedia_zh', 'public_id': item['article_title'],
                        'base_binding': {key: item[key] for key in ('package_sha256', 'article_id')}}
                try:
                    job = self._enqueue(db, turn['persona_id'], request, True, 'current' if latest else 'on_demand')
                except RuntimeErrorBase:
                    continue
                if job not in jobs:
                    jobs.append(job)
                if len(jobs) == 3:
                    break
        if latest and jobs:
            self.fetch_one(jobs[0])
        with self.store.connection() as db:
            checked = bool(jobs) and db.execute("SELECT state FROM knowledge_fetch_jobs WHERE id=?", (jobs[0],)).fetchone()[0] == 'completed'
        return {'state': 'checked' if checked else 'pending', 'job_ids': jobs}

    def status(self):
        with self.store.connection() as db:
            return {'fetch': {row['state']: row['n'] for row in db.execute('SELECT state,count(*) n FROM knowledge_fetch_jobs GROUP BY state')},
                'batches': {row['state']: row['n'] for row in db.execute('SELECT state,count(*) n FROM knowledge_batches GROUP BY state')},
                'claims': {row['state']: row['n'] for row in db.execute('SELECT state,count(*) n FROM knowledge_claims GROUP BY state')},
                'refresh': read(db, 'refresh', {}), 'withdrawals': read(db, 'withdrawals', {})}

    def close(self):
        self.stopped.set()
