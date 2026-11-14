# PCAR dev38 第二台电脑定向复验

2026-11-15。适用已完成dev37环境与人工迁移演练的第二台电脑。用户已通过C5对话审阅，本次不重复审批；正式业务B1—B4仍待完成。

## 更新内容

- 新High计划至少4096输出tokens，整理至少8192；原有更高值保留，人物回复参数不变。
- 已收到的截断响应记为已知失败并保留用量；真正未知结果仍不重放，旧失败记录不改写。
- PowerShell路径探针显式支持已有基础Python，并使用入口一致的进程执行参数。
- 只读核对工具新增任务状态、输出预算和已保存底稿的数量，不输出底稿或聊天正文。

本机已有42项关联和43项共享检查通过，后续4项重叠补验通过。另有2次真实High整理使用人工材料取得完整结果，共13382tokens；未新增真实Jev/Search/Fetch/人物回复。原回执复用核对底稿保存，未再次收费。第二台电脑的实际网页材料仍需按下文复验。

## 先关闭再登记原实例

正常退出旧工作台。将新的PCAR-0.9.0.dev38-Windows-x64-desktop.zip解压到独立程序目录，例如C:\PCAR 程序-dev38。核对包外配套SHA256，不覆盖正在运行的源码，不搬动数据，不重建虚拟环境，不重新导入人工包。

以下使用第二台电脑报告中的人工实例路径；需要时替换为实际位置：

```powershell
$Program = 'C:\PCAR 程序-dev38'
$Data = 'C:\PCAR 验收\人工迁移'
$Python = Join-Path $Data '.venv\Scripts\python.exe'
$Evidence = 'C:\PCAR 验收资料\记录'
Set-Location -LiteralPath $Program
$env:PCAR_PYTHON = $null
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $Data -BasePython $Python -RegisterExisting
& $Python -B -c 'import persona_runtime;print(persona_runtime.__version__)'
```

通过标准：显示0.9.0.dev38，定位仍指向原人工实例，原身份、历史、已配置主模型/Jev及授权保留。登记不会触发新的模型调用。此处显式传已有解释器，不依赖py启动器，也不修改系统环境变量。

## E4探针复验

```powershell
$env:PERSONA_RUNTIME_HOME = $Data
& $Python -B -m unittest tests.test_decoupling.InstallationTests.test_windows_powershell_51_preserves_non_ascii_paths -v
```

通过标准：单项OK、退出码0，无需更改系统执行策略。A1—A9已通过的其他项目不要求全部再跑；如果这次出现新的安装或目录故障，只补验受影响项。

## E5整理复验

先保存只读摘要，再打开同一实例：

```powershell
$Check = Join-Path $Program 'scripts\pcar_acceptance_check.py'
& $Python -B $Check --home $Data --out (Join-Path $Evidence 'dev38-问答前.json')
.\scripts\Start-Workbench.ps1 -RuntimeHome $Data
```

保留当前已配置的模型、密钥和按需联网开关，不修改人物回复上限。创建两条新的消息；不要点原C2/C4b的重试按钮，也不要恢复旧blocked_unknown任务。

建议的新问题如下，实际发送会产生正常模型/检索费用：

1. “新查一下三角洲行动单人撤离时的装备取舍和撤离顺序，社区实战经验也看看。先说重点，详细材料留给追问。”
2. “新查一下杭州西湖半日游怎么安排步行和公共交通、如何留返程余地。先给省事方案，详细材料留给追问。”

每条只保留第一次返回，不为改进措辞重生成。完成后正常关闭工作台，再保存摘要：

```powershell
& $Python -B $Check --home $Data --out (Join-Path $Evidence 'dev38-问答后.json')
```

通过标准：

- 两条都有正常Jev判断，需要联网时由High计划，实际检索来源仍可包含社区和其他网站。
- 新增conversation_organize任务的task_statuses.completed和received_rows分别增加2，organized_notes.with_notes增加2；新任务输出预算至少8192，思考强度high。
- 主回复照常继续，未修改原主模型回复设置；原C2/C4b的失败、未知状态与用量仍保留，没有新的物理重放。
- 新公共外发仍走system_proxy；已知tokens、未知用量和估算费用如实记录。正常退出仍释放所属组件。

如果检索本身未取得可用材料，可能没有整理任务。记录原因，不能拿“没有进入整理”代替E5成功；也不要求每条问题必须Fetch或只采信官网。

如果新整理仍被截断，保存该次任务与用量、状态和结束原因后停止重复尝试；返回记录即可，不无界提高预算或重放未知调用。文件同名时核对工具会拒绝覆盖，复验记录请改用带轮次的新文件名。

## 更新结论

E4：作者于2026-11-15确认第二台全部通过。

E5：作者于2026-11-15确认第二台全部通过，后续第三台也一次通过。

C5用户审阅：已完成，2026-11-15用户明确通过。

正式B1—B4：作者于2026-11-15确认第三台正式迁移顺利完成，阶段结项；旧的待办保留在历史记录中。本页保留dev38复验步骤，首发部署使用根目录AGENT_INSTALL.md。
