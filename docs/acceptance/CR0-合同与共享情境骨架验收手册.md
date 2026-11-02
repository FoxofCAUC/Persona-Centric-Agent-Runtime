# CR0 合同与共享情境骨架验收手册

日期：2026-11-02。适用dev17/cr0-contracts-v1。默认机器执行，原生界面本阶段不涉及。此手册为操作与结果索引，D盘记录才是实际证据。

## 准备

源码根为当前Git工作区，使用D盘`.venv/Scripts/python.exe`并加`-B`；TEMP/TMP指向D盘本项目tmp。关闭任何自行发起的真实模型或QQ测试操作。本工具只有prepare、simulate、report、verify，没有run。

证据根：`D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR0-contracts`。已有freeze或回执时不得重建或重放；恢复只读closure和现有结果。以下prepare/simulate仅供首次施工，不是收尾后的重复步骤。

## 操作与判断

| 项目 | 操作 | 通过标准 | 证据 |
|---|---|---|---|
| A01开工保护 | 保存HEAD、diff、未跟踪文件及保护指纹 | 既有规划保留，无正式/旧证据变化 | baseline及closure |
| A02准备 | `python -B scripts/dialogue_refactor.py prepare` | 6场景×2角色正常受理，材料/源码冻结、密钥为空 | freeze、requests |
| A03模拟 | `python -B scripts/dialogue_refactor.py simulate` | 36个主位置完成，回执一一对应，无真实调用 | receipts、results |
| A04报告 | `python -B scripts/dialogue_refactor.py report` | 旧路径无差异，新变化仅来自登记实验块，参数一致 | analysis |
| A05验证 | `python -B scripts/dialogue_refactor.py verify` | 来源/版本有效、输入指纹与客户端相等，R0的34场景零差异 | verification、r0-equivalence |
| A06定向故障 | 运行新增定向测试 | 撤回/到期/引用失效/错误绑定在发送前拒绝；写入失败不算成功、不自动恢复 | targeted-tests |
| A07字段消费 | 查F04分层视野及消费变体测试 | 新选择/观察/表达均收到前文尺寸，移除Spec无残留；旧上游缺口仍标未修复 | layer-view、测试 |
| A08隔离清理 | 人工登记替身、锁占用与邻接目录检查 | 锁占用拒绝清理；释放后副本及派生文件全清，原件/邻居保持 | 人工生命周期测试 |
| A09表达原则 | 查reading维度和六类人工锚点 | 不客服/不人机/即时通讯自然度均存在，无擅定数值、无模拟效果结论 | material |

可复制人工消息例：

- “皮影只裁出了轮廓，关节还没打孔。我就给你看一眼今天的进度。”
- “现在先不挑了，也别给配色建议。我只想抱怨一下改来改去好烦。”
- 前文：“木条长84厘米，两端各留6厘米，中间等分成6段。”本轮：“按刚才木条的尺寸算每段多长，把算式也给我。”

这些输入只在人工隔离库受理，不向工作台日常人格或QQ发送。本阶段回复是预先编写的协议夹具，不能据其好坏验证模型自然度。

## 故障与收尾

未知版本、源/父指纹不匹配、取消、超预算、已预留位置均停止；不能删回执后重跑。结果已有但终态写入失败仍是不完整。诊断修复单独保存前后版本和原因，不能修改旧基准来通过。

CR0报告元组/数组误比较修复及其原freeze保存在report-repair-v1，生成不重放。原生UI、真实模型、真实QQ和实际私人角色验证均记未执行，不填写通过。

所有A01–A09实际证据通过后，仅结算合同和模拟完成；效果仍未验证。清理失败或保护指纹变化时不得完整收尾。保留人工包及断点，下一步准备CR1，不启动真实调用。

| 项目 | 本次状态 | 复核日期/备注 |
|---|---|---|
| A01–A09 | 见D盘closure及对应证据 | |
| 原生界面 | 未执行 | |
| 真实模型/QQ | 未执行 | |
| 实际私人角色副本 | 未执行 | |
