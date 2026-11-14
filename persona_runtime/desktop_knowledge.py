"""One small, nontechnical retrieval panel; old review controls are retired."""
import tkinter as tk
from tkinter import ttk
class KnowledgeUI:
    def init_knowledge_vars(self):
        self.knowledge_read=tk.BooleanVar(value=False);self.knowledge_data={};self.knowledge_loaded_key=None
        self.knowledge_status=tk.StringVar(value='连接服务后可读取当前设置。')
    def build_knowledge(self,parent):
        ttk.Label(parent,text='联网与资料',font=('Microsoft YaHei UI',18,'bold')).pack(anchor='w',padx=16,pady=18)
        ttk.Label(parent,text='Jev判断是否联网，High负责检索与整理，再由人物按语境转译回应。',wraplength=760).pack(anchor='w',padx=16,pady=8)
        ttk.Label(parent,text='官网不是裁判，热度也不是结论；会兼顾官方材料、社区经验和不同观点，不用凑齐“权威证明”才交流。',wraplength=760).pack(anchor='w',padx=16,pady=8)
        line=ttk.Frame(parent);line.pack(anchor='w',padx=16,pady=16)
        ttk.Checkbutton(line,text='当前角色按需联网检索',variable=self.knowledge_read).pack(side='left')
        self.button(line,'保存设置',self.save_knowledge_reader,{'knowledge-write'})
        self.button(line,'刷新',self.refresh_knowledge,{'knowledge-read'})
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
