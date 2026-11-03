# 系统说明：构建、检索、字段与产物

本文对应第三课 p163 的要求：**仓库内说明构建与检索代码、公司/章节/页码字段、以及产物位置**。

---

## 一、流水线总览

```
巨潮资讯网
   │  src/download.py --check      （只检索候选，不下载）
   │  src/download.py --download   （下载 PDF 到 data/raw/）
   ▼
data/raw/*.pdf                       14 份 2025 年年度报告全文
   │  src/parse.py + src/parse_policy.py
   ▼
data/processed/*.jsonl               按页切块，含表格行列还原
   │  src/index.py
   ▼
indexes/retrieval_v1/                BM25 倒排 + 向量矩阵
   │  src/retrieve.py
   ▼
app.py (Streamlit)                   问答页面，答案带出处
```

### 各步骤的命令

| 步骤 | 命令 | 产出 |
|---|---|---|
| 检索候选公告 | `PYTHONPATH=. python src/download.py --check --json data_sources.json` | `data_sources.json` |
| 下载原文 | `PYTHONPATH=. python src/download.py --download` | `data/raw/000333_美的集团_2025_年度报告.pdf` 等 14 份 |
| 解析与切块 | `python -m src.parse` | `data/processed/{code}_2025.jsonl` |
| 建索引 | `python -m src.index` | `indexes/retrieval_v1/` |
| 启动问答页面 | `streamlit run app.py` | `http://localhost:8501` |

---

## 二、切块字段定义（公司 / 章节 / 页码）

每个块是一个 JSON 对象，写入 `data/processed/{code}_2025.jsonl`。字段如下：

| 字段 | 类型 | 示例 | 说明 |
|---|---|---|---|
| `doc_id` | str | `000333_2025_p0135_c0001` | 唯一标识，格式 `{代码}_{年份}_p{页码}_c{块序号}` |
| `证券代码` | str | `000333` | 6 位代码 |
| `公司简称` | str | `美的集团` | 与 `config/companies.csv` 一致 |
| `报告年份` | int | `2025` | 报告年度 |
| `页码` | int | `135` | **PDF 物理页**，供答案标注出处 |
| `章节` | str | `第八节 财务报告` | 章节归属，由章节标题识别得到 |
| `类型` | str | `table` / `prose` | 表格块或正文块 |
| `正文` | str | `一、营业总收入 458,502,407 409,084,266 …` | 表格已按行列还原为文本，保留表头行 |
| `块序号` | int | `1` | 同一页内的块顺序 |
| `字符数` | int | `2766` | 便于控制上下文长度 |

> `页码` 是 PDF 物理页码（`pdfplumber` 的 1-based 页码），与年报印刷页码可能存在 1 页偏差——这一点在人工核验报告 Q01 中已作为真实缺陷记录。

### 表格行列还原的做法

1. `pdfplumber` 的 `extract_tables()` 取表格的二维单元格矩阵，而非 `extract_text()`；
2. 每行拼接为 `单元格A | 单元格B | 单元格C`，**保留表头行**，让检索时"指标名 + 数值"同块出现；
3. 跨页续表：向上回溯最近一次表头行并继承（`src/parse_policy.py`），无法确定表头时在块上打 `blocked=true` 标记，检索时降级为候选证据；
4. 同一报告内两张表对同一科目金额冲突时，不自动择一，改为在答案中并列披露（Q04、Q10 的实际处理方式）。

---

## 三、检索实现

三处代码，职责分明：

| 文件 | 职责 |
|---|---|
| `src/index.py` | 建索引：BM25 倒排表 + 向量矩阵 |
| `src/retrieve.py` | 查询：两路召回 + RRF 融合 |
| `src/answer.py` | 组装答案：只允许引用 `retrieve` 返回的块，逐条标注页码 |

### 两路索引的具体实现

**BM25（关键词通道）**
- 参数 `k1=1.5, b=0.75`
- 中文按 2–3 字 n-gram 切分，ASCII 按词切分，另加领域短语词典
- 词项数 328,072；平均文档长度 815.8
- 对指标名（"营业总收入"、"归属于母公司股东的净利润"）命中率高

**向量（语义通道）**
- 字符 n-gram 哈希 TF-IDF，8192 维，L2 归一化，余弦相似度
- **说明：不是神经嵌入（非 BGE/FAISS）**，是无训练成本的字符级向量，用于补 BM25 对同义表述不敏感的短板
- 真实的神经嵌入升级列为后续工作

**融合**
- 倒数排名融合 RRF，`k=60`
- 保留两通道各自的排名与分数，便于逐题分析"是 BM25 捞到的还是向量捞到的"

### 索引规模（`indexes/retrieval_v1/index_manifest.json`）

| 项 | 值 |
|---|---|
| 公司数 | 14 |
| 文档（块）总数 | 3,470 |
| 表格块 | 2,573 |
| 正文块 | 897 |
| 总字符数 | 5,161,959 |
| BM25 词项数 | 328,072 |
| 构建时间 | 2026-10-21 14:44:48 |

---

## 四、产物位置

| 产物 | 路径 | 是否入库 |
|---|---|---|
| 公司清单（含来源 URL、校验信息） | `config/companies.csv` | ✅ 入库 |
| 公告检索结果 | `data_sources.json` | ✅ 入库 |
| 原始财报 PDF | `data/raw/*.pdf` | ❌ 不入库（`.gitignore`，体积大） |
| 解析后的块（jsonl） | `data/processed/*.jsonl` | ❌ 不入库（可由 PDF 重建） |
| BM25 + 向量索引 | `indexes/retrieval_v1/` | ❌ 不入库（可由 jsonl 重建） |
| 问答页面 | `app.py` | ✅ 入库 |
| 页面截图 | `screenshots/` | ✅ 入库 |
| 逐题测试与人工核验 | `deliverables/` | ✅ 入库 |

> 除 PDF 外的所有中间产物均可由 `data_sources.json` 中的官方 URL 重新下载并一键重建，故不入库。

---

## 五、测评设计（对应"至少 2 道跨公司全景题"）

题库在 `config/questions.csv`（10 题，未改题），其中跨公司题 3 道：

| 题号 | 类型 | 考察点 |
|---|---|---|
| Q01–Q07 | 单公司 | 财务指标、分部信息、研发投入、利润分配、销售模式 |
| Q08 | 跨公司·财务对比 | 三家头部营收/净利排序（同口径，可答） |
| Q09 | 跨公司·同业对比 | 石头 vs 科沃斯 境外占比 + 研发强度 |
| Q10 | 跨公司·综合 | 14 家境外收入占比前三名（口径不一致，触发拒答） |

逐题记录见 `deliverables/manual_evidence_review_2025.md` 与《人工核验报告》。
