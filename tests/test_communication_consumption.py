from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import unittest

from persona_runtime.archive import encoded, fingerprint
from persona_runtime.dialogue_context import SharedEvidence, EVIDENCE_VERSION, FIELDS, parse_joint
from persona_runtime.communication_consumption import (
    VERSION, UNDERSTANDING_VERSION, CommunicativeSpecV2, joint_messages_v2,
    parse_joint_v2, projection_v2, decision_input_v2,
)
from persona_runtime.models import RuntimeErrorBase


POOL = {'payload': {'roots': {'lore': [], 'example': []}, 'groups': {}, 'sources': []}}


def evidence(profile=True):
    rows = [
        {'id': 'current:u', 'kind': 'current', 'authority': 'user', 'version': '1',
         'text': '请给我明确答复，不要沿用旧说法。'},
        {'id': 'history:a', 'kind': 'history', 'authority': 'assistant', 'version': '1',
         'text': '两件先暂停操作，这是我的猜测。'},
    ]
    if profile:
        rows.append({'id': 'profile:expression', 'kind': 'profile', 'authority': 'accepted_profile',
                     'version': 'expr-1', 'text': '愿意说出具体喜好，认真任务先把内容完成。'})
    for row in rows: row['hash'] = fingerprint(row['text'])
    return SharedEvidence(encoded({'version': EVIDENCE_VERSION, 'turn_id': 'turn-1',
        'execution_revision': 1, 'scope': {'persona_id': 'artificial-1'},
        'current': ['current:u'], 'sources': rows, 'references': [], 'open_slots': []}))


def statement(text='完成用户当前请求。', source='current:u', scope='user', certainty='explicit', e=None):
    e = e or evidence()
    src = next(s for s in e.document()['sources'] if s['id'] == source)
    return {'text': text, 'certainty': certainty, 'claim_scope': scope,
            'evidence': [{'source_id': source, 'quote': src['text']}] if certainty != 'unknown' else []}


def wire(e=None, request=True):
    e = e or evidence(); frame = {name: [] for name in FIELDS}
    if request: frame['requests'] = [statement(e=e)]
    return {'schema_version': 2, 'evidence_hash': e.digest, 'frame': frame,
        'selection': {'schema_version': 1, 'lore_ids': [], 'example_ids': []},
        'gap_status': 'sufficient', 'communication_spec': {'version': VERSION,
            'required_content': [{'field': 'requests', 'index': 0}] if request else [],
            'state_constraints': [], 'optional_reactions': [], 'gap_checks': []}}


def parsed(value=None, e=None):
    e = e or evidence()
    return parse_joint_v2(encoded(value or wire(e)), e, POOL)


def referenced_evidence():
    body = evidence().document()
    text = '\n明确引用的本地历史资料：\n' + encoded({'selections': [], 'turns': [{
        'members': [{'id': 'ref-member', 'role': 'user', 'text': '用户已经核对了包装尺寸。'}],
        'replies': [{'id': 'ref-reply', 'text': '我猜包装里面可能有两张贺卡。'}],
        'user_read': 'unique-metadata-marker'}]})
    body['sources'].append({'id': 'references:accepted', 'kind': 'reference', 'authority': 'user',
                            'version': 'ref-1', 'text': text, 'hash': fingerprint(text)})
    body['references'] = ['references:accepted']
    return SharedEvidence(encoded(body))


class Contracts(unittest.TestCase):
    def test_runtime_binds_frame_and_expression_without_model_hashes(self):
        e = evidence(); value = wire(e)
        self.assertNotIn('frame_hash', value['communication_spec'])
        result = parsed(value, e); spec = result['communication_spec']
        self.assertEqual(spec['frame_hash'], fingerprint(value['frame']))
        self.assertEqual(spec['evidence_hash'], e.digest)
        self.assertEqual(spec['execution_revision'], 1)
        self.assertEqual(spec['expression_version'], 'expr-1')
        self.assertEqual(parsed(result, e), result)
        obj = CommunicativeSpecV2(encoded(spec))
        with self.assertRaises(FrozenInstanceError): obj.body_json = '{}'
        mutable = obj.document(); mutable['required_content'].clear()
        self.assertEqual(len(obj.document()['required_content']), 1)

    def test_joint_input_uses_new_version_and_original_candidates(self):
        e = evidence(); messages = joint_messages_v2(e, POOL, 4, 48000)
        payload = json.loads(messages[1]['content'])
        self.assertEqual(payload['contract'], UNDERSTANDING_VERSION)
        self.assertEqual(payload['candidates'], POOL['payload'])
        self.assertEqual(payload['evidence_hash'], e.digest)
        with self.assertRaisesRegex(RuntimeErrorBase, 'aux_input_budget'):
            joint_messages_v2(e, POOL, 4, 1)

    def test_unknown_versions_are_protocol_invalid(self):
        for target in ('schema', 'spec'):
            value = wire()
            if target == 'schema': value['schema_version'] = 999
            else: value['communication_spec']['version'] = 'communication-spec-future'
            with self.assertRaisesRegex(RuntimeErrorBase, 'provider_invalid_response'): parsed(value)

    def test_assistant_record_is_allowed_without_promoting_user_fact(self):
        e = evidence(); value = wire(e)
        value['frame']['progress'] = [statement('助手此前说先暂停，不代表用户确认。', 'history:a', 'conversation', e=e)]
        value['communication_spec']['state_constraints'] = [{'field': 'progress', 'index': 0}]
        result = parsed(value, e); text, _ = projection_v2(result, e)
        self.assertIn('助手的对话记录', text)
        value['frame']['progress'][0]['claim_scope'] = 'user'
        with self.assertRaisesRegex(RuntimeErrorBase, 'provider_invalid_response'): parsed(value, e)

    def test_v1_still_rejects_the_original_assistant_explicit_case(self):
        e = evidence(); value = wire(e); value.pop('communication_spec'); value['schema_version'] = 1
        value['frame']['progress'] = [statement('助手此前说先暂停。', 'history:a', 'conversation', e=e)]
        for rows in value['frame'].values():
            for row in rows: row.pop('claim_scope')
        with self.assertRaisesRegex(RuntimeErrorBase, 'provider_invalid_response'):
            parse_joint(encoded(value), e, POOL)

    def test_embedded_assistant_quote_cannot_become_explicit_user_fact(self):
        e = referenced_evidence(); value = wire(e)
        row = {'text': '用户已确认里面有两张贺卡。', 'certainty': 'explicit', 'claim_scope': 'user',
               'evidence': [{'source_id': 'references:accepted', 'quote': '我猜包装里面可能有两张贺卡。'}]}
        value['frame']['progress'] = [row]
        with self.assertRaisesRegex(RuntimeErrorBase, 'provider_invalid_response'): parsed(value, e)

    def test_embedded_assistant_conversation_keeps_original_speaker(self):
        e = referenced_evidence(); value = wire(e)
        value['frame']['progress'] = [{'text': '助手曾猜里面有两张贺卡，这不是用户确认。',
            'certainty': 'explicit', 'claim_scope': 'conversation',
            'evidence': [{'source_id': 'references:accepted', 'quote': '我猜包装里面可能有两张贺卡。'}]}]
        value['communication_spec']['state_constraints'] = [{'field': 'progress', 'index': 0}]
        text, _ = projection_v2(parsed(value, e), e)
        self.assertIn('助手的对话记录：', text)
        self.assertNotIn('用户的对话记录：', text)

    def test_reference_member_body_is_user_but_metadata_is_not(self):
        e = referenced_evidence(); value = wire(e)
        value['frame']['progress'] = [{'text': '用户已经核对包装尺寸。', 'certainty': 'explicit',
            'claim_scope': 'user', 'evidence': [{'source_id': 'references:accepted', 'quote': '用户已经核对了包装尺寸。'}]}]
        self.assertEqual(parsed(value, e)['frame']['progress'][0]['claim_scope'], 'user')
        value['frame']['progress'][0]['evidence'][0]['quote'] = 'unique-metadata-marker'
        with self.assertRaises(RuntimeErrorBase): parsed(value, e)

    def test_reference_escaped_body_quote_retains_assistant_authority(self):
        body = evidence().document(); said = '助手说"稍后"再讨论。'
        text = '\n明确引用资料：\n' + encoded({'selections': [], 'turns': [{
            'members': [], 'replies': [{'id': 'quoted-reply', 'text': said}]}]})
        body['sources'].append({'id': 'references:accepted', 'kind': 'reference', 'authority': 'user',
            'version': 'ref-2', 'text': text, 'hash': fingerprint(text)})
        e = SharedEvidence(encoded(body)); value = wire(e)
        escaped = json.dumps(said, ensure_ascii=False)[1:-1]
        value['frame']['progress'] = [{'text': '助手曾说稍后再讨论。', 'certainty': 'explicit',
            'claim_scope': 'conversation', 'evidence': [{'source_id': 'references:accepted', 'quote': escaped}]}]
        value['communication_spec']['state_constraints'] = [{'field': 'progress', 'index': 0}]
        self.assertIn('助手的对话记录：', projection_v2(parsed(value, e), e)[0])
        value['frame']['progress'][0]['claim_scope'] = 'user'
        with self.assertRaises(RuntimeErrorBase): parsed(value, e)

    def test_profile_fact_needs_approved_profile_authority(self):
        value = wire(); value['frame']['progress'] = [statement(scope='persona')]
        with self.assertRaises(RuntimeErrorBase): parsed(value)

    def test_bindings_stale_after_role_revision_or_expression_change(self):
        original = evidence(); value = parsed(e=original)
        for kind in ('role', 'revision', 'expression'):
            body = original.document()
            if kind == 'role': body['scope']['persona_id'] = 'artificial-2'
            elif kind == 'revision': body['execution_revision'] += 1
            else: body['sources'][-1]['version'] = 'expr-2'
            changed = SharedEvidence(encoded(body)); altered = deepcopy(value)
            altered['evidence_hash'] = changed.digest
            with self.assertRaises(RuntimeErrorBase): parsed(altered, changed)
        altered = deepcopy(value); altered['frame']['requests'][0]['text'] = '新内容'
        with self.assertRaisesRegex(RuntimeErrorBase, 'spec_stale'):
            projection_v2(altered, original)

    def test_quotes_source_ids_duplicates_and_outside_candidates_rejected(self):
        for fault in ('source', 'quote', 'id', 'duplicate_ref'):
            value = wire()
            if fault == 'source': value['frame']['requests'][0]['evidence'][0]['source_id'] = 'foreign'
            elif fault == 'quote': value['frame']['requests'][0]['evidence'][0]['quote'] = '不是原文'
            elif fault == 'id': value['selection']['example_ids'] = ['outside']
            else: value['communication_spec']['required_content'] *= 2
            with self.assertRaises(RuntimeErrorBase): parsed(value)
        with self.assertRaises(RuntimeErrorBase):
            parse_joint_v2('{"schema_version":2,"schema_version":2}', evidence(), POOL)

    def test_required_content_cannot_reference_unknown_or_old_assistant(self):
        for kind in ('uncertainty', 'assistant', 'interpretation'):
            value = wire()
            if kind == 'uncertainty':
                value['frame']['uncertainty'] = [statement(certainty='unknown')]
                value['communication_spec']['required_content'] = [{'field': 'uncertainty', 'index': 0}]
            elif kind == 'assistant': value['frame']['requests'] = [statement(source='history:a', scope='conversation')]
            else: value['frame']['requests'][0]['certainty'] = 'interpretation'
            with self.assertRaises(RuntimeErrorBase): parsed(value)

    def test_empty_spec_is_valid_without_profile(self):
        e = evidence(False); result = parsed(wire(e, request=False), e)
        self.assertEqual(projection_v2(result, e), ('', []))

    def test_natural_imagination_and_courtesy_do_not_need_sentence_evidence(self):
        e = evidence(); value = wire(e)
        value['communication_spec']['optional_reactions'] = [
            {'text': '草稿可以自然写下次请你喝饮料。', 'profile_ref': 'profile:expression'},
            {'text': '也可联想它像刚睡醒的小动物。', 'profile_ref': 'profile:expression'}]
        text, _ = projection_v2(parsed(value, e), e)
        self.assertIn('喝饮料', text); self.assertIn('小动物', text)
        value['communication_spec']['optional_reactions'][0]['profile_ref'] = 'foreign-expression'
        with self.assertRaises(RuntimeErrorBase): parsed(value, e)

    def test_optional_reactions_limit(self):
        value = wire(); value['communication_spec']['optional_reactions'] = [
            {'text': '关注眼前的具体变化。', 'profile_ref': 'profile:expression'}] * 3
        with self.assertRaises(RuntimeErrorBase): parsed(value)

    def test_precision_gap_does_not_force_question_or_enter_expression(self):
        e = evidence(); value = wire(e, request=False)
        value['frame']['missing_slots'] = [statement('独有精度未知', certainty='unknown')]
        value['frame']['uncertainty'] = [statement('独有非阻断未知', certainty='unknown')]
        value['communication_spec']['gap_checks'] = [{'slot_ref': {'field': 'missing_slots', 'index': 0},
                                                     'necessity': 'precision', 'request_ref': None}]
        result = parsed(value, e)
        self.assertEqual(projection_v2(result, e), ('', []))
        self.assertEqual(decision_input_v2(result)['gap_checks'][0]['slot']['text'], '独有精度未知')
        value['communication_spec']['state_constraints'] = [{'field': 'missing_slots', 'index': 0}]
        with self.assertRaises(RuntimeErrorBase): parsed(value, e)

    def test_blocker_requires_current_explicit_request_and_is_consumed(self):
        e = evidence(); value = wire(e)
        value['frame']['missing_slots'] = [statement('横梁厚度', certainty='unknown')]
        value['communication_spec']['gap_checks'] = [{'slot_ref': {'field': 'missing_slots', 'index': 0},
                    'necessity': 'blocking', 'request_ref': {'field': 'requests', 'index': 0}}]
        value['gap_status'] = 'needed'; result = parsed(value, e)
        self.assertIn('横梁厚度', projection_v2(result, e)[0])
        self.assertEqual(decision_input_v2(result)['gap_checks'][0]['request']['text'], '完成用户当前请求。')
        value['communication_spec']['gap_checks'][0]['request_ref'] = None
        with self.assertRaises(RuntimeErrorBase): parsed(value, e)

    def test_gap_coverage_and_needed_consistency(self):
        value = wire(); value['gap_status'] = 'needed'
        with self.assertRaises(RuntimeErrorBase): parsed(value)
        value['gap_status'] = 'unknown'; value['frame']['missing_slots'] = [statement(certainty='unknown')]
        with self.assertRaises(RuntimeErrorBase): parsed(value)

    def test_field_consumption_and_removal_have_no_hidden_frame_dump(self):
        e = evidence(); value = wire(e)
        value['frame']['willingness'] = [statement('独有状态约束')]
        value['frame']['focus'] = [statement('独有未消费焦点')]
        value['communication_spec']['state_constraints'] = [{'field': 'willingness', 'index': 0}]
        value['communication_spec']['optional_reactions'] = [{'text': '独有可选反应', 'profile_ref': 'profile:expression'}]
        before = projection_v2(parsed(value, e), e)[0]
        self.assertIn('完成用户当前请求。', before); self.assertIn('独有状态约束', before)
        self.assertIn('独有可选反应', before); self.assertNotIn('独有未消费焦点', before)
        value['communication_spec']['required_content'] = []
        value['communication_spec']['state_constraints'] = []
        value['communication_spec']['optional_reactions'] = []
        self.assertEqual(projection_v2(parsed(value, e), e)[0], '')

    def test_budget_removes_optional_first_never_truncates_necessary_content(self):
        e = evidence(); value = wire(e)
        base = projection_v2(parsed(value, e), e)[0]
        value['communication_spec']['optional_reactions'] = [
            {'text': '可选' * 70, 'profile_ref': 'profile:expression'},
            {'text': '人物' * 70, 'profile_ref': 'profile:expression'}]
        text, removed = projection_v2(parsed(value, e), e, maximum=len(base))
        self.assertEqual(text, base); self.assertEqual(len(removed), 2)
        with self.assertRaisesRegex(RuntimeErrorBase, 'projection_budget'):
            projection_v2(parsed(value, e), e, maximum=3)

    def test_semantically_bad_but_structurally_legal_is_not_quality_retried(self):
        value = wire(); value['frame']['requests'][0]['text'] = '故意误判：坚持旧说法并宣称全部完成。'
        self.assertEqual(parsed(value)['frame']['requests'][0]['text'], value['frame']['requests'][0]['text'])


if __name__ == '__main__':
    unittest.main()
