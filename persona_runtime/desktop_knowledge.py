"""Native knowledge controls using the workbench's background task hub."""

import json
import tkinter as tk
from tkinter import ttk, scrolledtext

from .archive import fingerprint
from .config import atomic_json
from .models import identifier
from .desktop_client import LocalAPIError


SOURCES = {'中文 Wikipedia': 'wikipedia_zh', 'Wikidata': 'wikidata'}


class KnowledgeUI:
    def init_knowledge_vars(self):
        self.knowledge_data = None
        self.knowledge_serial = 0
        self.knowledge_read = tk.BooleanVar(value=False)
        self.knowledge_process = tk.BooleanVar(value=False)
        self.knowledge_route = tk.BooleanVar(value=False)
        self.knowledge_route_variant = tk.StringVar(value='仅联网')
        self.knowledge_route_status = tk.StringVar(value='Jev尚未读取')
        self.knowledge_web = tk.BooleanVar(value=False)
        self.knowledge_wiki_fallback = tk.BooleanVar(value=True)
        self.knowledge_discovery = tk.BooleanVar(value=False)
        self.knowledge_web_domains = tk.StringVar(value='zh.wikipedia.org,www.wikidata.org')
        self.knowledge_web_topic = tk.StringVar()
        self.knowledge_web_query = tk.StringVar()
        self.knowledge_web_document = tk.StringVar()
        self.knowledge_web_documents = {}
        self.knowledge_learn = tk.BooleanVar(value=False)
        self.knowledge_daily = tk.BooleanVar(value=True)
        self.knowledge_channels = {channel: tk.BooleanVar(value=True) for channel in ('desktop', 'qq')}
        self.knowledge_source = tk.StringVar(value='中文 Wikipedia')
        self.knowledge_source_enabled = tk.BooleanVar(value=True)
        self.knowledge_store_body = tk.BooleanVar(value=True)
        self.knowledge_ttl = tk.StringVar(value='86400')
        self.knowledge_interval = tk.StringVar(value='1')
        self.knowledge_public_id = tk.StringVar()
        self.knowledge_directory = tk.StringVar(value='staging-71ecc16d8ab3')
        self.knowledge_backup = tk.StringVar()
        self.knowledge_query = tk.StringVar()
        self.knowledge_summary = tk.StringVar(value='连接人格服务后读取知识状态。')
        self.knowledge_source.trace_add('write', lambda *_: self.load_knowledge_source())

    def build_knowledge(self, parent):
        self.heading(parent, '世界知识', '公共资料查询与知识维护。角色读取与学习分别开启，新角色默认选择仅联网路径。')
        ttk.Label(parent, textvariable=self.knowledge_summary, wraplength=720).pack(anchor='w', pady=8)
        line = ttk.Frame(parent); line.pack(fill='x')
        self.button(line, '读取知识状态', self.refresh_knowledge, {'knowledge-read'})
        self.button(line, '找回上次操作', self.find_knowledge_operation, {'knowledge-read'})
        self.button(line, '继续原操作', self.resume_knowledge_operation, {'knowledge-write'})
        self.button(line, '查看详细记录', self.show_knowledge_details, {'knowledge-read'})
        line = ttk.Frame(parent); line.pack(fill='x')
        ttk.Checkbutton(line, text='允许知识回答', variable=self.knowledge_read).pack(side='left')
        for channel, label in (('desktop', '桌面'), ('qq', 'QQ')):
            ttk.Checkbutton(line, text=label, variable=self.knowledge_channels[channel]).pack(side='left')
        self.button(line, '保存角色开关', self.save_knowledge_reader, {'knowledge-write'})
        line = ttk.Frame(parent); line.pack(fill='x')
        ttk.Checkbutton(line, text='整理知识后回答（使用整理模型）', variable=self.knowledge_process).pack(side='left')
        self.button(line, '保存知识整理', self.save_knowledge_processing, {'knowledge-write'})
        line = ttk.Frame(parent); line.pack(fill='x')
        ttk.Checkbutton(line, text='允许公共来源学习', variable=self.knowledge_learn).pack(side='left')
        ttk.Checkbutton(line, text='每日协调', variable=self.knowledge_daily).pack(side='left')
        self.button(line, '保存维护授权', self.save_knowledge_maintenance, {'knowledge-write'})
        self.button(line, '停止学习', lambda: self.save_knowledge_maintenance(stop=True), {'knowledge-stop'})
        routing=ttk.LabelFrame(parent,text='Jev知识路径',padding=8);routing.pack(fill='x',pady=6)
        line=ttk.Frame(routing);line.pack(fill='x')
        ttk.Checkbutton(line,text='使用Jev判断本轮是否需要知识',variable=self.knowledge_route).pack(side='left')
        ttk.Combobox(line,textvariable=self.knowledge_route_variant,values=('仅联网','本地与联网'),state='readonly',width=12).pack(side='left')
        self.button(line,'保存决策设置',self.save_knowledge_routing,{'knowledge-write'})
        ttk.Label(routing,textvariable=self.knowledge_route_status,wraplength=700).pack(anchor='w')
        web = ttk.LabelFrame(parent,text='联网公开资料（复用主模型配置）',padding=8);web.pack(fill='x',pady=6)
        line=ttk.Frame(web);line.pack(fill='x')
        ttk.Checkbutton(line,text='允许联网查询，按请求计费',variable=self.knowledge_web).pack(side='left')
        self.button(line,'保存联网设置',self.save_knowledge_web,{'knowledge-write'})
        ttk.Checkbutton(web,text='维基正文不可用时，使用同一获准来源的原站API',variable=self.knowledge_wiki_fallback).pack(anchor='w')
        ttk.Checkbutton(web,text='自动定位未登记公共话题（提出与复核分别调用主模型）',variable=self.knowledge_discovery).pack(anchor='w')
        line=ttk.Frame(web);line.pack(fill='x')
        ttk.Label(line,text='允许的网站，逗号分隔').pack(side='left')
        ttk.Entry(line,textvariable=self.knowledge_web_domains).pack(side='left',fill='x',expand=True)
        line=ttk.Frame(web);line.pack(fill='x')
        ttk.Label(line,text='公开主题代号').pack(side='left')
        ttk.Entry(line,textvariable=self.knowledge_web_topic,width=14).pack(side='left')
        ttk.Label(line,text='登记的公开查询词').pack(side='left')
        ttk.Entry(line,textvariable=self.knowledge_web_query).pack(side='left',fill='x',expand=True)
        ttk.Label(web,text='外发登记主题或经独立复核的百科标题；不向来源站点发送原问题和私人经历。',wraplength=700).pack(anchor='w')
        line=ttk.Frame(web);line.pack(fill='x')
        self.knowledge_web_pages=ttk.Combobox(line,textvariable=self.knowledge_web_document,state='readonly')
        self.knowledge_web_pages.pack(side='left',fill='x',expand=True)
        self.button(line,'撤回所选网页',self.withdraw_web_knowledge,{'knowledge-write'})
        source = ttk.LabelFrame(parent, text='来源与公共页面', padding=8); source.pack(fill='x', pady=6)
        line = ttk.Frame(source); line.pack(fill='x')
        ttk.Combobox(line, textvariable=self.knowledge_source, values=tuple(SOURCES), state='readonly', width=20).pack(side='left')
        ttk.Checkbutton(line, text='来源政策可用', variable=self.knowledge_source_enabled).pack(side='left')
        ttk.Checkbutton(line, text='保存公共原文', variable=self.knowledge_store_body).pack(side='left')
        line = ttk.Frame(source); line.pack(fill='x')
        for label, variable in (('新鲜度（秒）', self.knowledge_ttl), ('最小间隔（秒）', self.knowledge_interval)):
            ttk.Label(line, text=label).pack(side='left')
            ttk.Entry(line, textvariable=variable, width=6).pack(side='left', padx=4)
        self.button(line, '保存来源政策', self.save_knowledge_source, {'knowledge-write'})
        line = self.bar(source); line.pack(fill='x')
        self.button(line, '启用／停用来源总开关', self.toggle_knowledge_source_global, {'knowledge-write'})
        line = ttk.Frame(source); line.pack(fill='x')
        ttk.Label(line, text='公开页面标题或Q编号').pack(side='left')
        ttk.Entry(line, textvariable=self.knowledge_public_id, width=1).pack(side='left', fill='x', expand=True)
        self.button(line, '刷新公共页面（可能计费）', self.refresh_public_knowledge, {'knowledge-write'})
        self.button(line, '撤回该页面', self.withdraw_public_knowledge, {'knowledge-write'})
        package = ttk.LabelFrame(parent, text='可选本地百科包及恢复', padding=8); package.pack(fill='x', pady=6)
        line = ttk.Frame(package); line.pack(fill='x')
        self.button(line, '准备固定中文包', self.prepare_knowledge_package, {'knowledge-write'})
        ttk.Entry(line, textvariable=self.knowledge_directory, width=1).pack(side='left', fill='x', expand=True)
        self.button(line, '激活目录', self.activate_knowledge_package, {'knowledge-write'})
        self.button(line, '回退上一包', self.rollback_knowledge_package, {'knowledge-write'})
        line = ttk.Frame(package); line.pack(fill='x')
        self.button(line, '创建业务备份', lambda: self.api_action('data.backup'), {'backup'})
        self.button(line, '导出当前角色', lambda: self.api_action('data.export'), {'backup'})
        self.button(line, '导入角色历史与知识', self.import_persona, {'persona'})
        line = ttk.Frame(package); line.pack(fill='x')
        ttk.Label(line, text='备份相对路径').pack(side='left')
        ttk.Entry(line, textvariable=self.knowledge_backup, width=1).pack(side='left', fill='x', expand=True)
        self.button(line, '合并恢复知识记录', self.restore_knowledge_backup, {'knowledge-write'})
        ttk.Label(package, text='恢复沿用当前权限和撤回状态，原调用账目继续保留。便携导入的知识进入待核对状态。', wraplength=700).pack(anchor='w')
        line = ttk.Frame(parent); line.pack(fill='x', pady=6)
        ttk.Entry(line, textvariable=self.knowledge_query).pack(side='left', fill='x', expand=True)
        self.button(line, '预览本地检索', self.preview_knowledge_search, {'knowledge-read'})
        self.knowledge_output = scrolledtext.ScrolledText(parent, height=16, wrap='word', state='disabled')
        self.knowledge_output.pack(fill='both', expand=True)

    def knowledge_key(self):
        return self.persona_id, self.view_epoch

    def refresh_knowledge(self):
        if not self.persona_id:
            return
        key = self.knowledge_key()
        self.knowledge_serial += 1
        serial = self.knowledge_serial
        def done(data):
            if key != self.knowledge_key() or serial != self.knowledge_serial:
                return
            self.knowledge_process.set(data.get('processing_policy',{}).get('data',{}).get('enabled',False))
            routing=data.get('routing_policy',{}).get('data',{})
            self.knowledge_route.set(routing.get('enabled',False))
            self.knowledge_route_variant.set('仅联网' if routing.get('variant')=='web_only' else '本地与联网')
            configured=data.get('routing_configuration',{});cost=data.get('routing_cost',{})
            self.knowledge_route_status.set(f"模型 {configured.get('name') or '未配置'}，十轮预检{'已通过' if configured.get('gate_passed') else '未通过'}；本角色决策 {cost.get('calls',0)} 次、已知 {cost.get('tokens_known',0)} tokens。费用包含在知识模型账目中。")
            web=data.get('web_policy',{}).get('data',{})
            if getattr(self,'knowledge_loaded_key',None)!=key:
                self.knowledge_web_topic.set('');self.knowledge_web_query.set('')
            self.knowledge_web_documents={f"{row['title']}（{row['id'][:8]}，{row['validity']}）":row['id'] for row in data.get('web_documents',[])}
            self.knowledge_web_pages.configure(values=tuple(self.knowledge_web_documents))
            if getattr(self,'knowledge_loaded_key',None)!=key or self.knowledge_web_document.get() not in self.knowledge_web_documents:
                self.knowledge_web_document.set('')
            self.knowledge_web.set(web.get('enabled',False))
            self.knowledge_wiki_fallback.set(web.get('wikimedia_fallback',False))
            self.knowledge_discovery.set(web.get('discover_public_pages',False))
            self.knowledge_web_domains.set(','.join(web.get('domains',[])))
            self.knowledge_data = data
            self.knowledge_loaded_key = key
            self.knowledge_read.set(data['policy']['data']['enabled'])
            for channel, variable in self.knowledge_channels.items():
                variable.set(channel in data['policy']['data']['channels'])
            self.knowledge_learn.set(data['maintenance_policy']['data']['enabled'])
            self.knowledge_daily.set(data['maintenance_policy']['data']['daily_scan'])
            self.load_knowledge_source()
            cost = data['cost']
            phase = (data.get('installation') or {}).get('phase', '尚无安装记录')
            route=data.get('routing_policy',{}).get('data',{}).get('variant')
            package_status=("仅联网路径：无需安装或预热本地百科。" if route=='web_only' else
                f"本地百科安装进度 {phase}，模型{'就绪' if data['model_ready'] else '未预热'}。")
            self.knowledge_summary.set(package_status+
                f"知识处理尝试 {cost['calls']} 次，已知 {cost['tokens_known']} tokens，未知用量 {cost['usage_unknown']} 次。金额按供应商账单核对。")
            self.set_text(self.knowledge_output, self.knowledge_report(data))
        self.run_task('读取世界知识', {'knowledge-read'}, lambda _: self.client.manage('knowledge.manage.show', persona_id=key[0]),
                      done, error_current=lambda: key == self.knowledge_key() and serial == self.knowledge_serial)

    def knowledge_report(self, data):
        from .desktop_organization import STATES
        states = STATES | {'fetching': '读取原文', 'extracting': '提取中', 'reviewing': '独立复核中',
            'committed': '已生效', 'quarantined': '已隔离', 'unavailable': '暂不可用',
            'withdrawn': '已撤回', 'waiting_extract': '待提取', 'waiting_review': '待独立复核',
            'restored_complete': '已恢复', 'restore_review_required': '恢复后待核对',
            'import_review_required': '导入后待核对'}
        registry = data['registry']['data']
        limits = data['automatic_limits']
        hourly = str(limits['hour']) if limits['hour'] else '不限'
        daily = str(limits['rolling_day']) if limits['rolling_day'] else '不限'
        lines = ['本地百科包', '当前包 ' + str(registry['active'] or '尚未激活'),
                 '可回退包 ' + str(registry['previous'] or '尚无'),
                 '自动维护额度 每小时' + hourly + '，滚动24小时' + daily, '', '来源政策']
        if data.get('routing_policy',{}).get('data',{}).get('variant')=='web_only':lines.insert(0,'当前选择仅联网，本地百科仅用于已保存的旧证据或显式切换。')
        lines.insert(1, '可选本地依赖 ' + ('就绪' if data['dependencies']['ready'] else '缺少 ' + '、'.join(data['dependencies']['missing'])))
        for name, source_id in SOURCES.items():
            policy = data['source_policies'][source_id]['data']
            available = policy['enabled'] and data['sources']['data'][source_id]['enabled']
            lines.append(f"{name} {'可用' if available else '已停用'}，新鲜度 {policy['freshness_seconds']} 秒")
        tool=data.get('tool_cost',{})
        lines.extend(['',f"联网工具尝试 {tool.get('calls',0)}，计费成功 {tool.get('billable_calls',0)}，按文档估算 ${tool.get('known_amount_usd','0')}，未知 {tool.get('unknown_effect',0)}"])
        lines.extend(['', '已登记公开主题'])
        for key,query in data.get('web_policy',{}).get('data',{}).get('topics',{}).items():lines.append(key+' · '+query)
        lines.extend(['', '知识整理'])
        for row in data.get('processing_tasks',[]):
            stage={'goal':'目标理解','select':'资料选择','translate':'知识翻译','compose':'知识合并','verify':'独立复核','public_page_plan':'公共页面定位','public_page_review':'查询内容复核'}.get(row['stage'],row['stage'])
            lines.append(stage+' · '+states.get(row['status'],row['status'])+' · '+(row['error_code'] or '无错误'))
        lines.extend(['', '最近使用页面'])
        for row in data['pages']:
            freshness = '新鲜' if row['freshness'] == 'fresh' else '本地快照'
            lines.append(f"{row['title']} · 修订 {row['latest_revision']} · {freshness} · {states.get(row['state'], row['state'])}")
        lines.extend(['', '获取队列'])
        for row in data['fetch_jobs']:
            lines.append(states.get(row['state'], row['state']) + ' · ' + (row['error'] or '无错误') + ' · ' + row['id'])
        lines.extend(['', '提取与复核'])
        for row in data['batches']:
            lines.append(states.get(row['state'], row['state']) + ' · ' + row['id'])
            coverage = row['coverage']
            if coverage:
                lines.append(f"已抽取 {coverage['end'] - coverage['start']}/{coverage['document_characters']} 字符")
            lines.append('隔离建议 ' + str(len(row['isolated'])) + ' 条')
            for item in row['isolated']:
                lines.append('  ' + item['reason'])
            if row['extract_error'] or row['review_error']:
                lines.append('处理原因 ' + str(row['extract_error'] or row['review_error']))
        return '\n'.join(lines)

    def show_knowledge_details(self):
        if self.knowledge_ready():
            self.show_json('知识版本、来源和处理记录', self.knowledge_data)

    def load_knowledge_source(self):
        if not self.knowledge_data:
            return
        data = self.knowledge_data['source_policies'][SOURCES[self.knowledge_source.get()]]['data']
        self.knowledge_source_enabled.set(data['enabled'])
        self.knowledge_store_body.set(data['store_body'])
        self.knowledge_ttl.set(str(data['freshness_seconds']))
        self.knowledge_interval.set(str(data['min_interval_seconds']))

    def knowledge_ready(self):
        if not self.knowledge_data or getattr(self, 'knowledge_loaded_key', None) != self.knowledge_key():
            self.notice.set('请先读取当前角色的知识状态。')
            return False
        return True

    def knowledge_intent_path(self, priority=False):
        return self.paths.home / ('runtime/knowledge-stop-operation.json' if priority else 'runtime/knowledge-operation.json')

    def pending_knowledge_operation(self):
        for priority in (True, False):
            path = self.knowledge_intent_path(priority)
            if path.exists():
                return json.loads(path.read_text(encoding='utf-8'))
        return None

    def clear_knowledge_intent(self, operation_id):
        for priority in (True, False):
            path = self.knowledge_intent_path(priority)
            if path.exists() and json.loads(path.read_text(encoding='utf-8'))['operation_id'] == operation_id:
                path.unlink()

    def knowledge_write(self, action, args, *, priority=False):
        if not self.knowledge_ready():
            return
        path = self.knowledge_intent_path(priority)
        intent = {'action': action, 'args': args, 'persona_id': self.persona_id}
        if path.exists():
            pending = json.loads(path.read_text(encoding='utf-8'))
            if pending['fingerprint'] != fingerprint(intent):
                self.notice.set('上次知识操作尚未确认，请先找回或继续原操作。')
                return
        else:
            pending = intent | {'fingerprint': fingerprint(intent), 'operation_id': identifier(), 'priority': priority}
            atomic_json(path, pending)
        self.send_knowledge_operation(pending)

    def send_knowledge_operation(self, pending):
        if pending['persona_id'] != self.persona_id:
            self.notice.set('请切换至该操作原来的角色后继续。')
            return
        key = self.knowledge_key()
        def done(value):
            self.clear_knowledge_intent(pending['operation_id'])
            if key != self.knowledge_key():
                return
            self.notice.set('知识操作已保存。')
            self.refresh_knowledge()
        def failed(error):
            # A definite validation rejection permits correcting the draft.
            if isinstance(error, LocalAPIError) and str(error) in {
                    'version_conflict', 'knowledge_operation_invalid', 'knowledge_query_invalid',
                    'knowledge_policy_invalid', 'knowledge_maintenance_policy_invalid',
                    'knowledge_source_policy_invalid', 'knowledge_refresh_scope_invalid',
                    'knowledge_backup_name_invalid', 'knowledge_backup_invalid',
                    'knowledge_previous_package_missing', 'knowledge_target_not_found', 'knowledge_dependencies_missing'}:
                self.clear_knowledge_intent(pending['operation_id'])
                if str(error) == 'knowledge_dependencies_missing' and key == self.knowledge_key():
                    self.notice.set('本地知识依赖尚未安装，请按验收手册安装 knowledge-ann 后继续。')
        resources = {'knowledge-stop'} if pending.get('priority') else {'knowledge-write'}
        self.run_task('保存知识操作', resources, lambda _: self.client.call('POST', '/v1/manage',
            {'action': pending['action'], 'args': pending['args'] | {'operation_id': pending['operation_id']}}, timeout=60),
            done, failure=failed, error_current=lambda: key == self.knowledge_key())

    def find_knowledge_operation(self):
        pending = self.pending_knowledge_operation()
        if not pending:
            self.notice.set('没有待确认的知识操作。')
            return
        key = self.knowledge_key()
        def done(value):
            if value['state'] == 'completed':
                self.clear_knowledge_intent(pending['operation_id'])
            if key != self.knowledge_key():
                return
            self.notice.set('已找回保存回执。' if value['state'] == 'completed' else '尚未找到保存回执，可继续原操作。')
            self.refresh_knowledge()
        self.run_task('找回知识操作', {'knowledge-read'}, lambda _: self.client.manage('knowledge.operation.show',
            operation_id=pending['operation_id']), done, error_current=lambda: key == self.knowledge_key())

    def resume_knowledge_operation(self):
        pending = self.pending_knowledge_operation()
        if pending:
            self.send_knowledge_operation(pending)

    def save_knowledge_reader(self):
        if self.knowledge_ready():
            self.knowledge_write('knowledge.policy.update', {'persona_id': self.persona_id,
                'data': {'enabled': self.knowledge_read.get(), 'channels': [k for k, v in self.knowledge_channels.items() if v.get()]},
                'expected_revision': self.knowledge_data['policy']['revision']})

    def save_knowledge_routing(self):
        if not self.knowledge_ready():return
        policy=self.knowledge_data['routing_policy']
        self.knowledge_write('knowledge.routing.policy.update',{'persona_id':self.persona_id,
            'data':policy['data']|{'enabled':self.knowledge_route.get(),'variant':'web_only' if self.knowledge_route_variant.get()=='仅联网' else 'local_web'},
            'expected_revision':policy['revision']})

    def save_knowledge_web(self):
        if not self.knowledge_ready() or not self.knowledge_data.get('web_policy'):return
        policy=self.knowledge_data['web_policy']
        data=dict(policy['data']);data['topics']=dict(data['topics'])
        key,query=self.knowledge_web_topic.get().strip(),self.knowledge_web_query.get().strip()
        if key and query:data['topics'][key]=query
        data.update(enabled=self.knowledge_web.get(),domains=[s.strip() for s in self.knowledge_web_domains.get().split(',') if s.strip()],wikimedia_fallback=self.knowledge_wiki_fallback.get(),discover_public_pages=self.knowledge_discovery.get())
        self.knowledge_write('knowledge.web.policy.update',{'persona_id':self.persona_id,'data':data,'expected_revision':policy['revision']})

    def withdraw_web_knowledge(self):
        if not self.knowledge_ready():return
        document_id=self.knowledge_web_documents.get(self.knowledge_web_document.get())
        if not document_id:
            self.notice.set('请先选择已经获取的网页。');return
        self.knowledge_write('knowledge.web.withdraw',{'persona_id':self.persona_id,'document_id':document_id,
            'reason':'用户在工作台撤回网页','expected_revision':self.knowledge_data['web_invalidations']['revision']})

    def save_knowledge_processing(self):
        if self.knowledge_ready() and self.knowledge_data.get('processing_policy'):
            policy=self.knowledge_data['processing_policy']
            self.knowledge_write('knowledge.processing.policy.update',{'persona_id':self.persona_id,
                'data':policy['data']|{'enabled':self.knowledge_process.get()},'expected_revision':policy['revision']})

    def save_knowledge_maintenance(self, stop=False):
        if self.knowledge_ready():
            data = self.knowledge_data['maintenance_policy']['data'] | {
                'enabled': False if stop else self.knowledge_learn.get(), 'daily_scan': self.knowledge_daily.get(),
                'provider': self.knowledge_data['maintenance_provider']}
            self.knowledge_write('knowledge.maintenance.policy.update', {'persona_id': self.persona_id, 'data': data,
                'expected_revision': self.knowledge_data['maintenance_policy']['revision']}, priority=stop)

    def save_knowledge_source(self):
        if not self.knowledge_ready():
            return
        source_id = SOURCES[self.knowledge_source.get()]
        try:
            data = {'enabled': self.knowledge_source_enabled.get(), 'store_body': self.knowledge_store_body.get(),
                'freshness_seconds': int(self.knowledge_ttl.get()), 'min_interval_seconds': int(self.knowledge_interval.get())}
        except ValueError:
            self.notice.set('来源时间设置须填写整数。')
            return
        self.knowledge_write('knowledge.source.policy.update', {'source_id': source_id, 'data': data,
            'expected_revision': self.knowledge_data['source_policies'][source_id]['revision']})

    def toggle_knowledge_source_global(self):
        if self.knowledge_ready():
            sources = self.knowledge_data['sources']
            data = {key: dict(value) for key, value in sources['data'].items()}
            source_id = SOURCES[self.knowledge_source.get()]
            data[source_id]['enabled'] = not data[source_id]['enabled']
            self.knowledge_write('knowledge.source.update', {'data': data, 'expected_revision': sources['revision']})

    def refresh_public_knowledge(self):
        if self.knowledge_ready():
            self.knowledge_write('knowledge.refresh.submit', {'persona_id': self.persona_id,
                'items': [{'source_id': SOURCES[self.knowledge_source.get()], 'public_id': self.knowledge_public_id.get().strip()}],
                'expected_revision': self.knowledge_data['network_refresh']['refresh']['revision']})

    def withdraw_public_knowledge(self):
        if not self.knowledge_ready():
            return
        target = next((row for row in self.knowledge_data['targets'] if row['source_id'] == SOURCES[self.knowledge_source.get()]
            and row['public_id'] == self.knowledge_public_id.get().strip()), None)
        if not target:
            self.notice.set('请填写当前角色已登记的公共页面。')
            return
        self.knowledge_write('knowledge.withdraw.submit', {'persona_id': self.persona_id, 'target_id': target['id'],
            'reason': 'explicit_management_withdrawal', 'expected_revision': self.knowledge_data['network_refresh']['withdrawals']['revision']})

    def prepare_knowledge_package(self):
        if self.knowledge_ready():
            self.knowledge_write('knowledge.package.prepare', {'expected_revision': self.knowledge_data['preparation']['revision']})

    def activate_knowledge_package(self):
        if self.knowledge_ready():
            self.knowledge_write('knowledge.package.activate', {'directory': self.knowledge_directory.get().strip(),
                'expected_revision': self.knowledge_data['registry']['revision']})

    def rollback_knowledge_package(self):
        if self.knowledge_ready():
            self.knowledge_write('knowledge.package.rollback', {'expected_revision': self.knowledge_data['registry']['revision']})

    def restore_knowledge_backup(self):
        if self.knowledge_ready():
            self.knowledge_write('knowledge.restore.submit', {'name': self.knowledge_backup.get().strip(),
                'expected_revision': self.knowledge_data['restore']['revision']})

    def preview_knowledge_search(self):
        if not self.persona_id:
            return
        key, query = self.knowledge_key(), self.knowledge_query.get().strip()
        self.knowledge_preview_version = getattr(self, 'knowledge_preview_version', 0) + 1
        self.poll_knowledge_search(key, query, self.knowledge_preview_version)

    def poll_knowledge_search(self, key, query, version):
        def current():
            return (version == self.knowledge_preview_version and key == self.knowledge_key()
                    and query == self.knowledge_query.get().strip())
        if not current():
            return
        def done(value):
            if current():
                self.set_text(self.knowledge_output, json.dumps(value, ensure_ascii=False, indent=2))
                if value.get("state") in ("pending", "busy"):
                    self.after(500, lambda: self.poll_knowledge_search(key, query, version))
        self.run_task('预览知识检索', {'knowledge-read'}, lambda _: self.client.manage('knowledge.search',
            persona_id=key[0], query=query, channel='desktop', timeout=0), done, error_current=current)
