"""检索原型纯逻辑测试（不加载 113MB 索引，不联网、不写文件）。

覆盖：分词与查询扩展、公司识别、结构意图识别、RRF 融合、跨公司
意图覆盖选择、表格元数据。真实索引的构建与检索结果由
``deliverables/retrieval_eval_2025.py`` 的端到端日志记录。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from src.index import hash_token, normalize_text, query_token_counts, table_metadata, tokenize
from src.retrieve import (
    analyze_table_header,
    attach_cross_page_context,
    detect_companies,
    detect_intents,
    document_intent_score,
    find_cross_page_header,
    financial_form,
    has_table_header,
    metric_warnings,
    reciprocal_rank_fusion,
    select_balanced_candidates,
)


class TestTokenizer(unittest.TestCase):
    def test_normalize_fullwidth_and_case(self):
        self.assertEqual(normalize_text("ＡＢＣ１２３"), "abc123")

    def test_chinese_ngrams_and_phrase(self):
        tokens = tokenize("美的2025年营业收入")
        self.assertIn("c:美的", tokens)
        self.assertIn("w:2025", tokens)
        self.assertIn("L:营业收入", tokens)

    def test_number_with_commas_is_one_token(self):
        self.assertIn("w:456,451,731", tokenize("456,451,731"))

    def test_query_expansion_is_explicit(self):
        counts = query_token_counts("归母净利润")
        self.assertIn("L:归属于母公司股东的净利润", counts)
        self.assertIn("L:归属于上市公司股东的净利润", counts)
        # 扩展只增加召回机会，不改变原查询 token
        self.assertIn("L:归母净利润", counts)

    def test_air_conditioner_expansion(self):
        self.assertIn("L:空调", query_token_counts("空调业务"))

    def test_hash_is_stable_and_in_range(self):
        first = hash_token("c:营业收入")
        second = hash_token("c:营业收入")
        self.assertEqual(first, second)
        bucket, sign = first
        self.assertGreaterEqual(bucket, 0)
        self.assertLess(bucket, 8192)
        self.assertIn(sign, (-1.0, 1.0))


class TestCompanyDetection(unittest.TestCase):
    def test_code_and_alias(self):
        self.assertEqual(detect_companies("美的集团2025年营业总收入"), ["000333"])
        self.assertEqual(detect_companies("格力2025年空调业务"), ["000651"])

    def test_multiple_companies(self):
        found = detect_companies("对比美的集团、格力电器、海尔智家2025年营业总收入")
        self.assertEqual(found, ["000333", "000651", "600690"])

    def test_all_fourteen(self):
        found = detect_companies("在全部14家公司中境外收入占比最高的三家")
        self.assertEqual(len(found), 14)
        self.assertEqual(len(set(found)), 14)

    def test_no_company_returns_empty(self):
        self.assertEqual(detect_companies("2025年家电行业整体情况如何"), [])


class TestIntentDetection(unittest.TestCase):
    def test_financial_not_triggered_by_revenue_alone(self):
        # “营业总收入”本身太宽泛，不应把 Q10 误判为财务指标题
        self.assertNotIn("financial", detect_intents("2025年营业总收入是多少"))

    def test_financial_triggered_by_attributable_profit(self):
        intents = detect_intents("归母净利润同比变动如何")
        self.assertIn("financial", intents)

    def test_region_and_rd(self):
        intents = detect_intents("对比境外收入占比与研发投入强度")
        self.assertIn("region", intents)
        self.assertIn("rd", intents)

    def test_dividend_and_channel(self):
        self.assertIn("dividend", detect_intents("利润分配预案每10股派息"))
        self.assertIn("channel", detect_intents("线上销售渠道收入占比"))

    def test_intent_scores_are_explainable(self):
        self.assertGreaterEqual(
            document_intent_score(
                "主营业务分地区情况 境内 境外 营业收入 毛利率", ["region"]
            ),
            5,
        )
        self.assertGreaterEqual(
            document_intent_score(
                "主营业务分产品情况 分产品 营业收入 毛利率", ["product"]
            ),
            4,
        )
        self.assertGreaterEqual(
            document_intent_score(
                "研发投入情况表 研发投入合计 研发投入总额占营业收入比例", ["rd"]
            ),
            3,
        )
        self.assertGreaterEqual(
            document_intent_score("利润分配预案 每10股 现金", ["dividend"]),
            5,
        )
        self.assertGreaterEqual(
            document_intent_score("分销售模式 线上销售 营业收入", ["channel"]),
            4,
        )
        self.assertGreaterEqual(
            document_intent_score(
                "主要会计数据 归属于上市公司股东的净利润 本年比上年 营业收入",
                ["financial"],
            ),
            5,
        )
        self.assertGreaterEqual(
            document_intent_score("合并利润表 一、营业总收入", ["financial"]),
            2,
        )
        self.assertGreaterEqual(
            document_intent_score("毛利率 营业成本", ["gross_margin"]), 2
        )


class TestRankFusion(unittest.TestCase):
    def test_rrf_order_and_metadata(self):
        fused = reciprocal_rank_fusion(
            [(1, 10.0), (2, 9.0), (3, 8.0)],
            [(3, 10.0), (1, 9.0), (4, 8.0)],
            [(2, 5.0), (4, 4.0)],
            limit=10,
        )
        self.assertEqual([item["doc_index"] for item in fused], [2, 4, 1, 3])
        self.assertEqual(fused[0]["intent_score"], 5.0)
        self.assertEqual(fused[0]["bm25_rank"], 2)
        self.assertIsNone(fused[0]["vector_rank"])

    def test_intent_weight_changes_order(self):
        without_intent = reciprocal_rank_fusion(
            [(1, 10.0)], [(2, 10.0)], None, limit=10
        )
        with_intent = reciprocal_rank_fusion(
            [(1, 10.0)], [(2, 10.0)], [(2, 5.0)], limit=10, intent_weight=5.0
        )
        self.assertEqual(without_intent[0]["doc_index"], 1)
        self.assertEqual(with_intent[0]["doc_index"], 2)


class TestBalancedSelection(unittest.TestCase):
    def setUp(self):
        self.documents = [
            {"正文": "主营业务分地区 境内 境外 营业收入 毛利率"},  # 0
            {"正文": "研发投入情况表 研发投入合计 占营业收入比例"},  # 1
            {"正文": "其他内容"},  # 2
            {"正文": "分地区 海外 营业收入 占比"},  # 3
            {"正文": "研发费用 营业收入"},  # 4
            {"正文": "其他内容"},  # 5
        ]
        self.grouped = {
            "A": [
                {"doc_index": 2, "rrf": 0.9},
                {"doc_index": 0, "rrf": 0.8},
                {"doc_index": 1, "rrf": 0.7},
            ],
            "B": [
                {"doc_index": 3, "rrf": 0.6},
                {"doc_index": 4, "rrf": 0.5},
                {"doc_index": 5, "rrf": 0.4},
            ],
        }

    def test_covers_each_intent_per_company(self):
        selected = select_balanced_candidates(
            self.grouped, ["A", "B"], 2, self.documents, ["region", "rd"]
        )
        indexes = [item["doc_index"] for item in selected]
        self.assertEqual(indexes, [0, 1, 3, 4])
        self.assertEqual(len(indexes), len(set(indexes)))

    def test_fills_remaining_slots_with_rrf_order(self):
        selected = select_balanced_candidates(
            self.grouped, ["A", "B"], 3, self.documents, ["region", "rd"]
        )
        indexes = [item["doc_index"] for item in selected]
        self.assertEqual(indexes, [0, 1, 2, 3, 4, 5])

    def test_without_intents_uses_rrf_order(self):
        selected = select_balanced_candidates(
            self.grouped, ["A", "B"], 2, self.documents, []
        )
        self.assertEqual([item["doc_index"] for item in selected], [2, 0, 3, 4])


class TestFinancialFormCoverage(unittest.TestCase):
    def test_financial_form_classification(self):
        self.assertEqual(
            financial_form("主要会计数据 归属于上市公司股东的净利润"),
            "主要会计数据",
        )
        self.assertEqual(
            financial_form("合并利润表 一、营业总收入"),
            "利润表",
        )
        self.assertIsNone(financial_form("普通正文，没有财务表"))

    def test_each_company_gets_both_forms(self):
        documents = [
            {"正文": "主要会计数据 归属于上市公司股东的净利润"},  # A 主表
            {"正文": "合并利润表 一、营业总收入 归属于母公司"},  # A 利润表
            {"正文": "主要会计数据 归属于上市公司股东的净利润"},  # B 主表
            {"正文": "合并利润表 一、营业总收入 归属于母公司"},  # B 利润表
        ]
        grouped = {
            "A": [
                {"doc_index": 0, "rrf": 0.9, "bm25_score": 1.0, "vector_score": 1.0},
                {"doc_index": 1, "rrf": 0.8, "bm25_score": 1.0, "vector_score": 1.0},
            ],
            "B": [
                {"doc_index": 2, "rrf": 0.7, "bm25_score": 1.0, "vector_score": 1.0},
                {"doc_index": 3, "rrf": 0.6, "bm25_score": 1.0, "vector_score": 1.0},
            ],
        }
        selected = select_balanced_candidates(
            grouped, ["A", "B"], 2, documents, ["financial"]
        )
        self.assertEqual([item["doc_index"] for item in selected], [0, 1, 2, 3])


def _doc(
    *,
    code: str,
    page: int,
    text: str,
    kind: str = "table",
    name: str = "测试公司",
    pdf: str = "test.pdf",
    block: int = 1,
    year: int = 2025,
) -> dict:
    return {
        "doc_id": f"{code}_{year}_p{page:04d}_c{block:04d}",
        "证券代码": code,
        "公司简称": name,
        "报告年份": year,
        "PDF文件名": pdf,
        "页码": page,
        "块序号": block,
        "类型": kind,
        "章节": "测试章节",
        "字符数": len(text),
        "正文": text,
        "warnings": [],
        "blocking_anomalies": [],
        "blocked": False,
        "reliable_for_deterministic_answer": True,
        "source": {
            "citation": f"{name}（{code}）{year} 年年度报告 p{page}",
            "pdf": pdf,
            "page": page,
            "chunk_type": kind,
            "section": "测试章节",
        },
    }


# 页面正文行，用于把表头推到页尾。这些行不含列头标记，也不会被当成
# 数据行；真实年报中表头前的章节标题/说明文字起同样作用。
HEADER_PAGE_FILLER = "\n".join(
    [
        "一、主营业务分析",
        "二、经营情况讨论",
        "三、行业格局",
        "四、公司未来展望",
        "五、风险因素",
        "六、其他事项",
    ]
)
HEADER_TEXT = (
    HEADER_PAGE_FILLER
    + "\n项目 2025 年 2024 年 同比增减"
    + "\n金额 占营业收入比重 金额 占营业收入比重"
)
MID_PAGE_HEADER_TEXT = (
    "项目 2025 年 2024 年 同比增减\n"
    "金额 占营业收入比重 金额 占营业收入比重\n"
    + HEADER_PAGE_FILLER
)
DATA_TEXT = (
    "营业收入合计 1,000 100% 900 100% 11.11%\n"
    "分产品 600 60% 540 60% 11.11%"
)
NEXT_PAGE_LOCAL_HEADER_TEXT = (
    "项目 2025 年 2024 年 同比增减\n"
    "金额 占营业收入比重 金额 占营业收入比重\n"
    + DATA_TEXT
)
NEXT_PAGE_SPLIT_HEADER_TEXT = (
    "54、 营业收入和营业成本\n"
    "本期发生额 上期发生额\n"
    "项目\n"
    "收入 成本 收入 成本\n"
    + DATA_TEXT
)


class TestCrossPageContext(unittest.TestCase):
    def test_header_page_is_attached_with_own_page_number(self):
        documents = [
            _doc(code="A", page=1, text=HEADER_TEXT),
            _doc(code="A", page=2, text=DATA_TEXT),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 2,
                "类型": "table",
                "正文": DATA_TEXT,
                "warnings": [],
            }
        ]
        result = attach_cross_page_context(response, documents)[0]
        self.assertEqual(result["页码"], 2)
        self.assertEqual(result["跨页表头"]["表头页"], 1)
        self.assertEqual(result["跨页表头"]["表头来源"], "上下文")
        self.assertEqual(result["跨页表头"]["关联可靠性"], "算法判定，待人工核验")
        self.assertEqual(result["上下文片段"][0]["页码"], 1)
        self.assertEqual(result["上下文片段"][0]["context_role"], "表头来源")
        self.assertEqual(result["上下文片段"][0]["关联数据页"], 2)
        # 跨页关联只是算法候选，不能因为源页本身可靠就变成确定性证据。
        self.assertTrue(
            result["上下文片段"][0]["源页reliable_for_deterministic_answer"]
        )
        self.assertFalse(
            result["上下文片段"][0]["reliable_for_deterministic_answer"]
        )
        self.assertEqual(
            result["上下文片段"][0]["上下文关联状态"],
            "算法判定，待人工核验",
        )
        self.assertFalse(result["上下文片段"][0]["上下文关联可靠"])

    def test_mid_page_header_is_not_attached(self):
        documents = [
            _doc(code="A", page=1, text=MID_PAGE_HEADER_TEXT),
            _doc(code="A", page=2, text=DATA_TEXT),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 2,
                "类型": "table",
                "正文": DATA_TEXT,
                "warnings": [],
            }
        ]
        result = attach_cross_page_context(response, documents)[0]
        self.assertEqual(result["跨页表头"]["表头来源"], "未找到")
        self.assertEqual(result["跨页表头"]["关联可靠性"], "未确认，不附加")
        self.assertIn("页尾", result["跨页表头"]["警告"])
        self.assertNotIn("上下文片段", result)

    def test_single_row_page_is_not_enough_for_continuation(self):
        # 只有一个“含两个数字”的说明性行，不能证明是表格续表。
        thin_page = "其他说明 1,000 2,000\n补充说明 3,000\n普通正文，没有表格。"
        documents = [
            _doc(code="A", page=1, text=HEADER_TEXT),
            _doc(code="A", page=2, text=thin_page),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 2,
                "类型": "table",
                "正文": thin_page,
                "warnings": [],
            }
        ]
        result = attach_cross_page_context(response, documents)[0]
        self.assertEqual(result["跨页表头"]["表头来源"], "未找到")
        self.assertEqual(result["跨页表头"]["关联可靠性"], "未确认，不附加")
        self.assertIn("两条", result["跨页表头"]["警告"])
        self.assertNotIn("上下文片段", result)

    def test_next_page_with_strong_local_header_is_not_attached(self):
        documents = [
            _doc(code="A", page=1, text=HEADER_TEXT),
            _doc(code="A", page=2, text=NEXT_PAGE_LOCAL_HEADER_TEXT),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 2,
                "类型": "table",
                "正文": NEXT_PAGE_LOCAL_HEADER_TEXT,
                "warnings": [],
            }
        ]
        result = attach_cross_page_context(response, documents)[0]
        self.assertEqual(result["跨页表头"]["表头来源"], "页内已有表头")
        self.assertNotIn("上下文片段", result)

    def test_next_page_with_split_local_header_is_not_attached(self):
        documents = [
            _doc(code="A", page=1, text=HEADER_TEXT),
            _doc(code="A", page=2, text=NEXT_PAGE_SPLIT_HEADER_TEXT),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 2,
                "类型": "table",
                "正文": NEXT_PAGE_SPLIT_HEADER_TEXT,
                "warnings": [],
            }
        ]
        result = attach_cross_page_context(response, documents)[0]
        self.assertEqual(result["跨页表头"]["表头来源"], "页内已有表头")
        self.assertNotIn("上下文片段", result)

    def test_does_not_cross_year_boundary(self):
        documents = [
            _doc(code="A", page=1, text=HEADER_TEXT, year=2024),
            _doc(code="A", page=2, text=DATA_TEXT),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 2,
                "类型": "table",
                "正文": DATA_TEXT,
                "warnings": [],
            }
        ]
        result = attach_cross_page_context(response, documents)[0]
        self.assertEqual(result["跨页表头"]["表头来源"], "未找到")
        self.assertNotIn("上下文片段", result)

    def test_does_not_cross_pdf_boundary(self):
        documents = [
            _doc(code="A", page=1, text=HEADER_TEXT, pdf="other.pdf"),
            _doc(code="A", page=2, text=DATA_TEXT),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 2,
                "类型": "table",
                "正文": DATA_TEXT,
                "warnings": [],
            }
        ]
        result = attach_cross_page_context(response, documents)[0]
        self.assertEqual(result["跨页表头"]["表头来源"], "未找到")
        self.assertNotIn("上下文片段", result)

    def test_does_not_cross_company_boundary(self):
        documents = [
            _doc(code="B", page=1, text=HEADER_TEXT, name="乙公司"),
            _doc(code="A", page=2, text=DATA_TEXT),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 2,
                "类型": "table",
                "正文": DATA_TEXT,
                "warnings": [],
            }
        ]
        result = attach_cross_page_context(response, documents)[0]
        self.assertEqual(result["跨页表头"]["表头来源"], "未找到")
        self.assertNotIn("上下文片段", result)

    def test_header_in_primary_results_is_not_duplicated(self):
        documents = [
            _doc(code="A", page=1, text=HEADER_TEXT),
            _doc(code="A", page=2, text=DATA_TEXT),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 1,
                "类型": "table",
                "正文": HEADER_TEXT,
                "warnings": [],
            },
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 2,
                "类型": "table",
                "正文": DATA_TEXT,
                "warnings": [],
            },
        ]
        result = attach_cross_page_context(response, documents)[1]
        self.assertEqual(result["跨页表头"]["表头来源"], "主结果")
        self.assertNotIn("上下文片段", result)

    def test_missing_previous_header_is_reported_not_invented(self):
        documents = [
            _doc(code="A", page=1, text="普通正文，没有列头。"),
            _doc(code="A", page=2, text=DATA_TEXT),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 2,
                "类型": "table",
                "正文": DATA_TEXT,
                "warnings": [],
            }
        ]
        result = attach_cross_page_context(response, documents)[0]
        self.assertEqual(result["跨页表头"]["表头来源"], "未找到")
        self.assertNotIn("上下文片段", result)

    def test_context_budget_is_bounded(self):
        documents = [
            _doc(code="A", page=1, text=HEADER_TEXT),
            _doc(code="A", page=2, text=DATA_TEXT),
            _doc(code="A", page=3, text=HEADER_TEXT),
            _doc(code="A", page=4, text=DATA_TEXT),
        ]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": page,
                "类型": "table",
                "正文": DATA_TEXT,
                "warnings": [],
            }
            for page in (2, 4)
        ]
        attach_cross_page_context(response, documents, max_contexts=1)
        self.assertEqual(response[0]["跨页表头"]["表头来源"], "上下文")
        self.assertEqual(response[1]["跨页表头"]["表头来源"], "未扩展（达到上限）")

    def test_prose_page_is_not_expanded(self):
        documents = [_doc(code="A", page=1, text=HEADER_TEXT)]
        response = [
            {
                "证券代码": "A",
                "报告年份": 2025,
                "PDF文件名": "test.pdf",
                "页码": 1,
                "类型": "prose",
                "正文": "普通正文",
                "warnings": [],
            }
        ]
        result = attach_cross_page_context(response, documents)[0]
        self.assertNotIn("跨页表头", result)

    def test_header_and_data_detection(self):
        self.assertTrue(analyze_table_header(HEADER_TEXT + "\n" + DATA_TEXT)["has_local_header"])
        self.assertFalse(analyze_table_header(DATA_TEXT)["has_local_header"])
        self.assertTrue(analyze_table_header(DATA_TEXT)["has_data"])

    def test_cross_page_header_must_be_near_page_tail(self):
        self.assertIsNotNone(find_cross_page_header(HEADER_TEXT))
        self.assertTrue(has_table_header(HEADER_TEXT))
        self.assertIsNone(find_cross_page_header(MID_PAGE_HEADER_TEXT))
        self.assertFalse(has_table_header(MID_PAGE_HEADER_TEXT))

    def test_split_local_header_is_detected(self):
        result = analyze_table_header(NEXT_PAGE_SPLIT_HEADER_TEXT)
        self.assertTrue(result["has_local_header"])
        self.assertTrue(result["has_data"])

    def test_financial_header_without_year_spacing_is_detected(self):
        # 美的 p135 形态：项目/附注 + 2025年度（“年”和“度”之间没有空格）
        text = (
            "项目 附注 2025年度 2024年度 2025年度 2024年度\n"
            "一、营业总收入 458,502,407 409,084,266 936,519 946,607"
        )
        self.assertTrue(analyze_table_header(text)["has_local_header"])

    def test_year_header_with_growth_column_is_detected(self):
        # 美的 p9 形态：年份行同时带“本年比上年增减”
        text = (
            "2025 年 2024 年 本年比上年增减 2023 年\n"
            "营业收入（千元） 456,451,731 407,149,600 12.11% 372,037,280"
        )
        self.assertTrue(analyze_table_header(text)["has_local_header"])

    def test_pure_year_line_is_not_mistaken_for_header(self):
        # 只有两个年份、没有项目/科目等列头词，仍应判为缺表头
        text = "2025 年 2024 年\n营业收入 1,000 900"
        result = analyze_table_header(text)
        self.assertFalse(result["has_local_header"])
        self.assertTrue(result["has_data"])

    def test_address_line_is_not_treated_as_table_data(self):
        # 美的 p9 形态：财务表之前有带门牌号的地址行
        text = (
            "境内会计师事务所办公地址 上海市浦东新区 555 号 45 层 4504 单元\n"
            "2025 年 2024 年 本年比上年增减 2023 年\n"
            "营业收入（千元） 456,451,731 407,149,600 12.11% 372,037,280"
        )
        self.assertTrue(analyze_table_header(text)["has_local_header"])

    def test_period_line_is_not_treated_as_table_data(self):
        # 格力 p88 形态：利润表标题后先出现“2025 年 1-12 月”
        text = (
            "合并利润表\n"
            "2025 年 1-12 月\n"
            "编制单位：珠海格力电器股份有限公司 单位：人民币元\n"
            "项目 附注 2025 年度 2024 年度\n"
            "一、营业总收入 171,118,161,275.41 190,038,071,604.78"
        )
        self.assertTrue(analyze_table_header(text)["has_local_header"])


class TestMetricWarnings(unittest.TestCase):
    def test_air_conditioner_does_not_map_to_consumer_appliance(self):
        warnings = metric_warnings("格力2025年空调业务毛利率", "消费电器 毛利率 35.28%")
        self.assertTrue(any("空调" in item and "消费电器" in item for item in warnings))
        self.assertTrue(any("不必然等价" in item for item in warnings))

    def test_total_revenue_is_not_silently_equated(self):
        warnings = metric_warnings("美的2025年营业总收入", "营业收入 456,451,731")
        self.assertTrue(any("营业总收入" in item and "营业收入" in item for item in warnings))

    def test_exact_metric_is_not_warned(self):
        self.assertEqual(metric_warnings("研发投入", "研发投入合计 980,519,969.24"), [])


class TestTableMetadata(unittest.TestCase):
    def test_table_page_is_kept_whole(self):
        meta = table_metadata(
            {
                "类型": "table",
                "正文": "项目 营业收入\n单位：元\n空调 1,000 10%\n",
                "需复核": ["表头跨行"],
            }
        )
        self.assertTrue(meta["整页保留"])
        self.assertEqual(meta["行数"], 3)
        self.assertEqual(meta["含数字行数"], 1)
        self.assertEqual(meta["单位行"], ["单位：元"])
        self.assertTrue(meta["表头跨行警告"])
        self.assertIn("未按行切分", meta["说明"])

    def test_prose_page_is_not_a_table(self):
        meta = table_metadata({"类型": "prose", "正文": "营业收入 100 元"})
        self.assertEqual(meta, {"整页保留": False})


if __name__ == "__main__":
    unittest.main()
