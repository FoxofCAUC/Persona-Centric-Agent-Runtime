"""Check source delivery imports and emit fingerprints without packaging data."""
import ast
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def verify():
    manifest=json.loads((ROOT/'source-delivery.json').read_text(encoding='utf-8'))
    excluded=set(manifest['excluded_modules'])
    forbidden={p[:-3].replace('/','.') for p in excluded}
    files=[p for root in manifest['roots'] for p in (ROOT/root).rglob('*.py') if p.relative_to(ROOT).as_posix() not in excluded]
    for path in files:
        module=path.relative_to(ROOT).with_suffix('').parts
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            names=[]
            if isinstance(node,ast.Import):names=[n.name for n in node.names]
            elif isinstance(node,ast.ImportFrom):
                base='.'.join(module[:-node.level]) if node.level else ''
                imported='.'.join(x for x in (base,node.module) if x)
                names=[imported]+[imported+'.'+n.name for n in node.names]
            if any(n in forbidden or n.startswith(('experiments.','plugins.')) or n in ('experiments','plugins') for n in names):
                raise ValueError('Production import requires excluded code: '+str(path.relative_to(ROOT)))
    return {'passed':True,'files':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files},'excluded':sorted(excluded)}


if __name__=='__main__':print(json.dumps(verify(),indent=2))
