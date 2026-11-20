"""Local operator entry point for admitting evidence, inspecting reads and withdrawing facts."""
import argparse
import json
from pathlib import Path

from persona_runtime import local_knowledge as library
from persona_runtime import semantic_knowledge as semantic
from persona_runtime import session_privacy
from persona_runtime.config import ConfigStore
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',required=True,type=Path)
    commands=parser.add_subparsers(dest='command',required=True)
    public=commands.add_parser('admit-public')
    public.add_argument('--scope',required=True,choices=('persona','instance'))
    public.add_argument('--persona')
    public.add_argument('material',type=Path)
    legacy=commands.add_parser('admit-legacy')
    legacy.add_argument('persona');legacy.add_argument('claim')
    turn=commands.add_parser('admit-turn')
    turn.add_argument('persona');turn.add_argument('session');turn.add_argument('turn')
    turn.add_argument('source');turn.add_argument('fact',type=Path)
    query=commands.add_parser('query')
    query.add_argument('persona');query.add_argument('session');query.add_argument('question')
    commands.add_parser('withdraw').add_argument('fact')
    commands.add_parser('withdraw-document').add_argument('document')
    commands.add_parser('rebuild-index')
    args=parser.parse_args()
    paths=RuntimePaths(args.home.resolve())
    store=Store(paths)
    with store.connection(write=True) as db:
        if args.command=='admit-public':
            material=json.loads(args.material.read_bytes())
            if set(material)!={'document','fact'} or (args.scope=='persona')!=bool(args.persona):
                raise RuntimeErrorBase('local_knowledge_invalid_document')
            if set(material['document'])-{'title','url','body','receipt','acquired_at','published_at','source_kind'}:
                raise RuntimeErrorBase('local_knowledge_invalid_document')
            doc=library.put_document(db,scope=args.scope,persona_id=args.persona,versioned=True,**material['document'])
            result=library.put_fact(db,doc,verification_receipt=material['document']['receipt'],versioned=True,**fact_fields(material['fact']))
        elif args.command=='admit-legacy':
            result=library.admit_legacy(db,args.persona,args.claim)
        elif args.command=='admit-turn':
            config=ConfigStore(paths);cfg=config.load()
            consent=session_privacy.current(db,config,cfg,args.persona,args.session,session_privacy.recipients(config,cfg))
            result=library.admit_turn_source(db,args.persona,args.session,args.turn,args.source,
                instance=cfg['instance_id'],consent=consent,versioned=True,**fact_fields(json.loads(args.fact.read_bytes())))
        elif args.command=='withdraw':
            library.withdraw(db,args.fact);result=args.fact
        elif args.command=='withdraw-document':
            library.withdraw_document(db,args.document);result=args.document
        elif args.command=='rebuild-index':
            library.rebuild(db);semantic.reset_index(paths);result='rebuilt'
        else:
            config=ConfigStore(paths);cfg=config.load()
            grant=session_privacy.current(db,config,cfg,args.persona,args.session,session_privacy.recipients(config,cfg))
            turn={'id':'','persona_id':args.persona,'session_id':args.session}
            base={'instance':cfg['instance_id'],'consent':grant}
            if library.enabled(db,args.persona):
                semantic.import_prior_sources(db,turn,base)
            candidates=semantic.materials(db,turn,base,args.question)
    if args.command=='query':
        facts=semantic.rank(paths,args.question,candidates)
        result={'outcome':'hit' if facts else 'miss','facts':facts}
    print(json.dumps(result,ensure_ascii=False))


def fact_fields(data):
    allowed={'subject','predicate','value','quote','start','end','aliases','attributes','conditions',
             'valid_from','valid_until','expires_at','evidence_state','previous_id','unit'}
    if not isinstance(data,dict) or set(data)-allowed:raise RuntimeErrorBase('local_knowledge_invalid_fact')
    return data


if __name__=='__main__':
    main()
