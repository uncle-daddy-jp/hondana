"""Unit tests for the retriever helpers added for hybrid search, recency, filters, provenance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from retriever import (
    _build_filter,
    _extract_keywords,
    _retriever_add_provenance,
    _retriever_recency_factor,
    _rrf_merge,
    retriever_resolve_date_filter,
)


def test_extract_keywords_picks_salient_terms():
    kws = _extract_keywords("Qwen3.6のtpsを上げるために何をやったらいい？", "MTP / スループット を上げる vLLM")
    low = [k.lower() for k in kws]
    assert "qwen3.6" in low  # alphanumeric model name kept
    assert "tps" in low and "mtp" in low and "vllm" in low
    assert "スループット" in kws  # katakana term kept
    # particles / pure-digit noise dropped
    assert "を" not in kws and "3.6" not in kws


def test_build_filter_empty_returns_none():
    assert _build_filter() is None
    assert _build_filter(source_type="all") is None


def test_build_filter_tags_all_and_exclude():
    flt = _build_filter(tags_all=["a", "b"], tags_exclude=["x"])
    assert flt is not None
    # tags_all → two separate must conditions (AND)
    tag_musts = [c for c in (flt.must or []) if getattr(c, "key", None) == "tags"]
    assert len(tag_musts) == 2
    # tags_exclude → must_not
    assert flt.must_not and len(flt.must_not) == 1


def test_build_filter_tags_any_single_condition():
    flt = _build_filter(tags_any=["a", "b", "c"])
    tag_musts = [c for c in (flt.must or []) if getattr(c, "key", None) == "tags"]
    assert len(tag_musts) == 1  # MatchAny is one condition


def test_recency_factor_fresh_old_and_missing():
    now = datetime(2026, 6, 22, tzinfo=timezone.utc)
    fresh = now.isoformat()
    old = (now - timedelta(days=360)).isoformat()
    assert _retriever_recency_factor(fresh, 180, 0.0, now) > 0.99
    # 360 days at half_life 180 → 0.5**2 = 0.25
    assert abs(_retriever_recency_factor(old, 180, 0.0, now) - 0.25) < 0.02
    # floor clamps it
    assert _retriever_recency_factor(old, 180, 0.5, now) == 0.5
    # missing / invalid → no penalty
    assert _retriever_recency_factor("", 180, 0.5, now) == 1.0
    assert _retriever_recency_factor(None, 180, 0.5, now) == 1.0


def test_rrf_merge_boosts_chunks_in_both_lists():
    a = {"id": "a", "_table_name": "t"}
    b = {"id": "b", "_table_name": "t"}
    c = {"id": "c", "_table_name": "t"}
    vector = [(0.9, a), (0.8, b)]  # a, b from vector
    fts = [c, a]  # c, a from keyword
    merged = _rrf_merge(vector, fts, rrf_k=60)
    ids = [chunk["id"] for _, chunk in merged]
    # 'a' appears in both lists → should rank first
    assert ids[0] == "a"
    assert set(ids) == {"a", "b", "c"}


def test_resolve_date_last_n_days():
    today = datetime.now(timezone.utc).date()
    date_from, date_to = retriever_resolve_date_filter(None, None, None, last_n_days=7)
    assert date_to == str(today)
    assert date_from == str(today - timedelta(days=6))
    # last_n_days takes priority over preset
    df, dt = retriever_resolve_date_filter("year", None, None, last_n_days=1)
    assert df == str(today) and dt == str(today)


def test_add_provenance_keys():
    chunks = [
        {"id": "l3", "level": 3, "parent_id": "l2", "heading": "Sec"},
        {"id": "l2", "level": 2, "parent_id": "l1", "heading": "Sec"},
    ]
    _retriever_add_provenance(chunks)
    l3, l2 = chunks
    assert l3["chunk_id"] == "l3" and l3["section_id"] == "l2" and l3["section_heading"] == "Sec"
    assert l2["chunk_id"] == "l2" and l2["section_id"] == "l2"  # L2's section_id is itself
