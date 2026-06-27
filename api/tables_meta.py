"""
tables_meta.py — テーブル説明文の永続化とテーブル一覧ビルド。

テーブルの説明文を data_dir/table_descriptions.json に保存する。
外部エージェント（MCP 経由の Claude Code 等）が list_tables で説明文を読み、
どのテーブルを検索対象にするかを自律的に判断するために使う。

Public API (tmeta_ prefix):
  tmeta_get_all(data_dir)              → dict[name, description]
  tmeta_set(data_dir, name, desc)      → None
  tmeta_delete(data_dir, name)         → None
  tmeta_build_list(data_dir)           → list[{name, description, total_articles, total_chunks, ...}]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from db import db_list_tables
from manager import manager_stats

logger = logging.getLogger(__name__)

_FILENAME = "table_descriptions.json"


def tmeta_get_all(data_dir: Path) -> dict[str, str]:
    """全テーブルの説明文を返す。ファイルが存在しない場合は空 dict。"""
    f = data_dir / _FILENAME
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def tmeta_set(data_dir: Path, name: str, description: str) -> None:
    """テーブルの説明文を設定する（上書き）。"""
    data = tmeta_get_all(data_dir)
    data[name] = description
    _write(data_dir, data)


def tmeta_delete(data_dir: Path, name: str) -> None:
    """テーブルの説明文を削除する（テーブル削除時に呼ぶ）。"""
    data = tmeta_get_all(data_dir)
    if name in data:
        data.pop(name)
        _write(data_dir, data)


def tmeta_build_list(data_dir: Path) -> list[dict]:
    """テーブル名・説明文・記事数・チャンク数を含む一覧を返す。
    GET /api/tables および MCP list_tables の共通ロジック。"""
    descriptions = tmeta_get_all(data_dir)
    result = []
    for name in db_list_tables():
        try:
            stats = manager_stats(table_name=name)
        except Exception:
            logger.warning("Failed to get stats for table %s", name, exc_info=True)
            stats = {"total_articles": 0, "total_chunks": 0, "article_count": 0, "answer_count": 0}
        result.append(
            {
                "name": name,
                "description": descriptions.get(name, ""),
                **stats,
            }
        )
    return result


# ── Internal helpers ──────────────────────────────────────────────────────────


def _write(data_dir: Path, data: dict[str, str]) -> None:
    (data_dir / _FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
