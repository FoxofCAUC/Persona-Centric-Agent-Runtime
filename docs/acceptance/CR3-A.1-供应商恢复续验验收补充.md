# CR3-A.1 供应商恢复续验：验收补充

本轮是用户要求恢复后的独立批次，沿用[原阶段手册](CR3-A.1-真实消费消融验收手册.md)的三条件、预算、任务边界和固定12组规则。正式工作台无新增入口，不启动正式服务或QQ。

证据使用新目录`D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-continuation`，原包只读。4个直回控制及2个完整组通过`real-v1/imports.json`引用，不再次生成；剩余22组使用新批次账目。原15次调用和新调用按来源、批次及编号分别核对，失败同样计账。

1. 查看`continuation-tests.json`、`root-targeted-checks.json`和`real-v1/analysis/targeted-checks-19.json`。后者补充了分析器更新后的完整19项检查，汇总需按测试标识去重，不能把前后运行次数相加。另有`root-audit-tests.json`的5项封存检查。
2. 查看`r0-equivalence.json`及`simulation-v1/verification.json`。本轮要求R0的34零差异，新增66逻辑条件、22辅助的模拟完整；模拟结果不得写成真实效果。
3. 查看`real-v1/state.json`和`receipts`。仅原生首次加最多3次重试，成功不重放；耗尽后应停止，不能重新执行已有`run.started.json`。连通性由首计划调用检验，没有额外收费探针。
4. 查看`real-v1/analysis/usage.json`。原包与新包分列，并保留原失败。用量缺失标未知，不按成功用量估算失败成本。
5. 查看正式数据、旧证据、模板、活动任务和凭据清理核查。全部通过后才能完成阻断收尾；保护失败不得写成收尾完成。
6. 只有固定12组齐备才发起用户审核。同输入合并组无需投票，记录为豁免，不能伪造平局或偏好。材料未齐时保留“未进行”，不补换组或套用以前阶段的用户答案。

本轮结果查[续验记录](../CR3-A.1-供应商恢复续验.md)。阶段完成与阻断收尾分开结算；工程通过不能覆盖真实采集未完成。原生界面、QQ收发和实际私人副本均不在本轮执行范围内。

| 检查 | 实际状态填写 | 证据 |
|---|---|---|
| 新增定向检查、R0与完整模拟 | 见本轮记录 | 测试及verification文件 |
| 新真实批次与全部回执 | 见state及逐次终态 | real-v1/state.json、receipts |
| 机器全文阅读与12组用户复核 | 未齐时保持未完成 | real-v1/analysis |
| 正式保护、旧封存及凭据清理 | 见收尾核查，不推定通过 | 工程核查及handoff |
| 界面、QQ及私人副本 | 未执行 | 无 |

封存后仅运行`scripts/dialogue_communication_continuation_audit.py`只读核查；不得再执行报告写入器或生成入口。未封存但已有`run.started.json`的包也禁止自动重发。
