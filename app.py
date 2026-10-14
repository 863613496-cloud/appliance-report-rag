"""上市公司财报问答知识库 —— 应用入口（Streamlit）。

当前阶段：骨架。
本文件只做两件事：
    1. 读取 config/companies.csv 与 config/questions.csv，展示项目基础信息；
    2. 明确提示问答功能尚未实现。
不含任何检索、索引或生成逻辑，也不会返回任何「示范答案」。

运行（需先安装可选依赖 streamlit）：
    pip install streamlit
    streamlit run app.py

不装 streamlit 也能做框架自检：
    python src/check_setup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import streamlit as st
except ImportError:
    sys.exit(
        "未安装 streamlit，app.py 无法运行。\n"
        "骨架阶段可以先不安装，改用自检脚本检查框架：\n"
        "    python src/check_setup.py\n"
        "需要看界面时再执行：pip install streamlit"
    )

from src.companies import (  # noqa: E402
    cross_company_questions,
    downloaded_count,
    load_companies,
    load_questions,
)

st.set_page_config(page_title="上市公司财报问答知识库", page_icon="📊", layout="wide")

st.title("上市公司财报问答知识库")
st.caption("研究方向 A · 家电与消费品 · 2025 年年度报告 · 课程作业")

st.warning(
    "**当前为项目骨架阶段**：年报尚未下载，索引与检索尚未构建，问答功能尚未实现。"
    "下方输入框仅为界面占位，不会返回任何答案。"
)

companies = load_companies()
questions = load_questions()
cross = cross_company_questions(questions)
n_downloaded = downloaded_count(companies)

col1, col2, col3, col4 = st.columns(4)
col1.metric("目标公司", f"{len(companies)} 家")
col2.metric("测试题", f"{len(questions)} 道")
col3.metric("跨公司综合题", f"{len(cross)} 道")
col4.metric("已下载年报", f"{n_downloaded} / {len(companies)}")

tab_company, tab_question, tab_chat = st.tabs(["公司名单", "测试题", "问答（未实现）"])

with tab_company:
    st.subheader("研究范围")
    st.dataframe(
        [
            {
                "公司简称": c["公司简称"],
                "证券代码": c["证券代码"],
                "交易所": c["交易所"],
                "板块": c["板块"],
                "报告年份": c["报告年份"],
                "下载状态": c["下载状态"],
                "解析状态": c["解析状态"],
            }
            for c in companies
        ],
        use_container_width=True,
        hide_index=True,
    )
    st.info(
        "数据来源：巨潮资讯网（证监会指定信息披露网站）为主，"
        "上交所、深交所官网为核验来源。必须使用年度报告全文，不得使用摘要。"
    )

with tab_question:
    st.subheader("10 道固定测试题")
    st.dataframe(
        [
            {
                "编号": q["题目编号"],
                "类型": q["题目类型"],
                "题目": q["题目"],
                "涉及公司": q["涉及公司"],
                "跨公司": q["是否跨公司"],
            }
            for q in questions
        ],
        use_container_width=True,
        hide_index=True,
    )
    st.info("评测结果记录在 logs/evaluation.csv（当前仅有表头，尚无测试结果）。")

with tab_chat:
    st.subheader("问答")
    st.text_input("请输入问题", disabled=True, placeholder="功能尚未实现")
    st.button("提问", disabled=True)
    st.error(
        "问答功能尚未实现。请勿在报告中把此界面描述为已完成，"
        "也不要把任何未实测的结果写入交付物。"
    )

st.divider()
st.caption("框架自检：python src/check_setup.py")
