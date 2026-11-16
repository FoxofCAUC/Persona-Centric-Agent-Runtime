"""MaiBot纯组件抽取；E1隔离使用。

Source: Mai-with-u/MaiBot@478f671e64467121fd3e39205c8d3507b5ce0bd6
Files: src/chat/utils/llm_sentence_splitter.py, src/chat/utils/utils.py,
       src/chat/replyer/maisaka_expression_selector.py
License: GNU GPL version 3; docs/licenses/MaiBot-GPL-3.0.txt.
Local changes: 保留句间空白、移除全局配置/LLM依赖、字符串来源ID适配。
"""
from dataclasses import dataclass
from typing import Any, List, Tuple

_NO_SPLIT_MARKER = 'no_split'
MAX_SELECTED_EXPRESSIONS = 5


_PUNCTUATION_TABLE = {
    **{code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)},  # 全角 ASCII（含，。！？～等）
    0x3000: 0x20,
    0x3002: ord("."),
    0x3001: ord(","),
    0x2018: ord("'"),
    0x2019: ord("'"),
    0x201C: ord('"'),
    0x201D: ord('"'),
    0x3010: ord("["),
    0x3011: ord("]"),
    0x300A: ord("<"),
    0x300B: ord(">"),
}


def _is_no_split_marker(payload: str) -> bool:
    """判断模型返回的是否为「不拆分」标记，兼容是否被写成 JSON 字符串。"""
    return payload.strip().strip('"').strip() == _NO_SPLIT_MARKER


def _strip_whitespace(text: str) -> str:
    """去除全部空白字符，并把中英文标点按一一对应归一，用于宽松比较。"""
    return "".join(text.split()).translate(_PUNCTUATION_TABLE)


def _map_sentences_to_original(text: str, sentences: List[str]) -> List[str]:
    """按各句的非空白字符数，把切分位置映射回原文，保留全部句间空白。"""
    counts = [len(_strip_whitespace(sentence)) for sentence in sentences]
    counts = [count for count in counts if count > 0]
    result: List[str] = []
    start = 0
    consumed = 0
    target = 0
    for index, count in enumerate(counts):
        target += count
        if index == len(counts) - 1:
            result.append(text[start:])
            break
        end = start
        while consumed < target:
            if not text[end].isspace():
                consumed += 1
            end += 1
        while end < len(text) and text[end].isspace():
            end += 1
        result.append(text[start:end])
        start = end
    return result


def _extract_trailing_comma(sentence: str) -> Tuple[str, str]:
    """将段尾逗号移入分隔符，逐条发送时省略，压缩合并时恢复。"""
    content = sentence.rstrip("，, \t\r\n")
    separator = sentence[len(content) :]
    if content.strip() and ("，" in separator or "," in separator):
        return content, separator
    return sentence, ""


@dataclass(frozen=True)
class ProcessedResponseSegment:
    """回复后处理产生的单条消息及其发送提示。"""

    text: str
    quote_previous: bool = False
    # 原始文本中紧跟本段的分隔符；多条消息被压缩合并为一条时，用它恢复句间的停顿
    separator: str = ""
    # 纠正内容始终独占一条消息，不依赖是否启用引用。
    is_correction: bool = False


def calculate_typing_time(
    input_string: str,
    chinese_time: float = 0.3,
    english_time: float = 0.15,
    is_emoji: bool = False,
    is_image: bool = False,
    typing_speed: float = 1.0,
) -> float:
    """
    计算输入字符串所需的时间，中文和英文字符有不同的输入时间
        input_string (str): 输入的字符串
        chinese_time (float): 中文字符的输入时间，默认为0.2秒
        english_time (float): 英文字符的输入时间，默认为0.1秒
        is_emoji (bool): 是否为emoji，默认为False
        is_image (bool): 是否为纯图片消息，默认为False

    特殊情况：
    - 如果只有一个中文字符，将使用3倍的中文输入时间
    - 在所有输入结束后，额外加上回车时间0.3秒
    - 如果is_emoji或is_image为True，将使用固定1秒的输入时间，再应用打字速度配置
    """
    chinese_chars = sum("\u4e00" <= char <= "\u9fff" for char in input_string)

    if not (is_emoji or is_image) and chinese_chars == 1 and len(input_string.strip()) == 1:
        return chinese_time * 3 + 0.3  # 加上回车时间

    total_time = 0.0
    for char in input_string:
        total_time += chinese_time if "\u4e00" <= char <= "\u9fff" else english_time
    if is_emoji or is_image:
        total_time = 1

    if typing_speed <= 0:
        return 0
    total_time *= typing_speed



    return total_time  # 加上回车时间


def build_expression_habits_block(selected_expressions: List[dict[str, Any]]) -> str:
    if not selected_expressions:
        return ""
    lines = [
        f"""- 当"{expression['situation']}"时，可以用"{expression['style']}"来表达。"""
        for expression in selected_expressions
    ]
    return "表达习惯参考：\n" + "\n".join(lines)


def normalize_selected_ids(payload: dict, candidates: list[dict]) -> list[str]:
    raw_selected_ids = payload.get("selected_expression_ids", []) if isinstance(payload, dict) else []
    if not isinstance(raw_selected_ids, list):
        return []

    candidate_map = {
        candidate["id"]: candidate
        for candidate in candidates
        if isinstance(candidate.get("id"), str)
    }
    selected_ids: List[int] = []
    for candidate_id in raw_selected_ids:
        if not isinstance(candidate_id, str):
            continue
        if candidate_id not in candidate_map or candidate_id in selected_ids:
            continue
        selected_ids.append(candidate_id)
        if len(selected_ids) >= MAX_SELECTED_EXPRESSIONS:
            break
    return selected_ids
