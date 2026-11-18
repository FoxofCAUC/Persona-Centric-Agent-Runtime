"""Small conversation-retrieval overview, not a factual audit dashboard."""
import json
from decimal import Decimal
from .knowledge_service import read,POLICY
def dependencies():return {'ready':True,'missing':[],'required_for_selected_route':False}
def overview(service,persona_id):
    processing=service.processing;org=processing.engine
    with service.store.connection() as db:
        model_calls=0;tokens=0;unknown=0;tools=0;billable=0;amount=Decimal(0);jev_calls=0;jev_tokens=0
        cny=Decimal(0);unknown_tool_usage=0;native_actions=0;native_counts={'tool_search':0,'tool_fetch':0,'tool_image_search':0}
        for row in db.execute("SELECT a.usage_json,json_extract(t.payload_json,'$.knowledge_processing.stage'),a.id FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id WHERE t.persona_id=? AND json_type(t.payload_json,'$.knowledge_processing')='object'",(persona_id,)):
            value=json.loads(row[0] or '{}') or {}
            if value.get('tool'):
                tools+=1;billable+=int(value.get('billable') is True)
                if value.get('amount_usd') is not None:amount+=Decimal(value['amount_usd'])
                if value.get('estimated_amount_cny') is not None:cny+=Decimal(value['estimated_amount_cny'])
            else:
                model_calls+=1;known=value.get('total_tokens');tokens+=known if type(known) is int else 0;unknown+=int(type(known) is not int)
                if row[1]=='conversation_web_decision':jev_calls+=1;jev_tokens+=known if type(known) is int else 0
                if row[1]=='conversation_native':
                    from .deepseek_native_contract import tool_accounting
                    saved=read(db,'native.receipt.'+row[2],{})['data'].get('packet',{})
                    account=tool_accounting(value,terminal=saved.get('outcome') in ('completed','failed'))
                    for name,count in account['billable'].items():
                        if count is not None:native_counts[name]+=count
                    if account['estimated_tool_amount'] is None:unknown_tool_usage+=1
                    else:cny+=Decimal(account['estimated_tool_amount'])
                    for step in saved.get('assistant',{}).get('server_tool_calls',[]):native_actions+=len(step.get('calls',[]))
        records=db.execute("SELECT count(*) FROM knowledge_control WHERE key LIKE 'knowledge.conversation.record.%' AND json_extract(value,'$.data.persona_id')=?",(persona_id,)).fetchone()[0]
        latest=db.execute("SELECT value FROM knowledge_control WHERE key LIKE 'knowledge.conversation.record.%' AND json_extract(value,'$.data.persona_id')=? ORDER BY rowid DESC LIMIT 1",(persona_id,)).fetchone()
        latest_decision=json.loads(latest[0])['data'].get('retrieval',{}).get('web_decision',{}) if latest else {}
    return {'mode':'native_two_stage_v1','mode_name':'深度搜索与人物回应','policy':service.current_policy(persona_id),
        'processing_policy':processing.policy(persona_id),'web_policy':org.web.policy(persona_id),'dependencies':dependencies(),
        'retrieval_records':records,
        'cost':{'calls':model_calls,'tokens_known':tokens,'usage_unknown':unknown,'amount':None},
        'web_decision':{'configuration':org.routing.configuration(),'authorization':org.routing.authorization(persona_id),'latest':latest_decision},
        'jev_cost':{'calls':jev_calls,'tokens_known':jev_tokens,'included_in_total':True,'amount':None},
        'native_policy':processing.native.policy(persona_id),
        'native_authorization':processing.native.authorization(persona_id),
        'tool_cost':{'calls':tools+native_actions,'billable_calls':billable+sum(native_counts.values()),
            'native_billable_counts':native_counts,'unknown_native_tool_usage_calls':unknown_tool_usage,
            'known_amount_usd':str(amount),'actual_billed_amount_usd':None,
            'known_estimated_amount_cny':str(cny),'price_version':'deepseek-cny-2026-11-15','actual_billed_amount_cny':None},
        'principle':'多看不同来源，由人物综合判断。没有来源权威、逐数字或日期裁决门槛。'}
def rollback(*args,**kwargs):
    from .models import RuntimeErrorBase
    raise RuntimeErrorBase('legacy_retrieval_frozen')
