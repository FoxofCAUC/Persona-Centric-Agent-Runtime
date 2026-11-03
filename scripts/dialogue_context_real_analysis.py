"""CR2.1 evidence reporting. No generation, network, paid judge or invented scores.

Authored readings are input data written only after inspecting actual outputs.
Automatic literal checks are deliberately separate from semantic/naturalness review.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import statistics

from persona_runtime.archive import encoded, fingerprint
from persona_runtime.config import atomic_json

ROOT = Path(__file__).resolve().parents[1]
OUT = Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR2.1-real-diagnosis')
VERSION = 'cr2-real-analysis-v1'
PROTOCOL = ROOT/'tests/fixtures/dialogue-context-real-protocol-v1.json'
DIMENSIONS = ('not_customer_service', 'not_mechanical', 'im_naturalness')
SAMPLE_CATEGORIES = ('natural_borrow', 'fitting_reuse', 'forced', 'fact_transfer', 'not_observable')


def require(ok, code):
    if not ok:
        raise ValueError(code)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def raw_of(result):
    value = result.get('reply', result.get('raw'))
    require(isinstance(value, str), 'cr21_reply_missing')
    return value


def bubbles_of(result):
    raw = raw_of(result)
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return [raw]
    if not isinstance(value, dict):
        return [raw]
    for field, content in (('messages', 'content'), ('bubbles', 'text'), ('segments', 'text')):
        if isinstance(value.get(field), list):
            return [str(row[content]) for row in value[field] if isinstance(row, dict) and content in row]
    return [raw]


def text_of(result):
    return '\n'.join(bubbles_of(result))


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    index = (len(values)-1)*p
    lo = int(index)
    hi = min(lo+1, len(values)-1)
    return values[lo] + (values[hi]-values[lo])*(index-lo)


def distribution(values):
    values = [v for v in values if type(v) in (int, float) and v >= 0]
    return {'count':len(values), 'P50':percentile(values,.5), 'P95':percentile(values,.95)}


def scene_interval(pairs, seed=71321):
    grouped = defaultdict(list)
    for pair in pairs:
        if pair.get('delta') is not None:
            grouped[pair['case_id']].append(pair['delta'])
    means = [statistics.mean(grouped[key]) for key in sorted(grouped)]
    if not means:
        return None
    rng = random.Random(seed)
    boot = [statistics.mean(rng.choices(means,k=len(means))) for _ in range(10000)]
    return {'scene_count':len(means),'mean':statistics.mean(means),
            'descriptive_95_percent_interval':[percentile(boot,.025),percentile(boot,.975)],
            'not_fixed_backend_causal_interval':True}


def is_real(value):
    if type(value.get('real')) is bool:
        return value['real']
    if type(value.get('simulation')) is bool:
        return not value['simulation']
    return None


def load_results(root, prepared):
    results = {}
    for position in prepared['positions']:
        ident = position['id']
        path = root/'results'/(ident+'.json')
        if not path.exists():
            continue
        value = read(path)
        require(value.get('id',ident)==ident, 'cr21_result_identity')
        for field in ('case_id','role_id','arm','replicate'):
            if field in value:
                require(value[field]==position[field], 'cr21_result_identity')
        require(type(value.get('real')) is bool, 'cr21_result_scope_missing')
        results[ident] = dict(position, **value)
    modes = {v['real'] for v in results.values()}
    require(len(modes)<=1, 'cr21_mixed_real_and_simulation_results')
    declared = is_real(prepared)
    if declared is not None:
        require(not modes or modes=={declared}, 'cr21_result_scope_mismatch')
    return results, declared if declared is not None else next(iter(modes),None)


def load_receipts(root, mode):
    rows = []
    for path in sorted((root/'receipts').glob('*.reserve.json')):
        row = read(path)
        reservation_hash = fingerprint(row)
        if mode is not None:
            require(is_real(row)==mode, 'cr21_receipt_scope_mismatch')
        row = {'receipt_id':path.name.removesuffix('.reserve.json'), **row}
        for suffix in ('transport','terminal'):
            other = path.with_name(path.name.replace('.reserve.',f'.{suffix}.'))
            if other.exists():
                row[suffix] = read(other)
                if mode is not None:
                    require(is_real(row[suffix])==mode,'cr21_receipt_scope_mismatch')
        if 'terminal' in row:
            require(row['terminal'].get('reservation_hash')==reservation_hash,'cr21_receipt_reservation_changed')
        rows.append(row)
    return rows


def literal_controls(result):
    """Only two numeric/format controls admit literal checking; all others require reading."""
    text = text_of(result)
    case = result['case_id']
    if case == 'S02':
        return {'status':'passed' if text.strip()=='432' else 'failed',
                'expected':'432 only','actual':text,'method':'exact_integer_output'}
    if case == 'S11':
        compact = ''.join(text.split())
        accepted = {'126平方厘米','126cm²','126cm^2','126cm2'}
        return {'status':'passed' if compact in accepted else 'needs_reading',
                'expected':'126 square centimetres; equivalent notation may require reading',
                'actual':text,'method':'exact_result_and_unit_only'}
    return {'status':'needs_reading','actual':text,
            'method':'semantic judgment required; no keyword-derived pass/fail'}


def validate_assessments(rows, results, protocol=None):
    indexed = {}
    for row in rows:
        ident = row['position_id']
        require(ident not in indexed and ident in results, 'cr21_assessment_identity')
        result = results[ident]
        require(result.get('status')=='completed', 'cr21_assessment_incomplete_result')
        text = text_of(result)
        require(row['output_hash']==fingerprint(raw_of(result)), 'cr21_assessment_output_changed')
        require(set(row['dimensions'])==set(DIMENSIONS), 'cr21_assessment_dimensions')
        for value in row['dimensions'].values():
            require(type(value.get('score')) is int and 1<=value['score']<=5, 'cr21_score_range')
            require(isinstance(value.get('reason'),str) and bool(value['reason'].strip()), 'cr21_score_reason')
            require(isinstance(value.get('excerpt'),str) and bool(value['excerpt']) and value['excerpt'] in text,
                    'cr21_score_excerpt')
        require(isinstance(row.get('obligations'),list) and isinstance(row.get('boundaries'),list), 'cr21_semantic_reading_missing')
        if protocol is not None:
            criteria=protocol['cases'][result['case_id']]
            require(len(row['obligations'])==len(criteria['required_content']), 'cr21_obligation_coverage')
            require(len(row['boundaries'])==len(criteria['forbidden']), 'cr21_boundary_coverage')
        for judgment in row['obligations']+row['boundaries']:
            require(judgment.get('status') in ('satisfied','partial','not_satisfied','unable','respected','violated'), 'cr21_semantic_status')
            require(isinstance(judgment.get('reason'),str) and bool(judgment['reason'].strip()), 'cr21_semantic_reason')
            require(isinstance(judgment.get('excerpt'),str) and bool(judgment['excerpt']) and judgment['excerpt'] in text,
                    'cr21_semantic_excerpt')
        if 'task_status' in row:
            require(row['task_status'] in ('satisfied','not_satisfied','unable'), 'cr21_task_status')
        if 'sample_use' in row:
            require(row['sample_use'].get('category') in SAMPLE_CATEGORIES, 'cr21_sample_category')
            require(bool(row['sample_use'].get('reason')), 'cr21_sample_reason')
        indexed[ident] = row
    return indexed


def validate_frames(rows, results, protocol=None):
    indexed = {}
    for row in rows:
        ident = row['position_id']
        require(ident not in indexed and ident in results, 'cr21_frame_identity')
        result = results[ident]
        pipeline = result.get('pipeline') or {}
        understanding = pipeline.get('understanding')
        require(result['arm']=='automatic' and understanding is not None, 'cr21_frame_not_executed')
        require(row['result_hash']==fingerprint(understanding), 'cr21_frame_result_changed')
        items = row.get('critical_items')
        require(isinstance(items,list) and bool(items), 'cr21_frame_critical_items_missing')
        if protocol is not None:
            require(len(items)==len(protocol['cases'][result['case_id']]['frame_key_items']), 'cr21_frame_critical_coverage')
        frame_text = encoded(understanding)
        source_text = '\n'.join(s['text'] for s in pipeline.get('shared',{}).get('sources',[]))
        for item in items:
            require(item.get('status') in ('correct','incorrect','missing','unable'), 'cr21_frame_status')
            require(isinstance(item.get('reason'),str) and bool(item['reason'].strip()), 'cr21_frame_reason')
            source = item.get('input_excerpt','')
            excerpt = item.get('frame_excerpt','')
            require(bool(source or excerpt), 'cr21_frame_evidence_missing')
            require(not source or source in source_text, 'cr21_frame_input_excerpt')
            require(not excerpt or excerpt in frame_text, 'cr21_frame_excerpt')
        indexed[ident] = row
    return indexed


def task_error(assessment):
    if not assessment:
        return None
    statuses = [j['status'] for j in assessment['obligations']+assessment['boundaries']]
    if assessment.get('task_status')=='not_satisfied' or any(s in ('partial','not_satisfied','violated') for s in statuses):
        return True
    if assessment.get('task_status')=='unable' or 'unable' in statuses:
        return None
    return False


def source_blocks(result):
    diagnostic = (result.get('manifest') or {}).get('prompt_assembly') or {}
    blocks = diagnostic.get('blocks')
    if not blocks:
        return None
    grouped = defaultdict(list)
    for block in blocks:
        fields=('message_index','start_character','characters')
        if not isinstance(block.get('content_hash'),str) or not all(type(block.get(k)) is int for k in fields):
            return None
        messages=result.get('messages',[])
        try:
            text=messages[block['message_index']]['content'][block['start_character']:block['start_character']+block['characters']]
        except (IndexError,TypeError,KeyError):
            return None
        require(fingerprint(text)==block['content_hash'], 'cr21_block_hash_mismatch')
        grouped[block['name']].append(block['content_hash'])
    return dict(grouped)


def selected_materials(result):
    pipeline = result.get('pipeline') or {}
    character = pipeline.get('character') or {}
    selection = ((result.get('manifest') or {}).get('prompt_assembly') or {}).get('material_selection') or {}
    return {name:character.get(name, selection.get('included_example_ids' if name=='example_ids' else name))
            for name in ('fact_paths','lore_ids','example_ids')}


def main_receipt(result, receipts):
    chosen = [r for r in receipts if r.get('kind')=='main' and
              (r.get('slot')==result['id']+'/main' or r.get('slot')==result['id'] or
               r.get('receipt_id') in {str(x).zfill(4) for x in result.get('calls',[])})]
    done = [r for r in chosen if r.get('terminal',{}).get('status')=='completed']
    return (done or chosen)[-1] if chosen else None


def backend(receipt):
    details = (receipt or {}).get('transport',{}).get('diagnostics') or {}
    return {key:details.get(key) for key in ('response_model','system_fingerprint')}


def attribution(candidate, control, receipts):
    left = candidate.get('input_hash') or fingerprint(candidate.get('messages'))
    right = control.get('input_hash') or fingerprint(control.get('messages'))
    a,b = source_blocks(candidate),source_blocks(control)
    changed = None if a is None or b is None else sorted(k for k in a.keys()|b.keys() if a.get(k)!=b.get(k))
    ca,cb = main_receipt(candidate,receipts),main_receipt(control,receipts)
    ma,mb = backend(ca),backend(cb)
    known = all(ma.values()) and all(mb.values())
    same_backend = bool(known and ma==mb)
    parameters_known = bool(ca and cb)
    if parameters_known:
        from persona_runtime.provider_compat import build_request
        wa = build_request(ca['model'],ca['messages'])[0]
        wb = build_request(cb['model'],cb['messages'])[0]
        same_params = {k:v for k,v in wa.items() if k!='messages'} == {k:v for k,v in wb.items() if k!='messages'}
    else:
        same_params = None
    material_a,material_b = selected_materials(candidate),selected_materials(control)
    material_known = all(v is not None for v in material_a.values()) and all(v is not None for v in material_b.values())
    same_material = bool(material_known and material_a==material_b)
    protected = {'persona_expression','relationship','temporary_state','behavior','presentation',
                 'reply_protocol','capsule','current_input','explicit_references'}
    unexplained = sorted(set(changed or []) & protected)
    same_input = left==right
    if same_input:
        category='same_main_request'
    elif changed is None:
        category='input_difference_not_fully_observable'
    elif unexplained or same_params is False:
        category='unexplained_or_parameter_difference'
    elif set(changed)=={'dialogue_context'} and same_material:
        category='frame_projection_only'
    elif set(changed)<={'dialogue_context','character'}:
        category='selection_and_or_frame_changed'
    else:
        category='shared_budget_or_material_changes'
    reasons=[]
    if same_input:reasons.append('Same request output differences are not component benefit evidence.')
    if not known:reasons.append('Response model or backend fingerprint missing.')
    elif not same_backend:reasons.append('Main response backend metadata differs.')
    if changed is None:reasons.append('Block-level diagnostics unavailable.')
    if not material_known:reasons.append('Selected material IDs not fully recorded.')
    if same_params is not True:reasons.append('Provider parameters differ or cannot be compared.')
    reasons.append('The cohort compares complete chains; matching metadata is not deterministic or fixed-weight proof.')
    return {'category':category,'same_main_request':same_input,'changed_blocks':changed,
            'main_backend_known':bool(known),'main_backend_same':same_backend,'candidate_backend':ma,'control_backend':mb,
            'provider_parameters_equal':same_params,'materials_known':material_known,'same_selected_materials':same_material,
            'candidate_materials':material_a,'control_materials':material_b,'unexplained_blocks':unexplained,
            'frame_projection_condition_subset':bool(category=='frame_projection_only' and same_backend and same_params),
            'causal_proof':False,'limitations':reasons}


def compare(results, assessments, receipts, prepared, authored_pairs=None):
    authored={p['id']:p for p in (authored_pairs or [])}
    require(len(authored)==len(authored_pairs or []), 'cr21_pair_reading_duplicate')
    grouped=defaultdict(dict)
    for position in prepared['positions']:
        grouped[(position['case_id'],position['role_id'],position['replicate'])][position['arm']]=position['id']
    pairs=[]
    for (case,role,rep),arms in sorted(grouped.items()):
        cid,bid=arms.get('automatic'),arms.get('legacy')
        c,b=results.get(cid),results.get(bid)
        ident=f'{case}-{role}-r{rep}'
        row={'id':ident,'case_id':case,'role_id':role,'replicate':rep,'candidate':cid,'control':bid,
             'verdict':'unable','delta':None,'reason':'incomplete pair','complete_pair':False}
        if c and b and c.get('status')==b.get('status')=='completed':
            row['complete_pair']=True;row['attribution']=attribution(c,b,receipts)
            ca,ba=assessments.get(cid),assessments.get(bid)
            if ca and ba:
                ce,be=task_error(ca),task_error(ba)
                delta=ca['dimensions']['im_naturalness']['score']-ba['dimensions']['im_naturalness']['score']
                row.update(delta=delta,candidate_task_or_boundary_error=ce,control_task_or_boundary_error=be,
                           candidate_excerpt=ca['dimensions']['im_naturalness']['excerpt'],
                           control_excerpt=ba['dimensions']['im_naturalness']['excerpt'])
                verdict=('unable' if ce is None or be is None else 'loss' if ce and not be else
                         'win' if be and not ce else 'win' if delta>0 else 'loss' if delta<0 else 'tie')
                row.update(verdict=verdict,reason=ca['dimensions']['im_naturalness']['reason']+'；对照：'+ba['dimensions']['im_naturalness']['reason'],
                           method='derived from authored machine readings; semantic error precedence; not independent judging')
            else:
                row['reason']='full output reading pending; no score or verdict invented'
            if ident in authored:
                reading=authored[ident]
                require(reading.get('verdict') in ('win','tie','loss','unable'), 'cr21_pair_verdict')
                require(bool(reading.get('reason')), 'cr21_pair_reason')
                for key,value in (('candidate_excerpt',c),('control_excerpt',b)):
                    require(bool(reading.get(key)) and reading[key] in text_of(value), 'cr21_pair_excerpt')
                row.update({k:reading[k] for k in ('verdict','reason','candidate_excerpt','control_excerpt')})
                row['method']='authored machine pair reading; not independent human judging'
        pairs.append(row)
    require(set(authored)<=set(p['id'] for p in pairs), 'cr21_pair_reading_unknown')
    return pairs


def review_plan(protocol=None):
    protocol=protocol or read(PROTOCOL)
    spec=protocol['anonymous_review'];selected=[]
    require(spec['orientation_seed']=='cr21-review-v1','cr21_review_seed')
    for entry in spec['entries']:
        case,role,rep=entry['scene'],entry['role'],entry['replicate']
        candidate=f'{case}-{role}-automatic-r{rep}';control=f'{case}-{role}-legacy-r{rep}'
        require(entry['candidate_side'] in ('A','B'),'cr21_review_orientation')
        selected.append({'case_id':case,'role_id':role,'replicate':rep,'route':entry['route'],
                         'number':entry['number'],'candidate':candidate,'control':control,
                         'A':candidate if entry['candidate_side']=='A' else control,
                         'B':control if entry['candidate_side']=='A' else candidate})
    require(len(selected)==12 and {r['number'] for r in selected}==set(range(1,13)),'cr21_review_coverage')
    require(len({r['case_id'] for r in selected})==12,'cr21_review_scene_coverage')
    require(Counter(r['role_id'] for r in selected)=={'process':6,'candid':6},'cr21_review_role_coverage')
    require(Counter(r['route'] for r in selected)=={'direct':6,'joint':6},'cr21_review_route_coverage')
    require(sum(r['A']==r['candidate'] for r in selected)==6,'cr21_review_orientation_coverage')
    return selected


def fenced(text):
    import re
    marker='`'*max(3,1+max((len(s) for s in re.findall(r'`+',text)),default=0))
    return marker+'text\n'+text+'\n'+marker


def anonymous_markdown(results, materials, *, real, mapping=None):
    cases={r['id']:r for r in materials.get('cases',[])}
    roles=materials.get('roles',{})
    lines=['# CR2.1 12组匿名审核','',
           '模型原回复对照，非QQ送达记录。' if real else '模拟报告检查材料：不能作为真实模型或自然度验收。',
           '逐组选择 A、B、平或无法判断；理由可选。条件映射在收到答复后揭示。','']
    available=0
    for row in (mapping or review_plan()):
        lines.extend([f'## {row["number"]:02d}',''])
        case=cases.get(row['case_id'])
        if case:
            style=roles.get(row['role_id'],{}).get('style','')
            if style:lines.extend(['人物说明：'+style,''])
            if case.get('history'):
                lines.extend(['前文：',''])
                for pair in case['history']:
                    user,answer=(pair.get('user',''),pair.get('assistant','')) if isinstance(pair,dict) else pair
                    lines.extend(['用户：'+user,'','角色：'+answer,''])
            if case.get('reference'):lines.extend(['本轮合法引用：上方第一条用户旧消息。',''])
            lines.extend(['本轮输入：','',fenced('\n'.join(case['members'])),''])
        a,b=results.get(row['A']),results.get(row['B'])
        if not a or not b or a.get('status')!=b.get('status') or a.get('status')!='completed':
            lines.extend(['该预定组缺少完整配对，保持未完成；不换题、不补造回复。',''])
            continue
        available+=1
        for label,value in (('A',a),('B',b)):
            lines.extend([f'**{label}**',''])
            for bubble in bubbles_of(value):lines.extend([fenced(bubble),''])
    lines.extend(['可回复：“01 A；02 平；……；12 B”。','',
                  '这是固定开发场景的用户偏好复核，不是独立多人盲审，也不自动代表所有日常聊天。',''])
    return '\n'.join(lines),available


def resolve_user_review(rows, mapping, results):
    indexed={};resolved=[]
    for row in rows:
        number=row['number'];choice=row.get('choice',row.get('answer'))
        require(type(number) is int and 1<=number<=12 and number not in indexed, 'cr21_user_review_number')
        require(choice in ('A','B','平','无法判断','tie','unable'), 'cr21_user_review_choice')
        indexed[number]=dict(row,choice=choice)
    for pair in mapping:
        row=indexed.get(pair['number'])
        if not row:continue
        choice=row['choice'];chosen=pair[choice] if choice in ('A','B') else None
        if choice not in ('无法判断','unable'):
            require(all(p in results and results[p].get('status')=='completed' for p in (pair['A'],pair['B'])),
                    'cr21_user_review_missing_result')
        verdict='win' if chosen==pair['candidate'] else 'loss' if chosen==pair['control'] else 'tie' if choice in ('平','tie') else 'unable'
        resolved.append({**pair,'user_choice':choice,'user_reason':row.get('reason'),
                         'chosen_position':chosen,'user_candidate_verdict':verdict})
    return resolved


def assessment_template(results):
    return [{'position_id':ident,'output_hash':fingerprint(raw_of(value)),
             'dimensions':{name:{'score':None,'reason':None,'excerpt':None} for name in DIMENSIONS},
             'obligations':[],'boundaries':[],'task_status':'unable','not_an_assessment_until_authored':True}
            for ident,value in sorted(results.items()) if value.get('status')=='completed']


def aggregate_receipts(rows):
    purposes=Counter();statuses=Counter();unknown_usage=[];known_tokens=0;backends=Counter()
    retry_slots=defaultdict(list)
    for row in rows:
        purposes[row.get('kind','unknown')]+=1
        status=row.get('terminal',{}).get('status','unsettled');statuses[status]+=1
        retry_slots[row.get('slot','unknown')].append(row)
        usage=row.get('transport',{}).get('usage')
        if isinstance(usage,dict) and type(usage.get('total_tokens')) is int:
            known_tokens+=usage['total_tokens']
        else:unknown_usage.append(row['receipt_id'])
        metadata=backend(row)
        backends[(row.get('kind'),metadata['response_model'],metadata['system_fingerprint'])]+=1
    retry_groups=[]
    for slot,group in sorted(retry_slots.items()):
        completed=next((i for i,r in enumerate(group) if r.get('terminal',{}).get('status')=='completed'),None)
        retry_groups.append({'slot':slot,'attempts':len(group),'first_attempt_success':completed==0,
                             'retry_recovered':completed is not None and completed>0,'completed':completed is not None})
    return {'reserved_attempts':len(rows),'transport_records':sum('transport' in r for r in rows),
            'purpose_counts':dict(purposes),'terminal_counts':dict(statuses),
            'known_total_tokens':known_tokens,'unknown_usage_receipts':unknown_usage,'amount':None,
            'usage_method':'supplier total_tokens only; reasoning/cache details not added again',
            'backends':[{'kind':k,'response_model':m,'system_fingerprint':f,'count':n} for (k,m,f),n in sorted(backends.items(),key=lambda t:str(t[0]))],
            'retry_slots':retry_groups,'first_attempt_success_slots':sum(g['first_attempt_success'] for g in retry_groups),
            'retry_recovered_slots':sum(g['retry_recovered'] for g in retry_groups)}


def report(root, *, write=True):
    root=Path(root).resolve()
    if write:
        require(not (root/'closure.json').exists() and not (root.parent/'closure.json').exists(), 'cr21_report_stage_sealed')
        require(root.is_relative_to(OUT.resolve()) or root.is_relative_to(Path('D:/Persona-Centric Agent Runtime/tmp').resolve()), 'cr21_report_outside_stage')
    prepared=read(root/'prepared.json');results,mode=load_results(root,prepared)
    receipts=load_receipts(root,mode)
    folder=root/'analysis'
    protocol=read(root/'protocol.json') if (root/'protocol.json').exists() else prepared.get('protocol') or read(PROTOCOL)
    assessments=validate_assessments(read(folder/'authored-assessments.json') if (folder/'authored-assessments.json').exists() else [],results,protocol)
    frames=validate_frames(read(folder/'authored-frame-assessments.json') if (folder/'authored-frame-assessments.json').exists() else [],results,protocol)
    authored_pairs=read(folder/'authored-pairs.json') if (folder/'authored-pairs.json').exists() else []
    pairs=compare(results,assessments,receipts,prepared,authored_pairs)
    complete={k:v for k,v in results.items() if v.get('status')=='completed'}
    controls={k:literal_controls(v) for k,v in complete.items()}
    materials=read(root/'materials.json') if (root/'materials.json').exists() else {}
    mapping=review_plan(protocol);markdown,available=anonymous_markdown(results,materials,real=mode is True,mapping=mapping)
    reviews=resolve_user_review(read(folder/'user-review.json') if (folder/'user-review.json').exists() else [],mapping,results)
    state=read(root/'state.json') if (root/'state.json').exists() else {'status':'unknown'}
    receipt_summary=aggregate_receipts(receipts)
    arm_summary={}
    for arm in ('legacy','automatic'):
        values=[v for v in complete.values() if v['arm']==arm]
        judged=[assessments[v['id']] for v in values if v['id'] in assessments]
        arm_ids={p['id'] for p in prepared['positions'] if p['arm']==arm}
        arm_receipts=[r for r in receipts if r.get('slot','').rsplit('/',1)[0] in arm_ids]
        arm_summary[arm]={'planned':sum(p['arm']==arm for p in prepared['positions']),'completed':len(values),
            'machine_read':len(judged),'task_or_boundary_errors':sum(task_error(v) is True for v in judged),
            'task_judgment_unknown':sum(task_error(v) is None for v in judged),
            'dimension_means':{name:statistics.mean(r['dimensions'][name]['score'] for r in judged) if judged else None for name in DIMENSIONS},
            'experimental_elapsed_seconds':distribution([v.get('elapsed_seconds') for v in values]),
            'receipts':aggregate_receipts(arm_receipts),
            'role_dimensions':{role:{'read':sum(results[r['position_id']]['role_id']==role for r in judged),
                'means':{name:statistics.mean(r['dimensions'][name]['score'] for r in judged if results[r['position_id']]['role_id']==role)
                         if any(results[r['position_id']]['role_id']==role for r in judged) else None for name in DIMENSIONS}}
                for role in ('process','candid')}}
    route_latency={}
    for name in ('direct','joint'):
        values=[v for v in complete.values() if v['arm']=='automatic' and (v.get('pipeline') or {}).get('routing',{}).get('path')==name]
        route_latency[name]=distribution([v.get('elapsed_seconds') for v in values])
    retry_latency={}
    for arm in ('legacy','automatic'):
        groups={'first_attempt_success':[],'retry_recovery':[]}
        for value in complete.values():
            if value['arm']!=arm:continue
            physical=[r for r in receipts if r.get('slot','').startswith(value['id']+'/')]
            counts=Counter(r.get('kind') for r in physical)
            retried=any(n>1 for n in counts.values())
            groups['retry_recovery' if retried else 'first_attempt_success'].append(value.get('elapsed_seconds'))
        retry_latency[arm]={k:distribution(v) for k,v in groups.items()}
    eligible=[p for p in pairs if p.get('attribution',{}).get('frame_projection_condition_subset')]
    summary={'version':VERSION,'real':mode,'scope':'real' if mode else 'simulation' if mode is False else 'unknown',
        'planned_main_positions':len(prepared['positions']),'main_completed':len(complete),'batch_state':state,
        'normal_expected_generations':224,'receipts':receipt_summary,'arms':arm_summary,
        'route_experimental_elapsed_seconds':route_latency,'latency_is_QQ_delivery':False,
        'retry_experimental_elapsed_seconds':retry_latency,
        'complete_pairs':sum(p['complete_pair'] for p in pairs),'planned_pairs':len(pairs),
        'machine_pair_verdicts':dict(Counter(p['verdict'] for p in pairs)),
        'scene_cluster_naturalness':scene_interval(pairs),'same_main_request_pairs':sum(p.get('attribution',{}).get('same_main_request',False) for p in pairs),
        'frame_projection_condition_pairs':len(eligible),'frame_projection_condition_verdicts':dict(Counter(p['verdict'] for p in eligible)),
        'attribution_categories':dict(Counter(p.get('attribution',{}).get('category','incomplete') for p in pairs)),
        'frame_outputs':sum(bool((v.get('pipeline') or {}).get('understanding')) for v in results.values()),
        'frame_machine_read':len(frames),'frame_critical_items':dict(Counter(x['status'] for r in frames.values() for x in r['critical_items'])),
        'machine_reading_pending':len(complete)-len(assessments),'user_review_required':12,
        'user_review_pairs_available':available,'user_review_received':len(reviews),
        'user_review_verdicts':dict(Counter(r['user_candidate_verdict'] for r in reviews)),
        'user_review_complete':len(reviews)==available==12,
        'real_effect_not_inferred_from_simulation':True,'not_release_approval':True,
        'naturalness_method':'authored output/excerpt readings only; no length, punctuation or keyword scoring',
        'user_review_method':'predeclared balanced developer cases; not independent multi-human blind review'}
    if write:
        folder.mkdir(parents=True,exist_ok=True)
        mapping_path=folder/'anonymous-mapping.json'
        require(not mapping_path.exists() or read(mapping_path)==mapping, 'cr21_anonymous_mapping_changed')
        atomic_json(mapping_path,mapping)
        atomic_json(folder/'literal-controls.json',controls);atomic_json(folder/'pairs.json',pairs)
        atomic_json(folder/'summary.json',summary)
        if reviews:atomic_json(folder/'user-review-resolved.json',reviews)
        (folder/'CR2.1-12组匿名审核.md').write_text(markdown,encoding='utf-8')
    return summary
