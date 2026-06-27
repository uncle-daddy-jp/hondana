"""pages/knowledge.py — 📚 Knowledge ページ。"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from api_client import _api_delete, _api_get, _api_post
from utils import _cached_articles, _cached_stats, _get_table_names, _public_api_url, _to_jst


def _chunk_tree_build(chunks: list[dict]) -> dict:
    """Build L1 → L2 → L3 tree from flat chunk list using level and parent_id."""
    l1 = None
    l2_map: dict[str, dict] = {}  # id → {chunk, children}

    for c in chunks:
        if c["level"] == 1:
            l1 = c
        elif c["level"] == 2:
            l2_map[c["id"]] = {"chunk": c, "children": []}

    for c in chunks:
        if c["level"] == 3:
            pid = c.get("parent_id", "")
            if pid not in l2_map:
                l2_map[pid] = {"chunk": None, "children": []}
            l2_map[pid]["children"].append(c)

    return {"l1": l1, "l2_list": list(l2_map.values())}


def _chunk_tree_render(chunks: list[dict], active_table: str = "hondana_chunks") -> None:
    """Render L2 sections in Streamlit with usage stats and delete button (L1/L3 excluded)."""
    tree = _chunk_tree_build(chunks)

    for entry in tree["l2_list"]:
        l2 = entry["chunk"]
        if not l2:
            continue
        use_count = l2.get("use_count", 0)
        last_used = _to_jst(l2.get("last_used_at", "")) if l2.get("last_used_at") else "未使用"
        label = f"🟡 {l2['heading']} | 参照 {use_count}回 | {last_used}"
        with st.expander(label):
            st.markdown(l2["text"])
            if st.button("🗑️ このセクションを削除", key=f"del_sec_{l2['id']}"):
                _api_delete(f"/api/sections/{l2['id']}?table={active_table}")
                _cached_articles.clear()
                _cached_stats.clear()
                st.success("削除しました")
                st.rerun()


def page_knowledge() -> None:
    st.header("📚 ナレッジ管理")

    table_names = _get_table_names()
    _ALL = "すべて (全テーブル)"
    table_options = [_ALL] + table_names
    selected = st.selectbox("対象テーブル", table_options, key="knowledge_table")
    all_tables_mode = selected == _ALL
    active_table = None if all_tables_mode else selected

    # Stats
    try:
        if all_tables_mode:
            total_arts = total_chunks = answer_chunks = 0
            for tname in table_names:
                s = _cached_stats(tname)
                total_arts    += s.get("total_articles", 0)
                total_chunks  += s.get("total_chunks", 0)
                answer_chunks += s.get("answer_count", 0)
        else:
            s = _cached_stats(active_table)
            total_arts, total_chunks, answer_chunks = (
                s["total_articles"], s["total_chunks"], s["answer_count"]
            )
        cols = st.columns(3)
        cols[0].metric("総記事数", total_arts)
        cols[1].metric("総チャンク数", total_chunks)
        cols[2].metric("保存済み回答", answer_chunks)
    except Exception:
        pass

    tab_list, tab_bulk = st.tabs(["一覧・手動削除", "条件一括削除"])

    _KM_PAGE_SIZE = 20

    # ── Tab: List ──
    with tab_list:
        with st.form("km_kw_form", clear_on_submit=False):
            kw_query = st.text_input("🔍 キーワード検索", placeholder="単語やフレーズを入力...", key="km_kw")
            kw_submitted = st.form_submit_button("検索")

        if kw_submitted and kw_query.strip():
            # ── Keyword search mode ──
            try:
                res = _api_post("/api/keyword-search", {
                    "query": kw_query.strip(),
                    "top_k": 30,
                    "table": active_table,
                })
                kw_chunks = res.get("chunks", [])
            except Exception as e:
                st.error(f"キーワード検索失敗: {e}")
                kw_chunks = []

            st.caption(f"キーワード検索結果: {len(kw_chunks)} 件")
            if not kw_chunks:
                st.info("該当するチャンクが見つかりませんでした")
            else:
                for chunk in kw_chunks:
                    level = chunk.get("level", "")
                    heading = chunk.get("heading", "")
                    title = chunk.get("title", "")
                    text_preview = (chunk.get("text") or "")[:200]
                    tname = chunk.get("_table_name", "")
                    table_badge = f"  `{tname}`" if all_tables_mode else ""
                    label = f"[L{level}] **{title}** › {heading}{table_badge}"
                    with st.expander(label):
                        st.markdown(f"🔗 {chunk.get('source_url') or '—'}")
                        st.text(text_preview + ("..." if len(chunk.get("text") or "") > 200 else ""))
            return

        source_filter = st.selectbox(
            "表示対象", ["all", "article", "llm_answer"],
            format_func=lambda x: {"all": "選択無し（両方）", "article": "記事から", "llm_answer": "LLM回答から"}[x],
            key="km_filter",
        )

        # Filter conditions
        st.markdown("**絞り込み条件**（0 = 条件なし）")
        col_f1, col_f2, col_f3, col_f4 = st.columns(4)
        f_use_min   = col_f1.number_input("参照回数 X回以上", min_value=0, value=0, step=1, key="km_f_use_min")
        f_use_max   = col_f2.number_input("参照回数 X回以下", min_value=0, value=0, step=1, key="km_f_use_max", help="0 = 上限なし")
        f_last_days = col_f3.number_input("最終利用 X日以上前", min_value=0, value=0, step=1, key="km_f_last")
        f_rec_days  = col_f4.number_input("登録 X日以上前", min_value=0, value=0, step=1, key="km_f_rec")

        try:
            if all_tables_mode:
                articles = []
                for tname in table_names:
                    for art in _cached_articles(source_filter, tname):
                        art["_table_name"] = tname
                        articles.append(art)
                articles.sort(key=lambda a: a.get("recorded_at", ""), reverse=True)
            else:
                articles = []
                for art in _cached_articles(source_filter, active_table):
                    art["_table_name"] = active_table
                    articles.append(art)
        except Exception as e:
            st.error(f"取得失敗: {e}")
            return

        if not articles:
            st.info("登録されているナレッジはありません")
            return

        # Apply filters client-side
        now = datetime.now(timezone.utc)

        def _km_match(art: dict) -> bool:
            uc = art.get("use_count", 0)
            if f_use_min > 0 and uc < f_use_min:
                return False
            if f_use_max > 0 and uc > f_use_max:
                return False
            if f_last_days > 0:
                lu = art.get("last_used_at", "")
                if lu:
                    try:
                        if (now - datetime.fromisoformat(lu)).days < f_last_days:
                            return False
                    except ValueError:
                        pass
            if f_rec_days > 0:
                ra = art.get("recorded_at", "")
                if ra:
                    try:
                        if (now - datetime.fromisoformat(ra)).days < f_rec_days:
                            return False
                    except ValueError:
                        pass
            return True

        filtered = [a for a in articles if _km_match(a)]

        # Pagination — reset when filter changes
        filter_key = f"{selected}|{source_filter}|{f_use_min}|{f_use_max}|{f_last_days}|{f_rec_days}"
        if st.session_state.get("km_filter_key") != filter_key:
            st.session_state.km_filter_key = filter_key
            st.session_state.km_page = 1

        page_count = st.session_state.get("km_page", 1)
        shown = filtered[: page_count * _KM_PAGE_SIZE]

        st.caption(f"表示: {len(shown)} / {len(filtered)} 件（全{len(articles)}件中）")

        for art in shown:
            art_table = art["_table_name"]
            table_badge = f"  `{art_table}`" if all_tables_mode else ""
            label = f"📄 {art['title']}{table_badge} | 参照 {art['use_count']}回 | {_to_jst(art.get('recorded_at', ''), 'long')}"
            with st.expander(label):
                cols = st.columns([2, 1, 1, 1])
                _permalink = f"{_public_api_url()}/k/{art['article_id']}"
                _src = art.get('source_url') or ''
                cols[0].markdown(
                    f"🔗 [{_src[:60]}]({_src})" if _src else "🔗 —"
                )
                cols[0].markdown(f"📌 [パーマリンク]({_permalink})")
                cols[1].markdown(f"最終利用: {_to_jst(art.get('last_used_at', '')) if art.get('last_used_at') else '未使用'}")
                cols[2].markdown(f"タグ: {', '.join(art.get('tags') or []) or '—'}")

                if cols[3].button("🗑️ 削除", key=f"del_{art['article_id']}"):
                    _api_delete(f"/api/articles/{art['article_id']}?table={art_table}")
                    _cached_articles.clear()
                    _cached_stats.clear()
                    st.success("削除しました")
                    st.rerun()

                st.info(f"🔵 **サマリー**\n\n{art.get('text', '')}")

                if st.toggle("セクションを表示", key=f"toggle_{art['article_id']}"):
                    chunks = _api_get(f"/api/articles/{art['article_id']}?table={art_table}")["chunks"]
                    _chunk_tree_render(chunks, art_table)

        # Load more button
        remaining = len(filtered) - len(shown)
        if remaining > 0:
            next_batch = min(_KM_PAGE_SIZE, remaining)
            if st.button(f"さらに {next_batch} 件を表示（残り {remaining} 件）"):
                st.session_state.km_page = page_count + 1
                st.rerun()

    # ── Tab: Bulk Delete ──
    with tab_bulk:
        if all_tables_mode:
            st.info("条件一括削除は特定のテーブルを選択してから使用してください。")
            return

        st.markdown("条件を指定してまとめて削除します。削除前にプレビューで確認できます。")

        col1, col2, col3 = st.columns(3)
        last_used_days = col1.number_input("最終利用から X 日以上経過", min_value=0, value=0, step=1)
        use_count_max  = col2.number_input("参照回数が X 回以下", min_value=0, value=0, step=1)
        recorded_days  = col3.number_input("登録から X 日以上経過", min_value=0, value=0, step=1)
        bulk_source    = st.selectbox("対象", ["all", "article", "llm_answer"], key="bulk_src")

        conditions: dict = {"source_type": bulk_source}
        if last_used_days > 0:
            conditions["last_used_days_gte"] = last_used_days
        if use_count_max > 0:
            conditions["use_count_lte"] = use_count_max
        if recorded_days > 0:
            conditions["recorded_days_gte"] = recorded_days

        if st.button("🔍 プレビュー"):
            with st.spinner("検索中..."):
                res = _api_post("/api/articles/bulk-delete-preview", {**conditions, "table": active_table})
                targets = res["targets"]
                st.warning(f"{len(targets)} セクションが削除対象です")
                for t in targets:
                    st.markdown(f"- **{t['title']}** › {t.get('heading', '')} (参照 {t['use_count']}回, 登録 {_to_jst(t.get('recorded_at', ''), 'long')})")

            if targets and st.button("🗑️ 一括削除を実行", type="primary"):
                res = _api_post("/api/articles/bulk-delete", {**conditions, "table": active_table})
                st.success(f"{res['deleted_rows']} チャンクを削除しました")
                st.rerun()
