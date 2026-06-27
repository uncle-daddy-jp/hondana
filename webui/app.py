"""app.py — HONDANA Streamlit WebUI ルーティング。"""
from __future__ import annotations

import streamlit as st

from page_bookmarklet import page_bookmarklet
from ui_pages.chat import page_chat
from ui_pages.ingest import page_ingest
from ui_pages.jobs import page_jobs
from ui_pages.knowledge import page_knowledge
from ui_pages.settings import page_settings
from utils import _cached_config


def main() -> None:
    instance_name = _cached_config().get("instance_name", "").strip()

    page_title = f"HONDANA — {instance_name}" if instance_name else "HONDANA"
    st.set_page_config(page_title=page_title, page_icon="📚", layout="wide",
                       menu_items={})

    st.sidebar.title("📚 HONDANA")
    if instance_name:
        st.sidebar.caption(instance_name)
    else:
        st.sidebar.caption("個人向け RAG 知識ベースシステム")

    page = st.sidebar.radio("ページ", ["💬 Chat", "📥 Ingest", "📚 Knowledge", "⚙️ Settings", "🔧 Jobs", "🔖 Bookmarklet"])

    if page == "💬 Chat":
        page_chat()
    elif page == "📥 Ingest":
        page_ingest()
    elif page == "📚 Knowledge":
        page_knowledge()
    elif page == "⚙️ Settings":
        page_settings()
    elif page == "🔧 Jobs":
        page_jobs()
    elif page == "🔖 Bookmarklet":
        page_bookmarklet()


if __name__ == "__main__":
    main()
