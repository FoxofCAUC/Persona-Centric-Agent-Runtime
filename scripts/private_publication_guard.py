"""Fail-closed local pre-push guard. Source records remain outside Git."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from contextlib import closing
sys.dont_write_bytecode = True

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import PrivatePersonas, publication_scan, safe
from persona_runtime.models import RuntimeErrorBase


def author_version_needles(paths):
    """Read protected author history in memory; the original package stays immutable."""
    records = PrivatePersonas(paths).load(required=True)['records']
    targets = set()
    for record in records:
        persona = record.get('persona_id')
        if persona is None:
            if record.get('state') == 'ready':
                raise RuntimeErrorBase('private_author_scope_invalid')
            continue
        if not isinstance(persona, str) or not persona:
            raise RuntimeErrorBase('private_author_scope_invalid')
        targets.add(persona)
    if not targets:
        return set()
    path = safe(paths.home / 'data/database/runtime.sqlite3', paths.home)
    needles = set()

    def add(text):
        # Keep complete and JSON-escaped forms; neither is persisted or reported.
        needles.update((text.encode(), json.dumps(text, ensure_ascii=False)[1:-1].encode(),
                        json.dumps(text, ensure_ascii=True)[1:-1].encode()))

    def visit(value):
        if isinstance(value, str) and len(value) >= 24:
            add(value)
            for i in range(0, max(1, len(value) - 47), 24):
                add(value[i:i + 48])
        elif isinstance(value, dict):
            for child in value.values(): visit(child)
        elif isinstance(value, list):
            for child in value: visit(child)

    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='persona_lifecycle'").fetchone():
                # Formal same-database copies inherit the registered privacy root.
                targets.update(row[0] for row in db.execute('SELECT persona_id,privacy_root FROM persona_lifecycle')
                               if row[1] in targets)
            for persona in sorted(targets):
                profiles = db.execute('SELECT kind,data_json FROM profile_versions WHERE persona_id=?',
                                      (persona,)).fetchall()
                characters = db.execute('SELECT data_json FROM character_versions WHERE persona_id=?',
                                        (persona,)).fetchall()
                if not {'core', 'expression', 'relationship', 'state'} <= {row[0] for row in profiles} or not characters:
                    raise RuntimeErrorBase('private_author_history_unavailable')
                for kind, raw in profiles:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError()
                    visit(value)
                    if kind == 'core':
                        name = value.get('name')
                        if not isinstance(name, str) or not name:
                            raise ValueError()
                        add(name)
                for (raw,) in characters:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError()
                    visit(value)
    except (sqlite3.Error, ValueError, TypeError):
        raise RuntimeErrorBase('private_author_history_unavailable') from None
    return needles


def author_version_scan(paths, repo, *, staged=False, base=None):
    """Extend publication protection to current and prior formal author versions."""
    needles = author_version_needles(paths)
    if not needles:
        return {'passed': True, 'checked_blobs': 0, 'hits': []}
    repo = Path(repo).resolve()
    hits, checked = [], 0

    def git(*args):
        return subprocess.check_output(['git', *args], cwd=repo, stderr=subprocess.DEVNULL)

    def check(location, data):
        nonlocal checked
        checked += 1
        if any(needle in data for needle in needles):
            hits.append({'location': location, 'reason': 'private_author_version_content'})

    names = set(git('ls-files', '-z').decode().split('\0')) - {''}
    if staged:
        for name in sorted(names): check('index:' + name, git('show', ':' + name))
    else:
        names |= set(git('ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')) - {''}
        for name in sorted(names):
            if (repo / name).is_file(): check(name, (repo / name).read_bytes())
    if base:
        seen = set()
        for commit in git('rev-list', base + '..HEAD').decode().splitlines():
            for item in git('ls-tree', '-rz', commit).split(b'\0'):
                if not item: continue
                meta, name = item.split(b'\t', 1)
                blob = meta.split()[-1].decode()
                if blob not in seen:
                    seen.add(blob)
                    check(commit[:12] + ':' + name.decode(), git('cat-file', 'blob', blob))
    return {'passed': not hits, 'checked_blobs': checked, 'hits': hits}


def check_publication(paths, repo, *, staged=False, base=None):
    result = publication_scan(paths, repo, staged=staged, base=base)
    if result['passed']:
        author = author_version_scan(paths, repo, staged=staged, base=base)
        result = {**result, 'passed': author['passed'],
                  'hits': result['hits'] + author['hits'], 'author_versions': author}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True);parser.add_argument('--home',type=Path,required=True)
    parser.add_argument('--outgoing',action='store_true');parser.add_argument('--staged',action='store_true')
    args=parser.parse_args()
    bases=[]
    if args.outgoing:
        for line in sys.stdin.read().splitlines():
            fields=line.split()
            if len(fields)==4 and fields[3]!='0'*40:bases.append(fields[3])
        if not bases:bases=['origin/main']
    else:bases=[None]
    for base in bases:
        result=check_publication(RuntimePaths(args.home.resolve()),args.repo,staged=args.staged,base=base)
        print(json.dumps(result,ensure_ascii=False))
        if not result['passed']:return 1
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print('Private publication validation unavailable; push blocked.',file=sys.stderr)
        raise SystemExit(1)
