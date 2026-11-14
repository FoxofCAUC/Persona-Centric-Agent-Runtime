# 桌面包公开模型许可与来源

PCAR源码采用AGPL-3.0-only；以下公开模型适用其自己的MIT许可，不改变项目源码许可。Python、uv、依赖轮子和QQ客户端未随桌面包分发，安装的依赖适用各自许可。

桌面包public-model包含BAAI/bge-small-zh-v1.5的CPU ONNX资产。上游[模型说明](https://huggingface.co/BAAI/bge-small-zh-v1.5)声明MIT；本次固定转换资产来自[Qdrant仓库](https://huggingface.co/Qdrant/bge-small-zh-v1.5)，revision为46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59。文件逐一SHA256见根目录model-assets.json，包内保留该资产README的MIT声明。

另保留[FlagEmbedding上游MIT全文](FlagEmbedding-MIT.txt)，来源为[FlagOpen/FlagEmbedding LICENSE](https://github.com/FlagOpen/FlagEmbedding/blob/master/LICENSE)，包含Copyright (c) 2022 staoxiao版权声明。模型资源仅按固定公开文件打包，不包含作者角色向量、记忆索引或业务数据。
