"""Review-only incoming-cue selection; reply-act similarity is diagnostic only.

Preceding messages establish visible context, not a causal reply annotation.
All language is copied from existing sources; no generative request is made.
"""
import argparse
import json
import math
from pathlib import Path
import socket
import time

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.expression_candidates import cosine
from .corpus_reply_study import read_private, save_once
from .corpus_scene_reply import source_response_contexts
from .corpus_scene_selection import (encode_texts, load_materials, quantile_nearest,
                                    select_background, validate_cache)


VERSION = 'local-response-cue-research-v4'


def response_calibration(assets, cues, background, vectors, quantile):
    v = lambda t: vectors[digest(t.encode())]
    result = {}
    for asset in assets:
        cue = cues[asset['id']]
        if not cue['available']:
            result[asset['id']] = {'source_context_available': False, 'source_reason': cue['reason']}
            continue
        situation = v(asset['situation'])
        peer = v(cue['peer_text'])
        context = v(cue['preceding_context'])
        situation_cutoff = quantile_nearest([cosine(situation, v(m['text'])) for m in background], quantile)
        peer_cutoff = quantile_nearest([cosine(peer, v(m['text'])) for m in background], quantile)
        context_cutoff = quantile_nearest([cosine(context, v(m['text'])) for m in background], quantile)
        grounding = cosine(situation, context)
        result[asset['id']] = {'source_context_available': True, 'source_situation_cutoff': situation_cutoff,
            'source_situation_context_score': grounding, 'source_situation_diagnostic_above_background': grounding > situation_cutoff,
            'current_input_cutoff': peer_cutoff, 'understanding_cutoff': peer_cutoff,
            'self_continuation': cue['self_continuation'], 'relation_author_verified': False}
    return result


def select_responses(assets, calibration, scores, situation_vectors, wrapper, *, maximum=5, character_limit=3000):
    ids = {a['id'] for a in assets}
    if (len(ids) != len(assets) or set(calibration) != ids or set(scores) != ids or set(situation_vectors) != ids
            or type(maximum) is not int or not 0 <= maximum <= 5
            or type(character_limit) is not int or not 0 <= character_limit <= 3000):
        raise CorpusError('response_selection_scope_invalid')
    audit, pool = {}, {}
    for asset in assets:
        ident = asset['id']
        c = calibration[ident]
        reasons = []
        if not c['source_context_available']:
            reasons.append(c['source_reason'])
            record = c | {'id': ident, 'eligible': False, 'selected': False, 'reasons': reasons}
        else:
            q = scores[ident]
            if any(not math.isfinite(v) for v in (q['raw_to_peer'], q['understanding_to_peer'],
                    c['current_input_cutoff'], c['understanding_cutoff'], c['source_situation_context_score'])):
                raise CorpusError('response_selection_nonfinite_score')
            if q['raw_to_peer'] <= c['current_input_cutoff']:
                reasons.append('incoming_text_not_above_source_peer_background')
            if q['understanding_to_peer'] <= c['understanding_cutoff']:
                reasons.append('actual_understanding_not_above_source_context_background')
            record = c | q | {'id': ident, 'eligible': not reasons, 'selected': False, 'reasons': reasons}
            if not reasons:
                pool[ident] = asset
        audit[ident] = record
    chosen, lines = [], []
    while pool and len(chosen) < maximum:
        def score(ident):
            q = scores[ident]
            novelty = max((max(0., cosine(situation_vectors[ident], situation_vectors[old])) for old in chosen), default=0.)
            return .7 * min(q['raw_to_peer'], q['understanding_to_peer']) - .3 * novelty
        ident = min(pool, key=lambda i: (-score(i), i))
        asset = pool.pop(ident)
        if score(ident) <= 0:
            audit[ident]['reasons'].append('no_positive_diversity_utility')
            continue
        line = wrapper['item_format'].format(situation=asset['situation'], style=asset['style'])
        block = wrapper['prefix'] + '\n'.join([*lines, line]) + wrapper['suffix_if_present']
        if len(block) > character_limit:
            audit[ident]['reasons'].append('rendered_character_capacity')
            continue
        chosen.append(ident)
        lines.append(line)
        audit[ident]['selected'] = True
    for ident in pool:
        audit[ident]['reasons'].append('candidate_count_capacity')
    text = wrapper['prefix'] + '\n'.join(lines) + wrapper['suffix_if_present'] if lines else ''
    return {'selected_ids': chosen, 'rendered_text': text, 'characters': len(text), 'audit': list(audit.values()),
            'model_use_approved': False, 'semantic_suitability_verified_by_author': False}


def run_local(study_root, root, provider):
    from persona_runtime.corpus_dataset import DatasetCorpusStore
    from persona_runtime.corpus_learning import CorpusLearning
    root, study_root = outside_repository(root), outside_repository(study_root)
    protocol = read_private(root / 'protocol.private.json')
    prior_root = study_root / 'selection-repair-v2'
    if (protocol['version'] != VERSION or protocol['background_quantile'] != .95
            or protocol['background_count'] != 128 or protocol['model_use_approved'] is not False
            or digest((prior_root / 'candidate.private.json').read_bytes()) != protocol['prior_candidate_sha256']):
        raise CorpusError('response_selection_protocol_changed')
    if digest((study_root / 'plan.private.json').read_bytes()) != protocol['source_study_plan_sha256']:
        raise CorpusError('response_selection_study_changed')
    old = read_private(study_root / 'plan.private.json')
    corpus = DatasetCorpusStore(Path(old['corpus_root']))
    learning = CorpusLearning(Path(old['learning_root']), corpus)
    plan, sources, messages, excluded, understandings, guard = load_materials(study_root, corpus, learning)
    assets = plan['assets']['assets']
    cues = source_response_contexts(assets, messages)
    background = select_background(messages, excluded, 128, plan['view_payload_hash'])
    previous = read_private(prior_root / 'candidate.private.json')
    if [m['message_id'] for m in background] != [m['message_id'] for m in previous['background']]:
        raise CorpusError('response_selection_background_changed')
    texts = [m['text'] for m in background] + [a['situation'] for a in assets]
    for cue in cues.values():
        if cue['available']:
            texts.extend((cue['peer_text'], cue['preceding_context']))
    for p in plan['positions']:
        texts.extend((p['input_text'], understandings[p['position']]['body']))
    texts = list(dict.fromkeys(texts))
    hashes = {digest(t.encode()) for t in texts}
    cache_path = root / 'local-vectors.private.json'
    started = time.monotonic()
    original_connect = socket.socket.connect
    attempts = []
    def forbidden(*_args, **_kwargs):
        attempts.append(True)
        raise OSError('response_selection_network_disabled')
    socket.socket.connect = forbidden
    try:
        identity = provider.identity()
        if identity['fingerprint'] != protocol['local_model_fingerprint'] or identity['dimension'] != protocol['local_dimension']:
            raise CorpusError('response_selection_actual_model_changed')
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
            raise CorpusError('response_selection_cached_protocol_changed')
    finally:
        socket.socket.connect = original_connect
    if attempts:
        raise CorpusError('response_selection_unexpected_network_attempt')
    vectors = cache['vectors']
    v = lambda t: vectors[digest(t.encode())]
    calibration = response_calibration(assets, cues, background, vectors, .95)
    situation_vectors = {a['id']: v(a['situation']) for a in assets}
    def evaluate(raw, understanding):
        scores = {a['id']: ({'raw_to_peer': cosine(v(raw), v(cues[a['id']]['peer_text'])),
                             'understanding_to_peer': cosine(v(understanding), v(cues[a['id']]['peer_text']))}
                            if cues[a['id']]['available'] else {}) for a in assets}
        return select_responses(assets, calibration, scores, situation_vectors, plan['binding']['variables']['expression_habits_block'])
    cases = []
    for p in plan['positions']:
        understanding = understandings[p['position']]
        chosen = evaluate(p['input_text'], understanding['body'])
        cases.append({'position': p['position'], 'input_text': p['input_text'], 'input_hash': p['input_text_hash'],
                      'actual_understanding': understanding, 'old_selected_ids': p['selection']['selected_ids'],
                      'first_semantic_selected_ids': previous['cases'][p['position'] - 1]['first_semantic_selected_ids'],
                      'new_selection': chosen})
    probes = []
    for a in assets:
        cue = cues[a['id']]
        if cue['available']:
            result = evaluate(cue['peer_text'], cue['peer_text'])
            probes.append({'asset_id': a['id'], 'source_quote': cue['quote'], 'source_context': cue['preceding_context'],
                           'source_peer': cue['peer_text'], 'selected_ids': result['selected_ids'],
                           'origin_asset_returned': a['id'] in result['selected_ids'],
                           'kind': 'single_source_peer_index_roundtrip_not_new_understanding'})
    guard({'position': 1})
    result = {'version': VERSION, 'status': 'candidate_pending_author_selection_review', 'protocol': protocol,
              'model_identity': cache['identity'], 'assets': assets, 'source_evidence': cues, 'calibration': calibration,
              'background': [{'message_id': m['message_id'], 'text_hash': m['text_hash'], 'group_id': m['group_id']} for m in background],
              'cases': cases, 'source_roundtrip_probes': probes, 'new_driver_llm_calls': 0, 'paid_selector_calls': 0,
              'network_attempts': 0, 'embedded_unique_texts': cache['text_count'], 'embedded_chunks': cache['chunk_count'],
              'model_use_approved': False, 'formal_activation': False, 'implementation_sha256': implementation_hash}
    save_once(root / 'candidate.private.json', result)
    summary = {'old_nonempty_positions': sum(bool(c['old_selected_ids']) for c in cases),
               'first_semantic_nonempty_positions': sum(bool(c['first_semantic_selected_ids']) for c in cases),
               'new_nonempty_positions': sum(bool(c['new_selection']['selected_ids']) for c in cases),
               'old_selected_total': sum(len(c['old_selected_ids']) for c in cases),
               'new_selected_total': sum(len(c['new_selection']['selected_ids']) for c in cases),
               'assets_with_visible_preceding_peer': sum(c['available'] for c in cues.values()),
               'assets_without_visible_preceding_peer': sum(not c['available'] for c in cues.values()),
               'source_contexts_pending_relation_review': sum(c['available'] for c in cues.values()),
               'source_roundtrip_returned': sum(p['origin_asset_returned'] for p in probes), 'source_roundtrip_total': len(probes),
               'local_elapsed_ms': round((time.monotonic() - started) * 1000, 3), 'new_driver_llm_calls': 0,
               'model_use_approved': False, 'candidate_path': str(root / 'candidate.private.json')}
    if (root / 'summary.private.json').exists():
        old_summary = read_private(root / 'summary.private.json')
        if {k: v for k, v in old_summary.items() if k != 'local_elapsed_ms'} != {k: v for k, v in summary.items() if k != 'local_elapsed_ms'}:
            raise CorpusError('response_selection_summary_changed')
        return old_summary
    save_once(root / 'summary.private.json', summary)
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
    print(json.dumps(run_local(args.study_root, args.output, provider), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
