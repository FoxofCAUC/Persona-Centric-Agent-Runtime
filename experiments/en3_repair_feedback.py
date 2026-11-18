"""Rebuild the Ling blind-feedback archive from frozen study files and author exports."""
import argparse
import json
from collections import Counter
from html import escape
from itertools import combinations
from pathlib import Path

from experiments.corpus_reply_study import read_private, save_once
from experiments.corpus_reply_trial_report import save_text_once
from experiments.en3_repair_blind import BRANCHES, KEY, LABELS, NAMES, bound_review
from persona_runtime.corpus_files import digest

BINDINGS=('study_id','input_contract_sha256','blind_key_sha256','results_sha256')


def unpack(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:
                raise ValueError('duplicate feedback field')
            result[key]=value
        return result
    return json.loads(raw.decode('utf-8-sig'),object_pairs_hook=unique)


def rank(row, mapping):
    first, second=row['first'],row['second']
    if not first or first=='暂难判断':
        if second: raise ValueError('unexpected second choice')
        return [],None
    if first=='相近':
        if second: raise ValueError('unexpected second choice')
        return [list(BRANCHES)],None
    first_choices=[mapping[label] for label in first]
    if not 1<=len(first_choices)<=2 or len(set(first_choices))!=len(first_choices):
        raise ValueError('invalid first choice')
    remainder=[b for b in BRANCHES if b not in first_choices]
    if second=='相近':
        if len(first_choices)!=1: raise ValueError('unexpected tied second')
        return [first_choices,remainder],remainder
    if second:
        branch=mapping[second]
        if len(first_choices)!=1 or branch not in remainder:
            raise ValueError('invalid second choice')
        return [first_choices,[branch], [b for b in remainder if b!=branch]],[branch]
    # Unranked alternatives compare only with an explicitly higher-ranked branch.
    return [first_choices],None


def pairwise(tiers):
    levels={b:i for i,tier in enumerate(tiers) for b in tier}
    result={}
    for a,b in combinations(BRANCHES,2):
        i,j=levels.get(a),levels.get(b)
        if i is None and j is None: choice='unknown'
        elif i==j: choice='tie'
        elif j is None or (i is not None and i<j): choice=a
        else: choice=b
        result[a+'/'+b]=choice
    return result


def summarize(rows):
    first,second,pairs=Counter(),Counter(),{a+'/'+b:Counter() for a,b in combinations(BRANCHES,2)}
    for row in rows:
        tiers=row['ranked_tiers']
        first['+'.join(sorted(tiers[0])) if tiers else 'unknown']+=1
        second['+'.join(sorted(row['explicit_second'])) if row['explicit_second'] else 'unfilled']+=1
        for k,v in row['pairwise'].items():
            pairs[k][v]+=1
    return {'n':len(rows),'first':dict(first),'explicit_second':dict(second),
            'pairwise':{k:dict(v) for k,v in pairs.items()}}


def decode(answers,view,key):
    if (set(answers)!={'version',*BINDINGS,'votes','notes','overall'}
        or answers['version']!='en3-repair-author-feedback-v2'
        or any(answers[k]!=view[k] for k in BINDINGS)):
        raise ValueError('feedback not bound to this study')
    if not isinstance(answers['overall'],str) or not isinstance(answers['votes'],dict) or not isinstance(answers['notes'],dict):
        raise ValueError('invalid review')
    rows=[]
    for scene in view['scenes']:
        group=scene['id']
        mapping=dict(zip(LABELS,key['scenes'][group],strict=True))
        for n in [*(t['n'] for t in scene['turns']),'conversation']:
            position=f'{group}/{n}'
            ballot=answers['votes'].get(position,{'first':'','second':''})
            if not isinstance(ballot,dict) or set(ballot)!={'first','second'}: raise ValueError('invalid ballot')
            tiers,second=rank(ballot,mapping)
            note=answers['notes'].get(position,'')
            if not isinstance(note,str):raise ValueError('invalid note')
            rows.append({'position':position,'scene':group,'regression':scene['regression'],
                         'kind':'conversation' if n=='conversation' else 'turn','vote_present':position in answers['votes'],
                         'verbatim':ballot,'ranked_tiers':tiers,'explicit_second':second,'pairwise':pairwise(tiers),'note':note})
    allowed={r['position'] for r in rows}
    if not (set(answers['votes'])|set(answers['notes'])) <= allowed:raise ValueError('foreign vote position')
    scopes={scope:{k:summarize([r for r in rows if r['kind']==k and (scope=='all' or r['regression']==(scope=='regression'))])
                  for k in ('turn','conversation')} for scope in ('fresh','regression','all')}
    return {'version':'en3-repair-feedback-decoded-v2',**{k:answers[k] for k in BINDINGS},
            'mapping_by_scene':{group:dict(zip(LABELS,v)) for group,v in key['scenes'].items()},
            'branch_names':NAMES,'scopes':scopes,'rows':rows,'overall_verbatim':answers['overall']}


def save_raw(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with path.open('xb') as out:out.write(raw)
    except FileExistsError:
        if path.read_bytes()!=raw:raise ValueError('conflicting original export')


def page(decoded):
    tr=lambda vals:'<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in vals)+'</tr>'
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>角色 · 修正意见解盲</title>',
           '<style>body{max-width:56em;margin:2em auto;padding:0 1em;font:16px/1.65 system-ui}table{border-collapse:collapse}th,td{padding:.4em .8em;text-align:left;border-bottom:1px solid #9997}blockquote{white-space:pre-wrap}summary{cursor:pointer}</style><main><h1>角色的修复扩测 · 更正后人工审阅意见</h1>',
           '<p>作者提交的更正件完全替代先前文件。当前结果仅按更正件计算，正文与整组偏好分别统计。</p>',
           '<table><tr><th>范围</th><th>评价</th>'+''.join('<th>'+escape(NAMES[b])+'</th>' for b in BRANCHES)+'</tr>']
    for scope,title in [('fresh','6组新场景'),('regression','X00定点复测'),('all','全部7组')]:
        for kind,label in [('turn','逐轮首选'),('conversation','整组首选')]:
            a=decoded['scopes'][scope][kind]['first']
            parts.append(tr((title,label,*(a.get(b,0) for b in BRANCHES))))
    parts.extend(['</table><h2>明示排序的两两结果</h2><table><tr><th>范围</th><th>对照双方</th><th>前者更优</th><th>后者更优</th><th>并列</th><th>未排</th></tr>'])
    for scope,title in [('fresh','新场景正文'),('regression','复测正文'),('all','全部正文')]:
        for key,pair in decoded['scopes'][scope]['turn']['pairwise'].items():
            a,b=key.split('/')
            parts.append(tr((title,NAMES[a]+' ／ '+NAMES[b],pair.get(a,0),pair.get(b,0),pair.get('tie',0),pair.get('unknown',0))))
    parts.extend(['</table><p>未填写次选时，仅首选与另外两版的关系可比较。每个完整会话保留自己的甲乙丙映射。</p>'])
    for group,mapping in decoded['mapping_by_scene'].items():
        parts.extend(['<details><summary>'+escape(group+' · '+'，'.join(k+'为'+NAMES[v] for k,v in mapping.items()))+'</summary>',
                      '<table><tr><th>位置</th><th>首选</th><th>次选</th><th>备注</th></tr>'])
        for row in decoded['rows']:
            if row['scene']!=group:continue
            tiers=row['ranked_tiers']
            name=lambda seq:'、'.join(NAMES[b] for b in seq)
            first=name(tiers[0]) if tiers else '未排'
            second=name(row['explicit_second']) if row['explicit_second'] else '未填'
            parts.append(tr((row['position'],first,second,row['note'])))
        parts.append('</table></details>')
    note=next(r for r in decoded['rows'] if r['position']=='X00/6')
    if note['ranked_tiers']==[['O']] and '上课' in note['note']:
        parts.append('<p>原始意见中的措辞沿用作者原文。X00第6轮首选为修复前结构33，核对该版的首条气泡可见作者所指的重复状态。</p>')
    parts.append('<p><a href="submitted.private.json">更正原件</a> · <a href="decoded.private.json">机器可读解盲</a> · <a href="../../review/index.html">本轮盲审页</a></p></main></html>')
    return ''.join(parts)


def archive(root,path,previous):
    view,_=bound_review(root)
    key=read_private(root/KEY)
    new=path.read_bytes();old=previous.read_bytes()
    if new==old:raise ValueError('identical revisions')
    decoded=decode(unpack(new),view,key)
    decode(unpack(old),view,key)
    sha,old_sha=digest(new),digest(old)
    directory=root/'author-feedback'; dest=directory/sha; prior=directory/old_sha
    save_raw(prior/'submitted.private.json',old)
    save_raw(dest/'submitted.private.json',new)
    decision={'replaced_sha256':old_sha,'effective_sha256':sha,'study_id':view['study_id'],
              'use':'corrected_export_only'}
    save_once(prior/'superseded-by.private.json',decision)
    decoded.update(submitted_sha256=sha,replaces_sha256=old_sha,source_file=path.name)
    save_once(dest/'decoded.private.json',decoded)
    save_once(dest/'revision.private.json',decision)
    save_text_once(dest/'index.html', page(decoded))
    return dest


if __name__=='__main__':
    cli=argparse.ArgumentParser()
    cli.add_argument('--root',required=True,type=Path)
    cli.add_argument('--corrected',required=True,type=Path)
    cli.add_argument('--superseded',required=True,type=Path)
    args=cli.parse_args()
    print(archive(args.root,args.corrected,args.superseded))
