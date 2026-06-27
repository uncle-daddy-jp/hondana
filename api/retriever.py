"""
retriever.py — Vector search with optional query expansion and parent-chunk context retrieval.

Search strategy:
  1. Optionally expand the question into 5 queries (3 JP + 2 EN) via LLM
     (expand_queries=True). Otherwise use the original question as a single query.
  2. Search L3 chunks with each query; also search L1 for overview hits
  3. For every L3 hit, fetch the parent L2 chunk (full section context)
  4. Deduplicate by chunk id; sort by score
  5. Update last_used_at and use_count for all returned chunks

Cross-table search:
  - table_name=None  → search all collections, merge and re-rank results
  - table_name="foo" → search only "foo"
  Chunks carry a _table_name key so parent enrichment and usage update
  know which collection each chunk belongs to.

Distance convention:
  LanceDB: _distance < threshold (small = similar)
  Qdrant:  score > score_threshold = 1.0 - distance_threshold (large = similar)
  For backward compat, _distance = 1.0 - score is added to every returned chunk.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from qdrant_client.http import models as qm

from db import DEFAULT_TABLE, db_list_tables, db_row_to_dict, qdrant_client
from hondana_types import EmbedModel, LLMClient

logger = logging.getLogger(__name__)


# ── Public API ────────────────────────────────────────────────────────────────


def retriever_resolve_date_filter(
    preset: str | None,
    date_from: str | None,
    date_to: str | None,
    last_n_days: int | None = None,
) -> tuple[str | None, str | None]:
    """
    Resolve a preset keyword, an explicit range, or a relative "last N days" window to
    (date_from, date_to) YYYY-MM-DD strings.
    Priority: last_n_days > preset > explicit dates.
    """
    if last_n_days is not None and last_n_days > 0:
        today = datetime.now(timezone.utc).date()
        return str(today - timedelta(days=last_n_days - 1)), str(today)
    if preset:
        offsets = {
            "today": 0,
            "week": 6,
            "month": 29,
            "3months": 89,
            "year": 364,
        }
        if preset in offsets:
            today = datetime.now(timezone.utc).date()
            date_from = str(today - timedelta(days=offsets[preset]))
            date_to = str(today)
    return date_from, date_to


# ── Filter / scoring helpers (shared by vector + keyword search) ────────────────


def _build_filter(
    source_type: str = "all",
    date_from: str | None = None,
    date_to: str | None = None,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_exclude: list[str] | None = None,
    level: int | None = None,
) -> qm.Filter | None:
    """Build a Qdrant Filter from the common metadata conditions. Returns None if empty.

    tags_all  → every tag must be present (AND)
    tags_any  → at least one tag present (OR)
    tags_exclude → none of these tags present (NOT)
    Note: source_url prefix is handled client-side (KEYWORD index has no prefix match).
    """
    must: list[qm.Condition] = []
    must_not: list[qm.Condition] = []
    if level is not None:
        must.append(qm.FieldCondition(key="level", match=qm.MatchValue(value=level)))
    if source_type and source_type != "all":
        must.append(qm.FieldCondition(key="source_type", match=qm.MatchValue(value=source_type)))
    if date_from:
        must.append(
            qm.FieldCondition(
                key="recorded_at",
                range=qm.DatetimeRange(gte=datetime.fromisoformat(f"{date_from}T00:00:00+00:00")),
            )
        )
    if date_to:
        must.append(
            qm.FieldCondition(
                key="recorded_at",
                range=qm.DatetimeRange(lte=datetime.fromisoformat(f"{date_to}T23:59:59+00:00")),
            )
        )
    for tag in tags_all or []:
        must.append(qm.FieldCondition(key="tags", match=qm.MatchValue(value=tag)))
    if tags_any:
        must.append(qm.FieldCondition(key="tags", match=qm.MatchAny(any=list(tags_any))))
    if tags_exclude:
        must_not.append(qm.FieldCondition(key="tags", match=qm.MatchAny(any=list(tags_exclude))))
    if not must and not must_not:
        return None
    return qm.Filter(must=must or None, must_not=must_not or None)


def _retriever_recency_factor(recorded_at: str | None, half_life_days: float, floor: float, now: datetime) -> float:
    """Soft exponential age decay in [floor, 1.0]. Missing/invalid date → 1.0 (no penalty)."""
    if not recorded_at:
        return 1.0
    try:
        dt = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 1.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    factor = 0.5 ** (age_days / max(half_life_days, 1e-6))
    return max(floor, min(1.0, factor))


def _rrf_merge(
    vector_scored: list[tuple[float, dict]],
    fts_list: list[dict],
    rrf_k: int,
) -> list[tuple[float, dict]]:
    """Reciprocal Rank Fusion of a (sorted) vector candidate list and an FTS candidate list.
    Keyed by (table, chunk id) so cross-table results stay distinct.
    fused = Σ 1/(rrf_k + rank) over the lists the chunk appears in."""
    fused: dict[tuple, list] = {}
    for rank, (_, chunk) in enumerate(vector_scored):
        key = (chunk.get("_table_name"), chunk["id"])
        fused.setdefault(key, [0.0, chunk])[0] += 1.0 / (rrf_k + rank)
    for rank, chunk in enumerate(fts_list):
        key = (chunk.get("_table_name"), chunk["id"])
        if key in fused:
            fused[key][0] += 1.0 / (rrf_k + rank)
        else:
            fused[key] = [1.0 / (rrf_k + rank), chunk]
    merged = sorted(fused.values(), key=lambda x: x[0], reverse=True)
    return [(score, chunk) for score, chunk in merged]


def _url_matches(source_url: str | None, prefix: str) -> bool:
    u = source_url or ""
    return u.startswith(prefix) or prefix in u


_KW_ASCII = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]{1,}")
_KW_KATAKANA = re.compile(r"[ァ-ヶー]{3,}")


def _extract_keywords(*texts: str, limit: int = 8) -> list[str]:
    """Pull salient terms (alphanumeric tokens like 'Qwen3.6'/'MTP', katakana words) from text.

    Used so hybrid FTS matches key terms (OR) instead of requiring the whole conversational
    query string — the original full-string MatchText needs every token present and rarely hits.
    """
    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for raw in _KW_ASCII.findall(text or ""):
            # strip('._-+') can shorten a match (e.g. 'a.' → 'a'); the len(k)<2 re-check then drops
            # single-char-after-strip tokens the regex's min length no longer covers — load-bearing,
            # distinct from the pure-digit filter that follows.
            k = raw.strip("._-+")
            if len(k) < 2 or re.fullmatch(r"[0-9.]+", k):
                continue
            if k.lower() not in seen:
                seen.add(k.lower())
                out.append(k)
        for k in _KW_KATAKANA.findall(text or ""):
            if k not in seen:
                seen.add(k)
                out.append(k)
    return out[:limit]


def retriever_search(
    question: str,
    llm_client: LLMClient,
    embed_model: EmbedModel,
    source_type_filter: str = "all",
    top_k: int = 8,
    distance_threshold: float = 0.3,
    date_from: str | None = None,
    date_to: str | None = None,
    table_name: str | None = None,
    expand_queries: bool = False,
    *,
    hybrid: bool = False,
    rrf_k: int = 60,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_exclude: list[str] | None = None,
    url_prefix: str | None = None,
    recency_decay: bool = False,
    recency_half_life_days: float = 180.0,
    recency_floor: float = 0.5,
) -> list[dict]:
    """
    Search Qdrant, enrich with parent context, update usage stats.
    Returns ranked list of chunk dicts (L2 + L3 deduplicated).

    Pipeline: build filter → vector (+ optional FTS) candidates → optional RRF fuse
    → optional recency decay → top_k → parent enrichment → provenance normalization.

    All ranking-altering options (hybrid / recency_decay) default OFF so behavior is
    unchanged unless explicitly enabled.
    """
    if not question.strip():
        return []
    queries = llm_client.expand_queries(question) if expand_queries else [question]
    logger.info(
        "question='%s' expand=%s queries=%d threshold=%s hybrid=%s recency=%s",
        question[:50],
        expand_queries,
        len(queries),
        distance_threshold,
        hybrid,
        recency_decay,
    )

    target_tables = db_list_tables() if table_name is None else [table_name]
    base_filter = _build_filter(source_type_filter, date_from, date_to, tags_all, tags_any, tags_exclude)
    orig_vector, query_vectors = _encode_queries(embed_model, question, queries)

    # The anchor article-id gate only restricts expanded-query drift, so it's only used when expanding.
    all_scored = _gather_vector_candidates(
        target_tables,
        orig_vector,
        query_vectors,
        top_k,
        distance_threshold,
        base_filter,
        use_anchor_gate=expand_queries,
    )
    all_scored = _apply_url_prefix(all_scored, url_prefix)

    if hybrid:
        ranked = _fuse_hybrid(all_scored, question, queries, target_tables, base_filter, url_prefix, rrf_k, top_k)
    else:
        ranked = all_scored

    if recency_decay:
        ranked = _apply_recency(ranked, recency_half_life_days, recency_floor)

    top_chunks = [chunk for _, chunk in ranked[:top_k]]
    logger.info("top_chunks after merge/fuse: %d", len(top_chunks))

    seen_ids: set[str] = {c["id"] for c in top_chunks}
    enriched = _retriever_enrich_with_parents(top_chunks, seen_ids)
    _retriever_update_usage(enriched)
    _retriever_add_provenance(enriched)

    logger.info("returned %d chunks (incl. parent L2)", len(enriched))
    return enriched


def _encode_queries(
    embed_model: EmbedModel, question: str, queries: list[str]
) -> tuple[list[float], list[list[float]]]:
    """Encode the question + queries in one batch, returning (orig_vector, query_vectors).

    Identical texts are encoded once (when expand=False the question is both anchor and query),
    then mapped back — encoding is deterministic so vectors are identical to a per-text encode.
    """
    all_texts = [question] + queries
    uniq = list(dict.fromkeys(all_texts))
    vec_by_text = dict(zip(uniq, embed_model.encode(uniq, normalize_embeddings=True).tolist()))
    return vec_by_text[question], [vec_by_text[q] for q in queries]


def _gather_vector_candidates(
    target_tables: list[str],
    orig_vector: list[float],
    query_vectors: list[list[float]],
    top_k: int,
    distance_threshold: float,
    base_filter: qm.Filter | None,
    *,
    use_anchor_gate: bool,
) -> list[tuple[float, dict]]:
    """Fan out per-table vector search across threads; merge and sort by score (desc)."""
    all_scored: list[tuple[float, dict]] = []

    def _search_one(tname: str) -> list[tuple[float, dict]]:
        return _retriever_search_table(
            tname, orig_vector, query_vectors, top_k, distance_threshold, base_filter, use_anchor_gate=use_anchor_gate
        )

    max_workers = min(len(target_tables), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_search_one, tname): tname for tname in target_tables}
        for future in as_completed(futures):
            all_scored.extend(future.result())

    all_scored.sort(key=lambda x: x[0], reverse=True)
    return all_scored


def _apply_url_prefix(scored: list[tuple[float, dict]], url_prefix: str | None) -> list[tuple[float, dict]]:
    """Client-side source_url prefix/domain filter (KEYWORD index has no prefix match)."""
    if not url_prefix:
        return scored
    return [(s, c) for s, c in scored if _url_matches(c.get("source_url"), url_prefix)]


def _fuse_hybrid(
    all_scored: list[tuple[float, dict]],
    question: str,
    queries: list[str],
    target_tables: list[str],
    base_filter: qm.Filter | None,
    url_prefix: str | None,
    rrf_k: int,
    top_k: int,
) -> list[tuple[float, dict]]:
    """Fuse vector candidates with keyword (FTS) candidates via RRF.

    FTS matches salient keywords (OR) extracted from the question + expanded queries, not the
    whole conversational string. The url_prefix filter is applied to fts_hits BEFORE the RRF
    merge so rank positions reflect only matching results.
    """
    kw_terms = _extract_keywords(question, *queries)
    logger.info("hybrid fts terms=%s", kw_terms)
    fts_hits: list[dict] = []
    for tname in target_tables:
        fts_hits.extend(
            retriever_keyword_search(
                query=question, terms=kw_terms, top_k=top_k * 5, table_name=tname, base_filter=base_filter
            )
        )
    if url_prefix:
        fts_hits = [c for c in fts_hits if _url_matches(c.get("source_url"), url_prefix)]
    return _rrf_merge(all_scored, fts_hits, rrf_k)


def _apply_recency(ranked: list[tuple[float, dict]], half_life_days: float, floor: float) -> list[tuple[float, dict]]:
    """Multiplicative soft recency decay (after fusion, before top_k), then re-sort by score."""
    now = datetime.now(timezone.utc)
    decayed = [
        (score * _retriever_recency_factor(chunk.get("recorded_at"), half_life_days, floor, now), chunk)
        for score, chunk in ranked
    ]
    decayed.sort(key=lambda x: x[0], reverse=True)
    return decayed


def _retriever_add_provenance(chunks: list[dict]) -> None:
    """Add stable, explicitly-named provenance keys so callers can cite precisely.
    Purely additive — existing keys are untouched."""
    for c in chunks:
        c["chunk_id"] = c.get("id")
        c["section_id"] = c.get("parent_id") if c.get("level") == 3 else c.get("id")
        c["section_heading"] = c.get("heading")
        # position and source_type already come from the payload when present


def retriever_keyword_search(
    query: str | None = None,
    top_k: int = 20,
    table_name: str | None = None,
    base_filter: qm.Filter | None = None,
    terms: list[str] | None = None,
) -> list[dict]:
    """Full-text keyword search using Qdrant multilingual text index.

    terms       → OR-match these salient keywords (min_should ≥1). Preferred for hybrid search.
    query       → fallback whole-string MatchText (every token must be present) when terms is None.
    table_name=None → search all collections, merge results.
    base_filter     → optional metadata filter (tags/date/source_type) merged in.
    Returns chunk dicts with _table_name field.
    Note: Qdrant FTS is boolean match (no relevance score) — order is the rank used by RRF.
    """
    use_terms = [t for t in (terms or []) if t and t.strip()]
    if not use_terms and not (query and query.strip()):
        return []
    target_tables = db_list_tables() if table_name is None else [table_name]
    logger.info("keyword_search terms=%s query='%s' tables=%s", use_terms, (query or "")[:50], target_tables)

    must_extra = list(base_filter.must) if (base_filter and base_filter.must) else []
    must_not_extra = list(base_filter.must_not) if (base_filter and base_filter.must_not) else []

    results: list[dict] = []
    for tname in target_tables:
        try:
            if use_terms:
                flt = qm.Filter(
                    must=must_extra or None,
                    must_not=must_not_extra or None,
                    min_should=qm.MinShould(
                        conditions=[qm.FieldCondition(key="text", match=qm.MatchText(text=t)) for t in use_terms],
                        min_count=1,
                    ),
                )
            else:
                flt = qm.Filter(
                    must=[qm.FieldCondition(key="text", match=qm.MatchText(text=query)), *must_extra],
                    must_not=must_not_extra or None,
                )
            pts, _ = qdrant_client().scroll(
                collection_name=tname,
                scroll_filter=flt,
                limit=top_k,
                with_payload=True,
                with_vectors=False,
            )
            for p in pts:
                d = db_row_to_dict(p)
                d["_table_name"] = tname
                results.append(d)
            logger.info("  table=%s fts hits=%d", tname, len(pts))
        except Exception as exc:
            logger.error("FTS search failed table=%s: %s", tname, exc)

    return results[:top_k]


# ── Internal helpers ──────────────────────────────────────────────────────────


def _retriever_search_table(
    collection: str,
    orig_vector: list[float],
    query_vectors: list[list[float]],
    top_k: int,
    distance_threshold: float,
    flt: qm.Filter | None,
    use_anchor_gate: bool = True,
) -> list[tuple[float, dict]]:
    """
    Vector search for a single collection. Returns scored (score, chunk) pairs (chunks carry _table_name).

    use_anchor_gate=False (expand_queries off): the single query IS the anchor, so the article-id
    gate would be a no-op (the query's top_k hits ⊆ the anchor's top_k*2 hits). Search once —
    avoids embedding and querying the identical vector twice.
    use_anchor_gate=True (expand_queries on): anchor (original question) + expanded queries run in
    parallel; the anchor's article ids gate the expanded results to limit query drift.
    """
    if not use_anchor_gate:
        hits = _retriever_vector_search(
            collection, query_vectors[0], k=top_k, distance_threshold=distance_threshold, flt=flt
        )
        scored: list[tuple[float, dict]] = []
        seen_ids: set[str] = set()
        for rank, row in enumerate(hits):
            cid = row["id"]
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            row["_table_name"] = collection
            scored.append((1.0 / (rank + 1), row))
        return scored

    search_jobs = [(orig_vector, top_k * 2)] + [(v, top_k) for v in query_vectors]

    def _do_search(args: tuple) -> list[dict]:
        vec, k = args
        return _retriever_vector_search(
            collection,
            vec,
            k=k,
            distance_threshold=distance_threshold,
            flt=flt,
        )

    with ThreadPoolExecutor(max_workers=len(search_jobs)) as inner_ex:
        all_results = list(inner_ex.map(_do_search, search_jobs))

    orig_hits = all_results[0]
    anchor_article_ids = {r.get("article_id") for r in orig_hits if r.get("article_id")}
    logger.debug("  table=%s anchor article_ids=%d", collection, len(anchor_article_ids))

    seen_ids: set[str] = set()
    scored: list[tuple[float, dict]] = []

    for i, hits in enumerate(all_results[1:]):
        logger.debug("  table=%s q[%d] → %d hits", collection, i, len(hits))
        for rank, row in enumerate(hits):
            cid = row["id"]
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            score = 1.0 / (rank + 1 + i * top_k)
            row["_table_name"] = collection
            scored.append((score, row))

    if anchor_article_ids:
        scored = [(s, r) for s, r in scored if r.get("article_id") in anchor_article_ids]

    return scored


def _retriever_vector_search(
    collection: str,
    vector: list[float],
    k: int,
    distance_threshold: float,
    flt: qm.Filter | None = None,
) -> list[dict]:
    score_threshold = 1.0 - distance_threshold  # Qdrant: higher score = more similar

    resp = qdrant_client().query_points(
        collection_name=collection,
        query=vector,
        query_filter=flt,
        limit=k,
        score_threshold=score_threshold,
        with_payload=True,
        with_vectors=False,
    )

    rows = []
    for p in resp.points:
        d = db_row_to_dict(p)
        d["_distance"] = 1.0 - p.score  # backward compat: keep _distance convention
        rows.append(d)
    logger.debug("  score_threshold=%.3f → %d hits", score_threshold, len(rows))
    return rows


def _retriever_enrich_with_parents(chunks: list[dict], seen_ids: set[str]) -> list[dict]:
    """For each L3 chunk, also fetch its L2 parent if not already in results.
    Groups by _table_name so each collection is queried separately.
    Each enriched parent inherits the best (lowest) _distance from its L3 children."""
    enriched = list(chunks)

    # Collect parent IDs and compute best _distance per parent from L3 children
    by_table: dict[str, list[str]] = {}
    parent_best_dist: dict[str, float | None] = {}
    for c in chunks:
        if c.get("level") == 3 and c.get("parent_id") and c["parent_id"] not in seen_ids:
            pid = c["parent_id"]
            tname = c.get("_table_name", DEFAULT_TABLE)
            by_table.setdefault(tname, []).append(pid)
            d = c.get("_distance")
            if pid not in parent_best_dist or (
                d is not None and (parent_best_dist[pid] is None or d < parent_best_dist[pid])
            ):
                parent_best_dist[pid] = d

    for tname, pids in by_table.items():
        pts = qdrant_client().retrieve(
            collection_name=tname,
            ids=list(set(pids)),
            with_payload=True,
            with_vectors=False,
        )
        for p in pts:
            d = db_row_to_dict(p)
            d["_table_name"] = tname
            d["_distance"] = parent_best_dist.get(d["id"])  # inherit from best child
            enriched.append(d)
            seen_ids.add(d["id"])

    return enriched


def _retriever_update_usage(enriched: list[dict]) -> None:
    """バックグラウンドスレッドで usage 統計を更新して即 return (fire-and-forget)。"""
    if not enriched:
        return
    by_table: dict[str, list[str]] = {}
    for c in enriched:
        tname = c.get("_table_name", DEFAULT_TABLE)
        by_table.setdefault(tname, []).append(c["id"])

    t = threading.Thread(
        target=_retriever_do_update_usage_multi,
        args=(by_table,),
        daemon=True,
    )
    t.start()


def _retriever_do_update_usage_multi(by_table: dict[str, list[str]]) -> None:
    """Dispatches per-collection usage updates. Called from background thread."""
    for tname, chunk_ids in by_table.items():
        try:
            _retriever_do_update_usage(tname, chunk_ids)
        except Exception as exc:
            logger.error("bg update_usage FAILED table=%s: %s", tname, exc)


def _retriever_do_update_usage(collection: str, chunk_ids: list[str]) -> None:
    """L3/L2 enriched + L2 parent + L1 article をまとめて更新する。
    バックグラウンドスレッドから呼ばれる。例外はログに吐いて握りつぶす。"""
    try:
        client = qdrant_client()
        now = datetime.now(timezone.utc).isoformat()
        ids_list = list(_gather_all_usage_ids(client, collection, chunk_ids))

        current = client.retrieve(
            collection_name=collection,
            ids=ids_list,
            with_payload=["use_count"],
            with_vectors=False,
        )
        client.set_payload(
            collection_name=collection,
            payload={"last_used_at": now},
            points=ids_list,
            wait=False,
        )

        count_groups: dict[int, list] = defaultdict(list)
        for p in current:
            cur = int((p.payload or {}).get("use_count") or 0)
            count_groups[cur + 1].append(p.id)

        for new_count, ids in count_groups.items():
            client.set_payload(
                collection_name=collection,
                payload={"use_count": new_count},
                points=ids,
                wait=False,
            )
    except Exception as exc:
        logger.error("bg update_usage FAILED: %s", exc)


def _gather_all_usage_ids(client, collection: str, chunk_ids: list[str]) -> set[str]:
    """chunk_ids から L2 parent と L1 article を逆引きして全更新対象 ID を返す。"""
    rows = client.retrieve(
        collection_name=collection,
        ids=chunk_ids,
        with_payload=True,
        with_vectors=False,
    )

    all_ids: set[str] = set(chunk_ids)
    article_ids: set[str] = set()
    for p in rows:
        pl = p.payload or {}
        if pl.get("article_id"):
            article_ids.add(pl["article_id"])
        if pl.get("level") == 3 and pl.get("parent_id"):
            all_ids.add(pl["parent_id"])

    if article_ids:
        flt = qm.Filter(
            must=[
                qm.FieldCondition(key="article_id", match=qm.MatchAny(any=list(article_ids))),
                qm.FieldCondition(key="level", match=qm.MatchValue(value=1)),
            ]
        )
        l1_pts, _ = client.scroll(
            collection_name=collection,
            scroll_filter=flt,
            limit=len(article_ids) + 10,
            with_payload=False,
            with_vectors=False,
        )
        for p in l1_pts:
            all_ids.add(str(p.id))

    return all_ids
