"""Import a fixed-history blind ballot without inventing votes or selecting a winner."""
import argparse
from pathlib import Path

from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from .corpus_reply_study import read_private,save_once
from .en3_repair_feedback import unpack,save_raw
from .en5_style_packet import require
from .en5_style_review import bound


GRADES={'','accept','service','help','reject','uncertain'}


def decode(vote,view,key,contract):
    bindings=('study_id','input_contract_sha256','results_sha256','blind_key_sha256')
    require(vote.get('version')=='en5-style-feedback-v1' and set(vote)=={'version',*bindings,'positions','overall'}
            and all(vote[k]==view[k] for k in bindings),'ballot_binding')
    require(isinstance(vote['overall'],str) and isinstance(vote['positions'],dict)
            and set(vote['positions'])<={p['position'] for p in view['cases']},'invalid_ballot')
    rows=[]
    for case,pos in zip(view['cases'],contract['positions'],strict=True):
        code=case['position']
        require(code==pos['position'],'position_changed')
        blank={'ratings':{},'ranks':{},'note':'','all_unacceptable':False}
        answer=vote['positions'].get(code,blank)
        require(isinstance(answer,dict) and set(answer)==set(blank) and isinstance(answer['ratings'],dict)
                and isinstance(answer['ranks'],dict) and type(answer['all_unacceptable']) is bool
                and isinstance(answer['note'],str),'ballot_shape')
        labels={k for k,v in case['options'].items() if v['state']=='completed'}
        r,rank=answer['ratings'],answer['ranks']
        require(set(r)<=labels and set(rank)<=labels and all(isinstance(x,str) and x in GRADES for x in r.values()),
                'invalid_rating')
        completed=bool(labels) and all(r.get(k) for k in labels)
        rejected=completed and all(r[k] in ('service','help','reject') for k in labels)
        distinct=len({case['options'][k]['body'] for k in labels})
        if rank:
            require(completed and not rejected and distinct>1
                    and all(isinstance(v,str) and v in {'',*map(str,range(1,distinct+1))} for v in rank.values()),
                    'rating_required_before_ranking')
        require(answer['all_unacceptable']==rejected,'all_reject_conflict')
        same={}
        groups=[]
        for k,w in key['labels'][code].items():
            o=case['options'][k]
            if k in labels:
                pair=(r.get(k,''),rank.get(k,''))
                require(o['body'] not in same or same[o['body']]==pair,'identical_body_conflict')
                same[o['body']]=pair
            groups.append({'label':k,'arms':[a for a,x in pos['arms'].items() if x['request_sha256']==w],
                           'request_sha256':w,'rating':r.get(k,''),'rank':rank.get(k,''),'state':o['state']})
        rows.append({'position':code,'verbatim':answer,'groups':groups})
    return {'version':'en5-style-decoded-v1',**{k:vote[k] for k in bindings},
            'positions':rows,'overall':vote['overall'],'candidate_decision':None}


def archive(root,path):
    root=outside_repository(root)
    raw=outside_repository(path).read_bytes()
    view,_,_=bound(root)
    decoded=decode(unpack(raw),view,read_private(root/'blind-key.private.json'),read_private(root/'input-contract.private.json'))
    sha=digest(raw)
    folder=root/'author-feedback'/sha
    folder.mkdir(parents=True,exist_ok=True)
    save_raw(folder/'submitted.private.json',raw)
    save_once(folder/'decoded.private.json',decoded|{'submitted_sha256':sha})
    return folder


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--feedback',required=True,type=Path)
    args=parser.parse_args()
    print(archive(args.root,args.feedback))
