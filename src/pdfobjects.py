"""直接读 PDF 内容流里的**文本对象与表格线**——即「原始 PDF 文本」这一级证据。

═══ 为什么需要它 ═══

``pdftotext`` 的每一种模式都是**有损的再排版**：它把 PDF 的内容流重新排成
文本行。表格模式下，一个放不下的单元格会被折成两行，折出来的尾巴就成了
「上一行看不出、下一行看不懂」的游离字符——``parse_policy.find_wrapped_dates``
能检出它，但**无法证明**它就是该日期的一部分。

要证明，只能回到 PDF 本身：内容流里每个文本串都带精确坐标
（``Tm`` 的平移量），表格线也都以矢量矩形/线段的形态留着。于是可以问一个
排版层面的问题：**那个游离数字，和残缺日期，是否同处一个单元格？**
单元格是排版的最小单位，一个单元格里的全部文本必然属于同一个值。

═══ 实测依据 ═══

2026-10-16 在 ``000333_美的集团_2025_年度报告.pdf``（Microsoft Word 生成，
PDF 1.7，Identity-H 子集字体 + 148 个对象流）上验证：本模块读出的 276 页
与 pdftotext 的页数一致，首页文本、页脚日期均正确。美的担保表 p102–p109 的
5 处日期折行，用本模块判定「残缺日期与游离数字同处一格、且该格无其他文本」，
全部成立。详见 ``logs/midea_verification.md`` 与 ``date_repair.py``。

═══ 只用标准库 ═══

本机既没装 pypdf 也没装 pdfplumber（见 ``src/parse.py`` 的说明），
而这里要的东西很窄——文本串 + 坐标 + 直线——所以用 ``re`` + ``zlib``
自己取，不引入新依赖。

═══ 能力边界（失败时返回空，不猜） ═══

支持：FlateDecode 内容流、对象流（ObjStm）内的页面与字体对象、
Identity-H（双字节码）与简单 TrueType（单字节码）字体。
不支持：加密文档、非 Flate 的流过滤器（LZW/JPEG 等与本用途无关）、
跨页续表（每页独立解析）。

任何一步取不到就返回 ``[]`` 或 ``None``，绝不猜测——调用方据此判定
「证据不足」，把该处异常标为待人工核验。
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from pathlib import Path

#: 数值 token（PDF 里的实数）
_NUM = r"[+-]?\d*\.?\d+"


@dataclass(frozen=True)
class TextRun:
    """一个文本串及其基线起点坐标（PDF 用户空间，单位 pt，原点在左下角）。"""

    x: float
    y: float
    font: str
    text: str


@dataclass(frozen=True)
class Cell:
    """一个表格单元格的矩形。"""

    x0: float
    y0: float
    x1: float
    y1: float

    def contains_point(self, x: float, y: float, *, pad: float = 0.6) -> bool:
        return self.x0 - pad <= x < self.x1 and self.y0 < y <= self.y1

    def contains_run(self, run: TextRun, *, pad: float = 0.6) -> bool:
        """文本串是否起于本格内。以基线起点判定——PDF 不记录串的结束位置。"""
        return self.contains_point(run.x, run.y, pad=pad)


class PdfText:
    """一个 PDF 文件的文本对象与表格线（只读）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._raw = self.path.read_bytes()
        self._streams: dict[int, bytes] = {}   # 对象号 → 解压后的流
        self._dicts: dict[int, bytes] = {}     # 对象号 → 字典源文本
        self._objects: dict[int, bytes] = {}   # 对象号 → 对象源文本（含对象流内的）
        self._pages: list[int] = []
        self._runs: dict[int, list[TextRun]] = {}
        self._lines: dict[int, tuple[list[float], list[float]]] = {}
        self._load()

    # ══════════════ 载入 ══════════════

    @classmethod
    def open(cls, path: str | Path) -> "PdfText | None":
        """打不开就返回 None——调用方据此走「证据不足」分支，而不是抛错中断解析。"""
        try:
            obj = cls(path)
        except Exception:  # noqa: BLE001 —— 外部文件格式千奇百怪，一律降级处理
            return None
        return obj if obj.page_count() else None

    def _load(self) -> None:
        raw = self._raw
        for m in re.finditer(rb"(?<![0-9])(\d+)\s+0\s+obj", raw):
            num = int(m.group(1))
            end = raw.find(b"endobj", m.end())
            if end < 0:
                continue
            body = raw[m.end() : end]
            if b"stream" not in body:
                self._objects[num] = body.strip()
                continue
            di, dj = body.find(b"<<"), body.rfind(b">>")
            self._dicts[num] = body[di : dj + 2] if 0 <= di < dj else b""
            s = body.find(b"stream")
            s = body.find(b"\n", s) + 1
            e = body.find(b"endstream", s)
            try:
                # decompressobj 容忍流尾的多余空白，比 decompress 宽容
                self._streams[num] = zlib.decompressobj().decompress(body[s:e])
            except Exception:  # noqa: BLE001 —— 非 Flate 的流直接跳过
                pass

        self._load_object_streams()
        self._pages = self._page_order()

    def _load_object_streams(self) -> None:
        """展开对象流（ObjStm）。

        Word 生成的 PDF 会把页面字典与字体对象压进对象流，不解开就看不到
        ``/Type /Page`` 与 ``/ToUnicode``。对象流本身是「N 个对象的号与偏移
        对」+ 拼接的对象正文，格式简单，无需完整 PDF 解析器。
        """
        for num, data in list(self._streams.items()):
            d = self._dicts.get(num, b"")
            if b"/ObjStm" not in d:
                continue
            mn = re.search(rb"/N\s+(\d+)", d)
            mf = re.search(rb"/First\s+(\d+)", d)
            if not mn or not mf:
                continue
            n, first = int(mn.group(1)), int(mf.group(1))
            nums = [int(x) for x in data[:first].split()]
            for k in range(n):
                if 2 * k + 1 >= len(nums):
                    break
                onum, off = nums[2 * k], nums[2 * k + 1]
                nxt = nums[2 * k + 3] if 2 * k + 3 < len(nums) else len(data) - first
                self._objects[onum] = data[first + off : first + nxt].strip()
            # 对象流本体不再作为普通对象参与后续查找
            self._objects.pop(num, None)

    def _page_order(self) -> list[int]:
        """按页面树的 Kids 顺序取页对象号——顺序即页序，不能按对象号排。

        取不到根节点时退化为「按对象号顺序」，并**由调用方以页数是否与
        pdftotext 一致来兜底**：一致才用，不一致就当没有。
        """
        root = None
        for num, body in self._objects.items():
            if re.search(rb"/Type\s*/Pages", body) and b"/Parent" not in body:
                root = num
                break
        out: list[int] = []

        def walk(num: int) -> None:
            body = self._objects.get(num, b"")
            if re.search(rb"/Type\s*/Page(?![s])", body):
                out.append(num)
                return
            km = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
            if not km:
                return
            for kid in re.findall(rb"(\d+)\s+0\s+R", km.group(1)):
                walk(int(kid))

        if root is not None:
            walk(root)
        if not out:
            out = [
                n
                for n, b in sorted(self._objects.items())
                if re.search(rb"/Type\s*/Page(?![s])", b)
            ]
        return out

    def page_count(self) -> int:
        return len(self._pages)

    # ══════════════ 字体与 CMap ══════════════

    def _cmap(self, objnum: int) -> dict[int, str]:
        data = self._streams.get(objnum)
        if data is None:
            return {}
        txt = data.decode("latin-1")
        out: dict[int, str] = {}
        for blk in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
            for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                out[int(src, 16)] = bytes.fromhex(dst).decode("utf-16-be", "replace")
        for blk in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
            for lo, hi, dst in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk
            ):
                a, b, c = int(lo, 16), int(hi, 16), int(dst, 16)
                for k in range(a, b + 1):
                    out[k] = chr(c + k - a)
        return out

    def _page_fonts(self, pagenum: int) -> dict[str, tuple[bool, dict[int, str]]]:
        """页内字体名（如 ``F2``）→ ``(是否双字节, 码→字符)``。"""
        body = self._objects.get(pagenum, b"")
        ref = re.search(rb"/Resources\s+(\d+)\s+0\s+R", body)
        res = self._objects.get(int(ref.group(1)), b"") if ref else body
        fm = re.search(rb"/Font\s*<<(.*?)>>", res, re.S)
        if not fm:
            fr = re.search(rb"/Font\s+(\d+)\s+0\s+R", res)
            if fr:
                fm = re.search(rb"<<(.*?)>>", self._objects.get(int(fr.group(1)), b""), re.S)
        if not fm:
            return {}
        out: dict[str, tuple[bool, dict[int, str]]] = {}
        for name, ref in re.findall(rb"/([A-Za-z0-9]+)\s+(\d+)\s+0\s+R", fm.group(1)):
            fobj = self._objects.get(int(ref), b"")
            tu = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", fobj)
            out[name.decode()] = (
                b"/Type0" in fobj,
                self._cmap(int(tu.group(1))) if tu else {},
            )
        return out

    @staticmethod
    def _decode(hexstr: str, two_byte: bool, cmap: dict[int, str]) -> str:
        if not hexstr:
            return ""
        if len(hexstr) % 2:
            hexstr += "0"
        b = bytes.fromhex(hexstr)
        if two_byte:
            return "".join(
                cmap.get(int.from_bytes(b[i : i + 2], "big"), "")
                for i in range(0, len(b) - 1, 2)
            )
        return "".join(cmap.get(c, chr(c) if 32 <= c < 127 else "") for c in b)

    # ══════════════ 文本串 ══════════════

    def _content(self, pageno: int) -> str | None:
        if not 1 <= pageno <= len(self._pages):
            return None
        body = self._objects.get(self._pages[pageno - 1], b"")
        ref = re.search(rb"/Contents\s+(\d+)\s+0\s+R", body)
        if not ref:
            return None
        data = self._streams.get(int(ref.group(1)))
        return None if data is None else data.decode("latin-1")

    #: 只认这四类操作符：换字体、定位、显示文本。
    #: ``TJ`` 数组里数字是字距微调，必须跳过只取 ``<...>``——
    #: Word 会把 ``2025/12/2`` 拆成 ``[<0015>-6<0013>7...]``，
    #: 按整体字节搜是搜不到的（这个坑实测踩过）。
    _TOKEN = re.compile(
        r"/([A-Za-z0-9]+)\s+" + _NUM + r"\s+Tf"
        r"|(?:" + _NUM + r"\s+){4}(" + _NUM + r")\s+(" + _NUM + r")\s+Tm"
        r"|\[(.*?)\]\s*TJ"
        r"|<([0-9A-Fa-f]*)>\s*Tj"
        r"|\(" r"((?:\\.|[^\\()])*)\)\s*Tj",
        re.S,
    )

    def page_runs(self, pageno: int) -> list[TextRun]:
        """该页全部文本串，按内容流顺序返回。取不到返回 ``[]``。"""
        if pageno in self._runs:
            return self._runs[pageno]
        txt = self._content(pageno)
        if txt is None:
            return []
        fonts = self._page_fonts(self._pages[pageno - 1])
        runs: list[TextRun] = []
        font, x, y = "", 0.0, 0.0
        for m in self._TOKEN.finditer(txt):
            if m.group(1) is not None:
                font = m.group(1)
                continue
            if m.group(2) is not None:
                x, y = float(m.group(2)), float(m.group(3))
                continue
            two, cmap = fonts.get(font, (True, {}))
            if m.group(4) is not None:
                s = "".join(
                    self._decode(h, two, cmap)
                    for h in re.findall(r"<([0-9A-Fa-f]*)>", m.group(4))
                )
            elif m.group(5) is not None:
                s = self._decode(m.group(5), two, cmap)
            elif m.group(6) is not None:
                s = self._decode(m.group(6).encode("latin-1").hex(), two, cmap)
            else:  # pragma: no cover —— 正则已穷尽分支
                continue
            if s:
                runs.append(TextRun(x, y, font, s))
        self._runs[pageno] = runs
        return runs

    # ══════════════ 表格线 ══════════════

    #: 描边路径的起笔/落笔与绘制操作符
    _PATH = re.compile(r"(" + _NUM + r")\s+(" + _NUM + r")\s+(m|l)\b|(S|s|f|f\*|B|b|n|W|W\*)\b")

    def _page_lines(self, pageno: int) -> tuple[list[float], list[float]]:
        """该页的竖线 x 集合与横线 y 集合。

        表格边框有两种画法，都要认：
        · Word 用**极窄的实心矩形**（``0.72 0 0 100 re f``），
        · 别的生成器用**描边线段**（``x y m x2 y2 l S``）。
        只取「长而细」的：宽 ≤1.5pt 且高 >4pt 记竖线，反之记横线。
        裁剪路径（``re W n``）必须排除，它同样以矩形出现但不是边框。
        """
        if pageno in self._lines:
            return self._lines[pageno]
        txt = self._content(pageno)
        if txt is None:
            return ([], [])
        xs: set[float] = set()
        ys: set[float] = set()

        for m in re.finditer(
            r"(" + _NUM + r")\s+(" + _NUM + r")\s+(" + _NUM + r")\s+(" + _NUM
            + r")\s+re\s*(f\*|f|S|B|b)?",
            txt,
        ):
            x, y, w, h = (float(m.group(i)) for i in range(1, 5))
            tail = txt[m.end() : m.end() + 6].lstrip()
            if tail.startswith("W"):  # 裁剪路径
                continue
            if w <= 1.5 < h:
                xs.add(round(x, 2))
            elif h <= 1.5 < w:
                ys.add(round(y, 2))

        cur: tuple[float, float] | None = None
        for m in self._PATH.finditer(txt):
            if m.group(1) is not None:
                pt = (float(m.group(1)), float(m.group(2)))
                if cur is not None and m.group(3) == "l":
                    (x0, y0), (x1, y1) = cur, pt
                    if abs(x1 - x0) <= 1.5 and abs(y1 - y0) > 4:
                        xs.add(round(x0, 2))
                    elif abs(y1 - y0) <= 1.5 and abs(x1 - x0) > 4:
                        ys.add(round(y0, 2))
                cur = pt
            else:
                cur = None
        self._lines[pageno] = (sorted(xs), sorted(ys))
        return self._lines[pageno]

    def cell_at(self, pageno: int, x: float, y: float) -> Cell | None:
        """点 ``(x, y)`` 所在的最小网格单元；取不到返回 None。

        用「最近的左/右竖线 + 最近的上/下横线」取格。只要能取到，
        结果就**不会大于**真实单元格（真格的边框本身也在候选里），
        所以漏掉一条线只会让格子变大致使判据失败，不会误判成功。
        """
        xs, ys = self._page_lines(pageno)
        left = [v for v in xs if v <= x]
        right = [v for v in xs if v > x]
        below = [v for v in ys if v <= y]
        above = [v for v in ys if v > y]
        if not (left and right and below and above):
            return None
        return Cell(max(left), max(below), min(right), min(above))
