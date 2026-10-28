"""Native engineering session browser; browsing never changes the send target."""
import json
import time
from datetime import datetime, timezone, timedelta
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from .models import identifier


class SessionUI:
    def init_session_vars(self):
        self.ss_channel=tk.StringVar(value='desktop');self.ss_id=tk.StringVar()
        cfg=self.config.load();self.ss_bot=tk.StringVar(value=cfg['bot_qq']);self.ss_peer=tk.StringVar(value=cfg['owner_qq'])
        self.ss_topic=tk.StringVar();self.ss_thread=tk.StringVar();self.ss_title=tk.StringVar();self.ss_description=tk.StringVar()
        self.ss_revision=None;self.ss_preview=None;self.ss_topic_row=None;self.ss_thread_row=None
        self.ss_turn_ids=[];self.ss_intents={};self.ss_epoch=0
        self.ss_clock_row=None
        for value in (self.ss_channel,self.ss_bot,self.ss_peer,self.ss_id):value.trace_add('write',lambda *_:self.invalidate_sessions())

    def invalidate_sessions(self):
        self.ss_epoch+=1;self.ss_revision=None;self.ss_preview=None;self.ss_topic_row=None;self.ss_thread_row=None
        self.ss_clock_row=None
        self.ss_topic.set('');self.ss_thread.set('');self.ss_turn_ids=[]
        if hasattr(self,'ss_members'):self.ss_members.delete(0,'end')
        if hasattr(self,'ss_output'):self.set_text(self.ss_output,'范围或会话已变化，请重新读取。标题、说明及聊天草稿保留。')

    def session_key(self):
        return (self.persona_id,self.view_epoch,self.ss_epoch,self.ss_channel.get(),self.ss_bot.get(),self.ss_peer.get(),self.ss_id.get(),self.ss_topic.get(),self.ss_thread.get())

    def session_scope(self):
        result={'persona_id':self.persona_id,'channel':self.ss_channel.get()}
        if result['channel']=='qq':result|={'bot':self.ss_bot.get(),'peer':self.ss_peer.get()}
        return result

    def build_sessions_page(self,parent):
        self.heading(parent,'会话与主题','运行中空闲30分钟封存；封存不调用模型。M8自主判断尚未启用。历史浏览不会改变聊天发送目标。')
        form=ttk.Frame(parent);form.pack(fill='x')
        self.ss_channels=ttk.Combobox(form,textvariable=self.ss_channel,values=('desktop','qq'),state='readonly',width=12);self.ss_channels.pack(side='left')
        ttk.Label(form,text='Bot').pack(side='left');ttk.Entry(form,textvariable=self.ss_bot,width=16).pack(side='left')
        ttk.Label(form,text='联系人').pack(side='left');ttk.Entry(form,textvariable=self.ss_peer,width=16).pack(side='left')
        self.ss_choices=ttk.Combobox(parent,textvariable=self.ss_id,width=60);self.ss_choices.pack(fill='x',pady=5)
        self.ss_clock_label=ttk.Label(parent,text='请读取状态，核对空闲观察及阻塞。');self.ss_clock_label.pack(anchor='w')
        self.after(1000,self.update_session_clock)
        row=ttk.Frame(parent);row.pack(fill='x')
        for title,action in (('读取会话列表',self.refresh_sessions),('查看状态',lambda:self.session_action('show')),
                            ('封存会话',lambda:self.session_write('seal')),('新建接续会话',lambda:self.session_write('reopen')),
                            ('只读回放',lambda:self.session_action('replay')),('查看触发',lambda:self.session_action('triggers'))):
            self.button(row,title,action,{'session-management'})
        row=ttk.Frame(parent);row.pack(fill='x',pady=4)
        for title,action in (('整理预览',lambda:self.session_action('organization.preview')),('提交本批整理',self.submit_session_organization),
                            ('处理输入阻塞',lambda:self.show_page('batches')),('处理投递阻塞',lambda:self.show_page('delivery')),
                            ('放弃旧整轮发送',self.abandon_session_delivery)):
            self.button(row,title,action,{'session-management'})
        ttk.Label(parent,text='主题段：从回放结果选择连续完整回合；关联只供组织展示，不自动召回。').pack(anchor='w')
        self.ss_members=tk.Listbox(parent,selectmode='extended',height=4,exportselection=False);self.ss_members.pack(fill='x')
        form=ttk.Frame(parent);form.pack(fill='x',pady=4)
        ttk.Label(form,text='标题').pack(side='left');ttk.Entry(form,textvariable=self.ss_title,width=25).pack(side='left',fill='x',expand=True)
        ttk.Label(form,text='说明').pack(side='left');ttk.Entry(form,textvariable=self.ss_description,width=40).pack(side='left',fill='x',expand=True)
        self.ss_topics=ttk.Combobox(parent,textvariable=self.ss_topic);self.ss_topics.pack(fill='x')
        self.ss_threads=ttk.Combobox(parent,textvariable=self.ss_thread);self.ss_threads.pack(fill='x')
        for kind,label in (('topic','主题段'),('thread','Thread')):
            row=ttk.Frame(parent);row.pack(fill='x',pady=3)
            for title,action in (('列出'+label,lambda k=kind:self.topic_action(k,'list')),('新建'+label,lambda k=kind:self.topic_action(k,'create')),
                                ('查看'+label,lambda k=kind:self.topic_action(k,'show')),('保存'+label,lambda k=kind:self.topic_action(k,'update')),
                                ('归档'+label,lambda k=kind:self.topic_action(k,'update',archived=True))):self.button(row,title,action,{'session-management'})
        row=ttk.Frame(parent);row.pack(fill='x')
        self.button(row,'关联主题段',lambda:self.link_topic(True),{'session-management'});self.button(row,'解除关联',lambda:self.link_topic(False),{'session-management'})
        self.button(row,'查看所选回合',self.open_topic_source,{'session-management'})
        self.ss_output=scrolledtext.ScrolledText(parent,height=12,wrap='word');self.ss_output.pack(fill='both',expand=True,pady=5)

    def update_session_clock(self):
        if self.closed:return
        value=self.ss_clock_row
        if value and value['key']==self.session_key():
            row=value['row'];idle=row.get('idle_seconds') or 0
            if row['state']=='active':idle+=max(0,time.monotonic()-value['received'])
            self.ss_clock_label.configure(text=f"{row['state']} · 空闲约 {int(idle)} 秒 · 距30分钟约 {max(0,1800-int(idle))} 秒（估计，封存以服务复核为准）")
        else:self.ss_clock_label.configure(text='请读取状态，核对空闲观察及阻塞。')
        self.after(1000,self.update_session_clock)

    @staticmethod
    def session_display(value):
        if isinstance(value,list):return [SessionUI.session_display(x) for x in value]
        if not isinstance(value,dict):return value
        result={k:SessionUI.session_display(v) for k,v in value.items()}
        for k,v in value.items():
            if k.endswith('_at') and isinstance(v,str):
                try:result[k+'_shanghai']=datetime.fromisoformat(v).astimezone(timezone(timedelta(hours=8))).isoformat()
                except ValueError:pass
        return result

    def session_call(self,action,args,done=None,writing=False):
        if not self.persona_id:return
        key=self.session_key()
        if writing:
            intent=json.dumps([action,args],ensure_ascii=False,sort_keys=True)
            args=args|{'operation_id':self.ss_intents.setdefault(intent,identifier())}
        def completed(value):
            if key!=self.session_key():return
            self.set_text(self.ss_output,json.dumps(self.session_display(value),ensure_ascii=False,indent=2))
            if done:done(value)
        self.run_task('会话与主题操作',{'session-management'},lambda _:self.client.manage(action,**args),completed,
                      error_current=lambda:key==self.session_key())

    def refresh_sessions(self):
        def done(rows):
            self.ss_choices.configure(values=[r['session_id'] for r in rows])
            if rows and not self.ss_id.get():self.ss_id.set(rows[0]['session_id'])
        self.session_call('session.list',self.session_scope(),done)

    def session_action(self,action):
        if not self.ss_id.get():self.notice.set('请选择会话。');return
        def done(value):
            if action=='show':
                self.ss_revision=value['revision'];self.ss_clock_row={'row':value,'received':time.monotonic(),'key':self.session_key()}
            if action=='organization.preview':self.ss_preview=value;self.ss_revision=value['lifecycle_revision']
            if action=='replay':
                self.ss_members.delete(0,'end');self.ss_turn_ids=[]
                for g in value['groups']:
                    self.ss_turn_ids.append(g['turn_id']);self.ss_members.insert('end',g['turn_id']+(' · 可用' if g['usable'] else ' · 来源失效'))
        self.session_call('session.'+action,self.session_scope()|{'session_id':self.ss_id.get()},done)

    def session_write(self,action):
        if self.ss_revision is None:self.notice.set('请先查看当前会话状态，取得版本。');return
        if action=='seal' and not messagebox.askyesno('封存会话','封存后原会话只读，失败请求不能原地重试；可新建接续会话。封存不会调用模型。继续？',parent=self):return
        def done(value):
            if value.get('revision') is not None:self.ss_revision=value['revision']
            if action=='reopen' and value.get('state')=='active':
                sid=value['session_id'];self.ss_id.set(sid)
                self.clear_archive_reference()
                if self.ss_channel.get()=='desktop':self.load_chat(session_id=sid)
                self.notice.set('已创建接续会话。聊天草稿保留；请核对新会话后再发送。')
        self.session_call('session.'+action,self.session_scope()|{'session_id':self.ss_id.get(),'expected_revision':self.ss_revision},done,True)

    def submit_session_organization(self):
        if not self.ss_preview:self.notice.set('请先预览本批整理。');return
        if not messagebox.askyesno('手动整理','提交当前预览的一批整理，可能调用已配置整理模型并计费。后续批次不会自动提交。继续？',parent=self):return
        self.session_call('session.organization.submit',self.session_scope()|{'preview':self.ss_preview,'expected_revision':self.ss_revision},writing=True)

    def abandon_session_delivery(self):
        from tkinter.simpledialog import askstring
        if self.ss_revision is None:self.notice.set('请先查看状态。');return
        turn=askstring('放弃旧整轮发送','填写阻塞列表中的旧turn ID；不能处置发送中或新片段。',parent=self)
        if not turn:return
        reason=askstring('审计原因','明确放弃后续发送的原因（不等于确认送达）：',parent=self)
        if reason:self.session_call('session.delivery.abandon',self.session_scope()|{'session_id':self.ss_id.get(),'turn_id':turn,'reason':reason,'expected_revision':self.ss_revision},writing=True)

    def topic_action(self,kind,action,archived=False):
        args=self.session_scope();var=self.ss_topic if kind=='topic' else self.ss_thread
        if action in ('show','update'):
            if not var.get():self.notice.set('请选择对象。');return
            args['object_id']=var.get()
        if action in ('create','update'):
            args|={'title':self.ss_title.get(),'description':self.ss_description.get()}
            if action=='create':
                args['expected_revision']=0
                if kind=='topic':args|={'session_id':self.ss_id.get(),'turn_ids':[self.ss_turn_ids[i] for i in self.ss_members.curselection()]}
            else:
                row=self.ss_topic_row if kind=='topic' else self.ss_thread_row
                if not row or row['id']!=var.get():self.notice.set('请先查看对象版本。');return
                args|={'expected_revision':row['revision'],'state':'archived' if archived else 'active'}
        chosen=var.get()
        def done(value):
            if action in ('show','update') and var.get()!=chosen:return
            if action=='list':(self.ss_topics if kind=='topic' else self.ss_threads).configure(values=[r['id'] for r in value])
            else:
                var.set(value['id'])
                if kind=='topic':
                    self.ss_topic_row=value
                    if action=='show':
                        self.ss_turn_ids=value['turn_ids'];self.ss_members.delete(0,'end')
                        for tid in self.ss_turn_ids:self.ss_members.insert('end',tid+(' · 来源失效' if tid in value['unavailable_turn_ids'] else ' · 可用'))
                else:
                    self.ss_thread_row=value
                    if action=='show':self.ss_topics.configure(values=value.get('segment_ids',[]))
        self.session_call(kind+'.'+action,args,done,action in ('create','update'))

    def open_topic_source(self):
        indexes=self.ss_members.curselection()
        if len(indexes)!=1:self.notice.set('请选择一个完整回合展开。');return
        self.clear_archive_reference()
        self.archive_channel.set(self.ss_channel.get());self.archive_bot.set(self.ss_bot.get());self.archive_peer.set(self.ss_peer.get())
        self.archive_audit.set(False);self.archive_kind.set('turn');self.archive_id.set(self.ss_turn_ids[indexes[0]])
        self.show_page('archive');self.archive_read('get')

    def link_topic(self,linked):
        t,s=self.ss_thread_row,self.ss_topic_row
        if not t or not s or t['id']!=self.ss_thread.get() or s['id']!=self.ss_topic.get():self.notice.set('请先分别查看Thread和主题段，核对两个版本。');return
        def done(value):
            t['revision']=value['thread_revision'];s['revision']=value['segment_revision']
        self.session_call('thread.link',self.session_scope()|{'thread_id':t['id'],'segment_id':s['id'],'expected_revision':t['revision'],'segment_revision':s['revision'],'linked':linked},done,True)
