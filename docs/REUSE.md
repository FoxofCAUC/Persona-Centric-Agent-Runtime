# 本地代码复用记录

PCAR 于2026-10-28按项目所有者决定采用 [MIT 许可证](../LICENSE)。以下复用来源记录继续保留；第三方组件及资源的原有许可不由本项目声明替代。

来源：用户授权复用的 `D:\AIPoweredTRPGinQQ\AIPoweredTRPG\integrations\manager`（面板 1.2）。

- `manager_tasks.py` → `persona_runtime/reused/tasks.py`：后台任务、资源互斥和 UI 事件队列。
- `manager_logs.py` → `persona_runtime/reused/logs.py`：增量 UTF-8 日志、轮转及 ANSI 清理。
- 服务宿主、Tk 页面布局和进程归属校验按原设计重构，移除了 TRPG 世界、接口、路径和端口绑定。

当前仓库包含所需代码，运行不导入旧项目。NapCat 程序文件按独立部署流程复制，不复制原配置、数据库、日志或身份缓存。
