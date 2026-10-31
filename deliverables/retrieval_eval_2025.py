"""10 道固定题的检索测试（真实索引、真实输出，不生成最终答案）。

运行：
    python -I deliverables/retrieval_eval_2025.py

输出：
    deliverables/retrieval_test_2025.json   完整机器可读日志（含 top-k 正文）
    deliverables/retrieval_test_2025.md     可读日志与逐题复核

说明：
- 题目文本直接来自 ``config/questions.csv``，本脚本不改题。
- 证据页是**助手根据 chunk 正文核对**后写下的检索基准，不是既有的
  标准答案；只有 Q01 另有 ``logs/midea_verification.md`` 的原文核验。
- ``严格证据充分`` 只表示“返回页里是否包含回答该题所需的原文证据”，
  不表示答案正确；最终问答准确率一律记为“未评测”。
"""

from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.retrieve import get_retriever  # noqa: E402

QUESTIONS_CSV = ROOT / "config" / "questions.csv"
COMPANIES_CSV = ROOT / "config" / "companies.csv"
MANIFEST = ROOT / "indexes" / "retrieval_v1" / "manifest.json"
OUT_JSON = ROOT / "deliverables" / "retrieval_test_2025.json"
OUT_MD = ROOT / "deliverables" / "retrieval_test_2025.md"
BASELINE_JSON = (
    ROOT / "deliverables" / "baseline" / "retrieval_test_2025_before_context_fix.json"
)
TOP_K = 8

# ---------------------------------------------------------------------------
# 证据页基准
#
# expected：用于统计“页码命中/证据页覆盖率”的相关页。
# required：回答题面所需的最小页集合；缺一页即“严格证据不充分”。
# content_supports_question：页到了，但正文是否真的能支撑题面（例如
#   Q02 返回的是“消费电器”而不是“空调”，就不算支撑）。
# ---------------------------------------------------------------------------
SPECS: dict[str, dict] = {
    "Q01": {
        "expected": {"000333": [9, 135]},
        "required": {"000333": [9, 135]},
        "content_supports_question": True,
        "standard_evidence": "logs/midea_verification.md",
        "numeric_checks": [
            ("000333", 9, ["456,451,731", "43,945,411", "12.11%", "14.03%"]),
            ("000333", 135, ["458,502,407"]),
        ],
        "header_loss": "未发现；p9 主要会计数据表、p135 合并利润表表头完整。",
        "number_risk": "未发现；p9 的营业收入/归母净利润及同比与 logs/midea_verification.md 一致；p135 的营业总收入为 458,502,407 千元。",
        "failure_reason": "无（检索层面）。",
        "suggestion": "可作为首批答案生成样本；生成时须区分 p9 的“营业收入”与 p135 的“营业总收入”。",
    },
    "Q02": {
        "expected": {"000651": [24]},
        "required": {"000651": [24]},
        "content_supports_question": False,
        "deterministic_ok": False,
        "deterministic_note": "题面问“空调业务”，格力 2025 年报分产品仅披露“消费电器”，未单列空调收入/毛利率；不能把消费电器数值当作空调答案。",
        "standard_evidence": None,
        "numeric_checks": [
            ("000651", 24, ["133,055,208,627.13", "78.06%", "35.28%"]),
        ],
        "header_loss": "未发现；p24 的“营业收入构成”表头和数据行在同一页。",
        "number_risk": "题面问“空调业务”，p24 分产品表披露的是“消费电器”（133,055,208,627.13 元，占 78.06%，毛利率 35.28%）；全库未找到“空调+毛利率”同时出现的 chunk，无法用现有检索结果回答空调单列口径。",
        "failure_reason": "题面口径与年报披露口径不一致：格力 2025 年报分产品未单列“空调”，而是“消费电器”。这不是排序失败，是证据本身不存在于该口径。",
        "suggestion": "人工确认年报是否在其他章节单独披露空调收入；若没有，应把题目改为“消费电器”或在题面注明使用合并口径，不能把消费电器数值直接当作空调答案。",
    },
    "Q03": {
        "expected": {"600690": [42]},
        "required": {"600690": [42]},
        "content_supports_question": True,
        "standard_evidence": None,
        "numeric_checks": [
            ("600690", 42, ["146,036,314,128.81", "154,545,365,361.13"]),
        ],
        "header_loss": "未发现；p42 的“主营业务分地区”表头与国内/海外数据行同页。",
        "number_risk": "未发现数值错配；但年报标签是“国内/海外”，题面是“境内/境外”，作答时需要说明口径对应关系。",
        "failure_reason": "无（检索层面）；仅存在措辞差异。",
        "suggestion": "答案生成时引用 p42，并写明“国内/海外”对应题面的“境内/境外”。",
    },
    "Q04": {
        "expected": {"002032": [12, 13]},
        "required": {"002032": [12, 13]},
        "content_supports_question": True,
        "deterministic_ok": False,
        "deterministic_note": "p12/p13 的“营业收入构成”表与 p13 的“10%以上”表对烹饪电器、食物料理电器给出不同金额，差异均为 17,294,550.00 元；同一报告内数据不一致，未人工确认前不能给出单一确定值。",
        "standard_evidence": None,
        "numeric_checks": [
            ("002032", 12, ["8,711,169,417.63", "3,738,068,530.81", "6,965,677,978.67", "30.59%"]),
            ("002032", 13, ["8,728,463,967.63", "3,720,773,980.81", "6,965,677,978.67", "30.59%"]),
        ],
        "header_loss": "p13 从数据行开始，第一张分产品表的列头在 p12；top-2 同时返回 p12 和 p13，可拼接阅读。单独看 p13 有表头丢失风险。",
        "number_risk": "发现同一报告内两表不一致：p12/p13 构成表为烹饪电器 8,711,169,417.63 元、食物料理电器 3,738,068,530.81 元；p13“10%以上”表为 8,728,463,967.63 元、3,720,773,980.81 元；两项差额均为 17,294,550.00 元，合计仍为 22,771,753,460.04 元。助手已对 p12/p13 原始 PDF 渲染页面做视觉检查，确认差异存在；原因未明，待人工核验。详见 manual_evidence_review_2025.md。",
        "failure_reason": "检索页齐全，但同一报告内两张表对烹饪电器/食物料理电器金额不一致；页码命中不等于答案唯一。",
        "suggestion": "答案并列披露两套数值、差额和各自出处，标“待核验”；人工确认前不静默选择其中一个。",
    },
    "Q05": {
        "expected": {"603486": [24]},
        "required": {"603486": [24]},
        "content_supports_question": True,
        "standard_evidence": None,
        "numeric_checks": [
            ("603486", 24, ["980,519,969.24", "5.15"]),
        ],
        "header_loss": "未发现；p24 研发投入情况表完整。",
        "number_risk": "未发现；研发投入合计 980,519,969.24 元、占营业收入 5.15%。同页研发费用金额相同，作答时仍需说明“研发投入”与“研发费用”在本题中数值相同。",
        "failure_reason": "无（检索层面）。",
        "suggestion": "答案生成时引用 p24，并保留“研发投入/研发费用”口径说明。",
    },
    "Q06": {
        "expected": {"002508": [2, 33, 152]},
        "required": {"002508": [2, 33]},
        "content_supports_question": True,
        "standard_evidence": None,
        "numeric_checks": [
            ("002508", 2, ["944,938,916", "每10股派发现金红利5元"]),
            ("002508", 33, ["472,469,458.00"]),
        ],
        "header_loss": "未发现；p2 为正文预案，p33 为利润分配表，p152 为日后事项说明。",
        "number_risk": "未发现；p2/p33/p152 三处均为每 10 股派 5 元（含税），股本基数 944,938,916 股，现金分红 472,469,458.00 元。",
        "failure_reason": "无（检索层面）。",
        "suggestion": "答案生成时优先引用 p2（预案原文），用 p33/p152 交叉核对。",
    },
    "Q07": {
        "expected": {"002959": [18, 19]},
        "required": {"002959": [18, 19]},
        "content_supports_question": True,
        "standard_evidence": None,
        "numeric_checks": [
            ("002959", 19, ["3,756,112,588.91", "71.82%"]),
        ],
        "header_loss": "p19 从数据行开始，列头（项目/金额/占营业收入比重/2025 年/2024 年/同比）在 p18。p18 位于页尾，已作为“表头来源”上下文片段附加；p18、p19 页码分别保留，但关联仍为算法候选、待人工核验。",
        "number_risk": "数值本身可见：线上销售 3,756,112,588.91 元、占 71.82%；表头由 p18 提供后，列语义可核对。上下文片段不是 p19 原文，引用时须同时标注 p18（表头）与 p19（数据）。",
        "failure_reason": "已缓解：主结果仍只命中数据页 p19，但系统自动附加同公司、同 PDF 的 p18 表头上下文，并标注表头来源页与数据页。",
        "suggestion": "答案生成时同时引用 p18（列头）和 p19（数据）；不得把 p18 文本写成 p19 原文。",
    },
    "Q08": {
        "expected": {
            "000333": [9, 135],
            "000651": [7, 88],
            "600690": [12, 122],
        },
        "required": {
            "000333": [9, 135],
            "000651": [7, 88],
            "600690": [12, 122],
        },
        "content_supports_question": True,
        "deterministic_ok": True,
        "deterministic_note": "",
        "direct_ranking": True,
        "standard_evidence": None,
        "numeric_checks": [
            ("000333", 9, ["456,451,731", "43,945,411"]),
            ("000333", 135, ["458,502,407"]),
            ("000651", 7, ["170,447,058,533.57", "29,003,103,411.66"]),
            ("000651", 88, ["171,118,161,275.41"]),
            ("600690", 12, ["302,346,783,918.30", "19,552,798,222.85"]),
        ],
        "header_loss": "p7 有“表头跨行”复核标记；p9 页内表头完整；p88/p135 页内可见合并利润表表头；p122 页首是资产负债表延续，利润表表头在同一页稍后位置。三家公司均同时返回“主要会计数据”表与“利润表”表。",
        "number_risk": "口径风险仍在：第二节主要会计数据表头为“营业收入”，题面问“营业总收入”。美的 p9（营业收入）与 p135（营业总收入）、格力 p7（营业收入）与 p88（营业总收入）、海尔 p12（营业收入）与 p122（营业总收入）分别返回，作答时必须保留两个名称，不能默认二者相等。",
        "failure_reason": "已缓解：海尔 p122（营业总收入）因检索层增加财务报表形态覆盖而进入结果；Q08 的三家公司营业总收入页均已返回。剩余风险是“营业总收入/营业收入”口径差异，不是漏召回。",
        "suggestion": "答案生成时优先引用 p135/p88/p122 的“营业总收入”，并同时说明 p9/p7/p12 的“营业收入”是另一口径；排序统一换算为元或亿元并保留页码引用。",
    },
    "Q09": {
        "expected": {"688169": [54, 45], "603486": [20, 24]},
        "required": {"688169": [54, 45], "603486": [20, 24]},
        "content_supports_question": True,
        "deterministic_ok": False,
        "deterministic_note": "科沃斯 p20（境外收入）带 blocking_anomalies（模式间不一致等），blocked=true；在人工核对前不能把该页数字当作确定性作答依据。",
        "standard_evidence": None,
        "numeric_checks": [
            ("688169", 54, ["10,441,601,948", "48.15"]),
            ("688169", 45, ["1,419,566,173", "7.59"]),
            ("603486", 20, ["8,847,439,964.88"]),
            ("603486", 24, ["980,519,969.24", "5.15"]),
        ],
        "header_loss": "p20 带“模式间不一致、表头跨行”复核标记且 blocked=true；p24/p45/p54 表头完整。",
        "number_risk": "未发现数值缺失；但 p20 属于 blocked，模式间不一致未解决，不能把其中的数字当作已确认事实直接作答。",
        "failure_reason": "无（证据页齐全）；科沃斯 p20 的解析异常会限制确定性作答。",
        "suggestion": "答案生成时保留 p20 的 blocked/需复核标记，并人工核对模式间不一致后再引用境外收入数字。",
    },
    "Q10": {
        "expected": {
            "000333": [50],
            "000651": [24],
            "600690": [42],
            "000921": [30],
            "002668": [23],
            "000521": [18],
            "600336": [15],
            "002032": [13],
            "002242": [13],
            "002705": [16],
            "002959": [19],
            "603486": [20],
            "688169": [54],
            "002508": [14],
        },
        "required": {
            "000333": [50],
            "000651": [24],
            "600690": [42],
            "000921": [30],
            "002668": [23],
            "000521": [18],
            "600336": [15],
            "002032": [13],
            "002242": [13],
            "002705": [16],
            "002959": [19],
            "603486": [20],
            "688169": [54],
            "002508": [14],
        },
        "content_supports_question": True,
        "deterministic_ok": False,
        "deterministic_note": "各公司“境外收入”分母与海外口径不一致（境外/海外/国外/出口/外销，且分母多为营业收入合计而非营业总收入），不具备直接排名的可比性；只能整理候选数据并标为待核验。",
        "direct_ranking": False,
        "standard_evidence": None,
        "numeric_checks": [
            ("000333", 50, ["195,947,693"]),
            ("000651", 24, ["27,374,894,918.04"]),
            ("600690", 42, ["154,545,365,361.13"]),
            ("000921", 30, ["37,924,502,225.32"]),
            ("002668", 23, ["14,404,968,772.76"]),
            ("000521", 18, ["11,340,736,294.77"]),
            ("600336", 15, ["2,036,544,394.92"]),
            ("002032", 13, ["7,437,959,967.44"]),
            ("002242", 13, ["825,127,010.61"]),
            ("002705", 16, ["12,658,042,162.63"]),
            ("002959", 19, ["951,278,116.98"]),
            ("603486", 20, ["8,847,439,964.88"]),
            ("688169", 54, ["10,441,601,948"]),
            ("002508", 14, ["86,523,510.34"]),
        ],
        "header_loss": "算法自动附加了 3 条跨页上下文：小熊 p19←p18、九阳 p13←p12、苏泊尔 p13←p12；海尔 p44←p43 已被拒绝（p43 列头位于页面前部，p44 是贸易/费用/研发内容），不再附加。另有 000521 p19←p18、600336 p15←p14 的表头页已在主结果中，标为“主结果”而未重复附加。保留的 3 条均为算法候选、待人工核验。",
        "number_risk": "各公司分母口径不一致：多数为营业收入合计，题面为营业总收入；海外口径有境外/海外/国外/出口/外销等不同写法。比例可以检索和计算，但不能直接把不同口径混排成最终排名。",
        "failure_reason": "题面所需 p42 已命中；跨页表头不是主要缺口，p44←p43 误挂已拒绝。剩余问题是口径：分母/海外业务定义不一致，且科沃斯 p20 等页带 blocked 标记。",
        "suggestion": "只整理候选数据并逐家标注原文指标名、分母、单位与 blocked 状态；无法确认可比性的项目标为“待核验”，不生成 14 家直接排名。",
    },
}


# ---------------------------------------------------------------------------
# 上下文语义归属的助手文本比对
#
# 这是**助手逐页阅读 chunk 正文后的判断**，不是人工目视核验，也不是自动
# 语义模型评分。`上下文来源错误` 指标只检查元数据一致性，不检查“上一页
# 表头是否真的属于下一页表格”；因此单独记录本清单，避免把 0 误读成
# 语义完全正确。
# ---------------------------------------------------------------------------
CONTEXT_SEMANTIC_AUDIT = {
    "方法": "助手逐页文本比对（非人工目视核验，非语义模型评分）",
    "结论": (
        "本轮共保留 5 条跨页上下文，助手文本比对认为 5 条均与下一页数据表相符；"
        "上一轮 4 条疑似误挂已全部被拒绝且不再附加；旧“合法但冗余”的 "
        "Q07 p155→p156 也因列头不在页尾而拒绝，不构成证据缺口。"
        "保留的 5 条仍只是算法候选，全部待人工核验。"
    ),
    "当前保留误挂数": 0,
    "已拒绝旧疑似误挂数": 4,
    "已拒绝冗余数": 1,
    "保留并待核验": [
        {"题号": "Q05", "证券代码": "603486", "上下文页": 105, "数据页": 106,
         "理由": "p105 页尾（约 80%）为合并利润表列头，p106 续接同一利润表的费用/利润数据；算法已附加，标为待人工核验。"},
        {"题号": "Q07", "证券代码": "002959", "上下文页": 18, "数据页": 19,
         "理由": "p18 页尾（约 98%）为营业收入构成列头，p19 以营业收入合计及分产品/分地区/分销售模式数据开始。"},
        {"题号": "Q10", "证券代码": "002959", "上下文页": 18, "数据页": 19,
         "理由": "同 Q07；同一物理上下文在两道题中分别返回。"},
        {"题号": "Q10", "证券代码": "002242", "上下文页": 12, "数据页": 13,
         "理由": "p12 页尾（约 86%）为营业收入构成列头及合计行，p13 续接分产品/分地区/分销售模式数据。"},
        {"题号": "Q10", "证券代码": "002032", "上下文页": 12, "数据页": 13,
         "理由": "p12 页尾（约 80%）为营业收入构成列头，p13 续接分产品/分地区数据。"},
    ],
    "已拒绝关联": [
        {"题号": "Q02", "证券代码": "000651", "上下文页": 172, "数据页": 173,
         "理由": "p172 最后一个强列头位于页面前部（约 36%），不是页尾；p173 自带“营业收入和营业成本”折行表头，算法已拒绝附加。"},
        {"题号": "Q02", "证券代码": "000651", "上下文页": 217, "数据页": 218,
         "理由": "p217 列头位于页面前部（约 24%）；p218 自带“营业收入和营业成本”折行表头，算法已拒绝附加。"},
        {"题号": "Q07", "证券代码": "002959", "上下文页": 80, "数据页": 81,
         "理由": "p80 为审计报告正文，没有位于页尾的可信列头；p81 为审计程序正文，缺少可衔接的多行数字表，算法已拒绝附加。"},
        {"题号": "Q10", "证券代码": "600690", "上下文页": 43, "数据页": 44,
         "理由": "p43 成本分析表头位于页面前部（约 28%），页尾是客户/供应商文字；p44 是贸易业务/费用/研发内容，算法已拒绝附加。"},
        {"题号": "Q07", "证券代码": "002959", "上下文页": 155, "数据页": 156,
         "理由": "p155 最后一个可识别强列头位于约 11%，不在页尾；p156 以“营业收入/营业成本”行开始，属另一张分解表。旧上下文为冗余，拒绝后不影响 Q07 必需页 p18/p19。"},
    ],
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def load_companies() -> dict[str, dict]:
    rows = list(csv.DictReader(COMPANIES_CSV.open(encoding="utf-8-sig")))
    return {row["证券代码"]: row for row in rows}


def pages_for(results: list[dict], code: str) -> set[int]:
    return {int(item["页码"]) for item in results if item["证券代码"] == code}


def context_pages_for(results: list[dict], code: str) -> set[int]:
    pages: set[int] = set()
    for item in results:
        if item["证券代码"] != code:
            continue
        for fragment in item.get("上下文片段", []):
            if fragment["证券代码"] == code:
                pages.add(int(fragment["页码"]))
    return pages


def all_pages_for(results: list[dict], code: str) -> set[int]:
    return pages_for(results, code) | context_pages_for(results, code)


def page_map(results: list[dict]) -> dict[str, list[int]]:
    mapping: dict[str, set[int]] = {}
    for item in results:
        mapping.setdefault(item["证券代码"], set()).add(int(item["页码"]))
    return {code: sorted(pages) for code, pages in sorted(mapping.items())}


def context_page_map(results: list[dict]) -> dict[str, list[int]]:
    mapping: dict[str, set[int]] = {}
    for item in results:
        for fragment in item.get("上下文片段", []):
            mapping.setdefault(fragment["证券代码"], set()).add(int(fragment["页码"]))
    return {code: sorted(pages) for code, pages in sorted(mapping.items())}


def header_source_counts(evaluation: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for header in evaluation.get("跨页表头清单", []):
        source = header.get("表头来源")
        if source:
            counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))


def reliability_summary(evaluation: dict) -> str:
    contexts = evaluation.get("上下文片段", [])
    if not contexts:
        return "无上下文"
    statuses = sorted(
        {str(item.get("上下文关联状态")) for item in contexts}
    )
    reliable = sorted({item.get("上下文关联可靠") for item in contexts})
    return f"{'、'.join(statuses)}；可靠={reliable}；{len(contexts)} 条"


def blocked_required_pages(spec: dict, results: list[dict]) -> list[dict]:
    by_page: dict[tuple[str, int], dict] = {}
    for item in results:
        by_page[(item["证券代码"], int(item["页码"]))] = item
        for fragment in item.get("上下文片段", []):
            by_page[(fragment["证券代码"], int(fragment["页码"]))] = fragment
    found: list[dict] = []
    for code, pages in spec["required"].items():
        for page in pages:
            item = by_page.get((code, page))
            if item is not None and item.get("blocked"):
                found.append(
                    {
                        "证券代码": code,
                        "页码": int(page),
                        "doc_id": item.get("doc_id"),
                        "blocking_anomalies": item.get("blocking_anomalies", []),
                    }
                )
    return found


def context_errors(results: list[dict]) -> list[str]:
    errors: list[str] = []
    for item in results:
        for fragment in item.get("上下文片段", []):
            if fragment["证券代码"] != item["证券代码"]:
                errors.append(
                    f"上下文跨公司：主结果 {item['doc_id']} 附加了 {fragment['doc_id']}"
                )
            if fragment["PDF文件名"] != item["PDF文件名"]:
                errors.append(
                    f"上下文 PDF 不一致：{fragment['doc_id']} != {item['PDF文件名']}"
                )
            if int(fragment["报告年份"]) != int(item["报告年份"]):
                errors.append(f"上下文年份不一致：{fragment['doc_id']}")
            if int(fragment["页码"]) != int(item["页码"]) - 1:
                errors.append(
                    f"上下文页码不是上一页：{fragment['doc_id']} 对 p{item['页码']}"
                )
            if fragment.get("context_role") != "表头来源":
                errors.append(f"上下文角色异常：{fragment['doc_id']}")
            if int(fragment.get("关联数据页", -1)) != int(item["页码"]):
                errors.append(f"上下文关联数据页异常：{fragment['doc_id']}")
            if int(fragment.get("source", {}).get("page", -1)) != int(fragment["页码"]):
                errors.append(f"上下文 source.page 不一致：{fragment['doc_id']}")
            if fragment.get("上下文关联状态") != "算法判定，待人工核验":
                errors.append(f"上下文关联状态异常：{fragment['doc_id']}")
            if fragment.get("上下文关联可靠") is not False:
                errors.append(f"未核验上下文被标为可靠：{fragment['doc_id']}")
            if fragment.get("reliable_for_deterministic_answer") is not False:
                errors.append(
                    f"未核验上下文可作确定性证据：{fragment['doc_id']}"
                )
            expected_prefix = (
                f"{fragment['证券代码']}_2025_p{int(fragment['页码']):04d}_"
            )
            if not fragment["doc_id"].startswith(expected_prefix):
                errors.append(f"上下文 doc_id 前缀不一致：{fragment['doc_id']}")
    return errors


def header_errors(results: list[dict]) -> list[str]:
    errors: list[str] = []
    primary = {
        (item["证券代码"], int(item["页码"])): item for item in results
    }
    for item in results:
        info = item.get("跨页表头")
        if not info:
            continue
        source = info.get("表头来源")
        header_page = info.get("表头页")
        if source == "上下文":
            pages = {
                int(fragment["页码"]) for fragment in item.get("上下文片段", [])
            }
            if header_page not in pages:
                errors.append(
                    f"{item['doc_id']} 标注表头来自上下文，但没有对应片段"
                )
            if info.get("关联可靠性") != "算法判定，待人工核验":
                errors.append(
                    f"{item['doc_id']} 上下文表头未标注待核验"
                )
        elif source == "主结果":
            if (item["证券代码"], header_page) not in primary:
                errors.append(
                    f"{item['doc_id']} 标注表头在主结果，但主结果没有 p{header_page}"
                )
        elif source == "未扩展（达到上限）":
            if not info.get("表头页doc_id"):
                errors.append(f"{item['doc_id']} 标注达到上限但缺少表头页 doc_id")
        elif source == "未找到":
            if item.get("上下文片段"):
                errors.append(
                    f"{item['doc_id']} 标注未找到表头却仍附加了上下文"
                )
            if not info.get("警告"):
                errors.append(f"{item['doc_id']} 未找到表头但缺少警告")
        elif source in ("页内已有表头", "本页无数据行"):
            if item.get("上下文片段"):
                errors.append(
                    f"{item['doc_id']} 标注{source}却仍附加了上下文"
                )
        else:
            errors.append(f"{item['doc_id']} 未知表头来源：{source}")
    return errors


def source_errors(results: list[dict], companies: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    for item in results:
        code = item["证券代码"]
        row = companies.get(code)
        if row is None:
            errors.append(f"未知证券代码 {code}")
            continue
        expected_pdf = row["文件名"]
        if item["PDF文件名"] != expected_pdf:
            errors.append(f"{code} p{item['页码']} PDF 文件名不一致：{item['PDF文件名']} != {expected_pdf}")
        if item.get("source", {}).get("pdf") != expected_pdf:
            errors.append(f"{code} p{item['页码']} source.pdf 不一致")
        if int(item.get("source", {}).get("page", -1)) != int(item["页码"]):
            errors.append(f"{code} p{item['页码']} source.page 不一致")
        if int(item["页码"]) > int(row["页码数"]):
            errors.append(f"{code} p{item['页码']} 超出 PDF 页数 {row['页码数']}")
        if not item["doc_id"].startswith(f"{code}_2025_p{int(item['页码']):04d}_"):
            errors.append(f"{code} p{item['页码']} doc_id 前缀不一致：{item['doc_id']}")
        if str(item["报告年份"]) != "2025":
            errors.append(f"{code} p{item['页码']} 报告年份异常：{item['报告年份']}")
        pdf_path = ROOT / "data" / "raw" / "reports" / expected_pdf
        if not pdf_path.exists():
            errors.append(f"{code} 缺少原始 PDF：{pdf_path}")
    return errors


def numeric_results(spec: dict, results: list[dict]) -> list[dict]:
    checked = []
    by_page: dict[tuple[str, int], str] = {}
    for item in results:
        by_page[(item["证券代码"], int(item["页码"]))] = normalize(item["正文"])
    for code, page, patterns in spec.get("numeric_checks", []):
        text = by_page.get((code, page), "")
        missing = [p for p in patterns if normalize(p) not in text]
        checked.append(
            {
                "证券代码": code,
                "页码": page,
                "核对字符串": patterns,
                "缺失": missing,
                "通过": not missing and bool(text),
            }
        )
    return checked


def evaluate(question: dict, results: list[dict], spec: dict, companies: dict[str, dict]) -> dict:
    expected = spec["expected"]
    required = spec["required"]
    expected_companies = list(expected)
    returned_companies = {item["证券代码"] for item in results}
    hit_pages: dict[str, list[int]] = {}
    context_pages: dict[str, list[int]] = {}
    missing_pages: dict[str, list[int]] = {}
    missing_with_context: dict[str, list[int]] = {}
    contexts: list[dict] = []
    header_list: list[dict] = []
    for item in results:
        for fragment in item.get("上下文片段", []):
            contexts.append(
                {
                    "doc_id": fragment["doc_id"],
                    "证券代码": fragment["证券代码"],
                    "页码": int(fragment["页码"]),
                    "context_role": fragment.get("context_role"),
                    "关联数据页": fragment.get("关联数据页"),
                    "上下文关联状态": fragment.get("上下文关联状态"),
                    "上下文关联可靠": fragment.get("上下文关联可靠"),
                    "reliable_for_deterministic_answer": fragment.get(
                        "reliable_for_deterministic_answer"
                    ),
                    "源页reliable_for_deterministic_answer": fragment.get(
                        "源页reliable_for_deterministic_answer"
                    ),
                    "上下文关联依据": fragment.get("上下文关联依据"),
                }
            )
        info = item.get("跨页表头")
        if info:
            header_list.append(
                {
                    "证券代码": item["证券代码"],
                    "公司简称": item["公司简称"],
                    "数据页": int(item["页码"]),
                    "表头页": info.get("表头页"),
                    "表头来源": info.get("表头来源"),
                    "表头页doc_id": info.get("表头页doc_id"),
                    "关联可靠性": info.get("关联可靠性"),
                    "候选表头页": info.get("候选表头页"),
                    "候选表头位置": info.get("候选表头位置"),
                    "警告": info.get("警告"),
                }
            )

    for code, pages in expected.items():
        got = sorted(pages_for(results, code) & set(pages))
        ctx = sorted(context_pages_for(results, code) & set(pages))
        hit_pages[code] = got
        context_pages[code] = ctx
        missing_pages[code] = sorted(set(pages) - set(got))
        missing_with_context[code] = sorted(set(pages) - set(got) - set(ctx))

    total_expected = sum(len(pages) for pages in expected.values())
    total_hit = sum(len(pages) for pages in hit_pages.values())
    total_with_context = sum(
        len(set(hit_pages[code]) | set(context_pages[code])) for code in expected
    )
    total_required = sum(len(pages) for pages in required.values())
    total_required_primary = sum(
        1
        for code, pages in required.items()
        for page in pages
        if page in pages_for(results, code)
    )
    total_required_all = sum(
        1
        for code, pages in required.items()
        for page in pages
        if page in all_pages_for(results, code)
    )
    page_hit = all(bool(hit_pages.get(code)) for code in expected_companies)
    page_hit_with_context = all(
        bool(hit_pages.get(code) or context_pages.get(code))
        for code in expected_companies
    )
    required_hits_primary = {
        code: all(page in pages_for(results, code) for page in required.get(code, []))
        for code in expected_companies
    }
    required_hits_all = {
        code: all(page in all_pages_for(results, code) for page in required.get(code, []))
        for code in expected_companies
    }
    strict_support_primary = (
        spec["content_supports_question"]
        and all(required_hits_primary.values())
        and all(hit_pages.get(code) for code in expected_companies)
    )
    strict_support = (
        spec["content_supports_question"]
        and all(required_hits_all.values())
        and page_hit_with_context
    )
    extra_companies = sorted(returned_companies - set(expected_companies))
    checks = numeric_results(spec, results)
    required_blocked = blocked_required_pages(spec, results)
    blocked_results = [
        {
            "证券代码": item["证券代码"],
            "公司简称": item["公司简称"],
            "页码": int(item["页码"]),
            "doc_id": item["doc_id"],
            "blocking_anomalies": item["blocking_anomalies"],
        }
        for item in results
        if item.get("blocked")
    ]
    blocked_fragments = [
        {
            "证券代码": fragment["证券代码"],
            "页码": int(fragment["页码"]),
            "doc_id": fragment["doc_id"],
            "blocking_anomalies": fragment.get("blocking_anomalies", []),
        }
        for item in results
        for fragment in item.get("上下文片段", [])
        if fragment.get("blocked")
    ]
    misuse = [
        {"doc_id": item["doc_id"], "页码": int(item["页码"])}
        for item in results
        if item.get("blocked") and item.get("reliable_for_deterministic_answer")
    ] + [
        {"doc_id": fragment["doc_id"], "页码": int(fragment["页码"])}
        for item in results
        for fragment in item.get("上下文片段", [])
        if fragment.get("blocked") and fragment.get("reliable_for_deterministic_answer")
    ]
    metric_warning_results = [
        {
            "证券代码": item["证券代码"],
            "页码": int(item["页码"]),
            "提示": item.get("口径提示", []),
        }
        for item in results
        if item.get("口径提示")
    ] + [
        {
            "证券代码": fragment["证券代码"],
            "页码": int(fragment["页码"]),
            "提示": fragment.get("口径提示", []),
        }
        for item in results
        for fragment in item.get("上下文片段", [])
        if fragment.get("口径提示")
    ]
    metric_warning_companies = sorted({item["证券代码"] for item in metric_warning_results})
    deterministic_ok = bool(
        strict_support
        and spec.get("deterministic_ok", True)
        and not required_blocked
    )

    return {
        "预期公司": [companies[code]["公司简称"] for code in expected_companies],
        "预期证据页": expected,
        "必需页": required,
        "命中公司": [
            companies[code]["公司简称"] for code in expected_companies if code in returned_companies
        ],
        "命中证据页": hit_pages,
        "上下文证据页": context_pages,
        "含上下文命中证据页": {
            code: sorted(set(hit_pages[code]) | set(context_pages[code]))
            for code in expected_companies
        },
        "缺失证据页": missing_pages,
        "缺失证据页（含上下文）": missing_with_context,
        "上下文片段": contexts,
        "上下文片段数": len(contexts),
        "跨页表头清单": header_list,
        "公司覆盖率": round(len(returned_companies & set(expected_companies)) / len(expected_companies), 4),
        "证据页覆盖率": round(total_hit / total_expected, 4) if total_expected else 0.0,
        "含上下文证据页覆盖率": (
            round(total_with_context / total_expected, 4) if total_expected else 0.0
        ),
        "必需页覆盖率": round(total_required_all / total_required, 4) if total_required else 0.0,
        "必需页覆盖率（仅主结果）": (
            round(total_required_primary / total_required, 4) if total_required else 0.0
        ),
        "页码命中": page_hit,
        "含上下文页码命中": page_hit_with_context,
        "严格证据充分": strict_support,
        "严格证据充分（仅主结果）": strict_support_primary,
        "内容支撑题面": spec["content_supports_question"],
        "确定性作答可用": deterministic_ok,
        "确定性作答不可用原因": "" if deterministic_ok else spec.get("deterministic_note", ""),
        "可直接排名": spec.get("direct_ranking"),
        "跨公司混淆": bool(extra_companies),
        "跨公司额外结果": [
            companies[code]["公司简称"] for code in extra_companies if code in companies
        ],
        "表头丢失风险": spec["header_loss"],
        "数字错配风险": spec["number_risk"],
        "来源错误": source_errors(results, companies),
        "上下文来源错误": context_errors(results),
        "跨页表头标注错误": header_errors(results),
        "来源错误（含上下文）": source_errors(results, companies)
        + context_errors(results)
        + header_errors(results),
        "口径提示结果数": len(metric_warning_results),
        "口径提示公司": [
            companies[code]["公司简称"]
            for code in metric_warning_companies
            if code in companies
        ],
        "口径提示明细": metric_warning_results,
        "阻断结果": blocked_results,
        "阻断上下文": blocked_fragments,
        "必需页被阻断": required_blocked,
        "异常数据误用": misuse,
        "数字存在性核对": checks,
        "数字存在性全部通过": all(item["通过"] for item in checks),
        "失败原因": spec["failure_reason"],
        "建议": spec["suggestion"],
        "标准答案或既有核验": spec["standard_evidence"],
        "答案准确率": (
            "未评测（已有 logs/midea_verification.md 原文核验，但本轮未生成回答）"
            if spec["standard_evidence"]
            else "未评测（无标准答案，且本轮未生成回答）"
        ),
    }


def excerpt(text: str, limit: int = 420) -> str:
    return re.sub(r"\s+", " ", text or "")[:limit]


def excerpt_for_result(result: dict, spec: dict) -> str:
    """优先截取该页已核对数字附近的文本，便于人工复核。"""
    text = re.sub(r"\s+", " ", result.get("正文", ""))
    patterns: list[str] = []
    for code, page, items in spec.get("numeric_checks", []):
        if code == result["证券代码"] and page == int(result["页码"]):
            patterns.extend(items)
    for pattern in patterns:
        index = text.find(pattern)
        if index >= 0:
            return text[max(0, index - 180) : index + 300]
    return text[:420]


def md_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def build_markdown(payload: dict) -> str:
    lines = [
        "# 检索原型真实测试日志（2025 年年报，10 道固定题）",
        "",
        f"- 生成时间：{payload['生成时间']}",
        f"- 基线：`{payload['统计']['基线文件']}`"
        "（本轮跨页修复前保存，用于逐题对比）",
        "- 索引：`indexes/retrieval_v1`（真实构建，未使用外部模型或外部服务）",
        "- 检索：BM25 + 字符 n-gram 哈希 TF-IDF 向量 + RRF + 结构意图覆盖",
        "- 上下文：仅对缺少本页列头的表格命中，要求上一页列头位于页面后 30%、"
        "同公司/同报告年份/同 PDF、下一页至少两条含数字的表格数据行；"
        "不满足时不附加并保留警告；每个片段保留自己的页码，最多 6 条，"
        "不计入主结果排名，且一律标为“算法判定，待人工核验”",
        "- 口径提示：题面指标未在片段出现、但出现相关指标时给出警告，不自动视为等价",
        "- 题目：直接读取 `config/questions.csv`，未改题",
        "- 说明：证据页由助手根据 chunk 正文核对，**不是人工目视核验**；"
        "Q01 另有 `logs/midea_verification.md` 的原文核验，Q02–Q10 没有标准答案。",
        "- 最终问答准确率：**未评测**。检索命中不等于答案正确；本日志不生成最终回答。",
        "",
        "## 汇总",
        "",
        "| 题目 | 类型 | 返回 | 页码命中 | 严格证据（含上下文） | 严格证据（仅主结果） | 上下文 | 确定性作答 | 可直接排名 | 跨公司混淆 | 答案准确率 |",
        "|---|---|---:|---|---|---|---:|---|---|---|---|",
    ]
    for item in payload["题目结果"]:
        ev = item["评估"]
        rankable = ev.get("可直接排名")
        rankable_text = "不适用" if rankable is None else ("是" if rankable else "否")
        lines.append(
            f"| {item['题目编号']} | {md_cell(item['题目类型'])} | {item['返回条数']} | "
            f"{'是' if ev['页码命中'] else '否'} | {'是' if ev['严格证据充分'] else '否'} | "
            f"{'是' if ev['严格证据充分（仅主结果）'] else '否'} | {ev['上下文片段数']} | "
            f"{'是' if ev['确定性作答可用'] else '否'} | {rankable_text} | "
            f"{'是' if ev['跨公司混淆'] else '否'} | 未评测 |"
        )
    lines.extend(
        [
            "",
            f"- 页码命中：{payload['统计']['页码命中题数']}/10"
            f"（含上下文 {payload['统计']['含上下文页码命中题数']}/10）。",
            f"- 严格证据充分：{payload['统计']['严格证据充分题数']}/10"
            f"（仅主结果 {payload['统计']['严格证据充分（仅主结果）题数']}/10）。",
            f"- 确定性作答可用：{payload['统计']['确定性作答可用题数']}/10；"
            "该列只表示证据与口径是否足以支持确定性回答，不是答案正确率。",
            f"- 上下文片段：{payload['统计']['上下文片段总数']} 条，涉及 "
            f"{payload['统计']['有上下文片段的题数']} 道题。",
            f"- 跨公司混淆：{payload['统计']['跨公司混淆题数']} 道。",
            f"- 来源错误：{payload['统计']['来源错误题数']} 道"
            f"（仅主结果 {payload['统计']['来源错误（仅主结果）题数']} 道；含上下文/表头标注检查）。"
            "该指标只检查元数据一致性，**不判断上下文表头是否语义上属于下一页表格**。",
            f"- 上下文语义（助手文本比对，非人工目视）：当前保留 "
            f"{payload['统计']['上下文片段总数']} 条中，文本相符 "
            f"{payload['统计']['上下文语义文本相符数']} 条、误挂 "
            f"{payload['统计']['上下文语义误挂数']} 条；上一轮 "
            f"{payload['统计']['已拒绝旧疑似误挂数']} 条疑似误挂已全部拒绝。"
            "详见下方“上下文语义核验”。",
            f"- 口径提示：{payload['统计']['口径提示结果总数']} 条，涉及 "
            f"{payload['统计']['口径提示题数']} 道题。",
            f"- 阻断结果：{payload['统计']['阻断结果总数']} 条；"
            f"必需页被阻断 {payload['统计']['必需页被阻断题数']} 道；"
            f"异常数据被标为可靠 {payload['统计']['异常数据误用数']} 条。",
            "- 未通过严格证据的题目见下方“严格证据未通过”与 `retrieval_failure_cases_2025.md`。",
            "- 所有题目的答案准确率均为未评测；没有用检索命中冒充答案正确。",
            "",
            "## 上下文语义核验",
            "",
            f"- 方法：{payload['上下文语义核验']['方法']}。",
            f"- 结论：{payload['上下文语义核验']['结论']}",
            "- 下表只记录助手逐页阅读 chunk 正文后的判断，**不是人工目视核验**；",
            "  最终以人工核对 PDF 为准。",
            "",
            "| 判定 | 题号 | 公司代码 | 上下文页 | 数据页 | 理由 |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for label, key in [
        ("保留并待核验", "保留并待核验"),
        ("已拒绝", "已拒绝关联"),
    ]:
        for row in payload["上下文语义核验"][key]:
            lines.append(
                f"| {label} | {row['题号']} | {row['证券代码']} | p{row['上下文页']} | "
                f"p{row['数据页']} | {md_cell(row['理由'])} |"
            )
    lines.extend(
        [
            "",
            "## 修改前后对比",
            "",
            "### 结果页与上下文页",
            "",
            "| 题目 | 修改前返回 | 修改后返回 | 修改前主结果页 | 修改后主结果页 | 修改前上下文页 | 修改后上下文页 | 新增/移除上下文 |",
            "|---|---:|---:|---|---|---|---|---|",
        ]
    )
    for item in payload["题目结果"]:
        before = item.get("修改前")
        change = item.get("修改后变化")
        if not before or not change:
            continue

        def render_pages(mapping: dict[str, list[int]]) -> str:
            return "；".join(
                f"{code}: {','.join(str(p) for p in pages)}"
                for code, pages in mapping.items()
            ) or "无"

        after_pages = page_map(item["结果"])
        after_contexts = context_page_map(item["结果"])
        context_change = (
            f"+{'、'.join(change['新增上下文页码']) or '无'}；"
            f"-{'、'.join(change['移除上下文页码']) or '无'}"
        )
        lines.append(
            f"| {item['题目编号']} | {before['返回条数']} | {item['返回条数']} | "
            f"{md_cell(render_pages(before['主结果页码']))} | "
            f"{md_cell(render_pages(after_pages))} | "
            f"{md_cell(render_pages(before['上下文页码']))} | "
            f"{md_cell(render_pages(after_contexts))} | "
            f"{md_cell(context_change)} |"
        )

    lines.extend(
        [
            "",
            "### 表头来源、关联可靠性与作答状态",
            "",
            "| 题目 | 修改前表头来源 | 修改后表头来源 | 修改前关联可靠性 | 修改后关联可靠性 | 严格证据 | 确定性作答 | 上下文片段数 |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for item in payload["题目结果"]:
        before = item.get("修改前")
        change = item.get("修改后变化")
        if not before or not change:
            continue
        lines.append(
            f"| {item['题目编号']} | "
            f"{md_cell(json.dumps(before['表头来源计数'], ensure_ascii=False))} | "
            f"{md_cell(json.dumps(change['表头来源计数']['修改后'], ensure_ascii=False))} | "
            f"{md_cell(before['关联可靠性'])} | "
            f"{md_cell(change['关联可靠性'])} | "
            f"{md_cell(change['严格证据充分'])} | "
            f"{md_cell(change['确定性作答'])} | "
            f"{md_cell(change['上下文片段数'])} |"
        )
    lines.extend(
        [
            "",
            "## 严格证据未通过",
            "",
            "| 题目 | 内容支撑题面 | 缺失（仅主结果） | 缺失（含上下文） | 原因 |",
            "|---|---|---|---|---|",
        ]
    )
    for item in payload["题目结果"]:
        ev = item["评估"]
        if ev["严格证据充分"]:
            continue
        lines.append(
            f"| {item['题目编号']} | {'是' if ev['内容支撑题面'] else '否'} | "
            f"{json.dumps(ev['缺失证据页'], ensure_ascii=False)} | "
            f"{json.dumps(ev['缺失证据页（含上下文）'], ensure_ascii=False)} | "
            f"{md_cell(ev['失败原因'])} |"
        )
    lines.append("")

    for item in payload["题目结果"]:
        ev = item["评估"]
        before = item.get("修改前")
        change = item.get("修改后变化")
        lines.extend(
            [
                f"## {item['题目编号']} {item['题目类型']}",
                "",
                f"**题目**：{item['题目']}",
                "",
                f"- 预期公司/范围：{item['涉及公司']}",
                f"- 预期出处：{item['预期出处']}",
                f"- 检索范围：{item['检索范围']['类型']}；公司：{'、'.join(item['检索范围']['公司'])}；每公司上限：{item['检索范围']['每公司上限']}",
                f"- 检索意图：{', '.join(item['检索意图']) if item['检索意图'] else '无'}",
                f"- 返回条数：{item['返回条数']}（请求 top_k={item['请求top_k']}；跨公司覆盖模式可能超过 top_k）",
                f"- 预期证据页：{json.dumps(ev['预期证据页'], ensure_ascii=False)}",
                f"- 命中证据页（主结果）：{json.dumps(ev['命中证据页'], ensure_ascii=False)}",
                f"- 上下文证据页：{json.dumps(ev['上下文证据页'], ensure_ascii=False)}",
                f"- 缺失证据页（仅主结果）：{json.dumps(ev['缺失证据页'], ensure_ascii=False)}",
                f"- 缺失证据页（含上下文）：{json.dumps(ev['缺失证据页（含上下文）'], ensure_ascii=False)}",
                f"- 页码命中：{'是' if ev['页码命中'] else '否'}；"
                f"严格证据充分（含上下文）：{'是' if ev['严格证据充分'] else '否'}；"
                f"严格证据充分（仅主结果）：{'是' if ev['严格证据充分（仅主结果）'] else '否'}；"
                f"证据页覆盖率：{ev['证据页覆盖率']:.2%}；"
                f"含上下文覆盖率：{ev['含上下文证据页覆盖率']:.2%}",
                f"- 确定性作答可用：{'是' if ev['确定性作答可用'] else '否'}"
                f"{'；原因：' + ev['确定性作答不可用原因'] if ev['确定性作答不可用原因'] else ''}",
                f"- 可直接排名：{'不适用' if ev['可直接排名'] is None else ('是' if ev['可直接排名'] else '否')}",
                f"- 跨公司混淆：{'是' if ev['跨公司混淆'] else '否'}；额外公司：{'、'.join(ev['跨公司额外结果']) or '无'}",
                f"- 来源错误（含上下文/表头标注）：{'；'.join(ev['来源错误（含上下文）']) if ev['来源错误（含上下文）'] else '未发现'}",
                f"- 数字存在性核对（仅已返回页）：{'全部通过' if ev['数字存在性全部通过'] else '存在缺失'}",
                f"- 表头丢失风险：{ev['表头丢失风险']}",
                f"- 数字错配风险：{ev['数字错配风险']}",
                f"- 失败原因：{ev['失败原因']}",
                f"- 建议：{ev['建议']}",
                f"- 答案准确率：{ev['答案准确率']}",
            ]
        )
        if before and change:
            lines.append(
                f"- 修改前：返回 {before['返回条数']} 条；"
                f"严格证据充分 {'是' if before['严格证据充分'] else '否'}；"
                f"确定性作答 {'是' if before['确定性作答可用'] else '否'}；"
                f"证据页覆盖率 {before['证据页覆盖率']:.2%}；"
                f"主结果页 {json.dumps(before['主结果页码'], ensure_ascii=False)}；"
                f"上下文页 {json.dumps(before['上下文页码'], ensure_ascii=False)}；"
                f"表头来源 {json.dumps(before['表头来源计数'], ensure_ascii=False)}；"
                f"关联可靠性 {before['关联可靠性']}"
            )
            lines.append(
                f"- 修改后变化：新增主结果页 {'、'.join(change['新增主结果页码']) or '无'}；"
                f"移除主结果页 {'、'.join(change['移除主结果页码']) or '无'}；"
                f"新增上下文页 {'、'.join(change['新增上下文页码']) or '无'}；"
                f"移除上下文页 {'、'.join(change['移除上下文页码']) or '无'}；"
                f"上下文片段数 {change['上下文片段数']}；"
                f"表头来源 {json.dumps(change['表头来源计数'], ensure_ascii=False)}；"
                f"关联可靠性 {change['关联可靠性']}；"
                f"严格证据 {change['严格证据充分']}；"
                f"确定性作答 {change['确定性作答']}；"
                f"覆盖率 {change['证据页覆盖率']}"
            )
        contexts = ev["上下文片段"]
        if contexts:
            lines.append(
                "- 上下文片段："
                + "；".join(
                    f"{f['证券代码']} p{f['页码']}"
                    f"（{f['context_role']}，关联数据页 p{f['关联数据页']}；"
                    f"{f.get('上下文关联状态')}，可靠={f.get('上下文关联可靠')}）"
                    for f in contexts
                )
            )
        else:
            lines.append("- 上下文片段：无")
        headers = ev["跨页表头清单"]
        if headers:
            rendered = []
            for h in headers:
                if h["表头页"] is None:
                    text = (
                        f"{h['证券代码']} 数据页 p{h['数据页']}"
                        f"（{h['表头来源']}"
                    )
                    if h.get("候选表头页") is not None:
                        text += f"；候选表头页 p{h['候选表头页']}"
                    text += f"；关联可靠性：{h.get('关联可靠性')}）"
                    rendered.append(text)
                else:
                    rendered.append(
                        f"{h['证券代码']} 数据页 p{h['数据页']} ← 表头页 p{h['表头页']}"
                        f"（{h['表头来源']}；关联可靠性：{h.get('关联可靠性')}）"
                    )
            lines.append("- 跨页表头：" + "；".join(rendered))
        else:
            lines.append("- 跨页表头：无")
        blocked = ev["阻断结果"] + ev["阻断上下文"]
        if blocked:
            lines.append(
                "- 阻断结果："
                + "；".join(
                    f"{b['证券代码']} p{b['页码']} "
                    + (
                        "、".join(
                            str(anomaly.get("kind", "blocked"))
                            for anomaly in b["blocking_anomalies"]
                        )
                        or "blocked"
                    )
                    for b in blocked
                )
            )
        else:
            lines.append("- 阻断结果：无")
        if ev["必需页被阻断"]:
            lines.append(
                "- 必需页被阻断："
                + "；".join(
                    f"{b['证券代码']} p{b['页码']}（{b['doc_id']}）"
                    for b in ev["必需页被阻断"]
                )
            )
        warnings = ev["口径提示明细"]
        if warnings:
            samples = []
            for warning in warnings[:3]:
                samples.append(
                    f"{warning['证券代码']} p{warning['页码']}："
                    + " ".join(warning["提示"])
                )
            lines.append(f"- 口径提示：{len(warnings)} 条；" + "；".join(samples))
        else:
            lines.append("- 口径提示：无")
        lines.extend(
            [
                "",
                "| 排名 | 公司 | 页码 | 类型 | 章节 | RRF | BM25/向量/意图 | blocked | 上下文/表头 | 警告 | 引用 |",
                "|---:|---|---:|---|---|---:|---|---|---|---|---|",
            ]
        )
        for result in item["结果"][:10]:
            scores = result["scores"]
            score_text = (
                f"{scores['bm25_rank']}/{scores['vector_rank']}/{scores['intent_rank']}"
                f"（I={scores['intent']}）"
            )
            warnings_text = "；".join(result["warnings"][:3]) or "无"
            context_text = "、".join(
                f"p{f['页码']}" for f in result.get("上下文片段", [])
            ) or "无"
            info = result.get("跨页表头")
            if info:
                context_text += f"；表头:{info.get('表头来源')}"
                if info.get("表头页") is not None:
                    context_text += f" p{info['表头页']}"
            lines.append(
                f"| {result['排名']} | {result['公司简称']}（{result['证券代码']}） | "
                f"{result['页码']} | {result['类型']} | {md_cell(result['章节'])} | "
                f"{scores['rrf']:.5f} | {score_text} | "
                f"{'是' if result['blocked'] else '否'} | {md_cell(context_text)} | "
                f"{md_cell(warnings_text)} | {md_cell(result['source']['citation'])} |"
            )
        lines.append("")
        lines.append("**Top 结果摘录**：")
        lines.append("")
        for result in item["结果"][:3]:
            lines.append(
                f"- p{result['页码']}：{md_cell(excerpt_for_result(result, SPECS[item['题目编号']]))}"
            )
        context_samples = [
            fragment
            for result in item["结果"]
            for fragment in result.get("上下文片段", [])
        ][:3]
        if context_samples:
            lines.append("")
            lines.append("**上下文片段摘录**：")
            lines.append("")
            for fragment in context_samples:
                lines.append(
                    f"- {fragment['证券代码']} p{fragment['页码']}"
                    f"（{fragment['context_role']}，关联数据页 p{fragment['关联数据页']}）："
                    f"{md_cell(excerpt(fragment['正文']))}"
                )
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    if not MANIFEST.exists():
        print("缺少 indexes/retrieval_v1/manifest.json；请先运行 python -m src.index", file=sys.stderr)
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    companies = load_companies()
    questions = list(csv.DictReader(QUESTIONS_CSV.open(encoding="utf-8-sig")))
    retriever = get_retriever()
    baseline: dict[str, dict] = {}
    if BASELINE_JSON.exists():
        baseline_payload = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))
        baseline = {item["题目编号"]: item for item in baseline_payload["题目结果"]}

    payload = {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "索引清单": {
            "路径": "indexes/retrieval_v1",
            "构建时间": manifest.get("构建时间"),
            "状态": manifest.get("状态"),
            "检索后端": manifest.get("检索后端"),
            "统计": manifest.get("统计"),
        },
        "方法说明": {
            "题目来源": "config/questions.csv（未改题）",
            "top_k": TOP_K,
            "跨公司模式": "识别到多家公司时按 per_company 覆盖，返回条数可能超过 top_k",
            "证据页基准": "助手根据 chunk 正文核对，不是既有标准答案；Q01 另有 logs/midea_verification.md",
            "人工目视": "未进行",
            "答案生成": "未进行",
            "答案准确率": "未评测",
        },
        "题目结果": [],
    }

    for question in questions:
        qid = question["题目编号"]
        if qid not in SPECS:
            raise KeyError(f"缺少 {qid} 的证据页基准")
        results = retriever.search(question["题目"], top_k=TOP_K)
        item = dict(question)
        item["请求top_k"] = TOP_K
        item["返回条数"] = len(results)
        item["检索范围"] = results[0]["检索范围"] if results else None
        item["检索意图"] = results[0]["检索意图"] if results else []
        item["结果"] = results
        item["评估"] = evaluate(question, results, SPECS[qid], companies)
        before = baseline.get(qid)
        if before is not None:
            before_pages = page_map(before["结果"])
            after_pages = page_map(results)
            before_flat = {
                f"{code} p{page}"
                for code, pages in before_pages.items()
                for page in pages
            }
            after_flat = {
                f"{code} p{page}"
                for code, pages in after_pages.items()
                for page in pages
            }
            before_contexts = context_page_map(before["结果"])
            after_contexts = context_page_map(results)
            before_ctx_flat = {
                f"{code} p{page}"
                for code, pages in before_contexts.items()
                for page in pages
            }
            after_ctx_flat = {
                f"{code} p{page}"
                for code, pages in after_contexts.items()
                for page in pages
            }
            before_context_count = before["评估"]["上下文片段数"]
            after_context_count = item["评估"]["上下文片段数"]
            item["修改前"] = {
                "返回条数": before["返回条数"],
                "主结果页码": before_pages,
                "上下文页码": before_contexts,
                "上下文片段数": before_context_count,
                "表头来源计数": header_source_counts(before["评估"]),
                "关联可靠性": (
                    "未记录（旧实现无该字段）"
                    if before_context_count
                    else "无上下文"
                ),
                "证据页覆盖率": before["评估"]["证据页覆盖率"],
                "严格证据充分": before["评估"]["严格证据充分"],
                "确定性作答可用": before["评估"]["确定性作答可用"],
                "来源错误": before["评估"]["来源错误"],
            }
            item["修改后变化"] = {
                "新增主结果页码": sorted(after_flat - before_flat),
                "移除主结果页码": sorted(before_flat - after_flat),
                "新增上下文页码": sorted(after_ctx_flat - before_ctx_flat),
                "移除上下文页码": sorted(before_ctx_flat - after_ctx_flat),
                "上下文片段数": f"{before_context_count} → {after_context_count}",
                "表头来源计数": {
                    "修改前": header_source_counts(before["评估"]),
                    "修改后": header_source_counts(item["评估"]),
                },
                "关联可靠性": (
                    "无上下文 → 无上下文"
                    if before_context_count == 0 and after_context_count == 0
                    else (
                        f"未记录（旧实现） → 无上下文"
                        f"（{before_context_count} 条已移除）"
                        if after_context_count == 0
                        else "未记录（旧实现） → "
                        + reliability_summary(item["评估"])
                    )
                ),
                "严格证据充分": (
                    f"{'是' if before['评估']['严格证据充分'] else '否'}"
                    f" → {'是' if item['评估']['严格证据充分'] else '否'}"
                ),
                "确定性作答": (
                    f"{'是' if before['评估']['确定性作答可用'] else '否'}"
                    f" → {'是' if item['评估']['确定性作答可用'] else '否'}"
                ),
                "证据页覆盖率": (
                    f"{before['评估']['证据页覆盖率']:.2%}"
                    f" → {item['评估']['证据页覆盖率']:.2%}"
                ),
            }
        payload["题目结果"].append(item)

    payload["上下文语义核验"] = CONTEXT_SEMANTIC_AUDIT
    payload["统计"] = {
        "题目数": len(payload["题目结果"]),
        "页码命中题数": sum(1 for item in payload["题目结果"] if item["评估"]["页码命中"]),
        "含上下文页码命中题数": sum(
            1 for item in payload["题目结果"] if item["评估"]["含上下文页码命中"]
        ),
        "严格证据充分题数": sum(1 for item in payload["题目结果"] if item["评估"]["严格证据充分"]),
        "严格证据充分（仅主结果）题数": sum(
            1 for item in payload["题目结果"] if item["评估"]["严格证据充分（仅主结果）"]
        ),
        "确定性作答可用题数": sum(
            1 for item in payload["题目结果"] if item["评估"]["确定性作答可用"]
        ),
        "跨公司混淆题数": sum(1 for item in payload["题目结果"] if item["评估"]["跨公司混淆"]),
        "来源错误题数": sum(
            1 for item in payload["题目结果"] if item["评估"]["来源错误（含上下文）"]
        ),
        "来源错误（仅主结果）题数": sum(
            1 for item in payload["题目结果"] if item["评估"]["来源错误"]
        ),
        "上下文片段总数": sum(item["评估"]["上下文片段数"] for item in payload["题目结果"]),
        "有上下文片段的题数": sum(
            1 for item in payload["题目结果"] if item["评估"]["上下文片段数"]
        ),
        "上下文语义误挂数": payload["上下文语义核验"]["当前保留误挂数"],
        "上下文语义文本相符数": len(
            payload["上下文语义核验"]["保留并待核验"]
        ),
        "已拒绝旧疑似误挂数": payload["上下文语义核验"][
            "已拒绝旧疑似误挂数"
        ],
        "上下文关联可靠数": sum(
            1
            for item in payload["题目结果"]
            for fragment in item["评估"]["上下文片段"]
            if fragment.get("上下文关联可靠") is True
        ),
        "上下文关联未确认数": sum(
            1
            for item in payload["题目结果"]
            for fragment in item["评估"]["上下文片段"]
            if fragment.get("上下文关联可靠") is False
        ),
        "口径提示题数": sum(
            1 for item in payload["题目结果"] if item["评估"]["口径提示结果数"]
        ),
        "口径提示结果总数": sum(
            item["评估"]["口径提示结果数"] for item in payload["题目结果"]
        ),
        "阻断结果总数": sum(
            len(item["评估"]["阻断结果"]) for item in payload["题目结果"]
        ),
        "必需页被阻断题数": sum(
            1 for item in payload["题目结果"] if item["评估"]["必需页被阻断"]
        ),
        "异常数据误用数": sum(
            len(item["评估"]["异常数据误用"]) for item in payload["题目结果"]
        ),
        "基线文件": (
            BASELINE_JSON.relative_to(ROOT).as_posix()
            if BASELINE_JSON.exists()
            else "未找到"
        ),
        "答案准确率": "未评测",
        "说明": "检索命中率与最终答案准确率分开；本轮不生成回答，不填写准确率。上下文来源错误=0 仅指元数据一致，不代表语义关联正确；语义归属见“上下文语义核验”，为助手文本比对，待人工核验。所有跨页上下文均固定 reliable_for_deterministic_answer=false。",
    }

    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    OUT_MD.write_text(build_markdown(payload), encoding="utf-8", newline="\n")

    print(f"已写入 {OUT_JSON.relative_to(ROOT)}")
    print(f"已写入 {OUT_MD.relative_to(ROOT)}")
    print(
        f"页码命中 {payload['统计']['页码命中题数']}/10；"
        f"严格证据充分 {payload['统计']['严格证据充分题数']}/10"
        f"（仅主结果 {payload['统计']['严格证据充分（仅主结果）题数']}/10）；"
        f"确定性作答可用 {payload['统计']['确定性作答可用题数']}/10；"
        f"上下文片段 {payload['统计']['上下文片段总数']} 条"
        f"（语义误挂 {payload['统计']['上下文语义误挂数']} 条；"
        f"标为可靠 {payload['统计']['上下文关联可靠数']} 条）；"
        f"跨公司混淆 {payload['统计']['跨公司混淆题数']}；"
        f"来源错误题数 {payload['统计']['来源错误题数']}；"
        f"必需页被阻断 {payload['统计']['必需页被阻断题数']} 题；答案准确率 未评测"
    )
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
