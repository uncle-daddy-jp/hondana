"""Tests for pipeline_ingest_document duplicate/on_changed logic."""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── 外部依存を stub してから _document だけを直接ロード ─────────────────────────
_API = Path(__file__).parent.parent
if str(_API) not in sys.path:
    sys.path.insert(0, str(_API))

# このファイルが sys.modules に注入する stub を後で正確に復元するため、
# stub 登録前の元モジュール（無ければ None）を記録しておく（後続テストの import を汚染しないため）。
_STUB_KEYS = (
    "ingest",
    "ingest.pipeline",
    "ingest.chunker",
    "ingest.pipeline._duplicate",
    "ingest.pipeline._rows",
    "ingest.pipeline._document",
    "constants",
    "db",
    "hondana_types",
)
_ORIG_MODULES = {k: sys.modules.get(k) for k in _STUB_KEYS}

# ingest / ingest.pipeline パッケージを stub 登録（__init__ を実行しない）
for _pkg in ("ingest", "ingest.pipeline", "ingest.chunker"):
    if _pkg not in sys.modules:
        mod = types.ModuleType(_pkg)
        mod.__path__ = []  # パッケージとして認識させる
        mod.__package__ = _pkg
        sys.modules[_pkg] = mod

# _document.py が使う隣接モジュールを stub 登録（必要なシンボルを付与）
_dup_mod = types.ModuleType("ingest.pipeline._duplicate")
_dup_mod._pipeline_check_duplicate = MagicMock()
sys.modules["ingest.pipeline._duplicate"] = _dup_mod

_rows_mod = types.ModuleType("ingest.pipeline._rows")
_rows_mod._pipeline_build_rows = MagicMock()
_rows_mod._pipeline_delete_article = MagicMock()
_rows_mod._rows_to_points = MagicMock(return_value=[])
sys.modules["ingest.pipeline._rows"] = _rows_mod

# constants / db / hondana_types を stub
if "constants" not in sys.modules:
    c = types.ModuleType("constants")
    c.MIN_TEXT_LENGTH = 50
    c.DEFAULT_TABLE = "default"
    sys.modules["constants"] = c
if "db" not in sys.modules:
    db_mod = types.ModuleType("db")
    db_mod.qdrant_client = MagicMock()
    sys.modules["db"] = db_mod
if "hondana_types" not in sys.modules:
    h = types.ModuleType("hondana_types")
    h.EmbedModel = object
    h.LLMClient = object
    sys.modules["hondana_types"] = h

# _document.py を直接ロード（パッケージ __init__ を経由しない）
_spec = importlib.util.spec_from_file_location(
    "ingest.pipeline._document",
    _API / "ingest" / "pipeline" / "_document.py",
)
_doc_mod = importlib.util.module_from_spec(_spec)
_doc_mod.__package__ = "ingest.pipeline"
sys.modules["ingest.pipeline._document"] = _doc_mod
_spec.loader.exec_module(_doc_mod)

pipeline_ingest_document = _doc_mod.pipeline_ingest_document

# 後続テストへの汚染防止: ここで注入した stub を元の状態に復元する。
# _doc_mod は読み込み済みで必要な参照を内部に束縛しているため、復元しても本ファイルの
# テスト（_doc_mod を直接 patch.object する）は動作する。元が実モジュールならそれを戻し、
# 元が無ければ削除する（先行テストが実モジュールを読み込んでいても汚染しない）。
for _k, _orig in _ORIG_MODULES.items():
    if _orig is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _orig


# ── テスト用フィクスチャ ──────────────────────────────────────────────────────


@dataclass
class _Doc:
    title: str = "Test Article"
    source_url: str = "https://example.com/test"
    raw_text: str = "x" * 200
    content_hash: str = "newhash"
    sections: list = field(default_factory=list)


_MINIMAL_ROW = [
    {
        "article_id": "new-article-id",
        "level": 1,
        "title": "Test Article",
        "text": "summary",
        "source_url": "https://example.com/test",
        "source_type": "article",
        "content_hash": "newhash",
        "tags": [],
        "recorded_at": "2026-01-01",
        "embedding": [0.0] * 384,
    }
]


def _mock_client():
    client = MagicMock()
    client.upsert.return_value = None
    return client


# ── on_changed="overwrite" ────────────────────────────────────────────────────


def test_on_changed_overwrite_deletes_old_article_when_content_changed():
    """duplicate_action=overwrite + on_changed=overwrite → 旧記事を削除して上書き。"""
    deleted = []

    with (
        patch.object(_doc_mod, "_pipeline_check_duplicate", return_value={"existing_id": "old-id", "same_hash": False}),
        patch.object(_doc_mod, "_pipeline_build_rows", return_value=_MINIMAL_ROW),
        patch.object(_doc_mod, "_pipeline_delete_article", side_effect=lambda col, aid: deleted.append(aid)),
        patch.object(_doc_mod, "qdrant_client", return_value=_mock_client()),
    ):
        result = pipeline_ingest_document(
            _Doc(),
            "2026-01-01",
            MagicMock(),
            MagicMock(),
            {},
            collection="test_col",
            duplicate_action="overwrite",
            on_changed="overwrite",
        )

    assert "old-id" in deleted
    assert result["status"] == "overwritten"


# ── on_changed="new" (バグ修正対象) ──────────────────────────────────────────


def test_on_changed_new_keeps_old_article_when_content_changed():
    """duplicate_action=overwrite + on_changed=new → 内容変化時に旧記事を保持して新規追加。"""
    deleted = []

    with (
        patch.object(_doc_mod, "_pipeline_check_duplicate", return_value={"existing_id": "old-id", "same_hash": False}),
        patch.object(_doc_mod, "_pipeline_build_rows", return_value=_MINIMAL_ROW),
        patch.object(_doc_mod, "_pipeline_delete_article", side_effect=lambda col, aid: deleted.append(aid)),
        patch.object(_doc_mod, "qdrant_client", return_value=_mock_client()),
    ):
        result = pipeline_ingest_document(
            _Doc(),
            "2026-01-01",
            MagicMock(),
            MagicMock(),
            {},
            collection="test_col",
            duplicate_action="overwrite",
            on_changed="new",
        )

    assert "old-id" not in deleted, "on_changed=new のとき旧記事を削除してはいけない"
    assert result["status"] == "inserted"
