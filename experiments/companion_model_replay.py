"""Sequential normal-ingress replay; no source transcript or credentials in code."""
from pathlib import Path
import hashlib
import time

from experiments import companion_c2_chat as trial
from experiments.companion_workbench_trial import TrialRuntime
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.turn_input import load_turn_input

VERSION = 'companion-native-model-replay-v1'


def execute(manifest, baseline_path, *, client=None, progress=None):
    """Once only. Every source batch waits for its own completed new reply."""
    owner, plan = trial.verify(manifest)
    trial.require(owner['version'] == trial.NATIVE_VERSION and owner.get('model_configuration'), 'replay_scope')
    baseline_path = trial.safe(baseline_path, owner['model_configuration']['home'])
    baseline = trial.read(baseline_path)
    trial.require(Path(baseline['source_home']).resolve() == Path(owner['model_configuration']['home']), 'replay_source_scope')
    baseline_hash = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    data = trial.body(owner) / 'model-replay'
    data.mkdir(exist_ok=False)
    positions = []

    def save(status):
        value = {'version': VERSION, 'status': status, 'baseline_sha256': baseline_hash,
                 'planned_positions': len(baseline['turns']), 'positions': positions,
                 'automatic_resume': False}
        atomic_json(data / 'progress.json', value)
        if progress:
            progress(value)

    save('starting')
    try:
        with TrialRuntime(trial_manifest=manifest, client=client) as runtime:
            local = LocalClient(ConfigStore(runtime.paths))
            for source in baseline['turns']:
                trial.verify(manifest)
                trial.require(hashlib.sha256(baseline_path.read_bytes()).hexdigest() == baseline_hash, 'replay_source_changed')
                row = {'ordinal': source['ordinal'], 'status': 'admitting', 'batch_id': None, 'turn_id': None,
                       'source_turn_id': source['source_turn_id'], 'inputs_exact': False}
                positions.append(row)
                save('running')
                batch_id = None
                for member in source['inputs']:
                    trial.require(hashlib.sha256(member['text'].encode('utf-8')).hexdigest() == member['text_sha256'], 'replay_text_changed')
                    receipt = local.submit_input({'channel': 'desktop', 'persona_id': owner['persona_id'],
                                                  'text': member['text'], 'request_id': identifier()})
                    trial.require(batch_id in (None, receipt['batch_id']), 'replay_batch_split')
                    batch_id = receipt['batch_id']
                row['batch_id'] = batch_id
                batch = local.call('GET', '/v2/batches/' + batch_id)
                local.call('POST', '/v2/batches/' + batch_id + '/seal', {
                    'expected_revision': batch['revision'], 'operation_id': identifier(),
                    'persona_id': owner['persona_id']})
                row['status'] = 'waiting_reply'
                save('running')
                while True:
                    batch = local.call('GET', '/v2/batches/' + batch_id)
                    if batch.get('turn_id'):
                        row['turn_id'] = batch['turn_id']
                        turn = local.call('GET', '/v1/requests/' + row['turn_id'])
                        if turn['status'] not in ('queued', 'running'):
                            break
                    time.sleep(.25)
                row.update(status=turn['status'], error_code=turn.get('error_code'))
                trial.require(turn['status'] == 'completed', 'replay_position_not_completed')
                with Store(runtime.paths).connection() as db:
                    accepted = dict(db.execute('SELECT * FROM turns WHERE id=?', (row['turn_id'],)).fetchone())
                    actual = load_turn_input(db, accepted)
                trial.require([m['text'] for m in actual] == [m['text'] for m in source['inputs']], 'replay_input_mismatch')
                row['inputs_exact'] = True
                audit = trial.audit(manifest)
                latest = next(r for r in audit['rows'] if r['turn_id'] == row['turn_id'])
                trial.require(all(latest.get(k) for k in ('manifest_matches', 'persisted_manifest_matches',
                              'runtime_audit_matches', 'wire_matches')), 'replay_audit_mismatch')
                row['physical_calls'] = latest['physical_calls']
                row['retries'] = latest.get('retries', 0)
                atomic_json(data / ('reply-' + str(source['ordinal']).zfill(2) + '.json'), {
                    'ordinal': source['ordinal'], 'turn_id': row['turn_id'], 'reply_text': turn['reply']})
                save('running')
        audit = trial.audit(manifest)
        trial.require(not audit['pending_turns'] and not audit['active_reservations'], 'replay_cleanup_pending')
        save('completed')
        return {'status': 'completed', 'home': owner['home'], 'replay_directory': str(data),
                'positions': positions, 'audit': audit}
    except BaseException:
        save('stopped_no_automatic_resume')
        raise
