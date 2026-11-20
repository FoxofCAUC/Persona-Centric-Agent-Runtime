"""Publish and restore verified local SQLite snapshots of one PCAR instance."""
from contextlib import closing
import hashlib
import hmac
import json
import math
import re
from pathlib import Path
import shutil
import sqlite3
import tempfile
from uuid import UUID

from .character_embedding import private_directory, safe_path
from .config import ConfigStore, atomic_json
from .instance import InstanceLock
from .knowledge_transfer import backup_dependencies, backup_manifest
from .models import RuntimeErrorBase, identifier

KEEP = 7
INTERVAL = 86400


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def _source_member(path):
    try:return path.open("rb")
    except PermissionError:raise RuntimeErrorBase("backup_source_unreadable") from None


def _filename(value):
    if not isinstance(value,str) or not value.endswith(".sqlite3"):
        raise RuntimeErrorBase("backup_path_invalid")
    try:valid=str(UUID(value[:-8]))+".sqlite3"==value
    except ValueError:valid=False
    if not valid:raise RuntimeErrorBase("backup_path_invalid")
    return value


def _time(value):
    if type(value) not in (float,int) or not math.isfinite(value) or value<0:
        raise RuntimeErrorBase("backup_time_invalid")
    return value


def _identity(path):
    info=path.stat()
    return [info.st_dev,info.st_ino] if info.st_ino and info.st_nlink==1 else None


def _inspect(path):
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro&immutable=1",uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA integrity_check").fetchall()!=[("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeErrorBase("backup_integrity_failed")
        from .storage import SCHEMA_VERSION,Store
        schema=db.execute("PRAGMA user_version").fetchone()[0]
        if schema==SCHEMA_VERSION:Store._check(db)
        elif not 24<=schema<SCHEMA_VERSION:raise RuntimeErrorBase("backup_schema_unsupported")
        counts={name:db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in
                ("personas","sessions","events","turns","memories","attempts")}
        dependencies=backup_dependencies(db)
        if any(not isinstance(v["sha256"],str) or not re.fullmatch('[a-f0-9]{64}',v["sha256"]) for v in dependencies):
            raise RuntimeErrorBase("backup_dependency_invalid")
        return {"schema":schema,"counts":counts,"required_packages":dependencies}


class Backups:
    def __init__(self,paths):
        self.paths=paths
        config=ConfigStore(paths)
        self.instance=config.load()["instance_id"]
        self.signing_key=config.token().encode()
        self.root=safe_path(paths.home,paths.home/"backups/workbench-automatic")
        self._stopping=lambda:False

    def _check_stop(self):
        if self._stopping():raise RuntimeErrorBase("backup_paused_on_exit")

    def _seal(self,data):
        raw=json.dumps(data,ensure_ascii=True,sort_keys=True,separators=(",",":")).encode()
        return data|{"seal":hmac.digest(self.signing_key,raw,"sha256").hex()}

    def _authentic(self,data):
        if not isinstance(data,dict) or data.get("instance")!=self.instance:return False
        body={k:v for k,v in data.items() if k!="seal"}
        seal=data.get("seal")
        return isinstance(seal,str) and seal.isascii() and hmac.compare_digest(seal,self._seal(body)["seal"])

    def _parse_index(self,data):
        if not isinstance(data,dict):raise RuntimeErrorBase("backup_index_unverified")
        if data.get("version")==2:
            if not self._authentic(data):raise RuntimeErrorBase("backup_index_unverified")
        elif set(data)=={"entries"} and isinstance(data["entries"],list):
            legacy=[]
            for row in data["entries"]:
                if not isinstance(row,dict):continue
                try:legacy.append({"file":_filename(row.get("file")),"at":_time(row.get("at"))})
                except RuntimeErrorBase:continue
            data={"version":2,"instance":self.instance,"entries":[],"legacy":legacy,"retired":[],
                  "unverified_legacy_count":len(data["entries"])-len(legacy)}
        else:raise RuntimeErrorBase("backup_index_unverified")
        if any(not isinstance(data.get(k),list) for k in ('entries','retired','legacy')):
            raise RuntimeErrorBase("backup_index_unverified")
        for row in [*data["entries"],*data["retired"],*data["legacy"]]:
            if not isinstance(row,dict):raise RuntimeErrorBase("backup_index_unverified")
            _filename(row["file"]);_time(row["at"])
        return {k:v for k,v in data.items() if k!="seal"}

    def _index(self):
        path=safe_path(self.root,self.root/"index.json")
        previous=safe_path(self.root,self.root/"index.previous.json")
        self._fallback=False
        if not path.exists() and not previous.exists():
            return {"version":2,"instance":self.instance,"entries":[],"retired":[],"legacy":[]}
        try:return self._parse_index(_read(path))
        except (FileNotFoundError,ValueError,KeyError,TypeError):
            if not previous.is_file():raise RuntimeErrorBase("backup_index_unverified") from None
            try:
                old=_read(previous)
                if not self._authentic(old):raise RuntimeErrorBase("backup_index_unverified")
                restored=self._parse_index(old)
            except (FileNotFoundError,ValueError,KeyError,TypeError):
                raise RuntimeErrorBase("backup_index_unverified") from None
            self._fallback=True
            return restored

    def _publish_index(self,index):
        self._check_stop()
        path=safe_path(self.root,self.root/"index.json")
        if path.exists():
            try:old=_read(path)
            except (ValueError,OSError):old=None
            if self._authentic(old) and old.get('entries')!=index['entries']:
                atomic_json(safe_path(self.root,self.root/"index.previous.json"),old,durable=True)
            elif isinstance(old,dict) and set(old)=={"entries"}:
                # Preserve the original unsigned index, including unrecognized user entries.
                archive=safe_path(self.root,self.root/("index-legacy-"+identifier()+".json"))
                shutil.copyfile(path,archive)
        atomic_json(path,self._seal(index),durable=True)

    def _paths(self,folder,entry):
        name=_filename(entry["file"])
        return [safe_path(self.root,folder/name),safe_path(self.root,folder/(name+".knowledge.json"))]

    def _verify(self,db_path,manifest_path,entry):
        info=_inspect(db_path)
        manifest=_read(manifest_path)
        sha=_digest(db_path)
        if (not isinstance(manifest,dict) or manifest.get("version")!=1 or sha!=manifest.get("database_sha256") or
                sorted(manifest["required_packages"],key=lambda v:v["sha256"])!=info["required_packages"]):
            raise RuntimeErrorBase("backup_manifest_mismatch")
        if "database_sha256" in entry and (entry["database_sha256"]!=sha or entry["manifest_sha256"]!=_digest(manifest_path)):
            raise RuntimeErrorBase("backup_digest_mismatch")
        return info|{"database_sha256":sha}

    def _retire(self,index):
        remaining=[]
        for row in index["retired"]:
            self._check_stop()
            hashes=[row["database_sha256"],row["manifest_sha256"]]
            identities=row.get("identities") or [None,None]
            try:
                files=self._paths(self.root,row)
                if any(path.exists() and (not ident or _identity(path)!=ident or _digest(path)!=sha)
                       for path,sha,ident in zip(files,hashes,identities,strict=True)):
                    remaining.append(row)
                    continue
                for path in files:path.unlink(missing_ok=True)
            except (PermissionError,FileNotFoundError,RuntimeErrorBase):
                remaining.append(row)
        if remaining!=index["retired"]:
            index["retired"]=remaining
            self._publish_index(index)

    def _recover(self,index):
        journal=safe_path(self.root,self.root/"pending.json")
        if journal.exists():
            try:work=_read(journal)
            except ValueError:work=None
            if not self._authentic(work):
                journal.rename(self.root/("unverified-operation-"+identifier()+".json"))
                self._retire(index)
                return
            entry=work["entry"]
            stage=safe_path(self.root,self.root/(".building-"+_filename(entry["file"])))
            staged=self._paths(stage,entry)
            marker=safe_path(self.root,stage/"owner.json")
            try:own_stage=marker.is_file() and _read(marker)==self._seal({"instance":self.instance,"building":entry["file"]})
            except (OSError,ValueError):own_stage=False
            if work["phase"]=="verified":
                targets=self._paths(self.root,entry)
                for src,dst,key in zip(staged,targets,("database_sha256","manifest_sha256")):
                    self._check_stop()
                    if dst.exists():
                        if _digest(dst)==entry[key]:continue
                    elif own_stage and src.is_file() and _digest(src)==entry[key]:
                        src.rename(dst)
                        continue
                    journal.rename(self.root/("unresolved-"+identifier()+".json"))
                    self._retire(index)
                    return
                # The signed verified journal binds both exact file digests
                # to the integrity and dependency checks already completed in staging.
                if not any(row["file"]==entry["file"] for row in index["entries"]):
                    entries=index["entries"]+[entry]
                    index["entries"]=entries[-KEEP:]
                    index["retired"].extend(entries[:-KEEP])
                    self._publish_index(index)
            elif work["phase"]!="copying":raise RuntimeErrorBase("backup_operation_unverified")
            if own_stage:
                for path in [*staged,*[Path(str(staged[0])+s) for s in ("-journal","-shm","-wal")]]:
                    safe_path(stage,path).unlink(missing_ok=True)
                marker.unlink()
                if not any(stage.iterdir()):stage.rmdir()
            journal.unlink()
        self._retire(index)

    def create(self,now,stopping=lambda:False):
        now=_time(now)
        source=safe_path(self.paths.home,self.paths.home/"data/database/runtime.sqlite3")
        if not source.is_file() or stopping():return
        if not self.root.exists():private_directory(self.paths.home,self.root)
        try:lock=InstanceLock(safe_path(self.root,self.root/"backup.lock"))
        except RuntimeErrorBase:return {"state":"busy"}
        try:
            self._stopping=stopping
            try:index=self._index()
            except RuntimeErrorBase as error:
                if str(error)!="backup_index_unverified":raise
                for name in ("index.json","index.previous.json"):
                    path=safe_path(self.root,self.root/name)
                    if path.is_file():path.rename(self.root/("index-unverified-"+identifier()+".json"))
                index=self._index()
            if self._fallback:
                path=safe_path(self.root,self.root/"index.json")
                if path.exists():path.rename(self.root/("index-unverified-"+identifier()+".json"))
                self._publish_index(index)
            self._recover(index)
            entries=index["entries"]
            if entries and now-entries[-1]["at"]<INTERVAL and all(p.is_file() for p in self._paths(self.root,entries[-1])):
                return {"state":"current","at":entries[-1]["at"]}
            if stopping():return
            private_directory(self.paths.home,self.root)
            entry={"file":identifier()+".sqlite3","at":now}
            work={"instance":self.instance,"phase":"copying","entry":entry}
            journal=self.root/"pending.json"
            stage=safe_path(self.root,self.root/(".building-"+entry["file"]))
            if stage.exists() or any(p.exists() for p in self._paths(self.root,entry)):
                raise RuntimeErrorBase("backup_destination_conflict")
            atomic_json(journal,self._seal(work),durable=True)
            stage.mkdir()
            atomic_json(stage/"owner.json",self._seal({"instance":self.instance,"building":entry["file"]}),durable=True)
            database,manifest=self._paths(stage,entry)
            def check(*_):
                if stopping():raise RuntimeErrorBase("backup_paused_on_exit")
            with closing(sqlite3.connect(source.as_uri()+"?mode=ro",uri=True)) as src,closing(sqlite3.connect(database)) as dst:
                src.backup(dst,pages=128,progress=check,sleep=.05)
            check()
            backup_manifest(database,include_empty=True)
            info=self._verify(database,manifest,entry)
            entry.update(database_sha256=info["database_sha256"],manifest_sha256=_digest(manifest),bytes=database.stat().st_size,
                         identities=[_identity(database),_identity(manifest)])
            check()
            atomic_json(journal,self._seal(work|{"phase":"verified"}),durable=True)
            self._recover(index)
            return {"state":"published","file":entry["file"],"at":now}
        finally:
            self._stopping=lambda:False
            lock.close()

    def status(self):
        index=self._index()
        rows=sorted([{"file":r["file"],"at":r["at"],"legacy":legacy} for legacy,key in ((False,"entries"),(True,"legacy"))
                     for r in index[key]],key=lambda r:r["at"],reverse=True)
        return {"entries":rows,"index_fallback":self._fallback,
                "deferred_retirement":len(index["retired"]),
                "unverified_legacy_count":index.get("unverified_legacy_count",0)}

    def restore(self,destination,name=None):
        from .installation import validate_home
        target=validate_home(Path(destination))
        if target.is_relative_to(self.paths.home) or self.paths.home.is_relative_to(target) or target.exists():
            raise RuntimeErrorBase("backup_target_must_be_new_and_independent")
        if not self.root.is_dir():raise RuntimeErrorBase("backup_not_available")
        index=self._index()
        candidates=sorted(index["entries"]+index["legacy"],key=lambda r:r["at"],reverse=True)
        if name:candidates=[r for r in candidates if r["file"]==_filename(name)]
        if not candidates:raise RuntimeErrorBase("backup_not_available")
        target.parent.mkdir(parents=True,exist_ok=True)
        stage=Path(tempfile.mkdtemp(prefix=".pcar-restore-",dir=target.parent))
        try:
            private_directory(stage,stage)
            database=stage/"runtime.sqlite3"
            manifest=stage/"runtime.sqlite3.knowledge.json"
            for entry in candidates:
                # Both immutable members stay open while being copied. Rotation
                # can be deferred; reading a restore requires no write on the source.
                try:
                    files=self._paths(self.root,entry)
                    with _source_member(files[0]) as source:
                        with database.open("wb") as dst:shutil.copyfileobj(source,dst,1024*1024)
                        try:
                            with _source_member(files[1]) as metadata,manifest.open("wb") as dst:
                                shutil.copyfileobj(metadata,dst)
                        except FileNotFoundError:
                            if entry not in index["legacy"] or _inspect(database)["required_packages"]:raise
                            # Early snapshots of databases without a knowledge registry omitted this sidecar.
                            backup_manifest(database,include_empty=True)
                    info=self._verify(database,manifest,entry)
                except (FileNotFoundError,sqlite3.DatabaseError,KeyError,TypeError,ValueError):
                    if name:raise RuntimeErrorBase("backup_invalid") from None
                    continue
                break
            else:raise RuntimeErrorBase("backup_not_available")
            live=None
            try:
                with closing(sqlite3.connect((self.paths.home/"data/database/runtime.sqlite3").as_uri()+"?mode=ro",uri=True)) as db:
                    db.execute("BEGIN")
                    live={t:db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in info["counts"]}
            except (OSError,sqlite3.Error):pass
            result=info|{"file":entry["file"],"at":entry["at"],"live_counts":live}
            from .knowledge_revision_restore import reconcile
            overlay=reconcile(database,self.paths.home/"data/database/runtime.sqlite3",ConfigStore(self.paths))
            if overlay is not None:
                backup_manifest(database,include_empty=True)
                result.update(knowledge_reconciliation=overlay,snapshot_database_sha256=info['database_sha256'],
                              database_sha256=_digest(database))
            atomic_json(stage/"restore.json",result,durable=True)
            stage.rename(target)
            stage=None
            return result|{"directory":str(target)}
        finally:
            if stage is not None and stage.exists():
                for member in ("runtime.sqlite3","runtime.sqlite3.knowledge.json","restore.json"):
                    safe_path(stage,stage/member).unlink(missing_ok=True)
                if not any(stage.iterdir()):stage.rmdir()
