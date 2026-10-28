"""Archive search isolation, full sources, cache races and read-only windows."""
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.archive import ArchiveService
from persona_runtime.archive_corpus import capture
from persona_runtime.archive_query import Budget, validate, source_epoch, normal
from persona_runtime.archive_search import ArchiveSearchService
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService
from tests.m71_fixtures import seed_fixture,seed_turn,insert
from scripts.evaluate_m71 import evaluate


class SearchTests(unittest.TestCase):
    def setUp(self):
        base=RuntimePaths.from_environment().home/'tmp';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='m71-test-',dir=base);self.addCleanup(self.temp.cleanup)
        self.paths=RuntimePaths(Path(self.temp.name));self.store=Store(self.paths);self.store.initialize()
        self.pid,self.sid,self.data=seed_fixture(self.store);self.api=ArchiveSearchService(self.store)
        self.args={'persona_id':self.pid,'channel':'desktop'}

    def search(self,text='青禾书店',**extra):
        return self.api.search(**(self.args|{'query':text}|extra))

    def test_fixed_80_qrels_and_read_only_authority(self):
        with self.store.connection() as db:before=list(db.iterdump())
        report=evaluate(self.store,self.pid,self.data)
        self.assertEqual(report['passed'],80,report)
        self.assertEqual(report['chinese_macro_recall_at_5'],1)
        self.assertEqual(report['chinese_hit_at_5'],1)
        self.assertEqual(report['chinese_mrr_at_5'],1)
        with self.store.connection() as db:self.assertEqual(before,list(db.iterdump()))

    def test_bulk_projection_equals_canonical_complete_groups(self):
        corpus=capture(self.store,validate(self.args|{'query':'书店'}),Budget())
        exact=ArchiveService(self.store)
        for group in corpus['groups']:
            with self.subTest(turn=group['turn_id']):
                self.assertEqual(group,exact.get(**self.args,target={'kind':'turn','id':group['turn_id']}))

    def test_invalid_queries_and_literal_operators(self):
        for values in ({'query':''},{'query':'x'*257},{'query':'\x00'},{'query':' '.join(f't{i}' for i in range(33))},
                       {'start':'2026-10-20'},{'start':'2026-10-22T00:00:00Z','end':'2026-10-21T00:00:00Z'},
                       {'local_date':'2026-02-30'},{'local_date':'2026-10-20','end':'2026-10-21T00:00:00Z'},
                       {'audit':True},{'limit':True},{'window':'everything'},{'cursor':'next'}):
            with self.subTest(values=values):self.assertEqual(self.search(**values)['status'],'invalid_query')
        for query in ('OR','x\" OR \"x','SELECT * FROM events','<script>'):
            with self.subTest(query=query):self.assertEqual(self.search(query)['status'],'no_matches')

    def test_phrase_terms_and_unicode_preserve_original(self):
        self.assertEqual({g['turn_id'] for g in self.search('阅读角 预算')['groups']},{'spread'})
        self.assertEqual(self.search('阅读角 预算',match_mode='phrase')['status'],'no_matches')
        original=self.search('abc')['groups'][0]['members'][0]['text']
        self.assertTrue(original.startswith('ＡＢＣ'))
        self.assertEqual(self.search('星空🙂')['groups'][0]['turn_id'],'emoji')
        self.assertEqual(self.search('Résumé')['groups'][0]['turn_id'],'emoji')

    def test_cross_scope_ranking_and_unknown_persona(self):
        first=self.search('预算')
        other=RuntimeService(self.store).create_persona(generic_persona())['id']
        self.assertEqual(self.search('预算',persona_id=other)['status'],'no_matches')
        self.assertEqual(self.search('预算',persona_id='missing')['status'],'unavailable')
        self.assertEqual([g['turn_id'] for g in first['groups']], [g['turn_id'] for g in self.search('预算')['groups']])
        self.assertEqual(self.search('青禾书店',channel='qq',bot='bot',peer='peer')['status'],'no_matches')

    def test_unowned_session_is_not_readable(self):
        self.assertEqual(self.search(session_id='not-owned')['status'],'unavailable')
        self.assertEqual(self.search(session_id=self.sid)['status'],'ok')

    def test_recent_50_logical_turns_and_empty_preview(self):
        r=self.search('window_00',window='recent_50');self.assertEqual(r['status'],'no_matches')
        self.assertEqual(r['window_manifest']['turn_count'],50)
        self.assertEqual(r['window_manifest']['turn_ids'][0],'window50')
        preview=self.api.preview(**self.args)
        self.assertEqual(preview['status'],'ok');self.assertEqual(preview['groups'],[])
        self.assertFalse(preview['total_input_checked']);self.assertFalse(preview['creates_request'])

    def test_working_set_targets_mapping_and_window_change(self):
        targets=[{'kind':'event','id':'book-e1'},{'kind':'event','id':'book-e3'}]
        r=self.api.preview(**self.args,window='all',targets=targets)
        self.assertEqual(r['status'],'ok');self.assertEqual(len(r['groups']),1)
        self.assertEqual(r['groups'][0]['targets'],targets)
        self.assertEqual(len(r['groups'][0]['members']),3)
        self.assertEqual(self.api.preview(**self.args,targets=targets)['status'],'unavailable')
        self.assertEqual(self.api.preview(**self.args,window='all')['status'],'invalid_query')
        self.assertEqual(self.api.preview(**self.args,targets=targets*11)['status'],'invalid_query')

    def test_orphan_audit_is_not_search_or_preview_eligible(self):
        exact=ArchiveService(self.store).get(**self.args,target={'kind':'event','id':'orphan0'},audit=True)
        self.assertTrue(exact['audit_only']);self.assertFalse(exact['usable'])
        self.assertEqual(self.search('orphan_0')['status'],'no_matches')
        self.assertEqual(self.api.preview(**self.args,window='all',targets=[{'kind':'event','id':'orphan0'}])['status'],'unavailable')

    def test_retract_nonfirst_invalidates_cached_full_turn(self):
        self.assertEqual(self.search()['status'],'ok')
        with self.store.connection(write=True) as db:
            before=db.execute('SELECT max(seq) FROM events').fetchone()[0]
            insert(db,'source_exclusions',dict(event_id='book-e2',persona_id=self.pid,user_id=self.args_user(),reason='test',created_at='2026-10-28T00:00:00Z'))
            self.assertEqual(before,db.execute('SELECT max(seq) FROM events').fetchone()[0])
        self.assertEqual(self.search()['status'],'no_matches')
        self.assertEqual(self.api.preview(**self.args,window='all',targets=[{'kind':'turn','id':'book'}])['status'],'unavailable')

    @staticmethod
    def args_user():
        from persona_runtime.models import LOCAL_USER_ID
        return LOCAL_USER_ID

    def test_race_between_snapshots_including_zero_hit_is_stale(self):
        real=capture;count=0
        def racing(store,spec,budget):
            nonlocal count
            count+=1
            if count==2:
                with store.connection(write=True) as db:seed_turn(db,self.pid,self.sid,'new-race',['未来新增标识'])
            return real(store,spec,budget)
        with patch('persona_runtime.archive_search.capture',side_effect=racing):
            self.assertEqual(self.search('未来新增标识')['status'],'index_stale')
        self.assertEqual(self.search('未来新增标识')['status'],'ok')

    def test_time_axes_do_not_substitute_missing_metadata(self):
        self.assertEqual(self.search('青禾书店',time_axis='platform',local_date='2026-10-20')['status'],'coverage_gap')
        self.assertEqual(self.search('青禾书店',time_axis='received',local_date='2026-10-20')['status'],'ok')
        self.assertEqual(self.search('紫色围巾',time_axis='received',local_date='2026-10-20')['status'],'no_matches')
        self.assertEqual(source_epoch('1792468800','platform'),1792468800)
        self.assertIsNone(source_epoch('NaN','platform'))

    def test_time_offsets_half_open_and_date_overflow(self):
        from persona_runtime.archive_query import epoch
        selected=validate(self.args|{'query':'书店','local_date':'2026-10-20'})
        self.assertEqual(epoch(selected['start']),epoch('2026-10-19T16:00:00Z'))
        self.assertEqual(epoch(selected['end']),epoch('2026-10-20T16:00:00Z'))
        self.assertEqual(self.search('青禾书店',start='2026-10-20T04:00:00Z',end='2026-10-20T04:00:01Z')['status'],'ok')
        self.assertEqual(self.search('青禾书店',end='2026-10-20T04:00:00Z')['status'],'no_matches')
        # Offset, rather than host timezone or daylight-saving assumptions,
        # determines an explicitly supplied instant.
        self.assertEqual(epoch('2026-11-01T01:30:00-04:00')+3600,epoch('2026-11-01T01:30:00-05:00'))
        self.assertEqual(self.search(local_date='9999-12-31')['status'],'invalid_query')

    def test_twenty_members_count_as_one_complete_working_set_turn(self):
        service=RuntimeService(self.store)
        pid=service.create_persona(generic_persona())['id'];service.use_persona(pid);sid=service.create_session()['id']
        with self.store.connection(write=True) as db:
            seed_turn(db,pid,sid,'twenty',[f'人工独立成员 member_{i:02d}' for i in range(20)])
        args={'persona_id':pid,'channel':'desktop'}
        result=self.api.search(**args,query='member_19',window='recent_50')
        self.assertEqual(result['status'],'ok');self.assertEqual(result['window_manifest']['turn_count'],1)
        self.assertEqual(len(result['groups'][0]['members']),20)
        self.assertEqual(result['groups'][0]['matched_targets'],[{'kind':'event','id':'twenty-e20'}])

    def test_cache_cannot_alias_authority_and_wrong_tokenizer_is_unavailable(self):
        import os
        self.search();self.api.index.path.unlink()
        before=self.store.path.read_bytes()
        os.link(self.store.path,self.api.index.path)
        try:
            self.assertEqual(self.search()['status'],'unavailable')
            self.assertEqual(self.api.status(**self.args)['status'],'unavailable')
            self.assertEqual(before,self.store.path.read_bytes())
        finally:self.api.index.path.unlink()
        self.search()
        with self.api.index.connection() as (db,_):db.execute("UPDATE meta SET tokenizer='incompatible-test-version'")
        self.assertEqual(self.search()['status'],'unavailable')

    def test_group_budget_and_document_budget_fail_explicitly(self):
        with self.store.connection(write=True) as db:seed_turn(db,self.pid,self.sid,'large',['巨大资料'+'长'*12500])
        self.assertEqual(self.search('巨大资料')['status'],'query_budget_exceeded')
        preview=self.api.preview(**self.args,window='all',targets=[{'kind':'turn','id':'large'}])
        self.assertEqual(preview['status'],'query_budget_exceeded');self.assertEqual(preview['groups'],[])
        with patch('persona_runtime.archive_corpus.MAX_DOCUMENTS',2):
            self.assertEqual(self.search()['status'],'query_budget_exceeded')
        with patch('persona_runtime.archive_query.MAX_SECONDS',0):
            self.assertEqual(self.search()['status'],'query_budget_exceeded')

    def test_cache_generation_cas_operations_and_cache_deletion(self):
        state=self.api.status(**self.args)
        self.assertIsNone(state['cache_instance_id'])
        params=self.args|{k:state[k] for k in ('cache_instance_id','generation')}
        params['expected_generation']=params.pop('generation');params['operation_id']='test-rebuild'
        first=self.api.rebuild(**params);self.assertEqual(first['status'],'ok')
        self.assertEqual(first,self.api.rebuild(**params))
        self.assertEqual(self.api.rebuild(**(params|{'expected_generation':99}))['status'],'operation_conflict')
        self.assertEqual(self.api.rebuild(**(params|{'operation_id':'new-operation'}))['status'],'cache_generation_changed')
        self.api.index.path.unlink()
        old=params|{'operation_id':'after-delete','cache_instance_id':first['cache_instance_id'],'expected_generation':1}
        self.assertEqual(self.api.rebuild(**old)['status'],'cache_generation_changed')
        self.assertEqual(self.search()['status'],'ok')

    def test_corrupt_cache_requires_explicit_rebuild(self):
        self.search();self.api.index.path.write_bytes(b'not a sqlite cache')
        self.assertEqual(self.search()['status'],'unavailable')
        status=self.api.status(**self.args);self.assertEqual(status['state'],'corrupt')
        r=self.api.rebuild(**self.args,cache_instance_id=None,expected_generation=0,operation_id='repair')
        self.assertEqual(r['status'],'ok');self.assertEqual(self.search()['status'],'ok')

    def test_modified_cached_body_is_not_evidence_and_missing_rows_are_not_no_matches(self):
        self.search()
        with self.api.index.connection() as (db,_):
            row=db.execute("SELECT rowid,data_json FROM documents WHERE document_id='book-e1'").fetchone()
            value=json.loads(row['data_json']);value['text']='不可信缓存正文'
            db.execute('UPDATE documents SET data_json=? WHERE rowid=?',(json.dumps(value),row['rowid']))
        result=self.search();self.assertEqual(result['status'],'index_stale')
        self.assertNotIn('不可信缓存正文',json.dumps(result))
        with self.api.index.connection() as (db,_):db.execute("DELETE FROM documents WHERE document_id='book-e1'")
        self.assertEqual(self.search()['status'],'unavailable')

    def test_cache_lock_conflict_and_sqlite_write_rollback(self):
        self.search();before=self.api.status(**self.args)
        with self.api.index.locked():self.assertEqual(self.search()['status'],'cache_busy')
        spec=validate(self.args|{'query':'青禾书店'});corpus=capture(self.store,spec,Budget())
        with self.api.index.locked(),patch('persona_runtime.archive_index.encoded_terms',side_effect=RuntimeError('injected fault')):
            with self.assertRaises(RuntimeError):self.api.index.synchronize(corpus,spec,Budget(),force=True)
        self.assertEqual(self.api.status(**self.args),before)
        self.assertEqual(self.search()['status'],'ok')
        with patch.object(self.api.index,'synchronize',side_effect=sqlite3.OperationalError('database or disk is full')):
            self.assertEqual(self.search()['status'],'unavailable')

    def test_fts_unavailable_uses_explicit_literal_fallback(self):
        self.search()
        with self.api.index.connection() as (db,_):db.execute('UPDATE meta SET fts=0')
        r=self.search('青禾书店');self.assertTrue(r['degraded']);self.assertEqual(r['path'],'literal_substring')
        self.assertEqual(r['status'],'ok')

    def test_partitions_do_not_share_generations(self):
        a=self.search();b=self.search('window_50',window='recent_50')
        self.assertEqual(a['generation'],1);self.assertEqual(b['generation'],1)
        self.assertNotEqual(a['source_digest'],b['source_digest'])
        self.assertEqual(self.api.status(**self.args)['freshness'],'unknown')

    def test_window_sizes_zero_one_49_50_51(self):
        service=RuntimeService(self.store)
        for count in (0,1,49,50,51):
            pid=service.create_persona(generic_persona())['id'];service.use_persona(pid);sid=service.create_session()['id']
            with self.store.connection(write=True) as db:
                for i in range(count):seed_turn(db,pid,sid,f'size-{count}-{i}',[f'人工轮次{i}'])
            result=self.api.preview(persona_id=pid,channel='desktop')
            self.assertEqual(result['status'],'ok');self.assertEqual(result['window_manifest']['turn_count'],min(50,count))

    def test_cache_never_contains_model_configuration_or_profile_snapshot(self):
        with self.store.connection(write=True) as db:
            db.execute('UPDATE turns SET model_snapshot=?',('{"encrypted_key":"CONFIGURATION-SECRET-MARKER"}',))
        result=self.search()
        self.assertNotIn('CONFIGURATION-SECRET-MARKER',json.dumps(result))
        self.assertNotIn(b'CONFIGURATION-SECRET-MARKER',self.api.index.path.read_bytes())

    def test_candidate_limit_and_source_character_limit_are_reported(self):
        with self.store.connection(write=True) as db:
            for i in range(210):seed_turn(db,self.pid,self.sid,f'many-{i}',['大量共同命中'])
        result=self.search('大量共同命中')
        self.assertEqual(result['status'],'ok');self.assertTrue(result['coverage']['candidate_limit'])
        with patch('persona_runtime.archive_corpus.MAX_SOURCE_CHARS',1):
            self.assertEqual(self.search()['status'],'query_budget_exceeded')

    def test_qq_actual_parts_time_and_old_reference_snapshot(self):
        from persona_runtime.delivery import DeliveryService
        from persona_runtime.archive import scope
        from persona_runtime.references import capture as bind_reference,prepare
        day='2026-10-20T04:00:00+00:00'
        with self.store.connection(write=True) as db:
            seed_turn(db,self.pid,self.sid,'qq-sent',['QQ三条之一','预算蓝色'],channel='qq',bot='456',peer='123',reply='原语义回复')
            insert(db,'attempts',dict(id='fixture-attempt',turn_id='qq-sent',started_epoch=0,status='completed',model_snapshot='{}'))
            insert(db,'reply_batches',dict(id='fixture-reply',turn_id='qq-sent',attempt_id='fixture-attempt',persona_id=self.pid,
                user_id=self.args_user(),schema_version=1,mode='context_structured',raw_fingerprint='fixture',segment_count=2,total_chars=8,created_at=day))
            for index,text in enumerate(('**实际已发送**','后来确认片段'),1):
                insert(db,'reply_segments',dict(batch_id='fixture-reply',persona_id=self.pid,user_id=self.args_user(),segment_index=index,text=text,format='markdown'))
            turn=dict(db.execute("SELECT * FROM turns WHERE id='qq-sent'").fetchone())
            DeliveryService(self.store).create_for_batch(db,'fixture-reply',turn)
            parts=list(db.execute("SELECT id FROM reply_delivery_parts WHERE turn_id='qq-sent' ORDER BY part_index"))
            db.execute("UPDATE reply_delivery_parts SET state='sent',platform_message_id='fixture-msg',sent_at=? WHERE id=?",(day,parts[0][0]))
            db.execute("UPDATE reply_delivery_parts SET state='uncertain' WHERE id=?",(parts[1][0],))
            seed_turn(db,self.pid,self.sid,'qq-quote',['引用测试','选择另一个成员'],channel='qq',bot='456',peer='123')
            bind_reference(db,'qq-quote-e1',scope(self.pid,'qq','456','123'),{'kind':'part','id':parts[0][0]})
            bind_reference(db,'qq-quote-e2',scope(self.pid,'qq','456','123'),{'kind':'event','id':'qq-sent-e2'})
            before=db.execute("SELECT binding_json FROM input_references WHERE event_id='qq-quote-e1'").fetchone()[0]
            choices=[tuple(r) for r in db.execute('SELECT event_id,binding_json FROM input_references ORDER BY event_id')]
        args={'persona_id':self.pid,'channel':'qq','bot':'456','peer':'123'}
        result=self.api.search(**args,query='实际已发送',time_axis='sent',local_date='2026-10-20')
        self.assertEqual(result['status'],'ok');self.assertEqual(result['groups'][0]['replies'][0]['text'],'实际已发送')
        self.assertEqual(self.api.search(**args,query='原语义回复')['status'],'no_matches')
        self.assertEqual(self.api.search(**args,query='后来确认片段')['status'],'no_matches')
        self.assertEqual(self.api.search(**args,query='QQ三条之一',time_axis='platform',local_date='2026-10-20')['status'],'ok')
        self.assertEqual(self.api.search(**(args|{'peer':'other'}),query='实际已发送')['status'],'no_matches')
        self.assertEqual(self.api.search(**(args|{'bot':'other'}),query='实际已发送')['status'],'no_matches')
        with self.store.connection(write=True) as db:
            db.execute("UPDATE reply_delivery_parts SET state='sent',sent_at=? WHERE id=?",(day,parts[1][0]))
        self.assertEqual(self.api.search(**args,query='后来确认片段')['status'],'ok')
        with self.store.connection() as db:
            block,_=prepare(db,dict(db.execute("SELECT * FROM turns WHERE id='qq-quote'").fetchone()))
            self.assertNotIn('后来确认片段',block)
            self.assertEqual(before,db.execute("SELECT binding_json FROM input_references WHERE event_id='qq-quote-e1'").fetchone()[0])
            self.assertEqual(choices,[tuple(r) for r in db.execute('SELECT event_id,binding_json FROM input_references ORDER BY event_id')])
