"""routers/mcp.py — MCP endpoints (REST + JSON-RPC 2.0) for Claude Code integration."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from constants import DEFAULT_TABLE
from manager import (
    manager_bulk_delete,
    manager_delete_article,
    manager_find_article_chunks,
    manager_get_recent,
)
from ingest.pipeline import pipeline_append_to_article, pipeline_ingest_directory, pipeline_ingest_url
from tables_meta import tmeta_build_list

from routers.ask import ReqSaveAnswer, ReqSearch, api_save_answer, api_search, api_tags

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])


# ── Request models ────────────────────────────────────────────────────────────


class ReqMcpIngestNew(BaseModel):
    url: str | None = None
    table: str = DEFAULT_TABLE
    enable_llm_summary: bool | None = None


class ReqMcpDeleteKnowledge(BaseModel):
    article_id: str | None = None
    conditions: dict | None = None
    table: str = DEFAULT_TABLE


class ReqMcpGetArticle(BaseModel):
    article_id: str
    table: str = DEFAULT_TABLE


class ReqMcpAppendToArticle(BaseModel):
    article_id: str
    append_text: str
    table: str = DEFAULT_TABLE


class ReqMcpGetRecent(BaseModel):
    n: int = 10
    table: str | None = None


class ReqMcpSearchByTag(BaseModel):
    tag: str
    table: str | None = None
    limit: int = 20


# ── REST endpoints ────────────────────────────────────────────────────────────


@router.post("/search_knowledge")
def mcp_search_knowledge(req: ReqSearch, request: Request):
    # MCP/agent default: query expansion ON (bridges vocabulary gaps, e.g. "tps向上" → "MTP").
    # Hybrid also ON by default for agents so exact terms (model names) are matched.
    if req.expand_queries is None:
        req.expand_queries = True
    if req.hybrid is None:
        req.hybrid = True
    return api_search(req, request)


@router.post("/ingest_new")
def mcp_ingest_new(req: ReqMcpIngestNew, request: Request):
    state = request.app.state.state_ref
    cfg = {**state.cfg}
    if req.enable_llm_summary is not None:
        cfg["enable_llm_summary"] = req.enable_llm_summary

    if req.url:
        result = pipeline_ingest_url(
            req.url,
            state.llm,
            state.embed,
            cfg,
            table_name=req.table,
        )
        processed = [req.url] if result["status"] != "skipped" else []
        skipped = [req.url] if result["status"] == "skipped" else []
        return {
            "processed": processed,
            "count": len(processed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }

    result = pipeline_ingest_directory(
        state.inbox_dir,
        state.done_dir,
        state.llm,
        state.embed,
        cfg,
        table_name=req.table,
    )
    return {
        "processed": result["processed"],
        "count": len(result["processed"]),
        "skipped": result["skipped"],
        "skipped_count": len(result["skipped"]),
    }


@router.post("/save_answer")
def mcp_save_answer(req: ReqSaveAnswer, request: Request):
    return api_save_answer(req, request)


@router.get("/list_tags")
def mcp_list_tags(request: Request, table: str = Query(DEFAULT_TABLE)):
    return api_tags(request, table)


@router.get("/list_tables")
def mcp_list_tables(request: Request):
    """テーブル一覧と各テーブルの説明文・記事数・チャンク数を返す。"""
    state = request.app.state.state_ref
    return tmeta_build_list(state.data_dir)


@router.post("/delete_knowledge")
def mcp_delete_knowledge(req: ReqMcpDeleteKnowledge, request: Request):
    if req.article_id:
        deleted = manager_delete_article(req.article_id, table_name=req.table)
        return {"deleted_rows": deleted}
    if req.conditions:
        deleted = manager_bulk_delete(req.conditions, table_name=req.table)
        return {"deleted_rows": deleted}
    raise HTTPException(status_code=400, detail="Provide article_id or conditions")


@router.post("/append_to_article")
def mcp_append_to_article(req: ReqMcpAppendToArticle, request: Request):
    """既存記事に追記してL1サマリーを再生成する。article_id は変わらない。"""
    state = request.app.state.state_ref
    try:
        result = pipeline_append_to_article(
            article_id=req.article_id,
            append_text=req.append_text,
            llm_client=state.llm,
            embed_model=state.embed,
            cfg={**state.cfg},
            table_name=req.table,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result


@router.post("/get_article")
def mcp_get_article(req: ReqMcpGetArticle, request: Request):
    """article_id で全チャンク（L1+L2+L3）を返す。記事を全文読むために使う。"""
    chunks, _ = manager_find_article_chunks(req.article_id, preferred_table=req.table)
    if not chunks:
        raise HTTPException(status_code=404, detail=f"Article not found: {req.article_id}")
    return {"article_id": req.article_id, "chunks": chunks, "chunk_count": len(chunks)}


@router.post("/get_recent")
def mcp_get_recent(req: ReqMcpGetRecent, request: Request):
    """直近N件の記事一覧を返す。「最近何を調べたか」を把握するために使う。"""
    articles = manager_get_recent(n=req.n, table_name=req.table)
    return {"articles": articles, "count": len(articles)}


@router.post("/search_by_tag")
def mcp_search_by_tag(req: ReqMcpSearchByTag, request: Request):
    """タグ名で記事を絞り込む。特定トピックの記事を一括参照するために使う。"""
    from db import db_list_tables, db_row_to_dict, qdrant_client
    from retriever import _build_filter

    # (tags_any=[tag] AND level==1) — identical condition pair, now shared with _build_filter.
    flt = _build_filter(tags_any=[req.tag], level=1)
    target_tables = db_list_tables() if req.table is None else [req.table]
    results: list[dict] = []
    for tname in target_tables:
        try:
            pts, _ = qdrant_client().scroll(
                collection_name=tname,
                scroll_filter=flt,
                limit=req.limit,
                with_payload=True,
                with_vectors=False,
            )
            for p in pts:
                d = db_row_to_dict(p)
                d["_table_name"] = tname
                results.append(d)
        except Exception as exc:
            logger.error("search_by_tag failed table=%s: %s", tname, exc)

    results.sort(key=lambda a: a.get("recorded_at", ""), reverse=True)
    return {"tag": req.tag, "articles": results[: req.limit], "count": len(results[: req.limit])}


# ── MCP Streamable HTTP (JSON-RPC 2.0) ───────────────────────────────────────

_MCP_TOOLS = [
    {
        "name": "search_knowledge",
        "description": (
            "【HONDANA専用・メインツール】HONDANAの知識ベースをベクトル検索し、関連チャンクを返す。\n"
            "調査・検索・質問にはまずこれを使う。回答文の生成は呼び出し元で行う。\n"
            "【テーブル選択】table を省略または null にすると全テーブル横断検索。特定テーブルに絞る場合は list_tables で確認してから指定する。\n"
            "【テーブル】用途別の名前空間。既定は hondana_chunks。各テーブルの説明・件数は list_tables で確認できる。\n"
            "【戻り値】chunks[].article_id と chunks[]._table_name を記録しておくこと。get_article を呼ぶときに必要。\n"
            "  各 chunk は出典用に chunk_id / section_id / section_heading / position / source_type も含む。\n"
            "【date_filter】preset は today/week/month/3months/year。または date_from/date_to (YYYY-MM-DD) で範囲指定。\n"
            "【filters】tags_all=全タグAND / tags_any=いずれかOR / tags_exclude=除外 / last_n_days=直近N日 / url_prefix=URL前方一致。\n"
            "【hybrid】既定 ON。ベクトル検索＋全文(キーワード)検索を RRF 融合（モデル名等の固有語に強い）。\n"
            "【expand_queries】既定 ON。質問を関連語・同義語に展開して検索（例『tps向上』→『MTP/推論高速化』）。LLM 呼び出しが増え少し遅くなる。\n"
            "【recency_decay】true で新しい記事を優先（recency_half_life_days で半減期を指定）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "検索クエリ（日本語可）"},
                "source_type": {"type": "string", "default": "all"},
                "top_k": {"type": "integer", "default": 10, "description": "取得チャンク数（デフォルト10）"},
                "distance_threshold": {"type": "number", "description": "類似度閾値。小さいほど厳密"},
                "date_filter": {
                    "type": "object",
                    "description": "日付フィルタ。preset: today/week/month/3months/year、またはdate_from/date_to (YYYY-MM-DD) で指定",
                    "properties": {
                        "preset": {"type": "string"},
                        "date_from": {"type": "string"},
                        "date_to": {"type": "string"},
                    },
                },
                "filters": {
                    "type": "object",
                    "description": "メタデータ絞り込み（ベクトル/キーワード両方に適用）",
                    "properties": {
                        "tags_all": {"type": "array", "items": {"type": "string"}, "description": "全タグを含む(AND)"},
                        "tags_any": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "いずれかのタグを含む(OR)",
                        },
                        "tags_exclude": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "これらのタグを除外(NOT)",
                        },
                        "last_n_days": {"type": "integer", "description": "直近N日の記事に限定"},
                        "url_prefix": {"type": "string", "description": "source_url の前方一致/ドメイン部分一致"},
                    },
                },
                "hybrid": {"type": "boolean", "description": "ベクトル＋全文検索を RRF 融合する（既定 ON）"},
                "expand_queries": {
                    "type": "boolean",
                    "description": "質問を関連語に展開して検索（既定 ON・語彙ギャップ対策）",
                },
                "recency_decay": {"type": "boolean", "description": "新しい記事を優先する（時間減衰）"},
                "recency_half_life_days": {"type": "number", "description": "recency_decay の半減期（日）。既定180"},
                "table": {"type": "string", "description": "検索対象テーブル名。省略または null で全テーブル横断検索"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "ingest_new",
        "description": (
            "【HONDANA専用】URLまたはinbox_dirのファイルを知識ベースに取り込む。\n"
            "【URLを保存する場合】url フィールドに保存したいURLを必ず指定すること。指定しないとinboxのファイル処理になる。\n"
            '  例: {"url": "https://example.com/article", "table": "hondana_chunks"}\n'
            "【inboxファイルを処理する場合】url を省略する。inbox_dir に置いたファイルを一括取り込む。\n"
            "【戻り値】processed: 取り込んだURL/ファイル一覧, count: 件数, skipped: 登録済みでスキップしたもの。\n"
            "  count=0 の場合はURLが登録済み（skipped）か、inboxが空。\n"
            "【テーブル】用途別の名前空間。既定は hondana_chunks。list_tables で一覧を確認できる。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "取り込むURL。指定するとURL単体を同期取り込み。省略するとinbox_dirのファイルを処理",
                },
                "table": {
                    "type": "string",
                    "default": "hondana_chunks",
                    "description": "取り込み先テーブル名（既定 hondana_chunks）。list_tables で一覧を確認できる",
                },
                "enable_llm_summary": {
                    "type": "boolean",
                    "description": "LLMサマリー生成を有効にするか。省略時はconfig設定に従う",
                },
            },
        },
    },
    {
        "name": "save_answer",
        "description": (
            "【HONDANA専用】会話・調査結果・LLM回答をテキストとして知識ベースに保存する。\n"
            "URLではなくテキストを保存したいときに使う（会話のまとめ、調査結果、メモなど）。\n"
            "【使い分け】URLを保存したい→ingest_new(url=...) / テキストを保存したい→save_answer\n"
            '  例: {"question": "ある技術の弱点は？", "answer": "...調査のまとめ...", "table": "hondana_chunks"}\n'
            "【重複】duplicate_action: new=毎回新規(既定) / skip=同じ質問が既にあれば作らず既存IDを返す / overwrite=同じ質問を上書き更新(article_id維持)。\n"
            "【出所】agent_id・origin を渡すと各チャンクに記録され、後で誰がいつ保存したか追える。\n"
            "【テーブル】用途別の名前空間。既定は hondana_chunks。list_tables で一覧を確認できる。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "質問・トピック文（重複判定のキー）"},
                "answer": {"type": "string", "description": "回答・まとめ・保存したい内容"},
                "table": {"type": "string", "default": "hondana_chunks", "description": "保存先テーブル名"},
                "duplicate_action": {
                    "type": "string",
                    "enum": ["new", "skip", "overwrite"],
                    "default": "new",
                    "description": "同じ質問が既にある場合の挙動",
                },
                "agent_id": {"type": "string", "description": "保存したエージェントの識別子（任意）"},
                "origin": {"type": "string", "description": "保存元・文脈の識別子（任意）"},
            },
            "required": ["question", "answer"],
        },
    },
    {
        "name": "get_article",
        "description": (
            "【HONDANA専用】article_id で記事の全チャンク（L1サマリー＋L2セクション＋L3段落）を取得する。\n"
            "【重要】article_id は必ず search_knowledge または get_recent の結果から取得すること。自分で生成・推測してはいけない。\n"
            "【重要】table は search_knowledge の結果チャンクの _table_name に必ず合わせること。テーブルが違うと404になる。\n"
            "  正しい使い方: search_knowledge → chunks[0].article_id と chunks[0]._table_name を取り出す → get_article に渡す\n"
            '  例: {"article_id": "abc123def456", "table": "hondana_chunks"}'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {
                    "type": "string",
                    "description": "取得する記事のarticle_id。search_knowledgeまたはget_recentの結果から取得すること",
                },
                "table": {
                    "type": "string",
                    "default": "hondana_chunks",
                    "description": "テーブル名。search_knowledgeの結果の_table_nameに合わせること",
                },
            },
            "required": ["article_id"],
        },
    },
    {
        "name": "get_recent",
        "description": (
            "【HONDANA専用】直近N件の記事一覧（タイトル・URL・タグ・article_id・recorded_at）を返す。\n"
            "「最近何を調べたか」「先週保存した記事を整理して」などのユースケースで使う。\n"
            "【戻り値の article_id と table】get_article を呼ぶときにそのまま使える。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "default": 10, "description": "取得件数（最大50）"},
                "table": {"type": "string", "description": "テーブル名。省略または null で全テーブル横断"},
            },
        },
    },
    {
        "name": "search_by_tag",
        "description": (
            "【HONDANA専用】タグ名で記事を絞り込む。\n"
            "「ComfyUI関連の記事を全部読む」「AIエージェント関連をまとめて」などのユースケースで使う。\n"
            "タグ名一覧は list_tags で確認できる。\n"
            "【戻り値の article_id と _table_name】get_article を呼ぶときにそのまま使える。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "絞り込むタグ名。list_tagsで確認してから指定する"},
                "table": {"type": "string", "description": "テーブル名。省略または null で全テーブル横断"},
                "limit": {"type": "integer", "default": 20, "description": "最大取得件数"},
            },
            "required": ["tag"],
        },
    },
    {
        "name": "list_tables",
        "description": (
            "【HONDANA専用】テーブル一覧と各テーブルの説明文・記事数・チャンク数を返す。\n"
            "search_knowledge で特定テーブルを指定する前に呼んで、どのテーブルを対象にするか判断する。\n"
            "テーブルは用途別の名前空間。既定は hondana_chunks。説明文・記事数を見て対象を選ぶ。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_tags",
        "description": (
            "【HONDANA専用】知識ベースに登録されている全タグ一覧を返す。\n"
            "search_by_tag を呼ぶ前に、存在するタグ名を確認するために使う。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "append_to_article",
        "description": (
            "【HONDANA専用】既存記事に追記テキストを加え、L1サマリーを再生成して同じ article_id で保存し直す。\n"
            "調査を継続して同じ記事に知識を積み重ねたいときに使う。article_id と参照は変わらない。\n"
            "【重要】article_id は search_knowledge または get_recent の結果から取得すること。\n"
            "【重要】table は元の記事の _table_name に合わせること。\n"
            "【追記形式】append_text には追加したい内容をテキストで渡す。## 見出し で新しいセクションを作れる。\n"
            '  例: {"article_id": "abc123", "append_text": "## 続報\\n\\n新しくわかったこと...", "table": "hondana_chunks"}'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {
                    "type": "string",
                    "description": "追記対象の記事ID。search_knowledgeまたはget_recentの結果から取得すること",
                },
                "append_text": {"type": "string", "description": "追記するテキスト。## 見出し でセクションを区切れる"},
                "table": {
                    "type": "string",
                    "default": "hondana_chunks",
                    "description": "テーブル名。元の記事の_table_nameに合わせること",
                },
            },
            "required": ["article_id", "append_text"],
        },
    },
    {
        "name": "delete_knowledge",
        "description": (
            "【HONDANA専用】記事を削除する。\n"
            '【article_id指定】1件削除: {"article_id": "abc123", "table": "hondana_chunks"}\n'
            '【conditions指定】条件一括削除: {"conditions": {"source_url": "https://..."}, "table": "hondana_chunks"}\n'
            "【重要】article_id は search_knowledge または get_recent の結果から取得すること。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {"type": "string", "description": "削除する記事ID（search_knowledgeの結果から取得）"},
                "conditions": {"type": "object", "description": '一括削除条件（例: {"source_url": "https://..."}）'},
                "table": {"type": "string", "default": "hondana_chunks", "description": "テーブル名"},
            },
        },
    },
]


# Short descriptions for the GET discovery manifest. Keys MUST match _MCP_TOOLS names —
# the drift-guard test enforces it so the manifest can never silently omit a tool again.
_MANIFEST_DESCRIPTIONS = {
    "search_knowledge": "【HONDANA専用・デフォルト】HONDANA を明示した調査・検索・質問すべてに使用。ベクトル検索＋L2親チャンク取得で関連チャンクを返す。",
    "ingest_new": "【HONDANA専用】url指定でURLを同期取り込み（Playwrightフォールバック込み）。url省略時はinbox_dirの未処理ファイルを取り込む。",
    "save_answer": "【HONDANA専用】LLM回答を知識ベースに保存する。",
    "get_article": "【HONDANA専用】article_idで記事の全チャンク（L1+L2+L3）を取得する。深い分析・考察に使う。",
    "get_recent": "【HONDANA専用】直近N件の記事一覧を返す。「最近何を調べたか」の把握に使う。",
    "search_by_tag": "【HONDANA専用】タグ名で記事を絞り込む。特定トピックを一括参照するために使う。",
    "list_tables": "【HONDANA専用】テーブル一覧と各テーブルの説明文・記事数・チャンク数を返す。",
    "list_tags": "【HONDANA専用】登録済みタグ一覧を返す。",
    "append_to_article": "【HONDANA専用】既存記事に追記して L1 を再生成し、同じ article_id で保存し直す。",
    "delete_knowledge": "【HONDANA専用】article_idまたは条件指定でナレッジを削除する。",
}


def _manifest_tools() -> list[dict]:
    """Derive the GET-manifest tool list from _MCP_TOOLS so the two cannot drift.

    Every tools/list entry gets a manifest entry with its short description and REST endpoint
    (this is what previously drifted — append_to_article was missing from the hand-written list).
    """
    return [
        {
            "name": t["name"],
            "description": _MANIFEST_DESCRIPTIONS.get(t["name"], t["description"].split("\n", 1)[0]),
            "endpoint": f"/mcp/{t['name']}",
        }
        for t in _MCP_TOOLS
    ]


def _build_dispatch_table(request: Request) -> dict:
    return {
        "search_knowledge": lambda args: mcp_search_knowledge(ReqSearch(**args), request),
        "append_to_article": lambda args: mcp_append_to_article(ReqMcpAppendToArticle(**args), request),
        "ingest_new": lambda args: mcp_ingest_new(ReqMcpIngestNew(**args), request),
        "save_answer": lambda args: mcp_save_answer(ReqSaveAnswer(**args), request),
        "list_tags": lambda args: mcp_list_tags(request, table=args.get("table", DEFAULT_TABLE)),
        "list_tables": lambda args: mcp_list_tables(request),
        "delete_knowledge": lambda args: mcp_delete_knowledge(ReqMcpDeleteKnowledge(**args), request),
        "get_article": lambda args: mcp_get_article(ReqMcpGetArticle(**args), request),
        "get_recent": lambda args: mcp_get_recent(ReqMcpGetRecent(**args), request),
        "search_by_tag": lambda args: mcp_search_by_tag(ReqMcpSearchByTag(**args), request),
    }


def _mcp_dispatch(name: str, args: dict, request: Request) -> dict:
    dispatch = _build_dispatch_table(request)
    if name not in dispatch:
        raise ValueError(f"Unknown tool: {name}")
    result = dispatch[name](args)
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
        "isError": False,
    }


# ── MCP manifest (for Claude Desktop discovery) ───────────────────────────────


@router.get("")
async def mcp_manifest(request: Request):
    # MCP Streamable HTTP: GETでSSEを要求するクライアントに対応
    if "text/event-stream" in request.headers.get("accept", ""):

        async def _sse_keepalive():
            try:
                while True:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(15)
            except asyncio.CancelledError:
                pass

        return StreamingResponse(
            _sse_keepalive(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return {
        "name": "hondana",
        "description": "HONDANA — 個人向け RAG 知識ベース。「HONDANAで」「本棚で」「HONDANAを使って」と明示された場合のみ使用すること。",
        "tools": _manifest_tools(),
    }


@router.post("")
async def mcp_jsonrpc(request: Request):
    """MCP Streamable HTTP — JSON-RPC 2.0 endpoint for Claude Code / VS Code extension."""
    try:
        msg = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})

    msg_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params", {})

    if msg_id is None:
        return Response(status_code=202)

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "hondana", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": _MCP_TOOLS}
        elif method == "tools/call":
            result = _mcp_dispatch(params.get("name", ""), params.get("arguments", {}), request)
        else:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
            )
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})
    except HTTPException as e:
        # 404/400 等の業務エラーは "Internal error" に潰さず、詳細を返す（見つからない等を明確に伝える）
        logger.info("JSON-RPC tool error: %s %s", e.status_code, e.detail)
        return JSONResponse(
            {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": f"[{e.status_code}] {e.detail}"}}
        )
    except Exception:
        logger.exception("JSON-RPC internal error")
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": "Internal error"}})
