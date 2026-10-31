"""基于检索结果和确定性规则的答案生成（第一阶段）。

本模块**不调用任何大模型接口**，也不把检索排名当作答案。它只做四件事：

1. 从 ``src.retrieve`` 返回的原文块中按指标名、章节、表格标题抽取行；
2. 对单位、分母、跨页表头、阻断状态和数值冲突做显式检查；
3. 按固定状态输出答案：可回答 / 需口径说明 / 需核验 / 证据冲突 /
   证据不足 / 已阻断；
4. 每条指标都保留公司、证券代码、报告年份、指标原文、数值、单位、
   数据页、表头来源页、表名、证据原文和是否采信。

边界
----
* ``blocked=true`` 或 ``reliable_for_deterministic_answer=false`` 的片段，
  其中的数值只能作为“候选（未采信）”记录，不能进入结论；
* 跨页表头仍为“算法判定，待人工核验”时，依赖该表头的数值标为
  ``需核验``，不声称人工已确认；
* 指标名称不一致时保留原文，不做自动替换或换算；
* 程序规则生成的“可回答”只是**规则判定**，不是人工核验，也不是
  答案准确率。所有答案的 ``人工核验状态`` 固定为 ``待核验``。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from src.companies import load_companies, load_questions
from src.config import QUESTIONS_CSV, REPORT_YEAR, make_output_robust
from src.retrieve import ALL_PATTERN, detect_companies, retrieve

# ---------------------------------------------------------------------------
# 状态与常量
# ---------------------------------------------------------------------------

STATUS_ANSWERABLE = "可回答"
STATUS_NEEDS_DEFINITION = "需口径说明"
STATUS_NEEDS_VERIFICATION = "需核验"
STATUS_CONFLICT = "证据冲突"
STATUS_INSUFFICIENT = "证据不足"
STATUS_BLOCKED = "已阻断"

ANSWER_STATUSES = (
    STATUS_ANSWERABLE,
    STATUS_NEEDS_DEFINITION,
    STATUS_NEEDS_VERIFICATION,
    STATUS_CONFLICT,
    STATUS_INSUFFICIENT,
    STATUS_BLOCKED,
)

REFUSAL_SENTENCE = "现有证据不足，无法可靠回答。"
HUMAN_PENDING = "待核验"
METHOD_NOTE = "确定性规则抽取；未调用大模型；未经人工语义核验。"
ANSWER_ACCURACY = "未评测"

UNIT_SCALE = {
    "元": Decimal("1"),
    "千元": Decimal("1000"),
    "万元": Decimal("10000"),
    "亿元": Decimal("100000000"),
}

REGION_FOREIGN_LABELS = (
    "境外",
    "境外销售",
    "境外地区",
    "境外收入",
    "海外",
    "海外收入",
    "国外",
    "国外销售",
    "出口",
    "外销",
    "外销-主营业务",
)
REGION_DOMESTIC_LABELS = (
    "境内",
    "境内销售",
    "境内地区",
    "国内",
    "国内销售",
    "国内地区",
    "内销",
    "内销-主营业务",
)

SPLIT_PARENT_LABELS = (
    "归属于母公司股东的",
    "归属于上市公司股东的",
    "归属于母公司所有者的",
)

_NUMBER_RE = re.compile(r"(?<![\d.])[-−]?\(?\d[\d,]*(?:\.\d+)?\)?[%％]?")
_NOTE_REF_RE = re.compile(r"[一二三四五六七八九十百]+\(\d{1,3}\)")
_NOTE_REF2_RE = re.compile(r"[一二三四五六七八九十百]+、\s*\d{1,3}")
_PAREN_NOTE_RE = re.compile(r"\(\d{1,3}\)")


# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------


@dataclass
class NumberToken:
    """一行中的一个数字 token。``value`` 已去掉千分位和百分号。"""

    text: str
    value: Decimal
    start: int
    end: int
    is_percent: bool
    is_year: bool = False


@dataclass
class Evidence:
    """一条可审计的指标证据。"""

    company: str
    code: str
    year: int
    metric: str
    value: str
    unit: str
    page: int
    table_title: str = ""
    excerpt: str = ""
    source_type: str = "原文值"
    definition_note: str = ""
    header_page: int | None = None
    blocked: bool = False
    adopted: bool = True
    context_status: str = ""
    reliable: bool = True
    needs_header_verification: bool = False
    doc_id: str = ""
    calculation: str = ""
    numeric: Decimal | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "公司简称": self.company,
            "证券代码": self.code,
            "报告年份": self.year,
            "指标原文": self.metric,
            "数值": self.value,
            "单位": self.unit,
            "数据页码": self.page,
            "表头来源页码": self.header_page,
            "表名": self.table_title,
            "证据原文": self.excerpt,
            "来源类型": self.source_type,
            "口径说明": self.definition_note,
            "blocked": self.blocked,
            "是否采信": self.adopted and not self.blocked,
            "上下文关联状态": self.context_status,
            "reliable_for_deterministic_answer": self.reliable,
            "表头核验状态": (
                "待人工核验" if self.needs_header_verification else "不适用"
            ),
            "计算依据": self.calculation,
            "doc_id": self.doc_id,
        }

    def citation_complete(self) -> bool:
        return all(
            [
                self.company,
                self.code,
                self.year,
                self.metric,
                self.value,
                self.unit,
                self.page,
                self.excerpt,
            ]
        )


@dataclass
class PageRecord:
    code: str
    company: str
    year: int
    page: int
    doc_id: str
    lines: list[tuple[str, str]]
    unit: str
    is_context: bool
    blocked: bool
    reliable: bool
    table_title: str = ""
    header_page: int | None = None
    context_status: str = ""
    needs_header_verification: bool = False
    result: dict = field(default_factory=dict, repr=False)


@dataclass
class Row:
    page: PageRecord
    raw_label: str
    label: str
    numbers: list[NumberToken]
    excerpt: str
    table_title: str = ""
    section: str = ""
    title_local: bool = False
    needs_header_verification: bool = False

    @property
    def company(self) -> str:
        return self.page.company

    @property
    def code(self) -> str:
        return self.page.code

    @property
    def year(self) -> int:
        return self.page.year

    @property
    def unit(self) -> str:
        return self.page.unit

    def amounts(self) -> list[NumberToken]:
        return [t for t in self.numbers if _is_amount_token(t)]

    def first_amount(self) -> NumberToken | None:
        values = self.amounts()
        return values[0] if values else None

    def second_amount(self) -> NumberToken | None:
        values = self.amounts()
        return values[1] if len(values) > 1 else None

    def disclosed_ratio(self) -> tuple[Decimal, str] | None:
        """返回原文紧随金额之后的占比列；不把毛利率当作收入占比。"""
        first = self.first_amount()
        if first is None:
            return None
        try:
            index = self.numbers.index(first)
        except ValueError:
            return None
        if index + 1 >= len(self.numbers):
            return None
        nxt = self.numbers[index + 1]
        if nxt.is_percent:
            return nxt.value, nxt.text
        # 部分年报的“占营业收入比重”列不带 %；只在明确的
        # “营业收入构成”表中识别，且要求后面还有金额列。
        title = _compact(self.table_title)
        if (
            "营业收入构成" in title
            and not nxt.is_year
            and Decimal("0") <= nxt.value <= Decimal("100")
            and index + 2 < len(self.numbers)
            and _is_amount_token(self.numbers[index + 2])
        ):
            return nxt.value, nxt.text
        return None


@dataclass
class Answer:
    question_id: str
    question: str
    status: str
    answer_text: str
    metrics: list[Evidence]
    scope_codes: list[str]
    report_year: int = REPORT_YEAR
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    refusal_reasons: list[str] = field(default_factory=list)
    deterministic: bool = False
    audit: dict = field(default_factory=dict)
    method: str = METHOD_NOTE
    human_verification: str = HUMAN_PENDING

    def to_dict(self) -> dict:
        metrics = [m.to_dict() for m in self.metrics]
        companies: list[str] = []
        codes: list[str] = []
        for m in self.metrics:
            if m.company and m.company not in companies:
                companies.append(m.company)
            if m.code and m.code not in codes:
                codes.append(m.code)
        for code in self.scope_codes:
            if code not in codes:
                codes.append(code)
        data_pages = sorted({m.page for m in self.metrics if m.page})
        header_pages = sorted(
            {m.header_page for m in self.metrics if m.header_page is not None}
        )
        excerpts: list[str] = []
        for m in self.metrics:
            if m.excerpt and m.excerpt not in excerpts:
                excerpts.append(m.excerpt)
        return {
            "题目编号": self.question_id,
            "问题": self.question,
            "回答状态": self.status,
            "答案正文": self.answer_text,
            "公司": companies,
            "证券代码": codes,
            "报告年份": self.report_year,
            "检索范围证券代码": self.scope_codes,
            "指标": metrics,
            "数据页码": data_pages,
            "表头来源页码": header_pages,
            "证据原文": excerpts,
            "口径说明": self.notes,
            "警告": self.warnings,
            "拒答原因": self.refusal_reasons,
            "确定性作答": self.deterministic,
            "人工核验状态": self.human_verification,
            "方法说明": self.method,
            "审计": self.audit,
        }


# ---------------------------------------------------------------------------
# 文本与数字工具
# ---------------------------------------------------------------------------


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _norm_question(text: str) -> str:
    return _compact(text).lower()


def _parse_decimal(raw: str) -> Decimal | None:
    s = raw.strip()
    negative = (
        s.startswith("(")
        or s.startswith("-")
        or s.startswith("−")
    )
    s = s.strip("()")
    s = s.replace(",", "").replace("−", "-").replace("%", "").replace("％", "")
    if not s or s in {"-", "—", "–"}:
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return -value if negative and value > 0 else value


def _scan_numbers(text: str) -> list[NumberToken]:
    tokens: list[NumberToken] = []
    for match in _NUMBER_RE.finditer(text):
        raw = match.group(0)
        value = _parse_decimal(raw)
        if value is None:
            continue
        is_percent = raw.endswith("%") or raw.endswith("％")
        is_year = (
            not is_percent
            and "," not in raw
            and "." not in raw
            and len(raw.lstrip("+−-")) == 4
            and 1900 <= int(value) <= 2100
        )
        tokens.append(
            NumberToken(
                text=raw,
                value=value,
                start=match.start(),
                end=match.end(),
                is_percent=is_percent,
                is_year=is_year,
            )
        )
    return tokens


def _is_amount_token(token: NumberToken) -> bool:
    if token.is_percent or token.is_year:
        return False
    return "," in token.text or "." in token.text or abs(token.value) >= 1000


def _format_decimal(value: Decimal, places: int = 2) -> str:
    if value == value.to_integral_value():
        return f"{value:,.0f}"
    quant = Decimal(1).scaleb(-places)
    return f"{value.quantize(quant, rounding=ROUND_HALF_UP):,.{places}f}"


def _format_signed(value: Decimal, places: int = 2) -> str:
    text = _format_decimal(abs(value), places)
    return f"+{text}" if value > 0 else f"-{text}" if value < 0 else text


def _format_pct(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}%"


def _ratio_percent(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return (numerator / denominator * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _strip_leading_numbering(text: str) -> str:
    s = text.strip()
    patterns = (
        r"^[（(]?[一二三四五六七八九十百]+[）)]?\s*[、.．]?\s*",
        r"^[（(]?\d{1,2}[）)]?\s*[、.．]\s*",
        r"^[（(]\d{1,2}[）)]\s*",
    )
    for pattern in patterns:
        new = re.sub(pattern, "", s, count=1)
        if new != s:
            s = new
            break
    return s.strip()


def _strip_label_prefix(label: str) -> str:
    s = label.strip()
    for prefix in ("其中：", "其中:", "减：", "减:", "加：", "加:"):
        if s.startswith(prefix):
            return s[len(prefix) :].strip()
    return s


def _strip_note_refs(text: str) -> str:
    text = _NOTE_REF_RE.sub(" ", text)
    text = _NOTE_REF2_RE.sub(" ", text)
    text = _PAREN_NOTE_RE.sub(" ", text)
    return text


def _merge_broken_labels(lines: Sequence[str]) -> list[tuple[str, str]]:
    """把 PDF 折行的指标标签拼回去；证据原文仍保留拼接来源。"""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        # 部分年报： “归属于母公司股东的” + “净利润 ...”，标签被 PDF 折行拆开。
        if line in SPLIT_PARENT_LABELS and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt.startswith("净利润"):
                combined = line + nxt
                out.append((combined, line + " " + nxt))
                i += 2
                continue
        # 部分年报： “研发投入总额占营业收入比 ...” + “例（%）”，指标名被拆开。
        if (
            re.match(r"^研发投入总额占营业收入比\s", line)
            and i + 1 < len(lines)
            and lines[i + 1].strip().startswith("例")
        ):
            match = re.match(r"^(研发投入总额占营业收入比)(\s+.*)$", line)
            if match:
                combined = match.group(1) + lines[i + 1].strip() + match.group(2)
                out.append((combined, line + " " + lines[i + 1].strip()))
                i += 2
                continue
        out.append((line, line))
        i += 1
    return out


def _detect_unit(text: str) -> str:
    if not text:
        return ""
    compact = _compact(text)
    patterns = (
        r"金额单位为人民币(千元|万元|亿元|元)",
        r"单位[:：](?:人民币)?(千元|万元|亿元|元)",
        r"单位[:：]人民币(千元|万元|亿元|元)",
    )
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            return match.group(1)
    return ""


def _detect_unit_from_result(result: Mapping) -> str:
    table = result.get("table") or {}
    for line in table.get("单位行") or []:
        unit = _detect_unit(str(line))
        if unit:
            return unit
    return _detect_unit(str(result.get("正文", "")))


def _table_title(line: str) -> str:
    compact = _compact(line)
    if len(compact) > 100:
        return ""
    if (
        "营业收入构成" in compact
        or ("10%以上" in compact and "行业" in compact)
        or "研发投入情况表" in compact
        or "合并利润表" in compact
        or "合并及公司利润表" in compact
        or "主要会计数据" in compact
        or compact == "主营业务分地区情况"
        or (
            "主营业务分行业" in compact
            and "分地区" in compact
            and "销售模式" in compact
        )
    ):
        return line.strip()
    return ""


def _section_name(line: str) -> str:
    compact = _compact(line)
    if compact in {"分行业", "分产品", "分地区", "分销售模式"}:
        return compact
    if compact.startswith("主营业务分行业"):
        return "分行业"
    if compact.startswith("主营业务分产品"):
        return "分产品"
    if compact.startswith("主营业务分地区"):
        return "分地区"
    if compact.startswith("主营业务分销售模式"):
        return "分销售模式"
    return ""


def _clean_page_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.match(r"^\d+\s*/\s*\d+$", line):
            continue
        if re.match(r"^第\s*\d+\s*页$", line):
            continue
        if re.match(r"^\d{1,4}$", line):
            continue
        if "年年度报告" in line and len(line) <= 80:
            continue
        out.append(line)
    return out


def _parse_row(text: str, excerpt: str) -> Row | None:
    scan = _strip_leading_numbering(text)
    scan = _strip_note_refs(scan)
    tokens = _scan_numbers(scan)
    if not tokens:
        return None
    # 去掉年份列头，如 “2025 年 2024 年”
    tokens = [t for t in tokens if not t.is_year]
    if not tokens:
        return None
    first = tokens[0]
    raw_label = scan[: first.start].strip(" ：:、.．")
    if not raw_label or len(raw_label) > 90:
        return None
    if "。" in raw_label or "，" in raw_label:
        return None
    label = _strip_label_prefix(raw_label)
    if not label:
        return None
    return Row(
        page=PageRecord("", "", REPORT_YEAR, 0, "", [], "", False, False, True),
        raw_label=raw_label,
        label=label,
        numbers=tokens,
        excerpt=excerpt.strip(),
    )


# ---------------------------------------------------------------------------
# 页面与行构建
# ---------------------------------------------------------------------------


def _company_maps() -> tuple[dict[str, str], list[str]]:
    companies = load_companies()
    names = {row["证券代码"]: row["公司简称"] for row in companies}
    return names, [row["证券代码"] for row in companies]


def _scope_codes(question: str, results: Sequence[Mapping]) -> list[str]:
    """确定本题的合法公司范围，防止跨公司结果混入单公司答案。"""
    if ALL_PATTERN.search(_compact(question)):
        # “全部公司”题的合法范围是公司名单全集，不是本次检索碰巧召回的子集；
        # 否则“未提取公司”会被范围过滤掩盖，且同一代码会按结果条数重复。
        return [row["证券代码"] for row in load_companies()]
    detected = detect_companies(question)
    if detected:
        return detected
    # 兼容纯内存测试：题面没有命中公司名单时，沿用检索结果的题面识别范围。
    for result in results:
        scope = result.get("检索范围") or {}
        names = scope.get("题面识别公司") or []
        if names:
            names_map, _ = _company_maps()
            by_name = {v: k for k, v in names_map.items()}
            codes = [by_name[n] for n in names if n in by_name]
            if codes:
                return codes
    return []


def _filter_results(results: Sequence[Mapping], scope: Sequence[str]) -> list[Mapping]:
    if not scope:
        return list(results)
    wanted = set(scope)
    return [r for r in results if r.get("证券代码") in wanted]


def _page_header_state(result: Mapping) -> tuple[bool, int | None, str]:
    info = result.get("跨页表头") or {}
    source = str(info.get("表头来源", ""))
    reliability = str(info.get("关联可靠性", ""))
    header_page = info.get("表头页")
    pending = False
    if source in {"上下文", "主结果"} and "待人工核验" in reliability:
        pending = True
    elif source == "未找到":
        pending = True
    elif source == "上下文":
        pending = True
    if source == "未找到":
        header_page = None
    return pending, (int(header_page) if header_page else None), reliability


def _build_pages(results: Sequence[Mapping]) -> list[PageRecord]:
    names, order = _company_maps()
    order_index = {code: i for i, code in enumerate(order)}
    pages: dict[tuple[str, int], PageRecord] = {}

    # 先放所有主结果页，再放上下文片段。否则某个较早结果的上下文页
    # 可能抢占同一页的主结果，导致 blocked 状态被错误替换。
    for result in results:
        code = str(result.get("证券代码", ""))
        page = int(result.get("页码") or 0)
        key = (code, page)
        if key in pages:
            continue
        pending, header_page, reliability = _page_header_state(result)
        pages[key] = PageRecord(
            code=code,
            company=str(result.get("公司简称") or names.get(code, code)),
            year=int(result.get("报告年份") or REPORT_YEAR),
            page=page,
            doc_id=str(result.get("doc_id", "")),
            lines=_merge_broken_labels(_clean_page_lines(str(result.get("正文", "")))),
            unit=_detect_unit_from_result(result),
            is_context=False,
            blocked=bool(result.get("blocked")),
            reliable=bool(result.get("reliable_for_deterministic_answer", True)),
            header_page=header_page,
            context_status=reliability if pending else "",
            needs_header_verification=pending,
            result=dict(result),
        )
    for result in results:
        code = str(result.get("证券代码", ""))
        for context in result.get("上下文片段") or []:
            cpage = int(context.get("页码") or 0)
            ckey = (code, cpage)
            if ckey in pages:
                continue
            cstatus = str(context.get("上下文关联状态", ""))
            pages[ckey] = PageRecord(
                code=code,
                company=str(context.get("公司简称") or names.get(code, code)),
                year=int(context.get("报告年份") or result.get("报告年份") or REPORT_YEAR),
                page=cpage,
                doc_id=str(context.get("doc_id", "")),
                lines=_merge_broken_labels(_clean_page_lines(str(context.get("正文", "")))),
                unit=_detect_unit(str(context.get("正文", ""))),
                is_context=True,
                blocked=bool(context.get("blocked")),
                reliable=False,
                context_status=cstatus or "算法判定，待人工核验",
                needs_header_verification=True,
                result=dict(context),
            )

    ordered = sorted(
        pages.values(),
        key=lambda p: (order_index.get(p.code, 999), p.page),
    )
    return ordered


def _build_rows(pages: Sequence[PageRecord]) -> list[Row]:
    rows: list[Row] = []
    by_code: dict[str, list[PageRecord]] = defaultdict(list)
    for page in pages:
        by_code[page.code].append(page)

    for code, company_pages in by_code.items():
        company_pages.sort(key=lambda p: p.page)
        carry_title = ""
        carry_section = ""
        prev_page: PageRecord | None = None
        for page in company_pages:
            adjacent = prev_page is not None and page.page == prev_page.page + 1
            title = carry_title if adjacent else ""
            section = carry_section if adjacent else ""
            title_local = False
            for text, excerpt in page.lines:
                found_title = _table_title(text)
                if found_title:
                    title = found_title
                    title_local = True
                    found_section = _section_name(found_title)
                    if found_section:
                        section = found_section
                    continue
                found_section = _section_name(text)
                if found_section:
                    section = found_section
                    if not title:
                        title = text.strip()
                        title_local = True
                    continue
                parsed = _parse_row(text, excerpt)
                if parsed is None:
                    continue
                parsed.page = page
                parsed.table_title = title
                parsed.section = section
                parsed.title_local = title_local
                parsed.needs_header_verification = (
                    page.needs_header_verification and not title_local
                )
                rows.append(parsed)
            carry_title = title
            carry_section = section
            prev_page = page
    return rows


# ---------------------------------------------------------------------------
# 指标匹配与抽取
# ---------------------------------------------------------------------------


def _label_matches(label: str, term: str, *, prefix: bool = False) -> bool:
    left = _compact(label)
    right = _compact(term)
    if prefix:
        return left.startswith(right)
    return left == right


def _rows_for_term(
    rows: Sequence[Row],
    term: str,
    *,
    prefix: bool = False,
    section: str = "",
) -> list[Row]:
    return [
        row
        for row in rows
        if (not section or row.section == section)
        and _label_matches(row.label, term, prefix=prefix)
    ]


def _choose_row(
    rows: Sequence[Row],
    preferred_titles: Sequence[str] = (),
    *,
    near_page: int | None = None,
) -> Row | None:
    if not rows:
        return None

    def score(row: Row) -> tuple:
        title = _compact(row.table_title)
        rank = len(preferred_titles)
        for index, term in enumerate(preferred_titles):
            if _compact(term) in title:
                rank = index
                break
        proximity = abs(row.page.page - near_page) if near_page is not None else 0
        return (
            bool(row.page.blocked or not row.page.reliable),
            proximity,
            rank,
            row.needs_header_verification,
            row.page.page,
        )

    return sorted(rows, key=score)[0]


def _row_unit(row: Row) -> str:
    if row.unit:
        return row.unit
    match = re.search(r"（(千元|万元|亿元|元)）", row.label)
    return match.group(1) if match else "未标注"


def _page_safety_kwargs(page: PageRecord) -> dict:
    unsafe = page.blocked or not page.reliable
    return {
        "blocked": unsafe,
        "adopted": not unsafe,
        "reliable": page.reliable,
        "context_status": page.context_status,
        "needs_header_verification": page.needs_header_verification,
        "header_page": (
            page.header_page if page.needs_header_verification else None
        ),
    }


def _evidence_from_row(
    row: Row,
    *,
    metric: str = "",
    value: str | None = None,
    unit: str | None = None,
    source_type: str = "原文值",
    definition_note: str = "",
    adopted: bool = True,
    calculation: str = "",
    numeric: Decimal | None = None,
) -> Evidence:
    token = row.first_amount()
    if value is None:
        value = token.text if token else ""
    if numeric is None:
        numeric = token.value if token else None
    return Evidence(
        company=row.company,
        code=row.code,
        year=row.year,
        metric=metric or row.raw_label,
        value=value,
        unit=(unit if unit is not None else _row_unit(row)),
        page=row.page.page,
        table_title=row.table_title,
        excerpt=row.excerpt,
        source_type=source_type,
        definition_note=definition_note,
        header_page=(
            row.page.header_page
            if (row.needs_header_verification or row.page.is_context)
            else None
        ),
        blocked=row.page.blocked or not row.page.reliable,
        adopted=adopted and not row.page.blocked and row.page.reliable,
        context_status=row.page.context_status,
        reliable=row.page.reliable,
        needs_header_verification=row.needs_header_verification,
        doc_id=row.page.doc_id,
        calculation=calculation,
        numeric=numeric,
    )


def _evidence_for_ratio(
    base: Evidence,
    ratio: Decimal,
    *,
    metric: str,
    definition_note: str,
    calculation: str,
    adopted: bool = True,
    value_text: str | None = None,
) -> Evidence:
    return Evidence(
        company=base.company,
        code=base.code,
        year=base.year,
        metric=metric,
        value=value_text or _format_pct(ratio),
        unit="%",
        page=base.page,
        table_title=base.table_title,
        excerpt=base.excerpt,
        source_type="计算值",
        definition_note=definition_note,
        header_page=base.header_page,
        blocked=base.blocked,
        adopted=adopted and not base.blocked and base.reliable,
        context_status=base.context_status,
        reliable=base.reliable,
        needs_header_verification=base.needs_header_verification,
        doc_id=base.doc_id,
        calculation=calculation,
        numeric=ratio,
    )


def _find_exact_revenue(
    rows: Sequence[Row], *, near_page: int | None = None
) -> Row | None:
    candidates = _rows_for_term(rows, "营业收入")
    candidates = [r for r in candidates if not _compact(r.label).startswith("营业总收入")]
    if not candidates:
        return None
    return _choose_row(
        candidates,
        ("合并及公司利润表", "合并利润表", "主要会计数据", "营业收入构成"),
        near_page=near_page,
    )


def _find_total_revenue(rows: Sequence[Row]) -> Row | None:
    return _choose_row(
        _rows_for_term(rows, "营业总收入"),
        ("合并及公司利润表", "合并利润表", "主要会计数据"),
    )


def _find_parent_net_profit(rows: Sequence[Row]) -> Row | None:
    candidates = [
        row
        for row in rows
        if _label_matches(row.label, "归属于母公司股东的净利润", prefix=True)
        or _label_matches(row.label, "归属于上市公司股东的净利润", prefix=True)
    ]
    return _choose_row(
        candidates,
        ("合并及公司利润表", "合并利润表", "主要会计数据"),
    )


def _find_rd_amount(rows: Sequence[Row]) -> Row | None:
    return _choose_row(_rows_for_term(rows, "研发投入合计"))


def _find_rd_ratio(rows: Sequence[Row]) -> Row | None:
    candidates = _rows_for_term(rows, "研发投入总额占营业收入比", prefix=True)
    return _choose_row(candidates)


def _find_operating_revenue_for_ratio(
    rows: Sequence[Row], *, near_page: int | None = None
) -> Row | None:
    return _find_exact_revenue(rows, near_page=near_page)


def _find_labeled_row(
    rows: Sequence[Row],
    labels: Sequence[str],
    *,
    section: str = "",
) -> Row | None:
    for label in labels:
        found = _rows_for_term(rows, label, section=section)
        if found:
            return found[0]
    return None


def _region_groups(rows: Sequence[Row]) -> dict[str, list[Row]]:
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if row.section != "分地区":
            continue
        if not (
            row.label in REGION_FOREIGN_LABELS or row.label in REGION_DOMESTIC_LABELS
        ):
            continue
        groups[_compact(row.table_title) or f"__page_{row.page.page}"].append(row)
    return groups


def _preferred_region_group(groups: Mapping[str, Sequence[Row]]) -> tuple[str, list[Row]] | None:
    if not groups:
        return None

    def score(item: tuple[str, Sequence[Row]]) -> tuple:
        key, rows = item
        title = rows[0].table_title if rows else key
        compact = _compact(title)
        rank = 2
        if "营业收入构成" in compact:
            rank = 0
        elif "10%以上" not in compact:
            rank = 1
        return (rank, rows[0].page.page)

    key, rows = sorted(groups.items(), key=score)[0]
    return key, list(rows)


def _region_total(rows: Sequence[Row]) -> Decimal | None:
    total = Decimal("0")
    seen = set()
    for row in rows:
        if row.section != "分地区":
            continue
        if not (
            row.label in REGION_FOREIGN_LABELS or row.label in REGION_DOMESTIC_LABELS
        ):
            continue
        if row.label in seen:
            continue
        token = row.first_amount()
        if token is None:
            continue
        total += token.value
        seen.add(row.label)
    return total if total else None


def _region_ratio(row: Row, region_rows: Sequence[Row]) -> tuple[Decimal, str] | None:
    disclosed = row.disclosed_ratio()
    if disclosed:
        return disclosed[0], "披露值"
    total = _region_total(region_rows)
    token = row.first_amount()
    if total is None or token is None:
        return None
    ratio = _ratio_percent(token.value, total)
    if ratio is None:
        return None
    return ratio, "计算值"


def _table_search_text(pages: Sequence[PageRecord]) -> str:
    return "\n".join(text for page in pages for text, _ in page.lines)


# ---------------------------------------------------------------------------
# 问题意图
# ---------------------------------------------------------------------------


def _intent(question: str) -> dict:
    q = _norm_question(question)
    all_companies = bool(ALL_PATTERN.search(q))
    return {
        "all_companies": all_companies,
        "ranking": any(term in q for term in ("最高", "前三", "排名", "排序", "从高到低", "居前")),
        "dividend": any(term in q for term in ("利润分配", "每10股", "现金红利", "分红")),
        "rd": "研发" in q,
        "region": any(term in q for term in ("境内", "境外", "海外", "国外", "出口", "外销")),
        "channel": any(term in q for term in ("线上销售", "线上渠道", "销售渠道")),
        "product_structure": any(
            term in q for term in ("产品品类", "产品构成", "主要产品", "分产品", "品类")
        ),
        "air_conditioner": "空调" in q,
        "gross_margin": "毛利率" in q,
        "total_revenue": "营业总收入" in q,
        "net_profit": any(term in q for term in ("归母净利润", "归属于母公司", "净利润")),
        "revenue": "营业收入" in q,
        "yoy": any(term in q for term in ("同比", "变动")),
    }


# ---------------------------------------------------------------------------
# 各问题类型的确定性回答
# ---------------------------------------------------------------------------


def _base_answer(
    question: str,
    question_id: str,
    status: str,
    text: str,
    metrics: Sequence[Evidence],
    scope: Sequence[str],
    *,
    notes: Sequence[str] = (),
    warnings: Sequence[str] = (),
    refusals: Sequence[str] = (),
    deterministic: bool = False,
    audit: Mapping | None = None,
) -> dict:
    metric_list = list(metrics)
    note_list = list(notes)
    warning_list = list(warnings)
    refusal_list = list(refusals)
    # 统一的安全闸门：只要答案里出现 blocked/不可靠片段，且原本想给
    # “可回答 / 需口径说明 / 证据冲突”，就降级为“已阻断”，不得把其中
    # 数值当作已确认事实。
    if status in {
        STATUS_ANSWERABLE,
        STATUS_NEEDS_DEFINITION,
        STATUS_CONFLICT,
    } and any(metric.blocked for metric in metric_list):
        status = STATUS_BLOCKED
        deterministic = False
        warning_list.append(
            "答案涉及 blocked 或 reliable_for_deterministic_answer=false 的片段，"
            "已自动降级为“已阻断”；相关数值只能作为候选。"
        )
        refusal_list.append(
            "所依赖的检索片段被阻断，不能作为确定性答案。"
        )
    answer = Answer(
        question_id=question_id,
        question=question,
        status=status,
        answer_text=text,
        metrics=metric_list,
        scope_codes=list(scope),
        notes=note_list,
        warnings=warning_list,
        refusal_reasons=refusal_list,
        deterministic=deterministic,
        audit=dict(audit or {}),
    )
    return answer.to_dict()


def _answer_dividend(
    question: str, question_id: str, pages: Sequence[PageRecord], scope: Sequence[str]
) -> dict:
    per_share: tuple[Decimal, PageRecord, str] | None = None
    base: tuple[Decimal, PageRecord, str] | None = None
    total: tuple[Decimal, PageRecord, str] | None = None
    approval_page: PageRecord | None = None
    send_share: tuple[Decimal, PageRecord, str] | None = None
    no_capital_reserve = False

    for page in pages:
        text = "".join(line for line, _ in page.lines)
        compact = _compact(text)
        if any(
            term in compact
            for term in ("利润分配预案", "利润分配预案的议案", "现金分红")
        ):
            match = re.search(r"每10股派发现金(?:红利|股利)([0-9.]+)元", compact)
            if match is None:
                match = re.search(r"每10股派([0-9.]+)元", compact)
            if match and per_share is None:
                value = _parse_decimal(match.group(1))
                if value is not None:
                    per_share = (value, page, match.group(0))
            match = re.search(r"(?:以|股本)([0-9,]+)股为基数", compact)
            if match and base is None:
                value = _parse_decimal(match.group(1))
                if value is not None:
                    base = (value, page, match.group(0))
            match = re.search(r"送红股([0-9,]+)股", compact)
            if match and send_share is None:
                value = _parse_decimal(match.group(1))
                if value is not None:
                    send_share = (value, page, match.group(0))
            if "不以公积金转增股本" in compact:
                no_capital_reserve = True
        match = re.search(r"现金分红金额（元）（含税）([0-9,]+\.[0-9]{2})", compact)
        if match is None:
            match = re.search(
                r"现金分红总额（含其他方式）（元）([0-9,]+\.[0-9]{2})", compact
            )
        if match is None:
            match = re.search(r"共计([0-9,]+\.[0-9]{2})元", compact)
        if match and total is None:
            value = _parse_decimal(match.group(1))
            if value is not None:
                total = (value, page, match.group(0))
        if "股东大会审议通过" in compact and "尚需" in compact:
            approval_page = page

    metrics: list[Evidence] = []
    if per_share:
        value, page, excerpt = per_share
        metrics.append(
            Evidence(
                company=page.company,
                code=page.code,
                year=page.year,
                metric="每10股派发现金红利",
                value=_format_decimal(value),
                unit="元/10股",
                page=page.page,
                table_title="利润分配预案",
                excerpt=excerpt,
                definition_note="含税；按每10股口径。",
                doc_id=page.doc_id,
                numeric=value,
                **_page_safety_kwargs(page),
            )
        )
    if base:
        value, page, excerpt = base
        metrics.append(
            Evidence(
                company=page.company,
                code=page.code,
                year=page.year,
                metric="利润分配预案的股本基数",
                value=_format_decimal(value),
                unit="股",
                page=page.page,
                table_title="利润分配预案",
                excerpt=excerpt,
                doc_id=page.doc_id,
                numeric=value,
                **_page_safety_kwargs(page),
            )
        )
    if total:
        value, page, excerpt = total
        metrics.append(
            Evidence(
                company=page.company,
                code=page.code,
                year=page.year,
                metric="现金分红总额",
                value=f"{value:,.2f}",
                unit="元",
                page=page.page,
                table_title="利润分配预案",
                excerpt=excerpt,
                definition_note="含税；含其他方式现金分红。",
                doc_id=page.doc_id,
                numeric=value,
                **_page_safety_kwargs(page),
            )
        )
    if send_share:
        value, page, excerpt = send_share
        metrics.append(
            Evidence(
                company=page.company,
                code=page.code,
                year=page.year,
                metric="送红股",
                value=_format_decimal(value),
                unit="股/10股",
                page=page.page,
                table_title="利润分配预案",
                excerpt=excerpt,
                doc_id=page.doc_id,
                numeric=value,
                **_page_safety_kwargs(page),
            )
        )

    if not (per_share and base):
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            metrics,
            scope,
            refusals=["未同时抽取到每10股现金红利和股本基数。"],
            audit={"已抽取字段": [m.to_dict() for m in metrics]},
        )

    year = per_share[1].year
    parts = [
        f"以 {_format_decimal(base[0])} 股为基数，向全体股东每 10 股派发现金红利 "
        f"{_format_decimal(per_share[0])} 元（含税）"
    ]
    if send_share:
        parts.append(f"送红股 {_format_decimal(send_share[0])} 股")
    if no_capital_reserve:
        parts.append("不以公积金转增股本")
    sentence = "；".join(parts) + "。"
    if total:
        sentence += f"现金分红总额 {total[0]:,.2f} 元（含税）。"
    if approval_page:
        sentence += f"该议案尚需提交公司 {year} 年度股东大会审议通过。"
    text = f"{year} 年度利润分配预案：" + sentence
    notes = ["预案口径，含税；尚未实施。"]
    warnings: list[str] = []
    if approval_page:
        warnings.append("该议案尚需股东大会审议，不能表述为已实施分红。")
    else:
        warnings.append("本次检索结果未确认股东大会审议状态。")
    audit = {
        "每10股现金红利": per_share[2] if per_share else "",
        "股本基数": base[2] if base else "",
        "现金分红总额": total[2] if total else "",
        "审议状态页": approval_page.page if approval_page else None,
        "证据来源": [m.to_dict() for m in metrics],
    }
    return _base_answer(
        question,
        question_id,
        STATUS_ANSWERABLE,
        text,
        metrics,
        scope,
        notes=notes,
        warnings=warnings,
        deterministic=True,
        audit=audit,
    )


def _answer_rd(
    question: str, question_id: str, pages: Sequence[PageRecord], scope: Sequence[str]
) -> dict:
    rows = _build_rows(pages)
    amount_row = _find_rd_amount(rows)
    ratio_row = _find_rd_ratio(rows)
    revenue_row = _find_operating_revenue_for_ratio(
        rows, near_page=ratio_row.page.page if ratio_row else None
    )
    if amount_row is None or ratio_row is None:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [],
            scope,
            refusals=["未同时找到“研发投入合计”和“研发投入总额占营业收入比例”。"],
        )

    amount_ev = _evidence_from_row(amount_row, metric="研发投入合计")
    ratio_token = ratio_row.first_amount() or (
        ratio_row.numbers[0] if ratio_row.numbers else None
    )
    ratio_value = ratio_token.value if ratio_token else None
    if ratio_value is None:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [amount_ev],
            scope,
            refusals=["研发投入占比行存在，但未解析出百分比数值。"],
        )
    ratio_ev = _evidence_from_row(
        ratio_row,
        metric=ratio_row.raw_label,
        value=_format_pct(ratio_value),
        unit="%",
    )
    metrics = [amount_ev, ratio_ev]
    notes = []
    warnings = []
    if revenue_row is not None and amount_ev.numeric is not None:
        revenue_token = revenue_row.first_amount()
        if revenue_token:
            computed = _ratio_percent(amount_ev.numeric, revenue_token.value)
            if computed is not None:
                notes.append(
                    f"按营业收入 {revenue_token.text} {revenue_row.unit} 复算，"
                    f"研发投入占比为 {_format_pct(computed)}，与披露值 "
                    f"{_format_pct(ratio_value)} 一致。"
                )
                metrics.append(
                    Evidence(
                        company=revenue_row.company,
                        code=revenue_row.code,
                        year=revenue_row.year,
                        metric="营业收入",
                        value=revenue_token.text,
                        unit=_row_unit(revenue_row),
                        page=revenue_row.page.page,
                        table_title=revenue_row.table_title,
                        excerpt=revenue_row.excerpt,
                        definition_note="仅作为研发投入占比的复算分母。",
                        doc_id=revenue_row.page.doc_id,
                        numeric=revenue_token.value,
                        **_page_safety_kwargs(revenue_row.page),
                    )
                )
    text = (
        f"{amount_ev.company}（{amount_ev.code}）{amount_ev.year} 年研发投入合计 "
        f"{amount_ev.value} {amount_ev.unit}（p{amount_ev.page}）；"
        f"研发投入总额占营业收入比例 {_format_pct(ratio_value)}"
        f"（{ratio_row.raw_label}，p{ratio_ev.page}，年报披露值）。"
    )
    if revenue_row is not None:
        token = revenue_row.first_amount()
        if token:
            text += f"营业收入为 {token.text} {revenue_row.unit}（p{revenue_row.page.page}）。"
    notes.append(
        "采用“研发投入合计”和“研发投入总额占营业收入比例”，未把“研发费用”"
        "自动替换为“研发投入”。"
    )
    # 如果同页存在“研发费用”且数值相同，只作为口径提示，不作为独立结论。
    for row in rows:
        if _label_matches(row.label, "研发费用") and row.first_amount():
            if amount_ev.numeric is not None and row.first_amount().value == amount_ev.numeric:
                notes.append(
                    f"同页“研发费用”为 {row.first_amount().text}，与研发投入合计相同；"
                    "本次仅作为口径核对，未用它替代研发投入。"
                )
                break
    blocked_pages = sorted({p.page for p in pages if p.blocked})
    if blocked_pages:
        warnings.append(
            "检索结果含 blocked 页面（"
            + "、".join(f"p{p}" for p in blocked_pages)
            + "），本题所需研发数据未取自这些页面。"
        )
    return _base_answer(
        question,
        question_id,
        STATUS_ANSWERABLE,
        text,
        metrics,
        scope,
        notes=notes,
        warnings=warnings,
        deterministic=True,
        audit={
            "研发投入行": amount_row.excerpt,
            "研发投入占比行": ratio_row.excerpt,
            "复算分母": revenue_row.excerpt if revenue_row else "",
            "blocked页面": blocked_pages,
        },
    )


def _answer_region_single(
    question: str, question_id: str, pages: Sequence[PageRecord], scope: Sequence[str]
) -> dict:
    rows = _build_rows(pages)
    groups = _region_groups(rows)
    preferred = _preferred_region_group(groups)
    if preferred is None:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [],
            scope,
            refusals=["未找到分地区表中的境内、境外收入行。"],
        )
    _, region_rows = preferred
    domestic = _find_labeled_row(region_rows, REGION_DOMESTIC_LABELS)
    foreign = _find_labeled_row(region_rows, REGION_FOREIGN_LABELS)
    if domestic is None or foreign is None:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [],
            scope,
            refusals=["分地区表缺少境内或境外收入行，无法同时给出两项及占比。"],
            audit={"地区行": [r.excerpt for r in region_rows]},
        )
    total = _region_total(region_rows)
    if total is None:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [],
            scope,
            refusals=["分地区行无法求和，不能计算占比。"],
        )
    dom_ev = _evidence_from_row(domestic, metric=domestic.raw_label)
    for_ev = _evidence_from_row(foreign, metric=foreign.raw_label)
    metrics = [dom_ev, for_ev]
    dom_ratio = _ratio_percent(dom_ev.numeric, total) if dom_ev.numeric else None
    for_ratio = _ratio_percent(for_ev.numeric, total) if for_ev.numeric else None
    if dom_ratio is None or for_ratio is None:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            metrics,
            scope,
            refusals=["占比计算失败。"],
        )
    definition = f"分母为同表分地区行合计 {_format_decimal(total)} {domestic.unit}（计算值）。"
    metrics.append(
        _evidence_for_ratio(
            dom_ev,
            dom_ratio,
            metric="境内收入占比",
            definition_note=definition,
            calculation=f"{dom_ev.value} / {_format_decimal(total)}",
        )
    )
    metrics.append(
        _evidence_for_ratio(
            for_ev,
            for_ratio,
            metric="境外收入占比",
            definition_note=definition,
            calculation=f"{for_ev.value} / {_format_decimal(total)}",
        )
    )
    text = (
        f"{dom_ev.company}（{dom_ev.code}）{dom_ev.year} 年：\n"
        f"- 境内收入（年报原文指标：{domestic.raw_label}）"
        f"{dom_ev.value} {dom_ev.unit}（p{dom_ev.page}）。\n"
        f"- 境外收入（年报原文指标：{foreign.raw_label}）"
        f"{for_ev.value} {for_ev.unit}（p{for_ev.page}）。\n"
        f"- 分地区合计 {_format_decimal(total)} {domestic.unit}；"
        f"境内占比 {_format_pct(dom_ratio)}，境外占比 {_format_pct(for_ratio)}"
        "（计算值）。"
    )
    notes = [
        f"题面使用“境内/境外”，年报原文使用“{domestic.raw_label}/{foreign.raw_label}”；"
        "本答案保留原文指标名并作对应，不改写原文。",
        definition,
    ]
    # 如果同公司另有营业收入（不是营业总收入），给出替代分母，防止把两个分母混用。
    revenue_row = _find_exact_revenue(rows)
    if revenue_row is not None and revenue_row.first_amount():
        rev = revenue_row.first_amount()
        alt_dom = _ratio_percent(dom_ev.numeric, rev.value) if dom_ev.numeric else None
        alt_for = _ratio_percent(for_ev.numeric, rev.value) if for_ev.numeric else None
        if alt_dom is not None and alt_for is not None:
            alternative = (
                f"若改用营业收入 {rev.text} {revenue_row.unit}（p{revenue_row.page.page}）"
                f"作分母，境内/境外占比约为 {_format_pct(alt_dom)}/{_format_pct(alt_for)}；"
                "该分母与分地区表合计不同，本答案不把它作为主结论。"
            )
            notes.append(alternative)
            text += "\n口径提示：" + alternative
    return _base_answer(
        question,
        question_id,
        STATUS_NEEDS_DEFINITION,
        text,
        metrics,
        scope,
        notes=notes,
        warnings=["占比为计算值；题面指标名与年报原文指标名不完全一致。"],
        deterministic=True,
        audit={
            "表名": domestic.table_title,
            "地区行": [r.excerpt for r in region_rows],
            "分母": _format_decimal(total),
            "境内占比": _format_pct(dom_ratio),
            "境外占比": _format_pct(for_ratio),
        },
    )


def _channel_table_rows(row: Row, rows: Sequence[Row]) -> list[Row]:
    title = _compact(row.table_title)
    return [
        candidate
        for candidate in rows
        if _compact(candidate.table_title) == title
        and candidate.code == row.code
        and candidate.page.year == row.page.year
    ]


def _channel_total_row(row: Row, rows: Sequence[Row]) -> Row | None:
    for candidate in _channel_table_rows(row, rows):
        if _label_matches(candidate.label, "营业收入合计"):
            return candidate
    return None


def _channel_sum(row: Row, rows: Sequence[Row]) -> Decimal | None:
    total = Decimal("0")
    seen: set[str] = set()
    for candidate in _channel_table_rows(row, rows):
        if candidate.section != "分销售模式" or not candidate.first_amount():
            continue
        label = _compact(candidate.label)
        if label in seen:
            continue
        total += candidate.first_amount().value
        seen.add(label)
    return total if seen else None


def _answer_channel(
    question: str, question_id: str, pages: Sequence[PageRecord], scope: Sequence[str]
) -> dict:
    rows = _build_rows(pages)
    candidates = _rows_for_term(rows, "线上销售", section="分销售模式")
    if not candidates:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [],
            scope,
            refusals=["未找到“分销售模式”下的“线上销售”行。"],
        )
    # 优先使用带“营业收入构成”标题的明细表；该表同时披露占比。
    row = _choose_row(candidates, ("营业收入构成",))
    amount_ev = _evidence_from_row(
        row, metric=row.raw_label, adopted=not row.needs_header_verification
    )
    total_row = _channel_total_row(row, rows)
    total_value = None
    if total_row and total_row.first_amount():
        total_value = total_row.first_amount().value
    if total_value is None:
        total_value = _channel_sum(row, rows)
    ratio = row.disclosed_ratio()
    if ratio is not None:
        ratio = (ratio[0], "披露值")
    if ratio is None and total_value and amount_ev.numeric is not None:
        computed = _ratio_percent(amount_ev.numeric, total_value)
        if computed is not None:
            ratio = (computed, "计算值")
    if ratio is None:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [amount_ev],
            scope,
            refusals=["找到线上销售收入，但未找到收入占比或可复算分母。"],
        )
    ratio_value, ratio_source = ratio
    denominator_note = (
        "分母为同表“营业收入合计”"
        if total_row is not None
        else "分母为同表线上销售与线下销售合计"
    )
    ratio_ev = _evidence_for_ratio(
        amount_ev,
        ratio_value,
        metric="线上销售收入占营业收入比重",
        definition_note=denominator_note + "；占比列语义依赖表头。",
        calculation=(
            "原文披露占比列"
            if ratio_source == "披露值"
            else "线上销售收入 / " + denominator_note
        ),
        adopted=not row.needs_header_verification,
        value_text=_format_pct(ratio_value) if ratio_source == "计算值" else None,
    )
    metrics = [amount_ev]
    if total_row is not None:
        metrics.append(
            _evidence_from_row(
                total_row,
                metric=total_row.raw_label,
                definition_note="线上销售占比的分母。",
                adopted=not row.needs_header_verification,
            )
        )
    metrics.append(ratio_ev)
    text = (
        f"{amount_ev.company}（{amount_ev.code}）{amount_ev.year} 年线上销售（年报原文指标："
        f"{row.raw_label}）营业收入 {amount_ev.value} {amount_ev.unit}"
        f"（p{amount_ev.page}），占营业收入比重 {_format_pct(ratio_value)}"
        f"（{ratio_source}）。"
    )
    if total_value is not None:
        text += (
            f"同表营业收入合计 {_format_decimal(total_value)} "
            f"{_row_unit(total_row) if total_row is not None else amount_ev.unit}"
            f"（p{total_row.page.page if total_row is not None else amount_ev.page}）。"
        )
    notes = [
        "题面问“线上销售渠道”，年报原文指标为“线上销售”。",
        denominator_note + "。",
    ]
    warnings = []
    deterministic = True
    status = STATUS_ANSWERABLE
    if row.needs_header_verification:
        status = STATUS_NEEDS_VERIFICATION
        deterministic = False
        warnings.append(
            "该行所在页从数据行开始，表头由算法从相邻页关联；当前跨页状态为"
            f"“{row.page.context_status or '算法判定，待人工核验'}”，"
            "不得标记为人工已确认；金额与占比只能作为候选。"
        )
        text += (
            "\n需核验：上述占比所依赖的表头尚未经人工语义确认，"
            "因此该数值不能作为确定性结论。"
        )
        notes.append(
            f"数据页 p{row.page.page}，表头来源页 "
            f"{row.page.header_page if row.page.header_page else '未确认'}"
            f"（{row.page.context_status or '算法判定，待人工核验'}）。"
        )
    return _base_answer(
        question,
        question_id,
        status,
        text,
        metrics,
        scope,
        notes=notes,
        warnings=warnings,
        deterministic=deterministic,
        audit={
            "数据行": row.excerpt,
            "数据页": row.page.page,
            "表头来源页": row.page.header_page,
            "上下文关联状态": row.page.context_status,
            "占比来源": ratio_source,
            "营业收入合计": total_row.excerpt if total_row is not None else "",
            "营业收入合计数值": (
                _format_decimal(total_value) if total_value is not None else ""
            ),
            "分母说明": denominator_note,
        },
    )


def _answer_product_structure(
    question: str, question_id: str, pages: Sequence[PageRecord], scope: Sequence[str]
) -> dict:
    rows = _build_rows(pages)
    product_rows = [row for row in rows if row.section == "分产品"]
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in product_rows:
        groups[_compact(row.table_title) or f"__page_{row.page.page}"].append(row)

    usable: list[tuple[str, list[Row]]] = []
    for key, group_rows in groups.items():
        with_amount = [r for r in group_rows if r.first_amount() is not None]
        if with_amount:
            usable.append((key, with_amount))
    if not usable:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [],
            scope,
            refusals=["未找到分产品收入行。"],
        )

    # 优先用“营业收入构成”表；若还有“10%以上”表，则并列比较。
    composition = [
        item for item in usable if "营业收入构成" in _compact(item[1][0].table_title)
    ]
    ten_percent = [
        item for item in usable if "10%以上" in _compact(item[1][0].table_title)
    ]
    if not composition:
        composition = [usable[0]]
    if not ten_percent:
        # 只有一张表时给出该表，但保留口径说明。
        _, group_rows = composition[0]
        metrics = []
        for row in group_rows:
            ev = _evidence_from_row(row, metric=row.raw_label)
            metrics.append(ev)
        text = f"{group_rows[0].company}分产品营业收入：\n" + "\n".join(
            f"- {m.metric}：{m.value} {m.unit}（p{m.page}）" for m in metrics
        )
        return _base_answer(
            question,
            question_id,
            STATUS_ANSWERABLE,
            text,
            metrics,
            scope,
            notes=["仅检索到一张分产品表；未发现第二张可比表。"],
            deterministic=True,
        )

    _, table_a = composition[0]
    _, table_b = ten_percent[0]
    title_a = table_a[0].table_title
    title_b = table_b[0].table_title
    a_by_label = {_compact(r.label): r for r in table_a}
    b_by_label = {_compact(r.label): r for r in table_b}
    shared = sorted(set(a_by_label) & set(b_by_label))
    comparisons: list[dict] = []
    differences: list[dict] = []
    for label in shared:
        a = a_by_label[label].first_amount()
        b = b_by_label[label].first_amount()
        if a is None or b is None:
            continue
        diff = b.value - a.value
        item = {
            "label": a_by_label[label].raw_label,
            "a": a,
            "b": b,
            "diff": diff,
            "a_row": a_by_label[label],
            "b_row": b_by_label[label],
        }
        comparisons.append(item)
        if diff != 0:
            differences.append(item)

    metrics: list[Evidence] = []
    for row in table_a:
        ev = _evidence_from_row(
            row,
            metric=row.raw_label,
            definition_note=f"表A《{title_a}》，数据页 p{row.page.page}。",
            adopted=(
                not row.needs_header_verification
                and not any(
                    _compact(row.label) == _compact(d["label"]) for d in differences
                )
            ),
        )
        metrics.append(ev)
    for row in table_b:
        ev = _evidence_from_row(
            row,
            metric=row.raw_label,
            definition_note=f"表B《{title_b}》，数据页 p{row.page.page}。",
            adopted=(
                not row.needs_header_verification
                and not any(
                    _compact(row.label) == _compact(d["label"]) for d in differences
                )
            ),
        )
        metrics.append(ev)

    pages_a = sorted({r.page.page for r in table_a})
    pages_b = sorted({r.page.page for r in table_b})
    sum_a = sum((r.first_amount().value for r in table_a if r.first_amount()), Decimal("0"))
    sum_b = sum((r.first_amount().value for r in table_b if r.first_amount()), Decimal("0"))

    if differences:
        lines = [
            f"检测到同一报告内两张表对同一产品品类给出不同金额：",
            f"表A《{title_a}》（p{'、'.join(map(str, pages_a))}）与"
            f"表B《{title_b}》（p{'、'.join(map(str, pages_b))}）。",
        ]
        for diff in comparisons:
            marker = "（不同）" if diff["diff"] != 0 else "（相同）"
            lines.append(
                f"- {diff['label']}：表A {diff['a'].text} 元，表B {diff['b'].text} 元，"
                f"差额 {_format_signed(diff['diff'])} 元{marker}。"
            )
        lines.append(
            f"表A分产品合计 {_format_decimal(sum_a)} 元；表B分产品合计 "
            f"{_format_decimal(sum_b)} 元；差额 {_format_signed(sum_b - sum_a)} 元。"
        )
        lines.append(
            "差异已发现、原因未明。本次检索返回的 p"
            + "、".join(map(str, pages_b))
            + " 原文中，“公司主营业务数据统计口径在报告期发生调整”勾选为“不适用”；"
            "在本次检索结果范围内未见可解释上述差额的说明。"
        )
        lines.append(
            "不得选择其中一组作为正确值；需人工回看原始 PDF 并核对统计口径。"
        )
        text = "\n".join(lines)
        notes = [
            "表A列名为“占营业收入比重”；表B披露的是营业收入、营业成本和毛利率，"
            "不把毛利率当作收入占比。",
            "两张表的产品标签相同但金额不一致，本模块不进行取舍或平均。",
        ]
        warnings = [
            "差异已发现、原因未明；不得把任一组数值当作唯一正确答案。",
            "表A的部分续页行依赖上一页表头；表B在本页有独立表头。",
        ]
        return _base_answer(
            question,
            question_id,
            STATUS_CONFLICT,
            text,
            metrics,
            scope,
            notes=notes,
            warnings=warnings,
            refusals=["同一报告内两张表数值冲突，且原因未明。"],
            deterministic=False,
            audit={
                "表A": {
                    "表名": title_a,
                    "页码": pages_a,
                    "合计": _format_decimal(sum_a),
                    "行": [r.excerpt for r in table_a],
                },
                "表B": {
                    "表名": title_b,
                    "页码": pages_b,
                    "合计": _format_decimal(sum_b),
                    "行": [r.excerpt for r in table_b],
                },
                "逐项比较": [
                    {
                        "指标": d["label"],
                        "表A": d["a"].text,
                        "表B": d["b"].text,
                        "差额": _format_signed(d["diff"]),
                        "是否不同": d["diff"] != 0,
                    }
                    for d in comparisons
                ],
                "不同项": [d["label"] for d in differences],
                "合计差额": _format_signed(sum_b - sum_a),
                "原因": "差异已发现、原因未明",
            },
        )

    # 没有差异：按表A回答，同时列出表B作为交叉核对。
    text = f"{table_a[0].company}分产品营业收入（表A《{title_a}》）：\n" + "\n".join(
        f"- {m.metric}：{m.value} {m.unit}（p{m.page}）" for m in metrics if m.table_title == title_a
    )
    return _base_answer(
        question,
        question_id,
        STATUS_ANSWERABLE,
        text,
        metrics,
        scope,
        notes=["两张表金额一致；表B另含营业成本和毛利率。"],
        deterministic=True,
        audit={
            "表A": [r.excerpt for r in table_a],
            "表B": [r.excerpt for r in table_b],
        },
    )


def _answer_product_metric(
    question: str, question_id: str, pages: Sequence[PageRecord], scope: Sequence[str]
) -> dict:
    rows = _build_rows(pages)
    product_rows = [r for r in rows if r.section == "分产品" and r.first_amount()]
    target = ""
    if "空调" in question:
        target = "空调"
    if not target:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [],
            scope,
            refusals=["未识别出要查询的具体产品指标。"],
        )
    exact = [r for r in product_rows if _compact(r.label) == _compact(target)]
    found_labels = []
    for row in product_rows:
        if row.raw_label not in found_labels:
            found_labels.append(row.raw_label)
    if not exact:
        candidates: list[Evidence] = []
        for row in product_rows:
            candidates.append(
                _evidence_from_row(
                    row,
                    metric=row.raw_label,
                    definition_note="分产品表中的其他口径，不能替代题面指标。",
                    adopted=False,
                )
            )
        text = (
            f"{REFUSAL_SENTENCE}\n"
            f"本次检索结果的分产品表未单列“{target}”营业收入、毛利率或占比；"
            f"检测到的分产品指标为：{'、'.join(found_labels) if found_labels else '无'}。"
            f"就本次检索返回的年报页面而言，年报未单独披露“{target}”业务收入与毛利率，"
            "属于披露不足；上述其他口径不能写成“空调收入”，也不能用合并口径替代。"
        )
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            text,
            candidates,
            scope,
            notes=[
                "题面要求独立“空调”指标；检索结果没有与题面完全一致的分产品行。",
                "未进行指标名替换，未把其他分产品口径当作空调。",
            ],
            warnings=["相近口径未采信。"],
            refusals=[
                f"分产品表无“{target}”独立行。",
                "仅有其他合并口径，不能替代。",
            ],
            audit={
                "目标指标": target,
                "检测到的分产品指标": found_labels,
                "分产品行": [r.excerpt for r in product_rows],
            },
        )
    row = _choose_row(exact)
    amount_ev = _evidence_from_row(row, metric=row.raw_label)
    metrics = [amount_ev]
    ratio = row.disclosed_ratio()
    # 毛利率：在“10%以上”表中同一标签行的第三个金额后通常有毛利率。
    gross: NumberToken | None = None
    for candidate in rows:
        if (
            candidate.section == "分产品"
            and _compact(candidate.label) == _compact(target)
            and "10%以上" in _compact(candidate.table_title)
        ):
            amounts = candidate.amounts()
            if len(candidate.numbers) >= 3:
                maybe = candidate.numbers[2]
                if maybe.is_percent:
                    gross = maybe
                    metrics.append(
                        _evidence_from_row(
                            candidate,
                            metric=f"{candidate.raw_label}毛利率",
                            value=maybe.text,
                            unit="%",
                        )
                    )
                    break
    text = (
        f"{amount_ev.company}（{amount_ev.code}）{amount_ev.year} 年{target}营业收入 "
        f"{amount_ev.value} {amount_ev.unit}（p{amount_ev.page}）。"
    )
    if ratio:
        ratio_ev = _evidence_for_ratio(
            amount_ev,
            ratio[0],
            metric=f"{target}收入占营业收入比重",
            definition_note="原文披露的占比列。",
            calculation="原文披露占比列",
        )
        metrics.append(ratio_ev)
        text += f"占营业收入比重 {_format_pct(ratio[0])}。"
    if gross:
        text += f"毛利率 {gross.text}。"
    if not ratio or not gross:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            metrics,
            scope,
            refusals=["找到空调收入行，但缺少占比或毛利率。"],
            audit={"命中行": row.excerpt},
        )
    return _base_answer(
        question,
        question_id,
        STATUS_ANSWERABLE,
        text,
        metrics,
        scope,
        notes=["保留年报原文指标名。"],
        deterministic=True,
        audit={"命中行": row.excerpt},
    )


def _answer_financial_single(
    question: str, question_id: str, pages: Sequence[PageRecord], scope: Sequence[str]
) -> dict:
    rows = _build_rows(pages)
    intent = _intent(question)
    total_row = _find_total_revenue(rows) if intent["total_revenue"] else None
    revenue_row = _find_exact_revenue(rows) if intent["revenue"] and not total_row else None
    net_row = _find_parent_net_profit(rows) if intent["net_profit"] else None
    if total_row is None and revenue_row is None:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [],
            scope,
            refusals=["未找到营业总收入或营业收入行。"],
        )
    chosen_revenue = total_row or revenue_row
    assert chosen_revenue is not None
    metrics: list[Evidence] = []
    lines: list[str] = []
    notes: list[str] = []
    name = chosen_revenue.company
    code = chosen_revenue.code
    if total_row is not None:
        ev = _evidence_from_row(total_row, metric=total_row.raw_label)
        metrics.append(ev)
        prior = total_row.second_amount()
        if prior:
            yoy = _ratio_percent(ev.numeric - prior.value, prior.value) if ev.numeric else None
            if yoy is not None:
                metrics.append(
                    Evidence(
                        company=ev.company,
                        code=ev.code,
                        year=ev.year,
                        metric="营业总收入同比",
                        value=_format_pct(yoy),
                        unit="%",
                        page=ev.page,
                        table_title=ev.table_title,
                        excerpt=ev.excerpt,
                        source_type="计算值",
                        definition_note=f"以 {ev.year} 年 {ev.value} 与 {ev.year - 1} 年 {prior.text} 计算。",
                        doc_id=ev.doc_id,
                        calculation=f"({ev.value} - {prior.text}) / {prior.text}",
                        numeric=yoy,
                        blocked=ev.blocked,
                        adopted=not ev.blocked,
                        reliable=ev.reliable,
                        context_status=ev.context_status,
                        needs_header_verification=ev.needs_header_verification,
                        header_page=ev.header_page,
                    )
                )
            lines.append(
                f"- 营业总收入 {ev.value} {ev.unit}（{ev.metric}，p{ev.page}）"
                + (f"，{ev.year - 1} 年 {prior.text} {ev.unit}" if prior else "")
                + (f"，同比 {_format_pct(yoy)}（计算值）" if yoy is not None else "")
                + "。"
            )
        notes.append("使用“营业总收入”口径；未用“营业收入”替代。")
    else:
        ev = _evidence_from_row(revenue_row, metric=revenue_row.raw_label)
        metrics.append(ev)
        lines.append(f"- {ev.metric} {ev.value} {ev.unit}（p{ev.page}）。")
        notes.append("题面或检索结果只支持“营业收入”口径。")
    if net_row is not None:
        net_ev = _evidence_from_row(net_row, metric=net_row.raw_label)
        metrics.append(net_ev)
        prior = net_row.second_amount()
        yoy = None
        if prior and net_ev.numeric is not None:
            yoy = _ratio_percent(net_ev.numeric - prior.value, prior.value)
        if yoy is not None:
            metrics.append(
                Evidence(
                    company=net_ev.company,
                    code=net_ev.code,
                    year=net_ev.year,
                    metric="归母净利润同比",
                    value=_format_pct(yoy),
                    unit="%",
                    page=net_ev.page,
                    table_title=net_ev.table_title,
                    excerpt=net_ev.excerpt,
                    source_type="计算值",
                    definition_note=f"以 {net_ev.year} 年 {net_ev.value} 与 {net_ev.year - 1} 年 {prior.text} 计算。",
                    doc_id=net_ev.doc_id,
                    calculation=f"({net_ev.value} - {prior.text}) / {prior.text}",
                    numeric=yoy,
                    blocked=net_ev.blocked,
                    adopted=not net_ev.blocked,
                    reliable=net_ev.reliable,
                    context_status=net_ev.context_status,
                    needs_header_verification=net_ev.needs_header_verification,
                    header_page=net_ev.header_page,
                )
            )
        lines.append(
            f"- 归属于母公司股东的净利润 {net_ev.value} {net_ev.unit}"
            f"（{net_ev.metric}，p{net_ev.page}）"
            + (f"，{net_ev.year - 1} 年 {prior.text} {net_ev.unit}" if prior else "")
            + (f"，同比 {_format_pct(yoy)}（计算值）" if yoy is not None else "")
            + "。"
        )
    text = f"{name}（{code}）{chosen_revenue.year} 年：\n" + "\n".join(lines)
    return _base_answer(
        question,
        question_id,
        STATUS_ANSWERABLE,
        text,
        metrics,
        scope,
        notes=notes,
        warnings=["同比为按年报两期数值计算的规则结果，未经人工复核。"],
        deterministic=True,
        audit={
            "营业总收入行": total_row.excerpt if total_row else "",
            "营业收入行": revenue_row.excerpt if revenue_row else "",
            "归母净利润行": net_row.excerpt if net_row else "",
        },
    )


def _answer_cross_financial(
    question: str, question_id: str, pages: Sequence[PageRecord], scope: Sequence[str]
) -> dict:
    rows = _build_rows(pages)
    by_code: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_code[row.code].append(row)
    names, order = _company_maps()
    ordered_codes = [c for c in order if c in by_code] or list(by_code)
    data: list[dict] = []
    metrics: list[Evidence] = []
    missing: list[str] = []
    for code in ordered_codes:
        company_rows = by_code[code]
        total = _find_total_revenue(company_rows)
        net = _find_parent_net_profit(company_rows)
        if total is None or net is None:
            missing.append(names.get(code, code))
            continue
        total_token = total.first_amount()
        net_token = net.first_amount()
        if total_token is None or net_token is None:
            missing.append(names.get(code, code))
            continue
        unit = total.unit
        scale = UNIT_SCALE.get(unit)
        if scale is None:
            missing.append(names.get(code, code))
            continue
        total_yuan = total_token.value * scale
        net_yuan = net_token.value * UNIT_SCALE.get(net.unit, Decimal("1"))
        total_ev = _evidence_from_row(
            total,
            metric=total.raw_label,
            definition_note=f"原文单位 {unit}；折合 {_format_decimal(total_yuan)} 元。",
            calculation=f"{total_token.text} × {scale}",
            numeric=total_token.value,
        )
        net_ev = _evidence_from_row(
            net,
            metric=net.raw_label,
            definition_note=f"原文单位 {net.unit}；折合 {_format_decimal(net_yuan)} 元。",
            calculation=f"{net_token.text} × {UNIT_SCALE.get(net.unit, Decimal('1'))}",
            numeric=net_token.value,
        )
        metrics.extend([total_ev, net_ev])
        data.append(
            {
                "code": code,
                "name": names.get(code, total.company),
                "total": total_yuan,
                "net": net_yuan,
                "total_ev": total_ev,
                "net_ev": net_ev,
            }
        )
    if missing:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            metrics,
            scope,
            refusals=[
                "以下公司未同时抽取到营业总收入和归母净利润："
                + "、".join(missing)
            ],
        )
    data.sort(key=lambda item: item["total"], reverse=True)
    # 引用记录与正文排序保持一致，避免读者按表格顺序误读为排名。
    metrics = [
        evidence
        for item in data
        for evidence in (item["total_ev"], item["net_ev"])
    ]
    lines = ["按营业总收入从高到低："]
    for index, item in enumerate(data, 1):
        lines.append(
            f"{index}. {item['name']}（{item['code']}）：营业总收入 "
            f"{item['total_ev'].value} {item['total_ev'].unit} = "
            f"{_format_decimal(item['total'])} 元（p{item['total_ev'].page}）；"
            f"归母净利润 {item['net_ev'].value} {item['net_ev'].unit} = "
            f"{_format_decimal(item['net'])} 元（p{item['net_ev'].page}）。"
        )
    text = "\n".join(lines)
    return _base_answer(
        question,
        question_id,
        STATUS_ANSWERABLE,
        text,
        metrics,
        scope,
        notes=[
            "三家公司原文单位不同，已按 1 千元=1000 元、1 万元=10000 元、"
            "1 亿元=100000000 元统一展示；排序只按折合后的营业总收入。",
            "净利润均为归属于母公司股东（或上市公司股东）口径。",
        ],
        warnings=["排序为规则计算结果，未经人工核验；不得把检索排名当作本排序。"],
        deterministic=True,
        audit={
            "排序依据": "营业总收入（折合元）",
            "公司顺序": [item["code"] for item in data],
            "缺失公司": missing,
        },
    )


def _answer_overseas_rd_comparison(
    question: str, question_id: str, pages: Sequence[PageRecord], scope: Sequence[str]
) -> dict:
    rows = _build_rows(pages)
    by_code: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_code[row.code].append(row)
    names, order = _company_maps()
    codes = [c for c in order if c in by_code] or list(by_code)
    metrics: list[Evidence] = []
    lines: list[str] = ["可单独回答的部分（研发投入强度，均取自未阻断页）："]
    blocked_companies: list[str] = []
    missing: list[str] = []
    audit_companies: dict[str, dict] = {}
    for code in codes:
        company_rows = by_code[code]
        name = names.get(code, company_rows[0].company)
        amount_row = _find_rd_amount(company_rows)
        ratio_row = _find_rd_ratio(company_rows)
        if amount_row is None or ratio_row is None:
            missing.append(name + "研发")
        else:
            amount_ev = _evidence_from_row(amount_row, metric="研发投入合计")
            ratio_token = ratio_row.first_amount() or (
                ratio_row.numbers[0] if ratio_row.numbers else None
            )
            if ratio_token:
                ratio_ev = _evidence_from_row(
                    ratio_row,
                    metric=ratio_row.raw_label,
                    value=_format_pct(ratio_token.value),
                    unit="%",
                )
                metrics.extend([amount_ev, ratio_ev])
                lines.append(
                    f"- {name}（{code}）：研发投入合计 {amount_ev.value} "
                    f"{amount_ev.unit}（p{amount_ev.page}），研发投入强度 "
                    f"{_format_pct(ratio_token.value)}（{ratio_row.raw_label}，"
                    f"p{ratio_ev.page}）。"
                )
        groups = _region_groups(company_rows)
        preferred = _preferred_region_group(groups)
        if preferred is None:
            missing.append(name + "境外")
            audit_companies[name] = {"分地区": "未找到"}
            continue
        _, region_rows = preferred
        foreign = _find_labeled_row(region_rows, REGION_FOREIGN_LABELS)
        if foreign is None or foreign.first_amount() is None:
            missing.append(name + "境外")
            audit_companies[name] = {"分地区": "未找到境外行"}
            continue
        is_blocked = foreign.page.blocked or not foreign.page.reliable
        region_ratio = _region_ratio(foreign, region_rows)
        audit_companies[name] = {
            "境外行": foreign.excerpt,
            "数据页": foreign.page.page,
            "blocked": is_blocked,
            "上下文关联状态": foreign.page.context_status,
            "占比": _format_pct(region_ratio[0]) if region_ratio else "未计算",
            "占比来源": region_ratio[1] if region_ratio else "",
        }
        if is_blocked:
            blocked_companies.append(name)
            # 阻断值只作为候选记录，adopted=False。
            candidate = _evidence_from_row(
                foreign,
                metric=foreign.raw_label,
                definition_note="blocked 页面，仅作为候选，不采信。",
                adopted=False,
            )
            metrics.append(candidate)
            if region_ratio:
                metrics.append(
                    _evidence_for_ratio(
                        candidate,
                        region_ratio[0],
                        metric="境外收入占比（候选）",
                        definition_note="由 blocked 页面计算，仅作为候选，不采信。",
                        calculation="blocked 页面分地区合计",
                        adopted=False,
                    )
                )
            lines.append(
                f"- {name}（{code}）：境外收入所在页 p{foreign.page.page} "
                "blocked=true，数值不得作为已确认事实，不能用于完成对比。"
            )
        else:
            ev = _evidence_from_row(foreign, metric=foreign.raw_label)
            metrics.append(ev)
            if region_ratio:
                metrics.append(
                    _evidence_for_ratio(
                        ev,
                        region_ratio[0],
                        metric="境外收入占比",
                        definition_note=(
                            "按同表分地区行合计计算。"
                            if region_ratio[1] == "计算值"
                            else "原文披露占比列。"
                        ),
                        calculation=(
                            "境外收入 / 同表分地区合计"
                            if region_ratio[1] == "计算值"
                            else "原文披露占比列"
                        ),
                    )
                )
                lines.append(
                    f"- {name}（{code}）：境外收入 {ev.value} {ev.unit}"
                    f"（{foreign.raw_label}，p{ev.page}），境外占比 "
                    f"{_format_pct(region_ratio[0])}（{region_ratio[1]}）。"
                )
    lines.append("")
    if blocked_companies:
        lines.append(
            "境外收入占比对比：现有证据不足，无法可靠回答。缺少可靠证据的公司："
            + "、".join(blocked_companies)
            + "（blocked 页面）。"
        )
    elif missing:
        lines.append(
            "境外收入占比对比：现有证据不足，无法可靠回答。缺少："
            + "、".join(missing)
        )
    else:
        lines.append(
            "境外收入占比对比：两家均有未阻断的境外收入行，但分母口径仍需人工核验；"
            "本模块不把计算值表述为已确认结论。"
        )
    status = STATUS_BLOCKED if blocked_companies else STATUS_INSUFFICIENT
    return _base_answer(
        question,
        question_id,
        status,
        "\n".join(lines),
        metrics,
        scope,
        notes=[
            "研发投入强度采用年报“研发投入总额占营业收入比例”披露值。",
            "境外收入口径保留原文（境外/海外/国外等），未自动替换。",
            "blocked 页面中的数值只作为候选记录，是否采信=False。",
        ],
        warnings=[
            "研发部分可单独阅读，但不能据此完成境外收入对比。",
            "境外占比若为计算值，分母为分地区表合计，不是营业总收入。",
        ],
        refusals=(
            ["以下公司的境外收入所需页面被阻断：" + "、".join(blocked_companies)]
            if blocked_companies
            else ["境外收入对比缺少完整可靠证据。"]
        ),
        deterministic=False,
        audit={"公司审计": audit_companies, "blocked公司": blocked_companies, "缺失": missing},
    )


def _answer_overseas_ranking(
    question: str, question_id: str, pages: Sequence[PageRecord], scope: Sequence[str]
) -> dict:
    rows = _build_rows(pages)
    by_code: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_code[row.code].append(row)
    names, order = _company_maps()
    metrics: list[Evidence] = []
    table_rows: list[dict] = []
    missing: list[str] = []
    multi_table: list[dict] = []
    multi_table_conflicts: list[dict] = []
    header_pending: list[str] = []
    blocked_companies: list[str] = []
    labels_seen: list[str] = []
    denominators_seen: list[str] = []

    for code in order:
        company_rows = by_code.get(code, [])
        name = names.get(code, code)
        if not company_rows:
            missing.append(name)
            continue
        groups = _region_groups(company_rows)
        if not groups:
            missing.append(name)
            continue
        preferred = _preferred_region_group(groups)
        if preferred is None:
            missing.append(name)
            continue
        key, region_rows = preferred
        foreign = _find_labeled_row(region_rows, REGION_FOREIGN_LABELS)
        if foreign is None or foreign.first_amount() is None:
            missing.append(name)
            continue
        if foreign.raw_label not in labels_seen:
            labels_seen.append(foreign.raw_label)
        ratio = _region_ratio(foreign, region_rows)
        is_blocked = foreign.page.blocked or not foreign.page.reliable
        if is_blocked:
            blocked_companies.append(name)
        if foreign.needs_header_verification:
            header_pending.append(name)
        ev = _evidence_from_row(
            foreign,
            metric=foreign.raw_label,
            definition_note=(
                f"分地区表；表名《{foreign.table_title}》；"
                + ("blocked 页面，仅候选。" if is_blocked else "原文披露。")
            ),
            adopted=not is_blocked and not foreign.needs_header_verification,
        )
        metrics.append(ev)
        ratio_text = ""
        ratio_source = ""
        if ratio:
            ratio_source = ratio[1]
            ratio_text = _format_pct(ratio[0])
            if ratio[1] == "计算值":
                denominators_seen.append(f"{name}：分地区合计")
            else:
                denominators_seen.append(f"{name}：原文占比列")
            metrics.append(
                _evidence_for_ratio(
                    ev,
                    ratio[0],
                    metric="境外收入占比（候选）" if is_blocked else "境外收入占比",
                    definition_note=(
                        "blocked 页面计算值，仅候选。"
                        if is_blocked
                        else "原文披露占比列。"
                        if ratio[1] == "披露值"
                        else "分母为同表分地区行合计。"
                    ),
                    calculation=(
                        "blocked 页面"
                        if is_blocked
                        else "原文披露占比列"
                        if ratio[1] == "披露值"
                        else "境外收入 / 同表分地区合计"
                    ),
                    adopted=not is_blocked and not foreign.needs_header_verification,
                )
            )
        table_rows.append(
            {
                "公司": name,
                "代码": code,
                "指标原文": foreign.raw_label,
                "金额": foreign.first_amount().text,
                "单位": foreign.unit,
                "占比": ratio_text,
                "占比来源": ratio_source,
                "数据页": foreign.page.page,
                "表头来源页": (
                    foreign.page.header_page
                    if foreign.needs_header_verification
                    else None
                ),
                "表名": foreign.table_title,
                "blocked": is_blocked,
                "表头核验": "待核验" if foreign.needs_header_verification else "不适用",
                "是否采信": bool(ev.adopted and not is_blocked),
            }
        )
        # 同一公司若还有另一张分地区表，记录数值差异；不取舍。
        if len(groups) > 1:
            other_values = []
            for group_key, group_rows in groups.items():
                if group_key == key:
                    continue
                other = _find_labeled_row(group_rows, REGION_FOREIGN_LABELS)
                if other and other.first_amount():
                    diff = other.first_amount().value - foreign.first_amount().value
                    other_values.append(
                        {
                            "表名": other.table_title,
                            "数据页": other.page.page,
                            "指标": other.raw_label,
                            "金额": other.first_amount().text,
                            "差额": _format_signed(diff),
                            "是否不同": diff != 0,
                        }
                    )
                    if diff != 0:
                        multi_table_conflicts.append(
                            {
                                "公司": name,
                                "采用表": {
                                    "表名": foreign.table_title,
                                    "数据页": foreign.page.page,
                                    "金额": foreign.first_amount().text,
                                },
                                "另一表": {
                                    "表名": other.table_title,
                                    "数据页": other.page.page,
                                    "金额": other.first_amount().text,
                                },
                                "差额": _format_signed(diff),
                            }
                        )
            if other_values:
                multi_table.append(
                    {
                        "公司": name,
                        "采用表": {
                            "表名": foreign.table_title,
                            "数据页": foreign.page.page,
                            "金额": foreign.first_amount().text,
                        },
                        "其他表": other_values,
                    }
                )

    reasons = [
        "海外业务定义未统一：原文指标名包括 " + "、".join(labels_seen) + "。",
        "分母未统一：部分公司披露“占营业收入比重”，部分只披露分地区金额；"
        "“营业收入”与“营业总收入”也不能直接互换。",
    ]
    if blocked_companies:
        reasons.append("以下公司的境外收入页 blocked=true，不能作为已确认事实：" + "、".join(blocked_companies) + "。")
    if header_pending:
        reasons.append("以下公司的分地区表表头仍待人工核验：" + "、".join(header_pending) + "。")
    if multi_table_conflicts:
        detail = "；".join(
            f"{item['公司']}（p{item['采用表']['数据页']} {item['采用表']['金额']} "
            f"vs p{item['另一表']['数据页']} {item['另一表']['金额']}，"
            f"差额 {item['差额']}）"
            for item in multi_table_conflicts
        )
        reasons.append(
            "以下公司同一报告内两张分地区表金额不同，未选择其中一张作为排名依据："
            + detail
            + "。"
        )
    elif multi_table:
        reasons.append("同一公司存在多张分地区表，未选择其中一张作为排名依据。")
    if missing:
        reasons.append("以下公司未提取到可用的境外收入行：" + "、".join(missing) + "。")
    reasons.append("在海外业务定义和分母统一前，禁止直接排名。")

    text = (
        "现有证据不足，无法可靠回答“境外收入占营业总收入比重最高的三家”。\n"
        "原因：\n"
        + "\n".join(f"{i}. {reason}" for i, reason in enumerate(reasons, 1))
        + "\n本表仅按 config/companies.csv 的公司顺序展示原始披露或候选值，"
        "不是排名；未计算前三名。"
    )
    notes = [
        "原文指标名： " + "、".join(labels_seen) + "。",
        "分母来源： " + "；".join(denominators_seen) + "。",
        "未把“境外收入÷分地区合计”改写成“境外收入÷营业总收入”。",
    ]
    warnings = [
        "表格顺序为公司名单顺序，不是按占比排序。",
        "blocked 页面和待核验表头对应的数值均标记为未采信。",
    ]
    return _base_answer(
        question,
        question_id,
        STATUS_INSUFFICIENT,
        text,
        metrics,
        scope,
        notes=notes,
        warnings=warnings,
        refusals=reasons,
        deterministic=False,
        audit={
            "排名": "未进行",
            "原始披露": table_rows,
            "同公司多表": multi_table,
            "同公司多表金额差异": multi_table_conflicts,
            "blocked公司": blocked_companies,
            "表头待核验公司": header_pending,
            "未提取公司": missing,
            "分母来源": denominators_seen,
            "禁止排名原因": reasons,
        },
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def answer_from_results(
    question: str,
    results: Sequence[Mapping],
    question_id: str = "",
) -> dict:
    """纯函数：从给定的检索结果生成答案，便于离线测试。"""
    scope = _scope_codes(question, results)
    scoped = _filter_results(results, scope)
    pages = _build_pages(scoped)
    intent = _intent(question)

    if not scoped:
        return _base_answer(
            question,
            question_id,
            STATUS_INSUFFICIENT,
            REFUSAL_SENTENCE,
            [],
            scope,
            refusals=["没有检索结果。"],
        )

    if intent["dividend"]:
        return _answer_dividend(question, question_id, pages, scope)
    if intent["rd"] and intent["region"] and not intent["all_companies"]:
        return _answer_overseas_rd_comparison(question, question_id, pages, scope)
    if intent["all_companies"] and intent["region"]:
        return _answer_overseas_ranking(question, question_id, pages, scope)
    if intent["region"] and not intent["all_companies"]:
        return _answer_region_single(question, question_id, pages, scope)
    if intent["channel"]:
        return _answer_channel(question, question_id, pages, scope)
    # 具体产品指标优先于宽泛的“产品结构”，否则含“空调”的题面若同时
    # 出现“主要产品”等词，会被错误路由到双表收入对比。
    if intent["air_conditioner"] or (intent["gross_margin"] and intent["product_structure"]):
        return _answer_product_metric(question, question_id, pages, scope)
    if intent["product_structure"]:
        return _answer_product_structure(question, question_id, pages, scope)
    if intent["rd"]:
        return _answer_rd(question, question_id, pages, scope)
    if intent["total_revenue"] or intent["net_profit"]:
        if len({p.code for p in pages}) > 1 or intent["ranking"]:
            return _answer_cross_financial(question, question_id, pages, scope)
        return _answer_financial_single(question, question_id, pages, scope)
    if intent["revenue"]:
        return _answer_financial_single(question, question_id, pages, scope)
    return _base_answer(
        question,
        question_id,
        STATUS_INSUFFICIENT,
        REFUSAL_SENTENCE,
        [],
        scope,
        refusals=["未识别出可确定性回答的指标类型。"],
    )


def answer_question(
    question: str,
    question_id: str = "",
    *,
    top_k: int | None = 8,
    results: Sequence[Mapping] | None = None,
) -> dict:
    """实时检索并生成答案；``results`` 仅用于测试/复现。"""
    if results is None:
        results = retrieve(question, top_k=top_k)
    return answer_from_results(question, results, question_id=question_id)


def answer_questions(
    *,
    top_k: int | None = 8,
    limit: int | None = None,
    questions_path: Path = QUESTIONS_CSV,
) -> list[dict]:
    questions = load_questions(questions_path)
    if limit is not None:
        questions = questions[:limit]
    return [
        answer_question(
            row["题目"],
            question_id=row.get("题目编号", ""),
            top_k=top_k,
        )
        for row in questions
    ]


def summarize_answers(answers: Sequence[Mapping]) -> dict:
    """统计状态分布、引用完整率、阻断误用和跨公司混淆。

    这些统计只描述规则输出，不代表答案准确率。
    """
    status_counts = Counter(answer.get("回答状态", "") for answer in answers)
    metrics = [
        metric
        for answer in answers
        for metric in (answer.get("指标") or [])
    ]
    complete = sum(
        1
        for metric in metrics
        if all(
            metric.get(key)
            for key in ("公司简称", "证券代码", "报告年份", "指标原文", "数值", "单位", "数据页码", "证据原文")
        )
    )
    blocked_misuse = sum(
        1
        for metric in metrics
        if metric.get("blocked") and metric.get("是否采信")
    )
    cross_confusion = 0
    for answer in answers:
        scope = set(answer.get("检索范围证券代码") or [])
        if not scope:
            continue
        for metric in answer.get("指标") or []:
            if metric.get("证券代码") and metric["证券代码"] not in scope:
                cross_confusion += 1
    return {
        "题目数": len(answers),
        "状态分布": {status: status_counts.get(status, 0) for status in ANSWER_STATUSES},
        "引用记录数": len(metrics),
        "引用完整记录数": complete,
        "引用完整率（%）": round(complete / len(metrics) * 100, 2) if metrics else None,
        "阻断证据误用数": blocked_misuse,
        "跨公司混淆数": cross_confusion,
        "确定性作答数": sum(1 for answer in answers if answer.get("确定性作答")),
        "答案准确率": ANSWER_ACCURACY,
        "人工核验状态": "未完成",
        "说明": (
            "引用完整率只检查字段是否齐全，不检查数值是否与 PDF 一致；"
            "不是答案准确率。"
        ),
    }


def _print_answer(answer: Mapping) -> None:
    print(f"【{answer.get('题目编号', '')}】{answer.get('回答状态', '')}")
    print(answer.get("问题", ""))
    print(answer.get("答案正文", ""))
    print()
    for metric in answer.get("指标") or []:
        print(
            f"  - {metric.get('公司简称')} {metric.get('证券代码')} "
            f"{metric.get('指标原文')} = {metric.get('数值')} {metric.get('单位')} "
            f"p{metric.get('数据页码')} blocked={metric.get('blocked')} "
            f"采信={metric.get('是否采信')}"
        )
    if answer.get("警告"):
        print("警告：" + "；".join(answer["警告"]))
    if answer.get("拒答原因"):
        print("拒答原因：" + "；".join(answer["拒答原因"]))
    print()


def main(argv: Iterable[str] | None = None) -> int:
    make_output_robust()
    parser = argparse.ArgumentParser(
        description="基于检索结果的确定性答案模块（不调用大模型）"
    )
    parser.add_argument("question", nargs="*", help="问题文本")
    parser.add_argument("--question-id", default="", help="题目编号，可选")
    parser.add_argument("--all", action="store_true", help="运行全部固定题")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--json", dest="json_path", default="", help="输出 JSON 路径")
    args = parser.parse_args(argv)

    if args.all:
        answers = answer_questions(top_k=args.top_k)
    elif args.question:
        answers = [
            answer_question(
                " ".join(args.question),
                question_id=args.question_id,
                top_k=args.top_k,
            )
        ]
    else:
        parser.error("请提供问题文本，或使用 --all")
        return 2

    summary = summarize_answers(answers)
    if args.json_path:
        payload = {
            "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "方法说明": METHOD_NOTE,
            "统计": summary,
            "题目结果": answers,
        }
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    for answer in answers:
        _print_answer(answer)
    print("统计：" + json.dumps(summary, ensure_ascii=False))
    print("提示：答案准确率未评测；所有答案仍需人工核验。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
