# 第一次使用 PCAR

本指南适用于 **v0.1 公开版（程序 0.1.0）**，核对日期为 2026-11-16。它带你完成下载、准备环境和第一次对话。开发候选使用仓库根目录的候选手册；v0.1 的完整技术步骤请使用[固定版本部署手册](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/blob/v0.1/AGENT_INSTALL.md)。

## 准备什么

| 需要准备 | 说明 |
| --- | --- |
| Windows 电脑 | 已验证 Windows 11 x64 |
| 部署 Agent | Codex、DSH，或其他能读写本机文件并执行命令的 Agent |
| 模型服务 | 基础聊天所需的接口地址、模型名和你自己的 API 密钥 |
| 网络与代理 | 有效的 Windows 手动 HTTP/HTTPS 系统代理；仅 PAC 的配置暂不支持 |
| 存储位置 | 分开的程序、数据、工具目录，均选当前用户可写的位置 |

Agent 会准备 Python 3.12 x64（含 Tkinter）、uv 和依赖。桌面包已包含公开的本地检索模型，仍需联网准备其他依赖。

模型服务可能收费。发送消息、显式测试、重试、记忆整理或联网任务都可能产生调用；打开窗口和保存配置本身不调用模型。密钥通过本机安全输入填写，不放进截图、仓库或公开反馈。

## 下载桌面包

打开 [v0.1 发行页](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/releases/tag/v0.1)，下载：

- [PCAR-v0.1-Windows-x64-desktop.zip](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/releases/download/v0.1/PCAR-v0.1-Windows-x64-desktop.zip)
- [SHA256SUMS.txt](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/releases/download/v0.1/SHA256SUMS.txt)

让 Agent 核对文件校验值，再解压到独立程序目录。GitHub 自动生成的 Source code 压缩包缺少桌面包中的模型和演练资源，请选择上述桌面 ZIP。

## 让 Agent 完成准备

将解压后的文件夹作为 Agent 工作区，发送：

> 请阅读包内 AGENTS.md 和 AGENT_INSTALL.md，按 v0.1 流程部署 PCAR。先确认我是新建实例、迁移自己的旧实例，还是登记已有实例；使用分开的程序、数据和工具目录。沿用我已指定的目录和授权，集中询问缺少的模型配置，密钥通过本机安全输入填写。完成环境准备和零调用检查后，从启动PCAR.bat 打开工作台；真实模型测试先明确范围和费用。请报告实际完成结果及仍缺的配置。

三种数据方式各有用途：

- **第一次使用：** 新建空数据目录，再创建角色。
- **换电脑或换位置：** 将自己的完整迁移包导入空目标，保留原实例备份。
- **已有可用实例：** 登记原数据位置，保留原数据和环境。

已有工作台先正常退出。迁移时先核对原资料，再启用目标；不要用新建流程覆盖旧数据，也不要向正式实例导入包内人工演练数据。详细步骤见 [v0.1 迁移说明](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/blob/v0.1/docs/PCAR-环境解耦与跨机迁移.md)。

## 发送第一条消息

环境准备完成后，双击程序目录的 `启动PCAR.bat`：

1. 在首页点击“只启动人格服务”。
2. 到“人格与记忆”创建模板或导入自己的角色，选中并切换到该角色。
3. 到“模型”填写主模型接口、模型名和密钥，保存配置。
4. 到“聊天”新建或恢复会话，发送一条消息。
5. 收到回应后，可以继续交流；在“人格与关系”查看和调整人物资料。

首次体验可以先保持联网和自动记忆关闭，只配置基础聊天。人物资料与会话保存在所选数据目录中，下次从同一入口打开即可接续。

## 再按需要开启其他能力

**自动记忆。** 新建、原生导入和复制的角色默认关闭自动记忆。配置整理模型、确认资料使用范围后，按角色开启；工作台运行期间会自动提取、复核和整理后续交流中的重要信息。操作参考 [v0.1 自动记忆手册](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/blob/v0.1/docs/acceptance/M8.D-自动记忆验收手册.md)。

**联网查资料。** v0.1 的完整联网需要支持相应工具的 DeepSeek 官方接口，以及独立 Jev 服务的 HTTPS 地址、模型名和密钥。Jev 负责判断是否联网，使用自己的凭据。在“联网与资料”页开启当前角色的按需检索，按 [v0.1 部署手册](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/blob/v0.1/AGENT_INSTALL.md)完成配置；不要混用 main 候选版的联网步骤。

**数据与备份。** 人物、聊天和记忆保存在本机数据目录；调用云端模型时，相应材料会发送给配置的服务。备份与迁移按固定版本手册执行，新机重新输入密钥并确认角色资料授权。

## 正常退出与求助

工作台与托盘运行期间，已开启的后台任务会继续工作。正常退出时等待清理完成；如果退出失败，窗口会保留并显示原因。仅最小化到托盘不等于退出。当前日常入口为工作台，QQ 接入暂停。

| 遇到的情况 | 先检查 |
| --- | --- |
| 环境准备失败 | Windows 手动代理是否开启、目录是否可写、Python 是否为 3.12 x64 且含 Tkinter |
| 服务没有启动 | 首页显示的具体错误；不要占用或结束不属于本实例的端口与进程 |
| 发消息失败 | 是否已选择角色，模型地址、模型名和密钥是否正确 |
| 记忆或联网没有运行 | 对应配置、当前角色的开关与资料授权是否齐全 |
| 换机后不能调用 | 迁移目标是否重新配置凭据并完成启用 |

仍有问题时，在 [Issues](https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime/issues)注明 v0.1、出错步骤和可公开的错误信息。附图前移除密钥、私人内容与本机身份信息。

[返回首页](../../README.md) · [文档导航](../index.md)
