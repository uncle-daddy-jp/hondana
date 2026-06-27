"""pages/ingest.py — 📥 Ingest ページ。"""
from __future__ import annotations

import time

import streamlit as st

from api_client import _api_get, _api_post, _api_upload
from utils import _cached_articles, _cached_config, _cached_stats, _get_table_names


def _ingest_poll_active_job() -> bool:
    """アクティブなジョブがあれば進捗を表示し True を返す。完了/失敗なら False。"""
    job_id = st.session_state.get("ingest_active_job_id")
    if not job_id:
        return False
    try:
        job = _api_get(f"/api/jobs/{job_id}")
    except Exception:
        return False
    status = job["status"]
    if status == "done":
        st.success(f"完了: {job.get('result', {})}")
        del st.session_state["ingest_active_job_id"]
        _cached_stats.clear()
        _cached_articles.clear()
        return False
    if status in ("failed", "error"):
        st.error(f"エラー: {job.get('error', '不明')}")
        del st.session_state["ingest_active_job_id"]
        return False
    st.info(f"処理中... (status: {status})")
    time.sleep(2)
    st.rerun()
    return True


def page_ingest() -> None:
    st.header("📥 取り込み")

    _ingest_poll_active_job()

    table_names = _get_table_names()
    active_table = st.selectbox("取り込み先テーブル", table_names, key="ingest_table")


    tab_inbox, tab_url, tab_file = st.tabs(["📂 inbox フォルダ", "🌐 URL", "📄 ファイルアップロード"])

    with tab_inbox:
        cfg_inbox = _cached_config()
        if cfg_inbox.get("inbox_dir"):
            st.info(f"**投入口**: `{cfg_inbox['inbox_dir']}`  →  **完了先**: `{cfg_inbox.get('done_dir', '未設定')}`")
        else:
            st.warning("設定を取得できませんでした。⚙️ Settings で設定してください。")

        if st.button("▶️ 新着を処理する", type="primary", use_container_width=True):
            with st.spinner("取り込み処理中... (LLM要約・埋め込みを実行しています)"):
                try:
                    res = _api_post(f"/api/ingest?table={active_table}")
                    count = res["count"]
                    files = res["processed"]
                    skipped = res.get("skipped", [])
                    if count == 0 and not skipped:
                        st.info("処理対象のファイルがありませんでした")
                    if count > 0:
                        st.success(f"{count} 件を取り込みました")
                        _cached_stats.clear()
                        _cached_articles.clear()
                        for f in files:
                            st.markdown(f"- ✅ {f}")
                    if skipped:
                        st.warning(f"{len(skipped)} 件をスキップしました（URL重複）")
                        for f in skipped:
                            st.markdown(f"- ⏭️ {f}")
                except Exception as e:
                    st.error(f"エラー: {e}")

    with tab_url:
        st.caption("URLをフェッチして知識ベースに取り込みます（LLM要約あり・バックグラウンド処理）")
        with st.form("ingest_url_form", clear_on_submit=True):
            url_input = st.text_input("URL", placeholder="https://example.com/article")
            dup_action = st.selectbox("重複時の動作", ["overwrite", "skip", "new"],
                                      format_func=lambda x: {"overwrite": "上書き", "skip": "スキップ", "new": "別記事として追加"}[x])
            submitted = st.form_submit_button("取り込む", type="primary")

        if submitted and url_input.strip():
            try:
                res = _api_post("/api/ingest/url", {"url": url_input.strip(), "duplicate_action": dup_action, "table": active_table})
                st.session_state["ingest_active_job_id"] = res["job_id"]
                st.rerun()
            except Exception as e:
                st.error(f"エラー: {e}")

    with tab_file:
        st.caption("ファイルをアップロードして知識ベースに取り込みます（バックグラウンド処理）")
        uploaded = st.file_uploader("ファイルを選択", type=["md", "html", "htm", "pdf", "docx", "pptx", "txt", "xlsx"])
        dup_action_file = st.selectbox("重複時の動作", ["overwrite", "skip", "new"],
                                       key="file_dup_action",
                                       format_func=lambda x: {"overwrite": "上書き", "skip": "スキップ", "new": "別記事として追加"}[x])
        if st.button("アップロードして取り込む", type="primary", disabled=uploaded is None):
            try:
                res = _api_upload("/api/ingest/file", uploaded.name, uploaded.read(),
                                  {"duplicate_action": dup_action_file, "table": active_table})
                st.session_state["ingest_active_job_id"] = res["job_id"]
                st.rerun()
            except Exception as e:
                st.error(f"エラー: {e}")

    st.divider()

    with st.expander("🔧 メンテナンス"):
        st.caption("タイトル・URLが重複している記事を検出し、古いものを削除します。")
        if st.button("🧹 重複を削除する"):
            with st.spinner("重複チェック中..."):
                try:
                    res = _api_post(f"/api/deduplicate?table={active_table}")
                    n = res["deleted_articles"]
                    if n == 0:
                        st.info("重複は見つかりませんでした")
                    else:
                        st.success(f"{n} 件の重複を削除しました（{res['deleted_rows']} チャンク）")
                except Exception as e:
                    st.error(f"エラー: {e}")
