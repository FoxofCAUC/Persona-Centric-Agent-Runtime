# Persona-Centric Agent Runtime

以人格为中心的私聊陪伴型智能体运行时。目标是与你长期相处的 AI 朋友，重点做好私聊体验、记忆与人格连续性；人格、经历与关系独立于具体模型和聊天渠道长期保存。

当前阶段：M1 持久化、M2 人格工作台与 M3 私聊稳定性已实现，准备 M4。支持兼容接口模型对话、窗口聊天、QQ 本人好友私聊、显式记忆召回与独立服务管理。自动记忆提取、群聊和人格演化尚未实现。

**M2、M3 真人验收已由用户确认通过，当前进入 M4 开发准备。** M3 提供逐条有序回复、请求阶段与耗时、未确认消息找回、独立对话刷新和可选排队取消/QQ 等待提示。见 [M3 收尾总结](docs/M3-收尾总结.md) 和 [M4 开发准备](docs/M4-开发准备.md)。M4 将解决长期记忆与长对话连续性，尚未实现或启用；群聊继续退出主线。

**双击 `启动人格工作台.bat` 打开桌面窗口。** 首次使用见 [M2 使用说明](docs/M2.md)。打开窗口不会自动启动组件、登录 QQ 或调用模型。

本轮直接按照 [M3 真人私聊验收手册](docs/acceptance/M3-真人私聊验收手册.md) 操作；历次手册收录在 [验收手册目录](docs/acceptance/README.md)。

## 目录与存储

- 当前 Git 仓库：源码、文档、配置模板。
- 默认运行数据根目录：`D:\Persona-Centric Agent Runtime`。
- 该 D 盘目录是运行数据目录，不是第二份源码仓库。
- 依赖环境位于 D 盘根目录下的 `.venv`；缓存、日志、临时文件、下载及模型文件也优先放在 D 盘。
- `PERSONA_RUNTIME_HOME` 可覆盖默认根目录；必须是绝对路径。存储盘不可用时直接报错，不自动写回 C 盘。

| D 盘子目录 | 用途 |
| --- | --- |
| `config` | 本地配置，未来模型凭据配置应留在此处 |
| `data/database` | 数据库 |
| `data/personas` / `data/events` / `data/memory` / `data/knowledge` | 为后续资源和索引预留；M1 的人格、事件、记忆统一存于 SQLite |
| `logs` / `backups` / `exports` | 日志、备份、导出 |
| `downloads` / `models` | 下载文件与模型权重 |
| `cache` / `tmp` / `.venv` | 工具缓存、临时文件、Python 环境 |

## 使用

在仓库目录中，通过 PowerShell 运行：

```powershell
.\scripts\dev.ps1 paths
.\scripts\dev.ps1 init-storage
.\scripts\dev.ps1 db init
.\scripts\dev.ps1 db status
```

`paths` 只输出路径；`init-storage` 创建缺失的存储目录；`db init` 幂等初始化数据库。所有命令均不会调用模型或发送消息。
开发脚本只临时配置子进程环境，退出后恢复原值，不修改系统级配置。
`.env.example` 是参考模板，当前程序不会自动加载 `.env`。

使用 Python 3.12 的 D 盘独立环境。人格服务使用锁定版本的 aiohttp；AstrBot 使用另一套 D 盘环境，依赖清单为 `requirements-astrbot.lock`。原 M1 的数据操作仍只使用标准库。
如需重建，先确认 D 盘可用，再执行：

```powershell
python -B -m persona_runtime init-storage
$env:UV_CACHE_DIR = 'D:\Persona-Centric Agent Runtime\cache\uv'
uv venv --offline --no-managed-python --python (Get-Command python.exe).Source 'D:\Persona-Centric Agent Runtime\.venv'
$env:UV_PROJECT_ENVIRONMENT = 'D:\Persona-Centric Agent Runtime\.venv'
uv sync --frozen
```

## 开发方向

使用步骤见 [M1 操作说明](docs/usage.md)。正式人格始终作为全新人格导入，不继承模板数据。

独立组件准备：`.\scripts\dev.ps1 prepare`；前台人格服务：`.\scripts\dev.ps1 serve`。组件准备可能下载依赖，但不登录 QQ、不调用模型。

运行验证：`.\scripts\test.ps1`。见 [架构与里程碑](docs/architecture.md) 和 [当前交接](docs/HANDOFF.md)。
