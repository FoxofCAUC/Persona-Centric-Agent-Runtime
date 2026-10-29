"""Desktop view and explicit controls for the M6.3 delivery ledger."""
import tkinter as tk
import json
from tkinter import messagebox, scrolledtext, ttk

from .models import identifier


class DeliveryUI:
    def init_delivery_vars(self):
        self.delivery_rows = []
        self.delivery_parts = []

    def build_delivery(self, parent):
        self.heading(parent, "回复投递", "按原人格和目标查看平台发送状态；不确定片段需要人工核对，系统不会自动重发。")
        bar = ttk.Frame(parent); bar.pack(fill="x", pady=5)
        self.button(bar, "刷新", self.refresh_deliveries, {"delivery-read"})
        self.button(bar, "表达与节奏预览", self.preview_reply, {"delivery-read"})
        self.delivery_tree = ttk.Treeview(parent, columns=("channel", "state", "parts", "blocked"), show="headings", height=8)
        for key, title, width in (("channel", "入口", 70), ("state", "状态", 110), ("parts", "已发送 / 总数", 130), ("blocked", "阻塞原因", 220)):
            self.delivery_tree.heading(key, text=title); self.delivery_tree.column(key, width=width)
        self.delivery_tree.pack(fill="both", expand=True)
        self.delivery_tree.bind("<<TreeviewSelect>>", lambda _: self.show_delivery_parts())
        self.delivery_part_tree = ttk.Treeview(parent, columns=("state", "semantic", "part", "attempt", "message_id"), show="headings", height=8)
        for key, title, width in (("state", "片段状态", 100), ("semantic", "语义段", 70), ("part", "顺序", 70), ("attempt", "尝试", 70), ("message_id", "平台消息 ID", 200)):
            self.delivery_part_tree.heading(key, text=title); self.delivery_part_tree.column(key, width=width)
        self.delivery_part_tree.pack(fill="both", expand=True, pady=(8, 0))
        self.delivery_part_tree.bind("<<TreeviewSelect>>", lambda _: self.show_delivery_detail())
        self.delivery_detail = scrolledtext.ScrolledText(parent, height=7, wrap="word", state="disabled")
        self.delivery_detail.pack(fill="x", pady=7)
        actions = self.bar(parent); actions.pack(fill="x")
        self.button(actions, "核对为已处理并继续", lambda: self.resolve_delivery("resolve", True), {"delivery-write"})
        self.button(actions, "核对后暂停队列", lambda: self.resolve_delivery("resolve", False), {"delivery-write"})
        self.button(actions, "跳过此段及本批余段", lambda: self.resolve_delivery("skip", True), {"delivery-write"})
        self.button(actions, "解除队列阻塞", lambda: self.resolve_delivery("release", True), {"delivery-write"})
        self.button(actions, "显式重发单段", self.retry_delivery_part, {"delivery-write"})
        self.button(actions, "取消本批可省略余段", self.cancel_optional_reply, {"delivery-write"})

    def refresh_deliveries(self):
        persona_id = self.persona_id; epoch = self.view_epoch
        if not persona_id:
            return
        def done(rows):
            if epoch != self.view_epoch or persona_id != self.persona_id:
                return
            selected = self.delivery_tree.selection()
            self.delivery_rows = rows
            self.delivery_tree.delete(*self.delivery_tree.get_children())
            for stream in rows:
                parts = stream["parts"]
                sent = sum(part["state"] == "sent" for part in parts)
                self.delivery_tree.insert("", "end", iid=stream["id"], values=(
                    "QQ" if stream["channel"] == "qq" else "窗口", stream["state"], f"{sent} / {len(parts)}", stream["blocked_reason"] or ""))
            if selected and self.delivery_tree.exists(selected[0]):
                self.delivery_tree.selection_set(selected[0])
            self.show_delivery_parts()
        self.run_task("读取回复投递", {"delivery-read"}, lambda _: self.client.manage("delivery.list", persona_id=persona_id), done, quiet=True)

    def selected_delivery(self):
        selection = self.delivery_tree.selection()
        return next((row for row in self.delivery_rows if selection and row["id"] == selection[0]
                     and row['persona_id'] == self.persona_id), None)

    def selected_delivery_part(self):
        stream = self.selected_delivery(); selection = self.delivery_part_tree.selection()
        return next((part for part in stream["parts"] if selection and part["id"] == selection[0]), None) if stream else None

    def show_delivery_parts(self):
        stream = self.selected_delivery()
        selected = self.delivery_part_tree.selection()
        self.delivery_part_tree.delete(*self.delivery_part_tree.get_children())
        self.delivery_parts = stream["parts"] if stream else []
        for part in self.delivery_parts:
            state=('新输入取消' if part.get('cancel_reason')=='new_input' else '已取消' if part.get('cancel_reason') else
                   f"等待 {part['remaining_ms']} ms" if part.get('remaining_ms') else
                   {'pending':'待领取','sending':'已领取','sent':'已发送','uncertain':'结果不确定','skipped':'已跳过'}.get(part['state'],part['state']))
            self.delivery_part_tree.insert("", "end", iid=part["id"], values=(state, part["semantic_index"], part["part_index"], part["attempt"], part["platform_message_id"] or ""))
        if selected and self.delivery_part_tree.exists(selected[0]):
            self.delivery_part_tree.selection_set(selected[0])
        self.show_delivery_detail()

    def show_delivery_detail(self):
        self.delivery_detail.configure(state="normal"); self.delivery_detail.delete("1.0", "end")
        part = self.selected_delivery_part()
        if part:
            self.delivery_detail.insert("end", f"片段 {part['part_index']} · SHA-256 {part['text_hash']}\n{part['text']}\n\n发送尝试：\n")
            for attempt in part.get("attempts",[]):
                self.delivery_detail.insert("end", f"#{attempt['attempt_no']} {attempt['state']} · 平台ID {attempt['platform_message_id'] or '无'} · {attempt['error'] or '无错误'} · {attempt['started_at']}\n")
            self.delivery_detail.insert("end", f"\n最近错误：{part['error'] or '无'}")
            self.delivery_detail.insert('end',f"\n类别：{part.get('content_kind')}；可省略：{bool(part.get('cancel_if_new_input'))}；计划等待：{part.get('wait_ms') or 0} ms\n取消原因：{part.get('cancel_reason') or '无'}；触发输入：{part.get('trigger_event_id') or '无'}")
        self.delivery_detail.configure(state="disabled")

    def cancel_optional_reply(self):
        stream=self.selected_delivery();part=self.selected_delivery_part();pid=self.persona_id;epoch=self.view_epoch
        if not stream or not part:return
        if not messagebox.askyesno('取消可省略余段','只取消整个消息尚未领取且明确可省略的余段，原文仍保留。继续？'):return
        def done(_):
            if epoch==self.view_epoch and pid==self.persona_id:self.refresh_deliveries()
        self.run_task('取消可省略余段',{'delivery-write'},lambda _:self.client.manage('reply.cancel',persona_id=pid,batch_id=part['reply_batch_id'],expected_revision=stream['revision'],operation_id=identifier()),done)

    def preview_reply(self):
        pid=self.persona_id;epoch=self.view_epoch;part=self.selected_delivery_part()
        if not pid:return
        dialog,body,footer=self.dialog_shell('表达与节奏预览',(760,600))
        ttk.Label(body,text='选择已保存片段可预览原批次；或填写完整模型输出，预览不会调用模型。').pack(anchor='w')
        source=scrolledtext.ScrolledText(body,height=8);source.pack(fill='both',expand=True)
        output=scrolledtext.ScrolledText(body,height=12,state='disabled');output.pack(fill='both',expand=True)
        def run(saved=False):
            if epoch!=self.view_epoch or pid!=self.persona_id:return
            target=self.selected_delivery_part()
            target_id=target['id'] if target else None
            if saved and (not part or target_id!=part['id']):return
            draft=source.get('1.0','end-1c')
            args={'batch_id':part['reply_batch_id']} if saved and part else {'raw':source.get('1.0','end-1c'),'mode':'natural_structured'}
            def done(value):
                if epoch!=self.view_epoch or pid!=self.persona_id or not dialog.winfo_exists():return
                current=self.selected_delivery_part()
                if (current['id'] if current else None)!=target_id or source.get('1.0','end-1c')!=draft:return
                output.configure(state='normal');output.delete('1.0','end');output.insert('end',json.dumps(value,ensure_ascii=False,indent=2));output.configure(state='disabled')
            self.run_task('预览表达',{'delivery-read'},lambda _:self.client.manage('reply.preview',persona_id=pid,**args),done)
        ttk.Button(footer,text='预览填写内容',command=run).pack(side='left')
        if part:ttk.Button(footer,text='预览选中批次',command=lambda:run(True)).pack(side='left')
        ttk.Button(footer,text='关闭',command=dialog.destroy).pack(side='right')

    def resolve_delivery(self, action, allow_continue):
        stream = self.selected_delivery(); part = self.selected_delivery_part(); persona_id = self.persona_id
        if not stream or not part or stream["persona_id"] != persona_id:
            return
        if action == "skip" and not messagebox.askyesno("跳过片段", "将保留原记录，并跳过本回复批次的此片段和后续片段。是否继续？"):
            return
        if action == "resolve":
            outcome = "后续发送将继续。" if allow_continue else "后续发送仍将暂停，须显式解除队列阻塞。"
            if not messagebox.askyesno("核对投递结果", "请先在 QQ 目标会话核对该片段，再记录为已处理。" + outcome):
                return
        if action == "release" and not messagebox.askyesno("解除阻塞", "只有已核对或已跳过阻塞片段后才能解除队列阻塞。是否继续检查？"):
            return
        self._delivery_action(stream, part, action, allow_continue)

    def retry_delivery_part(self):
        stream = self.selected_delivery(); part = self.selected_delivery_part()
        if not stream or not part:
            return
        if not messagebox.askyesno("可能重复发送", "平台可能已经收到此片段。仅重发这一片，存在重复显示的可能。仍要继续？"):
            return
        self._delivery_action(stream, part, "retry", True, "管理员显式重发；可能重复")

    def _delivery_action(self, stream, part, action, allow_continue, reason=None):
        persona_id = self.persona_id; epoch = self.view_epoch
        def done(_):
            if epoch == self.view_epoch and persona_id == self.persona_id:
                self.refresh_deliveries()
        self.run_task("处置回复片段", {"delivery-write"}, lambda _: self.client.manage(
            "delivery.resolve", stream_id=stream["id"], part_id=part["id"], action=action,
            expected_revision=stream["revision"], operation_id=identifier(), persona_id=persona_id,
            reason=reason, allow_continue=allow_continue), done)

    def poll_deliveries(self):
        if self.closed:
            return
        if self.persona_id and self.current_page == "delivery":
            self.refresh_deliveries()
        self.after(1800, self.poll_deliveries)
