"""Fetch a URL and ingest its content synchronously."""

from __future__ import annotations

import logging

from constants import DEFAULT_TABLE
from db import qdrant_client
from hondana_types import EmbedModel, LLMClient

from ..chunker import chunker_fetch_url
from ._collection import _ensure_collection
from ._document import pipeline_ingest_document

logger = logging.getLogger(__name__)


def pipeline_ingest_url(
    url: str,
    llm_client: LLMClient,
    embed_model: EmbedModel,
    cfg: dict,
    table_name: str = DEFAULT_TABLE,
    duplicate_action: str = "overwrite",
    on_changed: str = "overwrite",
) -> dict:
    """Fetch a URL and ingest it synchronously. Playwright fallback included.

    Returns {"status": "inserted" | "overwritten" | "skipped", "article_id": str}.
    """
    from datetime import date

    doc, suffix, binary = chunker_fetch_url(url)
    if binary is not None:
        raise ValueError(f"URL returned binary content ({suffix}): {url}")

    embed_dim = embed_model.get_sentence_embedding_dimension()
    collection = _ensure_collection(qdrant_client(), embed_dim, table_name)
    created = date.today().isoformat()

    return pipeline_ingest_document(
        doc=doc,
        created=created,
        llm_client=llm_client,
        embed_model=embed_model,
        cfg=cfg,
        collection=collection,
        duplicate_action=duplicate_action,
        on_changed=on_changed,
    )
