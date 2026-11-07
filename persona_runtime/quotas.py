"""Editable local call limits and explicit, recoverable acceptance overrides."""
from contextlib import contextmanager
import json
import time

from .config import atomic_json
from .instance import InstanceLock
from .memory_policy import project_calls
from .generation_retry import call_count
from .models import RuntimeErrorBase, timestamp

FIELDS = ('project_hourly', 'background_hourly', 'background_daily', 'selector_hourly', 'selector_daily')


def reserve_organization(conn,config,task,now):
    """Same transaction as the attempt: no second quota authority or fake turn."""
    from .memory_policy import project_capacity_used
    from .models import identifier
    cfg=config.load();limits=cfg['organization']
    conn.execute("UPDATE organization_reservations SET state='released' WHERE state='active' AND expires_epoch<=?",(now,))
    reserved=conn.execute("SELECT count(*) FROM organization_reservations WHERE state='active'").fetchone()[0]
    windows=((3600,limits['hourly_limit']),(86400,limits['daily_limit']))
    waits=[]
    for seconds,cap in windows:
        used=call_count(conn,'organization_attempts',now-seconds)+reserved
        if cap and used>=cap:
            row=conn.execute('SELECT min(started_epoch) FROM organization_attempts WHERE started_epoch>?',(now-seconds,)).fetchone()
            waits.append((row[0]+seconds+.01) if row[0] is not None else now+60)
    if cfg['model']['hourly_limit'] and project_capacity_used(conn,now-3600)>=cfg['model']['hourly_limit']:
        # Check again on a bounded poll; the primary ledger is the authority.
        waits.append(now+1)
    if waits:return None,max(waits)
    ident=identifier()
    conn.execute("INSERT INTO organization_reservations VALUES(?,?,NULL,?,'active',?,?)",(ident,task['id'],task['persona_id'],now,now+60))
    return ident,0


class QuotaService:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.journal = config.paths.home / 'runtime/acceptance-quotas.json'

    def _record(self):
        return json.loads(self.journal.read_text(encoding='utf-8')) if self.journal.exists() else {}

    def _limits(self):
        cfg = self.config.load()
        return dict(zip(FIELDS, (cfg['model']['hourly_limit'], cfg['organization']['hourly_limit'], cfg['organization']['daily_limit'], cfg['character_selector']['hourly_limit'], cfg['character_selector']['daily_limit'])))

    def _write(self, limits):
        self.config.update({'model': {'hourly_limit': limits['project_hourly']},
                            'organization': {'hourly_limit': limits['background_hourly'], 'daily_limit': limits['background_daily']},
                            'character_selector': {'hourly_limit':limits['selector_hourly'],'daily_limit':limits['selector_daily']}})

    def current(self):
        limits = self._limits(); record = self._record(); used = None
        try:
            with self.store.connection() as conn:
                now = time.time()
                used = {'project_hourly': project_calls(conn, now-3600),
                        'background_hourly': call_count(conn,'organization_attempts',now-3600),
                        'background_daily': call_count(conn,'organization_attempts',now-86400),
                        'selector_hourly':call_count(conn,'character_selection_attempts',now-3600),
                        'selector_daily':call_count(conn,'character_selection_attempts',now-86400),
                        'understanding_hourly':call_count(conn,'situation_observation_attempts',now-3600),
                        'understanding_daily':call_count(conn,'situation_observation_attempts',now-86400),
                        'compiler_hourly':call_count(conn,'disposition_compile_attempts',now-3600),
                        'compiler_daily':call_count(conn,'disposition_compile_attempts',now-86400),
                        'reserved':conn.execute("SELECT count(*) FROM quota_reservations WHERE state='active' AND expires_epoch>?",(now,)).fetchone()[0]+conn.execute("SELECT count(*) FROM organization_reservations WHERE state='active' AND expires_epoch>?",(now,)).fetchone()[0],
                        'background_unknown':conn.execute("SELECT count(*) FROM organization_attempts WHERE usage_json IS NULL AND status='interrupted'").fetchone()[0],
                        'primary_hourly':call_count(conn,'attempts',now-3600)}
        except RuntimeErrorBase:
            pass  # Settings remain editable before storage is initialized/upgraded.
        active = record.get('status') == 'active'
        return {'limits': limits, 'used': used, 'acceptance_active': active,
                'restore_limits': (limits | record.get('restore_limits',{})) if active else None,
                'acceptance_started_at': record.get('started_at') if active else None}

    def update(self, limits):
        if isinstance(limits,dict) and set(limits)==set(FIELDS[:3]):
            record=self._record()
            baseline=self._limits() | (record.get('restore_limits',{}) if record.get('status')=='active' else {})
            limits=baseline | limits
        if not isinstance(limits, dict) or set(limits) != set(FIELDS) or any(
                type(value) is not int or not 0 <= value <= 10000 for value in limits.values()):
            raise RuntimeErrorBase('Invalid quotas: use integers 0–10000; 0 means unlimited')
        lock = InstanceLock(self.config.paths.home / 'runtime/quota-settings.lock')
        try:
            record = self._record()
            if record.get('status') == 'active':
                record['restore_limits'] = dict(limits)
                atomic_json(self.journal, record)
                self._write(dict.fromkeys(FIELDS, 0))
            else:
                self._write(limits)
        finally:
            lock.close()
        return self.current()

    def begin_acceptance(self):
        from .private_personas import PrivatePersonas
        with self.store.connection() as db:pid=self.store.current(db,required=False)
        PrivatePersonas(self.store.paths).assert_acceptance(pid)
        lock = InstanceLock(self.config.paths.home / 'runtime/quota-settings.lock')
        try:
            record = self._record()
            if record.get('status') != 'active':
                # Save recovery data before changing limits. A process restart must
                # neither lose the original values nor restore during a crash test.
                atomic_json(self.journal, {'status': 'active', 'started_at': timestamp(), 'restore_limits': self._limits()})
            self._write(dict.fromkeys(FIELDS, 0))
        finally:
            lock.close()
        return self.current()

    def end_acceptance(self):
        lock = InstanceLock(self.config.paths.home / 'runtime/quota-settings.lock')
        try:
            record = self._record()
            if record.get('status') == 'active':
                self._write(self._limits() | record['restore_limits'])
                record.update(status='restored', ended_at=timestamp())
                atomic_json(self.journal, record)
        finally:
            lock.close()
        return self.current()

    @contextmanager
    def acceptance(self):
        already_active = self._record().get('status') == 'active'
        self.begin_acceptance()
        try:
            yield self
        finally:
            if not already_active:
                self.end_acceptance()
