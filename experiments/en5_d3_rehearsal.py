"""Run the D3 workbench locally with transport markers or replay the saved 2-14 first responses."""
import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
from html import escape
import json
from math import ceil
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
import time

from aiohttp import web

from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.chat import ModelCompletion
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.instance import InstanceLock
from persona_runtime.models import generic_persona
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import snapshot
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import select
from persona_runtime.storage import Store
from .corpus_reply_study import read_private,save_once
from .corpus_reply_trial_report import save_text_once
from .en5_decision_contract import require
from .en5_decision_packet import file_hash
from .en5_d3_desktop import local_app
from .en5_d3_presentation import D3Workbench,close_local_window,frames


class LocalResponses:
    d3_offline_fixture=True

    def __init__(self, script):
        self.script=script
        self.count=0

    async def complete(self, model, messages):
        n=self.count
        require(n<len(self.script) and messages[-1]['content']==self.script[n][0],'local_main_order')
        if n:
            require({'role':'assistant','content':self.script[n-1][1]} in messages,'local_history_missing')
        self.count+=1
        return ModelCompletion(self.script[n][1], {}, {'response_model':'local-receipt-playback','finish_reason':'stop'})


class LocalFields:
    d3_offline_fixture=True

    def __init__(self):
        self.count=0

    async def complete(self, model, wire, *, save_raw):
        self.count+=1
        target=json.loads(wire['messages'][-1]['content'])['id']
        choice={'target':target,'action':'参与交流','task_state':'无待办'}
        raw={'model':'deepseek-flash','choices':[{'index':0,'finish_reason':'stop',
             'message':{'content':json.dumps(choice,ensure_ascii=False)}}]}
        save_raw(json.dumps(raw).encode(),http_status=200,diagnostics={'origin':'local_fixture'})


async def wait_for(window, predicate, seconds=15):
    async with asyncio.timeout(seconds):
        while True:
            window.update()
            if predicate():return
            await asyncio.sleep(.016)


async def play(script, branch, display, title, show=False):
    with TemporaryDirectory() as tmp:
        paths=RuntimePaths(Path(tmp)); cfg=ConfigStore(paths); cfg.initialize()
        cfg.update({'model':{'base_url':'https://api.deepseek.com','name':'deepseek-v4-pro','hourly_limit':1000}})
        store=Store(paths);store.initialize();service=RuntimeService(store)
        pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
        with store.connection(write=True) as db:
            sid=select(db,pid,'desktop',None)
            profile=fingerprint(snapshot(db,pid))
        binding={'version':'d3-local-workbench-v1','mode':'local_fixture','home':str(paths.home.resolve()),
                 'instance_id':cfg.load()['instance_id'],'persona_id':pid,'profile_hash':profile,
                 'character_version':CharacterService(store).current(pid)['id'],
                 'sessions':{sid:{'branch':branch,'display':display,'inputs':[[fingerprint(t)] for t,_ in script]}}}
        main,flash=LocalResponses(script),LocalFields()
        app,ledger=local_app(paths,binding,main,flash,{'flash':'D3-F','pro':'D3-P'})
        runner=web.AppRunner(app,access_log=None)
        await runner.setup()
        window=None
        records=[]
        try:
            await web.TCPSite(runner,'127.0.0.1',0).start()
            cfg.update({'service_port':runner.addresses[0][1]})
            window=D3Workbench(paths,ledger)
            window.title(title+' · 本地工作台回放')
            window.geometry('940x680'+('+120+80' if show else '+20000+20000'))
            errors=[]
            window.report_callback_exception=lambda *exc: errors.append(str(exc[1]))
            window.show_page('chat');window.load_chat()
            await wait_for(window,lambda:window.session_id==sid and window.chat_persona.current()>=0)
            for n,(user,body) in enumerate(script,1):
                request_id=f'd3-local-{n}'
                window.chat_input.insert('1.0',user)
                window.send_chat(request_id=request_id)
                def accepted():
                    with store.connection() as db:
                        return db.execute('SELECT batch_id FROM ingress_receipts WHERE request_id=?',(request_id,)).fetchone()
                await wait_for(window,lambda:not window.chat_input.get('1.0','end-1c') and accepted())
                bid=accepted()[0]
                batch=next(b for b in app['ingress'].list(pid,channel='desktop') if b['id']==bid)
                if batch['status']=='collecting':
                    app['ingress'].action(bid,'seal',batch['revision'],request_id+'-seal',pid)
                def completed():
                    with store.connection() as db:
                        row=db.execute('SELECT id,status,error_code FROM turns WHERE input_batch_id=?',(bid,)).fetchone()
                        if row and row['status'] in ('interrupted','failed'):
                            raise RuntimeError(row['error_code'])
                        return row and row['status']=='completed'
                await wait_for(window,completed)
                with store.connection() as db:
                    turn=dict(db.execute('SELECT * FROM turns WHERE input_batch_id=?',(bid,)).fetchone())
                    accepted_at=db.execute('SELECT first_epoch FROM input_batches WHERE id=?',(bid,)).fetchone()[0]
                window.load_chat(session_id=sid)
                wanted=frames(body,display=='bubbles')
                def shown():
                    with ledger.db() as db:
                        return [dict(row) for row in db.execute('SELECT idx,at FROM visible WHERE turn=? ORDER BY idx',(turn['id'],))]
                await wait_for(window,lambda:len(shown())==len(wanted))
                with ledger.db() as db:
                    saved=json.loads(db.execute('SELECT frames FROM plans WHERE turn=?',(turn['id'],)).fetchone()[0])
                    d=db.execute('SELECT reason FROM decisions WHERE turn=?',(turn['id'],)).fetchone()
                require(''.join(x['text'] for x in saved)==body and not errors,'desktop_paint_failed')
                receipts=shown()
                require([x['idx'] for x in receipts]==list(range(len(saved))),'desktop_frame_order')
                segments=[{'text':f['text'],'planned_delay_ms':f['delay_ms'],'visible_epoch':v['at'],
                           'since_first_s':round(v['at']-receipts[0]['at'],3)}
                          for f,v in zip(saved,receipts,strict=True)]
                records.append({'turn':n,'turn_id':turn['id'],'source_event':turn['reply_event_id'],
                                'body_sha256':digest(body.encode()),'accepted_epoch':accepted_at,
                                'first_visible_s':round(receipts[0]['at']-accepted_at,3),'bubbles':segments,
                                'decision':d['reason'] if d else 'baseline'})
                if show:await wait_for(window,lambda t=time.monotonic()+1.4:time.monotonic()>=t)
            require(main.count==len(script) and flash.count==(len(script) if branch=='candidate' else 0),'local_count')
        finally:
            try:
                if window is not None:
                    await close_local_window(window)
            finally:
                await runner.cleanup()
        return {'title':title,'branch':branch,'mode':display,'local_main':main.count,'local_flash':flash.count,'turns':records}


def source_case(root):
    state=read_private(root.parent/'prompt-review-state.json')
    note_path=outside_repository(state['en5_decision_d1_2_14_clarification'])
    require(file_hash(note_path)==state['en5_decision_d1_2_14_clarification_sha256'],'author_clarification_changed')
    note=read_private(note_path)
    d1_path=root/'d1/results.private.json'
    require(file_hash(d1_path)==note['results_sha256'] and note['labels']=={'甲':'A','乙':'B'},'d1_source_changed')
    d1=read_private(d1_path)['results']['2-14']
    original,candidate=(d1[k]['body'] for k in ('A','B'))
    require(digest(candidate.encode())==note['body_sha256']
            and [f['text'] for f in frames(candidate,True)]==[f['text'] for f in note['frames']],'split_changed')
    case=next(c for c in read_private(root/'material.private.json')['cases'] if c['position']=='2-14')
    return case['original']['messages'][-1]['content'],original,candidate,note['results_sha256']


async def playback(root,show=False):
    user,a,b,source=source_case(root)
    panels=[]
    for title,text,mode in [('甲 · 单条',a,'single'),('乙 · 单条',b,'single'),('乙 · 分泡',b,'bubbles')]:
        panels.append(await play([(user,text)],'baseline',mode,title,show=show))
    return {'source_results_sha256':source,'surface':'Workbench Tk transcript','transport':'local_saved_first_body',
            'provider_calls':0,'panels':panels}


def review_document(record):
    html=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
          '<title>2-14 桌面分泡实测</title><style>body{max-width:54rem;margin:auto;padding:1rem;font:16px/1.65 system-ui}article{border-top:1px solid #bacbc1;padding:.7rem 0}.bubble{display:table;background:#e9f2ec;padding:.45rem .7rem;border-radius:.5rem;margin:.4rem 0;white-space:pre-wrap}button{font:inherit;padding:.3rem .6rem}</style>',
          '<h1>2-14 桌面分泡实测</h1><p>工作台本地回放了 D1 保存的首答。点击播放，可按 Tk 聊天区实际记录的显示间隔查看同一句话的阅读节奏。</p>']
    for p in record['panels']:
        turn=p['turns'][0]
        html.extend(['<article><h2>'+escape(p['title'])+'</h2><button type="button">按实测间隔播放</button>'])
        for item in turn['bubbles']:
            html.append(f'<p class="bubble" data-offset="{round(item["since_first_s"]*1000)}">'+escape(item['text'])+'</p>')
        intervals=[round((v['visible_epoch']-u['visible_epoch'])*1000) for u,v in zip(turn['bubbles'],turn['bubbles'][1:])]
        if intervals:html.append('<small>相邻两泡实测间隔　'+'、'.join(f'{n} 毫秒' for n in intervals)+'</small>')
        html.append('</article>')
    html.append("""<script>for(const p of document.querySelectorAll('article')){
    let timers=[];p.querySelector('button').onclick=()=>{timers.forEach(clearTimeout);timers=[];
    for(const b of p.querySelectorAll('.bubble')){b.style.visibility='hidden';
    timers.push(setTimeout(()=>b.style.visibility='visible',Number(b.dataset.offset)));}};}</script></html>""")
    return ''.join(html)


async def run(root,show=False):
    root=outside_repository(root)
    if show:
        await playback(root,True)
        return
    repo=Path(__file__).resolve().parents[1]
    inputs=('experiments/en5_d3_rehearsal.py','experiments/en5_d3_desktop.py','experiments/en5_d3_presentation.py',
            'experiments/en5_decision_contract.py','persona_runtime/chat.py','persona_runtime/desktop.py','persona_runtime/server.py')
    versions={name:digest((repo/name).read_bytes().replace(b'\r\n',b'\n')) for name in inputs}
    folder=root/'d3/workbench'/fingerprint(versions)[:12]
    with closing(InstanceLock(folder/'run.lock')):
        save_once(folder/'code-fingerprints.json',versions)
        saved=folder/'presentation.private.json'
        if not saved.is_file():save_once(saved,await playback(root))
        rec=read_private(saved)
        require(rec['source_results_sha256']==source_case(root)[3],'replay_binding')
        save_text_once(folder/'index.html',review_document(rec))
        engineering=folder/'local-60.private.json'
        if not engineering.exists():
            runs=[]
            for arm in ('baseline','candidate'):
                for part in (1,2):
                    script=[(f'U-{part}-{n:02}',f'{arm.upper()}-{part}-{n:02}') for n in range(1,16)]
                    runs.append(await play(script,arm,'bubbles',f'D3 {arm} {part}'))
            waits=sorted(t['first_visible_s'] for q in runs for t in q['turns'])
            save_once(engineering,{'version':'d3-local-60-v1','transport':'local_markers','provider_calls':0,
                                   'local_main':sum(r['local_main'] for r in runs),
                                   'local_flash':sum(r['local_flash'] for r in runs),'runs':runs,
                                   'first_visible_local_median_s':median(waits),
                                   'first_visible_local_p95_s':waits[ceil(.95*len(waits))-1]})
        return folder/'index.html'


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',required=True,type=Path)
    cli.add_argument('--show-case',action='store_true')
    a=cli.parse_args()
    print(asyncio.run(run(a.root,a.show_case)))
