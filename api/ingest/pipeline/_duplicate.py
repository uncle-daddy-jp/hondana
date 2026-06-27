"""Duplicate detection helpers for existing articles in Qdrant."""

from __future__ import annotations

import logging
from pathlib import Path

from qdrant_client.http import models as qm

from db import qdrant_client

logger = logging.getLogger(__name__)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _scroll_one(collection: str, flt: qm.Filter, fields: list[str]) -> dict:
    """Scroll a single L1 point and return its payload subset (or empty dict)."""
    pts, _ = qdrant_client().scroll(
        collection_name=collection,
        scroll_filter=flt,
        limit=1,
        with_payload=fields,
        with_vectors=False,
    )
    return (pts[0].payload or {}) if pts else {}


def _l1_filter(*extra: qm.Condition) -> qm.Filter:
    return qm.Filter(
        must=[
            qm.FieldCondition(key="level", match=qm.MatchValue(value=1)),
            *extra,
        ]
    )


# ── Public API ────────────────────────────────────────────────────────────────


def _pipeline_file_is_unchanged(collection: str, filename: str, file_mtime_iso: str) -> bool:
    """ファイル名（stem）が DB に存在し、recorded_at >= file_mtime なら True（パース前スキップ）。"""
    flt = _l1_filter(qm.FieldCondition(key="title", match=qm.MatchValue(value=Path(filename).stem)))
    payload = _scroll_one(collection, flt, ["recorded_at"])
    return bool(payload) and payload.get("recorded_at", "") >= file_mtime_iso


def _pipeline_find_article_id_by_url(collection: str, source_url: str) -> str | None:
    """Return article_id of existing L1 chunk with same source_url, or None."""
    if not source_url:
        return None
    flt = _l1_filter(qm.FieldCondition(key="source_url", match=qm.MatchValue(value=source_url)))
    return _scroll_one(collection, flt, ["article_id"]).get("article_id")


def _pipeline_find_article_id_by_title(collection: str, title: str) -> str | None:
    """Return article_id of existing L1 chunk with same title, or None."""
    if not title:
        return None
    flt = _l1_filter(qm.FieldCondition(key="title", match=qm.MatchValue(value=title)))
    return _scroll_one(collection, flt, ["article_id"]).get("article_id")


def _pipeline_check_duplicate(collection: str, source_url: str, title: str, content_hash: str) -> dict:
    """
    Check if a document already exists in the collection.
    Returns {"existing_id": str | None, "same_hash": bool}.
    """
    existing_id = _pipeline_find_article_id_by_url(collection, source_url) or _pipeline_find_article_id_by_title(
        collection, title
    )
    if not existing_id:
        return {"existing_id": None, "same_hash": False}

    flt = _l1_filter(qm.FieldCondition(key="article_id", match=qm.MatchValue(value=existing_id)))
    stored_hash = _scroll_one(collection, flt, ["content_hash"]).get("content_hash", "")
    return {
        "existing_id": existing_id,
        "same_hash": (stored_hash == content_hash and bool(content_hash)),
    }
