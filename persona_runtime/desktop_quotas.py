"""Workbench quota status and a compact editor; no model calls."""
import tkinter as tk
from tkinter import ttk,messagebox

from .quotas import QuotaService
from .storage import Store

LABELS = {'project_hourly': '自动模型调用总额 / 小时', 'background_hourly': '自动整理调用 / 小时', 'background_daily': '自动整理调用 / 24 小时', 'selector_hourly':'自动选择与理解共享调用 / 小时','selector_daily':'自动选择与理解共享调用 / 24 小时'}


class QuotaUI:
    def quota_service(self):
        return QuotaService(Store(self.paths), self.config)

    def build_quotas(self, parent):
        group = ttk.LabelFrame(parent, text='调用额度', padding=10)
        group.pack(fill='x', pady=10)
        self.quota_status = tk.StringVar(value='正在读取额度…')
        ttk.Label(group, textvariable=self.quota_status, wraplength=720).pack(anchor='w')
        bar = self.bar(group); bar.pack(fill='x')
        self.button(bar, '修改调用额度', self.edit_quotas, {'config'})
        self.button(bar, '查看等待额度的任务', lambda:self.prompt_quota_waits(True), {'quota-decision'})
        self.button(bar, '开发阶段不限次数', self.begin_development_quotas, {'config'})
        self.button(bar, '恢复原调用额度', self.restore_acceptance_quotas, {'config'})
        self.refresh_quotas()

    def refresh_quotas(self):
        def done(value):
            lines = []
            policy=value['call_limits']
            if policy['enabled']:
                for key,label in (('hourly','小时'),('daily','24 小时')):
                    if policy[key]:
                        count=(value['used'] or {}).get('global_'+key,0)
                        lines.append(f'全部模型调用 / {label}：已用 {count} / {policy[key]}')
            for key, label in LABELS.items():
                limit = value['limits'][key]
                used_value=(value['used'][key]+value['used'].get('compiler_'+key.removeprefix('selector_'),0)+value['used'].get('understanding_'+key.removeprefix('selector_'),0)) if value['used'] is not None and key.startswith('selector_') else value['used'][key] if value['used'] is not None else None
                used = str(used_value) if used_value is not None else '未知'
                lines.append(f'{label}：已用 {used} / {limit if limit else "不限额"}')
            if value['used'] is not None:
                lines.append('小时内已记录人工调用 '+str(value['used'].get('manual_hourly',0))+' 次。全部调用限额可在新版工作台的高级设置中修改。')
                lines.append('其中理解：小时 '+str(value['used'].get('understanding_hourly',0))+' / 日 '+str(value['used'].get('understanding_daily',0)))
                lines.append(f"主回复已调用：{value['used']['primary_hourly']}；未消费的回复预留：{value['used']['reserved']}")
            if value.get('development_unlimited'):
                lines.append('开发阶段：自动调用不设次数上限，调用和用量继续逐笔记录。')
            elif value['acceptance_active']:
                lines.append('机器验收临时不限额；结束后恢复保存的额度。')
            self.quota_status.set('\n'.join(lines))
            self.quota_waits=value.get('waiting_workflows',[])
            if any(r['state']=='waiting' for r in self.quota_waits):self.after_idle(self.prompt_quota_waits)
        self.run_task('读取调用额度', {'quota-read'}, lambda _: self.quota_service().current(), done, quiet=True)

    def prompt_quota_waits(self,manual=False):
        waits=getattr(self,'quota_waits',[])
        if not waits:
            if manual:self.notice.set('目前没有等待额度的任务。')
            return
        if getattr(self,'quota_prompt_open',False) or self.closed or self.shutdown_started or self.tray_hidden:return
        if not manual and not any(r['state']=='waiting' for r in waits):return
        self.quota_prompt_open=True
        ids=[r['id'] for r in waits]
        def finished(_):
            self.quota_prompt_open=False
            self.quota_waits=[]
            self.refresh_quotas()
        try:
            allow=messagebox.askyesno('已达到模型调用限额',f'当前有 {len(ids)} 项工作因本机限额暂停。是否临时放行本轮消息及这些工作流？选择否将继续等待，额度恢复后自动继续。',default='no',parent=self)
            if not self.run_task('记录额度选择',{'quota-decision'},
                    lambda _:self.client.manage('quota.workflow.decide',ids=ids,allow=allow),finished,failure=finished):
                self.quota_prompt_open=False
        except tk.TclError:self.quota_prompt_open=False

    def edit_quotas(self):
        settings=self.quota_service().desktop_settings()
        initial=settings['automatic']|{k:settings[k] for k in ('hourly','daily')}
        dialog, body, footer = self.dialog_shell('模型调用限额', (550, 510))
        enabled=tk.BooleanVar(value=settings['enabled'])
        ttk.Checkbutton(body,text='启用调用限额',variable=enabled).grid(row=0,column=0,columnspan=2,sticky='w')
        labels={'hourly':'全部模型调用 / 小时','daily':'全部模型调用 / 24 小时'}|LABELS
        variables = {key: tk.StringVar(value=str(initial[key])) for key in labels}
        for row, (key, label) in enumerate(labels.items(),start=1):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', pady=6)
            ttk.Entry(body, textvariable=variables[key], width=12).grid(row=row, column=1, sticky='ew', padx=10)
        hint = tk.StringVar(value='0 表示该项不限额。' + (' 开发或验收模式结束后使用保存的额度。' if settings['temporary_unlimited'] else ''))
        ttk.Label(body, textvariable=hint, wraplength=490).grid(row=len(labels)+1, column=0, columnspan=2, sticky='w', pady=8)
        def save():
            try:
                limits = {key: int(variable.get() or 0) for key, variable in variables.items()}
            except ValueError:
                hint.set('请填写非负整数。'); return
            choice=enabled.get();hourly=limits.pop('hourly');daily=limits.pop('daily')
            def done(_):
                if dialog.winfo_exists(): dialog.destroy()
                self.load_config_fields(); self.refresh_quotas()
            self.run_task('保存调用额度', {'config'}, lambda _: self.quota_service().configure(choice,hourly,daily,limits), done,
                          failure=lambda error: hint.set(str(error)) if dialog.winfo_exists() else None)
        ttk.Button(footer, text='取消', command=dialog.destroy).grid(row=0, column=0, padx=5)
        ttk.Button(footer, text='保存额度', command=save).grid(row=0, column=1)

    def begin_development_quotas(self):
        self.run_task('启用开发调用模式',{'config'},lambda _:self.quota_service().begin_development(),
                      lambda _:(self.load_config_fields(),self.refresh_quotas()))

    def restore_acceptance_quotas(self):
        self.run_task('恢复原调用额度', {'config'}, lambda _: self.quota_service().end_acceptance(),
                      lambda _: (self.load_config_fields(), self.refresh_quotas()))
