"""Install or pause a locally approved desktop scene package on a stopped instance."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from persona_runtime.migration import _closed, _offline
from persona_runtime.paths import RuntimePaths
from persona_runtime.scene_policy import active, install, pause
from persona_runtime.storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,required=True)
    parser.add_argument('--persona',required=True)
    actions = parser.add_subparsers(dest='action',required=True)
    enable = actions.add_parser('enable')
    enable.add_argument('--approved-material',type=Path,required=True)
    enable.add_argument('--catalog-sha256',required=True)
    enable.add_argument('--approval-sha256',required=True)
    actions.add_parser('disable')
    actions.add_parser('status')
    args = parser.parse_args()
    paths = RuntimePaths.from_environment(args.home)
    store = Store(paths)
    if args.action=='status':
        with store.connection() as db:
            result = active(db,args.persona)
        print(json.dumps({'enabled':bool(result),'catalog_sha256':result['catalog_sha256'] if result else None}))
        return
    _closed(paths)
    with _offline(paths):
        if args.action=='enable':
            result = install(store,args.persona,args.approved_material,args.catalog_sha256,args.approval_sha256)
            print(json.dumps({'enabled':True,'catalog_sha256':result['catalog_sha256']}))
        else:
            from persona_runtime.expression_compat import pause_speech
            with store.connection(write=True) as db:
                pause(db,args.persona)
                pause_speech(db,scene_persona=args.persona)
                db.execute("""UPDATE turns SET error_code='migration_requires_review',status='interrupted'
                    WHERE persona_id=? AND status!='completed'
                    AND json_extract(context_policy_snapshot,'$.scene_voice') IS NOT NULL""",(args.persona,))
            print(json.dumps({'enabled':False,'profile':'companion-runtime-v8'}))


if __name__=='__main__':
    main()
