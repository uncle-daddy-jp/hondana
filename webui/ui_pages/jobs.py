"""pages/jobs.py — 🔧 Jobs ページ。"""
from __future__ import annotations

import streamlit as st

from api_client import _api_get
from utils import _to_jst


def page_jobs() -> None:
    st.header("🔧 ジョブ管理")

    # ── 統計サマリー ─────────────────────────────────────────────────────────
    try:
        stats = _api_get("/api/jobs/stats")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("pending",  stats.get("pending", 0))
        c2.metric("running",  stats.get("running", 0))
        c3.metric("done",     stats.get("done", 0))
        c4.metric("failed",   stats.get("failed", 0))
        c5.metric("total",    stats.get("total", 0))
    except Exception as e:
        st.warning(f"統計取得失敗: {e}")

    if st.button("🔄 更新"):
        st.rerun()

    st.divider()

    # ── 失敗したジョブ一覧（status フィルタで必要な件数のみ取得）──────────────
    st.subheader("❌ 失敗したジョブ")
    try:
        res = _api_get("/api/jobs?limit=100&status=failed")
        failed_jobs = res.get("jobs", [])
    except Exception as e:
        st.error(f"ジョブ一覧の取得失敗: {e}")
        return

    if not failed_jobs:
        st.info("失敗したジョブはありません")
    else:
        st.warning(f"{len(failed_jobs)} 件の失敗ジョブがあります")
        for j in failed_jobs:
            payload = j.get("payload") or {}
            label_parts = [_to_jst(j["created_at"], "long"), f"ID: {j['id'][:8]}..."]
            if url := payload.get("url"):
                label_parts.append(url[:60])
            elif title := payload.get("title"):
                label_parts.append(title[:60])
            with st.expander(" | ".join(label_parts)):
                st.markdown(f"**タイプ**: `{j['type']}`")
                st.markdown(f"**作成日時**: {_to_jst(j['created_at'], 'long')}")
                st.markdown(f"**終了日時**: {_to_jst(j.get('finished_at'), 'long')}")
                st.markdown(f"**リトライ回数**: {j.get('retry_count', '—')}")
                if url:
                    st.markdown(f"**URL**: {url}")
                elif title:
                    st.markdown(f"**タイトル**: {title}")
                st.error(f"エラー: {j.get('error') or '不明'}")

    st.divider()

    # ── 最近のジョブ全件（遅延ロード：ボタンを押したときのみ取得）─────────────
    with st.expander("最近のジョブ一覧（全ステータス）"):
        if not st.session_state.get("jobs_recent_loaded"):
            if st.button("一覧を読み込む", key="load_recent_jobs"):
                st.session_state.jobs_recent_loaded = True
                st.rerun()
        else:
            try:
                res = _api_get("/api/jobs?limit=50")
                recent_jobs = res.get("jobs", [])
                icon = {"done": "✅", "failed": "❌", "running": "⏳", "pending": "⏸️", "error": "⚠️"}
                for j in recent_jobs:
                    payload = j.get("payload") or {}
                    label = payload.get("url") or payload.get("title") or payload.get("filename") or ""
                    st.markdown(
                        f"{icon.get(j['status'], '?')} `{j['id'][:8]}` | {j['type']} | "
                        f"{_to_jst(j['created_at'], 'long')} | {label[:50]}"
                    )
            except Exception as e:
                st.error(f"取得失敗: {e}")
