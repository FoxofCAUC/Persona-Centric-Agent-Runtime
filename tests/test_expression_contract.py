"""表达合同的越权、内容保真及实际呈现边界。"""
from dataclasses import replace
import unittest

from persona_runtime.expression_contract import (
    BubbleRange, CallReceipt, DialogueLine, ExpressionArtifact, ExpressionContext,
    ExpressionContractError, InteractionRef, MainDraftReceipt, PresentationPlan,
    PresentationReceipt, ProtectedSpan, ScopedText, context_hash, text_hash,
    validate_artifact, validate_call, validate_context, validate_draft, validate_plan,
    visible_utterances,
)


class ExpressionContractTests(unittest.TestCase):
    def setUp(self):
        self.ref = InteractionRef('root-1', 'public-persona', 'session-1', 'private-session', 'policy-1')
        self.body = '先确认环境。\n\n```python\nprint("好")\n```\n再执行。'
        start = self.body.index('```')
        end = self.body.index('```', start + 3) + 3
        self.protected = (ProtectedSpan('code-1', start, end, 'code'),)
        main_call = CallReceipt('main-attempt', 'primary', 'deepseek_official', 'deepseek-flash', 'completed')
        self.draft = MainDraftReceipt(self.ref, 'draft-1', self.body, text_hash(self.body),
                                      'durable-draft-receipt', main_call, self.protected)
        user = DialogueLine(self.ref, 'user-event', 'user', '给我完整代码。', 'accepted-user')
        old_ref = replace(self.ref, root_id='previous-root', policy_version='previous-policy')
        previous = DialogueLine(old_ref, 'previous-visible-event', 'assistant', '在这个环境试试。', 'desktop-presented-previous')
        notice = DialogueLine(self.ref, 'notice-event', 'assistant', '我翻一下刚才那页。', 'desktop-presented-notice', 'search_notice')
        style = ScopedText('style-1', 'v1', self.ref.persona_id, self.ref.scope_id, 'source-grant', '说话坦率。', text_hash('说话坦率。'))
        candidate = replace(style, source_id='candidate-1', body='先接住问题再补细节。', body_sha256=text_hash('先接住问题再补细节。'))
        self.context = ExpressionContext(self.ref, 'context-1', self.draft.draft_id, self.draft.body_sha256,
                                          'projection-grant', 'grant-v1', user, (style,), (previous,), (notice,), (candidate,))
        expression_call = replace(main_call, attempt_id='expression-attempt', stage='expression')
        self.artifact = ExpressionArtifact(self.ref, 'artifact-1', self.draft.draft_id, self.draft.body_sha256,
                                           self.context.context_id, context_hash(self.context), self.body,
                                           text_hash(self.body), expression_call, 'durable-expression-receipt', self.protected, ('candidate-1',))
        self.plan = PresentationPlan(self.ref, 'plan-1', 'artifact-1', self.artifact.body_sha256,
                                     'single-publication-claim', 'timing-test-v1',
                                     (BubbleRange('utterance-1', 0, start), BubbleRange('utterance-2', start, len(self.body), 'technical')))

    def assertCode(self, code, operation):
        with self.assertRaisesRegex(ExpressionContractError, '^' + code + '$'):
            operation()

    def receipt(self, index, state='presented', **changes):
        bubble = self.plan.bubbles[index]
        value = PresentationReceipt(self.ref, self.plan.plan_id, bubble.utterance_id,
                                     f'presented-{index}', state,
                                     text_hash(self.body[bubble.start:bubble.end]), 1234 + index)
        return replace(value, **changes)

    def test_complete_contract_accepts_unchanged_expression_and_empty_selection(self):
        validate_artifact(self.draft, self.context, self.artifact)
        validate_artifact(self.draft, self.context, replace(self.artifact, selected_expression_ids=()))
        validate_plan(self.artifact, self.plan)

    def test_unknown_main_call_cannot_become_expression_input(self):
        draft = replace(self.draft, call=replace(self.draft.call, state='unknown'))
        self.assertCode('draft_not_complete', lambda: validate_context(draft, self.context))

    def test_complete_draft_requires_storage_receipt_and_exact_hash(self):
        self.assertCode('draft_not_receipted', lambda: validate_draft(replace(self.draft, storage_receipt_id='')))
        self.assertCode('body_hash_mismatch', lambda: validate_draft(replace(self.draft, body=self.body + ' ')))

    def test_hidden_or_unacknowledged_history_has_no_valid_dialogue_receipt(self):
        hidden = replace(self.context.history[0], receipt_id='')
        self.assertCode('dialogue_without_receipt', lambda: validate_context(self.draft, replace(self.context, history=(hidden,))))

    def test_cross_role_projection_and_revoked_source_are_rejected(self):
        source = self.context.style[0]
        self.assertCode('source_scope_mismatch', lambda: validate_context(self.draft, replace(self.context, style=(replace(source, persona_id='other-persona'),))))
        self.assertCode('source_not_active', lambda: validate_context(self.draft, replace(self.context, style=(replace(source, state='revoked'),))))

    def test_same_root_already_spoken_is_not_duplicated_in_history(self):
        self.assertCode('duplicate_dialogue_event', lambda: validate_context(self.draft, replace(self.context, history=self.context.already_spoken)))
        notice = replace(self.context.already_spoken[0], ref=replace(self.ref, root_id='old-root'))
        self.assertCode('invalid_already_spoken', lambda: validate_context(self.draft, replace(self.context, already_spoken=(notice,))))

    def test_frozen_context_change_invalidates_artifact(self):
        context = replace(self.context, current_user=replace(self.context.current_user, body='换成另一个请求。'))
        self.assertCode('artifact_context_mismatch', lambda: validate_artifact(self.draft, context, self.artifact))

    def test_expression_attempt_is_separate_from_primary(self):
        artifact = replace(self.artifact, call=replace(self.artifact.call, attempt_id=self.draft.call.attempt_id))
        self.assertCode('expression_reuses_primary_attempt', lambda: validate_artifact(self.draft, self.context, artifact))
        self.assertCode('artifact_not_receipted', lambda: validate_artifact(self.draft, self.context, replace(self.artifact, storage_receipt_id='')))

    def test_protected_code_cannot_be_changed_or_omitted(self):
        changed = self.body.replace('print', 'PRINT')
        artifact = replace(self.artifact, body=changed, body_sha256=text_hash(changed))
        self.assertCode('protected_text_changed', lambda: validate_artifact(self.draft, self.context, artifact))
        self.assertCode('protected_text_changed', lambda: validate_artifact(self.draft, self.context, replace(self.artifact, protected=())))

    def test_unoffered_or_duplicate_expression_ids_are_rejected(self):
        for ids in (('candidate-other',), ('candidate-1', 'candidate-1')):
            self.assertCode('invalid_selected_expression_ids', lambda: validate_artifact(self.draft, self.context, replace(self.artifact, selected_expression_ids=ids)))

    def test_full_visible_text_restores_every_newline_and_space(self):
        events = visible_utterances(self.artifact, self.plan, (self.receipt(0), self.receipt(1)))
        self.assertEqual(''.join(event.body for event in events), self.body)
        self.assertEqual(events[1].presentation_receipt_id, 'presented-1')
        self.assertEqual(events[1].producer_id, self.artifact.artifact_id)

    def test_ranges_use_unicode_codepoints_including_emoji(self):
        body = '先🫖聊。再试试。'
        draft = replace(self.draft, body=body, body_sha256=text_hash(body),
                        protected=(ProtectedSpan('emoji', 1, 2, 'verbatim'),))
        plan = replace(self.plan, producer_id=draft.draft_id, body_sha256=draft.body_sha256,
                       bubbles=(BubbleRange('emoji-one', 0, 2), BubbleRange('emoji-two', 2, len(body))))
        receipts = tuple(PresentationReceipt(self.ref, plan.plan_id, b.utterance_id, 'emoji-receipt-'+str(i),
                         'presented', text_hash(body[b.start:b.end]), i) for i, b in enumerate(plan.bubbles))
        events = visible_utterances(draft, plan, receipts)
        self.assertEqual(events[0].body, '先🫖')
        self.assertEqual(''.join(e.body for e in events), body)

    def test_partial_and_unknown_presentation_do_not_publish_hidden_tail(self):
        for receipts in ((self.receipt(0),), (self.receipt(0), self.receipt(1, 'unknown'))):
            events = visible_utterances(self.artifact, self.plan, receipts)
            self.assertEqual([e.utterance_id for e in events], ['utterance-1'])

    def test_out_of_order_presentation_is_detected(self):
        self.assertCode('presentation_order_gap', lambda: visible_utterances(self.artifact, self.plan, (self.receipt(1),)))

    def test_protected_block_cannot_be_cut_and_whitespace_cannot_disappear(self):
        cut = self.protected[0].start + 4
        plan = replace(self.plan, bubbles=(BubbleRange('one', 0, cut), BubbleRange('two', cut, len(self.body))))
        self.assertCode('protected_text_split', lambda: validate_plan(self.artifact, plan))
        bad = replace(self.plan.bubbles[1], start=self.plan.bubbles[1].start + 1)
        self.assertCode('incomplete_presentation_ranges', lambda: validate_plan(self.artifact, replace(self.plan, bubbles=(self.plan.bubbles[0], bad))))
        self.assertCode('incomplete_presentation_ranges', lambda: validate_plan(self.artifact, replace(self.plan, bubbles=(self.plan.bubbles[0],))))

    def test_required_content_survives_new_input_cancellation(self):
        self.assertCode('required_answer_cancellable', lambda: visible_utterances(self.artifact, self.plan,
                        (self.receipt(0), self.receipt(1, 'cancelled', reason='new_input'))))
        events = visible_utterances(self.artifact, self.plan,
                                   (self.receipt(0), self.receipt(1, 'cancelled', reason='source_revoked')))
        self.assertEqual(len(events), 1)

    def test_optional_ending_can_be_cancelled_without_becoming_dialogue(self):
        end = self.protected[0].end
        self.plan = replace(self.plan, bubbles=(BubbleRange('one', 0, end, 'technical'),
                                               BubbleRange('two', end, len(self.body), required=False)))
        events = visible_utterances(self.artifact, self.plan,
                                   (self.receipt(0), self.receipt(1, 'cancelled', reason='new_input')))
        self.assertEqual(events[0].body, self.body[:end])
        self.assertEqual(len(events), 1)

    def test_duplicate_receipts_and_cross_root_receipts_are_rejected(self):
        self.assertCode('invalid_receipt_utterance', lambda: visible_utterances(self.artifact, self.plan, (self.receipt(0), self.receipt(0))))
        self.assertCode('receipt_identity_mismatch', lambda: visible_utterances(self.artifact, self.plan,
                        (self.receipt(0, ref=replace(self.ref, root_id='other-root')),)))
        self.assertCode('receipt_body_mismatch', lambda: visible_utterances(self.artifact, self.plan,
                        (self.receipt(0, body_sha256=text_hash('another text')),)))

    def test_saved_primary_can_supply_fallback_plan_without_new_call(self):
        plan = replace(self.plan, producer_id=self.draft.draft_id)
        validate_plan(self.draft, plan)
        events = visible_utterances(self.draft, plan, (self.receipt(0), self.receipt(1)))
        self.assertEqual(''.join(e.body for e in events), self.draft.body)

    def test_unknown_usage_and_amount_remain_unknown(self):
        validate_call(self.artifact.call)
        self.assertIsNone(self.artifact.call.input_tokens)
        self.assertIsNone(self.artifact.call.cost_cny)
        validate_call(replace(self.artifact.call, input_tokens=0, output_tokens=0, cost_cny='0'))
        estimate = replace(self.artifact.call, estimated_cost_cny='0.01', price_snapshot_id='official-price-20261116')
        validate_call(estimate)
        self.assertIsNone(estimate.cost_cny)
        self.assertCode('estimate_without_price_snapshot', lambda: validate_call(replace(estimate, price_snapshot_id=None)))
        for amount in ('NaN', '-1', 'Infinity', 'not-money'):
            self.assertCode('invalid_call_cost', lambda: validate_call(replace(self.artifact.call, cost_cny=amount)))
        for value in (True, -1, 1.5):
            self.assertCode('invalid_call_usage', lambda: validate_call(replace(self.artifact.call, input_tokens=value)))

    def test_unsupported_contract_and_nonofficial_provider_are_rejected(self):
        self.assertCode('unsupported_expression_version', lambda: validate_draft(replace(self.draft, version='future-version')))
        self.assertCode('invalid_expression_provider', lambda: validate_call(replace(self.artifact.call, provider='other-service')))


if __name__ == '__main__':
    unittest.main()
