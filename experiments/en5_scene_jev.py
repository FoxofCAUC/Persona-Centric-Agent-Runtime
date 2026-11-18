"""Evaluate the approved Jev scene choice on the same fixed 16 positions."""
import argparse
import ast
import asyncio
from collections import Counter
from contextlib import closing, contextmanager
from copy import deepcopy
from html import escape
import json
from pathlib import Path
from secrets import SystemRandom
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.dialogue_generation import GenerationHTTPClient
from persona_runtime.instance import InstanceLock
from persona_runtime.knowledge_routing import binding as provider_binding, configured_model
from persona_runtime.knowledge_web_decision import execute, usage
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch, provider_identity
from persona_runtime.reply_batch import ReplySchemaError, validate_reply
from persona_runtime.storage import Store

from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .en3_holdout_trial import FirstReceipts
from .en5_decision_packet import baseline_records, file_hash
from .en5_jev_transport import JevTransport, TIMING_PROFILE
from .en5_scene_compare import comparison
from .en5_scene_feedback import import_feedback
from .en5_scene_packet import LABELS, MARKER, freeze_author_review
from .en5_scene_route import SceneRouter, SceneSet
from .en5_scene_voice import WAIT_SECONDS, decision, require

STUDY = 'en5-scene-jev-first-v1'


def reference_binding(revision, feedback):
    archived, checksum, report = import_feedback(revision, feedback)
    # The author supplied complete paired ratings and preferences. Compare the author's
    # own judgments rather than interpreting free-form notes as operating instructions.
    ranks = report['preferred']
    require(ranks.get('B',0)>ranks.get('A',0)
            and report['grades']['B'].get('accept',0)>report['grades']['A'].get('accept',0),
            'author_improvement_required')
    return archived/'decoded.private.json',checksum


def body_wire(template, card, max_chars):
    wire = deepcopy(template)
    require(wire['messages'][0]['content'].count(MARKER)==1,'card_slot')
    wire['messages'][0]['content'] = wire['messages'][0]['content'].replace(MARKER,card,1)
    require(sum(len(msg['content']) for msg in wire['messages'])<=max_chars,'body_wire_budget')
    return wire


def report(folder, saved, view, manual):
    key_path = folder/'blind-key.private.json'
    if not key_path.exists():
        rng = SystemRandom()
        save_once(key_path,{'binding':saved['binding'],'labels':{
            pos:dict(zip('甲乙',rng.sample('BC',2))) for pos in saved['cases']}})
    key = read_private(key_path)
    require(key['binding']==saved['binding'] and set(key['labels'])==set(saved['cases']),'blind_key')
    cases = []
    evidence = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">',
                '<title>Jev 场景选择与首次回执</title><style>body{max-width:68rem;margin:auto;padding:1rem;font:16px/1.5 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>',
                '<h1>Jev 场景选择与首次回执</h1><pre>',escape(json.dumps(saved['stats'],ensure_ascii=False,indent=2)),'</pre>']
    for pos, row in saved['cases'].items():
        base = manual['results'][pos]['B']
        labels = key['labels'][pos]
        require(set(labels)==set('甲乙') and set(labels.values())=={'B','C'},'blind_labels')
        options = {}
        for label, arm in labels.items():
            body = base if arm=='B' else row['body']
            options[label] = {k:body.get(k) for k in ('state','body')}
        orig = view[pos]
        msgs = orig['main']['messages'][1:-1]
        before = [{'turn':i,'user':u['content'],'reply':a['content']}
                  for i,(u,a) in enumerate(zip(msgs[::2],msgs[1::2],strict=True),1)]
        cases.append({'position':pos,'user':orig['main']['messages'][-1]['content'],'before':before,'options':options})
        summary = {k:v for k,v in row.items() if k!='body'}
        summary['body_state'] = row['body']['state']
        evidence += ['<details><summary>',escape(pos+' · '+LABELS[row['reference']]+' → '+
                     (LABELS[row['scene']] if row['scene'] else '未运行')),'</summary><pre>',
                     escape(json.dumps({'方案身份':labels,**summary},ensure_ascii=False,indent=2)),'</pre></details>']
    view_data = {'study_id':STUDY,'input_contract_sha256':saved['requests_sha256'],
                 'results_sha256':file_hash(folder/'results.private.json'),
                 'blind_key_sha256':file_hash(key_path),'cases':cases}
    template = Path(__file__).with_name('en5_style_review.html').read_text(encoding='utf-8')
    template = template.replace('接话修复16位审阅意见.json','Jev选场景16位审阅意见.json')
    template = template.replace('角色的接话方式盲审','角色 · 人工选场景与 Jev 对照')
    template = template.replace('角色 · 接话方式盲审','角色 · Jev 选场景盲审')
    target = folder/'review/index.html'
    save_text_once(target,template.replace('@@DATA@@',json.dumps(view_data,ensure_ascii=False).replace('<',r'\u003c')))
    save_text_once(target.with_name('evidence.html'),''.join(evidence)+'</html>')
    return {'review':str(target),'evidence':str(target.with_name('evidence.html')),**saved['stats']}


def reusable_body(folder, old, pos, wire, model):
    body = old['cases'][pos]['body']
    if body.get('state')!='completed' or body.get('request_hash')!=fingerprint(wire):
        return None
    path = (folder/'calls.sqlite3').as_uri()+'?mode=ro'
    with closing(sqlite3.connect(path,uri=True)) as db:
        rec = db.execute('SELECT wire,raw,raw_hash,http FROM requests WHERE attempt_id=?',(body['attempt_id'],)).fetchone()
    if rec is None:
        return None
    sent,raw,checksum,status = rec
    require(json.loads(sent)==wire and status==200 and digest(raw)==checksum==body['raw_hash'],'prior_pro_receipt')
    from persona_runtime.dialogue_receipt import parse_receipt
    parsed = parse_receipt(raw,wire['stream'])
    require(parsed['valid'] and parsed['model']==wire['model'] and
            validate_reply(parsed['body'],'context_full',model.get('_expression_snapshot')).text==body['body'],
            'prior_pro_result')
    return body


async def run(root, submitted, *, check_gateway_again=False, model_name=None, recover_receipt=False,
              timed_rerun=False):
    root = outside_repository(root)
    state = read_private(root.parent/'prompt-review-state.json')
    revision = outside_repository(state['en5_scene_author_revision'])
    require(revision.parent==root/'scene-v1/author-revisions','author_revision_scope')
    feedback, feedback_hash = reference_binding(revision,submitted)
    require(freeze_author_review(root,revision/'author-review.private.json')==revision,'author_version')
    packet = root/'scene-v1/inputs.private.json'
    contract = revision/'requests.private.json'
    manual_path = revision/'manual-pro/results.private.json'
    require(file_hash(packet)==state['en5_scene_input_packet_sha256']
            and file_hash(manual_path)==state['en5_scene_manual_pro_results_sha256'],'author_binding')
    manual = read_private(manual_path)
    catalog = read_private(revision/'catalog.json')
    cards = SceneSet(revision,fingerprint(catalog),catalog['persona'])
    reviewed = read_private(contract)
    source, originals = baseline_records(Path(read_private(root/'material.private.json')['prior']))
    plan = comparison(reviewed,originals)
    view = {r['position']:r for r in read_private(packet)['cases']}
    paths = RuntimePaths(source/'home')
    store = Store(paths)
    corpus = DatasetCorpusStore(read_private(source/'selection.private.json')['corpus_root'])
    configured = configured_model(paths)
    require(model_name in (None,'jev-1.14') and not (model_name and check_gateway_again),'jev_retry_model')
    require(not recover_receipt or model_name=='jev-1.14','jev_receipt_recovery_model')
    require(not timed_rerun or (model_name=='jev-1.14' and not recover_receipt),'jev_timed_model')
    jev = configured | {'name':model_name} if model_name else configured
    for row in reviewed:
        wire = row['jev']
        require(wire['model']==configured['name'] and len(json.dumps(wire,ensure_ascii=False))<=jev['max_input_chars']
                and manual['results'][row['position']]['B']['request_hash']==fingerprint(row['main']),'approved_wire')
    reviewed_wires = {r['position']:fingerprint(r['jev']) for r in reviewed}
    if model_name:
        reviewed = [r | {'jev':r['jev'] | {'model':model_name}} for r in reviewed]
    files = {p:file_hash(p) for p in (packet,contract,manual_path,feedback,revision/'author-review.private.json')}
    scope = {'manual_feedback_sha256':feedback_hash,'source_packet_sha256':files[packet],
             'manual_results_sha256':files[manual_path],'requests_sha256':files[contract],
             'card_revision':cards.revision,'jev_provider':provider_binding(jev),
             'allowed_jev':{r['position']:fingerprint(r['jev']) for r in reviewed}}
    if model_name:
        scope.update(author_model_change='将角色模型的模型名更换为jev-1.14并重试。',
                     authorized_credential_source=provider_binding(configured),
                     endpoint=configured['base_url'],previous_model=configured['name'],
                     selected_model=model_name,reviewed_jev=reviewed_wires)
    folder = revision/('jev-pro-1.14' if model_name else 'jev-pro')
    previous_folder = folder
    old_timing_result = None
    if timed_rerun:
        old_path = previous_folder/'first-receipt-recovery/results.private.json'
        checksum = file_hash(old_path)
        prior_results = state.get('en5_scene_jev_original_timing_results_sha256',state['en5_scene_jev_results_sha256'])
        require(checksum==prior_results,'jev_timing_parent')
        old_timing_result = read_private(old_path)
        files[old_path] = checksum
        scope.update(timing_profile=TIMING_PROFILE,prior_results_sha256=checksum)
        folder = revision/'jev-pro-1.14-wire-timing'
    folder.mkdir(exist_ok=True)
    recovery_folder = folder/'first-receipt-recovery'
    if model_name and (recovery_folder/'recovered-first-receipt.private.json').is_file():
        recover_receipt = True
    authorization = folder/'scope.private.json'
    first_result = folder/'results.private.json'
    result_folder = folder/'gateway-check' if check_gateway_again else folder
    if recover_receipt:
        result_folder = recovery_folder
    terminal = result_folder/'results.private.json'
    permit = {'study':STUDY,**scope}
    with closing(InstanceLock(folder/'run.lock')):
        require(authorization.exists() or not (folder/'decisions.sqlite3').exists(),'dispatch_permission_withdrawn')
        permit_hash = save_once(authorization,permit)
        if terminal.exists():
            saved = read_private(terminal)
            require(saved['binding']==permit_hash,'results_binding')
            return report(result_folder,saved,view,manual)
        retry_after = None
        if check_gateway_again:
            previous = read_private(first_result)
            require(previous['binding']==permit_hash and previous['stats']['jev_calls']==1
                    and previous['stats']['blocked']=='jev_first_response_unavailable','gateway_followup_scope')
            retry_after = reviewed[0]['position']
            require(previous['cases'][retry_after]['first_response']['http']==525,'gateway_followup_status')
        if recover_receipt:
            original = read_private(first_result)
            require(original['binding']==permit_hash and original['stats']['jev_calls']==1
                    and original['stats']['blocked']=='jev_choice_contract_invalid','first_receipt_recovery_scope')
        prior = Path(read_private(root/'material.private.json')['prior'])
        def check(_=None):
            require(file_hash(authorization)==permit_hash and all(file_hash(p)==h for p,h in files.items()),'scope_changed')
            require(baseline_records(prior)==(source,originals),'fixed_sources_changed')
        @contextmanager
        def window():
            with corpus.connection(write=True),store.connection(write=True):
                yield
        credentials = ConfigStore(paths).load()['model']
        def route(slot, stage, model):
            pos = slot.split('.',1)[0]
            baseline = plan[pos][0]
            require(stage=='body' and model==baseline['model_public']
                    and provider_identity(model)==provider_identity(ConfigStore(paths).load()['model']),'pro_provider_changed')
            guard_cloud_dispatch(store,baseline['persona_id'],'main',model)
        journal = FirstReceipts(folder,permit_hash,client=GenerationHTTPClient(),guard=check,
                                authority_window=window,guard_route=route)
        router = SceneRouter(folder,cards)
        timed_client = JevTransport(jev) if timed_rerun else None
        try:
            results = {}
            blocked = None
            validated_model = False
            for item in reviewed:
                pos = item['position']
                baseline, expected, main = plan[pos]
                if pos==retry_after:
                    results[pos] = previous['cases'][pos]
                    continue
                if blocked:
                    results[pos] = {'reference':expected,'scene':None,'status':'study_halted','body':{'state':'blocked','body':None}}
                    continue
                def dispatch_check():
                    with window():
                        check()
                        require(provider_binding(configured_model(paths))==provider_binding(configured),'jev_provider_changed')
                        guard_cloud_dispatch(store,baseline['persona_id'],'knowledge_router',configured)
                        require(provider_binding(jev)==scope['jev_provider']
                                and fingerprint(item['jev'])==scope['allowed_jev'][pos],'jev_dispatch_scope')
                async def transport(wire, **timing):
                    if timed_client is not None:
                        def record_timing(diag):
                            save_once(folder/'timings'/(pos+'.private.json'),diag)
                        return await timed_client.complete(wire,diagnostics=record_timing,**timing)
                    try:
                        raw, _ = await execute(jev | {'timeout':min(jev['timeout'],30)},wire)
                        return 200,raw
                    except ModelError as error:
                        status = (error.diagnostics or {}).get('http_status')
                        if status is None:
                            raise
                        return status,getattr(error,'native_response','')
                use = fingerprint([permit_hash,pos,baseline['input_sha256'],scope['allowed_jev'][pos]])
                chosen, card = await router.resolve(pos,use,item['jev'],transport,dispatch_check,wire_timing=timed_rerun)
                if router.jobs:
                    # Keep a late first response for diagnostics and freeze the already chosen card.
                    await asyncio.gather(*list(router.jobs))
                with router.db() as db:
                    rec = dict(db.execute('SELECT * FROM routes WHERE turn=?',(pos,)).fetchone())
                require(json.loads(rec['wire'])==item['jev'] and rec['request_hash']==scope['allowed_jev'][pos],
                        'jev_wire_changed')
                raw = rec['raw']
                if recover_receipt and pos==reviewed[0]['position']:
                    recorded = original['cases'][pos]['first_response']
                    require(rec['http']==recorded['http']==200 and digest(raw.encode())==recorded['raw_sha256'],
                            'first_receipt_recovery_source')
                    raw, measured = ast.literal_eval(raw)
                    require(usage(json.loads(raw))==measured and decision(raw)[1]!='invalid','first_receipt_recovery_body')
                    status = 'timeout' if rec['elapsed_ms']>=1000*WAIT_SECONDS else decision(raw)[1]
                    chosen = 'fallback' if status=='timeout' else decision(raw)[0]
                    card = cards.card(chosen)
                    rec.update(reason=status,usage=json.dumps(measured))
                    save_once(result_folder/'recovered-first-receipt.private.json',{
                        'position':pos,'stored_sha256':recorded['raw_sha256'],'raw':raw,
                        'raw_sha256':digest(raw.encode()),'elapsed_ms':rec['elapsed_ms'],'scene':chosen,'status':status})
                response = {}
                if raw:
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed,dict):
                            response = parsed
                    except ValueError:
                        pass
                provider_reply = response.get('model')
                reply_notes = {'http':rec['http'],'response_model':provider_reply,'elapsed_ms':rec['elapsed_ms'],
                               'usage':json.loads(rec['usage']) if rec['usage'] else None,
                               'raw_sha256':digest(raw.encode()) if raw is not None else None,
                               'observed':response.get('answers',{}).get('scene') if isinstance(response.get('answers'),dict) else None}
                if timed_client is not None:
                    timing_file = folder/'timings'/(pos+'.private.json')
                    if timing_file.is_file():
                        timing = read_private(timing_file)
                        require(timing['http']==rec['http'] and (timing['wire_ms'] is None
                                or abs(timing['wire_ms']-rec['elapsed_ms'])<=.001),'jev_timing_receipt')
                        reply_notes['timing'] = timing
                parsed_choice, parsed_status = decision(raw) if rec['http']==200 else (None,'unavailable')
                if rec['http'] in (401,403,404,429) or (not validated_model and rec['http']!=200):
                    blocked = 'jev_first_response_unavailable'
                elif not validated_model and parsed_status=='invalid':
                    blocked = 'jev_choice_contract_invalid'
                elif isinstance(provider_reply,str) and 'jev-1.' in provider_reply and jev['name'] not in provider_reply:
                    blocked = 'jev_response_model_changed'
                if rec['http']==200 and parsed_status!='invalid' and not blocked:
                    validated_model = True
                body = {'state':'blocked','body':None}
                origin = 'not_dispatched'
                if not blocked:
                    wire = body_wire(view[pos]['main'],card,baseline['model_public']['max_input_chars'])
                    if wire==main:
                        body = manual['results'][pos]['B']
                        origin = 'manual_first_response'
                    elif old_timing_result is not None and (
                            previous := reusable_body(previous_folder,old_timing_result,pos,wire,baseline['model_public'])):
                        body = previous
                        origin = 'prior_pro_first_response'
                    else:
                        body = await journal.send(pos+'.pro.body','body',(baseline['model_public'],wire),
                                                  [scope['allowed_jev'][pos],reply_notes['raw_sha256']],credentials)
                        body_text = body.pop('content')
                        origin = 'new_pro'
                        body['body']=None
                        if body['state']=='completed':
                            try:
                                body['body'] = validate_reply(body_text,'context_full',
                                                               baseline['model_public'].get('_expression_snapshot')).text
                            except (ReplySchemaError,ValueError,TypeError):
                                body.update(state='failed',error_code='body_validation_failed')
                results[pos] = {'reference':expected,'scene':chosen,'status':rec['reason'],'parsed_choice':parsed_choice,
                                'parsed_status':parsed_status,'first_response':reply_notes,'origin':origin,'body':body}
                print(pos,rec['http'],rec['reason'],expected,chosen,origin,flush=True)
            check()
            with router.db() as db:
                jev_count = db.execute('SELECT count(*) FROM routes WHERE started IS NOT NULL').fetchone()[0]
            with closing(journal.open()) as db:
                pro_count = db.execute('SELECT count(*) FROM requests').fetchone()[0]
            used = Counter()
            unknown = Counter()
            for value in results.values():
                for kind, call in (('jev',value.get('first_response')),
                                   ('pro',value['body'] if value.get('origin')=='new_pro' and value['body'].get('attempt_id') else None)):
                    if call is not None:
                        n = (call.get('usage') or {}).get('total_tokens')
                        if type(n) is int:
                            used[kind] += n
                        else:
                            unknown[kind] += 1
            stats = {'jev_calls':jev_count,'pro_calls':pro_count,'known_tokens':dict(used),
                     'unknown_usage':dict(unknown),'actual_charge':None,'blocked':blocked,
                     'decision_status':dict(Counter(r['status'] for r in results.values())),
                     'same_as_author':sum(r['scene']==r['reference'] for r in results.values() if r.get('origin') not in ('not_dispatched',None)),
                     'reused_pro':sum(r.get('origin')=='manual_first_response' for r in results.values()),
                     'completed_body':sum(r['body']['state']=='completed' for r in results.values())}
            if timed_rerun:
                stats['reused_prior_pro'] = sum(r.get('origin')=='prior_pro_first_response' for r in results.values())
            saved = {'study':STUDY,'binding':permit_hash,'requests_sha256':files[contract],'cases':results,'stats':stats}
            save_once(terminal,saved)
            return report(result_folder,saved,view,manual)
        finally:
            try:
                await router.close()
            finally:
                if timed_client is not None:
                    await timed_client.close()


if __name__=='__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True)
    cli.add_argument('--feedback',type=Path,required=True)
    cli.add_argument('--check-gateway-again',action='store_true')
    cli.add_argument('--model',choices=('jev-1.14',))
    cli.add_argument('--recover-receipt',action='store_true')
    cli.add_argument('--timed-rerun',action='store_true')
    args = cli.parse_args()
    print(json.dumps(asyncio.run(run(args.root,args.feedback,check_gateway_again=args.check_gateway_again,
                                     model_name=args.model,recover_receipt=args.recover_receipt,
                                     timed_rerun=args.timed_rerun)),ensure_ascii=False))
