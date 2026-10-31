"""混合检索：BM25 关键词召回 + 字符 n-gram TF-IDF 向量召回 + RRF 融合。

重要说明
--------
向量通道是**哈希字符 n-gram TF-IDF 向量**，不是 BGE / sentence-transformers
等神经语义嵌入。本机没有离线中文 embedding 模型，本轮不下载模型、不调用
外部接口；因此这里明确不声称具备神经语义检索能力。

检索结果保留原始页码、PDF 文件名、chunk 类型、章节、完整正文和解析异常；
不会生成原文中不存在的引用。带 ``blocked=true`` 的块仍可被召回，但会标记
``reliable_for_deterministic_answer=false``，不能当作已确认事实直接作答。

跨公司检索
----------
默认按题面识别公司。识别到多家公司时，先对全部候选做全局打分，再按公司
各取 ``per_company`` 条，避免结果被单家公司垄断；识别到单家公司时按公司
过滤。也可以用 ``company_codes`` 显式过滤，或用 ``mode="all"`` 跨全部 14 家。
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

from src.config import INDEX_DIR
from src.index import (
    DEFAULT_TOP_K,
    PACK_BASE,
    RRF_K,
    RetrievalIndex,
    hash_token,
    load_companies,
    load_index,
    normalize_text,
    query_token_counts,
)

COMPANY_ALIASES = {
    "000333": ("美的",),
    "000651": ("格力",),
    "600690": ("海尔",),
    "000921": ("海信",),
    "002668": ("tcl",),
    "000521": ("长虹", "美菱"),
    "600336": ("澳柯玛",),
    "002032": ("苏泊尔",),
    "002242": ("九阳",),
    "002705": ("新宝",),
    "002959": ("小熊",),
    "603486": ("科沃斯",),
    "688169": ("石头",),
    "002508": ("老板",),
}

ALL_PATTERN = re.compile(r"全部\s*14\s*家|所有\s*14\s*家|14\s*家公司|全部公司|所有公司")

#: 跨页表头上下文：只对“本页从数据行开始、上一页页尾确有表头且下一页
#: 有表格数据行”的 table chunk 追加，最多 6 条，避免无条件扩大上下文。
MAX_CONTEXT_FRAGMENTS = 6
HEADER_SCAN_LINES = 40

#: 上一页列头必须落在页面后段，才允许作为下一页的表头来源。
#: 该阈值只约束位置，不识别公司/页码/题目；真实页面的正确关联（如
#: 小熊 p18→p19、九阳 p12→p13、苏泊尔 p12→p13、科沃斯 p105→p106）
#: 均在后 30% 内，而误挂案例的列头位于页面前半段。
CROSS_PAGE_HEADER_TAIL_RATIO = 0.70

#: 下一页至少要有这样一条“非正文、含 2 个以上数字”的数据行，才认为
#: 表头与数据表在结构上可衔接。
CROSS_PAGE_MIN_DATA_NUMBERS = 2

#: 仅一条含数字的行不足以证明是“续表”：审计说明、单行附注也可能含两个
#: 数字。要求至少两条数据行，证据不足时宁可不附加。
CROSS_PAGE_MIN_DATA_ROWS = 2

#: 列头行通常同时出现多个字段名；单个“营业收入”也可能只是数据行标签，
#: 因此要求至少 3 个不同标记，避免把数据行误判成表头。
HEADER_TOKENS = (
    "金额",
    "占营业收入",
    "占营业总收入",
    "占比",
    "比重",
    "同比",
    "增减",
    "2025 年",
    "2024 年",
    "2023 年",
    "2025 年度",
    "2024 年度",
    "2023 年度",
    "2025年度",
    "2024年度",
    "2023年度",
    "本年比上年",
    "单位：",
    "单位:",
    "项目",
    "科目",
    "附注",
    "营业收入",
    "营业成本",
    "毛利率",
    "本期数",
    "上年同期",
    "期末余额",
    "期初余额",
    "销售量",
    "生产量",
    "库存量",
)

#: 题面指标与年报中相近但不必然等价的写法。只用于提示，不做自动换算。
METRIC_RELATIONS = {
    "营业总收入": ("营业收入",),
    "营业收入": (),
    "空调": ("消费电器",),
    "研发投入": ("研发费用",),
    "研发费用": ("研发投入",),
    "境外收入": ("海外收入", "国外收入", "境外销售", "海外销售", "出口", "外销", "海外业务"),
    "境内收入": ("国内收入", "境内销售", "国内销售"),
    "归母净利润": (
        "归属于母公司股东的净利润",
        "归属于上市公司股东的净利润",
        "净利润",
    ),
}

_CACHE: dict[str, RetrievalIndex] = {}


def get_retriever(index_path: Path | None = None) -> "Retriever":
    path = Path(index_path) if index_path else INDEX_DIR / "retrieval_v1"
    key = str(path.resolve())
    if key not in _CACHE:
        _CACHE[key] = load_index(path)
    return Retriever(_CACHE[key])


def clear_cache() -> None:
    _CACHE.clear()


def detect_companies(question: str, companies: Sequence[dict] | None = None) -> list[str]:
    """按证券代码、公司简称、常用简称识别题面公司。"""
    companies = list(companies) if companies is not None else load_companies()
    normalized = normalize_text(question)
    if ALL_PATTERN.search(normalized):
        return [row["证券代码"] for row in companies]

    found: list[str] = []
    for row in companies:
        code = row["证券代码"]
        aliases = [code, row["公司简称"], *COMPANY_ALIASES.get(code, ())]
        if any(normalize_text(alias) in normalized for alias in aliases):
            found.append(code)
    return found


INTENT_QUERY_TERMS = {
    "region": ("境内", "境外", "海外", "国外", "分地区"),
    "product": ("分产品", "产品品类", "产品构成", "品类", "空调业务", "主营业务构成"),
    "rd": ("研发投入", "研发费用", "研发强度"),
    "dividend": ("利润分配", "分红", "每10股", "现金红利"),
    "channel": ("线上销售", "线上渠道", "销售模式", "电商"),
    "financial": ("归母净利润", "归属于母公司股东的净利润", "同比变动"),
    "gross_margin": ("毛利率",),
}


def detect_intents(question: str) -> list[str]:
    normalized = normalize_text(question)
    intents = [
        name
        for name, terms in INTENT_QUERY_TERMS.items()
        if any(term in normalized for term in terms)
    ]
    # “营业收入”本身太宽泛，只有在问利润/同比等财务指标时才启用 financial。
    if "financial" in intents and not (
        "归母" in normalized
        or "净利润" in normalized
        or "同比" in normalized
    ):
        intents.remove("financial")
    return intents


def document_intent_score(text: str, intents: Sequence[str]) -> int:
    """检测文档是否具备问题所需的结构特征；只做可解释的规则加分。"""
    score = 0
    if "region" in intents:
        if "分地区" in text:
            score += 2
        if "主营业务分地区" in text:
            score += 2
        if ("境内" in text or "境外" in text) and "营业收入" in text:
            score += 1
        if ("国内" in text and "海外" in text) and "营业收入" in text:
            score += 2
        if ("境外收入" in text or "海外收入" in text or "国外收入" in text) and "占比" in text:
            score += 1
    if "product" in intents:
        if "分产品" in text and ("营业收入" in text or "毛利率" in text):
            score += 2
        if "主营业务分产品" in text:
            score += 2
        if "营业收入构成" in text:
            score += 2
        if "消费电器" in text and "毛利率" in text:
            score += 1
    if "rd" in intents:
        if "研发投入" in text and (
            "研发投入合计" in text or "研发投入总额占营业收入比例" in text
        ):
            score += 3
        if "研发费用" in text and "营业收入" in text:
            score += 1
    if "dividend" in intents:
        if "利润分配预案" in text:
            score += 3
        if "每10股" in text and "现金" in text:
            score += 2
        if "现金分红" in text:
            score += 1
    if "channel" in intents:
        if "分销售模式" in text:
            score += 2
        if "线上销售" in text and "营业收入" in text:
            score += 2
        if "线上渠道" in text:
            score += 1
    if "financial" in intents:
        if "主要会计数据" in text and "净利润" in text:
            score += 3
        if "利润表" in text and "营业总收入" in text:
            score += 2
        if (
            "营业收入" in text
            and "归属于上市公司股东的净利润" in text
            and "本年比上年" in text
        ):
            score += 2
        if "营业总收入" in text and "归属于母公司" in text:
            score += 1
    if "gross_margin" in intents:
        if "毛利率" in text and "营业成本" in text:
            score += 2
    return score


def financial_form(text: str) -> str | None:
    """识别财务证据的两种常见表式，用于多公司检索时的结构覆盖。

    这是表式分类，不是页码/公司白名单：主要会计数据页与合并利润表页
    都可能提供“营业总收入/归母净利润”，但二者口径和列结构不同。
    """
    if "主要会计数据" in text and "净利润" in text:
        return "主要会计数据"
    if "利润表" in text and "营业总收入" in text:
        return "利润表"
    return None


def metric_warnings(question: str, text: str) -> list[str]:
    """提示题面指标与片段用词不一致的情况；不做自动等价换算。"""
    query = normalize_text(question)
    body = normalize_text(text)
    warnings: list[str] = []
    for term, related_terms in METRIC_RELATIONS.items():
        if term not in query or term in body:
            continue
        present = [item for item in related_terms if item in body]
        if present:
            warnings.append(
                f"口径提示：题面指标“{term}”未在本片段出现；"
                f"片段出现“{'、'.join(present)}”，两者不必然等价，引用前需核对原文口径。"
            )
    return warnings


def _content_lines(text: str) -> list[str]:
    """去掉重复页眉和单独页码，保留表格正文行。"""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "年度报告" in line and len(line) <= 60:
            continue
        if re.fullmatch(r"\d{1,3}", line) or re.fullmatch(r"\d+\s*/\s*\d+", line):
            continue
        lines.append(line)
    return lines


def _looks_like_prose(line: str) -> bool:
    return any(mark in line for mark in "，。；") or len(line) > 120


def _looks_like_address(line: str) -> bool:
    """地址/联系方式行常含多个数字，但不是表格数据行。"""
    return any(
        mark in line
        for mark in ("地址", "邮政编码", "邮编", "联系电话", "电话", "传真", "电子信箱", "网址")
    )


def _looks_like_year_line(line: str) -> bool:
    """“2025 年 2024 年”这类纯跨列年份行不是数据行。

    含“项目/科目/附注/金额/增减”等列头词的年份行是表头，不能当作纯年份行
    跳过；否则“项目 附注 2025年度 2024年度”会被误判为缺少表头。
    """
    if any(
        mark in line
        for mark in ("项目", "科目", "附注", "金额", "单位", "占比", "比重", "增减", "同比")
    ):
        return False
    stripped = re.sub(r"\d{4}\s*年(?:度)?", "", line)
    stripped = re.sub(r"[\s()（）/、,，.．%\-]+", "", stripped)
    return not stripped and len(re.findall(r"\d{4}\s*年", line)) >= 2


def _looks_like_period_line(line: str) -> bool:
    """“2025 年 1-12 月”“2025 年度”这类期间行不是数据行。"""
    compact = re.sub(r"\s+", "", line)
    return bool(
        re.fullmatch(r"(?:19|20)\d{2}年(?:\d{1,2}[—\-~至]\d{1,2}月)?", compact)
    )


def _looks_like_data_line(line: str) -> bool:
    return len(re.findall(r"\d[\d,.]*%?", line)) >= 2


def _header_tokens(line: str) -> set[str]:
    return {token for token in HEADER_TOKENS if token in line}


def _is_header_line(line: str) -> bool:
    return len(_header_tokens(line)) >= 3


def analyze_table_header(text: str) -> dict:
    """判断表格页在首个数据行之前是否有列头。

    返回 ``has_local_header`` / ``has_data``。只有“本页有数据行但本页
    没有列头”才需要上一页上下文；纯正文页和纯表头页不扩展。

    列头可能被 PDF 折成多行（例如“项目”单独一行，下一行才是
    “收入 成本 收入 成本”）。因此除了单行 3 个标记的强列头，还会
    累计首个数据行之前的列头标记；累计达到 3 个才视为本页有列头。
    这样可避免给“自带折行列头”的下一页错误附加更早的页面。
    """
    header_tokens: set[str] = set()
    meaningful = 0
    for line in _content_lines(text):
        if _looks_like_prose(line) or _looks_like_address(line):
            continue
        tokens = _header_tokens(line)
        if len(tokens) >= 3:
            return {"has_local_header": True, "has_data": True, "first_data": None}
        if _looks_like_year_line(line) or _looks_like_period_line(line):
            header_tokens.update(tokens)
            continue
        if _looks_like_data_line(line):
            if len(header_tokens) >= 3:
                return {"has_local_header": True, "has_data": True, "first_data": None}
            return {
                "has_local_header": False,
                "has_data": True,
                "first_data": line[:120],
            }
        header_tokens.update(tokens)
        meaningful += 1
        if meaningful >= HEADER_SCAN_LINES:
            break
    return {"has_local_header": False, "has_data": False, "first_data": None}


def find_cross_page_header(text: str) -> dict | None:
    """寻找“接近页尾”的列头行，作为下一页的表头候选。

    不能因为上一页任意位置出现过列头就附加：真实年报一页常有多张表，
    页面前半段的列头属于本页表格，不是下一页表头。这里要求最后一个
    非正文列头行位于页面后段（``CROSS_PAGE_HEADER_TAIL_RATIO``），
    但仍只做位置判断；下一页是否有数据、是否已有本页表头，由
    ``attach_cross_page_context`` 继续检查。
    """
    lines = _content_lines(text)
    candidates: list[tuple[int, str, set[str]]] = []
    for index, line in enumerate(lines):
        if _looks_like_prose(line) or _looks_like_address(line):
            continue
        tokens = _header_tokens(line)
        if len(tokens) >= 3:
            candidates.append((index, line, tokens))
    if not candidates:
        return None
    index, line, tokens = candidates[-1]
    denominator = max(1, len(lines) - 1)
    ratio = index / denominator
    if ratio < CROSS_PAGE_HEADER_TAIL_RATIO:
        return None
    return {
        "line_index": index,
        "line": line[:160],
        "tokens": sorted(tokens),
        "position_ratio": round(ratio, 4),
    }


def has_table_header(text: str) -> bool:
    """是否在上一页后段存在可信列头候选（兼容旧调用）。"""
    return find_cross_page_header(text) is not None


def _has_compatible_table_data(text: str) -> bool:
    """下一页是否存在可衔接列头的表格数据行。

    要求至少 ``CROSS_PAGE_MIN_DATA_ROWS`` 条非正文、非地址、非列头、
    非纯年份行，且每行含 ``CROSS_PAGE_MIN_DATA_NUMBERS`` 个以上数字。
    纯审计说明、正文段落、单行附注和纯表头页都不满足。

    这里只验证“下一页是多行数字表”这一文本层面的结构，不做单元格
    坐标或严格列对齐；因此它只能排除明显不相接的页面，不能证明列名
    语义完全对应。
    """
    data_rows = 0
    for line in _content_lines(text):
        if _looks_like_prose(line) or _looks_like_address(line):
            continue
        if _is_header_line(line):
            continue
        if _looks_like_year_line(line) or _looks_like_period_line(line):
            continue
        numbers = re.findall(r"-?\d[\d,.]*%?", line)
        if len(numbers) >= CROSS_PAGE_MIN_DATA_NUMBERS:
            data_rows += 1
            if data_rows >= CROSS_PAGE_MIN_DATA_ROWS:
                return True
    return False


def _page_key(doc: Mapping) -> tuple[str, int, str, int]:
    return (
        str(doc["证券代码"]),
        int(doc["报告年份"]),
        str(doc["PDF文件名"]),
        int(doc["页码"]),
    )


def _context_fragment(
    doc: Mapping,
    related_page: int,
    *,
    header_evidence: Mapping | None = None,
) -> dict:
    """构造表头上下文片段；页码、来源仍是表头页自身，不与数据页合并。

    ``reliable_for_deterministic_answer`` 固定为 False：即使源页本身没有
    blocked，跨页关联也只是算法按位置/结构判断的候选，尚未人工核验。
    源页原值保留在 ``源页reliable_for_deterministic_answer``，避免把两者
    混为一谈。
    """
    fragment = {
        "doc_id": doc["doc_id"],
        "证券代码": doc["证券代码"],
        "公司简称": doc["公司简称"],
        "报告年份": doc["报告年份"],
        "PDF文件名": doc["PDF文件名"],
        "页码": doc["页码"],
        "块序号": doc["块序号"],
        "类型": doc["类型"],
        "章节": doc["章节"],
        "字符数": doc["字符数"],
        "正文": doc["正文"],
        "source": doc["source"],
        "warnings": doc["warnings"],
        "blocking_anomalies": doc["blocking_anomalies"],
        "blocked": doc["blocked"],
        "源页reliable_for_deterministic_answer": doc[
            "reliable_for_deterministic_answer"
        ],
        "reliable_for_deterministic_answer": False,
        "context_role": "表头来源",
        "关联数据页": related_page,
        "上下文关联状态": "算法判定，待人工核验",
        "上下文关联可靠": False,
        "说明": (
            "该片段是上一页的表头上下文，不是命中页原文；"
            "页码为表头来源页，数据仍以关联数据页为准；"
            "跨页关联为算法判定，未经人工核验，不能作为确定性证据。"
        ),
    }
    if header_evidence:
        fragment["上下文关联依据"] = dict(header_evidence)
    return fragment


def attach_cross_page_context(
    results: Sequence[dict],
    documents: Sequence[dict],
    *,
    max_contexts: int = MAX_CONTEXT_FRAGMENTS,
) -> list[dict]:
    """给缺少本页列头的表格命中补充上一页表头。

    约束：
    - 只处理 ``类型=table`` 且本页从数据行开始的命中；
    - 上一页必须同证券代码、同报告年份、同 PDF，且确有列头；
    - 每个片段保留自己的页码，不把上一页正文写进命中页；
    - 表头页已在主结果时不重复追加，只标注来源；
    - 最多追加 ``max_contexts`` 条，按命中排名优先。
    """
    page_lookup: dict[tuple[str, int, str, int], list[int]] = collections.defaultdict(list)
    for doc_index, doc in enumerate(documents):
        page_lookup[_page_key(doc)].append(doc_index)
    for indexes in page_lookup.values():
        indexes.sort(key=lambda i: (int(documents[i].get("块序号", 1)), i))

    primary_pages = {
        (str(item["证券代码"]), int(item["页码"])) for item in results
    }
    budget = max(0, int(max_contexts))

    for result in results:
        if result.get("类型") != "table":
            continue
        page = int(result["页码"])
        state = analyze_table_header(result.get("正文", ""))
        if state["has_local_header"]:
            result["跨页表头"] = {
                "需要表头上下文": False,
                "数据页": page,
                "表头页": None,
                "表头来源": "页内已有表头",
                "关联可靠性": "不适用（页内表头）",
                "说明": "本页首个数据行之前已有列头，不追加相邻页。",
            }
            continue
        if not state["has_data"]:
            result["跨页表头"] = {
                "需要表头上下文": False,
                "数据页": page,
                "表头页": None,
                "表头来源": "本页无数据行",
                "关联可靠性": "不适用（本页无数据）",
                "说明": "本页没有可识别的数据行，不追加表头上下文。",
            }
            continue

        previous_page = page - 1
        key = (
            str(result["证券代码"]),
            int(result["报告年份"]),
            str(result["PDF文件名"]),
            previous_page,
        )
        previous_tables = [
            index
            for index in page_lookup.get(key, [])
            if documents[index].get("类型") == "table"
        ]
        if not previous_tables:
            result["跨页表头"] = {
                "需要表头上下文": True,
                "数据页": page,
                "表头页": None,
                "表头来源": "未找到",
                "关联可靠性": "未确认，不附加",
                "警告": "上一页没有同公司、同报告年份、同 PDF 的表格块，不能建立跨页关联。",
                "说明": "本页从数据行开始，但上一页没有同公司同 PDF 的表格块。",
            }
            continue
        previous = documents[previous_tables[-1]]
        header = find_cross_page_header(previous.get("正文", ""))
        if header is None:
            result["跨页表头"] = {
                "需要表头上下文": True,
                "数据页": page,
                "表头页": None,
                "表头来源": "未找到",
                "候选表头页": int(previous["页码"]),
                "候选表头页doc_id": previous["doc_id"],
                "关联可靠性": "未确认，不附加",
                "警告": (
                    "上一页没有位于页尾的可信列头；页面前半段列头可能属于"
                    "上一页自己的表格，不能作为下一页表头。"
                ),
                "说明": "本页从数据行开始，但上一页列头位置/可信度不足，不附加。",
            }
            continue
        if not _has_compatible_table_data(result.get("正文", "")):
            result["跨页表头"] = {
                "需要表头上下文": True,
                "数据页": page,
                "表头页": None,
                "表头来源": "未找到",
                "候选表头页": int(previous["页码"]),
                "候选表头页doc_id": previous["doc_id"],
                "候选表头位置": header["position_ratio"],
                "关联可靠性": "未确认，不附加",
                "警告": (
                    "下一页没有至少两条含多个数字的表格数据行，"
                    "无法确认上一页列头与它属于同一张表。"
                ),
                "说明": "本页从数据行开始，但下一页结构不满足表格数据条件，不附加。",
            }
            continue

        if (str(previous["证券代码"]), int(previous["页码"])) in primary_pages:
            result["跨页表头"] = {
                "需要表头上下文": True,
                "数据页": page,
                "表头页": int(previous["页码"]),
                "表头来源": "主结果",
                "表头页doc_id": previous["doc_id"],
                "候选表头位置": header["position_ratio"],
                "关联可靠性": "算法判定，待人工核验（主结果页）",
                "说明": "表头页已在本次主结果中，可直接对照，不重复追加片段。",
            }
            continue

        if budget <= 0:
            result["跨页表头"] = {
                "需要表头上下文": True,
                "数据页": page,
                "表头页": int(previous["页码"]),
                "表头来源": "未扩展（达到上限）",
                "表头页doc_id": previous["doc_id"],
                "候选表头位置": header["position_ratio"],
                "关联可靠性": "未附加（达到上限）",
                "说明": "已识别到上一页页尾列头，但本次上下文片段达到上限。",
            }
            continue

        evidence = {
            "表头位置": header["position_ratio"],
            "列头标记": header["tokens"],
            "下一页表格数据": True,
        }
        fragment = _context_fragment(previous, page, header_evidence=evidence)
        result.setdefault("上下文片段", []).append(fragment)
        result["跨页表头"] = {
            "需要表头上下文": True,
            "数据页": page,
            "表头页": int(previous["页码"]),
            "表头来源": "上下文",
            "表头页doc_id": previous["doc_id"],
            "候选表头位置": header["position_ratio"],
            "关联可靠性": "算法判定，待人工核验",
            "说明": (
                "本页从数据行开始；上一页页尾列头已作为独立上下文片段附加，"
                "页码分别保留；关联未经人工核验，不能作为确定性证据。"
            ),
        }
        budget -= 1

    return list(results)


def reciprocal_rank_fusion(
    bm25_ranked: Sequence[tuple[int, float]],
    vector_ranked: Sequence[tuple[int, float]],
    intent_ranked: Sequence[tuple[int, float]] | None = None,
    *,
    rrf_k: int = RRF_K,
    limit: int = 200,
    intent_weight: float = 2.0,
) -> list[dict]:
    """融合关键词、向量与结构意图三个排序，返回按 RRF 分数降序的候选。"""
    fused: dict[int, float] = {}
    bm25_rank: dict[int, int] = {}
    vector_rank: dict[int, int] = {}
    intent_rank: dict[int, int] = {}
    bm25_score: dict[int, float] = {}
    vector_score: dict[int, float] = {}
    intent_score: dict[int, float] = {}

    for rank, (doc_index, score) in enumerate(bm25_ranked[:limit], 1):
        fused[doc_index] = fused.get(doc_index, 0.0) + 1.0 / (rrf_k + rank)
        bm25_rank[doc_index] = rank
        bm25_score[doc_index] = float(score)
    for rank, (doc_index, score) in enumerate(vector_ranked[:limit], 1):
        fused[doc_index] = fused.get(doc_index, 0.0) + 1.0 / (rrf_k + rank)
        vector_rank[doc_index] = rank
        vector_score[doc_index] = float(score)
    for rank, (doc_index, score) in enumerate((intent_ranked or [])[:limit], 1):
        fused[doc_index] = fused.get(doc_index, 0.0) + intent_weight / (rrf_k + rank)
        intent_rank[doc_index] = rank
        intent_score[doc_index] = float(score)

    ordered = sorted(
        fused,
        key=lambda index: (
            -fused[index],
            -intent_score.get(index, 0.0),
            -bm25_score.get(index, 0.0),
            -vector_score.get(index, 0.0),
            index,
        ),
    )
    return [
        {
            "doc_index": index,
            "rrf": fused[index],
            "bm25_score": bm25_score.get(index),
            "vector_score": vector_score.get(index),
            "intent_score": intent_score.get(index),
            "bm25_rank": bm25_rank.get(index),
            "vector_rank": vector_rank.get(index),
            "intent_rank": intent_rank.get(index),
        }
        for index in ordered
    ]


def select_balanced_candidates(
    grouped: Mapping[str, Sequence[dict]],
    codes: Sequence[str],
    per_company: int,
    documents: Sequence[dict],
    intents: Sequence[str],
) -> list[dict]:
    """跨公司检索：先保证每家公司在每个结构意图上有一条证据，再用融合排序补足。

    多意图问题（例如同时问境外收入与研发投入）如果只按全局融合排序截断，
    某家公司可能被同一意图的多条结果占满。这里只做“覆盖”，不改变各通道
    的打分，也不把弱相关文档当作答案。
    """
    selected: list[dict] = []
    for code in codes:
        candidates = list(grouped.get(code, []))
        if not candidates:
            continue
        picked: list[dict] = []
        picked_indexes: set[int] = set()
        for intent in intents:
            if len(picked) >= per_company:
                break
            best: dict | None = None
            best_score = 0
            for candidate in candidates:
                text = documents[candidate["doc_index"]].get("正文", "")
                score = document_intent_score(text, [intent])
                if score > best_score:
                    best = candidate
                    best_score = score
            if best is not None and best["doc_index"] not in picked_indexes:
                picked.append(best)
                picked_indexes.add(best["doc_index"])

        # 财务题常同时需要“主要会计数据”和“合并利润表”两种表式：
        # 例如 Q08 的营业总收入在利润表页，归母净利润也可能在主要会计
        # 数据页。若只按一个 financial 意图取一条，同一公司的连续表格页
        # 会把另一种表式挤出 per_company 上限。这里只补表式覆盖，不按
        # 公司或页码硬编码。
        if "financial" in intents:
            picked_forms = {
                financial_form(documents[item["doc_index"]].get("正文", ""))
                for item in picked
            }
            for form in ("主要会计数据", "利润表"):
                if len(picked) >= per_company or form in picked_forms:
                    continue
                best = None
                best_score = 0
                for candidate in candidates:
                    if candidate["doc_index"] in picked_indexes:
                        continue
                    text = documents[candidate["doc_index"]].get("正文", "")
                    if financial_form(text) != form:
                        continue
                    score = document_intent_score(text, ["financial"])
                    if score > best_score:
                        best = candidate
                        best_score = score
                if best is not None:
                    picked.append(best)
                    picked_indexes.add(best["doc_index"])
                    picked_forms.add(form)

        for candidate in candidates:
            if len(picked) >= per_company:
                break
            if candidate["doc_index"] not in picked_indexes:
                picked.append(candidate)
                picked_indexes.add(candidate["doc_index"])
        selected.extend(picked)
    return selected


class Retriever:
    def __init__(self, index: RetrievalIndex):
        self.index = index
        self._len_norm = [
            index.k1 * (1.0 - index.b + index.b * length / index.avgdl)
            for length in index.doc_len
        ]

    # ------------------------------------------------------------------
    # 单通道打分
    # ------------------------------------------------------------------

    def bm25_scores(
        self, question: str, allowed: set[int] | None = None
    ) -> list[tuple[int, float]]:
        counts = query_token_counts(question)
        if not counts:
            return []
        scores: dict[int, float] = {}
        total = self.index.count
        for token, query_tf in counts.items():
            postings = self.index.postings.get(token)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
            for packed in postings:
                doc_index = packed // PACK_BASE
                if allowed is not None and doc_index not in allowed:
                    continue
                tf = packed % PACK_BASE
                denominator = tf + self._len_norm[doc_index]
                contribution = idf * query_tf * (tf * (self.index.k1 + 1.0) / denominator)
                scores[doc_index] = scores.get(doc_index, 0.0) + contribution
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))

    def intent_scores(
        self, question: str, allowed: set[int] | None = None
    ) -> list[tuple[int, float]]:
        """按问题意图给具备相应表格结构的文档加分。"""
        intents = detect_intents(question)
        if not intents:
            return []
        scored: list[tuple[int, float]] = []
        for doc_index, doc in enumerate(self.index.documents):
            if allowed is not None and doc_index not in allowed:
                continue
            score = document_intent_score(doc["正文"], intents)
            # 只有具备明确结构特征（>=2）的文档才进入意图通道；
            # 单个关键词命中（1 分）仍交给 BM25/向量，避免结构规则过拟合。
            if score >= 2:
                scored.append((doc_index, float(score)))
        return sorted(scored, key=lambda item: (-item[1], item[0]))

    def vector_scores(
        self, question: str, allowed: set[int] | None = None
    ) -> list[tuple[int, float]]:
        counts = query_token_counts(question)
        if not counts:
            return []
        query = np.zeros(self.index.dim, dtype=np.float32)
        for token, tf in counts.items():
            bucket, sign = hash_token(token, self.index.dim)
            query[bucket] += (1.0 + math.log(tf)) * float(self.index.idf[bucket]) * sign
        norm = float(np.linalg.norm(query))
        if not norm:
            return []
        query /= norm

        scores = self.index.vectors @ query
        if allowed is not None:
            mask = np.zeros(self.index.count, dtype=bool)
            for doc_index in allowed:
                mask[doc_index] = True
            scores = np.where(mask, scores, -np.inf)
        order = np.argsort(-scores)
        results: list[tuple[int, float]] = []
        for doc_index in order:
            score = float(scores[doc_index])
            if not math.isfinite(score):
                break
            results.append((int(doc_index), score))
        return results

    # ------------------------------------------------------------------
    # 范围与结果
    # ------------------------------------------------------------------

    def resolve_scope(
        self,
        question: str,
        company_codes: Sequence[str] | None = None,
        *,
        mode: str = "auto",
        per_company: int | None = None,
    ) -> dict:
        companies = load_companies()
        by_code = {row["证券代码"]: row for row in companies}
        detected = detect_companies(question, companies)

        if company_codes:
            codes = [str(code).zfill(6) for code in company_codes]
            unknown = [code for code in codes if code not in by_code]
            if unknown:
                raise ValueError(f"未知证券代码：{unknown}")
            codes = list(dict.fromkeys(codes))
        elif mode == "all":
            codes = [row["证券代码"] for row in companies]
        elif mode == "auto":
            codes = detected
        else:
            raise ValueError(f"未知 mode：{mode!r}（可选 auto / all）")

        if not codes:
            codes = [row["证券代码"] for row in companies]
            scope_kind = "全部公司（题面未识别到具体公司，按全局排序）"
            balanced = False
        elif len(codes) == 1:
            scope_kind = "单公司"
            balanced = False
        else:
            scope_kind = "跨公司"
            balanced = True
        if per_company is None and balanced:
            per_company = 3 if len(codes) <= 5 else 2
        if per_company is not None and per_company < 1:
            raise ValueError("per_company 必须 >= 1")

        return {
            "codes": codes,
            "detected": detected,
            "mode": mode,
            "scope_kind": scope_kind,
            "balanced": balanced,
            "per_company": per_company,
            "companies": [by_code[code]["公司简称"] for code in codes],
        }

    def _document_result(self, candidate: dict, rank: int) -> dict:
        doc = self.index.documents[candidate["doc_index"]]
        return {
            "排名": rank,
            "doc_id": doc["doc_id"],
            "证券代码": doc["证券代码"],
            "公司简称": doc["公司简称"],
            "报告年份": doc["报告年份"],
            "PDF文件名": doc["PDF文件名"],
            "页码": doc["页码"],
            "块序号": doc["块序号"],
            "类型": doc["类型"],
            "章节": doc["章节"],
            "字符数": doc["字符数"],
            "正文": doc["正文"],
            "需复核": doc["需复核"],
            "日期恢复": doc["日期恢复"],
            "warnings": doc["warnings"],
            "anomalies": doc["anomalies"],
            "blocking_anomalies": doc["blocking_anomalies"],
            "blocked": doc["blocked"],
            "reliable_for_deterministic_answer": doc["reliable_for_deterministic_answer"],
            "table": doc["table"],
            "source": doc["source"],
            "scores": {
                "rrf": round(float(candidate["rrf"]), 8),
                "bm25": None
                if candidate["bm25_score"] is None
                else round(float(candidate["bm25_score"]), 8),
                "vector": None
                if candidate["vector_score"] is None
                else round(float(candidate["vector_score"]), 8),
                "intent": None
                if candidate.get("intent_score") is None
                else round(float(candidate["intent_score"]), 4),
                "bm25_rank": candidate["bm25_rank"],
                "vector_rank": candidate["vector_rank"],
                "intent_rank": candidate.get("intent_rank"),
            },
        }

    # ------------------------------------------------------------------
    # 对外检索
    # ------------------------------------------------------------------

    def search(
        self,
        question: str,
        top_k: int | None = None,
        company_codes: Sequence[str] | None = None,
        *,
        mode: str = "auto",
        per_company: int | None = None,
    ) -> list[dict]:
        top_k = top_k or DEFAULT_TOP_K
        if top_k < 1:
            raise ValueError("top_k 必须 >= 1")
        scope = self.resolve_scope(
            question, company_codes, mode=mode, per_company=per_company
        )
        code_set = set(scope["codes"])
        by_code = {row["证券代码"]: row for row in load_companies()}
        allowed = {
            index
            for index, doc in enumerate(self.index.documents)
            if doc["证券代码"] in code_set
        }

        bm25_ranked = self.bm25_scores(question, allowed)
        vector_ranked = self.vector_scores(question, allowed)
        intent_ranked = self.intent_scores(question, allowed)
        intents = detect_intents(question)
        candidate_limit = max(200, top_k * 20)
        if scope["per_company"]:
            candidate_limit = max(candidate_limit, scope["per_company"] * len(code_set) * 10)
        fused = reciprocal_rank_fusion(
            bm25_ranked,
            vector_ranked,
            intent_ranked,
            rrf_k=self.index.rrf_k,
            limit=candidate_limit,
        )

        if not scope["balanced"]:
            selected = fused[:top_k]
        else:
            grouped: dict[str, list[dict]] = collections.defaultdict(list)
            for candidate in fused:
                doc = self.index.documents[candidate["doc_index"]]
                grouped[doc["证券代码"]].append(candidate)
            selected = select_balanced_candidates(
                grouped,
                scope["codes"],
                scope["per_company"],
                self.index.documents,
                intents,
            )
            selected.sort(
                key=lambda item: (
                    -item["rrf"],
                    -item["bm25_score"] if item["bm25_score"] is not None else 0.0,
                    -item["vector_score"] if item["vector_score"] is not None else 0.0,
                    item["doc_index"],
                )
            )

        results = [
            self._document_result(candidate, rank)
            for rank, candidate in enumerate(selected, 1)
        ]
        for result in results:
            result["检索范围"] = {
                "类型": scope["scope_kind"],
                "公司": scope["companies"],
                "每公司上限": scope["per_company"],
                "题面识别公司": [by_code[code]["公司简称"] for code in scope["detected"]],
            }
            result["检索意图"] = intents

        results = attach_cross_page_context(results, self.index.documents)
        for result in results:
            result["口径提示"] = metric_warnings(question, result["正文"])
            for fragment in result.get("上下文片段", []):
                fragment["口径提示"] = metric_warnings(question, fragment["正文"])
        return results

    def explain(self, question: str, **kwargs) -> dict:
        scope = self.resolve_scope(
            question,
            kwargs.get("company_codes"),
            mode=kwargs.get("mode", "auto"),
            per_company=kwargs.get("per_company"),
        )
        return scope


def retrieve(
    question: str,
    top_k: int | None = None,
    company_codes: Sequence[str] | None = None,
    *,
    mode: str = "auto",
    per_company: int | None = None,
    index_path: Path | None = None,
) -> list[dict]:
    """返回召回结果列表。每项含完整原文、页码、来源与异常警告。"""
    return get_retriever(index_path).search(
        question,
        top_k=top_k,
        company_codes=company_codes,
        mode=mode,
        per_company=per_company,
    )


def _print_result(result: dict, snippet: int) -> None:
    print(
        f"[{result['排名']}] {result['公司简称']} {result['证券代码']} "
        f"p{result['页码']} {result['类型']} {result['章节'] or '（无章节）'}"
    )
    scores = result["scores"]
    print(
        f"    RRF={scores['rrf']:.6f} BM25={scores['bm25']} "
        f"(rank {scores['bm25_rank']}) 向量={scores['vector']} "
        f"(rank {scores['vector_rank']}) 结构意图={scores.get('intent')} "
        f"(rank {scores.get('intent_rank')})"
    )
    if result["warnings"]:
        print("    警告：" + "；".join(result["warnings"][:4]))
    if result["blocked"]:
        print("    ** 含阻断异常，不得作为已确认事实 **")
    text = result["正文"].replace("\n", " ")
    print("    原文：" + text[:snippet] + ("…" if len(text) > snippet else ""))
    print()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="混合检索原型（BM25 + 字符 n-gram TF-IDF）")
    parser.add_argument("question", nargs="+", help="问题文本")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--company", action="append", default=None, help="证券代码，可重复")
    parser.add_argument("--per-company", type=int, default=None, help="跨公司时每家公司取几条")
    parser.add_argument("--mode", choices=("auto", "all"), default="auto")
    parser.add_argument("--index", type=Path, default=None)
    parser.add_argument("--snippet", type=int, default=320)
    parser.add_argument("--json", action="store_true", help="输出完整 JSON（含全文）")
    args = parser.parse_args(list(argv) if argv is not None else None)

    question = " ".join(args.question)
    retriever = get_retriever(args.index)
    scope = retriever.explain(
        question,
        company_codes=args.company,
        mode=args.mode,
        per_company=args.per_company,
    )
    results = retriever.search(
        question,
        top_k=args.top_k,
        company_codes=args.company,
        mode=args.mode,
        per_company=args.per_company,
    )
    print(f"问题：{question}")
    print(
        f"范围：{scope['scope_kind']}；公司：{'、'.join(scope['companies'])}；"
        f"每公司上限：{scope['per_company']}；返回 {len(results)} 条"
    )
    print()
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            _print_result(result, args.snippet)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
