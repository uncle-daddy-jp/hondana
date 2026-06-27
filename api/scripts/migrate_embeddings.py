#!/usr/bin/env python3
"""
migrate_embeddings.py — Re-embed all Qdrant points with the configured embedding model.

Does NOT re-fetch URLs or call an LLM. Only recomputes vectors from stored text payloads.
Truncation/normalization match live ingest (shared via ingest.embed_client.embed_texts), and
the collection/index setup is shared with ingest.pipeline._collection.ensure_indexes.

Usage (inside the api container, or anywhere QDRANT_URL / EMBED_URL are set):
  EMBED_URL=http://your-gpu-host:8001 EMBED_MODEL=bge-m3 EMBED_DIM=1024 \
    python scripts/migrate_embeddings.py [--dry-run] [--batch 64] [--collections a b]

EMBED_URL is the base endpoint (no trailing /v1/embeddings) — same as config.yml embedding_url.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import requests
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

# Make the flat api root importable when run as `python scripts/migrate_embeddings.py`
# (Python only puts the script's own dir on sys.path, not the api root above it).
_API_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _API_ROOT not in sys.path:
    sys.path.insert(0, _API_ROOT)

from ingest.embed_client import EMBED_MAX_CHARS, embed_texts  # noqa: E402  (after sys.path bootstrap)
from ingest.pipeline._collection import ensure_indexes  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8001")  # base URL (no /v1/embeddings)
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))
SCROLL_SIZE = 200

_ZERO_VEC = [0.0] * EMBED_DIM


# ── Embedding ─────────────────────────────────────────────────────────────────


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch via the shared embed_texts transport, with batch-split + zero-vec fallback."""
    try:
        arr = embed_texts(EMBED_URL, EMBED_MODEL, texts, normalize=True, max_chars=EMBED_MAX_CHARS, strip=True)
        return arr.tolist()
    except requests.HTTPError as exc:
        if len(texts) == 1:
            logger.warning("Embed failed for single text (len=%d), using zero vec: %s", len(texts[0]), exc)
            return [_ZERO_VEC]
        # Split the batch in half and retry (handles per-item size/content 4xx).
        mid = len(texts) // 2
        logger.warning("Batch error (size=%d), splitting: %s", len(texts), exc)
        return embed_batch(texts[:mid]) + embed_batch(texts[mid:])


# ── Scroll helpers ────────────────────────────────────────────────────────────


def scroll_all(client: QdrantClient, collection: str) -> list:
    """Return all points with payload (no vectors)."""
    points, offset = [], None
    while True:
        batch, next_offset = client.scroll(
            collection_name=collection,
            limit=SCROLL_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(batch)
        if next_offset is None:
            break
        offset = next_offset
    return points


# ── Migration ─────────────────────────────────────────────────────────────────


def _ensure_target_collection(client: QdrantClient, name: str) -> None:
    """Recreate the collection unless it already has the correct EMBED_DIM, then (re)build indexes."""
    existing = {c.name: c for c in client.get_collections().collections}
    needs_recreate = True
    if name in existing:
        info = client.get_collection(name)
        current_dim = info.config.params.vectors.size
        if current_dim == EMBED_DIM:
            logger.info("  Collection exists with correct dim=%d, upserting in-place", EMBED_DIM)
            needs_recreate = False
        else:
            logger.info("  Dim mismatch (%d->%d), recreating", current_dim, EMBED_DIM)

    if needs_recreate:
        logger.info("  Deleting and recreating collection with dim=%d ...", EMBED_DIM)
        client.delete_collection(name)
        client.create_collection(
            collection_name=name,
            vectors_config=qm.VectorParams(size=EMBED_DIM, distance=qm.Distance.COSINE),
            hnsw_config=qm.HnswConfigDiff(m=16, ef_construct=128),
        )

    ensure_indexes(client, name)


def _reembed_points(client: QdrantClient, name: str, points: list, batch_size: int) -> None:
    """Re-embed stored text payloads in batches and upsert with original ids/payloads."""
    total = len(points)
    inserted = 0
    for i in range(0, total, batch_size):
        chunk = points[i : i + batch_size]
        texts = [p.payload.get("text", "") or "" for p in chunk]
        vectors = embed_batch(texts)
        structs = [qm.PointStruct(id=p.id, vector=v, payload=p.payload) for p, v in zip(chunk, vectors)]
        client.upsert(collection_name=name, points=structs, wait=True)
        inserted += len(structs)
        logger.info("  [%s] %d / %d", name, inserted, total)
    logger.info("  Done: %d points re-embedded", inserted)


def migrate_collection(client: QdrantClient, name: str, batch_size: int, dry_run: bool) -> None:
    logger.info("=== %s ===", name)
    points = scroll_all(client, name)
    logger.info("  %d points loaded", len(points))
    if dry_run:
        logger.info("  [dry-run] skip")
        return
    _ensure_target_collection(client, name)
    _reembed_points(client, name, points, batch_size)


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-embed Qdrant collections with the configured model")
    parser.add_argument("--dry-run", action="store_true", help="List collections without modifying")
    parser.add_argument("--batch", type=int, default=64, help="Embedding batch size (default 64)")
    parser.add_argument("--collections", nargs="*", help="Collections to migrate (default: all)")
    args = parser.parse_args()

    client = QdrantClient(url=QDRANT_URL)
    all_collections = [c.name for c in client.get_collections().collections]
    targets = args.collections or all_collections

    logger.info("Qdrant: %s", QDRANT_URL)
    logger.info("Embed:  %s  model=%s  dim=%d", EMBED_URL, EMBED_MODEL, EMBED_DIM)
    logger.info("Collections to migrate: %s", targets)
    if args.dry_run:
        logger.info("[DRY RUN]")

    for name in targets:
        if name not in all_collections:
            logger.warning("Collection %r not found, skipping", name)
            continue
        t0 = time.monotonic()
        migrate_collection(client, name, args.batch, args.dry_run)
        logger.info("  Elapsed: %.1fs", time.monotonic() - t0)

    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
