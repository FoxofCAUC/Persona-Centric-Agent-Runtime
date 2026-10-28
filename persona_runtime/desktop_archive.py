"""Explicit exact-ID archive workbench. No fuzzy search or implicit chat calls."""
import json
import tkinter as tk
from tkinter import ttk, scrolledtext


class ArchiveUI:
    @staticmethod
    def set_text(widget,text):
        widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',text);widget.configure(state='disabled')
    def init_archive_vars(self):
        self.archive_channel=tk.StringVar(value='desktop')
        self.archive_kind=tk.StringVar(value='turn');self.archive_id=tk.StringVar()
        self.archive_bot=tk.StringVar();self.archive_peer=tk.StringVar()
        self.archive_audit=tk.BooleanVar(value=False);self.archive_rows=[];self.archive_epoch=0
        self.chat_reference=None;self.reference_text=tk.StringVar(value='未选择引用')

    def build_archive(self,parent):
        self.heading(parent,'原始档案与精确引用','按明确ID读取；审计内容不能进入聊天引用。')
        bar=ttk.Frame(parent);bar.pack(fill='x')
        ttk.Combobox(bar,textvariable=self.archive_channel,values=('desktop','qq'),state='readonly',width=10).pack(side='left')
        for label,var in (('Bot',self.archive_bot),('联系人',self.archive_peer)):
            ttk.Label(bar,text=label).pack(side='left');ttk.Entry(bar,textvariable=var,width=16).pack(side='left',padx=4)
        cfg=self.config.load();self.archive_bot.set(cfg['bot_qq']);self.archive_peer.set(cfg['owner_qq'])
        ttk.Checkbutton(parent,text='管理员审计（不可直接用作聊天引用）',variable=self.archive_audit).pack(anchor='w',pady=5)
        bar=ttk.Frame(parent);bar.pack(fill='x')
        ttk.Combobox(bar,textvariable=self.archive_kind,values=('event','turn','part','platform_message'),state='readonly',width=18).pack(side='left')
        ttk.Entry(bar,textvariable=self.archive_id).pack(side='left',fill='x',expand=True,padx=5)
        for label,action in (('读取最近20条','list'),('精确读取','get'),('引用预览','preview')):
            self.button(bar,label,lambda a=action:self.archive_read(a),{'archive-read'})
        self.archive_tree=ttk.Treeview(parent,columns=('target','state','coverage'),show='headings',height=7)
        for key,title in (('target','来源ID'),('state','回合状态'),('coverage','档案覆盖')):
            self.archive_tree.heading(key,text=title)
        self.archive_tree.pack(fill='x',pady=5);self.archive_tree.bind('<<TreeviewSelect>>',lambda _:self.archive_pick())
        self.archive_output=scrolledtext.ScrolledText(parent,height=16,wrap='word',state='disabled');self.archive_output.pack(fill='both',expand=True)
        self.button(parent,'选择为窗口聊天引用',self.choose_archive_reference,{'archive-read'})
        self.archive_channel.trace_add('write',lambda *_:self.invalidate_archive())
        self.archive_bot.trace_add('write',lambda *_:self.invalidate_archive())
        self.archive_peer.trace_add('write',lambda *_:self.invalidate_archive())
        self.archive_audit.trace_add('write',lambda *_:self.invalidate_archive())
        self.archive_kind.trace_add('write',lambda *_:self.invalidate_archive_target())
        self.archive_id.trace_add('write',lambda *_:self.invalidate_archive_target())

    def invalidate_archive(self):
        self.clear_archive_reference();self.archive_rows=[]
        if hasattr(self,'archive_tree'):self.archive_tree.delete(*self.archive_tree.get_children())
        if hasattr(self,'archive_output'):self.set_text(self.archive_output,'范围已变化，请重新读取。')

    def invalidate_archive_target(self):
        # Keep the loaded list usable while editing/selecting its target, but
        # invalidate all pending reads and reference selections.
        self.clear_archive_reference()
        if hasattr(self,'archive_output'):self.set_text(self.archive_output,'目标已变化，请重新读取或预览。')

    def archive_scope(self):
        channel=self.archive_channel.get()
        return {'persona_id':self.persona_id,'channel':channel,'bot':self.archive_bot.get() if channel=='qq' else '', 'peer':self.archive_peer.get() if channel=='qq' else ''}

    def archive_read(self,action):
        args=self.archive_scope();pid=self.persona_id;epoch=self.view_epoch
        if not pid:
            self.notice.set('当前人格尚未读取，请先连接人格服务并读取人格列表。');return
        self.archive_epoch+=1;request=self.archive_epoch
        def current():
            return not self.closed and epoch==self.view_epoch and pid==self.persona_id and request==self.archive_epoch
        args['audit']=self.archive_audit.get()
        if action!='list':
            kind=self.archive_kind.get();args['target']={'kind':kind,('message_id' if kind=='platform_message' else 'id'):self.archive_id.get()}
        def done(result):
            if not current():return
            self.archive_tree.delete(*self.archive_tree.get_children());self.archive_rows=result.get('items',[]) if action=='list' else []
            for i,row in enumerate(self.archive_rows):self.archive_tree.insert('', 'end', iid=str(i),values=(row['target']['id'],row.get('turn_status','legacy'),row['coverage']))
            self.set_text(self.archive_output,json.dumps(result,ensure_ascii=False,indent=2))
            if action=='list':self.notice.set('档案列表已读取。')
            elif result.get('status')=='found':self.notice.set('引用预览已完成。' if action=='preview' else '档案已读取。')
            else:
                status=result.get('status','unknown')
                label={'not_found':'未找到','unavailable':'来源不可用','ambiguous':'目标不唯一','invalid_query':'查询格式不正确'}.get(status,status)
                self.notice.set('引用预览结果：'+label if action=='preview' else '档案读取结果：'+label)
        self.run_task('读取精确档案',{'archive-read'},lambda _:self.client.manage('archive.'+action,**args),done,error_current=current)

    def archive_pick(self):
        selected=self.archive_tree.selection()
        if not selected:return
        row=self.archive_rows[int(selected[0])];self.archive_kind.set(row['target']['kind']);self.archive_id.set(row['target']['id'])
        self.set_text(self.archive_output,json.dumps(row,ensure_ascii=False,indent=2))

    def clear_archive_reference(self):
        self.archive_epoch+=1
        self.chat_reference=None;self.reference_text.set('未选择引用')

    def choose_archive_reference(self):
        if self.archive_channel.get()!='desktop' or self.archive_audit.get() or not self.persona_id:
            self.notice.set('只能选择当前人格的窗口来源；审计和QQ资料不能跨入口送入窗口聊天。');return
        target={'kind':self.archive_kind.get(),'id':self.archive_id.get()}
        if target['kind']=='platform_message':self.notice.set('窗口引用请选择event、turn或part ID。');return
        pid=self.persona_id;epoch=self.view_epoch;scope=self.archive_scope();audit=self.archive_audit.get()
        self.archive_epoch+=1;request=self.archive_epoch
        def current():
            return (not self.closed and epoch==self.view_epoch and pid==self.persona_id
                    and request==self.archive_epoch and scope==self.archive_scope()
                    and audit==self.archive_audit.get()
                    and target=={'kind':self.archive_kind.get(),'id':self.archive_id.get()})
        def done(result):
            if not current():return
            if result['status']!='found':self.notice.set('引用不可用：'+result['status']);return
            self.chat_reference={'persona_id':pid,'target':dict(target)};self.reference_text.set('引用 '+target['kind']+'：'+target['id'])
            self.notice.set('已选择窗口聊天引用。')
            self.show_page('chat')
        self.run_task('选择引用',{'archive-read'},lambda _:self.client.manage('archive.preview',persona_id=pid,channel='desktop',target=target),done,error_current=current)
