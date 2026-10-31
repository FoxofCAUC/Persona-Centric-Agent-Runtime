# Computer Use反复Esc中止排查记录

日期：2026-10-31（Asia/Shanghai）。用户明确未操作电脑；工具的physical Escape文本不能被当作已证实的用户动作。

## 结论

**当前已恢复可用；首次误触发根因未证实。** 现象更符合旧执行回合停止状态残留，这是本机前后对照推断，不是官方确认的版本缺陷。没有找到PCAR主动发送Esc的证据。

## 实测证据

- 两组各约10秒、200次Esc状态采样：高位按下及低位新按键计数均为0；第二组系统最后输入时间不变。未读取其他键、安装键盘钩子或注入输入。
- 当前进程与输入桌面均Default，窗口站WinSta0。未发现系统Scancode Map；不能据此排除所有驱动或间歇性硬件问题。
- 检查运行时、脚本、测试、桥接及实验目录，未找到所扫描的SendInput、keybd_event、键盘注入或发送Escape代码。
- 普通Node执行正常。本轮新执行回合列窗成功，文件管理器截图成功，标题栏无害单击及读取回执成功；未打开、修改或执行文件。
- 上一长执行回合重置JS后仍曾立即中止；本轮未重启ChatGPT、改系统或项目配置已恢复。不能宣传“重置JS即可修复”。
- 客户端26.1027.3384.0／插件26.1027.30824。只读检查相关客户端启动/状态日志，未找到首次误触发来源的确证；没有导出完整聊天、日志或浏览器存储。

Windows定义GetAsyncKeyState高位为当前按下，低位不宜作可靠按下判据；短时采样不等于永久排除硬件问题。[Microsoft官方说明](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getasynckeystate)

## 后续处理

当前可以正常使用。再次立即中止时先保留时间/原文并停止该轮Windows输入，在新的执行回合只读列窗验证；仅重置JS不保证恢复。新回合仍失败时由用户正常退出重开ChatGPT，再复验；当前无需重装或重启Windows。不修改或绕过Esc急停、应用权限和安全设置。

M7.6.1作者确认/预览验收及正式部署本轮未续跑，仍从[交接](../HANDOFF.md)继续。Explorer诊断不能代替PCAR阶段验收。

[OpenAI官方文档](https://learn.chatgpt.com/docs/computer-use)要求Windows目标位于活动桌面，本轮已核对；未声称官方确认了停止锁存缺陷。

详细脱敏数据在D盘运行目录exports/diagnostics/ComputerUse-ESC-20261031/report.json。未提交推送，PCAR正式库、政策及服务状态未改。首个报告写入脚本因本机无tzdata失败，已改用UTC+8记录，未安装依赖；该问题与Esc无证据关联。
