"""Build the private scene material and wire review from the authorized E.N5 receipts."""
import argparse
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.character_context import select_context
from persona_runtime.context_m4 import build_context
from persona_runtime.corpus_store import outside_repository
from persona_runtime.knowledge_search_notice import request_model
from persona_runtime.provider_compat import build_request
from persona_runtime.turn_input import load_turn_input
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .en5_decision_packet import baseline_records, file_hash, position_targets, visible_turns
from .en5_experience_setup import binding_at
from .en5_scene_voice import SCENES, VERSION, context, jev_request, project, require
from .en5_style_packet import frozen_evidence

MARKER = '314f4a70-f996-4699-be95-d3e04ce920b1'
LABELS = dict(zip(SCENES, ('日常交流','情绪与关系','明确求助','收束','日常兜底'),strict=True))


def excerpts(turn, case):
    profile = json.loads(turn['profile_snapshot'])
    core = profile['core']['data']
    style = profile['expression']['data']['style']
    rules = json.loads(turn['character_snapshot'])['data']['social']['context_rules']
    fields = {'core.introduction':core['introduction'], 'expression.style':style, 'social.context_rules':rules}
    fields.update({f'core.traits[{i}]':v for i,v in enumerate(core['core_traits'])})
    fields.update({f'core.boundaries[{i}]':v for i,v in enumerate(core['values_and_boundaries'])})
    def span(key, value=None):
        field = fields[key]
        value = field if value is None else value
        require(value and field.count(value)==1 and value in case['original']['messages'][0]['content'],'source_excerpt')
        start = field.index(value)
        return {'field':key,'start':start,'end':start+len(value),'text':value,'field_text':field,
                'field_sha256':fingerprint(field),'profile_version':profile['expression' if key=='expression.style' else 'core']['id']
                if key!='social.context_rules' else case['character_version']}
    identity = span('core.introduction',core['introduction'].partition('。')[0]+'。')
    return {
        'daily':[identity,span('core.traits[0]'),span('expression.style',style.split('。')[1]+'。')],
        'relationship':[identity,span('core.traits[4]'),span('social.context_rules',rules.split('；')[1]),
                        span('expression.style',style.split('。')[2]+'。')],
        'help':[identity,span('core.traits[2]'),span('social.context_rules',rules.split('；')[2].split('，')[0]),
                span('core.boundaries[1]',core['values_and_boundaries'][1].split('，')[-1].split('。')[0])],
        'closing':[identity,span('expression.style',style.split('：',1)[1].split('。')[0]+'。')],
        'fallback':[identity,span('expression.style',style.split('：',1)[0]),span('core.boundaries[2]')]
    }


def prepare(root):
    root = outside_repository(root)
    state = read_private(root.parent/'prompt-review-state.json')
    material_path = root/'material.private.json'
    require(file_hash(material_path)==state['en5_decision_material_sha256'],'frozen_material')
    material = read_private(material_path)
    prior = outside_repository(material['prior'])
    source, records = baseline_records(prior)
    paths, binding = binding_at(source)
    jev_model = read_private(paths.home/'private/knowledge-router/config.json')['name']
    report = read_private(paths.home/'exports/en5/report.private.json')
    by_turn = {r['turn_id']:r for r in records}
    directory = root/'scene-v1'
    rows = []
    cards = None
    with closing(sqlite3.connect((paths.home/'data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        turns = visible_turns(db,paths,binding,report,records)
        for case in material['cases']:
            tid = case['turn_id']
            record = by_turn[tid]
            turn = turns[tid]['turn']
            require(case['original']==record['wire'] and case['original_sha256']==fingerprint(case['original'])
                    and case['targets']==position_targets(db,record,turns),'frozen_request')
            selected = excerpts(turn,case)
            if cards is None:
                cards = selected
            require(cards==selected,'character_revision')
            details = json.loads(db.execute('SELECT details_json FROM context_uses WHERE turn_id=?',(tid,)).fetchone()[0])
            evidence = frozen_evidence(db,turn,details)
            members = [e['text'] for e in load_turn_input(db,turn)]
            character = select_context(json.loads(turn['character_snapshot']),'\n\n'.join(members),members=members,
                                       scenario=[],example_limit=4)
            model = record['model_public']
            outgoing = {}
            for phase in ('main','notice'):
                notice = phase=='notice'
                messages,diagnostic = build_context(None,db,turn|{'_search_notice':notice},evidence,character)
                if not notice:
                    require(build_request(model,messages)[0]==record['wire'],'baseline_reconstruction')
                projected, _ = project(messages,diagnostic['prompt_assembly'],MARKER,notice=notice)
                wire = build_request(request_model(model) if notice else model,projected)[0]
                require(wire['messages'][0]['content'].count(MARKER)==1,'card_slot')
                outgoing[phase] = wire
            state_input = context(case['targets'])
            require(state_input is not None,'fixed_scene_context_budget')
            rows.append({'position':case['position'],'users':members,'baseline':record['body'],
                         'original_hash':record['request_sha256'],'targets_hash':fingerprint(case['targets']),
                         'state':state_input,**outgoing})
    require(len(rows)==16 and len(records)==30,'packet_count')
    view = {'version':VERSION,'persona':binding['persona_id'],'character_version':case['character_version'],
            'profile_sha256':case['profile_sha256'],'material_sha256':file_hash(material_path),
            'model':jev_model,'marker':MARKER,'labels':LABELS,'cards':cards,'cases':rows,
            'jev_draft':jev_request(jev_model,[], '',dict.fromkeys(SCENES,''),draft=True)}
    packet = directory/'inputs.private.json'
    packet_hash = save_once(packet,view)
    for scene,parts in cards.items():
        save_once(directory/'source-excerpts'/(scene+'.json'),{'scene':scene,'packet_sha256':packet_hash,'parts':parts})
    page = Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    payload = json.dumps(dict(view,packet_sha256=packet_hash),ensure_ascii=False).replace('<',r'\u003c')
    target = directory/'index.html'
    save_text_once(target,page.replace('@@PAYLOAD@@',payload))
    return target


def freeze_author_review(root, feedback):
    target = prepare(root)
    packet = target.parent/'inputs.private.json'
    view = read_private(packet)
    result = read_private(outside_repository(feedback))
    require(result.get('version')==VERSION and result.get('packet_sha256')==file_hash(packet)
            and result.get('reviewed') is True,'author_review')
    bodies, criteria, instruction = result['bodies'],result['criteria'],result['instruction']
    positions = {r['position'] for r in view['cases']}
    limits = {r['position']:r['capacity'] for r in read_private(root/'material.private.json')['cases']}
    require(set(bodies)==set(SCENES) and all(isinstance(v,str) and v.strip() for v in bodies.values())
            and set(result['choices'])==positions and set(result['choices'].values())<=set(SCENES),'author_fields')
    catalog = {'version':VERSION,'persona':view['persona'],'character_version':view['character_version'],
               'profile_sha256':view['profile_sha256'],'instruction':instruction,'criteria':criteria,
               'cards':{k:fingerprint(v) for k,v in bodies.items()},'source_packet':file_hash(packet)}
    revisions = target.parent/'author-revisions'/fingerprint(result)
    cases = []
    for row in view['cases']:
        jev = jev_request(view['model'],row['state'],instruction,criteria)
        require(len(json.dumps(jev,ensure_ascii=False))<=limits[row['position']],'author_jev_budget')
        choice = result['choices'][row['position']]
        utterances = {}
        for phase in ('main','notice'):
            wire = deepcopy(row[phase])
            wire['messages'][0]['content'] = wire['messages'][0]['content'].replace(MARKER,bodies[choice],1)
            require(sum(len(msg['content']) for msg in wire['messages'])<=limits[row['position']],'author_pro_budget')
            utterances[phase] = wire
        cases.append({'position':row['position'],'choice':choice,'jev':jev,**utterances})
    for scene in SCENES:
        save_once(revisions/(scene+'.json'),{'body':bodies[scene]})
    save_once(revisions/'catalog.json',catalog)
    save_once(revisions/'requests.private.json',cases)
    save_once(revisions/'author-review.private.json',result)
    return revisions


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',required=True,type=Path)
    cli.add_argument('--author-review',type=Path)
    args=cli.parse_args()
    print(freeze_author_review(args.root,args.author_review) if args.author_review else prepare(args.root))
