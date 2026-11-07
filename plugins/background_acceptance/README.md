# PCAR 后台百轮验收插件

独立监督进程驱动隐藏的真实PCAR工作台，通过正常本机API聊天、整理记忆、关闭与重启。无需Computer Use、鼠标键盘或持续前台窗口。只创建人工角色和空白数据库，复制已授权的模型配置到D盘独立测试根。

在源码根使用项目D盘Python环境：

```powershell
python -B -m plugins.background_acceptance prepare --simulated --out <新的模拟目录>
python -B -m plugins.background_acceptance run --out <同一模拟目录>
python -B -m plugins.background_acceptance prepare --out <新的真实目录>
python -B -m plugins.background_acceptance run --out <同一真实目录>
python -B -m plugins.background_acceptance status --out <真实目录>
python -B -m plugins.background_acceptance stop --out <真实目录>
python -B -m plugins.background_acceptance report --out <真实目录>
```

v2真实run要求同源码完整百轮模拟通过；百轮还要求同源码真实小批通过（由 `real-smoke-current.json` 指向成功批次）。小批使用 `prepare --smoke --out <新的小批目录>`，然后正常run。Windows外层进程也可通过`Start-Process -WindowStyle Hidden`启动，输出保存到D盘；工作台子进程自动隐藏。无计划任务、开机自启或自动启动QQ。

100次主回复，10次正常模型记忆整理，25/50/75轮后实际进程退出重启；七处事实追问跨度13/27/51/52/76/82/94轮，两个更正事实分别在52和82轮后追问。输入清单在materials.py；评分答案仅供监督进程事后判定，不进入运行时请求。整理采用现有来源校验；v2的虚拟人格专用策略允许对明确用户更正、唯一有效目标和当前版本进行模拟审核，以模型实际候选正文替换。真实小批第5轮前保留待确认状态，之后批准；百轮第20轮起允许该审核。生产默认人工审核不变。它是插件明确触发，不能据此认定每日定时整理已验证。

每个批次只启动一次。已有started记录禁止重跑，包括已成功和结果未知的位置。语义答错保留，传输/来源/权限/账本异常则停止。stop会在当前请求结算后停止。报告包含调用、用量和逐轮回复；金额及未知失败用量不推算。

全部数据保存在D盘stage-6-memory-repair-20261108证据及tmp/pcar-background-acceptance归属目录。人工对话可保留用于复核；带凭据的配置不得提交。详细操作与验收表见[记忆修复手册](../../docs/acceptance/阶段6-记忆修复验收手册.md)。旧v1的100轮结果仍在stage-6-background-100，不能用v2覆盖或重跑。

已知格式拒绝的记忆整理单独记失败，不重生成。下面仅为v1历史接续合同，v2不用于跨版本恢复旧批次。若同版本在完整50轮后因此停止，可显式运行`continue --out <原目录>`，它保留原owner与全部旧证据并只发送尚未受理的后续位置；仅允许完整关闭的检查点，未知收费或不完整回执禁止接续。
