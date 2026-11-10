"""Visible app status and explicit reminders; normal refresh never creates work."""
import tkinter as tk
from tkinter import ttk,messagebox
from .models import identifier


def connection_status_text(states):
    native=states.get('napcat',{});bridge=states.get('astrbot',{})
    logged_in=native.get('account_logged_in')
    reason=native.get('login_reason')
    login=('已登录' if logged_in is True else '登录已失效，请重新登录' if reason=='session_expired'
        else '账号不符，请核对' if reason=='account_mismatch' else '客户端类型不符' if reason=='backend_mismatch'
        else '等待登录' if logged_in is False else '状态待核对')
    connected=bridge.get('onebot_connected')
    online=False if logged_in is False else bridge.get('account_online')
    label=lambda value,yes,no:yes if value is True else no if value is False else '状态待核对'
    prefix = 'SnowLuma（独立运行）　' if native.get('backend') == 'snowluma' else ''
    return prefix+'QQ 登录：'+login+'　OneBot：'+label(connected,'已连接','未连接')+'　账号在线：'+label(online,'在线','离线')


class RuntimeUI:
    def init_runtime_vars(self):
        self.runtime_summary=tk.StringVar(value='收起到托盘继续运行；退出软件释放所属组件。SnowLuma模式关闭PCAR连接，外部会话独立管理。')
        self.proactive_enabled=tk.BooleanVar(value=False);self.proactive_policy=None;self.proactive_rows={}
        self.rule_kind=tk.StringVar(value='一次');self.rule_mode=tk.StringVar(value='原文')
        self.rule_at=tk.StringVar();self.rule_time=tk.StringVar(value='09:00')

    def build_runtime(self,parent):
        self.heading(parent,'运行与约定提醒','提醒默认关闭，只执行你明确创建的约定。模型生成共用自动调用额度。')
        ttk.Label(parent,textvariable=self.runtime_summary,wraplength=720).pack(anchor='w')
        bar=self.bar(parent);bar.pack(fill='x')
        self.button(bar,'读取状态',self.refresh_runtime,{'runtime-read'})
        ttk.Checkbutton(parent,text='开启当前角色的约定提醒',variable=self.proactive_enabled).pack(anchor='w')
        self.button(bar,'保存开关',self.save_proactive_policy,{'runtime-write'})
        self.button(bar,'授权模型提醒',self.authorize_proactive,{'runtime-write'})
        ttk.Label(parent,text='北京时间 23:00—08:00 静默，每天最多两条，至少间隔一小时；错过不补发。').pack(anchor='w',pady=8)
        form=ttk.Frame(parent);form.pack(fill='x')
        ttk.Combobox(form,textvariable=self.rule_kind,values=('一次','每天'),state='readonly',width=7).pack(side='left')
        ttk.Combobox(form,textvariable=self.rule_mode,values=('原文','模型生成'),state='readonly',width=9).pack(side='left')
        ttk.Label(form,text='一次性日期时间').pack(side='left')
        ttk.Entry(form,textvariable=self.rule_at,width=19).pack(side='left')
        ttk.Label(form,text='每日时刻').pack(side='left');ttk.Entry(form,textvariable=self.rule_time,width=7).pack(side='left')
        ttk.Label(parent,text='一次性时间示例：2026-11-09 09:00；每日时刻示例：09:00。均按北京时间。').pack(anchor='w')
        self.rule_content=tk.Text(parent,height=4,wrap='word');self.rule_content.pack(fill='x')
        bar=self.bar(parent);bar.pack(fill='x')
        self.button(bar,'创建约定',self.create_proactive_rule,{'runtime-write'})
        self.button(bar,'停用选中约定',self.disable_proactive_rule,{'runtime-write'})
        self.rule_tree=ttk.Treeview(parent,columns=('kind','mode','enabled'),show='headings',height=5)
        for key,label in [('kind','执行方式'),('mode','内容方式'),('enabled','开启状态')]:self.rule_tree.heading(key,text=label)
        self.rule_tree.pack(fill='x',pady=8)
        self.runtime_details=tk.Text(parent,height=12,wrap='word',state='disabled');self.runtime_details.pack(fill='both',expand=True)

    def refresh_runtime(self):
        pid=self.persona_id
        if not pid:return
        def read(_):return (self.client.manage('runtime.current'),self.client.manage('proactive.policy.current',persona_id=pid),
            self.client.manage('proactive.rule.list',persona_id=pid),self.client.manage('proactive.occurrence.list',persona_id=pid),self.client.manage('quota.current'))
        def done(value):
            if self.persona_id!=pid:return
            runtime,policy,rules,occurrences,quota=value;self.proactive_policy=policy
            self.proactive_enabled.set(policy['data']['enabled']);self.proactive_rows={r['rule_id']:r for r in rules}
            self.rule_tree.delete(*self.rule_tree.get_children())
            for row in rules:self.rule_tree.insert('', 'end',iid=row['rule_id'],values=(
                '一次' if row['data']['kind']=='once' else '每天','原文' if row['data']['mode']=='fixed' else '模型生成','开启' if row['data']['enabled'] else '关闭'))
            disk=runtime.get('disk') or {};exit=runtime.get('last_exit') or {}
            exit_label = ('；上次连接释放：'+('已确认' if exit.get('processes_released') else '未确认或尚无记录')
                          if exit.get('external_qq_session') else '；上次注销：'+('已确认' if exit.get('logout_confirmed') else '未确认或尚无记录'))
            self.runtime_summary.set('工作台绑定运行；磁盘：'+('暂停新增调用和发送' if disk.get('blocked') else '正常')+exit_label)
            lines=[name+'：'+state.get('state','等待核对') for name,state in (runtime.get('health') or {}).get('states',{}).items()]
            lines.append(connection_status_text((runtime.get('health') or {}).get('states',{})))
            used=quota.get('used') or {};limits=quota['limits']
            lines.append(f"自动后台调用：本小时 {used.get('background_hourly','?')}/{limits['background_hourly']}，滚动24小时 {used.get('background_daily','?')}/{limits['background_daily']}")
            lines.append('最近约定执行：'+str(len(occurrences))+' 项；未知结果不会自动重发。')
            reasons={'missed_while_closed':'软件关闭期间错过，已过期','expired':'已超过约定时限','quiet_hours':'静默时段，已跳过',
                'frequency_limit':'已达到发送频率限制','waiting_quota':'等待自动调用额度','blocked_unknown':'调用效果未知，禁止重发',
                'queued':'等待执行','completed':'生成完成','pending':'等待发送','sent':'已确认送达','uncertain':'可能已送达，等待核对','skipped':'已隔离或跳过'}
            for occurrence in occurrences[:8]:
                task=occurrence.get('task') or {};parts=occurrence.get('delivery') or []
                reason=occurrence['decision'].get('reason') or task.get('error_code') or task.get('status') or occurrence['state']
                if parts:reason='、'.join(dict.fromkeys(reasons.get(p['state'],p['state']) for p in parts))
                lines.append('约定 '+occurrence['rule_id'][:8]+'：'+reasons.get(reason,reason))
            self.runtime_details.configure(state='normal');self.runtime_details.delete('1.0','end')
            self.runtime_details.insert('1.0','\n'.join(lines));self.runtime_details.configure(state='disabled')
        self.run_task('读取运行与约定',{'runtime-read'},read,done)

    def save_proactive_policy(self):
        if not self.proactive_policy or not self.persona_id:return
        pid=self.persona_id;version=self.proactive_policy['revision'];enabled=self.proactive_enabled.get()
        self.run_task('保存约定开关',{'runtime-write'},lambda _:self.client.manage('proactive.policy.update',persona_id=pid,
            expected_selected=pid,expected_version=version,operation_id=identifier(),data={'enabled':enabled}),lambda _:self.refresh_runtime())

    def authorize_proactive(self):
        if not self.proactive_policy or not self.persona_id:return
        if not messagebox.askyesno('授权当前角色','允许将当前角色可见资料及你创建的约定发送给已配置模型，生成提醒或问候？聊天和整理授权不会替代这项授权。'):return
        pid=self.persona_id;provider=self.proactive_policy['data']['provider']
        self.run_task('授权模型提醒',{'runtime-write'},lambda _:self.client.manage('proactive.authorize',persona_id=pid,
            expected_selected=pid,expected_provider=provider,operation_id=identifier()),lambda _:self.refresh_runtime())

    def create_proactive_rule(self):
        if not self.persona_id:return
        from datetime import datetime,timezone,timedelta
        at=self.rule_at.get()
        if self.rule_kind.get()=='一次':
            try:at=datetime.strptime(at,'%Y-%m-%d %H:%M').replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
            except ValueError:self.notice.set('请按示例填写一次性日期和时间。');return
        pid=self.persona_id;data={'enabled':True,'kind':'once' if self.rule_kind.get()=='一次' else 'daily',
            'mode':'fixed' if self.rule_mode.get()=='原文' else 'model','at':at,'time':self.rule_time.get(),
            'content':self.rule_content.get('1.0','end-1c')}
        self.run_task('创建明确约定',{'runtime-write'},lambda _:self.client.manage('proactive.rule.update',persona_id=pid,
            expected_selected=pid,expected_version=0,operation_id=identifier(),data=data),lambda _:self.refresh_runtime())

    def disable_proactive_rule(self):
        selected=self.rule_tree.selection()
        if not selected or not self.persona_id:return
        row=self.proactive_rows[selected[0]];pid=self.persona_id;data=row['data']|{'enabled':False}
        self.run_task('停用约定',{'runtime-write'},lambda _:self.client.manage('proactive.rule.update',persona_id=pid,
            expected_selected=pid,expected_version=row['revision'],operation_id=identifier(),rule_id=row['rule_id'],data=data),lambda _:self.refresh_runtime())
