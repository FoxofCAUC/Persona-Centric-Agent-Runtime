"""Read-only administrator previews for supplied output or an owned saved batch."""
import json
from .models import RuntimeErrorBase
from .reply_transport import preview


class ReplyService:
    def __init__(self, store):
        self.store=store

    def preview(self, persona_id, raw=None, mode='natural_structured', batch_id=None,presentation_version=None):
        with self.store.connection() as conn:
            if not conn.execute('SELECT 1 FROM personas WHERE id=?',(persona_id,)).fetchone():
                raise RuntimeErrorBase('persona_not_found')
            from .reply_batch import expression_snapshot
            expression=expression_snapshot(mode,version=3) if presentation_version=='model-bubbles-v1' else None
            if batch_id:
                batch=conn.execute('SELECT * FROM reply_batches WHERE id=? AND persona_id=?',(batch_id,persona_id)).fetchone()
                if not batch:raise RuntimeErrorBase('reply_batch_not_found')
                segments=conn.execute('SELECT * FROM reply_segments WHERE batch_id=? ORDER BY segment_index',(batch_id,)).fetchall()
                mode=batch['mode']
                row=conn.execute('SELECT expression_snapshot FROM turns WHERE id=?',(batch['turn_id'],)).fetchone()
                expression=row[0] if row else None
                if batch['schema_version'] in (2,3):
                    raw=json.dumps({'schema_version':batch['schema_version'],'behavior_intent':batch['behavior_intent'],'messages':[
                        {'content':s['text'],'content_kind':s['content_kind'],'cancel_if_new_input':bool(s['cancel_if_new_input'])} for s in segments]})
                elif mode=='context_structured':
                    raw=json.dumps({'schema_version':1,'segments':[{'text':s['text'],'format':s['format']} for s in segments]})
                else:raw='\n\n'.join(s['text'] for s in segments)
            if not isinstance(raw,str) or len(raw)>100000:raise RuntimeErrorBase('invalid_preview_input')
            result=preview(raw,mode,expression)
            from .chat_display import read as read_display, plan as display_plan
            if presentation_version not in (None,'chat-sentences-v1','model-bubbles-v1'):raise RuntimeErrorBase('expression_policy_invalid')
            if batch_id:
                result['display_messages']=read_display(conn,batch_id)
            elif presentation_version in ('chat-sentences-v1','model-bubbles-v1') and mode=='natural_structured':
                from dataclasses import asdict
                from .reply_batch import validate_reply
                result['display_messages']=display_plan([asdict(s) for s in validate_reply(raw,mode,expression).segments],expression)
            if result.get('display_messages'):result['planned_wait_ms']=sum(m['wait_ms'] for m in result['display_messages'])
            if batch_id:
                result.update(batch_id=batch_id,raw_fingerprint=batch['raw_fingerprint'],fallback_reason=batch['fallback_reason'])
                result['historical_parts']=[{k:r[k] for k in ('id','text','semantic_index','state','platform_message_id','cancel_reason','trigger_event_id')} for r in conn.execute('SELECT * FROM reply_delivery_parts WHERE reply_batch_id=? ORDER BY part_index',(batch_id,))]
            return result
