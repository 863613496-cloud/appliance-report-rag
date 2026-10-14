"""【未实现】把年报 PDF 解析为带页码的文本与文本块。

状态：骨架占位，尚未编写任何解析逻辑。

计划流程：
    1. 用 pypdf 抽取每页文本，保留页码（评测时需要给出「来源页码」）；
    2. 按标点与段落切分为文本块，块长 CHUNK_SIZE、重叠 CHUNK_OVERLAP；
    3. 每个文本块携带元数据：证券代码、公司简称、报告年份、页码，供检索时回引；
    4. 输出到 data/processed/{证券代码}_{报告年份}.jsonl（或 .txt）。

待解决的已知难点：
    * 部分年报为扫描件或含大量表格，纯文本抽取会丢失结构；
      若发现此类文件，需回退到 pdfplumber/OCR，并在 companies.csv 的备注中标注。
    * 财务数据以表格为主，抽取后需人工抽查若干关键数字是否与 PDF 一致。
"""

from __future__ import annotations

from pathlib import Path


def parse_pdf(pdf_path: Path) -> list[dict]:
    """解析单个 PDF，返回文本块列表。"""
    raise NotImplementedError("解析功能尚未实现。当前 data/processed/ 应为空。")


def parse_all() -> list[Path]:
    """解析 data/raw/reports/ 下全部 PDF，返回生成的产物路径。"""
    raise NotImplementedError("解析功能尚未实现。")


if __name__ == "__main__":
    print("提示：解析逻辑尚未实现，此脚本当前不会读写任何文件。")
