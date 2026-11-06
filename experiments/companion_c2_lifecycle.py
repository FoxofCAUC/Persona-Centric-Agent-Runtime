"""Two explicit openings of one owned trial, separated by a clean checkpoint."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.models import RuntimeErrorBase, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.submissions import SubmissionJournal

VERSION = 'companion-c2-integration-v1'
NAMESPACE = 'companion-c2-integration'
TITLE = 'PCAR · C2同日连续体验'


def require(condition, reason):
    if not condition:
        raise RuntimeErrorBase('c2_reopen_' + reason)


def local_day():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def snapshot(owner):
    from experiments.companion_expression_adoption import _idle
    paths = RuntimePaths(Path(owner['home']))
    store = Store(paths)
    _idle(store, owner['persona_id'])
    require(not SubmissionJournal(paths, 'desktop').pending(), 'submission_unknown')
    tables = ('events', 'turns', 'attempts', 'input_batches', 'input_batch_members',
              'sessions', 'session_lifecycle', 'session_heads', 'reply_batches',
              'reply_segments', 'reply_display_messages', 'quota_reservations',
              'context_manifests', 'deliveries', 'reply_delivery_parts', 'reply_delivery_streams')
    with store.connection() as db:
        require(not db.execute("SELECT 1 FROM turns WHERE status NOT IN ('completed','cancelled')").fetchone(),
                'request_requires_review')
        require(not db.execute("SELECT 1 FROM attempts WHERE status!='completed'").fetchone(),
                'attempt_requires_review')
        values = {name: [dict(r) for r in db.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]
                  for name in tables}
    # Normal service startup restarts only the idle-observation clock.
    clocks = {row['session_id']: row.pop('observation_started_at') for row in values['session_lifecycle']}
    return {'tables': {name: {'count': len(rows), 'hash': fingerprint(rows)} for name, rows in values.items()},
            'observation_clocks': clocks,
            'turn_ids': [row['id'] for row in values['turns']],
            'session_ids': [row['id'] for row in values['sessions']]}


def begin(owner, data, *, reopen):
    from experiments.companion_c2_chat import read
    path = data / 'lifecycle.json'
    current = snapshot(owner)
    if reopen:
        require(path.exists(), 'checkpoint_missing')
        previous = read(path)
        require(previous['status'] == 'checkpoint_ready' and previous['opening'] == 1,
                'checkpoint_not_available')
        require(previous['local_day'] == local_day(), 'different_day')
        checkpoint = read(data / 'opening-1-closed.json')
        require(fingerprint(checkpoint) == previous['checkpoint_hash'], 'checkpoint_changed')
        require(current == checkpoint['snapshot'], 'history_or_ledger_changed')
        require(current['tables']['turns']['count'] > 0, 'empty_first_segment')
        opening = 2
    else:
        require(not path.exists() and not (data / 'started.json').exists(), 'explicit_reopen_required')
        require(all(current['tables'][t]['count'] == 0 for t in
                    ('events', 'turns', 'attempts', 'input_batches', 'quota_reservations')),
                'initial_history_not_empty')
        opening = 1
        atomic_json(data / 'started.json', {'version': VERSION, 'at': timestamp(), 'startup_generation_calls': 0})
    state = {'version': VERSION, 'status': 'starting', 'opening': opening, 'local_day': local_day(),
             'at': timestamp(), 'before': current, 'automatic_resume': False}
    atomic_json(data / ('opening-' + str(opening) + '-start.json'), state)
    atomic_json(path, state)
    return current['tables']['attempts']['count']


def started(owner, data):
    from experiments.companion_c2_chat import read
    state = read(data / 'lifecycle.json')
    require(state['status'] == 'starting', 'startup_state_invalid')
    current = snapshot(owner)
    changed = [name for name in current['tables'] if current['tables'][name] != state['before']['tables'][name]]
    require({k:v for k,v in current.items() if k != 'observation_clocks'} ==
            {k:v for k,v in state['before'].items() if k != 'observation_clocks'},
            'startup_changed_history_or_generated_' + '_'.join(changed))
    receipt = {'version': VERSION, 'opening': state['opening'], 'at': timestamp(),
               'startup_generation_calls': 0, 'history_and_ledger_equal': True,
               'idle_observation_clock_restarted': current['observation_clocks'] != state['before']['observation_clocks']}
    atomic_json(data / ('opening-' + str(state['opening']) + '-ready.json'), receipt)
    atomic_json(data / 'lifecycle.json', {**state, 'status': 'running'})


def close(manifest, *, normal_exit):
    from experiments import companion_c2_chat as trial
    owner, _ = trial.verify(manifest)
    data = trial.body(owner)
    state = trial.read(data / 'lifecycle.json')
    try:
        require(normal_exit and state['status'] == 'running', 'unclean_exit')
        current = snapshot(owner)
        audit = trial.audit(manifest)
        require(not audit['pending_turns'] and not audit['active_reservations']
                and not any(audit['auxiliary_calls'].values()), 'pending_or_auxiliary')
        for row in audit['rows']:
            if row['status'] == 'cancelled':
                require(row['physical_calls'] == row['simulated_calls'] == 0, 'cancelled_receipt_unknown')
                continue
            require(all(row.get(key) for key in ('manifest_matches', 'persisted_manifest_matches',
                                                 'runtime_audit_matches', 'wire_matches')), 'audit_mismatch')
            folder = data / 'turns' / row['turn_id']
            attempts = row['provider_attempts']
            require(len(list(folder.glob('*.dispatch.json'))) == len(attempts), 'dispatch_count_mismatch')
            require(trial.read(folder / 'generation-terminal.json')['status'] == 'completed', 'terminal_missing')
            for attempt in attempts:
                require(attempt['status'] in ('completed', 'failed'), 'receipt_unknown')
                for suffix in ('.reserve.json', '.attempt.json', '.dispatch.json'):
                    require((folder / (attempt['id'] + suffix)).is_file(), 'receipt_missing')
                if attempt['status'] == 'completed':
                    require((folder / (attempt['id'] + '.response.json')).is_file(), 'response_missing')
        checkpoint = {'version': VERSION, 'opening': state['opening'], 'at': timestamp(),
                      'snapshot': current, 'physical_calls': audit['physical_calls'],
                      'simulated_calls': audit['simulated_calls']}
        path = data / ('opening-' + str(state['opening']) + '-closed.json')
        require(not path.exists(), 'checkpoint_already_written')
        atomic_json(path, checkpoint)
        status = 'checkpoint_ready' if state['opening'] == 1 else 'finished'
        atomic_json(data / 'lifecycle.json', {**state, 'status': status,
                    'checkpoint_hash': fingerprint(checkpoint), 'closed_at': timestamp()})
    except BaseException as error:
        atomic_json(data / 'lifecycle.json', {**state, 'status': 'blocked',
                    'error_code': trial.replay.base.error_code(error), 'automatic_resume': False})
        raise
