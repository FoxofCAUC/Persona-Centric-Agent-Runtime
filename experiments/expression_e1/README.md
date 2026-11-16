# E1隔离复用与选型

作者已审阅修复后8组并确认通过：[E1结项](../../docs/expression/E1-作者审阅与结项.md)。默认入口`current.py`保持JSON职责分离协议；纯正文代码对照偏好保留，E2尚未开始。以下运行和审阅说明保留各批的执行方法，当前作者结论以`user-review.json`及完成记录为准。

2026-11-16修复入口：新实验代码从`current.py`使用JSON职责分离协议，另保留纯正文＋本地气泡候选。16次实际模型补验全部可用，原首批32个结果和6份空白保持。完整记录见[协议与回执修复](../../docs/expression/E1-协议与回执问题修复.md)。下方`runner`命令为原首批工具，不作为新实验的默认生成入口。

本包从MaiBot固定提交`478f671`抽取原文映射、分隔符、分段数据、候选合法ID、表达习惯装配和打字估时纯组件，适配上下文和独立表达请求。来源与本地差异见源码头及`docs/expression/E1-上游抽取与差异.json`，许可全文为`docs/licenses/MaiBot-GPL-3.0.txt`。上游服务、Planner、群聊发送器及数据库不随本包启动。

输入沿E0的24份冻结材料。A只读保存的主稿，B仅本地原文分段和节奏计划，C独立再制；8个C0移除表达候选，保留人物与必要语境。正文、首次响应、账目及呈现计划分别保存。人工边界稿保留明确来源，不记为旧主模型真实返回。

E1运输v2由模型返回`body`、`segments`字符串数组或`no_split`、`selected_expression_ids`。它沿用MaiBot原文映射，程序计算E0固定的字符区间与保护片段；E0内部数据合同保持。这个适配在真实请求前冻结，避免要求模型数长文本下标。无效分段退到完整表达稿；正文或运输对象不可用时记候选失败，原始返回保持，不追加调用挑选。

`runner prepare`只核对材料并保存32个表达位置与请求摘要，`runner run`才派发。真实执行使用当前人工HTTP/HTTPS系统代理、官方DeepSeek端点及既有本地DPAPI密钥，密钥只在内存使用。每位置零自动重试；已开始但无结果、请求未知或用量未知均停止，恢复只读已有回执，不重放。每次派发前复核代码、材料及请求摘要，同一执行目录有进程锁和排他派发标记。

```powershell
<python> -B -X utf8 -m experiments.expression_e1.runner prepare --root <fresh-run> --materials-root <E0-materials> --upstream <pinned-upstream>
<python> -B -X utf8 -m experiments.expression_e1.runner run --root <run> --credential-home <existing-data-home>
<python> -B -X utf8 -m experiments.expression_e1.review --root <run>
```

生成代码及输入在首个请求前冻结；只读审阅生成器单独记录版本，可以修复呈现问题而不改变首次正文或再次调用模型。审阅页对24组A/B/C及8组C/C0分别匿名排序，保留全文，允许无区别或都不好，结论由作者填写。页面只展示气泡计划，逐条桌面呈现与打断由E4/E5验证。

本实验不写正式人物、记忆、表达资产或业务库，不自动部署表达链；生产接入在E3。调用预算、价格及停止规则沿E0有限计划，实际费用未知时保持`null`。

修复工具：`repair prepare/run/report`冻结并验证两个协议；`repair_continue prepare/run/report`只在存在未发送证据时启用受限执行修订。完整HTTP回执可本地恢复派生结果，未知请求保留；未发送用量为不适用，原字段保持null。修复全文由`repair_review --root <repair-run>`生成，作者审阅结果不预填。
