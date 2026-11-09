"""Finite daytime resource observation of the normal Workbench.

Reads health and accounting metadata. It performs the planned normal exit at
completion or on a failed observation. It never submits or resumes model work.
"""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
import sqlite3
import time

import psutil

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.instance import current_process_identity, process_identity_alive
from persona_runtime.lifecycle import ServiceController, read_console
from persona_runtime.paths import RuntimePaths, DEFAULT_HOME
from persona_runtime.runtime_control import exit_application
from .processes import owned_processes
from .runtime import core_hashes, read

OUT = DEFAULT_HOME / 'exports/acceptance/M8.E-20261109-daytime'
ATTEMPT_TABLES = ('attempts', 'organization_attempts', 'character_selection_attempts',
                  'disposition_compile_attempts', 'situation_observation_attempts')
LOCAL = timezone(timedelta(hours=8))


def audit(paths):
    file = paths.home / 'data/database/runtime.sqlite3'
    with closing(sqlite3.connect(file.as_uri()+'?mode=ro', uri=True)) as db:
        return {
            'attempts': {table: db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                         for table in ATTEMPT_TABLES},
            'critical_unknown': db.execute("SELECT count(*) FROM organization_tasks WHERE status='blocked_unknown'").fetchone()[0]
                + db.execute("SELECT count(*) FROM reply_delivery_parts WHERE state IN ('sending','uncertain')").fetchone()[0],
            'queued': db.execute("SELECT count(*) FROM organization_tasks WHERE status IN ('queued','waiting_quota','running')").fetchone()[0],
            'daily': [list(row) for row in db.execute('SELECT status,next_epoch FROM daily_updates')],
            'schema': db.execute('PRAGMA user_version').fetchone()[0],
        }


def finish(paths, lease, observed):
    controller = ServiceController(paths)
    exit_application(paths, lease['instance_id'], 'm8e-daytime-observation-finished')
    deadline = time.monotonic()+60
    while process_identity_alive(lease['process']) and time.monotonic() < deadline:
        time.sleep(.5)
    report = read(paths.home/'runtime/last-exit.json')
    assert report.get('console_token') == lease['token']
    assert not process_identity_alive(lease['process'])
    assert report['processes_released'] and not controller.has_resident_processes()
    deadline = time.monotonic()+15
    while time.monotonic() < deadline:
        remaining = []
        for pid, born in observed:
            try:
                process = psutil.Process(pid)
                if process.create_time() == born:
                    remaining.append(pid)
            except psutil.NoSuchProcess:
                pass
        if not remaining:
            break
        time.sleep(.5)
    assert not remaining
    atomic_json(OUT/'exit-result.json', {
        'report': report, 'owner_exited': True, 'owned_processes_released': True,
        'external_browsers_not_terminated': True})
    return report


def run():
    paths = RuntimePaths(DEFAULT_HOME)
    expected = read(OUT/'observation-manifest.json')
    lease = read_console(paths)
    assert lease['process'] == expected['owner']
    assert lease['instance_id'] == expected['instance_id']
    assert expected['core_hashes'] == core_hashes()
    assert process_identity_alive(lease['process'])
    cfg = ConfigStore(paths)
    client = LocalClient(cfg)
    observed = set()
    previous = {}
    previous_at = None
    started = time.time()
    ready_since = None
    baseline = None
    idle = []
    idle_result = None
    gaps = []
    outcome = None
    deadline = time.monotonic()+3*3600
    atomic_json(OUT/'observer-launch.json', {
        'process': current_process_identity(), 'owner': lease['process'],
        'started': started, 'model_calls_created': 0})
    try:
        with (OUT/'samples.jsonl').open('a', encoding='utf-8') as log:
            while time.monotonic() < deadline:
                if not process_identity_alive(lease['process']):
                    raise RuntimeError('workbench_exited_before_observation_completed')
                now = time.time()
                gap = now-previous_at if previous_at else None
                if gap and not 0 < gap <= 20:
                    gaps.append({'epoch': now, 'gap_seconds': gap})
                    ready_since = None
                    baseline = None
                    idle = []
                    idle_result = None
                current = {}
                rss = 0
                for process in owned_processes(paths, lease['process']):
                    try:
                        with process.oneshot():
                            key = (process.pid, process.create_time())
                            cpu = process.cpu_times()
                            current[key] = cpu.user+cpu.system
                            rss += process.memory_info().rss
                            observed.add(key)
                    except psutil.NoSuchProcess:
                        continue
                delta = sum(max(0, value-previous.get(key, 0)) for key, value in current.items())
                percent = 100*delta/gap/(os.cpu_count() or 1) if gap and 0 < gap <= 20 else None
                application = read(paths.home/'runtime/application.json')
                states = application['states']
                native = states.get('napcat', {})
                bridge = states.get('astrbot', {})
                health = client.call('GET', '/v1/health', timeout=3)
                accounting = audit(paths)
                ready = (native.get('account_logged_in') is True
                         and native.get('account_matches') is True
                         and bridge.get('onebot_connected') is True
                         and bridge.get('account_online') is True
                         and health['instance_id'] == expected['instance_id']
                         and (datetime.now(timezone.utc)-datetime.fromisoformat(application['at'])).total_seconds() < 20)
                if not ready:
                    raise RuntimeError('component_or_native_session_not_ready')
                if accounting['critical_unknown']:
                    raise RuntimeError('critical_unknown_requires_review')
                main_added = accounting['attempts']['attempts']-expected['accounting']['attempts']['attempts']
                background_added = accounting['attempts']['organization_attempts']-expected['accounting']['attempts']['organization_attempts']
                if main_added+3 >= 12 or background_added+9 >= 24:
                    raise RuntimeError('stage_acceptance_call_cap_reached')
                if ready_since is None:
                    ready_since = now
                row = {'epoch': now, 'local': datetime.now(LOCAL).isoformat(),
                       'process_count': len(current), 'rss_bytes': rss,
                       'cpu_percent_whole_machine': percent, 'ready': ready,
                       'tray': read_console(paths).get('display_state') == 'tray'}
                log.write(json.dumps(row)+'\n')
                log.flush()
                if baseline is None and now-ready_since >= 180:
                    baseline = row
                is_idle = not (health['pending'] or health['background_running']
                               or health['buffer_pending'] or health['sending_parts'] or accounting['queued'])
                idle = idle+[row] if is_idle and percent is not None else []
                if idle_result is None and idle and now-idle[0]['epoch'] >= 600:
                    seconds = idle[-1]['epoch']-idle[0]['epoch']
                    weighted = sum(item['cpu_percent_whole_machine']*(item['epoch']-prior['epoch'])
                                   for prior, item in zip(idle, idle[1:]))
                    mean = weighted/seconds
                    idle_result = {'seconds': seconds, 'samples': len(idle),
                                   'mean_cpu_percent_whole_machine': mean, 'passed': mean <= 3}
                    atomic_json(OUT/'idle-10min.json', idle_result)
                    if not idle_result['passed']:
                        raise RuntimeError('idle_cpu_above_agreed_threshold')
                progress = {'started': started, 'last_sample': row, 'baseline': baseline,
                            'continuous_ready_seconds': now-ready_since, 'idle_cpu': idle_result,
                            'sleep_or_observation_gaps': gaps, 'accounting': accounting,
                            'new_main_calls': main_added, 'new_background_calls': background_added,
                            'model_calls_created_by_observer': 0, 'owner_alive': True}
                atomic_json(OUT/'progress.json', progress)
                if baseline and now-baseline['epoch'] >= 7200 and idle_result:
                    limit = max(100*1024**2, baseline['rss_bytes']*.1)
                    growth = rss-baseline['rss_bytes']
                    assert core_hashes() == expected['core_hashes']
                    if growth > limit:
                        raise RuntimeError('memory_growth_requires_investigation')
                    outcome = progress | {'passed': True, 'two_hours': row,
                        'memory_growth_bytes': growth, 'memory_growth_limit_bytes': limit,
                        'natural_midnight_observed': False, 'daytime_amendment': True}
                    break
                previous = current
                previous_at = now
                time.sleep(5)
        if outcome is None:
            raise RuntimeError('finite_observation_window_exhausted')
    except Exception as error:
        outcome = {'passed': False, 'reason': str(error), 'error_type': type(error).__name__,
                   'started': started, 'ended': time.time(), 'sleep_or_observation_gaps': gaps,
                   'model_calls_created_by_observer': 0}
    finally:
        if process_identity_alive(lease['process']):
            try:
                report = finish(paths, lease, observed)
                outcome['logout_confirmed'] = report['logout_confirmed']
                outcome['software_exited'] = True
                outcome['passed'] = outcome['passed'] and report['logout_confirmed']
            except Exception as error:
                outcome.update(passed=False, exit_error=type(error).__name__)
        else:
            outcome.update(passed=False, software_exited=True)
        atomic_json(OUT/'result.json', outcome)
    print(json.dumps(outcome, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    run()
