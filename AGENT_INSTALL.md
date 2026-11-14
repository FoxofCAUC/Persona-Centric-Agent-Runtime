# PCAR v0.1 Agent部署与启用手册

PCAR v0.1是首个公开发行版，程序版本0.1.0，来自已验收的dev38功能基线。当前交付方式是将桌面包解压到独立文件夹，由DSH、Codex或其他具备Windows文件与命令执行能力的Agent接管该工作区，完成环境准备、实例选择和启用。它尚不是自带全部依赖的一键安装器。

作者已确认：第二台电脑更新包复验全部通过、实际对话审阅通过；相关包体整合后在第三台电脑一次部署验收通过，文本审阅及正式整库迁移均顺利完成。此手册使其他操作者能够复用同一路径，不包含作者的人物、聊天、密钥或正式业务。

## 让Agent开始

从GitHub Releases的v0.1下载PCAR-v0.1-Windows-x64-desktop.zip及SHA256SUMS.txt，核对哈希后解压。优先使用桌面包；GitHub自动生成的Source code压缩包不含公开模型权重和人工演练包，不能直接当成同一份桌面包。

将解压后的文件夹作为Agent工作区，发送：

> 请阅读AGENTS.md和AGENT_INSTALL.md，按v0.1已验证流程部署并启用PCAR。先区分新建、迁移还是登记已有实例；准备可检查的独立环境。缺少本机目录、模型服务、密钥或私人资料使用范围时向我集中询问，不复制其他项目的凭据。完成必要检查后从原入口启动，并报告实际结果。

Agent应先完成下述准备和零调用检查，再进行已获准的少量对话测试；不要直接运行整个历史测试仓库。

## 支持与包内容

- 已验证目标：Windows 11 x64、PowerShell5.1、Python3.12 64位且包含Tkinter。测试覆盖3.12.10与报告中的3.12.15；uv验证版本为0.13.5。
- 桌面包包含完整桌面运行源码、准备/迁移/核对脚本、uv.lock、公开BGE中文CPU模型及校验清单、人工迁移演练包、操作与验收文档。
- 不包含Python/uv安装器、虚拟环境、依赖轮子、API Key、用户人物与聊天、QQ客户端或独立服务。准备依赖仍需网络，模型权重可从包内public-model复用。
- 日常入口为启动PCAR.bat。基础聊天需配置主模型；当前Search/Fetch适配要求支持工具的DeepSeek官方接口，完整按需联网还需独立Jev的HTTPS地址、模型名和密钥。不能把任意OpenAI兼容地址等同于支持现有Search/Fetch或Jev契约。
- Jev只判断是否联网；High进行检索规划和资料整理；人物按原主模型配置和v8选择简答或展开。主模型与Jev使用各自凭据，不互相借用。
- 不包含QQ验收、Linux/macOS、无人值守服务、自启、自动更新或一键安装能力。

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

samples/artificial-business.pcar-migration.zip是公开人工包，5类业务各1，不含真实人物、聊天或密钥。用另一个空目标演练预览、导入、拒绝覆盖和回退；不要将它导入正式目标。它用来验证工具，不能代替用户正式资料的保留性比较或人物评阅。

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

完整联网在工作台“联网与资料”页勾选“当前角色按需联网检索”并保存，刷新确认Jev配置/授权就绪。导入密钥与启用联网不是同一件事。High计划/整理有单独输出预算，人物回复仍按自己的原配置和语境选择详略。

## 最小验收与交还

1. 环境：工作台关闭时doctor ready=true，实例解释器及公开模型校验正确。
2. 数据：新建角色/会话能保留；迁移时在首次打开前完成业务和原件比较。等待新机配置期间不能外发。
3. 零调用：准备、预览、导入、授权和doctor不收费。
4. 真实对话：已有明确范围和费用授权后，用人工公开问题验证闲聊、一次主动查询、已有材料追问。分别记录Jev、High、Search/Fetch及主回复；不为修饰质量重生成成功/未知结果，用户评阅由用户填写。
5. 退出：正常关闭后退出摘要processes_released=true、console_state=closed，其他程序/实例不受影响。

可以用pcar_acceptance_check.py保存只读摘要，检查任务状态、预算、回执与底稿数量；不打印完整配置、console或last-exit文件。Agent在交付时列出目录、版本、已完成步骤、仍缺配置、实际收费及必要失败；只报告真实结果，不将模拟或已有报告冒充本机测试。

首发包不是要求每位用户重复历史全仓验收。开发定向检查与完整跨机验收资料位于docs/acceptance，已有通过结果保持历史。首次完整部署以此手册为入口，旧dev37/dev38操作与包哈希属于当时基线。

## 版本与后续

GitHub tag为v0.1，程序与锁文件版本为0.1.0，数据库结构27。此前0.9.0.dev38是内部开发编号，不表示已经发布过0.9稳定版。现有实例通过结构和能力协议保持兼容，不用重新导入业务来匹配新版本号。

一键安装、自带解释器/依赖、自动升级和更多系统支持尚未交付，后续另行规划。QQ仍暂停；部署和对话验收不自动授权恢复QQ、主动外发、G-AUTO或公开用户资料。
