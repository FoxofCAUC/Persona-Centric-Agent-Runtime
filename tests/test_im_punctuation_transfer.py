"""标点职责在源v8与转译层之间互斥；仅本地输入及人工回执检查。"""
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from persona_runtime import companion_im as im,companion_selective_reply as selective
from persona_runtime.companion import IM_VERSION,SELECTIVE_VERSION
from persona_runtime.im_punctuation import GUIDANCE,EXPRESSION_PROFILE,delegate_blocks,delegated_for_turn
from persona_runtime.prompt_assembly import PromptBlock
from persona_runtime.provider_compat import build_request
from experiments.expression_persona_draft import protocol as v1,protocol_v2 as v2
from experiments.expression_persona_draft.study import model_for
from tests.prompt_baseline_fixture import capture
from tests.test_companion_im_chat import builder

VOICE={'core_traits':['坦率','温和'],'style':'说话随和，认真任务清楚。'}


class Transfer(unittest.TestCase):
    def test_original_v8_guidance_and_instruction_stay_byte_identical(self):
        old=('随口说此刻真正想回应的内容，短接话、自然省略都合适，意思到了就收住。'
             '建议和解释按当前话题与对方意愿取舍；普通闲聊的句尾通常留空，标点随语气和意思使用。')
        self.assertEqual(im.GUIDANCE,old)
        expected=im.PREVIOUS_INSTRUCTION.removesuffix('\n人物资料：')+old+'\n人物资料：'
        self.assertEqual(im.INSTRUCTION,expected);self.assertEqual(im.INSTRUCTION_VERSION,'companion-im-instruction-v1')
        self.assertEqual(selective.INSTRUCTION_VERSION,'companion-im-instruction-v2')

    def test_v1_research_prompt_remains_the_frozen_first_batch(self):
        root=Path(__file__).resolve().parents[1]
        frozen=json.loads((root/'docs/expression/E3.R0-最小输入冻结清单.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(v1.TASK.encode()).hexdigest(),frozen['task_sha256'])
        self.assertNotIn(GUIDANCE,v1.build_messages(VOICE,'原稿。')[0]['content'])

    def test_transfer_touches_only_known_instruction_prefix(self):
        suffix='人物原件中也有这句：'+GUIDANCE+'\n文件a.py与数字3.14保持。'
        for version,renderer in ((IM_VERSION,im),(SELECTIVE_VERSION,selective)):
            with self.subTest(version=version):
                other=PromptBlock('character','作者资料。','fixture','v1')
                blocks=[PromptBlock('persona_expression',renderer.INSTRUCTION+suffix,'fixture','v1'),other]
                actual,tag=delegate_blocks(blocks,version)
                self.assertEqual(actual[0].content,renderer.INSTRUCTION_WITHOUT_PUNCTUATION+suffix)
                self.assertNotIn(GUIDANCE,renderer.INSTRUCTION_WITHOUT_PUNCTUATION)
                self.assertIs(actual[1],other);self.assertIn('punctuation-delegated',tag)
                self.assertEqual(blocks[0].content,renderer.INSTRUCTION+suffix)

    def test_unknown_or_mismatched_instruction_fails_closed(self):
        for blocks,version in (([],IM_VERSION),([PromptBlock('persona_expression','改过的前缀','x','v')],IM_VERSION),([], 'unknown')):
            with self.subTest(version=version),self.assertRaises(ValueError):delegate_blocks(blocks,version)

    def test_owner_uses_frozen_enabled_v2_policy_only(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        self.assertFalse(delegated_for_turn(db,{'id':'t'}))
        db.execute('CREATE TABLE expression_runtime_turns(turn_id TEXT,policy_json TEXT)')
        db.execute('CREATE TABLE expression_runtime_policies(persona_id TEXT,data_json TEXT)')
        db.execute('INSERT INTO expression_runtime_turns VALUES(?,?)',('t',json.dumps({'data':{'enabled':True,'protocol':EXPRESSION_PROFILE}})))
        self.assertTrue(delegated_for_turn(db,{'id':'t'}))
        self.assertFalse(delegated_for_turn(db,{'id':'t','_search_notice':True}))
        db.execute('INSERT INTO expression_runtime_policies VALUES(?,?)',('p',json.dumps({'enabled':False})))
        self.assertTrue(delegated_for_turn(db,{'id':'t'}))
        for data in ({'enabled':False,'protocol':EXPRESSION_PROFILE},{'enabled':True,'protocol':v1.PROFILE},{'enabled':True,'protocol':'unknown'}):
            db.execute('UPDATE expression_runtime_turns SET policy_json=?',(json.dumps({'data':data}),))
            self.assertFalse(delegated_for_turn(db,{'id':'t'}))

    def test_actual_v8_wire_removes_only_the_punctuation_responsibility(self):
        case={'mode':'context_full','expression':3}
        for mode,renderer in (('minimal_im',im),('minimal_im_selective',selective)):
            with self.subTest(mode=mode):
                original=capture(case,builder(mode));diagnostics=[]
                with patch('persona_runtime.im_punctuation.delegated_for_turn',return_value=True):
                    delegated=capture(case,builder(mode,diagnostics))
                self.assertNotIn('error_code',delegated)
                expected=json.loads(json.dumps(original['messages']))
                self.assertEqual(expected[0]['content'].count(renderer.INSTRUCTION),1)
                expected[0]['content']=expected[0]['content'].replace(renderer.INSTRUCTION,renderer.INSTRUCTION_WITHOUT_PUNCTUATION,1)
                self.assertEqual(delegated['messages'],expected)
                self.assertEqual(original['details']['character'],delegated['details']['character'])
                self.assertEqual(diagnostics[0]['punctuation_owner'],'expression')
                self.assertIn('punctuation-delegated',diagnostics[0]['expression_renderer'])

    def test_new_translator_has_one_guidance_and_same_two_materials(self):
        draft='完整主稿。'
        old=v1.build_messages(VOICE,draft);new=v2.build_messages(VOICE,draft)
        self.assertEqual(v2.PROFILE,EXPRESSION_PROFILE)
        self.assertEqual(len(new),2);self.assertEqual(new[1],old[1])
        self.assertEqual(new[0]['content'].count(GUIDANCE),1)
        self.assertEqual(new[0]['content'].replace(GUIDANCE+'\n','',1),old[0]['content'])
        wire,_=build_request(model_for('fixture'),new)
        self.assertEqual(wire['messages'],new);self.assertEqual(wire['thinking'],{'type':'disabled'})

    def test_punctuation_is_model_guidance_not_a_body_cleaner(self):
        draft='值3.14，网址https://example.invalid/a.b。\n`a.py`\n```python\nprint("done.")\n```'
        body='按这个看：\n'+draft+'\n问题？省略号……'
        result=v2.decode_output(json.dumps({'body':body,'segments':'no_split'},ensure_ascii=False),draft)
        self.assertEqual(result['body'],body)
        self.assertEqual(''.join(s['text'] for s in result['segments']),body)

    def test_new_decoder_matches_frozen_v1_on_public_boundary_materials(self):
        from tests.test_expression_e1 import FIXTURES
        for case in FIXTURES['cases']:
            with self.subTest(case=case['id']):
                raw=json.dumps({'body':case['draft'],'segments':'no_split'},ensure_ascii=False)
                self.assertEqual(v2.decode_output(raw,case['draft']),v1.decode_output(raw,case['draft']))

    def test_extra_segmentation_flag_preserves_complete_body_as_fallback_only(self):
        raw=json.dumps({'body':'完整正文。','segments':[{'text':'完整正文。','type':'text'}],'no_split':False},ensure_ascii=False)
        result=v2.decode_output(raw,'原稿。')
        self.assertEqual(result['body'],'完整正文。');self.assertEqual(len(result['segments']),1)
        self.assertIn('unexpected_segmentation_field:no_split',result['notes'])
        with self.assertRaises(ValueError):v2.decode_output('{"body":"正文","segments":[],"analysis":"别的任务"}','原稿。')


class RuntimeTransfer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests import test_expression_runtime as fixtures
        fixtures.Runtime.setUp(self)
        self._fixture=fixtures
        self.runtime.configure(self.pid,{'protocol':EXPRESSION_PROFILE},self.runtime.policy(self.pid)['revision'],'enable-new-punctuation-owner')
        self.gate.raw_override=json.dumps({'body':self.gate.body,'segments':self.gate.parts},ensure_ascii=False)

    async def asyncTearDown(self):await self._fixture.Runtime.asyncTearDown(self)
    async def submit(self,text='USER_TEXT_NOT_FOR_EXPRESSION'):
        return await self._fixture.Runtime.submit(self,text)
    def acknowledge(self,*args,**kwargs):return self._fixture.Runtime.acknowledge(self,*args,**kwargs)
    def all_visible(self,turn):return self._fixture.Runtime.all_visible(self,turn)

    async def test_real_runtime_routes_two_inputs_without_candidate_lookup(self):
        from persona_runtime.expression_candidates import ExpressionCandidates
        with patch.object(ExpressionCandidates,'select',side_effect=AssertionError('new profile must not select candidates')):
            turn=await self.submit()
        self.assertEqual(turn['expression']['publication']['producer_kind'],'artifact')
        self.assertNotIn(GUIDANCE,self.main.calls[0][0]['content'])
        messages=self.gate.calls[0]
        self.assertEqual(len(messages),2);self.assertEqual(messages[1]['content'],'隐藏主稿。')
        self.assertEqual(messages[0]['content'].count(GUIDANCE),1)
        self.assertNotIn('USER_TEXT_NOT_FOR_EXPRESSION',json.dumps(messages))
        with self.store.connection() as db:
            self.assertIsNone(db.execute('SELECT candidate_snapshot_id FROM expression_runs').fetchone()[0])
        self.assertEqual(self.all_visible(turn)['reply'],self.gate.body)

    async def test_frozen_profile_parses_its_own_response_after_policy_change(self):
        import asyncio
        self.gate.gate=asyncio.Event()
        turn=await self.chat.submit('desktop','本轮冻结新配置。','profile-freeze-source')
        await self.gate.started.wait()
        current=self.runtime.policy(self.pid)
        self.runtime.configure(self.pid,{'protocol':'expression-json-scoped-v3'},current['revision'],'change-next-turn-only')
        self.gate.gate.set();await self.chat.wait()
        value=self.chat.get(turn['id']);self.assertEqual(value['expression']['run']['state'],'completed')
        self.assertEqual(self.all_visible(value)['reply'],self.gate.body)

    async def test_unknown_new_profile_falls_back_without_replaying(self):
        self.gate.failure='provider_transport_interrupted';turn=await self.submit()
        self.assertEqual(turn['expression']['publication']['producer_kind'],'draft')
        self.assertEqual(self.all_visible(turn)['reply'],'隐藏主稿。')
        await self.chat.resume_expression(turn['id'])
        self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),1)

    async def test_search_notice_delegates_punctuation_when_structure32_handles_translation(self):
        from types import SimpleNamespace
        from persona_runtime.knowledge_search_notice import SearchNotice,request_model
        turn=await self.submit()
        processor=SimpleNamespace(store=self.store,engine=SimpleNamespace(chat=self.chat))
        with self.store.connection() as db:
            raw=dict(db.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone())
            model=request_model(json.loads(raw['model_snapshot']))
            task={'id':'notice-fixture','model_snapshot':json.dumps(model)}
            payload={'knowledge_processing':{'root_turn_id':turn['id']}}
            with patch.object(self.runtime.outlet,'freeze_notice',return_value=True) as frozen:
                context=SearchNotice(processor).context(db,task,payload)
                frozen.assert_called_once()
        self.assertNotIn(GUIDANCE,context['messages'][0]['content'])
        self.assertIn('punctuation-delegated',context['manifest']['prompt_assembly']['expression_renderer'])

    async def test_missing_code_never_publishes_commentary_in_place_of_complete_source(self):
        code='```python\ndef dedupe(nums):\n    return list(dict.fromkeys(nums))\n```'
        async def main(model,messages):return code,{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}
        self.main.complete=main
        self.gate.raw_override=json.dumps({'body':'你这段代码写得很简洁。','segments':'no_split'},ensure_ascii=False)
        turn=await self.submit('给完整代码。')
        self.assertEqual(turn['expression']['run']['error_code'],'protected_text_changed')
        self.assertEqual(turn['expression']['publication']['producer_kind'],'draft')
        self.assertEqual(self.all_visible(turn)['reply'],code)


if __name__=='__main__':unittest.main()
