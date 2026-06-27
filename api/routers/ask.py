"""routers/ask.py — Search, Q&A, ingest trigger, article management, stats and tags."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from constants import DEFAULT_TABLE
from db import qdrant_client
from generator import generator_answer, generator_build_sources
from ingest.pipeline import pipeline_ingest_directory, pipeline_save_answer
from manager import (
    manager_bulk_delete,
    manager_deduplicate,
    manager_delete_article,
    manager_find_article_chunks,
    manager_list_articles,
    manager_preview_bulk_delete,
    manager_section_delete,
    manager_stats,
)
from retriever import retriever_keyword_search, retriever_resolve_date_filter, retriever_search

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ask"])


# ── Request / Response models ─────────────────────────────────────────────────


class DateFilter(BaseModel):
    preset: str | None = None  # "today" | "week" | "month" | "3months" | "year"
    date_from: str | None = None  # "YYYY-MM-DD"
    date_to: str | None = None  # "YYYY-MM-DD"


class MetaFilter(BaseModel):
    """Optional metadata filters applied to both vector and keyword search."""

    tags_all: list[str] | None = None  # AND — every tag must be present
    tags_any: list[str] | None = None  # OR  — at least one tag present
    tags_exclude: list[str] | None = None  # NOT — none of these tags present
    last_n_days: int | None = None  # relative recency window (overrides date_filter)
    url_prefix: str | None = None  # source_url startswith / domain substring


class ReqAsk(BaseModel):
    question: str
    source_type: str = "all"
    distance_threshold: float | None = None  # None → use config value
    date_filter: DateFilter | None = None
    filters: MetaFilter | None = None
    table: str | None = None  # None → search all tables
    expand_queries: bool = False  # WebUI sets True; API/MCP default False
    hybrid: bool | None = None  # None → config default (default OFF)
    recency_decay: bool | None = None  # None → config default (default OFF)
    recency_half_life_days: float | None = None


class ReqSearch(BaseModel):
    query: str
    source_type: str = "all"
    top_k: int = 10
    distance_threshold: float | None = None  # None → use config value
    date_filter: DateFilter | None = None
    filters: MetaFilter | None = None
    table: str | None = None  # None → search all tables
    hybrid: bool | None = None  # None → config default (default OFF)
    recency_decay: bool | None = None  # None → config default (default OFF)
    recency_half_life_days: float | None = None
    expand_queries: bool | None = None  # None → config default (REST off; MCP defaults on)


class ReqSaveAnswer(BaseModel):
    question: str
    answer: str
    table: str = DEFAULT_TABLE
    duplicate_action: str = "new"  # "new" | "skip" | "overwrite"
    agent_id: str | None = None
    origin: str | None = None


class ReqBulkDelete(BaseModel):
    last_used_days_gte: int | None = None
    use_count_lte: int | None = None
    recorded_days_gte: int | None = None
    source_type: str = "all"
    table: str = DEFAULT_TABLE


class ReqKeywordSearch(BaseModel):
    query: str
    top_k: int = 20
    table: str | None = None  # None → search all tables


# ── Endpoints ─────────────────────────────────────────────────────────────────


def _resolve_search_opts(req, state) -> dict:
    """Resolve threshold, date range, metadata filters, hybrid/recency from request + config."""
    cfg = state.cfg
    threshold = (
        req.distance_threshold if req.distance_threshold is not None else cfg.get("search_distance_threshold", 0.3)
    )
    df = req.date_filter
    f = req.filters
    date_from, date_to = retriever_resolve_date_filter(
        df.preset if df else None,
        df.date_from if df else None,
        df.date_to if df else None,
        last_n_days=f.last_n_days if f else None,
    )
    hybrid = req.hybrid if req.hybrid is not None else bool(cfg.get("search_hybrid_enabled", False))
    recency = (
        req.recency_decay if req.recency_decay is not None else bool(cfg.get("search_recency_decay_enabled", False))
    )
    half_life = (
        req.recency_half_life_days
        if req.recency_half_life_days is not None
        else float(cfg.get("search_recency_half_life_days", 180))
    )
    return {
        "source_type_filter": req.source_type,
        "distance_threshold": threshold,
        "date_from": date_from,
        "date_to": date_to,
        "table_name": req.table,
        "hybrid": hybrid,
        "rrf_k": int(cfg.get("search_rrf_k", 60)),
        "tags_all": f.tags_all if f else None,
        "tags_any": f.tags_any if f else None,
        "tags_exclude": f.tags_exclude if f else None,
        "url_prefix": f.url_prefix if f else None,
        "recency_decay": recency,
        "recency_half_life_days": half_life,
        "recency_floor": float(cfg.get("search_recency_floor", 0.5)),
    }


@router.post("/api/ask")
def api_ask(req: ReqAsk, request: Request):
    state = request.app.state.state_ref
    opts = _resolve_search_opts(req, state)
    chunks = retriever_search(
        req.question,
        state.llm,
        state.embed,
        expand_queries=req.expand_queries,
        **opts,
    )
    answer = generator_answer(
        req.question,
        chunks,
        state.cfg.get("llm_answer_model", "claude-haiku-4-5-20251001"),
        provider=state.cfg.get("llm_answer_provider", "claude"),
        url=state.cfg.get("llm_answer_url"),
        thinking_budget=state.cfg.get("llm_answer_thinking_budget", 0),
    )
    sources = generator_build_sources(chunks)
    return {"answer": answer, "sources": sources}


@router.post("/api/search")
def api_search(req: ReqSearch, request: Request):
    state = request.app.state.state_ref
    opts = _resolve_search_opts(req, state)
    expand = (
        req.expand_queries
        if req.expand_queries is not None
        else bool(state.cfg.get("search_expand_queries_default", False))
    )
    chunks = retriever_search(
        req.query,
        state.llm,
        state.embed,
        top_k=req.top_k,
        expand_queries=expand,
        **opts,
    )
    return {"chunks": chunks}


@router.post("/api/keyword-search")
def api_keyword_search(req: ReqKeywordSearch, request: Request):
    chunks = retriever_keyword_search(req.query, top_k=req.top_k, table_name=req.table)
    return {"chunks": chunks, "count": len(chunks)}


@router.post("/api/ingest")
def api_ingest(request: Request, table: str = Query(DEFAULT_TABLE)):
    state = request.app.state.state_ref
    result = pipeline_ingest_directory(
        state.inbox_dir,
        state.done_dir,
        state.llm,
        state.embed,
        state.cfg,
        table_name=table,
    )
    return {
        "processed": result["processed"],
        "count": len(result["processed"]),
        "skipped": result["skipped"],
        "skipped_count": len(result["skipped"]),
    }


@router.post("/api/save-answer")
def api_save_answer(req: ReqSaveAnswer, request: Request):
    state = request.app.state.state_ref
    article_id = pipeline_save_answer(
        req.question,
        req.answer,
        state.embed,
        state.cfg,
        table_name=req.table,
        duplicate_action=req.duplicate_action,
        agent_id=req.agent_id,
        origin=req.origin,
    )
    return {"article_id": article_id}


@router.get("/api/articles")
def api_list_articles(
    request: Request,
    source_type: str = "all",
    table: str = Query(DEFAULT_TABLE),
    limit: int = Query(200),
    offset: int = Query(0),
):
    return {"articles": manager_list_articles(source_type, table_name=table, limit=limit, offset=offset)}


@router.get("/api/articles/{article_id}")
def api_get_article(article_id: str, table: str = Query(DEFAULT_TABLE)):
    chunks, _ = manager_find_article_chunks(article_id, preferred_table=table)
    if not chunks:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"chunks": chunks}


@router.delete("/api/articles/{article_id}")
def api_delete_article(article_id: str, request: Request, table: str = Query(DEFAULT_TABLE)):
    deleted = manager_delete_article(article_id, table_name=table)
    return {"deleted_rows": deleted}


@router.delete("/api/sections/{section_id}")
def api_delete_section(section_id: str, request: Request, table: str = Query(DEFAULT_TABLE)):
    deleted = manager_section_delete(section_id, table_name=table)
    return {"deleted_rows": deleted}


@router.post("/api/articles/bulk-delete-preview")
def api_bulk_delete_preview(req: ReqBulkDelete, request: Request):
    conditions = req.model_dump(exclude_none=True)
    table = conditions.pop("table", DEFAULT_TABLE)
    targets = manager_preview_bulk_delete(conditions, table_name=table)
    return {"targets": targets, "count": len(targets)}


@router.post("/api/articles/bulk-delete")
def api_bulk_delete(req: ReqBulkDelete, request: Request):
    conditions = req.model_dump(exclude_none=True)
    table = conditions.pop("table", DEFAULT_TABLE)
    deleted_rows = manager_bulk_delete(conditions, table_name=table)
    return {"deleted_rows": deleted_rows}


@router.post("/api/deduplicate")
def api_deduplicate(request: Request, table: str = Query(DEFAULT_TABLE)):
    result = manager_deduplicate(table_name=table)
    return result


@router.get("/api/stats")
def api_stats(request: Request, table: str = Query(DEFAULT_TABLE)):
    return manager_stats(table_name=table)


@router.get("/api/tags")
def api_tags(request: Request, table: str = Query(DEFAULT_TABLE)):
    from qdrant_client.http import models as qm

    flt = qm.Filter(must=[qm.FieldCondition(key="level", match=qm.MatchValue(value=1))])
    client = qdrant_client()
    tag_set: set[str] = set()
    offset = None
    while True:
        pts, offset = client.scroll(
            collection_name=table,
            scroll_filter=flt,
            limit=512,
            offset=offset,
            with_payload=["tags"],
            with_vectors=False,
        )
        for p in pts:
            for tag in (p.payload or {}).get("tags") or []:
                tag_set.add(tag)
        if offset is None:
            break
    return {"tags": sorted(tag_set)}
