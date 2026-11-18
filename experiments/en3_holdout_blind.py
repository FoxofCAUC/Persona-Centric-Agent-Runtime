"""Freeze a stable anonymous reading order before the first holdout request."""
import json
from secrets import SystemRandom

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once


BRANCHES = ('A', 'B', 'R')
LABELS = ('甲', '乙', '丙')
KEY = 'blind-key.private.json'


def require(condition, reason):
    if not condition:
        raise CorpusError('en3_blind_' + reason)


def freeze(root, materials):
    root = outside_repository(root)
    ids = [s['id'] for s in materials['scenes']]
    require(len(ids) == len(set(ids)) == 2, 'scene_scope')
    binding = fingerprint(materials)
    target = root / KEY
    if not target.exists():
        require(not any((root / p).exists() for p in ('input-contract.private.json', 'results.private.json',
                'calls.sqlite3', 'inputs.html')), 'original_key_required')
        random = SystemRandom()
        first = random.sample(BRANCHES, len(BRANCHES))
        offset = random.choice((1, 2))
        second = first[offset:] + first[:offset]
        save_once(target, {'version':'en3-scene-blind-v1', 'materials':binding,
                           'scenes':{ids[0]:first, ids[1]:second}})
    key = read_private(target)
    require(key.get('version') == 'en3-scene-blind-v1' and key['materials'] == binding
            and list(key['scenes']) == ids and all(sorted(row) == sorted(BRANCHES)
            for row in key['scenes'].values()), 'changed_key')
    return digest(target.read_bytes())


def assemble_review(root):
    root = outside_repository(root)
    material_path, contract_path, results_path = (root / name for name in
        ('materials.private.json', 'input-contract.private.json', 'results.private.json'))
    materials, contract, results = map(read_private, (material_path, contract_path, results_path))
    contract_hash, result_hash = (digest(p.read_bytes()) for p in (contract_path, results_path))
    require(contract['materials_sha256'] == digest(material_path.read_bytes())
            and (root / KEY).is_file() and freeze(root, materials) == contract['blind_key_sha256'],
            'input_changed')
    require(results['version'] == 'en3-ling-results-v1' and results['study_id'] == contract['study_id']
            and results['input_contract_sha256'] == contract_hash, 'results_not_bound')
    key = read_private(root / KEY)['scenes']
    frozen = materials['scenes']
    require([s['id'] for s in results['scenes']] == [s['id'] for s in frozen], 'result_scene_mismatch')
    scenes, evidence = [], []
    for src, scene in zip(frozen, results['scenes']):
        scene_id = src['id']
        require(len(src['turns']) == len(scene['turns']) == 6, 'result_turn_count')
        turns = []
        for number, (item, row) in enumerate(zip(src['turns'], scene['turns']), 1):
            require(row['index'] == number and item['text'] == row['user']
                    and set(row['bodies']) == set(BRANCHES), 'result_source_mismatch')
            options = {}
            for label, branch in zip(LABELS, key[scene_id]):
                entry = row['bodies'][branch]
                body, parts = entry.get('body'), entry.get('segments')
                if entry['state'] == 'completed':
                    require(isinstance(body, str) and body.strip() and isinstance(parts, list)
                            and all(isinstance(s, str) and s for s in parts)
                            and ''.join(parts) == body, 'presentation_changed')
                else:
                    require(body is None and not parts, 'unconfirmed_text')
                options[label] = {'state':entry['state'], 'body':body, 'segments':parts or []}
                evidence.append({'scene':scene_id, 'turn':number, 'label':label, 'branch':branch,
                                 **{k: entry.get(k) for k in ('understanding', 'selected_ids', 'selected_assets', 'stages', 'error_code')}})
            turns.append({'n':number, 'user':item['text'], 'options':options,
                          'identical':all(x['state']=='completed' for x in options.values())
                          and len({x['body'] for x in options.values()})==1})
        scenes.append({'id':scene_id, 'title':src['title'], 'turns':turns})
    review = {'study_id':contract['study_id'], 'input_contract_sha256':contract_hash,
              'results_sha256':result_hash, 'blind_key_sha256':contract['blind_key_sha256'], 'scenes':scenes}
    return review, {'accounting':results['accounting'], 'slots':evidence}


def export(root):
    from html import escape
    from pathlib import Path
    root = outside_repository(root)
    review, evidence = assemble_review(root)
    output = root / 'review' / 'index.html'
    source = Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    payload = json.dumps(review, ensure_ascii=False).replace('<', r'\u003c')
    save_text_once(output, source.replace('@@DATA@@', payload))
    details = ['<!doctype html><meta charset="utf-8"><title>三链运行证据</title>',
               '<style>body{font:16px/1.6 system-ui;margin:2em auto;max-width:65em}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>',
               '<h1>三链运行证据</h1><p>此页公开每组甲乙丙的模型身份。</p><h2>用量和耗时</h2>',
               '<pre>' + escape(json.dumps(evidence['accounting'], ensure_ascii=False, indent=2)) + '</pre>']
    for record in evidence['slots']:
        summary = f"{record['scene']} · 第{record['turn']}轮 · {record['label']} · {record['branch']}"
        details.extend(('<details><summary>' + escape(summary) + '</summary>',
                        '<pre>' + escape(json.dumps(record, ensure_ascii=False, indent=2)) + '</pre></details>'))
    save_text_once(output.with_name('evidence.html'), ''.join(details))
    return output


if __name__ == '__main__':
    import argparse
    from pathlib import Path
    cli = argparse.ArgumentParser(description='Export first-receipt results for an offline blind reading.')
    cli.add_argument('--root', required=True, type=Path)
    print(export(cli.parse_args().root))
