# M9R.0 接口合同与最小探针验收

2026-11-15。结论：规定范围、可执行合同与最小真实协议验证完成；日常原生接入及人物体验未验收。[详细合同、五位置口径与费用](../M9R.0-范围与原生接口合同.md)

| 验证层 | 实际结果 | 结论边界 |
| --- | --- | --- |
| 新增离线合同 | 22项通过，无跳过 | 请求冲突、Jev分支、补缺、作用域、工具用量及异常状态可检查 |
| 受影响原有行为 | 65项通过，无跳过 | 旧用途、重试、用量、Jev与通用检索未见定向回归 |
| 真实公开协议 | 5/5首次完成，0重试 | 普通、深入SSE、读链接及两次追问在当前账号可用 |
| 人物体验 | 未执行 | 不判自然度、长期连续性及单段式优劣 |

新增模块tests.test_m9r0_native_contract；关联模块tests.test_deepseek_compatibility、tests.test_generation_retry、tests.test_provider_usage_summary、tests.test_jev_web_switch、tests.test_general_retrieval。共87项不同用例，没有运行历史全仓。

冻结计划及回执在所选数据目录tmp/m9r0-native-contract-20261115。计划先于调用固定5位置和0重试。每位置保存无密钥request／contract、原attempt编号的raw／transport、audit、observation、result；成功位置另有隔离packet。源码指纹对应实际实现，生产链及两份配置前后指纹一致。原始协议、思考和账号绑定只留本机。

5位置共42,032tokens，计费search8／fetch1／image0，工具估算CNY0.045（deepseek-cny-2026-11-15），实际总金额未知。计费查询数不等于动作数，思考未重复加到total。两追问无工具声明／新动作，终态省略工具对象按官方合同记全0并标识来源。

异常仅由人工固定响应验证，没有真实制造失败。无DONE但含error和usage的终块按已知失败；单纯断流的最后观察用量不冒充终态。旧生产客户端对新标记在凭据读取和网络之前拒绝，不能把探针成功当成生产已支持。

继续核对仅使用只读命令，解释器跟随本实例：

```powershell
& '<所选实例>\.venv\Scripts\python.exe' -B -m experiments.m9r_native_probe report --root '<所选数据目录>\tmp\m9r0-native-contract-20261115'
```

prepare／run用于另行登记的新计划；本批五位置已经完成，**不得重新run或换目录复制同题重跑**。预留／未知位置不自动清除；report不调用模型。后续真实验证另按对应工作包材料与预算执行，不继承这批额度。

未启用原生生产链、未改正式人物／聊天／记忆、未调用真实Jev或QQ、未迁移业务、未发行或提交推送。下一阶段仅M9R.1生产传输与回执，原v0.1继续使用Jev→High检索／整理→v8。
