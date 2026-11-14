# DeepSeek官方API接入与适配

## 2026-11-15 M9R 接入方向

后续云端生成与应用能力只支持DeepSeek官方API，Jev为独立联网判断例外，见[供应商总则](模型供应商支持总则.md)和[M9R开发手册](M9R-DeepSeek原生能力接入与后续研发规划.md)。当前发行仍为v0.1；本节是已采纳的迁移方向，未改变正式配置或启用原生工具。

M9R首版沿用Chat Completions和`deepseek-flash`，分别定义普通聊天、原生联网、后台结构化任务的请求合同。原生联网需要保存工具调用、引用、计费用量和必要回传字段，不沿用“只取正文”的传输假设；旧JSON格式和采样参数不能直接搬入原生工具请求。先以原生联网任务替换High计划与同步整理，再由v8回应；单段式通过体验比较后才能成为默认。

Jev none时不新增联网；Jev web和实际资料取得分别记录。角色、经历和长期知识由PCAR管理，协议回传包按作用域保存，思考内容不作为人物自述或长期记忆。型号能力、错误与费用合同在M9R.0定向核对，不自动切换协议或增加重试。详细实现与验收以M9R六个工作包为准。

## v0.1 已有接入与历史适配

下文保留现有配置方法和此前适配合同。当前v8人物回复使用原生文本，High检索计划／整理可启用高强度思考；早期“结构化主回复／关闭思考”的描述仅适用于各自用途，不代表全部当前请求，更不覆盖上方M9R合同。

## 连接填写

官方当前推荐的模型ID为`deepseek-flash`，另提供`deepseek-v4-pro`；Flash实际服务版本及兼容别名以[官方接入说明](https://api-docs.deepseek.com/)为准。以下使用Flash，运行时不替用户选择或改写模型名。

| 工作台项 | 填写或核对 |
|---|---|
| 基础URL | `https://api.deepseek.com`；兼容`/v1`及误填的完整`/chat/completions`地址 |
| 模型名称 | `deepseek-flash`，后续目录检查必须确实包含所填ID |
| API Key | 在本机工作台保存，不写入文档、聊天、日志或Git |
| 主模型 | 修改URL、模型和Key；保留原timeout、max_tokens和正常额度 |
| 角色选择模型 | 这是独立配置，也须修改；可用既有“复制主模型配置”入口，再核对其参数 |
| 编译／情境理解 | 复用角色选择连接，分别记账，无需增加新凭据入口 |
| 整理模型 | 若启用复用主模型则随主模型；独立配置时也须自行核对 |

本项目使用Chat Completions，不能填写Anthropic兼容或Responses接口。名称不明的第三方代理别名不猜测能力；官方精确域名或已登记DeepSeek模型名才启用本适配。旧`deepseek-chat/reasoner`仅保留兼容，不能作为当前模型目录保证。

连接修改保留现有参数和正常额度。目录可用、实际生成、阶段质量分别验证。

## 传输与校验

- 当前DeepSeek结构化接口使用`response_format={"type":"json_object"}`。主回复、候选选择、倾向编译、情境理解、整理分别声明结构化用途；连通性纯文本探针不启用JSON模式。原Schema、字段白名单、重复键、容量、引用及回复验证继续在本机执行。[JSON模式合同](https://api-docs.deepseek.com/guides/json_mode/)
- 已登记的当前型号显式发送`thinking={"type":"disabled"}`，减少交互中的推理等待；旧单模式别名保留`provider_default`，不宣称已关闭。未增加SDK或运行依赖，未改原messages、Prompt或输出预算。[Chat Completions参数](https://api-docs.deepseek.com/api/create-chat-completion/)
- 只展示`message.content`；`reasoning_content`既不作为答案，也不进入审计正文。只保留有限诊断及白名单数值用量，包括缓存和推理token，不伪造金额。
- 空白正文、截断、上游中止、资源不足、过滤或意外工具调用明确失败并保留用量；不展示半份结果，不自动改协议、修补JSON或追加调用。已冻结Prompt缺JSON合同则调用前失败，不暗中加词。
- TLS证书校验、原单次总截止、响应大小上限和无跳转规则保持；不因保活空白而无限延长等待。工作台补充余额不足、参数错误、过载及输出异常提示。
- 各用途Manifest在调用前增加实际`provider_request_contract`，记录传输格式、thinking及格式指纹；不只记录抽象回复协议。旧历史Manifest保持原样。

当前官方连接及R06实际生成已完成；[审阅准备与质量边界](acceptance/M7.6.3-R06-DeepSeek官方API续跑与审阅准备.md)。
