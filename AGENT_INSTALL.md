# PCAR 0.2.0rc3 召回开发候选与接入准备

当前为召回配套.0—.2开发候选，数据库结构28。151项不同关联检查及实际27→28人工业务升级、备份恢复、整库人工迁移已完成，见docs/acceptance/召回配套.0-2-开发与验证.md。连续体验与正式接入属于.3，本候选先用于独立人工实例；正式人物和配置尚未切换。原M9R、rc1／rc2证据及v0.1发行保持。联网仍为Jev决定、原生深度搜索和人物回应。

## 让Agent开始

使用本次交付的PCAR-0.2.0rc3-recall-Windows-x64-desktop.zip与SHA256SUMS.txt，先校验，再解压到新的独立程序目录。优先按docs/召回配套.3-连续体验与接入准备.md准备独立公开材料，不用正式业务库试迁移。旧程序和数据保留，当前目录已经是候选包时，不另行下载v0.1替换它。

给接管该目录的Agent说明：请阅读AGENTS.md、本手册和docs/M9R.5-本机验收与候选交付.md，先区分已有实例升级、新建或整库迁移。沿用本次操作者已指定的目录与授权，完成零调用检查，再进行已授权的少量人工公开对话；成功及未知调用不重放。最后通过原启动PCAR.bat进入。

## 支持与包内容

- Windows 11 x64、PowerShell5.1、Python3.12 64位及Tkinter；当前候选保留原依赖锁与公开BGE中文CPU模型。
- 包含桌面源码、准备／迁移／只读升级核对脚本、uv.lock、公开模型及许可、固定人工迁移演练包和操作文档。不含API Key、私人角色、聊天、虚拟环境、QQ客户端或独立服务。
- 当前生成连接支持DeepSeek官方HTTPS地址及当前型号；完整联网请选择deepseek-flash。旧网关配置保留读取与导出，实际外发前拒绝。Jev使用独立HTTPS连接与自己的密钥，不经过主模型的提供商过滤。
- 当前角色的联网开关仍决定是否联网，不因默认实现更换而开启。开启后使用原生两段式，无旧High规划／同步整理或单段式选择。原生任务使用深度思考搜索，查找前由人物按当前交流发言，之后接着回答。
- 沿用已验证的Agent辅助准备方式，不宣称一键安装、自动更新、无人值守、G-AUTO或更多系统支持。QQ保持暂停。

## 已有实例升级与回退

升级使用新的程序目录和原数据目录。先正常退出原工作台，在原机保存受保护业务备份及只读核对记录；不把业务包放到公开交付目录。候选包内的pcar_upgrade_check.py只输出行摘要哈希，不含原文或密钥，snapshot和compare均不调用模型。

在候选程序目录中，按实际路径运行：

```powershell
$Program = '候选程序的绝对路径'
$Data = '原数据目录的绝对路径'
$Python = Join-Path $Data '.venv\Scripts\python.exe'
$Before = '本地新建的升级前记录.json'
$After = '本地新建的升级后记录.json'
Set-Location -LiteralPath $Program
& $Python -B .\scripts\pcar_upgrade_check.py snapshot --home $Data --out $Before
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $Data -BasePython $Python -RegisterExisting
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action Doctor -Network
& $Python -B .\scripts\pcar_upgrade_check.py snapshot --home $Data --out $After
& $Python -B .\scripts\pcar_upgrade_check.py compare $Before $After --allow-schema-upgrade
```

本候选增加结构28的召回派生表，升级前保存结构27备份。核对工具默认要求结构相同；显式--allow-schema-upgrade仅允许声明的27→28且新表齐备，仍检查所有原业务行、可读资料和旧调用身份。旧检索未决结果不换实现重发，doctor不能代替人物体验。上面的正式接入步骤留待.3对应范围完成后执行，本轮只在人工实例验证。

此前rc1／rc2到v0.1的回退只针对结构27成立。结构28不能直接交给v0.1／rc2读取；.3正式接入前须完成保留新增业务的回退验证。当前隔离人工环境可从留存旧人工库重新演练，不用升级前备份覆盖正式新增业务。不要把原结构27回退成绩当作本候选已经验收的回退能力。

## 先确定三个目录和数据方式

选择互不包含的程序、数据和工具目录。例如程序C:\PCAR程序、数据C:\PCAR数据、工具C:\PCAR工具；没有权限时选择当前用户可写的普通本地目录。示例路径可替换，不要求D盘。不要使用盘符根目录、网络共享或目录链接；也不要把数据放在程序子目录。

Agent先询问或沿用本次已给出的方式：

1. 新建：空数据目录，稍后创建通用模板或导入用户自己的角色。
2. 整库迁移：用户自己的迁移包导入到空目标，先不创建人物、不打开目标工作台。
3. 已有实例：保留原数据和原.venv，只登记，不再次导入或覆盖。

已有工作台必须先正常退出。不要从其他工作区找一个密钥或人物来填补缺失信息，也不要为部署删除原数据。

## 准备系统代理和基础工具

确认Windows当前用户已开启手动HTTP/HTTPS系统代理，代理服务可用。PCAR不修改系统代理，不支持仅PAC的配置；缺失有效代理时先请操作者处理，不能回退直连或关闭证书检查。

Agent读取HKCU的Internet Settings后，将有效HTTP和HTTPS代理分别用于本次子进程的HTTP_PROXY/HTTPS_PROXY及小写同名变量；分协议的ProxyServer需要拆分映射，不能将整段分号字符串当URL。NO_PROXY仅保留localhost、127.0.0.1、::1。公开工具和依赖下载也遵循该路线，不改系统环境变量。

Python3.12与uv已存在时直接复用。否则由Agent从[Python官方](https://www.python.org/downloads/windows/)或[uv官方](https://docs.astral.sh/uv/getting-started/installation/)准备本机工具，记录实际来源与版本。不要从旧电脑复制.venv。

如果python.org安装器被本机策略拒绝，而uv可用，可在已设置本次系统代理的进程内使用uv管理的Python。下面的操作不注册全局Python、不安装全局快捷入口：

```powershell
$Tools = 'C:\PCAR工具'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools 'python'
$env:UV_CACHE_DIR = Join-Path $Tools 'cache\uv'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
uv python install 3.12 --no-registry --no-bin
$BasePython = (uv python find 3.12 --managed-python --no-project).Trim()
& $BasePython -I -B -c 'import sys,struct,tkinter,sqlite3,ssl; print(sys.version); print(struct.calcsize("P")*8); print(tkinter.Tcl().eval("info patchlevel"))'
```

通过标准是3.12、64位、Tk可用。若用已安装解释器，将BasePython设为其实际绝对路径。py启动器与PATH发现不是必需，后续显式传BasePython即可。遇到权限或低完整性工作区问题，使用Agent与操作者都有正常访问权的本地目录；不要批量解除ACL或改变系统安全策略来通过检查。

## 准备桌面实例

以下命令在解压后的程序根运行；BasePython须先填好：

```powershell
$Program = 'C:\PCAR程序'
$Data = 'C:\PCAR数据'
Set-Location -LiteralPath $Program
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $Data -BasePython $BasePython -LocalModelSource '.\public-model' -NoRegister
$Python = Join-Path $Data '.venv\Scripts\python.exe'
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action Doctor -Network
```

准备与doctor均不调用付费模型。准备退出码0、doctor ready=true之后再继续；日志位于数据目录logs\environment-prepare.log。未完成安装可以在修正原因后重试，已识别的完整实例不会被默认同步覆盖。

doctor请在工作台关闭时执行。它检查环境、包版本、模型哈希、系统代理与端口，不证明模型密钥可用或迁移已经完成。公开模型许可与来源见docs/licenses/THIRD-PARTY-MODELS.md。

如果是登记已有实例，跳过上面的准备命令：

```powershell
$Python = Join-Path $Data '.venv\Scripts\python.exe'
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $Data -BasePython $Python -RegisterExisting
```

登记只在程序根写本机定位文件，不搬动业务，不重建虚拟环境。继续使用原目录与实例身份；程序版本变化不要求重置密钥或迁移授权。

## 新建或迁移 选择一个分支

### 新建实例

只有确认不导入整库时才初始化数据库和创建模板：

```powershell
& $Python -B -m persona_runtime --home $Data db init
$Persona = & $Python -B -m persona_runtime --json --home $Data persona create | ConvertFrom-Json
& $Python -B -m persona_runtime --home $Data persona use $Persona.id
```

也可在工作台“人格与记忆”页创建通用模板或导入用户自己的角色。不同JSON格式使用对应导入入口，Agent应先核对格式，不把所有文件硬套进同一导入命令。模板不是作者正式人物，也不带作者记忆。

### 整库迁移

用户在原机正常退出后，用同一套工具的Export导出自己的业务包，连同原机只读基线记录私下传到新机。迁移包含私人业务，不能放入公开Release。

```powershell
$Package = '用户业务迁移包的绝对路径'
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action Preview -Package $Package
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action Import -Package $Package
& $Python -B .\scripts\pcar_acceptance_check.py --home $Data --out '本地新建核对记录的绝对路径.json'
```

预览applied=false，实际导入applied=true、state=needs_configuration。目标非空必须拒绝，不合并覆盖。首次打开目标工作台、重新输入密钥或产生新消息之前，与原机基线进行只读比较：

```powershell
& $Python -B .\scripts\pcar_acceptance_check.py --compare '原机基线记录.json' '新机导入后记录.json'
```

人物、会话、原文、记忆状态、原件和M9资料应保留，实例令牌和密钥不继承。旧未完成任务保持暂停，已完成/未知调用不重放。未启用目标可RollbackPreview后Rollback恢复导入前空状态；已启用且可能有新业务时不使用空实例回退。详见docs/PCAR-环境解耦与跨机迁移.md。

### 人工演练

旧包中的samples/artificial-business.pcar-migration.zip为结构27人工样例，不作为本结构28包的迁移夹具。本次候选不携该旧样例；先用experiments.recall_preparation生成独立结构28公开材料，需整库演练时从相同结构的人工实例导出。人工包只验证工具，不能代替正式资料保留性比较或人物评阅。

## 配置主模型 Jev和启用

Agent先收集本次操作者的接口地址、模型名及必要资料使用范围。密钥在本机隐藏输入，或经操作者已授权的安全凭据入口交给标准输入；不粘贴到命令参数、公开聊天、文档或日志。占位内容必须替换，Jev必须是支持项目既有/systemone契约的服务。

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action ConfigureMain -BaseUrl 'https://api.deepseek.com/v1' -Model '填主模型名'
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action ConfigureJev -BaseUrl '填Jev的HTTPS地址' -Model '填Jev模型名'
```

主模型和Jev分别使用自己的Key。如果暂时只有主模型，可以先保持联网关闭验证基础聊天；这不等于完整联网已就绪。迁移前启用了独立候选选择模型时另用ConfigureSelector，准备恢复独立后台整理时用ConfigureOrganization。保留默认额度，不为部署设为不限次数。

迁移目标还需要在确认当前角色及其必要可见材料的授权后执行：

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action ActivatePreview
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action Activate -AuthorizeVisible
```

授权仅适用于当前私人角色与选定提供商，不代表隐藏档案或其他人物。默认不加ResumeBackground，主动外发规则不恢复。新建/登记实例没有迁移标记时不执行Migration Activate；其正常人物授权使用对应工作台入口和当前操作者的明确范围。

## 打开与日常使用

```powershell
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $Data -BasePython $Python -RegisterExisting
.\scripts\Start-Workbench.ps1 -RuntimeHome $Data
```

之后双击启动PCAR.bat回到同一实例。程序根.pcar-local.json只记录数据位置，不含密钥，不随迁移包复制。配置参数或路径明确时Agent可全程用命令准备，最后交还原工作台。

完整联网在工作台“联网与资料”页勾选“当前角色按需联网检索”并保存，刷新确认主模型官方Flash、Jev及当前角色授权就绪。导入密钥与启用联网不是同一件事。资料任务固定为原生深度搜索，人物仍使用原有上下文。新机导入后须重新开启当前角色联网，不继承旧执行权限。

## 最小验收与交还

1. 环境：工作台关闭时doctor ready=true，实例解释器及公开模型校验正确。
2. 数据：新建角色/会话能保留；迁移时在首次打开前完成业务和原件比较。等待新机配置期间不能外发。
3. 零调用：准备、预览、导入、授权和doctor不收费。
4. 真实对话：已有明确范围和费用授权后，用人工公开问题验证闲聊、一次主动查询、已有材料追问。分别记录Jev、查找前发言、原生任务及主回复，独立Search/Fetch仅登记受控补缺；不为修饰质量重生成成功/未知结果，用户评阅由用户填写。
5. 退出：正常关闭后退出摘要processes_released=true、console_state=closed，其他程序/实例不受影响。

可以用pcar_acceptance_check.py保存只读摘要，检查任务状态、预算、回执与底稿数量；不打印完整配置、console或last-exit文件。Agent在交付时列出目录、版本、已完成步骤、仍缺配置、实际收费及必要失败；只报告真实结果，不将模拟或已有报告冒充本机测试。

首发包不是要求每位用户重复历史全仓验收。开发定向检查与完整跨机验收资料位于docs/acceptance，已有通过结果保持历史。首次完整部署以此手册为入口，旧dev37/dev38操作与包哈希属于当时基线。

## 版本与后续

已有GitHub tag为v0.1；本候选程序与锁文件版本0.2.0rc3、数据库结构28，不创建新tag或Release。此前0.9.0.dev38是内部开发编号，不表示已发布过0.9稳定版。版本、结构、正式启用及用户体验分别记录，不用重新导入业务来匹配版本号。

一键安装、自带解释器/依赖、自动升级和更多系统支持尚未交付，后续另行规划。QQ仍暂停；部署和对话验收不自动授权恢复QQ、主动外发、G-AUTO或公开用户资料。
