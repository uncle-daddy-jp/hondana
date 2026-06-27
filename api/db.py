"""
db.py — Shared Qdrant utilities: client singleton, collection ops, point conversion.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from constants import DEFAULT_TABLE

_TABLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_client: QdrantClient | None = None
_client_lock = threading.Lock()


def qdrant_client(url: str | None = None) -> QdrantClient:
    """Return module-level QdrantClient singleton. URL is fixed after first call."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _url = url or os.environ.get("QDRANT_URL") or "http://qdrant:6333"
                _client = QdrantClient(url=_url, prefer_grpc=False, timeout=30)
    return _client


def db_list_tables() -> list[str]:
    """Return sorted list of all collection names."""
    return sorted(c.name for c in qdrant_client().get_collections().collections)


def db_validate_table_name(name: str) -> None:
    """Raise ValueError if name contains illegal characters."""
    if not _TABLE_NAME_RE.match(name):
        raise ValueError(f"Invalid table name '{name}'. Use only a-z, A-Z, 0-9, _ or -.")


def db_table_exists(table_name: str) -> bool:
    """Return True if the collection exists."""
    return table_name in db_list_tables()


def db_drop_table(table_name: str) -> None:
    """Drop a collection. Raises ValueError if it doesn't exist."""
    if not db_table_exists(table_name):
        raise ValueError(f"Table '{table_name}' not found")
    qdrant_client().delete_collection(table_name)


def db_url_exists(source_url: str, table_name: str = DEFAULT_TABLE) -> bool:
    """source_url が指定コレクションの L1 チャンクに存在するか確認する。"""
    if not db_table_exists(table_name):
        return False
    flt = qm.Filter(
        must=[
            qm.FieldCondition(key="source_url", match=qm.MatchValue(value=source_url)),
            qm.FieldCondition(key="level", match=qm.MatchValue(value=1)),
        ]
    )
    pts, _ = qdrant_client().scroll(
        collection_name=table_name,
        scroll_filter=flt,
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    return len(pts) > 0


def db_row_to_dict(point_or_record: Any) -> dict:
    """Convert Qdrant ScoredPoint / Record → hondana row dict (no vector)."""
    if hasattr(point_or_record, "payload"):
        d = dict(point_or_record.payload or {})
        # Qdrant の point.id が真の id（payload の "id" フィールドと同値）
        if "id" not in d:
            d["id"] = str(point_or_record.id)
        if hasattr(point_or_record, "score"):
            d["_score"] = point_or_record.score
    else:
        d = dict(point_or_record)
    # tags は list で保存しているが、旧データや JSON 文字列の場合はデコード
    if isinstance(d.get("tags"), str):
        try:
            d["tags"] = json.loads(d["tags"])
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
    d.pop("vector", None)
    return d
