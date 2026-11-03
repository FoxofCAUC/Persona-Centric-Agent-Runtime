# CR3-A.1：Messages已接通隔离通路，生成仍受上游阻断

2026-11-03，按用户要求，将本阶段新隔离批次切换为Anthropic Messages，取消模型生成额度和全部本地生成超时。用户明确确认省略max_tokens、拒绝即停测，并保留原气泡回复协议。本轮工程与模拟通过，真实首位置首次加3次重试全部收到中转HTTP500，已停止并完成阻断收尾，真实效果未验证。

## 实际变更

模型仍为当前网关的grok-4.7，没有换成Claude，也不描述为官方xAI或Anthropic直连。实际请求为/v1/messages，前置system消息映射为顶层有序文本块，用户与助手消息正文和顺序保留；逻辑消息指纹和实际wire指纹分别记录。

不发送max_tokens，不用0或虚构大数替代。网络连接、读取、总等待和生成执行器均没有本地时限，旧超过600秒失败停测规则在本隔离合同中取消。首次失败后最多3次重试及1、2、4秒间隔保留；来源、权限、账目异常与取消仍停止。上游仍可能有自己的默认输出和等待限制，省略参数不代表上游无限。

原单段8000字符、总正文12000字符及气泡协议保留，输入预算不变。Messages不携带OpenAI专用response_format和reasoning_effort；提示正文与既有温度保留，有效传输与旧批次不同。因此旧4直回控制和2对照组只作为历史，不写成Messages控制通过，不把跨协议变化归为规格收益。

生产ChatEngine只增加可选内部generation_executor注入，未注入时仍执行原逻辑。正式配置未变；本次有效限制来自冻结的隔离执行合同，原受理快照中的8192、180或240等只作请求值保存。新分支Manifest标明实际Messages协议、有效限制和wire指纹，模拟已验证与实际模拟客户端接收一致。

## 验证与真实结果

91项去重局部检查通过，包括79项新增与12项旧重试检查；R0的34冻结场景再次零差异。完整最终simulation-v2通过22组、66新逻辑主条件、22辅助、64唯一主回复和2别名，共86次模拟生成。simulation-v1的86次早期模拟及对应源码另存，不计作最终或真实结果。

新批次只执行原未完成的G11-process-r1辅助，没有额外付费探针：

| 尝试 | 中转结果 | 耗时 |
|---|---|---:|
| 首次 | HTTP500，new_api_error | 120.813秒 |
| 第1次重试 | HTTP500，new_api_error | 121.843秒 |
| 第2次重试 | HTTP500，new_api_error | 120.719秒 |
| 第3次重试 | HTTP500，new_api_error | 121.281秒 |

四次错误均为“upstream error: do request failed”，回执保留脱敏摘要和中转请求ID，未记录密钥或完整错误网页。实际wire没有max_tokens，运行时审计timeout_seconds为null。这里是中转主动返回的错误，不是PCAR的180或240秒本地计时到期；未收到明确缺少max_tokens的拒绝，但没有成功生成，仍不能宣布当前Grok路由已兼容或已恢复。

新增4次调用全失败，零新主回复、零新辅助，剩余22组未完成。合并前两包累计23次物理调用：10成功、13失败，回执报告的total_tokens累计59822，13次失败用量及金额未知。网关原total_tokens与prompt、completion分项之和不完全相等，各原值独立保留，不人为配平。旧成功未重放，仍有原8唯一主回复、2辅助及2别名，合计10/76逻辑主条件，66未完成。

旧8主、2辅助和6配对全文阅读已按封存哈希导入并复核，属于历史。两组G08比较均平，不能外推聊天自然度收益；Messages本批没有可评价正文。固定12组只有1组齐备，未发用户审核，不把未执行算空选、平局或质量失败。工程、语义、偏好和归因分开结算，不关闭R4-A/C、R4-B、路由和CR4。

## 封存与断点

证据根：D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-recovery-2。

重点查看closure.json、handoff.json、real-v1/state.json、real-v1/generation-audit.json、逐次receipts、real-v1/analysis、root-consolidated-tests.json、protection.json和cleanup.json。原两包封存不改；旧19次调用和本轮4次按来源、批次及编号分别记账，失败全部保留。

177个隔离库已核对无活动预留、运行中任务及隐藏选择/观察调用，连接已释放，临时鉴权已删除。7538项正式保护绑定、132个旧受理模板文件及库存均未变，两份旧封存只读核查通过。后续仅使用scripts/dialogue_messages_audit.py只读核查，不再次execute任何已有run.started.json或回写封存报告。再次生成需要明确的新恢复授权与独立包，不自动重发。

正式Shadow、表达与选择默认v2、候选0.7.6.dev19及结构20/导出17/配置2/桥接0.7.2保持。零QQ、正式服务、实际私人副本、正式启用、提交、推送或部署，原生界面未执行。

结论：**Messages隔离接入及本地限额调整完成、模拟通过；真实生成仍被中转上游HTTP500阻断，本阶段真实验证未完成。**

协议依据：[Anthropic Messages规范](https://platform.claude.com/docs/en/api/messages/create)、[中转站原生接入说明](https://e-flowcode.cc/docs/setup/claude-code-reference)。规范与接入说明不能代替当前路由的真实兼容验证。

[验收补充](acceptance/CR3-A.1-Anthropic-Messages验收补充.md)
