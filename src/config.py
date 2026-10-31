"""全局路径与配置常量。

本模块只做两件事：
1. 统一定义各目录位置，避免各脚本里写死相对路径；
2. 提供 .env 的读取入口。

安全约定：本模块只读取环境变量，从不打印、记录或回显其取值。
"""

from __future__ import annotations

import os
import sys
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

# ---------- 解析与切块参数 ----------
# 块长按**字符**计——中文财报里 1 个汉字≈1 个 token，按字符切比按词切更可控。
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

#: 表格页判定：一行「含阿拉伯数字且不含句末标点」即计为一个疑似表格行。
#: 一页中此类行的占比达到下表阈值即判为表格页。
#:
#: 阈值 0.20 是 **2026-10-16 在美的年报 276 页上实测校准**的结果，不是经验默认值：
#:   · 9 张已知表格页（p9/20/49/50/52/53/78/135/224）比值 0.323–0.778，全部通过；
#:   · 3 张已知正文页（p40/146/242）比值均 <0.10，全部落选。
#: 但实测分布是**连续谱而非双峰**，所以这只是启发式，不是干净分界。
#: 取值偏保守是刻意的——误判正文页为表格只是块大一点，误判表格页为正文会切散
#: 行列关系，两者代价不对称。
TABLE_PAGE_MIN_RATIO = 0.20


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


def make_output_robust() -> None:
    """让输出在被重定向到管道/文件时不会因编码而中止程序。

    背景（本机实测，2026-10-14）：本项目 CLI 的输出含 U+2713 ✓、U+26A0 ⚠、
    U+2B1C ⬜、U+25B6 ▶ 等 GBK 无法编码的字符。真实控制台下 Python 走
    _WindowsConsoleIO，不受影响；但输出一旦被重定向（管道/文件），sys.stdout
    会退回 locale 编码（本机 cp936），遇到这些字符即抛 UnicodeEncodeError
    **并中止整个程序**——实测崩在 download.py 的 _print_company()，
    导致其后的 --json 导出根本没执行，整次检索白跑。

    处置：仅在非终端输出时改用 UTF-8。真实控制台保持原样，以免把本该由
    控制台处理的 Unicode 攫为己有而显示成乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if stream.isatty():
                continue
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass  # 流不支持 reconfigure 时静默跳过，不影响主流程


def expected_report_filename(code: str, name: str, year: int = REPORT_YEAR) -> str:
    """按命名规范生成年报文件名，例如 000333_美的集团_2025_年度报告.pdf。"""
    return REPORT_FILENAME_PATTERN.format(证券代码=code, 公司简称=name, 报告年份=year)
