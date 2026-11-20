# PCAR · 人格中心运行时

PCAR（Persona-Centric Agent Runtime）是一个开源项目，为 AI 人物保存独立的身份、关系和经历。你可以创建和维护同一个人物，把相处积累下来的信息带进以后的对话。

[下载 v0.2 公开桌面版](https://github.com/FoxMoreland/Persona-Centric-Agent-Runtime/releases/tag/v0.2) · [第一次使用](docs/user/getting-started.md) · [聊天记录还原](#聊天记录还原) · [产品路线](docs/product/roadmap.md)

## 人格中心与版本化

**人物有独立的身份。** PCAR 以人物组织设定、关系、会话和记忆。新建会话后，同一个人物仍沿用自己的设定和已保存的经历。

**设定的每次调整都有迹可循。** 核心人设、表达习惯和关系说明分别保留版本。你可以查看和比较变化，也可以恢复合适的旧设定。恢复会生成新版本，既有聊天记录继续保留。

## 长期记忆

PCAR 在本机保存会话和记忆，按当前话题找回相关内容，用于后续对话。重要信息可以回查来源，按需要修订或撤回。

自动记忆按角色开启，需要配置整理模型并授权。开启后，整理任务随工作台运行。

## 聊天记录还原

**新版 Windows 工作台候选**支持导入双人私聊 Markdown。核对双方身份和选用范围后，可以创建角色或接续已有角色的聊天。

- **从聊天记录生成角色卡。** 根据所选记录生成可编辑的双人角色草稿。你可以核对原话和设定，保存其中一人或两人，并选择带入历史。[角色卡指南](docs/user/双人聊天生成角色卡.md)
- **把历史接到已有角色。** 恢复所选原话、说话人和消息顺序，从最后一条接着聊，也可以查找和引用以前的消息。是否整理这段历史为长期记忆，由你在导入时选择。[历史接续指南](docs/user/历史聊天接续.md)

从当前仓库或对应候选包运行 `启动PCAR-新版.bat` 即可进入新版工作台。支持的格式、资料确认和模型条件见上面的指南。

## 远期愿景

PCAR 希望让同一个人格在对话之外也有自己的时间。角色能选择活动、学习和休息，保留未完成的事，并在暂停后接着做。长期目标是让人物从经历中形成新的理解和选择，让兴趣、习惯和关系随时间发展。

产品路线涵盖持续运行与恢复、虚拟生活、认知反思和受控成长。[产品路线](docs/product/roadmap.md) · [远期研发规划](docs/long-term/README.md)

## 开始使用

公开桌面版 **v0.2** 已验证 Windows 11 x64，提供人物与关系版本、会话历史、长期记忆与召回。下载桌面包和校验文件，按同版本指南由 Agent 准备环境，再通过 `启动PCAR.bat` 打开工作台。

使用需要网络、有效的 Windows 手动 HTTP/HTTPS 系统代理和自己的 DeepSeek 模型服务。云端调用可能产生费用。QQ 接入目前暂停。[下载桌面包](https://github.com/FoxMoreland/Persona-Centric-Agent-Runtime/releases/tag/v0.2) · [安装与迁移指南](docs/user/getting-started.md)

[文档导航](docs/index.md) · [参与共建](CONTRIBUTING.md) · [反馈问题](https://github.com/FoxMoreland/Persona-Centric-Agent-Runtime/issues) · [AGPL-3.0-only](LICENSE)
