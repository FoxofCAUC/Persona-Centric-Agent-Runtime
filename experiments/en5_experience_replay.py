"""Replay only the approved E.N5 bubbles through the bound, mapped native Workbench."""
import argparse
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import UUID

from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.en5_display import read_receipts
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from .corpus_reply_study import read_private, save_once


def require(ok, code):
    if not ok:
        raise RuntimeErrorBase('en5_replay_'+code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def plan_for(material, binding, part, checksum):
    require(part in (1,2) and binding['mode']=='author_material_replay', 'mode')
    require([s['part'] for s in material['sessions']]==[1,2], 'two_parts_required')
    turns=material['sessions'][part-1]['turns']
    require(len(turns)==15,'turn_count')
    positions=[]
    for n,turn in enumerate(turns,1):
        bubbles=turn['bubbles']
        require(turn['n']==n and isinstance(bubbles,list) and
                0<len(bubbles)<=20 and '\n'.join(b['text'] for b in bubbles)==turn['text'],'input_shape')
        task=turn.get('public_task')
        require(task is None or isinstance(task,dict) and isinstance(task.get('question'),str) and
                task['question'].strip(), 'public_question')
        if task is not None:
            from persona_runtime.knowledge_native import public_task
            canonical=public_task(task)
            require(task['question']==canonical['question'],'public_question_changed')
            task=canonical
        group=[]
        for i,bubble in enumerate(bubbles,1):
            body=bubble['text']
            require(isinstance(body,str) and bool(body.strip()),'empty_bubble')
            token=fingerprint([binding['study_id'],binding['instance_id'],checksum,part,n,i])
            group.append({'request_id':str(UUID(token[:32],version=4)), 'n':n,'i':i,'text':body,
                          'public_task':task if i==len(bubbles) else None})
        positions.append(group)
    return positions


def db_path(paths):
    return paths.directories()['database']/'runtime.sqlite3'


def snapshot(paths,binding,sid,plan):
    """Join only the designated session and compare every accepted input with the frozen prefix."""
    flat=[b for group in plan for b in group]
    with closing(sqlite3.connect(db_path(paths).as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        rows=db.execute("""SELECT i.request_id,i.batch_id,e.text,x.payload_json,b.state,b.deadline_epoch
             FROM ingress_receipts i JOIN events e ON e.id=i.event_id
             JOIN raw_events x ON x.id=i.raw_event_id JOIN input_batches b ON b.id=i.batch_id
             WHERE i.session_id=? AND i.persona_id=? AND i.channel='desktop' ORDER BY i.accepted_seq""",
             (sid,binding['persona_id'])).fetchall()
        require(len(rows)<=len(flat),'unexpected_input')
        accepted={}
        batches={}
        for row,b in zip(rows,flat):
            payload=json.loads(row['payload_json'])
            require(row['request_id']==b['request_id'] and row['text']==b['text'] and payload['text']==b['text']
                    and payload.get('public_task')==b['public_task'] and not payload.get('reference')
                    and payload.get('desktop_presentation')=='tk-visible-v1','input_prefix_mismatch')
            batch=row['batch_id']
            require(batches.setdefault(b['n'],batch)==batch, 'split_turn')
            accepted[b['request_id']]=dict(row)
        require(len(set(batches.values()))==len(batches),'merged_turn')
        turns={}
        for t in db.execute("""SELECT t.id,t.input_batch_id,t.status,t.cancelled_at,t.error_code,
                             t.reply_event_id,e.text FROM turns t LEFT JOIN events e ON e.id=t.reply_event_id
                             WHERE t.session_id=? AND t.persona_id=? ORDER BY t.rowid""",(sid,binding['persona_id'])):
            require(t['input_batch_id'] in batches.values() and t['input_batch_id'] not in turns,'foreign_turn')
            turns[t['input_batch_id']]=dict(t)
    return accepted,batches,turns


def intent(root,binding,sid,part,b,checksum):
    path=root/'replay-intents'/f'{part}-{b["n"]:02d}-{b["i"]:02d}.private.json'
    fields={'study_id':binding['study_id'],'instance_id':binding['instance_id'],'session_id':sid,
            'part':part, 'material_sha256':checksum,'request_id':b['request_id'],'text_sha256':sha(b['text'].encode()),
            'public_task_sha256':fingerprint(b['public_task'])}
    return path,fields


def completed(visible,turn):
    if not turn:return False
    require(turn['status'] not in ('failed','interrupted') and not turn['cancelled_at'],'prior_turn_stopped')
    if turn['status']!='completed':return False
    shown=visible.get(turn['id'],{}).get('main')
    if not shown:return False
    require(shown['source_id']==turn['reply_event_id'] and shown['body_sha256']==sha(turn['text'].encode()),
            'main_display_changed')
    return True


def read_progress(root,paths,binding,part,plan,checksum):
    sid=binding['sessions'][part-1]
    accepted,batches,turns=snapshot(paths,binding,sid,plan)
    visible=read_receipts(paths,binding['study_id'],binding['persona_id'],sid)
    for row in (b for group in plan for b in group if b['request_id'] in accepted):
        path,value=intent(root,binding,sid,part,row,checksum)
        require(path.is_file() and read_private(path)==value,'unbound_prior_input')
    finished=0
    for n,group in enumerate(plan,1):
        if not all(b['request_id'] in accepted for b in group):break
        turn=turns.get(batches[n])
        if not completed(visible,turn):break
        finished+=1
    return finished,accepted,batches,turns


async def drive(root,material,checksum,guard,window,app,binding,part):
    paths=window.paths
    sid=binding['sessions'][part-1]
    plan=plan_for(material,binding,part,checksum)
    def active():
        require(not window.closed and (window.persona_id,window.session_id)==(binding['persona_id'],sid),'desktop_scope_changed')
    async def wait(predicate):
        while True:
            active()
            value=predicate()
            if value:return value
            await asyncio.sleep(.08)
    def progress():
        return read_progress(root,paths,binding,part,plan,checksum)
    for n,group in enumerate(plan,1):
        guard()
        done,accepted,batches,turns=progress()
        if n<=done:continue
        require(done==n-1,'prior_position_not_displayed')
        for i,b in enumerate(group):
            rid=b['request_id']
            if rid in accepted:continue
            active()
            await wait(lambda:window.current_page=='chat' and window.transcript.winfo_viewable()
                       and not window.tray_hidden and window.state() not in ('iconic','withdrawn') and window.chat_persona.current()>=0
                       and not any(op.resources & {'send','chat','persona','chat-cancel','emergency'} for op in window.hub.active.values()))
            require(not window.chat_input.get('1.0','end-1c') and not window.chat_reference
                    and not window.chat_public_task,'desktop_draft_changed')
            guard()
            done,accepted,batches,turns=progress()
            require(rid not in accepted,'concurrent_input')
            if i:
                prior=accepted[group[i-1]['request_id']]
                require(prior['state']=='collecting' and time.time()<prior['deadline_epoch'],'partial_batch_closed')
            path,value=intent(root,binding,sid,part,b,checksum)
            # A saved click without a receipt cannot be safely reconstructed after a crash.
            require(not path.exists(),'submitted_position_unknown')
            if b['public_task'] is not None:
                window.chat_public_task={'persona_id':binding['persona_id'],'session_id':sid,'task':b['public_task']}
            window.chat_input.insert('1.0',b['text'])
            require(window.chat_input.get('1.0','end-1c')==b['text'],'input_widget_changed')
            save_once(path,value)
            operation=window.send_chat(request_id=rid)
            require(operation is not None,'desktop_submit_rejected')
            def received():
                require(operation.state!='failed','submission_unknown_or_rejected')
                if operation.state!='completed':return False
                row=snapshot(paths,binding,sid,plan)[0].get(rid)
                require(row is not None,'submission_receipt_missing')
                draft=window.chat_input.get('1.0','end-1c')
                require(draft in ('',b['text']),'desktop_draft_changed')
                return not draft
            await wait(received)
            _,accepted,batches,turns=progress()
        def displayed():
            received_rows,batches,turns=snapshot(paths,binding,sid,plan)
            turn=turns.get(batches[n])
            if not turn:
                require(received_rows[group[-1]['request_id']]['state'] not in ('cancelled','interrupted_buffer'),
                        'batch_stopped')
                return False
            has=completed(read_receipts(paths,binding['study_id'],binding['persona_id'],sid),turn)
            if not has and turn['status']=='completed' and window.current_page=='chat':
                # Bring the original reply into the viewport after a window restart.
                span=window.transcript.tag_ranges('en5-main-'+turn['id'])
                if span and not window.transcript.bbox(span[0]):window.transcript.see(span[0])
            return has
        await wait(displayed)
    done,_,_,_=progress()
    require(done==15,'part_incomplete')
    return {'study_id':binding['study_id'],'instance_id':binding['instance_id'],'material_sha256':checksum,
            'session_id':sid,'part':part,'displayed_turns':done}


async def run(root,part=None):
    from .en5_experience_setup import binding_at, run_desktop, validate_use
    from .en5_experience_material import verify_sources
    root=outside_repository(root)
    with closing(InstanceLock(root/'en5-replay.lock')):
        paths,binding=binding_at(root)
        require(binding['mode']=='author_material_replay' and (part is None or part in (1,2)),'wrong_run_scope')
        raw=(root/'materials.private.json').read_bytes()
        checksum=sha(raw)
        material=json.loads(raw)
        def guard():
            validate_use(root,material_sha256=checksum,require_live=True)
            require(verify_sources(root)==material,'reviewed_sources_changed')
        guard()
        results=[]
        for number in ((part,) if part else (1,2)):
            plan=plan_for(material,binding,number,checksum)
            finished,*_=read_progress(root,paths,binding,number,plan,checksum)
            sid=binding['sessions'][number-1]
            if finished==15:
                result={'study_id':binding['study_id'],'instance_id':binding['instance_id'],'material_sha256':checksum,
                        'session_id':sid,'part':number,'displayed_turns':finished}
            else:
                async def driver(window,app,current,active_part):
                    return await drive(root,material,checksum,guard,window,app,current,active_part)
                try:
                    result=await run_desktop(root,number,driver,material_sha256=checksum)
                except (Exception,asyncio.CancelledError) as error:
                    code=str(error) if isinstance(error,RuntimeErrorBase) else type(error).__name__
                    # Store only a stable reason (no transport payload, credentials or private text).
                    if not all(ch.isascii() and (ch.isalnum() or ch=='_') for ch in code):
                        code=type(error).__name__
                    with closing(sqlite3.connect(db_path(paths).as_uri()+'?mode=ro',uri=True)) as db:
                        counts=db.execute('SELECT count(*),count(DISTINCT batch_id) FROM ingress_receipts WHERE session_id=? AND persona_id=?',
                                          (sid,binding['persona_id'])).fetchone()
                    stop={'session_id':sid,'part':number,'material_sha256':checksum,
                          'reason':code,'accepted_bubbles':counts[0],'batches':counts[1]}
                    save_once(root/'replay-stops'/(fingerprint(stop)+'.private.json'),stop)
                    raise
            save_once(root/f'replay-part-{number}.private.json',result)
            results.append(result)
        return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--part',type=int,choices=(1,2))
    args=parser.parse_args()
    print(json.dumps(asyncio.run(run(args.root,args.part)),ensure_ascii=False))
