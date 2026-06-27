"""Pinning tests for _retriever_search_table: expand=False single search vs expand=True anchor gate.

Guards Theme-7: the expand=False path must score identically (1/(rank+1)) and skip the redundant
anchor query; the expand=True path must keep the anchor article-id gate that limits query drift.
"""

from __future__ import annotations

from unittest.mock import patch

import retriever


def _hit(cid, aid):
    return {"id": cid, "article_id": aid, "source_url": "", "_distance": 0.1}


def test_search_table_expand_false_single_search_no_gate():
    hits = [_hit("c1", "a1"), _hit("c2", "a2"), _hit("c3", "a3")]
    with patch.object(retriever, "_retriever_vector_search", return_value=hits) as m:
        scored = retriever._retriever_search_table(
            "col", [0.1], [[0.1]], top_k=3, distance_threshold=0.3, flt=None, use_anchor_gate=False
        )
    assert m.call_count == 1  # single query search; no separate anchor query
    assert [c["id"] for _, c in scored] == ["c1", "c2", "c3"]
    assert scored[0][0] == 1.0 and scored[1][0] == 0.5
    assert all(c["_table_name"] == "col" for _, c in scored)


def test_search_table_expand_true_anchor_gate_restricts_to_anchor_articles():
    anchor = [_hit("c1", "a1"), _hit("c2", "a2")]
    qhits = [_hit("c1", "a1"), _hit("c9", "a9")]  # a9 is NOT in the anchor → gated out

    def fake(collection, vector, k, distance_threshold, flt=None):
        return anchor if k > 2 else qhits  # anchor job uses k = top_k*2

    with patch.object(retriever, "_retriever_vector_search", side_effect=fake) as m:
        scored = retriever._retriever_search_table(
            "col", [0.1], [[0.2]], top_k=2, distance_threshold=0.3, flt=None, use_anchor_gate=True
        )
    ids = [c["id"] for _, c in scored]
    assert "c1" in ids and "c9" not in ids
    assert m.call_count == 2  # anchor + 1 expanded query


def test_encode_queries_dedupes_identical_texts():
    captured = {}

    class _Arr(list):
        def tolist(self):
            return list(self)

    class _Embed:
        def encode(self, texts, *, normalize_embeddings=False):
            captured["texts"] = list(texts)
            return _Arr([[float(i)] for i in range(len(texts))])

    orig, qs = retriever._encode_queries(_Embed(), "same", ["same"])
    # expand=False sends the question as both anchor and query → encode the unique text once
    assert captured["texts"] == ["same"]
    assert orig == qs[0]
