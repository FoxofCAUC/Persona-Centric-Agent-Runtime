"""Blind review of Ling's repaired candidate against v8 Pro and the previous candidate."""
import argparse
from html import escape
from itertools import permutations
import json
from pathlib import Path
from secrets import SystemRandom

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once


BRANCHES = ('B', 'O', 'R')
LABELS = ('甲', '乙', '丙')
NAMES = {'B':'原 v8 Pro', 'O':'修复前结构33', 'R':'修复候选'}
KEY = 'blind-key.private.json'
STUDY = 'en3-ling-repair-02'


def require(ok, reason):
    if not ok:
        raise CorpusError('en3_repair_review_' + reason)


def freeze(root, materials, regression_source_sha256):
    root = outside_repository(root)
    path = root/KEY
    ids = [s['id'] for s in materials['scenes']]
    require(len(ids) >= 3 and len(ids)==len(set(ids)), 'invalid_scene_set')
    require('X00' not in ids and isinstance(regression_source_sha256, str) and len(regression_source_sha256)==64, 'regression_identity')
    ids.append('X00')
    binding = fingerprint([materials, regression_source_sha256])
    if not path.exists():
        require(not any((root/f).exists() for f in ('inputs.html','input-contract.private.json',
                    'calls.sqlite3','author-use.private.json','results.private.json')), 'original_key_required')
        rng = SystemRandom()
        choices = list(permutations(BRANCHES))
        order = []
        while len(order)<len(ids):
            order.extend(rng.sample(choices,len(choices)))
        save_once(path, {'version':'en3-repair-key-v2', 'material_hash':binding, 'scenes':dict(zip(ids,order))})
    key = read_private(path)
    require(key['version']=='en3-repair-key-v2' and list(key['scenes'])==ids and key['material_hash']==binding
            and all(sorted(v)==sorted(BRANCHES) for v in key['scenes'].values()), 'key_changed')
    return digest(path.read_bytes())


def bound_review(root):
    root = outside_repository(root)
    mp,cp,rp = (root/f for f in ('materials.private.json','input-contract.private.json','results.private.json'))
    materials,contract,results = map(read_private,(mp,cp,rp))
    require(contract['study_id']==results['study_id']==STUDY and results['version']=='en3-ling-repair-results-v2'
            and results['input_contract_sha256']==digest(cp.read_bytes())
            and contract['materials_sha256']==digest(mp.read_bytes()) and (root/KEY).is_file()
            and contract['blind_key_sha256']==freeze(root,materials,contract['regression']['source_results_sha256']), 'unbound_results')
    key=read_private(root/KEY)['scenes']
    require([s['id'] for s in results['scenes']]+['X00']==list(key), 'wrong_scenes')
    reg, plan = results['regression'], contract['regression']
    require(reg['id']==plan['id']=='X00' and reg['source_scene_id']==plan['source_scene_id']=='G02', 'wrong_regression')
    fixed = {'id':'X00','title':'原项目讨论组复测','turns':plan['turns']}
    scenes, evidence = [], []
    for scene,frozen in zip([*results['scenes'], reg], [*materials['scenes'], fixed], strict=True):
        group = scene['id']
        require(len(scene['turns'])==len(frozen['turns'])>0,'incomplete_positions')
        turns = []
        for n,(t,item) in enumerate(zip(scene['turns'],frozen['turns']),1):
            require(t['index']==item['turn_number']==n and t['user']==item['text']
                    and set(t['bodies'])==set(BRANCHES),'wrong_turn')
            options = {}
            for label,branch in zip(LABELS,key[group],strict=True):
                outcome = t['bodies'][branch]
                reused = outcome.get('reused_from')
                if group=='X00' and branch in ('B','O'):
                    source = 'B' if branch=='B' else 'R'
                    require(reused=={'study':'en3-ling-01','scene':'G02','index':n,'branch':source},
                            'regression_control_changed')
                else:
                    require(reused is None, 'unexpected_reuse')
                state,body,parts=outcome['state'],outcome['body'],outcome['segments']
                if state=='completed':
                    require(isinstance(body,str) and body.strip() and isinstance(parts,list) and parts
                            and all(isinstance(p,str) and p for p in parts) and ''.join(parts)==body,
                            'invalid_visible_text')
                else:
                    require(state in ('failed','unknown','blocked') and body is None and not parts,'invalid_gap')
                options[label]={'state':state,'text':body,'parts':parts}
                evidence.append({'scene':group,'turn':n,'label':label,'branch':NAMES[branch],
                                 'record':{k:v for k,v in outcome.items() if k not in ('body','segments')}})
            same=all(o['state']=='completed' for o in options.values()) and len({o['text'] for o in options.values()})==1
            turns.append({'n':n,'user':item['text'],'options':options,'same':same})
        scenes.append({'id':group,'title':frozen['title'],'turns':turns,'regression':group=='X00'})
    view={'study_id':STUDY,'input_contract_sha256':results['input_contract_sha256'],
          'blind_key_sha256':contract['blind_key_sha256'],'results_sha256':digest(rp.read_bytes()),'scenes':scenes}
    return view,{'slots':evidence,'accounting':results['accounting']}


def export(root):
    root = outside_repository(root)
    view,evidence=bound_review(root)
    target=root/'review'/'index.html'
    page=Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    data=json.dumps(view,ensure_ascii=False).replace('<', r'\u003c')
    save_text_once(target,page.replace('@@DATA@@',data))
    details=['<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>角色 · 扩测技术记录</title>',
             '<style>body{max-width:62rem;margin:2rem auto;padding:0 1rem;font:16px/1.6 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>',
             '<h1>运行证据与模型身份</h1><h2>耗时与用量</h2><pre>',
             escape(json.dumps(evidence['accounting'],ensure_ascii=False,indent=2)), '</pre>']
    for entry in evidence['slots']:
        heading=f"{entry['scene']} · {entry['turn']} · {entry['label']} · {entry['branch']}"
        details.extend(['<details><summary>',escape(heading),'</summary><pre>',
                        escape(json.dumps(entry['record'],ensure_ascii=False,indent=2)),'</pre></details>'])
    save_text_once(target.with_name('evidence.html'),''.join(details))
    return target


if __name__=='__main__':
    cli=argparse.ArgumentParser()
    cli.add_argument('--root',type=Path,required=True)
    print(export(cli.parse_args().root))
