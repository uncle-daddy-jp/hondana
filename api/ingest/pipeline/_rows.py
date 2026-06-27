"""Row construction, embedding, point conversion, and filesystem move helpers."""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from qdrant_client.http import models as qm

from db import qdrant_client
from hondana_types import EmbedModel, LLMClient

logger = logging.getLogger(__name__)


def _pipeline_build_rows(
    doc,
    created: str,
    llm_client: LLMClient,
    embed_model: EmbedModel,
    cfg: dict,
) -> list[dict]:
    """Build row dicts from a parsed document (LLM summarise + batch embed)."""
    now = datetime.now(timezone.utc).isoformat()
    article_id = str(uuid.uuid4())
    summary_chars = cfg.get("chunk_l1_summary_chars", 200)
    tag_count = cfg.get("chunk_l1_tag_count", 8)
    if cfg.get("enable_llm_summary", True):
        summary, tags = llm_client.summarise(doc.raw_text, summary_chars, tag_count)
    else:
        summary = doc.title
        tags = []

    base = {
        "article_id": article_id,
        "source_type": "article",
        "title": doc.title,
        "source_url": doc.source_url,
        "tags": tags,  # list[str] — stored directly, no JSON encoding
        "content_hash": "",
        "created": created,
        "recorded_at": now,
        "last_used_at": "",
        "use_count": 0,
    }

    skeletons: list[dict] = []
    embed_texts: list[str] = []

    l1_id = str(uuid.uuid4())
    skeletons.append(
        {
            **base,
            "id": l1_id,
            "level": 1,
            "parent_id": "",
            "heading": "__summary__",
            "text": summary,
            "position": 0,
            "content_hash": doc.content_hash,
        }
    )
    embed_texts.append(summary)

    for section in doc.sections:
        l2_id = str(uuid.uuid4())
        l2_text = f"{section.heading}\n{section.text}"
        skeletons.append(
            {
                **base,
                "id": l2_id,
                "level": 2,
                "parent_id": l1_id,
                "heading": section.heading,
                "text": section.text,
                "position": section.position,
            }
        )
        embed_texts.append(l2_text)

        for chunk in section.children:
            skeletons.append(
                {
                    **base,
                    "id": str(uuid.uuid4()),
                    "level": 3,
                    "parent_id": l2_id,
                    "heading": section.heading,
                    "text": chunk.text,
                    "position": chunk.position,
                }
            )
            embed_texts.append(chunk.text)

    return _embed_and_attach(embed_model, skeletons, embed_texts)


def _normalize_tags(raw) -> list[str]:
    """Coerce tags field to list[str] regardless of legacy formats (JSON string / None / int)."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            logger.warning("Failed to parse tags JSON: %r", raw)
    return []


def _rows_to_points(rows: list[dict]) -> list[qm.PointStruct]:
    """Convert row dicts to Qdrant PointStruct objects."""
    pts = []
    for r in rows:
        r = dict(r)  # shallow copy — don't mutate caller's data
        vec = r.pop("vector")
        r["tags"] = _normalize_tags(r.get("tags"))
        pts.append(qm.PointStruct(id=r["id"], vector=vec, payload=r))
    return pts


def _embed_batch(model: EmbedModel, texts: list[str]) -> list[list[float]]:
    return model.encode(texts, normalize_embeddings=True).tolist()


def _embed_and_attach(model: EmbedModel, skeletons: list[dict], embed_texts: list[str]) -> list[dict]:
    """Batch-embed embed_texts and attach each vector to the matching skeleton (same order).

    Shared L1/L2/L3 assembly tail for both the document and the LLM-answer row builders.
    """
    vectors = _embed_batch(model, embed_texts)
    return [{**skeleton, "vector": vector} for skeleton, vector in zip(skeletons, vectors)]


def _extract_date(file_path: Path) -> str:
    mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
    return mtime.strftime("%Y-%m-%d")


def _pipeline_delete_article(collection: str, article_id: str) -> None:
    """Delete all chunks belonging to article_id."""
    qdrant_client().delete(
        collection_name=collection,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                must=[
                    qm.FieldCondition(key="article_id", match=qm.MatchValue(value=article_id)),
                ]
            )
        ),
        wait=True,
    )


def _pipeline_move_to_done(file_path: Path, done_dir: Path) -> None:
    done_dir.mkdir(parents=True, exist_ok=True)
    dest = done_dir / file_path.name
    if dest.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        dest = done_dir / f"{file_path.stem}_{ts}{file_path.suffix}"
    shutil.move(str(file_path), str(dest))
