# Persona-Centric Agent Runtime

以人格为中心的陪伴型智能体运行时。人格、经历与关系独立于具体模型和聊天渠道长期保存。

当前阶段：M1 已实现人格、关系、会话、事件和显式记忆持久化，支持重启恢复、修订与撤回、导出和备份。尚未实现模型聊天、自动记忆提取或 QQ 接入。

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

使用已安装的 Python 3.12 在 D 盘创建独立虚拟环境；运行和测试均只需标准库，无第三方依赖。
如需重建，先确认 D 盘可用，再执行：

```powershell
python -B -m persona_runtime init-storage
$env:UV_CACHE_DIR = 'D:\Persona-Centric Agent Runtime\cache\uv'
uv venv --offline --no-managed-python --python (Get-Command python.exe).Source 'D:\Persona-Centric Agent Runtime\.venv'
```

## 开发方向

使用步骤见 [M1 操作说明](docs/usage.md)。正式人格始终作为全新人格导入，不继承模板数据。

运行验证：`.\scripts\test.ps1`。见 [架构与里程碑](docs/architecture.md) 和 [当前交接](docs/HANDOFF.md)。
