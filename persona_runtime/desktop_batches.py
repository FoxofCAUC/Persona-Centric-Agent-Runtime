"""Structured workbench controls for durable input and explicit policy changes."""
import tkinter as tk
from tkinter import ttk,messagebox,scrolledtext
from .models import identifier

BATCH_LABELS={'collecting':'收集中','sealed_waiting_capacity':'已封存，等待队列容量','interrupted_buffer':'中断，待继续或取消','cancelled':'已取消','queued':'排队中','running':'正在生成','completed':'生成完成','failed':'生成失败','interrupted':'请求中断'}
REPLY_LABELS={'legacy_full':'兼容全文','context_full':'情境全文','context_structured':'情境结构回复'}

class BatchUI:
    def init_batch_vars(self):
        self.batch_rows=[];self.batch_policies={};self.batch_intents={};self.chat_batches=[]
        self.batch_policy_text=tk.StringVar(value='启动服务后读取入口模式。')

    def build_batches(self,parent):
        self.heading(parent,'输入与缓冲','按人格及入口单独开启；默认逐条。五秒静默、十五秒上限；重启后须明确继续。')
        bar=ttk.Frame(parent);bar.pack(fill='x',pady=5)
        self.button(bar,'刷新',self.refresh_batches,{'batch-read'})
        self.button(bar,'切换窗口模式',lambda:self.change_batch_mode('desktop'),{'batch-write'})
        self.button(bar,'切换 QQ 模式',lambda:self.change_batch_mode('qq'),{'batch-write'})
        ttk.Label(parent,textvariable=self.batch_policy_text,wraplength=700).pack(anchor='w',pady=5)
        reply_bar=ttk.Frame(parent);reply_bar.pack(fill='x',pady=(0,5))
        self.button(reply_bar,'设置窗口回复',lambda:self.change_reply_mode('desktop'),{'batch-write'})
        self.button(reply_bar,'设置 QQ 回复',lambda:self.change_reply_mode('qq'),{'batch-write'})
        self.reply_policy_text=tk.StringVar(value='启动服务后读取回复模式。')
        ttk.Label(parent,textvariable=self.reply_policy_text,wraplength=700).pack(anchor='w',pady=(0,5))
        self.batch_tree=ttk.Treeview(parent,columns=('channel','status','members','remaining'),show='headings',height=10)
        for key,title,width in (('channel','入口',80),('status','状态',220),('members','输入条数',90),('remaining','剩余等待',110)):
            self.batch_tree.heading(key,text=title);self.batch_tree.column(key,width=width)
        self.batch_tree.pack(fill='both',expand=True)
        self.batch_tree.bind('<<TreeviewSelect>>',lambda _:self.show_batch_members())
        self.batch_details=scrolledtext.ScrolledText(parent,height=8,wrap='word',state='disabled');self.batch_details.pack(fill='x',pady=7)
        bar=ttk.Frame(parent);bar.pack(fill='x')
        for label,action in (('立即回复','seal'),('继续中断输入','resume'),('取消未生成批次','cancel')):
            self.button(bar,label,lambda a=action:self.act_batch(a),{'batch-write'})
        ttk.Label(parent,text='取消保留输入原文；已创建请求后使用原请求队列处置。生成完成不代表 QQ 已送达。',wraplength=700).pack(anchor='w',pady=6)

    def refresh_batches(self):
        pid=self.persona_id;epoch=self.view_epoch
        if not pid:return
        def action(_):
            return self.client.manage('batch.list',persona_id=pid),{channel:self.client.manage('interaction.current',persona_id=pid,channel=channel) for channel in ('desktop','qq')}
        def done(value):
            if epoch!=self.view_epoch or pid!=self.persona_id:return
            rows,policies=value;self.batch_policies=policies
            self.batch_policy_text.set('；'.join(('窗口' if c=='desktop' else 'QQ')+'：'+('五秒缓冲' if p['mode']=='natural_batch' else '逐条回复') for c,p in policies.items()))
            self.reply_policy_text.set('；'.join(('窗口' if c=='desktop' else 'QQ')+'：'+REPLY_LABELS.get(p.get('reply_mode','legacy_full'),'兼容全文') for c,p in policies.items()))
            selected=self.batch_tree.selection()
            self.batch_rows=rows
            self.batch_tree.delete(*self.batch_tree.get_children())
            for b in rows:
                self.batch_tree.insert('', 'end',iid=b['id'],values=('窗口' if b['channel']=='desktop' else 'QQ',BATCH_LABELS.get(b['status'],b['status']),len(b['members']),f"{b['remaining_ms']/1000:.1f} 秒" if b['state']=='collecting' else '—'))
            if selected and self.batch_tree.exists(selected[0]):self.batch_tree.selection_set(selected[0])
            self.chat_batches=[b for b in rows if b['session_id']==self.session_id and b['turn_id'] is None]
            if self.current_page=='chat':self.render_chat()
            self.show_batch_members()
        self.run_task('读取输入批次',{'batch-read'},action,done,quiet=True)

    def selected_batch(self):
        selected=self.batch_tree.selection()
        return next((b for b in self.batch_rows if selected and b['id']==selected[0]),None)

    def show_batch_members(self):
        b=self.selected_batch();self.batch_details.configure(state='normal');self.batch_details.delete('1.0','end')
        if b:
            self.batch_details.insert('end','批次：'+b['id']+'\n封存原因：'+(b['seal_reason'] or '尚未封存')+'\n')
            for i,m in enumerate(b['members'],1):self.batch_details.insert('end',f"输入 {i} · {m['event_id']}\n{m['text']}\n")
            if b['turn_id']:self.batch_details.insert('end','请求：'+b['turn_id']+'；请在聊天请求详情查看生成与投递记录。')
        self.batch_details.configure(state='disabled')

    def change_batch_mode(self,channel):
        p=self.batch_policies.get(channel);pid=self.persona_id;epoch=self.view_epoch
        if not p or p['persona_id']!=pid:return
        mode='natural_batch' if p['mode']=='legacy_per_message' else 'legacy_per_message'
        title='开启五秒缓冲' if mode=='natural_batch' else '恢复逐条回复'
        if not messagebox.askyesno(title,'将为当前人格的'+('窗口' if channel=='desktop' else 'QQ')+'入口'+title+'。只影响后续输入，是否保存？'):return
        pending=[b for b in self.batch_rows if b['channel']==channel and b['state']=='collecting']
        pending_action=None
        if pending:
            answer=messagebox.askyesnocancel('处理收集中输入','先封存并回复现有输入，再切换？\n选择“否”取消现有批次后切换；“取消”不修改。')
            if answer is None:return
            pending_action='seal' if answer else 'cancel'
        key=('policy',pid,channel,p['id'],mode,pending_action);op=self.batch_intents.setdefault(key,identifier())
        def done(_):
            if epoch==self.view_epoch and pid==self.persona_id:self.refresh_batches()
        self.run_task('保存入口模式',{'batch-write'},lambda _:self.client.manage('interaction.update',persona_id=pid,channel=channel,mode=mode,expected_version=p['id'],operation_id=op,pending_action=pending_action),done)

    def change_reply_mode(self,channel):
        p=self.batch_policies.get(channel);pid=self.persona_id;epoch=self.view_epoch
        if not p or p['persona_id']!=pid:return
        dialog,body,footer=self.dialog_shell('选择回复模式',(430,245))
        value=tk.StringVar(value=p.get('reply_mode','legacy_full'))
        ttk.Label(body,text=('窗口' if channel=='desktop' else 'QQ')+' 后续受理的请求使用：').pack(anchor='w',pady=(8,4))
        for mode,label,description in (
            ('legacy_full','兼容全文','保持现有完整文本回复行为。'),
            ('context_full','情境全文','使用本地情境线索和角色样例，仍返回完整文本。'),
            ('context_structured','情境结构回复','一次主调用返回经校验的 1–3 段；仍以完整文本投递。'),
        ):
            row=ttk.Frame(body);row.pack(fill='x',anchor='w',pady=3)
            ttk.Radiobutton(row,text=label,value=mode,variable=value).pack(side='left')
            ttk.Label(row,text=description,wraplength=280).pack(side='left',padx=8)
        def save():
            mode=value.get()
            if mode==p.get('reply_mode','legacy_full'):
                dialog.destroy();return
            op=identifier()
            def done(_):
                if epoch==self.view_epoch and pid==self.persona_id:self.refresh_batches()
                if dialog.winfo_exists():dialog.destroy()
            def failed(_):
                if dialog.winfo_exists():dialog.destroy()
            self.run_task('保存回复模式',{'batch-write'},lambda _:self.client.manage('reply.update',persona_id=pid,channel=channel,reply_mode=mode,expected_version=p['id'],operation_id=op),done,failure=failed)
        ttk.Button(footer,text='取消',command=dialog.destroy).grid(row=0,column=0,padx=5)
        ttk.Button(footer,text='保存',command=save,style='Primary.TButton').grid(row=0,column=1,padx=5)

    def act_batch(self,action):
        b=self.selected_batch();pid=self.persona_id;epoch=self.view_epoch
        if not b or b['persona_id']!=pid:return
        if b['turn_id']:
            self.notice.set('此批次已创建请求，请使用请求队列中的重试或排队取消。');return
        if action=='cancel' and not messagebox.askyesno('取消批次','保留原文并取消此次回复，不进入记忆整理？'):return
        if action=='resume' and not messagebox.askyesno('继续中断输入','将按原成员和原设定封存，使用当前模型创建一次请求；可能计费。继续？'):return
        key=(b['id'],action,b['revision']);op=self.batch_intents.setdefault(key,identifier())
        def done(_):
            if epoch==self.view_epoch and pid==self.persona_id:self.refresh_batches()
        self.run_task('处理输入批次',{'batch-write'},lambda _:self.client.call('POST',f"/v2/batches/{b['id']}/{action}",{'expected_revision':b['revision'],'operation_id':op,'persona_id':pid}),done)

    def poll_batches(self):
        if self.closed:return
        if self.persona_id and self.current_page in ('batches','chat'):self.refresh_batches()
        self.after(700,self.poll_batches)
