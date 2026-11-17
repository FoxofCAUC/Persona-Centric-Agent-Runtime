"""Validate author-approved, existing-template messages without rewriting them."""
import json
from pathlib import Path

from .corpus_files import CorpusError, digest
from .corpus_store import fingerprint
from .expression_learning import learning_messages
from .provider_compat import build_request
from .provider_policy import require_generation_provider
from .vendor.maibot_expression_learning_prompt import PROMPT


def read_document(path, maximum=131072):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise CorpusError('corpus_learning_document_capacity')
        value = json.loads(raw.decode('utf-8'))
    except CorpusError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise CorpusError('corpus_learning_document_invalid') from None
    if not isinstance(value, dict):
        raise CorpusError('corpus_learning_document_invalid')
    return value, raw


def child(root, name):
    if not isinstance(name,str) or Path(name).name!=name:
        raise CorpusError('corpus_learning_document_path_invalid')
    path = (root/name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise CorpusError('corpus_learning_document_path_invalid')
    return path


def load_approved_bundle(corpus, approval_path, preview_root):
    preview_root = Path(preview_root).resolve()
    approval_path = Path(approval_path)
    if not approval_path.is_absolute() or not preview_root.is_dir():
        raise CorpusError('corpus_learning_paths_invalid')
    approval, approval_raw = read_document(approval_path)
    manifest, manifest_raw = read_document(preview_root/'manifest.private.json')
    if (approval.get('version') != 'corpus-learning-approval-v1'
            or approval.get('allowed_provider') != 'deepseek_official'
            or approval.get('allowed_model') != 'deepseek-flash'
            or approval.get('max_dispatches') != 2
            or approval.get('automatic_asset_publication') is not False
            or approval.get('new_prompt_writing_authorized') is not False
            or digest(manifest_raw) != approval.get('preview_manifest_sha256')):
        raise CorpusError('corpus_learning_approval_invalid')
    if (approval.get('view_id') != manifest.get('view_id')
            or approval.get('view_payload_hash') != manifest.get('view_payload_hash')
            or not isinstance(manifest.get('batches'), list) or len(manifest['batches']) != 2
            or not isinstance(approval.get('batches'), list) or len(approval['batches']) != 2):
        raise CorpusError('corpus_learning_approval_scope_changed')
    root = Path(__file__).resolve().parents[1]
    expected_paths = {'legacy_assembler':'persona_runtime/expression_learning.py',
                      'vendor_template':'persona_runtime/vendor/maibot_expression_learning_prompt.py',
                      'wire_serializer':'persona_runtime/provider_compat.py'}
    for name, expected in expected_paths.items():
        record = manifest.get(name, {})
        if record.get('path') != expected or record.get('sha256') != digest((root/expected).read_bytes().replace(b'\r\n',b'\n')):
            raise CorpusError('corpus_learning_reviewed_code_changed')
    if manifest['vendor_template'].get('prompt_utf8_sha256') != digest(PROMPT.encode('utf-8')):
        raise CorpusError('corpus_learning_reviewed_template_changed')
    corpus.verify_version(approval['view_id'])
    view = corpus.view_status(approval['view_id'])
    if view['payload_hash'] != approval['view_payload_hash']:
        raise CorpusError('corpus_learning_view_changed')
    payload = corpus.load_version(approval['view_id'])
    by_id = {r['message_id']: r for r in payload['messages']}
    fragments = {r['id']: r for r in payload['fragments']}
    with corpus.connection() as db:
        proof = corpus._source_proof(db, view['parse_id'])
        corpus_id = db.execute('SELECT corpus_id FROM corpus_meta').fetchone()[0]
    permitted = {r.get('position'): r for r in approval['batches'] if isinstance(r, dict)}
    if set(permitted) != {1, 2}:
        raise CorpusError('corpus_learning_positions_invalid')
    positions = []
    for record in manifest['batches']:
        position = record.get('batch')
        if position not in permitted:
            raise CorpusError('corpus_learning_position_unapproved')
        packet, raw = read_document(child(preview_root, record['file']))
        wire, wire_raw = read_document(child(preview_root, record['wire_file']))
        accepted = permitted[position]
        if (digest(raw) != record['sha256'] or digest(raw) != accepted.get('file_sha256')
                or digest(wire_raw) != record['wire_sha256'] or digest(wire_raw) != accepted.get('wire_sha256')
                or packet.get('view_id') != view['id'] or packet.get('split') != 'learn'
                or packet.get('batch') != position):
            raise CorpusError('corpus_learning_reviewed_input_changed')
        fragment = fragments.get(packet.get('fragment_id'))
        maps = packet.get('source_map')
        if not fragment or fragment['split'] != 'learn' or not isinstance(maps, list) or not 2 <= len(maps) <= 10:
            raise CorpusError('corpus_learning_source_scope_invalid')
        if [m.get('message_id') for m in maps] != fragment['message_ids']:
            raise CorpusError('corpus_learning_complete_fragment_required')
        sources = []
        for number, mapping in enumerate(maps, 1):
            row = by_id.get(mapping.get('message_id'))
            if (not row or mapping.get('source_id') != number or row['split'] != 'learn'
                    or not row['target_candidate'] or row['flags'] or row['redactions']
                    or mapping.get('role') != row['role'] or mapping.get('source_text_hash') != row['source_text_hash']
                    or mapping.get('view_text_hash') != row['text_hash']):
                raise CorpusError('corpus_learning_source_evidence_changed')
            sources.append({'source_id': fingerprint([corpus_id,view['id'],row['message_id']]),
                            'revision': 1, 'view_id': view['id'], 'view_message_id': row['message_id'],
                            'kind': 'private_corpus_view', 'body': row['text'], 'content_hash': row['text_hash'],
                            'speaker': 'HUMAN_'+row['role'], 'blacklisted': False})
        if {m['role'] for m in maps} != {'A', 'B'} or packet.get('messages') != learning_messages(sources):
            raise CorpusError('corpus_learning_messages_changed')
        request = wire.get('request')
        if not isinstance(request, dict) or request.get('model') != approval['allowed_model']:
            raise CorpusError('corpus_learning_model_changed')
        public_model = {'base_url':'https://api.deepseek.com','name':request['model'],'timeout':120,
                        'max_tokens':1024,'_json_output':True,
                        '_dialogue_profile':{'version':'natural-dialogue-v8','seed':'CORPUS-PREVIEW-'+str(position),'temperature':.2}}
        require_generation_provider(public_model)
        if request != build_request(public_model, packet['messages'])[0] or len(json.dumps(packet['messages'],ensure_ascii=False)) > 12000:
            raise CorpusError('corpus_learning_wire_changed')
        position_key = fingerprint([corpus_id, view['payload_hash'], packet['fragment_id'], sources, request])
        positions.append({'position':position,'position_key':position_key,'request':request,
                          'request_hash':fingerprint(request),'model':public_model,'sources':sources,
                          'source_proof':proof,'source_proof_hash':fingerprint(proof),
                          'batch_file_hash':digest(raw),'wire_file_hash':digest(wire_raw)})
    if {p['position'] for p in positions} != {1,2}:
        raise CorpusError('corpus_learning_positions_invalid')
    return {'approval':approval,'approval_hash':digest(approval_raw),'approval_path':str(approval_path),
            'preview_root':str(preview_root),'corpus_id':corpus_id,'view':view,'positions':positions}
