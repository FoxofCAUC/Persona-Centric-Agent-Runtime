# CR3-A.1 流式真实冒烟验收手册

2026-11-03。准备基线为 `a398410 / 0.7.6.dev19` 加本轮冒烟验证器补充。本手册首组已获用户授权并于 `7a10879` 执行：**5次真实调用，辅助第1次重试恢复，3主回复首尝成功，verify --smoke通过；临时鉴权及目录清理被自动策略拒绝，收尾未完成。** 见[实际结果](../CR3-A.1-流式单组真实冒烟结果.md)。下文前置和命令保留为原执行规程，不得据此再次执行已有real-v1。

## 前置与范围

Windows离线准备已通过：150项局部检查、R0的34零差异、22组完整模拟及首组限组演练。详见[准备记录](../CR3-A.1-Windows冒烟前准备.md)。真实派发前，仍须确认首组、90秒空闲读超时、bearer及不发送reasoning_effort；本轮准备不构成真实调用授权。

范围只包含已冻结的G11-process-r1：一次联合辅助，随后raw/essential/full三个消费条件，相同实际请求仅在组内别名引用。人工输入、候选、来源、主提示、参数及旧成功引用均沿用冻结包，不新增测试消息、不换题。准确正文在D盘 `real-v1/materials.json` 和 `prepared.json`，仅在准备成功后产生，不在公开手册复制付费输出。

预计4次物理调用，首次失败后最多3次原生重试、间隔1/2/4秒，单组最坏16次；执行器不加第二层重试。无模型生成额度、总时限或新总预算；保留单段8000字符/总正文12000字符回复协议。90秒仅限制流式连续空闲，不限制完整生成耗时。

## 获准后的操作

以下保留本批已执行的操作规程供核对，**不能原样重跑real-v1**，也不要加入自动启动。未来批次须另行授权及新目录；目录已存在、配置改变或冻结源码不一致时停止，不覆盖或重启。

```powershell
$taskRepo = 'C:\Users\ZhuanZ（无密码）\Documents\ChatGPT\Persona-Centric Agent Runtime'
$taskPython = 'D:\Persona-Centric Agent Runtime\.venv\Scripts\python.exe'
$taskPackage = 'D:\Persona-Centric Agent Runtime\exports\acceptance\PCAR-dialogue-refactor\CR3-A.1-real-consumption-ablation-stream-4'
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:TEMP = 'D:\Persona-Centric Agent Runtime\tmp'
$env:TMP = $env:TEMP
Set-Location -LiteralPath $taskRepo

# 先检查最新交接和源码冻结，确认真实目录不存在。
& $taskPython -B scripts/dialogue_stream_recovery.py prepare --real --root "$taskPackage\real-v1"
if ($LASTEXITCODE -ne 0) { throw '真实准备失败，停止。' }

# 仅在已明确授权本次单组冒烟时执行。
& $taskPython -B scripts/dialogue_stream_recovery.py execute --real --root "$taskPackage\real-v1" --max-groups 1
if ($LASTEXITCODE -ne 0) { throw '冒烟停止，保留断点，不自动重跑。' }

& $taskPython -B scripts/dialogue_stream_recovery.py verify --root "$taskPackage\real-v1" --smoke
if ($LASTEXITCODE -ne 0) { throw '冒烟证据不完整，停止并核查。' }
```

`prepare --real`只在隔离临时目录写DPAPI凭据sidecar。不得将其写入普通证据、Git或人工材料。普通 `verify` 用于完整批次，不能拿它验收刻意只跑一组的批次。

## 检查表

| 项目 | 通过依据 | 当前状态 |
| --- | --- | --- |
| Windows局部检查及R0 | 150去重通过、34零差异 | 准备通过 |
| 完整及单组模拟 | 86＋4次mock；完整verify及verify --smoke通过 | 准备通过 |
| 真实准备 | 新目录、冻结配置/源码/候选/来源，旧27预留及unknown保留 | 通过 |
| 联合辅助 | 流式完整结束，经原联合v2协议校验，实际来源绑定一致 | 第1次重试恢复，首尝协议失败保留 |
| 主回复 | 三条件完成或合法组内别名，实际请求等于Manifest | 3唯一主回复首尝通过 |
| 流式证据 | 成功回执有stream=true与正事件计数，无stream_downgraded；记录首事件/推理/正文时序，不要求必须存在推理事件 | 5次均有流式证据，无降级 |
| 账目 | 每次预留都有transport和原生终态，失败尝试计入，成功不重放，零悬挂预留 | 本轮5次通过；旧未知仍保留 |
| 停止 | 首组完成后stopped/group_limit_reached；失败不能冒充正常限组停止 | 通过，无后续组派发 |
| 隔离及清理 | 零正式/QQ/私人副本；结束线程与锁，清除凭据，核对临时目录归属和保护边界 | 隔离通过；鉴权及临时目录清理被策略拒绝，未完成 |
| 自然度与人物收益 | 本次传输冒烟不作完整效果结论 | 尚未验证 |

## 失败、收尾和接续

任何重试耗尽、来源/配置/权限/账目异常、用户取消或写入失败都停止整批，不临时换协议、模型、空闲时限或输出上限。质量差但协议合法的结果保留，不能作为重生成理由。断流后的供应商用量未知时保留未知；进程中断后只记录断点，禁止伪造终态或重发未知位置。

保存收据、请求指纹、流式数字诊断、原生审计和停止原因。结束本批线程、连接并清理凭据；临时运行目录的清理须先核对绝对路径与owner标记，只处理本批归属目录，不碰正式数据、旧模板、权重和封存证据。无法清理应如实标记未完成，不绕过自动策略。

通过只意味着“本组流式调用及消费链路有真实证据”，不表示76条件或固定12组复核完成，也不证明自然度改善。后续21组需另行授权和新目录，导入已结清首组，不重放成功；首组不完整时不得默认续跑。
