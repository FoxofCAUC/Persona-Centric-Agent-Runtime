"""Editable local call limits and explicit, recoverable acceptance overrides."""
from contextlib import contextmanager
import json
import time

from .config import atomic_json
from .instance import InstanceLock
from .memory_policy import project_calls
from .models import RuntimeErrorBase, timestamp

FIELDS = ('project_hourly', 'background_hourly', 'background_daily', 'selector_hourly', 'selector_daily')


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
                        'background_hourly': conn.execute('SELECT count(*) FROM organization_attempts WHERE started_epoch>?', (now-3600,)).fetchone()[0],
                        'background_daily': conn.execute('SELECT count(*) FROM organization_attempts WHERE started_epoch>?', (now-86400,)).fetchone()[0],
                        'selector_hourly':conn.execute('SELECT count(*) FROM character_selection_attempts WHERE started_epoch>?',(now-3600,)).fetchone()[0],
                        'selector_daily':conn.execute('SELECT count(*) FROM character_selection_attempts WHERE started_epoch>?',(now-86400,)).fetchone()[0],
                        'compiler_hourly':conn.execute('SELECT count(*) FROM disposition_compile_attempts WHERE started_epoch>?',(now-3600,)).fetchone()[0],
                        'compiler_daily':conn.execute('SELECT count(*) FROM disposition_compile_attempts WHERE started_epoch>?',(now-86400,)).fetchone()[0],
                        'reserved':conn.execute("SELECT count(*) FROM quota_reservations WHERE state='active' AND expires_epoch>?",(now,)).fetchone()[0],
                        'primary_hourly':conn.execute('SELECT count(*) FROM attempts WHERE started_epoch>?',(now-3600,)).fetchone()[0]}
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
