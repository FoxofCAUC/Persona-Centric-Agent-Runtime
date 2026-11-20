"""Bound new speech fragments without editing their canonical text."""

from unicodedata import category

from .chat_display import SEPARATORS, protected_ranges
from .expression_contract import text_hash
from .expression_sources import protected
from .models import RuntimeErrorBase

MAX_BUBBLE_CHARS = 75
BREAKS = SEPARATORS | set(" \t、：:")
DETAIL_REQUIRED = "reply_bubble_detail_required"
LIMIT_VIOLATION = "reply_bubble_limit"


class BubbleLimitError(RuntimeErrorBase):
    pass


def save_display_boundaries(text, presentation):
    """Attach a display-only plan to a newly settled desktop reply."""
    if len(text) <= MAX_BUBBLE_CHARS:
        return
    ranges = limit_ranges(text, [{"start": 0, "end": len(text)}])
    presentation["desktop_bubbles"] = {
        "body_sha256": text_hash(text),
        "ends": [item["end"] for item in ranges],
    }


def display_bubbles(text, presentation):
    """Read the original plan from a turn. Older turns have no recorded plan."""
    plan = presentation.get("desktop_bubbles")
    if plan is None:
        return None
    if not isinstance(plan, dict) or plan.get("body_sha256") != text_hash(text):
        raise RuntimeErrorBase(LIMIT_VIOLATION)
    ends = plan.get("ends")
    if not isinstance(ends, list) or not ends or ends[-1] != len(text):
        raise RuntimeErrorBase(LIMIT_VIOLATION)
    bubbles, start = [], 0
    for end in ends:
        if type(end) is not int or not start < end <= min(start + MAX_BUBBLE_CHARS, len(text)):
            raise RuntimeErrorBase(LIMIT_VIOLATION)
        bubbles.append(text[start:end])
        start = end
    return bubbles


def limit_ranges(body, ranges):
    """Refine a verified contiguous presentation plan while preserving its text."""
    if all(item["end"] - item["start"] <= MAX_BUBBLE_CHARS for item in ranges):
        return ranges

    spans = protected_ranges(body)
    spans.extend((item["start"], item["end"]) for item in protected(body))

    def safe_cut(offset):
        previous, following = body[offset - 1], body[offset]
        return (
            previous != "\u200d"
            and following != "\u200d"
            and category(following) not in ("Mn", "Mc", "Me")
            and not "\U0001f3fb" <= following <= "\U0001f3ff"
            and not (previous == "\r" and following == "\n")
            and not any(start < offset < end for start, end in spans)
        )

    limited = []
    for original in ranges:
        start, end = original["start"], original["end"]
        cut_at = start
        while end - cut_at > MAX_BUBBLE_CHARS:
            stop = cut_at + MAX_BUBBLE_CHARS
            legal = [
                i for i in range(cut_at + 1, stop + 1)
                if safe_cut(i) and body[cut_at:i].strip() and body[i:end].strip()
            ]
            if not legal:
                raise BubbleLimitError(DETAIL_REQUIRED)
            pause = next((i for i in reversed(legal) if body[i - 1] in BREAKS), legal[-1])
            limited.append(_slice(original, body, cut_at, pause))
            cut_at = pause
        limited.append(_slice(original, body, cut_at, end))
    return limited


def _slice(original, body, start, end):
    item = dict(original, start=start, end=end)
    if start != original["start"]:
        item["delay_ms"] = 0
    if "text" in item:
        item["text"] = body[start:end]
    return item
