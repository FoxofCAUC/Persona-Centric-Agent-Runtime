# 本地代码复用记录

PCAR 于2026-11-11按项目所有者决定采用 [AGPL-3.0-only](../LICENSE)，仅适用第3版。2026-10-28采用的[MIT许可](licenses/PCAR-MIT-20261028.txt)作为历史版本记录保留。以下复用来源与第三方版权声明继续保留，项目许可变更不改写各来源原有许可。

来源：用户授权复用的 `D:\AIPoweredTRPGinQQ\AIPoweredTRPG\integrations\manager`（面板 1.2）。

- `manager_tasks.py` → `persona_runtime/reused/tasks.py`：后台任务、资源互斥和 UI 事件队列。
- `manager_logs.py` → `persona_runtime/reused/logs.py`：增量 UTF-8 日志、轮转及 ANSI 清理。
- 服务宿主、Tk 页面布局和进程归属校验按原设计重构，移除了 TRPG 世界、接口、路径和端口绑定。

当前仓库包含所需代码，运行不导入旧项目。NapCat 程序文件按独立部署流程复制，不复制原配置、数据库、日志或身份缓存。

## 聊天记录导入与人设生成

2026-11-19。LT-13 固定核对 Lume、溯洄 suhui、QQ 聊天记录分析工具及两个导出格式来源。Lume 和 suhui 的 MIT 纯逻辑是局部移植候选，GPL v3 系列项目用于机制或文件格式参考。已核对的提交、许可原文和计划复用范围见 [LT-13 上游许可与复用范围](long-term/06-聊天记录导入与人设生成.md#上游许可与复用范围)。移植前按实际文件及依赖登记版权、许可、修改和交付范围。

## 工作台设计参考

2026-11-19。来源为 Chevey339 的 Kelivo，固定到 [v1.3.6／c2b4895](https://github.com/Chevey339/kelivo/commit/c2b48958de39c3a1715347d33fa8887bdef974ce)。上游根 [LICENSE](https://github.com/Chevey339/kelivo/blob/c2b48958de39c3a1715347d33fa8887bdef974ce/LICENSE) 为 GNU AGPL v3，与 PCAR 的 AGPL-3.0-only 在同一许可证版本上兼容。本次采用其桌面分栏、窄窗导航以及消息和输入区的相对位置，使用独立绘制的 PCAR 草图。[工作台终端交互定义](product/工作台终端交互定义.md#桌面版式--参考-kelivo)

如后续移植文件，逐项核对其来源与适用许可，保留版权、许可及变更声明，按 AGPL v3 的分发条款提供对应源码。上游品牌标识和第三方图像、图标、字体等素材按各自权利和许可核对。[Kelivo README](https://github.com/Chevey339/kelivo/blob/c2b48958de39c3a1715347d33fa8887bdef974ce/README.md) · [AGPL v3 条款](https://www.gnu.org/licenses/agpl-3.0.html)
