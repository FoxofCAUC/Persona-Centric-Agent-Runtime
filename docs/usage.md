# M1 操作说明

当前仅提供本地数据管理，不会自动对话或调用模型。以下命令在仓库目录中使用 PowerShell 运行。

## 初始化与状态

```powershell
.\scripts\dev.ps1 paths
.\scripts\dev.ps1 db init
.\scripts\dev.ps1 db status
```

`db init` 可重复运行，不清空已有数据。默认数据库为 `D:\Persona-Centric Agent Runtime\data\database\runtime.sqlite3`。
`init-storage` 仍只创建目录，不初始化数据库。数据库版本不兼容、损坏、不可写或锁等待超过 5 秒时，命令报错退出，不覆盖或重建原数据库。

所有查询均输出结构化 JSON；默认时间转换为本机时区。将 `--json` 放在命令组之前可获得适合脚本处理、时间保持 UTC 的 JSON：

```powershell
.\scripts\dev.ps1 --json db status
```

成功返回 0，业务失败返回 1，参数用法错误返回 2；错误输出到标准错误流。查询不会隐式初始化数据库。

## 创建人格与记录经历

```powershell
$persona = .\scripts\dev.ps1 --json persona create | ConvertFrom-Json
.\scripts\dev.ps1 persona use $persona.id
.\scripts\dev.ps1 persona list
.\scripts\dev.ps1 persona show
.\scripts\dev.ps1 relationship set --text '刚认识，从实际互动开始记录关系。'
.\scripts\dev.ps1 relationship show

$session = .\scripts\dev.ps1 --json session create | ConvertFrom-Json
$event = .\scripts\dev.ps1 --json event append --session $session.id --role user --text '我喜欢绿茶。' | ConvertFrom-Json
.\scripts\dev.ps1 event append --session $session.id --role persona --text '这是人工录入的示例发言，并非模型生成。'
.\scripts\dev.ps1 event list --session $session.id
.\scripts\dev.ps1 session show $session.id
.\scripts\dev.ps1 session list
```

创建人格不自动选中。默认没有选中人格，必须先 `persona use`。当前选中状态保存在数据库中，重启后仍然有效。

事件角色仅允许 `user`（用户）、`persona`（人格发言）、`note`（人工笔记）；事件仅追加，没有修改或删除接口。
`session create --scope private` 是唯一支持的会话范围，群聊等其他范围会被拒绝。
会话归属创建时的人格与本地用户，切换人格不会移动历史数据。

## 显式记忆与纠错

```powershell
$memory = .\scripts\dev.ps1 --json memory add --category preference --text '用户喜欢绿茶' --source $event.id | ConvertFrom-Json
.\scripts\dev.ps1 memory search '绿茶'
.\scripts\dev.ps1 memory search --limit 5
.\scripts\dev.ps1 memory show $memory.id

$correction = .\scripts\dev.ps1 --json event append --session $session.id --role user --text '现在更喜欢乌龙茶。' | ConvertFrom-Json
$revised = .\scripts\dev.ps1 --json memory revise $memory.id --text '用户现在更喜欢乌龙茶' --source $correction.id | ConvertFrom-Json
.\scripts\dev.ps1 memory show $revised.id
.\scripts\dev.ps1 memory retract $revised.id
```

| 类别参数 | 含义 |
| --- | --- |
| `user_fact` | 用户事实 |
| `preference` | 偏好 |
| `shared_experience` | 共同经历 |
| `relationship` | 关系说明 |
| `world_knowledge` | 世界知识 |

记忆必须有同一人格、同一本地用户的来源事件。记录外部知识时，先添加 `note` 事件，在正文注明出处和获知时间，再引用该事件。
`relationship set` 管理当前关系说明，不会自动创建记忆；需要追溯关系变化时，另外通过事件和记忆记录。

搜索是区分大小写的字面子串匹配，不是语义搜索；中文和表情可以直接检索，`%`、`_` 不作为通配符。
不填写搜索文本表示列出当前有效记忆，默认 20 条，`--limit` 范围 1–1000，按更新时间从新到旧排列。

修订返回新记忆 ID，旧版本标记为 `superseded`；只允许修订或撤回当前 `active` 版本。
修订时可指定新来源，省略 `--source` 则沿用旧来源。`show` 返回指定版本、完整修订链和每个版本的来源事件。
撤回仅停止默认召回，不物理删除数据；历史和导出中仍可查看，不应把它视为隐私数据擦除。

## 正式人格载入

先将仓库中的 `templates/persona.generic.json` 复制到 D 盘 `config` 目录，再编辑完整设定。
文件必须是 UTF-8 JSON（支持 BOM），包含以下全部字段，不接受额外字段：

```json
{
  "format_version": 1,
  "name": "正式人格名称",
  "introduction": "自我介绍",
  "core_traits": ["核心特征"],
  "expression_style": "表达习惯",
  "values_and_boundaries": ["价值与边界"],
  "initial_background": "初始背景，不虚构共同经历"
}
```

```powershell
$formal = .\scripts\dev.ps1 --json persona create --file 'D:\Persona-Centric Agent Runtime\config\persona.formal.json' | ConvertFrom-Json
.\scripts\dev.ps1 persona use $formal.id
.\scripts\dev.ps1 memory search
```

每次导入都创建全新人格，名称相同也不覆盖或继承；导入后需手动选中。旧人格保留，使用原 ID 切回即可访问原经历和关系。
M1 不提供人格原地修改、数据合并或历史迁移接口。导出快照与人格设定文件是不同格式，不能直接用 `persona create --file` 导入快照。

## 导出与备份

```powershell
.\scripts\dev.ps1 data export
.\scripts\dev.ps1 data backup
.\scripts\dev.ps1 data export --name 'my-persona.json'
.\scripts\dev.ps1 data backup --name 'runtime-backup.sqlite3'
```

默认使用唯一文件名。`--name` 仅接受文件名，不能指定目录；已有目标文件不会被覆盖。
导出位于数据根目录的 `exports`，包含当前人格、用户、关系、会话、事件及全部记忆版本。
备份位于 `backups`，包含整库所有人格和当前选中状态，使用 SQLite 备份接口并验证完整性。
JSON 导出和数据库备份均保持 UTC 时间，便于跨机器处理。

本阶段不提供自动恢复命令。需要人工恢复时，应先停止本项目所有进程，另行保留现有数据库，再将已验证的备份复制为 `data\database\runtime.sqlite3`，之后运行 `db status` 检查。
不要在程序运行时直接复制数据库作为备份，也不要编辑数据库结构或版本号。

## 日志、存储与验证

CLI 操作日志为 `logs\operations.jsonl`，只记录时间、操作类型、记录 ID 与错误类别，不记录发言、记忆、设定或完整命令参数。
如果业务操作成功而日志不可写，会输出警告，业务结果仍然有效。调用 Python 业务服务的其他渠道需自行接入相同的元数据日志策略。

`PERSONA_RUNTIME_HOME` 可显式覆盖数据根目录，必须是绝对路径。开发脚本的解释器仍取自默认 D 盘 `.venv`，不要求每个测试数据目录再创建虚拟环境。
直接使用 Python 时，请通过 `-B` 禁止生成源码目录字节码缓存，或将 `PYTHONPYCACHEPREFIX` 指向 D 盘。

```powershell
.\scripts\test.ps1
```

测试使用数据根目录下 `tmp\m1-test-*` 的独立目录，结束后清理；不会写入正式数据库。
测试包含每一步均启动新进程的完整验收流程，以及锁定、回滚、只读数据库、未知版本、跨人格访问与导出备份检查。
