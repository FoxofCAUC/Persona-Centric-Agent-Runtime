"""Approval and single-dispatch tests use marker sources and interface stubs only."""
import asyncio
from datetime import datetime,timedelta,timezone
import inspect
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from persona_runtime.corpus_files import CorpusError,digest
from persona_runtime.corpus_store import fingerprint
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_learning import CorpusLearning
from persona_runtime.corpus_learning_bundle import load_approved_bundle
from persona_runtime.corpus_learning_transport import CorpusCallFailure
from persona_runtime.corpus_learning_review import export_learning_review,peak_estimate
from scripts.pcar_corpus_learning import main as learning_main
from persona_runtime.expression_learning import learning_messages
from persona_runtime.provider_compat import build_request
from persona_runtime.vendor.maibot_expression_learning_prompt import PROMPT
from tests.test_corpus_moect import document,message,pair


CREDENTIALS={'base_url':'https://api.deepseek.com','name':'deepseek-flash','encrypted_key':'KEY-REFERENCE-001'}


def response(source_id=1,finish='stop'):
    content=json.dumps({'candidates':[{'title':'S-001','description':'D-001','source_id':source_id}]})
    return json.dumps({'model':'deepseek-flash','choices':[{'finish_reason':finish,'message':{'content':content}}],
                       'usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20}}).encode()


class StubClient:
    def __init__(self,mode='success',after_save=None):self.mode=mode;self.after_save=after_save;self.calls=0
    def preflight(self,model,request):
        if build_request(model,request['messages'])[0]!=request:raise ValueError('WIRE-001')
    async def complete(self,model,request,*,save_raw):
        self.calls+=1
        if self.mode in ('unknown','unsent'):
            raise CorpusCallFailure('NETWORK-001',{'request_headers_sent':self.mode!='unsent','response_headers_received':False})
        status=503 if self.mode=='http_failure' else 200
        raw=(b'{"error":{"code":"ERROR-001"}}' if status!=200 else b'RESULT-INVALID' if self.mode=='invalid_json'
             else response(99 if self.mode=='wrong_source' else 1,'length' if self.mode=='length' else 'stop'))
        diagnostics={'request_headers_sent':True,'response_headers_received':True,'http_status':status,'raw_complete':True}
        save_raw(raw,http_status=status,diagnostics=diagnostics)
        if self.after_save:self.after_save()
        return diagnostics


class WaitingClient(StubClient):
    def __init__(self):
        super().__init__();self.started=asyncio.Event();self.release=asyncio.Event()
    async def complete(self,model,request,*,save_raw):
        self.calls+=1;self.started.set()
        try:await self.release.wait()
        except asyncio.CancelledError as error:
            error.diagnostics={'request_headers_sent':True,'response_headers_received':False};raise
        diagnostic={'request_headers_sent':True,'response_headers_received':True,'http_status':200,'raw_complete':True}
        save_raw(response(),http_status=200,diagnostics=diagnostic)
        return diagnostic


class LearningFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pcar-learning-',dir=os.environ.get('PCAR_CORPUS_TEST_ROOT'))
        self.addCleanup(self.temp.cleanup);self.directory=Path(self.temp.name).resolve()
        self.corpus=DatasetCorpusStore(self.directory/'corpus');self.corpus.initialize()
        dataset=self.corpus.create_dataset('DATASET-001','CREATE-001')['id'];self.dataset_id=dataset
        data=document();records=[]
        for n in range(1,11):
            row=message(n,own=n%2==0,body='ID-'+str(n).zfill(3))
            row['create_time']+=((n-1)//2)*4000
            row['time_str']=datetime.fromtimestamp(row['create_time'],timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            records.append(row)
        data.update(messages=records,count=len(records));raw,md=pair(data)
        imports=[];sources=[]
        for kind,value in [('json',raw),('md',md)]:
            path=self.directory/('source.'+kind);path.write_bytes(value)
            preview=self.corpus.preview(dataset,path,'PREVIEW-'+kind)
            imports.append(self.corpus.retain_original(preview['id'],preview['manifest_hash'],'COMMIT-'+kind)['id'])
            sources.append({'kind':kind,'sha256':digest(value)})
        grant=self.directory/'material-grant.json'
        grant.write_text(json.dumps({'version':'author-wechat-material-grant-v1','conversation_kind':'private',
            'training_input_authorized':True,'speaker_scope':'both','speaker_confirmation':'author_confirmed_both_as_human_expression_sources',
            'model_request_version_approved':False,'sources':sources}),encoding='utf-8')
        parse=self.corpus.parse_pair(dataset,*imports,grant,'PARSE-001')['id']
        view=self.corpus.build_version(parse,'VIEW-001');self.view=view
        payload=self.corpus.load_version(view['id']);by_id={r['message_id']:r for r in payload['messages']}
        fragments=[f for f in payload['fragments'] if f['split']=='learn'][:2]
        self.preview=self.directory/'preview';self.preview.mkdir()
        root=Path(__file__).resolve().parents[1]
        manifest={'view_id':view['id'],'view_payload_hash':view['payload_hash'],'batches':[],
                  'legacy_assembler':{'path':'persona_runtime/expression_learning.py'},
                  'vendor_template':{'path':'persona_runtime/vendor/maibot_expression_learning_prompt.py','prompt_utf8_sha256':digest(PROMPT.encode())},
                  'wire_serializer':{'path':'persona_runtime/provider_compat.py'}}
        for name in ('legacy_assembler','vendor_template','wire_serializer'):
            manifest[name]['sha256']=digest((root/manifest[name]['path']).read_bytes().replace(b'\r\n',b'\n'))
        for position,fragment in enumerate(fragments,1):
            members=[by_id[i] for i in fragment['message_ids']]
            messages=learning_messages([{'body':r['text']} for r in members])
            model={'base_url':'https://api.deepseek.com','name':'deepseek-flash','timeout':120,'max_tokens':1024,'_json_output':True,
                   '_dialogue_profile':{'version':'natural-dialogue-v8','seed':'CORPUS-PREVIEW-'+str(position),'temperature':.2}}
            packet={'view_id':view['id'],'split':'learn','batch':position,'fragment_id':fragment['id'],'messages':messages,
                    'source_map':[{'source_id':n,'message_id':r['message_id'],'role':r['role'],'source_text_hash':r['source_text_hash'],'view_text_hash':r['text_hash']} for n,r in enumerate(members,1)]}
            packet_file=self.preview/f'batch-{position}.json';packet_file.write_text(json.dumps(packet,ensure_ascii=False),encoding='utf-8')
            wire_file=self.preview/f'wire-{position}.json';wire_file.write_text(json.dumps({'request':build_request(model,messages)[0]},ensure_ascii=False),encoding='utf-8')
            manifest['batches'].append({'batch':position,'file':packet_file.name,'sha256':digest(packet_file.read_bytes()),
                                        'wire_file':wire_file.name,'wire_sha256':digest(wire_file.read_bytes())})
        manifest_path=self.preview/'manifest.private.json';manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
        approval={'version':'corpus-learning-approval-v1','author_message':'APPROVAL-001','view_id':view['id'],'view_payload_hash':view['payload_hash'],
                  'preview_manifest_sha256':digest(manifest_path.read_bytes()),'allowed_provider':'deepseek_official','allowed_model':'deepseek-flash',
                  'max_dispatches':2,'automatic_asset_publication':False,'new_prompt_writing_authorized':False,
                  'batches':[{'position':b['batch'],'file_sha256':b['sha256'],'wire_sha256':b['wire_sha256']} for b in manifest['batches']]}
        self.approval_path=self.directory/'approval.json';self.approval_path.write_text(json.dumps(approval),encoding='utf-8')
        self.client=StubClient();self.learning=CorpusLearning(self.directory/'learning',self.corpus,self.client)
        self.learning.initialize();self.approval_id=self.learning.register_approval(self.approval_path,self.preview)['id']

    def prepare(self,position=1,operation='PREPARE-001'):
        return self.learning.prepare(self.approval_id,position,operation)['id']

    def run_job(self,job):return asyncio.run(self.learning.run(job,CREDENTIALS))

    def test_approved_wire_matches_existing_template_and_private_source_kind(self):
        bundle=load_approved_bundle(self.corpus,self.approval_path,self.preview)
        self.assertEqual(len(bundle['positions']),2)
        self.assertEqual(bundle['positions'][0]['request']['messages'],learning_messages(bundle['positions'][0]['sources']))
        self.assertTrue(all(s['kind']=='private_corpus_view' for p in bundle['positions'] for s in p['sources']))
        self.assertEqual(self.client.calls,0)

    def test_changed_batch_or_approval_manifest_is_rejected_before_dispatch(self):
        job=self.prepare();path=self.preview/'batch-1.json';path.write_bytes(path.read_bytes()+b' ')
        self.assertEqual(self.run_job(job)['state'],'stale')
        self.assertEqual(self.client.calls,0)

    def test_prepare_dedupes_same_position_even_under_new_operation_id(self):
        one=self.prepare();two=self.prepare(operation='PREPARE-002')
        self.assertEqual(one,two)
        with self.assertRaisesRegex(CorpusError,'operation_conflict'):self.prepare(2)
        with self.assertRaisesRegex(CorpusError,'position_unapproved'):self.prepare(3,'PREPARE-003')

    def test_success_is_single_dispatch_and_proposals_stay_pending(self):
        job=self.prepare();result=self.run_job(job)
        self.assertEqual(result['state'],'completed');self.assertEqual(result['proposal_count'],1)
        self.assertFalse(result['assets_automatically_published'])
        self.assertEqual(self.run_job(job),result);self.assertEqual(self.client.calls,1)
        with self.learning.connection() as db:
            proposal=json.loads(db.execute('SELECT data_json FROM learning_proposals').fetchone()[0])
            self.assertEqual(proposal['source_quote'],'ID-001')
            self.assertEqual(proposal['source_binding']['kind'],'private_corpus_view')

    def test_exactly_two_positions_can_consume_the_approved_study(self):
        jobs=[self.prepare(1),self.prepare(2,'PREPARE-002')]
        for job in jobs:self.run_job(job)
        self.assertEqual(self.client.calls,2)
        self.assertEqual(self.learning.inspect()['dispatch_attempts'],2)
        self.run_job(jobs[0]);self.assertEqual(self.client.calls,2)

    def test_unknown_request_is_never_automatically_repeated(self):
        self.client.mode='unknown';job=self.prepare();result=self.run_job(job)
        self.assertEqual(result['state'],'unknown');self.assertIsNone(result['usage'])
        self.assertEqual(self.run_job(job)['state'],'unknown');self.assertEqual(self.client.calls,1)

    def test_proven_unsent_failure_remains_one_local_attempt(self):
        self.client.mode='unsent';job=self.prepare();result=self.run_job(job)
        self.assertEqual(result['state'],'failed');self.assertEqual(result['result']['usage_class'],'not_applicable')
        self.run_job(job);self.assertEqual(self.client.calls,1)

    def test_http_failure_retains_complete_first_body_and_does_not_retry(self):
        self.client.mode='http_failure';job=self.prepare();result=self.run_job(job)
        self.assertEqual(result['state'],'failed');self.assertEqual(result['http_status'],503)
        self.assertIsNotNone(result['raw_response_hash']);self.assertEqual(result['proposal_count'],0)
        self.run_job(job);self.assertEqual(self.client.calls,1)

    def test_invalid_or_truncated_result_keeps_receipt_and_usage(self):
        for mode in ('invalid_json','length'):
            self.client.mode=mode
            position=1 if mode=='invalid_json' else 2
            result=self.run_job(self.prepare(position,'PREPARE-'+mode))
            self.assertEqual(result['state'],'completed');self.assertEqual(result['proposal_count'],0)
            self.assertIsNotNone(result['raw_response_hash'])
            if mode=='length':self.assertEqual(result['usage']['total_tokens'],20)

    def test_invalid_source_reference_is_rejected_without_rewriting_result(self):
        self.client.mode='wrong_source';job=self.prepare();result=self.run_job(job)
        self.assertEqual(result['proposal_count'],0)
        self.assertEqual(result['result']['rejected'][0]['reason'],'invalid_source_id')
        self.assertEqual(self.client.calls,1)

    def test_revocation_before_dispatch_prevents_client_call(self):
        job=self.prepare();self.learning.revoke(self.approval_id)
        self.assertEqual(self.run_job(job)['state'],'cancelled');self.assertEqual(self.client.calls,0)
        with self.assertRaisesRegex(CorpusError,'approval_unavailable'):self.prepare(2,'PREPARE-002')

    def test_source_withdrawal_before_dispatch_invalidates_the_job(self):
        job=self.prepare();self.corpus.revoke_dataset(self.dataset_id,1,'REVOKE-001')
        self.assertEqual(self.run_job(job)['state'],'stale');self.assertEqual(self.client.calls,0)

    def test_inflight_source_withdrawal_preserves_receipt_but_no_proposal(self):
        job=self.prepare();self.client.after_save=lambda:self.corpus.revoke_dataset(self.dataset_id,1,'REVOKE-001')
        result=self.run_job(job)
        self.assertEqual(result['state'],'stale');self.assertEqual(result['proposal_count'],0)
        self.assertIsNotNone(result['raw_response_hash']);self.assertEqual(result['usage']['total_tokens'],20)

    def test_cancelled_response_does_not_create_a_pending_asset(self):
        job=self.prepare();self.client.after_save=lambda:self.learning.cancel(job)
        result=self.run_job(job)
        self.assertEqual(result['state'],'cancelled');self.assertEqual(result['proposal_count'],0)
        self.assertIsNotNone(result['raw_response_hash'])

    def test_local_finalization_failure_recovers_from_receipt_without_new_call(self):
        job=self.prepare()
        with patch('persona_runtime.corpus_learning.filter_candidates',side_effect=OSError('LOCAL-001')):
            with self.assertRaises(OSError):self.run_job(job)
        self.assertIsNotNone(self.learning.status(job)['raw_response_hash'])
        result=self.learning.finalize(job)
        self.assertEqual(result['proposal_count'],1);self.assertEqual(self.client.calls,1)

    def test_restart_without_receipt_becomes_unknown_and_cannot_send_again(self):
        job=self.prepare()
        with self.learning.connection(write=True) as db:
            db.execute("UPDATE learning_jobs SET state='dispatched',attempt_id='ATTEMPT-001',started_epoch=1 WHERE id=?",(job,))
        self.learning.recover()
        self.assertEqual(self.run_job(job)['state'],'unknown');self.assertEqual(self.client.calls,0)

    def test_restart_with_receipt_completes_locally(self):
        job=self.prepare();raw=response()
        with self.learning.connection(write=True) as db:
            db.execute("UPDATE learning_jobs SET state='dispatched',attempt_id='ATTEMPT-001',started_epoch=1,raw_response=?,raw_response_hash=?,http_status=200 WHERE id=?",(raw,digest(raw),job))
        self.learning.recover()
        self.assertEqual(self.learning.status(job)['proposal_count'],1);self.assertEqual(self.client.calls,0)

    def test_frozen_request_and_first_receipt_cannot_be_overwritten(self):
        job=self.prepare();self.run_job(job)
        for sql in ["UPDATE learning_jobs SET request_json='{}'", "UPDATE learning_jobs SET raw_response=x'00'", "DELETE FROM learning_proposals"]:
            with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError):
                with self.learning.connection(write=True) as db:db.execute(sql)

    def test_public_status_does_not_contain_private_input_or_key_reference(self):
        job=self.prepare();self.run_job(job)
        output=json.dumps(self.learning.status(job))
        self.assertNotIn('ID-001',output);self.assertNotIn('KEY-REFERENCE-001',output)

    def test_stubbed_research_flow_requires_no_network(self):
        loop=asyncio.new_event_loop()
        try:
            with patch('socket.socket.connect',side_effect=AssertionError('NETWORK-001')), \
                 patch('socket.socket.connect_ex',side_effect=AssertionError('NETWORK-001')):
                job=self.prepare()
                result=loop.run_until_complete(self.learning.run(job,CREDENTIALS))
                self.assertEqual(result['proposal_count'],1)
        finally:loop.close()

    def test_concurrent_runs_only_enter_transport_once(self):
        job=self.prepare()
        async def check():
            client=WaitingClient();self.learning.client=client
            first=asyncio.create_task(self.learning.run(job,CREDENTIALS))
            await client.started.wait()
            second=await self.learning.run(job,CREDENTIALS)
            self.assertEqual(second['state'],'dispatched')
            client.release.set();result=await first
            self.assertEqual(result['proposal_count'],1);self.assertEqual(client.calls,1)
        asyncio.run(check())

    def test_task_cancellation_leaves_unknown_attempt_and_no_replay(self):
        job=self.prepare()
        async def check():
            client=WaitingClient();self.learning.client=client
            task=asyncio.create_task(self.learning.run(job,CREDENTIALS));await client.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            state=self.learning.status(job)
            self.assertEqual(state['state'],'unknown');self.assertIsNotNone(state['cancelled_at'])
            self.assertEqual((await self.learning.run(job,CREDENTIALS))['state'],'unknown')
            self.assertEqual(client.calls,1)
        asyncio.run(check())

    def test_review_keeps_exact_first_receipt_and_no_credential(self):
        job=self.prepare();self.run_job(job)
        output=self.directory/'review'
        result=export_learning_review(self.learning,output)
        self.assertEqual(result['proposals'],1)
        self.assertEqual((output/'batch-1.response.private.json').read_bytes(),response())
        self.assertNotIn('KEY-REFERENCE-001',(output/'index.html').read_text(encoding='utf-8'))
        self.assertIsNone(result['actual_billing_cny'])

    def test_unknown_or_partial_usage_is_not_counted_as_free(self):
        self.assertIsNone(peak_estimate(None))
        self.assertIsNone(peak_estimate({'prompt_tokens':12}))
        self.assertGreater(peak_estimate({'prompt_tokens':12,'completion_tokens':8}),0)

    def test_status_cli_does_not_load_credentials_or_return_input(self):
        job=self.prepare();output=io.StringIO()
        with patch('persona_runtime.config.ConfigStore.load',side_effect=AssertionError('KEY-READ-001')),redirect_stdout(output):
            code=learning_main(['--corpus',str(self.corpus.root),'--ledger',str(self.learning.root),'status','--job-id',job])
        self.assertEqual(code,0)
        self.assertNotIn('ID-001',output.getvalue())


if __name__=='__main__':unittest.main()
