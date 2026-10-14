"""全局路径与配置常量。

本模块只做两件事：
1. 统一定义各目录位置，避免各脚本里写死相对路径；
2. 提供 .env 的读取入口。

安全约定：本模块只读取环境变量，从不打印、记录或回显其取值。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------- 目录 ----------
PROJECT_ROOT = Path(__file__).resolve().parents[1]

CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
RAW_REPORTS_DIR = RAW_DIR / "reports"
PROCESSED_DIR = DATA_DIR / "processed"
INDEX_DIR = PROJECT_ROOT / "indexes"
LOGS_DIR = PROJECT_ROOT / "logs"
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"
DELIVERABLES_DIR = PROJECT_ROOT / "deliverables"

# ---------- 关键文件 ----------
COMPANIES_CSV = CONFIG_DIR / "companies.csv"
QUESTIONS_CSV = CONFIG_DIR / "questions.csv"
EVALUATION_CSV = LOGS_DIR / "evaluation.csv"
ENV_FILE = PROJECT_ROOT / ".env"

# 统一使用 utf-8-sig：既能被 Excel 正确打开中文，也能被 pandas 正常读取
CSV_ENCODING = "utf-8-sig"

# ---------- 业务常量 ----------
REPORT_YEAR = 2025
REPORT_TYPE = "年度报告"
EXPECTED_COMPANY_COUNT = 14
EXPECTED_QUESTION_COUNT = 10

# ---------- 状态字段的取值约定（写入 CSV 时必须取其中之一）----------
DOWNLOAD_STATUSES = ("待下载", "下载中", "已下载", "下载失败", "需人工获取")
PARSE_STATUSES = ("未开始", "解析中", "已解析", "解析失败")
# 评测结论取值
VERDICTS = ("正确", "部分正确", "错误", "未评测")

# ---------- CSV 列定义（供校验与写入时复用）----------
COMPANY_COLUMNS = (
    "公司简称",
    "证券代码",
    "交易所",
    "板块",
    "报告年份",
    "报告类型",
    "披露平台",
    "核验来源",
    "来源URL",
    "下载状态",
    "文件名",
    "下载时间",
    "文件字节数",
    "解析状态",
    "解析输出文件",
    "页码数",
    "备注",
)

QUESTION_COLUMNS = (
    "题目编号",
    "题目类型",
    "题目",
    "涉及公司",
    "证券代码",
    "是否跨公司",
    "考察点",
    "预期出处",
    "备注",
)

EVALUATION_COLUMNS = (
    "题目编号",
    "题目",
    "参考答案",
    "实际召回文本块",
    "来源",
    "实际回答",
    "正确性",
    "引用正确性",
    "错误原因",
    "人工备注",
    "测试时间",
)

# ---------- 财报文件命名规范 ----------
REPORT_FILENAME_PATTERN = "{证券代码}_{公司简称}_{报告年份}_年度报告.pdf"


def load_dotenv_if_present() -> bool:
    """加载项目根目录的 .env。返回是否成功加载。"""
    if not ENV_FILE.exists():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        # 未安装 python-dotenv 时静默跳过，不阻断骨架自检
        return False
    load_dotenv(ENV_FILE)
    return True


def get_env(name: str, default: str | None = None) -> str | None:
    """读取单个环境变量。仅返回取值，不打印。"""
    return os.environ.get(name, default)


def expected_report_filename(code: str, name: str, year: int = REPORT_YEAR) -> str:
    """按命名规范生成年报文件名，例如 000333_美的集团_2025_年度报告.pdf。"""
    return REPORT_FILENAME_PATTERN.format(证券代码=code, 公司简称=name, 报告年份=year)
