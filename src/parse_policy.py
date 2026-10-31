"""解析策略：用哪种模式提取，以及提取结果何时不可信。

本模块是 2026-10-14「解析策略对照实验」的产物，**不是凭经验写的默认值**。
实验对象：`data/raw/reports/000333_美的集团_2025_年度报告.pdf`（276 页）。
方法：在 4 组共 7 个页面上跑 pdftotext 的全部可用模式，逐项比对
行标签 / 列标题 / 数值 / 单位，并与经审计附注 四(48) 交叉裁决。

实测结论（**仅对已抽查页面成立，不外推为全文规律**）：

1. ``-table`` 是最可靠的单一模式——4 组中 3 组满分，第 4 组仅因单元格内
   折行标签失 1 项；在正文页上与默认模式的字符集完全一致（相似度 1.0000）。
2. ``-layout`` 会在表格页造成**间歇性**错位：第 20 页正确，第 49 / 53 / 135 页错位。
   最严重的一处把归母净利润 43,945,411 千元错成 574,785 千元（偏低 98.69%）；
   危险之处在于错位后挂上去的 574,785 是一个**真实存在**的数（少数股东损益），
   不会被「数值异常」这类启发式拦住。
3. ``-raw`` 的正确性依赖 PDF 内容流顺序：p49 / p224 恰好正确，p135 却有 2 项错。
   无从事先判断，故不用于生产解析。
4. ``-lineprinter`` 在 4 组抽查上全部 0 分，p53 / p78 整页无任何输出。
5. ``-fixed N`` 实测与默认模式逐字节相同，是空操作。
6. **没有任何一种模式能在全部抽查页上正确**——第 20 页的转置表 7 种模式全部失败。
   所以「选对模式」不足以解决问题，主提取之后**必须**有异常检测。

由于 ``-layout`` 的失败是间歇性的，「按页挑模式」无法事先决定。本模块因此
固定 ``-table`` 为主模式、``-simple`` 为交叉复核模式，改为在提取**之后**
按异常类型决定处置（见 :func:`scan_anomalies`）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ═══════════════════════════ 一、模式与参数 ═══════════════════════════

MODE_DEFAULT = "default"
MODE_LAYOUT = "layout"
MODE_TABLE = "table"
MODE_SIMPLE = "simple"
MODE_SIMPLE2 = "simple2"
MODE_RAW = "raw"
MODE_LINEPRINTER = "lineprinter"

#: 模式 → pdftotext 参数（参数支持情况已于 2026-10-14 实测确认）
MODE_FLAGS: dict[str, tuple[str, ...]] = {
    MODE_DEFAULT: (),
    MODE_LAYOUT: ("-layout",),
    MODE_TABLE: ("-table",),
    MODE_SIMPLE: ("-simple",),
    MODE_SIMPLE2: ("-simple2",),
    MODE_RAW: ("-raw",),
    MODE_LINEPRINTER: ("-lineprinter",),
}

#: 正式解析采用的模式。
PRODUCTION_MODE = MODE_TABLE

#: 表格页的交叉复核模式。实测与 ``-table`` 在全部抽查页上等效，
#: 两者结论不一致时即说明该页至少有一个提取结果不可信。
CROSSCHECK_MODE = MODE_SIMPLE

#: 禁止使用的模式 → 禁止理由（每条都对应一处实测故障，不是风格偏好）。
FORBIDDEN_MODES: dict[str, str] = {
    MODE_LAYOUT: (
        "实测在 PDF p49 / p53 / p135 三处造成标签-数值错位。最严重一处"
        "（p135 经审计合并利润表）把归母净利润 43,945,411 千元错成 574,785 千元，"
        "偏低 98.69%。"
    ),
    MODE_DEFAULT: "实测在全部带表格的抽查页上把行列打散，标签与数值完全脱离。",
    MODE_RAW: (
        "正确性依赖 PDF 内容流顺序：p49 / p224 恰好正确，p135 有 2 项错且标签被拆行。"
    ),
    MODE_LINEPRINTER: "实测 4 组抽查全部 0 分，p53 / p78 整页无输出。",
}

#: 本机 pdftotext（Xpdf 4.06）**不支持**的参数。不得直接使用。
UNSUPPORTED_FLAGS: dict[str, str] = {
    "-bbox": "Xpdf 4.06 不支持（该参数为 poppler 专有）。实测调用即报错。",
    "-bbox-layout": "同上，Xpdf 4.06 不支持。",
}

#: 支持但无收益的参数。
USELESS_FLAGS: dict[str, str] = {
    "-fixed": "支持 `-fixed <n>`，但实测 `-fixed 10` 与默认模式逐字节相同，是空操作。",
}

# ── 页面类型 ──

PAGE_PROSE = "prose"
PAGE_TABLE = "table"


def modes_for(page_kind: str = PAGE_PROSE) -> tuple[str, ...]:
    """该页类型需要提取的模式序列。

    正文页只跑主模式（``-table``，实测与默认模式字符集一致）；
    **表格页额外跑一个复核模式**，因为没有任何单一模式能在全部表格页上正确，
    两个模式结论不一致本身就是「该页不可信」的信号。
    """
    if page_kind == PAGE_TABLE:
        return (PRODUCTION_MODE, CROSSCHECK_MODE)
    return (PRODUCTION_MODE,)


# ── 提取 ──


def pdftotext_path() -> str | None:
    """返回可用的 pdftotext 可执行文件路径；不可用则返回 None。"""
    return shutil.which("pdftotext")


def extract_page(
    pdf: str | Path,
    page: int,
    mode: str = PRODUCTION_MODE,
    *,
    allow_forbidden: bool = False,
    timeout: int = 120,
) -> str:
    """提取单页文本。

    ``mode`` 若在 :data:`FORBIDDEN_MODES` 中会直接抛错，除非显式传
    ``allow_forbidden=True``（仅用于复核禁止模式的失败行为，不用于生产解析）。
    """
    if mode not in MODE_FLAGS:
        raise ValueError(f"未知模式：{mode!r}；可用：{sorted(MODE_FLAGS)}")
    if mode in FORBIDDEN_MODES and not allow_forbidden:
        raise ValueError(f"模式 {mode!r} 已被禁用：{FORBIDDEN_MODES[mode]}")
    exe = pdftotext_path()
    if exe is None:
        raise RuntimeError("本机未找到 pdftotext，无法提取。")
    cmd = [exe, *MODE_FLAGS[mode], "-f", str(page), "-l", str(page), "-enc", "UTF-8", str(pdf), "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"pdftotext 失败（{mode} p{page}）：{proc.stderr.decode('utf-8', 'replace')[:200]}"
        )
    return proc.stdout.decode("utf-8", "replace")


def extract_pages(
    pdf: str | Path, first: int, last: int, mode: str = PRODUCTION_MODE, **kw
) -> str:
    """提取一个连续页区间。

    跨页表格必须用本函数——实测 p49 的「分地区」标题与其数值分处两页
    （标题在 p49 末、数值在 p50 首），单页提取永远拿不到配对。

    页间**恰好**一个分页符：``extract_page`` 的返回值本身就以 ``\\f`` 结尾
    （pdftotext 每页输出自带），若直接 ``"\\f".join`` 会得到双分页符——
    实测 3 页区间会数出 5 个 ``\\f`` 而非 2 个。因此先 ``rstrip()`` 去掉
    每页尾部的换行与分页符，再用 ``"\\n\\f\\n"`` 拼接；分页符独占一行，
    这样上一页的最后一行不会与下一页的第一行粘成同一行（那会让
    ``find_row`` 把跨页相邻的两行误配成一对标签/数值）。
    """
    if last < first:
        raise ValueError(f"页区间非法：{first}-{last}")
    parts = [extract_page(pdf, p, mode, **kw).rstrip() for p in range(first, last + 1)]
    return "\n\f\n".join(parts)


def extract_document(
    pdf: str | Path,
    mode: str = PRODUCTION_MODE,
    *,
    allow_forbidden: bool = False,
    timeout: int = 300,
) -> list[str]:
    """整篇提取，按分页符拆成逐页文本；``len(结果)`` 即页数。

    为什么要整篇跑一次，而不是循环调 :func:`extract_page`：
    实测美的 276 页，整篇 2.2 秒 vs 逐页 27.9 秒，差 12 倍。

    为什么输出到**临时文件**而不是 stdout（这点踩过坑，务必保持）：
    实测 Windows 下 pdftotext 写给 stdout 时用文本模式，行尾变 ``CRLF``；
    写给文件时是 ``LF``。本函数统一走文件再读回，因此返回值恒为 ``LF``，
    与 .gitattributes 的 eol=lf 约定一致。

    等价性（**实测，非推断**）：2026-10-16 在美的 276 页上，
    本函数结果与逐页 :func:`extract_page` 逐字节比对，
    ``-table`` 与 ``-simple`` 两模式**各 276 页全部一致，0 处不符**
    （比对前对 CRLF 做了归一化——那是上面那条 stdout/文件差异，
    不是内容差异）。因此整篇提取在生产解析中是安全的。
    """
    if mode not in MODE_FLAGS:
        raise ValueError(f"未知模式：{mode!r}；可用：{sorted(MODE_FLAGS)}")
    if mode in FORBIDDEN_MODES and not allow_forbidden:
        raise ValueError(f"模式 {mode!r} 已被禁用：{FORBIDDEN_MODES[mode]}")
    exe = pdftotext_path()
    if exe is None:
        raise RuntimeError("本机未找到 pdftotext，无法提取。")

    with tempfile.TemporaryDirectory(prefix="rag-parse-") as tmp:
        out = Path(tmp) / "dump.txt"
        cmd = [exe, *MODE_FLAGS[mode], "-enc", "UTF-8", str(pdf), str(out)]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(
                f"pdftotext 整篇提取失败（{mode}）："
                f"{proc.stderr.decode('utf-8', 'replace')[:300]}"
            )
        raw = out.read_text(encoding="utf-8", errors="replace")

    raw = raw.replace("\r\n", "\n")
    parts = raw.split("\f")
    # pdftotext 每页输出末尾都带一个分页符，因此 split 后必然多出一个空尾段。
    # 若不是这个形态，说明页数无法可靠推断——宁可报错也不猜。
    if parts and not parts[-1].strip():
        parts.pop()
    return parts


# ═══════════════════════════ 二、行与数值 ═══════════════════════════

#: 数值/百分比 token。允许千分位、负号、括号负数、百分号。
#: 前置断言 ``(?<![\d,.])`` 避免从长数字中间切开。
_TOKEN_RE = re.compile(r"(?<![\d,.])(\(?-?\d[\d,]*(?:\.\d+)?\)?)(%?)")

#: 行内附注引用，如 ``四(48), 十八(3)``。
#: **必须先屏蔽再抽数值**——否则其中的 ``(48)`` ``(3)`` 会被当成括号负数，
#: 实测在 p135 上产生 ``(-3, 456,451,731, ...)`` 与 ``(-53, -17,787,624, ...)``
#: 这样的伪数值。特征：括号数字**紧跟在中文数字之后**，财务报表中不会
#: 出现「汉字+括号数字」形式的负数。
_NOTE_REF_INLINE_RE = re.compile(r"[一二三四五六七八九十]+\(\d+\)")

_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")


@dataclass(frozen=True)
class Row:
    """一个行标签及其数值。

    ``lines`` 保留**原始行文本**（可能是多行，见折行标签），
    ``integers`` / ``percents`` 是解析出的数值，供断言使用。
    """

    label: str
    lines: tuple[str, ...]
    integers: tuple[int, ...] = ()
    percents: tuple[str, ...] = ()

    @property
    def raw(self) -> str:
        """原始文本，保留换行——这是回引给用户的形态，不做加工。"""
        return "\n".join(self.lines)

    @property
    def single_line(self) -> bool:
        """标签与数值是否落在同一行。``False`` 表示发生了折行。"""
        return len(self.lines) == 1


def _tokenize(text: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """抽出文本中的整数与百分比，按出现顺序返回 ``(integers, percents)``。

    附注引用（``四(48)`` ``十八(3)``）先被等长空白屏蔽，避免其中的括号数字
    被误读成负数；等长替换保证字符偏移不变。
    """
    text = _NOTE_REF_INLINE_RE.sub(lambda m: " " * len(m.group()), text)
    ints: list[int] = []
    pcts: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        num, pct = m.group(1), m.group(2)
        if pct:
            pcts.append(f"{num}%")
            continue
        # 「2025 年」「2024 年度」是表头年份，不是数据。
        # 窗口取 8 个字符——``-table`` 会在年份与「年」之间塞进多个空格
        # （实测 p53 为 ``2025    年``，4 个空格），窗口太小会漏判。
        if _YEAR_RE.match(num) and text[m.end() : m.end() + 8].lstrip().startswith("年"):
            continue
        negative = num.startswith("(") and num.endswith(")")
        cleaned = num.strip("()").replace(",", "").lstrip("-")
        if not cleaned:
            continue
        value = int(float(cleaned)) if "." in cleaned else int(cleaned)
        if negative or num.startswith("-"):
            value = -value
        ints.append(value)
    return tuple(ints), tuple(pcts)


def numbers_in(line: str) -> tuple[int, ...]:
    """抽出一行中的整数（保留符号，``(335,989,528)`` → ``-335989528``）。"""
    return _tokenize(line)[0]


def percents_in(line: str) -> tuple[str, ...]:
    """抽出一行中的百分比，**原样保留**（含负号与小数位，如 ``-0.22%``）。"""
    return _tokenize(line)[1]


def find_row(
    text: str,
    label: str,
    *,
    after: str | None = None,
    before: str | None = None,
    max_gap: int = 1,
) -> Row | None:
    """定位行标签并取出其数值。

    ``after`` / ``before`` 限定搜索区块（例如 ``after="分产品"``、``before="分地区"``）。
    **这不是可选项而是必需品**——实测 PDF p49 中「其他业务」出现两次：
    分行业区块下是 47,179,307，分产品区块下是 28,718,768。不带区块限定就会取错。

    支持标签被折成两行的情况（``-table`` 在 p135 把「归属于母公司股东的」与
    「净利润」拆到两行，中间隔一个空行）：标签行若无数值，则向下跳过**空行**
    后取下一行的数值；中间只要出现非空行就放弃，避免张冠李戴。
    """
    lines = text.splitlines()
    lo, hi = 0, len(lines)
    if after is not None:
        idx = next((i for i, l in enumerate(lines) if after in l), None)
        if idx is None:
            return None
        lo = idx
    if before is not None:
        idx = next((i for i, l in enumerate(lines) if i > lo and before in l), None)
        hi = idx if idx is not None else len(lines)

    for i in range(lo, hi):
        line = lines[i]
        # 护栏一：正文行不参与取数。中文正文必然带「，」「。」，表格行不带。
        # 少了这条，p20 的「…达到智能家居业务内」会一路向下取到正文里的
        # 「零售规模突破 300 亿元」。
        if "，" in line or "。" in line:
            continue
        pos = line.find(label)
        if pos < 0:
            continue
        # 护栏二：跳过章节标题。「（一）智能家居业务」不是数据行，否则
        # 会向下取到子标题「1、中国区」的编号 1 当成数值（实测 p20 复现过）。
        if line[:pos].rstrip().endswith(("）", ")")):
            continue
        tail = line[pos + len(label) :]
        ints, pcts = _tokenize(tail)
        if ints or pcts:
            return Row(label, (lines[i],), ints, pcts)
        # 标签行没有数值 → 跳过空行后看下一行。
        # ``max_gap`` 计的是**跳过的空行数**，不是行号差：
        # p135 的「归属于母公司股东的」在 82 行、数值在 84 行，中间隔 1 个空行。
        j, skipped = i + 1, 0
        while j < hi and not lines[j].strip():
            skipped += 1
            if skipped > max_gap:
                break
            j += 1
        if skipped <= max_gap and j < hi and lines[j].strip():
            ints, pcts = _tokenize(lines[j])
            if ints or pcts:
                return Row(label, (lines[i], lines[j]), ints, pcts)
    return None


def modes_agree(text_a: str, text_b: str, label: str, **kw) -> bool:
    """两个模式的提取结果在该标签上是否一致。不一致即为不可信的信号。"""
    ra, rb = find_row(text_a, label, **kw), find_row(text_b, label, **kw)
    if ra is None or rb is None:
        return False
    return ra.integers == rb.integers


# ═══════════════════════ 三、变动列口径（百分比 / 百分点） ═══════════════════════

CHANGE_PERCENT = "百分比变化"
CHANGE_POINT = "百分点变化"

#: 表头出现这些词 → 该变动列是**百分点**差（今 − 昔），不是相对变化。
_POINT_HEADER_HINTS = ("毛利率", "占比", "比重", "比例")
#: 表头出现这些词 → 该变动列是**百分比**相对变化（(今−昔)/昔）。
_PERCENT_HEADER_HINTS = ("营业收入", "营业成本", "收入", "成本", "费用", "销量", "销售额")


def classify_change(header: str) -> str | None:
    """按表头文字判定变动列口径；返回 None 表示无法判定。

    **这不是学究式区分**。实测 PDF p50「10%以上」表的「海外」行同时含两种口径：

        海外  195,947,693  143,831,923  26.60%  15.92%  16.27%  -0.22%
                                             ↑百分比  ↑百分比  ↑百分点

    表头为「营业收入比上年同期增减 / 营业成本比上年同期增减 / 毛利率比上年同期增减」。
    前两列是相对变化，第三列是毛利率的**百分点**差（26.60% − 26.82% = −0.22pp）。
    两者在文本里长得一模一样（都带 ``%``），**只能靠表头区分**。

    返回 None 时调用方**必须**放弃该列，不得猜测口径。
    """
    if any(h in header for h in _POINT_HEADER_HINTS):
        return CHANGE_POINT
    if any(h in header for h in _PERCENT_HEADER_HINTS):
        return CHANGE_PERCENT
    return None


# ═══════════════════════════ 四、异常检测与隔离 ═══════════════════════════

#: 已知的文本层污染标签。**该名单只能由实测扩充，不得凭语义猜测添加。**
#: 现仅一项，来源：2026-10-14 对照实验，见 logs/midea_verification.md §6.2。
#:   命中特征：同一标签出现在两张语义不相关的表中（研发人员学历结构表 p53、
#:   员工教育程度表 p78），且六种提取模式给出逐字相同的字面。
#:   该行**数值可信**（经双重求和验证），**标签不可信**。
POLLUTED_LABELS: frozenset[str] = frozenset({"隐私保护"})

#: 异常状态值。带此状态的内容不得进入确定性财务答案。
NEEDS_REVIEW = "待人工核验"

KIND_SPANNING_HEADER = "表头跨行"
KIND_ORPHAN_NOTE = "孤立附注行"
KIND_POLLUTED_LABEL = "污染标签"
KIND_MODE_DISAGREEMENT = "模式间不一致"
KIND_WRAPPED_DATE = "日期折行截断"

#: 孤立的附注引用行，如 p135 中被甩出独立一行的 ``四(48),``。
#: 它属于上一行的附注列（实际是 ``四(48), 十八(3)``），脱离后会让解析器误判归属。
_ORPHAN_NOTE_RE = re.compile(
    r"^[一二三四五六七八九十]+\(\d+\)\s*(?:[,,]\s*[一二三四五六七八九十]+\(\d+\)\s*)*[,,]?$"
)


@dataclass(frozen=True)
class Finding:
    """一处需要人工复核的异常。"""

    page: int
    kind: str
    detail: str
    excerpt: str
    status: str = NEEDS_REVIEW
    #: 是否阻断该内容进入确定性财务答案。按异常类型区分——「孤立附注行」
    #: 只影响附注列的归属，数值仍可用，故不阻断；其余类型一律阻断。
    blocks: bool = True
    extra: dict = field(default_factory=dict)

    def blocks_deterministic_answer(self) -> bool:
        return self.blocks


def find_polluted_labels(text: str) -> list[str]:
    """检出命中的污染标签。"""
    return sorted({lab for lab in POLLUTED_LABELS if lab in text})


def find_orphan_notes(text: str) -> list[tuple[int, str]]:
    """检出孤立附注行 ``(行号, 内容)``。"""
    return [
        (i, line.strip())
        for i, line in enumerate(text.splitlines())
        if _ORPHAN_NOTE_RE.match(line.strip())
    ]


#: 「纯中文短词」——最可能的行标签形态。
_LABEL_ONLY_RE = re.compile(r"^[一-鿿]{2,10}$")

#: 区块标题与结构性文字。它们本来就不带数值，不参与异常检测。
_SECTION_TITLES = frozenset(
    {"分行业", "分产品", "分地区", "分销售模式", "分品类", "分渠道", "分业务", "合计", "小计", "总计"}
)


def find_spanning_headers(text: str) -> list[tuple[int, str]]:
    """检出「标签之后的第一个非空行居然是另一段文字」的行 ``(行号, 内容)``。

    实测形态（PDF p20 转置表）：

        机器人与自动          ← 标签，被从「机器人与自动化」截断
        (空行)
        单位：千元 智能家居业务 楼宇科技 工业技术 其他创新业务   ← 夹进来的整行
        (空行)
        化                    ← 标签的残余部分
        (空行)
        收入   299,927,239 35,790,825 31,010,933 …   ← 数值行

    按行解析会把**智能家居业务的 299,927,239 配给「机器人与自动化」**。
    7 种提取模式在本页无一能把标签与数值放到同一行，故只能检出后隔离。

    判据：某行是纯中文短词，且其后**第一个非空行不含任何数字**。
    正常表格里标签后面紧跟着的就是它自己的数值（或同为数值的折行尾巴），
    不会是一段纯文字，所以这个信号够用。代价是会把少数装饰性短行也标出来，
    故调用方应视其为「请人工看一眼这张表」，而非错误结论。
    """
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not _LABEL_ONLY_RE.match(s) or s in _SECTION_TITLES:
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and not re.search(r"\d", lines[j]):
            out.append((i, f"{s} → 下一非空行是「{lines[j].strip()[:40]}」，不含数值"))
    return out


#: 日期 token，如 ``2025/12/26``。
_DATE_RE = re.compile(r"(?<![\d/])((?:19|20)\d{2}/\d{1,2}/\d{1,2})(?![\d/])")


@dataclass(frozen=True)
class WrappedDate:
    """一处「日期单元格折行、末位被甩到相邻行」的检出，带完整**字符坐标**。

    行列号均为 0 起算，指向**提取文本**（不是 PDF 坐标）；``col`` 是该行内
    残缺日期的起始字符位，``tail_col`` 是尾巴在其行内的起始字符位。
    坐标是修复的前提——没有它就只能知道「有异常」，不能安全地改。
    """

    line: int
    col: int
    short: str
    tail_line: int
    tail_col: int
    tail: str

    @property
    def merged(self) -> str:
        """按折行语义拼回的完整日期。"""
        return self.short + self.tail


def find_wrapped_dates_detailed(text: str, *, lookahead: int = 3) -> list[WrappedDate]:
    """检出日期折行，返回 :class:`WrappedDate`（含坐标）。

    实测形态（PDF p105，2 处之一）：

        t 23| 区美的饮水  2025/3/29  6,500    2025/12/2  262  连带责  一年  否  否
        t 25| 机制造有限                      6              任保证
                                             ↑ col 27

    上一行 col 27 是「2025/12/2」的起点，这一行 col 27 恰好是「6」。
    该日期单元格内容是 ``2025/12/26``，**宽出一列，被 pdftotext 折成两行**：
    第 1 行放得下 9 个字符 ``2025/12/2``，溢出的 ``6`` 落到下一行同一起始列。
    于是 ``-table`` 给出的日期是 **2025/12/2**——少一天，且看不出任何异常。

    判据（三条同时成立）：
      1. 某行有日期 token，记下它的起始列 c 与长度 L；
      2. 其后 ``lookahead`` 个**非空行**内，有一行在 ``[c, c+L)`` 区间内
         **只有数字**（其余为空），且该行在区间外还有别的非空内容；
      3. 第 2 条排除了「整行只有那几个数字」的情况——那是页脚页码，
         不是折行尾巴。实测 p67 的页码 ``66`` 恰好落在日期列内，
         没有这条守卫就会误报。

    **为什么必须查这个**：它不是排版噪声，是**数值错误**（日期少一天），
    而且残缺形态 ``2025/12/2`` 本身是个合法日期，任何「数值合理性」检查都拦不住。
    """
    lines = text.splitlines()
    out: list[WrappedDate] = []
    for i, line in enumerate(lines):
        for m in _DATE_RE.finditer(line):
            col, val = m.start(), m.group(1)
            seen = 0
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if not nxt.strip():
                    continue
                seen += 1
                if seen > lookahead:
                    break
                seg = nxt[col : col + len(val)]
                frag = "".join(c for c in seg if c.strip())
                if frag and not frag.isdigit():
                    break  # 该列已有别的内容，不是折行尾巴
                if frag and frag.isdigit():
                    rest = nxt[:col] + nxt[col + len(val) :]
                    if rest.strip():
                        pad = len(seg) - len(seg.lstrip())
                        out.append(
                            WrappedDate(
                                line=i,
                                col=col,
                                short=val,
                                tail_line=j,
                                tail_col=col + pad,
                                tail=frag,
                            )
                        )
                    break
    return out


def find_wrapped_dates(text: str, *, lookahead: int = 3) -> list[tuple[int, str, str, str]]:
    """检出日期折行，返回 ``(行号, 残缺日期, 尾巴, 拼回日期)``。

    与 :func:`find_wrapped_dates_detailed` 是同一套判据；本函数保持原签名与
    返回形态，供既有调用点与测试使用（**检测器本身未被替换，只是多了一层坐标**）。
    """
    return [(w.line, w.short, w.tail, w.merged) for w in find_wrapped_dates_detailed(text, lookahead=lookahead)]


def scan_anomalies(
    page: int, text: str, *, other_mode_text: str | None = None
) -> list[Finding]:

    """扫描一页的提取结果，返回需要人工复核的异常。

    ``other_mode_text`` 传入另一模式的同页输出即可启用交叉核对——
    表格页应当传（见 :func:`modes_for`）。
    """
    findings: list[Finding] = []

    for lab in find_polluted_labels(text):
        findings.append(
            Finding(
                page,
                KIND_POLLUTED_LABEL,
                f"标签「{lab}」是已知的文本层污染，无法确认原始文字；"
                f"该行数值经求和验证可信，但标签不得用于构造答案。",
                _excerpt(text, lab),
            )
        )

    for lineno, content in find_orphan_notes(text):
        findings.append(
            Finding(
                page,
                KIND_ORPHAN_NOTE,
                "附注引用被甩到独立一行，与其所属行失去绑定，"
                "按行解析会把附注误配给相邻行。",
                f"第 {lineno + 1} 行：{content}",
                # 只影响附注列的归属，该行数值仍是正确的，故不阻断。
                blocks=False,
            )
        )

    for lineno, short, tail, merged in find_wrapped_dates(text):
        findings.append(
            Finding(
                page,
                KIND_WRAPPED_DATE,
                f"日期单元格折行疑似：-table 读出 {short}，"
                f"列坐标提示相邻行同列数字 {tail!r} 可能是折行末位，"
                f"候选拼合值 {merged}（仅为检测候选，尚未确认，也未写回）。"
                f"残缺值本身是合法日期，不会被数值合理性检查拦住。",
                f"第 {lineno + 1} 行：检测到 {short}；相邻行同列「{tail}」"
                f"→ 候选 {merged}（未确认，未写回）",
                # **必须阻断**：若折行成立就是数值错误，不是排版问题。
                # 但“检测到候选”不等于“已确认”，复核结论由 date_repair/审计台账给出。
                extra={"missing": merged, "candidate_only": True},
            )
        )

    for lineno, content in find_spanning_headers(text):
        findings.append(
            Finding(
                page,
                KIND_SPANNING_HEADER,
                "标签之后的第一个非空行不含数值，标签与数值无法按行配对；"
                "继续向下取数会把别的行的数值配给它。",
                f"第 {lineno + 1} 行：{content}",
                # **不阻断**。该判据误报率偏高（实测 7 页抽查中 1 处真报、
                # 3 处误报：正文句子结尾「体验双升级」、表头残片「同比增减」
                # 「教育程度」）。它只适合当「请人工看一眼」的提示。
                # 真正需要取数时，``find_row`` 取不到值本身就是更可靠的信号。
                blocks=False,
            )
        )

    if other_mode_text is not None:
        for lab in _data_label_candidates(text):
            a, b = find_row(text, lab), find_row(other_mode_text, lab)
            if a is None or b is None:
                continue
            if a.integers and b.integers and a.integers != b.integers:
                findings.append(
                    Finding(
                        page,
                        KIND_MODE_DISAGREEMENT,
                        f"标签「{lab}」在 {PRODUCTION_MODE} 与 {CROSSCHECK_MODE} 下"
                        f"得到不同数值，该页至少有一个提取结果不可信。",
                        f"{PRODUCTION_MODE}: {a.integers} / {CROSSCHECK_MODE}: {b.integers}",
                        extra={"label": lab},
                    )
                )

    return findings


def _excerpt(text: str, needle: str, width: int = 40) -> str:
    """取包含 ``needle`` 的原文片段，保留上下文供人工核对。"""
    pos = text.find(needle)
    if pos < 0:
        return ""
    start = max(0, pos - width)
    return ("…" if start else "") + text[start : pos + len(needle) + width].replace("\n", "⏎") + "…"


#: 结构性行标签——它们出现在每页/每表上，数值天然可能不同，不参与交叉核对。
_STRUCTURAL_LABELS = frozenset(
    {
        "合计", "小计", "总计", "其他", "其中", "其中：", "金额", "项目",
        "分行业", "分产品", "分地区", "分销售模式", "分品类", "分渠道", "分业务",
    }
)


def _data_label_candidates(text: str) -> list[str]:
    """从提取文本里挑出可作为交叉核对对象的行标签。

    只取「以中文字符开头、后接数值」的行——这类行的标签-数值绑定最容易出错，
    也最有核对价值。表头行（标签后面跟的是年份）必须排除，否则会把
    「项目   2025 年  2024 年」这类表头当成数据行去核对。
    """
    out: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^\s*([一-鿿][一-鿿\w（）()\-]{1,14}?)\s{2,}(?=\d)", line)
        if not m or m.end(1) >= len(line):
            continue
        tail = line[m.end(1) :]
        if re.match(r"\s*(?:19|20)\d{2}\s*年", tail):
            continue  # 表头年份行，不是数据行
        lab = m.group(1).strip()
        if lab in _STRUCTURAL_LABELS or lab in out:
            continue
        out.append(lab)
    return out


def assert_answer_safe(findings: list[Finding]) -> None:
    """若存在任何待复核异常则抛错——用于「阻止异常表格进入确定性答案」的闸门。

    调用点应在**生成确定性财务答案之前**，而不是之后。
    """
    blocking = [f for f in findings if f.blocks_deterministic_answer()]
    if blocking:
        detail = "\n".join(f"  · p{f.page} [{f.kind}] {f.detail}" for f in blocking)
        raise ValueError(f"存在 {len(blocking)} 处待人工核验的提取异常，已阻止其进入确定性答案：\n{detail}")
