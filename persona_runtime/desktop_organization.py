"""Tk workbench extension for M4. All service calls use the background task hub."""
import json
import tkinter as tk
from tkinter import ttk, messagebox

from .models import identifier

STATES = {'queued': '排队', 'waiting_quota': '等待额度', 'running': '处理中', 'completed': '完成',
          'failed': '失败', 'interrupted': '中断', 'cancelled': '取消', 'stale': '来源已过期',
          'pending': '待审阅', 'accepted': '已生效', 'rejected': '已拒绝', 'active': '有效',
          'superseded': '已替代', 'disabled': '已停用', 'scheduled': '等待每日计划',
          'awaiting_review': '冲突待授权', 'blocked': '异常待处理', 'paused_model': '模型变更后暂停', 'paused': '已暂停'}


class OrganizationUI:
    def init_organization_vars(self):
        self.org_model_vars = {key: tk.StringVar() for key in ('base_url','name','api_key','timeout','max_tokens','max_input_chars')}
        self.org_hour = tk.StringVar(value='6')
        self.org_day = tk.StringVar(value='20')
        self.org_reuse = tk.BooleanVar(value=False)
        self.org_clear_key = tk.BooleanVar(value=False)
        self.daily_enabled = tk.BooleanVar(value=True)
        self.org_session = tk.StringVar()
        self.org_mode = tk.StringVar(value='摘要与候选')
        self.org_start = tk.StringVar()
        self.org_end = tk.StringVar()
        self.org_rebuild = tk.BooleanVar(value=False)
        self.org_state_text = tk.StringVar(value='每日更新：连接人格服务后读取下次执行时间。')
        self.org_preview_text = tk.StringVar(value='先选择会话并预览，预览不调用模型。')
        self.org_preview = None
        self.org_records = {}
        self.org_lists = {}
        self.org_session_rows = []
        self.org_preview_generation = 0
        for variable in (self.org_session, self.org_mode, self.org_start, self.org_end, self.org_rebuild):
            variable.trace_add('write', lambda *_: self.invalidate_org_preview())

    def build_organization(self, parent):
        self.heading(parent, '记忆整理', '手动结果须审阅；每日全量更新正常结果自动生效，仅冲突待授权。旧来源屏蔽后仍保留原文历史。')
        ttk.Label(parent, textvariable=self.org_state_text, wraplength=720).pack(anchor='w', pady=8)
        bar = self.bar(parent)
        bar.pack(fill='x')
        self.button(bar, '刷新整理状态', self.refresh_organization, {'org-read'})
        self.button(bar, '每日更新详情与屏蔽来源', lambda: self.org_call('organization.status', title='每日计划、进度与屏蔽来源'), {'org-read'})
        self.button(bar, '继续受阻的每日更新', self.resume_daily_update, {'org-write'})
        self.button(bar, '立即执行一次全量更新', self.run_daily_now, {'org-write'})
        bar = self.bar(parent)
        bar.pack(fill='x')
        self.button(bar, '开启当前人格空闲整理', lambda: self.toggle_idle_organization(True), {'org-write'})
        self.button(bar, '关闭当前人格空闲整理', lambda: self.toggle_idle_organization(False), {'org-write'})
        ttk.Label(parent, text='每日更新仅覆盖开始时选中的人格；24 小时一次，额度不足自动等待。空闲整理是独立的可选增量功能。', wraplength=700).pack(anchor='w', pady=8)
        self.org_session_box = ttk.Combobox(parent, textvariable=self.org_session, state='readonly')
        self.org_session_box.pack(fill='x', pady=5)
        self.org_session_box.bind('<<ComboboxSelected>>', lambda _: self.invalidate_org_preview())
        ttk.Combobox(parent, textvariable=self.org_mode, state='readonly', values=('摘要与候选','仅摘要','仅候选')).pack(fill='x', pady=5)
        bounds = ttk.Frame(parent)
        bounds.pack(fill='x', pady=4)
        ttk.Label(bounds, text='来源轮次序号起点（可空）：').pack(side='left')
        ttk.Entry(bounds, textvariable=self.org_start, width=9).pack(side='left')
        ttk.Label(bounds, text='终点（可空）：').pack(side='left')
        ttk.Entry(bounds, textvariable=self.org_end, width=9).pack(side='left')
        ttk.Checkbutton(parent, text='从所选起点重建摘要（新结果成功前保留旧摘要）', variable=self.org_rebuild).pack(anchor='w')
        bar = self.bar(parent)
        bar.pack(fill='x')
        self.button(bar, '预览整理范围', self.preview_organization, {'org-preview'})
        self.button(bar, '提交本批整理（可能计费）', self.submit_organization, {'org-write'}, True)
        self.button(bar, '继续下一批（先预览）', self.next_organization_batch, {'org-preview'})
        ttk.Label(parent, textvariable=self.org_preview_text, wraplength=700).pack(anchor='w', pady=6)
        tabs = ttk.Notebook(parent)
        tabs.pack(fill='both', expand=True, pady=8)
        for kind, label in (('tasks','任务'),('candidates','记忆候选'),('summaries','会话摘要')):
            frame = ttk.Frame(tabs, padding=6)
            tabs.add(frame, text=label)
            listing = tk.Listbox(frame, width=1, height=7, exportselection=False)
            listing.pack(fill='both', expand=True)
            self.org_lists[kind] = listing
            self.org_records[kind] = []
            bar = self.bar(frame)
            bar.pack(fill='x')
            self.button(bar, '查看详情与来源', lambda k=kind: self.org_selected_action(k, 'show'), {'org-details'})
            if kind == 'tasks':
                self.button(bar, '显式重试（可能计费）', lambda: self.org_selected_action('tasks','retry'), {'org-write'})
                self.button(bar, '取消排队任务', lambda: self.org_selected_action('tasks','cancel'), {'org-write'})
                self.button(bar, '跳过受阻增量范围', lambda: self.org_selected_action('tasks','skip'), {'org-write'})
            elif kind == 'candidates':
                self.button(bar, '编辑后确认新记忆', self.review_candidate, {'org-review'})
                self.button(bar, '修订已有记忆', lambda: self.review_candidate(True), {'org-review'})
                self.button(bar, '拒绝并屏蔽来源', lambda: self.org_selected_action('candidates','reject'), {'org-write'})
            else:
                self.button(bar, '停用摘要', lambda: self.org_selected_action('summaries','disable'), {'org-write'})

    def build_organization_model(self, parent):
        group = ttk.LabelFrame(parent, text='记忆整理模型与费用', padding=12)
        group.pack(fill='x', pady=12)
        ttk.Checkbutton(group, text='明确复用聊天模型（不复制或显示密钥）', variable=self.org_reuse).pack(anchor='w')
        self.fields(group, self.org_model_vars, (('base_url','整理接口地址'),('name','整理模型名'),('api_key','新整理密钥'),
                    ('timeout','超时秒数'),('max_tokens','输出 token 上限'),('max_input_chars','输入字符上限')), secret='api_key')
        ttk.Checkbutton(group, text='明确清除整理模型密钥', variable=self.org_clear_key).pack(anchor='w')
        limits = ttk.Frame(group)
        limits.pack(fill='x', pady=5)
        ttk.Label(limits, text='后台滚动小时上限：').pack(side='left')
        ttk.Entry(limits, textvariable=self.org_hour, width=7).pack(side='left')
        ttk.Label(limits, text='后台滚动24小时上限：').pack(side='left')
        ttk.Entry(limits, textvariable=self.org_day, width=7).pack(side='left')
        ttk.Checkbutton(group, text='每24小时全量自动更新当前人格，正常记忆自动生效，冲突待授权', variable=self.daily_enabled).pack(anchor='w', pady=5)
        bar = self.bar(group)
        bar.pack(fill='x')
        self.button(bar, '保存整理配置', self.save_organization_model, {'config'})
        self.button(bar, '测试整理模型（可能计费）', self.test_organization_model, {'org-write'})
        ttk.Label(group, text='保存不调用模型。每日首次执行在服务初始化24小时后；下次本机时间在记忆整理页显示。所有调用仍计入全项目小时上限。', wraplength=650).pack(anchor='w', pady=8)

    def load_organization_fields(self, value):
        org = value['organization']
        self.org_reuse.set(org['reuse_chat'])
        self.daily_enabled.set(org['daily_enabled'])
        self.org_hour.set(str(org['hourly_limit']))
        self.org_day.set(str(org['daily_limit']))
        for key, variable in self.org_model_vars.items():
            if key != 'api_key':
                variable.set(str(org['model'][key]))

    def save_organization_model(self):
        model = {k: v.get().strip() for k,v in self.org_model_vars.items()}
        try:
            for field in ('timeout','max_tokens','max_input_chars'):
                model[field] = int(model[field])
            settings = {'reuse_chat': self.org_reuse.get(), 'daily_enabled': self.daily_enabled.get(),
                        'hourly_limit': int(self.org_hour.get()), 'daily_limit': int(self.org_day.get())}
        except ValueError:
            self.notice.set('整理额度和限制须填写整数。')
            return
        if not model['api_key'] and not self.org_clear_key.get():
            model.pop('api_key')
        settings['model'] = model
        def saved(_):
            self.org_model_vars['api_key'].set('')
            self.org_clear_key.set(False)
            self.load_config_fields()
            self.notice.set('整理配置已保存，未调用模型；变更模型会暂停已开启的空闲整理。')
        self.run_task('保存整理配置', {'config'}, lambda _: self.config.update({'organization': settings}), saved)

    def test_organization_model(self):
        if messagebox.askyesno('整理模型测试', '将使用已保存配置请求一次模型，计入后台和全项目额度。继续？'):
            self.org_call('organization.model-test', title='整理模型测试任务')

    def org_call(self, action, title=None, **args):
        self.run_task('记忆整理操作', {'org-write' if title is None else 'org-details'}, lambda _: self.client.manage(action, **args),
                      (lambda value: self.show_json(title, value)) if title else (lambda _: self.refresh_organization()))

    def refresh_organization(self):
        epoch = self.view_epoch
        def read(_):
            return {'status': self.client.manage('organization.status'), 'sessions': self.client.manage('organization.sessions'),
                    'tasks': self.client.manage('organization.list'), 'candidates': self.client.manage('candidate.list'), 'summaries': self.client.manage('summary.list')}
        def done(value):
            if epoch != self.view_epoch:
                return
            status = value['status']
            progress = status['progress']
            state = STATES.get(status['status'], status['status']) if status['enabled'] else '每日更新已关闭'
            self.org_state_text.set(f"每日更新：{state}；下次本机时间：{status['next_local_time']}\n"
                                    f"已完成批次：{progress.get('completed_batches',0)}；剩余有效轮次：{status['remaining_turns']}/{status['total_turns']}；空闲增量：{'开启' if status['automatic']['enabled'] else '关闭'}"
                                    + ('；尚未配置整理模型' if not status.get('model_ready') else ''))
            old_session = self.org_session.get()
            self.org_session_rows = value['sessions']
            labels = [f"{s['channel'] or '未分配入口'} · {s['id']} · {s['successful_turns']}轮" for s in value['sessions']]
            self.org_session_box['values'] = labels
            if old_session not in labels:
                self.org_session.set(labels[0] if labels else '')
                self.invalidate_org_preview()
            for key in ('tasks','candidates','summaries'):
                listing = self.org_lists[key]
                selection = listing.curselection()
                selected_id = self.org_records[key][selection[0]]['id'] if selection and selection[0] < len(self.org_records[key]) else None
                self.org_records[key] = value[key]
                listing.delete(0,'end')
                for index,row in enumerate(value[key]):
                    content = row.get('text', row.get('error_code') or row.get('mode') or ('摘要第'+str(row.get('revision',''))+'版'))
                    listing.insert('end', STATES.get(row['status'], row['status'])+' · '+row['id'][:8]+' · '+content[:65])
                    if row['id'] == selected_id:
                        listing.selection_set(index)
        self.run_task('读取记忆整理', {'org-read'}, read, done, quiet=True)

    def poll_organization(self):
        if self.closed:
            return
        if self.current_page == 'organization':
            self.refresh_organization()
        self.after(2000, self.poll_organization)

    def invalidate_org_preview(self):
        self.org_preview = None
        self.org_preview_generation += 1
        self.org_preview_text.set('选择范围后先预览；每批最多10轮。起终点是详情中的来源轮次序号，不是聊天文字行号。')

    def preview_organization(self):
        generation, epoch = self.org_preview_generation, self.view_epoch
        index = self.org_session_box.current()
        if index < 0 or index >= len(self.org_session_rows):
            self.notice.set('请先刷新并选择会话。')
            return
        modes = {'摘要与候选':'both','仅摘要':'summary','仅候选':'candidates'}
        try:
            args = {'session_id': self.org_session_rows[index]['id'], 'mode': modes[self.org_mode.get()],
                    'start_row': int(self.org_start.get()) if self.org_start.get().strip() else None,
                    'end_row': int(self.org_end.get()) if self.org_end.get().strip() else None, 'rebuild': self.org_rebuild.get()}
        except ValueError:
            self.notice.set('起终点须为非负整数或留空。')
            return
        def done(value):
            if generation != self.org_preview_generation or epoch != self.view_epoch:
                self.notice.set('范围或人格已改变，请重新预览。')
                return
            self.org_preview = value
            self.org_preview_text.set(f"本批 {len(value['rows'])} 轮，来源序号 {value['rows'][0]}–{value['rows'][-1]}；预计共 {value['estimated_batches']} 批；排除 {value['excluded_events']} 个事件。提交仅运行本批一次。")
        self.run_task('预览整理范围', {'org-preview'}, lambda _: self.client.manage('organization.preview', **args), done)

    def submit_organization(self):
        if not self.org_preview:
            self.notice.set('请先预览整理范围。')
            return
        preview = self.org_preview
        # Reuse the same key until the acceptance result is known, including a lost ACK.
        request_id = getattr(self, 'org_submission_id', None) or identifier()
        self.org_submission_id = request_id
        if not messagebox.askyesno('提交整理', '只处理预览中的本批，可能产生一次模型费用。手动候选仍需审阅。继续？'):
            return
        def done(task):
            self.org_submission_id = None
            self.org_preview_text.set('本批已受理：'+task['id']+'；在任务列表查看结果。')
            self.refresh_organization()
        self.run_task('提交整理', {'org-write'}, lambda _: self.client.manage('organization.submit', preview=preview, request_id=request_id), done)

    def next_organization_batch(self):
        if self.org_preview:
            self.org_start.set(str(self.org_preview['rows'][-1]+1))
            self.org_rebuild.set(False)
        self.org_submission_id = None
        self.preview_organization()

    def selected_org(self, kind):
        selected = self.org_lists[kind].curselection()
        return self.org_records[kind][selected[0]] if selected and selected[0] < len(self.org_records[kind]) else None

    def org_selected_action(self, kind, operation):
        row = self.selected_org(kind)
        if not row:
            return
        prefix, id_name = {'tasks':('organization','task_id'), 'candidates':('candidate','candidate_id'), 'summaries':('summary','summary_id')}[kind]
        args = {id_name: row['id']}
        if operation == 'reject':
            if not messagebox.askyesno('拒绝候选', '拒绝后屏蔽相关来源轮次用于后续上下文与整理，同轮其他细节也可能不可用。原文历史保留。继续？'):
                return
            args['operation_id'] = identifier()
        elif operation in ('retry','skip','disable'):
            if not messagebox.askyesno('确认操作', '重试可能计费；跳过不处理该范围；停用摘要会使依赖摘要停用。继续？'):
                return
        self.org_call(prefix+'.'+operation, title='来源与处理详情' if operation == 'show' else None, **args)

    def review_candidate(self, revise=False):
        row = self.selected_org('candidates')
        if not row or row['status'] != 'pending':
            self.notice.set('请选择待审阅候选。')
            return
        def edit(candidate, target=None):
            operation_id = identifier()
            self.text_dialog('审阅并确认记忆', candidate['text'],
                lambda text: self.client.manage('candidate.accept', candidate_id=candidate['id'], operation_id=operation_id,
                    text=text, target_memory_id=target['id'] if target else None, expected_revision=target['revision'] if target else None),
                resources={'org-review'}, on_saved=lambda _: self.refresh_organization(), save_label='确认生效')
        def loaded(candidate):
            if not revise:
                edit(candidate)
                return
            def choose(memories):
                dialog, body, footer = self.dialog_shell('选择要修订的有效记忆', (740,430))
                listing = tk.Listbox(body, width=1, height=1)
                listing.pack(fill='both', expand=True)
                for memory in memories:
                    listing.insert('end', memory['id'][:8]+' · '+memory['text'][:75])
                def selected():
                    if listing.curselection():
                        target = memories[listing.curselection()[0]]
                        dialog.destroy()
                        edit(candidate,target)
                ttk.Button(footer,text='选择后编辑',command=selected).pack(side='right')
                ttk.Button(footer,text='关闭',command=dialog.destroy).pack(side='right')
            self.run_task('读取有效记忆', {'org-review'}, lambda _: self.client.manage('memory.search', query='', limit=1000), choose)
        self.run_task('读取待审证据', {'org-review'}, lambda _: self.client.manage('candidate.show', candidate_id=row['id']), loaded)

    def toggle_idle_organization(self, enabled):
        if enabled and not messagebox.askyesno('开启空闲增量整理', '仅处理开启后的新增成功对话。保留最近10轮，旧轮次累计10轮且空闲120秒后整理；可能产生后台费用，候选仍须审阅。继续？'):
            return
        self.org_call('organization.automatic', enabled=enabled)

    def resume_daily_update(self):
        if messagebox.askyesno('继续每日更新', '将重新预览受阻范围并可能调用模型；不会跳过剩余内容。继续？'):
            self.org_call('organization.daily-resume')

    def run_daily_now(self):
        if messagebox.askyesno('立即全量更新', '将完整整理当前人格的有效历史，正常记忆自动生效、冲突待授权；可能多次调用模型，达到额度后等待继续。下次计划重设为本次开始后24小时。继续？'):
            self.org_call('organization.daily-run-now')

    def explain_chat_context(self):
        turn = self.selected_request or (self.turns[-1]['id'] if self.turns else None)
        if turn:
            self.org_call('recall.explain', title='本轮记忆、摘要与覆盖缺口', turn_id=turn)
