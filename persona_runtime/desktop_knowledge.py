"""One small, nontechnical retrieval panel; old review controls are retired."""
import tkinter as tk
from tkinter import ttk,scrolledtext
class KnowledgeUI:
    def init_knowledge_vars(self):
        self.knowledge_read=tk.BooleanVar(value=False);self.knowledge_data={};self.knowledge_loaded_key=None
        self.knowledge_status=tk.StringVar(value='连接服务后可读取当前设置。')
        self.chat_public_task=None
    def build_knowledge(self,parent):
        ttk.Label(parent,text='联网与资料',font=('Microsoft YaHei UI',18,'bold')).pack(anchor='w',padx=16,pady=18)
        ttk.Label(parent,text='按需查找公开资料，再由人物结合眼前的聊天回应。已有资料可以继续讨论。',wraplength=760).pack(anchor='w',padx=16,pady=8)
        ttk.Label(parent,text='官网不是裁判，热度也不是结论；会兼顾官方材料、社区经验和不同观点，不用凑齐“权威证明”才交流。',wraplength=760).pack(anchor='w',padx=16,pady=8)
        line=ttk.Frame(parent);line.pack(anchor='w',padx=16,pady=16)
        ttk.Checkbutton(line,text='当前角色按需联网检索',variable=self.knowledge_read).pack(side='left')
        self.button(line,'保存设置',self.save_knowledge_reader,{'knowledge-write'})
        self.button(line,'刷新',self.refresh_knowledge,{'knowledge-read'})
        ttk.Label(parent,text='联网使用深度思考搜索，保留原人物回应。聊天页“公开检索”可填写本次公开问题。',wraplength=760).pack(anchor='w',padx=16,pady=8)
        ttk.Label(parent,textvariable=self.knowledge_status,wraplength=760).pack(anchor='w',padx=16,pady=12)
    def knowledge_key(self):return self.persona_id,self.view_epoch
    def knowledge_ready(self):return bool(self.persona_id and self.knowledge_loaded_key==self.knowledge_key() and self.knowledge_data)
    def refresh_knowledge(self):
        if not self.persona_id:return
        key=self.knowledge_key()
        def done(data):
            if key!=self.knowledge_key():return
            self.knowledge_data=data;self.knowledge_loaded_key=key;self.knowledge_read.set(data['policy']['data']['enabled'])
            cost=data['cost'];tools=data['tool_cost']
            decision=data.get('web_decision',{});ready=decision.get('configuration',{}).get('key_configured') and decision.get('authorization',{}).get('authorized')
            fallback='上轮Jev未取得有效判断，已按策略联网继续。' if self.knowledge_read.get() and decision.get('latest',{}).get('decision_source')=='policy_fallback' else ''
            self.knowledge_status.set(f"通用检索{'已启用' if self.knowledge_read.get() else '未启用'}。Jev配置及授权{'已就绪' if ready else '未就绪'}。{fallback}知识模型调用{cost['calls']}次（含Jev），搜索/读取{tools['calls']}次；费用按记录留账。")
        self.run_task('读取联网设置',{'knowledge-read'},lambda _:self.client.manage('knowledge.manage.show',persona_id=key[0]),done,quiet=True)
    def save_knowledge_reader(self):
        if not self.knowledge_ready():return
        from .models import identifier
        data=self.knowledge_data;key=self.knowledge_key()
        args={'persona_id':self.persona_id,'enabled':self.knowledge_read.get(),'expected_reader_revision':data['policy']['revision'],
            'expected_processing_revision':data['processing_policy']['revision'],'expected_web_revision':data['web_policy']['revision'],'operation_id':identifier()}
        self.run_task('保存联网设置',{'knowledge-write'},lambda _:self.client.manage('knowledge.general.update',**args),lambda _:self.refresh_knowledge() if key==self.knowledge_key() else None)

    def edit_public_search(self):
        if not self.persona_id:
            self.notice.set('请先选择人物。');return
        pid=self.persona_id;sid=self.session_id;epoch=self.view_epoch
        dialog,body,footer=self.dialog_shell('本次公开检索',(690,360))
        ttk.Label(body,text='只把这里的问题用于查找。可以填写公开主题或粘贴网页链接，聊天中的其他内容不随查询发送。',wraplength=630).pack(anchor='w',pady=8)
        query=scrolledtext.ScrolledText(body,height=7,wrap='word');query.pack(fill='both',expand=True)
        current=getattr(self,'chat_public_task',None)
        if current and (current['persona_id'],current['session_id'])==(pid,sid):query.insert('1.0',current['task']['question'])
        ttk.Label(body,text='联网时使用深度思考搜索，查找前会先自然回应你。',wraplength=630).pack(anchor='w',pady=8)
        retain=tk.BooleanVar(value=bool(current and current['task'].get('retain_page')))
        ttk.Checkbutton(body,text='保留指定网页的可读正文，供之后讨论',variable=retain).pack(anchor='w',pady=4)
        def save():
            if (pid,sid,epoch)!=(self.persona_id,self.session_id,self.view_epoch):
                self.notice.set('人物或会话已改变，请重新设置本次公开问题。');dialog.destroy();return
            text=query.get('1.0','end-1c').strip()
            if not text or len(text)>4000:
                self.notice.set('请填写不超过4000字的公开问题。');return
            task={'question':text,'depth':'deep','intent':'search','retain_page':retain.get()}
            if text.startswith(('https://','http://')) and not any(char.isspace() for char in text):
                task.update(intent='read_link',url=text)
            if retain.get() and task['intent']!='read_link':
                self.notice.set('保留正文时，请单独填写一个公开网页链接。');return
            from .knowledge_native import public_task
            try:task=public_task(task)
            except Exception:
                self.notice.set('请填写可访问的公开网页链接。');return
            self.chat_public_task={'persona_id':pid,'session_id':sid,'task':task}
            self.notice.set('已为下一条消息设置公开问题：'+text[:70]);dialog.destroy()
        def clear():
            self.chat_public_task=None;self.notice.set('已清除本次公开问题。');dialog.destroy()
        ttk.Button(footer,text='用于下一条消息',command=save).pack(side='right',padx=5)
        ttk.Button(footer,text='清除',command=clear).pack(side='right',padx=5)
        ttk.Button(footer,text='关闭',command=dialog.destroy).pack(side='right',padx=5)

    def show_current_materials(self):
        if not self.persona_id:return
        turn_id=next((row['id'] for row in reversed(self.turns) if row['status']=='completed'),None)
        if not turn_id:
            self.notice.set('还没有可展开资料的已完成回复。');return
        pid=self.persona_id;epoch=self.view_epoch
        def done(value):
            if (pid,epoch)!=(self.persona_id,self.view_epoch):return
            dialog,body,footer=self.dialog_shell('本轮资料',(760,540))
            output=scrolledtext.ScrolledText(body,wrap='word');output.pack(fill='both',expand=True)
            rows=value.get('evidence',[])
            text='\n\n'.join((str(row.get('article_title') or '公开资料')+'\n'+str(row.get('url') or '')+'\n'+str(row.get('quote') or '')) for row in rows)
            if value.get('notes'):text+='\n\n工作摘要（模型归纳）：\n'+value['notes']
            output.insert('1.0',text or '本轮没有新增可用的外部资料。');output.configure(state='disabled')
            ttk.Button(footer,text='关闭',command=dialog.destroy).pack(side='right',padx=5)
        self.run_task('展开本轮资料',{'knowledge-read'},lambda _:self.client.manage('knowledge.evidence.show',persona_id=pid,turn_id=turn_id),done)
