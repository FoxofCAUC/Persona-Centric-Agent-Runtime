"""Stopped-instance, scoped Stage 7 deployment. No model or delivery dispatch."""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.verify_m761_migration import snapshot
from persona_runtime.character_embedding import private_directory
from persona_runtime.companion import choose
from persona_runtime.config import ConfigStore
from persona_runtime.context_policy import values
from persona_runtime.ingress import IngressService
from persona_runtime.models import identifier
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.qq_testing import QQTesting
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


def switch(ingress,pid,heads,*,record=lambda value:None):
    """Use the existing versioned authority; hold admission until both heads agree."""
    completed=[]
    try:
        for channel in ('desktop','qq'):
            old=heads[channel]
            if old.get('companion') or (old.get('delivery') or {}).get('state')=='disabled':
                raise ValueError('Existing active/disabled policy requires a fresh preview')
            result=ingress.update_policy(pid,channel,'natural_batch',old['id'],identifier(),
                expected_selected=pid,conversation_policy_version=2,expression_policy_version=3,
                reply_mode='context_full',context_mode='working_set',cross_session=False,
                character_retrieval_mode='hybrid',disposition_mode='baseline',
                situation_observation_mode='disabled',companion_mode='minimal_im',delivery_source='release')
            completed.append((channel,result));record({'channel':channel,'result':result})
        for channel,result in completed:
            current=ingress.current_policy(pid,channel)
            if current['id']!=result['id'] or current['companion']!=choose('minimal_im'):
                raise ValueError('Post-switch verification failed')
        return dict(completed)
    except BaseException:
        for channel,result in reversed(completed):
            current=ingress.current_policy(pid,channel)
            restored=ingress.update_policy(pid,channel,current['mode'],result['id'],identifier(),
                expected_selected=pid,companion_mode='disabled')
            record({'channel':channel,'rolled_back':restored})
        raise


def idle(db):
    checks={
        'turns':"status IN ('queued','running')",
        'input_batches':"state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')",
        'organization_tasks':"status IN ('queued','waiting_quota','running','interrupted')",
        'reply_delivery_parts':"state IN ('pending','sending','failed','uncertain')",
        'deliveries':"state IN ('pending','sending','uncertain') AND NOT EXISTS "
            "(SELECT 1 FROM reply_delivery_parts p WHERE p.turn_id=deliveries.turn_id) AND NOT EXISTS "
            "(SELECT 1 FROM legacy_delivery_dispositions x WHERE x.turn_id=deliveries.turn_id)",
        'quota_reservations':"state='active'",
        'attempts':"status IN ('running','interrupted')",
        'organization_attempts':"status IN ('running','interrupted')",
    }
    counts={t:db.execute('SELECT count(*) FROM '+t+' WHERE '+where).fetchone()[0] for t,where in checks.items()}
    if any(counts.values()):raise ValueError('Unresolved work; no automatic replay or migration: '+json.dumps(counts))
    return counts


def apply(out):
    out=Path(out).resolve()
    from persona_runtime.private_personas import safe
    safe(out,DEFAULT_HOME/'exports/acceptance')
    read=lambda p:json.loads(p.read_text(encoding='utf-8'))
    if (out/'deployment-started.json').exists():raise ValueError('Deployment already started; inspect checkpoint, never replay')
    qq_out=safe(out/read(out/'qq-selected.json')['directory'],out)
    desktop=read(out/'desktop-real-v1/summary.json');qq=read(qq_out/'summary.json')
    if not desktop['passed'] or not qq['passed'] or read(qq_out/'completion-close.json')['resident']:
        raise ValueError('Isolated acceptance and test Bot shutdown required')
    if not read(out/'formal-copy-migration/report.json')['source_unchanged']:raise ValueError('Migration proof required')
    preview=read(out/'formal-preview.json');pid=preview['daily_persona']
    if read(out/'formal-target-confirmed.json')['persona_id']!=pid:raise ValueError('Target confirmation required')
    paths=RuntimePaths(DEFAULT_HOME);store=Store(paths);config=ConfigStore(paths)
    with ExitStack() as stack:
        QQTesting(paths)._stopped(stack)
        test=QQTesting(paths).load(True);cfg=config.load()
        if test['state']!='inactive' or (cfg['bot_qq'],cfg['owner_qq'])!=(test['daily_bot'],test['daily_peer']):raise ValueError('Daily Bot assignment changed')
        with closing(sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute('PRAGMA user_version').fetchone()[0]!=20:raise ValueError('Expected unmigrated schema 20')
            if Store.current(db)!=pid:raise ValueError('Selected persona changed')
            idle(db);columns,before=snapshot(db)
            for p in preview['policies']:
                if db.execute('SELECT version_id FROM interaction_heads WHERE persona_id=? AND channel=?',(pid,p['channel'])).fetchone()[0]!=p['id']:raise ValueError('Preview is stale')
            backup_dir=paths.home/'backups'/('stage7-'+identifier());private_directory(paths.home,backup_dir)
            backup=backup_dir/'before-schema21.sqlite3'
            with closing(sqlite3.connect(backup)) as target:db.backup(target)
        def write(name,value):
            with (out/name).open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)
        write('deployment-started.json',{'persona_id':pid,'backup':str(backup),'old_tables':before,'policy_heads':preview['policies']})
        store.initialize()
        engine=SimpleNamespace(store=store,config=config,service=RuntimeService(store),tasks={})
        ingress=IngressService(engine);heads={c:ingress.current_policy(pid,c) for c in ('desktop','qq')}
        changes=[]
        def record(value):
            changes.append(value);write('deployment-step-'+str(len(changes))+'.json',value)
        result=switch(ingress,pid,heads,record=record)
        with store.connection() as db:
            idle(db)
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Integrity failed')
            after=snapshot(db,columns)[1]
            expected={'interaction_policies','interaction_heads','batch_operations'}
            changed={t for t in before if before[t]!=after[t]}
            if not changed.issubset(expected):raise ValueError('Unexpected business mutation: '+str(changed))
        write('deployment-result.json',{'schema':21,'persona_id':pid,'heads':result,'backup':str(backup),
            'changed_tables':sorted(changed),'unrelated_tables_unchanged':True,'services_started':False,
            'model_calls':0,'channel_validation':'pending'})
        # Pause the already-due daily trigger for the two finite formal positions.
        # Restore the saved flag after the verification host stops, before normal use.
        write('formal-schedule-pause.json',{'original_daily_enabled':cfg['organization']['daily_enabled'],
            'reason':'finite formal validation; no additional organization positions'})
        config.update({'organization':{'daily_enabled':False}})
    return {'schema':21,'dual_entry_v8':True,'services_started':False}


if __name__=='__main__':print(json.dumps(apply(Path(sys.argv[1]))))
