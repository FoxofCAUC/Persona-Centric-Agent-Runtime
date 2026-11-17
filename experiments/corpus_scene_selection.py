"""Local, review-only situation qualification over immutable real materials.

No prompt writer, provider credential or generative client is used. Situation
similarity is a candidate screen; author review determines actual suitability.
"""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import socket
import sqlite3
import time

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.expression_candidates import cosine, vector
from .corpus_reply_study import adopted_assets, read_private, save_once
from .corpus_reply_trial import authority_guard, parse_receipt


VERSION = 'local-scene-selection-research-v2'


def quantile_nearest(values, quantile):
    if (not values or not 0 < quantile <= 1 or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in values)):
        raise CorpusError('scene_selection_calibration_invalid')
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * quantile) - 1)]


def select_background(messages, excluded_groups, count, view_hash):
    eligible = [m for m in messages if m['split'] == 'learn' and m['group_id'] not in excluded_groups
                and m['kind'] == 'text' and m['target_candidate'] and not m['flags'] and not m['redactions']
                and 16 <= len(m['text']) <= 400]
    if len(eligible) < count:
        raise CorpusError('scene_selection_background_insufficient')
    return sorted(eligible, key=lambda m: fingerprint(['scene-background-v1', view_hash, m['message_id'], m['text_hash']]))[:count]


def load_materials(study_root, corpus, learning):
    plan = read_private(study_root / 'plan.private.json')
    guard = authority_guard(plan, corpus, learning)
    for position in plan['positions']:
        guard({'position': position['position']})
    approval = read_private(plan['approval_path'])
    prompt_root = Path(approval['prompt_root'])
    asset_acceptance = next(Path(p) for p in plan['source_files'] if Path(p).name == 'EL4-research-asset-acceptance-20261117.json')
    package = next(Path(p) for p in plan['source_files'] if Path(p).name == 'asset-package-candidate.private.json')
    fresh_assets = adopted_assets(corpus, learning, package, asset_acceptance)
    if fresh_assets != plan['assets']:
        raise CorpusError('scene_selection_asset_authority_changed')
    with corpus.connection() as db:
        fragments = {r['id']: json.loads(r['data_json']) for r in db.execute(
            "SELECT id,data_json FROM corpus_view_fragments WHERE view_id=? AND split='learn'", (plan['view_id'],))}
        messages = []
        for row in db.execute("SELECT data_json FROM corpus_view_messages WHERE view_id=? AND split='learn' ORDER BY ordinal", (plan['view_id'],)):
            item = json.loads(row[0])
            item['group_id'] = fragments[item['fragment_id']]['group_id']
            if digest(item['text'].encode()) != item['text_hash']:
                raise CorpusError('scene_selection_learning_text_changed')
            messages.append(item)
    by_id = {m['message_id']: m for m in messages}
    sources, excluded_groups = {}, set()
    for asset in plan['assets']['assets']:
        ident = asset['source_binding']['view_message_id']
        source = by_id.get(ident)
        if not source or source['text'] != asset['source_quote']:
            raise CorpusError('scene_selection_asset_source_unavailable')
        fragment = fragments[source['fragment_id']]
        index = fragment['message_ids'].index(ident)
        prefix_ids = fragment['message_ids'][max(0, index - 2):index + 1]
        rows = [by_id[i] for i in prefix_ids]
        if any(r['flags'] or r['redactions'] or not r['text'].strip() for r in rows):
            raise CorpusError('scene_selection_source_context_unavailable')
        sources[asset['id']] = {'quote': source['text'], 'context': '\n'.join(r['text'] for r in rows),
                                 'message_ids': prefix_ids, 'group_id': source['group_id']}
        excluded_groups.add(source['group_id'])
    understandings = {}
    with closing(sqlite3.connect((study_root / 'ledger/trial.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute("SELECT * FROM trial_calls WHERE kind='U' ORDER BY position"):
            if row['state'] != 'completed' or digest(row['raw_response']) != row['raw_hash']:
                raise CorpusError('scene_selection_understanding_unavailable')
            actual = parse_receipt(row['raw_response'], json.loads(row['request_json']).get('stream', False))
            if not actual['valid'] or actual != json.loads(row['result_json']):
                raise CorpusError('scene_selection_understanding_receipt_changed')
            understandings[row['position']] = {'body': actual['body'], 'response_hash': row['raw_hash']}
    if set(understandings) != set(range(1, 17)):
        raise CorpusError('scene_selection_understanding_count_invalid')
    return plan, sources, messages, excluded_groups, understandings, guard


def encode_texts(provider, texts):
    """Use existing chunk offsets; source strings and model files stay unchanged."""
    texts = list(dict.fromkeys(texts))
    identity = provider.identity()
    dimension = identity['dimension']
    chunks, groups = [], {}
    for text in texts:
        spans = provider.chunks(text)
        previous_end = 0
        members = []
        for span in spans:
            if text[span['start']:span['end']] != span['text']:
                raise CorpusError('scene_selection_embedding_span_changed')
            weight = span['token_end'] - previous_end
            if weight <= 0:
                raise CorpusError('scene_selection_embedding_weight_invalid')
            members.append((len(chunks), weight))
            chunks.append(span['text'])
            previous_end = span['token_end']
        groups[text] = members
    encoded = provider.embed_documents(chunks)
    if len(encoded) != len(chunks):
        raise CorpusError('scene_selection_embedding_count_invalid')
    encoded = [vector(v, dimension) for v in encoded]
    vectors = {}
    for text, members in groups.items():
        combined = [sum(encoded[i][axis] * weight for i, weight in members) / sum(w for _, w in members)
                    for axis in range(dimension)]
        vectors[digest(text.encode())] = vector(combined, dimension)
    return {'identity': identity, 'text_count': len(texts), 'chunk_count': len(chunks), 'vectors': vectors}


def validate_cache(cache, expected_hashes, model_fingerprint, dimension):
    if cache.get('data_hash') != fingerprint({k: v for k, v in cache.items() if k != 'data_hash'}):
        raise CorpusError('scene_selection_vector_cache_changed')
    if (cache['identity']['fingerprint'] != model_fingerprint or cache['identity']['dimension'] != dimension
            or set(cache['vectors']) != set(expected_hashes)):
        raise CorpusError('scene_selection_vector_cache_scope_changed')
    for value in cache['vectors'].values():
        checked = vector(value, dimension)
        if max(abs(a - b) for a, b in zip(checked, value)) > 1e-5:
            raise CorpusError('scene_selection_vector_cache_norm_invalid')


def calibrate(assets, sources, background, vectors, quantile):
    def v(text):
        return vectors[digest(text.encode())]
    calibration = {}
    for asset in assets:
        situation = v(asset['situation'])
        scores = [cosine(situation, v(m['text'])) for m in background]
        cutoff = quantile_nearest(scores, quantile)
        source = sources[asset['id']]
        quote_score = cosine(situation, v(source['quote']))
        context_score = cosine(situation, v(source['context']))
        calibration[asset['id']] = {'background_cutoff': cutoff, 'source_quote_score': quote_score,
                                    'source_context_score': context_score, 'source_supported': max(quote_score, context_score) > cutoff}
    return calibration


def qualify(assets, calibration, raw_scores, understanding_scores, situation_vectors, wrapper, *, maximum=5, character_limit=3000):
    if (type(maximum) is not int or not 0 <= maximum <= 5 or type(character_limit) is not int
            or not 0 <= character_limit <= 3000):
        raise CorpusError('scene_selection_capacity_invalid')
    ids = {a['id'] for a in assets}
    if (len(ids) != len(assets) or any(set(value) != ids for value in
            (calibration, raw_scores, understanding_scores, situation_vectors))):
        raise CorpusError('scene_selection_score_scope_invalid')
    records, eligible = {}, {}
    for item in assets:
        ident = item['id']
        c = calibration[ident]
        raw, understood, cutoff = raw_scores[ident], understanding_scores[ident], c['background_cutoff']
        if any(not math.isfinite(x) for x in (raw, understood, cutoff)):
            raise CorpusError('scene_selection_nonfinite_score')
        reasons = []
        if not c['source_supported']:
            reasons.append('source_support_below_background_gate')
        if raw <= cutoff:
            reasons.append('current_input_below_background_gate')
        if understood <= cutoff:
            reasons.append('persisted_understanding_below_background_gate')
        records[ident] = c | {'id': ident, 'raw_score': raw, 'understanding_score': understood,
                              'weakest_query_margin': min(raw, understood) - cutoff,
                              'eligible': not reasons, 'selected': False, 'reasons': reasons}
        if not reasons:
            eligible[ident] = item
    chosen, lines = [], []
    while eligible and len(chosen) < maximum:
        def score(ident):
            redundancy = max((max(0, cosine(situation_vectors[ident], situation_vectors[old])) for old in chosen), default=0)
            return .7 * min(raw_scores[ident], understanding_scores[ident]) - .3 * redundancy
        ident = min(eligible, key=lambda key: (-score(key), key))
        item = eligible.pop(ident)
        if score(ident) <= 0:
            records[ident]['reasons'].append('no_positive_diversity_utility')
            continue
        line = wrapper['item_format'].format(situation=item['situation'], style=item['style'])
        candidate = wrapper['prefix'] + '\n'.join([*lines, line]) + wrapper['suffix_if_present']
        if len(candidate) > character_limit:
            records[ident]['reasons'].append('rendered_character_capacity')
            continue
        chosen.append(ident)
        lines.append(line)
        records[ident]['selected'] = True
    for ident in eligible:
        records[ident]['reasons'].append('candidate_count_capacity')
    text = wrapper['prefix'] + '\n'.join(lines) + wrapper['suffix_if_present'] if lines else ''
    return {'selected_ids': chosen, 'rendered_text': text, 'characters': len(text), 'audit': list(records.values()),
            'model_use_approved': False, 'semantic_suitability_verified_by_author': False}


def run_local(study_root, root, provider):
    from persona_runtime.corpus_dataset import DatasetCorpusStore
    from persona_runtime.corpus_learning import CorpusLearning
    root, study_root = outside_repository(root), outside_repository(study_root)
    protocol = read_private(root / 'protocol.private.json')
    if (protocol['version'] != VERSION or protocol['background_count'] != 128
            or protocol['background_quantile'] != .95 or protocol['max_selected'] != 5
            or protocol['max_rendered_characters'] != 3000 or protocol['model_use_approved'] is not False
            or protocol['new_driver_llm_calls'] != 0):
        raise CorpusError('scene_selection_protocol_invalid')
    if digest((study_root / 'plan.private.json').read_bytes()) != protocol['source_study_plan_sha256']:
        raise CorpusError('scene_selection_study_changed')
    old = read_private(study_root / 'plan.private.json')
    corpus = DatasetCorpusStore(Path(old['corpus_root']))
    learning = CorpusLearning(Path(old['learning_root']), corpus)
    plan, sources, messages, excluded_groups, understandings, guard = load_materials(study_root, corpus, learning)
    background = select_background(messages, excluded_groups, 128, plan['view_payload_hash'])
    assets = plan['assets']['assets']
    texts = [m['text'] for m in background] + [a['situation'] for a in assets]
    for a in assets:
        texts.extend((sources[a['id']]['quote'], sources[a['id']]['context']))
    for p in plan['positions']:
        texts.extend((p['input_text'], understandings[p['position']]['body']))
    texts = list(dict.fromkeys(texts))
    hashes = {digest(t.encode()) for t in texts}
    cache_path = root / 'local-vectors.private.json'
    started = time.monotonic()
    # Enforce offline inference even if an optional dependency attempts telemetry.
    original_connect = socket.socket.connect
    attempts = []
    def forbidden(*_args, **_kwargs):
        attempts.append(True)
        raise OSError('scene_selection_network_disabled')
    socket.socket.connect = forbidden
    try:
        identity = provider.identity()
        if identity['fingerprint'] != protocol['local_model_fingerprint'] or identity['dimension'] != protocol['local_dimension']:
            raise CorpusError('scene_selection_actual_model_changed')
        implementation_hash = digest(Path(__file__).read_bytes().replace(b'\r\n', b'\n'))
        if cache_path.exists():
            cache = read_private(cache_path)
        else:
            cache = encode_texts(provider, texts)
            cache.update(version=VERSION, protocol_sha256=digest((root / 'protocol.private.json').read_bytes()),
                         implementation_sha256=implementation_hash)
            cache['data_hash'] = fingerprint(cache)
            save_once(cache_path, cache)
        validate_cache(cache, hashes, protocol['local_model_fingerprint'], protocol['local_dimension'])
        if (cache['protocol_sha256'] != digest((root / 'protocol.private.json').read_bytes())
                or cache['implementation_sha256'] != implementation_hash or cache['identity'] != identity):
            raise CorpusError('scene_selection_cached_protocol_changed')
    finally:
        socket.socket.connect = original_connect
    if attempts:
        raise CorpusError('scene_selection_unexpected_network_attempt')
    vectors = cache['vectors']
    v = lambda text: vectors[digest(text.encode())]
    calibration = calibrate(assets, sources, background, vectors, .95)
    situation_vectors = {a['id']: v(a['situation']) for a in assets}
    wrapper = plan['binding']['variables']['expression_habits_block']
    cases = []
    for p in plan['positions']:
        understanding = understandings[p['position']]
        raw_scores = {a['id']: cosine(v(p['input_text']), situation_vectors[a['id']]) for a in assets}
        understanding_scores = {a['id']: cosine(v(understanding['body']), situation_vectors[a['id']]) for a in assets}
        selected = qualify(assets, calibration, raw_scores, understanding_scores, situation_vectors, wrapper)
        cases.append({'position': p['position'], 'input_text': p['input_text'], 'input_hash': p['input_text_hash'],
                      'actual_understanding': understanding, 'old_selected_ids': p['selection']['selected_ids'],
                      'new_selection': selected})
    # Roundtrip probes use original learning texts, not invented questions or
    # freshly generated understandings. They are not independent validation.
    probes = []
    for asset in assets:
        source = sources[asset['id']]
        raw_scores = {a['id']: cosine(v(source['quote']), situation_vectors[a['id']]) for a in assets}
        context_scores = {a['id']: cosine(v(source['context']), situation_vectors[a['id']]) for a in assets}
        selected = qualify(assets, calibration, raw_scores, context_scores, situation_vectors, wrapper)
        probes.append({'asset_id': asset['id'], 'source_quote': source['quote'], 'source_context': source['context'],
                       'selected_ids': selected['selected_ids'], 'origin_asset_returned': asset['id'] in selected['selected_ids'],
                       'kind': 'local_source_roundtrip_not_generated_dialogue'})
    guard({'position': 1})
    result = {'version': VERSION, 'status': 'candidate_pending_author_selection_review',
              'protocol': protocol, 'model_identity': cache['identity'], 'assets': assets, 'source_evidence': sources,
              'background': [{'message_id': m['message_id'], 'text_hash': m['text_hash'], 'group_id': m['group_id']} for m in background],
              'calibration': calibration, 'cases': cases, 'source_roundtrip_probes': probes,
              'new_driver_llm_calls': 0, 'paid_selector_calls': 0, 'network_attempts': 0,
              'embedded_unique_texts': cache['text_count'], 'embedded_chunks': cache['chunk_count'],
              'model_use_approved': False, 'formal_activation': False,
              'implementation_sha256': digest(Path(__file__).read_bytes().replace(b'\r\n', b'\n'))}
    save_once(root / 'candidate.private.json', result)
    summary = {'old_nonempty_positions': sum(bool(c['old_selected_ids']) for c in cases),
               'new_nonempty_positions': sum(bool(c['new_selection']['selected_ids']) for c in cases),
               'old_selected_total': sum(len(c['old_selected_ids']) for c in cases),
               'new_selected_total': sum(len(c['new_selection']['selected_ids']) for c in cases),
               'source_roundtrip_returned': sum(p['origin_asset_returned'] for p in probes), 'source_roundtrip_total': len(probes),
               'source_supported_assets': sum(c['source_supported'] for c in calibration.values()),
               'local_elapsed_ms': round((time.monotonic() - started) * 1000, 3), 'new_driver_llm_calls': 0,
               'model_use_approved': False, 'candidate_path': str(root / 'candidate.private.json')}
    summary_path = root / 'summary.private.json'
    if summary_path.exists():
        old_summary = read_private(summary_path)
        if {k: v for k, v in old_summary.items() if k != 'local_elapsed_ms'} != {k: v for k, v in summary.items() if k != 'local_elapsed_ms'}:
            raise CorpusError('scene_selection_summary_changed')
        return old_summary
    save_once(summary_path, summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model-home', type=Path, required=True)
    parser.add_argument('--model-directory', type=Path, required=True)
    args = parser.parse_args(argv)
    from persona_runtime.character_embedding import CharacterEmbeddingProvider
    from persona_runtime.paths import RuntimePaths
    provider = CharacterEmbeddingProvider(RuntimePaths(args.model_home.resolve()), args.model_directory.resolve())
    result = run_local(args.study_root, args.output, provider)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
