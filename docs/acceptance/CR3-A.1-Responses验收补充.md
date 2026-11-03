# CR3-A.1 Responses 验收与终止收尾补充

2026-11-03 测试已按用户要求终止：3次524＋1次供应商结果未知，未补原生回执；83局部检查和34冻结场景通过。目录清空被自动策略阻断，不能填已完成。只读核查须明确native_audit_complete=false、known gap1，不能以封存通过冒充完整真实验收。

本轮仅隔离实测，没有新工作台按钮，不启动正式服务或QQ。沿用[原三条件手册](CR3-A.1-真实消费消融验收手册.md)的固定材料及12组复核规则，证据改为Responses新包。

1. 核对authorization-and-protocol-change.json、transport-contract及execution-responses：当前Grok路由、Responses、store:false、无两类输出上限字段、无本地时限，原气泡和输入预算保留。
2. 核对新适配、执行器、报告、只读核查及最终模拟。完整模拟要求22组/66新逻辑条件/22辅助/86次mock；R0的34场景零差异。模拟与真实必须分开。
3. 检查实际wire.input与逻辑消息角色、正文和顺序相同；Manifest的实际协议与wire指纹一致。不能仅修改端点后继续解析旧choices结构。
4. 查看首计划G11辅助和原生重试回执。最多首次加3次，成功不重放；非重试拒绝立即停止，不追加兼容探针或自动换参数。未完成预留不能当作成功。
5. 查看来源限定的四来源调用账目及协议分层。原23次调用/13失败和原成功项保留，别名不当独立样本，缺失用量标未知，原总量与分项不等时不配平。
6. 固定12组齐备后才用户复核；同输入合并组免投票，不伪造平局。机器逐条阅读需有实际引文，不能用长度、关键词或评分合计代替自然度判断。
7. 结束后核对隔离库无活动/运行中/隐藏调用，临时凭据已清理，正式保护和三旧封存未变。封存后仅用scripts/dialogue_responses_audit.py只读核查，不执行旧包或回写报告。

| 项目 | 验证类别 | 结果依据 |
|---|---|---|
| 新协议与旧默认兼容 | 自动 | 局部检查及root-preflight |
| 完整22组模拟 | 自动 | 最终simulation验证 |
| 真实生成与账目 | 自动 | real-v1/state、receipts |
| 全文阅读及12组用户复核 | 机器＋用户 | analysis，未齐则未完成 |
| 来源保护与清理 | 自动 | protection、cleanup |
| 原生界面、QQ和私人副本 | 不属本轮 | 未执行 |

新包：D盘exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-responses-3。实际结果见[本轮记录](../CR3-A.1-Responses恢复实测.md)。
