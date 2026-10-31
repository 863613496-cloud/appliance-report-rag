"""上市公司财报问答知识库 —— Streamlit 演示界面。

本文件只做界面与展示：
* 固定题从 ``config/questions.csv`` 读取，不另行编造；
* 查询直接调用 ``src.retrieve.retrieve`` 和
  ``src.answer.answer_from_results``，不复制检索或答案规则；
* 不调用任何外部大模型 API。

运行：
    streamlit run app.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import streamlit as st
except ImportError:
    sys.exit(
        "未安装 streamlit，界面无法运行。\n"
        "请先执行：pip install -r requirements.txt"
    )

from src.answer import answer_from_results  # noqa: E402
from src.companies import load_companies, load_questions  # noqa: E402
from src.config import INDEX_DIR  # noqa: E402
from src.retrieve import retrieve  # noqa: E402

STATUS_ANSWERABLE = "可回答"
STATUS_NEEDS_DEFINITION = "需口径说明"
STATUS_NEEDS_VERIFICATION = "需核验"
STATUS_CONFLICT = "证据冲突"
STATUS_INSUFFICIENT = "证据不足"
STATUS_BLOCKED = "已阻断"


def run_query(
    question: str,
    question_id: str = "",
    top_k: int = 8,
) -> tuple[list[dict], dict]:
    """只做模块编排：检索一次，把同一批结果交给答案模块。"""
    results = retrieve(question, top_k=top_k)
    answer = answer_from_results(question, results, question_id=question_id)
    return results, answer


def _short(text: str, limit: int = 36) -> str:
    compact = " ".join((text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _page_label(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        pages = sorted({int(page) for page in value if page is not None})
        return "、".join(f"p{page}" for page in pages) if pages else "—"
    if value is None or value == "":
        return "—"
    return f"p{value}"


def _score_text(value: object) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _risk_label(metric: dict) -> str:
    labels: list[str] = []
    if metric.get("blocked"):
        labels.append("blocked")
    if metric.get("reliable_for_deterministic_answer") is False:
        labels.append("不可靠")
    if metric.get("表头核验状态") == "待人工核验":
        labels.append("表头待核验")
    if not metric.get("是否采信"):
        labels.append("未采信")
    return "；".join(labels) if labels else "—"


def _show_status(status: str) -> None:
    if status == STATUS_ANSWERABLE:
        st.success("回答状态：可回答。该结论由规则判定，仍需人工对照 PDF 核验。")
    elif status == STATUS_NEEDS_DEFINITION:
        st.warning(
            "回答状态：需口径说明。数值存在口径差异，请按题面选择分母或指标名，"
            "界面不自动替换或换算。"
        )
    elif status == STATUS_NEEDS_VERIFICATION:
        st.warning(
            "回答状态：需核验。结论依赖尚未人工确认的跨页表头，"
            "相关数值只能作为候选，不能当作已确认事实。"
        )
    elif status == STATUS_CONFLICT:
        st.error(
            "回答状态：证据冲突。同一报告内出现不一致数值，"
            "界面只并列展示，不选择其中一组作为正确值。"
        )
    elif status == STATUS_INSUFFICIENT:
        st.error(
            "回答状态：证据不足。现有检索结果不足以支持确定性回答，"
            "不得把候选数值包装成结论。"
        )
    elif status == STATUS_BLOCKED:
        st.error(
            "回答状态：已阻断。关键证据被 blocked 或标记为不可靠，"
            "相关数值只能作为候选。"
        )
    else:
        st.info(f"回答状态：{status}")


def _render_metadata(answer: dict) -> None:
    companies = answer.get("公司") or []
    codes = answer.get("证券代码") or []
    year = answer.get("报告年份") or "—"
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("**公司**")
        st.write("、".join(companies) if companies else "—")
    with col2:
        st.markdown("**证券代码**")
        st.write("、".join(codes) if codes else "—")
    with col3:
        st.markdown("**报告年份**")
        st.write(str(year))


def _render_risks(answer: dict) -> None:
    metrics = answer.get("指标") or []
    warnings = answer.get("警告") or []
    refusals = answer.get("拒答原因") or []
    notes = answer.get("口径说明") or []
    pending = [
        metric
        for metric in metrics
        if metric.get("表头核验状态") == "待人工核验"
    ]
    blocked = [metric for metric in metrics if metric.get("blocked")]
    unadopted = [metric for metric in metrics if not metric.get("是否采信")]
    if not (warnings or refusals or notes or pending or blocked or unadopted):
        return

    st.markdown("#### 风险提示")
    col1, col2, col3 = st.columns(3)
    col1.metric("未采信指标", len(unadopted))
    col2.metric("blocked 指标", len(blocked))
    col3.metric("表头待核验", len(pending))

    for warning in warnings:
        st.warning(warning)
    for reason in refusals:
        st.error("拒答原因：" + reason)

    if pending:
        preview = "；".join(
            f"{metric.get('公司简称')} · {metric.get('指标原文')} "
            f"p{metric.get('数据页码')}→表头{_page_label(metric.get('表头来源页码'))}"
            for metric in pending[:8]
        )
        if len(pending) > 8:
            preview += "；…"
        st.caption("跨页表头待人工核验：" + preview)
    if blocked:
        preview = "；".join(
            f"{metric.get('公司简称')} · {metric.get('指标原文')} "
            f"p{metric.get('数据页码')}"
            for metric in blocked[:8]
        )
        if len(blocked) > 8:
            preview += "；…"
        st.caption("blocked 证据（未采信）：" + preview)

    if notes:
        with st.expander(
            "口径说明",
            expanded=answer.get("回答状态") == STATUS_NEEDS_DEFINITION,
        ):
            for note in notes:
                st.markdown(f"- {note}")


def _render_conflict_detail(answer: dict) -> None:
    audit = answer.get("审计") or {}
    comparisons = audit.get("逐项比较")
    if not comparisons:
        return

    st.markdown("#### 冲突明细（不选择正确值）")
    table_a = audit.get("表A") or {}
    table_b = audit.get("表B") or {}
    st.caption(
        "表A："
        f"{table_a.get('表名', '—')}（{_page_label(table_a.get('页码'))}）"
        " ｜ 表B："
        f"{table_b.get('表名', '—')}（{_page_label(table_b.get('页码'))}）"
    )
    rows = [
        {
            "指标": item.get("指标", ""),
            "表A": item.get("表A", ""),
            "表B": item.get("表B", ""),
            "差额（B-A）": item.get("差额", ""),
            "是否不同": "是" if item.get("是否不同") else "否",
        }
        for item in comparisons
    ]
    st.dataframe(rows, width="stretch", hide_index=True)
    st.error(
        f"原因：{audit.get('原因', '差异已发现、原因未明')}；"
        f"合计差额：{audit.get('合计差额', '—')}。"
        "界面不选择任何一组作为正确值。"
    )


def _render_evidence(answer: dict) -> None:
    metrics = answer.get("指标") or []
    st.markdown("#### 引用证据")
    st.caption(
        "页码为项目解析约定下的 PDF 页序号；“采信=否”的数值只能作为候选，"
        "不能当作已确认事实。"
    )
    if not metrics:
        st.info("本题没有可列出的指标记录。")
        return

    rows = [
        {
            "公司": metric.get("公司简称", ""),
            "代码": metric.get("证券代码", ""),
            "指标原文": metric.get("指标原文", ""),
            "数值": metric.get("数值", ""),
            "单位": metric.get("单位", ""),
            "数据页": metric.get("数据页码"),
            "表头页": metric.get("表头来源页码"),
            "表名": metric.get("表名", ""),
            "blocked": "是" if metric.get("blocked") else "否",
            "采信": "是" if metric.get("是否采信") else "否",
            "表头核验": metric.get("表头核验状态", ""),
            "风险": _risk_label(metric),
        }
        for metric in metrics
    ]
    st.dataframe(rows, width="stretch", hide_index=True)

    st.markdown("##### 逐条证据原文")
    for metric in metrics:
        flagged = (
            metric.get("blocked")
            or not metric.get("是否采信")
            or metric.get("表头核验状态") == "待人工核验"
        )
        prefix = "⚠ " if flagged else ""
        title = (
            f"{prefix}{metric.get('公司简称', '')} · "
            f"{metric.get('指标原文', '')} · "
            f"{metric.get('数值', '')} {metric.get('单位', '')} · "
            f"p{metric.get('数据页码', '—')}"
        )
        with st.expander(title):
            col1, col2, col3 = st.columns(3)
            col1.markdown(f"**表名**：{metric.get('表名') or '—'}")
            col2.markdown(
                f"**表头来源页**：{_page_label(metric.get('表头来源页码'))}"
            )
            col3.markdown(f"**来源类型**：{metric.get('来源类型') or '—'}")
            st.text(metric.get("证据原文") or "（无原文）")
            if metric.get("口径说明"):
                st.caption("口径说明：" + str(metric["口径说明"]))
            if metric.get("计算依据"):
                st.caption("计算依据：" + str(metric["计算依据"]))
            st.caption(
                f"blocked={metric.get('blocked')} · "
                f"采信={metric.get('是否采信')} · "
                f"表头核验={metric.get('表头核验状态')} · "
                f"doc_id={metric.get('doc_id') or '—'}"
            )


def _render_audit(answer: dict) -> None:
    audit = answer.get("审计") or {}
    if not audit:
        return
    with st.expander("查看完整审计记录（JSON）", expanded=False):
        st.json(audit, expanded=False)


def _render_answer(answer: dict) -> None:
    status = answer.get("回答状态", "")
    st.markdown(f"### 回答状态：{status}")
    _show_status(status)
    st.caption(
        f"确定性作答：{'是' if answer.get('确定性作答') else '否'} · "
        f"人工核验状态：{answer.get('人工核验状态', '待核验')} · "
        "答案准确率：未评测"
    )
    _render_metadata(answer)

    st.markdown("#### 答案正文")
    if not answer.get("确定性作答"):
        st.warning(
            "本答案不是确定性结论。下方数值可能是候选、未采信或冲突证据；"
            "不得把它们包装成已确认事实。"
        )
    st.markdown(answer.get("答案正文") or "（无）")

    _render_risks(answer)
    _render_conflict_detail(answer)
    _render_evidence(answer)
    _render_audit(answer)


def _render_results(results: list[dict]) -> None:
    st.markdown("#### 检索片段（答案依据）")
    st.caption(
        "以下为本次答案实际使用的检索结果；排序是现有混合检索结果，不是答案。"
        "blocked 或不可靠片段不能作为已确认事实。"
    )
    if not results:
        st.info("本次没有检索结果。")
        return

    overview = []
    for result in results:
        scores = result.get("scores") or {}
        reliable = bool(result.get("reliable_for_deterministic_answer", True))
        overview.append(
            {
                "排名": result.get("排名"),
                "公司": result.get("公司简称", ""),
                "代码": result.get("证券代码", ""),
                "页码": result.get("页码"),
                "类型": result.get("类型", ""),
                "章节": result.get("章节") or "—",
                "blocked": "是" if result.get("blocked") else "否",
                "可作确定性证据": "是" if reliable and not result.get("blocked") else "否",
                "RRF": _score_text(scores.get("rrf")),
            }
        )
    st.dataframe(overview, width="stretch", hide_index=True)

    for result in results:
        scores = result.get("scores") or {}
        title = (
            f"#{result.get('排名')} {result.get('公司简称', '')} "
            f"{result.get('证券代码', '')} · p{result.get('页码')} · "
            f"{result.get('类型', '')} · {result.get('章节') or '无章节'}"
        )
        with st.expander(title):
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("RRF", _score_text(scores.get("rrf")))
            col2.metric("BM25", _score_text(scores.get("bm25")))
            col3.metric("向量", _score_text(scores.get("vector")))
            col4.metric("意图", _score_text(scores.get("intent")))
            st.caption(
                f"PDF：{result.get('PDF文件名', '—')} · "
                f"类型：{result.get('类型', '—')} · "
                f"字符数：{result.get('字符数', '—')}"
            )
            if result.get("blocked"):
                st.error("blocked=true：该片段不得作为已确认事实。")
            if not result.get("reliable_for_deterministic_answer", True):
                st.warning(
                    "reliable_for_deterministic_answer=false："
                    "该片段不能直接用于确定性作答。"
                )
            for warning in result.get("warnings") or []:
                st.warning(warning)
            for anomaly in result.get("blocking_anomalies") or []:
                st.error(f"阻断异常：{anomaly}")
            if result.get("需复核"):
                st.caption("需复核：" + "、".join(result["需复核"]))
            if result.get("口径提示"):
                st.caption("口径提示：" + "；".join(result["口径提示"]))
            st.text(result.get("正文") or "（无正文）")

            contexts = result.get("上下文片段") or []
            if contexts:
                st.markdown("**跨页上下文片段**")
                for context in contexts:
                    st.warning(
                        f"p{context.get('页码')} · "
                        f"{context.get('上下文关联状态', '算法判定，待人工核验')} · "
                        f"blocked={context.get('blocked')} · "
                        "仅供参考，未采信"
                    )
                    st.text(context.get("正文") or "（无正文）")


def _render_intro(companies: list[dict], questions: list[dict]) -> None:
    st.subheader("使用说明")
    st.markdown(
        "1. 在左侧选择 10 道固定测试题，或直接编辑问题文本。\n"
        "2. 点击“查询”，界面会调用现有检索模块和确定性答案模块。\n"
        "3. 查看回答状态、引用证据、风险提示和检索片段。"
    )
    st.info(
        f"当前资料：{len(companies)} 家公司、{len(questions)} 道固定题。"
        "固定题直接读取 `config/questions.csv`，不另行编造。"
    )
    st.markdown("#### 10 道固定测试题")
    st.dataframe(
        [
            {
                "编号": question["题目编号"],
                "类型": question["题目类型"],
                "题目": question["题目"],
                "涉及公司": question["涉及公司"],
                "跨公司": question["是否跨公司"],
            }
            for question in questions
        ],
        width="stretch",
        hide_index=True,
    )
    st.warning(
        "答案由确定性规则生成，尚未经人工逐题核验；"
        "“可回答”只是规则判定，不是答案准确率。"
    )


def main() -> None:
    st.set_page_config(
        page_title="上市公司财报问答知识库",
        page_icon="📊",
        layout="wide",
    )

    companies = load_companies()
    questions = load_questions()
    question_map = {row["题目编号"]: row["题目"] for row in questions}
    if not questions:
        st.error("未读取到固定测试题，请检查 config/questions.csv。")
        return

    if "selected_qid" not in st.session_state:
        st.session_state["selected_qid"] = questions[0]["题目编号"]
    if "question_text" not in st.session_state:
        st.session_state["question_text"] = question_map.get(
            st.session_state["selected_qid"],
            questions[0]["题目"],
        )

    def _sync_question() -> None:
        selected = st.session_state.get("selected_qid")
        if selected in question_map:
            st.session_state["question_text"] = question_map[selected]

    with st.sidebar:
        st.header("提问")
        st.caption("固定题读取自 `config/questions.csv`")
        st.selectbox(
            "选择固定测试题",
            options=list(question_map),
            key="selected_qid",
            on_change=_sync_question,
            format_func=lambda qid: f"{qid} · {_short(question_map[qid], 32)}",
        )
        st.text_area(
            "问题（可直接编辑）",
            key="question_text",
            height=150,
        )
        top_k = st.slider(
            "检索片段数 top_k",
            min_value=3,
            max_value=15,
            value=8,
            step=1,
        )
        submitted = st.button(
            "查询",
            type="primary",
            width="stretch",
        )
        st.divider()
        st.caption(
            f"{len(companies)} 家公司 · {len(questions)} 道固定题 · "
            "本地 BM25 + 字符 n-gram TF-IDF"
        )
        st.caption("未调用大模型 API；答案仍需人工核验。")
        if not INDEX_DIR.exists():
            st.warning("未找到检索索引，请先运行 `python -m src.index`。")

    st.title("上市公司财报问答知识库")
    st.caption("2025 年年度报告 · 本地检索 + 确定性规则答案 · 未调用大模型")

    if submitted:
        question = (st.session_state.get("question_text") or "").strip()
        if not question:
            st.session_state["last_error"] = "请输入问题后再查询。"
            st.session_state["last_traceback"] = ""
        else:
            selected = st.session_state.get("selected_qid", "")
            question_id = (
                selected
                if question == (question_map.get(selected, "").strip())
                else ""
            )
            try:
                with st.spinner("正在检索年报并生成规则答案…"):
                    results, answer = run_query(
                        question,
                        question_id=question_id,
                        top_k=top_k,
                    )
            except Exception as exc:  # noqa: BLE001 - 界面上要显示可诊断的错误
                st.session_state["last_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                st.session_state["last_traceback"] = traceback.format_exc()
            else:
                st.session_state["last_error"] = ""
                st.session_state["last_traceback"] = ""
                st.session_state["last_run"] = {
                    "question": question,
                    "question_id": question_id,
                    "top_k": top_k,
                    "results": results,
                    "answer": answer,
                }

    error = st.session_state.get("last_error")
    if error:
        st.error("查询失败：" + error)
        trace = st.session_state.get("last_traceback")
        if trace:
            with st.expander("错误详情"):
                st.code(trace)
        return

    last_run = st.session_state.get("last_run")
    if not last_run:
        _render_intro(companies, questions)
        return

    st.markdown(f"**当前问题**：{last_run['question']}")
    source = (
        f"固定题 {last_run['question_id']}"
        if last_run.get("question_id")
        else "自定义问题"
    )
    st.caption(
        f"题目来源：{source} · top_k={last_run.get('top_k', '—')} · "
        "答案由本地规则生成，未调用大模型"
    )
    _render_answer(last_run["answer"])
    st.divider()
    _render_results(last_run["results"])


if __name__ == "__main__":
    main()
