# PCAR v0.2 Windows 工作台部署手册

本手册适用于 **v0.2** 桌面包，程序版本 **0.2.0**，业务数据库结构 **33**。桌面包自带公开的本地角色检索模型。Agent 协助准备 Python、依赖和资料位置，用户配置自己的模型服务。[第一次使用](docs/user/getting-started.md) · [发布页](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/releases/tag/v0.2)

带「启动PCAR-新版.bat」的双人成卡工作台包可直接使用内置的发布版提示词。新实例从新版入口首次启动会准备 WebView2 界面依赖。旧工作台用户退出原窗口后选择原数据目录，新版入口会补齐缺少的界面依赖并沿用原设置。[导入提示词和聊天记录](docs/user/双人聊天生成角色卡.md)

## 部署前核对

- 使用 Windows 11 x64、Python 3.12 x64（含 Tkinter）和 uv。为程序、数据和工具选择可写的本机目录。程序与数据目录各自独立。
- 下载 `PCAR-v0.2-Windows-x64-desktop.zip` 和 `SHA256SUMS.txt`，核对 ZIP 的 SHA-256 后解压到新程序目录。已有实例保留自己的数据和虚拟环境。
- 使用当前 Windows 用户的手动 HTTP/HTTPS 系统代理。公共依赖下载和模型请求沿用系统代理。Agent 每次只设置部署子进程的代理环境，保留操作系统配置。
- 先确定是新建、原机升级还是迁移。切换已有实例前从工作台正常退出。备份和迁移包含个人资料，放在本机受保护目录或私下交付。

下文 PowerShell 示例中的路径按实际目录填写。涉及密钥时使用隐藏输入。文档、命令参数和日志只记录模型、地址与授权状态。

## 新建实例

```powershell
$Program = 'C:\PCAR程序'
$Data = 'C:\PCAR数据'
$BasePython = 'C:\Python312\python.exe'
Set-Location -LiteralPath $Program
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $Data -BasePython $BasePython -LocalModelSource '.\public-model'
$Python = Join-Path $Data '.venv\Scripts\python.exe'
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action Doctor -Network
```

准备会同步锁定依赖和公开模型。doctor 检查解释器、依赖、模型文件、代理及端口，这一步模型调用为零。日志在数据目录的 `logs/environment-prepare.log`。

准备完成后双击 `启动PCAR.bat`。工作台按实例设置启动人格服务。在“人格与记忆”创建或导入人物并切换角色，在“模型”配置基础聊天，再从“聊天”进入会话。

## 原机从 v0.1 升级

保留旧程序和升级前数据目录的完整备份，包括数据库、配置和私人材料。备份完成后，使用新程序登记原数据目录。已有 v0.1 本机数据可直接升级，由新版把结构 **27 升至 33**。`db init` 对已登记的业务库执行兼容升级，系统生成升级前 SQLite 快照。

```powershell
$Program = 'C:\PCAR-v0.2程序'
$Data = 'C:\PCAR原数据'
$Python = Join-Path $Data '.venv\Scripts\python.exe'
$Before = 'C:\PCAR验收\before-upgrade.json'
$After = 'C:\PCAR验收\after-upgrade.json'
Set-Location -LiteralPath $Program
& $Python -B .\scripts\pcar_upgrade_check.py snapshot --home $Data --out $Before
.\scripts\Prepare-Desktop.ps1 -RuntimeHome $Data -BasePython $Python -RegisterExisting
& $Python -B -m persona_runtime --home $Data db init
& $Python -B .\scripts\pcar_upgrade_check.py snapshot --home $Data --out $After
& $Python -B .\scripts\pcar_upgrade_check.py compare $Before $After --allow-schema-upgrade
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action Doctor -Network
```

比较通过后从**新程序目录**启动。升级账目核对旧会话、记忆、原回执和旧请求未追加派发。登记定位文件 `.pcar-local.json` 只记录本机选定的数据路径。

## 换机或导入迁移包

使用 v0.2 从已升级的原实例导出结构 33 的业务包，再导入空的目标实例。若手里只有 v0.1 导出的结构 27 包，先用 [v0.1 部署手册](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/blob/v0.1/AGENT_INSTALL.md) 在独立空目录恢复，再用上节的升级流程升至结构 33。

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $OldData -Action Export
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action Preview -Package $Package
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action Import -Package $Package
& $Python -B .\scripts\pcar_acceptance_check.py --home $Data --out $AfterImport
```

先与原实例的只读核对记录比较，再在目标机重新配置模型和用途授权。迁移暂挂旧未完成请求及新场景发布权限，保留已完成和结果未知的回执。导入状态为 `needs_configuration`，确认之后启用。已有实例直接升级无需执行迁移启用动作。

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action ActivatePreview
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action Activate -AuthorizeVisible
```

桌面包内的 `samples/artificial-business.pcar-migration.zip` 是由 v0.1 的公开演练样本升级而成的结构 33 样本。演练使用独立的空数据目录。

## 模型与角色使用范围

- **基础聊天**使用官方 DeepSeek。主模型建议 `deepseek-v4-pro`。按界面要求配置自己的接口与密钥。旧供应商配置随资料保留，云端生成须符合[供应商支持范围](docs/模型供应商支持总则.md)。
- **按需联网**在“联网与资料”页按角色启用。Jev 使用独立地址和凭据判断是否需要上网。人物先发言，发言可见后，DeepSeek Flash 才执行原生深度检索。使用 v8 Pro 发言的私有人物另需授权 Flash 处理公开查询。
- **自动记忆和召回**按角色启用。自动记忆需要整理模型与资料范围授权。召回按当前交流使用已获准资料。
- **场景发言**需要该角色的五份获审场景材料、Jev 1.14、原生 v8 Pro 和对应用途授权，新实例默认关闭。安装、校验和启用见[场景发言接入与回退](docs/expression/E.N6-场景发言接入与回退.md)。

通过命令设置凭据时，分别在本机隐藏输入主模型和 Jev 的密钥。生态支持与云端成本随所选模型及使用情况核对。

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action ConfigureMain -BaseUrl 'https://api.deepseek.com' -Model 'deepseek-v4-pro'
.\scripts\Manage-Migration.ps1 -RuntimeHome $Data -Action ConfigureJev -BaseUrl $JevBaseUrl -Model $JevModel
```

## 验收、关闭与回退

1. 先核对版本、环境和服务。检查原会话和回执仍可读。真实对话从用户已授权的材料与范围开始。
2. 按正常方式退出工作台，确认当前实例所属的服务退出。关闭窗口与最小化到托盘是不同的操作，已开启的后台功能随工作台运行。
3. 若要停用新场景发言，关闭工作台后按场景接入手册在结构 33 上执行 `disable`。后续使用 v8 路线，已确认的历史继续可读。
4. 要退回 v0.1 程序时，用其对应的升级前结构 27 备份。在独立目录恢复和核对，先妥善另存新版新增业务。原 v0.1 程序只能读取它对应的结构。保持副本分开，防止把已有新业务的结构 33 文件用旧库覆盖。

[产品路线](docs/product/roadmap.md) · [文档导航](docs/index.md) · [模型许可](docs/licenses/THIRD-PARTY-MODELS.md)
