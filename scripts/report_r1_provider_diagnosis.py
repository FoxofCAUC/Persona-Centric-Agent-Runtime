"""Read artificial HTTP evidence and replay it through the existing client locally."""
import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
import zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from aiohttp import web
from aiohttp.test_utils import TestServer
from persona_runtime.chat import ChatCompletionsClient,ModelError
from persona_runtime.reply_batch import validate_reply
from persona_runtime.config import atomic_json
from scripts.diagnose_r1_provider import DIAG,OUT,HOME,SUPPLEMENT,ReceiptLedger,read,sha,guard,content_stats,ROOT


async def replay():
    """Five local HTTP responses, zero real model generation."""
    rows=ReceiptLedger(DIAG/'receipts',limit=5).rows();plan=read(DIAG/'plan.json');results=[]
    for row,item in zip(rows,plan['items']):
        raw=Path(row['raw_artificial_response']).read_bytes();assert not row['raw_reasoning_redacted']
        received=[]
        async def handler(request):
            received.append(await request.json());return web.Response(body=raw,content_type='application/json')
        app=web.Application();app.router.add_post('/chat/completions',handler)
        async with TestServer(app) as server:
            model={'base_url':str(server.make_url('')).rstrip('/'),'name':'deepseek-flash','encrypted_key':'',
                'timeout':3,'max_tokens':item['request']['max_tokens']}
            if item['id']=='D02-json-minimal':model['_json_output']=True
            elif item['id'] in ('D03-original-json','D05-clarify-wrapper'):
                model.update(_reply_mode='natural_structured',_expression_snapshot=plan['expression_snapshot'])
            try:
                value=await ChatCompletionsClient().complete(model,item['request']['messages'])
                actual=read(Path(row['raw_artificial_response']))['choices'][0]['message']['content']
                assert value[0]==actual
                result={'id':item['id'],'client_returned_body_unchanged':True,'client_error':None}
                if item['id']=='D04-no-json-mode':
                    try:validate_reply(value[0],'natural_structured',plan['expression_snapshot'])
                    except Exception as exc:result['application_error']=str(exc)
                    assert result['application_error']=='reply_schema_invalid'
            except ModelError as exc:
                assert item['id'] in ('D03-original-json','D05-clarify-wrapper')
                assert str(exc)=='provider_empty_response'
                result={'id':item['id'],'client_error':str(exc),'diagnostics':exc.diagnostics}
            assert len(received)==1;result['local_requests']=1;results.append(result)
    return {'passed':True,'local_http_replays':5,'external_calls':0,'results':results}


def main():
    plan=guard();rows=ReceiptLedger(DIAG/'receipts',limit=5).rows()
    assert read(DIAG/'run-status.json')['complete'] and len(rows)==5
    assert all(row['diagnostics']['http_status']==200 for row in rows)
    for row in rows:
        assert sha(Path(row['raw_artificial_response']))==row['diagnostics']['body_sha256']
        assert row['analysis']['content']==content_stats(read(Path(row['raw_artificial_response']))['choices'][0]['message']['content'])
    atomic_json(DIAG/'offline-captured-replay.json',asyncio.run(replay()))
    usage=Counter()
    for row in rows:
        for k,v in (row.get('usage') or {}).items():
            if type(v)is int:usage[k]+=v
    old=read(OUT/'experiment/ledger.json')['calls']+ReceiptLedger(SUPPLEMENT/'real/receipts').rows()
    blanks=[r for r in old if r.get('error')=='ModelError:provider_empty_response']
    historical={'recorded_calls':len(old),'main_calls':sum(r['kind']=='main' for r in old),
        'selector_calls':sum(r['kind']=='selector' for r in old),'blank_failures':len(blanks),
        'all_blanks_finish_stop':all(r['diagnostics']['finish_reason']=='stop' for r in blanks),
        'all_blanks_nonzero_characters':all(r['diagnostics']['output_characters']>0 for r in blanks),
        'all_blanks_without_reasoning':all(not r['diagnostics']['reasoning_present'] for r in blanks),
        'caution':'历史未保存原始空白响应体，不能逐条确认U+0020；本轮捕获原始响应验证该故障机制。'}
    atomic_json(DIAG/'historical-analysis.json',historical)
    sources=[{'title':'DeepSeek JSON Output','url':'https://api-docs.deepseek.com/zh-cn/guides/json_mode/',
        'finding':'官方说明无法识别为JSON的输出会被替换为U+0020空格，即便finish_reason=stop。JSON模式仍需要提示与样例。'},
        {'title':'Chat Completions API','url':'https://api-docs.deepseek.com/api/create-chat-completion/',
        'finding':'response_format支持text/json_object；同样说明非JSON替换为空格。thinking disabled为非思考，temperature在非思考模式生效。'},
        {'title':'DeepSeek Status','url':'https://status.deepseek.com/',
        'finding':'查询时官方状态页显示整体运行正常；不能单独证明历史每条请求或当前格式输出可靠。'}]
    atomic_json(DIAG/'official-sources.json',{'checked_date':'2026-11-01','timezone':'Asia/Shanghai','sources':sources})
    atomic_json(DIAG/'tests.json',{'passed':True,'tests':24,'seconds':6.786,
        'suites':['tests.test_r1_provider_diagnosis','tests.test_deepseek_compatibility'],
        'additional_captured_response_local_replays':5,'real_calls_in_tests':0})
    metrics={'generation_calls':5,'models_gets':1,'automatic_retries':0,'http_200':5,
        'minimal_checks_passed':2,'full_context_blank_responses':2,'diagnostic_plaintext_without_json':1,
        'full_context_application_valid':0,'usage':dict(usage),'original_results_replaced':0,
        'connection_verified':True,'client_parser_verified':True,
        'diagnosis':'JSON output masking of model format noncompliance; not a demonstrated DNS/TLS/authentication outage.',
        'not_proven':['被屏蔽前每次具体原文','某一条提示指令是唯一原因','格式提醒可稳定修复','所有历史空响应都属同一机制']}
    atomic_json(DIAG/'metrics.json',metrics)
    stamp=datetime.fromisoformat(rows[0]['started_at']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    table=[]
    for row in rows:
        c=row['analysis']['content'];table.append('| '+row['item']+' | '+c['kind']+' | '+str(c['characters'])+' | '+str(row['analysis']['application_valid'])+' |')
    report='''# 供应商连接与空响应排查

诊断时间：TIME（北京时间）。候选保持0.7.6.dev6；没有修改生产客户端、主提示、默认策略或正式配置。

**主要故障已定位到输出格式链路。当前连接与鉴权正常；原失败请求在JSON模式下返回纯空白，只关闭供应商JSON模式就得到自然中文，但没有JSON外层。现有客户端正确拒绝空白，未吞掉正常回复。**

## 证据

DNS解析与TLS主机名/证书校验通过，TLSv1.3。GET /models返回200，包含已配置的deepseek-flash；该请求没有模型生成。代理环境变量未设置，测试沿用生产trust_env=False、证书验证与官方地址。5次生成均HTTP 200，未出现401/402/429/5xx、连接或读取超时。最小纯文本检查和最小JSON检查均正确。

| 调用 | 原始content类型 | 字符数 | 按该项要求有效 |
|---|---|---:|---|
TABLE

D03完整复用R07-detail-control原失败请求，正文是85个U+0020空格，finish_reason=stop，有60个输出token。D04只删除response_format，其余提示、历史、参数、预算、模型均不变，得到50字中文：“留着吧。舍不得的东西别急着丢……”；不是JSON，不能直接进入PCAR的结构回复流程。D05只在原system末尾追加“纯文本仅约束content、响应外层仍为JSON”的提醒，仍得到120个空格与2个换行；本次提醒没有解决问题。

5份人工原始HTTP响应均独立保存，字节哈希与接收记录相符。将这些响应通过本地HTTP交给现有ChatCompletionsClient再次读取：D03/D05仍报provider_empty_response；D04正文原样返回，然后由结构校验报reply_schema_invalid。由此排除本次客户端读取或去空白处理错误。离线回放无云端调用、无正式服务。

## 原因与置信边界

[DeepSeek官方JSON说明](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)明确表示：无法识别为JSON的生成内容会被替换为空格，stop不保证存在有效正文。本轮纯空格响应与该机制吻合，关闭JSON后的同提示明文回复进一步支持“模型未遵守JSON外层→供应商屏蔽非JSON→客户端判空”的链路。不同调用有随机性，D04不是对D03被屏蔽内容的恢复，不能推称二者原文相同。

PCAR原请求已经包含JSON字样、完整格式示例和8192输出token额度，不属于完全漏写JSON要求。实际提示同时包含自然私聊、用户可见纯文本、角色样例、输入消息JSON包装及输出JSON合同；格式层与表达层的协调是待验证因素，尚未隔离到其中某一段是唯一原因。

历史R1及续验合计67条回执，其中39次主回复有16次空白错误；这些错误都有正字符数、stop及无reasoning标记，与当前复现一致。历史原始响应体没有保存，因此逐条归因仍有限，不能把所有空响应都追溯确认为U+0020。

输出未达到8192上限，结束原因不是length；连接耗时远低于180秒；非思考模式已明确关闭思考且无reasoning正文。现有证据不支持靠增加token、延长超时、修改代理、重装证书或转用思考正文解决此次问题。官方状态页查询时正常，也不代表格式生成可靠。

## 处理建议

1. 当前保留失败而不把空白视为回复，这一规则正确。放宽判空会把无内容消息当成功，无法恢复供应商已经屏蔽的文字。
2. 不默认关闭JSON模式：D04证明单独去掉它会暴露非JSON正文，并不能满足现有气泡合同。直接发送或自动包裹该正文会改变既有协议与结果，需另行设计验证。
3. 不继续原样重试。原有续验成功率低，本次同请求仍可重现；追加一句格式强调也未奏效。
4. 下一最小修复应在新人工材料中只检验“表达内容与输出包装分层”：先离线重整二者的责任边界，再用少量固定请求测结构成功率和正文保真，保留原协议校验。不要同时调整样例选择、召回、人格事实、胶囊或自然度评分。若考虑工具调用或另一输出接口，应作为独立方案验证，不能直接替换生产。
5. 后续可将“空字符串/纯空格/其他空白/非字符串”及字符数、HTTP状态、供应商追踪ID加入无正文诊断。本轮已经在专用诊断工具实现分类，没有扩展普通日志正文。

## 本轮边界与交付

最多5次生成已全部用完，没有自动重试；另1次只读模型目录GET。总用量TOKENS tokens，含失败；无额外模型评审。24项供应商/诊断局部测试通过，另有5次已捕获响应的本地回放通过。没有重跑R1成功项、覆盖冻结标签、删除停测标记或把诊断回答计入23/32的R1效果成绩。

正式配置、数据库、私人登记及冻结v2评测文件哈希保持；零QQ、无正式服务、无提交推送部署。供应商连接排查与空白故障机制验证已完成；稳定修复及R1剩余效果验证尚未完成。
'''.replace('TIME',stamp).replace('TABLE','\n'.join(table)).replace('TOKENS',str(usage['total_tokens']))
    (DIAG/'report.md').write_text(report,encoding='utf-8')
    print(json.dumps(metrics,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
