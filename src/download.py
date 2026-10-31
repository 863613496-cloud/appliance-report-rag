"""官方年报候选检索与下载模块。

两种模式：
    --check     只检索并展示候选公告。不下载文件，不修改 config/companies.csv，
                不写入任何数据。（当前阶段的主模式）
    --download  下载已人工确认的年报 PDF，并把真实结果回填到 config/companies.csv。
                必须显式指定才会执行，且默认还要二次确认。

用法：
    python -m src.download --check
    python -m src.download --check --code 000333
    python -m src.download --check --json logs/candidates.json
    python -m src.download --download --code 000333 --yes      （本轮不执行）

判定规则（全文 vs 摘要 vs 英文版）是本模块的核心：
    巨潮实测结果中，年度报告类公告的标题存在四种形态，其中两种是陷阱：
      · 「2025年年度报告」                                   → 全文
      · 「2025年年度报告摘要」/「2025年度报告摘要」            → 摘要（排除）
      · 「2025年年度报告（英文版）」                          → 英文版（排除）
        实测存在英文版的公司：海尔智家、老板电器、格力电器，
        且英文版的披露日期晚于中文版，若按日期倒序取首条会取错。
      · 「北京石头世纪科技股份有限公司2025年年度报告」         → 全文（标题含公司全称）
    因此不能只按「是否含摘要」过滤，也不能只按日期排序取第一条。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

from src import cninfo
from src.companies import load_companies, save_companies
from src.config import (
    RAW_REPORTS_DIR,
    REPORT_YEAR,
    expected_report_filename,
    get_env,
    make_output_robust,
)

# ---------------------------------------------------------------------------
# 公告分类
# ---------------------------------------------------------------------------

KIND_FULL = "年度报告全文"
KIND_SUMMARY = "年度报告摘要"
KIND_ENGLISH = "年度报告英文版"
KIND_UNCERTAIN = "存疑·年份不符"
KIND_OTHER = "非年度报告"

# 标题中「看起来像公司全称」的特征词。
# ⚠ 只用于生成提示，绝不作为「主体就是目标公司」的判定依据：
# 任何公司名都可能含这些词（如「九阳股份有限公司」），仅凭它们无法断定主体。
# 主体判定一律以公告返回的证券代码 + 检索范围（stock=<code>,<orgId>）为准。
NAME_FEATURE_WORDS = ("股份有限公司", "有限公司", "集团")


@dataclass
class Candidate:
    """一条候选公告记录（字段全部来自接口原始返回，未做改写）。"""

    kind: str
    title: str
    publish_date: str
    url: str
    size_kb: int | None
    adjunct_type: str
    basis: str
    doubts: list[str] = field(default_factory=list)
    announcement_id: str = ""
    sec_code: str = ""
    sec_name: str = ""


@dataclass
class CompanyResult:
    """一家公司的检索结果。"""

    code: str
    name: str
    exchange: str
    org_id: str | None = None
    sec_name: str = ""
    full: list[Candidate] = field(default_factory=list)
    others: list[Candidate] = field(default_factory=list)
    doubts: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def status(self) -> str:
        if self.error:
            return "检索失败"
        if len(self.full) == 1:
            return "找到 1 条全文候选"
        if len(self.full) > 1:
            return f"找到 {len(self.full)} 条全文候选，需人工选择"
        return "未找到全文候选"


def classify(
    title: str, year: int, company_names: tuple[str, ...]
) -> tuple[str, str, list[str]]:
    """判断公告标题属于哪一类。

    Returns:
        (类别, 判定依据, 疑点列表)
    """
    text = title.strip()
    doubts: list[str] = []

    # 顺序很重要，四条规则按「排除范围由大到小」排列：
    # 「2025年半年度报告摘要」必须先被判为「非年度报告」，否则会被误当成年报摘要；
    # 「2025年年度报告摘要（英文版）」同时命中摘要与英文，归入摘要即可。
    # 注：接口已按 category_ndbg_szsh 过滤，这里再做一层防御，避免接口行为变化时出错。
    if "半年度" in text or "半年报" in text or "季度" in text:
        return KIND_OTHER, "标题为半年度/季度报告，不属于年度报告", doubts

    if "摘要" in text:
        return KIND_SUMMARY, "标题含「摘要」，属摘要版而非全文", doubts

    if "英文" in text:
        return KIND_ENGLISH, "标题含「英文」，非中文正式版本", doubts

    if f"{year}年年度报告" in text or f"{year}年度报告" in text or f"{year}年年报" in text:
        # 主体判定依据的强弱顺序：
        #   强 —— 公告返回的证券代码 secCode 与目标一致，且检索范围限定为 stock=<code>,<orgId>；
        #   弱 —— 标题中出现目标公司简称；
        #   无 —— 仅出现「股份有限公司」等特征词（任何公司名都可能含，不能据此认定主体）。
        # 接口只返回简称（实测 orgName 恒为 None），因此标题只写全称的公司
        # （如「北京石头世纪科技股份有限公司2025年年度报告」）无法程序化核对，
        # 必须标记为待人工核实，而不是因为出现了「有限公司」就放行。
        short_named = any(n and n in text for n in company_names)
        name_like = any(w in text for w in NAME_FEATURE_WORDS)
        if not short_named and name_like:
            doubts.append(
                "标题含公司全称特征词但未出现目标简称，无法据标题断定主体即目标公司，"
                "须打开原文首页人工核对"
            )
        elif not short_named:
            listed = "、".join(dict.fromkeys(n for n in company_names if n)) or "公司简称"
            doubts.append(
                f"标题未出现任何公司名称（目标简称为「{listed}」），"
                "主体仅由公告证券代码与检索范围确定，建议打开原文首页再核对一次"
            )
        return KIND_FULL, f"标题含「{year}年年度报告」且不含「摘要」「英文」", doubts

    if "年度报告" in text or "年报" in text:
        # 非捕获组：findall 直接返回完整年份，而不是 (19|20) 这个前缀分组
        years = re.findall(r"(?:19|20)\d{2}", text)
        found = "、".join(years) if years else "未标注"
        return (
            KIND_UNCERTAIN,
            f"标题含「年度报告」但年份为「{found}」，与目标年份 {year} 不符",
            [f"标题年份与目标 {year} 年不一致，需人工判断是否为目标文件"],
        )

    return KIND_OTHER, "标题不含「年度报告」，非年度报告类公告", doubts


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def _to_candidate(raw: dict, year: int, company_names: tuple[str, ...]) -> Candidate:
    """把接口原始记录转换为 Candidate。缺失字段留空，不臆造。"""
    title = str(raw.get("announcementTitle") or "").strip()
    kind, basis, doubts = classify(title, year, company_names)

    ts = raw.get("announcementTime")
    if ts:
        publish_date = _dt.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
    else:
        publish_date = ""
        doubts.append("接口未返回披露时间")

    adjunct_url = str(raw.get("adjunctUrl") or "")
    if not adjunct_url:
        doubts.append("接口未返回附件地址，无法下载")
    url = cninfo.announcement_pdf_url(adjunct_url)

    adjunct_type = str(raw.get("adjunctType") or "")
    if adjunct_url and adjunct_type.upper() != "PDF":
        doubts.append(f"附件类型为「{adjunct_type}」而非 PDF，需人工确认")

    sec_code = str(raw.get("secCode") or "")
    sec_name = str(raw.get("secName") or "")

    size_kb = raw.get("adjunctSize")
    try:
        size_kb = int(size_kb) if size_kb is not None else None
    except (TypeError, ValueError):
        size_kb = None

    return Candidate(
        kind=kind,
        title=title,
        publish_date=publish_date,
        url=url,
        size_kb=size_kb,
        adjunct_type=adjunct_type,
        basis=basis,
        doubts=doubts,
        announcement_id=str(raw.get("announcementId") or ""),
        sec_code=sec_code,
        sec_name=sec_name,
    )


def search_company_reports(
    session: requests.Session, company: dict[str, str], year: int = REPORT_YEAR
) -> CompanyResult:
    """检索单家公司的年度报告候选。任何失败都如实记录，不伪造结果。"""
    code = str(company.get("证券代码", "")).strip()
    name = str(company.get("公司简称", "")).strip()
    exchange = str(company.get("交易所", "")).strip()

    result = CompanyResult(code=code, name=name, exchange=exchange)

    # 交易所与代码前缀应互相印证，不一致则提示
    expected_column = cninfo.column_for_code(code)
    if exchange == "上交所" and expected_column != "sse":
        result.doubts.append(f"名单标注上交所，但代码 {code} 前缀属于深市，请核对")
    if exchange == "深交所" and expected_column != "szse":
        result.doubts.append(f"名单标注深交所，但代码 {code} 前缀属于沪市，请核对")

    try:
        record = cninfo.search_company(session, code)
    except cninfo.CninfoError as exc:
        result.error = f"公司检索失败：{exc}"
        return result

    if not record:
        result.error = "巨潮公司检索未返回匹配的 A 股记录"
        return result

    result.org_id = str(record.get("orgId") or "")
    result.sec_name = str(record.get("zwjc") or "")

    if not result.org_id:
        result.error = "公司检索结果缺少 orgId，无法继续检索公告"
        return result

    if result.sec_name and result.sec_name != name:
        result.doubts.append(
            f"名单简称「{name}」与巨潮简称「{result.sec_name}」不一致，请确认是否同一家公司"
        )

    try:
        raw_list = cninfo.query_annual_reports(
            session, code, result.org_id, expected_column, year
        )
    except cninfo.CninfoError as exc:
        result.error = f"公告检索失败：{exc}"
        return result

    if not raw_list:
        return result

    for raw in raw_list:
        candidate = _to_candidate(raw, year, (name, result.sec_name))
        # 交叉核对：公告自带的证券代码必须与目标一致
        if candidate.sec_code and candidate.sec_code != code:
            candidate.doubts.append(
                f"公告证券代码 {candidate.sec_code} 与目标 {code} 不一致"
            )
            candidate.kind = KIND_UNCERTAIN

        if candidate.kind == KIND_FULL:
            result.full.append(candidate)
        else:
            result.others.append(candidate)

    if len(result.full) > 1:
        result.doubts.append(
            f"检测到 {len(result.full)} 条全文候选，可能包含更正版或重复披露，需人工选择"
        )
    if not result.full and any(c.kind == KIND_SUMMARY for c in result.others):
        result.doubts.append("仅找到摘要，未找到全文——绝不可用摘要替代全文")

    return result


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------

_LINE = "─" * 72
_HEAVY = "═" * 72


def _print_company(index: int, total: int, r: CompanyResult, year: int) -> None:
    print()
    print(_LINE)
    print(f"[{index:>2}/{total}] {r.name}   {r.code}   {r.exchange}")
    print(_LINE)
    print(f"  官方来源：巨潮资讯网  {cninfo.CNINFO_BASE}")

    if r.error:
        print(f"  ⚠ 检索状态：未找到 — {r.error}")
        print("     本条不能作为「已确认」结果使用，请人工到巨潮官网复查。")
        for d in r.doubts:
            print(f"     ⚠ 疑点：{d}")
        return

    print(
        f"  公司匹配：orgId={r.org_id}  巨潮简称={r.sec_name or '(未返回)'}  "
        f"公告证券代码={r.code}  ✓ 一致"
    )

    if r.full:
        print(f"  ▶ 全文候选 {len(r.full)} 条：")
        for n, c in enumerate(r.full, 1):
            print(f"    {n}. 「{c.title}」")
            print(f"       报告年度与类型：{year} 年度报告")
            print(f"       披露日期      ：{c.publish_date or '(未返回)'}")
            print(
                f"       附件          ：{c.adjunct_type or '(未返回)'}，"
                f"{c.size_kb if c.size_kb is not None else '?'} KB"
            )
            print(f"       官方来源网址  ：{c.url or '(未返回，无法下载)'}")
            print(f"       机器判定      ：{c.kind} —— {c.basis}")
            print("       是否确认全文  ：⬜ 待人工确认（请打开上方网址核对首页标题）")
            for d in c.doubts:
                print(f"       ⚠ 疑点：{d}")
    else:
        print("  ⚠ 未找到「年度报告全文」候选。")
        print("     需人工到巨潮官网复查，不得用摘要替代，也不得推测下载地址。")

    if r.others:
        print(f"  × 已排除 {len(r.others)} 条：")
        for c in r.others:
            print(f"    · 「{c.title}」（{c.publish_date or '日期未返回'}）— {c.basis}")

    for d in r.doubts:
        print(f"  ⚠ 疑点：{d}")

    if not r.others and not r.full:
        print("  （该时段未返回任何年度报告类公告）")


def _print_summary(results: list[CompanyResult], elapsed: float) -> None:
    exact = [r for r in results if r.org_id and len(r.full) == 1]
    multi = [r for r in results if r.org_id and len(r.full) > 1]
    none = [r for r in results if not r.error and not r.full]
    failed = [r for r in results if r.error]

    print()
    print(_HEAVY)
    print("汇总")
    print(_HEAVY)
    print(f"  公司总数            ：{len(results)}")
    print(f"  找到唯一全文候选    ：{len(exact)} 家")
    print(f"  找到多条全文候选    ：{len(multi)} 家（需人工选择）")
    print(f"  未找到全文候选      ：{len(none)} 家")
    print(f"  检索失败            ：{len(failed)} 家")
    print(f"  耗时                ：{elapsed:.1f} 秒")

    if multi:
        print("\n  需人工选择的公司：")
        for r in multi:
            print(f"    · {r.code} {r.name} —— {len(r.full)} 条候选")
    if none or failed:
        print("\n  未得到可靠结果的公司（不得视为已确认）：")
        for r in none + failed:
            print(f"    · {r.code} {r.name} —— {r.status}")

    print()
    print("  本次运行未下载任何文件，config/companies.csv 未被修改。")
    print("  下一轮：人工打开上述「官方来源网址」核对后，再执行下载。")


# ---------------------------------------------------------------------------
# 下载（本轮不执行，仅实现）
# ---------------------------------------------------------------------------

PDF_MAGIC = b"%PDF-"
MIN_PDF_BYTES = 100 * 1024  # 100 KB。实测各公司年报正文均 > 1 MB，摘要也有 134 KB


def looks_like_pdf(path: Path) -> tuple[bool, str]:
    """检查文件头是否为 PDF。返回 (是否通过, 说明)。"""
    try:
        with path.open("rb") as f:
            head = f.read(1024)
    except OSError as exc:
        return False, f"无法读取：{exc}"

    if not head:
        return False, "文件为空"
    if not head.startswith(PDF_MAGIC):
        preview = head[:60].decode("utf-8", errors="replace").replace("\n", " ")
        return False, f"文件头不是 PDF（实际开头：{preview!r}），可能是错误页或登录页"
    return True, "文件头为 %PDF-"


def check_pdf_size(n_bytes: int) -> tuple[bool, str]:
    """检查文件大小是否像一份完整年报。"""
    if n_bytes <= 0:
        return False, "文件大小为 0"
    if n_bytes < MIN_PDF_BYTES:
        return (
            False,
            f"文件仅 {n_bytes} 字节，小于阈值 {MIN_PDF_BYTES} 字节，疑似残缺或非年报正文",
        )
    return True, f"{n_bytes} 字节"


def preflight_reasons(
    candidate: Candidate, code: str, year: int = REPORT_YEAR
) -> list[str]:
    """下载前的硬性校验，返回「拒绝下载」的理由列表（空列表 = 允许下载）。

    这里只拦**结构性缺陷**——一旦出现，下回来的必定不是想要的文件：
    类别不是全文、地址缺失、附件不是 PDF、证券代码不符、年份不符。

    注意：标题里「未出现公司简称」之类的提示**不在此列**。那是给人看的
    核对提醒，不是机器能自动判定的错误；把它变成硬闸门会让 14 家里 12 家
    都无法下载，反而逼迫使用者绕过校验。
    """
    reasons: list[str] = []

    if candidate.kind != KIND_FULL:
        reasons.append(f"候选类别为「{candidate.kind}」，不是年度报告全文")

    if not candidate.url:
        reasons.append("候选记录没有官方附件地址，拒绝下载（绝不猜测地址）")

    if candidate.adjunct_type.upper() != "PDF":
        reasons.append(f"附件类型为「{candidate.adjunct_type or '未返回'}」而非 PDF，拒绝下载")

    if not candidate.sec_code:
        reasons.append("公告未返回证券代码，无法核对主体是否为目标公司")
    elif candidate.sec_code != code:
        reasons.append(f"公告证券代码 {candidate.sec_code} 与目标 {code} 不一致")

    if str(year) not in candidate.title:
        reasons.append(f"标题未出现目标年份 {year}")

    if any(w in candidate.title for w in ("摘要", "英文", "半年度", "半年报", "季度")):
        reasons.append("标题含「摘要/英文/半年度/季度」，不属于年度报告全文")

    return reasons


def download_one(
    session: requests.Session,
    candidate: Candidate,
    dest: Path,
    *,
    attempts: int = 3,
    force: bool = False,
) -> dict[str, str]:
    """下载单个候选 PDF 到 dest，返回结果记录。

    安全措施：请求超时、有限次重试、HTTP 状态检查、PDF 文件头验证、
    文件大小检查、失败原因记录、不覆盖已有且有效的 PDF（除非 force）、
    先写 .part 临时文件再原子改名。
    """
    if not candidate.url:
        return {"状态": "下载失败", "失败原因": "候选记录没有可用的附件地址"}

    if dest.exists() and not force:
        ok, why = looks_like_pdf(dest)
        size_ok, size_why = check_pdf_size(dest.stat().st_size)
        if ok and size_ok:
            return {
                "状态": "已存在，跳过",
                "文件名": dest.name,
                "字节数": str(dest.stat().st_size),
                "失败原因": "",
            }
        return {
            "状态": "下载失败",
            "失败原因": f"目标文件已存在但不是有效 PDF（{why}；{size_why}），"
            f"为避免覆盖，请人工确认后删除或加 --force",
        }

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.parent / f"{dest.name}.part"
    last_error = ""

    for attempt in range(attempts):
        try:
            with session.get(
                candidate.url,
                stream=True,
                timeout=(cninfo.CONNECT_TIMEOUT, 120),
            ) as resp:
                if resp.status_code != 200:
                    last_error = f"HTTP {resp.status_code}"
                    if 400 <= resp.status_code < 500 and resp.status_code != 429:
                        break  # 客户端错误不重试
                else:
                    ctype = resp.headers.get("Content-Type", "")
                    declared = resp.headers.get("Content-Length")
                    with part.open("wb") as f:
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            f.write(chunk)

                    ok, why = looks_like_pdf(part)
                    if not ok:
                        last_error = why
                        part.unlink(missing_ok=True)
                    else:
                        n = part.stat().st_size
                        size_ok, size_why = check_pdf_size(n)
                        if not size_ok:
                            last_error = size_why
                            part.unlink(missing_ok=True)
                        elif declared and declared.isdigit() and int(declared) != n:
                            last_error = f"实际字节数 {n} 与 Content-Length {declared} 不符"
                            part.unlink(missing_ok=True)
                        elif "pdf" not in ctype.lower() and ctype:
                            last_error = f"Content-Type 为 {ctype}，非 PDF"
                            part.unlink(missing_ok=True)
                        else:
                            os.replace(part, dest)
                            return {
                                "状态": "已下载",
                                "文件名": dest.name,
                                "字节数": str(n),
                                "失败原因": "",
                            }
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))

    part.unlink(missing_ok=True)
    return {"状态": "下载失败", "失败原因": last_error or "未知原因"}


def apply_download_result(
    company: dict[str, str],
    *,
    source_url: str,
    outcome: dict[str, str],
    report_year: int = REPORT_YEAR,
) -> dict[str, str]:
    """把下载结果写回该公司的行记录（返回新字典，不改动入参）。"""
    row = dict(company)
    filename = outcome.get("文件名", "")

    if outcome["状态"] == "已下载":
        row["来源URL"] = source_url
        row["下载状态"] = "已下载"
        row["文件名"] = filename
        row["下载时间"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row["文件字节数"] = outcome.get("字节数", "")
        row["解析状态"] = "未开始"
        row["解析输出文件"] = ""
        row["页码数"] = ""
    elif outcome["状态"] == "已存在，跳过":
        row["来源URL"] = row.get("来源URL") or source_url
        row["下载状态"] = "已下载"
        row["文件名"] = filename or row.get("文件名", "")
        row["文件字节数"] = outcome.get("字节数", "") or row.get("文件字节数", "")
    else:
        row["来源URL"] = source_url or row.get("来源URL", "")
        row["下载状态"] = "下载失败"
        reason = outcome.get("失败原因", "")
        if reason:
            row["备注"] = f"[下载失败] {reason}"[:200]
    return row


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _run_check(args: argparse.Namespace) -> int:
    companies = load_companies()
    if args.code:
        wanted = {c.strip() for c in args.code.split(",") if c.strip()}
        companies = [c for c in companies if c["证券代码"] in wanted]
        missing = wanted - {c["证券代码"] for c in companies}
        if missing:
            print(f"⚠ 名单中不存在这些证券代码：{sorted(missing)}")
        if not companies:
            print("没有匹配的公司，退出。")
            return 1

    user_agent = get_env("HTTP_USER_AGENT") or None
    session = cninfo.build_session(user_agent)

    print(_HEAVY)
    print("上市公司 2025 年年度报告 —— 候选公告检索（仅检索，不下载）")
    print(_HEAVY)
    print(f"来源：巨潮资讯网（证监会指定信息披露网站） {cninfo.CNINFO_BASE}")
    print(f"目标：{REPORT_YEAR} 年年度报告全文，共 {len(companies)} 家")
    print("说明：下列结果均为机器检索所得，全部标记为「待人工确认」。")
    print("      未找到的公司如实显示，不做推测，不生成任何下载地址。")

    started = time.time()
    results: list[CompanyResult] = []
    for i, company in enumerate(companies, 1):
        result = search_company_reports(session, company)
        results.append(result)
        _print_company(i, len(companies), result, REPORT_YEAR)
        if i < len(companies):
            time.sleep(args.delay)

    _print_summary(results, time.time() - started)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "检索时间": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "来源": cninfo.CNINFO_BASE,
            "目标年份": REPORT_YEAR,
            "说明": "机器检索结果，尚未经人工确认",
            "公司": [
                {
                    "证券代码": r.code,
                    "公司简称": r.name,
                    "交易所": r.exchange,
                    "orgId": r.org_id,
                    "巨潮简称": r.sec_name,
                    "状态": r.status,
                    "错误": r.error,
                    "疑点": r.doubts,
                    "全文候选": [asdict(c) for c in r.full],
                    "已排除": [asdict(c) for c in r.others],
                }
                for r in results
            ],
        }
        # newline="\n" 必须显式指定：Path.write_text 默认 newline=None，在 Windows
        # 上会把 "\n" 转成 "\r\n"，与 .gitattributes 的 eol=lf 约定不符（实测
        # 该文件曾写入 655 个 CR）。
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        print(f"\n  候选数据已导出：{out}")

    return 0 if not any(r.error for r in results) else 2


def _run_download(args: argparse.Namespace) -> int:
    companies = load_companies()
    if args.code:
        wanted = {c.strip() for c in args.code.split(",") if c.strip()}
        companies = [c for c in companies if c["证券代码"] in wanted]
    if not companies:
        print("没有匹配的公司，退出。")
        return 1

    # 下载模式必须显式确认，避免误触发批量下载
    if not args.yes:
        if not sys.stdin.isatty():
            print("非交互环境下必须显式加 --yes 才能执行下载。已中止。")
            return 1
        reply = input(f"即将下载 {len(companies)} 家公司的年报，确认？[y/N] ")
        if reply.strip().lower() not in {"y", "yes"}:
            print("已取消，未下载任何文件。")
            return 1

    delay = args.delay
    if delay is None:
        try:
            delay = float(get_env("DOWNLOAD_DELAY_SECONDS") or 3.0)
        except ValueError:
            delay = 3.0

    user_agent = get_env("HTTP_USER_AGENT") or None
    session = cninfo.build_session(user_agent)

    rows = {c["证券代码"]: c for c in load_companies()}
    downloaded = failures = 0

    for i, company in enumerate(companies, 1):
        code = company["证券代码"]
        name = company["公司简称"]
        print(f"\n[{i}/{len(companies)}] {name} {code}")

        result = search_company_reports(session, company)
        if len(result.full) != 1:
            print(f"  ⚠ {result.status}，跳过下载（不猜测地址）")
            if result.error:
                print(f"     {result.error}")
            failures += 1
            continue

        candidate = result.full[0]

        # 下载前硬性校验：证券代码、报告年份、全文类型、PDF 地址、附件类型
        blocking = preflight_reasons(candidate, code)
        if blocking:
            print("  ⚠ 下载前校验未通过，已拒绝下载（不猜测地址、不以摘要替代）：")
            for reason in blocking:
                print(f"     · {reason}")
            failures += 1
            continue

        dest = RAW_REPORTS_DIR / expected_report_filename(code, name)
        print(f"  目标文件：{dest.relative_to(RAW_REPORTS_DIR.parent.parent)}")
        outcome = download_one(session, candidate, dest, force=args.force)
        print(f"  {outcome['状态']}" + (f" — {outcome['失败原因']}" if outcome["失败原因"] else ""))

        rows[code] = apply_download_result(company, source_url=candidate.url, outcome=outcome)
        if outcome["状态"] in {"已下载", "已存在，跳过"}:
            downloaded += 1
        else:
            failures += 1

        # 每处理完一家立刻落盘，避免中途中断丢失进度
        save_companies([rows[c["证券代码"]] for c in load_companies()])
        time.sleep(delay)

    print(f"\n完成：成功 {downloaded} 家，失败/跳过 {failures} 家。")
    print("config/companies.csv 已按实际结果更新。")
    return 0 if failures == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.download",
        description="上市公司年报候选检索与下载。默认只检索，不下载。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m src.download --check\n"
            "  python -m src.download --check --code 000333\n"
            "  python -m src.download --check --json logs/candidates.json\n"
            "  python -m src.download --download --code 000333 --yes\n"
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="只检索并展示候选公告（默认模式）"
    )
    mode.add_argument(
        "--download", action="store_true", help="下载并回填 CSV（需 --yes 或交互确认）"
    )
    p.add_argument("--code", help="只处理指定证券代码，多个用逗号分隔，如 000333,000651")
    p.add_argument("--json", help="把候选数据导出为 JSON（仅 --check 模式）")
    p.add_argument(
        "--delay",
        type=float,
        default=None,
        help="每家之间的请求间隔秒数。--check 默认 1.0，--download 默认取 .env 的 "
        "DOWNLOAD_DELAY_SECONDS（3.0）",
    )
    p.add_argument("--force", action="store_true", help="下载时覆盖已存在的文件")
    p.add_argument("--yes", action="store_true", help="跳过交互确认（下载模式）")
    return p


def _make_output_robust() -> None:
    """保留此名以免影响既有调用点；实现已上移到 config，供各 CLI 共用。"""
    make_output_robust()


def main(argv: list[str] | None = None) -> int:
    _make_output_robust()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.delay is None and not args.download:
        args.delay = 1.0

    if args.download:
        return _run_download(args)
    return _run_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
