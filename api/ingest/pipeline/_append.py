"""pipeline_append_to_article — 既存記事に追記してサマリーを再生成する。"""

from __future__ import annotations

import logging

from constants import DEFAULT_TABLE
from db import qdrant_client
from hondana_types import EmbedModel, LLMClient
from ingest.chunker._dispatch import chunker_parse_text
from manager import manager_get_article_chunks

from ._rows import _pipeline_build_rows, _pipeline_delete_article, _rows_to_points

logger = logging.getLogger(__name__)

_SYSTEM_HEADINGS = {"__summary__", "__intro__"}


def pipeline_append_to_article(
    article_id: str,
    append_text: str,
    llm_client: LLMClient,
    embed_model: EmbedModel,
    cfg: dict,
    table_name: str = DEFAULT_TABLE,
) -> dict:
    """
    既存記事に追記テキストを加え、L1サマリーを再生成して同じ article_id で保存し直す。

    Returns {"status": "updated", "article_id": str, "chunk_count": int}
    """
    chunks = manager_get_article_chunks(article_id, table_name)
    if not chunks:
        raise ValueError(f"Article not found: {article_id}")

    l1 = next((c for c in chunks if c.get("level") == 1), None)
    if not l1:
        raise ValueError(f"L1 chunk not found for article: {article_id}")

    title = l1.get("title", "")
    source_url = l1.get("source_url", "")
    created = l1.get("created", "")

    # L2 チャンクから元テキストを再構築（セクション順を保つ）
    l2_chunks = [c for c in chunks if c.get("level") == 2]
    parts: list[str] = []
    for l2 in l2_chunks:
        heading = l2.get("heading", "")
        text = l2.get("text", "")
        if not text.strip():
            continue
        if heading and heading not in _SYSTEM_HEADINGS:
            parts.append(f"## {heading}\n\n{text}")
        else:
            parts.append(text)

    existing_text = "\n\n".join(parts)
    combined_text = f"{existing_text}\n\n{append_text.strip()}"

    doc = chunker_parse_text(title=title, text=combined_text, source_url=source_url)

    rows = _pipeline_build_rows(doc, created, llm_client, embed_model, cfg)

    # 生成された article_id を元の ID に差し替える
    for row in rows:
        row["article_id"] = article_id

    _pipeline_delete_article(table_name, article_id)
    qdrant_client().upsert(
        collection_name=table_name,
        points=_rows_to_points(rows),
        wait=True,
    )

    logger.info("append_to_article: article_id=%s chunks=%d table=%s", article_id, len(rows), table_name)
    return {"status": "updated", "article_id": article_id, "chunk_count": len(rows)}
