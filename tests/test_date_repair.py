"""日期折行**自动恢复**的回归测试。

覆盖本轮要求的六类（对照任务书「第二步」）：

1. 已确认的日期截断案例 —— 美的 p102 / p105 / p107 / p109 共 5 处；
2. 两个提取模式结果不一致的案例 —— p105 两模式给出不同日期；
3. 只有列坐标证据、缺少其他模式佐证 —— **不得**自动恢复；
4. 不能确定原值 —— **不得**自动补数字（含拼出来不是有效日期的情况）；
5. 普通日期不得被误改；
6. 负数、百分比、金额、单位、印刷页码不得误判为日期异常。

**全部参考值都来自 2026-10-16 对原 PDF 的实测**（原始 PDF 文本对象的
``Tm`` 坐标与表格线矩形、``-table``/``-simple`` 两种模式的实际输出），
没有一个是模型生成的日期。依赖 PDF 与 pdftotext 的测试在两者缺失时
**整体跳过**（不是通过）。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src import date_repair as dr
from src import parse as ps
from src import parse_policy as pp
from src import pdfobjects

_ROOT = Path(__file__).resolve().parents[1]
PDF = _ROOT / "data" / "raw" / "reports" / "000333_美的集团_2025_年度报告.pdf"
PDF2 = _ROOT / "data" / "raw" / "reports" / "688169_石头科技_2025_年度报告.pdf"

_REQUIRE = unittest.skipUnless(
    PDF.exists() and pp.pdftotext_path(), "需要美的 PDF 与 pdftotext"
)
_REQUIRE2 = unittest.skipUnless(
    PDF2.exists() and pp.pdftotext_path(), "需要石头科技 PDF 与 pdftotext"
)

_PAGE_CACHE: dict[tuple[int, str], str] = {}


def page(pg: int, mode: str = pp.PRODUCTION_MODE) -> str:
    """取某页的提取文本（带缓存，避免重复调用外部进程）。"""
    key = (pg, mode)
    if key not in _PAGE_CACHE:
        _PAGE_CACHE[key] = pp.extract_page(PDF, pg, mode)
    return _PAGE_CACHE[key]


_BOOK: pdfobjects.PdfText | None = None


def book() -> pdfobjects.PdfText | None:
    global _BOOK
    if _BOOK is None:
        _BOOK = pdfobjects.PdfText.open(PDF)
    return _BOOK


def only_page(pg: int, text: str) -> list[str]:
    """把单页文本放进一个「只到第 pg 页为止」的列表。

    ``plan_repairs`` 只按索引取页（``pages[i]`` ↔ PDF 第 ``i+1`` 页），
    其余位置留空字符串即可——不必为了测一页把 276 页全提一遍。
    """
    pages = [""] * pg
    pages[pg - 1] = text
    return pages


# ═══════════════ 一、已确认的日期截断案例 ═══════════════


@_REQUIRE
class TestConfirmedTruncations(unittest.TestCase):
    """5 处截断的检出结果必须逐字段稳定——这是本轮全部结论的地基。"""

    def test_p102(self):
        """p102 第 101 行：``2025/11/2`` ＋ 第 103 行的 ``1`` → ``2025/11/21``。"""
        hits = pp.find_wrapped_dates_detailed(page(102))
        self.assertEqual(len(hits), 1)
        w = hits[0]
        self.assertEqual((w.line + 1, w.short, w.tail, w.merged), (101, "2025/11/2", "1", "2025/11/21"))
        self.assertEqual(w.tail_line + 1, 103)

    def test_p105_has_two(self):
        """p105 第 9 / 23 行各一处，尾巴分别是 ``1`` 与 ``6``。"""
        hits = pp.find_wrapped_dates_detailed(page(105))
        self.assertEqual(
            [(w.line + 1, w.short, w.tail, w.merged) for w in hits],
            [(9, "2025/11/2", "1", "2025/11/21"), (23, "2025/12/2", "6", "2025/12/26")],
        )

    def test_p107(self):
        """p107 第 9 行：``2025/12/1`` ＋ ``7`` → ``2025/12/17``。"""
        hits = pp.find_wrapped_dates_detailed(page(107))
        self.assertEqual(
            [(w.line + 1, w.short, w.tail, w.merged) for w in hits],
            [(9, "2025/12/1", "7", "2025/12/17")],
        )

    def test_p109(self):
        """p109 第 45 行：``2025/10/2`` ＋ ``2`` → ``2025/10/22``。"""
        hits = pp.find_wrapped_dates_detailed(page(109))
        self.assertEqual(
            [(w.line + 1, w.short, w.tail, w.merged) for w in hits],
            [(45, "2025/10/2", "2", "2025/10/22")],
        )

    def test_detailed_and_plain_agree(self):
        """带坐标的新检出器与原有 4 元组接口逐条一致（原接口未改语义）。"""
        for pg in (102, 105, 107, 109):
            text = page(pg)
            self.assertEqual(
                pp.find_wrapped_dates(text),
                [(w.line, w.short, w.tail, w.merged) for w in pp.find_wrapped_dates_detailed(text)],
                f"p{pg}",
            )


# ═══════════════ 二、单元格取证（一等判据） ═══════════════


@_REQUIRE
class TestCellEvidence(unittest.TestCase):
    """回到原始 PDF：残缺日期与游离数字是否同处一格、格内是否再无他物。"""

    def test_p105_cell_holds_exactly_the_two_pieces(self):
        """p105 第 23 行的日期格（x 215.33–262.73）内只有 ``2025/12/2`` 与 ``6``。"""
        b = book()
        self.assertIsNotNone(b, "原始 PDF 文本对象读不出来，取证无从谈起")
        runs = [r for r in b.page_runs(105) if r.text.strip()]
        dates = [r for r in runs if r.text.strip() == "2025/12/2"]
        self.assertEqual(len(dates), 1, "该页应恰好有一处残缺日期 2025/12/2")
        cell = b.cell_at(105, dates[0].x, dates[0].y)
        self.assertIsNotNone(cell)
        # 实测该格 x 215.33–262.73（宽 47.40pt），文字区约 35.9pt ≈ 9 个字符，
        # 所以 10 字符的日期必然折行——这是 5 处截断共同的成因。
        self.assertAlmostEqual(cell.x0, 215.33, places=1)
        self.assertAlmostEqual(cell.x1, 262.73, places=1)
        inside = sorted((r for r in runs if cell.contains_run(r)), key=lambda r: (-r.y, r.x))
        self.assertEqual([r.text for r in inside], ["2025/12/2", "6"])
        self.assertLess(inside[1].y, inside[0].y, "游离数字必须落在下一行（y 更小）")

    def test_evidence_is_recorded_as_decisive(self):
        b = book()
        ev = dr._cell_evidence(b, 105, "2025/12/2", "6")
        self.assertTrue(ev.holds)
        self.assertTrue(ev.decisive, "单元格包含关系是一等判据")
        self.assertIn("同处一个单元格", ev.detail)

    def test_column_evidence_is_advisory_not_decisive(self):
        """列坐标只说明「疑似折行」——两个独立的值也可能恰好落进同一区间。"""
        w = pp.find_wrapped_dates_detailed(page(105))[1]
        ev = dr._column_evidence(w)
        self.assertTrue(ev.holds)
        self.assertFalse(ev.decisive, "列坐标不得单独支持恢复")

    def test_cross_mode_evidence_is_advisory_not_decisive(self):
        ev = dr._cross_mode_evidence("…2025/12/26…", "2025/12/26")
        self.assertTrue(ev.holds)
        self.assertFalse(ev.decisive, "-simple 会粘连相邻文本串，不能当判据")

    def test_open_rejects_a_file_that_is_not_a_pdf(self):
        """打不开就返回 None，调用方据此走「证据不足」，而不是抛错中断解析。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "not_a_pdf.pdf"
            p.write_bytes(b"this is not a pdf at all\n" * 10)
            self.assertIsNone(pdfobjects.PdfText.open(p))

    def test_runs_and_cells_return_empty_out_of_range(self):
        b = book()
        self.assertEqual(b.page_runs(0), [])
        self.assertEqual(b.page_runs(99999), [])
        self.assertIsNone(b.cell_at(99999, 100.0, 100.0))


# ═══════════════ 三、判定规则（纯函数，逐条对应任务书要求） ═══════════════


class TestDecide(unittest.TestCase):
    """三条规则的先后顺序：先过日历 → 再要一等证据 → 否则一律待人工核验。"""

    def test_restores_only_with_decisive_evidence(self):
        status, _ = dr.decide("2025/12/2", "6", "2025/12/26", decisive_ok=True)
        self.assertEqual(status, dr.RESTORED)

    def test_third_class_column_only_evidence_does_not_restore(self):
        """第 3 类：只有列坐标证据、缺少其他模式佐证 → 不得自动恢复。"""
        status, note = dr.decide("2025/12/2", "6", "2025/12/26", decisive_ok=False)
        self.assertEqual(status, dr.PENDING)
        self.assertIn("无充分证据", note)

    def test_fourth_class_refuses_to_invent_the_last_digit(self):
        """第 4 类：拼合结果不是真实日期 → 证据成立也**不得**填。

        ``2025/12/26`` 后面若还跟着一个 ``6``，拼出来是 ``2025/12/266``；
        这说明这一处的折行模型不成立（两个独立的值恰好列对齐而已）。
        """
        status, note = dr.decide("2025/12/26", "6", "2025/12/266", decisive_ok=True)
        self.assertEqual(status, dr.PENDING)
        self.assertIn("不是日历上的有效日期", note)

    def test_calendar_guard(self):
        # 注意 ``2025/12/2`` 是**合法**日期（12 月 2 日）——正因为合法，
        # 残缺值才不会被任何「数值合理性」检查拦住，也就必须靠证据来分辨。
        for s in ("2025/12/26", "2025/11/21", "2025/1/1", "2024/2/29", "2025/12/2"):
            self.assertTrue(dr.is_complete_date(s), s)
        for s in ("2025/2/30", "2025/13/1", "2025/12/266", "25/12/26", "2025-12-26", "2025/12/"):
            self.assertFalse(dr.is_complete_date(s), s)


# ═══════════════ 四、两个提取模式不一致 ═══════════════


@_REQUIRE
class TestCrossModeDisagreement(unittest.TestCase):
    """``-table`` 与 ``-simple`` 对同一个单元格给出不同日期；谁说了算由证据决定。"""

    def test_p105_the_two_modes_disagree_on_the_date(self):
        """实测：``-table`` 给 ``2025/12/2``，``-simple`` 给 ``2025/12/26``。"""
        table, simple = page(105), page(105, pp.CROSSCHECK_MODE)
        self.assertIn("2025/12/2", table)
        self.assertNotIn("2025/12/26", table)
        self.assertIn("2025/12/26", simple)

    def test_p102_simple_mode_does_not_give_the_full_date(self):
        """p102 的 ``-simple`` 把尾巴单独留着——所以「simple 支持」不能当必要条件。"""
        simple = page(102, pp.CROSSCHECK_MODE)
        self.assertNotIn("2025/11/21", simple)
        self.assertIn(r"2025/11/2", simple)

    def test_repair_does_not_depend_on_the_second_mode(self):
        """去掉 ``other_mode_pages`` 后 p102 仍然恢复——判据只在原始 PDF 里。"""
        text = page(102)
        with_cross, _ = dr.plan_repairs(PDF, only_page(102, text), other_mode_pages=[text])
        without, _ = dr.plan_repairs(PDF, only_page(102, text))
        self.assertEqual(
            [(r.status, r.merged) for r in with_cross],
            [(r.status, r.merged) for r in without],
        )
        self.assertEqual(with_cross[0].status, dr.RESTORED)

    def test_unfilled_pages_are_not_scanned_for_cross_mode(self):
        """没传交叉模式时，佐证如实记成「未做」，不假装成立。"""
        plans, _ = dr.plan_repairs(PDF, only_page(105, page(105)))
        ev = [e for r in plans for e in r.basis if e.source == dr.SRC_CROSS_MODE]
        self.assertTrue(ev)
        for e in ev:
            self.assertFalse(e.holds)
            self.assertIn("未做交叉模式提取", e.detail)


# ═══════════════ 五、证据不足 → 不猜、不改、仍然阻断 ═══════════════


class TestNoEvidenceNoEdit(unittest.TestCase):
    """把「疑似折行」的形态喂给恢复器，但拿不到 PDF 证据时：一个字都不许改。"""

    #: 与 p105 实测形态同构（日期占 9 字符位，尾巴在下一行同一字符区间内）。
    #: 它不是从 PDF 里抄的，而是**按形态构造**的——用来验证「没有 PDF 证据时
    #: 无论形态多像都不动」，因此刻意不依赖任何真实文件。
    SYNTHETIC = (
        "区某某制造  2025/3/29  6,500    2025/12/2  262  连带责  一年  否  否\n"
        "机制造有限                      6              任保证\n"
    )

    def test_detected_but_not_repaired_without_pdf_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "not_a_pdf.pdf"
            fake.write_bytes(b"not a pdf\n")
            plans, pdf_pages = dr.plan_repairs(fake, [self.SYNTHETIC])
            self.assertIsNone(pdf_pages, "内容流没读出来，页数不得编")
            self.assertEqual(len(plans), 1)
            r = plans[0]
            self.assertEqual((r.short, r.tail, r.merged), ("2025/12/2", "6", "2025/12/26"))
            self.assertEqual(r.status, dr.PENDING, "没有一等证据就不得自动恢复")
            evidence = {e.source: e for e in r.basis}
            self.assertFalse(evidence[dr.SRC_PDF_OBJECTS].holds)
            self.assertIn("无法读取", evidence[dr.SRC_PDF_OBJECTS].detail)
            # 列坐标那条依然成立——正因如此才需要它**不是**判据。
            self.assertTrue(evidence[dr.SRC_COLUMN].holds)
            self.assertFalse(evidence[dr.SRC_COLUMN].decisive)

            text, applied = dr.apply_repairs(self.SYNTHETIC, plans)
            self.assertEqual(applied, [])
            self.assertEqual(text, self.SYNTHETIC, "未恢复的条目必须原样保留")

    def test_tail_that_cannot_form_a_date_is_detected_but_not_filled(self):
        """第 4 类（真实检出器）：完整日期后面跟着一个数字，拼出来不是日期。

        检出器**照旧报**（这是异常，必须让人看到），但恢复器**拒绝补**——
        「检测到异常」不等于「可以自动修」。
        """
        text = (
            "项目                      2025/12/26  262  连带责\n"
            "某某公司                      6          任保证\n"
        )
        hits = pp.find_wrapped_dates_detailed(text)
        self.assertEqual(len(hits), 1, "该形态必须仍被检出，否则异常被静默吞掉")
        self.assertEqual(hits[0].merged, "2025/12/266")
        status, note = dr.decide(
            hits[0].short, hits[0].tail, hits[0].merged, decisive_ok=True
        )
        self.assertEqual(status, dr.PENDING)
        self.assertIn("不是日历上的有效日期", note)

    def test_apply_refuses_when_coordinates_do_not_match(self):
        """写回前逐字符核对；对不上就退回待复核，不做「差不多就改」。"""
        text = self.SYNTHETIC
        r = dr.Repair(
            page=1, line=0, col=999, short="2025/12/2", tail="6",
            tail_line=1, tail_col=999, merged="2025/12/26",
            status=dr.RESTORED,
        )
        out, applied = dr.apply_repairs(text, [r])
        self.assertEqual(applied, [])
        self.assertEqual(out, text)
        self.assertEqual(r.status, dr.PENDING)
        self.assertIn("写回前核对失败", r.note)


# ═══════════════ 六、普通日期 / 负数 / 金额 / 页码不得误判 ═══════════════


class TestNoFalsePositives(unittest.TestCase):
    """第 5、6 类：不该动的一个字都不动。

    这里用的都是**形状正确但语义不是折行**的构造文本 + 实测真实文本，
    负例比正例更容易出问题——误改一处普通日期，比漏修一处更坏。
    """

    NORMAL_TABLE = (
        "美的集团财务有限公司      2025/8/30  1,600,000  2025/1/16    1,135,584  任保证\n"
        "佛山市顺德区美的电热        2025/3/29    120,000  2025/1/23      101,928  任保证\n"
        "广东美的制冷设备有限公司    2025/12/26     70,000  2025/1/20       67,022  任保证\n"
    )

    def test_ordinary_dates_are_not_detected(self):
        self.assertEqual(pp.find_wrapped_dates_detailed(self.NORMAL_TABLE), [])

    def test_ordinary_dates_are_never_rewritten(self):
        plans, _ = dr.plan_repairs(Path("no_such_file.pdf"), [self.NORMAL_TABLE])
        self.assertEqual(plans, [])
        text, applied = dr.apply_repairs(self.NORMAL_TABLE, plans)
        self.assertEqual(applied, [])
        self.assertEqual(text, self.NORMAL_TABLE)

    def test_negatives_percents_amounts_units_are_not_dates(self):
        """负数、百分比、金额、单位、纯数字列——都不含 ``YYYY/M/D`` 形态。"""
        text = (
            "单位：千元\n"
            "项目              2025 年        2024 年     变动\n"
            "营业收入       (335,989,528)   1,234,567,890   -0.22%\n"
            "毛利率               30.15%         28.93%      1.22%\n"
            "合计              -1,234.56       0.00        100%\n"
            "股东总数                  66         1234          -\n"
        )
        self.assertEqual(pp.find_wrapped_dates_detailed(text), [])
        plans, _ = dr.plan_repairs(Path("no_such_file.pdf"), [text])
        self.assertEqual(plans, [])
        _, applied = dr.apply_repairs(text, plans)
        self.assertEqual(applied, [])

    @_REQUIRE
    def test_printed_page_number_and_bare_digit_are_not_detected(self):
        """实测：p67 的印刷页码 ``66``、p118 标题里的 ``10`` 都落在日期列内。

        守卫（整行只有那几个数字＝页码）把它们挡在检出之外；本测试为这两处
        真实负例留下回归位——它们一旦被检出，就会被自动化流程当成日期异常。
        """
        for pg in (67, 118):
            hits = pp.find_wrapped_dates_detailed(page(pg))
            self.assertEqual(hits, [], f"p{pg} 不应有日期折行检出")
            plans, _ = dr.plan_repairs(PDF, only_page(pg, page(pg)))
            self.assertEqual(plans, [], f"p{pg} 不应有任何修复计划")


# ═══════════════ 七、真实 PDF 的端到端恢复 ═══════════════


@_REQUIRE
class TestRestoreOnTheRealReport(unittest.TestCase):
    """5 处全部恢复，且**改动只落在该落的地方**。"""

    @classmethod
    def setUpClass(cls):
        cls.plans: dict[int, list[dr.Repair]] = {}
        for pg in (102, 105, 107, 109):
            text = page(pg)
            plans, pdf_pages = dr.plan_repairs(PDF, only_page(pg, text))
            cls.plans[pg] = plans
            cls.pdf_pages = pdf_pages

    def test_all_five_are_restored(self):
        statuses = [(pg, r.short, r.merged, r.status) for pg in self.plans for r in self.plans[pg]]
        self.assertEqual(
            statuses,
            [
                (102, "2025/11/2", "2025/11/21", dr.RESTORED),
                (105, "2025/11/2", "2025/11/21", dr.RESTORED),
                (105, "2025/12/2", "2025/12/26", dr.RESTORED),
                (107, "2025/12/1", "2025/12/17", dr.RESTORED),
                (109, "2025/10/2", "2025/10/22", dr.RESTORED),
            ],
        )
        self.assertEqual(self.pdf_pages, 276, "取证读到的页数应与 PDF 一致")

    def test_edits_are_confined_to_the_two_character_spans(self):
        """逐字符比对：除「残缺日期」与「游离数字」两处外，**一个字符都没变**。"""
        for pg, plans in self.plans.items():
            original = page(pg)
            out, applied = dr.apply_repairs(original, plans)
            self.assertEqual(len(applied), len(plans))
            self.assertTrue(all(r.verified for r in applied))

            before, after = original.splitlines(), out.splitlines()
            self.assertEqual(len(before), len(after), "行数不得变化")
            for i, (b, a) in enumerate(zip(before, after)):
                if b == a:
                    continue
                # 只有日期行与游离数字行允许不同，且差异位置必须与记录一致。
                touched = [
                    r for r in applied if r.line == i or r.tail_line == i
                ]
                self.assertTrue(touched, f"p{pg} 第 {i + 1} 行不该被改动")
                for r in touched:
                    if r.line == i:
                        self.assertEqual(a[r.col : r.col + len(r.merged)], r.merged)
                        self.assertEqual(
                            a[: r.col] + a[r.col + len(r.merged) :],
                            b[: r.col] + b[r.col + len(r.short) :],
                        )
                    else:
                        self.assertEqual(a[r.tail_col : r.tail_col + len(r.tail)], " " * len(r.tail))
                        self.assertEqual(
                            a[: r.tail_col] + a[r.tail_col + len(r.tail) :],
                            b[: r.tail_col] + b[r.tail_col + len(r.tail) :],
                        )

    def test_stray_digit_is_gone_from_the_continuation_line(self):
        """半吊子修复（只改日期不删游离数字）会让数字重复出现，必须避免。

        实测第 25 行的形态是「公司名残段 ＋ 游离数字 ＋ 附注列」三者共处一行，
        所以只能按**字符位**判断，不能按整行内容判断。
        """
        r = self.plans[105][1]
        original = page(105)
        out, _ = dr.apply_repairs(original, self.plans[105])
        before, after = original.splitlines(), out.splitlines()
        self.assertEqual(before[r.tail_line][r.tail_col], "6")
        self.assertEqual(after[r.tail_line][r.tail_col], " ")
        self.assertEqual(len(before[r.tail_line]), len(after[r.tail_line]), "等长替换以保住列对齐")
        for i, plan in ((r.tail_line, r), (self.plans[105][0].tail_line, self.plans[105][0])):
            self.assertEqual(
                before[i][: plan.tail_col] + before[i][plan.tail_col + len(plan.tail) :],
                after[i][: plan.tail_col] + after[i][plan.tail_col + len(plan.tail) :],
                f"第 {i + 1} 行的其余字符不得变化",
            )
        for i in (8, 22):  # 日期行只长了 1 个字符（末位回接）
            self.assertEqual(len(after[i]), len(before[i]) + 1)

    def test_unrepaired_pages_are_untouched(self):
        """没有检出的页：修复计划为空，文本逐字节不变。

        用构造文本而不是再去提 4 个真实页——本轮实测过 ``-f/-l`` 单页提取
        偶发长时间无响应（p135 一次等了近 300 秒），测试不该押在它的稳定性上；
        真实页的覆盖由 :class:`TestParsePipelineWiring` 全篇跑一次来保证。
        """
        text = TestNoFalsePositives.NORMAL_TABLE
        plans, _ = dr.plan_repairs(PDF, only_page(50, text))
        self.assertEqual(plans, [], "普通日期不该产生修复计划")
        out, applied = dr.apply_repairs(text, plans)
        self.assertEqual((out, applied), (text, []))

    def test_apply_preserves_the_trailing_blank_line(self):
        """``splitlines`` 会吃掉结尾换行；未涉及的字符必须无损。"""
        text = page(105)
        out, applied = dr.apply_repairs(text, self.plans[105])
        self.assertTrue(applied)
        self.assertEqual(len(text.splitlines()), len(out.splitlines()), "行数不得变化")
        self.assertEqual(text[len(text.rstrip("\n")) :], out[len(out.rstrip("\n")) :])

    def test_audit_payload_is_self_contained(self):
        """审计记录要能独立复核：原始值、修复值、页码、依据、验证状态俱全。"""
        all_plans = [r for pg in sorted(self.plans) for r in self.plans[pg]]
        out, applied = dr.apply_repairs(page(102), self.plans[102])
        payload = dr.audit_payload(
            code="000333", name="美的集团", year=2025, pdf_name=PDF.name,
            repairs=all_plans, pdf_pages=self.pdf_pages,
        )
        self.assertEqual((payload["检出"], payload["已恢复"], payload["待人工核验"]), (5, 5, 0))
        for item in payload["修复明细"]:
            self.assertTrue(item["原始值"] and item["修复值"] and item["依据"])
            self.assertIn("原始 PDF 文本对象", [e["来源"] for e in item["依据"]])
            self.assertTrue(any(e["判据"] and e["成立"] for e in item["依据"]))
        self.assertTrue(applied and out != page(102))


# ═══════════════ 八、接入 parse.py 后的端到端 ═══════════════


@_REQUIRE
class TestParsePipelineWiring(unittest.TestCase):
    """解析流水线：恢复发生在切块**之前**，块与台账都留下痕迹。"""

    @classmethod
    def setUpClass(cls):
        cls.result, cls.chunks = ps.parse_pdf(PDF, code="000333", name="美的集团")

    def test_document_wide_detection_is_exactly_five(self):
        """全文检出恰好 5 处，且都在这 4 页上——多一处就是误判，少一处就是漏判。"""
        self.assertEqual(
            sorted((r.page, r.short, r.merged) for r in self.result.日期恢复),
            [
                (102, "2025/11/2", "2025/11/21"),
                (105, "2025/11/2", "2025/11/21"),
                (105, "2025/12/2", "2025/12/26"),
                (107, "2025/12/1", "2025/12/17"),
                (109, "2025/10/2", "2025/10/22"),
            ],
        )
        self.assertTrue(all(r.status == dr.RESTORED for r in self.result.日期恢复))
        self.assertTrue(all(r.verified for r in self.result.日期恢复))

    def test_no_wrapped_date_finding_survives_and_all_repairs_are_logged(self):
        """恢复完就不该再有「日期折行截断」；取而代之的是 5 条已恢复记录。"""
        kinds = [f["kind"] for f in self.result.异常]
        self.assertNotIn(pp.KIND_WRAPPED_DATE, kinds)
        self.assertEqual(kinds.count(dr.KIND_DATE_RESTORED), 5)
        # 阻断语义：恢复后的值可信，不再阻断；异常清单里仍有 5 条来自本轮的记录。
        restored = [f for f in self.result.异常 if f["kind"] == dr.KIND_DATE_RESTORED]
        self.assertTrue(all(f["blocks"] is False for f in restored))
        self.assertTrue(all(f["status"] == dr.RESTORED for f in restored))

    def test_chunks_carry_the_repaired_text_and_the_record(self):
        """切块用的是修复后的文本，块里同时留下「改了什么」。"""
        p102 = [c for c in self.chunks if c["页码"] == 102]
        self.assertTrue(p102)
        self.assertTrue(any("2025/11/21" in c["正文"] for c in p102))
        self.assertFalse(any(r"2025/11/2 " in c["正文"] for c in p102))
        self.assertTrue(all(c["日期恢复"] == ["2025/11/2→2025/11/21"] for c in p102))

        p105 = [c for c in self.chunks if c["页码"] == 105]
        self.assertTrue(all(c["日期恢复"] == ["2025/11/2→2025/11/21", "2025/12/2→2025/12/26"] for c in p105))
        text = "".join(c["正文"] for c in p105)
        self.assertIn("2025/12/26", text)
        self.assertIn("2025/11/21", text)

    def test_side_effects_are_recorded_not_hidden(self):
        """修复会连带改变别的异常——必须显式记下，不能让它悄悄少掉。

        实测：p105 的「模式间不一致」原有 4 条，修复后剩 2 条。其中一条是**真解决**
        （两模式都给出 ``2025/12/26``），另一条是因为那一行原本唯一的数值就是游离
        数字 ``6``，被清掉后该行不含任何数值、模式间比对自然跳过——
        **这不是「分歧解决了」，是「产生分歧的残料没了」**。两者都进审计。
        """
        side = self.result.日期恢复副作用
        self.assertEqual(sorted(side), [102, 105, 107, 109])
        gone = [f for pg in side for f in side[pg]["消失的异常"]]
        kinds = {f["类型"] for f in gone}
        self.assertIn(pp.KIND_WRAPPED_DATE, kinds)
        self.assertIn(pp.KIND_MODE_DISAGREEMENT, kinds)
        self.assertTrue(all(f["说明"] for f in gone), "每条副作用都要带可读说明")
        self.assertIn("消失不等于", side[105]["注"])

        # 同一标签的模式间比对会因修复而**改写**（片段里的数字变了），
        # 那是同一条异常换了措辞，不是新增一条——否则变化数会被记成两倍。
        # 实测 p105：table 侧的日期由 2025/11/2 变成 2025/11/21，与 simple 侧一致了。
        self.assertIn(
            {
                "类型": pp.KIND_MODE_DISAGREEMENT,
                "修复前": "table: (2025, 11, 2) / simple: (2025, 3, 29, 3000, 2025, 11, 21)",
                "修复后": "table: (2025, 11, 21) / simple: (2025, 3, 29, 3000, 2025, 11, 21)",
            },
            side[105]["改写的异常"],
        )
        # p102：游离数字 ``1`` 被清掉后，那一行的整数序列里少了一个伪数值 1——
        # 这是修复的正当效果，也如实记在「改写」里。
        self.assertIn(
            {
                "类型": pp.KIND_MODE_DISAGREEMENT,
                "修复前": "table: (2025, 3, 29, 350000, 1, 31500) / simple: (2025, 3, 29, 350000, 2025, 11, 2, 1, 31500)",
                "修复后": "table: (2025, 3, 29, 350000, 31500) / simple: (2025, 3, 29, 350000, 2025, 11, 2, 1, 31500)",
            },
            side[102]["改写的异常"],
        )
        self.assertEqual(
            sum(len(side[pg]["新增的异常"]) for pg in side), 0,
            "本轮修复不应制造任何新异常",
        )

    def test_other_pages_have_no_date_record(self):
        pages_with = {r.页码 for r in self.result.页记录 if r.日期恢复}
        self.assertEqual(pages_with, {102, 105, 107, 109})

    def test_chunks_without_repairs_carry_an_empty_record(self):
        others = [c for c in self.chunks if c["页码"] not in (102, 105, 107, 109)]
        self.assertTrue(others)
        self.assertTrue(all(c["日期恢复"] == [] for c in others))


@_REQUIRE2
class TestNoDetectionMeansNoExtraWork(unittest.TestCase):
    """石头科技全文 0 处检出：既不该改文本，也不该去读 PDF 内容流。"""

    def test_zero_detections_and_no_pdf_parse(self):
        pages = pp.extract_document(PDF2, pp.PRODUCTION_MODE)
        self.assertEqual(len(pages), 282)
        plans, pdf_pages = dr.plan_repairs(PDF2, pages)
        self.assertEqual(plans, [])
        self.assertIsNone(pdf_pages, "没有检出就不去读内容流（那一趟不便宜）")
        hits = sum(len(pp.find_wrapped_dates(t)) for t in pages)
        self.assertEqual(hits, 0)


if __name__ == "__main__":
    unittest.main()
