"""Nontechnical editors for explicit persona versions and relationship evidence."""
from datetime import datetime, timedelta, timezone
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from .models import identifier

KINDS={'核心设定':'core','表达习惯':'expression','关系说明':'relationship','临时状态':'state'}


class ProfileUI:
    def init_profile_vars(self):
        self.profile_text=tk.StringVar(value='启动人格服务并选择人格后读取。')
        self.profile_kind=tk.StringVar(value='核心设定')
        self.state_hours=tk.StringVar(value='24')
        self.profile_history_rows=[]
        self.relationship_proposal_rows=[]

    def build_profile(self,parent):
        self.heading(parent,'人格与关系','编辑保留同一身份和历史；导入仍创建全新人格。设定背景不作为与用户真实共同经历。')
        ttk.Label(parent,textvariable=self.profile_text,wraplength=720).pack(anchor='w',pady=8)
        bar=self.bar(parent);bar.pack(fill='x')
        self.button(bar,'刷新人格与关系',self.refresh_profile,{'profile-read'})
        self.button(bar,'编辑当前人格核心',self.edit_core,{'profile-edit'})
        self.button(bar,'编辑表达习惯',lambda:self.edit_profile_text('expression'),{'profile-edit'})
        self.button(bar,'编辑关系说明',lambda:self.edit_profile_text('relationship'),{'profile-edit'})
        row=ttk.Frame(parent);row.pack(fill='x',pady=8)
        ttk.Label(row,text='临时状态有效小时（默认24，最多168）：').pack(side='left')
        ttk.Entry(row,textvariable=self.state_hours,width=8).pack(side='left')
        bar=self.bar(parent);bar.pack(fill='x')
        self.button(bar,'设置临时状态',lambda:self.edit_profile_text('state'),{'profile-edit'})
        self.button(bar,'结束临时状态',self.clear_profile_state,{'profile-edit'})
        ttk.Label(parent,text='临时状态只影响当下表达，到期不再用于新调用，不自动推断情绪。来源被屏蔽的关系会提示复核；每日记忆任务不会修改这些设定。',wraplength=700).pack(anchor='w',pady=8)
        choice=ttk.Combobox(parent,textvariable=self.profile_kind,state='readonly',values=tuple(KINDS))
        choice.pack(fill='x');choice.bind('<<ComboboxSelected>>',lambda _:self.refresh_profile_history())
        self.profile_history_list=tk.Listbox(parent,height=5,width=1,exportselection=False)
        self.profile_history_list.pack(fill='both',expand=True,pady=6)
        bar=self.bar(parent);bar.pack(fill='x')
        self.button(bar,'读取版本历史',self.refresh_profile_history,{'profile-read'})
        self.button(bar,'查看选中版本',self.show_profile_version,{'profile-read'})
        self.button(bar,'与当前版本比较',self.compare_profile_version,{'profile-read'})
        self.button(bar,'恢复选中版本',self.restore_profile_version,{'profile-edit'})
        ttk.Label(parent,text='关系建议（只在你选择原文后创建，不调用模型，必须确认才生效）').pack(anchor='w',pady=8)
        self.relationship_proposal_list=tk.Listbox(parent,height=5,width=1,exportselection=False)
        self.relationship_proposal_list.pack(fill='both',expand=True)
        bar=self.bar(parent);bar.pack(fill='x')
        self.button(bar,'从来源创建关系建议',self.propose_relationship_ui,{'profile-edit'})
        self.button(bar,'查看建议与原文',self.show_relationship_proposal,{'profile-read'})
        self.button(bar,'编辑并确认关系建议',lambda:self.review_relationship_ui(True),{'profile-edit'})
        self.button(bar,'拒绝关系建议',lambda:self.review_relationship_ui(False),{'profile-edit'})

    def refresh_profile(self):
        epoch=self.view_epoch
        def read(_):
            return self.client.manage('persona.show'),self.client.manage('profile.current'),self.client.manage('relationship.proposals')
        def done(result):
            if epoch!=self.view_epoch:return
            persona,versions,proposals=result
            relation=versions['relationship']
            state=versions['state']
            expiry=datetime.fromisoformat(state['data']['expires_at']).astimezone().isoformat(timespec='seconds') if state['data']['expires_at'] else '无'
            self.profile_text.set(persona['name']+' · '+persona['id']+'\n'+' / '.join(k+' v'+str(versions[kind]['revision']) for k,kind in KINDS.items())+
                '\n当前关系：'+relation['data']['description'][:160]+('【来源已屏蔽，待复核】' if relation['source_blocked'] else '')+
                '\n临时状态：'+('生效中 · '+state['data']['text'][:100] if state['active'] else '未设置或已到期')+'；到期本机时间：'+expiry)
            self.relationship_proposal_rows=proposals
            self.relationship_proposal_list.delete(0,'end')
            labels={'pending':'待确认','accepted':'已确认','rejected':'已拒绝','stale':'来源已过期'}
            for row in proposals:
                self.relationship_proposal_list.insert('end',labels.get(row['status'],row['status'])+' · '+row['text'][:70])
        self.run_task('读取人格与关系',{'profile-read'},read,done,quiet=True)

    def poll_profile(self):
        if self.closed:return
        if self.current_page=='profile':self.refresh_profile()
        self.after(2000,self.poll_profile)

    def edit_profile_text(self,kind):
        epoch=self.view_epoch
        title={'expression':'表达习惯','relationship':'关系说明','state':'临时状态'}[kind]
        def loaded(version):
            if epoch!=self.view_epoch:return
            field={'expression':'style','relationship':'description','state':'text'}[kind]
            expires=None
            if kind=='state':
                try:
                    hours=float(self.state_hours.get())
                    if not 1/60<=hours<=168:raise ValueError()
                    expires=(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()
                except (ValueError,OverflowError):
                    self.notice.set('有效期应为1分钟至168小时。');return
            operation_id=identifier()
            def save(text):
                data={field:text}
                if kind=='state':data['expires_at']=expires
                return self.client.manage('profile.update',kind=kind,data=data,expected_version=version['id'],operation_id=operation_id)
            self.text_dialog(title,version['data'][field],save,resources={'profile-edit'},on_saved=lambda _:self.refresh_profile())
        self.run_task('读取'+title,{'profile-read'},lambda _:self.client.manage('profile.current',kind=kind),loaded)

    def clear_profile_state(self):
        def loaded(version):
            self.run_task('结束临时状态',{'profile-edit'},lambda _:self.client.manage('profile.update',kind='state',data={'text':'','expires_at':None},expected_version=version['id'],operation_id=identifier()),lambda _:self.refresh_profile())
        self.run_task('读取临时状态',{'profile-read'},lambda _:self.client.manage('profile.current',kind='state'),loaded)

    def edit_core(self):
        epoch=self.view_epoch
        def loaded(version):
            if epoch==self.view_epoch:self.core_editor(version)
        self.run_task('读取核心设定',{'profile-read'},lambda _:self.client.manage('profile.current',kind='core'),loaded)

    def core_editor(self,version):
        from .desktop import ScrollPage
        dialog,body,footer=self.dialog_shell('编辑当前人格核心',(780,620),(480,300))
        scroll=ScrollPage(body);scroll.pack(fill='both',expand=True)
        editors={}
        fields=(('name','名称'),('introduction','自我介绍'),('core_traits','核心特征（每行一项）'),('values_and_boundaries','价值与边界（每行一项）'),('initial_background','初始背景（不代表与用户已发生的经历）'))
        for field,label in fields:
            ttk.Label(scroll.body,text=label).pack(anchor='w',pady=(6,0))
            box=scrolledtext.ScrolledText(scroll.body,height=2 if field=='name' else 4,width=1,wrap='word')
            box.pack(fill='x',pady=4)
            value=version['data'][field]
            box.insert('1.0','\n'.join(value) if isinstance(value,list) else value)
            editors[field]=box
        status=tk.StringVar(value='保存保留人格ID、历史会话和记忆；不调用模型。')
        ttk.Label(footer,textvariable=status,wraplength=620).grid(row=0,column=0,columnspan=3,sticky='w')
        busy=False
        initial={field:box.get('1.0','end-1c') for field,box in editors.items()}
        operation_id=identifier()
        def close():
            if busy:status.set('正在保存，请等待结果。');return
            changed=any(box.get('1.0','end-1c')!=initial[field] for field,box in editors.items())
            if not changed or messagebox.askyesno('放弃修改','核心设定尚未保存，确定放弃？',parent=dialog):dialog.destroy()
        def failed(error):
            nonlocal busy
            busy=False;save_button.configure(state='normal');status.set('保存失败，内容保留：'+str(error))
            for box in editors.values():box.configure(state='normal')
        def saved(_):
            dialog.destroy();self.refresh_personas();self.refresh_profile()
        def save(_=None):
            nonlocal busy
            if busy:return 'break'
            data={field:box.get('1.0','end-1c') for field,box in editors.items()}
            for field in ('core_traits','values_and_boundaries'):
                data[field]=[line.strip() for line in data[field].splitlines() if line.strip()]
            busy=True;save_button.configure(state='disabled');status.set('正在保存…')
            for box in editors.values():box.configure(state='disabled')
            operation=self.run_task('保存核心设定',{'profile-edit'},lambda _:self.client.manage('profile.update',kind='core',data=data,expected_version=version['id'],operation_id=operation_id),saved,failure=failed)
            if operation is None:failed(ValueError('相关操作仍忙，请稍后保存。'))
            return 'break'
        ttk.Button(footer,text='取消',command=close).grid(row=1,column=1,padx=6)
        save_button=ttk.Button(footer,text='保存核心设定',command=save);save_button.grid(row=1,column=2)
        dialog.protocol('WM_DELETE_WINDOW',close);dialog.bind('<Control-Return>',save)
        dialog.core_editors=editors
        return dialog

    def refresh_profile_history(self):
        kind=KINDS[self.profile_kind.get()];epoch=self.view_epoch
        def done(rows):
            if epoch!=self.view_epoch or kind!=KINDS[self.profile_kind.get()]:return
            self.profile_history_rows=rows;self.profile_history_list.delete(0,'end')
            for row in rows:self.profile_history_list.insert('end',('● ' if row['current'] else '○ ')+'v'+str(row['revision'])+' · '+row['created_at']+' · '+row['actor'])
        self.run_task('读取版本历史',{'profile-read'},lambda _:self.client.manage('profile.history',kind=kind),done)

    def selected_profile_version(self):
        indexes=self.profile_history_list.curselection()
        return self.profile_history_rows[indexes[0]] if indexes else None

    def show_profile_version(self):
        row=self.selected_profile_version()
        if row:self.show_json('人格版本详情',row)

    def compare_profile_version(self):
        row=self.selected_profile_version()
        if not row:return
        def loaded(current):
            self.run_task('比较版本',{'profile-read'},lambda _:self.client.manage('profile.compare',left_id=row['id'],right_id=current['id']),lambda value:self.show_json('与当前版本比较',value))
        self.run_task('读取当前版本',{'profile-read'},lambda _:self.client.manage('profile.current',kind=row['kind']),loaded)

    def restore_profile_version(self):
        row=self.selected_profile_version()
        if not row or not messagebox.askyesno('恢复版本','将以新版本恢复所选内容，保留历史和当前记忆有效性。过期状态或屏蔽来源不能恢复。继续？'):return
        def loaded(current):
            self.run_task('恢复版本',{'profile-edit'},lambda _:self.client.manage('profile.restore',version_id=row['id'],expected_version=current['id'],operation_id=identifier()),lambda _:self.refresh_profile_history())
        self.run_task('读取当前版本',{'profile-read'},lambda _:self.client.manage('profile.current',kind=row['kind']),loaded)

    def propose_relationship_ui(self):
        def selected(event_id):
            operation_id=identifier()
            self.text_dialog('带来源的关系建议','',lambda text:self.client.manage('relationship.propose',text=text,source_event_id=event_id,operation_id=operation_id),resources={'profile-edit'},on_saved=lambda _:self.refresh_profile())
        self.source_dialog(selected)

    def selected_relationship_proposal(self):
        indexes=self.relationship_proposal_list.curselection()
        return self.relationship_proposal_rows[indexes[0]] if indexes else None

    def show_relationship_proposal(self):
        row=self.selected_relationship_proposal()
        if row:self.run_task('读取关系来源',{'profile-read'},lambda _:self.client.manage('relationship.proposal-show',proposal_id=row['id']),lambda value:self.show_json('关系建议与原文',value))

    def review_relationship_ui(self,accept):
        row=self.selected_relationship_proposal()
        if not row or row['status']!='pending':return
        operation_id=identifier()
        if accept:
            self.text_dialog('确认关系建议',row['text'],lambda text:self.client.manage('relationship.review',proposal_id=row['id'],accept=True,operation_id=operation_id,text=text),resources={'profile-edit'},on_saved=lambda _:self.refresh_profile(),save_label='确认关系生效')
        else:
            self.run_task('拒绝关系建议',{'profile-edit'},lambda _:self.client.manage('relationship.review',proposal_id=row['id'],accept=False,operation_id=operation_id),lambda _:self.refresh_profile())
