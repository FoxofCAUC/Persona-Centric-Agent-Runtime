# PCAR 第二台电脑验收流程与通过标准

适用版本：0.9.0.dev37 桌面解耦候选。文档日期：2026年11月14日。

这份文档用于在另一台真实 Windows 11 x64 电脑上，验证 PCAR 能独立安装、保留业务资料、重新配置模型，并延续原有桌面对话体验。按“空环境安装 → 人工迁移演练 → 正式资料核对 → 实际对话评阅”的顺序执行。

**当前第二台电脑的结果尚未填写。本机已通过的检查只是准备证据，不能替代本次验收。** 全程使用原 `启动PCAR.bat` 作为日常入口；不接 QQ，不启用主动外发，不把“回答绝对正确”或“只引用官网”作为通过条件。

## 一 验收结论怎么判定

| 结论 | 必须具备的证据 | 可以做什么 |
| --- | --- | --- |
| 环境验收通过 | A1—A5 全通过 | 证明新机能独立准备、定位和运行桌面 |
| 人工迁移演练通过 | A6—A9 全通过 | 证明工具能保留人工业务、拒绝覆盖并完成回退 |
| 正式业务迁移通过 | B1—B4 全通过 | 证明实际业务在新机保留、凭据与授权重新建立 |
| 本阶段最终验收通过 | 上述项目全部通过，且 C1—C5 的实际链路通过、用户接受对话质量 | 才能将跨机迁移标记为完成；提交推送仍另行处理 |

未执行、缺少证据、配置未完成或等待用户评阅，都填“待完成”；不能填“通过”。没有正式迁移包时，可以先完成环境和人工演练，但不能写“正式业务迁移通过”。

普通网站一时打不开、某条攻略有争议、模型观点与官方不同，不能单独作为环境迁移失败。应观察能否继续检索、换用其他网站并自然交流。人物质量由用户评判，不用固定字数、逐数字核对、强制中立或“暂时不能认定”等审核话术替代评阅。

## 二 带到新机的文件与环境

需要三个文件：

1. `PCAR-0.9.0.dev37-Windows-x64-desktop.zip`：程序及已校验的公开本地模型，不含正式业务、API Key或旧虚拟环境。
2. `PCAR-artificial-business-migration.pcar-migration.zip`：人工演练资料，包含1个人物、1个会话、1条原文、1条记忆、1条M9笔记。
3. `PCAR-第二台电脑验收资料.zip`：本文、浏览器阅读版、记录表和只读核对工具 `pcar_acceptance_check.py`。

本候选尚未提交推送，远端 main 不等同于这个验收包；不要用重新克隆远端来替换它。另有正式业务迁移包时，单独私下传输，不放进公开程序包，也不上传Git。

### A1 文件与设备检查

新机使用 Windows 11 x64、含Tkinter的Python 3.12 64位和uv；本机验证基线为Python 3.12.10、uv 0.13.5。Python/uv安装使用其正常安装方式，不复制旧电脑的 `.venv`。运行所需依赖由PCAR准备脚本按 `uv.lock` 安装。

在新机PowerShell中核对两个程序资料包：

```powershell
Get-FileHash -LiteralPath 'C:\PCAR传输\PCAR-0.9.0.dev37-Windows-x64-desktop.zip' -Algorithm SHA256
Get-FileHash -LiteralPath 'C:\PCAR传输\PCAR-artificial-business-migration.pcar-migration.zip' -Algorithm SHA256
```

桌面包SHA256：

```text
a8005d996e18b60ac5ad00c64e91dd80a61a6a2ad9f90d29a43d968bd917a7d8
```

人工迁移包SHA256：

```text
6badfaccfed2a25626260e760f76a91b852ac7a99b2943cf4a6005e4fcbfa249
```

**通过标准：** 两个哈希一致；设备、Python位数/版本符合要求；所用包确为dev37。哈希不一致时先重新传输，不继续迁移。

### A2 目录和代理准备

建议将程序解压到 `C:\PCAR 程序`，验收资料解压到 `C:\PCAR 验收资料`。准备以下三个互不包含的数据目录；后面命令中的路径均可替换为自己的本地绝对路径。

| 目录 | 用途 |
| --- | --- |
| `C:\PCAR 验收\空桌面` | 首次启动、取消选择及目录记忆 |
| `C:\PCAR 验收\人工迁移` | 人工资料导入、回退、真实公开问题测试 |
| `C:\PCAR 正式数据` | 正式业务迁移；人工演练通过后才使用 |

源码和数据目录不能互相包含。使用本地独立目录，不使用盘符根目录、网络共享、目录链接或旧电脑路径。新机不要求有D盘，也不能依赖原机的用户名、其他项目或运行中的服务。

在新机Windows系统设置中确认已启用手动HTTP/HTTPS代理，代理程序正在运行。PCAR不自动修改系统设置；当前不支持仅PAC的配置。代理地址使用新机实际设置，不照搬原机端口。没有有效系统代理时应停止公共访问，不能退回直连。

随后在程序根目录打开PowerShell，建立本次操作变量：

```powershell
$ProgramRoot = 'C:\PCAR 程序'
$SmokeRoot = 'C:\PCAR 验收\空桌面'
$MoveRoot = 'C:\PCAR 验收\人工迁移'
$LiveRoot = 'C:\PCAR 正式数据'
$Fixture = 'C:\PCAR传输\PCAR-artificial-business-migration.pcar-migration.zip'
$Check = 'C:\PCAR 验收资料\pcar_acceptance_check.py'
$Evidence = 'C:\PCAR 验收资料\记录'
New-Item -ItemType Directory -Path $Evidence -Force | Out-Null
Set-Location -LiteralPath $ProgramRoot
```

这些变量只用于当前PowerShell，不需要修改系统环境变量。**通过标准：** 程序与业务分开，路径可以包含中文和空格；新机系统代理就绪，后续公共连接均有代理路线记录。

## 三 新机环境验收

### A3 准备空桌面环境

先准备环境但不登记默认目录，以便下一步实际测试目录选择：

```powershell
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $SmokeRoot -LocalModelSource '.\public-model' -NoRegister
```

命令完成后检查退出状态：

```powershell
$LASTEXITCODE
.\scripts\Manage-Migration.ps1 -RuntimeHome $SmokeRoot -Action Doctor -Network
```

**通过标准：** 准备命令退出码为0；`doctor` 的 `ready=true`；解释器位于 `$SmokeRoot\.venv`，模型校验和公开下载检查通过，网络路线为 `system_proxy`。没有安装或启动AstrBot/NapCat，没有模型收费调用。

`doctor`要在工作台关闭时运行；窗口运行中服务占用自身端口，不应将这种占用误记为安装失败。`doctor`只证明环境条件，不证明密钥可用、迁移完成或人物质量通过。

准备失败时保留 `logs\environment-prepare.log`，修正代理、Python或磁盘问题后对同一未完成安装重试。已识别的完整实例不会被默认重新同步覆盖；不要为排错删除正式数据。

### A4 原入口选择 取消和记忆

确保解压出来的程序目录没有从旧机复制 `.pcar-local.json`。如当前验收PowerShell带有其他实例的路径覆盖，仅清除本次进程的覆盖：

```powershell
$env:PERSONA_RUNTIME_HOME = $null
$env:PCAR_PYTHON = $null
& (Join-Path $ProgramRoot '启动PCAR.bat')
```

第一次在目录选择框点“取消”。核对没有启动新桌面、没有登记新目录。再次从同一入口打开，选择已准备的 `$SmokeRoot`，待窗口出现后正常退出。第三次双击同一入口，不提供参数。

**通过标准：** 取消不会创建另一个实例；选择后能打开dev37桌面；源码根定位文件只记本机路径，不含密钥；第三次自动回到同一目录，能看到同一份本机数据，没有退回旧D盘路径或其他Python。两个窗口不能同时争用同一实例。

本例提前准备了空桌面目录，因此取消时不要求该目录消失；要求的是取消不新增实例、不改定位。

### A5 正常退出与所属组件

正常关闭桌面，等待退出结束；不使用任务管理器结束进程来代替正常退出。打开一个与PCAR无关的普通程序作观察，不需要启动QQ。

从只读核对工具查看退出摘要：

```powershell
$SmokePython = Join-Path $SmokeRoot '.venv\Scripts\python.exe'
& $SmokePython -B $Check --home $SmokeRoot --out (Join-Path $Evidence 'A5-空桌面退出.json')
```

**通过标准：** 摘要中 `exit.processes_released=true`、控制台状态为 `closed`；工作台及所属服务退出，无无主后台常驻，无收费调用；其他程序保持运行。如新机恰有另一个PCAR实例，也应保持运行。

再次打开后仍能正常退出。界面自动化若失败，允许用户手动完成操作；记录实际操作和退出证据，不把失败截图当成通过。不要直接展示整份 `console.json` 或 `last-exit.json`，其中可能含本机令牌。

## 四 人工资料迁移演练

### A6 先准备目标 再预览

关闭空桌面。准备独立的人工迁移目录，**此时先不要打开它的工作台、创建人物或发送消息**：

```powershell
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $MoveRoot -LocalModelSource '.\public-model' -NoRegister
$MovePython = Join-Path $MoveRoot '.venv\Scripts\python.exe'
& $MovePython -B $Check --home $MoveRoot --out (Join-Path $Evidence 'A6-导入前.json')
.\scripts\Manage-Migration.ps1 -RuntimeHome $MoveRoot -Action Preview -Package $Fixture
& $MovePython -B $Check --home $MoveRoot --out (Join-Path $Evidence 'A6-预览后.json')
```

**通过标准：** 预览返回 `applied=false`，显示人工包的5类业务计数各1；预览前后均没有新增人物、原文、记忆、M9笔记或模型调用，目标身份没有改变。

这里用的是已经准备好的空目标，因此不要求目标目录不存在。若要验证“未创建的目标预览后仍不落盘”，应使用已准备解释器直接运行CLI并显式指定另一个尚不存在的目标；不能用依赖目标解释器的管理脚本冒充这项检查。

### A7 实际导入和未启用状态

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $MoveRoot -Action Import -Package $Fixture
& $MovePython -B $Check --home $MoveRoot --out (Join-Path $Evidence 'A7-人工导入后.json')
```

**通过标准：** `applied=true`，状态为 `needs_configuration`；5类业务计数各1；主模型和Jev没有继承旧密钥，没有旧模型授权；调用账目行数仍为0。不是“导入后立即自动联网”。

现在显式打开这个目标：

```powershell
.\scripts\Start-Workbench.ps1 -RuntimeHome $MoveRoot
```

只读查看人工人物、会话和记忆。原文应为“人工迁移演练：我喜欢清淡口味。”，记忆为“人工偏好：清淡口味”。M9人工笔记可由核对工具的 `knowledge_control` 计数与指纹确认；不要因工作台没有展示原始存储条目就认定资料丢失。

在尚未启用的人工目标尝试发送一句普通话，应得到迁移配置提示，不能产生模型调用。然后正常关闭。

**通过标准：** 人工内容和原编号保留；等待本机配置期间可读历史，但不会自动重放旧任务或收费；未配置的“发送失败”本身不是验收失败，若发生真实外发才是失败。

### A8 拒绝重复导入和回退

对已导入的目标再次执行一次 `Import`。应明确拒绝，不能覆盖已有业务；错误可能提示已有迁移记录或目标非空，不要求固定报错文案。核对原业务计数和指纹没有变化。

随后在未启用、工作台已关闭时回退：

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $MoveRoot -Action RollbackPreview
.\scripts\Manage-Migration.ps1 -RuntimeHome $MoveRoot -Action Rollback
& $MovePython -B $Check --home $MoveRoot --out (Join-Path $Evidence 'A8-回退后.json')
```

**通过标准：** 回退预览不改业务；实际回退返回 `restored=true`，恢复到A6的导入前空目标，虚拟环境仍保留。导入前没有数据库时，回退后数据库不存在是正常结果。

再次导入同一人工包应成功。**已启用且可能有新业务的目标不允许使用这类空实例回退**；不要为了通过测试去清空或覆盖它。

### A9 异常检查和本机专项

保留人工资料的失败记录。破损包或非空目标须被拒绝；导入中断后未完成的目标须保持不可外发，不能被记成成功。普通验收不要求断电、强杀进程或破坏正式库。

开发核对人员可以在关闭工作台后运行包内17项隔离专项，其中包括导入失败恢复、路径、凭据、授权、旧任务和历史序号保护：

```powershell
$env:PERSONA_RUNTIME_HOME = $MoveRoot
& $MovePython -B -m unittest tests.test_decoupling -v
```

**通过标准：** 专项结果为17项通过、无失败，或由开发人员说明新机实际失败及修复证据。此命令只用人工隔离数据，不代替前面已经要求的真实桌面操作，不运行历史全仓测试或付费问答。

## 五 正式业务迁移验收

人工演练通过后再执行本节。原机保留原业务作为回退依据；导出后暂停原机继续聊天，避免导出后又新增内容导致两边难以比较。本节不改变原人物设定、原有记忆状态或已批准的人物表达方向。

### B1 原机基线与业务导出

先正常关闭原机工作台。将同一份只读核对工具带到原机，用原机当前解释器和实际业务根执行；下面路径仅是原机现状示例：

```powershell
$OldRoot = 'D:\Persona-Centric Agent Runtime'
$OldPython = Join-Path $OldRoot '.venv\Scripts\python.exe'
& $OldPython -B '核对工具的绝对路径' --home $OldRoot --out '原机基线记录的绝对路径.json'
```

在有dev37工具的程序目录执行：

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $OldRoot -Action Export
```

使用实际返回的导出文件名，不复制带星号的命令去猜文件。对导出包运行 `Get-FileHash`，记录SHA256，私下传输至新机后再次核对。正式包与人工包不能混用。

**通过标准：** 原机基线可读，SQLite完整性正常；导出不改原人物、原文、记忆、原件与授权配置；新旧两边的迁移包哈希一致。包内没有旧DPAPI凭据、模型授权文件、进程租约或旧 `.venv`。

原机基线记录只含计数、指纹和必要运行摘要；按本机验收资料保管。不要发送人物原文、正式聊天、完整配置或密钥给别人来证明通过。

### B2 新机正式导入与保留性比较

在新机准备另一个空目标，不复用人工演练目标：

```powershell
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $LiveRoot -LocalModelSource '.\public-model' -NoRegister
$LivePython = Join-Path $LiveRoot '.venv\Scripts\python.exe'
$PrivatePackage = '新机上正式业务迁移包的绝对路径'
.\scripts\Manage-Migration.ps1 -RuntimeHome $LiveRoot -Action Preview -Package $PrivatePackage
.\scripts\Manage-Migration.ps1 -RuntimeHome $LiveRoot -Action Import -Package $PrivatePackage
& $LivePython -B $Check --home $LiveRoot --out (Join-Path $Evidence 'B2-正式导入后.json')
```

**在第一次打开新机正式工作台之前**，使用工具比较原机基线与新机导入后记录：

```powershell
& $LivePython -B $Check --compare '传到新机的原机基线记录.json' (Join-Path $Evidence 'B2-正式导入后.json')
```

| 检查对象 | 通过标准 |
| --- | --- |
| 人物 会话 原文 记忆 | 所选业务表计数和内容指纹一致，原ID与记忆状态保留 |
| M9来源 笔记与资料 | knowledge_control等对应业务指纹一致，不能只看最终回复 |
| 私人原件 | 登记及三个原件文件逐一SHA256相同，隐藏/可见范围不被重写 |
| 新机身份 | 实例及本机令牌指纹与原机不同；旧API Key和旧授权未继承 |
| 未完成任务 | 排队/执行中暂停，原完成与未知回执保留，不自动重发 |

为了可比较，工具忽略允许清理的结构化凭据及发送方临时信息，且不把暂停后的任务状态与原机强求相同。比较须在桌面读取、授权或新消息改变运行元数据之前完成。若指纹不一致，先检查时点和差异，不能用“计数相同”掩盖原文或原件变化。

### B3 新机配置与重新授权

先关闭工作台。主模型和Jev分别输入自己的密钥，输入过程应隐藏，不把密钥写在命令参数里：

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $LiveRoot -Action ConfigureMain
.\scripts\Manage-Migration.ps1 -RuntimeHome $LiveRoot -Action ConfigureJev
```

两者接口地址和模型名可沿用迁移前设置；人工包没有原模型设置时，另用 `-BaseUrl` 和 `-Model` 填入自己的配置。当前Search/Fetch实现要求使用支持该工具的DeepSeek官方主模型接口；只替换为任意兼容地址，不能据此认定联网也受支持。Jev仍保留独立接口、模型名和密钥。

如果旧配置启用了候选选择模型，需要另行执行 `ConfigureSelector`；后台整理使用独立模型且准备恢复时执行 `ConfigureOrganization`。不要为绕过配置而借用另一个提供商的密钥。

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $LiveRoot -Action ActivatePreview
.\scripts\Manage-Migration.ps1 -RuntimeHome $LiveRoot -Action Activate -AuthorizeVisible
```

`ActivatePreview`只展示拟启用范围，不会实际连模型验证凭据；真正能否调用由C节验证。`-AuthorizeVisible`由用户执行，表示同意当前所选私人角色的必要可见资料使用新机模型，不等于批准其他角色或隐藏档案。其他私人角色分别选择和授权。

**通过标准：** 无密钥时不能靠旧授权启用；新机分别配置后状态为 `active`，当前角色获得绑定目标实例和目录的新授权。默认 `resume_background=false`，不会恢复主动提醒或主动外发。暂不添加 `-ResumeBackground`，先完成普通桌面对话验收。

### B4 正式入口与再次打开

```powershell
.\scripts\Start-Workbench.ps1 -RuntimeHome $LiveRoot
```

核对人物、会话和必要资料，再正常退出。之后直接双击原 `启动PCAR.bat`，不传参数。

**通过标准：** 回到同一个正式目标，角色、会话和历史连续；不回到人工演练目录，也不另建同名人物。历史ID保留、新机实例身份独立、密钥可解密并可供实际调用。原机保持原样，不发生两机自动同步。

## 六 实际对话与联网链路验收

先在人工迁移目标完成公开问题测试，再视已授权的可见范围在正式目标做少量连续对话。下文的 `$TargetRoot`、`$TargetPython` 换成正在测试的实例；不要混用人工实例和正式实例账目。

人工包没有原机模型配置。先将下面的占位内容替换为实际地址和模型名；密钥仍在随后的隐藏输入中填写：

```powershell
$TargetRoot = $MoveRoot
$TargetPython = $MovePython
.\scripts\Manage-Migration.ps1 -RuntimeHome $TargetRoot -Action ConfigureMain -BaseUrl 'https://api.deepseek.com/v1' -Model '填现有主模型名称'
.\scripts\Manage-Migration.ps1 -RuntimeHome $TargetRoot -Action ConfigureJev -BaseUrl '填现有Jev的HTTPS接口地址' -Model '填现有Jev模型名称'
.\scripts\Manage-Migration.ps1 -RuntimeHome $TargetRoot -Action Activate -AuthorizeVisible
.\scripts\Start-Workbench.ps1 -RuntimeHome $TargetRoot
```

正式目标已按B3完成配置时，不重复输入或重新授权；直接改用 `$TargetRoot=$LiveRoot`、`$TargetPython=$LivePython`。人工实例只证明调用链和新机配置，不能替代正式角色的v8表达及人物连续性评阅。

在工作台“联网与资料”页，勾选“当前角色按需联网检索”，点击“保存设置”，刷新确认通用检索已启用、Jev配置与授权已就绪。人工包默认未开启联网；只输入密钥不等于打开联网开关。

**本节会产生真实模型/工具费用。** 启动、诊断、导入和授权本身应为0次；发送实际问题后的费用须逐笔记录。只保存第一次实际返回；完成或结果未知的请求不重放，不为修饰质量反复重生成。

### C1 闲聊

输入：“今天有点累，先不查资料，陪我随便聊两句。”

**通过标准：** 有原生Jev判断回执；不需要联网时没有新增High检索计划、Search、Fetch或后台整理，人物能正常接话。不能以High代替Jev重新判断是否联网。

### C2 当前游戏信息与不同网站

输入：“帮我查一下三角洲行动当前版本，新手单排先注意什么？官方更新和玩家社区的经验都看看，先简短说重点。”

**通过标准：** Jev决定需要新资料后，High规划检索并取得实际搜索/读取回执，检索与整理为high；素材可来自官网、社区或普通网站，不要求预登记域名。回复先说眼前需要的重点，由人物自己形成判断；不因为某官网页面失败就整轮停摆。

按实际需求使用搜索摘要或继续读取网页，不强制每一问都做Fetch。本节跨话题测试须实际拿到可用公开材料；若所有联网问题都只有失败或空结果，只能记录“交流可继续，资料获取待完成”，不能认定检索能力已通过。

若本轮自然检索只使用一种网站，再追加一次“也看看玩家社区的实际经验”。这里验证通用获取能力，不要求每次都凑齐固定来源数量或证明官网绝对正确。记录实际拿到了什么，失败结果也保留。

### C3 根据已有资料追问

接着问：“刚才第二点具体怎么做？把步骤讲清楚，其他不用重复；先用你刚才查到的资料。”

**通过标准：** 已有材料足够时复用资料，能补充细节；通常应无新的Search/Fetch或整理，不能换执行编号就重发成功调用。确实需要新资料时如实记录理由。转译使用主模型原配置和v8，按语境选择摘要或展开，不把High思考档位强行复制给最终表达。

### C4 其他话题

从下面选两项，均使用公开且不含私人地址/账号的输入：

- 游戏社区：“查一下原神当前活动，米哈游官方和米游社玩家经验有什么值得参考的？先说我需要做什么。”
- 热点讨论：“网上看到一条‘某公司CEO已经辞职’的说法，我还不知道真假。你帮我检索实际消息，再说说你的判断。”将“某公司”替换为验收当天实际要查的话题，不把例句当真实新闻。
- 饮食：“查一下番茄炒蛋怎么做更下饭，普通菜谱和大家的做法都可以，给我一版省事的。”
- 出行：“帮我查一下杭州西湖周边的公共交通和步行安排，给第一次去的人一版简单建议。”地点可替换，不输入私人住址。

**通过标准：** 不依赖NASA或固定网站模板；能获取游戏之外的公开资料并给出实用回应。遇到争议允许有观点，遇到可核实的错误能在后续交流中修正；不引回旧的来源/数字/日期裁决链。

### C5 表达 用户评阅和真实回执

用户判断以下项目为“保留、需调整或未评阅”，不要求逐字匹配示范答案：

| 项目 | 通过标准 |
| --- | --- |
| 人物连续性 | 与原人物方向一致，有自己的思考，不突然变成审核助手 |
| 详略 | 简单询问先给够用的回答，追问再补细节，没有一律长篇转述 |
| 正文格式 | 普通对话没有意外出现标题、列表语法、强调符等MD排版残留；代码或原文确实需要符号时单独判断 |
| 观点与交流 | 不机械等待官方结论，不因不能绝对核实就拒绝正常判断，不强制各打五十大板 |
| 可信的执行记录 | 确有Jev、High和主回复各自回执；真实public外发为system_proxy，成功/未知调用未重放 |

Jev异常时允许沿用已批准策略，在已有权限内优先联网继续，并保留降级记录。但降级成功不能冒充“本次Jev已正常工作”；需取得正常Jev回执后才能完成Jev职责验收。High后台未完成时，人物可依据已取得素材继续交流；后台失败如实留账，不把它改写为成功。

“联网与资料”页可看累计调用与费用；随附核对工具可输出按阶段的账目行数、已知用量、决策和路线摘要。账目行数不等于物理调用次数，带重试的请求须查看原逐调用回执；未知用量和实际金额未知就保留未知，不能填0。

可在问答前和正常关闭问答窗口后各保存一次摘要，使用不同文件名比较新增阶段：

```powershell
& $TargetPython -B $Check --home $TargetRoot --out (Join-Path $Evidence 'C-问答前.json')
# 执行上述公开问题，记录实际回复；完成后正常关闭工作台。
& $TargetPython -B $Check --home $TargetRoot --out (Join-Path $Evidence 'C-问答后.json')
```

业务保留性专用的 `--compare` 不用于这两份问答记录，因为新消息本来就会改变业务。摘要文件采用新建方式保存，已有同名文件会拒绝覆盖；复验时换用含轮次的新文件名。

## 七 失败处理和暂停条件

| 现象 | 处理与判定 |
| --- | --- |
| doctor未就绪 | 按失败项修正Python、代理、模型或端口，再测受影响项；不改成通过 |
| 准备中断 | 查看准备日志，对未完成目录重试；不覆盖正式库 |
| 导入拒绝或失败 | 保留原包与记录；新目标未启用时使用RollbackPreview/Rollback恢复空目标 |
| 指纹不同 | 先核对比较时点、凭据清理及新增业务；正式保留性未确认前不发新消息 |
| 原件变化 密钥泄露 或其他实例被关闭 | 该项失败，暂停正式切换并保留原机，不能以聊天正常抵消 |
| 模型超时 网页失败 | 区分配置、代理、供应商和站点；保留原回执，在有限必要范围处理，不无界重复付费 |
| 返回结果未知 | 不重放原物理请求；检查原有回执或等待恢复，记录待完成 |
| 人物读感不接受 | 留原始回复与具体语境，用户填写需调整；Codex不代填通过 |

目标已经启用或产生新业务时，保留双方资料，另行做明确备份恢复；不能把正式库覆盖回旧快照来“凑验收”。本阶段不要求QQ、后台自启、自动跨设备同步或主动外发通过。

## 八 留证与最终签收

记录本次机器代号、系统/Python/uv版本、程序包SHA256、程序目录与数据目录、起止时间。每项写明“通过、失败、待完成、不适用”，并给出理由；本文件列出的必做项不能用“不适用”跳过。

保存doctor输出、只读指纹摘要、导入/回退返回、正常退出摘要、必要的无敏感界面截图、实际调用账目和用户评阅。不要保存整份带令牌配置到共享报告；正式聊天原文只在已授权的本地评阅位置保留。

填写随附《验收结果记录表》。最终结论须同时满足：A1—A9通过，B1—B4通过，C1—C5链路和用户评阅通过，已知影响业务保留、凭据保护、费用或退出的问题已处理。若仅做了人工演练，最终结论只能是“环境及人工迁移演练通过，正式业务迁移待完成”。

完成验收不自动产生提交推送、删除原机正式资料、恢复QQ或启用其他角色的授权。保留原机直到新机结果经用户确认，再安排后续交付。
