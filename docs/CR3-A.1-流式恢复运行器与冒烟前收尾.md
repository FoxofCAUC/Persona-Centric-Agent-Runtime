# CR3-A.1 流式恢复运行器与真实冒烟前收尾

2026-11-03。承接[网关 500/524 诊断与流式传输修复](CR3-A.1-网关500-524诊断与流式传输修复.md)。本阶段把 CR3-A.1 的恢复运行器接到流式合同上，并在**任何真实调用之前**完成阶段性收尾。零真实生成调用、零 QQ、零正式服务、零私人副本；正式配置、旧封存、生产路径均未改动。

**最新状态：Windows离线准备已执行并通过，真实冒烟仍未执行。** 本机150项局部检查、R0的34零差异、22组完整模拟及首组限组演练通过；补充 `verify --smoke` 严格验收入口。见[本机准备记录](CR3-A.1-Windows冒烟前准备.md)与[真实冒烟手册](acceptance/CR3-A.1-流式真实冒烟验收手册.md)。下文Linux结果及当时待执行清单保留为历史。

## 1. 本阶段改动

新增 `scripts/dialogue_stream_recovery.py`（`cr3-stream-recovery-v1`，证据包 `CR3-A.1-real-consumption-ablation-stream-4`）。以 Responses recovery-3 运行器为骨架，冻结材料、模板、路由、重试规则（首次加最多 3 次，间隔 1/2/4 秒）、成功不重放、逐调用源校验都不变，只有下列差异：

| 项目 | 内容 |
|---|---|
| 传输 | `anthropic-messages-stream-v1`：`stream: true`，只有 90 秒空闲读超时，无总时限，不发 `max_tokens`，不发 `reasoning_effort`。鉴权沿用 recovery-2 的 bearer |
| 前序包 | 原批次 real-v1、continuation、recovery-2、responses-3 共 4 个。准备阶段依次运行原批次、recovery-2、responses-3 的只读审计（归档模式），全部通过才继续；三个恢复包都必须没有成功结果。累计保守账目：27 次预留，其中 16 次失败、1 次未知 |
| 收据 | reserve 记录 `stream: true` 与空闲时限；如果请求体不是流式，或带了 `max_tokens`，直接拒绝预留。transport 收据另记 `stream_diagnostics`（事件数、首个事件/推理/正文的毫秒数，只有数字和标志，不含文本），用于评估空闲时限是否合适 |
| `--max-groups N` | 跑完前 N 组后正常停止，`state.status=stopped`、`reason=group_limit_reached`，`run.started.json` 记录上限。用于冒烟 |
| 包内续跑 | 同一包内后建的真实批次会把先前**已结清**的真实批次的结果、辅助结果和收据纳入哈希保护，作为已完成组导入，不重放。先前批次只要有未结清收据或处于 running 状态，就阻断准备；部分完成的组仍要求另定计划 |
| 验证 | `verify` 要求所有收据都是流式、空闲时限与准备时一致、无输出上限；合计 76 个逻辑条件、24 个辅助结果 |

为便于测试，`run()` 增加了可注入的 `group_runner` 参数，默认值就是原来的 `group`。

## 2. 收尾中发现并修正的问题

上一轮写的《手工清理完成补充记录》被放进了 responses-3 的封存目录。该包 closure.json 封存了 449 个证据文件的清单，多出的文件会让 `dialogue_responses_audit.verify` 报 `evidence_inventory_changed`，而新运行器的准备阶段正好要调用这个审计，结果就是新包无法准备。现已把该文件原样移到 `PCAR-dialogue-refactor/CR3-A.1-post-seal-supplements/`，用 `cmp` 确认内容未变，并复核 responses-3 的文件清单与 449 个哈希全部一致。以后对已封存包的补充一律放在 post-seal-supplements，不写入原封存目录。

## 3. 验证（Linux/Python 3.10 沙箱，均为离线）

- **新运行器测试：** `tests/test_dialogue_stream_recovery.py` 25 项通过。覆盖沿用的守卫（成功不重放、首次加 3 次上限、请求变化拒绝重试、收据写入失败后禁止继续、已开始批次不可重启）、流式收据、非流式请求拒绝、旧合同文件拒绝、流式诊断白名单、Manifest 描述流式 wire、冒烟限组（含非法上限、失败原因不被覆盖）、包内导入与未结清阻断。
- **全量对比：** 以 `b12b40e`（流式改动之前）为基线，与当前工作区逐项比较全部 133/134 个测试模块，**没有任何一项改变状态**；新增 49 项全部通过（传输 24 项、运行器 25 项）。另有 1 项因输出交错导致基线解析漏记，单独重跑后确认修改前后都因 DPAPI 出错，状态一致。
- **沙箱的辅助措施：** 只为让测试在 Linux 上能运行，没有放进仓库。
  - 用 `fcntl` 实现的 `msvcrt.locking` 替身；
  - 在沙箱临时目录里建立与 D 盘路径同名的相对目录。
- **沙箱里跑不了的部分：** 剩余约 800 项在两边出错情况一致，全部来自环境，不能记为通过：
  - Windows 盘符路径不被识别为绝对路径：596 项；
  - DPAPI：143 项；
  - 缺少 D 盘封存证据：28 项；
  - 缺少 tkinter/httpx：30 项；
  - 其他路径或导入问题：6 项；
  - 原有的 1 项 FAIL 修改前就存在。
- **未执行：** 新运行器的 `prepare` / `simulate` / `verify` 依赖 D 盘上的真实模板、材料和封存包，在沙箱里**无法执行**，只能在 Windows 上跑（见第 4 节第 2 步）。

## 4. 真实冒烟之前，用户需要在 Windows 上做的事

1. **复跑测试（Python 3.12）：**
   `python -m unittest tests.test_dialogue_anthropic_transport tests.test_dialogue_stream_recovery tests.test_communication_consumption tests.test_dialogue_context tests.test_prompt_assembly`
   有条件的话再跑全量。
2. **离线模拟（零真实调用，运行器自带断网保护）：**
   `python scripts/dialogue_stream_recovery.py prepare --root <stream-4>\simulation-v1`
   → `simulate --root <stream-4>\simulation-v1` → `verify --root <stream-4>\simulation-v1`
   预期结果：22 组，66 个新逻辑条件，22 个新辅助结果，合计 76/24。会在 `tmp/cr3-real/` 下新建一个运行目录，前几轮约 96 MB，之后需要清理。
3. **以上都通过后**，才进入第 5 节的真实冒烟，并且需要另行明确授权。

## 5. 真实冒烟方案（待授权，未执行）

- **范围：** 新建 `real-v1`，执行 `execute --real --root <stream-4>\real-v1 --max-groups 1`。只跑第一组 G11-process-r1：1 次联合辅助，再加最多 3 个主条件（相同请求会合并为别名）。
- **调用量：**

  | 情况 | 物理调用次数 |
  |---|---|
  | 预期 | 约 4 次 |
  | 硬上限（每个槽位首次加最多 3 次重试，共 4 个槽位） | 16 次 |

- **耗时与 token：** 辅助调用参照流式探测，预计 136–219 秒；token 数量参照探测，金额未知、不估价。
- **成功判据：**
  - 辅助调用经流式完成，并通过 `parse_joint_v2` 合同；
  - 主回复完成；
  - 收据全部结清；
  - `stream_diagnostics` 具有真实流式事件证据，无JSON降级；记录首事件、推理及正文时序，不要求必须出现thinking。
- **单组验收：** `verify --root <stream-4>\real-v1 --smoke`；完整 `verify` 仍要求全批完成。
- **停止规则：** 任何一组失败，整批立即停止（运行器既有行为），先分析再决定，不自动续跑。
- **冒烟成功之后：** 另建 `real-v2`，自动导入 G11 的结果，继续剩余 21 组。这一步需要再次授权。
- **收尾：** 冒烟后清除临时凭据 sidecar；临时运行目录按惯例清理。
- **需要用户确认的参数：**
  - 空闲时限 90 秒；
  - `reasoning_effort` 不发送；
  - bearer 鉴权；
  - 冒烟范围 1 组。

## 6. 仍然未做

- **其他传输与生产路径：** Responses 传输和生产 `provider_compat.py` 仍是非流式。QQ 主回复一旦超过约 120 秒同样会失败，需要单独授权处理。
- **真实效果：** 真实网关上流式能否成功、真实自然度和人物取舍收益都未验证；固定 12 组用户复核材料仍只就绪 1 组。
- **遗留状态：** receipt 0004 结果未知；R4、CR4 未完成；路由泛化问题（16 条挑战只有 8 条一致）未修。
