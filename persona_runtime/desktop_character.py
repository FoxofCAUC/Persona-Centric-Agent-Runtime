"""Structured character forms. Drafts remain local until a version-checked save succeeds."""
from copy import deepcopy
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .character import FIELDS, MAX_BYTES, validate_character
from .models import identifier

LABELS = {
 'identity':('身份扩展',{'aliases':'别名（每行一项）','species':'物种','gender':'性别设定','pronouns':'称谓'}),
 'age':('年龄与生日',{'policy':'年龄策略','fixed_age':'固定年龄','birth_date':'出生日期（YYYY-MM-DD）','birthday':'生日（MM-DD）'}),
 'appearance':('身体与外貌',{'height_cm':'身高（厘米）','weight_kg':'体重（千克）','blood_type':'血型（A/B/AB/O/other）','hair_color':'发色','eye_color':'瞳色','description':'外貌描述'}),
 'biography':('社会背景',{'hometown':'家乡','occupation':'职业','education':'教育经历','family':'家庭设定','relationships':'背景关系'}),
 'interests':('兴趣',{'likes':'喜欢（每行一项）','dislikes':'讨厌（每行一项）','hobbies':'爱好（每行一项）','notes':'补充说明'}),
 'social':('对外形象',{'public_image':'社交形象','humor':'幽默方式','context_rules':'情境表达规则'}),
 'world':('角色世界',{'name':'世界名称','description':'世界说明'}),
}
AGE_LABELS={'未设定':'unspecified','固定年龄':'fixed','随现实日期增长':'calendar'}


def read_package(path):
    file=Path(path)
    if file.stat().st_size>256*1024:
        raise ValueError('角色包超过256 KiB，未读取。')
    return json.loads(file.read_text(encoding='utf-8-sig'))


class CharacterUI:
    def character_bar(self, parent):
        bar=self.bar(parent);bar.pack(fill="x",pady=3)
        return bar

    def init_character_vars(self):
        self.character_status=tk.StringVar(value='启动服务后读取角色资料。')
        self.character_summary=tk.StringVar()
        self.character_data=None
        self.character_rows=[]
        self.character_dialogs=[]

    def build_character(self,parent):
        ttk.Label(parent,text='角色资料',style='Title.TLabel').pack(anchor='w')
        ttk.Label(parent,textvariable=self.character_status,wraplength=700).pack(anchor='w',pady=6)
        bar=self.character_bar(parent)
        self.button(bar,'读取角色资料',self.refresh_character,{'character-read'})
        self.button(bar,'编辑角色资料',self.edit_character,{'character-read'})
        self.button(bar,'导出角色包',self.export_character,{'character-write'})
        self.button(bar,'导入为新角色',self.import_character,{'character-write'})
        tabs=ttk.Notebook(parent);tabs.pack(fill='both',expand=True,pady=8)
        basic=ttk.Frame(tabs,padding=8);tabs.add(basic,text='基本资料')
        ttk.Label(basic,textvariable=self.character_summary,wraplength=700,justify='left').pack(anchor='w')
        bar=self.character_bar(basic)
        self.button(bar,'编辑姓名与人格核心',self.edit_core,{'profile-read'})
        self.button(bar,'编辑基础表达',lambda:self.edit_profile_text('expression'),{'profile-read'})
        for kind,title in [('lorebook','设定书'),('examples','表达样例')]:
            frame=ttk.Frame(tabs,padding=8);tabs.add(frame,text=title)
            ttk.Label(frame,text='点击“编辑角色资料”管理条目；保存前不会改变聊天设定。').pack(anchor='w')
            listing=tk.Listbox(frame,height=9,width=1);listing.pack(fill='both',expand=True)
            setattr(self,'character_'+kind+'_list',listing)
        history=ttk.Frame(tabs,padding=8);tabs.add(history,text='版本与预览')
        self.character_history=tk.Listbox(history,height=5,width=1);self.character_history.pack(fill='x')
        bar=self.character_bar(history)
        self.button(bar,'读取版本历史',self.refresh_character_history,{'character-read'})
        self.button(bar,'查看选中版本',self.show_character_version,{'character-read'})
        self.button(bar,'与当前比较',self.compare_character_version,{'character-read'})
        self.button(bar,'恢复选中版本',self.restore_character_version,{'character-write'})
        ttk.Label(history,text='输入测试消息，预览本地激活结果（不调用模型）').pack(anchor='w',pady=6)
        self.character_preview_input=scrolledtext.ScrolledText(history,height=3,width=1,wrap='word');self.character_preview_input.pack(fill='x')
        self.button(self.character_bar(history),'预览设定激活',self.preview_character,{'character-read'})

    def character_read(self, action, done, **args):
        epoch=self.view_epoch
        def read(_):
            persona=self.client.manage('persona.show')
            return persona['id'],self.client.manage(action,persona_id=persona['id'],**args)
        def loaded(result):
            if epoch==self.view_epoch and not self.closed:done(*result)
        return self.run_task('读取角色资料',{'character-read'},read,loaded)

    def refresh_character(self):
        def loaded(persona_id,value):
            self.character_data=value
            core=value['profile']['core']['data'];data=value['data']
            self.character_status.set(core['name']+' · 角色资料 v'+str(value['revision']))
            lines=['姓名：'+core['name'],'核心特征：'+'、'.join(core['core_traits']),
                   '基础表达：'+value['profile']['expression']['data']['style']]
            for group,(_,fields) in LABELS.items():
                for field,label in fields.items():
                    item=data[group][field]
                    if field=='policy':item=next(k for k,v in AGE_LABELS.items() if v==item)
                    if item is not None and item!=[]:
                        lines.append(label+'：'+('、'.join(item) if isinstance(item,list) else str(item)))
            self.character_summary.set('\n'.join(lines))
            for kind in ('lorebook','examples'):
                listing=getattr(self,'character_'+kind+'_list');listing.delete(0,'end')
                for row in data[kind]:listing.insert('end',('启用' if row['enabled'] else '停用')+' · '+row.get('title',row.get('scenario','')))
        self.character_read('character.current',loaded)

    def edit_character(self):
        self.character_read('character.current',lambda persona,value:self.character_editor(persona,value))

    def character_editor(self,persona_id,version):
        from .desktop import ScrollPage
        dialog,body,footer=self.dialog_shell('编辑角色资料 · v'+str(version['revision']),(840,650),(500,340))
        tabs=ttk.Notebook(body);tabs.pack(fill='both',expand=True)
        page=ScrollPage(tabs);tabs.add(page,text='基本资料')
        editors={};draft=deepcopy(version['data']);initial=deepcopy(draft)
        for group,(title,fields) in LABELS.items():
            frame=ttk.LabelFrame(page.body,text=title,padding=8);frame.pack(fill='x',pady=5)
            for field,label in fields.items():
                ttk.Label(frame,text=label).pack(anchor='w')
                value=draft[group][field]
                if field=='policy':
                    widget=ttk.Combobox(frame,state='readonly',values=tuple(AGE_LABELS))
                    widget.set(next(k for k,v in AGE_LABELS.items() if v==value))
                else:
                    widget=scrolledtext.ScrolledText(frame,height=2,width=1,wrap='word')
                    widget.insert('1.0','\n'.join(value) if isinstance(value,list) else '' if value is None else str(value))
                widget.pack(fill='x',pady=(2,6));editors[(group,field)]=widget
        lists={}
        for kind,title in [('lorebook','设定书'),('examples','表达样例')]:
            frame=ttk.Frame(tabs,padding=8);tabs.add(frame,text=title)
            listing=tk.Listbox(frame,height=12,width=1,exportselection=False);listing.pack(fill='both',expand=True);lists[kind]=listing
            def refresh(kind=kind,listing=listing):
                listing.delete(0,'end')
                for row in draft[kind]:listing.insert('end',('启用' if row['enabled'] else '停用')+' · '+row.get('title',row.get('scenario','')))
            def selected(kind=kind,listing=listing):
                return listing.curselection()[0] if listing.curselection() else None
            def edit(new=False,kind=kind,refresh=refresh,selected=selected):
                index=None if new else selected()
                if not new and index is None:return
                self.character_entry_editor(dialog,draft,kind,index,refresh)
            def remove(kind=kind,refresh=refresh,selected=selected):
                index=selected()
                if index is not None and messagebox.askyesno('删除条目','删除草稿中的选中条目？保存角色资料后生效。',parent=dialog):
                    draft[kind].pop(index);refresh()
            def toggle(kind=kind,refresh=refresh,selected=selected):
                index=selected()
                if index is not None:draft[kind][index]['enabled']=not draft[kind][index]['enabled'];refresh()
            bar=self.character_bar(frame)
            for label,fn in [('新增',lambda edit=edit:edit(True)),('编辑',edit),('启用/停用',toggle),('删除',remove)]:
                ttk.Button(bar,text=label,command=fn).pack(side='left',padx=4)
            refresh()
        status=tk.StringVar(value='未知资料留空；本页只修改扩展资料，保存保留原身份与历史。')
        ttk.Label(footer,textvariable=status,wraplength=600).grid(row=0,column=0,columnspan=3,sticky='w')
        busy=False;operation={'id':identifier(),'payload':None}
        control_states=[]
        def freeze(lock):
            if lock:
                control_states.clear()
                def walk(parent):
                    for child in parent.winfo_children():
                        if isinstance(child,(tk.Text,tk.Listbox,ttk.Button,ttk.Combobox,ttk.Checkbutton)):
                            control_states.append((child,str(child.cget('state'))));child.configure(state='disabled')
                        walk(child)
                walk(dialog)
            else:
                for child,state in control_states:
                    if child.winfo_exists():child.configure(state=state)
                control_states.clear()

        def collect():
            data=deepcopy(draft)
            for (group,field),widget in editors.items():
                text=widget.get() if field=='policy' else widget.get('1.0','end-1c').strip()
                if field=='policy':value=AGE_LABELS[text]
                elif isinstance(FIELDS[group][field],list):value=[s.strip() for s in text.splitlines() if s.strip()]
                elif field in ('height_cm','weight_kg'):value=float(text) if text else None
                elif field=='fixed_age':value=int(text) if text else None
                else:value=text or None
                data[group][field]=value
            return data
        def changed():
            try:return collect()!=initial
            except (ValueError,KeyError):return True
        def close():
            if busy:status.set('正在保存，请等待结果。');return False
            if any(isinstance(c,tk.Toplevel) and c.winfo_exists() for c in self.winfo_children() if getattr(c,'character_parent',None) is dialog):
                status.set('请先保存或关闭正在编辑的设定条目。');return False
            if changed() and not messagebox.askyesno('保留或放弃草稿','角色资料尚未保存。确定放弃草稿？',parent=dialog):return False
            dialog.destroy();return True
        def failed(error):
            nonlocal busy
            busy=False;save_button.configure(state='normal')
            status.set('保存失败，草稿已保留：'+str(error)+'。可重新读取或比较差异。')
            freeze(False)
        def save():
            nonlocal busy
            if busy:return
            try:data=validate_character(collect())
            except (ValueError,TypeError) as error:failed(error);return
            payload=json.dumps(data,ensure_ascii=False,sort_keys=True)
            if operation['payload'] is not None and operation['payload']!=payload:operation['id']=identifier()
            operation['payload']=payload
            busy=True;freeze(True);status.set('正在保存…')
            def saved(value):
                nonlocal busy,initial,version
                busy=False;version=value;initial=deepcopy(data);draft.update(deepcopy(data))
                freeze(False);operation.update(id=identifier(),payload=None);status.set('已保存为 v'+str(value['revision']))
                self.refresh_character()
            result=self.run_task('保存角色资料',{'character-write','persona'},
                lambda _:self.client.manage('character.update',persona_id=persona_id,data=data,expected_version=version['id'],operation_id=operation['id']),saved,failure=failed)
            if result is None:failed(ValueError('相关操作仍忙，请稍后保存。'))
        def reload():
            if changed() and not messagebox.askyesno('重新读取','重新读取会放弃当前草稿，继续？',parent=dialog):return
            def loaded(value):dialog.destroy();self.character_editor(persona_id,value)
            self.run_task('重新读取角色资料',{'character-read'},lambda _:self.client.manage('character.current',persona_id=persona_id),loaded)
        def compare():
            self.run_task('比较角色资料',{'character-read'},lambda _:self.client.manage('character.current',persona_id=persona_id),
                          lambda value:self.show_json('服务器当前版本（当前草稿保留）',value['data']))
        ttk.Button(footer,text='重新读取',command=reload).grid(row=1,column=0,sticky='w')
        ttk.Button(footer,text='查看服务器当前资料',command=compare).grid(row=2,column=0,sticky='w')
        ttk.Button(footer,text='关闭',command=close).grid(row=1,column=1,padx=6)
        save_button=ttk.Button(footer,text='保存角色资料',command=save);save_button.grid(row=1,column=2)
        dialog.protocol('WM_DELETE_WINDOW',close)
        dialog.character_editors=editors;dialog.character_draft=draft;dialog.character_save=save
        dialog.character_close=close;dialog.character_dirty=changed;dialog.character_busy=lambda:busy
        self.character_dialogs.append(dialog)
        return dialog

    def character_entry_editor(self,parent,draft,kind,index,refresh):
        new=index is None
        value=deepcopy(draft[kind][index]) if not new else (
            {'id':identifier(),'title':'','category':'背景事件','keywords':[],'text':'','priority':0,'enabled':True,'visibility':'conversation','dependencies':[]}
            if kind=='lorebook' else {'id':identifier(),'scenario':'','keywords':[],'text':'','enabled':True})
        dialog,body,footer=self.dialog_shell('编辑设定条目' if kind=='lorebook' else '编辑表达样例',(720,570),(480,340))
        from .desktop import ScrollPage
        page=ScrollPage(body);page.pack(fill='both',expand=True);widgets={}
        fields=[('title','标题'),('category','类别'),('keywords','关键词（每行一项）'),('text','正文'),('priority','优先级')] if kind=='lorebook' else [('scenario','情境'),('keywords','场景关键词（每行一项）'),('text','示例回复')]
        for field,label in fields:
            ttk.Label(page.body,text=label).pack(anchor='w')
            box=scrolledtext.ScrolledText(page.body,height=5 if field=='text' else 2,width=1,wrap='word');box.pack(fill='x',pady=4)
            item=value[field];box.insert('1.0','\n'.join(item) if isinstance(item,list) else str(item));widgets[field]=box
        enabled=tk.BooleanVar(value=value['enabled']);ttk.Checkbutton(page.body,text='启用',variable=enabled).pack(anchor='w')
        visible=None;deps=None;candidates=[]
        if kind=='lorebook':
            visible=tk.BooleanVar(value=value['visibility']=='conversation');ttk.Checkbutton(page.body,text='允许在聊天中使用（取消后仅创作者可见）',variable=visible).pack(anchor='w')
            ttk.Label(page.body,text='依赖条目（可多选）').pack(anchor='w')
            deps=tk.Listbox(page.body,selectmode='multiple',height=4,exportselection=False);deps.pack(fill='x')
            candidates=[r for r in draft['lorebook'] if r['id']!=value['id']]
            for i,row in enumerate(candidates):
                deps.insert('end',row['title'])
                if row['id'] in value['dependencies']:deps.selection_set(i)
        status=tk.StringVar();ttk.Label(footer,textvariable=status,wraplength=500).grid(row=0,column=0,columnspan=3,sticky='w')
        def collect():
            row=deepcopy(value)
            for field,box in widgets.items():
                text=box.get('1.0','end-1c').strip()
                row[field]=[s.strip() for s in text.splitlines() if s.strip()] if field=='keywords' else int(text) if field=='priority' else text
            row['enabled']=enabled.get()
            if visible is not None:
                row['visibility']='conversation' if visible.get() else 'creator_only'
                row['dependencies']=[candidates[i]['id'] for i in deps.curselection()]
            return row
        def close():
            try:dirty=collect()!=value
            except ValueError:dirty=True
            if dirty and not messagebox.askyesno('放弃条目修改','此条目尚未加入角色草稿，确定放弃？',parent=dialog):return
            dialog.destroy()
        def save():
            try:
                row=collect();candidate=deepcopy(draft)
                if new:candidate[kind].append(row)
                else:candidate[kind][index]=row
                # Full character validation happens on the parent save (basic fields may still be in flight).
                validate_character(candidate)
            except (ValueError,TypeError) as error:status.set(str(error));return
            draft[kind]=candidate[kind];refresh();dialog.destroy()
        ttk.Button(footer,text='取消',command=close).grid(row=1,column=1)
        ttk.Button(footer,text='加入角色草稿',command=save).grid(row=1,column=2)
        dialog.character_parent=parent
        dialog.protocol('WM_DELETE_WINDOW',close);dialog.transient(parent);dialog.grab_set()

    def character_can_leave(self):
        for dialog in list(self.character_dialogs):
            if dialog.winfo_exists() and not dialog.character_close():return False
        return True

    def refresh_character_history(self):
        def loaded(persona,rows):
            self.character_rows=rows;self.character_history.delete(0,'end')
            for row in rows:self.character_history.insert('end',('当前 · ' if row['current'] else '')+'v'+str(row['revision'])+' · '+row['actor']+' · '+row['created_at'])
        self.character_read('character.history',loaded)

    def selected_character(self):
        indexes=self.character_history.curselection()
        return self.character_rows[indexes[0]] if indexes else None

    def show_character_version(self):
        row=self.selected_character()
        if row:self.show_json('角色资料 v'+str(row['revision']),row['data'])

    def compare_character_version(self):
        row=self.selected_character()
        if row:self.character_read('character.current',lambda persona,current:self.run_task('比较角色版本',{'character-read'},
            lambda _:self.client.manage('character.compare',persona_id=persona,left_id=row['id'],right_id=current['id']),lambda value:self.show_json('角色资料差异',value)))

    def restore_character_version(self):
        row=self.selected_character()
        if not row or not self.character_can_leave():return
        if not messagebox.askyesno('恢复角色资料','将选中版本恢复为一个新版本，历史、关系和记忆不会回退。',parent=self):return
        def loaded(persona,current):
            operation=identifier()
            self.run_task('恢复角色资料',{'character-write','persona'},lambda _:self.client.manage('character.restore',persona_id=persona,version_id=row['id'],expected_version=current['id'],operation_id=operation),lambda _:self.refresh_character())
        self.character_read('character.current',loaded)

    def preview_character(self):
        text=self.character_preview_input.get('1.0','end-1c')
        self.character_read('character.preview',lambda persona,value:self.show_json('本地设定激活预览（未调用模型）',value),text=text)

    def export_character(self):
        self.character_read('character.export',lambda persona,value:self.notice.set('角色包已导出：'+value['path']))

    def import_character(self):
        if not self.character_can_leave():return
        path=filedialog.askopenfilename(parent=self,title='选择原生PCAR角色包',filetypes=[('JSON角色包','*.json')])
        if not path:return
        if not messagebox.askyesno('创建新角色','导入会创建独立新身份，不继承聊天、关系或记忆，继续？',parent=self):return
        operation=identifier()
        def loaded(persona,current):
            self.run_task('导入新角色',{'character-write','persona'},lambda _:self.client.manage('character.import',persona_id=persona,package=read_package(path),operation_id=operation),lambda value:(self.refresh_personas(),self.notice.set('已创建新角色：'+value['name']+'；可在角色列表选择。')))
        self.character_read('character.current',loaded)
