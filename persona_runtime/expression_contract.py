"""E0表达合同及纯校验；运行派发、落盘和桌面呈现由后续阶段接入。"""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json


VERSION = 'pcar-expression-v1'


class ExpressionContractError(ValueError):
    """错误码不携带正文，便于在任务账目中记录失败。"""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _require(ok, code):
    if not ok:
        raise ExpressionContractError(code)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _integer(value):
    return type(value) is int and value >= 0


@dataclass(frozen=True)
class InteractionRef:
    root_id: str
    persona_id: str
    session_id: str
    scope_id: str
    policy_version: str


@dataclass(frozen=True)
class CallReceipt:
    attempt_id: str
    stage: str
    provider: str
    model: str
    state: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_cny: str | None = None
    estimated_cost_cny: str | None = None
    price_snapshot_id: str | None = None


@dataclass(frozen=True)
class ProtectedSpan:
    span_id: str
    start: int
    end: int
    kind: str


@dataclass(frozen=True)
class MainDraftReceipt:
    ref: InteractionRef
    draft_id: str
    body: str
    body_sha256: str
    storage_receipt_id: str
    call: CallReceipt
    protected: tuple[ProtectedSpan, ...] = ()
    version: str = VERSION


@dataclass(frozen=True)
class DialogueLine:
    ref: InteractionRef
    event_id: str
    speaker: str
    body: str
    receipt_id: str
    phase: str = 'reply'


@dataclass(frozen=True)
class ScopedText:
    source_id: str
    source_version: str
    persona_id: str
    scope_id: str
    grant_id: str
    body: str
    body_sha256: str
    state: str = 'active'


@dataclass(frozen=True)
class ExpressionContext:
    ref: InteractionRef
    context_id: str
    draft_id: str
    draft_sha256: str
    grant_id: str
    grant_version: str
    current_user: DialogueLine
    style: tuple[ScopedText, ...] = ()
    history: tuple[DialogueLine, ...] = ()
    already_spoken: tuple[DialogueLine, ...] = ()
    candidates: tuple[ScopedText, ...] = ()
    purpose: str = 'expression'
    version: str = VERSION


def context_hash(context: ExpressionContext) -> str:
    raw = json.dumps(asdict(context), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return text_hash(raw)


@dataclass(frozen=True)
class ExpressionArtifact:
    ref: InteractionRef
    artifact_id: str
    draft_id: str
    draft_sha256: str
    context_id: str
    context_sha256: str
    body: str
    body_sha256: str
    call: CallReceipt
    storage_receipt_id: str
    protected: tuple[ProtectedSpan, ...] = ()
    selected_expression_ids: tuple[str, ...] = ()
    version: str = VERSION


@dataclass(frozen=True)
class BubbleRange:
    utterance_id: str
    start: int
    end: int
    kind: str = 'chat'
    required: bool = True
    delay_ms: int = 0


@dataclass(frozen=True)
class PresentationPlan:
    ref: InteractionRef
    plan_id: str
    producer_id: str
    body_sha256: str
    publication_id: str
    timing_policy_version: str
    bubbles: tuple[BubbleRange, ...]
    version: str = VERSION


@dataclass(frozen=True)
class PresentationReceipt:
    ref: InteractionRef
    plan_id: str
    utterance_id: str
    receipt_id: str
    state: str
    body_sha256: str
    observed_at_ms: int
    reason: str = ''
    version: str = VERSION


@dataclass(frozen=True)
class UtteranceEvent:
    ref: InteractionRef
    utterance_id: str
    producer_id: str
    plan_id: str
    presentation_receipt_id: str
    body: str
    body_sha256: str
    ordinal: int
    version: str = VERSION


def _ref(ref):
    _require(all(_text(v) for v in asdict(ref).values()), 'invalid_interaction_ref')


def validate_call(call: CallReceipt):
    _require(all(_text(v) for v in (call.attempt_id, call.model)), 'invalid_call_identity')
    _require(call.stage in ('primary', 'expression'), 'invalid_call_stage')
    _require(call.provider == 'deepseek_official', 'invalid_expression_provider')
    _require(call.state in ('completed', 'failed', 'unknown', 'cancelled'), 'invalid_call_state')
    for value in (call.input_tokens, call.output_tokens):
        _require(value is None or _integer(value), 'invalid_call_usage')
    for value in (call.cost_cny, call.estimated_cost_cny):
        if value is None:
            continue
        _require(_text(value), 'invalid_call_cost')
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise ExpressionContractError('invalid_call_cost') from None
        _require(amount.is_finite() and amount >= 0, 'invalid_call_cost')
    _require(call.estimated_cost_cny is None or _text(call.price_snapshot_id), 'estimate_without_price_snapshot')


def _body(body, digest):
    _require(_text(body), 'empty_body')
    _require(text_hash(body) == digest, 'body_hash_mismatch')


def _spans(body, spans):
    previous_end = 0
    ids = set()
    for span in spans:
        _require(_text(span.span_id) and span.span_id not in ids, 'invalid_protected_identity')
        _require(span.kind in ('code', 'command', 'formula', 'quote', 'verbatim'), 'invalid_protected_kind')
        _require(_integer(span.start) and _integer(span.end) and
                 previous_end <= span.start < span.end <= len(body), 'invalid_protected_range')
        ids.add(span.span_id)
        previous_end = span.end


def validate_draft(draft: MainDraftReceipt):
    _require(draft.version == VERSION, 'unsupported_expression_version')
    _ref(draft.ref)
    _require(_text(draft.draft_id) and _text(draft.storage_receipt_id), 'draft_not_receipted')
    _body(draft.body, draft.body_sha256)
    validate_call(draft.call)
    _require(draft.call.stage == 'primary' and draft.call.state == 'completed', 'draft_not_complete')
    _spans(draft.body, draft.protected)


def _same_scope(left, right):
    return (left.persona_id, left.session_id, left.scope_id) == (right.persona_id, right.session_id, right.scope_id)


def _dialogue(line, ref):
    _ref(line.ref)
    _require(_same_scope(line.ref, ref), 'dialogue_scope_mismatch')
    _require(all(_text(v) for v in (line.event_id, line.body, line.receipt_id)), 'dialogue_without_receipt')
    _require(line.speaker in ('user', 'assistant'), 'invalid_dialogue_speaker')
    _require(line.phase in ('reply', 'search_notice'), 'invalid_dialogue_phase')
    _require(line.phase != 'search_notice' or line.speaker == 'assistant', 'invalid_search_notice')


def validate_context(draft: MainDraftReceipt, context: ExpressionContext):
    validate_draft(draft)
    _require(context.version == VERSION and context.purpose == 'expression', 'invalid_context_purpose')
    _require(context.ref == draft.ref, 'context_identity_mismatch')
    _require((context.draft_id, context.draft_sha256) == (draft.draft_id, draft.body_sha256), 'context_draft_mismatch')
    _require(all(_text(v) for v in (context.context_id, context.grant_id, context.grant_version)), 'context_without_grant')
    _dialogue(context.current_user, context.ref)
    _require(context.current_user.ref == context.ref and context.current_user.speaker == 'user', 'invalid_current_user')
    event_ids = {context.current_user.event_id}
    for line in (*context.history, *context.already_spoken):
        _dialogue(line, context.ref)
        _require(line.event_id not in event_ids, 'duplicate_dialogue_event')
        event_ids.add(line.event_id)
    for line in context.already_spoken:
        _require(line.ref == context.ref and line.speaker == 'assistant', 'invalid_already_spoken')
    source_ids = set()
    for source in (*context.style, *context.candidates):
        _require(all(_text(v) for v in (source.source_id, source.source_version, source.grant_id)), 'source_without_grant')
        _require((source.persona_id, source.scope_id) == (context.ref.persona_id, context.ref.scope_id), 'source_scope_mismatch')
        _require(source.state == 'active', 'source_not_active')
        _require(source.source_id not in source_ids, 'duplicate_expression_source')
        _body(source.body, source.body_sha256)
        source_ids.add(source.source_id)


def validate_artifact(draft: MainDraftReceipt, context: ExpressionContext, artifact: ExpressionArtifact):
    validate_context(draft, context)
    _require(artifact.version == VERSION and artifact.ref == draft.ref, 'artifact_identity_mismatch')
    _require(_text(artifact.artifact_id) and artifact.artifact_id != draft.draft_id, 'invalid_artifact_identity')
    _require(_text(artifact.storage_receipt_id), 'artifact_not_receipted')
    _require((artifact.draft_id, artifact.draft_sha256) == (draft.draft_id, draft.body_sha256), 'artifact_draft_mismatch')
    _require((artifact.context_id, artifact.context_sha256) == (context.context_id, context_hash(context)), 'artifact_context_mismatch')
    _body(artifact.body, artifact.body_sha256)
    validate_call(artifact.call)
    _require(artifact.call.stage == 'expression' and artifact.call.state == 'completed', 'artifact_not_complete')
    _require(artifact.call.attempt_id != draft.call.attempt_id, 'expression_reuses_primary_attempt')
    _spans(artifact.body, artifact.protected)
    originals = {s.span_id: (s.kind, draft.body[s.start:s.end]) for s in draft.protected}
    preserved = {s.span_id: (s.kind, artifact.body[s.start:s.end]) for s in artifact.protected}
    _require(preserved == originals, 'protected_text_changed')
    candidate_ids = {source.source_id for source in context.candidates}
    selected = artifact.selected_expression_ids
    _require(len(set(selected)) == len(selected) and set(selected) <= candidate_ids, 'invalid_selected_expression_ids')


def validate_plan(producer: MainDraftReceipt | ExpressionArtifact, plan: PresentationPlan):
    """调用方先校验完整产物；呈现计划引用原文字符区间，保留全部空白。"""
    _require(plan.version == VERSION and producer.version == VERSION, 'unsupported_expression_version')
    if isinstance(producer, MainDraftReceipt):
        validate_draft(producer)
    else:
        validate_call(producer.call)
        _require(_text(producer.storage_receipt_id), 'artifact_not_receipted')
        _require(producer.call.stage == 'expression', 'invalid_call_stage')
    _ref(producer.ref)
    _body(producer.body, producer.body_sha256)
    _spans(producer.body, producer.protected)
    _require(producer.call.state == 'completed', 'producer_not_complete')
    producer_id = producer.draft_id if isinstance(producer, MainDraftReceipt) else producer.artifact_id
    _require(plan.ref == producer.ref and plan.producer_id == producer_id, 'plan_identity_mismatch')
    _require(plan.body_sha256 == producer.body_sha256, 'plan_body_mismatch')
    _require(all(_text(v) for v in (plan.plan_id, plan.publication_id, plan.timing_policy_version)), 'invalid_plan_identity')
    _require(bool(plan.bubbles), 'empty_presentation_plan')
    end = 0
    ids = set()
    for index, bubble in enumerate(plan.bubbles):
        _require(_text(bubble.utterance_id) and bubble.utterance_id not in ids, 'duplicate_utterance_identity')
        _require(_integer(bubble.start) and _integer(bubble.end) and
                 bubble.start == end < bubble.end <= len(producer.body), 'incomplete_presentation_ranges')
        _require(bubble.kind in ('chat', 'technical', 'code', 'quote'), 'invalid_bubble_kind')
        _require(type(bubble.required) is bool and _integer(bubble.delay_ms), 'invalid_presentation_budget')
        _require(bubble.required or (index > 0 and bubble.kind == 'chat'), 'required_answer_cancellable')
        _require(all(not (span.start < bubble.end < span.end) for span in producer.protected), 'protected_text_split')
        ids.add(bubble.utterance_id)
        end = bubble.end
    _require(end == len(producer.body), 'incomplete_presentation_ranges')


def visible_utterances(producer: MainDraftReceipt | ExpressionArtifact, plan: PresentationPlan,
                       receipts: tuple[PresentationReceipt, ...]) -> tuple[UtteranceEvent, ...]:
    """从呈现回执投影实际发言；调用方负责先校验产物并事务持久化事件。"""
    validate_plan(producer, plan)
    by_id = {}
    receipt_ids = set()
    bubbles = {item.utterance_id: item for item in plan.bubbles}
    for receipt in receipts:
        _require(receipt.version == VERSION, 'unsupported_expression_version')
        _require(receipt.ref == plan.ref and receipt.plan_id == plan.plan_id, 'receipt_identity_mismatch')
        _require(receipt.utterance_id in bubbles and receipt.utterance_id not in by_id, 'invalid_receipt_utterance')
        _require(_text(receipt.receipt_id) and receipt.receipt_id not in receipt_ids, 'duplicate_presentation_receipt')
        _require(receipt.state in ('presented', 'cancelled', 'unknown'), 'invalid_presentation_state')
        _require(_integer(receipt.observed_at_ms), 'invalid_presentation_time')
        bubble = bubbles[receipt.utterance_id]
        _require(receipt.body_sha256 == text_hash(producer.body[bubble.start:bubble.end]), 'receipt_body_mismatch')
        if receipt.state == 'cancelled':
            _require(receipt.reason in ('new_input', 'source_revoked', 'permission_revoked', 'session_closed'), 'invalid_cancellation_reason')
            _require(receipt.reason != 'new_input' or not bubble.required, 'required_answer_cancellable')
        by_id[receipt.utterance_id] = receipt
        receipt_ids.add(receipt.receipt_id)
    visible = []
    blocked = False
    for ordinal, bubble in enumerate(plan.bubbles):
        receipt = by_id.get(bubble.utterance_id)
        if receipt is None or receipt.state == 'unknown':
            blocked = True
            continue
        if receipt.state == 'cancelled':
            if receipt.reason != 'new_input':
                blocked = True
            continue
        _require(not blocked, 'presentation_order_gap')
        body = producer.body[bubble.start:bubble.end]
        visible.append(UtteranceEvent(plan.ref, bubble.utterance_id, plan.producer_id, plan.plan_id,
                                      receipt.receipt_id, body, text_hash(body), ordinal))
    return tuple(visible)
