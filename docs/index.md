# 超级大肥鱼 · PCAR 文档导航

v0.2 的程序版本为 0.2.0，研发接续从工作台重构开始。[场景发言与回退](expression/E.N6-场景发言接入与回退.md)

了解人格持续存在、自主生活与成长方向，从[产品路线](product/roadmap.md)开始；体验当前公开能力，从[用户入门](user/getting-started.md)开始。下列用户操作链接固定到 v0.2，开发资料跟随当前仓库。

## 使用 PCAR

| 任务 | 文档 |
| --- | --- |
| 下载、准备环境、第一次对话 | [用户入门](user/getting-started.md) · [v0.2 完整部署手册](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/blob/v0.2/AGENT_INSTALL.md) |
| 管理人物、表达和关系资料 | [v0.2 人物操作说明](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/blob/v0.2/docs/M5.md) · [界面操作手册](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/blob/v0.2/docs/acceptance/M5-人格与关系真人验收手册.md) |
| 开启和查看自动记忆 | [v0.2 自动记忆操作手册](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/blob/v0.2/docs/acceptance/M8.D-自动记忆验收手册.md) |
| 了解自动调用的额度 | [v0.2 调用额度与用量说明](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/blob/v0.2/docs/自动调用额度与人工免限额.md) |
| 备份、迁移与登记已有实例 | [v0.2 原机升级与跨机迁移](../AGENT_INSTALL.md#原机从-v01-升级) |
| 了解 QQ 接入状态 | [暂停政策](QQ接口开发暂停政策.md) · [后续跟踪方向](SnowCat-持续跟踪与QQ恢复开发指导.md) |

部分操作文档仍沿用 M5、M8.D 等研发阶段名称；其部署前提以同版本完整部署手册为准。QQ 历史接入资料仅供追溯，当前使用工作台。

## 了解产品

- [参与共建](../CONTRIBUTING.md)：对话体验、记忆场景、界面与开发的贡献入口。
- [产品路线图](product/roadmap.md)：当前基础、自然交流、持续生活与经历驱动成长。
- [产品目标与参考线](AI伴侣产品方向与大肥鱼参考线.md)：围绕人物和相处体验作出开发取舍。
- [远期规划](long-term/README.md)：持续人格、虚拟生活、自主能力与受控成长。

## 开发与研究

| 目的 | 入口 |
| --- | --- |
| 接续当前开发 | [协作约定](../AGENTS.md) · [当前状态](STATUS.md) · [交接](HANDOFF.md) |
| 了解已部署的场景发言 | [E.N6 接入与回退](expression/E.N6-场景发言接入与回退.md) · [E.N5 场景研究与验收](expression/E.N5-场景人设拆分与Jev决策施工指导.md) |
| 遵守模型输入创作与使用边界 | [模型输入与提示词写作总则](模型输入与提示词写作总则.md) · [E.N0否决与职责纠偏](expression/E.N0-作者否决与开发代理职责纠偏.md) |
| 部署开发候选 | [当前部署手册](../AGENT_INSTALL.md) · [模型供应商支持总则](模型供应商支持总则.md) |
| 理解持久化与运行结构 | [架构说明](architecture.md) · [命令行操作](usage.md) |
| 接续人物回复重构 | [E.N5接话修复专项](expression/E.N5-人物接话专项修复施工指导.md) · [E.N5连续桌面体验](expression/E.N5-连续桌面体验.md) · [最终基线与桌面呈现](expression/E.N4-原v8Pro基线与桌面呈现.md) · [角色的归属修复与扩测](expression/E.N3-单角色来源归属修复与扩测.md) · [E.N3角色的单角色留出对照](expression/E.N3-单角色留出对照.md) · [E.L5人物回复候选](expression/E.L5-人物回复候选接入与验收.md) · [E.N0—E.N6开发指导](expression/E阶段重构-人物回复链路开发指导.md) · [已否决候选记录](expression/E.N0-人物口吻与提示候选交付.md) · [MaiBot机制研究](research/MaiBot-活人感机制深读-20261116.md) · [PCAR 与 MaiBot 发言链路差距](research/PCAR与MaiBot发言链路差距复核-20261118.md) |
| 准备真实聊天学习语料 | [语义选材后的正文对照](expression/E.L4-语义选材后的正文对照.md) · [已审语义终选](expression/E.L4-语义终选16位真实验证与审阅.md) · [选材修订与回归](expression/E.L4-选材修订回归与语义终选待审.md) · [详细施工规划](expression/真实聊天语料与MaiBot表达学习施工规划.md) |
| 追溯此前表达方案 | [原表达施工方案](PCAR-MaiBot表达改造施工方案.md) · [原最小输入转译路线](expression/E3R-成稿转译修订路线.md) |
| 了解记忆与召回 | [自动记忆实施](M8.D-自动记忆实施记录.md) · [召回接入与验证](召回配套.3-实施与验证.md) |
| 运行相关检查 | [分级验证原则](开发验证与验收分级原则.md) · [验收目录](acceptance/README.md) |
| 更新 README 与接续其他窗口 | [README 维护与跨窗口交接指南](product/readme-maintenance.md) |
| 研究关系认知与人格演化 | [社会科学指导](social-science/README.md) · [远期研发计划](long-term/README.md) |
| 查阅总体安排 | [项目规划](pcar_project_plan_zh.md) · [规划衔接](后续开发指导与规划衔接.md) |

研发资料包含历史状态。阅读“当前”“待审阅”“下一阶段”时结合该段日期，并以 STATUS、HANDOFF 中后续生效的记录为准。

## 版本与来源

[公开发行](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/releases)保存可下载版本；[CHANGELOG](../CHANGELOG.md)、[当前状态](STATUS.md)和[验收目录](acceptance/README.md)记录研发变化。

[AGPL-3.0-only](../LICENSE)为项目当前许可。[代码复用记录](REUSE.md)、[第三方模型许可](licenses/THIRD-PARTY-MODELS.md)与[历史 MIT 许可](licenses/PCAR-MIT-20261028.txt)分别说明相应来源。

[返回首页](../README.md)
