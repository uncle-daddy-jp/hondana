"""routers/config.py — Runtime config, API key management, and clip bookmarklet entry point."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from config_store import load_config, save_config
from constants import DEFAULT_TABLE
from ingest.llm_client import llm_client_build
from ingest.pipeline import pipeline_ingest_url
from jobs_db import jdb_has_pending_url

logger = logging.getLogger(__name__)

router = APIRouter(tags=["config"])


# ── Request models ────────────────────────────────────────────────────────────


class ReqCreateKey(BaseModel):
    name: str


# ── Bookmarklet HTML template ─────────────────────────────────────────────────

_CLIP_HTML = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>HONDANA Clip</title>
<style>
  body{{font-family:sans-serif;display:flex;align-items:center;
       justify-content:center;height:100vh;margin:0;background:{bg};}}
  .msg{{text-align:center;font-size:1.4rem;}}
  .sub{{font-size:0.85rem;color:#666;margin-top:0.5rem;}}
</style></head>
<body><div class="msg">{icon} {text}<div class="sub">{sub}</div></div>
<script>setTimeout(function(){{window.close();}},3000);</script>
</body></html>
"""


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/clip", response_class=HTMLResponse)
def clip_url(request: Request, url: str = Query(""), table: str = Query(DEFAULT_TABLE)):
    """ブックマークレットから呼ばれる。URL を HONDANA に取り込んで結果を表示する。"""
    state = request.app.state.state_ref
    if not url:
        return HTMLResponse(_CLIP_HTML.format(bg="#fff3f3", icon="❌", text="URL が空です", sub=""))
    if jdb_has_pending_url(url):
        return HTMLResponse(_CLIP_HTML.format(bg="#fffde7", icon="⏭", text="スキップ（処理待ち）", sub=url[:80]))
    try:
        result = pipeline_ingest_url(url, state.llm, state.embed, {**state.cfg}, table_name=table)
    except Exception:
        logger.exception("clip ingest failed: %s", url)
        return HTMLResponse(_CLIP_HTML.format(bg="#fff3f3", icon="❌", text="取り込み失敗", sub=url[:80]))
    if result.get("status") == "skipped":
        return HTMLResponse(
            _CLIP_HTML.format(
                bg="#fffde7",
                icon="⏭",
                text="スキップ（登録済み）",
                sub=url[:80],
            )
        )
    return HTMLResponse(
        _CLIP_HTML.format(
            bg="#f0fff0",
            icon="✅",
            text="保存しました",
            sub=url[:80],
        )
    )


@router.get("/api/config")
def api_get_config():
    cfg = load_config()
    # Mask sensitive keys
    safe = {k: v for k, v in cfg.items() if "key" not in k.lower()}
    return safe


@router.post("/api/config")
def api_save_config(cfg: dict, request: Request):
    state = request.app.state.state_ref
    # Preserve fields not managed by the Settings form
    existing = load_config()
    for preserve_key in (
        "api_keys",
        "data_dir",
        "embedding_model",
        "embedding_url",
        "llm_answer_provider",
        "llm_answer_url",
        "llm_answer_thinking_budget",
        "llm_summary_url",
    ):
        if preserve_key in existing and preserve_key not in cfg:
            cfg[preserve_key] = existing[preserve_key]
    save_config(cfg)
    # Reload state
    state.cfg = cfg
    state.data_dir = Path(cfg.get("data_dir", "/data/hondana"))
    state.inbox_dir = Path(cfg.get("inbox_dir", "/inbox"))
    state.done_dir = Path(cfg.get("done_dir", "/done"))
    state.llm = llm_client_build(cfg)
    return {"ok": True}


@router.get("/api/keys")
def api_list_keys():
    """Return all named API keys (key values shown in full — admin only)."""
    cfg = load_config()
    return {"api_keys": cfg.get("api_keys", [])}


@router.post("/api/keys")
def api_create_key(req: ReqCreateKey, request: Request):
    """Generate a new API key with the given name."""
    state = request.app.state.state_ref
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    cfg = load_config()
    api_keys: list[dict] = cfg.get("api_keys", [])
    if any(k.get("name") == req.name for k in api_keys):
        raise HTTPException(status_code=409, detail=f"Key name '{req.name}' already exists")
    new_key = f"hondana-{secrets.token_hex(16)}"
    api_keys.append({"name": req.name, "key": new_key})
    cfg["api_keys"] = api_keys
    save_config(cfg)
    state.cfg = cfg
    return {"name": req.name, "key": new_key}


@router.delete("/api/keys/{name}")
def api_delete_key(name: str, request: Request):
    """Delete an API key by name."""
    state = request.app.state.state_ref
    cfg = load_config()
    api_keys: list[dict] = cfg.get("api_keys", [])
    new_keys = [k for k in api_keys if k.get("name") != name]
    if len(new_keys) == len(api_keys):
        raise HTTPException(status_code=404, detail=f"Key '{name}' not found")
    cfg["api_keys"] = new_keys
    save_config(cfg)
    state.cfg = cfg
    return {"ok": True, "deleted": name}
