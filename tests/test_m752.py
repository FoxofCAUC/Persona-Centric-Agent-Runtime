"""Internal retrieval contracts; no live chat, QQ or paid models."""
from copy import deepcopy
from pathlib import Path
import json
import unittest
from unittest.mock import patch

import test_m751 as prior
from persona_runtime.character import empty_character
from persona_runtime.character_context import select_context, render_parts
from persona_runtime.character_embedding import SemanticFailure, digest, file_hash
from persona_runtime.character_retrieval import (CharacterCandidateRetriever, rank_candidates,
    assemble, literal_match, dependency_group, config_checked, ALGORITHM)
from persona_runtime.models import RuntimeErrorBase, generic_persona


def config():
    return {'algorithm':ALGORITHM,'rrf_k':60,'model_fingerprint':'test-space-v1',
            'thresholds':{'lore':.6,'example':.7}}


def candidate(row,kind='lore',score=.8):
    return {'kind':kind,'item_id':row['id'],'source_hash':digest(row),'score':score,'member_index':0,'chunk_index':0}


def ranks(lore=(),examples=()):
    return {'lore':[{'item_id':x} for x in lore],'example':[{'item_id':x} for x in examples]}


class RetrievalTests(unittest.TestCase):
    setUp=prior.SemanticTests.setUp
    save=prior.SemanticTests.save

    def retriever(self):
        return CharacterCandidateRetriever(self.store,self.index,config())

    def test_lexical_is_original_and_zero_index_query(self):
        old=select_context(self.version,'你血型是什么，书店呢？')
        with patch.object(self.index,'query',side_effect=AssertionError('lexical must not query')):
            value=self.retriever().retrieve(self.pid,self.version['id'],['你血型是什么，书店呢？'],mode='lexical')
        self.assertEqual(old,{k:value[k] for k in old})

    def test_member_boundaries_do_not_create_keyword(self):
        value=self.retriever().retrieve(self.pid,self.version['id'],['书','店'],mode='lexical')
        self.assertEqual(value['lore_ids'],[])
        ranked,_=rank_candidates(self.version,['书','店'],{},config())
        self.assertEqual(ranked['lore'],[])

    def test_ascii_literal_boundaries(self):
        for text in ('RAILWAY','aim','x_AI'):self.assertFalse(literal_match('AI',text))
        for text in ('AI','了解AI吗','用 ai 编码','(AI)'):self.assertTrue(literal_match('AI',text))
        self.assertTrue(literal_match('小林','问问小林'))

    def test_known_unknown_fact_and_semantic_does_not_mutate(self):
        data=deepcopy(self.data);data['appearance']['height_cm']=168;version=self.save(data)
        result=assemble(version,['你身高和血型是什么'],ranks(['book']),2)
        payload=[json.loads(p['text']) for p in result['parts'] if p['kind']=='fact']
        self.assertEqual(payload[0]['data']['value'],168)
        self.assertEqual(payload[1]['data']['status'],'unset')
        self.assertEqual(result['origin_type'],'creator_setting')

    def test_low_semantic_score_rejected(self):
        ranked,rejected=rank_candidates(self.version,['无关'],{'lore':[candidate(self.data['lorebook'][0],score=.575)]},config())
        self.assertEqual(ranked['lore'],[]);self.assertEqual(rejected[0]['reason'],'below_threshold')

    def test_rrf_ties_use_priority_then_id(self):
        v=deepcopy(self.version);v['data']['lorebook']=[prior.lore('z',keywords=['甲'],priority=1),prior.lore('a',keywords=['乙'],priority=2)]
        ranked,_=rank_candidates(v,['甲和乙'],{},config())
        self.assertEqual(ranked['lore'][0]['item_id'],'a')
        self.assertEqual(ranked,rank_candidates(v,['甲和乙'],{},config())[0])

    def ambiguous_version(self):
        v=deepcopy(self.version);v['data']['lorebook']=[prior.lore('a',title='小林',keywords=['小林','松树校园']),prior.lore('b',title='小林',keywords=['小林','竹子校园'])]
        return v

    def test_ambiguity_not_resolved_by_higher_cosine(self):
        v=self.ambiguous_version()
        ranked,rejected=rank_candidates(v,['小林是谁'],{'lore':[candidate(v['data']['lorebook'][0],score=.99),candidate(v['data']['lorebook'][1],score=.8)]},config())
        self.assertEqual(ranked['lore'],[])
        self.assertEqual({r['item_id'] for r in rejected},{'a','b'})

    def test_literal_clue_and_two_independent_members(self):
        v=self.ambiguous_version()
        ranked,_=rank_candidates(v,['松树校园的小林'],{},config())
        self.assertEqual([r['item_id'] for r in ranked['lore']],['a'])
        ranked,_=rank_candidates(v,['松树校园的小林','竹子校园的小林'],{},config())
        self.assertEqual({r['item_id'] for r in ranked['lore']},{'a','b'})

    def test_shared_dependency_is_single_and_complete(self):
        v=deepcopy(self.version);v['data']['lorebook']=[prior.lore('base'),prior.lore('a',dependencies=['base']),prior.lore('b',dependencies=['base'])]
        selected=assemble(v,['人物'],ranks(['a','b']),2)
        self.assertEqual(selected['lore_ids'],['base','a','b'])
        self.assertEqual(len(selected['dependency_groups']),2)

    def test_dependency_cycle_missing_private_disabled_depth(self):
        for rows,root in [([prior.lore('a',dependencies=['a'])],'a'),
            ([prior.lore('a',dependencies=['missing'])],'a'),
            ([prior.lore('a',dependencies=['b']),prior.lore('b',visibility='creator_only')],'a'),
            ([prior.lore('a',dependencies=['b']),prior.lore('b',enabled=False)],'a'),
            ([prior.lore('a',dependencies=['b']),prior.lore('b',dependencies=['c']),prior.lore('c',dependencies=['d']),prior.lore('d')],'a')]:
            with self.subTest(rows=rows):
                with self.assertRaises(SemanticFailure):dependency_group({r['id']:r for r in rows},root)
                v=deepcopy(self.version);v['data']['lorebook']=rows
                self.assertEqual(assemble(v,['x'],ranks([root]),2)['lore_ids'],[])

    def test_group_count_cannot_leave_half_group(self):
        v=deepcopy(self.version);v['data']['lorebook']=[prior.lore(str(i)) for i in range(5)]+[prior.lore('root',dependencies=['0','1','2','3','4'])]
        result=assemble(v,['x'],ranks(['root']),2)
        self.assertEqual(result['lore_ids'],[]);self.assertEqual(result['removed'][0]['reason'],'item_limit')

    def test_dynamic_budget_and_required_priority(self):
        v=deepcopy(self.version);v['data']['lorebook']=[prior.lore(text='大'*1590)]
        v['data']['appearance']['height_cm']=168
        result=assemble(v,['你多高'],ranks(['book']),2)
        self.assertEqual(result['lore_ids'],[]);self.assertIn('appearance.height_cm',result['fact_paths'])
        self.assertLessEqual(result['budgets']['dynamic'],1600)

    def test_required_overflow_is_explicit(self):
        v=deepcopy(self.version);v['data']['appearance']['description']='长'*1900
        with self.assertRaisesRegex(RuntimeErrorBase,'required_fact_too_large'):
            assemble(v,['角色长什么样'],ranks(),2)

    def test_example_count_2_and_4_and_budget(self):
        v=deepcopy(self.version);v['data']['examples']=[{'id':str(i),'scenario':'问候','keywords':['嗨'],'text':'你好','enabled':True} for i in range(6)]
        self.assertEqual(len(assemble(v,['x'],ranks(examples=list('012345')),2)['example_ids']),2)
        self.assertEqual(len(assemble(v,['x'],ranks(examples=list('012345')),4)['example_ids']),4)
        for row in v['data']['examples']:row['text']='长'*490
        result=assemble(v,['x'],ranks(examples=list('012345')),4)
        self.assertEqual(len(result['example_ids']),1);self.assertLessEqual(result['budgets']['examples'],600)

    def test_total_budget_rechecks_dependency_closure(self):
        v=deepcopy(self.version)
        v['data']['social']['public_image']='像'*650
        v['data']['lorebook']=[prior.lore('base',text='基'*300),prior.lore('a',text='甲'*500,dependencies=['base']),prior.lore('b',text='乙'*450,dependencies=['base'])]
        v['data']['examples']=[{'id':str(i),'scenario':'问候','keywords':['嗨'],'text':'句'*210,'enabled':True} for i in range(3)]
        result=assemble(v,['x'],ranks(['a','b'],list('012')),4)
        self.assertLessEqual(len(result['text']),3000)
        for group in result['dependency_groups']:self.assertTrue(set(group['members']).issubset(result['lore_ids']))
        self.assertEqual(result['budgets']['total'],len(render_parts(result['parts'])))

    def test_total_budget_actually_trims_complete_example(self):
        # Fill each partition independently just below its cap, then account for HEADER.
        v=deepcopy(self.version);v['data']['social']['public_image']='像'*730
        v['data']['lorebook']=[prior.lore('base',text='基'*300),prior.lore('a',text='甲'*1160,dependencies=['base'])]
        v['data']['examples']=[{'id':'one','scenario':'问候','keywords':['嗨'],'text':'句'*490,'enabled':True}]
        result=assemble(v,['x'],ranks(['a'],['one']),2)
        self.assertTrue(any(r['reason']=='character_total_budget' for r in result['removed']))
        self.assertEqual(result['lore_ids'],['base','a'])
        self.assertEqual(result['example_ids'],[])
        self.assertLessEqual(len(result['text']),3000)

    def test_unselected_private_or_disabled_cannot_be_semantic_candidate(self):
        for row in self.data['lorebook'][1:]:
            with self.assertRaisesRegex(SemanticFailure,'source_changed'):
                rank_candidates(self.version,['书店'],{'lore':[candidate(row,score=.99)]},config())

    def test_each_soft_failure_falls_back_once(self):
        from persona_runtime.character_retrieval import FALLBACK_CODES
        original=select_context(self.version,'书店')
        for reason in FALLBACK_CODES:
            with self.subTest(reason=reason),patch.object(self.index,'query',side_effect=SemanticFailure(reason)) as query:
                value=self.retriever().retrieve(self.pid,self.version['id'],['书店'])
                self.assertEqual(value['text'],original['text']);self.assertEqual(value['degrade_reason'],reason)
                self.assertEqual(query.call_count,1)

    def test_resource_failure_fallback(self):
        with patch.object(self.index,'query',side_effect=MemoryError()):
            self.assertEqual(self.retriever().retrieve(self.pid,self.version['id'],['书店'])['effective_mode'],'lexical')

    def test_hard_failures_never_fallback(self):
        for reason in ('character_source_changed','unsafe_semantic_path','character_scope_mismatch'):
            with patch.object(self.index,'query',side_effect=SemanticFailure(reason)):
                with self.assertRaisesRegex(SemanticFailure,reason):self.retriever().retrieve(self.pid,self.version['id'],['书店'])
        other=self.service.create_persona(generic_persona())['id']
        with self.assertRaises(RuntimeErrorBase):self.retriever().retrieve(other,self.version['id'],['书店'])
        with self.assertRaises(RuntimeErrorBase):self.retriever().retrieve(self.pid,'missing',['书店'])

    def test_final_source_recheck(self):
        retriever=self.retriever(); changed=deepcopy(self.version);changed['data']['interests']['notes']='changed'
        with patch.object(retriever.characters,'show',side_effect=[self.version,changed]):
            with self.assertRaisesRegex(SemanticFailure,'source_changed'):
                retriever.retrieve(self.pid,self.version['id'],['书店'],mode='lexical')

    def test_legal_old_version_and_no_write(self):
        self.index.build(self.pid,self.version['id'])
        changed=deepcopy(self.data);changed['lorebook'][0]['text']='新资料';self.save(changed)
        before=file_hash(self.store.path)
        result=self.retriever().retrieve(self.pid,self.version['id'],['书店'])
        self.assertIn('虚构' if '虚构' in self.data['lorebook'][0]['text'] else '阅读角',result['text'])
        self.assertEqual(file_hash(self.store.path),before)

    def test_classified_query_encodes_once(self):
        self.index.build(self.pid,self.version['id'])
        with patch.object(self.provider,'embed_query',wraps=self.provider.embed_query) as embed:
            result=self.index.query(self.pid,self.version['id'],['a','b'],limit=1,per_kind_limit=10)
        self.assertEqual(embed.call_count,1);self.assertEqual(len(result['candidates']),1)
        self.assertEqual(set(result['by_kind']),{'lore','example'})
        self.assertEqual(len(result['by_kind']['example']),1)

    def test_many_lore_do_not_starve_examples(self):
        data=deepcopy(self.data);data['lorebook']=[prior.lore(str(i)) for i in range(15)]
        v=self.save(data);self.index.build(self.pid,v['id'])
        value=self.index.query(self.pid,v['id'],['x'],per_kind_limit=10)
        self.assertEqual(len(value['by_kind']['lore']),10);self.assertEqual(len(value['by_kind']['example']),1)

    def test_result_diagnostics_exclude_bodies(self):
        value=self.retriever().retrieve(self.pid,self.version['id'],['书店'],mode='lexical')
        diagnostic=self.retriever().diagnostics(value)
        self.assertNotIn('text',diagnostic);self.assertNotIn('parts',diagnostic)
        self.assertNotIn(self.data['lorebook'][0]['text'],json.dumps(diagnostic,ensure_ascii=False))
        self.assertFalse(value['activation_authorized'])

    def test_invalid_inputs_and_config(self):
        for members in ([],[''],['x']*21):
            with self.assertRaises(SemanticFailure):self.retriever().retrieve(self.pid,self.version['id'],members)
        with self.assertRaises(SemanticFailure):self.retriever().retrieve(self.pid,self.version['id'],['x'],scenario=12)
        with self.assertRaises(SemanticFailure):config_checked(config() | {'thresholds':{'lore':float('nan'),'example':.7}})

    def test_frozen_development_corpus_and_no_locked_set(self):
        path=Path(__file__).parent/'fixtures/m752_development.json';data=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(len(data['cases']),32);self.assertEqual(sum(r['negative'] for r in data['cases']),16)
        self.assertEqual(data['split'],'development_only');self.assertEqual(data['locked_acceptance_reserved'],48)
