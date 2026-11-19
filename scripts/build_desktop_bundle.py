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
         'dev.ps1','test.ps1','force_stop_services.py','force_stop_qq.py','build_desktop_bundle.py','pcar_acceptance_check.py','pcar_upgrade_check.py','pcar_recall_rollback.py','pcar_recall_preflight.py','pcar_scene_voice.py')
ROOT_FILES=('启动PCAR.bat','README.md','AGENTS.md','AGENT_INSTALL.md','LICENSE','pyproject.toml','uv.lock','model-assets.json','source-delivery.json')
WEBVIEW_FILES = (
    '启动PCAR-新版.bat', 'persona_runtime/workbench_web/index.html',
    'persona_runtime/workbench_web/desktop.js', 'persona_runtime/workbench_web/persona_import.js',
)
SCENE_DOCS=('docs/acceptance/工作台双人私聊导入与历史接续.md','docs/expression/E.N6-场景发言接入与回退.md','docs/模型输入与提示词写作总则.md',
            'docs/第三方角色Embedding依赖与许可.md','docs/M9R.5-本机验收与候选交付.md')
SCENE_TESTS=('test_import_personas.py','test_scene_voice_delivery.py','test_tk_notice.py','test_m9r_native_retrieval.py',
             'test_m9r0_native_contract.py','test_general_retrieval.py','test_m4.py')
PUBLIC_FIXTURE_SHA256={
    'v0.1':'6badfaccfed2a25626260e760f76a91b852ac7a99b2943cf4a6005e4fcbfa249',
    'v0.2':'93f4faae190d1874f7b59ee08d444705fd1e1979451859363ab0aa4a17777ec1',
}
README_ASSETS=('ATTRIBUTION.md','provenance.json','superfish-vision.webp','superfish-vision.png',
               'superfish-vision.prompt.md','daily-life-vision.webp','daily-life-vision.png',
               'daily-life-vision.prompt.md','continuity-concept.png','continuity-concept.svg')


def build(destination,model=None,*,release_tag=None,source_revision=None,artificial_fixture=None,candidate_base_revision=None,profile='standard'):
    if profile not in ('standard','scene-desktop'):raise ValueError('安装包配置无效。')
    destination=Path(destination).absolute()
    if destination.is_relative_to(SOURCE):raise ValueError('安装包必须保存在源码目录以外。')
    if source_revision is not None and not re.fullmatch('[0-9a-f]{40}',source_revision):raise ValueError('无效源码提交编号。')
    if candidate_base_revision is not None and not re.fullmatch('[0-9a-f]{40}',candidate_base_revision):raise ValueError('无效候选基线编号。')
    if release_tag and not (source_revision and model and artificial_fixture):raise ValueError('发行包需要提交编号、公开模型和人工演练资料。')
    roots=('persona_runtime',) if profile=='scene-desktop' else ('persona_runtime','tests')
    files={p.relative_to(SOURCE).as_posix():p for folder in roots for p in (SOURCE/folder).rglob('*') if p.is_file() and p.suffix in ('.py','.txt','.json') and '__pycache__' not in p.parts}
    policy=json.loads((SOURCE/'source-delivery.json').read_text(encoding='utf-8'))
    for name in policy['excluded_modules']:files.pop(name,None)
    files.update({n:SOURCE/n for n in ROOT_FILES})
    files.update({n: SOURCE / n for n in WEBVIEW_FILES})
    files.update({'scripts/'+n:SOURCE/'scripts'/n for n in SCRIPTS})
    for name in ('__init__.py','m9r_comparison.py','m9r_comparison_materials.py','m9r_native_probe.py','m9r_fatigue_materials.py','recall_preparation.py','recall_continuity.py'):
        path=SOURCE/'experiments'/name
        if path.is_file():files['experiments/'+name]=path
    if profile=='scene-desktop':
        files.update({name:SOURCE/name for name in SCENE_DOCS if (SOURCE/name).is_file()})
        files.update({'tests/'+name:SOURCE/'tests'/name for name in SCENE_TESTS})
        documentation=SOURCE/'docs' if release_tag else SOURCE/'docs/licenses'
    else:
        documentation=SOURCE/'docs'
    files.update({p.relative_to(SOURCE).as_posix():p for p in documentation.rglob('*')
                  if p.is_file() and p.suffix.lower() in ('.md','.docx','.txt')})
    if release_tag:
        files.update({n:SOURCE/n for n in ('CHANGELOG.md','CONTRIBUTING.md')})
        files.update({'assets/readme/'+n:SOURCE/'assets/readme'/n for n in README_ASSETS})
    if artificial_fixture:
        fixture=Path(artificial_fixture)
        with fixture.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        allowed={PUBLIC_FIXTURE_SHA256.get(release_tag)} if release_tag else set(PUBLIC_FIXTURE_SHA256.values())
        if actual not in allowed:raise ValueError('只允许本版本核准的公开人工迁移包，不能发布用户业务包。')
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
              'release_tag':release_tag,'source_revision':source_revision,'schema_version':policy['schema_version'],
              'deployment_mode':'agent-assisted',
              'profile':profile,'public_model_included':bool(model),'artificial_fixture_included':bool(artificial_fixture),
              'files':{n:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for n,p in sorted(files.items())}}
    manifest['source_tree_sha256']=hashlib.sha256(json.dumps(manifest['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if candidate_base_revision:
        manifest['candidate_source']={'kind':'working_tree','base_revision':candidate_base_revision,
            'published':False,'commit_identity_claimed':False}
    destination.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation prevents accidentally replacing another delivery.
    with zipfile.ZipFile(destination,'x',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('desktop-kit.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        for name,p in files.items():archive.write(p,name)
    return {'path':str(destination),'files':len(files),'business_data_included':False,'public_model_included':bool(model),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest()}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--public-model',type=Path)
    parser.add_argument('--release-tag');parser.add_argument('--source-revision');parser.add_argument('--artificial-fixture',type=Path)
    parser.add_argument('--candidate-base-revision')
    parser.add_argument('--profile',choices=('standard','scene-desktop'),default='standard')
    args=parser.parse_args();print(json.dumps(build(args.output,args.public_model,release_tag=args.release_tag,source_revision=args.source_revision,artificial_fixture=args.artificial_fixture,candidate_base_revision=args.candidate_base_revision,profile=args.profile),ensure_ascii=False,indent=2))
