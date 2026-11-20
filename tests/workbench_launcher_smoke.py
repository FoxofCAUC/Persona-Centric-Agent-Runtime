"""Exercise the real Tk and WebView BAT launchers against an isolated uv cache."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib
from unittest.mock import patch

from persona_runtime import installation
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.lifecycle import ServiceController,read_console,require_console
from persona_runtime.models import PersonaSpec,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


def wait(predicate,seconds=45):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        result=predicate()
        if result:return result
        time.sleep(.1)
    raise AssertionError('launcher condition timed out at '+str(predicate.__code__.co_firstlineno))


def prepare_cache(source,original,destination):
    with (source/'uv.lock').open('rb') as f:packages={v['name']:v for v in tomllib.load(f)['package']}
    names=set();todo=['pywebview','certifi','aiohttp']
    while todo:
        key=todo.pop()
        if key not in names:
            names.add(key);todo.extend(d['name'] for d in packages[key].get('dependencies',()))
    names.update(('setuptools','wheel','packaging'))
    for key in names:
        for part in ('sdists-v10/pypi','wheels-v7/pypi'):
            old=original/part/key
            if old.is_dir():shutil.copytree(old,destination/part/key)
        for record in (original/'simple-v27/pypi').glob(key+'.*'):
            dest=destination/'simple-v27/pypi'/record.name
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(record,dest)
    for old in (original/'archive-v0').iterdir():
        if old.is_dir() and any(d.name.split('-')[0].replace('_','-') in names for d in old.glob('*.dist-info')):
            shutil.copytree(old,destination/'archive-v0'/old.name)


def run(root,cache,legacy_source=None):
    if root.exists():raise ValueError('验收需要新的隔离目录。')
    root.mkdir(parents=True);source=root/'program';source.mkdir()
    for name in ('uv.lock','pyproject.toml','model-assets.json','启动PCAR.bat','启动PCAR-新版.bat'):
        shutil.copyfile(installation.SOURCE_ROOT/name,source/name)
    for name in ('persona_runtime','scripts'):
        shutil.copytree(installation.SOURCE_ROOT/name,source/name,ignore=shutil.ignore_patterns('__pycache__'))
    legacy=source
    if legacy_source:
        legacy=root/'v0.2';legacy.mkdir()
        for name in ('uv.lock','pyproject.toml','model-assets.json','启动PCAR.bat'):
            shutil.copyfile(legacy_source/name,legacy/name)
        for name in ('persona_runtime','scripts'):
            shutil.copytree(legacy_source/name,legacy/name,ignore=shutil.ignore_patterns('__pycache__'))
    paths=RuntimePaths(root/'旧实例');config=ConfigStore(paths);config.initialize()
    from tests.workbench_daily_cycle import port
    config.update({'service_port':port()})
    store=Store(paths);store.initialize();service=RuntimeService(store)
    pid=service.create_persona(PersonaSpec(1,'661','661',['661'],'661',['661'],'661'))['id'];service.use_persona(pid)
    prepare_cache(source,cache,paths.home/'cache/uv')
    python=paths.home/'.venv/Scripts/python.exe'
    env=installation.child_environment(paths,source_root=source)|{'UV_OFFLINE':'1','PCAR_PYTHON':str(python)}
    def command(args,seconds=90,*,check=True,cwd=source):
        r=subprocess.run(args,env=env,cwd=cwd,capture_output=True,timeout=seconds,creationflags=subprocess.CREATE_NO_WINDOW)
        if check and r.returncode:raise AssertionError((r.returncode,r.stderr.decode('utf8',errors='replace')[-400:]))
        return r
    command(['uv','sync','--offline','--frozen','--no-dev','--no-install-project','--no-managed-python',
             '--python',str(Path(sys.base_prefix)/'python.exe'),'--link-mode','copy'])
    controller=ServiceController(paths)
    def start(name,program=source):
        return subprocess.Popen(['cmd.exe','/d','/c',name],cwd=program,env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def live():
        try:return require_console(paths)
        except ValueError:return None
    def stop():
        owner=live()
        if owner:
            atomic_json(paths.home/'runtime/wakeup.json',{'token':owner['token'],'id':identifier(),'action':'exit'})
            wait(lambda:(read_console(paths) or {}).get('state')=='closed' and not controller.has_resident_processes(),65)
    try:
        first=start('启动PCAR.bat',legacy);assert first.wait(40)==0
        lease=wait(live);wait(lambda:controller.health('service')['state']=='运行')
        second=start('启动PCAR-新版.bat');assert second.wait(20)==0
        assert live()['token']==lease['token']
        assert json.loads((paths.home/'runtime/wakeup.json').read_text())['token']==lease['token']
        assert not (paths.home/'.venv/pcar-webview').exists()
        stop()
        with store.path.open('rb') as stream:db_hash=hashlib.file_digest(stream,'sha256').hexdigest()
        cfg_bytes=config.file.read_bytes()
        changed={}
        for file in (paths.home/'cache/uv').rglob('__init__.py'):
            if file.parent.name=='webview':
                assert file.resolve().is_relative_to(paths.home)
                changed[file]=file.read_bytes()
                file.write_bytes(changed[file]+b'\n# 667\n')
        assert changed
        args=[str(python),'-B','-m','persona_runtime','--json','--home',str(paths.home),'prepare','--register-existing','--frontend','webview']
        bad=command(args,check=False)
        assert bad.returncode and not (paths.home/'.venv/Lib/site-packages/pcar-managed-webview.pth').exists()
        command([str(python),'-I','-B','-c','import tkinter,aiohttp;tkinter.Tcl()'])
        for file,original in changed.items():file.write_bytes(original)
        # Restore cached metadata invalidated by uv's failed offline refresh.
        fresh_cache=paths.home/'cache/uv'
        fresh_cache.rename(paths.home/'cache/corrupt-fixture')
        prepare_cache(source,cache,fresh_cache)
        crashed=command([sys.executable,'-B','-m','tests.workbench_launcher_smoke','--interrupt',str(paths.home),str(source)],
                        check=False,cwd=installation.SOURCE_ROOT)
        assert crashed.returncode==74,(crashed.returncode,crashed.stderr[-300:])
        assert config.file.read_bytes()==cfg_bytes
        with store.path.open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==db_hash
        command([str(python),'-I','-B','-c','import tkinter,aiohttp;tkinter.Tcl()'])
        one=start('启动PCAR-新版.bat');two=start('启动PCAR-新版.bat')
        assert one.wait(95)==0 and two.wait(95)==0
        current=wait(live)
        wait(lambda:controller.health('service')['state']=='运行')
        assert current['token']!=lease['token']
        command([str(python),'-I','-B','-c','from webview.platforms import winforms;import tkinter,aiohttp;tkinter.Tcl()'])
        assert not (paths.home/'runtime/webview-install.json').exists()
        with store.connection() as db:assert db.execute('SELECT id FROM personas').fetchone()[0]==pid
        stop()
        if legacy_source:
            reader=start('启动PCAR.bat',legacy);assert reader.wait(40)==0
            wait(live);wait(lambda:controller.health('service')['state']=='运行')
            assert controller.client.manage('persona.list')[0]['id']==pid
            stop()
        print(json.dumps({'bat_launchers':2,'old_window_woken':True,'concurrent_webview_launches':2,
            'managed_frontend_verified':True,'interrupted_install':74,'tk_ready_after_failure':True,
            'business_unchanged_during_install':True,'corrupt_cache_rejected':True,
            'released_tk_roundtrip':bool(legacy_source),'real_model_calls':0}))
    finally:stop()


if __name__=='__main__':
    if sys.argv[1]=='--interrupt':
        paths=RuntimePaths(Path(sys.argv[2]))
        with patch.object(installation,'_write_pointer',side_effect=lambda *_:os._exit(74)):
            installation.register_existing(paths,source_root=Path(sys.argv[3]),frontend='webview')
    else:run(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve(),Path(sys.argv[3]).resolve() if len(sys.argv)>3 else None)
