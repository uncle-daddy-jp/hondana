"""job_handler.py — Dispatch queued jobs to the appropriate ingest pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from db import DEFAULT_TABLE
from ingest.chunker import chunker_fetch_url, chunker_parse, chunker_parse_text
from ingest.pipeline import pipeline_ingest_directory, pipeline_ingest_document, pipeline_open_collection

logger = logging.getLogger(__name__)


async def job_handler(job_type: str, payload: dict) -> dict:
    """Dispatch queued jobs to the appropriate ingest function (runs in asyncio thread pool)."""
    # Imported lazily to avoid a circular import with main.py at module load time.
    from main import state

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ジョブ単位の cfg: グローバル設定を基に payload のオーバーライドを適用
    job_cfg = {**state.cfg}
    if "enable_llm_summary" in payload:
        job_cfg["enable_llm_summary"] = payload["enable_llm_summary"]

    table = payload.get("table", DEFAULT_TABLE)
    extra: dict = {}  # job-type-specific fields appended to the return value

    if job_type == "ingest_url":
        url = payload["url"]
        doc, suffix, binary_bytes = await asyncio.to_thread(chunker_fetch_url, url)

        if suffix is not None:
            # バイナリ(PDF/DOCX等): inbox_dir に保存して directory pipeline に委譲
            filename = Path(urlparse(url).path).name or f"downloaded{suffix}"
            dest = state.inbox_dir / filename
            if dest.exists():
                ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                dest = state.inbox_dir / f"{dest.stem}_{ts}{suffix}"
            dest.write_bytes(binary_bytes)
            result = await asyncio.to_thread(
                pipeline_ingest_directory,
                state.inbox_dir,
                state.done_dir,
                state.llm,
                state.embed,
                job_cfg,
                table_name=table,
            )
            return {"url": url, "processed": result["processed"], "skipped": result["skipped"]}

        extra["raw_text"] = (doc.raw_text or "")[:6000]

    elif job_type == "ingest_text":
        doc = await asyncio.to_thread(
            chunker_parse_text,
            payload["title"],
            payload["text"],
            payload.get("source_url", ""),
        )

    elif job_type == "ingest_file":
        doc = await asyncio.to_thread(chunker_parse, Path(payload["tmp_path"]))
        extra = {"filename": payload.get("filename", "")}

    else:
        raise ValueError(f"Unknown job type: {job_type}")

    # 共通: コレクションを開いてドキュメントを取り込む
    collection = pipeline_open_collection(state.embed.get_sentence_embedding_dimension(), table)
    result = await asyncio.to_thread(
        pipeline_ingest_document,
        doc,
        today,
        state.llm,
        state.embed,
        job_cfg,
        collection,
        payload.get("duplicate_action", "overwrite"),
        payload.get("on_changed", "overwrite"),
    )
    article_id = result.get("article_id", "")
    base_url = os.getenv("HONDANA_PUBLIC_URL", "http://localhost:8200")
    view_url = f"{base_url}/k/{article_id}" if article_id else ""
    return {"title": doc.title, **extra, **result, "view_url": view_url}
