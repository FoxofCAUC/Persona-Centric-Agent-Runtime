# PCAR 环境解耦与跨机业务迁移

2026-11-14 开工。范围是 Windows 11 x64 的桌面部署、整库搬迁和原入口延续；另一台真实电脑的验收单列，不用本机换目录代替。

## 交付结构

| 层 | 内容 | 搬迁方式 |
| --- | --- | --- |
| 源码 | PCAR、锁定依赖、桌面入口、公开模型校验清单 | 源码包解压到任意独立目录 |
| 本机定位 | 源码根 `.pcar-local.json`，只记版本和数据目录 | 新机重新选择，不随包搬迁，不进 Git |
| 运行环境 | 所选数据目录的 `.venv`、缓存、公开本地模型 | 新机重建虚拟环境；模型可从公开模型包复用并校验 |
| 业务 | 整份 SQLite、私人角色原件与登记、M9 来源及笔记 | 单独的本地业务迁移包，保持原编号 |
| 机器凭据 | API Key、DPAPI 密文、实例令牌、云端授权 | 不搬迁；新机重新输入并授权 |

运行目录选择顺序是显式 `--home` / `-RuntimeHome`、当前进程的 `PERSONA_RUNTIME_HOME`、本机定位文件。都没有时，原桌面入口弹出目录选择；命令行给出说明，不猜用户名、C/D盘或其他项目路径。解释器使用所选目录的 `.venv\Scripts\python.exe`；显式 `PCAR_PYTHON` 可覆盖，找不到就报错，不借用其他实例。源码和业务目录不能互相包含。

普通退出和“强制停止全部组件”仅依据当前实例保存的进程身份及其子进程，不按 Python/QQ 名称或共用源码路径扫杀。独立的旧 QQ 专项入口不在本次改造与验收范围；QQ 开发、登录和平台测试继续暂停。

## 施工顺序和验收

1. 统一定位、解释器和子进程环境；保留原 `启动PCAR.bat`。Windows PowerShell 5.1 可运行，不修改系统环境变量。
2. 独立桌面准备：Python 3.12 x64、uv 锁文件、核心和本地语义依赖、BGE CPU 原模型。不安装 AstrBot/NapCat，不改变人物或模型表达逻辑。
3. 只读 `doctor`：检查版本、Tk、实例解释器、公开模型哈希、端口、系统代理。显式 `--network` 只测公开下载，不调用模型。
4. 整库导出/预览/导入：保持人物、聊天、记忆、来源及笔记的编号和既有记忆状态，不套用旧的单人物导入流程。
5. 新机启用：重新输入 API Key，当前角色可见材料重新确认。Jev独立配置保留，仍只判断是否联网；High只做联网后的规划与整理，v8转译沿用主模型原配置。
6. 本机全新环境、中文空格路径、跨盘源码/业务目录、导入失败回退、无收费启动退出验证；产出可复制的桌面包。
7. 第二台 Windows 11 x64 运行同一桌面包，完成下面的实机清单。此项通过前不称为跨机最终验收完成。

本轮不开发安装器/独立exe、Linux/macOS、后台自启、跨设备同步、QQ、G-AUTO或新人物机制。已批准的人物体验和通用检索原则保持，不恢复旧M9.B审核链。

## 新机准备

需要 Python 3.12 64位（包含Tkinter）和 uv。当前验证基线为 Python 3.12.10、uv 0.13.5。依赖以 `uv.lock` 为准；`--diagnostics` 另装锁定的 psutil 诊断依赖。模型及依赖下载都只走 Windows 当前用户开启的手动 HTTP/HTTPS 系统代理；没有代理就停止公共下载，不退回直连。PAC目前未实现。

解压桌面包，例如源码放 `C:\PCAR程序`，数据选 `C:\PCAR数据`，两者分别存放。首次双击 `启动PCAR.bat` 选择数据目录，准备成功后记住该目录。取消选择不会创建实例。也可先在 PowerShell 中执行：

```powershell
.\scripts\Prepare-Desktop.ps1 -RuntimeHome 'C:\PCAR数据'
```

带公开模型的桌面包可避免重复下载权重：

```powershell
.\scripts\Prepare-Desktop.ps1 -RuntimeHome 'C:\PCAR数据' -LocalModelSource '.\public-model'
.\scripts\Manage-Migration.ps1 -RuntimeHome 'C:\PCAR数据' -Action Doctor -Network
```

准备中断保留 `runtime\install-progress.json` 和日志，可对同一未完成安装重试。已存在实例不会被自动同步依赖覆盖；原机保留原目录与环境，只登记定位：

```powershell
.\scripts\Prepare-Desktop.ps1 -RuntimeHome '原实例绝对路径' -RegisterExisting
```

## 业务搬迁

原机先正常退出桌面。导出会核对所属运行进程已退出，使用SQLite备份副本，不修改原业务库。迁移包含私人角色及聊天，始终留在本地，不进Git或公开桌面包。不要拷贝整个旧数据目录或旧虚拟环境。

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome '原实例绝对路径' -Action Export
```

输出位于原实例 `exports\instance-migration-*.pcar-migration.zip`。将该文件私下带到新机，再对新准备、尚未创建人物的实例导入。目标若有任何业务数据会拒绝，不覆盖合并：

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome 'C:\PCAR数据' -Action Preview -Package '业务迁移包的绝对路径'
.\scripts\Manage-Migration.ps1 -RuntimeHome 'C:\PCAR数据' -Action Import -Package '业务迁移包的绝对路径'
```

预览检查包路径、内容清单和哈希，不创建目标目录。导入前校验结构、完整性、外键及磁盘空间；保留导入前空实例备份，记录安装阶段，失败恢复空实例。原机仍然可用。安装中的中断状态会阻止模型和旧任务运行，不能把半次导入当作成功。

迁移包排除密钥配置、结构化模型快照中的凭据、旧授权文件、运行租约、PID/锁、QQ登录资料、缓存、虚拟环境和开发评阅。数据库压实后再打包，避免被覆盖的密文留在空闲页中。仅清理结构化凭据字段，不全局替换聊天原文或人物原件。私人原件三个文件和登记文件保持字节；原件中的历史路径/历史批准字段不等于新机授权。

旧排队/执行中请求标为迁移暂停，输入缓冲取消，待投递片段暂停，发送中的结果保持不确定；已完成回执和原有未知结果保留。新机不自动重放它们。检查旧记录后，需继续的业务以新消息发起。整库保留原记忆状态，不把既有记忆全部改成待审。

## 新机重新配置与启用

导入后状态为 `needs_configuration`，可读历史，模型调用和自动任务暂停。先关闭工作台，在本机隐藏输入新密钥，切勿将密钥写进命令参数：

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome 'C:\PCAR数据' -Action ConfigureMain
.\scripts\Manage-Migration.ps1 -RuntimeHome 'C:\PCAR数据' -Action ConfigureJev
```

主模型和Jev各自保留原接口地址和模型名，也可分别用 `-BaseUrl` / `-Model` 修改。Jev必须输入自己的密钥，不借用主模型的。启用了候选选择模型或独立后台模型时，分别使用 `ConfigureSelector` / `ConfigureOrganization`。

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome 'C:\PCAR数据' -Action ActivatePreview
.\scripts\Manage-Migration.ps1 -RuntimeHome 'C:\PCAR数据' -Action Activate -AuthorizeVisible
```

`-AuthorizeVisible` 是当前所选角色必要可见材料的新机模型使用授权。新授权绑定这次迁移、目标实例身份和目标目录，复制授权文件或再换目录不能继承。其他私人角色需各自选择并授权。默认只恢复手动桌面对话；检查过后台配置后，才可额外添加 `-ResumeBackground` 恢复普通自动整理。旧主动外发规则保持关闭，不因恢复整理而启用。

此后从原 `启动PCAR.bat` 打开。启用前的回退只恢复导入前空实例，保留新建环境：

```powershell
.\scripts\Manage-Migration.ps1 -RuntimeHome 'C:\PCAR数据' -Action RollbackPreview
.\scripts\Manage-Migration.ps1 -RuntimeHome 'C:\PCAR数据' -Action Rollback
```

目标已启用可能有新业务，工具会拒绝这种空实例回退；此时保留两边业务，另做明确的备份恢复处理。不要靠覆盖旧库“回退”新对话。

## 第二台设备待完成的验收

使用与原机不同的用户名、源码目录和数据目录；不依赖D盘或原机任何项目。记录Python/uv/系统版本、`doctor`结果及桌面包SHA256。

- 双击原入口，首次选择/取消、再次启动记住目录；只有桌面组件运行。
- 先用人工迁移包演练：核对人物编号、原对话、记忆状态、M9笔记；预览不落盘、非空目标拒绝、回退后可再导入。
- 在另一个空实例进行正式搬迁；核对原件哈希和业务计数，不把正式内容放进验收报告。
- 主模型和Jev重新输入密钥并确认当前角色可见范围；关闭再开，不继承旧授权/进程身份。
- 人工问题验证闲聊、主动查询、资料追问三条：分别观察Jev判断、High检索、v8转译；实际收费另记，不重放成功或未知调用。人物读感仍由用户评判。
- 正常退出后只回收此实例组件，其他实例与其他项目继续运行。

本机完成情况与剩余实机项见 `STATUS.md` / `HANDOFF.md`。本轮没有得到第二台电脑执行结果前，不据此提交推送后续阶段或声称正式迁移已完成。
