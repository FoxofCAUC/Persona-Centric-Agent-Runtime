"""Strict, no-repair validation for semantic reply batches."""
from dataclasses import dataclass
import hashlib
import json
import re

from .models import RuntimeErrorBase

REPLY_MODES = ("legacy_full", "context_full", "context_structured")
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


@dataclass(frozen=True)
class ReplyBatch:
    schema_version: int
    segments: tuple[ReplySegment, ...]
    mode: str
    raw_fingerprint: str

    @property
    def text(self) -> str:
        return "\n\n".join(segment.text for segment in self.segments)


def prompt_contract() -> str:
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
