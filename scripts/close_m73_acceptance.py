"""Zero-call R08 restart, export and restoration after the seven fixed slots."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from scripts.acceptance_m73 import Acceptance
from persona_runtime.config import atomic_json
from persona_runtime.lifecycle import ServiceController


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def snapshot(driver):
    with driver.store.connection() as db:
        result = {'global_calls': {table: db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                                  for table in ('attempts', 'organization_attempts')}}
        for table in ('session_packages', 'context_manifests', 'session_operations'):
            rows = [dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE persona_id=? ORDER BY rowid', (driver.pid,))]
            result[table] = {'count': len(rows), 'hash': digest(rows)}
        # Check confirmed platform IDs and attempts, without retaining claim tokens.
        parts = [dict(r) for r in db.execute('SELECT id,state,platform_message_id,sent_at,attempt,text_hash FROM reply_delivery_parts WHERE persona_id=? ORDER BY rowid', (driver.pid,))]
        result['parts'] = parts
        result['pending'] = {}
        for table, clause in (
            ('turns', "status IN ('queued','running')"),
            ('input_batches', "state NOT IN ('linked','cancelled')"),
            ('reply_delivery_parts', "state IN ('pending','sending','failed','uncertain')"),
            ('organization_tasks', "status IN ('queued','running','waiting_quota','interrupted')")):
            result['pending'][table] = db.execute('SELECT count(*) FROM '+table+' WHERE persona_id=? AND '+clause, (driver.pid,)).fetchone()[0]
    return result


def export_check(driver):
    receipt = driver.client.manage('data.export', name='M73-final-acceptance')
    data = json.loads(Path(receipt['path']).read_text(encoding='utf-8'))
    assert data['format_version'] == 10 and data['session_packages'] and data['session_triggers']
    assert data['topic_segments'] and data['conversation_threads'] and data['context_manifests']
    assert any(r['source_refs_json'] for r in data['summaries'])
    forbidden = {'encrypted_key','admin_token','bridge_token','claim_token','model_snapshot','api_key'}
    def inspect(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for v in value.values(): inspect(v)
        elif isinstance(value, list):
            for v in value: inspect(v)
        elif isinstance(value, str) and value[:1] in ('[','{'):
            try: nested = json.loads(value)
            except ValueError: return
            inspect(nested)
    inspect(data)
    result = {'passed': True, 'format_version': 10, 'path': receipt['path'],
              'sha256': hashlib.sha256(Path(receipt['path']).read_bytes()).hexdigest(),
              'counts': {t: len(data[t]) for t in ('session_packages','session_triggers','topic_segments','conversation_threads','context_manifests')},
              'credentials_excluded': True}
    atomic_json(driver.out/'export-final-check.json', result)


def run(output):
    driver = Acceptance(output)
    report_path = driver.out/'R08-restoration.json'
    if report_path.exists(): raise RuntimeError('R08 already recorded; do not rerun')
    expected = {'R01','R02','R03','R04','R05','R06-desktop','R06-qq'}
    assert set(driver.state['calls']) == expected
    assert all(c['status']=='completed' for c in driver.state['calls'].values())
    assert driver.count_calls()==7
    assert json.loads((driver.out/'R04-simulated-idle.json').read_text(encoding='utf-8'))['simulation_exited']
    original = json.loads((driver.out/'original-encrypted-config.json').read_text(encoding='utf-8'))
    controller = ServiceController(driver.paths)
    driver.online()
    assert not driver.cfg.load()['organization']['daily_enabled']
    try:
        driver.use(driver.pid)
        # Only end this stage's empty/finished test sessions. No other persona is sealed here.
        for channel in ('desktop','qq'):
            for row in driver.client.manage('session.list', **driver.scope(channel)):
                if row['state']=='active': driver.seal(row['session_id'],'cleanup-'+row['session_id'],channel)
        before = snapshot(driver)
        assert not any(before['pending'].values())
        atomic_json(driver.out/'R08-before.json', before)
        controller.stop('service')
        controller.start('service')
        time.sleep(6)
        driver.online()
        after = snapshot(driver)
        assert before==after, 'Restart changed calls, packages, manifests, operations or confirmed delivery'
        for channel in ('desktop','qq'):
            for row in driver.client.manage('session.list', **driver.scope(channel)):
                if row['state']=='sealed':
                    driver.client.manage('session.package', **driver.scope(channel), session_id=row['session_id'])
        export_check(driver)
        atomic_json(driver.out/'R08-restart.json', {'passed':True,'before':before,'after':after,'additional_calls':0,'confirmed_resends':0})
    finally:
        driver.use(driver.original)
        # Leave daily processing disabled until services stop, avoiding a new automatic run.
        driver.cfg.update({'model':{'hourly_limit':original['model']['hourly_limit']},
                           'organization':{'hourly_limit':original['organization']['hourly_limit'],
                                           'daily_limit':original['organization']['daily_limit']}})
        controller.stop_all(include_qq=True)
        driver.cfg.update({'organization':{'daily_enabled':original['organization']['daily_enabled']}})
        final_cfg = driver.cfg.load()
        final_services = controller.snapshot()
        final = snapshot(driver)
        with driver.store.connection() as db:
            current = driver.store.current(db)
        report = {'passed':final_cfg==original and current==driver.original and
                            all(v['state']=='已停止' for v in final_services.values()) and
                            not any(final['pending'].values()),
                  'full_configuration_equal':final_cfg==original,'original_persona_restored':current==driver.original,
                  'quota':[final_cfg['model']['hourly_limit'],final_cfg['organization']['hourly_limit'],final_cfg['organization']['daily_limit']],
                  'daily_enabled':final_cfg['organization']['daily_enabled'], 'services':final_services,
                  'final':final,'simulation_exited':True,'model_calls':driver.count_calls()}
        atomic_json(report_path, report)
        assert report['passed'], 'Restoration incomplete; inspect private report'
    print(json.dumps({'passed':True,'calls':7,'services_restored':'stopped','configuration_restored':True}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args().output)
