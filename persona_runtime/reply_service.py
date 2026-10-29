"""Read-only administrator previews for supplied output or an owned saved batch."""
import json
from .models import RuntimeErrorBase
from .reply_transport import preview


class ReplyService:
    def __init__(self, store):
        self.store=store

    def preview(self, persona_id, raw=None, mode='natural_structured', batch_id=None):
        with self.store.connection() as conn:
            if not conn.execute('SELECT 1 FROM personas WHERE id=?',(persona_id,)).fetchone():
                raise RuntimeErrorBase('persona_not_found')
            if batch_id:
                batch=conn.execute('SELECT * FROM reply_batches WHERE id=? AND persona_id=?',(batch_id,persona_id)).fetchone()
                if not batch:raise RuntimeErrorBase('reply_batch_not_found')
                segments=conn.execute('SELECT * FROM reply_segments WHERE batch_id=? ORDER BY segment_index',(batch_id,)).fetchall()
                mode=batch['mode']
                if batch['schema_version']==2:
                    raw=json.dumps({'schema_version':2,'behavior_intent':batch['behavior_intent'],'messages':[
                        {'content':s['text'],'content_kind':s['content_kind'],'cancel_if_new_input':bool(s['cancel_if_new_input'])} for s in segments]})
                elif mode=='context_structured':
                    raw=json.dumps({'schema_version':1,'segments':[{'text':s['text'],'format':s['format']} for s in segments]})
                else:raw='\n\n'.join(s['text'] for s in segments)
            if not isinstance(raw,str) or len(raw)>100000:raise RuntimeErrorBase('invalid_preview_input')
            result=preview(raw,mode)
            if batch_id:
                result.update(batch_id=batch_id,raw_fingerprint=batch['raw_fingerprint'],fallback_reason=batch['fallback_reason'])
                result['historical_parts']=[{k:r[k] for k in ('id','text','semantic_index','state','platform_message_id','cancel_reason','trigger_event_id')} for r in conn.execute('SELECT * FROM reply_delivery_parts WHERE reply_batch_id=? ORDER BY part_index',(batch_id,))]
            return result
