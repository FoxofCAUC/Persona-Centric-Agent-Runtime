"""User-amended, zero-call formal short check and normal online exit.

This does not complete N1 or replace its interrupted observation evidence.
It never edits business task state, quotas, authentication, or delivery results.
"""
import hashlib
import time

import psutil

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.instance import process_identity_alive
from persona_runtime.lifecycle import ServiceController, read_console
from persona_runtime.models import timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.runtime_control import exit_application
from .finish import backup_audit, business_audit, native_totals, snapshot_processes
from .runtime import EVIDENCE, core_hashes, read


def run():
    amendment = read(EVIDENCE/'user-schedule-amendment.json')
    assert amendment['tonight_two_hours_cancelled']
    assert amendment['tonight_natural_midnight_cancelled']
    assert read(EVIDENCE/'formal-component-applied.json')['applied']
    paths = RuntimePaths(DEFAULT_HOME)
    cfg = ConfigStore(paths).load()
    lease = read_console(paths)
    identity = lease['process']
    assert process_identity_alive(identity)
    assert read(EVIDENCE/'formal-owner.json')['core_hashes'] == core_hashes()
    for slot in ('I1', 'I2', 'I3A', 'I3B', 'L1', 'P1'):
        assert read(EVIDENCE/(slot+'-result.json'))['passed']
    controller = ServiceController(paths)
    states = {role: controller.health(role) for role in ('service', 'astrbot', 'napcat')}
    assert all(state['owned'] for state in states.values())
    assert states['napcat']['account_logged_in'] is True
    assert states['napcat']['account_matches'] is True
    assert states['astrbot']['login'] is True and states['astrbot']['onebot'] is True
    native = read(paths.home/'runtime/napcat.json')
    environment = psutil.Process(native['qq_listener']['ProcessId']).environ()
    assert environment['NAPCAT_QQ_DATA_DIR'] == str(paths.home/'data/qq')
    assert environment['NAPCAT_WORKDIR'] == str(paths.home/'components/napcat')
    deployment = read(paths.home/'runtime/deployment.json')
    assert deployment['napcat_release'] == '4.18.42'
    assert deployment['qq_data_contract'] == 'runtime-home-v1'
    assert hashlib.sha256((paths.home/'components/napcat/napcat.mjs').read_bytes()).hexdigest() == deployment['napcat_main_sha256']
    window = read(EVIDENCE/'formal-window/ready.json')
    assert window['pid'] == identity['pid'] and window['tray_active']
    business = business_audit(paths)
    assert business['critical_unknown_count'] == business['queue_count'] == 0
    totals = native_totals()
    assert totals == read(EVIDENCE/'formal-component-applied.json')['native_calls']
    processes = snapshot_processes(paths, identity)
    atomic_json(EVIDENCE/'formal-new-component-short-result.json', {
        'at': timestamp(), 'passed': True, 'owner': identity, 'states': states,
        'independent_data_verified': True, 'tray_active': True,
        'business': business, 'backup': backup_audit(paths), 'native_calls': totals,
        'new_model_calls': 0, 'new_qq_messages': 0,
        'prior_P1_user_visible_proof_reused': True, 'N1_passed': False,
        'long_run_deferred_by_user': True, 'observed_owned_processes': processes})
    exit_application(paths, cfg['instance_id'], 'm8e-user-amended-tonight-normal-exit')
    deadline = time.monotonic()+55
    while process_identity_alive(identity) and time.monotonic() < deadline:
        time.sleep(.5)
    report = read(paths.home/'runtime/last-exit.json')
    assert report.get('console_token') == lease['token']
    assert not process_identity_alive(identity)
    assert report['logout_confirmed'] and report['processes_released']
    # Native identity diagnostics can finish just after the GUI has exited.
    deadline = time.monotonic()+15
    while any(process_identity_alive(p['identity']) for p in processes) and time.monotonic() < deadline:
        time.sleep(.5)
    assert all(not process_identity_alive(p['identity']) for p in processes)
    assert not controller.has_resident_processes()
    assert read(EVIDENCE/'formal-window/destroyed.json')['native_window_destroyed']
    assert business_audit(paths)['critical_unknown_count'] == 0
    assert native_totals() == totals
    atomic_json(EVIDENCE/'formal-exit-result.json', {
        'at': timestamp(), 'passed': True, 'scope': 'normal_online_logout_and_owned_process_release',
        'report': report, 'owner_exited': True, 'observed_owned_children_released': True,
        'external_browsers_not_terminated': True, 'native_calls': totals,
        'long_run_pending': True, 'all_acceptance_passed': False})
    print('Formal short check, online logout and owned process release passed; N1 deferred.', flush=True)


if __name__ == '__main__':
    run()
