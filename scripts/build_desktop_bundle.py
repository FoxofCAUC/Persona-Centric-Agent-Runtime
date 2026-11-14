"""Build a local desktop source kit; business migrations are separate files."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import re
import tomllib
import zipfile

SOURCE=Path(__file__).resolve().parents[1]
SCRIPTS=('Start-Workbench.ps1','Prepare-Desktop.ps1','pcar-location.ps1','Manage-Migration.ps1',
         'dev.ps1','test.ps1','force_stop_services.py','force_stop_qq.py','build_desktop_bundle.py','pcar_acceptance_check.py')
ROOT_FILES=('启动PCAR.bat','README.md','AGENTS.md','AGENT_INSTALL.md','LICENSE','pyproject.toml','uv.lock','model-assets.json','source-delivery.json')
PUBLIC_FIXTURE_SHA256='6badfaccfed2a25626260e760f76a91b852ac7a99b2943cf4a6005e4fcbfa249'


def build(destination,model=None,*,release_tag=None,source_revision=None,artificial_fixture=None):
    destination=Path(destination).absolute()
    if destination.is_relative_to(SOURCE):raise ValueError('安装包必须保存在源码目录以外。')
    if source_revision is not None and not re.fullmatch('[0-9a-f]{40}',source_revision):raise ValueError('无效源码提交编号。')
    if release_tag and not (source_revision and model and artificial_fixture):raise ValueError('发行包需要提交编号、公开模型和人工演练资料。')
    files={p.relative_to(SOURCE).as_posix():p for folder in ('persona_runtime','tests') for p in (SOURCE/folder).rglob('*') if p.is_file() and p.suffix in ('.py','.txt','.json') and '__pycache__' not in p.parts}
    policy=json.loads((SOURCE/'source-delivery.json').read_text(encoding='utf-8'))
    for name in policy['excluded_modules']:files.pop(name,None)
    files.update({n:SOURCE/n for n in ROOT_FILES})
    files.update({'scripts/'+n:SOURCE/'scripts'/n for n in SCRIPTS})
    files.update({p.relative_to(SOURCE).as_posix():p for p in (SOURCE/'docs').rglob('*') if p.is_file() and p.suffix.lower() in ('.md','.docx','.txt')})
    if artificial_fixture:
        fixture=Path(artificial_fixture)
        with fixture.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=PUBLIC_FIXTURE_SHA256:raise ValueError('只允许固定的公开人工迁移包，不能发布用户业务包。')
        files['samples/artificial-business.pcar-migration.zip']=fixture
    if model:
        index=json.loads((SOURCE/'model-assets.json').read_text(encoding='utf-8'))['character_embedding']
        for name,expected in index['files'].items():
            p=Path(model)/name
            with p.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
            if actual!=expected:raise ValueError('公开模型文件不完整：'+name)
            files['public-model/'+name]=p
    manifest={'format':'pcar-desktop-kit-v1','business_data_included':False,'credentials_included':False,
              'runtime_version':tomllib.loads((SOURCE/'pyproject.toml').read_text(encoding='utf-8'))['project']['version'],
              'release_tag':release_tag,'source_revision':source_revision,'deployment_mode':'agent-assisted',
              'public_model_included':bool(model),'artificial_fixture_included':bool(artificial_fixture),
              'files':{n:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for n,p in sorted(files.items())}}
    destination.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation prevents accidentally replacing another delivery.
    with zipfile.ZipFile(destination,'x',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('desktop-kit.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        for name,p in files.items():archive.write(p,name)
    return {'path':str(destination),'files':len(files),'business_data_included':False,'public_model_included':bool(model),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest()}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--public-model',type=Path)
    parser.add_argument('--release-tag');parser.add_argument('--source-revision');parser.add_argument('--artificial-fixture',type=Path)
    args=parser.parse_args();print(json.dumps(build(args.output,args.public_model,release_tag=args.release_tag,source_revision=args.source_revision,artificial_fixture=args.artificial_fixture),ensure_ascii=False,indent=2))
