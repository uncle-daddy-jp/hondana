"""utils.py — 共有ユーティリティ・環境変数・キャッシュ付き API 呼び出し。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import streamlit as st

API_URL = os.environ.get("HONDANA_API_URL", "http://localhost:8000")
_API_KEY = os.environ.get("HONDANA_API_KEY", "")


def _public_api_url() -> str:
    """Return the browser-accessible API base URL (for permalink generation).

    HONDANA_PUBLIC_URL を設定すればそれを使う。未設定の場合は、ブラウザから到達できない
    内部 URL (api:8000 / localhost) を、公開ポート localhost:8200 に読み替える。
    """
    custom = os.environ.get("HONDANA_PUBLIC_URL", "").rstrip("/")
    if custom:
        return custom
    url = API_URL
    if "api:8000" in url or "localhost" in url:
        return "http://localhost:8200"
    return url.rstrip("/")


_JST = timezone(timedelta(hours=9))


def _to_jst(utc_str: str | None, fmt: str = "short") -> str:
    """UTC ISO文字列をJST表示文字列に変換する。

    fmt="short" → "2026-04-12"
    fmt="long"  → "2026-04-12 18:30:05"
    """
    if not utc_str:
        return "—"
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        jst = dt.astimezone(_JST)
        return jst.strftime("%Y-%m-%d") if fmt == "short" else jst.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return utc_str


def _headers() -> dict:
    return {"X-API-Key": _API_KEY} if _API_KEY else {}


# ── Cached API calls ──────────────────────────────────────────────────────────
# Streamlit はウィジェット操作のたびにスクリプト全体を再実行する。
# 頻繁に変わらないデータを st.cache_data でキャッシュし、不要な API 呼び出しを排除する。
# 変更操作（保存・削除）の後は該当する .clear() を呼んでキャッシュを破棄すること。

@st.cache_data(ttl=30)
def _cached_config() -> dict:
    from api_client import _api_get
    try:
        return _api_get("/api/config")
    except Exception:
        return {}


@st.cache_data(ttl=30)
def _cached_tables() -> list[dict]:
    from api_client import _api_get
    try:
        return _api_get("/api/tables")
    except Exception:
        return []


@st.cache_data(ttl=60)
def _cached_stats(table: str) -> dict:
    from api_client import _api_get
    return _api_get(f"/api/stats?table={table}")


@st.cache_data(ttl=60)
def _cached_articles(source_type: str, table: str) -> list[dict]:
    from api_client import _api_get
    return _api_get(f"/api/articles?source_type={source_type}&table={table}").get("articles", [])


@st.cache_data(ttl=30)
def _cached_keys() -> list[dict]:
    from api_client import _api_get
    try:
        return _api_get("/api/keys").get("api_keys", [])
    except Exception:
        return []


def _get_table_names() -> list[str]:
    """Return available table names from API, fallback to default."""
    names = [t["name"] for t in _cached_tables()]
    return names if names else ["hondana_chunks"]
