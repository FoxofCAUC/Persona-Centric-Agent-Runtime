import json,unittest
from unittest.mock import patch
from tests import test_knowledge_processing as processing
from tests import test_knowledge_routing as routing
from tests import test_m4 as fixtures

class WindowSelectionTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    asyncTearDown=routing.cleanup
    ask=processing.ProcessingTests.ask
    async def test_full_windows_include_tail_and_bind_original_offsets(self):
        body='无关背景介绍。'*900+'尾部事实：月球是地球唯一的天然卫星。'
        self.assertEqual(self.org.processing.defaults()['prompt_profile'],'question_focused_v6')
        processing.ProcessingTests.enable(self,body)
        policy=self.org.processing.policy(self.persona['id'])
        self.org.processing.update_policy(self.persona['id'],policy['data']|{'prompt_profile':'question_focused_v6'},policy['revision'],'window-profile')
        original=self.processor.complete;seen=[];translated=[]
        async def choose(model,messages):
            data=json.loads(messages[-1]['content'])
            if 'sources' in data:
                if not data.get('full_windows'):return json.dumps({'format_version':1,'section_ids':[data['sources'][0]['id']]}),{'total_tokens':1}
                seen.extend(data['sources'])
                return json.dumps({'format_version':1,'section_ids':[s['id'] for s in data['sources'] if '尾部事实' in s['lead']][-1:]}),{'total_tokens':1}
            if 'materials' in data and 'units' not in data:translated.extend(data['materials'])
            return await original(model,messages)
        with patch.object(self.processor,'complete',side_effect=choose):result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertTrue(all(len(s['lead'])==s['characters']<=500 for s in seen))
        self.assertTrue(any('尾部事实' in s['lead'] for s in seen))
        self.assertTrue(all('尾部事实' in m['text'] and len(m['text'])<=500 for m in translated))
        evidence=self.knowledge.show_evidence(self.persona['id'],result['id'])['evidence']
        self.assertTrue(evidence)
        for e in evidence:self.assertEqual(body[e['start']:e['end']],e['quote'])
        self.assertEqual(evidence[0]['start'],body.index('尾部事实'))
