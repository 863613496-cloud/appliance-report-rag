# 稳定性检查报告（2025 年年报 RAG 项目）

> 检查日期：2026-10-21（跨页表头修复后复检）
> 检查范围：当前工作区、188 项测试、`src/check_setup.py`、10 题检索评测、
> README 与检索报告一致性。
>
> 本轮**未提交 Git**、**未开发 Streamlit UI**、**未生成最终回答**、
> **未填写答案准确率**、**未新增模型/外部 API/依赖**。
> 原始 PDF、解析 JSONL、索引、异常记录和 blocked 标记均未删除或覆盖。

---

## 1. 实际执行的命令与结果

### 1.1 Git 状态

```bash
git status --short
git diff --stat
git diff --check
```

结果：

| 命令 | 实际结果 |
|---|---|
| `git status --short` | 分支 `main`；10 个已修改 tracked 文件；20 个 untracked 条目（含目录）；无 staged 文件 |
| `git diff --stat` | 10 files changed, **3,394 insertions(+), 119 deletions(−)** |
| `git diff --check` | **exit 0**，无空白/冲突标记问题 |

`git status --short` 的 untracked 条目包括：
`.gitattributes`、`deliverables/answer_reference_review_2025.md`、
`deliverables/answer_rules_2025.md`、`deliverables/baseline/`、
`deliverables/parse_quality_check.py`、`deliverables/parse_quality_report_2025.json`、
`deliverables/parse_quality_report_2025.md`、`deliverables/q08_p122_diagnosis_2025.md`、
`deliverables/retrieval_eval_2025.py`、`deliverables/retrieval_failure_cases_2025.md`、
`deliverables/retrieval_improvement_2025.md`、`deliverables/retrieval_prototype_2025.md`、
`deliverables/retrieval_test_2025.json`、`deliverables/retrieval_test_2025.md`、
`deliverables/stability_check_2025.md`、`src/cninfo.py`、`src/date_repair.py`、
`src/parse_policy.py`、`src/pdfobjects.py`、`tests/`。

### 1.2 完整测试

```bash
python -m unittest discover -s tests -v
```

实际结果：

```text
Ran 188 tests in 10.997s

OK
```

说明：Windows 控制台把部分中文测试说明显示成乱码（GBK 输出），
但测试进程退出码为 0、结果为 `OK`；这不是测试失败。

### 1.3 项目自检

```bash
PYTHONIOENCODING=utf-8 python -X utf8 src/check_setup.py
```

实际结果：**全部通过**。

要点：

- 目录结构齐全；
- `config/companies.csv` 14 条，代码格式/状态合规；
- `config/questions.csv` 10 道，跨公司题 3 道；
- `logs/evaluation.csv` 模板 11 列，已有数据行 0（尚未开展答案评测）；
- `.gitignore` 覆盖 `.env`、原始 PDF、`indexes/*`、`logs/*`；
- 未发现形似真实密钥的字符串；
- `data/raw/reports/` 14 个 PDF，`data/processed/` 14 个 JSONL。

自检中的警告：

```text
[WARN] 已下载年报：14 / 14（骨架阶段应为 0）
```

这是骨架阶段遗留的提示文案：当前项目已进入检索原型阶段，14/14 是预期状态，
不是数据错误，也不是下载失败。

### 1.4 10 题检索评测

```bash
PYTHONIOENCODING=utf-8 python -X utf8 deliverables/retrieval_eval_2025.py
```

实际输出：

```text
已写入 deliverables\retrieval_test_2025.json
已写入 deliverables\retrieval_test_2025.md
页码命中 10/10；严格证据充分 9/10（仅主结果 8/10）；确定性作答可用 6/10；
上下文片段 5 条（语义误挂 0 条；标为可靠 0 条）；跨公司混淆 0；
来源错误题数 0；必需页被阻断 2 题；答案准确率 未评测
```

> “来源错误 0”只检查公司/代码/年份/页码/上下文角色等**元数据一致性**，
> 不判断上一页表头在语义上是否属于下一页表格。上下文语义另见第 3 节。

---

## 2. 数据只读与 .gitignore 检查

实际执行：

```bash
git check-ignore -v indexes/retrieval_v1/documents.jsonl \
  data/processed/000333_2025.jsonl \
  data/raw/reports/000333_2025.pdf \
  logs/retrieval.log
```

结果均命中 `.gitignore`：

| 路径 | 忽略规则 |
|---|---|
| `indexes/retrieval_v1/documents.jsonl` | `.gitignore:26:indexes/*` |
| `data/processed/000333_2025.jsonl` | `.gitignore:28:data/processed/*` |
| `data/raw/reports/000333_2025.pdf` | `.gitignore:19:data/raw/**/*.PDF` |
| `logs/retrieval.log` | `.gitignore:37:logs/*` |

结论：

- 原始 PDF、解析 JSONL、索引、运行日志均未进入 Git 跟踪范围；
- `git status --short` 中没有这些数据文件；
- 本轮没有删除、覆盖或重写原始 PDF/JSONL/异常记录/blocked 标记；
- 临时核对脚本放在项目外的 `%TEMP%`，没有进入仓库。

---

## 3. 一致性检查：README、评测文件与检索报告

### 3.1 已修正的过时表述

| 文件 | 修正前 | 修正后 | 原因 |
|---|---|---|---|
| `README.md` | 确定性作答可用 7/10 | **6/10** | Q04 两表冲突，`deterministic_ok=False` |
| `deliverables/retrieval_improvement_2025.md` | 7/10 | **6/10** | 同上 |
| `deliverables/retrieval_failure_cases_2025.md` | 7/10 | **6/10** | 同上 |
| `deliverables/retrieval_prototype_2025.md` | 7/10 | **6/10** | 同上 |
| 各检索报告 | 上下文 10 条；Q10 4 条、Q07 3 条、Q02 2 条 | **5 条；Q10 3 条、Q07 1 条、Q02 0 条** | 跨页修复拒绝 4 条旧疑似误挂 + 1 条冗余 |
| 各检索报告 | Q10 “海尔 p43→p44 疑似误挂，待核验” | **已拒绝：p43 列头不在页尾、p44 结构不匹配** | 新规则实际执行，不再是仅标注 |
| `retrieval_failure_cases_2025.md` | 无 Q04 冲突案例 | 新增 Q04 案例 | 上一轮发现同一报告两表数值不一致 |

### 3.2 评测文件与报告是否一致

- `deliverables/retrieval_test_2025.json` 与 `.md` 已由评测脚本重新生成；
- JSON 顶层包含 `上下文语义核验`：
  - `保留并待核验` 5 条；
  - `已拒绝关联` 5 条（4 条旧疑似误挂 + 1 条冗余）；
  - `当前保留误挂数` 0；`上下文关联可靠数` 0；`上下文关联未确认数` 5；
- Markdown 汇总的统计数字与 JSON 一致：
  - 页码命中 10/10；
  - 含上下文页码命中 10/10；
  - 严格证据 9/10（仅主结果 8/10）；
  - 确定性 6/10；
  - 上下文 5 条，涉及 3 道题；
  - 上下文语义误挂 0 条；标为可靠 0 条；
  - 口径提示 42 条，涉及 7 道题；
  - 阻断结果 6 条，必需页被阻断 2 题；
  - 跨公司混淆 0；
  - 来源错误 0（仅元数据）；
  - 答案准确率未评测。

### 3.3 仍未解决的口径问题（不是文档笔误）

| 题号 | 问题 | 当前处理 |
|---|---|---|
| Q01 | p9“营业收入”同比 +12.11% 不等于“营业总收入”同比 +12.08% | 答案框架明确区分，使用 p135 计算值 |
| Q02 | 年报只披露“消费电器”，没有“空调”单列 | 固定拒答，不把消费电器写成空调 |
| Q03 | 按主营业务收入/营业总收入两种分母，占比不同 | 答案必须写明分母（51.42% 或 51.12%） |
| Q04 | p12/p13 两表对烹饪电器、食物料理电器给出不同金额 | 确定性改为“否”，并列两套值 |
| Q08 | 美的报表单位千元，格力/海尔为元 | 统一换算后再排序 |
| Q09 | 科沃斯 p20 blocked | 研发强度可答，境外占比对比拒答 |
| Q10 | 境外/海外/国外/出口/外销定义与分母不统一，p20 blocked | 不排名，只展示原始披露 |

---

## 4. 本轮发现的问题

### 4.1 Q04：同一报告两表数值冲突（高）

- 营业收入构成表（p12/p13）：
  - 烹饪电器 `8,711,169,417.63` 元；
  - 食物料理电器 `3,738,068,530.81` 元。
- p13“10%以上”表：
  - 烹饪电器 `8,728,463,967.63` 元；
  - 食物料理电器 `3,720,773,980.81` 元。
- 两项差额均为 `17,294,550.00` 元，方向相反，合计仍为
  `22,771,753,460.04` 元；原始 PDF p13 已复核差异存在，原因未明。
- 影响：Q04 从“确定性可用”改为“否”；答案必须并列两套值并标“待核验”。

### 4.2 跨页表头误挂（高，本轮已修复）

- `attach_cross_page_context()` 曾给海尔 p44 附加 p43 作为“表头来源”；
  p43 的列头位于正文前段（约 28%），p44 为贸易业务/费用/研发内容，
  结构不匹配；
- 根因：旧 `has_table_header()` 接受上一页任意位置的列头行，
  未要求列头接近页尾、也未校验下一页是否有可衔接的数字数据行；
- 修复：`find_cross_page_header()` 要求列头位于正文后 30%，
  `_has_compatible_table_data()` 要求下一页至少两条各含两个数字的数据行；
  同公司/同年/同 PDF 的隔离保持不变；
- 当前结果：p43→p44 为 `表头来源="未找到"`、`关联可靠性="未确认，不附加"`，
  保留警告且不附加上下文；
- 同类旧关联均已拒绝：Q02 000651 p172→p173、p217→p218（下一页已有页内
  拆分列头）、Q07 002959 p80→p81（无页尾列头/无兼容数据行）；
  Q07 p155→p156 为冗余拒绝（列头不在页尾，不影响必需证据）。

**结论：4 条旧疑似误挂已由通用规则拒绝，不是靠硬编码公司/页码/题号。**

### 4.3 上下文“来源错误 0”容易被误读（中）

- 该指标只验证元数据：公司、代码、年份、页码、`context_role`、
  `关联数据页` 是否一致；
- 它**不验证**上一页表头是否语义上属于下一页表格；
- 因此不能把“来源错误 0”写成“上下文全部正确”。
- 本轮在 JSON/Markdown 和 README 中已补充“仅元数据”限定。

### 4.4 Q02/Q09/Q10 的拒答边界（中）

- Q02：题面“空调”，年报只披露“消费电器”，必须拒答；
- Q09：科沃斯 p20 blocked，境外占比不能确定性对比；研发强度可答；
- Q10：定义/分母不统一，禁止排名，只展示原始披露。
- 上述规则已写入 `deliverables/answer_rules_2025.md` 与
  `deliverables/answer_reference_review_2025.md`。

### 4.5 文档过时数字（低，已修正）

- README 和三份检索报告仍写“确定性 7/10”，与重新运行的 6/10 不一致；
- 三份报告仍称 Q10 “4 家跨页表头已补齐”，与语义审计不一致；
- 已在第 3.1 节列出的文件中修正。

### 4.6 未发现的问题

- 未发现测试失败（188/188 OK，其中检索专项 47/47）；
- 未发现密钥或 `.env` 被跟踪；
- 未发现 PDF/JSONL/索引/日志进入 Git；
- 未发现 `blocked=true` 同时 `reliable_for_deterministic_answer=true`
  的矛盾记录；
- 未发现本轮新增依赖、模型下载或外部 API 调用。

---

## 5. 本轮改动文件

### 5.1 新建

| 文件 | 作用 |
|---|---|
| `deliverables/answer_rules_2025.md` | 问答规则草案（引用字段、口径、拒答、计算、模板） |
| `deliverables/answer_reference_review_2025.md` | 10 题参考答案框架 + 人工核验清单 |
| `deliverables/stability_check_2025.md` | 本报告 |
| `deliverables/manual_evidence_review_2025.md` | 跨页上下文与 Q04 原始页面核验记录 |
| `deliverables/evidence_pages_2025/` | 原始 PDF 页面 PNG 与连续页 excerpt |

### 5.2 修改

| 文件 | 改动 |
|---|---|
| `src/retrieve.py` | 跨页表头：页尾位置约束（70%）、下一页两行数字表约束、`未找到` 警告、可靠性字段；拒绝 4 条旧疑似误挂 |
| `tests/test_retrieval.py` | 新增页尾/中部、单行数据、页内完整/拆分列头、跨年/跨 PDF、可靠性、页码/role 回归测试；检索专项 47 项 |
| `deliverables/retrieval_eval_2025.py` | 基线切换为 `before_context_fix`；新增表头来源/可靠性/当前误挂/已拒绝统计；上下文校验强化 |
| `deliverables/retrieval_test_2025.json` | 重新生成；5 条上下文全部待核验；确定性 6/10 |
| `deliverables/retrieval_test_2025.md` | 重新生成；含逐题前后对比、表头来源与可靠性 |
| `README.md` | 更新上下文规则、5 条待核验、4 条旧误挂已拒绝 |
| `deliverables/retrieval_improvement_2025.md` | 记录两轮基线；更新跨页逻辑、逐题状态、测试数与指标 |
| `deliverables/retrieval_failure_cases_2025.md` | Q07/Q10 更新为 1 条/3 条保留，p80/p155/p43 已拒绝 |
| `deliverables/retrieval_prototype_2025.md` | 更新跨页条件、字段说明、指标与验收状态 |
| `deliverables/answer_rules_2025.md` | 更新上下文数量与可靠性规则 |
| `deliverables/answer_reference_review_2025.md` | 更新逐题上下文页码与人工核验清单 |

> 本轮没有修改 `config/questions.csv`，10 道固定题保持原样。
> `src/retrieve.py` 的跨页附加逻辑已修改；所有新关联仍标为“算法判定，待人工核验”。

---

## 6. 建议提交的文件清单（尚未提交）

> 以下只是建议，**本轮没有执行 `git add` / `git commit` / `git push`**。
> 数据文件（PDF、JSONL、索引、日志）继续由 `.gitignore` 排除，不应提交。

### 6.1 代码、配置与测试

```text
.gitattributes
config/companies.csv
requirements.txt
src/check_setup.py
src/companies.py
src/config.py
src/download.py
src/index.py
src/parse.py
src/retrieve.py
src/cninfo.py
src/date_repair.py
src/parse_policy.py
src/pdfobjects.py
tests/
```

### 6.2 文档与评测产物

```text
README.md
deliverables/answer_rules_2025.md
deliverables/answer_reference_review_2025.md
deliverables/stability_check_2025.md
deliverables/retrieval_eval_2025.py
deliverables/retrieval_test_2025.json
deliverables/retrieval_test_2025.md
deliverables/retrieval_prototype_2025.md
deliverables/retrieval_improvement_2025.md
deliverables/retrieval_failure_cases_2025.md
deliverables/q08_p122_diagnosis_2025.md
deliverables/parse_quality_check.py
deliverables/parse_quality_report_2025.json
deliverables/parse_quality_report_2025.md
deliverables/baseline/retrieval_test_2025_pre_context.json
deliverables/baseline/retrieval_test_2025_pre_context.md
deliverables/baseline/retrieval_test_2025_before_context_fix.json
deliverables/baseline/retrieval_test_2025_before_context_fix.md
```

体积提示：

- `retrieval_test_2025.json` 约 800 KB；
- `baseline/retrieval_test_2025_pre_context.json` 约 638 KB；
- `baseline/retrieval_test_2025_before_context_fix.json` 约 818 KB；
- `parse_quality_report_2025.json` 约 416 KB。

如果希望仓库更轻，可只提交 `.md` 汇总，把三个大 JSON 作为本地评测产物；
但那样会削弱“结果可复核”的证据链。建议至少保留
`retrieval_test_2025.json` 与 baseline JSON。

### 6.3 建议的提交拆分

1. **代码/测试**：`src/`、`tests/`、`config/companies.csv`、
   `requirements.txt`、`.gitattributes`；
2. **文档/评测**：`README.md`、`deliverables/` 下的报告、规则、
   参考答案框架、评测脚本与 JSON。

不建议把以下内容加入提交：

```text
data/raw/reports/*.pdf
data/processed/*.jsonl
indexes/*
logs/*
.env
```

---

## 7. 未完成与下一步

### 7.1 本轮明确未完成

- **人工目视核验**：10 题全部“待核验”；
- **原始 PDF 页面检查（助手，不是人工）**：4 条唯一跨页关联与 Q04
  已完成页面渲染和助手视觉检查，见 `manual_evidence_review_2025.md`；
  页面证据支持关联有效，但人工语义确认仍未完成；
- **最终回答生成**：没有 `src/answer.py`，没有实际问答输出；
- **问答准确率**：未评测，不能填写；
- **Streamlit UI**：未开发；
- **Git 提交**：未执行；
- **上下文语义人工核验**：5 条保留关联仍为“算法判定，待人工核验”，
  其中 Q07 p18→p19 直接影响严格证据，需优先核对；
- **Q04 冲突原因**：未确认，只并列两套值。

### 7.2 建议的下一步（按优先级）

1. **人工核验 10 题**：按 `answer_reference_review_2025.md` 的清单逐题
   打开 PDF，先处理 Q01/Q04/Q05/Q06/Q07/Q08，再处理 Q02/Q09/Q10 的拒答边界。
2. **跨页上下文人工核验**：修复已完成（页尾 70% + 两行数字表），
   4 条旧疑似误挂已拒绝；下一步需人工打开 PDF 核对保留的 5 条关联，
   尤其是 Q07 002959 p18→p19。
3. **Q04 冲突确认**：原始页面差异已由助手页面检查确认（见
   `manual_evidence_review_2025.md`），但年报没有披露可解释原因；
   人工确认前保持“原因未明 + 并列两套值 + 待核验”，不得预设
   “重分类”或“笔误”。
4. **Q02 题目决策**：保持“空调”题面并拒答，或经用户同意改为“消费电器”；
   不得擅自改题。
5. **Q09/Q10 p20 人工核对**：确认 `模式间不一致` 的具体位置；
   未确认前 46.47% 只能作候选。
6. **实现 `src/answer.py`**：复用 `answer_rules_2025.md`，先做规则化答案，
   不引入新模型/API；输出必须带页码、口径、blocked 状态和拒答句。
7. **最后再做答案准确率评测与 Streamlit**：准确率必须在真实回答生成后，
   由人工参考答案逐题比对，不能用检索指标代替。

---

## 8. 结论

- 工程稳定性：**通过**（188 tests OK，检索专项 47；check_setup 全通过；
  git diff --check 0）。
- 数据安全：**通过**（原始数据只读；索引/PDF/JSONL/日志均被忽略）。
- 检索证据：页码命中 10/10；严格证据 9/10（仅主结果 8/10）。
- 答案确定性：**6/10**；Q04 因两表冲突降级。
- 上下文语义：保留 5 条，当前保留误挂 0 条；4 条旧疑似误挂 + 1 条冗余
  已由通用规则拒绝；5 条均标“算法判定，待人工核验”，可靠数 0；
  均为助手文本比对，**不是人工目视核验**。
- 最终问答准确率：**未评测**。
- Git：**未提交**。
