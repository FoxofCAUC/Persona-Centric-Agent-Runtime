"""Extensible Windows workbench. Blocking operations never run on the Tk thread."""
import json
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import webbrowser

from .config import ConfigStore, atomic_json
from .deployment import prepare
from .desktop_client import LocalClient
from .instance import InstanceLock
from .lifecycle import ServiceController
from .models import identifier
from .reused.logs import LogTail
from .reused.tasks import TaskHub
from .desktop_organization import OrganizationUI
from .desktop_profile import ProfileUI
from .desktop_character import CharacterUI
from .desktop_batches import BatchUI, BATCH_LABELS
from .desktop_delivery import DeliveryUI
from .desktop_quotas import QuotaUI
from .desktop_archive import ArchiveUI
from .desktop_archive_search import ArchiveSearchUI
from .desktop_context import ContextUI
from .desktop_sessions import SessionUI
from .desktop_selector import SelectorUI

LABELS = {"service": "人格服务", "astrbot": "消息服务", "napcat": "QQ 连接"}
ERRORS = {"provider_timeout": "模型响应超时，可检查状态后手动重试。", "hourly_limit_reached": "已达到每小时调用上限。",
          "model_not_configured": "尚未配置模型地址和名称。", "provider_http_401": "模型密钥未通过验证。",
          "provider_http_429": "模型提供商限流，请稍后手动重试。", "queue_full": "等待队列已满，请稍后再试。",
          "provider_network_error": "模型网络连接失败。", "context_limit_exceeded": "人格、关系与消息超过输入长度限制。",
          "service_restarted": "请求因服务重启而中断，未自动重试。"}
ERRORS.update({"session_sealed":"此会话已封存。草稿保留，请在会话与主题页新建接续会话后明确发送。", "session_has_pending_work":"会话还有未决输入或投递，请先处置。", "session_version_conflict":"会话版本已变化，请重新读取后操作。", "organization_session_busy":"此会话已有整理任务，请等待或处置后继续。","reference_not_found":"引用目标未找到，本批未调用模型；请更换或清除引用。", "reference_unavailable":"引用来源不可用，本批未调用模型。", "reference_ambiguous":"引用目标不唯一，本批未调用模型。", "reference_budget_exceeded":"完整引用超出预算，本批未调用模型。", "reference_source_changed":"引用来源已变化，本批未调用模型。", "buffer_full":"输入缓冲已满，本条未受理；请先处理已有批次。", "unresolved_input_batches":"仍有未处理输入，请到输入与缓冲页继续或取消后再停止。", "runtime_upgrade_required":"人格服务版本不兼容，请先升级服务。", "only_queued_can_cancel": "只能取消尚未开始生成的排队消息。", "cancelled_by_user": "排队消息已取消。",
               "queue_cancellation_disabled": "排队取消尚未开启，可在模型页的私聊交互设置中显式开启。",
               "submission_conflict": "此请求标识已用于其他内容；请先找回原消息，不能覆盖。",
               "provider_http_403": "提供商拒绝访问，请检查模型权限。", "provider_invalid_response": "提供商响应格式不正确。",
               "request_failed": "本机处理失败，请查看请求详情和诊断记录。"})
PHASES = {"character_local":"正在本地召回", "character_selection":"正在云端选择候选", "character_fallback":"已退回词法，准备回复", "queued": "排队中", "running": "正在生成", "completed": "已完成", "failed": "生成失败",
          "interrupted": "已中断", "cancelled": "已取消", "awaiting_delivery": "已生成，等待投递",
          "delivering": "正在发送", "delivery_uncertain": "送达结果不确定", "delivery_failed": "发送失败"}


class ScrollPage(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f4f6f8")
        bar = ttk.Scrollbar(self, command=self.canvas.yview)
        bar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=18)
        window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        def resized(event):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            def wrap(frame, width):
                for child in frame.winfo_children():
                    if isinstance(child, ttk.Label) and int(child.cget("wraplength") or 0):
                        target=max(180,width-10)
                        if int(child.cget("wraplength")) != target:child.configure(wraplength=target)
                    elif isinstance(child, ttk.Frame):
                        wrap(child, max(180, width-20))
            wrap(self.body, event.width)
        self.body.bind("<Configure>", resized)
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(window, width=event.width))


class Workbench(SelectorUI, SessionUI, ContextUI, ArchiveSearchUI, ArchiveUI, QuotaUI, DeliveryUI, BatchUI, CharacterUI, ProfileUI, OrganizationUI, tk.Tk):
    # Extension points: add a page builder or a ServiceAdapter without replacing the shell.
    PAGE_REGISTRY = (("home", "首页", "build_home"), ("chat", "聊天", "build_chat"),
                     ("persona", "人格与记忆", "build_persona"), ("connection", "QQ 连接", "build_connection"),
                     ("model", "模型", "build_model"), ("organization", "记忆整理", "build_organization"),
                     ('profile','人格与关系','build_profile'),
                     ('character','角色资料','build_character'),
                     ('batches','输入与缓冲','build_batches'),
                     ('delivery','回复投递','build_delivery'),
                     ('archive','原始档案','build_archive'),
                     ('archive-search','档案检索','build_archive_search'),
                     ('context','认知上下文','build_context_page'),
                     ('sessions','会话与主题','build_sessions_page'),
                     ("diagnostics", "故障排查", "build_diagnostics"))

    def __init__(self, paths, *, managed_lifecycle=False, controller=None):
        super().__init__()
        self.paths = paths
        self.config = ConfigStore(paths)
        self.config.initialize()
        self.client = LocalClient(self.config)
        self.hub = TaskHub()
        self.controller = controller or ServiceController(paths, self.hub.progress)
        self.controller.notify = self.hub.progress
        self.managed_lifecycle = managed_lifecycle
        self.shutdown_started = False
        self.closed = False
        self.buttons = []
        self.personas = []
        self.session_id = None
        self.persona_id = None
        self.chat_version = None
        self.turns = []
        self.selected_request = None
        self.view_epoch = 0
        self.memory_rows = []
        self.last_ready = False
        self.tail = LogTail()
        self.current_page = "home"
        self.notice = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="正在读取本项目状态…")
        self.operation_text = tk.StringVar(value="没有正在执行的操作")
        self.persona_choice = tk.StringVar()
        self.memory_query = tk.StringVar()
        self.qq_vars = {key: tk.StringVar() for key in ("bot_qq", "owner_qq", "service_port", "napcat_port", "astrbot_port", "onebot_port")}
        self.model_vars = {key: tk.StringVar() for key in ("base_url", "name", "api_key", "timeout", "max_tokens", "hourly_limit", "max_input_chars")}
        self.clear_key = tk.BooleanVar(value=False)
        self.allow_queue_cancel = tk.BooleanVar(value=False)
        self.qq_wait_notice = tk.BooleanVar(value=False)
        self.qq_wait_seconds = tk.StringVar(value="15")
        self.log_source = tk.StringVar(value="service")
        self.log_search = tk.StringVar()
        self.init_organization_vars()
        self.init_profile_vars()
        self.init_character_vars()
        self.init_batch_vars()
        self.init_delivery_vars()
        self.init_archive_vars()
        self.init_archive_search_vars()
        self.init_context_vars()
        self.init_session_vars()
        self.title("Persona-Centric Agent Runtime (PCAR) · M7.3")
        self.geometry(f"{min(1120, self.winfo_screenwidth()-50)}x{min(780, self.winfo_screenheight()-100)}")
        self.minsize(780, 560)
        self.build_shell()
        self.load_config_fields()
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        self.after(100, self.tick)
        self.after(300, self.poll)
        self.after(450, self.refresh_conversation)
        self.after(900, self.poll_organization)
        self.after(1100,self.poll_profile)
        self.after(1200,self.poll_batches)
        self.after(1500,self.poll_deliveries)

    def run_task(self, name, resources, action, success=None, quiet=False, urgent=False, failure=None, error_current=None):
        if self.closed and self.managed_lifecycle:
            return None
        if not urgent and any("emergency" in operation.resources for operation in self.hub.active.values()):
            if not quiet:
                self.notice.set("强制停止正在执行，请等待完成。")
            return None
        def report_failure(error):
            # Background refreshes must not overwrite a foreground result.
            # Reads can also expire with their view/target; writes and service
            # operations keep their normal diagnostics and editor callbacks.
            if not quiet and (error_current is None or error_current()):
                self.notice.set(ERRORS.get(str(error), str(error)))
            if failure:
                failure(error)
        operation = self.hub.start(name, resources, lambda cancel: action(cancel), success=success, failure=report_failure, quiet=quiet, urgent=urgent)
        if not operation and not quiet:
            self.notice.set("相关操作仍在进行，请等待完成。")
        return operation

    def button(self, parent, label, callback, resources=(), primary=False):
        button = ttk.Button(parent, text=label, command=callback, style="Primary.TButton" if primary else "TButton")
        self.buttons.append((button, set(resources)))
        if getattr(parent, "flow_buttons", False):
            button.grid(row=0, column=len(parent.winfo_children())-1, sticky="w", padx=(0, 7), pady=4)
        else:
            button.pack(side="left", padx=(0, 7), pady=4)
        return button

    def bar(self, parent):
        frame = ttk.Frame(parent)
        frame.flow_buttons = True
        def layout(event):
            row = col = used = 0
            for child in frame.winfo_children():
                width = child.winfo_reqwidth()+7
                if used and used+width > event.width:
                    row += 1
                    col = used = 0
                child.grid(row=row, column=col, sticky="w", padx=(0, 7), pady=4)
                col += 1
                used += width
        frame.bind("<Configure>", layout)
        return frame

    def heading(self, parent, title, description):
        ttk.Label(parent, text=title, style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(parent, text=description, wraplength=700, foreground="#526174").pack(anchor="w", pady=(0, 12))

    def build_shell(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background="#f4f6f8")
        style.configure("TButton", padding=(10, 7))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Primary.TButton", background="#2859b8", foreground="white")
        header = ttk.Frame(self, padding=(18, 12))
        header.pack(fill="x")
        ttk.Label(header, text="Persona-Centric Agent Runtime (PCAR)", font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True)
        nav = ttk.Frame(shell, padding=12)
        nav.pack(side="left", fill="y")
        host = ttk.Frame(shell)
        host.pack(side="left", fill="both", expand=True)
        self.pages = {}
        for key, label, builder in self.PAGE_REGISTRY:
            ttk.Button(nav, text=label, command=lambda page=key: self.show_page(page)).pack(fill="x", pady=5)
            self.pages[key] = ScrollPage(host)
            getattr(self, builder)(self.pages[key].body)
        self.pages["home"].pack(fill="both", expand=True)
        ttk.Label(self, textvariable=self.notice, wraplength=1000, padding=(15, 10)).pack(fill="x")
        self.bind_all("<MouseWheel>", self.scroll_page, add=True)

    def scroll_page(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        if widget and not isinstance(widget, (tk.Text, ttk.Treeview, ttk.Combobox)):
            self.pages[self.current_page].canvas.yview_scroll(int(-event.delta/120), "units")

    def show_page(self, key):
        if key != self.current_page and not self.character_can_leave():return
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self.current_page = key
        if key == "character":self.refresh_character()
        if key in ("chat", "persona"):
            self.refresh_personas()
        if key == "diagnostics":
            self.read_logs()
        if key == "organization":
            self.refresh_organization()
        if key=='profile':self.refresh_profile()
        if key=='delivery':self.refresh_deliveries()
        if key=='context':self.refresh_context_policy()

    def build_home(self, parent):
        ttk.Label(parent, text="运行状态", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(parent, textvariable=self.status_text, wraplength=720).pack(anchor="w", pady=12)
        bar = self.bar(parent)
        bar.pack(fill="x")
        self.button(bar, "启动服务", lambda: self.run_task("启动服务", {"lifecycle", "config"}, self.controller.start_all), {"lifecycle", "config"}, True)
        self.button(bar, "停止服务", lambda: self.run_task("停止服务", {"lifecycle", "config"}, lambda _: self.controller.stop_all()), {"lifecycle", "config"})
        self.button(bar, "强制停止全部相关程序", self.force_stop_everything, {"emergency"})
        self.button(bar, "强制停止 QQ", self.force_stop_qq, {"emergency"})
        bar = self.bar(parent)
        bar.pack(fill="x")
        self.button(bar, "只启动人格服务", lambda: self.start_one("service"), {"lifecycle"})
        self.button(bar, "准备独立组件", self.prepare_components, {"lifecycle", "config"})
        ttk.Label(parent, textvariable=self.operation_text, wraplength=700).pack(anchor="w", pady=8)
        self.progress = ttk.Progressbar(parent, mode="indeterminate")
        self.progress.pack(fill="x", pady=8)
        ttk.Button(parent, text="取消后续启动步骤", command=self.cancel_tasks).pack(anchor="w", pady=5)
        self.login_text = tk.StringVar(value="QQ：正在核对 · OneBot：正在核对")
        ttk.Label(parent, textvariable=self.login_text, wraplength=700).pack(anchor="w", pady=12)
        ttk.Button(parent, text="打开 QQ 登录页", command=lambda: self.open_panel("napcat")).pack(anchor="w")
        self.build_quotas(parent)

    def build_chat(self, parent):
        self.heading(parent, "聊天", "窗口和 QQ 共享长期记忆，各自保留近期会话。失败不会自动重试。")
        self.chat_persona = ttk.Combobox(parent, textvariable=self.persona_choice, state="readonly")
        self.chat_persona.pack(fill="x")
        self.chat_persona.bind("<<ComboboxSelected>>", lambda _: self.select_persona())
        bar = self.bar(parent)
        bar.pack(fill="x", pady=5)
        self.button(bar, "恢复当前会话", self.load_chat, {"chat"})
        self.button(bar, "新建会话", lambda: self.load_chat(fresh=True), {"chat"})
        self.button(bar, "选择已有会话", self.choose_session, {"chat"})
        self.transcript = scrolledtext.ScrolledText(parent, height=17, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10))
        self.transcript.pack(fill="both", expand=True, pady=8)
        reference_bar=ttk.Frame(parent);reference_bar.pack(fill='x')
        ttk.Label(reference_bar,textvariable=self.reference_text).pack(side='left')
        self.button(reference_bar,'清除引用',self.clear_archive_reference)
        self.chat_input = scrolledtext.ScrolledText(parent, height=4, wrap="word")
        self.chat_input.pack(fill="x")
        bar = self.bar(parent)
        bar.pack(fill="x")
        self.button(bar, "发送", self.send_chat, {"send"}, True)
        self.button(bar, "重试最后失败请求", self.retry_chat, {"send"})
        self.button(bar, "找回未确认消息", self.recover_submissions, {"submission-recovery"})
        self.button(bar, "请求队列与详情", self.request_queue, {"request-list"})
        self.button(bar, "查看本轮记忆使用", self.explain_chat_context, {"org-details"})

    def build_persona(self, parent):
        self.heading(parent, "人格与记忆", "正式设定作为全新人格导入，不继承模板期数据。记忆需要明确来源。")
        self.persona_list = tk.Listbox(parent, height=5, exportselection=False)
        self.persona_list.pack(fill="x")
        bar = self.bar(parent)
        bar.pack(fill="x")
        for label, action in (("创建通用模板", self.create_template), ("导入人格 JSON", self.import_persona),
                              ("切换选中人格", self.use_selected_persona), ("查看设定", self.show_persona),
                              ("关系说明", self.edit_relationship),('私有兼容检查',self.check_private_compatibility)):
            self.button(bar, label, action, {"persona"})
        ttk.Separator(parent).pack(fill="x", pady=14)
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.memory_query).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.button(row, "检索记忆", self.search_memory, {"memory"})
        self.memories = ttk.Treeview(parent, columns=("category", "text"), show="headings", height=7)
        self.memories.heading("category", text="类别")
        self.memories.heading("text", text="记忆内容")
        self.memories.column("category", width=120, stretch=False)
        self.memories.column("text", width=500)
        self.memories.pack(fill="x", pady=7)
        bar = self.bar(parent)
        bar.pack(fill="x")
        for label, action in (("显式录入", self.add_memory), ("查看来源与历史", self.show_memory),
                              ("修订", self.revise_memory), ("撤回", self.retract_memory)):
            self.button(bar, label, action, {"memory"})

    def fields(self, parent, variables, labels, secret=None):
        form = ttk.Frame(parent)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        for index, (key, label) in enumerate(labels):
            ttk.Label(form, text=label).grid(row=index, column=0, sticky="w", pady=6, padx=(0, 12))
            ttk.Entry(form, textvariable=variables[key], show="*" if key == secret else "").grid(row=index, column=1, sticky="ew", pady=6)

    def build_connection(self, parent):
        self.heading(parent, "QQ 连接", "只允许你本人的好友私聊。新项目独立运行，不接管旧 TRPG 的 QQ 实例。")
        self.fields(parent, self.qq_vars, (("bot_qq", "Bot QQ"), ("owner_qq", "本人 QQ"), ("service_port", "人格服务端口"),
                    ("napcat_port", "NapCat 页面端口"), ("astrbot_port", "AstrBot 页面端口"), ("onebot_port", "OneBot 端口")))
        bar = self.bar(parent)
        bar.pack(fill="x", pady=10)
        self.button(bar, "保存连接设置", self.save_connection, {"config", "lifecycle"}, True)
        self.button(bar, "检查连接", lambda: self.poll(force=True), {"poll"})
        self.button(bar, "打开 QQ 登录页", lambda: self.open_panel("napcat"))
        ttk.Label(parent, text="更改账号或端口前，请在故障排查中停止本项目全部服务，包括 QQ。所有地址绑定本机。", wraplength=700).pack(anchor="w", pady=10)

    def build_model(self, parent):
        self.heading(parent, "模型", "Chat Completions 兼容接口。地址填写到 /v1 等基础路径，程序追加 /chat/completions。")
        self.fields(parent, self.model_vars, (("base_url", "接口基础地址"), ("name", "模型名称"), ("api_key", "新密钥（留空保留）"),
                    ("timeout", "总超时（秒）"), ("max_tokens", "输出 token 上限"), ("hourly_limit", "每小时调用上限（0 为不限额）"),
                    ("max_input_chars", "输入字符上限")), secret="api_key")
        self.key_status = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.key_status).pack(anchor="w", pady=5)
        ttk.Checkbutton(parent, text="明确清除已保存密钥", variable=self.clear_key).pack(anchor="w")
        bar = self.bar(parent)
        bar.pack(fill="x", pady=10)
        self.button(bar, "保存配置", self.save_model, {"config"}, True)
        self.button(bar, "测试已保存配置", self.test_model, {"model-test"})
        ttk.Label(parent, text="保存不调用模型；测试会产生一次真实请求并计入限额。默认失败不自动重试。用量缺失时显示未知，不估算费用。", wraplength=700).pack(anchor="w", pady=10)
        group = ttk.LabelFrame(parent, text="私聊交互（默认保持原有行为）", padding=12)
        group.pack(fill="x", pady=12)
        ttk.Checkbutton(group, text="允许取消尚未开始生成的排队消息", variable=self.allow_queue_cancel).pack(anchor="w", pady=4)
        ttk.Checkbutton(group, text="QQ 等待时发送一次服务提示（不调用模型）", variable=self.qq_wait_notice).pack(anchor="w", pady=4)
        row = ttk.Frame(group)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text="等待阈值（5–120 秒）：").pack(side="left")
        ttk.Entry(row, textvariable=self.qq_wait_seconds, width=8).pack(side="left")
        bar = self.bar(group)
        bar.pack(fill="x")
        self.button(bar, "保存私聊设置", self.save_interaction, {"config"})
        ttk.Label(group, text="默认逐条有序；可到“输入与缓冲”按入口显式开启五秒缓冲。窗口显示完整回复。取消只作用于未开始的请求，不中断远端模型。", wraplength=650).pack(anchor="w", pady=6)
        self.build_organization_model(parent)
        self.build_selector(parent)

    def build_diagnostics(self, parent):
        self.heading(parent, "故障排查", "正常停止核对归属；强制停止会退出全部 QQ 与相关服务，不核对托管来源。日志仅含元数据。")
        for role, label in LABELS.items():
            row = ttk.Frame(parent)
            row.pack(fill="x")
            ttk.Label(row, text=label, width=12).pack(side="left")
            self.button(row, "启动", lambda r=role: self.start_one(r), {"lifecycle", "config"})
            self.button(row, "正常停止", lambda r=role: self.run_task("停止"+LABELS[r], {"lifecycle", "config"}, lambda _: self.controller.stop(r)), {"lifecycle", "config"})
            if role != "service":
                self.button(row, "打开管理页", lambda r=role: self.open_panel(r))
        bar = self.bar(parent)
        bar.pack(fill="x", pady=8)
        self.button(bar, "全部停止（含 QQ）", self.stop_everything, {"lifecycle", "config"})
        self.button(bar, "强制停止全部相关程序", self.force_stop_everything, {"emergency"})
        self.button(bar, "强制停止 QQ", self.force_stop_qq, {"emergency"})
        self.button(bar, "备份数据库", lambda: self.api_action("data.backup"), {"backup"})
        self.button(bar, "导出当前人格", lambda: self.api_action("data.export"), {"backup"})
        self.button(bar, "备份加密配置", self.backup_config, {"config"})
        self.button(bar, "查看 QQ 待处理请求", self.pending_requests, {"delivery-read"})
        self.button(bar, "QQ 未确认接收", self.recover_qq_inbox, {"inbox-read"})
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Combobox(row, textvariable=self.log_source, values=list(LABELS), state="readonly", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self.log_search).pack(side="left", fill="x", expand=True, padx=8)
        self.button(row, "刷新／查找日志", self.read_logs, {"logs"})
        self.log_text = scrolledtext.ScrolledText(parent, height=14, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, pady=8)
        self.delivery_note = tk.StringVar(value="待处理 QQ 投递可在启动服务后的连接检查中查看；不自动补发不确定消息。")
        ttk.Label(parent, textvariable=self.delivery_note, wraplength=700).pack(anchor="w")

    def load_config_fields(self):
        value = self.config.public()
        for key, var in self.qq_vars.items():
            var.set(str(value[key]))
        for key, var in self.model_vars.items():
            if key != "api_key":
                var.set(str(value["model"][key]))
        self.key_status.set("已保存密钥（Windows 加密保护）" if value["model"]["has_key"] else "尚未保存密钥")
        settings = value["interaction"]
        self.allow_queue_cancel.set(settings["cancel_queued"])
        self.qq_wait_notice.set(settings["qq_wait_notice"])
        self.qq_wait_seconds.set(str(settings["qq_wait_seconds"]))
        self.load_organization_fields(value)

    def save_interaction(self):
        try:
            seconds = int(self.qq_wait_seconds.get())
        except ValueError:
            self.notice.set("等待阈值须为 5–120 的整数秒。")
            return
        value = {"cancel_queued": self.allow_queue_cancel.get(), "qq_wait_notice": self.qq_wait_notice.get(), "qq_wait_seconds": seconds}
        self.run_task("保存私聊设置", {"config"}, lambda _: self.config.update({"interaction": value}),
                      lambda _: self.notice.set("私聊设置已保存；未调用模型。"))

    def start_one(self, role):
        self.run_task("启动" + LABELS[role], {"lifecycle", "config"}, lambda _: self.controller.start(role))

    def prepare_components(self):
        self.run_task("准备独立组件", {"lifecycle", "config"}, lambda _: self.prepare_stopped(),
                      lambda _: self.notice.set("独立组件已准备。填写连接配置后可启动。"))

    def prepare_stopped(self):
        if any(self.controller.owned(role) for role in LABELS):
            raise ValueError("准备组件前请先停止本项目全部服务。")
        return prepare(self.paths)

    def stop_everything(self):
        if messagebox.askyesno("停止本项目 QQ", "正常停止本项目全部服务，包括 QQ 登录进程？"):
            self.run_task("全部停止", {"lifecycle", "config"}, lambda _: self.controller.stop_all(True))

    def force_stop_everything(self):
        if not messagebox.askyesno("强制停止全部相关程序", "将直接结束全部 QQ（包括普通 QQ）、NapCat，以及人格/TRPG/AstrBot 的相关服务、宿主和其他管理窗口。\n\n不校验由谁启动，不等待请求完成；未保存的工作可能丢失。当前工作台保留。确定执行？"):
            return
        for operation in self.hub.active.values():
            operation.cancel.set()
        def done(report):
            self.session_id = None
            self.last_ready = False
            if report["success"]:
                self.status_text.set("相关程序已强制停止。")
                self.notice.set("强制停止完成；普通 QQ 也已退出。记录：" + report["report_file"])
            else:
                remaining = ", ".join(f"{p['name']} (PID {p['pid']})" for p in report["remaining"])
                self.notice.set("仍有程序未结束：" + remaining + "。查看停止报告；可能需要管理员权限。")
        self.run_task("强制停止全部相关程序", {"emergency"}, lambda _: self.controller.force_stop_all(), done, urgent=True)

    def force_stop_qq(self):
        if not messagebox.askyesno("强制停止 QQ", "将以管理员权限强制结束全部 QQ（包括普通 QQ）、NapCat 和 QQ 启动进程，包含外部进程。\n\n人格服务与消息服务保持运行；聊天草稿保留。出现 Windows 管理员授权时请确认。完成后需重新启动 QQ 并登录。确定执行？"):
            return
        def done(report):
            if report["success"]:
                self.notice.set("QQ 已强制停止；人格服务与消息服务保持运行。请在故障排查页启动 QQ 连接并重新登录。记录：" + report["report_file"])
            else:
                remaining = ", ".join(f"{p['name']} (PID {p['pid']})" for p in report["remaining"])
                self.notice.set("QQ 强停后仍有进程：" + remaining + "。请查看报告：" + report["report_file"])
            self.after(500, lambda: self.poll(force=True))
        self.run_task("强制停止 QQ", {"emergency"}, lambda _: self.controller.force_stop_qq(), done, urgent=True)

    def save_connection(self):
        values = {k: v.get().strip() for k, v in self.qq_vars.items()}
        try:
            for key in ("service_port", "napcat_port", "astrbot_port", "onebot_port"):
                values[key] = int(values[key])
        except ValueError:
            self.notice.set("端口须填写整数。")
            return
        def action(_):
            if any(self.controller.owned(role) for role in LABELS):
                raise ValueError("请先停止本项目全部服务，包括 QQ，再修改连接。")
            return self.config.update(values)
        self.run_task("保存连接", {"config", "lifecycle"}, action, lambda _: self.notice.set("连接设置已保存。"))

    def save_model(self):
        values = {k: v.get().strip() for k, v in self.model_vars.items()}
        try:
            for key in ("timeout", "max_tokens", "hourly_limit", "max_input_chars"):
                values[key] = int(values[key])
        except ValueError:
            self.notice.set("调用限制须填写整数。")
            return
        if not values["api_key"] and not self.clear_key.get():
            values.pop("api_key")
        def done(_):
            self.model_vars["api_key"].set("")
            self.clear_key.set(False)
            self.load_config_fields()
            self.notice.set("模型配置已保存，未调用模型。")
        self.run_task("保存模型", {"config"}, lambda _: self.config.update({"model": values}), done)

    def test_model(self):
        if messagebox.askyesno("真实模型测试", "使用已保存配置请求模型一次，可能计费，并计入每小时限额。继续？"):
            self.run_task("测试模型", {"model-test"}, lambda _: self.client.call("POST", "/v1/model-test", {}), self.watch_test)

    def watch_test(self, turn):
        if turn["status"] in ("queued", "running"):
            self.after(700, lambda: self.run_task("等待模型测试", {"model-test"}, lambda _: self.client.call("GET", "/v1/requests/"+turn["id"]), self.watch_test))
        else:
            self.notice.set("模型测试成功，用量：" + json.dumps(turn["usage"], ensure_ascii=False) if turn["status"] == "completed" else ERRORS.get(turn["error_code"], turn["error_code"]))

    def refresh_personas(self):
        epoch = self.view_epoch
        def done(rows):
            if self.closed or epoch != self.view_epoch:
                return
            self.personas = rows
            self.persona_list.delete(0, "end")
            for row in rows:
                self.persona_list.insert("end", ("● " if row["selected"] else "○ ") + row["name"] + " · " + row["id"][:8]+(' · 个人私有／禁止原体验收' if row.get('private_protected') else ''))
            self.chat_persona["values"] = [p["name"] + " · " + p["id"][:8] for p in rows]
            selected = next((p for p in rows if p["selected"]), None)
            # Archive reads need the active identity even before any chat
            # session has been selected. Reading the list creates no session.
            if self.persona_id is None and selected:
                self.persona_id = selected["id"]
            self.persona_choice.set(selected["name"] + " · " + selected["id"][:8] if selected else "")
        self.run_task("读取人格", {"persona-read"}, lambda _: self.client.manage("persona.list"), done, quiet=True)

    def create_template(self):
        self.run_task("创建模板", {"persona"}, lambda _: self.client.manage("persona.create"), lambda _: self.refresh_personas())

    def check_private_compatibility(self):
        row=self.selected_persona()
        if not row:return
        def read(_):
            from .private_personas import PrivatePersonas
            from .storage import Store
            private=PrivatePersonas(self.paths)
            record=next((r for r in private.load()['records'] if r.get('persona_id')==row['id']),None)
            return private.check(Store(self.paths),record['id']) if record else {'private':False,'message':'本机未登记为受保护的个人角色。'}
        self.run_task('私有角色只读检查',{'persona-read'},read,lambda value:self.show_json('私有兼容检查（零模型调用）',value))

    def import_persona(self):
        path = filedialog.askopenfilename(initialdir=self.paths.home / "config", filetypes=[("人格 JSON", "*.json")])
        if path:
            self.run_task("导入新的人格", {"persona"}, lambda _: self.client.manage("persona.create", spec=json.loads(Path(path).read_text(encoding="utf-8-sig"))), lambda _: self.refresh_personas())

    def selected_persona(self):
        selection = self.persona_list.curselection()
        return self.personas[selection[0]] if selection else None

    def use_selected_persona(self):
        row = self.selected_persona()
        if row:
            self.use_persona(row["id"])

    def select_persona(self):
        index = self.chat_persona.current()
        if index >= 0:
            self.use_persona(self.personas[index]["id"])

    def use_persona(self, persona_id):
        if not self.character_can_leave():return
        self.invalidate_context()
        self.invalidate_search()
        self.invalidate_archive()
        self.clear_archive_reference()
        self.view_epoch += 1
        self.delivery_rows = []
        self.delivery_tree.delete(*self.delivery_tree.get_children())
        self.show_delivery_parts()
        def done(_):
            self.session_id = None
            self.turns = []
            self.notice.set("人格已切换，正在读取会话。")
            self.refresh_personas()
            self.load_chat()
            self.search_memory()
        self.run_task("切换人格", {"persona", "chat"}, lambda _: self.client.manage("persona.use", persona_id=persona_id), done)

    def dialog_shell(self, title, size=(620, 330), minimum=(420, 240)):
        """Reserve a non-shrinking action row; only the content row expands."""
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        width = min(size[0], max(minimum[0], self.winfo_screenwidth()-80))
        height = min(size[1], max(minimum[1], self.winfo_screenheight()-140))
        dialog.geometry(f"{width}x{height}")
        dialog.minsize(minimum[0], minimum[1])
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        body = ttk.Frame(dialog, padding=(12, 12, 12, 0))
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        footer = ttk.Frame(dialog, padding=12)
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        return dialog, body, footer

    def show_json(self, title, value):
        dialog, body, footer = self.dialog_shell(title, (760, 540))
        body.rowconfigure(0, weight=1)
        box = scrolledtext.ScrolledText(body, wrap="word", width=1, height=1)
        box.grid(row=0, column=0, sticky="nsew")
        box.insert("1.0", json.dumps(value, ensure_ascii=False, indent=2))
        box.configure(state="disabled")
        ttk.Button(footer, text="关闭", command=dialog.destroy).grid(row=0, column=1)

    def show_persona(self):
        row = self.selected_persona()
        if row:
            self.run_task("读取设定", {"persona-read"}, lambda _: self.client.manage("persona.show", persona_id=row["id"]), lambda value: self.show_json("人格设定", value))

    def text_dialog(self, title, initial, action, *, resources=("management",), on_saved=None,
                    save_label="保存", categories=None):
        """Keep edits until a confirmed backend success, including after a failed save."""
        dialog, body, footer = self.dialog_shell(title)
        category = None
        text_row = 0
        if categories:
            category = ttk.Combobox(body, values=categories, state="readonly", width=1)
            category.current(0)
            category.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            text_row = 1
        body.rowconfigure(text_row, weight=1)
        box = scrolledtext.ScrolledText(body, wrap="word", width=1, height=1)
        box.grid(row=text_row, column=0, sticky="nsew")
        box.insert("1.0", initial)
        status = tk.StringVar(value="点击保存才会写入；Ctrl+Enter 也可保存。")
        status_label = ttk.Label(footer, textvariable=status, wraplength=380)
        status_label.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        footer.bind("<Configure>", lambda event: status_label.configure(wraplength=max(180, event.width-24)))
        saving = False

        def close():
            if saving:
                status.set("正在保存，请等待结果后再关闭。")
                return
            if box.get("1.0", "end-1c") != initial and not messagebox.askyesno(
                    "放弃未保存内容", "内容尚未保存。确定放弃并关闭？", parent=dialog):
                return
            dialog.destroy()

        def set_busy(busy):
            nonlocal saving
            saving = busy
            box.configure(state="disabled" if busy else "normal")
            save_button.configure(state="disabled" if busy else "normal")
            cancel_button.configure(state="disabled" if busy else "normal")
            if category:
                category.configure(state="disabled" if busy else "readonly")

        def failed(error):
            if dialog.winfo_exists():
                set_busy(False)
                status.set("保存失败，内容已保留：" + ERRORS.get(str(error), str(error)))

        def completed(result):
            if dialog.winfo_exists():
                dialog.destroy()
            self.notice.set(title + "：已保存。")
            if on_saved:
                on_saved(result)

        def save(_=None):
            if saving:
                return "break"
            value = box.get("1.0", "end-1c")
            if not value.strip():
                status.set("内容不能为空，请填写后保存。")
                return "break"
            selected_category = category.get() if category else None
            set_busy(True)
            status.set("正在保存…")
            operation = self.run_task("保存" + title, set(resources),
                lambda _: action(value, selected_category) if category else action(value),
                completed, failure=failed)
            if operation is None:
                set_busy(False)
                status.set("相关操作仍在进行，内容已保留，请稍后再保存。")
            return "break"

        cancel_button = ttk.Button(footer, text="取消", command=close)
        cancel_button.grid(row=1, column=1, padx=(0, 8))
        save_button = ttk.Button(footer, text=save_label, command=save, style="Primary.TButton")
        save_button.grid(row=1, column=2)
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.bind("<Control-Return>", save)
        box.focus_set()
        return dialog

    def edit_relationship(self):
        self.edit_profile_text('relationship')

    def load_chat(self, fresh=False, session_id=None):
        if fresh or (session_id and session_id != self.session_id):
            self.view_epoch += 1
        epoch = self.view_epoch
        def action(_):
            current = self.client.manage("session.select", channel="desktop", fresh=fresh, session_id=session_id)
            return current, self.client.manage("chat.history", session_id=current["session_id"])
        def done(result):
            if epoch != self.view_epoch:
                return
            if self.persona_id != result[0]['persona_id']:
                self.invalidate_sessions()
                self.invalidate_context()
                self.invalidate_search()
                self.clear_archive_reference()
            if self.session_id != result[0]["session_id"]:
                self.clear_archive_reference()
            self.session_id = result[0]["session_id"]
            self.persona_id = result[0]["persona_id"]
            self.chat_version = None
            self.turns = result[1]
            self.render_chat()
            self.notice.set("当前人格的窗口会话已读取。")
        self.run_task("读取会话", {"chat"}, action, done,
                      error_current=lambda: not self.closed and epoch == self.view_epoch)

    def choose_session(self):
        def done(rows):
            dialog, body, footer = self.dialog_shell("选择已有私聊会话", (780, 420))
            body.rowconfigure(0, weight=1)
            listing = tk.Listbox(body, width=1, height=1)
            listing.grid(row=0, column=0, sticky="nsew")
            for row in rows:
                listing.insert("end", row["created_at"] + " · " + row["id"])
            def select():
                if listing.curselection():
                    selected_id=rows[listing.curselection()[0]]["id"]
                    self.run_task("只读历史", {"history-read"}, lambda _: self.client.manage("chat.history",session_id=selected_id),lambda value:self.show_json("历史会话（只读）",value))
                    dialog.destroy()
            ttk.Button(footer, text="关闭", command=dialog.destroy).grid(row=0, column=1, padx=(0, 8))
            ttk.Button(footer, text="只读查看历史", command=select).grid(row=0, column=2)
        self.run_task("读取会话列表", {"chat"}, lambda _: self.client.manage("chat.sessions"), done)

    def render_chat(self):
        at_bottom = self.transcript.yview()[1] >= .98
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        for turn in self.turns:
            members = turn.get('input_members')
            if members:
                for member in members:
                    self.transcript.insert("end", "你：" + member['text'] + "\n")
            else:
                self.transcript.insert("end", "你：" + (turn["text"] or "") + "\n")
            if turn["status"] == "completed":
                self.transcript.insert("end", "人格：" + (turn.get('display_reply',turn["reply"]) or "") + "\n\n")
                self.transcript.insert("end", "用量：" + (json.dumps(turn["usage"], ensure_ascii=False) if turn["usage"] else "未知") + "\n\n")
            else:
                phase = turn.get("phase", turn["status"])
                label = PHASES.get(phase, phase)
                if turn.get("queue_position"):
                    label += " · 当前顺位 " + str(turn["queue_position"])
                self.transcript.insert("end", "[" + label + "] " + ERRORS.get(turn["error_code"], turn["error_code"] or "") + "\n")
                if turn.get("recovery"):
                    self.transcript.insert("end", turn["recovery"] + "\n")
                self.transcript.insert("end", "\n")
            timing = turn.get("timings", {})
            context = turn.get('context') or {}
            if context.get('coverage_gap_turns'):
                self.transcript.insert('end', f"上下文提示：{context['coverage_gap_turns']}轮较早对话未被本次摘要或近期窗口覆盖。\n")
            if context.get('summary_partial'):
                self.transcript.insert('end', '上下文提示：摘要因输入预算仅使用部分条目。\n')
            measured = [(label, timing.get(key)) for key, label in (
                ("queue_seconds", "排队"), ("provider_seconds", "模型"), ("delivery_seconds", "投递"))]
            if any(value is not None for _, value in measured):
                self.transcript.insert("end", "耗时：" + " / ".join(f"{label} {value:.2f}s" for label, value in measured if value is not None) + "\n\n")
        linked_batches={t.get('input_batch_id') for t in self.turns}
        for batch in getattr(self,'chat_batches',[]):
            if batch['id'] in linked_batches:continue
            if batch['session_id']!=self.session_id or batch['persona_id']!=self.persona_id:continue
            for member in batch['members']:self.transcript.insert('end','你：'+member['text']+'\n')
            self.transcript.insert('end','['+BATCH_LABELS.get(batch['status'],batch['status'])+f" · {batch['remaining_ms']/1000:.1f} 秒]\n\n")
        self.transcript.configure(state="disabled")
        if at_bottom:
            self.transcript.see("end")

    def send_chat(self):
        text = self.chat_input.get("1.0", "end-1c")
        if not text.strip():
            return
        payload = {"channel": "desktop", "text": text, "request_id": identifier()}
        persona_id = self.persona_id or next((p["id"] for p in self.personas if p["selected"]), None)
        if not persona_id:
            self.notice.set("请先选中人格并恢复或新建会话。")
            return
        from .presentation import chat_target_matches
        if not chat_target_matches(persona_id,self.personas,self.chat_persona.current()):
            self.notice.set('人格选择与会话不同步，草稿已保留。请先点击恢复当前会话或重新选择人格。')
            return
        selected_reference=getattr(self,"chat_reference",None)
        if selected_reference and selected_reference["persona_id"]!=persona_id:
            self.notice.set("引用属于原人格，请清除或重新选择。");return
        if selected_reference:payload["reference"]=dict(selected_reference["target"])
        payload["persona_id"] = persona_id
        if self.session_id:
            payload["session_id"] = self.session_id
        epoch = self.view_epoch
        def done(turn):
            if epoch != self.view_epoch:
                self.notice.set("消息已提交给原人格；切回该人格可查看结果。")
                return
            # A user may already be typing the next message while HTTP is in flight.
            if self.chat_input.get("1.0", "end-1c") == text and getattr(self,"chat_reference",None)==selected_reference:
                self.chat_input.delete("1.0", "end")
                if selected_reference:self.clear_archive_reference()
            self.session_id = turn["session_id"]
            self.selected_request = turn.get("turn_id") if "receipt_id" in turn else turn["id"]
            self.load_chat(session_id=self.session_id)
            if 'receipt_id' in turn:self.refresh_batches()
            else:self.watch_turn(turn, epoch)
        self.run_task("提交消息", {"send"}, lambda _: self.client.submit_input(payload), done)

    def refresh_conversation(self):
        """Fast conversation refresh is independent of slow OS/service polling."""
        if self.closed:
            return
        session_id, epoch = self.session_id, self.view_epoch
        if session_id and self.current_page == "chat":
            version = self.chat_version
            def done(snapshot):
                if epoch == self.view_epoch and session_id == self.session_id:
                    self.chat_version = snapshot["version"]
                    if snapshot["turns"] is not None and snapshot["turns"] != self.turns:
                        self.turns = snapshot["turns"]
                        self.render_chat()
            self.run_task("更新对话", {"conversation-refresh"}, lambda _: self.client.manage("chat.snapshot", session_id=session_id, version=version), done, quiet=True)
        self.after(500, self.refresh_conversation)

    def recover_submissions(self):
        def done(rows):
            dialog, body, footer = self.dialog_shell("本机未确认消息", (800, 440))
            body.rowconfigure(0, weight=1)
            listing = tk.Listbox(body, width=1, height=1)
            listing.grid(row=0, column=0, sticky="nsew")
            for row in rows:
                turn = row["turn"]
                listing.insert("end", ("已受理：" + PHASES.get(turn.get("phase"), turn["status"]) if turn else "未查到受理记录") + " · " + row["payload"]["text"][:70])
            def select():
                return rows[listing.curselection()[0]] if listing.curselection() else None
            def details():
                row = select()
                if row:
                    self.show_json("本机消息与受理结果", row)
            def resubmit():
                row = select()
                if row and row["turn"] is None:
                    self.run_task("按原标识重新提交", {"send"}, lambda _: self.client.submit_input(row["payload"]),
                                  lambda value: self.notice.set("原消息已受理，未创建新请求标识：" + value.get("receipt_id",value.get("id",""))))
                    dialog.destroy()
            ttk.Button(footer, text="查看详情", command=details).grid(row=0, column=0, padx=5)
            ttk.Button(footer, text="提交选中未受理消息", command=resubmit).grid(row=0, column=1, padx=5)
            ttk.Button(footer, text="关闭", command=dialog.destroy).grid(row=0, column=2)
            self.notice.set(f"检查 {len(rows)} 条本机记录；已受理消息不会重复调用，未受理需显式提交。")
        self.run_task("找回未确认消息", {"submission-recovery"}, lambda _: self.client.reconcile(), done)

    def recover_qq_inbox(self):
        def done(rows):
            dialog, body, footer = self.dialog_shell("QQ 未确认接收（不会自动重放）", (820, 450))
            body.rowconfigure(0, weight=1)
            listing = tk.Listbox(body, width=1, height=1)
            listing.grid(row=0, column=0, sticky="nsew")
            for row in rows:
                listing.insert("end", ("已找回受理" if row["turn"] else "未查到受理记录") + " · " + row["payload"]["text"][:70])
            def selected():
                return rows[listing.curselection()[0]] if listing.curselection() else None
            def details():
                row = selected()
                if row:
                    self.show_json("QQ 接收与受理状态", row)
            def submit():
                row = selected()
                if row and row["turn"] is None and messagebox.askyesno("重新提交原 QQ 消息", "将按当前选中人格提交这条尚未受理的原消息；可能调用模型并计费。继续？", parent=dialog):
                    request_id = row["payload"]["request_id"]
                    self.run_task("提交未受理 QQ 消息", {"send"}, lambda _: self.client.manage("inbox.submit", request_id=request_id),
                                  lambda _: self.notice.set("已按原消息标识提交；账号目标未改变。"))
                    dialog.destroy()
            ttk.Button(footer, text="查看详情", command=details).grid(row=0, column=0, padx=5)
            ttk.Button(footer, text="按当前人格提交未受理消息", command=submit).grid(row=0, column=1, padx=5)
            ttk.Button(footer, text="关闭", command=dialog.destroy).grid(row=0, column=2)
        self.run_task("核对 QQ 未确认接收", {"inbox-read"}, lambda _: self.client.manage("inbox.check"), done)

    def request_queue(self):
        def done(rows):
            dialog, body, footer = self.dialog_shell("请求队列与详情", (820, 470))
            body.rowconfigure(0, weight=1)
            listing = tk.Listbox(body, width=1, height=1)
            listing.grid(row=0, column=0, sticky="nsew")
            for row in rows:
                listing.insert("end", PHASES.get(row.get("phase"), row["status"]) + " · " + row["channel"] + " · " + row["id"][:8])
            def selected():
                return rows[listing.curselection()[0]] if listing.curselection() else None
            def details():
                row = selected()
                if row:
                    self.run_task("读取请求详情", {"request-details"}, lambda _: self.client.call("GET", "/v1/requests/"+row["id"]),
                                  lambda value: self.show_json("请求阶段、耗时与恢复建议", value))
            def cancel():
                row = selected()
                if row:
                    self.run_task("取消排队消息", {"request-cancel"}, lambda _: self.client.call("POST", f"/v1/requests/{row['id']}/cancel", {}),
                                  lambda _: self.notice.set("排队消息已取消；正在生成的请求不会被此操作中断。"))
                    dialog.destroy()
            ttk.Button(footer, text="查看详情", command=details).grid(row=0, column=0, padx=5)
            ttk.Button(footer, text="取消选中排队消息", command=cancel).grid(row=0, column=1, padx=5)
            ttk.Button(footer, text="关闭", command=dialog.destroy).grid(row=0, column=2)
        self.run_task("读取请求队列", {"request-list"}, lambda _: self.client.manage("chat.requests"), done)

    def watch_turn(self, turn, epoch):
        if self.closed or epoch != self.view_epoch:
            return
        # One session-level refresh loop covers all in-flight requests, without
        # repeatedly rewriting session selection or loading the whole transcript.
        self.chat_version = None
        self.notice.set("消息已受理：" + PHASES.get(turn.get("phase"), turn["status"]))

    def retry_chat(self):
        failed = next((t for t in reversed(self.turns) if t["status"] in ("failed", "interrupted")), None)
        if failed:
            epoch = self.view_epoch
            self.run_task("手动重试", {"send"}, lambda _: self.client.call("POST", f"/v1/requests/{failed['id']}/retry", {}), lambda value: self.watch_turn(value, epoch))

    def pending_requests(self):
        def done(rows):
            dialog, body, footer = self.dialog_shell("QQ 待处理请求", (860, 430), (560, 240))
            body.rowconfigure(0, weight=1)
            listing = tk.Listbox(body, width=1, height=1)
            listing.grid(row=0, column=0, sticky="nsew")
            for row in rows:
                listing.insert("end", row["turn_id"] + " · " + row["status"] + " · 投递 " + row["state"])
            def selected():
                return rows[listing.curselection()[0]] if listing.curselection() else None
            def details():
                row = selected()
                if row:
                    self.run_task("读取 QQ 请求", {"delivery-read"}, lambda _: self.client.call("GET", "/v1/requests/" + row["turn_id"]),
                                  lambda value: self.show_json("请求详情", value))
            def retry():
                row = selected()
                if row and row["status"] in ("failed", "interrupted"):
                    self.run_task("手动重试 QQ 请求", {"send"}, lambda _: self.client.call("POST", f"/v1/requests/{row['turn_id']}/retry", {}),
                                  lambda _: self.notice.set("已提交重试。插件会投递生成完成且尚未发送的回复；不确定投递不会自动补发。"))
                    dialog.destroy()
            ttk.Button(footer, text="查看详情", command=details).grid(row=0, column=1, padx=(0, 8))
            ttk.Button(footer, text="重试失败生成（可能计费）", command=retry).grid(row=1, column=1, pady=(8, 0))
            ttk.Button(footer, text="关闭", command=dialog.destroy).grid(row=0, column=2)
        self.run_task("读取 QQ 待处理", {"delivery-read"}, lambda _: self.client.manage("delivery.pending"), done)

    def search_memory(self):
        query = self.memory_query.get()
        def done(rows):
            self.memory_rows = rows
            self.memories.delete(*self.memories.get_children())
            for row in rows:
                self.memories.insert("", "end", iid=row["id"], values=(row["category"], row["text"]))
        self.run_task("检索记忆", {"memory"}, lambda _: self.client.manage("memory.search", query=query), done)

    def memory_id(self):
        selected = self.memories.selection()
        return selected[0] if selected else None

    def show_memory(self):
        memory_id = self.memory_id()
        if memory_id:
            self.run_task("读取记忆历史", {"memory"}, lambda _: self.client.manage("memory.show", memory_id=memory_id), lambda value: self.show_json("记忆来源与历史", value))

    def source_dialog(self, callback):
        def done(sessions):
            dialog, body, footer = self.dialog_shell("选择来源事件", (780, 470), (480, 260))
            body.rowconfigure(1, weight=1)
            choices = ttk.Combobox(body, state="readonly", width=1, values=[s["id"] for s in sessions])
            choices.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            tree_frame = ttk.Frame(body)
            tree_frame.grid(row=1, column=0, sticky="nsew")
            tree_frame.columnconfigure(0, weight=1)
            tree_frame.rowconfigure(0, weight=1)
            tree = ttk.Treeview(tree_frame, columns=("role", "text"), show="headings", height=1)
            tree.heading("role", text="发言者")
            tree.heading("text", text="原始文本")
            tree.column("role", width=80, stretch=False)
            tree.column("text", width=250, minwidth=80)
            tree.grid(row=0, column=0, sticky="nsew")
            scroll = ttk.Scrollbar(tree_frame, command=tree.yview)
            scroll.grid(row=0, column=1, sticky="ns")
            tree.configure(yscrollcommand=scroll.set)
            def loaded(value):
                if not dialog.winfo_exists():
                    return
                tree.delete(*tree.get_children())
                for row in value["events"]:
                    tree.insert("", "end", iid=row["id"], values=(row["role"], row["text"]))
            def load(_=None):
                if not dialog.winfo_exists():
                    return
                session_id = choices.get()
                if session_id:
                    self.run_task("读取来源", {"sources"}, lambda _: self.client.manage("session.show", session_id=session_id), loaded)
            choices.bind("<<ComboboxSelected>>", load)
            def selected():
                if tree.selection():
                    callback(tree.selection()[0])
                    dialog.destroy()
            ttk.Button(footer, text="选择事件", command=selected).grid(row=0, column=1, padx=(0, 8))
            ttk.Button(footer, text="关闭", command=dialog.destroy).grid(row=0, column=2)
            def note():
                session_id = choices.get()
                if session_id:
                    self.text_dialog("人工笔记（外部知识请注明出处与获知时间）", "",
                        lambda text: self.client.manage("event.append", session_id=session_id, role="note", text=text),
                        resources=("sources",), on_saved=loaded_note)
            def loaded_note(_):
                load()
            ttk.Button(footer, text="在此会话添加人工笔记", command=note).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
            if sessions:
                choices.current(0)
                load()
        self.run_task("选择来源", {"sources"}, lambda _: self.client.manage("session.list"), done)

    def add_memory(self):
        from .models import CATEGORIES
        def source_selected(event_id):
            self.text_dialog("显式录入记忆", "",
                lambda text, category: self.client.manage("memory.add", category=category, text=text, source_event_id=event_id),
                resources=("memory",), on_saved=lambda _: self.search_memory(),
                save_label="保存记忆", categories=CATEGORIES)
        self.source_dialog(source_selected)

    def revise_memory(self):
        memory_id = self.memory_id()
        row = next((m for m in self.memory_rows if m["id"] == memory_id), None)
        if row:
            self.source_dialog(lambda event_id: self.text_dialog("修订记忆（产生新版本）", row["text"],
                lambda text: self.client.manage("memory.revise", memory_id=memory_id, text=text, source_event_id=event_id),
                resources=("memory",), on_saved=lambda _: self.search_memory()))

    def retract_memory(self):
        memory_id = self.memory_id()
        if memory_id and messagebox.askyesno("撤回记忆", "停止召回这条记忆？原记录仍会保留。"):
            self.api_action("memory.retract", memory_id=memory_id)

    def api_action(self, action, **args):
        def done(value):
            self.notice.set("操作完成" + ("：" + value["path"] if isinstance(value, dict) and "path" in value else ""))
            if action.startswith("memory."):
                self.search_memory()
        self.run_task("执行操作", {"memory" if action.startswith("memory.") else "management"}, lambda _: self.client.manage(action, **args), done)

    def backup_config(self):
        def action(_):
            target = self.paths.home / "backups" / ("config-" + identifier() + ".json")
            atomic_json(target, self.config.load())
            return str(target)
        self.run_task("备份加密配置", {"config"}, action, lambda path: self.notice.set("加密配置已备份：" + path))

    def open_panel(self, role):
        def action(_):
            url = self.controller.management_url(role)
            try:
                opened = webbrowser.open(url)
            except Exception:
                raise ValueError("无法启动默认浏览器，请检查 Windows 的默认浏览器设置。") from None
            if not opened:
                raise ValueError("默认浏览器未接受打开请求，请检查 Windows 的默认浏览器设置。")
        self.run_task("打开管理页面", {"panel"}, action,
                      lambda _: self.notice.set("已请求默认浏览器打开管理页。"))

    def read_logs(self):
        source, query = self.log_source.get(), self.log_search.get()
        def action(_):
            path = self.paths.home / "logs" / (source + ".jsonl")
            self.tail.states.pop(str(path), None)
            text = self.tail.read(path)
            return "\n".join(line for line in text.splitlines() if not query or query.lower() in line.lower())
        def done(text):
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", text or "暂无匹配的元数据日志。")
            self.log_text.configure(state="disabled")
        self.run_task("读取日志", {"logs"}, action, done, quiet=True)

    def poll(self, force=False):
        if self.closed:
            return
        def done(states):
            self.refresh_quotas()
            self.status_text.set("\n".join(LABELS[role] + "：" + value["state"] for role, value in states.items()))
            bot = states["astrbot"]
            self.login_text.set("QQ 账号：" + ("已核对" if bot.get("login") else "等待核对") +
                                "　OneBot：" + ("在线" if bot.get("onebot") else "未连接" if "onebot" in bot else "状态待核对") +
                                ("（" + bot["reason"] + "）" if bot.get("reason") else ""))
            ready = states["service"]["state"] == "运行"
            if ready and not self.last_ready:
                self.refresh_personas()
            self.last_ready = ready
            if ready:
                self.run_task("检查待投递", {"delivery-read"}, lambda _: self.client.manage("delivery.pending"),
                    lambda rows: self.delivery_note.set("QQ 待处理投递：" + str(len(rows)) + " 条（不会自动补发不确定消息）"), quiet=True)
        self.run_task("检查状态", {"poll"}, lambda _: self.controller.snapshot(), done, quiet=True)
        if not force:
            self.after(5000, self.poll)

    def cancel_tasks(self):
        for operation in self.hub.active.values():
            if operation.name == "启动服务":
                operation.cancel.set()
        self.notice.set("已请求取消后续启动步骤；当前安全操作会完成。")

    def tick(self):
        if self.closed:
            return
        for operation, kind, value, callback, quiet in self.hub.drain():
            if kind == "progress":
                self.notice.set(value)
            elif callback:
                try:
                    callback(value)
                except Exception:
                    self.notice.set("界面刷新未完成，请重新读取对应页面。")
            elif kind == "error" and not quiet:
                self.notice.set(ERRORS.get(str(value), str(value)))
        active = list(self.hub.active.values())
        self.operation_text.set("；".join(op.name + " · " + op.phase for op in active if op.name != "检查状态") or "没有正在执行的操作")
        resources = set().union(*(op.resources for op in active)) if active else set()
        for button, required in self.buttons:
            if button.winfo_exists():
                state="disabled" if required & resources or "emergency" in resources else "normal"
                if str(button.cget("state")) != state:button.configure(state=state)
        if any(op.name != "检查状态" for op in active):
            self.progress.start(15)
        else:
            self.progress.stop()
        self.after(100, self.tick)

    def close_window(self):
        if self.shutdown_started:
            return
        if not self.character_can_leave():return
        if self.managed_lifecycle:
            self.shutdown_started = True
            self.closed = True
            self.notice.set('正在强制停止全部服务，完成后关闭控制台。')
            for button, _ in self.buttons:
                if button.winfo_exists():button.configure(state='disabled')
            for operation in self.hub.active.values():
                operation.cancel.set()
            for timer in self.tk.splitlist(self.tk.call('after', 'info')):
                self.tk.call('after', 'cancel', timer)
            self.shutdown_result = None
            def stop():
                try:
                    self.controller.end_console()
                    from .emergency import force_stop_all_elevated
                    report = force_stop_all_elevated(self.paths)
                    self.shutdown_result = (bool(report['success']), report)
                except Exception as exc:
                    self.shutdown_result = (False, str(exc))
            threading.Thread(target=stop, name='console-shutdown', daemon=True).start()
            self.after(100, self.finish_console_shutdown)
            return
        self.destroy_console()

    def finish_console_shutdown(self):
        if self.shutdown_result is None:
            self.after(100, self.finish_console_shutdown)
            return
        success, result = self.shutdown_result
        if success:
            self.destroy_console()
        else:
            self.shutdown_started = False
            self.notice.set('全部服务尚未停止，控制台保留。请再次关闭以重试。' +
                            (str(result.get('report_file', '')) if isinstance(result, dict) else str(result)))

    def destroy_console(self):
        self.closed = True
        self.hub.close()
        for timer in self.tk.splitlist(self.tk.call("after", "info")):
            self.tk.call("after", "cancel", timer)
        self.destroy()


def run(paths):
    import os
    from .emergency import force_stop_all_elevated
    from .workbench_errors import report_failure
    lock = controller = window = None
    stage = 'initialize_paths'
    failed = False
    try:
        paths.initialize()
        stage = 'instance_lock'
        lock = InstanceLock(paths.home / "runtime/desktop.lock")
        stage = 'console_lease'
        controller = ServiceController(paths)
        lease = controller.start_console()
        # Pre-lease hosts and a crashed console must not obstruct the new one.
        stage = 'resident_check'
        legacy_running = controller.has_resident_processes()
        if lease.get('previous_stale') or legacy_running:
            stage = 'cleanup_legacy'
            report = force_stop_all_elevated(paths)
            if not report['success']:
                raise RuntimeError('遗留服务未完全停止：' + report['report_file'])
        stage = 'create_workbench'
        window = Workbench(paths, managed_lifecycle=True, controller=controller)
        stage = 'mainloop'
        window.mainloop()
    except Exception as exc:
        failed = True
        report_failure(paths, exc, stage)
        raise
    finally:
        try:
            if controller is not None:
                controller.end_console()
            if window is not None and not window.shutdown_started:
                force_stop_all_elevated(paths)
        except Exception as exc:
            failed = True
            report_failure(paths, exc, 'console_cleanup')
            raise
        finally:
            try:
                if lock is not None:
                    lock.close()
            except Exception as exc:
                failed = True
                report_failure(paths, exc, 'lock_release')
                raise
            finally:
                # No worker thread may leave a headless process after the window,
                # including an unexpected Tk mainloop failure.
                if window is not None:
                    os._exit(0 if window.shutdown_started and not failed else 1)
