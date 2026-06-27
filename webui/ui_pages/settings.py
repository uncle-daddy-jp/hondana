"""pages/settings.py — ⚙️ Settings ページ。"""
from __future__ import annotations

import streamlit as st

from api_client import _api_delete, _api_patch, _api_post
from utils import _cached_articles, _cached_config, _cached_keys, _cached_stats, _cached_tables


def page_settings() -> None:
    st.header("⚙️ 設定")

    cfg = _cached_config()

    with st.form("settings_form"):
        st.subheader("基本")
        instance_name = st.text_input(
            "インスタンス名",
            value=cfg.get("instance_name", ""),
            placeholder="例: 自宅サーバー / 会社用",
            help="WebUI のサイドバーに表示される名前。複数の HONDANA を区別するときに便利。",
        )

        st.subheader("ディレクトリ")
        inbox = st.text_input("投入口 (inbox_dir)", value=cfg.get("inbox_dir", "/inbox"))
        done  = st.text_input("完了先 (done_dir)",  value=cfg.get("done_dir", "/done"))

        st.subheader("LLM: 要約・クエリ展開")
        _SUM_PROVIDERS = ["claude", "groq", "openai_compat"]
        _sum_prov = cfg.get("llm_summary_provider", "claude")
        provider = st.selectbox("プロバイダー", _SUM_PROVIDERS,
                                index=_SUM_PROVIDERS.index(_sum_prov) if _sum_prov in _SUM_PROVIDERS else 0)
        summary_model = st.text_input("モデル名", value=cfg.get("llm_summary_model", "claude-haiku-4-5-20251001"))

        st.subheader("LLM: 回答生成")
        answer_model = st.text_input("モデル名 (Claude固定)", value=cfg.get("llm_answer_model", "claude-sonnet-4-6"))

        st.subheader("検索設定")
        distance_threshold_cfg = st.number_input(
            "距離閾値 (search_distance_threshold)",
            min_value=0.0, max_value=2.0,
            value=float(cfg.get("search_distance_threshold", 0.2)), step=0.05,
            help="小さいほど厳密。チャットページでも一時的に上書き可能。",
        )

        st.subheader("チャンク設定")
        l3_max   = st.number_input("L3 最大文字数", value=cfg.get("chunk_l3_max_chars", 500), step=50)
        l3_overlap = st.number_input("L3 オーバーラップ文字数", value=cfg.get("chunk_l3_overlap_chars", 100), step=10)
        l1_chars = st.number_input("L1 サマリー文字数", value=cfg.get("chunk_l1_summary_chars", 200), step=50)
        l1_tags  = st.number_input("タグ最大数", value=cfg.get("chunk_l1_tag_count", 8), step=1)

        if st.form_submit_button("保存", type="primary"):
            new_cfg = {
                "instance_name": instance_name.strip(),
                "inbox_dir": inbox,
                "done_dir": done,
                "llm_summary_provider": provider,
                "llm_summary_model": summary_model,
                "llm_answer_model": answer_model,
                "search_distance_threshold": float(distance_threshold_cfg),
                "chunk_l3_max_chars": int(l3_max),
                "chunk_l3_overlap_chars": int(l3_overlap),
                "chunk_l1_summary_chars": int(l1_chars),
                "chunk_l1_tag_count": int(l1_tags),
            }
            try:
                _api_post("/api/config", new_cfg)
                _cached_config.clear()
                st.success("設定を保存しました")
            except Exception as e:
                st.error(f"保存失敗: {e}")

    # ── API Key management ────────────────────────────────────────────────────
    st.subheader("🔑 APIキー管理")
    st.caption("X-API-Key ヘッダーで認証します。キーが未設定の場合は認証なし（オープンアクセス）。")

    api_keys = _cached_keys()

    if api_keys:
        for entry in api_keys:
            col_name, col_key, col_del = st.columns([2, 4, 1])
            col_name.text(entry.get("name", ""))
            col_key.code(entry.get("key", ""))
            if col_del.button("削除", key=f"del_key_{entry.get('name')}"):
                try:
                    _api_delete(f"/api/keys/{entry['name']}")
                    _cached_keys.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"削除失敗: {e}")
    else:
        st.info("APIキーが設定されていません（認証なし）")

    with st.form("create_key_form", clear_on_submit=True):
        new_key_name = st.text_input("新しいキーの名前", placeholder="例: claude-code")
        if st.form_submit_button("キーを生成"):
            if new_key_name.strip():
                try:
                    result = _api_post("/api/keys", {"name": new_key_name.strip()})
                    st.success(f"生成されました: `{result['key']}`")
                    _cached_keys.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"生成失敗: {e}")
            else:
                st.warning("名前を入力してください")

    st.divider()
    _settings_tables()


def _settings_tables() -> None:
    st.subheader("📂 テーブル管理")
    st.caption(
        "テーブルはナレッジの分類単位です。"
        "説明文を設定すると、MCP 経由のエージェントがどのテーブルを検索対象にするか判断できるようになります。"
    )

    tables = _cached_tables()
    if not tables:
        st.info("テーブルがありません")
        return

    for tbl in tables:
        name = tbl["name"]
        is_default = name == "hondana_chunks"

        header_cols = st.columns([3, 1, 1, 1])
        header_cols[0].markdown(f"**{name}**")
        header_cols[1].markdown(f"記事: {tbl.get('total_articles', 0)}")
        header_cols[2].markdown(f"チャンク: {tbl.get('total_chunks', 0)}")

        if is_default:
            header_cols[3].caption("(デフォルト)")
        else:
            if header_cols[3].button("🗑️", key=f"del_tbl_{name}", help="削除"):
                st.session_state[f"delete_tbl_confirm_{name}"] = True
            if st.session_state.get(f"delete_tbl_confirm_{name}"):
                confirm_input = st.text_input(
                    f"削除するには「{name}」と入力してください",
                    key=f"del_tbl_input_{name}",
                )
                if confirm_input == name:
                    if st.button("本当に削除する", type="primary", key=f"del_tbl_go_{name}"):
                        try:
                            _api_delete(f"/api/tables/{name}?confirm={name}")
                            _cached_tables.clear()
                            _cached_stats.clear()
                            _cached_articles.clear()
                            st.success(f"テーブル「{name}」を削除しました")
                            st.rerun()
                        except Exception as e:
                            st.error(f"削除失敗: {e}")

        # Description inline edit
        with st.form(f"desc_form_{name}", clear_on_submit=False):
            current_desc = tbl.get("description", "")
            new_desc = st.text_input(
                "説明文",
                value=current_desc,
                placeholder="例: カスタマーサポートの問い合わせ記事",
                key=f"desc_input_{name}",
                help="MCP 経由のエージェントがどのテーブルを検索対象にするか判断するために使います。",
            )
            if st.form_submit_button("保存"):
                try:
                    _api_patch(f"/api/tables/{name}", {"description": new_desc.strip()})
                    st.success("説明文を保存しました")
                    st.rerun()
                except Exception as e:
                    st.error(f"保存失敗: {e}")

        st.divider()

    with st.form("create_table_form", clear_on_submit=True):
        st.markdown("**新しいテーブルを作成**")
        new_table_name = st.text_input("テーブル名", placeholder="例: work, private, archive")
        new_table_desc = st.text_input(
            "説明文（任意）",
            placeholder="例: 仕事関連の記事",
            help="後から Settings で変更できます。",
        )
        if st.form_submit_button("テーブルを作成"):
            if new_table_name.strip():
                try:
                    _api_post("/api/tables", {
                        "name": new_table_name.strip(),
                        "description": new_table_desc.strip(),
                    })
                    _cached_tables.clear()
                    st.success(f"テーブル「{new_table_name.strip()}」を作成しました")
                    st.rerun()
                except Exception as e:
                    st.error(f"作成失敗: {e}")
            else:
                st.warning("テーブル名を入力してください")
