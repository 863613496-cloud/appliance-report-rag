"""巨潮资讯网（CNINFO）信息披露检索客户端。

巨潮资讯网是证监会指定的上市公司信息披露网站，沪深两市公告齐全，
因此作为本项目年报检索的首选官方渠道；上交所（www.sse.com.cn）、
深交所（www.szse.cn）官网作为后续人工核验来源。

本模块使用的两个接口均为 2026-10-14 实地探查确认，不是按记忆拼写的：

  1) 公司检索（用于取得 orgId）
     POST https://www.cninfo.com.cn/new/information/topSearch/query
     表单：keyWord=<证券代码>  maxNum=10
     返回：[{code, orgId, zwjc, category, ...}]

     ⚠ orgId 格式无规律可循，实测出现四种形态：
       9900005965（美的）、gssh0600690（海尔智家）、gssz0000921（海信家电）、
       GD134549（科沃斯）。因此 orgId 只能检索获取，绝不可按规则构造。

  2) 公告检索
     POST https://www.cninfo.com.cn/new/hisAnnouncement/query
     表单：stock=<证券代码>,<orgId>
           category=category_ndbg_szsh   ← 年度报告类别
           column=sse|szse               ← 沪市 / 深市
           seDate=<起>~<止>
     返回：announcements[]，每条含
           announcementTitle / announcementTime（毫秒时间戳）/ adjunctUrl /
           adjunctSize（单位 KB）/ adjunctType / secCode / secName / orgId

  3) 附件下载地址 = https://static.cninfo.com.cn/ + adjunctUrl
     实测：HTTP 200，Content-Type: application/pdf

本模块只负责「检索」，不做任何文件下载；下载相关校验在 src/download.py。
"""

from __future__ import annotations

import time
from typing import Any

import requests

CNINFO_BASE = "https://www.cninfo.com.cn"
CNINFO_STATIC = "https://static.cninfo.com.cn"

# 巨潮前端「年度报告」类别取值，实测 category_ndbg_szsh 返回且仅返回年度报告类公告
CATEGORY_ANNUAL_REPORT = "category_ndbg_szsh"

TOP_SEARCH_PATH = "/new/information/topSearch/query"
ANNOUNCEMENT_PATH = "/new/hisAnnouncement/query"

# 实测可穿的浏览器 UA。如需改用自定义 UA，可在 .env 中设置 HTTP_USER_AGENT
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

CONNECT_TIMEOUT = 10      # 秒
READ_TIMEOUT = 30         # 秒
MAX_ATTEMPTS = 4          # 含首次在内最多请求次数


class CninfoError(RuntimeError):
    """巨潮接口调用失败（网络异常、非 200 响应或响应不是合法 JSON）。"""


def build_session(user_agent: str | None = None) -> requests.Session:
    """构造带正确请求头的会话。缺少 Referer / X-Requested-With 会被接口拒绝。"""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
            "Referer": f"{CNINFO_BASE}/new/commonUrl?url=disclosure/list/notice",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
    )
    return session


def _post_json(
    session: requests.Session,
    path: str,
    data: dict[str, Any],
    *,
    attempts: int = MAX_ATTEMPTS,
) -> Any:
    """POST 表单并解析 JSON，带超时与有限次重试。

    重试策略：网络异常、HTTP 429、HTTP 5xx 会重试；
    HTTP 4xx（除 429 外）属客户端错误，立即失败不重试。
    """
    url = f"{CNINFO_BASE}{path}"
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            resp = session.post(url, data=data, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except requests.RequestException as exc:
            last_error = exc
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    last_error = CninfoError(f"响应不是合法 JSON：{exc}")
            elif 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise CninfoError(f"HTTP {resp.status_code}（客户端错误，不重试）：{url}")
            else:
                last_error = CninfoError(f"HTTP {resp.status_code}")

        if attempt < attempts - 1:
            # 递增退避：1.5s、3.0s、4.5s
            time.sleep(1.5 * (attempt + 1))

    raise CninfoError(
        f"请求失败（已重试 {attempts} 次）：{type(last_error).__name__}: {last_error}"
    )


def search_company(session: requests.Session, code: str) -> dict[str, Any] | None:
    """按证券代码检索公司，返回 A 股对应记录；找不到返回 None。

    返回记录中 orgId 供后续公告检索使用，zwjc 为巨潮使用的公司简称。
    """
    items = _post_json(session, TOP_SEARCH_PATH, {"keyWord": code, "maxNum": 10})
    for item in items or []:
        # 同一代码可能对应 A 股 / B 股 / 债券，只取 A 股且代码完全一致的记录
        if str(item.get("code")) == code and str(item.get("category", "")).startswith("A"):
            return item
    return None


def column_for_code(code: str) -> str:
    """按证券代码判断巨潮要求的 column 取值。6 开头为沪市，其余为深市。"""
    return "sse" if code.startswith("6") else "szse"


def query_annual_reports(
    session: requests.Session,
    code: str,
    org_id: str,
    column: str,
    year: int,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """检索指定年度的年度报告类公告（含全文、摘要、英文版）。

    检索窗口默认为 year-12-01 ~ (year+1)-12-31：
    上市公司上一年度年报通常在次年 1–4 月披露，个别延后或补发，
    留出宽窗口后再由上层按标题年份精确过滤。

    Returns:
        公告列表（原始记录，未做任何筛选或改写）。
    """
    payload = {
        "pageNum": 1,
        "pageSize": 30,
        "column": column,
        "tabName": "fulltext",
        "plate": "",
        "stock": f"{code},{org_id}",
        "searchkey": "",
        "secid": "",
        "category": CATEGORY_ANNUAL_REPORT,
        "trade": "",
        "seDate": f"{date_from or f'{year}-12-01'}~{date_to or f'{year + 1}-12-31'}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    data = _post_json(session, ANNOUNCEMENT_PATH, payload)
    return data.get("announcements") or []


def announcement_pdf_url(adjunct_url: str) -> str:
    """把公告的 adjunctUrl 拼成完整下载地址。"""
    if not adjunct_url:
        return ""
    if adjunct_url.startswith("http"):
        return adjunct_url
    return f"{CNINFO_STATIC}/{adjunct_url.lstrip('/')}"


def announcement_page_url(code: str, org_id: str) -> str:
    """该公司在巨潮的公告列表页地址，供人工复核时点开查看。"""
    return (
        f"{CNINFO_BASE}/new/commonUrl?url=disclosure/list/notice"
        f"&stock={code},{org_id}"
    )
