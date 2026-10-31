"""把年报 PDF 解析为带页码的文本块。

═══ 一、用什么提取 ═══

用 **pdftotext（Xpdf 4.06）**，不用 pypdf。

原因有两条，都是实测而非偏好：
1. ``requirements.txt`` 虽列了 pypdf，但本机**根本没装**（``ModuleNotFoundError``），
   pandas 同样没装。写一份依赖未安装库的实现等于交一份跑不起来的代码。
2. ``parse_policy.py`` 记录的全部实测结论——哪种模式在哪些页上会错位、
   错多少、错成哪个真实的数——都是针对 pdftotext 的。换库就等于丢掉
   这些已核验的护栏，而护栏正是本模块最值钱的部分。

═══ 二、怎么提取 ═══

整篇提取一次（``parse_policy.extract_document``），按分页符切成逐页文本。
实测美的 276 页：整篇 2.2 秒 vs 逐页 27.9 秒，差 12 倍；且 2026-10-16 已逐页
复验 ``-table`` / ``-simple`` 两模式各 276 页**全部逐字节一致（0 处不符）**。

═══ 三、正文与表格分开处理 ═══

早先的整篇纯文本方案会把财务表格压成一串数字，行列关系彻底丢失——这是
本项目最不能接受的失败（错的是数，而且看起来像对的数）。本模块因此按页
判定类型，两类页走不同的切块策略：

* **表格页 → 整页作为 1 个块，不切。** 宁可块大，不可切散。「美的 2025 年
  营业收入 4xxxxxx 千元」必须与它所在的那一行同进同出，检索命中时才可能
  连同表头、单位、年份一起召回。
* **正文页 → 按 CHUNK_SIZE 切，带 CHUNK_OVERLAP 重叠。**

代价要说清楚：表格页不切分会产生远超 CHUNK_SIZE 的长块（报表附注页可达数千
字符），对向量检索的语义集中度不利。这是**刻意选择**——行列关系不可重建，
而长块只是检索效果打折。是否再按空行把多张表拆开，留待有实测依据时再做。

表格页判据见 ``config.TABLE_PAGE_MIN_RATIO``，其校准过程与局限写在那里。

═══ 四、可追溯与不静默跳过 ═══

每个块都带 证券代码 / 公司简称 / 报告年份 / PDF文件名 / 页码，可回引到原 PDF 的
具体某页。另生成 ``logs/parse_{code}_{year}.json`` 逐页报告：**每一页都有一行**，
含页类型、字符数、块数、命中的异常；空白页、提取不到内容的页都在里面显式记着，
不存在「没出错就不记录」的页。异常清单来自 ``parse_policy.scan_anomalies``，
按页隔离，不阻断其余页的解析。

═══ 五、不做什么 ═══

本模块只做「PDF → 带页码的文本块」。不建索引、不做向量化、不做问答，
也不改动 ``parse_policy`` 的既有判定逻辑。

命令行：
    python -m src.parse --code 000333,688169
    python -m src.parse                      # 全部已下载的公司
    python -m src.parse --code 000333 --force
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src import date_repair as dr
from src import parse_policy as pp
from src.companies import load_companies, save_companies
from src.config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    LOGS_DIR,
    PROCESSED_DIR,
    PROJECT_ROOT,
    RAW_REPORTS_DIR,
    REPORT_YEAR,
    TABLE_PAGE_MIN_RATIO,
    make_output_robust,
)

# ═══════════════════════════ 一、页类型判定 ═══════════════════════════

#: 句末标点。中文正文必然带这些；表格行不带。
_SENTENCE_END = "。！？；"

#: 页脚页码这类「只有数字」的行也算表格行——它与正文行的区别和表格数据行一致。
#: 校准 TABLE_PAGE_MIN_RATIO 时用的就是这个定义，改定义必须重新校准。
_NUMBER_RE = re.compile(r"\d")


def table_line_ratio(text: str) -> float:
    """一页中「疑似表格行」占非空行的比例。

    判据：该行**含阿拉伯数字**且**不含句末标点**。
    依据：实测美的 276 页，9 张已知表格页比值 0.323–0.778，
    3 张已知正文页（p40 / p146 / p242）比值均 <0.10。
    分布是连续谱不是双峰，故阈值只是启发式，取值刻意偏保守。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return 0.0
    hits = sum(1 for ln in lines if _NUMBER_RE.search(ln) and not any(c in ln for c in _SENTENCE_END))
    return hits / len(lines)


def classify_page(text: str) -> str:
    """判定页类型。空页判为正文（无事可切，判成什么都一样，但报告里更好读）。"""
    if not text.strip():
        return pp.PAGE_PROSE
    return pp.PAGE_TABLE if table_line_ratio(text) >= TABLE_PAGE_MIN_RATIO else pp.PAGE_PROSE


# ═══════════════════════════ 二、章节 ═══════════════════════════

#: 章节标题，如「第三节 管理层讨论与分析」「第二节          公司简介和主要财务指标」。
_HEADING_RE = re.compile(r"^\s*(第[一二三四五六七八九十]+节)\s*(\S.*?)\s*$")


def build_chapter_map(pages: list[str]) -> dict[int, str | None]:
    """页号 → 该页所属章节；标题之前的页（封面、目录）为 None。

    排除目录页的点导引行（``第三节 管理层讨论与分析.........``）——
    实测美的 p5 是目录，8 个章节标题全在那，若不过滤会把整本报告都算成
    「第八节 财务报告」。

    只做字面提取，不猜。提取不到就是 None，不编一个章节名。
    """
    chapter: str | None = None
    out: dict[int, str | None] = {}
    for i, text in enumerate(pages, 1):
        for line in text.splitlines():
            s = line.strip()
            if "..." in s or "…" in s:
                continue  # 目录点导引行
            m = _HEADING_RE.match(s)
            if m:
                chapter = f"{m.group(1)} {m.group(2)}".strip()
                break  # 一页只认第一个标题
        out[i] = chapter
    return out


# ═══════════════════════════ 三、切块 ═══════════════════════════

#: 块的**结束**断点，从强到弱：优先按行断开，保住表格与段落的行结构。
_BREAK_CHARS = ("\n", "。", "；", "！", "？", "，")

#: 块的**起始**吸附断点，从强到弱——与结束断点**不同**，句末标点优先。
#: 原因是 PDF 的换行本身就可能断在词中间（实测块首出现「成突防爆破任务等。」
#: 这种残词），所以起点优先吸附到真正的句末，而不是行末。
_START_BREAK_CHARS = ("。", "；", "！", "？", "\n", "，")


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把正文切成 ``size`` 字符左右的块，相邻块重叠 ``overlap`` 字符。

    断点优先级：换行 > 句末标点 > 逗号。只在**后半段**（``size`` 的一半之后）
    找断点，避免切出过短的块。

    进展保证：``start`` 每轮至少前进 1 个字符，不会死循环。
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start, n = 0, len(text)
    floor = size // 2
    while start < n:
        end = min(start + size, n)
        if end < n:
            cut = -1
            for ch in _BREAK_CHARS:
                pos = text.rfind(ch, start + floor, end)
                if pos > cut:
                    cut = pos
                if cut >= 0:
                    break  # 高优先级字符找到即用，不再看低优先级
            if cut > start:
                end = cut + 1  # 断点字符归入本块
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)

        # 把块首吸附到断点上。不做这一步，块首就是裸字符偏移处，会切在词中间——
        # 实测块首出现「容。」「句，这里…」这种残词开头，回引给用户的片段将以
        # 残词起头。窗口取 40 字符：重叠从 150 最多降到 110，仍远大于一句的长度。
        hi = min(start + 40, n)
        for ch in _START_BREAK_CHARS:
            pos = text.find(ch, start, hi)
            if pos >= 0:
                start = pos + 1
                break
    return chunks


# ═══════════════════════════ 四、逐页解析 ═══════════════════════════


@dataclass
class PageRecord:
    """一页的解析结果与台账。**每一页都会有一条**，空白页也不例外。"""

    页码: int
    类型: str
    章节: str | None
    字符数: int
    块数: int
    需复核: list[str] = field(default_factory=list)
    说明: str = ""
    #: 本页的日期恢复记录（原始值 / 修复值 / 依据 / 状态），空列表＝本页无此事。
    #: 与 ``需复核`` 分开：恢复成功的不再进入异常清单，但**必须**留下台账。
    日期恢复: list[dict] = field(default_factory=list)
    #: 修复造成的异常增减（消失/新增）。「消失」不等于「解决」，必须逐条看。
    日期恢复副作用: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    证券代码: str
    公司简称: str
    报告年份: int
    PDF文件名: str
    页数: int
    块数: int
    页记录: list[PageRecord]
    异常: list[dict]
    输出文件: str
    报告文件: str
    #: 日期折行的处置记录（含**未恢复**的），审计文件写入 logs/date_audit_{code}_{year}.json
    日期恢复: list[dr.Repair] = field(default_factory=list)
    #: 原始 PDF 内容流读出的页数；None＝未读取（无检出，无需取证）
    PDF内容流页数: int | None = None
    #: 页码 → 修复造成的异常增减
    日期恢复副作用: dict[int, dict] = field(default_factory=dict)


def scan_page(page_no: int, text: str, kind: str, other_mode_text: str | None) -> list[pp.Finding]:
    """扫描一页的提取异常。

    表格页才传 ``other_mode_text``：两个模式不一致是「该页至少有一个提取结果
    不可信」的信号，这只对表格结构有意义。

    「表头跨行」（KIND_SPANNING_HEADER）在正文页上被过滤掉。它在
    ``parse_policy`` 的 docstring 里已注明误报率偏高（7 页抽查中 1 处真报、
    3 处误报），只适合当表格结构的提示；拿它去扫叙述性正文纯属噪声。
    """
    findings = pp.scan_anomalies(page_no, text, other_mode_text=other_mode_text)
    if kind == pp.PAGE_PROSE:
        findings = [f for f in findings if f.kind != pp.KIND_SPANNING_HEADER]
    return findings


def parse_pdf(pdf_path: Path, *, code: str, name: str, year: int = REPORT_YEAR) -> tuple[ParseResult, list[dict]]:
    """解析单个 PDF，返回 ``(解析结果, 文本块列表)``。

    会真实调用 pdftotext 两次（``-table`` 生产模式 + ``-simple`` 复核模式）。
    任一模式失败都直接抛错——不降级、不用另一模式顶替，因为「为什么失败」
    本身就是必须让人看到的信息。
    """
    pages = pp.extract_document(pdf_path, pp.PRODUCTION_MODE)
    cross = pp.extract_document(pdf_path, pp.CROSSCHECK_MODE)

    if len(cross) != len(pages):
        raise RuntimeError(
            f"两模式页数不一致：{pp.PRODUCTION_MODE}={len(pages)}，"
            f"{pp.CROSSCHECK_MODE}={len(cross)}。页数都定不下来，解析结果不可信。"
        )

    # 日期折行：先检出并**回到 PDF 内容流取证**，再决定改不改。
    # 放在切块之前——块是从 ``text`` 生成的，改在之后就成了「正文和台账对不上」。
    # 没有检出时这里不做任何额外的 PDF 解析（见 date_repair.plan_repairs）。
    repairs, pdf_pages = dr.plan_repairs(pdf_path, pages, other_mode_pages=cross)
    by_page: dict[int, list[dr.Repair]] = {}
    for r in repairs:
        by_page.setdefault(r.page, []).append(r)

    chapters = build_chapter_map(pages)
    records: list[PageRecord] = []
    chunks: list[dict] = []
    findings_out: list[dict] = []
    side_by_page: dict[int, dict] = {}

    for idx, raw in enumerate(pages, 1):
        text = raw.strip("\n")
        page_repairs = by_page.get(idx, [])
        # 修复前先扫一遍：修复会**让别的异常不再出现**（游离数字清掉后那一行不再含
        # 任何数值，模式间比对自然跳过）。这类副作用必须显式记下来，否则「异常
        # 总数下降」会被误读成「问题变少了」。只对本轮要改的几页付这份开销。
        pre_kind = classify_page(text)
        before_findings = (
            scan_page(idx, text, pre_kind, cross[idx - 1] if pre_kind == pp.PAGE_TABLE else None)
            if page_repairs
            else []
        )

        # 只有证据充分（status == 已自动恢复）的条目才会真的写回；
        # 写回前还会逐字符核对坐标，对不上就退回待复核（见 apply_repairs）。
        text, applied = dr.apply_repairs(text, page_repairs) if page_repairs else (text, [])
        kind = classify_page(text)
        chapter = chapters.get(idx)

        page_findings = scan_page(idx, text, kind, cross[idx - 1] if kind == pp.PAGE_TABLE else None)
        if applied:
            side = dr.side_effects(before_findings, page_findings)
            if side["消失的异常"] or side["新增的异常"]:
                side_by_page[idx] = side

        # 已恢复的日期不再出现在异常清单里（文本已经改对了），但**必须**留台账，
        # 否则「改过什么」就没有痕迹了。改完仍有残缺日期的页，find_wrapped_dates
        # 会把它照旧检出并阻断——检测与阻断机制一处未撤。
        for r in applied:
            page_findings.append(
                pp.Finding(
                    page=idx,
                    kind=dr.KIND_DATE_RESTORED,
                    detail=(
                        f"日期单元格折行已自动恢复：{r.short} → {r.merged}"
                        f"（游离数字 {r.tail!r} 原在第 {r.tail_line + 1} 行，已回接）。"
                        f"依据见 date_audit 审计文件。"
                    ),
                    excerpt=r.after,
                    status=dr.RESTORED,
                    # 恢复后该值可信，不再阻断；依据与验证状态见审计文件。
                    blocks=False,
                    extra={"原始值": r.short, "修复值": r.merged, "验证": r.verified},
                )
            )

        for f in page_findings:
            d = asdict(f)
            d["页类型"] = kind
            findings_out.append(d)

        if not text.strip():
            pieces: list[str] = []
            note = "空白页：提取不到任何文本（可能是纯图像页或本页无正文）"
        elif kind == pp.PAGE_TABLE:
            # 表格页整页不切，保住行列关系。
            pieces = [text.strip()]
            note = ""
        else:
            pieces = chunk_text(text)
            note = ""

        for seq, piece in enumerate(pieces, 1):
            chunks.append(
                {
                    "证券代码": code,
                    "公司简称": name,
                    "报告年份": year,
                    "PDF文件名": pdf_path.name,
                    "页码": idx,
                    "块序号": seq,
                    "类型": kind,
                    "章节": chapter,
                    "字符数": len(piece),
                    "需复核": sorted({f.kind for f in page_findings}),
                    "日期恢复": [f"{r.short}→{r.merged}" for r in applied],
                    "正文": piece,
                }
            )

        records.append(
            PageRecord(
                页码=idx,
                类型=kind,
                章节=chapter,
                字符数=len(text),
                块数=len(pieces),
                需复核=sorted({f.kind for f in page_findings}),
                说明=note,
                日期恢复=[r.to_dict() for r in page_repairs],
                日期恢复副作用=side_by_page.get(idx, {}),
            )
        )

    result = ParseResult(
        证券代码=code,
        公司简称=name,
        报告年份=year,
        PDF文件名=pdf_path.name,
        页数=len(pages),
        块数=len(chunks),
        页记录=records,
        异常=findings_out,
        输出文件="",
        报告文件="",
        日期恢复=repairs,
        PDF内容流页数=pdf_pages,
        日期恢复副作用=side_by_page,
    )
    return result, chunks


# ═══════════════════════════ 五、落盘 ═══════════════════════════


def _rel(path: Path) -> str:
    """项目内相对路径，统一用正斜杠（Windows 反斜杠写进 CSV 很难看也难移植）。"""
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + ``os.replace`` 原子落盘，沿用本项目既有写法。

    ``newline="\\n"`` 是**必须**的：``Path.write_text`` 默认 ``newline=None``，
    Windows 上会把 ``\\n`` 转成 ``\\r\\n``，与 .gitattributes 的 eol=lf 约定不符。
    这个坑在 download.py 上已经踩过并留下过痕迹（``logs/candidates.json``
    曾被写出 655 个 CR）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_outputs(result: ParseResult, chunks: list[dict]) -> tuple[Path, Path, Path | None]:
    """写出 JSONL 文本块、JSON 逐页报告与日期恢复审计文件。

    返回 ``(jsonl, 报告, 审计文件或 None)``，并回填 result 的字段名。
    """
    jsonl_path = PROCESSED_DIR / f"{result.证券代码}_{result.报告年份}.jsonl"
    _atomic_write_text(
        jsonl_path, "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks)
    )
    result.输出文件 = _rel(jsonl_path)

    # 有检出（无论修没修成）才写审计文件：它逐条记下原始值、修复值、页码、
    # 依据与验证状态，目标是「拿它 + 原 PDF 就能独立复核每一处修复」。
    audit_path = None
    if result.日期恢复:
        audit_path = LOGS_DIR / f"date_audit_{result.证券代码}_{result.报告年份}.json"
        _atomic_write_text(
            audit_path,
            json.dumps(
                dr.audit_payload(
                    code=result.证券代码,
                    name=result.公司简称,
                    year=result.报告年份,
                    pdf_name=result.PDF文件名,
                    repairs=result.日期恢复,
                    pdf_pages=result.PDF内容流页数,
                    side_effect_pages=result.日期恢复副作用,
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    report_path = LOGS_DIR / f"parse_{result.证券代码}_{result.报告年份}.json"
    report = {
        "证券代码": result.证券代码,
        "公司简称": result.公司简称,
        "报告年份": result.报告年份,
        "PDF文件名": result.PDF文件名,
        "页数": result.页数,
        "块数": result.块数,
        "解析时间": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "解析模式": {"主模式": pp.PRODUCTION_MODE, "复核模式": pp.CROSSCHECK_MODE},
        "切块参数": {"块长": CHUNK_SIZE, "重叠": CHUNK_OVERLAP},
        "表格页阈值": TABLE_PAGE_MIN_RATIO,
        "空白页": [r.页码 for r in result.页记录 if r.字符数 == 0],
        "表格页数": sum(1 for r in result.页记录 if r.类型 == pp.PAGE_TABLE),
        "正文页数": sum(1 for r in result.页记录 if r.类型 == pp.PAGE_PROSE),
        "异常总数": len(result.异常),
        "异常分类计数": _count_by(result.异常, "kind"),
        "日期恢复": {
            "检出": len(result.日期恢复),
            "已恢复": sum(1 for r in result.日期恢复 if r.status == dr.RESTORED),
            "待人工核验": sum(1 for r in result.日期恢复 if r.status != dr.RESTORED),
            "PDF内容流页数": result.PDF内容流页数,
            "审计文件": _rel(audit_path) if audit_path else "",
            "判据": "仅「原始 PDF 文本对象」的单元格包含关系是一等判据；"
                    "「交叉模式 -simple」与「列坐标」只作佐证，不单独支持恢复。",
            "明细": [r.to_dict() for r in result.日期恢复],
            "副作用": {f"p{pg}": v for pg, v in sorted(result.日期恢复副作用.items())} or "无",
        },
        "输出文件": result.输出文件,
        "页记录": [asdict(r) for r in result.页记录],
        "异常明细": result.异常,
    }
    _atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    result.报告文件 = _rel(report_path)
    return jsonl_path, report_path, audit_path


def _count_by(items: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[str(it.get(key))] = out.get(str(it.get(key)), 0) + 1
    return out


# ═══════════════════════════ 六、入口 ═══════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.parse",
        description="把年报 PDF 解析为带页码的文本块（不建索引、不做问答）。",
    )
    p.add_argument("--code", help="证券代码，逗号分隔；省略则处理全部已下载的公司")
    p.add_argument("--force", action="store_true", help="已存在输出文件时也重新解析")
    p.add_argument("--no-csv", action="store_true", help="不回填 companies.csv 的解析状态")
    return p


def _resolve_targets(codes: list[str] | None) -> list[dict]:
    rows = load_companies()
    by_code = {r["证券代码"]: r for r in rows}
    if codes:
        missing = [c for c in codes if c not in by_code]
        if missing:
            raise SystemExit(f"companies.csv 中没有这些证券代码：{missing}")
        return [by_code[c] for c in codes]
    ready = [r for r in rows if r["下载状态"] == "已下载"]
    if not ready:
        raise SystemExit("没有任何「已下载」的公司，无可解析对象。")
    return ready


def main(argv: list[str] | None = None) -> int:
    make_output_robust()
    args = build_parser().parse_args(argv)

    codes = [c.strip() for c in args.code.split(",")] if args.code else None
    targets = _resolve_targets(codes)

    if pp.pdftotext_path() is None:
        raise SystemExit("本机未找到 pdftotext，无法解析。")

    print(f"待解析 {len(targets)} 家；主模式 {pp.PRODUCTION_MODE}，复核模式 {pp.CROSSCHECK_MODE}")
    print(f"切块：{CHUNK_SIZE} 字符 / 重叠 {CHUNK_OVERLAP}；表格页整页不切（阈值 {TABLE_PAGE_MIN_RATIO}）")
    print("=" * 96)

    rows = load_companies()
    index = {r["证券代码"]: i for i, r in enumerate(rows)}
    failures: list[tuple[str, str]] = []

    for row in targets:
        code, name = row["证券代码"], row["公司简称"]
        pdf = RAW_REPORTS_DIR / row["文件名"] if row["文件名"] else None
        if pdf is None or not pdf.exists():
            print(f"[跳过] {code} {name}：CSV 记的文件名在磁盘上不存在（{row['文件名']!r}）")
            failures.append((code, "PDF 文件不存在"))
            continue

        out_path = PROCESSED_DIR / f"{code}_{REPORT_YEAR}.jsonl"
        if out_path.exists() and not args.force:
            print(f"[跳过] {code} {name}：{_rel(out_path)} 已存在（--force 可重解析）")
            continue

        try:
            result, chunks = parse_pdf(pdf, code=code, name=name)
        except Exception as exc:  # noqa: BLE001 —— 逐家隔离，一家失败不影响其余家
            print(f"[失败] {code} {name}：{type(exc).__name__}: {exc}")
            failures.append((code, f"{type(exc).__name__}: {exc}"))
            rows[index[code]]["解析状态"] = "解析失败"
            rows[index[code]]["备注"] = f"解析失败：{type(exc).__name__}"
            continue

        jsonl_path, report_path, audit_path = write_outputs(result, chunks)

        blank = [r.页码 for r in result.页记录 if r.字符数 == 0]
        table_pages = sum(1 for r in result.页记录 if r.类型 == pp.PAGE_TABLE)
        long_chunks = [c for c in chunks if c["字符数"] > CHUNK_SIZE]

        print(f"[完成] {code} {name}")
        print(f"    页数 {result.页数}｜块数 {result.块数}｜表格页 {table_pages}｜正文页 {result.页数 - table_pages}")
        print(f"    空白页 {len(blank)} 页 {blank[:10]}{' …' if len(blank) > 10 else ''}")
        print(f"    异常 {len(result.异常)} 处 {_count_by(result.异常, 'kind')}")
        restored = [r for r in result.日期恢复 if r.status == dr.RESTORED]
        pending = [r for r in result.日期恢复 if r.status != dr.RESTORED]
        print(f"    日期折行 检出 {len(result.日期恢复)} 处｜已恢复 {len(restored)}"
              f"｜待人工核验 {len(pending)}"
              + (f"（PDF 内容流 {result.PDF内容流页数} 页）" if result.PDF内容流页数 else ""))
        for r in restored:
            print(f"        · p{r.page} 第 {r.line + 1} 行 {r.short} → {r.merged}"
                  f"（游离数字 {r.tail!r} 在第 {r.tail_line + 1} 行）")
        for r in pending:
            print(f"        ! p{r.page} 第 {r.line + 1} 行 {r.short} 未恢复：{r.note}")
        for pg, side in sorted(result.日期恢复副作用.items()):
            print(f"        ~ p{pg} 修复副作用：异常消失 {len(side['消失的异常'])} 条"
                  f"｜新增 {len(side['新增的异常'])} 条（消失≠解决，见审计文件）")
        if audit_path:
            print(f"    日期审计 {_rel(audit_path)}")
        print(f"    超长块（> {CHUNK_SIZE} 字符）{len(long_chunks)} 个，最长 "
              f"{max((c['字符数'] for c in chunks), default=0)} 字符")
        print(f"    输出 {result.输出文件}（{jsonl_path.stat().st_size:,} 字节）")
        print(f"    报告 {result.报告文件}")

        if not args.no_csv:
            rows[index[code]]["解析状态"] = "已解析"
            rows[index[code]]["解析输出文件"] = result.输出文件
            rows[index[code]]["页码数"] = str(result.页数)

    if not args.no_csv:
        save_companies(rows)
        print("=" * 96)
        print("已回填 companies.csv：解析状态 / 解析输出文件 / 页码数")

    print("=" * 96)
    if failures:
        print(f"失败 {len(failures)} 家：")
        for c, why in failures:
            print(f"  · {c} {why}")
        return 2
    print("全部成功。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
