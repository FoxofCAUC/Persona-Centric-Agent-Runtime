# CR3-A.1 网关 500/524 诊断与流式传输修复

2026-11-03。本文是对 CR3-A.1 四轮真实恢复连续被中转站 e-flowcode.cc（grok-4.7）阻断的离线诊断，以及隔离实验传输的修复。只改实验工具 `scripts/dialogue_anthropic_transport.py` 及其测试；生产路径、正式配置、旧封存和既有运行批次均未改动。零真实生成调用、零 QQ、零正式服务。

## 结论

**根因（强推断）：所有实验传输都发送 `stream: false`。联合辅助调用要先推理很久才出正文，网关在约 120 秒内收不到任何字节就断开连接。** 根据失败的位置，分两种表现：

- 中转程序自身的上游超时：HTTP 500 `upstream error: do request failed`，耗时 120.7–121.8 秒（Messages recovery-2，4/4）。
- 前置 Cloudflare 超时：HTTP 524，耗时 125.8–126.5 秒（第一批、continuation、Responses recovery-3 等）。

“换协议也无效”是因为 chat、Anthropic Messages、Responses 三种传输都写死了非流式，协议从来不是变量。同一 Key 在 Kelivo 中能正常聊天，原因是 Kelivo 通常默认流式，而且普通聊天很短。Key、额度、模型路由、请求体大小都不是主要因素。

**限定：** 回执没有保存响应头。Cloudflare 的判断来自 524（Cloudflare 专有状态码），中转程序的判断来自 500 的错误文案格式，都没有经过网关方确认。中转超载、偶发不稳定不能完全排除，但耗时集中在两个固定值，更符合固定时限。

## 证据

| 证据 | 观察 |
|---|---|
| CR3-A.1 第一批 15 次调用 | 主回复 11 次，耗时 3.8–17.4 秒，全部成功；辅助调用 49.7 秒、87.3 秒两次成功；另外 5 次辅助调用全部在约 126 秒处返回 524 |
| 独立探测 `long-anthropic-messages-no-client-limits-9` | 同网关、同一份 G11 辅助请求，**`stream: true`**，9/9 成功；首个 thinking 4.3–6.5 秒，正文首字 130–212 秒，总耗时 136–219 秒 |
| 同一探测的 Messages 非流式版本（recovery-2） | 4/4 在约 121 秒返回 500 |
| 辅助调用的推理量 | 未发送 `reasoning_effort`；推理约占输出 93%，每次 1.0–1.6 万输出 token，约 73 token/秒，因此天然需要 140–220 秒 |

证据位置：D 盘 `exports/acceptance/PCAR-dialogue-refactor/` 下各包的 `real-v1/receipts/*.transport.json`，以及 `long-anthropic-messages-no-client-limits-9-*/summary.json`。

## 修复

新增**显式选用**的合同 `anthropic-messages-stream-v1`（`stream_transport_contract()`）：

- 请求体 `stream: true`，其余字段与旧合同相同；仍省略 `max_tokens`，不改推理强度。
- SSE 事件折叠成与非流式完全相同的消息对象，再交给原有的 `parse_message`。截断、拒答、tool_use、空正文、用量合计的判定保持一致。推理文本只计数，不保留。
- 唯一的本地限制是**空闲读超时**，默认 90 秒没有收到任何数据才判失败，可设为 `None` 关闭；没有总时限，也没有输出上限。
- 失败映射到已有的可重试错误码，`generation_retry.py` 不用改：

  | 情况 | 错误码 | 用量 |
  |---|---|---|
  | 在 `message_stop` 之前断流 | `provider_network_error` | 未知（None），不当作 0 |
  | 流内 `error` 事件 | `provider_aborted`，附带脱敏后的 provider_error | 未知 |
  | 沉默超过空闲时限 | `provider_timeout`，`phase=streaming` | 未知 |
- 网关忽略 `stream` 参数、直接返回 JSON 时，按非流式解析，并标记 `stream_downgraded`。

旧合同 `anthropic-messages-uncapped-v1` 保持不变，作为默认值。已封存批次的分析脚本用默认合同重建请求体再比对，因此不受影响。

## 验证

- **旧合同逐字节等价：** HEAD 与修改后的模块对 17 组输入（含报错路径与合同字典）输出逐字节相同。
- **单元测试：** `tests/test_dialogue_anthropic_transport.py` 共 41 项通过（原有 17 项，新增 24 项），连跑 5 次稳定。
- **变异检查：** 把流式请求体强制改回 `stream:false`，有 3 项失败；去掉空闲超时，空闲超时测试在 5 秒内明确失败，不会挂死测试套件。
- **真实 aiohttp 本地模拟：** 本机 HTTPS 服务模拟“上游沉默超过时限就返回 524”的网关，时间按比例缩到 2 秒；生成需要 4 秒，期间每 0.5 秒推送一次 thinking。

  | 场景 | 结果 |
  |---|---|
  | 旧合同 | 2.01 秒返回 524 |
  | 流式 | 4.01 秒成功，正文与最终用量完整 |
  | 上游沉默 | 1 秒后 `provider_timeout`，phase 为 streaming |
  | 中途断开 | `provider_network_error`，phase 为 streaming |

  这是模拟，不能证明真实网关的行为。
- **相关模块回归：** 依赖本传输的 9 个测试模块，修改前后每项测试的状态都相同，没有回归。其中 72 项在 Linux/Python 3.10 沙箱中因环境出错，修改前后一致：69 项写死了 D 盘 tmp 路径，3 项缺 `msvcrt`。这些测试未在本环境执行，需要在 Windows/Python 3.12 复跑，不记通过。

## 未做与边界

- **运行器未接线：** 现有恢复运行器仍写入旧合同；任何新批次都要新建运行器版本和独立证据包，并经用户明确授权。
- **其他传输未改：** Responses 传输（`dialogue_responses_transport.py`，会校验并拒绝 `stream=True`）没有改。
- **生产路径未改：** 生产 `persona_runtime/provider_compat.py`（同样是 `stream:false`）没有改；QQ 主回复一旦超过约 120 秒，同样会失败，需要单独授权。
- **推理强度未动：** `reasoning_effort` 没有设置，以保持冻结的实验条件。
- **真实效果未验证：** 真实网关上的流式成功率、空闲超时取值是否合适，都要等授权后的小规模冒烟才能下结论。
- **封存与费用：** `--worktree` 模式的封存核查会因为本文件改动报 `worktree_changed`，这是预期行为；默认模式读取归档，不受影响。receipt 0004 的结果仍然未知，金额未知。
