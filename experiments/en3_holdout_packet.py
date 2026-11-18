"""Prepare the reviewed Ling holdout inputs locally. No provider client or credentials."""
import argparse
import asyncio
from contextlib import ExitStack
from html import escape
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.dialogue_assembly import load_offline_bundle
from persona_runtime.expression_corpus_sources import document, register_bundle
from persona_runtime.models import PersonaSpec
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.turn_input import render_input
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .fresh_scene_assembly import Assembly

VERSION = 'en3-ling-holdout-inputs-v1'
BRANCHES = {'A': '原 v8 Flash', 'B': '原 v8 Pro', 'R': '结构33新链'}


def assembly_sources():
    repo = Path(__file__).resolve().parents[1]
    paths = ('experiments/en3_holdout_packet.py', 'experiments/fresh_scene_assembly.py',
             'persona_runtime/dialogue_assembly.py','persona_runtime/dialogue_repair.py',
             'persona_runtime/dialogue_display.py','persona_runtime/expression_protocol.py',
             'persona_runtime/turn_input.py','persona_runtime/provider_compat.py')
    return {name:digest((repo/name).read_bytes().replace(b'\r\n',b'\n')) for name in paths}


def require(ok, reason):
    if not ok:
        raise CorpusError('en3_input_' + reason)


def target(item):
    """Replay the desktop input envelope using stable, study-local event IDs."""
    members = []
    for bubble in item['bubbles']:
        eid = str(UUID(fingerprint([VERSION, bubble['message_id'], bubble['text_hash']])[:32], version=4))
        members.append({'id':eid, 'text':bubble['text']})
    require(members and '\n'.join(m['text'] for m in members) == item['text'], 'bubble_projection')
    return render_input(members, batched=True)


class ReplayAssembly:
    """Offline reconstruction. Dispatch authority belongs to the specific study and its reviewed inputs."""
    def __init__(self, state_path, root, bundle_path):
        self.state_path = outside_repository(state_path)
        self.root = outside_repository(root)
        self.bundle_path = outside_repository(bundle_path)
        self.stack = ExitStack()

    def __enter__(self):
        try:
            self.original = Assembly(self.state_path, self.root)
            state = read_private(self.state_path)
            equivalence = read_private(state['el5_equivalence'])
            self.bundle, self.bundle_hash = document(self.bundle_path, equivalence['review_material_sha256'])
            require(bool(self.original.persona) and self.bundle['persona'] == self.original.persona, 'wrong_persona')
            tmp = self.stack.enter_context(TemporaryDirectory(prefix='en3-offline-', dir=self.root))
            self.store = Store(RuntimePaths(Path(tmp)))
            self.store.initialize()
            service = RuntimeService(self.store)
            self.pid = service.create_persona(PersonaSpec.from_dict(self.original.snapshot['persona_spec']))['id']
            service.use_persona(self.pid)
            self.binding = register_bundle(self.store, self.pid, self.bundle_path)
            self.guard()
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def guard(self):
        self.original.guard()
        with self.store.connection() as db:
            self.direct = load_offline_bundle(self.store, db, self.pid, self.binding)

    async def baseline(self, scene_id, turn_no, item, history, branch):
        require(branch in BRANCHES and len(history) + 1 == turn_no and item['turn_number'] == turn_no, 'position')
        rows = [(target(old), body, old['accepted_at'], spoken_at) for old, body, spoken_at in history]
        model, wire = await self.original.baseline(scene_id, turn_no, target(item), item['accepted_at'], rows, 'B')
        if branch != 'A':
            return model, wire
        model = dict(model, name='deepseek-flash')
        return model, build_request(model, wire['messages'])[0]

    def candidate_material(self, scene_id, turn_no, item, history, baseline):
        model, wire = baseline
        require(model['name'] == 'deepseek-v4-pro' and wire == build_request(model, wire['messages'])[0], 'candidate_base')
        selected = []
        pairs = iter(history)
        previous = wire['messages'][1:-1]
        for user, spoken in zip(previous[::2], previous[1::2], strict=True):
            hit = next(((old, body, stamp) for old, body, stamp in pairs
                        if user == {'role':'user', 'content':target(old)}
                        and spoken == {'role':'assistant','content':body}), None)
            require(hit is not None, 'unconfirmed_history')
            old, body, stamp = hit
            selected.append((old['text'], body, old['bubbles'][0]['accepted_at'], stamp))
        turn = {'id': f'{scene_id}.{turn_no:02d}', 'persona_id':self.pid}
        material = self.direct.material(turn, self.binding, model, wire['messages'], 'main', selected,
                                        target(item), item['accepted_at'])
        material['current'] = item['text']
        material['current_lines'] = [[b['text'], b['accepted_at']] for b in item['bubbles']]
        return material

    def request(self, stage, material, thought=None, selected_ids=()):
        require(stage in ('understanding','selection','reply'), 'stage')
        if stage != 'understanding':
            require(isinstance(thought, str) and bool(thought.strip()), 'first_understanding_required')
        turn = {'id':material['turn_id'], 'persona_id':self.pid}
        return self.direct.request(stage, turn, material, thought, list(selected_ids))

    def parse_selection(self, body, material):
        return self.direct.parse_selection(body, material)

    def projection(self, body, material):
        return self.direct.projection(body, material)

    def segments(self, body, material):
        return self.direct.segments(body, material)


def first_request(branch, stage, model_wire):
    model, wire = model_wire
    require(wire == build_request(model, wire['messages'])[0] and not wire.get('tools'), 'wire_contract')
    require('encrypted_key' not in model and 'api_key' not in model, 'model_secret')
    return {'branch':branch, 'stage':stage, 'endpoint':model['base_url'], 'request_hash':fingerprint(wire), 'wire':wire}


async def opening_requests(assembly, scene):
    item = scene['turns'][0]
    flash = await assembly.baseline(scene['id'], 1, item, [], 'A')
    pro = await assembly.baseline(scene['id'], 1, item, [], 'B')
    require(flash[1]['messages'] == pro[1]['messages'], 'unequal_baselines')
    material = assembly.candidate_material(scene['id'], 1, item, [], pro)
    return [first_request('A', 'body', flash), first_request('B', 'body', pro),
            first_request('R', 'understanding', assembly.request('understanding', material))]


def details(caption, content):
    return '<details><summary>'+escape(caption)+'</summary><pre>'+escape(content)+'</pre></details>'


def report(materials, contract, bundle, root):
    model_count = contract['planned_dialogue_requests']
    count = sum(len(t['bubbles']) for s in materials['scenes'] for t in s['turns'])
    bounds = [gap for scene in materials['scenes'] for gap in scene['omitted_counterpart_turns']]
    with_peer = sum(bool(gap['kinds']) for gap in bounds)
    parts = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1"><title>角色 · E.N3 留出输入审阅</title>'
             '<style>body{max-width:54rem;margin:2rem auto;padding:0 1rem;font:16px/1.65 system-ui;color:#20352d}'
             'section{margin:2.5rem 0}h1,h2{line-height:1.3}article,details{border-top:1px solid #ccd7cf;padding:.55rem 0}'
             'summary{cursor:pointer}pre,.bubble{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}'
             '.bubble{background:#edf3ee;padding:.52rem .8rem;margin:.25rem 0}.meta{font-size:.88rem;color:#52675b}'
             'dt{font-weight:bold}dd{margin-bottom:.6rem}code{overflow-wrap:anywhere}</style></head><body><main>',
             '<h1>角色 · E.N3 留出输入审阅</h1>',
             f'<p>两组独立私聊，每组连续六轮。以下 {count} 条真人气泡来自最终留出分区，按原顺序和时间供三条链共用。'
             '每组从空会话开始，续聊时各链只接续本链已经呈现的发言。原对话中另一方的回答留在本机。</p>',
             f'<p>两组共有 {len(bounds)} 处轮间间隔。{with_peer} 处原聊天有对方接话，'
             f'另 {len(bounds)-with_peer} 处是同一人暂停后继续发言，按桌面现行的收束时间划为下一轮。'
             '实测时每轮由模型回应一次；下轮用户话语仍取当时的原文。</p>',
             '<p>拟用提供商为官方 DeepSeek。两条原 v8 链分别使用 Flash 和 Pro，各生成12次人物正文。'
             '新链每轮由 Flash 理解、Flash 选材，再由 Pro 生成人物正文。'
             f'上述对话步骤共 {model_count} 个请求位置，实际用量、等待及费用由首次回执记录。'
             '本页生成时尚未向提供商发送请求。</p>',
             '<p>本次范围为人物语言对照。三条链都关闭 Jev 和搜索；受控资料包为空。'
             '这两组会话的结论适用于所选话题。资料成功、资料缺失和真实联网的效果另行验证。</p>']
    for scene in materials['scenes']:
        parts.append('<section><h2>'+escape(scene['id']+' · '+scene['title'])+'</h2>')
        for turn in scene['turns']:
            parts.append(f'<article><strong>第 {turn["turn_number"]} 轮</strong>')
            for bubble in turn['bubbles']:
                parts.append('<div class="meta">'+escape(bubble['accepted_at'])+'</div><div class="bubble">'+escape(bubble['text'])+'</div>')
            if turn['turn_number'] < len(scene['turns']):
                gap = scene['omitted_counterpart_turns'][turn['turn_number']-1]
                label = '原聊天中对方有接话' if gap['kinds'] else '原聊天中同一人停顿后续发'
                parts.append('<div class="meta">'+label+f'，距下一轮 {gap["seconds"]} 秒</div>')
            parts.append('</article>')
        parts.append('<h3>首轮完整请求</h3>')
        for request in contract['opening_requests'][scene['id']]:
            where = '理解' if request['stage'] == 'understanding' else '正文'
            parts.append(details(BRANCHES[request['branch']]+' · '+where, json.dumps(request['wire'], ensure_ascii=False, indent=2)))
        parts.append('</section>')
    parts += ['<section><h2>后续请求如何装配</h2><dl>',
              '<dt>原 v8 两链</dt><dd>复用同一版角色的人物资料和 v8 上下文选择，分别写入各自已经呈现的回复。</dd>',
              '<dt>新链的理解与选材</dt><dd>理解可见当前真人气泡及所选的本链已说前文。选择器接收当前原文、本轮时间和首次理解回执。'
              '可在冻结清单内选择零至五条表达。</dd>',
              '<dt>新链的正文</dt><dd>以本链的 v8 Pro 输入为底稿，按已审装配加入本轮首次理解和适用表达，'
              '本轮真人输入位于末尾。已出现的不可用响应、失败和未知均单独记入审阅。</dd></dl>',
              details('选择器系统消息及候选表达全文', bundle['selector_system']['content']),
              details('理解与选材所用格式原文', json.dumps({'flash_user':bundle['flash_user'], 'variables':bundle['variables']},
                                                    ensure_ascii=False, indent=2)),
              '</section><section><h2>本机核对</h2>',
              '<p>原文与来源清单位于 <code>'+escape(str(root/'materials.private.json'))+'</code>。'
              '请求、三条链的版本和校验值位于 <code>'+escape(str(root/'input-contract.private.json'))+'</code>。</p>',
              '<p>来源分区为 <code>final_holdout</code>，角色材料摘要为 <code>'+escape(contract['source_bundle_sha256'])+'</code>。</p>',
              '</section></main></body></html>']
    return '\n'.join(parts)


async def prepare(state_path, root, bundle_path):
    from .en3_holdout_material import verify_materials
    from .en3_holdout_blind import freeze
    state_path = outside_repository(state_path)
    root = outside_repository(root)
    materials, checksum = document(root / 'materials.private.json')
    state = read_private(state_path)
    require(materials['split'] == 'final_holdout' and len(materials['scenes']) == 2
            and all(len(scene['turns']) == 6 for scene in materials['scenes']), 'holdout_shape')
    with ReplayAssembly(state_path,root,bundle_path) as assembly:
        previous = read_private(outside_repository(state['fresh_scene_study_root']) / 'materials.private.json')
        verify_materials(DatasetCorpusStore(assembly.original.plan['corpus_root']), materials, assembly.original.plan, previous)
        openings = {s['id']:await opening_requests(assembly,s) for s in materials['scenes']}
        contract = {'version':VERSION,'study_id':'en3-ling-01','persona':assembly.original.persona,'split':materials['split'],
                    'branches':BRANCHES,'scene_count':2,'turns_per_branch':12,'planned_dialogue_requests':60,
                    'materials_sha256':checksum,'materials_hash':materials['materials_hash'],
                    'source_bundle_path':str(assembly.bundle_path),'source_bundle_sha256':assembly.bundle_hash,
                    'assembly_sources':assembly_sources(),'persona_spec_sha256':assembly.original.snapshot['spec_utf8_sha256'],
                    'character_version':assembly.original.snapshot['character_version'],
                    'state_path':str(state_path),'root':str(root),
                    'blind_key_sha256':freeze(root, materials), 'opening_requests':openings,
                    'raw_peer_replies_in_requests':False,'online_retrieval':False,
                    'jev_enabled':False,'controlled_materials':[],
                    'receipt_dependencies':{'A.body':['A.presented_history'],'B.body':['B.presented_history'],
                         'R.understanding':['R.presented_history'],'R.selection':['R.understanding'],
                         'R.body':['R.presented_history','R.understanding','R.selection']},
                    'study_use_approval':'required','formal_activation':False}
        page = report(materials, contract, assembly.bundle, root)
        contract['inputs_html_sha256'] = digest(page.encode('utf-8'))
        save_once(root/'input-contract.private.json', contract)
        save_text_once(root/'inputs.html', page)
        return contract


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--state',required=True,type=Path)
    cli.add_argument('--root',required=True,type=Path)
    cli.add_argument('--source-bundle',required=True,type=Path)
    args = cli.parse_args()
    result=asyncio.run(prepare(args.state,args.root,args.source_bundle))
    print(json.dumps({'scenes':result['scene_count'],'turns_per_branch':result['turns_per_branch'],
                      'new_provider_requests':0},ensure_ascii=False))
