"""Native context policy and metadata-only context inspection."""
import json
import tkinter as tk
from tkinter import ttk,scrolledtext,messagebox
from .models import identifier


class ContextUI:
    def init_context_vars(self):
        self.ctx_channel=tk.StringVar(value='desktop');self.ctx_task=tk.StringVar(value='dialogue')
        self.ctx_turn=tk.StringVar();self.ctx_organization=tk.StringVar()
        cfg=self.config.load();self.ctx_bot=tk.StringVar(value=cfg['bot_qq']);self.ctx_peer=tk.StringVar(value=cfg['owner_qq'])
        self.ctx_enabled=tk.BooleanVar(value=True);self.ctx_cross=tk.BooleanVar(value=False)
        self.ctx_character=tk.StringVar(value='词法')
        self.ctx_policy=None;self.ctx_epoch=0;self.ctx_intents={};self.ctx_draft_epoch=0
        self.ctx_records=[];self.ctx_read_serial=0;self.ctx_record=tk.StringVar()
        for var in (self.ctx_enabled,self.ctx_cross,self.ctx_character):
            var.trace_add('write',lambda *_:setattr(self,'ctx_draft_epoch',self.ctx_draft_epoch+1))

    def build_context_page(self,parent):
        self.heading(parent,'认知上下文与工作集','预览读取请求受理时政策；未保存的开关只影响保存后新输入。未就绪的任务视图不触发模型。')
        line=ttk.Frame(parent);line.pack(fill='x',pady=3)
        ttk.Label(line,text='入口').pack(side='left')
        ttk.Combobox(line,textvariable=self.ctx_channel,values=('desktop','qq'),state='readonly',width=12).pack(side='left',padx=5)
        for label,var in (('Bot',self.ctx_bot),('联系人',self.ctx_peer)):
            ttk.Label(line,text=label).pack(side='left');ttk.Entry(line,textvariable=var,width=18).pack(side='left',padx=4)
        line=ttk.Frame(parent);line.pack(fill='x',pady=4)
        ttk.Checkbutton(line,text='自动工作集召回',variable=self.ctx_enabled).pack(side='left',padx=5)
        ttk.Checkbutton(line,text='允许同入口跨会话',variable=self.ctx_cross).pack(side='left',padx=5)
        ttk.Label(line,text='角色检索').pack(side='left')
        ttk.Combobox(line,textvariable=self.ctx_character,values=('词法','本地语义（旧方案）','本地召回＋云端选择'),state='readonly',width=23).pack(side='left')
        self.button(line,'读取入口政策',self.refresh_context_policy,{'context-read'})
        self.button(line,'保存开关',self.save_context_policy,{'context-write'})
        line=ttk.Frame(parent);line.pack(fill='x',pady=4)
        ttk.Label(line,text='任务类型').pack(side='left')
        ttk.Combobox(line,textvariable=self.ctx_task,values=('dialogue','reflection','affect','director','planning'),state='readonly',width=15).pack(side='left',padx=5)
        self.button(line,'载入最近请求',self.load_context_requests,{'context-read'})
        line=ttk.Frame(parent);line.pack(fill='x',pady=4)
        ttk.Label(line,text='聊天请求ID').pack(side='left')
        self.ctx_turns=ttk.Combobox(line,textvariable=self.ctx_turn,width=45);self.ctx_turns.pack(side='left',fill='x',expand=True,padx=5)
        line=ttk.Frame(parent);line.pack(fill='x',pady=4)
        ttk.Label(line,text='整理任务ID').pack(side='left');ttk.Entry(line,textvariable=self.ctx_organization).pack(side='left',fill='x',expand=True,padx=5)
        bar=self.bar(parent);bar.pack(fill='x',pady=4)
        self.button(bar,'预览任务上下文',lambda:self.context_request('preview'),{'context-read'})
        self.button(bar,'查看调用清单历史',lambda:self.context_request('inspect'),{'context-read'})
        tabs=ttk.Notebook(parent);tabs.pack(fill='both',expand=True,pady=6)
        summary=ttk.Frame(tabs);raw=ttk.Frame(tabs)
        tabs.add(summary,text='装配摘要（只读）');tabs.add(raw,text='原始 JSON')
        self.ctx_record_picker=ttk.Combobox(summary,textvariable=self.ctx_record,state='disabled')
        self.ctx_record_picker.pack(fill='x',pady=3)
        self.ctx_record_picker.bind('<<ComboboxSelected>>',lambda _:self.show_context_record())
        self.ctx_summary=scrolledtext.ScrolledText(summary,height=8,wrap='word',state='disabled')
        self.ctx_summary.pack(fill='x',pady=3)
        table=ttk.Frame(summary);table.pack(fill='both',expand=True)
        columns=('order','block','owner','version','characters','hash')
        self.ctx_blocks=ttk.Treeview(table,columns=columns,show='headings',height=7)
        for key,label,width in zip(columns,('顺序','提示块','责任模块','渲染版本','字符数','内容指纹'),(50,150,170,200,65,450)):
            self.ctx_blocks.heading(key,text=label);self.ctx_blocks.column(key,width=width,minwidth=40,stretch=False)
        vertical=ttk.Scrollbar(table,orient='vertical',command=self.ctx_blocks.yview)
        horizontal=ttk.Scrollbar(table,orient='horizontal',command=self.ctx_blocks.xview)
        self.ctx_blocks.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.ctx_blocks.grid(row=0,column=0,sticky='nsew');vertical.grid(row=0,column=1,sticky='ns');horizontal.grid(row=1,column=0,sticky='ew')
        table.rowconfigure(0,weight=1);table.columnconfigure(0,weight=1)
        self.ctx_output=scrolledtext.ScrolledText(raw,height=23,wrap='word',state='disabled');self.ctx_output.pack(fill='both',expand=True)
        self.clear_context_summary()
        for var in (self.ctx_channel,self.ctx_task,self.ctx_turn,self.ctx_organization,self.ctx_bot,self.ctx_peer):
            var.trace_add('write',lambda *_:self.invalidate_context())

    def invalidate_context(self):
        self.ctx_epoch+=1;self.ctx_policy=None
        if hasattr(self,'ctx_output'):self.set_text(self.ctx_output,'范围或目标已变化，请重新读取。查询及聊天草稿保留。')
        if hasattr(self,'ctx_summary'):self.clear_context_summary()

    def clear_context_summary(self):
        self.ctx_records=[];self.ctx_record.set('')
        self.ctx_record_picker.configure(values=(),state='disabled')
        self.ctx_blocks.delete(*self.ctx_blocks.get_children())
        self.set_text(self.ctx_summary,'请选择请求，再读取预览或调用清单历史。')

    def show_context_result(self,action,value):
        self.clear_context_summary();self.ctx_result_action=action
        self.ctx_records=value.get('manifests',[]) if action=='inspect' else [value.get('manifest',{})]
        labels=[str(i+1)+' · '+r.get('context_id','未保存预览') for i,r in enumerate(self.ctx_records)]
        self.ctx_record_picker.configure(values=labels,state='readonly' if action=='inspect' and labels else 'disabled')
        if labels:self.ctx_record_picker.current(0)
        self.show_context_record()

    def show_context_record(self):
        self.ctx_blocks.delete(*self.ctx_blocks.get_children())
        history=self.ctx_result_action=='inspect'
        title='历史：仅展示该次保存的 Manifest。' if history else '预览：按当前可用来源重建，未发起模型调用。'
        if not self.ctx_records:
            self.set_text(self.ctx_summary,title+'\n未找到已保存的调用清单。');return
        index=max(0,self.ctx_record_picker.current());record=self.ctx_records[index]
        info=record.get('prompt_assembly')
        if not info:
            self.set_text(self.ctx_summary,title+'\n'+('该历史记录未采集分块信息' if history else '此预览未提供分块信息（任务未接入或诊断因大小上限被省略）。'))
            return
        requested=info.get('requested',{});effective=info.get('effective',{});capsule=effective.get('capsule',{})
        capsule_text=('已应用 '+str(capsule.get('renderer')) if capsule.get('applied') else '未应用；原因：'+str(capsule.get('reason')))
        text=(title+'\n装配器：'+info.get('assembler_version','未知')+
              '\n表达：请求 '+str(requested.get('dialogue_voice') or '旧版')+'；实际 '+str(effective.get('expression'))+
              '\n格式：提示词 '+str(effective.get('format'))+'；输出合同表达版本 '+str((effective.get('wire') or {}).get('expression_version'))+
              '\n胶囊：请求 '+str(capsule.get('requested_mode'))+'；'+capsule_text+
              '\n最终输入：'+str(info.get('input_characters'))+' 字符；指纹 '+str(info.get('input_snapshot_hash')))
        if info.get('details_omitted'):text+='\n明细因诊断大小上限被省略，仅保留汇总。'
        else:
            text+='\n裁减记录：'+str(len(info.get('truncation_info',[])))+'；来源引用与裁减原因见原始 JSON。'
        selection=info.get('material_selection')
        if selection:
            text+='\n材料选择：请求 '+str(selection.get('requested_prompt') or '无云端选择')+'；实际 '+str(selection.get('effective_mode') or '未执行')
            text+='；生效版本 '+str(selection.get('applied_prompt') or '未应用云端结果')
            text+='\n表达样例：选择器返回 '+str(selection.get('returned_example_count') if selection.get('returned_example_count') is not None else '未采集')+'；实际入模 '+str(selection.get('included_example_count',0))
            if selection.get('fallback_reason'):text+='；回退原因 '+selection['fallback_reason']
            text+='；来源 ID 与选择调用见原始 JSON。'
        else:text+='\n材料选择版本及返回明细未采集。'
        self.set_text(self.ctx_summary,text)
        names={'persona_expression':'人格与表达','relationship':'关系','temporary_state':'临时状态','behavior':'弱行为提示',
               'presentation':'展示合同','character':'角色资料','memory_summary':'记忆与摘要','explicit_references':'明确引用',
               'reply_protocol':'回复协议','capsule':'胶囊','history':'历史消息','current_input':'当前输入'}
        for i,row in enumerate(info.get('blocks',[]),1):
            label=names.get(row['name'],row['name'])+('（未进入）' if not row.get('present') else '')
            self.ctx_blocks.insert('','end',values=(i,label,row['owner'],row['version'],row['characters'],row['content_hash']))

    def context_key(self):
        return (self.persona_id,self.view_epoch,self.ctx_epoch,self.ctx_channel.get(),self.ctx_bot.get(),self.ctx_peer.get(),self.ctx_task.get(),self.ctx_turn.get(),self.ctx_organization.get())

    def context_scope(self):
        value={'persona_id':self.persona_id,'channel':self.ctx_channel.get()}
        if value['channel']=='qq':value|={'bot':self.ctx_bot.get(),'peer':self.ctx_peer.get()}
        return value

    def refresh_context_policy(self):
        if not self.persona_id:return
        key=self.context_key();pid=self.persona_id;channel=self.ctx_channel.get();draft=self.ctx_draft_epoch
        def done(value):
            if key!=self.context_key():return
            if draft!=self.ctx_draft_epoch:return
            self.ctx_policy=value;self.ctx_enabled.set(value.get('context_mode')=='working_set');self.ctx_cross.set(value.get('cross_session',False))
            self.clear_context_summary()
            self.ctx_character.set({'lexical':'词法','hybrid':'本地语义（旧方案）','hybrid_llm':'本地召回＋云端选择'}.get(value.get('character_retrieval',{}).get('mode'),'词法'))
            self.set_text(self.ctx_output,json.dumps(value,ensure_ascii=False,indent=2))
        self.run_task('读取上下文政策',{'context-read'},lambda _:self.client.manage('interaction.current',persona_id=pid,channel=channel),done,error_current=lambda:key==self.context_key())

    def save_context_policy(self):
        p=self.ctx_policy
        if not p or p['persona_id']!=self.persona_id or p['channel']!=self.ctx_channel.get():
            self.notice.set('请先读取当前入口政策，再修改并保存开关。');return
        key=self.context_key();pending_action=None
        batches=[b for b in self.batch_rows if b['channel']==p['channel'] and b['persona_id']==self.persona_id and b['state']=='collecting']
        if batches:
            answer=messagebox.askyesnocancel('处理收集中输入','先封存并回复现有输入，再修改上下文政策？\n选择“否”取消现有批次；“取消”保留原政策。')
            if answer is None:return
            pending_action='seal' if answer else 'cancel'
        mode='working_set' if self.ctx_enabled.get() else 'legacy_recent';cross=self.ctx_cross.get()
        character_mode={'词法':'lexical','语义增强':'hybrid','本地语义（旧方案）':'hybrid','本地召回＋云端选择':'hybrid_llm'}[self.ctx_character.get()]
        intent=(p['persona_id'],p['channel'],p['id'],mode,cross,pending_action,character_mode)
        args={'persona_id':p['persona_id'],'channel':p['channel'],'mode':p['mode'],'expected_version':p['id'],
              'operation_id':self.ctx_intents.setdefault(intent,identifier()),'context_mode':mode,'cross_session':cross,'pending_action':pending_action,'character_retrieval_mode':character_mode}
        def done(value):
            if key!=self.context_key():return
            self.ctx_policy=value;self.clear_context_summary();self.set_text(self.ctx_output,json.dumps(value,ensure_ascii=False,indent=2));self.notice.set('上下文政策已保存，只影响后续受理输入。')
        self.run_task('保存上下文政策',{'context-write'},lambda _:self.client.manage('interaction.update',**args),done,error_current=lambda:key==self.context_key())

    def load_context_requests(self):
        if not self.persona_id:return
        key=self.context_key();selected=self.context_scope()
        def done(rows):
            if key!=self.context_key():return
            ids=[r['id'] for r in rows if r['persona_id']==selected['persona_id'] and r['channel']==selected['channel']
                 and (r['channel']=='desktop' or r['origin']=={'bot':selected['bot'],'sender':selected['peer']})]
            self.ctx_turns.configure(values=ids)
            if ids and not self.ctx_turn.get():self.ctx_turn.set(ids[0])
        self.run_task('读取上下文请求',{'context-read'},lambda _:self.client.manage('chat.requests'),done,error_current=lambda:key==self.context_key())

    def context_request(self,action):
        if not self.persona_id:return
        args=self.context_scope();kind=self.ctx_task.get()
        if action=='preview':args['task_type']=kind
        if kind=='dialogue':args['turn_id']=self.ctx_turn.get()
        elif kind=='reflection':args['task_id']=self.ctx_organization.get()
        elif action=='inspect':self.notice.set('该任务尚无实际调用，只有只读合同预览。');return
        if (kind=='dialogue' and not args['turn_id']) or (kind=='reflection' and not args['task_id']):
            self.notice.set('请先选择聊天请求或填写整理任务ID。');return
        key=self.context_key();self.ctx_read_serial+=1;serial=self.ctx_read_serial
        def done(value):
            if key!=self.context_key() or serial!=self.ctx_read_serial:return
            self.set_text(self.ctx_output,json.dumps(value,ensure_ascii=False,indent=2))
            self.show_context_result(action,value)
            self.notice.set('上下文：'+('缺少后续状态，尚未就绪。' if value.get('status')=='not_ready' else '已读取。'))
        self.run_task('读取任务上下文',{'context-read'},lambda _:self.client.manage('context.'+action,**args),done,error_current=lambda:key==self.context_key() and serial==self.ctx_read_serial)
