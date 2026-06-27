"""Main ingest logic for a single parsed document."""

from __future__ import annotations

import logging

from constants import MIN_TEXT_LENGTH
from db import qdrant_client
from hondana_types import EmbedModel, LLMClient

from ._duplicate import _pipeline_check_duplicate
from ._rows import _pipeline_build_rows, _pipeline_delete_article, _rows_to_points

logger = logging.getLogger(__name__)


def pipeline_ingest_document(
    doc,
    created: str,
    llm_client: LLMClient,
    embed_model: EmbedModel,
    cfg: dict,
    collection: str,
    duplicate_action: str = "overwrite",  # "overwrite" | "skip" | "new"
    on_changed: str = "overwrite",  # "overwrite" | "new" (when hash differs)
) -> dict:
    """
    Insert a parsed document into Qdrant with duplicate handling.

    重複チェックは高コスト処理(LLM/埋め込み)の前に1回だけ実施し、同一 content_hash なら全 action で
    スキップする。Qdrant がサーバー側で書き込みを直列化するためプロセス内ロックは不要。

    Returns {"status": "inserted" | "overwritten" | "skipped", "article_id": str}.
    """
    if len(doc.raw_text.strip()) < MIN_TEXT_LENGTH:
        logger.info("SKIP (too short: %d chars): %r", len(doc.raw_text.strip()), doc.title)
        return {"status": "skipped", "article_id": ""}

    # 重複チェックは高コスト処理の前に1回だけ（旧コードの phase-0/phase-2 二重チェックを統合）。
    dup = _pipeline_check_duplicate(collection, doc.source_url, doc.title, doc.content_hash)

    # 変更なし（同一 content_hash）なら全 action で LLM/埋め込みごとスキップ
    if dup["existing_id"] and dup["same_hash"]:
        logger.info("SKIP (unchanged): %r", doc.title)
        return {"status": "skipped", "article_id": dup["existing_id"]}

    # 高コスト処理（LLM 要約 + ベクトル生成）
    rows = _pipeline_build_rows(doc, created, llm_client, embed_model, cfg)

    # 既存ありなら action に応じて旧記事を削除/保持
    do_delete = False
    if dup["existing_id"]:
        if duplicate_action == "skip":
            logger.info("SKIP (duplicate): %r", doc.title)
            return {"status": "skipped", "article_id": dup["existing_id"]}
        if duplicate_action == "new" or on_changed == "new":
            logger.info("NEW (keep old): %r", doc.title)
        else:
            logger.info("OVERWRITE (changed): %r", doc.title)
            do_delete = True

    if do_delete:
        _pipeline_delete_article(collection, dup["existing_id"])

    qdrant_client().upsert(
        collection_name=collection,
        points=_rows_to_points(rows),
        wait=True,
    )

    article_id = rows[0]["article_id"]
    status = "overwritten" if do_delete else "inserted"
    return {"status": status, "article_id": article_id}
