"""纯逻辑部分的离线测试（不联网、不写仓库内文件）。

测试中使用的公告标题**全部来自 2026-10-14 对巨潮资讯网的真实检索结果**，
不是编造的样例；这样测的是真实数据里出现过的形态。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import requests

from src.companies import load_companies, save_companies
from src.config import COMPANIES_CSV
from src.download import (
    KIND_ENGLISH,
    KIND_FULL,
    KIND_OTHER,
    KIND_SUMMARY,
    KIND_UNCERTAIN,
    Candidate,
    apply_download_result,
    check_pdf_size,
    classify,
    download_one,
    looks_like_pdf,
    preflight_reasons,
)

YEAR = 2025
NAMES = ("美的集团", "美的集团股份有限公司")


class TestClassify(unittest.TestCase):
    """标题分类：全文 / 摘要 / 英文版 / 半年度 / 存疑。"""

    def test_plain_full_text_title(self):
        """美的集团的真实标题形态：只有年份和文种，不含公司名。"""
        kind, basis, doubts = classify("2025年年度报告", YEAR, NAMES)
        self.assertEqual(kind, KIND_FULL)
        self.assertIn("2025年年度报告", basis)
        # 标题里没有公司名，必须提示人工核对主体
        self.assertEqual(len(doubts), 1)
        self.assertIn("标题未出现任何公司名称", doubts[0])

    def test_full_name_only_title_still_needs_human_check(self):
        """石头科技的真实标题形态：只写全称、不含简称。

        标题出现「有限公司」不代表主体已被确认——任何公司名都可能含该词。
        此类情况必须保持为待人工核实，且提示文字不能错误地说「未出现公司名称」。
        """
        kind, _, doubts = classify(
            "北京石头世纪科技股份有限公司2025年年度报告", YEAR, ("石头科技",)
        )
        self.assertEqual(kind, KIND_FULL)
        self.assertEqual(len(doubts), 1)
        self.assertIn("全称特征词", doubts[0])
        self.assertIn("人工核对", doubts[0])
        self.assertNotIn("标题未出现任何公司名称", doubts[0])

    def test_full_text_with_company_name(self):
        """海尔智家的真实标题形态：含公司全称，不应产生疑点。"""
        kind, _, doubts = classify(
            "海尔智家股份有限公司2025年年度报告", YEAR, ("海尔智家", "海尔智家股份有限公司")
        )
        self.assertEqual(kind, KIND_FULL)
        self.assertEqual(doubts, [])

    def test_summary_is_excluded(self):
        kind, basis, _ = classify("2025年年度报告摘要", YEAR, NAMES)
        self.assertEqual(kind, KIND_SUMMARY)
        self.assertIn("摘要", basis)

    def test_summary_without_nian(self):
        """石头科技的真实形态：「2025年度报告摘要」中间没有「年」字。"""
        kind, _, _ = classify(
            "北京石头世纪科技股份有限公司2025年度报告摘要",
            YEAR,
            ("石头科技", "北京石头世纪科技股份有限公司"),
        )
        self.assertEqual(kind, KIND_SUMMARY)

    def test_english_version_is_not_full_text(self):
        """老板电器/格力电器的真实形态：英文版不含「摘要」，容易被误判为全文。"""
        kind, basis, _ = classify("2025年年度报告（英文版）", YEAR, NAMES)
        self.assertEqual(kind, KIND_ENGLISH)
        self.assertIn("英文", basis)

    def test_english_summary_counts_as_summary(self):
        """九阳股份的真实形态：摘要（英文版），应归入摘要而非英文版。"""
        kind, _, _ = classify("2025年年度报告摘要（英文版）", YEAR, NAMES)
        self.assertEqual(kind, KIND_SUMMARY)

    def test_wrong_year_is_uncertain(self):
        kind, basis, doubts = classify("2024年年度报告", YEAR, NAMES)
        self.assertEqual(kind, KIND_UNCERTAIN)
        self.assertIn("2024", basis)
        self.assertTrue(doubts)

    def test_semiannual_is_not_annual(self):
        """「半年度报告」含「年度报告」子串，必须先被判为非年度报告。"""
        for title in ("2025年半年度报告", "2025年半年度报告摘要", "2025年第一季度报告"):
            with self.subTest(title=title):
                kind, _, _ = classify(title, YEAR, NAMES)
                self.assertEqual(kind, KIND_OTHER)

    def test_unrelated_announcement(self):
        kind, _, _ = classify("关于召开2025年年度股东大会的通知", YEAR, NAMES)
        self.assertEqual(kind, KIND_OTHER)


class TestPdfChecks(unittest.TestCase):
    """PDF 文件头与大小校验。"""

    def _write(self, data: bytes) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "sample.pdf"
        tmp.write_bytes(data)
        return tmp

    def test_valid_pdf_header(self):
        ok, why = looks_like_pdf(self._write(b"%PDF-1.7\n1 0 obj\n"))
        self.assertTrue(ok, why)

    def test_html_error_page_rejected(self):
        ok, why = looks_like_pdf(self._write(b"<html><body>404 Not Found</body></html>"))
        self.assertFalse(ok)
        self.assertIn("不是 PDF", why)

    def test_empty_file_rejected(self):
        ok, why = looks_like_pdf(self._write(b""))
        self.assertFalse(ok)
        self.assertIn("为空", why)

    def test_truncated_magic_rejected(self):
        ok, _ = looks_like_pdf(self._write(b"%PD"))
        self.assertFalse(ok)

    def test_size_thresholds(self):
        self.assertFalse(check_pdf_size(0)[0])
        self.assertFalse(check_pdf_size(50 * 1024)[0])       # 50 KB，小于阈值
        self.assertTrue(check_pdf_size(5 * 1024 * 1024)[0])  # 5 MB，正常年报量级


class _FakeResponse:
    """最小的 requests.Response 替身，用于离线测试下载的失败路径。"""

    def __init__(self, *, status_code=200, headers=None, body=b"", raise_at=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self._raise_at = raise_at

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_content(self, chunk_size=1):
        if self._raise_at is not None:
            raise self._raise_at
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class _FakeSession:
    """按顺序返回预置响应；用完后重复最后一个。不联网。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, **kwargs):
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        if isinstance(resp, Exception):
            raise resp
        return resp


VALID_BODY = b"%PDF-1.7\n" + b"0" * (200 * 1024)  # 约 200 KB，超过 100 KB 阈值


def _candidate(**overrides) -> Candidate:
    kwargs = dict(
        kind=KIND_FULL,
        title="2025年年度报告",
        publish_date="2026-03-31",
        url="https://static.cninfo.com.cn/finalpage/2026-03-31/1225065145.PDF",
        size_kb=5804,
        adjunct_type="PDF",
        basis="",
        sec_code="000333",
        sec_name="美的集团",
    )
    kwargs.update(overrides)
    return Candidate(**kwargs)


class TestPreflight(unittest.TestCase):
    """下载前的硬性校验必须拦住结构性缺陷。"""

    def test_normal_candidate_passes(self):
        self.assertEqual(preflight_reasons(_candidate(), "000333"), [])

    def test_title_only_doubt_does_not_block(self):
        """「标题未出现简称」只是给人看的提醒，不能变成拒绝下载的理由。"""
        c = _candidate(doubts=["标题未出现任何公司名称（目标简称为「美的集团」）…"])
        self.assertEqual(preflight_reasons(c, "000333"), [])

    def test_missing_url_blocks(self):
        self.assertTrue(preflight_reasons(_candidate(url=""), "000333"))

    def test_non_pdf_adjunct_blocks(self):
        self.assertTrue(preflight_reasons(_candidate(adjunct_type="DOC"), "000333"))

    def test_wrong_sec_code_blocks(self):
        reasons = preflight_reasons(_candidate(sec_code="000651"), "000333")
        self.assertTrue(any("不一致" in r for r in reasons))

    def test_missing_sec_code_blocks(self):
        self.assertTrue(preflight_reasons(_candidate(sec_code=""), "000333"))

    def test_wrong_year_blocks(self):
        self.assertTrue(preflight_reasons(_candidate(title="2024年年度报告"), "000333"))

    def test_summary_or_english_blocks(self):
        for title in ("2025年年度报告摘要", "2025年年度报告（英文版）", "2025年半年度报告"):
            with self.subTest(title=title):
                # 即使 kind 被上游误标为全文，标题检查仍须拦住
                self.assertTrue(preflight_reasons(_candidate(title=title), "000333"))

    def test_non_full_kind_blocks(self):
        self.assertTrue(preflight_reasons(_candidate(kind=KIND_SUMMARY), "000333"))


class TestDownloadLeavesNoPartialFile(unittest.TestCase):
    """下载失败时不得留下任何不完整文件（半截 .part 或损坏的目标文件）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dest = self.tmp / "000333_美的集团_2025_年度报告.pdf"

    def _leftovers(self):
        return sorted(p.name for p in self.tmp.iterdir())

    def test_success_writes_file_and_no_part(self):
        session = _FakeSession(
            [_FakeResponse(headers={"Content-Type": "application/pdf",
                                    "Content-Length": str(len(VALID_BODY))},
                           body=VALID_BODY)]
        )
        out = download_one(session, _candidate(), self.dest, attempts=1)
        self.assertEqual(out["状态"], "已下载")
        self.assertEqual(out["字节数"], str(len(VALID_BODY)))
        self.assertEqual(self._leftovers(), [self.dest.name], "不应残留 .part 文件")
        self.assertTrue(looks_like_pdf(self.dest)[0])

    def test_html_error_page_leaves_nothing(self):
        body = b"<html><body>502 Bad Gateway</body></html>"
        session = _FakeSession(
            [_FakeResponse(headers={"Content-Type": "text/html",
                                    "Content-Length": str(len(body))}, body=body)]
        )
        out = download_one(session, _candidate(), self.dest, attempts=1)
        self.assertEqual(out["状态"], "下载失败")
        self.assertEqual(self._leftovers(), [], "失败后不得留下任何文件")

    def test_truncated_large_file_leaves_nothing(self):
        """最危险的情形：5.9 MB 的 PDF 截断在 3 MB。

        此时文件头仍是 %PDF-、大小也远超 100 KB 阈值，前两道检查都会放行，
        **只有 Content-Length 比对**能发现不完整。必须确认这条检查真的生效。
        """
        partial = b"%PDF-1.7\n" + b"0" * (150 * 1024)  # 150 KB，已过大小阈值
        session = _FakeSession(
            [_FakeResponse(headers={"Content-Type": "application/pdf",
                                    "Content-Length": str(5_942_399)},
                           body=partial)]
        )
        out = download_one(session, _candidate(), self.dest, attempts=1)
        self.assertEqual(out["状态"], "下载失败")
        self.assertIn("不符", out["失败原因"])
        self.assertEqual(self._leftovers(), [], "被截断的文件不得留存")

    def test_tiny_body_rejected_by_size(self):
        """声明与实收都只有 1 KB 的「PDF」——由大小阈值拦下。"""
        tiny = b"%PDF-1.7\n" + b"0" * 1024
        session = _FakeSession(
            [_FakeResponse(headers={"Content-Type": "application/pdf",
                                    "Content-Length": str(len(tiny))}, body=tiny)]
        )
        out = download_one(session, _candidate(), self.dest, attempts=1)
        self.assertEqual(out["状态"], "下载失败")
        self.assertIn("小于阈值", out["失败原因"])
        self.assertEqual(self._leftovers(), [])

    def test_connection_drop_mid_stream_leaves_nothing(self):
        session = _FakeSession(
            [_FakeResponse(raise_at=requests.ConnectionError("连接中断"))]
        )
        out = download_one(session, _candidate(), self.dest, attempts=1)
        self.assertEqual(out["状态"], "下载失败")
        self.assertEqual(self._leftovers(), [])

    def test_http_404_leaves_nothing(self):
        session = _FakeSession([_FakeResponse(status_code=404)])
        out = download_one(session, _candidate(), self.dest, attempts=1)
        self.assertEqual(out["状态"], "下载失败")
        self.assertEqual(session.calls, 1, "4xx 不应重试")
        self.assertEqual(self._leftovers(), [])

    def test_failed_forced_redownload_keeps_good_file(self):
        """--force 重下失败时，原有的有效 PDF 必须原封不动。"""
        self.dest.write_bytes(VALID_BODY)
        body = b"<html>error</html>"
        session = _FakeSession(
            [_FakeResponse(headers={"Content-Type": "text/html",
                                    "Content-Length": str(len(body))}, body=body)]
        )
        out = download_one(session, _candidate(), self.dest, attempts=1, force=True)
        self.assertEqual(out["状态"], "下载失败")
        self.assertEqual(self.dest.read_bytes(), VALID_BODY, "原有有效文件被破坏")
        self.assertEqual(self._leftovers(), [self.dest.name])

    def test_existing_broken_file_is_not_overwritten(self):
        self.dest.write_bytes(b"<html>not a pdf</html>")
        session = _FakeSession([_FakeResponse(headers={"Content-Type": "application/pdf"},
                                              body=VALID_BODY)])
        out = download_one(session, _candidate(), self.dest, attempts=1)
        self.assertEqual(out["状态"], "下载失败")
        self.assertEqual(session.calls, 0, "检测到无效旧文件后不应发起请求")
        self.assertEqual(self.dest.read_bytes(), b"<html>not a pdf</html>")


#: 回填测试用的**虚构**公司，刻意不用任何真实公司，避免测试输入与仓库数据挂钩。
TEST_SEC_CODE = "900000"
TEST_NAME = "测试样例公司"
TEST_URL = "https://example.invalid/finalpage/2026-03-31/0000000000.PDF"


def _pending_row() -> dict[str, str]:
    """构造一行「尚未下载」的记录，字段与 config/companies.csv 完全一致。

    本函数**不读任何文件**。历史上这里用的是 ``load_companies()[0]``，
    2026-10-14 美的集团那行被回填为「已下载」并写入文件名后，「失败时不得
    写入文件名」这条断言因数据漂移而失效——测试的输入不应随真实数据的
    状态改变，否则同一份代码在数据推进后会悄悄失去覆盖。
    """
    return {
        "公司简称": TEST_NAME,
        "证券代码": TEST_SEC_CODE,
        "交易所": "深交所",
        "板块": "主板",
        "报告年份": "2025",
        "报告类型": "年度报告",
        "披露平台": "巨潮资讯网",
        "核验来源": "巨潮资讯网",
        "来源URL": "",
        "下载状态": "待下载",
        "文件名": "",
        "下载时间": "",
        "文件字节数": "",
        "解析状态": "未开始",
        "解析输出文件": "",
        "页码数": "",
        "备注": "",
    }


class TestApplyDownloadResult(unittest.TestCase):
    """元数据回填：只有真正成功下载的记录才能被标记为已下载。

    本类不把 ``config/companies.csv`` 当作测试数据源——样本行由
    :func:`_pending_row` 现造，因此**不要求**仓库里存在「待下载」记录，
    也不会因为它不存在而失败。真实数据文件的只读校验见
    :meth:`test_operations_do_not_touch_the_real_csv`。
    """

    def setUp(self):
        self.row = _pending_row()

    def test_failure_is_not_marked_downloaded(self):
        row = apply_download_result(
            self.row,
            source_url=TEST_URL,
            outcome={"状态": "下载失败", "失败原因": "HTTP 404"},
        )
        self.assertEqual(row["下载状态"], "下载失败")
        self.assertEqual(row["文件名"], "", "失败时不得写入文件名")
        self.assertEqual(row["文件字节数"], "", "失败时不得写入字节数")
        self.assertEqual(row["下载时间"], "", "失败时不得写入下载时间")
        self.assertIn("HTTP 404", row["备注"])

    def test_success_fills_all_metadata(self):
        row = apply_download_result(
            self.row,
            source_url=TEST_URL,
            outcome={"状态": "已下载", "文件名": "000333_美的集团_2025_年度报告.pdf",
                     "字节数": "5942399", "失败原因": ""},
        )
        self.assertEqual(row["下载状态"], "已下载")
        self.assertEqual(row["来源URL"], TEST_URL)
        self.assertEqual(row["文件名"], "000333_美的集团_2025_年度报告.pdf")
        self.assertEqual(row["文件字节数"], "5942399")
        self.assertEqual(row["解析状态"], "未开始")
        self.assertTrue(row["下载时间"], "成功下载必须记录下载时间")
        self.assertRegex(row["下载时间"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_other_columns_untouched(self):
        row = apply_download_result(
            self.row,
            source_url="u",
            outcome={"状态": "已下载", "文件名": "f.pdf", "字节数": "1", "失败原因": ""},
        )
        for col in ("公司简称", "证券代码", "交易所", "板块", "报告年份", "报告类型",
                    "披露平台", "核验来源"):
            self.assertEqual(row[col], self.row[col], f"{col} 不应被下载流程改动")

    def test_apply_returns_new_dict_without_mutating_input(self):
        """回填返回新字典，入参对象本身不得被改动——调用方可能还在用它。"""
        row = apply_download_result(
            self.row,
            source_url=TEST_URL,
            outcome={"状态": "已下载", "文件名": "f.pdf", "字节数": "1", "失败原因": ""},
        )
        self.assertIsNot(row, self.row)
        self.assertEqual(self.row["下载状态"], "待下载")
        self.assertEqual(self.row["文件名"], "")

    def test_operations_do_not_touch_the_real_csv(self):
        """跑一遍成功与失败两条回填路径后，真实 CSV 必须逐字节不变。

        这是「测试不写真实数据文件」这条约束的可执行证据，而不是靠约定。
        顺带确认本类用的样本行确实不在真实数据里（否则下面这次比对没有意义）。
        """
        before = COMPANIES_CSV.read_bytes()
        real_text = before.decode("utf-8-sig")

        self.assertNotIn(TEST_SEC_CODE, real_text, "测试样本的证券代码不该出现在真实数据里")
        self.assertNotIn(TEST_NAME, real_text, "测试样本的公司简称不该出现在真实数据里")

        apply_download_result(
            self.row, source_url=TEST_URL,
            outcome={"状态": "已下载", "文件名": "f.pdf", "字节数": "1", "失败原因": ""},
        )
        apply_download_result(
            self.row, source_url=TEST_URL,
            outcome={"状态": "下载失败", "失败原因": "HTTP 500"},
        )
        self.assertEqual(COMPANIES_CSV.read_bytes(), before,
                         "回填测试改动了真实的 config/companies.csv")


class TestCompaniesCsvRoundTrip(unittest.TestCase):
    """CSV 写回必须不丢列、不重排、保留 BOM 与证券代码前导零。"""

    def test_roundtrip_is_byte_identical(self):
        original = COMPANIES_CSV.read_bytes()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "companies.csv"
            tmp.write_bytes(original)
            save_companies(load_companies(tmp), tmp)
            self.assertEqual(tmp.read_bytes(), original, "读入再写回后文件字节应完全一致")

    def test_bom_and_leading_zeros_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "companies.csv"
            tmp.write_bytes(COMPANIES_CSV.read_bytes())
            save_companies(load_companies(tmp), tmp)

            self.assertTrue(tmp.read_bytes().startswith(b"\xef\xbb\xbf"), "BOM 丢失")
            codes = [row["证券代码"] for row in load_companies(tmp)]
            self.assertIn("000333", codes)
            self.assertNotIn("333", codes, "证券代码前导零丢失")
            self.assertEqual(len(codes), 14)
            self.assertEqual(len(set(codes)), 14, "证券代码出现重复")

    def test_unknown_column_is_rejected(self):
        rows = load_companies(COMPANIES_CSV)
        rows[0]["计划新增的列"] = "x"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "companies.csv"
            tmp.write_bytes(COMPANIES_CSV.read_bytes())
            with self.assertRaises(ValueError):
                save_companies(rows, tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
