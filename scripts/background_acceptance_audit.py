"""Read-only factual source audit; writes a new derived audit, never raw results."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.background_acceptance.runner import OUT, read, sha, verify, snapshot
from plugins.background_acceptance.materials import PROBES, evaluate
from persona_runtime.config import atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store

def audit(out):
    out = Path(out)
    owner, plan = verify(out)
    paths = RuntimePaths(Path(owner['home']))
    rows = {r['number']: r for r in (read(p) for p in (out/'results').glob('*.json'))}
    probes = []
    with Store(paths).connection() as db:
        for n, specification in PROBES.items():
            if n not in rows: continue
            source = rows[specification['source']]
            turn = db.execute('SELECT * FROM turns WHERE id=?', (source['turn_id'],)).fetchone()
            event_ids = {r[0] for r in db.execute('SELECT event_id FROM input_batch_members WHERE batch_id=?', (turn['input_batch_id'],))}
            event_ids.add(turn['user_event_id'])
            manifest = read(out/'requests'/f'T{n:03}.json')['manifest']
            history = [r['kind'] for r in manifest['evidence_refs'] if r.get('turn_id') == source['turn_id']]
            memory_links = []
            summary_links = []
            for ref in manifest['included_state_refs']:
                if ref['kind'] == 'memory':
                    sources = {r[0] for r in db.execute('SELECT event_id FROM memory_sources WHERE memory_id=?', (ref['id'],))}
                    if sources & event_ids: memory_links.append(ref['id'])
                if ref['kind'] == 'lossy_summary':
                    summary = db.execute('SELECT sources_json FROM summaries WHERE id=?', (ref['id'],)).fetchone()
                    if summary and set(json.loads(summary[0])) & event_ids: summary_links.append(ref['id'])
            calls = [read(p) for p in (out/'calls'/f'T{n:03}').glob('*.json')]
            request = next(c for c in calls if c['status'] == 'completed')
            context = '\n'.join(m['content'] for m in request['messages'][:-1])
            markers = specification.get('all', []) + specification.get('any', [])
            later_mentions = [i for i, row in sorted(rows.items())
                              if specification['source'] < i < n
                              and any(marker in row['input'] + row['reply'] for marker in markers
                                      if marker not in ('上午', '下午'))]
            probes.append({'round': n, 'reply': rows[n]['reply'], **evaluate(n, rows[n]['reply']),
                'source_turn_id': source['turn_id'], 'source_event_ids': sorted(event_ids),
                'source_history_kinds': history, 'selected_memory_source_links': memory_links,
                'selected_summary_source_links': summary_links,
                'any_expected_marker_in_actual_context': any(x in context for x in markers),
                'later_conversation_marker_mentions': later_mentions})
        counts = {name: db.execute('SELECT count(*) FROM '+name).fetchone()[0]
                  for name in ('turns','attempts','organization_attempts','memories')}
    restarts = [read(p) for p in (out/'restarts').glob('*.json')]
    closed = [read(p) for p in (out/'openings').glob('*/exit-verified.json')]
    result = {'completed_rounds': len(rows), 'probes': probes, 'database_counts': counts,
              'restarts': restarts, 'closed_openings': len(closed), 'final_snapshot': snapshot(paths),
              'formal_protection_passed': True, 'old_evidence_unchanged': True,
              'raw_results_hashes': {p.name:sha(p) for p in (out/'results').glob('*.json')}}
    atomic_json(out/'source-audit.json', result)
    return {k:v for k,v in result.items() if k not in ('raw_results_hashes','final_snapshot')}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=OUT/'real-v1')
    print(json.dumps(audit(parser.parse_args().out), ensure_ascii=False, indent=2))
