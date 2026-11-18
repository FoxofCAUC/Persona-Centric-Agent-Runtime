"""Resolve approved private-corpus proposals against their current external authority."""
from contextlib import contextmanager, nullcontext
import json

from .archive import scope
from .corpus_dataset import DatasetCorpusStore
from .corpus_files import CorpusError, digest
from .corpus_learning import CorpusLearning
from .corpus_store import encoded, fingerprint, outside_repository
from .expression_assets import authority, normalized_scope, policy
from .models import timestamp
from .provider_policy import require_generation_provider


REPLAY = 'offline_replay'
CANDIDATE = 'candidate_runtime'
VERSION = 'dialogue-reviewed-material-v1'


def require(condition, reason):
    if not condition:
        raise CorpusError('dialogue_source_' + reason)


def document(path, expected=None):
    raw = outside_repository(path).read_bytes()
    checksum = digest(raw)
    require(expected is None or checksum == expected, 'document_changed')
    return json.loads(raw), checksum


@contextmanager
def source_window(manifest):
    corpus = DatasetCorpusStore(manifest['corpus_root'])
    learning = CorpusLearning(manifest['learning_root'], corpus)
    with corpus.connection(write=True), learning.connection(write=True):
        yield


def verify_assets(manifest):
    """Resolve proposal, learning receipt, reviewed grant and current source in both stores."""
    corpus = DatasetCorpusStore(manifest['corpus_root'])
    learning = CorpusLearning(manifest['learning_root'], corpus)
    output = {}
    with corpus.connection() as src, learning.connection() as led:
        corpus_id = src.execute('SELECT corpus_id FROM corpus_meta').fetchone()[0]
        require(corpus_id == manifest['corpus_id'], 'wrong_corpus')
        views, grants = {}, set()
        for record in manifest['supports']:
            pid = record['proposal_id']
            require(pid not in output, 'duplicate_proposal')
            row = led.execute('SELECT * FROM learning_proposals WHERE id=?', (pid,)).fetchone()
            require(row and row['data_hash'] == record['proposal_hash'], 'proposal_changed')
            proposal = json.loads(row['data_json'])
            require(fingerprint(proposal) == row['data_hash'], 'proposal_changed')
            job = led.execute('SELECT * FROM learning_jobs WHERE id=?', (row['job_id'],)).fetchone()
            require(job and job['id'] == record['job_id'] and job['state'] == 'completed'
                    and job['approval_id'] == proposal['approval_id'] and job['request_hash'] == proposal['request_hash']
                    and job['raw_response'] is not None and digest(job['raw_response']) == job['raw_response_hash'] == proposal['response_hash'],
                    'learning_receipt_changed')
            if job['approval_id'] not in grants:
                grant = led.execute('SELECT * FROM learning_approvals WHERE id=?', (job['approval_id'],)).fetchone()
                require(grant and grant['state'] == 'active', 'learning_approval_unavailable')
                binding = json.loads(grant['binding_json'])
                require(fingerprint(binding) == grant['binding_hash'] and binding['corpus_id'] == corpus_id, 'learning_grant_changed')
                document(binding['approval_path'], grant['approval_hash'])
                grants.add(job['approval_id'])
            ref = proposal['source_binding']
            vid = ref['view_id']
            if vid not in views:
                view = corpus._view_status(src, vid)
                proof = corpus._source_proof(src, view['parse_id'])
                views[vid] = (view, proof)
            view, proof = views[vid]
            require(record['source_binding'] == ref and ref['kind'] == 'private_corpus_view'
                    and view['payload_hash'] == record['view_hash'] and proof['dataset_id'] == record['dataset_id']
                    and proof['grant_id'] == record['grant_id'] and proof['dataset_revision'] == record['dataset_revision']
                    and view['source_proof_hash'] == record['proof_hash'], 'view_or_grant_changed')
            row = src.execute('SELECT * FROM corpus_view_messages WHERE view_id=? AND message_id=?',
                              (vid, ref['view_message_id'])).fetchone()
            message = json.loads(row['data_json']) if row else {}
            require(row and row['split'] == message['split'] == 'learn' and row['target_candidate']
                    and not message['flags'] and not message['redactions']
                    and 'HUMAN_' + message['role'] == ref['speaker'] and message['text'] == proposal['source_quote']
                    and digest(message['text'].encode()) == row['text_hash'] == ref['content_hash'], 'source_changed')
            output[pid] = {'id': pid, 'situation': proposal['situation'], 'style': proposal['style'],
                           'source_quote': proposal['source_quote']}
    return output


def reviewed_manifest(path, expected=None):
    data, checksum = document(path, expected)
    require(data.get('version') == VERSION and isinstance(data.get('review_files'), list) and data['review_files'],
            'review_manifest_required')
    acceptance, packages = [], {}
    for record in data['review_files']:
        raw = outside_repository(record['path']).read_bytes()
        require(digest(raw) == record['sha256'], 'review_withdrawn_or_changed')
        if data.get('supports') and (b'corpus-research-asset-acceptance-v1' in raw or b'corpus-expression-research-package-v1' in raw):
            evidence = json.loads(raw)
            if evidence.get('version') == 'corpus-research-asset-acceptance-v1':
                acceptance.append(evidence)
            if evidence.get('version') == 'corpus-expression-research-package-v1':
                packages[record['sha256']] = evidence
    if data.get('supports'):
        require(len(acceptance) == 1 and acceptance[0]['package_sha256'] in packages, 'accepted_proposals_required')
        permit = acceptance[0]
        package = packages[permit['package_sha256']]
        proposals = package['proposals']
        require(permit['package_hash'] == package['package_hash'] == fingerprint({k:v for k,v in package.items() if k != 'package_hash'})
                and permit['accepted_proposal_ids'] == [p['proposal_id'] for p in proposals]
                and len(proposals) == len(set(permit['accepted_proposal_ids'])), 'accepted_proposals_changed')
        proposed = {p['proposal_id']:(p['data_hash'],p['job_id']) for p in proposals}
        require(all(proposed.get(ref['proposal_id']) == (ref['proposal_hash'],ref['job_id'])
                    and ref['view_hash'] == package['view_payload_hash'] for ref in data['supports']), 'proposal_outside_review')
    require(all(isinstance(data.get(k), str) and data[k].strip() for k in
                ('flash_system','flash_user','speaker','persona','persona_spec_hash')), 'prompt_material_missing')
    variables = data.get('variables', {})
    required = {'time_block':('prefix',),'dialogue_prompt':('line_format',),
                'expression_habits_block':('item_format','prefix','suffix_if_present'),
                'planner_reasoning':('format',)}
    require(all(isinstance(variables.get(section), dict) and
                all(isinstance(variables[section].get(key),str) for key in keys) for section,keys in required.items()),
            'prompt_binding_missing')
    for key in ('flash_model','selector_model'):
        model = data[key]
        require_generation_provider(model)
        require(model['name'] == 'deepseek-flash' and not set(model) - {
            'name','base_url','max_tokens','max_input_chars','timeout','_dialogue_profile','_json_output'},
            'model_contract_changed')
    require(data['selector_model'].get('_json_output') is True and not data['flash_model'].get('_json_output')
            and set(data['selector_system']) == {'role','content'} and data['selector_system']['role'] == 'system'
            and isinstance(data['selector_system']['content'],str), 'selector_contract_changed')
    numbers = data['candidate_numbers']
    require(isinstance(numbers, dict) and set(numbers) == {str(n) for n in range(1, len(numbers) + 1)}
            and len(set(numbers.values())) == len(numbers), 'selector_numbers_invalid')
    assets = verify_assets(data)
    require(set(numbers.values()) == set(assets), 'selector_pool_changed')
    return data, checksum, assets


def scope_record(db, store, persona_id):
    selected = scope(persona_id, 'desktop')
    return {'persona_id': persona_id, 'scope_key': normalized_scope(persona_id, selected),
            'authority': authority(db, persona_id), 'home': str(store.paths.home.resolve())}


def approval_fields(manifest_hash, target):
    return {'version': 'dialogue-candidate-material-approval-v1', 'purpose': CANDIDATE,
            'provider': 'deepseek_official', 'approved': True, 'manifest_sha256': manifest_hash, **target}


def validate_approval(path, expected, checksum, target):
    data, approval_hash = document(path, expected)
    require(data == approval_fields(checksum, target), 'candidate_approval_required')
    return approval_hash


def register_bundle(store, persona_id, manifest_path, *, approval_path=None):
    """Offline registration has no dispatch rights. Candidate use needs a separate scoped receipt."""
    manifest_path = outside_repository(manifest_path)
    pointer, expected = document(manifest_path)
    with source_window(pointer) if approval_path else nullcontext():
        manifest, checksum, assets = reviewed_manifest(manifest_path, expected)
        return _register(store, persona_id, manifest_path, manifest, checksum, assets, approval_path)


def _register(store, persona_id, manifest_path, manifest, checksum, assets, approval_path):
    with store.connection(write=True) as db:
        target = scope_record(db, store, persona_id)
        spec = db.execute('SELECT spec_json FROM personas WHERE id=?', (persona_id,)).fetchone()[0]
        require(digest(spec.encode()) == manifest['persona_spec_hash'], 'wrong_persona_material')
        purpose = CANDIDATE if approval_path else REPLAY
        if approval_path:
            approval_path = outside_repository(approval_path)
        approval_hash = validate_approval(approval_path, None, checksum, target) if approval_path else None
        bundle_id = fingerprint([target, purpose, str(manifest_path), checksum, str(approval_path), approval_hash])
        existing = db.execute('SELECT state FROM dialogue_source_bundles WHERE id=?', (bundle_id,)).fetchone()
        if existing:
            require(existing['state'] == 'active', 'previous_binding_unavailable')
            check_local_supports(db, bundle_id, manifest, assets)
            return bundle_id
        maximum = policy(db, persona_id)['data']['max_assets']
        used = db.execute('SELECT count(*) FROM expression_assets WHERE persona_id=? AND scope_key=?',
                          (persona_id, target['scope_key'])).fetchone()[0]
        ids = {key: fingerprint(['corpus_support_v2', persona_id, target['scope_key'], manifest['corpus_id'], key])
               for key in assets}
        existing_ids = {r[0] for r in db.execute('SELECT id FROM expression_assets WHERE persona_id=? AND scope_key=?',
                                                 (persona_id, target['scope_key']))}
        require(used + len(set(ids.values()) - existing_ids) <= maximum, 'asset_capacity')
        db.execute('INSERT INTO dialogue_source_bundles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (bundle_id, persona_id, target['scope_key'], target['authority'], target['home'], purpose,
                    str(manifest_path), checksum, str(approval_path) if approval_path else None, approval_hash, 'active', timestamp()))
        for ref in manifest['supports']:
            key = ref['proposal_id']
            asset, aid = assets[key], ids[key]
            content_hash = fingerprint([asset['situation'], asset['style'], []])
            if aid not in existing_ids:
                db.execute('INSERT INTO expression_assets VALUES(?,?,?,1,\'active\')', (aid, persona_id, target['scope_key']))
                db.execute('INSERT INTO expression_asset_versions VALUES(?,1,?,?,\'[]\',?,NULL,?)',
                           (aid, asset['situation'], asset['style'], content_hash, timestamp()))
            db.execute('INSERT INTO dialogue_corpus_supports VALUES(?,?,?,1,?,?)', (bundle_id, key, aid, encoded(ref), fingerprint(ref)))
        check_local_supports(db, bundle_id, manifest, assets)
        return bundle_id


def check_local_supports(db, bundle_id, manifest, assets):
    rows = list(db.execute('''SELECT s.*, a.state, a.revision, v.situation, v.style, v.content_hash, a.scope_key, a.persona_id
            FROM dialogue_corpus_supports s JOIN expression_assets a ON a.id=s.asset_id
            JOIN expression_asset_versions v ON v.asset_id=a.id AND v.revision=s.asset_revision
            JOIN dialogue_source_bundles b ON b.id=s.bundle_id AND b.persona_id=a.persona_id AND b.scope_key=a.scope_key
            WHERE s.bundle_id=?''', (bundle_id,)))
    refs = {r['proposal_id']: r for r in manifest['supports']}
    require(len(rows) == len(refs), 'local_support_missing')
    for row in rows:
        aid = row['proposal_id']
        require(aid in refs and row['state'] == 'active' and row['revision'] == row['asset_revision']
                and row['ref_hash'] == fingerprint(refs[aid]) and json.loads(row['ref_json']) == refs[aid]
                and row['situation'] == assets[aid]['situation'] and row['style'] == assets[aid]['style']
                and row['content_hash'] == fingerprint([row['situation'], row['style'], []]), 'asset_disabled_or_changed')


def resolve_bundle(db, store, persona_id, bundle_id, purpose):
    row = db.execute('SELECT * FROM dialogue_source_bundles WHERE id=? AND persona_id=?', (bundle_id, persona_id)).fetchone()
    require(row and row['state'] == 'active' and row['purpose'] == purpose, 'binding_unavailable')
    target = scope_record(db, store, persona_id)
    require(all(row[k] == v for k, v in target.items()), 'scope_or_authority_changed')
    manifest, checksum, assets = reviewed_manifest(row['manifest_path'], row['manifest_hash'])
    if purpose == CANDIDATE:
        validate_approval(row['approval_path'], row['approval_hash'], checksum, target)
    check_local_supports(db, bundle_id, manifest, assets)
    return manifest, assets


def revoke_bundle(store, persona_id, bundle_id):
    with store.connection(write=True) as db:
        result = db.execute("UPDATE dialogue_source_bundles SET state='revoked' WHERE id=? AND persona_id=?",
                            (bundle_id, persona_id))
        require(result.rowcount == 1, 'binding_unavailable')
