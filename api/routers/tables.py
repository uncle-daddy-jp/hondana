"""
routers/tables.py — テーブル管理エンドポイント。

  GET    /api/tables                      → テーブル一覧（説明文・記事数・チャンク数付き）
  POST   /api/tables                      → 新しいテーブルを作成（説明文オプション）
  PATCH  /api/tables/{name}               → テーブルの説明文を更新
  GET    /api/tables/{name}/preview       → 削除影響プレビュー
  DELETE /api/tables/{name}?confirm=name  → テーブル削除（二段階確認）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from constants import DEFAULT_TABLE
from db import db_drop_table, db_list_tables, db_validate_table_name
from ingest.pipeline import pipeline_open_collection
from manager import manager_stats
from tables_meta import tmeta_build_list, tmeta_delete, tmeta_set

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tables", tags=["tables"])


class ReqCreateTable(BaseModel):
    name: str
    description: str = ""


class ReqPatchTable(BaseModel):
    description: str


@router.get("")
def list_tables(request: Request):
    """テーブル一覧を返す。各テーブルの説明文・記事数・チャンク数を含む。"""
    state = request.app.state.state_ref
    return tmeta_build_list(state.data_dir)


@router.post("")
def create_table(req: ReqCreateTable, request: Request):
    """新しいテーブルを作成する。description が指定された場合は説明文も保存する。"""
    state = request.app.state.state_ref
    try:
        db_validate_table_name(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if req.name in db_list_tables():
        raise HTTPException(status_code=409, detail=f"Table '{req.name}' already exists")
    embed_dim = state.embed.get_sentence_embedding_dimension()
    pipeline_open_collection(embed_dim, req.name)
    if req.description.strip():
        tmeta_set(state.data_dir, req.name, req.description.strip())
    return {"name": req.name, "description": req.description.strip(), "status": "created"}


@router.patch("/{name}")
def update_table_description(name: str, req: ReqPatchTable, request: Request):
    """テーブルの説明文を更新する。空文字を渡すと説明文を削除する。"""
    state = request.app.state.state_ref
    if name not in db_list_tables():
        raise HTTPException(status_code=404, detail=f"Table '{name}' not found")
    desc = req.description.strip()
    if desc:
        tmeta_set(state.data_dir, name, desc)
    else:
        tmeta_delete(state.data_dir, name)
    return {"name": name, "description": desc}


@router.get("/{name}/preview")
def preview_table_delete(name: str, request: Request):
    """テーブル削除時の影響（記事数・チャンク数）をプレビューする。"""
    if name not in db_list_tables():
        raise HTTPException(status_code=404, detail=f"Table '{name}' not found")
    try:
        stats = manager_stats(table_name=name)
    except Exception:
        logger.warning("Failed to get stats for table %s", name, exc_info=True)
        stats = {"total_articles": 0, "total_chunks": 0, "article_count": 0, "answer_count": 0}
    return {"name": name, **stats}


@router.delete("/{name}")
def drop_table(
    name: str,
    request: Request,
    confirm: str = Query("", description="削除確認。テーブル名と一致する必要がある"),
):
    """テーブルを削除する。confirm にテーブル名を渡すことで誤削除を防ぐ。"""
    state = request.app.state.state_ref
    if confirm != name:
        raise HTTPException(
            status_code=400,
            detail=f"Confirmation mismatch. Pass ?confirm={name} to confirm deletion.",
        )
    if name == DEFAULT_TABLE:
        raise HTTPException(status_code=400, detail=f"Cannot delete the default table '{DEFAULT_TABLE}'")
    try:
        db_drop_table(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    tmeta_delete(state.data_dir, name)
    return {"name": name, "status": "dropped"}
