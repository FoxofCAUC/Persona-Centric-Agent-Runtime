# QQ双后端接入与SnowLuma验收手册

**2026-11-12政策更新：QQ接口开发与实际平台验收已暂停，恢复请等待作者明确公告或推送通知。** 以下保留历史实施／操作记录，不作为当前启动、登录或补测指令。详见[暂停政策](../QQ接口开发暂停政策.md)。

2026-11-10，临时插入的QQ接入功能已实现。源码候选为 `0.9.0.dev4`，AstrBot插件为 `0.7.4`，数据库结构26、配置3、业务导出20沿用当前M9.A候选。正式业务接入继续按阶段交接执行。

## 接入方式

工作台“QQ连接”页提供 `napcat` 和 `snowluma` 两个选项。旧配置缺少新增字段时按napcat读取，保存配置时写入所选方式。

| 方式 | 客户端与登录 | PCAR负责的部分 |
| --- | --- | --- |
| napcat | 所属NapCat及专属QQ进程 | 准备组件、启动、登录状态核对、注销及所属进程退出 |
| snowluma | 用户独立运行SnowLuma与QQ | 准备AstrBot桥接、OneBot反向WebSocket、账号及客户端核对、文本收发与连接退出 |

SnowLuma方式由用户明确选定为独立客户端接入。PCAR以 `get_version_info.app_name` 核对SnowLuma身份，以 `get_login_info` 核对Bot，以 `get_status` 观察在线状态。消息仍限定为本人好友纯文本及现有本地精确引用。外部会话的登录和退出在独立SnowLuma中管理。

## 首次配置

1. 正常停止PCAR组件。在“QQ连接”选择snowluma，填写专属测试Bot、本人QQ以及本机端口，点击“保存连接设置”。SnowLuma管理页默认5099，OneBot反向监听默认6299，按本机实际端口配置。
2. 点击“准备独立组件”。SL方式只准备AstrBot及PCAR插件。组件环境可以使用 `PCAR_ASTRBOT_PYTHON` 指向已安装的专属AstrBot解释器。
3. 选定测试角色后启动服务。消息服务会生成当前连接参数。QQ连接区域显示“等待外部连接”时，可点击“查看SL连接参数”。
4. 在独立SnowLuma中登录专属测试Bot，添加OneBot v11 WebSocket客户端，即反向WebSocket连接。地址使用弹窗的 `ws://127.0.0.1:<onebot_port>/ws`，访问令牌使用该弹窗token，消息格式选择array，连接角色使用Universal。令牌按OneBot Bearer鉴权传递。
5. 启用这条SL连接，点击PCAR“检查连接”。应显示SnowLuma独立运行、QQ已登录、OneBot已连接、账号在线。账号或客户端不符时按界面状态核对。

连接令牌使用DPAPI保存，写入本机第三方原生配置时受目录ACL保护。相同账号和路由的正常消息服务重启复用令牌。更换后端、Bot、本人账号或OneBot端口后生成新令牌，再将新值填入SL。

“打开QQ登录页”在SL方式打开显式配置的本机SnowLuma管理页。登录动作在该独立客户端完成。PCAR的“停止QQ连接”会停用连接并停止所属AstrBot，正常退出软件也会释放PCAR组件。退出回执以 external_session_connection_only 说明本次连接管理范围，外部QQ注销由用户在SL确认。

## 本机后续测试根

已准备 D 盘 `exports/acceptance/QQ-SnowLuma-20261110/sl-test-runtime/中文 空格运行`。配置已选择snowluma，组件期望状态全部关闭，账号和模型密钥留待绑定。插件复制完成，使用已有公共SDK解释器，启动前设置 `PCAR_ASTRBOT_PYTHON`。

该根用于后续声明的新测试位置。M9.A.5原断点、原成功模型位置及未知投递继续按原记录结算。恢复真实测试时先核对Bot可用性、未知投递和停测条件，再冻结新的执行源码与逻辑位置。

## 已执行验证

| 层次 | 实际结果 |
| --- | --- |
| 新增专项 | 21项通过，覆盖旧配置、模式选择、令牌轮换、组件准备、原生界面、身份、启停及外部故障 |
| 针对性组合 | 48项通过，含新增21项与原桥接、登录健康、NapCat配置和启动就绪检查 |
| 既有工作台和生命周期组 | 108项中106项通过，2项旧启动测试失败，已用施工前run函数复现 |
| 已安装OneBot SDK与模拟SL对端 | WebSocket鉴权、错误令牌拒绝、账号与版本探测、send_msg、好友事件及连接释放通过 |
| 已安装AstrBot SDK | SL方式插件装载成功，合成事件与队列入口核对通过，模型调用0 |
| 实际SnowLuma与QQ | 待用户登录后的平台收发验收 |
| 账号风控变化 | 待后续实际运行观察 |

本轮使用模拟对端提供OneBot回执，相关消息ID仅代表本地协议检查。实际QQ确认送达0、真实模型调用0。

两项原有失败为 `test_cancelled_cleanup_elevation_is_visible_and_releases_lease` 与 `test_unexpected_mainloop_failure_keeps_cleanup_and_headless_exit`。它们预期启动异常调用全局强停，而施工前run函数采用所属组件清理。前后run函数AST一致，复现结果保存在 preexisting-startup-failures.json。本轮保留这两项失败。

新增界面测试第一次清理方法与复用测试类不兼容，已改为本类清理后复验通过。真实SDK装载检查退出码0，日志保留第三方SQLAlchemy取消连接及被本地网络限制拦截的元数据请求诊断。

证据根为 D 盘 `exports/acceptance/QQ-SnowLuma-20261110`，工程、SDK装载、模拟传输、插件入口及最终指纹分别留存。源码差异检查与依赖锁离线检查纳入最终收尾记录。

## 实际平台待验步骤

1. 核对专属Bot与本人账号组合，在SL完成登录并确认客户端版本，PCAR检查应全部就绪。
2. 本人发送一句普通文本，核对唯一受理、模型位置及实际平台消息ID。再验证原生引用，确保取用同角色与入口的本地档案。
3. 主动断开SL连接，检查PCAR显示离线或未知，消息服务按自身健康状态运行，外部登录问题显示待处理。更换错误Bot或其他OneBot客户端时，输入与投递领取应停止。
4. 停止QQ连接并退出PCAR，核对PCAR所属组件退出回执，SL的独立会话由用户确认。重新接入后，已成功或未知的旧投递沿原记录处理。
5. 遇明确QQ中断或连续第五次模型失败时停止实测，保存平台与模型各自账目。M9.A更正、冲突、撤回及资源观察仍按原阶段清单验收。

## 参考实现

参考MaiBot的客户端识别和按类型选择动作的方法。PCAR复用现有AstrBot反向WebSocket传输，SL私聊使用通用send_msg动作，消息归属由当前Bot与本人账号核对。[MaiBot统一QQ连接器](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter)、[客户端识别与动作画像](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter/blob/main/profile.py)
