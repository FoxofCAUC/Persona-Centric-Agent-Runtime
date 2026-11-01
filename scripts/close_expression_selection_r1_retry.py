"""One-time R1 supplement closure from saved artificial evidence; no network."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.retry_expression_selection_r1 import (OUT,HOME,ORIGINAL,SUPPLEMENT,ReceiptLedger,
    read,sha,check_original,formal_hashes,sources)
from scripts.report_expression_selection_r1_retry import report
from persona_runtime.config import atomic_json


def main():
    metrics=report();root=SUPPLEMENT/'real';out=SUPPLEMENT/'analysis'
    rows=ReceiptLedger(root/'receipts').rows();prepared=read(root/'prepared.json')
    assert len(rows)==11 and sum(r['status']=='completed' for r in rows)==4
    assert all(r['status']=='failed' for r in rows[-5:])
    assert read(root/'stop.json')['reason']=='failure_stop_rule'
    assert all(r['status']!='reserved' for r in rows)
    check_original(read(root/'original-seal.json'))
    assert sources()==read(root/'execution-sources.json')
    before=read(root/'formal-before.json');after=formal_hashes();assert before==after
    old_formal=read(OUT/'final-protected-files.json')['after']
    assert all(after[str(Path(path).relative_to(HOME))]==digest for path,digest in old_formal.items())
    atomic_json(SUPPLEMENT/'protected-files.json',{'before':before,'after':after,'unchanged':True,
        'formal_and_frozen_review_files':len(before),'original_r1_formal_hashes_match':True})
    items=[]
    for item in prepared['items']:
        calls=[r for r in rows if r['item']==item['key']]
        destination=root/'results'/(item['key']+'.json')
        items.append({'item':item['key'],'operation':item['operation'],
            'calls':[r['ordinal'] for r in calls],'result_saved':destination.exists(),
            'status':'completed' if any(r['kind']=='main' and r['status']=='completed' for r in calls)
                else 'failed_with_receipts' if calls else 'not_called',
            'attempts_remaining_not_authorized_to_resume':max(0,2-len(calls)) if item['operation']=='replay_main' else None})
    audit={'stopped':True,'reason':'fifth_consecutive_actual_model_failure','receipt_pairs':len(rows),
        'reserved_without_result':0,'original_unknown_dispatches_retained':2,
        'last_five_ordinals':[r['ordinal'] for r in rows[-5:]],'items':items,
        'missing_item_result_explanation':'第11条失败已持久化终态回执，随即触发停测，未写汇总结果文件；以回执记账，不是发出状态未知。',
        'failure_diagnostics':[{k:r.get(k) for k in ('ordinal','item','error','seconds','diagnostics')} for r in rows if r['status']=='failed']}
    atomic_json(out/'stop-audit.json',audit)
    previous=read(ORIGINAL/'machine-review.json');reviews=deepcopy(previous['reviews'])
    updates={
        'P03':{'attribution':'实验臂A多入模opinion和small_win，宽松口径接受opinion但不接受small_win。主请求不同；生硬类比是可观察风险，单次差异不能证明因果。'},
        'P05':{'naturalness':'对照臂续验两次仍为空白；实验臂原回复能顺任期玩笑接话，但缺少完整对照。'},
        'P06':{'outcome':'tie','naturalness':'A能沿任期开被窝委员会和判决书玩笑，B用被子谈判接话；两者都能接住输入。A明显复用“理由过于离谱，我得先笑一会儿”和“我站你这边”，未见明确净收益。',
            'persona':'A有站队反应，但依赖样例原句；B也能表现玩笑。人物辨识与模板套用相互牵制，暂不判优胜。',
            'facts':'两边没有编造用户或共同事实；B低风险个人被窝小经历按已有政策允许。',
            'attribution':'新增适用play并伴随不适用small_win/opinion，实验回复来自本次原请求重发。材料变化明确，不能从一次回复断言单一条目的因果效果。'},
        'P07':{'outcome':'tie','naturalness':'A简洁表达留空闲，B增加对满档安排者的理解。两边均完成意见交换；不以短或长判胜。',
            'persona':'两边都用具体歇息与违约感解释立场，也都接近opinion样例；未显示新的人物差异。',
            'facts':'未见关键事实、任务或用户经历捏造问题。','attribution':'两臂请求相同，均为本次补充回复，差异不能归因选择器。'},
        'P08':{'outcome':'tie','naturalness':'A与B都表达周末不愿排满的取舍，措辞均接近opinion样例，没有可解释的明显自然度优势。',
            'persona':'立场可辨，但两边表现相近，额外small_win没有可识别贡献。',
            'facts':'意见交换完成，未发现关键事实问题。','attribution':'实验臂A为本次补充，额外入模small_win；B沿用原成功回复。无法排除调用时间和随机性影响。'},
        'P13':{'naturalness':'两臂续验各两次均为空白，没有完整回复。'},
        'P14':{'naturalness':'对照臂续验首次空白后触发第五次连续失败；实验臂未重发，仍无完整对照。',
            'attribution':'实验臂多入模opinion，宽松口径允许一般取舍立场；没有输出可判断收益。'},
        'P15':{'naturalness':'本次隔离重新受理及模拟通过；实际调用在前面连续更正场景停止，未执行到引用场景。'},
        'P16':{'naturalness':'本次隔离重新受理及模拟通过；真实调用未执行到本对。'},
    }
    for review in reviews:
        review.update(updates.get(review['id'],{}))
        review['original_review_preserved_at']='experiment/machine-review.json'
    counts=dict(Counter(r['outcome'] for r in reviews));counts.setdefault('win',0)
    assert counts=={'tie':8,'loss':3,'undetermined':5,'win':0}
    analysis={'reviewer':'Codex当前开发会话','method':'逐对机器开发分析，无额外评审调用；不是独立人工盲审。',
        'relative_to':'experiment versus control','counts':counts,'reviews':reviews,
        'lenient_scope':'只增加样例适用性敏感性分析；不因标签放宽自动重判自然度。',
        'full_reply_pairs':11,'full_pairs_with_different_main_requests':3,
        'limitations':['原结果已知后制定宽松标签，非独立无偏评测。','真实选择/回复仍未完成；连续更正与引用缺少结果。',
                      '同请求输出差异不归因选择器；跨时重发无法保证模型输出不变。'],
        'decision':'保留实现与证据，默认v2/正式Shadow不变；未证实自然度净改善，不判阶段通过。'}
    atomic_json(out/'machine-review.json',analysis)
    report_path=ROOT/'docs/M7.6.3-R1-续验与宽松适用性分析.md'
    (SUPPLEMENT/'report.md').write_bytes(report_path.read_bytes())
    source_paths=[ROOT/name for name in subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode('utf-8').split('\0') if name]
    hashes={str(p.relative_to(ROOT)).replace('\\','/'):sha(p) for p in source_paths if p.is_file()}
    atomic_json(SUPPLEMENT/'source-hashes.json',hashes)
    with zipfile.ZipFile(SUPPLEMENT/'source.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for name in hashes:archive.write(ROOT/name,name)
    (SUPPLEMENT/'worktree.patch').write_bytes(subprocess.check_output(['git','diff','--binary','HEAD'],cwd=ROOT))
    old_sources=read(OUT/'final-source-hashes.json')
    changed=[p for p,digest in hashes.items() if old_sources.get(p)!=digest]
    assert not any(p.startswith('persona_runtime/') for p in changed)
    assert not any(p in ('pyproject.toml','uv.lock','tests/expression_selection_fixture.py','tests/fixtures/prompt-baseline-r0.json') for p in changed)
    evidence={str(p.relative_to(SUPPLEMENT)):sha(p) for p in SUPPLEMENT.rglob('*') if p.is_file()
        and p.name not in ('closure.json',) and 'runtime' not in p.relative_to(SUPPLEMENT).parts}
    atomic_json(SUPPLEMENT/'closure.json',{'stage':'M7.6.3-R1 authorized supplement','candidate':'0.7.6.dev6',
        'status':'bounded_retry_stopped_on_fifth_consecutive_model_failure','stage_acceptance_passed':False,
        'engineering_verified':True,'real_effect_verification_complete':False,'selector_default':'character-selector-v2',
        'formal_mode':'Shadow','new_runtime_changes':False,'schema':20,'export':17,'config':2,'bridge':'0.7.2',
        'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'tests':{'original_r1_affected':31,'receipt_final':12,'overlapping_runs_not_added':True,'r0_scenarios':34,'r0_differences':0},
        'simulation':read(SUPPLEMENT/'simulation/run-status.json'),'real':metrics,'machine_review_counts':counts,
        'original_evidence_unchanged':True,'protected_files_unchanged':True,'supplement_receipts_complete':True,
        'ui':'unchanged; original isolated verification retained; no new UI session',
        'qq':0,'formal_service_started':False,'committed':False,'pushed':False,'deployed':False,
        'source_files':len(hashes),'changed_from_original_r1':changed,'evidence_hashes':evidence})
    print(json.dumps({'closed':True,'new_calls':len(rows),'successful_replies':metrics['successful_replies'],
        'stage_passed':False,'protected_files_unchanged':True,'changed_source_files':len(changed)}))


if __name__=='__main__':main()
