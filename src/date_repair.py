"""日期折行的**可审计自动恢复**：只在证据充分时改，且每改一处都留下依据。

═══ 为什么不能只靠检测 ═══

``parse_policy.find_wrapped_dates`` 能可靠地**发现**「日期单元格折行、末位数字
被甩到相邻行」，但发现不等于知道：

    t 23| 区美的饮水  2025/3/29  6,500    2025/12/2  262  连带责  一年  否  否
    t 25| 机制造有限                      6              任保证

「2025/12/2 + 6」只是**一种**解释。同一形态也可能是两个独立的值恰好列对齐
（实测 p118 的 ``10``、p67 的页码 ``66`` 就是这样，检测器已用「整行只有那几个
数字即页码」的守卫排除，但守卫只是排除法，不是证明）。

所以本模块把「解释」变成「取证」：**回到 PDF 内容流**，问一个排版层面的问题
（见 :mod:`src.pdfobjects`）——残缺日期与游离数字，是否同处一个单元格，
且该格内再无别的文本。单元格是排版的最小单位，一个格里的全部文本必然属于
同一个值；这条成立，拼接才是**证明**而不是猜测。

═══ 证据分三等，只有一等能修 ═══

=======================================  ==========  ===============================================
证据                                     是否判据    来自哪里
=======================================  ==========  ===============================================
单元格包含（``原始 PDF 文本对象``）      **是**      内容流里的 ``Tm`` 坐标 + 表格线矩形
交叉模式 ``-simple``（同页是否也给出全值）否          另一次 pdftotext；只是**佐证**，见下
列坐标（检出器自身的几何判据）           否          折行尾巴与该列对齐，整行另有内容
=======================================  ==========  ===============================================

为什么 ``-simple`` 不能当判据：它会把**相邻文本串粘连**。实测 p105 无空格地粘出
完整的 ``2025/12/26``（真的），p102 却把尾巴单独留在原处（``2025/11/2`` 与 ``1``
分开）。同一个模式在同类页面上行为不一致，就无法用「它给出了全值」来证明；
它没给出全值也不能反证。故只记录，不采纳。

═══ 证据不足怎么办 ═══

**不猜。** 保留原始内容，状态记为 :data:`PENDING`（待人工核验），并沿用检测器
原有的**阻断**语义——该内容不得进入确定性财务答案。宁可少修一处，不可多填一位：
``2025/12/2`` 与 ``2025/12/26`` 都是合法日期，填错永远不会被数值合理性检查发现。
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from src import parse_policy as pp
from src import pdfobjects

# ── 状态 ──

#: 已按原始 PDF 证据恢复。
RESTORED = "已自动恢复"
#: 证据不足，保留原值待人工核验（沿用检测器的措辞，不另造词）。
PENDING = pp.NEEDS_REVIEW

#: 异常类型：日期折行**已恢复**。它不是「异常」而是「处置记录」——
#: 恢复后该值不再阻断，保留它只为让逐页台账能追溯「这一页改过什么」。
KIND_DATE_RESTORED = "日期折行已恢复"

# ── 证据来源 ──

SRC_PDF_OBJECTS = "原始 PDF 文本对象"
SRC_CROSS_MODE = "交叉模式 " + pp.CROSSCHECK_MODE
SRC_COLUMN = "列坐标"

#: 补全后的日期必须能过日历校验，否则一律不恢复。
_COMPLETE_DATE_RE = re.compile(r"((?:19|20)\d{2})/(\d{1,2})/(\d{1,2})")


@dataclass(frozen=True)
class Evidence:
    """一条可复核的取证结论。``decisive`` 标注它能否单独支持恢复。"""

    source: str
    holds: bool
    decisive: bool
    detail: str

    def to_dict(self) -> dict:
        return {"来源": self.source, "成立": self.holds, "判据": self.decisive, "结论": self.detail}


@dataclass
class Repair:
    """一处日期折行的处置记录。**未被恢复的也留档**，便于统计未决数量。"""

    page: int
    line: int  # 0 起算行号（残缺日期所在行）
    col: int
    short: str
    tail: str
    tail_line: int
    tail_col: int
    merged: str
    status: str = PENDING
    basis: list[Evidence] = field(default_factory=list)
    note: str = ""
    before: str = ""
    after: str = ""
    verified: bool = False

    def to_dict(self) -> dict:
        return {
            "页码": self.page,
            "行号": self.line + 1,
            "列": self.col,
            "原始值": self.short,
            "修复值": self.merged,
            "游离数字行号": self.tail_line + 1,
            "游离数字列": self.tail_col,
            "游离数字": self.tail,
            "游离数字处置": f"原位替换为 {len(self.tail)} 个空格（保持该行其余列的字符对齐）",
            "修复方式": "下一行同一字符区间内的游离数字回接为本日期末位",
            "状态": self.status,
            "验证": "写回时逐字符核对通过" if self.verified else "未写回",
            "说明": self.note,
            "原文": self.before,
            "修复后": self.after,
            "依据": [e.to_dict() for e in self.basis],
        }


def is_complete_date(s: str) -> bool:
    """是否为**日历上真实存在**的完整日期（``2025/2/30`` 不算）。"""
    m = _COMPLETE_DATE_RE.fullmatch(s)
    if not m:
        return False
    try:
        _dt.date(*(int(g) for g in m.groups()))
    except ValueError:
        return False
    return True


def decide(short: str, tail: str, merged: str, *, decisive_ok: bool) -> tuple[str, str]:
    """决定一处检出是「自动恢复」还是「保留原值待人工核验」。

    三条规则的先后顺序不能换：

    1. **先过日历**：拼合结果必须是真实存在的日期。``2025/12/26`` 后面若还跟着
       一个 ``6``，拼出来是 ``2025/12/266``——它不是日期，说明这一处的折行
       模型不成立（两个独立的值恰好列对齐而已）。**证据成立也不能补**，
       因为补出来的是个不存在的日期。
    2. **再要一等证据**：只有「原始 PDF 文本对象」证明两段文本同处一格才算数。
    3. 否则一律 ``PENDING``：保留原值，交给人工。
    """
    if not is_complete_date(merged):
        return PENDING, f"拼合结果 {merged} 不是日历上的有效日期，拒绝自动恢复。"
    if decisive_ok:
        return RESTORED, f"{SRC_PDF_OBJECTS}证明 {tail!r} 属于该日期，已恢复。"
    return PENDING, "无充分证据（单元格包含关系不成立），保留原始内容待人工核验。"


# ═══════════════════════ 一、取证 ═══════════════════════


def _cell_evidence(
    book: pdfobjects.PdfText, page: int, short: str, tail: str
) -> Evidence:
    """一等地证据：残缺日期与游离数字同处一个单元格，且格内再无其他文本。

    判定用的坐标是文本串的**基线起点**（PDF 不记录串的结束位置），
    因此「同格」是「起于同一格内」。对折行尾巴这个用途足够：尾巴若真属于
    该格，它的起点必然落在格内。
    """
    runs = [r for r in book.page_runs(page) if r.text.strip()]
    if not runs:
        return Evidence(SRC_PDF_OBJECTS, False, True, "读不到该页的文本对象")

    want = short + tail
    for date_run in [r for r in runs if r.text.strip() == short]:
        cell = book.cell_at(page, date_run.x, date_run.y)
        if cell is None:
            continue
        inside = sorted(
            (r for r in runs if cell.contains_run(r)), key=lambda r: (-r.y, r.x)
        )
        if len(inside) != 2:
            continue
        upper, lower = inside
        if upper.text.strip() != short or lower.text.strip() != tail:
            continue
        if lower.y >= upper.y:
            continue  # 尾巴必须在下一行（y 更小）
        return Evidence(
            SRC_PDF_OBJECTS,
            True,
            True,
            f"该页文本对象中 {short!r}(x={upper.x:.2f}, y={upper.y:.2f}) 与 "
            f"{tail!r}(x={lower.x:.2f}, y={lower.y:.2f}) 同处一个单元格"
            f"(x {cell.x0:.2f}–{cell.x1:.2f}, y {cell.y0:.2f}–{cell.y1:.2f})，"
            f"该格内再无其他文本，格内容即 {want}",
        )
    return Evidence(
        SRC_PDF_OBJECTS,
        False,
        True,
        f"文本对象中找不到「{short!r} 与 {tail!r} 同处一格且格内无他物」的单元格，"
        f"无法证明 {tail!r} 属于该日期",
    )


def _cross_mode_evidence(other_text: str | None, merged: str) -> Evidence:
    """二等证据（佐证）：交叉模式是否独立给出了同一个完整日期。

    **只记录，不采纳**（理由见模块 docstring）。但把它写进审计记录有价值：
    它能解释「为什么两模式不一致」这类后续现象。
    """
    if other_text is None:
        return Evidence(SRC_CROSS_MODE, False, False, "本页未做交叉模式提取")
    holds = bool(
        re.search(r"(?<![\d/])" + re.escape(merged) + r"(?![\d/])", other_text)
    )
    detail = (
        f"{pp.CROSSCHECK_MODE} 模式在本页也给出完整日期 {merged}"
        if holds
        else f"{pp.CROSSCHECK_MODE} 模式在本页未给出完整日期 {merged}"
    )
    return Evidence(SRC_CROSS_MODE, holds, False, detail + "（仅供参考，不作为判据）")


def _column_evidence(w: pp.WrappedDate) -> Evidence:
    """三等证据（触发条件）：检出器自身的几何判据。

    它说明「这里**像**折行」，不说明「折行的确是这么折的」——
    两个独立的值也可能恰好列对齐，故不作为判据。
    """
    return Evidence(
        SRC_COLUMN,
        True,
        False,
        f"残缺日期占第 {w.col}–{w.col + len(w.short) - 1} 字符位，游离数字 {w.tail!r} "
        f"落在第 {w.tail_col} 位（**在该区间内**），且该行在区间外另有内容"
        f"——不是页脚页码。这只说明「疑似折行」：两个独立的值也可能恰好落进同一区间，"
        f"p118 的 ``10``、p67 的页码 ``66`` 就是靠「整行只有这几个数字」的守卫排除的，"
        f"那是排除法，不是证明",
    )


# ═══════════════════════ 二、计划 ═══════════════════════


def plan_repairs(
    pdf_path: str | Path,
    pages: list[str],
    *,
    other_mode_pages: list[str] | None = None,
) -> tuple[list[Repair], int | None]:
    """检出全文的日期折行并逐条取证。

    返回 ``(修复计划, PDF 内容流页数)``。计划含**未通过**取证的条目——
    「检出了几处、修了几处、还差几处」必须都能数出来。
    页数为 ``None`` 表示内容流没读（无检出，无需取证，也就没付那份开销）。

    只有在 ``pages`` 里真的检出异常时才去读 PDF 内容流——那一趟不便宜
    （要解压全部对象流），没有异常就不该付这个成本。
    """
    pending: list[tuple[int, pp.WrappedDate]] = []
    for i, text in enumerate(pages, 1):
        for w in pp.find_wrapped_dates_detailed(text):
            pending.append((i, w))
    if not pending:
        return [], None

    book = pdfobjects.PdfText.open(pdf_path)
    out: list[Repair] = []
    for page, w in pending:
        other = (
            other_mode_pages[page - 1]
            if other_mode_pages and page <= len(other_mode_pages)
            else None
        )
        r = Repair(
            page=page,
            line=w.line,
            col=w.col,
            short=w.short,
            tail=w.tail,
            tail_line=w.tail_line,
            tail_col=w.tail_col,
            merged=w.merged,
        )
        r.basis = [
            _cell_evidence(book, page, w.short, w.tail) if book else Evidence(
                SRC_PDF_OBJECTS, False, True, "PDF 内容流无法读取（文件打不开或不是本模块支持的形态）"
            ),
            _cross_mode_evidence(other, w.merged),
            _column_evidence(w),
        ]
        r.status, r.note = decide(
            w.short, w.tail, w.merged,
            decisive_ok=any(e.decisive and e.holds for e in r.basis),
        )
        out.append(r)
    return out, (book.page_count() if book else None)


# ═══════════════════════ 三、写回 ═══════════════════════


def apply_repairs(text: str, repairs: list[Repair]) -> tuple[str, list[Repair]]:
    """把**已恢复**的条目写回页面文本，返回 ``(新文本, 实际生效的条目)``。

    一处修复要动两个地方，缺一不可：

    1. 残缺日期 → 完整日期；
    2. 游离数字 → **等长空格**（不是删除，保住该行其余列的字符对齐）。

    只改第 1 处会把数字改重、只改第 2 处会丢掉末位，两种半吊子改法都比不改更坏。

    写回前逐字符核对 ``行号/列`` 上确实是预期内容；对不上就**跳过并记录**，
    绝不按「差不多就改」处理。核对通过才置 ``verified``。

    函数对**未涉及的字符**必须逐字节无损——包括行尾。天真的写法
    ``"\\n".join(text.splitlines())`` 会把 CRLF 一律改成 LF，还会吃掉结尾的
    换行/换页符，等于悄悄重写了整页；实测 ``extract_page`` 的输出正是 CRLF 收尾
    的，一旦有人拿它直接调用就会踩到。故这里按 :func:`_split_lines` 的行
    ``(正文, 行尾)`` 拆开，只改正文、行尾原样带回。
    """
    if not repairs:
        return text, []
    lines = _split_lines(text)

    def span_ok(i: int, j: int, want: str) -> bool:
        return 0 <= i < len(lines) and lines[i][0][j : j + len(want)] == want

    applied: list[Repair] = []
    for r in repairs:
        if r.status != RESTORED:
            continue
        if not (span_ok(r.line, r.col, r.short) and span_ok(r.tail_line, r.tail_col, r.tail)):
            r.status = PENDING
            r.note = (
                f"写回前核对失败：第 {r.line + 1} 行第 {r.col} 列不是 {r.short!r}，"
                f"或第 {r.tail_line + 1} 行第 {r.tail_col} 列不是 {r.tail!r}。"
                f"文本与检出时不一致，已放弃自动恢复并转为待人工核验。"
            )
            continue
        # 原文/修复后都覆盖**两行**：修复动的是两处，只留一处无法复核。
        r.before = _both(lines, r.line, r.col, len(r.short), r.tail_line, r.tail_col, len(r.tail))
        body, eol = lines[r.line]
        lines[r.line] = (body[: r.col] + r.merged + body[r.col + len(r.short) :], eol)
        body, eol = lines[r.tail_line]
        lines[r.tail_line] = (body[: r.tail_col] + " " * len(r.tail) + body[r.tail_col + len(r.tail) :], eol)
        r.after = _both(lines, r.line, r.col, len(r.merged), r.tail_line, r.tail_col, len(r.tail))
        r.verified = True
        applied.append(r)

    # 只在真的改过时才重建文本：未被恢复的条目必须原样保留（含行尾与空行）。
    if not applied:
        return text, []
    return "".join(b + e for b, e in lines), applied


#: ``str.splitlines`` 认可的全部行终止符。逐个剥离即可还原「正文 + 行尾」，
#: 不会误伤连续的同类字符——``splitlines`` 保证每个片段末尾只带一个终止符。
_EOL_CHARS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def _split_lines(text: str) -> list[tuple[str, str]]:
    """按 ``str.splitlines`` 的语义拆成 ``[(正文, 行尾), …]``。

    行号与 :func:`src.parse_policy.find_wrapped_dates_detailed` 用 ``splitlines``
    数出来的行号**完全一致**，否则写回就会改错行。
    """
    out: list[tuple[str, str]] = []
    for raw in text.splitlines(keepends=True):
        body = raw.rstrip(_EOL_CHARS)
        out.append((body, raw[len(body) :]))
    return out


def _context(lines: list[tuple[str, str]], i: int, col: int, width: int, pad: int = 24) -> str:
    line = lines[i][0]
    lo, hi = max(0, col - pad), min(len(line), col + width + pad)
    pre = "…" if lo else ""
    post = "…" if hi < len(line) else ""
    return f"{pre}{line[lo:hi]}{post}"


def _both(
    lines: list[tuple[str, str]],
    i: int,
    col: int,
    width: int,
    j: int,
    jcol: int,
    jwidth: int,
) -> str:
    """日期行与游离数字行的上下文，用 ``⏎`` 连接（沿用 ``_excerpt`` 的记法）。"""
    if i == j:
        return _context(lines, i, col, width)
    return (
        f"[日期行] {_context(lines, i, col, width)}"
        f" ⏎ [游离数字行] {_context(lines, j, jcol, jwidth)}"
    )


# ═══════════════════════ 四、修复的副作用 ═══════════════════════


def _finding_key(f: pp.Finding) -> tuple:
    """一条异常的**稳定身份**。

    不能直接拿 ``Finding`` 相等性来比对：异常的文字里往往**嵌着被修复的那个数字**
    （「模式间不一致」的片段就是两模式各自的整数序列），修复后同一条异常会换一身
    措辞，按相等性比就会被同时算成「消失」和「新增」——把 1 条改写记成 2 条变化。
    身份取「类型 + 位置/标签」，内容另放「改写」桶。
    """
    label = f.extra.get("label")
    if label is not None:
        return (f.kind, str(label))
    pos = f.excerpt.split("：", 1)[0]  # 「第 101 行：…」→ 取行号
    return (f.kind, pos, f.extra.get("missing", ""))


def side_effects(before: list[pp.Finding], after: list[pp.Finding]) -> dict:
    """修复前后本页异常的增减。

    **修一处会让别处跟着变**——这不是猜测，是实测到的：游离数字被清掉后，
    那一行就不含任何数值了，``find_row`` 取不到值，模式间比对自然跳过，
    于是「模式间不一致」少了一条。它既不是「分歧解决了」，也不是「异常被吞了」，
    而是「产生该分歧的那颗残料没了」。这类副作用必须显式记下来，
    否则「异常总数下降」会被读成「问题变少了」。
    """
    kb = {_finding_key(f): f for f in before}
    ka = {_finding_key(f): f for f in after}

    def rec(f: pp.Finding) -> dict:
        return {"类型": f.kind, "状态": f.status, "说明": f.detail, "片段": f.excerpt}

    # 改写的判定要连 ``excerpt`` 一起比：模式间不一致的**数字在 excerpt 里**，
    # 只看 detail 会漏掉（detail 里只有标签，没有数值）。
    rewritten = [
        {"类型": ka[k].kind, "修复前": kb[k].excerpt, "修复后": ka[k].excerpt}
        for k in kb
        if k in ka and (kb[k].detail, kb[k].excerpt) != (ka[k].detail, ka[k].excerpt)
    ]
    return {
        "消失的异常": [rec(f) for k, f in kb.items() if k not in ka],
        "新增的异常": [rec(f) for k, f in ka.items() if k not in kb],
        "改写的异常": rewritten,
        "注": "消失不等于「问题已解决」：请按「说明」逐条判断。"
              "已知情形——游离数字被清除后该行不再含任何数值，模式间比对跳过。",
    }


# ═══════════════════════ 五、落盘用汇总 ═══════════════════════


def audit_payload(
    *,
    code: str,
    name: str,
    year: int,
    pdf_name: str,
    repairs: list[Repair],
    pdf_pages: int | None,
    side_effect_pages: dict[int, dict] | None = None,
) -> dict:
    """审计文件的正文。它存在的意义是：任何人拿它 + 原 PDF，能独立复核每一处修复。"""
    return {
        "证券代码": code,
        "公司简称": name,
        "报告年份": year,
        "PDF文件名": pdf_name,
        "PDF内容流": (
            f"读取成功（{pdf_pages} 页）" if pdf_pages else "未读取（本页无检出，无需取证）"
        ),
        "检出": len(repairs),
        "已恢复": sum(1 for r in repairs if r.status == RESTORED),
        "待人工核验": sum(1 for r in repairs if r.status != RESTORED),
        "生成时间": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "证据判据": (
            f"仅「{SRC_PDF_OBJECTS}」是一等判据；"
            f"「{SRC_CROSS_MODE}」与「{SRC_COLUMN}」只作佐证，不单独支持恢复。"
        ),
        "修复明细": [r.to_dict() for r in repairs],
        "修复的副作用": {
            f"p{pg}": v for pg, v in sorted((side_effect_pages or {}).items())
        }
        or "无",
    }
