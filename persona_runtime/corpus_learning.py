"""Approved private-corpus learning: durable single attempts and pending proposals."""
from .call_limits import resume_on_limit, external_gate, workflow
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .corpus_files import CorpusError,digest
from .corpus_store import encoded,fingerprint,now,outside_repository,field
from .corpus_dataset import execute_statements
from .corpus_learning_bundle import load_approved_bundle
from .corpus_learning_transport import CorpusLearningHTTPClient
from .expression_learning import filter_candidates
from .expression_assets import ExpressionAssetError
from .provider_compat import build_request,usage_fields
from .provider_policy import require_generation_provider
from .schema_corpus_learning import APPLICATION_ID,SCHEMA_VERSION,SCHEMA,TABLES


class CorpusLearning:
    def __init__(self,root,corpus,client=None,*,paths=None):
        self.root=outside_repository(root)
        self.path=self.root/'learning.sqlite3'
        self.corpus=corpus
        self.paths=paths
        self.client=client or CorpusLearningHTTPClient()

    def initialize(self):
        self.root.mkdir(parents=True,exist_ok=True)
        if self.path.exists():return self.inspect()
        if any(self.root.iterdir()):raise CorpusError('corpus_learning_empty_target_required')
        with self.path.open('xb'):pass
        db=sqlite3.connect(self.path);complete=False
        try:
            db.execute('BEGIN IMMEDIATE');execute_statements(db,SCHEMA)
            db.execute('INSERT INTO learning_meta VALUES(1,?,?,?)',(str(uuid4()),digest(SCHEMA.encode()),now()))
            db.execute(f'PRAGMA application_id={APPLICATION_ID}');db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            db.commit();complete=True
        except BaseException:db.rollback();raise
        finally:
            db.close()
            if not complete:self.path.unlink(missing_ok=True)
        return self.inspect()

    @contextmanager
    def connection(self,write=False):
        if not self.path.is_file() or not self.path.resolve().is_relative_to(self.root):
            raise CorpusError('corpus_learning_not_initialized')
        db=sqlite3.connect(self.path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=10)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            if write:db.execute('BEGIN IMMEDIATE')
            if (db.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID
                    or db.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA_VERSION):
                raise CorpusError('corpus_learning_database_incompatible')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(TABLES)<=tables or db.execute('SELECT schema_hash FROM learning_meta').fetchone()[0]!=digest(SCHEMA.encode()):
                raise CorpusError('corpus_learning_schema_incompatible')
            yield db
            if write:db.commit()
        except BaseException:
            if write:db.rollback()
            raise
        finally:db.close()

    @staticmethod
    def _binding(bundle):
        return {'approval_path':bundle['approval_path'],'preview_root':bundle['preview_root'],
                'corpus_id':bundle['corpus_id'],'view_id':bundle['view']['id'],
                'view_payload_hash':bundle['view']['payload_hash'],
                'positions':[{k:p[k] for k in ('position','position_key','request_hash','source_proof_hash','batch_file_hash','wire_file_hash')}
                             for p in bundle['positions']]}

    def register_approval(self,approval_path,preview_root):
        bundle=load_approved_bundle(self.corpus,approval_path,preview_root)
        binding=self._binding(bundle)
        with self.connection(write=True) as db:
            row=db.execute('SELECT * FROM learning_approvals WHERE approval_hash=?',(bundle['approval_hash'],)).fetchone()
            if row:
                if row['binding_hash']!=fingerprint(binding):raise CorpusError('corpus_learning_binding_changed')
                return {'id':row['id'],'state':row['state'],'max_dispatches':row['max_dispatches']}
            ident=str(uuid4())
            db.execute('INSERT INTO learning_approvals VALUES(?,?,?,?,?,\'active\',2,?)',
                       (ident,bundle['approval_hash'],encoded(bundle['approval']),encoded(binding),fingerprint(binding),now()))
            return {'id':ident,'state':'active','max_dispatches':2}

    def _guard(self,approval_id):
        with self.connection() as db:
            row=db.execute('SELECT * FROM learning_approvals WHERE id=?',(approval_id,)).fetchone()
            if not row or row['state']!='active':raise CorpusError('corpus_learning_approval_unavailable')
            binding=json.loads(row['binding_json']);expected=row['binding_hash'];approval_hash=row['approval_hash']
        if fingerprint(binding)!=expected:raise CorpusError('corpus_learning_binding_changed')
        bundle=load_approved_bundle(self.corpus,Path(binding['approval_path']),Path(binding['preview_root']))
        if bundle['approval_hash']!=approval_hash or fingerprint(self._binding(bundle))!=expected:
            raise CorpusError('corpus_learning_binding_changed')
        return bundle

    @staticmethod
    def _prior(db,operation_id,action,request):
        field(operation_id,'operation_id')
        row=db.execute('SELECT * FROM learning_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if row:
            if row['action']!=action or row['request_hash']!=fingerprint(request):raise CorpusError('corpus_learning_operation_conflict')
            return json.loads(row['result_json'])

    @staticmethod
    def _save(db,operation_id,action,request,result):
        db.execute('INSERT INTO learning_operations VALUES(?,?,?,?,?)',(operation_id,action,fingerprint(request),encoded(result),now()))
        return result

    @staticmethod
    def _row(db,job_id):
        row=db.execute('SELECT * FROM learning_jobs WHERE id=?',(job_id,)).fetchone()
        if not row:raise CorpusError('corpus_learning_job_missing')
        return dict(row)

    @staticmethod
    def _status(db,job_id):
        row=CorpusLearning._row(db,job_id)
        return {k:row[k] for k in ('id','approval_id','position','state','attempt_id','started_epoch','cancelled_at','raw_response_hash','http_status','error_code')}|{
            'usage':json.loads(row['usage_json']) if row['usage_json'] else None,
            'result':json.loads(row['result_json']) if row['result_json'] else None,
            'proposal_count':db.execute('SELECT count(*) FROM learning_proposals WHERE job_id=?',(job_id,)).fetchone()[0],
            'assets_automatically_published':False}

    def status(self,job_id):
        with self.connection() as db:return self._status(db,job_id)

    def inspect(self):
        with self.connection() as db:
            return {'schema':SCHEMA_VERSION,'jobs':[self._status(db,r[0]) for r in db.execute('SELECT id FROM learning_jobs ORDER BY created_at,id')],
                    'approvals':db.execute('SELECT count(*) FROM learning_approvals').fetchone()[0],
                    'dispatch_attempts':db.execute('SELECT count(*) FROM learning_jobs WHERE attempt_id IS NOT NULL').fetchone()[0]}

    def prepare(self,approval_id,position,operation_id):
        if type(position) is not int or position not in (1,2):raise CorpusError('corpus_learning_position_unapproved')
        bundle=self._guard(approval_id);item=next(p for p in bundle['positions'] if p['position']==position)
        request={'approval_id':approval_id,'position':position,'position_key':item['position_key']}
        with self.corpus.connection(write=True) as authority:
            if self.corpus._source_proof(authority,bundle['view']['parse_id'])!=item['source_proof']:
                raise CorpusError('corpus_learning_source_changed')
            with self.connection(write=True) as db:
                prior=self._prior(db,operation_id,'prepare',request)
                if prior is not None:return self._status(db,prior['id'])
                if db.execute('SELECT state FROM learning_approvals WHERE id=?',(approval_id,)).fetchone()[0]!='active':
                    raise CorpusError('corpus_learning_approval_unavailable')
                existing=db.execute('SELECT id FROM learning_jobs WHERE position_key=?',(item['position_key'],)).fetchone()
                if existing:return self._save(db,operation_id,'prepare',request,self._status(db,existing[0]))
                ident=str(uuid4())
                db.execute('''INSERT INTO learning_jobs(id,approval_id,position,position_key,state,request_json,request_hash,
                    model_json,sources_json,sources_hash,created_at) VALUES(?,?,?,?,'prepared',?,?,?,?,?,?)''',
                    (ident,approval_id,position,item['position_key'],encoded(item['request']),item['request_hash'],
                     encoded(item['model']),encoded(item['sources']),fingerprint(item['sources']),now()))
                return self._save(db,operation_id,'prepare',request,self._status(db,ident))

    def revoke(self,approval_id):
        with self.connection(write=True) as db:
            if not db.execute('SELECT 1 FROM learning_approvals WHERE id=?',(approval_id,)).fetchone():
                raise CorpusError('corpus_learning_approval_unavailable')
            db.execute("UPDATE learning_approvals SET state='revoked' WHERE id=?",(approval_id,))
            db.execute("UPDATE learning_jobs SET state='cancelled',cancelled_at=?,error_code='approval_revoked' WHERE approval_id=? AND state='prepared'",(now(),approval_id))
        return {'id':approval_id,'state':'revoked'}

    def cancel(self,job_id):
        with self.connection(write=True) as db:
            row=self._row(db,job_id)
            if row['state']=='prepared':db.execute("UPDATE learning_jobs SET state='cancelled',cancelled_at=? WHERE id=?",(now(),job_id))
            elif row['state']=='dispatched':db.execute('UPDATE learning_jobs SET cancelled_at=? WHERE id=?',(now(),job_id))
            return self._status(db,job_id)

    @workflow
    @resume_on_limit
    async def run(self,job_id,credentials):
        with self.connection() as db:row=self._row(db,job_id)
        if row['raw_response'] is not None:return self.finalize(job_id)
        if row['state']!='prepared' or row['attempt_id']:return self.status(job_id)
        try:
            bundle=self._guard(row['approval_id'])
            item=next(p for p in bundle['positions'] if p['position_key']==row['position_key'])
            request=json.loads(row['request_json']);public=json.loads(row['model_json'])
            if (fingerprint(request)!=row['request_hash'] or request!=item['request'] or public!=item['model']
                    or fingerprint(json.loads(row['sources_json']))!=row['sources_hash']
                    or json.loads(row['sources_json'])!=item['sources']):raise CorpusError('corpus_learning_job_contract_changed')
            require_generation_provider(credentials)
            live=public|{'encrypted_key':credentials.get('encrypted_key','')}
            if build_request(live,request['messages'])[0]!=request:raise CorpusError('corpus_learning_wire_changed')
            self.client.preflight(live,request)
        except (CorpusError,ValueError,OSError,KeyError,StopIteration):
            with self.connection(write=True) as db:
                db.execute("UPDATE learning_jobs SET state='stale',error_code='dispatch_preflight_or_authority_failed' WHERE id=? AND state='prepared' AND attempt_id IS NULL",(job_id,))
            return self.status(job_id)
        with self.corpus.connection(write=True) as authority:
            if self.corpus._source_proof(authority,bundle['view']['parse_id'])!=item['source_proof']:
                raise CorpusError('corpus_learning_source_changed')
            with self.connection(write=True) as db:
                fresh=self._row(db,job_id)
                if fresh['state']!='prepared' or fresh['attempt_id']:return self._status(db,job_id)
                approved=db.execute('SELECT * FROM learning_approvals WHERE id=?',(row['approval_id'],)).fetchone()
                if approved['state']!='active':raise CorpusError('corpus_learning_approval_unavailable')
                used=db.execute('SELECT count(*) FROM learning_jobs WHERE approval_id=? AND attempt_id IS NOT NULL',(row['approval_id'],)).fetchone()[0]
                if used>=approved['max_dispatches']:raise CorpusError('corpus_learning_budget_reached')
                external_gate(self,'corpus','corpus/'+job_id)
                attempt=str(uuid4())
                db.execute("UPDATE learning_jobs SET state='dispatched',attempt_id=?,started_epoch=? WHERE id=?",(attempt,time.time(),job_id))
        def save_raw(raw,*,http_status,diagnostics):
            with self.connection(write=True) as db:
                saved=self._row(db,job_id)
                if saved['attempt_id']!=attempt:raise CorpusError('corpus_learning_receipt_attempt_conflict')
                if saved['raw_response'] is not None:
                    if saved['raw_response_hash']==digest(raw) and saved['http_status']==http_status:return
                    raise CorpusError('corpus_learning_receipt_conflict')
                db.execute('UPDATE learning_jobs SET raw_response=?,raw_response_hash=?,http_status=?,transport_json=? WHERE id=?',
                           (raw,digest(raw),http_status,encoded(diagnostics),job_id))
        try:
            diagnostics=await self.client.complete(live,request,save_raw=save_raw)
            with self.connection(write=True) as db:db.execute('UPDATE learning_jobs SET transport_json=? WHERE id=?',(encoded(diagnostics),job_id))
        except BaseException as error:
            with self.connection(write=True) as db:
                fresh=self._row(db,job_id)
                if fresh['raw_response'] is None:
                    diagnostic=getattr(error,'diagnostics',{})
                    unsent=diagnostic.get('request_headers_sent') is False and diagnostic.get('response_headers_received') is False
                    db.execute('UPDATE learning_jobs SET state=?,transport_json=?,result_json=?,error_code=? WHERE id=?',
                               ('failed' if unsent else 'unknown',encoded(diagnostic),
                                encoded({'status':'not_dispatched' if unsent else 'unknown','usage_class':'not_applicable' if unsent else 'unknown'}),
                                'corpus_learning_transport_failed',job_id))
            if isinstance(error,asyncio.CancelledError):
                with self.connection(write=True) as db:db.execute('UPDATE learning_jobs SET cancelled_at=? WHERE id=?',(now(),job_id))
                if fresh['raw_response'] is not None:self.finalize(job_id)
                raise
            if not isinstance(error,Exception):
                if fresh['raw_response'] is not None:self.finalize(job_id)
                raise
            if fresh['raw_response'] is None:return self.status(job_id)
        return self.finalize(job_id)

    def finalize(self,job_id):
        with self.connection() as db:row=self._row(db,job_id)
        if row['state'] in ('completed','stale','cancelled','failed') and row['result_json'] is not None:return self.status(job_id)
        raw=bytes(row['raw_response']) if row['raw_response'] is not None else None
        if raw is None:raise CorpusError('corpus_learning_receipt_unavailable')
        if digest(raw)!=row['raw_response_hash']:raise CorpusError('corpus_learning_receipt_changed')
        try:data=json.loads(raw)
        except (UnicodeError,ValueError):data={}
        usage=usage_fields(data.get('usage') if isinstance(data,dict) else None,deepseek=True)
        with self.connection(write=True) as db:db.execute('UPDATE learning_jobs SET usage_json=? WHERE id=?',(encoded(usage) if usage is not None else None,job_id))
        try:
            bundle=self._guard(row['approval_id'])
            item=next(p for p in bundle['positions'] if p['position_key']==row['position_key'])
            if json.loads(row['sources_json'])!=item['sources'] or fingerprint(item['sources'])!=row['sources_hash']:
                raise CorpusError('corpus_learning_sources_changed')
        except (CorpusError,ValueError,KeyError,StopIteration):
            with self.connection(write=True) as db:
                db.execute("UPDATE learning_jobs SET state='stale',result_json=?,error_code='source_or_approval_changed' WHERE id=?",(encoded({'status':'source_or_approval_changed','proposals':[]}),job_id))
            return self.status(job_id)
        accepted,rejected=[],[]
        result={'status':'invalid_output','proposals':[]}
        terminal='completed'
        if row['http_status']!=200:
            result={'status':'http_failure','proposals':[]};terminal='failed'
        else:
            try:
                choice=data['choices'][0]
                if choice.get('finish_reason')!='stop' or choice['message'].get('tool_calls'):
                    raise ValueError('incomplete_output')
                accepted,rejected=filter_candidates(choice['message']['content'],item['sources'])
                result={'status':'proposals_prepared','proposals':[],'rejected':rejected}
            except (KeyError,IndexError,TypeError,ValueError,ExpressionAssetError):pass
        with self.corpus.connection(write=True) as authority:
            if self.corpus._source_proof(authority,bundle['view']['parse_id'])!=item['source_proof']:
                raise CorpusError('corpus_learning_source_changed')
            with self.connection(write=True) as db:
                fresh=self._row(db,job_id)
                if fresh['state'] in ('completed','stale','cancelled','failed') and fresh['result_json'] is not None:return self._status(db,job_id)
                if fresh['cancelled_at']:
                    terminal='cancelled';result={'status':'cancelled','proposals':[]};accepted=[]
                elif db.execute('SELECT state FROM learning_approvals WHERE id=?',(row['approval_id'],)).fetchone()[0]!='active':
                    terminal='stale';result={'status':'approval_revoked','proposals':[]};accepted=[]
                for ordinal,candidate in enumerate(accepted):
                    ident=fingerprint([job_id,ordinal,candidate['content_hash']])
                    refs=candidate['source_refs'];source=next(s for s in item['sources'] if s['source_id']==refs[0]['source_id'])
                    candidate=candidate|{'source_binding':{k:source[k] for k in ('view_id','view_message_id','kind','speaker','content_hash')},
                                         'approval_id':row['approval_id'],'request_hash':row['request_hash'],'response_hash':row['raw_response_hash']}
                    db.execute("INSERT OR IGNORE INTO learning_proposals VALUES(?,?,?,?,?,'pending')",(ident,job_id,ordinal,encoded(candidate),fingerprint(candidate)))
                    result['proposals'].append(ident)
                db.execute('UPDATE learning_jobs SET state=?,result_json=?,error_code=? WHERE id=?',
                           (terminal,encoded(result),None if result['status']=='proposals_prepared' else result['status'],job_id))
                return self._status(db,job_id)

    def recover(self):
        with self.connection(write=True) as db:
            complete=[r[0] for r in db.execute("SELECT id FROM learning_jobs WHERE state IN ('dispatched','unknown') AND raw_response IS NOT NULL")]
            db.execute("UPDATE learning_jobs SET state='unknown',error_code='restarted_without_complete_receipt' WHERE state='dispatched' AND raw_response IS NULL")
        for ident in complete:self.finalize(ident)
        return self.inspect()
