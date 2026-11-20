# PCAR v0.3 Windows 工作台部署手册

适用于 Windows 11 x64，程序版本 **0.3.0**，业务数据库结构 **36**。使用 Python 3.12 x64、uv 和 Edge WebView2 Runtime。此手册与桌面 ZIP 中的 `AGENT_INSTALL.md` 相同。命令在解压后的程序目录执行。[用户入门](docs/user/getting-started.md) · [本版更新](CHANGELOG.md)

## 准备文件和目录

1. 取得 `PCAR-v0.3-Windows-x64-desktop.zip`、`PCAR-v0.3-Agent-Setup.md` 和 `SHA256SUMS.txt`。用 `Get-FileHash -Algorithm SHA256` 核对 ZIP 和手册，再解压到新的程序目录。
2. 为程序和数据分别选择可写的本机目录。实例的 Python 环境、模型、日志和私人资料保存在所选数据目录。公共依赖下载使用已开启的 Windows 手动 HTTP/HTTPS 系统代理。
3. 新建实例使用空数据目录。原机升级沿用原数据目录。迁移包导入独立空实例。已有实例在升级或导出前从工作台正常退出并保存备份。

## 新建实例

以下示例按本机实际路径填写。

```powershell
$PcarProgram = 'C:\PCAR-v0.3'
$PcarData = 'C:\PCAR数据'
$PcarBasePython = 'C:\Python312\python.exe'
Set-Location -LiteralPath $PcarProgram
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $PcarData -BasePython $PcarBasePython -Frontend webview -LocalModelSource '.\public-model'
$PcarPython = Join-Path $PcarData '.venv\Scripts\python.exe'
& $PcarPython -B -m persona_runtime --home $PcarData doctor
```

准备程序安装锁定依赖和包内的公开 BGE 检索模型。双击 **启动PCAR-新版.bat** 进入新版工作台，按实际使用配置模型及接收方，创建或导入角色。旧 Tk 入口为 `启动PCAR.bat`。原实例仍在运行时，启动器会尝试唤回对应窗口。

## 原机从 v0.2 升级

关闭原工作台，保留 v0.2 程序以及数据、配置和私人材料的完整备份。将 v0.3 解压到新的程序目录，使用原实例的解释器登记数据位置，结构 33 升到 36。已有结构 36 的本机实例可登记原目录继续使用。结构 27 的 v0.1 原机实例也沿兼容迁移路径升级。

```powershell
$PcarProgram = 'C:\PCAR-v0.3'
$PcarData = 'C:\PCAR原数据'
$PcarPython = Join-Path $PcarData '.venv\Scripts\python.exe'
$PcarBefore = 'C:\PCAR验收\before-upgrade.json'
$PcarAfter = 'C:\PCAR验收\after-upgrade.json'
Set-Location -LiteralPath $PcarProgram
& $PcarPython -B .\scripts\pcar_upgrade_check.py snapshot --home $PcarData --out $PcarBefore
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $PcarData -BasePython $PcarPython -RegisterExisting -Frontend webview
& $PcarPython -B -m persona_runtime --home $PcarData db init
& $PcarPython -B .\scripts\pcar_upgrade_check.py snapshot --home $PcarData --out $PcarAfter
& $PcarPython -B .\scripts\pcar_upgrade_check.py compare $PcarBefore $PcarAfter --allow-schema-upgrade
& $PcarPython -B -m persona_runtime --home $PcarData doctor
```

核对原会话、有效记忆、回复和调用回执后，从新程序目录打开工作台。业务库升级按阶段建立 SQLite 快照。回退 v0.2 时，将升级前结构 33 的完整备份恢复到独立数据目录，再用原程序登记该目录。升级后新增的业务另存并核对。

## 换机和人工演练

v0.3 可导出和导入结构 36 的完整业务迁移包。仅持有结构 33 或 27 的旧迁移包时，先用相应旧版恢复到空目录，再原机升级并用 v0.3 导出。个人迁移包含业务资料，应私下保管。

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $PcarOldData -Action Export
.\scripts\Manage-Migration.ps1 -RuntimeHome $PcarNewData -Action Preview -Package $PcarMigration
.\scripts\Manage-Migration.ps1 -RuntimeHome $PcarNewData -Action Import -Package $PcarMigration
.\scripts\Manage-Migration.ps1 -RuntimeHome $PcarNewData -Action ActivatePreview
```

导入后状态为 `needs_configuration`。核对旧业务，重新配置本机凭据、接收方和可见资料权限后再启用。启用示例为 `.\scripts\Manage-Migration.ps1 -RuntimeHome $PcarNewData -Action Activate -AuthorizeVisible`。结构 36 的公开演练包在 `samples/artificial-business.pcar-migration.zip`，源自 v0.2 已发布的人工样本。演练使用空实例。

## 功能和启用条件

- **建卡与历史接续。** 新版工作台内置已审定的三段双人建卡模板。用户选择自己的私聊 Markdown，确认身份、脱敏材料和用途后建卡或接续历史。[角色卡](docs/user/双人聊天生成角色卡.md) · [历史接续](docs/user/历史聊天接续.md)
- **会话与恢复。** 逐条、定时和手动整轮发送共用持久受理记录。重新打开可读回已确认气泡。主题、授权、费用限额、维修及备份从新版工作台操作。[用户入门](docs/user/getting-started.md)
- **聊天、场景和记忆。** 基础聊天支持官方 DeepSeek。后台自动记忆按角色启用。场景发言需要该角色获审的场景材料、Jev 配置、正文模型和相应用途授权。[场景发言](docs/expression/E.N6-场景发言接入与回退.md)
- **新版私聊和本地知识。** 包含 J1 至 J6 的私聊决策、会话授权、本地语义取材、资料入库与更正、查前短预告、查后短答和独立长文。启用该路线需要与本实例、人物和接收方匹配的已审装配。用本机文件的已确认 SHA-256 登记，在目标会话的「会话授权」确认接收方。个人角色装配随其原授权私下保存。管理命令和分工见下节。
- **接收方分流。** 已配置且获准的文本中转承接 Pro 正文和文本 Flash，官方 DeepSeek 执行原生联网和网页取材，Jev 负责独立决策。新用户配置自己的服务和凭据。[供应商范围](docs/模型供应商支持总则.md)

## 登记已审私聊装配

在解压目录使用所选实例的解释器。先核对装配的实例、人物、模型接收方、必要的本地检索模型和用途，批准对应文件的准确版本。下列变量由操作者指定。

```powershell
& $PcarPython -B -m persona_runtime.ordinary_providers --home $PcarData --bundle $PcarReviewedBundle --reviewed-sha256 $PcarReviewedHash
& $PcarPython -B -m scripts.pcar_local_knowledge --help
```

重开该实例后选择目标人物和会话。完全退出工作台后，可用 `python -m persona_runtime.ordinary_providers --home <实例目录> --retire` 停用这份登记。新机按新实例的审阅和授权重新登记。将真实模型或联网试用安排在用户确认相应资料范围之后。工作台关闭时所属服务随之退出。

[产品路线](docs/product/roadmap.md) · [第三方模型许可](docs/licenses/THIRD-PARTY-MODELS.md)
