"""Store an LLM answer as L1/L2/L3 chunks in Qdrant."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from constants import DEFAULT_TABLE
from db import qdrant_client
from hondana_types import EmbedModel

from ..chunker import _build_l3_chunks
from ._collection import _ensure_collection
from ._duplicate import _pipeline_check_duplicate
from ._rows import _embed_and_attach, _pipeline_delete_article, _rows_to_points

logger = logging.getLogger(__name__)


def pipeline_save_answer(
    question: str,
    answer: str,
    embed_model: EmbedModel,
    cfg: dict,
    table_name: str = DEFAULT_TABLE,
    duplicate_action: str = "new",
    agent_id: str | None = None,
    origin: str | None = None,
) -> str:
    """Store an LLM answer as L1/L2/L3 chunks in Qdrant. Returns article_id.

    duplicate_action (keyed on the question/title):
      "new"       → always create a fresh article (default; backward-compatible)
      "skip"      → if an answer with the same question exists, return it unchanged
      "overwrite" → replace the existing answer in place (same article_id)
    agent_id / origin → optional provenance stored on every chunk's payload.
    """
    now = datetime.now(timezone.utc).isoformat()
    max_chars = cfg.get("chunk_l3_max_chars", 500)
    overlap = cfg.get("chunk_l3_overlap_chars", 100)
    summary_chars = cfg.get("chunk_l1_summary_chars", 200)

    title = question[:120]
    embed_dim = embed_model.get_sentence_embedding_dimension()
    collection = _ensure_collection(qdrant_client(), embed_dim, table_name)

    existing_id: str | None = None
    if duplicate_action in ("skip", "overwrite"):
        # content_hash="" / source_url="" keep the url/hash branches no-ops → dedup keyed on exact title.
        existing_id = _pipeline_check_duplicate(collection, "", title, "")["existing_id"]
    if existing_id and duplicate_action == "skip":
        logger.info("save_answer: skip duplicate title=%r id=%s", title[:40], existing_id)
        return existing_id

    article_id = existing_id if (existing_id and duplicate_action == "overwrite") else str(uuid.uuid4())
    l1_id = str(uuid.uuid4())
    l2_id = str(uuid.uuid4())
    summary = answer[:summary_chars]

    base = {
        "article_id": article_id,
        "source_type": "llm_answer",
        "title": title,
        "source_url": "",
        "tags": [],
        "content_hash": "",
        "created": now[:10],
        "recorded_at": now,
        "last_used_at": "",
        "use_count": 0,
    }
    if agent_id:
        base["agent_id"] = agent_id
    if origin:
        base["origin"] = origin

    rows = _pipeline_build_answer_rows(
        base=base,
        answer=answer,
        summary=summary,
        l1_id=l1_id,
        l2_id=l2_id,
        max_chars=max_chars,
        overlap=overlap,
        embed_model=embed_model,
    )

    if existing_id and duplicate_action == "overwrite":
        _pipeline_delete_article(collection, existing_id)

    qdrant_client().upsert(
        collection_name=collection,
        points=_rows_to_points(rows),
        wait=True,
    )
    return article_id


def _pipeline_build_answer_rows(
    base: dict,
    answer: str,
    summary: str,
    l1_id: str,
    l2_id: str,
    max_chars: int,
    overlap: int,
    embed_model,
) -> list[dict]:
    """Build L1/L2/L3 rows (with vectors) for a saved LLM answer."""
    l3_chunks = _build_l3_chunks(answer, max_chars=max_chars, overlap=overlap)
    skeletons: list[dict] = [
        {**base, "id": l1_id, "level": 1, "parent_id": "", "heading": "__summary__", "text": summary, "position": 0},
        {**base, "id": l2_id, "level": 2, "parent_id": l1_id, "heading": "Answer", "text": answer, "position": 0},
    ]
    embed_texts = [summary, answer[:1000]]
    for i, c in enumerate(l3_chunks):
        skeletons.append(
            {
                **base,
                "id": str(uuid.uuid4()),
                "level": 3,
                "parent_id": l2_id,
                "heading": "Answer",
                "text": c.text,
                "position": i,
            }
        )
        embed_texts.append(c.text)
    return _embed_and_attach(embed_model, skeletons, embed_texts)
