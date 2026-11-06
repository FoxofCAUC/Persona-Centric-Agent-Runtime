"""Render C2 review from frozen actual input and normal display segments."""
import hashlib
import re

from experiments import companion_c2 as replay
from persona_runtime.config import atomic_json


def render(root):
    with replay.base.offline():
        owner, plan = replay.verify(root)
        data = replay.data_root(owner)
        lines = ['# 八组匿名合同对照', '',
                 '请按01至08选择更愿意继续交流的一条，可回复A、B、平或无法判断，也可标出出戏处。', '',
                 '以下前文均为人工材料，展示的是两臂实际共同进入模型的部分。'
                 '原始材料每组3至4轮，现有历史策略实际保留前七组各2轮、第八组3轮。', '',
                 '复述、确认与追问是否自然，请按具体语境判断。第六组也请看任务完整性。'
                 '回复采用正常展示处理后的正文，代码按代码块排版。', '']
        for index, case in enumerate(plan['cases'], 1):
            request = replay.read(data / 'preflight' / (case['id'] + '-control') / 'request.json')
            lines.extend([f'## {index:02d}', '', '实际入模的人工前文', ''])
            for message in request['messages'][1:-1]:
                speaker = '用户' if message['role'] == 'user' else '人物'
                lines.extend([speaker + '　' + message['content'], ''])
            lines.extend(['本轮输入', '', request['messages'][-1]['content'], ''])
            for label, arm in plan['anonymous_mapping'][case['id']].items():
                folder = data / 'positions' / (case['id'] + '-' + arm)
                lines.extend(['### ' + label, ''])
                if not (folder / 'terminal.json').is_file():
                    lines.extend(['未完成，无法判断。', ''])
                    continue
                result = replay.read(folder / 'result.json')
                if result['status'] != 'completed':
                    lines.extend(['未完成，无法判断。', ''])
                    continue
                for message in result['display_messages']:
                    text = message['text']
                    if message['content_kind'] == 'code':
                        longest = max((len(m) for m in re.findall(r'`+', text)), default=0)
                        fence = '`' * max(3, longest + 1)
                        lines.extend([fence, text, fence, ''])
                    else:
                        lines.extend([text, ''])
            lines.extend(['选择　　出戏处或原因（可选）', ''])
        output = data / '八组匿名实际输入审核.md'
        output.write_text('\n'.join(lines), encoding='utf-8')
        record = {'version': 'c2-actual-input-review-v1', 'file': output.name,
                  'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                  'source_sha256': replay.sha(__file__), 'physical_calls_added': 0,
                  'history_source': 'frozen_actual_messages', 'reply_source': 'normal_display_messages',
                  'user_review': 'pending'}
        atomic_json(data / 'review-render.json', record)
        return record
