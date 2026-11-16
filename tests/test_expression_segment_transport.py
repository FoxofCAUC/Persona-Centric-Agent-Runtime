"""分段对象兼容只影响原文映射；旧运输版本和已确定正文保持。"""
import asyncio
import json
import unittest
from persona_runtime.expression_persona_draft import decode_output,STRING_SEGMENTS,OBJECT_SEGMENTS
from tests import test_im_punctuation_transfer as fixtures


class Transport(unittest.TestCase):
    def test_text_objects_preserve_exact_body_and_independent_markers(self):
        body='先放着，\n我去查查'
        for marker in ({'type':'text'},{'type':'no_split'},{'no_split':True},{'no_split':False}):
            with self.subTest(marker=marker):
                raw=json.dumps({'body':body,'segments':[{'text':'先放着，',**marker},{'text':'我去查查',**marker}]},ensure_ascii=False)
                old=decode_output(raw,body,segment_transport=STRING_SEGMENTS);self.assertTrue(old['plan_fallback'])
                new=decode_output(raw,body,segment_transport=OBJECT_SEGMENTS)
                self.assertFalse(new['plan_fallback']);self.assertEqual(new['body'],body)
                self.assertEqual(''.join(s['text'] for s in new['segments']),body);self.assertEqual(len(new['segments']),2)

    def test_invented_piece_unknown_fields_and_wrong_marker_type_keep_full_body(self):
        for segments in ([{'text':'不是原文'}],[{'text':'正文','instruction':'new task'}],[{'text':'正文','no_split':'true'}]):
            with self.subTest(segments=segments):
                value=decode_output(json.dumps({'body':'正文','segments':segments},ensure_ascii=False),'正文',segment_transport=OBJECT_SEGMENTS)
                self.assertTrue(value['plan_fallback']);self.assertEqual(value['body'],'正文');self.assertEqual(len(value['segments']),1)

    def test_code_boundary_cannot_be_split_or_missing(self):
        body='```python\nprint(1)\n```'
        value=decode_output(json.dumps({'body':body,'segments':[{'text':'```python\n'},{'text':'print(1)\n```'}]}),body,segment_transport=OBJECT_SEGMENTS)
        self.assertTrue(value['plan_fallback']);self.assertEqual(value['segments'][0]['text'],body)
        with self.assertRaisesRegex(ValueError,'protected_text_changed'):
            decode_output('{"body":"代码挺好","segments":[{"text":"代码挺好"}]}',body,segment_transport=OBJECT_SEGMENTS)


class FrozenTransport(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.RuntimeTransfer.setUp
    asyncTearDown=fixtures.RuntimeTransfer.asyncTearDown
    submit=fixtures.RuntimeTransfer.submit
    acknowledge=fixtures.RuntimeTransfer.acknowledge
    all_visible=fixtures.RuntimeTransfer.all_visible

    async def test_frozen_object_transport_survives_later_policy_change(self):
        self.gate.gate=asyncio.Event()
        self.gate.raw_override=json.dumps({'body':'一段。二段。','segments':[{'text':'一段。','type':'text'},{'text':'二段。','no_split':True}]},ensure_ascii=False)
        turn=await self.chat.submit('desktop','场景',__import__('persona_runtime.models',fromlist=['identifier']).identifier())
        await asyncio.wait_for(self.gate.started.wait(),8)
        current=self.runtime.policy(self.pid)
        self.runtime.configure(self.pid,{'segment_transport':STRING_SEGMENTS},current['revision'],'next-transport-only')
        self.gate.gate.set();await self.chat.wait()
        plan=self.publisher.plan(self.pid,turn['session_id'],turn['id']);self.assertEqual(len(plan['items']),2)
        self.assertEqual(self.all_visible(turn)['reply'],'一段。二段。')


if __name__=='__main__':unittest.main()
