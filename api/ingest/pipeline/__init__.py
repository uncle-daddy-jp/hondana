"""Ingestion pipeline: parse → summarise → embed → store → move."""

from ._answer import pipeline_save_answer
from ._append import pipeline_append_to_article
from ._collection import pipeline_ensure_fts_index, pipeline_open_collection
from ._directory import pipeline_ingest_directory
from ._document import pipeline_ingest_document
from ._url import pipeline_ingest_url

__all__ = [
    "pipeline_append_to_article",
    "pipeline_ensure_fts_index",
    "pipeline_ingest_directory",
    "pipeline_ingest_document",
    "pipeline_ingest_url",
    "pipeline_open_collection",
    "pipeline_save_answer",
]
