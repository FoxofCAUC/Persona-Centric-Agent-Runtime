"""Workbench quota status and a compact editor; no model calls."""
import tkinter as tk
from tkinter import ttk

from .quotas import QuotaService
from .storage import Store

LABELS = {'project_hourly': '全部模型调用 / 小时', 'background_hourly': '后台整理调用 / 小时', 'background_daily': '后台整理调用 / 24 小时'}


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
        self.button(bar, '恢复验收前额度', self.restore_acceptance_quotas, {'config'})
        self.refresh_quotas()

    def refresh_quotas(self):
        def done(value):
            lines = []
            for key, label in LABELS.items():
                limit = value['limits'][key]
                used = str(value['used'][key]) if value['used'] is not None else '未知'
                lines.append(f'{label}：已用 {used} / {limit if limit else "不限额"}')
            if value['acceptance_active']:
                lines.append('机器验收临时不限额；结束后恢复保存的额度。')
            self.quota_status.set('\n'.join(lines))
        self.run_task('读取调用额度', {'quota-read'}, lambda _: self.quota_service().current(), done, quiet=True)

    def edit_quotas(self):
        value = self.quota_service().current()
        initial = value['restore_limits'] if value['acceptance_active'] else value['limits']
        dialog, body, footer = self.dialog_shell('修改调用额度', (550, 300))
        variables = {key: tk.StringVar(value=str(initial[key])) for key in LABELS}
        for row, (key, label) in enumerate(LABELS.items()):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', pady=6)
            ttk.Entry(body, textvariable=variables[key], width=12).grid(row=row, column=1, sticky='ew', padx=10)
        hint = tk.StringVar(value='0 表示不限额；范围 0–10000。' + (' 验收中保存的值将在结束验收时恢复。' if value['acceptance_active'] else ''))
        ttk.Label(body, textvariable=hint, wraplength=490).grid(row=3, column=0, columnspan=2, sticky='w', pady=8)
        def save():
            try:
                limits = {key: int(variable.get()) for key, variable in variables.items()}
            except ValueError:
                hint.set('请填写 0–10000 的整数，0 表示不限额。'); return
            def done(_):
                if dialog.winfo_exists(): dialog.destroy()
                self.load_config_fields(); self.refresh_quotas()
            self.run_task('保存调用额度', {'config'}, lambda _: self.quota_service().update(limits), done,
                          failure=lambda error: hint.set(str(error)) if dialog.winfo_exists() else None)
        ttk.Button(footer, text='取消', command=dialog.destroy).grid(row=0, column=0, padx=5)
        ttk.Button(footer, text='保存额度', command=save).grid(row=0, column=1)

    def restore_acceptance_quotas(self):
        self.run_task('恢复验收前额度', {'config'}, lambda _: self.quota_service().end_acceptance(),
                      lambda _: (self.load_config_fields(), self.refresh_quotas()))
