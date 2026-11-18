"""Local maintenance of reviewed dialogue materials and their dispatch scope."""
import argparse
import json
from pathlib import Path

from persona_runtime.dialogue_assembly import load_bundle, load_offline_bundle
from persona_runtime.expression_corpus_sources import approval_fields, register_bundle, reviewed_manifest, revoke_bundle, scope_record
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--home', type=Path, required=True)
    cli.add_argument('--persona', required=True)
    actions = cli.add_subparsers(dest='action', required=True)
    for kind in ('preview', 'register'):
        sub = actions.add_parser(kind)
        sub.add_argument('--manifest', type=Path, required=True)
        if kind == 'register':
            sub.add_argument('--author-approval', type=Path)
    for kind in ('verify', 'revoke'):
        sub = actions.add_parser(kind)
        sub.add_argument('--bundle', required=True)
    args = cli.parse_args(argv)
    store = Store(RuntimePaths(args.home))
    if args.action == 'preview':
        manifest, checksum, assets = reviewed_manifest(args.manifest)
        with store.connection() as db:
            target = scope_record(db, store, args.persona)
        value = {'proposal_count':len(assets), 'provider':'deepseek_official', 'manifest_sha256':checksum,
                 'review_files':len(manifest['review_files']), 'approval_draft':approval_fields(checksum,target) | {'approved':False}}
    elif args.action == 'register':
        value = {'bundle_id':register_bundle(store, args.persona, args.manifest, approval_path=args.author_approval),
                 'purpose':'candidate_runtime' if args.author_approval else 'offline_replay'}
    elif args.action == 'verify':
        with store.connection() as db:
            row = db.execute('SELECT purpose FROM dialogue_source_bundles WHERE id=? AND persona_id=?',
                             (args.bundle,args.persona)).fetchone()
            if not row:
                raise ValueError('dialogue_source_binding_unavailable')
            loader = load_offline_bundle if row[0] == 'offline_replay' else load_bundle
            loader(store, db, args.persona, args.bundle)
            value = {'bundle_id':args.bundle, 'purpose':row[0], 'current_sources_valid':True}
    else:
        revoke_bundle(store, args.persona, args.bundle)
        value = {'bundle_id':args.bundle, 'state':'revoked'}
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
