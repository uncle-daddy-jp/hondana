"""Qdrant collection setup and payload/FTS index management."""

from __future__ import annotations

import logging

from qdrant_client.http import models as qm

from constants import DEFAULT_TABLE, EMBED_DIM_DEFAULT
from db import qdrant_client

logger = logging.getLogger(__name__)


# ── Payload indexes ───────────────────────────────────────────────────────────

_PAYLOAD_INDEXES = [
    ("level", qm.PayloadSchemaType.INTEGER),
    ("source_type", qm.PayloadSchemaType.KEYWORD),
    ("article_id", qm.PayloadSchemaType.KEYWORD),
    ("parent_id", qm.PayloadSchemaType.KEYWORD),
    ("source_url", qm.PayloadSchemaType.KEYWORD),
    ("title", qm.PayloadSchemaType.KEYWORD),
    ("content_hash", qm.PayloadSchemaType.KEYWORD),
    ("recorded_at", qm.PayloadSchemaType.DATETIME),
    ("tags", qm.PayloadSchemaType.KEYWORD),
]


def pipeline_open_collection(
    embed_dim: int = EMBED_DIM_DEFAULT,
    collection: str = DEFAULT_TABLE,
) -> str:
    """Open (or create) a Qdrant collection. Returns collection name."""
    _ensure_collection(qdrant_client(), embed_dim, collection)
    return collection


def _ensure_collection(client, embed_dim: int, name: str) -> str:
    """Create Qdrant collection with HNSW + payload indexes if it doesn't exist. Returns name."""
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            collection_name=name,
            vectors_config=qm.VectorParams(size=embed_dim, distance=qm.Distance.COSINE),
            hnsw_config=qm.HnswConfigDiff(m=16, ef_construct=128),
            on_disk_payload=False,
        )
    ensure_indexes(client, name)
    return name


def ensure_indexes(client, name: str) -> None:
    """Create payload + multilingual FTS indexes on `name` using the GIVEN client.

    Idempotent best-effort (already-existing indexes raise and are logged+skipped). Threads the
    passed client so callers with their own connection (e.g. the re-embed migration script) hit
    the right instance — do NOT route through pipeline_ensure_fts_index, which uses the db singleton.
    """
    for fn, fs in _PAYLOAD_INDEXES:
        try:
            client.create_payload_index(collection_name=name, field_name=fn, field_schema=fs)
        except Exception as exc:
            logger.warning("Qdrant payload index creation skipped for %s.%s: %s", name, fn, exc)
    try:
        client.create_payload_index(
            collection_name=name,
            field_name="text",
            field_schema=qm.TextIndexParams(
                type="text",
                tokenizer=qm.TokenizerType.MULTILINGUAL,
                min_token_len=2,
                max_token_len=20,
            ),
        )
    except Exception as exc:
        logger.warning("FTS index skipped (%s): %s", name, exc)


def pipeline_ensure_fts_index(collection: str) -> None:
    """Create multilingual FTS index on 'text' field. Idempotent — safe to call repeatedly."""
    try:
        qdrant_client().create_payload_index(
            collection_name=collection,
            field_name="text",
            field_schema=qm.TextIndexParams(
                type="text",
                tokenizer=qm.TokenizerType.MULTILINGUAL,
                min_token_len=2,
                max_token_len=20,
            ),
        )
    except Exception as exc:
        logger.warning("FTS index skipped (%s): %s", collection, exc)
