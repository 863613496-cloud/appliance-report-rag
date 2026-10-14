"""【未实现】从官方披露平台下载 14 家公司 2025 年年度报告 PDF。

状态：骨架占位，尚未编写任何下载逻辑。

计划采用的来源（按优先级）：
    1. 巨潮资讯网  https://www.cninfo.com.cn   —— 证监会指定信息披露网站，
       沪深两市公告齐全，适合批量按「证券代码 + 公告类别=年度报告」检索。
    2. 上交所      https://www.sse.com.cn      —— 沪市公司（600690/600336/603486/688169）核验来源。
    3. 深交所      https://www.szse.cn        —— 深市公司核验来源。

关键约束：
    * 必须下载**年度报告全文 PDF**，不得用「年度报告摘要」「业绩快报」替代；
      文件名中通常含「摘要」二字，实现时需据此排除。
    * 需在 PDF 首页确认标题为「XXXX 股份有限公司 2025 年年度报告」。
    * 各平台接口的具体参数需在实现时实地核实后写入，不要凭记忆写死。
    * 下载间隔不小于 DOWNLOAD_DELAY_SECONDS，礼貌访问公开服务器。

落地后需要回写 config/companies.csv 的字段：
    来源URL、下载状态、文件名、下载时间、文件字节数、页码数
文件命名规范见 src.config.REPORT_FILENAME_PATTERN。
"""

from __future__ import annotations

from src.companies import load_companies


def download_all(force: bool = False) -> list[dict[str, str]]:
    """下载全部缺失的年报，返回逐条结果。

    Args:
        force: 为 True 时忽略已下载记录重新下载。
    """
    raise NotImplementedError(
        "下载功能尚未实现。当前仅为项目骨架，请勿假定 data/raw/reports/ 中已有文件。"
    )


def download_one(code: str) -> dict[str, str]:
    """下载单家公司的年报。"""
    raise NotImplementedError("下载功能尚未实现。")


if __name__ == "__main__":
    companies = load_companies()
    print(f"待处理公司：{len(companies)} 家")
    print("提示：下载逻辑尚未实现，此脚本当前不会发起任何网络请求。")
