# CR1 人工正确输入诊断验收手册

2026-11-02；候选0.7.6.dev17，真实工具cr1-real-v1。CR1已完成：工程、144条机器阅读、12组用户复核及拟稿边界裁定均已结算；阶段结论为未见稳定自然度改善，不是发布通过。

## 已执行结果

24次共享v2选材和144个主位置全部完成，共169次物理调用：168成功、1次主回复超时后第1重试恢复。39项去重局部检查（33项原工程＋6项复核映射/口径）、原离线模拟及新执行器168次模拟通过，R0的34冻结场景零差异。原生24条限额预留已释放，无活跃预留、无成功重放。真实用量与模拟分别记账。

正式Shadow、表达/选择器默认v2，结构20/导出17/配置2/桥接0.7.2不变。无QQ收发、正式服务启动、私人角色副本、提交、推送或部署。实际界面与QQ送达未执行，不由本次协议测试代替。

## 用户匿名复核（已完成12/12）

打开D盘：

`exports/acceptance/PCAR-dialogue-refactor/CR1-oracle-diagnosis/real-v1/analysis/CR1-12组匿名审核.md`

共12组，均附本轮输入、必要前文、人物关注及匿名A/B回复。按当前情境判断更愿意收到哪条：像真人接话、不客服、不机械，同时考虑用户明确任务。礼貌、长解释、主动要求的复述与精确短答都可自然。自然想象不因没有逐句出处扣分。

可直接在聊天回复：

```text
01 A；02 平；03 B；04 A；05 无法判断；06 B；07 A；08 平；09 B；10 A；11 B；12 平
```

以上仅为格式示例。用户实际12组选择已经录入`analysis/user-review.json`，没有猜测理由或补造数值评分；已揭示的映射与结果见`analysis/CR1-匿名审核映射与结果.md`。无需再次审核。三比较各4组为2胜2负、2胜2负、1胜3负；这是关键案例复核，不是多人独立盲审或随机代表性抽样。

## 拟稿边界裁定（已确认）

用户已确认允许普通礼貌回请作为未发送草稿的自然发挥。首版人工Spec额外禁止了该表达，不能据此把用户未禁止的内容判为真实行动越权；真实发送、执行和明确禁止内容继续按各自要求处理。四条相关判断已在v2视图解除，原人工输入不回写。

原严格机器判断、原始输出及自然度分数均保留。裁定只追加边界复核，不重生成或追改旧R1/R4成绩，不因裁定改变三维自然度分数。

## 封存后只读核查入口

在源码根目录`C:/Users/ZhuanZ（无密码）/Documents/ChatGPT/Persona-Centric Agent Runtime`使用PowerShell：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
& 'D:/Persona-Centric Agent Runtime/.venv/Scripts/python.exe' -B scripts/dialogue_diagnosis_adjudicate.py --verify-closure
```

预期`passed=True`、`writes=0`、`generation_calls=0`，归档源码与封存证据逐项一致。开工前核对当前工作区还可加`--worktree`；后续阶段合法修改源码后，归档核对仍可通过，当前工作区比较应如实显示差异。

该入口不重写报告、不重抽匿名映射、不打开供应商客户端。封存后使用它核查完整性，不重跑会向旧包写入的prepare/simulate/run/report或复核写入入口。原生24条active预留已经为0，无需再次释放。

只运行新增执行器和报告检查：

```powershell
& 'D:/Persona-Centric Agent Runtime/.venv/Scripts/python.exe' -B -m unittest tests.test_dialogue_diagnosis_real -v
```

预期11项通过。另22项旧离线检查及R0结果已有封存证据，不默认全量重跑。材料、正常受理和原168次模拟的复现方法见[离线准备](../CR1-开发材料与离线准备.md)。旧离线入口没有真实调用动作。

实际run已结束，该包拒绝再次run或自动恢复生成。新建付费批次、改模型、改材料、重跑成功位置均不属于本手册收尾步骤。

复核口径的新增检查可单独运行`python -B -m unittest tests.test_dialogue_diagnosis_adjudicate -v`，预期6项通过；不默认重跑全量或任何旧付费实验。

## 结果与证据核对

证据根目录为D盘`exports/acceptance/PCAR-dialogue-refactor/CR1-oracle-diagnosis`：

| 项目 | 当前状态 | 证据 |
|---|---|---|
| 24新场景、两角色、48组初始绑定 | 通过 | material-check-v1 |
| 39项去重局部检查（33项原工程＋6项复核映射/口径） | 通过 | offline-checks及real-runner-checks |
| 新执行器168次模拟 | 通过，真实效果不由模拟证明 | real-runner-simulation-v1 |
| 24共享选择＋144主回复 | 完成，含一次恢复 | real-v1/receipts、results、terminal.json |
| 最终消息/供应商请求/回执指纹 | 一致 | experiment_manifest及evidence-metrics.json |
| 原生预留收尾 | 24转released，active为0 | native-reservation-cleanup.json |
| 144条机器阅读 | 完成，开发分析 | analysis/machine-assessments.json |
| 用户匿名复核 | 12/12完成 | user-review.json、user-review-resolved.json |
| 拟稿边界 | 允许普通礼貌回请，四条追加解除 | boundary-adjudication.json及policy-adjudication-v2.json |
| CR1整体 | 完成；未见稳定自然度改善 | 根目录closure.json、completion-audit.json |
| 原生界面、QQ送达、私人副本 | 未执行 | 继续由后续集成/CR4安排 |

供应商金额未知；已知total_tokens直接采用原回执，不把推理明细再叠加。一次失败用量未知，不填0。机器研究结果详见[诊断结果](../CR1-人工正确输入诊断结果.md)，用户偏好复核与机器分析分列。

## 收尾与签核

用户偏好、边界裁定及差异已记录，原机器判断与回复保留。全部位置与证据指纹已核对，阶段完成以根目录closure.json为准。未执行项不记为通过。下一包仅给出CR2建议，不自动实施、改正式默认、进入Live/M8或发起下一批模型调用。

人工材料包可以保存；本批没有实际私人副本，不能记私人验证通过。后续私人副本仍按审阅结束后清理及不提交原则执行。

签核：2026-11-02用户回复已收齐；匿名复核12/12、礼貌拟稿允许、阶段完成。逐条用户原始答复保存在D盘，不复制到源码表格。
