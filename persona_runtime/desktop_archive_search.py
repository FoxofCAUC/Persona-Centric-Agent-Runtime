"""Engineering archive search; all work happens outside the Tk event loop."""
import json
import tkinter as tk
from tkinter import ttk, scrolledtext

from .models import identifier
from .desktop_client import TransportUnknown

LABELS={'ok':'已完成','no_matches':'所选范围无命中','invalid_query':'查询条件不正确',
        'unavailable':'来源或缓存不可用','coverage_gap':'所选时间轴缺少记录',
        'index_stale':'来源在查询中变化，请重新查询','query_budget_exceeded':'超出查询或完整组预算',
        'cache_busy':'缓存正在维护','cache_generation_changed':'缓存代次已变化，请重新读取',
        'operation_conflict':'操作标识冲突'}


class ArchiveSearchUI:
    def init_archive_search_vars(self):
        self.search_vars={key:tk.StringVar(value=value) for key,value in
            (('query',''),('match_mode','all_terms'),('time_axis','recorded'),('local_date',''),
             ('start',''),('end',''),('session_id',''),('window','all'))}
        self.search_epoch=0;self.search_rows=[];self.search_target=tk.StringVar()
        self.search_pending_rebuild=None;self.search_display_scope=None
        self.search_target.trace_add('write',lambda *_:self.search_target_changed())

    def search_target_changed(self):
        self.search_epoch+=1

    def build_archive_search(self,parent):
        self.heading(parent,'档案检索与只读工作集','查询和预览不调用模型；时间缺口、窗口及完整组限制会单独显示。')
        line=ttk.Frame(parent);line.pack(fill='x',pady=3)
        ttk.Label(line,text='入口').pack(side='left')
        ttk.Combobox(line,textvariable=self.archive_channel,values=('desktop','qq'),state='readonly',width=10).pack(side='left',padx=5)
        for label,var in (('Bot',self.archive_bot),('联系人',self.archive_peer)):
            ttk.Label(line,text=label).pack(side='left');ttk.Entry(line,textvariable=var,width=15).pack(side='left',padx=4)
        line=ttk.Frame(parent);line.pack(fill='x',pady=3)
        ttk.Label(line,text='关键词').pack(side='left')
        ttk.Entry(line,textvariable=self.search_vars['query']).pack(side='left',fill='x',expand=True,padx=5)
        for key,options in (('match_mode',('all_terms','phrase')),('window',('all','recent_50')),('time_axis',('recorded','received','platform','sent'))):
            ttk.Combobox(line,textvariable=self.search_vars[key],values=options,state='readonly',width=13).pack(side='left',padx=3)
        for fields in ((('上海日期','local_date'),('会话ID（可空）','session_id')),
                       (('起始ISO时间','start'),('结束ISO时间','end'))):
            line=ttk.Frame(parent);line.pack(fill='x',pady=3)
            for label,key in fields:
                ttk.Label(line,text=label).pack(side='left')
                ttk.Entry(line,textvariable=self.search_vars[key],width=25).pack(side='left',fill='x',expand=True,padx=5)
        bar=self.bar(parent);bar.pack(fill='x',pady=3)
        for title,action in (('查询','search'),('缓存状态','search-status'),('重建派生缓存','search-rebuild'),('工作集预览','working-set-preview'),('查看完整回合','get')):
            self.button(bar,title,lambda a=action:self.archive_search_request(a),{'archive-search'})
        self.search_tree=ttk.Treeview(parent,columns=('turn','hits','coverage'),show='headings',height=6,selectmode='extended')
        for key,label in (('turn','回合ID'),('hits','命中目标数'),('coverage','来源覆盖')):self.search_tree.heading(key,text=label)
        self.search_tree.pack(fill='x',pady=5);self.search_tree.bind('<<TreeviewSelect>>',lambda _:self.pick_search_target())
        line=ttk.Frame(parent);line.pack(fill='x')
        self.search_targets=ttk.Combobox(line,textvariable=self.search_target,state='readonly');self.search_targets.pack(side='left',fill='x',expand=True)
        self.button(line,'选择为窗口引用',self.choose_search_reference,{'archive-read'})
        self.search_output=scrolledtext.ScrolledText(parent,height=15,wrap='word',state='disabled');self.search_output.pack(fill='both',expand=True,pady=5)
        for var in list(self.search_vars.values())+[self.archive_channel,self.archive_bot,self.archive_peer]:
            var.trace_add('write',lambda *_:self.invalidate_search())

    def invalidate_search(self):
        self.search_epoch+=1;self.search_rows=[];self.search_display_scope=None;self.search_target.set('')
        if hasattr(self,'search_tree'):self.search_tree.delete(*self.search_tree.get_children())
        if hasattr(self,'search_targets'):self.search_targets.configure(values=())
        if hasattr(self,'search_output'):self.set_text(self.search_output,'条件或人格已变化，请重新查询。')

    def search_partition_args(self):
        args=self.archive_scope()|{'window':self.search_vars['window'].get()}
        if self.search_vars['session_id'].get():args['session_id']=self.search_vars['session_id'].get()
        return args

    def pick_search_target(self):
        if self.search_display_scope!=self.search_partition_args():return
        selected=self.search_tree.selection();targets=[]
        for key in selected:
            for target in self.search_rows[int(key)].get('matched_targets',[]):
                label=target['kind']+':'+target['id']
                if label not in targets:targets.append(label)
        self.search_targets.configure(values=targets)
        self.search_target.set(targets[0] if targets else '')

    def archive_search_request(self,action):
        if not self.persona_id:
            self.notice.set('请先连接人格服务并选择人格。');return
        args=self.search_partition_args();selected=self.search_tree.selection()
        if action=='search':
            for key in ('query','match_mode','time_axis'):args[key]=self.search_vars[key].get()
            for key in ('local_date','start','end'):
                if self.search_vars[key].get():args[key]=self.search_vars[key].get()
        elif action in ('get','working-set-preview'):
            if selected and self.search_display_scope!=self.search_partition_args():
                self.notice.set('结果已过期，请重新查询。');return
            if action=='get':
                if not selected:self.notice.set('请先选择一条结果。');return
                args=self.archive_scope()|{'target':{'kind':'turn','id':self.search_rows[int(selected[0])]['turn_id']}}
            else:
                args['targets']=[target for key in selected for target in self.search_rows[int(key)]['matched_targets']]
                if args['window']=='all' and not args['targets']:
                    self.notice.set('全范围预览请先选择结果；窗口清单请选择 recent_50。');return
        self.search_epoch+=1;generation=self.search_epoch;view=self.view_epoch;scope=self.search_partition_args()
        def current():
            return not self.closed and view==self.view_epoch and generation==self.search_epoch and scope==self.search_partition_args()
        def work(_):
            if action=='search-rebuild':
                pending=self.search_pending_rebuild
                if not pending or pending['scope']!=scope:
                    status=self.client.manage('archive.search-status',**args)
                    if status['status']!='ok':return status
                    payload=args|{'cache_instance_id':status['cache_instance_id'],'expected_generation':status['generation'],'operation_id':identifier()}
                    pending={'scope':dict(scope),'payload':payload};self.search_pending_rebuild=pending
                return self.client.manage('archive.search-rebuild',**pending['payload'])
            return self.client.manage('archive.'+action,**args)
        def done(result):
            if action=='search-rebuild':self.search_pending_rebuild=None
            if not current():return
            if action=='search':
                self.search_rows=result.get('groups',[]);self.search_display_scope=dict(scope)
                self.search_tree.delete(*self.search_tree.get_children());self.search_targets.configure(values=());self.search_target.set('')
                for i,row in enumerate(self.search_rows):
                    self.search_tree.insert('','end',iid=str(i),values=(row['turn_id'],len(row['matched_targets']),row['coverage']))
            self.set_text(self.search_output,json.dumps(result,ensure_ascii=False,indent=2))
            self.notice.set('档案检索：'+LABELS.get(result.get('status'),result.get('status','unknown')))
        def failed(error):
            if action=='search-rebuild' and not isinstance(error,TransportUnknown):self.search_pending_rebuild=None
        self.run_task('档案检索',{'archive-search'},work,done,failure=failed,error_current=current)

    def choose_search_reference(self):
        if self.search_display_scope!=self.search_partition_args() or not self.search_target.get():
            self.notice.set('请先查询并选择命中目标。');return
        if self.archive_channel.get()!='desktop':
            self.notice.set('QQ来源不能跨入口送入窗口聊天。');return
        kind,ident=self.search_target.get().split(':',1)
        self.archive_audit.set(False);self.archive_kind.set(kind);self.archive_id.set(ident)
        self.choose_archive_reference()
