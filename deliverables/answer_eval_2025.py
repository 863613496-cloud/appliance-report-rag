"""10 道固定题的确定性答案测试（真实检索结果，不调用大模型）。

运行：
    python -I deliverables/answer_eval_2025.py

输出：
    deliverables/answer_test_2025.json   完整机器可读结果与审计
    deliverables/answer_test_2025.md     可读报告与人工复核清单

边界：
- 题目文本来自 ``config/questions.csv``，本脚本不改题。
- 答案由 ``src/answer.py`` 的规则生成，不是大模型生成，也不是人工答案。
- 引用完整率只检查字段是否齐全，**不是答案准确率**。
- 没有人工逐题核验，答案准确率一律记为“未评测”。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.answer import (  # noqa: E402
    ANSWER_ACCURACY,
    METHOD_NOTE,
    answer_questions,
    summarize_answers,
)
from src.config import make_output_robust  # noqa: E402

OUT_JSON = ROOT / "deliverables" / "answer_test_2025.json"
OUT_MD = ROOT / "deliverables" / "answer_test_2025.md"
TOP_K = 8
DETAILED = {"Q04", "Q07", "Q09", "Q10"}


def _md_cell(value: object, *, limit: int | None = None) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\n", "<br>")
    if limit is not None and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _metric_table(answer: dict) -> str:
    metrics = answer.get("指标") or []
    if not metrics:
        return "_没有可列出的指标记录。_"
    lines = [
        "| 公司 | 代码 | 指标原文 | 数值 | 单位 | 数据页 | 表头页 | blocked | 采信 | 表头核验 | 表名 |",
        "|---|---|---|---:|---|---:|---:|---|---|---|---|",
    ]
    for metric in metrics:
        lines.append(
            "| {company} | {code} | {metric} | {value} | {unit} | {page} | {header} | {blocked} | {adopted} | {header_check} | {table} |".format(
                company=_md_cell(metric.get("公司简称")),
                code=_md_cell(metric.get("证券代码")),
                metric=_md_cell(metric.get("指标原文")),
                value=_md_cell(metric.get("数值")),
                unit=_md_cell(metric.get("单位")),
                page=_md_cell(metric.get("数据页码")),
                header=_md_cell(metric.get("表头来源页码")),
                blocked="是" if metric.get("blocked") else "否",
                adopted="是" if metric.get("是否采信") else "否",
                header_check=_md_cell(metric.get("表头核验状态")),
                table=_md_cell(metric.get("表名"), limit=60),
            )
        )
    return "\n".join(lines)


def _evidence_table(answer: dict) -> str:
    metrics = answer.get("指标") or []
    if not metrics:
        return ""
    lines = [
        "| 公司 | 指标原文 | 证据原文 | 口径说明 | 计算依据 |",
        "|---|---|---|---|---|",
    ]
    for metric in metrics:
        lines.append(
            "| {company} | {metric} | {excerpt} | {note} | {calc} |".format(
                company=_md_cell(metric.get("公司简称")),
                metric=_md_cell(metric.get("指标原文")),
                excerpt=_md_cell(metric.get("证据原文"), limit=100),
                note=_md_cell(metric.get("口径说明"), limit=100),
                calc=_md_cell(metric.get("计算依据"), limit=100),
            )
        )
    return "\n".join(lines)


def _audit_section(answer: dict) -> str:
    qid = answer.get("题目编号", "")
    if qid not in DETAILED:
        return ""
    audit = answer.get("审计") or {}
    lines = [f"### {qid} 详细审计", ""]
    if qid == "Q04":
        lines += [
            "核对要点：表A与表B的**表名、页码、逐项金额、差额**；"
            "本页只并列，不选择“正确值”。",
            "",
        ]
    elif qid == "Q07":
        lines += [
            "核对要点：数据页与表头来源页是否真的对应；"
            "表头状态是否仍为“算法判定，待人工核验”；"
            "金额和占比是否都标记为未采信。",
            "",
        ]
    elif qid == "Q09":
        lines += [
            "核对要点：研发投入强度是否取自未阻断页；"
            "科沃斯境外收入页是否仍为 blocked；"
            "是否没有把 blocked 数值用于完成对比。",
            "",
        ]
    elif qid == "Q10":
        lines += [
            "核对要点：是否按公司名单顺序展示、没有排名；"
            "各公司原文指标名与分母来源；blocked 公司与待核验表头；"
            "同公司多表（尤其长虹美菱）的金额差异。",
            "",
        ]
    lines.append("```json")
    lines.append(json.dumps(audit, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines)


def build_markdown(payload: dict) -> str:
    summary = payload["统计"]
    answers = payload["题目结果"]
    lines = [
        "# 2025 年 10 道固定题：确定性答案测试报告",
        "",
        f"生成时间：{payload['生成时间']}",
        "",
        "## 结论摘要",
        "",
        f"- 题目数：{summary['题目数']}",
        f"- 回答状态分布：{json.dumps(summary['状态分布'], ensure_ascii=False)}",
        f"- 引用完整率：{summary['引用完整率（%）']}%（{summary['引用完整记录数']}/{summary['引用记录数']} 条记录字段齐全）",
        f"- 阻断证据误用数：{summary['阻断证据误用数']}",
        f"- 跨公司混淆数：{summary['跨公司混淆数']}",
        f"- 确定性作答数：{summary['确定性作答数']}",
        f"- **答案准确率：{summary['答案准确率']}**（没有人工逐题核验）",
        f"- **人工核验状态：{summary['人工核验状态']}**",
        "",
        "> 引用完整率只说明公司、代码、年份、指标、数值、单位、页码、"
        "证据原文这些字段是否齐全，**不说明数值是否与 PDF 一致**，"
        "更不是答案准确率。本报告未计算“检索证据充分率”。",
        "",
        "## 数据与方法",
        "",
        f"- 题目来源：`config/questions.csv`（固定 10 题，未改题）。",
        f"- 检索：`src/retrieve.py` 的 BM25 + 字符 n-gram TF-IDF + RRF，"
        f"top_k={payload['检索设置']['top_k']}，mode=auto。",
        f"- 答案：`src/answer.py` 的规则抽取；{METHOD_NOTE}",
        "- 未调用大模型 API；未开发 Streamlit；未修改原始 PDF、解析 JSONL、"
        "异常记录、固定测试题或既有检索判定标准。",
        "",
        "## 状态分布",
        "",
        "| 状态 | 题数 |",
        "|---|---:|",
    ]
    for status, count in summary["状态分布"].items():
        lines.append(f"| {status} | {count} |")
    lines += [
        "",
        "## 逐题结果",
        "",
    ]
    for answer in answers:
        qid = answer.get("题目编号", "")
        lines += [
            f"## {qid}",
            "",
            f"**问题**：{answer.get('问题', '')}",
            "",
            f"**回答状态**：{answer.get('回答状态', '')}",
            "",
            f"**确定性作答**：{'是' if answer.get('确定性作答') else '否'}；"
            f"**人工核验状态**：{answer.get('人工核验状态', '')}",
            "",
            f"**检索范围证券代码**：{'、'.join(answer.get('检索范围证券代码') or []) or '未限定'}",
            "",
            "**答案正文**：",
            "",
            answer.get("答案正文", ""),
            "",
        ]
        if answer.get("口径说明"):
            lines += ["**口径说明**：", ""]
            lines += [f"- {note}" for note in answer["口径说明"]]
            lines.append("")
        if answer.get("警告"):
            lines += ["**警告**：", ""]
            lines += [f"- {warning}" for warning in answer["警告"]]
            lines.append("")
        if answer.get("拒答原因"):
            lines += ["**拒答原因**：", ""]
            lines += [f"- {reason}" for reason in answer["拒答原因"]]
            lines.append("")
        lines += ["**指标与引用**：", "", _metric_table(answer), ""]
        evidence = _evidence_table(answer)
        if evidence:
            lines += ["**证据原文与口径**：", "", evidence, ""]
        audit = _audit_section(answer)
        if audit:
            lines += [audit, ""]
    lines += [
        "## 仍需人工确认的项目",
        "",
        "以下项目均为程序或助手文本检查，**不是人工确认**：",
        "",
        "1. **全部 10 题**：答案中的数值、单位、公司、年份、指标原文、"
        "数据页码和表头来源页，仍需人工对照原始 PDF。",
        "2. **Q04**：表A与表B的两组收入、表名、页码和差额已并列；"
        "“差异已发现、原因未明”，需人工确认差异原因，不能预先选定一组。",
        "3. **Q07**：线上销售金额与占比依赖 p18 表头；跨页状态仍为"
        "“算法判定，待人工核验”，需人工确认表头与 p19 数据行的对应关系。",
        "4. **Q09**：科沃斯 p20 境外收入为 blocked，不能作为已确认事实；"
        "需人工确认是否存在其他可靠披露，以及两家公司研发投入强度的口径。",
        "5. **Q10**：海外业务定义、指标名和分母尚未统一，未排名；"
        "需人工确认 14 家公司的原始披露、科沃斯 blocked 页、待核验表头，"
        "以及长虹美菱 p18/p19 两张分地区表的金额差异。",
        "6. **Q02**：本次检索结果未见独立“空调”分产品行；需人工确认"
        "整份年报是否确实没有独立空调披露，不能把“消费电器”改写为“空调收入”。",
        "7. **Q03**：境内/境外占比的分母采用分地区表合计；若改用营业收入，"
        "占比会不同，需人工确认题面所需口径。",
        "8. **Q05**：研发投入合计与研发费用数值相同，但概念不同；"
        "需人工确认年报的研发投入口径及资本化情况。",
        "9. **Q06**：利润分配为预案、含税、尚需股东大会审议；"
        "需人工确认审议状态和“每 10 股”口径。",
        "10. **Q01 / Q08**：同比和跨公司排序为规则计算值；"
        "需人工复核两期数值、单位换算和排序依据。",
        "",
        "## 解释边界",
        "",
        "- 本报告是**规则输出测试**，不是问答准确率评测。",
        "- “可回答”只表示在当前检索块中，指标、单位、年份、公司和页码"
        "字段齐全且未被阻断；不表示数值已经人工核对。",
        "- “需核验”表示答案依赖尚未人工确认的跨页表头；"
        "“证据冲突”表示同一报告内出现不同数值且原因未明；"
        "“已阻断”表示关键片段被阻断；"
        "“证据不足”表示检索结果不足以支撑题面。",
        "- 所有 `blocked=true` 或 `reliable_for_deterministic_answer=false` "
        "的数值都只能作为候选记录；报告中 `采信=否`。",
        "",
        f"答案准确率：**{ANSWER_ACCURACY}**。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    make_output_robust()
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    answers = answer_questions(top_k=TOP_K)
    summary = summarize_answers(answers)
    payload = {
        "生成时间": generated,
        "题目来源": "config/questions.csv",
        "方法说明": METHOD_NOTE,
        "检索设置": {
            "top_k": TOP_K,
            "mode": "auto",
            "索引": "src/retrieve.py 当前索引",
        },
        "统计": summary,
        "人工核验": {
            "状态": "未完成",
            "待核验题目": [answer["题目编号"] for answer in answers],
            "说明": "所有答案仍需人工对照原始 PDF 核验；本报告不提供人工确认。",
        },
        "题目结果": answers,
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    OUT_MD.write_text(build_markdown(payload), encoding="utf-8")
    print(f"已写入 {OUT_JSON}")
    print(f"已写入 {OUT_MD}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
