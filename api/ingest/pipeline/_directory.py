"""Batch ingest of a directory tree containing supported source files."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from constants import DEFAULT_TABLE, SUPPORTED_EXTENSIONS
from db import qdrant_client
from hondana_types import EmbedModel, LLMClient

from ..chunker import chunker_parse
from ._collection import _ensure_collection
from ._document import pipeline_ingest_document
from ._duplicate import _pipeline_file_is_unchanged
from ._rows import _extract_date, _pipeline_move_to_done

logger = logging.getLogger(__name__)


def pipeline_ingest_directory(
    inbox_dir: Path,
    done_dir: Path,
    llm_client: LLMClient,
    embed_model: EmbedModel,
    cfg: dict,
    table_name: str = DEFAULT_TABLE,
) -> dict:
    """
    Process all supported files in inbox_dir.
    Returns {"processed": [...], "skipped": [...]}.
    """
    excluded_dirs: set[str] = {str(done_dir)}
    for child in inbox_dir.iterdir():
        if child.is_dir():
            try:
                if os.path.samefile(child, done_dir):
                    excluded_dirs.add(str(child))
            except OSError:
                pass

    files = [
        f
        for f in inbox_dir.rglob("*")
        if f.suffix.lower() in SUPPORTED_EXTENSIONS
        and f.is_file()
        and not any(str(f).startswith(excl + os.sep) or str(f).startswith(excl + "/") for excl in excluded_dirs)
    ]

    if not files:
        return {"processed": [], "skipped": []}

    embed_dim = embed_model.get_sentence_embedding_dimension()
    collection = _ensure_collection(qdrant_client(), embed_dim, table_name)
    processed = []
    skipped = []

    for file_path in files:
        try:
            file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc).isoformat()
            if _pipeline_file_is_unchanged(collection, file_path.name, file_mtime):
                skipped.append(file_path.name)
                continue
            doc = chunker_parse(file_path)
            created = _extract_date(file_path)
            result = pipeline_ingest_document(
                doc=doc,
                created=created,
                llm_client=llm_client,
                embed_model=embed_model,
                cfg=cfg,
                collection=collection,
                duplicate_action="overwrite",
                on_changed="overwrite",
            )
            if result["status"] == "skipped":
                skipped.append(file_path.name)
            else:
                _pipeline_move_to_done(file_path, done_dir)
                processed.append(file_path.name)
        except Exception as e:
            logger.error("ERROR processing %s: %s", file_path.name, e)

    return {"processed": processed, "skipped": skipped}
