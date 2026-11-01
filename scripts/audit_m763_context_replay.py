"""Rebuild already successful R06 contexts offline; never regenerate replies.

Authority connections are read-only and all model dispatch is rejected. Only
hashes and binding diagnostics are written to a new evidence file.
"""
import argparse
import asyncio
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.decision_capsule import AssistAdmission
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.evaluation_material import EvaluationSession
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import safe
from persona_runtime.storage import Store
from scripts.m763_r04 import execution_hashes
from scripts.m763_r06 import PACKAGE
from scripts.m763_retries import effective_case


class ReadOnlyStore(Store):
    def _connect(self, *, create=False):
        if create:
            raise AssertionError('context_audit_cannot_initialize')
        conn = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True,
                               isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        return conn

    @contextmanager
    def connection(self, *, write=False):
        if write:
            raise AssertionError('context_audit_cannot_write_authority')
        with super().connection() as conn:
            yield conn


class NoDispatch:
    async def complete(self, *args, **kwargs):
        raise AssertionError('context_audit_cannot_call_model')


def verify_message_hash(messages, manifest, trace):
    current = fingerprint(messages)
    if current != manifest['input_snapshot_hash'] or current != trace['request_hash']:
        raise ValueError('successful_context_not_reproducible')
    return current


def reject_index(*args, **kwargs):
    raise AssertionError('context_audit_must_reuse_frozen_material')


async def audit(home, out):
    home = safe(home)
    out = safe(out, home / 'exports/acceptance')
    prepared = json.loads((out / 'prepared.json').read_text(encoding='utf-8'))
    freeze = json.loads((out / 'execution-freeze.json').read_text(encoding='utf-8'))
    if execution_hashes() != freeze['source_hashes']:
        raise ValueError('context_audit_source_changed')
    if sha256((out / 'independent-v1.json').read_bytes()).hexdigest() != prepared['fixture_sha256']:
        raise ValueError('context_audit_fixture_changed')
    ledger = EvaluationStore(RuntimePaths(out))
    with ledger.connect(PACKAGE) as db:
        slots = [dict(r) for r in db.execute("SELECT * FROM slots WHERE state='success'")]
    cases = {c['case_id']: c for c in prepared['cases']}
    results = []
    for slot in slots:
        canonical = cases[slot['case_id']]
        case = effective_case(out, canonical)
        turns = case['turns']
        engines = {}
        before = {}
        try:
            for arm in 'ABC':
                root = safe(Path(case['runtime_roots'][arm]), out / 'cases')
                paths = RuntimePaths(root)
                store = ReadOnlyStore(paths)
                cfg = ConfigStore(paths)
                before[arm] = (sha256(store.path.read_bytes()).hexdigest(), sha256(cfg.file.read_bytes()).hexdigest())
                grant = AssistAdmission(root, cfg.load()['instance_id'], frozenset((case['persona_id'],)), home)
                engine = ChatEngine(store, cfg, NoDispatch(), assist_admission=grant)
                engine.cognitive.characters.index_factory = reject_index
                engines[arm] = engine
            sessions = EvaluationSession.register_independent(engines, turns, material_turns=case.get('material_turns'),prompt_comparison=bool(prepared.get('prompt_comparison')))
            b = await engines['B'].selector.prepare(turns['B'])
            c = await engines['C'].selector.prepare(turns['C'])
            observation = await engines['C'].observer.prepare(turns['C'])
            contexts = {arm: await asyncio.to_thread(engines[arm].cognitive.prepare, turns[arm], selection,
                                                     observation if arm == 'C' else None)
                        for arm, selection in (('B', b), ('C', c))}
            common = sessions['C'].verify_independent(turns, contexts)
            arm = slot['arm']
            engine = engines[arm]
            context = contexts[arm] if arm != 'A' else await asyncio.to_thread(engine.cognitive.prepare, turns[arm])
            with engine.store.connection() as db:
                turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (turns[arm],)).fetchone())
                final = engine.cognitive.finalize(db, turn, context)
                row = db.execute('SELECT turn_id,manifest_json FROM context_manifests WHERE attempt_id=?',
                                 (slot['attempt_id'],)).fetchone()
                if not row or row['turn_id'] != turn['id']:
                    raise ValueError('context_audit_attempt_scope')
                manifest = json.loads(row['manifest_json'])
            trace = json.loads((out / 'dispatch' / ('main-' + slot['attempt_id'] + '.json')).read_text(encoding='utf-8'))
            current = verify_message_hash(final['messages'], manifest, trace)
            results.append({'case_id': slot['case_id'], 'arm': arm, 'attempt_id': slot['attempt_id'],
                            'input_hash': current, 'normalized_bc_hash': common, 'reproduced': True})
        finally:
            for arm, engine in engines.items():
                await engine.wait()
                drained = await engine.observer.drain()
                engine.cognitive.shadow.close()
                engine.cognitive.characters.close()
                if not drained:
                    raise AssertionError('context_audit_pending_worker')
                after = (sha256(engine.store.path.read_bytes()).hexdigest(), sha256(engine.config.file.read_bytes()).hexdigest())
                if before[arm] != after:
                    raise AssertionError('context_audit_authority_changed')
    return {'passed': True, 'model_calls': 0, 'authority_read_only': True,
            'source_hashes': freeze['source_hashes'], 'fixture_sha256': prepared['fixture_sha256'],
            'successful_contexts': len(results), 'rows': results,
            'outputs_replaced': 0, 'semantic_quality_verdict': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    dest = safe(args.report, args.output)
    if dest.exists():
        raise FileExistsError('context_audit_preserve_existing_report')
    result = asyncio.run(audit(args.home, args.output))
    with dest.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: result[k] for k in ('passed', 'model_calls', 'authority_read_only', 'successful_contexts')}))
