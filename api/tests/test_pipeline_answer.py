"""Pinning tests for the LLM-answer ingest path: save_answer MCP contract + row structure.

Guards Theme-6 consolidation: overwrite=article_id維持 / skip=既存ID返却 / source_type=llm_answer.
"""

from __future__ import annotations

from unittest.mock import patch

import ingest.pipeline._answer as answer_mod
from ingest.pipeline._answer import _pipeline_build_answer_rows, pipeline_save_answer


class _FakeArr(list):
    def tolist(self):
        return list(self)


class _FakeEmbed:
    """Deterministic embed model: a distinct 1-d vector per input text (vector i == [float(i)])."""

    def encode(self, texts, *, normalize_embeddings=False):
        return _FakeArr([[float(i)] for i in range(len(texts))])

    def get_sentence_embedding_dimension(self):
        return 1


_BASE = {
    "article_id": "A1",
    "source_type": "llm_answer",
    "title": "Q?",
    "source_url": "",
    "tags": [],
    "content_hash": "",
    "created": "2026-01-01",
    "recorded_at": "2026-01-01T00:00:00+00:00",
    "last_used_at": "",
    "use_count": 0,
}


def test_build_answer_rows_structure_and_vector_order():
    rows = _pipeline_build_answer_rows(
        base=dict(_BASE),
        answer="This is the answer body. " * 5,
        summary="short summary",
        l1_id="L1",
        l2_id="L2",
        max_chars=500,
        overlap=100,
        embed_model=_FakeEmbed(),
    )
    l1, l2, l3s = rows[0], rows[1], rows[2:]

    assert l1["level"] == 1 and l1["heading"] == "__summary__" and l1["parent_id"] == ""
    assert l1["text"] == "short summary" and l1["position"] == 0
    assert l1["source_type"] == "llm_answer"
    assert l2["level"] == 2 and l2["heading"] == "Answer" and l2["parent_id"] == "L1"
    assert l2["text"].startswith("This is the answer body.")
    for pos, r in enumerate(l3s):
        assert r["level"] == 3 and r["parent_id"] == "L2" and r["heading"] == "Answer" and r["position"] == pos
    # vectors attached in embed order: [summary, answer[:1000], l3...] → L1=v0, L2=v1
    assert all("vector" in r for r in rows)
    assert l1["vector"] == [0.0] and l2["vector"] == [1.0]


def _patched_save(*, existing=None, **kwargs):
    """Run pipeline_save_answer with collection/dup/qdrant/delete mocked."""
    with (
        patch.object(answer_mod, "_ensure_collection", return_value="col"),
        patch.object(answer_mod, "qdrant_client") as mock_qc,
        patch.object(answer_mod, "_pipeline_delete_article") as mock_del,
        patch.object(
            answer_mod, "_pipeline_check_duplicate", return_value={"existing_id": existing, "same_hash": False}
        ),
    ):
        article_id = pipeline_save_answer("Q?", "the answer", _FakeEmbed(), {}, table_name="col", **kwargs)
        return article_id, mock_qc, mock_del


def test_save_answer_skip_returns_existing_id_without_upsert():
    article_id, mock_qc, mock_del = _patched_save(duplicate_action="skip", existing="E1")
    assert article_id == "E1"
    mock_qc.return_value.upsert.assert_not_called()
    mock_del.assert_not_called()


def test_save_answer_overwrite_preserves_article_id_and_deletes_old():
    article_id, mock_qc, mock_del = _patched_save(duplicate_action="overwrite", existing="E1")
    assert article_id == "E1"  # overwrite keeps the existing article_id
    mock_del.assert_called_once()
    mock_qc.return_value.upsert.assert_called_once()


def test_save_answer_new_creates_fresh_id():
    article_id, mock_qc, mock_del = _patched_save(duplicate_action="new")
    assert article_id and article_id != "E1"
    mock_del.assert_not_called()
    mock_qc.return_value.upsert.assert_called_once()
