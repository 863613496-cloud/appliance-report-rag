"""【未实现】基于召回文本块生成带引用的回答。

状态：骨架占位，尚未编写任何生成逻辑，也未接入任何大模型接口。

计划：
    * 提示词要求模型：只依据给定文本块作答、数值须原样引用、
      每条结论后附「公司简称 + 页码」、信息不足时明确回答「年报中未披露」而不臆测。
    * 生成接口按 .env 中的 LLM_PROVIDER 选择：
      anthropic（Claude 官方 SDK，如 claude-opus-5-5）或 openai_compatible。
    * 成本控制：本课程作业只对 10 道测试题运行，调用量很小；
      但仍应先跑通「不调用模型」的召回路，确认召回质量后再接入生成。

红线：
    * 不得把模型生成的数字当作年报原文写入交付物；
      参考答案必须来自 PDF 原文（可人工核对），不能由模型代为确定。
"""

from __future__ import annotations


def build_prompt(question: str, contexts: list[dict]) -> str:
    """把问题与召回块拼成提示词。"""
    raise NotImplementedError("提示词尚未设计完成。")


def answer(question: str, contexts: list[dict]) -> dict:
    """返回 {'answer': str, 'citations': [...]}。"""
    raise NotImplementedError("回答生成功能尚未实现，当前不会调用任何模型接口。")


if __name__ == "__main__":
    print("提示：生成逻辑尚未实现，此脚本当前不会发起任何 API 请求。")
