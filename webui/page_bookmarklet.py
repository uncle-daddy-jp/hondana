"""page_bookmarklet.py — ブックマークレット生成ページ"""
from __future__ import annotations

import streamlit as st

from utils import _get_table_names, _public_api_url


def _bml_build(host: str, table: str) -> str:
    """ブックマークレット JavaScript 文字列を生成する。
    window.open GET 方式: HTTPS ページからの Mixed Content ブロックを回避する。
    """
    return (
        "javascript:(function(){"
        f"var u='{host}/clip?table={table}&url='+encodeURIComponent(location.href);"
        "window.open(u,'_blank','width=420,height=200,toolbar=0,menubar=0,location=0');"
        "})();"
    )


# ── 定数 ──────────────────────────────────────────────────────────────────────

_IP_LOCAL = "ローカル（LAN内）"
_IP_GLOBAL = "グローバル（外部公開）"
_IP_CUSTOM = "カスタム"


# ── ページ本体 ─────────────────────────────────────────────────────────────────

def page_bookmarklet() -> None:
    st.header("🔖 ブックマークレット")
    st.caption("ブラウザのブックマークバーに追加すると、閲覧中のページをワンクリックで HONDANA に登録できます。")

    # ① テーブル選択
    tables = _get_table_names()
    table = st.selectbox("登録先テーブル", tables, key="bml_table")

    # ② サーバーアドレス（ブラウザから直接叩くため公開アドレス）
    ip_mode = st.radio(
        "サーバーアドレス",
        [_IP_LOCAL, _IP_GLOBAL, _IP_CUSTOM],
        horizontal=True,
        key="bml_ip_mode",
    )
    if ip_mode == _IP_LOCAL:
        host = st.text_input("ローカルアドレス", value=_public_api_url(), key="bml_host_local")
    elif ip_mode == _IP_GLOBAL:
        host = st.text_input("グローバルアドレス（例: http://203.0.113.10:8200）", key="bml_host_global")
    else:
        host = st.text_input("カスタムアドレス", key="bml_host_custom")

    st.divider()

    # ③ 生成（/clip エンドポイントは重複時 overwrite 固定なので動作選択は不要）
    if not st.button("ブックマークレットを生成", key="bml_generate"):
        return

    host = (host or "").strip()
    if not host.startswith(("http://", "https://")):
        st.error("有効なアドレスを入力してください（http:// または https:// で始まること）")
        return

    bml = _bml_build(host, table)

    st.success("生成しました。下のボタンをブックマークバーへドラッグしてください。")
    st.markdown(
        f'<a href="{bml}" style="display:inline-block;padding:10px 20px;'
        f"background:#1976D2;color:#fff;border-radius:6px;text-decoration:none;"
        f'font-weight:bold;font-size:15px;">📌 HONDANA → {table}</a>',
        unsafe_allow_html=True,
    )
    st.caption("⚠️ リンクを**クリックせず**、ブックマークバーへ**ドラッグ**してください。")

    with st.expander("コードを確認"):
        st.code(bml, language="javascript")
