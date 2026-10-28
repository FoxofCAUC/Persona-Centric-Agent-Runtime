"""Observe one actual owner-QQ message; never synthesize or replay ingress."""
import argparse
import json
import re
import time
from scripts.acceptance_m72 import Acceptance
from persona_runtime.config import atomic_json
from persona_runtime.quotas import QuotaService


def plain(text):
    return not re.search(r'\*\*|__|```|(?m:^\s{0,3}#{1,6}\s)|(?m:^\s*[-*+]\s)',text)


def observe(driver):
    driver.online();a=driver.state['personas'][0]
    if 'R08' in driver.state['calls']:raise RuntimeError('R08 already reserved; inspect existing receipt, never repeat automatically')
    original=driver.client.manage('db.status')['current_persona'];cfg=driver.cfg.load()
    with driver.store.connection() as db:cutoff=db.execute('SELECT coalesce(max(rowid),0) FROM turns').fetchone()[0]
    try:
        with QuotaService(driver.store,driver.cfg).acceptance():
            driver.use(a);driver.state['calls']['R08']={'status':'awaiting_user_message'};driver.save()
            atomic_json(driver.out/'qq-ready.json',{'ready':True,'persona_id':a,'marker':'M72-QQ','no_synthetic_ingress':True})
            deadline=time.monotonic()+600;next_health=time.monotonic()+10
            while time.monotonic()<deadline:
                if time.monotonic()>=next_health:driver.online();next_health=time.monotonic()+10
                with driver.store.connection() as db:
                    row=db.execute("SELECT t.id FROM turns t JOIN events e ON e.id=t.user_event_id WHERE t.persona_id=? AND t.channel='qq' AND t.rowid>? AND e.text LIKE 'M72-QQ%' ORDER BY t.rowid LIMIT 1",(a,cutoff)).fetchone()
                if not row:time.sleep(.4);continue
                turn=driver.client.call('GET','/v1/requests/'+row[0])
                if turn['status'] in ('queued','running'):time.sleep(.4);continue
                if turn['status']!='completed':raise RuntimeError('Actual QQ turn failed; do not resubmit')
                with driver.store.connection() as db:
                    parts=[dict(r) for r in db.execute('SELECT id,text,state,platform_message_id,part_index FROM reply_delivery_parts WHERE turn_id=? ORDER BY part_index',(turn['id'],))]
                    raws=[dict(r) for r in db.execute('SELECT r.id,r.event_id,r.channel,r.bot,r.peer,r.platform_message_id,r.representation FROM raw_events r JOIN input_batch_members m ON m.event_id=r.event_id WHERE m.batch_id=? ORDER BY m.member_order',(turn['input_batch_id'],))]
                if not parts or any(p['state'] in ('pending','sending') for p in parts):time.sleep(.4);continue
                manifests=driver.client.manage('context.inspect',persona_id=a,channel='qq',bot=cfg['bot_qq'],peer=cfg['owner_qq'],turn_id=turn['id'])['manifests']
                passed=(turn['attempt_count']==1 and len(raws)==1 and all(r['bot']==cfg['bot_qq'] and r['peer']==cfg['owner_qq'] and r['platform_message_id'] for r in raws)
                        and all(p['state']=='sent' and p['platform_message_id'] and plain(p['text']) for p in parts) and len(manifests)==1)
                atomic_json(driver.out/'R08.json',{'turn':turn,'raw_metadata':raws,'parts':parts,'manifests':manifests,'passed':bool(passed),'platform_readback':'not_performed','user_read':'unknown','amount':'unknown','actual_platform_ingress':True})
                driver.state['calls']['R08'].update(status='completed' if passed else 'acceptance_failed',turn_id=turn['id'],attempts=turn['attempt_count'],usage=turn['usage']);driver.save()
                if not passed:raise RuntimeError('Actual QQ delivery gate failed; do not resend')
                print(json.dumps({'actual_qq_ingress':True,'single_major_attempt':True,'sent_parts':len(parts),'plain_text':True,'user_read':'unknown'}));return
            raise RuntimeError('No actual QQ message observed; no synthetic replacement permitted')
    finally:driver.use(original)


if __name__=='__main__':
    from pathlib import Path
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    observe(Acceptance(parser.parse_args().output))
