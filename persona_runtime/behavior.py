"""Deterministic, advisory dialogue cues; never a separate model call."""
import re


RULESET_VERSION = "behavior_resolver_v1"

_RULES = (
    ("correction", re.compile(r"更正|纠正|你弄错|刚才说错|不对，|不是这样|事实是"),
     "优先核对并直接修正事实；承认明确错误，不为维持角色形象而嘴硬。"),
    ("technical_help", re.compile(r"代码|报错|调试|程序|配置|算法|sql|python|函数|脚本|编译|接口|公式|怎么做|如何实现", re.I),
     "按问题需要给出准确、完整的解释；代码、公式、URL 和列表保持可用，不为追求简短而删掉关键步骤。"),
    ("support_request", re.compile(r"我很难受|我好难过|压力很大|我很焦虑|需要安慰|我该怎么办|想哭|好烦"),
     "回应用户明确说出的困扰，避免客服套话；不要据此诊断或假设未表达的心理状态。"),
    ("celebration", re.compile(r"我成功了|通过考试|拿到 offer|拿到offer|升职了|考上了|好消息|太开心了|终于完成了"),
     "针对用户明确分享的好消息作具体、适度的回应，不套用固定夸赞。"),
    ("playful_tone", re.compile(r"哈哈|hhh|233|开玩笑|逗你|😂|🤣"),
     "文本中有轻松或玩笑线索；结合上下文谨慎回应，不自动道歉或把它当成冲突。"),
)


def resolve_behavior(text: str) -> dict:
    """Return weak lexical cues in stable order without claiming user intent."""
    matches = [
        {"act": name, "guidance": guidance}
        for name, pattern, guidance in _RULES
        if pattern.search(text)
    ]
    return {
        "resolver": RULESET_VERSION,
        "signals": matches[:2],
        "uncertainty": "lexical_cues_only_not_a_user_state_or_intent_diagnosis",
    }


def render_behavior(cues: dict, selected_character: dict | None) -> str:
    """Render compact advisory context; exact version provenance stays in the manifest."""
    if not cues["signals"]:
        return ""
    source = (
        "\n本轮表达线索（本地规则的弱提示，不是对用户心理或意图的结论；"
        "不覆盖事实、来源权限、角色边界或关系状态）："
    )
    source += "\n".join("- " + signal["guidance"] for signal in cues["signals"])
    if selected_character:
        source += "\n仅使用本版本明确启用并命中的表达样例；样例是风格参考，不是复述脚本。"
    return source
