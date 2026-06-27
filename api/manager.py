"""
manager.py — Knowledge lifecycle: list, delete, and bulk-delete articles and sections.

Article deletion (L1-scoped): removes all L1/L2/L3 chunks for an article_id.
Section deletion (L2-scoped): removes one L2 chunk and its L3 children.
Bulk delete conditions operate at L2 level.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from qdrant_client.http import models as qm
from qdrant_client.http.exceptions import UnexpectedResponse

from db import DEFAULT_TABLE, db_row_to_dict, db_table_exists, qdrant_client

logger = logging.getLogger(__name__)

# ── Public API ────────────────────────────────────────────────────────────────


def _list_l1(table_name: str, flt: qm.Filter, limit: int | None, offset: int = 0) -> list[dict]:
    """Return L1 rows matching `flt`, newest-first (recorded_at desc), with limit/offset.

    Uses Qdrant server-side order_by + early-stop when a limit is given (O(offset+limit));
    falls back to a full scan + in-memory sort if order_by is unavailable (OffsetOutOfBounds).
    Does NOT stamp _table_name — callers merging across tables add it themselves.
    Shared by manager_list_articles (single table) and manager_get_recent (cross-table).
    """
    if not db_table_exists(table_name):
        return []

    if limit is not None:
        # Server-side ordering + early-stop fetch: O(offset + limit) instead of O(total)
        # Falls back to full-scan sort if Qdrant order_by returns an internal error (OffsetOutOfBounds)
        try:
            pts, _ = qdrant_client().scroll(
                collection_name=table_name,
                scroll_filter=flt,
                limit=offset + limit,
                order_by=qm.OrderBy(key="recorded_at", direction=qm.Direction.DESC),
                offset=None,
                with_payload=True,
                with_vectors=False,
            )
            return [db_row_to_dict(p) for p in pts][offset : offset + limit]
        except UnexpectedResponse:
            pass  # fall through to full-scan

    # limit=None path (also used as fallback when order_by fails)
    articles = [db_row_to_dict(p) for p in _scroll_all(table_name, flt)]
    articles.sort(key=lambda a: a.get("recorded_at", ""), reverse=True)
    sliced = articles[offset:] if offset else articles
    return sliced[:limit] if limit is not None else sliced


def manager_list_articles(
    source_type_filter: str = "all",
    table_name: str = DEFAULT_TABLE,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """
    Return one record per article (L1 chunks only) with usage stats.
    Sorted by recorded_at descending.
    limit=None returns all articles (used by the deduplicate caller).
    """
    must = [qm.FieldCondition(key="level", match=qm.MatchValue(value=1))]
    if source_type_filter != "all":
        must.append(qm.FieldCondition(key="source_type", match=qm.MatchValue(value=source_type_filter)))
    return _list_l1(table_name, qm.Filter(must=must), limit, offset)


def manager_get_article_chunks(article_id: str, table_name: str = DEFAULT_TABLE) -> list[dict]:
    """Return all chunks for a given article_id, sorted by level."""
    if not db_table_exists(table_name):
        return []
    flt = qm.Filter(
        must=[
            qm.FieldCondition(key="article_id", match=qm.MatchValue(value=article_id)),
        ]
    )
    chunks = [db_row_to_dict(p) for p in _scroll_all(table_name, flt)]
    chunks.sort(key=lambda c: (c.get("level", 0), c.get("parent_id", ""), c.get("position", 0), c.get("heading", "")))
    return chunks


def manager_find_article_chunks(article_id: str, preferred_table: str | None = None) -> tuple[list[dict], str | None]:
    """Find an article's chunks by id, with all-table fallback.

    With preferred_table, try it first then the remaining tables; without it, scan every table.
    Returns (chunks, table_name) for the first table containing the article, or ([], None).
    Shared by GET /api/articles/{id}, the MCP get_article tool, and the /k/{id} permalink view.
    """
    from db import db_list_tables

    if preferred_table is not None:
        chunks = manager_get_article_chunks(article_id, table_name=preferred_table)
        if chunks:
            return chunks, preferred_table

    for table in db_list_tables():
        if table == preferred_table:
            continue
        chunks = manager_get_article_chunks(article_id, table_name=table)
        if chunks:
            if preferred_table is not None:
                logger.info("find_article fallback: found in table=%s (requested=%s)", table, preferred_table)
            return chunks, table

    return [], None


def manager_delete_article(article_id: str, table_name: str = DEFAULT_TABLE) -> int:
    """Delete all chunks for article_id. Returns number of rows deleted."""
    client = qdrant_client()
    before = client.count(collection_name=table_name, exact=True).count
    client.delete(
        collection_name=table_name,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                must=[
                    qm.FieldCondition(key="article_id", match=qm.MatchValue(value=article_id)),
                ]
            )
        ),
        wait=True,
    )
    after = client.count(collection_name=table_name, exact=True).count
    return before - after


def manager_section_delete(section_id: str, table_name: str = DEFAULT_TABLE) -> int:
    """Delete one L2 section and all its L3 children. Returns rows deleted."""
    client = qdrant_client()
    before = client.count(collection_name=table_name, exact=True).count
    client.delete(
        collection_name=table_name,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                should=[
                    qm.HasIdCondition(has_id=[section_id]),
                    qm.FieldCondition(key="parent_id", match=qm.MatchValue(value=section_id)),
                ]
            )
        ),
        wait=True,
    )
    after = client.count(collection_name=table_name, exact=True).count
    return before - after


def manager_section_list(source_type_filter: str = "all", table_name: str = DEFAULT_TABLE) -> list[dict]:
    """Return all L2 sections with usage stats, sorted by recorded_at descending."""
    if not db_table_exists(table_name):
        return []
    must = [qm.FieldCondition(key="level", match=qm.MatchValue(value=2))]
    if source_type_filter != "all":
        must.append(qm.FieldCondition(key="source_type", match=qm.MatchValue(value=source_type_filter)))
    flt = qm.Filter(must=must)
    sections = [db_row_to_dict(p) for p in _scroll_all(table_name, flt)]
    sections.sort(key=lambda s: s.get("recorded_at", ""), reverse=True)
    return sections


def manager_preview_bulk_delete(conditions: dict, table_name: str = DEFAULT_TABLE) -> list[dict]:
    """
    Preview L2 sections that match deletion conditions without deleting.
    conditions keys (all optional, combined with AND):
      - last_used_days_gte: int   — last_used_at is >= N days ago (or never used)
      - use_count_lte: int        — use_count <= N
      - recorded_days_gte: int    — recorded_at is >= N days ago
      - source_type: str          — "article" | "llm_answer" | "all"
    Returns list of L2 section dicts that would be deleted.
    """
    sections = manager_section_list(conditions.get("source_type", "all"), table_name)
    return [s for s in sections if _manager_matches_conditions(s, conditions)]


def manager_bulk_delete(conditions: dict, table_name: str = DEFAULT_TABLE) -> int:
    """Delete L2 sections matching conditions (plus their L3 children). Returns total rows deleted."""
    targets = manager_preview_bulk_delete(conditions, table_name)
    total = 0
    for section in targets:
        total += manager_section_delete(section["id"], table_name)
    return total


def manager_get_recent(
    n: int = 10,
    table_name: str | None = None,
) -> list[dict]:
    """Return the n most-recently recorded articles (L1 only) across one or all tables.

    Each returned dict includes title, source_url, tags, recorded_at, article_id, and _table_name.
    table_name=None → search all collections and merge.
    """
    from db import db_list_tables

    target_tables = db_list_tables() if table_name is None else [table_name]
    flt = qm.Filter(must=[qm.FieldCondition(key="level", match=qm.MatchValue(value=1))])
    articles: list[dict] = []
    for tname in target_tables:
        rows = _list_l1(tname, flt, n)
        for d in rows:
            d["_table_name"] = tname
        articles.extend(rows)

    articles.sort(key=lambda a: a.get("recorded_at", ""), reverse=True)
    return articles[:n]


def manager_deduplicate(table_name: str = DEFAULT_TABLE) -> dict:
    """
    Remove duplicate articles sharing the same URL or the same title.
    Articles are processed newest-first; the first occurrence is kept, the rest deleted.
    Returns counts of deleted articles and rows.
    """
    articles = manager_list_articles(table_name=table_name)  # newest first

    seen_urls: dict[str, str] = {}
    seen_titles: dict[str, str] = {}
    to_delete: list[str] = []

    for article in articles:
        url = (article.get("source_url") or "").strip()
        title = (article.get("title") or "").strip()
        article_id = article["article_id"]

        is_dup = (url and url in seen_urls) or (title and title in seen_titles)
        if is_dup:
            to_delete.append(article_id)
        else:
            if url:
                seen_urls[url] = article_id
            if title:
                seen_titles[title] = article_id

    deleted_rows = 0
    for article_id in to_delete:
        deleted_rows += manager_delete_article(article_id, table_name)

    return {"deleted_articles": len(to_delete), "deleted_rows": deleted_rows}


def manager_stats(table_name: str = DEFAULT_TABLE) -> dict:
    """Return aggregate DB statistics using exact filtered counts."""
    if not db_table_exists(table_name):
        return {"total_articles": 0, "total_chunks": 0, "article_count": 0, "answer_count": 0}
    client = qdrant_client()
    l1_flt = qm.Filter(must=[qm.FieldCondition(key="level", match=qm.MatchValue(value=1))])
    total_chunks = client.count(collection_name=table_name, exact=False).count
    total_articles = client.count(collection_name=table_name, exact=True, count_filter=l1_flt).count
    article_count = client.count(
        collection_name=table_name,
        exact=True,
        count_filter=qm.Filter(
            must=[
                qm.FieldCondition(key="level", match=qm.MatchValue(value=1)),
                qm.FieldCondition(key="source_type", match=qm.MatchValue(value="article")),
            ]
        ),
    ).count
    answer_count = client.count(
        collection_name=table_name,
        exact=True,
        count_filter=qm.Filter(
            must=[
                qm.FieldCondition(key="level", match=qm.MatchValue(value=1)),
                qm.FieldCondition(key="source_type", match=qm.MatchValue(value="llm_answer")),
            ]
        ),
    ).count
    return {
        "total_articles": total_articles,
        "total_chunks": total_chunks,
        "article_count": article_count,
        "answer_count": answer_count,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────


def _scroll_all(collection: str, flt: qm.Filter) -> list:
    """Cursor-paginated scroll to retrieve all matching points.

    Fetches IDs first (no payload), then batch-retrieves payloads.
    Workaround for Qdrant OffsetOutOfBounds bug on on_disk_payload collections.
    """
    client = qdrant_client()

    # Phase 1: collect all IDs without payload (unaffected by the bug)
    ids = []
    offset = None
    while True:
        pts, offset = client.scroll(
            collection_name=collection,
            scroll_filter=flt,
            limit=512,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.extend(p.id for p in pts)
        if offset is None:
            break

    if not ids:
        return []

    # Phase 2: fetch payloads in batches of 100, skipping corrupted points
    results = []
    batch_size = 100
    for i in range(0, len(ids), batch_size):
        chunk = ids[i : i + batch_size]
        try:
            batch = client.retrieve(
                collection_name=collection,
                ids=chunk,
                with_payload=True,
                with_vectors=False,
            )
            results.extend(batch)
        except UnexpectedResponse:
            # A corrupted point is in this chunk; try one-by-one and skip bad ones
            for uid in chunk:
                try:
                    pts = client.retrieve(
                        collection_name=collection,
                        ids=[uid],
                        with_payload=True,
                        with_vectors=False,
                    )
                    results.extend(pts)
                except UnexpectedResponse:
                    pass
    return results


def _manager_matches_conditions(section: dict, conditions: dict) -> bool:
    now = datetime.now(timezone.utc)

    if "last_used_days_gte" in conditions:
        threshold = conditions["last_used_days_gte"]
        last_used = section.get("last_used_at", "")
        if last_used:
            delta = (now - datetime.fromisoformat(last_used)).days
            if delta < threshold:
                return False
        # Never used counts as matching (infinite days since last use)

    if "use_count_lte" in conditions:
        if section.get("use_count", 0) > conditions["use_count_lte"]:
            return False

    if "recorded_days_gte" in conditions:
        threshold = conditions["recorded_days_gte"]
        recorded = section.get("recorded_at", "")
        if recorded:
            delta = (now - datetime.fromisoformat(recorded)).days
            if delta < threshold:
                return False

    return True
