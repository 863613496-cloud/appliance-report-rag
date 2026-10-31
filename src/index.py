"""构建本地、可审计的混合检索索引（BM25 + 字符 n-gram TF-IDF 向量）。

为什么不是神经语义索引
----------------------
本机没有可离线使用的中文 embedding 模型，也没有安装 torch /
sentence-transformers / faiss / chromadb；把年报正文发到外部接口又不符合
本项目的合规约定。因此本模块**不下载模型、不调用外部服务**，只使用
numpy 实现一个可复现的本地检索原型：

* 关键词通道：BM25，中文 2–3 字 n-gram + ASCII 词 + 领域短语；
* 向量通道：字符 n-gram 的哈希 TF-IDF 向量（8192 维，L2 归一化），
  用余弦相似度召回。它是**词形/字符级向量，不是神经语义嵌入**；
* 融合：倒数排名融合（RRF），保留两个通道各自的排名与分数。

索引产物写入 indexes/retrieval_v1/，该目录被 .gitignore 忽略。原始
data/processed/*.jsonl 与 PDF 只读，构建过程不会改写它们。

表格策略
--------
解析阶段的表格页本来就是“整页一个 chunk”。本模块**不再切分表格块**，
原样保存 pdftotext -table 的阅读顺序，并单独记录单位行、行数、表头跨行
警告等可核对信息；不尝试猜测表头与数据列的对应关系。
"""

from __future__ import annotations

import argparse
import collections
import csv
import gzip
import hashlib
import json
import math
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from src.config import (
    COMPANIES_CSV,
    CSV_ENCODING,
    EXPECTED_COMPANY_COUNT,
    INDEX_DIR,
    LOGS_DIR,
    PROCESSED_DIR,
    RAW_REPORTS_DIR,
    REPORT_YEAR,
)

INDEX_VERSION = "retrieval_v1"
DEFAULT_INDEX_PATH = INDEX_DIR / INDEX_VERSION
VECTOR_DIM = 8192
NGRAM_SIZES = (2, 3)
BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60
PACK_BASE = 10_000
DEFAULT_TOP_K = 8

#: 只影响“是否已确认”，不改变原始检测记录。
BLOCKING_KINDS = {"污染标签", "模式间不一致", "日期折行截断"}

#: 领域短语既作为 BM25 词项，也作为向量 token。顺序无关。
PHRASES = (
    "营业总收入",
    "营业收入",
    "归属于母公司股东的净利润",
    "归属于母公司所有者的净利润",
    "归属于上市公司股东的净利润",
    "归母净利润",
    "净利润",
    "毛利率",
    "境外收入",
    "境外营业收入",
    "国外收入",
    "海外收入",
    "境内收入",
    "国内收入",
    "研发投入",
    "研发投入合计",
    "研发费用",
    "研发投入强度",
    "利润分配",
    "利润分配预案",
    "现金分红",
    "每10股",
    "线上销售",
    "线上渠道",
    "电商渠道",
    "主营业务",
    "分产品",
    "分地区",
    "分行业",
    "空调",
    "家电",
    "年度报告",
    "单位:元",
    "单位:万元",
    "人民币元",
    "同比",
    "占比",
    "比重",
)

CJK_RE = re.compile(r"[㐀-䶿一-鿿]+")
ASCII_RE = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*%?")
UNIT_RE = re.compile(r"(?:金额\s*)?单位\s*(?:为|[:：])\s*[^\s，,；;）)]+")
NUMERIC_RE = re.compile(r"\d")


class IndexBuildError(RuntimeError):
    """索引构建前置条件不满足。"""


@dataclass
class RetrievalIndex:
    """已加载的检索索引。只读使用，不修改 documents 中的原文。"""

    path: Path
    manifest: dict
    documents: list[dict]
    vectors: np.ndarray
    idf: np.ndarray
    postings: dict[str, list[int]]
    doc_len: list[int]
    avgdl: float
    k1: float
    b: float
    dim: int
    rrf_k: int

    @property
    def count(self) -> int:
        return len(self.documents)

    @property
    def codes(self) -> list[str]:
        seen: list[str] = []
        for doc in self.documents:
            code = doc["证券代码"]
            if code not in seen:
                seen.append(code)
        return seen


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """NFKC + 小写；不删除标点，避免改变原文语义。"""
    return unicodedata.normalize("NFKC", text or "").lower()


def tokenize(text: str) -> list[str]:
    """把文本切成可复现的检索 token。

    - 中文连续段：2–3 字 n-gram；单字段落保留 1 字 token；
    - ASCII 字母/数字：整段作为一个 token（保留逗号小数、百分号）；
    - 领域短语：命中时额外加入 ``L:`` token，增强精确匹配。
    """
    normalized = normalize_text(text)
    tokens: list[str] = []

    for run in CJK_RE.findall(normalized):
        if len(run) == 1:
            tokens.append("c:" + run)
            continue
        for size in NGRAM_SIZES:
            if len(run) < size:
                continue
            for start in range(len(run) - size + 1):
                tokens.append("c:" + run[start:start + size])

    for word in ASCII_RE.findall(normalized):
        tokens.append("w:" + word)

    for phrase in PHRASES:
        if phrase in normalized:
            tokens.append("L:" + phrase)

    return tokens


def hash_token(token: str, dim: int = VECTOR_DIM) -> tuple[int, float]:
    """稳定哈希到 [0, dim)，并返回 ±1 符号，降低桶碰撞的系统性偏差。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "little")
    bucket = value % dim
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return bucket, sign


def query_token_counts(question: str) -> collections.Counter[str]:
    """查询词频；额外加入少量会在年报中出现的同义表述。

    这些扩展只增加召回机会，不改变任何原文。扩展可能把“营业总收入”与
    “营业收入”等口径混在一起，因此检索结果必须回到原文核对。
    """
    normalized = normalize_text(question)
    tokens = tokenize(question)
    expansions = {
        "归母净利润": (
            "归属于母公司股东的净利润",
            "归属于上市公司股东的净利润",
        ),
        "境外收入": ("海外收入", "国外收入"),
        "境内收入": ("国内收入",),
        "研发投入": ("研发费用",),
        "线上销售": ("线上渠道", "电商渠道"),
        "利润分配": ("现金分红", "每10股"),
        "空调业务": ("空调",),
        "产品品类": ("分产品",),
    }
    for trigger, alternatives in expansions.items():
        if trigger in normalized:
            for alternative in alternatives:
                tokens.extend(tokenize(alternative))
    return collections.Counter(tokens)


# ---------------------------------------------------------------------------
# 读取与校验
# ---------------------------------------------------------------------------


def load_companies(path: Path = COMPANIES_CSV) -> list[dict[str, str]]:
    with path.open(encoding=CSV_ENCODING, newline="") as fh:
        rows = [dict(row) for row in csv.DictReader(fh)]
    if len(rows) != EXPECTED_COMPANY_COUNT:
        raise IndexBuildError(
            f"公司名单应为 {EXPECTED_COMPANY_COUNT} 家，实际 {len(rows)} 家：{path}"
        )
    codes = [row["证券代码"] for row in rows]
    if len(codes) != len(set(codes)):
        raise IndexBuildError("config/companies.csv 中存在重复证券代码")
    for row in rows:
        if row.get("解析状态") != "已解析":
            raise IndexBuildError(
                f'{row["证券代码"]} 解析状态为 {row.get("解析状态")!r}，不能建索引'
            )
        if not row.get("解析输出文件"):
            raise IndexBuildError(f'{row["证券代码"]} 缺少解析输出文件字段')
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise IndexBuildError(f"{path} 第 {lineno} 行不是合法 JSON：{exc}") from exc
    return rows


def _load_page_anomalies(code: str) -> dict[int, list[dict]]:
    """从解析台账读取逐页异常，保留原始检测字段。"""
    path = LOGS_DIR / f"parse_{code}_2025.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    grouped: dict[int, list[dict]] = {}
    for finding in payload.get("异常明细", []):
        page = int(finding["page"])
        grouped.setdefault(page, []).append(
            {
                "page": page,
                "kind": finding.get("kind"),
                "detail": finding.get("detail"),
                "excerpt": finding.get("excerpt"),
                "status": finding.get("status"),
                "blocks": bool(finding.get("blocks")),
                "extra": finding.get("extra", {}),
                "页类型": finding.get("页类型"),
            }
        )
    return grouped


def table_metadata(chunk: dict) -> dict:
    """记录表格块的完整性线索，不猜测表头与数据列的对应关系。"""
    if chunk.get("类型") != "table":
        return {"整页保留": False}
    text = chunk.get("正文", "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    unit_lines = [line for line in lines if UNIT_RE.search(line)]
    return {
        "整页保留": True,
        "行数": len(lines),
        "含数字行数": sum(1 for line in lines if NUMERIC_RE.search(line)),
        "单位行": unit_lines[:5],
        "表头跨行警告": "表头跨行" in chunk.get("需复核", []),
        "说明": (
            "表格页在解析阶段整页作为一个 chunk；索引原样保存 -table "
            "阅读顺序，未按行切分，也未推断列对齐关系。"
        ),
    }


def _make_document(
    chunk: dict,
    *,
    code: str,
    name: str,
    pdf_name: str,
    anomalies: list[dict],
    doc_id: str,
) -> dict:
    review = list(chunk.get("需复核") or [])
    date_recovery = list(chunk.get("日期恢复") or [])
    blocking = [a for a in anomalies if a.get("blocks")]
    kinds = sorted({a["kind"] for a in anomalies if a.get("kind")})
    warnings: list[str] = []
    for kind in review:
        warnings.append(f"解析复核标记：{kind}")
    for kind in kinds:
        if kind not in review:
            warnings.append(f"页面异常：{kind}")
    for item in date_recovery:
        warnings.append(f"日期恢复记录：{item}")

    document = {
        "doc_id": doc_id,
        "证券代码": code,
        "公司简称": name,
        "报告年份": chunk.get("报告年份", REPORT_YEAR),
        "PDF文件名": pdf_name,
        "页码": int(chunk["页码"]),
        "块序号": int(chunk.get("块序号", 1)),
        "类型": chunk.get("类型"),
        "章节": chunk.get("章节"),
        "字符数": int(chunk.get("字符数", len(chunk.get("正文", "")))),
        "需复核": review,
        "日期恢复": date_recovery,
        "正文": chunk.get("正文", ""),
        "warnings": warnings,
        "anomalies": anomalies,
        "blocking_anomalies": blocking,
        "blocked": bool(blocking),
        "reliable_for_deterministic_answer": not bool(blocking),
        "table": table_metadata(chunk),
        "source": {
            "citation": f"{name}（{code}）{chunk.get('报告年份', REPORT_YEAR)} 年年度报告 p{int(chunk['页码'])}",
            "pdf": pdf_name,
            "page": int(chunk["页码"]),
            "chunk_type": chunk.get("类型"),
            "section": chunk.get("章节"),
        },
    }
    return document


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def _validate_chunk(chunk: dict, code: str, name: str, pdf_name: str) -> None:
    required = ("页码", "块序号", "类型", "正文")
    missing = [key for key in required if key not in chunk]
    if missing:
        raise IndexBuildError(f"{code} 的 chunk 缺少字段：{missing}")
    if chunk.get("证券代码") != code:
        raise IndexBuildError(f"{code} 的 chunk 证券代码不一致：{chunk.get('证券代码')}")
    if chunk.get("公司简称") != name:
        raise IndexBuildError(f"{code} 的 chunk 公司简称不一致：{chunk.get('公司简称')}")
    if int(chunk.get("报告年份", 0)) != REPORT_YEAR:
        raise IndexBuildError(f"{code} 的 chunk 报告年份不是 {REPORT_YEAR}")
    if chunk.get("PDF文件名") != pdf_name:
        raise IndexBuildError(
            f"{code} 的 chunk PDF文件名不一致：{chunk.get('PDF文件名')} != {pdf_name}"
        )
    if int(chunk["页码"]) < 1:
        raise IndexBuildError(f"{code} 的 chunk 页码非法：{chunk['页码']}")
    if chunk.get("类型") not in {"table", "prose"}:
        raise IndexBuildError(f"{code} 的 chunk 类型未知：{chunk.get('类型')}")
    if not str(chunk.get("正文", "")).strip():
        raise IndexBuildError(f"{code} p{chunk['页码']} 存在空正文 chunk")


def _build_lexical(
    texts: Sequence[str],
) -> tuple[dict[str, list[int]], list[int], np.ndarray, float]:
    postings: dict[str, list[int]] = {}
    doc_len: list[int] = []
    vector_df = np.zeros(VECTOR_DIM, dtype=np.int32)

    for doc_index, text in enumerate(texts):
        counts = collections.Counter(tokenize(text))
        doc_len.append(sum(counts.values()))
        buckets = set()
        for token, tf in counts.items():
            bucket, _ = hash_token(token)
            buckets.add(bucket)
            packed = doc_index * PACK_BASE + min(tf, PACK_BASE - 1)
            existing = postings.get(token)
            if existing is None:
                postings[token] = [packed]
            else:
                existing.append(packed)
        if buckets:
            vector_df[list(buckets)] += 1

    if not doc_len:
        raise IndexBuildError("没有可建索引的文档")
    avgdl = sum(doc_len) / len(doc_len)
    return postings, doc_len, vector_df, avgdl


def _build_vectors(texts: Sequence[str], df: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    count = len(texts)
    idf = np.log((1.0 + count) / (1.0 + df.astype(np.float64))) + 1.0
    vectors = np.zeros((count, VECTOR_DIM), dtype=np.float32)

    for doc_index, text in enumerate(texts):
        counts = collections.Counter(tokenize(text))
        accumulator: dict[int, float] = {}
        for token, tf in counts.items():
            bucket, sign = hash_token(token)
            weight = (1.0 + math.log(tf)) * float(idf[bucket]) * sign
            accumulator[bucket] = accumulator.get(bucket, 0.0) + weight
        if not accumulator:
            continue
        indices = np.fromiter(accumulator.keys(), dtype=np.int32)
        values = np.fromiter(accumulator.values(), dtype=np.float32)
        vectors[doc_index, indices] = values
        norm = float(np.linalg.norm(vectors[doc_index]))
        if norm:
            vectors[doc_index] /= norm

    return vectors, idf


def build_index(
    processed_dir: Path | None = None,
    output_dir: Path | None = None,
) -> Path:
    """构建索引并返回索引目录。

    只读取 data/processed/*.jsonl、config/companies.csv、原始 PDF（计算
    SHA-256）和 logs/parse_*_2025.json（读取异常）。所有写入都发生在
    output_dir 的临时目录中，成功后再替换目标目录。
    """
    processed_dir = Path(processed_dir) if processed_dir else PROCESSED_DIR
    output_dir = Path(output_dir) if output_dir else DEFAULT_INDEX_PATH
    companies = load_companies()

    docs: list[dict] = []
    source_files: list[dict] = []
    anomaly_counts: collections.Counter[str] = collections.Counter()
    company_stats: list[dict] = []

    for row in companies:
        code = row["证券代码"]
        name = row["公司简称"]
        pdf_name = row["文件名"]
        jsonl_path = Path(row["解析输出文件"])
        if not jsonl_path.is_absolute():
            jsonl_path = processed_dir.parent.parent / jsonl_path
        # CSV 中存的是 data/processed/... 相对项目根目录的路径。
        if not jsonl_path.exists():
            candidate = processed_dir / f"{code}_2025.jsonl"
            if candidate.exists():
                jsonl_path = candidate
        if not jsonl_path.exists():
            raise IndexBuildError(f"{code} 的 JSONL 不存在：{jsonl_path}")

        pdf_path = RAW_REPORTS_DIR / pdf_name
        if not pdf_path.exists():
            raise IndexBuildError(f"{code} 的原始 PDF 不存在：{pdf_path}")

        chunks = _read_jsonl(jsonl_path)
        if not chunks:
            raise IndexBuildError(f"{code} 的 JSONL 为空")
        page_anomalies = _load_page_anomalies(code)
        # 异常按“页”计数；一页可能有多个正文 chunk，不能重复计数。
        for page_findings in page_anomalies.values():
            for anomaly in page_findings:
                if anomaly.get("kind"):
                    anomaly_counts[anomaly["kind"]] += 1
        seen_chunk_keys: set[tuple[int, int]] = set()

        for chunk in chunks:
            _validate_chunk(chunk, code, name, pdf_name)
            key = (int(chunk["页码"]), int(chunk.get("块序号", 1)))
            if key in seen_chunk_keys:
                raise IndexBuildError(f"{code} 出现重复的页码/块序号：{key}")
            seen_chunk_keys.add(key)
            doc_id = f"{code}_{REPORT_YEAR}_p{int(chunk['页码']):04d}_c{int(chunk.get('块序号', 1)):04d}"
            anomalies = page_anomalies.get(int(chunk["页码"]), [])
            docs.append(
                _make_document(
                    chunk,
                    code=code,
                    name=name,
                    pdf_name=pdf_name,
                    anomalies=anomalies,
                    doc_id=doc_id,
                )
            )

        stats = {
            "证券代码": code,
            "公司简称": name,
            "块数": len(chunks),
            "表格块": sum(1 for c in chunks if c.get("类型") == "table"),
            "正文块": sum(1 for c in chunks if c.get("类型") == "prose"),
            "页数": int(row.get("页码数") or max(int(c["页码"]) for c in chunks)),
            "JSONL": str(jsonl_path.relative_to(processed_dir.parent.parent)),
            "JSONL_SHA256": sha256_file(jsonl_path),
            "PDF文件名": pdf_name,
            "PDF_SHA256": sha256_file(pdf_path),
            "PDF字节": pdf_path.stat().st_size,
        }
        company_stats.append(stats)
        source_files.append(stats)

    texts = [doc["正文"] for doc in docs]
    postings, doc_len, vector_df, avgdl = _build_lexical(texts)
    vectors, idf = _build_vectors(texts, vector_df)

    manifest = {
        "索引版本": INDEX_VERSION,
        "构建时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "状态": "真实构建完成",
        "检索后端": {
            "关键词": (
                "BM25（k1=1.5, b=0.75），中文 2–3 字 n-gram + ASCII 词 + 领域短语"
            ),
            "向量": (
                "字符 n-gram 哈希 TF-IDF 向量（8192 维，L2 归一化，余弦相似度）；"
                "非神经语义嵌入，不是 BGE/FAISS"
            ),
            "融合": "倒数排名融合（RRF, k=60），保留两通道排名与分数",
        },
        "依赖": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "外部模型": "无（未下载、未调用）",
            "外部服务": "无",
        },
        "参数": {
            "向量维度": VECTOR_DIM,
            "n-gram": list(NGRAM_SIZES),
            "BM25_k1": BM25_K1,
            "BM25_b": BM25_B,
            "RRF_k": RRF_K,
            "默认TopK": DEFAULT_TOP_K,
            "表格块策略": "整页保留，不切分",
        },
        "统计": {
            "公司数": len(companies),
            "文档数": len(docs),
            "表格块": sum(1 for d in docs if d["类型"] == "table"),
            "正文块": sum(1 for d in docs if d["类型"] == "prose"),
            "总字符数": sum(len(d["正文"]) for d in docs),
            "BM25词项数": len(postings),
            "平均文档长度": round(avgdl, 3),
            "异常记录数": int(sum(anomaly_counts.values())),
            "异常分类": dict(sorted(anomaly_counts.items())),
        },
        "公司": company_stats,
        "文件": {
            "documents": "documents.jsonl",
            "vectors": "vectors.npy",
            "vectorizer": "vectorizer.json",
            "bm25": "bm25.json.gz",
        },
        "限制": [
            "向量通道是字符 n-gram TF-IDF，不是神经语义模型；同义改写召回弱于 embedding。",
            "表格块整页保留，未推断表头与列的对应关系；表头跨行警告可能很多是排版误报。",
            "带 blocks=true 异常的 chunk 仍可检索，但 reliable_for_deterministic_answer=false。",
        ],
    }

    tmp_dir = output_dir.with_name(output_dir.name + ".build-tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    with (tmp_dir / "documents.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for doc in docs:
            fh.write(json.dumps(doc, ensure_ascii=False) + "\n")

    np.save(tmp_dir / "vectors.npy", vectors)

    vectorizer = {
        "维度": VECTOR_DIM,
        "n-gram": list(NGRAM_SIZES),
        "哈希": "blake2b-64，带 ±1 符号",
        "tokenizer": "NFKC + 中文 2–3 字 n-gram + ASCII 词 + 领域短语",
        "短语": list(PHRASES),
        "idf": [round(float(value), 8) for value in idf],
    }
    (tmp_dir / "vectorizer.json").write_text(
        json.dumps(vectorizer, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with gzip.open(tmp_dir / "bm25.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(
            {
                "k1": BM25_K1,
                "b": BM25_B,
                "avgdl": avgdl,
                "doc_len": doc_len,
                "postings": postings,
            },
            fh,
            ensure_ascii=False,
        )

    (tmp_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    if output_dir.exists():
        manifest_path = output_dir / "manifest.json"
        if not manifest_path.exists():
            raise IndexBuildError(
                f"目标目录已存在且不是本项目索引，拒绝覆盖：{output_dir}"
            )
        shutil.rmtree(output_dir)
    tmp_dir.rename(output_dir)
    return output_dir


def load_index(index_path: Path | None = None) -> RetrievalIndex:
    """加载并校验索引。索引缺失时明确报错，不自动构建。"""
    path = Path(index_path) if index_path else DEFAULT_INDEX_PATH
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"索引不存在：{path}。请先运行 `python -m src.index` 构建索引。"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("索引版本") != INDEX_VERSION:
        raise IndexBuildError(
            f"索引版本不匹配：{manifest.get('索引版本')} != {INDEX_VERSION}"
        )

    documents = _read_jsonl(path / "documents.jsonl")
    vectors = np.load(path / "vectors.npy")
    vectorizer = json.loads((path / "vectorizer.json").read_text(encoding="utf-8"))
    with gzip.open(path / "bm25.json.gz", "rt", encoding="utf-8") as fh:
        bm25 = json.load(fh)

    dim = int(vectorizer["维度"])
    if vectors.shape != (len(documents), dim):
        raise IndexBuildError(
            f"向量形状 {vectors.shape} 与文档数/维度 {(len(documents), dim)} 不一致"
        )
    if len(bm25["doc_len"]) != len(documents):
        raise IndexBuildError("BM25 文档长度数量与文档数不一致")

    return RetrievalIndex(
        path=path,
        manifest=manifest,
        documents=documents,
        vectors=vectors,
        idf=np.asarray(vectorizer["idf"], dtype=np.float64),
        postings=bm25["postings"],
        doc_len=bm25["doc_len"],
        avgdl=float(bm25["avgdl"]),
        k1=float(bm25["k1"]),
        b=float(bm25["b"]),
        dim=dim,
        rrf_k=int(manifest.get("参数", {}).get("RRF_k", RRF_K)),
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建本地混合检索索引")
    parser.add_argument("--processed-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    path = build_index(args.processed_dir, args.output_dir)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    stats = manifest["统计"]
    print(f"索引已建立：{path}")
    print(
        f"公司 {stats['公司数']} 家，文档 {stats['文档数']} 块"
        f"（表格 {stats['表格块']} / 正文 {stats['正文块']}），"
        f"总字符 {stats['总字符数']:,}"
    )
    print(
        f"BM25 词项 {stats['BM25词项数']:,}，向量维度 {manifest['参数']['向量维度']}，"
        f"异常记录 {stats['异常记录数']:,}"
    )
    print(f"索引文件：{', '.join(manifest['文件'].values())}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
