"""Six-position coverage study using approved real sources and unchanged E2 text.

This is a research coordinator, not a new prompt or generation protocol. Three
two-position approvals reuse the existing single-attempt ledger and transport.
"""
from collections import Counter
from datetime import datetime,timedelta,timezone
from html import escape
from itertools import combinations
import json
import os
from pathlib import Path

from persona_runtime.corpus_files import CorpusError,digest
from persona_runtime.corpus_store import encoded,fingerprint,outside_repository
from persona_runtime.expression_learning import learning_messages
from persona_runtime.provider_compat import build_request
from persona_runtime.corpus_learning_review import export_learning_review


SELECTION_VERSION='real-corpus-coverage-v1'


def atomic_json(path,value):
    temporary=path.with_suffix(path.suffix+'.pending')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(temporary,path)


def select_six(candidates):
    if len(candidates)<6:raise CorpusError('coverage_six_complete_fragments_unavailable')
    # The present approved pool is small. Refuse unbounded combination search
    # instead of silently changing the selection policy for a future corpus.
    if len(candidates)>40:raise CorpusError('coverage_candidate_plan_requires_revision')
    candidates=sorted(candidates,key=lambda c:(c['first_epoch'],c['fragment_id']))
    best=None;best_score=None
    for selected in combinations(candidates,6):
        if len({c['group_id'] for c in selected})!=6:continue
        score=(len({c['month'] for c in selected}),len({c['source_count'] for c in selected}),
               len({c['dominant'] for c in selected}),len({c['first_role'] for c in selected}),
               sum(c['source_count'] for c in selected),selected[-1]['first_epoch']-selected[0]['first_epoch'])
        if best_score is None or score>best_score:best,best_score=selected,score
    if best is None:raise CorpusError('coverage_distinct_groups_unavailable')
    return list(best)


def collect_candidates(payload,relations,used_message_ids):
    by_id={r['message_id']:r for r in payload['messages']}
    used_groups={f['group_id'] for f in payload['fragments'] if set(f['message_ids'])&set(used_message_ids)}
    references={r['source_id']:r['target_id'] for r in relations if r['kind']=='reply' and r['state']=='resolved'}
    result=[]
    for fragment in payload['fragments']:
        if fragment['split']!='learn' or fragment['group_id'] in used_groups or not 2<=len(fragment['message_ids'])<=10:continue
        rows=[by_id[i] for i in fragment['message_ids']];ids=set(fragment['message_ids'])
        if not all(r['target_candidate'] and not r['flags'] and not r['redactions'] for r in rows):continue
        if {r['role'] for r in rows}!={'A','B'}:continue
        if any(references.get(i) and references[i] not in ids for i in ids):continue
        messages=learning_messages([{'body':r['text']} for r in rows])
        if len(json.dumps(messages,ensure_ascii=False))>12000:continue
        roles=Counter(r['role'] for r in rows)
        result.append({'fragment_id':fragment['id'],'group_id':fragment['group_id'],
            'first_epoch':fragment['first_epoch'],'month':datetime.fromtimestamp(fragment['first_epoch'],timezone(timedelta(hours=8))).strftime('%Y-%m'),
            'source_count':len(rows),'dominant':'tie' if roles['A']==roles['B'] else max(roles,key=roles.get),
            'first_role':rows[0]['role'],'role_counts':dict(roles),'members':rows,'messages':messages})
    return result


def freeze_coverage(corpus,learning,scope_path,baseline_preview,previous_state,output):
    scope_path=Path(scope_path).resolve();scope_raw=scope_path.read_bytes();scope=json.loads(scope_raw)
    if (scope.get('version')!='corpus-coverage-author-scope-v1' or scope.get('additional_request_limit')!=6
            or scope.get('cumulative_request_limit')!=8 or scope.get('first_batch_value_confirmed') is not True
            or scope.get('template_changes_authorized') is not False or scope.get('automatic_asset_publication') is not False
            or scope.get('provider')!='deepseek_official' or scope.get('model')!='deepseek-flash'):
        raise CorpusError('coverage_author_scope_invalid')
    output=outside_repository(output);envelope_path=output/'envelope.private.json'
    if envelope_path.exists():
        result=json.loads(envelope_path.read_text(encoding='utf-8'))
        if result['author_scope_sha256']!=digest(scope_raw):raise CorpusError('coverage_frozen_scope_changed')
        validate_envelope(corpus,learning,envelope_path)
        return result
    if output.exists() and any(output.iterdir()):raise CorpusError('coverage_empty_target_required')
    prior=json.loads(Path(previous_state).read_text(encoding='utf-8'))
    prior_ids=prior['job_ids']
    if len(prior_ids)!=2:raise CorpusError('coverage_previous_study_invalid')
    used=set();prior_bindings=[]
    with learning.connection() as db:
        if {r[0] for r in db.execute('SELECT id FROM learning_jobs')}!=set(prior_ids):
            raise CorpusError('coverage_existing_unexpected_jobs')
        for ident in prior_ids:
            row=learning._row(db,ident)
            if row['state']!='completed' or row['http_status']!=200 or row['raw_response'] is None:
                raise CorpusError('coverage_previous_position_not_complete')
            used.update(s['view_message_id'] for s in json.loads(row['sources_json']))
            prior_bindings.append({k:row[k] for k in ('id','approval_id','request_hash','raw_response_hash','position_key')})
    corpus.verify_version(scope['view_id']);view=corpus.view_status(scope['view_id'])
    if view['payload_hash']!=scope['view_payload_hash']:raise CorpusError('coverage_approved_view_changed')
    payload=corpus.load_version(view['id'])
    with corpus.connection() as db:
        relations=[dict(r) for r in db.execute('SELECT * FROM corpus_relations WHERE parse_id=? ORDER BY source_id,kind',(view['parse_id'],))]
    candidates=collect_candidates(payload,relations,used);selected=select_six(candidates)
    baseline=json.loads((Path(baseline_preview)/'manifest.private.json').read_text(encoding='utf-8'))
    if baseline['view_id']!=view['id'] or baseline['view_payload_hash']!=view['payload_hash']:
        raise CorpusError('coverage_baseline_view_changed')
    output.mkdir(parents=True,exist_ok=True)
    envelope={'version':SELECTION_VERSION,'author_scope_path':str(scope_path),'author_scope_sha256':digest(scope_raw),
              'coordinator_sha256':digest(Path(__file__).read_bytes().replace(b'\r\n',b'\n')),
              'view_id':view['id'],'view_payload_hash':view['payload_hash'],'previous_positions':prior_bindings,
              'maximum_additional_dispatches':6,'maximum_cumulative_dispatches':8,
              'eligible_fragments':len(candidates),'pairs':[],'positions':[],'new_prompt_authored':False}
    for pair_index in range(3):
        directory=output/('pair-'+str(pair_index+1));directory.mkdir()
        manifest={k:v for k,v in baseline.items() if k!='batches'}
        manifest.update(batches=[],status='frozen_under_author_coverage_scope',parent_scope_sha256=digest(scope_raw))
        for local_position,candidate in enumerate(selected[pair_index*2:pair_index*2+2],1):
            global_position=3+pair_index*2+local_position-1
            rows=candidate['members'];messages=candidate['messages']
            public={'base_url':'https://api.deepseek.com','name':'deepseek-flash','timeout':120,'max_tokens':1024,'_json_output':True,
                    '_dialogue_profile':{'version':'natural-dialogue-v8','seed':'CORPUS-PREVIEW-'+str(local_position),'temperature':.2}}
            request,contract=build_request(public,messages)
            packet={'version':'existing-e2-learning-visible-messages-v1','batch':local_position,'view_id':view['id'],
                    'fragment_id':candidate['fragment_id'],'split':'learn','messages':messages,
                    'source_map':[{'source_id':n,'message_id':r['message_id'],'role':r['role'],'source_text_hash':r['source_text_hash'],'view_text_hash':r['text_hash']} for n,r in enumerate(rows,1)],
                    'approved_under_parent_scope':True,'dispatch_ready':False}
            packet_path=directory/f'batch-{local_position}.private.json';atomic_json(packet_path,packet)
            wire_path=directory/f'batch-{local_position}.wire-preview.private.json'
            atomic_json(wire_path,{'status':'frozen_under_author_coverage_scope','request':request,'contract':contract,'credentials_loaded':False})
            manifest['batches'].append({'batch':local_position,'sources':len(rows),'file':packet_path.name,'sha256':digest(packet_path.read_bytes()),
                'wire_file':wire_path.name,'wire_sha256':digest(wire_path.read_bytes()),'fragment_id':candidate['fragment_id']})
            envelope['positions'].append({k:v for k,v in candidate.items() if k not in ('members','messages')}|{
                'global_position':global_position,'pair':pair_index+1,'local_position':local_position,
                'request_hash':fingerprint(request),'packet_sha256':digest(packet_path.read_bytes()),'wire_sha256':digest(wire_path.read_bytes())})
        manifest_path=directory/'manifest.private.json';atomic_json(manifest_path,manifest)
        approval={'version':'corpus-learning-approval-v1','date':scope['date'],'author_message':scope['author_message'],
                  'view_id':view['id'],'view_payload_hash':view['payload_hash'],'preview_manifest_sha256':digest(manifest_path.read_bytes()),
                  'allowed_provider':'deepseek_official','allowed_model':'deepseek-flash','max_dispatches':2,
                  'new_prompt_writing_authorized':False,'automatic_asset_publication':False,
                  'parent_coverage_scope_sha256':digest(scope_raw),'individual_input_pre_read':False,
                  'authority_basis':'inherited six-position expansion scope for approved data and unchanged template',
                  'batches':[{'position':b['batch'],'file_sha256':b['sha256'],'wire_sha256':b['wire_sha256']} for b in manifest['batches']]}
        approval_path=directory/'author-approval.private.json';atomic_json(approval_path,approval)
        envelope['pairs'].append({'pair':pair_index+1,'directory':directory.name,'manifest_sha256':digest(manifest_path.read_bytes()),
                                  'approval_sha256':digest(approval_path.read_bytes())})
    atomic_json(envelope_path,envelope)
    return envelope


def validate_envelope(corpus,learning,envelope_path):
    path=Path(envelope_path).resolve();root=path.parent
    envelope=json.loads(path.read_text(encoding='utf-8'))
    scope_raw=Path(envelope['author_scope_path']).read_bytes();scope=json.loads(scope_raw)
    if (envelope['version']!=SELECTION_VERSION or envelope['author_scope_sha256']!=digest(scope_raw)
            or envelope.get('coordinator_sha256')!=digest(Path(__file__).read_bytes().replace(b'\r\n',b'\n'))
            or scope.get('additional_request_limit')!=6 or not scope.get('first_batch_value_confirmed')
            or scope.get('template_changes_authorized') is not False or scope.get('automatic_asset_publication') is not False
            or scope.get('view_id')!=envelope['view_id'] or scope.get('view_payload_hash')!=envelope['view_payload_hash']
            or envelope.get('maximum_additional_dispatches')!=6 or envelope.get('maximum_cumulative_dispatches')!=8
            or len(envelope['pairs'])!=3 or len(envelope['positions'])!=6
            or {p['global_position'] for p in envelope['positions']}!=set(range(3,9))
            or len({p['fragment_id'] for p in envelope['positions']})!=6
            or len({p['group_id'] for p in envelope['positions']})!=6):
        raise CorpusError('coverage_envelope_invalid')
    if ({p['pair'] for p in envelope['pairs']}!={1,2,3}
            or {(p['pair'],p['local_position']) for p in envelope['positions']}!={(a,b) for a in (1,2,3) for b in (1,2)}
            or any(p['global_position']!=2+2*(p['pair']-1)+p['local_position'] for p in envelope['positions'])):
        raise CorpusError('coverage_position_layout_invalid')
    hashes=[p['request_hash'] for p in envelope['previous_positions']]+[p['request_hash'] for p in envelope['positions']]
    if len(hashes)!=8 or len(set(hashes))!=8:
        raise CorpusError('coverage_duplicate_model_input')
    view=corpus.view_status(envelope['view_id'])
    if view['payload_hash']!=envelope['view_payload_hash']:raise CorpusError('coverage_view_changed')
    with learning.connection() as db:
        for previous in envelope['previous_positions']:
            row=learning._row(db,previous['id'])
            if any(row[k]!=v for k,v in previous.items()) or row['state']!='completed':
                raise CorpusError('coverage_previous_result_changed')
    for pair in envelope['pairs']:
        directory=(root/pair['directory']).resolve()
        if not directory.is_relative_to(root):raise CorpusError('coverage_path_invalid')
        if (digest((directory/'manifest.private.json').read_bytes())!=pair['manifest_sha256']
                or digest((directory/'author-approval.private.json').read_bytes())!=pair['approval_sha256']):
            raise CorpusError('coverage_frozen_material_changed')
        approval=json.loads((directory/'author-approval.private.json').read_text(encoding='utf-8'))
        manifest=json.loads((directory/'manifest.private.json').read_text(encoding='utf-8'))
        if approval.get('parent_coverage_scope_sha256')!=digest(scope_raw) or manifest.get('parent_scope_sha256')!=digest(scope_raw):
            raise CorpusError('coverage_child_authority_changed')
        for record in manifest['batches']:
            position=next(p for p in envelope['positions'] if p['pair']==pair['pair'] and p['local_position']==record['batch'])
            packet=json.loads((directory/record['file']).read_text(encoding='utf-8'))
            wire=json.loads((directory/record['wire_file']).read_text(encoding='utf-8'))
            if (digest((directory/record['file']).read_bytes())!=position['packet_sha256']
                    or digest((directory/record['wire_file']).read_bytes())!=position['wire_sha256']
                    or packet['fragment_id']!=position['fragment_id'] or len(packet['source_map'])!=position['source_count']
                    or fingerprint(wire['request'])!=position['request_hash'] or wire['request']['messages']!=packet['messages']):
                raise CorpusError('coverage_material_binding_changed')
    return envelope


def prepare_coverage(corpus,learning,envelope_path):
    envelope=validate_envelope(corpus,learning,envelope_path);root=Path(envelope_path).resolve().parent
    state_path=root/'prepared-state.private.json'
    if state_path.exists():
        state=json.loads(state_path.read_text(encoding='utf-8'))
        if state['envelope_sha256']!=digest(Path(envelope_path).read_bytes()):raise CorpusError('coverage_prepared_scope_changed')
        return state
    jobs=[]
    for pair in envelope['pairs']:
        directory=root/pair['directory'];approval=learning.register_approval(directory/'author-approval.private.json',directory)
        for local in (1,2):
            position=next(p for p in envelope['positions'] if p['pair']==pair['pair'] and p['local_position']==local)
            job=learning.prepare(approval['id'],local,'COVERAGE:'+envelope['author_scope_sha256']+':'+str(position['global_position']))
            jobs.append({'global_position':position['global_position'],'pair':pair['pair'],'local_position':local,
                         'job_id':job['id'],'approval_id':approval['id']})
    state={'version':'coverage-prepared-v1','envelope_sha256':digest(Path(envelope_path).read_bytes()),'ledger':str(learning.root),'jobs':jobs}
    atomic_json(state_path,state)
    return state


def verify_prepared(corpus,learning,envelope_path):
    envelope=validate_envelope(corpus,learning,envelope_path);root=Path(envelope_path).resolve().parent
    state=json.loads((root/'prepared-state.private.json').read_text(encoding='utf-8'))
    if (state['envelope_sha256']!=digest(Path(envelope_path).read_bytes()) or len(state['jobs'])!=6
            or Path(state['ledger']).resolve()!=learning.root
            or {j['global_position'] for j in state['jobs']}!=set(range(3,9))):
        raise CorpusError('coverage_prepared_scope_changed')
    expected={p['id'] for p in envelope['previous_positions']}|{j['job_id'] for j in state['jobs']}
    with learning.connection() as db:
        if {r[0] for r in db.execute('SELECT id FROM learning_jobs')}!=expected:raise CorpusError('coverage_unexpected_ledger_positions')
        for job in state['jobs']:
            row=learning._row(db,job['job_id']);position=next(p for p in envelope['positions'] if p['global_position']==job['global_position'])
            if row['request_hash']!=position['request_hash'] or row['approval_id']!=job['approval_id'] or row['position']!=job['local_position']:
                raise CorpusError('coverage_job_binding_changed')
    return envelope,state


async def run_coverage(corpus,learning,envelope_path,credentials,on_record=None):
    envelope,state=verify_prepared(corpus,learning,envelope_path)
    root=Path(envelope_path).resolve().parent;results=[];consecutive_errors=0
    for job in sorted(state['jobs'],key=lambda j:j['global_position']):
        verify_prepared(corpus,learning,envelope_path)
        before=learning.status(job['job_id'])
        result=await learning.run(job['job_id'],credentials)
        results.append({'global_position':job['global_position'],'status':result})
        atomic_json(root/'run-state.private.json',{'envelope_sha256':state['envelope_sha256'],'results':results})
        if on_record:on_record(job['global_position'],result)
        newly_attempted=before['attempt_id'] is None and result['attempt_id'] is not None
        failed=newly_attempted and result['http_status'] is not None and result['http_status']!=200
        if newly_attempted:consecutive_errors=consecutive_errors+1 if failed else 0
        if (result['state'] in ('unknown','stale','cancelled')
                or newly_attempted and result['http_status'] in (401,403,429) or consecutive_errors>=3):break
    return results


class PairReview:
    def __init__(self,learning,jobs):self.learning=learning;self.jobs=jobs
    def inspect(self):return {'jobs':[self.learning.status(j['job_id']) for j in self.jobs]}
    def _guard(self,*args):return self.learning._guard(*args)
    def connection(self,*args,**kwargs):return self.learning.connection(*args,**kwargs)
    def _row(self,*args):return self.learning._row(*args)


def export_coverage(corpus,learning,envelope_path,output):
    envelope,state=verify_prepared(corpus,learning,envelope_path)
    output=outside_repository(output)
    if output.exists() and any(output.iterdir()):raise CorpusError('coverage_review_empty_target_required')
    output.mkdir(parents=True,exist_ok=True);pairs=[]
    for pair in range(1,4):
        jobs=[j for j in state['jobs'] if j['pair']==pair]
        result=export_learning_review(PairReview(learning,jobs),output/('pair-'+str(pair)))
        page=output/('pair-'+str(pair))/'index.html';body=page.read_text(encoding='utf-8')
        label='E.L3扩大覆盖 · 组'+str(pair)+'（全局位置'+str(jobs[0]['global_position'])+'、'+str(jobs[1]['global_position'])+'）'
        page.write_text(body.replace('<title>E.L3首批真实表达学习结果</title>','<title>'+label+'</title>')
                            .replace('<h1>E.L3首批真实表达学习结果</h1>','<h1>'+label+'</h1>'),encoding='utf-8')
        pairs.append({'pair':pair,'jobs':jobs,'result':result})
    body='<h1>E.L3扩大覆盖结果</h1><p>本轮新增六个固定位置，沿用已批准的数据版本、模板和参数。提案均待审，未自动发布；首批结果保持。</p><table><thead><tr><th>全局位置</th><th>月份</th><th>原话</th><th>A／B</th><th>状态</th><th>提案</th></tr></thead><tbody>'
    statuses={j['global_position']:learning.status(j['job_id']) for j in state['jobs']}
    for position in envelope['positions']:
        status=statuses[position['global_position']]
        body+='<tr>'+''.join('<td>'+escape(str(value))+'</td>' for value in (position['global_position'],position['month'],position['source_count'],str(position['role_counts'].get('A',0))+'／'+str(position['role_counts'].get('B',0)),status['state'],status['proposal_count']))+'</tr>'
    body+='</tbody></table><nav>'+''.join('<a href="pair-'+str(n)+'/index.html">组'+str(n)+'：提案、原文与首次回执</a>' for n in range(1,4))+'</nav><p>时间、片段长度和双方轮次属于结构覆盖，不冒充已覆盖所有话题或整体人物体验通过。</p>'
    css='body{font:16px/1.7 system-ui,Microsoft YaHei,sans-serif;background:#f5f7f6;color:#173238}main{max-width:1050px;margin:24px auto;padding:24px;background:white}table{display:block;max-width:100%;overflow:auto;border-collapse:collapse}td,th{padding:9px;border:1px solid #dbe5e1}nav{display:flex;flex-wrap:wrap;gap:16px;margin:24px 0}a{color:#087578}@media(max-width:650px){main{margin:8px;padding:14px}}'
    (output/'index.html').write_text('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>E.L3扩大覆盖结果</title><style>'+css+'</style></head><body><main>'+body+'</main></body></html>',encoding='utf-8')
    result={'version':SELECTION_VERSION,'envelope_sha256':digest(Path(envelope_path).read_bytes()),'pairs':pairs,
            'new_positions':6,'new_sources':sum(p['source_count'] for p in envelope['positions']),
            'dispatch_attempts':sum(s['attempt_id'] is not None for s in statuses.values()),
            'complete_responses':sum(s['raw_response_hash'] is not None for s in statuses.values()),
            'proposals':sum(s['proposal_count'] for s in statuses.values()),
            'known_tokens':sum(p['result']['known_total_tokens'] for p in pairs),
            'unknown_usage_jobs':sum(p['result']['unknown_usage_jobs'] for p in pairs),
            'assets_published':0,'actual_billing_cny':None}
    atomic_json(output/'manifest.private.json',result)
    return result|{'review':str(output/'index.html')}


def main(argv=None):
    import argparse,asyncio,sqlite3
    from persona_runtime.corpus_dataset import DatasetCorpusStore
    from persona_runtime.corpus_learning import CorpusLearning
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,required=True);parser.add_argument('--ledger',type=Path,required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    freeze=sub.add_parser('freeze')
    for name in ('scope','baseline-preview','previous-state','output'):freeze.add_argument('--'+name,type=Path,required=True)
    for name in ('prepare','verify','run','report'):
        action=sub.add_parser(name);action.add_argument('--envelope',type=Path,required=True)
        if name=='run':action.add_argument('--credential-home',type=Path,required=True)
        if name=='report':action.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        corpus=DatasetCorpusStore(args.corpus);learning=CorpusLearning(args.ledger,corpus)
        if args.action=='freeze':
            value=freeze_coverage(corpus,learning,args.scope,args.baseline_preview,args.previous_state,args.output)
            result={'frozen_positions':len(value['positions']),'eligible_fragments':value['eligible_fragments'],'envelope':str(args.output/'envelope.private.json')}
        elif args.action=='prepare':
            value=prepare_coverage(corpus,learning,args.envelope);result={'prepared_positions':len(value['jobs'])}
        elif args.action=='verify':
            value,state=verify_prepared(corpus,learning,args.envelope);result={'passed':True,'frozen_positions':len(value['positions']),'prepared_positions':len(state['jobs'])}
        elif args.action=='report':result=export_coverage(corpus,learning,args.envelope,args.output)
        else:
            from persona_runtime.config import ConfigStore
            from persona_runtime.paths import RuntimePaths
            credentials=ConfigStore(RuntimePaths(args.credential_home.resolve())).load()['model']
            def record(position,status):
                print(json.dumps({'position':position,'state':status['state'],'http_status':status['http_status'],
                                  'proposals':status['proposal_count'],'usage':status['usage']},ensure_ascii=False),flush=True)
            value=asyncio.run(run_coverage(corpus,learning,args.envelope,credentials,record))
            result={'recorded_positions':len(value),'dispatch_attempts':learning.inspect()['dispatch_attempts']}
    except CorpusError as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False));return 2
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
        print(json.dumps({'status':'failed','error':'coverage_local_operation_failed'},ensure_ascii=False));return 2
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
