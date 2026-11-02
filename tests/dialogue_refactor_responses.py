"""Canned protocol responses, intentionally independent of Frame/Spec labels."""
import json

ANSWERS = {
    'F01': '轮廓已经有样子了，关节那点细活留着慢慢弄。',
    'F02': '改招牌都快改成变色龙了，先让它歇会儿。',
    'F03': '这闹钟还真是不请自来。',
    'F04': '（84－6×2）÷6＝12，每段12厘米。',
    'F05': '那今天先收在试色这一步。整张的事明天再说。',
    'F06': 'from typing import TypeVar\nT = TypeVar("T")\ndef rotate_left(items: list[T], k: int) -> list[T]:\n    if not items:\n        return []\n    k %= len(items)\n    return items[k:] + items[:k]\n\nassert rotate_left([1, 2, 3], 1) == [2, 3, 1]\nassert rotate_left([1, 2, 3], -1) == [3, 1, 2]\nassert rotate_left([], 5) == []',
}


def answer(case_id):
    return json.dumps({'schema_version': 3, 'behavior_intent': 'explain' if case_id in ('F04', 'F06') else 'respond',
                       'messages': [{'content': ANSWERS[case_id], 'content_kind': 'code' if case_id == 'F06' else 'technical' if case_id == 'F04' else 'chat',
                                     'cancel_if_new_input': False}]}, ensure_ascii=False)
