"""Combine unchanged Grok successes with authorized retry results; no calls."""
from collections import Counter
import json
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.grok47_r1_retry import SUPPLEMENT,PARENT,EVIDENCE,RetryReceipts
from scripts.grok47_r1_gate import GATE
from scripts.expression_selection_r1 import read,sha
from scripts.retry_expression_selection_r1 import ReceiptLedger,write_once
from scripts.report_grok47_r1 import partial_analysis,usage
from persona_runtime.config import atomic_json


def machine_review(out, metrics):
    """Persist this developer's reading; no judge call or independent blind review."""
    original=read(EVIDENCE/'machine-analysis.json')
    entries=original['entries'][:14]
    entries[4]['reason']=entries[4]['reason'].replace('唯一有样例入模变化的完整对照。','本组有样例入模变化。')
    entries.extend([
        {'id':'P15','experiment_label':'A','verdict':'胜','complete':True,
         'reason':'两臂均正确接续杯子未坏、歪和三次重做。实验臂将注意力放在看不见的付出，没有沿用对照的“你嫌它”和“失败也粘在杯子上”；更贴合细节型人物。本次偏好为实验臂略优，但“只有你知道”仍有修辞性推断，且“最费劲的那截常常没人看见”明显沿用样例。',
         'main_request_changed':True,'positive_effect_attributable_to_selector':False,
         'confound':'主回复后端指纹从fp_9fe65cd761变为fp_c60958d81f66f847，跨补测时间；无法把改善单独归因选择器。'},
        {'id':'P16','experiment_label':'A','verdict':'平','complete':True,
         'reason':'两臂均保留三次重做及合法引用。对照更明确表态喜欢手作痕迹，实验臂以“折腾都写在形状上”接话；后者更收束，但“更占地方”含义较含混，人物立场未显著增强，不能仅凭简短判优。均有对烦恼的推断，未移植样例事件为共同经历。',
         'main_request_changed':True,'positive_effect_attributable_to_selector':False}
    ])
    counts=Counter(e['verdict'] for e in entries)
    result={'method':original['method'],'original_analysis_sha256':sha(EVIDENCE/'machine-analysis.json'),
        'complete_pairs':16,'wins':counts['胜'],'ties':counts['平'],'losses':counts['负'],'unknown':counts['无法判断'],
        'entries':entries,
        'causality':'4个样本级胜中3个主请求完全相同，另1个P15伴随后端指纹变化。主请求改变的P05、P16为平。不存在本批次可单独归因选择器的已证实自然度收益。相同指纹也不等于输出确定。',
        'persona':'两类人物在条件权衡与直接表态方面可见区别，但两臂普遍已有；周末、保留杯子及R08仍出现明显样例复用。未证明跨情境人物辨识稳定提高，不以短句率或词汇计数代替判断。',
        'boundary':'32条回复中4条数值严格为172，4条缺失实体均承认未知，4条中途更正均遵从最终想留，4条引用回复均保留合法内容。未发现新增硬事实或任务边界退步；强情绪推断及套用样例仍是表达弱点。严格材料边界退步单独保留，不被上述内容检查覆盖。',
        'selection':metrics['selection'],'strict_gate_passed':False,'lenient_material_gate_passed':True,
        'naturalness_improvement_proven':False,'independent_human_review':False,'paid_judge_calls':0,
        'conclusion':'工程与真实数据收集完成。严格口径v3引入2条原排除样例，未通过原效果要求；事前冻结的宽松口径入模9/12到12/12、排除0/16，支持保留实验方向，但自然度因样本小、复用和后端混杂未获证实。默认v2/Shadow保持，不启用v3，不追加生成或扩大修改。'}
    atomic_json(out/'machine-analysis.json',result)
    md=['# 完整32条回复的机器开发分析','',result['method'],'',
        '样本级偏好：4胜、12平、0负、0无法判断；可单独归因选择器的已证实改善为0。','',
        '| 对照 | 实验臂 | 判断 | 具体依据 |','|---|---|---|---|']
    md += [f"| {e['id']} | {e['experiment_label']} | {e['verdict']} | {e['reason']} {e.get('confound','')} |" for e in entries]
    md += ['','## 归因与边界','',result['causality'],'',result['persona'],'',result['boundary'],'',result['conclusion'],'']
    (out/'machine-analysis.md').write_text('\n'.join(md),encoding='utf-8')


def report():
    root=SUPPLEMENT/'real';out=SUPPLEMENT/'analysis';out.mkdir(exist_ok=True)
    original=read(PARENT/'prepared.json');old=ReceiptLedger(PARENT/'receipts',limit=64).rows()
    new=RetryReceipts(root/'receipts',limit=23).rows();gate=ReceiptLedger(GATE/'receipts',limit=6).rows()
    values={};origins=[]
    for item in original['items']:
        key=item['key'];a=PARENT/'results'/(key+'.json');b=root/'results'/(key+'.json')
        if a.exists() and b.exists():raise ValueError('successful_original_replaced')
        p=a if a.exists() else b
        if p.exists():
            values[key]=read(p);origins.append({'item':key,'source':'original_grok_success' if p==a else 'retry_three','path':str(p),'sha256':sha(p)})
    shutil.copyfile(PARENT/'fixture.json',out/'fixture.json')
    partial_analysis(out,original,values,old+new)
    atomic_json(out/'combined-results.json',values);atomic_json(out/'result-sources.json',origins)
    calls=[]
    for phase,rows in [('gate',gate),('original',old),('retry_three',new)]:
        for row in rows:calls.append({'global_ordinal':len(calls)+1,'phase':phase,**row})
    atomic_json(out/'all-provider-calls.json',calls)
    differences=read(out/'request-differences.json');material=read(out/'material-differences.json')
    fingerprints=[]
    keyed={(r['item'],r['kind']):r for r in old+new}
    for pair in dict.fromkeys(i['pair'] for i in original['items']):
        for kind in ('selector','main'):
            refs=[keyed.get((pair+'-'+arm,kind),{}).get('diagnostics',{}).get('system_fingerprint') for arm in ('control','experiment')]
            fingerprints.append({'pair':pair,'kind':kind,'values':refs,'equal':bool(all(refs) and refs[0]==refs[1])})
    result={'status':'complete' if len(values)==32 else 'incomplete','successful_replies':len(values),
        'original_successes_reused':sum(r['source']=='original_grok_success' for r in origins),
        'new_successful_requests':sum(r['source']=='retry_three' for r in origins),
        'complete_pairs':sum(d['complete'] for d in differences),'all_differences_explained':all(d['explained'] for d in differences if d['complete']),
        'changed_main_pairs':sum(d.get('examples_changed',False) for d in differences),
        'all_final_hashes_match':all(r['hash_matches'] for r in material),'supplement_calls':len(new),
        'supplement_by_kind':dict(Counter(r['kind'] for r in new)),'supplement_failures':sum(r['status']!='completed' for r in new),
        'parent_failed_selector_retried':sum(r['item']=='R08-detail-experiment' and r['kind']=='selector' for r in new),
        'cohort_conservative_calls':len(old)+len(new),'total_including_gate':len(calls),
        'original_terminal_gap_retained':[59],'unknown_usage_calls':sum(not r.get('usage') for r in calls),
        'usage':{'supplement':usage(new),'cohort':usage(old+new),'including_gate':usage(calls)},
        'amount':'未知，以网关账单为准；旧超时用量未恢复','selection':read(out/'selection-metrics.json')['matched'],
        'backend_observations':fingerprints,'paid_judge_calls':0,'qq':0}
    atomic_json(out/'metrics.json',result)
    machine_review(out,result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('backend_observations','selection')},ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':report()
