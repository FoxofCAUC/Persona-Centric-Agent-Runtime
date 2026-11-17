"""Bounded real-source coverage tests; marker fixtures never become model samples."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from persona_runtime.corpus_files import CorpusError,digest
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_learning import CorpusLearning
from persona_runtime.expression_learning import learning_messages
from persona_runtime.provider_compat import build_request
from persona_runtime.vendor.maibot_expression_learning_prompt import PROMPT
from experiments.corpus_learning_coverage import (select_six,freeze_coverage,prepare_coverage,
    run_coverage,verify_prepared,export_coverage,atomic_json,validate_envelope)
from tests.test_corpus_moect import document,message,pair
from tests.test_corpus_learning import StubClient,CREDENTIALS


class CoverageSelection(unittest.TestCase):
    def candidates(self):
        return [{'fragment_id':'F-'+str(n),'group_id':'G-'+str(n),'first_epoch':n*2000,
                 'month':['M-01','M-02','M-02','M-02','M-03','M-04','M-05'][n],
                 'source_count':[2,3,4,5,4,6,4][n],'dominant':'A' if n%2 else 'B','first_role':'A' if n%3 else 'B'} for n in range(7)]

    def test_selection_is_deterministic_covers_months_and_lengths_without_semantic_scoring(self):
        one=select_six(self.candidates());two=select_six(list(reversed(self.candidates())))
        self.assertEqual(one,two)
        self.assertEqual(len(one),6)
        self.assertEqual(len({c['month'] for c in one}),5)
        self.assertEqual(len({c['source_count'] for c in one}),5)

    def test_not_enough_distinct_sources_or_unbounded_search_is_refused(self):
        with self.assertRaises(CorpusError):select_six(self.candidates()[:5])
        candidates=self.candidates()
        for item in candidates:item['group_id']='SAME'
        with self.assertRaisesRegex(CorpusError,'distinct_groups'):select_six(candidates)
        with self.assertRaisesRegex(CorpusError,'requires_revision'):select_six(self.candidates()*6)


class CoverageFlow(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pcar-coverage-',dir=os.environ.get('PCAR_CORPUS_TEST_ROOT'))
        self.addCleanup(self.temp.cleanup);self.directory=Path(self.temp.name).resolve()
        self.corpus=DatasetCorpusStore(self.directory/'corpus');self.corpus.initialize()
        dataset=self.corpus.create_dataset('DATASET-001','CREATE-001')['id']
        data=document();records=[]
        for n in range(1,41):
            row=message(n,own=n%2==0,body='ID-'+str(n).zfill(3));row['create_time']+=((n-1)//2)*864000
            row['time_str']=datetime.fromtimestamp(row['create_time'],timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S');records.append(row)
        data.update(messages=records,count=len(records));raw,md=pair(data);imports=[];sources=[]
        for kind,value in [('json',raw),('md',md)]:
            path=self.directory/('source.'+kind);path.write_bytes(value)
            preview=self.corpus.preview(dataset,path,'PREVIEW-'+kind)
            imports.append(self.corpus.retain_original(preview['id'],preview['manifest_hash'],'COMMIT-'+kind)['id'])
            sources.append({'kind':kind,'sha256':digest(value)})
        grant=self.directory/'material-grant.json'
        atomic_json(grant,{'version':'author-wechat-material-grant-v1','conversation_kind':'private','training_input_authorized':True,
            'speaker_scope':'both','speaker_confirmation':'author_confirmed_both_as_human_expression_sources','model_request_version_approved':False,'sources':sources})
        parse=self.corpus.parse_pair(dataset,*imports,grant,'PARSE-001')['id']
        view=self.corpus.build_version(parse,'VIEW-001');self.view=view
        payload=self.corpus.load_version(view['id']);by_id={r['message_id']:r for r in payload['messages']}
        chosen=[f for f in payload['fragments'] if f['split']=='learn'][:2]
        root=Path(__file__).resolve().parents[1];self.preview=self.directory/'preview';self.preview.mkdir()
        manifest={'view_id':view['id'],'view_payload_hash':view['payload_hash'],'batches':[],
            'legacy_assembler':{'path':'persona_runtime/expression_learning.py'},
            'vendor_template':{'path':'persona_runtime/vendor/maibot_expression_learning_prompt.py','prompt_utf8_sha256':digest(PROMPT.encode())},
            'wire_serializer':{'path':'persona_runtime/provider_compat.py'}}
        for name in ('legacy_assembler','vendor_template','wire_serializer'):
            manifest[name]['sha256']=digest((root/manifest[name]['path']).read_bytes().replace(b'\r\n',b'\n'))
        for position,fragment in enumerate(chosen,1):
            rows=[by_id[i] for i in fragment['message_ids']];messages=learning_messages([{'body':r['text']} for r in rows])
            model={'base_url':'https://api.deepseek.com','name':'deepseek-flash','timeout':120,'max_tokens':1024,'_json_output':True,
                   '_dialogue_profile':{'version':'natural-dialogue-v8','seed':'CORPUS-PREVIEW-'+str(position),'temperature':.2}}
            packet={'view_id':view['id'],'split':'learn','batch':position,'fragment_id':fragment['id'],'messages':messages,
                'source_map':[{'source_id':n,'message_id':r['message_id'],'role':r['role'],'source_text_hash':r['source_text_hash'],'view_text_hash':r['text_hash']} for n,r in enumerate(rows,1)]}
            source=self.preview/f'batch-{position}.json';atomic_json(source,packet)
            wire=self.preview/f'wire-{position}.json';atomic_json(wire,{'request':build_request(model,messages)[0]})
            manifest['batches'].append({'batch':position,'file':source.name,'sha256':digest(source.read_bytes()),'wire_file':wire.name,'wire_sha256':digest(wire.read_bytes())})
        manifest_path=self.preview/'manifest.private.json';atomic_json(manifest_path,manifest)
        approval=self.directory/'approval.json';atomic_json(approval,{'version':'corpus-learning-approval-v1','author_message':'APPROVAL-001',
            'view_id':view['id'],'view_payload_hash':view['payload_hash'],'preview_manifest_sha256':digest(manifest_path.read_bytes()),
            'allowed_provider':'deepseek_official','allowed_model':'deepseek-flash','max_dispatches':2,'automatic_asset_publication':False,'new_prompt_writing_authorized':False,
            'batches':[{'position':r['batch'],'file_sha256':r['sha256'],'wire_sha256':r['wire_sha256']} for r in manifest['batches']]})
        self.client=StubClient();self.learning=CorpusLearning(self.directory/'learning',self.corpus,self.client);self.learning.initialize()
        approval_id=self.learning.register_approval(approval,self.preview)['id']
        self.prior_ids=[self.learning.prepare(approval_id,p,'PREPARE-'+str(p))['id'] for p in (1,2)]
        import asyncio
        for job in self.prior_ids:asyncio.run(self.learning.run(job,CREDENTIALS))
        self.prior_state=self.directory/'first-state.json';atomic_json(self.prior_state,{'job_ids':self.prior_ids})
        self.scope=self.directory/'scope.json';atomic_json(self.scope,{'version':'corpus-coverage-author-scope-v1','date':'2026-11-17','author_message':'APPROVAL-COVERAGE-001',
            'additional_request_limit':6,'cumulative_request_limit':8,'first_batch_value_confirmed':True,'template_changes_authorized':False,
            'automatic_asset_publication':False,'provider':'deepseek_official','model':'deepseek-flash','view_id':view['id'],'view_payload_hash':view['payload_hash']})
        self.output=self.directory/'coverage'
        self.envelope=freeze_coverage(self.corpus,self.learning,self.scope,self.preview,self.prior_state,self.output)
        self.envelope_path=self.output/'envelope.private.json'

    def test_six_new_positions_reuse_same_ledger_and_preserve_first_results(self):
        import asyncio
        before={j:self.learning.status(j) for j in self.prior_ids}
        state=prepare_coverage(self.corpus,self.learning,self.envelope_path)
        self.assertEqual(len(state['jobs']),6)
        asyncio.run(run_coverage(self.corpus,self.learning,self.envelope_path,CREDENTIALS))
        self.assertEqual(self.client.calls,8)
        self.assertEqual(self.learning.inspect()['dispatch_attempts'],8)
        self.assertEqual({j:self.learning.status(j) for j in self.prior_ids},before)
        self.assertTrue(all(r['max_dispatches']==2 for r in self._approvals()))
        asyncio.run(run_coverage(self.corpus,self.learning,self.envelope_path,CREDENTIALS))
        self.assertEqual(self.client.calls,8)

    def _approvals(self):
        with self.learning.connection() as db:return [dict(r) for r in db.execute('SELECT * FROM learning_approvals')]

    def test_changed_scope_or_child_wire_is_rejected_before_dispatch(self):
        prepare_coverage(self.corpus,self.learning,self.envelope_path)
        path=self.output/'pair-1/batch-1.wire-preview.private.json';path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(CorpusError):verify_prepared(self.corpus,self.learning,self.envelope_path)
        self.assertEqual(self.client.calls,2)

    def test_seventh_new_position_is_not_accepted(self):
        envelope=deepcopy(self.envelope);extra=deepcopy(envelope['positions'][-1]);extra['global_position']=9;envelope['positions'].append(extra)
        atomic_json(self.envelope_path,envelope)
        with self.assertRaisesRegex(CorpusError,'envelope_invalid'):validate_envelope(self.corpus,self.learning,self.envelope_path)
        self.assertEqual(self.client.calls,2)

    def test_value_gate_cannot_be_removed_from_author_scope(self):
        scope=json.loads(self.scope.read_text(encoding='utf-8'));scope['first_batch_value_confirmed']=False;atomic_json(self.scope,scope)
        with self.assertRaises(CorpusError):validate_envelope(self.corpus,self.learning,self.envelope_path)

    def test_pair_reports_keep_distinct_receipts_without_replacing_old_study(self):
        import asyncio
        state=prepare_coverage(self.corpus,self.learning,self.envelope_path)
        asyncio.run(run_coverage(self.corpus,self.learning,self.envelope_path,CREDENTIALS))
        result=export_coverage(self.corpus,self.learning,self.envelope_path,self.directory/'report')
        self.assertEqual(result['new_positions'],6);self.assertEqual(result['dispatch_attempts'],6)
        self.assertEqual(result['proposals'],6)
        for pair in (1,2,3):
            self.assertTrue((self.directory/f'report/pair-{pair}/batch-1.response.private.json').exists())
            self.assertTrue((self.directory/f'report/pair-{pair}/batch-2.response.private.json').exists())

    def test_unknown_response_stops_remaining_new_positions(self):
        import asyncio
        state=prepare_coverage(self.corpus,self.learning,self.envelope_path)
        self.client.mode='unknown'
        results=asyncio.run(run_coverage(self.corpus,self.learning,self.envelope_path,CREDENTIALS))
        self.assertEqual(len(results),1);self.assertEqual(self.client.calls,3)
        self.assertEqual(sum(self.learning.status(j['job_id'])['state']=='prepared' for j in state['jobs']),5)
        asyncio.run(run_coverage(self.corpus,self.learning,self.envelope_path,CREDENTIALS))
        self.assertEqual(self.client.calls,3)

    def test_three_new_provider_failures_stop_and_history_is_not_replayed(self):
        import asyncio
        state=prepare_coverage(self.corpus,self.learning,self.envelope_path)
        self.client.mode='http_failure'
        result=asyncio.run(run_coverage(self.corpus,self.learning,self.envelope_path,CREDENTIALS))
        self.assertEqual(len(result),3);self.assertEqual(self.client.calls,5)
        self.client.mode='success'
        asyncio.run(run_coverage(self.corpus,self.learning,self.envelope_path,CREDENTIALS))
        self.assertEqual(self.client.calls,8)
        self.assertEqual(sum(self.learning.status(j['job_id'])['state']=='completed' for j in state['jobs']),3)


if __name__=='__main__':unittest.main()
