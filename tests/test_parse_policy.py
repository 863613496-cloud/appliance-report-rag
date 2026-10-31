"""解析策略的回归测试。

**所有参考值都来自 `logs/midea_verification.md` 中已核验的原文证据，
以及 2026-10-14 对照实验的实测输出——没有一个是模型生成的答案。**
每个测试的 docstring 标明了出处页码。

依赖 PDF 与 pdftotext 的测试在两者缺失时**整体跳过**（不是通过）。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

from src import parse_policy as pp

_ROOT = Path(__file__).resolve().parents[1]
PDF = _ROOT / "data" / "raw" / "reports" / "000333_美的集团_2025_年度报告.pdf"

_CACHE: dict[tuple[int, str], str] = {}


def page(pg: int, mode: str = pp.PRODUCTION_MODE) -> str:
    """取某页的提取文本（带缓存，避免重复调用外部进程）。"""
    key = (pg, mode)
    if key not in _CACHE:
        _CACHE[key] = pp.extract_page(PDF, pg, mode)
    return _CACHE[key]


def _layout(pg: int) -> str:
    key = (pg, pp.MODE_LAYOUT)
    if key not in _CACHE:
        _CACHE[key] = pp.extract_page(PDF, pg, pp.MODE_LAYOUT, allow_forbidden=True)
    return _CACHE[key]


_REQUIRES_PDF = unittest.skipUnless(
    PDF.exists() and pp.pdftotext_path() is not None,
    f"缺少 {PDF.name} 或本机无 pdftotext，已跳过依赖真实 PDF 的测试",
)


# ═══════════════════════ 一、模式裁决（纯逻辑） ═══════════════════════


class TestModePolicy(unittest.TestCase):
    """模式禁止清单与页面类型分派。依据见模块 docstring 的实测结论。"""

    def test_layout_is_forbidden(self):
        """`-layout` 实测在 p49 / p53 / p135 三处错位，必须被禁用。"""
        self.assertIn(pp.MODE_LAYOUT, pp.FORBIDDEN_MODES)

    def test_layout_forbidden_takes_effect_before_touching_disk(self):
        """禁止模式必须在访问文件**之前**就被拦下。"""
        with self.assertRaises(ValueError) as ctx:
            pp.extract_page("不存在的文件.pdf", 1, pp.MODE_LAYOUT)
        self.assertIn("禁用", str(ctx.exception))

    def test_default_reading_order_is_forbidden(self):
        """默认阅读顺序实测在表格页把行列打散，同样必须被禁用。

        实证据：p49 上「机器人与自动化 工业技术 其他业务 其他 分地区」
        被挤进同一行，三个标签的数值全部取不到。
        """
        self.assertIn(pp.MODE_DEFAULT, pp.FORBIDDEN_MODES)
        with self.assertRaises(ValueError) as ctx:
            pp.extract_page("不存在的文件.pdf", 1, pp.MODE_DEFAULT)
        self.assertIn("禁用", str(ctx.exception))

    def test_all_four_bad_modes_are_forbidden(self):
        """四种实测有故障的模式一个都不能漏。"""
        for mode in (pp.MODE_LAYOUT, pp.MODE_DEFAULT, pp.MODE_RAW, pp.MODE_LINEPRINTER):
            with self.subTest(mode=mode):
                self.assertIn(mode, pp.FORBIDDEN_MODES)

    def test_forbidden_gate_requires_explicit_opt_in(self):
        """`allow_forbidden` 默认必须是 False——闸门默认是关着的。

        该逃生门只供测试刻画失败形态使用（全仓库仅 test_parse_policy 一处调用）。
        """
        import inspect

        default = inspect.signature(pp.extract_page).parameters["allow_forbidden"].default
        self.assertIs(default, False)

    def test_production_mode_is_usable(self):
        self.assertEqual(pp.PRODUCTION_MODE, pp.MODE_TABLE)
        self.assertNotIn(pp.PRODUCTION_MODE, pp.FORBIDDEN_MODES)

    def test_crosscheck_mode_is_usable(self):
        self.assertNotIn(pp.CROSSCHECK_MODE, pp.FORBIDDEN_MODES)

    def test_unsupported_flags_are_documented_not_used(self):
        """本机 pdftotext 是 Xpdf 4.06，`-bbox` 系参数不支持，不得出现在模式表里。"""
        used = {f for flags in pp.MODE_FLAGS.values() for f in flags}
        for flag in pp.UNSUPPORTED_FLAGS:
            self.assertNotIn(flag, used)

    def test_useless_flags_are_documented_and_not_used(self):
        """`-fixed <n>` 实测是空操作（与默认模式逐字节相同），不得进入模式表。"""
        used = {f for flags in pp.MODE_FLAGS.values() for f in flags}
        for flag in pp.USELESS_FLAGS:
            self.assertNotIn(flag, used)
            self.assertNotIn(flag, pp.UNSUPPORTED_FLAGS, "空操作与不支持是两回事，不要混在一起")

    def test_no_forbidden_mode_in_any_dispatch_path(self):
        """任何页面类型的提取序列里都不得出现被禁用的模式。"""
        for kind in (pp.PAGE_PROSE, pp.PAGE_TABLE):
            for mode in pp.modes_for(kind):
                self.assertNotIn(mode, pp.FORBIDDEN_MODES, f"{kind} 分派到了禁用模式 {mode}")

    def test_table_pages_get_a_crosscheck_mode(self):
        """表格页必须跑两个模式——没有任何单一模式能在全部表格页上正确。"""
        modes = pp.modes_for(pp.PAGE_TABLE)
        self.assertEqual(len(modes), 2)
        self.assertEqual(modes[0], pp.PRODUCTION_MODE)
        self.assertNotEqual(modes[0], modes[1])

    def test_prose_pages_get_one_mode(self):
        self.assertEqual(pp.modes_for(pp.PAGE_PROSE), (pp.PRODUCTION_MODE,))

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            pp.extract_page("x.pdf", 1, "不存在的模式")


# ═══════════════════════ 二、数值抽取（纯逻辑） ═══════════════════════


class TestTokenization(unittest.TestCase):
    """数值抽取的正确性——这几条都由实测故障反推而来。"""

    def test_note_reference_is_not_a_negative_number(self):
        """附注引用「四(48), 十八(3)」中的括号数字不得被当成负数。

        实测故障：修复前 p135 的「其中：营业收入」抽成
        `(-3, 456451731, ...)`、「研发费用」抽成 `(-53, -17787624, ...)`。
        """
        line = "其中：营业收入          四(48), 十八(3)   456,451,731   407,149,600"
        self.assertEqual(pp.numbers_in(line), (456451731, 407149600))

    def test_parenthesised_amount_is_negative(self):
        """真正的括号负数仍须保留符号。"""
        self.assertEqual(pp.numbers_in("减：营业成本  (335,989,528)"), (-335989528,))

    def test_header_year_is_skipped(self):
        """表头年份不是数据。实测 p53 的年份与「年」之间夹了 4 个空格。"""
        self.assertEqual(pp.numbers_in("项目   2025    年            2024  年"), ())

    def test_plain_thousands_separated(self):
        self.assertEqual(pp.numbers_in("营业收入合计  456,451,731  407,149,600"),
                         (456451731, 407149600))

    def test_percents_kept_verbatim(self):
        """百分比原样保留——`-0.22%` 的负号与小数位是有意义的。"""
        self.assertEqual(
            pp.percents_in("海外  195,947,693  42.93%  169,034,383  41.52%  15.92%"),
            ("42.93%", "41.52%", "15.92%"),
        )
        self.assertEqual(pp.percents_in("毛利率比上年同期增减  -0.22%"), ("-0.22%",))


class TestChangeBasis(unittest.TestCase):
    """百分比变化 vs 百分点变化——两者在文本里长得一样，只能靠表头区分。"""

    def test_gross_margin_change_is_a_point_difference(self):
        """「毛利率比上年同期增减」是百分点差。"""
        self.assertEqual(pp.classify_change("毛利率比上年同期增减"), pp.CHANGE_POINT)

    def test_revenue_change_is_a_percentage(self):
        """「营业收入比上年同期增减」是相对变化。"""
        self.assertEqual(pp.classify_change("营业收入比上年同期增减"), pp.CHANGE_PERCENT)

    def test_ratio_change_is_a_point_difference(self):
        self.assertEqual(pp.classify_change("占营业收入比重"), pp.CHANGE_POINT)

    def test_undecidable_header_returns_none(self):
        """判不出来就必须返回 None——调用方禁止猜测口径。"""
        self.assertIsNone(pp.classify_change("往来款项"))


# ═══════════════════ 三、已核验指标（需要真实 PDF） ═══════════════════


@_REQUIRES_PDF
class TestInstalledByPage(unittest.TestCase):
    """已核验指标必须能按「页码 + 标签」稳定取回。

    参考值出处：logs/midea_verification.md §1–§5。
    """

    def test_pdf_is_the_verified_file(self):
        """页数与体量须与核验记录一致，防止测试跑在别的文件上。"""
        self.assertEqual(PDF.stat().st_size, 5_942_399)

    def test_page_49_revenue_total(self):
        """p49「营业收入合计」= 456,451,731 / 407,149,600 千元。"""
        row = pp.find_row(page(49), "营业收入合计")
        self.assertIsNotNone(row)
        self.assertEqual(row.integers, (456451731, 407149600))

    def test_page_50_overseas(self):
        """p50「分地区→海外」= 195,947,693 千元，占比 42.93%，同比 15.92%。"""
        row = pp.find_row(page(50), "海外", before="分销售模式")
        self.assertIsNotNone(row)
        self.assertEqual(row.integers, (195947693, 169034383))
        self.assertEqual(row.percents, ("42.93%", "41.52%", "15.92%"))

    def test_page_224_audited_note(self):
        """p224 附注 四(48)(a)「机器人与自动化」= 31,010,933 千元。"""
        row = pp.find_row(page(224), "机器人与自动化")
        self.assertIsNotNone(row)
        self.assertEqual(row.integers[0], 31010933)
        self.assertEqual(row.integers[2], 28700565)

    def test_page_135_income_statement(self):
        """p135 经审计合并利润表：营业总收入、营业收入、净利润、归母净利润。"""
        t = page(135)
        self.assertEqual(pp.find_row(t, "营业总收入").integers[:2], (458502407, 409084266))
        self.assertEqual(pp.find_row(t, "其中：营业收入").integers[:2], (456451731, 407149600))
        self.assertEqual(pp.find_row(t, "四、净利润").integers[:2], (44520196, 38757214))
        self.assertEqual(
            pp.find_row(t, "归属于母公司股东的").integers[:2], (43945411, 38537237)
        )

    def test_p135_parent_net_income_label_is_folded(self):
        """p135 的「归属于母公司股东的」与「净利润」被拆到两行，中间隔一个空行。

        这是 `-table` 在合并利润表上唯一的失分项，也是 `find_row` 必须支持
        折行标签（而非只认同行）的原因。相邻的正常行不应被误判为折行。
        """
        t = page(135)
        folded = pp.find_row(t, "归属于母公司股东的")
        self.assertFalse(folded.single_line, "该标签在 p135 上确实是折行的")
        self.assertEqual(len(folded.lines), 2)
        self.assertEqual(folded.lines[1].strip().split()[0], "净利润")

        self.assertTrue(pp.find_row(t, "四、净利润").single_line)


@_REQUIRES_PDF
class TestNoConceptConfusion(unittest.TestCase):
    """用户指定的六项「不得混淆」检查。"""

    def test_revenue_total_is_not_operating_revenue(self):
        """营业收入 ≠ 营业总收入。

        差额 = 利息收入 2,050,168 + 手续费及佣金收入 508 = 2,050,676。
        混用会虚增约 20.5 亿元。
        """
        t = page(135)
        total = pp.find_row(t, "营业总收入").integers[0]
        revenue = pp.find_row(t, "其中：营业收入").integers[0]
        self.assertNotEqual(total, revenue)
        self.assertEqual(total - revenue, 2_050_676)
        self.assertEqual(total, 458_502_407)
        self.assertEqual(revenue, 456_451_731)

    def test_net_profit_is_not_attributable_net_profit(self):
        """净利润 ≠ 归母净利润。

        差额 = 少数股东损益 574,785。把 574,785 当归母净利润是 ``-layout``
        模式的实测故障（偏低 98.69%）。
        """
        t = page(135)
        net = pp.find_row(t, "四、净利润").integers[0]
        attr = pp.find_row(t, "归属于母公司股东的").integers[0]
        self.assertNotEqual(net, attr)
        self.assertEqual(net - attr, 574_785)
        self.assertEqual(net, 44_520_196)
        self.assertEqual(attr, 43_945_411)

    def test_overseas_share_and_growth_are_mutually_consistent(self):
        """海外收入、占比、同比三者必须自洽。"""
        t = page(50)
        overseas = pp.find_row(t, "海外", before="分销售模式")
        domestic = pp.find_row(t, "国内", before="分销售模式")
        revenue = pp.find_row(page(49), "营业收入合计").integers[0]

        self.assertEqual(overseas.integers, (195_947_693, 169_034_383))
        # 国内外合计 = 营业收入
        self.assertEqual(domestic.integers[0] + overseas.integers[0], revenue)
        # 占比
        self.assertAlmostEqual(
            overseas.integers[0] / revenue * 100, 42.93, places=2
        )
        # 同比
        self.assertAlmostEqual(
            (overseas.integers[0] / overseas.integers[1] - 1) * 100, 15.92, places=2
        )

    def test_rd_expense_is_not_rd_investment_total(self):
        """研发费用 ≠ 研发投入总额。

        报告实际披露的科目是「研发费用金额」与「研发费用占营业收入比例」，
        **未单独披露「研发投入总额」**，模板问句勾选「不适用」。
        不得用研发费用冒充研发投入总额。
        """
        t = page(53)
        self.assertEqual(pp.find_row(t, "研发费用金额（千元）").integers, (17_787_624, 16_232_771))
        self.assertEqual(pp.find_row(t, "研发费用占营业收入比例").percents, ("3.90%", "3.99%", "-0.09%"))
        # 报告中不存在带数值的「研发投入总额」行
        self.assertIsNone(
            pp.find_row(t, "研发投入总额占营业收入的比重"),
            "若取到数值，说明该行被误配了别的数据",
        )
        # 口径自洽：研发费用 ÷ 营业收入 = 3.90%
        revenue = pp.find_row(page(49), "营业收入合计").integers[0]
        self.assertAlmostEqual(17_787_624 / revenue * 100, 3.90, places=2)

    def test_percentage_change_is_not_point_change(self):
        """同一行里的两个「%」口径不同，不得混为一谈。

        p50「10%以上」表「海外」行：
            营业收入同比 15.92%  ← 百分比变化
            毛利率同比  -0.22%   ← 百分点变化
        判据只在表头里，数值本身看不出来。
        """
        t = page(50)
        row = pp.find_row(t, "海外", after="分地区", before="分销售模式")
        self.assertIsNotNone(row)
        self.assertEqual(row.integers, (195_947_693, 143_831_923))
        self.assertEqual(row.percents, ("26.60%", "15.92%", "16.27%", "-0.22%"))
        # 两个变动值的口径由表头决定，且必须能被区分
        self.assertEqual(pp.classify_change("营业收入比上年同期增减"), pp.CHANGE_PERCENT)
        self.assertEqual(pp.classify_change("毛利率比上年同期增减"), pp.CHANGE_POINT)
        # 毛利率 26.60% 同比降 0.22 个百分点（不是 0.22%）
        self.assertAlmostEqual(26.60 + 0.22, 26.82, places=2)


@_REQUIRES_PDF
class TestRobotLabelMisalignment(unittest.TestCase):
    """已知的「机器人与自动化」行标签错位，必须被钉死。"""

    LABEL = "机器人与自动化"

    def test_table_mode_gives_audited_value_on_p49(self):
        """p49 分产品表：`-table` 给出 31,010,933，与经审计附注一致。"""
        row = pp.find_row(page(49), self.LABEL, after="分产品", before="分地区")
        self.assertEqual(row.integers[0], 31_010_933)
        self.assertAlmostEqual(
            (row.integers[0] / row.integers[1] - 1) * 100, 8.05, places=2
        )

    def test_audited_note_agrees(self):
        """p224 附注 四(48)(a) 独立印证同一个数。"""
        self.assertEqual(pp.find_row(page(224), self.LABEL).integers[0], 31_010_933)

    def test_layout_mode_misaligns_the_label(self):
        """`-layout` 在 p49 把标签整体上移一行，得到 27,232,432（实为工业技术）。

        这条测试同时在**看住** `-layout` 的故障形态：一旦哪天上游修好了，
        这里会失败并提醒重新评估禁止清单。
        """
        row = pp.find_row(_layout(49), self.LABEL, after="分产品", before="分地区")
        self.assertIsNotNone(row, "预期 -layout 仍能取到该标签（只是配错了数值）")
        self.assertEqual(row.integers[0], 27_232_432)
        self.assertNotEqual(row.integers[0], 31_010_933)

    def test_industrial_technology_is_also_shifted(self):
        """错位不是孤例：工业技术与「其他业务」同样串位。"""
        t = _layout(49)
        self.assertEqual(
            pp.find_row(t, "工业技术", after="分产品", before="分地区").integers[0],
            28_718_768,  # 实为「其他业务」的值
        )

    def test_layout_misaligns_parent_net_income_on_p135(self):
        """最严重的一处：`-layout` 把归母净利润错成 574,785（偏低 98.69%）。

        错位后挂上去的 574,785 是少数股东损益——一个**真实存在**的数，
        不会被「数值异常」的启发式拦住。
        """
        row = pp.find_row(_layout(135), "归属于母公司股东的")
        self.assertIsNotNone(row)
        self.assertEqual(row.integers[0], 574_785)
        true_value = pp.find_row(page(135), "归属于母公司股东的").integers[0]
        self.assertEqual(true_value, 43_945_411)
        self.assertAlmostEqual((1 - row.integers[0] / true_value) * 100, 98.69, places=2)

    def test_modes_agree_detects_the_p53_disagreement(self):
        """两模式交叉核对能发现 p53 上「隐私保护」行的分歧。

        注意「博士」测不出来：`-layout` 下该行没有数值，而 `find_row` 的
        折行容错会向下取到「其他」行的 903/802，**恰好掩盖了错位**。
        这说明折行容错本身也会掩盖故障，故它只在标签确实折行时才应生效。
        """
        self.assertFalse(
            pp.modes_agree(page(53), _layout(53), "隐私保护"),
            "p53 上 -table 给 (2845, 967)、-layout 给 (2567, 3935)，应报告为不一致",
        )
        self.assertEqual(pp.find_row(page(53), "隐私保护").integers, (2845, 967))
        self.assertEqual(pp.find_row(_layout(53), "隐私保护").integers, (2567, 3935))

    def test_default_mode_loses_all_three_labels_on_p49(self):
        """默认阅读顺序在 p49 把标签挤成一行，三个标签一个都取不到。

        实测该行内容为「机器人与自动化 工业技术 其他业务 其他 分地区」——
        标签齐全、数值全无，且「分地区」这个区块标题也被卷了进来。
        """
        t = pp.extract_page(PDF, 49, pp.MODE_DEFAULT, allow_forbidden=True)
        for lab in ("机器人与自动化", "楼宇科技", "工业技术"):
            with self.subTest(label=lab):
                self.assertIsNone(
                    pp.find_row(t, lab, after="分产品", before="分地区"),
                    f"{lab} 在默认模式下竟能取到数值，禁用理由需重新评估",
                )
        self.assertFalse(
            pp.modes_agree(t, page(49), "机器人与自动化", after="分产品", before="分地区")
        )


@_REQUIRES_PDF
class TestSameLabelDifferentValues(unittest.TestCase):
    """同名标签在不同区块下指向不同数值——解析时必须带区块限定。"""

    def test_other_business_differs_by_section(self):
        """p49 的「其他业务」出现两次：分行业 47,179,307 / 分产品 28,718,768。

        不带 `after`/`before` 限定就会取错。
        """
        t = page(49)
        by_industry = pp.find_row(t, "其他业务", after="分行业", before="分产品")
        by_product = pp.find_row(t, "其他业务", after="分产品", before="分地区")
        self.assertEqual(by_industry.integers[0], 47_179_307)
        self.assertEqual(by_product.integers[0], 28_718_768)
        self.assertNotEqual(by_industry.integers[0], by_product.integers[0])

    def test_cross_page_table_needs_page_range(self):
        """p49 的「分地区」标题与数值分处两页，单页提取拿不到配对。

        必须用 `extract_pages` 取 49–50 的连续区间，标题与数值才在同一个文本里。
        """
        self.assertIsNone(
            pp.find_row(page(49), "海外"),
            "p49 上不应取到海外数值——它在 p50",
        )
        joined = pp.extract_pages(PDF, 49, 50)
        self.assertIn("分地区", joined, "区间提取应同时含 p49 的区块标题")
        self.assertEqual(
            pp.find_row(joined, "海外", after="分地区").integers,
            (195_947_693, 169_034_383),
        )

    def test_extract_pages_joins_in_order(self):
        """区间提取按页序拼接，页与页之间用分页符分隔。"""
        joined = pp.extract_pages(PDF, 49, 51)
        self.assertEqual(joined.count("\f"), 2, "3 页之间应有 2 个分页符")
        self.assertLess(joined.index("分地区"), joined.index("海外"))

    def test_page_boundary_does_not_glue_lines_together(self):
        """分页符必须独占一行：上一页的末行不能与下一页的首行粘成同一行。

        若用 ``"\\f".join``（分页符夹在行中间）或先 ``splitlines`` 再拼，
        跨页处的两行会并成一行，``find_row`` 就有机会把不相干的标签与数值
        配成一对——这是静默错数，比抛异常危险得多。

        因此逐行断言：区间输出 == 第一页各行 + 一个分页符行 + 第二页各行。
        这里按 ``"\\n"`` 切分而非 ``splitlines()``，因为后者会把 ``\\f``
        也当作换行符，掩盖「分页符是否独占一行」这件事；
        ``find_row`` 用的也正是 ``"\\n"``。
        """
        first = pp.extract_page(PDF, 49).rstrip()
        second = pp.extract_page(PDF, 50).rstrip()
        lines = pp.extract_pages(PDF, 49, 50).split("\n")

        self.assertEqual(lines.count("\f"), 1, "两页之间应恰好一个分页符行")
        boundary = lines.index("\f")
        self.assertEqual(lines[:boundary], first.split("\n"), "分页符前应是 p49 的全部行")
        self.assertEqual(lines[boundary + 1 :], second.split("\n"), "分页符后应是 p50 的全部行")


@_REQUIRES_PDF
class TestQuarantine(unittest.TestCase):
    """无法可靠还原的内容必须被隔离，且阻断其进入确定性答案。"""

    def test_polluted_label_detected_on_both_pages(self):
        """「隐私保护」在 p53 与 p78 都被检出。"""
        for pg in (53, 78):
            with self.subTest(page=pg):
                self.assertIn("隐私保护", pp.find_polluted_labels(page(pg)))

    def test_polluted_label_values_are_still_readable(self):
        """数值可信（经求和验证），**标签**不可信。"""
        self.assertEqual(pp.find_row(page(53), "隐私保护").integers, (2845, 967))
        self.assertEqual(pp.find_row(page(78), "隐私保护").integers, (18_898,))

    def test_polluted_label_blocks_deterministic_answer(self):
        findings = pp.scan_anomalies(53, page(53))
        polluted = [f for f in findings if f.kind == pp.KIND_POLLUTED_LABEL]
        self.assertTrue(polluted)
        self.assertTrue(all(f.blocks_deterministic_answer() for f in polluted))
        self.assertTrue(all(f.status == pp.NEEDS_REVIEW for f in polluted))
        with self.assertRaises(ValueError):
            pp.assert_answer_safe(findings)

    def test_orphan_note_does_not_block(self):
        """孤立附注行只影响附注列，数值仍可用，故不阻断。"""
        findings = [
            f for f in pp.scan_anomalies(135, page(135)) if f.kind == pp.KIND_ORPHAN_NOTE
        ]
        self.assertTrue(findings, "p135 的「四(48),」应被检出")
        self.assertFalse(any(f.blocks_deterministic_answer() for f in findings))

    def test_clean_page_yields_no_blocking_finding(self):
        """p224 经审计附注：无阻断项——它的数值可以直接用。"""
        blocking = [
            f for f in pp.scan_anomalies(224, page(224)) if f.blocks_deterministic_answer()
        ]
        self.assertEqual(blocking, [])

    def test_assert_answer_safe_passes_when_nothing_blocks(self):
        pp.assert_answer_safe([])  # 不应抛错


@_REQUIRES_PDF
class TestWrappedDateDetection(unittest.TestCase):
    """日期单元格折行导致 ``-table`` 少读一位数字。

    2026-10-16 解析 000333 时发现，是全流程里**唯一一处被证实的数据错误**
    （不是排版噪声）。证据是列坐标：残缺日期的起始列，恰好落在
    相邻行那个游离数字所在的列上。详见 ``pp.find_wrapped_dates`` 的 docstring。
    """

    def test_p105_date_loses_its_last_digit(self):
        """p105「区美的饮水」行：真实 2025/12/26，-table 只读出 2025/12/2。"""
        found = pp.find_wrapped_dates(page(105))
        self.assertIn((22, "2025/12/2", "6", "2025/12/26"), found)

    def test_p105_has_two_such_rows(self):
        """p105 有两处：2025/11/21 与 2025/12/26。"""
        merged = {r[3] for r in pp.find_wrapped_dates(page(105))}
        self.assertEqual(merged, {"2025/11/21", "2025/12/26"})

    def test_p109_date_loses_its_last_digit(self):
        """p109「团芜湖制冷」行：-table 读出 2025/10/2，真实 2025/10/22。"""
        found = pp.find_wrapped_dates(page(109))
        self.assertIn((44, "2025/10/2", "2", "2025/10/22"), found)

    def test_page_number_is_not_a_wrap_tail(self):
        """p67 的页码 66 恰好落在日期列内，必须靠「该行还有无别的内容」挡住。

        没有这条守卫，页码会被当成日期尾巴，凭空造出 2027/7/166 这种日期。
        """
        self.assertEqual(pp.find_wrapped_dates(page(67)), [])

    def test_wrapped_date_blocks_deterministic_answer(self):
        """少一天的日期是数值错误，必须阻断，不能只当提示。"""
        findings = [
            f for f in pp.scan_anomalies(105, page(105)) if f.kind == pp.KIND_WRAPPED_DATE
        ]
        self.assertTrue(findings)
        self.assertTrue(all(f.blocks_deterministic_answer() for f in findings))
        self.assertTrue(all(f.status == pp.NEEDS_REVIEW for f in findings))

    def test_finding_wording_marks_candidate_not_confirmed(self):
        """检测文案不得把候选拼合值写成已确认的“真实日期”。"""
        findings = [
            f for f in pp.scan_anomalies(105, page(105)) if f.kind == pp.KIND_WRAPPED_DATE
        ]
        self.assertTrue(findings)
        for f in findings:
            self.assertIn("候选拼合值", f.detail)
            self.assertIn("尚未确认", f.detail)
            self.assertNotIn("真实日期应为", f.detail)
            self.assertTrue(f.extra.get("candidate_only"))

    def test_clean_text_yields_nothing(self):
        """没有日期折行的文本不应报出任何东西。"""
        self.assertEqual(pp.find_wrapped_dates("项目    2025/12/26    1,234\n单位：千元\n"), [])


@_REQUIRES_PDF
class TestKnownUnresolved(unittest.TestCase):
    """把尚未解决的问题钉成测试，避免它们被悄悄"修好"或被当成已解决。"""

    def test_p20_transposed_table_is_not_recoverable(self):
        """p20 转置表的标签与数值在**全部 7 种模式**下都不同行。

        该页数据已由 p49 与 p224 独立印证，故答案可用；
        但 p20 自身的提取产物不得作为该答案的唯一出处。
        """
        for mode in pp.MODE_FLAGS:
            if mode in pp.FORBIDDEN_MODES:
                continue
            with self.subTest(mode=mode):
                self.assertIsNone(
                    pp.find_row(page(20, mode), "机器人与自动化"),
                    f"{mode} 模式下竟能取到 p20 的该标签，结论需重新评估",
                )

    def test_p20_third_corroboration_still_holds(self):
        """p20 仍提供了第三处佐证：31,010,933 ÷ 28,700,565 − 1 = 8.05%。"""
        t = page(20)
        self.assertIn("31,010,933", t)
        self.assertIn("8.05%", t)
        self.assertAlmostEqual((31_010_933 / 28_700_565 - 1) * 100, 8.05, places=2)

    def test_p53_age_breakdown_discrepancy_is_recorded(self):
        """p53 年龄构成 2025 年分项合计 23,903 ≠ 披露总数 23,926，差 23 人。

        这是**报告原文的不一致**，三种提取模式取值一致，不是提取错位。
        不得把 23,903 改成 23,926，也不得反过来改。
        """
        t = page(53)
        below30 = pp.find_row(t, "30 岁及以下").integers[0]
        mid = pp.find_row(t, "30~40 岁").integers[0]
        above40 = pp.find_row(t, "40 岁以上").integers[0]
        self.assertEqual((below30, mid, above40), (6823, 10870, 6210))
        self.assertEqual(below30 + mid + above40, 23_903)
        self.assertEqual(pp.find_row(t, "研发人员数量（人）").integers[0], 23_926)
        self.assertEqual(23_926 - 23_903, 23)

    def test_rd_headcount_education_sums_match(self):
        """学历结构分项之和等于研发人员总数（2025 与 2024 双年都对）。

        正因为这条成立，才能判定「隐私保护」行的**数值**是真的。
        """
        t = page(53)
        edu = [pp.find_row(t, lab).integers for lab in ("本科", "硕士", "博士", "其他")]
        self.assertEqual([e[0] for e in edu], [11820, 5791, 903, 2567])
        self.assertEqual([e[1] for e in edu], [12271, 5718, 802, 3935])
        polluted = pp.find_row(t, "隐私保护").integers
        self.assertEqual(sum(e[0] for e in edu) + polluted[0], 23_926)
        self.assertEqual(sum(e[1] for e in edu) + polluted[1], 23_693)

    def test_employee_education_sums_match(self):
        """员工教育程度分项之和 = 198,570。"""
        t = page(78)
        parts = [pp.find_row(t, lab).integers[0] for lab in ("博士、硕士", "大学本科", "其他")]
        self.assertEqual(sum(parts) + pp.find_row(t, "隐私保护").integers[0], 198_570)


if __name__ == "__main__":
    unittest.main(verbosity=2)
