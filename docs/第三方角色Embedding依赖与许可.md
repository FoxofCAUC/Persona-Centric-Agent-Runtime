# 角色 Embedding 第三方依赖与许可登记

日期：2026-10-29，M7.5.1开发记录，非法律意见。PCAR自身MIT许可不替代以下组件的原许可；本次只引用库接口，无第三方源文件复制。

| 组件 | 实际版本/修订 | 许可来源 |
| --- | --- | --- |
| FastEmbed | 0.9.1 | 已安装包元数据License-Expression: Apache-2.0；[项目](https://github.com/qdrant/fastembed) |
| ONNX Runtime | 1.31.1 | 已安装包元数据MIT License；[项目](https://github.com/microsoft/onnxruntime) |
| NumPy | 2.5.4 | 已安装wheel声明BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0；保留其捆绑组件许可，[项目](https://github.com/numpy/numpy) |
| tokenizers | 0.23.3 | 包分类器声明Apache Software License；[项目LICENSE](https://github.com/huggingface/tokenizers/blob/main/LICENSE) |
| BAAI原始中文模型 | bge-small-zh-v1.5 | [原模型卡MIT声明](https://huggingface.co/BAAI/bge-small-zh-v1.5) |
| Qdrant ONNX制品 | 46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59 | [固定修订README](https://huggingface.co/Qdrant/bge-small-zh-v1.5/blob/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59/README.md)中license: mit；本地README及哈希保留 |

[制品清单](model-manifests/character-embedding-bge-small-zh-v1.5.json)登记实际ONNX、tokenizer、配置及README哈希。上游BAAI与ONNX转换制品身份分别记录，不能把逻辑模型名当成下载来源证明。

依赖完整锁见uv.lock；模型权重/缓存不进入仓库。本次没有分发第三方二进制或制作安装包。将来若打包分发，须随包保留依赖和模型的许可证、NOTICE及捆绑库通知，不能只带PCAR的LICENSE。当前登记不宣称完成未来分发包的许可审计。

[实施合同](M7.5.1-本地Embedding底座与索引生命周期实施计划.md) · [开发记录](M7.5.1-开发记录与本地模型探针.md) · [参考项目总登记](施工总计划/07-参考项目借鉴与许可.md)。

## M7.6.1 编译TLS证书依赖（2026-10-31）

将已安装并已在uv.lock登记的certifi 2026.10.23提升为基础依赖，用于编译模型连接的公开CA证书验证，未关闭证书或主机名检查；原M7聊天未切换此路径。无新增安装或权重下载。CA证书来源Mozilla根证书集，原许可MPL-2.0，见[上游LICENSE](https://github.com/certifi/python-certifi/blob/master/LICENSE)。本项目MIT不覆盖该组件原许可；分发时保留通知，不复制或修改证书内容。
