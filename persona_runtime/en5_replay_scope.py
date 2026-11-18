"""Limit an opted-in experience instance to the exact bubbles authorized for its two sessions."""
import hashlib
import json
from pathlib import Path
from .models import RuntimeErrorBase


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _require(valid):
    if not valid:raise RuntimeErrorBase('en5_replay_input_not_approved')


def replay_request(paths, db, value, persona_id, session_id):
    path=paths.home/'runtime/en5-experience.json'
    if not path.is_file():
        return
    try:
        scope=json.loads(path.read_bytes())
        _require(scope['version']=='en5-experience-v1' and Path(scope['home']).is_absolute() and Path(scope['home']).resolve()==paths.home.resolve()
                 and persona_id==scope['persona_id'] and session_id in scope['sessions']
                 and value.get('channel','desktop')=='desktop' and not value.get('reference'))
        if scope['mode']=='author_desktop':return
        _require(scope['mode']=='author_material_replay' and value.get('desktop_presentation')=='tk-visible-v1')
        from .private_personas import PrivatePersonas
        policy=PrivatePersonas(paths).record_for_persona(persona_id,db=db)
        slots=(paths.home.parent/'approved-slots.private.json').read_bytes()
        use=(paths.home.parent/'author-use.private.json').read_bytes()
        _require(policy and policy.get('state')=='en5_visible_copy' and
                 policy.get('en5_approval_sha256')==_sha(use) and policy.get('en5_slots_sha256')==_sha(slots))
        ledger=json.loads(slots)
        _require(ledger['instance_id']==scope['instance_id'] and ledger['approval_sha256']==_sha(use))
        plan=ledger['sessions'][session_id]
        previous=[r[0] for r in db.execute(
            'SELECT request_id FROM ingress_receipts WHERE session_id=? AND persona_id=? ORDER BY rowid',
            (session_id,persona_id))]
        _require(len(previous)<len(plan) and previous==[p['request_id'] for p in plan[:len(previous)]])
        slot=plan[len(previous)]
        if previous:
            last=db.execute('''SELECT b.state,t.status,t.reply_event_id FROM ingress_receipts r JOIN input_batches b
                ON b.id=r.batch_id LEFT JOIN turns t ON t.input_batch_id=b.id WHERE r.session_id=? AND r.request_id=?''',
                (session_id,previous[-1])).fetchone()
            if slot['i']==1:
                _require(last and last['status']=='completed' and last['reply_event_id'])
            else:
                _require(last and last['state']=='collecting')
        _require(slot['request_id']==value['request_id'] and slot['text_sha256']==_sha(value['text'].encode('utf-8'))
                 and slot['public_task']==value.get('public_task'))
    except (OSError,KeyError,TypeError,ValueError):
        raise RuntimeErrorBase('en5_replay_input_not_approved') from None
