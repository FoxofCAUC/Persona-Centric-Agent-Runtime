"""Local desktop installation and diagnosis; no credentials or paid model calls."""
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid

SOURCE_ROOT=Path(__file__).resolve().parents[1]
LOCATOR='.pcar-local.json'
NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def resolve_home(explicit=None,*,source_root=None,environ=None,required=True):
    source=Path(source_root or SOURCE_ROOT).resolve();env=os.environ if environ is None else environ
    value=explicit or env.get('PERSONA_RUNTIME_HOME')
    if not value and (source/LOCATOR).exists():
        data=read_json(source/LOCATOR)
        if data.get('version')!=1:raise ValueError('本机启动定位配置版本不兼容，请重新准备环境。')
        value=data.get('runtime_home')
    if not value:
        if required:raise ValueError('尚未选择数据目录，请从原桌面入口准备环境，或显式指定 --home。')
        return None
    path=Path(value)
    if not path.is_absolute():raise ValueError('数据目录必须是绝对路径。')
    return validate_home(path,source_root=source)


def validate_home(home,*,source_root=None):
    raw=Path(home)
    if not raw.is_absolute() or '..' in raw.parts:raise ValueError('数据目录必须是规范的绝对路径。')
    path=raw.absolute();source=Path(source_root or SOURCE_ROOT).resolve()
    if not path.is_absolute() or path==Path(path.anchor) or str(path).startswith('\\\\'):
        raise ValueError('请选择本地磁盘中的专用数据目录。')
    if path.is_relative_to(source) or source.is_relative_to(path):
        raise ValueError('源码目录与数据目录不能相互包含，请选择独立目录。')
    for node in (path,*path.parents):
        if node.exists() and (node.is_symlink() or node.is_junction()):raise ValueError('数据目录不能包含链接或重解析目录。')
    if path.exists() and not path.is_dir():raise ValueError('数据目录指向了文件。')
    return path


def runtime_python(paths,*,environ=None):
    env=os.environ if environ is None else environ
    value=env.get('PCAR_PYTHON')
    path=Path(value) if value else paths.home/'.venv/Scripts/python.exe'
    if not path.is_absolute() or not path.is_file():raise ValueError('当前实例解释器未准备，请运行桌面环境准备。')
    return path


def child_environment(paths,*,source_root=None):
    source=Path(source_root or SOURCE_ROOT).resolve();env=os.environ.copy();home=paths.home
    env.update(PERSONA_RUNTIME_HOME=str(home),PYTHONPATH=str(source),PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',PYTHONIOENCODING='utf-8',
        UV_PROJECT_ENVIRONMENT=str(home/'.venv'),UV_CACHE_DIR=str(home/'cache/uv'),PIP_CACHE_DIR=str(home/'cache/pip'),
        PYTHONPYCACHEPREFIX=str(home/'cache/pycache'),TEMP=str(home/'tmp'),TMP=str(home/'tmp'),
        HF_HOME=str(home/'cache/huggingface'),XDG_CACHE_HOME=str(home/'cache/xdg'))
    return env


@contextmanager
def activated(paths):
    values=child_environment(paths);keys=('PERSONA_RUNTIME_HOME','PYTHONPATH','PYTHONDONTWRITEBYTECODE','PYTHONUTF8','PYTHONIOENCODING',
        'UV_PROJECT_ENVIRONMENT','UV_CACHE_DIR','PIP_CACHE_DIR','PYTHONPYCACHEPREFIX','TEMP','TMP','HF_HOME','XDG_CACHE_HOME')
    before={k:os.environ.get(k) for k in keys}
    try:
        for k in keys:os.environ[k]=values[k]
        yield
    finally:
        for k,v in before.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v


def provisioning_environment(paths):
    from .network_proxy import configured_proxy
    proxy=configured_proxy('https');http=configured_proxy('http')
    if not proxy or not http:raise ValueError('公共下载需要已开启的Windows手动HTTP/HTTPS系统代理；不会回退直连。')
    env=child_environment(paths)
    env.update(HTTP_PROXY=http,HTTPS_PROXY=proxy,ALL_PROXY=proxy,http_proxy=http,https_proxy=proxy,all_proxy=proxy,NO_PROXY='localhost,127.0.0.1,::1',no_proxy='localhost,127.0.0.1,::1',UV_PYTHON_DOWNLOADS='never')
    return env


def register_existing(paths,*,source_root=None):
    source=Path(source_root or SOURCE_ROOT).resolve();validate_home(paths.home,source_root=source)
    file=paths.home/'config/workbench.json'
    if not file.is_file():raise ValueError('该目录没有可识别的PCAR实例，未登记。')
    data=read_json(file)
    if data.get('version')!=3 or not data.get('instance_id'):raise ValueError('实例配置不兼容，未登记。')
    runtime_python(paths)
    atomic_json(source/LOCATOR,{'version':1,'runtime_home':str(paths.home)})
    return {'registered':True,'runtime_home':str(paths.home),'business_data_changed':False}


def model_identity(paths,*,source_root=None):
    from .character_embedding import CharacterEmbeddingProvider
    index=read_json(Path(source_root or SOURCE_ROOT)/'model-assets.json')['character_embedding']
    return CharacterEmbeddingProvider(paths,paths.home/'models/character-embedding'/index['fingerprint']).identity()


def prepare_model(paths,*,local_source=None,source_root=None):
    from .character_embedding import private_directory,file_hash,CharacterEmbeddingProvider
    source=Path(source_root or SOURCE_ROOT);index=read_json(source/'model-assets.json')['character_embedding']
    root=private_directory(paths.home,paths.home/'models/character-embedding');destination=root/index['fingerprint']
    if destination.exists():return {'reused':True,'identity':CharacterEmbeddingProvider(paths,destination).identity()}
    staging=root/('preparing-'+uuid.uuid4().hex);staging.mkdir()
    try:
        for name,expected in index['files'].items():
            if Path(name).name!=name:raise ValueError('模型清单包含无效路径。')
            target=staging/name
            if local_source:shutil.copyfile(Path(local_source)/name,target)
            else:
                from .network_proxy import system_urlopen
                url='https://huggingface.co/'+index['artifact_repository']+'/resolve/'+index['revision']+'/'+name
                with system_urlopen(url,timeout=120) as response,target.open('wb') as output:shutil.copyfileobj(response,output)
            if file_hash(target)!=expected:raise ValueError('模型文件不完整：'+name)
        atomic_json(staging/'manifest.json',index);os.replace(staging,destination)
        return {'reused':False,'identity':CharacterEmbeddingProvider(paths,destination).identity()}
    finally:
        if staging.exists():
            if staging.resolve().parent!=root.resolve() or staging.is_symlink() or staging.is_junction():raise ValueError('模型临时目录归属异常。')
            shutil.rmtree(staging)


def prepare_desktop(paths,*,base_python=None,local_model_source=None,diagnostics=False,source_root=None,register=True):
    from .paths import RuntimePaths
    source=Path(source_root or SOURCE_ROOT).resolve();home=validate_home(paths.home,source_root=source)
    if home.exists() and any(home.iterdir()) and not (home/'config/workbench.json').exists() and not (home/'runtime/install-progress.json').exists():
        raise ValueError('目标目录非空且不是已识别实例，请选择新的专用目录。')
    if (home/'config/workbench.json').exists():
        # Existing production environments require explicit registration, not an
        # implicit uv sync that could change the environment beneath a live app.
        raise ValueError('该目录已存在实例。保留原环境可使用 --register-existing；新部署请选择空目录。')
    from .character_embedding import private_directory
    paths.initialize();private_directory(home,home)
    atomic_json(home/'runtime/install-progress.json',{'version':1,'state':'preparing'})
    env=provisioning_environment(paths);uv=shutil.which('uv')
    if not uv:raise ValueError('缺少uv，请先按部署说明安装uv。')
    python=Path(base_python) if base_python else Path(sys.base_prefix)/'python.exe'
    probe=subprocess.run([str(python),'-I','-B','-c','import sys,struct,tkinter,sqlite3,ssl;print(str(sys.version_info.major)+"."+str(sys.version_info.minor)+"/"+str(struct.calcsize("P")*8))'],capture_output=True,text=True,creationflags=NO_WINDOW)
    if probe.returncode or probe.stdout.strip()!='3.12/64':raise ValueError('需要含Tkinter的Python 3.12 64位。')
    venv=home/'.venv'
    commands=[]
    if (venv/'Scripts/python.exe').exists():
        check=subprocess.run([str(venv/'Scripts/python.exe'),'-I','-B','-c','import sys,struct;print(str(sys.version_info.major)+"."+str(sys.version_info.minor)+"/"+str(struct.calcsize("P")*8))'],capture_output=True,text=True,creationflags=NO_WINDOW)
        if check.returncode or check.stdout.strip()!='3.12/64':raise ValueError('未完成安装的虚拟环境不可用，请选择新的空目录重新准备。')
    if not (venv/'Scripts/python.exe').exists():commands.append([uv,'venv','--no-managed-python','--python',str(python),str(venv)])
    commands.append([uv,'sync','--frozen','--no-managed-python','--extra','character-semantic']+(['--group','dev'] if diagnostics else ['--no-dev']))
    log=home/'logs/environment-prepare.log'
    with log.open('a',encoding='utf-8') as output:
        for command in commands:
            result=subprocess.run(command,cwd=source,env=env,stdout=output,stderr=subprocess.STDOUT,creationflags=NO_WINDOW)
            if result.returncode:raise ValueError('依赖准备未完成，日志位于 '+str(log))
    interpreter=venv/'Scripts/python.exe'
    arguments=[str(interpreter),'-B','-m','persona_runtime','--home',str(home),'prepare-model']
    if local_model_source:arguments+=['--local-source',str(Path(local_model_source).absolute())]
    with log.open('a',encoding='utf-8') as output:
        result=subprocess.run(arguments,cwd=source,env=env,stdout=output,stderr=subprocess.STDOUT,creationflags=NO_WINDOW)
    if result.returncode:raise ValueError('模型准备未完成，日志位于 '+str(log))
    # Creating the new blank configuration is the last step. Failed downloads
    # leave a resumable installation marker and never modify business data.
    from .config import ConfigStore
    ConfigStore(paths).initialize()
    atomic_json(home/'runtime/installation.json',{'version':1,'state':'ready','source_root':str(source),'runtime_home':str(home),'python':'3.12','desktop_only':True})
    (home/'runtime/install-progress.json').unlink(missing_ok=True)
    if register:register_existing(paths,source_root=source)
    return {'prepared':True,'runtime_home':str(home),'python':str(interpreter),'paid_calls':0}


def doctor(home=None,*,network=False,source_root=None):
    source=Path(source_root or SOURCE_ROOT).resolve();checks=[]
    checks.append({'name':'platform','ready':os.name=='nt' and platform.machine().upper() in ('AMD64','X86_64'),'actual':platform.platform()})
    checks.append({'name':'python','ready':sys.version_info[:2]==(3,12) and sys.maxsize>2**32,'actual':platform.python_version()})
    for package in ('aiohttp','certifi','fastembed','numpy','onnxruntime','tokenizers'):
        try:value=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:value=None
        expected={'aiohttp':'3.14.5','certifi':'2026.10.23'}|read_json(source/'model-assets.json')['character_embedding']['packages']
        checks.append({'name':package,'ready':value==expected[package],'version':value,'expected':expected[package]})
    try:
        import tkinter
        tcl=tkinter.Tcl();checks.append({'name':'tkinter','ready':True,'version':tcl.eval('info patchlevel')})
    except Exception:checks.append({'name':'tkinter','ready':False})
    from .network_proxy import configured_proxy
    checks.append({'name':'system_proxy','ready':bool(configured_proxy('http') and configured_proxy('https'))})
    if home is not None:
        from .paths import RuntimePaths
        paths=RuntimePaths(Path(home));checks.append({'name':'runtime_home','ready':paths.home.is_dir(),'path':str(paths.home)})
        try:runtime_python(paths);checks.append({'name':'project_python','ready':True})
        except ValueError:checks.append({'name':'project_python','ready':False})
        try:model_identity(paths,source_root=source);checks.append({'name':'character_model','ready':True})
        except Exception:checks.append({'name':'character_model','ready':False})
        config=paths.home/'config/workbench.json'
        checks.append({'name':'configuration','ready':config.is_file()})
        if config.exists():
            try:
                value=read_json(config);port=value['service_port'];available=False
                with socket.socket() as probe:
                    try:probe.bind(('127.0.0.1',port));available=True
                    except OSError:pass
                checks.append({'name':'service_port','ready':available,'port':port})
            except (OSError,ValueError,KeyError):checks.append({'name':'configuration','ready':False})
        from .migration import migration_status
        migration=migration_status(paths)
    else:
        migration=None
        checks.append({'name':'runtime_home','ready':False})
    if network:
        from .network_proxy import system_urlopen
        try:
            with system_urlopen('https://pypi.org/simple/aiohttp/',timeout=20) as response:ok=response.status==200
            checks.append({'name':'public_download_route','ready':ok,'network_route':'system_proxy'})
        except Exception:checks.append({'name':'public_download_route','ready':False,'network_route':'system_proxy_required'})
    return {'ready':all(x['ready'] for x in checks),'checks':checks,'migration':migration,'read_only':True,'paid_calls':0}


def configure_model(paths,role,*,base_url=None,name=None,key_stdin=False):
    """Secrets are read from stdin/getpass, never command-line arguments."""
    import getpass
    from .config import ConfigStore
    from .security import protect
    from .character_embedding import private_directory
    secret=sys.stdin.readline().rstrip('\r\n') if key_stdin else getpass.getpass('API Key（隐藏输入）：')
    if not secret:raise ValueError('没有输入密钥，配置未改变。')
    cfg=ConfigStore(paths);cfg.initialize()
    if role=='jev':
        from urllib.parse import urlsplit
        file=paths.home/'private/knowledge-router/config.json';old=read_json(file) if file.exists() else {}
        url=base_url or old.get('base_url');model=name or old.get('name')
        parsed=urlsplit(url or '')
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not model:
            raise ValueError('请指定Jev的HTTPS接口地址及模型名。')
        private_directory(paths.home,file.parent);atomic_json(file,{'base_url':url,'name':model,'encrypted_key':protect(secret)})
    else:
        field='model' if role=='main' else 'character_selector' if role=='selector' else 'organization'
        update={'api_key':secret}
        if base_url is not None:update['base_url']=base_url
        if name is not None:update['name']=name
        cfg.update({field:{'model':update} if role=='organization' else update})
    secret=''
    return {'configured':True,'role':role,'secret_echoed':False,'paid_calls':0}
