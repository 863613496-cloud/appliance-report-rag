"""上市公司财报问答知识库 —— 源码包。

模块划分（对应 RAG 流水线各阶段）：
    config      路径与全局配置常量
    companies   读取/校验 config/ 下的 CSV
    download    从官方披露平台下载年报 PDF   （未实现）
    parse       PDF -> 文本 -> 文本块          （未实现）
    index       文本块 -> 向量索引             （未实现）
    retrieve    问题 -> 召回相关文本块         （未实现）
    answer      召回块 + 问题 -> 带引用的回答  （未实现）
    evaluate    在 10 道测试题上评测并写日志   （未实现）
"""
