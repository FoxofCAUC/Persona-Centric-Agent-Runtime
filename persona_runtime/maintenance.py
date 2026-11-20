"""Bounded app-local maintenance; no system settings or old backup restoration."""
from contextlib import closing
import json
import os
import shutil
import sqlite3

from .config import atomic_json
from .models import RuntimeErrorBase,identifier


def mark_write_fault(paths,directory):
    """Best-effort shared latch; failed stores still fail closed before dispatch."""
    directory=directory.resolve();home=paths.home.resolve()
    if not directory.is_relative_to(home):return
    try:atomic_json(paths.home/'runtime/storage-fault.json',{'directory':str(directory.relative_to(home)),'id':identifier()})
    except OSError:pass


def storage_fault_pending(paths):
    return any((paths.home/'runtime'/name).exists() for name in ('storage-fault.json','storage-checkpoint.json'))


def _probe_fault(paths,database):
    from .character_embedding import safe_path
    from .instance import InstanceLock
    runtime=paths.home/'runtime'
    try:lock=InstanceLock(safe_path(paths.home,runtime/'storage-check.lock'))
    except RuntimeErrorBase:return False
    try:
        latch=safe_path(paths.home,runtime/'storage-fault.json')
        claim=safe_path(paths.home,runtime/'storage-checkpoint.json')
        if not claim.exists():
            if not latch.exists():return True
            latch.rename(claim)
        directories={runtime}
        malformed=False
        try:
            reported=json.loads(claim.read_text(encoding='utf8'))['directory']
            if not isinstance(reported,str) or not reported:raise ValueError()
            directories.add(safe_path(paths.home,paths.home/reported))
        except (TypeError,ValueError,KeyError):
            # A damaged latch is retained after checking every runtime fault source.
            malformed=True
        if database.exists():directories.add(safe_path(paths.home,database.parent))
        receipts=runtime/'platform-receipts'
        if receipts.exists():directories.add(safe_path(paths.home,receipts))
        for folder in directories:
            probe=folder/('.pcar-write-probe-'+identifier())
            created=False
            try:
                with probe.open('xb') as stream:
                    created=True
                    stream.write(b'pcar-write-probe');stream.flush();os.fsync(stream.fileno())
            finally:
                if created:probe.unlink(missing_ok=True)
        if database.exists():
            safe_path(paths.home,database)
            with closing(sqlite3.connect(database.as_uri()+'?mode=rw',uri=True,timeout=.25)) as db:
                db.execute('BEGIN IMMEDIATE')
                version=db.execute('PRAGMA user_version').fetchone()[0]
                db.execute('PRAGMA user_version='+str(version))
                db.rollback()
        # A later fault owns the original name and remains latched.
        if malformed:claim.rename(runtime/('storage-unverified-'+identifier()+'.json'))
        else:claim.unlink()
        return not latch.exists()
    except (OSError,ValueError,KeyError,sqlite3.Error):return False
    finally:lock.close()


def inspect_disk(paths):
    database=paths.home/'data/database/runtime.sqlite3'
    free=shutil.disk_usage(paths.home).free
    threshold=max(1024**3,(database.stat().st_size if database.exists() else 0)*3)
    result={'free_bytes':free,'minimum_bytes':threshold,'warning':free<5*1024**3,'blocked':free<threshold}
    if storage_fault_pending(paths) and not _probe_fault(paths,database):
        result.update(blocked=True,reason='storage_write_fault')
    atomic_json(paths.home/'runtime/disk-health.json',result)
    return result


def require_writable_storage(paths):
    """Dispatch and sending must check the real disk, even after a stale health file."""
    if storage_fault_pending(paths):raise RuntimeErrorBase('storage_write_fault')
    free=shutil.disk_usage(paths.home).free
    database=paths.home/'data/database/runtime.sqlite3'
    if free<max(1024**3,(database.stat().st_size if database.exists() else 0)*3):
        raise RuntimeErrorBase('storage_low_pause_dispatch')


def automatic_backup(paths,now,stopping=lambda:False):
    source=paths.home/'data/database/runtime.sqlite3'
    if not source.is_file() or stopping():return
    from .automatic_backup import Backups
    return Backups(paths).create(now,stopping)
