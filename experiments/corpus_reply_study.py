"""Local E.L4 preparation: approved research assets and real development inputs.

No provider client, credential access or prompt prose lives in this module.
Research acceptance is separate from publication and from model-input approval.
"""
from collections import Counter
import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.expression_candidates import matching_terms, rendered


VERSION = 'corpus-reply-study-preparation-v1'
ROLES = ('qinghe', 'xiazhi', 'ningzhou', 'linchuan')


def read_private(path):
    path = outside_repository(path)
    return json.loads(path.read_text(encoding='utf-8'))


def save_once(path, value):
    path = outside_repository(path)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as target:
            target.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise CorpusError('reply_study_frozen_artifact_conflict') from None
    return digest(payload)


def adopted_assets(corpus, learning, package_path, approval_path):
    """Recheck the reviewed package against original proposals and live grants."""
    package, approval = read_private(package_path), read_private(approval_path)
    contents = {k: v for k, v in package.items() if k != 'package_hash'}
    if (package.get('version') != 'corpus-expression-research-package-v1'
            or package.get('package_hash') != fingerprint(contents)
            or approval.get('version') != 'corpus-research-asset-acceptance-v1'
            or approval.get('package_sha256') != digest(Path(package_path).read_bytes())
            or approval.get('package_hash') != package['package_hash']
            or approval.get('purpose') != 'el4_fixed_context_research'
            or approval.get('formal_publication_authorized') is not False
            or approval.get('new_prompt_writing_authorized') is not False):
        raise CorpusError('reply_study_asset_acceptance_invalid')
    proposals = package['proposals']
    ids = [p['proposal_id'] for p in proposals]
    if (not ids or len(set(ids)) != len(ids)
            or approval.get('accepted_proposal_ids') != ids):
        raise CorpusError('reply_study_accepted_items_changed')
    checked_approvals, assets = set(), []
    for item in proposals:
        with learning.connection() as db:
            row = db.execute('SELECT * FROM learning_proposals WHERE id=?', (item['proposal_id'],)).fetchone()
            job = db.execute('SELECT * FROM learning_jobs WHERE id=?', (item['job_id'],)).fetchone()
        if (not row or not job or row['job_id'] != job['id'] or job['state'] != 'completed'
                or row['data_hash'] != item['data_hash']
                or fingerprint(item['content']) != row['data_hash']
                or json.loads(row['data_json']) != item['content']
                or job['raw_response'] is None or digest(job['raw_response']) != job['raw_response_hash']):
            raise CorpusError('reply_study_proposal_receipt_changed')
        data = item['content']
        if (data['approval_id'] != job['approval_id'] or data['request_hash'] != job['request_hash']
                or data['response_hash'] != job['raw_response_hash']):
            raise CorpusError('reply_study_proposal_provenance_changed')
        if job['approval_id'] not in checked_approvals:
            learning._guard(job['approval_id'])
            checked_approvals.add(job['approval_id'])
        binding = data['source_binding']
        view = corpus.view_status(binding['view_id'])
        if view['payload_hash'] != package['view_payload_hash']:
            raise CorpusError('reply_study_asset_view_changed')
        with corpus.connection() as db:
            source = db.execute('SELECT split,data_json FROM corpus_view_messages WHERE view_id=? AND message_id=?',
                                (binding['view_id'], binding['view_message_id'])).fetchone()
        message = json.loads(source['data_json']) if source else None
        if (not message or source['split'] != 'learn' or message['split'] != 'learn'
                or message['text'] != data['source_quote']
                or digest(message['text'].encode('utf-8')) != binding['content_hash']):
            raise CorpusError('reply_study_asset_source_changed')
        assets.append({'id': item['proposal_id'], 'situation': data['situation'], 'style': data['style'],
                       'proposal_hash': row['data_hash'], 'job_id': job['id'],
                       'source_binding': binding, 'source_quote': data['source_quote']})
    snapshot = {'version': VERSION, 'purpose': 'el4_fixed_context_research',
                'package_hash': package['package_hash'], 'approval_sha256': digest(Path(approval_path).read_bytes()),
                'view_payload_hash': package['view_payload_hash'],
                'assets': assets, 'formal_publication': False, 'generation_input_approved': False}
    return snapshot | {'snapshot_hash': fingerprint(snapshot)}


def select_assets(snapshot, query, *, maximum=5, character_limit=3000):
    """Use the existing lexical/MMR recipe; source quotes never enter projection."""
    if (not isinstance(query, str) or len(query) > 12000 or type(maximum) is not int
            or not 0 <= maximum <= 5 or type(character_limit) is not int
            or not 0 <= character_limit <= 3000):
        raise CorpusError('reply_study_selection_capacity_invalid')
    if snapshot.get('snapshot_hash') != fingerprint({k: v for k, v in snapshot.items() if k != 'snapshot_hash'}):
        raise CorpusError('reply_study_asset_snapshot_changed')
    query_terms = matching_terms(query)
    tokens = {a['id']: matching_terms(a['situation'] + ' ' + a['style']) for a in snapshot['assets']}
    lexical = {key: len(query_terms & terms) / max(1, len(query_terms)) for key, terms in tokens.items()}
    remaining = {a['id']: a for a in snapshot['assets'] if lexical[a['id']] > 0}
    chosen, text = [], ''
    while remaining and len(chosen) < maximum:
        def score(key):
            novelty = max((len(tokens[key] & tokens[a['id']]) / max(1, len(tokens[key] | tokens[a['id']]))
                           for a in chosen), default=0)
            return .7 * (.7 * lexical[key]) - .3 * novelty
        key = min(remaining, key=lambda key: (-score(key), key))
        item = remaining.pop(key)
        projected = {'situation': item['situation'], 'style': item['style']}
        candidate_text = '\n\n'.join(filter(None, (text, rendered(projected))))
        if len(candidate_text) > character_limit:
            continue
        chosen.append(item)
        text = candidate_text
    result = {'version': VERSION, 'asset_snapshot_hash': snapshot['snapshot_hash'],
              'query_hash': fingerprint(query), 'selected_ids': [a['id'] for a in chosen],
              'text': text, 'characters': len(text), 'selector': 'existing_e2_lexical_mmr_without_embedding',
              'paid_selection_requests': 0, 'model_use_approved': False}
    return result | {'selection_hash': fingerprint(result)}


def _bin(text):
    return min(3, (len(text) >= 40) + (len(text) >= 80) + (len(text) >= 160))


def development_candidates(corpus, view_id):
    """Query only development rows; final holdout bodies are never loaded."""
    view = corpus.view_status(view_id)
    with corpus.connection() as db:
        fragments = {r['id']: json.loads(r['data_json']) for r in db.execute(
            "SELECT id,data_json FROM corpus_view_fragments WHERE view_id=? AND split='dev_review'", (view_id,))}
        messages = [json.loads(r[0]) for r in db.execute(
            "SELECT data_json FROM corpus_view_messages WHERE view_id=? AND split='dev_review' ORDER BY ordinal", (view_id,))]
    candidates = []
    for message in messages:
        if (message['kind'] != 'text' or not message['target_candidate'] or message['flags']
                or message['redactions'] or not 16 <= len(message['text']) <= 400):
            continue
        if message['split'] != 'dev_review' or message['text_hash'] != digest(message['text'].encode('utf-8')):
            raise CorpusError('reply_study_development_source_changed')
        fragment = fragments[message['fragment_id']]
        candidates.append({'message': message, 'group_id': fragment['group_id'],
                           'month': datetime.fromtimestamp(fragment['first_epoch'], timezone(timedelta(hours=8))).strftime('%Y-%m'),
                           'length_bin': _bin(message['text'])})
    return view, candidates, messages


def choose_positions(candidates):
    """Freeze 16 different source groups using structure only, before any model."""
    remaining = sorted(candidates, key=lambda c: (c['message']['ordinal'], c['message']['message_id']))
    chosen, groups = [], set()
    months, lengths, speakers = Counter(), Counter(), Counter()
    while len(chosen) < 16:
        pool = [c for c in remaining if c['group_id'] not in groups]
        if not pool:
            raise CorpusError('reply_study_sixteen_distinct_development_groups_unavailable')
        selected = min(pool, key=lambda c: (months[c['month']], lengths[c['length_bin']],
                       speakers[c['message']['role']], c['message']['ordinal'], c['message']['message_id']))
        chosen.append(selected)
        groups.add(selected['group_id'])
        months[selected['month']] += 1
        lengths[selected['length_bin']] += 1
        speakers[selected['message']['role']] += 1
    return chosen


def freeze_positions(corpus, view_id, snapshot):
    view, candidates, messages = development_candidates(corpus, view_id)
    if view['payload_hash'] != read_view_hash(snapshot):
        raise CorpusError('reply_study_position_view_changed')
    chosen = choose_positions(candidates)
    positions = []
    for index, item in enumerate(chosen):
        message = item['message']
        # Original two-human context is review evidence, not persona history.
        preceding = [m for m in messages if m['fragment_id'] == message['fragment_id']
                     and m['ordinal'] < message['ordinal']][-4:]
        positions.append({'position': index + 1, 'role': ROLES[index % 4],
                          'view_id': view_id, 'view_payload_hash': view['payload_hash'],
                          'group_id': item['group_id'], 'month': item['month'], 'length_bin': item['length_bin'],
                          'message_id': message['message_id'], 'input_text': message['text'],
                          'input_text_hash': message['text_hash'], 'speaker': message['role'],
                          'persona_history': [], 'review_only_original_human_context': preceding,
                          'selection': select_assets(snapshot, message['text']),
                          'standalone_input_author_review': 'pending', 'model_use_approved': False})
    result = {'version': VERSION, 'positions': positions, 'candidate_pool': len(candidates),
              'asset_snapshot_hash': snapshot['snapshot_hash'], 'view_payload_hash': view['payload_hash'],
              'maximum_later_client_requests': 80, 'client_requests_this_preparation': 0,
              'context_rule': 'one unchanged real message; original human context is review-only',
              'generation_input_approved': False}
    return result | {'positions_hash': fingerprint(result)}


def verify_preparation(corpus, learning, package_path, approval_path, review_root, view_id):
    """Recheck saved assets and source/selection contracts; never authorizes calls."""
    root = outside_repository(review_root)
    snapshot = adopted_assets(corpus, learning, package_path, approval_path)
    positions = freeze_positions(corpus, view_id, snapshot)
    saved_materials = read_private(root / 'source-materials.private.json')
    materials = source_materials(Path(__file__).resolve().parents[1], Path(saved_materials['upstream_root']),
                                 Path(saved_materials['excerpt_path']))
    if (read_private(root / 'assets.private.json') != snapshot
            or read_private(root / 'positions.private.json') != positions or saved_materials != materials):
        raise CorpusError('reply_study_preparation_changed')
    return {'assets': len(snapshot['assets']), 'positions': len(positions['positions']),
            'source_authority_valid': True, 'model_input_approved': False,
            'model_calls': 0, 'formal_publication': False}


def read_view_hash(snapshot):
    """The adoption caller binds the immutable view outside model-visible text."""
    value = snapshot.get('view_payload_hash')
    if not isinstance(value, str) or len(value) != 64:
        raise CorpusError('reply_study_view_binding_missing')
    return value


def source_materials(repo, upstream_root, excerpt_path):
    """Copy verified source text for review; do not compose a generation prompt."""
    repo, upstream_root = Path(repo).resolve(), Path(upstream_root).resolve()
    evidence = read_private(excerpt_path)
    if (evidence.get('version') != 'el4-upstream-excerpt-readonly-v1'
            or evidence.get('repository') != 'Mai-with-u/MaiBot'
            or evidence.get('commit') != '478f671e64467121fd3e39205c8d3507b5ce0bd6'):
        raise CorpusError('reply_study_upstream_source_invalid')
    expected = {'src/maisaka/builtin_tool/reply.py', 'prompts/zh-CN/main/reply_private.prompt'}
    if {e['path'] for e in evidence['excerpts']} != expected or len(evidence['excerpts']) != 2:
        raise CorpusError('reply_study_upstream_excerpt_set_changed')
    items = []
    for excerpt in evidence['excerpts']:
        source = upstream_root / excerpt['path']
        text = source.read_text(encoding='utf-8')
        if not excerpt['text'] or excerpt['text'] not in text:
            raise CorpusError('reply_study_upstream_excerpt_changed')
        if source.suffix == '.prompt' and excerpt['text'] != text:
            raise CorpusError('reply_study_upstream_template_changed')
        items.append({'label': excerpt['kind'], 'source': 'https://github.com/Mai-with-u/MaiBot/blob/'
                      + evidence['commit'] + '/' + excerpt['path'], 'text': excerpt['text'],
                      'source_sha256': digest(source.read_bytes()), 'excerpt_utf8_sha256': digest(excerpt['text'].encode()),
                      'status': 'verbatim_source_reference_pending_new_use_review'})
    path = repo / 'experiments/expression_e3_review/fixtures.json'
    roles = json.loads(path.read_text(encoding='utf-8'))['personas']
    if set(roles) != set(ROLES):
        raise CorpusError('reply_study_original_core_set_changed')
    for key in ROLES:
        items.append({'label': roles[key]['name'] + ' · 原E3角色核心',
                      'source': path.relative_to(repo).as_posix() + '#' + key,
                      'text': json.dumps(roles[key], ensure_ascii=False, indent=2),
                      'source_sha256': digest(path.read_bytes()), 'origin': 'historical_public_synthetic_core',
                      'status': 'original_core_reference_pending_new_use_review'})
    result = {'version': VERSION, 'items': items,
              'upstream_root': str(upstream_root), 'excerpt_path': str(Path(excerpt_path).resolve()),
              'excerpt_sha256': digest(Path(excerpt_path).read_bytes()),
              'new_prompt_authored': False, 'assembled_model_requests': 0,
              'generation_input_approved': False,
              'missing_bindings': ['standalone_flash_request', 'pro_persona_system', 'pro_variable_binding',
                                   'v8_original_request_capture', 'shared_live_understanding_receipt']}
    return result | {'materials_hash': fingerprint(result)}


def export_review(root, snapshot, positions, materials):
    root = outside_repository(root)
    save_once(root / 'assets.private.json', snapshot)
    save_once(root / 'positions.private.json', positions)
    save_once(root / 'source-materials.private.json', materials)
    body = '<h1>E.L4：真实表达资产与新回复输入准备</h1>'
    body += '<p>' + str(len(snapshot['assets'])) + '条提案已通过研究审阅；本页的新回复输入和上游模板尚未批准使用。本轮模型调用为0。</p>'
    body += '<p>对照预留：A原v8 Flash，B原v8 Pro，C共享Flash理解＋Pro无库，D同一理解＋Pro有库。上限80次，尚未派发。</p>'
    body += '<p>输入候选来自开发分区，每份只拟使用一条真人原话，人物历史为空。原双人前文只供检查能否独立使用，不冒充人物说过的话。若依赖缺失前文，请拒绝该位置。</p>'
    body += '<nav><a href="#sources">上游原文和原角色核心</a> · <a href="#positions">16份真实输入候选</a> · <a href="#assets">33条已认可资产</a></nav>'
    body += '<h2 id="sources">上游原文和原角色核心</h2><p>这里逐字列出来源，不补写人物口吻。模板中的变量尚未绑定为可发送请求；完整装配需另行审阅。</p>'
    for item in materials['items']:
        body += '<details><summary>' + escape(item['label']) + '</summary><p>' + escape(item['source']) + '</p><pre>' + escape(item['text']) + '</pre></details>'
    body += '<h2 id="positions">16份真实输入候选</h2>'
    role_names = {item['source'].split('#')[-1]: item['label'].split(' · ')[0]
                  for item in materials['items'] if '#' in item['source']}
    for item in positions['positions']:
        body += '<article><h3>位置' + str(item['position']) + ' · ' + escape(role_names.get(item['role'], item['role'])) + ' · ' + escape(item['month']) + '</h3><pre>' + escape(item['input_text']) + '</pre>'
        body += '<details><summary>原始双方前文（不发送）</summary>'
        for previous in item['review_only_original_human_context']:
            body += '<p>' + escape(previous['role'] + ' · ' + previous['kind']) + '</p><pre>' + escape(previous['text']) + '</pre>'
        body += '</details><details><summary>本地初选表达 · ' + str(len(item['selection']['selected_ids'])) + '条</summary><pre>' + escape(item['selection']['text']) + '</pre></details></article>'
    body += '<h2 id="assets">' + str(len(snapshot['assets'])) + '条已认可资产</h2><p>情境／表达可参与后续受审装配；原话只作为来源证据，不自动充当人物例句。当前未发布到正式人物。</p>'
    for item in snapshot['assets']:
        body += '<details><summary>' + escape(item['situation']) + '</summary><pre>' + escape(item['style']) + '</pre><p>真人来源原句</p><pre>' + escape(item['source_quote']) + '</pre></details>'
    css = 'body{margin:0;background:#f4f6f2;color:#233a32;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;overflow-wrap:anywhere}main{max-width:1000px;margin:auto;padding:32px 22px 90px}h1{font-size:27px;line-height:1.45}h2{margin-top:36px}nav{display:flex;gap:16px;flex-wrap:wrap}a{color:#25664c}article,details{background:white;border:1px solid #d9e2d7;border-radius:8px;padding:14px 18px;margin:14px 0}article details{background:#f8faf6}summary{cursor:pointer}pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}p{margin:12px 0}@media(max-width:650px){main{padding:20px 13px 70px}h1{font-size:23px}article,details{padding:12px}}'
    html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>E.L4研究准备审阅</title><style>' + css + '</style><main>' + body + '</main></html>'
    target = root / 'index.html'
    if target.exists() and target.read_text(encoding='utf-8') != html:
        raise CorpusError('reply_study_review_conflict')
    if not target.exists():
        target.write_text(html, encoding='utf-8')
    return {'positions': len(positions['positions']), 'assets': len(snapshot['assets']),
            'empty_selections': sum(not p['selection']['selected_ids'] for p in positions['positions']),
            'model_calls': 0, 'model_input_approved': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'verify'))
    for name in ('corpus', 'learning', 'package', 'acceptance', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--view', required=True)
    parser.add_argument('--upstream', type=Path)
    parser.add_argument('--upstream-excerpts', type=Path)
    args = parser.parse_args(argv)
    from persona_runtime.corpus_dataset import DatasetCorpusStore
    from persona_runtime.corpus_learning import CorpusLearning
    class NoTransport:
        async def complete(self, *_args, **_kwargs):
            raise CorpusError('reply_study_preparation_has_no_transport')
    corpus = DatasetCorpusStore(args.corpus)
    learning = CorpusLearning(args.learning, corpus, NoTransport())
    if args.action == 'verify':
        result = verify_preparation(corpus, learning, args.package, args.acceptance, args.output, args.view)
    else:
        if not args.upstream or not args.upstream_excerpts:
            parser.error('upstream_and_pinned_excerpts_required')
        snapshot = adopted_assets(corpus, learning, args.package, args.acceptance)
        positions = freeze_positions(corpus, args.view, snapshot)
        materials = source_materials(Path(__file__).resolve().parents[1], args.upstream, args.upstream_excerpts)
        result = export_review(args.output, snapshot, positions, materials)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
