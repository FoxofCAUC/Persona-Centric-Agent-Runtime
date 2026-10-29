"""Local privacy/test policy, never a second authority for character canon.

No personal names, source text or source paths belong in this module or Git.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from .config import atomic_json
from .instance import InstanceLock, process_identity_alive
from .models import RuntimeErrorBase, identifier, timestamp


def sha(data):return hashlib.sha256(data).hexdigest()
def canonical(value):return sha(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
def provider_identity(model):return canonical({k:model.get(k) for k in ('base_url','name')})


def safe(path, parent=None):
    path=Path(path).absolute()
    for item in (path,*path.parents):
        if item.exists() and (item.is_symlink() or getattr(item.lstat(),'st_file_attributes',0)&0x400):
            raise RuntimeErrorBase('private_path_reparse_point')
    value=path.resolve()
    if parent is not None and (value==Path(parent).resolve() or not value.is_relative_to(Path(parent).resolve())):
        raise RuntimeErrorBase('private_path_outside_root')
    return value


class PrivatePersonas:
    def __init__(self,paths):
        self.paths=paths;self.root=safe(paths.home/'private/personas');self.file=self.root/'registry.json'

    def load(self,required=False):
        if not self.file.exists():
            if required:raise RuntimeErrorBase('private_registry_unavailable')
            return {'version':1,'records':[]}
        safe(self.file,self.root)
        try:value=json.loads(self.file.read_text(encoding='utf-8'))
        except (ValueError,OSError):raise RuntimeErrorBase('private_registry_unavailable') from None
        if value.get('version')!=1 or not isinstance(value.get('records'),list):
            raise RuntimeErrorBase('private_registry_invalid')
        return value

    def save_record(self,record):
        self.root.mkdir(parents=True,exist_ok=True)
        lock=InstanceLock(self.root/'registry.lock')
        try:
            data=self.load();data['records']=[r for r in data['records'] if r['id']!=record['id']]+[record]
            atomic_json(self.file,data)
        finally:lock.close()

    def record(self,private_id):
        matches=[r for r in self.load(True)['records'] if r['id']==private_id]
        if len(matches)!=1:raise RuntimeErrorBase('private_record_not_found')
        return matches[0]

    def package(self,record):
        p=safe(self.root/record['id']/'source-package.json',self.root)
        if sha(p.read_bytes())!=record['package_sha256']:raise RuntimeErrorBase('private_original_changed')
        return json.loads(p.read_text(encoding='utf-8-sig'))

    def register(self,package_path,archive_path,relationship):
        from .character import validate_character
        from .models import PersonaSpec
        raw=Path(package_path).read_bytes();archive=Path(archive_path).read_bytes()
        value=json.loads(raw.decode('utf-8-sig'))
        if set(value)!={'format','persona_spec','character'} or value['format']!='pcar_character_v1':
            raise RuntimeErrorBase('invalid_character_package')
        PersonaSpec.from_dict(value['persona_spec']);validate_character(value['character'])
        if not isinstance(relationship,str) or not relationship.strip():raise RuntimeErrorBase('private_relationship_required')
        for old in self.load()['records']:
            if old['package_sha256']==sha(raw) and old['archive_sha256']==sha(archive):
                if canonical(relationship)!=old['relationship_sha256']:raise RuntimeErrorBase('private_source_conflict')
                return old
        rid=identifier();folder=safe(self.root/rid,self.root);folder.mkdir(parents=True)
        (folder/'source-package.json').write_bytes(raw);(folder/'source-archive.md').write_bytes(archive)
        (folder/'initial-relationship.txt').write_text(relationship,encoding='utf-8')
        record={'id':rid,'persona_id':None,'state':'registered','registered_at':timestamp(),
            'package_sha256':sha(raw),'archive_sha256':sha(archive),'literary_sha256':canonical(value),
            'relationship_sha256':canonical(relationship),'cloud_permissions':{'main':'necessary_visible_only','selector':'necessary_visible_only'},
            'hidden_archive_injection':False,'acceptance_on_original':False,
            'deferred':['creator_only_canon','future_narrative','affect','reflection','director','evolution','visual_performance']}
        from .config import ConfigStore
        cfg=ConfigStore(self.paths).load()
        record['approved_models']={'main':provider_identity(cfg['model']),'selector':provider_identity(cfg['character_selector'])}
        self.save_record(record);return record

    def protected(self,persona_id):
        return bool(persona_id and self.record_for_persona(persona_id))

    def record_for_persona(self,persona_id):
        records=self.load()['records']
        direct=next((r for r in records if r.get('persona_id')==persona_id and persona_id),None)
        if direct:return direct
        from .storage import Store
        import sqlite3
        from contextlib import closing
        path=Store(self.paths).path
        if not path.exists():return None
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='persona_lifecycle'").fetchone():return None
            row=db.execute('SELECT privacy_root FROM persona_lifecycle WHERE persona_id=?',(persona_id,)).fetchone()
        if not row or not row[0]:return None
        record=next((r for r in records if r.get('persona_id')==row[0]),None)
        if not record:raise RuntimeErrorBase('private_registry_unavailable')
        return record

    def assert_acceptance(self,persona_id):
        if self.protected(persona_id):raise RuntimeErrorBase('private_persona_acceptance_forbidden')

    def import_original(self,store,private_id):
        from .character import CharacterService
        from .service import RuntimeService
        from .profile import ProfileService,head
        from .context_policy import initialize
        record=self.record(private_id);package=self.package(record)
        relationship=(self.root/private_id/'initial-relationship.txt').read_text(encoding='utf-8')
        if canonical(relationship)!=record['relationship_sha256']:raise RuntimeErrorBase('private_original_changed')
        service=RuntimeService(store)
        if record.get('state')=='ready':
            self.check(store,private_id);return record
        if not record.get('persona_id'):
            current=service.show_persona()['id']
            result=CharacterService(store).import_package(current,package,'private-original-'+private_id)
            # Persist protection immediately; never retry by creating a different ID.
            record.update(persona_id=result['id'],state='imported');self.save_record(record)
        pid=record['persona_id']
        if 'relationship_expected_version' not in record:
            with store.connection() as db:record['relationship_expected_version']=head(db,pid,'relationship')['id']
            self.save_record(record)
        with store.connection(write=True) as db:
            initialize(db,pid)
            ProfileService(store)._write(db,pid,'relationship',{'description':relationship},
                record['relationship_expected_version'],'private-relationship-'+private_id,actor='creator_import')
        self.check(store,private_id)
        record['state']='ready';self.save_record(record)
        return record

    def check(self,store,private_id,persona_id=None):
        from .service import RuntimeService
        from .character import CharacterService
        from .profile import head
        record=self.record(private_id);package=self.package(record);pid=persona_id or record.get('persona_id')
        if persona_id and self.record_for_persona(persona_id)!=record:raise RuntimeErrorBase('private_scope_mismatch')
        archive=safe(self.root/private_id/'source-archive.md',self.root)
        if sha(archive.read_bytes())!=record['archive_sha256']:raise RuntimeErrorBase('private_original_changed')
        if not pid:raise RuntimeErrorBase('private_persona_not_imported')
        actual={'format':'pcar_character_v1','persona_spec':RuntimeService(store).show_persona(pid)['spec'],
            'character':CharacterService(store).current(pid)['data']}
        with store.connection() as db:relationship=head(db,pid,'relationship')['data']['description']
        ok=canonical(actual)==record['literary_sha256'] and canonical(relationship)==record['relationship_sha256']
        if not ok:raise RuntimeErrorBase('private_literary_content_changed')
        return {'private':True,'literary_unchanged':True,'original_bytes_unchanged':True,'deferred_capabilities':record['deferred']}

    def create_sandbox(self,private_id):
        from .paths import RuntimePaths
        from .storage import Store
        from .service import RuntimeService
        from .character import CharacterService
        from .profile import ProfileService,head
        record=self.record(private_id);package=self.package(record)
        root=safe(self.paths.home/'tmp/private-acceptance');root.mkdir(parents=True,exist_ok=True)
        folder=safe(root/identifier(),root);paths=RuntimePaths(folder);store=Store(paths);store.initialize()
        from .models import generic_persona
        service=RuntimeService(store);current=service.create_persona(generic_persona())['id'];service.use_persona(current)
        result=CharacterService(store).import_package(current,package,identifier());service.use_persona(result['id'])
        relationship=(self.root/private_id/'initial-relationship.txt').read_text(encoding='utf-8')
        with store.connection(write=True) as db:
            ProfileService(store)._write(db,result['id'],'relationship',{'description':relationship},head(db,result['id'],'relationship')['id'],identifier(),actor='creator_copy')
        atomic_json(folder/'private-sandbox.json',{'private_id':private_id,'persona_id':result['id'],'created_at':timestamp(),'formal_home':str(self.paths.home.resolve())})
        atomic_json(folder/'runtime/private-copy-policy.json',{'persona_id':result['id'],'approved_models':record['approved_models']})
        return {'home':str(folder),'persona_id':result['id'],'private_id':private_id}

    def cleanup_sandbox(self,folder):
        root=safe(self.paths.home/'tmp/private-acceptance');folder=safe(folder,root)
        marker=json.loads((folder/'private-sandbox.json').read_text(encoding='utf-8'))
        self.record(marker['private_id'])
        if marker['formal_home']!=str(self.paths.home.resolve()):raise RuntimeErrorBase('private_cleanup_scope_conflict')
        for path in folder.rglob('*'):safe(path,folder)
        lease=folder/'runtime/console.json'
        if lease.exists() and process_identity_alive(json.loads(lease.read_text())['process']):raise RuntimeErrorBase('private_sandbox_still_running')
        locks=[]
        try:
            for name in ('desktop','console','service'):locks.append(InstanceLock(folder/'runtime'/f'{name}.lock'))
        finally:
            for lock in locks:lock.close()
        shutil.rmtree(folder)
        return {'local_copy_deleted':not folder.exists(),'original_unchanged':True}


def guard_acceptance_write(store,persona_id):
    from .qq_testing import QQTesting
    if QQTesting(store.paths).active():PrivatePersonas(store.paths).assert_acceptance(persona_id)
    journal=store.paths.home/'runtime/acceptance-quotas.json'
    if journal.exists():
        try:active=json.loads(journal.read_text(encoding='utf-8')).get('status')=='active'
        except (ValueError,OSError):raise RuntimeErrorBase('acceptance_state_unavailable') from None
        if active:PrivatePersonas(store.paths).assert_acceptance(persona_id)


def guard_cloud_dispatch(store,persona_id,kind,model):
    policy=PrivatePersonas(store.paths).record_for_persona(persona_id)
    copied=store.paths.home/'runtime/private-copy-policy.json'
    if policy is None and copied.exists():
        candidate=json.loads(copied.read_text(encoding='utf-8'))
        if candidate.get('persona_id')==persona_id:policy=candidate
    if policy is not None and policy.get('approved_models',{}).get(kind)!=provider_identity(model):
        raise RuntimeErrorBase('private_cloud_provider_not_authorized')


def publication_scan(paths,repo,*,staged=False,base=None):
    """Inspect local files/index/outgoing blobs; never include matches in output."""
    repo=Path(repo).resolve();registry=PrivatePersonas(paths);records=registry.load(True)['records']
    if not records:raise RuntimeErrorBase('private_registry_unavailable')
    needles=set();original_hashes=set()
    for record in records:
        package=registry.package(record);folder=registry.root/record['id']
        archive=(folder/'source-archive.md').read_bytes()
        if sha(archive)!=record['archive_sha256']:raise RuntimeErrorBase('private_original_changed')
        original_hashes.update((record['package_sha256'],record['archive_sha256']))
        def visit(value):
            if isinstance(value,str):
                if len(value)>=24:
                    needles.add(value.encode())
                    for i in range(0,max(1,len(value)-47),24):needles.add(value[i:i+48].encode())
            elif isinstance(value,dict):
                for v in value.values():visit(v)
            elif isinstance(value,list):
                for v in value:visit(v)
        visit(package);visit((folder/'initial-relationship.txt').read_text(encoding='utf-8'))
        needles.add(package['persona_spec']['name'].encode())
        for line in archive.decode('utf-8-sig').splitlines():
            if len(line)>36:
                for i in range(0,len(line)-35,32):needles.add(line[i:i+64].encode())
    hits=[];checked=0
    def check(name,data):
        nonlocal checked
        checked+=1
        if (name.replace('\\','/').split(':')[-1].startswith('private/') or Path(name).suffix.lower() in ('.sqlite','.sqlite3','.db','.onnx','.png','.jpg','.zip','.safetensors')
                or b'\x00' in data or data.startswith((b'\x89PNG\r\n',b'\xff\xd8\xff',b'PK\x03\x04'))
                or sha(data) in original_hashes or any(n in data for n in needles)):
            hits.append({'location':name,'reason':'private_or_runtime_content'})
    def git(*args):return subprocess.check_output(['git',*args],cwd=repo)
    names=set(git('ls-files','-z').decode().split('\0'))-{''}
    if staged:
        for name in sorted(names):
            try:check('index:'+name,git('show',':'+name))
            except subprocess.CalledProcessError:continue
    else:
        names|=set(git('ls-files','--others','--exclude-standard','-z').decode().split('\0'))-{''}
        for name in sorted(names):
            if (repo/name).is_file():check(name,(repo/name).read_bytes())
    if base:
        commits=git('rev-list',base+'..HEAD').decode().splitlines();seen=set()
        for commit in commits:
            for item in git('ls-tree','-rz',commit).split(b'\0'):
                if not item:continue
                meta,name=item.split(b'\t',1);blob=meta.split()[-1].decode()
                if blob in seen:continue
                seen.add(blob);check(commit[:12]+':'+name.decode(),git('cat-file','blob',blob))
    return {'passed':not hits,'checked_blobs':checked,'hits':hits}


def install_hook(paths,repo):
    import shlex
    import sys
    repo=Path(repo).resolve()
    gitdir=Path(subprocess.check_output(['git','rev-parse','--git-path','hooks'],cwd=repo).decode().strip())
    if not gitdir.is_absolute():gitdir=repo/gitdir
    local_git=Path(subprocess.check_output(['git','rev-parse','--absolute-git-dir'],cwd=repo).decode().strip()).resolve()
    external_hook=None
    if not gitdir.resolve().is_relative_to(local_git):
        external_hook=gitdir/'pre-push';gitdir=local_git/'pcar-private-hooks'
        subprocess.run(['git','config','--local','core.hooksPath',str(gitdir)],cwd=repo,check=True)
    gitdir.mkdir(parents=True,exist_ok=True);hook=gitdir/'pre-push';marker='# PCAR private publication guard'
    previous=None;meta=gitdir/'pcar-private-guard.json'
    if external_hook and external_hook.exists():
        previous=gitdir/('pre-push.before-private-'+identifier());shutil.copy2(external_hook,previous)
    if hook.exists() and marker in hook.read_text(encoding='utf-8') and meta.exists():
        saved=json.loads(meta.read_text(encoding='utf-8'));previous=Path(saved['previous']) if saved.get('previous') else None
    if hook.exists() and marker not in hook.read_text(encoding='utf-8'):
        previous=gitdir/('pre-push.before-private-'+identifier());shutil.copy2(hook,previous)
    q=lambda x:shlex.quote(str(x).replace('\\','/'))
    code='#!/bin/sh\n'+marker+'\ninput=$(cat)\nprintf "%s\\n" "$input" | '+q(sys.executable)+' -B '+q(repo/'scripts/private_publication_guard.py')+' --repo '+q(repo)+' --home '+q(paths.home)+' --outgoing\nstatus=$?\n[ "$status" -eq 0 ] || exit "$status"\n'
    if previous:code+='printf "%s\\n" "$input" | '+q(previous)+' "$@"\n'
    hook.write_text(code,encoding='utf-8');hook.chmod(0o755)
    atomic_json(meta,{'previous':str(previous) if previous else None})
    return {'installed':True,'previous_hook_preserved':bool(previous)}


def main():
    from .paths import RuntimePaths
    from .storage import Store
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--home',type=Path)
    parser.add_argument('action',choices=('register','status','check','import','clone','cleanup','scan','install-hook'))
    parser.add_argument('--id');parser.add_argument('--path',type=Path);parser.add_argument('--repo',type=Path);parser.add_argument('--base')
    parser.add_argument('--package',type=Path);parser.add_argument('--archive',type=Path);parser.add_argument('--relationship-file',type=Path)
    args=parser.parse_args();paths=RuntimePaths(args.home.resolve()) if args.home else RuntimePaths.from_environment();private=PrivatePersonas(paths)
    if args.action=='register':result=private.register(args.package,args.archive,args.relationship_file.read_text(encoding='utf-8'))
    elif args.action=='status':result={'registered':len(private.load()['records']),'protected':sum(bool(r.get('persona_id')) for r in private.load()['records'])}
    elif args.action=='check':result=private.check(Store(paths),args.id)
    elif args.action=='import':result=private.import_original(Store(paths),args.id)
    elif args.action=='clone':result=private.create_sandbox(args.id)
    elif args.action=='cleanup':result=private.cleanup_sandbox(args.path)
    elif args.action=='scan':result=publication_scan(paths,args.repo,base=args.base)
    else:result=install_hook(paths,args.repo)
    print(json.dumps(result,ensure_ascii=False))
    if result.get('passed') is False:raise SystemExit(1)


if __name__=='__main__':main()
