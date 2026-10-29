"""Selector configuration is independent; ordinary inspection never calls cloud."""
import json
import tkinter as tk
from tkinter import ttk,messagebox
from .models import identifier

class SelectorUI:
    def build_selector(self,parent):
        group=ttk.LabelFrame(parent,text='角色候选选择模型（独立配置）',padding=12);group.pack(fill='x',pady=10)
        cfg=self.config.public()['character_selector']
        self.selector_vars={k:tk.StringVar(value=str(cfg[k])) for k in ('base_url','name','timeout','max_tokens','max_input_chars','hourly_limit','daily_limit')}
        self.selector_vars['api_key']=tk.StringVar();self.selector_enabled=tk.BooleanVar(value=cfg['enabled'])
        self.selector_clear=tk.BooleanVar(value=False);self.selector_epoch=0
        for var in (*self.selector_vars.values(),self.selector_enabled,self.selector_clear):
            var.trace_add('write',lambda *_:setattr(self,'selector_epoch',self.selector_epoch+1))
        self.fields(group,self.selector_vars,(('base_url','接口基础地址'),('name','模型名称'),('api_key','新密钥（留空保留）'),('timeout','超时（最多60秒）'),('max_tokens','输出tokens'),('max_input_chars','输入字符上限'),('hourly_limit','选择调用/小时（0不限）'),('daily_limit','选择调用/24小时（0不限）')),secret='api_key')
        ttk.Checkbutton(group,text='启用独立选择模型',variable=self.selector_enabled).pack(anchor='w')
        ttk.Checkbutton(group,text='明确清除选择模型密钥',variable=self.selector_clear).pack(anchor='w')
        self.selector_notice=tk.StringVar(value='密钥已保存' if cfg['has_key'] else '未设置密钥')
        ttk.Label(group,textvariable=self.selector_notice,wraplength=700).pack(anchor='w',pady=4)
        bar=self.bar(group);bar.pack(fill='x')
        self.button(bar,'保存选择配置',self.save_selector,{'config'})
        self.button(bar,'复制主模型配置',self.copy_selector,{'config'})
        self.button(bar,'测试选择模型（计费一次）',self.test_selector,{'selector-test'})
        self.button(bar,'最近选择调用',self.selector_records,{'selector-read'})

    def save_selector(self):
        try:
            data={k:(v.get().strip() if k in ('base_url','name','api_key') else int(v.get())) for k,v in self.selector_vars.items()}
            if not data['api_key'] and not self.selector_clear.get():data.pop('api_key')
            data['enabled']=self.selector_enabled.get()
        except ValueError:self.selector_notice.set('限制项须填写整数');return
        epoch=self.selector_epoch
        def done(value):
            if epoch!=self.selector_epoch:return
            self.selector_vars['api_key'].set('');self.selector_clear.set(False)
            self.selector_notice.set('选择配置已保存；新建请求使用新配置，旧请求快照保持。')
            self.refresh_quotas()
        self.run_task('保存选择配置',{'config'},lambda _:self.config.update({'character_selector':data}),done,
            failure=lambda error:self.selector_notice.set(str(error)) if epoch==self.selector_epoch else None)

    def copy_selector(self):
        epoch=self.selector_epoch
        def done(value):
            if epoch!=self.selector_epoch:return
            cfg=value['character_selector']
            for k in ('base_url','name'):self.selector_vars[k].set(str(cfg[k]))
            self.selector_notice.set('已独立复制并保存主模型地址、名称和密钥；不会跟随后续主模型修改。')
        self.run_task('复制选择配置',{'config'},lambda _:self.config.copy_selector_model(),done)

    def test_selector(self):
        if not self.persona_id:return
        key=(self.persona_id,self.view_epoch,self.selector_epoch);op=identifier()
        def done(value):
            if key!=(self.persona_id,self.view_epoch,self.selector_epoch):return
            self.show_json('选择模型真实测试（仅测试已保存配置）',value);self.refresh_quotas()
        self.run_task('选择模型真实测试',{'selector-test'},lambda _:self.client.call('POST','/v1/manage',{'action':'character.selector-test','args':{'persona_id':key[0],'operation_id':op}},timeout=65),done,error_current=lambda:key==(self.persona_id,self.view_epoch,self.selector_epoch))

    def selector_records(self):
        if not self.persona_id:return
        key=(self.persona_id,self.view_epoch)
        self.run_task('读取选择记录',{'selector-read'},lambda _:self.client.manage('character.selector-records',persona_id=key[0]),
            lambda rows:self.show_json('选择调用、用量与降级',rows) if key==(self.persona_id,self.view_epoch) else None,error_current=lambda:key==(self.persona_id,self.view_epoch))
