"""Strict, no-repair validation for semantic reply batches."""
from dataclasses import dataclass
import hashlib
import json
import re

from .models import RuntimeErrorBase
from .bubble_contract import V3

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


def prompt_contract(mode="context_structured", expression=None) -> str:
    frozen_version=expression_version(expression,mode)
    if isinstance(expression,str):expression=json.loads(expression)
    if mode == "natural_structured":
        if frozen_version==3:
            return bubble_prompt()
        extra=('整批输入一起理解并作一次回答，不按每条输入机械逐条作答。'
               '普通chat正文必须用无普通句读的短句：不写逗号、句号、问号、感叹号或分号；需要停顿时另起消息。'
               '技术问题前后的普通说明若标为chat也遵循此规则；代码、URL、数值及原文引用中的符号必须保留。'
               '例如确认和追问分别写成两个content：收到啦、你今天忙完了吗。此例仅示范结构，不照抄内容。'
               if expression and expression.get('version')==2 else '')
        return extra+('\n自然私聊回复合同：只输出完整 JSON，不加外层代码围栏。'
                '形状为 {"schema_version":2,"behavior_intent":"respond","messages":'
                '[{"content":"核心回答","content_kind":"chat","cancel_if_new_input":false}]}。'
                '只允许这些字段。messages 为1至3条，每条最多8000字符，总正文最多12000字符。'
                'behavior_intent 仅 respond/clarify/support/explain/acknowledge；content_kind 仅 chat/technical/code/quote。'
                '第一条、技术、代码和引用必须 cancel_if_new_input=false。只有完全省略也不影响任务完成的补充或结束语可标true；'
                '必要答案不得放在可取消消息中。messages每一项是一条独立发送的消息；确认与另外发起的追问应分别放两项，不用空行拼成一项。'
                '不要为凑数拆段。代码、网址、数字与引用必须完整。'
                '日常聊天用自然短句，通常无需句末句号，避免文章腔和多余逗号；技术、代码、引用保留必要标点。'
                '不要装饰性Markdown；禁止指定延迟或调用工具。'
                '历史assistant消息展示的是实际聊天正文，不是本轮输出格式范例。'
                '本轮必须返回上述完整JSON对象，正确转义正文中的换行、双引号与反斜杠，不加任何对象外文字。')
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


def validate_reply(raw: str, mode: str, expression=None) -> ReplyBatch:
    policy_version=expression_version(expression,mode)
    if mode not in REPLY_MODES:
        raise ReplySchemaError("reply_mode_invalid")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ReplySchemaError("reply_schema_invalid")
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if mode == "natural_structured":
        if policy_version==3:
            return _bubbles(raw, fingerprint)
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


def expression_snapshot(mode, policy_id=None, *, version=3):
    if version not in (1,2,3):raise ReplySchemaError('expression_policy_invalid')
    return {"version": version, "presentation": {1:'plain-dialogue-v1',2:'chat-sentences-v1',3:'model-bubbles-v1'}[version], "policy_id": policy_id, "mode": mode,
            "min_wait_ms": 600, "max_wait_ms": 1800, "char_wait_ms": 15,
            "part_chars": 450, "max_messages": V3.max_messages if version==3 else 3}


def expression_version(expression,mode=None):
    if expression is None:return 1
    try:
        value=json.loads(expression) if isinstance(expression,str) else expression
        version=value['version']
        if type(version) is not int or version not in (1,2,3):raise ValueError()
        if version==3 and value!=expression_snapshot(value['mode'],value.get('policy_id'),version=3):raise ValueError()
        if version==3 and mode is not None and value['mode']!=mode:raise ValueError()
        return version
    except (ValueError,TypeError,KeyError):
        raise ReplySchemaError('expression_policy_invalid') from None


def bubble_prompt():
    return ('\n自然聊天气泡合同：只输出完整JSON，不加代码围栏或对象外文字。'
        '形状为 {"schema_version":3,"behavior_intent":"respond","messages":'
        '[{"content":"一个完整聊天表达","content_kind":"chat","cancel_if_new_input":false}]}。'
        f'只允许这些字段，messages为1至{V3.max_messages}项，不凑数；单项最多{V3.item_characters}字符，总正文最多{V3.total_characters}字符。'
        f'behavior_intent仅{"/".join(V3.intents)}，content_kind仅{"/".join(V3.kinds)}。'
        '每一项直接对应一个聊天气泡。综合整批输入回答，不机械逐条回答输入。'
        '普通chat一项围绕一个主要表达目的，通常约8至35字；嗯、好等短回应也可以，不能为字数填充。'
        '独立建议、确认与另起追问分别放不同项；条件与结果、原因与解释可以用逗号放在同一项。'
        '通常省略普通句末句号，保留合理逗号、顿号和表达疑问或情绪的问号、感叹号、省略号、波浪号。'
        '不要把多项建议堆成无标点长串，也不要把每个短语拆碎；不要用空行冒充多个气泡。'
        '示例：确认与追问可分为“看到了”和“刚才怎么啦？”；条件结果可为“雨还没停，等小一点再走”。'
        '多项建议可分为“杯子放右侧”和“线从桌后绕过去”；只需确认时仅“好”即可。示例不照抄。'
        '不能为短句删除条件、事实或必要说明；技术、代码、网址、数值与引用保持完整，保留必要标点。'
        '第一项、technical/code/quote以及所有必需答案必须cancel_if_new_input=false。'
        '仅完全省略也不影响回答完整性的补充或结束语可为true。不要为了凑数添加追问或结束语。'
        '不要装饰性Markdown，不指定延迟、不调用工具。历史assistant正文不是本轮协议范例。'
        '正确转义正文换行、双引号及反斜杠。')


def _bubbles(raw, fingerprint):
    try:
        value=json.loads(raw,object_pairs_hook=_unique_object)
        if not isinstance(value,dict) or set(value)!={'schema_version','behavior_intent','messages'}:raise ValueError()
        if type(value['schema_version']) is not int or value['schema_version']!=V3.schema_version:raise ValueError()
        items=value['messages']
        if not isinstance(items,list) or not 1<=len(items)<=V3.max_messages:raise ValueError()
        valid=value['behavior_intent'] in V3.intents
        checked=[];total=0
        for index,item in enumerate(items):
            if not isinstance(item,dict) or set(item)!={'content','content_kind','cancel_if_new_input'}:raise ValueError()
            body=item['content'];kind=item['content_kind'];cancel=item['cancel_if_new_input']
            if not isinstance(body,str) or not body.strip() or '\x00' in body or len(body)>V3.item_characters or not _valid_fences(body):raise ValueError()
            total+=len(body)
            known=isinstance(kind,str) and kind in V3.kinds
            valid=valid and known and type(cancel) is bool and (not cancel or (index>0 and kind=='chat'))
            checked.append(ReplySegment(body,'plain',kind if known else 'chat',cancel if type(cancel) is bool else False))
        if total>V3.total_characters:raise ValueError()
        if not valid:
            checked=[ReplySegment(s.text,'plain',s.content_kind,False) for s in checked]
        return ReplyBatch(3,tuple(checked),'natural_structured',fingerprint,
            value['behavior_intent'] if valid else 'respond',None if valid else 'safe_text_fallback:invalid_expression_metadata')
    except (ValueError,TypeError,KeyError,UnicodeError):
        raise ReplySchemaError('reply_schema_invalid') from None


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
