"""pages/chat.py — 💬 Chat ページ。"""
from __future__ import annotations

import streamlit as st

from api_client import _api_post
from utils import _cached_config, _get_table_names, _to_jst


def page_chat() -> None:
    st.header("💬 Chat")

    # Table selector — "すべて" (None) is the default for cross-table search
    table_names = _get_table_names()
    _ALL = "すべて (全テーブル横断)"
    table_options = [_ALL] + table_names
    selected_table_label = st.selectbox(
        "検索対象テーブル", table_options, index=0, key="chat_table",
        help="「すべて」を選ぶと全テーブルを横断して検索します。",
    )
    active_table: str | None = None if selected_table_label == _ALL else selected_table_label

    col_filter, col_date, col_threshold = st.columns([2, 2, 1])
    source_filter = col_filter.selectbox(
        "検索対象", ["all", "article", "llm_answer"],
        format_func=lambda x: {"all": "すべて", "article": "記事のみ", "llm_answer": "保存済み回答のみ"}[x],
    )
    _PRESET_OPTIONS = ["指定なし", "今日", "今週", "今月", "3ヶ月以内", "1年以内", "カスタム"]
    _PRESET_MAP = {"今日": "today", "今週": "week", "今月": "month", "3ヶ月以内": "3months", "1年以内": "year"}
    date_preset_label = col_date.selectbox("期間", _PRESET_OPTIONS, key="chat_date_preset")
    date_filter: dict | None = None
    if date_preset_label in _PRESET_MAP:
        date_filter = {"preset": _PRESET_MAP[date_preset_label]}
    elif date_preset_label == "カスタム":
        c1, c2 = col_date.columns(2)
        df_from = c1.date_input("開始日", key="chat_date_from")
        df_to   = c2.date_input("終了日", key="chat_date_to")
        date_filter = {"date_from": str(df_from), "date_to": str(df_to)}
    cfg_threshold = _cached_config().get("search_distance_threshold", 0.2)
    distance_threshold = col_threshold.number_input(
        "距離閾値", min_value=0.0, max_value=2.0,
        value=float(cfg_threshold), step=0.05,
        help="小さいほど厳密。0に近いほど完全一致のみヒット。2.0で全件ヒット。",
    )

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    if "last_answer" not in st.session_state:
        st.session_state.last_answer = None
    if "last_question" not in st.session_state:
        st.session_state.last_question = None

    # Display history
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    question = st.chat_input("質問を入力してください")
    if question:
        st.session_state.chat_history.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("検索・回答生成中..."):
                try:
                    body: dict = {
                        "question": question,
                        "source_type": source_filter,
                        "distance_threshold": distance_threshold,
                        "expand_queries": True,
                    }
                    if active_table is not None:
                        body["table"] = active_table
                    if date_filter:
                        body["date_filter"] = date_filter
                    res = _api_post("/api/ask", body)
                    answer = res["answer"]
                    sources = res.get("sources", [])
                except Exception as e:
                    answer = f"エラー: {e}"
                    sources = []

            st.markdown(answer)

            if sources:
                with st.expander(f"参照した情報源 ({len(sources)}件)"):
                    for s in sources:
                        cols = st.columns([3, 1, 1, 1])
                        title_label = f"**{s['title']}**"
                        if tbl := s.get("_table_name"):
                            title_label += f"  `{tbl}`"
                        cols[0].markdown(title_label)
                        cols[1].markdown(f"参照回数: {s['use_count']}")
                        cols[2].markdown(f"最終利用: {_to_jst(s.get('last_used_at', ''))}")
                        dist = s.get("_distance")
                        cols[3].markdown(f"距離: {dist:.4f}" if dist is not None else "距離: —")
                        if s.get("source_url"):
                            st.markdown(f"🔗 {s['source_url']}")
                        if s.get("tags"):
                            st.markdown(" ".join(f"`{t}`" for t in s["tags"]))

            st.session_state.chat_history.append({"role": "assistant", "content": answer})
            st.session_state.last_answer = answer
            st.session_state.last_question = question

    # Save last answer
    if st.session_state.last_answer:
        save_table = active_table if active_table is not None else _get_table_names()[0]
        if st.button("💾 この回答を知識ベースに保存"):
            with st.spinner("保存中..."):
                try:
                    _api_post("/api/save-answer", {
                        "question": st.session_state.last_question,
                        "answer": st.session_state.last_answer,
                        "table": save_table,
                    })
                    st.success("保存しました")
                except Exception as e:
                    st.error(f"保存失敗: {e}")
