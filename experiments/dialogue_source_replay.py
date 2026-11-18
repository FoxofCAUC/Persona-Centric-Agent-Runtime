"""Import the approved E.L4 material and compare the candidate offline to first receipts."""
import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.dialogue_assembly import load_bundle, load_offline_bundle
from persona_runtime.expression_assets import ExpressionAssetError, asset
from persona_runtime.archive import scope
from persona_runtime.expression_corpus_sources import VERSION, document, register_bundle, require
from persona_runtime.models import PersonaSpec
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from .corpus_reply_study import save_once
from .corpus_reply_trial import parse_receipt
from .fresh_scene_assembly import Assembly


def export_reviewed(state_path, destination):
    root = outside_repository(destination)
    root.mkdir(parents=True, exist_ok=True)
    state, _ = document(state_path)
    old = Assembly(state_path, root)
    decision, _ = document(state['fresh_scene_overall_author_receipt'])
    require(decision['decision'] == 'approved' and decision['scope'] == 'candidate_engineering'
            and decision['formal_activation'] is False and decision['result_set_hash'] == state['fresh_scene_result_set_hash']
            and decision['feedback_sha256'] == state['fresh_scene_feedback_sha256']
            and decision['manifest_sha256'] == state['fresh_scene_manifest_sha256'], 'value_review_required')
    fresh = outside_repository(state['fresh_scene_study_root'])
    manifest, _ = document(fresh / 'manifest.private.json', decision['manifest_sha256'])
    results, _ = document(fresh / 'results.private.json', decision['result_set_hash'])
    require(results['manifest_sha256'] == decision['manifest_sha256'], 'wrong_results')
    corpus = DatasetCorpusStore(old.plan['corpus_root'])
    with corpus.connection() as db:
        view = corpus._view_status(db, old.plan['view_id'])
        proof = corpus._source_proof(db, view['parse_id'])
        corpus_id = db.execute('SELECT corpus_id FROM corpus_meta').fetchone()[0]
    supports = [{'proposal_id':key, 'job_id':item['job_id'], 'proposal_hash':item['proposal_hash'],
                 'source_binding':item['source_binding'], 'view_hash':view['payload_hash'],
                 'proof_hash':view['source_proof_hash'], **{k:proof[k] for k in ('dataset_id','dataset_revision','grant_id')}}
                for key, item in old.assets.items()]
    files = {str(path): value for path, value in old.approval_hashes.items()}
    for path, expected in old.plan['source_files'].items():
        if Path(path).suffix.lower() != '.py':
            files[str(outside_repository(path))] = expected
    for path in (state['fresh_scene_overall_author_receipt'], state['fresh_scene_feedback_original'],
                 old.sources.study / 'plan.private.json', fresh / 'manifest.private.json',
                 fresh / 'author-use.private.json'):
        resolved = outside_repository(path)
        files[str(resolved)] = digest(resolved.read_bytes())
    approved = {'version':VERSION, 'persona_spec_hash':old.snapshot['spec_utf8_sha256'],
                'corpus_root':old.plan['corpus_root'], 'learning_root':old.plan['learning_root'], 'corpus_id':corpus_id,
                'review_files':[{'path':path, 'sha256':checksum} for path, checksum in sorted(files.items())],
                'supports':supports, 'candidate_numbers':{str(i):v for i,v in old.numbers.items()},
                'flash_system':old.prompts['Flash-system.txt'], 'flash_user':old.prompts['Flash-user.template.txt'],
                'variables':{k:old.variables[k] for k in ('time_block','dialogue_prompt','expression_habits_block','planner_reasoning')},
                'persona':old.persona, 'speaker':old.speaker, 'selector_system':old.selector_system,
                'selector_model':old.selector_model, 'flash_model':old.understanding_model}
    path = root / 'source-bundle.private.json'
    save_once(path, approved)
    return path, old, manifest, fresh, results


def record(db, slot):
    row = db.execute('SELECT r.* FROM uses u JOIN requests r ON r.hash=u.hash WHERE u.slot=?', (slot,)).fetchone()
    require(row and row['state'] == 'completed' and row['http'] == 200 and digest(row['raw']) == row['raw_hash'],
            'first_receipt_changed')
    wire = json.loads(row['wire'])
    parsed = parse_receipt(row['raw'], wire['stream'])
    require(row['hash'] == fingerprint(wire) and parsed == json.loads(row['result']) and parsed['valid'], 'first_wire_changed')
    return wire, parsed['body']


async def verify(state_path, destination):
    path, original, manifest, source, results = export_reviewed(state_path, destination)
    root = path.parent
    totals = {'understanding':0, 'selection':0, 'reply':0, 'display':0}
    with TemporaryDirectory(dir=root, prefix='rc8-offline-') as temporary:
        store = Store(RuntimePaths(Path(temporary)))
        store.initialize()
        service = RuntimeService(store)
        persona_id = service.create_persona(PersonaSpec.from_dict(original.snapshot['persona_spec']))['id']
        service.use_persona(persona_id)
        bundle_id = register_bundle(store, persona_id, path)
        with store.connection() as db:
            assembly = load_offline_bundle(store, db, persona_id, bundle_id)
            try:
                load_bundle(store, db, persona_id, bundle_id)
            except ValueError:
                pass
            else:
                raise ValueError('research_receipt_granted_dispatch')
            for row in db.execute('SELECT asset_id FROM dialogue_corpus_supports WHERE bundle_id=?', (bundle_id,)):
                try:
                    asset(db, persona_id, scope(persona_id, 'desktop'), row[0])
                except ExpressionAssetError:
                    pass
                else:
                    raise ValueError('legacy_selector_exposed_private_asset')
        with closing(sqlite3.connect((source / 'calls.sqlite3').as_uri()+'?mode=ro', uri=True)) as calls:
            calls.row_factory = sqlite3.Row
            cases = {(row['scene_id'], row['turn']):row for row in results['cases']}
            for scene in manifest['scenes']:
                history = []
                for n, item in enumerate(scene['turns'], 1):
                    text, stamp = item['text'], item['accepted_at']
                    turn = {'id':f"{scene['id']}.{n:02}", 'persona_id':persona_id}
                    baseline, v8_wire = await original.baseline(scene['id'], n, text, stamp, history, 'R')
                    material = assembly.material(turn, bundle_id, baseline, v8_wire['messages'], 'main', history, text, stamp)
                    thought, chosen = None, []
                    for stage, slot in (('understanding','understanding'), ('selection','selection'), ('reply','body')):
                        old_wire, body = record(calls, f"{scene['id']}.{n:02}.R.{slot}")
                        _, new_wire = assembly.request(stage, turn, material, thought, chosen)
                        require(old_wire == new_wire, 'candidate_request_differs')
                        totals[stage] += 1
                        if stage == 'understanding':
                            thought = body
                        elif stage == 'selection':
                            chosen = assembly.parse_selection(body, material)
                    projected = assembly.projection(body, material)
                    saved = cases[scene['id'], n]['bodies']['R']
                    require(saved['body'] == projected['display_body'] and saved['raw_body'] == body
                            and saved['removed_spans'] == projected['removed_spans'], 'display_differs')
                    history.append((text, projected['display_body'], stamp))
                    totals['display'] += 1
    report = {'version':'direct-reply-offline-equivalence-v1', 'scenes':len(manifest['scenes']), 'checked':totals,
              'supports':len(original.assets), 'review_material_sha256':digest(path.read_bytes()),
              'research_dispatch_rejected':True, 'legacy_selector_excluded':True, 'new_requests':0}
    save_once(root / 'equivalence.private.json', report)
    return report


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--state',type=Path,required=True)
    cli.add_argument('--destination',type=Path,required=True)
    args = cli.parse_args()
    print(json.dumps(asyncio.run(verify(args.state,args.destination)), ensure_ascii=False))
