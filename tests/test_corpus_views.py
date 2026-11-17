"""E.L2 boundary tests use identifiers and bytes, never authored dialogue."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_views import build_dataset, redact_text, verify_redactions, validate_dataset
from persona_runtime.corpus_ingest import MoectCorpusStore
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_dataset_export import export_dataset
from tests.test_corpus_moect import document, pair, message


KEY = b'K'*32
IDENTITIES = {'a':{'source_id':'SOURCE-A','names':['NAME-A']},'b':{'source_id':'SOURCE-B','names':['NAME-B']}}


def source_rows(count=12):
    result=[]
    for n in range(count):
        body='ID-'+str(n).zfill(3)
        result.append({'id':'M-'+str(n),'ordinal':n,'created_epoch':1773000000+n*2000,
                       'text':body,'text_hash':digest(body.encode()),'role':'self' if n%2==0 else 'peer',
                       'kind':'text','target_candidate':1})
    return result


def text_update(rows,index,text):
    rows[index].update(text=text,text_hash=digest(text.encode('utf-8')))


def build(rows=None,relations=None,changes=None):
    return build_dataset(rows or source_rows(),relations or [],IDENTITIES,key=KEY,
                         namespace='DATASET-001',parse_id='PARSE-001',changes=changes)


class PrivacyAndSplits(unittest.TestCase):
    def test_redactions_preserve_every_unmatched_character_and_roundtrip_spans(self):
        original='ID-001\r\n  13800000001，u001@example.invalid！https://example.invalid/a?phone=13800000001。\nNAME-A / wxid_SOURCE_001'
        text,maps,flags=redact_text(original,key=KEY,namespace='DATASET-001',identities=IDENTITIES)
        verify_redactions(original,text,maps)
        self.assertEqual([r['kind'] for r in maps],['phone','email','url','person','account'])
        self.assertIn('\r\n  ',text)
        self.assertTrue(text.endswith(' / '+maps[-1]['replacement']))
        self.assertIn('！',text)
        self.assertIn('。\n',text)
        self.assertNotIn('13800000001',text)
        self.assertEqual(flags,[])

    def test_stable_tokens_are_scoped_to_dataset_and_local_secret(self):
        def token(key,namespace):return redact_text('13800000001',key=key,namespace=namespace,identities={})[0]
        self.assertEqual(token(KEY,'A'),token(KEY,'A'))
        self.assertNotEqual(token(KEY,'A'),token(b'Z'*32,'A'))
        self.assertNotEqual(token(KEY,'A'),token(KEY,'B'))

    def test_unmatched_punctuation_and_spacing_are_not_normalized(self):
        original='ID-001\r\n  ID-002！？？\U0001f539'
        text,mapping,flags=redact_text(original,key=KEY,namespace='A',identities={})
        self.assertEqual((text,mapping,flags),(original,[],[]))

    def test_short_names_and_uncertain_identifiers_require_review(self):
        identity={'a':{'source_id':'SOURCE-A','names':['Q']}}
        text,mapping,flags=redact_text('Q / 123456789012345678',key=KEY,namespace='A',identities=identity)
        self.assertIn('short_name_match_pending',flags)
        self.assertIn('identifier_pattern_unconfirmed',flags)
        self.assertTrue(text.startswith('Q / '))

    def test_single_character_name_does_not_match_inside_an_identifier_or_word(self):
        identities={'a':{'source_id':'SOURCE-A','names':['A']}}
        text,mapping,flags=redact_text('HOLDMARK-001 / DATA_001',key=KEY,namespace='A',identities=identities)
        self.assertEqual((text,mapping,flags),('HOLDMARK-001 / DATA_001',[],[]))

    def test_mapping_change_and_unmapped_rewrite_are_detected(self):
        original='ID-001 13800000001 ID-002'
        text,mapping,_=redact_text(original,key=KEY,namespace='A',identities={})
        with self.assertRaisesRegex(CorpusError,'unmapped_change'):
            verify_redactions(original,text+'ID-003',mapping)
        mapping[0]['source_start']+=1
        with self.assertRaises(CorpusError):verify_redactions(original,text,mapping)

    def test_three_partitions_are_deterministic_and_complete(self):
        first=build();second=build()
        self.assertEqual(first,second)
        self.assertTrue(first['summary']['three_way_split_available'])
        self.assertEqual(sum(v['messages'] for v in first['summary']['splits'].values()),12)
        self.assertEqual(len({r['message_id'] for r in first['messages']}),12)

    def test_cross_episode_reply_chain_is_kept_in_one_group_and_partition(self):
        rows=source_rows()
        edge={'source_id':rows[-1]['id'],'target_id':rows[0]['id'],'kind':'reply','state':'resolved'}
        result=build(rows,[edge])
        first,last=result['messages'][0],result['messages'][-1]
        self.assertEqual(first['split'],last['split'])
        fragments={f['id']:f for f in result['fragments']}
        self.assertEqual(fragments[first['fragment_id']]['group_id'],fragments[last['fragment_id']]['group_id'])

    def test_exact_distinctive_and_near_duplicate_targets_cannot_cross_partitions(self):
        rows=source_rows()
        body=''.join('TOKEN-'+str(n).zfill(3)+'|' for n in range(60))
        text_update(rows,0,body)
        text_update(rows,10,body)
        text_update(rows,11,body+'TAIL-001')
        result=build(rows)
        self.assertEqual(result['messages'][0]['split'],result['messages'][10]['split'])
        self.assertEqual(result['messages'][0]['split'],result['messages'][11]['split'])
        self.assertIn('near_target',result['summary']['group_edge_reasons'])

    def test_shared_short_text_remains_context_but_not_a_reused_holdout_reference(self):
        rows=source_rows()
        text_update(rows,0,'ID-SHARED')
        text_update(rows,11,'ID-SHARED')
        result=build(rows)
        self.assertEqual(result['messages'][0]['split'],'learn')
        self.assertEqual(result['messages'][11]['split'],'final_holdout')
        self.assertEqual(result['messages'][11]['text'],'ID-SHARED')
        self.assertFalse(result['messages'][11]['target_candidate'])
        self.assertIn('shared_short_text_context_only',result['messages'][11]['flags'])

    def test_nontext_excluded_and_unresolved_sources_do_not_become_targets(self):
        rows=source_rows()
        rows[2].update(kind='image',target_candidate=0)
        edge={'source_id':rows[3]['id'],'target_id':None,'kind':'reply','state':'unresolved'}
        result=build(rows,[edge],{'excluded_message_ids':[rows[4]['id']]})
        self.assertEqual(result['messages'][2]['text'],'')
        self.assertEqual(result['messages'][4]['text'],'')
        self.assertFalse(result['messages'][3]['target_candidate'])
        self.assertIn('unresolved_reference',result['messages'][3]['flags'])

    def test_corrupted_message_partition_is_rejected_by_independent_membership_check(self):
        rows=source_rows();result=build(rows)
        result['messages'][0]['split']='final_holdout'
        with self.assertRaisesRegex(CorpusError,'message_split_changed'):
            validate_dataset(result,rows,[])

    def test_recipe_source_and_duplicate_capacity_errors_stop_locally(self):
        for changes in [{'unknown':1},{'excluded_message_ids':None},{'near_jaccard':False},
                        {'excluded_message_ids':['ABSENT']},{'learn_percent':99,'dev_percent':2}]:
            with self.subTest(changes=changes),self.assertRaises(CorpusError):build(changes=changes)
        rows=source_rows(10)
        prefix=''.join('TOKEN-'+str(n).zfill(3)+'|' for n in range(50))
        for n in range(10):text_update(rows,n,prefix+'TAIL-'+str(n))
        with self.assertRaisesRegex(CorpusError,'candidate_capacity'):
            build(rows,changes={'max_duplicate_pairs':2})


class DatasetStorage(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pcar-views-',dir=os.environ.get('PCAR_CORPUS_TEST_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name).resolve()
        self.root=self.directory/'corpus'
        old=MoectCorpusStore(self.root);old.initialize()
        self.dataset=old.create_dataset('DATASET-001','CREATE-001')['id']
        raw,md=pair()
        saved=[];sources=[]
        for kind,value in [('json',raw),('md',md)]:
            path=self.directory/('source.'+kind);path.write_bytes(value)
            preview=old.preview(self.dataset,path,'PREVIEW-'+kind)
            saved.append(old.retain_original(preview['id'],preview['manifest_hash'],'COMMIT-'+kind)['id'])
            sources.append({'kind':kind,'sha256':digest(value)})
        grant=self.directory/'grant.json'
        grant.write_text(json.dumps({'version':'author-wechat-material-grant-v1','conversation_kind':'private',
            'training_input_authorized':True,'speaker_scope':'both','speaker_confirmation':'author_confirmed_both_as_human_expression_sources',
            'model_request_version_approved':False,'sources':sources}),encoding='utf-8')
        self.parse=old.parse_pair(self.dataset,*saved,grant,'PARSE-001')['id']
        self.store=DatasetCorpusStore(self.root)
        self.store.initialize()

    def test_migration_preserves_parse_and_keeps_a_version_two_backup(self):
        self.assertEqual(self.store.inspect()['schema'],3)
        self.assertEqual(self.store.verify_parse(self.parse)['records_checked'],5)
        backups=list((self.root/'backups').glob('before-views-v3-*.sqlite3'))
        self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM corpus_messages').fetchone()[0],5)
        self.store.initialize()
        self.assertEqual(len(list((self.root/'backups').glob('*.sqlite3'))),1)

    def test_snapshot_is_idempotent_and_never_enables_model_input(self):
        first=self.store.build_version(self.parse,'BUILD-001')
        self.assertEqual(self.store.build_version(self.parse,'BUILD-001'),first)
        self.assertEqual(self.store.build_version(self.parse,'BUILD-002')['id'],first['id'])
        self.assertFalse(first['model_input_approved'])
        self.assertTrue(self.store.verify_version(first['id'])['passed'])

    def test_snapshot_tables_are_append_only(self):
        view=self.store.build_version(self.parse,'BUILD-001')
        for query in ['UPDATE corpus_view_messages SET target_candidate=1','DELETE FROM corpus_view_groups',
                      'UPDATE corpus_dataset_versions SET model_input_approved=1']:
            with self.subTest(query=query),self.assertRaises(sqlite3.IntegrityError):
                with self.store.connection(write=True) as db:db.execute(query)
        self.assertTrue(self.store.verify_version(view['id'])['passed'])

    def test_source_tampering_and_revocation_disable_existing_view_access(self):
        view=self.store.build_version(self.parse,'BUILD-001')
        self.store.revoke_dataset(self.dataset,1,'REVOKE-001')
        with self.assertRaisesRegex(CorpusError,'dataset_revoked'):self.store.load_version(view['id'])
        with self.assertRaisesRegex(CorpusError,'dataset_revoked'):self.store.build_version(self.parse,'BUILD-002')

    def test_changed_source_body_blocks_loading_an_existing_view(self):
        view=self.store.build_version(self.parse,'BUILD-001')
        with self.store.connection(write=True) as db:
            db.execute('UPDATE corpus_messages SET text=\'ID-CHANGED\' WHERE parse_id=? AND ordinal=0',(self.parse,))
        with self.assertRaisesRegex(CorpusError,'message_evidence_changed'):
            self.store.load_version(view['id'])

    def test_failed_migration_preserves_version_two_readability(self):
        root=self.directory/'migration-case'
        old=MoectCorpusStore(root);old.initialize()
        old.create_dataset('DATASET-002','CREATE-002')
        with patch('persona_runtime.corpus_dataset.EXTENSION','CREATE TABLE PARTIAL(id TEXT);\nINVALID;\n'):
            with self.assertRaises(sqlite3.Error):DatasetCorpusStore(root).initialize()
        self.assertEqual(old.inspect()['schema'],2)
        self.assertEqual(len(old.inspect()['datasets']),1)
        with old.connection() as db:
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='PARTIAL'").fetchone())

    def test_failed_snapshot_commit_has_no_partial_version(self):
        original=self.store._save
        def fail(db,operation_id,action,request,result):
            if action=='dataset_view_build':raise OSError('STATE-001')
            return original(db,operation_id,action,request,result)
        with patch.object(self.store,'_save',side_effect=fail):
            with self.assertRaises(OSError):self.store.build_version(self.parse,'BUILD-001')
        self.assertEqual(self.store.inspect()['dataset_versions'],0)
        view=self.store.build_version(self.parse,'BUILD-001')
        self.assertTrue(self.store.verify_version(view['id'])['passed'])

    def test_export_keeps_audit_separate_from_data_and_escapes_markup(self):
        view=self.store.build_version(self.parse,'BUILD-001')
        output=self.directory/'review'
        result=export_dataset(self.store,view['id'],output)
        self.assertTrue(result['final_holdout_sealed'])
        data=[json.loads(line) for line in (output/'learn.fragments.jsonl').read_text(encoding='utf-8').splitlines()]
        text=json.dumps(data)
        self.assertNotIn('original_hash',text)
        self.assertNotIn('source_text_hash',text)
        self.assertNotIn('redactions',text)
        self.assertFalse(data[0]['model_input_approved'])
        html=(output/'fragments-1.html').read_text(encoding='utf-8')
        self.assertNotIn('<script',html)
        self.assertIn('&lt;script&gt;',html)
        with self.assertRaisesRegex(CorpusError,'empty_target_required'):
            export_dataset(self.store,view['id'],output)

    def test_full_view_build_and_export_work_without_network(self):
        with patch('socket.socket',side_effect=AssertionError('NETWORK-001')):
            view=self.store.build_version(self.parse,'BUILD-001')
            self.store.verify_version(view['id'])
            export_dataset(self.store,view['id'],self.directory/'review')

    def test_final_holdout_text_is_not_written_to_default_review_pages(self):
        data=document();records=[]
        for n in range(1,13):
            row=message(n,own=n%2==0,body='HOLDMARK-'+str(n).zfill(3))
            row['create_time']+=n*2000
            row['time_str']=datetime.fromtimestamp(row['create_time'],timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            records.append(row)
        data.update(messages=records,count=len(records))
        raw,md=pair(data);imports=[];sources=[]
        for kind,value in [('json',raw),('md',md)]:
            path=self.directory/('extra.'+kind);path.write_bytes(value)
            preview=self.store.preview(self.dataset,path,'EXTRA-PREVIEW-'+kind)
            imports.append(self.store.retain_original(preview['id'],preview['manifest_hash'],'EXTRA-COMMIT-'+kind)['id'])
            sources.append({'kind':kind,'sha256':digest(value)})
        grant=self.directory/'extra-grant.json'
        grant.write_text(json.dumps({'version':'author-wechat-material-grant-v1','conversation_kind':'private',
            'training_input_authorized':True,'speaker_scope':'both','speaker_confirmation':'author_confirmed_both_as_human_expression_sources',
            'model_request_version_approved':False,'sources':sources}),encoding='utf-8')
        parsed=self.store.parse_pair(self.dataset,*imports,grant,'EXTRA-PARSE')['id']
        view=self.store.build_version(parsed,'EXTRA-BUILD')
        payload=self.store.load_version(view['id'])
        self.assertTrue(payload['summary']['three_way_split_available'])
        output=self.directory/'sealed-review'
        export_dataset(self.store,view['id'],output)
        html=''.join(p.read_text(encoding='utf-8') for p in output.glob('*.html'))
        final=[m['text'] for m in payload['messages'] if m['split']=='final_holdout']
        self.assertTrue(final)
        for text in final:self.assertNotIn(text,html)
        self.assertGreater((output/'final_holdout.fragments.jsonl').stat().st_size,0)


if __name__=='__main__':unittest.main()
