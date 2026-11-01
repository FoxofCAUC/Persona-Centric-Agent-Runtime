"""Blind review UI with optional, separately generated AI suggestions."""
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from .models import identifier


TASK_LABELS = {'pass': '通过', 'critical_error': '关键错误', 'uncertain': '无法判断'}
ACTION_LABELS = {
    'respond': '直接回应',
    'acknowledge': '确认或肯定',
    'ask': '询问必要信息',
    'clarify': '澄清歧义',
    'support': '支持或倾听',
    'explain': '解释说明',
    'challenge': '依要求质疑',
    'defer': '说明暂不能处理',
    'set_boundary': '说明边界',
    'uncertain': '无法判断',
}
NATURALNESS_LABELS = {'left': '左侧更自然', 'right': '右侧更自然', 'tie': '平局'}


def open_review(owner):
    dialog=tk.Toplevel(owner);dialog.title('PCAR 独立评测盲审');dialog.geometry('900x760')
    owner.character_dialogs.append(dialog)
    package=tk.StringVar();batch=tk.StringVar(value='1');index=tk.IntVar(value=1)
    status=tk.StringVar(value='填写本地评测包ID；AI建议须由独立脚本预先生成')
    bar=ttk.Frame(dialog);bar.pack(fill='x')
    ttk.Entry(bar,textvariable=package,width=30).pack(side='left')
    ttk.Combobox(bar,textvariable=batch,values=('1','2','3','4'),state='readonly',width=4).pack(side='left')
    ttk.Label(dialog,textvariable=status).pack(fill='x')
    source=scrolledtext.ScrolledText(dialog,height=6);source.pack(fill='x')
    pane=ttk.Frame(dialog);pane.pack(fill='both',expand=True)
    left=scrolledtext.ScrolledText(pane,width=40,height=14);left.pack(side='left',fill='both',expand=True)
    right=scrolledtext.ScrolledText(pane,width=40,height=14);right.pack(side='left',fill='both',expand=True)
    from .disposition_core.contracts import ACTIONS
    fields={};labels={};values={};selectors=[]
    action_choices={key:ACTION_LABELS[key] for key in (*ACTIONS,'uncertain')}
    for label,key,choices in [('左侧任务结果','left_task',TASK_LABELS),('右侧任务结果','right_task',TASK_LABELS),
        ('左侧主要交流动作','left_action',action_choices),('右侧主要交流动作','right_action',action_choices),('自然度','natural',NATURALNESS_LABELS)]:
        row=ttk.Frame(dialog);row.pack(fill='x');ttk.Label(row,text=label,width=18).pack(side='left')
        labels[key]=choices;values[key]={label:value for value,label in choices.items()}
        fields[key]=tk.StringVar(value='')
        selector=ttk.Combobox(row,textvariable=fields[key],values=tuple(choices.values()),state='readonly')
        selector.pack(side='left');selectors.append(selector)
    ttk.Label(dialog,text='评分理由（必填，请简述判断依据）').pack(anchor='w')
    reason=scrolledtext.ScrolledText(dialog,height=3);reason.pack(fill='x')
    state={'rows':[],'ai_rows':{},'generation':0,'loaded':None,'operations':{},'baseline':None,
           'saving':False,'pending_move':None,'ai_mode':None}
    def answers():
        return {key:values[key].get(var.get(),'') for key,var in fields.items()}
    def draft():
        return answers()|{'reason':reason.get('1.0','end-1c')}
    def is_dirty():
        return state['loaded'] is not None and (state['ai_mode']=='editing' or draft()!=state['baseline'])
    def token():return (owner.view_epoch,getattr(owner,'persona_id',None),package.get(),batch.get(),state['generation'])
    def call(name,work,done,failed=None,finished=None):
        accepted=token()
        completed=False
        def current():return not owner.closed and bool(dialog.winfo_exists()) and token()==accepted
        def success(value):
            nonlocal completed
            completed=True
            if finished:finished()
            if current():done(value)
        def failure(error):
            nonlocal completed
            completed=True
            if finished:finished()
            if current():
                if failed:failed(error)
                else:
                    status.set('载入失败，请核对评测包及服务连接后重试')
                    messagebox.showerror('载入失败','请核对评测包ID及人格服务连接；当前草稿仍保留。',parent=dialog)
        operation=owner.run_task(name,{'character-read'},lambda _:work(),success,
                                 failure=failure,error_current=current)
        if operation is None and not completed:failure(RuntimeError('review_operation_busy'))
    def editable(value):
        for widget in selectors:widget.configure(state='readonly' if value else 'disabled')
        reason.configure(state='normal' if value else 'disabled')
    def render():
        if not state['rows']:return
        row=state['rows'][index.get()-1];state['loaded']=row
        for widget,text in ((source,row['scenario']+'\n\n作者资料：'+row['author_summary']),(left,row['left']),(right,row['right'])):
            widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',text);widget.configure(state='disabled')
        assisted=state['ai_rows'].get(row['case_id'])
        old=row.get('review') or (assisted or {}).get('suggestion') or {}
        for key,var in fields.items():var.set(labels[key].get(old.get(key,''),''))
        reason.configure(state='normal');reason.delete('1.0','end');reason.insert('1.0',old.get('reason',''))
        if row.get('review'):
            state['ai_mode']='saved';editable(True)
            note=f'已保存人工审阅，修订{row["revision"]}'
        elif assisted and assisted.get('suggestion'):
            state['ai_mode']='pending';editable(False)
            note='DeepSeek建议待核查：请选择“通过”或“修改”'
        else:
            state['ai_mode']=None;editable(True)
            note='没有可用AI建议，请人工填写'
        state['baseline']=draft()
        status.set(f'第{batch.get()}批，第{index.get()}/25组；{note}')
    def load():
        if state['saving']:status.set('正在保存，请等待结果后再载入');return
        if is_dirty() and not messagebox.askyesno('保留草稿','载入会放弃本地未保存审阅，是否继续？',default='no',parent=dialog):return
        state['generation']+=1;pid=package.get();number=int(batch.get())
        scope=(owner.view_epoch,getattr(owner,'persona_id',None),pid,batch.get())
        def done(value):
            state['rows']=value['cases'];state['target']=scope;index.set(1)
            status.set('正在载入DeepSeek辅助评分')
            def ai_done(ai):
                state['ai_rows']={row['case_id']:row for row in ai['cases']};render()
            def ai_failed(_):
                state['ai_rows']={};render()
                status.set(f'第{batch.get()}批，第1/25组；未找到AI建议，可继续人工审阅')
            dialog.after(0,lambda:call('载入AI辅助评分',lambda:owner.client.manage('evaluation.ai.load',package_id=pid,batch=number),ai_done,ai_failed))
        call('载入盲审',lambda:owner.client.manage('evaluation.review.load',package_id=pid,batch=number),done)
    def move(delta):
        if not state['rows']:return
        if state['saving']:
            state['pending_move']=delta;status.set('正在保存，成功后将切换；新编辑的草稿会保留');return
        if is_dirty():
            messagebox.showwarning('保留草稿','请先保存当前审阅。',parent=dialog);return
        if state['ai_mode']=='pending':
            messagebox.showwarning('核查AI评分','请先选择“通过”或“修改”。',parent=dialog);return
        index.set(max(1,min(25,index.get()+delta)));state['generation']+=1;render()
    def save(*,assisted=None,auto_next=False):
        row=state['loaded']
        if not row:return
        if state['saving']:status.set('正在保存，请等待结果');return
        if assisted is None and state['ai_mode']=='editing':
            assisted='modified'
        if state['ai_mode']=='pending' and assisted!='approved':
            messagebox.showwarning('核查AI评分','请先选择“通过”或“修改”。',parent=dialog);return
        # Do not save an old package after changing the target controls.
        if state.get('target')!=(owner.view_epoch,getattr(owner,'persona_id',None),package.get(),batch.get()):
            messagebox.showwarning('目标已变','请重新载入评测包；当前草稿未提交。',parent=dialog);return
        review=draft()
        missing=[label for label,key in [('左侧任务结果','left_task'),('右侧任务结果','right_task'),
                 ('左侧主要交流动作','left_action'),('右侧主要交流动作','right_action'),('自然度','natural')]
                 if not review[key]]
        if not review['reason'].strip():missing.append('评分理由')
        if missing:
            text='请填写：'+'、'.join(missing)
            status.set('未保存；'+text);messagebox.showwarning('评分未填写完整',text,parent=dialog);return
        if len(review['reason'].strip())>2000:
            status.set('未保存；评分理由最多2000字')
            messagebox.showwarning('评分理由过长','评分理由最多2000字，当前草稿仍保留。',parent=dialog);return
        from .archive import fingerprint
        body={'package_id':package.get(),'case_id':row['case_id'],'review':review,'expected_revision':row['revision']}
        ai=state['ai_rows'].get(row['case_id']) or {}
        if assisted:
            body['suggestion_hash']=ai.get('suggestion_hash')
        op=state['operations'].setdefault(fingerprint(body),identifier())
        def done(value):
            row.update(revision=value['revision'],review=review)
            state['baseline']=dict(review)
            state['ai_mode']='saved';editable(True)
            pending=state['pending_move'];state['pending_move']=None
            status.set(f'已保存（修订{value["revision"]}）；'+('新编辑的草稿仍保留' if is_dirty() else '可继续下一组'))
            if (auto_next or pending is not None) and not is_dirty():advance()
        def failed(error):
            state['pending_move']=None
            messages={'evaluation_revision_conflict':'评分版本已变化，请保留草稿并重新载入后核对。',
                      'evaluation_review_frozen':'审阅已经冻结，无法再修改评分。',
                      'evaluation_review_contract':'评分未填写完整或内容格式不符合要求，请核对所有选项及评分理由。',
                      'review_operation_busy':'其他操作仍在进行，本次尚未提交，请稍后再保存。'}
            text=next((text for code,text in messages.items() if code in str(error)),
                      '保存未成功，请检查人格服务连接后重试；当前评分和理由仍保留。')
            status.set('未保存；'+text);messagebox.showerror('保存失败',text,parent=dialog)
        state['saving']=True;state['pending_move']=None;status.set('正在保存，请等待确认')
        action=('evaluation.ai.approve' if assisted=='approved' else
                'evaluation.ai.save-modified' if assisted=='modified' else 'evaluation.review.save')
        call('保存盲审',lambda:owner.client.manage(action,**body,operation_id=op),
             done,failed,lambda:state.update(saving=False))
    def advance():
        if index.get()<25:
            move(1);return
        number=int(batch.get())
        if number<4:
            batch.set(str(number+1));load();return
        status.set('第4批第25/25组已保存；100组完成后可冻结并解盲')
    def approve_ai():
        if state['ai_mode']!='pending':
            messagebox.showwarning('没有待核查建议','当前组没有待核查的DeepSeek建议。',parent=dialog);return
        save(assisted='approved',auto_next=True)
    def modify_ai():
        if state['saving']:
            status.set('正在保存，请等待结果');return
        if state['ai_mode']!='pending':
            messagebox.showwarning('没有待核查建议','当前组没有待核查的DeepSeek建议。',parent=dialog);return
        state['ai_mode']='editing';editable(True)
        status.set(f'第{batch.get()}批，第{index.get()}/25组；请修改后点击“保存”，再点击“下一组”')
    def export():
        if state.get('target')!=(owner.view_epoch,getattr(owner,'persona_id',None),package.get(),batch.get()):
            messagebox.showwarning('目标已变','请重新载入评测包。',parent=dialog);return
        if state['saving']:status.set('正在保存，请等待结果后再冻结');return
        if is_dirty():messagebox.showwarning('保留草稿','请先保存当前审阅。',parent=dialog);return
        if not messagebox.askyesno('冻结并解盲','只有100组全部保存才能冻结，冻结后不能修改。是否继续？',default='no',parent=dialog):return
        pid=package.get()
        def done(value):
            status.set('100组已冻结并解盲')
            source.configure(state='normal');source.delete('1.0','end');source.insert('1.0',str(value));source.configure(state='disabled')
        call('冻结审阅',lambda:owner.client.manage('evaluation.review.export',package_id=pid),done)
    ttk.Button(bar,text='载入',command=load).pack(side='left')
    controls=ttk.Frame(dialog);controls.pack(fill='x')
    for label,fn in [('上一组',lambda:move(-1)),('通过',approve_ai),('修改',modify_ai),
                     ('保存',save),('下一组',lambda:move(1)),('冻结并解盲',export)]:
        ttk.Button(controls,text=label,command=fn).pack(side='left')
    def close():
        if state['saving']:status.set('正在保存，请等待结果后再关闭');return
        if is_dirty() and not messagebox.askyesno('保留草稿','关闭将放弃未保存审阅，是否继续？',default='no',parent=dialog):return
        dialog.destroy()
    dialog.protocol('WM_DELETE_WINDOW',close)
    return dialog
