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
        self.ctx_policy=None;self.ctx_epoch=0;self.ctx_intents={}

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
        self.ctx_output=scrolledtext.ScrolledText(parent,height=23,wrap='word',state='disabled');self.ctx_output.pack(fill='both',expand=True,pady=6)
        for var in (self.ctx_channel,self.ctx_task,self.ctx_turn,self.ctx_organization,self.ctx_bot,self.ctx_peer):
            var.trace_add('write',lambda *_:self.invalidate_context())

    def invalidate_context(self):
        self.ctx_epoch+=1;self.ctx_policy=None
        if hasattr(self,'ctx_output'):self.set_text(self.ctx_output,'范围或目标已变化，请重新读取。查询及聊天草稿保留。')

    def context_key(self):
        return (self.persona_id,self.view_epoch,self.ctx_epoch,self.ctx_channel.get(),self.ctx_bot.get(),self.ctx_peer.get(),self.ctx_task.get(),self.ctx_turn.get(),self.ctx_organization.get())

    def context_scope(self):
        value={'persona_id':self.persona_id,'channel':self.ctx_channel.get()}
        if value['channel']=='qq':value|={'bot':self.ctx_bot.get(),'peer':self.ctx_peer.get()}
        return value

    def refresh_context_policy(self):
        if not self.persona_id:return
        key=self.context_key();pid=self.persona_id;channel=self.ctx_channel.get()
        def done(value):
            if key!=self.context_key():return
            self.ctx_policy=value;self.ctx_enabled.set(value.get('context_mode')=='working_set');self.ctx_cross.set(value.get('cross_session',False))
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
        intent=(p['persona_id'],p['channel'],p['id'],mode,cross,pending_action)
        args={'persona_id':p['persona_id'],'channel':p['channel'],'mode':p['mode'],'expected_version':p['id'],
              'operation_id':self.ctx_intents.setdefault(intent,identifier()),'context_mode':mode,'cross_session':cross,'pending_action':pending_action}
        def done(value):
            if key!=self.context_key():return
            self.ctx_policy=value;self.set_text(self.ctx_output,json.dumps(value,ensure_ascii=False,indent=2));self.notice.set('上下文政策已保存，只影响后续受理输入。')
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
        key=self.context_key()
        def done(value):
            if key!=self.context_key():return
            self.set_text(self.ctx_output,json.dumps(value,ensure_ascii=False,indent=2))
            self.notice.set('上下文：'+('缺少后续状态，尚未就绪。' if value.get('status')=='not_ready' else '已读取。'))
        self.run_task('读取任务上下文',{'context-read'},lambda _:self.client.manage('context.'+action,**args),done,error_current=lambda:key==self.context_key())
