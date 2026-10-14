"""【未实现】把文本块编码为向量并建立可检索索引。

状态：骨架占位，尚未编写任何索引逻辑。

计划：
    * Embedding：本地 sentence-transformers（如 BAAI/bge-small-zh-v1.5）为首选，
      中文财报语料效果与离线可用性较均衡；在线接口作为备选。
    * 向量库：faiss-cpu 或 chromadb，二选一，索引持久化到 indexes/（已被 .gitignore 忽略）。
    * 每个向量保留文本块元数据（证券代码/公司简称/页码），用于回答时给出引用。

注意事项：
    * 索引目录可能较大且与模型绑定，不入库；
      换 embedding 模型后必须重建索引，并在 README 中记录所用模型名。
    * 14 份年报的文本量在单机可承受范围内，无需分片集群。
"""

from __future__ import annotations

from pathlib import Path


def build_index(processed_dir: Path | None = None) -> Path:
    """根据 data/processed/ 下的解析产物构建索引，返回索引路径。"""
    raise NotImplementedError("索引功能尚未实现。当前 indexes/ 为空。")


def load_index(index_path: Path | None = None):
    """加载已有索引。"""
    raise NotImplementedError("索引功能尚未实现。")


if __name__ == "__main__":
    print("提示：索引逻辑尚未实现，此脚本当前不会写入 indexes/。")
