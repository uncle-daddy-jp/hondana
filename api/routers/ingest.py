"""
routers/ingest.py — Ingest API endpoints.

  POST /api/ingest/url     body: {url, duplicate_action?, on_changed?, table?}
                           → {"job_id": "...", "status": "pending"}  (async)
  POST /api/ingest/urls    body: {urls: [...], duplicate_action?, on_changed?, table?}
                           → {"results": [...], "accepted": N, "skipped": N}  (async, batch ≤50)
  POST /api/ingest/text    body: {title, text, source_url?, duplicate_action?, on_changed?, table?}
                           → {"job_id": "...", "status": "pending"}  (async, persisted)
  POST /api/ingest/file    multipart: file + metadata fields
                           → {"job_id": "...", "status": "pending"}  (async)

All endpoints enqueue background jobs and return immediately.
URL and text jobs are persisted to SQLite and survive server restarts.
File jobs are not persisted — the uploaded temp file is the implicit record.

バッチエンドポイント /urls の上限: INGEST_URLS_MAX = 50。超過時は 422。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from constants import DEFAULT_TABLE, INGEST_URLS_MAX, SUPPORTED_EXTENSIONS
from db import db_url_exists
from jobs import jobs_enqueue
from jobs_db import jdb_has_pending_url

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


class ReqIngestUrl(BaseModel):
    url: str
    duplicate_action: str = "overwrite"  # "overwrite" | "skip" | "new"
    on_changed: str = "overwrite"  # "overwrite" | "new"
    table: str = DEFAULT_TABLE
    enable_llm_summary: bool | None = None  # None = use config default


class ReqIngestUrls(BaseModel):
    urls: list[str]
    duplicate_action: str = "overwrite"
    on_changed: str = "overwrite"
    table: str = DEFAULT_TABLE
    enable_llm_summary: bool | None = None  # None = use config default


class ReqIngestText(BaseModel):
    title: str
    text: str
    source_url: str = ""
    duplicate_action: str = "overwrite"
    on_changed: str = "overwrite"
    table: str = DEFAULT_TABLE
    enable_llm_summary: bool | None = None  # None = use config default


def _enqueue_url(
    url: str,
    duplicate_action: str,
    on_changed: str,
    table: str,
    enable_llm_summary: bool | None = None,
) -> dict:
    """1件の URL をエンキューして結果 dict を返す。重複チェック込み。"""
    if jdb_has_pending_url(url):
        return {"url": url, "job_id": None, "status": "skipped", "reason": "already_queued"}
    if duplicate_action == "skip" and db_url_exists(url, table):
        return {"url": url, "job_id": None, "status": "skipped", "reason": "duplicate"}
    payload: dict = {
        "url": url,
        "duplicate_action": duplicate_action,
        "on_changed": on_changed,
        "table": table,
    }
    if enable_llm_summary is not None:
        payload["enable_llm_summary"] = enable_llm_summary
    job_id = jobs_enqueue("ingest_url", payload)
    return {"url": url, "job_id": job_id, "status": "pending"}


@router.post("/url")
def ingest_url(req: ReqIngestUrl):
    """Enqueue a URL fetch-and-ingest job. Returns job_id immediately.

    エンキュー前の重複チェック（LLM 不要）:
    1. 同じ URL のジョブがキューに pending/running で存在 → スキップ
    2. duplicate_action=skip かつ同一 source_url が DB に存在 → スキップ
    """
    result = _enqueue_url(req.url, req.duplicate_action, req.on_changed, req.table, req.enable_llm_summary)
    # 単件エンドポイントは url フィールドなしの従来レスポンス形式を維持
    return {k: v for k, v in result.items() if k != "url"}


@router.post("/urls")
def ingest_urls(req: ReqIngestUrls):
    """URL を一括エンキューする。バッチサイズ上限: INGEST_URLS_MAX（50）件。

    超過時は 422 を返す。クライアントは 50 件以下に分割して送ること。
    各 URL に対して /url と同じ重複チェックを行い、result に status: pending / skipped を返す。
    """
    if len(req.urls) > INGEST_URLS_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"Batch size {len(req.urls)} exceeds limit of {INGEST_URLS_MAX}. "
            f"Split into batches of at most {INGEST_URLS_MAX} URLs.",
        )
    results = [
        _enqueue_url(url, req.duplicate_action, req.on_changed, req.table, req.enable_llm_summary) for url in req.urls
    ]
    accepted = sum(1 for r in results if r["status"] == "pending")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    return {"results": results, "accepted": accepted, "skipped": skipped}


@router.post("/text")
def ingest_text(req: ReqIngestText):
    """Enqueue a text ingest job. Persisted to SQLite — survives restarts."""
    if not req.text.strip():
        raise HTTPException(status_code=422, detail="text is empty")
    if req.source_url:
        if jdb_has_pending_url(req.source_url):
            return {"job_id": None, "status": "skipped", "reason": "already_queued"}
        if req.duplicate_action == "skip" and db_url_exists(req.source_url, req.table):
            return {"job_id": None, "status": "skipped", "reason": "duplicate"}
    payload: dict = {
        "title": req.title,
        "text": req.text,
        "source_url": req.source_url,
        "duplicate_action": req.duplicate_action,
        "on_changed": req.on_changed,
        "table": req.table,
    }
    if req.enable_llm_summary is not None:
        payload["enable_llm_summary"] = req.enable_llm_summary
    job_id = jobs_enqueue("ingest_text", payload)
    return {"job_id": job_id, "status": "pending"}


@router.post("/file")
async def ingest_file(
    file: UploadFile = File(...),
    duplicate_action: str = Form("overwrite"),
    on_changed: str = Form("overwrite"),
    table: str = Form(DEFAULT_TABLE),
):
    """Save uploaded file to a temp path and enqueue for processing. Returns job_id."""
    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=422, detail=f"Unsupported file format: {suffix}")

    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    job_id = jobs_enqueue(
        "ingest_file",
        {
            "tmp_path": tmp_path,
            "filename": file.filename,
            "duplicate_action": duplicate_action,
            "on_changed": on_changed,
            "table": table,
        },
    )
    return {"job_id": job_id, "status": "pending", "filename": file.filename}
