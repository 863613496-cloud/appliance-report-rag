"""答案模块的离线单元测试。

这些测试只使用手工构造的最小检索块，不读取索引、不访问网络、不写仓库文件。
测试目的是验证规则边界，不代表真实年报答案准确率。
"""

from __future__ import annotations

import unittest

from src.answer import (
    STATUS_ANSWERABLE,
    STATUS_BLOCKED,
    STATUS_CONFLICT,
    STATUS_INSUFFICIENT,
    STATUS_NEEDS_DEFINITION,
    STATUS_NEEDS_VERIFICATION,
    answer_from_results,
    summarize_answers,
)

YEAR = 2025


def make_header(
    *,
    needs_context: bool = False,
    source: str = "页内已有表头",
    header_page: int | None = None,
    reliability: str = "",
) -> dict:
    return {
        "需要表头上下文": needs_context,
        "表头来源": source,
        "表头页": header_page,
        "关联可靠性": reliability,
    }


def make_result(
    code: str,
    name: str,
    page: int,
    text: str,
    *,
    blocked: bool = False,
    reliable: bool = True,
    unit_lines: list[str] | None = None,
    header: dict | None = None,
    contexts: list[dict] | None = None,
) -> dict:
    return {
        "doc_id": f"{code}_{YEAR}_p{page:04d}_c0001",
        "证券代码": code,
        "公司简称": name,
        "报告年份": YEAR,
        "PDF文件名": f"{code}_{YEAR}.pdf",
        "页码": page,
        "块序号": 1,
        "类型": "table",
        "章节": "",
        "字符数": len(text),
        "正文": text,
        "需复核": False,
        "日期恢复": False,
        "warnings": [],
        "anomalies": [],
        "blocking_anomalies": [],
        "blocked": blocked,
        "reliable_for_deterministic_answer": reliable,
        "table": {
            "整页保留": False,
            "行数": len(text.splitlines()),
            "含数字行数": 0,
            "单位行": unit_lines or [],
            "表头跨行警告": [],
            "说明": "",
        },
        "source": {},
        "scores": {},
        "检索范围": {"题面识别公司": [name]},
        "检索意图": [],
        "跨页表头": header or make_header(),
        "上下文片段": contexts or [],
        "口径提示": [],
    }


def make_context(
    code: str,
    name: str,
    page: int,
    text: str,
    *,
    status: str = "算法判定，待人工核验",
) -> dict:
    return {
        "doc_id": f"{code}_{YEAR}_p{page:04d}_c0001",
        "证券代码": code,
        "公司简称": name,
        "报告年份": YEAR,
        "页码": page,
        "正文": text,
        "blocked": False,
        "reliable_for_deterministic_answer": False,
        "上下文关联状态": status,
    }


def labels(answer: dict) -> list[str]:
    return [metric["指标原文"] for metric in answer["指标"]]


class TestAnswerable(unittest.TestCase):
    def test_single_company_total_revenue_and_net_profit(self) -> None:
        result = make_result(
            "000333",
            "美的集团",
            100,
            "\n".join(
                [
                    "2025 年度合并及公司利润表",
                    "单位：元",
                    "一、营业总收入 1,000,000 900,000",
                    "归属于母公司股东的净利润 100,000 80,000",
                ]
            ),
            unit_lines=["单位：元"],
        )
        answer = answer_from_results(
            "美的集团2025年营业总收入与归属于母公司股东的净利润分别是多少？同比变动如何？",
            [result],
            "Q01",
        )
        self.assertEqual(answer["回答状态"], STATUS_ANSWERABLE)
        self.assertTrue(answer["确定性作答"])
        self.assertEqual(answer["检索范围证券代码"], ["000333"])
        self.assertEqual(answer["指标"][0]["数值"], "1,000,000")
        self.assertEqual(answer["指标"][0]["单位"], "元")
        self.assertIn("营业总收入", labels(answer))
        self.assertIn("归母净利润同比", labels(answer))
        self.assertTrue(all(m["是否采信"] for m in answer["指标"]))


class TestDefinitionDifference(unittest.TestCase):
    def test_domestic_overseas_label_mismatch_needs_definition(self) -> None:
        result = make_result(
            "600690",
            "海尔智家",
            42,
            "\n".join(
                [
                    "主营业务分地区情况",
                    "单位：元",
                    "分地区",
                    "国内 600,000,000 400,000,000 33.33",
                    "海外 400,000,000 250,000,000 37.50",
                ]
            ),
            unit_lines=["单位：元"],
        )
        answer = answer_from_results(
            "海尔智家2025年境内收入与境外收入分别是多少？各自占比如何？",
            [result],
            "Q03",
        )
        self.assertEqual(answer["回答状态"], STATUS_NEEDS_DEFINITION)
        self.assertTrue(answer["确定性作答"])
        self.assertIn("国内", labels(answer))
        self.assertIn("海外", labels(answer))
        ratio_values = [
            m["数值"] for m in answer["指标"] if m["单位"] == "%"
        ]
        self.assertEqual(ratio_values, ["60.00%", "40.00%"])
        self.assertTrue(any("计算值" in note for note in answer["口径说明"]))


class TestPendingHeader(unittest.TestCase):
    def test_cross_page_header_is_not_human_confirmed(self) -> None:
        context = make_context(
            "002959",
            "小熊电器",
            9,
            "（1） 营业收入构成\n单位：元",
        )
        result = make_result(
            "002959",
            "小熊电器",
            10,
            "\n".join(
                [
                    "营业收入合计 100,000,000 100% 90,000,000 100%",
                    "分销售模式",
                    "线上销售 80,000,000 80% 70,000,000 70%",
                    "线下销售 20,000,000 20% 30,000,000 30%",
                ]
            ),
            unit_lines=["单位：元"],
            header=make_header(
                needs_context=True,
                source="上下文",
                header_page=9,
                reliability="算法判定，待人工核验",
            ),
            contexts=[context],
        )
        answer = answer_from_results(
            "小熊电器2025年线上销售渠道实现的营业收入占比是多少？",
            [result],
            "Q07",
        )
        self.assertEqual(answer["回答状态"], STATUS_NEEDS_VERIFICATION)
        self.assertFalse(answer["确定性作答"])
        ratio = next(
            m
            for m in answer["指标"]
            if m["指标原文"] == "线上销售收入占营业收入比重"
        )
        self.assertEqual(ratio["数值"], "80.00%")
        self.assertEqual(ratio["表头来源页码"], 9)
        self.assertFalse(ratio["是否采信"])
        self.assertEqual(ratio["表头核验状态"], "待人工核验")
        self.assertIn("不得标记为人工已确认", answer["警告"][0])


class TestNumericConflict(unittest.TestCase):
    def test_two_product_tables_are_reported_not_resolved(self) -> None:
        page_a = make_result(
            "002032",
            "苏泊尔",
            10,
            "\n".join(
                [
                    "（1） 营业收入构成",
                    "单位：元",
                    "分产品",
                    "烹饪电器 100,000,000 50%",
                    "食物料理电器 50,000,000 25%",
                ]
            ),
            unit_lines=["单位：元"],
        )
        page_b = make_result(
            "002032",
            "苏泊尔",
            11,
            "\n".join(
                [
                    "（2） 占公司营业收入或营业利润 10%以上的行业、产品、地区、销售模式的情况",
                    "单位：元",
                    "分产品",
                    "烹饪电器 110,000,000 60,000,000 45%",
                    "食物料理电器 50,000,000 30,000,000 40%",
                ]
            ),
            unit_lines=["单位：元"],
        )
        answer = answer_from_results(
            "苏泊尔2025年主要产品品类的营业收入构成如何？",
            [page_a, page_b],
            "Q04",
        )
        self.assertEqual(answer["回答状态"], STATUS_CONFLICT)
        self.assertFalse(answer["确定性作答"])
        self.assertIn("差异已发现、原因未明", answer["答案正文"])
        self.assertNotIn("选择表A", answer["答案正文"])
        self.assertEqual(answer["审计"]["表A"]["页码"], [10])
        self.assertEqual(answer["审计"]["表B"]["页码"], [11])
        self.assertEqual(answer["审计"]["合计差额"], "+10,000,000")
        conflict_metrics = [
            m for m in answer["指标"] if m["指标原文"] == "烹饪电器"
        ]
        self.assertTrue(conflict_metrics)
        self.assertTrue(all(not m["是否采信"] for m in conflict_metrics))


class TestBlockedEvidence(unittest.TestCase):
    def test_blocked_rd_page_is_not_confirmed(self) -> None:
        result = make_result(
            "603486",
            "科沃斯",
            20,
            "\n".join(
                [
                    "研发投入情况表",
                    "单位：元",
                    "研发投入合计 100,000,000",
                    "研发投入总额占营业收入比例（%） 5",
                    "营业收入 2,000,000,000",
                ]
            ),
            blocked=True,
            reliable=False,
            unit_lines=["单位：元"],
        )
        answer = answer_from_results(
            "科沃斯2025年研发投入金额是多少？占当期营业收入的比例是多少？",
            [result],
            "Q05",
        )
        self.assertEqual(answer["回答状态"], STATUS_BLOCKED)
        self.assertFalse(answer["确定性作答"])
        self.assertTrue(answer["指标"])
        self.assertTrue(all(m["blocked"] for m in answer["指标"]))
        self.assertTrue(all(not m["是否采信"] for m in answer["指标"]))
        self.assertTrue(any("阻断" in reason for reason in answer["拒答原因"]))


class TestMissingEvidence(unittest.TestCase):
    def test_air_conditioner_absent_is_refused(self) -> None:
        result = make_result(
            "000651",
            "格力电器",
            24,
            "\n".join(
                [
                    "（1） 营业收入构成",
                    "单位：元",
                    "分产品",
                    "消费电器 100,000,000 50%",
                ]
            ),
            unit_lines=["单位：元"],
        )
        answer = answer_from_results(
            "格力电器2025年空调业务的营业收入与毛利率分别是多少？该业务占公司总营收的比重是多少？",
            [result],
            "Q02",
        )
        self.assertEqual(answer["回答状态"], STATUS_INSUFFICIENT)
        self.assertFalse(answer["确定性作答"])
        self.assertIn("披露不足", answer["答案正文"])
        self.assertTrue(answer["指标"])
        self.assertTrue(all(not m["是否采信"] for m in answer["指标"]))
        self.assertNotIn("空调收入", labels(answer))


class TestCrossCompanyIsolation(unittest.TestCase):
    def test_result_from_another_company_is_filtered(self) -> None:
        meide = make_result(
            "000333",
            "美的集团",
            100,
            "\n".join(
                [
                    "2025 年度合并及公司利润表",
                    "单位：元",
                    "一、营业总收入 1,000,000 900,000",
                    "归属于母公司股东的净利润 100,000 80,000",
                ]
            ),
            unit_lines=["单位：元"],
        )
        gree = make_result(
            "000651",
            "格力电器",
            100,
            "\n".join(
                [
                    "2025 年度合并及公司利润表",
                    "单位：元",
                    "一、营业总收入 2,000,000 1,800,000",
                    "归属于母公司股东的净利润 200,000 180,000",
                ]
            ),
            unit_lines=["单位：元"],
        )
        answer = answer_from_results(
            "美的集团2025年营业总收入与归属于母公司股东的净利润分别是多少？同比变动如何？",
            [meide, gree],
            "Q01",
        )
        self.assertEqual(answer["检索范围证券代码"], ["000333"])
        self.assertTrue(answer["指标"])
        self.assertTrue(all(m["证券代码"] == "000333" for m in answer["指标"]))
        self.assertNotIn("000651", answer["证券代码"])


class TestOverseasComparison(unittest.TestCase):
    def test_blocked_overseas_row_does_not_block_independent_rd(self) -> None:
        ecovacs_region = make_result(
            "603486",
            "科沃斯",
            20,
            "\n".join(
                [
                    "主营业务分地区情况",
                    "单位：元",
                    "分地区",
                    "境外 500,000,000 300,000,000 40",
                    "境内 500,000,000 300,000,000 40",
                ]
            ),
            blocked=True,
            reliable=False,
            unit_lines=["单位：元"],
        )
        ecovacs_rd = make_result(
            "603486",
            "科沃斯",
            24,
            "\n".join(
                [
                    "研发投入情况表",
                    "单位：元",
                    "研发投入合计 80,000,000",
                    "研发投入总额占营业收入比例（%） 5",
                ]
            ),
            unit_lines=["单位：元"],
        )
        robrock_region = make_result(
            "688169",
            "石头科技",
            54,
            "\n".join(
                [
                    "主营业务分地区情况",
                    "单位：元",
                    "分地区",
                    "境外 600,000,000 300,000,000 50",
                    "境内 400,000,000 200,000,000 50",
                ]
            ),
            unit_lines=["单位：元"],
        )
        robrock_rd = make_result(
            "688169",
            "石头科技",
            45,
            "\n".join(
                [
                    "研发投入情况表",
                    "单位：元",
                    "研发投入合计 100,000,000",
                    "研发投入总额占营业收入比例（%） 10",
                ]
            ),
            unit_lines=["单位：元"],
        )
        answer = answer_from_results(
            "对比石头科技与科沃斯2025年的境外收入占比与研发投入强度（研发投入÷营业收入）。",
            [ecovacs_region, ecovacs_rd, robrock_region, robrock_rd],
            "Q09",
        )
        self.assertEqual(answer["回答状态"], STATUS_BLOCKED)
        self.assertFalse(answer["确定性作答"])
        rd_metrics = [
            m
            for m in answer["指标"]
            if m["指标原文"] in {"研发投入合计", "研发投入总额占营业收入比例（%）"}
        ]
        self.assertEqual(len(rd_metrics), 4)
        self.assertTrue(all(m["是否采信"] for m in rd_metrics))
        blocked_metrics = [m for m in answer["指标"] if m["blocked"]]
        self.assertTrue(blocked_metrics)
        self.assertTrue(all(not m["是否采信"] for m in blocked_metrics))


class TestOverseasRankingRefusal(unittest.TestCase):
    def test_all_companies_question_does_not_rank(self) -> None:
        meide = make_result(
            "000333",
            "美的集团",
            50,
            "\n".join(
                [
                    "（1） 营业收入构成",
                    "单位：元",
                    "分地区",
                    "海外 600,000,000 60%",
                    "国内 400,000,000 40%",
                ]
            ),
            unit_lines=["单位：元"],
        )
        gree = make_result(
            "000651",
            "格力电器",
            24,
            "\n".join(
                [
                    "（1）营业收入构成",
                    "单位：元",
                    "分地区",
                    "外销 300,000,000 30%",
                    "内销 700,000,000 70%",
                ]
            ),
            unit_lines=["单位：元"],
        )
        answer = answer_from_results(
            "在全部14家公司中2025年境外收入占营业总收入比重最高的三家分别是哪几家？请给出比例与判断依据。",
            [meide, gree],
            "Q10",
        )
        self.assertEqual(answer["回答状态"], STATUS_INSUFFICIENT)
        self.assertFalse(answer["确定性作答"])
        self.assertEqual(answer["审计"]["排名"], "未进行")
        self.assertIn("不是排名", answer["答案正文"])
        self.assertIn("未计算前三名", answer["答案正文"])
        self.assertEqual(len(answer["审计"]["原始披露"]), 2)
        self.assertTrue(answer["审计"]["未提取公司"])


class TestSummary(unittest.TestCase):
    def test_summary_reports_zero_blocked_misuse_and_no_accuracy(self) -> None:
        blocked = make_result(
            "603486",
            "科沃斯",
            20,
            "研发投入情况表\n单位：元\n研发投入合计 100,000,000\n研发投入总额占营业收入比例（%） 5\n营业收入 2,000,000,000",
            blocked=True,
            reliable=False,
            unit_lines=["单位：元"],
        )
        answer = answer_from_results(
            "科沃斯2025年研发投入金额是多少？占当期营业收入的比例是多少？",
            [blocked],
            "Q05",
        )
        summary = summarize_answers([answer])
        self.assertEqual(summary["阻断证据误用数"], 0)
        self.assertEqual(summary["跨公司混淆数"], 0)
        self.assertEqual(summary["答案准确率"], "未评测")
        self.assertEqual(summary["人工核验状态"], "未完成")


if __name__ == "__main__":
    unittest.main()
