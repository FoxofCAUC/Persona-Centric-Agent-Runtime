# 最小输入成稿转译研究

首批16位为冻结v1，作者反馈14偏转译、2相当。新增标点职责版本通过`protocol_v2`复用运行核心协议`expression-persona-draft-v2`，保持两项输入。后续40位另建计划，不使用本页首批准备器重投原16位。新版本按冻结配置在源v8与转译层之间互斥分工，尚未接转译的预告保留源处理。详见docs/expression/E3.R1-审阅确认与标点职责迁移.md。

配置`expression-persona-draft-v1`只接收`core_traits/style`口吻资料及完整主稿，固定两条消息。模块与当前人物输出链隔离；角色名、材料类型、来源与检查信息不下发模型。

```powershell
python -B -X utf8 -m experiments.expression_persona_draft.study prepare --root <新证据目录> --source-root <旧48轮run目录>
python -B -X utf8 -m experiments.expression_persona_draft.study run --root <证据目录> --credential-home <已选实例目录>
python -B -X utf8 -m experiments.expression_persona_draft.study report --root <证据目录>
python -B -X utf8 -m experiments.expression_persona_draft.study verify --root <证据目录> --credential-home <已选实例目录>
```

`prepare`冻结16个位置、输入、实际请求、源码和匿名键。8个人工能力稿位置与8份真实主稿分开标记。`run`仅调用表达模型，密钥在内存注入，来源配置与旧稿不改。

每位置只有一次派发预留，完整响应先保存；已有完整响应只本地解析，失败或未知不重放、不补位。任何技术失败停止当前批次，原始响应保持。不要通过删掉派发标记或修改计划重新运行。

`report`与`verify`不调用模型。报告默认只并排展示差异正文，原样位置保留在索引及完整Markdown中，不自动填作者评分。运行末尾核对函数的`new_model_calls=0`表示核对本身无新增请求；实际实验调用以派发标记、HTTP回执与`report.json.real_expression_calls`核对。

首批待作者确认价值，后续40份和完整多轮A/B另按修订路线推进。当前不接入正式输出或升级结构32。
