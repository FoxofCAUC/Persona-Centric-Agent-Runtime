"""Locked artificial protocol/content cases, zero provider or scoring calls."""
import argparse
import hashlib
import json
from pathlib import Path
from persona_runtime.reply_batch import validate_reply,ReplySchemaError
from persona_runtime.reply_transport import render_parts


def evaluate():
    folder=Path(__file__).resolve().parents[1]/'tests/fixtures/m74'
    records=[]
    for case in json.loads((folder/'acceptance.json').read_text(encoding='utf-8')):
        try:
            batch=validate_reply(case['raw'],'natural_structured')
            assert case['expected']!='fail'
            assert bool(batch.fallback_reason)==(case['expected']=='fallback')
            for item in case.get('required',[]):
                assert any(item in s.text and not s.cancel_if_new_input for s in batch.segments)
            for segment in batch.segments:
                rendered,parts=render_parts(segment.text,segment.content_kind)
                assert ''.join(parts)==rendered and all(len(p)<=450 for p in parts)
            result='passed'
        except ReplySchemaError:
            assert case['expected']=='fail',case['id']
            result='passed_rejection'
        records.append({'id':case['id'],'result':result})
    dev=[]
    for case in json.loads((folder/'development.json').read_text(encoding='utf-8')):
        try:
            rendered,parts=render_parts(case['text'],case['kind'])
            assert not case['fail'] and ''.join(parts)==rendered
        except ReplySchemaError:assert case['fail']
        dev.append({'id':case['id'],'passed':True})
    return {'locked':records,'development':dev,'paid_calls':0,'scoring_calls':0,
            'fixture_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.json')}}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    report=evaluate();args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'locked':len(report['locked']),'development':len(report['development']),'paid_calls':0}))
