# CR2.1 共享情境真实诊断验收手册

候选0.7.6.dev18，施工基线bb68120上的未提交CR2工作区。真实采集、模拟、工程验证及12组用户偏好复核已完成；最终封存以closure和只读核查为准。不新增工作台入口或启用按钮，原生界面、QQ与实际私人角色副本未执行。

## 前置与边界

真实批次real-v1已completed（128主回复、232物理调用）。勿再次execute、删除run.started、恢复生成、复制模拟结果当真实或追加兼容探针。重试均已由原生运行时完成，成功位置不能重放。临时鉴权sidecar已清理，正式配置及私人原件未动。

## 只读核查

查看实时终态（不产生调用）：

```powershell
Get-Content -LiteralPath 'D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR2.1-real-diagnosis/real-v1/state.json'
```

预期status=completed、real=true、main_completed=128、physical_calls=232。analysis/terminal-engineering-audit.json应passed=true、errors=[]、无无归属回执；160库无active/running。所有232次均有reserve/transport/terminal，224成功、8失败均已恢复。失败次数不能填0。

最终使用专用只读检查器（默认不与后续工作区强制等同）：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
& 'D:/Persona-Centric Agent Runtime/.venv/Scripts/python.exe' -B 'C:/Users/ZhuanZ（无密码）/Documents/ChatGPT/Persona-Centric Agent Runtime/scripts/dialogue_context_real_audit.py'
```

此命令要求最终closure及12组复核完整。封存后勿运行会回写的report或旧实测入口。--worktree仅在确需核对当前源码仍等于归档时使用，后续源码变更不改旧证据。

## 已执行自动检查

| 项目 | 结果及证据 |
|---|---|
| 开工 | CR2的656源码/718证据一致，opening保留源码差异和原工作区 |
| 模拟 | 最终simulation-v3：128主/224模拟；非execute动作阻断网络 |
| 局部检查 | 49项去重检查通过，含6项只读封存校验的改动/新增文件/缺复核/路径越界负例 |
| R0 | 34冻结场景零差异，基准未改、结果写本阶段 |
| 真实冒烟 | S01/S11同批16主/28调用，smoke.json通过，无重放 |
| 真实完整 | 128主/232物理，全部终态，6联合辅助位置重试恢复 |
| 请求与旧路径 | 实收消息=Manifest=wire指纹；64旧臂与封存renderer字节等价 |
| 内容阅读 | 两臂各64任务/冻结边界通过，32Frame的92关键项正确；附加问题独立保存 |
| 分流挑战 | 新16条本地挑战有4漏/4过度分流，已执行未改善，不据原场景全过宣称泛化 |
| 来源及鉴权 | 冻结、权限、实际入模、隐藏材料检查通过；公开快照无非空鉴权，sidecar已移除 |
| 原生收尾 | 160库active/running=0，无在途/无无归属回执 |
| 保护 | 正式/独立评测7538文件及CR0/CR1/CR2旧封存只读通过 |
| 匿名复核 | 固定12组已完成；新路径3胜1平8负，原答/映射及机器评分保持 |

工程通过不能替代自然度效果；自然度1–5三维独立，活泼优先、条件猜测及礼貌拟稿回请允许。长度、问号、礼貌或文字重合均不能代替判断。机器结论不是独立人工盲审。

## 用户复核已完成

1. 打开D盘real-v1/analysis/CR2.1-12组匿名审核.md。
2. 先读人物关注方式、前文和完整当前输入，再读A/B所有原气泡。两角色均为普通朋友、无共同事件，身份知识相同，仅表达关注与取舍不同。
3. 每组回复A、B、平或无法判断，可附原因。格式：`01 A；02 平；……；12 B`。
4. 收到后保留原始选择，以冻结anonymous-mapping.json离线揭示条件，不更换配对或补造理由/数值分数。
5. 用户12组选择已完成，冻结映射解码新路径3胜1平8负；未给原因与数值分数，不推测。真实采集和机器阅读完整，离线结算新增调用0。原始结果不重做；总体结论为工程验收完成、未见稳定自然度改善。

| 人工项目 | 当前状态 | 证据 |
|---|---|---|
| 固定12组用户偏好 | 2026-11-03已完成 | user-review-original.json、user-review-resolved.json及映射结果.md |
| 原生工作台视觉 | 未执行，不计通过 | 本阶段无界面改动 |
| 实际QQ收发 | 未执行 | 本阶段禁止 |
| 实际私人副本 | 未执行 | 留CR4，未来验收结束后清理、不提交 |

## 收尾及恢复

真实采集、用户复核、离线结算、指导更新和保护核查均完成。最终读取closure.json和handoff.json，使用专用只读检查器核对归档；原checkpoint.json为复核前历史快照，不覆盖最终状态。若下次继续，只制定下一阶段计划，禁止恢复生成或回写本包。

R4-B待后续集成与CR4复验；R4-A/C留CR3。来源资格校验缺口、条件收紧/历史误读、策略报幕与路由泛化风险继续登记，不在本轮修提示追分。无提交、推送、部署或Live/M8。

[阶段结果](../CR2.1-共享情境真实诊断.md)
