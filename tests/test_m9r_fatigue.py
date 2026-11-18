"""Preflight regression for returning to an older saved topic within one conversation."""
import unittest
import json
from unittest.mock import patch
from persona_runtime.knowledge_native_memory import previous
from tests import test_m4 as fixture


class SavedTopicSelection(unittest.TestCase):
    setUp=fixture.M4Tests.setUp

    def records(self):
        def record(key,title,notes):
            return {'root_turn_id':key,'persona_id':self.persona['id'],'channel':'desktop','session_id':'continuous',
                'sources':[{'id':key,'url':'https://example.org/'+key,'title':title,'summary':notes}],
                'organized':{'notes':notes,'source_ids':[key]}}
        return [record('recent','Python虚拟环境','虚拟环境激活与依赖安装是不同操作。'),
                record('older','空气炸锅杏鲍菇做法','杏鲍菇切厚片，少量油，分批烤制。')]

    def test_older_relevant_topic_beats_newer_unrelated_topic_in_same_session(self):
        turn={'id':'current','persona_id':self.persona['id'],'channel':'desktop','session_id':'continuous'}
        with self.store.connection() as db,patch('persona_runtime.knowledge_native_memory.candidates',return_value=self.records()):
            found=previous(db,turn,'回到前面的空气炸锅杏鲍菇，厚片能堆起来烤吗？')
        self.assertEqual(found['sources'][0]['id'],'older')

    def test_without_topic_terms_continue_with_most_recent_material(self):
        turn={'id':'current','persona_id':self.persona['id'],'channel':'desktop','session_id':'continuous'}
        with self.store.connection() as db,patch('persona_runtime.knowledge_native_memory.candidates',return_value=self.records()):
            found=previous(db,turn,'那接着说吧')
        self.assertEqual(found['sources'][0]['id'],'recent')

    def test_inherited_source_does_not_relabel_unrelated_new_notes_as_old_topic(self):
        records=self.records()
        records[0]['sources']+=records[1]['sources']
        turn={'id':'current','persona_id':self.persona['id'],'channel':'desktop','session_id':'continuous'}
        with self.store.connection() as db,patch('persona_runtime.knowledge_native_memory.candidates',return_value=records):
            found=previous(db,turn,'空气炸锅杏鲍菇厚片怎么分批烤？')
        self.assertEqual(found['organized']['notes'],records[1]['organized']['notes'])
        self.assertEqual([s['id'] for s in found['sources']],['older'])

    def test_other_session_needs_a_topic_or_exact_source_link(self):
        records=self.records()
        records[0]['sources'][0]['content']='这个问题，换种方式又不行，不打扰。'
        turn={'id':'current','persona_id':self.persona['id'],'channel':'desktop','session_id':'other'}
        with self.store.connection() as db,patch('persona_runtime.knowledge_native_memory.candidates',return_value=records):
            self.assertFalse(previous(db,turn,'这个问题不大，我又不打网球')['sources'])
            self.assertFalse(previous(db,turn,'请看 https://example.org/older/?other=1')['sources'])
            self.assertEqual(previous(db,turn,'再看 https://example.org/older#section')['sources'][0]['id'],'older')
            self.assertEqual(previous(db,turn,'空气炸锅杏鲍菇还能分批烤吗')['sources'][0]['id'],'older')


class DecisionBudget(unittest.TestCase):
    def test_large_source_history_is_only_a_bounded_routing_sketch(self):
        from persona_runtime.knowledge_native import decision_materials
        from persona_runtime.knowledge_web_decision import request,NATIVE_DECISION_PROFILE
        sources=[{'url':'https://example.org/'+str(i)+'?'+'x'*1500,'title':'公开天气资料'*40,'date':None} for i in range(177)]
        notes={'kind':'model_synthesis','notes':'已经查回的公开结论'*1000}
        hints,sketch=decision_materials(sources,notes)
        self.assertLessEqual(len(hints),8);self.assertLessEqual(len(sketch),1200)
        body=request({'name':'jev-fixture','max_input_chars':24000},{'question':'就用已有资料收个尾',
            'decision_profile':NATIVE_DECISION_PROFILE,'available_sources':hints,'cached_public_notes':sketch})
        self.assertLess(len(json.dumps(body,ensure_ascii=False)),24000)
        self.assertGreater(len(sources[0]['url']),512)

    def test_user_correction_is_not_forwarded_as_a_public_model_note(self):
        from persona_runtime.knowledge_native import decision_materials
        _,sketch=decision_materials([],{'kind':'user_correction','notes':'仅供本地使用的更正'})
        self.assertEqual(sketch,'')


if __name__=='__main__':unittest.main()
