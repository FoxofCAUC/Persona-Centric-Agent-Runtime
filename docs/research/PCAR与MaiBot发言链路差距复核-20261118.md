# PCAR 与 MaiBot 发言链路差距复核

2026-11-18。复核对象为 PCAR E.N5 实际使用的原 v8 Pro、此前 E.L4／E.N3 候选及 MaiBot 固定提交 `478f671e64467121fd3e39205c8d3507b5ce0bd6` 的普通模型私聊路径。

**当前最值得验证的差距，在正文生成前如何确定这一次要接什么话。** MaiBot 由 Planner 结合实际对话选择是否参与、接哪一条、需要交给 Replyer 哪些已知情况。PCAR 的 v8 在受理一批输入后，直接让 Pro 判断当轮该做什么并生成可见回复。角色有时第一句已经像在和熟人聊天，接下来却把对方的分享接成了任务。这个现象涉及她选择做什么，气泡和措辞只能解释其中的表达观感。[MaiBot Planner](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/prompts/zh-CN/main/maisaka_chat.prompt) · [PCAR 主回复入口](../../persona_runtime/chat.py#L496) · [E.N5 实际表现](../expression/E.N5-连续发言客服感根因复核.md)

## 同样收到私聊消息，两边如何决定发言

| 环节 | MaiBot 普通模型私聊 | PCAR E.N5 的原 v8 Pro |
| --- | --- | --- |
| 消息到达 | 有新的外部消息就唤醒 Planner。是否调用 `reply` 由 Planner 决定。`reply` 可指向一条真实消息，发送后可等待，也可继续处理。群聊的频率门控另有实现。[私聊调度](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/maisaka/scenes/private/scheduling.py) · [回复工具](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/maisaka/builtin_tool/reply.py) | 输入缓冲合并连续气泡，封存后按普通回合走主回复。Jev 判断是否检索公开资料，日常交流的接话目标和做法都留给正文模型当场决定。此次30轮均取得正文，Jev 的30次决定均为无需联网。[输入缓冲](../../persona_runtime/ingress.py#L388) · [实际回执复核](../expression/E.N5-连续发言客服感根因复核.md) |
| 正文如何起笔 | `reply` 交接真实消息目标、当前局面和查证事实，以及可选的当轮长短。工具合同限制 `chat_anlyze` 的职责，避免把回复草稿或行动建议交给 Replyer。普通创意模式装配当前目标、真实发言者、时间与引用。历史中的 Planner 输出被过滤，已发出的正文继续作为真实发言。[回复工具](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/maisaka/builtin_tool/reply.py) · [私聊模板](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/prompts/zh-CN/main/reply_private.prompt) · [上下文装配](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/chat/replyer/retro_prompt.py) | v8 保留最近双方实际消息与显式引用，以当前用户输入结束消息列表。工作集另外按词项命中选至多三对早期记录，真人和人物的旧发言均可触发。30轮共11轮带入19对早期记录，其中6对仅由旧人物回复触发。[上下文选择](../../persona_runtime/context_builders.py#L120) · [呈现及历史角色](../expression/E.N4-原v8Pro基线与桌面呈现.md) |
| 人物与口吻 | 人物身份、参与对话的行为取向、写出正文时的表达风格有各自入口。默认表达有具体语体取向，如日常社区口语、短句和省略主语。表达库可在当前会话或配置的共享范围内提供用法。[人物配置](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/config/official_configs.py) · [表达选择](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/chat/replyer/maisaka_expression_selector.py) | 角色的固定材料有明确的性格、关系和理工偏好。全批系统消息长1948字符，人物材料与通用指导随每轮发送，认真受托时确认目标、反馈进度的习惯同样常驻。30轮动态人物事实、设定和例句命中均为0，所选 B 链也没有表达条目入模。[人物资料选择](../../persona_runtime/character_context.py#L19) · [v8 指导](../../persona_runtime/companion_im.py) · [回执复核](../expression/E.N5-连续发言客服感根因复核.md) |
| 发出后的观感 | 可按配置使用分段、引用、错字处理与发送节奏，作用发生在已有正文之后。[后处理与发送](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/maisaka/builtin_tool/reply.py) | 本次30轮桌面正文全部与首次 Pro 正文指纹一致。PCAR 已有发言呈现回执、历史恢复和查前发言可见后再搜索的工程能力，这些能力可以承接未来的接话策略。[原生正文核对](../expression/E.N5-连续发言客服感根因复核.md) · [查前可见回执](../../persona_runtime/tk_notice.py) |

经过这条分工，MaiBot 的 Replyer 进入的是一段已有对象、目标和局面的交流。Planner 转交一个局面时可以说清刚才在处理哪件事、对方现在接的是哪句话。随后由对话模型自己表态。PCAR 当前也有真实双方前文，关键在于这份前文里的**当前交流阶段**与固定的人物任务习惯一起交给 Pro。分享里一旦出现熟悉技术词，模型有机会把题材误当成交办。Jev 能区分要不要网上查，不能据此区分对方是求助、转回闲聊还是在结束话题。

## PCAR 此前借过哪些部分

1. **E.L4 用过真实聊天表达和改编过的 MaiBot 私聊模板。** 试验的 S 链将真实对话、固定的末条目标、Flash 理解和已选表达一起交给 Pro。另一条 R 链继续用 v8 装配，将理解与选中条目加在当前用户原话前。脚本每个位置都会生成一份回复，目标由脚本选定。23个新场景的34轮中，有15个是单轮。该批 R 相对原 v8 获27比7，说明这套资料和装配在当批有可见价值。[E.L4 装配](../../experiments/fresh_scene_assembly.py#L104) · [新场景三链结果](../expression/E.L4-新场景三链扩测.md)
2. **结构33把理解与选材接到了 v8，修复候选进一步缩小了口吻资料。** E.N3 的 O 链将 Flash 理解及表达装进 v8 目标之前的一条 `user` 消息。修复候选 R 区分真实说话人，将理解只供选材，将跨场景候选池从33条收至7条，最近两轮用过的条目暂不参选。两条都按试验位置生成正文，也都沿用 v8 的最终接话结构。[原装配](../../persona_runtime/dialogue_assembly.py#L158) · [修复装配](../../persona_runtime/dialogue_portable.py#L157) · [来源修复](../expression/E.N3-单角色来源归属修复与扩测.md)
3. **语料能带来口语，也能把具体状态或应答习惯一并带过来。** 原问题组里“正在上课”一类状态来自一条真人表达，其影响从6轮中的5轮降到0轮。与此同时，36轮新场景中，三种具体的赞同词开头从9轮增加到23轮。作者更正后的盲审中，B、O、R 在这36轮的首选分别为22、12、2；B 随后成为 E.N5 基线。该试验同时改变了理解归属、正文参考和候选池，现有数字无法单独归因到某项修复。[E.N3 实测记录](../expression/E.N3-单角色来源归属修复与扩测.md) · [E.N4 基线决议](../expression/E.N4-原v8Pro基线与桌面呈现.md)

因此此前 PCAR 已测试过选材、模板和两阶段输入。最接近 MaiBot 模板的 S 也由脚本固定目标和每轮必须产出的正文。结构33又回到 v8 正文装配。**这几批验证过资料能怎样影响既定回话，尚没有单独验证运行时根据当下局面选择接话目标的作用。**

MaiBot 的表达学习在这条路径上将真人聊天提取为情境与说法，做自发言过滤和适用性检查，再在需要时取用。已有模型权重继续用于生成。普通默认快速档使用加权抽样，库存少于10条则跳过；精细档才启用向量筛选和额外选材模型。专用 `mai_replier` 路径的人设 system 与普通 DeepSeek 模板不同，且省去表达习惯块。群聊频率、精细选材和专用模型都需要实际配置证据才能用来解释一个具体部署。[学习实现](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/learners/expression_learner.py) · [选材实现](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/chat/replyer/maisaka_expression_selector.py) · [模型适配说明](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/docs/replyer-prompt-profiles.md)

## 删指导和历史投影分别能解释什么

- **删通用指导后出现局部改善。** 最新10个位置只删人物资料前的180字符。初读里1-07减少主动揽写稿，1-12减少未来代测邀约，2-03更直接地给了角色自己的见解；2-10保留对明确求助的必要澄清。整段删减和单次采样的证据支持继续独立检查这段指导，无法定位其中哪句话的效果。[10位删除对照](../expression/E.N5-通用发言指导删除对照.md)
- **眼下局面依然可能接错。** 2-15里对方已从排障转回吐槽。删减版仍提供更多排障动作。这里和2-03一样，实际输入中只带最近两轮，额外历史无法解释这一例；1-11删除后还出现缺少来源的版本差异。实际例子支持同时核对任务是否还在继续、建议是否有资料依据。[10位删除对照](../expression/E.N5-通用发言指导删除对照.md)
- **旧历史会放大问题，但在部分位置才成立。** 1-11的版本问题取回午餐旧发言，2-13的截图问题取回剧情旧发言。恢复隔离环境的向量模型后，30轮实际人物资料块的摘要逐份一致。首批16位的字段和历史试验取得作者认可的局部改善，仍有服务化，风格门尚未通过。[历史及向量复核](../expression/E.N5-连续发言客服感根因复核.md) · [首批因素试验交接](../HANDOFF.md)

## 现有证据能支撑的优先级

优先核对接话目标与交流阶段的交接。最小有辨识力的比较是在既有审阅原话、相同已呈现前文和 Pro 参数之下，观察明确相关的真实发言及当前局面两项信息如何改变结果。重点保护角色自己的看法、自然的反问、明确请求所需的帮助，同时检查能否随对方结束排障。表达库适合另作变量，避免把更多口语材料、模型数量或气泡变化一同算成收益。PCAR 的显式引用、角色权限、事实来源、呈现回执和恢复机制可以作为同一研究底座。

这项判断基于固定版本的上游源码和 PCAR 实际回执。尚缺同角色、同模型设置和同一段实时往返的 MaiBot／PCAR 对照。E.N5 的30条输入是真人原话，按单方发言固定回放，继续接的是角色的新回复；第二段的一处原始话题跨度超过46天。它能暴露已经发生的服务化，也会压缩真实互动的时间边界。本轮读取现成证据，新增模型调用为0，产物是本份研究记录。[E.N5 回放范围](../expression/E.N5-连续桌面体验.md) · [来源与时间差复核](../expression/E.N5-连续发言客服感根因复核.md)
