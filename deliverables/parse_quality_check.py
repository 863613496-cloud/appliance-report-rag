"""本轮 12 家年报解析的独立质量核验（只读，不改 CSV / JSONL / 台账）。

核验不是「解析程序自己说自己成功」，而是拿四份独立材料互相印证：

1. ``config/companies.csv``：状态、页数、输出路径、PDF 字节数；
2. ``logs/parse_{code}_2025.json``：逐页台账、异常分类、日期处置；
3. ``data/processed/{code}_2025.jsonl``：实际交付的文本块；
4. 原始 PDF：重新跑一次 pdftotext、读内容流页数、算 SHA-256。

抽检页面用固定种子选取，结果写入 JSON，供 Markdown 报告引用。
本脚本只做**自动核对**与**文本片段导出**；不声称做过人工目视检查。
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import date_repair as dr  # noqa: E402
from src import parse_policy as pp  # noqa: E402
from src import pdfobjects  # noqa: E402
from src.config import CSV_ENCODING  # noqa: E402

CODES = [
    "000651", "600690", "000921", "002668", "000521", "600336",
    "002032", "002242", "002705", "002959", "603486", "002508",
]
BASELINE = ["000333", "688169"]
SEED = 20261021

# 实测墙钟耗时（ms）：每家一个独立 `python -m src.parse --code X` 进程，
# 用 `date +%s%N` 包住进程测得，含解释器启动与 CSV 回填。
TIMING_MS = {
    "000651": 2799, "600690": 2782, "000921": 2628, "002668": 2291,
    "000521": 3729, "600336": 2066, "002032": 2742, "002242": 1879,
    "002705": 2531, "002959": 2498, "603486": 2752, "002508": 1844,
}

DATE_KINDS = {pp.KIND_WRAPPED_DATE, dr.KIND_DATE_RESTORED}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_csv() -> list[dict[str, str]]:
    with (ROOT / "config/companies.csv").open(encoding=CSV_ENCODING, newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def load_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def page_findings(report: dict, page: int) -> list[dict]:
    return [f for f in report["异常明细"] if f["page"] == page]


def coverage_gap(page_text: str, pieces: list[str]) -> tuple[int, int]:
    """顺序回找每个块，返回 (最大未覆盖间隙, 尾部未覆盖长度)。

    块间有 150 字符重叠，下一块起点可能落在上一块内部，故搜索窗口回退 400。
    """
    cursor, max_gap = 0, 0
    for piece in pieces:
        pos = page_text.find(piece, max(0, cursor - 400))
        if pos < 0:
            return 10**9, len(page_text) - cursor
        if pos > cursor:
            max_gap = max(max_gap, pos - cursor)
        cursor = max(cursor, pos + len(piece))
    return max_gap, max(0, len(page_text) - cursor)


def table_crosscheck(pdf: Path, page: int, page_text: str) -> dict:
    """在抽检表格页上，用 -simple 复跑并逐标签比对数值。"""
    other = pp.extract_page(pdf, page, pp.CROSSCHECK_MODE)
    labels = pp._data_label_candidates(page_text)
    agree = disagree = missing = 0
    disagree_labels: list[str] = []
    for lab in labels:
        a = pp.find_row(page_text, lab)
        b = pp.find_row(other, lab)
        if a is None or b is None or not a.integers or not b.integers:
            missing += 1
        elif a.integers == b.integers:
            agree += 1
        else:
            disagree += 1
            disagree_labels.append(lab)
    return {
        "复核模式": pp.CROSSCHECK_MODE,
        "候选标签数": len(labels),
        "两模式一致": agree,
        "两模式不一致": disagree,
        "一方取不到值": missing,
        "不一致标签": disagree_labels[:10],
    }


def check_date_findings(page_text: str, findings: list[dict], page: int) -> list[dict]:
    """逐条核对日期异常在文本中的坐标与落盘状态。"""
    out = []
    lines = page_text.splitlines()
    for f in findings:
        if f["kind"] not in (pp.KIND_WRAPPED_DATE, dr.KIND_DATE_RESTORED):
            continue
        out.append({
            "类型": f["kind"],
            "说明": f["detail"],
            "片段": f["excerpt"],
            "状态": f.get("status"),
            "阻断": f.get("blocks"),
        })
    return out


def verify_company(row: dict[str, str], report: dict, chunks: list[dict], pdf: Path, pages: list[str]) -> dict:
    code, name = row["证券代码"], row["公司简称"]
    report_pages = report["页数"]
    records = report["页记录"]
    chunks_by_page: dict[int, list[dict]] = {}
    for c in chunks:
        chunks_by_page.setdefault(c["页码"], []).append(c)

    failures: list[str] = []
    checks: list[dict] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append({"检查": label, "通过": bool(ok), "说明": detail})
        if not ok:
            failures.append(f"{label}：{detail}" if detail else label)

    # ---------- CSV ----------
    check("CSV 解析状态为已解析", row["解析状态"] == "已解析", row["解析状态"])
    expected_out = f"data/processed/{code}_2025.jsonl"
    check("CSV 输出路径正确", row["解析输出文件"] == expected_out, row["解析输出文件"])
    check("CSV 页码数等于台账页数", row["页码数"] == str(report_pages), f'{row["页码数"]} vs {report_pages}')
    check("CSV 下载状态为已下载", row["下载状态"] == "已下载", row["下载状态"])
    check("PDF 文件名与 CSV 一致", pdf.name == row["文件名"], pdf.name)
    check("PDF 字节数与 CSV 一致", pdf.stat().st_size == int(row["文件字节数"]),
          f'{pdf.stat().st_size} vs {row["文件字节数"]}')

    # ---------- 台账结构 ----------
    check("台账页记录数等于页数", len(records) == report_pages, f"{len(records)} vs {report_pages}")
    check("台账页号连续 1..N", [r["页码"] for r in records] == list(range(1, report_pages + 1)))
    check("台账块数之和等于报告块数", sum(r["块数"] for r in records) == report["块数"],
          f'{sum(r["块数"] for r in records)} vs {report["块数"]}')
    check("台账表格页数一致",
          sum(1 for r in records if r["类型"] == pp.PAGE_TABLE) == report["表格页数"])
    check("台账正文页数一致",
          sum(1 for r in records if r["类型"] == pp.PAGE_PROSE) == report["正文页数"])
    blank_pages = [r["页码"] for r in records if r["字符数"] == 0]
    check("台账空白页清单一致", blank_pages == report["空白页"], f"{blank_pages} vs {report['空白页']}")

    # ---------- 异常 ----------
    cats = report["异常分类计数"]
    check("异常分类计数之和等于异常总数", sum(cats.values()) == report["异常总数"],
          f'{sum(cats.values())} vs {report["异常总数"]}')
    check("异常明细条数等于异常总数", len(report["异常明细"]) == report["异常总数"])
    check("异常页码均在范围内",
          all(1 <= f["page"] <= report_pages for f in report["异常明细"]))
    by_page_kinds: dict[int, set[str]] = {}
    for f in report["异常明细"]:
        by_page_kinds.setdefault(f["page"], set()).add(f["kind"])
    check("异常类型与逐页台账一致",
          all(set(r["需复核"]) == by_page_kinds.get(r["页码"], set()) for r in records))

    # ---------- JSONL ----------
    check("JSONL 行数等于报告块数", len(chunks) == report["块数"], f'{len(chunks)} vs {report["块数"]}')
    check("JSONL 块序号连续",
          all([c["块序号"] for c in chunks_by_page.get(p, [])] == list(range(1, len(chunks_by_page.get(p, [])) + 1))
              for p in range(1, report_pages + 1)))
    check("JSONL 页码范围正确",
          all(1 <= c["页码"] <= report_pages for c in chunks))
    check("JSONL 字符数字段等于正文长度",
          all(c["字符数"] == len(c["正文"]) for c in chunks))
    check("JSONL 元数据与 CSV 一致",
          all(c["证券代码"] == code and c["公司简称"] == name and c["报告年份"] == report["报告年份"]
              and c["PDF文件名"] == pdf.name for c in chunks))

    page_detail_fail: list[str] = []
    for r in records:
        p = r["页码"]
        cs = chunks_by_page.get(p, [])
        text = pages[p - 1].strip("\n")
        if len(cs) != r["块数"]:
            page_detail_fail.append(f"p{p} 块数 {len(cs)}≠{r['块数']}")
            continue
        if r["字符数"] != len(text):
            page_detail_fail.append(f"p{p} 字符数 {r['字符数']}≠{len(text)}")
        for c in cs:
            if c["类型"] != r["类型"]:
                page_detail_fail.append(f"p{p} 块类型 {c['类型']}≠{r['类型']}")
            if c["章节"] != r["章节"]:
                page_detail_fail.append(f"p{p} 章节不一致")
            if c["需复核"] != r["需复核"]:
                page_detail_fail.append(f"p{p} 需复核不一致")
            expected_repairs = [f'{x["原始值"]}→{x["修复值"]}' for x in r["日期恢复"]
                                if x["状态"] == dr.RESTORED]
            if c["日期恢复"] != expected_repairs:
                page_detail_fail.append(f"p{p} 日期恢复记录不一致")
            if c["正文"] not in text:
                page_detail_fail.append(f"p{p} 块正文无法回引")
        if r["类型"] == pp.PAGE_TABLE and cs:
            if len(cs) != 1 or cs[0]["正文"] != text.strip():
                page_detail_fail.append(f"p{p} 表格页未整页保留")
        if r["类型"] == pp.PAGE_PROSE and cs:
            gap, tail = coverage_gap(text, [c["正文"] for c in cs])
            if gap > 200 or tail > 0:
                page_detail_fail.append(f"p{p} 覆盖缺口 {gap}/{tail}")
    check("逐页块数/类型/回引/覆盖一致", not page_detail_fail, "; ".join(page_detail_fail[:5]))

    # ---------- 日期审计 ----------
    date_count = report["日期恢复"]["检出"]
    restored = report["日期恢复"]["已恢复"]
    pending = report["日期恢复"]["待人工核验"]
    audit_rel = report["日期恢复"]["审计文件"]
    audit_ok = True
    audit_note = ""
    if date_count:
        audit_path = ROOT / audit_rel if audit_rel else None
        if not audit_path or not audit_path.exists():
            audit_ok, audit_note = False, "检出日期异常但审计文件缺失"
        else:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            if (audit["检出"], audit["已恢复"], audit["待人工核验"]) != (date_count, restored, pending):
                audit_ok, audit_note = False, "审计文件计数与台账不一致"
            if len(audit["修复明细"]) != date_count:
                audit_ok, audit_note = False, "审计明细条数不一致"
            for d in audit["修复明细"]:
                text = pages[d["页码"] - 1].strip("\n")
                if d["状态"] != dr.RESTORED:
                    if d["原始值"] not in text:
                        audit_ok, audit_note = False, f'p{d["页码"]} 待复核原值未保留'
                    if d["修复值"] in text:
                        audit_ok, audit_note = False, f'p{d["页码"]} 待复核却出现修复值'
    else:
        stale = ROOT / f"logs/date_audit_{code}_2025.json"
        if stale.exists():
            audit_ok, audit_note = False, "无检出却存在审计文件（可能为陈旧文件）"
    check("日期审计与台账一致", audit_ok, audit_note)

    # ---------- PDF 独立页数 / 哈希 ----------
    book = pdfobjects.PdfText.open(pdf)
    content_pages = book.page_count() if book else None
    check("PDF 内容流页数等于 pdftotext 页数",
          content_pages is None or content_pages == report_pages,
          f"{content_pages} vs {report_pages}")
    if report["日期恢复"]["PDF内容流页数"] is not None:
        check("台账记录的 PDF 内容流页数一致",
              report["日期恢复"]["PDF内容流页数"] == content_pages,
              f'{report["日期恢复"]["PDF内容流页数"]} vs {content_pages}')

    digest = sha256(pdf)
    raw = pdf.read_bytes()
    check("PDF 头部为 %PDF", raw.startswith(b"%PDF-"), raw[:8].decode("latin1"))
    check("PDF 尾部含 EOF", b"%%EOF" in raw[-64:], raw[-16:].decode("latin1", "replace"))

    date_details = report["日期恢复"]["明细"]
    other_cats = {k: v for k, v in cats.items() if k not in DATE_KINDS}

    # ---------- 抽检 ----------
    rng = random.Random(SEED * 1_000_000 + int(code))
    normal_pool = [r for r in records if r["类型"] == pp.PAGE_PROSE and r["字符数"] >= 200 and not r["需复核"]]
    if not normal_pool:
        normal_pool = [r for r in records if r["类型"] == pp.PAGE_PROSE and r["字符数"] >= 200]
    table_pool = [r for r in records if r["类型"] == pp.PAGE_TABLE and r["字符数"] >= 200 and not r["需复核"]]
    if not table_pool:
        table_pool = [r for r in records if r["类型"] == pp.PAGE_TABLE and r["字符数"] >= 200]
    if not table_pool:
        table_pool = [r for r in records if r["类型"] == pp.PAGE_TABLE]

    abnormal_pool = [r for r in records if any(k != pp.KIND_SPANNING_HEADER for k in r["需复核"])]
    date_pool = [r for r in abnormal_pool if DATE_KINDS & set(r["需复核"])]
    if date_pool:
        abnormal_pool = date_pool
    else:
        strong = [r for r in abnormal_pool if set(r["需复核"]) & {
            pp.KIND_MODE_DISAGREEMENT, pp.KIND_POLLUTED_LABEL, pp.KIND_ORPHAN_NOTE,
        }]
        if strong:
            abnormal_pool = strong
    if not abnormal_pool:
        abnormal_pool = [r for r in records if r["需复核"]]

    used: set[int] = set()

    def pick(pool: list[dict]) -> dict | None:
        cand = [r for r in pool if r["页码"] not in used] or pool
        if not cand:
            return None
        chosen = rng.choice(sorted(cand, key=lambda r: r["页码"]))
        used.add(chosen["页码"])
        return chosen

    samples = []

    def build_sample(label: str, rec: dict, pool_size: int, seed_note: str) -> dict:
        p = rec["页码"]
        text = pages[p - 1].strip("\n")
        cs = chunks_by_page.get(p, [])
        fs = page_findings(report, p)
        gap, tail = coverage_gap(text, [c["正文"] for c in cs]) if rec["类型"] == pp.PAGE_PROSE else (0, 0)
        labels = pp._data_label_candidates(text)
        sample = {
            "类别": label,
            "页码": p,
            "页类型": rec["类型"],
            "字符数": rec["字符数"],
            "块数": rec["块数"],
            "需复核": rec["需复核"],
            "候选池大小": pool_size,
            "选择种子": seed_note,
            "自动核对": {
                "块数与台账一致": len(cs) == rec["块数"],
                "块正文可回引": all(c["正文"] in text for c in cs),
                "字符数与台账一致": rec["字符数"] == len(text),
                "覆盖缺口": gap,
                "尾部未覆盖": tail,
                "异常归属一致": {f["kind"] for f in fs} == set(rec["需复核"]),
            },
            "文本片段": text[:900],
            "异常明细": [
                {"类型": f["kind"], "状态": f.get("status"), "阻断": f.get("blocks"),
                 "说明": f["detail"], "片段": f["excerpt"]}
                for f in fs
            ],
        }
        if rec["类型"] == pp.PAGE_TABLE:
            sample["表格复核"] = table_crosscheck(pdf, p, text)
            sample["非空行数"] = sum(1 for line in text.splitlines() if line.strip())
            sample["数字 token 数"] = len(pp.numbers_in(text))
            sample["表格形态判读"] = {
                "可核对数据标签数": len(labels),
                "自动判读": "含可核对数据标签" if labels else "无可核对数据标签（可能是正文误分类或装饰表）",
            }
        sample["日期异常核对"] = check_date_findings(text, fs, p)
        return sample

    for label, pool in (("正常页", normal_pool), ("表格页", table_pool), ("异常页", abnormal_pool)):
        rec = pick(pool)
        if rec is None:
            samples.append({"类别": label, "页码": None, "说明": "候选池为空，未抽检"})
            continue
        samples.append(build_sample(label, rec, len(pool), f"{SEED}*1000000+{int(code)}"))

    # 补充：定向抽一个「无异常 + 含财务关键词 + 至少 3 个数据标签」的表格页。
    # 随机表格页可能抽到装饰表或正文误分类页（这本身是有价值的结果，予以保留），
    # 但还需要一个真实财务表来验证表格数值是否完整。该页用确定性规则挑选：
    # 页码最小的符合条件页，不参与随机抽样。
    fin_words = ("营业收入", "营业成本", "净利润", "资产总计", "负债合计", "货币资金", "应收账款")
    fin_pool = [
        r for r in records
        if r["类型"] == pp.PAGE_TABLE and r["字符数"] >= 200 and not r["需复核"]
        and len(pp._data_label_candidates(pages[r["页码"] - 1])) >= 3
        and any(w in pages[r["页码"] - 1] for w in fin_words)
    ]
    if fin_pool:
        rec = sorted(fin_pool, key=lambda r: r["页码"])[0]
        samples.append(build_sample("财务表格页（补充）", rec, len(fin_pool), "确定性：页码最小的符合条件页"))
    else:
        samples.append({"类别": "财务表格页（补充）", "页码": None,
                        "说明": "未找到「无异常 + 含财务关键词 + 至少 3 个数据标签」的表格页"})

    page_summary = {
        "页数": report_pages,
        "块数": report["块数"],
        "表格块数": sum(1 for c in chunks if c["类型"] == pp.PAGE_TABLE),
        "正文块数": sum(1 for c in chunks if c["类型"] == pp.PAGE_PROSE),
        "表格页数": report["表格页数"],
        "正文页数": report["正文页数"],
        "空白页数": len(report["空白页"]),
        "空白页": report["空白页"],
        "超长块数": sum(1 for c in chunks if c["字符数"] > 1000),
        "最长块字符": max((c["字符数"] for c in chunks), default=0),
    }

    return {
        "证券代码": code,
        "公司简称": name,
        "解析状态_CSV": row["解析状态"],
        "解析输出文件_CSV": row["解析输出文件"],
        "页码数_CSV": row["页码数"],
        "耗时毫秒": TIMING_MS.get(code),
        "解析": page_summary,
        "异常": {
            "总数": report["异常总数"],
            "分类计数": cats,
            "日期异常数": date_count,
            "自动恢复数": restored,
            "待复核数": pending,
            "其他异常分类": other_cats,
            "其他异常总数": sum(other_cats.values()),
        },
        "日期异常明细": date_details,
        "PDF": {
            "文件名": pdf.name,
            "字节数": pdf.stat().st_size,
            "字节数与CSV一致": pdf.stat().st_size == int(row["文件字节数"]),
            "SHA256": digest,
            "内容流页数": content_pages,
            "pdftotext页数": report_pages,
            "下载时间": row["下载时间"],
            "来源URL": row["来源URL"],
        },
        "自动核验": {
            "通过": not failures,
            "检查项数": len(checks),
            "失败项": failures,
            "检查项": checks,
        },
        "抽检": {
            "随机种子": SEED,
            "抽样方法": "每家独立 Random(种子*1000000+证券代码)；正常页=无异常正文页，表格页=无异常表格页，异常页=优先日期异常，其次模式不一致/污染/孤立附注，避免与前两类重页",
            "样本": samples,
        },
    }


def main() -> int:
    rows = {r["证券代码"]: r for r in load_csv()}
    results = []
    all_failures = []
    for code in CODES:
        row = rows[code]
        pdf = ROOT / "data/raw/reports" / row["文件名"]
        report_path = ROOT / f"logs/parse_{code}_2025.json"
        jsonl_path = ROOT / row["解析输出文件"]
        if not (pdf.exists() and report_path.exists() and jsonl_path.exists()):
            results.append({"证券代码": code, "公司简称": row["公司简称"], "自动核验": {"通过": False,
                            "失败项": ["PDF/台账/JSONL 缺失"], "检查项": []}})
            all_failures.append(f"{code} 文件缺失")
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        chunks = load_jsonl(jsonl_path)
        pages = pp.extract_document(pdf, pp.PRODUCTION_MODE)
        item = verify_company(row, report, chunks, pdf, pages)
        results.append(item)
        if not item["自动核验"]["通过"]:
            all_failures.append(f'{code} {row["公司简称"]}：' + "；".join(item["自动核验"]["失败项"]))
        print(f'{code} {row["公司简称"]}: 自动核验 {"通过" if item["自动核验"]["通过"] else "失败"}'
              f'（{item["自动核验"]["检查项数"]} 项，抽检 {len(item["抽检"]["样本"])} 页）', flush=True)

    payload = {
        "生成时间": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "检查范围": {"本轮新解析": CODES, "基线未重解析": BASELINE},
        "随机种子": SEED,
        "方法": {
            "自动核对": "CSV/台账/JSONL/PDF 四方互证；重跑 pdftotext 逐页比对块正文、字符数、覆盖；读 PDF 内容流页数；算 SHA-256",
            "抽检": "每家抽正常页、表格页、异常页各 1 页（固定种子）；另定向抽 1 个无异常、含财务关键词、至少 3 个数据标签的表格页；表格页额外用 -simple 复跑并逐标签比对数值",
            "人工目视检查": "本轮未执行。抽检中的文本片段供助手阅读，性质为 AI 文本审阅，不是人工目视；本机无 pdftoppm/pdftopng，未渲染页面图像",
        },
        "汇总": {
            "公司数": len(results),
            "自动核验通过": sum(1 for r in results if r["自动核验"]["通过"]),
            "自动核验失败": sum(1 for r in results if not r["自动核验"]["通过"]),
            "页数合计": sum(r.get("解析", {}).get("页数", 0) for r in results),
            "块数合计": sum(r.get("解析", {}).get("块数", 0) for r in results),
            "表格块合计": sum(r.get("解析", {}).get("表格块数", 0) for r in results),
            "正文块合计": sum(r.get("解析", {}).get("正文块数", 0) for r in results),
            "空白页合计": sum(r.get("解析", {}).get("空白页数", 0) for r in results),
            "日期异常合计": sum(r.get("异常", {}).get("日期异常数", 0) for r in results),
            "自动恢复合计": sum(r.get("异常", {}).get("自动恢复数", 0) for r in results),
            "待复核合计": sum(r.get("异常", {}).get("待复核数", 0) for r in results),
            "耗时毫秒合计": sum(r.get("耗时毫秒") or 0 for r in results),
            "失败项": all_failures,
        },
        "公司": results,
    }
    out = ROOT / "logs/parse_runs/verification_raw.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"写入 {out.relative_to(ROOT)}", flush=True)
    print("全部通过" if not all_failures else f"有 {len(all_failures)} 项失败", flush=True)
    return 0 if not all_failures else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
