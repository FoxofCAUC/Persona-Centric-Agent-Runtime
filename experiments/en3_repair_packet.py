"""Freeze the new Ling study and render its exact input materials locally."""
import argparse
import asyncio
from html import escape
import json
from pathlib import Path

from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.dialogue_assembly import load_bundle, load_offline_bundle
from persona_runtime.expression_corpus_sources import document, register_bundle
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .en3_holdout_packet import ReplayAssembly, assembly_sources as baseline_sources, details, first_request, target


VERSION = 'en3-ling-repair-inputs-v2'
STUDY = 'en3-ling-repair-02'
BRANCHES = {'B':'原 v8 Pro', 'O':'修复前结构33', 'R':'修复候选'}


def require(condition, reason):
    if not condition:
        raise CorpusError('en3_repair_' + reason)


def sources():
    repo = Path(__file__).resolve().parents[1]
    paths = ['experiments/en3_repair_packet.py','experiments/en3_repair_trial.py',
             'experiments/en3_expanded_material.py','experiments/en3_holdout_trial.py',
             'persona_runtime/dialogue_portable.py']
    return baseline_sources() | {p:digest((repo/p).read_bytes().replace(b'\r\n',b'\n')) for p in paths}


def original_inputs(state):
    old_root = outside_repository(state['en3_ling_input_contract']).parent
    feedback = outside_repository(state['en3_ling_feedback_original'])
    require(digest(feedback.read_bytes()) == state['en3_ling_feedback_sha256'], 'prior_feedback_changed')
    prior, _ = document(old_root/'input-contract.private.json',state['en3_ling_input_contract_sha256'])
    material, _ = document(old_root/'materials.private.json', prior['materials_sha256'])
    actual, _ = document(old_root/'results.private.json', state['en3_ling_results_sha256'])
    require(actual['input_contract_sha256'] == state['en3_ling_input_contract_sha256']
            and digest((old_root/'input-contract.private.json').read_bytes()) == actual['input_contract_sha256'],
            'previous_holdout_changed')
    fresh_root = outside_repository(state['fresh_scene_study_root'])
    return read_private(fresh_root/'materials.private.json'), material


def original_regression(state):
    previous_root = outside_repository(state['en3_ling_input_contract']).parent
    contract, contract_hash = document(previous_root/'input-contract.private.json',state['en3_ling_input_contract_sha256'])
    material, material_hash = document(previous_root/'materials.private.json',contract['materials_sha256'])
    results, results_hash = document(previous_root/'results.private.json',state['en3_ling_results_sha256'])
    audit, audit_hash = document(outside_repository(state['en3_ling_independent_audit']))
    require(audit['study_id']==results['study_id']=='en3-ling-01' and audit['verified_raw_responses']==60
            and audit['verified_rendered_bodies']==36 and audit['verified_manifest']==contract_hash
            and audit['results_sha256']==results_hash, 'prior_evidence_changed')
    src = next(s for s in material['scenes'] if s['id']=='G02')
    actual = next(s for s in results['scenes'] if s['id']=='G02')
    require(len(src['turns'])==len(actual['turns'])==6 and all(
        x['user']==y['text'] and x['bodies']['B']['state']==x['bodies']['R']['state']=='completed'
        for x,y in zip(actual['turns'],src['turns'])), 'incomplete_regression')
    return (src|{'id':'X00'}, actual, {'source_materials_sha256':material_hash,
        'source_results_sha256':results_hash,'source_contract_sha256':contract_hash,'source_audit_sha256':audit_hash})


class ComparisonAssembly(ReplayAssembly):
    """Both candidates use the same persona. Each resolves its own reviewed source version."""
    runtime_bound = False

    def __init__(self, state_path, root, old_bundle, new_bundle):
        super().__init__(state_path,root,old_bundle)
        self.new_bundle_path = outside_repository(new_bundle)
        self.repair_binding = None

    def __enter__(self):
        super().__enter__()
        try:
            self.repair_manifest, self.repair_hash = document(self.new_bundle_path)
            original_fields={k:v for k,v in self.repair_manifest.items()
                             if k not in ('assembly_profile','expression_projection')}
            require(original_fields == self.bundle and self.repair_manifest.get('assembly_profile') ==
                    'direct-reviewed-speaker-roles-v2', 'unreviewed_material_changes')
            self.repair_binding = register_bundle(self.store,self.pid,self.new_bundle_path)
            self.guard()
            return self
        except BaseException:
            self.stack.close()
            raise

    def guard(self):
        from persona_runtime.dialogue_portable import adapt_bundle
        self.original.guard()
        loader = load_bundle if self.runtime_bound else load_offline_bundle
        with self.store.connection() as db:
            self.direct = loader(self.store,db,self.pid,self.binding)
            if self.repair_binding is not None:
                self.repaired = adapt_bundle(loader(self.store,db,self.pid,self.repair_binding))

    def engine(self, branch):
        require(branch in ('O','R'), 'candidate_branch_required')
        return self.direct if branch=='O' else self.repaired

    async def baseline(self, scene_id, turn_no, item, history, branch):
        require(branch in BRANCHES, 'unknown_branch')
        return await super().baseline(scene_id,turn_no,item,history,'B')

    def for_turn(self, branch, scene_id, turn_no, item, history, baseline, recent):
        previous = super().candidate_material(scene_id,turn_no,item,history,baseline)
        if branch=='O':
            return previous
        require(branch=='R', 'candidate_branch_required')
        turn = {'id':previous['turn_id'], 'persona_id':self.pid}
        material = self.repaired.material(turn,self.repair_binding,baseline[0],baseline[1]['messages'],'main',
                                          previous['history'],target(item),item['accepted_at'])
        material.update(current=previous['current'],current_lines=previous['current_lines'])
        return self.repaired.with_recent(material,recent)

    def wire(self, branch, kind, material, thought=None, selected_ids=()):
        return self.engine(branch).request(kind,{'id':material['turn_id'],'persona_id':self.pid},
                                         material,thought,list(selected_ids))

    def applied(self, branch, choice, material, thought):
        if branch=='R':
            return self.repaired.selection_audit(choice,material)
        from persona_runtime.dialogue_repair import repair_messages
        requested = self.direct.parse_selection(choice,material)
        items = [self.original.assets[i] for i in requested]
        _, applied, omitted = repair_messages(material['messages'],{'variables':self.bundle['variables']},
                                             thought,items,material['messages'][-1]['content'])
        return {'requested_ids':requested,'applied_ids':applied,'omitted':omitted}


async def openings(assembly, scene):
    item = scene['turns'][0]
    pro = await assembly.baseline(scene['id'],1,item,[],'B')
    values = [first_request('B','body',pro)]
    for branch in ('O','R'):
        material = assembly.for_turn(branch,scene['id'],1,item,[],pro,[])
        values.append(first_request(branch,'understanding',assembly.wire(branch,'understanding',material)))
    return values


def render(materials, contract, assembly):
    scenes = materials['scenes']
    turns = sum(len(s['turns']) for s in scenes)
    bubbles = sum(len(t['bubbles']) for s in scenes for t in s['turns'])
    gaps = [g for s in scenes for g in s['omitted_counterpart_turns']]
    peer = sum('text' in g['kinds'] for g in gaps)
    emoji = sum(bool(g['kinds']) and 'text' not in g['kinds'] for g in gaps)
    chunks = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>角色 · 扩测输入审阅</title><style>body{max-width:56rem;margin:2rem auto;padding:0 1rem;font:16px/1.65 system-ui}h1,h2{line-height:1.35}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}details{border-top:1px solid #adb6ad;padding:.55rem 0}summary{cursor:pointer}.bubble{white-space:pre-wrap;padding:.6em;background:#edf3ee;color:#24372d}small{color:#536759}</style>',
        '<main><h1>角色的扩测输入审阅</h1>',
        f'<p>共{len(scenes)}组，{turns}轮，包含{bubbles}条真人原始气泡。三链使用同一组桌面收束边界，组内各自接续本链的实际发言。',
        f'相邻轮次共{len(gaps)}处，{peer}处原聊天中有对方文字，{emoji}处仅有对方表情，{len(gaps)-peer-emoji}处为同一人停顿后续说。原对话中另一方的回答留在本机。</p>',
        '<p>拟发送到官方 DeepSeek。原 v8 Pro 直接发言；另两链分别按修复前和修复后的选材、表达组装生成正文。',
        f'新场景预计{contract["expanded_request_positions"]}个请求位置，原问题回归另有{contract["regression_request_positions"]}个位置，'
        f'合计最多{contract["request_positions"]}个。选择范围为空时可跳过选择步骤。每个实际位置保留首次回执。</p>',
        '<p>本批观察人物表达、连续接话和说话人归属。Jev 与检索关闭。实际等待、用量和失败情况由运行回执记录。</p>']
    reg=contract['regression']
    chunks += ['<section><h2>原问题定点回归 · '+escape(reg['source_scene_id'])+'</h2>',
               '<p>复用上一批该场景原 v8 Pro 与修复前结构33的12份首次正文。修复候选从空会话再走6轮，'
               '每轮续接自己实际说过的话。该组6轮仅用于核对原问题，与下方新增场景分开审阅和汇总。</p>']
    for turn in reg['turns']:
        chunks.append(f'<b>第{turn["turn_number"]}轮</b>')
        for bubble in turn['bubbles']:
            chunks.append('<div><small>'+escape(bubble['accepted_at'])+'</small></div><p class="bubble">'+escape(bubble['text'])+'</p>')
    chunks.extend((details('定点回归首轮 · 修复候选理解',json.dumps(reg['opening_request']['wire'],ensure_ascii=False,indent=2)),
                   '</section><h2>新的留出场景</h2>'))
    for scene in scenes:
        chunks.append('<section><h2>'+escape(scene['id']+' · '+scene['title'])+'</h2>')
        for point, turn in enumerate(scene['turns']):
            chunks.append(f'<b>第{point+1}轮</b>')
            for bubble in turn['bubbles']:
                chunks.append('<div><small>'+escape(bubble['accepted_at'])+'</small></div><p class="bubble">'+escape(bubble['text'])+'</p>')
            if point<len(scene['omitted_counterpart_turns']):
                gap=scene['omitted_counterpart_turns'][point]
                chunks.append('<p><small>'+('原对话有文字接话' if 'text' in gap['kinds'] else '原对话有表情回应' if gap['kinds'] else '同一人停顿后续说')+
                              f'，距下一轮{gap["seconds"]}秒</small></p>')
        for req in contract['opening_requests'][scene['id']]:
            stage = '人物正文' if req['stage']=='body' else '理解'
            chunks.append(details('首轮 · '+BRANCHES[req['branch']]+' · '+stage,
                                  json.dumps(req['wire'],ensure_ascii=False,indent=2)))
        chunks.append('</section>')
    chunks += ['<h2>理解、选材与正文</h2><p>原 v8 Pro 使用已有角色材料和实际前文直接生成。修复前结构33将首次理解交给选材及正文生成。',
               '修复候选按真实说话人组织理解输入；理解仅供选材。人物正文依据 v8 的角色材料、当轮输入、本链实际前文和通过筛选的表达。最近两轮已投影的条目从本轮选择池里去除。</p>',
               '<p>后续请求里的实际回复与理解将由各链首次回执填入，本页列出可预先冻结的全文及条目来源。</p>',
               details('连续会话中 Flash 实际怎样区分两人的发言 · 旧 G02 第二轮离线装配示例',
                       json.dumps(contract['role_layout_example']['wire'],ensure_ascii=False,indent=2)),
               '<p>上述示例使用上一轮已归档的人物首句说明说话人位置，仅供本机审阅。实际扩测沿用本轮同一条链的首次回执。</p>',
               details('既有理解与选材格式',json.dumps({'flash_user':assembly.bundle['flash_user'],
                       'variables':assembly.bundle['variables']},ensure_ascii=False,indent=2)),
               details('旧选择器全文',assembly.bundle['selector_system']['content']),
               details('修复候选的首轮选择器全文',contract['repair_selector_system']),
               details('修复候选的首轮资产范围',json.dumps(contract['repair_pool_review'],ensure_ascii=False,indent=2)),
               '<p>同目录的 materials.private.json 保存原始消息标识、指纹和分区，input-contract.private.json 保存完整身份、模型及程序版本绑定。</p></main></html>']
    return ''.join(chunks)


def source_materials(assembly, state, materials):
    from .en3_expanded_material import verify_materials
    earlier, holdout = original_inputs(state)
    from .en3_holdout_material import verify_materials as verify_prior
    corpus=DatasetCorpusStore(assembly.original.plan['corpus_root'])
    verify_prior(corpus,holdout,assembly.original.plan,earlier)
    return verify_materials(corpus,materials,assembly.original.plan,earlier,holdout)


async def prepare(state_path, root, bundle_path, repair_bundle_path=None):
    from .en3_repair_blind import freeze
    root = outside_repository(root)
    repair_bundle_path = repair_bundle_path or root/'portable-source-bundle.private.json'
    materials, sha = document(root/'materials.private.json')
    state = read_private(state_path)
    with ComparisonAssembly(state_path,root,bundle_path,repair_bundle_path) as assembly:
        source_materials(assembly,state,materials)
        scenes = materials['scenes']
        count = sum(len(s['turns']) for s in scenes)
        require(materials['split']=='final_holdout' and len(scenes)>2 and count>12,'expanded_holdout_required')
        opening = {s['id']:await openings(assembly,s) for s in scenes}
        first=scenes[0]['turns'][0]
        baseline=await assembly.baseline(scenes[0]['id'],1,first,[],'R')
        sample=assembly.for_turn('R',scenes[0]['id'],1,first,[],baseline,[])
        pool=assembly.repaired.candidate_pool(sample)
        regression, old_result, reg_bindings = original_regression(state)
        regression_opens = await openings(assembly,regression)
        old_first, old_second = regression['turns'][:2]
        example_history = [(old_first,old_result['turns'][0]['bodies']['R']['body'],old_first['accepted_at'])]
        example_baseline=await assembly.baseline('X00',2,old_second,example_history,'R')
        example_material=assembly.for_turn('R','X00',2,old_second,example_history,example_baseline,[])
        example = first_request('R','understanding',assembly.wire('R','understanding',example_material))
        reg_plan = {**reg_bindings, 'id':'X00','source_scene_id':'G02','title':regression['title'],
                    'turns':regression['turns'],'opening_request':regression_opens[-1],
                    'reused_branches':{'B':'B','O':'R'},'new_branch':'R'}
        contract={'version':VERSION,'study_id':STUDY,'root':str(root),'state_path':str(outside_repository(state_path)),
                  'persona':assembly.original.persona,'split':'final_holdout','branches':BRANCHES,'materials_sha256':sha,
                  'source_bundle_path':str(assembly.bundle_path),'source_bundle_sha256':assembly.bundle_hash,
                  'repair_bundle_path':str(assembly.new_bundle_path),'repair_bundle_sha256':assembly.repair_hash,
                  'assembly_sources':sources(),'blind_key_sha256':freeze(root,materials,reg_plan['source_results_sha256']),
                  'opening_requests':opening,'regression':reg_plan,
                  'role_layout_example':{'source_scene_id':'G02','source_history_branch':'R','preview_only':True,
                                         'wire':example['wire'],'request_hash':example['request_hash']},
                  'repair_selector_system':assembly.repaired.selector_message(sample)['content'],
                  'repair_pool_review':{'eligible':[{'id':i,**{k:assembly.original.assets[i][k] for k in ('situation','style')}} for i in pool['eligible_ids']],
                                        'omitted':pool['omitted']},
                  'prior_feedback_sha256':state['en3_ling_feedback_sha256'],
                  'scene_count':len(scenes),'turns_per_branch':count,'expanded_request_positions':count*7,
                  'regression_request_positions':len(regression['turns'])*3,
                  'request_positions':count*7+len(regression['turns'])*3,
                  'online_retrieval':False,'jev_enabled':False,'controlled_materials':[],'formal_activation':False,
                  'persona_spec_sha256':assembly.original.snapshot['spec_utf8_sha256'],'character_version':assembly.original.snapshot['character_version']}
        page=render(materials,contract,assembly)
        contract['inputs_html_sha256']=digest(page.encode('utf8'))
        save_once(root/'input-contract.private.json',contract)
        save_text_once(root/'inputs.html',page)
        return contract


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    for param in ('state','root','source-bundle'):
        cli.add_argument('--'+param,type=Path,required=True)
    cli.add_argument('--repair-bundle',type=Path)
    a=cli.parse_args()
    v=asyncio.run(prepare(a.state,a.root,a.source_bundle,a.repair_bundle))
    print(json.dumps({k:v[k] for k in ('study_id','scene_count','turns_per_branch','request_positions')},ensure_ascii=False))
