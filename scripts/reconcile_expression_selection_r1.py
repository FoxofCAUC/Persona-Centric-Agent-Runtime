"""Read-only accounting investigation after a stop; NEVER enables a resume."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.expression_selection_r1 import OUT, read, sha, Store, RuntimePaths, ConfigStore, ChatEngine, NoDispatch
from persona_runtime.config import atomic_json


def main():
    root=OUT/'experiment';frozen=read(root/'prepared.json');ledger=read(root/'ledger.json');issues=[];local=[]
    if not (root/'ledger-at-stop.json').exists():(root/'ledger-at-stop.json').write_bytes((root/'ledger.json').read_bytes())
    if not (root/'stop.json').exists():atomic_json(root/'stop.json',read(OUT/'last-stop.json'))
    for item in frozen['items']:
        paths=RuntimePaths(Path(item['runtime']));store=Store(paths)
        with store.connection() as db:
            for kind,table in (('selector','character_selection_attempts'),('main','attempts')):
                attempts=[dict(r) for r in db.execute('SELECT id,status,error_code FROM '+table+' WHERE turn_id=?',(item['turn_id'],))]
                calls=[r for r in ledger['calls'] if r['item']==item['key'] and r['kind']==kind]
                if len(attempts)!=len(calls):issues.append({'item':item['key'],'kind':kind,'runtime_attempts':attempts,
                    'ledger_call_ordinals':[r['ordinal'] for r in calls],'classification':'local_attempt_without_durable_dispatch_receipt'})
        if not (root/'results'/(item['key']+'.json')).exists():
            engine=ChatEngine(store,ConfigStore(paths),NoDispatch())
            try:
                with store.connection() as db:manifests=[read_manifest[0] for read_manifest in db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(item['turn_id'],))]
                import json
                local.append({'item':item['key'],'turn':engine.get(item['turn_id']),'manifests':[json.loads(m) for m in manifests],
                    'recorded_dispatches':[r['ordinal'] for r in ledger['calls'] if r['item']==item['key']]})
            finally:engine.cognitive.shadow.close();engine.cognitive.characters.close()
    results=[read(p) for p in (root/'results').glob('*.json')]
    report={'stopped':True,'resume_allowed':False,'verification_complete':False,'recorded_calls':len(ledger['calls']),
        'runtime_attempts_without_dispatch_receipt':len(issues),'conservative_budget_slots':len(ledger['calls'])+len(issues),
        'finished_items':len(results),'successful_replies':sum(r['turn']['status']=='completed' for r in results),
        'failed_replies':sum(r['turn']['status']=='failed' for r in results),
        'ledger_sha256':sha(root/'ledger.json'),'issues':issues,
        'cause':'未持久化的预留出现在内存账本，主请求被完整性检查阻止。原始写入异常没有留存，具体文件写入失败原因未确认。',
        'interpretation':'运行时尝试数不等于供应商调用数。56条有完整调用账本；另外2条仅有本地尝试，保守占位，不补造供应商回执。',
        'repair':'仅离线加强预留写入失败的立即停测及持久停测标记；不重跑此次真实批次。'}
    atomic_json(root/'stop-reconciliation.json',report);atomic_json(root/'local-incomplete-items.json',local)
    atomic_json(root/'run-status.json',{'real':True,'items':len(results),'calls':len(ledger['calls']),'complete':False,
        'failed_calls':sum(r['status']=='failed' for r in ledger['calls']),'stop':'ledger_integrity','successful_replies':report['successful_replies']})
    print(__import__('json').dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
