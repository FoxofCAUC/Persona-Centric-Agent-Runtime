/* Native workbench bindings. The standalone design preview has its own data. */
window.pcarLive = (() => {
  "use strict";
  const api = () => window.pywebview.api;
  const run = fn => (...args) => Promise.resolve().then(() => fn(...args)).catch(e => notify(String(e.message || e)));
  const action = (name, args={}) => api().import_action(name,args);
  const titles = {introduction:"人物介绍", core_traits:"核心特征", expression_style:"表达习惯",
    values_and_boundaries:"价值与边界", initial_background:"初始背景"};
  const jobs = {preview:"待确认归属", prepared:"已准备脱敏片段", ready:"已提炼", partial_ready:"已按部分分段预填", running:"正在提炼",
    unknown:"发送结果待核对", failed:"提炼失败", cancelled:"已取消", interrupted:"已中断", revoked:"已撤回"};
  let active = false, epoch = 0, polling = false, pendingReceipt = null;
  function newId() {
    const bytes=crypto.getRandomValues(new Uint8Array(16));
    bytes[6]=(bytes[6]&15)|64;
    bytes[8]=(bytes[8]&63)|128;
    const hex=Array.from(bytes,b=>b.toString(16).padStart(2,"0")).join("");
    return [hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join("-");
  }
  const presenter = newId();

  function el(tag, value, parent) {
    const item=document.createElement(tag);
    if(value!==null) item.textContent=value;
    if(parent) parent.append(item);
    return item;
  }
  function button(parent,label,fn) {
    const item=el("button",label,parent);
    item.type="button"; item.className="button";
    item.onclick=run(async () => {item.disabled=true; try {await fn();} finally {item.disabled=false;}});
    return item;
  }
  function panel(title) {
    present(title,"");
    const box=el("div",null,dialogBody);
    box.className="fields";
    return box;
  }
  function check(parent,label,checked=false) {
    const wrap=el("label",null,parent), input=el("input",null,wrap);
    input.type="checkbox"; input.checked=checked;
    wrap.append(document.createTextNode(" "+label)); return input;
  }
  function field(parent,label,value="",list=false) {
    const wrap=el("label",label,parent); wrap.className="field";
    const input=el("textarea",null,wrap); input.rows=2;
    input.value=list?value.join("\n"):value; return input;
  }
  function specForm(parent,spec) {
    const name=field(parent,"名称",spec.name);
    const fields=Object.entries(titles).map(([key,title])=>{
      const list=["core_traits","values_and_boundaries"].includes(key);
      return {key,list,input:field(parent,title+(list?"（每行一项）":""),spec[key],list)};
    });
    return ()=>Object.fromEntries([["format_version",1],["name",name.value.trim()],
      ...fields.map(({key,list,input})=>[key,list?input.value.split("\n").map(s=>s.trim()).filter(Boolean):input.value.trim()])]);
  }
  async function home() {
    const box=panel("新建角色");
    button(box,"从聊天记录生成",async()=>{const data=await api().pick_transcript(); if(data) await openImport(data);});
    button(box,"导入已有角色包",async()=>{
      const pack=await api().live_package();if(!pack)return;
      const form=panel("核对并导入角色包"),read=specForm(form,pack.persona_spec),request=newId();
      button(form,"保存为新角色",async()=>{pack.persona_spec=read();const p=await api().live_save_package(pack,request);
        dialog.close();await reload(p.id);});
    });
    button(box,"空白创建",()=>{
      const form=panel("空白创建"), read=specForm(form,{name:"",introduction:"",core_traits:[],
        expression_style:"",values_and_boundaries:[],initial_background:""});
      button(form,"创建并选用",async()=>{const result=await api().live_create(read()); dialog.close();await reload(result.id);});
    });
    const docs=await action("list");
    if(!box.isConnected)return;
    for(const d of docs)button(box,d.format+" · "+d.created.slice(0,16)+" · "+(jobs[d.status]||d.status),
      async()=>openImport(await action("status",{ident:d.id})));
  }

  async function evidence(data,start,prepared,back,overrides=null) {
    const p=await action("evidence",{ident:data.id,start,prepared});
    const box=panel(prepared?"将用于分析的脱敏片段":"文件原话与消息归属");
    button(box,"返回",back);
    for(const r of p.rows){
      const label=r.speaker+" · #"+r.id+(r.date?" · "+r.date:"");
      el("strong",label,box);
      const body=el("p",prepared?r.text:r.original,box);body.style.whiteSpace="pre-wrap";
      if(overrides && Object.hasOwn(data.mapping,r.speaker)){
        const sel=el("select",null,box);
        for(const v of ["","A","B","-"])sel.add(new Option(v===""?"沿用说话人归属":v==="-"?"排除":v,v));
        sel.value=overrides[r.id]||"";
        sel.onchange=()=>{if(sel.value) overrides[r.id]=sel.value; else delete overrides[r.id];};
      }
    }
    if(start>0)button(box,"上一页",()=>evidence(data,Math.max(0,start-50),prepared,back,overrides));
    if(start+50<p.total)button(box,"下一页",()=>evidence(data,start+50,prepared,back,overrides));
  }
  function mapping(data) {
    const box=panel("确认两位说话人");
    const map=data.mapping,overrides=data.overrides;
    const range=data.preview||{};
    el("p",[data.format,range.count && range.count+" 条记录",range.from,range.to].filter(Boolean).join(" · "),box);
    for(const [speaker,count] of Object.entries(data.counts)){
      const label=el("label",speaker+"（"+count+" 条）",box), sel=el("select",null,label);
      for(const value of ["A","B","-"])sel.add(new Option(value==="-"?"排除":value,value));
      sel.value=map[speaker]; sel.onchange=()=>{map[speaker]=sel.value;};
    }
    button(box,"逐条核对或修正",()=>evidence(data,0,false,()=>mapping(data),overrides));
    button(box,"准备脱敏片段和两份草稿",async()=>openImport(await action("prepare",{ident:data.id,revision:data.revision,mapping:map,overrides})));
  }

  async function openImport(data,role="A") {
    if(data.status==="preview"){mapping(data);return;}
    const box=panel("聊天人设草稿");
    el("p",data.format+" · "+(jobs[data.status]||data.status),box);
    if(data.status==="revoked"){button(box,"返回",home);return;}
    if(data.status==="running"){
      button(box,"取消提炼",async()=>openImport(await action("cancel",{ident:data.id})));
      setTimeout(run(async()=>{if(box.isConnected)await openImport(await action("status",{ident:data.id}),role);}),900);
      return;
    }
    const again=async()=>openImport(await action("status",{ident:data.id}),role);
    button(box,"查看脱敏范围",()=>evidence(data,0,true,again));
    if(data.status!=="ready"){
      button(box,"用已收到的回执预填",async()=>openImport(await action("replay",{ident:data.id,revision:data.revision})));
      const consent=check(box,"允许官方 DeepSeek 分析此文件中预览的双方脱敏发言，调用可能产生费用");
      button(box,"分析两位人物",async()=>{if(!consent.checked)throw Error("请选择本次分析用途。");
        await openImport(await action("analyze",{ident:data.id,revision:data.revision,confirmed:true}));});
    }
    const draft=data.draft[role];
    if(!draft)return;
    const line=el("div",null,box);line.className="inline-actions";
    const other=role==="A"?"B":"A";
    el("h3",role+" · "+draft.spec.name,box);
    if(draft.coverage)el("p","已采用 "+draft.coverage.received+" / "+draft.coverage.eligible+" 个分段。",box);
    const read=specForm(box,draft.spec), relation=field(box,"关系说明",draft.relationship);
    const saveDraft=()=>{
      const spec=read(),prior=data.draft[role];
      const same=Object.keys(spec).every(k=>JSON.stringify(spec[k])===JSON.stringify(prior.spec[k]));
      return same && relation.value===prior.relationship?Promise.resolve(data):
        action("edit",{ident:data.id,revision:data.revision,role,spec,relationship:relation.value});
    };
    button(line,"暂存并查看 "+other,async()=>openImport(await saveDraft(),other));
    button(line,"暂存草稿",async()=>openImport(await saveDraft(),role));
    const proof=el("details",null,box);el("summary","提炼依据与表达样例",proof);
    const proofPage=el("div",null,proof);
    function showProof(start=0){
      proofPage.replaceChildren();
      const total=draft.claims.length+draft.samples.length;
      for(let i=start;i<Math.min(start+25,total);i++){
        if(i<draft.claims.length){
          const claim=draft.claims[i];
          el("p",(titles[claim.field]||claim.field)+(claim.needs_review?"（待核）":"")+" · "+claim.value,proofPage);
          for(const ref of claim.evidence)button(proofPage,"#"+ref.id+" "+ref.quote,()=>evidence(data,Number(ref.id)-1,false,again));
        }else{
          const sample=draft.samples[i-draft.claims.length];
          button(proofPage,"#"+sample.id+" "+sample.text,()=>evidence(data,Number(sample.id)-1,false,again));
        }
      }
      if(start>0)button(proofPage,"上一页",()=>showProof(start-25));
      if(start+25<total)button(proofPage,"下一页",()=>showProof(start+25));
    }
    showProof();
    button(box,"用现有通用模板补齐空项",async()=>{
      const spec=read(),defaults=await api().template_spec();
      for(const key of Object.keys(titles))if(!spec[key]?.length)spec[key]=defaults[key];
      await openImport(await action("edit",{ident:data.id,revision:data.revision,role,spec,relationship:relation.value}),role);
    });
    const both=check(box,"同时创建 A 和 B"),style=check(box,"允许把本角色的脱敏原话用于日常发言");
    const modes=el("fieldset",null,box);el("legend","创建后的会话",modes);
    const fresh=check(modes,"从空白会话开始",true);
    const keep=check(modes,"还原这段历史并接着聊（另一方对应我，历史可进入后续聊天和已启用的长期记忆整理）");
    for(const option of [fresh,keep]){option.type="radio";option.name="historyMode";}
    fresh.checked=true;
    button(box,"确认创建",async()=>{
      data=await saveDraft();
      const roles=both.checked?["A","B"]:[role];
      const operation=[data.id,data.revision,roles.join(""),Number(style.checked),Number(keep.checked)].join(".");
      const ids=await action("create",{ident:data.id,revision:data.revision,roles,
        enable_style:style.checked,continue_history:keep.checked,operation_id:operation});
      dialog.close();await reload(ids[both.checked && role==="B"?1:0]);
    });
    button(box,"重新核对归属",()=>mapping(data));
    button(box,"撤回该文件的提炼与风格使用",async()=>{if(confirm("停止该来源的提炼与表达取样？已确认还原的历史可在会话中查看。"))
      await openImport(await action("revoke",{ident:data.id}));});
  }

  async function reload(prefer=null) {
    const result=await api().live_people();
    state.connected=result.connected;state.model=result.model;
    state.people=result.people.filter(p=>!p.deleted).map(p=>({id:p.id,profile:{name:p.name},sessions:[]}));
    const chosen=state.people.find(p=>p.id===prefer) || state.people.find(p=>result.people.find(q=>q.id===p.id)?.selected) || state.people[0];
    if(chosen)await select(chosen);
    else {state.current=null;state.guide=true;render();}
  }
  async function select(person,fresh=false){
    const id=++epoch;
    const result=await api().live_select(person.id,fresh);
    if(id!==epoch)return;
    person.sessions=result.sessions.map(s=>({id:s.id,label:s.created_at.slice(0,16).replace("T"," "),
      draft:"",messages:[],past:[],version:null,historyReady:false,turns:[]}));
    person.profile=result.person.spec; person.activeSession=result.active;state.current=person.id;
    state.guide=false;closeDrawer();if(dialog.open)dialog.close();render();
    await refresh();
  }
  async function refresh(){
    if(polling||!active)return;
    const p=currentPerson(),s=currentSession(),id=epoch;
    if(!p||!s)return;
    polling=true;
    try{
      if(!s.historyReady){
        const h=await action("history",{persona_id:p.id,session_id:s.id});
        if(id!==epoch||s!==currentSession())return;
        s.past=h.rows;s.before=h.before;s.historyReady=true;
      }
      const result=await api().live_snapshot(s.id,s.version);
      if(id!==epoch||s!==currentSession())return;
      if(result.turns!==null){
        const atEnd=timeline.scrollHeight-timeline.scrollTop-timeline.clientHeight<110 || s.version===null;
        s.turns=result.turns;s.version=result.version;render({scrollToEnd:atEnd});
      }
      if(document.hidden||dialog.open)return;
      if(pendingReceipt){
        await api().live_present("expression.v2.ack",pendingReceipt);
        pendingReceipt=null;s.version=null;return;
      }
      const pending=result.pending_notices?.[0];
      if(pending){
        const args={...pending,persona_id:p.id,session_id:s.id};
        const note=await api().live_present("chat.notice.preview",args);
        if(!note.published && await paint(s,id,note.text))await api().live_present("chat.notice.presented",args);
        s.version=null;return;
      }
      const speech=s.turns.map(t=>t.scene_speech).find(x=>x && ["pending","partial"].includes(x.state));
      const item=speech?.items.find(i=>i.state!=="presented");
      if(!item || item.state!=="pending")return;
      const last=speech.visible.at(-1);
      if(last && Date.now()-Date.parse(last.presented_at)<item.delay_ms)return;
      const owner={persona_id:p.id,session_id:s.id,utterance_id:speech.utterance_id,presenter_id:presenter};
      const offer=await api().live_present("expression.v2.offer",{...owner,publication_id:speech.publication_id,
        revision:speech.revision,ordinal:item.ordinal});
      if(offer.state==="offered" && offer.lease_id){
        const visible=await paint(s,id,offer.body);
        pendingReceipt={...owner,fragment_id:offer.id,lease_id:offer.lease_id,
          receipt_id:newId(),body_hash:offer.body_hash,state:visible?"presented":"not_presented"};
        await api().live_present("expression.v2.ack",pendingReceipt);pendingReceipt=null;
      }
      s.version=null;
    }finally{polling=false;}
  }
  async function paint(s,id,text){
    const visible=()=>s===currentSession() && id===epoch && !document.hidden && !dialog.open;
    if(!visible())return false;
    const bubble=el("p",text,timeline.querySelector(".conversation-inner")||timeline);
    bubble.className="bubble reply";bubble.scrollIntoView({block:"nearest"});
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const rect=bubble.getBoundingClientRect(),area=timeline.getBoundingClientRect();
    const shown=visible() && bubble.isConnected && rect.height>0 && rect.top<area.bottom && rect.bottom>area.top;
    if(!shown)bubble.remove();return shown;
  }
  function draw(s){
    timeline.classList.remove("first");timeline.replaceChildren();
    const thread=el("div",null,timeline);thread.className="conversation-inner";
    if(s.before)button(thread,"查看更早的导入消息",async()=>{
      const result=await action("history",{persona_id:currentPerson().id,session_id:s.id,before:s.before});
      if(s!==currentSession())return;
      s.past.unshift(...result.rows);s.before=result.before;render();
    });
    function bubble(role,text){
      const p=el("p",text,thread);p.className="bubble "+(role==="user"?"own":"reply");return p;
    }
    let day=null;
    for(const event of s.past){
      const nextDay=event.created_at?.slice(0,10)||"日期待核";
      if(nextDay!==day){el("div",nextDay+" · 导入历史",thread).className="date-divider";day=nextDay;}
      const node=event.role==="note"?el("small",event.text,thread):bubble(event.role,event.text);
      node.title=event.created_at||"来源未提供时间";
    }
    for(const t of s.turns){
      for(const m of t.input_members||[{text:t.text}])if(m.text)bubble("user",m.text);
      if(t.search_notice)bubble("persona",t.search_notice.text);
      if(t.scene_speech){for(const m of t.scene_speech.visible)bubble("persona",m.text);}
      else if(t.status==="completed" && t.reply)bubble("persona",t.display_reply||t.reply);
      if(t.reply_event_id && t.context?.import_style)button(thread,"这条不像",()=>feedback(currentPerson().id,t.id));
      if(["queued","running"].includes(t.status))button(thread,"停止本轮",()=>api().live_cancel(t.id));
      if(t.error_code)el("small",t.error_code,thread);
    }
    if(!thread.childElementCount)el("p","可以发送第一条消息。",thread);
  }
  async function send(){
    const p=currentPerson(),s=currentSession(),value=message.value;
    if(!p||!s||!value.trim()||s.sending)return;
    if(!state.connected){connection();return;}
    s.sending=true;
    const request=s.pending?.value===value?s.pending.request:newId();
    s.pending={value,request};
    try{
      await api().live_submit(p.id,s.id,value,request);
      s.pending=null;if(s.draft===value)s.draft="";s.version=null;
      if(s===currentSession())render();
    }finally{s.sending=false;}
  }
  async function feedback(pid,tid){
    const samples=await action("feedback",{persona_id:pid,turn_id:tid});
    const box=panel("本轮发言参考");
    for(const sample of samples){
      el("p",sample.text,box);
      for(const [mode,label] of [["demote","同类语境降低排序"],["disable","停用这条样例"]])
        button(box,label,async()=>{await action("feedback",{persona_id:pid,turn_id:tid,mode,sample:sample.id});dialog.close();});
    }
    button(box,"记录本轮待复核",async()=>{await action("feedback",{persona_id:pid,turn_id:tid,mode:"review"});dialog.close();});
  }
  async function profile(){
    const person=currentPerson(),pid=person.id,box=panel(person.profile.name);
    for(const [field,label] of Object.entries(titles))el("p",label+" · "+[].concat(person.profile[field]||[]).join("、"),box);
    const memory=await api().live_memory(pid);
    button(box,memory.enabled?"暂停自动记忆":"开启自动记忆并整理历史",async()=>{
      if(!memory.enabled && !confirm("授权当前角色使用已配置的整理模型处理历史及新对话？每批提取与复核按原后台额度计费。"))return;
      await api().live_memory(pid,!memory.enabled,memory.version);await profile();
    });
    const binding=await action("style",{persona_id:pid});
    if(!box.isConnected||!binding)return;
    el("p",binding.revoked?"风格来源已撤回":binding.enabled?"风格取样已开启":"风格取样已暂停",box);
    const update=async(extra)=>{await action("update",{persona_id:pid,expected_revision:binding.revision,...extra});await profile();};
    if(!binding.revoked)button(box,binding.enabled?"暂停风格取样":"启用风格取样",()=>update({enabled:!binding.enabled}));
    for(const version of binding.versions.filter(v=>v!==binding.version))
      button(box,"恢复版本 "+version.slice(0,8),()=>update({enabled:true,version}));
    button(box,"采用另一份草稿的风格",async()=>{
      const choices=panel("选择已核对的人物草稿");
      for(const doc of await action("list")){
        if(!["ready","partial_ready"].includes(doc.status))continue;
        const d=await action("status",{ident:doc.id});
        for(const [role,entry] of Object.entries(d.draft))button(choices,entry.spec.name+" · "+doc.created.slice(0,10),
          ()=>update({enabled:true,ident:doc.id,revision:d.revision,role}));
      }
    });
    el("p","待复核及已处理反馈 "+binding.feedback.length+" 条",box);
    button(box,"查看字段与原话依据",async()=>openImport(await action("status",{ident:binding.document}),binding.role));
  }
  function connection(){
    const box=panel("模型连接"),model=el("select",null,box);
    for(const name of ["deepseek-v4-pro","deepseek-flash"])model.add(new Option(name,name));
    model.value=state.model;const key=el("input",null,box);key.type="password";key.placeholder="官方 DeepSeek API Key";
    button(box,"保存连接",async()=>{await api().live_model(model.value,key.value);key.value="";dialog.close();await reload(state.current);});
  }
  async function connect(){
    if(active)return;
    active=true;byId("sample-mode").hidden=true;
    document.querySelector('[data-menu="reset"]')?.remove();
    let retries=0;
    while(true){
      try{await reload();break;}catch(e){if(++retries>=30){notify(String(e.message||e));break;}await new Promise(r=>setTimeout(r,1000));}
    }
    setInterval(()=>refresh().catch(e=>{if(pendingReceipt)notify(String(e.message||e));}),750);
  }
  window.addEventListener("pywebviewready",run(connect),{once:true});
  if(window.pywebview?.api)run(connect)();
  return {get active(){return active;},home:run(home),select:run(select),draw,send:run(send),profile:run(profile),connection};
})();
