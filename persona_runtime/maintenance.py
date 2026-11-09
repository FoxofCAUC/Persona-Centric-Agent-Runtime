"""Bounded app-local maintenance; no system settings or old backup restoration."""
from contextlib import closing
import json
import shutil
import sqlite3

from .config import atomic_json
from .models import RuntimeErrorBase,identifier


def mark_write_fault(paths,directory):
    """Best-effort shared latch; failed stores still fail closed before dispatch."""
    directory=directory.resolve();home=paths.home.resolve()
    if not directory.is_relative_to(home):return
    try:atomic_json(paths.home/'runtime/storage-fault.json',{'directory':str(directory.relative_to(home))})
    except OSError:pass


def inspect_disk(paths):
    database=paths.home/'data/database/runtime.sqlite3'
    free=shutil.disk_usage(paths.home).free
    threshold=max(1024**3,(database.stat().st_size if database.exists() else 0)*3)
    result={'free_bytes':free,'minimum_bytes':threshold,'warning':free<5*1024**3,'blocked':free<threshold}
    latch=paths.home/'runtime/storage-fault.json'
    if latch.exists():
        try:
            directory=(paths.home/json.loads(latch.read_text())['directory']).resolve()
            if not directory.is_relative_to(paths.home.resolve()):raise OSError('Unowned fault path')
            probe=directory/('.pcar-write-probe-'+identifier())
            with probe.open('xb') as stream:stream.write(b'pcar-write-probe');stream.flush();__import__('os').fsync(stream.fileno())
            probe.unlink();latch.unlink()
        except (OSError,ValueError,KeyError):result.update(blocked=True,reason='storage_write_fault')
    atomic_json(paths.home/'runtime/disk-health.json',result)
    return result


def require_writable_storage(paths):
    """Dispatch and sending must check the real disk, even after a stale health file."""
    if (paths.home/'runtime/storage-fault.json').exists():raise RuntimeErrorBase('storage_write_fault')
    free=shutil.disk_usage(paths.home).free
    database=paths.home/'data/database/runtime.sqlite3'
    if free<max(1024**3,(database.stat().st_size if database.exists() else 0)*3):
        raise RuntimeErrorBase('storage_low_pause_dispatch')


def automatic_backup(paths,now):
    source=paths.home/'data/database/runtime.sqlite3'
    if not source.exists():return
    root=paths.home/'backups/workbench-automatic'
    journal=root/'index.json'
    value=json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else {'entries':[]}
    entries=value['entries']
    if entries and now-entries[-1]['at']<86400:return
    from .character_embedding import private_directory
    private_directory(paths.home,root)
    name=identifier()+'.sqlite3';destination=root/name
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeErrorBase('backup_integrity_failed')
    entries.append({'file':name,'at':now})
    atomic_json(journal,{'entries':entries[-7:]})
    for entry in entries[:-7]:
        target=(root/entry['file']).resolve()
        if target.parent!=root.resolve() or target.suffix!='.sqlite3':raise RuntimeErrorBase('backup_path_invalid')
        target.unlink(missing_ok=True)
