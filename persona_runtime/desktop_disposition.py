"""Small author-review panel. Async replies cannot replace another scope/draft."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk,messagebox,scrolledtext

from .models import identifier
from .archive import fingerprint
from .disposition_core.contracts import ACTIONS,CATEGORIES


class DispositionUI:
    def init_disposition_vars(self):
        self.dispo_channel=tk.StringVar(value='desktop');self.dispo_mode=tk.StringVar(value='baseline')
        self.dispo_status=tk.StringVar(value='未读取人格倾向；Shadow不影响回复。')
        self.dispo_state=None;self.dispo_document=None;self.dispo_sources=[];self.dispo_history=[]
        self.dispo_dirty=False;self.dispo_generation=0
        self.dispo_operations={}

    def _dispo_operation(self,value):
        key=fingerprint(value)
        if key not in self.dispo_operations:
            if len(self.dispo_operations)>=128:self.dispo_operations.pop(next(iter(self.dispo_operations)))
            self.dispo_operations[key]=identifier()
        return self.dispo_operations[key]

    def build_disposition(self,parent):
        ttk.Label(parent,textvariable=self.dispo_status,wraplength=700).pack(anchor='w')
        bar=self.character_bar(parent)
        self.button(bar,'读取倾向与来源',self.refresh_dispositions,{'character-read'})
        self.button(bar,'查看来源原文',self.disposition_source,{'character-read'})
        self.button(bar,'编译所选来源',self.compile_dispositions,{'character-write'})
        self.dispo_source_list=tk.Listbox(parent,selectmode='extended',exportselection=False,height=5,width=1)
        self.dispo_source_list.pack(fill='x')
        bar=self.character_bar(parent)
        ttk.Combobox(bar,textvariable=self.dispo_channel,values=('desktop','qq'),state='readonly',width=10).grid(row=0,column=0)
        self.dispo_mode_combo=ttk.Combobox(bar,textvariable=self.dispo_mode,values=('baseline','shadow'),state='readonly',width=10)
        self.dispo_mode_combo.grid(row=0,column=1)
        self.button(bar,'保存观察模式',self.save_disposition_mode,{'character-write'})
        self.button(bar,'查询编译结果',self.disposition_compile_status,{'character-read'})
        self.dispo_definition_list=tk.Listbox(parent,height=5,width=1,exportselection=False);self.dispo_definition_list.pack(fill='x',pady=5)
        bar=self.character_bar(parent)
        for label,fn in [('修改草案项',self.edit_disposition),('拒绝草案项',self.reject_disposition),('保存草案',self.save_disposition_draft),('集中确认',self.confirm_dispositions)]:
            self.button(bar,label,fn,{'character-write'})
        self.dispo_version_list=tk.Listbox(parent,height=3,width=1,exportselection=False);self.dispo_version_list.pack(fill='x')
        bar=self.character_bar(parent)
        for label,fn in [('查看倾向版本',self.show_disposition_version),('与草案比较',self.compare_disposition_version),('恢复为草案',self.restore_dispositions),('撤回选中版本',self.withdraw_dispositions)]:
            self.button(bar,label,fn,{'character-read' if label.startswith(('查看','与')) else 'character-write'})
        self.dispo_preview_text=scrolledtext.ScrolledText(parent,height=2,width=1);self.dispo_preview_text.pack(fill='x',pady=5)
        bar=self.character_bar(parent)
        self.dispo_preview_button=self.button(bar,'预览Shadow（零模型）',self.preview_disposition,{'character-read'})
        self.button(bar,'查看决策记录',self.inspect_dispositions,{'character-read'})

    def _dispo_token(self):
        return (self.view_epoch,getattr(self,'persona_id',None),self.dispo_channel.get(),self.dispo_generation)

    def _dispo_run(self,name,work,done):
        token=self._dispo_token()
        current=lambda: not self.closed and token==self._dispo_token()
        return self.run_task(name,{'character-read'},lambda _:work(),
            lambda result:done(result) if current() else None,error_current=current)

    def refresh_dispositions(self):
        if self.dispo_dirty and not messagebox.askyesno('保留草稿','重新读取会放弃未保存修改，是否继续？',default='no',parent=self):return
        self.dispo_generation+=1;channel=self.dispo_channel.get()
        def read():
            pid=self.client.manage('persona.show')['id'];state=self.client.manage('disposition.current',persona_id=pid)
            return {'pid':pid,'state':state,'sources':self.client.manage('disposition.sources',persona_id=pid)['sources'],
                'document':self.client.manage('disposition.show',persona_id=pid,version_id=state['draft_id'])['document'] if state['draft_id'] else None,
                'history':self.client.manage('disposition.history',persona_id=pid),
                'policy':self.client.manage('interaction.current',persona_id=pid,channel=channel)}
        def loaded(value):
            self.dispo_pid=value['pid'];self.dispo_state=value['state'];self.dispo_sources=value['sources']
            self.dispo_document=value['document'];self.dispo_history=value['history'];self.dispo_policy=value['policy'];self.dispo_dirty=False
            self.dispo_mode_combo.configure(values=('baseline','shadow','assist') if value['policy'].get('assist_admitted') else ('baseline','shadow'))
            self.dispo_mode.set(value['policy'].get('disposition_mode','baseline'))
            self.dispo_preview_button.configure(text='预览胶囊（零模型）' if value['policy'].get('assist_admitted') else '预览Shadow（零模型）')
            note='隔离Assist，本实例允许受控渲染' if self.dispo_mode.get()=='assist' else '仅观察，不进入Prompt'
            self.dispo_status.set('状态：'+value['state']['status']+'；修订 '+str(value['state']['revision'])+'；'+note)
            self.dispo_source_list.delete(0,'end')
            for source in self.dispo_sources:self.dispo_source_list.insert('end',source['kind']+' · '+source['path']+' · '+source['entry_id']+' · '+str(len(source['text']))+'字')
            self._render_disposition_defs();self.dispo_version_list.delete(0,'end')
            for v in self.dispo_history:self.dispo_version_list.insert('end',str(v['revision'])+' · '+v['kind']+' · '+v['id'])
        self._dispo_run('读取人格倾向',read,loaded)

    def _render_disposition_defs(self):
        self.dispo_definition_list.delete(0,'end')
        for d in (self.dispo_document or {}).get('definitions',[]):self.dispo_definition_list.insert('end',d['label']+('' if d['enabled'] else '（停用）'))

    def disposition_source(self):
        selected=self.dispo_source_list.curselection()
        if selected:
            original=dict(self.dispo_sources[selected[0]]);pid=self.dispo_pid
            def read():
                rows=self.client.manage('disposition.sources',persona_id=pid)['sources']
                current=next((r for r in rows if r['source_id']==original['source_id'] and r['version_id']==original['version_id'] and r['content_hash']==original['content_hash']),None)
                if current is None:raise ValueError('作者来源已变化或不可读取，请重新读取并比较。')
                return current
            self._dispo_run('重新核验作者来源',read,lambda v:self.show_json('作者来源（本次授权读取）',v))

    def compile_dispositions(self):
        if not self.dispo_state:return
        selected=[self.dispo_sources[i] for i in self.dispo_source_list.curselection()]
        if not selected:return
        if not messagebox.askyesno('单次编译','将向已配置的选择模型提交所选 '+str(sum(len(s['text']) for s in selected))+' 字作者资料；最多一次调用，不自动确认。是否继续？',parent=self):return
        pid=self.dispo_pid;rev=self.dispo_state['revision'];ids=[r['source_id'] for r in selected];op=self._dispo_operation(['compile',pid,rev,ids])
        self._dispo_run('提出倾向草案',lambda:self.client.manage('disposition.compile',persona_id=pid,source_ids=ids,expected_revision=rev,operation_id=op),
            lambda value:self.show_json('编译已登记；请查询结果',value))

    def disposition_compile_status(self):
        if not self.dispo_state:return
        pid=self.dispo_pid
        self._dispo_run('查询编译账本',lambda:self.client.manage('disposition.status',persona_id=pid),lambda value:self.show_json('编译状态与用量（完成后重新读取草案）',value))

    def edit_disposition(self):
        selection=self.dispo_definition_list.curselection()
        if not selection or not self.dispo_document:return
        index=selection[0];draft=deepcopy(self.dispo_document['definitions'][index]);token=self._dispo_token()
        from .desktop import ScrollPage
        dialog,host,footer=self.dialog_shell('审核倾向草案',(720,620),(450,320))
        page=ScrollPage(host);page.pack(fill='both',expand=True);body=page.body
        ttk.Label(body,text='来源定位不可在此伪造；新设定应先修改角色权威资料。').pack(anchor='w')
        label=ttk.Entry(body);label.insert(0,draft['label']);label.pack(fill='x')
        priority=tk.IntVar(value=draft['priority']);ttk.Spinbox(body,from_=0,to=100,textvariable=priority).pack(anchor='w')
        enabled=tk.BooleanVar(value=draft['enabled']);ttk.Checkbutton(body,text='启用',variable=enabled).pack(anchor='w')
        prefs={};grid=ttk.Frame(body);grid.pack(fill='x')
        for i,action in enumerate(ACTIONS):
            ttk.Label(grid,text=action).grid(row=i,column=0,sticky='w');var=tk.IntVar(value=draft['preferences'].get(action,0));prefs[action]=var
            ttk.Spinbox(grid,from_=-2,to=2,textvariable=var,width=6).grid(row=i,column=1)
        ttk.Label(body,text='触发条件（全部满足；空为无条件）').pack(anchor='w')
        trigger_rows=[];trigger_frame=ttk.Frame(body);trigger_frame.pack(fill='x')
        values=('acknowledgement','support_request','celebration','information_request','advice_request','correction','ambiguous_reference','unknown','technical','facts','neutral','close','distant')
        for i in range(8):
            row=draft['triggers'][i] if i<len(draft['triggers']) else {'field':'','op':'eq','value':'unknown'}
            variables=[tk.StringVar(value=row[k]) for k in ('field','op','value')];trigger_rows.append(variables)
            for j,options in enumerate((('','category','topic','relationship'),('eq','ne'),values)):
                ttk.Combobox(trigger_frame,textvariable=variables[j],values=options,state='readonly',width=(14 if j<2 else 25)).grid(row=i,column=j)
        def sources():
            pid=self.dispo_pid;refs=deepcopy(draft['source_refs']);versions=deepcopy(self.dispo_document['source_versions'])
            def read():
                catalog={r['source_id']:r for r in self.client.manage('disposition.sources',persona_id=pid,versions=versions)['sources']}
                result=[]
                for ref in refs:
                    row=catalog.get(ref['source_id'])
                    valid=row is not None and row['version_id']==ref['version_id'] and row['content_hash']==ref['content_hash']
                    quote=row['text'][ref['start']:ref['end']] if valid else None
                    if valid and fingerprint(quote)!=ref['fragment_hash']:quote=None
                    result.append({'reference':ref,'quote':quote,'status':'readable' if quote is not None else 'unavailable'})
                return result
            self._dispo_run('核验草案引文',read,lambda value:self.show_json('草案引文（本次授权）',value) if dialog.winfo_exists() else None)
        ttk.Button(footer,text='来源定位',command=sources).grid(row=0,column=0)
        def accept():
            if token!=self._dispo_token():
                messagebox.showwarning('选择已变化','保留窗口内容，请返回原人格重新读取比较。',parent=dialog);return
            try:
                draft.update(label=label.get(),priority=priority.get(),enabled=enabled.get(),preferences={a:v.get() for a,v in prefs.items()},
                    triggers=[dict(zip(('field','op','value'),[v.get() for v in row])) for row in trigger_rows if row[0].get()])
            except (ValueError,tk.TclError):return
            self.dispo_document['definitions'][index]=draft;self.dispo_dirty=True;self.dispo_generation+=1;self._render_disposition_defs();dialog.destroy()
        ttk.Button(footer,text='保留到本地草案',command=accept).grid(row=0,column=1)
        def changed():
            try:
                return label.get()!=draft['label'] or priority.get()!=draft['priority'] or enabled.get()!=draft['enabled'] or {a:v.get() for a,v in prefs.items()}!=draft['preferences'] or [dict(zip(('field','op','value'),[v.get() for v in row])) for row in trigger_rows if row[0].get()]!=draft['triggers']
            except (ValueError,tk.TclError):return True
        def close():
            if changed() and not messagebox.askyesno('关闭审核窗口','放弃本窗口未保留的修改？',default='no',parent=dialog):return False
            dialog.destroy();return True
        dialog.character_close=close;dialog.character_dirty=changed;dialog.character_busy=lambda:False
        self.character_dialogs.append(dialog);dialog.protocol('WM_DELETE_WINDOW',close)

    def reject_disposition(self):
        selected=self.dispo_definition_list.curselection()
        if selected and self.dispo_document:
            self.dispo_document['definitions'].pop(selected[0]);self.dispo_dirty=True;self.dispo_generation+=1;self._render_disposition_defs()

    def _dispo_write(self,action,**args):
        if not self.dispo_state:return
        pid=self.dispo_pid;revision=self.dispo_state['revision'];operation=self._dispo_operation([action,pid,revision,args])
        def done(value):
            self.dispo_dirty=False;self.refresh_dispositions()
        self._dispo_run('保存倾向操作',lambda:self.client.manage('disposition.'+action,persona_id=pid,expected_revision=revision,operation_id=operation,**args),done)

    def save_disposition_draft(self):
        if self.dispo_document:self._dispo_write('draft-update',document=deepcopy(self.dispo_document))

    def _dispo_pending(self):
        answer=messagebox.askyesnocancel('处理收集中输入','如有收集中批次：是=先封存；否=取消批次；取消=不执行本操作。',parent=self)
        return None if answer is None else 'seal' if answer else 'cancel'

    def confirm_dispositions(self):
        if not self.dispo_state or self.dispo_dirty:
            messagebox.showwarning('先保存草案','请先保存并核对草案，未保存内容不会发布。',parent=self);return
        action=self._dispo_pending()
        if action:self._dispo_write('confirm',draft_id=self.dispo_state['draft_id'],expected_active_id=self.dispo_state['active_id'],pending_action=action)

    def _dispo_selected_version(self):
        selected=self.dispo_version_list.curselection()
        return self.dispo_history[selected[0]]['id'] if selected else None

    def show_disposition_version(self):
        vid=self._dispo_selected_version()
        if vid:self._dispo_run('查看版本',lambda:self.client.manage('disposition.show',persona_id=self.dispo_pid,version_id=vid),lambda v:self.show_json('投影版本',v))

    def compare_disposition_version(self):
        vid=self._dispo_selected_version()
        if vid and self.dispo_state['draft_id']:self._dispo_run('比较倾向',lambda:self.client.manage('disposition.compare',persona_id=self.dispo_pid,left_id=vid,right_id=self.dispo_state['draft_id']),lambda v:self.show_json('版本差异',v))

    def restore_dispositions(self):
        vid=self._dispo_selected_version()
        if vid:self._dispo_write('restore',version_id=vid)

    def withdraw_dispositions(self):
        vid=self._dispo_selected_version()
        if vid:
            action=self._dispo_pending()
            if action:self._dispo_write('withdraw',version_id=vid,pending_action=action)

    def save_disposition_mode(self):
        if not self.dispo_state:return
        pid=self.dispo_pid;channel=self.dispo_channel.get();mode=self.dispo_mode.get();action=self._dispo_pending()
        if not action:return
        policy=self.dispo_policy
        if policy['channel']!=channel:
            messagebox.showwarning('先读取入口','入口已变化，请重新读取政策后保存。',parent=self);return
        op=self._dispo_operation(['mode',pid,channel,mode,policy['id'],action])
        def save():
            return self.client.manage('interaction.update',persona_id=pid,channel=channel,mode=policy['mode'],expected_version=policy['id'],operation_id=op,disposition_mode=mode,pending_action=action)
        def saved(value):
            self.dispo_policy=value
            self.dispo_status.set('观察模式已保存：'+value['disposition_mode'])
        self._dispo_run('保存Shadow模式',save,saved)

    def preview_disposition(self):
        if not self.dispo_state:return
        pid=self.dispo_pid;text=self.dispo_preview_text.get('1.0','end-1c');token=self._dispo_token();mode=self.dispo_mode.get()
        self._dispo_run('Shadow只读预览',lambda:self.client.manage('disposition.preview',persona_id=pid,members=[text],mode='assist' if mode=='assist' else 'shadow'),
            lambda v:self.show_json('隔离Assist预览' if mode=='assist' else 'Shadow预览（不进入Prompt）',v) if mode==self.dispo_mode.get() and text==self.dispo_preview_text.get('1.0','end-1c') and token==self._dispo_token() else None)

    def inspect_dispositions(self):
        if self.dispo_state:self._dispo_run('读取Shadow记录',lambda:self.client.manage('disposition.inspect',persona_id=self.dispo_pid),lambda v:self.show_json('观察记录；不同于模型behavior_intent',v))
