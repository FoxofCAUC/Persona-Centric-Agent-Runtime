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
    item.onclick=run(async () => {
      const owner=dialog.contains(item) ? dialogBody.firstElementChild : null;
      if(owner?.inert) return;
      const controls=owner ? [...owner.querySelectorAll("button:not(:disabled)"), ...byId("dialog-actions").querySelectorAll("button:not(:disabled)")] : [item];
      if(owner) owner.inert=true;
      for(const control of controls) control.disabled=true;
      try {await fn();}
      finally {
        if(owner) owner.inert=false;
        for(const control of controls) control.disabled=false;
      }
    });
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
  function field(parent,label,value="",list=false,path=null) {
    const wrap=el("label",label,parent); wrap.className="field";
    const input=el("textarea",null,wrap); input.rows=2;
    if(path) input.dataset.cardPath=path;
    input.value=list?value.join("\n"):value; return input;
  }
  function readField(input,original,parse=value=>value.trim()) {
    const initial=input.value;
    return ()=>input.value===initial ? original : parse(input.value);
  }
  function lines(value) {
    return value.split("\n").map(line=>line.trim()).filter(Boolean);
  }
  function specForm(parent,spec) {
    const name=field(parent,"名称",spec.name,false,"spec.name");
    const fields=Object.entries(titles).map(([key,title])=>{
      const list=["core_traits","values_and_boundaries"].includes(key);
      const input=field(parent,title+(list?"（每行一项）":""),spec[key],list,"spec."+key);
      return [key,readField(input,spec[key],list?lines:undefined)];
    });
    const readName=readField(name,spec.name);
    return ()=>Object.fromEntries([["format_version",1],["name",readName()],
      ...fields.map(([key,read])=>[key,read()])]);
  }
  function inputField(box,label,type,value="") {
    const line=el("label",label,box); line.className="field";
    const control=el(type==="select"?"select":"input",null,line);
    if(type!=="select") {control.type=type;control.value=value;}
    return control;
  }
  function showRecipients(box,providers) {
    const names={dialogue:"续聊主模型",jev:"场景判断（按角色设置）",memory:"记忆整理（按本次选择）"};
    for(const [key,label] of Object.entries(names)) {
      const value=providers?.[key];
      el("p",label+" · "+(value?.name || "待配置")+(value?.base_url?" · "+value.base_url:""),box);
    }
  }
  async function importHistory() {
    const person=currentPerson();
    if(!person) return;
    const data=await api().pick_transcript();
    if(data && currentPerson()?.id===person.id)
      setupHistory(data,{personaId:person.id,targetName:person.profile.name});
  }
  function setupHistory(data,options,draft=null) {
    draft ??= {mapping:{...data.mapping},overrides:{...data.overrides},self:"",first:"",last:"",
               excluded:"",timezone:String(-new Date().getTimezoneOffset()),organize:false};
    const box=panel("历史聊天接续");
    el("strong",options.targetName ? "接续人物 · "+options.targetName : "从这份记录创建角色",box);
    el("p",data.format+" · "+data.preview.count+" 条记录",box);
    const names=Object.entries(data.counts);
    if(options.personaId && !options.sessionId){
      const mappingBox=groupBox(box,"说话人归属",names.length!==2);
      for(const [name,n] of names) {
        const select=inputField(mappingBox,name+" · "+n+" 条","select");
        for(const value of ["A","B","-"])select.add(new Option(value==="-"?"排除":value,value));
        select.value=draft.mapping[name];
        select.onchange=()=>{draft.mapping[name]=select.value;fillIdentity();};
      }
    }
    const self=inputField(box,"原记录中哪一方是我","select");
    function fillIdentity(){
      self.replaceChildren(new Option("请选择本人",""));
      for(const role of roles){
        const labels=names.filter(([name])=>draft.mapping[name]===role).map(([name])=>name);
        self.add(new Option(role+(labels.length?" · "+labels.join("、"):""),role));
      }
      self.value=draft.self;
    }
    fillIdentity();
    const range=groupBox(box,"消息范围与时间");
    const first=inputField(range,"从消息编号","number",draft.first),last=inputField(range,"至消息编号","number",draft.last);
    first.min=last.min="1";first.max=last.max=String(data.preview.count);
    first.placeholder="1";last.placeholder=String(data.preview.count);
    const excluded=inputField(range,"排除的消息编号（用逗号或空格分隔）","text",draft.excluded);
    const tz=inputField(range,"原记录未标明时区时采用","select");
    for(let n=-720;n<=840;n+=15){
      const value=Math.abs(n), time=String(Math.floor(value/60)).padStart(2,"0")+":"+String(value%60).padStart(2,"0");
      tz.add(new Option("UTC"+(n<0?"−":"+")+time,String(n)));
    }
    tz.value=draft.timezone;
    const organize=check(box,"整理为长期记忆 · 按该角色的后台模型设置和额度处理",draft.organize);
    const error=el("p",null,box);error.className="error";error.setAttribute("role","alert");
    function remember(){
      Object.assign(draft,{first:first.value,last:last.value,excluded:excluded.value,timezone:tz.value,
        organize:organize.checked,self:self.value});
    }
    button(box,options.personaId && !options.sessionId?"逐条核对或修正":"查看原件与逐条归属",()=>{
      remember();return evidence(data,0,false,()=>setupHistory(data,options,draft),
        options.personaId && !options.sessionId?draft.overrides:null,true);
    });
    const previewButton=button(box,"预览接续范围",async()=>{
      remember();
      try {
        if(!draft.self) throw Error("请选择原记录中的本人。");
        if(options.creation && !options.creation.roles.includes(draft.self==="A"?"B":"A"))
          throw Error("历史要接到对方角色，请返回角色卡选择对方或同时创建两人。");
        const omit=draft.excluded.trim()?draft.excluded.trim().split(/[,，、\s]+/):[];
        if(omit.some(v=>!(/^[1-9]\d*$/.test(v)))) throw Error("排除项请填写有效的消息编号。");
        const preview=await action("history_preview",{ident:data.id,revision:data.revision,self_role:draft.self,
          mapping:draft.mapping,overrides:draft.overrides,first:draft.first?Number(draft.first):null,
          last:draft.last?Number(draft.last):null,excluded:omit.map(Number),timezone_offset:Number(draft.timezone),
          persona_id:options.personaId || null,organize:draft.organize,session_id:options.sessionId || null,creation:options.creation || null});
        if(box.isConnected) confirmHistory(data,options,draft,preview);
      } catch(e){error.textContent=String(e.message||e);}
    });
    self.onchange=()=>{draft.self=self.value;previewButton.disabled=!self.value;};
    previewButton.disabled=!self.value;
    if(options.back) button(box,"返回角色卡",options.back);
  }
  function confirmHistory(data,options,draft,preview){
    const box=panel("确认历史接续");
    const peer=draft.self==="A"?"B":"A";
    const labels=role=>[role,...(preview.names[role] || [])].join(" · ");
    el("p","本人："+labels(draft.self)+"　对方："+labels(peer),box);
    el("p","接续人物："+(options.targetName || options.cards?.[peer].spec.name || labels(peer)),box);
    el("p","所选范围 · "+preview.total+" 条",box);
    for(const row of preview.preview_rows || []) {
      el("small","#"+row.id+" · "+row.speaker+" · "+(row.date || "时间待核"),box);
      el("p",row.text,box).className="import-original";
    }
    const receivers=groupBox(box,"使用这段历史的模型",true);
    showRecipients(receivers,preview.providers);
    el("p",draft.organize?"本次历史将按该角色的后台设置整理。":"本次历史用于续聊和查档，暂不做长期记忆整理。",box);
    if(options.personaId && !options.sessionId && !preview.reused)
      el("p","已有角色会开启独立会话，当前会话按正常流程封存。重复导入可打开原会话。",box);
    const consent=check(box,"确认本人、对方和消息范围，允许上述模型按所选用途使用原话");
    const operation=newId(), error=el("p",null,box);error.className="error";error.setAttribute("role","alert");
    const commit=button(box,"保存并打开接续会话",async()=>{
      if(!consent.checked)return;
      try {
        const result=await action("history_commit",{preview,confirmed:true,operation_id:operation,creation:options.creation || null});
        await reload(result.persona_id,true,result.session_id);
        if(result.reused) notify("已打开先前导入的会话。");
      } catch(e){error.textContent=String(e.message||e);}
    });
    commit.classList.add("primary");commit.disabled=true;
    consent.onchange=()=>{commit.disabled=!consent.checked;};
    button(box,"返回调整范围",()=>setupHistory(data,options,draft));
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
    const templates=el("div",null,box); templates.className="import-template-row";
    const status=el("span",null,templates); status.setAttribute("role","status");
    function showTemplate(value) {
      const sourceNames={builtin:"内置",file:"已导入"};
      status.textContent=value.active ? value.label+" · "+sourceNames[value.source] : value.message;
      status.title=value.active ? [value.version,value.model,value.sha256].filter(Boolean).join(" · ") : value.message;
    }
    const error=el("small",null,box); error.className="error"; error.hidden=true; error.setAttribute("role","alert");
    button(templates,"导入提示词文件",async()=>{
      error.hidden=true;
      try {
        const result=await api().import_prompt_file();
        if(result && box.isConnected) showTemplate(result);
      } catch(e) {error.textContent=String(e.message || e);error.hidden=false;}
    });
    const [docs,template]=await Promise.all([action("list"),action("prompt_status")]);
    if(!box.isConnected)return;
    if(!status.textContent) showTemplate(template);
    for(const d of docs)button(box,d.format+" · "+d.created.slice(0,16)+" · "+(jobs[d.status]||d.status),
      async()=>openImport(await action("status",{ident:d.id})));
  }

  async function evidence(data,start,prepared,back,overrides=null,showSections=false) {
    const p=await action("evidence",{ident:data.id,start,prepared,...(showSections ? {sections:true} : {})});
    const box=panel(prepared?"将用于分析的脱敏片段":"文件原话与消息归属");
    button(box,"返回",back);
    if (!prepared && p.total > 50) {
      const jump = el("label","跳至消息编号",box);
      jump.className = "field";
      const number = el("input",null,jump);
      number.type = "number"; number.min = "1"; number.max = String(p.total); number.value = String(start+1);
      button(box,"定位消息",()=>{
        const id = number.valueAsNumber;
        if (!Number.isInteger(id) || id < 1 || id > p.total) throw Error("请输入文件中的消息编号。");
        return evidence(data,id-1,prepared,back,overrides,showSections);
      });
    }
    for(const r of p.rows){
      const label=r.speaker+" · #"+r.id+(r.date?" · "+r.date:"");
      el("strong",label,box);
      const body=el("p",prepared?r.text:r.original,box);body.className="import-original";
      if(showSections && r.sections?.length){
        const sectionNames={body:"说话人正文",quote:"引用",forward:"转发",system:"系统",attachment:"附件",code:"代码"};
        const parts=Array.from(r.original);
        body.replaceChildren();
        for(const part of r.sections){
          el("small",sectionNames[part.kind] || part.kind,body).className="import-section-label";
          body.append(document.createTextNode(parts.slice(part.start-r.start,part.end-r.start).join("")));
        }
      }
      if(overrides && Object.hasOwn(data.mapping,r.speaker)){
        const sel=el("select",null,box);
        for(const v of ["","A","B","-"])sel.add(new Option(v===""?"沿用说话人归属":v==="-"?"排除":v,v));
        sel.value=overrides[r.id]||"";
        sel.onchange=()=>{if(sel.value) overrides[r.id]=sel.value; else delete overrides[r.id];};
      }
    }
    if(start>0)button(box,"上一页",()=>evidence(data,Math.max(0,start-50),prepared,back,overrides,showSections));
    if(start+50<p.total)button(box,"下一页",()=>evidence(data,start+50,prepared,back,overrides,showSections));
  }
  function mapping(data, map={...data.mapping}, overrides={...data.overrides}) {
    const box=panel("确认两位说话人");
    const range=data.preview||{};
    el("p",[data.format,range.count && range.count+" 条记录",range.from,range.to].filter(Boolean).join(" · "),box);
    const speakers = Object.entries(data.counts);
    if (speakers.length === 1)
      el("p","文件中只有一个昵称。同名的两位说话人可在逐条核对时指定归属，缺少一方时请补充记录。",box);
    for(const [speaker,count] of speakers){
      const label=el("label",speaker+"（"+count+" 条）",box), sel=el("select",null,label);
      for(const value of ["A","B","-"])sel.add(new Option(value==="-"?"排除":value,value));
      sel.value=map[speaker]; sel.onchange=()=>{map[speaker]=sel.value; checkMapping();};
    }
    button(box,"逐条核对或修正",()=>evidence(data,0,false,()=>mapping(data,map,overrides),overrides,true));
    const message = el("small",null,box);
    const generate = button(box,"确认说话人并继续",async()=>openImport(await action("plan",{
      ident:data.id,revision:data.revision,mapping:map,overrides})));
    function checkMapping() {
      const assigned = new Set([...Object.values(map),...Object.values(overrides)]);
      generate.disabled = !assigned.has("A") || !assigned.has("B");
      message.hidden = !generate.disabled;
      message.textContent = generate.disabled ? "需要给 A、B 各指定至少一条有效发言。" : "";
    }
    checkMapping();
  }

  async function openImport(data,role="A") {
    if(data.status==="preview"){mapping(data);return;}
    if(data.plan){return openPlanned(data,role);}
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
    button(box,"确认创建",async()=>{
      data=await saveDraft();
      const roles=both.checked?["A","B"]:[role];
      const operation=[data.id,data.revision,roles.join(""),Number(style.checked),0].join(".");
      const ids=await action("create",{ident:data.id,revision:data.revision,roles,
        enable_style:style.checked,continue_history:false,operation_id:operation});
      await reload(ids[both.checked && role==="B"?1:0]);
    });
    button(box,"使用新版分批提炼",async()=>mapping(await saveDraft()));
    button(box,"撤回该文件的提炼与风格使用",async()=>{if(confirm("停止该来源的提炼与表达取样？已确认还原的历史可在会话中查看。"))
      await openImport(await action("revoke",{ident:data.id}));});
  }

  const roles = ["A", "B"];
  const batchStates = {planned:"已计划", accepted:"已验收", partial:"部分观察待核", rejected:"已隔离", unknown:"发送结果待核对",
    unsent:"尚未派发", awaiting_approval:"等待请求审定", pending:"等待处理", sending:"正在派发", received:"已收到回执"};
  function batchState(state) { return batchStates[state] || state || "已计划"; }
  function nameOf(data, role) { return role + " · " + data.draft[role].spec.name; }

  function showCoverage(box, data) {
    const plan = data.plan;
    const sectionList = el("details",null,box);
    sectionList.open = data.status === "running" || data.status === "prepared";
    el("summary","分析覆盖 · " + roles.map(r=>r+" "+plan.roles[r].selected+"/"+plan.roles[r].eligible+" 批").join("、"),sectionList);
    for (const role of roles) {
      const info = plan.roles[role];
      const counts = new Map();
      for (const batch of info.batches) {
        const name = batchState(batch.state);
        counts.set(name, (counts.get(name) || 0) + 1);
      }
      const section = el("div", null, sectionList);
      el("strong", nameOf(data,role), section);
      el("p", "学习范围 " + info.messages + " 条 · 选中 " + info.selected + " / " + info.eligible
        + " 批" + (info.selected_messages == null ? "" : " · 本批范围共 " + info.selected_messages + " 条本人发言"), section);
      const dates = [info.first_date,info.last_date].filter(Boolean);
      if (dates.length) el("small", dates.join(" 至 "), section);
      if (counts.size) el("p", Array.from(counts, ([name,n]) => name + " " + n).join(" · "), section);
      if (info.merges?.length) el("p","汇总 · " + info.merges.map(m=>batchState(m.state)).join("、"),section);
      const reasons = {ref_outside_target_core:"引用超出本人的主片段",ref_quote:"引文与原话不符",
        ref_span:"引文区间无效",ref_source_span:"无法映射回原文件",foreign_observation:"引用其他人物或字段的观察",
        conflicting_sources:"支持与反例冲突",missing_support:"缺少支持证据",identity:"回执身份或批次不符",
        envelope:"回执结构无效",missing_template:"汇总配置待审定"};
      for (const [stage,batches] of [["提取",info.batches],["汇总",info.merges || []]]) {
        for (const batch of batches.filter(b=>b.rejected_count)) {
          const codes = batch.reasons.map(code=>reasons[code] || code).join("、");
          el("small",stage + "批次 " + batch.id.slice(0,8) + " · 隔离 " + batch.rejected_count + " 项 · " + codes,section)
            .className = "import-section-label";
        }
      }
    }
    const budget=Number.isInteger(plan.max_tokens) ? "每次最多输出 "+plan.max_tokens.toLocaleString("zh-CN")+" tokens。" : "";
    const notice=plan.prompt_release ? "跨段观察按需汇总，最后最多调用一次生成两张角色卡。" : "汇总与成卡按实例的用途授权执行，成卡最多一次。";
    el("p","计划分段提取 "+plan.requests+" 次。"+notice+budget+"官方 DeepSeek 按实际用量计费。",box);
  }

  async function batchEvidence(data, role, batch, back, start=0) {
    const page = await action("plan_evidence", {ident:data.id, plan_id:data.plan.id, role, batch_id:batch.id, start});
    const box = panel("本批待发的脱敏片段");
    button(box,"返回分析范围", back);
    for (const row of page.rows) {
      const isSupport = row.kind === "core" && row.speaker === role;
      el("strong", (isSupport ? "本人主片段" : "对话语境") + " · " + row.speaker + " · #" + row.id, box);
      el("small", "字符 [" + row.start + ", " + row.end + ")" + (row.date ? " · " + row.date : ""), box);
      el("p",row.text,box).className = "import-original";
    }
    const size = page.rows.length;
    if (start > 0) button(box,"上一页",()=>batchEvidence(data,role,batch,back,Math.max(0,start-50)));
    if (size && start + size < page.total) button(box,"下一页",()=>batchEvidence(data,role,batch,back,start+size));
  }

  function planScope(data, tab, back, selected, start=0) {
    const plan = data.plan, box = panel("核对分析范围");
    const choices = plan.choices;
    if (!selected) {
      selected = Object.fromEntries(roles.map(r => [r,new Set(choices[r].filter(b=>b.selected).map(b=>b.id))]));
    }
    button(box,"返回草稿",back);
    const nav = el("div",null,box); nav.className = "inline-actions";
    for (const r of roles) {
      const control = button(nav,nameOf(data,r),()=>planScope(data,r,back,selected));
      control.setAttribute("aria-pressed",String(r === tab));
    }
    const info = plan.roles[tab];
    el("p","共 " + info.messages + " 条本人学习材料 · " + choices[tab].length + " 个可选批次",box);
    const page = choices[tab].slice(start,start+20);
    const editable = data.status !== "running";
    for (const batch of page) {
      const cell = el("div",null,box); cell.className = "import-batch";
      const toggle = check(cell, "第 " + batch.ordinal + " 批 · " + batch.messages + " 条本人发言", selected[tab].has(batch.id));
      toggle.disabled = !editable;
      toggle.onchange = () => {
        if (toggle.checked && selected[tab].size >= 6) {
          toggle.checked = false;
          notify("每位人物每份计划最多选择六批。");
          return;
        }
        if (toggle.checked) selected[tab].add(batch.id); else selected[tab].delete(batch.id);
        changed();
      };
      const dates = [batch.first_date, batch.last_date].filter(Boolean);
      if (dates.length) el("small",dates.join(" 至 "),cell);
      if (batch.selected) button(cell,"查看第 " + batch.ordinal + " 批待发内容",
        ()=>batchEvidence(data,tab,batch,()=>planScope(data,tab,back,selected,start)));
    }
    const pager = el("div",null,box); pager.className = "inline-actions";
    if (start) button(pager,"上一页",()=>planScope(data,tab,back,selected,Math.max(0,start-20)));
    if (start + page.length < choices[tab].length) button(pager,"下一页",()=>planScope(data,tab,back,selected,start+page.length));
    const label = el("p",null,box);
    const apply = editable ? button(box,"按所选范围生成新计划",async()=>{
      const selection = Object.fromEntries(roles.map(r=>[r,choices[r].filter(b=>selected[r].has(b.id)).map(b=>b.id)]));
      const next = await action("plan",{ident:data.id,revision:data.revision,mapping:data.mapping,overrides:data.overrides,selection});
      await openImport(next,tab);
    }) : null;
    function changed() {
      label.textContent = "已勾选 A " + selected.A.size + " 批、B " + selected.B.size + " 批";
      if (apply) apply.disabled = !(selected.A.size + selected.B.size) ||
        !roles.some(r=>choices[r].some(b=>Boolean(b.selected) !== selected[r].has(b.id)));
    }
    changed();
  }

  async function claimSource(data, role, ref, back, backLabel="返回人设建议") {
    const pos = Number(ref.id);
    if (!Number.isSafeInteger(pos) || pos < 1) throw Error("原文位置无效。");
    const result = await action("evidence",{ident:data.id,start:pos-1,prepared:false,sections:true});
    const raw = result.rows.find(row=>String(row.id) === String(ref.id));
    if (!raw) throw Error("此项证据无法定位到原文件。");
    const box = panel("核对原话");
    button(box,backLabel,back);
    el("strong",(ref.speaker || role) + " · #" + ref.id + (raw.date ? " · " + raw.date : ""),box);
    if (ref.quote) {
      const hasRange = Number.isInteger(ref.start) && Number.isInteger(ref.end);
      el("small","已核验的脱敏引文" + (hasRange ? " · 字符 [" + ref.start + ", " + ref.end + ")" : ""),box);
      el("p",ref.quote,box).className = "import-original";
    }
    el("strong","文件原文 · " + raw.speaker,box);
    const body = el("p",null,box); body.className = "import-original";
    const chars = Array.from(raw.original), from = ref.raw_start-raw.start, to = ref.raw_end-raw.start;
    if (Number.isInteger(from) && Number.isInteger(to) && from >= 0 && from < to && to <= chars.length) {
      body.append(document.createTextNode(chars.slice(0,from).join("")));
      el("mark",chars.slice(from,to).join(""),body);
      body.append(document.createTextNode(chars.slice(to).join("")));
    } else body.textContent = raw.original;
  }

  function suggestions(box, data, role, save, readonly=false) {
    const claims = data.draft[role].claims;
    const wrap = el("div",null,box); wrap.className = "fields";
    const decisionNames = {accept:"已采纳", reject:"已舍弃"};
    function drawPage(start) {
      wrap.replaceChildren();
      el("h3","人设建议 · " + claims.length + " 项",wrap);
      for (const claim of claims.slice(start,start+12)) {
        const item = el("details",null,wrap);
        const pending = claim.needs_review ? (readonly ? "待核" : "冲突待核") : "待采纳";
        const status = decisionNames[claim.decision] || pending;
        el("summary",(titles[claim.field] || claim.field) + " · " + status + " · " + claim.value,item);
        if (claim.scope) el("p","适用情境 · " + claim.scope,item);
        const refs = el("div",null,item); refs.className = "fields";
        if (claim.support_groups) el("small","来自 " + claim.support_groups.length + " 个去重组",refs);
        for (const [label,evidence] of [["依据",claim.evidence],["反例",claim.counter_evidence || []]]) {
          for (const ref of evidence) {
            button(refs,label + " · " + (ref.speaker || role) + " · #" + ref.id + " · " + ref.quote,async()=>{
              const current = readonly ? data : await save();
              await claimSource(current,role,ref,()=>readonly ? archiveView(current,role) : openImport(current,role));
            }).classList.add("import-citation");
          }
        }
        if (readonly) continue;
        const actions = el("div",null,item); actions.className = "inline-actions";
        for (const [decision,label] of [["accept","采纳"],["reject","舍弃"]]) {
          if (claim.decision === decision || (decision === "accept" && claim.needs_review)) continue;
          button(actions,label,async()=>{
            const current = await save();
            const updated = await action("review",{ident:current.id,revision:current.revision,
              plan_id:current.plan.id,role,suggestion_id:claim.id,decision});
            await openImport(updated,role);
          });
        }
      }
      const nav = el("div",null,wrap); nav.className = "inline-actions";
      if (start) button(nav,"上一页建议",()=>drawPage(Math.max(0,start-12)));
      if (start+12 < claims.length) button(nav,"下一页建议",()=>drawPage(start+12));
    }
    drawPage(0);
  }

  function archiveView(data, role) {
    const box = panel("归档草稿 · " + nameOf(data,role));
    button(box,"返回当前草稿",async()=>openImport(await action("status",{ident:data.id}),role));
    const other = role === "A" ? "B" : "A";
    button(box,"查看归档 " + other,()=>archiveView(data,other));
    const draft = data.draft[role];
    for (const [key,label] of Object.entries(titles)) {
      el("strong",label,box);
      el("p",[].concat(draft.spec[key] || []).join("\n") || "待补",box).className = "import-original";
    }
    if (draft.relationship) el("p","关系说明 · " + draft.relationship,box);
    if (draft.claims?.length) suggestions(box,data,role,null,true);
  }

  const sceneLabels = {daily:"日常交流",relationship:"情绪与关系",help:"明确求助",closing:"收束",fallback:"兜底"};
  const cardStates = {none:"尚未整合", available:"可整合", awaiting_approval:"等待合成请求审定",
    sending:"正在生成两张角色卡", running:"正在生成两张角色卡", received:"已收到首次回执",
    unknown:"合成结果待核对", unsent:"上次请求未发送", ready:"角色卡已生成",
    stale:"合成来源已变化", not_ready:"等待两份画像就绪", capacity:"两份画像超过本次输入容量",
    unconfigured:"角色卡合成模板待配置", no_key:"请先配置官方 DeepSeek 连接",invalid_config:"角色卡配置需要核对",
    partial:"可用字段已预填", manual_only:"画像材料不足，可补填后保存所选人物", failed:"合成未完成", revoked:"来源已撤回"};
  const groups = {
    identity:["身份",{aliases:"别名",species:"种族",gender:"性别",pronouns:"代词"}],
    age:["年龄与生日",{fixed_age:"固定年龄",birth_date:"出生日期",birthday:"生日（月-日）"}],
    appearance:["外貌",{height_cm:"身高（厘米）",weight_kg:"体重（千克）",blood_type:"血型",
      hair_color:"发色",eye_color:"瞳色",description:"外貌描述"}],
    biography:["经历",{hometown:"家乡",occupation:"职业",education:"教育经历",family:"家庭",relationships:"人物关系"}],
    interests:["兴趣",{likes:"喜欢",dislikes:"不喜欢",hobbies:"爱好",notes:"兴趣补充"}],
    social:["社交表达",{public_image:"外在印象",humor:"幽默方式",context_rules:"不同情境的表达"}],
    world:["世界设定",{name:"世界名称",description:"世界描述"}],
  };
  function groupBox(parent,title,open=false) {
    const detail=el("details",null,parent); detail.className="import-editor-section"; detail.open=open;
    el("summary",title,detail);
    const body=el("div",null,detail); body.className="fields";
    return body;
  }
  function numberOrNull(value,integer=false) {
    if (!value) return null;
    const number=Number(value);
    if (!Number.isFinite(number) || (integer && !Number.isInteger(number))) throw Error("请填写有效数值。");
    return number;
  }
  function characterForm(box, character) {
    const readers=[];
    for(const [group,[title,fields]] of Object.entries(groups)){
      const section=groupBox(box,title);
      for(const [key,label] of Object.entries(fields)){
        const old=character[group][key], list=Array.isArray(old);
        const input=field(section,label+(list?"（每行一项）":""),old??"",list,"character."+group+"."+key);
        const initial=input.value;
        readers.push(result=>{
          if(input.value===initial) return;
          const text=input.value.trim();
          let value=text || null;
          if (list) value=text.split("\n").map(v=>v.trim()).filter(Boolean);
          if (["fixed_age","height_cm","weight_kg"].includes(key)) value=numberOrNull(text,key==="fixed_age");
          result[group][key]=value;
        });
      }
    }
    const extras=groupBox(box,"表达样例与补充资料");
    const examples=new Map(), entries=new Map();

    function addExample(example,open=false){
      const sub=groupBox(extras,"表达样例 "+(examples.size+1),open);
      const text=field(sub,"原话",example.text), scenario=field(sub,"情境",example.scenario);
      const keys=field(sub,"关键词（每行一项）",example.keywords,true);
      const enabled=check(sub,"启用样例",example.enabled);
      const readText=readField(text,example.text), readScenario=readField(scenario,example.scenario);
      const readKeys=readField(keys,example.keywords,lines);
      examples.set(example.id,()=>({...example,text:readText(),scenario:readScenario(),
                                    keywords:readKeys(),enabled:enabled.checked}));
      button(sub,"移除此样例",()=>{examples.delete(example.id);sub.parentElement.remove();});
    }
    function addEntry(entry,open=false){
      const sub=groupBox(extras,entry.title||"补充资料 "+(entries.size+1),open);
      const title=field(sub,"标题",entry.title), category=field(sub,"类别",entry.category), body=field(sub,"正文",entry.text);
      const keys=field(sub,"关键词（每行一项）",entry.keywords,true);
      const enabled=check(sub,"启用条目",entry.enabled);
      const visible=check(sub,"允许对话引用",entry.visibility==="conversation");
      const readTitle=readField(title,entry.title),readBody=readField(body,entry.text);
      const readCategory=readField(category,entry.category),readKeys=readField(keys,entry.keywords,lines);
      entries.set(entry.id,()=>({...entry,title:readTitle(),text:readBody(),category:readCategory(),
                                keywords:readKeys(),enabled:enabled.checked,
                                visibility:visible.checked?"conversation":"creator_only"}));
      button(sub,"移除此条目",()=>{entries.delete(entry.id);sub.parentElement.remove();});
    }
    for(const item of character.examples) addExample(item);
    for(const item of character.lorebook) addEntry(item);
    const additions=el("div",null,extras);additions.className="inline-actions";
    button(additions,"添加表达样例",()=>addExample({id:newId(),text:"",scenario:"",keywords:[],enabled:false},true));
    button(additions,"添加补充资料",()=>addEntry({id:newId(),title:"",text:"",category:"",priority:0,keywords:[],
                                                dependencies:[],enabled:false,visibility:"creator_only"},true));
    return ()=>{
      const result=structuredClone(character);
      for(const reader of readers) reader(result);
      if (result.age.fixed_age!==null && result.age.birth_date) throw Error("固定年龄和出生日期请选择一种年龄依据。");
      if(result.age.fixed_age!==character.age.fixed_age || result.age.birth_date!==character.age.birth_date)
        result.age.policy=result.age.fixed_age!==null?"fixed":result.age.birth_date?"calendar":"unspecified";
      result.examples=Array.from(examples.values(),read=>read());
      result.lorebook=Array.from(entries.values(),read=>read());
      return result;
    };
  }
  function pathLabel(path) {
    if (path.startsWith("spec.")) return titles[path.slice(5)] || "名称";
    if (path.startsWith("scenes.")) return sceneLabels[path.slice(7)];
    if (path.startsWith("jev.criteria.")) return "Jev · "+sceneLabels[path.slice(13)];
    if (path.startsWith("character.")) {
      const [,group,key]=path.split(".");
      return groups[group]?.[1]?.[key] || ({examples:"表达样例",lorebook:"补充资料"})[group] || path;
    }
    return ({relationship:"关系说明",common:"共通人设提示","jev.instruction":"Jev 总决策说明"})[path] || path;
  }
  function cardPath(card, path) {
    if (typeof path !== "string") return null;
    const parts=path.split(".");
    const [section,key,subkey]=parts;
    const sceneSlot=parts.length===2 && section==="scenes" && Object.hasOwn(sceneLabels,key);
    const criterion=parts.length===3 && section==="jev" && key==="criteria" && Object.hasOwn(sceneLabels,subkey);
    const persona=parts.length===2 && section==="spec" && Object.hasOwn(titles,key);
    const fact=section==="character" && ((parts.length===2 && ["examples","lorebook"].includes(key)) ||
      (parts.length===3 && Object.hasOwn(groups,key) && Object.hasOwn(groups[key][1],subkey)));
    if(!["relationship","common","jev.instruction"].includes(path) && !sceneSlot && !criterion && !persona && !fact)
      return null;
    let parent=card;
    for(const part of parts.slice(0,-1)) {
      if(!Object.hasOwn(parent,part)) return null;
      parent=parent[part];
    }
    return {parent,key:parts.at(-1)};
  }
  function pendingProposals(box,card,onApply,scenesOnly=false,onApplyText=null) {
    const proposals=card.issues.filter(i=>i.severity!=="conflict" && Object.hasOwn(i,"proposal") && cardPath(card,i.field) &&
      (!scenesOnly || i.field==="common" || i.field.startsWith("scenes.") || i.field.startsWith("jev.")));
    if(!proposals.length) return;
    const pending=proposals.filter(item=>{
      const slot=cardPath(card,item.field);
      return JSON.stringify(slot.parent[slot.key])!==JSON.stringify(item.proposal);
    }).length;
    const pane=groupBox(box,pending?"可选补充 · "+pending:"补充填写记录");
    const prose=proposals.filter(i=>!i.field.startsWith("character."));
    if(onApplyText && prose.some(i=>{
      const slot=cardPath(card,i.field);
      return JSON.stringify(slot.parent[slot.key])!==JSON.stringify(i.proposal);
    }))button(pane,"核对并填入本角色文案",()=>onApplyText(prose));
    for(const item of proposals) {
      const title=pathLabel(item.field), section=groupBox(pane,title);
      el("p",item.reason,section);
      const value=item.proposal;
      el("pre",typeof value==="string" ? value : JSON.stringify(value,null,2),section).className="import-original";
      const {parent,key}=cardPath(card,item.field);
      if(JSON.stringify(parent[key])===JSON.stringify(value)) el("small","已填入当前草稿",section);
      else button(section,"核对后填入「"+title+"」",()=>onApply(item));
    }
  }
  function fillTextProposals(card,issues) {
    const updated=structuredClone(card), inserts=[],replacements=[];
    for(const issue of issues){
      const slot=cardPath(updated,issue.field);
      if(!slot || issue.field.startsWith("character.")) continue;
      const before=slot.parent[slot.key];
      if(JSON.stringify(before)===JSON.stringify(issue.proposal)) continue;
      const filled=before!==null && before!=="" && (!Array.isArray(before) || before.length>0);
      (filled?replacements:inserts).push(pathLabel(issue.field));
      slot.parent[slot.key]=structuredClone(issue.proposal);
    }
    if(!inserts.length && !replacements.length) return null;
    const summary=["将候选文案作为手工填写保存到「"+card.spec.name+"」的草稿。"];
    if(inserts.length) summary.push("填入空项："+inserts.join("、"));
    if(replacements.length) summary.push("覆盖已有内容："+replacements.join("、"));
    return confirm(summary.join("\n")) ? updated : null;
  }
  function fillProposal(card,issue) {
    const updated=structuredClone(card), slot=cardPath(updated,issue.field);
    if(!slot) throw Error("该字段需要在表单内填写。");
    const before=slot.parent[slot.key];
    const occupied=before!==null && before!=="" && (!Array.isArray(before) || before.length>0);
    if(occupied && JSON.stringify(before)!==JSON.stringify(issue.proposal) &&
       !confirm("覆盖草稿中已有的「"+pathLabel(issue.field)+"」？")) return null;
    slot.parent[slot.key]=structuredClone(issue.proposal);
    if(issue.field==="character.age.fixed_age" || issue.field==="character.age.birth_date"){
      const age=updated.character.age;
      if(age.fixed_age!==null && age.birth_date) throw Error("请先在人物资料中选择固定年龄或出生日期。");
      age.policy=age.fixed_age!==null?"fixed":age.birth_date?"calendar":"unspecified";
    }
    return updated;
  }
  function cardProof(parent, data, role, card, save) {
    const sources=Object.entries(card.sources);
    const issues=card.issues.filter(i=>i.severity!=="conflict" && (!Object.hasOwn(i,"proposal")||!cardPath(card,i.field)));
    if(!sources.length && !issues.length && !card.generated_fields?.length) return;
    const box=groupBox(parent,"字段出处与待补项");
    if(card.generated_fields?.length)
      el("p","模型整理的文案 · "+card.generated_fields.map(pathLabel).join("、"),box);
    for (const issue of issues) el("p",pathLabel(issue.field)+" · "+issue.reason,box);
    for (const [path,observations] of sources) {
      const part=groupBox(box,pathLabel(path));
      for(const obs of observations) {
        el("strong",obs.value,part);
        if(obs.scope) el("p",obs.scope,part);
        for (const ref of obs.evidence || []) {
          button(part,role+" · #"+ref.id+" · "+ref.quote,async()=>{
            await save();
            await claimSource(data,role,ref,async()=>openImport(await action("status",{ident:data.id}),role),"返回角色卡");
          }).classList.add("import-citation");
        }
      }
    }
  }
  function sceneMaterial(box,card) {
    const common=field(box,"角色共通提示",card.common,false,"common");
    const readCommon=readField(common,card.common), inputs={};
    for(const [key,label] of Object.entries(sceneLabels)) {
      const group=groupBox(box,label);
      const segment=field(group,"场景正文",card.scenes[key],false,"scenes."+key);
      const preview=groupBox(group,"完整场景提示");
      const body=el("pre",null,preview); body.className="import-original";
      const readSegment=readField(segment,card.scenes[key]);
      const update=()=>{body.textContent=readCommon() && readSegment() ? readCommon()+"\n\n"+readSegment() : "待补";};
      common.addEventListener("input",update); segment.addEventListener("input",update); update();
      inputs[key]=readSegment;
    }
    const routing=groupBox(box,"Jev 场景判断");
    const instruction=field(routing,"总决策说明",card.jev.instruction,false,"jev.instruction");
    const readInstruction=readField(instruction,card.jev.instruction), criteria={};
    for(const [key,label] of Object.entries(sceneLabels)){
      const input=field(routing,label+"判别依据",card.jev.criteria[key],false,"jev.criteria."+key);
      criteria[key]=readField(input,card.jev.criteria[key]);
    }
    const values=readers=>Object.fromEntries(Object.entries(readers).map(([key,read])=>[key,read()]));
    return ()=>({common:readCommon(),scenes:values(inputs),jev:{instruction:readInstruction(),criteria:values(criteria)}});
  }
  function cardConflicts(cards, selected=roles) {
    const items=[];
    for (const role of selected) {
      const card=cards[role];
      const seen=new Set();
      for(const issue of card?.issues || []) {
        if(issue.severity!=="conflict" || seen.has(issue.field)) continue;
        seen.add(issue.field);
        items.push({role,field:issue.field,reason:issue.reason,alternatives:issue.alternatives || [],
          observations:issue.observations || card.sources[issue.field] || []});
      }
    }
    return items;
  }

  const notifiedConflicts = new Map();
  function conflictWarning(items, cards, locate) {
    if (!items.length || !dialog.open) return;
    const popup=el("dialog",null,document.body);
    popup.className="import-conflict-dialog"; popup.setAttribute("role","alertdialog");
    popup.setAttribute("aria-labelledby","card-conflict-title");popup.setAttribute("aria-describedby","card-conflict-message");
    const header=el("div",null,popup);header.className="dialog-head";
    el("h2","角色卡有严重冲突",header).id="card-conflict-title";
    const body=el("div",null,popup);body.className="dialog-body";
    el("p","请核对以下字段的依据，修改内容或确认保留当前值。",body).id="card-conflict-message";
    for(const issue of items){
      const group=el("section",null,body);group.className="import-conflict-row";
      el("strong",issue.role+" · "+cards[issue.role].spec.name+" · "+pathLabel(issue.field),group);
      el("p",issue.reason,group);
      button(group,"处理 "+issue.role+" 的「"+pathLabel(issue.field)+"」",async()=>{
        popup.close();await locate(issue.role,issue.field);
      });
    }
    const foot=el("div",null,popup);foot.className="dialog-foot";
    button(foot,"返回角色卡",()=>popup.close());
    const closeOnExit=()=>{if(popup.open) popup.close();};
    dialog.addEventListener("close",closeOnExit);
    popup.addEventListener("close",()=>{
      dialog.removeEventListener("close",closeOnExit);
      popup.remove();
    },{once:true});
    popup.showModal();
  }

  function focusCardField(box,path) {
    const control=[...box.querySelectorAll("[data-card-path]")].find(el=>el.dataset.cardPath===path);
    if(!control) return;
    for(let parent=control.parentElement; parent && parent!==box; parent=parent.parentElement)
      if(parent.tagName==="DETAILS") parent.open=true;
    const focus=()=>{if(control.isConnected){control.scrollIntoView({block:"center"});control.focus();}};
    focus();
    setTimeout(focus,0);
  }

  function addConflictResolution(box, issue, save, finish, openSource) {
    const control=[...box.querySelectorAll("[data-card-path]")].find(input=>input.dataset.cardPath===issue.field);
    const pane=el("div",null,box);pane.className="import-conflict-row";
    const parent=control?.closest("label.field") || control;
    if(parent) parent.after(pane);
    else {pane.dataset.cardPath=issue.field; pane.tabIndex=-1;}
    el("strong","需核对 · "+pathLabel(issue.field),pane);
    el("p",issue.reason,pane);
    if(issue.alternatives.length) {
      el("strong","分析返回的候选值",pane);
      const list=el("ul",null,pane);
      for(const variant of issue.alternatives) {
        const value=Array.isArray(variant.value)?variant.value.join("、"):String(variant.value);
        el("li",pathLabel(variant.field)+" · "+value,list);
      }
    }
    if(issue.observations.length) {
      const proof=groupBox(pane,"相关原话");
      for(const obs of issue.observations) {
        el("p",obs.value,proof);
        for(const [name,refs] of [["依据",obs.evidence],["反例",obs.counter_evidence]])
          for(const ref of refs||[])button(proof,name+" · #"+ref.id+" · "+ref.quote,()=>openSource(ref));
      }
    }
    button(pane,"确认「"+pathLabel(issue.field)+"」当前填写",async()=>{
      const result=await save([issue.field]);finish(result);
    });
  }

  function openCard(data, role, job, creation={}) {
    const card=job.cards[role],other=role==="A"?"B":"A";
    const box=panel("核对角色卡 · "+role+" · "+card.spec.name);
    el("p",cardStates[job.state] || job.state,box);
    const blockers=cardConflicts(job.cards);
    async function locate(target,path){
      if(target===role){focusCardField(box,path);return;}
      openCard(data,target,await save(),{both:both.checked,focusField:path});
    }
    if(blockers.length) button(box,"处理严重冲突（"+blockers.length+"）",()=>conflictWarning(blockers,job.cards,locate));
    if(job.stale) {
      el("p","当前显示最新草稿。",box);
      const previous=job.stale_candidate?.[role];
      if(previous){
        const compare=groupBox(box,"旧版本候选");
        for(const [key,title] of Object.entries(titles)){
          el("strong",title,compare);el("p",[].concat(previous.spec[key]||[]).join("\n") || "待补",compare).className="import-original";
        }
        for(const [title,body] of [["共通提示",previous.common],["Jev 总决策说明",previous.jev.instruction],
                                   ...Object.entries(sceneLabels).map(([key,label])=>[label,previous.scenes[key]])]){
          el("strong",title,compare);el("p",body || "待补",compare).className="import-original";
        }
      }
    }
    if(job.error) el("p",job.error==="invalid_receipt"?"成卡回执未通过格式校验，请手工补齐。":String(job.error),box);
    const nav=el("div",null,box); nav.className="inline-actions";
    button(nav,"暂存并查看 "+other,async()=>openCard(data,other,await save(),{both:both.checked}));
    button(nav,"查看原画像",async()=>{
      await save(); await openPlanned(await action("status",{ident:data.id}),role,{source:true});
    });
    async function saveProposal(issues,fill){
      const current=await save();
      const proposal=fill(current.cards[role],issues);
      if(!proposal) return;
      const next=await action("card_edit",{ident:data.id,revision:data.revision,plan_id:data.plan.id,
                              card_revision:current.revision,role,card:proposal});
      openCard(data,role,next,{both:both.checked});
    }
    pendingProposals(box,card,issue=>saveProposal(issue,fillProposal),false,
                     items=>saveProposal(items,fillTextProposals));
    const base=groupBox(box,"基础人设与语言风格",true);
    const readSpec=specForm(base,card.spec);
    const relation=readField(field(base,"关系说明",card.relationship,false,"relationship"),card.relationship);
    const nativePreview=groupBox(base,"预览普通发言人设正文");
    const ordinary=el("pre",null,nativePreview); ordinary.className="import-original";
    let previewRequest=0;
    const refreshNative=run(async()=>{
      const current=++previewRequest;
      ordinary.textContent="";
      if (!nativePreview.parentElement.open) return;
      const preview=await action("card_preview",{spec:readSpec()});
      if(current===previewRequest && nativePreview.isConnected) ordinary.textContent=preview;
    });
    nativePreview.parentElement.addEventListener("toggle",refreshNative);
    base.addEventListener("input",()=>{nativePreview.parentElement.open=false; ++previewRequest; ordinary.textContent="";});
    const factBox=groupBox(box,"人物资料");
    const readCharacter=characterForm(factBox,card.character);
    const materialBox=groupBox(box,"场景提示与 Jev");
    const readScenes=sceneMaterial(materialBox,card);
    cardProof(box,data,role,card,save);
    async function save(resolvedFields=[]){
      const changed={...card,spec:readSpec(),relationship:relation(),
                     character:readCharacter(),...readScenes()};
      if (!resolvedFields.length && JSON.stringify(changed) === JSON.stringify(job.cards[role])) return job;
      job=await action("card_edit",{ident:data.id,revision:data.revision,plan_id:data.plan.id,
        card_revision:job.revision,role,card:changed,...(resolvedFields.length?{resolved_fields:resolvedFields}:{})});
      return job;
    }
    button(box,"暂存角色卡",async()=>openCard(data,role,await save(),{both:both.checked}));
    const finalActions=el("div",null,byId("dialog-actions"));
    finalActions.className="import-create-actions";
    const both=check(finalActions,"同时创建 A 和 B",Boolean(creation.both));
    const mode=inputField(box,"保存后打开的会话","select");
    mode.add(new Option("从空白开始","empty"));
    mode.add(new Option("带历史开始","history"));
    const scene=check(materialBox,"核准已预览的场景提示和 Jev 判别，配置齐备时启用");
    const publish=button(finalActions,"确认创建并打开聊天",async()=>{
      const selected=both.checked?roles:[role], enableScene=scene.checked;
      const result=await save();
      const remaining=cardConflicts(result.cards,selected);
      if(remaining.length){
        conflictWarning(remaining,result.cards,locate);
        return;
      }
      for(const r of selected) {
        const spec=result.cards[r].spec;
        const missing=["name",...Object.keys(titles)].find(key=>!spec[key]?.length);
        if(missing) {
          if(r!==role) openCard(data,r,result,{both:both.checked});
          const title=titles[missing]||"名称";
          const target=[...dialogBody.querySelectorAll("label.field")].find(n=>n.firstChild?.textContent.startsWith(title));
          target?.querySelector("textarea")?.focus();
          throw Error(r+" 的「"+title+"」需要填写。");
        }
      }
      if(mode.value==="history"){
        setupHistory(data,{cards:result.cards,creation:{plan_id:data.plan.id,card_revision:result.revision,
          roles:selected,enable_scene:enableScene},back:()=>openCard(data,role,result,{both:both.checked})});
        return;
      }
      const request=[data.id.slice(0,32),data.revision,result.revision,selected.join(""),Number(enableScene)].join(".");
      const ids=await action("card_create",{ident:data.id,revision:data.revision,plan_id:data.plan.id,
        card_revision:result.revision,roles:selected,enable_scene:enableScene,operation_id:request,confirmed:true});
      await reload(ids[both.checked && role==="B"?1:0]);
    });
    publish.classList.add("primary");
    for(const issue of blockers.filter(i=>i.role===role)) {
      addConflictResolution(box,issue,save,next=>openCard(data,role,next,{both:both.checked}),
        async ref=>{
          const current=await save();
          await claimSource(data,role,ref,()=>openCard(data,role,current,{both:both.checked,focusField:issue.field}),"返回冲突字段");
        });
    }
    const sessionKey=JSON.stringify([data.id,data.plan.id,data.revision]);
    const currentKeys=blockers.map(item=>item.role+" "+item.field);
    const prior=notifiedConflicts.get(sessionKey);
    if(currentKeys.length) notifiedConflicts.set(sessionKey,new Set(currentKeys)); else notifiedConflicts.delete(sessionKey);
    if(creation.focusField) focusCardField(box,creation.focusField);
    else if(currentKeys.some(key=>!prior?.has(key))) conflictWarning(blockers,job.cards,locate);
  }

  async function openPlanned(data, role, creation={}) {
    const box = panel("聊天人设草稿");
    const plan = data.plan;
    let read=null, relation=null;
    el("p",data.format + " · " + (data.status==="partial_ready" ? "部分结果可审阅" : jobs[data.status] || data.status),box);
    if (data.status === "revoked") {button(box,"返回",home); return;}
    showCoverage(box,data);
    if (data.status === "running") {
      button(box,"查看本次分析范围",()=>planScope(data,role,()=>openImport(data,role)));
      button(box,"取消提炼",async()=>openImport(await action("cancel",{ident:data.id}),role));
      setTimeout(run(async()=>{
        if (!box.isConnected || !dialog.open) return;
        const latest = await action("status",{ident:data.id});
        if (box.isConnected && dialog.open) await openImport(latest,role);
      }),900);
      return;
    }
    const job = await action("card_status",{ident:data.id});
    if(!box.isConnected) return;
    if(!creation.source && job.cards?.[role] &&
       (job.revision > 0 || creation.editCard || job.state==="manual_only") && !["sending","running"].includes(job.state)) {
      return openCard(data,role,job,creation);
    }
    const waiting=["sending","running"].includes(job.state);
    const hasBoth=roles.every(r=>data.draft[r]?.claims.some(c=>c.decision!=="reject"));
    const draft = data.draft[role];
    const refreshed = next => openImport(next,role);
    if(waiting) {
      el("p",cardStates[job.state],box);
      button(box,"取消本次成卡",async()=>refreshed(await action("cancel",{ident:data.id})));
      setTimeout(run(async()=>{if(box.isConnected && dialog.open)await refreshed(await action("status",{ident:data.id}));}),850);
      return;
    }
    if(job.cards?.[role]) button(box,job.revision>0 ? "打开角色卡" : "手工编辑角色卡",async()=>{
      const current=await save();
      const latest=await action("card_status",{ident:current.id});
      if(!latest.cards?.[role]) throw Error("最新画像尚未就绪。");
      openCard(current,role,latest,creation);
    });
    if(hasBoth && job.state==="received"){
      button(box,"校验已收成卡回执",async()=>{
        const current=await save();
        await action("card_compose",{ident:current.id,revision:current.revision,plan_id:plan.id,confirmed:true});
        await refreshed(await action("status",{ident:current.id}));
      });
    } else if(hasBoth && ["available","unsent"].includes(job.state) && !job.stale) {
      const ready=check(box,"允许官方 DeepSeek 用这两份画像生成角色卡，最多一次额外请求");
      const make=button(box,"整合两张角色卡",async()=>{
        if(!ready.checked) throw Error("请先确认本次合成用途。");
        const current=await save();
        const result=await action("card_compose",{ident:current.id,revision:current.revision,plan_id:plan.id,confirmed:true});
        if(result.state==="awaiting_approval") notify(cardStates.awaiting_approval);
        await refreshed(await action("status",{ident:data.id}));
      });
      make.disabled=true;
      ready.onchange=()=>{make.disabled=!ready.checked||!job.configured;};
      if(!job.configured) el("small","合成模板待配置。",box);
    } else if(["unknown","failed","capacity","no_key","invalid_config","unconfigured","awaiting_approval","stale"].includes(job.state)) {
      el("p",cardStates[job.state],box);
      if(job.state==="no_key") button(box,"配置模型连接",async()=>{await save();connection();});
      if(["awaiting_approval","unconfigured","invalid_config","unknown"].includes(job.state))
        button(box,"重新检查成卡状态",async()=>refreshed(await save()));
    }
    button(box,"查看或调整本次分析范围",async()=>{
      const current = await save();
      planScope(current,role,()=>openImport(current,role));
    });
    if (!plan.configured) {
      el("p","提炼配置待审定。可先核对范围和编辑草稿。",box);
      button(box,"按当前配置重新计划",async()=>{
        const current = await save();
        await refreshed(await action("plan",{ident:data.id,revision:current.revision,mapping:current.mapping,
          overrides:current.overrides}));
      });
    }
    if (data.status !== "ready") {
      if(!state.connected) button(box,"配置本次模型连接",async()=>{
        await save(); connection(async()=>openImport(await action("status",{ident:data.id}),role));
      });
      const consent = check(box,"允许官方 DeepSeek 按上述范围提取、按需汇总并生成两张角色卡");
      const analyze = button(box,"分析并自动填写两张角色卡",async()=>{
        if (!consent.checked) throw Error("请先确认本次分析用途。");
        const current = await save();
        await refreshed(await action("analyze",{ident:data.id,revision:current.revision,plan_id:plan.id,confirmed:true,compose_card:true}));
      });
      analyze.disabled = true;
      consent.onchange = () => {analyze.disabled = !consent.checked || !plan.configured || !state.connected;};
      button(box,"从现有回执恢复",async()=>{
        const current = await save();
        await refreshed(await action("replay",{ident:data.id,revision:current.revision,plan_id:plan.id}));
      });
    }
    if (!hasBoth && !roles.some(r=>data.draft[r]?.claims.length)) {
      button(box,"重新核对说话人",()=>mapping(data));
      return;
    }
    const other = role === "A" ? "B" : "A";
    el("h3",nameOf(data,role),box);
    const stats = plan.roles[role].stats;
    if (stats) {
      const detail = el("details",null,box); el("summary","本地表达统计",detail);
      el("p",stats.messages + " 条完整的本人发言 · 平均 " + stats.mean_chars + " 字 · 连发 " + stats.burst_messages + " 条",detail);
      const marks = Object.entries(stats.ending_punctuation).map(([mark,count])=>mark + " " + count + " 次");
      if (marks.length) el("p","句末标点 · " + marks.join("、"),detail);
      for (const repeated of stats.common_messages)
        button(detail,"重复表达 #" + repeated.id + " · " + repeated.count + " 次",async()=>{
          const current = await save();
          await claimSource(current,role,repeated,()=>openImport(current,role));
        });
    }
    button(box,"暂存并查看 " + other,async()=>openImport(await save(),other));
    if (draft.claims.some(claim => claim.decision === "pending" && !claim.needs_review &&
        claim.field !== "values_and_boundaries")) {
      button(box,"采纳本人物可用建议",async()=>{
        const current = await save();
        await refreshed(await action("review_all",{ident:data.id,revision:current.revision,plan_id:plan.id,role}));
      });
    }
    suggestions(box,data,role,save);
    read = specForm(box,draft.spec);
    relation = field(box,"关系说明",draft.relationship);
    async function save() {
      if(!read) return data;
      const spec = read(), previous = data.draft[role];
      if (Object.keys(spec).every(k=>JSON.stringify(spec[k])===JSON.stringify(previous.spec[k])) &&
          relation.value === previous.relationship) return data;
      data = await action("edit",{ident:data.id,revision:data.revision,role,spec,relationship:relation.value});
      return data;
    }
    button(box,"暂存草稿",async()=>refreshed(await save()));
    if (plan.archived?.length) {
      const list = el("details",null,box); el("summary","查看旧计划草稿",list);
      for (const old of plan.archived) button(list,old.created || old.id.slice(0,12),async()=>{
        const current = await save();
        const entry = await action("plan_archive",{ident:current.id,plan_id:old.id});
        archiveView({...entry,id:current.id},role);
      });
    }
    button(box,"重新核对归属",async()=>mapping(await save()));
    button(box,"撤回该文件的提炼与风格使用",async()=>{
      if (confirm("停止该来源的提炼与风格使用？已还原的历史继续保存在会话中。"))
        await refreshed(await action("revoke",{ident:data.id}));
    });
  }

  async function reload(prefer=null,closeDialog=true,viewSession=null) {
    const result=await api().live_people();
    state.connected=result.connected;state.model=result.model;
    state.people=result.people.filter(p=>!p.deleted).map(p=>({id:p.id,profile:{name:p.name},sessions:[]}));
    const chosen=state.people.find(p=>p.id===prefer) || state.people.find(p=>result.people.find(q=>q.id===p.id)?.selected) || state.people[0];
    if(chosen)await select(chosen,false,closeDialog,viewSession);
    else {state.current=null;state.guide=true;render();}
  }
  async function select(person,fresh=false,closeDialog=true,viewSession=null){
    const id=++epoch;
    const result=viewSession ? await api().live_select(person.id,false,viewSession) : await api().live_select(person.id,fresh);
    if(id!==epoch)return;
    person.sessions=result.sessions.map(s=>({id:s.id,label:s.created_at.slice(0,16).replace("T"," "),
      draft:"",messages:[],past:[],version:null,historyReady:false,turns:[],readOnly:s.id!==result.active}));
    person.profile=result.person.spec; person.activeSession=viewSession || result.active;state.current=person.id;
    state.guide=false;closeDrawer();if(closeDialog && dialog.open)dialog.close();render();
    await refresh();
  }
  async function refresh(){
    if(polling||!active)return;
    const p=currentPerson(),s=currentSession(),id=epoch;
    if(!p||!s)return;
    polling=true;
    try{
      if(!s.historyReady){
        const revision=s.historyRevision;
        const h=await action("history",{persona_id:p.id,session_id:s.id});
        if(id!==epoch||s!==currentSession()||revision!==s.historyRevision)return;
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
  const memoryStateNames={off:"未开启",waiting_configuration:"等待角色记忆或模型配置",pending:"待整理",
    processing:"正在整理",waiting_quota:"等待后台额度",processed:"已处理",quarantined:"冲突待核",
    unknown:"结果待核对",blocked_input:"个别原话超过整理容量",withdrawn:"已撤回"};
  async function historySettings(s){
    const pid=currentPerson()?.id;
    const info=await action("history_status",{persona_id:pid,session_id:s.id});
    if(s!==currentSession())return;
    const box=panel("历史来源与用途");
    if(!info){el("p","本会话没有导入记录。",box);return;}
    el("p","续聊用途 · "+({active:"已确认",legacy:"待核对本人和原件",provider_changed:"当前模型待确认",
      withdrawn:"已撤回"}[info.usage_state] || "待确认"),box);
    el("p","长期记忆 · "+(memoryStateNames[info.memory_state] || "待核对"),box);
    showRecipients(box,info.current_providers);
    if(info.usage_state==="legacy") {
      button(box,"选择原件并核对本人",async()=>{
        const file=await api().pick_transcript();
        if(!file)return;
        if(file.id!==info.document) throw Error("请选这段历史原先使用的聊天文件。");
        setupHistory(file,{personaId:pid,sessionId:s.id,targetName:currentPerson().profile.name});
      });
      return;
    }
    if(info.withdrawn)return;
    async function change(actionName) {
      await action("history_update",{persona_id:pid,session_id:s.id,expected_revision:info.revision,
        action:actionName,confirmed:true,expected_providers:info.current_providers});
      if(actionName==="withdraw") {
        for(const row of s.past) row.referenceable=false;
        s.reference=null;
      }
      s.historyRevision=(s.historyRevision||0)+1;
      s.historyReady=false;s.version=null;render();
      await historySettings(s);await refresh();
    }
    function providerChanged(key){
      const old=info.providers?.[key],current=info.current_providers?.[key];
      return old?.name!==current?.name || old?.base_url!==current?.base_url;
    }
    if(info.usage_state==="provider_changed" || providerChanged("jev"))button(box,"确认当前模型用于续聊",()=>change("authorize"));
    if(info.organize && providerChanged("memory")) button(box,"确认当前记忆整理模型",()=>change("organize"));
    if(info.organize)button(box,"暂停本次整理",()=>change("pause"));
    else button(box,"允许当前模型整理本次历史",()=>change("organize"));
    button(box,"撤回历史使用",async()=>{
      const confirmBox=panel("撤回历史使用");
      el("p","撤回后仍可在本机翻看。续聊、召回和派生记忆会停止使用这段来源。",confirmBox);
      button(confirmBox,"确认撤回历史使用",()=>change("withdraw"));
      button(confirmBox,"返回历史用途",()=>historySettings(s));
    });
  }
  function historyDetails(s,event,back=null){
    const box=panel("历史消息");
    const source=event.source || {};
    el("p",[source.speaker,source.number && "#"+source.number,
      event.created_at || "时间待核",source.line && "文件第 "+source.line+" 行"].filter(Boolean).join(" · "),box);
    const body=el("p",event.text,box); body.className="import-original";
    if(source.sections?.length>1 || source.sections?.length===1 && source.sections[0].kind!=="body"){
      const kinds={body:"正文",quote:"引用",forward:"转发",attachment:"附件",code:"代码",system:"系统"};
      const chars=Array.from(event.text);
      body.replaceChildren();
      for(const part of source.sections) {
        el("small",kinds[part.kind] || part.kind,body).className="import-section-label";
        body.append(document.createTextNode(chars.slice(part.start-source.start,part.end-source.start).join("")));
      }
    }
    if(!s.readOnly && event.role!=="note" && event.referenceable)button(box,"引用这条消息",()=>{
      if(s!==currentSession())throw Error("请回到该消息所在的会话。");
      s.reference={id:event.id,text:event.text};dialog.close();render();message.focus();
    });
    if(back)button(box,"返回查找结果",back);
  }
  function historySearch(s,query="",rows=[],before=null,loaded=false){
    const box=panel("查找历史消息");
    const input=inputField(box,"消息正文","search",query);
    async function lookup(next=false) {
      const value=input.value.trim();if(!value)throw Error("请输入要查找的文字。");
      const cursor=next && value===query?before:null;
      const result=await action("history",{persona_id:currentPerson().id,session_id:s.id,query:value,before:cursor,limit:80});
      if(s===currentSession() && box.isConnected) historySearch(s,value,result.rows,result.before,true);
    }
    button(box,"查找",()=>lookup());
    input.onkeydown=e=>{if(e.key==="Enter" && !e.isComposing){e.preventDefault();run(lookup)();}};
    if(loaded){
      if(!rows.length)el("p",before?"当前范围没有匹配，可继续向前查找。":"此范围没有匹配消息。",box);
      for(const row of rows){
        const label=(row.source?.speaker || (row.role==="user"?"本人":"对方"))+" · "+row.text.slice(0,100);
        button(box,label,()=>historyDetails(s,row,()=>historySearch(s,query,rows,before,true)));
      }
      if(before)button(box,"继续向前查找",()=>lookup(true));
    }
    input.focus();
  }
  function draw(s){
    timeline.classList.remove("first");timeline.replaceChildren();
    const thread=el("div",null,timeline);thread.className="conversation-inner";
    const reference=byId("history-reference");
    reference.hidden=!s.reference || s.readOnly;
    reference.querySelector("span").textContent=s.reference?"引用 · "+s.reference.text.slice(0,180):"";
    if(s.past.length){
      const actions=el("div",null,thread);actions.className="inline-actions history-actions";
      button(actions,"查找历史消息",()=>historySearch(s));
      button(actions,"历史来源与用途",()=>historySettings(s));
    }
    if(s.before)button(thread,"查看更早的消息",async()=>{
      const result=await action("history",{persona_id:currentPerson().id,session_id:s.id,before:s.before});
      if(s!==currentSession())return;
      const height=timeline.scrollHeight,top=timeline.scrollTop;
      s.past.unshift(...result.rows);s.before=result.before;render();
      timeline.scrollTop=top+timeline.scrollHeight-height;
    });
    function bubble(role,text){
      const p=el("p",text,thread);p.className="bubble "+(role==="user"?"own":"reply");return p;
    }
    let day=null;
    for(const event of s.past){
      const nextDay=event.created_at?.slice(0,10)||"日期待核";
      if(nextDay!==day){el("div",nextDay+" · 导入历史",thread).className="date-divider";day=nextDay;}
      const node=button(thread,event.text,()=>historyDetails(s,event));
      node.className=event.role==="note"?"button ghost history-actions":"bubble "+(event.role==="user"?"own":"reply");
      node.title="查看来源与引用 · "+(event.created_at||"时间待核");
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
    if(!p||!s||s.readOnly||!value.trim()||s.sending)return;
    if(!state.connected){connection();return;}
    s.sending=true;
    const ref=s.reference?.id || null;
    const request=s.pending?.value===value && s.pending.ref===ref?s.pending.request:newId();
    s.pending={value,ref,request};
    try{
      if(ref) await api().live_submit(p.id,s.id,value,request,{kind:"event",id:ref});
      else await api().live_submit(p.id,s.id,value,request);
      s.pending=null;if(s.draft===value)s.draft="";
      if(s.reference?.id===ref)s.reference=null;s.version=null;
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
  async function editPublishedScenes(scene) {
    const data=await action("status",{ident:scene.document});
    const job=await action("card_status",{ident:data.id});
    if(data.status==="revoked" || !job.cards?.[scene.role] || job.plan_id!==scene.plan_id ||
       job.source_revision!==scene.source_revision || data.revision!==scene.source_revision)
      throw Error("当前来源已变化，请重新核对角色资料。");
    const card=job.cards[scene.role],box=panel("编辑来源场景 · "+card.spec.name);
    const read=sceneMaterial(box,card);
    async function write(material){
      return action("card_edit",{ident:data.id,revision:data.revision,card_revision:job.revision,
        plan_id:data.plan.id,role:scene.role,card:material});
    }
    async function saveProposal(issues,fill){
      const proposal=fill({...card,...read()},issues);
      if(proposal){await write(proposal); await editPublishedScenes(scene);}
    }
    pendingProposals(box,card,issue=>saveProposal(issue,fillProposal),true,
                     items=>saveProposal(items,fillTextProposals));
    button(box,"暂存并返回人物资料",async()=>{await write({...card,...read()});await profile();});
  }
  async function profile(){
    const person=currentPerson(),pid=person.id,box=panel(person.profile.name);
    for(const [field,label] of Object.entries(titles))el("p",label+" · "+[].concat(person.profile[field]||[]).join("、"),box);
    const memory=await api().live_memory(pid);
    button(box,memory.enabled?"暂停自动记忆":"开启自动记忆并整理历史",async()=>{
      const recipient=memory.provider || {};
      if(!memory.enabled && !confirm("记忆整理模型 · "+(recipient.name || "待配置")+" · "+(recipient.base_url || "待配置")
        +"\n允许该角色使用上述模型整理获准的历史和新对话？后台提取与复核可能产生费用。")) return;
      await api().live_memory(pid,!memory.enabled,memory.version,memory.provider);await profile();
    });
    const scene=await action("card_scene_status",{persona_id:pid});
    if(!box.isConnected) return;
    if(scene){
      const pane=groupBox(box,"已保存的角色卡与场景");
      el("p", scene.enabled ? "场景发言已启用" : scene.reason || "当前使用普通发言",pane);
      if(scene.main && scene.jev_model) el("small","主回复 · "+scene.main+"　场景判断 · "+scene.jev_model,pane);
      if(scene.revoked && scene.affected_fields?.length)
        el("p","来源已撤回，需复核字段 · "+scene.affected_fields.map(pathLabel).join("、"),pane);
      if (scene.common) {el("strong","共通提示",pane); el("p",scene.common,pane).className="import-original";}
      for(const [key,title] of Object.entries(sceneLabels)){
        const slot=groupBox(pane,title);
        el("p",scene.common && scene.scenes[key] ? scene.common+"\n\n"+scene.scenes[key] : "待补",slot).className="import-original";
        if(scene.jev.criteria[key]){
          el("strong","Jev 判别依据",slot);el("p",scene.jev.criteria[key],slot).className="import-original";
        }
      }
      if(scene.jev.instruction) {
        const selection=groupBox(pane,"Jev 总决策说明");
        el("p",scene.jev.instruction,selection).className="import-original";
      }
      if(scene.updated) el("p","来源草稿中的场景已修改，请核对当前版本。",pane);
      if(scene.can_edit_source) button(pane,"编辑来源场景",()=>editPublishedScenes(scene));
      if(scene.enabled || scene.available)button(pane,scene.enabled?"暂停场景发言":"确认并启用场景发言",async()=>{
        await action("card_scene_enable",{persona_id:pid,card_revision:scene.revision,version:scene.version,
          enabled:!scene.enabled,confirmed:true});
        await profile();
      });
    }
    const binding=await action("style",{persona_id:pid});
    if(!box.isConnected||!binding)return;
    el("p",binding.revoked?"风格来源已撤回":binding.enabled?"风格取样已开启":"风格取样已暂停",box);
    const update=async(extra)=>{await action("update",{persona_id:pid,expected_revision:binding.revision,...extra});await profile();};
    if(binding.affected_fields?.length)el("p","需复核来源的字段 · "+binding.affected_fields.map(k=>titles[k]||k).join("、"),box);
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
  function connection(afterSave=null){
    const box=panel("模型连接"),model=el("select",null,box);
    for(const name of ["deepseek-v4-pro","deepseek-flash"])model.add(new Option(name,name));
    model.value=state.model;const key=el("input",null,box);key.type="password";key.placeholder="官方 DeepSeek API Key";
    button(box,"保存连接",async()=>{
      await api().live_model(model.value,key.value);key.value="";
      if(typeof afterSave==="function") {
        const latest=await api().live_people(); state.connected=latest.connected; state.model=latest.model; await afterSave();
      } else {dialog.close();await reload(state.current);}
    });
  }
  async function connect(){
    if(active)return;
    active=true;byId("sample-mode").hidden=true;
    document.querySelector('[data-menu="reset"]')?.remove();
    let retries=0;
    while(true){
      try{await reload(null,false);break;}catch(e){if(++retries>=30){notify(String(e.message||e));break;}await new Promise(r=>setTimeout(r,1000));}
    }
    setInterval(()=>refresh().catch(e=>{if(pendingReceipt)notify(String(e.message||e));}),750);
  }
  window.addEventListener("pywebviewready",run(connect),{once:true});
  if(window.pywebview?.api)run(connect)();
  return {get active(){return active;},home:run(home),select:run(select),draw,send:run(send),profile:run(profile),importHistory:run(importHistory),connection};
})();
