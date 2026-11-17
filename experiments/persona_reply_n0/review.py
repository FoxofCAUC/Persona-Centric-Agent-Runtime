"""静态口吻与提示审阅；导出意见绑定材料摘要，不具备派发功能。"""
import html
import json
from pathlib import Path

from .bundle import ROOT,digest,encoded,review_sections,validate,require_active_candidate

STYLE='''body{max-width:1180px;margin:30px auto;padding:0 24px 72px;background:#f2f4ee;color:#26392f;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}h1{font-size:29px;line-height:1.4}h2{font-size:24px}h3{font-size:18px}section{background:#fffefb;border:1px solid #d5dece;border-radius:12px;padding:24px;margin:24px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;padding:15px;background:#f5f7f0;border-radius:7px;margin:12px 0}.two{display:grid;grid-template-columns:1fr 1fr;gap:20px}.muted,small{color:#637463}.bar{position:sticky;top:0;z-index:1;display:flex;flex-wrap:wrap;align-items:center;gap:12px;background:#f2f4eef5;padding:12px 0;border-bottom:1px solid #ccd6c6}button,select,textarea{font:inherit;color:inherit;max-width:100%}button,select{background:white;border:1px solid #b4c5b3;border-radius:7px;padding:7px 12px;cursor:pointer}button[aria-pressed=true]{background:#dcead7;border-color:#5b8561}textarea{width:100%;box-sizing:border-box;min-height:70px;margin:10px 0;padding:10px;border:1px solid #bfceb8;border-radius:7px}.primary{background:#315e46;color:#fff}table{border-collapse:collapse;width:100%;font-size:15px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #d5dfce;padding:10px;overflow-wrap:anywhere}td:first-child{min-width:100px}details{padding:10px 0;border-top:1px solid #e1e6db}summary{cursor:pointer}.user{padding:11px 14px;background:#eaf0e3;border-radius:7px}.sample{border-left:3px solid #92af7c;padding-left:15px;margin:14px 0}#progress{margin-left:auto}a{color:#2e654b}footer{margin-top:30px}@media(max-width:760px){body{padding:0 12px 50px}.two{grid-template-columns:1fr}section{padding:16px}h1{font-size:25px}.bar{position:relative}table{display:block;max-width:100%;overflow-x:auto}th,td{min-width:120px}td:last-child{min-width:190px}}'''

STYLE+='body{overflow-wrap:anywhere}.two>div{min-width:0}section,#prompts{scroll-margin-top:90px}'

SCRIPT='''const data=JSON.parse(document.getElementById('data').textContent),key='pcar-persona-n0:'+data.bundle_sha256;let state={sections:{},overall:'',note:''};try{const old=JSON.parse(localStorage.getItem(key));if(old?.sections)state={...state,...old}}catch(e){}function save(){try{localStorage.setItem(key,JSON.stringify(state));document.getElementById('save-status').textContent='意见已保存在当前浏览器，可导出后交回。'}catch(e){document.getElementById('save-status').textContent='请导出意见文件保留选择。'}}function update(){for(const panel of document.querySelectorAll('[data-review-id]')){const item=state.sections[panel.dataset.reviewId]||{};for(const b of panel.querySelectorAll('[data-choice]'))b.setAttribute('aria-pressed',String(item.choice===b.dataset.choice))}document.getElementById('progress').textContent=data.sections.filter(s=>state.sections[s.id]?.choice).length+' / '+data.sections.length+'项已选'}for(const panel of document.querySelectorAll('[data-review-id]')){const id=panel.dataset.reviewId;for(const b of panel.querySelectorAll('[data-choice]'))b.onclick=()=>{state.sections[id]={...(state.sections[id]||{}),choice:b.dataset.choice};save();update()};const n=panel.querySelector('textarea');n.value=state.sections[id]?.note||'';n.oninput=()=>{state.sections[id]={...(state.sections[id]||{}),note:n.value};save()}}const overall=document.getElementById('overall');overall.value=state.overall||'';overall.onchange=()=>{state.overall=overall.value;save()};const note=document.getElementById('overall-note');note.value=state.note||'';note.oninput=()=>{state.note=note.value;save()};document.getElementById('export').onclick=()=>{const out={version:'persona-reply-n0-author-review-v1',bundle_sha256:data.bundle_sha256,sections:data.sections.map(s=>({...s,choice:state.sections[s.id]?.choice||null,note:state.sections[s.id]?.note||''})),overall:state.overall||null,note:state.note||'',exported_at:new Date().toISOString()},url=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:'application/json;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='E.N0-人物口吻与提示审阅意见.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)};update();'''


def render(bundle,out):
    require_active_candidate()
    counts=validate(bundle)
    out=Path(out)
    if out.exists():raise ValueError('n0_review_output_already_exists')
    out.mkdir(parents=True)
    ph=digest(bundle);sections=review_sections(bundle);esc=html.escape
    (out/'bundle.json').write_text(json.dumps(bundle,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (out/'bundle-hash.json').write_text(json.dumps({'sha256':ph},indent=2)+'\n',encoding='utf-8')
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>E.N0 人物口吻与提示审阅</title><style>'+STYLE+'</style>',
      '<header><small>PCAR · E.N0 · 2026-11-17</small><h1>人物口吻与提示审阅</h1><p>先看四个人像不像自己，再看最后三份提示和完整输入。你确认这些材料以后，才进入真实三链对照。</p>',
      '<p>本页的48条情境表达与16组话语片段，由开发代理静态起草，均为公开合成材料。片段展示拟议口吻，没有调用模型，不能据此判断新链已经优于v8。</p>',
      '<p class="muted">原有姓名、介绍、核心性格、人物边界及初始关系背景保持；本次提交新的说话习惯。当前运行代码、正式配置和实例未切换。</p></header>',
      '<nav class="bar"><a href="#qinghe">青禾</a><a href="#xiazhi">夏栀</a><a href="#ningzhou">宁舟</a><a href="#linchuan">林川</a><a href="#prompts">提示词</a><button id="export" class="primary">导出意见</button><span id="progress"></span></nav><p id="save-status" class="muted"></p>']
    markdown=['# E.N0：人物口吻、情境表达和提示候选','',
      '2026-11-17。开发代理静态起草，0次真实模型调用。作者确认后再冻结素材并进入E.N1。话语片段仅供口吻审阅，未作为真实回复或实验收益。','']
    scenes={s['id']:s for s in bundle['review_scenes']}
    def vote(ident):
        return '<div class="vote"><button data-choice="approve" aria-pressed="false">这版可以进入真实对照</button> <button data-choice="revise" aria-pressed="false">需要修改</button></div><textarea aria-label="'+esc(ident)+'意见" placeholder="哪句或哪种习惯需要调整？也可以指出想保留的地方。"></textarea>'
    for role in bundle['roles']:
        profile=role['proposed_profile'];original=role['original_profile'];rid=role['id']
        parts.append('<section id="'+rid+'" data-review-id="role:'+rid+'"><h2>'+esc(profile['name'])+'</h2><p>'+esc(profile['introduction'])+' 性格：'+esc('、'.join(profile['core_traits']))+'。</p><p class="muted">'+esc(role['review_focus'])+'</p><div class="two"><div><h3>此前说话设定</h3><pre>'+esc(original['expression_style'])+'</pre></div><div><h3>本次口语设定</h3><pre>'+esc(role['voice'])+'</pre></div></div>')
        markdown.extend(['## '+profile['name'],'','原介绍：'+profile['introduction'],'','核心性格：'+'、'.join(profile['core_traits']),'','### 口语设定','',role['voice'],'','### 静态话语片段',''])
        parts.append('<h3>四组静态话语片段</h3><p class="muted">这些片段只供你审阅口吻，不自动放进后续人物提示。四个人使用相同场景，便于比较。</p>')
        for sample in role['review_samples']:
            s=scenes[sample['scene']]
            h=''.join('<p class="muted">'+esc(('对方' if item['speaker']=='user' else profile['name'])+'之前说：'+item['text'])+'</p>' for item in s['history'])
            parts.append('<div class="sample"><strong>'+esc(s['category'])+'</strong>'+h+'<p class="user">对方：'+esc(s['user'])+'</p><pre>'+esc(sample['text'])+'</pre></div>')
            markdown.extend(['#### '+s['category'],'']+[('对方' if i['speaker']=='user' else profile['name'])+'之前：'+i['text'] for i in s['history']]+['','对方：'+s['user'],'',profile['name']+'（静态创作）：'+sample['text'],''])
        parts.append('<details><summary>展开12条情境表达</summary><p>作为可选语言素材，后续按场景选择0—5条。当前只审阅，尚未登记为运行资产。</p><table><thead><tr><th>情境</th><th>对应说法或习惯</th></tr></thead><tbody>')
        markdown.extend(['### 12条情境表达','','| ID | 情境 | 说法或习惯 |','| --- | --- | --- |'])
        for a in role['assets']:
            parts.append('<tr><td>'+esc(a['situation'])+'</td><td>'+esc(a['style'])+'</td></tr>')
            markdown.append('| '+a['id']+' | '+a['situation']+' | '+a['style']+' |')
        markdown.extend(['','### 完整候选人物资料','','```json',json.dumps(profile,ensure_ascii=False,indent=2),'```',''])
        parts.append('</tbody></table></details><details><summary>完整人物资料与版本依据</summary><pre>'+esc(json.dumps(profile,ensure_ascii=False,indent=2))+'</pre><p class="muted">当前仍为待审阅候选，来源为此前四种公开合成人物；不是正式人物资料的修改。</p></details>'+vote(profile['name'])+'</section>')
    parts.append('<div id="prompts"><h2>三份提示候选</h2><p>花括号会填入对应人物的真实资料。内部理解笔记只帮助人物把握现场，最终Replyer直接输出发言正文。</p></div>')
    labels={'understanding':'Flash：理解局面','replyer':'Pro：人物直接发言','search_notice':'Pro：查前发言'}
    for name,fixed in bundle['prompts'].items():
        parts.append('<section data-review-id="prompt:'+name+'"><h3>'+labels[name]+'</h3><pre>'+esc(fixed)+'</pre>'+vote(labels[name])+'</section>')
        markdown.extend(['## '+labels[name],'','```text',fixed,'```',''])
    parts.append('<section><h2>完整输入预览</h2><p>每个场景展开后能看到实际会装配成什么。这里的交流、资料和理解笔记都是静态说明材料；还没有派发请求，也没有执行候选检索。</p><h3>补充场景：查前、资料取得与未取得、关系记忆</h3>')
    markdown.extend(['## 完整输入预览','','以下均为静态装配，理解笔记由开发代理填写，非模型返回。',''])
    def preview(item):
        role=next(r for r in bundle['roles'] if r['id']==item['role_id'])
        caption=role['label']+' · '+item['id']
        parts.append('<details><summary>'+esc(caption)+'</summary><p class="user">'+esc(item['user'])+'</p>')
        markdown.extend(['### '+caption,''])
        for key,label in (('understanding','Flash理解输入'),('replyer','Pro人物输入')):
            request=item.get(key)
            if request is None:continue
            parts.append('<h4>'+label+'</h4>')
            markdown.extend(['#### '+label,''])
            for msg in request['messages']:
                parts.append('<p><small>'+msg['role']+'</small></p><pre>'+esc(msg['content'])+'</pre>')
                markdown.extend([msg['role'],'','```text',msg['content'],'```',''])
        if item['understanding']:
            parts.append('<p class="muted">上面用于Pro的理解笔记为静态填写：</p><pre>'+esc(item['understanding']['illustrated_note'] or '')+'</pre>')
        parts.append('</details>')
    for item in bundle['supplemental_previews']:preview(item)
    parts.append('<h3>首批16个研究位置的静态装配</h3><p>每个角色四类场景，与上方口吻示范分开。正式对照三链共用相同确认后的人物资料、脚本及初始交流。</p>')
    for item in bundle['pilot_previews']:preview(item)
    parts.append('</section><section><h2>整体判断</h2><p>需要确认四种口吻、情境表达及三份提示。只有全部条目确认并作整体通过选择，导出意见才显示材料门通过。</p><select id="overall" aria-label="整体审阅结论"><option value="">尚未作整体判断</option><option value="approve_for_n1">材料与分工通过，可进入E.N1真实三链研究</option><option value="revise">需要修改后再看</option></select><textarea id="overall-note" aria-label="整体修改意见" placeholder="整体意见或必须先改的地方"></textarea><p class="muted">下一阶段最多64次首次请求：v8 Flash 16、v8 Pro 16、Flash理解16、Pro Replyer 16。当前执行次数为0；导出文件本身不派发请求。</p></section>')
    parts.append('<footer><p>完整机器可读材料：<a href="bundle.json">bundle.json</a> · <a href="E.N0-人物口吻与提示全文.md">全文Markdown</a>。预览不是实际桌面聊天。</p><small>材料摘要 '+ph+'</small></footer>')
    data=json.dumps({'bundle_sha256':ph,'sections':sections},ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    parts.append('<script id="data" type="application/json">'+data+'</script><script>'+SCRIPT+'</script></html>')
    name='E.N0-人物口吻与提示审阅.html';(out/name).write_text(''.join(parts),encoding='utf-8')
    (out/'E.N0-人物口吻与提示全文.md').write_text('\n'.join(markdown),encoding='utf-8')
    target=out/'inputs';target.mkdir()
    for item in bundle['pilot_previews']+bundle['supplemental_previews']:
        (target/(item['id']+'.json')).write_text(json.dumps(item,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    report=counts|{'status':'pending_author_review','bundle_sha256':ph,'html_path':str(out/name),'next_trial_dispatch_authorized':False,
        'formal_config_modified':False,'production_assets_registered':False,'approval_sections':len(sections)}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return report
