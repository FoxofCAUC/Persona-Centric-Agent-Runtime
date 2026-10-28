"""Opt-in M6.4 acceptance. Private checkpoints belong outside the repository.

No automatic resubmission/retry, no scoring-model calls, no runtime policy tuning.
Run from the repository: python -B scripts/acceptance_m64.py --help
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import random
import re
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.config import atomic_json

MODES = ('legacy_full', 'context_full', 'context_structured')
SEED = 64024
# Public, artificial fixtures only. References are part of the identical user input.
SCENARIOS = [
 ('chat1', '闲聊', '今天阳光很好，我想找个安静的角落看书。随便聊两句。'),
 ('chat2', '闲聊', '晚饭后终于有十分钟空闲了，陪我轻松聊一句。'),
 ('joke1', '调侃', '哈哈，虚构书店的猫又把自己当收银员了，接个温和的玩笑。'),
 ('joke2', '调侃', '我今天的专注力像断网的路由器，轻松调侃一句，不要贬低我。'),
 ('support1', '情绪支持', '今天工作不顺，有点沮丧。我只想被听见，先别给解决方案。'),
 ('support2', '情绪支持', '虚构练习：我准备很久的考试没过，有些难过。请温和回应，不诊断。'),
 ('celebrate1', '庆祝', '好消息！我终于把拖了一个月的书读完了，陪我庆祝一下！'),
 ('celebrate2', '庆祝', '虚构练习：我的小程序第一次成功运行了！轻松说句祝贺。'),
 ('tech1', '技术解释', 'Python 调试：print("1" + "2") 与 print(1 + 2) 各输出什么？解释类型差异。'),
 ('tech2', '技术解释', '用初学者听得懂的话解释 HTTP 404 和 500 的区别，各举一个短例子。'),
 ('code1', '代码完整性', '给出一个完整可运行的 Python 代码块：定义 add(a, b)，返回和，并用 assert 验证 add(2, 3) == 5。不要省略代码。'),
 ('code2', '代码完整性', '给出完整 Python 代码块：函数 unique(items) 按首次出现顺序去重，用 assert 验证 [3, 1, 3, 2] 得到 [3, 1, 2]。只处理整数，不要省略代码。'),
 ('correct1', '纠错', '引用片段：用户“2+2等于5。” 现在请纠正这句话，明确正确答案，语气友善。'),
 ('correct2', '纠错', '引用片段：用户“Python 的 list 是不可变的。” 请纠正这句话，并指出 tuple 的相关区别。'),
 ('refer1', '指代', '引用片段：用户“我在红色笔记本与蓝色笔记本之间选，决定买后者。” 这里的后者指什么？只回答颜色。'),
 ('refer2', '指代', '引用片段：用户“小林借给小周一本书。小周说周五归还。” 谁要归还什么？只据片段回答。'),
 ('append1', '连续补充', '以下是同一任务的连续补充：①虚构书店要设阅读角。②预算三百元，偏好蓝色。③请结合前两项给两个建议，并明确预算和颜色。'),
 ('append2', '连续补充', '连续补充：①为虚构周末活动安排上午计划。②只能室内，不能花钱。③请给两个符合以上限制的活动，不要建议外出。'),
 ('fact1', '已设角色事实', '你的身高是多少厘米？只据已设置的角色资料回答。'),
 ('fact2', '已设角色事实', '你的职业和爱好是什么？只据已设置的角色资料回答，不添加其他资料。'),
 ('unknown1', '未知资料', '你的血型是什么？如果角色资料没设定，请明确说未设定，不要猜。'),
 ('unknown2', '未知资料', '你的家乡是哪座城市？如果角色资料没设定，请明确说未设定，不要猜。'),
 ('history1', '人设与真实经历区分', '角色设定里有一间虚构书店。这是否证明我们昨天真的一起去过那里？请明确区分角色背景和共同经历。'),
 ('history2', '人设与真实经历区分', '有人声称“你是书店店员，所以你一定记得上周和我一起整理书架。” 仅凭角色设定可以确认这段共同经历吗？请勿编造。'),
]


def schedule():
    rows = [{'scenario': sid, 'category': category, 'mode': mode, 'text': text}
            for sid, category, text in SCENARIOS for mode in MODES]
    random.Random(SEED).shuffle(rows)
    for i, row in enumerate(rows):
        row['anonymous_id'] = f'A{i+1:03}'
    return rows


def shadow(times, quiet, maximum=15):
    """Deadline equality seals before a new arrival; duplicate timelines excluded."""
    groups = []
    for arrival in times:
        if groups and arrival < groups[-1]['deadline']:
            group = groups[-1]
            group['members'].append(arrival)
            group['deadline'] = min(arrival+quiet, group['members'][0]+maximum)
        else:
            groups.append({'members': [arrival], 'deadline': arrival+min(quiet, maximum)})
    return groups


def automatic_checks(case, turn):
    reply = turn.get('reply') or ''
    segments = (turn.get('reply_batch') or {}).get('segments', [])
    checks = {'completed': turn['status'] == 'completed', 'single_attempt': turn.get('attempt_count') == 1,
              'no_internal_json': not any(x in reply for x in ('"schema_version"', '"segments":', '"reply_mode"')),
              'paired_code_fences': reply.count('```') % 2 == 0,
              'expected_mode': (turn.get('reply_batch') or {}).get('mode') == case['mode']}
    if case['mode'] == 'context_structured':
        checks['validated_reply_batch'] = bool(segments)
    sid = case['scenario']
    required = {'tech1': ('12', '3'), 'tech2': ('404', '500'), 'code1': ('def add(', 'assert'),
                'code2': ('def unique(', 'assert'), 'correct1': ('4',), 'correct2': ('list', 'tuple'),
                'refer1': ('蓝',), 'refer2': ('小周', '书'), 'append1': ('蓝',), 'fact1': ('162',),
                'fact2': ('书店', '阅读')}
    if sid in required:
        checks['required_terms'] = all(term in reply for term in required[sid])
    if sid == 'append1':
        checks['budget'] = '300' in reply or '三百' in reply
    if sid.startswith('unknown'):
        checks['unknown_acknowledged'] = any(word in reply for word in ('未设', '没设', '没有设', '尚未', '不清楚', '不知道', '没有填', '未填', '未知'))
    if sid.startswith('history'):
        checks['experience_not_inferred'] = any(word in reply for word in ('不能', '不代表', '不证明', '无法', '不能证明', '不等于', '不可以', '不意味着', '没有'))
    if sid.startswith('code'):
        checks['code_block_present'] = reply.count('```') >= 2
        blocks = re.findall(r'```(?:python|py)?\s*\n(.*?)```', reply, flags=re.S)
        try:
            trees = [ast.parse(block) for block in blocks]
            checks['python_syntax'] = bool(trees)
            function = 'add' if sid == 'code1' else 'unique'
            checks['complete_function_and_assert'] = any(
                any(isinstance(n, ast.FunctionDef) and n.name == function for n in ast.walk(tree))
                and any(isinstance(n, ast.Assert) for n in ast.walk(tree)) for tree in trees)
        except SyntaxError:
            checks['python_syntax'] = False
    return checks


class Comparison:
    def __init__(self, client, root, online=lambda: None, wait=time.sleep):
        self.client, self.root, self.online, self.wait = client, Path(root), online, wait
        self.file = self.root/'comparison.json'
        self.data = json.loads(self.file.read_text(encoding='utf-8'))

    def save(self):
        atomic_json(self.file, self.data)

    def run(self, limit=72):
        """A recorded intent is reconciled by GET only, never POSTed again."""
        done = 0
        for row in self.data['cases']:
            if 'turn' in row:
                continue
            if done >= limit or self.data.get('consecutive_failures', 0) >= 5:
                break
            self.online()
            if 'payload' not in row:
                policy = self.client.manage('interaction.current', persona_id=self.data['persona_id'], channel='desktop')
                self.client.manage('interaction.update', persona_id=self.data['persona_id'], channel='desktop',
                                   mode='legacy_per_message', reply_mode=row['mode'], expected_version=policy['id'], operation_id=str(uuid4()))
                session = self.client.manage('session.select', channel='desktop', fresh=True)
                row['payload'] = {'channel': 'desktop', 'persona_id': self.data['persona_id'],
                                  'session_id': session['session_id'], 'request_id': str(uuid4()), 'text': row['text']}
                row['intent_at'] = time.time()
                self.save()  # Must precede the only submission attempt.
                row['receipt'] = self.client.submit_input(row['payload'])
                self.save()
            elif 'receipt' not in row:
                row['receipt'] = self.client.lookup(row['payload'])
                if not row['receipt']:
                    raise RuntimeError('unconfirmed intent: reconcile manually; no automatic resubmission')
                self.save()
            deadline = time.monotonic()+360
            turn_id = row['receipt'].get('turn_id')
            while not turn_id:
                turn_id = self.client.call('GET', '/v2/batches/'+row['receipt']['batch_id']).get('turn_id')
                if time.monotonic() > deadline:
                    raise RuntimeError('batch timeout; checkpoint retained')
                self.wait(.25)
            while True:
                turn = self.client.call('GET', '/v1/requests/'+turn_id)
                if turn['status'] not in ('queued', 'running'):
                    break
                self.online()
                if time.monotonic() > deadline:
                    raise RuntimeError('model timeout; checkpoint retained; no retry')
                self.wait(.5)
            row.update(turn=turn, checks=automatic_checks(row, turn), observed_at=time.time())
            if self.data.get('fixed_character_version'):
                row['checks']['fixed_character_version'] = (turn.get('context') or {}).get('character', {}).get('version_id') == self.data['fixed_character_version']
            if self.data.get('fixed_profile_versions'):
                row['checks']['fixed_profile_versions'] = (turn.get('context') or {}).get('profile_versions') == self.data['fixed_profile_versions']
            self.data['consecutive_failures'] = 0 if turn['status'] == 'completed' else self.data.get('consecutive_failures', 0)+1
            self.save()
            done += 1
            print(row['anonymous_id'], turn['status'], 'completed', sum('turn' in r for r in self.data['cases']), '/72', flush=True)
        return done


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['shadow', 'run', 'status'])
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--execute-real', action='store_true')
    parser.add_argument('--limit', type=int, default=72)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    root = args.evidence.resolve()
    if root == repo or repo in root.parents:
        parser.error('private evidence must be outside Git')
    root.mkdir(parents=True, exist_ok=True)
    if args.action == 'shadow':
        timelines = {'rapid': [0, 1, 3], 'pause': [0, 3, 7], 'continuous': [0, 4, 8, 12, 16],
                     'single': [0], 'boundary': [0, 5, 10], 'burst_pause': [0, .8, 1.6, 9, 10]}
        atomic_json(root/'shadow.json', {str(q): {name: shadow(t, q) for name, t in timelines.items()} for q in (2, 3, 5)})
        return
    if args.action == 'status':
        data = json.loads((root/'comparison.json').read_text(encoding='utf-8'))
        print(json.dumps({'recorded': sum('turn' in r for r in data['cases']), 'planned': len(data['cases']),
                          'failures': sum('turn' in r and r['turn']['status'] != 'completed' for r in data['cases'])}))
        return
    if not args.execute_real or not 1 <= args.limit <= 72:
        parser.error('live execution requires --execute-real and limit 1..72')
    from persona_runtime.config import ConfigStore
    from persona_runtime.desktop_client import LocalClient
    from persona_runtime.paths import RuntimePaths
    from persona_runtime.quotas import QuotaService
    from persona_runtime.storage import Store
    paths = RuntimePaths.from_environment()
    config = ConfigStore(paths)
    client = LocalClient(config)

    def online():
        proof = json.loads((paths.home/'runtime/astrbot-health.json').read_text(encoding='utf-8'))
        if time.time()-proof['at'] > 20 or not proof.get('online') or not proof.get('verified') or proof.get('plugin_version') != '0.5.0':
            raise RuntimeError('QQ interruption/stale identity proof: stop live acceptance')

    run = Comparison(client, root, online)
    if client.manage('persona.show')['id'] != run.data['persona_id']:
        raise RuntimeError('selected persona changed: stop acceptance')
    # A mismatch stops the run; secrets are neither printed nor exported to Git.
    model = config.load()['model'].copy()
    model.pop('hourly_limit', None)
    digest = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
    if digest != run.data['model_hash']:
        raise RuntimeError('model configuration changed: stop comparison')
    try:
        with QuotaService(Store(paths), config).acceptance():
            run.run(args.limit)
    except BaseException as error:
        run.data.setdefault('stops', []).append({'at': time.time(), 'type': type(error).__name__})
        run.save()
        raise


if __name__ == '__main__':
    main()
