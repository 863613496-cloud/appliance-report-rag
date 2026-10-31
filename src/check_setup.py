"""骨架自检脚本（只依赖标准库，无需先安装任何依赖）。

用法：
    python src/check_setup.py

检查内容：
    1. 目录结构是否齐全；
    2. config/companies.csv 是否为 14 家、证券代码是否为合法 6 位字符串；
    3. config/questions.csv 是否为 10 道、跨公司题是否≥2 道；
    4. logs/evaluation.csv 表头是否正确；
    5. .env 是否被 .gitignore 覆盖、工作区是否误存了真实密钥；
    6. 年报下载与解析进度（骨架阶段应全为 0，仅作如实统计）。

退出码：0 表示全部通过，1 表示存在问题。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.companies import (  # noqa: E402
    cross_company_questions,
    downloaded_count,
    load_companies,
    load_questions,
    validate_companies,
    validate_questions,
)
from src.config import (  # noqa: E402
    CSV_ENCODING,
    EVALUATION_COLUMNS,
    EVALUATION_CSV,
    INDEX_DIR,
    DELIVERABLES_DIR,
    LOGS_DIR,
    PROCESSED_DIR,
    PROJECT_ROOT as ROOT,
    QUESTIONS_CSV,
    RAW_REPORTS_DIR,
    SCREENSHOTS_DIR,
)

OK = "[ OK ]"
BAD = "[FAIL]"
WARN = "[WARN]"

problems: list[str] = []


def check_directories() -> None:
    print("\n== 1. 目录结构 ==")
    for path in (
        RAW_REPORTS_DIR,
        PROCESSED_DIR,
        INDEX_DIR,
        LOGS_DIR,
        SCREENSHOTS_DIR,
        DELIVERABLES_DIR,
    ):
        rel = path.relative_to(ROOT)
        if path.is_dir():
            print(f"{OK} {rel}/")
        else:
            print(f"{BAD} {rel}/ 不存在")
            problems.append(f"缺少目录：{rel}")


def check_companies() -> None:
    print("\n== 2. config/companies.csv ==")
    companies = load_companies()
    print(f"     共 {len(companies)} 条记录")
    issues = validate_companies(companies)
    for issue in issues:
        print(f"{BAD} {issue}")
        problems.append(issue)
    if not issues:
        print(f"{OK} 公司数量、代码格式与状态取值均合规")
    print(f"{WARN} 已下载年报：{downloaded_count(companies)} / {len(companies)}（骨架阶段应为 0）")


def check_questions() -> None:
    print("\n== 3. config/questions.csv ==")
    questions = load_questions()
    cross = cross_company_questions(questions)
    print(f"     共 {len(questions)} 道，其中跨公司综合题 {len(cross)} 道")
    issues = validate_questions(questions)
    if issues:
        for issue in issues:
            print(f"{BAD} {issue}")
        problems.extend(issues)
    else:
        print(f"{OK} 10 道题目、跨公司题数量满足要求")
    for q in cross:
        print(f"     · 跨公司题 {q['题目编号']}：{q['题目'][:28]}...")


def check_evaluation_template() -> None:
    print("\n== 4. logs/evaluation.csv 模板 ==")
    if not EVALUATION_CSV.exists():
        print(f"{BAD} 文件不存在：{EVALUATION_CSV.relative_to(ROOT)}")
        problems.append("缺少 logs/evaluation.csv")
        return

    lines = EVALUATION_CSV.read_text(encoding=CSV_ENCODING).splitlines()
    header = tuple(lines[0].split(",")) if lines else ()
    if header == EVALUATION_COLUMNS:
        print(f"{OK} 表头正确（{len(header)} 列）")
    else:
        print(f"{BAD} 表头不符：{header}")
        problems.append("evaluation.csv 表头不符")

    data_rows = [ln for ln in lines[1:] if ln.strip()]
    print(f"     已有数据行：{len(data_rows)} 行（当前应为 0，评测尚未开展）")


def check_secrets() -> None:
    print("\n== 5. 密钥与敏感信息 ==")
    gitignore = ROOT / ".gitignore"
    if not gitignore.exists():
        print(f"{BAD} 缺少 .gitignore")
        problems.append("缺少 .gitignore")
        return

    text = gitignore.read_text(encoding="utf-8")
    for pattern in (".env", "data/raw/**/*.pdf", "indexes/*", "logs/*"):
        if pattern in text:
            print(f"{OK} .gitignore 覆盖 {pattern}")
        else:
            print(f"{BAD} .gitignore 未覆盖 {pattern}")
            problems.append(f".gitignore 未覆盖 {pattern}")

    if (ROOT / ".env").exists():
        print(f"{WARN} 检测到 .env（已被忽略）；请确认未提交、未截图")
    else:
        print(f"{OK} 尚未创建 .env（骨架阶段无需创建）")

    # 扫描仓库内是否误存形似真实密钥的字符串
    key_pattern = re.compile(r"sk-[A-Za-z0-9_\-]{16,}")
    hits: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".zip"}:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if key_pattern.search(content):
            hits.append(path)

    if hits:
        for path in hits:
            print(f"{BAD} 疑似真实密钥出现在：{path.relative_to(ROOT)}")
            problems.append(f"{path.relative_to(ROOT)} 中疑似存在真实密钥")
    else:
        print(f"{OK} 未在项目文件中发现形似真实密钥的字符串")


def check_data_progress() -> None:
    print("\n== 6. 数据进度（如实统计）==")
    pdfs = sorted(RAW_REPORTS_DIR.glob("*.pdf"))
    parsed = sorted(PROCESSED_DIR.glob("*.jsonl"))
    print(f"     data/raw/reports/ 中 PDF：{len(pdfs)} 个")
    print(f"     data/processed/ 中解析产物（JSONL）：{len(parsed)} 个")
    if not pdfs:
        print(f"{WARN} 尚未下载任何年报——这是骨架阶段的预期状态")
        print("     下一步：实现 src/download.py 后从巨潮资讯网获取 14 份 2025 年年度报告")


def main() -> int:
    print("=" * 68)
    print("上市公司财报问答知识库 —— 骨架自检")
    print("=" * 68)

    check_directories()
    check_companies()
    check_questions()
    check_evaluation_template()
    check_secrets()
    check_data_progress()

    print("\n" + "=" * 68)
    if problems:
        print(f"结果：发现 {len(problems)} 个问题")
        for i, issue in enumerate(problems, 1):
            print(f"  {i}. {issue}")
        return 1
    print("结果：全部通过。")
    if (ROOT / ".git").exists():
        print("提示：Git 仓库已存在；索引、原始 PDF、解析产物和运行日志仍由 .gitignore 排除。")
    else:
        print("提示：尚未初始化 Git 仓库，.gitignore 需执行 git init 后才会生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
