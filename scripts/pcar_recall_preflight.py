"""Read-only recall .3 readiness report. No migration, service or network calls."""
import argparse,json,sqlite3,sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from persona_runtime import __version__
from persona_runtime.storage import SCHEMA_VERSION
from persona_runtime.network_proxy import configured_proxy
from persona_runtime.provider_policy import require_generation_provider

def read_json(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}

def database_info(home,*,prepared=False):
    path=home/'data/database/runtime.sqlite3'
    if not path.is_file():return {'exists':False}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.execute('PRAGMA query_only=ON')
        result={'exists':True,'schema_version':db.execute('PRAGMA user_version').fetchone()[0],'mode':'read_only'}
        if prepared:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result['public_fixture_units']=db.execute('SELECT count(*) FROM recall_support_units').fetchone()[0] if 'recall_support_units' in tables else None
            result['consumption_events']=db.execute('SELECT count(*) FROM recall_support_events').fetchone()[0] if 'recall_support_events' in tables else None
        return result

def inspect(home,preparation):
    home=Path(home).resolve();preparation=Path(preparation).resolve()
    main=read_json(home/'config/workbench.json').get('model',{})
    jev=read_json(home/'private/knowledge-router/config.json')
    try:require_generation_provider(main);official=True
    except (ValueError,KeyError,TypeError):official=False
    endpoint=urlsplit(str(jev.get('base_url','')))
    jev_ready=endpoint.scheme=='https' and bool(endpoint.hostname) and not endpoint.username and not endpoint.password
    materials=read_json(preparation/'continuous-materials.json')
    fixture=database_info(preparation/'instance',prepared=True)
    report={'stage':'recall .3 minimal pre-start preparation','program':{'version':__version__,'schema_version':SCHEMA_VERSION},
        'data':database_info(home),'providers':{'main_official_deepseek':official,'main_key_present':bool(main.get('encrypted_key')),
            'jev_https_configured':jev_ready,'jev_model_configured':bool(str(jev.get('name','')).strip()),'jev_key_present':bool(jev.get('encrypted_key'))},
        'system_https_proxy_configured':bool(configured_proxy('https')),
        'preparation':{'public_artificial':materials.get('public_artificial') is True,
            'continuous_turns':len(materials.get('turns',[])),'database':fixture},
        'real_model_calls':0,'network_requests':0,'connection_test_performed':False,
        'database_migrated':False,'services_started':False,'formal_activation':False}
    report['data']['upgrade_required_for_candidate']=report['data'].get('schema_version')!=SCHEMA_VERSION
    report['passed']=all(report['providers'].values()) and report['system_https_proxy_configured'] and (
        report['data'].get('schema_version') in (27,28,29) and SCHEMA_VERSION in (28,29) and
        fixture.get('schema_version') in (28,29) and fixture.get('public_fixture_units')==7
        and report['preparation']['public_artificial'] and report['preparation']['continuous_turns']==24)
    return report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,required=True);parser.add_argument('--preparation',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True);args=parser.parse_args()
    output=args.out.resolve()
    if output.is_relative_to(REPO):raise ValueError('Evidence output must be outside the source repository')
    result=inspect(args.home,args.preparation)
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
