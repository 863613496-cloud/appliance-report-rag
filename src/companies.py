"""读取并校验 config/companies.csv 与 config/questions.csv。

这是骨架阶段少数已实现的模块之一：只做只读的加载与校验，
不下载、不解析、不生成任何数据。
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

from src.config import (
    COMPANIES_CSV,
    CSV_ENCODING,
    DOWNLOAD_STATUSES,
    EXPECTED_COMPANY_COUNT,
    EXPECTED_QUESTION_COUNT,
    PARSE_STATUSES,
    QUESTIONS_CSV,
    QUESTION_COLUMNS,
    COMPANY_COLUMNS,
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    """读取 CSV 为字典列表。证券代码等字段保持字符串，避免丢失前导零。"""
    if not path.exists():
        raise FileNotFoundError(f"找不到文件：{path}")
    with path.open("r", encoding=CSV_ENCODING, newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def load_companies(path: Path = COMPANIES_CSV) -> list[dict[str, str]]:
    """加载公司名单。"""
    return _read_csv(path)


def load_questions(path: Path = QUESTIONS_CSV) -> list[dict[str, str]]:
    """加载测试题。"""
    return _read_csv(path)


def cross_company_questions(questions: list[dict[str, str]]) -> list[dict[str, str]]:
    """筛选跨公司综合题。"""
    return [q for q in questions if str(q.get("是否跨公司", "")).strip() in {"是", "Y", "yes", "True"}]


def downloaded_count(companies: list[dict[str, str]]) -> int:
    """已下载的公司数。骨架阶段应为 0，不得人工填高。"""
    return sum(1 for c in companies if str(c.get("下载状态", "")).strip() == "已下载")


def validate_companies(companies: list[dict[str, str]]) -> list[str]:
    """校验公司名单，返回问题描述列表（空列表表示通过）。"""
    problems: list[str] = []

    if companies and tuple(companies[0].keys()) != COMPANY_COLUMNS:
        problems.append(
            f"companies.csv 表头与预期不一致：\n  实际：{tuple(companies[0].keys())}\n  预期：{COMPANY_COLUMNS}"
        )

    if len(companies) != EXPECTED_COMPANY_COUNT:
        problems.append(f"公司数量为 {len(companies)}，预期 {EXPECTED_COMPANY_COUNT} 家")

    seen_codes: set[str] = set()
    for row in companies:
        code = str(row.get("证券代码", "")).strip()
        name = str(row.get("公司简称", "")).strip() or "<缺公司简称>"

        if len(code) != 6 or not code.isdigit():
            problems.append(f"{name}：证券代码 {code!r} 不是 6 位数字（注意前导零是否丢失）")
        if code in seen_codes:
            problems.append(f"{name}：证券代码 {code} 重复")
        seen_codes.add(code)

        status = str(row.get("下载状态", "")).strip()
        if status and status not in DOWNLOAD_STATUSES:
            problems.append(f"{name}：下载状态 {status!r} 不在约定取值 {DOWNLOAD_STATUSES} 内")

        pstatus = str(row.get("解析状态", "")).strip()
        if pstatus and pstatus not in PARSE_STATUSES:
            problems.append(f"{name}：解析状态 {pstatus!r} 不在约定取值 {PARSE_STATUSES} 内")

        if str(row.get("解析状态", "")).strip() == "已解析" and not str(row.get("文件名", "")).strip():
            problems.append(f"{name}：标记为已解析但文件名为空，状态与记录不一致")

    return problems


def validate_questions(questions: list[dict[str, str]]) -> list[str]:
    """校验测试题，返回问题描述列表（空列表表示通过）。"""
    problems: list[str] = []

    if questions and tuple(questions[0].keys()) != QUESTION_COLUMNS:
        problems.append(f"questions.csv 表头与预期不一致：{tuple(questions[0].keys())}")

    if len(questions) != EXPECTED_QUESTION_COUNT:
        problems.append(f"题目数量为 {len(questions)}，预期 {EXPECTED_QUESTION_COUNT} 道")

    cross = cross_company_questions(questions)
    if len(cross) < 2:
        problems.append(f"跨公司综合题仅有 {len(cross)} 道，要求至少 2 道")

    seen_ids: set[str] = set()
    for row in questions:
        qid = str(row.get("题目编号", "")).strip()
        if not qid:
            problems.append("存在题目编号为空的记录")
        if qid in seen_ids:
            problems.append(f"题目编号 {qid} 重复")
        seen_ids.add(qid)
        if not str(row.get("题目", "")).strip():
            problems.append(f"{qid}：题目正文为空")

    return problems


def save_companies(companies: list[dict[str, str]], path: Path = COMPANIES_CSV) -> None:
    """把公司名单写回 CSV（下载阶段回填来源 URL / 状态 / 文件名等字段时使用）。

    写回的三条硬性要求：
      1. 使用 utf-8-sig 编码写出，保留 BOM，Excel 打开中文不乱码；
      2. 严格按 COMPANY_COLUMNS 的既定顺序写全部 17 列，不遗漏、不重排、不新增列；
      3. 证券代码一律按字符串写出，保留前导零（000333 不能变成 333）。

    实现要点：先写同目录临时文件再原子替换，中途失败不会破坏原文件；
    行终止符固定为 \\n，与当前仓库中的文件一致，保证「读入再写回」字节级不变。
    """
    unknown = set()
    for row in companies:
        unknown |= set(row) - set(COMPANY_COLUMNS)
    if unknown:
        raise ValueError(f"出现未定义的列，拒绝写回以免破坏表结构：{sorted(unknown)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.tmp"
    with tmp_path.open("w", encoding=CSV_ENCODING, newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=COMPANY_COLUMNS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in companies:
            writer.writerow({col: row.get(col, "") for col in COMPANY_COLUMNS})

    os.replace(tmp_path, path)
    problems = validate_companies(load_companies(path))
    if problems:
        raise ValueError("写回后的文件未通过校验：" + "；".join(problems))
