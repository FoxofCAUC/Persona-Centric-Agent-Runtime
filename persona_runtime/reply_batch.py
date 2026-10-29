"""Strict, no-repair validation for semantic reply batches."""
from dataclasses import dataclass
import hashlib
import json
import re

from .models import RuntimeErrorBase

REPLY_MODES = ("legacy_full", "context_full", "context_structured", "natural_structured")
SCHEMA_VERSION = 1
MAX_SEGMENTS = 3
MAX_SEGMENT_CHARS = 8000
MAX_TOTAL_CHARS = 12000
FORMATS = ("plain", "markdown")


class ReplySchemaError(RuntimeErrorBase):
    """A generated structured reply failed validation."""


@dataclass(frozen=True)
class ReplySegment:
    text: str
    format: str
    content_kind: str = "chat"
    cancel_if_new_input: bool = False
    wait_ms: int = 0


@dataclass(frozen=True)
class ReplyBatch:
    schema_version: int
    segments: tuple[ReplySegment, ...]
    mode: str
    raw_fingerprint: str
    behavior_intent: str | None = None
    fallback_reason: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(segment.text for segment in self.segments)


def prompt_contract(mode="context_structured") -> str:
    if mode == "natural_structured":
        return ('\n自然私聊回复合同：只输出完整 JSON，不加外层代码围栏。'
                '形状为 {"schema_version":2,"behavior_intent":"respond","messages":'
                '[{"content":"核心回答","content_kind":"chat","cancel_if_new_input":false}]}。'
                '只允许这些字段。messages 为1至3条，每条最多8000字符，总正文最多12000字符。'
                'behavior_intent 仅 respond/clarify/support/explain/acknowledge；content_kind 仅 chat/technical/code/quote。'
                '第一条、技术、代码和引用必须 cancel_if_new_input=false。只有完全省略也不影响任务完成的补充或结束语可标true；'
                '必要答案不得放在可取消消息中。不要为凑数拆段。代码、网址、数字与引用必须完整。'
                '日常聊天用自然短句，通常无需句末句号，避免文章腔和多余逗号；技术、代码、引用保留必要标点。'
                '不要装饰性Markdown；禁止指定延迟或调用工具。')
    return (
        "\n回复格式合同：只返回一个 JSON 对象，不要代码围栏、前言或对象之外的说明。"
        '固定形状：{"schema_version":1,"segments":[{"text":"回复内容","format":"plain"}]}。'
        "segments 必须有 1 到 3 项；每项只能含 text 与 format。text 为非空字符串，"
        "format 只能是 plain 或 markdown。总正文不超过 12000 字符、每段不超过 8000 字符。"
        "按语义决定分段，不必凑满三段；代码、公式、URL、列表和引用要保持完整，"
        "不要在代码围栏、URL 或公式中间切段。"
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object field")
        result[key] = value
    return result


def _valid_fences(text: str) -> bool:
    """Reject visibly unclosed Markdown fences without rewriting their contents."""
    for marker in ("```", "~~~"):
        fences = re.findall(r"(?m)^[ \t]*" + re.escape(marker) + r"+[^\n]*$", text)
        if len(fences) % 2:
            return False
    return True


def validate_reply(raw: str, mode: str) -> ReplyBatch:
    if mode not in REPLY_MODES:
        raise ReplySchemaError("reply_mode_invalid")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ReplySchemaError("reply_schema_invalid")
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if mode == "natural_structured":
        return _natural(raw, fingerprint)
    if mode != "context_structured":
        return ReplyBatch(SCHEMA_VERSION, (ReplySegment(raw, "plain"),), mode, fingerprint)

    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeError):
        raise ReplySchemaError("reply_schema_invalid") from None
    if not isinstance(value, dict) or set(value) != {"schema_version", "segments"}:
        raise ReplySchemaError("reply_schema_invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ReplySchemaError("reply_schema_invalid")
    segments = value["segments"]
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
        raise ReplySchemaError("reply_schema_invalid")

    checked = []
    total = 0
    for item in segments:
        if not isinstance(item, dict) or set(item) != {"text", "format"}:
            raise ReplySchemaError("reply_schema_invalid")
        text, fmt = item["text"], item["format"]
        if (not isinstance(text, str) or not text.strip() or "\x00" in text
                or len(text) > MAX_SEGMENT_CHARS or fmt not in FORMATS or not _valid_fences(text)):
            raise ReplySchemaError("reply_schema_invalid")
        total += len(text)
        if total > MAX_TOTAL_CHARS:
            raise ReplySchemaError("reply_schema_invalid")
        checked.append(ReplySegment(text, fmt))
    return ReplyBatch(SCHEMA_VERSION, tuple(checked), mode, fingerprint)


def expression_snapshot(mode, policy_id=None):
    return {"version": 1, "policy_id": policy_id, "mode": mode,
            "min_wait_ms": 600, "max_wait_ms": 1800, "char_wait_ms": 15,
            "part_chars": 450, "max_messages": 3}


def _natural(raw, fingerprint):
    """Metadata may fall back; ambiguous or incomplete bodies never do."""
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
        version = value.get("schema_version") if isinstance(value, dict) else None
        if type(version) is not int or version not in (1, 2):
            raise ValueError()
        outer = {"schema_version", "segments"} if version == 1 else {"schema_version", "behavior_intent", "messages"}
        if set(value) != outer:
            raise ValueError()
        items = value["segments" if version == 1 else "messages"]
        if not isinstance(items, list) or not 1 <= len(items) <= 3:
            raise ValueError()
        bodies = []
        valid = version == 2 and value["behavior_intent"] in ("respond", "clarify", "support", "explain", "acknowledge")
        checked = []
        for index, item in enumerate(items):
            keys = {"text", "format"} if version == 1 else {"content", "content_kind", "cancel_if_new_input"}
            if not isinstance(item, dict) or set(item) != keys:
                raise ValueError()
            body = item["text" if version == 1 else "content"]
            if not isinstance(body, str) or not body.strip() or "\x00" in body or len(body) > 8000 or not _valid_fences(body):
                raise ValueError()
            bodies.append(body)
            kind = item.get("content_kind", "chat")
            cancel = item.get("cancel_if_new_input", False)
            valid = valid and kind in ("chat", "technical", "code", "quote") and type(cancel) is bool
            valid = valid and (not cancel or (index > 0 and kind == "chat"))
            checked.append(ReplySegment(body, "plain", kind, cancel,
                                       min(1800, 600 + 15 * len(bodies[index-1])) if index else 0))
        if sum(map(len, bodies)) > 12000:
            raise ValueError()
        if valid:
            return ReplyBatch(2, tuple(checked), "natural_structured", fingerprint, value["behavior_intent"])
        combined = "\n\n".join(bodies)
        # A fallback is itself a valid single message, not a capacity bypass.
        if len(combined) > 8000:
            raise ValueError()
        return ReplyBatch(2, (ReplySegment(combined, "plain"),), "natural_structured", fingerprint,
                          "respond", "safe_text_fallback:invalid_expression_metadata")
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ReplySchemaError("reply_schema_invalid") from None
