"""【未实现】在 10 道固定测试题上评测，并写入 logs/evaluation.csv。

状态：骨架占位，尚未编写任何评测逻辑，因此 logs/evaluation.csv 目前只有表头。

计划：
    1. 读取 config/questions.csv 的 10 道题；
    2. 逐题调用 retrieve + answer，记录实际召回文本块与来源页码；
    3. 由**人工**核对年报原文后填写参考答案，并判定正确性与引用正确性；
    4. 结果追加写入 logs/evaluation.csv（不覆盖历史记录）。

评测列取值约定（写入时必须落在此范围）：
    正确性 / 引用正确性 ：正确 | 部分正确 | 错误 | 未评测
    参考答案必须转录自 PDF 原文，不得由模型生成后充当标准答案。

重要：本脚本不得自动填写「正确性」「引用正确性」等需要人工判断的列，
      也不得为了让表格好看而修改召回或回答结果。
"""

from __future__ import annotations

from pathlib import Path

from src.config import EVALUATION_CSV, QUESTIONS_CSV


def run_evaluation(questions_csv: Path = QUESTIONS_CSV, out_csv: Path = EVALUATION_CSV) -> Path:
    """执行评测并写出结果。"""
    raise NotImplementedError(
        "评测功能尚未实现。logs/evaluation.csv 当前只有表头，尚无任何测试结果。"
    )


if __name__ == "__main__":
    print("提示：评测逻辑尚未实现，此脚本当前不会写入 logs/evaluation.csv。")
